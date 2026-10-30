"""Git credential helper for the hub and index hosts (stdlib only, any OS).

Git runs `aihub credential get|store|erase` and talks key=value lines on stdin/stdout. One entry per host, so a new token replaces
the old one (no duplicates). Where the secret lives, strongest first:
  macOS    Keychain (`security`)            Windows  DPAPI (ctypes), bound to your user account
  Linux    Secret Service (`secret-tool`)   fallback  ~/.aihub/git-credentials.json, mode 0600 (like ssh keys)
`register()` points git at this helper for our hosts only (and resets other helpers for them), leaving every other host alone."""
import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse

from . import gitx, paths, ui

SERVICE = "aihub-git"


def _run(args, inp=None):
    try:
        return subprocess.run(args, input=inp, capture_output=True, text=True, timeout=20, **gitx.popen_kw())
    except FileNotFoundError:                                  # the program (git, security, ...) is not installed
        return subprocess.CompletedProcess(args, 127, "", "%s: not found" % args[0])


# ---- backends: each has put(host, blob) / get(host) -> blob|None / drop(host); a failure raises or returns falsy ----------------
class Keychain:
    name = "macOS Keychain"

    @staticmethod
    def usable():
        return sys.platform == "darwin" and bool(shutil.which("security"))

    @staticmethod
    def put(host, blob):
        return _run(["security", "add-generic-password", "-U", "-s", SERVICE, "-a", host, "-w", blob]).returncode == 0

    @staticmethod
    def get(host):
        r = _run(["security", "find-generic-password", "-s", SERVICE, "-a", host, "-w"])
        if r.returncode not in (0, 44):                    # 44 = item not found; anything else (locked, no UI, busy) is a failure, not "empty"
            raise OSError("security exited %d" % r.returncode)
        return r.stdout.strip() if r.returncode == 0 else None

    @staticmethod
    def drop(host):
        _run(["security", "delete-generic-password", "-s", SERVICE, "-a", host])


class SecretService:
    name = "Secret Service (secret-tool)"

    @staticmethod
    def usable():
        if not (sys.platform.startswith("linux") and shutil.which("secret-tool")):
            return False
        probe = _run(["secret-tool", "lookup", "service", SERVICE, "host", "-probe-"])     # exit 1 = reachable, empty; other = no keyring
        return probe.returncode == 1 and not probe.stderr.strip()

    @staticmethod
    def put(host, blob):
        return _run(["secret-tool", "store", "--label=aihub git %s" % host, "service", SERVICE, "host", host], inp=blob).returncode == 0

    @staticmethod
    def get(host):
        r = _run(["secret-tool", "lookup", "service", SERVICE, "host", host])
        if r.returncode not in (0, 1) or r.stderr.strip():       # 1 + no message = not found
            raise OSError("secret-tool exited %d" % r.returncode)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None

    @staticmethod
    def drop(host):
        _run(["secret-tool", "clear", "service", SERVICE, "host", host])


def _dpapi(data, protect):
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("n", wintypes.DWORD), ("p", ctypes.POINTER(ctypes.c_char))]
    src = BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(out.p, out.n)
    finally:
        ctypes.windll.kernel32.LocalFree(out.p)


def _read_file():
    return paths.load("git-credentials.json", {})


class File:
    """Plain 0600 JSON file; on Windows the values are DPAPI-encrypted for the current user first."""
    name = "private file ~/.aihub/git-credentials.json"
    encrypted = False

    @staticmethod
    def usable():
        return True

    @classmethod
    def put(cls, host, blob):
        d = _read_file()
        d[host] = base64.b64encode(_dpapi(blob.encode(), True)).decode() if os.name == "nt" else blob
        paths.save("git-credentials.json", d, private=True)
        return True

    @classmethod
    def get(cls, host):
        v = _read_file().get(host)
        if not v:
            return None
        return _dpapi(base64.b64decode(v), False).decode() if os.name == "nt" else v

    @classmethod
    def drop(cls, host):
        d = _read_file()
        if d.pop(host, None) is not None:
            paths.save("git-credentials.json", d, private=True)


def backends():
    return [b for b in (Keychain, SecretService) if b.usable()] + [File]


def describe():
    """Where a stored login lands, for the one-line notice."""
    b = backends()[0]
    if b is File and os.name != "nt":
        return b.name + " (not encrypted; protected by file permissions)"
    return b.name + (" (DPAPI-encrypted)" if b is File else "")


# ---- git credential protocol --------------------------------------------------------------------------------------------------
def _parse(text):
    d = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k] = v
    return d


def _host(d):
    return d.get("host", "")


PLACEHOLDERS = ("oauth2", "x-access-token", "x-token-auth", "git", "token", "personalaccesstoken")      # user names that only mean "a token follows"
CACHE_TTL = 5.0                      # seconds a good read is served without asking the backends again
_cache = {}                          # (aihub home, host) -> (time, (username, secret)); last good login, kept when a backend fails
_lock = threading.Lock()


