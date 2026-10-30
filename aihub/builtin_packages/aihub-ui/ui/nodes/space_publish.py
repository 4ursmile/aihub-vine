#!/usr/bin/env python3
"""Publish to Space: sends a file or folder (the PATH an earlier node produced) to the hub as a Space (document, slides, report, PDF, sheet,
dashboard, HTML page) through `aihub space publish`. Same name again = a new revision of that space. JSON on stdin, JSON on stdout."""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile

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
name = str(cfg.get("name") or "").strip().lower()
if name and not re.match(r"^[a-z0-9][a-z0-9._-]{0,63}$", name):
    done(ok=False, error="space name: lower-case letters, digits and . _ - (max 64)")
argv = [sys.executable, "-m", "aihub.cli"]
spacebundle = None
try:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from aihubui import aihub as A
    from aihubui import spacebundle
    argv = A.command()
except Exception:
    pass
TIMEOUT = 600
orig = path
tmp = tempfile.mkdtemp(prefix="aihub-publish-")
try:
    # Same staging as Share to Space in a chat: a page is bundled into one self-contained file, a folder is copied without .git /
    # node_modules / dot files. (Publishing the raw folder made the CLI pack and upload everything in it, which looked like a hang.)
    root = wd if path.startswith(os.path.realpath(wd) + os.sep) else (path if os.path.isdir(path) else os.path.dirname(path))
    if spacebundle:
        try:
            path, used, skipped = spacebundle.stage(path, root, tmp)
        except ValueError as e:
            done(ok=False, error=str(e))
        except OSError as e:
            done(ok=False, error="cannot prepare %s for publishing: %s" % (path, e))
        if used:
            print("bundled %d local file(s) into the page" % len(used), file=sys.stderr)
    size = 0
    if os.path.isdir(path):
        for dp, _, names in os.walk(path):
            for n in names:
                try:
                    size += os.path.getsize(os.path.join(dp, n))
                except OSError:
                    pass
    if size > 300 * 1024 * 1024:
        done(ok=False, error="%d MB is too large for a Space (limit about 300 MB): publish a smaller folder" % (size // 1024 // 1024))
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
    # stdin is /dev/null (a CLI waiting for input can never hang), stdout/stderr go to FILES (no pipe that a background helper
    # started by the CLI could keep open), no auto-update tail, and the whole process group is killed on timeout.
    out_f, err_f = os.path.join(tmp, "out.txt"), os.path.join(tmp, "err.txt")
    print("running: %s" % " ".join(argv + args), file=sys.stderr)
    try:
        with open(out_f, "wb") as fo, open(err_f, "wb") as fe:
            kw = {"start_new_session": True} if os.name != "nt" else {}
            proc = subprocess.Popen(argv + args, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe, env=dict(os.environ, AIHUB_JSON="1", AIHUB_NO_UPDATE="1"), **kw)
            try:
                rc = proc.wait(timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                try:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=10)
                    else:
                        os.killpg(proc.pid, signal.SIGKILL)
                except (OSError, subprocess.SubprocessError):
                    proc.kill()
                done(ok=False, error="publishing took longer than %d s and was stopped (%s)" % (TIMEOUT, "staged %d MB" % (size // 1024 // 1024) if size else "single file"))
    except OSError as e:
        done(ok=False, error="cannot run the aihub CLI: %s" % e)
    rd = lambda f: open(f, "rb").read().decode("utf-8", "replace")
    stdout, stderr = rd(out_f), rd(err_f)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
for ln in stderr.strip().splitlines()[-20:]:
    print(ln, file=sys.stderr)
try:
    doc = json.loads(stdout)
except ValueError:
    doc = {}
if rc != 0 or doc.get("ok") is False:
    done(ok=False, error=str(doc.get("error") or (stderr or stdout).strip()[-400:] or "aihub space publish failed"))
res = doc.get("data") or {}
sp = res.get("space") or {}
url = res.get("url") or ""
done(ok=True, output="%s space %s\n%s" % ("Published" if res.get("created") else "Updated", sp.get("ref", ""), url),
     data={"space": sp.get("ref", ""), "url": url, "created": bool(res.get("created")), "rev": sp.get("rev"), "visibility": sp.get("visibility", "")},
     log="path %s (from %s)\n%s %s (%s, rev %s)" % (orig, via, "published" if res.get("created") else "updated", sp.get("ref", ""), sp.get("kind", ""), sp.get("rev", "")))
