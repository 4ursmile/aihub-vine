"""Thin git wrapper used for index/package fetch and publish."""
import os
import re
import subprocess

from . import paths, tls


class GitError(Exception):
    pass


BATCH = False           # set by the server: no person is there to answer a prompt, so git must fail fast instead of waiting forever
NETWORK = ("fetch", "push", "ls-remote", "clone", "pull")
NETWORK_TIMEOUT = 300   # seconds; in BATCH mode a network command never runs without a limit


def _env(extra_env=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", **tls.git_env())
    env.setdefault("GIT_HTTP_LOW_SPEED_LIMIT", "1000")             # a stalled https transfer (proxy, dead link) aborts instead of hanging
    env.setdefault("GIT_HTTP_LOW_SPEED_TIME", "60")
    if BATCH:
        env.setdefault("GCM_INTERACTIVE", "never")                 # Git Credential Manager: no sign-in window
        env.setdefault("GIT_ASKPASS", "echo")                      # any askpass prompt gets an empty answer -> auth fails with a message
        env.setdefault("SSH_ASKPASS", "echo")
        env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=20")
    if extra_env:
        env.update(extra_env)
    return env


def run(args, cwd=None, check=True, timeout=None, input=None, extra_env=None):
    env = _env(extra_env)
    if timeout is None and BATCH and args and args[0] in NETWORK:
        timeout = NETWORK_TIMEOUT
    try:
        r = subprocess.run(["git"] + list(args), cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, input=input)
    except subprocess.TimeoutExpired:
        if check:
            raise GitError("git %s timed out after %ss" % (args[0], timeout))
        return ""
    if check and r.returncode != 0:
        raise GitError((r.stderr or r.stdout).strip() or "git %s failed" % args[0])
    return r.stdout.strip()


MISSING = ("couldn't find remote ref", "could not find remote ref")


def fetch_tip(cwd, remote, branch, ref, depth=None, timeout=120):
    """Fetch the tip of `branch` into the local `ref` -> its commit, or "" when the remote has no such branch (or no commits at all).
    One network round trip: no `ls-remote` first. Any other failure (offline, no access) raises."""
    args = ["fetch", "-q", "--no-tags"] + (["--depth", str(depth)] if depth else []) + [remote, "+refs/heads/%s:%s" % (branch, ref)]
    try:
        run(args, cwd=cwd, timeout=timeout)
    except GitError as e:
        if any(w in str(e).lower() for w in MISSING):
            run(["update-ref", "-d", ref], cwd=cwd, check=False)
            return ""
        raise
    return run(["rev-parse", ref], cwd=cwd)


def _batch(cwd, mode, names, timeout):
    r = subprocess.run(["git", "cat-file", mode], cwd=cwd, env=_env(), capture_output=True, timeout=timeout,
                       input="".join(n + "\n" for n in names).encode())
    if r.returncode != 0:
        raise GitError(r.stderr.decode("utf-8", "replace").strip() or "git cat-file failed")
    return r.stdout


def objects(cwd, names, timeout=120):
    """{name: (sha, type, size)} for many objects (`<sha>`, `<rev>:<path>`) in ONE git process. Missing ones are left out."""
    names = list(dict.fromkeys(n for n in names if n))
    if not names:
        return {}
    out = {}
    for name, line in zip(names, _batch(cwd, "--batch-check", names, timeout).decode("utf-8", "replace").splitlines()):
        parts = line.split()
        if len(parts) == 3 and parts[2].isdigit():
            out[name] = (parts[0], parts[1], int(parts[2]))
    return out


def blobs(cwd, shas, timeout=300):
    """{sha: text} for many blobs in ONE git process (`cat-file --batch`) instead of one process per blob."""
    shas = list(dict.fromkeys(s for s in shas if s))
    if not shas:
        return {}
    raw, out, pos = _batch(cwd, "--batch", shas, timeout), {}, 0
    while pos < len(raw):
        end = raw.index(b"\n", pos)
        parts = raw[pos:end].split()
        pos = end + 1
        if len(parts) != 3:                                               # "<name> missing"
            continue
        size = int(parts[2])
        out[parts[0].decode()] = raw[pos:pos + size].decode("utf-8", "replace")
        pos += size + 1
    return out


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


def remote_head(url, branch, timeout=60):
    out = run(["ls-remote", url, "refs/heads/" + branch], check=False, timeout=timeout)
    return out.split()[0] if out else None


def remote_empty(url):
    """True when the remote is reachable but has no commits at all (a freshly created repo). An unreachable or
    unauthorised remote is NOT empty: that stays an error."""
    try:
        r = subprocess.run(["git", "ls-remote", url], env=_env(), capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0 and not r.stdout.strip()


def reachable(url):
    """True when `git ls-remote` succeeds (the repo exists and we may read it), whether or not it has any branch."""
    try:
        return subprocess.run(["git", "ls-remote", url], env=_env(), capture_output=True, text=True, timeout=60).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def large_files(cwd, limit_mb=50):
    out, big = run(["ls-files", "-co", "--exclude-standard"], cwd=cwd), []
    for rel in out.splitlines():
        try:
            if os.path.getsize(os.path.join(cwd, rel)) > limit_mb * 1048576:
                big.append(rel)
        except OSError:
            pass
    return big
