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

from ..core.release import VERSION
from .cache import init_cache
from .sync import Sync
from .backup import Backup
from .config import Settings
from .db import init_db
from .repos import EventBuffer, Repos
from .routers import r as api
from . import builtin

HERE = os.path.dirname(os.path.abspath(__file__))

INSTALL_SH = """#!/bin/sh
set -e
HUB="{url}"
if [ -t 1 ] && [ -z "$NO_COLOR" ] && [ "$TERM" != "dumb" ]; then
  B=$(printf '\\033[38;5;75m'); G=$(printf '\\033[38;5;41m'); Y=$(printf '\\033[38;5;214m'); R=$(printf '\\033[38;5;203m'); D=$(printf '\\033[2m'); N=$(printf '\\033[0m')
  OKM="$G+$N"
else
  B=""; G=""; Y=""; R=""; D=""; N=""; OKM="+"
fi
step() {{ printf '  %s->%s %s\\n' "$B" "$N" "$1"; }}
ok() {{ printf '  %s %s\\n' "$OKM" "$1"; }}
printf '\\n  %saihub%s %sinstaller%s\\n\\n' "$B" "$N" "$D" "$N"
PY=""
step "Looking for Python 3.9+"
for c in python3 python; do
  p=$(command -v $c 2>/dev/null) || continue
  if "$p" -c 'import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)' >/dev/null 2>&1; then PY="$p"; break; fi
done
if [ -z "$PY" ]; then
  printf '  %sx Python 3.9+ is required but was not found.%s\\n' "$R" "$N"
  case "$(uname -s)" in
    Darwin) echo "    Install it with:  brew install python   (or https://www.python.org/downloads/macos/)" ;;
    *) echo "    Install it with your package manager, e.g.  sudo apt install python3  /  sudo dnf install python3" ;;
  esac
  exit 1
fi
ok "Using $PY"
mkdir -p "$HOME/.aihub/bin"
step "Downloading the CLI from $HUB"
curl -fsSL "$HUB/cli/aihub.pyz" -o "$HOME/.aihub/bin/aihub.pyz"
printf '#!/bin/sh\\nexec %s "$HOME/.aihub/bin/aihub.pyz" "$@"\\n' "$PY" > "$HOME/.aihub/bin/aihub"
chmod +x "$HOME/.aihub/bin/aihub"
"$HOME/.aihub/bin/aihub" config set hub "$HUB" >/dev/null || true
ok "Installed to $HOME/.aihub/bin"
step "Connecting your AI tools"
"$HOME/.aihub/bin/aihub" welcome -y < /dev/null || true   # usage hooks + packaging skill for detected tools
# Put ~/.aihub/bin on PATH for future shells. Guard: ask first when a person is at the terminal (default yes),
# add directly when non-interactive (CI), and skip entirely with AIHUB_NO_MODIFY_PATH=1.
BIN="$HOME/.aihub/bin"
case "$(basename "${{SHELL:-sh}}")" in
  zsh) RC="${{ZDOTDIR:-$HOME}}/.zshrc"; LINE='export PATH="$HOME/.aihub/bin:$PATH"' ;;
  bash) if [ -f "$HOME/.bash_profile" ] || [ "$(uname -s)" = "Darwin" ]; then RC="$HOME/.bash_profile"; else RC="$HOME/.bashrc"; fi; LINE='export PATH="$HOME/.aihub/bin:$PATH"' ;;
  fish) RC="$HOME/.config/fish/config.fish"; LINE='fish_add_path $HOME/.aihub/bin' ;;
  *) RC="$HOME/.profile"; LINE='export PATH="$HOME/.aihub/bin:$PATH"' ;;
esac
case ":$PATH:" in *":$BIN:"*) ONPATH=1 ;; *) ONPATH=0; export PATH="$BIN:$PATH" ;; esac
if [ -f "$RC" ] && grep -q '# >>> aihub' "$RC" 2>/dev/null; then
  ok "$BIN is already set up in $RC"
elif [ -n "$AIHUB_NO_MODIFY_PATH" ]; then
  printf '  %sSkipped PATH change.%s Add this to %s:  %s\\n' "$Y" "$N" "$RC" "$LINE"
else
  GO=y
  if [ -t 1 ] && [ -r /dev/tty ]; then
    printf '  Add %s to your PATH by editing %s? [Y/n] ' "$BIN" "$RC"
    read GO < /dev/tty || GO=y
  fi
  case "$GO" in
    n|N|no|NO) printf '  %sNot changed.%s To do it yourself add to %s:  %s\\n' "$Y" "$N" "$RC" "$LINE" ;;
    *) mkdir -p "$(dirname "$RC")" 2>/dev/null
       if printf '\\n# >>> aihub\\n%s\\n# <<< aihub\\n' "$LINE" >> "$RC" 2>/dev/null; then ok "Added $BIN to PATH in $RC"
       else printf '  %sCould not write %s.%s Add this yourself:  %s\\n' "$Y" "$RC" "$N" "$LINE"; fi ;;
  esac
fi
printf '  %sOpen a new terminal, then run:%s  aihub\\n\\n' "$D" "$N"
"""

