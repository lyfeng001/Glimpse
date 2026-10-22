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


def test_new_session_url(hub_mod, monkeypatch, tmp_path):
    from urllib.parse import parse_qs, urlsplit
    C, server = hub_mod
    monkeypatch.setattr(server, "detect_tailscale", lambda: ("100.1.2.3", "mac.tail.ts.net"))
    server.RUNTIME.clear()
    cfg = C.load()
    cfg["hub"]["listen"] = [{"host": "auto", "port": 8443}]
    cfg["hub"]["tailscale_hostname"] = "auto"
    cfg["machines"] = [{"id": "M2", "type": "ssh"}, {"id": "M1", "type": "local"}]
    d = tmp_path / "我的 项目 & #1"
    (d / "src").mkdir(parents=True)
    (d / "src" / "a.py").write_text("x")

    def parse(url):
        assert url.startswith("https://mac.tail.ts.net:8443/#new?")
        q = parse_qs(urlsplit(url).fragment[4:])               # what the web app's URLSearchParams sees
        return q["cwd"][0], q.get("m", [None])[0]
    assert parse(server.new_session_url(cfg, str(d))) == (str(d), "M1")
    assert parse(server.new_session_url(cfg, str(d / "src" / "a.py"))) == (str(d / "src"), "M1")   # a file: its folder
    url = server.new_session_url(cfg, str(d))
    assert " " not in url and "&m=" in url and url.count("#") == 1
    monkeypatch.chdir(d)
    assert parse(server.new_session_url(cfg, "src")) == (str(d / "src"), "M1")                    # relative to here
    with pytest.raises(ValueError):
        server.new_session_url(cfg, str(d / "missing"))
    assert server.new_session_url(cfg, str(d), "https://127.0.0.1:8443/").startswith("https://127.0.0.1:8443/#new?cwd=")
    cfg["machines"] = []
    assert parse(server.new_session_url(cfg, str(d)))[1] is None
    server.RUNTIME.clear()


def test_finder_quick_action(tmp_path):
    import plistlib
    import shlex
    import subprocess
    import finder
    from urllib.parse import parse_qs, urlsplit
    dest = finder.write(str(tmp_path / "Glimpse.workflow"), "https://mac.tail.ts.net:8443/", "M1")
    info = plistlib.load(open(os.path.join(dest, "Contents", "Info.plist"), "rb"))
    svc = info["NSServices"][0]
    assert svc["NSMenuItem"]["default"] == finder.MENU and svc["NSMessage"] == "runWorkflowAsService"
    assert svc["NSRequiredContext"]["NSApplicationIdentifier"] == "com.apple.finder"
    assert svc["NSSendFileTypes"] == ["public.folder"]
    assert finder.ours(dest) and not finder.ours(str(tmp_path))
    doc = plistlib.load(open(os.path.join(dest, "Contents", "document.wflow"), "rb"))
    p = doc["actions"][0]["action"]["ActionParameters"]
    assert p["inputMethod"] == 1 and p["shell"] == "/bin/sh"           # the selection arrives as "$@", not as text
    assert doc["workflowMetaData"]["serviceApplicationBundleID"] == "com.apple.finder"
    cmd = p["COMMAND_STRING"]
    # Quick Actions may not touch ~/Documents (where the project lives): the script runs nothing from the project
    assert ROOT not in cmd and os.path.abspath(ROOT) not in cmd and "gli" not in cmd
    if sys.platform != "darwin":
        return
    for f in ("Info.plist", "document.wflow"):
        subprocess.run(["plutil", "-lint", os.path.join(dest, "Contents", f)], check=True, capture_output=True)
    # run it with `open` replaced by printf: the link for the selected folder (one argument, never evaluated)
    sh = tmp_path / "run.sh"
    sh.write_text(cmd.replace("/usr/bin/open", "printf '%s'"))
    d = "/Users/x/我的 项目 & #1/$(touch boom)"
    url = subprocess.run(["/bin/sh", str(sh), d], capture_output=True, text=True, cwd=str(tmp_path)).stdout
    assert url.startswith("https://mac.tail.ts.net:8443/#new?")
    q = parse_qs(urlsplit(url).fragment[4:])
    assert q["cwd"] == [d] and q["m"] == ["M1"] and not (tmp_path / "boom").exists()
    assert subprocess.run(["/bin/sh", str(sh)], capture_output=True, text=True).stdout == ""   # nothing selected
    sh.write_text(finder.script("https://h:1/", "").replace("/usr/bin/open", "printf '%s'"))
    assert subprocess.run(["/bin/sh", str(sh), "/a"], capture_output=True, text=True).stdout == "https://h:1/#new?cwd=%2Fa"
    # an installed Glimpse window but no way to reach the hub over 127.0.0.1: the link, in the default browser
    # (Chrome would open the web app at its start page and drop the link)
    app = "/Users/x/Applications/Chrome Apps.localized/Glimpse.app"
    sh.write_text(finder.script("https://127.0.0.1:8443/", "M1", app).replace("/usr/bin/open", "printf '%s|'"))
    out = subprocess.run(["/bin/sh", str(sh), "/a b"], capture_output=True, text=True).stdout
    assert out == "https://127.0.0.1:8443/#new?cwd=%2Fa%20b&m=M1|"
    # with the relay the token is not on curl's command line (stdin, -K -)
    s = finder.script("https://x/", "M1", app, ("https://127.0.0.1:8443/api/ui/new", ["--cacert", "/c a.crt"], "SECRET"))
    curl = [ln for ln in s.splitlines() if ln.startswith("/usr/bin/curl")][0]
    assert "SECRET" not in curl and "-K -" in curl and "'/c a.crt'" in curl and 'header = "Authorization: Bearer SECRET"' in s


