"""Encrypted store for secrets the UI keeps itself (for example an API key given to the chat harness).

  ~/.aihub/ui/.key       32 random bytes, created once, mode 0600
  ~/.aihub/ui/secrets.bin  MAGIC | version | nonce | ciphertext | HMAC-SHA256 tag, mode 0600
Stdlib only: keystream = HMAC-SHA256(enc_key, nonce | counter), encrypt-then-MAC, enc/mac keys derived from the key file under
distinct labels. A file that fails its tag is never used and never overwritten by a read. Values are never sent to the browser."""
import hashlib
import hmac
import json
import os
import threading

MAGIC, VERSION, NONCE, TAG = b"AHUI", 1, 16, 32
_lock = threading.Lock()


def write_private(path, data):
    """Atomic write, readable by the owner only (the file never exists with wider rights)."""
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


def _keys(master):
    return (hmac.new(master, b"aihub-ui enc v1", hashlib.sha256).digest(),
            hmac.new(master, b"aihub-ui mac v1", hashlib.sha256).digest())


def _stream(key, nonce, n):
    out, i = bytearray(), 0
    while len(out) < n:
        out += hmac.new(key, nonce + i.to_bytes(8, "big"), hashlib.sha256).digest()
        i += 1
    return bytes(out[:n])


def seal(master, plain):
    ek, mk = _keys(master)
    nonce, head = os.urandom(NONCE), MAGIC + bytes([VERSION])
    ct = bytes(a ^ b for a, b in zip(plain, _stream(ek, nonce, len(plain))))
    return head + nonce + ct + hmac.new(mk, head + nonce + ct, hashlib.sha256).digest()


def unseal(master, blob):
    head = MAGIC + bytes([VERSION])
    if len(blob) < len(head) + NONCE + TAG or not blob.startswith(head):
        raise ValueError("not a secrets file")
    ek, mk = _keys(master)
    nonce, ct, tag = blob[len(head):len(head) + NONCE], blob[len(head) + NONCE:-TAG], blob[-TAG:]
    if not hmac.compare_digest(tag, hmac.new(mk, head + nonce + ct, hashlib.sha256).digest()):
        raise ValueError("secrets file failed its integrity check (edited, or another key)")
    return bytes(a ^ b for a, b in zip(ct, _stream(ek, nonce, len(ct))))


class Box:
    def __init__(self, folder):
        self.key_path = os.path.join(folder, ".key")
        self.path = os.path.join(folder, "secrets.bin")
        self.error = None

    def _master(self):
        try:
            with open(self.key_path, "rb") as f:
                k = f.read()
            if len(k) == 32:
                return k
        except OSError:
            pass
        if os.path.exists(self.path):
            raise ValueError("secrets exist but the key file is missing or damaged; remove secrets.bin to start over")
        k = os.urandom(32)
        write_private(self.key_path, k)
        return k

    def _load(self):
        if not os.path.exists(self.path):
            return {}
        with open(self.path, "rb") as f:
            return json.loads(unseal(self._master(), f.read()).decode("utf-8"))

    def names(self):
        with _lock:
            try:
                self.error = None
                return sorted(self._load())
            except Exception as e:
                self.error = str(e)
                return []

    def get_all(self):
        """-> {name: value} for the server's own use (environment of the chat process). Never returned over HTTP."""
        with _lock:
            try:
                return self._load()
            except Exception as e:
                self.error = str(e)
                return {}

    def put(self, name, value):
        with _lock:
            d = self._load()
            d[name] = value
            write_private(self.path, seal(self._master(), json.dumps(d).encode("utf-8")))

    def remove(self, name):
        with _lock:
            d = self._load()
            if d.pop(name, None) is not None:
                write_private(self.path, seal(self._master(), json.dumps(d).encode("utf-8")))

    def remove_prefix(self, prefix):
        with _lock:
            d = self._load()
            keys = [k for k in d if k.startswith(prefix)]
            for k in keys:
                del d[k]
            if keys:
                write_private(self.path, seal(self._master(), json.dumps(d).encode("utf-8")))
