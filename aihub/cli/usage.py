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
# ESTIMATE (bedrock/enterprise/subscription billing differs). PRICES is only the last-resort fallback: the real table is
# aihub-prices/prices.json (bundled, refreshed from the hub package aihub-prices), and ~/.aihub/prices.json overrides both:
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


_RX = {}


def _rx(pat):
    """Case-insensitive regex (compiled once); a pattern that is not valid regex is matched as plain text."""
    if pat in _RX:
        return _RX[pat]
    _RX[pat] = _rx_new(pat)
    return _RX[pat]


def _rx_new(pat):
    try:
        return re.compile(pat, re.I)
    except re.error:
        return re.compile(re.escape(pat), re.I)


def _norm(s):
    """Lower case, every run of non-alphanumerics (space _ . / :) becomes one '-': DeepSeek_V4.Flash == deepseek-v4-flash."""
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")


def price_for(model):
    """-> (input, output, cache_write, cache_read) per 1M tokens, or None when the model is unknown.
    ~/.aihub/prices.json keys match as a substring of the model name, ignoring case and separators; the longest (most specific)
    key wins, so "deepseek-v4-flash" prices DeepSeek-V4-Flash-Vision-Exp unless a "deepseek-v4-flash-vision" key exists."""
    nm = _norm(model)
    best = None
    try:
        for k, v in (paths.load("prices.json", {}) or {}).items():
            nk = _norm(k)
            if nk and nk in nm and (best is None or len(nk) > best[0]):
                best = (len(nk), (float(v["input"]), float(v["output"]), float(v.get("cache_write", v["input"] * 1.25)),
                                  float(v.get("cache_read", v["input"] * 0.1))))
    except (TypeError, ValueError, KeyError, AttributeError):
        best = None
    if best:
        return best[1]
    for pat, p in _table():
        if _rx(pat).search(model) or _rx(pat).search(nm):
            return p
    return None


def _files():
    """prices.json candidates, best first: installed hub package, then the bundled one (source folder, else the build copy)."""
    here = os.path.dirname(os.path.abspath(__file__))
    out = [paths.p("packages", "aihub-prices", "prices.json"),
           os.path.join(os.path.dirname(here), "builtin_packages", "aihub-prices", "prices.json")]
    try:
        from importlib import resources
        out.append(resources.files("aihub.core").joinpath("prices.json"))
    except Exception:
        pass
    return out


def _table():
    """[(regex, (in, out, cache_write, cache_read))]: hub package, then bundled file, then the PRICES fallback."""
    out = []
    for f in _files():
        try:
            d = json.loads(f.read_text(encoding="utf-8")) if hasattr(f, "read_text") else json.load(open(f, encoding="utf-8"))
            rows = d.get("prices") or []
        except Exception:
            continue
        for e in rows:
            try:
                i = float(e["input"])
                out.append((str(e["match"]), (i, float(e["output"]), float(e.get("cache_write", i * 1.25)),
                                              float(e.get("cache_read", i * 0.1)))))
            except (TypeError, ValueError, KeyError, AttributeError):
                continue
    return out + PRICES


