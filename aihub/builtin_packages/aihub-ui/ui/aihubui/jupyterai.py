"""AI help inside the notebook: inline suggestions, "write this cell" and "fix this error". Each has its own model (Notebook settings), picked from the
same list as the chat model picker; an empty model = the default. Nothing runs here: the page shows the code and the user decides."""
import re

from . import jupyter, magic, sqlstudio

FENCE = re.compile(r"```[ \t]*[A-Za-z0-9_+-]*[ \t]*\n(.*?)\n?[ \t]*```", re.S)
CTX_CHARS = 5000

SUGGEST = """You complete code inside a Jupyter notebook cell, like an editor's inline suggestion. You get the earlier cells, the text before the cursor and the text
after it. Reply with ONLY the text to insert at the cursor: no explanation, no markdown fences, never repeat the text before the cursor. Keep it short (a line to
a few lines) and finish the thought the code started. Reply with nothing at all when there is no good continuation."""
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


def _ask(box, system, prompt, key, tokens):
    model = jupyter.settings()[key]["model"]
    text, used, how = magic.ask_model(system, prompt, model, box.get_all() if box else None, "auto", tokens)
    return text, used, how


def _code(text):
    m = FENCE.search(text)
    return (m.group(1) if m else text).strip("\n"), (FENCE.sub("", text) if m else "").strip()[:600]


def suggest(box, prefix, suffix, lang, cells):
    prompt = "# Language: %s\n\n# Earlier cells\n%s\n\n# Text before the cursor\n%s\n\n# Text after the cursor\n%s\n" % (lang or "python", _ctx(cells), prefix[-2500:], suffix[:600])
    text, used, how = _ask(box, SUGGEST, prompt, "suggest", 300)
    text = text.rstrip()
    m = FENCE.search(text)
    if m:
        text = m.group(1)
    if text.startswith(prefix[-40:]) and len(prefix) >= 40:
        text = text[40:]
    return {"text": text, "model": used}


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
