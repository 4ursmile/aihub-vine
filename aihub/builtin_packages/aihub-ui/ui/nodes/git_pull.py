#!/usr/bin/env python3
"""Pull from git: clone or update a repository into a persistent folder and hand the PATH on, never the content. The next node (Claude, Free
chat, Publish to Space ...) opens the files itself, so big data (parquet, csv, images) never goes through a prompt. Uses the git logins saved
in the aihub Vault. JSON on stdin, JSON on stdout."""
import json
import os
import re
import subprocess
import sys
import time
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
subs = [s.strip().strip("/") for s in str(cfg.get("paths") or "").splitlines() if s.strip()]
for s in subs:
    if s.startswith("/") or ".." in s.split("/") or "\0" in s:
        done(ok=False, error="a path must stay inside the repository")

home = os.environ.get("AIHUB_HOME") or os.path.join(os.path.expanduser("~"), ".aihub")
slug = re.sub(r"[^A-Za-z0-9._-]+", "_", (u.netloc + u.path).lower().removesuffix(".git")).strip("_")[:80]
if ref:
    slug += "@" + re.sub(r"[^A-Za-z0-9._-]+", "_", ref)[:30]
dest = os.path.join(home, "workingspace", "data", "git", slug)       # workingspace is readable by the data nodes; everything else under ~/.aihub is not
env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_ASKPASS="true", SSH_ASKPASS="true")
base = ["git", "-c", "core.symlinks=false", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "core.hooksPath=/dev/null",
        "-c", "credential.interactive=never"]
try:
    _login_args, _login_env = F.git_login(cfg, u.netloc)               # node-level / credential-set login overrides the Vault for this call only
except ValueError as e:
    done(ok=False, error=str(e))
base += _login_args
env.update(_login_env)


DEADLINE = time.time() + 270                                          # clone + fetch + LFS share one budget, below the node limit


def git(args, cwd=None, timeout=None):
    left = DEADLINE - time.time()
    if left < 3:
        raise subprocess.TimeoutExpired(args, 0)
    return subprocess.run(base + args, cwd=cwd, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=min(timeout or 270, left))


def explain(err):
    err = F.scrub(err, cfg)
    if re.search(r"authentication failed|could not read (username|password)|403|401|invalid credentials|terminal prompts disabled", err, re.I):
        return "git asked for a login for %s and the Vault has none that works. Add it in Vault (or: aihub vault add https://%s --user NAME --token-stdin). Git said: %s" % (u.netloc, u.netloc, err.strip()[-200:])
    if re.search(r"not found|does not exist|404", err, re.I):
        return "repository, branch or tag not found (or your login cannot see it): " + err.strip()[-200:]
    return "git failed: " + err.strip()[-300:]


os.makedirs(os.path.dirname(dest), exist_ok=True)
lockf = dest + ".lock"
lock_fd = None
while time.time() < DEADLINE:                                         # one fetch per folder at a time (two runs of the same repo)
    try:
        lock_fd = os.open(lockf, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        break
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(lockf) > 300:
                os.remove(lockf)
                continue
        except OSError:
            pass
        time.sleep(1)
    except OSError:
        break
if lock_fd is None and time.time() >= DEADLINE:
    done(ok=False, error="another run is still updating this repository")
try:
    if os.path.isdir(os.path.join(dest, ".git")):
        what = "updated"
        r = git(["fetch", "-q", "--depth", "1", "origin", ref or "HEAD"], cwd=dest)
        if r.returncode == 0:
            r = git(["reset", "-q", "--hard", "FETCH_HEAD"], cwd=dest)
    else:
        what = "cloned"
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        sha = bool(re.match(r"^[0-9a-f]{7,40}$", ref))
        if sha:
            os.makedirs(dest, exist_ok=True)
            git(["init", "-q", dest])
            git(["remote", "add", "origin", url], cwd=dest)
            r = git(["fetch", "-q", "--depth", "1", "origin", ref], cwd=dest)
            if r.returncode == 0:
                r = git(["checkout", "-q", "FETCH_HEAD"], cwd=dest)
        else:
            r = git(["clone", "-q", "--depth", "1", "--no-tags", "--single-branch"] + (["--branch", ref] if ref else []) + ["--", url, dest])
    if r.returncode != 0:
        done(ok=False, error=explain(r.stderr or r.stdout))
    attrs = os.path.join(dest, ".gitattributes")
    if os.path.isfile(attrs) and "filter=lfs" in open(attrs, encoding="utf-8", errors="replace").read():
        l = git(["lfs", "pull"], cwd=dest)                  # real files, not pointer stubs
        if l.returncode != 0:
            done(ok=False, error="the repository uses Git LFS and `git lfs pull` failed (is git-lfs installed?): " + (l.stderr or l.stdout).strip()[-200:])
except subprocess.TimeoutExpired:
    done(ok=False, error="git timed out (is the host reachable?)")
except OSError as e:
    done(ok=False, error="cannot run git: %s" % e)
finally:
    if lock_fd is not None:
        os.close(lock_fd)
        try:
            os.remove(lockf)
        except OSError:
            pass

root = os.path.realpath(dest)
targets = []
for s in subs or [""]:
    t = os.path.realpath(os.path.join(root, s)) if s else root
    if t != root and not t.startswith(root + os.sep):
        done(ok=False, error="the path leads outside the repository")
    if not os.path.exists(t):
        done(ok=False, error="no %r in %s" % (s, u.netloc + u.path))
    targets.append(t)
sha_now = git(["rev-parse", "--short", "HEAD"], cwd=dest).stdout.strip()
count = 0
for t in targets:
    if os.path.isfile(t):
        count += 1
    else:
        for _r, ds, fs in os.walk(t):
            ds[:] = [x for x in ds if x != ".git"]
            count += len(fs)
data = {"path": targets[0], "paths": targets, "root": root, "repo": u.netloc + u.path, "ref": ref or "default", "commit": sha_now, "files": count}
done(ok=True, output="\n".join(targets), data=data,
     log="%s %s@%s: %d file(s) at %s (pointer only, no content read)" % (what, u.netloc + u.path, sha_now, count, root))
