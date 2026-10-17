"""Optional key balancing (keys.balance in config.json): which key a session should use.

Pure functions; the hub calls them with its key list, the usage it fetched (keys.usage_url) and the keys
sessions use right now. Nothing here talks to the network or to a machine.
"""
import re
import time

MODES = ("off", "on_exhausted", "least_used")
EXHAUSTED_TTL = 30 * 60          # a key that ran out is skipped this long (or until its usage shows room again)
SWAP_LIMIT, SWAP_WINDOW = 3, 10 * 60   # automatic swaps per session in that window, then the user decides
REBALANCE_AT, REBALANCE_TO = 90.0, 60.0   # least_used: move a session off a key above 90 % onto one below 60 %

# the key itself is the problem: no balance / quota, rejected / invalid / disabled key
KEY_FATAL_RE = re.compile(
    r"余额不足|额度不足|额度已用|insufficient[_ ](balance|quota|funds)|quota|billing|credit balance|"
    r"权限验证失败|无效的?\s*key|key\s*(无效|已停用|已禁用)|invalid[_ ]?api[_ ]?key|incorrect api key|"
    r"unauthori[sz]ed|authentication[_ ]error|\b401\b|\b40111\b", re.I)
NOT_KEY_RE = re.compile(r"context.{0,20}(length|window)|prompt is too long|too many tokens|rate.?limit", re.I)


def is_key_fatal(text):
    t = text or ""
    return bool(KEY_FATAL_RE.search(t)) and not NOT_KEY_RE.search(t)


def usage_percent(u):
    """Highest window use in percent (None = unknown). A key at 100 % of any window is spent."""
    if not isinstance(u, dict) or u.get("error"):
        return None
    ws = [w.get("percent") for w in u.get("windows") or [] if isinstance(w.get("percent"), (int, float))]
    return max(ws) if ws else None


def mode_of(cfg):
    b = (cfg.get("keys") or {}).get("balance") or {}
    m = b.get("mode")
    return m if m in MODES else "off"


def pool(cfg):
    """Key names that take part in balancing (in config order)."""
    keys = cfg.get("keys") or {}
    ex = set(((keys.get("balance") or {}).get("exclude")) or [])
    return [p["name"] for p in keys.get("profiles") or [] if p.get("name") and p.get("value") and p["name"] not in ex]


def live_exhausted(exhausted, usage, now=None):
    """Drop entries that expired or whose usage shows room again."""
    now = time.time() if now is None else now
    out = {}
    for name, t in (exhausted or {}).items():
        u = usage.get(name) or {}
        fresh = isinstance(u.get("ts"), (int, float)) and u["ts"] > t
        if now - t < EXHAUSTED_TTL and not (fresh and (usage_percent(u) or 0) < 100):
            out[name] = t
    return out


def pick_key(names, usage, exhausted=(), in_use=None, avoid=()):
    """Best key among names: not exhausted / invalid / spent, lowest window use, then fewest sessions on it,
    then config order. None when no key qualifies."""
    in_use = in_use or {}
    best = None
    for i, n in enumerate(names):
        if n in avoid or n in exhausted:
            continue
        u = usage.get(n)
        if isinstance(u, dict) and u.get("error"):
            continue                    # the usage API says the key is invalid / disabled
        pct = usage_percent(u)
        if pct is not None and pct >= 100:
            continue
        score = (pct if pct is not None else 50.0, in_use.get(n, 0), i)
        if best is None or score < best[0]:
            best = (score, n)
    return best[1] if best else None


def should_rebalance(cur, names, usage, exhausted=(), in_use=None):
    """least_used, between turns: a better key when the current one is nearly spent (else None)."""
    pct = usage_percent(usage.get(cur))
    if pct is None or pct < REBALANCE_AT:
        return None
    alt = pick_key(names, usage, exhausted, in_use, avoid=(cur,))
    alt_pct = usage_percent(usage.get(alt)) if alt else None
    return alt if alt and alt_pct is not None and alt_pct <= REBALANCE_TO else None
