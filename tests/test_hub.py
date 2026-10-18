import asyncio
import json
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "hub"))


@pytest.fixture()
def hub_mod(tmp_path, monkeypatch):
    cfgp = tmp_path / "config.json"
    cfgp.write_text(json.dumps({
        "hub": {"listen": [{"host": "127.0.0.1", "port": 0}], "caffeinate": False},
        "keys": {"env": {"claude": "ANTHROPIC_AUTH_TOKEN", "codex": "OPENAI_API_KEY"},
                 "profiles": [{"name": "k1", "value": "secret-one"}]},
        "machines": [],
    }))
    monkeypatch.setenv("GLIMPSE_CONFIG", str(cfgp))
    import importlib
    import config as C
    importlib.reload(C)
    monkeypatch.setattr(C, "DATA", str(tmp_path / "data"))
    os.makedirs(C.DATA, exist_ok=True)
    import server
    importlib.reload(server)
    monkeypatch.setattr(server, "DATA", C.DATA)
    return C, server


def test_config_defaults_and_key_env(hub_mod):
    C, _ = hub_mod
    cfg = C.load()
    assert cfg["hub"]["approval_wait_seconds"] == 600          # default merged in
    assert C.key_env(cfg, "codex", "v") == {"OPENAI_API_KEY": "v"}
    assert C.key_env(cfg, "claude", "v") == {"ANTHROPIC_AUTH_TOKEN": "v"}
    assert C.key_env(cfg, "shell", "v") == {}
    assert C.profile(cfg, "k1")["value"] == "secret-one"


def test_config_save_is_private(hub_mod):
    C, _ = hub_mod
    cfg = C.load()
    C.save(cfg)
    assert oct(os.stat(C.CONFIG).st_mode & 0o777) == "0o600"


def test_http_auth_static_and_state(hub_mod):
    from aiohttp.test_utils import TestClient, TestServer
    C, server = hub_mod

    async def run():
        hub = server.Hub(C.load())
        client = TestClient(TestServer(hub.app()))
        await client.start_server()
        try:
            r = await client.get("/api/me")
            assert r.status == 401
            r = await client.post("/api/login", json={"password": "wrong"})
            assert r.status in (401, 403)
            r = await client.get("/static/../config.json")
            assert r.status in (401, 403, 404)
            r = await client.post("/api/login", json={"pair": hub.auth.new_pair()})
            assert r.status == 200
            sc = r.headers.get("Set-Cookie", "")
            assert "HttpOnly" in sc and "Secure" in sc and "SameSite=Strict" in sc
            auth = {"Authorization": "Bearer " + (await r.json())["session"]}
            r = await client.get("/api/me", headers=auth)
            assert r.status == 200
            r = await client.get("/static/../../config.json", headers=auth)
            assert r.status in (403, 404)
            r = await client.get("/")
            assert r.status == 200 and "Glimpse" in await r.text()
            ws = await client.ws_connect("/ws", headers=auth)
            st = json.loads((await ws.receive()).data)
            assert st["t"] == "state"
            # key values never reach the phone, only names
            assert st["keys"] == [{"name": "k1"}]
            assert "secret-one" not in json.dumps(st)
            await ws.close()
        finally:
            await client.close()
    asyncio.run(run())


def test_default_codex_key_env_and_options(hub_mod, tmp_path):
    C, server = hub_mod
    assert C.DEFAULTS["keys"]["env"]["codex"] == "OPENAI_API_KEY"
    cfg = C.load()
    cfg["options"] = {"claude": {"models": ["opus"]}}
    opts = server.Hub(cfg).options()
    assert opts["claude"]["models"] == ["opus"]                 # overridden list
    assert "plan" in opts["claude"]["modes"]                    # untouched default
    assert opts["codex"]["sandboxes"][0] == "read-only"
    assert C.DEFAULTS["options"]["claude"]["models"][:2] == ["default", "opus"]   # defaults not mutated
    assert opts["claude"]["efforts"] == ["low", "medium", "high", "xhigh", "max", "ultracode"]
    assert opts["claude"]["modes"] == ["manual", "acceptEdits", "auto", "plan", "dontAsk", "bypassPermissions"]
    assert opts["codex"]["efforts"] == ["low", "medium", "high", "xhigh", "max", "ultra"]
    assert opts["codex"]["approvals"] == ["on-request", "never"]          # codex 0.16x has no "untrusted"
    cfg["options"] = {"codex": {"approvals": ["untrusted", "on-request", "never"]}}   # the list older versions saved
    assert server.Hub(cfg).options()["codex"]["approvals"] == ["on-request", "never"]
    assert "minimal" not in json.dumps(opts)
    # old defaults that an earlier save() wrote into config.json give way to the new ones
    cfg["options"] = {"claude": {"modes": ["default", "acceptEdits", "auto", "plan", "bypassPermissions"],
                                 "efforts": ["low", "medium", "high", "xhigh", "max"],
                                 "models": ["opus", "sonnet", "haiku"]},
                      "codex": {"efforts": ["minimal", "low", "medium", "high", "max"], "models": ["gpt-x"]}}
    opts = server.Hub(cfg).options()
    assert opts["claude"] == C.DEFAULTS["options"]["claude"]
    assert opts["codex"]["efforts"][-1] == "ultra" and opts["codex"]["models"] == ["gpt-x"]


