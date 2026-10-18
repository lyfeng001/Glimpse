import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))
import glimpse_agent as A  # noqa: E402


def test_claude_transcript_parsing():
    rows = A.parse_claude_line({"type": "user", "message": {"role": "user", "content": "hello"}})
    assert rows == [("user", "hello", {})]
    rows = A.parse_claude_line({"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "x"},
        {"type": "text", "text": "hi there"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}}]}})
    assert rows[0] == ("assistant", "hi there", {})
    assert rows[1][0] == "tool" and rows[1][1] == "Bash: ls -la"
    rows = A.parse_claude_line({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": [{"type": "text", "text": "out"}], "is_error": True}]}})
    assert rows == [("result", "out", {"error": True})]
    assert A.parse_claude_line({"type": "user", "isSidechain": True, "message": {"content": "x"}}) == []
    rows = A.parse_claude_line({"type": "user", "message": {"content":
                                "<command-name>/model</command-name><command-args></command-args>"}})
    assert rows == [("system", "/model", {})]


def test_codex_rollout_parsing():
    ev = lambda p: {"type": "event_msg", "payload": p}  # noqa: E731
    assert A.parse_codex_line(ev({"type": "item_completed", "item": {
        "type": "UserMessage", "content": [{"type": "text", "text": "do it"}]}})) == [("user", "do it", {})]
    assert A.parse_codex_line(ev({"type": "item_completed", "item": {
        "type": "AgentMessage", "content": [{"type": "Text", "text": "done"}]}})) == [("assistant", "done", {})]
    rows = A.parse_codex_line(ev({"type": "item_completed", "item": {
        "type": "CommandExecution", "command": ["/bin/zsh", "-lc", "pwd"], "aggregated_output": "/x\n",
        "exit_code": 0}}))
    assert rows[0] == ("tool", "Shell: pwd", {"tool": "Shell"}) and rows[1][1] == "/x"
    assert A.parse_codex_line(ev({"type": "task_complete", "last_agent_message": "ok", "error": None})) == \
        [("@done", "ok", {})]
    assert A.parse_codex_line(ev({"type": "task_complete", "error": {"message": "stream closed"}}))[0][0] == "@error"


def test_codex_parser_on_real_rollouts():
    """Parse the newest local rollout files (if any) without exceptions."""
    import glob
    files = sorted(glob.glob(os.path.expanduser("~/.codex/sessions/*/*/*/*.jsonl")))[-5:]
    n = 0
    for f in files:
        with open(f) as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                n += len(A.parse_codex_line(d))
    assert n >= 0


def test_tail_incremental(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text('{"a":1}\n{"b"')
    t = A.Tail(str(p))
    assert t.read() == [{"a": 1}]
    with open(p, "a") as f:
        f.write(':2}\n')
    assert t.read() == [{"b": 2}]
    assert t.read() == []


def test_prepare_claude_argv_merges_hooks(tmp_path):
    base = tmp_path / "s.json"
    base.write_text(json.dumps({"env": {"X": "1"}, "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "u"}]}]}}))
    argv, path = A.prepare_claude_argv(["claude", "--settings", str(base), "--model", "opus"], "abc", str(tmp_path))
    assert argv[0] == "claude" and argv[1] == "--settings" and argv[2] == path and argv[3:] == ["--model", "opus"]
    merged = json.load(open(path))
    assert merged["env"] == {"X": "1"}
    assert len(merged["hooks"]["Stop"]) == 2           # user's hook kept + ours
    assert set(A.CLAUDE_HOOK_EVENTS) <= set(merged["hooks"])
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_replay_offsets():
    class D:
        sdir = None
    s = A.Session(D(), "s1", "shell", "n", "/", [], 80, 24)
    s.ring = bytearray(b"0123456789")
    s.ring_start = 100
    r = s.d and A.Daemon.replay(None, s, 105, None)
    assert r["off"] == 105 and r["end"] == 110 and not r["reset"]
    assert A.base64.b64decode(r["d"]) == b"56789"
    r = A.Daemon.replay(None, s, 50, None)        # older than ring -> from ring start, reset
    assert r["off"] == 100 and r["reset"]
    r = A.Daemon.replay(None, s, None, None)
    assert r["reset"] and r["end"] == 110


def test_hook_relay_command_is_valid_python():
    cmd = A.hook_command()
    import shlex
    parts = shlex.split(cmd)
    assert parts[1] == "-c"
    compile(parts[2], "relay", "exec")


class FakeDaemon:
    sdir = None
    approval_wait = 600

    def __init__(self):
        self.memory = A.Memory(self)      # disabled: capture() is a no-op
        self.sent = []

    def send(self, c, obj):
        self.sent.append((c, obj))

    def send_subs(self, s, obj):
        pass

    def broadcast(self, obj):
        pass

    def event(self, s, ev):
        pass

    def later(self, delay, fn):
        pass


def _session(kind="claude"):
    d = FakeDaemon()
    s = A.Session(d, "s1", kind, "n", "/", [], 80, 24)
    s.event = lambda *a, **k: None
    return d, s


PERM_EV = {"hook_event_name": "PermissionRequest", "session_id": "abc", "tool_name": "Bash",
           "tool_input": {"command": "rm -rf build"},
           "permission_suggestions": [{"type": "addRules", "rules": [{"toolName": "Bash"}], "behavior": "allow",
                                       "destination": "session"}]}


def test_permission_request_waits_and_allow_always_returns_rules():
    d, s = _session()
    waiter = object()
    assert s.on_hook(dict(PERM_EV), waiter) is True          # hook client must block
    assert s.status == "waiting" and s.pending["tool"] == "Bash" and s.pending["can_always"]
    assert "suggestions" not in s.info()["pending"]           # rules never leave the daemon
    assert s.resolve_pending("always")
    (c, msg), = d.sent
    assert c is waiter
    dec = msg["out"]["hookSpecificOutput"]["decision"]
    assert dec["behavior"] == "allow" and dec["updatedPermissions"][0]["rules"][0]["toolName"] == "Bash"
    assert s.pending is None and s.status == "running"


def test_permission_deny_and_terminal_fallback():
    d, s = _session()
    s.on_hook(dict(PERM_EV), "w1")
    s.resolve_pending("deny")
    assert d.sent[-1][1]["out"]["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    s.on_hook(dict(PERM_EV), "w2")
    s.user_input()                                            # a key press hands it to claude's dialog
    assert d.sent[-1] == ("w2", {"t": "ok", "out": None}) and s.pending is None


def test_same_request_again_supersedes_old_one():
    d, s = _session()
    s.on_hook(dict(PERM_EV), "w1")
    s.on_hook(dict(PERM_EV), "w2")                             # same tool + input asked again
    assert d.sent[0] == ("w1", {"t": "ok", "out": None})       # the stale hook gets released
    assert s.pending_waiter == "w2" and s.info()["pending_count"] == 1


def test_parallel_pendings_keyed_by_id():
    d, s = _session()
    s.on_hook(dict(PERM_EV), "w1")
    s.on_hook(dict(PERM_EV, tool_input={"command": "ls"}), "w2")
    info = s.info()
    assert info["pending_count"] == 2 and info["pending"]["summary"] == "Bash: rm -rf build"   # oldest shown
    assert "waiter" not in info["pending"] and "match" not in info["pending"]
    json.dumps(info)
    # PostToolUse of the second call resolves only that one
    s.on_hook({"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert d.sent == [("w2", {"t": "ok", "out": None})]
    assert s.pending_waiter == "w1" and s.status == "waiting"
    # a different tool input does not resolve anything
    s.on_hook({"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "pwd"}})
    assert s.info()["pending_count"] == 1
    # approving by id answers the right hook
    pid = s.pending["id"]
    s.on_hook(dict(PERM_EV, tool_input={"command": "make"}), "w3")
    assert s.resolve_pending("allow", pid=pid)
    assert d.sent[-1][0] == "w1" and d.sent[-1][1]["out"]["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    assert s.pending_waiter == "w3" and s.status == "waiting"


def test_drop_releases_pendings_of_that_client_only():
    import socket
    d = A.Daemon.__new__(A.Daemon)
    d.sessions, d.clients, d.timers = {}, set(), []
    d.sel = type("Sel", (), {"modify": lambda *x: None, "unregister": lambda *x: None})()
    d.approval_wait = 600
    d.broadcast = lambda obj: None
    s = A.Session(d, "s1", "claude", "n", "/", [], 80, 24)
    s.event = lambda *a, **k: None
    d.sessions["s1"] = s
    socks = [socket.socketpair() for _ in range(2)]
    c1, c2 = A.Client(socks[0][0]), A.Client(socks[1][0])
    d.clients = {c1, c2}
    s.on_hook(dict(PERM_EV), c1)
    s.on_hook(dict(PERM_EV, tool_input={"command": "ls"}), c2)
    A.Daemon.drop(d, c1)
    assert s.info()["pending_count"] == 1 and s.pending_waiter is c2
    for a, b in socks:
        a.close()
        b.close()


def test_stop_releases_pending_and_sets_idle():
    d, s = _session()
    s.on_hook(dict(PERM_EV), "w1")
    s.on_hook({"hook_event_name": "Stop", "last_assistant_message": "all done"})
    assert s.pending is None and s.status == "idle" and s.preview == "all done"


def test_claude_key_goes_into_settings_not_env(tmp_path):
    base = tmp_path / "s.json"
    base.write_text(json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "old", "ANTHROPIC_BASE_URL": "u"}}))
    argv, path = A.prepare_claude_argv(["claude", "--settings", str(base)], "x", str(tmp_path),
                                       {"ANTHROPIC_AUTH_TOKEN": "new"})
    env = json.load(open(path))["env"]
    assert env == {"ANTHROPIC_AUTH_TOKEN": "new", "ANTHROPIC_BASE_URL": "u"}


def test_usage_tracking_claude_dedupes_message_ids():
    _, s = _session()
    s.tail_kind = "claude"
    line = {"type": "assistant", "message": {"id": "m1", "model": "claude-x", "usage": {
        "input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20}}}
    s.track_usage(line)
    s.track_usage(line)               # same message id written once per content block
    u = s.usage
    assert (u["input"], u["output"], u["cache_read"], u["cache_write"]) == (10, 5, 100, 20)
    assert u["context_used"] == 130 and u["model"] == "claude-x"


def test_usage_tracking_codex_token_count():
    _, s = _session("codex")
    s.tail_kind = "codex"
    s.track_usage({"type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 50},
        "last_token_usage": {"input_tokens": 900, "output_tokens": 20},
        "model_context_window": 258400}}})
    u = s.usage
    assert u["input"] == 600 and u["cache_read"] == 400 and u["output"] == 50
    assert u["context_window"] == 258400 and u["context_used"] > 0


def test_lone_surrogate_does_not_crash_send():
    import socket
    a, b = socket.socketpair()
    d = A.Daemon.__new__(A.Daemon)
    c = A.Client(a)
    d.clients = {c}
    d.sel = type("Sel", (), {"modify": lambda *x: None, "unregister": lambda *x: None})()
    A.Daemon.send(d, c, {"t": "chat", "text": json.loads('"bad \\ud83d end"')})
    b.settimeout(2)
    assert b"bad" in (bytes(c.wbuf) or b.recv(4096))
    a.close()
    b.close()


# --------------------------------------------------------------------------- v3


def _iso(t):
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000Z"


def _jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def _codex_dir(home, t):
    import time
    lt = time.localtime(t)
    return home / ".codex" / "sessions" / ("%04d" % lt.tm_year) / ("%02d" % lt.tm_mon) / ("%02d" % lt.tm_mday)


def _rollout_name(t, tid):
    import time
    return "rollout-%s-%s.jsonl" % (time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime(t)), tid)


def _codex_meta(tid, cwd, t, **kw):
    return {"timestamp": _iso(t), "type": "session_meta",
            "payload": dict({"id": tid, "cwd": cwd, "timestamp": _iso(t), "source": "cli"}, **kw)}


def _user_item(text, t):
    return {"timestamp": _iso(t), "type": "event_msg", "payload": {"type": "item_completed", "item": {
        "type": "UserMessage", "content": [{"type": "text", "text": text}]}}}


def test_history_lists_claude_and_codex(tmp_path, monkeypatch):
    import time
    monkeypatch.setenv("HOME", str(tmp_path))
    A._HIST_CACHE.clear()
    t = time.time()
    proj = tmp_path / ".claude" / "projects" / "-x-proj"
    old = _jsonl(proj / "c-old.jsonl", [
        {"type": "permission-mode", "permissionMode": "default"},
        {"type": "user", "cwd": "/x/proj", "message": {"content": "<command-name>/clear</command-name>"}},
        {"type": "user", "cwd": "/x/proj", "isMeta": True, "message": {"content": "meta"}},
        {"type": "user", "cwd": "/x/proj", "message": {"content": "<system-reminder>r</system-reminder>"}},
        {"type": "user", "cwd": "/x/proj", "message": {"content": [{"type": "text", "text": "fix the bug\nplease"}]}}])
    os.utime(old, (t - 3600, t - 3600))
    _jsonl(proj / "c-new.jsonl", [{"type": "user", "cwd": "/x/proj", "message": {"content": "x" * 200}}])
    _jsonl(proj / "c-empty.jsonl", [{"type": "summary"}])                      # never prompted: skipped
    _jsonl(proj / "c-new" / "subagents" / "agent-1.jsonl", [{"type": "user", "message": {"content": "sub"}}])
    _jsonl(proj / "agent-abc.jsonl", [{"type": "user", "isSidechain": True, "message": {"content": "side"}}])
    d = _codex_dir(tmp_path, t)
    cx = _jsonl(d / _rollout_name(t - 600, "tid-1"), [_codex_meta("tid-1", "/y", t - 600),
                                                      {"type": "turn_context", "payload": {"model": "m"}},
                                                      _user_item("codex task", t - 590)])
    os.utime(cx, (t - 600, t - 600))
    _jsonl(d / _rollout_name(t - 500, "tid-sub"), [
        _codex_meta("tid-sub", "/y", t - 500, source={"subagent": {}}, thread_source="subagent"),
        _user_item("sub task", t - 500)])
    items = A.list_history(None, 30)
    assert [(i["kind"], i["id"]) for i in items] == [("claude", "c-new"), ("codex", "tid-1"), ("claude", "c-old")]
    assert items[0]["active"] and not items[1]["active"]
    assert len(items[0]["title"]) == 80
    assert items[2]["title"] == "fix the bug please" and items[2]["cwd"] == "/x/proj"
    assert items[1]["cwd"] == "/y" and items[1]["title"] == "codex task"
    assert [i["id"] for i in A.list_history("codex", 30)] == ["tid-1"]
    assert [i["id"] for i in A.list_history(None, 30, owned=["c-new"])] == ["tid-1", "c-old"]
    assert len(A.list_history(None, 1)) == 1


def test_usage_today_incremental(tmp_path, monkeypatch):
    import time
    monkeypatch.setenv("HOME", str(tmp_path))
    t = time.time()
    d0 = A.day_start(t)
    now_ = max(d0 + 60, t - 60)
    yday = d0 - 3600

    def asst(mid, ts, i, o, cr=0, cw=0, model="opus"):
        return {"type": "assistant", "timestamp": _iso(ts), "message": {"id": mid, "model": model, "usage": {
            "input_tokens": i, "output_tokens": o, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}}}
    cf = _jsonl(tmp_path / ".claude" / "projects" / "p" / "a.jsonl", [
        asst("m0", yday, 1000, 1000),                       # yesterday: ignored
        asst("m1", now_, 10, 5, 100, 20), asst("m1", now_, 10, 5, 100, 20),   # same message twice
        asst("m2", now_, 1, 2)])
    # the same message in a fork / sidechain file is counted once
    _jsonl(tmp_path / ".claude" / "projects" / "p" / "a" / "subagents" / "agent-x.jsonl", [asst("m2", now_, 1, 2)])

    def tc(ts, inp, cached, out):
        return {"timestamp": _iso(ts), "type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out}}}}
    xf = _jsonl(_codex_dir(tmp_path, t) / _rollout_name(t, "tid"), [
        _codex_meta("tid", "/y", yday), {"type": "turn_context", "payload": {"model": "gpt-x"}},
        tc(yday, 1000, 400, 100),                           # total before today
        tc(now_, 1500, 600, 150), tc(now_, 2000, 900, 300)])  # last total today
    u = A.UsageToday()
    r = u.scan(t)
    assert r["day"] == time.strftime("%Y-%m-%d", time.localtime(t))
    assert r["claude"] == {"input": 11, "output": 7, "cache_read": 100, "cache_write": 20}
    assert r["codex"] == {"input": 500, "output": 200, "cache_read": 500}
    assert r["by_model"]["opus"] == {"input": 131, "output": 7} and r["by_model"]["gpt-x"] == {"input": 1000, "output": 200}
    # incremental: appended lines are read from the saved offset, nothing is double counted
    pos = u.files[str(cf)]["pos"]
    with open(cf, "a") as f:
        f.write(json.dumps(asst("m3", now_, 100, 50)) + "\n" + '{"partial')
    with open(xf, "a") as f:
        f.write(json.dumps(tc(now_, 3000, 900, 400)) + "\n")
    r = u.scan(t)
    assert u.files[str(cf)]["pos"] > pos
    assert r["claude"]["input"] == 111 and r["claude"]["output"] == 57
    assert r["codex"] == {"input": 1500, "output": 300, "cache_read": 500}
    assert u.scan(t) == r                                   # unchanged files: same result
    # the next day starts from zero
    assert u.scan(d0 + 86400 + 7200)["claude"]["input"] == 0


def test_terminal_reports_are_not_user_input():
    assert not A.is_user_keys(b"\x1b[I")
    assert not A.is_user_keys(b"\x1b[O\x1b[I")
    assert not A.is_user_keys(b"\x1b[<0;12;5M\x1b[<0;12;5m")
    assert not A.is_user_keys(b"\x1b[12;40R")
    assert not A.is_user_keys(b"abc")
    assert A.is_user_keys(b"\r")
    assert A.is_user_keys(b"\x1b")
    assert A.is_user_keys(b"\x1b[A")
    assert A.is_user_keys(b"\x1b[Ix\r")
    assert A.is_user_keys(b"\x03")


def test_focus_report_keeps_pending():
    d = A.Daemon.__new__(A.Daemon)
    _, s = _session()
    s.on_hook(dict(PERM_EV), "w1")
    s.fd = None
    d.sessions = {"s1": s}
    d.send = lambda c, obj: None
    c = A.Client(None)
    c.kind = "hub"
    A.Daemon.handle(d, c, {"op": "in", "sid": "s1", "d": A.b64e(b"\x1b[I")})
    assert s.pending is not None
    A.Daemon.handle(d, c, {"op": "in", "sid": "s1", "d": A.b64e(b"\r")})
    assert s.pending is None


def test_rollout_matching_requires_start_after_spawn(tmp_path, monkeypatch):
    import time
    monkeypatch.setenv("HOME", str(tmp_path))
    t = time.time()
    d = _codex_dir(tmp_path, t)
    cwd = str(tmp_path)
    # an older conversation in the same cwd that is still being written (mtime is fresh)
    _jsonl(d / _rollout_name(t - 3600, "old"), [_codex_meta("old", cwd, t - 3600)])
    assert A.find_codex_rollout(cwd, t, set()) is None
    new = _jsonl(d / _rollout_name(t + 1, "new"), [_codex_meta("new", cwd, t + 1)])
    _jsonl(d / _rollout_name(t + 1, "other"), [_codex_meta("other", "/elsewhere", t + 1)])
    assert A.find_codex_rollout(cwd, t, set()) == str(new)
    assert A.find_codex_rollout(cwd, t, {str(new)}) is None
    # no payload timestamp: the file name is used
    nots = _jsonl(d / _rollout_name(t + 2, "nots"), [{"type": "session_meta", "payload": {"id": "nots", "cwd": cwd}}])
    assert A.find_codex_rollout(cwd, t, {str(new)}) == str(nots)
    # resumed conversations are found by id, any date
    od = tmp_path / ".codex" / "sessions" / "2020" / "01" / "02"
    f = _jsonl(od / "rollout-2020-01-02T03-04-05-tid-9.jsonl", [_codex_meta("tid-9", cwd, 0)])
    assert A.find_codex_rollout_by_id("tid-9") == str(f)
    assert A.find_codex_rollout_by_id("*") is None and A.find_codex_rollout_by_id("nope") is None
    import calendar
    assert A.iso_ts("2026-10-06T12:53:28.5Z") == calendar.timegm((2026, 10, 6, 12, 53, 28)) + 0.5


class SubsDaemon(FakeDaemon):
    def __init__(self):
        FakeDaemon.__init__(self)
        self.subs_sent = []
        self.events = []

    def send_subs(self, s, obj):
        self.subs_sent.append(obj)

    def broadcast(self, obj):
        if obj.get("t") == "event":
            self.events.append(obj)

    def taken_rollouts(self):
        return set()


def test_resumed_codex_imports_history_without_events(tmp_path):
    import time
    t = time.time()
    rows = [_codex_meta("tid", "/y", t - 900), {"type": "turn_context", "payload": {
        "model": "gpt-x", "effort": "high", "approval_policy": "on-request",
        "sandbox_policy": {"type": "workspace-write"}}}]
    for i in range(250):
        rows.append(_user_item("q%d" % i, t - 800))
    rows.append({"timestamp": _iso(t - 700), "type": "event_msg",
                 "payload": {"type": "task_complete", "last_agent_message": "old done"}})
    rows.append({"timestamp": _iso(t - 700), "type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": {"input_tokens": 50, "cached_input_tokens": 10, "output_tokens": 5}}}})
    f = _jsonl(tmp_path / "r.jsonl", rows)
    d = SubsDaemon()
    s = A.Session(d, "s1", "codex", "n", "/y", ["codex"], 80, 24)
    s.spawned_at = t
    s.attach_rollout(str(f), import_history=True)
    assert len(s.chat) == A.RESUME_IMPORT and s.chat[-1]["text"] == "q249"
    assert d.events == [] and s.status == "starting"
    assert s.usage["input"] == 40 and s.agent_session_id == "tid"
    assert s.info()["mode"] == {"model": "gpt-x", "effort": "high", "approval": "on-request",
                                "sandbox": "workspace-write"}
    # new lines after the import are tailed (and do fire events); nothing is read twice
    with open(f, "a") as fh:
        fh.write(json.dumps(_user_item("fresh", t + 5)) + "\n")
        fh.write(json.dumps({"timestamp": _iso(t + 6), "type": "event_msg",
                             "payload": {"type": "task_complete", "last_agent_message": "new done"}}) + "\n")
    s.poll_tail()
    assert s.chat[-1]["text"] == "fresh" and len(s.chat) == A.RESUME_IMPORT + 1
    assert len(d.events) == 1 and s.status == "idle"


def test_claude_history_replay_fires_no_events(tmp_path):
    import time
    t = time.time()
    f = _jsonl(tmp_path / "c.jsonl", [
        {"type": "user", "timestamp": _iso(t - 600), "message": {"content": "old question"}},
        {"type": "assistant", "timestamp": _iso(t - 590), "message": {"content": "old answer"}}])
    d = SubsDaemon()
    s = A.Session(d, "s1", "codex", "n", "/", [], 80, 24)    # tail_kind decides the parser
    s.spawned_at = t
    s.set_transcript(str(f))
    s.poll_tail()
    assert [c["text"] for c in s.chat] == ["old question", "old answer"] and d.events == []
    # old codex-style "@done" lines are skipped too
    s2 = A.Session(d, "s2", "codex", "n", "/", [], 80, 24)
    s2.spawned_at = t
    s2.tail_kind = "codex"
    s2.tail = A.Tail(str(_jsonl(tmp_path / "x.jsonl", [
        {"timestamp": _iso(t - 60), "type": "event_msg", "payload": {"type": "task_complete", "error": {"message": "x"}}}])))
    s2.poll_tail()
    assert s2.status == "starting" and d.events == []


class FakeSel:
    def register(self, *a):
        pass

    def unregister(self, *a):
        pass

    def modify(self, *a):
        pass


def _daemon(tmp_path):
    d = A.Daemon("x.sock", str(tmp_path))
    d.sel = FakeSel()
    d.out = []
    d.broadcast = lambda obj: d.out.append(obj)
    return d


def _drain(d):
    """run worker-thread results the way tick() does (the stubbed workers below run inline)"""
    while d.bg_done:
        cb, res, err = d.bg_done.pop(0)
        cb(res, err)


def test_netinfo_and_speedtest_ops(tmp_path, monkeypatch):
    d = _daemon(tmp_path)
    d.background = lambda fn: fn()
    sent = []
    d.send = lambda c, obj: sent.append(obj)
    looks = []
    monkeypatch.setattr(A, "fetch_netinfo", lambda: looks.append(1) or {"ip": "203.0.113.9", "city": "Shanghai"})
    d.handle(None, {"op": "netinfo", "req": 1})
    _drain(d)
    assert sent[-1]["t"] == "netinfo" and sent[-1]["req"] == 1 and sent[-1]["ip"] == "203.0.113.9" and sent[-1]["ts"]
    d.handle(None, {"op": "netinfo", "req": 2})                     # cached
    assert sent[-1]["req"] == 2 and len(looks) == 1
    d.handle(None, {"op": "netinfo", "req": 3, "refresh": True})    # refresh looks it up again
    _drain(d)
    assert sent[-1]["req"] == 3 and len(looks) == 2

    def down():
        raise OSError("no route")
    monkeypatch.setattr(A, "fetch_netinfo", down)
    d.handle(None, {"op": "netinfo", "req": 4, "refresh": True})
    _drain(d)
    assert sent[-1]["t"] == "error" and "no route" in sent[-1]["msg"]
    # speed test: one at a time; the busy flag clears on success and on failure
    monkeypatch.setattr(A, "speed_test", lambda: {"down_bps": 1000, "bytes": 10, "secs": 1.0, "ttfb_ms": 5.0})
    d.background = lambda fn: None                                   # keep it running
    d.handle(None, {"op": "speedtest", "req": 5})
    d.handle(None, {"op": "speedtest", "req": 6})
    assert sent[-1]["t"] == "error" and sent[-1]["req"] == 6
    d._speed_busy = False
    d.background = lambda fn: fn()
    d.handle(None, {"op": "speedtest", "req": 7})
    _drain(d)
    assert sent[-1] == {"t": "speedtest", "req": 7, "down_bps": 1000, "bytes": 10, "secs": 1.0, "ttfb_ms": 5.0}

    def slow():
        raise TimeoutError("timed out")
    monkeypatch.setattr(A, "speed_test", slow)
    d.handle(None, {"op": "speedtest", "req": 8})
    _drain(d)
    assert sent[-1]["t"] == "error" and "timed out" in sent[-1]["msg"] and not d._speed_busy


def test_speed_test_measures_download(monkeypatch):
    import io
    import urllib.request

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    seen = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: seen.append(req.full_url) or Resp(b"x" * 3000))
    r = A.speed_test(nbytes=3000, timeout=5)
    assert seen == [A.SPEED_URL % 3000] and r["bytes"] == 3000 and r["down_bps"] > 0 and r["ttfb_ms"] >= 0
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: Resp(json.dumps(
        {"ip": "198.51.100.7", "city": "Hangzhou", "region": "Zhejiang", "country": "CN", "org": "AS1", "extra": 1}).encode()))
    assert A.fetch_netinfo() == {"ip": "198.51.100.7", "city": "Hangzhou", "region": "Zhejiang", "country": "CN",
                                 "org": "AS1", "timezone": ""}
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: Resp(b'{"bogon": true}'))
    import pytest
    with pytest.raises(RuntimeError):
        A.fetch_netinfo()


def test_delete_sequencing(tmp_path, monkeypatch):
    d = _daemon(tmp_path)
    monkeypatch.setattr(A.Session, "spawn", lambda self, resume=False: setattr(self, "fd", 99) or
                        setattr(self, "pid", 4242))
    killed = []
    monkeypatch.setattr(A.Daemon, "kill", lambda self, s: killed.append(s.sid) or setattr(s, "killed", True))
    s = d.create("shell", str(tmp_path), None, None, 80, 24)
    sent = []
    d.send = lambda c, obj: sent.append(obj)
    d.handle(None, {"op": "delete", "sid": s.sid, "req": 7})
    assert killed == [s.sid] and s.sid in d.sessions and sent == []    # waits for the exit
    s.fd = None
    s.on_exit(-15)
    assert s.sid not in d.sessions and {"t": "gone", "sid": s.sid} in d.out
    assert sent == [{"t": "ok", "req": 7, "sid": s.sid}]
    assert not [o for o in d.out if o.get("t") == "event"]         # no "exited" push for a deleted session
    # exited sessions are deleted right away
    s2 = d.create("shell", str(tmp_path), None, None, 80, 24)
    s2.fd = None
    d.handle(None, {"op": "delete", "sid": s2.sid, "req": 8})
    assert s2.sid not in d.sessions and sent[-1] == {"t": "ok", "req": 8, "sid": s2.sid}


def test_create_with_resume_id(tmp_path, monkeypatch):
    d = _daemon(tmp_path)
    seen = []
    monkeypatch.setattr(A.Session, "spawn", lambda self, resume=False: seen.append((resume, self.agent_session_id))
                        or setattr(self, "fd", 99))
    s = d.create("claude", str(tmp_path), None, None, 80, 24, resume_id="abc-123")
    assert seen == [(True, "abc-123")] and s.info()["agent_session_id"] == "abc-123"
    import pytest
    with pytest.raises(RuntimeError):
        d.create("claude", str(tmp_path), None, None, 80, 24, resume_id="abc-123")   # already running
    with pytest.raises(RuntimeError):
        d.create("claude", str(tmp_path), None, None, 80, 24, resume_id="--evil")
    with pytest.raises(RuntimeError):
        d.create("shell", str(tmp_path), None, None, 80, 24, resume_id="x")


def test_info_hides_argv():
    _, s = _session()
    s.argv = ["/opt/bin/claude", "--settings", "/secret/path.json"]
    info = s.info()
    assert "argv" not in info and info["argv0"] == "claude"
    assert info["mode"]["mode"] == "default" and info["overrides"] == {} and info["pending_count"] == 0


def test_codex_launch_argv_no_forced_no_daemon_and_overrides():
    _, s = _session("codex")
    s.argv = ["codex", "-m", "old"]
    assert "--no-daemon" not in s.launch_argv()                # B1: preset decides
    s.agent_session_id = "tid-1"
    s.overrides = {"model": "gpt-x"}
    argv = s.launch_argv(resume=True)
    i = argv.index("resume")                                   # global -c flags go before the subcommand
    assert argv[0] == "codex" and argv[i + 1] == "tid-1" and argv[-2:] == ["-m", "gpt-x"]
    assert "old" not in argv and "tui.notifications=true" in argv
    out = A.codex_override_argv(["codex", "-m", "old", "-c", 'model_reasoning_effort="low"', "--sandbox=read-only",
                                 "--no-daemon"], {"model": "gpt-x", "effort": "high", "sandbox": "danger-full-access",
                                                  "approval": "never"})
    assert out == ["codex", "--no-daemon", "-m", "gpt-x", "-c", 'model_reasoning_effort="high"', "-a", "never",
                   "-s", "danger-full-access"]


def test_always_allow_uses_the_resolved_requests_rules():
    d, s = _session()
    s.on_hook(dict(PERM_EV, tool_name="Write", tool_input={"file_path": "/a"}, permission_suggestions=[]), "w1")
    s.on_hook(dict(PERM_EV), "w2")                      # second, different request with suggestions
    second = [p for p in s.pendings.values() if p["waiter"] == "w2"][0]
    assert s.resolve_pending("always", pid=second["id"])
    out = [o for c, o in d.sent if c == "w2"][-1]["out"]
    assert out["hookSpecificOutput"]["decision"]["updatedPermissions"][0]["rules"][0]["toolName"] == "Bash"


# --------------------------------------------------------------------------- v4

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _screen_menu(name):
    scr = A.Screen(100, 30)
    with open(os.path.join(FIX, name), "rb") as f:
        scr.feed(f.read())
    return scr.menu()


def test_menu_claude_model_capture():
    m = _screen_menu("tui_claude_model.bin")
    assert m["kind"] == "list" and m["title"] == "Select model" and m["cursor"] == 0
    assert [o["label"] for o in m["options"]] == ["Default (recommended)", "global.anthropic.claude-opus-5-5",
                                                  "Sonnet 5", "Haiku 4.5"]
    assert m["options"][0]["current"] and not m["options"][1]["current"]
    assert m["options"][0]["desc"].endswith("(currently global.anthropic.claude-opus-5-5[1m])")   # wrapped line
    assert m["options"][3]["desc"] == "Custom Haiku model (global.anthropic.claude-haiku-4-5-20251001-v1:0)"
    assert "s to use this session only" in m["hint"]
    json.dumps(m)


def test_menu_claude_effort_slider_capture():
    m = _screen_menu("tui_claude_effort.bin")
    assert m["kind"] == "slider" and m["title"] == "Effort"
    assert [o["label"] for o in m["options"]] == ["low", "medium", "high", "xhigh", "max", "ultracode"]
    assert m["cursor"] == 2 and "Enter to confirm" in m["hint"]


def test_menu_claude_effort_slider_toggle_layout():
    """Newer layout: ultracode is a Tab toggle after "max", the slider has five stops."""
    m = _screen_menu("tui_claude_effort2.bin")
    assert m["kind"] == "slider" and m["title"] == "Effort" and m["cursor"] == 3
    assert [o["label"] for o in m["options"]] == ["low", "medium", "high", "xhigh", "max"]


def test_menu_codex_model_capture():
    m = _screen_menu("tui_codex_model.bin")
    assert m["kind"] == "list" and m["title"] == "Select Model and Effort" and m["cursor"] == 1
    assert len(m["options"]) == 8 and m["options"][1]["label"] == "GPT-6-Astra" and m["options"][1]["current"]
    assert m["options"][0]["desc"].startswith("Latest workhorse") and m["hint"] == "enter select · esc back"


def test_menu_none_without_footer_or_when_unsure():
    assert A.parse_menu(["  1. foo", "❯ 2. bar"]) is None                          # no footer
    assert A.parse_menu(["Pick", "  1. a", "  2. b", "", "Esc to cancel"]) is None   # no cursor mark
    assert A.parse_menu(["Pick", "❯ 1. a", "  3. b", "", "Esc to cancel"]) is None   # gap in numbering
    assert A.parse_menu(["", "Esc to cancel"]) is None
    many = ["Pick"] + ["%s%d. x" % ("❯ " if i == 1 else "  ", i) for i in range(1, 23)] + ["esc to cancel"]
    assert A.parse_menu(many) is None
    m = A.parse_menu(["Do you want to proceed?", "› 1. Yes", "  2. No (current)", "Esc to cancel"])
    assert m["options"][1] == {"i": 1, "label": "No", "desc": "", "current": True}
    long_ = A.parse_menu(["T", "❯ 1. " + "y" * 300, "esc to cancel"])
    assert len(long_["options"][0]["label"]) == 200


class MenuSess:
    def __init__(self, menu):
        self.fd, self.menu, self.menu_dirty, self.writes = 5, menu, False, []

    def check_menu(self, t=None):
        pass

    def touch(self):
        pass

    def user_input(self):
        pass

    def write(self, d):
        self.writes.append(d)


def _timer_daemon():
    d = A.Daemon.__new__(A.Daemon)
    d.timers = []
    return d


def _run_timers(d):
    for _, fn in sorted(d.timers, key=lambda x: x[0]):
        fn()
    d.timers = []


def test_menu_op_key_sequences():
    d = _timer_daemon()
    s = MenuSess({"kind": "list", "options": [{"i": i} for i in range(4)], "cursor": 1})
    d.menu_op(s, index=3)
    _run_timers(d)
    assert s.writes == [b"\x1b[B", b"\x1b[B", b"\r"] and s.menu is None and s.menu_dirty
    s = MenuSess({"kind": "list", "options": [{"i": i} for i in range(4)], "cursor": 2})
    d.menu_op(s, index=0)
    _run_timers(d)
    assert s.writes == [b"\x1b[A", b"\x1b[A", b"\r"]
    s = MenuSess({"kind": "slider", "options": [{"i": i} for i in range(6)], "cursor": 2})
    d.menu_op(s, index=5)
    _run_timers(d)
    assert s.writes == [b"\x1b[C"] * 3 + [b"\r"]
    s = MenuSess({"kind": "slider", "options": [{"i": i} for i in range(6)], "cursor": 2})
    d.menu_op(s, index=2)
    assert s.writes == [b"\r"]
    for key, data in (("esc", b"\x1b"), ("s", b"s")):
        s = MenuSess({"kind": "list", "options": [{"i": 0}], "cursor": 0})
        d.menu_op(s, key=key)
        assert s.writes == [data]
    import pytest
    for bad in (dict(index=4), dict(index=-1), dict(index=True), dict(key="q")):
        with pytest.raises(RuntimeError):
            d.menu_op(MenuSess({"kind": "list", "options": [{"i": i} for i in range(4)], "cursor": 0}), **bad)
    with pytest.raises(RuntimeError):
        d.menu_op(MenuSess(None), index=0)


def test_session_menu_detected_after_settle():
    _, s = _session("codex")
    with open(os.path.join(FIX, "tui_codex_model.bin"), "rb") as f:
        s.screen.feed(f.read())
    s.menu_dirty = True
    s.activity = A.now()
    s.menu_parsed = s.activity
    s.check_menu(s.activity + 0.05)
    assert s.menu is None                                    # output still settling
    s.check_menu(s.activity + 0.2)
    assert s.info()["menu"]["title"] == "Select Model and Effort"
    # output that never pauses (claude's animated logo) still gets parsed every MENU_MAX_WAIT
    s.menu, s.menu_dirty, s.menu_parsed = None, True, A.now()
    s.activity = s.menu_parsed + 0.55
    s.check_menu(s.activity + 0.01)
    assert s.menu is not None
    s.on_exit(0)
    assert s.info()["menu"] is None


def test_models_claude_from_settings(tmp_path):
    st = tmp_path / "s.json"
    st.write_text(json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "secret-tok", "ANTHROPIC_BASE_URL": "https://x",
                                      "ANTHROPIC_DEFAULT_OPUS_MODEL": "my-opus",
                                      "ANTHROPIC_DEFAULT_HAIKU_MODEL": "my-haiku"}}))
    d = _daemon(tmp_path)
    out = []
    d.models("claude", ["claude", "--settings", str(st)], lambda r, e: out.append((r, e)))
    (r, e), = out
    assert e is None and "secret-tok" not in json.dumps(r) and "https://x" not in json.dumps(r)
    assert [m["id"] for m in r["models"]] == ["default", "opus", "sonnet", "haiku", "fable", "opusplan"]
    assert "my-opus" in r["models"][1]["desc"] and "my-haiku" in r["models"][3]["desc"]
    assert r["efforts"] == ["low", "medium", "high", "xhigh", "max", "ultracode"]
    assert r["modes"] == ["manual", "acceptEdits", "auto", "plan", "dontAsk", "bypassPermissions"]
    out = []
    d.models("claude", None, lambda r, e: out.append(r))      # no settings file: still a list
    assert out[0]["models"][0]["label"] == "Default"


CODEX_MODELS_JSON = json.dumps({"models": [
    {"slug": "b", "display_name": "B", "description": "bee", "visibility": "list", "priority": 2,
     "default_reasoning_level": "medium", "supported_reasoning_levels": [{"effort": "low"}, {"effort": "max"}]},
    {"slug": "hidden", "display_name": "H", "visibility": "hide", "priority": 0,
     "supported_reasoning_levels": [{"effort": "minimal"}]},
    {"slug": "a", "display_name": "A", "description": "ay", "visibility": "list", "priority": 1,
     "default_reasoning_level": "low",
     "supported_reasoning_levels": [{"effort": "ultra"}, {"effort": "low"}, {"effort": "xhigh"}]}]})


def test_models_codex_subprocess_and_cache(tmp_path, monkeypatch):
    d = _daemon(tmp_path)
    calls = []
    monkeypatch.setattr(A, "run_login_shell", lambda cmd, timeout: calls.append(cmd) or "motd\n" + CODEX_MODELS_JSON)
    d.background = lambda fn: fn()
    out = []
    d.models("codex", ["codex", "-c", "x=1", "--no-daemon", "-p", "prof"], lambda r, e: out.append((r, e)))
    assert out == []                                          # delivered from tick() (main loop)
    d.tick()
    (r, e), = out
    assert calls == [["codex", "debug", "models", "-c", "x=1"]]
    assert [m["id"] for m in r["models"]] == ["a", "b"]
    assert r["models"][0] == {"id": "a", "label": "A", "desc": "ay", "efforts": ["ultra", "low", "xhigh"],
                              "default_effort": "low"}
    assert r["efforts"] == ["low", "xhigh", "max", "ultra"] and r["modes"] == []
    d.models("codex", ["codex", "-c", "x=1"], lambda r, e: out.append((r, e)))
    assert len(calls) == 1 and out[-1][0] is r                 # cached
    d.models_cache = {}
    monkeypatch.setattr(A, "run_login_shell", lambda cmd, timeout: (_ for _ in ()).throw(RuntimeError("boom")))
    d.models("codex", None, lambda r, e: out.append((r, e)))
    d.tick()
    assert out[-1][0] is None and "boom" in out[-1][1] and d.models_cache == {}


def test_create_dedupes_resume_id(tmp_path, monkeypatch):
    d = _daemon(tmp_path)
    spawned = []

    def spawn(self, resume=False):
        spawned.append((self.sid, resume, dict(self.env)))
        self.fd, self.pid = 99, 4242
    monkeypatch.setattr(A.Session, "spawn", spawn)
    out = []
    d.create_or_reuse({"kind": "claude", "cwd": str(tmp_path), "resume_id": "conv-1"}, lambda *a: out.append(a))
    s, existing, err = out[-1]
    assert not existing and err is None and len(spawned) == 1
    # running -> the same session, no second process
    d.create_or_reuse({"kind": "claude", "cwd": str(tmp_path), "resume_id": "conv-1"}, lambda *a: out.append(a))
    assert out[-1] == (s, True, None) and len(spawned) == 1 and len(d.sessions) == 1
    # exited -> restarted in place (same sid), resuming, with the new key
    s.fd = None
    s.on_exit(0)
    s.add_chat("user", "keep me")
    d.create_or_reuse({"kind": "claude", "cwd": str(tmp_path), "resume_id": "conv-1", "env": {"K": "v2"},
                       "key": "key2"}, lambda *a: out.append(a))
    assert out[-1] == (s, True, None) and spawned[-1] == (s.sid, True, {"K": "v2"}) and s.key == "key2"
    assert len(d.sessions) == 1 and any(c["text"] == "keep me" for c in s.chat) and s.restarts == 1
    # through handle(): the reply carries existing=True
    sent = []
    d.send = lambda c, obj: sent.append(obj)
    d.handle(None, {"op": "create", "req": 3, "kind": "claude", "cwd": str(tmp_path), "resume_id": "conv-1"})
    assert sent[-1]["t"] == "created" and sent[-1]["existing"] is True and sent[-1]["s"]["sid"] == s.sid
    d.handle(None, {"op": "create", "req": 4, "kind": "claude", "cwd": str(tmp_path)})
    assert sent[-1]["t"] == "created" and "existing" not in sent[-1] and len(d.sessions) == 2


def test_init_cmds_typed_after_first_session_start(tmp_path, monkeypatch):
    d = _daemon(tmp_path)
    monkeypatch.setattr(A.Session, "spawn", lambda self, resume=False: setattr(self, "fd", 99))
    typed = []
    monkeypatch.setattr(A.Daemon, "type_text", lambda self, s, text: typed.append(text))
    s = d.create("claude", str(tmp_path), None, None, 80, 24, init_cmds=["/effort ultracode"])
    s.event = lambda *a, **k: None
    assert s.init_cmds == ["/effort ultracode"]
    s.on_hook({"hook_event_name": "SessionStart", "session_id": "x"})
    assert s.init_cmds == [] and typed == []
    for t, fn in d.timers:
        fn()
    assert typed == ["/effort ultracode"]
    d.timers = []
    s.on_hook({"hook_event_name": "SessionStart", "session_id": "x"})    # /clear etc.: not again
    assert d.timers == []
    import pytest
    for bad in (["effort"], ["/x"] * 5, ["/" + "x" * 200], "/x", ["/a\r/b"]):
        with pytest.raises(RuntimeError):
            d.create("claude", str(tmp_path), None, None, 80, 24, init_cmds=bad)
    assert d.create("codex", str(tmp_path), None, None, 80, 24, init_cmds=["/x"]).init_cmds == []


def test_control_effort_values(tmp_path):
    d = _timer_daemon()
    typed = []
    d.type_text = lambda s, text: typed.append(text)
    _, s = _session()
    s.fd = 5
    d.control(s, "effort", "ultracode", lambda err: typed.append(err))
    assert typed == ["/effort ultracode", None]
    _, cs = _session("codex")
    restarts = []
    cs.restart = lambda env, key, resume, done: restarts.append(resume) or done(None)
    d.control(cs, "effort", "ultra", lambda err: restarts.append(err))
    assert cs.overrides == {"effort": "ultra"} and restarts == [True, None]


def _cfg(tmp_path, monkeypatch, key=None):
    cfg = {"keys": {"env": {"claude": "ANTHROPIC_AUTH_TOKEN", "codex": "OPENAI_API_KEY"},
                    "profiles": [{"name": "key1", "value": "v1"}, {"name": "key2", "value": "v2"}]},
           "machines": [{"id": "M2", "type": "ssh", "presets": {"claude": {"argv": ["nope"]}}},
                        {"id": "M1", "type": "local", "presets": {
                            "claude": {"argv": ["claude", "--settings", "~/s.json"], "key": key},
                            "codex": {"argv": ["codex", "--no-daemon"]}}}]}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setenv("GLIMPSE_CONFIG", str(p))


def test_cmd_run_key_resolution(tmp_path, monkeypatch):
    import pytest
    _cfg(tmp_path, monkeypatch, key="key1")
    assert A.local_launch("claude") == (["claude", "--settings", "~/s.json"], "key1", {"ANTHROPIC_AUTH_TOKEN": "v1"})
    assert A.local_launch("claude", "key2")[1:] == ("key2", {"ANTHROPIC_AUTH_TOKEN": "v2"})
    assert A.local_launch("claude", "none")[1:] == (None, None)
    assert A.local_launch("codex") == (["codex", "--no-daemon"], None, None)
    assert A.local_launch("codex", "key2")[1:] == ("key2", {"OPENAI_API_KEY": "v2"})
    with pytest.raises(RuntimeError):
        A.local_launch("claude", "missing")
    monkeypatch.setenv("GLIMPSE_CONFIG", str(tmp_path / "absent.json"))
    assert A.local_launch("claude") == (["claude"], None, None)


def test_cmd_run_sends_key_and_attaches_existing(tmp_path, monkeypatch, capsys):
    _cfg(tmp_path, monkeypatch, key="key1")
    sent, attached = [], []

    class FakeConn:
        def __init__(self, sock):
            pass

        def call(self, obj, want):
            sent.append(obj)
            if obj["op"] == "hello":
                return {"t": "hello"}
            return {"t": "created", "s": {"sid": "abc"}, "existing": True}
    monkeypatch.setattr(A, "Conn", FakeConn)
    monkeypatch.setattr(A, "ensure_daemon", lambda: None)
    monkeypatch.setattr(A, "attach", lambda sid: attached.append(sid))
    monkeypatch.setattr(A.time, "sleep", lambda x: None)
    A.cmd_run(["claude", "--resume", "conv-1", "-C", str(tmp_path)])
    cr = sent[-1]
    assert cr["env"] == {"ANTHROPIC_AUTH_TOKEN": "v1"} and cr["key"] == "key1" and cr["resume_id"] == "conv-1"
    assert attached == ["abc"]
    out = capsys.readouterr().out
    assert "[glimpse] key: key1" in out and "v1" not in out and "already" in out
    A.cmd_run(["codex", "-k", "key2", "-d"])
    assert sent[-1]["key"] == "key2" and sent[-1]["env"] == {"OPENAI_API_KEY": "v2"} and attached == ["abc"]
    import pytest
    with pytest.raises(SystemExit):
        A.cmd_run(["claude", "--key", "nope"])


def test_shell_wrap_parsing():
    w = A.split_wrap_args
    tid = "01a0edaf-75de-7bd2-b706-c991c4118ecb"
    assert w("codex", ["resume", tid, "--no-daemon"]) == (True, tid, ["--no-daemon"])
    assert w("codex", ["--no-daemon"]) == (True, None, ["--no-daemon"])
    assert w("codex", ["fix the tests"]) == (True, None, ["fix the tests"])        # prompt, not a sub-command
    assert w("codex", ["resume", "--last"]) == (True, None, ["resume", "--last"])   # codex's own picker
    assert w("codex", ["exec", "hi"])[0] is False
    assert w("codex", ["login"])[0] is False
    assert w("codex", ["--version"])[0] is False
    assert w("claude", ["--resume", "abc-123", "--model", "opus"]) == (True, "abc-123", ["--model", "opus"])
    assert w("claude", ["-r", "abc-123"]) == (True, "abc-123", [])
    assert w("claude", ["--resume=abc-123"]) == (True, "abc-123", [])
    assert w("claude", ["-r"]) == (True, None, ["-r"])                              # interactive picker
    assert w("claude", ["-p", "hi"])[0] is False
    assert w("claude", ["mcp", "list"])[0] is False
    assert w("claude", []) == (True, None, [])


def test_shell_wrap_merge_argv():
    m = A.merge_wrap_argv
    assert m(["codex", "--no-daemon"], ["--no-daemon"]) == ["codex", "--no-daemon"]
    assert m(["claude", "--settings", "a.json"], ["--model", "opus"]) == ["claude", "--settings", "a.json", "--model", "opus"]
    assert m(["claude", "--settings", "a.json"], ["--settings", "b.json"]) == ["claude", "--settings", "b.json"]


def test_shell_init_defines_functions(capsys):
    A.cmd_shell_init()
    out = capsys.readouterr().out
    assert "claude() { _glimpse_wrap claude" in out and "codex() { _glimpse_wrap codex" in out
    assert "wrap \"$@\"" in out and "GLIMPSE_SID" in out
    import subprocess
    r = subprocess.run(["sh", "-n", "-c", out], capture_output=True)
    assert r.returncode == 0, r.stderr


def test_transient_error_classification():
    t = A.is_transient_error
    assert t("stream disconnected before completion: Our servers are currently overloaded. Please try again later.")
    assert t("unexpected status 502 Bad Gateway")
    assert t("overloaded_error")
    assert not t('unexpected status 666: {"code":9000,"code_msg":"服务器异常","code_reason":"Key余额不足，无法完成当前操作"}')
    assert not t("API KEY 权限验证失败，请检查header")
    assert not t("401 Unauthorized")
    assert not t("rate limit reached for your plan quota")
    assert not t("")
    # the four wordings reported from real codex runs: all retried with "继续"
    assert t("Incomplete response returned, reason: max_output_tokens")
    assert t("Connection failed: error sending request for url (https://api.example.com/v1/responses)")
    assert t("stream disconnected before completion: Service temporarily unavailable")
    assert t("stream disconnected before completion: stream closed before response.completed")


def test_auto_continue_retries_transient_only():
    d, s = _session("codex")
    s.fd = 99
    timers, typed = [], []
    d.later = lambda delay, fn: timers.append((delay, fn))
    d.type_text = lambda ss, t: typed.append(t)
    overload = "stream disconnected before completion: Our servers are currently overloaded."
    assert s.maybe_auto_continue(overload) is False          # off by default
    s.auto_continue = True
    assert s.maybe_auto_continue(overload) is True
    assert s.status == "waiting" and timers[0][0] == 15
    timers[0][1]()
    assert typed == ["继续"] and s.status == "running"
    # no balance: never retried, the user has to act
    s.status = "error"
    assert s.maybe_auto_continue("Key余额不足，无法完成当前操作") is False
    # backoff grows on consecutive failures, then gives up
    for _ in range(2, A.AUTO_CONTINUE_MAX + 1):
        s.status = "error"
        assert s.maybe_auto_continue(overload) is True
        timers[-1][1]()
    s.status = "error"
    assert s.maybe_auto_continue(overload) is False and s.ac_count == A.AUTO_CONTINUE_MAX
    assert [t[0] for t in timers] == list(A.AUTO_CONTINUE_DELAYS)



def test_memory_project_id_and_launch_args(tmp_path):
    import subprocess
    repo = tmp_path / "work" / "checkout"
    repo.mkdir(parents=True)
    (repo / "sub").mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:someone/My Repo.git"], check=True)
    # same project id from any subdirectory and on any machine (from the remote name)
    assert A.memory_project_id(str(repo / "sub")) == ("My-Repo", os.path.realpath(str(repo)))
    plain = tmp_path / "plain dir"
    plain.mkdir()
    assert A.memory_project_id(str(plain))[0] == "plain-dir"
    d, s = _session("claude")
    s.cwd = str(repo)
    mem = A.Memory(d)
    mem.cfg = dict(A.MEMORY_DEFAULT, enabled=True, python="/opt/py", store=str(tmp_path / "vault"))
    captured = []
    mem.capture = lambda ss: captured.append(ss.memory_project)
    mem.attach(s)
    assert s.memory_project is None                     # not probed yet: memory stays off
    mem.version = "0.4.0"
    mem.attach(s)
    assert s.memory_project == "My-Repo" and captured == ["My-Repo"]   # earlier history imported at spawn
    args = mem.claude_args(s)
    cfg = json.loads(args[args.index("--mcp-config") + 1])["mcpServers"]["amem"]
    assert cfg["command"] == "/opt/py" and cfg["args"][-3:] == ["--project", "My-Repo", "--no-context"]
    assert "the live repository state always win" in args[args.index("--append-system-prompt") + 1]
    assert "mcp__amem__memory_save" in mem.claude_allow(s)
    c = mem.codex_args(s)
    assert 'mcp_servers.amem.command="/opt/py"' in c and any(x.startswith("developer_instructions=") for x in c)
    # a root already bound in the vault keeps that project id
    mem.set_roots({"echo-sim": {"root": str(repo)}})
    mem.attach(s)
    assert s.memory_project == "echo-sim" and mem.bound["echo-sim"] == os.path.realpath(str(repo))
    mem.roots = {}
    # a project that wires AMem itself is left alone
    (repo / ".mcp.json").write_text('{"mcpServers": {"x": {"args": ["-m", "agent_memory"]}}}')
    mem.attach(s)
    assert s.memory_project is None and mem.claude_args(s) == []
    # shells never get memory
    d2, sh = _session("shell")
    mem.attach(sh)
    assert sh.memory_project is None


def test_memory_settings_allow_merge(tmp_path):
    src = tmp_path / "s.json"
    src.write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}, "env": {"A": "1"}}))
    argv, path = A.prepare_claude_argv(["claude", "--settings", str(src)], "x1", str(tmp_path),
                                       allow=["mcp__amem__memory_save"])
    merged = json.load(open(path))
    assert merged["permissions"]["allow"] == ["Bash(ls:*)", "mcp__amem__memory_save"]
    assert merged["env"] == {"A": "1"}


