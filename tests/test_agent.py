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
