"""Schedules: seconds or a 5-field cron expression. Shared by the hub (sync, backup, updates) and the CLI (`aihub run --schedule`). Stdlib only."""
import re
import time

UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _field(spec, lo, hi):
    out = set()
    for part in spec.split(","):
        rng, _, step = part.partition("/")
        step = int(step) if step else 1
        if step < 1:
            raise ValueError("bad step")
        if rng in ("*", ""):
            a, b = lo, hi
        elif "-" in rng:
            a, b = (int(x) for x in rng.split("-", 1))
        else:
            a = int(rng)
            b = hi if step > 1 else a
        if a < lo or b > hi or a > b:
            raise ValueError("out of range")
        out.update(range(a, b + 1, step))
    return out


def parse_schedule(text, lo=10, hi=86400):
    """'60' (seconds, lo..hi; also 30s / 5m / 2h / 1d) or a 5-field cron 'min hour dom month dow' (*, lists, ranges, steps). -> ('every', secs) | ('cron', fields)."""
    t = str(text).strip()
    m = re.match(r"^(?:every\s+)?(\d+)\s*([smhd])$", t.lower())
    if m:
        t = str(int(m.group(1)) * UNITS[m.group(2)])
    if t.isdigit():
        n = int(t)
        if not lo <= n <= hi:
            raise ValueError("seconds must be between %d and %d" % (lo, hi))
        return ("every", n)
    f = t.split()
    if len(f) != 5:
        raise ValueError("use seconds (e.g. 60) or a 5-field cron expression (e.g. */5 * * * *)")
    try:
        mi, ho, dom, mo, dow = _field(f[0], 0, 59), _field(f[1], 0, 23), _field(f[2], 1, 31), _field(f[3], 1, 12), _field(f[4], 0, 7)
    except ValueError as e:
        raise ValueError("invalid cron expression: %s" % e)
    return ("cron", (mi, ho, dom, mo, {d % 7 for d in dow}, f[2] != "*", f[4] != "*"))


def next_delay(schedule, now=None):
    """Seconds until the next run."""
    kind, v = schedule
    now = now or time.time()
    if kind == "every":
        return v
    mi, ho, dom, mo, dow, dom_set, dow_set = v
    t = int(now // 60 + 1) * 60
    for _ in range(366 * 24 * 60):
        lt = time.localtime(t)
        d_ok, w_ok = lt.tm_mday in dom, (lt.tm_wday + 1) % 7 in dow
        day_ok = (d_ok or w_ok) if (dom_set and dow_set) else (d_ok and w_ok)      # standard cron: both restricted = OR
        if lt.tm_min in mi and lt.tm_hour in ho and lt.tm_mon in mo and day_ok:
            return max(1, t - now)
        t += 60
    return 3600
