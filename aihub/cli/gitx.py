"""Thin git wrapper used for index/package fetch and publish."""
import os
import re
import subprocess

from . import paths, tls


class GitError(Exception):
    pass


def run(args, cwd=None, check=True):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", **tls.git_env())
    r = subprocess.run(["git"] + list(args), cwd=cwd, env=env, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise GitError((r.stderr or r.stdout).strip() or "git %s failed" % args[0])
    return r.stdout.strip()


def redact_url(url):
    return re.sub(r"//[^/@]+@", "//", url or "")


def cache_dir(url, base=None):
    key = re.sub(r"[^A-Za-z0-9]+", "_", redact_url(url)).strip("_")[-80:]
    if base:
        d = os.path.join(base, key)
        os.makedirs(d, exist_ok=True)
        return d
    return paths.ensure("repos", key)


def fetch(url, branch="main", ref=None, base=None):
    """Clone or update a cached checkout and leave it at `ref` (tag, branch or commit) or the branch tip.
    A ref that cannot be resolved is an error: a pinned install must never fall back to something else."""
    d = cache_dir(url, base)
    if not os.path.isdir(os.path.join(d, ".git")):
        run(["init", "-q"], cwd=d)
        run(["remote", "add", "origin", url], cwd=d)
    else:
        run(["remote", "set-url", "origin", url], cwd=d)
    want = ref or branch
    try:
        run(["fetch", "-q", "--depth", "1", "origin", want], cwd=d)           # branch, tag, or (if the host allows) a commit
    except GitError:
        try:
            run(["fetch", "-q", "--tags", "origin", branch], cwd=d)             # full history of the branch, then resolve locally
            run(["rev-parse", "--verify", "-q", want + "^{commit}"], cwd=d)
            run(["checkout", "-q", "--force", want], cwd=d)
            return d
        except GitError:
            raise GitError("cannot find '%s' in %s" % (want, redact_url(url)))
    run(["checkout", "-q", "--force", "FETCH_HEAD"], cwd=d)
    return d


def remote_info(cwd="."):
    """(origin url, branch) of a working repo, or (None, None)."""
    url = run(["remote", "get-url", "origin"], cwd=cwd, check=False) or None
    branch = run(["symbolic-ref", "--short", "-q", "HEAD"], cwd=cwd, check=False) or None
    return url, branch


def is_repo(cwd="."):
    return run(["rev-parse", "--is-inside-work-tree"], cwd=cwd, check=False) == "true"


def remote_head(url, branch):
    out = run(["ls-remote", url, "refs/heads/" + branch], check=False)
    return out.split()[0] if out else None


def remote_empty(url):
    """True when the remote is reachable but has no commits at all (a freshly created repo). An unreachable or
    unauthorised remote is NOT empty: that stays an error."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", **tls.git_env())
    r = subprocess.run(["git", "ls-remote", url], env=env, capture_output=True, text=True)
    return r.returncode == 0 and not r.stdout.strip()


def large_files(cwd, limit_mb=50):
    out, big = run(["ls-files", "-co", "--exclude-standard"], cwd=cwd), []
    for rel in out.splitlines():
        try:
            if os.path.getsize(os.path.join(cwd, rel)) > limit_mb * 1048576:
                big.append(rel)
        except OSError:
            pass
    return big
