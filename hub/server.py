#!/usr/bin/env python3
"""Glimpse hub: serves the phone web app and relays between phones and machine agents."""
import argparse
import asyncio
import hmac
import ipaddress
import json
import logging
import copy
import os
import re
import ssl
import subprocess
import sys
import time
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
from auth import PAIR_TTL, Auth  # noqa: E402
import keybalance as KB  # noqa: E402
import webpush  # noqa: E402
from machines import Machine  # noqa: E402
import machines as M  # noqa: E402

ROOT = C.ROOT
WEB = os.path.join(ROOT, "web")
DATA = C.DATA
VERSION = "0.4.1"
log = logging.getLogger("glimpse.hub")
PHONE_QUEUE_MAX = 4000       # messages; a phone that falls this far behind is disconnected
VISIBLE_FRESH = 15           # s; a "visible" phone that has been silent longer is treated as gone (push again)
CODE_CHECK_EVERY = 10        # s; how often the agent source on disk is checked against what the machines run
FS_ACTIONS = ("list", "read", "git", "diff", "write", "create", "mkdir")
FS_TEXT_LIMIT = 2 * 1024 * 1024   # characters in a saved / new file (the agent checks the encoded size too)
CERT_RETRY = 600             # s between certificate attempts while serving without one
CERT_RENEW = 12 * 3600
HOST_RE = re.compile(r"^[A-Za-z0-9_.@:-]+$")
ARG_RE = re.compile(r"^[A-Za-z0-9_.:/\[\]@+=-]{1,120}$")   # model / effort / mode values that go on argv
RESUME_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
MEMORY_ACTIONS = ("overview", "memories", "context", "sessions", "search", "compact", "budget")
CONTROL_ACTIONS = ("mode", "model", "effort", "goal", "approval", "sandbox")
MENU_KEYS = ("esc", "s")
MODELS_TTL = 600             # s; model / effort lists per (machine, kind) are cached this long
RUNTIME = {}                 # resolved hub.listen / tailscale_hostname ("auto" -> tailscale), never saved


def agent_error(r, default):
    """Error text of an agent reply; an agent too old for the op says so instead of "unknown op"."""
    msg = r.get("msg") or default
    return "这台机器的 agent 版本较旧，需要更新" if msg.startswith("unknown op") else msg


def build_argv(kind, base, msg):
    """argv for a new session: preset argv + phone extras + flags for the advanced create fields."""
    args = msg.get("args")
    if isinstance(args, list) and args:
        argv = [str(a) for a in args][:60]        # full argv from an older client
    else:
        argv = list(base) + [str(a) for a in (msg.get("extra") or [])][:50]

    def val(name):
        v = msg.get(name)
        if v is None or v == "":
            return None
        if not isinstance(v, str) or not ARG_RE.match(v):
            raise ValueError("无效的 %s: %s" % (name, str(v)[:40]))
        return v
    if kind == "claude":
        if val("model"):
            argv += ["--model", val("model")]
        if val("effort") == "ultracode":
            argv += ["--effort", "xhigh"]                # ultracode = xhigh + /effort ultracode typed after start
        elif val("effort"):
            argv += ["--effort", val("effort")]
        if val("mode") and val("mode") != "default":    # "default" = keep the settings file's defaultMode
            argv += ["--permission-mode", val("mode")]
    elif kind == "codex":
        if val("model"):
            argv += ["-m", val("model")]
        if val("effort"):
            argv += ["-c", 'model_reasoning_effort="%s"' % val("effort")]
        if val("approval"):
            argv += ["-a", val("approval")]
        if val("sandbox"):
            argv += ["-s", val("sandbox")]
    return argv


def init_cmds(kind, msg):
    """Slash commands the agent types once the new session is up (only for flags with no argv equivalent)."""
    if kind == "claude" and msg.get("effort") == "ultracode":
        return ["/effort ultracode"]
    return None


def device_name(request):
    """A readable name for the device list, from the User-Agent (no fingerprinting beyond that)."""
    ua = request.headers.get("User-Agent", "")
    for k, v in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Macintosh", "Mac"),
                 ("Windows", "Windows"), ("Linux", "Linux")):
        if k in ua:
            name = v
            break
    else:
        name = "设备"
    for k, v in (("Edg/", "Edge"), ("CriOS", "Chrome"), ("Chrome/", "Chrome"), ("Firefox/", "Firefox"),
                 ("Safari/", "Safari")):
        if k in ua:
            return "%s · %s" % (name, v)
    return name


def origin_ok(request):
    """Cross-site websocket hijacking guard: browsers always send Origin, it must name this host."""
    origin = request.headers.get("Origin")
    if not origin:
        return True                  # non-browser clients (gli attach, tests)
    try:
        return urlsplit(origin).netloc.lower() == (request.host or "").lower()
    except ValueError:
        return False


def check_host(host):
    host = str(host or "").strip()
    if not HOST_RE.match(host) or host.startswith("-"):
        raise ValueError("主机格式不对：只能是 ~/.ssh/config 里的 Host 名或 user@host")
    return host


def check_port(port):
    if port in (None, ""):
        return None
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError("端口必须是数字")
    if not 0 < port < 65536:
        raise ValueError("端口超出范围")
    return port


def check_name(name):
    name = str(name or "").strip()
    if not name or len(name) > 40:
        raise ValueError("名称不能为空（最多 40 个字符）")
    return name


def check_cwd(cwd):
    cwd = str(cwd or "~").strip() or "~"
    if len(cwd) > 300 or "\n" in cwd or "\0" in cwd:
        raise ValueError("默认目录无效")
    return cwd


def next_machine_id(mcs):
    used = [int(x["id"][1:]) for x in mcs if re.match(r"^M\d+$", str(x.get("id") or ""))]
    return "M%d" % (max(used + [0]) + 1)


async def ssh_test(host, port=None):
    """Can the hub reach `host` with its own keys, without prompts, and is there a python3?"""
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"] + (["-p", str(port)] if port else []) + \
        [host, 'python3 -c "import sys;print(sys.version_info[:2])"']
    try:
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except OSError as e:
        return False, "无法运行 ssh: %s" % e
    try:
        out, err = await asyncio.wait_for(proc.communicate(), 20)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False, "连接超时"
    out = out.decode("utf-8", "replace").strip()
    err = "; ".join(ln for ln in err.decode("utf-8", "replace").strip().splitlines()[-3:] if ln)
    if proc.returncode != 0:
        return False, err or "ssh 退出码 %s" % proc.returncode
    m = re.search(r"\((\d+), (\d+)\)", out)
    if not m:
        return False, "没有找到 python3: %s" % (out or err)[:200]
    if (int(m.group(1)), int(m.group(2))) < (3, 8):
        return False, "python3 版本太旧 (%s.%s)，需要 3.8+" % m.groups()
    return True, "连接成功，python %s.%s" % m.groups()


class Phone:
    """One websocket. Messages go through a queue so a slow phone never stalls the machines."""

    def __init__(self, ws, device=None):
        self.ws = ws
        self.device = device       # device session id (None: local CLI)
        self.subs = set()          # (m, sid)
        self.visible = True
        self.last_seen = time.time()
        self.q = asyncio.Queue()
        self.task = asyncio.ensure_future(self._pump())

    def send(self, obj):
        if self.ws.closed:
            return
        if self.q.qsize() > PHONE_QUEUE_MAX:
            log.warning("phone too slow, closing")
            asyncio.ensure_future(self.ws.close())
            return
        self.q.put_nowait(obj)

    async def _pump(self):
        while True:
            obj = await self.q.get()
            try:
                await self.ws.send_str(json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
                                       .encode("utf-8", "replace").decode("utf-8"))
            except (ConnectionError, RuntimeError):
                return

    def close(self):
        self.task.cancel()

    def on_screen(self):
        return self.visible and time.time() - self.last_seen < VISIBLE_FRESH


