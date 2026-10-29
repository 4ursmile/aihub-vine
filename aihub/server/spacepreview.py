"""Tiny text preview of a space for the cards: a few lines (or a 4x4 sheet corner) read from the already built files, no rendering and no images.
Written as `preview.json` when a space is published; spaces published before that get it on the first request."""
import html
import json
import os
import re

LINES, WIDTH, ROWS, COLS, READ = 6, 90, 4, 4, 200000
_DROP = re.compile(r"<(script|style|head|svg|template)\b.*?</\1>", re.S | re.I)
_BREAK = re.compile(r"</(p|h[1-6]|li|tr|div|section|article|pre|blockquote|table)>|<br\s*/?>", re.I)
_TAG = re.compile(r"<[^>]+>")


def _text_lines(markup):
    s = _BREAK.sub("\n", _DROP.sub(" ", markup[:READ]))
    out = []
    for ln in html.unescape(_TAG.sub(" ", s)).split("\n"):
        ln = " ".join(ln.split())
        if len(ln) > 1:
            out.append(ln[:WIDTH])
            if len(out) == LINES:
                break
    return out


def _load(path):
    try:
        with open(path, "rb") as f:
            return f.read(8 * 1024 * 1024 if path.endswith(".json") else READ).decode("utf-8", "replace")
    except OSError:
        return ""


def make(kind, folder):
    """-> {"lines": [...]} | {"rows": [[...]]} | {} for a folder built by spaceconv (kinds with nothing to show give {})."""
    j = lambda n: os.path.join(folder, n)
    try:
        if kind == "slides":
            d = json.loads(_load(j("deck.json")))
            s = (d.get("slides") or [{}])[0]
            lines = _text_lines(s.get("html", "")) or _text_lines(s.get("notes", ""))
            return {"lines": lines, "count": len(d.get("slides") or [])} if lines else {}
        if kind == "sheet":
            sh = (json.loads(_load(j("sheet.json"))).get("sheets") or [{}])[0]
            rows = [[str(c)[:18] for c in r[:COLS]] for r in (sh.get("rows") or [])[:ROWS]]
            return {"rows": rows} if rows else {}
        if kind == "doc":
            lines = _text_lines(_load(j("doc.html")))
        elif kind in ("report", "dashboard", "html"):
            lines = _text_lines(_load(j(os.path.join("site", "index.html"))))
        else:
            return {}
        return {"lines": lines} if lines else {}
    except (ValueError, TypeError, AttributeError, IndexError):
        return {}


def write(kind, folder):
    try:
        with open(os.path.join(folder, "preview.json"), "w", encoding="utf-8") as f:
            json.dump(make(kind, folder), f, ensure_ascii=False)
    except OSError:
        pass
