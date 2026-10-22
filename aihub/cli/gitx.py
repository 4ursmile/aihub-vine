"""Thin git wrapper used for index/package fetch and publish."""
import os
import re
import subprocess

from ..core import textsafe
from . import paths, tls


class GitError(Exception):
    pass


BATCH = False           # set by the server: no person is there to answer a prompt, so git must fail fast instead of waiting forever
NETWORK = ("fetch", "push", "ls-remote", "clone", "pull")
NETWORK_TIMEOUT = 300   # seconds; in BATCH mode a network command never runs without a limit


LOGIN = None            # server: fn(host) -> (user, secret) or None, set by Sync.start; consulted for git calls that need a login
TRUSTED = []            # server: folders git may open although another account owns them (see trust())


def trust(*dirs):
    """Tell every later git call that these folders are safe (git's `safe.directory`). A hub that runs as another account than the one that
    created its data folder (a container, a service user, a restored volume) otherwise fails with "detected dubious ownership in
    repository", which a cached checkout then mistook for a broken one. The folders are the server's own data; nothing user supplied.
    "*" would trust every path, so only the named folders are given."""
    for d in dirs:
        d = os.path.abspath(os.path.realpath(d)) if d else ""
        if d and d not in TRUSTED:
            TRUSTED.append(d)


def _host_of(url):
    """(scheme, 'host[:port]') of an http(s) URL the way git connects to it, or None. Stricter than a regex on purpose: the stored login is
    keyed by this value, so it must be the host git will really talk to. A URL with a fragment, a query, a backslash, whitespace or control
    characters before the path is refused ("http://evil:80#@good/x" would otherwise read as host "good" while git connects to evil)."""
    import urllib.parse
    if not isinstance(url, str) or re.search(r"[\s\\\x00-\x1f\x7f]", url):
        return None
    try:
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname or u.fragment or u.query or "#" in url or "?" in url.split("//", 1)[-1].split("/", 1)[0]:
            return None
        name = u.hostname.lower()
        name = "[%s]" % name if ":" in name else name                       # IPv6 literal: git sends (and we key by) the bracketed form
        port = u.port                                                        # kept exactly as written: git sends host= with the port only when the URL has it
    except ValueError:
        return None
    return u.scheme, name + (":%d" % port if port else "")


def _count(env):
    """GIT_CONFIG_COUNT as a non-negative int (git itself rejects anything else, so a garbage value counts as 0 and is overwritten)."""
    try:
        return max(0, int(env.get("GIT_CONFIG_COUNT") or 0))
    except (TypeError, ValueError):
        return 0


def _hosts_in(args):
    """[(scheme, host[:port])] of the remote URLs named in a git command line."""
    out = []
    for a in args or []:
        h = _host_of(a)
        if h and h not in out:
            out.append(h)
    return out


# Inline credential helper. Git passes the REAL destination (protocol=, host=) on the helper's stdin, so the secret is only ever printed for
# the one host it was issued for: a redirect, a `.lfsconfig` endpoint or a look-alike URL asks for another host and gets nothing.
# printf, not echo: dash expands backslashes in echo and would corrupt or split a secret.
_HELPER = ('!f() { test "$1" = get || exit 0; p=; h=; while IFS== read -r k v; do case "$k" in protocol) p=$v;; host) h=$v;; esac; done; '
           'h=$(printf %s "$h" | tr A-Z a-z); [ "$p" = "$AIHUB_GIT_PROTO" ] && [ "$h" = "$AIHUB_GIT_HOST" ] || exit 0; '
           "printf 'username=%s\\npassword=%s\\n' \"$AIHUB_GIT_USER\" \"$AIHUB_GIT_SECRET\"; }; f")


def _login_env(env, cwd, args):
    """Give git the stored login for the host this call talks to, without touching disk or git config and without the secret appearing in a
    command line: user and secret go in the environment of this one child, and the inline helper (see _HELPER) prints them only for exactly
    the scheme and host[:port] they were issued for. Only when LOGIN is set (the server), the call can reach a network and the URL has no
    login of its own. A redirect is followed for the first request only (git's own default), and the helper still refuses any other host. https only, unless AIHUB_GIT_ALLOW_HTTP=1."""
    if LOGIN is None or not args or args[0] not in NETWORK + ("lfs",):
        return env
    hosts = _hosts_in(args)
    if not hosts and cwd:
        try:
            url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=cwd, env=_trusted_env(dict(os.environ), cwd), capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=20, **popen_kw()).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return env
        hosts = _hosts_in([url])
    for scheme, host in hosts:
        if scheme != "https" and not os.environ.get("AIHUB_GIT_ALLOW_HTTP"):
            continue                                                   # a token is never offered over plain http by default
        try:
            got = LOGIN(host)
        except Exception:
            got = None
        if got and got[0] and got[1]:
            n = _count(env)
            pairs = [("credential.helper", ""), ("credential.helper", _HELPER), ("http.followRedirects", "initial"),
                     ("http.proxy", os.environ.get("AIHUB_GIT_PROXY", "")), ("http.extraHeader", "")]       # a repo-local proxy/header cannot see the login
            for i, (k, v) in enumerate(pairs):
                env["GIT_CONFIG_KEY_%d" % (n + i)], env["GIT_CONFIG_VALUE_%d" % (n + i)] = k, v
            env["GIT_CONFIG_COUNT"] = str(n + len(pairs))
            env["AIHUB_GIT_PROTO"], env["AIHUB_GIT_HOST"] = scheme, host
            env["AIHUB_GIT_USER"], env["AIHUB_GIT_SECRET"] = got
            _warn_unverified(scheme, host)
            break
    return env


