"""Local usage report (`aihub usage`): sessions, models, tokens, cache and an estimated price per AI tool.

Reads the tools' own session logs on this machine (Claude Code, Codex). Nothing is sent anywhere and the hub is not involved.
Stdlib only."""
import datetime
import glob
import json
import os
import re
import shutil
import sys
import textwrap

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


def _clean(s, n=120):
    return " ".join(str(s).split())[:n]


def parse_claude(fp):
    """-> (records, meta). meta = {session: {"title", "cwd"}}: the AI title (else the first prompt) and the working folder."""
    msgs, meta = {}, {}
    for j in _lines(fp, ('"usage"', '"ai-title"', '"last-prompt"')):
        t = j.get("type")
        if t in ("ai-title", "last-prompt"):
            txt = j.get("aiTitle") if t == "ai-title" else j.get("lastPrompt")
            if j.get("sessionId") and isinstance(txt, str) and txt.strip():
                m = meta.setdefault(str(j["sessionId"]), {})
                if t == "ai-title":
                    m["title"] = _clean(txt)
                else:
                    m.setdefault("prompt", _clean(txt))
            continue
        m = j.get("message")
        if t != "assistant" or not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
            continue
        model = m.get("model") or ""
        if not model or model.startswith("<"):
            continue
        ts = _ts(j.get("timestamp"))
        if ts is None:
            continue
        u = m["usage"]
        sid = str(j.get("sessionId") or os.path.basename(fp))
        if isinstance(j.get("cwd"), str):
            meta.setdefault(sid, {}).setdefault("cwd", j["cwd"])
        cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
        cw = _n(u.get("cache_creation_input_tokens")) or _n(cc.get("ephemeral_5m_input_tokens")) + _n(cc.get("ephemeral_1h_input_tokens"))
        msgs[m.get("id") or j.get("uuid") or len(msgs)] = {
            "ts": ts, "tool": "claude", "session": sid, "model": model,
            "input": _n(u.get("input_tokens")), "output": _n(u.get("output_tokens")), "cache_write": cw,
            "cache_read": _n(u.get("cache_read_input_tokens"))}
    return list(msgs.values()), {k: {"title": v.get("title") or v.get("prompt") or "", "cwd": v.get("cwd") or ""} for k, v in meta.items()}


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
    """-> (records, meta); the title is the first user message."""
    out, meta = [], {}
    sid, model, prev = os.path.basename(fp), "unknown", {}
    for j in _lines(fp, ('"session_meta"', '"turn_context"', '"token_count"', '"user_message"')):
        p = j.get("payload") if isinstance(j.get("payload"), dict) else {}
        if j.get("type") == "session_meta":
            sid = str(p.get("id") or sid)
            if isinstance(p.get("cwd"), str):
                meta.setdefault(sid, {})["cwd"] = p["cwd"]
        elif j.get("type") == "turn_context":
            model = str(p.get("model") or model)
        elif j.get("type") == "event_msg" and p.get("type") == "user_message":
            m = meta.setdefault(sid, {})
            if not m.get("title") and isinstance(p.get("message"), str):
                msg = p["message"].split("## My request for Codex:")[-1]
                m["title"] = _clean(msg)
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
    return out, {k: {"title": v.get("title") or "", "cwd": v.get("cwd") or ""} for k, v in meta.items()}


CACHE = "usage-cache.json"
CACHE_V = 2


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


def collect(tools=TOOLS, since=0.0, progress=None, meta=None):
    """All records. A log file is parsed once: its records are cached by (mtime, size), so later runs only read new or
    changed files. progress(done, total, tool) is called per file (for a spinner). A dict passed as `meta` is filled with
    {(tool, session): {"title", "cwd"}}."""
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
            recs, md = hit["r"], hit.get("t") or {}
        else:
            recs, md = (parse_claude if t == "claude" else parse_codex)(fp)
            dirty = True
        fresh[key] = {"m": st.st_mtime, "s": st.st_size, "r": recs, "t": md}
        out += recs
        if meta is not None:
            for sid, v in md.items():
                cur = meta.setdefault((t, sid), {})
                for k, val in v.items():
                    if val and not cur.get(k):
                        cur[k] = val
    if progress:
        progress(len(jobs), len(jobs), "")
    if dirty or len(fresh) != len(cache):                        # files added, changed or removed (no deep compare of the whole cache)
        _cache_save(fresh)
    return out


def load(tools, ui, quiet=False):
    """collect() behind a spinner (none when quiet) -> (records, meta)."""
    meta = {}
    if quiet:
        return collect(tools, 0.0, None, meta), meta
    with ui.Spinner("Reading session logs") as sp:
        def prog(i, n, tool):
            sp.text("Reading %s session logs (%d/%d)" % (tool, i, n) if n else "Reading session logs")
        recs = collect(tools, 0.0, prog, meta)
        sp.text("Read %d requests" % len(recs))
    return recs, meta


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
    """Local-time windows -> [(key, label, since)]: today (since local midnight), this month (since the 1st, local midnight), the last N
    days (left out when days <= 0), all time."""
    now = now or datetime.datetime.now()
    mid = now.replace(hour=0, minute=0, second=0, microsecond=0)
    out = [("today", "Today", mid.timestamp()), ("month", "This month", mid.replace(day=1).timestamp())]
    if days > 0:
        out.append(("last", "Last %s" % ("day" if days == 1 else "%d days" % days), now.timestamp() - days * 86400))
    out.append(("all", "All time", 0.0))
    return out


