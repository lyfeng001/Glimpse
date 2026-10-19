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


def script(base, mid, app=None):
    """base = the hub's URL ("https://host:port/"), mid = this computer's machine id (may be ""), app = the
    installed Glimpse window (a browser's web app) to open the link in, else the default browser."""
    from urllib.parse import quote
    m = "&m=" + quote(mid, safe="") if mid else ""
    return ("[ -n \"$1\" ] || exit 0\n"
            "q=$(/usr/bin/osascript -l JavaScript -e 'function run(a) { return encodeURIComponent(a[0]) }' \"$1\") || exit 1\n"
            "exec /usr/bin/open %s%s\"$q\"%s\n") % ("-a %s " % shlex.quote(app) if app else "",
                                                  shlex.quote(base + "#new?cwd="), shlex.quote(m) if m else "")


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


def workflow(base, mid, app=None):
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
                "COMMAND_STRING": script(base, mid, app),
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


def write(dest, base, mid, app=None):
    """Write the .workflow bundle at `dest` (replacing an older one of ours)."""
    info, doc = workflow(base, mid, app)
    contents = os.path.join(dest, "Contents")
    os.makedirs(contents, exist_ok=True)
    with open(os.path.join(contents, "Info.plist"), "wb") as f:
        plistlib.dump(info, f)
    with open(os.path.join(contents, "document.wflow"), "wb") as f:
        plistlib.dump(doc, f)
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


def cmd_new(path):
    """gli new PATH: the new-session form for PATH, in the Glimpse window if one is installed."""
    cfg = load_cfg()
    import server
    base, app = link_target(cfg)
    try:
        url = server.new_session_url(cfg, path, base)
    except ValueError as e:
        raise SystemExit("gli new: %s" % e)
    if sys.platform == "darwin":
        os.execv("/usr/bin/open", ["open"] + (["-a", app] if app else []) + [url])
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
        import server
        base, app = link_target(cfg)
        mid = server.local_machine_id(cfg)
        write(dest, base, mid, app)
        refresh_services()
        print("installed %s\n  opens %s in %s, machine %s" % (dest, base, app or "the default browser", mid or "-"))
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