class Hub:
    def __init__(self, cfg):
        self.cfg = cfg
        self.token = C.token()       # local CLI token: only accepted from 127.0.0.1
        self.auth = Auth(os.path.join(DATA, "auth.json"), C.write_secret)
        self.vapid = webpush.Vapid(os.path.join(DATA, "vapid.json"))
        self.push_path = os.path.join(DATA, "push_subs.json")
        self.push_subs = self._load_push()
        self.phones = set()
        self.machines = {}
        self.order = []
        self.sub_count = {}        # (m, sid) -> number of phones subscribed
        self.reply_to = {}         # (m, req) -> (phone, {"op", "sid"}) for routing the agent's reply
        self.models_cache = {}     # (m, kind) -> (time, reply) for op "models"
        self.models_pending = {}   # (m, kind) -> future of the in-flight agent call
        self.state_dirty = asyncio.Event()
        self.recent_push = {}
        self.http = None
        self.key_usage = {}        # key name -> {limits, today_tokens, ...} from keys.usage_url
        self.key_usage_wake = asyncio.Event()
        self.key_exhausted = {}    # key name -> time it ran out / was rejected (balancing skips it a while)
        self.hot_swapped = {}      # (machine, sid) -> the key before a live swap, until a message goes through
        self.key_swaps = {}        # (machine, sid) -> times of automatic swaps (loop guard)
        self.key_rates = KB.RateTracker()   # measured 7-day spend rate per key (from successive usage polls)
        self.key_moved = {}        # (machine, sid) -> last balancing move
        self.bg = []
        for mc in cfg.get("machines", []):
            if mc.get("enabled", True) is False:
                continue
            self.add_machine(mc)

    def add_machine(self, mc):
        m = Machine(mc, self.on_machine_msg, self.on_machine_state)
        self.machines[m.id] = m
        self.order.append(m.id)
        return m

    # --- push subscriptions
    def _load_push(self):
        try:
            with open(self.push_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def _save_push(self):
        C.write_secret(self.push_path, json.dumps(self.push_subs))

    async def push_all(self, payload, skip=None):
        """Push to every subscription except those of the devices in `skip` (they have the app on screen).
        A subscription saved before devices were recorded is skipped while any device is on screen (the old
        rule) until its device registers it again. Returns the number of subscriptions pushed to."""
        skip = skip or set()
        subs = [s for s in self.push_subs if not skip or (s.get("device") and s["device"] not in skip)]
        if not subs:
            return 0
        gone = []

        async def one(sub):
            try:
                status, text = await webpush.send(self.http, self.vapid, sub, payload, self.cfg["hub"]["push_subject"])
                if status in (404, 410):
                    gone.append(sub["endpoint"])
                elif status >= 300:
                    log.warning("push %s -> %s %s", sub["endpoint"][:40], status, text)
            except Exception as e:
                log.warning("push error: %s", e)
        await asyncio.gather(*(one(s) for s in subs))
        if gone:
            self.push_subs = [s for s in self.push_subs if s["endpoint"] not in gone]
            self._save_push()
        return len(subs) - len(gone)

    # --- presets / keys (secrets never leave the hub)
    def preset(self, m, kind):
        p = (m.cfg.get("presets") or {}).get(kind)
        if isinstance(p, list):
            p = {"argv": p}
        return p or {}

    def presets_view(self, m):
        out = {}
        for kind in ("claude", "codex", "shell"):
            p = self.preset(m, kind)
            out[kind] = {"key": p.get("key")}
        return out

    def keys_view(self):
        """Key names (never values) + the last usage numbers fetched for each."""
        out = []
        for p in self.cfg["keys"].get("profiles") or []:
            if p.get("name"):
                k = {"name": p["name"]}
                u = self.key_usage.get(p["name"])
                if u:
                    k["usage"] = u
                if p["name"] in self.key_exhausted:
                    k["exhausted"] = self.key_exhausted[p["name"]]
                out.append(k)
        return out

    # --- key balancing (keys.balance, optional)
    def balance_view(self):
        b = self.cfg["keys"].get("balance") or {}
        return {"mode": KB.mode_of(self.cfg), "exclude": list(b.get("exclude") or []),
                "usage": bool(self.cfg["keys"].get("usage_url"))}

    def keys_in_use(self):
        n = {}
        for m in self.machines.values():
            for s in m.sessions.values():
                if s.get("key") and s.get("status") not in ("exited",):
                    n[s["key"]] = n.get(s["key"], 0) + 1
        return n

    def best_key(self, avoid=(), any_window=False):
        self.key_exhausted = KB.live_exhausted(self.key_exhausted, self.key_usage)
        return KB.pick_key(KB.pool(self.cfg), self.key_usage, self.key_exhausted, self.keys_in_use(), avoid,
                           rates=self.key_rates.rate, any_window=any_window)

    def balance_applies(self, m, sid, ev):
        """Quick check (no I/O): will balance_on_event act on this error? Same conditions as its early returns."""
        s = m.sessions.get(sid) or {}
        cur = s.get("key")
        if ev.get("kind") != "error" or s.get("kind") not in ("claude", "codex") or not cur \
                or not KB.is_key_fatal("%s %s" % (ev.get("title"), ev.get("body"))):
            return False
        prev = self.hot_swapped.get((m.id, sid))
        if prev is not None and prev != cur:
            return True
        return KB.mode_of(self.cfg) != "off" and cur in KB.pool(self.cfg)

    async def balance_on_event(self, m, sid, ev):
        """An error event: when the session's key ran out / was rejected, switch it to another key (restart +
        resume: claude keeps a message's key for all its retries, so a live swap would only help the message after
        the next one) and type 继续. Returns the event to show instead, or None."""
        s = m.sessions.get(sid) or {}
        cur = s.get("key")
        if ev.get("kind") != "error" or s.get("kind") not in ("claude", "codex") or not cur \
                or not KB.is_key_fatal("%s %s" % (ev.get("title"), ev.get("body"))):
            return None
        prev = self.hot_swapped.pop((m.id, sid), None)
        if prev is not None and prev != cur:
            # swapped live and no message has gone through since: this failure is the old key's (still used for
            # this message), not the new one's. Restart on the new key and go on.
            if prev in KB.pool(self.cfg):
                self.key_exhausted[prev] = time.time()
                self.state_dirty.set()
            hot = await self.switch_key(m, sid, s.get("kind"), cur, cold=True)
            if hot is None:
                return None
            asyncio.ensure_future(self.continue_after_swap(m, sid, hot))
            return {"kind": "keyswap", "title": "%s 不可用，已用 %s 重启并继续" % (prev, cur), "body": ev.get("body") or "",
                    "ts": ev.get("ts")}
        if KB.mode_of(self.cfg) == "off" or cur not in KB.pool(self.cfg):
            return None
        self.key_exhausted[cur] = time.time()
        self.state_dirty.set()
        t = time.time()
        recent = [x for x in self.key_swaps.get((m.id, sid), []) if t - x < KB.SWAP_WINDOW]
        if len(recent) >= KB.SWAP_LIMIT:
            return dict(ev, title="%s 不可用，已自动换过 %d 次，请手动处理" % (cur, len(recent)))
        new = self.best_key(avoid=(cur,), any_window=True)    # the replacement must work now, 5 h window included
        if not new:
            return dict(ev, title="%s 不可用，没有其他可用的 Key" % cur)
        self.key_swaps[(m.id, sid)] = recent + [t]
        hot = await self.switch_key(m, sid, s.get("kind"), new, cold=True)
        if hot is None:
            return dict(ev, title="%s 不可用，切换到 %s 失败" % (cur, new))
        asyncio.ensure_future(self.continue_after_swap(m, sid, hot))
        return {"kind": "keyswap", "title": "%s 不可用，已切换到 %s 并继续" % (cur, new), "body": ev.get("body") or "",
                "ts": ev.get("ts")}

    async def switch_key(self, m, sid, kind, name, cold=False):
        """Give the session another key. True = swapped live, False = restarted + resumed, None = failed.
        cold: always restart (after a key that failed)."""
        old = (m.sessions.get(sid) or {}).get("key")
        try:
            r = await m.call({"op": "restart", "sid": sid, "env": self.env_for(kind, name), "key": name, "resume": True,
                              "cold": cold}, timeout=30)
        except (ConnectionError, asyncio.TimeoutError, ValueError):
            return None
        if r.get("t") != "restarted":
            return None
        self.note_swap(m, sid, old, bool(r.get("hot")))
        return bool(r.get("hot"))

    def note_swap(self, m, sid, old, hot):
        # a live swap reaches claude only with the message after next; until one goes through, a key failure
        # is the old key's (balance_on_event)
        if hot:
            self.hot_swapped[(m.id, sid)] = old
        else:
            self.hot_swapped.pop((m.id, sid), None)

    async def continue_after_swap(self, m, sid, hot):
        # a restart needs the TUI ready; a live swap waits until claude re-read the key file
        await asyncio.sleep(5 if hot else 3)
        for _ in range(40):
            s = m.sessions.get(sid) or {}
            if s.get("status") in ("idle", "error") and not s.get("menu"):
                break
            await asyncio.sleep(1)
        try:
            m.write({"op": "text", "sid": sid, "text": "继续"})
        except ConnectionError:
            pass

    async def balance_on_done(self, m, sid):
        """least_used, between turns: move a session off a nearly spent key (only when it can be done live)."""
        s = m.sessions.get(sid) or {}
        cur = s.get("key")
        if KB.mode_of(self.cfg) != "least_used" or not s.get("hot_key") or not cur or cur not in KB.pool(self.cfg):
            return
        if time.time() - self.key_moved.get((m.id, sid), 0) < KB.REBALANCE_GAP:
            return
        self.key_exhausted = KB.live_exhausted(self.key_exhausted, self.key_usage)
        new = KB.should_rebalance(cur, KB.pool(self.cfg), self.key_usage, self.key_exhausted, self.keys_in_use(),
                                  rates=self.key_rates.rate)
        if new and await self.switch_key(m, sid, s.get("kind"), new):
            self.key_moved[(m.id, sid)] = time.time()
            log.info("%s/%s: key %s -> %s (balancing)", m.id, sid, cur, new)

    async def fetch_key_usage(self, value):
        """One key's quota + today's usage from keys.usage_url (a key-usage API like funcloud's:
        POST <url>/limit and <url>/summary with the key as Bearer). Returns a small dict or {"error"}."""
        base = str(self.cfg["keys"].get("usage_url") or "").rstrip("/")
        hdr = {"Authorization": "Bearer " + value, "Content-Type": "application/json"}
        out = {"ts": time.time()}
        for ep in ("limit", "summary"):
            try:
                async with self.http.post(base + "/" + ep, json={"data": {}}, headers=hdr,
                                          timeout=aiohttp.ClientTimeout(total=20)) as r:
                    j = await r.json(content_type=None)
                if not isinstance(j, dict) or j.get("code") != 200:
                    msg = (j.get("code_reason") or j.get("code_msg")) if isinstance(j, dict) else None
                    msg = "Key 无效或已停用" if str(msg).strip().lower() == "no response" else msg
                    return {"ts": time.time(), "error": str(msg or "查询失败")[:80]}
                d = j.get("resp_data") or {}
                if ep == "limit":
                    out["windows"] = [{"limit": w.get("limit"), "used": round(float(w.get("used") or 0), 2),
                                       "percent": round(float(w.get("percent") or 0), 1),
                                       "window_sec": w.get("window_sec"),
                                       "reset_at": (w.get("next_reset_at") or 0) / 1000.0}
                                      for w in d.get("windows") or [] if w.get("enabled", True)]
                else:
                    out["today_tokens"] = int(d.get("today_tokens") or 0)
                    out["today_cost"] = round(float(d.get("today_cost") or 0), 2)
                    out["today_requests"] = int(d.get("today_req_cnt") or 0)
                    out["total_cost"] = round(float(d.get("total_cost") or 0), 2)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as e:
                return {"ts": time.time(), "error": "查询失败：%s" % (str(e) or type(e).__name__)[:80]}
        return out

    async def code_watch_loop(self):
        """The agent source changed on disk (git pull): machines running the old code are flagged 需要更新
        within seconds, idle ones are updated right away, without restarting the hub."""
        while True:
            await asyncio.sleep(CODE_CHECK_EVERY)
            code = M.agent_code()
            if any([M.check_code(m, code) for m in self.machines.values()]):
                self.state_dirty.set()

    async def config_watch_loop(self):
        """config.json edited by hand: reload the keys (names, values, usage_url) and memory settings without a
        restart. Machines / listen addresses still need a restart. A file that doesn't parse is ignored."""
        last = self.config_mtime()
        while True:
            await asyncio.sleep(2)
            mt = self.config_mtime()
            if mt == last:
                continue
            last = mt
            try:
                new = C.load()
            except (OSError, ValueError) as e:
                log.warning("config.json not reloaded: %s", e)
                continue
            keys_changed = new["keys"] != self.cfg["keys"]
            mem_changed = new.get("memory") != self.cfg.get("memory")
            if not keys_changed and not mem_changed:
                continue
            self.cfg["keys"], self.cfg["memory"] = new["keys"], new.get("memory")
            log.info("config.json reloaded (%s)", ", ".join(n for n, c in (("keys", keys_changed), ("memory", mem_changed)) if c))
            if keys_changed:
                names = {x.get("name") for x in self.cfg["keys"].get("profiles") or []}
                self.key_usage = {k: v for k, v in self.key_usage.items() if k in names}
                if self.cfg["keys"].get("usage_url") and not any(getattr(t, "_kul", False) for t in self.bg):
                    t = asyncio.ensure_future(self.key_usage_loop())
                    t._kul = True
                    self.bg.append(t)
                self.key_usage_wake.set()
            if mem_changed:
                for m in self.machines.values():
                    if m.online:
                        asyncio.ensure_future(self.memory_config(m))
            self.state_dirty.set()

    @staticmethod
    def config_mtime():
        try:
            return os.stat(C.CONFIG).st_mtime_ns
        except OSError:
            return None

    async def key_usage_loop(self):
        """Refresh every key's usage every 5 minutes (or right away when a phone asks)."""
        while True:
            for p in list(self.cfg["keys"].get("profiles") or []):
                if p.get("name") and p.get("value"):
                    self.key_usage[p["name"]] = await self.fetch_key_usage(p["value"])
                    self.key_rates.observe(p["name"], self.key_usage[p["name"]])
                    self.state_dirty.set()
            self.key_usage_wake.clear()
            try:
                await asyncio.wait_for(self.key_usage_wake.wait(), 300)
            except asyncio.TimeoutError:
                pass

    def env_for(self, kind, key_name):
        if kind not in ("claude", "codex") or not key_name:
            return None
        p = C.profile(self.cfg, key_name)
        if not p:
            raise ValueError("没有名为 %s 的 Key" % key_name)
        return C.key_env(self.cfg, kind, p.get("value"))

    # --- machine side
    def options(self):
        """Choices for model / effort / mode pickers: DEFAULTS["options"] overridden per list by config.json."""
        out = copy.deepcopy(C.DEFAULTS["options"])
        for kind, lists in (self.cfg.get("options") or {}).items():
            if isinstance(lists, dict):
                legacy = C.LEGACY_OPTIONS.get(kind) or {}
                out.setdefault(kind, {}).update({k: v for k, v in lists.items()
                                                 if isinstance(v, list) and v not in legacy.get(k, [])})
        return out

    def state(self):
        return {"t": "state", "hub": {"version": VERSION}, "keys": self.keys_view(), "balance": self.balance_view(),
                "options": self.options(),
                "machines": [self.machines[k].info(self.presets_view(self.machines[k])) for k in self.order]}

    def on_machine_state(self, m):
        self.state_dirty.set()

    async def state_loop(self):
        while True:
            await self.state_dirty.wait()
            await asyncio.sleep(0.15)
            self.state_dirty.clear()
            st = self.state()
            for p in list(self.phones):
                p.send(st)

    async def on_machine_msg(self, m, msg):
        t = msg.get("t")
        if t == "resync":
            # new daemon connection: restore daemon-side subscriptions, phones re-request replays
            for (mid, sid), n in self.sub_count.items():
                if mid == m.id and n > 0:
                    m.write({"op": "sub", "sid": sid})
            for p in list(self.phones):
                p.send({"t": "resync", "m": m.id})
            asyncio.ensure_future(self.memory_config(m))
            return
        if t in ("out", "chat"):
            key = (m.id, msg.get("sid"))
            out = dict(msg, m=m.id)
            for p in list(self.phones):
                if key in p.subs:
                    p.send(out)
        elif t == "event":
            await self.on_event(m, msg["sid"], msg["ev"])
        elif t in ("replay", "error", "restarted", "ok", "history", "fs"):
            r = self.reply_to.pop((m.id, msg.get("req")), None)
            if r is None:
                return               # unsolicited errors are not broadcast to everyone
            p, info = r
            if t == "ok":
                # confirmations the phone waits for; other ops ("in", "resize", ...) stay silent
                name = {"approve": "approved", "control": "control_ok", "delete": "delete_ok",
                        "menu": "menu_ok"}.get(info.get("op"))
                if name:
                    p.send({"t": name, "m": m.id, "sid": info.get("sid")})
                return
            if t == "history":
                p.send({"t": "history", "m": m.id, "req": msg.get("req"), "kind": info.get("kind"),
                        "items": msg.get("items") or []})
                return
            if info.get("op") == "fs" and t == "error":
                # an agent without file browsing (older) or a session that is gone: shown in the files view
                p.send({"t": "fs", "m": m.id, "sid": info.get("sid"), "action": info.get("action"), "path": info.get("path"),
                        "error": agent_error(msg, "读取失败")})
                return
            out = dict(msg, m=m.id)
            if t == "error" and not out.get("sid") and info.get("sid"):
                out["sid"] = info["sid"]     # the agent may not echo it; the phone routes errors by session
            p.send(out)

    async def on_event(self, m, sid, ev):
        """Runs inside the machine's read loop: must never wait for that machine's replies (a key swap waits
        for the agent's "restarted", which arrives through this same loop), so the swap runs as a task."""
        if ev.get("kind") == "error" and self.balance_applies(m, sid, ev):
            asyncio.ensure_future(self.balance_then_emit(m, sid, ev))
            return
        if ev.get("kind") == "done":
            self.hot_swapped.pop((m.id, sid), None)          # a message went through on the swapped key
            asyncio.ensure_future(self.balance_on_done(m, sid))
        await self.emit(m, sid, ev)

    async def balance_then_emit(self, m, sid, ev):
        out = None
        try:
            out = await self.balance_on_event(m, sid, ev)
        except Exception:
            log.exception("%s/%s: key switch failed", m.id, sid)
        await self.emit(m, sid, out or ev)

    async def emit(self, m, sid, ev):
        """An event to every phone (banner) and as a push to devices that don't have the app on screen."""
        s = m.sessions.get(sid) or {}
        name = s.get("name") or sid
        title = "%s · %s" % (name, ev.get("title", ""))
        body = ev.get("body") or ""
        k = (m.id, sid, ev.get("kind"), body)
        t = time.time()
        if t - self.recent_push.get(k, 0) < 5:
            return
        self.recent_push = {kk: v for kk, v in self.recent_push.items() if t - v < 60}
        self.recent_push[k] = t
        for p in list(self.phones):
            p.send({"t": "event", "m": m.id, "sid": sid, "ev": ev, "title": title})
        # a device that has the app open on screen gets the in-app banner instead of a push; every other device
        # is still notified (an open desktop window must not silence the phone)
        on = {p.device for p in self.phones if p.device and p.on_screen()}
        sent = await self.push_all({"title": title, "body": body[:300] or ev.get("title", ""),
                                    "subtitle": "%s %s" % (m.id, m.name),
                                    "tag": "%s:%s" % (m.id, sid), "url": "/#%s/%s" % (m.id, sid),
                                    "kind": ev.get("kind")}, skip=on)
        log.info("event %s %s/%s: push to %d device(s), %d on screen", ev.get("kind"), m.id, sid, sent, len(on))

    # --- phone side
    def _sub(self, m, sid):
        k = (m.id, sid)
        self.sub_count[k] = self.sub_count.get(k, 0) + 1
        if self.sub_count[k] == 1:
            m.write({"op": "sub", "sid": sid})

    def _unsub(self, m, sid):
        k = (m.id, sid)
        n = self.sub_count.get(k, 0) - 1
        if n <= 0:
            self.sub_count.pop(k, None)
            try:
                m.write({"op": "unsub", "sid": sid})
            except ConnectionError:
                pass
        else:
            self.sub_count[k] = n

    def forward(self, p, m, obj):
        """Send an op to a machine; its error/reply comes back only to this phone."""
        req = m.next_req()
        self.reply_to[(m.id, req)] = (p, {"op": obj.get("op"), "sid": obj.get("sid"), "kind": obj.get("kind"),
                                          "action": obj.get("action"), "path": obj.get("path")})
        if len(self.reply_to) > 5000:
            for k in list(self.reply_to)[:1000]:
                del self.reply_to[k]
        m.write(dict(obj, req=req))

    async def phone_op(self, p, msg):
        op = msg.get("op")
        if op == "ping":
            p.send({"t": "pong", "ts": msg.get("ts")})
            return
        if op == "key_usage":
            self.key_usage_wake.set()         # refresh now; the result arrives with the next state
            return
        if op == "key_balance":
            mode, ex = msg.get("mode"), msg.get("exclude")
            names = {x.get("name") for x in self.cfg["keys"].get("profiles") or []}
            if mode not in KB.MODES or not isinstance(ex, list) or any(not isinstance(x, str) or x not in names for x in ex):
                p.send({"t": "error", "op": op, "msg": "无效的均衡设置"})
                return
            self.cfg["keys"]["balance"] = {"mode": mode, "exclude": sorted(set(ex))}
            C.save(self.cfg)
            self.state_dirty.set()
            p.send({"t": "key_balance_ok"})
            return
        if op == "visible":
            p.visible = bool(msg.get("v"))
            return
        if op in ("machine_test", "machine_add", "machine_update", "machine_remove"):
            try:
                await self.machine_op(p, op, msg)
            except ValueError as e:
                p.send({"t": "error", "op": op, "msg": str(e)})
            return
        m = self.machines.get(msg.get("m")) if isinstance(msg.get("m"), str) else None
        if m is None:
            p.send({"t": "error", "msg": "未知机器 %s" % msg.get("m")})
            return
        sid = msg.get("sid")
        if sid is not None and not isinstance(sid, str):
            return
        try:
            if op == "sub":
                k = (m.id, sid)
                if k not in p.subs:
                    p.subs.add(k)
                    self._sub(m, sid)
                self.forward(p, m, {"op": "replay", "sid": sid, "off": msg.get("off"),
                                    "chat_from": msg.get("chat_from")})
            elif op == "unsub":
                if (m.id, sid) in p.subs:
                    p.subs.discard((m.id, sid))
                    self._unsub(m, sid)
            elif op in ("in", "text", "resize", "kill", "forget", "ack", "rename"):
                fwd = {k: v for k, v in msg.items() if k in ("op", "sid", "d", "text", "cols", "rows", "name")}
                self.forward(p, m, fwd)
            elif op == "auto_continue":
                self.forward(p, m, {"op": "auto_continue", "sid": sid, "on": bool(msg.get("on"))})
            elif op == "approve":
                self.forward(p, m, {"op": "approve", "sid": sid, "id": msg.get("id"),
                                    "decision": msg.get("decision"), "message": msg.get("message")})
            elif op == "history":
                kind = msg.get("kind") if msg.get("kind") in ("claude", "codex") else None
                self.forward(p, m, {"op": "history", "kind": kind, "limit": 30})
            elif op == "fs":
                # files of the session's directory (browse, edit, new file / folder); the agent confines every
                # request to that directory and refuses Glimpse's own data and .git
                action, path = msg.get("action"), msg.get("path") or ""
                if not sid or action not in FS_ACTIONS or not isinstance(path, str) or len(path) > 4096:
                    raise ValueError("无效的文件请求")
                out = {"op": "fs", "sid": sid, "action": action, "path": path, "force": bool(msg.get("force"))}
                if action in ("write", "create"):
                    text, sig = msg.get("text"), msg.get("sig")
                    if not isinstance(text, str) or len(text) > FS_TEXT_LIMIT or (action == "write" and not
                                                                                   (isinstance(sig, str) and re.fullmatch(r"[0-9a-f]{16}", sig))):
                        raise ValueError("无效的文件内容")
                    out.update(text=text, sig=sig if action == "write" else None)
                self.forward(p, m, out)
            elif op == "control":
                action, value = msg.get("action"), msg.get("value")
                if action not in CONTROL_ACTIONS or not isinstance(value, str) or len(value) > 500:
                    raise ValueError("无效的操作")
                if action != "goal" and value and not ARG_RE.match(value):
                    raise ValueError("无效的值: %s" % value[:40])
                self.forward(p, m, {"op": "control", "sid": sid, "action": action, "value": value})
            elif op == "menu":
                index, key = msg.get("index"), msg.get("key")
                if not sid:
                    raise ValueError("会话不存在")
                if index is not None:
                    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 30:
                        raise ValueError("无效的选项")
                    self.forward(p, m, {"op": "menu", "sid": sid, "index": index})
                elif key in MENU_KEYS:
                    self.forward(p, m, {"op": "menu", "sid": sid, "key": key})
                else:
                    raise ValueError("无效的按键")
            elif op == "models":
                await self.models(p, m, msg)
            elif op == "memory":
                await self.memory(p, m, msg)
            elif op == "netinfo":
                asyncio.ensure_future(self.machine_netinfo(m, bool(msg.get("refresh"))))
            elif op == "speedtest":
                asyncio.ensure_future(self.machine_speedtest(p, m))
            elif op == "agent_update":
                # restart the machine's agent daemon on the current code; its sessions are restarted and resumed
                log.info("%s: agent update requested", m.id)
                m.update_agent()
                p.send({"t": "agent_update_ok", "m": m.id})
            elif op == "delete":
                self.forward(p, m, {"op": "delete", "sid": sid})
            elif op == "clean_exited":
                for xs in list(m.sessions.values()):
                    if xs.get("status") in ("exited", "error"):
                        self.forward(p, m, {"op": "forget", "sid": xs["sid"]})
            elif op == "create":
                await self.create(p, m, msg)
            elif op == "restart":
                await self.restart(p, m, msg)
            elif op == "reconnect":
                m.reconnect_now()
            else:
                p.send({"t": "error", "msg": "unknown op %s" % op})
        except (ConnectionError, asyncio.TimeoutError) as e:
            p.send({"t": "error", "m": m.id, "sid": sid, "msg": "%s: %s" % (m.name, str(e) or "超时")})
        except ValueError as e:
            p.send({"t": "error", "m": m.id, "sid": sid, "msg": str(e)})

    def memory_cfg(self, m):
        """hub-wide memory settings, overridden per machine by machines[].memory."""
        cfg = dict(self.cfg.get("memory") or {})
        cfg.update(m.cfg.get("memory") or {})
        return cfg

    async def memory_config(self, m):
        try:
            r = await m.call({"op": "memory_config", "cfg": self.memory_cfg(m)}, timeout=90)
        except (ConnectionError, asyncio.TimeoutError):
            return
        if r.get("t") == "memory_status":
            m.memory = {k: v for k, v in r.items() if k not in ("t", "req", "store")}
            self.state_dirty.set()

    async def memory(self, p, m, msg):
        action = msg.get("action")
        if action not in MEMORY_ACTIONS:
            raise ValueError("无效的记忆操作")
        q = {k: msg.get(k) for k in ("project", "q", "budget", "dry_run") if msg.get(k) is not None}
        r = await m.call(dict(q, op="memory", action=action), timeout=900 if action == "compact" else 90)
        if r.get("t") != "memory":
            raise ValueError(r.get("msg") or "记忆查询失败")
        st = r.get("status") or {}
        m.memory = {k: v for k, v in st.items() if k != "store"}
        p.send({"t": "memory", "m": m.id, "action": action, "project": msg.get("project"), "data": r.get("data")})
        if action in ("budget", "compact"):
            self.state_dirty.set()

    async def machine_netinfo(self, m, refresh=False):
        """Public IP / location of a machine (the agent caches it 10 minutes); shown by the network widget."""
        try:
            r = await m.call({"op": "netinfo", "refresh": refresh}, timeout=20)
        except (ConnectionError, asyncio.TimeoutError):
            return
        if r.get("t") == "netinfo":
            m.netinfo = {k: r.get(k) for k in ("ip", "city", "region", "country", "org", "timezone", "ts")}
        else:
            m.netinfo = {"error": agent_error(r, "查询失败"), "ts": time.time()}
        self.state_dirty.set()

    async def machine_speedtest(self, p, m):
        """Download speed of a machine's internet connection (10 MB from a public speed-test server).
        A second request while one runs joins it instead of failing with "already running"."""
        if m.speed_task is None or m.speed_task.done():
            m.speed_task = asyncio.ensure_future(self._speedtest(m))
        try:
            await asyncio.shield(m.speed_task)
        except Exception:
            pass
        p.send({"t": "speedtest_done", "m": m.id, "speed": m.speed})

    async def _speedtest(self, m):
        try:
            r = await m.call({"op": "speedtest"}, timeout=40)
        except ConnectionError:
            r = {"t": "error", "msg": "机器离线"}
        except asyncio.TimeoutError:
            r = {"t": "error", "msg": "测速超时"}
        if r.get("t") == "speedtest":
            m.speed = {k: r.get(k) for k in ("down_bps", "ttfb_ms", "bytes", "secs")}
        else:
            m.speed = {"error": agent_error(r, "测速失败")}
        m.speed["ts"] = time.time()
        self.state_dirty.set()

    async def create(self, p, m, msg):
        kind = msg.get("kind") if msg.get("kind") in ("claude", "codex", "shell") else "claude"
        preset = self.preset(m, kind)
        base = list(preset.get("argv") or ([] if kind == "shell" else [kind]))
        argv = build_argv(kind, base, msg)
        resume_id = msg.get("resume_id")
        if resume_id is not None and (kind == "shell" or not isinstance(resume_id, str) or not RESUME_RE.match(resume_id)):
            raise ValueError("无效的会话 id")
        key = msg.get("key") or preset.get("key")
        if kind != "shell" and not msg.get("key") and KB.mode_of(self.cfg) == "least_used":
            key = self.best_key() or key       # "默认" = let the balancer choose
        env = self.env_for(kind, key) if key else None
        r = await m.call({"op": "create", "kind": kind, "cwd": msg.get("cwd") or m.cfg.get("default_cwd"),
                          "name": msg.get("name") or None, "args": argv or None, "env": env,
                          "key": key if env else None, "cols": msg.get("cols") or 80, "rows": msg.get("rows") or 30,
                          "resume_id": resume_id or None, "init_cmds": init_cmds(kind, msg)})
        if r.get("t") == "created":
            out = {"t": "created", "m": m.id, "s": r["s"]}
            if r.get("existing"):
                out["existing"] = True       # the conversation already had a session: the phone just opens it
            p.send(out)
        else:
            p.send({"t": "error", "m": m.id, "msg": r.get("msg") or "创建失败"})

    async def models(self, p, m, msg):
        """Real model / effort / mode lists from the agent (codex asks its CLI), cached per (machine, kind)."""
        kind = msg.get("kind")
        if kind not in ("claude", "codex"):
            raise ValueError("无效的类型")
        k = (m.id, kind)
        hit = self.models_cache.get(k)
        if hit is None or time.time() - hit[0] > MODELS_TTL:
            fut = self.models_pending.get(k)
            if fut is None:
                # one agent call per (machine, kind) even when several phones open the sheet at once
                argv = self.preset(m, kind).get("argv") or [kind]
                fut = asyncio.ensure_future(m.call({"op": "models", "kind": kind, "argv": list(argv)}, timeout=45))
                self.models_pending[k] = fut
                fut.add_done_callback(lambda f: self.models_pending.pop(k, None))
            try:
                r = await asyncio.shield(fut)
            except (ConnectionError, asyncio.TimeoutError) as e:
                r = {"t": "error", "msg": "%s: %s" % (m.name, str(e) or "超时")}
            if r.get("t") != "models":
                p.send({"t": "error", "op": "models", "m": m.id, "kind": kind,
                        "msg": r.get("msg") or "获取模型列表失败"})
                return
            hit = (time.time(), {x: r.get(x) or [] for x in ("models", "efforts", "modes")})
            self.models_cache[k] = hit
        p.send(dict(hit[1], t="models", m=m.id, kind=kind))

    async def restart(self, p, m, msg):
        sid = msg.get("sid")
        s = m.sessions.get(sid)
        if not s:
            raise ValueError("会话不存在")
        kind = s.get("kind")
        env, key = None, None
        nk = msg.get("new_key")
        if isinstance(nk, dict) and str(nk.get("value") or "").strip():
            value = str(nk["value"]).strip()
            name = str(nk.get("save_as") or "").strip()
            if name:
                profs = [x for x in self.cfg["keys"].get("profiles") or [] if x.get("name") != name]
                profs.append({"name": name, "value": value})
                self.cfg["keys"]["profiles"] = profs
                C.save(self.cfg)
                self.state_dirty.set()
            key = name or "自定义"
            env = C.key_env(self.cfg, kind, value)
        elif msg.get("key"):
            key = msg["key"]
            env = self.env_for(kind, key)
        # after a failure (usually the key): restart, a live swap would only take effect after the next message
        cold = env is not None and s.get("status") == "error"
        r = await m.call({"op": "restart", "sid": sid, "env": env, "key": key,
                          "resume": bool(msg.get("resume", True)), "cold": cold}, timeout=30)
        if r.get("t") == "restarted":
            if env is not None:
                self.note_swap(m, sid, s.get("key"), bool(r.get("hot")))
            p.send({"t": "restarted", "m": m.id, "sid": sid, "hot": bool(r.get("hot")), "key": key})
        else:
            p.send({"t": "error", "m": m.id, "sid": sid, "msg": r.get("msg") or "重启失败"})

    # --- machines managed from the phone
    async def machine_op(self, p, op, msg):
        if op == "machine_test":
            ok, text = await ssh_test(check_host(msg.get("host")), check_port(msg.get("port")))
            p.send({"t": "machine_test", "ok": ok, "msg": text})
            return
        mcs = self.cfg.setdefault("machines", [])
        if op == "machine_add":
            mc = {"id": next_machine_id(mcs), "name": check_name(msg.get("name")), "type": "ssh",
                  "host": check_host(msg.get("host")), "default_cwd": check_cwd(msg.get("default_cwd"))}
            port = check_port(msg.get("port"))
            if port:
                mc["port"] = port
            local = next((x for x in mcs if x.get("type") == "local"), None)
            if local and local.get("presets"):
                # same launch presets as this computer (argv, default key); editable later in config.json
                mc["presets"] = {k: v for k, v in copy.deepcopy(local["presets"]).items()
                                 if k in ("claude", "codex", "shell")}
            mcs.append(mc)
            C.save(self.cfg)
            self.add_machine(mc).start()
        else:
            mid = msg.get("m")
            mc = next((x for x in mcs if x.get("id") == mid), None) if isinstance(mid, str) else None
            if mc is None:
                raise ValueError("未知机器 %s" % mid)
            m = self.machines.get(mid)
            if op == "machine_update":
                if "name" in msg:
                    mc["name"] = check_name(msg.get("name"))
                if "default_cwd" in msg:
                    mc["default_cwd"] = check_cwd(msg.get("default_cwd"))
                reconnect = False
                if mc.get("type") == "ssh":
                    if "host" in msg:
                        host = check_host(msg.get("host"))
                        reconnect |= host != mc.get("host")
                        mc["host"] = host
                    if "port" in msg:
                        port = check_port(msg.get("port"))
                        reconnect |= port != mc.get("port")
                        if port:
                            mc["port"] = port
                        else:
                            mc.pop("port", None)
                elif "host" in msg or "port" in msg:
                    raise ValueError("本机不能改成 ssh 主机")
                C.save(self.cfg)
                if m:
                    m.name = mc.get("name") or m.id
                    if reconnect:
                        m.reconnect_fresh()
            else:
                if mc.get("type") == "local":
                    raise ValueError("本机不能删除")
                mcs.remove(mc)
                C.save(self.cfg)
                if m:
                    await self.drop_machine(m)
        self.state_dirty.set()
        p.send({"t": "machines_ok", "op": op})

    async def drop_machine(self, m):
        self.machines.pop(m.id, None)
        if m.id in self.order:
            self.order.remove(m.id)
        for k in [k for k in self.sub_count if k[0] == m.id]:
            del self.sub_count[k]
        for k in [k for k in self.reply_to if k[0] == m.id]:
            del self.reply_to[k]
        for k in [k for k in self.models_cache if k[0] == m.id]:
            del self.models_cache[k]
        for ph in list(self.phones):
            ph.subs = {k for k in ph.subs if k[0] != m.id}
        await m.stop()

    def phone_gone(self, p):
        self.phones.discard(p)
        p.close()
        for (mid, sid) in list(p.subs):
            m = self.machines.get(mid)
            if m:
                self._unsub(m, sid)
        p.subs.clear()
        for k, v in list(self.reply_to.items()):
            if v[0] is p:
                del self.reply_to[k]

    # --- http
    @staticmethod
    def client_ip(request):
        return request.remote or ""

    def is_local(self, request):
        return self.client_ip(request) in ("127.0.0.1", "::1")

    def request_token(self, request):
        h = request.headers.get("Authorization", "")
        if h.startswith("Bearer "):
            return h[7:].strip()
        if request.path == "/ws" and request.query.get("t"):
            return request.query["t"]
        return request.cookies.get("glimpse") or ""

    def authed(self, request):
        tok = self.request_token(request)
        if not tok:
            return False
        if self.auth.device_for(tok):
            return True
        # the CLI token in data/token is for this computer only (gli attach); never valid over the network
        return self.is_local(request) and hmac.compare_digest(tok.encode(), self.token.encode())

    def set_cookie(self, resp, tok):
        resp.set_cookie("glimpse", tok, max_age=365 * 86400, httponly=True, secure=True,
                        samesite="Strict", path="/")

    def issue_session(self, request):
        tok, dev = self.auth.new_device(device_name(request), self.client_ip(request))
        resp = web.json_response({"ok": True, "session": tok, "device": dev["id"]})
        self.set_cookie(resp, tok)
        self.auth.ok(self.client_ip(request))
        return resp

    async def login(self, request):
        """{"password"} or {"pair": code} -> a new device session (cookie + returned token)."""
        ip = self.client_ip(request)
        if self.auth.blocked(ip):
            raise web.HTTPTooManyRequests(text="too many attempts, try again later")
        body = await self.json_body(request)
        pair, pw = body.get("pair"), body.get("password")
        if (isinstance(pair, str) and self.auth.use_pair(pair)) or (isinstance(pw, str) and self.auth.check_password(pw)):
            return self.issue_session(request)
        self.auth.failed(ip)
        await asyncio.sleep(1)
        raise web.HTTPUnauthorized(text="bad credentials")
    def static_file(self, name):
        path = os.path.realpath(os.path.join(WEB, name))
        if not path.startswith(os.path.realpath(WEB) + os.sep) or not os.path.isfile(path):
            raise web.HTTPNotFound()
        resp = web.FileResponse(path)
        resp.headers["Cache-Control"] = "no-cache"
        if name.endswith(".webmanifest"):
            resp.content_type = "application/manifest+json"
        if name == "sw.js":
            resp.headers["Service-Worker-Allowed"] = "/"
        return resp

    async def index(self, request):
        resp = self.static_file("index.html")
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self' wss: ws:; frame-ancestors 'none'")
        return resp

    async def static(self, request):
        return self.static_file(request.match_info["path"])

    async def root_file(self, request):
        return self.static_file(request.path.lstrip("/"))

    async def manifest(self, request):
        with open(os.path.join(WEB, "manifest.webmanifest")) as f:
            man = json.load(f)
        # Home Screen apps on iOS do not share Safari's cookies: the start URL carries a fresh one-time
        # pairing code (never a session or the CLI token), and only for an already logged-in device
        if self.auth.device_for(self.request_token(request)):
            man["start_url"] = "/?pair=" + self.auth.new_pair()
        resp = web.json_response(man, content_type="application/manifest+json")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    async def json_body(self, request):
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise web.HTTPBadRequest(text="invalid json")
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="invalid json")
        return body

    async def logout(self, request):
        dev = self.auth.device_for(self.request_token(request))
        if dev:
            self.auth.revoke(dev["id"])
            self.kick_device(dev["id"])
        resp = web.json_response({"ok": True})
        resp.del_cookie("glimpse", path="/")
        return resp

    def kick_device(self, dev_id=None, keep=None):
        """Close the websockets of a revoked device (dev_id None: every device session except keep)."""
        for p in list(self.phones):
            if p.device and p.device != keep and (dev_id is None or p.device == dev_id):
                asyncio.ensure_future(p.ws.close(code=4401, message=b"logged out"))

    async def devices(self, request):
        cur = self.auth.device_for(self.request_token(request))
        return web.json_response({"devices": self.auth.public_devices(cur and cur["id"]),
                                  "has_password": self.auth.has_password()})

    async def device_revoke(self, request):
        body = await self.json_body(request)
        cur = self.auth.device_for(self.request_token(request))
        if body.get("all"):
            keep = cur and cur["id"]
            self.auth.revoke_all(keep=keep)
            self.kick_device(None, keep=keep)
            return web.json_response({"ok": True})
        did = str(body.get("id") or "")
        if not self.auth.revoke(did):
            raise web.HTTPNotFound(text="no such device")
        self.kick_device(did)
        return web.json_response({"ok": True})

    async def change_password(self, request):
        """Set / change the password. Changing an existing one needs the current one."""
        body = await self.json_body(request)
        if self.auth.has_password() and not self.auth.check_password(body.get("current")):
            self.auth.failed(self.client_ip(request))
            raise web.HTTPForbidden(text="当前密码不正确")
        try:
            self.auth.set_password(body.get("new"))
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e))
        return web.json_response({"ok": True})

    @web.middleware
    async def auth_mw(self, request, handler):
        p = request.path
        if (p.startswith("/api/") and p not in ("/api/login", "/api/logout")) or p == "/ws":
            if not self.authed(request):
                raise web.HTTPUnauthorized(text="login required")
        resp = await handler(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

    async def me(self, request):
        dev = self.auth.device_for(self.request_token(request))
        return web.json_response({"ok": True, "version": VERSION, "device": dev and dev["id"],
                                  "has_password": self.auth.has_password()})

    async def vapid_key(self, request):
        return web.json_response({"publicKey": self.vapid.public_b64})

    async def push_subscribe(self, request):
        sub = await self.json_body(request)
        keys = sub.get("keys") if isinstance(sub.get("keys"), dict) else {}
        if not str(sub.get("endpoint", "")).startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
            raise web.HTTPBadRequest(text="invalid subscription")
        sub = {"endpoint": sub["endpoint"], "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}}
        dev = self.auth.device_for(self.request_token(request))
        if dev:
            sub["device"] = dev["id"]          # so this device is only skipped while it has the app on screen
        old = next((s for s in self.push_subs if s["endpoint"] == sub["endpoint"]), None)
        self.push_subs = [s for s in self.push_subs if s["endpoint"] != sub["endpoint"]] + [sub]
        if old != sub:
            self._save_push()
        return web.json_response({"ok": True, "count": len(self.push_subs)})

    async def push_unsubscribe(self, request):
        body = await self.json_body(request)
        self.push_subs = [s for s in self.push_subs if s["endpoint"] != body.get("endpoint")]
        self._save_push()
        return web.json_response({"ok": True})

    async def push_test(self, request):
        await self.push_all({"title": "Glimpse 测试通知", "body": "通知已经可以正常工作", "tag": "test", "url": "/"})
        return web.json_response({"ok": True, "count": len(self.push_subs)})

    async def ws(self, request):
        if not origin_ok(request):
            raise web.HTTPForbidden(text="bad origin")
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=8 * 1024 * 1024, compress=True)
        await ws.prepare(request)
        dev = self.auth.device_for(self.request_token(request))
        p = Phone(ws, dev and dev["id"])
        self.phones.add(p)
        p.send(self.state())
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                p.last_seen = time.time()
                if data.get("op") in ("create", "restart", "models", "machine_test", "machine_add", "machine_update",
                                      "machine_remove"):
                    asyncio.ensure_future(self.phone_op(p, data))
                else:
                    await self.phone_op(p, data)
        finally:
            self.phone_gone(p)
        return ws

    def app(self):
        app = web.Application(middlewares=[self.auth_mw], client_max_size=4 * 1024 * 1024)
        app.router.add_get("/", self.index)
        app.router.add_get("/sw.js", self.root_file)
        app.router.add_get("/manifest.webmanifest", self.manifest)
        app.router.add_get("/static/{path:.+}", self.static)
        app.router.add_post("/api/login", self.login)
        app.router.add_post("/api/logout", self.logout)
        app.router.add_get("/api/me", self.me)
        app.router.add_get("/api/devices", self.devices)
        app.router.add_post("/api/devices/revoke", self.device_revoke)
        app.router.add_post("/api/password", self.change_password)
        app.router.add_get("/api/vapid", self.vapid_key)
        app.router.add_post("/api/push/subscribe", self.push_subscribe)
        app.router.add_post("/api/push/unsubscribe", self.push_unsubscribe)
        app.router.add_post("/api/push/test", self.push_test)
        app.router.add_get("/ws", self.ws)
        app.on_startup.append(self.startup)
        app.on_cleanup.append(self.cleanup)
        return app

    async def startup(self, app):
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        self.bg = [asyncio.ensure_future(self.state_loop()), asyncio.ensure_future(self.config_watch_loop()),
                   asyncio.ensure_future(self.code_watch_loop())]
        if self.cfg["keys"].get("usage_url"):
            t = asyncio.ensure_future(self.key_usage_loop())
            t._kul = True
            self.bg.append(t)
        for m in self.machines.values():
            m.start()

    async def cleanup(self, app):
        for t in self.bg:
            t.cancel()
        for m in self.machines.values():
            await m.stop()
        await self.http.close()


def cert_paths():
    return os.path.join(DATA, "certs", "ts.crt"), os.path.join(DATA, "certs", "ts.key")


def ssl_context(paths=None):
    cert, key = paths or cert_paths()
    if not (os.path.isfile(cert) and os.path.isfile(key)):
        return None
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert, key)
    return ctx


