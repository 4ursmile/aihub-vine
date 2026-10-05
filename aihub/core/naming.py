import re

_RE = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$")


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def validate(name: str) -> str:
    n = normalize(name)
    if not n or len(n) > 64 or not _RE.match(n):
        raise ValueError("invalid package name: %r" % name)
    return n
