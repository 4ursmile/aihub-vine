#!/usr/bin/env python3
"""Publish to Space: sends a file or folder (the PATH an earlier node produced) to the hub as a Space (document, slides, report, PDF, sheet,
dashboard, HTML page) through `aihub space publish`. Same name again = a new revision of that space. JSON on stdin, JSON on stdout."""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fsread as F  # noqa: E402

d = json.load(sys.stdin)
cfg = d["config"]
wd = d.get("workdir") or os.path.expanduser("~")


def done(**k):
    print(json.dumps(k, ensure_ascii=False))
    sys.exit(0)


via = "the File field"
path = F.pointer(cfg.get("path"), wd)
if not path:
    via = "the last line of the input that names an existing path"
    path = F.pointer(d.get("input"), wd)
if not path:
    done(ok=False, error="no existing file or folder to publish: set 'File or folder' or let the previous node end with its path (OUTPUT: /full/path)")
why = F.blocked(path)
if why:
    done(ok=False, error="refused: " + why)
if os.path.isdir(path) and not os.path.isfile(os.path.join(path, "index.html")):
    done(ok=False, error="a folder is published as a web page and needs an index.html at its top: " + path)
name = str(cfg.get("name") or "").strip().lower()
if name and not re.match(r"^[a-z0-9][a-z0-9._-]{0,63}$", name):
    done(ok=False, error="space name: lower-case letters, digits and . _ - (max 64)")
argv = [sys.executable, "-m", "aihub.cli"]
try:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from aihubui import aihub as A
    argv = A.command()
except Exception:
    pass
args = ["--json", "space", "publish", path] + ([name] if name else [])
kind = str(cfg.get("kind") or "auto")
if kind in ("doc", "slides", "report", "pdf", "sheet", "dashboard", "html"):
    args += ["--kind", kind]
title = " ".join(str(cfg.get("title") or "").split())[:200]
if title:
    args += ["--title", title]
desc = " ".join(str(cfg.get("description") or "").split())[:500]
if desc:
    args += ["--description", desc]
args.append("--public" if cfg.get("visibility") == "public" else "--private")          # private unless asked
try:
    p = subprocess.run(argv + args, capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL)
except subprocess.TimeoutExpired:
    done(ok=False, error="publishing timed out")
except OSError as e:
    done(ok=False, error="cannot run the aihub CLI: %s" % e)
try:
    doc = json.loads(p.stdout)
except ValueError:
    doc = {}
if p.returncode != 0 or doc.get("ok") is False:
    done(ok=False, error=str(doc.get("error") or (p.stderr or p.stdout).strip()[-400:] or "aihub space publish failed"))
res = doc.get("data") or {}
sp = res.get("space") or {}
url = res.get("url") or ""
done(ok=True, output="%s space %s\n%s" % ("Published" if res.get("created") else "Updated", sp.get("ref", ""), url),
     data={"space": sp.get("ref", ""), "url": url, "created": bool(res.get("created")), "rev": sp.get("rev"), "visibility": sp.get("visibility", "")},
     log="path %s (from %s)\n%s %s (%s, rev %s)" % (path, via, "published" if res.get("created") else "updated", sp.get("ref", ""), sp.get("kind", ""), sp.get("rev", "")))