def local_cert_paths():
    """`gli local-cert`: a certificate for 127.0.0.1 (the Tailscale one only names the .ts.net host)."""
    return os.path.join(DATA, "certs", "local.crt"), os.path.join(DATA, "certs", "local.key")


def is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def site_context(host, ctx, local):
    """TLS context for one listen address: the 127.0.0.1 certificate on loopback when there is one (only
    together with TLS elsewhere, so every address keeps the same scheme)."""
    return local if ctx is not None and local is not None and is_loopback(host) else ctx


def renew_cert(cfg):
    """Get / refresh the Tailscale (Let's Encrypt) certificate; tailscale only re-issues near expiry."""
    host = resolve_hub(cfg)[1]
    if not host:
        return False
    cert, key = cert_paths()
    os.makedirs(os.path.dirname(cert), mode=0o700, exist_ok=True)
    try:
        subprocess.run(["tailscale", "cert", "--cert-file", cert, "--key-file", key, host],
                       check=True, capture_output=True, timeout=120)
        os.chmod(key, 0o600)
        return True
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("tailscale cert failed: %s", getattr(e, "stderr", b"") or e)
        return False


def detect_tailscale():
    """(ip, MagicDNS name) of this machine on the tailnet, or (None, None)."""
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, timeout=10, check=True).stdout
        me = json.loads(out)["Self"]
        return (me.get("TailscaleIPs") or [None])[0], (me.get("DNSName") or "").rstrip(".") or None
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return None, None


