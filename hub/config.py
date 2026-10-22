"""Configuration: one local, git-ignored file (config.json) holds every setting, including secrets.

config.example.json in the repo documents all fields. Runtime state the hub generates itself
(login token, VAPID key, push subscriptions, TLS cert) lives in data/ (also git-ignored).
"""
import json
import os
import secrets

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("GLIMPSE_DATA") or os.path.join(ROOT, "data")
CONFIG = os.environ.get("GLIMPSE_CONFIG") or os.path.join(ROOT, "config.json")
EXAMPLE = os.path.join(ROOT, "config.example.json")

DEFAULTS = {
    # host "auto" = this computer's Tailscale IP, tailscale_hostname "auto" = its MagicDNS name
    "hub": {"listen": [{"host": "auto", "port": 8443}, {"host": "127.0.0.1", "port": 8443}],
            "tailscale_hostname": "auto", "caffeinate": True,
            "push_subject": "mailto:glimpse@users.noreply.github.com", "approval_wait_seconds": 600,
            # what the hub and the machines write (notifications, system lines in chat): "zh" | "en"; set from
            # Settings → 外观 → 语言 when a language is picked by hand
            "language": "zh"},
    # usage_url: optional key-usage API (POST <url>/limit and /summary with the key as Bearer),
    # e.g. "https://api.funcloud.ai/v1/model/key-usage"; empty = no usage shown
    # balance (optional, off by default): "off" | "on_exhausted" (a session whose key runs out of balance / is
    # rejected switches to another key and continues) | "least_used" (also: new sessions without a chosen key
    # get the least used one, busy keys are moved off between turns). exclude: key names never picked.
    "keys": {"env": {"claude": "ANTHROPIC_AUTH_TOKEN", "codex": "OPENAI_API_KEY"}, "profiles": [], "usage_url": "",
             "balance": {"mode": "off", "exclude": []}},
    "machines": [{"id": "M1", "name": "this-mac", "type": "local", "default_cwd": "~"}],
    # optional persistent memory through AMem (https://github.com/lyfeng001/AMem), off by default.
    # python: an interpreter with agent-memory-local[mcp] installed (per machine: machines[].memory.python);
    # store: the vault ("" = data/memory in the project on this computer, ~/.glimpse/data/memory on ssh machines);
    # budget: vault size limit, oldest tool output / tool calls are compacted away beyond it
    "memory": {"enabled": False, "python": "", "store": "", "budget": "20G", "keep_days": 30},
    # extensions (optional tools in their own repositories, each a folder with an extension.json): per entry
    # {"id", "dir": the folder on that machine, "machine": "" = this computer, "enabled", "settings": {...}}.
    # Settings → 扩展 adds / configures them. vault-linker (the project workflow + 知识库) is one.
    "extensions": [],
    # choices offered by the new-session sheet and the "controls" widget (lists replace, not merge)
    "options": {
        "claude": {"models": ["default", "opus", "sonnet", "haiku", "fable", "opusplan"],
                   "efforts": ["low", "medium", "high", "xhigh", "max", "ultracode"],
                   "modes": ["manual", "acceptEdits", "auto", "plan", "dontAsk", "bypassPermissions"]},
        "codex": {"models": [], "efforts": ["low", "medium", "high", "xhigh", "max", "ultra"],
                  "approvals": ["on-request", "never"],     # codex 0.16x: "untrusted" was removed
                  "sandboxes": ["read-only", "workspace-write", "danger-full-access"]},
    },
}
# option lists older versions wrote into config.json (save() persists the merged defaults): not user choices
LEGACY_OPTIONS = {
    "claude": {"models": [["opus", "sonnet", "haiku"]], "efforts": [["low", "medium", "high", "xhigh", "max"]],
               "modes": [["default", "acceptEdits", "auto", "plan", "bypassPermissions"]]},
    "codex": {"efforts": [["minimal", "low", "medium", "high", "max"]],
              "approvals": [["untrusted", "on-request", "never"]]},
}


def write_secret(path, text):
    """Atomic 0600 write: a crash mid-write never leaves a truncated config / token behind."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _merge(base, over):
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def _migrate(cfg):
    """Older config.json: "vault": {linker, path, machine, owner} becomes the vault-linker extension entry."""
    v = cfg.pop("vault", None)
    if not isinstance(v, dict):
        return v is not None
    exts = cfg.setdefault("extensions", [])
    if v.get("linker") and v.get("path") and not any(isinstance(e, dict) and e.get("id") == "vault-linker" for e in exts):
        linker = os.path.expanduser(v["linker"])
        exts.append({"id": "vault-linker", "dir": os.path.dirname(os.path.realpath(linker)) if os.path.exists(linker)
                     else os.path.dirname(linker), "machine": v.get("machine") or "", "enabled": True,
                     "settings": {"path": v["path"], "owner": v.get("owner") or "lyfeng001"}})
    return True


def load():
    if not os.path.exists(CONFIG):
        src = EXAMPLE if os.path.exists(EXAMPLE) else None
        text = open(src).read() if src else json.dumps(DEFAULTS, indent=2)
        write_secret(CONFIG, text)
    with open(CONFIG) as f:
        cfg = json.load(f)
    migrated = _migrate(cfg)
    cfg = _merge(DEFAULTS, cfg)
    if migrated:
        save(cfg)
    os.makedirs(DATA, mode=0o700, exist_ok=True)
    os.chmod(DATA, 0o700)
    return cfg


def save(cfg):
    write_secret(CONFIG, json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")


def token():
    p = os.path.join(DATA, "token")
    if not os.path.exists(p):
        write_secret(p, secrets.token_urlsafe(24))
    with open(p) as f:
        return f.read().strip()


def key_env(cfg, kind, value):
    """Environment for a key profile value: one key value is used by claude and codex alike."""
    var = (cfg["keys"].get("env") or {}).get(kind)
    return {var: value} if var and value else {}


def profile(cfg, name):
    for p in cfg["keys"].get("profiles") or []:
        if p.get("name") == name:
            return p
    return None
