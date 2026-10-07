import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from concurrent.futures import ThreadPoolExecutor

from . import paths

SINGLE_MAX = 8 * 1024 * 1024      # at or below this, one request is fine under any sane proxy limit


class ApiError(Exception):
    pass


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never send the hub token to a different host (e.g. a pre-signed S3/CDN URL).
    S3 also rejects a request that has both a signed query string and an Authorization header."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            for h in list(new.headers):
                if h.lower() == "authorization":
                    del new.headers[h]
        return new


_opener = urllib.request.build_opener(_SafeRedirect)


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
        return _opener.open(rq, timeout=timeout)
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


def _retry(fn, tries=5):
    """Run fn(); retry transient failures (network, 5xx, 429, and a proxy's 502/504) with exponential backoff."""
    for i in range(tries):
        try:
            return fn()
        except ApiError as e:
            code = str(e).split(" ", 1)[0]
            transient = code in ("429", "500", "502", "503", "504") or "cannot reach" in str(e)
            if not transient or i == tries - 1:
                raise
        except (OSError, ConnectionError):
            if i == tries - 1:
                raise
        time.sleep(min(2 ** i, 15))


def _file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def upload_package(path, visibility=None, progress=None, workers=3):
    """Publish an archive. Small files go in one request; larger ones use the resumable chunked API so a reverse proxy's
    body-size cap cannot reject them. Falls back to the single request when the hub predates chunked uploads."""
    name, size = os.path.basename(path), os.path.getsize(path)
    hdr = {"X-Aihub-Filename": name, "Content-Type": "application/octet-stream"}
    if visibility:
        hdr["X-Aihub-Visibility"] = visibility

    def single():
        with open(path, "rb") as f:
            return call("POST", "/upload", raw=f.read(), headers=hdr, timeout=300)

    if size <= SINGLE_MAX:
        return single()
    try:
        init = call("POST", "/uploads", {"filename": name, "size": size, "sha256": _file_sha256(path), "visibility": visibility})
    except ApiError as e:
        if str(e).split(" ", 1)[0] in ("404", "405"):
            return single()
        raise
    uid, cs = init["upload_id"], init["chunk_size"]
    have = set(call("GET", "/uploads/" + uid).get("received", []))
    todo = [n for n in range(init["chunks"]) if n not in have]
    done = [len(have) * cs]
    lock = threading.Lock()

    def send(n):
        with open(path, "rb") as f:
            f.seek(n * cs)
            data = f.read(cs)
        _retry(lambda: call("PUT", "/uploads/%s/chunks/%d" % (uid, n), raw=data, timeout=120,
                            headers={"X-Chunk-Sha256": hashlib.sha256(data).hexdigest(), "Content-Type": "application/octet-stream"}))
        with lock:
            done[0] += len(data)
            if progress:
                progress(min(done[0], size), size)

    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            list(ex.map(send, todo))
        return _retry(lambda: call("POST", "/uploads/%s/complete" % uid, {}, timeout=600))
    except ApiError as e:
        if str(e).split(" ", 1)[0] in ("404", "409", "403", "413"):      # session gone or publish refused: don't leave chunks behind
            try:
                call("DELETE", "/uploads/" + uid)
            except ApiError:
                pass
        raise


def download(url, dest, sha256, progress=None):
    """Download to `dest`, resuming a partial `dest.part` with a Range request. The sha256 is checked over the whole file."""
    part = dest + ".part"
    for attempt in range(5):
        have = os.path.getsize(part) if os.path.exists(part) else 0
        try:
            with _req("GET", url, headers={"Range": "bytes=%d-" % have} if have else None, timeout=120) as r:
                resumed = r.status == 206
                if have and not resumed:                 # server ignored Range: start over
                    have = 0
                total = have + int(r.headers.get("Content-Length") or 0)
                got = have
                with open(part, "ab" if resumed else "wb") as f:
                    for b in iter(lambda: r.read(1 << 16), b""):
                        f.write(b)
                        got += len(b)
                        if progress:
                            progress(got, total)
            break
        except ApiError as e:
            if "416" in str(e).split(" ", 1)[0] and have:         # partial is already complete or stale
                break
            if attempt == 4 or str(e).split(" ", 1)[0] not in ("502", "503", "504") and "cannot reach" not in str(e):
                raise
        except OSError:
            if attempt == 4:
                raise
        time.sleep(min(2 ** attempt, 10))
    if _file_sha256(part) != sha256:
        os.remove(part)
        raise ApiError("sha256 mismatch for download")
    os.replace(part, dest)
