"""Optional key balancing (keys.balance in config.json): which key a session should use.

least_used keeps the keys' 7-day usage percentages close to each other: a new session goes to the key with the
lowest percentage, and between turns a session moves from a key that is clearly ahead (REBALANCE_POINTS) to the
lowest one. "Percentage" is projected a little ahead with the key's measured spend rate (PROJECT_SEC), so several
sessions started between two usage polls don't all land on the same key.

Pure functions; the hub calls them with its key list, the usage it fetched (keys.usage_url) and the keys
sessions use right now. Nothing here talks to the network or to a machine.
"""
import re
import time

MODES = ("off", "on_exhausted", "least_used")
EXHAUSTED_TTL = 30 * 60          # a key that ran out is skipped this long (or until its usage shows room again)
SWAP_LIMIT, SWAP_WINDOW = 3, 10 * 60   # automatic swaps per session in that window, then the user decides
REBALANCE_GAP = 30 * 60           # least_used: a session moves at most once in this time
REBALANCE_POINTS = 5.0            # ... and only off a key this many percentage points above the target
PROJECT_SEC = 3600                # percentages are compared as they will be in an hour at the measured rate

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


def week_window(u):
    """The longest quota window (the 7-day one) of a key's usage, or None."""
    if not isinstance(u, dict) or u.get("error"):
        return None
    ws = [w for w in u.get("windows") or [] if isinstance(w.get("window_sec"), (int, float)) and w["window_sec"] >= 86400]
    return max(ws, key=lambda w: w["window_sec"]) if ws else None


def week_percent(u, rate=None, extra=0.0):
    """A key's 7-day usage in percent, projected PROJECT_SEC ahead at `rate` (its measured spend per second)
    plus `extra` (a session's spend moved to / from it). None = unknown."""
    w = week_window(u)
    if not w:
        return None
    lim = w.get("limit")
    if isinstance(lim, (int, float)) and lim > 0:
        used = float(w.get("used") or 0) + max(0.0, (rate or 0.0) + extra) * PROJECT_SEC
        return 100.0 * used / lim
    p = w.get("percent")
    return float(p) if isinstance(p, (int, float)) else None


class RateTracker:
    """Measured spend rate per key from successive 7-day 'used' readings (EWMA; a reset restarts it)."""
    ALPHA = 0.3

    def __init__(self):
        self.last = {}      # name -> (ts, used)
        self.rate = {}      # name -> per second

    def observe(self, name, u):
        w = week_window(u)
        ts = u.get("ts") if isinstance(u, dict) else None
        if not w or not isinstance(ts, (int, float)):
            return
        used = float(w.get("used") or 0)
        prev = self.last.get(name)
        self.last[name] = (ts, used)
        if not prev or ts <= prev[0]:
            return
        if used < prev[1]:              # window reset
            self.rate.pop(name, None)
            return
        r = (used - prev[1]) / (ts - prev[0])
        self.rate[name] = r if name not in self.rate else self.ALPHA * r + (1 - self.ALPHA) * self.rate[name]


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


def session_rate(names, rates, in_use):
    """Typical spend of one session per second: the average over keys with a measured rate and sessions."""
    xs = [rates[n] / in_use[n] for n in names if (rates or {}).get(n) is not None and (in_use or {}).get(n, 0) > 0]
    return sum(xs) / len(xs) if xs else 0.0


def usable(n, usage, exhausted, avoid, any_window=False):
    """any_window: also skip a key whose short (5 h) window is full (a replacement after a no-balance error
    must work right away); balancing itself only looks at the 7-day window."""
    if n in avoid or n in exhausted:
        return False
    u = usage.get(n)
    if isinstance(u, dict) and u.get("error"):
        return False                    # the usage API says the key is invalid / disabled
    if any_window:
        pct = usage_percent(u)
        return pct is None or pct < 100
    w = week_window(u)
    return not (w and isinstance(w.get("percent"), (int, float)) and w["percent"] >= 100)


def pick_key(names, usage, exhausted=(), in_use=None, avoid=(), rates=None, now=None, any_window=False):
    """Key for one more session: the lowest 7-day usage percentage with that session added (keys with usage
    data first). Then fewest sessions, then config order."""
    in_use, rates = in_use or {}, rates or {}
    sr = session_rate(names, rates, in_use)
    best = None
    for i, n in enumerate(names):
        if not usable(n, usage, exhausted, avoid, any_window):
            continue
        p = week_percent(usage.get(n), rates.get(n), sr)
        score = (p is None, p or 0.0, in_use.get(n, 0), i)
        if best is None or score < best[0]:
            best = (score, n)
    return best[1] if best else None


def should_rebalance(cur, names, usage, exhausted=(), in_use=None, rates=None, now=None):
    """Between turns: move one session off `cur` when its 7-day percentage is REBALANCE_POINTS or more above
    the lowest usable key's, to that key (fewer sessions, then config order on a tie). None = stay.
    The session takes its own spend along (its key's measured rate / its sessions); a move that would leave the
    higher of the two percentages no lower is skipped."""
    in_use, rates = in_use or {}, rates or {}
    k0 = in_use.get(cur, 0)
    r0 = rates.get(cur)
    mine = r0 / k0 if r0 is not None and k0 > 0 else session_rate(names, rates, in_use)
    p0 = week_percent(usage.get(cur), r0)
    if p0 is None:
        return None
    p0_after = week_percent(usage.get(cur), r0, -mine)
    best = None
    for i, n in enumerate(names):
        if n == cur or not usable(n, usage, exhausted, ()):
            continue
        p = week_percent(usage.get(n), rates.get(n))
        if p is None or p0 - p < REBALANCE_POINTS:
            continue
        there = week_percent(usage.get(n), rates.get(n), mine)
        if mine > 0 and max(p0_after, there) >= max(p0, p):     # (spend unknown yet: the gap alone decides)
            continue
        score = (p, in_use.get(n, 0), i)
        if best is None or score < best[0]:
            best = (score, n)
    return best[1] if best else None
