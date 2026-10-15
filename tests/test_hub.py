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
            r = await client.post("/api/login", json={"token": "wrong"})
            assert r.status in (401, 403)
            r = await client.get("/static/../config.json")
            assert r.status in (401, 403, 404)
            r = await client.post("/api/login", json={"token": hub.token})
            assert r.status == 200
            sc = r.headers.get("Set-Cookie", "")
            assert "HttpOnly" in sc and "Secure" in sc and "SameSite=Strict" in sc
            auth = {"Authorization": "Bearer " + hub.token}
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
    assert C.DEFAULTS["options"]["claude"]["models"] == ["opus", "sonnet", "haiku"]   # defaults not mutated


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
    assert url == "https://mac.tail.ts.net:8443/?t=<run ./glimpse url>"
    assert C.token() not in url
    server.RUNTIME.clear()
