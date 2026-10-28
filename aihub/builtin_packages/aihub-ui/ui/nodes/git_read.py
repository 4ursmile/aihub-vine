#!/usr/bin/env python3
"""Read from git: a file or folder of a remote repository, using the git logins you saved in the aihub Vault (aihub's credential helper
answers git's login question, exactly as in your terminal). Shallow clone into a temp folder, read, delete. JSON on stdin, JSON on stdout."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fsread as F  # noqa: E402

d = json.load(sys.stdin)
cfg = d["config"]


def done(**k):
    print(json.dumps(k, ensure_ascii=False))
    sys.exit(0)


url = str(cfg.get("url") or "").strip()
u = urllib.parse.urlsplit(url)
if u.scheme != "https" or not u.netloc or u.username or u.password or any(c in url for c in " \t\r\n\\#?"):
    done(ok=False, error="the repository URL must be a plain https:// address (logins come from the Vault, not from the URL)")
ref = str(cfg.get("ref") or "").strip()
if ref and (not re.match(r"^[A-Za-z0-9._/@+-]{1,100}$", ref) or ref.startswith("-") or ".." in ref):
    done(ok=False, error="bad branch, tag or commit")
sub = str(cfg.get("path") or "").strip().strip("/")
if sub.startswith("/") or ".." in sub.split("/") or "\0" in sub:
    done(ok=False, error="the path must stay inside the repository")
try:
    limit = min(max(int(cfg.get("max_chars") or 20000), 200), 400000)
except ValueError:
    limit = 20000
env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_ASKPASS="true", SSH_ASKPASS="true", GIT_CONFIG_NOSYSTEM="0")
base = ["git", "-c", "core.symlinks=false", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "core.hooksPath=/dev/null", "-c", "credential.interactive=never"]


def git(args, cwd=None, timeout=240):
    return subprocess.run(base + args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)


def explain(err):
    host = u.netloc
    if re.search(r"authentication failed|could not read (username|password)|403|401|invalid credentials|terminal prompts disabled", err, re.I):
        return "git asked for a login for %s and the Vault has none that works. Add it in Vault (or: aihub vault add https://%s --user NAME --token-stdin). Git said: %s" % (host, host, err.strip()[-200:])
    if re.search(r"not found|does not exist|404", err, re.I):
        return "repository, branch or tag not found (or your login cannot see it): " + err.strip()[-200:]
    return "git failed: " + err.strip()[-300:]


tmp = tempfile.mkdtemp(prefix="aihub-gitread-")
try:
    dest = os.path.join(tmp, "repo")
    sha = bool(re.match(r"^[0-9a-f]{7,40}$", ref))
    try:
        if sha:
            git(["init", "-q", dest])
            git(["remote", "add", "origin", url], cwd=dest)
            r = git(["fetch", "-q", "--depth", "1", "origin", ref], cwd=dest)
            if r.returncode == 0:
                r = git(["checkout", "-q", "FETCH_HEAD"], cwd=dest)
        else:
            r = git(["clone", "-q", "--depth", "1", "--no-tags", "--single-branch"] + (["--branch", ref] if ref else []) + ["--", url, dest])
    except subprocess.TimeoutExpired:
        done(ok=False, error="git timed out (is the host reachable?)")
    except OSError as e:
        done(ok=False, error="cannot run git: %s" % e)
    if r.returncode != 0:
        done(ok=False, error=explain(r.stderr or r.stdout))
    root = os.path.realpath(dest)
    target = os.path.realpath(os.path.join(root, sub)) if sub else root
    if target != root and not target.startswith(root + os.sep):
        done(ok=False, error="the path leads outside the repository")
    if not os.path.exists(target):
        done(ok=False, error="no %r in %s at %s" % (sub, u.netloc + u.path, ref or "the default branch"))
    why = F.blocked(target) if sub else ""
    if why:
        done(ok=False, error="refused: " + why)
    sha_now = git(["rev-parse", "--short", "HEAD"], cwd=dest).stdout.strip()
    try:
        if os.path.isdir(target):
            out = F.listing(target, sub, bool(cfg.get("read_files")), limit)
            kind = "folder"
        else:
            out = F.read_text(target, limit)
            kind = "file"
    except (ValueError, OSError) as e:
        done(ok=False, error=str(e))
    data = {"repo": u.netloc + u.path, "ref": ref or "default", "commit": sha_now, "path": sub, "kind": kind}
    if kind == "file" and cfg.get("parse") == "json":
        j = F.as_json(out)
        if j is None:
            done(ok=False, error="the file is not valid JSON")
        data["json"] = j
        out = json.dumps(j, indent=2, ensure_ascii=False)
    done(ok=True, output=out, data=data, log="%s %s of %s@%s (%d characters)" % (kind, sub or "/", u.netloc + u.path, sha_now, len(out)))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
