"""AI help inside the notebook: inline suggestions, "write this cell" and "fix this error". Each has its own model (Notebook settings), picked from the
same list as the chat model picker; an empty model = the default. Nothing runs here: the page shows the code and the user decides."""
import re

import atexit
import hashlib
import json
import threading
import time

from . import codetree, jupyter, magic, sqlstudio, store

FENCE = re.compile(r"```[ \t]*[A-Za-z0-9_+-]*[ \t]*\n(.*?)\n?[ \t]*```", re.S)
CTX_CHARS = 5000

SUGGEST = """You are the code completion engine of a Jupyter notebook (like GitHub Copilot). The user message has notebook facts, the code before <CURSOR> and the code after it.
Your whole reply is pasted into the file verbatim, so it is code only: never explain, never greet, never say what is missing. No fences, never repeat code that is already before it, join cleanly with what follows it.
Use the real variable names, column names and imports from the facts; follow the style of the code. If MODE is line, finish the current statement (more lines only when the
statement needs them). If MODE is block, write the next few lines that do what the preceding comment or loop asks. If MODE is function, write the whole body of the def or class being started (a one-line docstring is fine), starting with a line break when the cursor ends the def line, and use the arguments, imports and helpers from the facts. Reply with nothing when unsure."""
WRITE = """You write code for a Jupyter notebook cell. You get the notebook's earlier cells (code and a short view of their output), the cell now being edited and
the request. Reply with ONE fenced code block holding the complete cell, then at most two short sentences (assumptions, what to run first).
- Use the language of the notebook's kernel. Reuse variables and imports that earlier cells already define; do not repeat them.
- Plain, runnable code; short comments only where useful. Pandas tables: end the cell with the DataFrame so it is shown as a table; plots: matplotlib or plotly figures
  that display inline. Never invent credentials or file paths you were not given."""
FIX = """You fix a failing Jupyter notebook cell. You get the earlier cells, the failing cell and its error. Reply with ONE fenced code block holding the complete
corrected cell, then one or two sentences naming the cause. Change as little as needed. If the cause is outside the cell (a missing package, a variable an earlier
cell should define), say so after the block and still make the cell as correct as you can."""


def _ctx(cells):
    """The earlier cells as text, newest last, cut to a budget (oldest dropped first)."""
    parts, size = [], 0
    for c in reversed(cells or []):
        t = "# [%s cell]\n%s" % (c.get("type") or "code", str(c.get("src") or "")[:1500])
        o = str(c.get("out") or "")[:300]
        if o:
            t += "\n# output:\n# " + o.replace("\n", "\n# ")
        if size + len(t) > CTX_CHARS:
            break
        parts.append(t)
        size += len(t)
    return "\n\n".join(reversed(parts)) or "(no earlier cells)"


def _ask(box, system, prompt, key, tokens, quick=False):
    model = jupyter.settings()[key]["model"]
    text, used, how = magic.ask_model(system, prompt, model, box.get_all() if box else None, "auto", tokens, quick)
    return text, used, how


def _code(text):
    m = FENCE.search(text)
    return (m.group(1) if m else text).strip("\n"), (FENCE.sub("", text) if m else "").strip()[:600]


_IMPORT = re.compile(r"^\s*(?:import|from)\s+\S.*$")
_DEF = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+[^\n]*?(?::\s*$|$)")
_ASSIGN = re.compile(r"^([A-Za-z_]\w*)\s*(?::[^=]+)?=\s*([^\n]{0,70})")


def digest(cells, budget=1600):
    """What a completion needs to know about the notebook, in a few hundred tokens: imports, function/class signatures, top-level names with a hint of their value,
    then the last cell verbatim with its output (columns of a table, an error). Older cells are never sent whole."""
    code = [c for c in (cells or []) if (c.get("type") or "code") in ("code", "sql")]
    imports, defs, names = [], [], {}
    for c in code[:-1]:
        for ln in str(c.get("src") or "").splitlines()[:600]:
            if _IMPORT.match(ln) and ln.strip() not in imports:
                imports.append(ln.strip()[:100])
            elif _DEF.match(ln) and not ln.startswith(" ") and ln.strip() not in defs:
                defs.append(ln.strip()[:110])
            else:
                m = _ASSIGN.match(ln)
                if m:
                    names[m.group(1)] = m.group(2).strip()
    parts = []
    if imports:
        parts.append("imports: " + "; ".join(imports[-12:]))
    if defs:
        parts.append("defined: " + " | ".join(defs[-10:]))
    if names:
        parts.append("variables: " + "; ".join("%s=%s" % (k, v) for k, v in list(names.items())[-14:]))
    if code:
        last = code[-1]
        t = "previous cell:\n" + str(last.get("src") or "")[-500:]
        o = str(last.get("out") or "")[:220]
        parts.append(t + ("\noutput: " + o if o else ""))
    return "\n".join(parts)[-budget:]


