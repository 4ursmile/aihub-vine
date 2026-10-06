import argparse
import os
import tempfile

import contextlib
import json
import logging
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.background import BackgroundTask
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from .cache import init_cache
from .config import Settings
from .db import init_db
from .repos import EventBuffer, Repos
from .routers import r as api
from .storage import init_storage

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

INSTALL_PS1 = r"""$ErrorActionPreference = "Stop"
$Hub = "__HUB__"
$Py = (Get-Command python3, python, py -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $Py) { Write-Host "Python 3 required"; exit 1 }
$Bin = Join-Path $env:USERPROFILE ".aihub\bin"
New-Item -ItemType Directory -Force -Path $Bin | Out-Null
Invoke-WebRequest "$Hub/cli/aihub.pyz" -OutFile (Join-Path $Bin "aihub.pyz") -UseBasicParsing
Set-Content -Path (Join-Path $Bin "aihub.cmd") -Encoding ASCII -Value ('@echo off' + "`r`n" + '"' + $Py + '" "%~dp0aihub.pyz" %*')
& "$Bin\aihub.cmd" config set hub $Hub | Out-Null
try { & "$Bin\aihub.cmd" hooks install } catch {}
$User = [Environment]::GetEnvironmentVariable("Path", "User")
if ($User -notlike "*$Bin*") { [Environment]::SetEnvironmentVariable("Path", "$User;$Bin", "User") }
Write-Host "Installed. Open a new terminal to use: aihub"
"""


def create_app(settings: Settings = None) -> FastAPI:
    s = settings or Settings.from_env()
    os.makedirs(s.data_dir, exist_ok=True)
    @contextlib.asynccontextmanager
    async def lifespan(_app):
        logging.getLogger("aihub").info("backends: %s", json.dumps(s.describe()))
        yield
        _app.state.events.close()  # drain queued usage events on shutdown
        try:
            db.close()
        except Exception:
            pass

    app = FastAPI(title="AI Hub", version="0.1.0", lifespan=lifespan)
    db = init_db(s)
    app.state.settings = s
    app.state.repos = Repos(db)
    app.state.cache = init_cache(s)
    app.state.storage = init_storage(s)
    app.state.events = EventBuffer(app.state.repos, s.event_flush_rows, s.event_flush_secs)
    app.add_middleware(GZipMiddleware, minimum_size=800)
    app.include_router(api)

    class DbUnitOfWork:
        """Pure ASGI middleware: one database unit per request, always ended - even when the client disconnects or the handler raises.
        (Postgres: borrows one pooled connection lazily and returns it here. SQLite: no-op.)"""
        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await self.inner(scope, receive, send)
            token = db.begin()
            try:
                await self.inner(scope, receive, send)
            finally:
                db.end(token)

    app.add_middleware(DbUnitOfWork)

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
        for v in repos.versions(p["id"]):
            if v["file"] == filename:
                repos.version_download(p["id"], v["version"])
        signed = st.url(name, filename, expires=300)       # S3: let the client fetch straight from the bucket
        if signed:
            return RedirectResponse(signed, status_code=302, headers={"Cache-Control": "no-store"})
        from starlette.responses import StreamingResponse
        f = st.open(name, filename)
        hdr = {"Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff", "Content-Length": str(st.size(name, filename)),
               "Content-Disposition": 'attachment; filename="%s"' % os.path.basename(filename)}
        return StreamingResponse(iter(lambda: f.read(1 << 20), b""), media_type="application/octet-stream", headers=hdr,
                                 background=BackgroundTask(f.close))

    @app.get("/install.sh", response_class=PlainTextResponse)
    def install_sh():
        return INSTALL_SH.format(url=s.public_url.rstrip("/"))

    @app.get("/install.ps1", response_class=PlainTextResponse)
    def install_ps1():
        return INSTALL_PS1.replace("__HUB__", s.public_url.rstrip("/"))

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
    ap = argparse.ArgumentParser(description="AI Hub server. Backends (database, cache, storage) are chosen in .env / environment.")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--data", default=None)
    ap.add_argument("--public-url", default=None)
    ap.add_argument("--env-file", default=None, help="path to a .env file (default: ./.env or <data>/.env)")
    ap.add_argument("--check", action="store_true", help="validate configuration, test every backend connection, then exit")
    a = ap.parse_args()
    import logging
    import uvicorn
    try:
        s = Settings.load(env_file=a.env_file, data_dir=a.data, public_url=a.public_url)
    except ValueError as e:
        raise SystemExit("aihub: " + str(e))
    host = a.host or os.environ.get("AIHUB_HOST") or "127.0.0.1"
    port = a.port or int(os.environ.get("AIHUB_PORT") or 8000)
    if not a.public_url and "AIHUB_PUBLIC_URL" not in os.environ and s.public_url == Settings().public_url:
        s.public_url = "http://%s:%d" % ("localhost" if host in ("0.0.0.0", "127.0.0.1") else host, port)
    logging.basicConfig(level=s.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if a.check:
        raise SystemExit(check(s))
    uvicorn.run(create_app(s), host=host, port=port)


def check(s):
    """`python -m aihub.server --check`: report which backends are configured and whether each is reachable."""
    print(json.dumps(s.describe(), indent=2))
    ok = True
    for name, fn in (("database", lambda: init_db(s).conn().execute("SELECT 1").fetchone()),
                     ("cache", lambda: init_cache(s).health() or (_ for _ in ()).throw(RuntimeError("unhealthy"))),
                     ("storage", lambda: init_storage(s).health() or (_ for _ in ()).throw(RuntimeError("bucket/dir not accessible")))):
        try:
            fn()
            print("  ok    %s" % name)
        except Exception as e:
            ok = False
            print("  FAIL  %s: %s" % (name, e))
    return 0 if ok else 1