def test_local_cert(tmp_path):
    import socket
    import ssl
    import subprocess
    import threading
    from cryptography import x509
    import localcert
    crt, key, ca = localcert.write(str(tmp_path))
    assert oct(os.stat(key).st_mode & 0o777) == "0o600"
    assert sorted(os.listdir(tmp_path / "certs")) == ["local-ca.crt", "local.crt", "local.key"]   # no CA key anywhere
    cacert = x509.load_pem_x509_certificate(open(ca, "rb").read())
    nc = cacert.extensions.get_extension_for_class(x509.NameConstraints)
    assert nc.critical and cacert.extensions.get_extension_for_class(x509.BasicConstraints).value.path_length == 0
    assert {str(s.value) for s in nc.value.permitted_subtrees} == {"127.0.0.1/32", "::1/128", "localhost"}
    leaf = x509.load_pem_x509_certificate(open(crt, "rb").read())
    days = (leaf.not_valid_after_utc - leaf.not_valid_before_utc).days
    assert days <= 825
    # a real TLS handshake: the CA as the only trust anchor, 127.0.0.1 verified, another name refused
    sctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    sctx.load_cert_chain(crt, key)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]

    def serve():
        for _ in range(2):
            c, _ = srv.accept()
            try:
                with sctx.wrap_socket(c, server_side=True) as s:
                    s.recv(1)
            except (ssl.SSLError, OSError):
                pass
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    cctx = ssl.create_default_context(cafile=ca)
    with socket.create_connection(("127.0.0.1", port)) as raw:
        with cctx.wrap_socket(raw, server_hostname="127.0.0.1") as s:
            assert s.getpeercert()["subjectAltName"][0] == ("IP Address", "127.0.0.1")
    with pytest.raises(ssl.SSLCertVerificationError):
        with socket.create_connection(("127.0.0.1", port)) as raw:
            cctx.wrap_socket(raw, server_hostname="example.com")
    t.join(5)
    srv.close()
    if sys.platform == "darwin":
        # macOS's own evaluation (what Safari / Chrome use), with the CA as root for this check only
        one = tmp_path / "leaf.pem"
        one.write_bytes(leaf.public_bytes(__import__("cryptography").hazmat.primitives.serialization.Encoding.PEM))
        ok = subprocess.run(["security", "verify-cert", "-c", str(one), "-r", ca, "-p", "ssl", "-s", "127.0.0.1", "-L", "-q"],
                            capture_output=True)
        assert ok.returncode == 0, ok.stderr
        bad = subprocess.run(["security", "verify-cert", "-c", str(one), "-r", ca, "-p", "ssl", "-s", "example.com", "-L", "-q"],
                             capture_output=True)
        assert bad.returncode != 0


def test_vendor_served_gzipped(hub_mod, tmp_path, monkeypatch):
    """mermaid (5 MB) goes out gzipped to browsers that accept it; the original stays for those that don't."""
    import gzip
    from aiohttp.test_utils import TestClient, TestServer
    C, server = hub_mod
    web = tmp_path / "web"
    (web / "vendor" / "mermaid").mkdir(parents=True)
    big = ("var mermaid = %r;\n" % ("x" * 64) * 4000).encode()
    (web / "vendor" / "mermaid" / "mermaid.min.js").write_bytes(big)
    (web / "vendor" / "small.js").write_bytes(b"var a = 1;\n")
    monkeypatch.setattr(server, "WEB", str(web))
    server.precompress_vendor()
    gz = web / "vendor" / "mermaid" / "mermaid.min.js.gz"
    assert gz.exists() and gzip.decompress(gz.read_bytes()) == big and gz.stat().st_size < len(big) / 10
    assert not (web / "vendor" / "small.js.gz").exists()                    # small files: not worth it
    t = gz.stat().st_mtime
    server.precompress_vendor()
    assert gz.stat().st_mtime == t                                           # up to date: left alone

    async def run():
        hub = server.Hub(C.load())
        client = TestClient(TestServer(hub.app()))
        await client.start_server()
        try:
            r = await client.get("/static/vendor/mermaid/mermaid.min.js", headers={"Accept-Encoding": "gzip"}, auto_decompress=False)
            assert r.status == 200 and r.headers.get("Content-Encoding") == "gzip"
            assert "javascript" in r.headers.get("Content-Type", "") and gzip.decompress(await r.read()) == big
            r = await client.get("/static/vendor/mermaid/mermaid.min.js", headers={"Accept-Encoding": "identity"})
            assert r.status == 200 and r.headers.get("Content-Encoding") is None and await r.read() == big
            r = await client.get("/static/vendor/mermaid/mermaid.min.js.gz/../mermaid.min.js")
            assert r.status in (200, 404)                                    # (normalised; never outside web/)
        finally:
            await client.close()
    asyncio.run(run())


def test_site_context_loopback(hub_mod):
    _, server = hub_mod
    ts, local = object(), object()
    assert server.site_context("127.0.0.1", ts, local) is local
    assert server.site_context("::1", ts, local) is local and server.site_context("localhost", ts, local) is local
    assert server.site_context("100.96.1.2", ts, local) is ts
    assert server.site_context("127.0.0.1", ts, None) is ts
    assert server.site_context("127.0.0.1", None, local) is None       # no TLS elsewhere: keep one scheme


