#!/usr/bin/env python3
"""glimpse agent: runs on every machine. Python 3.8+, standard library only.

Sub-commands
  daemon              PTY session daemon (auto-started by the other commands)
  bridge              stdio <-> daemon socket; the hub runs this locally or over ssh
  hook                Claude Code hook relay (hook JSON on stdin -> daemon)
  ls                  list sessions on this machine
  run [opts] [-- cmd] create a session and attach this terminal to it
  run KIND --resume ID  continue an existing claude / codex conversation in a new session
  attach [SID]        attach this terminal to a session, default the most recent live one (detach: Ctrl-])
  adopt [claude|codex] pick a recent conversation on this machine and continue it in a session
  history [KIND]      list recent claude / codex conversations on this machine
  kill SID            terminate a session
"""
import base64
import calendar
import fcntl
import glob
import hashlib
import json
import os
import pty
import re
import select
import selectors
import shlex
import signal
import socket
import struct
import sys
import termios
import time

VERSION = "0.4.1"


def code_id(src):
    """Short hash of the agent source. The daemon outlives the hub (and code updates), so it reports the code
    it runs in hello; the hub compares it with the source it has and replaces an outdated daemon."""
    return hashlib.sha256(src).hexdigest()[:12]


try:
    with open(os.path.abspath(__file__), "rb") as _f:
        CODE_ID = code_id(_f.read())
except (NameError, OSError):
    CODE_ID = ""                  # exec'd from stdin over ssh: the hub passes it with `bridge --code`
RING_MAX = 2 * 1024 * 1024        # raw terminal bytes kept per session
DEFAULT_REPLAY = 256 * 1024       # bytes replayed to a subscriber that has no offset
CHAT_MAX = 3000                   # parsed transcript items kept per session
EVENTS_MAX = 300
EXITED_MAX = 30
STRIP_ENV = ("CLAUDECODE", "CLAUDE_CODE_", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_PROJECT_DIR", "GLIMPSE_")
CLAUDE_HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Notification",
                      "PermissionRequest", "Stop", "StopFailure", "SessionEnd")
CODEX_FLAGS = ["-c", "tui.notifications=true", "-c", 'tui.notification_method="osc9"',
               "-c", 'tui.notification_condition="always"']
DEFAULT_PRESETS = {"claude": {"argv": ["claude"]}, "codex": {"argv": ["codex"]}, "shell": {"argv": []}}
DETACH_KEY = b"\x1d"              # Ctrl-]
HISTORY_ACTIVE = 120              # a transcript written this recently is probably still open elsewhere
HISTORY_DAYS = 14                 # codex rollouts older than this are not offered for adoption
RESUME_IMPORT = 200               # chat items imported from a resumed codex rollout
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")       # resume ids end up in argv
VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,99}$")  # model names etc.
CLAUDE_MODES = ("default", "acceptEdits", "auto", "plan", "bypass", "manual", "dontAsk")
CLAUDE_MODE_ALIAS = {"bypassPermissions": "bypass"}
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultracode")
CODEX_OVERRIDES = ("model", "effort", "approval", "sandbox")
CODEX_EFFORT_LABELS = {"xhigh": "extra high"}           # phone value -> label in codex's /model picker
# presets of codex's /permissions picker -> (approval, sandbox); other combinations need a restart with -a / -s
CODEX_PERMISSION_PRESETS = {"read only": ("on-request", "read-only"), "ask for approval": ("on-request", "workspace-write"),
                            "approve for me": (None, "workspace-write"), "full access": ("never", "danger-full-access")}
SHIFT_TAB = b"\x1b[Z"
# op "models": what the pickers on the phone offer
CLAUDE_MODEL_LIST = (("default", "Default"), ("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku"),
                     ("fable", "Fable"), ("opusplan", "Opus Plan"))
CLAUDE_MODEL_ENV = {"opus": "ANTHROPIC_DEFAULT_OPUS_MODEL", "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
                    "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL"}
CLAUDE_MODE_LIST = ("manual", "acceptEdits", "auto", "plan", "dontAsk", "bypassPermissions")
EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
MODELS_TTL = 600                  # codex `debug models` result cache (s)
MODELS_TIMEOUT = 30
MENU_SETTLE = 0.15                # the screen is parsed for a menu once output paused this long
MENU_MAX_WAIT = 0.5               # ... or at least this often while output never pauses (claude's animated logo)
MENU_KEYS = {"esc": b"\x1b", "s": b"s"}
INIT_CMDS_MAX = 4


def project_dir():
    if "__file__" not in globals():
        return os.path.expanduser("~/.glimpse")
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def state_dir():
    # all local state lives inside the project directory (data/agent)
    d = os.environ.get("GLIMPSE_STATE") or os.path.join(project_dir(), "data", "agent")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def sock_path():
    return os.environ.get("GLIMPSE_SOCK") or os.path.join(state_dir(), "agent.sock")


def now():
    return time.time()


def b64e(b):
    return base64.b64encode(b).decode("ascii")


def set_nonblock(fd):
    fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) | os.O_NONBLOCK)


def set_cloexec(fd):
    fcntl.fcntl(fd, fcntl.F_SETFD, fcntl.fcntl(fd, fcntl.F_GETFD) | fcntl.FD_CLOEXEC)


def clip(s, n):
    if not isinstance(s, str):
        s = json.dumps(s, ensure_ascii=False)
    return s if len(s) <= n else s[: n - 1] + "…"


def shrink(o, n=1000, depth=0):
    """Bound the size of hook payloads (tool inputs can be huge file contents)."""
    if isinstance(o, str):
        return clip(o, n)
    if depth > 4:
        return clip(o, n)
    if isinstance(o, dict):
        return {k: shrink(v, n, depth + 1) for k, v in list(o.items())[:50]}
    if isinstance(o, list):
        return [shrink(v, n, depth + 1) for v in o[:50]]
    return o


# --------------------------------------------------------------------------- terminal parsing

OSC_RE = re.compile(rb"\x1b\](\d+);([^\x07\x1b]*)(?:\x07|\x1b\\)")
QUERY_RE = re.compile(rb"\x1b\[6n|\x1b\[0?c|\x1b\]1[01];\?(?:\x07|\x1b\\)")
# responses a terminal emulator sends back for the queries above; stripped from phone input
RESPONSE_RE = re.compile(rb"\x1b\[\d+;\d+R|\x1b\[\?[\d;]*c|\x1b\]1[01];rgb:[^\x07\x1b]*(?:\x07|\x1b\\)")
TITLE_JUNK_RE = re.compile(r"^[^\w(\[{<\"'`]+", re.U)


def query_reply(q):
    if q == b"\x1b[6n":
        return b"\x1b[1;1R"
    if q.startswith(b"\x1b[") and q.endswith(b"c"):
        return b"\x1b[?62;22c"
    if q.startswith(b"\x1b]10"):
        return b"\x1b]10;rgb:e6e6/e6e6/e6e6\x1b\\"
    if q.startswith(b"\x1b]11"):
        return b"\x1b]11;rgb:1414/1414/1818\x1b\\"
    return b""


def clean_title(t):
    t = TITLE_JUNK_RE.sub("", t.strip())
    return t.strip()


# terminal reports (focus in/out, SGR mouse) the emulator sends on its own; not a sign of a user at the keys
REPORT_RE = re.compile(rb"\x1b\[[IO]|\x1b\[<\d+;\d+;\d+[Mm]")
VT_TOKEN_RE = re.compile(r"\x1b\[([0-9;?<>=]*)[ -/]*([@-~])|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|"
                         r"\x1b[P^_X][^\x1b]*\x1b\\|\x1b[()*+].|\x1b[^\[\]P^_X()*+]|[\x00-\x1f\x7f]|[^\x00-\x1f\x7f\x1b]+")
def is_user_keys(data):
    """Input that means someone is typing (Enter, Esc, arrows, Ctrl-C); terminal reports don't count."""
    data = REPORT_RE.sub(b"", RESPONSE_RE.sub(b"", data))
    return b"\r" in data or data[:1] == b"\x1b" or b"\x03" in data


MODE_RE = re.compile(r"(plan mode|accept edits|auto mode|bypass permissions|manual mode|don't ask mode|dontask mode) on",
                     re.I)
MODE_NAMES = {"plan mode": "plan", "accept edits": "acceptEdits", "auto mode": "auto",
              "bypass permissions": "bypass", "manual mode": "manual", "don't ask mode": "dontAsk",
              "dontask mode": "dontAsk"}