INSTALL_PS1 = r"""$ErrorActionPreference = "Stop"
$Hub = "__HUB__"
function Say-Step($t) { Write-Host "  -> " -ForegroundColor Cyan -NoNewline; Write-Host $t }
function Say-Ok($t) { Write-Host "  + " -ForegroundColor Green -NoNewline; Write-Host $t }
Write-Host ""
Write-Host "  aihub " -ForegroundColor Cyan -NoNewline; Write-Host "installer" -ForegroundColor DarkGray
Write-Host ""
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072 } catch {}

# A candidate only counts if it really runs Python >= 3.9. This rejects the Microsoft Store
# "python.exe" alias in WindowsApps, which is on PATH but prints "Python was not found".
function Test-Python($exe, $pre) {
  try {
    $ErrorActionPreference = "Continue"
    $v = & $exe @pre -c "import sys;print(sys.version_info[0]*100+sys.version_info[1])" 2>$null
    return ($LASTEXITCODE -eq 0 -and [int]"$v" -ge 309)
  } catch { return $false }
}

function Find-Python {
  $c = @()
  foreach ($n in "py", "python", "python3") {
    foreach ($cmd in @(Get-Command $n -All -ErrorAction SilentlyContinue)) {
      if ($cmd.Source -and $cmd.Source -notlike "*\WindowsApps\*") { $c += ,@($cmd.Source, @()) }
    }
  }
  foreach ($root in @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles\Python", "${env:ProgramFiles(x86)}\Python")) {
    if (Test-Path $root) {
      Get-ChildItem $root -Directory -Filter "Python3*" | Sort-Object Name -Descending | ForEach-Object {
        $exe = Join-Path $_.FullName "python.exe"
        if (Test-Path $exe) { $c += ,@($exe, @()) }
      }
    }
  }
  foreach ($x in $c) {
    $exe = $x[0]; $pre = $x[1]
    if ((Split-Path $exe -Leaf) -eq "py.exe") { $pre = @("-3") }
    if (Test-Python $exe $pre) {
      if ($pre.Count) { return (& $exe @pre -c "import sys;print(sys.executable)" 2>$null).Trim() }
      return $exe
    }
  }
  return $null
}

function Install-Python {
  Say-Step "Python 3.9+ not found, installing Python 3.12 for the current user"
  $winget = Get-Command winget -ErrorAction SilentlyContinue
  if ($winget) {
    try {
      & winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Null
      if (Find-Python) { return }
    } catch {}
  }
  $arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "amd64" }
  $url = "https://www.python.org/ftp/python/3.12.8/python-3.12.8-$arch.exe"
  $tmp = Join-Path $env:TEMP "python-installer.exe"
  Invoke-WebRequest $url -OutFile $tmp -UseBasicParsing
  $p = Start-Process $tmp -ArgumentList "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_test=0", "Include_launcher=0" -Wait -PassThru
  Remove-Item $tmp -Force -ErrorAction SilentlyContinue
  if ($p.ExitCode -ne 0) { throw "Python installer failed (exit $($p.ExitCode))" }
}

$Py = Find-Python
if (-not $Py) {
  try { Install-Python } catch { Write-Host "  Automatic Python install failed: $_" -ForegroundColor Yellow }
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
  $Py = Find-Python
}
if (-not $Py) {
  Write-Host "  x Could not find or install Python 3.9+. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then re-run this command." -ForegroundColor Red
  exit 1
}
Say-Ok "Using Python: $Py"

$Bin = Join-Path $env:USERPROFILE ".aihub\bin"
New-Item -ItemType Directory -Force -Path $Bin | Out-Null
Say-Step "Downloading the CLI from $Hub"
Invoke-WebRequest "$Hub/cli/aihub.pyz" -OutFile (Join-Path $Bin "aihub.pyz") -UseBasicParsing
Set-Content -Path (Join-Path $Bin "aihub.cmd") -Encoding ASCII -Value ('@echo off' + "`r`n" + '"' + $Py + '" "%~dp0aihub.pyz" %*')
# Extensionless twin for Git Bash / WSL-style shells (what Claude Code and Codex use on Windows): they ignore .cmd files.
$PyPosix = $Py -replace '\\', '/'
[IO.File]::WriteAllText((Join-Path $Bin "aihub"), ("#!/bin/sh`n" + 'exec "' + $PyPosix + '" "$(dirname "$0")/aihub.pyz" "$@"' + "`n"))
& "$Bin\aihub.cmd" config set hub $Hub | Out-Null
Say-Ok "Installed to $Bin"
Say-Step "Connecting your AI tools"
try { $null | & "$Bin\aihub.cmd" welcome -y } catch {}
$User = [Environment]::GetEnvironmentVariable("Path", "User")
if ($User -notlike "*$Bin*") { [Environment]::SetEnvironmentVariable("Path", "$User;$Bin", "User") }
$env:Path += ";$Bin"
Write-Host "  Open a new terminal, then run: " -NoNewline -ForegroundColor DarkGray; Write-Host "aihub" -ForegroundColor Cyan
Write-Host ""
"""