def resolve_hub(cfg):
    """hub.listen hosts / tailscale_hostname set to "auto" (or listen empty) -> ask tailscale. Cached per process."""
    if RUNTIME:
        return RUNTIME["listen"], RUNTIME["hostname"]
    h = cfg["hub"]
    listen = [dict(x) for x in h.get("listen") or []] or [{"host": "auto", "port": 8443},
                                                          {"host": "127.0.0.1", "port": 8443}]
    name = h.get("tailscale_hostname") or ""
    ip = dns = None
    if name == "auto" or any(x.get("host") in ("auto", "", None) for x in listen):
        ip, dns = detect_tailscale()
    out = []
    for x in listen:
        if x.get("host") in ("auto", "", None):
            if not ip:
                log.warning("hub.listen host \"auto\": tailscale IP not found, skipping")
                continue
            x["host"] = ip
        if x not in out:
            out.append(x)
    if name == "auto":
        name = dns or ""
    RUNTIME.update(listen=out, hostname=name)
    return out, name


def login_url(cfg, mask=False):
    """One-time pairing link for a new device (valid PAIR_TTL seconds, single use). Needs the running hub's
    data/auth.json: the code is added to it, and the hub reloads the file when the link is used."""
    base = hub_base_url(cfg)
    if mask:
        return base
    a = Auth(os.path.join(DATA, "auth.json"), C.write_secret)
    return base + "?pair=" + a.new_pair()