def _overlap(prefix, text):
    """Models often start by repeating the end of the prefix; drop that."""
    for k in range(min(len(prefix), len(text), 60), 1, -1):
        if text.startswith(prefix[-k:]):
            return text[k:]
    return text


_PROSE = re.compile(r"^[A-Z][a-z]+(?:\s+[\w'`,.-]+){3,}[.:!]?\s*$")


_DEFLINE = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+\w+.*:\s*$")


def _mode(prefix):
    """function: the cursor ends a def/class line or starts the line under it (write the whole body); block: under a comment or a line ending in ':'; line: finish the statement."""
    lines = prefix.split("\n")
    cur = lines[-1]
    if cur.strip():
        return ("function", 40) if _DEFLINE.match(cur) else ("line", 3)
    prev = next((ln for ln in reversed(lines[:-1]) if ln.strip()), "")
    if _DEFLINE.match(prev):
        return "function", 40
    if prev.rstrip().endswith(":") or prev.lstrip().startswith("#"):
        return "block", 14
    return "line", 4


def _trim(text, prefix, suffix, mode, maxlines):
    """Cut the reply where the code it continues would end: at the first dedent below the block's indent, at a blank line in line mode, or where it meets the suffix."""
    text = _overlap(prefix, text.replace("<CURSOR>", ""))
    if mode == "function" and prefix.split("\n")[-1].strip() and not text.startswith("\n"):
        pl = prefix.split("\n")[-1]
        text = "\n" + " " * (len(pl) - len(pl.lstrip()) + 4) + text.lstrip()      # the body goes on the next line, one level in
    lines = text.split("\n")
    if mode != "line" and len(lines) > 1 and _PROSE.match(lines[0]):          # a stray sentence before the code: drop everything up to the first blank line
        k = next((i for i, ln in enumerate(lines) if not ln.strip()), None)
        lines = lines[k + 1:] if k is not None else []
    base = len(prefix.split("\n")[-1]) - len(prefix.split("\n")[-1].lstrip())
    prevl = next((ln for ln in reversed(prefix.split("\n")[:-1]) if ln.strip()), "")
    curl = prefix.split("\n")[-1]
    if mode == "function" and curl.strip():
        base = len(curl) - len(curl.lstrip()) + 1          # cursor ends the def line: the body is indented one level deeper
    elif mode in ("block", "function") and prevl.rstrip().endswith(":"):
        base = len(prevl) - len(prevl.lstrip()) + 1       # the body must stay inside the def/for/if above
    out = []
    for i, ln in enumerate(lines):
        if i and ln.strip():
            ind = len(ln) - len(ln.lstrip())
            if mode == "line" and ind <= base and out and not out[-1].rstrip().endswith((":", "(", "[", "{", ",", "\\")):
                break
            if mode != "line" and ind < base and base > 0:
                break
        if i and not ln.strip() and (mode == "line" or (out and not out[-1].strip())):
            break
        out.append(ln)
        if len(out) >= maxlines:
            break
    text = "\n".join(out).rstrip()
    head = (suffix.lstrip().split("\n")[0] if suffix.strip() else "")[:20].rstrip()
    if head and text.endswith(head):
        text = text[: -len(head)].rstrip()
    return text


# ---- suggestion cache: memory + a small file, reused across restarts; also answers "the same request, a few letters further" from a longer cached reply
_CACHE_FILE, _TTL, _MAX_BUCKETS, _PER_BUCKET = "suggest_cache.json", 7 * 86400, 300, 10
_buckets, _loaded, _dirty, _saved_at, _lock = {}, False, False, 0.0, threading.Lock()


def _h(*parts):
    return hashlib.sha1("\u0001".join(str(p) for p in parts).encode("utf-8", "replace")).hexdigest()[:20]


def _cache_load():
    global _loaded, _buckets
    if _loaded:
        return
    _loaded = True
    try:
        d = store._read(store.ui_dir(_CACHE_FILE), {})
        now = time.time()
        _buckets = {k: [e for e in v if isinstance(e, list) and len(e) == 3 and now - e[2] < _TTL][-_PER_BUCKET:] for k, v in (d or {}).items() if isinstance(v, list)}
        _buckets = {k: v for k, v in _buckets.items() if v}
    except Exception:
        _buckets = {}


