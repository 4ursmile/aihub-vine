#!/usr/bin/env python3
"""Read file: a local file (text) or a folder (listing, optionally the text of its first files). JSON on stdin, JSON on stdout."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fsread as F  # noqa: E402

d = json.load(sys.stdin)
cfg = d["config"]
wd = d.get("workdir") or os.path.expanduser("~")


def done(**k):
    print(json.dumps(k, ensure_ascii=False))
    sys.exit(0)


raw = str(cfg.get("path") or "").strip()
if not raw:
    done(ok=False, error="give a file or folder path")
path = os.path.expanduser(raw)
if not os.path.isabs(path):
    path = os.path.join(wd, path)                       # relative paths start in the run's working folder
path = os.path.realpath(path)
why = F.blocked(path)
if why:
    done(ok=False, error="refused: " + why)
try:
    limit = min(max(int(cfg.get("max_chars") or 20000), 200), 400000)
except ValueError:
    limit = 20000
try:
    if os.path.isdir(path):
        out = F.listing(path, "", bool(cfg.get("read_files")), limit)
    elif os.path.isfile(path):
        out = F.read_text(path, limit)
    else:
        done(ok=False, error="no such file or folder: " + path)
except (ValueError, OSError) as e:
    done(ok=False, error=str(e))
data = {"path": path, "chars": len(out)}
if cfg.get("parse") == "json":
    j = F.as_json(out)
    if j is None:
        done(ok=False, error="the file is not valid JSON")
    out = json.dumps(j, indent=2, ensure_ascii=False)
    data["json"] = j
done(ok=True, output=out, data=data, log="read %s (%d characters)" % (path, len(out)))
