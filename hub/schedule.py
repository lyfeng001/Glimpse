"""Scheduled tasks: cron times (local time) and the checks on a task entry. No dependencies, no I/O.

A task (config.json "schedules"): {"id", "name", "cron": "0 8 * * *", "machine": "M1", "kind": "claude" | "codex",
"cwd", "prompt", "enabled", optional "model" / "effort" / "mode"}. Each run starts an ordinary session with the
prompt as its first message. A run missed while the computer slept (or the machine was offline) happens once
when it is back, if it is not older than CATCH_UP.
"""
import datetime
import re

CATCH_UP = 24 * 3600                 # s; an older missed run is skipped (the next one is near)
LOOK = 8                             # days searched for the previous / next run time
FIELDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))          # minute hour day-of-month month day-of-week (0 = Sunday)
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
PROMPT_MAX = 20000


def parse_field(s, lo, hi, dow=False):
    """One cron field -> the set of values ('*', '5', '1-5', '*/15', '1,3,5', '0-30/10'); ValueError if bad."""
    out = set()
    for part in s.split(","):
        m = re.match(r"^(\*|\d+(?:-\d+)?)(?:/(\d+))?$", part)
        if not m:
            raise ValueError(part)
        rng, step = m.group(1), int(m.group(2) or 1)
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            a, b = map(int, rng.split("-"))
        else:
            a = b = int(rng)
        if dow:
            a, b = (0 if a == 7 else a), (6 if b == 7 and a != 7 else (0 if b == 7 else b))
        if step < 1 or a < lo or b > hi or a > b:
            raise ValueError(part)
        out.update(range(a, b + 1, step))
    return out


class Cron:
    """Standard 5-field cron. Day of month and day of week both restricted: either may match (like cron)."""

    def __init__(self, expr):
        parts = str(expr).split()
        if len(parts) != 5:
            raise ValueError(expr)
        self.expr = " ".join(parts)
        self.mins, self.hours, self.dom, self.months, self.dow = (
            parse_field(p, lo, hi, dow=(i == 4)) for i, (p, (lo, hi)) in enumerate(zip(parts, FIELDS)))
        self.dom_any, self.dow_any = parts[2] == "*", parts[4] == "*"

    def day_ok(self, d):
        if d.month not in self.months:
            return False
        dom, dow = d.day in self.dom, (d.isoweekday() % 7) in self.dow
        if self.dom_any or self.dow_any:
            return dom and dow
        return dom or dow

    def times(self, d):
        """datetimes of one day's runs, in order."""
        return [datetime.datetime(d.year, d.month, d.day, h, mi) for h in sorted(self.hours) for mi in sorted(self.mins)]

    def prev(self, now):
        """The latest run time <= now (a local naive datetime), or None within LOOK days."""
        for back in range(LOOK + 1):
            d = (now - datetime.timedelta(days=back)).date()
            if self.day_ok(d):
                for t in reversed(self.times(d)):
                    if t <= now:
                        return t
        return None

    def next(self, now):
        """The first run time > now, or None within LOOK days."""
        for ahead in range(LOOK + 1):
            d = (now + datetime.timedelta(days=ahead)).date()
            if self.day_ok(d):
                for t in self.times(d):
                    if t > now:
                        return t
        return None


def check(task, machines):
    """A task from the phone, checked and normalized; ValueError with the field that is wrong."""
    if not isinstance(task, dict):
        raise ValueError("task")
    out = {}
    name = task.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 60:
        raise ValueError("name")
    out["name"] = name.strip()
    Cron(task.get("cron"))
    out["cron"] = " ".join(str(task["cron"]).split())
    if task.get("machine") not in machines:
        raise ValueError("machine")
    out["machine"] = task["machine"]
    if task.get("kind") not in ("claude", "codex"):
        raise ValueError("kind")
    out["kind"] = task["kind"]
    cwd = task.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip() or len(cwd) > 1024 or "\x00" in cwd:
        raise ValueError("cwd")
    out["cwd"] = cwd.strip()
    prompt = task.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > PROMPT_MAX or "\x00" in prompt:
        raise ValueError("prompt")
    out["prompt"] = prompt.strip()
    for k in ("model", "effort", "mode"):
        v = task.get(k)
        if v not in (None, ""):
            if not isinstance(v, str) or not re.match(r"^[A-Za-z0-9_.:/\[\]@+=-]{1,120}$", v):
                raise ValueError(k)
            out[k] = v
    out["enabled"] = task.get("enabled", True) is not False
    return out


def due(cron, last, now_ts, catch_up=CATCH_UP):
    """Should a task run now? -> (run time ts, or None). last = when it last ran (or was created), ts.
    A slot after last is due; one older than catch_up is skipped (returned as negative, so the caller
    can record it without running)."""
    now = datetime.datetime.fromtimestamp(now_ts)
    t = cron.prev(now)
    if t is None:
        return None
    ts = t.timestamp()
    if last is not None and ts <= last:
        return None
    return ts if now_ts - ts <= catch_up else -ts
