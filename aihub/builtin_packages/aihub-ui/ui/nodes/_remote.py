"""Shared by the remote fetch nodes (Jupyter, SSH): where a fetched copy lives and how it replaces the last one."""
import json
import os
import re
import shutil
import sys
import time

HOME = os.environ.get("AIHUB_HOME") or os.path.join(os.path.expanduser("~"), ".aihub")


def done(**k):
    sys.stdout.write("\n" + json.dumps(k, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()
    sys.exit(0)


def slug(s, n=80):
    return re.sub(r"[^A-Za-z0-9._@-]+", "_", str(s)).strip("_.")[:n] or "x"


def dest_for(kind, where, rpath):
    """~/.aihub/workingspace/data/<kind>/<where>/<path as folders>: the data nodes may read workingspace, nothing else under ~/.aihub."""
    parts = [slug(p, 60) for p in re.split(r"[\\/]+", str(rpath)) if p and p not in (".", "..", "~")]
    base = os.path.join(HOME, "workingspace", "data", kind, slug(where))
    return os.path.join(base, *parts) if parts else os.path.join(base, "_root")


def stage(dest):
    """A fresh folder next to DEST to download into; swap() then replaces DEST in one move (removed remote files disappear too)."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = "%s.part-%d-%d" % (dest, os.getpid(), int(time.time() * 1000))
    os.makedirs(tmp)
    return tmp


def swap(tmp, dest):
    old = None
    if os.path.lexists(dest):
        old = "%s.old-%d" % (dest, os.getpid())
        os.replace(dest, old)
    os.replace(tmp, dest)
    if old:
        if os.path.isdir(old) and not os.path.islink(old):
            shutil.rmtree(old, ignore_errors=True)
        else:
            os.remove(old)


def drop(tmp):
    shutil.rmtree(tmp, ignore_errors=True)


def count(path):
    if os.path.isfile(path):
        return 1, os.path.getsize(path)
    n = size = 0
    for r, _ds, fs in os.walk(path):
        for f in fs:
            n += 1
            try:
                size += os.path.getsize(os.path.join(r, f))
            except OSError:
                pass
    return n, size


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return ("%d %s" % (n, u)) if u == "B" else ("%.1f %s" % (n, u))
        n /= 1024.0


def number(cfg, key, default, lo, hi):
    try:
        return min(max(float(cfg.get(key) or default), lo), hi)
    except (TypeError, ValueError):
        return default
