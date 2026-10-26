'use strict';
// Glimpse phone app (v4). State lives on the hub; this page renders it and forwards input.
const APP_VERSION = '0.4.0';
// ---- manual light / dark theme (Settings → 外观). Runs first so the forced theme is in place before anything renders.
// glimpse.theme = auto | light | dark | solarized  ->  <html data-theme="light|dark"> (auto: no attribute, CSS follows
// prefers-color-scheme). solarized = dark + data-palette="solarized" (Warp's Solarized Dark), so every dark rule still applies.
const THEME_BG = { dark: '#0B0C0F', light: '#F2F2F7', solarized: '#002B36' };
function themePref() { const t = window.localStorage.getItem('glimpse.theme'); return ['light', 'dark', 'solarized'].includes(t) ? t : 'auto'; }
function applyTheme(t) {
  const root = document.documentElement;
  const scheme = t === 'solarized' ? 'dark' : t;
  if (scheme === 'light' || scheme === 'dark') root.setAttribute('data-theme', scheme); else root.removeAttribute('data-theme');
  if (t === 'solarized') root.setAttribute('data-palette', 'solarized'); else root.removeAttribute('data-palette');
  // status bar / browser chrome: forced = one colour for both media queries; auto = per-scheme colours again
  for (const m of document.querySelectorAll('meta[name=theme-color]')) {
    const s = /light/.test(m.media || '') ? 'light' : 'dark';
    m.setAttribute('content', THEME_BG[t === 'auto' ? s : t]);
  }
  const cs = document.querySelector('meta[name=color-scheme]');
  if (cs) cs.setAttribute('content', t === 'auto' ? 'dark light' : scheme);
  if (window.onThemeApplied) window.onThemeApplied();      // the terminal (created later) follows the palette
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
const NETI = { testing: {}, asked: {} };  // testing: machine id -> timer while its speed test runs; asked: last lookup request

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
  if (d < 45) return T('刚刚'); if (d < 3600) return Math.max(1, Math.round(d / 60)) + T(' 分钟前');
  if (d < 86400) return Math.floor(d / 3600) + T(' 小时前'); return Math.floor(d / 86400) + T(' 天前');
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
const STATUS = { starting: T('启动中'), running: T('运行中'), waiting: T('等待回复'), idle: T('空闲'), exited: T('已退出'), error: T('出错') };
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
    id: 'net', title: T('网络'), icon: 'wifi', scope: 'both',
    render(ctx) {
      const n = S.net; const rtt = n.rtt.length ? n.rtt[n.rtt.length - 1] : null;
      const rx = n.rx.length ? n.rx[n.rx.length - 1] : 0;
      if (ctx.scope === 'session') {
        const st = ctx.mc && ctx.mc.stats;
        return ('<div class="w-col"><div class="w-sub">' + T('延迟 手机↔hub') + '</div><div class="w-big">') + (rtt == null ? '—' : Math.round(rtt)) +
          '<small>ms</small></div><div class="w-sub">hub↔' + esc(ctx.mc ? ctx.mc.id : '') + ' ' + fmtMs(st && st.rtt_ms) + '</div></div>';
      }
      let rows = '';
      for (const mc of S.machines) {
        if (!mc.online || !mc.stats) continue;
        rows += '<div class="w-row"><b>' + esc(mc.id) + '</b><span class="grow">' + fmtMs(mc.stats.rtt_ms) + '</span><span>↓' +
          fmtBps(mc.stats.rx_bps) + '</span></div>';
      }
      return wHead(this) + '<div class="w-big">' + (rtt == null ? '—' : Math.round(rtt)) + '<small>ms</small></div>' +
        ('<div class="w-sub">' + T('下行 ')) + fmtBps(rx) + '</div>' + spark(n.rtt) + (rows ? '<div class="w-rows">' + rows + '</div>' : '');
    },
  },
  {
    id: 'tokens', title: T('Token 用量'), icon: 'coins', scope: 'both',
    render(ctx) {
      if (ctx.scope === 'session') {
        const u = ctx.s && ctx.s.usage; if (!u) return null;
        return ('<div class="w-col"><div class="w-sub">' + T('Token 输入 / 输出') + '</div><div class="w-big">') + fmtNum(u.input) + '<small>/ ' + fmtNum(u.output) +
          ('</small></div><div class="w-sub">' + T('缓存 读 ')) + fmtNum(u.cache_read) + T(' · 写 ') + fmtNum(u.cache_write) + '</div></div>';
      }
      let tot = 0; let io = 0; let n = 0;
      for (const [, s] of allSessions()) if (s.usage) { tot += tokTotal(s.usage); io += (s.usage.input || 0) + (s.usage.output || 0); n++; }
      const days = S.machines.filter((mc) => mc.usage_today);
      if (!days.length) {
        return wHead(this) + '<div class="w-big">' + fmtNum(tot) + ('</div><div class="w-sub">' + T('含缓存 · ')) + n + (T(' 个会话') + '</div>') +
          ('<div class="w-rows"><div class="w-row"><span class="grow">' + T('输入+输出') + '</span><b>') + fmtNum(io) + '</b></div>' +
          ('<div class="w-row"><span class="grow">' + T('缓存') + '</span><b>') + fmtNum(tot - io) + '</b></div></div>';
      }
      // today, all conversations on every machine (not only Glimpse ones), from the agents' transcript scan
      let day = 0; let dio = 0;
      for (const mc of days) { day += dayTotal(mc.usage_today); dio += dayIO(mc.usage_today); }
      const rows = days.slice(0, 3).map((mc) => '<div class="w-row"><b style="min-width:22px">' + esc(mc.id) + '</b><span class="grow">' +
        esc(mc.name) + '</span><b>' + fmtNum(dayTotal(mc.usage_today)) + '</b></div>').join('');
      return wHead(this) + '<div class="w-big">' + fmtNum(day) + ('<small>' + T('今日') + '</small></div><div class="w-sub">' + T('含缓存 · 输入+输出 ')) + fmtNum(dio) + '</div>' +
        '<div class="w-rows">' + rows + ('<div class="w-row w-sep"><span class="grow">' + T('当前 ')) + n + (T(' 个会话') + '</span><b>') + fmtNum(tot) + '</b></div></div>';
    },
  },
  {
    id: 'context', title: T('上下文'), icon: 'gauge', scope: 'both',
    render(ctx) {
      if (ctx.scope === 'session') {
        const u = ctx.s && ctx.s.usage; if (!u || !u.context_window) return null;
        const pct = 100 * u.context_used / u.context_window;
        let rate = '';
        if (u.rate && (u.rate.primary != null || u.rate.secondary != null)) {
          rate = ('<div class="w-sub">' + T('限额 ')) + (u.rate.primary != null ? Math.round(u.rate.primary) + '%' : '—') + T(' · 周 ') +
            (u.rate.secondary != null ? Math.round(u.rate.secondary) + '%' : '—') + '</div>';
        }
        return ring(pct, T('上下文已用 ') + Math.round(pct) + '%') + ('<div class="w-col"><div class="w-sub">' + T('上下文') + '</div><div class="w-big" style="font-size:15px">') +
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
      if (!rows) return wHead(this) + ('<div class="w-sub">' + T('暂无数据') + '</div>');
      return wHead(this).replace('w-h"', 'w-h" style="margin-bottom:2px"') + '<div class="w-rows" style="gap:9px">' + rows + '</div>';
    },
  },
  {
    id: 'machines', title: T('机器负载'), icon: 'cpu', scope: 'both',
    render(ctx) {
      const one = (mc) => {
        const st = mc.stats;
        if (!mc.online || !st) return '<div class="w-row"><b>' + esc(mc.id) + ('</b><span class="grow" style="color:var(--c-err)">' + T('离线') + '</span></div>');
        const cpu = st.ncpu ? 100 * st.load[0] / st.ncpu : 0;
        return '<div class="w-row"><b style="min-width:22px">' + esc(mc.id) + '</b><span style="min-width:24px">CPU</span>' + meter(cpu) +
          ('<span style="min-width:26px">' + T('内存') + '</span>') + meter(st.mem_pct) + '</div>';
      };
      if (ctx.scope === 'session') {
        const mc = ctx.mc; if (!mc || !mc.stats) return null;
        const cpu = mc.stats.ncpu ? 100 * mc.stats.load[0] / mc.stats.ncpu : 0;
        return '<div class="w-col"><div class="w-sub">' + esc(mc.id) + (T(' 负载') + '</div><div class="w-big">') + Math.round(cpu) +
          ('<small>% CPU</small></div><div class="w-sub">' + T('内存 ')) + Math.round(mc.stats.mem_pct) + '% · ' + mc.stats.ncpu + (T(' 核') + '</div></div>');
      }
      return wHead(this) + '<div class="w-rows" style="gap:9px">' + S.machines.map(one).join('') + '</div>';
    },
  },
  {
    id: 'controls', title: T('模型与模式'), icon: 'sliders', scope: 'session',
    render(ctx) {
      const s = ctx.s; if (!s || (s.kind !== 'claude' && s.kind !== 'codex')) return null;
      const md = sessMode(s);
      const top = s.kind === 'claude' ? modeName(md.mode || 'default') : (md.model ? shortModel(md.model) : 'default model');
      const sub = s.kind === 'claude' ? [md.model ? shortModel(md.model) : 'default model', md.effort ? 'effort ' + md.effort : ''] :
        [md.effort ? 'effort ' + md.effort : '', md.sandbox || ''];
      return ('<button type="button" class="w-tap" data-act="ctl" aria-label="' + T('模型与模式：')) + esc(top) + (T('，点按修改') + '"><span class="w-col"><span class="w-sub">') +
        (s.kind === 'claude' ? T('模式') : T('模型')) + ' ' + ic('chev', 'xs') + '</span><span class="w-big" style="font-size:15px">' + esc(top) + '</span><span class="w-sub">' +
        esc(sub.filter(Boolean).join(' · ') || T('点按修改')) + '</span></span></button>';
    },
  },
  {
    // 7-day quota of every key from keys.usage_url; in a session: the session's own key
    id: 'keys', title: T('Key 用量'), icon: 'key', scope: 'both',
    render(ctx) {
      const keys = (S.keys || []).filter((k) => k.usage);
      if (!keys.length) return null;                    // no usage_url configured: nothing to show
      const week = (u) => { const ws = (u && u.windows) || []; return ws.reduce((a, w) => (!a || (w.window_sec || 0) > (a.window_sec || 0) ? w : a), null); };
      const short = (u) => { const ws = (u && u.windows) || []; return ws.reduce((a, w) => (!a || (w.window_sec || 0) < (a.window_sec || 0) ? w : a), null); };
      const pctTxt = (w) => (w && w.percent != null ? Math.round(w.percent) + '%' : '—');
      if (ctx.scope === 'session') {
        const k = ctx.s && ctx.s.key && keys.find((x) => x.name === ctx.s.key); if (!k) return null;
        if (k.usage.error) return '<div class="w-col"><div class="w-sub">Key ' + esc(k.name) + ('</div><div class="w-big" style="font-size:15px;color:var(--c-err)">' + T('不可用') + '</div><div class="w-sub">') + esc(k.usage.error) + '</div></div>';
        const w = week(k.usage), s5 = short(k.usage);
        return '<div class="w-col"><div class="w-sub">Key ' + esc(k.name) + (k.exhausted ? T(' · 已用完') : '') + '</div><div class="w-big">' + pctTxt(w) +
          ('<small>' + T('7 天') + '</small></div><div class="w-sub">' + T('5 小时 ')) + pctTxt(s5) + (k.usage.today_cost != null ? T(' · 今日 $') + k.usage.today_cost : '') + '</div></div>';
      }
      // every key, highest 7-day percentage first (unusable ones last); four rows tall, the rest scrolls
      const pct = (k) => (k.usage.error ? -1 : (week(k.usage) || {}).percent || 0);
      const rows = keys.slice().sort((a, b) => pct(b) - pct(a)).map((k) => {
        if (k.usage.error) return '<div class="w-row"><span class="grow">' + esc(k.name) + ('</span><span style="color:var(--c-err)">' + T('不可用') + '</span></div>');
        const w = week(k.usage);
        return '<div class="w-row"><span class="grow">' + esc(k.name) + '</span>' + meter(w ? w.percent : 0) + '<b style="min-width:34px;text-align:right">' + pctTxt(w) + '</b></div>';
      }).join('');
      const b = S.balance || {};
      const mode = { off: '', on_exhausted: T(' · 自动换'), least_used: T(' · 按用量分配') }[b.mode] || '';
      return wHead(this) + ('<div class="w-sub" style="margin-bottom:4px">' + T('7 天额度')) + mode + (keys.length > 4 ? ' · ' + keys.length + T(' 个') : '') + '</div>' +
        '<div class="w-rows w-scroll" style="gap:9px"' + (keys.length > 4 ? (' tabindex="0" role="region" aria-label="' + T('全部 ')) + keys.length + (T(' 个 Key 的用量') + '"') : '') + '>' + rows + '</div>';
    },
  },
  {
    // AMem memory: storage of each machine's vault; in a session: the project it reads / writes
    id: 'memory', title: T('记忆'), icon: 'sparkle', scope: 'both',
    render(ctx) {
      const ms = S.machines.filter((mc) => mc.memory && mc.memory.enabled);
      if (!ms.length) return null;
      if (ctx.scope === 'session') {
        const s = ctx.s; if (!s || !s.memory || !ctx.mc || !ctx.mc.memory || !ctx.mc.memory.enabled) return null;
        memOverviewSoon(ctx.mc);
        const ov = MEM.data[ctx.mc.id + ':overview'];
        const p = ov && ov.projects.find((x) => x.project === s.memory);
        return '<button type="button" class="w-tap" data-act="mem" data-m="' + esc(ctx.mc.id) + '" data-proj="' + esc(s.memory) + ('" aria-label="' + T('记忆：项目 ')) + esc(s.memory) +
          (T('，点按查看') + '"><span class="w-col"><span class="w-sub">' + T('记忆 ')) + ic('chev', 'xs') + '</span><span class="w-big" style="font-size:15px">' + esc(s.memory) +
          '</span><span class="w-sub">' + (p ? p.memories + T(' 条记忆 · ') + fmtNum(p.records) + T(' 条记录') : T('已接入')) + '</span></span></button>';
      }
      // one tappable row per machine (each opens that machine's vault)
      const rows = ms.map((mc) => {
        memOverviewSoon(mc);
        const ov = MEM.data[mc.id + ':overview'];
        const head = '<b style="min-width:22px">' + esc(mc.id) + '</b>';
        let body;
        if (!mc.online) body = ('<span class="grow" style="color:var(--c-err)">' + T('离线') + '</span>');
        else if (!mc.memory.available) body = ('<span class="grow" style="color:var(--c-err)">' + T('不可用') + '</span>');
        else if (!ov) body = ('<span class="grow">' + T('读取中…') + '</span>');
        else {
          // two lines: size + meter, then the counts (one line is too narrow for both)
          const pct = ov.budget ? 100 * ov.size.total / ov.budget.max_bytes : 0;
          return '<button type="button" class="w-row-tap stack" data-act="mem" data-m="' + esc(mc.id) + ('" aria-label="' + T('打开 ')) + esc(mc.id) + T(' 的记忆，已用 ') +
            fmtBytes(ov.size.total) + (ov.budget ? T(' / 上限 ') + fmtBytes(ov.budget.max_bytes) : '') + '">' +
            '<span class="w-row">' + head + meter(pct) + '<b style="min-width:52px;text-align:right">' + fmtBytes(ov.size.total) + '</b></span>' +
            '<span class="w-sub">' + ov.projects.length + T(' 个项目 · ') + ov.projects.reduce((a, p) => a + p.memories, 0) + (T(' 条记忆') + '</span></button>');
        }
        return '<button type="button" class="w-row w-row-tap" data-act="mem" data-m="' + esc(mc.id) + ('" aria-label="' + T('打开 ')) + esc(mc.id) + (T(' 的记忆') + '">') + head + body + '</button>';
      }).join('');
      return wHead(this) + '<div class="w-rows" style="gap:6px">' + rows + '</div>';
    },
  },
  {
    // the machine's internet connection: public IP + location, and an on-demand download speed test.
    // The IP lookup (ipinfo.io, from the machine) only happens while this widget is shown.
    id: 'netinfo', title: T('机器网络'), icon: 'globe', scope: 'both',
    render(ctx) {
      const loc = (n) => [n.city, n.region !== n.city ? n.region : '', n.country].filter(Boolean).join(', ');
      for (const mc of ctx.scope === 'session' ? [ctx.mc].filter(Boolean) : S.machines) netinfoSoon(mc);
      const speed = (mc) => {
        const sp = mc.speed;
        if (mc.outdated) return ('<span style="color:var(--c-wait)">' + T('agent 需要更新才能测速') + '</span>');
        if (NETI.testing[mc.id]) return ('<span class="spinner" aria-hidden="true"></span>' + T(' 测速中…'));
        if (!sp) return '';
        if (sp.error) return '<span style="color:var(--c-err)">' + esc(sp.error) + '</span>';
        const mbps = (sp.down_bps || 0) / 1e6;   // speed tests are quoted in Mbps, like broadband plans
        return '↓ ' + (mbps >= 10 ? Math.round(mbps) : mbps.toFixed(1)) + T(' Mbps · 延迟 ') + Math.round(sp.ttfb_ms) + ' ms';
      };
      const btn = (mc) => mc.outdated ? '<button type="button" class="btn sm" data-au="' + esc(mc.id) + '"' + (AU[mc.id] ? ' disabled' : '') + (' aria-label="' + T('更新 ')) + esc(mc.id) + (T(' 的 agent') + '">') + (AU[mc.id] ? T('更新中') : T('更新')) + '</button>' :
        '<button type="button" class="btn sm" data-speed="' + esc(mc.id) + '"' + (NETI.testing[mc.id] || !mc.online ? ' disabled' : '') + ('>' + T('测速') + '</button>');
      if (ctx.scope === 'session') {
        const mc = ctx.mc; if (!mc) return null;
        const n = mc.netinfo;
        return '<div class="w-col" style="min-width:200px"><div class="w-sub">' + esc(mc.id) + (T(' 公网出口') + '</div><div class="w-big" style="font-size:15px">') +
          (n && n.ip ? esc(n.ip) : n && n.error && !mc.outdated ? ('<span style="color:var(--c-err)">' + T('查询失败') + '</span>') : '—') + '</div><div class="w-sub">' +
          (n && n.ip ? esc(loc(n) || n.org || '') : '') + '</div><div class="w-sub" style="display:flex;align-items:center;gap:6px">' + speed(mc) + btn(mc) + '</div></div>';
      }
      // one line each for IP, location and speed: a widget is too narrow to put two of them side by side
      const line = (h, cls) => '<span class="ni-l' + (cls ? ' ' + cls : '') + '">' + h + '</span>';
      const rows = S.machines.map((mc) => {
        const n = mc.netinfo;
        let body;
        if (!mc.online) body = line(AU[mc.id] ? ('<span class="spinner" aria-hidden="true"></span>' + T(' 正在更新 agent…')) : ('<span style="color:var(--c-err)">' + T('离线') + '</span>'));
        else if (n && n.ip) body = line(esc(n.ip), 'ni-ip') + (loc(n) ? line(esc(loc(n))) : '');
        else body = line(mc.outdated ? '—' : n && n.error ? ('<span style="color:var(--c-err)">' + T('查询失败') + '</span>') : '…');
        const sp = mc.online ? speed(mc) : '';
        return '<div class="w-row" style="align-items:flex-start"><b style="min-width:22px">' + esc(mc.id) + '</b><span class="grow ni">' + body +
          (sp ? line(sp) : '') + '</span>' + (mc.online ? btn(mc) : '') + '</div>';
      }).join('');
      return wHead(this) + '<div class="w-rows" style="gap:9px">' + rows + '</div>';
    },
  },
  {
    id: 'activity', title: T('活跃会话'), icon: 'pulse', scope: 'home',
    render() {
      const c = { running: 0, waiting: 0, idle: 0, error: 0 };
      for (const [, s] of allSessions()) {
        const k = s.status === 'starting' ? 'running' : s.status;
        if (k in c) c[k]++;
      }
      const cell = (k, l) => '<div><span class="dot ' + k + '" style="animation:none"></span><b>' + c[k] + '</b>' + l + '</div>';
      return wHead(this) + '<div class="act-grid">' + cell('running', T('运行')) + cell('waiting', T('等待')) + cell('idle', T('空闲')) + cell('error', T('出错')) + '</div>';
    },
  },
];
function netinfoSoon(mc) {
  // ask the machine for its public IP when we have none, it is over 10 min old, or the last lookup failed 5 min ago
  if (!mc.online || mc.outdated) return;               // an outdated agent cannot answer it
  const t = Date.now() / 1000; const n = mc.netinfo;
  if (n && n.ts && t - n.ts < (n.error ? 300 : 600)) return;
  if (t - (NETI.asked[mc.id] || 0) < 60) return;        // a lookup is in flight (or just failed to send)
  if (send({ op: 'netinfo', m: mc.id })) NETI.asked[mc.id] = t;
}
const WIDGET_BY_ID = Object.fromEntries(WIDGETS.map((w) => [w.id, w]));
const DEFAULT_WIDGETS = ['controls', 'net', 'tokens', 'context', 'machines', 'keys', 'memory', 'netinfo'];
function widgetPrefs() {
  // {order:[id...], on:{id:bool}}
  let p = null; try { p = JSON.parse(LS.getItem('glimpse.widgets') || 'null'); } catch (_) { p = null; }
  if (!p || !Array.isArray(p.order)) p = { order: ['controls'].concat(WIDGETS.map((w) => w.id).filter((id) => id !== 'controls')), on: Object.fromEntries(WIDGETS.map((w) => [w.id, DEFAULT_WIDGETS.includes(w.id)])) };
  // widgets added in a later version: "controls" goes first, the rest at the end (on if they are default-on)
  for (const w of WIDGETS) if (!p.order.includes(w.id)) { const on = DEFAULT_WIDGETS.includes(w.id); if (w.id === 'controls') p.order.unshift(w.id); else p.order.push(w.id); p.on[w.id] = on; }
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
    const sc = el.querySelector('.w-scroll'); const top = sc ? sc.scrollTop : 0;
    setHTML(el, html);
    const next = prev ? prev.nextSibling : host.firstChild;
    if (next !== el) host.insertBefore(el, next);
    const sc2 = el.querySelector('.w-scroll');
    if (sc2) { if (sc2 !== sc) sc2.scrollTop = top; wScrollFade(sc2); }     // a usage refresh keeps where you scrolled to
    prev = el;
  }
  for (const el of have.values()) el.remove();
  return want.length;
}
// a scrolling list in a widget: faded at the bottom while there is more below
function wScrollFade(el) { el.classList.toggle('more', el.scrollTop + el.clientHeight < el.scrollHeight - 2); }
document.addEventListener('scroll', (e) => { if (e.target.classList && e.target.classList.contains('w-scroll')) wScrollFade(e.target); }, true);

