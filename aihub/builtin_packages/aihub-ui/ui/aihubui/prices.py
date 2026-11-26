"""Estimated USD cost of a Chat / Lite turn (they call the API themselves, so no total_cost_usd comes back like Claude Code's).

Same rules as the CLI's `aihub usage` (aihub/cli/usage.py price_for): ~/.aihub/prices.json overrides (longest matching key wins), then the
hub package aihub-prices/prices.json (installed, else the sibling folder of this package), then a small fallback table. List prices: an
ESTIMATE. Never raises."""
import json
import os
import re

from .aihub import HOME

FALLBACK = [
    (r"opus", (5.0, 25.0, 6.25, 0.5)),
    (r"sonnet", (3.0, 15.0, 3.75, 0.3)),
    (r"haiku-?(4|5)", (1.0, 5.0, 1.25, 0.1)),
    (r"haiku", (0.8, 4.0, 1.0, 0.08)),
]
_cache = {}                                                              # path -> (mtime, parsed)


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")


def _load(path):
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return None
    hit = _cache.get(path)
    if hit and hit[0] == mt:
        return hit[1]
    try:
        with open(path, encoding="utf-8-sig") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = None
    _cache[path] = (mt, d)
    return d


def _four(v):
    i = float(v["input"])
    return (i, float(v["output"]), float(v.get("cache_write", i * 1.25)), float(v.get("cache_read", i * 0.1)))


def _table():
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))      # .../aihub-ui
    out = []
    for f in (os.path.join(HOME(), "packages", "aihub-prices", "prices.json"), os.path.join(os.path.dirname(here), "aihub-prices", "prices.json")):
        d = _load(f)
        for e in ((d or {}).get("prices") or []) if isinstance(d, dict) else []:
            try:
                out.append((str(e["match"]), _four(e)))
            except (TypeError, ValueError, KeyError, AttributeError):
                continue
        if out:
            break                                                        # the installed table is complete: the bundled one is only a fallback
    return out + FALLBACK


def price_for(model):
    """-> (input, output, cache_write, cache_read) USD per 1M tokens, or None when the model is unknown."""
    if not model:
        return None
    nm = _norm(model)
    best = None
    d = _load(os.path.join(HOME(), "prices.json"))
    if isinstance(d, dict):
        for k, v in d.items():
            nk = _norm(k)
            try:
                if nk and nk in nm and (best is None or len(nk) > best[0]):
                    best = (len(nk), _four(v))
            except (TypeError, ValueError, KeyError, AttributeError):
                continue
    if best:
        return best[1]
    for pat, p in _table():
        try:
            rx = re.compile(pat, re.I)
        except re.error:
            rx = re.compile(re.escape(pat), re.I)
        if rx.search(str(model)) or rx.search(nm):
            return p
    return None


def cost(model, u):
    """USD for the tokens in U ({input, output, cache_write, cache_read}) on MODEL; 0 when the model has no price."""
    try:
        p = price_for(model)
        if not p:
            return 0.0
        n = lambda k: max(0, int((u or {}).get(k) or 0))
        return round((n("input") * p[0] + n("output") * p[1] + n("cache_write") * p[2] + n("cache_read") * p[3]) / 1e6, 6)
    except Exception:
        return 0.0
