"""Short-lived signed download links: <package>|<version>|<expiry> signed with a server secret kept in data_dir.
Stateless, so it works across workers sharing a data dir. The link authorises one archive only, for TTL seconds."""
import base64
import hashlib
import hmac
import os
import secrets
import time

TTL = 120
_cache = {}


def _secret(data_dir):
    if data_dir not in _cache:
        p = os.path.join(data_dir, ".download_secret")
        if not os.path.exists(p):
            fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600) if not os.path.exists(p + ".tmp") else None
            if fd is not None:
                with os.fdopen(fd, "wb") as f:
                    f.write(secrets.token_bytes(32))
                os.replace(p + ".tmp", p)
        with open(p, "rb") as f:
            _cache[data_dir] = f.read()
    return _cache[data_dir]


def _sig(data_dir, msg):
    return hmac.new(_secret(data_dir), msg.encode(), hashlib.sha256).hexdigest()[:40]


def make(data_dir, name, version):
    msg = "%s|%s|%d" % (name, version, time.time() + TTL)
    return base64.urlsafe_b64encode(msg.encode()).decode().rstrip("=") + "." + _sig(data_dir, msg)


def check(data_dir, token):
    """-> (name, version) or None."""
    try:
        b, sig = token.rsplit(".", 1)
        msg = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode()
        name, version, exp = msg.split("|")
        if hmac.compare_digest(sig, _sig(data_dir, msg)) and time.time() < int(exp):
            return name, version
    except Exception:
        pass
    return None