def test_ui_new_through_the_hub(hub_mod, tmp_path, monkeypatch):
    """`gli new` / the Quick Action: the generated script (real curl) asks the hub, the hub hands the form to the
    Glimpse window on this computer that is on screen, never to other devices; tokens and origin are checked."""
    import hashlib
    from aiohttp.test_utils import TestClient, TestServer
    import finder
    C, server = hub_mod
    if sys.platform != "darwin":
        pytest.skip("the Quick Action script uses macOS tools")

    async def run():
        hub = server.Hub(C.load())
        client = TestClient(TestServer(hub.app()))
        await client.start_server()
        try:
            r = await client.post("/api/login", json={"pair": hub.auth.new_pair()})
            sess = {"Authorization": "Bearer " + (await r.json())["session"]}
            win = await client.ws_connect("/ws", headers=sess)
            assert json.loads((await win.receive()).data)["t"] == "state"
            await win.send_json({"op": "visible", "v": True})
            await asyncio.sleep(0.05)
            url = "http://127.0.0.1:%d/api/ui/new" % client.port
            tok = "finder-token-abc"
            C.write_secret(os.path.join(C.DATA, "finder.json"), json.dumps({"token_sha256": hashlib.sha256(tok.encode()).hexdigest()}))
            sh = tmp_path / "qa.sh"
            sh.write_text(finder.script("https://x/", "M1", "/Apps/Glimpse.app", (url, [], tok))
                          .replace("exec /usr/bin/open", "echo OPEN"))
            d = "/Users/x/我的 项目 & \"q\" $(touch boom)"
            p = await asyncio.create_subprocess_exec("/bin/sh", str(sh), d, cwd=str(tmp_path),
                                                     stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await p.communicate()
            assert p.returncode == 0, err
            assert out.decode().strip() == "OPEN -a /Apps/Glimpse.app" and not (tmp_path / "boom").exists()
            got = json.loads((await win.receive(timeout=3)).data)
            assert got == {"t": "ui_new", "cwd": d, "m": "M1"}
            # the CLI token works too; a session token, a wrong token, no token, form posts, another computer: no
            r = await client.post("/api/ui/new", json={"cwd": "/a"}, headers={"Authorization": "Bearer " + hub.token})
            assert r.status == 200 and (await r.json())["delivered"] is True
            assert json.loads((await win.receive(timeout=3)).data)["cwd"] == "/a"
            for h in (sess, {"Authorization": "Bearer nope"}, {}):
                assert (await client.post("/api/ui/new", json={"cwd": "/a"}, headers=h)).status == 401
            r = await client.post("/api/ui/new", data="cwd=/a", headers={"Authorization": "Bearer " + tok,
                                                                          "Content-Type": "text/plain"})
            assert r.status == 415
            assert (await client.post("/api/ui/new", json={"cwd": ""}, headers={"Authorization": "Bearer " + tok})).status == 400
            with monkeypatch.context() as mp:                # as if from another computer on the tailnet
                mp.setattr(server.Hub, "client_ip", staticmethod(lambda req: "100.64.1.2"))
                r = await client.post("/api/ui/new", json={"cwd": "/a"}, headers={"Authorization": "Bearer " + tok})
                assert r.status == 401
            # an open window that is not in front (Finder is) still gets it: the caller brings it up afterwards
            await win.send_json({"op": "visible", "v": False})
            await asyncio.sleep(0.05)
            r = await client.post("/api/ui/new", json={"cwd": "/bg"}, headers={"Authorization": "Bearer " + tok})
            assert (await r.json())["delivered"] is True
            assert json.loads((await win.receive(timeout=3)).data)["cwd"] == "/bg"
            # no Glimpse window on this computer (only a phone elsewhere, and `gli attach` with the CLI token): held,
            # then handed to the window that opens next
            for ph in hub.phones:
                ph.local = False                       # as if it were a phone over Tailscale
            cli = await client.ws_connect("/ws", headers={"Authorization": "Bearer " + hub.token})
            assert json.loads((await cli.receive()).data)["hub"]["web"] == server.WEB_BUILD
            r = await client.post("/api/ui/new", json={"cwd": "/held"}, headers={"Authorization": "Bearer " + tok})
            assert (await r.json())["delivered"] is False and hub.ui_pending is not None
            win2 = await client.ws_connect("/ws", headers=sess)
            assert json.loads((await win2.receive(timeout=3)).data)["t"] == "state"
            assert json.loads((await win2.receive(timeout=3)).data) == {"t": "ui_new", "cwd": "/held", "m": None}
            assert hub.ui_pending is None
            for w in (win, cli):                       # neither the phone nor the CLI got it
                while True:
                    try:
                        m = json.loads((await w.receive(timeout=0.3)).data)
                    except asyncio.TimeoutError:
                        break
                    assert m["t"] != "ui_new" or m["cwd"] != "/held"
            for w in (win, win2, cli):
                await w.close()
        finally:
            await client.close()
    asyncio.run(run())


def test_finder_finds_the_installed_web_app(tmp_path):
    import plistlib
    import finder

    def app(where, name, url, **extra):
        c = tmp_path / where / (name + ".app") / "Contents"
        c.mkdir(parents=True)
        with open(c / "Info.plist", "wb") as f:
            plistlib.dump(dict({"CFBundleName": name, "CrAppModeShortcutURL": url}, **extra), f)
        return str(c.parent)
    a = app("Chrome Apps.localized", "Glimpse", "https://127.0.0.1:8443/?pair=old")   # installed from a login link
    app("Chrome Apps.localized", "Other", "https://127.0.0.1:9000/")
    app("Chrome Apps.localized", "Elsewhere", "https://example.com:8443/")
    app(".", "Plain", "")
    roots = [str(tmp_path)]
    assert finder.desktop_app(8443, {"127.0.0.1", "mac.ts.net"}, roots) == (a, "https://127.0.0.1:8443/")
    assert finder.desktop_app(8443, {"mac.ts.net"}, roots) is None             # another host: not ours
    assert finder.desktop_app(7000, {"127.0.0.1"}, roots) is None
    assert finder.desktop_app(8443, {"127.0.0.1"}, [str(tmp_path / "missing")]) is None


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


def test_fs_op_validation_and_routing(hub_mod):
    C, server = hub_mod

    async def run():
        hub = server.Hub(C.load())
        m = FakeMachine()
        hub.machines[m.id] = m
        hub.order.append(m.id)
        p, other = FakePhone(), FakePhone()
        hub.phones.update([p, other])
        await hub.phone_op(p, {"op": "fs", "m": "M1", "sid": "s1", "action": "read", "path": "src/a.py"})
        w = m.written[-1]
        assert w["op"] == "fs" and w["action"] == "read" and w["path"] == "src/a.py" and w["force"] is False
        await hub.on_machine_msg(m, {"t": "fs", "req": w["req"], "sid": "s1", "action": "read", "path": "src/a.py",
                                     "kind": "text", "text": "x"})
        assert p.sent[-1]["t"] == "fs" and p.sent[-1]["text"] == "x" and p.sent[-1]["m"] == "M1" and other.sent == []
        # an agent without file browsing answers "unknown op": shown in place as "needs update"
        await hub.phone_op(p, {"op": "fs", "m": "M1", "sid": "s1", "action": "list", "path": ""})
        await hub.on_machine_msg(m, {"t": "error", "req": m.written[-1]["req"], "msg": "unknown op 'fs'"})
        assert p.sent[-1]["t"] == "fs" and p.sent[-1]["action"] == "list" and "需要更新" in p.sent[-1]["error"]
        # a save carries the text and the version it was edited from; new file / folder
        await hub.phone_op(p, {"op": "fs", "m": "M1", "sid": "s1", "action": "write", "path": "a.py", "text": "x = 1\n",
                               "sig": "0123456789abcdef"})
        w = m.written[-1]
        assert w["action"] == "write" and w["text"] == "x = 1\n" and w["sig"] == "0123456789abcdef" and w["force"] is False
        await hub.phone_op(p, {"op": "fs", "m": "M1", "sid": "s1", "action": "create", "path": "new.md", "text": ""})
        assert m.written[-1]["action"] == "create" and m.written[-1]["text"] == "" and m.written[-1]["sig"] is None
        await hub.phone_op(p, {"op": "fs", "m": "M1", "sid": "s1", "action": "mkdir", "path": "d"})
        assert m.written[-1]["action"] == "mkdir" and "text" not in m.written[-1]
        n = len(m.written)
        for bad in ({"action": "write", "path": "a"}, {"action": "write", "path": "a", "text": "x"},                 # no version
                    {"action": "write", "path": "a", "text": "x", "sig": "../../x"}, {"action": "create", "path": "a"},
                    {"action": "write", "path": "a", "text": "x" * (server.FS_TEXT_LIMIT + 1), "sig": "0" * 16},
                    {"action": "delete", "path": "a"}, {"action": "read", "path": "x" * 5000}, {"action": "read", "path": 3}):
            await hub.phone_op(p, dict({"op": "fs", "m": "M1", "sid": "s1"}, **bad))
            assert p.sent[-1]["t"] == "error"
        await hub.phone_op(p, {"op": "fs", "m": "M1", "action": "list", "path": ""})            # no session
        assert p.sent[-1]["t"] == "error" and len(m.written) == n
    asyncio.run(run())


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


def test_login_again_keeps_the_device(hub_mod):
    """An installed app opens its start URL (a fresh pairing code) on every launch: an already logged-in device
    keeps its session and stays one entry in the device list; the code is used up all the same."""
    from aiohttp.test_utils import TestClient, TestServer
    C, server = hub_mod

    async def run():
        hub = server.Hub(C.load())
        client = TestClient(TestServer(hub.app()))
        await client.start_server()
        try:
            r = await client.post("/api/login", json={"pair": hub.auth.new_pair()})
            tok = (await r.json())["session"]
            auth = {"Authorization": "Bearer " + tok}
            code = hub.auth.new_pair()
            r = await client.post("/api/login", json={"pair": code}, headers=auth)
            assert r.status == 200 and (await r.json())["session"] == tok
            r = await client.get("/api/devices", headers=auth)
            assert len((await r.json())["devices"]) == 1
            assert (await client.post("/api/login", json={"pair": code})).status == 401      # used up
            # a revoked session is no session: logging in makes a new device
            hub.auth.revoke_all()
            r = await client.post("/api/login", json={"pair": hub.auth.new_pair()}, headers=auth)
            assert r.status == 200 and (await r.json())["session"] != tok
        finally:
            await client.close()
    asyncio.run(run())


def test_hub_language(hub_mod):
    """op lang: validated, saved in config.json, sent to the machines (now and on every reconnect), in state;
    the hub's own messages follow it."""
    C, server = hub_mod

    class FakeMachine:
        def __init__(self):
            self.id, self.sent, self.online = "M1", [], True

        def write(self, obj):
            self.sent.append(obj)

    class P:
        sent = []

        def send(self, o):
            self.sent.append(o)

    async def run():
        hub = server.Hub(C.load())
        assert hub.lang() == "zh" and hub.state()["hub"]["lang"] == "zh" and server.tr("机器离线") == "机器离线"
        fm = FakeMachine()
        hub.machines = {"M1": fm}
        p = P()
        for bad in ("fr", None, 3):
            await hub.phone_op(p, {"op": "lang", "lang": bad})
        assert hub.lang() == "zh" and not fm.sent
        await hub.phone_op(p, {"op": "lang", "lang": "en"})
        assert hub.lang() == "en" and json.load(open(C.CONFIG))["hub"]["language"] == "en"
        assert fm.sent == [{"op": "lang", "lang": "en"}] and hub.state()["hub"]["lang"] == "en"
        assert server.tr("机器离线") == "Machine offline" and server.tr("未知机器 %s", "M9") == "Unknown machine M9"
        assert not [x for x in p.sent if x.get("t") == "error"]            # not mistaken for a machine op
        await hub.on_machine_msg(fm, {"t": "resync"})                       # a reconnect gets it again
        assert fm.sent[-1] == {"op": "lang", "lang": "en"}
        await hub.phone_op(p, {"op": "lang", "lang": "zh"})
        assert server.tr("机器离线") == "机器离线"
    asyncio.run(run())
    server.HUB_LANG[0] = "zh"


VAULT_LINKER = os.path.expanduser("~/Documents/spiritai/vault-linker/vault-linker")


@pytest.mark.skipif(not os.access(VAULT_LINKER, os.X_OK), reason="vault-linker not checked out next to Glimpse")
def test_project_workflow(hub_mod, tmp_path, monkeypatch):
    """project_workflow: project init on the vault machine through the agent's op ext (the real vault-linker and its
    extension.json, a throwaway vault); refused on other machines and without a vault; the vault extension goes to
    its machine only; Settings → 扩展 adds (from the folder's manifest), turns on, configures and removes it."""
    import subprocess
    C, server = hub_mod
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent"))
    import glimpse_agent as A
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@example.com"), ("GIT_COMMITTER_NAME", "t"),
                 ("GIT_COMMITTER_EMAIL", "t@example.com")):
        monkeypatch.setenv(k, v)
    vault = tmp_path / "vault"
    subprocess.run([VAULT_LINKER, "vault", "init", str(vault)], check=True, capture_output=True)
    proj = tmp_path / "newproj"
    proj.mkdir()

    class Agent:
        """A machine whose calls run the real agent Extensions (synchronously)."""
        def __init__(self, mid, local=False):
            self.id, self.sent, self.cfg = mid, [], {"type": "local" if local else "ssh"}
            self.v = A.Extensions(type("D", (), {"bg_done": [], "background": staticmethod(lambda f: f())})())

        async def call(self, obj, timeout=None):
            self.sent.append(obj)
            if obj["op"] == "ext_config":
                self.v.configure(obj["cfg"])
                return {"t": "ext_status", "ext": self.v.status()}
            if obj["op"] == "ext_probe":
                folder, man = A.ext_load(obj["dir"])
                return {"t": "ext_probe", "dir": folder, "man": man}
            if obj["op"] == "ext":
                out = {}
                self.v.run(obj["id"], obj["args"], lambda r, e: out.update({"t": "error", "msg": e} if e else {"t": "ext", "data": r}),
                           stdin=obj.get("input"), timeout=obj.get("timeout", 60))
                while self.v.d.bg_done:                    # refusals answer at once, CLI runs come back here
                    fn, res, err = self.v.d.bg_done.pop()
                    fn(res, err)
                return out
            return {"t": "error", "msg": "unknown op"}

    class P:
        def __init__(self):
            self.out = []

        def send(self, o):
            self.out.append(o)

    async def run():
        hub = server.Hub(C.load())
        local, other = Agent("M1", True), Agent("M9")
        hub.machines = {"M1": local, "M9": other}
        hub.cfg["machines"] = [{"id": "M1", "type": "local"}, {"id": "M9", "type": "ssh", "host": "x"}]
        assert server.local_machine_id(hub.cfg) == "M1"
        p = P()
        with pytest.raises(ValueError):                      # no vault configured yet
            await hub.project_workflow(p, local, {"cwd": str(proj)})
        hub.cfg["extensions"] = []
        await hub.ext_op(p, "ext_add", {"m": "M1", "dir": os.path.dirname(VAULT_LINKER)})
        e = hub.ext_entry("vault-linker")
        assert e and e["enabled"] is False and e["machine"] == "" and "path" in e["schema"] and p.out[-1]["t"] == "ext_ok"
        with pytest.raises(ValueError):                      # added, but still off
            await hub.project_workflow(p, local, {"cwd": str(proj)})
        with pytest.raises(ValueError):                      # the same one twice
            await hub.ext_op(p, "ext_add", {"m": "M1", "dir": os.path.dirname(VAULT_LINKER)})
        await hub.ext_op(p, "ext_update", {"id": "vault-linker", "enabled": True, "settings": {"path": str(vault), "nope": 1}})
        assert e["settings"]["path"] == str(vault) and "nope" not in e["settings"]
        await hub.ext_config(other)
        assert local.sent[-1]["cfg"]["vault-linker"]["settings"]["path"] == str(vault) and other.sent[-1]["cfg"] == {}
        assert local.vault and not getattr(other, "vault", False) and hub.vault_on()
        v = hub.ext_view()[0]
        assert v["id"] == "vault-linker" and v["on"] and v["machine"] == "M1" and v["version"]
        with pytest.raises(ValueError):                      # not the vault machine
            await hub.project_workflow(p, other, {"cwd": str(proj)})
        await hub.project_workflow(p, local, {"cwd": str(proj), "sid": "abc123"})
        r = p.out[-1]
        assert r["t"] == "project_workflow" and r["init"]["id"] == "newproj" and r["init"]["git_init"]
        assert (proj / ".vault-linker.json").exists() and (vault / "projects" / "newproj" / "project.md").exists()
        assert A.ext_marker(str(proj), [".vault-linker.json"]) == ("newproj", os.path.realpath(str(proj)))
        with pytest.raises(ValueError) as e:                  # vault-linker's own refusals come through
            await hub.project_workflow(p, local, {"cwd": str(tmp_path / "missing")})
        assert "no such folder" in str(e.value)
        # the vault view: browse, read, search, images, a person's edit (guarded by mtime), no paths taken as flags
        os.makedirs(vault / "notes" / "t")
        (vault / "notes" / "t" / "a.md").write_text("# A\n\nflow matching\n")
        (vault / "notes" / "t" / "x.png").write_bytes(b"\x89PNG\r\n\x1a\nxx")
        async def view(**kw):
            await hub.vault_view(p, dict(kw, op="vault_view", key="k"))
            return p.out[-1]
        r = await view(action="overview")
        assert r["t"] == "vault_view" and r["key"] == "k" and "notes/t/a.md" in r["data"]["paths"]
        assert [x["id"] for x in r["data"]["projects"]] == ["newproj"]
        assert (await view(action="ls", folder="notes"))["data"]["dirs"][0]["name"] == "t"
        rd = (await view(action="read", path="notes/t/a.md"))["data"]
        assert rd["body"].startswith("# A")
        assert (await view(action="search", q="flow"))["data"]["hits"][0]["path"] == "notes/t/a.md"
        assert (await view(action="search", q="--help"))["t"] == "vault_view"         # text, not a flag
        assert (await view(action="asset", path="notes/t/x.png"))["data"]["mime"] == "image/png"
        assert (await view(action="project", id="newproj"))["data"]["page"]["meta"]["type"] == "project"
        w = await view(action="write", path="notes/t/a.md", text="# A\n\n改过了\n", expect=rd["mtime"])
        assert w["t"] == "vault_view" and "改过了" in (vault / "notes" / "t" / "a.md").read_text()
        w = await view(action="write", path="notes/t/a.md", text="stale", expect=rd["mtime"])
        assert w["t"] == "error" and "changed" in w["msg"] and "改过了" in (vault / "notes" / "t" / "a.md").read_text()
        for bad in ({"action": "read", "path": "../../etc/passwd"}, {"action": "asset", "path": "-h"},
                    {"action": "nope"}, {"action": "read"}, {"action": "write", "path": "notes/t/x.png", "text": "x"}):
            assert (await view(**bad))["t"] == "error", bad
        assert (await view(action="sync"))["data"]["pushed"] is False
        # the agent refuses first words the manifest does not allow, whatever the hub sends
        with pytest.raises(ValueError):
            await hub.ext_cli("vault-linker", ["mcp"])
        await hub.ext_op(p, "ext_update", {"id": "vault-linker", "enabled": False})
        assert not hub.vault_on() and local.sent[-1]["cfg"] == {}
        await hub.ext_op(p, "ext_remove", {"id": "vault-linker"})
        assert hub.ext_entries() == []
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


