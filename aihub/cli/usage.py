"""Local usage report (`aihub usage`): sessions, models, tokens, cache and an estimated price per AI tool.

Reads the tools' own session logs on this machine (Claude Code, Codex). Nothing is sent anywhere and the hub is not involved.
Stdlib only."""
import datetime
import glob
import json
import os
import re

from . import paths

# USD per 1M tokens: (input, output, cache write 5m, cache read). First matching pattern wins. These are list prices as an
# ESTIMATE (bedrock/enterprise/subscription billing differs); override or add entries in ~/.aihub/prices.json:
#   {"my-model": {"input": 3, "output": 15, "cache_write": 3.75, "cache_read": 0.3}}
PRICES = [
    (r"opus", (5.0, 25.0, 6.25, 0.5)),
    (r"sonnet", (3.0, 15.0, 3.75, 0.3)),
    (r"haiku-?(4|5)", (1.0, 5.0, 1.25, 0.1)),
    (r"haiku", (0.8, 4.0, 1.0, 0.08)),
    (r"gpt-5.*mini", (0.25, 2.0, 0.25, 0.025)),
    (r"gpt-5.*nano", (0.05, 0.4, 0.05, 0.005)),
    (r"gpt-5|codex", (1.25, 10.0, 1.25, 0.125)),
    (r"o3|o4-mini", (2.0, 8.0, 2.0, 0.5)),
]
TOOLS = ("claude", "codex")


def price_for(model):
    """-> (input, output, cache_write, cache_read) per 1M tokens, or None when the model is unknown."""
    try:
        for k, v in (paths.load("prices.json", {}) or {}).items():
            if k.lower() in model.lower():
                return (float(v["input"]), float(v["output"]), float(v.get("cache_write", v["input"] * 1.25)),
                        float(v.get("cache_read", v["input"] * 0.1)))
    except (TypeError, ValueError, KeyError, AttributeError):
        pass
    for pat, p in PRICES:
        if re.search(pat, model.lower()):
            return p
    return None


def _ts(s):
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                try:
                    j = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(j, dict):
                    yield j
    except OSError:
        return


def _n(v):
    return v if isinstance(v, int) and v > 0 else 0


def read_claude(since=0.0):
    """Yield one record per assistant message. A message streams as several lines with one id: the last line wins."""
    root = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    for fp in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(fp) < since:
                continue
        except OSError:
            continue
        msgs = {}
        for j in _lines(fp):
            m = j.get("message")
            if j.get("type") != "assistant" or not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
                continue
            model = m.get("model") or ""
            if not model or model.startswith("<"):
                continue
            ts = _ts(j.get("timestamp"))
            if ts is None:
                continue
            u = m["usage"]
            cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
            cw = _n(u.get("cache_creation_input_tokens")) or _n(cc.get("ephemeral_5m_input_tokens")) + _n(cc.get("ephemeral_1h_input_tokens"))
            msgs[m.get("id") or j.get("uuid") or len(msgs)] = {
                "ts": ts, "tool": "claude", "session": str(j.get("sessionId") or os.path.basename(fp)), "model": model,
                "input": _n(u.get("input_tokens")), "output": _n(u.get("output_tokens")), "cache_write": cw,
                "cache_read": _n(u.get("cache_read_input_tokens"))}
        for r in msgs.values():
            yield r


def read_codex(since=0.0):
    """Codex logs cumulative totals: each increase is one request, attributed to the model of the latest turn."""
    root = os.path.join(os.path.expanduser("~"), ".codex", "sessions")
    for fp in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(fp) < since:
                continue
        except OSError:
            continue
        sid, model, prev = os.path.basename(fp), "unknown", {}
        for j in _lines(fp):
            p = j.get("payload") if isinstance(j.get("payload"), dict) else {}
            if j.get("type") == "session_meta":
                sid = str(p.get("id") or sid)
            elif j.get("type") == "turn_context":
                model = str(p.get("model") or model)
            elif j.get("type") == "event_msg" and p.get("type") == "token_count" and isinstance(p.get("info"), dict):
                tot = p["info"].get("total_token_usage")
                ts = _ts(j.get("timestamp"))
                if not isinstance(tot, dict) or ts is None:
                    continue
                cur = {k: _n(tot.get(k)) for k in ("input_tokens", "cached_input_tokens", "output_tokens")}
                if sum(cur.values()) == sum(prev.values()) and cur == prev:
                    continue
                d = {k: (cur[k] - prev.get(k, 0)) if cur[k] >= prev.get(k, 0) else cur[k] for k in cur}     # a drop = counter reset
                prev = cur
                yield {"ts": ts, "tool": "codex", "session": sid, "model": model, "input": max(0, d["input_tokens"] - d["cached_input_tokens"]),
                       "output": d["output_tokens"], "cache_write": 0, "cache_read": d["cached_input_tokens"]}


def collect(tools=TOOLS, since=0.0):
    out = []
    for t in tools:
        out += list({"claude": read_claude, "codex": read_codex}[t](since))
    return out


