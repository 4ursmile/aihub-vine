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
import urllib.parse

from . import gitx, paths, ui

SERVICE = "aihub-git"


def _run(args, inp=None):
    return subprocess.run(args, input=inp, capture_output=True, text=True, timeout=20, **gitx.popen_kw())


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


def run(action, stdin=None, out=None):
    d = _parse(sys.stdin.read() if stdin is None else stdin)
    out = out or sys.stdout
    host = _host(d)
    if not host:
        return 0
    if action == "get":
        for b in backends():
            try:
                raw = b.get(host)
                c = json.loads(raw) if raw else None
            except Exception:
                continue
            if c and c.get("password") and (not d.get("username") or d["username"] == c.get("username")):
                out.write("username=%s\npassword=%s\n" % (c["username"], c["password"]))
                break
    elif action == "store":
        if d.get("username") and d.get("password"):
            blob = json.dumps({"username": d["username"], "password": d["password"]})
            for b in backends():
                try:
                    if b.put(host, blob):
                        for other in backends():              # one copy only: drop it from every other store
                            if other is not b:
                                other.drop(host)
                        break
                except Exception:
                    continue
    elif action == "erase":
        for b in backends():
            try:
                b.drop(host)
            except Exception:
                pass
    return 0


# ---- registration -------------------------------------------------------------------------------------------------------------
def helper_value():
    from . import hooks, telemetry
    shim = paths.p("bin", "aihub")
    base = [shim] if os.name != "nt" and os.path.exists(shim) else telemetry.self_cmd()
    return "!" + " ".join(hooks._quote(x) for x in base) + " credential"


def register(urls):
    """Point git at our helper for each https host in urls. Resets other helpers for those hosts (empty value) so a login is kept in
    exactly one place, and is idempotent: a host that is already right is not touched. Returns (changed, already_ok) host lists."""
    want = helper_value()
    changed, ok = [], []
    for url in urls:
        u = urllib.parse.urlsplit(url or "")
        if u.scheme not in ("http", "https") or not u.netloc:
            continue
        key = "credential.%s://%s.helper" % (u.scheme, u.netloc)
        cur = _run(["git", "config", "--global", "--get-all", key]).stdout.splitlines()
        if cur == ["", want]:
            if u.netloc not in ok:
                ok.append(u.netloc)
            continue
        _run(["git", "config", "--global", "--unset-all", key])
        _run(["git", "config", "--global", "--add", key, ""])
        _run(["git", "config", "--global", "--add", key, want])
        if u.netloc not in changed:
            changed.append(u.netloc)
    return changed, ok


def unregister():
    """Remove every git config entry that points at this helper (used by self-uninstall)."""
    r = _run(["git", "config", "--global", "--get-regexp", r"^credential\..*\.helper$"])
    for line in r.stdout.splitlines():
        key, _, val = line.partition(" ")
        if val.endswith(" credential") and "aihub" in val:
            _run(["git", "config", "--global", "--unset-all", key])


# ---- reading a stored login from code (the server's git calls, `aihub vault test`) -------------------------------------------------
def lookup(host):
    """(username, secret) stored for exactly `host` (host or host:port as git connects) by `aihub vault add` / `aihub setup` / a git `store`,
    or None. Asks the backends directly (no subprocess, no prompt). The default port is the one without ':port', so an entry for
    git.example.com is NOT offered to git.example.com:9999."""
    for key in [host]:
        for b in backends():
            try:
                raw = b.get(key)
                c = json.loads(raw) if raw else None
            except Exception:
                continue
            if c and c.get("username") and c.get("password"):
                return c["username"], c["password"]
    return None
