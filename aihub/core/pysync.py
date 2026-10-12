"""Keep `pyproject.toml` in step with `aihub.toml` (aihub.toml is the source of truth). Stdlib only; never raises on odd input.
Synced fields: name, version, description. Only single-line string values are touched; dynamic or missing fields are skipped."""
import os
import re

TABLES = ("project", "tool.poetry")            # PEP 621, then Poetry
FIELDS = ("name", "version", "description")
_HEADER = re.compile(r"^\s*\[\s*([A-Za-z0-9_.\"' -]+?)\s*\]\s*(?:#.*)?$")
_LINE = re.compile(r"""^(\s*(%s)\s*=\s*)(?:"((?:[^"\\]|\\.)*)"|'([^']*)')(.*)$""" % "|".join(FIELDS))


def _same(field, a, b):
    if field == "name":                        # PEP 503: my_pkg == My.Pkg == my-pkg
        norm = lambda s: re.sub(r"[-_.]+", "-", s).lower()
        return norm(a) == norm(b)
    return a == b


def _unescape(s):
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(m.group(1), m.group(0)), s)


def _find(lines):
    """-> {field: (line index, current value)} for the first matching table."""
    table, found = None, {}
    for i, ln in enumerate(lines):
        h = _HEADER.match(ln)
        if h and not ln.lstrip().startswith("[["):
            if found and table in TABLES:
                break
            table = h.group(1).replace(" ", "")
            continue
        if table in TABLES:
            m = _LINE.match(ln)
            if m and m.group(2) not in found:
                val = _unescape(m.group(3)) if m.group(3) is not None else m.group(4)
                found[m.group(2)] = (i, val)
    return found


def _read(root):
    p = os.path.join(root, "pyproject.toml")
    try:
        text = open(p, encoding="utf-8", newline="").read()
    except (OSError, UnicodeDecodeError):
        return p, None, "\n"
    return p, text, ("\r\n" if "\r\n" in text else "\n")


def check(root, want):
    """want = {field: value} from aihub.toml. -> [(field, found, wanted)] for each field that differs (empty list = in sync)."""
    p, text, nl = _read(root)
    if text is None:
        return []
    found = _find(text.split(nl))
    return [(f, found[f][1], v) for f, v in want.items() if f in found and v and not _same(f, found[f][1], v)]


def sync(root, want):
    """Rewrite differing fields to the aihub.toml values. -> the list check() returned (what was changed)."""
    diffs = check(root, want)
    if not diffs:
        return []
    p, text, nl = _read(root)
    lines = text.split(nl)
    found = _find(lines)
    for f, _, v in diffs:
        i = found[f][0]
        m = _LINE.match(lines[i])
        esc = v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
        lines[i] = '%s"%s"%s' % (m.group(1), esc, m.group(5))
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(nl.join(lines))
    return diffs


def want(m):
    """The synced fields of a parsed aihub.toml."""
    p = m["package"]
    return {f: p.get(f) or "" for f in FIELDS}
