"""Data for the Usage page and the price alerts: `aihub --json usage` (local session logs + this UI's free chat, priced by the CLI with the
hub's price table) plus the alert rules from the preferences. Estimated cost: list prices, a subscription or enterprise bill differs.

An alert rule = {id, period: today|week|month|quarter|last, n (for last: days), limit (USD), at: [percent, ...]}. Its spend is summed from
the CLI's per-day costs (local dates, week starts Monday). Every threshold in `at` that the spend has reached raises an alert; the highest
one decides the level (100 or more = over). week/month/quarter also warn when the pace so far would pass the limit."""
import calendar
import datetime
import threading
import time

from . import aihub as A, hubusage, store

_cache = {}
_lock = threading.Lock()
TTL = 45.0
PERIODS = {"today": "Today", "week": "This week", "month": "This month", "quarter": "This quarter", "last": "Last %d days"}


def label(r):
    return PERIODS[r["period"]] % r["n"] if r["period"] == "last" else PERIODS[r["period"]]


def window(period, n, now):
    """-> (first local date, length in days of the whole period)."""
    d = now.date()
    if period == "today":
        return d, 1
    if period == "week":
        return d - datetime.timedelta(days=d.weekday()), 7
    if period == "month":
        return d.replace(day=1), calendar.monthrange(d.year, d.month)[1]
    if period == "quarter":
        m = 3 * ((d.month - 1) // 3) + 1
        first = datetime.date(d.year, m, 1)
        end = datetime.date(d.year + (m == 10), (m + 3 - 1) % 12 + 1, 1)
        return first, (end - first).days
    return d - datetime.timedelta(days=n - 1), n


def spent(by_day, period, n, now):
    lo, _ = window(period, n, now)
    lo = lo.isoformat()
    return sum(float(x.get("cost") or 0) for x in by_day if str(x.get("name")) >= lo)


def evaluate(by_day, rules, now=None):
    """-> (rules with their state, alerts). Each state: spent, pct, crossed (highest threshold reached, 0 = none), level ok|warn|over."""
    now = now or datetime.datetime.now()
    states, alerts = [], []
    for r in rules:
        lim = float(r["limit"])
        sp = spent(by_day, r["period"], r["n"], now)
        pct = sp / lim * 100 if lim > 0 else 0
        hit = [t for t in r["at"] if pct >= t]
        crossed = max(hit) if hit else 0
        lv = "over" if pct >= 100 else "warn" if crossed else "ok"
        st = dict(r, label=label(r), spent=sp, pct=pct, crossed=crossed, level=lv, projected=None)
        if r["period"] in ("week", "month", "quarter"):
            lo, length = window(r["period"], r["n"], now)
            done = (now.date() - lo).days + now.hour / 24.0 + 1 / 24.0
            if done >= 0.1 * length:
                st["projected"] = sp / done * length
        states.append(st)
        who = "%s: $%.2f of $%.2f (%d%%)" % (st["label"], sp, lim, pct)
        if lv == "over":
            alerts.append({"id": r["id"], "level": "over", "title": st["label"] + ": limit reached", "text": who + ".", "spent": sp, "limit": lim, "pct": pct})
        elif crossed:
            alerts.append({"id": r["id"], "level": "warn", "title": "%s: passed %d%%" % (st["label"], crossed), "spent": sp, "limit": lim, "pct": pct,
                           "text": who + ("; at this pace it ends near $%.2f." % st["projected"] if st["projected"] else ".")})
        elif st["projected"] and st["projected"] > lim:
            alerts.append({"id": r["id"] + ":pace", "level": "warn", "title": st["label"] + ": on pace to pass the limit", "spent": sp, "limit": lim, "pct": pct,
                           "text": "$%.2f so far; at this pace it ends near $%.2f (limit $%.2f)." % (sp, st["projected"], lim)})
    return states, alerts


def report(days=7, force=False):
    pr = store.prefs()
    rules = pr.get("alerts") or []
    days = max(1, min(365, int(days or 7)))
    key = (days, repr(rules))
    with _lock:
        hit = _cache.get(key)
        if hit and not force and time.time() - hit[0] < TTL:
            return hit[1]
    doc = A.run(["usage", "--days", str(days)], timeout=240)
    per = {p["key"]: p for p in (doc.get("data") or []) if isinstance(p, dict)}
    slim = {k: {"period": p.get("period"), "total": p.get("total"), "by_day": p.get("by_day") or [], "by_tool": p.get("by_tool") or [],
                "by_model": p.get("by_model") or []} for k, p in per.items()}
    states, alerts = evaluate((per.get("all") or {}).get("by_day") or [], rules)
    out = {"days": days, "periods": slim, "rules": states, "alerts": alerts, "sync": hubusage.sync_state()}
    with _lock:
        _cache.clear()
        _cache[key] = (time.time(), out)
    return out