_warned_tls = set()


def _warn_unverified(scheme, host):
    """The login is about to travel over a connection whose certificate is NOT checked (insecure_tls is the shipped default): a machine in the
    path could take it. Never refuse (many hubs sit behind a private CA and would lose git access), but say it once per host and name the fix."""
    if scheme == "https" and host not in _warned_tls and tls.insecure():
        _warned_tls.add(host)
        import logging
        logging.getLogger("aihub").warning("git login for %s is sent over TLS with certificate checks OFF; set AIHUB_CA_BUNDLE=<ca.pem> "
                                           "(or `aihub config set insecure_tls false`) so the server verifies the git host", host)


def _trusted_env(env, cwd):
    """Environment entries that add safe.directory for the trusted folders and for the cwd of this call when it lies inside one of them
    (checkouts are created below the data folder, so each is covered by its parent). Uses GIT_CONFIG_COUNT (git >= 2.31), which git
    still honours for safe.directory where a plain `-c` is ignored by some versions."""
    if not TRUSTED:
        return env
    here = os.path.abspath(os.path.realpath(cwd)) if cwd else ""
    keep = [d for d in TRUSTED if here == d or here.startswith(d + os.sep)]
    vals = list(dict.fromkeys(([here] if keep and here else []) + [os.path.join(d, "*") for d in keep] + keep))
    if not vals:
        return env
    n = _count(env)
    for v in vals:
        env["GIT_CONFIG_KEY_%d" % n] = "safe.directory"
        env["GIT_CONFIG_VALUE_%d" % n] = v
        n += 1
    env["GIT_CONFIG_COUNT"] = str(n)
    return env


NO_WINDOW = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW: on Windows every git call otherwise flashes a console window


def popen_kw():
    """Extra subprocess arguments so a child never opens a console window on Windows (empty elsewhere)."""
    return {"creationflags": NO_WINDOW} if NO_WINDOW else {}


def _env(extra_env=None, cwd=None):
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
    return _trusted_env(env, cwd)


def run(args, cwd=None, check=True, timeout=None, input=None, extra_env=None):
    env = _login_env(_env(extra_env, cwd), cwd, list(args))
    if timeout is None and BATCH and args and args[0] in NETWORK:
        timeout = NETWORK_TIMEOUT
    if isinstance(input, str):
        input = textsafe.clean(input)              # a path or message with a non-UTF-8 byte must not fail the encode of git's stdin
    try:
        r = subprocess.run(["git"] + list(args), cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, input=input, **popen_kw())
    except subprocess.TimeoutExpired:
        if check:
            raise GitError("git %s timed out after %ss" % (args[0], timeout))
        return ""
    if check and r.returncode != 0:
        raise GitError(explain((r.stderr or r.stdout).strip() or "git %s failed" % args[0]))
    return r.stdout.strip()


def explain(msg):
    """Add the one-line fix to the git errors people cannot guess from git's own words."""
    if "dubious ownership" in msg and "aihub hint" not in msg:
        m = re.search(r"repository at '([^']+)'", msg)
        msg += ("\n[aihub hint] the folder belongs to another account than the one running git. The hub trusts its own data folder automatically; "
                "for any other: git config --global --add safe.directory %s" % (m.group(1) if m else "<folder>"))
    return msg


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
    r = subprocess.run(["git", "cat-file", mode], cwd=cwd, env=_env(None, cwd), capture_output=True, timeout=timeout, **popen_kw(),
                       input="".join(textsafe.clean(n) + "\n" for n in names).encode("utf-8", "replace"))
    if r.returncode != 0:
        raise GitError(explain(r.stderr.decode("utf-8", "replace").strip() or "git cat-file failed"))
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


def rmtree(path):
    """Delete a folder tree even when it holds read-only files (git objects on Windows) or is half removed."""
    import shutil
    import stat

    def _force(fn, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            fn(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=_force)


def _sound(d):
    """A cached checkout whose .git lost pieces (interrupted removal) cannot be fetched into."""
    g = os.path.join(d, ".git")
    if not (os.path.isfile(os.path.join(g, "HEAD")) and os.path.isdir(os.path.join(g, "objects"))):
        return False
    r = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=d, env=_env(None, d), capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=60, **popen_kw())
    if "dubious ownership" in (r.stderr or ""):
        raise GitError(explain(r.stderr.strip()))     # intact but not ours to open: deleting and re-cloning would only fail the same way
    return r.returncode == 0 and r.stdout.strip() == ".git"