def test_write_secret_atomic(hub_mod, tmp_path):
    C, _ = hub_mod
    p = str(tmp_path / "data" / "s.txt")
    C.write_secret(p, "one")
    C.write_secret(p, "two")
    assert open(p).read() == "two"
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"
    assert os.listdir(os.path.dirname(p)) == ["s.txt"]          # no temp files left behind


def test_build_argv_flags(hub_mod):
    _, server = hub_mod
    b = server.build_argv
    assert b("claude", ["claude", "--settings", "x.json"], {"model": "opus", "effort": "high", "mode": "plan"}) == \
        ["claude", "--settings", "x.json", "--model", "opus", "--effort", "high", "--permission-mode", "plan"]
    assert b("claude", ["claude"], {"mode": "default", "model": ""}) == ["claude"]
    assert b("codex", ["codex", "--no-daemon"], {"model": "gpt-5", "effort": "high", "approval": "never",
                                                 "sandbox": "workspace-write"}) == \
        ["codex", "--no-daemon", "-m", "gpt-5", "-c", 'model_reasoning_effort="high"', "-a", "never",
         "-s", "workspace-write"]
    assert b("shell", [], {"model": "opus"}) == []
    with pytest.raises(ValueError):
        b("claude", ["claude"], {"model": "opus --dangerously-skip-permissions"})
    with pytest.raises(ValueError):
        b("codex", ["codex"], {"effort": 'high" -c x="y'})
    # ultracode has no CLI flag: xhigh on argv, the agent types /effort ultracode after start
    assert b("claude", ["claude"], {"effort": "ultracode"}) == ["claude", "--effort", "xhigh"]
    assert server.init_cmds("claude", {"effort": "ultracode"}) == ["/effort ultracode"]
    assert server.init_cmds("claude", {"effort": "high"}) is None
    assert server.init_cmds("codex", {"effort": "ultra"}) is None
    assert b("codex", ["codex"], {"effort": "ultra"}) == ["codex", "-c", 'model_reasoning_effort="ultra"']


def test_origin_check(hub_mod):
    from aiohttp import WSServerHandshakeError
    from aiohttp.test_utils import TestClient, TestServer
    C, server = hub_mod

    async def run():
        hub = server.Hub(C.load())
        client = TestClient(TestServer(hub.app()))
        await client.start_server()
        try:
            auth = {"Authorization": "Bearer " + hub.token}
            host = "%s:%s" % (client.host, client.port)
            with pytest.raises(WSServerHandshakeError) as ei:
                await client.ws_connect("/ws", headers=dict(auth, Origin="https://evil.example"))
            assert ei.value.status == 403
            ws = await client.ws_connect("/ws", headers=dict(auth, Origin="http://" + host))
            assert json.loads((await ws.receive()).data)["t"] == "state"
            await ws.close()
            ws = await client.ws_connect("/ws", headers=auth)          # no Origin: CLI client
            await ws.close()
        finally:
            await client.close()
    asyncio.run(run())


class FakePhone:
    def __init__(self):
        self.sent = []
        self.subs = set()

    def send(self, obj):
        self.sent.append(obj)


class FakeMachine:
    def __init__(self, mid="M1"):
        self.id, self.name, self.cfg = mid, mid, {"presets": {}}
        self.sessions = {"s1": {"sid": "s1", "status": "running"}, "s2": {"sid": "s2", "status": "exited"}}
        self.written = []
        self.req = 0
        self.stopped = False

    def next_req(self):
        self.req += 1
        return self.req

    def write(self, obj):
        self.written.append(obj)

    async def call(self, obj, timeout=20):
        self.written.append(obj)
        return {"t": "created", "s": {"sid": "new"}}

    async def stop(self):
        self.stopped = True


