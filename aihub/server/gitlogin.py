"""Git logins an admin saves in the web UI (Admin > Backup > Git login), for the server's own git calls (backup, sync, transfer, publish fetch).

They are a FALLBACK (`gitx.FALLBACK`): a git call first runs with the usual login (AIHUB_GIT_TOKEN, the vault, git's own helpers) and only a
call refused for its login is retried with the saved one, so a hub that works today behaves exactly as before.

Storage, never in the database (so never in a backup, an export or the audit log):
  <data>/.git-login.key   32 random bytes, created once, mode 0600 (or AIHUB_SECRET_KEY_FILE, e.g. a mounted secret outside the data folder)
  <data>/.git-login.bin   binary: MAGIC | version | 16-byte nonce | ciphertext | 32-byte HMAC-SHA256 tag, mode 0600
The cipher is stdlib only: the keystream is HMAC-SHA256(enc_key, nonce | counter) (HMAC as a PRF in counter mode), authenticated with
encrypt-then-MAC over header, nonce and ciphertext; enc and mac keys are derived from the key file with HMAC under distinct labels.
A file that fails its tag (edited, other key) is not used and is not overwritten by a read. The secret never goes back to the browser."""
import hashlib
import hmac
import json
import logging
import os
import threading
import time

from ..cli import gitx

log = logging.getLogger("aihub")
MAGIC, VERSION = b"AHGL", 1
NONCE, TAG = 16, 32
MAX_SECRET = 4096


def _write_private(path, data):
    """Atomic write, readable by the owner only."""
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _keys(master):
    return (hmac.new(master, b"aihub git-login enc v1", hashlib.sha256).digest(),
            hmac.new(master, b"aihub git-login mac v1", hashlib.sha256).digest())


def _stream(key, nonce, n):
    out, i = bytearray(), 0
    while len(out) < n:
        out += hmac.new(key, nonce + i.to_bytes(8, "big"), hashlib.sha256).digest()
        i += 1
    return bytes(out[:n])


def seal(master, plain):
    ek, mk = _keys(master)
    nonce = os.urandom(NONCE)
    head = MAGIC + bytes([VERSION])
    ct = bytes(a ^ b for a, b in zip(plain, _stream(ek, nonce, len(plain))))
    return head + nonce + ct + hmac.new(mk, head + nonce + ct, hashlib.sha256).digest()


def unseal(master, blob):
    """-> plaintext bytes; ValueError when the blob is not ours, damaged or sealed with another key."""
    head = MAGIC + bytes([VERSION])
    if len(blob) < len(head) + NONCE + TAG or not blob.startswith(head):
        raise ValueError("not a git login file")
    ek, mk = _keys(master)
    nonce, ct, tag = blob[len(head):len(head) + NONCE], blob[len(head) + NONCE:-TAG], blob[-TAG:]
    if not hmac.compare_digest(tag, hmac.new(mk, head + nonce + ct, hashlib.sha256).digest()):
        raise ValueError("git login file failed its integrity check (edited, or another key)")
    return bytes(a ^ b for a, b in zip(ct, _stream(ek, nonce, len(ct))))


def clean_host(v):
    """host[:port] from a host or URL as typed ('' when none)."""
    return gitx.host_key(str(v or ""))


def check_text(name, v, required=True):
    """A user or secret goes into git's credential protocol line by line: no control characters (a newline would add fields)."""
    v = str(v or "")
    if required and not v:
        raise ValueError("%s is required" % name)
    if len(v) > MAX_SECRET or any(ord(c) < 32 or ord(c) == 127 for c in v):
        raise ValueError("%s must be at most %d characters without line breaks or control characters" % (name, MAX_SECRET))
    return v