def cost(r):
    p = price_for(r["model"])
    if not p:
        return None
    return (r["input"] * p[0] + r["output"] * p[1] + r["cache_write"] * p[2] + r["cache_read"] * p[3]) / 1e6


def summarize(records, key):
    """Group records by key(r) -> rows with sessions, requests, tokens, cache hit rate and estimated cost."""
    g = {}
    for r in records:
        a = g.setdefault(key(r), {"sessions": set(), "requests": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0,
                                  "cost": 0.0, "unpriced": 0})
        a["sessions"].add((r["tool"], r["session"]))
        a["requests"] += 1
        for k in ("input", "output", "cache_write", "cache_read"):
            a[k] += r[k]
        c = cost(r)
        if c is None:
            a["unpriced"] += 1
        else:
            a["cost"] += c
    rows = []
    for k, a in g.items():
        ctx = a["input"] + a["cache_write"] + a["cache_read"]
        rows.append(dict(a, name=k, sessions=len(a["sessions"]), cache_hit=(a["cache_read"] / ctx if ctx else 0.0)))
    return sorted(rows, key=lambda x: (-x["cost"], -x["requests"]))


def periods(now=None, days=30):
    """Local-time windows: today (since local midnight), last N days, all."""
    now = now or datetime.datetime.now()
    mid = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return [("Today", mid), ("Last %d days" % days, now.timestamp() - days * 86400), ("All time", 0.0)]


def report(tools=TOOLS, days=30, now=None):
    recs = collect(tools)
    out = []
    for label, lo in periods(now, days):
        rs = [r for r in recs if r["ts"] >= lo]
        out.append({"period": label, "since": lo, "total": (summarize(rs, lambda r: "all") or [None])[0],
                    "by_day": sorted(summarize(rs, lambda r: datetime.datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d")), key=lambda x: x["name"], reverse=True),
                    "by_tool": summarize(rs, lambda r: r["tool"]), "by_model": summarize(rs, lambda r: (r["tool"], r["model"]))})
    return out


def tok(n):
    n = float(n)
    for u, d in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if n >= d:
            return "%.1f%s" % (n / d, u)
    return "%d" % n


def usd(v, unpriced=0):
    return ("$%.2f" % v if v >= 0.01 or not v else "$%.3f" % v) + ("*" if unpriced else "")


def _row(name, a):
    return [name, str(a["sessions"]), str(a["requests"]), tok(a["input"]), tok(a["output"]), tok(a["cache_write"]),
            tok(a["cache_read"]), "%d%%" % round(a["cache_hit"] * 100), usd(a["cost"], a["unpriced"])]


HEADS = ["Sessions", "Requests", "Input", "Output", "Cache write", "Cache read", "Hit", "Est. cost"]


def show_period(p, ui, view="summary"):
    """One period. view: summary (by tool + by model) or day (one row per local day, newest first)."""
    t = p["total"]
    ui.heading("%s  %s" % (p["period"], ui.dim("%d sessions, est. %s" % (t["sessions"], usd(t["cost"], t["unpriced"])) if t else "no usage")))
    if not t:
        return False
    if view == "day":
        ui.table(["Day"] + HEADS, [_row(x["name"], x) for x in p["by_day"][:90]])
    else:
        ui.table(["Tool"] + HEADS, [_row(x["name"], x) for x in p["by_tool"]])
        ui.table(["Model"] + HEADS, [_row("%s: %s" % (x["name"][0], x["name"][1].split("/")[-1]), x) for x in p["by_model"][:12]])
    return any(x["unpriced"] for x in p["by_tool"])


def footer(ui, unpriced):
    ui.info("Estimate from list prices per 1M tokens (see aihub usage --help). Hit = cache read share of all input context." +
            ("  * = some requests use a model with no known price." if unpriced else ""))


def show(rep, ui, by_day=False):
    """Print every period (summary, or by day)."""
    unpriced = False
    for p in rep:
        unpriced = show_period(p, ui, "day" if by_day else "summary") or unpriced
    ui.out("")
    footer(ui, unpriced)


def interactive(rep, ui, read=input):
    """Keys: 1/2/3 period, d = by day, s = summary, r = reload, q = quit. Line based (Enter after the key): works in every terminal."""
    idx, view = 1, "summary"
    while True:
        ui.out("")
        un = show_period(rep[idx], ui, view)
        ui.out("")
        footer(ui, un)
        try:
            k = read("  [1] %s  [2] %s  [3] %s   [s]ummary  [d]ay  [q]uit > " % tuple(p["period"] for p in rep)).strip().lower()
        except (EOFError, KeyboardInterrupt):
            ui.out("")
            return
        if k in ("q", "quit", "exit"):
            return
        if k in ("1", "2", "3"):
            idx = int(k) - 1
        elif k in ("d", "day"):
            view = "day"
        elif k in ("s", "summary"):
            view = "summary"