// ------------------------------------------------------------------ toast / in-app banner
function toast(title, body, onTap, kind) {
  const host = $('#toast-host');
  const old = host.firstChild; if (old) old.remove();
  const t = document.createElement('button');
  t.type = 'button'; t.className = 'toast ' + (kind || '');
  const icn = { done: 'check', question: 'help', error: 'alert', exit: 'power' }[kind] || 'bell';
  t.innerHTML = '<span class="t-ic">' + ic(icn) + '</span><span class="grow"><b>' + esc(title) + '</b>' +
    (body ? '<span>' + esc(String(body).slice(0, 240)) + '</span>' : '') + '</span>';
  t.setAttribute('aria-label', title + (body ? T('：') + body : ''));
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
// OK -> true, cancel / dismissed -> false; with altText a second choice -> 'alt'. style 'primary' = a blue OK
// (not a destructive red one)
function confirmSheet(title, msg, okText, altText, style) {
  return new Promise((resolve) => {
    const d = $('#dlg-confirm');
    $('#cf-title').textContent = title; $('#cf-msg').textContent = msg; $('#cf-ok').textContent = okText;
    $('#cf-ok').className = 'btn lg block ' + (style === 'primary' ? 'primary' : 'danger');
    const alt = $('#cf-alt');
    alt.hidden = !altText; alt.textContent = altText || '';
    let done = false;
    const finish = (v) => { if (done) return; done = true; resolve(v); };
    $('#cf-ok').onclick = () => { finish(true); closeSheet(d); };
    alt.onclick = () => { finish('alt'); closeSheet(d); };
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
    // an installed app opens its start URL, which carries a fresh pairing code (for iOS Home Screen apps, which
    // don't share Safari's login). Already logged in: keep that session; pairing again added a device per launch
    let known = false;
    try { await api('/api/me'); known = true; } catch (_) {}
    if (!known) {
      try { await loginWith({ pair }); } catch (e) { S.pairErr = e.status === 429 ? T('尝试次数过多，请稍后再试') : T('登录链接无效或已过期，请在电脑上重新运行 gli url'); }
    }
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
  if (!v) { $('#login-err').textContent = T('请输入密码'); return; }
  const b = $('#login-btn'); b.disabled = true; b.innerHTML = ('<span class="spinner" aria-hidden="true"></span>' + T('登录中…'));
  try { await loginWith({ password: v }); $('#login-pw').value = ''; $('#login-err').textContent = ''; boot(); } catch (err) {
    $('#login-err').textContent = err.status === 401 ? T('密码不正确') : err.status === 429 ? T('尝试次数过多，请稍后再试') : T('无法连接 hub');
  } finally { b.disabled = false; b.textContent = T('登录'); }
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
  setConn('wait', S.haveState ? T('重连中…') : T('连接中…'));
  // a TCP connect that never completes (Tailscale down, hub asleep) would otherwise hang for minutes
  const openTimer = setTimeout(() => { if (S.ws === ws && ws.readyState === 0) { try { ws.close(); } catch (_) {} if (S.ws === ws) { S.ws = null; setConn('bad', T('连接超时')); scheduleReconnect(); } } }, 8000);
  ws.onopen = () => {
    clearTimeout(openTimer);
    if (S.ws !== ws) return;
    S.wsOk = true; S.backoff = 500; S.lastPong = performance.now(); setConn('ok', T('已连接'));
    flushOutbox();
    send({ op: 'visible', v: onScreen() });
    ping();
    if (S.view) { subscribe(); fsChanged(); }        // fs requests sent before the socket was up were dropped
    if (curScreen === 'kb' && KB.page) { KB.asked = {}; kbAsk('overview', ''); kbFetch(KB.page); kbImages($('#kb-body')); }
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
      show('login'); $('#login-err').textContent = T('这台设备已退出登录，请重新登录');
      return;
    }
    setConn('bad', T('已断开'));
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
  if (!S.pongTimer) S.pongTimer = setTimeout(() => { S.pongTimer = null; setConn('bad', T('无响应')); S.backoff = 500; connect(); }, 10000);
}
// "on screen" for the hub (it holds this device's push notifications back meanwhile): visible, and on a
// desktop also the focused window — a Glimpse window left open behind other apps still gets notifications
const DESKTOP = !matchMedia('(pointer: coarse)').matches;
function onScreen() { return document.visibilityState === 'visible' && (!DESKTOP || document.hasFocus()); }
// iOS suspends pages in the background: reconnect immediately when we come back
document.addEventListener('visibilitychange', () => {
  const vis = document.visibilityState === 'visible';
  if (vis && booted) {
    // a socket that has not answered for >10 s across a suspend is almost certainly dead: don't wait for the ping timeout
    if (!S.ws || S.ws.readyState > 1 || (S.ws.readyState === 1 && performance.now() - S.lastPong > 10000)) { S.backoff = 500; connect(); } else ping();
    if (S.view) send({ op: 'ack', m: S.view.m, sid: S.view.sid });
  }
  send({ op: 'visible', v: onScreen() });
});
for (const ev of ['focus', 'blur']) window.addEventListener(ev, () => send({ op: 'visible', v: onScreen() }));
// ---- the hub serves newer web code than this page runs (a window left open across an update): reload, but only
// while nobody looks at it and nothing would be lost (no unsaved edit, no sheet open); else on the next blur / hide
function checkBuild(b) {
  if (!b) return;
  if (!S.webBuild) { S.webBuild = b; return; }        // what this page loaded with
  if (b === S.webBuild || S.reloadWanted) return;
  S.reloadWanted = true;
  if (!reloadIfSafe()) toast(T('Glimpse 已更新'), T('切到别的窗口后会自动刷新，或点这里现在刷新'), () => reloadIfSafe(true), 'done');
}
function reloadIfSafe(now) {
  if (!S.reloadWanted) return false;
  if ((FS.edit && FS.edit.text !== FS.edit.base) || $$('dialog[open]').length) return false;
  if (!now && onScreen()) return false;
  draftSave(); edStash();
  location.reload();
  return true;
}
window.addEventListener('blur', () => setTimeout(reloadIfSafe, 300));
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') reloadIfSafe(); });
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
        agentUpdated(mc);
        if (mc.stats) {
          const h = S.mhist[mc.id] || (S.mhist[mc.id] = { rtt: [], rx: [], ts: 0 });
          if (mc.stats.ts !== h.ts) { h.ts = mc.stats.ts; pushHist(h.rtt, mc.stats.rtt_ms || 0); pushHist(h.rx, mc.stats.rx_bps || 0); }
        }
      }
      S.keys = m.keys || []; S.balance = m.balance || null; S.hub = m.hub || null; S.options = m.options || null; S.haveState = true;
      S.ext = Array.isArray(m.extensions) ? m.extensions : [];
      S.sched = Array.isArray(m.schedules) ? m.schedules : [];
      if (SCH.openWhen && (SCH.openWhen.until < Date.now() || session(SCH.openWhen.m, SCH.openWhen.sid))) {
        const w = SCH.openWhen; SCH.openWhen = null;
        if (session(w.m, w.sid)) setTimeout(() => openSession(w.m, w.sid), 0);
      }
      checkBuild(S.hub && S.hub.web);
      renderHome();
      if (S.view) renderView();
      $('#btn-vault').hidden = !kbOn();
      if (curScreen === 'kb' && KB.page && KB.data[kbPageKey(KB.page)] === undefined) kbFetch(KB.page);
      refreshOpenSheets();
      newFromRouteNow();
      break;
    case 'ui_new':         // `gli new` / Finder (hub computer only): the new-session form for a folder
      if (typeof m.cwd === 'string' && m.cwd) {
        newRoute = { cwd: m.cwd.slice(0, 4096), m: typeof m.m === 'string' ? m.m : '' };
        if (S.haveState) newFromRouteNow();
      }
      break;
    case 'pong':
      clearTimeout(S.pongTimer); S.pongTimer = null; S.lastPong = performance.now();
      flushOutbox();
      if (typeof m.ts === 'number') pushHist(S.net.rtt, Math.max(0, performance.now() - m.ts));
      else if (S.lastPing) pushHist(S.net.rtt, performance.now() - S.lastPing);
      if (S.wsOk) setConn('ok', T('已连接'));
      renderHomeWidgets(); if (S.view) renderViewWidgets();
      break;
    case 'out': if (isView(m)) onOut(m); break;
    case 'replay': if (isView(m)) onReplay(m); break;
    case 'chat': if (isView(m)) addChat(m.items || [], false); break;
    case 'resync': if (S.view && S.view.m === m.m) subscribe(); break;
    case 'created': {
      newBusy(false); closeSheet($('#dlg-new')); closeSheet($('#dlg-adopt'));
      if (m.workflow) {
        const w = m.workflow; const init = w.init || {};
        toast(T('项目已就绪'), (init.git_init ? T('新建了 git 仓库') : T('沿用已有的 git 仓库')) +
          (w.publish ? T('，已推到 ') + w.publish.repo : w.publish_error ? T('；GitHub 推送失败：') + w.publish_error : ''),
          null, w.publish_error ? 'error' : 'done');
      }
      // the state broadcast may arrive after this reply: insert the session now so the view has cols/rows/kind
      const mc = S.byId[m.m];
      if (mc && m.s && !mc.sessions.some((x) => x.sid === m.s.sid)) { mc.sessions.push(m.s); renderHome(); }
      if (m.s) openSession(m.m, m.s.sid);
      break;
    }
    case 'restarted': if (RS.active && RS.m === m.m && RS.sid === m.sid) rsDone(m); break;
    case 'key_balance_ok': toast(T('已保存'), '', null, 'done'); break;
    case 'project_workflow': onProjectWorkflow(m); break;
    case 'approved': if (isView(m)) { S.view.approving = null; S.view.cardSig = null; renderCard(); } break;
    case 'history': onHistory(m); break;
    case 'models': onModels(m, false); break;
    case 'memory': onMemory(m); break;
    case 'fs': onFs(m); break;
    case 'speedtest_done': {
      clearTimeout(NETI.testing[m.m]); delete NETI.testing[m.m];
      const mc = S.byId[m.m]; if (mc && m.speed) mc.speed = m.speed;   // show it now; the state broadcast follows
      renderHomeWidgets(); if (S.view) renderViewWidgets();
      break;
    }
    case 'menu_ok': if (isView(m)) menuDone(); break;
    case 'control_ok': ctlDone(m); break;
    case 'delete_ok': onDeleted(m); break;
    case 'machine_test': mcTested(m); break;
    case 'machines_ok': mcSaved(); break;
    case 'event': {
      // a turn of the open session ended: its files may have changed
      if (isView(m) && m.ev && (m.ev.kind === 'done' || m.ev.kind === 'error')) fsChanged();
      const here = isView(m) && document.visibilityState === 'visible';
      if (!here) toast(m.title || (m.ev && m.ev.title) || 'Glimpse', m.ev && m.ev.body, () => openSession(m.m, m.sid), m.ev && m.ev.kind === 'keyswap' ? 'done' : m.ev && m.ev.kind);
      break;
    }
    case 'vault_view': onVault(m); break;
    case 'ext_ok': onExtOk(m); break;
    case 'sched_ok': onSchedOk(m); break;
    case 'error':
      if (m.op === 'models' && m.kind) { onModels(m, true); break; }
      if (m.op === 'vault_view') { onVaultErr(m); break; }
      if (/^sched_/.test(m.op || '')) { onSchedErr(m); break; }
      if (/^ext_/.test(m.op || '')) { EXT.busy = false; $('#ext-add-btn').disabled = false; toast(T('扩展'), m.msg, null, 'error'); break; }
      if ($('#dlg-new').open && !m.sid) { newBusy(false); $('#new-err').textContent = m.msg; }
      else if (ADOPT.busy && $('#dlg-adopt').open && !m.sid) { adoptFail(m.msg); }
      else if (MC.busy && $('#dlg-machine').open) { mcFail(m.msg); }
      else if (RS.active && $('#dlg-restart').open) { rsFail(m.msg); }
      else if (CTL.busy && $('#dlg-ctl').open) { ctlFail(m.msg); }
      else if ($('#dlg-mem').open && !m.sid) { MEM.busy = false; toast(T('记忆'), m.msg, null, 'error'); }
      else toast(T('出错了'), m.msg, null, 'error');
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
  if (s.status === 'waiting') return s.detail || T('等待你回复');
  if (s.detail && s.status !== 'idle') return s.detail;
  return STATUS[s.status] || s.status;
}
function machineStats(mc) {
  if (!mc.online) return mc.error || T('离线');
  const st = mc.stats; const n = mc.sessions.length;
  const parts = [(mc.host || mc.type || '')];
  parts.push(n + T(' 个会话'));
  if (st) { parts.push('CPU ' + Math.round(100 * st.load[0] / (st.ncpu || 1)) + '%'); parts.push(T('内存 ') + Math.round(st.mem_pct) + '%'); parts.push(fmtMs(st.rtt_ms)); }
  return parts.filter(Boolean).join(' · ');
}
function cardHTML(mc, s) {
  let flag = '';
  if (s.pending) flag = '<div class="c-flag question">' + ic('shield', 'sm') + ('<span class="grow">' + T('待授权：')) + esc(s.pending.tool) + ' · ' + esc(s.pending.summary || '') + '</span></div>';
  else if (s.question) flag = '<div class="c-flag question">' + ic('help', 'sm') + ('<span class="grow">' + T('提问')) +
    (s.question.questions ? T('（') + (s.question.review ? T('待提交') : ((s.question.qi || 0) + 1) + '/' + s.question.questions.length) + T('）') : '') + T('：') + esc(s.question.question) + '</span></div>';
  else if (s.side) flag = '<div class="c-flag side">' + ic('chat', 'sm') + '<span class="grow">' + (s.side === 'side' ? T('旁支对话中（/btw），回复只在终端') : T('旁支对话还开着')) + '</span></div>';
  else if (s.attention === 'error') flag = '<div class="c-flag error">' + ic('alert', 'sm') + '<span class="grow">' + esc(s.detail || T('出错')) + '</span></div>';
  else if (s.attention === 'done') flag = '<div class="c-flag done">' + ic('check-c', 'sm') + ('<span class="grow">' + T('已完成') + '</span></div>');
  else if (s.attention === 'question') flag = '<div class="c-flag question">' + ic('help', 'sm') + ('<span class="grow">' + T('需要你回复') + '</span></div>');
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
    b.setAttribute('aria-label', mc.id + ' ' + s.name + T('，') + (STATUS[s.status] || s.status));
    setHTML(b, '<span class="dot ' + esc(s.status) + '" aria-hidden="true"></span><span class="t"><span class="m">' + esc(mc.id) + '</span> · ' + esc(s.name) + '</span>');
  });
  chips.scrollLeft = sl;
  chips.hidden = items.length === 0;
  // groups
  const groups = $('#groups'); groups.removeAttribute('aria-busy');
  if (!S.machines.length) {
    setHTML(groups, '<div class="empty"><div class="art">' + ic('server', 'lg') + ('</div><h3>' + T('还没有机器') + '</h3><p>' + T('在 hub 的配置文件里添加机器后会显示在这里。') + '</p></div>'));
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
    g.setAttribute('aria-label', T('机器 ') + mc.id);
    return g;
  }, (g, mc) => {
    $('.mid', g).textContent = mc.id;
    const dot = $('.mbadge .dot', g); dot.className = 'dot ' + (mc.online ? 'on' : 'off'); dot.setAttribute('aria-label', mc.online ? T('在线') : T('离线')); dot.setAttribute('role', 'img');
    $('.mname', g).textContent = mc.name;
    const st = $('.mstats', g); st.textContent = machineStats(mc); st.classList.toggle('err', !mc.online);
    let rc = $('.mhead .btn', g);
    if (!mc.online && !rc) {
      rc = document.createElement('button'); rc.type = 'button'; rc.className = 'btn sm'; rc.textContent = T('重连');
      rc.setAttribute('aria-label', T('重连 ') + mc.id); rc.onclick = () => send({ op: 'reconnect', m: mc.id }); $('.mhead', g).appendChild(rc);
    } else if (mc.online && rc) rc.remove();
    const cards = $('.cards', g);
    const ss = visibleSessions(mc).slice().sort(bySessionName);
    if (!ss.length) {
      setHTML(cards, mc.online ? ('<div class="empty-m"><span>' + T('这台机器上还没有会话') + '</span><button type="button" class="btn sm primary" data-new="') + esc(mc.id) + '">' + ic('plus', 'xs') + (T('新建') + '</button></div>')
        : ('<div class="empty-m"><span>' + T('机器离线，重连后可查看会话') + '</span></div>'));
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
    row.innerHTML = '<button type="button" class="sw-del" tabindex="-1">' + ic('trash') + ('<span>' + T('删除') + '</span></button><button type="button" class="card"></button>');
    const b = row.lastChild;
    b.onclick = () => { if (swipeConsumed(row)) return; openSession(mc.id, s.sid); };
    row.firstChild.onclick = () => { swipeClose(row); deleteSession(mc.id, s.sid); };
    swipeable(row, b);
    return row;
  }, (row, [mc, s]) => {
    const b = row.lastChild;
    b.className = 'card ' + (s.attention ? 'attn-' + s.attention : '') + (s.pending || s.question ? ' attn-question' : '') + (s.status === 'exited' ? ' exited' : '');
    b.setAttribute('aria-label', mc.id + ' ' + s.name + T('，') + (KIND[s.kind] || s.kind) + T('，') + statusLine(s) + T('，') + ago(s.activity));
    row.firstChild.setAttribute('aria-label', T('删除 ') + s.name);
    setHTML(b, cardHTML(mc, s));
  });
}
// status groups, most urgent first; a session needing an answer counts as "waiting" whatever its status
const STATUS_GROUPS = [
  { id: 'attn', title: T('需要处理'), icon: 'alert', test: (s) => !!(s.pending || s.question || s.attention === 'question') },
  { id: 'error', title: T('出错'), icon: 'x-c', test: (s) => s.status === 'error' || s.attention === 'error' },
  { id: 'running', title: T('进行中'), icon: 'pulse', test: (s) => s.status === 'running' || s.status === 'starting' },
  { id: 'waiting', title: T('等待回复'), icon: 'help', test: (s) => s.status === 'waiting' },
  { id: 'idle', title: T('空闲'), icon: 'check-c', test: (s) => s.status === 'idle' },
  { id: 'exited', title: T('已结束'), icon: 'power', test: () => true },
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
    setHTML(groups, '<div class="empty"><div class="art">' + ic('inbox', 'lg') + ('</div><h3>' + T('还没有会话') + '</h3><p>' + T('点右下角 ＋ 新建一个会话。') + '</p></div>'));
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
    g.setAttribute('aria-label', b.g.title + ' ' + b.items.length + T(' 个会话'));
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
  if (running && !(await confirmSheet(T('结束进程并删除？'), '“' + s.name + T('”还在运行，进程会被结束，会话从列表中删除。'), T('结束进程并删除')))) return;
  if (!send({ op: 'delete', m, sid })) toast(T('未连接'), T('稍后再试'), null, 'error');
}
function onDeleted(m) {
  const mc = S.byId[m.m];
  if (mc) mc.sessions = mc.sessions.filter((x) => x.sid !== m.sid);
  if (isView(m)) {
    for (const d of $$('dialog.sheet[open]')) closeSheet(d);
    if (history.state && history.state.glimpse) history.back(); else { history.replaceState(null, '', location.pathname); closeSession(); }
  }
  renderHome();
  toast(T('已删除'), null, null, 'done');
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
const TERM_THEMES = {
  dark: { background: '#0B0D10', foreground: '#DCE1E8', cursor: '#5AA9FF', cursorAccent: '#0B0D10', selectionBackground: 'rgba(90,169,255,.35)',
    black: '#1C1F25', red: '#FF6B63', green: '#3DDC6A', yellow: '#FFB547', blue: '#5AA9FF', magenta: '#C792EA', cyan: '#56D4DD', white: '#DCE1E8',
    brightBlack: '#5C636E', brightRed: '#FF8E86', brightGreen: '#6EE7A0', brightYellow: '#FFD07A', brightBlue: '#8CC4FF', brightMagenta: '#DDB3F5', brightCyan: '#8BE8EE', brightWhite: '#FFFFFF' },
  // Solarized Dark (Ethan Schoonover's palette, as in Warp's built-in theme)
  solarized: { background: '#002B36', foreground: '#93A1A1', cursor: '#93A1A1', cursorAccent: '#002B36', selectionBackground: 'rgba(147,161,161,.28)',
    black: '#073642', red: '#DC322F', green: '#859900', yellow: '#B58900', blue: '#268BD2', magenta: '#D33682', cyan: '#2AA198', white: '#EEE8D5',
    brightBlack: '#586E75', brightRed: '#CB4B16', brightGreen: '#93A1A1', brightYellow: '#839496', brightBlue: '#839496', brightMagenta: '#6C71C4', brightCyan: '#93A1A1', brightWhite: '#FDF6E3' },
};
function termTheme() { return TERM_THEMES[document.documentElement.getAttribute('data-palette') === 'solarized' ? 'solarized' : 'dark']; }
window.onThemeApplied = () => { if (term) term.options.theme = termTheme(); };
function ensureTerm() {
  if (term) return term;
  term = new window.Terminal({
    fontSize: S.fontSize, fontFamily: 'ui-monospace, "SF Mono", Menlo, monospace', scrollback: 5000, convertEol: false,
    theme: termTheme(),
    // a thin blinking accent bar (Warp-like); TUIs that set their own cursor shape still win
    cursorStyle: 'bar', cursorWidth: 2, cursorBlink: true, cursorInactiveStyle: 'outline',
    allowProposedApi: true, screenReaderMode: false,
  });
  if (window.FitAddon) { try { term.loadAddon(new window.FitAddon.FitAddon()); } catch (_) {} }
  // opened lazily (openTerm) the first time the terminal tab is visible: xterm measures the font on open
  term.onData((d) => { if (S.view) sendBytes(enc.encode(d)); });
  term.onBinary((d) => { if (S.view) { const b = new Uint8Array(d.length); for (let i = 0; i < d.length; i++) b[i] = d.charCodeAt(i) & 255; sendBytes(b); } });
  termLineEditing();
  return term;
}
// ---- line editing on the input line (where the cursor is): a click moves the cursor there, a selection on
// that line is deleted with Backspace / Delete. The program only understands keys, so both become arrow keys
// and backspaces sent to it; text outside the cursor's line (already sent output) is never touched.
const ARROW_R = '\x1b[C', ARROW_L = '\x1b[D', BKSP = '\x7f';
function cellAt(e) {
  // xterm's screen element is the grid the rows are drawn into
  const scr = $('#term .xterm-screen'); if (!scr) return null;
  const r = scr.getBoundingClientRect();
  if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) return null;
  return { col: Math.floor((e.clientX - r.left) / (r.width / term.cols)), row: Math.floor((e.clientY - r.top) / (r.height / term.rows)) };
}
function onCursorLine(row) {
  const b = term.buffer.active;
  return b.viewportY === b.baseY && row === b.cursorY;     // only while scrolled to the bottom
}
function lineEnd(row) {
  // last column with text on the cursor's line (so a click past the text lands at its end, not in empty space)
  const line = term.buffer.active.getLine(term.buffer.active.baseY + row); if (!line) return 0;
  return line.translateToString(true).length;
}
function termLineEditing() {
  let down = null;
  $('#term').addEventListener('mousedown', (e) => { down = e.button === 0 ? { x: e.clientX, y: e.clientY } : null; });
  $('#term').addEventListener('mouseup', (e) => {
    const d = down; down = null;
    if (!d || !S.view || Math.abs(e.clientX - d.x) > 3 || Math.abs(e.clientY - d.y) > 3) return;   // a drag selects
    if (term.hasSelection()) return;
    const c = cellAt(e); if (!c || !onCursorLine(c.row)) return;
    const cur = term.buffer.active.cursorX;
    const tgt = Math.max(0, Math.min(c.col, lineEnd(c.row)));
    if (tgt !== cur) sendBytes(enc.encode((tgt > cur ? ARROW_R : ARROW_L).repeat(Math.abs(tgt - cur))));
  });
  term.attachCustomKeyEventHandler((e) => {
    if (e.type !== 'keydown' || (e.key !== 'Backspace' && e.key !== 'Delete') || !term.hasSelection() || !S.view) return true;
    const p = term.getSelectionPosition(); const b = term.buffer.active;
    if (!p) return true;
    const row = b.cursorY + b.baseY;                    // absolute line of the cursor
    if (p.start.y !== row || p.end.y !== row) return true;   // spans other lines: leave it to the program
    const s = Math.min(p.start.x, p.end.x), en = Math.min(Math.max(p.start.x, p.end.x), lineEnd(b.cursorY));
    if (en <= s) return true;
    // go to the end of the selection, then erase backwards over it
    const cur = b.cursorX;
    const move = en > cur ? ARROW_R.repeat(en - cur) : ARROW_L.repeat(cur - en);
    sendBytes(enc.encode(move + BKSP.repeat(en - s)));
    term.clearSelection();
    return false;
  });
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
  if (/^new(\?|$)/.test(h || '')) { newFromRoute(h); return; }
  const kb = kbParse(h);
  if (kb) { kbShow(kb); return; }
  if (curScreen === 'kb') {
    // back out of the vault: to the session it was opened from (its hash), else the list
    const [m0, sid0] = (h || '').split('/');
    if (S.view && m0 === S.view.m && sid0 === S.view.sid) { kbLeave(); return; }
    KB.page = null; KB.edit = null;
  }
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
  $('#input').value = draftGet(m, sid); autoGrow(); slashOpen(false);
  renderView();
  renderOutbox();
  subscribe();
  send({ op: 'ack', m, sid });
}
// ---- unsent text per session: kept while you leave the session (and across reloads), cleared once sent
function draftKey(m, sid) { return 'glimpse.draft.' + m + '/' + sid; }
function draftGet(m, sid) { return LS.getItem(draftKey(m, sid)) || ''; }
function draftSave() {
  const v = S.view; if (!v) return;
  const t = $('#input').value;
  if (t.trim()) LS.setItem(draftKey(v.m, v.sid), t.slice(0, 20000)); else LS.removeItem(draftKey(v.m, v.sid));
}
$('#input').addEventListener('input', draftSave);
function closeSession(keepScreen) {
  const v = S.view; if (!v) return;
  draftSave();
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
    $('#v-sub').textContent = mc && mc.online ? T('会话已不存在') : T('机器离线');
    setBanner('exited', 'info', mc && mc.online ? T('会话已不存在') : T('机器离线'), mc && mc.online ? T('它可能已被移除。') : T('机器重新上线后会自动恢复。'));
    renderCard(); renderViewWidgets(); return;
  }
  $('#v-sub').textContent = [v.m + ' ' + (mc ? mc.name : ''), KIND[s.kind] || s.kind, STATUS[s.status] || s.status, s.cwd].join(' · ');
  sizeTerm(s.cols, s.rows); v.sized = true;
  // the approval we were sending was resolved elsewhere (terminal, another phone) or replaced by a new one
  if (v.approving && (!s.pending || s.pending.id !== v.approvingId)) { v.approving = null; v.approvingId = null; v.cardSig = null; }
  if (s.status === 'waiting' && !s.pending && !s.question) setBanner('waiting', 'alert', s.detail || T('等待你回复'), T('在下方用快捷键（1/2/3、↑↓、⏎、Esc）或输入框回复，也可切到终端查看'));
  else if (s.status === 'error') setBanner('error', 'x-c', T('出错') + (s.exit_code != null ? T('（退出码 ') + s.exit_code + T('）') : ''), s.detail || '', T('更换 Key'), openRestart);
  else if (s.status === 'exited') setBanner('exited', 'power', T('进程已退出') + (s.exit_code != null ? T('（退出码 ') + s.exit_code + T('）') : ''), s.detail || '', s.kind !== 'shell' ? T('重启') : null, openRestart);
  // codex /btw: an ephemeral fork whose messages go to no rollout, so the chat and the status cannot follow it
  else if (s.side === 'side') setBanner('side', 'chat', T('codex 旁支对话（/btw）进行中'), T('旁支的回复不写入会话记录，聊天里看不到，请在终端查看。按 Ctrl+C 回到主对话（旁支的内容会丢失）。'), S.mode !== 'term' ? T('切到终端') : null, () => { setMode('term'); renderView(); });
  else if (s.side === 'main') setBanner('side', 'chat', T('旁支对话还开着'), T('终端里现在是主对话；按 ⌃/ 切回旁支，在旁支里按 Ctrl+C 关闭它。'));
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
  t.setAttribute('aria-label', S.stripOpen ? T('收起会话插件') : T('展开会话插件'));
  t.querySelector('.xs use').setAttribute('href', S.stripOpen ? '#i-up' : '#i-down');
}
$('#btn-wstrip').onclick = () => { S.stripOpen = !S.stripOpen; LS.setItem('glimpse.strip', S.stripOpen ? '1' : '0'); renderViewWidgets(); };
// widget delegates (home and session strips): data-act="ctl" opens the model/mode picker; data-act="mem" opens the memory sheet; data-speed triggers a speed test
for (const host of [$('#home-widgets'), $('#v-widgets')]) {
  host.addEventListener('click', (e) => {
    const act = e.target.closest('[data-act]');
    if (act) {
      if (act.dataset.act === 'ctl') openCtl();
      else if (act.dataset.act === 'mem') {
        const mid = act.dataset.m;
        if (mid) {
          openMemory(mid);
          if (act.dataset.proj) memOpenProject(act.dataset.proj);    // a session's widget opens its own project
        }
      }
      return;
    }
    const au = e.target.closest('[data-au]');
    if (au) { updateAgent(au.dataset.au); return; }
    const speed = e.target.closest('[data-speed]');
    if (speed) {
      const mid = speed.dataset.speed;
      if (NETI.testing[mid] || !send({ op: 'speedtest', m: mid })) return;
      NETI.testing[mid] = setTimeout(() => { delete NETI.testing[mid]; renderHomeWidgets(); if (S.view) renderViewWidgets(); }, 45000);
      renderHomeWidgets(); if (S.view) renderViewWidgets();
    }
  });
}

