"""Serving the hub under a path prefix behind a reverse proxy (https://host/aihub/...).

Every URL the server hands out (logo, avatars, docs, the page's own scripts) and every URL the web app calls must carry the prefix, or the
browser asks the host root (`/api/v1/logo` instead of `/aihub/api/v1/logo`). The prefix comes from, in order:
  1. AIHUB_BASE_PATH                      (explicit)
  2. the path of AIHUB_PUBLIC_URL / the admin `cli_endpoint`   (https://host/aihub -> /aihub)
  3. the proxy's X-Forwarded-Prefix header, ONLY when AIHUB_TRUST_PROXY_PREFIX=1 and 1 and 2 are empty (any client can send it, so by default it is ignored)
The proxy may strip the prefix before forwarding or keep it: a request that still starts with a configured prefix is accepted either way."""
import contextvars
import re
import time
import urllib.parse

from starlette.concurrency import run_in_threadpool

_SAFE = re.compile(r"/[A-Za-z0-9._~\-]+(?:/[A-Za-z0-9._~\-]+)*")
MAX_LEN, MAX_SEGMENTS = 64, 4
_current = contextvars.ContextVar("aihub_base_path", default="")


def clean(p):
    """'/aihub/' -> '/aihub'. Only a plain path is accepted; anything else is REJECTED (-> ''), never repaired into something else:
    '//evil.com', 'javascript:', 'aihub' (no leading slash), quotes, spaces, '?', '#', '..', backslashes."""
    p = str(p or "")
    if len(p) > 1:
        p = p.rstrip("/")
    if len(p) > MAX_LEN or not _SAFE.fullmatch(p) or p.count("/") > MAX_SEGMENTS or ".." in p.split("/"):
        return ""
    return p


def from_url(url):
    return clean(urllib.parse.urlsplit(url or "").path)


def configured(settings, repos=None):
    """The prefix fixed by configuration ('' when there is none)."""
    p = clean(settings.env_get("AIHUB_BASE_PATH"))
    if not p and repos is not None:
        try:
            p = from_url(repos.setting("cli_endpoint", ""))
        except Exception:
            p = ""
    return p or from_url(settings.public_url)


def current():
    """The prefix of the request being handled ('' outside a request)."""
    return _current.get()


def url(path):
    """A root-relative server path ('/api/v1/logo') with the current prefix in front."""
    return current() + path if path.startswith("/") else path


class Middleware:
    """Pure ASGI: remembers the prefix for this request (for url()) and tells Starlette about a configured prefix the proxy did not remove.

    `get_configured` may read the database (the admin `cli_endpoint`), so it never runs on the event loop and never per request: the answer
    is cached for TTL seconds and refreshed in a worker thread, which hands its database connection back (`done`). Until the first answer
    arrives, and whenever a refresh fails, the last known value is used."""
    TTL = 30.0

    def __init__(self, inner, get_configured, done=None, trust_header=lambda: False):
        self.inner, self.get, self.done, self.trust_header = inner, get_configured, done, trust_header
        self.base, self.at = "", 0.0

    def _refresh(self):
        try:
            return self.get()
        finally:
            if self.done:
                self.done()

    async def configured(self):
        now = time.monotonic()
        if now - self.at > self.TTL:
            self.at = now                                   # one refresher at a time: concurrent requests keep the cached value
            try:
                self.base = await run_in_threadpool(self._refresh)
            except Exception:
                pass
        return self.base

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.inner(scope, receive, send)
        base = await self.configured()
        path = scope.get("path", "")
        if base and (path == base or path.startswith(base + "/")):
            scope = dict(scope, root_path=base)            # path stays full: Starlette cuts root_path off itself when it routes
        if not base and self.trust_header():
            for k, v in scope.get("headers", []):
                if k == b"x-forwarded-prefix":
                    base = clean(v.decode("latin-1"))
                    break
        token = _current.set(base)
        try:
            await self.inner(scope, receive, send)
        finally:
            _current.reset(token)
