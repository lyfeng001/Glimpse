'use strict';
// Glimpse phone app (v4). State lives on the hub; this page renders it and forwards input.
const APP_VERSION = '0.4.0';
// ---- manual light / dark theme (Settings → 外观). Runs first so the forced theme is in place before anything renders.
// glimpse.theme = auto | light | dark  ->  <html data-theme="light|dark"> (auto: no attribute, CSS follows prefers-color-scheme)
const THEME_BG = { dark: '#0B0C0F', light: '#F2F2F7' };
function themePref() { const t = window.localStorage.getItem('glimpse.theme'); return t === 'light' || t === 'dark' ? t : 'auto'; }
function applyTheme(t) {
  const root = document.documentElement;
  if (t === 'light' || t === 'dark') root.setAttribute('data-theme', t); else root.removeAttribute('data-theme');
  // status bar / browser chrome: forced = one colour for both media queries; auto = per-scheme colours again
  for (const m of document.querySelectorAll('meta[name=theme-color]')) {
    const scheme = /light/.test(m.media || '') ? 'light' : 'dark';
    m.setAttribute('content', THEME_BG[t === 'auto' ? scheme : t]);
  }
  const cs = document.querySelector('meta[name=color-scheme]');
  if (cs) cs.setAttribute('content', t === 'auto' ? 'dark light' : t);
}
applyTheme(themePref());
const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
const enc = new TextEncoder();
const LS = window.localStorage;
const reduceMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
// Chrome/Android: let the keyboard resize the layout viewport. Added at runtime because WebKit logs an error for the unknown key.
if (!/AppleWebKit/.test(navigator.userAgent) || /Chrome|Chromium|Edg\//.test(navigator.userAgent)) {
  const vp = document.querySelector('meta[name=viewport]');
  if (vp && !/interactive-widget/.test(vp.content)) vp.content += ', interactive-widget=resizes-content';
}

const S = {
  ws: null, wsOk: false, backoff: 500, reconnectTimer: null, pongTimer: null, lastPing: 0, lastPong: 0,
  outbox: [],           // composer messages waiting for a confirmed socket
  machines: [], byId: {}, keys: [], hub: null, options: null, haveState: false,
  view: null,           // per-open-session stream state
  hideExited: LS.getItem('glimpse.hideExited') === '1',
  groupBy: LS.getItem('glimpse.groupBy') === 'status' ? 'status' : 'machine',
  fontSize: parseInt(LS.getItem('glimpse.font') || '11', 10),
  stripOpen: LS.getItem('glimpse.strip') !== '0',
  net: { rtt: [], rx: [], rxBytes: 0, rxT: performance.now() },
  mhist: {},            // machine id -> {rtt:[], rx:[], tx:[], ts}
};

// ------------------------------------------------------------------ helpers
function b64ToBytes(b64) {
  const bin = atob(b64); const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}
function bytesToB64(bytes) {
  let s = ''; for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
function ic(name, cls) { return '<svg class="ic' + (cls ? ' ' + cls : '') + '" aria-hidden="true"><use href="#i-' + name + '"/></svg>'; }
function ago(t) {
  if (!t) return '';
  const d = Math.max(0, Date.now() / 1000 - t);
  if (d < 45) return '刚刚'; if (d < 3600) return Math.max(1, Math.round(d / 60)) + ' 分钟前';
  if (d < 86400) return Math.floor(d / 3600) + ' 小时前'; return Math.floor(d / 86400) + ' 天前';
}
function hhmm(t) { const d = new Date(t * 1000); return d.toTimeString().slice(0, 5); }
function fmtNum(n) {
  n = Number(n) || 0;
  if (n < 1000) return String(Math.round(n));
  if (n < 1e6) return (n / 1e3).toFixed(n < 1e4 ? 1 : 0).replace(/\.0$/, '') + 'k';
  if (n < 1e9) return (n / 1e6).toFixed(n < 1e7 ? 2 : 1).replace(/\.?0+$/, '') + 'M';
  return (n / 1e9).toFixed(1) + 'B';
}
function fmtBps(b) {
  b = Number(b) || 0;
  if (b < 1024) return Math.round(b) + ' B/s';
  if (b < 1048576) return (b / 1024).toFixed(b < 10240 ? 1 : 0) + ' KB/s';
  return (b / 1048576).toFixed(1) + ' MB/s';
}
function fmtMs(ms) { return ms == null ? '—' : (ms < 10 ? ms.toFixed(1) : Math.round(ms)) + ' ms'; }
function pushHist(arr, v, n) { arr.push(v); if (arr.length > (n || 30)) arr.shift(); }
function setHTML(el, html) { if (el._h !== html) { el._h = html; el.innerHTML = html; } }
function announce(t) { const l = $('#sr-live'); l.textContent = ''; setTimeout(() => { l.textContent = t; }, 30); }
const STATUS = { starting: '启动中', running: '运行中', waiting: '等待回复', idle: '空闲', exited: '已退出', error: '出错' };
const KIND = { claude: 'Claude', codex: 'Codex', shell: 'Shell' };
const KEYS = {
  esc: '\x1b', ctrlc: '\x03', tab: '\t', stab: '\x1b[Z', up: '\x1b[A', down: '\x1b[B', right: '\x1b[C', left: '\x1b[D',
  enter: '\r', 1: '1', 2: '2', 3: '3', y: 'y', n: 'n',
};

function spark(vals, cls) {
  const W = 140; const H = 30;
  if (!vals || vals.length < 2) return '<svg class="spark ' + (cls || '') + '" viewBox="0 0 ' + W + ' ' + H + '" aria-hidden="true"></svg>';
  const max = Math.max(...vals, 1e-9); const min = Math.min(...vals, 0);
  const span = max - min || 1; const step = W / (vals.length - 1);
  const pts = vals.map((v, i) => [i * step, H - 2 - ((v - min) / span) * (H - 5)]);
  const d = pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join('');
  return '<svg class="spark ' + (cls || '') + '" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" aria-hidden="true">' +
    '<path class="a" d="' + d + 'L' + W + ' ' + H + 'L0 ' + H + 'Z"/><path class="l" d="' + d + '" vector-effect="non-scaling-stroke"/></svg>';
}
function ring(pct, label) {
  const r = 16; const c = 2 * Math.PI * r; const p = Math.max(0, Math.min(100, pct));
  const cls = p >= 90 ? 'hot' : p >= 70 ? 'warn' : '';
  return '<svg class="ring ' + cls + '" viewBox="0 0 40 40" role="img" aria-label="' + esc(label) + '">' +
    '<circle class="bg" cx="20" cy="20" r="' + r + '"/>' +
    '<circle class="fg" cx="20" cy="20" r="' + r + '" stroke-dasharray="' + (c * p / 100).toFixed(1) + ' ' + c.toFixed(1) + '" transform="rotate(-90 20 20)"/>' +
    '<text x="20" y="23.5" text-anchor="middle">' + Math.round(p) + '%</text></svg>';
}
function meter(pct) {
  const p = Math.max(0, Math.min(100, pct || 0));
  return '<span class="meter ' + (p >= 90 ? 'hot' : p >= 70 ? 'warn' : '') + '" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' +
    Math.round(p) + '"><i style="width:' + p.toFixed(1) + '%"></i></span>';
}

// ------------------------------------------------------------------ widget registry ("插件")
// Each widget: {id, title, icon, scope:'home'|'session'|'both', render(ctx) -> html string | null}
// ctx = {scope, s (session, session scope only), mc (machine of s)}. Returning null hides the widget in that place.
function allSessions() {
  const out = [];
  for (const mc of S.machines) for (const s of mc.sessions) out.push([mc, s]);
  return out;
}
function tokTotal(u) { return u ? (u.input || 0) + (u.output || 0) + (u.cache_read || 0) + (u.cache_write || 0) : 0; }
// machine.usage_today = {day, claude:{input,output,cache_read,cache_write}, codex:{input,output,cache_read}, by_model}
function dayTotal(ut) { return ut ? tokTotal(ut.claude) + tokTotal(ut.codex) : 0; }
function dayIO(ut) { let n = 0; if (ut) for (const k of ['claude', 'codex']) if (ut[k]) n += (ut[k].input || 0) + (ut[k].output || 0); return n; }
// pickers show the CLIs' own English ids; these are the short English hints shown under / next to them
const MODE_DESC = { manual: 'ask before edits and commands', default: 'settings file default', acceptEdits: 'auto-accept file edits',
  auto: 'auto-approve safe actions', plan: 'read-only planning', dontAsk: 'deny anything not pre-approved',
  bypassPermissions: 'skip all permission prompts', bypass: 'skip all permission prompts' };
const EFFORT_DESC = { none: 'no reasoning', minimal: 'fastest', low: 'fast', medium: 'balanced', high: 'thorough', xhigh: 'extra thorough',
  max: 'maximum', ultra: 'deepest reasoning', ultracode: 'xhigh + dynamic workflows' };
const APPROVAL_DESC = { untrusted: 'ask unless the command is trusted', 'on-request': 'model decides when to ask', 'on-failure': 'ask when a command fails',
  never: 'never ask' };
const SANDBOX_DESC = { 'read-only': 'no writes', 'workspace-write': 'write inside the workspace', 'danger-full-access': 'no sandbox' };
const EFFORT_ORDER = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra', 'ultracode'];
function modeName(m) { return m === 'bypass' ? 'bypassPermissions' : m; }
// session "mode" dict from the agent; falls back to the model seen in the usage stats
function sessMode(s) { const m = Object.assign({}, (s && s.mode) || {}); if (!m.model && s && s.usage) m.model = s.usage.model; return m; }
function shortModel(m) { return String(m || '').replace(/^claude-/, '').replace(/-\d{8}$/, ''); }
function wHead(w) { return '<div class="w-h">' + ic(w.icon) + esc(w.title) + '</div>'; }

const WIDGETS = [
  {
    id: 'net', title: '网络', icon: 'wifi', scope: 'both',
    render(ctx) {
      const n = S.net; const rtt = n.rtt.length ? n.rtt[n.rtt.length - 1] : null;
      const rx = n.rx.length ? n.rx[n.rx.length - 1] : 0;
      if (ctx.scope === 'session') {
        const st = ctx.mc && ctx.mc.stats;
        return '<div class="w-col"><div class="w-sub">延迟 手机↔hub</div><div class="w-big">' + (rtt == null ? '—' : Math.round(rtt)) +
          '<small>ms</small></div><div class="w-sub">hub↔' + esc(ctx.mc ? ctx.mc.id : '') + ' ' + fmtMs(st && st.rtt_ms) + '</div></div>';
      }
      let rows = '';
      for (const mc of S.machines) {
        if (!mc.online || !mc.stats) continue;
        rows += '<div class="w-row"><b>' + esc(mc.id) + '</b><span class="grow">' + fmtMs(mc.stats.rtt_ms) + '</span><span>↓' +
          fmtBps(mc.stats.rx_bps) + '</span></div>';
      }
      return wHead(this) + '<div class="w-big">' + (rtt == null ? '—' : Math.round(rtt)) + '<small>ms</small></div>' +
        '<div class="w-sub">下行 ' + fmtBps(rx) + '</div>' + spark(n.rtt) + (rows ? '<div class="w-rows">' + rows + '</div>' : '');
    },
  },
  {
    id: 'tokens', title: 'Token 用量', icon: 'coins', scope: 'both',
    render(ctx) {
      if (ctx.scope === 'session') {
        const u = ctx.s && ctx.s.usage; if (!u) return null;
        return '<div class="w-col"><div class="w-sub">Token 输入 / 输出</div><div class="w-big">' + fmtNum(u.input) + '<small>/ ' + fmtNum(u.output) +
          '</small></div><div class="w-sub">缓存 读 ' + fmtNum(u.cache_read) + ' · 写 ' + fmtNum(u.cache_write) + '</div></div>';
      }
      let tot = 0; let io = 0; let n = 0;
      for (const [, s] of allSessions()) if (s.usage) { tot += tokTotal(s.usage); io += (s.usage.input || 0) + (s.usage.output || 0); n++; }
      const days = S.machines.filter((mc) => mc.usage_today);
      if (!days.length) {
        return wHead(this) + '<div class="w-big">' + fmtNum(tot) + '</div><div class="w-sub">含缓存 · ' + n + ' 个会话</div>' +
          '<div class="w-rows"><div class="w-row"><span class="grow">输入+输出</span><b>' + fmtNum(io) + '</b></div>' +
          '<div class="w-row"><span class="grow">缓存</span><b>' + fmtNum(tot - io) + '</b></div></div>';
      }
      // today, all conversations on every machine (not only Glimpse ones), from the agents' transcript scan
      let day = 0; let dio = 0;
      for (const mc of days) { day += dayTotal(mc.usage_today); dio += dayIO(mc.usage_today); }
      const rows = days.slice(0, 3).map((mc) => '<div class="w-row"><b style="min-width:22px">' + esc(mc.id) + '</b><span class="grow">' +
        esc(mc.name) + '</span><b>' + fmtNum(dayTotal(mc.usage_today)) + '</b></div>').join('');
      return wHead(this) + '<div class="w-big">' + fmtNum(day) + '<small>今日</small></div><div class="w-sub">含缓存 · 输入+输出 ' + fmtNum(dio) + '</div>' +
        '<div class="w-rows">' + rows + '<div class="w-row w-sep"><span class="grow">当前 ' + n + ' 个会话</span><b>' + fmtNum(tot) + '</b></div></div>';
    },
  },
  {
    id: 'context', title: '上下文', icon: 'gauge', scope: 'both',
    render(ctx) {
      if (ctx.scope === 'session') {
        const u = ctx.s && ctx.s.usage; if (!u || !u.context_window) return null;
        const pct = 100 * u.context_used / u.context_window;
        let rate = '';
        if (u.rate && (u.rate.primary != null || u.rate.secondary != null)) {
          rate = '<div class="w-sub">限额 ' + (u.rate.primary != null ? Math.round(u.rate.primary) + '%' : '—') + ' · 周 ' +
            (u.rate.secondary != null ? Math.round(u.rate.secondary) + '%' : '—') + '</div>';
        }
        return ring(pct, '上下文已用 ' + Math.round(pct) + '%') + '<div class="w-col"><div class="w-sub">上下文</div><div class="w-big" style="font-size:15px">' +
          fmtNum(u.context_used) + '<small>/ ' + fmtNum(u.context_window) + '</small></div><div class="w-sub">' + esc(u.model || '') + '</div>' + rate + '</div>';
      }
      // home: the fullest context among live sessions
      let rows = ''; let k = 0;
      const live = allSessions().filter(([, s]) => s.usage && s.usage.context_window && s.status !== 'exited')
        .map(([mc, s]) => [mc, s, 100 * s.usage.context_used / s.usage.context_window]).sort((a, b) => b[2] - a[2]);
      for (const [, s, p] of live) {
        if (k++ >= 3) break;
        rows += '<div class="w-row"><span class="grow">' + esc(s.name) + '</span>' + meter(p) + '<b style="min-width:30px;text-align:right">' + Math.round(p) + '%</b></div>';
      }
      if (!rows) return wHead(this) + '<div class="w-sub">暂无数据</div>';
      return wHead(this).replace('w-h"', 'w-h" style="margin-bottom:2px"') + '<div class="w-rows" style="gap:9px">' + rows + '</div>';
    },
  },
  {
    id: 'machines', title: '机器负载', icon: 'cpu', scope: 'both',
    render(ctx) {
      const one = (mc) => {
        const st = mc.stats;
        if (!mc.online || !st) return '<div class="w-row"><b>' + esc(mc.id) + '</b><span class="grow" style="color:var(--c-err)">离线</span></div>';
        const cpu = st.ncpu ? 100 * st.load[0] / st.ncpu : 0;
        return '<div class="w-row"><b style="min-width:22px">' + esc(mc.id) + '</b><span style="min-width:24px">CPU</span>' + meter(cpu) +
          '<span style="min-width:26px">内存</span>' + meter(st.mem_pct) + '</div>';
      };
      if (ctx.scope === 'session') {
        const mc = ctx.mc; if (!mc || !mc.stats) return null;
        const cpu = mc.stats.ncpu ? 100 * mc.stats.load[0] / mc.stats.ncpu : 0;
        return '<div class="w-col"><div class="w-sub">' + esc(mc.id) + ' 负载</div><div class="w-big">' + Math.round(cpu) +
          '<small>% CPU</small></div><div class="w-sub">内存 ' + Math.round(mc.stats.mem_pct) + '% · ' + mc.stats.ncpu + ' 核</div></div>';
      }
      return wHead(this) + '<div class="w-rows" style="gap:9px">' + S.machines.map(one).join('') + '</div>';
    },
  },
  {
    id: 'controls', title: '模型与模式', icon: 'sliders', scope: 'session',
    render(ctx) {
      const s = ctx.s; if (!s || (s.kind !== 'claude' && s.kind !== 'codex')) return null;
      const md = sessMode(s);
      const top = s.kind === 'claude' ? modeName(md.mode || 'default') : (md.model ? shortModel(md.model) : 'default model');
      const sub = s.kind === 'claude' ? [md.model ? shortModel(md.model) : 'default model', md.effort ? 'effort ' + md.effort : ''] :
        [md.effort ? 'effort ' + md.effort : '', md.sandbox || ''];
      return '<button type="button" class="w-tap" data-act="ctl" aria-label="模型与模式：' + esc(top) + '，点按修改"><span class="w-col"><span class="w-sub">' +
        (s.kind === 'claude' ? '模式' : '模型') + ' ' + ic('chev', 'xs') + '</span><span class="w-big" style="font-size:15px">' + esc(top) + '</span><span class="w-sub">' +
        esc(sub.filter(Boolean).join(' · ') || '点按修改') + '</span></span></button>';
    },
  },
  {
    id: 'activity', title: '活跃会话', icon: 'pulse', scope: 'home',
    render() {
      const c = { running: 0, waiting: 0, idle: 0, error: 0 };
      for (const [, s] of allSessions()) {
        const k = s.status === 'starting' ? 'running' : s.status;
        if (k in c) c[k]++;
      }
      const cell = (k, l) => '<div><span class="dot ' + k + '" style="animation:none"></span><b>' + c[k] + '</b>' + l + '</div>';
      return wHead(this) + '<div class="act-grid">' + cell('running', '运行') + cell('waiting', '等待') + cell('idle', '空闲') + cell('error', '出错') + '</div>';
    },
  },
];
const WIDGET_BY_ID = Object.fromEntries(WIDGETS.map((w) => [w.id, w]));
const DEFAULT_WIDGETS = ['controls', 'net', 'tokens', 'context', 'machines'];
function widgetPrefs() {
  // {order:[id...], on:{id:bool}}
  let p = null; try { p = JSON.parse(LS.getItem('glimpse.widgets') || 'null'); } catch (_) { p = null; }
  if (!p || !Array.isArray(p.order)) p = { order: ['controls'].concat(WIDGETS.map((w) => w.id).filter((id) => id !== 'controls')), on: Object.fromEntries(WIDGETS.map((w) => [w.id, DEFAULT_WIDGETS.includes(w.id)])) };
  // widgets added in a later version: default-on ones go first (e.g. "controls"), the rest at the end
  for (const w of WIDGETS) if (!p.order.includes(w.id)) { const on = DEFAULT_WIDGETS.includes(w.id); if (on) p.order.unshift(w.id); else p.order.push(w.id); p.on[w.id] = on; }
  p.order = p.order.filter((id) => WIDGET_BY_ID[id]);
  return p;
}
function saveWidgetPrefs(p) { LS.setItem('glimpse.widgets', JSON.stringify(p)); }
function enabledWidgets(scope) {
  const p = widgetPrefs();
  return p.order.filter((id) => p.on[id]).map((id) => WIDGET_BY_ID[id]).filter((w) => w.scope === 'both' || w.scope === scope);
}
// keyed render into a container: one element per widget, innerHTML only touched when the widget's html changes
function renderWidgets(host, scope, ctx) {
  const want = [];
  for (const w of enabledWidgets(scope)) {
    let html = null; try { html = w.render(Object.assign({ scope }, ctx)); } catch (e) { html = null; }
    if (html != null) want.push([w, html]);
  }
  const have = new Map($$(':scope > .widget', host).map((el) => [el.dataset.w, el]));
  let prev = null;
  for (const [w, html] of want) {
    let el = have.get(w.id);
    if (!el) {
      el = document.createElement('section'); el.className = 'widget' + (scope === 'home' && (w.id === 'machines' || w.id === 'context') ? ' wide' : '');
      el.dataset.w = w.id; el.setAttribute('aria-label', w.title);
    }
    have.delete(w.id);
    setHTML(el, html);
    const next = prev ? prev.nextSibling : host.firstChild;
    if (next !== el) host.insertBefore(el, next);
    prev = el;
  }
  for (const el of have.values()) el.remove();
  return want.length;
}

// ------------------------------------------------------------------ toast / in-app banner
function toast(title, body, onTap, kind) {
  const host = $('#toast-host');
  const old = host.firstChild; if (old) old.remove();
  const t = document.createElement('button');
  t.type = 'button'; t.className = 'toast ' + (kind || '');
  const icn = { done: 'check', question: 'help', error: 'alert', exit: 'power' }[kind] || 'bell';
  t.innerHTML = '<span class="t-ic">' + ic(icn) + '</span><span class="grow"><b>' + esc(title) + '</b>' +
    (body ? '<span>' + esc(String(body).slice(0, 240)) + '</span>' : '') + '</span>';
  t.setAttribute('aria-label', title + (body ? '：' + body : ''));
  const hide = () => { if (!t.isConnected) return; t.classList.add('out'); setTimeout(() => t.remove(), reduceMotion() ? 0 : 200); };
  t.onclick = () => { hide(); if (onTap) onTap(); };
  // swipe up to dismiss
  let y0 = null;
  t.addEventListener('touchstart', (e) => { y0 = e.touches[0].clientY; }, { passive: true });
  t.addEventListener('touchmove', (e) => { if (y0 != null && e.touches[0].clientY - y0 < -12) { y0 = null; hide(); } }, { passive: true });
  host.appendChild(t);
  setTimeout(hide, 6000);
}

// ------------------------------------------------------------------ sheets (<dialog class="sheet">)
function openSheet(d) {
  if (d.open) return;
  d.classList.remove('closing');
  d.showModal();
  // focus the sheet itself (announced by its label) instead of auto-focusing the first header button
  if (!d.hasAttribute('tabindex')) d.tabIndex = -1;
  if (!d.contains(document.activeElement) || document.activeElement.closest('.sh-head')) d.focus({ preventScroll: true });
  d.querySelector('.sh-body').scrollTop = 0;
}
function closeSheet(d) {
  if (!d.open || d.classList.contains('closing')) return;
  if (reduceMotion()) { d.close(); return; }
  d.classList.add('closing');
  setTimeout(() => { d.classList.remove('closing'); d.style.transform = ''; d.close(); }, 210);
}
for (const d of $$('dialog.sheet')) {
  d.addEventListener('cancel', (e) => { e.preventDefault(); closeSheet(d); });       // Esc key
  d.addEventListener('click', (e) => { if (e.target === d) closeSheet(d); });         // backdrop tap
  for (const b of $$('[data-close]', d)) b.addEventListener('click', () => closeSheet(d));
  // drag the handle / header down to dismiss
  let y0 = null; let dy = 0;
  const start = (e) => { if (e.target.closest('button')) return; y0 = e.touches[0].clientY; dy = 0; d.classList.add('dragging'); };
  const move = (e) => { if (y0 == null) return; dy = Math.max(0, e.touches[0].clientY - y0); d.style.transform = 'translateY(' + dy + 'px)'; };
  const end = () => {
    if (y0 == null) return; y0 = null; d.classList.remove('dragging');
    if (dy > 90) closeSheet(d);
    else { d.style.transition = 'transform .2s'; d.style.transform = ''; setTimeout(() => { d.style.transition = ''; }, 220); }
  };
  for (const h of $$('.sh-grab, .sh-head', d)) {
    h.addEventListener('touchstart', start, { passive: true });
    h.addEventListener('touchmove', move, { passive: true });
    h.addEventListener('touchend', end); h.addEventListener('touchcancel', end);
  }
}
function confirmSheet(title, msg, okText) {
  return new Promise((resolve) => {
    const d = $('#dlg-confirm');
    $('#cf-title').textContent = title; $('#cf-msg').textContent = msg; $('#cf-ok').textContent = okText;
    let done = false;
    const finish = (v) => { if (done) return; done = true; resolve(v); };
    $('#cf-ok').onclick = () => { finish(true); closeSheet(d); };
    d.addEventListener('close', () => finish(false), { once: true });
    openSheet(d);
  });
}

// ------------------------------------------------------------------ screens
let curScreen = null;
function show(id, dir) {
  for (const s of $$('.screen')) s.hidden = s.id !== id;
  const el = $('#' + id);
  if (curScreen && curScreen !== id && dir && !reduceMotion()) {
    el.classList.remove('enter', 'enter-back'); void el.offsetWidth;
    el.classList.add(dir === 'back' ? 'enter-back' : 'enter');
  }
  curScreen = id;
}

// ------------------------------------------------------------------ auth / boot
async function api(path, body) {
  const opt = { method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', headers: {} };
  const tok = LS.getItem('glimpse.token');
  if (tok) opt.headers.Authorization = 'Bearer ' + tok;
  if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  if (!r.ok) { const e = new Error((await r.text()) || r.statusText); e.status = r.status; throw e; }
  return r.json();
}
// session token of this device (also an HttpOnly cookie; the copy here survives iOS Home Screen cookie loss)
async function loginWith(body) {
  const r = await api('/api/login', body);
  if (r && r.session) LS.setItem('glimpse.token', r.session);
  return r;
}

let booted = false;
async function boot() {
  const u = new URL(location.href);
  const pair = u.searchParams.get('pair');
  if (u.searchParams.has('t')) u.searchParams.delete('t');             // links from older versions: ignored
  if (pair) {
    try { await loginWith({ pair }); } catch (e) { S.pairErr = e.status === 429 ? '尝试次数过多，请稍后再试' : '登录链接无效或已过期，请在电脑上重新运行 gli url'; }
    u.searchParams.delete('pair');
  }
  if (pair || location.search) history.replaceState(null, '', u.pathname + u.hash);
  // only a definite 401 shows the login screen; anything else (network) shows "无法连接"
  let status = 0;
  try { const me = await api('/api/me'); S.me = me; status = 200; } catch (e) { status = e.status || 0; }
  if (status === 401) {
    LS.removeItem('glimpse.token');
    show('login'); $('#login-err').textContent = S.pairErr || ''; S.pairErr = '';
    setTimeout(() => $('#login-pw').focus(), 50); return;
  }
  if (status !== 200) { show('offline'); $('#off-retry').disabled = false; return; }
  if (booted) return;
  booted = true;
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {});
    navigator.serviceWorker.addEventListener('message', (ev) => {
      const d = ev.data || {};
      if (d.type === 'open' && d.url) routeTo(d.url.split('#')[1] || '');
    });
  }
  show('home');
  connect();
  window.addEventListener('popstate', () => routeTo(location.hash.slice(1)));
  routeTo(location.hash.slice(1));
}
$('#off-retry').onclick = () => { $('#off-retry').disabled = true; setTimeout(boot, 250); };

