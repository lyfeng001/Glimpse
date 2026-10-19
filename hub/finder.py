"""macOS Finder Quick Action "用 Glimpse 新建会话": right-click a folder (or a file: its folder) in Finder, and the
web app's new-session form opens with that path filled in (`gli new PATH`). Nothing is started until 创建.

    gli finder-install      write ~/Library/Services/Glimpse 新建会话.workflow
    gli finder-uninstall    remove it

The workflow is a plain Automator document with one "Run Shell Script" action; the selected path reaches the
script as an argument ("$@"), never through a shell string.

macOS runs Quick Actions without access to ~/Documents, ~/Desktop, ~/Downloads ("Operation not permitted"), and
this project usually lives there. So the action touches no file at all: the hub address and this computer's machine
id are written into it at install time (run finder-install again if they change), and at run time it only
percent-encodes the folder's path (osascript) and `open`s the link."""
import os
import plistlib
import shlex
import shutil
import subprocess
import sys

NAME = "Glimpse 新建会话"
MENU = "用 Glimpse 新建会话"
BUNDLE_ID = "com.glimpse.new-session"
SERVICES = os.path.expanduser("~/Library/Services")


def script(base, mid, app=None, relay=None):
    """base = the hub's URL ("https://host:port/"), mid = this computer's machine id (may be "").
    With an installed Glimpse window (app) and relay = (url, curl TLS args, token): ask the hub to show the form in
    that window (POST /api/ui/new), then bring the window up. Chrome opens a web app at its start page whatever URL
    it is given, so a link can't do it. Without: open the #new link in the default browser."""
    if app and relay:
        url, tls, tok = relay
        return ("[ -n \"$1\" ] || exit 0\n"
                "body=$(/usr/bin/osascript -l JavaScript -e 'function run(a) { return JSON.stringify({cwd: a[0], m: a[1] || null}) }' "
                "\"$1\" %s) || exit 1\n"
                # the token goes in on stdin (curl -K -), not on the command line where `ps` would show it
                "/usr/bin/curl -sS -f --noproxy '*' -m 5 %s -H 'Content-Type: application/json' --data-binary \"$body\" "
                "-o /dev/null -K - %s <<'GLIMPSE'\nheader = \"Authorization: Bearer %s\"\nGLIMPSE\n"
                "[ $? -eq 0 ] || { echo 'Glimpse: 连不上 hub（gli hub 在运行吗？）' >&2; exit 1; }\n"
                "exec /usr/bin/open -a %s\n") % (shlex.quote(mid or ""), " ".join(shlex.quote(a) for a in tls),
                                               shlex.quote(url), tok, shlex.quote(app))
    from urllib.parse import quote
    m = "&m=" + quote(mid, safe="") if mid else ""
    return ("[ -n \"$1\" ] || exit 0\n"
            "q=$(/usr/bin/osascript -l JavaScript -e 'function run(a) { return encodeURIComponent(a[0]) }' \"$1\") || exit 1\n"
            "exec /usr/bin/open %s\"$q\"%s\n") % (shlex.quote(base + "#new?cwd="), shlex.quote(m) if m else "")


def hub_relay(cfg, ca_copy):
    """How a process on this computer reaches the hub over loopback: (url of /api/ui/new, curl TLS args, CA file to
    copy to `ca_copy` or None). None when the hub doesn't listen on 127.0.0.1."""
    import server
    listen, name = server.resolve_hub(cfg)
    lo = next((x for x in listen if server.is_loopback(x["host"])), None)
    if not lo:
        return None
    port = lo["port"]
    cert = server.cert_paths()[0]
    local_crt, _ = server.local_cert_paths()
    ca = os.path.join(os.path.dirname(local_crt), "local-ca.crt")
    if not os.path.isfile(cert):
        return "http://127.0.0.1:%d/api/ui/new" % port, [], None
    if os.path.isfile(local_crt) and os.path.isfile(ca):
        return "https://127.0.0.1:%d/api/ui/new" % port, ["--cacert", ca_copy], ca
    if name:                                   # the Tailscale certificate, reached on 127.0.0.1 under its name
        return "https://%s:%d/api/ui/new" % (name, port), ["--resolve", "%s:%d:127.0.0.1" % (name, port)], None
    return None


def desktop_app(port, hosts, roots=None):
    """The Glimpse window installed from a Chromium browser (Chrome / Edge / Brave: 安装 / 创建快捷方式 → 窗口):
    (app path, its address). The address matters: the login cookie belongs to the one it was installed from.
    None when there is none."""
    from urllib.parse import urlsplit
    roots = roots or [os.path.expanduser("~/Applications"), "/Applications"]
    found = []
    for r in roots:
        try:
            subs = [r] + [os.path.join(r, d) for d in os.listdir(r) if d.endswith(" Apps.localized")]
        except OSError:
            continue
        for d in subs:
            try:
                apps = [os.path.join(d, a) for a in os.listdir(d) if a.endswith(".app")]
            except OSError:
                continue
            for a in apps:
                try:
                    with open(os.path.join(a, "Contents", "Info.plist"), "rb") as f:
                        url = plistlib.load(f).get("CrAppModeShortcutURL") or ""
                    u = urlsplit(url)
                    if u.scheme == "https" and u.port == port and (u.hostname or "") in hosts:
                        found.append((os.path.getmtime(a), a, "https://%s/" % u.netloc))
                except (OSError, ValueError, plistlib.InvalidFileException):
                    continue
    return max(found)[1:] if found else None