def test_keybalance_percent_and_rates():
    import keybalance as KB
    # the 7-day window's percentage, projected an hour ahead at the measured rate
    assert KB.week_percent(_wk(600, 60, 7)) == 10.0
    assert abs(KB.week_percent(_wk(600, 60, 7), rate=1e-3) - 100 * (60 + 3.6) / 600) < 1e-9
    assert abs(KB.week_percent(_wk(600, 60, 7), rate=1e-3, extra=-1e-3) - 10.0) < 1e-9
    assert KB.week_percent({"ts": 1, "windows": [{"window_sec": 18000, "limit": 1, "used": 0}]}) is None   # 5 h only
    assert KB.week_percent({"ts": 1, "error": "x"}) is None
    t = KB.RateTracker()
    t.observe("a", _wk(100, 10, 7, ts=0))
    t.observe("a", _wk(100, 13, 7, ts=300))            # 3 per 300 s
    assert abs(t.rate["a"] - 0.01) < 1e-12
    t.observe("a", _wk(100, 1, 7, ts=600))             # window reset: rate starts over
    assert "a" not in t.rate


def test_keybalance_pick_lowest_percent():
    import keybalance as KB
    now = 1000.0
    # the percentage decides, not the absolute amount: k2 used more (300 > 80) but is at 30 % of its week, k1 at 80 %
    u = {"k1": _wk(100, 80, 3, now), "k2": _wk(1000, 300, 3, now)}
    rates = {"k1": 0.001, "k2": 0.001}
    assert KB.pick_key(["k1", "k2"], u, in_use={"k1": 1, "k2": 1}, rates=rates, now=now) == "k2"
    # same percentage now, but k1 burns fast: in an hour it is ahead, so the new session goes to k2
    u2 = {"k1": _wk(600, 60, 3, now), "k2": _wk(600, 60, 3, now)}
    assert KB.pick_key(["k1", "k2"], u2, in_use={"k1": 1}, rates={"k1": 5e-3}, now=now) == "k2"
    # equal: fewer sessions, then config order; a key without usage data comes after the ones with
    assert KB.pick_key(["k1", "k2"], u2, in_use={"k1": 1}, now=now) == "k2"
    assert KB.pick_key(["k1", "k2"], u2, now=now) == "k1"
    assert KB.pick_key(["x", "k1"], dict(u2, x={}), now=now) == "k1"
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


