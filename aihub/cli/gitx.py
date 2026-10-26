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
FALLBACK = None         # server: fn(host) -> (user, secret) or None: the login an admin saved (Admin > Backup), tried only when the normal one fails
FALLBACK_STICKY = 3600  # seconds a host whose normal login was refused tries the fallback first (saves one failed call per git command)
_fallback_first = {}    # host -> time until which the fallback goes first
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


def _call_hosts(cwd, args):
    """[(scheme, host)] this network call talks to: the URLs in its arguments, else origin of `cwd`."""
    hosts = _hosts_in(args)
    if not hosts and cwd:
        try:
            url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=cwd, env=_trusted_env(dict(os.environ), cwd), capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=20, **popen_kw()).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return []
        hosts = _hosts_in([url])
    return hosts


def _apply_login(env, hosts, login):
    """Put the login `login(host)` gives for the first matching host into env (see _login_env). -> that host, or None."""
    for scheme, host in hosts:
        if scheme != "https" and not os.environ.get("AIHUB_GIT_ALLOW_HTTP"):
            continue                                                   # a token is never offered over plain http by default
        try:
            got = login(host)
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
            return host
    return None


def _login_env(env, cwd, args):
    """Give git the stored login for the host this call talks to, without touching disk or git config and without the secret appearing in a
    command line: user and secret go in the environment of this one child, and the inline helper (see _HELPER) prints them only for exactly
    the scheme and host[:port] they were issued for. Only when LOGIN is set (the server), the call can reach a network and the URL has no
    login of its own. A redirect is followed for the first request only (git's own default), and the helper still refuses any other host. https only, unless AIHUB_GIT_ALLOW_HTTP=1."""
    if LOGIN is None or not args or args[0] not in NETWORK + ("lfs",):
        return env
    _apply_login(env, _call_hosts(cwd, args), LOGIN)
    return env


def _attempts(env, cwd, args):
    """[(env, fallback_host)] to try in order. Normally one: the usual login (_login_env). When an admin saved a login for this host
    (FALLBACK) that differs from the usual one, it is a second try, used only after an auth failure; a host where that already happened
    tries it first for FALLBACK_STICKY s. Without a saved login nothing changes."""
    if FALLBACK is None or not args or args[0] not in NETWORK + ("lfs",):
        return [(_login_env(env, cwd, args), None)]
    hosts = _call_hosts(cwd, args)
    normal = dict(env)
    if LOGIN is not None:
        _apply_login(normal, hosts, LOGIN)
    fb = dict(env)
    h = _apply_login(fb, hosts, FALLBACK)
    if not h or (fb.get("AIHUB_GIT_HOST"), fb.get("AIHUB_GIT_USER"), fb.get("AIHUB_GIT_SECRET")) == \
            (normal.get("AIHUB_GIT_HOST"), normal.get("AIHUB_GIT_USER"), normal.get("AIHUB_GIT_SECRET")):
        return [(normal, None)]
    import time
    if _fallback_first.get(h, 0) > time.time():
        return [(fb, h), (normal, None)]
    return [(normal, None), (fb, h)]


def _note_attempt(host, ok, first):
    """Remember how a fallback try went (host = the fallback was used)."""
    import time
    import logging
    if host and ok and not first:
        if _fallback_first.get(host, 0) <= time.time():
            logging.getLogger("aihub").warning("git login for %s was refused; the login saved in Admin > Backup worked and is used first for now", host)
        _fallback_first[host] = time.time() + FALLBACK_STICKY
    elif host and not ok:
        _fallback_first.pop(host, None)


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


def _tried(attempts, call):
    """Run call(env) for each attempt until one does not fail on its login. -> the last CompletedProcess (None = timeout)."""
    r = None
    for i, (env, fb) in enumerate(attempts):
        r = call(env)
        bad = r is not None and r.returncode != 0 and fetch_failure((r.stderr or "") + "\n" + (r.stdout or "")) == "auth"
        if len(attempts) > 1:
            _note_attempt(fb, r is not None and r.returncode == 0, i == 0)
        if not bad:
            break
    return r


def run(args, cwd=None, check=True, timeout=None, input=None, extra_env=None):
    args = list(args)
    attempts = _attempts(_env(extra_env, cwd), cwd, args)
    if timeout is None and BATCH and args and args[0] in NETWORK:
        timeout = NETWORK_TIMEOUT
    if isinstance(input, str):
        input = textsafe.clean(input)              # a path or message with a non-UTF-8 byte must not fail the encode of git's stdin
    r = _tried(attempts, lambda env: _git(args, cwd, env, timeout, input))
    if r is None:
        if check:
            msg = "git %s timed out after %ss" % (args[0], timeout)
            raise GitError(explain(msg, _error_url(list(args), cwd, msg), "net") if args[0] in NETWORK else msg)
        return ""
    if check and r.returncode != 0:
        msg = (r.stderr or r.stdout).strip() or "git %s failed" % args[0]
        raise GitError(explain(msg, _error_url(list(args), cwd, msg)))
    return r.stdout.strip()