$('#login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const v = $('#login-pw').value;
  if (!v) { $('#login-err').textContent = '请输入密码'; return; }
  const b = $('#login-btn'); b.disabled = true; b.innerHTML = '<span class="spinner" aria-hidden="true"></span>登录中…';
  try { await loginWith({ password: v }); $('#login-pw').value = ''; $('#login-err').textContent = ''; boot(); } catch (err) {
    $('#login-err').textContent = err.status === 401 ? '密码不正确' : err.status === 429 ? '尝试次数过多，请稍后再试' : '无法连接 hub';
  } finally { b.disabled = false; b.textContent = '登录'; }
});

// ------------------------------------------------------------------ websocket
function send(o) { if (S.ws && S.ws.readyState === 1) { S.ws.send(JSON.stringify(o)); return true; } return false; }
// after iOS resumes a suspended page the socket can look open but be dead: "confirmed" = a pong within the last 10 s
function wsConfirmed() { return !!(S.ws && S.ws.readyState === 1 && S.wsOk && performance.now() - S.lastPong < 10000); }
function flushOutbox() {
  if (!S.outbox.length || !wsConfirmed()) return;
  const q = S.outbox; S.outbox = [];
  for (const o of q) send(o);
  renderOutbox();
}
function setConn(state, text) {
  const c = $('#conn');
  c.className = 'conn ' + state;
  c.querySelector('.ct').textContent = text;
}
function connect() {
  clearTimeout(S.reconnectTimer); S.reconnectTimer = null;
  clearTimeout(S.pongTimer); S.pongTimer = null;
  const old = S.ws; S.ws = null;
  if (old) { try { old.onclose = null; old.close(); } catch (_) {} }
  const tok = LS.getItem('glimpse.token');
  const url = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws' + (tok ? '?t=' + encodeURIComponent(tok) : '');
  let ws;
  try { ws = new WebSocket(url); } catch (_) { scheduleReconnect(); return; }
  S.ws = ws;
  setConn('wait', S.haveState ? '重连中…' : '连接中…');
  // a TCP connect that never completes (Tailscale down, hub asleep) would otherwise hang for minutes
  const openTimer = setTimeout(() => { if (S.ws === ws && ws.readyState === 0) { try { ws.close(); } catch (_) {} if (S.ws === ws) { S.ws = null; setConn('bad', '连接超时'); scheduleReconnect(); } } }, 8000);
  ws.onopen = () => {
    clearTimeout(openTimer);
    if (S.ws !== ws) return;
    S.wsOk = true; S.backoff = 500; S.lastPong = performance.now(); setConn('ok', '已连接');
    flushOutbox();
    send({ op: 'visible', v: document.visibilityState === 'visible' });
    ping();
    if (S.view) subscribe();
  };
  ws.onmessage = (ev) => {
    if (S.ws !== ws) return;
    S.net.rxBytes += ev.data.length || 0;
    let m; try { m = JSON.parse(ev.data); } catch (_) { return; }
    onMsg(m);
  };
  ws.onclose = (ev) => {
    clearTimeout(openTimer);
    if (S.ws !== ws) return;
    S.wsOk = false; S.ws = null;
    if (ev && ev.code === 4401) {          // this device was logged out (here or from another device / the CLI)
      LS.removeItem('glimpse.token'); booted = false;
      show('login'); $('#login-err').textContent = '这台设备已退出登录，请重新登录';
      return;
    }
    setConn('bad', '已断开');
    scheduleReconnect();
  };
  ws.onerror = () => { try { ws.close(); } catch (_) {} };
}
function scheduleReconnect() {
  if (S.reconnectTimer) return;
  S.reconnectTimer = setTimeout(() => { S.reconnectTimer = null; connect(); }, S.backoff);
  S.backoff = Math.min(S.backoff * 2, 8000);
}
function ping() {
  if (!S.ws || S.ws.readyState !== 1) return;
  S.lastPing = performance.now();
  send({ op: 'ping', ts: S.lastPing });
  if (!S.pongTimer) S.pongTimer = setTimeout(() => { S.pongTimer = null; setConn('bad', '无响应'); S.backoff = 500; connect(); }, 10000);
}
// iOS suspends pages in the background: reconnect immediately when we come back
document.addEventListener('visibilitychange', () => {
  const vis = document.visibilityState === 'visible';
  if (vis && booted) {
    // a socket that has not answered for >10 s across a suspend is almost certainly dead: don't wait for the ping timeout
    if (!S.ws || S.ws.readyState > 1 || (S.ws.readyState === 1 && performance.now() - S.lastPong > 10000)) { S.backoff = 500; connect(); } else ping();
    if (S.view) send({ op: 'ack', m: S.view.m, sid: S.view.sid });
  }
  send({ op: 'visible', v: vis });
});
setInterval(() => { if (document.visibilityState === 'visible') ping(); }, 5000);
// websocket download rate, sampled every second
setInterval(() => {
  const now = performance.now(); const n = S.net;
  pushHist(n.rx, n.rxBytes * 1000 / Math.max(1, now - n.rxT)); n.rxBytes = 0; n.rxT = now;
}, 1000);

function isView(m) { return S.view && m.m === S.view.m && m.sid === S.view.sid; }
function onMsg(m) {
  switch (m.t) {
    case 'state':
      S.machines = m.machines || []; S.byId = {};
      for (const mc of S.machines) {
        S.byId[mc.id] = mc;
        if (mc.stats) {
          const h = S.mhist[mc.id] || (S.mhist[mc.id] = { rtt: [], rx: [], ts: 0 });
          if (mc.stats.ts !== h.ts) { h.ts = mc.stats.ts; pushHist(h.rtt, mc.stats.rtt_ms || 0); pushHist(h.rx, mc.stats.rx_bps || 0); }
        }
      }
      S.keys = m.keys || []; S.balance = m.balance || null; S.hub = m.hub || null; S.options = m.options || null; S.haveState = true;
      renderHome();
      if (S.view) renderView();
      refreshOpenSheets();
      break;
    case 'pong':
      clearTimeout(S.pongTimer); S.pongTimer = null; S.lastPong = performance.now();
      flushOutbox();
      if (typeof m.ts === 'number') pushHist(S.net.rtt, Math.max(0, performance.now() - m.ts));
      else if (S.lastPing) pushHist(S.net.rtt, performance.now() - S.lastPing);
      if (S.wsOk) setConn('ok', '已连接');
      renderHomeWidgets(); if (S.view) renderViewWidgets();
      break;
    case 'out': if (isView(m)) onOut(m); break;
    case 'replay': if (isView(m)) onReplay(m); break;
    case 'chat': if (isView(m)) addChat(m.items || [], false); break;
    case 'resync': if (S.view && S.view.m === m.m) subscribe(); break;
    case 'created': {
      newBusy(false); closeSheet($('#dlg-new')); closeSheet($('#dlg-adopt'));
      // the state broadcast may arrive after this reply: insert the session now so the view has cols/rows/kind
      const mc = S.byId[m.m];
      if (mc && m.s && !mc.sessions.some((x) => x.sid === m.s.sid)) { mc.sessions.push(m.s); renderHome(); }
      if (m.s) openSession(m.m, m.s.sid);
      break;
    }
    case 'restarted': if (RS.active && RS.m === m.m && RS.sid === m.sid) rsDone(m); break;
    case 'key_balance_ok': toast('已保存', '', null, 'done'); break;
    case 'approved': if (isView(m)) { S.view.approving = null; S.view.cardSig = null; renderCard(); } break;
    case 'history': onHistory(m); break;
    case 'models': onModels(m, false); break;
    case 'memory': onMemory(m); break;
    case 'menu_ok': if (isView(m)) menuDone(); break;
    case 'control_ok': ctlDone(m); break;
    case 'delete_ok': onDeleted(m); break;
    case 'machine_test': mcTested(m); break;
    case 'machines_ok': mcSaved(); break;
    case 'event': {
      const here = isView(m) && document.visibilityState === 'visible';
      if (!here) toast(m.title || (m.ev && m.ev.title) || 'Glimpse', m.ev && m.ev.body, () => openSession(m.m, m.sid), m.ev && m.ev.kind === 'keyswap' ? 'done' : m.ev && m.ev.kind);
      break;
    }
    case 'error':
      if (m.op === 'models' && m.kind) { onModels(m, true); break; }
      if ($('#dlg-new').open && !m.sid) { newBusy(false); $('#new-err').textContent = m.msg; }
      else if (ADOPT.busy && $('#dlg-adopt').open && !m.sid) { adoptFail(m.msg); }
      else if (MC.busy && $('#dlg-machine').open) { mcFail(m.msg); }
      else if (RS.active && $('#dlg-restart').open) { rsFail(m.msg); }
      else if (CTL.busy && $('#dlg-ctl').open) { ctlFail(m.msg); }
      else if ($('#dlg-mem').open && !m.sid) { MEM.busy = false; toast('记忆', m.msg, null, 'error'); }
      else toast('出错了', m.msg, null, 'error');
      if (S.view) { S.view.approving = null; renderCard(); }
      break;
    default: break;
  }
}
function session(mid, sid) {
  const mc = S.byId[mid]; if (!mc) return null;
  return mc.sessions.find((s) => s.sid === sid) || null;
}

