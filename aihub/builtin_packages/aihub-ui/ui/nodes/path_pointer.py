#!/usr/bin/env python3
"""File / folder pointer: checks that a local path exists and passes the PATH on (never the content). Several paths, one per line.
JSON on stdin, JSON on stdout."""
import glob
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


found = []
for line in str(cfg.get("path") or "").splitlines():
    line = line.strip()
    if not line:
        continue
    p = os.path.expanduser(line)
    if not os.path.isabs(p):
        p = os.path.join(wd, p)
    hits = sorted(glob.glob(p)) if any(c in p for c in "*?[") else [p]
    for h in hits:
        h = os.path.realpath(h)
        why = F.blocked(h)
        if why:
            done(ok=False, error="refused: " + why)
        if os.path.exists(h) and h not in found:
            found.append(h)
if not found and cfg.get("must_exist", True):
    done(ok=False, error="nothing found at: " + str(cfg.get("path") or "").strip()[:200])
files = 0
for h in found:
    if os.path.isfile(h):
        files += 1
    else:
        for _r, ds, fs in os.walk(h):
            ds[:] = [x for x in ds if x != ".git"]
            files += len(fs)
done(ok=True, output="\n".join(found), data={"path": found[0] if found else "", "paths": found, "files": files},
     log="%d pointer(s), %d file(s) (content not read)" % (len(found), files))
