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


def script(base, mid):
    """base = the hub's URL ("https://host:port/"), mid = this computer's machine id (may be "")."""
    from urllib.parse import quote
    m = "&m=" + quote(mid, safe="") if mid else ""
    return ("[ -n \"$1\" ] || exit 0\n"
            "q=$(/usr/bin/osascript -l JavaScript -e 'function run(a) { return encodeURIComponent(a[0]) }' \"$1\") || exit 1\n"
            "exec /usr/bin/open %s\"$q\"%s\n") % (shlex.quote(base + "#new?cwd="), shlex.quote(m) if m else "")


def workflow(base, mid):
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
                "COMMAND_STRING": script(base, mid),
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


def write(dest, base, mid):
    """Write the .workflow bundle at `dest` (replacing an older one of ours)."""
    info, doc = workflow(base, mid)
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


def hub_link():
    """The hub's URL (as `gli url` prints it: its certificate and your login cookie belong to that name) and this
    computer's machine id, from config.json."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import config as C
        import server
    except ImportError as e:
        raise SystemExit("run `gli setup` first (%s)" % e)
    cfg = C.load()
    return server.hub_base_url(cfg), server.local_machine_id(cfg)


def main(argv):
    if sys.platform != "darwin":
        raise SystemExit("the Finder Quick Action is macOS only; use `gli new PATH` instead")
    cmd = argv[0] if argv else ""
    dest = os.path.join(SERVICES, NAME + ".workflow")
    if cmd == "install":
        if os.path.exists(dest) and not ours(dest):
            raise SystemExit("%s exists and is not Glimpse's; not touching it" % dest)
        base, mid = hub_link()
        write(dest, base, mid)
        refresh_services()
        print("installed %s (opens %s, machine %s)" % (dest, base, mid or "-"))
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
        raise SystemExit("usage: finder.py install|uninstall")


if __name__ == "__main__":
    main(sys.argv[1:])