class Store:
    def __init__(self, data_dir, key_file=""):
        self.path = os.path.join(data_dir, ".git-login.bin")
        self.key_file = key_file or os.path.join(data_dir, ".git-login.key")
        self.lock = threading.Lock()
        self._seen, self._data, self.error = None, {}, ""

    def _key(self, create=False):
        try:
            with open(self.key_file, "rb") as f:
                k = f.read()
        except FileNotFoundError:
            if not create:
                return None
            os.makedirs(os.path.dirname(os.path.abspath(self.key_file)), exist_ok=True)
            try:
                fd = os.open(self.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                return self._key()
            with os.fdopen(fd, "wb") as f:
                f.write(os.urandom(32))
                f.flush()
                os.fsync(f.fileno())
            return self._key()
        if len(k) < 32:
            raise ValueError("%s is too short (needs 32 random bytes)" % self.key_file)
        return k

    def _load(self):
        """{host: {user, secret, by, ts}}; re-read only when the file changed. Caller holds the lock."""
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            self._seen, self._data, self.error = None, {}, ""
            return self._data
        sig = (st.st_mtime_ns, st.st_size)
        if sig == self._seen:
            return self._data
        try:
            k = self._key()
            if k is None:
                raise ValueError("the key file %s is missing" % self.key_file)
            with open(self.path, "rb") as f:
                doc = json.loads(unseal(k, f.read()).decode("utf-8"))
            self._data, self.error = dict(doc.get("logins") or {}), ""
        except (OSError, ValueError) as e:
            if str(e) != self.error:
                log.error("saved git logins not usable: %s", e)
            self._data, self.error = {}, str(e)
        self._seen = sig
        return self._data

    def _save(self, data):
        k = self._key(create=True)
        _write_private(self.path, seal(k, json.dumps({"v": 1, "logins": data}, sort_keys=True).encode("utf-8")))
        self._seen = None

    def get(self, host):
        """(user, secret) for exactly host[:port], or None. Used by gitx as FALLBACK."""
        with self.lock:
            e = self._load().get((host or "").strip().lower())
        return (e.get("user") or "oauth2", e["secret"]) if e and e.get("secret") else None

    def list(self):
        """What the admin page shows: never the secret."""
        with self.lock:
            data = self._load()
            return [{"host": h, "user": e.get("user") or "oauth2", "by": e.get("by", ""), "ts": e.get("ts", 0)} for h, e in sorted(data.items())]

    def put(self, host, user, secret, by=""):
        host = clean_host(host)
        if not host:
            raise ValueError("host must be a git host such as github.com or https://git.example.com/org/repo.git")
        user, secret = check_text("user", user, required=False), check_text("token", secret)
        with self.lock:
            data = dict(self._load())
            if self.error and os.path.exists(self.path):
                raise ValueError("the saved logins cannot be read (%s): remove them first" % self.error)
            data[host] = {"user": user, "secret": secret, "by": by, "ts": int(time.time())}
            self._save(data)
        return host

    def remove(self, host):
        host = clean_host(host) or (host or "").strip().lower()
        with self.lock:
            data = dict(self._load())
            if self.error:                        # unreadable file: removing = start again
                try:
                    os.remove(self.path)
                except OSError:
                    pass
                self._seen, self._data, self.error = None, {}, ""
                return True
            if host not in data:
                return False
            del data[host]
            self._save(data)
        return True


_stores = {}
_slock = threading.Lock()


def store(settings):
    """The one Store of this data folder (gitauth.install and the admin routes share it)."""
    key = (os.path.abspath(settings.data_dir), settings.env_get("AIHUB_SECRET_KEY_FILE"))
    with _slock:
        if key not in _stores:
            _stores[key] = Store(key[0], key[1])
        return _stores[key]


def check(st, url, host=""):
    """`git ls-remote url` with ONLY the saved login of the url's host (no other login, no git helpers). -> (ok, message)."""
    hosts = gitx._hosts_in([url])
    if not hosts:
        return False, "no https git URL to test against"
    if host and hosts[0][1] != host:
        return False, "the saved login is for %s, the backup repo is on %s" % (host, hosts[0][1])
    env = gitx._env()
    if not gitx._apply_login(env, hosts, st.get):
        return False, "no saved login for %s" % hosts[0][1]
    r = gitx._git(["ls-remote", "--heads", url], None, env, 60)
    if r is None:
        return False, "git ls-remote timed out"
    if r.returncode:
        return False, gitx.redact_url((r.stderr or r.stdout or "").strip())[:300] or "git ls-remote failed"
    return True, "the saved login can read %s" % gitx.redact_url(url)