def pick(rep, days=30):
    """The report entry the --days window selects (all time when days <= 0)."""
    return next(p for p in rep if p["key"] == ("last" if days > 0 else "all"))


_DAYS = {}


def _day(ts):
    """Local day name of a timestamp. strftime runs once per 15 minute bucket (covers :30 and :45 time zones too)."""
    k = int(ts) // 900
    if k not in _DAYS:
        _DAYS[k] = datetime.datetime.fromtimestamp(k * 900).strftime("%Y-%m-%d")
    return _DAYS[k]


def ctx(r):
    """Context sent with one request: all input, fresh or cached."""
    return r["input"] + r["cache_write"] + r["cache_read"]


def _project(cwd):
    if not cwd:
        return "-"
    home = os.path.expanduser("~")
    if os.path.normpath(cwd) == os.path.normpath(home):
        return "~"
    return os.path.basename(cwd.rstrip("/\\")) or cwd


def sessions_of(records, meta=None, rates=None):
    """Group records into sessions: title, project, time span, tokens, cost, peak/last context and the time ordered requests (`reqs`)."""
    meta = meta or {}
    rates = {} if rates is None else rates
    g = {}
    for r in records:
        k = (r["tool"], r["session"])
        s = g.get(k)
        if s is None:
            m = meta.get(k) or {}
            s = g[k] = {"tool": r["tool"], "session": r["session"], "id": r["session"][-8:], "title": m.get("title") or "", "cwd": m.get("cwd") or "",
                        "project": _project(m.get("cwd")), "start": r["ts"], "end": r["ts"], "requests": 0, "input": 0, "output": 0,
                        "cache_write": 0, "cache_read": 0, "cost": 0.0, "unpriced": 0, "peak": 0, "models": {}, "reqs": []}
        s["start"], s["end"] = min(s["start"], r["ts"]), max(s["end"], r["ts"])
        s["requests"] += 1
        for f in ("input", "output", "cache_write", "cache_read"):
            s[f] += r[f]
        c = cost(r, rates)
        mm = s["models"].setdefault(r["model"], {"requests": 0, "cost": 0.0})
        mm["requests"] += 1
        if c is None:
            s["unpriced"] += 1
        else:
            s["cost"] += c
            mm["cost"] += c
        s["peak"] = max(s["peak"], ctx(r))
        s["reqs"].append(r)
    for s in g.values():
        s["reqs"].sort(key=lambda r: r["ts"])
        tot = s["input"] + s["cache_write"] + s["cache_read"]
        s["cache_hit"] = s["cache_read"] / tot if tot else 0.0
        s["last"] = ctx(s["reqs"][-1])
        s["model"] = max(s["models"], key=lambda m: (s["models"][m]["cost"], s["models"][m]["requests"]))
        s["text"] = ("%s %s %s %s %s" % (s["session"], s["title"], s["cwd"], s["tool"], " ".join(s["models"]))).lower()
    return list(g.values())


SORTS = (("recent", "Recent", lambda s: -s["end"]), ("cost", "Cost", lambda s: -s["cost"]),
         ("ctx", "Peak context", lambda s: -s["peak"]), ("requests", "Requests", lambda s: -s["requests"]))


def sort_sessions(ss, how="recent"):
    fn = next((f for k, _, f in SORTS if k == how), SORTS[0][2])
    return sorted(ss, key=fn)


def find_sessions(ss, text):
    """Sessions whose id, title, folder, tool or model matches text (plain text, or the end of the id)."""
    t = str(text).lower()
    return [s for s in ss if t in s["text"]]