def test_reply_routing(hub_mod):
    C, server = hub_mod

    async def run():
        hub = server.Hub(C.load())
        m = FakeMachine()
        hub.machines[m.id] = m
        hub.order.append(m.id)
        p, other = FakePhone(), FakePhone()
        hub.phones.update([p, other])
        await hub.phone_op(p, {"op": "approve", "m": "M1", "sid": "s1", "id": "a1", "decision": "allow"})
        req = m.written[-1]["req"]
        await hub.on_machine_msg(m, {"t": "ok", "req": req})
        assert p.sent == [{"t": "approved", "m": "M1", "sid": "s1"}] and other.sent == []
        for op, extra, want in (("control", {"action": "mode", "value": "plan"}, "control_ok"),
                                ("delete", {}, "delete_ok")):
            await hub.phone_op(p, dict({"op": op, "m": "M1", "sid": "s1"}, **extra))
            await hub.on_machine_msg(m, {"t": "ok", "req": m.written[-1]["req"]})
            assert p.sent[-1] == {"t": want, "m": "M1", "sid": "s1"}
        # plain input acks stay silent
        n = len(p.sent)
        await hub.phone_op(p, {"op": "in", "m": "M1", "sid": "s1", "d": ""})
        await hub.on_machine_msg(m, {"t": "ok", "req": m.written[-1]["req"]})
        assert len(p.sent) == n
        # history goes to the asking phone only
        await hub.phone_op(p, {"op": "history", "m": "M1", "kind": "codex"})
        assert m.written[-1]["op"] == "history" and m.written[-1]["kind"] == "codex"
        await hub.on_machine_msg(m, {"t": "history", "req": m.written[-1]["req"], "items": [{"id": "x"}]})
        assert p.sent[-1]["t"] == "history" and p.sent[-1]["m"] == "M1" and p.sent[-1]["items"] == [{"id": "x"}]
        assert other.sent == []
        # bad control values never reach the agent
        n = len(m.written)
        await hub.phone_op(p, {"op": "control", "m": "M1", "sid": "s1", "action": "rm", "value": "x"})
        await hub.phone_op(p, {"op": "control", "m": "M1", "sid": "s1", "action": "model", "value": "a b"})
        assert len(m.written) == n and p.sent[-1]["t"] == "error"
        # clean_exited forgets only finished sessions
        await hub.phone_op(p, {"op": "clean_exited", "m": "M1"})
        assert m.written[-1] == {"op": "forget", "sid": "s2", "req": m.written[-1]["req"]}
        # create carries resume_id and the flag-built argv
        await hub.phone_op(p, {"op": "create", "m": "M1", "kind": "claude", "resume_id": "abc-123", "model": "opus"})
        c = m.written[-1]
        assert c["op"] == "create" and c["resume_id"] == "abc-123" and c["args"] == ["claude", "--model", "opus"]
        await hub.phone_op(p, {"op": "create", "m": "M1", "kind": "claude", "resume_id": "../x"})
        assert p.sent[-1]["t"] == "error"
        # a phone that stopped talking no longer suppresses push
        ph = FakePhone()
        ph.visible, ph.last_seen = True, 0
        assert not server.Phone.on_screen(ph)
    asyncio.run(run())


def test_machine_ops_validate_and_persist(hub_mod, monkeypatch):
    C, server = hub_mod
    started = []
    monkeypatch.setattr(server.Machine, "start", lambda self: started.append(self.id))

    async def run():
        cfg = C.load()
        cfg["machines"] = [{"id": "M1", "name": "mac", "type": "local", "default_cwd": "~",
                            "presets": {"claude": {"argv": ["claude", "--settings", "s.json"]}}}]
        hub = server.Hub(cfg)
        p = FakePhone()
        for bad in ("-oProxyCommand=x", "a b", "host;rm", ""):
            await hub.phone_op(p, {"op": "machine_add", "name": "x", "host": bad})
            assert p.sent[-1]["t"] == "error", bad
        await hub.phone_op(p, {"op": "machine_add", "name": "box", "host": "user@devbox", "port": "70000"})
        assert p.sent[-1]["t"] == "error"
        assert len(hub.cfg["machines"]) == 1
        await hub.phone_op(p, {"op": "machine_add", "name": "box", "host": "user@devbox", "port": 2222,
                               "default_cwd": "~/code"})
        assert p.sent[-1] == {"t": "machines_ok", "op": "machine_add"}
        assert started == ["M2"] and hub.order == ["M1", "M2"]
        saved = json.load(open(C.CONFIG))
        m2 = saved["machines"][1]
        assert m2["id"] == "M2" and m2["type"] == "ssh" and m2["host"] == "user@devbox" and m2["port"] == 2222
        assert m2["presets"]["claude"]["argv"] == ["claude", "--settings", "s.json"]
        assert hub.machines["M2"].argv()[-4:-2] == ["-p", "2222"]
        await hub.phone_op(p, {"op": "machine_update", "m": "M2", "name": "box2", "port": None})
        assert hub.machines["M2"].name == "box2" and "port" not in json.load(open(C.CONFIG))["machines"][1]
        await hub.phone_op(p, {"op": "machine_remove", "m": "M1"})
        assert p.sent[-1]["t"] == "error"                               # local machine is protected
        await hub.phone_op(p, {"op": "machine_update", "m": "M1", "host": "x"})
        assert p.sent[-1]["t"] == "error"
        await hub.phone_op(p, {"op": "machine_remove", "m": "M2"})
        assert p.sent[-1]["t"] == "machines_ok" and "M2" not in hub.machines and hub.order == ["M1"]
        assert [x["id"] for x in json.load(open(C.CONFIG))["machines"]] == ["M1"]
        await hub.phone_op(p, {"op": "machine_add", "name": "again", "host": "devbox"})
        assert hub.cfg["machines"][-1]["id"] == "M2"
        await hub.phone_op(p, {"op": "machine_test", "host": "-oProxyCommand=evil"})
        assert p.sent[-1]["t"] == "error"
    asyncio.run(run())