def _error_url(args, cwd, msg):
    """The remote a failed network command talked to, for the hint: a URL in its arguments, else (only when the message names none) origin."""
    if not args or args[0] not in NETWORK + ("lfs",):
        return None
    hosts = _hosts_in(args)
    if hosts:
        return next(a for a in args if _host_of(a))
    if cwd and not _text_url(msg) and fetch_failure(msg) in ("auth", "net"):
        try:
            return subprocess.run(["git", "remote", "get-url", "origin"], cwd=cwd, env=_trusted_env(dict(os.environ), cwd), capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=20, **popen_kw()).stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None
    return None


HINT = "[aihub hint] "


def strip_hints(msg):
    """The message without our own `[aihub hint]` lines (so a hint can never change how an error is classified)."""
    return "\n".join(ln for ln in str(msg or "").splitlines() if not ln.startswith(HINT))


def host_key(url):
    """`host[:port]` of an http(s) URL, scheme-less ("git.example.com" counts as https), lowercase, no userinfo, IPv6 bracketed, port as
    written; '' when there is none. THE key for a stored login (credhelper, vault, server): all three must agree, so they call this."""
    url = (url or "").strip()
    h = _host_of(url if "://" in url else "https://" + url)
    return h[1] if h else ""


_URL_IN_TEXT = re.compile(r"https?://[^\s'\"<>]+")


def _text_url(msg):
    """First http(s) URL named in a git message ("unable to access 'https://host/x.git/'"), redacted and without query, or None."""
    for m in _URL_IN_TEXT.finditer(redact_url(msg or "")):
        u = m.group(0).split("?")[0].split("#")[0].rstrip(".,;:)")
        if _host_of(u):
            return u
    return None


def hint(kind, url=None, msg=""):
    """The one-line fix for a failure `kind` (auth | net | missing) or None. HOST comes from `url`, else from the URL inside `msg`; always redacted.
    auth/net need a host (without one the failure is not about a git host); on the server (LOGIN set) the auth text names the server's settings."""
    if kind == "missing":
        return (HINT + "the published commit/branch is not on the remote (force-push or branch deleted?). Re-publish: aihub dev publish, "
                "or install another version: aihub install NAME@VERSION")
    shown = redact_url(url) if url and _host_of(redact_url(url)) else _text_url(msg)
    h = _host_of(shown) if shown else None
    if not h:
        return None
    scheme, host = h
    if kind == "auth":
        if LOGIN is not None:
            return (HINT + "the hub has no working git login for %s: set AIHUB_GIT_TOKEN (+AIHUB_GIT_USER, AIHUB_GIT_HOSTS=%s) in the server env, "
                    "or run `aihub vault add %s://%s` as the account that runs the server" % (host, host, scheme, host))
        return (HINT + "git login for %s was refused or is missing. Run: aihub vault add %s://%s --user NAME  (2FA accounts: use a personal "
                "access token, not the password). Check it: aihub vault test %s" % (host, scheme, host, host))
    if kind == "net":
        return (HINT + "cannot reach %s (network, proxy or TLS). Check: git ls-remote %s; behind a proxy set AIHUB_GIT_PROXY; "
                "private CA: AIHUB_CA_BUNDLE=<ca.pem>" % (host, shown if "/" in shown.split("//", 1)[-1] else shown + "/"))
    return None


def explain(msg, url=None, kind=None):
    """Add the one-line fix to the git errors people cannot guess from git's own words: the original text first, ONE `[aihub hint]` line
    last, never twice. `kind` forces a class ("missing" for our own "cannot find"); else fetch_failure() decides. `url` names the remote
    when the message does not."""
    if HINT in msg:
        return msg
    if "dubious ownership" in msg:
        m = re.search(r"repository at '([^']+)'", msg)
        return msg + ("\n" + HINT + "the folder belongs to another account than the one running git. The hub trusts its own data folder automatically; "
                      "for any other: git config --global --add safe.directory %s" % (m.group(1) if m else "<folder>"))
    h = hint(kind or fetch_failure(msg), url, msg)
    return msg + "\n" + h if h else msg


MISSING = ("couldn't find remote ref", "could not find remote ref")


def is_missing(msg):
    """True for our own "cannot find '<ref>' in <url>" error (the commit/tag does not exist in that repository)."""
    return "cannot find '" in str(msg)


def add_note(msg, note):
    """Put `note` after the error text but before its single trailing `[aihub hint]` line, which stays last."""
    msg = str(msg)
    head, sep, hint = msg.partition("\n" + HINT)
    return head + "\n" + note + sep + hint


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


def _cache_key(url):
    return re.sub(r"[^A-Za-z0-9]+", "_", redact_url(url)).strip("_")[-80:]


def _norm_url(url):
    """Same repo, same key: host case, a trailing "/" and a trailing ".git" do not matter. Only used for the cache folder name."""
    u = redact_url(url).strip()
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*://)([^/]*)(.*)$", u)
    if m:
        u = m.group(1).lower() + m.group(2).lower() + m.group(3)
    u = u.rstrip("/")
    return u[:-4].rstrip("/") if u.lower().endswith(".git") else u


