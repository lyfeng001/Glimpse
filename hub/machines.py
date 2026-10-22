"""Connections from the hub to the Glimpse agent on each machine (local subprocess or ssh)."""
import asyncio
import base64
import json
import logging
import os
import sys
import time
import zlib

log = logging.getLogger("glimpse.machines")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = os.path.join(ROOT, "agent", "glimpse_agent.py")
# The agent source is sent zlib+base64 on the first stdin line and exec'd in memory:
# nothing is installed on the remote machine. `or b"\n"` stops the read loop on EOF.
# The bridge gets the source's code id (it has no file to hash) and, for an update, --replace.
REMOTE_CMD = ("python3 -u -c 'import os,sys,zlib,base64;sys.argv=[\"glimpse\",\"bridge\"%s];"
              "exec(compile(zlib.decompress(base64.b64decode(b\"\".join(iter(lambda:os.read(0,1) or b\"\\n\",b\"\\n\")))),"
              "\"glimpse_agent\",\"exec\"))'")


def agent_source():
    """(source bytes, code id) of the agent as it is on disk now; the same hash the agent computes."""
    import hashlib
    with open(AGENT, "rb") as f:
        src = f.read()
    return src, hashlib.sha256(src).hexdigest()[:12]


_code_cache = [None, None]


def agent_code():
    """Code id of the agent on disk (hashed again only when the file changed)."""
    try:
        st = os.stat(AGENT)
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    if _code_cache[0] != key:
        _code_cache[:] = [key, agent_source()[1]]
    return _code_cache[1]


def live_sessions(m):
    return [s for s in m.sessions.values() if s.get("pid") and s.get("exit_code") is None and s.get("status") != "exited"]


def check_code(m, code):
    """After the agent on disk changed (git pull): flag the machine outdated, and reconnect one without running
    sessions so its bridge replaces the daemon right away (once per code id). Returns True when the flag changed."""
    if not m.online or not code or m.code is None and not m.outdated:
        return False
    out = m.code != code
    changed = out != m.outdated
    m.outdated = out
    if out and not live_sessions(m) and m.refreshed_for != code:
        m.refreshed_for = code
        log.info("%s: agent code changed, reconnecting to update the idle daemon", m.id)
        m.reconnect_fresh()
    return changed
SSH_OPTS = ["-T", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            "-o", "ConnectTimeout=15"]
STATS_EVERY = 5.0
USAGE_EVERY = 12                  # stats ticks between usage_today scans (~60 s)


def dumps(obj):
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8", "replace")