class Screen:
    """Minimal VT screen (text only) so the claude footer can be read even when the TUI
    redraws it with cell-level diffs. Handles cursor movement, erase, scrolling; ignores colours."""

    def __init__(self, cols, rows):
        import codecs
        self.dec = codecs.getincrementaldecoder("utf-8")("replace")
        self.rest = ""
        self.cols, self.rows = cols, rows
        self.x = self.y = 0
        self.saved = (0, 0)
        self.grid = [[" "] * cols for _ in range(rows)]

    def resize(self, cols, rows):
        g = [[" "] * cols for _ in range(rows)]
        for i, row in enumerate(self.grid[-rows:]):
            g[i][:min(cols, len(row))] = row[:cols]
        self.grid, self.cols, self.rows = g, cols, rows
        self.x, self.y = min(self.x, cols - 1), min(self.y, rows - 1)

    def lines(self):
        return ["".join(r).rstrip() for r in self.grid]

    def _lf(self):
        if self.y >= self.rows - 1:
            del self.grid[0]
            self.grid.append([" "] * self.cols)
        else:
            self.y += 1

    def _put(self, text):
        import unicodedata
        for ch in text:
            w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
            if self.x + w > self.cols:
                self.x = 0
                self._lf()
            self.grid[self.y][self.x] = ch
            if w == 2 and self.x + 1 < self.cols:
                self.grid[self.y][self.x + 1] = ""
            self.x += w

    def _csi(self, params, final):
        priv = params.startswith(("?", "<", ">", "="))
        nums = [int(p) if p.isdigit() else 0 for p in params.lstrip("?<>=").split(";")] if params else []
        n = (nums[0] if nums else 0) or 1
        if priv:
            if final in "hl" and nums and nums[0] in (47, 1047, 1049):
                self.grid = [[" "] * self.cols for _ in range(self.rows)]   # alternate screen switch
            return
        if final == "A":
            self.y = max(0, self.y - n)
        elif final in "Be":
            self.y = min(self.rows - 1, self.y + n)
        elif final in "Ca":
            self.x = min(self.cols - 1, self.x + n)
        elif final == "D":
            self.x = max(0, self.x - n)
        elif final in "EF":
            self.y = min(self.rows - 1, self.y + n) if final == "E" else max(0, self.y - n)
            self.x = 0
        elif final in "G`":
            self.x = min(self.cols - 1, n - 1)
        elif final == "d":
            self.y = min(self.rows - 1, n - 1)
        elif final in "Hf":
            self.y = min(self.rows - 1, (nums[0] if nums else 1) - 1 if nums and nums[0] else 0)
            self.x = min(self.cols - 1, (nums[1] - 1) if len(nums) > 1 and nums[1] else 0)
        elif final == "K":
            row, m = self.grid[self.y], nums[0] if nums else 0
            a, b = (self.x, self.cols) if m == 0 else (0, self.x + 1) if m == 1 else (0, self.cols)
            row[a:b] = [" "] * (b - a)
        elif final == "J":
            m = nums[0] if nums else 0
            if m == 0:
                self._csi("0", "K")
                rng = range(self.y + 1, self.rows)
            elif m == 1:
                self._csi("1", "K")
                rng = range(0, self.y)
            else:
                rng = range(self.rows)
            for i in rng:
                self.grid[i] = [" "] * self.cols
        elif final in "LM":
            for _ in range(min(n, self.rows - self.y)):
                if final == "L":
                    self.grid.insert(self.y, [" "] * self.cols)
                    del self.grid[self.rows]
                else:
                    del self.grid[self.y]
                    self.grid.append([" "] * self.cols)
        elif final in "ST":
            for _ in range(min(n, self.rows)):
                if final == "S":
                    del self.grid[0]
                    self.grid.append([" "] * self.cols)
                else:
                    self.grid.insert(0, [" "] * self.cols)
                    del self.grid[self.rows]
        elif final == "P":
            row = self.grid[self.y]
            del row[self.x:self.x + n]
            row.extend([" "] * (self.cols - len(row)))
        elif final == "X":
            row = self.grid[self.y]
            row[self.x:self.x + n] = [" "] * len(row[self.x:self.x + n])
        elif final == "s":
            self.saved = (self.x, self.y)
        elif final == "u":
            self.x, self.y = self.saved

    def feed(self, data):
        text = self.rest + self.dec.decode(data)
        self.rest = ""
        i = text.rfind("\x1b")
        if i >= 0 and len(text) - i < 256:
            m = VT_TOKEN_RE.match(text, i)
            if not m or m.group(0) == "\x1b" or (m.end() == len(text) and text[i + 1:i + 2] == "]" and
                                                  not text.endswith(("\x07", "\x1b\\"))):
                text, self.rest = text[:i], text[i:]     # incomplete escape sequence: wait for more
        for m in VT_TOKEN_RE.finditer(text):
            tok = m.group(0)
            c = tok[0]
            if c == "\x1b":
                if m.group(2):
                    self._csi(m.group(1) or "", m.group(2))
                elif tok == "\x1b7":
                    self.saved = (self.x, self.y)
                elif tok == "\x1b8":
                    self.x, self.y = self.saved
                elif tok == "\x1bM":
                    if self.y == 0:
                        self.grid.insert(0, [" "] * self.cols)
                        del self.grid[self.rows]
                    else:
                        self.y -= 1
                elif tok == "\x1bc":
                    self.grid = [[" "] * self.cols for _ in range(self.rows)]
                    self.x = self.y = 0
            elif c == "\r":
                self.x = 0
            elif c == "\n" or c in "\x0b\x0c":
                self._lf()
            elif c == "\b":
                self.x = max(0, self.x - 1)
            elif c == "\t":
                self.x = min(self.cols - 1, (self.x // 8 + 1) * 8)
            elif c >= " " and c != "\x7f":
                self._put(tok)

    def claude_mode(self):
        """Permission mode shown in claude's footer: plan/acceptEdits/auto/bypass/manual/default, None = not visible."""
        for ln in reversed(self.lines()):
            m = MODE_RE.search(ln)
            if m:
                return MODE_NAMES[m.group(1).lower()]
            if "for shortcuts" in ln:
                return "default"
        return None

    def menu(self):
        return parse_menu(self.lines())


# interactive pickers drawn by claude / codex (/model, /effort, approval prompts ...)
MENU_FOOTER_RE = re.compile(r"esc to cancel|esc back|enter to confirm|enter select", re.I)
MENU_OPT_RE = re.compile(r"^(\s*)([❯›>▶]\s*)?(\d{1,2})\.\s+(\S.*?)(?:\s{2,}(\S.*))?$")
MENU_CURRENT_RE = re.compile(r"\s*(✔|✓|\(current\))\s*")
BOX_ONLY_RE = re.compile(r"^[\s─-▟←-⇿·•●○◯…-]*$")
BOX_RUN_RE = re.compile(r"[─-▟]{3}")      # separator lines (also with a status label in them)
SLIDER_WORDS = set(EFFORT_ORDER) | {"ultracode"}
MENU_MAX_OPTS = 20
MENU_MAX_STR = 200


def _menu_str(s):
    return clip(s.strip(), MENU_MAX_STR)


# claude's AskUserQuestion with several questions: a tab bar "←  ☐ A  ☒ B  ✔ Submit  →" above the current
# question; after the last one a "Review your answers" page with "1. Submit answers / 2. Cancel"
QTABS_RE = re.compile(r"^\s*←\s+(.*[☐☒].*?)\s+→\s*$")
QREVIEW_RE = re.compile(r"review your answers", re.I)


def ask_item(q):
    """One AskUserQuestion question -> what the phone shows."""
    return {"question": clip(q.get("question", ""), 600), "header": clip(q.get("header", ""), 60),
            "options": [{"label": clip(o.get("label", ""), 120), "description": clip(o.get("description", ""), 300)}
                        for o in (q.get("options") or []) if isinstance(o, dict)][:9],
            "multi": bool(q.get("multiSelect"))}


def _norm(s):
    return re.sub(r"\s+", "", s or "")


def question_progress(lines, items):
    """Where claude's multi-question picker is: {"qi", "answered", "review", "submit", "summary"}, or None when
    the picker is not on the screen (keep the last known state)."""
    bar = None
    for i in range(len(lines) - 1, -1, -1):
        m = QTABS_RE.match(lines[i])
        if m:
            bar = (i, [t for t in re.split(r"\s{2,}", m.group(1).strip()) if t])
            break
    if bar is None:
        return None
    i, tabs = bar
    tabs = [t for t in tabs if t[:1] in ("☐", "☒")]
    answered = [t.startswith("☒") for t in tabs][:len(items)]
    answered += [False] * (len(items) - len(answered))
    below = [ln for ln in lines[i + 1:i + 40]]
    if any(QREVIEW_RE.search(ln) for ln in below[:4]):
        menu = parse_menu(lines)
        submit = [{"n": o["i"] + 1, "label": o["label"]} for o in (menu or {}).get("options") or []] or \
            [{"n": 1, "label": "Submit answers"}, {"n": 2, "label": "Cancel"}]
        # the answers as listed ("● question" / "→ answer"); the list is redrawn in parts, so it is matched
        # back to the known questions and an answer that is not on the screen stays ""
        got, cur = {}, None
        for ln in below:
            t = ln.strip()
            if MENU_OPT_RE.match(ln) or MENU_FOOTER_RE.search(ln):
                break
            if t.startswith("●"):
                cur = _norm(t.lstrip("● "))
            elif t.startswith("→") and cur is not None:
                got[cur] = clip(t.lstrip("→ ").strip(), 300)
        summary = [{"q": it["question"], "a": got.get(_norm(it["question"]), "")} for it in items]
        return {"qi": len(items) - 1, "answered": answered, "review": True, "submit": submit[:4], "summary": summary}
    # the current question: its text is the first non-empty line under the bar (long ones wrap)
    head = next((_norm(ln) for ln in below if ln.strip()), "")
    best, best_n = None, 0
    for k, it in enumerate(items):
        q = _norm(it["question"])
        n = 0
        while n < min(len(q), len(head)) and q[n] == head[n]:
            n += 1
        if n > best_n:
            best, best_n = k, n
    if best is None or best_n < min(6, len(head)):
        best = next((k for k, a in enumerate(answered) if not a), len(items) - 1)
    return {"qi": best, "answered": answered, "review": False}


def parse_menu(lines):
    """Screen text -> {"kind": "list"|"slider", "title", "options", "cursor", "hint"} or None (no / unsure)."""
    foot = None
    for i in range(len(lines) - 1, -1, -1):
        if MENU_FOOTER_RE.search(lines[i]):
            foot = i
            break
    if foot is None:
        return None
    hint = _menu_str(lines[foot])
    top = max(0, foot - 40)
    return _parse_slider(lines, top, foot, hint) or _parse_list(lines, top, foot, hint)


def _menu_title(lines, first, top):
    """First line of the paragraph right above the options (claude: "Select model" + a description)."""
    j = first - 1
    while j >= top and (not lines[j].strip() or BOX_ONLY_RE.match(lines[j]) or BOX_RUN_RE.search(lines[j])):
        j -= 1
    if j < top:
        return ""
    k = j
    while k - 1 >= top and lines[k - 1].strip() and not BOX_RUN_RE.search(lines[k - 1]) and j - k < 3:
        k -= 1
    return _menu_str(lines[k])


def _parse_list(lines, top, foot, hint):
    blocks, cur = [], None
    for i in range(top, foot):
        ln = lines[i]
        m = MENU_OPT_RE.match(ln)
        if m:
            indent = len(m.group(1)) + len(m.group(2) or "")
            if cur is None:
                cur = {"first": i, "opts": [], "indent": indent}
                blocks.append(cur)
            cur["opts"].append({"n": int(m.group(3)), "label": m.group(4), "desc": m.group(5) or "",
                                "mark": bool(m.group(2)), "line": i})
        elif cur is not None and ln.strip() and len(ln) - len(ln.lstrip()) > cur["indent"] \
                and not BOX_ONLY_RE.match(ln):
            o = cur["opts"][-1]
            o["desc"] = (o["desc"] + " " + ln.strip()).strip()     # wrapped description
        else:
            cur = None
    if not blocks:
        return None
    b = blocks[-1]                    # the block nearest the footer
    opts = b["opts"]
    if [o["n"] for o in opts] != list(range(opts[0]["n"], opts[0]["n"] + len(opts))) or len(opts) > MENU_MAX_OPTS:
        return None
    marks = [k for k, o in enumerate(opts) if o["mark"]]
    if len(marks) != 1:
        return None
    out = []
    for k, o in enumerate(opts):
        label, current = o["label"], False
        if MENU_CURRENT_RE.search(label):
            current = True
            label = MENU_CURRENT_RE.sub(" ", label).strip()
        if MENU_CURRENT_RE.search(o["desc"]):
            current = True
            o["desc"] = MENU_CURRENT_RE.sub(" ", o["desc"]).strip()
        out.append({"i": k, "label": _menu_str(label), "desc": _menu_str(o["desc"]), "current": current})
    return {"kind": "list", "title": _menu_title(lines, b["first"], top), "options": out,
            "cursor": marks[0], "hint": hint}


def _parse_slider(lines, top, foot, hint):
    for i in range(foot - 1, top - 1, -1):
        # leading run of effort words, 2+ spaces apart (claude may put e.g. "Tab to toggle" after them)
        words, pos, ln = [], 0, lines[i]
        for m in re.finditer(r"\S+(?: \S+)*", ln):
            if m.group(0) not in SLIDER_WORDS or (words and m.start() - pos < 2):
                break
            words.append((m.start(), m.group(0)))
            pos = m.end()
        if len(words) < 3:
            continue
        for j in (i - 1, i + 1):
            if top <= j < foot and "▲" in lines[j]:
                col = lines[j].index("▲")
                cursor = min(range(len(words)), key=lambda k: abs(words[k][0] + len(words[k][1]) / 2.0 - col))
                # skip scale captions like "Faster ...... Smarter" above the track
                t = min(i, j) - 1
                while t >= top and (not lines[t].strip() or BOX_ONLY_RE.match(lines[t]) or
                                    re.search(r"\S\s{4,}\S", lines[t].strip())):
                    t -= 1
                return {"kind": "slider", "title": _menu_title(lines, t + 1, top),
                        "options": [{"i": k, "label": w, "desc": ""} for k, (_, w) in enumerate(words)],
                        "cursor": cursor, "hint": hint}
    return None


# --------------------------------------------------------------------------- claude transcript

def summarize_tool(name, inp):
    if not isinstance(inp, dict):
        return name
    for k in ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt"):
        v = inp.get(k)
        if isinstance(v, str) and v:
            return "%s: %s" % (name, clip(v.replace("\n", " "), 200))
    if name == "AskUserQuestion":
        qs = inp.get("questions") or []
        if qs and isinstance(qs[0], dict):
            more = " (+%d 个问题)" % (len(qs) - 1) if len(qs) > 1 else ""
            return "Question: " + clip(qs[0].get("question", ""), 300) + more
    return name


def block_text(c):
    """tool_result content -> plain text"""
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out = []
        for b in c:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    out.append(b.get("text", ""))
                elif b.get("type") == "image":
                    out.append("[image]")
            elif isinstance(b, str):
                out.append(b)
        return "\n".join(out)
    return "" if c is None else str(c)


def parse_claude_line(d):
    """One transcript JSON object -> list of (role, text, extra) tuples."""
    t = d.get("type")
    if t not in ("user", "assistant") or d.get("isSidechain") or d.get("isMeta"):
        return []
    msg = d.get("message") or {}
    content = msg.get("content")
    out = []
    if t == "user":
        if isinstance(content, str):
            s = content.strip()
            if s.startswith("<local-command-caveat>") or s.startswith("<system-reminder>"):
                return []
            m = re.match(r"<command-name>(.*?)</command-name>", s, re.S)
            if m:
                args = re.search(r"<command-args>(.*?)</command-args>", s, re.S)
                cmd = m.group(1).strip() + ((" " + args.group(1).strip()) if args and args.group(1).strip() else "")
                return [("system", cmd, {})]
            m = re.match(r"<local-command-stdout>(.*?)</local-command-stdout>", s, re.S)
            if m:
                return [("system", clip(m.group(1).strip(), 1500), {})] if m.group(1).strip() else []
            if s:
                out.append(("user", s, {}))
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_result":
                    txt = clip(block_text(b.get("content")).strip(), 1500)
                    out.append(("result", txt, {"error": bool(b.get("is_error"))}))
                elif b.get("type") == "text":
                    s = (b.get("text") or "").strip()
                    if s and not s.startswith("<system-reminder>"):
                        out.append(("user", s, {}))
    else:
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for b in content or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and (b.get("text") or "").strip():
                out.append(("assistant", b["text"].strip(), {}))
            elif b.get("type") == "tool_use":
                out.append(("tool", summarize_tool(b.get("name", "tool"), b.get("input")), {"tool": b.get("name")}))
    return out


class Tail:
    """Incrementally reads appended JSONL lines from a file."""

    def __init__(self, path, from_start=True):
        self.path = path
        self.pos = 0
        self.buf = b""
        self.ino = None
        if not from_start:
            try:
                self.pos = os.path.getsize(path)
            except OSError:
                pass

    def read(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return []
        if self.ino is not None and st.st_ino != self.ino or st.st_size < self.pos:
            self.pos, self.buf = 0, b""
        self.ino = st.st_ino
        if st.st_size == self.pos:
            return []
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read(8 * 1024 * 1024)
        except OSError:
            return []
        self.pos += len(data)
        self.buf += data
        lines = self.buf.split(b"\n")
        self.buf = lines.pop()
        out = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return out


# --------------------------------------------------------------------------- hooks

# Runs inside claude's hook. PermissionRequest blocks until the phone answers (or the wait
# expires / someone types in the terminal) and then prints claude's decision JSON.
HOOK_RELAY = r'''
import json,os,socket,sys
try:
    ev=json.loads(sys.stdin.read() or "{}")
    p=os.environ.get("GLIMPSE_SOCK","");sid=os.environ.get("GLIMPSE_SID","")
    if p and sid:
        w=ev.get("hook_event_name")=="PermissionRequest"
        s=socket.socket(socket.AF_UNIX);s.settimeout(float(os.environ.get("GLIMPSE_APPROVAL_WAIT","600"))+10 if w else 5)
        s.connect("\0"+p[1:] if p.startswith("@") else p)
        if p.startswith("@") and hasattr(socket,"SO_PEERCRED"):
            import struct
            if struct.unpack("3i",s.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))[1]!=os.getuid():
                raise RuntimeError("foreign daemon")
        s.sendall((json.dumps({"op":"hook","sid":sid,"ev":ev,"wait":w})+"\n").encode("utf-8","replace"))
        b=b""
        while b"\n" not in b:
            c=s.recv(65536)
            if not c:break
            b+=c
        r=json.loads(b.split(b"\n")[0] or b"{}")
        if r.get("out"):sys.stdout.write(json.dumps(r["out"]))
except Exception:
    pass
'''


def hook_command():
    py = sys.executable or "python3"
    return "%s -c %s" % (shlex.quote(py), shlex.quote(HOOK_RELAY.strip()))


def claude_hook_settings(approval_wait):
    out = {}
    for ev in CLAUDE_HOOK_EVENTS:
        t = int(approval_wait) + 30 if ev == "PermissionRequest" else 10
        out[ev] = [{"hooks": [{"type": "command", "command": hook_command(), "timeout": t}]}]
    return out


def private_dir():
    """0700 directory for per-session files (settings with tokens). In bootstrap mode a temp dir."""
    global _PRIV
    try:
        return _PRIV
    except NameError:
        pass
    if BOOTSTRAP:
        import tempfile
        _PRIV = tempfile.mkdtemp(prefix="glimpse-")
    else:
        _PRIV = state_dir()
    return _PRIV


def prepare_claude_argv(argv, sid, sdir, env_extra=None, approval_wait=600, allow=None, key_file=None):
    """Merge our hooks into the --settings file the user launches claude with.

    claude only honours the last --settings flag, so the user's file (which carries
    their API endpoint/token env) is merged with our hooks into one settings file.
    """
    argv = list(argv)
    base, idx = {}, None
    for i, a in enumerate(argv):
        if a == "--settings" and i + 1 < len(argv):
            idx = i
        elif a.startswith("--settings="):
            idx = i
            argv[i:i + 1] = ["--settings", a.split("=", 1)[1]]
    if idx is not None:
        src = os.path.expanduser(argv[idx + 1])
        try:
            if src.lstrip().startswith("{"):
                base = json.loads(src)
            else:
                with open(src) as f:
                    base = json.load(f)
        except (OSError, ValueError) as e:
            raise RuntimeError("cannot read claude settings %s: %s" % (src, e))
        del argv[idx:idx + 2]
    merged = dict(base)
    hooks = {k: list(v) for k, v in (base.get("hooks") or {}).items()}
    for ev, entry in claude_hook_settings(approval_wait).items():
        hooks.setdefault(ev, []).extend(entry)
    merged["hooks"] = hooks
    if allow:
        perms = dict(merged.get("permissions") or {})
        perms["allow"] = list(perms.get("allow") or []) + [a for a in allow if a not in (perms.get("allow") or [])]
        merged["permissions"] = perms
    if env_extra:
        # settings env wins over the process env in claude, so a key swap must go here too
        merged["env"] = dict(merged.get("env") or {}, **env_extra)
    if key_file:
        # hot key: claude asks apiKeyHelper (every CLAUDE_HELPER_TTL_MS) instead of using a fixed token, so the
        # key can be swapped by rewriting key_file. A token in env would take precedence: drop it.
        e = {k: v for k, v in (merged.get("env") or {}).items() if k not in CLAUDE_KEY_VARS}
        e["CLAUDE_CODE_API_KEY_HELPER_TTL_MS"] = CLAUDE_HELPER_TTL_MS
        merged["env"] = e
        merged["apiKeyHelper"] = "cat " + shlex.quote(key_file)
    # always a 0600 file: the settings may carry API tokens and argv is world-readable via ps
    path = os.path.join(sdir or private_dir(), "claude-%s.json" % sid)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(merged, f)
    return argv[:1] + ["--settings", path] + argv[1:], path


# --------------------------------------------------------------------------- codex rollout

def parse_codex_line(d):
    """One codex rollout JSON object -> list of (role, text, extra) plus optional ("@done"/"@error", text)."""
    if d.get("type") != "event_msg":
        return []
    p = d.get("payload") or {}
    pt = p.get("type")
    if pt == "item_completed":
        it = p.get("item") or {}
        t = it.get("type")
        if t == "UserMessage":
            txt = "\n".join(c.get("text", "") for c in it.get("content") or [] if isinstance(c, dict))
            return [("user", txt.strip(), {})] if txt.strip() else []
        if t == "AgentMessage":
            c = it.get("content")
            txt = "\n".join(x.get("text", "") for x in c if isinstance(x, dict)) if isinstance(c, list) else str(c or "")
            return [("assistant", txt.strip(), {})] if txt.strip() else []
        if t == "CommandExecution":
            cmd = it.get("command")
            if isinstance(cmd, list):
                cmd = cmd[-1] if len(cmd) >= 3 and cmd[1] in ("-lc", "-c") else " ".join(cmd)
            out = [("tool", "Shell: " + clip(str(cmd).replace("\n", " "), 200), {"tool": "Shell"})]
            res = it.get("aggregated_output") or it.get("formatted_output") or ""
            ec = it.get("exit_code")
            if res or ec not in (None, 0):
                out.append(("result", clip(str(res).strip() + ("" if ec in (None, 0) else "\n[exit %s]" % ec), 1500),
                            {"error": ec not in (None, 0)}))
            return out
        if t == "FileChange":
            files = list((it.get("changes") or {}).keys())
            return [("tool", "Edit: " + clip(", ".join(os.path.basename(f) for f in files), 200), {"tool": "Edit"})]
        if t == "McpToolCall":
            return [("tool", "%s.%s" % (it.get("server"), it.get("tool")), {"tool": "Mcp"})]
        return []
    if pt == "user_message":
        m = (p.get("message") or "").strip()
        return [("user", m, {})] if m else []
    if pt == "agent_message":
        m = (p.get("message") or "").strip()
        return [("assistant", m, {})] if m else []
    if pt == "task_complete":
        err = p.get("error")
        if err:
            return [("@error", (err.get("message") if isinstance(err, dict) else str(err)) or "turn failed", {})]
        return [("@done", p.get("last_agent_message") or "", {})]
    if pt == "turn_aborted":
        return [("@aborted", p.get("reason") or "", {})]
    return []


def iso_ts(s):
    """'2026-10-06T12:53:28.476Z' (UTC) -> epoch seconds, None if unparsable."""
    if not isinstance(s, str) or len(s) < 19:
        return None
    try:
        t = calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None
    m = re.match(r"\.(\d+)", s[19:])
    return t + (float("0." + m.group(1)) if m else 0.0)


ROLLOUT_NAME_RE = re.compile(r"rollout-(\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d)")


def rollout_start(path, meta):
    """When a rollout began: session_meta.payload.timestamp, else the (local time) file name."""
    t = iso_ts(meta.get("timestamp"))
    if t is None:
        m = ROLLOUT_NAME_RE.search(os.path.basename(path))
        if m:
            try:
                t = time.mktime(time.strptime(m.group(1), "%Y-%m-%dT%H-%M-%S"))
            except (ValueError, OverflowError):
                t = None
    return t


def rollout_meta(path):
    """session_meta payload of a rollout file ({} if unreadable), None for subagent rollouts."""
    try:
        with open(path, "rb") as f:
            meta = json.loads(f.readline() or b"{}").get("payload") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    src = meta.get("source")
    if isinstance(src, dict) or meta.get("thread_source") == "subagent":
        return None
    return meta


def codex_day_dirs(since, days):
    root = os.path.expanduser("~/.codex/sessions")
    out = []
    for back in range(days):
        day = time.localtime(since - back * 86400)
        out.append(os.path.join(root, "%04d" % day.tm_year, "%02d" % day.tm_mon, "%02d" % day.tm_mday))
    return out


def find_codex_rollout(cwd, since, taken):
    """The rollout a freshly started codex writes: same cwd, started (not just modified) after spawn."""
    cands = []
    for d in codex_day_dirs(since, 2):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            p = os.path.join(d, n)
            if not n.endswith(".jsonl") or p in taken:
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_mtime < since - 2:
                continue
            meta = rollout_meta(p)
            if not meta:
                continue
            if os.path.realpath(meta.get("cwd") or "") != os.path.realpath(cwd):
                continue
            start = rollout_start(p, meta)
            if start is None or start < since - 2:
                continue     # an older conversation still being written by another codex
            cands.append((st.st_mtime, p))
    return max(cands)[1] if cands else None


def codex_override_argv(argv, ov):
    """Apply phone-chosen model / effort / approval / sandbox to a codex argv (replacing preset flags)."""
    drop = {"model": ("-m", "--model"), "approval": ("-a", "--ask-for-approval"), "sandbox": ("-s", "--sandbox")}
    out, i = list(argv[:1]), 1
    while i < len(argv):
        a = argv[i]
        hit = False
        for k, flags in drop.items():
            if ov.get(k) and a in flags:
                hit, i = True, i + 2
            elif ov.get(k) and any(a.startswith(f + "=") for f in flags if f.startswith("--")):
                hit, i = True, i + 1
            if hit:
                break
        if not hit and ov.get("effort") and a in ("-c", "--config") and i + 1 < len(argv) and \
                argv[i + 1].replace(" ", "").startswith("model_reasoning_effort="):
            hit, i = True, i + 2
        if not hit:
            out.append(a)
            i += 1
    if ov.get("model"):
        out += ["-m", ov["model"]]
    if ov.get("effort"):
        out += ["-c", 'model_reasoning_effort="%s"' % ov["effort"]]
    if ov.get("approval"):
        out += ["-a", ov["approval"]]
    if ov.get("sandbox"):
        out += ["-s", ov["sandbox"]]
    return out


def codex_mode(p, prev=None):
    """turn_context payload -> {"model", "effort", "approval", "sandbox"} (strings, "" if unknown)."""
    out = dict(prev or {})
    if p.get("model"):
        out["model"] = str(p["model"])
    if p.get("effort"):
        out["effort"] = str(p["effort"])
    ap = p.get("approval_policy")
    if isinstance(ap, str):
        out["approval"] = ap
    elif isinstance(ap, dict) and ap:
        out["approval"] = next(iter(ap))          # {"granular": {...}} -> "granular"
    sp = p.get("sandbox_policy")
    if isinstance(sp, dict) and sp.get("type"):
        out["sandbox"] = str(sp["type"])
    elif isinstance(sp, str):
        out["sandbox"] = sp
    return out


def read_jsonl_end(path, max_bytes=16 * 1024 * 1024):
    """Parse the last max_bytes of a JSONL file. Returns (objects, offset just after the last full line)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            start = max(0, size - max_bytes)
            f.seek(start)
            data = f.read(size - start)
    except OSError:
        return [], 0
    if start:
        nl = data.find(b"\n")
        data, start = (data[nl + 1:], start + nl + 1) if nl >= 0 else (b"", size)
    cut = data.rfind(b"\n") + 1
    out = []
    for ln in data[:cut].split(b"\n"):
        if ln.strip():
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
    return out, start + cut


def find_codex_rollout_by_id(tid):
    """Newest rollout file of a codex thread id, any date (resumed conversations)."""
    if not tid or not ID_RE.match(tid):
        return None
    root = os.path.expanduser("~/.codex/sessions")
    cands = []
    for p in glob.glob(os.path.join(glob.escape(root), "*", "*", "*", "rollout-*%s.jsonl" % tid)):
        try:
            cands.append((os.stat(p).st_mtime, p))
        except OSError:
            pass
    return max(cands)[1] if cands else None


# --------------------------------------------------------------------------- conversation history

def claude_title(d):
    """First real user prompt of a claude transcript line, or None."""
    if d.get("type") != "user" or d.get("isSidechain") or d.get("isMeta"):
        return None
    for role, text, _ in parse_claude_line(d):
        if role == "user" and not text.startswith("<"):
            return clip(text.replace("\n", " ").strip(), 80)
    return None


def scan_head(path, fn, max_lines=400, max_bytes=4 * 1024 * 1024):
    """Feed the first lines of a JSONL file to fn(obj) until it returns True."""
    try:
        with open(path, "rb") as f:
            n = 0
            for ln in f:
                n += len(ln)
                if n > max_bytes or max_lines <= 0:
                    return
                max_lines -= 1
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(d, dict) and fn(d):
                    return
    except OSError:
        pass


_HIST_CACHE = {}     # path -> ((size, mtime), item)


def _cached(path, st, build):
    key = (st.st_size, st.st_mtime)
    hit = _HIST_CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]
    item = build()
    _HIST_CACHE[path] = (key, item)
    return item


def claude_history():
    out = []
    for p in glob.glob(os.path.join(glob.escape(os.path.expanduser("~/.claude/projects")), "*", "*.jsonl")):
        if os.path.basename(p).startswith("agent-"):
            continue      # sidechain files of older claude versions
        try:
            st = os.stat(p)
        except OSError:
            continue

        def build(p=p):
            info = {"cwd": None, "title": None}

            def see(d):
                if info["cwd"] is None and isinstance(d.get("cwd"), str):
                    info["cwd"] = d["cwd"]
                if info["title"] is None:
                    info["title"] = claude_title(d)
                return info["cwd"] is not None and info["title"] is not None
            scan_head(p, see)
            if info["title"] is None:
                return None   # nothing was ever asked: not worth resuming
            return {"kind": "claude", "id": os.path.basename(p)[:-6], "cwd": info["cwd"] or "",
                    "title": info["title"]}
        it = _cached(p, st, build)
        if it:
            out.append(dict(it, updated=st.st_mtime))
    return out


def codex_title(d):
    rows = parse_codex_line(d)
    for role, text, _ in rows:
        if role == "user":
            return clip(text.replace("\n", " ").strip(), 80)
    return None


def codex_history(t=None):
    out = []
    t = now() if t is None else t
    for d in codex_day_dirs(t, HISTORY_DAYS):
        for p in glob.glob(os.path.join(glob.escape(d), "rollout-*.jsonl")):
            try:
                st = os.stat(p)
            except OSError:
                continue

            def build(p=p):
                info = {"meta": None, "title": None}

                def see(obj):
                    if info["meta"] is None:
                        if obj.get("type") != "session_meta":
                            return True
                        pl = obj.get("payload") or {}
                        src = pl.get("source")
                        if isinstance(src, dict) or pl.get("thread_source") == "subagent":
                            return True
                        info["meta"] = pl
                        return False
                    info["title"] = codex_title(obj)
                    return info["title"] is not None
                scan_head(p, see, max_lines=2000, max_bytes=16 * 1024 * 1024)
                m = info["meta"]
                if not m or not m.get("id") or not info["title"]:
                    return None
                return {"kind": "codex", "id": str(m["id"]), "cwd": m.get("cwd") or "", "title": info["title"]}
            it = _cached(p, st, build)
            if it:
                out.append(dict(it, updated=st.st_mtime))
    # a resumed conversation can have several files: keep the newest
    best = {}
    for it in out:
        if it["id"] not in best or it["updated"] > best[it["id"]]["updated"]:
            best[it["id"]] = it
    return list(best.values())


def list_history(kind=None, limit=30, owned=()):
    """Recent claude / codex conversations on this machine, newest first."""
    items = []
    if kind in (None, "claude"):
        items += claude_history()
    if kind in (None, "codex"):
        items += codex_history()
    t = now()
    owned = set(owned)
    items = [dict(it, active=t - it["updated"] < HISTORY_ACTIVE) for it in items if it["id"] not in owned]
    items.sort(key=lambda it: -it["updated"])
    return items[:max(1, min(int(limit or 30), 200))]


# --------------------------------------------------------------------------- daily usage

def day_start(t=None):
    """Local midnight of the day containing t."""
    lt = time.localtime(now() if t is None else t)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


class UsageToday:
    """Per-machine token totals of today across ALL claude / codex transcripts.

    Each file is parsed incrementally: an entry per path keeps the read offset and that
    file's running numbers, so a rescan only reads appended bytes (and skips untouched files
    by (size, mtime)). The cache resets at local midnight."""

    def __init__(self):
        self.day = None
        self.files = {}      # path -> state dict

    def _reset(self, t):
        self.day = day_start(t)
        self.files = {}

    @staticmethod
    def _read(path, st, needles):
        """New complete lines of a file since st["pos"] (only lines containing one of needles are parsed)."""
        try:
            with open(path, "rb") as f:
                f.seek(st["pos"])
                data = f.read(64 * 1024 * 1024)
        except OSError:
            return []
        cut = data.rfind(b"\n") + 1
        st["pos"] += cut
        out = []
        for ln in data[:cut].split(b"\n"):
            if any(n in ln for n in needles):
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    pass
        return out

    def _claude(self, path, st, d0, d1):
        for d in self._read(path, st, (b'"usage"',)):
            if not isinstance(d, dict) or d.get("type") != "assistant":
                continue
            t = iso_ts(d.get("timestamp"))
            if t is None or not d0 <= t < d1:
                continue
            msg = d.get("message") or {}
            us = msg.get("usage") or {}
            mid = msg.get("id") or d.get("uuid")
            if not us or not mid or mid in st["seen"]:
                continue
            st["seen"].add(mid)
            st["rows"].append((mid, msg.get("model") or "", int(us.get("input_tokens") or 0),
                               int(us.get("output_tokens") or 0), int(us.get("cache_read_input_tokens") or 0),
                               int(us.get("cache_creation_input_tokens") or 0)))

    def _codex(self, path, st, d0, d1):
        for d in self._read(path, st, (b"token_count", b"turn_context")):
            if not isinstance(d, dict):
                continue
            p = d.get("payload") or {}
            if d.get("type") == "turn_context" and p.get("model"):
                st["model"] = str(p["model"])
                continue
            if d.get("type") != "event_msg" or p.get("type") != "token_count" or not p.get("info"):
                continue
            tot = (p["info"] or {}).get("total_token_usage") or {}
            if not tot:
                continue
            t = iso_ts(d.get("timestamp"))
            if t is None or t >= d1:
                continue
            v = (int(tot.get("input_tokens") or 0), int(tot.get("cached_input_tokens") or 0),
                 int(tot.get("output_tokens") or 0))
            if t < d0:
                st["before"] = v           # last total before today
            else:
                st["last"] = v             # last total today
                if st["model"]:
                    st["model_last"] = st["model"]

    def scan(self, t=None):
        t = now() if t is None else t
        if self.day is None or day_start(t) != self.day:
            self._reset(t)
        d0, d1 = self.day, self.day + 86400 + 3600     # +1 h: DST days are 25 h long
        seen = set()
        paths = [("claude", p) for p in glob.glob(os.path.join(
            glob.escape(os.path.expanduser("~/.claude/projects")), "*", "**", "*.jsonl"), recursive=True)]
        paths += [("codex", p) for d in codex_day_dirs(t, HISTORY_DAYS)
                  for p in glob.glob(os.path.join(glob.escape(d), "rollout-*.jsonl"))]
        for kind, p in paths:
            try:
                fs = os.stat(p)
            except OSError:
                continue
            if fs.st_mtime < d0:
                continue                   # not written today
            seen.add(p)
            st = self.files.get(p)
            if st is None or fs.st_size < st["pos"]:
                st = self.files[p] = {"kind": kind, "pos": 0, "key": None, "seen": set(), "rows": [],
                                      "model": "", "model_last": "", "before": None, "last": None}
            if st["key"] == (fs.st_size, fs.st_mtime):
                continue
            st["key"] = (fs.st_size, fs.st_mtime)
            (self._claude if kind == "claude" else self._codex)(p, st, d0, d1)
        for p in [p for p in self.files if p not in seen]:
            del self.files[p]
        return self.totals()

    def totals(self):
        cl = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        cx = {"input": 0, "output": 0, "cache_read": 0}
        by = {}
        mids = set()      # one message can show up in several files (forks / sidechains)

        def add_model(m, i, o):
            e = by.setdefault(m or "unknown", {"input": 0, "output": 0})
            e["input"] += i
            e["output"] += o
        for st in self.files.values():
            if st["kind"] == "claude":
                for mid, model, i, o, cr, cw in st["rows"]:
                    if mid in mids:
                        continue
                    mids.add(mid)
                    cl["input"] += i
                    cl["output"] += o
                    cl["cache_read"] += cr
                    cl["cache_write"] += cw
                    add_model(model, i + cr + cw, o)
            elif st["last"]:
                b = st["before"] or (0, 0, 0)
                inp, cached, out = (max(0, x - y) for x, y in zip(st["last"], b))
                cx["input"] += max(0, inp - cached)
                cx["cache_read"] += cached
                cx["output"] += out
                add_model(st["model_last"], inp, out)
        return {"day": time.strftime("%Y-%m-%d", time.localtime(self.day)), "claude": cl, "codex": cx,
                "by_model": by}


# --------------------------------------------------------------------------- session

class Session:
    def __init__(self, daemon, sid, kind, name, cwd, argv, cols, rows):
        self.d = daemon
        self.sid, self.kind, self.name, self.cwd, self.argv = sid, kind, name, cwd, argv
        self.cols, self.rows = cols, rows
        self.title = ""
        self.status, self.detail, self.attention = "starting", "", None
        self.created = self.activity = now()
        self.pid, self.fd, self.exit_code = None, None, None
        self.ring = bytearray()
        self.ring_start = 0
        self.chat = []
        self.chat_seq = 0
        self.preview = ""
        self.subs = set()
        self.tail = None
        self.tail_kind = None
        self.transcript = None
        self.settings_file = None
        self.last_event = {}
        self.dirty = True
        self.last_bcast = 0.0
        self.qbuf = b""
        self.paste_mode = False
        self.last_key = None
        self.killed = False
        self.env = {}                  # extra env (API key swaps); never sent to clients
        self.key_file = None           # hot key: 0600 file the process reads the key from (None = env / settings)
        self.key = None                # key profile name shown in the UI
        self.agent_session_id = None   # claude session id / codex thread id, for resume
        self.restarts = 0
        self.auto_continue = False     # retry transient errors by typing "继续"
        self.ac_count = 0              # consecutive auto-continues (reset when a turn completes)
        self.ac_pending = False
        self.memory_project = None     # AMem project this session reads / writes (None = memory off)
        self.memory_root = None
        self.restart_req = None        # (resume, callback) while a restart is in progress
        self.usage = None
        # approvals by id, oldest first: {"id", "tool", "summary", "detail", "can_always", "ts", "suggestions",
        # "waiter" (daemon Client blocked in the PermissionRequest hook), "match" (tool name + input hash)}
        self.pendings = {}
        self.question = None
        self.last_rollout_scan = 0.0
        self.spawned_at = self.created
        self.resumed_at = None
        self.overrides = {}            # codex launch overrides: model / effort / approval / sandbox
        self.cx_mode = {}              # codex settings from the rollout turn_context
        self.cmode = None              # claude permission mode read from the footer
        self.hook_mode = None          # claude permission_mode reported by hooks (fallback)
        self.effort = None             # claude effort level from the transcript
        self.screen = Screen(cols, rows) if kind in ("claude", "codex") else None
        self.screen_dirty = False
        self.menu_dirty = False
        self.menu_parsed = 0.0         # last parse_menu run (MENU_MAX_WAIT)
        self.menu = None               # interactive picker visible on the screen (parse_menu)
        self.init_cmds = []            # slash commands typed once after the first SessionStart (claude)
        self.delete_req = None         # callback(ok) while a delete waits for the process to exit

    # --- info
    @property
    def end(self):
        return self.ring_start + len(self.ring)

    @property
    def pending(self):
        """The oldest open approval (the one the UI shows)."""
        for p in self.pendings.values():
            return p
        return None

    @property
    def pending_waiter(self):
        p = self.pending
        return p["waiter"] if p else None

    def mode(self):
        if self.kind == "claude":
            return {"mode": self.cmode or CLAUDE_MODE_ALIAS.get(self.hook_mode, self.hook_mode) or "default",
                    "model": (self.usage or {}).get("model") or "", "effort": self.effort or ""}
        if self.kind == "codex":
            return dict({"model": "", "effort": "", "approval": "", "sandbox": ""}, **self.cx_mode)
        return {}

    def info(self):
        p = self.pending
        return {"sid": self.sid, "kind": self.kind, "name": self.name, "title": self.title, "cwd": self.cwd,
                "argv0": os.path.basename(self.argv[0]) if self.argv else "",
                "status": self.status, "detail": self.detail, "attention": self.attention,
                "created": self.created, "activity": self.activity, "pid": self.pid, "exit_code": self.exit_code,
                "cols": self.cols, "rows": self.rows, "end": self.end, "chat_seq": self.chat_seq,
                "preview": self.preview, "usage": self.usage,
                "pending": {k: v for k, v in p.items() if k not in ("suggestions", "waiter", "match")} if p else None,
                "pending_count": len(self.pendings),
                "question": self.question, "agent_session_id": self.agent_session_id,
                "can_resume": bool(self.agent_session_id) and self.kind in ("claude", "codex"),
                "key": self.key, "restarts": self.restarts, "auto_continue": self.auto_continue, "ac_count": self.ac_count, "mode": self.mode(), "overrides": dict(self.overrides),
                "memory": self.memory_project, "hot_key": bool(self.key_file) and self.fd is not None,
                "menu": self.menu}

    def touch(self):
        self.dirty = True

    def set_status(self, status, detail=None, attention="keep"):
        if status is not None:
            self.status = status
        if detail is not None:
            self.detail = detail
        if attention != "keep":
            self.attention = attention
        self.touch()

    # --- process
    def launch_argv(self, resume=False):
        """Program argv: preset + codex overrides + resume + codex notification flags (claude settings added later).
        --no-daemon etc. are NOT forced: the preset argv carries whatever the user's codex needs."""
        argv = list(self.argv)
        if self.kind == "codex" and self.overrides:
            argv = codex_override_argv(argv, self.overrides)
        if resume and self.agent_session_id:
            if self.kind == "claude":
                argv = argv[:1] + ["--resume", self.agent_session_id] + argv[1:]
            elif self.kind == "codex":
                argv = argv[:1] + ["resume", self.agent_session_id] + argv[1:]
        if self.kind == "codex":
            argv = argv[:1] + CODEX_FLAGS + argv[1:]
        return argv

    def hot_key_setup(self, argv):
        """Before spawn: can this session's key be swapped live? Then write the key file and return
        (env vars to remove, extra codex argv). None: classic env / settings key (a swap restarts)."""
        self.key_file = None
        if len(self.env) != 1 or self.kind not in ("claude", "codex"):
            return None
        var, value = next(iter(self.env.items()))
        tmpl = []
        if self.kind == "claude":
            if var not in CLAUDE_KEY_VARS:
                return None
            drop = CLAUDE_KEY_VARS
        else:
            tmpl = codex_hot_provider(argv, var)
            if tmpl is None:
                return None
            drop = (var,)
        path = os.path.join(self.d.sdir or private_dir(), "key-%s" % self.sid)
        try:
            write_private(path, value)
        except OSError:
            return None
        self.key_file = path
        return drop, [a.replace("{KEYFILE}", path) for a in tmpl]

    def swap_key(self, env, key):
        """Change the API key of the running process without a restart. False: not possible (restart instead)."""
        if self.fd is None or not self.key_file or not env or len(env) != 1 or set(env) != set(self.env):
            return False
        try:
            write_private(self.key_file, next(iter(env.values())))
        except OSError:
            return False
        self.env, self.key = dict(env), key
        it = self.add_chat("system", "—— 已切换到 %s（无需重启）——" % (key or "新 key"))
        if it:
            self.d.send_subs(self, {"t": "chat", "sid": self.sid, "items": [it]})
        self.touch()
        return True

    def spawn(self, resume=False):
        env = {k: v for k, v in os.environ.items() if not k.startswith(STRIP_ENV)}
        env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor", "GLIMPSE_SID": self.sid,
                    "GLIMPSE_SOCK": self.d.sock_name, "TERM_PROGRAM": "glimpse",
                    "GLIMPSE_APPROVAL_WAIT": str(int(self.d.approval_wait))})
        if "LANG" not in env:
            env["LANG"] = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"
        argv = self.launch_argv(resume)
        hot = self.hot_key_setup(argv)   # -> (env var the key replaces, extra argv) or None
        if self.kind != "claude" and not hot:
            env.update(self.env)     # codex: export OPENAI_API_KEY=... ; claude: key goes into the settings file only
        if hot:
            for v in hot[0]:
                env.pop(v, None)
            if self.kind == "codex":
                argv = argv[:1] + hot[1] + argv[1:]
        self.d.memory.attach(self)
        if self.kind == "codex" and argv:
            argv = argv[:1] + self.d.memory.codex_args(self) + argv[1:]
        if self.kind == "claude":
            argv = argv[:1] + self.d.memory.claude_args(self) + argv[1:] if argv else argv
            argv, self.settings_file = prepare_claude_argv(argv, self.sid, self.d.sdir, None if hot else self.env,
                                                           self.d.approval_wait, self.d.memory.claude_allow(self),
                                                           key_file=self.key_file if hot else None)
        shell = env.get("SHELL")
        if not shell:
            try:
                import pwd
                shell = pwd.getpwuid(os.getuid()).pw_shell
            except (ImportError, KeyError):
                shell = "/bin/sh"
            env["SHELL"] = shell
        if argv:
            # login+interactive shell so PATH/aliases match the user's terminal; key env is exported again
            # after the rc files ran (a `export OPENAI_API_KEY=...` in ~/.bashrc must not override the chosen key)
            pre = ""
            if hot:
                # an `export ANTHROPIC_AUTH_TOKEN=...` / `export V_API_KEY=...` in the rc files would win over the key file
                pre += "".join("unset %s; " % v for v in hot[0])
            if self.kind == "codex" and hot:
                pass
            elif self.kind != "claude":
                for k in self.env:
                    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
                        env["GLIMPSE_KEY_" + k] = self.env[k]
                        pre += 'export %s="$GLIMPSE_KEY_%s"; unset GLIMPSE_KEY_%s; ' % (k, k, k)
            full = [shell, "-l", "-i", "-c", pre + 'exec "$@"', "glimpse"] + argv
        else:
            full = [shell, "-l"]
        cwd = self.cwd if os.path.isdir(self.cwd) else os.path.expanduser("~")
        if os.environ.get("GLIMPSE_DEBUG"):
            sys.stderr.write("spawn %s %s\n" % (self.sid, " ".join(a if len(a) < 80 else a[:20] + "…" for a in argv)))
            sys.stderr.flush()
        pid, fd = pty.fork()
        if pid == 0:
            try:
                for sig in (signal.SIGPIPE, signal.SIGHUP, signal.SIGINT, signal.SIGTERM, signal.SIGCHLD):
                    signal.signal(sig, signal.SIG_DFL)
                os.chdir(cwd)
                os.execvpe(full[0], full, env)
            except Exception as e:  # pragma: no cover
                os.write(2, ("glimpse: exec failed: %s\n" % e).encode())
            os._exit(127)
        self.pid, self.fd = pid, fd
        self.spawned_at = now()
        self.resumed_at = self.spawned_at if resume and self.agent_session_id else None
        self.cmode = None
        self.menu = None
        if self.screen is not None:
            self.screen = Screen(self.cols, self.rows)
        if self.kind == "codex":
            self.tail = None       # a fresh codex writes a new rollout file (found by poll_tail)
            self.transcript = None
            if resume and self.agent_session_id:
                # `codex resume` appends to the conversation's existing rollout
                p = find_codex_rollout_by_id(self.agent_session_id)
                if p:
                    self.attach_rollout(p, import_history=not self.chat)
        set_nonblock(fd)
        set_cloexec(fd)
        self._winsz()
        self.set_status("idle" if self.kind == "shell" else "starting")

    def _winsz(self):
        if self.fd is not None:
            try:
                fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", self.rows, self.cols, 0, 0))
            except OSError:
                pass

    def resize(self, cols, rows):
        cols, rows = max(20, min(int(cols), 500)), max(5, min(int(rows), 300))
        if (cols, rows) != (self.cols, self.rows):
            self.cols, self.rows = cols, rows
            if self.screen is not None:
                self.screen.resize(cols, rows)
            self._winsz()
            if self.pid:
                try:
                    os.kill(self.pid, signal.SIGWINCH)
                except OSError:
                    pass
            self.touch()

    def write(self, data):
        if self.fd is None or not data:
            return
        self.d.pty_write(self, data)

    def user_input(self):
        if self.attention:
            self.attention = None
            self.touch()
        if self.pendings:
            # someone is at a keyboard: hand the decisions back to claude's own dialog
            self.release_pendings()
            return
        if self.status == "waiting":
            self.set_status("running", "")
        self.question = None

    # --- approvals (claude PermissionRequest hook blocks until resolve_pending)
    @staticmethod
    def tool_match(tool, inp):
        raw = json.dumps(inp, sort_keys=True, ensure_ascii=False, default=str)
        return "%s:%s" % (tool, hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest())

    def set_pending(self, ev, waiter):
        """Parallel tool calls can each wait for approval: requests are kept by id, the oldest is shown."""
        tool = ev.get("tool_name") or ""
        inp = ev.get("tool_input") or {}
        match = self.tool_match(tool, inp)
        detail = inp.get("command") or inp.get("file_path") or inp.get("url") or ""
        if isinstance(inp, dict) and inp.get("content") and isinstance(inp["content"], str):
            detail = (detail + "\n\n" if detail else "") + inp["content"]
        elif isinstance(inp, dict) and inp.get("new_string"):
            detail = (detail + "\n\n" if detail else "") + "- " + str(inp.get("old_string", ""))[:1500] + \
                "\n+ " + str(inp["new_string"])[:1500]
        if not detail:
            detail = json.dumps(inp, ensure_ascii=False, indent=1)
        sugg = ev.get("permission_suggestions") or []
        for old in [p for p in self.pendings.values() if p["match"] == match]:
            self.resolve_pending(None, pid=old["id"])     # the same call asked again: the old hook is stale
        pid = os.urandom(4).hex()
        while pid in self.pendings:
            pid = os.urandom(4).hex()
        p = {"id": pid, "tool": tool, "summary": summarize_tool(tool, inp), "detail": clip(detail, 4000),
             "can_always": bool(sugg), "ts": now(), "suggestions": sugg, "waiter": waiter, "match": match}
        self.pendings[pid] = p
        self.set_status("waiting", clip(self.pending["summary"], 200), "question")
        self.event("question", "需要你确认", p["summary"])
        if waiter is not None:
            self.d.later(self.d.approval_wait, lambda: pid in self.pendings and self.resolve_pending(None, pid=pid))

    def release_pendings(self, waiter=None):
        """Hand every open approval (or only those of one hook client) back to the terminal."""
        for p in list(self.pendings.values()):
            if waiter is None or p["waiter"] is waiter:
                self.resolve_pending(None, pid=p["id"], notify=waiter is None)

    def resolve_pending(self, decision, message=None, pid=None, notify=True):
        """decision: allow | always | deny | None (= let the terminal prompt handle it).
        pid: which request (default the oldest). notify=False: the waiter is gone, nothing to send."""
        p = self.pendings.get(pid) if pid else self.pending
        if not p:
            return False
        out = None
        if decision in ("allow", "always"):
            dec = {"behavior": "allow"}
            if decision == "always" and p.get("suggestions"):
                dec["updatedPermissions"] = p["suggestions"]
            out = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": dec}}
        elif decision == "deny":
            out = {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                          "decision": {"behavior": "deny", "message": message or "用户在手机上拒绝了"}}}
        self.pendings.pop(p["id"], None)
        w = p["waiter"]
        if w is not None and notify:
            self.d.send(w, {"t": "ok", "out": out})
        if self.pendings:
            nxt = self.pending
            self.set_status("waiting", clip(nxt["summary"], 200), "question")
        elif decision is not None:
            self.attention = None
            self.set_status("running", "")
        self.touch()
        return True

    def resolve_tool_done(self, tool, inp):
        """PostToolUse: the call ran (approved in the terminal) -> release only the matching request."""
        match = self.tool_match(tool or "", inp or {})
        for p in list(self.pendings.values()):
            if p["match"] == match:
                self.resolve_pending(None, pid=p["id"])
                return True
        return False

    def restart(self, env, key, resume, done, cold=False):
        """Kill the process, swap env (API key), start again (optionally resuming the conversation).
        A key change on a session that reads its key from a file is applied live instead (no restart), unless
        `cold`: claude keeps the key it had when a message started for all of that message's retries, so after a
        key that ran out the next message would still fail; a new process uses the new key at once."""
        if env is not None and resume and not cold and self.swap_key(env, key):
            done(None)
            return
        if env is not None:
            self.env = dict(env)
            self.key = key
        if self.fd is not None:
            self.restart_req = (resume, done)
            self.d.kill(self)
        else:
            self._respawn(resume, done)

    def _respawn(self, resume, done):
        self.restart_req = None
        self.killed = False
        self.exit_code = None
        self.restarts += 1
        self.release_pendings()
        self.question = None
        it = self.add_chat("system", "—— 进程已重启%s ——" % ("，恢复原对话" if resume and self.agent_session_id else ""))
        if it:
            self.d.send_subs(self, {"t": "chat", "sid": self.sid, "items": [it]})
        try:
            self.spawn(resume=resume)
            self.d.sel.register(self.fd, selectors.EVENT_READ, ("pty", self))
            done(None)
        except Exception as e:
            self.set_status("error", "重启失败: %s" % e, "error")
            done(str(e))

    def on_output(self, data):
        self.activity = now()
        self.ring += data
        if len(self.ring) > RING_MAX:
            cut = len(self.ring) - RING_MAX + 64 * 1024
            del self.ring[:cut]
            self.ring_start += cut
        off = self.end - len(data)
        # answer terminal queries (cursor position, DA, colours) if no real terminal is attached
        scan = self.qbuf + data
        fresh = len(self.qbuf)   # only act on sequences that end inside the new data
        for m in QUERY_RE.finditer(scan):
            if m.end() > fresh and not self.d.has_terminal(self):
                self.write(query_reply(m.group(0)))
        self.qbuf = scan[-256:]
        i_on, i_off = scan.rfind(b"\x1b[?2004h"), scan.rfind(b"\x1b[?2004l")
        if i_on != i_off:
            self.paste_mode = i_on > i_off
        for m in OSC_RE.finditer(scan):
            if m.end() > fresh:
                self.on_osc(m.group(1), m.group(2).decode("utf-8", "replace"))
        self.d.send_subs(self, {"t": "out", "sid": self.sid, "off": off, "d": b64e(data)})
        if self.screen is not None:
            self.screen.feed(data)
            self.screen_dirty = True     # footer re-read once per tick (output bursts)
            self.menu_dirty = True       # menus are parsed once the output settled
        if self.status == "starting" and self.kind != "claude":
            self.set_status("idle")
        self.touch()

    def on_osc(self, code, text):
        if code in (b"0", b"2"):
            t = clean_title(text)
            if t and t != self.title:
                self.title = t
                self.touch()
        elif code == b"9" and not text.startswith("4;"):
            self.on_desktop_notify(text)
        elif code == b"777" and text.startswith("notify;"):
            parts = text.split(";", 2)
            self.on_desktop_notify(parts[-1] if len(parts) > 2 else "")

    def on_desktop_notify(self, text):
        if self.kind == "claude":
            return  # hooks are authoritative for claude
        low = text.lower()
        if "approval" in low or "question" in low or "plan mode" in low or "wants to" in low:
            self.set_status("waiting", clip(text, 200), "question")
            self.event("question", "需要你确认", text)
        elif self.kind != "codex" or self.tail is None:
            self.set_status("idle", "", "done")
            self.event("done", "已完成本轮", text)

    def check_mode(self):
        """Re-read claude's permission mode from the footer after an output burst."""
        if self.screen is None or not self.screen_dirty:
            return
        self.screen_dirty = False
        if self.kind != "claude":
            return
        m = self.screen.claude_mode()
        if m is not None and m != self.cmode:
            self.cmode = m
            self.touch()

    def check_menu(self, t=None):
        """Parse the screen for an interactive picker once output paused for MENU_SETTLE."""
        if self.screen is None or not self.menu_dirty:
            return
        t = t or now()
        if t - self.activity < MENU_SETTLE and t - self.menu_parsed < MENU_MAX_WAIT:
            return
        self.menu_dirty = False
        self.menu_parsed = t
        try:
            m = self.screen.menu()
        except Exception:
            m = None
        if m != self.menu:
            self.menu = m
            self.touch()
        q = self.question
        if q and q.get("questions"):
            try:
                p = question_progress(self.screen.lines(), q["questions"])
            except Exception:
                p = None
            if p:
                nq = dict(q, **p)
                if not p["review"]:
                    nq.pop("submit", None)
                    nq.pop("summary", None)
                    nq.update(q["questions"][p["qi"]])           # top-level fields = the question shown now
                if nq != q:
                    self.question = nq
                    self.touch()

    def run_init_cmds(self):
        """Type the create's init_cmds (e.g. "/effort ultracode") once claude is up."""
        cmds, self.init_cmds = self.init_cmds, []
        delay = 0.6                      # SessionStart fires before the prompt accepts input
        for cmd in cmds:
            self.d.later(delay, lambda cmd=cmd: self.fd is not None and self.d.type_text(self, cmd))
            delay += 0.8
        if cmds:
            self.type_busy = now() + delay

    def on_exit(self, code):
        self.exit_code = code
        self.fd = None
        if self.menu is not None:
            self.menu = None
        self.release_pendings()
        if self.settings_file:
            try:
                os.unlink(self.settings_file)
            except OSError:
                pass
            self.settings_file = None
        if self.key_file:
            try:
                os.unlink(self.key_file)
            except OSError:
                pass
            self.key_file = None
        if self.restart_req:
            self.poll_tail()
            resume, done = self.restart_req
            self._respawn(resume, done)
            return
        if self.resumed_at and code not in (0, None) and not self.killed and now() - self.resumed_at < 20:
            # `--resume` / `codex resume` failed right away (conversation not found, ...): start fresh instead
            self.resumed_at = None
            it = self.add_chat("system", "—— 无法恢复原对话，已开始新对话 ——")
            if it:
                self.d.send_subs(self, {"t": "chat", "sid": self.sid, "items": [it]})
            self.agent_session_id = None
            self._respawn(False, lambda err: None)
            return
        bad = code not in (0, None) and not self.killed
        self.set_status("error" if bad else "exited", "exit code %s" % code, "error" if bad else "done")
        self.poll_tail()
        if self.delete_req:
            # deleted while running: no exit event, just forget it once it is gone
            cb, self.delete_req = self.delete_req, None
            cb(self.d.forget(self.sid))
            return
        self.event("error" if bad else "exit", "进程异常退出 (code %s)" % code if bad else "进程已退出",
                   self.preview or self.name)
        # attached terminals learn about the exit right away (they are not hub clients)
        self.d.send_subs(self, {"t": "session", "s": self.info()})

    # --- chat / events
    def add_chat(self, role, text, extra=None):
        if not text and role not in ("tool",):
            return None
        self.chat_seq += 1
        it = {"seq": self.chat_seq, "role": role, "text": clip(text, 20000), "ts": now()}
        if extra:
            it.update({k: v for k, v in extra.items() if v})
        self.chat.append(it)
        if len(self.chat) > CHAT_MAX:
            del self.chat[: len(self.chat) - CHAT_MAX]
        if role == "assistant":
            self.preview = clip(text.replace("\n", " "), 300)
        return it

    def maybe_auto_continue(self, err):
        """Transient error + auto-continue on: type "继续" after a backoff instead of stopping.
        Returns True when a retry was scheduled (the caller then skips the error status / push)."""
        if not self.auto_continue or self.kind not in ("claude", "codex") or self.fd is None or self.ac_pending:
            return False
        if not is_transient_error(err) or self.ac_count >= AUTO_CONTINUE_MAX:
            if self.ac_count >= AUTO_CONTINUE_MAX:
                self.event("error", "自动继续已达上限（%d 次）" % AUTO_CONTINUE_MAX, err)
            return False
        delay = AUTO_CONTINUE_DELAYS[min(self.ac_count, len(AUTO_CONTINUE_DELAYS) - 1)]
        self.ac_count += 1
        self.ac_pending = True
        n = self.ac_count
        self.set_status("waiting", "临时错误，%d 秒后自动继续（第 %d/%d 次）" % (delay, n, AUTO_CONTINUE_MAX), None)
        it = self.add_chat("system", "—— %s：%d 秒后自动发送“%s”（第 %d 次）——"
                           % (clip(err, 120), delay, AUTO_CONTINUE_TEXT, n))
        if it:
            self.d.send_subs(self, {"t": "chat", "sid": self.sid, "items": [it]})

        def fire():
            self.ac_pending = False
            if self.fd is None or not self.auto_continue or self.status == "running":
                return              # process gone, switched off, or someone already continued it
            self.d.type_text(self, AUTO_CONTINUE_TEXT)
            self.set_status("running", "", None)
        self.d.later(delay, fire)
        return True

    def event(self, kind, title, body):
        body = clip((body or "").strip(), 400)
        key = (kind, body)
        t = now()
        if t - self.last_event.get(key, 0) < 8:
            return
        self.last_event[key] = t
        self.d.broadcast({"t": "event", "sid": self.sid,
                          "ev": {"kind": kind, "title": title, "body": body, "ts": t}})

    def track_usage(self, d):
        """Token / context usage from transcript lines (claude assistant usage, codex token_count)."""
        if not isinstance(d, dict):
            return
        u = self.usage or {"model": "", "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                           "context_used": 0, "context_window": 0, "turns": 0, "rate": None}
        if self.tail_kind == "claude" and d.get("type") == "assistant" and not d.get("isSidechain"):
            msg = d.get("message") or {}
            us = msg.get("usage") or {}
            mid = msg.get("id")
            if not us or mid == getattr(self, "_last_mid", None):
                return     # claude writes one line per content block with the same usage
            self._last_mid = mid
            if isinstance(d.get("effort"), str) and d["effort"] != self.effort:
                self.effort = d["effort"]
            u["model"] = msg.get("model") or u["model"]
            u["input"] += int(us.get("input_tokens") or 0)
            u["output"] += int(us.get("output_tokens") or 0)
            u["cache_read"] += int(us.get("cache_read_input_tokens") or 0)
            u["cache_write"] += int(us.get("cache_creation_input_tokens") or 0)
            u["context_used"] = int(us.get("input_tokens") or 0) + int(us.get("cache_read_input_tokens") or 0) + \
                int(us.get("cache_creation_input_tokens") or 0)
            u["context_window"] = 1000000 if "[1m]" in (u["model"] or "") or u["context_used"] > 200000 else 200000
            u["turns"] += 1
        elif self.tail_kind == "codex":
            p = d.get("payload") or {}
            if d.get("type") == "session_meta":
                self.agent_session_id = p.get("id") or p.get("session_id") or self.agent_session_id
                return
            if d.get("type") == "turn_context":
                u["model"] = p.get("model") or u["model"]
                self.cx_mode = codex_mode(p, self.cx_mode)
            elif d.get("type") == "event_msg" and p.get("type") == "token_count" and p.get("info"):
                info = p["info"]
                tot = info.get("total_token_usage") or {}
                last = info.get("last_token_usage") or {}
                u["input"] = int(tot.get("input_tokens") or 0) - int(tot.get("cached_input_tokens") or 0)
                u["cache_read"] = int(tot.get("cached_input_tokens") or 0)
                u["output"] = int(tot.get("output_tokens") or 0)
                u["context_used"] = int(last.get("input_tokens") or 0) + int(last.get("output_tokens") or 0)
                u["context_window"] = int(info.get("model_context_window") or 0)
                rl = p.get("rate_limits") or {}
                prim, sec = rl.get("primary") or {}, rl.get("secondary") or {}
                if prim or sec:
                    u["rate"] = {"primary": prim.get("used_percent"), "secondary": sec.get("used_percent")}
                u["turns"] += 1
            elif d.get("type") == "event_msg" and p.get("type") == "thread_settings_applied":
                u["model"] = (p.get("thread_settings") or {}).get("model") or u["model"]
                return
            else:
                return
        else:
            return
        self.usage = u
        self.touch()

    def attach_rollout(self, path, import_history=False):
        """Follow an existing codex rollout (resumed conversation) from its current end.
        import_history: show the last RESUME_IMPORT chat items and the usage so far, without events."""
        self.transcript = path
        self.tail_kind = "codex"
        objs, end = read_jsonl_end(path) if import_history else ([], None)
        self.tail = Tail(path, from_start=False)
        if end is not None:
            self.tail.pos = end      # lines written after our read are picked up by the tail
        if not objs:
            return
        rows = []
        for d in objs:
            if isinstance(d, dict):
                self.track_usage(d)
                rows += [r for r in parse_codex_line(d) if not r[0].startswith("@")]
        new = [it for it in (self.add_chat(role, text, extra) for role, text, extra in rows[-RESUME_IMPORT:]) if it]
        if new:
            self.d.send_subs(self, {"t": "chat", "sid": self.sid, "items": new})
            self.touch()

    def is_history(self, d):
        """A transcript line written before this process started (claude replays the resumed file)."""
        t = iso_ts(d.get("timestamp"))
        return t is not None and t < self.spawned_at - 1

    def poll_tail(self):
        if self.tail is None:
            if self.kind == "codex" and self.fd is not None and now() - self.last_rollout_scan > 2:
                self.last_rollout_scan = now()
                if self.resumed_at and self.agent_session_id:
                    p = find_codex_rollout_by_id(self.agent_session_id)
                    if p:
                        self.attach_rollout(p, import_history=not self.chat)
                else:
                    p = find_codex_rollout(self.cwd, self.spawned_at, self.d.taken_rollouts())
                    if p:
                        self.transcript = p
                        self.tail = Tail(p)
                        self.tail_kind = "codex"
            if self.tail is None:
                return
        new = []
        for d in self.tail.read():
            if not isinstance(d, dict):
                continue
            self.track_usage(d)
            rows = parse_claude_line(d) if self.tail_kind == "claude" else parse_codex_line(d)
            old = self.is_history(d)
            if self.tail_kind == "codex" and d.get("type") == "event_msg" and not old and \
                    (d.get("payload") or {}).get("type") == "task_started":
                self.set_status("running", "", None)
            for role, text, extra in rows:
                if old and role.startswith("@"):
                    continue          # history replay: chat and usage only, no status changes / events
                if role.startswith("@") and new:
                    # deliver the turn's messages before its done/error event
                    self.d.send_subs(self, {"t": "chat", "sid": self.sid, "items": new})
                    new = []
                if role == "@done":
                    self.ac_count = 0
                    self.d.memory.capture(self)
                    self.set_status("idle", "", "done")
                    self.event("done", "已完成本轮", text or self.preview)
                elif role == "@error":
                    if not self.maybe_auto_continue(text):
                        self.set_status("error", clip(text, 200), "error")
                        self.event("error", "出错停止", text)
                elif role == "@aborted":
                    self.set_status("idle", "已中断")
                else:
                    it = self.add_chat(role, text, extra)
                    if it:
                        new.append(it)
        if new:
            self.d.send_subs(self, {"t": "chat", "sid": self.sid, "items": new})
            self.touch()

    def set_transcript(self, path):
        if not path or path == self.transcript:
            return
        if self.transcript:
            it = self.add_chat("system", "—— 新对话 ——")
            if it:
                self.d.send_subs(self, {"t": "chat", "sid": self.sid, "items": [it]})
        self.transcript = path
        self.tail = Tail(path)
        self.tail_kind = "claude"

    def on_hook(self, ev, waiter=None):
        """Returns True if the hook client must wait for a decision (PermissionRequest)."""
        name = ev.get("hook_event_name")
        if ev.get("session_id"):
            self.agent_session_id = ev["session_id"]
        if isinstance(ev.get("permission_mode"), str) and ev["permission_mode"] != self.hook_mode:
            self.hook_mode = ev["permission_mode"]
            self.touch()
        self.set_transcript(ev.get("transcript_path"))
        wait = False
        if name == "SessionStart":
            self.set_status("idle", "", None)
            if self.init_cmds:
                self.run_init_cmds()
        elif name == "UserPromptSubmit":
            self.question = None
            self.set_status("running", "", None)
        elif name in ("PreToolUse", "PostToolUse"):
            tool = ev.get("tool_name") or ""
            if name == "PreToolUse" and tool == "AskUserQuestion":
                inp = ev.get("tool_input") or {}
                items = [ask_item(q) for q in (inp.get("questions") or []) if isinstance(q, dict)][:6] or [ask_item({})]
                # the top-level fields are the question shown now (what older phones read); with several
                # questions check_menu() follows claude's picker and moves qi on as they are answered
                self.question = dict(items[0], qi=0)
                if len(items) > 1:
                    self.question.update(questions=items, answered=[False] * len(items), review=False)
                q = summarize_tool(tool, inp)
                self.set_status("waiting", clip(q, 200), "question")
                self.event("question", "有问题需要回答", q)
            else:
                if name == "PostToolUse" and tool == "AskUserQuestion":
                    self.question = None
                if self.pendings and name == "PostToolUse":
                    self.resolve_tool_done(tool, ev.get("tool_input"))    # answered in the terminal
                if self.pendings:
                    pass           # other parallel calls still wait for a decision: stay "waiting"
                else:
                    self.set_status("running", summarize_tool(tool, ev.get("tool_input")) if name == "PreToolUse"
                                    else self.detail, None if self.status == "waiting" else "keep")
        elif name == "PermissionRequest":
            if ev.get("tool_name") == "AskUserQuestion":
                pass   # shown as a question card; the terminal prompt handles it
            else:
                self.set_pending(ev, waiter)
                wait = waiter is not None
        elif name == "Notification":
            nt = ev.get("notification_type") or ""
            msg = ev.get("message") or ""
            if nt in ("permission_prompt", "elicitation_dialog"):
                if self.status != "waiting":
                    self.set_status("waiting", clip(msg, 200), "question")
                    self.event("question", "需要你确认", msg)
        elif name == "Stop":
            self.release_pendings()
            self.question = None
            self.poll_tail()
            msg = ev.get("last_assistant_message") or self.preview
            if msg:
                self.preview = clip(msg.replace("\n", " "), 300)
            self.ac_count = 0
            self.d.memory.capture(self)
            self.set_status("idle", "", "done")
            self.event("done", "已完成本轮", msg)
        elif name == "StopFailure":
            err = ev.get("error") or "unknown"
            det = ev.get("error_details") or ""
            if not isinstance(det, str):
                det = json.dumps(det, ensure_ascii=False)
            # the hook often carries only a category ("server_error"): the real text ("API Error: 500
            # Key余额不足 ...") is the last assistant message, which decides fatal vs. transient
            self.poll_tail()
            last = ev.get("last_assistant_message") or ""
            if not isinstance(last, str):
                last = json.dumps(last, ensure_ascii=False)
            full = " ".join(x for x in (err, det, last, self.preview if not last else "") if x)
            if not self.maybe_auto_continue(full):
                self.set_status("error", clip("%s %s" % (err, det), 200), "error")
                self.event("error", "出错停止: %s" % err, det or ev.get("last_assistant_message") or "")
        self.poll_tail()
        return wait


# --------------------------------------------------------------------------- hot key swap
# The API key of a claude / codex session can change without restarting it: the process reads the key from a
# per-session 0600 file on each refresh (claude: settings apiKeyHelper; codex: provider auth.command), and a
# swap only rewrites that file. Sessions where this is impossible keep the env / settings key and restart.

CLAUDE_KEY_VARS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
CLAUDE_HELPER_TTL_MS = "3000"     # claude re-runs the helper this often (it serves the old key once more)
CODEX_AUTH_REFRESH_MS = 2000
TOML_KEY_RE = re.compile(r'^\s*("[^"]+"|[A-Za-z0-9_.-]+)\s*=\s*(.+?)\s*$')


def toml_tables(text):
    """Tiny TOML reader for codex's config.toml: {table path: {key: raw value text}} ("" = top level).
    Only single-line values (all codex provider settings are); anything else is skipped."""
    out, cur = {"": {}}, ""
    for ln in text.splitlines():
        s = ln.strip()
        # drop a trailing comment outside quotes: `wire_api = "responses"  # note`
        m = re.match(r'^((?:[^"#]|"(?:[^"\\]|\\.)*")*)#', s)
        if m:
            s = m.group(1).strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("[") and s.endswith("]") and not s.startswith("[["):
            cur = s[1:-1].strip()
            out.setdefault(cur, {})
            continue
        m = TOML_KEY_RE.match(s)
        if m:
            out.setdefault(cur, {})[m.group(1).strip('"')] = m.group(2)
    return out


def codex_hot_provider(argv, key_var):
    """-c args that clone the active codex provider without env_key, reading the token from the key file
    (placeholder "{KEYFILE}"); None when the active provider is not the one whose env_key we inject."""
    if any(a in ("-p", "--profile") or a.startswith("--profile=") for a in argv) or \
            any(a.replace(" ", "").startswith(("model_provider=", "model_providers.")) for a in argv):
        return None                    # the user steers providers on the command line: leave it alone
    home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    try:
        with open(os.path.join(home, "config.toml"), errors="replace") as f:
            t = toml_tables(f.read())
    except OSError:
        return None
    pid = (t[""].get("model_provider") or "").strip().strip('"')
    sect = t.get("model_providers." + pid) or t.get('model_providers."%s"' % pid)
    if not pid or not sect or "base_url" not in sect or sect.get("env_key", "").strip('"') != key_var \
            or not re.match(r"^[A-Za-z0-9_-]+$", pid):
        return None
    new = pid + "-glimpse"
    args = ["-c", 'model_provider="%s"' % new]
    for k, v in sect.items():
        if k not in ("env_key", "env_key_instructions", "experimental_bearer_token", "requires_openai_auth") \
                and re.match(r"^[A-Za-z0-9_]+$", k):
            args += ["-c", "model_providers.%s.%s=%s" % (new, k, v)]
    for sub, vals in t.items():           # e.g. [model_providers.x.http_headers]
        pre = "model_providers.%s." % pid
        if sub.startswith(pre) and sub[len(pre):] not in ("auth",) and re.match(r"^[A-Za-z0-9_]+$", sub[len(pre):]):
            for k, v in vals.items():
                if re.match(r"^[A-Za-z0-9_-]+$", k):
                    args += ["-c", "model_providers.%s.%s.%s=%s" % (new, sub[len(pre):], k, v)]
    args += ["-c", 'model_providers.%s.auth.command="/bin/cat"' % new,
             "-c", "model_providers.%s.auth.args=[\"{KEYFILE}\"]" % new,
             "-c", "model_providers.%s.auth.refresh_interval_ms=%d" % (new, CODEX_AUTH_REFRESH_MS)]
    return args


def write_private(path, data):
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    os.replace(path + ".tmp", path)


# --------------------------------------------------------------------------- auto-continue

# Errors worth simply retrying with "继续": provider overload, dropped streams, gateway errors.
TRANSIENT_ERR_RE = re.compile(
    r"overloaded|stream disconnected|stream closed|disconnected before completion|try again later|"
    r"temporarily unavailable|server error|internal error|bad gateway|gateway time-?out|service unavailable|"
    r"\b50[0234]\b|\b529\b|timed? ?out|connection reset|econnreset|server_error|overloaded_error|"
    r"incomplete response|max_output_tokens|connection failed|error sending request|connection (refused|closed|error)|"
    r"response\.completed|network error|broken pipe", re.I)
# Never retried: the user must act (top up / swap the key, fix auth). Checked first.
FATAL_ERR_RE = re.compile(
    r"余额不足|额度|insufficient|balance|quota|billing|credit|权限验证失败|unauthori[sz]ed|invalid.{0,10}key|"
    r"authentication|\b401\b|\b403\b|forbidden|context.{0,20}(length|window)|prompt is too long", re.I)
AUTO_CONTINUE_TEXT = "继续"
AUTO_CONTINUE_MAX = 5            # consecutive retries per session before giving up and notifying
AUTO_CONTINUE_DELAYS = (15, 30, 60, 120, 240)   # seconds before each retry


def is_transient_error(text):
    t = text or ""
    return not FATAL_ERR_RE.search(t) and bool(TRANSIENT_ERR_RE.search(t))


# --------------------------------------------------------------------------- memory (optional AMem)
# Persistent cross-session memory through AMem (https://github.com/lyfeng001/AMem), when installed.
# Glimpse never imports it: it runs `python -m agent_memory` from the configured environment.
# Sessions of the same project share one memory; nothing is written into the project directory.

MEMORY_DEFAULT = {"enabled": False, "python": "", "store": "", "budget": "20G", "keep_days": 30}
MEMORY_TIMEOUT = 60
MEMORY_PROMPT = (
    "You have persistent project memory (AMem MCP server `{server}`, project `{project}`), shared with every "
    "earlier and later Claude Code / Codex session of this project on any machine.\n"
    "- At the start of a task call memory_context; use memory_search / memory_read when earlier work may matter.\n"
    "- Priority: the user's current request and the live repository state always win. Memory is background "
    "evidence: it can be stale, check dates and verify before relying on it, and ignore records that are not "
    "relevant to the current task.\n"
    "- When you reach a verified, durable conclusion (a decision, a constraint, a root cause, a finished "
    "milestone, a handoff for the next session) save it with memory_save under a short stable key "
    "(read the current version first and pass its ID as expected_parents). Do not save guesses, secrets "
    "or step-by-step logs; the full history is captured automatically.")
PROJECT_ID_RE = re.compile(r"[^a-zA-Z0-9_.-]+")
# availability probe: AMem version + the projects already bound in the vault (folder -> project id)
MEMORY_PROBE = ("import json, sys, agent_memory, mcp\n"
                "from agent_memory.store import Store\n"
                "s = Store(sys.argv[1])\n"
                "print(json.dumps({'version': agent_memory.__version__, 'projects': s.projects()}))")


def memory_project_id(cwd):
    """Stable project id: git remote name (same on every machine), else the git top-level / cwd basename."""
    import subprocess
    top, remote = None, None
    try:
        top = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, timeout=5).stdout.strip() or None
        if top:
            remote = subprocess.run(["git", "-C", top, "remote", "get-url", "origin"], capture_output=True,
                                    text=True, timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    if remote:
        name = remote.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        name = name[:-4] if name.endswith(".git") else name
    else:
        name = os.path.basename(top or cwd.rstrip("/")) or "home"
    name = PROJECT_ID_RE.sub("-", name).strip("-._")[:80] or "project"
    if not name[0].isalnum():
        name = "p" + name[:79]
    return name, top or cwd


def memory_external(top):
    """The project already wires AMem itself (its own .mcp.json / .codex/config.toml): leave it alone."""
    for rel in (".mcp.json", os.path.join(".codex", "config.toml")):
        try:
            with open(os.path.join(top, rel), errors="replace") as f:
                if "agent_memory" in f.read(65536):
                    return True
        except OSError:
            pass
    return False


class Memory:
    """Per-daemon AMem integration: config from the hub, availability probe, launch args, capture, queries."""

    def __init__(self, daemon):
        self.d = daemon
        self.cfg = dict(MEMORY_DEFAULT)
        self.version = None            # AMem version once the probe succeeded
        self.error = None
        self.probing = False
        self.running = set()           # projects with a capture in flight
        self.again = set()             # ... and a turn that finished meanwhile
        self.bound = {}                # project -> root registered with `amem init`
        self.roots = {}                # root -> project id already bound in the vault (wins over the derived id)

    def store(self):
        return os.path.expanduser(self.cfg.get("store") or os.path.join(project_dir(), "data", "memory"))

    def python(self):
        return os.path.expanduser(self.cfg.get("python") or "python3")

    def on(self):
        return bool(self.cfg.get("enabled")) and bool(self.version)

    def status(self):
        return {"enabled": bool(self.cfg.get("enabled")), "available": bool(self.version), "version": self.version,
                "error": self.error, "store": self.store(), "budget": self.cfg.get("budget"),
                "keep_days": self.cfg.get("keep_days")}

    def configure(self, cfg, done=None):
        self.cfg = dict(MEMORY_DEFAULT, **{k: v for k, v in (cfg or {}).items() if k in MEMORY_DEFAULT})
        self.version, self.error = None, None
        if not self.cfg["enabled"]:
            if done:
                done()
            return

        def probed(res, err):
            self.probing = False
            if err:
                self.error = err
            else:
                info = json.loads(res)
                self.set_roots(info.get("projects"))
                self.version = info["version"]
                b = str(self.cfg.get("budget") or "0")
                self.run(["budget", "--max-bytes", b, "--keep-days", str(int(self.cfg.get("keep_days") or 30))],
                         lambda r, e: setattr(self, "error", e) if e else None)
            if done:
                done()
        self.probing = True
        self.exec_py(["-c", MEMORY_PROBE, self.store()], probed, "AMem 不可用（需要 Python 3.11+ 且安装 agent-memory-local[mcp]）")

    def configure_now(self, cfg):
        """Blocking variant of configure() (daemon start, before any session is restored)."""
        import subprocess
        self.cfg = dict(MEMORY_DEFAULT, **{k: v for k, v in (cfg or {}).items() if k in MEMORY_DEFAULT})
        try:
            r = subprocess.run([self.python(), "-c", MEMORY_PROBE, self.store()],
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
            if r.returncode == 0:
                info = json.loads(r.stdout)
                self.set_roots(info.get("projects"))
                self.version, self.error = info["version"], None
        except (OSError, subprocess.SubprocessError) as e:
            self.error = clip(str(e), 300)

    def set_roots(self, projects):
        self.roots = {os.path.realpath(v["root"]): k for k, v in (projects or {}).items()
                      if isinstance(v, dict) and isinstance(v.get("root"), str)}
        self.bound.update({k: v for v, k in self.roots.items()})

    def exec_py(self, args, done, fail=None, timeout=MEMORY_TIMEOUT):
        import subprocess
        cmd = [self.python()] + args

        def work():
            try:
                r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                                   cwd=os.path.expanduser("~"))
                out, err = r.stdout.decode("utf-8", "replace"), None
                if r.returncode != 0:
                    msg = r.stderr.decode("utf-8", "replace").strip().splitlines()
                    err = fail or clip(msg[-1] if msg else "exit %s" % r.returncode, 300)
            except (OSError, subprocess.SubprocessError) as e:
                out, err = None, fail or clip(str(e), 300)
            self.d.bg_done.append((done, out, err))
        self.d.background(work)

    def run(self, args, done, timeout=MEMORY_TIMEOUT):
        """amem <args> against the store; done(parsed JSON or text, error)."""
        def parsed(out, err):
            if err:
                done(None, err)
                return
            try:
                done(json.loads(out), None)
            except ValueError:
                done(out, None)
        self.exec_py(["-m", "agent_memory", "--store", self.store()] + args, parsed, timeout=timeout)

    # --- per session
    def attach(self, s):
        """At spawn: decide the session's memory project (None = memory off / project wires AMem itself)."""
        s.memory_project = None
        if not self.on() or s.kind not in ("claude", "codex"):
            return
        project, top = memory_project_id(s.cwd)
        project = self.roots.get(os.path.realpath(top), project)   # e.g. a vault imported from an older setup
        if memory_external(top) or memory_external(os.path.expanduser("~")):
            return
        s.memory_project, s.memory_root = project, top
        self.capture(s)          # import this project's earlier conversations now, not after the first turn

    def server_entry(self, project):
        return {"command": self.python(),
                "args": ["-m", "agent_memory", "--store", self.store(), "serve", "--project", project, "--no-context"]}

    def prompt(self, project):
        return MEMORY_PROMPT.format(server="amem", project=project)

    def claude_args(self, s):
        """Extra claude argv: the MCP server and the memory instructions."""
        if not getattr(s, "memory_project", None):
            return []
        cfg = json.dumps({"mcpServers": {"amem": self.server_entry(s.memory_project)}})
        return ["--mcp-config", cfg, "--append-system-prompt", self.prompt(s.memory_project)]

    def claude_allow(self, s):
        """Tools pre-approved in the merged settings, so memory calls never stop on a permission prompt."""
        if not getattr(s, "memory_project", None):
            return []
        return ["mcp__amem__" + t for t in ("memory_context", "memory_search", "memory_read", "memory_save",
                                             "memory_status")]

    def codex_args(self, s):
        if not getattr(s, "memory_project", None):
            return []
        e = self.server_entry(s.memory_project)
        return ["-c", "mcp_servers.amem.command=%s" % json.dumps(e["command"]),
                "-c", "mcp_servers.amem.args=%s" % json.dumps(e["args"]),
                "-c", "mcp_servers.amem.default_tools_approval_mode=\"approve\"",
                "-c", "developer_instructions=%s" % json.dumps(self.prompt(s.memory_project), ensure_ascii=False)]

    def capture(self, s):
        """A turn ended: archive the new transcript lines (and enforce the budget). One run per project at a time."""
        project = getattr(s, "memory_project", None)
        if not project or not self.on():
            return
        if project in self.running:
            self.again.add(project)
            return
        self.running.add(project)
        root = s.memory_root

        def finished(_res, err):
            self.running.discard(project)
            if err:
                self.error = "记忆采集失败: %s" % err
            if project in self.again:
                self.again.discard(project)
                self.capture(s)

        def refresh(_res=None, err=None):
            if err:
                finished(None, err)
                return
            self.bound[project] = root
            self.run(["refresh", "--project", project, "--quiet", "--no-context"], finished, timeout=600)
        if self.bound.get(project) == root:
            refresh()
        else:
            self.run(["init", "--project", project, "--root", root], refresh)

    # --- op "memory" (phone)
    def query(self, m, done):
        if not self.cfg.get("enabled"):
            done(None, "记忆功能未开启（config.json 里的 memory.enabled）")
            return
        if not self.version:
            done(None, self.error or "AMem 还没准备好")
            return
        action, project = m.get("action"), m.get("project")
        if project is not None and (not isinstance(project, str) or not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$", project)):
            done(None, "无效的项目")
            return
        q = m.get("q")
        if action == "overview":
            args = ["vault"]
        elif action == "memories" and project:
            args = ["memories", "--project", project]
        elif action == "context" and project:
            args = ["context", "--project", project, "--chars", "20000"]
        elif action == "sessions" and project:
            args = ["sessions", "--project", project, "--limit", "20"]
        elif action == "search" and project and isinstance(q, str) and q.strip() and len(q) <= 200:
            args = ["search", "--project", project, "--limit", "30", q]
        elif action == "compact":
            args = ["compact"] + (["--dry-run"] if m.get("dry_run") else [])
        elif action == "budget":
            b = m.get("budget")
            if not isinstance(b, str) or not re.match(r"^(0|\d+(\.\d+)?\s*[KMGT]i?B?)$", b.strip(), re.I):
                done(None, "无效的上限（例如 20G）")
                return
            args = ["budget", "--max-bytes", b.strip(), "--keep-days", str(int(self.cfg.get("keep_days") or 30))]
        else:
            done(None, "无效的记忆操作")
            return
        if action == "search":
            # results can be long tool outputs: the phone only needs a preview
            def trimmed(res, err):
                if isinstance(res, list):
                    for r in res:
                        r["text"] = clip(r.get("text") or "", 600)
                done(res, err)
            self.run(args, trimmed)
        else:
            self.run(args, done, timeout=900 if action == "compact" else MEMORY_TIMEOUT)


# --------------------------------------------------------------------------- model lists (op "models")

def check_init_cmds(cmds):
    """create's init_cmds: up to 4 slash commands, typed once claude is up."""
    if cmds is None:
        return []
    if not isinstance(cmds, list) or len(cmds) > INIT_CMDS_MAX or not all(
            isinstance(x, str) and x.startswith("/") and len(x) <= 200 and not re.search(r"[\x00-\x1f\x7f]", x)
            for x in cmds):
        raise RuntimeError("无效的 init_cmds")
    return list(cmds)


def settings_arg(argv):
    """The value of --settings in a claude argv (path or inline JSON), or None."""
    argv = list(argv or [])
    for i, a in enumerate(argv):
        if a == "--settings" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--settings="):
            return a.split("=", 1)[1]
    return None


def claude_model_env(argv):
    """Only the three custom model names from the --settings env (the file also carries tokens)."""
    src = settings_arg(argv)
    if not src:
        return {}
    try:
        if src.lstrip().startswith("{"):
            data = json.loads(src)
        else:
            with open(os.path.expanduser(src)) as f:
                data = json.load(f)
    except (OSError, ValueError):
        return {}
    env = data.get("env") if isinstance(data, dict) else None
    if not isinstance(env, dict):
        return {}
    return {k: clip(env[k], 120) for k in CLAUDE_MODEL_ENV.values() if isinstance(env.get(k), str) and env[k]}


def claude_models(argv, current=None):
    env = claude_model_env(argv)
    desc = {"default": "Recommended for your account" + (" (currently %s)" % current if current else ""),
            "opus": "Most capable", "sonnet": "Everyday tasks", "haiku": "Fastest",
            "fable": "Fable", "opusplan": "Opus in plan mode, Sonnet otherwise"}
    out = []
    for mid, label in CLAUDE_MODEL_LIST:
        d = desc[mid]
        custom = env.get(CLAUDE_MODEL_ENV.get(mid, ""))
        if custom:
            d = "Custom %s model (%s)" % (label, custom)
        out.append({"id": mid, "label": label, "desc": d, "efforts": None, "default_effort": None})
    return {"models": out, "efforts": list(CLAUDE_EFFORTS), "modes": list(CLAUDE_MODE_LIST)}


def codex_models_cmd(argv):
    """`codex debug models`, keeping the preset's config flags (-c / --enable / --disable; it rejects -p)."""
    argv = list(argv or ["codex"])
    cmd = [argv[0] if argv else "codex", "debug", "models"]
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in ("-c", "--config", "--enable", "--disable") and i + 1 < len(argv):
            cmd += [a, argv[i + 1]]
            i += 2
            continue
        if a.startswith(("--config=", "--enable=", "--disable=")):
            cmd.append(a)
        i += 1
    return cmd


def parse_codex_models(text):
    i = text.find('{"models"')      # rc files of the login shell may print something first
    i = text.find("{") if i < 0 else i
    data = json.JSONDecoder().raw_decode(text[max(i, 0):])[0]
    rows = [m for m in data.get("models") or [] if isinstance(m, dict) and m.get("slug")
            and m.get("visibility", "list") == "list"]
    rows.sort(key=lambda m: m.get("priority") if isinstance(m.get("priority"), (int, float)) else 1e9)
    models, seen = [], set()
    for m in rows:
        effs = [e.get("effort") for e in m.get("supported_reasoning_levels") or []
                if isinstance(e, dict) and isinstance(e.get("effort"), str)]
        seen.update(effs)
        models.append({"id": clip(str(m["slug"]), 100), "label": clip(str(m.get("display_name") or m["slug"]), 100),
                       "desc": clip(str(m.get("description") or ""), 300), "efforts": effs or None,
                       "default_effort": m.get("default_reasoning_level") if isinstance(
                           m.get("default_reasoning_level"), str) else None})
    efforts = [e for e in EFFORT_ORDER if e in seen] + sorted(e for e in seen if e not in EFFORT_ORDER)
    return {"models": models, "efforts": efforts, "modes": []}


def run_login_shell(cmd, timeout):
    """Run cmd through the user's login shell (PATH as in their terminal). Returns stdout text."""
    import subprocess
    shell = os.environ.get("SHELL") or "/bin/sh"
    env = {k: v for k, v in os.environ.items() if not k.startswith(STRIP_ENV)}
    r = subprocess.run([shell, "-l", "-i", "-c", 'exec "$@"', "glimpse"] + cmd, stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=env,
                       cwd=os.path.expanduser("~"))
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError("%s exited %s%s" % (cmd[0], r.returncode, (": " + clip(err[-1], 200)) if err else ""))
    return r.stdout.decode("utf-8", "replace")


# --------------------------------------------------------------------------- daemon

def peer_ok(sock):
    """Only the same uid may talk to the daemon (Linux abstract sockets have no file permissions)."""
    so_peercred = getattr(socket, "SO_PEERCRED", None)
    if so_peercred is None:
        return True      # macOS: path socket in a 0700 dir, mode 0600
    try:
        _pid, uid, _gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, so_peercred, struct.calcsize("3i")))
    except OSError:
        return False
    return uid == os.getuid()