def create_app(settings: Settings = None) -> FastAPI:
    s = settings or Settings.from_env()
    os.makedirs(s.data_dir, exist_ok=True)
    @contextlib.asynccontextmanager
    async def lifespan(_app):
        logging.getLogger("aihub").info("backends: %s", json.dumps(s.describe()))
        if s.sync_enabled:
            _app.state.sync.start()
            _app.state.backup.start()
        yield
        _app.state.sync.close()
        _app.state.backup.close()
        _app.state.events.close()  # drain queued usage events on shutdown
        try:
            db.close()
        except Exception:
            pass

    app = FastAPI(title="AI Hub", version=VERSION, lifespan=lifespan)
    db = init_db(s)
    app.state.settings = s
    app.state.repos = Repos(db)
    app.state.cache = init_cache(s)
    app.state.events = EventBuffer(app.state.repos, s.event_flush_rows, s.event_flush_secs)
    app.state.sync = Sync(app.state.repos, s)
    app.state.backup = Backup(app.state.repos, s, app.state.sync)
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
        h.setdefault("Content-Security-Policy", CSP)
        h.setdefault("X-Frame-Options", "DENY")
        p = request.url.path
        if p.startswith("/static/"):
            h["Cache-Control"] = "no-cache"  # always revalidate (ETag); updates show on next load
        elif p.startswith("/api/") and "cache-control" not in h:
            h["Cache-Control"] = "no-store" if request.method != "GET" else "no-cache"
        return resp

    def _cli_url():
        """Admin-set CLI endpoint (Settings > Site), else the configured public URL."""
        return (app.state.repos.setting("cli_endpoint") or s.public_url).rstrip("/")

    @app.get("/install.sh", response_class=PlainTextResponse)
    def install_sh():
        return INSTALL_SH.format(url=_cli_url())

    @app.get("/install.ps1", response_class=PlainTextResponse)
    def install_ps1():
        return INSTALL_PS1.replace("__HUB__", _cli_url())

    def _pyz():
        from ..core import zipapp
        out = os.path.join(s.data_dir, "aihub.pyz")
        src = os.path.dirname(HERE)
        newest = max(os.path.getmtime(os.path.join(d, f)) for d, _, fs in os.walk(src) for f in fs if f.endswith(".py"))
        if not os.path.exists(out) or os.path.getmtime(out) < newest:
            zipapp.build(out)
        return out

    @app.get("/cli/version")
    def cli_version():
        """Polled by `aihub` for auto-update. `sha256` lets the client verify the zipapp before replacing itself."""
        import hashlib
        out = _pyz()
        with open(out, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        return {"version": VERSION, "sha256": sha, "url": "/cli/aihub.pyz", "size": os.path.getsize(out)}

    @app.get("/cli/aihub.pyz")
    def cli_pyz():
        return FileResponse(_pyz(), media_type="application/octet-stream")

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
    ap.add_argument("--ssl-cert", default=None, help="TLS certificate (PEM). With --ssl-key the server speaks HTTPS; without them it speaks plain HTTP "
                                                     "(env AIHUB_SSL_CERT)")
    ap.add_argument("--ssl-key", default=None, help="TLS private key (PEM) for --ssl-cert (env AIHUB_SSL_KEY)")
    ap.add_argument("--env-file", default=None, help="path to a .env file (default: ./.env or <data>/.env)")
    ap.add_argument("--recover", default=None, choices=["ask", "local", "remote", "latest", "skip"],
                    help="when the local database and the git backup differ: ask (default), keep local, use the backup, take the latest, or skip the check "
                         "(env AIHUB_RECOVER)")
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
    cert, key = a.ssl_cert or os.environ.get("AIHUB_SSL_CERT"), a.ssl_key or os.environ.get("AIHUB_SSL_KEY")
    if bool(cert) != bool(key):
        raise SystemExit("aihub: --ssl-cert and --ssl-key must be given together")
    for f in (cert, key):
        if f and not os.path.isfile(f):
            raise SystemExit("aihub: TLS file not found: " + f)
    if not a.public_url and "AIHUB_PUBLIC_URL" not in os.environ and s.public_url == Settings().public_url:
        s.public_url = "%s://%s:%d" % ("https" if cert else "http", "localhost" if host in ("0.0.0.0", "127.0.0.1") else host, port)
    logging.basicConfig(level=s.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if a.check:
        raise SystemExit(check(s))
    from . import recover
    recover.run(s, a.recover)
    uvicorn.run(create_app(s), host=host, port=port, ssl_certfile=cert, ssl_keyfile=key)


def check(s):
    """`python -m aihub.server --check`: report which backends are configured and whether each is reachable."""
    print(json.dumps(s.describe(), indent=2))
    ok = True
    for name, fn in (("database", lambda: init_db(s).conn().execute("SELECT 1").fetchone()),
                     ("cache", lambda: init_cache(s).health() or (_ for _ in ()).throw(RuntimeError("unhealthy"))),
                     ):
        try:
            fn()
            print("  ok    %s" % name)
        except Exception as e:
            ok = False
            print("  FAIL  %s: %s" % (name, e))
    return 0 if ok else 1