function setMode(mode, initial) {
  const v = S.view;
  // wide screens show the files as a column next to chat / terminal, so "files" is not a mode there
  if (mode === 'files' && filesWide()) mode = S.mode && S.mode !== 'files' ? S.mode : 'chat';
  // remembered per session; only chat / terminal become the default for other sessions
  if (v) { LS.setItem('glimpse.mode.' + v.m + '/' + v.sid, mode); if (!initial && mode !== 'files') LS.setItem('glimpse.mode', mode); }
  S.mode = mode;
  layoutPanes();
  $('#jump').hidden = true;
  if (v) { v.cardSig = null; renderCard(); }     // the menu card only shows in chat mode
  if (mode === 'term') requestAnimationFrame(() => { openTerm(); if (term) { term.refresh(0, term.rows - 1); term.scrollToBottom(); } termFollow(); });
  else if (mode === 'chat') { scheduleLive(); requestAnimationFrame(() => { const c = $('#chat'); c.scrollTop = c.scrollHeight; }); }
}
$('#mode-chat').onclick = () => setMode('chat');
$('#mode-term').onclick = () => setMode('term');
$('#mode-files').onclick = () => setMode('files');
$('#seg').addEventListener('keydown', (e) => {
  if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
  e.preventDefault();
  const tabs = ['chat', 'term', 'files'].filter((m) => !$('#mode-' + m).hidden);
  const m = tabs[(tabs.indexOf(S.mode) + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
  setMode(m); $('#mode-' + m).focus();
});

// ------------------------------------------------------------------ files (read-only browser of the session's directory)
// Phones: a third tab next to 对话 / 终端. Wide screens: a column on the left that stays next to chat / terminal
// (toggled with #btn-files). The agent confines every request to the session's directory and never writes.
const WIDE_FILES = matchMedia('(min-width: 1200px)');
const FS = { key: null, open: new Set(['']), dirs: {}, tab: 'tree', git: null, file: null, edit: null, lastDir: '', wrap: false, stale: true, at: 0 };
function filesWide() { return WIDE_FILES.matches; }
function filesColumn() { return filesWide() && LS.getItem('glimpse.filesCol') !== '0'; }
function layoutPanes() {
  const wide = filesWide(); const col = filesColumn(); const mode = S.mode;
  const view = $('#view');
  view.classList.toggle('files-col', col);
  $('#btn-files').hidden = !wide;
  $('#btn-files').setAttribute('aria-pressed', String(col));
  $('#mode-files').hidden = wide;
  const tabs = ['chat', 'term', 'files'].filter((m) => !$('#mode-' + m).hidden);
  const seg = $('#seg');
  seg.style.setProperty('--n', tabs.length);
  seg.style.setProperty('--i', Math.max(0, tabs.indexOf(mode)));
  for (const m of ['chat', 'term', 'files']) {
    $('#mode-' + m).setAttribute('aria-selected', String(m === mode));
    $('#mode-' + m).tabIndex = m === mode ? 0 : -1;
  }
  const showFiles = col || mode === 'files';
  $('#fpane').hidden = !showFiles;
  $('#chat').hidden = mode !== 'chat';
  $('#term-wrap').hidden = mode !== 'term';
  view.classList.toggle('files-only', !wide && mode === 'files');
  applyFw();
  if (showFiles) fsEnsure();
}
// ---- the files column's width (wide screens): drag the divider on its right edge, ← → when focused, double-click = default.
// The tree and the preview each remember their own width; none stored = the CSS default (340 px / wider for a preview).
const FW_MIN = 220; const FW_RIGHT = 420;                         // the right side keeps at least this much
function fwKey() { return FS.file ? 'glimpse.fwPreview' : 'glimpse.fwTree'; }
function fwMax() { return Math.max(FW_MIN, window.innerWidth - FW_RIGHT); }
function fwClamp(w) { return Math.round(Math.min(fwMax(), Math.max(FW_MIN, w))); }
function applyFw() {
  const view = $('#view'); const sp = $('#f-split');
  const w = parseInt(LS.getItem(fwKey()) || '', 10);
  if (filesColumn() && w > 0) view.style.setProperty('--fw', fwClamp(w) + 'px'); else view.style.removeProperty('--fw');
  const now = Math.round($('#fpane').getBoundingClientRect().width) || 0;
  sp.setAttribute('aria-valuemin', String(FW_MIN)); sp.setAttribute('aria-valuemax', String(fwMax()));
  sp.setAttribute('aria-valuenow', String(now));
}
function setFw(w) { LS.setItem(fwKey(), String(fwClamp(w))); applyFw(); }
(function () {
  const sp = $('#f-split'); let drag = null;
  sp.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || !filesColumn()) return;
    e.preventDefault();
    drag = { x0: e.clientX, w0: $('#fpane').getBoundingClientRect().width };
    sp.setPointerCapture(e.pointerId);
    document.body.classList.add('col-resizing');
  });
  sp.addEventListener('pointermove', (e) => {
    if (!drag) return;
    $('#view').style.setProperty('--fw', fwClamp(drag.w0 + e.clientX - drag.x0) + 'px');
  });
  const end = (e) => {
    if (!drag) return;
    setFw(drag.w0 + e.clientX - drag.x0);
    drag = null;
    document.body.classList.remove('col-resizing');
  };
  sp.addEventListener('pointerup', end);
  sp.addEventListener('pointercancel', end);
  sp.addEventListener('dblclick', () => { LS.removeItem(fwKey()); applyFw(); });
  sp.addEventListener('keydown', (e) => {
    const step = e.shiftKey ? 80 : 20; const w = $('#fpane').getBoundingClientRect().width;
    if (e.key === 'ArrowLeft') setFw(w - step);
    else if (e.key === 'ArrowRight') setFw(w + step);
    else if (e.key === 'Home') { LS.removeItem(fwKey()); applyFw(); }
    else return;
    e.preventDefault();
  });
  window.addEventListener('resize', () => { if (S.view && filesColumn()) applyFw(); });
})();
WIDE_FILES.addEventListener('change', () => { if (S.view) setMode(S.mode === 'files' && filesWide() ? 'chat' : S.mode); });
$('#btn-files').onclick = () => {
  LS.setItem('glimpse.filesCol', filesColumn() ? '0' : '1');
  layoutPanes();
  if (S.mode === 'term') requestAnimationFrame(() => { if (term) term.refresh(0, term.rows - 1); });
};
// the pane became visible (or the session changed): load the root and the git status
function fsEnsure() {
  const v = S.view; if (!v) return;
  const key = v.m + '/' + v.sid;
  if (FS.key !== key) {
    edStash();                                    // an unsaved edit of the session we leave is kept for later
    Object.assign(FS, { key, open: new Set(['']), dirs: {}, tab: 'tree', git: null, file: null, edit: null, reEdit: null, lastDir: '', stale: true, at: 0 });
    const host = $('#f-preview'); host._h = null; host.innerHTML = ''; host.classList.remove('editing');
  }
  if (FS.stale || Date.now() - FS.at > 20000) fsRefresh(true);
  renderFiles();
}
function fsSend(action, path, force, extra) {
  const v = S.view; if (!v) return false;
  return send(Object.assign({ op: 'fs', m: v.m, sid: v.sid, action, path: path || '', force: !!force }, extra || {}));
}
// reload what is shown (open folders, git status, the open file); quiet = keep the old content until the new arrives
function fsRefresh(quiet) {
  FS.stale = false; FS.at = Date.now();
  for (const p of FS.open) {
    const d = FS.dirs[p];
    if (!d || !quiet) FS.dirs[p] = Object.assign({}, d || {}, { loading: true });
    fsSend('list', p);
  }
  if (!FS.git || !quiet) FS.git = Object.assign({}, FS.git || {}, { loading: true });
  fsSend('git');
  if (FS.file) fsOpen(FS.file.path, FS.file.mode, FS.file.force, true);
  renderFiles();
}
// a turn ended / the files changed: refresh what is visible now, or on the next show
function fsChanged() {
  if (!S.view || $('#fpane').hidden) { FS.stale = true; return; }
  fsRefresh(true);
}
function onFs(m) {
  const v = S.view;
  if (!v || m.m !== v.m || m.sid !== v.sid || FS.key !== v.m + '/' + v.sid) return;
  if (m.action === 'list') {
    FS.dirs[m.path || ''] = m.error ? { error: m.error } : { entries: m.entries || [], more: m.more };
  } else if (m.action === 'git') {
    if (m.error) FS.git = { error: m.error };
    else {
      const map = {}; const dirty = new Set();
      for (const f of m.files || []) {
        map[f.path] = f.st;
        let p = f.path; let i;
        while ((i = p.lastIndexOf('/')) > 0) { p = p.slice(0, i); dirty.add(p); }
      }
      FS.git = { repo: !!m.repo, branch: m.branch || '', files: m.files || [], map, dirty };
    }
  } else if ((m.action === 'read' || m.action === 'diff') && FS.file && FS.file.path === m.path && FS.file.mode === m.action) {
    FS.file.loading = false; FS.file.res = m;
    // the file was reloaded while it is being edited (a turn ended, 刷新): never replace the text, only tell
    const e = FS.edit;
    if (e && e.path === m.path && m.action === 'read' && !m.error && m.sig && m.sig !== e.sig) e.stale = true;
    if (FS.reEdit === m.path && m.action === 'read') { FS.reEdit = null; if (!e && m.editable) { edStart(); return; } }
  } else if (m.action === 'write') { edSaved(m); return; }
  else if (m.action === 'create' || m.action === 'mkdir') { fnewDone(m); return; }
  renderFiles();
}
function fsOpen(path, mode, force, quiet) {
  const same = FS.file && FS.file.path === path && FS.file.mode === mode;
  FS.file = { path, mode: mode || 'read', force: !!force, loading: !quiet || !same, res: same && quiet ? FS.file.res : null,
    md: FS.file && same ? FS.file.md : LS.getItem('glimpse.mdView') !== 'raw' };
  if (!same && !quiet) $('#f-preview').scrollTop = 0;
  fsSend(FS.file.mode, path, force);
  renderFiles();
}
const FS_ST = { M: T('已修改'), A: T('新增'), D: T('已删除'), R: T('重命名'), U: T('未跟踪'), C: T('冲突') };
function fsSize(n) { return n == null ? '' : n < 1024 ? n + ' B' : fmtBytes(n); }
function fsName(p) { return p.split('/').pop() || p; }
function fsDir(p) { const i = p.lastIndexOf('/'); return i > 0 ? p.slice(0, i) : ''; }
function renderFiles() {
  const v = S.view; if (!v || $('#fpane').hidden) return;
  const s = session(v.m, v.sid);
  const f = FS.file;
  $('#f-back').hidden = !f;
  $('#f-new').hidden = !!FS.edit;
  $('#view').classList.toggle('f-editing', !!FS.edit);
  $('#f-tabs').hidden = !!f;
  $('#f-list').hidden = !!f;
  $('#f-preview').hidden = !f;
  $('#fpane').classList.toggle('previewing', !!f);
  $('#view').classList.toggle('files-preview', !!f);
  applyFw();
  if (f) {
    $('#f-title').textContent = fsName(f.path);
    $('#f-sub').textContent = (fsDir(f.path) || '.') + (f.res && f.res.size != null ? ' · ' + fsSize(f.res.size) : '');
    renderPreview();
    return;
  }
  const cwd = (s && s.cwd) || '';
  $('#f-title').textContent = fsName(cwd.replace(/\/+$/, '')) || cwd || T('文件');
  $('#f-sub').textContent = cwd + (FS.git && FS.git.branch ? ' · ' + FS.git.branch : '');
  const n = FS.git && FS.git.files ? FS.git.files.length : 0;
  $('#f-gitn').hidden = !n; $('#f-gitn').textContent = n > 999 ? '999+' : String(n);
  for (const b of $$('#f-tabs [data-ft]')) b.setAttribute('aria-checked', String(b.dataset.ft === FS.tab));
  setHTML($('#f-list'), FS.tab === 'git' ? gitHTML() : treeHTML('', 0));
}
function treeHTML(path, depth) {
  const d = FS.dirs[path];
  const pad = 'style="padding-left:' + (12 + Math.min(depth, 10) * 14) + 'px"';
  if (!d || (d.loading && !d.entries)) return '<div class="f-row f-note" ' + pad + ('><span class="spinner" aria-hidden="true"></span>' + T('读取中…') + '</div>');
  if (d.error) return '<div class="f-row f-note err" ' + pad + '>' + esc(d.error) + '</div>';
  if (!d.entries.length) return '<div class="f-row f-note" ' + pad + '>' + (depth ? T('空文件夹') : T('这个目录是空的')) + '</div>';
  const st = FS.git && FS.git.map || {};
  let h = '';
  for (const e of d.entries) {
    const p = path ? path + '/' + e.name : e.name;
    if (e.out) {
      h += '<div class="f-row dim" ' + pad + (' title="' + T('指向会话目录之外，不能打开') + '"><span class="f-chev"></span>') + ic(e.dir ? 'folder' : 'file', 'sm') +
        '<span class="f-name">' + esc(e.name) + ('</span><span class="f-meta">' + T('↗ 目录外') + '</span></div>');
      continue;
    }
    if (e.dir) {
      const open = FS.open.has(p);
      const mark = st[p] ? '<span class="f-st st-' + st[p] + '" title="' + esc(FS_ST[st[p]] || '') + '">' + st[p] + '</span>' :
        FS.git && FS.git.dirty && FS.git.dirty.has(p) ? ('<span class="f-dot" title="' + T('里面有改动') + '"></span>') : '';
      h += '<button type="button" class="f-row' + (e.dim ? ' dim' : '') + '" ' + pad + ' data-dir="' + esc(p) + '" aria-expanded="' + open + '">' +
        '<span class="f-chev' + (open ? ' open' : '') + '">' + ic('chev', 'xs') + '</span>' + ic('folder', 'sm') +
        '<span class="f-name">' + esc(e.name) + '</span>' + mark + '</button>';
      if (open) h += treeHTML(p, depth + 1);
    } else {
      const s2 = st[p];
      h += '<button type="button" class="f-row" ' + pad + ' data-file="' + esc(p) + '"' + (s2 ? ' data-st="' + s2 + '"' : '') + '>' +
        '<span class="f-chev"></span>' + ic(e.sensitive ? 'key' : 'file', 'sm') + '<span class="f-name">' + esc(e.name) + '</span>' +
        (s2 ? '<span class="f-st st-' + s2 + '" title="' + esc(FS_ST[s2] || '') + '">' + s2 + '</span>' : '<span class="f-meta">' + esc(fsSize(e.size)) + '</span>') + '</button>';
    }
  }
  if (d.more) h += '<div class="f-row f-note" ' + pad + ('>' + T('只显示前 2000 项') + '</div>');
  return h;
}
function gitHTML() {
  const g = FS.git;
  if (!g || (g.loading && !g.files)) return ('<div class="f-row f-note"><span class="spinner" aria-hidden="true"></span>' + T('读取 git 状态…') + '</div>');
  if (g.error) return '<div class="f-row f-note err">' + esc(g.error) + '</div>';
  if (!g.repo) return '<div class="f-empty">' + ic('folder', 'lg') + ('<div>' + T('这个目录不在 git 仓库里') + '</div></div>');
  if (!g.files.length) return '<div class="f-empty">' + ic('check-c', 'lg') + ('<div>' + T('没有改动')) + (g.branch ? '<br><span class="f-sub">' + esc(g.branch) + '</span>' : '') + '</div></div>';
  return g.files.map((x) => {
    const counts = x.add != null ? '<span class="f-cnt"><span class="a">+' + x.add + '</span> <span class="d">−' + x.del + '</span></span>' : '';
    const dir = fsDir(x.path);
    return '<button type="button" class="f-row f-change" ' + (x.dir ? 'data-gdir="' : 'data-diff="') + esc(x.path) + '" data-st="' + esc(x.st) + '">' +
      '<span class="f-st st-' + esc(x.st) + '" title="' + esc(FS_ST[x.st] || '') + '">' + esc(x.st) + '</span>' +
      '<span class="f-name">' + esc(fsName(x.path)) + (x.dir ? '/' : '') + (dir ? '<span class="f-path">' + esc(dir) + '</span>' : '') + '</span>' + counts + '</button>';
  }).join('');
}
function renderPreview() {
  const f = FS.file; const r = f.res; const host = $('#f-preview');
  if (FS.edit && FS.edit.path === f.path) { renderEditor(host); return; }
  host.classList.remove('editing');
  if (host.querySelector('.f-ed')) host._h = null;   // leaving the editor: rebuild the preview
  const isMd = /\.(md|markdown|mdx)$/i.test(f.path);
  const st = FS.git && FS.git.map ? FS.git.map[f.path] : null;
  const lang = window.HL ? HL.detect(f.path, r && r.text) : '';
  const rendered = isMd && f.mode === 'read' && f.md;
  // toolbar: rendered / source (markdown), full file / changes, wrap, reference in the composer, copy the path, language
  let bar = '<div class="f-bar">';
  if (isMd && f.mode === 'read') bar += ('<div class="f-seg" role="radiogroup" aria-label="' + T('Markdown 显示方式') + '">') +
    '<button type="button" role="radio" data-fmd="render" aria-checked="' + f.md + ('">' + T('渲染') + '</button>') +
    '<button type="button" role="radio" data-fmd="raw" aria-checked="' + !f.md + ('">' + T('源码') + '</button></div>');
  if (st) bar += ('<div class="f-seg" role="radiogroup" aria-label="' + T('显示') + '">') +
    (st !== 'D' ? '<button type="button" role="radio" data-fmode="read" aria-checked="' + (f.mode === 'read') + ('">' + T('文件') + '</button>') : '') +
    '<button type="button" role="radio" data-fmode="diff" aria-checked="' + (f.mode === 'diff') + ('">' + T('改动') + '</button></div>');
  if (r && r.kind === 'text' && r.editable && f.mode === 'read') bar += '<button type="button" class="f-chip" data-fedit>' + ic('pencil', 'xs') +
    (LS.getItem(edKey(f.path)) ? T('继续编辑') : T('编辑')) + '</button>';
  if (!rendered) bar += '<button type="button" class="f-chip" data-fwrap aria-pressed="' + FS.wrap + ('">' + T('自动换行') + '</button>');
  bar += ('<button type="button" class="f-chip" data-fref title="' + T('在输入框插入 @')) + esc(f.path) + ('">' + T('引用') + '</button>') +
    ('<button type="button" class="f-chip" data-fcopy>' + T('复制路径') + '</button>') +
    (lang && !rendered && r && (r.kind === 'text' || r.kind === 'diff') ? '<span class="f-lang">' + esc(HL.label(lang)) + '</span>' : '') + '</div>';
  let body;
  if (f.loading && !r) body = ('<div class="f-msg"><span class="spinner" aria-hidden="true"></span>' + T('读取中…') + '</div>');
  else if (r.error) body = '<div class="f-msg err">' + esc(r.error) + '</div>';
  else if (r.kind === 'sensitive') body = '<div class="f-msg">' + ic('key', 'lg') + ('<div><b>' + T('可能包含密钥或密码') + '</b><br>') +
    (r.reason === 'content' ? T('内容里有像密钥的字符串。') : T('这类文件通常保存密钥。')) + (T('为了不在屏幕上露出来，默认不显示内容。') + '</div>') +
    ('<button type="button" class="btn sm" data-fshow>' + T('显示内容') + '</button></div>');
  else if (r.kind === 'binary') body = '<div class="f-msg">' + ic('file', 'lg') + ('<div>' + T('二进制文件，不能预览')) + (r.size != null ? T('（') + esc(fsSize(r.size)) + T('）') : '') + '</div></div>';
  else if (r.kind === 'large') body = '<div class="f-msg">' + ic('file', 'lg') + ('<div>' + T('文件太大，不能预览（')) + esc(fsSize(r.size)) + (T('）') + '</div></div>');
  else if (r.kind === 'image') {
    const ok = /^image\/(png|jpeg|gif|webp|svg\+xml|bmp|x-icon)$/.test(r.mime || '') && /^[A-Za-z0-9+/=]*$/.test(r.data || '');
    body = ok ? '<div class="f-img"><img alt="' + esc(fsName(f.path)) + '" src="data:' + r.mime + ';base64,' + r.data + '"></div>' : ('<div class="f-msg err">' + T('无法显示') + '</div>');
  } else if (r.kind === 'diff') {
    // each changed / context line highlighted on its own (after its +/-/space), on the add / del background
    const hl1 = (s) => (lang && window.HL ? HL.lines(s, lang)[0] : esc(s));
    body = r.text ? '<div class="f-code diff' + (FS.wrap ? ' wrap' : '') + '">' + r.text.replace(/\n$/, '').split('\n').map((l) => {
      const c = /^\+(?!\+\+)/.test(l) ? 'add' : /^-(?!--)/.test(l) ? 'del' : /^@@/.test(l) ? 'hunk' : /^(diff |index |\+\+\+|---|\\ )/.test(l) ? 'meta' : '';
      const code = c === 'hunk' || c === 'meta' ? esc(l) : esc(l.slice(0, 1)) + hl1(l.slice(1));
      return '<div class="' + c + '">' + (code || ' ') + '</div>';
    }).join('') + '</div>' + (r.truncated ? ('<div class="f-msg sm">' + T('改动太多，只显示前一部分') + '</div>') : '') : ('<div class="f-msg">' + T('没有改动') + '</div>');
  } else if (rendered) {
    body = '<div class="f-md md">' + md(r.text) + '</div>';
  } else {
    const text = r.text.replace(/\n$/, '');
    const lines = lang && window.HL ? HL.lines(text, lang) : text.split('\n').map(esc);
    body = '<div class="f-code' + (FS.wrap ? ' wrap' : '') + '" style="--gw:' + String(lines.length).length + 'ch">' +
      lines.map((l, i) => '<div><span class="ln">' + (i + 1) + '</span>' + (l || ' ') + '</div>').join('') + '</div>' +
      (r.truncated ? ('<div class="f-msg sm">' + T('文件较大，只显示前 512 KB') + '</div>') : '');
  }
  const top = host.scrollTop;
  setHTML(host, bar + body);
  if (rendered) renderRich(host);
  host.scrollTop = top;
}
// ---- editing a file: a textarea over the highlighted text (same font and padding, scrolled together), line
// numbers on the left. Saving names the version it was edited from; a file that changed meanwhile (the AI edited it)
// is not overwritten without asking. An unsaved edit survives leaving the session / reloading (localStorage).
function edKey(path) { return 'glimpse.edit.' + FS.key + ':' + path; }
function edStash() {
  const e = FS.edit;
  if (e && FS.key && e.text !== e.base) LS.setItem(edKey(e.path), JSON.stringify({ sig: e.sig, text: e.text.slice(0, 2e6) }));
}
function edDrop(path) { if (FS.key) LS.removeItem(edKey(path)); }
function edIndent(text, path) {
  if (/(^|\/)(makefile|gnumakefile)$|\.mk$/i.test(path)) return '\t';
  let tabs = 0; let sp = 0; let unit = 0;
  for (const l of text.split('\n', 400)) {
    const m = /^([ \t]+)\S/.exec(l); if (!m) continue;
    if (m[1][0] === '\t') tabs++;
    else { sp++; const n = m[1].length; if (n === 2 || n === 4) unit = unit && unit < n ? unit : n; }
  }
  return tabs > sp ? '\t' : ' '.repeat(unit || 4);
}
function edStart() {
  const f = FS.file; const r = f && f.res; if (!r || !r.editable) return;
  let text = r.text; let restored = false;
  try {
    const s = JSON.parse(LS.getItem(edKey(f.path)) || 'null');
    if (s && s.sig === r.sig && typeof s.text === 'string' && s.text !== r.text) { text = s.text; restored = true; }
  } catch (_) {}
  FS.edit = { path: f.path, sig: r.sig, base: r.text, text, lang: window.HL ? HL.detect(f.path, r.text) : '', indent: edIndent(r.text, f.path),
    stale: false, saving: false, sent: null };
  renderFiles();
  const ta = $('#f-preview .f-ed-ta'); if (ta) ta.focus();
  if (restored) toast(T('已恢复未保存的修改'), f.path, null, 'done');
}
// leave the editor; asks first when there are unsaved changes. Resolves true when left.
async function edLeave() {
  const e = FS.edit; if (!e) return true;
  if (e.text !== e.base && !(await confirmSheet(T('放弃未保存的修改？'), fsName(e.path) + T(' 的修改还没有保存。'), T('放弃修改')))) return false;
  edDrop(e.path);
  FS.edit = null;
  if (FS.file) fsOpen(FS.file.path, 'read', FS.file.force, true);
  renderFiles();
  return true;
}
function edSave(force) {
  const e = FS.edit; if (!e || e.saving) return;
  e.saving = true; e.sent = e.text;
  if (!fsSend('write', e.path, force, { text: e.text, sig: e.sig })) { e.saving = false; toast(T('未连接'), T('稍后再试'), null, 'error'); }
  renderEditorBar();
}
async function edSaved(m) {
  const e = FS.edit;
  if (!e || e.path !== m.path) {                         // the editor was left while saving
    if (m.saved) fsChanged(); else if (m.error || m.conflict) toast(T('没有保存'), fsName(m.path) + (m.error ? T('：') + m.error : T(' 在别处被改过')), null, 'error');
    return;
  }
  e.saving = false;
  if (m.error) { toast(T('保存失败'), m.error, null, 'error'); renderEditorBar(); return; }
  if (m.conflict) {
    e.stale = true; renderEditorBar();
    if (await confirmSheet(T('文件在别处被改过'), fsName(e.path) + T(' 在你编辑时被修改了（可能是 AI 改的）。用你的版本覆盖，还是先取消，用“载入最新”查看？'), T('用我的版本覆盖'))) edSave(true);
    return;
  }
  e.sig = m.sig; e.base = e.sent; e.stale = false; e.sent = null; e.saved = true;
  edDrop(e.path);
  if (FS.file && FS.file.res) Object.assign(FS.file.res, { text: e.base, sig: m.sig, size: m.size });
  toast(T('已保存'), e.path, null, 'done');
  FS.git = Object.assign({}, FS.git || {}, { loading: !FS.git });
  fsSend('git'); fsSend('list', fsDir(e.path));
  renderEditorBar();
}
// throw the edit away and edit what is on disk now (onFs starts the editor again when the read arrives)
async function edReload() {
  const e = FS.edit; if (!e) return;
  if (e.text !== e.base && !(await confirmSheet(T('载入磁盘上的最新内容？'), T('你的修改会被丢掉。'), T('载入最新')))) return;
  if (FS.edit !== e) return;
  edDrop(e.path); FS.edit = null; FS.reEdit = e.path;
  fsOpen(e.path, 'read', FS.file && FS.file.force);
}
function renderEditorBar() {
  const host = $('#f-preview'); const e = FS.edit; const bar = host.querySelector('.f-ed-bar'); if (!e || !bar) return;
  const dirty = e.text !== e.base;
  const st = e.saving ? ('<span class="spinner" aria-hidden="true"></span>' + T('保存中…')) : e.stale ? T('磁盘上的文件已被修改') : dirty ? T('未保存') : e.saved ? T('已保存') : '';
  setHTML(bar, '<button type="button" class="f-chip primary" data-fsave' + (e.saving || !dirty ? ' disabled' : '') + ('>' + T('保存') + '</button>') +
    '<button type="button" class="f-chip" data-fcancel>' + (dirty ? T('放弃修改') : T('完成')) + '</button>' +
    (e.stale ? ('<button type="button" class="f-chip warn" data-freload>' + T('载入最新') + '</button>') : '') +
    '<span class="f-ed-st' + (e.stale ? ' warn' : dirty ? ' dirty' : '') + '" role="status">' + st + '</span>' +
    (e.lang ? '<span class="f-lang">' + esc(HL.label(e.lang)) + '</span>' : ''));
}
function renderEditor(host) {
  const e = FS.edit;
  host.classList.add('editing');
  if (!host.querySelector('.f-ed') || host.querySelector('.f-ed').dataset.path !== e.path) {
    host._h = null;
    host.innerHTML = '<div class="f-bar f-ed-bar"></div><div class="f-ed" data-path="' + esc(e.path) + '"><div class="f-ed-gut" aria-hidden="true"></div>' +
      '<div class="f-ed-main"><pre class="f-ed-hl" aria-hidden="true"></pre><textarea class="f-ed-ta" spellcheck="false" autocapitalize="off" ' +
      ('autocomplete="off" autocorrect="off" wrap="off" aria-label="' + T('编辑 ')) + esc(e.path) + '" aria-describedby="f-ed-help"></textarea></div></div>' +
      ('<span id="f-ed-help" class="sr">' + T('Tab 缩进，Shift+Tab 取消缩进，⌘S 或 Ctrl+S 保存；先按 Esc 再按 Tab 离开编辑框。') + '</span>');
    const ta = host.querySelector('.f-ed-ta');
    ta.value = e.text;
    edWire(ta);
    edPaint(true);
  }
  renderEditorBar();
}
let edTimer = null;
function edPaint(now) {
  const host = $('#f-preview'); const e = FS.edit; if (!e) return;
  const pre = host.querySelector('.f-ed-hl'); const gut = host.querySelector('.f-ed-gut'); if (!pre) return;
  const n = e.text.split('\n').length;
  if (gut._n !== n) { gut._n = n; gut.textContent = Array.from({ length: n }, (_, i) => i + 1).join('\n'); }
  const paint = () => { pre.innerHTML = (e.lang && window.HL ? HL.html(e.text, e.lang) : esc(e.text)) + '\n'; edScroll(); };
  clearTimeout(edTimer);
  if (now || e.text.length < 60000) paint(); else edTimer = setTimeout(paint, 250);   // big files: after a pause
}
function edScroll() {
  const host = $('#f-preview'); const ta = host.querySelector('.f-ed-ta'); if (!ta) return;
  const pre = host.querySelector('.f-ed-hl'); const gut = host.querySelector('.f-ed-gut');
  pre.scrollTop = ta.scrollTop; pre.scrollLeft = ta.scrollLeft; gut.scrollTop = ta.scrollTop;
}
// insert as if typed: the browser's undo (⌘Z) keeps working
function edInsert(ta, s) {
  ta.focus();
  if (document.execCommand && document.execCommand('insertText', false, s)) return;       // fires "input" itself
  ta.setRangeText(s, ta.selectionStart, ta.selectionEnd, 'end');
  ta.dispatchEvent(new Event('input'));
}
function edLineStart(v, i) { return i > 0 ? v.lastIndexOf('\n', i - 1) + 1 : 0; }
function edWire(ta) {
  ta.addEventListener('input', () => {
    const e = FS.edit; if (!e) return;
    const was = e.text !== e.base;
    e.text = ta.value;
    edPaint(false);
    if (was !== (e.text !== e.base)) renderEditorBar();
  });
  ta.addEventListener('scroll', edScroll, { passive: true });
  ta.addEventListener('focus', () => $('#view').classList.add('f-typing'));
  ta.addEventListener('blur', () => $('#view').classList.remove('f-typing'));
  let esc1 = false;                                                     // Esc, then Tab leaves the editor (no keyboard trap)
  ta.addEventListener('keydown', (ev) => {
    const e = FS.edit; if (!e || ev.isComposing || ev.keyCode === 229) return;
    const escaped = esc1; esc1 = ev.key === 'Escape';
    if ((ev.metaKey || ev.ctrlKey) && !ev.altKey && ev.key.toLowerCase() === 's') { ev.preventDefault(); edSave(false); return; }
    if (ev.key === 'Tab' && !escaped && !ev.metaKey && !ev.ctrlKey && !ev.altKey) {
      ev.preventDefault();
      const v = ta.value; const a = ta.selectionStart; const b = ta.selectionEnd;
      if (!ev.shiftKey && !v.slice(a, b).includes('\n')) { edInsert(ta, e.indent); return; }
      // indent / outdent every line the selection touches (a selection ending at a line start leaves that line alone)
      const ls = edLineStart(v, a);
      const end = b > a && v[b - 1] === '\n' ? b - 1 : b;
      let le = v.indexOf('\n', end); if (le < 0) le = v.length;
      const un = new RegExp('^(\\t|' + (e.indent === '\t' ? ' {1,4}' : ' {1,' + e.indent.length + '}') + ')');
      const out = v.slice(ls, le).split('\n').map((l) => ev.shiftKey ? l.replace(un, '') : (l ? e.indent + l : l)).join('\n');
      if (out === v.slice(ls, le)) return;
      const single = a === b;
      ta.setSelectionRange(ls, le); edInsert(ta, out);
      if (single) { const p = Math.max(ls, a + out.length - (le - ls)); ta.setSelectionRange(p, p); }
      else ta.setSelectionRange(ls, ls + out.length);
      return;
    }
    if (ev.key === 'Enter' && !ev.shiftKey && !ev.metaKey && !ev.ctrlKey && !ev.altKey) {
      const v = ta.value; const a = ta.selectionStart;
      const ind = /^[ \t]*/.exec(v.slice(edLineStart(v, a), a))[0];
      if (ind) { ev.preventDefault(); edInsert(ta, '\n' + ind); }         // keep the line's indentation
    }
  });
}
window.addEventListener('beforeunload', (ev) => {
  const e = FS.edit; if (!e || e.text === e.base) return;
  edStash(); ev.preventDefault(); ev.returnValue = '';
});
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') edStash(); });