// ------------------------------------------------------------------ home (keyed, incremental)
function visibleSessions(mc) { return mc.sessions.filter((s) => !(S.hideExited && s.status === 'exited')); }
function statusLine(s) {
  if (s.status === 'waiting') return s.detail || '等待你回复';
  if (s.detail && s.status !== 'idle') return s.detail;
  return STATUS[s.status] || s.status;
}
function machineStats(mc) {
  if (!mc.online) return mc.error || '离线';
  const st = mc.stats; const n = mc.sessions.length;
  const parts = [(mc.host || mc.type || '')];
  parts.push(n + ' 个会话');
  if (st) { parts.push('CPU ' + Math.round(100 * st.load[0] / (st.ncpu || 1)) + '%'); parts.push('内存 ' + Math.round(st.mem_pct) + '%'); parts.push(fmtMs(st.rtt_ms)); }
  return parts.filter(Boolean).join(' · ');
}
function cardHTML(mc, s) {
  let flag = '';
  if (s.pending) flag = '<div class="c-flag question">' + ic('shield', 'sm') + '<span class="grow">待授权：' + esc(s.pending.tool) + ' · ' + esc(s.pending.summary || '') + '</span></div>';
  else if (s.question) flag = '<div class="c-flag question">' + ic('help', 'sm') + '<span class="grow">提问：' + esc(s.question.question) + '</span></div>';
  else if (s.attention === 'error') flag = '<div class="c-flag error">' + ic('alert', 'sm') + '<span class="grow">' + esc(s.detail || '出错') + '</span></div>';
  else if (s.attention === 'done') flag = '<div class="c-flag done">' + ic('check-c', 'sm') + '<span class="grow">已完成</span></div>';
  else if (s.attention === 'question') flag = '<div class="c-flag question">' + ic('help', 'sm') + '<span class="grow">需要你回复</span></div>';
  return '<div class="c-r1"><span class="dot ' + esc(s.status) + '" aria-hidden="true"></span><span class="c-name">' + esc(s.name) + '</span>' +
    '<span class="c-time">' + esc(ago(s.activity)) + '</span></div>' +
    '<div class="c-r2"><span class="pill ' + esc(s.status) + '">' + esc(STATUS[s.status] || s.status) + '</span>' +
    '<span class="tag ' + esc(s.kind) + '">' + esc(KIND[s.kind] || s.kind) + '</span><span class="tag">' + esc(mc.id) + '</span>' +
    (s.status !== 'waiting' && s.detail && s.status !== 'idle' && !s.pending ? '<span class="c-detail">' + esc(s.detail) + '</span>' : '') + '</div>' +
    '<div class="c-cwd">' + ic('folder', 'xs') + '<span>' + esc(s.cwd) + '</span></div>' +
    (s.preview ? '<div class="c-pv">' + esc(s.preview) + '</div>' : '') + flag;
}
function keyedList(host, items, key, make, update) {
  // items: array; key(item) -> string; make(item) -> element; update(el, item)
  const have = new Map(); for (const el of Array.from(host.children)) if (el.dataset.key) have.set(el.dataset.key, el); else el.remove();
  let prev = null;
  for (const it of items) {
    const k = key(it);
    let el = have.get(k);
    if (!el) { el = make(it); el.dataset.key = k; }
    have.delete(k);
    update(el, it);
    const next = prev ? prev.nextSibling : host.firstChild;
    if (next !== el) host.insertBefore(el, next);
    prev = el;
  }
  for (const el of have.values()) el.remove();
}
function renderHome() {
  if (!S.haveState) return;
  renderHomeWidgets();
  // chips
  const chips = $('#chips');
  const items = allSessions().filter(([, s]) => !(S.hideExited && s.status === 'exited'));
  const sl = chips.scrollLeft;
  keyedList(chips, items, ([mc, s]) => mc.id + '/' + s.sid, ([mc, s]) => {
    const b = document.createElement('button'); b.type = 'button'; b.className = 'chip';
    b.onclick = () => openSession(mc.id, s.sid); return b;
  }, (b, [mc, s]) => {
    b.className = 'chip' + (s.attention === 'error' ? ' attn-error' : s.attention === 'question' || s.pending || s.question ? ' attn' : '');
    b.setAttribute('aria-label', mc.id + ' ' + s.name + '，' + (STATUS[s.status] || s.status));
    setHTML(b, '<span class="dot ' + esc(s.status) + '" aria-hidden="true"></span><span class="t"><span class="m">' + esc(mc.id) + '</span> · ' + esc(s.name) + '</span>');
  });
  chips.scrollLeft = sl;
  chips.hidden = items.length === 0;
  // groups
  const groups = $('#groups'); groups.removeAttribute('aria-busy');
  if (!S.machines.length) {
    setHTML(groups, '<div class="empty"><div class="art">' + ic('server', 'lg') + '</div><h3>还没有机器</h3><p>在 hub 的配置文件里添加机器后会显示在这里。</p></div>');
    return;
  }
  if (S.groupBy === 'status') renderGroupsByStatus(groups); else renderGroupsByMachine(groups);
  const waiting = allSessions().filter(([, s]) => s.attention || s.pending || s.question).length;
  document.title = waiting ? '(' + waiting + ') Glimpse' : 'Glimpse';
  if (navigator.setAppBadge) { try { (waiting ? navigator.setAppBadge(waiting) : navigator.clearAppBadge()).catch(() => {}); } catch (_) {} }
}
// home lists are ordered by session name (natural order: "任务2" before "任务10"), stable across restarts
const NAME_COLL = new Intl.Collator('zh-Hans-CN', { numeric: true, sensitivity: 'base' });
function bySessionName(a, b) { return NAME_COLL.compare(a.name || '', b.name || '') || (a.sid < b.sid ? -1 : a.sid > b.sid ? 1 : 0); }
function renderGroupsByMachine(groups) {
  if (groups._st) { groups._st = false; groups._h = null; groups.innerHTML = ''; }
  if (groups._h) { groups._h = null; groups.innerHTML = ''; }
  keyedList(groups, S.machines, (mc) => 'm:' + mc.id, (mc) => {
    const g = document.createElement('section'); g.className = 'mgroup';
    g.innerHTML = '<header class="mhead"><span class="mbadge"><span class="mid"></span><span class="dot"></span></span>' +
      '<div class="mtitle"><h2 class="mname"></h2><div class="mstats"></div></div></header><div class="cards"></div>';
    g.setAttribute('aria-label', '机器 ' + mc.id);
    return g;
  }, (g, mc) => {
    $('.mid', g).textContent = mc.id;
    const dot = $('.mbadge .dot', g); dot.className = 'dot ' + (mc.online ? 'on' : 'off'); dot.setAttribute('aria-label', mc.online ? '在线' : '离线'); dot.setAttribute('role', 'img');
    $('.mname', g).textContent = mc.name;
    const st = $('.mstats', g); st.textContent = machineStats(mc); st.classList.toggle('err', !mc.online);
    let rc = $('.mhead .btn', g);
    if (!mc.online && !rc) {
      rc = document.createElement('button'); rc.type = 'button'; rc.className = 'btn sm'; rc.textContent = '重连';
      rc.setAttribute('aria-label', '重连 ' + mc.id); rc.onclick = () => send({ op: 'reconnect', m: mc.id }); $('.mhead', g).appendChild(rc);
    } else if (mc.online && rc) rc.remove();
    const cards = $('.cards', g);
    const ss = visibleSessions(mc).slice().sort(bySessionName);
    if (!ss.length) {
      setHTML(cards, mc.online ? '<div class="empty-m"><span>这台机器上还没有会话</span><button type="button" class="btn sm primary" data-new="' + esc(mc.id) + '">' + ic('plus', 'xs') + '新建</button></div>'
        : '<div class="empty-m"><span>机器离线，重连后可查看会话</span></div>');
      const nb = $('[data-new]', cards); if (nb) nb.onclick = () => openNew(mc.id);
      return;
    }
    if (cards._h) { cards._h = null; cards.innerHTML = ''; }
    renderCards(cards, ss.map((s) => [mc, s]));
  });
}
function renderGroupBy() {
  for (const b of $$('.groupby [data-gb]')) b.setAttribute('aria-checked', String(b.dataset.gb === S.groupBy));
}
for (const b of $$('.groupby [data-gb]')) b.onclick = () => {
  S.groupBy = b.dataset.gb; LS.setItem('glimpse.groupBy', S.groupBy); renderGroupBy(); renderHome();
};
renderGroupBy();
// one card per [machine, session]; keyed so cards keep their DOM (and swipe state) across updates
function renderCards(cards, items) {
  keyedList(cards, items, ([mc, s]) => mc.id + '/' + s.sid, ([mc, s]) => {
    // swipe row: the delete action sits behind the card and is revealed by swiping the card left
    const row = document.createElement('div'); row.className = 'swipe';
    row.innerHTML = '<button type="button" class="sw-del" tabindex="-1">' + ic('trash') + '<span>删除</span></button><button type="button" class="card"></button>';
    const b = row.lastChild;
    b.onclick = () => { if (swipeConsumed(row)) return; openSession(mc.id, s.sid); };
    row.firstChild.onclick = () => { swipeClose(row); deleteSession(mc.id, s.sid); };
    swipeable(row, b);
    return row;
  }, (row, [mc, s]) => {
    const b = row.lastChild;
    b.className = 'card ' + (s.attention ? 'attn-' + s.attention : '') + (s.pending || s.question ? ' attn-question' : '') + (s.status === 'exited' ? ' exited' : '');
    b.setAttribute('aria-label', mc.id + ' ' + s.name + '，' + (KIND[s.kind] || s.kind) + '，' + statusLine(s) + '，' + ago(s.activity));
    row.firstChild.setAttribute('aria-label', '删除 ' + s.name);
    setHTML(b, cardHTML(mc, s));
  });
}
// status groups, most urgent first; a session needing an answer counts as "waiting" whatever its status
const STATUS_GROUPS = [
  { id: 'attn', title: '需要处理', icon: 'alert', test: (s) => !!(s.pending || s.question || s.attention === 'question') },
  { id: 'error', title: '出错', icon: 'x-c', test: (s) => s.status === 'error' || s.attention === 'error' },
  { id: 'running', title: '进行中', icon: 'pulse', test: (s) => s.status === 'running' || s.status === 'starting' },
  { id: 'waiting', title: '等待回复', icon: 'help', test: (s) => s.status === 'waiting' },
  { id: 'idle', title: '空闲', icon: 'check-c', test: (s) => s.status === 'idle' },
  { id: 'exited', title: '已结束', icon: 'power', test: () => true },
];
function renderGroupsByStatus(groups) {
  if (!groups._st) { groups._st = true; groups._h = null; groups.innerHTML = ''; }
  const buckets = STATUS_GROUPS.map((g) => ({ g, items: [] }));
  for (const mc of S.machines) {
    for (const s of visibleSessions(mc)) buckets.find((b) => b.g.test(s)).items.push([mc, s]);
  }
  for (const b of buckets) b.items.sort((x, y) => bySessionName(x[1], y[1]) || x[0].id.localeCompare(y[0].id));
  const shown = buckets.filter((b) => b.items.length);
  if (!shown.length) {
    setHTML(groups, '<div class="empty"><div class="art">' + ic('inbox', 'lg') + '</div><h3>还没有会话</h3><p>点右下角 ＋ 新建一个会话。</p></div>');
    groups._st = false;
    return;
  }
  if (groups._h) { groups._h = null; groups.innerHTML = ''; }
  keyedList(groups, shown, (b) => 'st:' + b.g.id, (b) => {
    const g = document.createElement('section'); g.className = 'mgroup sgroup sg-' + b.g.id;
    g.innerHTML = '<header class="shead">' + ic(b.g.icon, 'sm') + '<h2></h2><span class="scount"></span></header><div class="cards"></div>';
    return g;
  }, (g, b) => {
    $('h2', g).textContent = b.g.title;
    $('.scount', g).textContent = b.items.length;
    g.setAttribute('aria-label', b.g.title + ' ' + b.items.length + ' 个会话');
    renderCards($('.cards', g), b.items);
  });
}
// ---- swipe-left to reveal 删除 on a session card (touch only; keyboard / screen readers use 会话操作 → 删除会话)
const SW_W = 88;
let swOpen = null;
function swipeClose(row) {
  if (!row) return;
  const b = row.lastChild; b.style.transition = ''; b.style.transform = '';
  row.classList.remove('open'); row.firstChild.tabIndex = -1;
  if (swOpen === row) swOpen = null;
}
function swipeConsumed(row) {
  // a tap on an open card (or right after a drag) only closes it
  if (row._dragged) { row._dragged = false; return true; }
  if (row.classList.contains('open')) { swipeClose(row); return true; }
  if (swOpen) { swipeClose(swOpen); return true; }
  return false;
}
function swipeable(row, card) {
  let x0 = null; let y0 = 0; let dx = 0; let horiz = null; let base = 0;
  card.addEventListener('touchstart', (e) => {
    if (swOpen && swOpen !== row) swipeClose(swOpen);
    x0 = e.touches[0].clientX; y0 = e.touches[0].clientY; dx = 0; horiz = null; base = row.classList.contains('open') ? -SW_W : 0;
  }, { passive: true });
  card.addEventListener('touchmove', (e) => {
    if (x0 == null) return;
    const mx = e.touches[0].clientX - x0; const my = e.touches[0].clientY - y0;
    if (horiz == null && (Math.abs(mx) > 8 || Math.abs(my) > 8)) horiz = Math.abs(mx) > Math.abs(my);
    if (!horiz) return;
    dx = mx;
    const t = Math.min(0, Math.max(-SW_W - 40, base + dx));
    card.style.transition = 'none'; card.style.transform = 'translateX(' + t + 'px)';
    row.classList.add('dragging');
  }, { passive: true });
  const end = () => {
    if (x0 == null) return;
    x0 = null; row.classList.remove('dragging');
    if (!horiz) return;
    row._dragged = true; setTimeout(() => { row._dragged = false; }, 350);
    card.style.transition = '';
    if (base + dx < -SW_W / 2) {
      card.style.transform = 'translateX(' + -SW_W + 'px)'; row.classList.add('open'); row.firstChild.tabIndex = 0; swOpen = row;
      if (navigator.vibrate) navigator.vibrate(10);
    } else swipeClose(row);
  };
  card.addEventListener('touchend', end); card.addEventListener('touchcancel', end);
}
document.addEventListener('touchstart', (e) => { if (swOpen && !swOpen.contains(e.target)) swipeClose(swOpen); }, { passive: true });

// delete: running sessions are killed first (the agent sequences kill → forget); exited ones go immediately
async function deleteSession(m, sid) {
  const s = session(m, sid); if (!s) return;
  const running = s.status !== 'exited' && s.status !== 'error';
  if (running && !(await confirmSheet('结束进程并删除？', '“' + s.name + '”还在运行，进程会被结束，会话从列表中删除。', '结束进程并删除'))) return;
  if (!send({ op: 'delete', m, sid })) toast('未连接', '稍后再试', null, 'error');
}
function onDeleted(m) {
  const mc = S.byId[m.m];
  if (mc) mc.sessions = mc.sessions.filter((x) => x.sid !== m.sid);
  if (isView(m)) {
    for (const d of $$('dialog.sheet[open]')) closeSheet(d);
    if (history.state && history.state.glimpse) history.back(); else { history.replaceState(null, '', location.pathname); closeSession(); }
  }
  renderHome();
  toast('已删除', null, null, 'done');
}

function renderHomeWidgets() {
  if (!S.haveState) return;
  renderInstall();
  const host = $('#home-widgets');
  host.hidden = renderWidgets(host, 'home', {}) === 0;
}
setInterval(() => { if (!S.view && document.visibilityState === 'visible') renderHome(); }, 30000);
$('#home-scroll').addEventListener('scroll', (e) => { $('#home-nav').classList.toggle('scrolled', e.target.scrollTop > 36); }, { passive: true });

// ------------------------------------------------------------------ session view
let term = null;
function ensureTerm() {
  if (term) return term;
  term = new window.Terminal({
    fontSize: S.fontSize, fontFamily: 'ui-monospace, "SF Mono", Menlo, monospace', scrollback: 5000, convertEol: false,
    theme: { background: '#0B0D10', foreground: '#DCE1E8', cursor: '#5AA9FF', selectionBackground: 'rgba(90,169,255,.35)',
      black: '#1C1F25', red: '#FF6B63', green: '#3DDC6A', yellow: '#FFB547', blue: '#5AA9FF', magenta: '#C792EA', cyan: '#56D4DD', white: '#DCE1E8',
      brightBlack: '#5C636E', brightRed: '#FF8E86', brightGreen: '#6EE7A0', brightYellow: '#FFD07A', brightBlue: '#8CC4FF', brightMagenta: '#DDB3F5', brightCyan: '#8BE8EE', brightWhite: '#FFFFFF' },
    allowProposedApi: true, cursorBlink: false, screenReaderMode: false,
  });
  if (window.FitAddon) { try { term.loadAddon(new window.FitAddon.FitAddon()); } catch (_) {} }
  // opened lazily (openTerm) the first time the terminal tab is visible: xterm measures the font on open
  term.onData((d) => { if (S.view) sendBytes(enc.encode(d)); });
  term.onBinary((d) => { if (S.view) { const b = new Uint8Array(d.length); for (let i = 0; i < d.length; i++) b[i] = d.charCodeAt(i) & 255; sendBytes(b); } });
  return term;
}
let termOpened = false;
function openTerm() {
  if (termOpened || !term || $('#term-wrap').hidden) return;
  termOpened = true; term.open($('#term'));
}
function sendBytes(bytes) {
  const v = S.view; if (!v) return false;
  return send({ op: 'in', m: v.m, sid: v.sid, d: bytesToB64(bytes) });
}

function routeTo(h) {
  const [m, sid] = (h || '').split('/');
  if (m && sid) { if (!S.view || S.view.m !== m || S.view.sid !== sid) openSession(m, sid, true); }
  else if (S.view) closeSession();
}
function openSession(m, sid, fromRoute) {
  if (S.view) closeSession(true);
  for (const d of $$('dialog.sheet[open]')) d.close();
  const s = session(m, sid);
  S.view = { m, sid, off: null, chatNext: null, ready: false, items: new Map(), sized: false, cardSig: null, approving: null, approvingId: null };
  if (!fromRoute && location.hash !== '#' + m + '/' + sid) history.pushState({ glimpse: 1 }, '', '#' + m + '/' + sid);
  show('view', 'fwd');
  const chat = $('#chat'); chat.innerHTML = '';
  chat.insertAdjacentHTML('beforeend', '<div class="chat-skel" aria-hidden="true"><div class="skel" style="height:44px;width:62%;align-self:flex-end;margin-left:auto;border-radius:20px"></div>' +
    '<div class="skel" style="height:96px;width:86%;border-radius:20px"></div><div class="skel" style="height:28px;width:55%"></div></div>');
  $('#jump').hidden = true;
  ensureTerm(); term.reset();
  const mode = (s && s.kind === 'shell') ? 'term' : (LS.getItem('glimpse.mode.' + m + '/' + sid) || LS.getItem('glimpse.mode') || 'chat');
  setMode(mode, true);
  $('#input').value = ''; autoGrow(); slashOpen(false);
  renderView();
  renderOutbox();
  subscribe();
  send({ op: 'ack', m, sid });
}
function closeSession(keepScreen) {
  const v = S.view; if (!v) return;
  send({ op: 'unsub', m: v.m, sid: v.sid });
  S.view = null;
  if (!keepScreen) { show('home', 'back'); renderHome(); }
}
$('#btn-back').onclick = () => {
  if (history.state && history.state.glimpse) history.back();          // popstate → routeTo('') → closeSession()
  else { history.replaceState(null, '', location.pathname); closeSession(); }
};

function subscribe() {
  const v = S.view; if (!v) return;
  v.ready = false;
  send({ op: 'sub', m: v.m, sid: v.sid, off: v.off, chat_from: v.chatNext });
}
function onReplay(r) {
  const v = S.view;
  const data = b64ToBytes(r.d || '');
  const s = session(v.m, v.sid);
  if (s && !v.sized) { sizeTerm(s.cols, s.rows); v.sized = true; }
  if (r.reset || v.off == null) { term.reset(); term.write(data); }
  else {
    const skip = v.off - r.off;
    if (skip < 0) { v.off = null; v.chatNext = null; subscribe(); return; }   // bytes missing: start over
    if (skip < data.length) term.write(skip > 0 ? data.subarray(skip) : data);
  }
  v.off = Math.max(v.off || 0, r.end != null ? r.end : r.off + data.length);
  v.ready = true;
  const sk = $('.chat-skel'); if (sk) sk.remove();
  if (r.chat_reset) { v.items.clear(); for (const el of $$('#chat > .msg')) el.remove(); v.chatNext = null; }
  addChat(r.chat || [], true);
  chatEmptyState();
  scheduleLive();
}
function onOut(m) {
  const v = S.view; if (!v.ready) return;
  const data = b64ToBytes(m.d);
  const skip = v.off - m.off;
  if (skip >= data.length) return;
  if (skip < 0) { subscribe(); return; }  // gap: ask for a fresh replay from our offset
  term.write(skip > 0 ? data.subarray(skip) : data);
  v.off = m.off + data.length;
  scheduleLive();
}
function sizeTerm(cols, rows) { if (cols && rows && (term.cols !== cols || term.rows !== rows)) term.resize(cols, rows); }

function renderView() {
  const v = S.view; if (!v) return;
  const mc = S.byId[v.m]; const s = session(v.m, v.sid);
  $('#v-name').textContent = s ? s.name : v.sid;
  $('#v-dot').className = 'dot ' + (s ? s.status : 'exited');
  if (!s) {
    $('#v-sub').textContent = mc && mc.online ? '会话已不存在' : '机器离线';
    setBanner('exited', 'info', mc && mc.online ? '会话已不存在' : '机器离线', mc && mc.online ? '它可能已被移除。' : '机器重新上线后会自动恢复。');
    renderCard(); renderViewWidgets(); return;
  }
  $('#v-sub').textContent = [v.m + ' ' + (mc ? mc.name : ''), KIND[s.kind] || s.kind, STATUS[s.status] || s.status, s.cwd].join(' · ');
  sizeTerm(s.cols, s.rows); v.sized = true;
  // the approval we were sending was resolved elsewhere (terminal, another phone) or replaced by a new one
  if (v.approving && (!s.pending || s.pending.id !== v.approvingId)) { v.approving = null; v.approvingId = null; v.cardSig = null; }
  if (s.status === 'waiting' && !s.pending && !s.question) setBanner('waiting', 'alert', s.detail || '等待你回复', '在下方用快捷键（1/2/3、↑↓、⏎、Esc）或输入框回复，也可切到终端查看');
  else if (s.status === 'error') setBanner('error', 'x-c', '出错' + (s.exit_code != null ? '（退出码 ' + s.exit_code + '）' : ''), s.detail || '', '更换 Key', openRestart);
  else if (s.status === 'exited') setBanner('exited', 'power', '进程已退出' + (s.exit_code != null ? '（退出码 ' + s.exit_code + '）' : ''), s.detail || '', s.kind !== 'shell' ? '重启' : null, openRestart);
  else setBanner(null);
  if (s.attention && document.visibilityState === 'visible') send({ op: 'ack', m: v.m, sid: v.sid });
  renderCard();
  $('#btn-slash').hidden = s.kind !== 'claude' && s.kind !== 'codex';
  if (!!s.menu !== !!v.hadMenu) { v.hadMenu = !!s.menu; scheduleLive(); }
  renderViewWidgets();
  if ($('#dlg-more').open) fillMore();
}
function setBanner(kind, icon, title, body, action, onAction) {
  const b = $('#v-banner');
  if (!kind) { b.hidden = true; b._h = null; return; }
  b.hidden = false; b.className = 'banner ' + kind;
  setHTML(b, ic(icon) + '<div class="grow"><b>' + esc(title) + '</b>' + (body ? esc(body) : '') + '</div>' +
    (action ? '<button type="button" class="btn sm">' + esc(action) + '</button>' : ''));
  const btn = $('.btn', b); if (btn) btn.onclick = onAction;
}
function renderViewWidgets() {
  const v = S.view; if (!v) return;
  const host = $('#v-widgets');
  const n = renderWidgets(host, 'session', { s: session(v.m, v.sid), mc: S.byId[v.m] });
  const t = $('#btn-wstrip');
  t.hidden = n === 0;
  host.hidden = n === 0 || !S.stripOpen;
  t.setAttribute('aria-expanded', String(S.stripOpen));
  t.setAttribute('aria-label', S.stripOpen ? '收起会话插件' : '展开会话插件');
  t.querySelector('.xs use').setAttribute('href', S.stripOpen ? '#i-up' : '#i-down');
}
$('#v-widgets').addEventListener('click', (e) => { if (e.target.closest('[data-act="ctl"]')) openCtl(); });
$('#btn-wstrip').onclick = () => { S.stripOpen = !S.stripOpen; LS.setItem('glimpse.strip', S.stripOpen ? '1' : '0'); renderViewWidgets(); };

function setMode(mode, initial) {
  const v = S.view;
  if (v) { LS.setItem('glimpse.mode.' + v.m + '/' + v.sid, mode); if (!initial) LS.setItem('glimpse.mode', mode); }
  S.mode = mode;
  $('#mode-chat').setAttribute('aria-selected', String(mode === 'chat'));
  $('#mode-term').setAttribute('aria-selected', String(mode === 'term'));
  $('#mode-chat').tabIndex = mode === 'chat' ? 0 : -1; $('#mode-term').tabIndex = mode === 'term' ? 0 : -1;
  $('#seg').classList.toggle('right', mode === 'term');
  $('#chat').hidden = mode !== 'chat';
  $('#term-wrap').hidden = mode !== 'term';
  $('#jump').hidden = true;
  if (v) { v.cardSig = null; renderCard(); }     // the menu card only shows in chat mode
  if (mode === 'term') requestAnimationFrame(() => { openTerm(); if (term) { term.refresh(0, term.rows - 1); term.scrollToBottom(); } termFollow(); });
  else { scheduleLive(); requestAnimationFrame(() => { const c = $('#chat'); c.scrollTop = c.scrollHeight; }); }
}
$('#mode-chat').onclick = () => setMode('chat');
$('#mode-term').onclick = () => setMode('term');
$('#seg').addEventListener('keydown', (e) => {
  if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { e.preventDefault(); const m = S.mode === 'chat' ? 'term' : 'chat'; setMode(m); $('#mode-' + m).focus(); }
});