def test_resolve_hub_auto(hub_mod, monkeypatch):
    C, server = hub_mod
    monkeypatch.setattr(server, "detect_tailscale", lambda: ("100.1.2.3", "mac.tail.ts.net"))
    server.RUNTIME.clear()
    cfg = C.load()
    cfg["hub"]["listen"] = [{"host": "auto", "port": 8443}, {"host": "127.0.0.1", "port": 8443}]
    cfg["hub"]["tailscale_hostname"] = "auto"
    listen, name = server.resolve_hub(cfg)
    assert listen[0]["host"] == "100.1.2.3" and name == "mac.tail.ts.net"
    url = server.login_url(cfg, mask=True)
    assert url == "https://mac.tail.ts.net:8443/"
    assert C.token() not in url
    full = server.login_url(cfg)
    assert full.startswith("https://mac.tail.ts.net:8443/?pair=") and C.token() not in full
    server.RUNTIME.clear()


def test_create_ultracode_and_codex_ultra_control(hub_mod):
    C, server = hub_mod

    async def run():
        hub = server.Hub(C.load())
        m = FakeMachine()
        m.cfg["presets"] = {"claude": {"argv": ["claude", "--settings", "s.json"]}}
        hub.machines[m.id] = m
        hub.order.append(m.id)
        p = FakePhone()
        await hub.phone_op(p, {"op": "create", "m": "M1", "kind": "claude", "effort": "ultracode"})
        c = m.written[-1]
        assert c["args"] == ["claude", "--settings", "s.json", "--effort", "xhigh"]
        assert "ultracode" not in c["args"] and c["init_cmds"] == ["/effort ultracode"]
        await hub.phone_op(p, {"op": "create", "m": "M1", "kind": "codex", "effort": "high"})
        assert m.written[-1]["init_cmds"] is None
        await hub.phone_op(p, {"op": "control", "m": "M1", "sid": "s1", "action": "effort", "value": "ultra"})
        assert m.written[-1]["op"] == "control" and m.written[-1]["value"] == "ultra"
        await hub.phone_op(p, {"op": "control", "m": "M1", "sid": "s1", "action": "effort", "value": "ultracode"})
        assert m.written[-1]["value"] == "ultracode"
    asyncio.run(run())


def test_menu_op_validation_and_reply(hub_mod):
    C, server = hub_mod

    async def run():
        hub = server.Hub(C.load())
        m = FakeMachine()
        hub.machines[m.id] = m
        hub.order.append(m.id)
        p, other = FakePhone(), FakePhone()
        hub.phones.update([p, other])
        await hub.phone_op(p, {"op": "menu", "m": "M1", "sid": "s1", "index": 3, "extra": "x"})
        w = m.written[-1]
        assert w == {"op": "menu", "sid": "s1", "index": 3, "req": w["req"]}
        await hub.on_machine_msg(m, {"t": "ok", "req": w["req"]})
        assert p.sent[-1] == {"t": "menu_ok", "m": "M1", "sid": "s1"} and other.sent == []
        for key in ("esc", "s"):
            await hub.phone_op(p, {"op": "menu", "m": "M1", "sid": "s1", "key": key})
            assert m.written[-1]["key"] == key and "index" not in m.written[-1]
        n = len(m.written)
        for bad in ({"index": -1}, {"index": 31}, {"index": "2"}, {"index": True}, {"index": 1.5},
                    {"key": "enter"}, {"key": "\x1b[A"}, {}):
            await hub.phone_op(p, dict({"op": "menu", "m": "M1", "sid": "s1"}, **bad))
            assert p.sent[-1]["t"] == "error", bad
        await hub.phone_op(p, {"op": "menu", "m": "M1", "index": 0})          # no session
        assert p.sent[-1]["t"] == "error" and len(m.written) == n
        # agent errors (no menu on screen) go back to the asking phone
        await hub.phone_op(p, {"op": "menu", "m": "M1", "sid": "s1", "index": 0})
        await hub.on_machine_msg(m, {"t": "error", "req": m.written[-1]["req"], "msg": "no menu"})
        assert p.sent[-1]["t"] == "error" and p.sent[-1]["msg"] == "no menu" and other.sent == []
    asyncio.run(run())


def test_session_menu_passes_through_state(hub_mod):
    C, server = hub_mod
    from machines import Machine

    async def run():
        cfg = C.load()
        cfg["machines"] = [{"id": "M1", "name": "mac", "type": "local"}]
        hub = server.Hub(cfg)
        m = hub.machines["M1"]
        assert isinstance(m, Machine)
        menu = {"kind": "list", "title": "Select model", "cursor": 1, "hint": "Esc to cancel",
                "options": [{"i": 1, "label": "Default", "desc": "", "current": True}]}
        await m.handle({"t": "session", "s": {"sid": "s1", "status": "running", "menu": menu}})
        st = hub.state()
        assert st["machines"][0]["sessions"][0]["menu"] == menu
        await m.handle({"t": "session", "s": {"sid": "s1", "status": "running", "menu": None}})
        assert hub.state()["machines"][0]["sessions"][0]["menu"] is None
    asyncio.run(run())


class ModelsMachine(FakeMachine):
    def __init__(self):
        FakeMachine.__init__(self)
        self.cfg["presets"] = {"codex": {"argv": ["codex", "--profile", "x"]}}
        self.calls = []
        self.reply = {"t": "models", "kind": "codex", "efforts": ["low", "ultra"], "modes": [],
                      "models": [{"id": "gpt-x", "label": "GPT-X", "desc": "", "efforts": ["low", "ultra"],
                                  "default_effort": "low"}]}

    async def call(self, obj, timeout=20):
        self.calls.append(obj)
        await asyncio.sleep(0.01)
        return dict(self.reply, req=obj.get("req"))


