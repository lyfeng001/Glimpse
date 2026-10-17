"""Optional key balancing (keys.balance in config.json): which key a session should use.

Pure functions; the hub calls them with its key list, the usage it fetched (keys.usage_url) and the keys
sessions use right now. Nothing here talks to the network or to a machine.
"""
import re
import time

MODES = ("off", "on_exhausted", "least_used")
EXHAUSTED_TTL = 30 * 60          # a key that ran out is skipped this long (or until its usage shows room again)
SWAP_LIMIT, SWAP_WINDOW = 3, 10 * 60   # automatic swaps per session in that window, then the user decides
REBALANCE_GAP = 30 * 60           # least_used: a session moves at most once in this time

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


def capacity(u, now=None):
    """Sustainable spend rate of a key: what is left of its 7-day quota per second until that window resets.
    None = unknown (no usage data / no long window)."""
    w = week_window(u)
    if not w or not isinstance(w.get("limit"), (int, float)) or w["limit"] <= 0:
        return None
    now = time.time() if now is None else now
    left = max(0.0, float(w["limit"]) - float(w.get("used") or 0))
    secs = (w.get("reset_at") or 0) - now
    if secs <= 0:
        secs = float(w.get("window_sec") or 7 * 86400)
    return left / max(secs, 3600.0)


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


def loads(names, usage, rates, in_use, now=None):
    """Per key: (capacity, measured rate, sessions). The per-session rate of a key is its rate / sessions."""
    out = {}
    for n in names:
        out[n] = (capacity(usage.get(n), now), (rates or {}).get(n), (in_use or {}).get(n, 0))
    return out


def per_session_rate(ld):
    """Typical spend of one session: the average over keys with measured rate and sessions (None = unknown)."""
    xs = [r / k for c, r, k in ld.values() if r is not None and k > 0]
    return sum(xs) / len(xs) if xs else None


def ratio(c, r, k, sess_rate, extra=0):
    """Load ratio of a key with `extra` more sessions: spend / sustainable spend. Lower is better."""
    if c is None:
        return None
    spend = (r if r is not None else (k * sess_rate if sess_rate else 0.0)) + extra * (sess_rate or 0.0)
    if c <= 0:
        return float("inf")
    return spend / c


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
    """Key for one more session: the one whose 7-day load ratio is lowest after adding it (keeps all keys
    burning at the same relative pace). Without usage data: fewest sessions. Then config order."""
    in_use = in_use or {}
    ld = loads(names, usage, rates, in_use, now)
    sr = per_session_rate(ld) or 1.0
    best = None
    for i, n in enumerate(names):
        if not usable(n, usage, exhausted, avoid, any_window):
            continue
        c, r, k = ld[n]
        q = ratio(c, r, k, sr, extra=1)
        score = (0 if q is not None else 1, q if q is not None else 0.0, k, i)
        if best is None or score < best[0]:
            best = (score, n)
    return best[1] if best else None


def should_rebalance(cur, names, usage, exhausted=(), in_use=None, rates=None, now=None, gain=0.2):
    """Between turns: another key for one session on `cur` when moving it lowers the higher of the two
    load ratios by at least `gain` (20 %). None = stay. Needs 7-day usage on both keys."""
    in_use = in_use or {}
    ld = loads(names, usage, rates, in_use, now)
    if cur not in ld or ld[cur][0] is None:
        return None
    sr = per_session_rate(ld)
    if not sr:
        return None
    c0, r0, k0 = ld[cur]
    before_cur = ratio(c0, r0, k0, sr)
    after_cur = ratio(c0, r0, k0, sr, extra=-1)
    best = None
    for n in names:
        if n == cur or not usable(n, usage, exhausted, ()):
            continue
        c, r, k = ld[n]
        if c is None:
            continue
        before = max(before_cur, ratio(c, r, k, sr))
        after = max(after_cur, ratio(c, r, k, sr, extra=1))
        if before > 0 and after <= before * (1 - gain) and (best is None or after < best[0]):
            best = (after, n)
    return best[1] if best else None
