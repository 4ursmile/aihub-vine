import argparse
import os
import tempfile

import contextlib
import logging
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from .cache import init_cache
from .config import Settings
from .db import init_db
from .repos import EventBuffer, Repos
from .routers import r as api
from .storage import LocalStorage

HERE = os.path.dirname(os.path.abspath(__file__))

INSTALL_SH = """#!/bin/sh
set -e
HUB="{url}"
PY=$(command -v python3 || command -v python)
[ -z "$PY" ] && echo "python3 required" && exit 1
mkdir -p "$HOME/.aihub/bin"
curl -fsSL "$HUB/cli/aihub.pyz" -o "$HOME/.aihub/bin/aihub.pyz"
printf '#!/bin/sh\\nexec %s "$HOME/.aihub/bin/aihub.pyz" "$@"\\n' "$PY" > "$HOME/.aihub/bin/aihub"
chmod +x "$HOME/.aihub/bin/aihub"
"$HOME/.aihub/bin/aihub" config set hub "$HUB" >/dev/null || true
"$HOME/.aihub/bin/aihub" hooks install || true   # usage hooks for detected tools (Claude Code / OpenCode)
echo "Installed. Add to PATH:  export PATH=\\"$HOME/.aihub/bin:$PATH\\""
"""


def create_app(settings: Settings = None) -> FastAPI:
    s = settings or Settings.from_env()
    os.makedirs(s.data_dir, exist_ok=True)
    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield
        _app.state.events.close()  # drain queued usage events on shutdown

    app = FastAPI(title="AI Hub", version="0.1.0", lifespan=lifespan)
    db = init_db(s)
    app.state.settings = s
    app.state.repos = Repos(db)
    app.state.cache = init_cache(s)
    app.state.storage = LocalStorage(os.path.join(s.data_dir, "files"))
    app.state.events = EventBuffer(app.state.repos, s.event_flush_rows, s.event_flush_secs)
    app.add_middleware(GZipMiddleware, minimum_size=800)
    app.include_router(api)

    CSP = ("default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline'; "
           "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

    log = logging.getLogger("aihub")

    @app.middleware("http")
    async def headers(request: Request, call_next):
        t0 = time.time()
        try:
            resp = await call_next(request)
        except Exception:
            log.exception("unhandled error %s %s", request.method, request.url.path)
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "internal error"}, status_code=500)
        if request.url.path.startswith("/api/") and request.url.path not in ("/api/v1/healthz", "/api/v1/readyz"):
            log.info("%s %s %d %.0fms", request.method, request.url.path, resp.status_code, (time.time() - t0) * 1000)
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "same-origin")
        if not request.url.path.startswith("/files/"):
            h.setdefault("Content-Security-Policy", CSP)
            h.setdefault("X-Frame-Options", "DENY")
        p = request.url.path
        if p.startswith("/static/"):
            h["Cache-Control"] = "no-cache"  # always revalidate (ETag); updates show on next load
        elif p.startswith("/api/") and "cache-control" not in h:
            h["Cache-Control"] = "no-store" if request.method != "GET" else "no-cache"
        return resp

    @app.get("/files/{name}/{filename}")
    def download(name: str, filename: str, request: Request):
        from .deps import current_user, viewer_of
        u = current_user(request)
        repos = app.state.repos
        p = repos.package(name)
        # one error for "missing" and "not allowed", so private package names can't be probed
        if not p or not app.state.storage.exists(name, filename):
            raise HTTPException(404)
        lvl = repos.access_level(p["id"], viewer_of(repos, u))
        if lvl is None:
            raise HTTPException(401 if not u else 404, "sign in required" if not u else "not found")
        if not u and (repos.setting("public_install") or "0") != "1":
            raise HTTPException(401, "sign in required")
        st = app.state.storage
        if p:
            for v in app.state.repos.versions(p["id"]):
                if v["file"] == filename:
                    app.state.repos.version_download(p["id"], v["version"])
        return FileResponse(st.path(name, filename), filename=filename,
                            headers={"Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff"})

    @app.get("/install.sh", response_class=PlainTextResponse)
    def install_sh():
        return INSTALL_SH.format(url=s.public_url.rstrip("/"))

    @app.get("/cli/version")
    def cli_version():
        return {"version": "0.1.0"}

    @app.get("/cli/aihub.pyz")
    def cli_pyz():
        from ..core import zipapp
        out = os.path.join(s.data_dir, "aihub.pyz")
        src = os.path.dirname(HERE)
        newest = max(os.path.getmtime(os.path.join(d, f)) for d, _, fs in os.walk(src) for f in fs if f.endswith(".py"))
        if not os.path.exists(out) or os.path.getmtime(out) < newest:
            zipapp.build(out)
        return FileResponse(out, media_type="application/octet-stream")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return FileResponse(os.path.join(HERE, "static", "index.html"),
                            headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
    return app


def cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data", default=None)
    ap.add_argument("--public-url", default=None)
    a = ap.parse_args()
    import logging
    import uvicorn
    logging.basicConfig(level=os.environ.get("AIHUB_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    s = Settings.from_env(data_dir=a.data, public_url=a.public_url)
    if not a.public_url and "AIHUB_PUBLIC_URL" not in os.environ:
        s.public_url = "http://%s:%d" % ("localhost" if a.host in ("0.0.0.0", "127.0.0.1") else a.host, a.port)
    uvicorn.run(create_app(s), host=a.host, port=a.port)