// ------------------------------------------------------------------ chat rendering
// tiny, safe markdown: everything is escaped first; only code fences, inline code, bold, headings, lists, links
function mdInline(s) {
  return esc(s)
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
}
function md(text) {
  const parts = String(text || '').split(/```/);
  let h = '';
  parts.forEach((p, i) => {
    if (i % 2) {
      const m = p.match(/^([\w+#.-]*)\n/); const lang = m ? m[1] : '';
      const body = m ? p.slice(m[0].length) : p;
      h += '<pre>' + (lang ? '<span class="lang">' + esc(lang) + '</span>' : '') +
        '<button type="button" class="copy-code" aria-label="复制代码">' + ic('copy', 'sm') + '<span>复制</span></button>' +
        '<code>' + esc(body.replace(/\n$/, '')) + '</code></pre>';
      return;
    }
    let list = null;
    const close = () => { if (list) { h += '</' + list + '>'; list = null; } };
    let para = [];
    const flush = () => { if (para.length) { h += '<p>' + para.map(mdInline).join('<br>') + '</p>'; para = []; } };
    for (const line of p.split('\n')) {
      let mm;
      if ((mm = line.match(/^(#{1,4})\s+(.*)$/))) { flush(); close(); h += '<h' + mm[1].length + '>' + mdInline(mm[2]) + '</h' + mm[1].length + '>'; }
      else if ((mm = line.match(/^\s*[-*•]\s+(.*)$/))) { flush(); if (list !== 'ul') { close(); h += '<ul>'; list = 'ul'; } h += '<li>' + mdInline(mm[1]) + '</li>'; }
      else if ((mm = line.match(/^\s*\d+[.)]\s+(.*)$/))) { flush(); if (list !== 'ol') { close(); h += '<ol>'; list = 'ol'; } h += '<li>' + mdInline(mm[1]) + '</li>'; }
      else if (!line.trim()) { flush(); close(); }
      else { close(); para.push(line); }
    }
    flush(); close();
  });
  return h;
}
const TOOL_ICON = { Bash: 'term', Shell: 'term', Read: 'file', Write: 'pencil', Edit: 'pencil', MultiEdit: 'pencil', Grep: 'search', Glob: 'search',
  WebFetch: 'globe', WebSearch: 'globe', Task: 'sparkle', Agent: 'sparkle', TodoWrite: 'list', AskUserQuestion: 'help' };
function chatItemEl(it, el) {
  el = el || document.createElement('div');
  el.className = 'msg ' + it.role + (it.error ? ' err' : '');
  if (it.role === 'user') {
    setHTML(el, mdInline(it.text).replace(/\n/g, '<br>') + '<span class="ts">' + hhmm(it.ts) + '</span>');
  } else if (it.role === 'assistant') {
    el.classList.add('md');
    setHTML(el, md(it.text) + '<span class="ts">' + hhmm(it.ts) + '</span>');
  } else if (it.role === 'tool') {
    const i = it.text.indexOf(':');
    const name = it.tool || (i > 0 ? it.text.slice(0, i) : it.text);
    const rest = i > 0 ? it.text.slice(i + 1).trim() : '';
    setHTML(el, '<span class="ti">' + ic(TOOL_ICON[name] || 'tool') + '</span><span class="tx"><span class="tn">' + esc(name) + '</span> ' + esc(rest) + '</span>');
  } else if (it.role === 'result') {
    const lines = String(it.text).split('\n').length;
    const short = lines <= 4 && it.text.length < 300;
    if (el.tagName !== 'BUTTON') { const b = document.createElement('button'); b.type = 'button'; el = b; }
    el.className = 'msg result' + (it.error ? ' err' : '') + (short ? ' short' : '');
    if (!el.hasAttribute('aria-expanded')) el.setAttribute('aria-expanded', 'false');
    setHTML(el, '<div class="rh">' + (it.error ? '错误输出' : '输出') + (short ? '' : ' · ' + lines + ' 行' + ic('down')) + '</div><pre>' + esc(it.text) + '</pre>');
    el.onclick = short ? null : () => el.setAttribute('aria-expanded', el.getAttribute('aria-expanded') === 'true' ? 'false' : 'true');
  } else {
    el.textContent = it.text;
  }
  return el;
}
function nearBottom(el) { return el.scrollHeight - el.scrollTop - el.clientHeight < 90; }
function addChat(items, initial) {
  const v = S.view; if (!v || !items.length) return;
  const box = $('#chat');
  const stick = initial || nearBottom(box);
  const live = $('.livebox', box);
  for (const it of items) {
    const old = v.items.get(it.seq);
    const el = chatItemEl(it, old && old.tagName === (it.role === 'result' ? 'BUTTON' : 'DIV') ? old : null);
    if (old && old !== el) old.replaceWith(el);
    else if (!old) box.insertBefore(el, live || null);
    v.items.set(it.seq, el);
    v.chatNext = Math.max(v.chatNext || 0, it.seq + 1);
  }
  chatEmptyState();
  if (stick) box.scrollTop = box.scrollHeight;
  else $('#jump').hidden = S.mode !== 'chat';
}
function chatEmptyState() {
  const v = S.view; if (!v) return;
  const box = $('#chat'); let e = $('.chat-empty', box);
  const s = session(v.m, v.sid);
  if (v.items.size || !v.ready) { if (e) e.remove(); return; }
  if (!e) { e = document.createElement('div'); e.className = 'chat-empty'; box.insertBefore(e, $('.livebox', box)); }
  setHTML(e, ic(s && s.kind === 'shell' ? 'term' : 'chat', 'lg') + '<div>' + (s && s.kind === 'shell' ? 'Shell 会话没有对话记录，切到“终端”查看' :
    '还没有对话。<br>在下面输入第一条指令吧') + '</div>');
}
$('#chat').addEventListener('scroll', (e) => { if (nearBottom(e.target)) $('#jump').hidden = true; }, { passive: true });
// copy a code block (delegated: messages are re-rendered). The text comes from the <code>, not the button label
$('#chat').addEventListener('click', (e) => {
  const b = e.target.closest('.copy-code'); if (!b) return;
  e.preventDefault(); e.stopPropagation();
  const code = b.parentElement.querySelector('code');
  copyText(code ? code.textContent : '', '代码').then(() => {
    b.classList.add('done'); b.lastChild.textContent = '已复制';
    setTimeout(() => { b.classList.remove('done'); b.lastChild.textContent = '复制'; }, 1500);
  });
});
$('#jump').onclick = () => { const c = $('#chat'); c.scrollTo({ top: c.scrollHeight, behavior: reduceMotion() ? 'auto' : 'smooth' }); $('#jump').hidden = true; };

// ------------------------------------------------------------------ live screen (tail of the terminal under the chat)
let liveTimer = null;
function scheduleLive() {
  if (liveTimer) return;
  liveTimer = setTimeout(() => { liveTimer = null; renderLive(); }, 250);
}
function screenText(maxLines) {
  const b = term.buffer.active; const lines = [];
  const end = b.baseY + term.rows;
  for (let y = Math.max(0, end - term.rows); y < end; y++) { const l = b.getLine(y); lines.push(l ? l.translateToString(true) : ''); }
  while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
  let i = 0; while (i < lines.length && !lines[i].trim()) i++;
  return lines.slice(Math.max(i, lines.length - maxLines)).join('\n');
}
// the PTY can be taller than the visible box: keep the cursor line in view
function termFollow() {
  if (!termOpened || S.mode !== 'term') return;
  const w = $('#term-wrap'); const rows = $('.xterm-rows', w);
  const ch = rows && rows.firstElementChild ? rows.firstElementChild.getBoundingClientRect().height : S.fontSize * 1.2;
  const y = (term.buffer.active.cursorY + 2) * ch + 6;
  w.scrollTop = Math.max(0, y - w.clientHeight);
}
function renderLive() {
  termFollow();
  const v = S.view; if (!v || S.mode !== 'chat' || !term || !v.ready) return;
  const s = session(v.m, v.sid);
  const box = $('#chat');
  let live = $('.livebox', box);
  if (!live) {
    live = document.createElement('details'); live.className = 'livebox';
    live.innerHTML = '<summary><span class="live-dot" aria-hidden="true"></span>实时屏幕' + ic('down') + '</summary><pre aria-label="终端当前画面"></pre>';
    live.open = LS.getItem('glimpse.liveOpen') !== '0';
    // a forced open (waiting / menu) is not the user's choice: don't persist it
    live.addEventListener('toggle', () => { if (live._forced) { live._forced = false; return; } LS.setItem('glimpse.liveOpen', live.open ? '1' : '0'); });
    box.appendChild(live);
  }
  const waiting = s && s.status === 'waiting';
  const menu = !!(s && s.menu);       // an interactive TUI menu is up: show the whole screen, not the tail
  if ((waiting || menu) && !live.open) { live._forced = true; live.open = true; }
  live.classList.toggle('full', menu);
  const stick = nearBottom(box) || (menu && !live._menu);
  live._menu = menu;
  const txt = screenText(menu ? term.rows : waiting ? 30 : 14);
  const pre = $('pre', live); if (pre.textContent !== txt) pre.textContent = txt;
  live.hidden = !txt;
  if (stick) box.scrollTop = box.scrollHeight;
}

// ------------------------------------------------------------------ approval / question card (above the composer)
function diffHTML(t) {
  return String(t).split('\n').map((l) => {
    const e = esc(l);
    if (/^\+/.test(l)) return '<span class="add">' + e + '</span>';
    if (/^-/.test(l)) return '<span class="del">' + e + '</span>';
    if (/^@@/.test(l)) return '<span class="hunk">' + e + '</span>';
    return e;
  }).join('\n');
}
function renderCard() {
  const host = $('#v-card'); const v = S.view;
  const s = v && session(v.m, v.sid);
  let sig = ''; let html = '';
  if (s && s.pending) {
    const p = s.pending; sig = 'p:' + p.id + ':' + (v.approving || '');
    const busy = (d) => (v.approving === d ? '<span class="spinner" aria-hidden="true"></span>' : '');
    const dis = v.approving ? ' disabled' : '';
    html = '<section class="acard" role="alertdialog" aria-labelledby="a-s" aria-describedby="a-d">' +
      '<div class="a-h"><span class="a-ic">' + ic('shield') + '</span><div class="a-t"><div class="a-k">' + esc(p.tool) + ' 请求授权</div>' +
      '<div class="a-s" id="a-s">' + esc(p.summary) + '</div></div></div>' +
      (p.detail ? '<pre class="a-d" id="a-d" tabindex="0">' + diffHTML(p.detail) + '</pre>' : '') +
      '<div class="a-btns">' +
      '<button type="button" class="btn primary" data-a="allow"' + dis + '>' + busy('allow') + ic('check') + '允许</button>' +
      '<button type="button" class="btn danger" data-a="deny"' + dis + '>' + busy('deny') + ic('x') + '拒绝</button>' +
      (p.can_always ? '<button type="button" class="btn" data-a="always"' + dis + '>' + busy('always') + '总是允许</button>' : '') +
      '<button type="button" class="btn ghost' + (p.can_always ? '' : ' wide') + '" data-a="terminal"' + dis + '>在终端处理</button>' +
      '</div></section>';
  } else if (s && s.question && s.question.options && s.question.options.length) {
    const q = s.question; sig = 'q:' + q.question;
    html = '<section class="acard q" role="group" aria-labelledby="q-s">' +
      '<div class="a-h"><span class="a-ic">' + ic('help') + '</span><div class="a-t"><div class="a-k">' + esc(q.header || '提问') + (q.multi ? ' · 可多选' : '') + '</div>' +
      '<div class="a-s" id="q-s">' + esc(q.question) + '</div></div></div><div class="opts">' +
      q.options.map((o, i) => '<button type="button" class="opt" data-q="' + (i + 1) + '"><span class="n">' + (i + 1) + '</span><span class="grow"><div class="ol">' +
        esc(o.label) + '</div>' + (o.description ? '<div class="od">' + esc(o.description) + '</div>' : '') + '</span></button>').join('') +
      '<button type="button" class="opt other" data-q="other"><span class="n">…</span><span class="grow"><div class="ol">其他</div><div class="od">在下方输入框回复</div></span></button>' +
      '</div></section>';
  } else if (s && s.menu && S.mode === 'chat' && Array.isArray(s.menu.options) && s.menu.options.length) {
    // interactive TUI menu (claude /model, /effort slider, codex /model) detected by the agent from the screen
    const mn = s.menu; sig = 'm:' + JSON.stringify(mn) + ':' + (v.menuBusy == null ? '' : v.menuBusy);
    const dis = v.menuBusy != null ? ' disabled' : '';
    const canS = s.kind === 'claude' && /s to use|s for this session/i.test(mn.hint || '');
    const slider = mn.kind === 'slider';
    html = '<section class="acard mcard' + (slider ? ' slider' : '') + '" role="group" aria-labelledby="mn-t">' +
      '<div class="a-h"><span class="a-ic">' + ic(slider ? 'sliders' : 'menu-list') + '</span><div class="a-t"><div class="a-k">终端菜单 · ' + esc(KIND[s.kind] || s.kind) + '</div>' +
      '<div class="a-s" id="mn-t">' + esc(mn.title || (slider ? 'Adjust' : 'Select')) + '</div></div></div>' +
      '<div class="' + (slider ? 'mslider' : 'opts mopts') + '" role="listbox" aria-labelledby="mn-t">' +
      mn.options.map((o) => {
        const cur = o.i === mn.cursor; const busy = v.menuBusy === o.i;
        return '<button type="button" class="opt' + (cur ? ' cursor' : '') + (o.current ? ' current' : '') + '" role="option" aria-selected="' + cur + '" data-mi="' + esc(o.i) + '"' + dis + '>' +
          (slider ? '' : '<span class="n">' + (busy ? '<span class="spinner" aria-hidden="true"></span>' : esc(o.i + 1)) + '</span>') +
          '<span class="grow"><div class="ol">' + (slider && busy ? '<span class="spinner" aria-hidden="true"></span>' : '') + esc(o.label) +
          (o.current ? ' <span class="cur-tag">current</span>' : '') + '</div>' + (o.desc ? '<div class="od">' + esc(o.desc) + '</div>' : '') + '</span>' +
          (cur && !slider ? '<span class="cur-mark" aria-hidden="true">' + ic('chev', 'sm') + '</span>' : '') + '</button>';
      }).join('') + '</div>' +
      (mn.hint ? '<p class="m-hint">' + esc(mn.hint) + '</p>' : '') +
      '<div class="a-btns">' + '<button type="button" class="btn' + (canS ? '' : ' wide') + '" data-mk="esc"' + dis + '>Esc · 取消</button>' +
      (canS ? '<button type="button" class="btn" data-mk="s"' + dis + '>s · 仅本会话</button>' : '') + '</div></section>';
  }
  if (v && v.menuBusy != null && !(s && s.menu)) { clearTimeout(v.menuTimer); v.menuBusy = null; }   // menu closed: done
  if (v && v.cardSig === sig) return;
  if (v) v.cardSig = sig;
  const had = !!host.firstChild;
  host.innerHTML = html;
  host.parentNode.classList.toggle('has-menu', sig.startsWith('m:'));
  for (const b of $$('[data-mi]', host)) b.onclick = () => menuSend({ index: +b.dataset.mi }, +b.dataset.mi);
  for (const b of $$('[data-mk]', host)) b.onclick = () => menuSend({ key: b.dataset.mk }, -1);
  for (const b of $$('[data-a]', host)) b.onclick = () => approve(b.dataset.a);
  for (const b of $$('[data-q]', host)) b.onclick = () => {
    if (b.dataset.q === 'other') { $('#input').focus(); return; }
    sendBytes(enc.encode(b.dataset.q));
    if (!s.question.multi) { v.cardSig = null; host.innerHTML = ''; }
  };
  if (html && !had) {
    announce(s.pending ? '需要授权：' + s.pending.summary : s.question ? '有问题需要回答：' + s.question.question : '终端菜单：' + (s.menu.title || ''));
    if (navigator.vibrate) navigator.vibrate(30);
  }
}
function approve(decision) {
  const v = S.view; const s = v && session(v.m, v.sid);
  if (!s || !s.pending || v.approving) return;
  v.approving = decision; v.approvingId = s.pending.id; renderCard();
  if (!send({ op: 'approve', m: v.m, sid: v.sid, id: s.pending.id, decision })) { v.approving = null; renderCard(); toast('未连接', '稍后再试', null, 'error'); }
  setTimeout(() => { if (S.view === v && v.approving) { v.approving = null; v.cardSig = null; renderCard(); } }, 8000);
}

// menu card: op menu {index} moves the TUI cursor there and presses Enter; {key:'esc'|'s'} sends Esc / "s". Reply menu_ok
function menuSend(arg, busyIdx) {
  const v = S.view; const s = v && session(v.m, v.sid);
  if (!s || !s.menu || v.menuBusy != null) return;
  if (!send(Object.assign({ op: 'menu', m: v.m, sid: v.sid }, arg))) { toast('未连接', '稍后再试', null, 'error'); return; }
  v.menuBusy = busyIdx; v.cardSig = null; renderCard();
  clearTimeout(v.menuTimer);
  v.menuTimer = setTimeout(() => { if (S.view === v && v.menuBusy != null) menuDone(); }, 6000);
}
function menuDone() {
  const v = S.view; if (!v) return;
  clearTimeout(v.menuTimer); v.menuBusy = null; v.cardSig = null; renderCard();
}

// ---- "/" quick slash commands: tap = type the command + Enter (same as op text); /clear only fills the composer (it wipes the context)
const SLASH = {
  claude: [['/model', 'switch model'], ['/effort', 'reasoning effort'], ['/compact', 'summarize the context'], ['/status', 'account, model, usage'], ['/clear', 'start a fresh context', 1]],
  codex: [['/model', 'model and effort'], ['/status', 'session config and usage'], ['/compact', 'summarize the context']],
};
function slashOpen(on) {
  const pop = $('#slash-pop'); const btn = $('#btn-slash');
  const v = S.view; const s = v && session(v.m, v.sid);
  const list = s && SLASH[s.kind];
  if (on && !list) on = false;
  pop.hidden = !on; btn.setAttribute('aria-expanded', String(!!on)); btn.classList.toggle('on', !!on);
  if (!on) return;
  pop.innerHTML = list.map(([c, d, fill]) => '<button type="button" role="menuitem" class="slash-it" data-cmd="' + esc(c) + '"' + (fill ? ' data-fill="1"' : '') + '>' +
    '<span class="mono">' + esc(c) + '</span><span class="sd">' + esc(d) + '</span></button>').join('');
  for (const b of $$('.slash-it', pop)) {
    b.addEventListener('mousedown', (e) => e.preventDefault());
    b.onclick = () => {
      slashOpen(false);
      if (b.dataset.fill) { input.value = b.dataset.cmd; autoGrow(); input.focus(); return; }
      const vv = S.view; if (!vv) return;
      if (!send({ op: 'text', m: vv.m, sid: vv.sid, text: b.dataset.cmd })) toast('未连接', '稍后再试', null, 'error');
    };
  }
}
$('#btn-slash').addEventListener('mousedown', (e) => e.preventDefault());
$('#btn-slash').onclick = () => slashOpen($('#slash-pop').hidden);
document.addEventListener('click', (e) => { if (!$('#slash-pop').hidden && !e.target.closest('#slash-pop, #btn-slash')) slashOpen(false); });

// ------------------------------------------------------------------ input: quick keys + composer
// ---- quick keys: user-configurable bar (Settings → 快捷键). Built-ins by id, plus custom keys {label, send}.
const KEY_CATALOG = [
  { id: 'paste', label: '粘贴', aria: '粘贴剪贴板' },
  { id: 'esc', label: 'Esc' }, { id: 'ctrlc', label: '^C', aria: 'Control C 中断' }, { id: 'tab', label: 'Tab' },
  { id: 'stab', label: '⇧Tab', aria: 'Shift Tab' }, { id: 'up', label: '↑', aria: '上' }, { id: 'down', label: '↓', aria: '下' },
  { id: 'left', label: '←', aria: '左' }, { id: 'right', label: '→', aria: '右' }, { id: 'enter', label: '⏎', aria: '回车' },
  { id: '1', label: '1' }, { id: '2', label: '2' }, { id: '3', label: '3' }, { id: 'y', label: 'y' }, { id: 'n', label: 'n' },
  { id: 'ctrld', label: '^D', aria: 'Control D' }, { id: 'ctrll', label: '^L', aria: 'Control L 清屏' },
  { id: 'ctrlr', label: '^R', aria: 'Control R' }, { id: 'ctrlz', label: '^Z', aria: 'Control Z' },
  { id: 'ctrlu', label: '^U', aria: 'Control U 清空输入' }, { id: 'pgup', label: 'PgUp' }, { id: 'pgdn', label: 'PgDn' },
  { id: 'home', label: 'Home' }, { id: 'end', label: 'End' }, { id: 'slash', label: '/' }, { id: 'bang', label: '!' },
];
Object.assign(KEYS, { ctrld: '\x04', ctrll: '\x0c', ctrlr: '\x12', ctrlz: '\x1a', ctrlu: '\x15', pgup: '\x1b[5~', pgdn: '\x1b[6~',
  home: '\x1b[H', end: '\x1b[F', slash: '/', bang: '!' });
const DEFAULT_KEYBAR = ['paste', 'esc', 'ctrlc', 'tab', 'stab', 'up', 'down', 'left', 'right', 'enter', '1', '2', '3', 'y', 'n'];
// custom key: {id:'c:<n>', label, send} where send is text with escapes: \e \r \n \t ^X (ctrl), \xNN
function keyBytes(id) {
  if (KEYS[id] != null) return KEYS[id];
  const c = customKeys().find((k) => k.id === id);
  return c ? unescapeKey(c.send) : null;
}
function unescapeKey(t) {
  return String(t || '').replace(/\\(e|r|n|t|\\|x[0-9a-fA-F]{2})|\^([A-Za-z@\[\]\\^_?])/g, (m, a, c) => {
    if (c) return c === '?' ? '\x7f' : String.fromCharCode(c.toUpperCase().charCodeAt(0) & 0x1f);
    if (a === 'e') return '\x1b'; if (a === 'r') return '\r'; if (a === 'n') return '\n'; if (a === 't') return '\t';
    if (a === '\\') return '\\'; return String.fromCharCode(parseInt(a.slice(1), 16));
  });
}
function customKeys() { try { const v = JSON.parse(LS.getItem('glimpse.customKeys') || '[]'); return Array.isArray(v) ? v : []; } catch (_) { return []; } }
function keybar() {
  try { const v = JSON.parse(LS.getItem('glimpse.keybar') || 'null'); if (Array.isArray(v)) return v; } catch (_) { /* default */ }
  return DEFAULT_KEYBAR.slice();
}
function keyDef(id) { return KEY_CATALOG.find((k) => k.id === id) || customKeys().find((k) => k.id === id) || null; }
function renderKeybar() {
  const host = $('#keys');
  const ids = keybar().filter(keyDef);
  host.innerHTML = ids.map((id, i) => {
    const k = keyDef(id);
    const sep = i && ['up', '1', 'paste'].includes(id) ? ' sep' : '';
    return '<button type="button" class="key' + sep + (id === 'paste' ? ' paste' : '') + '" data-k="' + esc(id) + '" aria-label="' +
      esc(k.aria || k.label) + '">' + (id === 'paste' ? ic('clipboard', 'xs') : '') + esc(k.label) + '</button>';
  }).join('');
}
async function pasteToSession() {
  let t = '';
  try { t = await navigator.clipboard.readText(); } catch (_) { t = ''; }
  if (!t) { openPasteSheet(); return; }          // no clipboard access (http, denied): paste into a box instead
  sendPaste(t);
}
function sendPaste(t) {
  const v = S.view; if (!v || !t) return;
  // bracketed paste so TUIs (claude / codex / vim) take it as one paste, not keystrokes
  sendBytes(enc.encode('\x1b[200~' + t.replace(/\r\n/g, '\n') + '\x1b[201~'));
  toast('已粘贴', t.length > 80 ? t.slice(0, 77) + '…' : t, null, 'done');
}
$('#keys').addEventListener('mousedown', (e) => { if (e.target.closest('.key')) e.preventDefault(); });   // keep the keyboard open
$('#keys').addEventListener('click', (e) => {
  const b = e.target.closest('.key'); if (!b) return;
  e.preventDefault();
  if (b.dataset.k === 'paste') { pasteToSession(); return; }
  const k = keyBytes(b.dataset.k); if (k) sendBytes(enc.encode(k));
});
renderKeybar();
function openPasteSheet() {
  const d = $('#dlg-paste'); const ta = $('#paste-text');
  ta.value = ''; openSheet(d);
  setTimeout(() => ta.focus(), 120);       // long-press -> 粘贴 works in a textarea even when clipboard API does not
}
$('#paste-go').onclick = () => { const t = $('#paste-text').value; closeSheet($('#dlg-paste')); sendPaste(t); };
// paste straight into the terminal view (desktop browsers / iPad with a keyboard: Cmd-V)
document.addEventListener('paste', (e) => {
  if (!S.view || S.mode !== 'term' || e.target.closest('textarea, input')) return;
  const t = e.clipboardData && e.clipboardData.getData('text');
  if (t) { e.preventDefault(); sendPaste(t); }
});
const input = $('#input');
function autoGrow() {
  input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 140) + 'px';
  const has = !!input.value.trim(); const btn = $('#btn-send');
  btn.classList.toggle('enter', !has);
  btn.setAttribute('aria-label', has ? '发送' : '发送回车');
  $('use', btn).setAttribute('href', has ? '#i-send' : '#i-return');
}
input.addEventListener('input', autoGrow);
$('#composer').addEventListener('submit', (e) => {
  e.preventDefault();
  const v = S.view; if (!v) return;
  const text = input.value;
  if (!text.trim()) { sendBytes(enc.encode('\r')); return; }
  const o = { op: 'text', m: v.m, sid: v.sid, text };
  if (wsConfirmed()) send(o);
  else {
    // socket not confirmed (e.g. just resumed from background): queue, probe, send after the next pong / open
    S.outbox.push(o); renderOutbox();
    if (!S.ws || S.ws.readyState > 1) { S.backoff = 500; connect(); } else ping();
  }
  input.value = ''; autoGrow();
  const c = $('#chat'); c.scrollTop = c.scrollHeight;
});
function renderOutbox() {
  const box = $('#chat'); let el = $('.outbox', box);
  const v = S.view;
  const mine = v ? S.outbox.filter((o) => o.m === v.m && o.sid === v.sid) : [];
  if (!mine.length) { if (el) el.remove(); return; }
  if (!el) { el = document.createElement('div'); el.className = 'outbox'; }
  box.insertBefore(el, $('.livebox', box));
  setHTML(el, mine.map((o) => '<div class="msg user pending">' + mdInline(o.text).replace(/\n/g, '<br>') + '<span class="ts"><span class="spinner" aria-hidden="true"></span>发送中…</span></div>').join(''));
}
input.addEventListener('keydown', (e) => {
  // hardware keyboard: Enter sends, Shift+Enter newline
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && window.matchMedia('(pointer: fine)').matches) { e.preventDefault(); $('#composer').requestSubmit(); }
});

// ------------------------------------------------------------------ more / rename / confirm
function fillMore() {
  const v = S.view; const s = v && session(v.m, v.sid);
  const ai = s && (s.kind === 'claude' || s.kind === 'codex');
  $('#act-restart').hidden = !ai;
  $('#act-restart-sub').textContent = s ? '当前：' + (s.key || '默认') + (s.can_resume ? ' · 可恢复对话' : '') : '';
  $('#act-fit-sub').textContent = s ? '当前 ' + s.cols + '×' + s.rows : '';
  $('#act-ac-row').hidden = !ai;
  if (ai) {
    $('#act-ac').checked = !!s.auto_continue;
    $('#act-ac-sub').textContent = s.auto_continue ? (s.ac_count ? '已自动继续 ' + s.ac_count + ' 次（成功一轮后清零，最多连续 5 次）' : '已开启：遇到临时错误会自动发送“继续”')
      : '服务器过载、流中断、输出被截断时自动发送“继续”；Key 余额不足等仍会通知你';
  }
  const running = s && s.status !== 'exited' && s.status !== 'error';
  $('#act-kill').hidden = !running;
  $('#act-delete').disabled = !s;
  $('#act-delete-sub').textContent = running ? '会先结束进程' : '';
  $('#act-ctl').hidden = !ai;
  if (ai) {
    const md = sessMode(s);
    $('#act-ctl-sub').textContent = s.kind === 'claude' ? [modeName(md.mode || 'default'), md.model ? shortModel(md.model) : '', md.effort || ''].filter(Boolean).join(' · ')
      : [md.model ? shortModel(md.model) : 'default model', md.effort || '', md.sandbox || ''].filter(Boolean).join(' · ');
  }
  if (v) {
    $('#act-cmd').textContent = attachCmd(v.m, v.sid);
    $('#act-link-sub').textContent = deepLink(v.m, v.sid);
  }
}
// `gli attach <sid>` on the hub computer finds the session on any machine (local daemon first, then through the hub)
function attachCmd(m, sid) { return 'gli attach ' + sid; }
function deepLink(m, sid) { return location.origin + '/#' + m + '/' + sid; }
async function copyText(t, what) {
  let ok = false;
  try { await navigator.clipboard.writeText(t); ok = true; } catch (_) {
    // http:// or older WebKit: fall back to a hidden textarea + execCommand
    const ta = document.createElement('textarea'); ta.value = t; ta.setAttribute('readonly', ''); ta.style.position = 'fixed'; ta.style.opacity = '0';
    (document.querySelector('dialog[open]') || document.body).appendChild(ta); ta.select();
    try { ok = document.execCommand('copy'); } catch (__) { ok = false; }
    ta.remove();
  }
  const preview = t.length > 120 ? t.slice(0, 117).replace(/\s+/g, ' ') + '…' : t;
  if (ok) toast('已复制' + (what ? what : ''), preview, null, 'done'); else toast('无法复制', '请长按选中后手动复制', null, 'error');
  return ok;
}
$('#act-ac').onchange = (e) => {
  const v = S.view; if (!v) return;
  send({ op: 'auto_continue', m: v.m, sid: v.sid, on: e.target.checked });
  toast(e.target.checked ? '已开启自动继续' : '已关闭自动继续', '', null, 'done');
};
$('#act-cmd-copy').onclick = () => { const v = S.view; if (v) copyText(attachCmd(v.m, v.sid), '命令'); };
$('#act-link').onclick = () => { const v = S.view; if (v) copyText(deepLink(v.m, v.sid), '链接'); };
$('#act-ctl').onclick = () => { closeSheet($('#dlg-more')); setTimeout(openCtl, 230); };
$('#btn-more').onclick = () => { fillMore(); openSheet($('#dlg-more')); };
$('#act-rename').onclick = () => {
  const v = S.view; const s = v && session(v.m, v.sid); if (!s) return;
  closeSheet($('#dlg-more'));
  $('#rename-input').value = s.name;
  setTimeout(() => { openSheet($('#dlg-rename')); $('#rename-input').focus(); $('#rename-input').select(); }, 230);
};
$('#rename-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const v = S.view; const name = $('#rename-input').value.trim();
  if (v && name) send({ op: 'rename', m: v.m, sid: v.sid, name });
  closeSheet($('#dlg-rename'));
});
$('#act-fit').onclick = () => {
  const v = S.view; if (!v) return;
  closeSheet($('#dlg-more')); setMode('term');
  requestAnimationFrame(() => {
    const wrap = $('#term-wrap');
    let cw = S.fontSize * 0.6; let ch = S.fontSize * 1.2;
    try { const c = term._core._renderService.dimensions.css.cell; if (c.width) { cw = c.width; ch = c.height; } } catch (_) {}
    const cols = Math.floor((wrap.clientWidth - 12) / cw); const rows = Math.floor((wrap.clientHeight - 12) / ch);
    if (cols > 0 && rows > 0) { send({ op: 'resize', m: v.m, sid: v.sid, cols: Math.max(40, cols), rows: Math.max(10, rows) }); toast('终端尺寸', Math.max(40, cols) + ' × ' + Math.max(10, rows)); }
  });
};
$('#act-kill').onclick = async () => {
  const v = S.view; if (!v) return;
  closeSheet($('#dlg-more'));
  await new Promise((r) => setTimeout(r, 230));
  if (await confirmSheet('结束这个进程？', '正在运行的任务会被中断，对话记录会保留。', '结束进程')) send({ op: 'kill', m: v.m, sid: v.sid });
};
$('#act-delete').onclick = async () => {
  const v = S.view; if (!v) return;
  closeSheet($('#dlg-more'));
  await new Promise((r) => setTimeout(r, 230));
  deleteSession(v.m, v.sid);
};

// ------------------------------------------------------------------ new session sheet
function newBusy(on) {
  $('#new-note').hidden = !on;
  for (const b of $$('#new-form [type=submit]')) b.disabled = on;
}
function recentDirs(mid) { try { return JSON.parse(LS.getItem('glimpse.recent.' + mid) || '[]'); } catch (_) { return []; } }
function pushRecent(mid, dir) {
  if (!dir) return;
  const l = [dir].concat(recentDirs(mid).filter((d) => d !== dir)).slice(0, 6);
  LS.setItem('glimpse.recent.' + mid, JSON.stringify(l));
}
function newKind() { const r = $('#new-form input[name=kind]:checked'); return r ? r.value : 'claude'; }
function fillNew() {
  const sel = $('#new-machine'); const prev = sel.value || LS.getItem('glimpse.newMachine');
  sel.innerHTML = S.machines.map((m) => '<option value="' + esc(m.id) + '"' + (m.online ? '' : ' disabled') + '>' +
    esc(m.id + ' · ' + m.name + (m.online ? '' : '（离线）')) + '</option>').join('');
  const ok = S.machines.find((m) => m.id === prev && m.online) || S.machines.find((m) => m.online);
  if (ok) sel.value = ok.id;
  fillNewDeps();
}
function fillNewDeps() {
  const mc = S.byId[$('#new-machine').value]; const kind = newKind();
  const ai = kind !== 'shell';
  $('#new-key-row').hidden = !ai || !S.keys.length;
  const def = mc && mc.presets && mc.presets[kind] && mc.presets[kind].key;
  $('#new-key').innerHTML = '<option value="">' + (kbState().mode === 'least_used' ? '自动（用量最低的 Key）' : '默认' + (def ? '（' + esc(def) + '）' : '')) + '</option>' +
    S.keys.map((k) => '<option value="' + esc(k.name) + '">' + esc(k.name) + (keyUsageText(k) ? '  ·  ' + esc(keyUsageText(k)) : '') + '</option>').join('');
  const rec = mc ? recentDirs(mc.id) : [];
  const cwd = $('#new-cwd');
  if (!cwd.dataset.touched) cwd.value = rec[0] || (mc && mc.default_cwd) || '~';
  $('#new-recent').innerHTML = rec.map((d) => '<button type="button" class="rchip" data-dir="' + esc(d) + '">' + ic('folder', 'xs') + '<span>' + esc(d) + '</span></button>').join('');
  for (const b of $$('#new-recent .rchip')) b.onclick = () => { cwd.value = b.dataset.dir; cwd.dataset.touched = '1'; };
  fillAdv(kind);
}
// state.options = {claude:{models,efforts,modes}, codex:{models,efforts,approvals,sandboxes}} (hub defaults / config.json)
function opts(kind) {
  const o = (S.options && S.options[kind]) || {};
  const d = kind === 'claude' ? { models: ['default', 'opus', 'sonnet', 'haiku', 'fable', 'opusplan'], efforts: ['low', 'medium', 'high', 'xhigh', 'max', 'ultracode'],
    modes: ['manual', 'acceptEdits', 'auto', 'plan', 'dontAsk', 'bypassPermissions'] }
    : { models: [], efforts: ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'], approvals: ['untrusted', 'on-request', 'never'], sandboxes: ['read-only', 'workspace-write', 'danger-full-access'] };
  return Object.assign(d, o);
}
// ---- real model / effort / mode lists: op models {m, kind} -> {t:'models', m, kind, models:[{id,label,desc,efforts,default_effort}], efforts, modes}
// fetched when a picker opens, cached per machine+kind for the page lifetime; state.options is the fallback while loading / on error
const MODELS = {};          // 'M1/claude' -> {st:'loading'|'ok'|'err', data, msg, timer}
function fetchModels(m, kind) {
  if (!m || (kind !== 'claude' && kind !== 'codex')) return;
  const k = m + '/' + kind; const c = MODELS[k];
  if (c && (c.st === 'ok' || c.st === 'loading')) return;
  if (!send({ op: 'models', m, kind })) { MODELS[k] = { st: 'err', msg: '未连接到 hub' }; return; }
  const e = MODELS[k] = { st: 'loading' };
  e.timer = setTimeout(() => { if (MODELS[k] === e && e.st === 'loading') { e.st = 'err'; e.msg = '超时'; modelsChanged(); } }, 50000);
}
function onModels(r, err) {
  const k = r.m + '/' + r.kind; const e = MODELS[k] || (MODELS[k] = {});
  clearTimeout(e.timer);
  if (err) { e.st = 'err'; e.msg = r.msg; } else { e.st = 'ok'; e.data = { models: r.models || [], efforts: r.efforts || [], modes: r.modes || [] }; }
  modelsChanged();
}
function modelsChanged() {
  if ($('#dlg-new').open && newKind() !== 'shell') fillAdv(newKind());
  if ($('#dlg-ctl').open) renderCtl();
}
function sortEfforts(list) {
  const out = Array.from(new Set(list || []));
  return out.sort((a, b) => { const x = EFFORT_ORDER.indexOf(a); const y = EFFORT_ORDER.indexOf(b); return (x < 0 ? 99 : x) - (y < 0 ? 99 : y); });
}
// merged picker data: fetched lists when available, else state.options. loading = request in flight and nothing fetched yet
function catalog(m, kind) {
  const o = opts(kind); const c = MODELS[m + '/' + kind];
  const d = c && c.st === 'ok' ? c.data : null;
  const models = d && d.models.length ? d.models.map((x) => ({ id: String(x.id), label: x.label || x.id, desc: x.desc || '', efforts: Array.isArray(x.efforts) && x.efforts.length ? x.efforts : null,
    default_effort: x.default_effort || null }))
    : (o.models || []).map((id) => ({ id, label: id, desc: '', efforts: null, default_effort: null }));
  return { loading: !!(c && c.st === 'loading'), failed: !!(c && c.st === 'err'), models,
    efforts: sortEfforts(d && d.efforts.length ? d.efforts : o.efforts), modes: d && d.modes.length ? d.modes : (o.modes || []),
    approvals: o.approvals || [], sandboxes: o.sandboxes || [] };
}
// the catalog entry for a running session's model name ("claude-sonnet-4-5" matches "sonnet", "gpt-5-codex" matches itself)
function findModel(cat, name) {
  if (!name) return null;
  return cat.models.find((x) => x.id === name) || cat.models.find((x) => shortModel(name) === x.id) ||
    cat.models.find((x) => x.id !== 'default' && name.indexOf(x.id) >= 0) || null;
}
function effortsFor(cat, model) { return model && model.efforts ? sortEfforts(model.efforts) : cat.efforts; }
function withDesc(v, desc) { return desc && desc[v] ? v + ' — ' + desc[v] : v; }
function optList(vals, desc, keep, def) {
  return '<option value="">默认' + (def ? ' (' + esc(def) + ')' : '') + '</option>' + (vals || []).map((v) => '<option value="' + esc(v) + '"' + (v === keep ? ' selected' : '') + '>' +
    esc(withDesc(v, desc)) + '</option>').join('');
}
function fillAdv(kind) {
  const ai = kind === 'claude' || kind === 'codex';
  $('#new-adv').hidden = !ai; if (!ai) return;
  const m = $('#new-machine').value;
  fetchModels(m, kind);
  const cat = catalog(m, kind);
  // model: fetched list (label · id), "Other model id" reveals the free-text field
  const sel = $('#new-model-sel'); const keep = sel.value;
  sel.innerHTML = '<option value="">默认（机器预设）</option>' + cat.models.map((x) => '<option value="' + esc(x.id) + '">' +
    esc(x.label !== x.id ? x.label + ' · ' + x.id : x.id) + '</option>').join('') + '<option value="__other">Other model id…</option>';
  sel.value = keep && (keep === '__other' || cat.models.some((x) => x.id === keep)) ? keep : '';
  sel.disabled = cat.loading && !cat.models.length;
  $('#new-model-spin').hidden = !cat.loading;
  $('#new-model-row').hidden = sel.value !== '__other';
  const picked = cat.models.find((x) => x.id === sel.value) || null;
  const nd = $('#new-model-desc');
  nd.hidden = !picked && !cat.failed;
  nd.innerHTML = picked ? '<code>' + esc(picked.id) + '</code>' + (picked.desc ? ' — ' + esc(picked.desc) : '')
    : cat.failed ? '无法读取模型列表，显示的是默认选项' : '';
  // effort: codex shows the selected model's own efforts
  const efforts = kind === 'codex' ? effortsFor(cat, picked) : cat.efforts;
  $('#new-effort').innerHTML = optList(efforts, EFFORT_DESC, $('#new-effort').value, picked && picked.default_effort);
  $('#new-pmode-row').hidden = kind !== 'claude';
  $('#new-approval-row').hidden = kind !== 'codex';
  $('#new-sandbox-row').hidden = kind !== 'codex';
  if (kind === 'claude') $('#new-pmode').innerHTML = optList(cat.modes, MODE_DESC, $('#new-pmode').value);
  else {
    $('#new-approval').innerHTML = optList(cat.approvals, APPROVAL_DESC, $('#new-approval').value);
    $('#new-sandbox').innerHTML = optList(cat.sandboxes, SANDBOX_DESC, $('#new-sandbox').value);
  }
}
$('#new-model-sel').onchange = () => {
  fillAdv(newKind());
  if ($('#new-model-sel').value === '__other') $('#new-model').focus();
};
function openNew(mid) {
  if (mid) LS.setItem('glimpse.newMachine', mid);
  $('#new-err').textContent = ''; newBusy(false);
  const kind = LS.getItem('glimpse.newKind') || 'claude';
  for (const r of $$('#new-form input[name=kind]')) r.checked = r.value === kind;
  $('#new-cwd').dataset.touched = ''; $('#new-name').value = ''; $('#new-args').value = '';
  for (const id of ['#new-model', '#new-model-sel', '#new-effort', '#new-pmode', '#new-approval', '#new-sandbox']) $(id).value = '';
  $('#new-machine').value = '';
  fillNew();
  openSheet($('#dlg-new'));
}
$('#btn-new').onclick = () => openNew();
$('#fab').onclick = () => openNew();
$('#new-machine').onchange = () => { $('#new-cwd').dataset.touched = ''; fillNewDeps(); };
for (const r of $$('#new-form input[name=kind]')) r.onchange = fillNewDeps;
$('#new-cwd').addEventListener('input', () => { $('#new-cwd').dataset.touched = '1'; });
function splitArgs(s) {
  const out = []; const re = /"([^"]*)"|'([^']*)'|(\S+)/g; let m;
  while ((m = re.exec(s))) out.push(m[1] !== undefined ? m[1] : m[2] !== undefined ? m[2] : m[3]);
  return out;
}
function phoneDims() {
  const cols = Math.floor((window.innerWidth - 12) / (S.fontSize * 0.6));
  const rows = Math.floor((window.innerHeight - 230) / (S.fontSize * 1.2));
  return { cols: Math.max(60, Math.min(cols, 120)), rows: Math.max(24, Math.min(rows, 60)) };
}
$('#new-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const m = $('#new-machine').value; const mc = S.byId[m];
  if (!mc || !mc.online) { $('#new-err').textContent = '请选择一台在线的机器'; return; }
  const kind = newKind(); const cwd = $('#new-cwd').value.trim();
  if (!cwd) { $('#new-err').textContent = '请填写工作目录'; $('#new-cwd').focus(); return; }
  LS.setItem('glimpse.newMachine', m); LS.setItem('glimpse.newKind', kind); pushRecent(m, cwd);
  const d = phoneDims();
  $('#new-err').textContent = ''; newBusy(true);
  const msg = { op: 'create', m, kind, cwd, name: $('#new-name').value.trim(), extra: splitArgs($('#new-args').value.trim()),
    key: kind !== 'shell' ? ($('#new-key').value || null) : null, cols: d.cols, rows: d.rows };
  // advanced: the hub turns these into --model/--effort/--permission-mode (claude) or -m/-c/-a/-s (codex)
  if (kind !== 'shell') {
    const ms = $('#new-model-sel').value;
    const model = ms === '__other' ? $('#new-model').value.trim() : ms; const effort = $('#new-effort').value;
    if (model) msg.model = model;
    if (effort) msg.effort = effort;
    if (kind === 'claude' && $('#new-pmode').value) msg.mode = $('#new-pmode').value;
    if (kind === 'codex' && $('#new-approval').value) msg.approval = $('#new-approval').value;
    if (kind === 'codex' && $('#new-sandbox').value) msg.sandbox = $('#new-sandbox').value;
  }
  const ok = send(msg);
  if (!ok) { newBusy(false); $('#new-err').textContent = '未连接到 hub'; }
  setTimeout(() => { if (!$('#new-note').hidden) { newBusy(false); $('#new-err').textContent = '启动超时，请重试'; } }, 25000);
});

// ------------------------------------------------------------------ restart with another key
// key usage (hub polls keys.usage_url; optional): quota windows (e.g. 5 h / 7 d) + today's tokens and cost
function winName(sec) { return !sec ? '' : sec % 86400 === 0 ? (sec / 86400) + ' 天' : sec % 3600 === 0 ? (sec / 3600) + ' 小时' : Math.round(sec / 60) + ' 分钟'; }
function keyUsageText(k) {
  const u = k && k.usage; if (!u) return '';
  if (u.error) return u.error;
  const w = (u.windows || []).map((x) => winName(x.window_sec) + ' 已用 ' + Math.round(x.percent) + '%');
  if (u.today_tokens != null) w.push('今日 ' + fmtNum(u.today_tokens) + ' tokens' + (u.today_cost ? ' · $' + u.today_cost : ''));
  return w.join(' · ');
}
function keyUsageBars(u) {
  if (!u || !u.windows || !u.windows.length) return '';
  return '<div class="kbars">' + u.windows.map((x) => '<span class="kbar"><span class="kb-l">' + esc(winName(x.window_sec)) + '</span>' + meter(x.percent) +
    '<span class="kb-r">' + (x.percent >= 100 ? (x.reset_at ? ago2(x.reset_at) + '重置' : '已用完') : Math.round(100 - x.percent) + '% 剩余') + '</span></span>').join('') + '</div>';
}
function ago2(t) {
  const d = Math.max(0, t - Date.now() / 1000);
  return d < 3600 ? Math.ceil(d / 60) + ' 分钟后' : d < 86400 ? Math.round(d / 3600) + ' 小时后' : Math.round(d / 86400) + ' 天后';
}
const RS = { active: false, m: null, sid: null, timer: null };
function openRestart() {
  const v = S.view; const s = v && session(v.m, v.sid); if (!s) return;
  if ($('#dlg-more').open) closeSheet($('#dlg-more'));
  RS.active = false; RS.m = v.m; RS.sid = v.sid;
  const opts = [{ v: '', l: '保持当前', d: s.key ? '继续使用 ' + s.key : '使用默认配置' }]
    .concat(S.keys.map((k) => ({ v: k.name, l: k.name, d: [k.name === s.key ? '当前使用中' : '', keyUsageText(k)].filter(Boolean).join(' · '),
      u: k.usage })))
    .concat([{ v: '__new', l: '输入新的 Key', d: '临时使用或保存到列表' }]);
  $('#rs-keys').innerHTML = opts.map((o, i) => '<label class="cell radio"><input type="radio" name="rskey" value="' + esc(o.v) + '"' + (i === 0 ? ' checked' : '') + '>' +
    '<span class="grow">' + esc(o.l) + (o.d ? '<div class="sub">' + esc(o.d) + '</div>' : '') + keyUsageBars(o.u) + '</span>' + ic('check', 'chk') + '</label>').join('');
  send({ op: 'key_usage' });                       // refresh the numbers while the sheet is open
  for (const r of $$('#rs-keys input')) r.onchange = () => { $('#rs-new').hidden = rsKey() !== '__new'; if (!$('#rs-new').hidden) $('#rs-value').focus(); };
  $('#rs-new').hidden = true; $('#rs-value').value = ''; $('#rs-save').value = '';
  const rz = $('#rs-resume'); rz.checked = !!s.can_resume; rz.disabled = !s.can_resume;
  $('#rs-resume-sub').textContent = s.can_resume ? '重启后接着之前的上下文继续' : '还没有可恢复的对话（会开始新对话）';
  // a session that reads its key from a file switches keys live: no restart, the conversation goes on
  RS.hot = !!s.hot_key;
  $('#dlg-rs-title').textContent = RS.hot ? '切换 Key' : '更换 Key 并重启';
  $('#rs-ok').textContent = RS.hot ? '切换' : '重启';
  $('#rs-hot-note').hidden = !RS.hot;
  $('#rs-progress').hidden = true; $('#rs-err').textContent = ''; $('#rs-ok').disabled = false;
  setTimeout(() => openSheet($('#dlg-restart')), $('#dlg-more').open ? 230 : 0);
}
$('#act-restart').onclick = openRestart;
function rsKey() { const r = $('#rs-keys input:checked'); return r ? r.value : ''; }
function rsSteps(cur) {
  const steps = ['结束旧进程', '设置新的 Key', '启动并恢复对话'];
  $('#rs-steps').innerHTML = steps.map((t, i) => '<div class="pstep ' + (i < cur ? 'done' : i === cur ? 'cur' : '') + '">' +
    (i < cur ? ic('check-c') : i === cur ? '<span class="spinner" aria-hidden="true"></span>' : '<span class="o"></span>') + t + '</div>').join('');
}
$('#rs-form').addEventListener('submit', (e) => {
  e.preventDefault();
  if (RS.active) return;
  const k = rsKey(); let msg;
  if (k === '__new') {
    const value = $('#rs-value').value.trim();
    if (!value) { $('#rs-err').textContent = '请输入 Key'; $('#rs-value').focus(); return; }
    msg = { op: 'restart', m: RS.m, sid: RS.sid, key: null, new_key: { value, save_as: $('#rs-save').value.trim() || null }, resume: $('#rs-resume').checked };
  } else msg = { op: 'restart', m: RS.m, sid: RS.sid, key: k || null, new_key: null, resume: $('#rs-resume').checked };
  if (!send(msg)) { $('#rs-err').textContent = '未连接到 hub'; return; }
  RS.active = true; $('#rs-err').textContent = ''; $('#rs-ok').disabled = true;
  $('#rs-progress').hidden = false; rsSteps(0);
  setTimeout(() => { if (RS.active) rsSteps(1); }, 600);
  clearTimeout(RS.timer); RS.timer = setTimeout(() => { if (RS.active) rsFail('重启超时'); }, 40000);
});
function rsDone(m) {
  clearTimeout(RS.timer); rsSteps(3); RS.active = false;
  $('#rs-value').value = '';
  const hot = m && m.hot;
  setTimeout(() => {
    closeSheet($('#dlg-restart'));
    toast(hot ? '已切换到 ' + (m.key || '新 Key') : '已重启', hot ? '无需重启，下一次请求开始使用新 Key' : '进程已用新的配置重新启动', null, 'done');
  }, hot ? 200 : 600);
}
function rsFail(msg) { clearTimeout(RS.timer); RS.active = false; $('#rs-ok').disabled = false; $('#rs-progress').hidden = true; $('#rs-err').textContent = msg || '重启失败'; }

// ------------------------------------------------------------------ adopt: resume a conversation started on the computer
// The agent lists recent claude / codex transcripts (op history); picking one starts a Glimpse session with resume_id.
const ADOPT = { busy: false, m: null, kind: '', items: null, loading: false };
function openAdopt() {
  const sel = $('#adopt-machine');
  const prev = $('#new-machine').value || LS.getItem('glimpse.newMachine');
  sel.innerHTML = S.machines.map((m) => '<option value="' + esc(m.id) + '"' + (m.online ? '' : ' disabled') + '>' +
    esc(m.id + ' · ' + m.name + (m.online ? '' : '（离线）')) + '</option>').join('');
  const ok = S.machines.find((m) => m.id === prev && m.online) || S.machines.find((m) => m.online);
  if (ok) sel.value = ok.id;
  ADOPT.kind = ''; ADOPT.busy = false; $('#adopt-err').textContent = '';
  if ($('#dlg-new').open) closeSheet($('#dlg-new'));
  setTimeout(() => { openSheet($('#dlg-adopt')); adoptLoad(); }, $('#dlg-new').open ? 230 : 0);
}
function adoptLoad() {
  ADOPT.m = $('#adopt-machine').value; ADOPT.items = null; ADOPT.loading = true;
  for (const b of $$('#adopt-kind button')) b.setAttribute('aria-checked', String(b.dataset.k === ADOPT.kind));
  $('#adopt-err').textContent = '';
  renderAdopt();
  if (!ADOPT.m) { ADOPT.loading = false; ADOPT.items = []; renderAdopt(); return; }
  if (!send({ op: 'history', m: ADOPT.m, kind: ADOPT.kind || null })) { ADOPT.loading = false; adoptFail('未连接到 hub'); return; }
  clearTimeout(ADOPT.timer);
  ADOPT.timer = setTimeout(() => { if (ADOPT.loading) { ADOPT.loading = false; adoptFail('读取超时，请重试'); } }, 20000);
}
function onHistory(m) {
  if (m.m !== ADOPT.m) return;
  clearTimeout(ADOPT.timer); ADOPT.loading = false;
  ADOPT.items = (m.items || []).filter((it) => !ADOPT.kind || it.kind === ADOPT.kind);
  renderAdopt();
}
function renderAdopt() {
  const host = $('#adopt-list');
  host.setAttribute('aria-busy', String(ADOPT.loading));
  if (ADOPT.loading || ADOPT.items == null) {
    host.innerHTML = '<div class="cell"><span class="spinner" aria-hidden="true"></span><span class="grow sub" style="margin:0">正在读取电脑上的对话记录…</span></div>';
    return;
  }
  if (!ADOPT.items.length) {
    host.innerHTML = '<div class="cell"><span class="grow sub" style="margin:0;white-space:normal">没有找到最近的' + (ADOPT.kind ? ' ' + KIND[ADOPT.kind] + ' ' : '') +
      '对话记录（Claude 读取 ~/.claude/projects，Codex 读取最近 14 天的 ~/.codex/sessions）。</span></div>';
    return;
  }
  host.innerHTML = ADOPT.items.map((it, i) => '<button type="button" class="cell hitem" data-i="' + i + '">' +
    '<span class="grow"><span class="h-r1"><span class="tag ' + esc(it.kind) + '">' + esc(KIND[it.kind] || it.kind) + '</span>' +
    (it.active ? '<span class="pill waiting"><span class="dot waiting" aria-hidden="true"></span>可能仍在运行</span>' : '') +
    '<span class="h-time">' + esc(ago(it.updated)) + '</span></span>' +
    '<span class="h-title">' + esc(it.title || '（无标题）') + '</span>' +
    '<span class="h-cwd">' + ic('folder', 'xs') + '<span>' + esc(it.cwd || '') + '</span></span></span>' +
    '<span class="h-go">' + ic('chev', 'sm') + '</span></button>').join('');
  for (const b of $$('.hitem', host)) b.onclick = () => adoptPick(ADOPT.items[+b.dataset.i], b);
}
async function adoptPick(it, btn) {
  if (!it || ADOPT.busy) return;
  const mc = S.byId[ADOPT.m];
  if (!mc || !mc.online) { adoptFail('机器离线'); return; }
  if (it.active && !(await confirmSheet('这个对话可能还在电脑上运行', '它在 2 分钟内还有写入。请先在电脑上退出原来的 ' + (KIND[it.kind] || it.kind) +
    '，否则两个进程同时写同一段对话会分叉。', '已关闭，继续接管'))) return;
  const d = phoneDims();
  const msg = { op: 'create', m: mc.id, kind: it.kind, cwd: it.cwd || mc.default_cwd || '~', resume_id: it.id, name: '', extra: [], key: null, cols: d.cols, rows: d.rows };
  if (!send(msg)) { adoptFail('未连接到 hub'); return; }
  ADOPT.busy = true; $('#adopt-err').textContent = '';
  for (const b of $$('#adopt-list .hitem')) b.disabled = true;
  const go = $('.h-go', btn); if (go) go.innerHTML = '<span class="spinner" aria-hidden="true"></span>';
  clearTimeout(ADOPT.timer);
  ADOPT.timer = setTimeout(() => { if (ADOPT.busy) adoptFail('启动超时，请重试'); }, 25000);
}
function adoptFail(msg) {
  clearTimeout(ADOPT.timer); ADOPT.busy = false;
  $('#adopt-err').textContent = msg || '失败';
  if (ADOPT.items) renderAdopt(); else { ADOPT.items = []; renderAdopt(); }
}
$('#new-adopt').onclick = openAdopt;
$('#adopt-machine').onchange = adoptLoad;
$('#adopt-refresh').onclick = adoptLoad;
for (const b of $$('#adopt-kind button')) b.onclick = () => { ADOPT.kind = b.dataset.k; adoptLoad(); };
$('#dlg-adopt').addEventListener('close', () => { clearTimeout(ADOPT.timer); ADOPT.busy = false; ADOPT.loading = false; });

// ------------------------------------------------------------------ session controls: model / effort / mode ("模型与模式")
// claude: applied live in the TUI (Shift+Tab for the mode, /model, /effort, /goal); codex: restart with resume + flags
const CTL = { busy: null, m: null, sid: null, timer: null };
function ctlChips(action, vals, cur, desc, dis) {
  return '<div class="fchips wrap" role="radiogroup">' + vals.map((v) => {
    const on = v === cur; const busy = CTL.busy && CTL.busy.action === action && CTL.busy.value === v;
    return '<button type="button" role="radio" aria-checked="' + on + '" data-ca="' + esc(action) + '" data-cv="' + esc(v) + '"' + (dis ? ' disabled' : '') +
      (desc && desc[v] ? ' title="' + esc(desc[v]) + '"' : '') + '>' + (busy ? '<span class="spinner" aria-hidden="true"></span>' : '') + esc(v) + '</button>';
  }).join('') + '</div>';
}
// one-line hint under a chip row: what the current / busy value means
function chipHint(desc, v) { return v && desc[v] ? '<p class="g-f chint"><b>' + esc(v) + '</b> — ' + esc(desc[v]) + '</p>' : ''; }
function ctlModeValue(m) { return m === 'bypassPermissions' ? 'bypass' : m; }   // detected footer name == control value
function skelRows(n) { let h = ''; for (let i = 0; i < n; i++) h += '<div class="cell"><span class="skel" style="height:14px;width:' + (40 + (i * 23) % 45) + '%"></span></div>'; return '<div class="group" aria-busy="true">' + h + '</div>'; }
function renderCtl() {
  const s = session(CTL.m, CTL.sid); const host = $('#ctl-body');
  if (!s) { host.innerHTML = '<p class="g-f">会话已不存在。</p>'; return; }
  const md = sessMode(s); const cat = catalog(CTL.m, s.kind);
  const dead = s.status === 'exited' || s.status === 'error';
  const dis = dead || !!CTL.busy;
  let h = '';
  if (dead) h += '<div class="note-box warn">' + ic('power', 'sm') + '<div>进程已退出，重启会话后才能修改。</div></div>';
  if (cat.failed) h += '<div class="note-box warn">' + ic('alert', 'sm') + '<div>无法读取这台机器上的模型列表，下面是默认选项。</div></div>';
  if (s.kind === 'claude') {
    const modes = [];
    for (const m of cat.modes) { const v = ctlModeValue(m); if (!modes.includes(v)) modes.push(v); }
    const cur = md.mode || 'default'; if (!modes.includes(cur)) modes.push(cur);
    h += '<div class="g-h">Permission mode</div><div class="group">' + modes.map((v) => {
      const busy = CTL.busy && CTL.busy.action === 'mode' && CTL.busy.value === v;
      return '<button type="button" class="cell radio" role="radio" aria-checked="' + (v === cur) + '" data-ca="mode" data-cv="' + esc(v) + '"' + (dis ? ' disabled' : '') + '>' +
        '<span class="grow"><span class="mono">' + esc(modeName(v)) + '</span><div class="sub">' + esc(MODE_DESC[v] || '') + '</div></span>' +
        (busy ? '<span class="spinner" aria-hidden="true"></span>' : v === cur ? ic('check', 'chk on') : '') + '</button>';
    }).join('') + '</div><p class="g-f">通过 Shift+Tab 在终端里切换，当前模式从 Claude Code 底部状态栏识别。部分模式需要在 Claude 设置里先启用才会出现在循环中。</p>';
  } else {
    h += '<div class="note-box">' + ic('refresh', 'sm') + '<div>模型、推理强度和权限直接在当前会话里切换（Codex 的 /model、/permissions，只对这个会话生效），不会重启。只有 Codex 菜单里没有的组合才会重启并恢复对话。请在这一轮结束后再改。</div></div>';
  }
  const curModel = md.model || ''; const curM = findModel(cat, curModel);
  h += '<div class="g-h">Model' + (curModel ? '<span class="g-cur mono">' + esc(shortModel(curModel)) + '</span>' : '') + '</div>';
  if (cat.loading && !MODELS[CTL.m + '/' + s.kind].data) h += skelRows(3);
  else if (cat.models.length) {
    h += '<div class="group">' + cat.models.map((x) => {
      const on = curM === x; const busy = CTL.busy && CTL.busy.action === 'model' && CTL.busy.value === x.id;
      return '<button type="button" class="cell radio" role="radio" aria-checked="' + on + '" data-ca="model" data-cv="' + esc(x.id) + '"' + (dis ? ' disabled' : '') + '>' +
        '<span class="grow">' + esc(x.label) + (x.label !== x.id ? ' <span class="mid-id">' + esc(x.id) + '</span>' : '') +
        (x.desc ? '<div class="sub">' + esc(x.desc) + '</div>' : '') + '</span>' +
        (busy ? '<span class="spinner" aria-hidden="true"></span>' : on ? ic('check', 'chk on') : '') + '</button>';
    }).join('') + '</div>';
  }
  h += '<div class="group" style="margin-top:8px"><div class="cell"><input id="ctl-model" class="mono" style="text-align:left" placeholder="Other model id" autocapitalize="off" autocorrect="off" spellcheck="false" enterkeyhint="done" aria-label="Other model id"' + (dis ? ' disabled' : '') + '>' +
    '<button type="button" class="btn sm" id="ctl-model-go"' + (dis ? ' disabled' : '') + '>应用</button></div></div>';
  // codex: the efforts the current model supports
  const efforts = s.kind === 'codex' ? effortsFor(cat, curM) : cat.efforts;
  h += '<div class="g-h">Reasoning effort' + (md.effort ? '<span class="g-cur mono">' + esc(md.effort) + '</span>' : '') + '</div>' +
    (cat.loading && !efforts.length ? skelRows(1) : ctlChips('effort', efforts, md.effort, EFFORT_DESC, dis)) + chipHint(EFFORT_DESC, md.effort);
  if (s.kind === 'claude') {
    h += '<div class="g-h">目标 /goal</div><div class="group"><div class="cell"><input id="ctl-goal" style="text-align:left" placeholder="例如：让所有测试通过" enterkeyhint="done" aria-label="目标"' + (dis ? ' disabled' : '') + '>' +
      '<button type="button" class="btn sm" id="ctl-goal-go"' + (dis ? ' disabled' : '') + '>设置</button></div>' +
      '<button type="button" class="cell" id="ctl-goal-clear"' + (dis ? ' disabled' : '') + '><span class="grow" style="color:var(--accent-text)">清除目标</span></button></div>';
  } else {
    h += '<div class="g-h">Approval policy' + (md.approval ? '<span class="g-cur mono">' + esc(md.approval) + '</span>' : '') + '</div>' +
      ctlChips('approval', cat.approvals, md.approval, APPROVAL_DESC, dis) + chipHint(APPROVAL_DESC, md.approval) +
      '<div class="g-h">Sandbox' + (md.sandbox ? '<span class="g-cur mono">' + esc(md.sandbox) + '</span>' : '') + '</div>' +
      ctlChips('sandbox', cat.sandboxes, md.sandbox, SANDBOX_DESC, dis) + chipHint(SANDBOX_DESC, md.sandbox);
  }
  h += '<p id="ctl-err" class="sh-err" role="alert"></p>';
  const keep = { model: ($('#ctl-model') || {}).value, goal: ($('#ctl-goal') || {}).value, err: ($('#ctl-err') || {}).textContent };
  host.innerHTML = h;
  if (keep.model) $('#ctl-model').value = keep.model;
  if (keep.goal) $('#ctl-goal').value = keep.goal;
  if (keep.err) $('#ctl-err').textContent = keep.err;
  for (const b of $$('[data-ca]', host)) b.onclick = () => { if (b.getAttribute('aria-checked') !== 'true') ctlSend(b.dataset.ca, b.dataset.cv); };
  $('#ctl-model-go').onclick = () => { const v = $('#ctl-model').value.trim(); if (v) ctlSend('model', v); };
  $('#ctl-model').onkeydown = (e) => { if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); $('#ctl-model-go').click(); } };
  if (s.kind === 'claude') {
    $('#ctl-goal-go').onclick = () => { const v = $('#ctl-goal').value.trim(); if (v) ctlSend('goal', v); };
    $('#ctl-goal').onkeydown = (e) => { if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); $('#ctl-goal-go').click(); } };
    $('#ctl-goal-clear').onclick = () => ctlSend('goal', '');
  }
}
function openCtl() {
  const v = S.view; const s = v && session(v.m, v.sid); if (!s || (s.kind !== 'claude' && s.kind !== 'codex')) return;
  CTL.m = v.m; CTL.sid = v.sid; CTL.busy = null;
  $('#ctl-body').innerHTML = '';
  fetchModels(v.m, s.kind);
  renderCtl();
  openSheet($('#dlg-ctl'));
}
function ctlSend(action, value) {
  const s = session(CTL.m, CTL.sid); if (!s || CTL.busy) return;
  if (!send({ op: 'control', m: CTL.m, sid: CTL.sid, action, value })) { ctlFail('未连接到 hub'); return; }
  CTL.busy = { action, value };
  renderCtl(); $('#ctl-err').textContent = '';
  clearTimeout(CTL.timer);
  CTL.timer = setTimeout(() => { if (CTL.busy) ctlFail('操作超时：请切到终端确认'); }, s.kind === 'codex' ? 45000 : 15000);
}
function ctlDone(m) {
  if (!CTL.busy || m.m !== CTL.m || m.sid !== CTL.sid) return;
  const b = CTL.busy; const s = session(CTL.m, CTL.sid);
  clearTimeout(CTL.timer); CTL.busy = null;
  if (b.action === 'goal') { const g = $('#ctl-goal'); if (g) g.value = ''; }
  if (b.action === 'model') { const g = $('#ctl-model'); if (g) g.value = ''; }
  if ($('#dlg-ctl').open) renderCtl();
  const what = { mode: '模式', model: '模型', effort: '推理强度', goal: '目标', approval: '审批策略', sandbox: '沙箱' }[b.action] || b.action;
  const val = b.action === 'goal' && !b.value ? '已清除' : b.action === 'mode' ? modeName(b.value) : b.value;
  toast(what + (s && s.kind === 'codex' ? '已更新，会话已重启' : '已更新'), val, null, 'done');
}
function ctlFail(msg) {
  clearTimeout(CTL.timer); CTL.busy = null;
  if ($('#dlg-ctl').open) { renderCtl(); $('#ctl-err').textContent = msg || '操作失败'; } else toast('操作失败', msg, null, 'error');
}
$('#dlg-ctl').addEventListener('close', () => { clearTimeout(CTL.timer); CTL.busy = null; });

// ------------------------------------------------------------------ settings
function standalone() { return window.navigator.standalone === true || window.matchMedia('(display-mode: standalone)').matches; }
function isIOS() { return /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1); }

// ---- install as an app: iOS has no install API (show 分享 → 添加到主屏幕); Chrome/Edge/Android fire beforeinstallprompt
const INST = { prompt: null };
window.addEventListener('beforeinstallprompt', (e) => { e.preventDefault(); INST.prompt = e; renderInstall(); });
window.addEventListener('appinstalled', () => { INST.prompt = null; LS.setItem('glimpse.installHint', 'done'); renderInstall(); });
function renderInstall() {
  const b = $('#install');
  const show = !standalone() && LS.getItem('glimpse.installHint') == null && (isIOS() || !!INST.prompt);
  b.hidden = !show; if (!show) return;
  setHTML(b, '<img src="/static/icon.svg?v=4" alt="" width="40" height="40"><div class="grow"><b>把 Glimpse 装成 App</b><span>' +
    (isIOS() ? '点 Safari 的 ' + ic('share', 'xs') + ' 分享 → 添加到主屏幕，全屏打开并接收通知' : '全屏打开、独立图标，并可接收推送通知') + '</span></div>' +
    (INST.prompt ? '<button type="button" class="btn sm primary" data-i="go">安装</button>' : '<button type="button" class="btn sm" data-i="how">怎么做</button>') +
    '<button type="button" class="icon-btn plain x" data-i="x" aria-label="不再提示">' + ic('x', 'sm') + '</button>');
}
$('#install').addEventListener('click', (e) => {
  const t = e.target.closest('[data-i]'); if (!t) return;
  if (t.dataset.i === 'x') { LS.setItem('glimpse.installHint', 'dismissed'); renderInstall(); }
  else if (t.dataset.i === 'go') installNow();
  else openInstall();
});
async function installNow() {
  const p = INST.prompt; if (!p) { openInstall(); return; }
  INST.prompt = null;
  try { p.prompt(); const r = await p.userChoice; if (r && r.outcome === 'accepted') LS.setItem('glimpse.installHint', 'done'); } catch (_) {}
  renderInstall(); closeSheet($('#dlg-install'));
}
function openInstall() {
  const ios = isIOS();
  $('#inst-ios').hidden = !ios; $('#inst-other').hidden = ios;
  $('#inst-go').hidden = !INST.prompt;
  const go = () => openSheet($('#dlg-install'));
  if ($('#dlg-settings').open) { closeSheet($('#dlg-settings')); setTimeout(go, 230); } else go();
}
$('#inst-go').onclick = installNow;
$('#btn-install').onclick = () => { if (standalone()) return; openInstall(); };
async function pushState() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
    const ios = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    if (ios && !standalone()) return '先在 Safari 点“分享 → 添加到主屏幕”，再从主屏幕图标打开';
    return '这个浏览器不支持推送通知（应用内横幅仍然可用）';
  }
  try {
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (window.Notification && Notification.permission === 'denied') return '通知权限被拒绝：请在系统或浏览器的通知设置里允许 Glimpse';
    return sub ? '已开启 ✓' : '未开启';
  } catch (_) { return '未开启'; }
}
function renderSettingsWidgets() {
  const p = widgetPrefs(); const host = $('#set-widgets');
  host.innerHTML = p.order.map((id, i) => {
    const w = WIDGET_BY_ID[id];
    return '<div class="cell"><span class="cell-ic" aria-hidden="true">' + ic(w.icon) + '</span><span class="grow">' + esc(w.title) + '</span>' +
      '<button type="button" class="icon-btn plain" data-up="' + i + '" aria-label="上移 ' + esc(w.title) + '"' + (i === 0 ? ' disabled' : '') + '>' + ic('up', 'sm') + '</button>' +
      '<button type="button" class="icon-btn plain" data-dn="' + i + '" aria-label="下移 ' + esc(w.title) + '"' + (i === p.order.length - 1 ? ' disabled' : '') + '>' + ic('down', 'sm') + '</button>' +
      '<span class="switch"><input type="checkbox" role="switch" data-w="' + esc(id) + '" aria-label="显示 ' + esc(w.title) + '"' + (p.on[id] ? ' checked' : '') + '><i></i></span></div>';
  }).join('');
  const apply = () => { saveWidgetPrefs(p); renderSettingsWidgets(); renderHomeWidgets(); if (S.view) renderViewWidgets(); };
  for (const b of $$('[data-up]', host)) b.onclick = () => { const i = +b.dataset.up; [p.order[i - 1], p.order[i]] = [p.order[i], p.order[i - 1]]; apply(); };
  for (const b of $$('[data-dn]', host)) b.onclick = () => { const i = +b.dataset.dn; [p.order[i + 1], p.order[i]] = [p.order[i], p.order[i + 1]]; apply(); };
  for (const c of $$('[data-w]', host)) c.onchange = () => { p.on[c.dataset.w] = c.checked; apply(); };
}
// quick-key bar editor: every built-in + custom key, on/off + order; custom keys can be added / deleted
function renderSettingsKeys() {
  const host = $('#set-keys');
  const on = keybar();
  const all = on.concat(KEY_CATALOG.map((k) => k.id).concat(customKeys().map((k) => k.id)).filter((id) => !on.includes(id)));
  host.innerHTML = all.filter(keyDef).map((id) => {
    const k = keyDef(id); const i = on.indexOf(id); const custom = id.startsWith('c:');
    return '<div class="cell"><span class="kchip">' + esc(k.label) + '</span><span class="grow">' + esc(k.aria || k.label) +
      (custom ? '<div class="sub mono">' + esc(k.send) + '</div>' : '') + '</span>' +
      (i >= 0 ? '<button type="button" class="icon-btn plain" data-ku="' + esc(id) + '" aria-label="上移"' + (i === 0 ? ' disabled' : '') + '>' + ic('up', 'sm') + '</button>' +
        '<button type="button" class="icon-btn plain" data-kd="' + esc(id) + '" aria-label="下移"' + (i === on.length - 1 ? ' disabled' : '') + '>' + ic('down', 'sm') + '</button>' : '') +
      (custom ? '<button type="button" class="icon-btn plain" data-kx="' + esc(id) + '" aria-label="删除 ' + esc(k.label) + '">' + ic('trash', 'sm') + '</button>' : '') +
      '<span class="switch"><input type="checkbox" role="switch" data-kk="' + esc(id) + '" aria-label="显示 ' + esc(k.label) + '"' + (i >= 0 ? ' checked' : '') + '><i></i></span></div>';
  }).join('');
  const save = (ids) => { LS.setItem('glimpse.keybar', JSON.stringify(ids)); renderKeybar(); renderSettingsKeys(); };
  const move = (id, d) => { const ids = keybar(); const i = ids.indexOf(id); const j = i + d; if (i < 0 || j < 0 || j >= ids.length) return; [ids[i], ids[j]] = [ids[j], ids[i]]; save(ids); };
  for (const b of $$('[data-ku]', host)) b.onclick = () => move(b.dataset.ku, -1);
  for (const b of $$('[data-kd]', host)) b.onclick = () => move(b.dataset.kd, 1);
  for (const c of $$('[data-kk]', host)) c.onchange = () => { const ids = keybar().filter((x) => x !== c.dataset.kk); if (c.checked) ids.push(c.dataset.kk); save(ids); };
  for (const b of $$('[data-kx]', host)) b.onclick = () => {
    LS.setItem('glimpse.customKeys', JSON.stringify(customKeys().filter((k) => k.id !== b.dataset.kx)));
    save(keybar().filter((x) => x !== b.dataset.kx));
  };
}
$('#key-add').onclick = () => {
  const label = $('#key-label').value.trim(); const sendv = $('#key-send').value;
  if (!label || !sendv) { $('#key-err').textContent = '名称和发送内容都要填'; return; }
  if (label.length > 8) { $('#key-err').textContent = '名称最多 8 个字'; return; }
  const ks = customKeys(); const id = 'c:' + Date.now().toString(36);
  ks.push({ id, label, send: sendv.slice(0, 500) });
  LS.setItem('glimpse.customKeys', JSON.stringify(ks));
  LS.setItem('glimpse.keybar', JSON.stringify(keybar().concat([id])));
  $('#key-label').value = ''; $('#key-send').value = ''; $('#key-err').textContent = '';
  renderKeybar(); renderSettingsKeys();
};
$('#key-reset').onclick = () => { LS.removeItem('glimpse.keybar'); renderKeybar(); renderSettingsKeys(); };

function renderSettingsMachines() {
  const host = $('#set-machines');
  setHTML(host, S.machines.map((m) => '<div class="cell"><span class="cell-ic" aria-hidden="true">' + ic(m.type === 'ssh' ? 'server' : 'laptop') + '</span>' +
    '<span class="grow"><b>' + esc(m.id) + '</b> ' + esc(m.name) + '<div class="sub">' + (m.online ? '在线' + (m.version ? ' · agent ' + esc(m.version) : '') +
      (m.stats ? ' · ' + fmtMs(m.stats.rtt_ms) : '') : '<span style="color:var(--c-err)">' + esc(m.error || '离线') + '</span>') + '</div></span>' +
    (m.online ? '' : '<button type="button" class="btn sm" data-rc="' + esc(m.id) + '">重连</button>') +
    (m.type === 'ssh' ? '<button type="button" class="icon-btn plain" data-ed="' + esc(m.id) + '" aria-label="编辑 ' + esc(m.name) + '">' + ic('pencil', 'sm') + '</button>' : '') +
    '</div>').join('') || '<div class="cell"><span class="grow sub">没有机器</span></div>');
  for (const b of $$('[data-rc]', host)) b.onclick = () => { send({ op: 'reconnect', m: b.dataset.rc }); toast('正在重连', b.dataset.rc); };
  for (const b of $$('[data-ed]', host)) b.onclick = () => openMachine(b.dataset.ed);
  const n = allSessions().filter(([, x]) => x.status === 'exited' || x.status === 'error').length;
  $('#btn-clean').disabled = !n;
  $('#btn-clean-sub').textContent = n ? n + ' 个已退出或出错的会话' : '没有已结束的会话';
}

// ---- machines: add / edit / remove (hub ops machine_add / machine_update / machine_remove / machine_test)
const MC = { busy: false, m: null, timer: null, testing: false };
const HOST_RE = /^[A-Za-z0-9_.@:-]+$/;
function openMachine(mid) {
  const mc = mid ? S.byId[mid] : null;
  MC.m = mc ? mc.id : null; MC.busy = false; MC.testing = false;
  $('#dlg-mc-title').textContent = mc ? '编辑 ' + mc.id : '添加机器';
  $('#mc-name').value = mc ? mc.name : ''; $('#mc-host').value = mc ? mc.host || '' : '';
  $('#mc-port').value = mc && mc.port ? mc.port : ''; $('#mc-cwd').value = mc ? mc.default_cwd || '' : '';
  $('#mc-del-g').hidden = !mc || mc.type !== 'ssh';
  $('#mc-err').textContent = ''; $('#mc-ok').disabled = false;
  $('#mc-test-sub').textContent = '检查 SSH 免密登录和远端 Python'; $('#mc-test-sub').className = 'sub';
  $('#mc-test').disabled = false;
  const go = () => { openSheet($('#dlg-machine')); if (!mc) $('#mc-name').focus(); };
  if ($('#dlg-settings').open) { closeSheet($('#dlg-settings')); setTimeout(go, 230); } else go();
}
function mcHost() {
  const h = $('#mc-host').value.trim();
  if (!h) return [null, '请填写 SSH 主机'];
  if (!HOST_RE.test(h) || h[0] === '-') return [null, '主机只能包含字母、数字和 _ . @ : -，不能以 - 开头'];
  return [h, null];
}
function mcPort() { const v = $('#mc-port').value.trim(); if (!v) return [null, null]; const n = parseInt(v, 10); return n >= 1 && n <= 65535 ? [n, null] : [null, '端口应在 1–65535 之间']; }
$('#mc-test').onclick = () => {
  const [host, e1] = mcHost(); const [port, e2] = mcPort();
  if (e1 || e2) { $('#mc-err').textContent = e1 || e2; return; }
  const msg = { op: 'machine_test', host }; if (port) msg.port = port;
  if (!send(msg)) { $('#mc-err').textContent = '未连接到 hub'; return; }
  MC.testing = true; $('#mc-test').disabled = true; $('#mc-err').textContent = '';
  const sub = $('#mc-test-sub'); sub.className = 'sub'; sub.innerHTML = '<span class="spinner" style="width:12px;height:12px;display:inline-block;vertical-align:-1px;margin-right:6px" aria-hidden="true"></span>正在连接 ' + esc(host) + '…';
  clearTimeout(MC.timer); MC.timer = setTimeout(() => { if (MC.testing) mcTested({ ok: false, msg: '超时：hub 没有回应' }); }, 20000);
};
function mcTested(m) {
  if (!MC.testing) return;
  clearTimeout(MC.timer); MC.testing = false; $('#mc-test').disabled = false;
  const sub = $('#mc-test-sub');
  sub.className = 'sub ' + (m.ok ? 'ok' : 'bad');
  sub.textContent = (m.ok ? '✓ ' : '✕ ') + (m.msg || (m.ok ? '连接成功' : '连接失败'));
}
$('#mc-form').addEventListener('submit', (e) => {
  e.preventDefault();
  if (MC.busy) return;
  const name = $('#mc-name').value.trim();
  if (!name) { $('#mc-err').textContent = '请填写名称'; $('#mc-name').focus(); return; }
  const [host, e1] = mcHost(); const [port, e2] = mcPort();
  if (e1 || e2) { $('#mc-err').textContent = e1 || e2; return; }
  const msg = MC.m ? { op: 'machine_update', m: MC.m, name, host, port, default_cwd: $('#mc-cwd').value.trim() || '~' }
    : { op: 'machine_add', name, host, default_cwd: $('#mc-cwd').value.trim() || '~' };
  if (!MC.m && port) msg.port = port;
  if (!send(msg)) { $('#mc-err').textContent = '未连接到 hub'; return; }
  MC.busy = true; $('#mc-ok').disabled = true; $('#mc-err').textContent = '';
  clearTimeout(MC.timer); MC.timer = setTimeout(() => { if (MC.busy) mcFail('保存超时'); }, 15000);
});
$('#mc-del').onclick = async () => {
  const mc = S.byId[MC.m]; if (!mc) return;
  closeSheet($('#dlg-machine'));
  await new Promise((r) => setTimeout(r, 230));
  if (!(await confirmSheet('删除机器 ' + mc.id + '？', '“' + mc.name + '”会从 hub 配置中移除，它上面正在运行的会话不会被结束，但手机上将看不到。', '删除机器'))) return;
  MC.busy = true;
  if (!send({ op: 'machine_remove', m: mc.id })) { MC.busy = false; toast('未连接', '稍后再试', null, 'error'); }
};
function mcSaved() {
  if (!MC.busy) return;
  clearTimeout(MC.timer); MC.busy = false;
  const wasOpen = $('#dlg-machine').open;
  if (wasOpen) closeSheet($('#dlg-machine'));
  toast(MC.m && !wasOpen ? '已删除机器' : MC.m ? '已保存' : '已添加机器', MC.m ? null : '正在连接，稍后会出现在首页', null, 'done');
}
function mcFail(msg) { clearTimeout(MC.timer); MC.busy = false; $('#mc-ok').disabled = false; $('#mc-err').textContent = msg || '保存失败'; }
$('#btn-add-machine').onclick = () => openMachine(null);
$('#btn-clean').onclick = async () => {
  const dead = allSessions().filter(([, x]) => x.status === 'exited' || x.status === 'error');
  if (!dead.length) return;
  closeSheet($('#dlg-settings'));
  await new Promise((r) => setTimeout(r, 230));
  if (!(await confirmSheet('清理已结束的会话？', '将删除 ' + dead.length + ' 个已退出或出错的会话（对话记录文件仍保留在电脑上）。', '全部清理'))) return;
  const ms = new Set(dead.map(([mc]) => mc.id));
  let ok = true; for (const m of ms) ok = send({ op: 'clean_exited', m }) && ok;
  if (ok) toast('已清理', dead.length + ' 个会话', null, 'done'); else toast('未连接', '稍后再试', null, 'error');
};
function renderThemeSeg() {
  const t = themePref();
  for (const b of $$('#opt-theme [data-theme-v]')) b.setAttribute('aria-checked', String(b.dataset.themeV === t));
}
for (const b of $$('#opt-theme [data-theme-v]')) {
  b.onclick = () => {
    const t = b.dataset.themeV;
    LS.setItem('glimpse.theme', t);
    applyTheme(t); renderThemeSeg();
  };
}
async function renderSettings() {
  renderThemeSeg();
  $('#opt-hide-exited').checked = S.hideExited;
  $('#opt-font').textContent = String(S.fontSize);
  $('#set-ver').textContent = APP_VERSION;
  $('#set-hubver').textContent = S.hub && S.hub.version ? S.hub.version : '—';
  renderSettingsWidgets(); renderSettingsKeys(); renderSettingsMachines(); renderSettingsMemory(); renderKeyPage(); renderAccount();
  $('#btn-install-sub').textContent = standalone() ? '已作为 App 运行 ✓' : isIOS() ? 'Safari：分享 → 添加到主屏幕' : INST.prompt ? '可以直接安装' : '查看安装方法';
  renderSettingsNav();
  $('#set-push').textContent = await pushState();
  const ps = $('#set-push').textContent;
  $('#nav-push-sub').textContent = /已开启/.test(ps) ? '已开启' : /拒绝/.test(ps) ? '权限被拒绝' : /主屏幕/.test(ps) ? '需从主屏幕图标打开' : /不支持/.test(ps) ? '此浏览器不支持' : ps;
}
// ---- settings sub-pages: the main list links to pages that slide in from the right
const THEME_NAMES = { auto: '跟随系统', light: '浅色', dark: '深色' };
function renderSettingsNav() {
  $('#nav-look-sub').textContent = THEME_NAMES[themePref()] + ' · 终端字号 ' + S.fontSize;
  const w = $$('#set-widgets input[type=checkbox]'); const won = w.filter((x) => x.checked).length;
  $('#nav-widgets-sub').textContent = w.length ? '已开启 ' + won + ' / ' + w.length : '';
  $('#nav-keys-sub').textContent = '按键栏 ' + keybar().length + ' 个键';
  $('#nav-machines-sub').textContent = S.machines.map((mc) => mc.id + (mc.online ? '' : ' 离线')).join(' · ');
  $('#nav-acct-sub').textContent = $('#acct-pw-sub').textContent === '已设置' ? '已设置密码' : '登录设备与密码';
  $('#nav-keys2-sub').textContent = (S.keys || []).length + ' 个 · 均衡：' + KB_NAMES[kbState().mode];
}
// ---- Key page: optional balancing (keys.balance) + which keys take part
const KB_NAMES = { off: '关闭', on_exhausted: '没余额时自动换', least_used: '按用量分配' };
function kbState() { return S.balance || { mode: 'off', exclude: [], usage: false }; }
function renderKeyPage() {
  const b = kbState();
  for (const el of $$('#kb-modes [data-kb]')) {
    const on = el.dataset.kb === b.mode;
    el.setAttribute('aria-checked', String(on));
    const c = $('.chk', el); if (c) c.remove();
    if (on) el.insertAdjacentHTML('beforeend', ic('check', 'chk on'));
  }
  $('#kb-note').textContent = b.mode === 'least_used' && !b.usage
    ? '没有配置 keys.usage_url，看不到各 Key 的用量：会按每个 Key 上的会话数平均分配。' : '';
  const keys = S.keys || [];
  setHTML($('#kb-keys'), keys.length ? keys.map((k) => {
    const inPool = !b.exclude.includes(k.name);
    const ex = k.exhausted ? '<span class="tag" style="color:var(--c-err)">' + esc(ago(k.exhausted)) + '不可用</span> ' : '';
    return '<label class="cell"><span class="grow">' + esc(k.name) + ' ' + ex + '<div class="sub">' + esc(keyUsageText(k) || '暂无用量数据') + '</div>' + keyUsageBars(k.usage) + '</span>' +
      '<span class="switch"><input type="checkbox" role="switch" data-kbk="' + esc(k.name) + '"' + (inPool ? ' checked' : '') + ' aria-label="' + esc(k.name) + ' 参与均衡"><i></i></span></label>';
  }).join('') : '<div class="cell"><span class="grow sub">config.json 里还没有 Key</span></div>');
  for (const el of $$('#kb-keys [data-kbk]')) el.onchange = () => {
    const cur = kbState(); const ex = new Set(cur.exclude);
    if (el.checked) ex.delete(el.dataset.kbk); else ex.add(el.dataset.kbk);
    send({ op: 'key_balance', mode: cur.mode, exclude: [...ex] });
  };
}
for (const el of $$('#kb-modes [data-kb]')) el.onclick = () => send({ op: 'key_balance', mode: el.dataset.kb, exclude: kbState().exclude });
function settingsPage(name, back) {
  const d = $('#dlg-settings'); const cur = $('.spage:not([hidden])', d); const next = $('.spage[data-page="' + name + '"]', d);
  if (!next || cur === next) return;
  if (cur) cur.hidden = true;
  next.hidden = false;
  next.classList.remove('in', 'back');
  if (!reduceMotion()) { void next.offsetWidth; next.classList.add(back ? 'back' : 'in'); }
  $('#dlg-set-title').textContent = next.dataset.title;
  $('#set-back').style.visibility = name === 'main' ? 'hidden' : '';
  d.querySelector('.sh-body').scrollTop = 0;
  if (name === 'main') { renderSettingsNav(); const b = $('[data-spage="' + (cur && cur.dataset.page) + '"]', d); if (b) b.focus({ preventScroll: true }); }
  else $('#set-back').focus({ preventScroll: true, focusVisible: false });
}
for (const b of $$('#dlg-settings [data-spage]')) b.onclick = () => settingsPage(b.dataset.spage);
$('#set-back').onclick = () => settingsPage('main', true);
{
  // swipe right on a sub-page goes back, like iOS
  const body = $('#dlg-settings .sh-body'); let x0 = null; let y0 = 0;
  body.addEventListener('touchstart', (e) => { const t = e.touches[0]; x0 = t.clientX < 40 || !$('#dlg-settings .spage[data-page="main"]').hidden ? null : t.clientX; y0 = t.clientY; }, { passive: true });
  body.addEventListener('touchend', (e) => {
    if (x0 == null) return; const t = e.changedTouches[0];
    if (t.clientX - x0 > 80 && Math.abs(t.clientY - y0) < 50) settingsPage('main', true);
    x0 = null;
  }, { passive: true });
}
// ---- account: password state + per-device sessions (GET /api/devices, POST /api/devices/revoke)
async function renderAccount() {
  let r;
  try { r = await api('/api/devices'); } catch (e) { $('#acct-pw-sub').textContent = '无法读取'; return; }
  $('#acct-pw-sub').textContent = r.has_password ? '已设置' : '未设置（只能用一次性链接登录），在电脑上运行 gli password';
  const host = $('#acct-devices');
  host.innerHTML = r.devices.map((d) => '<div class="cell"><span class="cell-ic" style="background:#5C636E" aria-hidden="true">' + ic(/iPhone|Android|iPad/.test(d.name) ? 'phone' : 'laptop') + '</span>' +
    '<span class="grow">' + esc(d.name) + (d.current ? ' <span class="tag">本机</span>' : '') +
    '<div class="sub">最近 ' + esc(ago(d.last_seen)) + (d.ip ? ' · ' + esc(d.ip) : '') + '</div></span>' +
    (d.current ? '' : '<button type="button" class="btn sm danger" data-revoke="' + esc(d.id) + '">退出</button>') + '</div>').join('');
  for (const b of $$('[data-revoke]', host)) b.onclick = async () => {
    if (!(await confirmSheet('退出这台设备？', '它需要重新登录才能再次访问。', '退出'))) return;
    try { await api('/api/devices/revoke', { id: b.dataset.revoke }); toast('已退出', '', null, 'done'); renderAccount(); } catch (e) { toast('操作失败', e.message, null, 'error'); }
  };
  $('#acct-revoke-others').hidden = r.devices.filter((d) => !d.current).length === 0;
}
// ---- memory (optional AMem): per-machine status in settings, a sheet with projects / memories / search / compaction
function fmtBytes(b) {
  b = Number(b) || 0;
  const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return (i ? b.toFixed(b < 10 ? 2 : b < 100 ? 1 : 0) : String(Math.round(b))) + ' ' + u[i];
}
const MEM = { m: null, view: 'home', project: null, data: {}, busy: false, q: '' };
function memMachines() { return S.machines.filter((mc) => mc.memory && mc.memory.enabled); }
function renderSettingsMemory() {
  const list = memMachines();
  $('#nav-mem').hidden = !list.length;
  const tot = list.map((mc) => MEM.data[mc.id + ':overview']).filter(Boolean).reduce((a, ov) => a + ov.size.total, 0);
  $('#nav-mem-sub').textContent = list.length ? (tot ? fmtBytes(tot) + ' · ' : '') + list.map((mc) => mc.id).join(' · ') : '';
  setHTML($('#set-mem'), list.map((mc) => {
    const st = mc.memory; const ov = MEM.data[mc.id + ':overview'];
    const sub = !mc.online ? '机器离线' : st.available ? (ov ? fmtBytes(ov.size.total) + (ov.budget ? ' / ' + fmtBytes(ov.budget.max_bytes) : '') + ' · ' + ov.projects.length + ' 个项目' : 'AMem ' + esc(st.version || '')) : esc(st.error || '不可用');
    return '<button type="button" class="cell" data-mem="' + esc(mc.id) + '"><span class="cell-ic" style="background:#8E5CF7" aria-hidden="true">' + ic('sparkle') + '</span>' +
      '<span class="grow">' + esc(mc.id) + ' · ' + esc(mc.name) + '<div class="sub">' + sub + '</div>' + (ov ? memMeter(ov) : '') + '</span>' +
      '<svg class="ic chev" aria-hidden="true"><use href="#i-chev"/></svg></button>';
  }).join(''));
  for (const b of $$('[data-mem]', $('#set-mem'))) b.onclick = () => openMemory(b.dataset.mem);
  for (const mc of list) if (mc.online && mc.memory.available && !MEM.data[mc.id + ':overview']) memAsk(mc.id, 'overview');
}
function memMeter(ov) {
  if (!ov.budget) return '';
  const pct = Math.min(100, (ov.size.total / ov.budget.max_bytes) * 100);
  return '<div class="kbars"><span class="kbar"><span class="kb-l">存储</span>' + meter(pct) + '<span class="kb-r">' + (pct < 1 ? '<1' : Math.round(pct)) + '%</span></span></div>';
}
function memAsk(m, action, extra) {
  send(Object.assign({ op: 'memory', m, action }, extra || {}));
}
function onMemory(m) {
  const key = m.m + ':' + m.action + (m.project ? ':' + m.project : '');
  MEM.data[key] = m.data;
  if (m.action === 'overview') MEM.data[m.m + ':overview'] = m.data;
  if (m.action === 'compact' || m.action === 'budget') {
    MEM.busy = false;
    if (m.action === 'compact') {
      const r = m.data || {}; const n = Object.values(r.removed || {}).reduce((a, b) => a + b, 0);
      toast(r.dry_run ? '预估清理' : n ? '已清理' : '无需清理', n ? n + ' 条记录 · ' + fmtBytes(r.before.total) + ' → ' + fmtBytes(r.after.total) : '当前用量在上限之内', null, 'done');
    } else toast('已更新存储上限', '', null, 'done');
    memAsk(m.m, 'overview');
  }
  if ($('#dlg-settings').open) renderSettingsMemory();
  if ($('#dlg-mem').open && MEM.m === m.m) renderMemory();
}
function openMemory(m) {
  MEM.m = m; MEM.view = 'home'; MEM.project = null;
  memAsk(m, 'overview');
  renderMemory();
  openSheet($('#dlg-mem'));
}
const KIND_NAMES = { message: '对话', tool_call: '工具调用', tool_result: '工具输出', document: '文档', memory: '记忆' };
function renderMemory() {
  const body = $('#mem-body'); const mc = S.byId[MEM.m];
  $('#mem-back').style.visibility = MEM.view === 'home' ? 'hidden' : '';   // keep the title centred
  if (!mc || !mc.memory) { setHTML(body, '<p class="g-f">这台机器没有开启记忆功能。</p>'); return; }
  const ov = MEM.data[MEM.m + ':overview'];
  if (MEM.view === 'home') {
    $('#dlg-mem-title').textContent = '记忆 · ' + mc.id;
    if (!ov) { setHTML(body, '<div class="empty">' + (mc.memory.available ? '读取中…' : esc(mc.memory.error || '不可用')) + '</div>'); return; }
    const last = ov.last_compact;
    let h = '<div class="g-h">存储</div><div class="group">' +
      '<div class="cell stack"><span class="grow">已用 ' + fmtBytes(ov.size.total) + (ov.budget ? ' / 上限 ' + fmtBytes(ov.budget.max_bytes) : '（未设上限）') +
      '<div class="sub">记录 ' + fmtBytes(ov.size.objects) + ' · 索引 ' + fmtBytes(ov.size.index) + (last ? ' · 上次清理 ' + esc(ago(Date.parse(last.utc) / 1000)) : '') + '</div>' + memMeter(ov) + '</span></div>' +
      '<div class="cell"><span class="grow" id="mem-budget-lbl">存储上限</span><input id="mem-budget" class="mono" style="width:90px;text-align:right" value="' + esc(ov.budget ? fmtBytes(ov.budget.max_bytes).replace(/\.0+ /, ' ').replace(' ', '') : '') + '" placeholder="20G" aria-labelledby="mem-budget-lbl" autocapitalize="off" autocorrect="off" spellcheck="false">' +
      '<button type="button" class="btn sm" id="mem-budget-save">保存</button></div>' +
      '<button type="button" class="cell" id="mem-compact"><span class="cell-ic" style="background:#5C636E" aria-hidden="true">' + ic('broom') + '</span><span class="grow">立即整理<div class="sub">超过上限时清理最旧的工具输出和工具调用；记忆和对话不会被删</div></span></button>' +
      '</div><div class="g-h">项目</div><div class="group">';
    h += ov.projects.length ? ov.projects.map((p) => '<button type="button" class="cell" data-proj="' + esc(p.project) + '"><span class="grow">' + esc(p.project) +
      '<div class="sub">' + p.memories + ' 条记忆 · ' + fmtNum(p.records) + ' 条记录 · ' + fmtBytes(p.object_bytes) + (p.last ? ' · ' + esc(ago(Date.parse(p.last) / 1000)) : '') + '</div></span>' +
      '<svg class="ic chev" aria-hidden="true"><use href="#i-chev"/></svg></button>').join('') : '<div class="cell"><span class="grow sub">还没有记忆。开启后新建的 Claude Code / Codex 会话会自动记录。</span></div>';
    h += '</div>';
    setHTML(body, h);
    for (const b of $$('[data-proj]', body)) b.onclick = () => memOpenProject(b.dataset.proj);
    $('#mem-budget-save').onclick = () => {
      const v = $('#mem-budget').value.trim();
      if (!/^(0|\d+(\.\d+)?\s*[KMGT]i?B?)$/i.test(v)) { toast('格式不对', '例如 20G、500M，0 表示不设上限', null, 'error'); return; }
      MEM.busy = true; memAsk(MEM.m, 'budget', { budget: v });
    };
    $('#mem-compact').onclick = async () => {
      if (MEM.busy) return;
      if (!(await confirmSheet('立即整理记忆？', '只在超过上限时删除最旧的工具输出和工具调用。整理过的记忆、对话和最近 ' + ((mc.memory.keep_days) || 30) + ' 天的记录都会保留。', '整理'))) return;
      MEM.busy = true; memAsk(MEM.m, 'compact'); toast('整理中…', '', null, 'info');
    };
    return;
  }
  const p = MEM.project; const proj = ov && ov.projects.find((x) => x.project === p);
  $('#dlg-mem-title').textContent = p;
  const mems = MEM.data[MEM.m + ':memories:' + p];
  const hits = MEM.data[MEM.m + ':search:' + p];
  let h = '<div class="group"><div class="cell"><svg class="ic sm" aria-hidden="true"><use href="#i-search"/></svg>' +
    '<input id="mem-q" type="search" class="grow mem-q" placeholder="搜索这个项目的历史（命令、输出、对话）" value="' + esc(MEM.q) + '" enterkeyhint="search" autocapitalize="off" autocorrect="off" spellcheck="false" aria-label="搜索记忆"></div></div>';
  if (proj) h += '<p class="g-f">' + Object.entries(proj.kinds).map(([k, n]) => esc(KIND_NAMES[k] || k) + ' ' + fmtNum(n)).join(' · ') + (proj.pruned ? ' · 已清理 ' + fmtNum(proj.pruned) : '') + '</p>';
  if (hits && MEM.q) {
    h += '<div class="g-h">搜索结果</div><div class="group">' + (hits.length ? hits.map((r) => '<div class="cell stack"><span class="grow"><span class="tag">' + esc(KIND_NAMES[r.kind] || r.kind) + '</span> <span class="sub">' + esc(ago(Date.parse(r.timestamp) / 1000)) + ' · ' + esc(r.source) + '</span>' +
      '<div class="mem-text">' + esc(r.text) + '</div></span></div>').join('') : '<div class="cell"><span class="grow sub">没有找到</span></div>') + '</div>';
  }
  h += '<div class="g-h">已保存的记忆</div><div class="group">';
  h += !mems ? '<div class="cell"><span class="grow sub">读取中…</span></div>' : mems.length ? mems.map((r) => '<div class="cell stack"><span class="grow"><b>' + esc(r.memory_key) + '</b>' + (r.conflict ? ' <span class="tag">冲突</span>' : '') +
    ' <span class="sub">' + esc(ago(Date.parse(r.timestamp) / 1000)) + '</span><div class="mem-text md">' + md(r.text) + '</div></span></div>').join('') :
    '<div class="cell"><span class="grow sub">还没有保存的结论。助手确认重要结论后会自动保存；完整历史已在后台记录，可以在上面搜索。</span></div>';
  h += '</div>';
  setHTML(body, h);
  const q = $('#mem-q');
  q.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); memSearch(q.value); } };
  q.onsearch = () => memSearch(q.value);
}
function memSearch(v) {
  MEM.q = v.trim().slice(0, 200);
  if (MEM.q) memAsk(MEM.m, 'search', { project: MEM.project, q: MEM.q });
  else renderMemory();
}
function memOpenProject(p) {
  MEM.view = 'project'; MEM.project = p; MEM.q = '';
  delete MEM.data[MEM.m + ':search:' + p];
  memAsk(MEM.m, 'memories', { project: p });
  renderMemory();
}
$('#mem-back').onclick = () => { MEM.view = 'home'; MEM.project = null; renderMemory(); };
$('#acct-revoke-others').onclick = async () => {
  if (!(await confirmSheet('退出其他所有设备？', '除了这台以外，所有设备都需要重新登录。', '全部退出'))) return;
  try { await api('/api/devices/revoke', { all: true }); toast('已退出其他设备', '', null, 'done'); renderAccount(); } catch (e) { toast('操作失败', e.message, null, 'error'); }
};

function refreshOpenSheets() {
  if ($('#dlg-settings').open) {
    renderSettingsMachines(); renderKeyPage(); renderSettingsNav();
    $('#set-hubver').textContent = S.hub && S.hub.version ? S.hub.version : '—';
  }
  if ($('#dlg-new').open) {
    const sel = $('#new-machine'); const cur = sel.value;
    for (const o of sel.options) { const m = S.byId[o.value]; if (m) o.disabled = !m.online; }
    sel.value = cur;
  }
  if ($('#dlg-more').open) fillMore();
  if ($('#dlg-ctl').open) renderCtl();
}
$('#btn-settings').onclick = () => { settingsPage('main'); renderSettings(); openSheet($('#dlg-settings')); };
$('#opt-hide-exited').onchange = (e) => { S.hideExited = e.target.checked; LS.setItem('glimpse.hideExited', S.hideExited ? '1' : '0'); renderHome(); };
function setFont(v) {
  S.fontSize = Math.max(8, Math.min(18, v)); LS.setItem('glimpse.font', String(S.fontSize));
  $('#opt-font').textContent = String(S.fontSize);
  if (term) term.options.fontSize = S.fontSize;
}
$('#font-dec').onclick = () => setFont(S.fontSize - 1);
$('#font-inc').onclick = () => setFont(S.fontSize + 1);
function urlB64ToBytes(s) { const pad = '='.repeat((4 - (s.length % 4)) % 4); return b64ToBytes((s + pad).replace(/-/g, '+').replace(/_/g, '/')); }
$('#btn-push').onclick = async () => {
  try {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) { $('#set-push').textContent = await pushState(); return; }
    const perm = await Notification.requestPermission();   // must run inside the tap handler on iOS
    if (perm !== 'granted') { $('#set-push').textContent = await pushState(); return; }
    const reg = await navigator.serviceWorker.ready;
    const { publicKey } = await api('/api/vapid');
    let sub = await reg.pushManager.getSubscription();
    if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlB64ToBytes(publicKey) });
    await api('/api/push/subscribe', sub.toJSON());
    $('#set-push').textContent = '已开启 ✓';
  } catch (err) { $('#set-push').textContent = '开启失败：' + err.message; }
};
$('#btn-push-test').onclick = async () => {
  try { const r = await api('/api/push/test', {}); toast('已发送', '发给 ' + r.count + ' 个设备', null, 'done'); } catch (err) { toast('发送失败', err.message, null, 'error'); }
};
$('#btn-logout').onclick = async () => {
  if (!(await confirmSheet('退出登录？', '退出后需要重新输入访问令牌。', '退出登录'))) return;
  // drop this device's push subscription first (needs the still-valid session)
  try {
    if ('serviceWorker' in navigator && 'PushManager' in window) {
      const reg = await navigator.serviceWorker.getRegistration();
      const sub = reg && await reg.pushManager.getSubscription();
      if (sub) { try { await api('/api/push/unsubscribe', { endpoint: sub.endpoint }); } catch (_) {} await sub.unsubscribe(); }
    }
  } catch (_) {}
  try { await api('/api/logout', {}); } catch (_) {}
  LS.removeItem('glimpse.token');
  location.replace('/');
};

boot();