class Machine:
    def __init__(self, cfg, on_msg, on_state):
        self.id = cfg["id"]
        self.name = cfg.get("name") or self.id
        self.cfg = cfg
        self.on_msg = on_msg          # async (machine, msg)
        self.on_state = on_state      # sync (machine)
        self.online = False
        self.error = None
        self.version = None
        self.host = None
        self.stats = None
        self.usage_today = None
        self.sessions = {}
        self.order = []
        self.proc = None
        self.task = None
        self.req = 0
        self.memory = None             # AMem status reported by the agent (None = not configured / old agent)
        self.vault = False             # the vault (vault-linker) is on this machine and configured
        self.ext = {}                  # extension id -> {on, error, name, version} as this machine's agent reports
        self.netinfo = None            # {ip, city, region, country, org, ts} of the machine's internet exit
        self.speed = None              # last speed test {down_bps, ttfb_ms, ts} or {error, ts}
        self.speed_task = None         # the running speed test (requests from several devices share it)
        self.outdated = False          # the daemon runs older agent code (it had live sessions, so it was kept)
        self.code = None               # code id the daemon reported
        self.replace_next = False      # next connection replaces an outdated daemon even with sessions (update)
        self.refreshed_for = None      # agent code id an automatic reconnect was already tried for
        self.waiters = {}
        self.wake = asyncio.Event()

    def info(self, presets_view):
        return {"id": self.id, "name": self.name, "type": self.cfg.get("type"), "online": self.online,
                "error": self.error, "version": self.version, "outdated": self.outdated, "host": self.host, "stats": self.stats,
                "usage_today": self.usage_today, "memory": self.memory, "vault": self.vault, "netinfo": self.netinfo, "speed": self.speed, "ssh_host": self.cfg.get("host"), "port": self.cfg.get("port"),
                "presets": presets_view, "default_cwd": self.cfg.get("default_cwd"),
                "sessions": [self.sessions[k] for k in self.order if k in self.sessions]}

    def argv(self, code="", replace=False):
        extra = ["--replace"] if replace else []
        if self.cfg.get("type") == "ssh":
            port = ["-p", str(self.cfg["port"])] if self.cfg.get("port") else []
            args = "".join(',"%s"' % a for a in (["--code", code] if code else []) + extra)   # hex / fixed flags only
            return ["ssh"] + SSH_OPTS + port + list(self.cfg.get("ssh_opts") or []) + [self.cfg["host"], REMOTE_CMD % args]
        return [sys.executable, AGENT, "bridge"] + extra       # the local bridge hashes its own file

    def update_agent(self):
        """Replace an outdated daemon now, even with sessions running: they are restarted and resumed
        (same session ids) by the new daemon."""
        self.replace_next = True
        self.reconnect_fresh()

    def start(self):
        self.task = asyncio.ensure_future(self.run())

    async def stop(self):
        if self.task:
            self.task.cancel()
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
        for f in self.waiters.values():
            if not f.done():
                f.set_exception(ConnectionError("machine removed"))
        self.waiters.clear()

    def reconnect_fresh(self):
        """Drop the current connection (e.g. host changed) and reconnect with the new settings."""
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
        self.wake.set()

    def reconnect_now(self):
        self.wake.set()

    async def run(self):
        delay = 1
        while True:
            try:
                await self.session_once()
                delay = 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("%s: %s", self.id, e)
                self.error = str(e)[:300]
            self.online = False
            self.stats = None
            for f in self.waiters.values():
                if not f.done():
                    f.set_exception(ConnectionError("machine offline"))
            self.waiters.clear()
            self.on_state(self)
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 30)

    async def stats_loop(self):
        tick = 0
        while True:
            if tick % USAGE_EVERY == 0:
                await self.poll_usage()
            tick += 1
            await asyncio.sleep(STATS_EVERY)
            t0 = time.monotonic()
            try:
                r = await self.call({"op": "stats"}, timeout=10)
            except (ConnectionError, asyncio.TimeoutError):
                continue
            st = dict(r.get("stats") or {})
            st["rtt_ms"] = round((time.monotonic() - t0) * 1000, 1)
            self.stats = st
            self.on_state(self)

    async def poll_usage(self):
        """Today's token totals over every transcript on that machine (older agents answer with an error)."""
        try:
            r = await self.call({"op": "usage_today"}, timeout=30)
        except (ConnectionError, asyncio.TimeoutError):
            return
        if r.get("t") == "usage_today":
            self.usage_today = {k: v for k, v in r.items() if k not in ("t", "req")}
            self.on_state(self)

    async def session_once(self):
        src, code = agent_source()
        replace, self.replace_next = self.replace_next, False
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv(code, replace), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=64 * 1024 * 1024, start_new_session=True)
        errbuf = []

        async def pump_err():
            while True:
                ln = await self.proc.stderr.readline()
                if not ln:
                    return
                s = ln.decode("utf-8", "replace").strip()
                if s and not s.startswith("Welcome to "):     # ssh bastion banners
                    errbuf.append(s)
                    del errbuf[:-5]
        errt = asyncio.ensure_future(pump_err())
        statst = None
        try:
            if self.cfg.get("type") == "ssh":
                self.proc.stdin.write(base64.b64encode(zlib.compress(src, 9)) + b"\n")
            self.write({"op": "hello", "client": "hub"})
            hello = False
            while True:
                ln = await self.proc.stdout.readline()
                if not ln:
                    break
                try:
                    m = json.loads(ln)
                except ValueError:
                    continue       # login banners etc.
                if not isinstance(m, dict):
                    continue
                if not hello:
                    if m.get("t") != "hello":
                        continue
                    hello = True
                    self.online, self.error = True, None
                    self.version, self.host = m.get("version"), m.get("host")
                    # the bridge replaces an outdated daemon unless sessions run there; then it is kept and flagged
                    self.code = m.get("code")
                    self.outdated = self.code != code
                    # errors from the previous daemon (e.g. "agent too old") do not apply to this one
                    if self.netinfo and self.netinfo.get("error"):
                        self.netinfo = None
                    if self.speed and self.speed.get("error"):
                        self.speed = None
                    self.sessions = {s["sid"]: s for s in m.get("sessions", [])}
                    self.order = [s["sid"] for s in m.get("sessions", [])]
                    log.info("%s online (%s, %d sessions%s)", self.id, self.host, len(self.order),
                             ", agent outdated" if self.outdated else "")
                    self.on_state(self)
                    statst = asyncio.ensure_future(self.stats_loop())
                    await self.on_msg(self, {"t": "resync"})
                    continue
                try:
                    await self.handle(m)
                except Exception:
                    log.exception("%s: error handling %s", self.id, m.get("t"))
        finally:
            errt.cancel()
            if statst:
                statst.cancel()
            if self.proc.returncode is None:
                self.proc.kill()
            await self.proc.wait()
        msg = "; ".join(errbuf) or "connection closed (exit %s)" % self.proc.returncode
        raise ConnectionError(msg)

    async def handle(self, m):
        t = m.get("t")
        if t == "session":
            s = m["s"]
            if s["sid"] not in self.sessions:
                self.order.append(s["sid"])
            self.sessions[s["sid"]] = s
            self.on_state(self)
        elif t == "gone":
            self.sessions.pop(m["sid"], None)
            if m["sid"] in self.order:
                self.order.remove(m["sid"])
            self.on_state(self)
        req = m.get("req")
        if req is not None and req in self.waiters:
            f = self.waiters.pop(req)
            if not f.done():
                f.set_result(m)
            return       # replies to call() go only to the caller
        await self.on_msg(self, m)

    def write(self, obj):
        if not self.proc or self.proc.returncode is not None or self.proc.stdin.is_closing():
            raise ConnectionError("machine %s offline" % self.id)
        self.proc.stdin.write(dumps(obj))

    async def call(self, obj, timeout=20):
        self.req += 1
        req = self.req
        f = asyncio.get_running_loop().create_future()
        self.waiters[req] = f
        try:
            self.write(dict(obj, req=req))
            return await asyncio.wait_for(f, timeout)
        finally:
            self.waiters.pop(req, None)

    def next_req(self):
        self.req += 1
        return self.req
