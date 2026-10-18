"""macOS Finder Quick Action "用 Glimpse 新建会话": right-click a folder (or a file: its folder) in Finder, and the
web app's new-session form opens with that path filled in (`gli new PATH`). Nothing is started until 创建.

    gli finder-install      write ~/Library/Services/Glimpse 新建会话.workflow
    gli finder-uninstall    remove it

The workflow is a plain Automator document with one "Run Shell Script" action; the selected path reaches the
script as an argument ("$@"), never through a shell string. Standard library only (runs without the .venv)."""
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


def script(gli, path_env):
    # Automator runs this with a minimal PATH (no Homebrew): keep the installer's PATH so `tailscale` is found and
    # the link uses the hub's real host name (its certificate and your login cookie belong to that name)
    return ("export PATH=%s\n"
            "exec %s new \"${1:-$HOME}\"\n") % (shlex.quote(path_env), shlex.quote(gli))


def workflow(gli, path_env):
    """(Info.plist, document.wflow) as dicts."""
    info = {
        "NSServices": [{
            "NSMenuItem": {"default": MENU},
            "NSMessage": "runWorkflowAsService",
            "NSRequiredContext": {"NSApplicationIdentifier": "com.apple.finder"},
            "NSSendFileTypes": ["public.item"],
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
                "COMMAND_STRING": script(gli, path_env),
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


def write(dest, gli, path_env):
    """Write the .workflow bundle at `dest` (replacing an older one of ours)."""
    info, doc = workflow(gli, path_env)
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


def main(argv):
    if sys.platform != "darwin":
        raise SystemExit("the Finder Quick Action is macOS only; use `gli new PATH` instead")
    cmd = argv[0] if argv else ""
    dest = os.path.join(SERVICES, NAME + ".workflow")
    if cmd == "install":
        gli = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gli")
        if os.path.exists(dest) and not ours(dest):
            raise SystemExit("%s exists and is not Glimpse's; not touching it" % dest)
        write(dest, gli, os.environ.get("PATH") or "/usr/bin:/bin:/usr/sbin:/sbin")
        refresh_services()
        print("installed %s\nFinder: right-click a folder → 快速操作 (or 服务) → %s" % (dest, MENU))
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
