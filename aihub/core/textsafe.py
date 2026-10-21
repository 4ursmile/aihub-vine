"""Make text safe to store and to hand to a process (stdlib only; CLI and server share it).

Two kinds of characters poison a whole batch of events or a whole backup, long after the place that produced them:
  - a lone surrogate (U+D800..U+DFFF). Python makes one from any byte that is not UTF-8 (a folder name, a `cwd`, an environment variable:
    `os.getcwd()` uses surrogateescape), JSON carries it as "\\udc8d", and every later `.encode("utf-8")` (SQLite/psycopg binding, a git
    stdin pipe, a file write) raises `'utf-8' codec can't encode character '\\udc8d' ... surrogates not allowed`;
  - NUL (U+0000): PostgreSQL text columns refuse it.
Both are replaced (surrogate -> U+FFFD, NUL -> nothing) at the edges: where an event is built, received, bound to SQL, or exported.
"""
import re

_BAD = re.compile("[\x00\ud800-\udfff]")


def _fix(m):
    return "" if m.group() == "\x00" else "�"


def clean(s):
    """The string without lone surrogates / NUL. Anything that is not a str is returned as is."""
    if isinstance(s, str) and _BAD.search(s):
        return _BAD.sub(_fix, s)
    return s


def deep(v, _depth=0):
    """`v` (str, dict, list, tuple) with every string cleaned, also dict keys. Containers are copied only when something changed."""
    if isinstance(v, str):
        return clean(v)
    if _depth > 12:
        return v
    if isinstance(v, dict):
        out, changed = {}, False
        for k, x in v.items():
            k2, x2 = clean(k), deep(x, _depth + 1)
            changed = changed or k2 is not k or x2 is not x
            out[k2] = x2
        return out if changed else v
    if isinstance(v, (list, tuple)):
        out = [deep(x, _depth + 1) for x in v]
        return type(v)(out) if any(a is not b for a, b in zip(out, v)) else v
    return v