def hub_base_url(cfg):
    listen, name = resolve_hub(cfg)
    first = listen[0] if listen else {"host": "127.0.0.1", "port": 8443}
    return "https://%s:%s/" % (name or first["host"], first["port"])


def new_session_url(cfg, path, base=None):
    """`gli new PATH` / the Finder Quick Action: a link that opens the web app's new-session sheet with this
    computer and PATH filled in (a file: its folder). It only fills the form; nothing starts until 创建.
    base: the hub address to use (default: the one `gli url` prints)."""
    from urllib.parse import quote
    p = os.path.abspath(os.path.expanduser(path or "."))
    if not os.path.exists(p):
        raise ValueError("no such file or folder: " + p)
    if not os.path.isdir(p):
        p = os.path.dirname(p)
    q = "cwd=" + quote(p, safe="")
    mid = local_machine_id(cfg)
    if mid:
        q += "&m=" + quote(mid, safe="")
    return (base or hub_base_url(cfg)) + "#new?" + q


def local_machine_id(cfg):
    m = next((m for m in cfg.get("machines") or [] if m.get("type") == "local" and m.get("id")), None)
    return str(m["id"]) if m else ""


async def serve(cfg, hub):
    runner = web.AppRunner(hub.app(), access_log=None)
    await runner.setup()
    ctx = ssl_context()
    if ctx is None:
        log.warning("no TLS certificate: serving plain HTTP (iPhone push notifications need HTTPS)")
    try:
        local = ssl_context(local_cert_paths())
    except (OSError, ssl.SSLError) as e:
        log.warning("127.0.0.1 certificate unusable (%s): run gli local-cert again", e)
        local = None
    sites = 0
    for ls in resolve_hub(cfg)[0]:
        sctx = site_context(ls["host"], ctx, local)
        try:
            await web.TCPSite(runner, ls["host"], ls["port"], ssl_context=sctx).start()
            sites += 1
            log.info("listening on %s://%s:%s%s", "https" if sctx else "http", ls["host"], ls["port"],
                     " (127.0.0.1 certificate)" if sctx is local and local is not None else "")
        except OSError as e:
            log.error("cannot listen on %s:%s: %s", ls["host"], ls["port"], e)
    if not sites:
        raise SystemExit("no listen address available")
    log.info("open on the phone: %s", login_url(cfg, mask=True))
    caf = None
    if cfg["hub"].get("caffeinate", True) and sys.platform == "darwin":
        try:
            caf = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        except OSError:
            pass
    try:
        while True:
            await asyncio.sleep(CERT_RENEW if ctx else CERT_RETRY)
            if not await asyncio.get_running_loop().run_in_executor(None, renew_cert, cfg):
                continue
            if ctx:
                ctx.load_cert_chain(*cert_paths())
            elif ssl_context():
                # the listening sockets are plain HTTP: let launchd / the user restart us with TLS
                log.warning("certificate obtained, restarting")
                raise SystemExit(0)
    finally:
        if caf:
            caf.terminate()
        await runner.cleanup()


