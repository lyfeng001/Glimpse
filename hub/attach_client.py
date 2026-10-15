#!/usr/bin/env python3
"""`./glimpse attach [M2:]SID` on the hub computer.

Sessions owned by the local agent daemon are attached directly (agent/glimpse_agent.py attach). Anything
else, i.e. sessions on ssh machines, goes through the running hub's websocket (same protocol as the phone:
sub / replay / out / in / resize), so every session on every machine can be opened from this terminal.
Ctrl-] detaches.
"""
import asyncio
import base64
import json
import os
import signal
import ssl
import sys

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402

AGENT = os.path.join(C.ROOT, "agent", "glimpse_agent.py")
DETACH_KEY = b"\x1d"              # Ctrl-]
DEAD = ("exited", "error")


def local_sessions():
    """Sessions of the local daemon, or None if it is not running (never starts one)."""
    sys.path.insert(0, os.path.dirname(AGENT))
    try:
        import glimpse_agent as A
        c = A.Conn(A.connect(A.daemon_addr()[0], timeout=2))
        try:
            return c.call({"op": "hello", "client": "cli"}, ("hello",)).get("sessions") or []
        finally:
            c.s.close()
    except Exception:            # no daemon / incompatible agent: fall back to the hub
        return None


def exec_local(sid):
    args = [sys.executable, AGENT, "attach"] + ([sid] if sid else [])
    os.execv(sys.executable, args)


def hub_port():
    try:
        cfg = C.load()
    except (OSError, ValueError):
        return 8443
    for ls in cfg["hub"].get("listen") or []:
        if ls.get("port"):
            return int(ls["port"])
    return 8443


def term_size():
    try:
        sz = os.get_terminal_size(sys.stdout.fileno())
        if sz.columns and sz.lines:
            return sz.columns, sz.lines
    except OSError:
        pass
    return 100, 32


def pick(machines, want_m, want_sid):
    """(machine id, session) for the request; no sid -> the most recently active live session."""
    found = []
    for mi in machines:
        if want_m and mi["id"] != want_m:
            continue
        for s in mi.get("sessions") or []:
            if want_sid is None or s["sid"] == want_sid:
                found.append((mi["id"], s))
    if want_sid is None:
        found = [f for f in found if f[1].get("status") not in DEAD] or found
    found.sort(key=lambda f: -(f[1].get("activity") or 0))
    return found[0] if found else (None, None)


async def connect_hub(token, port):
    """https first (Tailscale cert, verify off: this is 127.0.0.1 only), then plain http."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    http = aiohttp.ClientSession(headers={"Authorization": "Bearer " + token})
    last = None
    for scheme, sslarg in (("https", ctx), ("http", False)):
        try:
            ws = await http.ws_connect("%s://127.0.0.1:%d/ws" % (scheme, port), ssl=sslarg, heartbeat=20,
                                       max_msg_size=64 * 1024 * 1024, timeout=aiohttp.ClientWSTimeout(ws_close=5))
            return http, ws
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
            last = e
    await http.close()
    raise SystemExit("glimpse: cannot reach the hub on 127.0.0.1:%d (%s). Is ./glimpse hub running?" % (port, last))


async def attach_hub(want_m, want_sid):
    import termios
    import tty
    http, ws = await connect_hub(C.token(), hub_port())
    try:
        first = await ws.receive_json()
        if first.get("t") != "state":
            raise SystemExit("glimpse: unexpected hub reply")
        mid, s = pick(first["machines"], want_m, want_sid)
        if s is None:
            raise SystemExit("glimpse: no session %s%s" % (want_m + ":" if want_m else "", want_sid or ""))
        sid = s["sid"]
        cols, rows = term_size()
        await ws.send_json({"op": "resize", "m": mid, "sid": sid, "cols": cols, "rows": rows})
        await ws.send_json({"op": "sub", "m": mid, "sid": sid, "off": None})
        out = sys.stdout.buffer
        pos = None                    # byte offset written so far; None until the replay arrives
        loop = asyncio.get_running_loop()
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        done = loop.create_future()
        keys = asyncio.Queue()

        def on_key():
            d = os.read(fd, 65536)
            keys.put_nowait(d)

        def on_winch():
            c, r = term_size()
            asyncio.ensure_future(ws.send_json({"op": "resize", "m": mid, "sid": sid, "cols": c, "rows": r}))

        async def pump_keys():
            while True:
                d = await keys.get()
                if not d or DETACH_KEY in d:
                    if not done.done():
                        done.set_result("detached")
                    return
                c, r = term_size()
                await ws.send_json({"op": "in", "m": mid, "sid": sid, "d": base64.b64encode(d).decode(),
                                    "cols": c, "rows": r})

        async def pump_ws():
            nonlocal pos
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                m = json.loads(msg.data)
                t = m.get("t")
                if m.get("m") not in (None, mid):
                    continue
                if t == "replay" and m.get("sid") == sid:
                    out.write(b"\x1b[H\x1b[2J" + base64.b64decode(m.get("d") or ""))
                    out.flush()
                    pos = m.get("end")
                elif t == "out" and m.get("sid") == sid and pos is not None:
                    data = base64.b64decode(m["d"])
                    skip = pos - m["off"]
                    if skip < len(data):
                        out.write(data[max(0, skip):])
                        out.flush()
                        pos = m["off"] + len(data)
                elif t == "resync" and m.get("m") == mid:
                    await ws.send_json({"op": "sub", "m": mid, "sid": sid, "off": pos})
                elif t == "state":
                    for mi in m.get("machines") or []:
                        if mi["id"] != mid:
                            continue
                        cur = next((x for x in mi.get("sessions") or [] if x["sid"] == sid), None)
                        if cur is None:
                            return "session removed"
                        if cur.get("status") in DEAD and cur.get("exit_code") is not None:
                            return "session exited (code %s)" % cur["exit_code"]
                elif t == "error" and m.get("sid") in (None, sid):
                    return "error: %s" % m.get("msg")
            return "hub closed the connection"

        tty.setraw(fd)
        loop.add_reader(fd, on_key)
        loop.add_signal_handler(signal.SIGWINCH, on_winch)
        tasks = [asyncio.ensure_future(pump_keys()), asyncio.ensure_future(pump_ws())]
        tasks[1].add_done_callback(lambda t: done.done() or done.set_result(
            t.result() if not t.cancelled() and t.exception() is None else "connection lost"))
        try:
            reason = await done
        finally:
            loop.remove_reader(fd)
            loop.remove_signal_handler(signal.SIGWINCH)
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            for t in tasks:
                t.cancel()
        sys.stdout.write("\r\n[glimpse] %s: %s:%s\r\n" % (reason, mid, sid))
    finally:
        await ws.close()
        await http.close()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    target = argv[0] if argv else None
    want_m, want_sid = None, target
    if target and ":" in target:
        want_m, want_sid = target.split(":", 1)
        want_sid = want_sid or None
    if not want_m:
        local = local_sessions()
        if local is not None:
            if want_sid and any(s["sid"] == want_sid for s in local):
                exec_local(want_sid)
            if not want_sid and any(s.get("status") not in DEAD for s in local):
                exec_local(None)      # the agent picks its most recent live session
    if not sys.stdin.isatty():
        raise SystemExit("glimpse: attach needs a terminal")
    try:
        asyncio.run(attach_hub(want_m, want_sid))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
