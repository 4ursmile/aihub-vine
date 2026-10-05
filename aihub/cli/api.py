import hashlib
import json
import os
import urllib.error
import urllib.request

from . import paths


class ApiError(Exception):
    pass


def _req(method, path, body=None, raw=None, headers=None, timeout=30):
    hub = paths.config()["hub"].rstrip("/")
    url = path if path.startswith("http") else hub + "/api/v1" + path
    h = dict(headers or {})
    tok = paths.load("credentials.json", {}).get("token")
    if tok:
        h["Authorization"] = "Bearer " + tok
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    rq = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        return urllib.request.urlopen(rq, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("detail", e.reason)
        except Exception:
            msg = e.reason
        raise ApiError("%s %s: %s" % (e.code, path, msg))
    except urllib.error.URLError as e:
        raise ApiError("cannot reach hub %s (%s)" % (hub, e.reason))


def call(method, path, body=None, **kw):
    with _req(method, path, body, **kw) as r:
        return json.loads(r.read() or b"{}")


def download(url, dest, sha256):
    h = hashlib.sha256()
    with _req("GET", url, timeout=120) as r, open(dest, "wb") as f:
        for b in iter(lambda: r.read(1 << 20), b""):
            h.update(b)
            f.write(b)
    if h.hexdigest() != sha256:
        os.remove(dest)
        raise ApiError("sha256 mismatch for download")