def main():
    ap = argparse.ArgumentParser(description="Glimpse hub")
    ap.add_argument("--print-url", action="store_true", help="print a one-time pairing link for a new device")
    ap.add_argument("--set-password", action="store_true", help="set / change the login password")
    ap.add_argument("--devices", action="store_true", help="list logged-in devices")
    ap.add_argument("--revoke", metavar="ID", help="log out a device (ID from --devices, or 'all')")
    ap.add_argument("--new-url", metavar="PATH", help="print a link that opens the new-session form for PATH")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = C.load()
    if a.print_url:
        print(login_url(cfg))
        print("(one-time link, valid %d minutes; the device stays logged in afterwards)" % (PAIR_TTL // 60),
              file=sys.stderr)
        return
    if a.new_url is not None:
        try:
            print(new_session_url(cfg, a.new_url))
        except ValueError as e:
            raise SystemExit("gli new: %s" % e)
        return
    auth = Auth(os.path.join(DATA, "auth.json"), C.write_secret)
    if a.set_password:
        import getpass
        pw = getpass.getpass("new password (8+ characters): ")
        if pw != getpass.getpass("again: "):
            raise SystemExit("passwords differ")
        try:
            auth.set_password(pw)
        except ValueError as e:
            raise SystemExit(str(e))
        print("password set")
        return
    if a.devices:
        devs = auth.public_devices()
        for d in devs:
            print("%s  %-24s last seen %s  %s" % (d["id"], d["name"], time.strftime("%m-%d %H:%M",
                  time.localtime(d["last_seen"])), d["ip"]))
        print("%d device(s)%s" % (len(devs), "" if auth.has_password() else "; no password set (gli password)"))
        return
    if a.revoke:
        if a.revoke == "all":
            auth.revoke_all()
            print("all devices logged out")
        elif not auth.revoke(a.revoke):
            raise SystemExit("no such device: " + a.revoke)
        else:
            print("logged out " + a.revoke)
        return
    os.environ["GLIMPSE_APPROVAL_WAIT"] = str(int(cfg["hub"].get("approval_wait_seconds") or 600))
    renew_cert(cfg)

    async def run():
        await serve(cfg, Hub(cfg))
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