NETINFO_URL = "https://ipinfo.io/json"   # public IP + approximate location of this machine's internet exit
NETINFO_TTL = 600
SPEED_URL = "https://speed.cloudflare.com/__down?bytes=%d"   # public speed-test endpoint, no account
SPEED_BYTES = 10 * 1024 * 1024
SPEED_TIMEOUT = 25


def speed_test(nbytes=SPEED_BYTES, timeout=SPEED_TIMEOUT):
    """Download speed of this machine's internet connection: time to first byte (latency) and bits/s over a
    fixed-size download (stops early at the timeout and reports what it measured)."""
    import urllib.request
    t0 = time.time()
    req = urllib.request.Request(SPEED_URL % nbytes, headers={"User-Agent": "glimpse"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        first = r.read(1)
        t1 = time.time()
        got = len(first)
        while got < nbytes and time.time() - t0 < timeout:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            got += len(chunk)
    t2 = time.time()
    secs = max(t2 - t1, 1e-3)
    return {"down_bps": round(got * 8 / secs), "bytes": got, "secs": round(t2 - t0, 2), "ttfb_ms": round((t1 - t0) * 1000, 1)}


def fetch_netinfo(timeout=8):
    """{ip, city, region, country, org} of the address this machine reaches the internet from."""
    import urllib.request
    req = urllib.request.Request(NETINFO_URL, headers={"Accept": "application/json", "User-Agent": "glimpse"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read(65536).decode("utf-8", "replace"))
    if not isinstance(d, dict) or not d.get("ip"):
        raise RuntimeError("no address in the reply")
    return {k: clip(str(d.get(k) or ""), 80) for k in ("ip", "city", "region", "country", "org", "timezone")}


# --------------------------------------------------------------------------- files (read-only, session cwd)
# The phone browses the session's working directory: list / read / git status / git diff. Everything is
# confined to that directory (realpath, so `..` and symlinks cannot leave it) and nothing is ever written.
FS_HIDE = {".git", ".DS_Store", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".idea", "Thumbs.db"}
FS_DIM = {"node_modules", ".venv", "venv", "dist", "build", "target", ".next", ".cache", ".tox", ".gradle"}
FS_MAX_ENTRIES = 2000
FS_TEXT_MAX = 512 * 1024
FS_IMAGE_MAX = 3 * 1024 * 1024
FS_GIT_MAX = 2000
FS_DIFF_MAX = 400 * 1024
FS_IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
             ".webp": "image/webp", ".svg": "image/svg+xml", ".bmp": "image/bmp", ".ico": "image/x-icon"}
# names whose content is only read after an explicit "show" on the phone
FS_SENSITIVE_RE = re.compile(
    r"^(\.env(\..*)?|.*\.(pem|key|p12|pfx|jks|keystore|kdbx|tfstate|tfvars)|id_(rsa|dsa|ecdsa|ed25519)(\.old)?|"
    r"\.netrc|\.npmrc|\.pypirc|\.pgpass|\.htpasswd|credentials(\.json)?|secrets?(\..*)?|.*\.secrets?|"
    r"service[-_]?account.*\.json|key-[0-9a-f]+)$", re.I)
FS_NOT_SENSITIVE_RE = re.compile(r"\.(example|sample|template|dist|defaults?)$|\.pub$", re.I)
# secrets in a file's first bytes (a config.json with a key in it is gated as well): well-known key formats, and
# a long random-looking value assigned to a key / token / secret / password field
FS_SECRET_RE = re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(sk-(ant-|proj-)?[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|"
                          rb"gh[pousr]_[A-Za-z0-9]{30,}|xox[abp]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35})|"
                          rb"(?i:(api[_-]?key|secret|token|passw(or)?d|private[_-]?key|access[_-]?key)[\"']?\s*[:=]\s*[\"']?)"
                          rb"[A-Za-z0-9+/_\-.=]{24,}")


# Glimpse's own secrets on this machine (data/: login token, VAPID / auth keys, per-session key files; config.json
# with the API keys): always gated, whatever their names. Filled in by the daemon (real paths).
FS_PRIVATE = set()


def fs_private_paths(sdir):
    out = set()
    if sdir:
        d = os.path.realpath(sdir)
        out.add(d)
        if os.path.basename(d) == "agent" and os.path.basename(os.path.dirname(d)) == "data":
            out.add(os.path.dirname(d))                     # <project>/data (hub token, auth, VAPID, push, memory)
    cfg = os.environ.get("GLIMPSE_CONFIG") or os.path.join(project_dir(), "config.json")
    out.add(os.path.realpath(cfg))
    return out


def fs_sensitive(name, real=None):
    if real and any(real == p or real.startswith(p.rstrip(os.sep) + os.sep) for p in FS_PRIVATE):
        return True
    return bool(FS_SENSITIVE_RE.match(name)) and not FS_NOT_SENSITIVE_RE.search(name)


def fs_resolve(root, rel):
    """(real root, real path) for `rel` inside the session directory `root`; ValueError when it leaves it."""
    if not isinstance(rel, str) or len(rel) > 4096 or "\0" in rel:
        raise ValueError("无效的路径")
    base = os.path.realpath(root)
    p = os.path.realpath(os.path.join(base, rel.lstrip("/")))
    if p != base and not p.startswith(base.rstrip(os.sep) + os.sep):
        raise ValueError("路径不在会话目录内")
    return base, p


def fs_rel(base, p):
    r = os.path.relpath(p, base)
    return "" if r == "." else r.replace(os.sep, "/")


def fs_list(root, rel):
    base, p = fs_resolve(root, rel)
    out, more = [], False
    with os.scandir(p) as it:
        for e in it:
            if e.name in FS_HIDE:
                continue
            if len(out) >= FS_MAX_ENTRIES:
                more = True
                break
            item = {"name": e.name}
            try:
                if e.is_symlink():
                    item["link"] = True
                    t = os.path.realpath(e.path)
                    if t != base and not t.startswith(base.rstrip(os.sep) + os.sep):
                        item["out"] = True            # points outside the session directory: not opened
                is_dir = e.is_dir()
                st = e.stat() if not item.get("out") else e.stat(follow_symlinks=False)
            except OSError:
                is_dir, st = False, None
            item["dir"] = is_dir
            if st is not None and not is_dir:
                item["size"], item["mtime"] = st.st_size, int(st.st_mtime)
            if is_dir and e.name in FS_DIM:
                item["dim"] = True
            if not is_dir and fs_sensitive(e.name, os.path.realpath(e.path) if FS_PRIVATE else None):
                item["sensitive"] = True
            out.append(item)
    out.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return {"path": fs_rel(base, p), "entries": out, "more": more}


def fs_decode(data):
    for enc in ("utf-8", "gb18030"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", "replace"), "utf-8?"


def fs_read(root, rel, force=False):
    base, p = fs_resolve(root, rel)
    st = os.stat(p)
    import stat as _stat
    if not _stat.S_ISREG(st.st_mode):
        raise ValueError("不是普通文件")
    name = os.path.basename(p)
    r = {"path": fs_rel(base, p), "size": st.st_size, "mtime": int(st.st_mtime)}
    if fs_sensitive(name, p) and not force:
        return dict(r, kind="sensitive")
    mime = FS_IMAGES.get(os.path.splitext(name)[1].lower())
    if mime:
        if st.st_size > FS_IMAGE_MAX:
            return dict(r, kind="large")
        with open(p, "rb") as f:
            return dict(r, kind="image", mime=mime, data=b64e(f.read(FS_IMAGE_MAX)))
    with open(p, "rb") as f:
        data = f.read(FS_TEXT_MAX)
    if b"\0" in data[:8192]:
        return dict(r, kind="binary")
    if not force and FS_SECRET_RE.search(data[:65536]):
        return dict(r, kind="sensitive", reason="content")
    text, enc = fs_decode(data)
    return dict(r, kind="text", text=text, encoding=enc, truncated=st.st_size > FS_TEXT_MAX)


def fs_git_run(cwd, args, limit=4 * 1024 * 1024, timeout=10, ok=(0,)):
    import subprocess
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", LC_ALL="C", GIT_TERMINAL_PROMPT="0")
    r = subprocess.run(["git", "-c", "core.quotepath=off"] + args, cwd=cwd, env=env, capture_output=True, timeout=timeout)
    if r.returncode not in ok:
        raise RuntimeError(r.stderr.decode("utf-8", "replace").strip()[:200] or "git failed")
    return r.stdout[:limit]


def fs_git(root):
    """Changed files under the session directory: {"repo": bool, "branch", "files": [{"path", "st", "add", "del"}]}."""
    base, _ = fs_resolve(root, "")
    try:
        top = fs_git_run(base, ["rev-parse", "--show-toplevel"]).decode().strip()
    except (RuntimeError, OSError):
        return {"repo": False, "files": []}
    try:
        branch = fs_git_run(base, ["rev-parse", "--abbrev-ref", "HEAD"]).decode().strip()
    except (RuntimeError, OSError):
        branch = ""
    raw = fs_git_run(base, ["status", "--porcelain=v1", "-z", "--untracked-files=normal", "--", "."]).split(b"\0")
    files, i = [], 0
    while i < len(raw):
        ent = raw[i]
        i += 1
        if len(ent) < 4:
            continue
        xy, path = ent[:2].decode(), ent[3:].decode("utf-8", "replace")
        if xy[0] in "RC" and i < len(raw):
            i += 1                                     # the old path of a rename
        rel = fs_rel(base, os.path.join(top, path))
        if rel.startswith("../") or rel == "..":
            continue
        st = "U" if xy == "??" else "C" if "U" in xy or xy in ("AA", "DD") else \
            "D" if "D" in xy else "A" if "A" in xy else "R" if "R" in xy else "M"
        files.append({"path": rel.rstrip("/"), "st": st, "dir": path.endswith("/")})
        if len(files) >= FS_GIT_MAX:
            break
    try:                                               # line counts against HEAD (none in a repo without commits)
        num = fs_git_run(base, ["diff", "--numstat", "-z", "HEAD", "--", "."]).split(b"\0")
        counts, j = {}, 0
        while j < len(num):
            f = num[j].split(b"\t")
            j += 1
            if len(f) == 3:
                path = f[2]
                if not path and j + 1 < len(num):      # rename: "a\td\t" then old\0new\0
                    path, j = num[j + 1], j + 2
                counts[fs_rel(base, os.path.join(top, path.decode("utf-8", "replace")))] = (f[0].decode(), f[1].decode())
        for x in files:
            a, d = counts.get(x["path"], ("", ""))
            if a.isdigit():
                x["add"], x["del"] = int(a), int(d)
    except (RuntimeError, OSError):
        pass
    return {"repo": True, "branch": branch, "files": files}


def fs_diff(root, rel, force=False):
    base, p = fs_resolve(root, rel)
    r = {"path": fs_rel(base, p)}
    if fs_sensitive(os.path.basename(p), p) and not force:
        return dict(r, kind="sensitive")
    rp = os.path.relpath(p, base)
    try:
        out = fs_git_run(base, ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "HEAD", "--", rp], FS_DIFF_MAX)
    except RuntimeError:
        out = fs_git_run(base, ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--", rp], FS_DIFF_MAX)
    if not out and os.path.isfile(p):                  # untracked: everything is new (--no-index exits 1 on a difference)
        out = fs_git_run(base, ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-index", "--",
                                os.devnull, rp], FS_DIFF_MAX, ok=(0, 1))
    if not force and FS_SECRET_RE.search(out[:65536]):
        return dict(r, kind="sensitive", reason="content")
    if b"Binary files" in out[:4096] and b"@@" not in out[:4096]:
        return dict(r, kind="binary")
    return dict(r, kind="diff", text=fs_decode(out)[0], truncated=len(out) >= FS_DIFF_MAX)


def sys_stats(prev):
    """load, cpu count, memory %, NIC byte rates. Returns (stats, state)."""
    st = {"load": list(os.getloadavg()) if hasattr(os, "getloadavg") else None, "ncpu": os.cpu_count() or 1,
          "mem_pct": None, "rx_bps": None, "tx_bps": None, "ts": now()}
    rx = tx = None
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo") as f:
                mi = dict((ln.split(":")[0], int(ln.split()[1])) for ln in f if len(ln.split()) >= 2)
            if mi.get("MemTotal"):
                st["mem_pct"] = round(100.0 * (1 - mi.get("MemAvailable", 0) / mi["MemTotal"]), 1)
            rx = tx = 0
            with open("/proc/net/dev") as f:
                for ln in f.readlines()[2:]:
                    name, data = ln.split(":", 1)
                    if name.strip() == "lo":
                        continue
                    v = data.split()
                    rx += int(v[0])
                    tx += int(v[8])
        elif sys.platform == "darwin":
            import subprocess
            out = subprocess.run(["netstat", "-ibn"], capture_output=True, text=True, timeout=3).stdout
            rx = tx = 0
            seen = set()
            for ln in out.splitlines()[1:]:
                v = ln.split()
                if len(v) >= 10 and v[2].startswith("<Link") and not v[0].startswith("lo") and v[0] not in seen:
                    seen.add(v[0])
                    rx += int(v[6])
                    tx += int(v[9])
            vm = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=3).stdout
            pg = 16384
            m = re.search(r"page size of (\d+)", vm)
            if m:
                pg = int(m.group(1))
            vals = dict((k.strip(), int(v.strip().rstrip("."))) for k, v in
                        (ln.split(":", 1) for ln in vm.splitlines()[1:] if ":" in ln) if v.strip().rstrip(".").isdigit())
            total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
                                       timeout=3).stdout.strip() or 0)
            used = (vals.get("Pages active", 0) + vals.get("Pages wired down", 0) +
                    vals.get("Pages occupied by compressor", 0)) * pg
            if total:
                st["mem_pct"] = round(100.0 * used / total, 1)
    except Exception:
        pass
    if rx is not None and prev and prev[0] is not None:
        dt = st["ts"] - prev[2]
        if dt > 0:
            st["rx_bps"] = max(0, (rx - prev[0]) / dt)
            st["tx_bps"] = max(0, (tx - prev[1]) / dt)
    return st, (rx, tx, st["ts"])