def test_memory_query_validation():
    d, s = _session("claude")
    mem = A.Memory(d)
    out = []
    mem.query({"action": "overview"}, lambda r, e: out.append(e))
    assert "未开启" in out[-1]
    mem.cfg["enabled"], mem.version = True, "0.4.0"
    ran = []
    mem.run = lambda args, done, timeout=60: ran.append(args)
    for bad in ({"action": "search", "project": "p", "q": ""}, {"action": "search", "project": "../x", "q": "a"},
                {"action": "budget", "budget": "20G; rm -rf"}, {"action": "nope"}):
        mem.query(bad, lambda r, e: out.append(e))
        assert out[-1]
    assert ran == []
    mem.query({"action": "search", "project": "p", "q": "build fail"}, lambda r, e: None)
    mem.query({"action": "budget", "budget": "20G"}, lambda r, e: None)
    assert ran[0] == ["search", "--project", "p", "--limit", "30", "build fail"]
    assert ran[1][:3] == ["budget", "--max-bytes", "20G"]


def test_daemon_saves_and_restores_live_sessions(tmp_path, monkeypatch):
    d = A.Daemon("@unused", str(tmp_path))
    live = A.Session(d, "a1b2c3", "codex", "codex · x", str(tmp_path), ["codex", "--no-daemon"], 100, 30)
    live.fd, live.agent_session_id, live.key, live.auto_continue = 5, "019a-conv", "key3", True
    live.overrides = {"model": "gpt-x"}
    gone = A.Session(d, "dddddd", "claude", "n", str(tmp_path), ["claude"], 80, 24)
    gone.agent_session_id = "c-conv"                    # exited: not restored
    shell = A.Session(d, "eeeeee", "shell", "n", str(tmp_path), [], 80, 24)
    shell.fd = 6
    for s in (live, gone, shell):
        d.sessions[s.sid] = s
        d.order.append(s.sid)
    live.env = {"OPENAI_API_KEY": "sk-secret-value"}
    d.save_state()
    raw = (tmp_path / "sessions.json").read_text()
    assert "sk-secret-value" not in raw and oct(os.stat(tmp_path / "sessions.json").st_mode & 0o777) == "0o600"
    saved = json.loads(raw)["sessions"]
    assert [r["sid"] for r in saved] == ["a1b2c3"]
    # a new daemon resumes it under the same sid, key looked up again by name
    d2 = A.Daemon("@unused", str(tmp_path))
    calls = []

    def fake_create(kind, cwd, name, args, cols, rows, env=None, key=None, resume_id=None, init_cmds=None,
                    sid=None, overrides=None):
        calls.append((kind, args, key, env, resume_id, sid, overrides))
        s = A.Session(d2, sid, kind, name, cwd, args, cols, rows)
        d2.sessions[sid] = s
        return s
    d2.create = fake_create
    monkeypatch.setattr(A, "local_launch", lambda kind, key: (["codex"], key, {"OPENAI_API_KEY": "fresh"}))
    d2.restore_state()
    assert calls == [("codex", ["codex", "--no-daemon"], "key3", {"OPENAI_API_KEY": "fresh"}, "019a-conv",
                      "a1b2c3", {"model": "gpt-x"})]
    assert d2.sessions["a1b2c3"].auto_continue is True
    assert "自动恢复" in d2.sessions["a1b2c3"].chat[-1]["text"]
    # a key removed from config.json: resumed without a key instead of failing
    calls.clear()
    d3 = A.Daemon("@unused", str(tmp_path))
    d3.create = fake_create

    def no_key(kind, key):
        raise RuntimeError("no key")
    monkeypatch.setattr(A, "local_launch", no_key)
    (tmp_path / "sessions.json").write_text(raw)
    d3.restore_state()
    assert calls[0][2] is None and calls[0][3] is None


