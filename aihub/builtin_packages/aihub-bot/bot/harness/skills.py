"""Skills of every installed aihub package (AIHUB_SKILL_DIRS). The prompt lists name + description; `read_skill` loads the body."""
import os
import re

from . import config


def _front(text):
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    out = {}
    for ln in (m.group(1).splitlines() if m else []):
        k, _, v = ln.partition(":")
        if v:
            out[k.strip()] = v.strip().strip("\"'")
    return out


def index():
    out = {}
    for d in config.SKILL_DIRS:
        f = os.path.join(d, "SKILL.md")
        try:
            with open(f, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        fm = _front(text)
        out[fm.get("name") or os.path.basename(d)] = {"description": fm.get("description", ""), "path": f}
    return out


def prompt_block():
    ix = index()
    return "\n".join("- %s: %s" % (n, s["description"][:200]) for n, s in sorted(ix.items()))


def read(name):
    s = index().get(name)
    if not s:
        return "no such skill"
    with open(s["path"], encoding="utf-8") as f:
        return f.read()[:40000]
