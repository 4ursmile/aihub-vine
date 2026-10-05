import hashlib
import hmac
import os
import secrets


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
    return "pbkdf2$%s$%s" % (salt.hex(), h.hex())


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        c = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000)
        return hmac.compare_digest(c.hex(), h)
    except Exception:
        return False


def new_token() -> str:
    return "aih_" + secrets.token_urlsafe(32)


def hash_token(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()