def cache_dir(url, base=None):
    key = _cache_key(_norm_url(url))
    root = base or paths.ensure("repos")
    d = os.path.join(root, key)
    old = os.path.join(root, _cache_key(url))
    if old != d and not os.path.isdir(os.path.join(d, ".git")) and os.path.isdir(os.path.join(old, ".git")):
        return old                                   # a clone made under the old name keeps being used (it may hold unpushed work)
    os.makedirs(d, exist_ok=True)
    return d


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


_MISSING_REF = ("couldn't find remote ref", "could not find remote ref", "not our ref", "unadvertised object", "no such remote ref",
                "no such ref", "invalid refspec", "did not match any")
_AUTH = ("authentication", "invalid username or password", "password authentication is not supported", "access denied", "not allowed to download",
         "terminal prompts disabled", "could not read username", "could not read password", "permission denied", "publickey", "forbidden",
         "unauthorized", "could not read from remote", "denied")
_AUTH_RE = re.compile(r"\b40[13]\b|repository\b.*\bnot found")
_NET = ("timed out", "could not resolve host", "unable to access", "couldn't connect", "failed to connect", "connection refused",
        "connection reset", "network is unreachable", "ssl", "certificate", "does not appear to be a git repository")


def fetch_failure(msg):
    """Classify a failed `git fetch`: "missing" (the ref/sha is not offered: GitHub 'not our ref', GitLab 'unadvertised object', 'couldn't find remote ref'),
    "auth" (login needed or refused, incl. a private repo reported as 'Repository not found'), "net" (unreachable, timeout, TLS) or "other"."""
    m = strip_hints(msg).lower()
    if any(w in m for w in _MISSING_REF):
        return "missing"
    if any(w in m for w in _AUTH) or _AUTH_RE.search(m):
        return "auth"
    if any(w in m for w in _NET):
        return "net"
    return "other"


NO_SMUDGE = {"GIT_LFS_SKIP_SMUDGE": "1"}       # checkout must not download LFS files (no login env there); lfs_pull does it explicitly


def _try_fetch(d, args):
    """Run a fetch: None when it worked, the GitError when the ref was just not there; auth/network failures are raised as they are."""
    try:
        run(["fetch", "-q"] + args, cwd=d)
    except GitError as e:
        if fetch_failure(str(e)) in ("auth", "net"):
            raise
        return e
    return None


def _resolve(d, want, pinned):
    for c in ([want, "refs/remotes/origin/" + want] if pinned else [want]):
        sha = run(["rev-parse", "--verify", "-q", c + "^{commit}"], cwd=d, check=False)
        if sha:
            return sha
    return ""


def _deepen(d, rest):
    """Fetch `rest` with full history (the cache is usually depth-1, which hides every older commit)."""
    deep = ["--unshallow"] if os.path.exists(os.path.join(d, ".git", "shallow")) else []
    try:
        return _try_fetch(d, ["--tags"] + deep + rest)
    except GitError as e:
        if deep and "complete repository" in str(e):
            return _try_fetch(d, ["--tags"] + rest)
        raise


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
    hexish = bool(ref and re.fullmatch(r"[0-9a-fA-F]{7,40}", ref))
    if hexish:                                      # a commit never changes: if the cache has it, no network is needed
        sha = _resolve(d, want, False)
        if sha:
            run(["checkout", "-q", "--force", sha], cwd=d, extra_env=NO_SMUDGE)
            return d
    err = _try_fetch(d, ["--depth", "1", "origin", want])           # branch, tag, or (if the host allows) a commit
    if err is None:
        run(["checkout", "-q", "--force", "FETCH_HEAD"], cwd=d, extra_env=NO_SMUDGE)
        return d
    if not ref and fetch_failure(str(err)) == "other":
        raise err
    sha = ""
    if ref:                                         # pinned: look for it where it can be (a branch is never swapped for another commit)
        steps = [lambda: _deepen(d, ["origin", branch]),                                            # the manifest branch, full history
                 lambda: _deepen(d, ["origin", "+refs/heads/*:refs/remotes/origin/*"])]             # any branch (publish pushed another one, or renamed)
        if hexish:
            steps.append(lambda: _try_fetch(d, ["origin", want]))                                   # a host that allows a sha, without depth
        for step in steps:
            err = step() or err
            sha = _resolve(d, want, True)
            if sha:
                break
    if not sha:
        last = (strip_hints(err).strip().splitlines() or [""])[-1]
        raise GitError(explain("cannot find '%s' in %s%s" % (want, redact_url(url), " (git: %s)" % redact_url(last) if last else ""), url, "missing"))
    run(["checkout", "-q", "--force", sha], cwd=d, extra_env=NO_SMUDGE)
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
    return _tried(_attempts(_env(), None, args), lambda env: _git(args, None, env, timeout))


def _git(args, cwd, env, timeout, input=None):
    """One git child. -> CompletedProcess, or None when it timed out."""
    try:
        return subprocess.run(["git"] + args, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, input=input, **popen_kw())
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