def test_models_forwarding_and_cache(hub_mod, monkeypatch):
    C, server = hub_mod

    async def run():
        hub = server.Hub(C.load())
        m = ModelsMachine()
        hub.machines[m.id] = m
        hub.order.append(m.id)
        p, q, other = FakePhone(), FakePhone(), FakePhone()
        hub.phones.update([p, q, other])
        # two phones at once -> one agent call with the preset argv
        await asyncio.gather(hub.phone_op(p, {"op": "models", "m": "M1", "kind": "codex"}),
                             hub.phone_op(q, {"op": "models", "m": "M1", "kind": "codex"}))
        assert m.calls == [{"op": "models", "kind": "codex", "argv": ["codex", "--profile", "x"]}]
        want = {"t": "models", "m": "M1", "kind": "codex", "models": m.reply["models"],
                "efforts": ["low", "ultra"], "modes": []}
        assert p.sent == [want] and q.sent == [want] and other.sent == []
        await hub.phone_op(p, {"op": "models", "m": "M1", "kind": "codex"})
        assert len(m.calls) == 1 and p.sent[-1] == want                     # cached
        # claude falls back to [kind] when the machine has no preset
        await hub.phone_op(p, {"op": "models", "m": "M1", "kind": "claude"})
        assert m.calls[-1] == {"op": "models", "kind": "claude", "argv": ["claude"]}
        # expiry after 10 minutes
        now = server.time.time()
        monkeypatch.setattr(server.time, "time", lambda: now + server.MODELS_TTL + 1)
        await hub.phone_op(p, {"op": "models", "m": "M1", "kind": "codex"})
        assert len([c for c in m.calls if c["kind"] == "codex"]) == 2
        # errors are not cached and go only to the asker
        hub.models_cache.clear()
        m.reply = {"t": "error", "msg": "codex not found"}
        await hub.phone_op(q, {"op": "models", "m": "M1", "kind": "codex"})
        assert q.sent[-1]["t"] == "error" and q.sent[-1]["msg"] == "codex not found"
        assert ("M1", "codex") not in hub.models_cache and other.sent == []
        await hub.phone_op(p, {"op": "models", "m": "M1", "kind": "shell"})
        assert p.sent[-1]["t"] == "error"
    asyncio.run(run())


class NetMachine(FakeMachine):
    def __init__(self):
        FakeMachine.__init__(self)
        self.netinfo = self.speed = self.speed_task = None
        self.calls = []
        self.replies = {"speedtest": {"t": "speedtest", "down_bps": 80_000_000, "ttfb_ms": 40.0, "bytes": 10, "secs": 1.0},
                        "netinfo": {"t": "netinfo", "ip": "203.0.113.9", "city": "Shanghai", "region": "Shanghai",
                                    "country": "CN", "org": "AS1", "timezone": "Asia/Shanghai", "ts": 1.0}}

    async def call(self, obj, timeout=20):
        self.calls.append(obj)
        await asyncio.sleep(0.05)
        r = self.replies[obj["op"]]
        if isinstance(r, Exception):
            raise r
        return r


def test_speedtest_shared_and_netinfo(hub_mod):
    C, server = hub_mod

    async def run():
        hub = server.Hub(C.load())
        m = NetMachine()
        hub.machines[m.id] = m
        hub.order.append(m.id)
        p, q = FakePhone(), FakePhone()
        hub.phones.update([p, q])
        # two devices at once -> one test on the machine; both get the result
        await hub.phone_op(p, {"op": "speedtest", "m": "M1"})
        await hub.phone_op(q, {"op": "speedtest", "m": "M1"})
        await asyncio.sleep(0.15)
        assert [c["op"] for c in m.calls] == ["speedtest"]
        assert m.speed["down_bps"] == 80_000_000 and m.speed["ts"] > 0
        for ph in (p, q):
            assert ph.sent[-1]["t"] == "speedtest_done" and ph.sent[-1]["speed"]["down_bps"] == 80_000_000
        # agent errors and timeouts become a speed {error}
        m.replies["speedtest"] = {"t": "error", "msg": "测速失败: boom"}
        await hub.phone_op(p, {"op": "speedtest", "m": "M1"})
        await asyncio.sleep(0.1)
        assert m.speed["error"] == "测速失败: boom" and p.sent[-1]["speed"]["error"]
        m.replies["speedtest"] = asyncio.TimeoutError()
        await hub.phone_op(p, {"op": "speedtest", "m": "M1"})
        await asyncio.sleep(0.1)
        assert m.speed["error"] == "测速超时"
        # netinfo is stored on the machine and published with the state
        await hub.phone_op(p, {"op": "netinfo", "m": "M1", "refresh": True})
        await asyncio.sleep(0.1)
        assert m.calls[-1] == {"op": "netinfo", "refresh": True}
        assert m.netinfo["ip"] == "203.0.113.9" and m.netinfo["city"] == "Shanghai"
        m.replies["netinfo"] = {"t": "error", "msg": "查询公网 IP 失败: x"}
        await hub.machine_netinfo(m)
        assert m.netinfo["error"].startswith("查询公网 IP 失败")
        # an agent older than the op says so
        m.replies["speedtest"] = {"t": "error", "msg": "unknown op 'speedtest'"}
        await hub.phone_op(p, {"op": "speedtest", "m": "M1"})
        await asyncio.sleep(0.1)
        assert "需要更新" in m.speed["error"]
        # the update op asks the machine to replace its daemon
        m.update_agent = lambda: m.calls.append({"op": "UPDATE"})
        await hub.phone_op(p, {"op": "agent_update", "m": "M1"})
        assert m.calls[-1] == {"op": "UPDATE"} and p.sent[-1] == {"t": "agent_update_ok", "m": "M1"}
    asyncio.run(run())