def host_of(url):
    """Scheme-less `host[:port]`, lowercase, no userinfo: the key git sends as host= and the one stored. '' when there is none.
    The same function as vault._norm and the server's host check (gitx.host_key)."""
    return gitx.host_key(url)


def _forget_cache(host):
    with _lock:
        _cache.pop((paths.HOME(), host), None)


def _vault_hosts():
    d = paths.load("vault.json", {})
    return set(d) if isinstance(d, dict) else set()


def _vault_user(host):
    d = paths.load("vault.json", {})
    v = d.get(host) if isinstance(d, dict) else None
    return (v.get("username") or "") if isinstance(v, dict) else ""


def _legacy_keys(b, host, users):
    """Keys older versions stored this host's login under in backend `b`: `store_git` used to send the whole netloc as host=, so
    'user@host' / 'user:token@host' (and the host as written, any case). The private file can be scanned; the Keychain and Secret Service
    cannot be listed cheaply, so only the 'user@host' candidates for the names we know are tried."""
    if issubclass(b, File):
        try:
            keys = list(_read_file())
        except Exception:
            return []
        return [k for k in keys if isinstance(k, str) and k != host and k.rsplit("@", 1)[-1].lower() == host]
    return ["%s@%s" % (u, host) for u in dict.fromkeys(u for u in users if u)]


def _legacy(host, users):
    """[(backend, key, credential)] saved under a legacy key, the best match first (a user name git asked for, then the vault's)."""
    users = [u for u in dict.fromkeys(users) if u]
    found = []
    for b in backends():
        for k in _legacy_keys(b, host, users):
            try:
                raw = b.get(k)
                c = json.loads(raw) if raw else None
            except Exception:
                continue
            if isinstance(c, dict) and c.get("username") and c.get("password"):
                found.append((b, k, c))
    rank = {u: i for i, u in enumerate(users)}
    found.sort(key=lambda t: rank.get(t[2]["username"], len(rank)))
    return found


def _drop_key(key):
    for b in backends():
        try:
            b.drop(key)
        except Exception:
            pass


def _read(host, users=()):
    """(credential dict | None, failed). failed = a backend raised and none had the entry, so "not found" is not certain.
    A login saved by an older version under a legacy key (see _legacy_keys) is found too and MOVED to the plain host key, secret kept:
    nobody has to enter it again."""
    failed = False
    for b in backends():
        try:
            raw = b.get(host)
            c = json.loads(raw) if raw else None
        except Exception:
            failed = True
            continue
        if isinstance(c, dict) and c.get("password"):
            return c, False
    for b, key, c in _legacy(host, users):
        if save(host, c["username"], c["password"]):
            _drop_key(key)
        return c, False
    return None, failed


def save(host, username, password):
    """Store a login for `host` in this process (same path as git's `store`): first backend that accepts it wins, the copy in every
    other backend is dropped. True when a backend took it. Does not verify; see remote_config.store_git."""
    if not (host and username and password):
        return False
    _forget_cache(host)
    blob = json.dumps({"username": username, "password": password})
    for b in backends():
        try:
            if b.put(host, blob):
                for other in backends():
                    if other is not b:
                        try:
                            other.drop(host)
                        except Exception:
                            pass
                _forget_cache(host)
                return True
        except Exception:
            continue
    return False


def forget(host):
    """Remove the login for `host` from every backend (what `aihub vault remove` does). git's own reject never gets here."""
    _forget_cache(host)
    legacy = [k for b in backends() for k in _legacy_keys(b, host, [_vault_user(host)])]
    for b in backends():
        for k in [host] + legacy:
            try:
                b.drop(k)
            except Exception:
                pass
    _forget_cache(host)


def run(action, stdin=None, out=None):
    d = _parse(sys.stdin.read() if stdin is None else stdin)
    out = out or sys.stdout
    host = _host(d)
    if not host:
        return 0
    if action == "get":
        want = d.get("username")
        c, _ = _read(host, [u for u in (want, _vault_user(host)) if u and u.lower() not in PLACEHOLDERS])
        if c and (not want or want == c.get("username") or want.lower() in PLACEHOLDERS):     # a placeholder user means "use the token"
            out.write("username=%s\npassword=%s\n" % (c["username"], c["password"]))
    elif action == "store":
        save(host, d.get("username"), d.get("password"))
    elif action == "erase":
        # git calls this after ANY failed attempt (flaky 401, rate limit, another user in the URL). Erase only the login git just
        # saw rejected, never one managed by `aihub vault` (that is `vault remove`, via forget()).
        if host in _vault_hosts() or not d.get("password"):
            return 0
        c, _ = _read(host)
        if c and c.get("password") == d["password"]:
            forget(host)
    return 0


# ---- registration -------------------------------------------------------------------------------------------------------------
def helper_value():
    from . import hooks, telemetry
    shim = paths.p("bin", "aihub")
    base = [shim] if os.name != "nt" and os.path.exists(shim) else telemetry.self_cmd()
    return "!" + " ".join(hooks._quote(x) for x in base) + " credential"


def _is_ours(val):
    return val == "" or (val.endswith(" credential") and "aihub" in val)