def link_target(cfg):
    """(hub address, app or None) for links opened on this computer: the installed Glimpse window and its
    address when there is one, else the default browser and the address `gli url` prints."""
    import server
    base = server.hub_base_url(cfg)
    listen, name = server.resolve_hub(cfg)
    port = (listen[0] if listen else {"port": 8443})["port"]
    hosts = {x["host"] for x in listen} | {"127.0.0.1", "localhost"} | ({name} if name else set())
    app = desktop_app(port, hosts) if sys.platform == "darwin" else None
    return (app[1], app[0]) if app else (base, None)


def workflow(base, mid, app=None, relay=None):
    """(Info.plist, document.wflow) as dicts."""
    info = {
        "NSServices": [{
            "NSMenuItem": {"default": MENU},
            "NSMessage": "runWorkflowAsService",
            "NSRequiredContext": {"NSApplicationIdentifier": "com.apple.finder"},
            "NSSendFileTypes": ["public.folder"],     # folders only: telling a file from a folder would need a stat
        }],
        "CFBundleName": NAME,
        "CFBundleIdentifier": BUNDLE_ID,
    }
    action = {
        "action": {
            "AMAccepts": {"Container": "List", "Optional": True, "Types": ["com.apple.cocoa.path"]},
            "AMActionVersion": "2.0.3",
            "AMApplication": ["Automator"],
            "AMParameterProperties": {"COMMAND_STRING": {}, "CheckedForUserDefaultShell": {}, "inputMethod": {},
                                      "shell": {}, "source": {}},
            "AMProvides": {"Container": "List", "Types": ["com.apple.cocoa.string"]},
            "ActionBundlePath": "/System/Library/Automator/Run Shell Script.action",
            "ActionName": "Run Shell Script",
            "ActionParameters": {
                "COMMAND_STRING": script(base, mid, app, relay),
                "CheckedForUserDefaultShell": True,
                "inputMethod": 1,                      # 1 = as arguments
                "shell": "/bin/sh",
                "source": "",
            },
            "BundleIdentifier": "com.apple.RunShellScript",
            "CFBundleVersion": "2.0.3",
            "CanShowSelectedItemsWhenRun": False,
            "CanShowWhenRun": True,
            "Category": ["AMCategoryUtilities"],
            "Class Name": "RunShellScriptAction",
            "InputUUID": "0B9A1C5E-3F1D-4C8E-9E9A-6C2B5A7D1E01",
            "Keywords": ["Shell", "Script", "Command", "Run", "Unix"],
            "OutputUUID": "0B9A1C5E-3F1D-4C8E-9E9A-6C2B5A7D1E02",
            "UUID": "0B9A1C5E-3F1D-4C8E-9E9A-6C2B5A7D1E03",
            "UnlocalizedApplications": ["Automator"],
            "arguments": {},
            "isViewVisible": 1,
            "location": "300.000000:250.000000",
            "nibPath": "/System/Library/Automator/Run Shell Script.action/Contents/Resources/Base.lproj/main.nib",
        },
        "isViewVisible": 1,
    }
    doc = {
        "AMApplicationBuild": "523",
        "AMApplicationVersion": "2.10",
        "AMDocumentVersion": "2",
        "actions": [action],
        "connectors": {},
        "workflowMetaData": {
            "applicationBundleIDsByPath": {},
            "applicationPaths": [],
            "inputTypeIdentifier": "com.apple.Automator.fileSystemObject",
            "outputTypeIdentifier": "com.apple.Automator.nothing",
            "presentationMode": 15,
            "processesInput": False,
            "serviceApplicationBundleID": "com.apple.finder",
            "serviceApplicationPath": "/System/Library/CoreServices/Finder.app",
            "serviceInputTypeIdentifier": "com.apple.Automator.fileSystemObject",
            "serviceOutputTypeIdentifier": "com.apple.Automator.nothing",
            "serviceProcessesInput": False,
            "systemImageName": "NSActionTemplate",
            "useAutomaticInputType": False,
            "workflowTypeIdentifier": "com.apple.Automator.servicesMenu",
        },
    }
    return info, doc