def _cache_get(ctx, prefix):
    """-> reply text or None. An entry [prefix, text, time] answers PREFIX when PREFIX is that prefix, or the same prefix with the first letters of the reply typed."""
    with _lock:
        _cache_load()
        for pre, text, ts in reversed(_buckets.get(ctx, [])):
            if prefix == pre:
                return text
            if prefix.startswith(pre) and text and text.startswith(prefix[len(pre):]) and len(prefix) - len(pre) < len(text):
                return text[len(prefix) - len(pre):]
    return None


def _cache_put(ctx, prefix, text):
    global _dirty, _saved_at
    with _lock:
        _cache_load()
        b = _buckets.setdefault(ctx, [])
        b[:] = [e for e in b if e[0] != prefix][-(_PER_BUCKET - 1):] + [[prefix, text, time.time()]]
        while len(_buckets) > _MAX_BUCKETS:
            _buckets.pop(next(iter(_buckets)))
        _dirty = True
        if time.time() - _saved_at > 8:
            _cache_save()


def _cache_save():
    global _dirty, _saved_at
    try:
        store._write(store.ui_dir(_CACHE_FILE), _buckets, True)
        _dirty, _saved_at = False, time.time()
    except Exception:
        pass


def cache_flush():
    with _lock:
        if _dirty:
            _cache_save()


atexit.register(lambda: cache_flush())


def cache_clear():
    global _buckets, _dirty
    with _lock:
        _buckets, _dirty = {}, True
        _cache_save()


def suggest(box, prefix, suffix, lang, cells, ns=""):
    prefix, suffix = prefix[-1800:], suffix[:400]
    if len(prefix.strip()) < 2:
        return {"text": "", "model": "", "cached": True}
    mode, maxlines = _mode(prefix)
    py = (lang or "python") == "python"
    try:
        dg = codetree.context(cells, prefix, suffix) if py else ""
    except Exception:
        dg = ""
    last = [c for c in (cells or []) if (c.get("type") or "code") in ("code", "sql")][-1:]
    if last:                                              # the cell just above, with what it printed: a table's columns, an error
        t = str(last[0].get("src") or "")[-400:]
        o = str(last[0].get("out") or "")[:220]
        dg = (dg + "\n" if dg else "") + "previous cell:\n" + t + ("\noutput: " + o if o else "")
    if not py and not dg:
        dg = digest(cells)
    if ns:
        dg = "live variables:\n" + ns[:700] + ("\n" + dg if dg else "")
    dg = dg[-2600:]
    ctx = _h(jupyter.settings()["suggest"]["model"], lang, mode, suffix[:160], dg)
    hit = _cache_get(ctx, prefix[-1000:])
    if hit is not None:
        return {"text": hit, "model": "", "cached": True, "mode": mode}
    prompt = "[%s] MODE: %s\n%s\n--- before cursor ---\n%s<CURSOR>%s" % (lang or "python", mode, dg, prefix, suffix)
    text, used, how = _ask(box, SUGGEST, prompt, "suggest", {"line": 900, "block": 1200}.get(mode, 2000), True)   # headroom: reasoning models think before they answer
    m = FENCE.search(text)
    text = _trim((m.group(1) if m else text).rstrip(), prefix, suffix, mode, maxlines)
    _cache_put(ctx, prefix[-1000:], text)
    return {"text": text, "model": used, "mode": mode}


def write(box, ask, code, lang, cells, kind="code", conn="", error=""):
    if kind == "sql":
        r = sqlstudio.ask_sql(box, ask, conn, code, error, None, jupyter.settings()["write"]["model"])
        return {"code": r["sql"], "note": r["note"], "conn": r["conn"], "model": r.get("model", "")}
    prompt = "# Kernel language: %s\n\n# Earlier cells\n%s\n\n# Cell being edited\n%s\n\n# Request\n%s\n" % (lang or "python", _ctx(cells), code[:4000] or "(empty)", ask.strip() or "Improve this cell.")
    text, used, _ = _ask(box, WRITE, prompt, "write", 4000)
    out, note = _code(text)
    return {"code": out, "note": note, "model": used}


def fix(box, code, error, lang, cells, kind="code", conn=""):
    if kind == "sql":
        r = sqlstudio.ask_sql(box, "Fix the error.", conn, code, error, None, jupyter.settings()["fix"]["model"])
        return {"code": r["sql"], "note": r["note"], "conn": r["conn"], "model": r.get("model", "")}
    prompt = "# Kernel language: %s\n\n# Earlier cells\n%s\n\n# Failing cell\n%s\n\n# Error\n%s\n" % (lang or "python", _ctx(cells), code[:5000], jupyter.ANSI.sub("", error)[-2500:])
    text, used, _ = _ask(box, FIX, prompt, "fix", 4000)
    out, note = _code(text)
    return {"code": out, "note": note, "model": used}