def test_keybalance_rebalance_evens_out_percentages():
    import keybalance as KB
    now = 1000.0
    u = {"k1": _wk(100, 90, 3, now), "k2": _wk(100, 10, 3, now)}
    rates = {"k1": 3e-4, "k2": 1e-4}
    assert KB.should_rebalance("k1", ["k1", "k2"], u, in_use={"k1": 3, "k2": 1}, rates=rates, now=now) == "k2"
    # no measured rates yet: the percentages alone are far apart, so it moves too
    assert KB.should_rebalance("k1", ["k1", "k2"], u, in_use={"k1": 3, "k2": 1}, now=now) == "k2"
    # close already (less than REBALANCE_POINTS apart): stay; at the threshold: move; never to a key that is ahead
    for used2, want in ((47, None), (45, "k2"), (60, None)):
        u2 = {"k1": _wk(100, 50, 3, now), "k2": _wk(100, used2, 3, now)}
        assert KB.should_rebalance("k1", ["k1", "k2"], u2, in_use={"k1": 1, "k2": 1}, now=now) == want, used2


def test_keybalance_rebalance_with_todays_numbers():
    # seen 2026-10-19 10:25: session fd49b2, alone on key2 (1.2 % of its week), was moved to key1 (10.2 %, the most
    # used key). With percentages: from key2 it stays (all keys within a few points), and once it is on key1 it
    # moves to an unused key (key3: 0 %, no sessions, first of those in config order), not to key10 (6.2 %).
    import keybalance as KB
    now = 1000.0
    used = {"key1": (600, 60.92), "key2": (600, 6.97), "key3": (600, 0), "key5": (600, 0), "key7": (600, 0),
            "key8": (600, 0), "key9": (600, 7.63), "key10": (900, 56.19), "public": (600, 0)}
    u = {n: _wk(lim, x, 6.5, now) for n, (lim, x) in used.items()}
    u["key4"] = u["key6"] = {"ts": now, "error": "Key 无效或已停用"}
    names = ["key1", "key2", "key3", "key4", "key5", "key6", "key7", "key8", "key9", "key10", "public"]
    before = {"key1": 1, "key2": 1, "key5": 2}                  # key5: two idle codex sessions (rate 0)
    assert KB.should_rebalance("key2", names, u, in_use=before, rates={"key1": 1e-4, "key2": 1e-3, "key5": 0.0}, now=now) is None
    now_ = {"key1": 2, "key5": 2}                               # fd49b2 and an idle codex session on key1
    assert KB.should_rebalance("key1", names, u, in_use=now_, rates={"key1": 2e-3, "key5": 0.0}, now=now) == "key3"
    assert KB.pick_key(names, u, in_use=now_, rates={"key1": 2e-3, "key5": 0.0}, now=now) == "key3"


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
        return {"t": "restarted", "hot": self.hot and not obj.get("cold")}

    def write(self, obj):
        self.writes.append(obj)