def write(dest, base, mid, app=None, relay=None, ca=None):
    """Write the .workflow bundle at `dest` (replacing an older one of ours). The document holds the Quick
    Action's token, so the bundle is readable by you only. ca: the 127.0.0.1 CA certificate to copy into it (the
    action can't read data/ under ~/Documents)."""
    info, doc = workflow(base, mid, app, relay)
    contents = os.path.join(dest, "Contents")
    os.makedirs(os.path.join(contents, "Resources"), mode=0o700, exist_ok=True)
    os.chmod(dest, 0o700)
    for name, obj in (("Info.plist", info), ("document.wflow", doc)):
        p = os.path.join(contents, name)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            plistlib.dump(obj, f)
        os.chmod(p, 0o600)
    if ca:
        shutil.copyfile(ca, os.path.join(contents, "Resources", "local-ca.crt"))
    return dest


def ours(dest):
    try:
        with open(os.path.join(dest, "Contents", "Info.plist"), "rb") as f:
            return plistlib.load(f).get("CFBundleIdentifier") == BUNDLE_ID
    except (OSError, ValueError):
        return False


def refresh_services():
    # let Finder pick the new menu entry up without logging out (harmless when it fails)
    try:
        subprocess.run(["/System/Library/CoreServices/pbs", "-update"], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        pass


def load_cfg():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import config as C
        __import__("server")                     # needs the .venv (aiohttp)
    except ImportError as e:
        raise SystemExit("run `gli setup` first (%s)" % e)
    return C.load()


def post_ui_new(cfg, cwd, mid, token):
    """POST /api/ui/new over 127.0.0.1 (verification off: loopback only, like `gli attach`)."""
    import json
    import ssl
    import urllib.request
    import server
    listen = server.resolve_hub(cfg)[0]
    lo = next((x for x in listen if server.is_loopback(x["host"])), None)
    if not lo:
        raise SystemExit("gli new: the hub doesn't listen on 127.0.0.1 (hub.listen)")
    tls = os.path.isfile(server.cert_paths()[0])
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    req = urllib.request.Request("%s://127.0.0.1:%d/api/ui/new" % ("https" if tls else "http", lo["port"]),
                                 data=json.dumps({"cwd": cwd, "m": mid or None}).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
    try:
        with opener.open(req, timeout=5) as r:
            return json.loads(r.read() or b"{}")
    except OSError as e:
        raise SystemExit("gli new: cannot reach the hub on 127.0.0.1:%d (%s). Is `gli hub` running?" % (lo["port"], e))


def cmd_new(path):
    """gli new PATH: the new-session form for PATH, in the Glimpse window if one is installed."""
    cfg = load_cfg()
    import config as C
    import server
    base, app = link_target(cfg)
    try:
        url = server.new_session_url(cfg, path, base)
    except ValueError as e:
        raise SystemExit("gli new: %s" % e)
    if sys.platform == "darwin" and app:
        post_ui_new(cfg, server.session_dir(path), server.local_machine_id(cfg), C.token())
        os.execv("/usr/bin/open", ["open", "-a", app])
    if sys.platform == "darwin":
        os.execv("/usr/bin/open", ["open", url])
    if shutil.which("xdg-open"):
        os.execvp("xdg-open", ["xdg-open", url])
    print(url)


def main(argv):
    cmd = argv[0] if argv else ""
    if cmd == "new":
        return cmd_new(argv[1] if len(argv) > 1 else ".")
    if sys.platform != "darwin":
        raise SystemExit("the Finder Quick Action is macOS only; use `gli new PATH` instead")
    dest = os.path.join(SERVICES, NAME + ".workflow")
    if cmd == "install":
        if os.path.exists(dest) and not ours(dest):
            raise SystemExit("%s exists and is not Glimpse's; not touching it" % dest)
        cfg = load_cfg()
        import config as C
        import server
        base, app = link_target(cfg)
        mid = server.local_machine_id(cfg)
        relay = ca = None
        if app:
            r = hub_relay(cfg, os.path.join(dest, "Contents", "Resources", "local-ca.crt"))
            if r:
                # the Quick Action's own token: it can only ask for this form, and only from this computer
                import hashlib
                import json
                import secrets
                tok = secrets.token_urlsafe(24)
                C.write_secret(os.path.join(C.DATA, "finder.json"),
                               json.dumps({"token_sha256": hashlib.sha256(tok.encode()).hexdigest()}))
                relay, ca = (r[0], r[1], tok), r[2]
        write(dest, base, mid, app, relay, ca)
        refresh_services()
        print("installed %s\n  opens the form in %s, machine %s" % (
            dest, app + " (through the hub)" if relay else base + " in the default browser", mid or "-"))
        print("Finder: right-click a folder → 快速操作 (or 服务) → %s" % MENU)
        print("not in the menu? turn on “%s” in System Settings → 键盘 → 键盘快捷键 → 服务 "
              "(or 隐私与安全性 → 扩展 → 访达)" % MENU)
    elif cmd == "uninstall":
        if not os.path.exists(dest):
            print("not installed")
        elif not ours(dest):
            raise SystemExit("%s is not Glimpse's; not touching it" % dest)
        else:
            shutil.rmtree(dest)
            refresh_services()
            print("removed " + dest)
    else:
        raise SystemExit("usage: finder.py new [PATH] | install | uninstall")


if __name__ == "__main__":
    main(sys.argv[1:])
