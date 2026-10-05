import json
import os

HOME = lambda: os.environ.get("AIHUB_HOME") or os.path.join(os.path.expanduser("~"), ".aihub")


def p(*a):
    d = os.path.join(HOME(), *a)
    return d


def ensure(*a):
    d = p(*a)
    os.makedirs(d, exist_ok=True)
    return d


def load(name, default):
    try:
        with open(p(name)) as f:
            return json.load(f)
    except Exception:
        return default


def save(name, data, private=False):
    ensure()
    tmp = p(name) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    if private:
        os.chmod(tmp, 0o600)
    os.replace(tmp, p(name))


def config():
    c = load("config.json", {})
    c.setdefault("hub", os.environ.get("AIHUB_URL", "http://localhost:8000"))
    return c