def test_claude_stop_failure_no_balance_is_not_retried():
    d, s = _session("claude")
    s.fd, s.auto_continue = 99, True
    timers = []
    d.later = lambda delay, fn: timers.append((delay, fn))
    s.poll_tail = lambda: None
    # the hook only says server_error; the reason is in the last assistant message
    s.on_hook({"hook_event_name": "StopFailure", "error": "server_error", "error_details": "",
               "last_assistant_message": "API Error: 500 Key余额不足，无法完成当前操作. This is a server-side issue"})
    assert timers == [] and s.status == "error"
    # same category with a transient message is retried
    s.status = "running"
    s.on_hook({"hook_event_name": "StopFailure", "error": "server_error", "error_details": "",
               "last_assistant_message": "API Error: 529 Overloaded"})
    assert len(timers) == 1 and s.status == "waiting"
    # no message in the hook: the transcript preview is used
    s.ac_pending, s.status, timers[:] = False, "running", []
    s.preview = "API Error: 401 权限验证失败"
    s.on_hook({"hook_event_name": "StopFailure", "error": "server_error"})
    assert timers == [] and s.status == "error"


def test_memory_probe_reports_bound_projects(tmp_path, monkeypatch):
    # the probe output carries the bound folders, so the very first session after start uses them
    d, s = _session("codex")
    mem = A.Memory(d)
    root = tmp_path / "sim"
    root.mkdir()
    out = json.dumps({"version": "0.4.0", "projects": {"echo-sim": {"root": str(root)}}})
    mem.exec_py = lambda args, done, fail=None, timeout=60: done(out, None)
    mem.run = lambda args, done, timeout=60: None
    mem.configure({"enabled": True, "python": "/x/py"})
    assert mem.version == "0.4.0" and mem.roots == {os.path.realpath(str(root)): "echo-sim"}
    s.cwd = str(root)
    mem.capture = lambda ss: None
    mem.attach(s)
    assert s.memory_project == "echo-sim"