def _drop_userinfo_entries(wanted):
    """Older versions registered `credential.https://user@host.helper` (the whole netloc). Remove those entries for the hosts in `wanted`
    ({(scheme, host)}) when every helper in them is ours; a key that also holds someone else's helper is left alone."""
    groups = {}
    for line in _run(["git", "config", "--global", "--get-regexp", r"^credential\..*\.helper$"]).stdout.splitlines():
        key, _, val = line.partition(" ")
        groups.setdefault(key, []).append(val)
    for key, vals in groups.items():
        url = key[len("credential."):-len(".helper")]
        u = urllib.parse.urlsplit(url)
        if "@" not in u.netloc or (u.scheme, host_of(url)) not in wanted or not all(_is_ours(v) for v in vals):
            continue
        if any(v for v in vals):
            _run(["git", "config", "--global", "--unset-all", key])


HELPERS_FILE = "git-helpers.json"       # {config key: the helpers YOU had set for that host before we touched it}, restored by unregister


def _mine(val):
    return bool(val) and val.endswith(" credential") and "aihub" in val


def _user_helpers():
    """`credential.helper` values from the system and global config, in the order git applies them ("" = reset, as git does)."""
    r = _run(["git", "-C", os.path.expanduser("~"), "config", "--get-all", "credential.helper"])
    acc = []
    for v in r.stdout.splitlines():
        acc = [] if v == "" else acc + [v]
    return acc


def _chain(own):
    """The helpers git would use for a host without us: system/global ones, then the host's own ("" resets), ours left out."""
    acc = _user_helpers()
    for v in own:
        acc = [] if v == "" else acc + [v]
    return [v for i, v in enumerate(acc) if not _mine(v) and v not in acc[:i]]


def register(urls):
    """Point git at our helper for each http(s) host in urls WITHOUT taking anything away: the key becomes "" (reset), ours, then every
    helper git used for that host before (osxkeychain, manager, store, cache, ... in their order). Ours answers first when it holds a login
    (`aihub vault`); otherwise git falls through to your own helpers, so a login you already saved keeps working. The host's own earlier
    values are remembered in git-helpers.json and restored by unregister. Idempotent; re-synced when your global helper changes.
    Returns (changed, already_ok) host lists. An entry an older version wrote under `user@host` is removed (ours only)."""
    if not gitx.available():
        return [], []                                                        # no git, nothing to register a helper with
    want = helper_value()
    changed, ok = [], []
    wanted = set()
    saved = paths.load(HELPERS_FILE, {})
    for url in urls:
        u = urllib.parse.urlsplit(url or "")
        host = host_of(url) if u.scheme in ("http", "https") else ""        # no userinfo: user@host would match that one user only
        if not host:
            continue
        wanted.add((u.scheme, host))
        key = "credential.%s://%s.helper" % (u.scheme, host)
        cur = _run(["git", "config", "--global", "--get-all", key]).stdout.splitlines()
        if not any(_mine(v) for v in cur):
            saved[key] = cur                                                 # yours, from before aihub: kept and restored on uninstall
        own = saved.get(key, [])                                             # older aihub wrote ["", ours] and kept nothing: own = []
        desired = ["", want] + _chain(own)
        if cur == desired:
            if host not in ok:
                ok.append(host)
            continue
        _run(["git", "config", "--global", "--unset-all", key])
        for v in desired:
            _run(["git", "config", "--global", "--add", key, v])
        if host not in changed:
            changed.append(host)
    paths.save(HELPERS_FILE, saved, private=True)
    if wanted:
        _drop_userinfo_entries(wanted)
    return changed, ok


def unregister():
    """Remove our helper from git config (self-uninstall) and put back what each host key held before aihub."""
    saved = paths.load(HELPERS_FILE, {})
    r = _run(["git", "config", "--global", "--get-regexp", r"^credential\..*\.helper$"])
    keys = []
    for line in r.stdout.splitlines():
        key, _, val = line.partition(" ")
        if _mine(val) and key not in keys:
            keys.append(key)
    for key in keys:
        _run(["git", "config", "--global", "--unset-all", key])
        for v in saved.get(key, []):
            _run(["git", "config", "--global", "--add", key, v])


# ---- reading a stored login from code (the server's git calls, `aihub vault test`) -------------------------------------------------
def lookup(host):
    """(username, secret) stored for exactly `host` (host or host:port as git connects) by `aihub vault add` / `aihub setup` / a git `store`,
    or None. Asks the backends directly (no subprocess, no prompt). The default port is the one without ':port', so an entry for
    git.example.com is NOT offered to git.example.com:9999. A good read is remembered per host: while a backend fails (keychain busy,
    locked, timeout) the last good login is returned; a clean "not found" returns None and forgets it."""
    key = (paths.HOME(), host)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    c, failed = _read(host, [_vault_user(host)])
    got = (c["username"], c["password"]) if c and c.get("username") else None
    with _lock:
        if got:
            _cache[key] = (now, got)
        elif failed and hit:
            return hit[1]                        # backend trouble, not proof the login is gone
        else:
            _cache.pop(key, None)
    return got