def fetch(url, branch="main", ref=None, base=None, lfs=False, subdir=""):
    """Clone or update a cached checkout and leave it at `ref` (tag, branch or commit) or the branch tip.
    A ref that cannot be resolved is an error: a pinned install must never fall back to something else.
    lfs=True also downloads the Git LFS files of `subdir`, so the tree holds real files, not pointers."""
    d = _fetch(url, branch, ref, base)
    if lfs:
        lfs_pull(d, subdir)
    return d


def current(url, branch="main", ref=None, base=None):
    """The cached checkout of url when it is already at the wanted commit (one cheap `ls-remote`, no download), else None.
    A pinned ref counts only when it is a full commit id (tags and branches can move)."""
    d = cache_dir(url, base)
    if not (os.path.isdir(os.path.join(d, ".git")) and _sound(d)):
        return None
    head = run(["rev-parse", "-q", "--verify", "HEAD^{commit}"], cwd=d, check=False)
    if not head:
        return None
    if ref:
        return d if re.fullmatch(r"[0-9a-f]{40}", ref) and head == ref else None
    return d if remote_head(url, branch, timeout=15) == head else None


LFS_TIMEOUT = 3600


def lfs_used(d):
    """True when a .gitattributes in the checkout sends files to Git LFS."""
    return bool(run(["grep", "-l", "-e", "filter=lfs", "--", ":(glob)**/.gitattributes"], cwd=d, check=False))


def lfs_pull(d, subdir=""):
    """Replace LFS pointer files with the real content (only under `subdir`). -> True when LFS is used here.
    Raises GitError when the package needs LFS and it cannot be had: a pointer file must never be installed as if it were the file."""
    if not lfs_used(d):
        return False
    if run(["lfs", "version"], cwd=d, check=False) == "":
        raise GitError("this package stores files in Git LFS but git-lfs is not installed; install it (https://git-lfs.com) and retry")
    run(["lfs", "install", "--local", "--force"], cwd=d)
    args = ["lfs", "pull", "origin"]
    sub = (subdir or "").strip("/")
    if sub:
        args += ["--include", sub + "/**"]
    try:
        run(args, cwd=d, timeout=LFS_TIMEOUT)
    except GitError as e:
        raise GitError("git-lfs could not download the package files: %s" % redact_url(str(e)))
    left = lfs_pointers(os.path.join(d, sub))
    if left:
        raise GitError("git-lfs left %d file(s) as pointers (%s); check that the LFS objects were uploaded and that `git lfs pull` works in %s"
                       % (len(left), ", ".join(left[:3]), d))
    return True


def lfs_pointers(root):
    """Relative paths under `root` that are still Git LFS pointer files (small text starting with the LFS spec line)."""
    out = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x != ".git"]
        for n in files:
            f = os.path.join(base, n)
            try:
                if os.path.getsize(f) < 1024:
                    with open(f, "rb") as fh:
                        if fh.read(40).startswith(b"version https://git-lfs.github.com/spec"):
                            out.append(os.path.relpath(f, root))
            except OSError:
                pass
    return out


def _fetch(url, branch, ref, base):
    d = cache_dir(url, base)
    if os.path.isdir(os.path.join(d, ".git")) and not _sound(d):
        rmtree(d)                                   # broken leftover (e.g. after self-uninstall): start clean
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
            # full history of the branch, then resolve locally. The cache is usually depth-1 from an earlier fetch, so a plain
            # fetch would keep it shallow and an older pinned commit (hosts refuse a bare sha) could never be found.
            deep = ["--unshallow"] if os.path.exists(os.path.join(d, ".git", "shallow")) else []
            run(["fetch", "-q", "--tags"] + deep + ["origin", branch], cwd=d)
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


def _ls(url, timeout=60):
    """`git ls-remote url` with the same environment as every other call (trusted folders, stored login). -> CompletedProcess or None on timeout."""
    args = ["ls-remote", url]
    try:
        return subprocess.run(["git"] + args, env=_login_env(_env(), None, args), capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, **popen_kw())
    except subprocess.TimeoutExpired:
        return None


def remote_empty(url):
    """True when the remote is reachable but has no commits at all (a freshly created repo). An unreachable or
    unauthorised remote is NOT empty: that stays an error."""
    r = _ls(url)
    return bool(r) and r.returncode == 0 and not r.stdout.strip()


def reachable(url):
    """True when `git ls-remote` succeeds (the repo exists and we may read it), whether or not it has any branch."""
    r = _ls(url)
    return bool(r) and r.returncode == 0


def large_files(cwd, limit_mb=50):
    out, big = run(["ls-files", "-co", "--exclude-standard"], cwd=cwd), []
    for rel in out.splitlines():
        try:
            if os.path.getsize(os.path.join(cwd, rel)) > limit_mb * 1048576:
                big.append(rel)
        except OSError:
            pass
    return big