class MenuDaemon(FakeDaemon):
    """Runs later() callbacks immediately in order; type_text records the command."""
    def __init__(self):
        FakeDaemon.__init__(self)
        self.q, self.typed = [], []

    def later(self, delay, fn):
        self.q.append(fn)

    def type_text(self, s, text):
        self.typed.append(text)

    def run(self, n=400):
        while self.q and n:
            self.q.pop(0)()
            n -= 1


def _codex_with_menus(menus):
    """A codex session whose screen shows menus[k] after the k-th selection key."""
    d = MenuDaemon()
    s = A.Session(d, "s1", "codex", "n", "/", ["codex"], 80, 24)
    s.fd, s.status = 99, "idle"
    keys = []
    state = {"k": 0}

    def write(b):
        keys.append(b)
        if b in (b"\r", b"s"):
            state["k"] += 1
    s.write = write
    s.check_menu = lambda t=None: setattr(s, "menu", menus[state["k"]] if state["k"] < len(menus) else None)
    return d, s, keys


def _menu(title, labels, cur=0, current=None):
    return {"kind": "list", "title": title, "cursor": cur,
            "options": [{"i": i, "label": l, "current": l == current} for i, l in enumerate(labels)]}


def test_codex_model_and_effort_switch_in_session():
    models = _menu("Select Model and Effort", ["GPT-6.1-Sol", "GPT-6-Astra (current)", "GPT-6-Luna"], 1, "GPT-6-Astra (current)")
    levels = _menu("Select Reasoning Level for GPT-6-Luna", ["Low (default)", "Medium", "High", "Extra high", "More reasoning…"], 0)
    d, s, keys = _codex_with_menus([models, levels])
    out = []
    d.walk_menus = A.Daemon.walk_menus.__get__(d)
    A.Daemon.codex_set_model(d, s, "gpt-6-luna", "xhigh", out.append)
    d.run()
    assert d.typed == ["/model"] and out == [None]
    # model: one down from the cursor (Astra -> Luna) + Enter; effort: three down + "s" (this session only)
    assert keys == [b"\x1b[B", b"\r", b"\x1b[B", b"\x1b[B", b"\x1b[B", b"s"]