def report(tools=TOOLS, days=30, now=None, model=None, recs=None, progress=None, meta=None):
    """model = case-insensitive regex on "tool model" (e.g. "OPUS|gpt"); recs = records from collect() to re-filter without re-reading.
    Every period also carries `recs` (its records) for sessions_of()."""
    recs = collect(tools, 0.0, progress) if recs is None else recs
    if model:
        rx = _rx(model)
        recs = [r for r in recs if rx.search("%s %s" % (r["tool"], r["model"]))]
    out, rates = [], {}
    for key, label, lo in periods(now, days):
        rs = [r for r in recs if r["ts"] >= lo]
        out.append({"key": key, "period": label, "since": lo, "recs": rs, "total": (summarize(rs, lambda r: "all", rates) or [None])[0],
                    "by_day": sorted(summarize(rs, lambda r: _day(r["ts"]), rates), key=lambda x: x["name"], reverse=True),
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


def prompt_view(rep, ui, read=input, refilter=None, days=30):
    """Line based fallback when the terminal has no raw keys (pipes, dumb terminals). Keys (then Enter): 1/2/3 period, d = by day, s = summary, /regex = filter tool+model (case-insensitive, `/` alone clears),
    q = quit. refilter(regex or None) -> a new report (records already loaded: instant)."""
    idx, view, flt = next((i for i, q in enumerate(rep) if q["key"] == "last"), len(rep) - 1), "summary", None
    while True:
        ui.out("")
        un = show_period(rep[idx], ui, view)
        ui.out("")
        footer(ui, un)
        if flt:
            ui.info("filter: /%s" % flt)
        names = [p["period"] for p in rep]
        prompt = "  %s   %s  %s  %s > " % (
            "  ".join(ui.accent("[%d]" % (i + 1)) + " " + (ui.bold(n) if i == idx else n) for i, n in enumerate(names)),
            ui.accent("[s]") + "ummary", ui.accent("[d]") + "ay", ui.accent("[/]") + "filter  " + ui.accent("[q]") + "uit")
        try:
            k = read(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            ui.out("")
            return
        if k.startswith("/"):
            flt = k[1:].strip() or None
            if refilter:
                rep = refilter(flt, days)
            continue
        k = k.lower()
        if k in ("q", "quit", "exit"):
            return
        if k in ("1", "2", "3", "4") and int(k) <= len(rep):
            idx = int(k) - 1
        elif k in ("d", "day"):
            view = "day"
        elif k in ("s", "summary"):
            view = "summary"


def interactive(rep, ui, meta=None, refilter=None, read=None, start="summary", days=30):
    """Full screen browser (arrows, mouse) in a real terminal; the line based prompt when there is none or `read` is given."""
    if read is None and getattr(ui, "INTERACTIVE", False) and getattr(ui, "FANCY", False):
        try:
            with ui.Keys() as keys:
                Browser(rep, meta, ui, refilter, view=start, days=days).run(keys)
            return
        except OSError:
            pass
    prompt_view(rep, ui, read or input, refilter, days)


# ---------- efficiency tips
BIG_CTX = 150000        # tokens of context per request from which every request gets expensive
COLD_GAP = 300          # seconds idle after which the prompt cache has (about) expired
COLD_MIN = 20000        # cache-write tokens that count as a rebuild


def _rate(model, rates):
    if model not in rates:
        rates[model] = price_for(model)
    return rates[model]


def notes_of(s):
    """Per request flags of a session (memoized): cold = cache rebuilt after an idle gap, compact = context fell by half or
    more, big = over BIG_CTX."""
    if "notes" in s:
        return s["notes"]
    out, prev, pts = [], 0, None
    for r in s["reqs"]:
        c, n = ctx(r), []
        if pts is not None and r["ts"] - pts > COLD_GAP and r["cache_write"] >= COLD_MIN:
            n.append("cold")
        if prev >= 20000 and c < prev * 0.5:
            n.append("compact")
        if c > BIG_CTX:
            n.append("big")
        out.append(n)
        prev, pts = c, r["ts"]
    s["notes"] = out
    return out


def tips(ss, rates=None):
    """Efficiency suggestions from sessions_of() rows -> [{id, level, title, detail, amount (USD affected), sessions [(tool, id)]}],
    biggest first. One pass over the requests."""
    rates = {} if rates is None else rates
    total = sum(s["cost"] for s in ss)
    out = []

    def add(tid, title, detail, amount, keys):
        out.append({"id": tid, "level": "warn" if total and amount >= 0.15 * total else "info", "title": title, "detail": detail,
                    "amount": amount, "sessions": keys})

    cold, extra, ckeys = 0, 0.0, []
    big, bigc, bkeys = 0, 0.0, []
    models = {}
    light, lightc, lkeys = 0, 0.0, []
    for s in ss:
        c0, b0, l0 = cold, big, light
        for r, n in zip(s["reqs"], notes_of(s)):
            p = _rate(r["model"], rates)
            if p is None:
                models.setdefault(r["model"], 0)
                models[r["model"]] += 1
            if "cold" in n:
                cold += 1
                if p:
                    extra += r["cache_write"] * (p[2] - p[3]) / 1e6
            if "big" in n:
                big += 1
                bigc += cost(r, rates) or 0.0
            if p and (p[0] >= 5 or "opus" in r["model"].lower()) and r["output"] < 300 and ctx(r) < 30000:
                light += 1
                lightc += cost(r, rates) or 0.0
        key = (s["tool"], s["session"])
        if cold > c0:
            ckeys.append(key)
        if big > b0:
            bkeys.append(key)
        if light > l0:
            lkeys.append(key)
    if cold >= 2:
        add("cold", "Cache rebuilt after idle gaps",
            "%d requests re-wrote the prompt cache after more than %d minutes idle (about %s extra versus a cache hit). The cache lives "
            "~5 minutes: send follow-ups in one go, and /compact or /clear before a long break so the rebuild is small." % (cold, COLD_GAP // 60, usd(extra)),
            extra, ckeys)
    if big >= 3:
        add("big", "Very large context",
            "%d requests carried more than %s tokens of context (%s). Every request re-reads it. Use /compact, or start a new session when the task changes." % (big, tok(BIG_CTX), usd(bigc)),
            bigc, bkeys)
    lowhit = [s for s in ss if s["requests"] >= 5 and s["input"] + s["cache_write"] + s["cache_read"] >= 200000 and s["cache_hit"] < 0.5]
    if lowhit:
        add("lowhit", "Low cache hit rate",
            "%d sessions read less than half of their input context from cache. Changing the model, tools or system prompt in the middle of a session, or long idle gaps, break the cache." % len(lowhit),
            sum(s["cost"] for s in lowhit), [(s["tool"], s["session"]) for s in lowhit])
    if light >= 10:
        add("light", "Premium model for short replies",
            "%d requests on an Opus-class model produced under 300 output tokens with a small context (%s). A smaller model (Sonnet, Haiku) is usually enough for quick questions and small edits." % (light, usd(lightc)),
            lightc, lkeys)
    tiny = [s for s in ss if s["requests"] <= 2]
    if len(tiny) >= 8:
        add("tiny", "Many one-shot sessions",
            "%d sessions have one or two requests. Each starts with a cold cache and re-sends the system prompt and tools; keep related questions in one session." % len(tiny),
            sum(s["cost"] for s in tiny), [(s["tool"], s["session"]) for s in tiny])
    if ss and total >= 1.0:
        top = max(ss, key=lambda s: s["cost"])
        if top["cost"] >= 0.4 * total:
            add("top", "One session dominates the cost",
                "%s: %s of %s (%d%%). Check its context chart for a long-running thread that could be split or compacted." % (top["title"] or top["id"], usd(top["cost"]), usd(total), round(100 * top["cost"] / total)),
                top["cost"], [(top["tool"], top["session"])])
    if models:
        add("unpriced", "Models without a price",
            "No price for %s, so their cost shows as $0. Add them to ~/.aihub/prices.json." % ", ".join(sorted(models)[:4]), 0.0, [])
    return sorted(out, key=lambda t: -t["amount"])


# ---------- plain output
def when(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def dur(sec):
    sec = int(sec)
    if sec < 60:
        return "<1m"
    if sec < 3600:
        return "%dm" % (sec // 60)
    if sec < 86400:
        return "%dh%02dm" % (sec // 3600, sec % 3600 // 60)
    return "%dd%dh" % (sec // 86400, sec % 86400 // 3600)


def show_sessions(ss, ui, top=0):
    rows = []
    mx = max([s["cost"] for s in ss] or [0.0])
    for s in ss[:top] if top else ss:
        a = dict(s, name="", sessions=1)
        r = _row(s["id"], a, ui, mx)
        rows.append([when(s["end"]), s["tool"], ui.accent(s["id"]), s["project"], str(s["requests"]), tok(s["peak"]), r[7], r[8], s["title"] or "-"])
    ui.table(["Last used", "Tool", "Id", "Project", "Req", "Peak ctx", "Hit", "Est. cost", "Title"], rows)
    if top and len(ss) > top:
        ui.info("%d more sessions (raise --top)" % (len(ss) - top))


def show_session(s, ui):
    """One session: totals, a context sparkline and every request (context size, cache, flags)."""
    ui.heading(s["title"] or s["id"])
    ui.info("%s  id %s  %s  %s" % (s["tool"], s["session"], s["cwd"] or "-", when(s["start"]) + " -> " + when(s["end"]) + " (" + dur(s["end"] - s["start"]) + ")"))
    ui.out("  models: " + ", ".join("%s x%d" % (m.split("/")[-1], v["requests"]) for m, v in sorted(s["models"].items(), key=lambda x: -x[1]["requests"])))
    ui.out("  %d requests, in %s out %s, cache write %s read %s, hit %d%%, est. %s, peak context %s" % (
        s["requests"], tok(s["input"]), tok(s["output"]), tok(s["cache_write"]), tok(s["cache_read"]), round(s["cache_hit"] * 100),
        usd(s["cost"], s["unpriced"]), tok(s["peak"])))
    ui.out("  context " + spark([ctx(r) for r in s["reqs"]], 60, ui.U))
    rates, rows = {}, []
    mx = max([ctx(r) for r in s["reqs"]] or [1]) or 1
    for i, (r, n) in enumerate(zip(s["reqs"], notes_of(s)), 1):
        c = cost(r, rates)
        rows.append([str(i), when(r["ts"]), r["model"].split("/")[-1], tok(r["input"]), tok(r["output"]), tok(r["cache_write"]), tok(r["cache_read"]),
                     tok(ctx(r)) + " " + _bar(ctx(r) / mx, 8), "-" if c is None else usd(c), ",".join(n)])
    ui.table(["#", "Time", "Model", "Input", "Output", "Cache w", "Cache r", "Context", "Cost", "Notes"], rows)


def show_tips(tl, ui):
    if not tl:
        ui.good("Nothing to suggest for this period.")
        return
    for t in tl:
        (ui.note if t["level"] == "warn" else ui.step)("%s  %s" % (ui.bold(t["title"]), ui.dim(usd(t["amount"]) + " affected" if t["amount"] else "")))
        for ln in textwrap.wrap(t["detail"], max(40, ui.width() - 8)):
            ui.out("      " + ln)
    ui.out("")
    ui.info("Open `aihub usage` and press t, then Enter on a tip to list its sessions.")


SPARK = "▁▂▃▄▅▆▇█"


def spark(vals, n, unicode_ok=True):
    """Tiny bar chart of up to n columns (the max of each bucket)."""
    if not vals or n < 1:
        return ""
    chars = SPARK if unicode_ok else " .:-=+*#"
    if len(vals) > n:
        step = len(vals) / float(n)
        vals = [max(vals[int(i * step):max(int((i + 1) * step), int(i * step) + 1)]) for i in range(n)]
    top = max(vals) or 1
    return "".join(chars[min(len(chars) - 1, int(v / float(top) * (len(chars) - 1) + 0.5))] for v in vals)


# ---------- full screen browser
class Browser(object):
    """Interactive usage screen: period tabs, views (sessions / summary / days / tips), a session detail with a context chart.
    Arrows, PgUp/PgDn, Home/End, mouse click and wheel. Rows are drawn only for the visible window and every derived list is
    memoized per (period, filter), so keys never re-read logs or recompute sessions.

    keys: up/down k/j move, left/right period (prev/next session in detail), enter or click open, esc back, tab or m s d t views,
    1-4 period, n custom number of days, o sort, / model regex, f find in sessions, c clear filters, q quit."""
    VIEWS = (("summary", "Summary", "m"), ("sessions", "Sessions", "s"), ("days", "Days", "d"), ("tips", "Tips", "t"))
    NUM = ["Sess", "Req", "Input", "Output", "Cache w", "Cache r", "Hit", "Est. cost"]

    def __init__(self, rep, meta, ui, refilter=None, size=None, out=None, view="summary", period=None, sort="recent", days=30):
        self.rep, self.meta, self.ui, self.refilter = rep, meta or {}, ui, refilter
        self.size, self.out, self.days = size, out, days
        if period is None:                               # open on the "last N days" window (else the last entry)
            period = next((i for i, q in enumerate(rep) if q["key"] == "last"), len(rep) - 1)
        self.view, self.pi, self.sort = view, min(max(period, 0), len(rep) - 1), sort
        self.model = self.find = None
        self.drill = None                                # ("day"|"model"|"tool"|"keys", value, label)
        self.detail = None
        self.cur = self.top = 0
        self.mode, self.buf = None, ""
        self.memo, self.hits, self.msg = {}, [], ""
        self.rates = {}

    # ----- data (memoized)
    def _m(self, key, fn):
        if key not in self.memo:
            self.memo[key] = fn()
        return self.memo[key]

    def period_sessions(self):
        return self._m(("ss", self.pi), lambda: sessions_of(self.rep[self.pi]["recs"], self.meta, self.rates))

    def sessions(self):
        d = self.drill
        key = ("list", self.pi, d and (d[0], str(d[1])), self.find, self.sort)

        def build():
            if d and d[0] == "keys":
                ss = [s for s in self.period_sessions() if (s["tool"], s["session"]) in d[1]]
            elif d:
                f = {"day": lambda r: _day(r["ts"]) == d[1], "model": lambda r: (r["tool"], r["model"]) == d[1],
                     "tool": lambda r: r["tool"] == d[1]}[d[0]]
                ss = sessions_of([r for r in self.rep[self.pi]["recs"] if f(r)], self.meta, self.rates)
            else:
                ss = self.period_sessions()
            if self.find:
                rx = _rx(self.find)
                ss = [s for s in ss if rx.search(s["text"])]
            return sort_sessions(ss, self.sort)
        return self._m(key, build)

    def tip_list(self):
        return self._m(("tips", self.pi), lambda: tips(self.period_sessions(), self.rates))

    def items(self):
        """[{cells: [(text, style)], head: bool, drill, open}] for the current view."""
        v = self.view
        return self._m(("items", v, self.pi, self.drill and (self.drill[0], str(self.drill[1])), self.find, self.sort), getattr(self, "_items_" + v))

    def _cost_style(self, cost, top):
        ui = self.ui
        share = cost / top if top else 0.0
        return ui.err if share > 0.66 else ui.warn if share > 0.25 else ui.ok

    def _hit_style(self, a):
        ui, hit = self.ui, round(a["cache_hit"] * 100)
        return (ui.ok if hit >= 70 else ui.warn if hit >= 40 else ui.err) if a["cache_read"] + a["input"] + a["cache_write"] else ui.dim

    def _items_sessions(self):
        ss = self.sessions()
        top = max([s["cost"] for s in ss] or [0.0])
        out = []
        for s in ss:
            out.append({"open": s, "cells": [
                (when(s["end"]), None), (s["tool"], None), (s["project"], self.ui.accent), (str(s["requests"]), None), (tok(s["peak"]), None),
                ("%d%%" % round(s["cache_hit"] * 100), self._hit_style(s)), (usd(s["cost"], s["unpriced"]), self._cost_style(s["cost"], top)),
                (s["title"] or s["id"], None)]})
        return out

    def _stat_cells(self, name, a, top):
        return [(name, self.ui.accent), (str(a["sessions"]), None), (str(a["requests"]), None), (tok(a["input"]), None), (tok(a["output"]), None),
                (tok(a["cache_write"]), None), (tok(a["cache_read"]), None), ("%d%%" % round(a["cache_hit"] * 100), self._hit_style(a)),
                (usd(a["cost"], a["unpriced"]), self._cost_style(a["cost"], top))]

    def _items_summary(self):
        p, out = self.rep[self.pi], []
        for title, rows, key in (("By tool", p["by_tool"], "tool"), ("By model", p["by_model"][:25], "model")):
            out.append({"head": title})
            top = max([x["cost"] for x in rows] or [0.0])
            for x in rows:
                nm = x["name"] if key == "tool" else "%s: %s" % (x["name"][0], x["name"][1].split("/")[-1])
                out.append({"cells": self._stat_cells(nm, x, top), "drill": (key, x["name"], nm)})
        return out

    def _items_days(self):
        rows = self.rep[self.pi]["by_day"][:366]
        top = max([x["cost"] for x in rows] or [0.0])
        return [{"cells": self._stat_cells(x["name"], x, top), "drill": ("day", x["name"], x["name"])} for x in rows]

    def _items_tips(self):
        out = []
        for t in self.tip_list():
            out.append({"tip": t, "cells": [(t["title"], self.ui.warn if t["level"] == "warn" else self.ui.accent),
                                            (usd(t["amount"]) if t["amount"] else "", None), (str(len(t["sessions"])) + " sessions" if t["sessions"] else "", None)]})
        return out

    def detail_lines(self):
        s = self.detail
        return self._m(("detail", s["tool"], s["session"], self.pi), lambda: self._detail(s))

    def _detail(self, s):
        ui, W = self.ui, self.cols - 2
        L = [{"head": "%s" % (s["title"] or s["id"])},
             {"text": "%s  id %s" % (s["tool"], s["session"])},
             {"text": "%s  %s -> %s (%s)" % (s["cwd"] or "-", when(s["start"]), when(s["end"]), dur(s["end"] - s["start"]))},
             {"text": "models  " + ", ".join("%s x%d" % (m.split("/")[-1], v["requests"]) for m, v in sorted(s["models"].items(), key=lambda x: -x[1]["requests"]))},
             {"text": "%d requests   in %s   out %s   cache write %s   cache read %s   hit %d%%   est. %s" % (
                 s["requests"], tok(s["input"]), tok(s["output"]), tok(s["cache_write"]), tok(s["cache_read"]), round(s["cache_hit"] * 100), usd(s["cost"], s["unpriced"]))},
             {"text": ""},
             {"head": "Context per request (peak %s, last %s)" % (tok(s["peak"]), tok(s["last"]))},
             {"text": spark([ctx(r) for r in s["reqs"]], max(10, W), ui.U), "style": ui.accent},
             {"text": ""}]
        flags = {"cold": "cold cache rebuild", "compact": "context compacted", "big": "very large context"}
        seen = set(n for ns in notes_of(s) for n in ns)
        for k in ("cold", "compact", "big"):
            if k in seen:
                L.append({"text": "%s %s" % (ui.BANG, flags[k]) + "  (" + str(sum(1 for ns in notes_of(s) if k in ns)) + " requests)", "style": ui.warn if k != "compact" else ui.dim})
        L += [{"text": ""}, {"table": True}]
        mx, rates = (max([ctx(r) for r in s["reqs"]] or [1]) or 1), {}
        for i, (r, n) in enumerate(zip(s["reqs"], notes_of(s)), 1):
            c = cost(r, rates)
            L.append({"cells": [(str(i), None), (when(r["ts"]), None), (r["model"].split("/")[-1], None), (tok(r["input"]), None), (tok(r["output"]), None),
                                (tok(r["cache_write"]), None), (tok(r["cache_read"]), None), (tok(ctx(r)), None), (_bar(ctx(r) / float(mx), 10), ui.accent),
                                ("-" if c is None else usd(c), None), (",".join(n), ui.warn)]})
        return L

    # ----- layout
    @property
    def cols(self):
        return (self.size or shutil.get_terminal_size((100, 30)))[0]

    @property
    def rows(self):
        return (self.size or shutil.get_terminal_size((100, 30)))[1]

    def body_h(self):
        return max(3, self.rows - 6)

    def grid(self, cells, widths, aligns, sel):
        """One line: cells [(text, style)] in columns; the last column takes the rest. A selected line is plain + reverse video."""
        ui, parts = self.ui, []
        for i, (t, st) in enumerate(cells):
            w = widths[i] if i < len(widths) else None
            t = ui.clip(t, w) if w else str(t)
            if w:
                t = t.ljust(w) if aligns[i] == "l" else t.rjust(w)
            parts.append(t if sel or not st else st(t))
        return "  ".join(parts)

    def _widths(self, view):
        W = self.cols - 4
        if view == "sessions":
            fixed = [11, 6, 14 if W >= 100 else 9, 4, 7, 4, 9]
            return fixed + [max(8, W - sum(fixed) - 2 * len(fixed))], "lllrrrrl", ["Last used", "Tool", "Project", "Req", "Peak", "Hit", "Cost", "Title"]
        if view in ("summary", "days"):
            rest = [5, 6, 7, 7, 8, 8, 4, 9]
            first = max(12, min(44, W - sum(rest) - 2 * len(rest))) if view == "summary" else 10
            w = [first] + rest
            return w, "l" + "r" * 8, [("Model/tool" if view == "summary" else "Day")] + self.NUM
        if view == "tips":
            return [34, 11, 12], "lrr", ["Suggestion", "Affected", "Sessions"]
        return [3, 11, 22, 7, 7, 8, 8, 8, 10, 9, 10], "lllrrrrrlrl", ["#", "Time", "Model", "Input", "Output", "Cache w", "Cache r", "Context", "", "Cost", "Notes"]

    # ----- drawing
    def frame(self):
        ui, W, H = self.ui, self.cols, self.rows
        rv = (lambda s: "\x1b[7m%s\x1b[0m" % s) if ui.COLOR else (lambda s: s)
        bh = self.body_h()
        self.hits = []
        lines = []
        # title
        p = self.rep[self.pi]
        t = p["total"]
        filt = []
        if self.model:
            filt.append("model /%s" % self.model)
        if self.find:
            filt.append("find %s" % self.find)
        if self.drill:
            filt.append(self.drill[2])
        lines.append(ui.bold("  aihub usage") + ui.dim("   " + (", ".join(filt) if filt else "all tools")))
        # period tabs
        x, tab = 2, "  "
        for i, q in enumerate(self.rep):
            lab = " %d %s " % (i + 1, q["period"])
            self.hits.append((2, x + 1, x + len(lab), ("period", i)))
            tab += (rv(lab) if i == self.pi else ui.dim(lab)) + " "
            x += len(lab) + 1
        lines.append(tab)
        # view tabs
        x, tab = 2, "  "
        for k, name, key in self.VIEWS:
            lab = " %s " % name
            on = self.view == k and not self.detail
            self.hits.append((3, x + 1, x + len(lab), ("view", k)))
            tab += (rv(lab) if on else ui.accent(lab)) + " "
            x += len(lab) + 1
        lines.append(tab)
        lines.append("  " + (ui.dim("%d sessions, %s requests, est. " % (t["sessions"], format(t["requests"], ",")))
                             + ui.bold(usd(t["cost"], t["unpriced"])) + ui.dim("   in %s  out %s  cache read %s" % (tok(t["input"]), tok(t["output"]), tok(t["cache_read"])))
                             if t else ui.dim("no usage in this period")))
        if self.detail:
            self._draw_detail(lines, bh, rv)
        else:
            self._draw_list(lines, bh, rv)
        while len(lines) < H - 1:
            lines.append("")
        lines = lines[:H - 1]
        lines.append(self.footer())
        return "\x1b[H" + "".join(ln + "\x1b[K\n" for ln in lines[:-1]) + lines[-1] + "\x1b[K\x1b[J"

    def _draw_list(self, lines, bh, rv):
        ui = self.ui
        widths, al, heads = self._widths(self.view)
        sortn = next(n for k, n, _ in SORTS if k == self.sort)
        lines.append("  " + ui.dim(self.grid([(h, None) for h in heads], widths, al, True)) + (ui.dim("   sort: %s" % sortn) if self.view == "sessions" else ""))
        its = self.items()
        self._fix(its, bh)
        if not its:
            lines.append("  " + ui.dim("nothing here" + (" (c clears filters)" if self.model or self.find or self.drill else "")))
        for i in range(self.top, min(len(its), self.top + bh)):
            it = its[i]
            y = len(lines) + 1
            if "head" in it:
                lines.append("  " + ui.bold(it["head"]))
                continue
            sel = i == self.cur
            self.hits.append((y, 1, self.cols, ("row", i)))
            line = self.grid(it["cells"], widths, al, sel)
            lines.append(((rv(ui.ARROW + " " + line) if sel else "  " + line)))
        if self.view == "tips" and its and 0 <= self.cur < len(its) and "tip" in its[self.cur]:
            tl = textwrap.wrap(its[self.cur]["tip"]["detail"], max(30, self.cols - 6))
            room = self.rows - 1 - len(lines)
            if room >= 2:
                while len(lines) < self.rows - 1 - min(len(tl), room - 1) - 1:
                    lines.append("")
                lines.append("  " + ui.dim("-" * (self.cols - 4)))
                for ln in tl[:max(1, room - 1)]:
                    lines.append("  " + ln)

    def _draw_detail(self, lines, bh, rv):
        ui = self.ui
        L = self.detail_lines()
        widths, al, heads = self._widths("detail")
        self.top = max(0, min(self.top, max(0, len(L) - bh)))
        for it in L[self.top:self.top + bh]:
            if "head" in it:
                lines.append("  " + ui.bold(it["head"]))
            elif "table" in it:
                lines.append("  " + ui.dim(self.grid([(h, None) for h in heads], widths, al, True)))
            elif "cells" in it:
                lines.append("  " + self.grid(it["cells"], widths, al, False))
            else:
                st = it.get("style")
                lines.append("  " + (st(it["text"]) if st else it["text"]))

    def footer(self):
        ui = self.ui
        if self.mode:
            lab = {"model": "/", "find": "find ", "days": "last N days: "}[self.mode]
            return "  " + ui.accent(lab) + self.buf + ui.dim("   enter apply, esc cancel")
        if self.msg:
            return "  " + ui.warn(self.msg)
        if self.detail:
            h = "up/down scroll   left/right prev/next session   esc back   q quit"
        else:
            h = {"sessions": "enter/click open   o sort   f find   / model   ", "summary": "enter/click: sessions of that row   / model   ",
                 "days": "enter/click: sessions of that day   / model   ", "tips": "enter/click: sessions behind the tip   "}[self.view]
            h += "arrows move   left/right period   n days   tab views   c clear   q quit"
        return "  " + ui.dim(ui.clip(h, self.cols - 4))

    # ----- movement
    def _fix(self, its, bh):
        n = len(its)
        if n == 0:
            self.cur = self.top = 0
            return
        self.cur = max(0, min(self.cur, n - 1))
        if "head" in its[self.cur]:
            self.cur = self._next(its, self.cur, 1) if self._next(its, self.cur, 1) != self.cur else self._next(its, self.cur, -1)
        if self.cur < self.top:
            self.top = self.cur
        elif self.cur >= self.top + bh:
            self.top = self.cur - bh + 1
        if self.cur == self._first(its) and self.top <= self._first(its) + 1:
            self.top = 0
        self.top = max(0, min(self.top, max(0, n - bh)))

    def _first(self, its):
        return next((i for i, it in enumerate(its) if "head" not in it), 0)

    def _next(self, its, i, d):
        j = i + d
        while 0 <= j < len(its) and "head" in its[j]:
            j += d
        return j if 0 <= j < len(its) else i

    def move(self, d):
        if self.detail:
            self.top = max(0, self.top + d)
            return
        its = self.items()
        for _ in range(abs(d)):
            self.cur = self._next(its, self.cur, 1 if d > 0 else -1)

    def jump(self, end):
        its = self.items()
        if self.detail:
            self.top = 10 ** 6 if end else 0
        elif its:
            self.cur = len(its) - 1 if end else self._first(its)

    def reset(self):
        self.cur = self.top = 0

    # ----- actions
    def go_view(self, v):
        self.detail, self.view, self.drill, self.msg = None, v, None, ""
        self.reset()

    def go_period(self, i):
        self.pi = i % len(self.rep)
        self.detail = None
        self.drill = None
        self.reset()

    def activate(self, it):
        if "open" in it:
            self.detail, self.top = it["open"], 0
        elif "tip" in it:
            ks = set(it["tip"]["sessions"])
            if not ks:
                self.msg = "Nothing to open for this tip."
                return
            self.drill, self.view = ("keys", ks, it["tip"]["title"]), "sessions"
            self.sort = "cost"
            self.reset()
        elif it.get("drill"):
            self.drill, self.view = it["drill"], "sessions"
            self.reset()

    def refilter_now(self, keep=False):
        """Rebuild the report for the current model filter and number of days. keep=True leaves the selected period tab where it is."""
        if self.refilter:
            key = self.rep[self.pi]["key"]
            self.rep = self.refilter(self.model, self.days)
            self.pi = next((i for i, q in enumerate(self.rep) if q["key"] == key), min(self.pi, len(self.rep) - 1))
        self.memo.clear()
        self.detail = None
        self.drill = None
        self.reset()

    def back(self):
        if self.detail:
            self.detail = None
        elif self.drill:
            self.drill = None
            self.reset()
        elif self.find or self.model:
            self.find = None
            if self.model:
                self.model = None
                self.refilter_now()
            self.reset()
        else:
            return False
        return True

    def step_session(self, d):
        ss = self.sessions()
        try:
            i = [(s["tool"], s["session"]) for s in ss].index((self.detail["tool"], self.detail["session"]))
        except ValueError:
            return
        if 0 <= i + d < len(ss):
            self.detail, self.top = ss[i + d], 0

    def key(self, k):
        """Handle one key token; returns False to quit."""
        self.msg = ""
        if self.mode:
            return self._typing(k)
        if isinstance(k, tuple):
            return self._mouse(k)
        if k in ("q", "Q", "ctrl-c"):
            return False
        if k == "esc":
            return self.back()
        its = None if self.detail else self.items()
        page = self.body_h() - 1
        if k in ("up", "k"):
            self.move(-1)
        elif k in ("down", "j"):
            self.move(1)
        elif k == "pgup":
            self.move(-page)
        elif k == "pgdn":
            self.move(page)
        elif k == "home":
            self.jump(False)
        elif k == "end":
            self.jump(True)
        elif k in ("left", "h"):
            self.step_session(-1) if self.detail else self.go_period(self.pi - 1)
        elif k in ("right", "l"):
            self.step_session(1) if self.detail else self.go_period(self.pi + 1)
        elif k in ("enter", " "):
            if its and 0 <= self.cur < len(its) and "head" not in its[self.cur]:
                self.activate(its[self.cur])
        elif k == "backspace":
            self.back()
        elif k in ("1", "2", "3", "4") and int(k) <= len(self.rep):
            self.go_period(int(k) - 1)
        elif k == "n" and self.refilter:
            self.mode, self.buf = "days", str(self.days) if self.days > 0 else ""
        elif k in ("tab", "btab"):
            vs = [v[0] for v in self.VIEWS]
            self.go_view(vs[(vs.index(self.view) + (1 if k == "tab" else -1)) % len(vs)])
        elif k in ("s", "m", "d", "t"):
            self.go_view(next(v for v, _, key in self.VIEWS if key == k))
        elif k == "o" and self.view == "sessions" and not self.detail:
            ks = [s[0] for s in SORTS]
            self.sort = ks[(ks.index(self.sort) + 1) % len(ks)]
            self.reset()
        elif k in ("/", "f"):
            self.mode, self.buf = ("model" if k == "/" else "find"), (self.model if k == "/" else self.find) or ""
            if k == "f":
                self.detail, self.view = None, "sessions"
        elif k == "c":
            self.detail = self.drill = None
            self.find = None
            if self.model:
                self.model = None
                self.refilter_now()
            self.reset()
        return True

    def _typing(self, k):
        if k in ("esc", "ctrl-c"):
            self.mode, self.buf = None, ""
        elif k == "enter":
            mode, v = self.mode, self.buf.strip() or None
            self.mode, self.buf = None, ""
            if mode == "days":
                if v and v.isdigit() and 0 < int(v) <= 3650:
                    self.days = int(v)
                    self.refilter_now(keep=True)
                    self.go_period(next(i for i, q in enumerate(self.rep) if q["key"] == "last"))
                else:
                    self.msg = "Type a whole number of days (1-3650)."
            elif mode == "model":
                self.model = v
                self.refilter_now()
            else:
                self.find, self.detail, self.drill = v, None, None
                self.reset()
        elif k == "backspace":
            self.buf = self.buf[:-1]
        elif isinstance(k, str) and len(k) == 1 and k.isprintable():
            self.buf += k
        return True

    def _mouse(self, k):
        if k[0] == "wheel":
            self.move(3 * k[1])
            return True
        _, x, y = k
        for hy, x0, x1, act in self.hits:
            if hy == y and x0 <= x <= x1:
                if act[0] == "period":
                    self.go_period(act[1])
                elif act[0] == "view":
                    self.go_view(act[1])
                else:
                    its = self.items()
                    if act[1] < len(its):
                        self.cur = act[1]                      # a click selects and opens
                        self.activate(its[act[1]])
                return True
        return True

    def run(self, keys):
        """keys: object with get() (ui.Keys). Redraws only after a key; the frame is one write."""
        w = self.out or sys.stdout.write
        while True:
            w(self.frame())
            if hasattr(w, "__self__"):
                sys.stdout.flush()
            if not self.key(keys.get()):
                return