def test_key_failure_after_a_live_swap_is_the_old_keys(hub_mod, monkeypatch):
    """claude keeps a message's key for all its retries: after a live swap k1 -> k2 the next message still runs
    on k1. Its failure must not burn k2 (and so on through every key): restart on k2, mark k1."""
    import asyncio
    C, server = hub_mod
    cfg = C.load()
    cfg["keys"]["profiles"] = [{"name": "k1", "value": "v1"}, {"name": "k2", "value": "v2"}, {"name": "k3", "value": "v3"}]
    h = server.Hub(cfg)
    m = _M()
    m.sessions["s1"].update(kind="claude", status="idle")
    sleeps = []

    async def no_sleep(t):
        sleeps.append(t)
    monkeypatch.setattr(server.asyncio, "sleep", no_sleep)
    ev = {"kind": "error", "title": "出错停止", "body": "API Error: 500 Key余额不足"}

    async def run(coro):
        out = await coro
        await asyncio.gather(*[t for t in asyncio.all_tasks() if t is not asyncio.current_task()])
        return out
    # a live swap k1 -> k2 (balancing, or from the phone while the session was fine)
    assert asyncio.run(run(h.switch_key(m, "s1", "claude", "k2"))) is True
    m.sessions["s1"]["key"] = "k2"
    # the next message fails with k1's no-balance: works even with balancing off
    out = asyncio.run(run(h.balance_on_event(m, "s1", ev)))
    assert out["kind"] == "keyswap" and "k1" in out["title"] and "k2" in out["title"]
    assert m.calls[-1]["op"] == "restart" and m.calls[-1]["key"] == "k2" and m.calls[-1]["cold"] is True
    assert "k1" in h.key_exhausted and "k2" not in h.key_exhausted
    assert m.writes[-1] == {"op": "text", "sid": "s1", "text": "继续"}
    # a second failure right after that restart is k2's own (balancing off: shown as it is)
    assert asyncio.run(run(h.balance_on_event(m, "s1", ev))) is None and "k2" not in h.key_exhausted
    # a message that went through clears it: a later failure is the current key's
    asyncio.run(run(h.switch_key(m, "s1", "claude", "k3")))
    m.sessions["s1"]["key"] = "k3"
    asyncio.run(run(h.on_event(m, "s1", {"kind": "done", "title": "已完成本轮", "body": "x"})))
    n = len(m.calls)
    assert asyncio.run(run(h.balance_on_event(m, "s1", ev))) is None and len(m.calls) == n