def test_push_only_skips_devices_with_the_app_on_screen(hub_mod, monkeypatch):
    C, server = hub_mod

    async def run():
        hub = server.Hub(C.load())
        m = FakeMachine()
        hub.machines[m.id] = m
        sent = []

        async def fake_send(http, vapid, sub, payload, subject):
            sent.append(sub["endpoint"])
            return 201, ""
        monkeypatch.setattr(server.webpush, "send", fake_send)
        hub.push_subs = [{"endpoint": "https://p/phone", "keys": {}, "device": "phone"},
                         {"endpoint": "https://p/desk", "keys": {}, "device": "desk"},
                         {"endpoint": "https://p/old", "keys": {}}]          # saved before devices were recorded

        def ph(dev, visible):
            p = FakePhone()
            p.device, p.visible, p.last_seen = dev, visible, server.time.time()
            p.on_screen = lambda p=p: server.Phone.on_screen(p)
            return p
        # the desktop window is on screen: the phone is still notified, the desktop is not
        hub.phones = {ph("desk", True), ph("phone", False), ph(None, True)}     # None = a local CLI client
        await hub.on_event(m, "s1", {"kind": "done", "title": "已完成本轮", "body": "a"})
        assert sent == ["https://p/phone"]
        # nobody on screen: everyone, including the old subscription
        sent.clear()
        hub.phones = {ph("desk", False), ph(None, True)}
        await hub.on_event(m, "s1", {"kind": "done", "title": "已完成本轮", "body": "b"})
        assert sorted(sent) == ["https://p/desk", "https://p/old", "https://p/phone"]
    asyncio.run(run())


def test_new_agent_code_flags_and_updates_idle_machines(hub_mod):
    import machines as M
    m = M.Machine({"id": "M2", "type": "ssh", "host": "box"}, None, None)
    reconnects = []
    m.reconnect_fresh = lambda: reconnects.append(1)
    m.online, m.code, m.outdated = True, "old", False
    m.sessions = {"a": {"sid": "a", "pid": 5, "exit_code": None, "status": "idle"}}
    assert M.check_code(m, "new") is True and m.outdated and reconnects == []          # busy: flag only
    m.sessions = {}
    assert M.check_code(m, "new") is False and reconnects == [1]                       # idle: reconnect once
    assert M.check_code(m, "new") is False and reconnects == [1]
    m.code = "new"
    assert M.check_code(m, "new") is True and not m.outdated
    m.online = False
    assert M.check_code(m, "newer") is False                                            # offline: nothing
    assert M.agent_code() == M.agent_source()[1]


def test_bridge_argv_code_and_replace(hub_mod):
    import machines as M
    src, code = M.agent_source()
    assert len(code) == 12 and code == __import__("hashlib").sha256(src).hexdigest()[:12]
    ssh = M.Machine({"id": "M2", "type": "ssh", "host": "box"}, None, None)
    cmd = ssh.argv(code, False)[-1]
    assert 'sys.argv=["glimpse","bridge","--code","%s"]' % code in cmd
    assert '"--replace"' in ssh.argv(code, True)[-1] and ssh.argv()[-2] == "box"
    loc = M.Machine({"id": "M1", "type": "local"}, None, None)
    assert loc.argv(code)[-1] == "bridge" and loc.argv(code, True)[-2:] == ["bridge", "--replace"]
    loc.reconnect_fresh = lambda: None
    loc.update_agent()
    assert loc.replace_next and loc.info({})["outdated"] is False


