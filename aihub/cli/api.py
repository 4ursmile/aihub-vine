import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from concurrent.futures import ThreadPoolExecutor

from . import paths, tls

SINGLE_MAX = 8 * 1024 * 1024      # at or below this, one request is fine under any sane proxy limit
UP_WORKERS = 4                    # chunks sent at the same time (`publish -i`)
DL_WORKERS = 4                    # Range chunks fetched at the same time (`install`, `download`)
DL_CHUNK = 4 * 1024 * 1024        # bytes per download chunk; a file this small or smaller is one request


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


class _Opener:
    """Builds the opener per call so ca_bundle / insecure_tls changes apply immediately."""
    def open(self, rq, timeout=None):
        ctx = tls.context()
        handlers = [_SafeRedirect] + ([urllib.request.HTTPSHandler(context=ctx)] if ctx else [])
        return urllib.request.build_opener(*handlers).open(rq, timeout=timeout)


_opener = _Opener()


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
        raise ApiError("cannot reach hub %s (%s)%s" % (hub, e.reason, tls.hint(e.reason)))


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


def _code(e):
    return str(e).split(" ", 1)[0]


def publish_direct(path, progress=None, workers=UP_WORKERS):
    """`aihub dev publish -i`: send a package .tar.gz to the hub, which stores and indexes it (no git). -> the hub's answer.
    Small archives go in one request. Bigger ones use the resumable chunk API: chunks are sent in parallel (any order, a chunk again is
    harmless), an interrupted upload continues where it stopped, and the hub checks the joined file against its sha256.
    `progress(done_bytes, total_bytes)` is called as chunks finish."""
    size = os.path.getsize(path)
    hdr = {"Content-Type": "application/octet-stream"}

    def single():
        with open(path, "rb") as f:
            data = f.read()
        if progress:
            progress(size, size)
        return _retry(lambda: call("POST", "/index/direct", raw=data, headers=hdr, timeout=600))

    try:
        if size <= SINGLE_MAX:
            return single()
        try:
            return _chunked_publish(path, size, progress, workers)
        except ApiError as e:
            if _code(e) in ("404", "405"):                       # a hub without the chunk routes: one request, as before
                return single()
            raise
    except ApiError as e:
        code = _code(e)
        if code in ("401", "403"):
            raise ApiError("%s (publishing to the hub needs an account that may publish: aihub login)" % e)
        if code in ("404", "405"):
            raise ApiError("%s (this hub is too old for `publish -i`; update the hub)" % e)
        if code == "413":
            raise ApiError("%s (the hub or a proxy in front of it refuses archives this big; ask the admin to raise direct_max_mb / the proxy limit)" % e)
        raise


def _chunked_publish(path, size, progress, workers):
    base = "/index/direct/uploads"
    init = _retry(lambda: call("POST", base, {"size": size, "sha256": _file_sha256(path)}))
    uid, cs = init["upload_id"], init["chunk_size"]
    have = set(call("GET", "%s/%s" % (base, uid)).get("received", init.get("received", [])))
    todo = [n for n in range(init["chunks"]) if n not in have]
    done, lock = [min(size, len(have) * cs)], threading.Lock()
    if progress:
        progress(done[0], size)

    def send(n):
        with open(path, "rb") as f:
            f.seek(n * cs)
            data = f.read(cs)
        _retry(lambda: call("PUT", "%s/%s/chunks/%d" % (base, uid, n), raw=data, timeout=180,
                            headers={"X-Chunk-Sha256": hashlib.sha256(data).hexdigest(), "Content-Type": "application/octet-stream"}))
        with lock:
            done[0] = min(size, done[0] + len(data))
            if progress:
                progress(done[0], size)

    try:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(todo) or 1))) as ex:
            list(ex.map(send, todo))
        return _retry(lambda: call("POST", "%s/%s/complete" % (base, uid), {}, timeout=900))
    except ApiError as e:
        if _code(e) in ("403", "409", "413", "422"):             # publish refused: do not leave the chunks behind
            try:
                call("DELETE", "%s/%s" % (base, uid))
            except ApiError:
                pass
        raise