def test_key_switch_does_not_block_the_machine_read_loop(hub_mod, monkeypatch):
    """The no-balance error arrives through the machine's read loop; the switch waits for the agent's
    "restarted", which comes through that same loop. Handled inline it deadlocked until the 30 s timeout
    ("切换到 B 失败", no 继续). Real Machine: messages are fed one at a time like session_once does."""
    import asyncio
    import machines as MM
    C, server = hub_mod
    cfg = C.load()
    cfg["keys"]["profiles"] = [{"name": "A", "value": "a"}, {"name": "B", "value": "b"}]
    cfg["keys"]["balance"] = {"mode": "on_exhausted", "exclude": []}
    h = server.Hub(cfg)

    async def run():
        m = MM.Machine({"id": "M1", "type": "local"}, h.on_machine_msg, h.on_machine_state)
        h.machines["M1"] = m
        m.online = True
        m.sessions = {"s1": {"sid": "s1", "kind": "claude", "key": "A", "status": "error", "hot_key": True}}
        q, writes = asyncio.Queue(), []

        def write(obj):
            writes.append(obj)
            if obj.get("op") == "restart":           # the agent answers once it restarted
                q.put_nowait({"t": "restarted", "req": obj["req"], "sid": "s1", "hot": False})
        m.write = write
        real_sleep = asyncio.sleep
        monkeypatch.setattr(server.asyncio, "sleep", lambda t: real_sleep(0))
        q.put_nowait({"t": "event", "sid": "s1", "ev": {"kind": "error", "title": "出错停止", "body": "API Error: 500 Key余额不足"}})

        async def read_loop():                        # session_once: one message at a time
            while True:
                await m.handle(await q.get())
        loop = asyncio.ensure_future(read_loop())
        t0 = asyncio.get_running_loop().time()
        while not any(w.get("op") == "text" for w in writes) and asyncio.get_running_loop().time() - t0 < 5:
            await real_sleep(0.01)
        loop.cancel()
        return writes, asyncio.get_running_loop().time() - t0
    writes, secs = asyncio.run(run())
    restart = [w for w in writes if w.get("op") == "restart"]
    assert restart and restart[0]["key"] == "B" and restart[0]["cold"] is True
    assert {"op": "text", "sid": "s1", "text": "继续"} in writes and secs < 3      # not after a 30 s timeout


def test_manual_key_change_restarts_a_failed_session(hub_mod):
    import asyncio
    C, server = hub_mod
    cfg = C.load()
    cfg["keys"]["profiles"] = [{"name": "k1", "value": "v1"}, {"name": "k2", "value": "v2"}]
    h = server.Hub(cfg)
    m = _M()
    m.sessions["s1"].update(kind="claude")
    p = FakePhone()
    for status, cold in (("error", True), ("idle", False)):
        m.sessions["s1"]["status"] = status
        asyncio.run(h.restart(p, m, {"sid": "s1", "key": "k2"}))
        assert m.calls[-1]["cold"] is cold and p.sent[-1]["t"] == "restarted" and p.sent[-1]["hot"] is (not cold)
    asyncio.run(h.restart(p, m, {"sid": "s1"}))                          # plain restart, no key change
    assert m.calls[-1]["cold"] is False and m.calls[-1]["env"] is None


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
    assert m.calls[-1]["cold"] is True                                  # a failed key: restart, not a live swap
    assert m.writes[-1] == {"op": "text", "sid": "s1", "text": "继续"} and sleeps[0] == 3      # restarted: wait for the TUI
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


def test_schedule_cron_and_due():
    import datetime as D
    import schedule as SC
    c = SC.Cron("0 8 * * *")
    t = D.datetime(2026, 10, 22, 7, 59)
    assert c.next(t) == D.datetime(2026, 10, 22, 8, 0) and c.prev(t) == D.datetime(2026, 10, 21, 8, 0)
    assert c.prev(D.datetime(2026, 10, 22, 8, 0)) == D.datetime(2026, 10, 22, 8, 0)
    wk = SC.Cron("30 9 * * 1-5")                         # weekdays only: Friday -> Monday
    assert wk.next(D.datetime(2026, 10, 23, 10, 0)) == D.datetime(2026, 10, 26, 9, 30)
    assert SC.Cron("*/15 * * * *").next(D.datetime(2026, 1, 1, 0, 7)) == D.datetime(2026, 1, 1, 0, 15)
    assert SC.Cron("0 0 * * 7").dow == {0}               # 7 = Sunday too
    assert SC.Cron("0 8 1 * 1").day_ok(D.date(2026, 10, 26))      # dom and dow both set: either
    for bad in ("0 8 * *", "60 8 * * *", "0 24 * * *", "0 8 0 * *", "x 8 * * *", "0 8 * * */0", "5-1 * * * *"):
        with pytest.raises(ValueError):
            SC.Cron(bad)
    # due: the 8:00 slot after the last run; a missed one runs on wake within a day; older ones are skipped
    at = lambda *a: D.datetime(*a).timestamp()
    assert SC.due(c, at(2026, 10, 21, 8, 0), at(2026, 10, 22, 7, 0)) is None          # today's not yet
    assert SC.due(c, at(2026, 10, 21, 8, 0), at(2026, 10, 22, 8, 0, 30)) == at(2026, 10, 22, 8, 0)
    assert SC.due(c, at(2026, 10, 22, 8, 0), at(2026, 10, 22, 9, 0)) is None          # already ran
    assert SC.due(c, at(2026, 10, 20, 8, 0), at(2026, 10, 22, 7, 30)) == at(2026, 10, 21, 8, 0)   # slept: catch up
    assert SC.due(c, at(2026, 10, 18, 8, 0), at(2026, 10, 22, 7, 30), catch_up=3600) == -at(2026, 10, 21, 8, 0)
    ok = SC.check({"name": " 论文日报 ", "cron": "0  8 * * *", "machine": "M1", "kind": "codex", "cwd": "~/x",
                   "prompt": "run", "model": "gpt-5", "junk": 1}, {"M1"})
    assert ok == {"name": "论文日报", "cron": "0 8 * * *", "machine": "M1", "kind": "codex", "cwd": "~/x",
                  "prompt": "run", "model": "gpt-5", "enabled": True}
    base = {"name": "a", "cron": "0 8 * * *", "machine": "M1", "kind": "claude", "cwd": "~", "prompt": "p"}
    for k, v in (("cron", "bad"), ("machine", "M9"), ("kind", "shell"), ("cwd", ""), ("prompt", " "),
                 ("name", "x" * 61), ("model", "a b")):
        with pytest.raises(ValueError):
            SC.check(dict(base, **{k: v}), {"M1"})