def test_login_pairing_password_devices(hub_mod, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    C, server = hub_mod
    from auth import Auth

    async def run():
        hub = server.Hub(C.load())
        client = TestClient(TestServer(hub.app()))
        await client.start_server()
        try:
            code = hub.auth.new_pair()
            r = await client.post("/api/login", json={"pair": code})
            assert r.status == 200
            tok1 = (await r.json())["session"]
            r = await client.post("/api/login", json={"pair": code})           # single use
            assert r.status == 401
            # a pairing code made by the CLI (another Auth instance on the same file) works on the running hub
            cli = Auth(hub.auth.path, C.write_secret)
            r = await client.post("/api/login", json={"pair": cli.new_pair()})
            assert r.status == 200
            tok2 = (await r.json())["session"]
            a1, a2 = ({"Authorization": "Bearer " + t} for t in (tok1, tok2))
            r = await client.get("/api/devices", headers=a1)
            devs = (await r.json())["devices"]
            assert len(devs) == 2 and sum(d["current"] for d in devs) == 1
            other = [d for d in devs if not d["current"]][0]["id"]
            # device 1 logs device 2 out; device 2 stops working immediately
            r = await client.post("/api/devices/revoke", headers=a1, json={"id": other})
            assert r.status == 200
            r = await client.get("/api/me", headers=a2)
            assert r.status == 401
            # password: none yet -> set -> login with it; wrong current password is refused when changing
            r = await client.post("/api/login", json={"password": "anything1"})
            assert r.status == 401
            r = await client.post("/api/password", headers=a1, json={"new": "short"})
            assert r.status == 400
            r = await client.post("/api/password", headers=a1, json={"new": "correct horse"})
            assert r.status == 200
            r = await client.post("/api/password", headers=a1, json={"current": "bad", "new": "another one"})
            assert r.status == 403
            r = await client.get("/api/me", headers=a1)
            assert r.status == 200 and (await r.json())["has_password"] is True
            r = await client.post("/api/login", json={"password": "correct horse"})
            assert r.status == 200
            # brute force: lockout after repeated failures
            for _ in range(6):
                r = await client.post("/api/login", json={"password": "nope-nope"})
            assert r.status == 429
            # auth.json stores hashes only
            raw = open(hub.auth.path).read()
            assert tok1 not in raw and "correct horse" not in raw
            assert oct(os.stat(hub.auth.path).st_mode & 0o777) == "0o600"
        finally:
            await client.close()
    asyncio.run(run())


def test_cli_token_only_from_localhost(hub_mod):
    C, server = hub_mod
    hub = server.Hub(C.load())

    class Req:
        def __init__(self, remote):
            self.remote, self.path, self.query, self.cookies = remote, "/api/me", {}, {}
            self.headers = {"Authorization": "Bearer " + hub.token}
    assert hub.authed(Req("127.0.0.1")) is True
    assert hub.authed(Req("100.64.1.2")) is False                 # over Tailscale the file token is useless


def _wk(limit, used, days_left, now=1000.0, ts=None, pct=None):
    """usage dict with one 7-day window"""
    return {"ts": now if ts is None else ts, "windows": [{"window_sec": 18000, "percent": 10, "limit": 100, "used": 10, "reset_at": now + 3600},
                                         {"window_sec": 604800, "limit": limit, "used": used, "reset_at": now + days_left * 86400,
                                          "percent": pct if pct is not None else 100.0 * used / limit}]}


def test_keybalance_capacity_and_rates():
    import keybalance as KB
    now = 1000.0
    # 70 left, 7 days to the reset -> 10 per day
    assert abs(KB.capacity(_wk(100, 30, 7), now) - 70 / (7 * 86400)) < 1e-12
    assert KB.capacity({"ts": 1, "windows": [{"window_sec": 18000, "limit": 1, "used": 0}]}, now) is None   # 5 h only
    t = KB.RateTracker()
    t.observe("a", _wk(100, 10, 7, ts=0))
    t.observe("a", _wk(100, 13, 7, ts=300))            # 3 per 300 s
    assert abs(t.rate["a"] - 0.01) < 1e-12
    t.observe("a", _wk(100, 1, 7, ts=600))             # window reset: rate starts over
    assert "a" not in t.rate


def test_keybalance_pick_equalises_burn_rate():
    import keybalance as KB
    now = 1000.0
    # same burn rate, but k2 has much more left: the new session goes to k2 even though k2 used more in absolute terms
    u = {"k1": _wk(100, 80, 3, now), "k2": _wk(1000, 300, 3, now)}
    rates = {"k1": 0.001, "k2": 0.001}
    assert KB.pick_key(["k1", "k2"], u, in_use={"k1": 1, "k2": 1}, rates=rates, now=now) == "k2"
    # a key spent on the 7-day window or flagged invalid is never picked; the 5 h window does not matter here
    u["k3"] = _wk(100, 100, 3, now)
    u["k4"] = {"ts": 1, "error": "Key 无效或已停用"}
    u["k5"] = _wk(500, 10, 3, now)
    u["k5"]["windows"][0]["percent"] = 100                # 5 h full: left to on_exhausted
    assert KB.pick_key(["k3", "k4", "k5"], u, now=now) == "k5"
    assert KB.pick_key(["k3", "k4", "k5"], u, now=now, any_window=True) is None   # replacement after no balance
    assert KB.pick_key(["k3", "k4"], u, now=now) is None
    # no usage data at all: fewest sessions, then order
    assert KB.pick_key(["a", "b"], {}, in_use={"a": 2}) == "b"


def test_keybalance_rebalance_only_when_it_helps():
    import keybalance as KB
    now = 1000.0
    u = {"k1": _wk(100, 90, 3, now), "k2": _wk(100, 10, 3, now)}
    # k1: 10 left, 3 sessions burning; k2: 90 left, 1 session -> moving one session lowers the max load a lot
    rates = {"k1": 3e-4, "k2": 1e-4}
    assert KB.should_rebalance("k1", ["k1", "k2"], u, in_use={"k1": 3, "k2": 1}, rates=rates, now=now) == "k2"
    # balanced already: stay
    u2 = {"k1": _wk(100, 50, 3, now), "k2": _wk(100, 50, 3, now)}
    assert KB.should_rebalance("k1", ["k1", "k2"], u2, in_use={"k1": 1, "k2": 1}, rates={"k1": 1e-4, "k2": 1e-4}, now=now) is None
    # no measured rates yet: no move
    assert KB.should_rebalance("k1", ["k1", "k2"], u, in_use={"k1": 3, "k2": 1}, now=now) is None


def test_keybalance_fatal_and_exhausted():
    import keybalance as KB
    for t in ("API Error: 500 Key余额不足，无法完成当前操作", 'unexpected status 666: {"code":40111,"code_msg":"API KEY 权限验证失败"}',
              "Invalid API key · Please run /login", "insufficient_quota"):
        assert KB.is_key_fatal(t), t
    for t in ("stream disconnected before completion", "prompt is too long: 210000 tokens", "exit code 1", "429 rate limit"):
        assert not KB.is_key_fatal(t), t
    assert KB.live_exhausted({"k3": 5}, {"k3": {"ts": 9, "windows": [{"percent": 10}]}}, now=10) == {}
    assert KB.live_exhausted({"k2": 5}, {}, now=10) == {"k2": 5}
    assert KB.live_exhausted({"k2": 5}, {}, now=5 + KB.EXHAUSTED_TTL + 1) == {}


class _M:
    """Machine stub: records calls, answers restart like an agent that can swap live."""
    id, name = "M1", "mac"

    def __init__(self, hot=True):
        self.sessions = {"s1": {"sid": "s1", "kind": "codex", "key": "k1", "status": "error", "hot_key": hot}}
        self.calls, self.writes, self.hot = [], [], hot

    async def call(self, obj, timeout=20):
        self.calls.append(obj)
        return {"t": "restarted", "hot": self.hot}

    def write(self, obj):
        self.writes.append(obj)


def test_balance_swaps_key_on_no_balance(hub_mod, monkeypatch):
    import asyncio
    C, server = hub_mod
    cfg = C.load()
    cfg["keys"]["profiles"] = [{"name": "k1", "value": "v1"}, {"name": "k2", "value": "v2"}, {"name": "k3", "value": "v3"}]
    cfg["keys"]["env"]["codex"] = "V_API_KEY"
    h = server.Hub(cfg)
    m = _M()
    ev = {"kind": "error", "title": "出错停止", "body": "unexpected status 666: Key余额不足，无法完成当前操作"}

    async def run(e):
        return await h.balance_on_event(m, "s1", e)
    assert asyncio.run(run(ev)) is None                                 # mode off: nothing happens
    cfg["keys"]["balance"] = {"mode": "on_exhausted", "exclude": ["k2"]}
    sleeps = []

    async def no_sleep(t):
        sleeps.append(t)
    monkeypatch.setattr(server.asyncio, "sleep", no_sleep)

    async def run2():
        out = await h.balance_on_event(m, "s1", ev)
        await asyncio.gather(*[t for t in asyncio.all_tasks() if t is not asyncio.current_task()])
        return out
    out = asyncio.run(run2())
    assert out["kind"] == "keyswap" and "k1" in out["title"] and "k3" in out["title"]      # k2 excluded
    assert m.calls[-1]["op"] == "restart" and m.calls[-1]["key"] == "k3" and m.calls[-1]["env"] == {"V_API_KEY": "v3"}
    assert m.writes[-1] == {"op": "text", "sid": "s1", "text": "继续"} and sleeps[0] == 5      # hot swap: wait for claude
    assert "k1" in h.key_exhausted and any(k.get("exhausted") for k in h.keys_view() if k["name"] == "k1")
    # a transient error is not the key's fault
    n = len(m.calls)
    assert asyncio.run(run({"kind": "error", "title": "出错停止", "body": "stream disconnected"})) is None and len(m.calls) == n
    # the loop guard stops after SWAP_LIMIT swaps
    h.key_exhausted.clear()
    m.sessions["s1"]["key"] = "k3"
    for _ in range(5):
        out = asyncio.run(run2())
        m.sessions["s1"]["key"] = m.calls[-1]["key"]
        h.key_exhausted.clear()
    assert "请手动处理" in out["title"]


def test_least_used_picks_key_for_new_sessions(hub_mod):
    C, server = hub_mod
    cfg = C.load()
    cfg["keys"]["profiles"] = [{"name": "k1", "value": "v1"}, {"name": "k2", "value": "v2"}]
    cfg["keys"]["balance"] = {"mode": "least_used", "exclude": []}
    h = server.Hub(cfg)
    import time as _t
    now = _t.time()
    h.key_usage = {"k1": _wk(100, 80, 3, now), "k2": _wk(100, 10, 3, now)}
    assert h.best_key() == "k2"
    assert h.balance_view() == {"mode": "least_used", "exclude": [], "usage": False}