def _ts(s):
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _lines(path, need=()):
    """JSON objects of a .jsonl file. A line is parsed only if it contains one of the `need` substrings (cheap prefilter:
    most lines of a session log are tool output that never carries usage)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                if need and not any(n in ln for n in need):
                    continue
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


def _files_of(tool):
    root = os.path.join(os.path.expanduser("~"), "." + tool, "projects" if tool == "claude" else "sessions")
    return glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)


def read_claude(since=0.0):
    """Yield one record per assistant message. A message streams as several lines with one id: the last line wins."""
    for fp in _files_of("claude"):
        try:
            if os.path.getmtime(fp) < since:
                continue
        except OSError:
            continue
        for r in parse_claude(fp):
            yield r


def parse_claude(fp):
    msgs = {}
    for j in _lines(fp, ('"usage"',)):
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
    return list(msgs.values())


def read_codex(since=0.0):
    """Codex logs cumulative totals: each increase is one request, attributed to the model of the latest turn."""
    for fp in _files_of("codex"):
        try:
            if os.path.getmtime(fp) < since:
                continue
        except OSError:
            continue
        for r in parse_codex(fp):
            yield r


def parse_codex(fp):
    out = []
    sid, model, prev = os.path.basename(fp), "unknown", {}
    for j in _lines(fp, ('"session_meta"', '"turn_context"', '"token_count"')):
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
            out.append({"ts": ts, "tool": "codex", "session": sid, "model": model, "input": max(0, d["input_tokens"] - d["cached_input_tokens"]),
                        "output": d["output_tokens"], "cache_write": 0, "cache_read": d["cached_input_tokens"]})
    return out


CACHE = "usage-cache.json"
CACHE_V = 1


def _cache_load():
    try:
        with open(paths.p(CACHE), encoding="utf-8") as f:
            d = json.load(f)
        return d["files"] if d.get("v") == CACHE_V and isinstance(d.get("files"), dict) else {}
    except Exception:
        return {}


def _cache_save(files):
    try:
        paths.ensure()
        tmp = paths.p(CACHE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"v": CACHE_V, "files": files}, f, separators=(",", ":"))
        os.replace(tmp, paths.p(CACHE))
    except (OSError, TypeError, ValueError):
        pass


def collect(tools=TOOLS, since=0.0, progress=None):
    """All records. A log file is parsed once: its records are cached by (mtime, size), so later runs only read new or
    changed files. progress(done, total, tool) is called per file (for a spinner)."""
    cache, fresh, out, dirty = _cache_load(), {}, [], False
    jobs = [(t, fp) for t in tools for fp in _files_of(t)]
    for i, (t, fp) in enumerate(jobs):
        if progress:
            progress(i, len(jobs), t)
        try:
            st = os.stat(fp)
        except OSError:
            continue
        key = "%s|%s" % (t, fp)
        hit = cache.get(key)
        if hit and hit.get("m") == st.st_mtime and hit.get("s") == st.st_size:
            recs = hit["r"]
        else:
            recs = (parse_claude if t == "claude" else parse_codex)(fp)
            dirty = True
        fresh[key] = {"m": st.st_mtime, "s": st.st_size, "r": recs}
        out += recs
    if progress:
        progress(len(jobs), len(jobs), "")
    if dirty or len(fresh) != len(cache):                        # files added, changed or removed (no deep compare of the whole cache)
        _cache_save(fresh)
    return out


def cost(r, rates=None):
    """Estimated USD of one record. `rates` (model -> price tuple or None) memoizes price_for for a whole report: the price files are
    read and the patterns matched once per model, not once per record."""
    if rates is None:
        p = price_for(r["model"])
    else:
        m = r["model"]
        if m not in rates:
            rates[m] = price_for(m)
        p = rates[m]
    if not p:
        return None
    return (r["input"] * p[0] + r["output"] * p[1] + r["cache_write"] * p[2] + r["cache_read"] * p[3]) / 1e6


def summarize(records, key, rates=None):
    """Group records by key(r) -> rows with sessions, requests, tokens, cache hit rate and estimated cost."""
    g = {}
    rates = {} if rates is None else rates
    for r in records:
        a = g.setdefault(key(r), {"sessions": set(), "requests": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0,
                                  "cost": 0.0, "unpriced": 0})
        a["sessions"].add((r["tool"], r["session"]))
        a["requests"] += 1
        for k in ("input", "output", "cache_write", "cache_read"):
            a[k] += r[k]
        c = cost(r, rates)
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


def report(tools=TOOLS, days=30, now=None, model=None, recs=None, progress=None):
    """model = case-insensitive regex on "tool model" (e.g. "OPUS|gpt"); recs = records from collect() to re-filter without re-reading."""
    recs = collect(tools, 0.0, progress) if recs is None else recs
    if model:
        rx = _rx(model)
        recs = [r for r in recs if rx.search("%s %s" % (r["tool"], r["model"]))]
    out, rates, dayname = [], {}, {}

    def day(r):
        k = int(r["ts"]) // 3600                                  # strftime once per hour bucket, not once per record
        if k not in dayname:
            dayname[k] = datetime.datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d")
        return dayname[k]

    for label, lo in periods(now, days):
        rs = [r for r in recs if r["ts"] >= lo]
        out.append({"period": label, "since": lo, "total": (summarize(rs, lambda r: "all", rates) or [None])[0],
                    "by_day": sorted(summarize(rs, day, rates), key=lambda x: x["name"], reverse=True),
                    "by_tool": summarize(rs, lambda r: r["tool"], rates), "by_model": summarize(rs, lambda r: (r["tool"], r["model"]), rates)})
    return out


def tok(n):
    n = float(n)
    for u, d in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if n >= d:
            return "%.1f%s" % (n / d, u)
    return "%d" % n


def usd(v, unpriced=0):
    return ("$%.2f" % v if v >= 0.01 or not v else "$%.3f" % v) + ("*" if unpriced else "")


def _bar(frac, n=10):
    k = max(0, min(n, int(round(frac * n))))
    return ("█" * k).ljust(n, "░")


def _row(name, a, ui=None, top=0.0):
    """Table row. With a ui: cost is coloured by its share of the biggest row, the cache hit rate by quality."""
    hit = round(a["cache_hit"] * 100)
    cost_s, hit_s = usd(a["cost"], a["unpriced"]), "%d%%" % hit
    if ui:
        share = a["cost"] / top if top else 0.0
        cost_s = (ui.err if share > 0.66 else ui.warn if share > 0.25 else ui.ok)(cost_s)
        hit_s = (ui.ok if hit >= 70 else ui.warn if hit >= 40 else ui.err)(hit_s) if a["cache_read"] + a["input"] + a["cache_write"] else ui.dim(hit_s)
        name = ui.accent(name)
    return [name, str(a["sessions"]), str(a["requests"]), tok(a["input"]), tok(a["output"]), tok(a["cache_write"]),
            tok(a["cache_read"]), hit_s, cost_s]


HEADS = ["Sessions", "Requests", "Input", "Output", "Cache write", "Cache read", "Hit", "Est. cost"]


def _rows(items, ui, label=lambda x: x["name"]):
    top = max([x["cost"] for x in items] or [0.0])
    return [_row(label(x), x, ui, top) for x in items]


def show_period(p, ui, view="summary"):
    """One period. view: summary (by tool + by model) or day (one row per local day, newest first)."""
    t = p["total"]
    ui.heading("%s  %s" % (p["period"], ui.dim("%d sessions, est. " % t["sessions"]) + ui.bold(usd(t["cost"], t["unpriced"])) if t else ui.dim("no usage")))
    if not t:
        return False
    if view == "day":
        ui.table(["Day"] + HEADS, _rows(p["by_day"][:90], ui))
    else:
        ui.table(["Tool"] + HEADS, _rows(p["by_tool"], ui))
        ui.table(["Model"] + HEADS, _rows(p["by_model"][:12], ui, lambda x: "%s: %s" % (x["name"][0], x["name"][1].split("/")[-1])))
        top = max([x["cost"] for x in p["by_model"]] or [0.0])
        if top and len(p["by_model"]) > 1:
            ui.out("")
            for x in p["by_model"][:5]:
                ui.out("  %s %s %s" % (ui.accent(_bar(x["cost"] / top)), usd(x["cost"], x["unpriced"]).rjust(9), ui.dim(x["name"][1].split("/")[-1])))
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


def interactive(rep, ui, read=input, refilter=None):
    """Keys (then Enter): 1/2/3 period, d = by day, s = summary, /regex = filter tool+model (case-insensitive, `/` alone clears),
    q = quit. refilter(regex or None) -> a new report (records already loaded: instant)."""
    idx, view, flt = 1, "summary", None
    while True:
        ui.out("")
        un = show_period(rep[idx], ui, view)
        ui.out("")
        footer(ui, un)
        if flt:
            ui.info("filter: /%s" % flt)
        names = [p["period"] for p in rep]
        prompt = "  %s  %s  %s   %s  %s  %s > " % tuple(
            [ui.accent("[%d]" % (i + 1)) + " " + (ui.bold(n) if i == idx else n) for i, n in enumerate(names)] +
             [ui.accent("[s]") + "ummary", ui.accent("[d]") + "ay", ui.accent("[/]") + "filter  " + ui.accent("[q]") + "uit"])
        try:
            k = read(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            ui.out("")
            return
        if k.startswith("/"):
            flt = k[1:].strip() or None
            if refilter:
                rep = refilter(flt)
            continue
        k = k.lower()
        if k in ("q", "quit", "exit"):
            return
        if k in ("1", "2", "3"):
            idx = int(k) - 1
        elif k in ("d", "day"):
            view = "day"
        elif k in ("s", "summary"):
            view = "summary"