def test_codex_max_effort_goes_through_advanced_menu():
    models = _menu("Select Model and Effort", ["GPT-6-Astra (current)"], 0, "GPT-6-Astra (current)")
    levels = _menu("Select Reasoning Level for GPT-6-Astra", ["Low", "Medium", "High", "Extra high", "More reasoning…"], 0)
    adv = _menu("Advanced Reasoning", ["Max", "Ultra"], 0)
    d, s, keys = _codex_with_menus([models, levels, adv])
    out = []
    d.walk_menus = A.Daemon.walk_menus.__get__(d)
    A.Daemon.codex_set_model(d, s, None, "ultra", out.append)    # keep the current model
    d.run()
    assert out == [None]
    # current model: Enter; "More reasoning…" is four down + Enter; Ultra one down + "s"
    assert keys == [b"\r"] + [b"\x1b[B"] * 4 + [b"\r"] + [b"\x1b[B", b"s"]


def test_codex_permissions_presets_and_missing_combination():
    perms = _menu("Update Model Permissions", ["Ask for approval (current)", "Approve for me", "Full Access", "Read Only"],
                  0, "Ask for approval (current)")
    d, s, keys = _codex_with_menus([perms])
    out = []
    d.walk_menus = A.Daemon.walk_menus.__get__(d)
    A.Daemon.codex_set_permissions(d, s, "sandbox", "read-only", out.append)
    d.run()
    assert out == [None] and keys == [b"\x1b[B"] * 3 + [b"\r"]
    assert s.cx_mode["sandbox"] == "read-only" and s.cx_mode["approval"] == "on-request"
    # never + workspace-write: no such preset -> error, so the caller restarts with -a never
    d, s, keys = _codex_with_menus([perms])
    d.walk_menus = A.Daemon.walk_menus.__get__(d)
    out = []
    A.Daemon.codex_set_permissions(d, s, "approval", "never", out.append)
    d.run()
    assert out and out[0] and "没有这个选项" in out[0] and keys[0] == b"\x1b"