class Client:
    def __init__(self, sock):
        self.sock = sock
        self.rbuf = b""
        self.wbuf = bytearray()
        self.kind = None
        self.subs = set()


class Daemon:
    def __init__(self, sock_name, sdir):
        self.sock_name = sock_name     # "@name" = linux abstract socket
        self.sdir = sdir               # None = keep no files
        FS_PRIVATE.update(fs_private_paths(sdir))      # the files view never shows these without 显示内容
        self.sessions = {}
        self.order = []
        self.clients = set()
        self.sel = selectors.DefaultSelector()
        self.timers = []
        self.pty_wbuf = {}
        self.idle_since = now()
        self.host = socket.gethostname()
        self.approval_wait = float(os.environ.get("GLIMPSE_APPROVAL_WAIT") or 600)
        self.net_prev = None
        self.usage_today = UsageToday()
        self.models_cache = {}         # codex `debug models` argv -> (time, result)
        self.models_wait = {}          # same key -> callbacks waiting for the running lookup
        self.bg_done = []              # (callback, result, error) from worker threads, run in tick()
        self.memory = Memory(self)     # optional AMem integration (configured by the hub)

    # --- plumbing
    def listen(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if self.sock_name.startswith("@"):
            s.bind("\0" + self.sock_name[1:])
        else:
            # never steal the socket of a live daemon (two clients racing to start one)
            try:
                connect(self.sock_name, timeout=1).close()
                raise RuntimeError("daemon already running")
            except OSError:
                pass
            try:
                os.unlink(self.sock_name)
            except OSError:
                pass
            old = os.umask(0o177)
            try:
                s.bind(self.sock_name)
            finally:
                os.umask(old)
        s.listen(16)
        s.setblocking(False)
        set_cloexec(s.fileno())
        self.lsock = s
        self.sel.register(s, selectors.EVENT_READ, ("listen", None))

    def _mask(self, c):
        return selectors.EVENT_READ | (selectors.EVENT_WRITE if c.wbuf else 0)

    def send(self, c, obj):
        if c not in self.clients:
            return
        # "replace": transcripts can contain lone surrogates (strings cut mid-emoji)
        c.wbuf += (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8", "replace")
        if len(c.wbuf) > 32 * 1024 * 1024:
            self.drop(c)
            return
        self._flush(c)

    def _flush(self, c):
        try:
            while c.wbuf:
                n = c.sock.send(c.wbuf[:262144])
                del c.wbuf[:n]
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            self.drop(c)
            return
        try:
            self.sel.modify(c.sock, self._mask(c), ("client", c))
        except (KeyError, ValueError):
            pass

    def drop(self, c):
        if c not in self.clients:
            return
        self.clients.discard(c)
        for sid in c.subs:
            s = self.sessions.get(sid)
            if s:
                s.subs.discard(c)
        for s in self.sessions.values():
            if s.pendings:
                s.release_pendings(waiter=c)   # its hook process died (claude timeout, Ctrl-C ...)
        try:
            self.sel.unregister(c.sock)
        except (KeyError, ValueError):
            pass
        c.sock.close()

    def broadcast(self, obj):
        for c in list(self.clients):
            if c.kind == "hub":
                self.send(c, obj)

    def send_subs(self, s, obj):
        for c in list(s.subs):
            self.send(c, obj)

    def has_terminal(self, s):
        return any(c.kind == "attach" for c in s.subs)

    def taken_rollouts(self):
        return {s.transcript for s in self.sessions.values() if s.transcript}

    def pty_write(self, s, data):
        buf = self.pty_wbuf.setdefault(s.sid, bytearray())
        buf += data
        self._pty_flush(s)

    def _pty_flush(self, s):
        buf = self.pty_wbuf.get(s.sid)
        if s.fd is None:
            self.pty_wbuf.pop(s.sid, None)
            return
        try:
            while buf:
                n = os.write(s.fd, bytes(buf[:4096]))
                del buf[:n]
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            buf.clear()
        try:
            self.sel.modify(s.fd, selectors.EVENT_READ | (selectors.EVENT_WRITE if buf else 0), ("pty", s))
        except (KeyError, ValueError):
            pass

    def later(self, delay, fn):
        self.timers.append((now() + delay, fn))

    # --- sessions
    def session_list(self):
        return [self.sessions[k].info() for k in self.order if k in self.sessions]

    # --- persistence: a restarted daemon (upgrade, crash, reboot) resumes the conversations that were live
    def state_file(self):
        return os.path.join(self.sdir, "sessions.json") if self.sdir else None

    def save_state(self):
        """Live claude / codex sessions with a known conversation id. No key values / env: the key is looked
        up again by name in config.json when the session is restored."""
        path = self.state_file()
        if not path:
            return
        rows = []
        for k in self.order:
            s = self.sessions.get(k)
            if s and s.fd is not None and s.kind in ("claude", "codex") and s.agent_session_id:
                rows.append({"sid": s.sid, "kind": s.kind, "cwd": s.cwd, "name": s.name, "argv": list(s.argv),
                             "key": s.key, "conv": s.agent_session_id, "auto_continue": s.auto_continue,
                             "overrides": dict(s.overrides), "cols": s.cols, "rows": s.rows})
        data = json.dumps({"v": 1, "sessions": rows, "memory": self.memory.cfg}, ensure_ascii=False)
        if data == getattr(self, "_saved", None):
            return
        tmp = path + ".tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(data)
            os.replace(tmp, path)
            self._saved = data
        except OSError as e:
            sys.stderr.write("glimpse: cannot save sessions: %s\n" % e)

    def restore_state(self):
        """Resume the sessions saved by the previous daemon (same sid, so phones and attach keep working)."""
        path = self.state_file()
        if not path:
            return
        try:
            with open(path) as f:
                saved = json.load(f) or {}
            rows = saved.get("sessions") or []
        except (OSError, ValueError):
            return
        mcfg = saved.get("memory") if isinstance(saved.get("memory"), dict) else local_memory_config()
        if mcfg and mcfg.get("enabled") and rows:
            # the hub sends the memory settings only after it connected: restored sessions need them at spawn
            self.memory.configure_now(mcfg)
        for r in rows:
            try:
                if not isinstance(r, dict) or r.get("kind") not in ("claude", "codex") or r.get("sid") in self.sessions:
                    continue
                env, key = None, r.get("key")
                if key:
                    try:
                        _, key, env = local_launch(r["kind"], key)
                    except RuntimeError:
                        key = None         # the key was removed from config.json: start without it
                s = self.create(r["kind"], r.get("cwd"), r.get("name"), [str(a) for a in r.get("argv") or []],
                                r.get("cols"), r.get("rows"), env, key if env else None, r.get("conv"),
                                sid=r.get("sid"), overrides=r.get("overrides"))
                s.auto_continue = bool(r.get("auto_continue"))
                it = s.add_chat("system", "—— Glimpse 守护进程已重启，已自动恢复这个对话 ——")
                if it:
                    self.send_subs(s, {"t": "chat", "sid": s.sid, "items": [it]})
                sys.stderr.write("glimpse: restored %s (%s %s)\n" % (s.sid, s.kind, s.agent_session_id))
            except Exception as e:
                sys.stderr.write("glimpse: cannot restore %s: %s\n" % (r.get("sid") if isinstance(r, dict) else r, e))
        self.save_state()


    def create(self, kind, cwd, name, args, cols, rows, env=None, key=None, resume_id=None, init_cmds=None,
               sid=None, overrides=None):
        """New session. With resume_id a session that already owns that conversation is reused:
        running -> returned as is, exited -> restarted in place (see create_or_reuse)."""
        if kind not in DEFAULT_PRESETS:
            raise RuntimeError("unknown kind %r" % kind)
        cmds = check_init_cmds(init_cmds) if kind == "claude" else []
        if resume_id is not None:
            if kind not in ("claude", "codex") or not isinstance(resume_id, str) or not ID_RE.match(resume_id):
                raise RuntimeError("无效的对话 id")
            for o in self.sessions.values():
                if o.agent_session_id == resume_id and o.fd is not None:
                    raise RuntimeError("这个对话已经在会话 %s 中运行" % o.sid)
        argv = list(args) if args else list(DEFAULT_PRESETS[kind]["argv"])
        argv = [os.path.expanduser(a) if a.startswith("~/") else a for a in argv]
        if args and kind != "shell" and argv[0] != kind and not os.path.isabs(argv[0]):
            argv = DEFAULT_PRESETS[kind]["argv"] + argv   # args given without the program name
        cwd = os.path.abspath(os.path.expanduser(cwd or "~"))
        if not os.path.isdir(cwd):
            raise RuntimeError("目录不存在: %s" % cwd)
        if not (isinstance(sid, str) and re.match(r"^[0-9a-f]{6}$", sid) and sid not in self.sessions):
            sid = os.urandom(3).hex()
            while sid in self.sessions:
                sid = os.urandom(3).hex()
        nm = name or "%s · %s" % (kind, os.path.basename(cwd) or cwd)
        s = Session(self, sid, kind, nm, cwd, argv, int(cols or 100), int(rows or 32))
        if env:
            s.env = {str(k): str(v) for k, v in env.items()}
        s.key = key
        s.init_cmds = cmds
        if kind == "codex" and isinstance(overrides, dict):
            s.overrides = {k: str(v) for k, v in overrides.items() if k in ("model", "effort", "approval", "sandbox") and v}
        if resume_id:
            s.agent_session_id = resume_id
        self.sessions[sid] = s          # before spawn: a resumed rollout is imported with send_subs etc.
        try:
            s.spawn(resume=bool(resume_id))
        except Exception:
            del self.sessions[sid]
            raise
        self.order.append(sid)
        self.sel.register(s.fd, selectors.EVENT_READ, ("pty", s))
        self.gc_exited()
        return s

    def owner_of(self, kind, resume_id):
        """The session that owns this claude / codex conversation (a running one first), or None."""
        if not resume_id:
            return None
        found = [o for o in self.sessions.values() if o.kind == kind and o.agent_session_id == resume_id]
        found.sort(key=lambda o: (o.fd is not None, o.activity))
        return found[-1] if found else None

    def create_or_reuse(self, m, done):
        """op create. done(session, existing, err). A resumed conversation already owned by a session
        shows up as that same session: running -> returned, exited -> restarted in place."""
        kind = m.get("kind") or "claude"
        resume_id = m.get("resume_id") or None
        o = self.owner_of(kind, resume_id) if isinstance(resume_id, str) else None
        if o is None:
            done(self.create(kind, m.get("cwd"), m.get("name"), m.get("args"), m.get("cols"), m.get("rows"),
                             m.get("env"), m.get("key"), resume_id, m.get("init_cmds")), False, None)
            return
        if o.fd is not None:
            done(o, True, None)
            return
        env = m.get("env")
        if env is not None and not isinstance(env, dict):
            raise RuntimeError("bad env")
        if kind == "claude":
            o.init_cmds = check_init_cmds(m.get("init_cmds"))
        o.restart({str(k): str(v) for k, v in (env or {}).items()} if env is not None or m.get("key") is not None
                  else None, m.get("key"), True, lambda err: done(o, True, err))

    def gc_exited(self):
        dead = [k for k in self.order if self.sessions[k].fd is None]
        for k in dead[:-EXITED_MAX] if len(dead) > EXITED_MAX else []:
            self.forget(k)

    def forget(self, sid):
        s = self.sessions.get(sid)
        if not s or s.fd is not None:
            return False
        del self.sessions[sid]
        self.order.remove(sid)
        self.broadcast({"t": "gone", "sid": sid})
        return True

    def kill(self, s):
        if s.pid and s.fd is not None:
            s.killed = True
            for sig in (signal.SIGHUP, signal.SIGTERM):
                try:
                    os.killpg(os.getpgid(s.pid), sig)
                except OSError:
                    try:
                        os.kill(s.pid, sig)
                    except OSError:
                        pass

            def hard(pid=s.pid, ss=s):
                if ss.fd is not None and ss.pid == pid:
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
                    except OSError:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except OSError:
                            pass
            self.later(3, hard)

    def type_text(self, s, text):
        text = text.replace("\r\n", "\n")
        if s.paste_mode:
            s.write(b"\x1b[200~" + text.encode() + b"\x1b[201~")
            self.later(0.35, lambda: s.write(b"\r"))
        else:
            s.write(text.replace("\n", "\r").encode())
            self.later(0.15, lambda: s.write(b"\r"))

    def delete(self, s, done):
        """Forget a session; a running one is killed first and forgotten once it exited."""
        if s.fd is None:
            done(self.forget(s.sid))
            return
        s.restart_req = None             # a pending restart must not bring it back
        s.delete_req = done
        self.kill(s)

    def control(self, s, action, value, done):
        """Session controls from the phone: claude mode / model / effort / goal, codex launch overrides."""
        value = "" if value is None else value
        if not isinstance(value, str):
            raise RuntimeError("bad value")
        value = value.strip()
        if s.kind == "claude":
            if s.fd is None:
                raise RuntimeError("会话没有在运行")
            if action == "mode":
                target = CLAUDE_MODE_ALIAS.get(value, value)
                if target not in CLAUDE_MODES:
                    raise RuntimeError("未知模式 %s" % value)
                self.cycle_mode(s, target, done)
                return
            if action == "model":
                if not VALUE_RE.match(value):
                    raise RuntimeError("无效的模型名")
                cmd = "/model " + value
            elif action == "effort":
                if value not in CLAUDE_EFFORTS:
                    raise RuntimeError("无效的推理强度 %s" % value)
                cmd = "/effort " + value
            elif action == "goal":
                goal = clip(re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip(), 2000)
                cmd = "/goal " + (goal or "clear")
            else:
                raise RuntimeError("未知操作 %s" % action)
            # one slash command at a time: the next one waits until the previous Enter was sent
            t = now()
            wait = max(0.0, getattr(s, "type_busy", 0) - t)
            s.type_busy = t + wait + 0.8
            if wait:
                self.later(wait, lambda: s.fd is not None and self.type_text(s, cmd))
            else:
                self.type_text(s, cmd)
            done(None)
        elif s.kind == "codex":
            if action not in CODEX_OVERRIDES:
                raise RuntimeError("未知操作 %s" % action)
            if value and not VALUE_RE.match(value):
                raise RuntimeError("无效的值")
            def keep(err=None):
                # remember the choice: a later restart (key swap, daemon restart) launches with it
                if err is None:
                    s.overrides[action] = value
                    if action in ("model", "effort"):
                        s.cx_mode = dict(s.cx_mode, **{action: value})
                    s.touch()
                done(err)

            def restart():
                if value:
                    s.overrides[action] = value
                else:
                    s.overrides.pop(action, None)   # back to the preset / config.toml default
                s.cx_mode = dict(s.cx_mode, **{action: value}) if value else s.cx_mode
                s.touch()
                s.restart(None, None, True, done)   # not settable in the running session: restart and resume

            def walked(err):
                if err is None or err.startswith("会话"):
                    keep(err)                  # done, or busy / exited: report it, no restart behind the user's back
                else:
                    sys.stderr.write("glimpse: codex %s=%s needs a restart: %s\n" % (action, value, err))
                    restart()
            if s.fd is None or not value:
                restart()
            elif action in ("model", "effort"):
                cur = dict(s.cx_mode, **s.overrides)
                self.codex_set_model(s, value if action == "model" else cur.get("model"),
                                     value if action == "effort" else cur.get("effort"), walked)
            else:
                self.codex_set_permissions(s, action, value, walked)
        else:
            raise RuntimeError("只有 Claude / Codex 会话支持")

    # --- codex: change model / effort / permissions inside the running session (its own pickers)
    def walk_menus(self, s, cmd, steps, done, timeout=6.0):
        """Type `cmd`, then drive the pickers it opens. steps: [(title_re, pick)] where pick(menu) returns
        (index, key) — key "\\r" (Enter) or "s" (session only) — or None to fail. done(err)."""
        def label(o):
            return re.sub(r"\s*\((current|default)\)\s*", " ", o.get("label") or "").strip()

        def step(i, deadline):
            if s.fd is None:
                done("会话已退出")
                return
            s.check_menu(now() + MENU_SETTLE)
            mn = s.menu
            if i >= len(steps):
                if mn is None:
                    done(None)
                elif now() > deadline:
                    s.write(b"\x1b")
                    done("Codex 还停在菜单“%s”，已取消" % mn.get("title"))
                else:
                    self.later(0.2, lambda: step(i, deadline))
                return
            title_re, pick = steps[i]
            if not mn or not re.search(title_re, mn.get("title") or "", re.I):
                if now() > deadline:
                    if mn:
                        s.write(b"\x1b")
                    done("Codex 没有弹出预期的菜单（%s）" % (mn.get("title") if mn else "无菜单"))
                else:
                    self.later(0.2, lambda: step(i, deadline))
                return
            choice = pick(mn, label)
            if choice is None:
                s.write(b"\x1b")
                self.later(0.2, lambda: s.write(b"\x1b"))
                done("菜单里没有这个选项（%s）" % ", ".join(label(o) for o in mn["options"]))
                return
            idx, key = choice
            n = idx - mn["cursor"]
            keys = [b"\x1b[B" if n > 0 else b"\x1b[A"] * abs(n) + [key.encode()]
            s.menu, s.menu_dirty = None, True
            for k, data in enumerate(keys):
                self.later(0.04 * k, lambda data=data: s.fd is not None and s.write(data))
            self.later(0.04 * len(keys) + 0.3, lambda: step(i + 1, now() + timeout))

        if s.status not in ("idle", "starting") or s.menu:
            done("会话正忙，等这一轮结束再改")
            return
        self.type_text(s, cmd)
        self.later(0.4, lambda: step(0, now() + timeout))

    def codex_set_model(self, s, model, effort, done):
        """/model -> pick the model (or keep the current one) -> pick the effort, "s" = this session only."""
        want_m = (model or "").lower()
        want_e = CODEX_EFFORT_LABELS.get((effort or "").lower(), (effort or "").lower())

        def pick_model(mn, label):
            for o in mn["options"]:
                if (want_m and label(o).lower() == want_m) or (not want_m and o.get("current")):
                    return o["i"], "\r"
            return None

        def effort_index(mn, label):
            for o in mn["options"]:
                if (want_e and label(o).lower() == want_e) or (not want_e and o.get("current")):
                    return o["i"]
            return None

        def pick_level(mn, label):
            i = effort_index(mn, label)
            if i is not None:
                return i, "s"
            more = next((o["i"] for o in mn["options"] if label(o).lower().startswith("more reasoning")), None)
            return (more, "\r") if more is not None and want_e in ("max", "ultra") else None

        def pick_advanced(mn, label):
            i = effort_index(mn, label)
            return (i, "s") if i is not None else None
        steps = [(r"^Select Model", pick_model), (r"^Select Reasoning Level", pick_level)]
        if want_e in ("max", "ultra"):
            steps.append((r"^Advanced Reasoning", pick_advanced))
        self.walk_menus(s, "/model", steps, done)

    def codex_set_permissions(self, s, action, value, done):
        """/permissions: the picker's current preset says what is set now; pick the preset with the new
        approval / sandbox and the other half unchanged. No such preset -> error (the caller restarts)."""
        chosen = []

        def pick(mn, label):
            cur = next((CODEX_PERMISSION_PRESETS.get(label(o).lower()) for o in mn["options"] if o.get("current")), None)
            ap, sb = cur or (None, None)
            want = (value, sb) if action == "approval" else (ap, value)
            for o in mn["options"]:
                p = CODEX_PERMISSION_PRESETS.get(label(o).lower())
                if p and p[1] == want[1] and (p[0] == want[0] or (p[0] is None and o.get("current") and action == "sandbox")):
                    chosen.append(label(o))
                    return o["i"], "\r"
            # same sandbox, approval unknown (e.g. "Approve for me" -> workspace): the plain preset for that sandbox
            if action == "sandbox":
                for o in mn["options"]:
                    p = CODEX_PERMISSION_PRESETS.get(label(o).lower())
                    if p and p[1] == value and p[0] is not None:
                        chosen.append(label(o))
                        return o["i"], "\r"
            return None

        def confirm(mn, label):
            return next(((o["i"], "\r") for o in mn["options"] if label(o).lower().startswith("yes")), None)

        def finished(err):
            if err is None and chosen:
                ap, sb = CODEX_PERMISSION_PRESETS[chosen[0].lower()]
                s.cx_mode = dict(s.cx_mode, **({"approval": ap} if ap else {}), sandbox=sb)
            done(err)
        steps = [(r"Permissions", pick)]
        if (action, value) in (("sandbox", "danger-full-access"), ("approval", "never")):
            # the phone user chose it explicitly; codex asks once more ("Apply full access for this session")
            steps.append((r"full access", confirm))
        self.walk_menus(s, "/permissions", steps, finished)

    def menu_op(self, s, index=None, key=None, gap=0.04):
        """Operate the picker on screen: move the cursor to index with arrow keys, then Enter; or Esc / "s"."""
        if s.fd is None:
            raise RuntimeError("会话没有在运行")
        s.check_menu(now() + MENU_SETTLE)        # use the latest screen, even mid-burst
        mn = s.menu
        if not mn:
            raise RuntimeError("当前没有菜单")
        if key is not None:
            if key not in MENU_KEYS:
                raise RuntimeError("无效的按键")
            keys = [MENU_KEYS[key]]
        else:
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(mn["options"]):
                raise RuntimeError("无效的选项")
            fwd, back = (b"\x1b[C", b"\x1b[D") if mn["kind"] == "slider" else (b"\x1b[B", b"\x1b[A")
            n = index - mn["cursor"]
            keys = [fwd if n > 0 else back] * abs(n) + [b"\r"]
        s.menu = None                   # re-detected from the screen once the TUI redrew
        s.menu_dirty = True
        s.touch()
        s.user_input()
        for k, data in enumerate(keys):
            if k == 0:
                s.write(data)
            else:
                self.later(gap * k, lambda data=data: s.fd is not None and s.write(data))
        return keys

    def models(self, kind, argv, done):
        """op models: claude from the preset's --settings, codex from `codex debug models` (worker thread, cached)."""
        if kind == "claude":
            cur = [o for o in self.sessions.values() if o.kind == "claude" and (o.usage or {}).get("model")]
            cur.sort(key=lambda o: o.activity)
            argv = argv if isinstance(argv, list) else DEFAULT_PRESETS["claude"]["argv"]
            done(claude_models([str(a) for a in argv], cur[-1].usage["model"] if cur else None), None)
            return
        if kind != "codex":
            raise RuntimeError("无效的类型 %r" % kind)
        argv = [os.path.expanduser(str(a)) if str(a).startswith("~/") else str(a)
                for a in (argv if isinstance(argv, list) and argv else DEFAULT_PRESETS["codex"]["argv"])]
        cmd = tuple(codex_models_cmd(argv))
        hit = self.models_cache.get(cmd)
        if hit and now() - hit[0] < MODELS_TTL:
            done(hit[1], None)
            return
        if cmd in self.models_wait:
            self.models_wait[cmd].append(done)
            return
        self.models_wait[cmd] = [done]

        def finished(res, err):
            if err is None:
                self.models_cache[cmd] = (now(), res)
            for cb in self.models_wait.pop(cmd, []):
                cb(res, err)

        def work():
            try:
                res, err = parse_codex_models(run_login_shell(list(cmd), MODELS_TIMEOUT)), None
            except Exception as e:
                import subprocess
                res = None
                err = "codex debug models 超时" if isinstance(e, subprocess.TimeoutExpired) else \
                    "codex debug models 失败: %s" % clip(str(e), 300)
            self.bg_done.append((finished, res, err))
        self.background(work)

    def netinfo(self, refresh, done):
        """Public IP / location (cached NETINFO_TTL, looked up in a worker thread; callers share one lookup)."""
        hit = getattr(self, "_netinfo", None)
        if hit and not refresh and now() - hit[0] < NETINFO_TTL:
            done(dict(hit[1], ts=hit[0]), None)
            return
        waiting = getattr(self, "_netinfo_wait", None)
        if waiting is not None:
            waiting.append(done)
            return
        self._netinfo_wait = [done]

        def finished(res, err):
            if err is None:
                self._netinfo = (now(), res)
            for cb in self._netinfo_wait or []:
                cb(dict(res, ts=now()) if res else None, err)
            self._netinfo_wait = None

        def work():
            try:
                res, err = fetch_netinfo(), None
            except Exception as e:
                res, err = None, "查询公网 IP 失败: %s" % clip(str(e) or type(e).__name__, 120)
            self.bg_done.append((finished, res, err))
        self.background(work)

    def background(self, fn):
        import threading
        threading.Thread(target=fn, daemon=True).start()

    def fs_op(self, c, req, s, action, path, force):
        """Read-only file browsing of the session's directory (list / read / git / diff). The reply always is
        {"t": "fs", "action", "path", ...} so the phone can show errors in place."""
        head = {"t": "fs", "req": req, "sid": s.sid, "action": action, "path": path}
        fns = {"list": lambda: fs_list(s.cwd, path), "read": lambda: fs_read(s.cwd, path, force),
               "git": lambda: fs_git(s.cwd), "diff": lambda: fs_diff(s.cwd, path, force)}
        fn = fns.get(action)
        if fn is None:
            self.send(c, dict(head, error="unknown action"))
            return

        def run():
            try:
                return fn(), None
            except (OSError, ValueError, RuntimeError) as e:
                msg = e.strerror if isinstance(e, OSError) and e.strerror else str(e)
                return None, clip(msg or type(e).__name__, 200)
            except Exception as e:  # subprocess.TimeoutExpired etc.
                return None, clip(str(e) or type(e).__name__, 200)

        def reply(res, err):
            # the requested path is echoed (not the resolved one: a symlink must still match the phone's request)
            self.send(c, dict(head, error=err) if err else dict(res, **head))
        if action in ("git", "diff"):                  # git can be slow in a big repo: off the event loop
            def work():
                res, err = run()
                self.bg_done.append((reply, res, err))
            self.background(work)
        else:
            reply(*run())

    def cycle_mode(self, s, target, done, tries=6, gap=0.3):
        """Press Shift+Tab until claude's footer shows the target mode (best effort)."""
        def step(left):
            s.check_mode()
            if s.fd is None:
                done("会话已退出")
            elif (s.cmode or "default") == target:
                done(None)
            elif left <= 0:
                done("没能切换到 %s（当前 %s），请在终端里按 Shift+Tab" % (target, s.cmode or "未知"))
            else:
                s.write(SHIFT_TAB)
                self.later(gap, lambda: step(left - 1))
        step(tries)

    def replay(self, s, off, chat_from):
        if off is None or off < s.ring_start or off > s.end:
            reset = True
            start = max(s.ring_start, s.end - DEFAULT_REPLAY) if off is None or off > s.end else s.ring_start
        else:
            reset, start = False, off
        data = bytes(s.ring[start - s.ring_start:])
        if chat_from is None or (s.chat and chat_from < s.chat[0]["seq"] and chat_from <= s.chat_seq - len(s.chat)) \
                or chat_from > s.chat_seq + 1:
            chat, creset = s.chat[-400:], True
        else:
            chat, creset = [c for c in s.chat if c["seq"] >= chat_from], False
        return {"t": "replay", "sid": s.sid, "off": start, "end": s.end, "reset": reset, "d": b64e(data),
                "chat": chat, "chat_reset": creset}

    # --- requests
    def handle(self, c, m):
        if not isinstance(m, dict):
            return
        op = m.get("op")
        req = m.get("req")
        sid = m.get("sid")
        s = self.sessions.get(sid) if isinstance(sid, str) else None
        try:
            if op == "hello":
                c.kind = m.get("client") or "cli"
                self.send(c, {"t": "hello", "version": VERSION, "code": CODE_ID, "host": self.host, "pid": os.getpid(),
                              "sessions": self.session_list()})
            elif op == "list":
                self.send(c, {"t": "list", "req": req, "sessions": self.session_list()})
            elif op == "ping":
                self.send(c, {"t": "pong", "req": req})
            elif op == "history":
                kind = m.get("kind") if m.get("kind") in ("claude", "codex") else None
                owned = [o.agent_session_id for o in self.sessions.values() if o.agent_session_id and o.fd is not None]
                self.send(c, {"t": "history", "req": req, "items": list_history(kind, m.get("limit") or 30, owned)})
            elif op == "memory_config":
                self.memory.configure(m.get("cfg") or {},
                                      lambda c=c, req=req: self.send(c, dict(self.memory.status(), t="memory_status", req=req)))
            elif op == "memory":
                def answered(res, err, c=c, req=req, action=m.get("action")):
                    if err:
                        self.send(c, {"t": "error", "req": req, "msg": err})
                    else:
                        self.send(c, {"t": "memory", "req": req, "action": action, "data": res,
                                      "status": self.memory.status()})
                self.memory.query(m, answered)
            elif op == "usage_today":
                self.send(c, dict(self.usage_today.scan(), t="usage_today", req=req))
            elif op == "create":
                def created(ns, existing, err, c=c, req=req):
                    if err:
                        self.send(c, {"t": "error", "req": req, "sid": ns.sid, "msg": err})
                        return
                    r = {"t": "created", "req": req, "s": ns.info()}
                    if existing:
                        r["existing"] = True
                    self.send(c, r)
                    self.broadcast({"t": "session", "s": ns.info()})
                    ns.dirty = False
                self.create_or_reuse(m, created)
            elif op == "models":
                def listed(res, err, c=c, req=req, kind=m.get("kind")):
                    if err:
                        self.send(c, {"t": "error", "req": req, "msg": err})
                    else:
                        self.send(c, dict(res, t="models", req=req, kind=kind))
                self.models(m.get("kind"), m.get("argv"), listed)
            elif op == "hook":
                if os.environ.get("GLIMPSE_DEBUG"):
                    ev = m.get("ev") or {}
                    sys.stderr.write("hook %s %s %s\n" % (sid, ev.get("hook_event_name"), ev.get("tool_name") or ""))
                    sys.stderr.flush()
                waiting = False
                if s:
                    waiting = s.on_hook(m.get("ev") or {}, c if m.get("wait") else None)
                if not waiting:
                    self.send(c, {"t": "ok"})
            elif op == "stats":
                st, self.net_prev = sys_stats(self.net_prev)
                self.send(c, {"t": "stats", "req": req, "stats": st})
            elif op == "netinfo":
                def got(res, err, c=c, req=req):
                    self.send(c, {"t": "error", "req": req, "msg": err} if err else dict(res, t="netinfo", req=req))
                self.netinfo(bool(m.get("refresh")), got)
            elif op == "speedtest":
                if getattr(self, "_speed_busy", False):
                    raise RuntimeError("测速正在进行")
                self._speed_busy = True

                def tested(res, err, c=c, req=req):
                    self._speed_busy = False
                    self.send(c, {"t": "error", "req": req, "msg": err} if err else dict(res, t="speedtest", req=req))

                def work():
                    try:
                        res, err = speed_test(), None
                    except Exception as e:
                        res, err = None, "测速失败: %s" % clip(str(e) or type(e).__name__, 120)
                    self.bg_done.append((tested, res, err))
                self.background(work)
            elif s is None:
                if op in ("sub", "unsub", "in", "text", "resize", "kill", "forget", "rename", "ack", "replay", "auto_continue",
                          "approve", "restart", "control", "delete", "menu", "fs"):
                    self.send(c, {"t": "error", "req": req, "sid": sid, "msg": "no such session %s" % sid})
                else:
                    self.send(c, {"t": "error", "req": req, "msg": "unknown op %r" % op})
            elif op == "sub":
                s.subs.add(c)
                c.subs.add(sid)
            elif op == "unsub":
                s.subs.discard(c)
                c.subs.discard(sid)
            elif op == "replay":
                r = self.replay(s, m.get("off"), m.get("chat_from"))
                r["req"] = req
                self.send(c, r)
            elif op == "in":
                if m.get("cols") and m.get("rows"):
                    s.resize(m["cols"], m["rows"])
                data = base64.b64decode(m.get("d") or "")
                if c.kind != "attach":
                    data = RESPONSE_RE.sub(b"", data)
                if data:
                    s.write(data)
                    if is_user_keys(data):
                        s.user_input()
            elif op == "text":
                self.type_text(s, m.get("text") or "")
                s.user_input()
            elif op == "resize":
                s.resize(m.get("cols") or s.cols, m.get("rows") or s.rows)
            elif op == "kill":
                self.kill(s)
            elif op == "forget":
                if not self.forget(sid):
                    self.send(c, {"t": "error", "req": req, "msg": "session still running"})
            elif op == "delete":
                def deleted(ok, c=c, req=req, sid=sid):
                    self.send(c, {"t": "ok", "req": req, "sid": sid} if ok else
                              {"t": "error", "req": req, "sid": sid, "msg": "删除失败"})
                self.delete(s, deleted)
            elif op == "control":
                def controlled(err, c=c, req=req, sid=sid):
                    self.send(c, {"t": "error", "req": req, "sid": sid, "msg": err} if err else
                              {"t": "ok", "req": req, "sid": sid})
                self.control(s, m.get("action"), m.get("value"), controlled)
            elif op == "menu":
                self.menu_op(s, m.get("index"), m.get("key"))
                self.send(c, {"t": "ok", "req": req, "sid": sid})
            elif op == "fs":
                self.fs_op(c, req, s, m.get("action"), m.get("path") or "", bool(m.get("force")))
            elif op == "rename":
                s.name = clip(m.get("name") or s.name, 80)
                s.touch()
            elif op == "auto_continue":
                s.auto_continue = bool(m.get("on"))
                if not s.auto_continue:
                    s.ac_count = 0
                s.touch()
                self.send(c, {"t": "ok", "req": req, "sid": sid})
            elif op == "ack":
                s.attention = None
                s.touch()
            elif op == "approve":
                dec = m.get("decision")
                pid = m.get("id") if isinstance(m.get("id"), str) else (s.pending or {}).get("id")
                if not pid or pid not in s.pendings:
                    self.send(c, {"t": "error", "req": req, "sid": sid, "msg": "这个授权请求已经处理过了"})
                elif dec == "terminal":
                    s.resolve_pending(None, pid=pid)
                    self.send(c, {"t": "ok", "req": req})
                elif dec in ("allow", "always", "deny"):
                    s.resolve_pending(dec, m.get("message"), pid=pid)
                    self.send(c, {"t": "ok", "req": req})
                else:
                    self.send(c, {"t": "error", "req": req, "msg": "bad decision"})
            elif op == "restart":
                if s.kind not in ("claude", "codex"):
                    raise RuntimeError("只有 Claude / Codex 会话可以更换 Key 重启")
                env = m.get("env")
                if env is not None and not isinstance(env, dict):
                    raise RuntimeError("bad env")

                n0 = s.restarts

                def done(err, c=c, req=req, sid=sid, n0=n0):
                    # hot: the key was swapped in the running process (no restart happened)
                    self.send(c, {"t": "error", "req": req, "sid": sid, "msg": err} if err else
                              {"t": "restarted", "req": req, "sid": sid, "hot": s.restarts == n0})
                s.restart(env, m.get("key"), bool(m.get("resume", True)), done, cold=bool(m.get("cold")))
            else:
                self.send(c, {"t": "error", "req": req, "msg": "unknown op %r" % op})
        except Exception as e:
            self.send(c, {"t": "error", "req": req, "msg": str(e)})

    # --- loop
    def reap(self):
        while True:
            try:
                pid, st = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                return
            code = os.waitstatus_to_exitcode(st) if hasattr(os, "waitstatus_to_exitcode") else (st >> 8)
            for s in list(self.sessions.values()):     # on_exit may forget a deleted session
                if s.pid == pid and s.fd is not None:
                    self._close_pty(s)
                    s.on_exit(code)
                    s.pid_exit = True

    def _close_pty(self, s):
        if s.fd is None:
            return
        # drain anything left
        try:
            while True:
                d = os.read(s.fd, 65536)
                if not d:
                    break
                s.on_output(d)
        except OSError:
            pass
        try:
            self.sel.unregister(s.fd)
        except (KeyError, ValueError):
            pass
        try:
            os.close(s.fd)
        except OSError:
            pass
        s.fd = None
        self.pty_wbuf.pop(s.sid, None)

    def tick(self):
        t = now()
        if t - getattr(self, "_saved_at", 0) > 2:
            self._saved_at = t
            self.save_state()
        while self.bg_done:
            cb, res, err = self.bg_done.pop(0)
            try:
                cb(res, err)
            except Exception as e:
                sys.stderr.write("glimpse background error: %s\n" % e)
        self.run_timers(t)
        for s in list(self.sessions.values()):
            try:
                s.poll_tail()
                s.check_mode()
                s.check_menu(t)
            except Exception as e:
                sys.stderr.write("glimpse tail error: %s\n" % e)
            if s.dirty and t - s.last_bcast >= 0.25:
                info = s.info()
                key = json.dumps({k: v for k, v in info.items() if k not in ("activity", "end", "chat_seq")},
                                 sort_keys=True)
                # pure activity updates are rate limited harder than real state changes
                if key == s.last_key and t - s.last_bcast < 2:
                    continue
                s.dirty = False
                s.last_bcast = t
                s.last_key = key
                self.broadcast({"t": "session", "s": info})
                for c in list(s.subs):
                    if c.kind == "attach":      # hub clients got it from broadcast already
                        self.send(c, {"t": "session", "s": info})

    def run_timers(self, t=None):
        t = now() if t is None else t
        due = [x for x in self.timers if x[0] <= t]
        if not due:
            return
        self.timers = [x for x in self.timers if x[0] > t]
        for _, fn in sorted(due, key=lambda x: x[0]):
            try:
                fn()
            except Exception as e:
                sys.stderr.write("glimpse timer error: %s\n" % e)

    def run(self, idle_exit=None):
        self.listen()
        self.restore_state()
        self.run_listening(idle_exit)

    def run_listening(self, idle_exit=None):
        signal.signal(signal.SIGCHLD, lambda *a: None)
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
        last_tick = 0
        while True:
            # timers (key sequences with small gaps) fire close to their time, not once per tick
            wait = min([0.25] + [max(0.0, x[0] - now()) for x in self.timers])
            try:
                events = self.sel.select(timeout=wait)
            except InterruptedError:
                events = []
            for key, mask in events:
                kind, obj = key.data
                if kind == "listen":
                    try:
                        cs, _ = self.lsock.accept()
                    except OSError:
                        continue
                    if not peer_ok(cs):
                        cs.close()
                        continue
                    cs.setblocking(False)
                    set_cloexec(cs.fileno())
                    c = Client(cs)
                    self.clients.add(c)
                    self.sel.register(cs, selectors.EVENT_READ, ("client", c))
                elif kind == "client":
                    c = obj
                    if mask & selectors.EVENT_WRITE:
                        self._flush(c)
                    if mask & selectors.EVENT_READ and c in self.clients:
                        try:
                            d = c.sock.recv(262144)
                        except (BlockingIOError, InterruptedError):
                            continue
                        except OSError:
                            d = b""
                        if not d:
                            self.drop(c)
                            continue
                        c.rbuf += d
                        while b"\n" in c.rbuf and c in self.clients:
                            line, c.rbuf = c.rbuf.split(b"\n", 1)
                            if line.strip():
                                try:
                                    m = json.loads(line)
                                except ValueError:
                                    continue
                                self.handle(c, m)
                elif kind == "pty":
                    s = obj
                    if s.fd is None:
                        continue
                    if mask & selectors.EVENT_WRITE:
                        self._pty_flush(s)
                    if mask & selectors.EVENT_READ:
                        try:
                            d = os.read(s.fd, 65536)
                        except (BlockingIOError, InterruptedError):
                            continue
                        except OSError:
                            d = b""
                        if d:
                            s.on_output(d)
                        else:
                            # EOF: child closed the pty; reap() will deliver the exit code
                            try:
                                self.sel.unregister(s.fd)
                            except (KeyError, ValueError):
                                pass
            self.reap()
            self.run_timers()
            if now() - last_tick >= 0.2:
                last_tick = now()
                self.tick()
            if idle_exit:
                busy = self.clients or any(s.fd is not None for s in self.sessions.values())
                if busy:
                    self.idle_since = now()
                elif now() - self.idle_since > idle_exit:
                    return


# --------------------------------------------------------------------------- client side

# exec()'d from an ssh one-liner: no script file exists, keep no files on that machine
BOOTSTRAP = "__file__" not in globals()


def daemon_addr():
    if BOOTSTRAP and sys.platform.startswith("linux"):
        return "@glimpse-%d" % os.getuid(), None
    sdir = state_dir()
    return sock_path(), sdir


def connect(addr, timeout=None):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    if timeout:
        s.settimeout(timeout)
    s.connect("\0" + addr[1:] if addr.startswith("@") else addr)
    if addr.startswith("@") and not peer_ok(s):
        # anyone can bind an abstract name first: never talk to another user's "daemon"
        s.close()
        raise OSError("glimpse: %s is owned by another user" % addr)
    s.settimeout(None)
    return s


def start_daemon(addr, sdir):
    """Fork a detached daemon (works without a script file, i.e. when bootstrapped over ssh)."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            os.setsid()
            if os.fork() != 0:
                os._exit(0)
            devnull = os.open(os.devnull, os.O_RDWR)
            logf = devnull
            if sdir:
                logf = os.open(os.path.join(sdir, "agent.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.dup2(devnull, 0)
            os.dup2(logf, 1)
            os.dup2(logf, 2)
            os.closerange(3, w)
            os.closerange(w + 1, 65536)
            d = Daemon(addr, sdir)
            d.listen()
            os.write(w, b"1")
            os.close(w)
            d.restore_state()
            d.run_listening()
        except BaseException as e:  # pragma: no cover
            try:
                sys.stderr.write("glimpse daemon died: %r\n" % (e,))
            except Exception:
                pass
        os._exit(0)
    os.close(w)
    os.waitpid(pid, 0)
    ok = os.read(r, 1)
    os.close(r)
    return ok == b"1"


def ensure_daemon():
    addr, sdir = daemon_addr()
    for attempt in range(3):
        try:
            return connect(addr, timeout=3)
        except OSError:
            pass
        if attempt == 0:
            start_daemon(addr, sdir)
        time.sleep(0.2)
    raise SystemExit("glimpse: cannot reach or start the agent daemon at %s" % addr)


class Conn:
    """Small blocking JSON-lines client."""

    def __init__(self, sock):
        self.s = sock
        self.buf = b""

    def send(self, obj):
        self.s.sendall((json.dumps(obj) + "\n").encode())

    def recv(self, timeout=None):
        while b"\n" not in self.buf:
            if timeout is not None:
                r, _, _ = select.select([self.s], [], [], timeout)
                if not r:
                    return None
            d = self.s.recv(262144)
            if not d:
                raise EOFError
            self.buf += d
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def call(self, obj, want):
        self.send(obj)
        while True:
            m = self.recv()
            if m.get("t") in want or (m.get("t") == "error" and m.get("req") == obj.get("req")):
                return m


def daemon_hello(addr, timeout=5):
    """The running daemon's hello reply ({version, code, pid, sessions}), or None when none is reachable."""
    try:
        c = Conn(connect(addr, timeout=3))
    except OSError:
        return None
    try:
        c.send({"op": "hello", "client": "probe"})
        end = time.time() + timeout
        while time.time() < end:
            m = c.recv(timeout=max(0.05, end - time.time()))
            if m is None:
                break
            if m.get("t") == "hello":
                return m
    except (OSError, EOFError, ValueError):
        pass
    finally:
        try:
            c.s.close()
        except OSError:
            pass
    return None


def upgrade_daemon(force=False):
    """Replace a daemon that runs other (older) code than this process. Without `force` only when no session
    has a live process; the new daemon restores the saved sessions (same ids, resumed conversations).
    Returns "replaced", "outdated" (left running), or None (current, or no daemon)."""
    addr, _ = daemon_addr()
    h = daemon_hello(addr)
    if not h or not CODE_ID or h.get("code") == CODE_ID:
        return None
    if pick_live(h.get("sessions") or []) and not force:
        return "outdated"
    pid = h.get("pid")
    if not isinstance(pid, int) or pid <= 1 or pid == os.getpid():
        return "outdated"
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return "outdated"
    end = time.time() + 10
    while time.time() < end:
        try:
            connect(addr, timeout=1).close()
        except OSError:
            return "replaced"         # gone: the caller's ensure_daemon() starts the new one
        time.sleep(0.1)
    return "outdated"


def cmd_bridge(argv=()):
    """stdio <-> daemon socket byte pump. Exits when either side closes; the daemon keeps running.
    --code ID: the hash of the source this process runs (given by the hub when exec'd over ssh);
    --replace: replace an outdated daemon even while sessions run (they are restarted and resumed)."""
    global CODE_ID
    argv = list(argv)
    if "--code" in argv[:-1]:
        CODE_ID = argv[argv.index("--code") + 1]
    res = upgrade_daemon(force="--replace" in argv)
    if res == "replaced":
        sys.stderr.write("glimpse: replaced the outdated agent daemon\n")
    s = ensure_daemon()
    s.setblocking(False)
    out = sys.stdout.fileno()
    inp = sys.stdin.fileno()
    set_nonblock(inp)
    pending = bytearray()
    while True:
        r, _, _ = select.select([s, inp], [], [])
        if inp in r:
            try:
                d = os.read(inp, 262144)
            except (BlockingIOError, InterruptedError):
                d = None
            if d == b"":
                return
            if d:
                pending += d
        if s in r:
            try:
                d = s.recv(262144)
            except (BlockingIOError, InterruptedError):
                d = None
            if d == b"":
                return
            if d:
                mv = memoryview(d)
                while mv:
                    try:
                        n = os.write(out, mv)
                        mv = mv[n:]
                    except BlockingIOError:
                        select.select([], [out], [])
        while pending:
            try:
                n = s.send(pending)
                del pending[:n]
            except BlockingIOError:
                select.select([], [s], [])


def cmd_hook():
    data = sys.stdin.read()
    sid = os.environ.get("GLIMPSE_SID")
    if not sid:
        return
    try:
        ev = json.loads(data or "{}")
        c = Conn(connect(os.environ.get("GLIMPSE_SOCK") or daemon_addr()[0], timeout=3))
        c.send({"op": "hook", "sid": sid, "ev": ev})
        c.recv(timeout=3)
    except Exception:
        pass


def fmt_age(t):
    d = int(now() - t)
    return "%ds" % d if d < 60 else "%dm" % (d // 60) if d < 3600 else "%dh" % (d // 3600)


def cmd_ls():
    c = Conn(ensure_daemon())
    m = c.call({"op": "hello", "client": "cli"}, ("hello",))
    rows = m["sessions"]
    if not rows:
        print("no sessions")
    for s in rows:
        print("%-7s %-7s %-8s %-6s %-30s %s" % (s["sid"], s["kind"], s["status"], fmt_age(s["activity"]),
                                                 clip(s["name"], 30), s["cwd"]))


def term_size():
    try:
        rows, cols = struct.unpack("HH", fcntl.ioctl(1, termios.TIOCGWINSZ, b"\0" * 4))
        if rows and cols:
            return cols, rows
    except OSError:
        pass
    return 100, 32


def pick_live(sessions):
    """Most recently active session that still has a process, or None."""
    live = [s for s in sessions if s.get("pid") and s.get("exit_code") is None and s.get("status") != "exited"]
    return max(live, key=lambda s: s.get("activity") or 0) if live else None


def attach_reconnect(sid, wait=30):
    """After the daemon went away (e.g. replaced by an update): a connection to the new one once it has
    restored `sid`, or None."""
    end = time.time() + wait
    while time.time() < end:
        time.sleep(0.5)
        try:
            c = Conn(connect(daemon_addr()[0], timeout=2))
            h = c.call({"op": "hello", "client": "attach"}, ("hello",))
        except (OSError, EOFError, ValueError):
            continue
        if any(s["sid"] == sid for s in h.get("sessions") or []):
            return c
        c.s.close()
    return None


def attach_draw(c, sid):
    """Subscribe to `sid`, clear the screen and replay its output. Returns the byte offset reached."""
    cols, rows = term_size()
    c.send({"op": "resize", "sid": sid, "cols": cols, "rows": rows})
    c.send({"op": "sub", "sid": sid})
    r = c.call({"op": "replay", "req": 1, "sid": sid, "off": None}, ("replay",))
    if r.get("t") == "error":
        raise SystemExit(r.get("msg"))
    out = sys.stdout.buffer
    out.write(b"\x1b[H\x1b[2J" + base64.b64decode(r["d"]))
    out.flush()
    return r["end"]


def attach(sid=None):
    import tty
    c = Conn(ensure_daemon())
    hello = c.call({"op": "hello", "client": "attach"}, ("hello",))
    if not sid:
        s = pick_live(hello.get("sessions") or [])
        if s is None:
            raise SystemExit("glimpse: no running session (start one: gli run claude)")
        sid = s["sid"]
        print("[glimpse] attaching %s  %s" % (sid, s.get("name") or ""))
    elif sid not in [s["sid"] for s in hello.get("sessions") or []]:
        # exit code 3 = not on this machine (the launcher may then try through the hub)
        sys.stderr.write("glimpse: no such session %s here (gli ls)\n" % sid)
        raise SystemExit(3)
    pos = attach_draw(c, sid)
    out = sys.stdout.buffer
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    winch = [False]
    signal.signal(signal.SIGWINCH, lambda *a: winch.__setitem__(0, True))
    tty.setraw(fd)
    reason = "detached"
    try:
        while True:
            if reason == "daemon closed":
                # the daemon was restarted (an agent update): wait for the new one to restore this session
                out.write(b"\r\n[glimpse] agent restarting, reconnecting...\r\n")
                out.flush()
                c = attach_reconnect(sid)
                if c is None:
                    break
                pos = attach_draw(c, sid)
                reason = "detached"
            if winch[0]:
                winch[0] = False
                cols, rows = term_size()
                try:
                    c.send({"op": "resize", "sid": sid, "cols": cols, "rows": rows})
                except OSError:
                    reason = "daemon closed"
                    continue
            try:
                rr, _, _ = select.select([c.s, fd], [], [], 0.5)
            except InterruptedError:
                continue
            if fd in rr:
                d = os.read(fd, 65536)
                if not d or DETACH_KEY in d:
                    break
                cols, rows = term_size()
                try:
                    c.send({"op": "in", "sid": sid, "d": b64e(d), "cols": cols, "rows": rows})
                except OSError:
                    reason = "daemon closed"
                    continue
            if c.s in rr or b"\n" in c.buf:
                try:
                    m = c.recv(timeout=0)
                except (EOFError, OSError):
                    reason = "daemon closed"
                    continue                # reconnect at the top of the loop
                while m is not None:
                    if m.get("t") == "out" and m.get("sid") == sid:
                        data = base64.b64decode(m["d"])
                        skip = pos - m["off"]
                        if skip < len(data):
                            out.write(data[max(0, skip):])
                            out.flush()
                            pos = m["off"] + len(data)
                    elif m.get("t") == "session" and m["s"]["sid"] == sid and m["s"]["status"] in ("exited", "error") \
                            and m["s"].get("exit_code") is not None:
                        # ("error" without an exit code = a failed turn, the process still runs)
                        reason = "session exited (code %s)" % m["s"]["exit_code"]
                    m = c.recv(timeout=0) if b"\n" in c.buf else None
                if reason.startswith("session exited"):
                    break
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    sys.stdout.write("\r\n[glimpse] %s: %s\r\n" % (reason, sid))


def local_memory_config():
    """config.json memory settings for this machine (hub-wide `memory` + the local machine's override)."""
    try:
        with open(os.environ.get("GLIMPSE_CONFIG") or os.path.join(project_dir(), "config.json")) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict) or not isinstance(cfg.get("memory"), dict):
        return None
    out = dict(cfg["memory"])
    for mc in cfg.get("machines") or []:
        if isinstance(mc, dict) and mc.get("type") == "local" and isinstance(mc.get("memory"), dict):
            out.update(mc["memory"])
    return out


KEY_ENV_DEFAULT = {"claude": "ANTHROPIC_AUTH_TOKEN", "codex": "OPENAI_API_KEY"}


def local_launch(kind, key_name=None):
    """Launch settings of this machine from config.json, like the hub uses them:
    (argv, key name or None, env {var: value} or None). key_name overrides the preset key, "none" = no key.
    Raises RuntimeError for an unknown key name. The key value is never printed."""
    base = list(DEFAULT_PRESETS[kind]["argv"])
    cfg, pkey = {}, None
    try:
        with open(os.environ.get("GLIMPSE_CONFIG") or os.path.join(project_dir(), "config.json")) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    for mc in cfg.get("machines") or []:
        if isinstance(mc, dict) and mc.get("type") == "local":
            pre = (mc.get("presets") or {}).get(kind)
            if isinstance(pre, dict):
                pkey = pre.get("key")
                pre = pre.get("argv")
            if isinstance(pre, list):
                base = [str(x) for x in pre]
            break
    name = pkey if key_name is None else (None if key_name == "none" else key_name)
    if not name or kind not in ("claude", "codex"):
        if key_name not in (None, "none") and kind not in ("claude", "codex"):
            raise RuntimeError("--key needs claude or codex")
        return base, None, None
    keys = cfg.get("keys") if isinstance(cfg.get("keys"), dict) else {}
    prof = [p for p in keys.get("profiles") or [] if isinstance(p, dict) and p.get("name") == name]
    if not prof:
        known = [p.get("name") for p in keys.get("profiles") or [] if isinstance(p, dict) and p.get("name")]
        raise RuntimeError("no key named %r in config.json (known: %s)" % (name, ", ".join(known) or "none"))
    var = (keys.get("env") if isinstance(keys.get("env"), dict) else KEY_ENV_DEFAULT).get(kind)
    value = prof[0].get("value")
    if not var or not value:
        return base, None, None
    return base, name, {str(var): str(value)}


def cmd_run(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="gli run", description="start an AI coding session that the phone can see")
    ap.add_argument("kind", choices=sorted(DEFAULT_PRESETS))
    ap.add_argument("-n", "--name")
    ap.add_argument("-C", "--cwd", default=os.getcwd())
    ap.add_argument("-d", "--detach", action="store_true", help="do not attach this terminal")
    ap.add_argument("-r", "--resume", metavar="ID", help="continue this claude session id / codex thread id")
    ap.add_argument("-k", "--key", metavar="NAME", help="API key profile from config.json (none = no key)")
    extra = []
    if "--" in argv:                       # everything after -- goes to claude / codex untouched
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    a = ap.parse_args(argv)
    if a.resume and a.kind == "shell":
        ap.error("--resume needs claude or codex")
    args = extra
    # same launch preset as the phone uses: config.json -> machines[type=local].presets[kind] argv + key
    try:
        base, key, env = local_launch(a.kind, a.key)
    except RuntimeError as e:
        ap.error(str(e))
    if a.kind != "shell":
        print("[glimpse] key: %s" % (key or "none (preset / environment default)"))
    full = (base + args) if (base or args) else None
    start_session(a.kind, a.cwd, a.name, full, a.resume, env, key, a.detach)


def start_session(kind, cwd, name, full, resume, env, key, detach=False):
    """Create (or reuse, for a conversation that already has a session) and attach this terminal."""
    c = Conn(ensure_daemon())
    c.call({"op": "hello", "client": "cli"}, ("hello",))
    cols, rows = term_size()
    m = c.call({"op": "create", "req": 1, "kind": kind, "cwd": cwd, "name": name, "args": full or None,
                "cols": cols, "rows": rows, "resume_id": resume or None, "env": env, "key": key}, ("created",))
    if m.get("t") == "error":
        raise SystemExit("glimpse: " + m.get("msg", "error"))
    sid = m["s"]["sid"]
    if m.get("existing"):
        print("[glimpse] this conversation is already session %s, attaching (detach: Ctrl-])" % sid)
    else:
        print("[glimpse] session %s started (detach: Ctrl-], re-attach: gli attach %s)" % (sid, sid))
    if not detach:
        time.sleep(0.1)
        attach(sid)


# --------------------------------------------------------------------------- shell integration

# claude sub-commands / flags that are not an interactive session: run the real binary untouched
CLAUDE_PASSTHRU_CMDS = {"mcp", "plugin", "plugins", "config", "doctor", "update", "upgrade", "install", "setup-token",
                        "migrate-installer", "agents", "auth", "login", "logout"}
CLAUDE_PASSTHRU_FLAGS = {"-p", "--print", "-h", "--help", "-v", "--version"}
CODEX_PASSTHRU_FLAGS = {"-h", "--help", "-V", "--version"}


def split_wrap_args(kind, args):
    """Raw `claude ...` / `codex ...` arguments -> (wrap?, resume id or None, remaining args)."""
    args = list(args)
    if kind == "codex":
        if any(a in CODEX_PASSTHRU_FLAGS for a in args):
            return False, None, args
        pos = [i for i, a in enumerate(args) if not a.startswith("-")]
        # the interactive TUI: no sub-command (a prompt is fine) or `codex resume <id>`
        if pos and args[pos[0]] == "resume":
            i = pos[0]
            if i + 1 < len(args) and not args[i + 1].startswith("-") and ID_RE.match(args[i + 1]):
                return True, args[i + 1], args[:i] + args[i + 2:]
            return True, None, args                # `codex resume` / `--last`: codex's own picker, in a session
        if pos and re.match(r"^[a-z][a-z-]*$", args[pos[0]]) and args[pos[0]] in CODEX_SUBCOMMANDS:
            return False, None, args
        return True, None, args
    if any(a in CLAUDE_PASSTHRU_FLAGS for a in args):
        return False, None, args
    if args and not args[0].startswith("-") and args[0] in CLAUDE_PASSTHRU_CMDS:
        return False, None, args
    out, rid, i = [], None, 0
    while i < len(args):
        a = args[i]
        if a.startswith("--resume="):
            rid = a.split("=", 1)[1] or None
        elif a in ("-r", "--resume") and i + 1 < len(args) and not args[i + 1].startswith("-") \
                and ID_RE.match(args[i + 1]):
            rid = args[i + 1]
            i += 1
        else:
            out.append(a)
        i += 1
    return True, rid, out


CODEX_SUBCOMMANDS = {"exec", "e", "review", "login", "logout", "mcp", "plugin", "app-server", "remote-control", "app",
                     "completion", "update", "doctor", "sandbox", "debug", "apply", "a", "queue", "archive", "delete",
                     "migrate-rollouts", "unarchive", "fork", "cloud", "exec-server", "features", "help", "agents",
                     "mcp-server"}


def merge_wrap_argv(base, extra):
    """Preset argv + the user's own arguments. A flag the preset already has is not repeated (clap rejects
    `--no-daemon` twice) and a user `--settings` replaces the preset's."""
    base = list(base)
    if "--settings" in extra or any(a.startswith("--settings=") for a in extra):
        if "--settings" in base:
            i = base.index("--settings")
            del base[i:i + 2]
        base = [a for a in base if not a.startswith("--settings=")]
    bare = {a for i, a in enumerate(base) if a.startswith("--") and "=" not in a and
            (i + 1 == len(base) or base[i + 1].startswith("-"))}
    return base + [a for a in extra if a not in bare]


def cmd_wrap(argv):
    """`gli wrap claude|codex ARGS...`: what the shell functions from `gli shell-init` call.
    The usual command line runs as a Glimpse session (visible on the phone) with this terminal attached;
    anything that is not an interactive session runs the real program unchanged."""
    if not argv or argv[0] not in ("claude", "codex"):
        raise SystemExit("usage: gli wrap claude|codex [args...]")
    kind, args = argv[0], argv[1:]
    wrap, rid, extra = split_wrap_args(kind, args)
    if not wrap or os.environ.get("GLIMPSE_SID") or os.environ.get("GLIMPSE") == "0" \
            or not (sys.stdin.isatty() and sys.stdout.isatty()):
        os.execvp(kind, [kind] + args)
    try:
        base, key, env = local_launch(kind, os.environ.get("GLIMPSE_KEY") or None)
    except RuntimeError as e:
        raise SystemExit("glimpse: %s" % e)
    full = merge_wrap_argv(base, extra)
    print("[glimpse] %s via Glimpse%s, key: %s   (plain run: GLIMPSE=0 %s ...)" % (
        kind, " (resume %s)" % rid if rid else "", key or "default", kind))
    start_session(kind, os.getcwd(), None, full, rid, env, key)


SHELL_INIT = r'''# Glimpse shell integration: `claude` and `codex` typed at the prompt run as Glimpse sessions
# (visible on the phone). GLIMPSE=0 claude ... / command claude ... run the real program directly.
_glimpse_wrap() {
  if [ -n "${GLIMPSE_SID:-}" ] || [ "${GLIMPSE:-1}" = 0 ] || [ ! -t 0 ] || [ ! -t 1 ]; then
    command "$@"
  else
    %(launcher)s wrap "$@"
  fi
}
claude() { _glimpse_wrap claude "$@"; }
codex() { _glimpse_wrap codex "$@"; }
'''


def cmd_shell_init():
    launcher = os.path.join(project_dir(), "gli")
    print(SHELL_INIT % {"launcher": shlex.quote(launcher)}, end="")


def cmd_history(argv, adopt=False):
    """List recent claude / codex conversations; adopt: pick one and continue it in a session."""
    kind = argv[0] if argv and argv[0] in ("claude", "codex") else None
    c = Conn(ensure_daemon())
    c.call({"op": "hello", "client": "cli"}, ("hello",))
    m = c.call({"op": "history", "req": 1, "kind": kind, "limit": 30}, ("history",))
    items = m.get("items") or []
    if not items:
        print("no recent conversations")
        return
    home = os.path.expanduser("~")
    for i, it in enumerate(items, 1):
        cwd = it["cwd"].replace(home, "~", 1) if it["cwd"].startswith(home) else it["cwd"]
        print("%3d  %-6s %-5s %s%-40s %s" % (i, it["kind"], fmt_age(it["updated"]), "* " if it["active"] else "  ",
                                             clip(it["title"], 40), cwd))
        if not adopt:
            print("     gli run %s --resume %s" % (it["kind"], it["id"]))
    if any(it["active"] for it in items):
        print("* = written in the last 2 minutes, probably still open in another terminal (close it first)")
    if not adopt:
        return
    try:
        ans = input("continue which one (number, empty = cancel)? ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not ans.isdigit() or not 1 <= int(ans) <= len(items):
        return
    it = items[int(ans) - 1]
    c.s.close()
    cwd = it["cwd"] if it["cwd"] and os.path.isdir(it["cwd"]) else os.getcwd()
    cmd_run([it["kind"], "--resume", it["id"], "-C", cwd, "-n", clip(it["title"], 40)])


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "help"
    if cmd == "daemon":
        addr, sdir = daemon_addr()
        try:
            connect(addr, timeout=1).close()
            print("daemon already running at %s" % addr)
            return
        except OSError:
            pass
        Daemon(addr, sdir).run()
    elif cmd == "bridge":
        cmd_bridge(argv[1:])
    elif cmd == "hook":
        cmd_hook()
    elif cmd == "ls":
        cmd_ls()
    elif cmd == "run":
        cmd_run(argv[1:])
    elif cmd == "wrap":
        cmd_wrap(argv[1:])
    elif cmd == "shell-init":
        cmd_shell_init()
    elif cmd == "attach":
        attach(argv[1] if len(argv) > 1 else None)
    elif cmd in ("history", "adopt"):
        cmd_history(argv[1:], adopt=cmd == "adopt")
    elif cmd in ("kill", "forget") and len(argv) > 1:
        c = Conn(ensure_daemon())
        c.call({"op": "hello", "client": "cli"}, ("hello",))
        c.send({"op": cmd, "sid": argv[1]})
        m = c.recv(timeout=0.5)
        if m and m.get("t") == "error":
            raise SystemExit("glimpse: " + m["msg"])
    elif cmd == "version":
        print(VERSION)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