// ---- new file / folder
function fnewOpen() {
  const kind = LS.getItem('glimpse.fnewKind') === 'dir' ? 'dir' : 'file';
  fnewKind(kind);
  $('#fnew-path').value = FS.lastDir ? FS.lastDir + '/' : '';
  $('#fnew-err').textContent = ''; $('#fnew-ok').disabled = false;
  openSheet($('#dlg-fnew'));
  setTimeout(() => { const i = $('#fnew-path'); i.focus(); i.setSelectionRange(i.value.length, i.value.length); }, 60);
}
function fnewKind(k) {
  for (const b of $$('#dlg-fnew [data-fk]')) b.setAttribute('aria-checked', String(b.dataset.fk === k));
  $('#fnew-path').placeholder = k === 'dir' ? T('例如 docs/drafts') : T('例如 docs/notes.md');
  $('#dlg-fnew-title').textContent = k === 'dir' ? T('新建文件夹') : T('新建文件');
}
for (const b of $$('#dlg-fnew [data-fk]')) b.onclick = () => { LS.setItem('glimpse.fnewKind', b.dataset.fk); fnewKind(b.dataset.fk); };
$('#fnew-form').addEventListener('submit', (ev) => {
  ev.preventDefault();
  const dir = $('#dlg-fnew [data-fk="dir"]').getAttribute('aria-checked') === 'true';
  const p = $('#fnew-path').value.trim().replace(/^\/+|\/+$/g, '');
  if (!p || /(^|\/)\.\.?(\/|$)/.test(p)) { $('#fnew-err').textContent = T(dir ? '请填写文件夹名' : '请填写文件名') + T('（不能用 . 或 ..）'); return; }
  $('#fnew-err').textContent = ''; $('#fnew-ok').disabled = true;
  if (!fsSend(dir ? 'mkdir' : 'create', p, false, dir ? null : { text: '' })) { $('#fnew-ok').disabled = false; $('#fnew-err').textContent = T('未连接'); }
});
// open (and reload: one of them may be new) the root and every folder on the way to `path`
function fsReveal(path) {
  let a = ''; const dirs = [''];
  for (const part of path ? path.split('/') : []) { a = a ? a + '/' + part : part; dirs.push(a); }
  for (const d of dirs) { FS.open.add(d); FS.dirs[d] = Object.assign({}, FS.dirs[d] || {}, { loading: !FS.dirs[d] }); fsSend('list', d); }
}
function fnewDone(m) {
  if (!$('#dlg-fnew').open) { if (!m.error) fsRefresh(true); return; }
  $('#fnew-ok').disabled = false;
  if (m.error) { $('#fnew-err').textContent = m.error; return; }
  const p = m.created; const d = $('#dlg-fnew');
  // the modal keeps focus until it has closed (and then gives it back to 新建): the editor takes it after that
  d.addEventListener('close', () => { const ta = $('#f-preview .f-ed-ta'); if (ta && FS.edit && FS.edit.path === p) ta.focus(); }, { once: true });
  closeSheet(d);
  FS.tab = 'tree'; fsSend('git');
  if (m.dir) { fsReveal(p); FS.lastDir = p; renderFiles(); toast(T('已新建文件夹'), p, null, 'done'); return; }
  fsReveal(fsDir(p)); FS.lastDir = fsDir(p);
  // a new empty file: straight into the editor
  FS.file = { path: p, mode: 'read', force: false, loading: false, md: false,
    res: { t: 'fs', action: 'read', path: p, kind: 'text', text: '', sig: m.sig, size: 0, editable: true, encoding: 'utf-8', truncated: false } };
  edStart();
}

function fsRef(path) {
  const t = $('#input'); const ref = '@' + (/\s/.test(path) ? '"' + path + '"' : path);
  t.value = t.value + (t.value && !/\s$/.test(t.value) ? ' ' : '') + ref + ' ';
  autoGrow(); draftSave();
  if (DESKTOP) t.focus();
  toast(T('已插入引用'), ref, null, 'done');
}
$('#fpane').addEventListener('click', (e) => {
  const t = e.target.closest('button'); if (!t || !$('#fpane').contains(t)) return;
  if (t.id === 'f-back') { edLeave().then((ok) => { if (ok) { FS.file = null; renderFiles(); } }); return; }
  if (t.id === 'f-refresh') { fsRefresh(false); return; }
  if (t.id === 'f-new') { fnewOpen(); return; }
  if (t.dataset.ft) { FS.tab = t.dataset.ft; renderFiles(); return; }
  if (t.dataset.dir != null) {
    const p = t.dataset.dir;
    if (FS.open.has(p)) FS.open.delete(p);
    else { FS.open.add(p); FS.lastDir = p; if (!FS.dirs[p] || FS.dirs[p].error) { FS.dirs[p] = { loading: true }; fsSend('list', p); } }
    renderFiles(); return;
  }
  if (t.dataset.file != null) { FS.lastDir = fsDir(t.dataset.file); fsOpen(t.dataset.file, 'read'); return; }
  if (t.dataset.diff != null) { fsOpen(t.dataset.diff, 'diff'); return; }
  if (t.dataset.gdir != null) {
    // an untracked folder: show it in the tree, opened
    const p = t.dataset.gdir; let a = '';
    for (const part of p.split('/')) { a = a ? a + '/' + part : part; if (!FS.open.has(a)) { FS.open.add(a); FS.dirs[a] = { loading: true }; fsSend('list', a); } }
    FS.tab = 'tree'; renderFiles(); return;
  }
  const f = FS.file; if (!f) return;
  if (t.dataset.fedit != null) { edStart(); return; }
  if (t.dataset.fsave != null) { edSave(false); return; }
  if (t.dataset.fcancel != null) { edLeave(); return; }
  if (t.dataset.freload != null) { edReload(); return; }
  if (t.dataset.fshow != null) { fsOpen(f.path, f.mode, true); return; }
  if (t.dataset.fmode) { if (t.dataset.fmode !== f.mode) fsOpen(f.path, t.dataset.fmode, f.force); return; }
  if (t.dataset.fmd) { f.md = t.dataset.fmd === 'render'; LS.setItem('glimpse.mdView', f.md ? 'render' : 'raw'); renderFiles(); return; }
  if (t.dataset.fwrap != null) { FS.wrap = !FS.wrap; renderFiles(); return; }
  if (t.dataset.fref != null) { fsRef(f.path); return; }
  if (t.dataset.fcopy != null) { copyText(f.path, T('路径')); return; }
  if (t.classList.contains('copy-code')) {
    const code = t.parentElement.querySelector('code');
    copyText(code ? code.textContent : '', T('代码'));
  }
});