def test_codex_menu_walk_refuses_while_busy():
    d, s, keys = _codex_with_menus([])
    s.status = "running"
    out = []
    d.walk_menus = A.Daemon.walk_menus.__get__(d)
    A.Daemon.codex_set_model(d, s, "x", "low", out.append)
    assert out and out[0].startswith("会话") and d.typed == []


def test_toml_tables_and_codex_hot_provider(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    home.mkdir()
    (home / "config.toml").write_text('''model = "gpt-x"
model_provider = "relay"   # comment
[model_providers.relay]
name = "Relay"
base_url = "https://relay.example/v1"
env_key = "V_API_KEY"
wire_api = "responses"
request_max_retries = 0
[model_providers.relay.http_headers]
X-Team = "a#b"
[projects."/x"]
trust_level = "trusted"
''')
    monkeypatch.setenv("CODEX_HOME", str(home))
    t = A.toml_tables((home / "config.toml").read_text())
    assert t[""]["model_provider"] == '"relay"' and t["model_providers.relay.http_headers"]["X-Team"] == '"a#b"'
    args = A.codex_hot_provider(["codex", "--no-daemon"], "V_API_KEY")
    pairs = [args[i + 1] for i in range(0, len(args), 2)]
    assert pairs[0] == 'model_provider="relay-glimpse"'
    assert 'model_providers.relay-glimpse.base_url="https://relay.example/v1"' in pairs
    assert 'model_providers.relay-glimpse.http_headers.X-Team="a#b"' in pairs
    assert not any("env_key" in p for p in pairs)
    assert 'model_providers.relay-glimpse.auth.command="/bin/cat"' in pairs and any("{KEYFILE}" in p for p in pairs)
    # a different key variable, an explicit provider / profile on the command line: not ours to rewrite
    assert A.codex_hot_provider(["codex"], "OPENAI_API_KEY") is None
    assert A.codex_hot_provider(["codex", "-p", "work"], "V_API_KEY") is None
    assert A.codex_hot_provider(["codex", "-c", 'model_provider="x"'], "V_API_KEY") is None


def test_hot_key_claude_settings_and_swap(tmp_path):
    src = tmp_path / "s.json"
    src.write_text(json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "placeholder-old", "ANTHROPIC_BASE_URL": "https://relay"}}))
    d, s = _session("claude")
    d.sdir = str(tmp_path)
    s.env = {"ANTHROPIC_AUTH_TOKEN": "placeholder-one"}
    s.key = "key1"
    hot = s.hot_key_setup(["claude", "--settings", str(src)])
    assert hot and "ANTHROPIC_AUTH_TOKEN" in hot[0] and hot[1] == []
    kf = s.key_file
    assert open(kf).read() == "placeholder-one" and oct(os.stat(kf).st_mode & 0o777) == "0o600"
    argv, path = A.prepare_claude_argv(["claude", "--settings", str(src)], "x1", str(tmp_path), None, key_file=kf)
    merged = json.load(open(path))
    raw = open(path).read()
    assert "placeholder-one" not in raw and "placeholder-old" not in raw     # no token in the settings file
    assert merged["apiKeyHelper"].endswith(kf) and merged["env"]["ANTHROPIC_BASE_URL"] == "https://relay"
    assert merged["env"]["CLAUDE_CODE_API_KEY_HELPER_TTL_MS"] == A.CLAUDE_HELPER_TTL_MS
    # live swap rewrites the file; the process keeps running
    s.fd = 7
    assert s.swap_key({"ANTHROPIC_AUTH_TOKEN": "placeholder-two"}, "key2") is True
    assert open(kf).read() == "placeholder-two" and s.key == "key2" and s.info()["hot_key"] is True
    assert "已切换到 key2" in s.chat[-1]["text"]
    assert s.swap_key({"OTHER": "x"}, "k") is False                  # a different variable: restart needed
    # restart(env, key, resume=True) on a hot session swaps instead of restarting
    out = []
    s.restart({"ANTHROPIC_AUTH_TOKEN": "placeholder-three"}, "key3", True, out.append)
    assert out == [None] and s.restarts == 0 and open(kf).read() == "placeholder-three"
    # exit removes the key file
    s.on_exit(0)
    assert not os.path.exists(kf) and s.key_file is None


def test_no_hot_key_without_a_glimpse_key():
    d, s = _session("claude")
    assert s.hot_key_setup(["claude"]) is None and s.key_file is None
    s.env = {"SOMETHING_ELSE": "x"}
    assert s.hot_key_setup(["claude"]) is None