def test_schedule_loop_runs_catches_up_and_waits(hub_mod):
    """sched_tick: a new task starts from now; the 8:00 slot starts a session with the task's prompt; a slot missed
    while asleep runs once on wake; not while the machine is offline or the last run is busy; a slot older than a
    day is skipped. sched_save / sched_run / sched_remove from the phone."""
    import datetime as D
    C, server = hub_mod
    at = lambda *a: D.datetime(*a).timestamp()

    class Mach:
        def __init__(self):
            self.id, self.name, self.online, self.cfg, self.sessions, self.calls = "M1", "Mac", True, {"type": "local"}, {}, []

        async def call(self, obj, timeout=None):
            self.calls.append(obj)
            sid = "s%d" % len(self.calls)
            self.sessions[sid] = {"sid": sid, "status": "running"}
            return {"t": "created", "s": {"sid": sid, "name": obj["name"]}}

    class P:
        def __init__(self):
            self.out = []

        def send(self, o):
            self.out.append(o)

    async def run():
        hub = server.Hub(C.load())
        m = Mach()
        hub.machines, hub.order = {"M1": m}, ["M1"]
        hub.cfg["machines"] = [{"id": "M1", "type": "local"}]
        pushed = []

        async def push_all(payload, skip=None):
            pushed.append(payload)
            return 1
        hub.push_all = push_all
        p = P()
        await hub.sched_op(p, "sched_save", {"task": {"name": "论文日报", "cron": "0 8 * * *", "machine": "M1",
                                                       "kind": "codex", "cwd": "~/radar", "prompt": "整理今天的论文"}})
        tid = p.out[-1]["id"]
        assert hub.sched_tasks()[0]["id"] == tid and hub.sched_view()[0]["next"]

        async def tick(ts):
            await hub.sched_tick(ts)
            for _ in range(3):
                await asyncio.sleep(0)
        hub.sched_runs = {}
        await tick(at(2026, 10, 22, 7, 0))                # first sight: from now on
        assert m.calls == []
        await tick(at(2026, 10, 22, 8, 0, 20))
        c = m.calls[-1]
        assert c["op"] == "create" and c["kind"] == "codex" and c["first_prompt"] == "整理今天的论文"
        assert c["cwd"] == "~/radar" and "论文日报" in c["name"]
        assert hub.sched_runs[tid]["sid"] == "s1" and pushed[-1]["url"] == "/#M1/s1"
        await tick(at(2026, 10, 22, 8, 1))                 # same slot: never twice
        assert len(m.calls) == 1
        # asleep through the 23rd's 8:00, awake at 9:30: once; but while the last run is busy, it waits
        await tick(at(2026, 10, 23, 9, 30))
        assert len(m.calls) == 1
        m.sessions["s1"]["status"] = "idle"
        m.online = False
        await tick(at(2026, 10, 23, 9, 31))                # offline: waits too
        assert len(m.calls) == 1
        m.online = True
        await tick(at(2026, 10, 23, 9, 32))
        assert len(m.calls) == 2 and hub.sched_runs[tid]["last"] == at(2026, 10, 23, 8, 0)
        m.sessions["s2"]["status"] = "idle"
        # asleep from the 24th; the newest missed slot (the 25th's) is 23 h old at 7:00 on the 26th: it still runs,
        # once (the 24th's is never run on its own)
        await tick(at(2026, 10, 26, 7, 0))
        assert len(m.calls) == 3 and hub.sched_runs[tid]["last"] == at(2026, 10, 25, 8, 0)
        m.sessions["s3"]["status"] = "idle"
        # the newest slot is what counts: at 9:00 on the 28th the 28th's 8:00 runs (the 26th's / 27th's are not
        # run on their own)
        await tick(at(2026, 10, 28, 9, 0))
        assert len(m.calls) == 4
        m.sessions["s4"]["status"] = "idle"
        # a run older than a day is skipped: the task (with a 6:00 slot) last ran on the 28th, the computer wakes at
        # 7:00 on the 31st... the newest slot (the 31st's 6:00) is 1 h old: runs. One slot per wake, never a backlog.
        hub.sched_tasks()[0]["cron"] = "0 6 * * 1"            # Mondays only; the 26th was the last Monday
        hub.sched_runs[tid]["last"] = at(2026, 10, 28, 8, 0)
        await tick(at(2026, 11, 1, 12, 0))                     # Sunday: the newest Monday 6:00 is before last
        assert len(m.calls) == 4
        await tick(at(2026, 11, 3, 7, 0))                      # Tuesday: Monday the 2nd's 6:00 is 25 h old
        assert len(m.calls) == 4 and hub.sched_runs[tid]["skipped"] == at(2026, 11, 2, 6, 0)
        hub.sched_tasks()[0]["cron"] = "0 8 * * *"
        # run now, edit (off), remove
        await hub.sched_op(p, "sched_run", {"id": tid})
        assert len(m.calls) == 5 and p.out[-1]["t"] == "sched_ok" and p.out[-1]["sid"] == "s5"
        await hub.sched_op(p, "sched_save", {"id": tid, "task": dict(hub.sched_tasks()[0], enabled=False)})
        assert hub.sched_tasks()[0]["enabled"] is False and hub.sched_view()[0]["next"] is None
        await tick(at(2026, 10, 31, 8, 0, 30))
        assert len(m.calls) == 5
        with pytest.raises(ValueError):
            await hub.sched_op(p, "sched_save", {"task": {"name": "x", "cron": "bad", "machine": "M1",
                                                           "kind": "claude", "cwd": "~", "prompt": "p"}})
        await hub.sched_op(p, "sched_remove", {"id": tid})
        assert hub.sched_tasks() == [] and tid not in hub.sched_runs
    asyncio.run(run())