def download_ranges(path, dest, sha256="", progress=None, workers=DL_WORKERS, chunk=DL_CHUNK):
    """GET a hub API path into the file `dest` in parallel Range chunks. An interrupted download continues (`dest.part` + `dest.part.json`
    remember which chunks are done), a chunk that fails is retried, and every chunk must come from the same version of the file (X-Aihub-Rev).
    Small files, and hubs that ignore Range, are a plain single download. The sha256 (given, else the hub's X-Aihub-Sha256) is checked.
    -> the response headers of the first request (X-Aihub-Rev, X-Aihub-Cache, ...). `progress(done_bytes, total_bytes)`."""
    part, meta_path = dest + ".part", dest + ".part.json"
    with _req("GET", path, headers={"Range": "bytes=0-%d" % (chunk - 1)}, timeout=300) as r:
        head = r.headers                          # case-insensitive (a server may send x-aihub-rev); stays readable after the response closes
        first = r.read()
        status, crange = r.status, r.headers.get("Content-Range") or ""
    rev = head.get("X-Aihub-Rev") or head.get("ETag") or ""
    total = len(first)
    if status == 206 and "/" in crange and crange.rsplit("/", 1)[1].isdigit():
        total = int(crange.rsplit("/", 1)[1])
    want = sha256 or head.get("X-Aihub-Sha256") or ""
    meta = {"rev": rev, "total": total, "chunk": chunk, "done": []}
    if status == 206 and total > len(first):
        try:
            with open(meta_path) as f:
                old = json.load(f)
            if old.get("rev") == rev and old.get("total") == total and old.get("chunk") == chunk and os.path.getsize(part) == total:
                meta["done"] = [int(n) for n in old.get("done", [])]
        except (OSError, ValueError, TypeError):
            pass
        if not meta["done"]:
            with open(part, "wb") as f:
                f.truncate(total)
        n_chunks = -(-total // chunk)
        lock, state = threading.Lock(), {"done": set(meta["done"]), "bytes": 0}

        def mark(i, data):
            with lock:
                with open(part, "r+b") as f:
                    f.seek(i * chunk)
                    f.write(data)
                state["done"].add(i)
                state["bytes"] += len(data)
                with open(meta_path, "w") as f:
                    json.dump(dict(meta, done=sorted(state["done"])), f)
                if progress:
                    progress(min(total, sum(min(chunk, total - j * chunk) for j in state["done"])), total)

        if 0 not in state["done"]:
            mark(0, first)
        elif progress:
            progress(min(total, sum(min(chunk, total - j * chunk) for j in state["done"])), total)

        def fetch(i):
            a, b = i * chunk, min(total, (i + 1) * chunk) - 1
            def go():
                hdr = {"Range": "bytes=%d-%d" % (a, b)}
                if head.get("ETag"):
                    hdr["If-Range"] = head["ETag"]                # the file changed meanwhile: the hub answers 200 (whole file), which we refuse below
                with _req("GET", path, headers=hdr, timeout=300) as rr:
                    if rr.status != 206 or (rr.headers.get("X-Aihub-Rev") or rev) != (head.get("X-Aihub-Rev") or rev):
                        raise ApiError("409 the package changed on the hub while it was downloading; run the command again")
                    data = rr.read()
                if len(data) != b - a + 1:
                    raise ApiError("502 short chunk %d (%d of %d bytes)" % (i, len(data), b - a + 1))
                return data
            mark(i, _retry(go))

        todo = [i for i in range(n_chunks) if i not in state["done"]]
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(todo) or 1))) as ex:
            list(ex.map(fetch, todo))
        try:
            os.remove(meta_path)
        except OSError:
            pass
    else:
        with open(part, "wb") as f:
            f.write(first)
        if progress:
            progress(total, total)
    if want and _file_sha256(part) != want:
        os.remove(part)
        raise ApiError("sha256 mismatch for download (expected %s)" % want[:12])
    os.replace(part, dest)
    return head


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