// ------------------------------------------------------------------ chat rendering
// tiny, safe markdown: everything is escaped first; code fences (highlighted), inline code, bold, italic, strikethrough,
// headings, lists, quotes, rules, tables and http(s) links only (no raw HTML, no images: the page loads nothing remote).
// math ($…$, $$…$$, \(…\), \[…\], ```math) and ```mermaid come out as placeholders with their source; renderRich()
// draws them with KaTeX / mermaid (vendored, loaded the first time one is shown)
function mathSpan(tex, display) {
  return '<span class="math' + (display ? ' display' : '') + '" data-tex="' + esc(tex) + '">' + esc(display ? tex : '$' + tex + '$') + '</span>';
}
// $…$ only when it can't be money: no space just inside the $, no digit right after the closing one, and not only
// a number ("$5 and $10" stays text). \$ is a literal dollar.
const MATH_INLINE = /\\\$|\\\((.+?)\\\)|\$(?!\s)((?:\\.|[^$\n\\])+?)\$(?!\d)/g;
// code spans as in CommonMark: a run of N backticks up to the next run of exactly N, so ``a`b`` and ```` ```js ````
// are code too. -> [[isCode, text, raw], ...]; one space inside both ends is dropped (`` `x` `` shows `x`)
function codeSplit(s) {
  const out = []; let i = 0; let from = 0;
  while (i < s.length) {
    if (s[i] !== '`') { i++; continue; }
    let j = i; while (s[j] === '`') j++;
    let k = j; let end = -1;
    while (k < s.length) {
      if (s[k] !== '`') { k++; continue; }
      let e = k; while (s[e] === '`') e++;
      if (e - k === j - i) { end = k; break; }
      k = e;
    }
    if (end < 0) { i = j; continue; }                      // no closing run: the backticks are text
    if (i > from) out.push([false, s.slice(from, i), s.slice(from, i)]);
    let c = s.slice(j, end);
    if (c.length >= 2 && c[0] === ' ' && c[c.length - 1] === ' ' && c.trim()) c = c.slice(1, -1);
    out.push([true, c, s.slice(i, end + j - i)]);
    i = from = end + j - i;
  }
  if (from < s.length) out.push([false, s.slice(from), s.slice(from)]);
  return out;
}
let MD_LINK = null;          // the vault view's link resolver while it renders a note (kbMd), else null
function mdInline(s, math) {
  const keep = [];                                                // finished pieces, out of the way of later rules
  const hold = (html) => '\u0002' + (keep.push(html) - 1) + '\u0003';
  const link = (text, url) => '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + text + '</a>';
  let t = String(s).replace(/[\u0002\u0003\u0004\u0005]/g, (c) => (math && (c === '\u0004' || c === '\u0005') ? c : ''));
  // code spans first (a $ or * in code is code), then math, both out of the way before escaping
  t = codeSplit(t).map(([code, x]) => (code ? hold('<code>' + esc(x) + '</code>') : x)).join('');
  if (math) {
    t = t.replace(/\u0004(\d+)\u0005/g, (_, k) => hold(mathSpan(math[+k], true)))
      .replace(MATH_INLINE, (m0, paren, dollar) => {
        if (m0 === '\\$') return '$';
        const tex = paren != null ? paren : dollar;
        if (paren == null && (/\s$/.test(tex) || /^[\d.,\s]+$/.test(tex))) return m0;
        return hold(mathSpan(tex, false));
      });
  }
  let h = esc(t);
  if (MD_LINK) {
    h = h.replace(/(!?)\[([^\]\n]*)\]\(([^\s()]+)\)|\[\[([^\]\n|]+)(?:\|([^\]\n]+))?\]\]/g, (m0, bang, label, href, wiki, alias) => {
      const r = MD_LINK(bang, label, href, wiki, alias); return r == null ? m0 : hold(r);
    });
  }
  h = h
    .replace(/!?\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, t, u) => hold(link(t, u)))
    .replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>')
    .replace(/(^|[^\w*])\*([^*\s][^*\n]*?)\*(?![\w*])/g, '$1<i>$2</i>')
    .replace(/~~([^~\n]+)~~/g, '<s>$1</s>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, (_, pre, u) => pre + hold(link(u, u)));
  // a held piece can hold another (`code` inside [link text](url)): put them back until none is left
  for (let n = 0; n < 4 && /\u0002\d+\u0003/.test(h); n++) h = h.replace(/\u0002(\d+)\u0003/g, (_, k) => keep[+k]);
  return h;
}
function codeBlock(body, lang) {
  // highlighted when the fence names a language we know (always the dark palette: the block is dark)
  const code = window.HL && HL.lang(lang) ? HL.html(body, lang) : esc(body);
  return '<pre>' + (lang ? '<span class="lang">' + esc(lang) + '</span>' : '') +
    ('<button type="button" class="copy-code" aria-label="' + T('复制代码') + '">') + ic('copy', 'sm') + ('<span>' + T('复制') + '</span></button>') +
    '<code>' + code + '</code></pre>';
}
// a fenced block: ``` or ~~~ (3 or more) at the start of a line, closed by a line of the same character, at least
// as long (CommonMark). Backticks in the middle of a line are code spans, never fences.
// (~~~ only with nothing or one word after it: "~~~ 分割线 ~~~" stays text)
const MD_FENCE = /^(\s*)(?:(`{3,})\s*([^\s`]*)[^`]*|(~{3,})\s*([^\s~`]*)\s*)$/;
function mdFence(body, lang, closed) {
  const ll = lang.toLowerCase();
  if (ll === 'math') return '<div class="math-block">' + mathSpan(body, true) + '</div>';
  // closed fence only (a reply still being written may stop inside the diagram)
  if (ll === 'mermaid' && closed) {
    return '<div class="mermaid-box" data-src="' + esc(body) + '"><div class="mm-bar"><span class="lang">mermaid</span>' +
      ('<button type="button" class="mm-toggle" aria-pressed="false">' + T('源码') + '</button></div><div class="mm-view" role="img" aria-label="' + T('Mermaid 图') + '"></div>') +
      codeBlock(body, '') + '</div>';
  }
  return codeBlock(body, lang);
}
function md(text) {
  const lines = String(text || '').replace(/\r\n?/g, '\n').replace(/[\u0002-\u0007]/g, '').split('\n');
  let h = ''; let prose = [];
  for (let k = 0; k < lines.length; k++) {
    const f = lines[k].match(MD_FENCE);
    if (!f) { prose.push(lines[k]); continue; }
    if (prose.length) { h += mdProse(prose.join('\n')); prose = []; }
    const ind = f[1].length; const mark = f[2] || f[4]; const lang = f[3] || f[5] || '';
    const body = []; let closed = false;
    for (k++; k < lines.length; k++) {
      const c = lines[k].match(/^\s*(`{3,}|~{3,})\s*$/);
      if (c && c[1][0] === mark[0] && c[1].length >= mark.length) { closed = true; break; }
      body.push(ind ? lines[k].replace(new RegExp('^ {0,' + ind + '}'), '') : lines[k]);
    }
    h += mdFence(body.join('\n'), lang, closed);
  }
  if (prose.length) h += mdProse(prose.join('\n'));
  return h;
}
function mdProse(p) {
  let h = '';
  {
    // display math ($$…$$, \[…\]) can span lines: taken out first, put back as a block (alone on its line) or inline.
    // Code spans are set aside meanwhile (a $$ in code is code).
    const math = []; const codes = [];
    p = p.split('\n').map((ln) => codeSplit(ln).map(([code, , raw]) => (code ? '\u0006' + (codes.push(raw) - 1) + '\u0007' : raw)).join('')).join('\n')
      .replace(/\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]/g, (m0, a, b) => '\u0004' + (math.push((a != null ? a : b).trim()) - 1) + '\u0005');
    const unc = (s) => s.replace(/\u0006(\d+)\u0007/g, (_, k) => codes[+k]);
    p = unc(p);
    for (let i = 0; i < math.length; i++) math[i] = unc(math[i]);
    const inl = (s) => mdInline(s, math);
    let list = null;
    const close = () => { if (list) { h += '</' + list + '>'; list = null; } };
    let para = [];
    const flush = () => { if (para.length) { h += '<p>' + para.map(inl).join('<br>') + '</p>'; para = []; } };
    const lines = p.split('\n');
    for (let k = 0; k < lines.length; k++) {
      const line = lines[k];
      let mm;
      if (mdTableStart(lines, k)) {                               // | a | b |  +  | --- | :-: |
        flush(); close();
        const head = mdCells(line); const al = mdCells(lines[k + 1]).map((c) => /^:-+:$/.test(c) ? ' class="ta-c"' : /-:$/.test(c) ? ' class="ta-r"' : '');
        k += 2;
        const rows = [];
        while (k < lines.length && lines[k].includes('|') && lines[k].trim()) rows.push(mdCells(lines[k++]));
        k--;
        h += '<div class="md-table"><table><thead><tr>' + head.map((c, j) => '<th' + (al[j] || '') + '>' + inl(c) + '</th>').join('') +
          '</tr></thead><tbody>' + rows.map((r) => '<tr>' + head.map((_, j) => '<td' + (al[j] || '') + '>' + inl(r[j] || '') + '</td>').join('') + '</tr>').join('') +
          '</tbody></table></div>';
      }
      else if ((mm = line.match(/^\s*\u0004(\d+)\u0005\s*$/))) { flush(); close(); h += '<div class="math-block">' + mathSpan(math[+mm[1]], true) + '</div>'; }
      else if ((mm = line.match(/^(#{1,4})\s+(.*)$/))) { flush(); close(); h += '<h' + mm[1].length + '>' + inl(mm[2]) + '</h' + mm[1].length + '>'; }
      else if (/^ {0,3}([-*_])(?:\s*\1){2,}\s*$/.test(line)) { flush(); close(); h += '<hr>'; }
      else if ((mm = line.match(/^\s*>\s?(.*)$/))) {
        flush(); close();
        const q = [mm[1]];
        while (k + 1 < lines.length && (mm = lines[k + 1].match(/^\s*>\s?(.*)$/))) { q.push(mm[1]); k++; }
        h += '<blockquote>' + q.map(inl).join('<br>') + '</blockquote>';
      }
      else if ((mm = line.match(/^\s*[-*•]\s+(.*)$/))) { flush(); if (list !== 'ul') { close(); h += '<ul>'; list = 'ul'; } h += '<li>' + inl(mm[1]) + '</li>'; }
      else if ((mm = line.match(/^\s*(\d+)[.)]\s+(.*)$/))) {
        // a list interrupted by a code block goes on with its own number (3. after a block stays 3.)
        flush(); if (list !== 'ol') { close(); h += mm[1] !== '1' ? '<ol start="' + (+mm[1]) + '">' : '<ol>'; list = 'ol'; }
        h += '<li>' + inl(mm[2]) + '</li>';
      }
      else if (!line.trim()) { flush(); close(); }
      else { close(); para.push(line); }
    }
    flush(); close();
  }
  return h;
}
// a GFM table: a row with "|" followed by a delimiter row like "| --- | :-: |"
const MD_DELIM = /^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*$|^\s*\|\s*:?-+:?\s*\|\s*$/;
function mdTableStart(lines, k) { return lines[k].includes('|') && k + 1 < lines.length && MD_DELIM.test(lines[k + 1]); }
function mdCells(line) {
  let s = line.trim().replace(/\\\|/g, '\u0001');                // \| is a "|" inside a cell
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|')) s = s.slice(0, -1);
  return s.split('|').map((c) => c.trim().replace(/\u0001/g, '|'));
}
// ---- math (KaTeX) and diagrams (mermaid): vendored under /static/vendor, loaded the first time one is shown
// (mermaid is big: ~1.5 MB gzipped). Until then, and if drawing fails, the source stays visible.
const VENDOR = {};
function vendor(name) {
  if (VENDOR[name]) return VENDOR[name];
  const files = { katex: ['katex/katex.min.css', 'katex/katex.min.js'], mermaid: ['mermaid/mermaid.min.js'] }[name];
  VENDOR[name] = Promise.all(files.map((f) => new Promise((ok, bad) => {
    const el = f.endsWith('.css') ? Object.assign(document.createElement('link'), { rel: 'stylesheet', href: '/static/vendor/' + f })
      : Object.assign(document.createElement('script'), { src: '/static/vendor/' + f, async: true });
    el.onload = ok; el.onerror = () => bad(new Error(f));
    document.head.appendChild(el);
  }))).then(() => window[name]);
  VENDOR[name].catch(() => { delete VENDOR[name]; });       // a failed load is tried again next time
  return VENDOR[name];
}
function darkScheme() {
  const t = document.documentElement.getAttribute('data-theme');
  return t ? t === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
}
const MM_CACHE = new Map();                                 // theme + source -> svg (chat re-renders a message often)
let mmQueue = Promise.resolve(); let mmSeq = 0;
function mermaidSvg(src) {
  const theme = darkScheme() ? 'dark' : 'default';
  const key = theme + '\n' + src;
  if (MM_CACHE.has(key)) return MM_CACHE.get(key);
  // one diagram at a time (mermaid keeps global state), with the theme of the moment
  const p = mmQueue = mmQueue.catch(() => {}).then(() => vendor('mermaid')).then((mm) => {
    // labels as plain SVG text (no HTML in them at all), strict: no click handlers / links into scripts
    // (dark: pie slices in the app's colours; mermaid's first dark slice is nearly black)
    const pie = theme === 'dark' ? Object.fromEntries(['#5AA9FF', '#C792EA', '#3DDC6A', '#FFB547', '#56D4DD', '#FF6B63', '#8CC4FF', '#DDB3F5']
      .map((c, i) => ['pie' + (i + 1), c])) : {};
    mm.initialize({ startOnLoad: false, securityLevel: 'strict', theme, suppressErrorRendering: true, htmlLabels: false,
      flowchart: { htmlLabels: false }, themeVariables: pie, fontFamily: getComputedStyle(document.body).fontFamily });
    return mm.render('mm' + (++mmSeq), src);
  }).then((r) => cleanSvg(r.svg));
  MM_CACHE.set(key, p);
  if (MM_CACHE.size > 60) MM_CACHE.delete(MM_CACHE.keys().next().value);
  p.catch(() => MM_CACHE.delete(key));
  return p;
}
// belt and braces on top of mermaid's own sanitising: no scripts, embedded HTML, event handlers or script links
function cleanSvg(svg) {
  const doc = new DOMParser().parseFromString(svg, 'image/svg+xml');
  const root = doc.documentElement;
  if (!root || root.nodeName !== 'svg') throw new Error('not svg');
  for (const el of root.querySelectorAll('script, foreignObject, iframe, object, embed, img, image')) el.remove();
  for (const el of [root, ...root.querySelectorAll('*')]) {
    for (const a of [...el.attributes]) {
      if (/^on/i.test(a.name) || (/href$/i.test(a.name) && !/^#/.test(a.value.trim()))) el.removeAttribute(a.name);
    }
  }
  return new XMLSerializer().serializeToString(root);
}
function renderRich(root) {
  if (!root) return;
  const maths = $$('.math:not([data-done])', root);
  if (maths.length) {
    for (const el of maths) el.dataset.done = '1';
    vendor('katex').then((katex) => {
      for (const el of maths) {
        try {
          el.innerHTML = katex.renderToString(el.dataset.tex, { displayMode: el.classList.contains('display'), throwOnError: false,
            trust: false, strict: 'ignore', maxExpand: 500, output: 'htmlAndMathml' });
        } catch (_) { el.classList.add('bad'); }
      }
    }).catch(() => { for (const el of maths) delete el.dataset.done; });
  }
  for (const box of $$('.mermaid-box:not([data-done])', root)) {
    box.dataset.done = '1';
    mermaidSvg(box.dataset.src).then((svg) => {
      const v = box.querySelector('.mm-view'); if (!v) return;
      v.innerHTML = svg;                                    // mermaid's own output, sanitised (securityLevel strict)
      box.classList.add('drawn');
    }).catch(() => { box.classList.add('failed'); const v = box.querySelector('.mm-view'); if (v) v.textContent = T('图表语法有误，显示源码'); });
  }
}
document.addEventListener('click', (e) => {
  const b = e.target.closest('.mm-toggle'); if (!b) return;
  const box = b.closest('.mermaid-box'); const src = !box.classList.contains('src');
  box.classList.toggle('src', src); b.setAttribute('aria-pressed', String(src)); b.textContent = src ? T('图') : T('源码');
});
// the theme changed: diagrams are drawn again in its colours
const themeHook = window.onThemeApplied;
window.onThemeApplied = () => {
  if (themeHook) themeHook();
  for (const box of $$('.mermaid-box[data-done]')) { delete box.dataset.done; box.classList.remove('drawn', 'failed'); }
  renderRich(document.body);
};
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => window.onThemeApplied());
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
    renderRich(el);
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
    setHTML(el, '<div class="rh">' + (it.error ? T('错误输出') : T('输出')) + (short ? '' : ' · ' + lines + T(' 行') + ic('down')) + '</div><pre>' + esc(it.text) + '</pre>');
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
  setHTML(e, ic(s && s.kind === 'shell' ? 'term' : 'chat', 'lg') + '<div>' + (s && s.kind === 'shell' ? T('Shell 会话没有对话记录，切到“终端”查看') :
    (T('还没有对话。') + '<br>' + T('在下面输入第一条指令吧'))) + '</div>');
}
$('#chat').addEventListener('scroll', (e) => { if (nearBottom(e.target)) $('#jump').hidden = true; }, { passive: true });
// copy a code block (delegated: messages are re-rendered). The text comes from the <code>, not the button label
$('#chat').addEventListener('click', (e) => {
  const b = e.target.closest('.copy-code'); if (!b) return;
  e.preventDefault(); e.stopPropagation();
  const code = b.parentElement.querySelector('code');
  copyText(code ? code.textContent : '', T('代码')).then(() => {
    b.classList.add('done'); b.lastChild.textContent = T('已复制');
    setTimeout(() => { b.classList.remove('done'); b.lastChild.textContent = T('复制'); }, 1500);
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
    live.innerHTML = ('<summary><span class="live-dot" aria-hidden="true"></span>' + T('实时屏幕')) + ic('down') + ('</summary><pre aria-label="' + T('终端当前画面') + '"></pre>');
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
  if (s && (s.pending || s.question) && $('#view').classList.contains('files-only')) {
    // the files tab on a phone: a one-line reminder instead of the full card (it would cover the list)
    const q = s.question;
    sig = 'mini:' + (s.pending ? s.pending.id : q.question + ':' + (q.qi || 0) + (q.review ? 'r' : ''));
    html = '<button type="button" class="acard mini" data-amini>' + '<span class="a-ic">' + ic(s.pending ? 'shield' : 'help') + '</span>' +
      '<span class="grow"><b>' + (s.pending ? esc(s.pending.tool) + T(' 请求授权') : T('有问题需要回答')) + '</b><span>' +
      esc(s.pending ? s.pending.summary : q.review ? T('确认答案并提交') : q.question) + ('</span></span><span class="btn sm">' + T('去处理') + '</span></button>');
  } else if (s && s.pending) {
    const p = s.pending; sig = 'p:' + p.id + ':' + (v.approving || '');
    const busy = (d) => (v.approving === d ? '<span class="spinner" aria-hidden="true"></span>' : '');
    const dis = v.approving ? ' disabled' : '';
    html = '<section class="acard" role="alertdialog" aria-labelledby="a-s" aria-describedby="a-d">' +
      '<div class="a-h"><span class="a-ic">' + ic('shield') + '</span><div class="a-t"><div class="a-k">' + esc(p.tool) + (T(' 请求授权') + '</div>') +
      '<div class="a-s" id="a-s">' + esc(p.summary) + '</div></div></div>' +
      (p.detail ? '<pre class="a-d" id="a-d" tabindex="0">' + diffHTML(p.detail) + '</pre>' : '') +
      '<div class="a-btns">' +
      '<button type="button" class="btn primary" data-a="allow"' + dis + '>' + busy('allow') + ic('check') + (T('允许') + '</button>') +
      '<button type="button" class="btn danger" data-a="deny"' + dis + '>' + busy('deny') + ic('x') + (T('拒绝') + '</button>') +
      (p.can_always ? '<button type="button" class="btn" data-a="always"' + dis + '>' + busy('always') + (T('总是允许') + '</button>') : '') +
      '<button type="button" class="btn ghost' + (p.can_always ? '' : ' wide') + '" data-a="terminal"' + dis + ('>' + T('在终端处理') + '</button>') +
      '</div></section>';
  } else if (s && s.question && ((s.question.options && s.question.options.length) || s.question.review)) {
    // AskUserQuestion. With several questions the agent follows claude's picker: qi = the question shown,
    // answered[], and review = the final "Submit answers" page
    const q = s.question; const qs = q.questions || null; const n = qs ? qs.length : 1; const qi = q.qi || 0;
    sig = 'q:' + q.question + ':' + qi + ':' + (q.review ? 'r' : '') + ':' + (q.answered || []).join(',');
    if (v && v.qFor !== (qs ? qs[0].question : q.question)) { v.qFor = qs ? qs[0].question : q.question; v.qAns = {}; }
    const steps = qs ? '<div class="q-steps" aria-hidden="true">' + qs.map((x, i) => '<span class="' + (q.review ? 'done' : i === qi ? 'cur' : (q.answered || [])[i] ? 'done' : '') +
      '">' + esc(x.header || String(i + 1)) + '</span>').join('') + '<span class="' + (q.review ? 'cur' : '') + ('">' + T('提交') + '</span></div>') : '';
    const kicker = qs ? (q.review ? T('确认答案 · ') + n + T(' 个问题') : T('问题 ') + (qi + 1) + ' / ' + n + (q.header ? ' · ' + q.header : '')) : (q.header || T('提问'));
    if (q.review) {
      const sum = (q.summary || qs.map((x) => ({ q: x.question, a: '' }))).map((r, i) => '<div class="q-sum"><div class="od">' + esc(r.q) + '</div><div class="ol">' +
        esc(r.a || (v && v.qAns && v.qAns[i]) || '—') + '</div></div>').join('');
      const sub = (q.submit || [{ n: 1, label: 'Submit answers' }, { n: 2, label: 'Cancel' }]);
      html = '<section class="acard q" role="group" aria-labelledby="q-s">' +
        '<div class="a-h"><span class="a-ic">' + ic('help') + '</span><div class="a-t"><div class="a-k">' + esc(kicker) + '</div>' +
        ('<div class="a-s" id="q-s">' + T('确认后提交给 ')) + esc(KIND[s.kind] || s.kind) + '</div></div></div>' + steps + '<div class="q-sums">' + sum + '</div><div class="a-btns">' +
        sub.map((o) => { const cancel = /cancel/i.test(o.label); return '<button type="button" class="btn ' + (cancel ? 'ghost' : 'primary') + '" data-q="' + esc(o.n) + '">' +
          (cancel ? T('取消') : /submit/i.test(o.label) ? ic('check') + T('提交答案') : esc(o.label)) + '</button>'; }).join('') +
        '<button type="button" class="btn wide" data-qk="left">' + ic('back') + (T('返回修改') + '</button></div></section>');
    } else {
      html = '<section class="acard q" role="group" aria-labelledby="q-s">' +
        '<div class="a-h"><span class="a-ic">' + ic('help') + '</span><div class="a-t"><div class="a-k">' + esc(kicker) + (q.multi ? T(' · 可多选') : '') + '</div>' +
        '<div class="a-s" id="q-s">' + esc(q.question) + '</div></div></div>' + steps + '<div class="opts">' +
        q.options.map((o, i) => '<button type="button" class="opt" data-q="' + (i + 1) + '" data-ql="' + esc(o.label) + '"><span class="n">' + (i + 1) + '</span><span class="grow"><div class="ol">' +
          esc(o.label) + '</div>' + (o.description ? '<div class="od">' + esc(o.description) + '</div>' : '') + '</span></button>').join('') +
        ('<button type="button" class="opt other" data-q="other"><span class="n">…</span><span class="grow"><div class="ol">' + T('其他') + '</div><div class="od">' + T('在下方输入框回复') + '</div></span></button>') +
        '</div>' + (qs ? '<div class="a-btns q-nav"><button type="button" class="btn" data-qk="left"' + (qi === 0 ? ' disabled' : '') + '>' + ic('back') + (T('上一题') + '</button>') +
          '<button type="button" class="btn" data-qk="right">' + (q.multi ? T('选好了 · ') : '') + T('下一题') + ic('chev') + '</button></div>' : '') + '</section>';
    }
  } else if (s && s.menu && S.mode === 'chat' && Array.isArray(s.menu.options) && s.menu.options.length) {
    // interactive TUI menu (claude /model, /effort slider, codex /model) detected by the agent from the screen
    const mn = s.menu; sig = 'm:' + JSON.stringify(mn) + ':' + (v.menuBusy == null ? '' : v.menuBusy);
    const dis = v.menuBusy != null ? ' disabled' : '';
    const canS = s.kind === 'claude' && /s to use|s for this session/i.test(mn.hint || '');
    const slider = mn.kind === 'slider';
    html = '<section class="acard mcard' + (slider ? ' slider' : '') + '" role="group" aria-labelledby="mn-t">' +
      '<div class="a-h"><span class="a-ic">' + ic(slider ? 'sliders' : 'menu-list') + ('</span><div class="a-t"><div class="a-k">' + T('终端菜单 · ')) + esc(KIND[s.kind] || s.kind) + '</div>' +
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
      '<div class="a-btns">' + '<button type="button" class="btn' + (canS ? '' : ' wide') + '" data-mk="esc"' + dis + ('>' + T('Esc · 取消') + '</button>') +
      (canS ? '<button type="button" class="btn" data-mk="s"' + dis + ('>' + T('s · 仅本会话') + '</button>') : '') + '</div></section>';
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
  for (const b of $$('[data-amini]', host)) b.onclick = () => setMode('chat');
  for (const b of $$('[data-q]', host)) b.onclick = () => {
    if (b.dataset.q === 'other') { $('#input').focus(); return; }
    sendBytes(enc.encode(b.dataset.q));
    if (b.dataset.ql && v.qAns && !s.question.multi) v.qAns[s.question.qi || 0] = b.dataset.ql;   // for the summary
    // single choice / submit: the card goes away; with more questions the next one (or the submit page)
    // comes back as soon as claude shows it
    if (!s.question.multi || s.question.review) { v.cardSig = null; host.innerHTML = ''; }
  };
  // ← / → move between the questions (claude: "Tab/Arrow keys to navigate")
  for (const b of $$('[data-qk]', host)) b.onclick = () => { sendBytes(enc.encode(KEYS[b.dataset.qk])); v.cardSig = null; host.innerHTML = ''; };
  if (html && !had) {
    announce(s.pending ? T('需要授权：') + s.pending.summary : s.question ? (s.question.review ? T('确认答案并提交') : T('有问题需要回答：') + s.question.question) : T('终端菜单：') + (s.menu.title || ''));
    if (navigator.vibrate) navigator.vibrate(30);
  }
}
function approve(decision) {
  const v = S.view; const s = v && session(v.m, v.sid);
  if (!s || !s.pending || v.approving) return;
  v.approving = decision; v.approvingId = s.pending.id; renderCard();
  if (!send({ op: 'approve', m: v.m, sid: v.sid, id: s.pending.id, decision })) { v.approving = null; renderCard(); toast(T('未连接'), T('稍后再试'), null, 'error'); }
  setTimeout(() => { if (S.view === v && v.approving) { v.approving = null; v.cardSig = null; renderCard(); } }, 8000);
}

// menu card: op menu {index} moves the TUI cursor there and presses Enter; {key:'esc'|'s'} sends Esc / "s". Reply menu_ok
function menuSend(arg, busyIdx) {
  const v = S.view; const s = v && session(v.m, v.sid);
  if (!s || !s.menu || v.menuBusy != null) return;
  if (!send(Object.assign({ op: 'menu', m: v.m, sid: v.sid }, arg))) { toast(T('未连接'), T('稍后再试'), null, 'error'); return; }
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
      if (!send({ op: 'text', m: vv.m, sid: vv.sid, text: b.dataset.cmd })) toast(T('未连接'), T('稍后再试'), null, 'error');
    };
  }
}
$('#btn-slash').addEventListener('mousedown', (e) => e.preventDefault());
$('#btn-slash').onclick = () => slashOpen($('#slash-pop').hidden);
document.addEventListener('click', (e) => { if (!$('#slash-pop').hidden && !e.target.closest('#slash-pop, #btn-slash')) slashOpen(false); });

// ------------------------------------------------------------------ input: quick keys + composer
// ---- quick keys: user-configurable bar (Settings → 快捷键). Built-ins by id, plus custom keys {label, send}.
const KEY_CATALOG = [
  { id: 'paste', label: T('粘贴'), aria: T('粘贴剪贴板') },
  { id: 'esc', label: 'Esc' }, { id: 'ctrlc', label: '^C', aria: T('Control C 中断') }, { id: 'tab', label: 'Tab' },
  { id: 'stab', label: '⇧Tab', aria: 'Shift Tab' }, { id: 'up', label: '↑', aria: T('上') }, { id: 'down', label: '↓', aria: T('下') },
  { id: 'left', label: '←', aria: T('左') }, { id: 'right', label: '→', aria: T('右') }, { id: 'enter', label: '⏎', aria: T('回车') },
  { id: '1', label: '1' }, { id: '2', label: '2' }, { id: '3', label: '3' }, { id: 'y', label: 'y' }, { id: 'n', label: 'n' },
  { id: 'ctrld', label: '^D', aria: 'Control D' }, { id: 'ctrll', label: '^L', aria: T('Control L 清屏') },
  { id: 'ctrlr', label: '^R', aria: 'Control R' }, { id: 'ctrlz', label: '^Z', aria: 'Control Z' },
  { id: 'ctrlu', label: '^U', aria: T('Control U 清空输入') }, { id: 'pgup', label: 'PgUp' }, { id: 'pgdn', label: 'PgDn' },
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
  toast(T('已粘贴'), t.length > 80 ? t.slice(0, 77) + '…' : t, null, 'done');
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
  btn.setAttribute('aria-label', has ? T('发送') : T('发送回车'));
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
  input.value = ''; autoGrow(); draftSave();
  const c = $('#chat'); c.scrollTop = c.scrollHeight;
});
function renderOutbox() {
  const box = $('#chat'); let el = $('.outbox', box);
  const v = S.view;
  const mine = v ? S.outbox.filter((o) => o.m === v.m && o.sid === v.sid) : [];
  if (!mine.length) { if (el) el.remove(); return; }
  if (!el) { el = document.createElement('div'); el.className = 'outbox'; }
  box.insertBefore(el, $('.livebox', box));
  setHTML(el, mine.map((o) => '<div class="msg user pending">' + mdInline(o.text).replace(/\n/g, '<br>') + ('<span class="ts"><span class="spinner" aria-hidden="true"></span>' + T('发送中…') + '</span></div>')).join(''));
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
  $('#act-restart-sub').textContent = s ? T('当前：') + (s.key || T('默认')) + (s.can_resume ? T(' · 可恢复对话') : '') : '';
  $('#act-fit-sub').textContent = s ? T('当前 ') + s.cols + '×' + s.rows : '';
  $('#act-ac-row').hidden = !ai;
  const mc = v && S.byId[v.m];
  $('#act-wf').hidden = !ai || !(mc && mc.vault) || !!s.vault;     // already a workflow session: nothing to turn on
  $('#act-kb').hidden = !s || !s.vault || !kbOn();
  if (s && s.vault) $('#act-kb-sub').textContent = s.vault + ' · ' + T('实验记录、日志、文档');
  $('#act-wf-sub').textContent = s && s.status === 'running' ? T('建 git 仓库、登记到知识库；这一轮结束后重启并接上原对话')
    : T('建 git 仓库、登记到知识库，然后重启并接上原对话');
  if (ai) {
    $('#act-ac').checked = !!s.auto_continue;
    $('#act-ac-sub').textContent = s.auto_continue ? (s.ac_count ? T('已连着出错并自动继续 %a 次（会一直重试；AI 正常回复完一次或你发了消息后从 0 算起）', { a: s.ac_count }) : T('已开启：遇到临时错误会自动发送“继续”'))
      : T('服务器过载、流中断、输出被截断时自动发送“继续”；Key 余额不足等仍会通知你');
  }
  const running = s && s.status !== 'exited' && s.status !== 'error';
  $('#act-kill').hidden = !running;
  $('#act-delete').disabled = !s;
  $('#act-delete-sub').textContent = running ? T('会先结束进程') : '';
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
  if (ok) toast(T('已复制') + (what ? SP + what : ''), preview, null, 'done'); else toast(T('无法复制'), T('请长按选中后手动复制'), null, 'error');
  return ok;
}
$('#act-ac').onchange = (e) => {
  const v = S.view; if (!v) return;
  send({ op: 'auto_continue', m: v.m, sid: v.sid, on: e.target.checked });
  toast(e.target.checked ? T('已开启自动继续') : T('已关闭自动继续'), '', null, 'done');
};
$('#act-cmd-copy').onclick = () => { const v = S.view; if (v) copyText(attachCmd(v.m, v.sid), T('命令')); };
$('#act-link').onclick = () => { const v = S.view; if (v) copyText(deepLink(v.m, v.sid), T('链接')); };
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
// ---- terminal size: fit to this screen, back to the size before the last change, or a common size
const SIZE_PRESETS = [[100, 32], [120, 40], [160, 48], [200, 56]];
function screenSize() {
  const wrap = $('#term-wrap');
  let cw = S.fontSize * 0.6; let ch = S.fontSize * 1.2;
  try { const c = term._core._renderService.dimensions.css.cell; if (c.width) { cw = c.width; ch = c.height; } } catch (_) {}
  const cols = Math.floor((wrap.clientWidth - 12) / cw); const rows = Math.floor((wrap.clientHeight - 12) / ch);
  return cols > 0 && rows > 0 ? [Math.max(40, cols), Math.max(10, rows)] : null;
}
function prevSizeKey(v) { return 'glimpse.prevSize.' + v.m + '/' + v.sid; }
function setTermSize(cols, rows) {
  const v = S.view; const s = v && session(v.m, v.sid); if (!s) return;
  if (s.cols !== cols || s.rows !== rows) LS.setItem(prevSizeKey(v), JSON.stringify([s.cols, s.rows]));
  send({ op: 'resize', m: v.m, sid: v.sid, cols, rows });
  closeSheet($('#dlg-size'));
  toast(T('终端尺寸'), cols + ' × ' + rows, null, 'done');
}
function openSizeSheet() {
  const v = S.view; const s = v && session(v.m, v.sid); if (!s) return;
  setMode('term');
  requestAnimationFrame(() => {
    const fit = screenSize();
    let prev = null; try { prev = JSON.parse(LS.getItem(prevSizeKey(v)) || 'null'); } catch (_) {}
    const rows = [];
    if (prev && (prev[0] !== s.cols || prev[1] !== s.rows)) rows.push([T('恢复到之前的尺寸'), prev, T('改尺寸之前是 ') + prev[0] + ' × ' + prev[1]]);
    if (fit) rows.push([T('适应这块屏幕'), fit, T('不用左右滑动，每行字更少')]);
    for (const p of SIZE_PRESETS) rows.push([p[0] + ' × ' + p[1], p, p[0] === 120 ? T('常用的电脑终端大小') : '']);
    setHTML($('#size-opts'), ('<div class="cell"><span class="grow">' + T('当前') + '<div class="sub">') + s.cols + ' × ' + s.rows + '</div></span></div>' +
      rows.map(([t, sz, sub], i) => '<button type="button" class="cell" data-size="' + i + '"><span class="grow">' + esc(t) +
        (sub ? '<div class="sub">' + esc(sub) + '</div>' : '') + '</span>' + (sz[0] === s.cols && sz[1] === s.rows ? ic('check', 'chk on') : '') + '</button>').join(''));
    for (const b of $$('#size-opts [data-size]')) b.onclick = () => { const sz = rows[+b.dataset.size][1]; setTermSize(sz[0], sz[1]); };
    openSheet($('#dlg-size'));
  });
}
$('#act-fit').onclick = () => { closeSheet($('#dlg-more')); setTimeout(openSizeSheet, 230); };
$('#act-kill').onclick = async () => {
  const v = S.view; if (!v) return;
  closeSheet($('#dlg-more'));
  await new Promise((r) => setTimeout(r, 230));
  if (await confirmSheet(T('结束这个进程？'), T('正在运行的任务会被中断，对话记录会保留。'), T('结束进程'))) send({ op: 'kill', m: v.m, sid: v.sid });
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
    esc(m.id + ' · ' + m.name + (m.online ? '' : T('（离线）'))) + '</option>').join('');
  const ok = S.machines.find((m) => m.id === prev && m.online) || S.machines.find((m) => m.online);
  if (ok) sel.value = ok.id;
  fillNewDeps();
}
function fillNewDeps() {
  const mc = S.byId[$('#new-machine').value]; const kind = newKind();
  const ai = kind !== 'shell';
  $('#new-key-row').hidden = !ai || !S.keys.length;
  // project workflow: only where the vault is (the hub's computer, vault-linker configured), for Claude / Codex
  const wf = ai && !!(mc && mc.vault);
  $('#new-wf-row').hidden = !wf;
  if (!wf) $('#new-wf').checked = false;
  $('#new-wf-pub-row').hidden = !wf || !$('#new-wf').checked;
  const def = mc && mc.presets && mc.presets[kind] && mc.presets[kind].key;
  $('#new-key').innerHTML = '<option value="">' + (kbState().mode === 'least_used' ? T('自动（用量最低的 Key）') : T('默认') + (def ? T('（') + esc(def) + T('）') : '')) + '</option>' +
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
  if (!send({ op: 'models', m, kind })) { MODELS[k] = { st: 'err', msg: T('未连接到 hub') }; return; }
  const e = MODELS[k] = { st: 'loading' };
  e.timer = setTimeout(() => { if (MODELS[k] === e && e.st === 'loading') { e.st = 'err'; e.msg = T('超时'); modelsChanged(); } }, 50000);
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
  return ('<option value="">' + T('默认')) + (def ? ' (' + esc(def) + ')' : '') + '</option>' + (vals || []).map((v) => '<option value="' + esc(v) + '"' + (v === keep ? ' selected' : '') + '>' +
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
  sel.innerHTML = ('<option value="">' + T('默认（机器预设）') + '</option>') + cat.models.map((x) => '<option value="' + esc(x.id) + '">' +
    esc(x.label !== x.id ? x.label + ' · ' + x.id : x.id) + '</option>').join('') + '<option value="__other">Other model id…</option>';
  sel.value = keep && (keep === '__other' || cat.models.some((x) => x.id === keep)) ? keep : '';
  sel.disabled = cat.loading && !cat.models.length;
  $('#new-model-spin').hidden = !cat.loading;
  $('#new-model-row').hidden = sel.value !== '__other';
  const picked = cat.models.find((x) => x.id === sel.value) || null;
  const nd = $('#new-model-desc');
  nd.hidden = !picked && !cat.failed;
  nd.innerHTML = picked ? '<code>' + esc(picked.id) + '</code>' + (picked.desc ? ' — ' + esc(picked.desc) : '')
    : cat.failed ? T('无法读取模型列表，显示的是默认选项') : '';
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
  $('#new-wf').checked = false; $('#new-wf-pub').checked = false;
  $('#new-wf-pub-row').hidden = true;
  fillNew();
  openSheet($('#dlg-new'));
}
// #new?cwd=PATH&m=M1 (`gli new`, the Finder Quick Action): the new-session form with the machine and folder filled
// in. Only fills it: nothing starts until 创建. The hash is dropped at once, so a reload doesn't open it again.
let newRoute = null;
function newFromRoute(h) {
  const q = new URLSearchParams(h.slice(4));
  newRoute = { cwd: (q.get('cwd') || '').slice(0, 4096), m: q.get('m') || '' };
  history.replaceState(history.state, '', location.pathname + (S.view ? '#' + S.view.m + '/' + S.view.sid : ''));
  if (S.haveState) newFromRouteNow();                // else: when the first state arrives (the machine list)
}
function newFromRouteNow() {
  const r = newRoute; if (!r) return; newRoute = null;
  const local = S.machines.find((x) => x.type === 'local');
  openNew(S.byId[r.m] ? r.m : local && local.id);
  if (r.cwd) { $('#new-cwd').value = r.cwd; $('#new-cwd').dataset.touched = '1'; }
}
$('#btn-new').onclick = () => openNew();
$('#fab').onclick = () => openNew();
$('#new-machine').onchange = () => { $('#new-cwd').dataset.touched = ''; fillNewDeps(); };
for (const r of $$('#new-form input[name=kind]')) r.onchange = fillNewDeps;
$('#new-wf').onchange = () => {
  $('#new-wf-pub-row').hidden = !$('#new-wf').checked;
  if ($('#new-wf').checked) $('#new-wf-pub').checked = LS.getItem('glimpse.newPublish') !== '0';   // default: push
};
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
  if (!mc || !mc.online) { $('#new-err').textContent = T('请选择一台在线的机器'); return; }
  const kind = newKind(); const cwd = $('#new-cwd').value.trim();
  if (!cwd) { $('#new-err').textContent = T('请填写工作目录'); $('#new-cwd').focus(); return; }
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
    if (!$('#new-wf-row').hidden && $('#new-wf').checked) {
      msg.workflow = true;
      msg.publish = $('#new-wf-pub').checked;
      LS.setItem('glimpse.newPublish', msg.publish ? '1' : '0');
    }
  }
  const ok = send(msg);
  if (!ok) { newBusy(false); $('#new-err').textContent = T('未连接到 hub'); }
  setTimeout(() => { if (!$('#new-note').hidden) { newBusy(false); $('#new-err').textContent = T('启动超时，请重试'); } }, 25000);
});

// ------------------------------------------------------------------ restart with another key
// key usage (hub polls keys.usage_url; optional): quota windows (e.g. 5 h / 7 d) + today's tokens and cost
function winName(sec) { return !sec ? '' : sec % 86400 === 0 ? (sec / 86400) + T(' 天') : sec % 3600 === 0 ? (sec / 3600) + T(' 小时') : Math.round(sec / 60) + T(' 分钟'); }
function keyUsageText(k) {
  const u = k && k.usage; if (!u) return '';
  if (u.error) return u.error;
  const w = (u.windows || []).map((x) => winName(x.window_sec) + T(' 已用 ') + Math.round(x.percent) + '%');
  if (u.today_tokens != null) w.push(T('今日 ') + fmtNum(u.today_tokens) + ' tokens' + (u.today_cost ? ' · $' + u.today_cost : ''));
  return w.join(' · ');
}
function keyUsageBars(u) {
  if (!u || !u.windows || !u.windows.length) return '';
  return '<div class="kbars">' + u.windows.map((x) => '<span class="kbar"><span class="kb-l">' + esc(winName(x.window_sec)) + '</span>' + meter(x.percent) +
    '<span class="kb-r">' + (x.percent >= 100 ? (x.reset_at ? T('%a重置', { a: ago2(x.reset_at) }) : T('已用完')) : Math.round(100 - x.percent) + T('% 剩余')) + '</span></span>').join('') + '</div>';
}
function ago2(t) {
  const d = Math.max(0, t - Date.now() / 1000);
  return d < 3600 ? Math.ceil(d / 60) + T(' 分钟后') : d < 86400 ? Math.round(d / 3600) + T(' 小时后') : Math.round(d / 86400) + T(' 天后');
}
const RS = { active: false, m: null, sid: null, timer: null };
function openRestart() {
  const v = S.view; const s = v && session(v.m, v.sid); if (!s) return;
  if ($('#dlg-more').open) closeSheet($('#dlg-more'));
  RS.active = false; RS.m = v.m; RS.sid = v.sid;
  const opts = [{ v: '', l: T('保持当前'), d: s.key ? T('继续使用 ') + s.key : T('使用默认配置') }]
    .concat(S.keys.map((k) => ({ v: k.name, l: k.name, d: [k.name === s.key ? T('当前使用中') : '', keyUsageText(k)].filter(Boolean).join(' · '),
      u: k.usage })))
    .concat([{ v: '__new', l: T('输入新的 Key'), d: T('临时使用或保存到列表') }]);
  $('#rs-keys').innerHTML = opts.map((o, i) => '<label class="cell radio"><input type="radio" name="rskey" value="' + esc(o.v) + '"' + (i === 0 ? ' checked' : '') + '>' +
    '<span class="grow">' + esc(o.l) + (o.d ? '<div class="sub">' + esc(o.d) + '</div>' : '') + keyUsageBars(o.u) + '</span>' + ic('check', 'chk') + '</label>').join('');
  send({ op: 'key_usage' });                       // refresh the numbers while the sheet is open
  for (const r of $$('#rs-keys input')) r.onchange = () => { $('#rs-new').hidden = rsKey() !== '__new'; if (!$('#rs-new').hidden) $('#rs-value').focus(); };
  $('#rs-new').hidden = true; $('#rs-value').value = ''; $('#rs-save').value = '';
  const rz = $('#rs-resume'); rz.checked = !!s.can_resume; rz.disabled = !s.can_resume;
  $('#rs-resume-sub').textContent = s.can_resume ? T('重启后接着之前的上下文继续') : T('还没有可恢复的对话（会开始新对话）');
  // a session that reads its key from a file switches keys live: no restart, the conversation goes on
  RS.hot = !!s.hot_key;
  $('#dlg-rs-title').textContent = RS.hot ? T('切换 Key') : T('更换 Key 并重启');
  $('#rs-ok').textContent = RS.hot ? T('切换') : T('重启');
  $('#rs-hot-note').hidden = !RS.hot;
  $('#rs-progress').hidden = true; $('#rs-err').textContent = ''; $('#rs-ok').disabled = false;
  setTimeout(() => openSheet($('#dlg-restart')), $('#dlg-more').open ? 230 : 0);
}
$('#act-restart').onclick = openRestart;
// ---- session actions → 启用项目工作流: set the project up now, restart (resume) once the turn is over, so the
// session starts with the vault tools. GitHub: asked here, not silently.
const WF = {};          // "m/sid" -> {stage: 'setup' | 'restart', publish}
$('#act-wf').onclick = async () => {
  const v = S.view; const s = v && session(v.m, v.sid); if (!s) return;
  closeSheet($('#dlg-more'));
  await new Promise((r) => setTimeout(r, 230));
  const ans = await confirmSheet(T('启用项目工作流？'),
    T('在 %a 建 git 仓库（已有就沿用），补齐标准目录，登记到知识库；之后会话重启并接上原对话，AI 就能记录实验和日志。要不要同时建 GitHub 私有仓库并推送？',
      { a: s.cwd }), T('启用并推到 GitHub'), T('只在本地启用'), 'primary');
  if (!ans) return;
  const pub = ans === true;
  WF[v.m + '/' + v.sid] = { stage: 'setup', publish: pub };
  if (!send({ op: 'project_workflow', m: v.m, sid: v.sid, cwd: s.cwd, publish: pub })) {
    delete WF[v.m + '/' + v.sid]; toast(T('未连接'), T('稍后再试'), null, 'error');
  }
};
function onProjectWorkflow(m) {
  const k = m.m + '/' + m.sid; const w = WF[k]; if (!w) return;
  const init = m.init || {};
  const pub = m.publish ? T('，已推到 ') + m.publish.repo : m.publish_error ? T('；GitHub 推送失败：') + m.publish_error : '';
  toast(T('项目已就绪'), (init.git_init ? T('新建了 git 仓库') : T('沿用已有的 git 仓库')) + pub, null, m.publish_error ? 'error' : 'done');
  w.stage = 'restart';
  wfRestart(m.m, m.sid);
}
function wfRestart(mid, sid) {
  // wait for the turn to end: a restart mid-reply would cut the answer off
  const k = mid + '/' + sid; const w = WF[k]; const s = session(mid, sid);
  if (!w || !s) { delete WF[k]; return; }
  if (s.status === 'running' || s.status === 'starting') { w.timer = setTimeout(() => wfRestart(mid, sid), 3000); return; }
  delete WF[k];
  send({ op: 'restart', m: mid, sid, resume: true });
}
function rsKey() { const r = $('#rs-keys input:checked'); return r ? r.value : ''; }
function rsSteps(cur) {
  const steps = [T('结束旧进程'), T('设置新的 Key'), T('启动并恢复对话')];
  $('#rs-steps').innerHTML = steps.map((t, i) => '<div class="pstep ' + (i < cur ? 'done' : i === cur ? 'cur' : '') + '">' +
    (i < cur ? ic('check-c') : i === cur ? '<span class="spinner" aria-hidden="true"></span>' : '<span class="o"></span>') + t + '</div>').join('');
}
$('#rs-form').addEventListener('submit', (e) => {
  e.preventDefault();
  if (RS.active) return;
  const k = rsKey(); let msg;
  if (k === '__new') {
    const value = $('#rs-value').value.trim();
    if (!value) { $('#rs-err').textContent = T('请输入 Key'); $('#rs-value').focus(); return; }
    msg = { op: 'restart', m: RS.m, sid: RS.sid, key: null, new_key: { value, save_as: $('#rs-save').value.trim() || null }, resume: $('#rs-resume').checked };
  } else msg = { op: 'restart', m: RS.m, sid: RS.sid, key: k || null, new_key: null, resume: $('#rs-resume').checked };
  if (!send(msg)) { $('#rs-err').textContent = T('未连接到 hub'); return; }
  RS.active = true; $('#rs-err').textContent = ''; $('#rs-ok').disabled = true;
  $('#rs-progress').hidden = false; rsSteps(0);
  setTimeout(() => { if (RS.active) rsSteps(1); }, 600);
  clearTimeout(RS.timer); RS.timer = setTimeout(() => { if (RS.active) rsFail(T('重启超时')); }, 40000);
});
function rsDone(m) {
  clearTimeout(RS.timer); rsSteps(3); RS.active = false;
  $('#rs-value').value = '';
  const hot = m && m.hot;
  setTimeout(() => {
    closeSheet($('#dlg-restart'));
    toast(hot ? T('已切换到 ') + (m.key || T('新 Key')) : T('已重启'), hot ? T('无需重启，下一次请求开始使用新 Key') : T('进程已用新的配置重新启动'), null, 'done');
  }, hot ? 200 : 600);
}
function rsFail(msg) { clearTimeout(RS.timer); RS.active = false; $('#rs-ok').disabled = false; $('#rs-progress').hidden = true; $('#rs-err').textContent = msg || T('重启失败'); }

// ------------------------------------------------------------------ adopt: resume a conversation started on the computer
// The agent lists recent claude / codex transcripts (op history); picking one starts a Glimpse session with resume_id.
const ADOPT = { busy: false, m: null, kind: '', items: null, loading: false };
function openAdopt() {
  const sel = $('#adopt-machine');
  const prev = $('#new-machine').value || LS.getItem('glimpse.newMachine');
  sel.innerHTML = S.machines.map((m) => '<option value="' + esc(m.id) + '"' + (m.online ? '' : ' disabled') + '>' +
    esc(m.id + ' · ' + m.name + (m.online ? '' : T('（离线）'))) + '</option>').join('');
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
  if (!send({ op: 'history', m: ADOPT.m, kind: ADOPT.kind || null })) { ADOPT.loading = false; adoptFail(T('未连接到 hub')); return; }
  clearTimeout(ADOPT.timer);
  ADOPT.timer = setTimeout(() => { if (ADOPT.loading) { ADOPT.loading = false; adoptFail(T('读取超时，请重试')); } }, 20000);
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
    host.innerHTML = ('<div class="cell"><span class="spinner" aria-hidden="true"></span><span class="grow sub" style="margin:0">' + T('正在读取电脑上的对话记录…') + '</span></div>');
    return;
  }
  if (!ADOPT.items.length) {
    host.innerHTML = ('<div class="cell"><span class="grow sub" style="margin:0;white-space:normal">' + T('没有找到最近的')) + (ADOPT.kind ? ' ' + KIND[ADOPT.kind] + ' ' : SP) +
      (T('对话记录（Claude 读取 ~/.claude/projects，Codex 读取最近 14 天的 ~/.codex/sessions）。') + '</span></div>');
    return;
  }
  host.innerHTML = ADOPT.items.map((it, i) => '<button type="button" class="cell hitem" data-i="' + i + '">' +
    '<span class="grow"><span class="h-r1"><span class="tag ' + esc(it.kind) + '">' + esc(KIND[it.kind] || it.kind) + '</span>' +
    (it.active ? ('<span class="pill waiting"><span class="dot waiting" aria-hidden="true"></span>' + T('可能仍在运行') + '</span>') : '') +
    '<span class="h-time">' + esc(ago(it.updated)) + '</span></span>' +
    '<span class="h-title">' + esc(it.title || T('（无标题）')) + '</span>' +
    '<span class="h-cwd">' + ic('folder', 'xs') + '<span>' + esc(it.cwd || '') + '</span></span></span>' +
    '<span class="h-go">' + ic('chev', 'sm') + '</span></button>').join('');
  for (const b of $$('.hitem', host)) b.onclick = () => adoptPick(ADOPT.items[+b.dataset.i], b);
}
async function adoptPick(it, btn) {
  if (!it || ADOPT.busy) return;
  const mc = S.byId[ADOPT.m];
  if (!mc || !mc.online) { adoptFail(T('机器离线')); return; }
  if (it.active && !(await confirmSheet(T('这个对话可能还在电脑上运行'), T('它在 2 分钟内还有写入。请先在电脑上退出原来的 ') + (KIND[it.kind] || it.kind) +
    T('，否则两个进程同时写同一段对话会分叉。'), T('已关闭，继续接管')))) return;
  const d = phoneDims();
  const msg = { op: 'create', m: mc.id, kind: it.kind, cwd: it.cwd || mc.default_cwd || '~', resume_id: it.id, name: '', extra: [], key: null, cols: d.cols, rows: d.rows };
  if (!send(msg)) { adoptFail(T('未连接到 hub')); return; }
  ADOPT.busy = true; $('#adopt-err').textContent = '';
  for (const b of $$('#adopt-list .hitem')) b.disabled = true;
  const go = $('.h-go', btn); if (go) go.innerHTML = '<span class="spinner" aria-hidden="true"></span>';
  clearTimeout(ADOPT.timer);
  ADOPT.timer = setTimeout(() => { if (ADOPT.busy) adoptFail(T('启动超时，请重试')); }, 25000);
}
function adoptFail(msg) {
  clearTimeout(ADOPT.timer); ADOPT.busy = false;
  $('#adopt-err').textContent = msg || T('失败');
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
  if (!s) { host.innerHTML = ('<p class="g-f">' + T('会话已不存在。') + '</p>'); return; }
  const md = sessMode(s); const cat = catalog(CTL.m, s.kind);
  const dead = s.status === 'exited' || s.status === 'error';
  const dis = dead || !!CTL.busy;
  let h = '';
  if (dead) h += '<div class="note-box warn">' + ic('power', 'sm') + ('<div>' + T('进程已退出，重启会话后才能修改。') + '</div></div>');
  if (cat.failed) h += '<div class="note-box warn">' + ic('alert', 'sm') + ('<div>' + T('无法读取这台机器上的模型列表，下面是默认选项。') + '</div></div>');
  if (s.kind === 'claude') {
    const modes = [];
    for (const m of cat.modes) { const v = ctlModeValue(m); if (!modes.includes(v)) modes.push(v); }
    const cur = md.mode || 'default'; if (!modes.includes(cur)) modes.push(cur);
    h += '<div class="g-h">Permission mode</div><div class="group">' + modes.map((v) => {
      const busy = CTL.busy && CTL.busy.action === 'mode' && CTL.busy.value === v;
      return '<button type="button" class="cell radio" role="radio" aria-checked="' + (v === cur) + '" data-ca="mode" data-cv="' + esc(v) + '"' + (dis ? ' disabled' : '') + '>' +
        '<span class="grow"><span class="mono">' + esc(modeName(v)) + '</span><div class="sub">' + esc(MODE_DESC[v] || '') + '</div></span>' +
        (busy ? '<span class="spinner" aria-hidden="true"></span>' : v === cur ? ic('check', 'chk on') : '') + '</button>';
    }).join('') + ('</div><p class="g-f">' + T('通过 Shift+Tab 在终端里切换，当前模式从 Claude Code 底部状态栏识别。部分模式需要在 Claude 设置里先启用才会出现在循环中。') + '</p>');
  } else {
    h += '<div class="note-box">' + ic('refresh', 'sm') + ('<div>' + T('模型、推理强度和权限直接在当前会话里切换（Codex 的 /model、/permissions，只对这个会话生效），不会重启。只有 Codex 菜单里没有的组合才会重启并恢复对话。请在这一轮结束后再改。') + '</div></div>');
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
    '<button type="button" class="btn sm" id="ctl-model-go"' + (dis ? ' disabled' : '') + ('>' + T('应用') + '</button></div></div>');
  // codex: the efforts the current model supports
  const efforts = s.kind === 'codex' ? effortsFor(cat, curM) : cat.efforts;
  h += '<div class="g-h">Reasoning effort' + (md.effort ? '<span class="g-cur mono">' + esc(md.effort) + '</span>' : '') + '</div>' +
    (cat.loading && !efforts.length ? skelRows(1) : ctlChips('effort', efforts, md.effort, EFFORT_DESC, dis)) + chipHint(EFFORT_DESC, md.effort);
  if (s.kind === 'claude') {
    h += ('<div class="g-h">' + T('目标 /goal') + '</div><div class="group"><div class="cell"><input id="ctl-goal" style="text-align:left" placeholder="' + T('例如：让所有测试通过') + '" enterkeyhint="done" aria-label="' + T('目标') + '"') + (dis ? ' disabled' : '') + '>' +
      '<button type="button" class="btn sm" id="ctl-goal-go"' + (dis ? ' disabled' : '') + ('>' + T('设置') + '</button></div>') +
      '<button type="button" class="cell" id="ctl-goal-clear"' + (dis ? ' disabled' : '') + ('><span class="grow" style="color:var(--accent-text)">' + T('清除目标') + '</span></button></div>');
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
  if (!send({ op: 'control', m: CTL.m, sid: CTL.sid, action, value })) { ctlFail(T('未连接到 hub')); return; }
  CTL.busy = { action, value };
  renderCtl(); $('#ctl-err').textContent = '';
  clearTimeout(CTL.timer);
  CTL.timer = setTimeout(() => { if (CTL.busy) ctlFail(T('操作超时：请切到终端确认')); }, s.kind === 'codex' ? 45000 : 15000);
}
function ctlDone(m) {
  if (!CTL.busy || m.m !== CTL.m || m.sid !== CTL.sid) return;
  const b = CTL.busy; const s = session(CTL.m, CTL.sid);
  clearTimeout(CTL.timer); CTL.busy = null;
  if (b.action === 'goal') { const g = $('#ctl-goal'); if (g) g.value = ''; }
  if (b.action === 'model') { const g = $('#ctl-model'); if (g) g.value = ''; }
  if ($('#dlg-ctl').open) renderCtl();
  const what = { mode: T('模式'), model: T('模型'), effort: T('推理强度'), goal: T('目标'), approval: T('审批策略'), sandbox: T('沙箱') }[b.action] || b.action;
  const val = b.action === 'goal' && !b.value ? T('已清除') : b.action === 'mode' ? modeName(b.value) : b.value;
  toast(what + (s && s.kind === 'codex' ? T('已更新，会话已重启') : T('已更新')), val, null, 'done');
}
function ctlFail(msg) {
  clearTimeout(CTL.timer); CTL.busy = null;
  if ($('#dlg-ctl').open) { renderCtl(); $('#ctl-err').textContent = msg || T('操作失败'); } else toast(T('操作失败'), msg, null, 'error');
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
  setHTML(b, ('<img src="/static/icon.svg?v=4" alt="" width="40" height="40"><div class="grow"><b>' + T('把 Glimpse 装成 App') + '</b><span>') +
    (isIOS() ? T('点 Safari 的 ') + ic('share', 'xs') + T(' 分享 → 添加到主屏幕，全屏打开并接收通知') : T('全屏打开、独立图标，并可接收推送通知')) + '</span></div>' +
    (INST.prompt ? ('<button type="button" class="btn sm primary" data-i="go">' + T('安装') + '</button>') : ('<button type="button" class="btn sm" data-i="how">' + T('怎么做') + '</button>')) +
    ('<button type="button" class="icon-btn plain x" data-i="x" aria-label="' + T('不再提示') + '">') + ic('x', 'sm') + '</button>');
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
    if (ios && !standalone()) return T('先在 Safari 点“分享 → 添加到主屏幕”，再从主屏幕图标打开');
    return T('这个浏览器不支持推送通知（应用内横幅仍然可用）');
  }
  try {
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (window.Notification && Notification.permission === 'denied') return T('通知权限被拒绝：请在系统或浏览器的通知设置里允许 Glimpse');
    return sub ? T('已开启 ✓') : T('未开启');
  } catch (_) { return T('未开启'); }
}
function renderSettingsWidgets() {
  const p = widgetPrefs(); const host = $('#set-widgets');
  host.innerHTML = p.order.map((id, i) => {
    const w = WIDGET_BY_ID[id];
    return '<div class="cell"><span class="cell-ic" aria-hidden="true">' + ic(w.icon) + '</span><span class="grow">' + esc(w.title) + '</span>' +
      '<button type="button" class="icon-btn plain" data-up="' + i + ('" aria-label="' + T('上移 ')) + esc(w.title) + '"' + (i === 0 ? ' disabled' : '') + '>' + ic('up', 'sm') + '</button>' +
      '<button type="button" class="icon-btn plain" data-dn="' + i + ('" aria-label="' + T('下移 ')) + esc(w.title) + '"' + (i === p.order.length - 1 ? ' disabled' : '') + '>' + ic('down', 'sm') + '</button>' +
      '<span class="switch"><input type="checkbox" role="switch" data-w="' + esc(id) + ('" aria-label="' + T('显示 ')) + esc(w.title) + '"' + (p.on[id] ? ' checked' : '') + '><i></i></span></div>';
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
      (i >= 0 ? '<button type="button" class="icon-btn plain" data-ku="' + esc(id) + ('" aria-label="' + T('上移') + '"') + (i === 0 ? ' disabled' : '') + '>' + ic('up', 'sm') + '</button>' +
        '<button type="button" class="icon-btn plain" data-kd="' + esc(id) + ('" aria-label="' + T('下移') + '"') + (i === on.length - 1 ? ' disabled' : '') + '>' + ic('down', 'sm') + '</button>' : '') +
      (custom ? '<button type="button" class="icon-btn plain" data-kx="' + esc(id) + ('" aria-label="' + T('删除 ')) + esc(k.label) + '">' + ic('trash', 'sm') + '</button>' : '') +
      '<span class="switch"><input type="checkbox" role="switch" data-kk="' + esc(id) + ('" aria-label="' + T('显示 ')) + esc(k.label) + '"' + (i >= 0 ? ' checked' : '') + '><i></i></span></div>';
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
  if (!label || !sendv) { $('#key-err').textContent = T('名称和发送内容都要填'); return; }
  if (label.length > 8) { $('#key-err').textContent = T('名称最多 8 个字'); return; }
  const ks = customKeys(); const id = 'c:' + Date.now().toString(36);
  ks.push({ id, label, send: sendv.slice(0, 500) });
  LS.setItem('glimpse.customKeys', JSON.stringify(ks));
  LS.setItem('glimpse.keybar', JSON.stringify(keybar().concat([id])));
  $('#key-label').value = ''; $('#key-send').value = ''; $('#key-err').textContent = '';
  renderKeybar(); renderSettingsKeys();
};
$('#key-reset').onclick = () => { LS.removeItem('glimpse.keybar'); renderKeybar(); renderSettingsKeys(); };

// ---- agent update: the daemon on a machine outlives code updates; when it still runs older code (it had
// sessions running, so the hub did not replace it), restart it on the current code. Sessions come back resumed.
const AU = {};    // machine id -> true while its update runs
async function updateAgent(mid) {
  const mc = S.byId[mid]; if (!mc || AU[mid]) return;
  const live = mc.sessions.filter((s) => s.pid && s.exit_code == null && s.status !== 'exited');
  // only Claude / Codex sessions with a conversation id are restored; shells and the like just end
  const back = live.filter((s) => (s.kind === 'claude' || s.kind === 'codex') && s.agent_session_id).length;
  const lost = live.length - back;
  const msg = !live.length ? T('这台机器上没有正在运行的会话。') :
    (back ? back + T(' 个 Claude / Codex 会话会重启并自动恢复对话（同一个会话，接着原来的上下文），正在生成的回复会中断，电脑上连着的终端会自动重连。') : '') +
    (lost ? lost + T(' 个其他会话（如 Shell）会结束，无法恢复。') : '');
  const ok = await confirmSheet(T('更新 ') + mid + T(' 的 agent？'), msg, T('更新'));
  if (!ok) return;
  if (!send({ op: 'agent_update', m: mid })) { toast(T('未连接'), T('稍后再试'), null, 'error'); return; }
  AU[mid] = true; renderSettingsMachines(); renderHomeWidgets();
  setTimeout(() => { if (AU[mid]) { delete AU[mid]; renderSettingsMachines(); renderHomeWidgets(); } }, 60000);
}
function agentUpdated(mc) {
  // a fresh connection without the outdated flag ends a running update
  if (AU[mc.id] && mc.online && !mc.outdated) { delete AU[mc.id]; toast(mc.id + T(' 的 agent 已更新'), '', null, 'done'); }
}
function renderSettingsMachines() {
  const host = $('#set-machines');
  setHTML(host, S.machines.map((m) => '<div class="cell"><span class="cell-ic" aria-hidden="true">' + ic(m.type === 'ssh' ? 'server' : 'laptop') + '</span>' +
    '<span class="grow"><b>' + esc(m.id) + '</b> ' + esc(m.name) + '<div class="sub">' + (m.online ? T('在线') + (m.version ? ' · agent ' + esc(m.version) : '') +
      (m.outdated ? ' · <span style="color:var(--c-wait)">' + (AU[m.id] ? T('正在更新…') : T('需要更新')) + '</span>' : '') +
      (m.stats ? ' · ' + fmtMs(m.stats.rtt_ms) : '') : '<span style="color:var(--c-err)">' + esc(AU[m.id] ? T('正在更新…') : m.error || T('离线')) + '</span>') + '</div></span>' +
    (m.online && m.outdated && !AU[m.id] ? '<button type="button" class="btn sm" data-au="' + esc(m.id) + ('">' + T('更新') + '</button>') : '') +
    (m.online ? '' : '<button type="button" class="btn sm" data-rc="' + esc(m.id) + ('">' + T('重连') + '</button>')) +
    (m.type === 'ssh' ? '<button type="button" class="icon-btn plain" data-ed="' + esc(m.id) + ('" aria-label="' + T('编辑 ')) + esc(m.name) + '">' + ic('pencil', 'sm') + '</button>' : '') +
    '</div>').join('') || ('<div class="cell"><span class="grow sub">' + T('没有机器') + '</span></div>'));
  for (const b of $$('[data-rc]', host)) b.onclick = () => { send({ op: 'reconnect', m: b.dataset.rc }); toast(T('正在重连'), b.dataset.rc); };
  for (const b of $$('[data-au]', host)) b.onclick = () => updateAgent(b.dataset.au);
  for (const b of $$('[data-ed]', host)) b.onclick = () => openMachine(b.dataset.ed);
  const n = allSessions().filter(([, x]) => x.status === 'exited' || x.status === 'error').length;
  $('#btn-clean').disabled = !n;
  $('#btn-clean-sub').textContent = n ? n + T(' 个已退出或出错的会话') : T('没有已结束的会话');
}

// ---- machines: add / edit / remove (hub ops machine_add / machine_update / machine_remove / machine_test)
const MC = { busy: false, m: null, timer: null, testing: false };
const HOST_RE = /^[A-Za-z0-9_.@:-]+$/;
function openMachine(mid) {
  const mc = mid ? S.byId[mid] : null;
  MC.m = mc ? mc.id : null; MC.busy = false; MC.testing = false;
  $('#dlg-mc-title').textContent = mc ? T('编辑 ') + mc.id : T('添加机器');
  $('#mc-name').value = mc ? mc.name : ''; $('#mc-host').value = mc ? mc.host || '' : '';
  $('#mc-port').value = mc && mc.port ? mc.port : ''; $('#mc-cwd').value = mc ? mc.default_cwd || '' : '';
  $('#mc-del-g').hidden = !mc || mc.type !== 'ssh';
  $('#mc-err').textContent = ''; $('#mc-ok').disabled = false;
  $('#mc-test-sub').textContent = T('检查 SSH 免密登录和远端 Python'); $('#mc-test-sub').className = 'sub';
  $('#mc-test').disabled = false;
  const go = () => { openSheet($('#dlg-machine')); if (!mc) $('#mc-name').focus(); };
  if ($('#dlg-settings').open) { closeSheet($('#dlg-settings')); setTimeout(go, 230); } else go();
}
function mcHost() {
  const h = $('#mc-host').value.trim();
  if (!h) return [null, T('请填写 SSH 主机')];
  if (!HOST_RE.test(h) || h[0] === '-') return [null, T('主机只能包含字母、数字和 _ . @ : -，不能以 - 开头')];
  return [h, null];
}
function mcPort() { const v = $('#mc-port').value.trim(); if (!v) return [null, null]; const n = parseInt(v, 10); return n >= 1 && n <= 65535 ? [n, null] : [null, T('端口应在 1–65535 之间')]; }
$('#mc-test').onclick = () => {
  const [host, e1] = mcHost(); const [port, e2] = mcPort();
  if (e1 || e2) { $('#mc-err').textContent = e1 || e2; return; }
  const msg = { op: 'machine_test', host }; if (port) msg.port = port;
  if (!send(msg)) { $('#mc-err').textContent = T('未连接到 hub'); return; }
  MC.testing = true; $('#mc-test').disabled = true; $('#mc-err').textContent = '';
  const sub = $('#mc-test-sub'); sub.className = 'sub'; sub.innerHTML = ('<span class="spinner" style="width:12px;height:12px;display:inline-block;vertical-align:-1px;margin-right:6px" aria-hidden="true"></span>' + T('正在连接 ')) + esc(host) + '…';
  clearTimeout(MC.timer); MC.timer = setTimeout(() => { if (MC.testing) mcTested({ ok: false, msg: T('超时：hub 没有回应') }); }, 20000);
};
function mcTested(m) {
  if (!MC.testing) return;
  clearTimeout(MC.timer); MC.testing = false; $('#mc-test').disabled = false;
  const sub = $('#mc-test-sub');
  sub.className = 'sub ' + (m.ok ? 'ok' : 'bad');
  sub.textContent = (m.ok ? '✓ ' : '✕ ') + (m.msg || (m.ok ? T('连接成功') : T('连接失败')));
}
$('#mc-form').addEventListener('submit', (e) => {
  e.preventDefault();
  if (MC.busy) return;
  const name = $('#mc-name').value.trim();
  if (!name) { $('#mc-err').textContent = T('请填写名称'); $('#mc-name').focus(); return; }
  const [host, e1] = mcHost(); const [port, e2] = mcPort();
  if (e1 || e2) { $('#mc-err').textContent = e1 || e2; return; }
  const msg = MC.m ? { op: 'machine_update', m: MC.m, name, host, port, default_cwd: $('#mc-cwd').value.trim() || '~' }
    : { op: 'machine_add', name, host, default_cwd: $('#mc-cwd').value.trim() || '~' };
  if (!MC.m && port) msg.port = port;
  if (!send(msg)) { $('#mc-err').textContent = T('未连接到 hub'); return; }
  MC.busy = true; $('#mc-ok').disabled = true; $('#mc-err').textContent = '';
  clearTimeout(MC.timer); MC.timer = setTimeout(() => { if (MC.busy) mcFail(T('保存超时')); }, 15000);
});
$('#mc-del').onclick = async () => {
  const mc = S.byId[MC.m]; if (!mc) return;
  closeSheet($('#dlg-machine'));
  await new Promise((r) => setTimeout(r, 230));
  if (!(await confirmSheet(T('删除机器 ') + mc.id + T('？'), '“' + mc.name + T('”会从 hub 配置中移除，它上面正在运行的会话不会被结束，但手机上将看不到。'), T('删除机器')))) return;
  MC.busy = true;
  if (!send({ op: 'machine_remove', m: mc.id })) { MC.busy = false; toast(T('未连接'), T('稍后再试'), null, 'error'); }
};
function mcSaved() {
  if (!MC.busy) return;
  clearTimeout(MC.timer); MC.busy = false;
  const wasOpen = $('#dlg-machine').open;
  if (wasOpen) closeSheet($('#dlg-machine'));
  toast(MC.m && !wasOpen ? T('已删除机器') : MC.m ? T('已保存') : T('已添加机器'), MC.m ? null : T('正在连接，稍后会出现在首页'), null, 'done');
}
function mcFail(msg) { clearTimeout(MC.timer); MC.busy = false; $('#mc-ok').disabled = false; $('#mc-err').textContent = msg || T('保存失败'); }
$('#btn-add-machine').onclick = () => openMachine(null);
$('#btn-clean').onclick = async () => {
  const dead = allSessions().filter(([, x]) => x.status === 'exited' || x.status === 'error');
  if (!dead.length) return;
  closeSheet($('#dlg-settings'));
  await new Promise((r) => setTimeout(r, 230));
  if (!(await confirmSheet(T('清理已结束的会话？'), T('将删除 ') + dead.length + T(' 个已退出或出错的会话（对话记录文件仍保留在电脑上）。'), T('全部清理')))) return;
  const ms = new Set(dead.map(([mc]) => mc.id));
  let ok = true; for (const m of ms) ok = send({ op: 'clean_exited', m }) && ok;
  if (ok) toast(T('已清理'), dead.length + T(' 个会话'), null, 'done'); else toast(T('未连接'), T('稍后再试'), null, 'error');
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
// language: per device (glimpse.lang). Picking 中文 / English by hand also sets the hub's language, which the
// texts made on the hub and the machines use (notifications, system lines in chat). The page reloads in it.
function renderLangSeg() {
  const v = langPref();
  for (const b of $$('#opt-lang [data-lang-v]')) b.setAttribute('aria-checked', String(b.dataset.langV === v));
}
for (const b of $$('#opt-lang [data-lang-v]')) {
  b.onclick = () => {
    const v = b.dataset.langV;
    if (v === langPref()) return;
    if (v === 'auto') LS.removeItem('glimpse.lang'); else LS.setItem('glimpse.lang', v);
    if (v !== 'auto') send({ op: 'lang', lang: v });
    renderLangSeg();
    draftSave(); edStash();
    setTimeout(() => location.reload(), 150);             // let the op go out first
  };
}
async function renderSettings() {
  renderThemeSeg(); renderLangSeg();
  $('#opt-hide-exited').checked = S.hideExited;
  $('#opt-font').textContent = String(S.fontSize);
  $('#set-ver').textContent = APP_VERSION;
  $('#set-hubver').textContent = S.hub && S.hub.version ? S.hub.version : '—';
  renderSettingsWidgets(); renderSettingsKeys(); renderSettingsMachines(); renderSettingsMemory(); renderKeyPage(); renderAccount();
  $('#btn-install-sub').textContent = standalone() ? T('已作为 App 运行 ✓') : isIOS() ? T('Safari：分享 → 添加到主屏幕') : INST.prompt ? T('可以直接安装') : T('查看安装方法');
  renderSettingsNav();
  $('#set-push').textContent = await pushState();
  const ps = $('#set-push').textContent;
  $('#nav-push-sub').textContent = /已开启/.test(ps) ? T('已开启') : /拒绝/.test(ps) ? T('权限被拒绝') : /主屏幕/.test(ps) ? T('需从主屏幕图标打开') : /不支持/.test(ps) ? T('此浏览器不支持') : ps;
}
// ---- settings sub-pages: the main list links to pages that slide in from the right
const THEME_NAMES = { auto: T('跟随系统'), light: T('浅色'), dark: T('深色'), solarized: 'Solarized' };
function renderSettingsNav() {
  renderExtPage(); renderSchedPage();
  $('#nav-look-sub').textContent = THEME_NAMES[themePref()] + T(' · 终端字号 ') + S.fontSize;
  const w = $$('#set-widgets input[type=checkbox]'); const won = w.filter((x) => x.checked).length;
  $('#nav-widgets-sub').textContent = w.length ? T('已开启 ') + won + ' / ' + w.length : '';
  $('#nav-keys-sub').textContent = T('按键栏 ') + keybar().length + T(' 个键');
  $('#nav-machines-sub').textContent = S.machines.map((mc) => mc.id + (mc.online ? '' : T(' 离线'))).join(' · ');
  $('#nav-acct-sub').textContent = $('#acct-pw-sub').textContent === T('已设置') ? T('已设置密码') : T('登录设备与密码');
  $('#nav-keys2-sub').textContent = (S.keys || []).length + T(' 个 · 均衡：') + KB_NAMES[kbState().mode];
}
// ---- Key page: optional balancing (keys.balance) + which keys take part
const KB_NAMES = { off: T('关闭'), on_exhausted: T('没余额时自动换'), least_used: T('按用量分配') };
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
    ? T('没有配置 keys.usage_url，看不到各 Key 的用量：会按每个 Key 上的会话数平均分配。5 小时额度不参与均衡，用完时由“没余额时自动换”处理。') : '';
  const keys = S.keys || [];
  setHTML($('#kb-keys'), keys.length ? keys.map((k) => {
    const inPool = !b.exclude.includes(k.name);
    const ex = k.exhausted ? '<span class="tag" style="color:var(--c-err)">' + esc(ago(k.exhausted)) + (T('不可用') + '</span> ') : '';
    return '<label class="cell"><span class="grow">' + esc(k.name) + ' ' + ex + '<div class="sub">' + esc(keyUsageText(k) || T('暂无用量数据')) + '</div>' + keyUsageBars(k.usage) + '</span>' +
      '<span class="switch"><input type="checkbox" role="switch" data-kbk="' + esc(k.name) + '"' + (inPool ? ' checked' : '') + ' aria-label="' + esc(k.name) + (T(' 参与均衡') + '"><i></i></span></label>');
  }).join('') : ('<div class="cell"><span class="grow sub">' + T('config.json 里还没有 Key') + '</span></div>'));
  for (const el of $$('#kb-keys [data-kbk]')) el.onchange = () => {
    const cur = kbState(); const ex = new Set(cur.exclude);
    if (el.checked) ex.delete(el.dataset.kbk); else ex.add(el.dataset.kbk);
    send({ op: 'key_balance', mode: cur.mode, exclude: [...ex] });
  };
}
for (const el of $$('#kb-modes [data-kb]')) el.onclick = () => send({ op: 'key_balance', mode: el.dataset.kb, exclude: kbState().exclude });
// ---- scheduled tasks (Settings → 定时任务): config.json "schedules"; the hub starts a session at each time
const SCH = { id: null, busy: false };
const SCH_DOW = [T('周日'), T('周一'), T('周二'), T('周三'), T('周四'), T('周五'), T('周六')];
function schedList() { return S.sched || []; }
function two(n) { return String(n).padStart(2, '0'); }
function cronText(c) {
  // the common shapes in words; anything else as written
  const f = String(c || '').split(' ');
  if (f.length !== 5) return c;
  const [mi, h, dom, mon, dow] = f; const num = (x) => /^\d+$/.test(x);
  if (num(mi) && num(h) && dom === '*' && mon === '*') {
    const hm = two(h) + ':' + two(mi);
    if (dow === '*') return T('每天 ') + hm;
    if (dow === '1-5') return T('工作日 ') + hm;
    if (num(dow) && +dow <= 7) return T('每%a', { a: SCH_DOW[+dow % 7] }) + ' ' + hm;
  }
  if (num(mi) && h === '*' && dom === '*' && mon === '*' && dow === '*') return T('每小时第 %a 分', { a: mi });
  return 'cron ' + c;
}
function whenText(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000); const now = new Date();
  const hm = two(d.getHours()) + ':' + two(d.getMinutes());
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const dd = Math.round((day(d) - day(now)) / 86400000);
  return (dd === 0 ? T('今天 ') : dd === 1 ? T('明天 ') : dd === -1 ? T('昨天 ') : (d.getMonth() + 1) + '/' + d.getDate() + ' ') + hm;
}
function renderSchedPage() {
  const list = schedList();
  $('#nav-sched-sub').textContent = list.length ? list.filter((x) => x.enabled).length + ' / ' + list.length + T(' 个已启用') : '';
  const h = list.map((x) => {
    const mc = S.byId[x.machine];
    const st = !x.enabled ? T('已关闭') : x.next ? T('下次 ') + whenText(x.next) : '';
    const last = x.error ? '<div class="sub bad">' + T('上次失败：') + esc(x.error) + '</div>'
      : x.last ? '<div class="sub">' + T('上次 ') + esc(whenText(x.last)) + (x.skipped ? ' · ' + T('错过的一次已跳过') : '') + '</div>' : '';
    return '<button type="button" class="cell sched-row" data-sched="' + esc(x.id) + '"><span class="cell-ic" style="background:' + (x.enabled ? '#FF6B35' : '#8E8E93') + '" aria-hidden="true">' + ic('history') + '</span>' +
      '<span class="grow">' + esc(x.name) + '<div class="sub">' + esc(cronText(x.cron)) + ' · ' + esc(x.machine) + (mc && !mc.online ? T('（离线）') : '') + ' · ' + esc(KIND[x.kind] || x.kind) + (st ? ' · ' + esc(st) : '') + '</div>' + last + '</span>' +
      '<svg class="ic chev" aria-hidden="true"><use href="#i-chev"/></svg></button>';
  }).join('');
  setHTML($('#set-sched'), '<div class="group">' + (h || '<div class="cell"><span class="grow sub">' + T('还没有定时任务') + '</span></div>') + '</div>');
  for (const b of $$('[data-sched]', $('#set-sched'))) b.onclick = () => openSched(b.dataset.sched);
}
function schedFreqFrom(c) {
  // the editor's simple form for a cron, or "cron" when it has none
  const f = String(c || '').split(' '); const num = (x) => /^\d+$/.test(x);
  if (f.length === 5 && f[2] === '*' && f[3] === '*' && num(f[0])) {
    if (num(f[1]) && f[4] === '*') return { freq: 'daily', h: +f[1], mi: +f[0] };
    if (num(f[1]) && f[4] === '1-5') return { freq: 'weekdays', h: +f[1], mi: +f[0] };
    if (num(f[1]) && num(f[4]) && +f[4] <= 7) return { freq: 'weekly', h: +f[1], mi: +f[0], dow: +f[4] % 7 };
    if (f[1] === '*' && f[4] === '*') return { freq: 'hourly', mi: +f[0] };
  }
  return { freq: 'cron' };
}
function schedCron() {
  const fq = $('#sc-freq').value;
  if (fq === 'cron') return $('#sc-cron').value.trim().replace(/\s+/g, ' ');
  if (fq === 'hourly') return Math.max(0, Math.min(59, parseInt($('#sc-min').value, 10) || 0)) + ' * * * *';
  const [h, mi] = ($('#sc-time').value || '08:00').split(':').map((x) => parseInt(x, 10) || 0);
  return mi + ' ' + h + ' * * ' + (fq === 'weekdays' ? '1-5' : fq === 'weekly' ? $('#sc-dow').value : '*');
}
function schedFreqRows() {
  const fq = $('#sc-freq').value;
  $('#sc-time-row').hidden = fq === 'cron' || fq === 'hourly';
  $('#sc-dow-row').hidden = fq !== 'weekly';
  $('#sc-min-row').hidden = fq !== 'hourly';
  $('#sc-cron-row').hidden = fq !== 'cron';
  $('#sc-next').textContent = T('本机时间：') + cronText(schedCron());
}
for (const id of ['#sc-freq', '#sc-time', '#sc-dow', '#sc-min', '#sc-cron']) $(id).addEventListener('input', schedFreqRows);
function openSched(id) {
  const x = id ? schedList().find((t) => t.id === id) : null;
  SCH.id = x ? x.id : null; SCH.busy = false;
  $('#dlg-sc-title').textContent = x ? T('编辑定时任务') : T('新建定时任务');
  $('#sc-name').value = x ? x.name : '';
  $('#sc-on').checked = x ? x.enabled : true;
  const fr = schedFreqFrom(x ? x.cron : '0 8 * * *');
  $('#sc-freq').value = fr.freq;
  $('#sc-time').value = two(fr.h == null ? 8 : fr.h) + ':' + two(fr.mi || 0);
  $('#sc-dow').value = String(fr.dow == null ? 1 : fr.dow);
  $('#sc-min').value = String(fr.mi || 0);
  $('#sc-cron').value = x ? x.cron : '0 8 * * *';
  setHTML($('#sc-m'), S.machines.map((mc) => '<option value="' + esc(mc.id) + '">' + esc(mc.id + ' · ' + mc.name) + '</option>').join(''));
  $('#sc-m').value = x ? x.machine : (S.machines[0] || {}).id || '';
  $('#sc-kind').value = x ? x.kind : 'claude';
  $('#sc-cwd').value = x ? x.cwd : '';
  $('#sc-prompt').value = x ? x.prompt : '';
  $('#sc-run-g').hidden = $('#sc-del-g').hidden = !x;
  $('#sc-err').textContent = '';
  schedFreqRows();
  openSheet($('#dlg-sched'));
}
$('#btn-sched-new').onclick = () => openSched(null);
$('#sc-form').addEventListener('submit', (e) => {
  e.preventDefault();
  if (SCH.busy) return;
  const task = { name: $('#sc-name').value.trim(), cron: schedCron(), machine: $('#sc-m').value, kind: $('#sc-kind').value,
    cwd: $('#sc-cwd').value.trim(), prompt: $('#sc-prompt').value.trim(), enabled: $('#sc-on').checked };
  const miss = !task.name ? '#sc-name' : !task.cwd ? '#sc-cwd' : !task.prompt ? '#sc-prompt' : null;
  if (miss) { $('#sc-err').textContent = T('请填写名称、工作目录和任务内容'); $(miss).focus(); return; }
  const old = SCH.id && schedList().find((t) => t.id === SCH.id);
  if (old) for (const k of ['model', 'effort', 'mode']) if (old[k]) task[k] = old[k];
  if (!send({ op: 'sched_save', id: SCH.id, task })) { $('#sc-err').textContent = T('未连接到 hub'); return; }
  SCH.busy = true; $('#sc-ok').disabled = true;
});
$('#sc-run').onclick = () => {
  if (SCH.busy || !SCH.id) return;
  if (!send({ op: 'sched_run', id: SCH.id })) { $('#sc-err').textContent = T('未连接到 hub'); return; }
  SCH.busy = true; $('#sc-run-sub').textContent = T('启动中…');
};
$('#sc-del').onclick = async () => {
  if (!SCH.id || !(await confirmSheet(T('删除这个定时任务？'), T('以后不会再运行。已经开过的会话不受影响。'), T('删除')))) return;
  send({ op: 'sched_remove', id: SCH.id }); SCH.busy = true;
};
function onSchedOk(m) {
  SCH.busy = false; $('#sc-ok').disabled = false; $('#sc-run-sub').textContent = T('按保存的设置新开一个会话');
  if (m.op === 'sched_run') {
    // straight into the new session (a toast would sit under the settings sheet); openSession closes the sheets
    toast(T('已开始运行'), '', null, 'done');
    if (session(m.m, m.sid)) openSession(m.m, m.sid);
    else SCH.openWhen = { m: m.m, sid: m.sid, until: Date.now() + 10000 };   // the state with it is on its way
    return;
  }
  closeSheet($('#dlg-sched'));
  toast(m.op === 'sched_remove' ? T('已删除定时任务') : T('已保存定时任务'), '', null, 'done');
}
function onSchedErr(m) {
  SCH.busy = false; $('#sc-ok').disabled = false; $('#sc-run-sub').textContent = T('按保存的设置新开一个会话');
  if ($('#dlg-sched').open) $('#sc-err').textContent = m.msg; else toast(T('定时任务'), m.msg, null, 'error');
}
// ---- extensions (Settings → 扩展): what config.json "extensions" lists, with each machine's verdict
const EXT = { open: {}, busy: false };
function extList() { return (S.ext || []); }
function renderExtPage() {
  const list = extList();
  $('#nav-ext-sub').textContent = list.length ? list.filter((e) => e.enabled && e.on).length + ' / ' + list.length + T(' 个已启用') : '';
  const sel = $('#ext-m'); const cur = sel.value;
  setHTML(sel, S.machines.map((mc) => '<option value="' + esc(mc.id) + '">' + esc(mc.id + ' · ' + mc.name) + '</option>').join(''));
  if (cur && S.byId[cur]) sel.value = cur;
  const h = list.map((e) => {
    const st = !e.enabled ? T('已关闭') : e.on ? T('已启用') : T('不可用') + (e.error ? '：' + e.error : '');
    const sch = e.schema || {}; const open = EXT.open[e.id];
    let x = '<div class="group ext-card" data-ext="' + esc(e.id) + '">' +
      '<div class="cell"><span class="cell-ic" style="background:' + (e.enabled && e.on ? '#8E5CF7' : '#8E8E93') + '" aria-hidden="true">' + ic('plus-sq') + '</span>' +
      '<span class="grow">' + esc(e.name || e.id) + (e.version ? ' <span class="sub" style="display:inline">' + esc(e.version) + '</span>' : '') +
      '<div class="sub' + (e.enabled && !e.on ? ' bad' : '') + '">' + esc(st) + ' · ' + esc(e.machine || '') + '</div></span>' +
      '<span class="switch"><input type="checkbox" role="switch" data-ext-on aria-label="' + esc(T('启用') + ' ' + (e.name || e.id)) + '"' + (e.enabled ? ' checked' : '') + '><i></i></span></div>';
    if (e.description) x += '<div class="cell-note">' + esc(e.description) + '</div>';
    x += '<button type="button" class="cell" data-ext-more aria-expanded="' + (open ? 'true' : 'false') + '"><span class="grow">' + T('设置') + '</span><svg class="ic chev" aria-hidden="true" style="transform:rotate(' + (open ? 90 : 0) + 'deg)"><use href="#i-chev"/></svg></button>';
    if (open) {
      for (const [k, spec] of Object.entries(sch)) {
        const v = (e.settings || {})[k]; const id = 'ext-' + e.id + '-' + k;
        x += spec && spec.type === 'boolean'
          ? '<div class="cell"><span class="grow">' + esc(T(spec.label || k)) + '</span><span class="switch"><input type="checkbox" role="switch" id="' + esc(id) + '" data-ext-set="' + esc(k) + '"' + (v ? ' checked' : '') + ' aria-label="' + esc(T(spec.label || k)) + '"><i></i></span></div>'
          : '<div class="cell stack"><label class="lbl" for="' + esc(id) + '">' + esc(T((spec && spec.label) || k)) + (spec && spec.required ? ' *' : '') + '</label>' +
            '<input id="' + esc(id) + '" data-ext-set="' + esc(k) + '"' + (spec && (spec.type === 'path') ? ' class="mono"' : '') + ' value="' + esc(v == null ? '' : v) + '" autocapitalize="off" autocorrect="off" spellcheck="false"></div>';
      }
      x += '<div class="cell-note mono">' + esc(e.dir) + '</div>' +
        '<div class="cell"><button type="button" class="btn sm primary" data-ext-save>' + T('保存设置') + '</button><span class="grow"></span>' +
        '<button type="button" class="btn sm danger" data-ext-rm>' + T('移除') + '</button></div>';
    }
    return x + '</div>';
  }).join('');
  setHTML($('#set-ext'), list.length ? h : '<div class="group"><div class="cell"><span class="grow sub">' + T('还没有扩展') + '</span></div></div>');
  for (const card of $$('[data-ext]', $('#set-ext'))) {
    const eid = card.dataset.ext;
    $('[data-ext-on]', card).onchange = (ev) => extSend({ op: 'ext_update', id: eid, enabled: ev.target.checked });
    $('[data-ext-more]', card).onclick = () => { EXT.open[eid] = !EXT.open[eid]; renderExtPage(); };
    const save = $('[data-ext-save]', card);
    if (save) save.onclick = () => {
      const st = {};
      for (const el of $$('[data-ext-set]', card)) st[el.dataset.extSet] = el.type === 'checkbox' ? el.checked : el.value.trim();
      extSend({ op: 'ext_update', id: eid, settings: st });
    };
    const rm = $('[data-ext-rm]', card);
    if (rm) rm.onclick = async () => {
      if (!(await confirmSheet(T('移除这个扩展？'), T('只是从 Glimpse 里移除，扩展的文件夹和数据不会动。'), T('移除')))) return;
      extSend({ op: 'ext_remove', id: eid });
    };
  }
}
function extSend(o) {
  if (EXT.busy) return;
  if (!send(o)) { toast(T('未连接'), T('稍后再试'), null, 'error'); return; }
  EXT.busy = true; $('#ext-add-btn').disabled = true;
}
function onExtOk(m) {
  EXT.busy = false; $('#ext-add-btn').disabled = false;
  if (m.op === 'ext_add') { $('#ext-dir').value = ''; toast(T('已添加扩展'), T('打开开关并填好设置后生效'), null, 'done'); }
  else if (m.op === 'ext_remove') toast(T('已移除扩展'), '', null, 'done');
  else toast(T('已保存'), T('新开或重启的会话会带上'), null, 'done');
}
$('#ext-add').addEventListener('submit', (e) => {
  e.preventDefault();
  const dir = $('#ext-dir').value.trim(); if (!dir) { $('#ext-dir').focus(); return; }
  extSend({ op: 'ext_add', m: $('#ext-m').value, dir });
});
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
  try { r = await api('/api/devices'); } catch (e) { $('#acct-pw-sub').textContent = T('无法读取'); return; }
  $('#acct-pw-sub').textContent = r.has_password ? T('已设置') : T('未设置（只能用一次性链接登录），在电脑上运行 gli password');
  const host = $('#acct-devices');
  host.innerHTML = r.devices.map((d) => '<div class="cell"><span class="cell-ic" style="background:#5C636E" aria-hidden="true">' + ic(/iPhone|Android|iPad/.test(d.name) ? 'phone' : 'laptop') + '</span>' +
    '<span class="grow">' + esc(d.name) + (d.current ? (' <span class="tag">' + T('本机') + '</span>') : '') +
    ('<div class="sub">' + T('最近 ')) + esc(ago(d.last_seen)) + (d.ip ? ' · ' + esc(d.ip) : '') + '</div></span>' +
    (d.current ? '' : '<button type="button" class="btn sm danger" data-revoke="' + esc(d.id) + ('">' + T('退出') + '</button>')) + '</div>').join('');
  for (const b of $$('[data-revoke]', host)) b.onclick = async () => {
    if (!(await confirmSheet(T('退出这台设备？'), T('它需要重新登录才能再次访问。'), T('退出')))) return;
    try { await api('/api/devices/revoke', { id: b.dataset.revoke }); toast(T('已退出'), '', null, 'done'); renderAccount(); } catch (e) { toast(T('操作失败'), e.message, null, 'error'); }
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
const MEM = { m: null, view: 'home', project: null, data: {}, busy: false, q: '', asked: {} };
function memMachines() { return S.machines.filter((mc) => mc.memory && mc.memory.enabled); }
function renderSettingsMemory() {
  const list = memMachines();
  $('#nav-mem').hidden = !list.length;
  const tot = list.map((mc) => MEM.data[mc.id + ':overview']).filter(Boolean).reduce((a, ov) => a + ov.size.total, 0);
  $('#nav-mem-sub').textContent = list.length ? (tot ? fmtBytes(tot) + ' · ' : '') + list.map((mc) => mc.id).join(' · ') : '';
  setHTML($('#set-mem'), list.map((mc) => {
    const st = mc.memory; const ov = MEM.data[mc.id + ':overview'];
    const sub = !mc.online ? T('机器离线') : st.available ? (ov ? fmtBytes(ov.size.total) + (ov.budget ? ' / ' + fmtBytes(ov.budget.max_bytes) : '') + ' · ' + ov.projects.length + T(' 个项目') : 'AMem ' + esc(st.version || '')) : esc(st.error || T('不可用'));
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
  return ('<div class="kbars"><span class="kbar"><span class="kb-l">' + T('存储') + '</span>') + meter(pct) + '<span class="kb-r">' + (pct < 1 ? '<1' : Math.round(pct)) + '%</span></span></div>';
}
function memOverviewSoon(mc) {
  // lazy: the widget will fetch the overview on first render, and refresh it every 5 min
  if (!mc.online || !mc.memory || !mc.memory.available) return;
  const key = mc.id + ':overview';
  const age = Date.now() / 1000 - (MEM.asked[key] || 0);
  if (age < (MEM.data[key] ? 300 : 30)) return;          // fresh, or a request is in flight
  if (send({ op: 'memory', m: mc.id, action: 'overview' })) MEM.asked[key] = Date.now() / 1000;
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
      toast(r.dry_run ? T('预估清理') : n ? T('已清理') : T('无需清理'), n ? n + T(' 条记录 · ') + fmtBytes(r.before.total) + ' → ' + fmtBytes(r.after.total) : T('当前用量在上限之内'), null, 'done');
    } else toast(T('已更新存储上限'), '', null, 'done');
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
const KIND_NAMES = { message: T('对话'), tool_call: T('工具调用'), tool_result: T('工具输出'), document: T('文档'), memory: T('记忆') };
function renderMemory() {
  const body = $('#mem-body'); const mc = S.byId[MEM.m];
  $('#mem-back').style.visibility = MEM.view === 'home' ? 'hidden' : '';   // keep the title centred
  if (!mc || !mc.memory) { setHTML(body, ('<p class="g-f">' + T('这台机器没有开启记忆功能。') + '</p>')); return; }
  const ov = MEM.data[MEM.m + ':overview'];
  if (MEM.view === 'home') {
    $('#dlg-mem-title').textContent = T('记忆 · ') + mc.id;
    if (!ov) { setHTML(body, '<div class="empty">' + (mc.memory.available ? T('读取中…') : esc(mc.memory.error || T('不可用'))) + '</div>'); return; }
    const last = ov.last_compact;
    let h = ('<div class="g-h">' + T('存储') + '</div><div class="group">') +
      ('<div class="cell stack"><span class="grow">' + T('已用 ')) + fmtBytes(ov.size.total) + (ov.budget ? T(' / 上限 ') + fmtBytes(ov.budget.max_bytes) : T('（未设上限）')) +
      ('<div class="sub">' + T('记录 ')) + fmtBytes(ov.size.objects) + T(' · 索引 ') + fmtBytes(ov.size.index) + (last ? T(' · 上次清理 ') + esc(ago(Date.parse(last.utc) / 1000)) : '') + '</div>' + memMeter(ov) + '</span></div>' +
      ('<div class="cell"><span class="grow" id="mem-budget-lbl">' + T('存储上限') + '</span><input id="mem-budget" class="mono" style="width:90px;text-align:right" value="') + esc(ov.budget ? fmtBytes(ov.budget.max_bytes).replace(/\.0+ /, ' ').replace(' ', '') : '') + '" placeholder="20G" aria-labelledby="mem-budget-lbl" autocapitalize="off" autocorrect="off" spellcheck="false">' +
      ('<button type="button" class="btn sm" id="mem-budget-save">' + T('保存') + '</button></div>') +
      '<button type="button" class="cell" id="mem-compact"><span class="cell-ic" style="background:#5C636E" aria-hidden="true">' + ic('broom') + ('</span><span class="grow">' + T('立即整理') + '<div class="sub">' + T('超过上限时清理最旧的工具输出和工具调用；记忆和对话不会被删') + '</div></span></button>') +
      ('</div><div class="g-h">' + T('项目') + '</div><div class="group">');
    h += ov.projects.length ? ov.projects.map((p) => '<button type="button" class="cell" data-proj="' + esc(p.project) + '"><span class="grow">' + esc(p.project) +
      '<div class="sub">' + p.memories + T(' 条记忆 · ') + fmtNum(p.records) + T(' 条记录 · ') + fmtBytes(p.object_bytes) + (p.last ? ' · ' + esc(ago(Date.parse(p.last) / 1000)) : '') + '</div></span>' +
      '<svg class="ic chev" aria-hidden="true"><use href="#i-chev"/></svg></button>').join('') : ('<div class="cell"><span class="grow sub">' + T('还没有记忆。开启后新建的 Claude Code / Codex 会话会自动记录。') + '</span></div>');
    h += '</div>';
    setHTML(body, h);
    for (const b of $$('[data-proj]', body)) b.onclick = () => memOpenProject(b.dataset.proj);
    $('#mem-budget-save').onclick = () => {
      const v = $('#mem-budget').value.trim();
      if (!/^(0|\d+(\.\d+)?\s*[KMGT]i?B?)$/i.test(v)) { toast(T('格式不对'), T('例如 20G、500M，0 表示不设上限'), null, 'error'); return; }
      MEM.busy = true; memAsk(MEM.m, 'budget', { budget: v });
    };
    $('#mem-compact').onclick = async () => {
      if (MEM.busy) return;
      if (!(await confirmSheet(T('立即整理记忆？'), T('只在超过上限时删除最旧的工具输出和工具调用。整理过的记忆、对话和最近 ') + ((mc.memory.keep_days) || 30) + T(' 天的记录都会保留。'), T('整理')))) return;
      MEM.busy = true; memAsk(MEM.m, 'compact'); toast(T('整理中…'), '', null, 'info');
    };
    return;
  }
  const p = MEM.project; const proj = ov && ov.projects.find((x) => x.project === p);
  $('#dlg-mem-title').textContent = p;
  const mems = MEM.data[MEM.m + ':memories:' + p];
  const hits = MEM.data[MEM.m + ':search:' + p];
  let h = '<div class="group"><div class="cell"><svg class="ic sm" aria-hidden="true"><use href="#i-search"/></svg>' +
    ('<input id="mem-q" type="search" class="grow mem-q" placeholder="' + T('搜索这个项目的历史（命令、输出、对话）') + '" value="') + esc(MEM.q) + ('" enterkeyhint="search" autocapitalize="off" autocorrect="off" spellcheck="false" aria-label="' + T('搜索记忆') + '"></div></div>');
  if (proj) h += '<p class="g-f">' + Object.entries(proj.kinds).map(([k, n]) => esc(KIND_NAMES[k] || k) + ' ' + fmtNum(n)).join(' · ') + (proj.pruned ? T(' · 已清理 ') + fmtNum(proj.pruned) : '') + '</p>';
  if (hits && MEM.q) {
    h += ('<div class="g-h">' + T('搜索结果') + '</div><div class="group">') + (hits.length ? hits.map((r) => '<div class="cell stack"><span class="grow"><span class="tag">' + esc(KIND_NAMES[r.kind] || r.kind) + '</span> <span class="sub">' + esc(ago(Date.parse(r.timestamp) / 1000)) + ' · ' + esc(r.source) + '</span>' +
      '<div class="mem-text">' + esc(r.text) + '</div></span></div>').join('') : ('<div class="cell"><span class="grow sub">' + T('没有找到') + '</span></div>')) + '</div>';
  }
  h += ('<div class="g-h">' + T('已保存的记忆') + '</div><div class="group">');
  h += !mems ? ('<div class="cell"><span class="grow sub">' + T('读取中…') + '</span></div>') : mems.length ? mems.map((r) => '<div class="cell stack"><span class="grow"><b>' + esc(r.memory_key) + '</b>' + (r.conflict ? (' <span class="tag">' + T('冲突') + '</span>') : '') +
    ' <span class="sub">' + esc(ago(Date.parse(r.timestamp) / 1000)) + '</span><div class="mem-text md">' + md(r.text) + '</div></span></div>').join('') :
    ('<div class="cell"><span class="grow sub">' + T('还没有保存的结论。助手确认重要结论后会自动保存；完整历史已在后台记录，可以在上面搜索。') + '</span></div>');
  h += '</div>';
  setHTML(body, h);
  renderRich(body);
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

// ------------------------------------------------------------------ the vault (知识库): vault-linker's knowledge base
// Routes: #kb (home), #kb/p/<id> (project), #kb/n/<path> (note), #kb/d/<folder>, #kb/s/<query>. Reads go through
// op vault_view {action, key}; notes render with the chat's markdown plus vault links ([[wikilinks]], relative links
// and images, resolved next to the note). Editing a note: the whole text in a textarea, saved only if unchanged since.
const KB = { page: null, data: {}, asked: {}, err: {}, paths: [], edit: null, imgs: new Map() };
function kbOn() { return S.machines.some((mc) => mc.vault); }
function kbParse(h) {
  const m = /^kb(?:\/([pnds])\/(.*))?$/.exec(h || ''); if (!m) return null;
  if (!m[1]) return { t: 'home', arg: '' };
  try { return { t: m[1], arg: decodeURIComponent(m[2] || '') }; } catch (_) { return { t: 'home', arg: '' }; }
}
function kbHash(pg) { return pg.t === 'home' ? 'kb' : 'kb/' + pg.t + '/' + encodeURIComponent(pg.arg); }
function kbGo(pg, replace) {
  const h = '#' + kbHash(pg);
  if (location.hash !== h) history[replace ? 'replaceState' : 'pushState']({ glimpse: 1, kb: 1 }, '', h);
  kbShow(pg);
}
function kbShow(pg) {
  for (const d of $$('dialog.sheet[open]')) d.close();
  if (curScreen !== 'kb') show('kb', 'fwd');
  KB.page = pg; KB.edit = null;
  $('#kb-q').value = pg.t === 's' ? pg.arg : '';
  kbAsk('overview', '');                                  // every note's path: [[wikilinks]] resolve against it
  kbFetch(pg, true);
  kbRender(); $('#kb-body').scrollTop = 0;
}
function kbLeave() {             // a route outside the vault: back to the screen it was opened from
  if (curScreen !== 'kb') return;
  KB.page = null; KB.edit = null;
  show(S.view ? 'view' : 'home', 'back');
  if (!S.view) renderHome();
}
function kbKey(action, arg) { return action + ':' + (arg || ''); }
function kbAsk(action, arg, extra, force) {
  const key = kbKey(action, arg);
  if (!force && (KB.data[key] !== undefined || KB.asked[key] > Date.now() - 15000)) return;
  if (KB.asked[key] > Date.now() - 1500) return;          // the same request a moment ago: wait for it
  if (send(Object.assign({ op: 'vault_view', action, key }, extra || {}))) KB.asked[key] = Date.now();
}
const KB_ACTION = { home: 'overview', p: 'project', n: 'read', d: 'ls', s: 'search' };
function kbPageKey(pg) { return kbKey(KB_ACTION[pg.t], pg.t === 'home' ? '' : pg.arg); }
function kbFetch(pg, force) {
  const a = pg.arg;
  const extra = { home: null, p: { id: a }, n: { path: a }, d: { folder: a }, s: { q: a } }[pg.t];
  kbAsk(KB_ACTION[pg.t], pg.t === 'home' ? '' : a, extra, force);
}
function onVault(m) {
  const key = m.key; if (typeof key !== 'string') return;
  if (m.action === 'write') { kbSaved(m.data || {}); return; }
  if (m.action === 'sync') { kbSynced(m.data || {}); return; }
  KB.data[key] = m.data; delete KB.err[key]; delete KB.asked[key];
  if (m.action === 'overview' && m.data) KB.paths = m.data.paths || [];
  if (m.action === 'asset') { kbImage(m.data || {}); return; }
  if (m.action === 'read' && KB.edit && KB.edit.loading && key === kbKey('read', KB.edit.path) + '#raw') {
    KB.edit = { path: KB.edit.path, text: m.data.text, mtime: m.data.mtime };
  }
  if (curScreen === 'kb') kbRender();
}
function onVaultErr(m) {
  const key = m.key;
  if (m.action === 'write' || m.action === 'sync') {
    KB.busy = false; toast(m.action === 'write' ? T('没有保存') : T('同步失败'), m.msg, null, 'error'); kbRender(); return;
  }
  if (typeof key === 'string') { KB.err[key] = m.msg; delete KB.asked[key]; }
  if (m.action === 'asset') { for (const im of $$('img.kb-img[data-path]', $('#kb-body'))) if (kbKey('asset', im.dataset.path) === key) im.replaceWith(kbImgFail(im.alt, m.msg)); return; }
  if (KB.edit && KB.edit.loading) KB.edit = null;
  if (curScreen === 'kb') kbRender();
}
// ---- paths inside the vault: a link in a note is relative to the note's folder ("/x" = from the vault's top)
function kbDir(p) { const i = p.lastIndexOf('/'); return i < 0 ? '' : p.slice(0, i); }
function kbJoin(base, rel) {
  const out = rel.startsWith('/') ? [] : (base ? base.split('/') : []);
  for (const seg of rel.replace(/^\/+/, '').split('/')) {
    if (seg === '' || seg === '.') continue;
    if (seg === '..') { if (!out.length) return null; out.pop(); } else out.push(seg);
  }
  return out.join('/');
}
function kbWiki(name, from) {
  // [[name]] as Obsidian finds it: a path, else a note with that file name (the nearest one to this note)
  const n = name.trim().replace(/\.md$/i, ''); if (!n) return null;
  const want = n + '.md'; const dir = kbDir(from || '');
  if (KB.paths.includes(want)) return want;
  const hits = KB.paths.filter((p) => p === want || p.endsWith('/' + want));
  if (!hits.length) return null;
  hits.sort((a, b) => (b.startsWith(dir) - a.startsWith(dir)) || a.length - b.length);
  return hits[0];
}
function kbLinkTarget(href, from) {
  let h = href.trim().replace(/^<|>$/g, '');
  if (/^[a-z][a-z0-9+.-]*:/i.test(h) || h.startsWith('#')) return null;      // http(s) etc. are handled by md()
  h = h.split('#')[0]; if (!h) return null;
  try { h = decodeURIComponent(h); } catch (_) {}
  return kbJoin(kbDir(from || ''), h);
}
// vault links in a rendered note: while MD_LINK is set (kbMd), mdInline hands it ![alt](figs/x.png), [text](other.md)
// and [[name|alias]] (http(s) links stay as they are); paths resolve next to the note. Images load from the vault.
function kbMd(text, from) {
  MD_LINK = (bang, label, href, wiki, alias) => {
    if (wiki != null) {
      const p = kbWiki(unesc(wiki).split('#')[0], from);
      return p ? kbA(p, alias || wiki) : '<span class="kb-missing" title="' + esc(T('知识库里没有这篇')) + '">' + (alias || wiki) + '</span>';
    }
    const p = kbLinkTarget(unesc(href), from);
    if (p == null) return null;
    if (bang) return KB_IMG.test(p) ? '<img class="kb-img" alt="' + label.replace(/\u0002\d+\u0003/g, '') + '" data-path="' + esc(p) + '" loading="lazy" decoding="async">' : null;
    return kbA(p, label);
  };
  try { return md(text); } finally { MD_LINK = null; }
}
function unesc(s) { return String(s).replace(/&(amp|lt|gt|quot|#39);/g, (_, e) => ({ amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'" }[e])); }
const KB_IMG = /\.(png|jpe?g|gif|webp|svg)$/i;
function kbA(p, html) {
  // html: already escaped (it comes out of mdInline)
  const pg = /\.md$/i.test(p) ? { t: 'n', arg: p } : !/\.[a-z0-9]{1,8}$/i.test(p) ? { t: 'd', arg: p.replace(/\/$/, '') } : null;
  if (!pg) return '<span class="kb-file" title="' + esc(p) + '">' + html + '</span>';
  return '<a class="kb-link" href="#' + esc(kbHash(pg)) + '">' + html + '</a>';
}
function kbImages(root) {
  for (const im of $$('img.kb-img[data-path]:not([src])', root)) {
    const u = KB.imgs.get(im.dataset.path);
    if (u) im.src = u; else kbAsk('asset', im.dataset.path, { path: im.dataset.path });
  }
}
function kbImgFail(alt, msg) {
  const sp = document.createElement('span'); sp.className = 'kb-imgfail'; sp.textContent = (alt || T('图片')) + ' (' + T('无法显示') + ')'; sp.title = msg || ''; return sp;
}
function kbImage(d) {
  if (!d.path || !/^image\/(png|jpeg|gif|webp|svg\+xml)$/.test(d.mime || '') || !/^[A-Za-z0-9+/=]*$/.test(d.data || '')) return;
  const u = 'data:' + d.mime + ';base64,' + d.data;
  KB.imgs.set(d.path, u);
  if (KB.imgs.size > 80) KB.imgs.delete(KB.imgs.keys().next().value);
  delete KB.data[kbKey('asset', d.path)];                          // the pixels live in KB.imgs only
  for (const im of $$('img.kb-img[data-path]', $('#kb-body'))) if (im.dataset.path === d.path) im.src = u;
}
$('#kb-body').addEventListener('click', (e) => {
  const a = e.target.closest('a[href^="#kb"]'); if (!a) return;
  const pg = kbParse((a.getAttribute('href') || '').slice(1)); if (!pg) return;
  e.preventDefault(); kbGo(pg);
});
// ---- pages
const KB_STATUS = { planned: T('计划中'), running: T('进行中'), done: T('完成'), failed: T('失败'), active: T('进行中'), paused: T('暂停'),
  archived: T('已归档'), unread: T('未读'), reading: T('在读'), read: T('已读') };
const KB_TYPE = { project: T('项目'), experiment: T('实验'), log: T('日志'), doc: T('文档'), paper: T('论文'), note: T('笔记') };
function kbPill(st) { return st ? '<span class="kb-st ' + esc(st) + '">' + esc(KB_STATUS[st] || st) + '</span>' : ''; }
function kbRow(f, sub) {
  const pg = { t: 'n', arg: f.path };
  return '<a class="cell kb-row" href="#' + esc(kbHash(pg)) + '"><span class="grow"><span class="kb-t">' + esc(f.title || f.path) + '</span>' + kbPill(f.status) +
    '<div class="sub">' + [sub != null ? sub : esc(KB_TYPE[f.type] || '') + (f.type ? ' · ' : '') + esc(f.path), f.mtime ? esc(ago(f.mtime)) : ''].filter(Boolean).join(' · ') + '</div></span>' +
    '<svg class="ic chev" aria-hidden="true"><use href="#i-chev"/></svg></a>';
}
function kbDirRow(d) {
  return '<a class="cell kb-row" href="#' + esc(kbHash({ t: 'd', arg: d.path })) + '"><span class="cell-ic" style="background:#5C8DF6" aria-hidden="true">' + ic('folder') + '</span>' +
    '<span class="grow">' + esc(d.name) + '<div class="sub">' + d.notes + T(' 篇') + '</div></span><svg class="ic chev" aria-hidden="true"><use href="#i-chev"/></svg></a>';
}
function kbGroup(title, rows, empty) {
  return (title ? '<div class="g-h">' + title + '</div>' : '') + '<div class="group">' + (rows.length ? rows.join('') : '<div class="cell"><span class="grow sub">' + empty + '</span></div>') + '</div>';
}
function kbWait(key) {
  return KB.err[key] ? '<div class="empty">' + ic('alert', 'lg') + '<div>' + esc(KB.err[key]) + '</div></div>' :
    '<div class="kb-pad"><div class="skel" style="height:22px;width:60%"></div><div class="skel" style="height:120px;margin-top:14px"></div></div>';
}
function kbCrumbs(path) {
  // notes/diffusion/x.md -> 知识库 / notes / diffusion: each a link to that folder
  const parts = path.split('/'); parts.pop();
  let acc = ''; let h = '<a class="kb-link" href="#kb">' + T('知识库') + '</a>';
  for (const p of parts) { acc = acc ? acc + '/' + p : p; h += ' / <a class="kb-link" href="#' + esc(kbHash({ t: 'd', arg: acc })) + '">' + esc(p) + '</a>'; }
  return '<nav class="kb-crumbs" aria-label="' + T('位置') + '">' + h + '</nav>';
}
function kbRender() {
  const pg = KB.page; const body = $('#kb-body'); if (!pg) return;
  const key = kbPageKey(pg); const d = KB.data[key];
  $('#kb-edit').hidden = pg.t !== 'n' || !d || !!KB.edit;
  $('#kb-refresh').hidden = !!KB.edit;
  $('#kb-search').hidden = !!KB.edit || pg.t === 'n';
  $('#kb-back').setAttribute('aria-label', KB.edit ? T('取消编辑') : T('返回'));
  if (KB.edit) { kbRenderEdit(); return; }
  const title = pg.t === 'p' ? pg.arg : pg.t === 'n' ? (d ? (d.meta && d.meta.title) || kbTitleOf(d) : '')
    : pg.t === 'd' ? (pg.arg.split('/').pop() || T('知识库')) : pg.t === 's' ? T('搜索') : T('知识库');
  $('#kb-title').textContent = title || T('知识库');
  $('#kb-sub').textContent = pg.t === 'n' ? pg.arg : pg.t === 'd' ? pg.arg : pg.t === 's' ? '“' + pg.arg + '”' : '';
  if (d === undefined) { setHTML(body, kbWait(key)); return; }
  let h = '';
  if (pg.t === 'home') h = kbHomeHtml(d);
  else if (pg.t === 'p') h = kbProjectHtml(d);
  else if (pg.t === 'd') h = kbDirHtml(d);
  else if (pg.t === 's') h = kbGroup('', (d.hits || []).map((x) => kbRow(x, esc(x.snippet || x.path))), T('没有找到'));
  else if (pg.t === 'n') h = kbNoteHtml(d);
  setHTML(body, '<div class="kb-pad">' + h + '</div>');
  if (pg.t === 'n') { renderRich(body); kbImages(body); }
  if (pg.t === 'home') {
    const sy = $('#kb-sync'); if (sy) sy.onclick = kbSync;
  }
}
function kbTitleOf(d) { const m = /^#\s+(.+)$/m.exec(d.body || ''); return m ? m[1].trim() : d.path.split('/').pop().replace(/\.md$/, ''); }
function kbHomeHtml(d) {
  const sync = d.dirty ? d.dirty + T(' 个改动未提交') : d.ahead ? d.ahead + T(' 个提交未推送') : d.remote ? T('已同步到 GitHub') : T('只在本机（没有远程仓库）');
  let h = '<div class="group"><div class="cell"><span class="cell-ic" style="background:#8E5CF7" aria-hidden="true">' + ic('book') + '</span>' +
    '<span class="grow">' + esc(sync) + '<div class="sub">' + esc(d.vault || '') + '</div></span>' +
    (d.remote ? '<button type="button" class="btn sm" id="kb-sync"' + (KB.busy ? ' disabled' : '') + '>' + (KB.busy ? T('同步中…') : T('同步')) + '</button>' : '') + '</div></div>';
  h += kbGroup(T('项目'), (d.projects || []).map((p) => '<a class="cell kb-row" href="#' + esc(kbHash({ t: 'p', arg: p.id })) + '"><span class="grow"><span class="kb-t">' + esc(p.id) + '</span>' + kbPill(p.status) +
    '<div class="sub">' + (p.goal ? esc(p.goal) + ' · ' : '') + p.experiments + T(' 个实验') + (p.repo ? ' · GitHub' : '') + '</div></span><svg class="ic chev" aria-hidden="true"><use href="#i-chev"/></svg></a>'),
    T('还没有项目。新建会话时打开“项目工作流”，或在会话操作里启用'));
  h += kbGroup(T('分类'), [['notes', T('笔记'), 'pencil', '#F5A524'], ['papers', T('论文'), 'file', '#E8875B'], ['inbox', T('收件箱'), 'inbox', '#5C636E']].map(([p, n, i, c]) =>
    '<a class="cell kb-row" href="#' + esc(kbHash({ t: 'd', arg: p })) + '"><span class="cell-ic" style="background:' + c + '" aria-hidden="true">' + ic(i) + '</span><span class="grow">' + n +
    (p === 'inbox' && d.inbox ? '<div class="sub">' + d.inbox + T(' 条待整理') + '</div>' : '') + '</span><svg class="ic chev" aria-hidden="true"><use href="#i-chev"/></svg></a>'), '');
  h += kbGroup(T('最近更新'), (d.recent || []).map((f) => kbRow(f)), T('还没有笔记'));
  return h;
}
function kbProjectHtml(d) {
  const meta = (d.page && d.page.meta) || {};
  const exp = d.exp || []; const logs = d.log || []; const docs = d.docs || []; const refs = d.refs || [];
  let h = '<div class="group kb-card">' +
    '<div class="cell stack"><span class="grow">' + kbPill(meta.status) + (meta.goal ? ' <span class="kb-goal">' + esc(meta.goal) + '</span>' : '') +
    (meta.repo ? '<div class="sub"><a href="' + esc(/^https:\/\/github\.com\//.test(meta.repo) ? meta.repo : '#') + '" target="_blank" rel="noopener noreferrer">' + esc(meta.repo) + '</a></div>' : '') +
    (Array.isArray(meta.locations) ? meta.locations.map((l) => '<div class="sub mono">' + esc((l.machine || '') + ' · ' + (l.path || '')) + '</div>').join('') : '') +
    '</span></div><a class="cell kb-row" href="#' + esc(kbHash({ t: 'n', arg: d.page.path })) + '"><span class="grow">' + T('项目页') + '<div class="sub">' + T('目标、现状、下一步') + '</div></span><svg class="ic chev" aria-hidden="true"><use href="#i-chev"/></svg></a></div>';
  h += kbGroup(T('实验') + ' · ' + exp.length, exp.map((e) => {
    const mt = e.metrics && typeof e.metrics === 'object' ? Object.entries(e.metrics).slice(0, 4).map(([k, v]) => esc(k) + ' ' + esc(typeof v === 'object' ? JSON.stringify(v) : v)).join(' · ') : '';
    return kbRow(e, esc([e.date, e.machine].filter(Boolean).join(' · ')) + (mt ? ' · <span class="mono">' + mt + '</span>' : ''));
  }), T('还没有实验记录'));
  h += kbGroup(T('日志'), logs.slice(0, 30).map((f) => kbRow(f, '')), T('还没有日志'));
  if (docs.length) h += kbGroup(T('文档'), docs.map((f) => kbRow(f, '')), '');
  if (refs.length) h += kbGroup(T('资料'), refs.map((f) => kbRow(f, '')), '');
  return h;
}
function kbDirHtml(d) {
  let h = d.folder ? kbCrumbs(d.folder + '/x') : '';
  h += kbGroup('', (d.dirs || []).map(kbDirRow).concat((d.files || []).map((f) => kbRow(f, ''))), T('这里还没有笔记'));
  if (d.other) h += '<p class="g-f">' + d.other + T(' 个其他文件（图片、代码等）') + '</p>';
  return h;
}
function kbNoteHtml(d) {
  const meta = d.meta || {};
  const skip = new Set(['title', 'type']);
  const rows = Object.entries(meta).filter(([k, v]) => !skip.has(k) && v != null && v !== '' && !(Array.isArray(v) && !v.length) && !(typeof v === 'object' && !Array.isArray(v) && !Object.keys(v).length));
  const val = (v) => Array.isArray(v) ? v.map((x) => typeof x === 'object' ? JSON.stringify(x) : String(x)).join(', ') : typeof v === 'object' ? Object.entries(v).map(([a, b]) => a + ' ' + (typeof b === 'object' ? JSON.stringify(b) : b)).join(' · ') : String(v);
  let h = kbCrumbs(d.path);
  if (meta.type || rows.length) {
    h += '<div class="kb-meta">' + (meta.type ? '<span class="tag">' + esc(KB_TYPE[meta.type] || meta.type) + '</span>' : '') +
      rows.map(([k, v]) => k === 'status' ? kbPill(v) : '<span class="kb-kv"><b>' + esc(k) + '</b> ' + esc(val(v)) + '</span>').join('') + '</div>';
  }
  return h + '<article class="md kb-md">' + kbMd(d.body || '', d.path) + '</article>';
}
// ---- editing a note: the whole file (front matter too) in a textarea; saved only if nobody changed it meanwhile
function kbEditStart() {
  const pg = KB.page; if (!pg || pg.t !== 'n') return;
  KB.edit = { path: pg.arg, loading: true };
  send({ op: 'vault_view', action: 'read', key: kbKey('read', pg.arg) + '#raw', path: pg.arg, raw: true });
  kbRender();
}
function kbRenderEdit() {
  const e = KB.edit; const body = $('#kb-body');
  $('#kb-title').textContent = T('编辑'); $('#kb-sub').textContent = e.path;
  if (e.loading) { setHTML(body, kbWait('')); return; }
  if (!$('#kb-text')) {
    body._h = null;
    body.innerHTML = '<div class="kb-pad kb-editor"><label class="sr" for="kb-text">' + T('笔记内容') + '</label>' +
      '<textarea id="kb-text" class="kb-text mono" autocapitalize="off" autocorrect="off" spellcheck="false"></textarea>' +
      '<div class="kb-edit-bar"><span class="sub">' + T('保存后在“同步”时提交并推送') + '</span>' +
      '<button type="button" class="btn" id="kb-cancel">' + T('取消') + '</button><button type="button" class="btn primary" id="kb-save">' + T('保存') + '</button></div></div>';
    const ta = $('#kb-text'); ta.value = e.text;
    $('#kb-cancel').onclick = kbEditCancel;
    $('#kb-save').onclick = kbSave;
    ta.addEventListener('keydown', (ev) => { if ((ev.metaKey || ev.ctrlKey) && ev.key === 's') { ev.preventDefault(); kbSave(); } });
  }
  $('#kb-save').disabled = !!KB.busy;
  $('#kb-save').textContent = KB.busy ? T('保存中…') : T('保存');
}
async function kbEditCancel() {
  const e = KB.edit; const ta = $('#kb-text');
  if (e && ta && ta.value !== e.text && !(await confirmSheet(T('放弃修改？'), T('改动还没有保存。'), T('放弃')))) return;
  KB.edit = null; $('#kb-body')._h = null; kbRender();
}
function kbSave() {
  const e = KB.edit; const ta = $('#kb-text'); if (!e || !ta || KB.busy) return;
  if (ta.value === e.text) { kbEditCancel(); return; }
  KB.busy = true;
  if (!send({ op: 'vault_view', action: 'write', key: 'write', path: e.path, text: ta.value, expect: e.mtime })) { KB.busy = false; toast(T('未连接'), T('稍后再试'), null, 'error'); }
  kbRender();
}
function kbSaved(d) {
  KB.busy = false;
  const path = KB.edit && KB.edit.path;
  KB.edit = null; $('#kb-body')._h = null;
  toast(T('已保存'), d.path || '', null, 'done');
  if (path) { delete KB.data[kbKey('read', path)]; kbAsk('read', path, { path }, true); }
  delete KB.data[kbKey('overview', '')];
  kbRender();
}
function kbSync() {
  if (KB.busy) return;
  KB.busy = true;
  send({ op: 'vault_view', action: 'sync', key: 'sync', push: true });
  kbRender();
}
function kbSynced(d) {
  KB.busy = false;
  toast(d.pushed ? T('已推送到 GitHub') : T('已提交'), d.commit || T('没有新的改动'), null, 'done');
  delete KB.data[kbKey('overview', '')]; kbAsk('overview', '', null, true);
  kbRender();
}
// ---- wiring
$('#btn-vault').onclick = () => kbGo({ t: 'home', arg: '' });
$('#kb-back').onclick = () => {
  if (KB.edit) { kbEditCancel(); return; }
  if (history.state && history.state.kb) history.back();
  else { history.replaceState(null, '', location.pathname + (S.view ? '#' + S.view.m + '/' + S.view.sid : '')); kbLeave(); }
};
$('#kb-refresh').onclick = () => {
  if (!KB.page) return;
  delete KB.data[kbKey('overview', '')]; kbAsk('overview', '', null, true);
  kbFetch(KB.page, true);
};
$('#kb-edit').onclick = kbEditStart;
$('#kb-search').addEventListener('submit', (e) => {
  e.preventDefault();
  const q = $('#kb-q').value.trim().slice(0, 200); if (!q) return;
  delete KB.data[kbKey('search', q)];
  kbGo({ t: 's', arg: q }, KB.page && KB.page.t === 's');
});
$('#act-kb').onclick = () => {
  const v = S.view; const s = v && session(v.m, v.sid); if (!s || !s.vault) return;
  closeSheet($('#dlg-more'));
  setTimeout(() => kbGo({ t: 'p', arg: s.vault }), 230);
};
$('#acct-revoke-others').onclick = async () => {
  if (!(await confirmSheet(T('退出其他所有设备？'), T('除了这台以外，所有设备都需要重新登录。'), T('全部退出')))) return;
  try { await api('/api/devices/revoke', { all: true }); toast(T('已退出其他设备'), '', null, 'done'); renderAccount(); } catch (e) { toast(T('操作失败'), e.message, null, 'error'); }
};

function refreshOpenSheets() {
  if ($('#dlg-settings').open) {
    renderSettingsMachines(); renderKeyPage(); renderSettingsNav();
    if (!$('#set-ext').contains(document.activeElement)) renderExtPage();     // not while typing a setting
    renderSchedPage();
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
    $('#set-push').textContent = T('已开启 ✓');
  } catch (err) { $('#set-push').textContent = T('开启失败：') + err.message; }
};
$('#btn-push-test').onclick = async () => {
  try { const r = await api('/api/push/test', {}); toast(T('已发送'), T('发给 ') + r.count + T(' 个设备'), null, 'done'); } catch (err) { toast(T('发送失败'), err.message, null, 'error'); }
};
$('#btn-logout').onclick = async () => {
  if (!(await confirmSheet(T('退出登录？'), T('退出后需要重新输入访问令牌。'), T('退出登录')))) return;
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

$('#term-size-btn').onclick = () => openSizeSheet();

// an existing push subscription is registered again on every start: the hub then knows which device it
// belongs to (and only holds that device's notifications back while this device has the app on screen)
async function pushRefresh() {
  try {
    if (!('serviceWorker' in navigator) || !('PushManager' in window) || !window.Notification || Notification.permission !== 'granted') return;
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (sub) await api('/api/push/subscribe', sub.toJSON());
  } catch (_) {}
}
setTimeout(pushRefresh, 3000);

boot();
