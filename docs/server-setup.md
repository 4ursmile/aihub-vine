# Production server setup

## Requirements

- Python 3.10 or later.
- The server packages listed in `requirements-server.txt`: FastAPI 0.110+ and Uvicorn 0.29+.
- A persistent writable data directory with room for SQLite data and uploaded archives.
- A TLS-terminating reverse proxy for public deployments.

## Install and run

From a source checkout:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-server.txt
python -m aihub.server --host 127.0.0.1 --port 8000 --data /var/lib/aihub --public-url https://hub.example.com
```

The options are `--host` (default `127.0.0.1`), `--port` (default `8000`), `--data` (default `./aihub-data`), and `--public-url`. If neither `--public-url` nor `AIHUB_PUBLIC_URL` is set, the public URL is derived from host and port. Set it to the exact externally reachable scheme, host, and optional path that clients use. AI Hub embeds it in package download URLs returned by `/api/v1/resolve` and in the generated `/install.sh`.

Check the service with `GET https://hub.example.com/api/v1/healthz`. It returns `{"ok":true}`.

## Environment variables

Each setting is read from an `AIHUB_`-prefixed uppercase name. Command-line `--data` and `--public-url`, when given, override the corresponding environment variables.

| Variable | Default | Meaning |
| --- | --- | --- |
| `AIHUB_DATA_DIR` | `./aihub-data` | Data root. Contains `aihub.db` and uploaded package files. |
| `AIHUB_PUBLIC_URL` | `http://localhost:8000` | Public base URL embedded in download and CLI installation URLs. If unset, it is derived from `--host` and `--port`. The `--public-url` flag overrides it. |
| `AIHUB_CACHE_BACKEND` | `memory` | Cache implementation. `memory` uses process memory; any other value selects the null cache. |
| `AIHUB_EVENT_FLUSH_ROWS` | `50` | Number of queued usage events that wakes the database flusher. |
| `AIHUB_EVENT_FLUSH_SECS` | `5.0` | Maximum interval in seconds between usage-event flush attempts. |
| `AIHUB_MAX_UPLOAD_MB` | `200` | Maximum request archive size in MiB. Oversized uploads return HTTP 413. |
| `AIHUB_OPEN_REGISTRATION` | `true` | Default signup mode when no persisted admin signup setting exists. Boolean values `1`, `true`, and `yes` (case-insensitive) are true; other values are false. |
| `AIHUB_LOG_LEVEL` | `INFO` | Log level for the server process (`DEBUG`, `INFO`, `WARNING`, `ERROR`). Read when started with `python -m aihub.server`. |

Registration mode is stored in the database after it is changed in the admin UI or API. That persisted mode takes precedence over `AIHUB_OPEN_REGISTRATION`.

## First run and signup policy

The first account registered on an empty database becomes an active admin. Later registrations follow the signup mode:

- `open`: new accounts become active users.
- `approval`: new accounts are pending until an admin activates them.
- `closed`: registration is rejected.

An admin can view or update the mode in the web UI's admin area, or use `GET /api/v1/admin/settings/registration` and `PUT /api/v1/admin/settings/registration` with `{"mode":"open"}`, `{"mode":"approval"}`, or `{"mode":"closed"}`. These endpoints require an admin bearer token. The first account cannot be configured in advance via the environment to bypass initial registration; register it before opening the service to general users.

## Data, backups, and upgrades

The data directory contains:

```text
<data-dir>/
  aihub.db       SQLite metadata, accounts, tokens, settings, audit and events
  files/         Uploaded archives, organized by package name
  aihub.pyz      Generated CLI zipapp (created on first download)
```

SQLite connections use WAL mode. Back up the database using SQLite's backup API rather than copying only the live database file:

```sh
sqlite3 /var/lib/aihub/aihub.db ".backup '/var/backups/aihub.db'"
```

Back up the `files/` directory as well. Keep database and package-file backups consistent; restore both while the server is stopped. Restore the backup database to `aihub.db` in the data directory and restore `files/` to the matching location. Keep the backup separate from active storage.

For upgrades, stop the service gracefully, preserve a database and files backup, update the source/deployment image, and start the service again. Database initialization applies the current schema and the package `readme` column migration at startup. Verify `/api/v1/healthz` and run a CLI check after upgrade.

## systemd example

Adjust the user, checkout path, data path, and public URL to your deployment. The virtual environment must be created and dependencies installed at `/opt/aihub/.venv`.

```ini
[Unit]
Description=AI Hub registry
After=network.target

[Service]
Type=simple
User=aihub
Group=aihub
WorkingDirectory=/opt/aihub
Environment=AIHUB_DATA_DIR=/var/lib/aihub
Environment=AIHUB_PUBLIC_URL=https://hub.example.com
ExecStart=/opt/aihub/.venv/bin/python -m aihub.server --host 127.0.0.1 --port 8000 --data /var/lib/aihub --public-url https://hub.example.com
Restart=on-failure
KillSignal=SIGTERM
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
```

Uvicorn runs one worker by default. Login throttling is held in process memory, so use a single worker unless you add a shared rate limiter. Multiple worker processes would each have separate throttling counters.

Usage events are queued in memory and flushed in batches every `AIHUB_EVENT_FLUSH_SECS` or when at least `AIHUB_EVENT_FLUSH_ROWS` are queued. Graceful application shutdown drains queued events. Stop with `SIGTERM` and allow the process to exit; `SIGKILL` prevents graceful draining.

## Nginx reverse proxy

Terminate TLS at the proxy, preserve the external host and scheme headers, and set the request body limit to match `AIHUB_MAX_UPLOAD_MB`. The example below assumes the default 200 MiB limit. Replace both values together if you change the setting.

```nginx
server {
    listen 443 ssl;
    server_name hub.example.com;

    # TLS certificate configuration omitted.
    client_max_body_size 200m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

The application uses its configured `AIHUB_PUBLIC_URL` to generate client-facing links. Forwarded headers do not replace that setting.

## Docker

The repository currently includes `Dockerfile` and `docker-compose.yml`. The image runs Python 3.12 and starts the server with `python -m aihub.server --host 0.0.0.0 --port 8000 --data /data`. Compose exposes port 8000, persists a named `aihub-data` volume at `/data`, and declares `AIHUB_PUBLIC_URL` (default `http://localhost:8000`). The image also has a health check for `/api/v1/healthz`.

Set the public URL with the `AIHUB_PUBLIC_URL` environment variable (Compose reads it from your shell or an `.env` file). The `--public-url` flag, when given, takes precedence.

```sh
# Compose
AIHUB_PUBLIC_URL=https://hub.example.com docker compose up -d --build

# Plain docker
docker build -t aihub .
docker run -d --name aihub -p 8000:8000 -v aihub-data:/data \
  -e AIHUB_PUBLIC_URL=https://hub.example.com aihub
```

Compose does not configure a TLS reverse proxy; put one in front of the container for public use. Verify the configured URL using `GET /api/v1/meta` and `GET /api/v1/resolve`.

## Air-gapped networks

The web interface loads the pinned Preact/htm bundle from `cdn.jsdelivr.net` in `aihub/server/static/index.html`, with a Subresource Integrity hash. The Content Security Policy in `aihub/server/main.py` permits scripts only from `'self'` and `https://cdn.jsdelivr.net`. An offline deployment must serve a local copy of the bundle by changing its script URL in `index.html` and updating the CSP in `main.py` to allow the local source instead.

## Security checklist

- Serve the public site over TLS and set `AIHUB_PUBLIC_URL` to its HTTPS URL.
- Restrict filesystem access to the service account; back up both SQLite and `files/`.
- Treat bearer tokens as credentials. Keep CLI credentials private and revoke unused API tokens.
- Keep the Content Security Policy and other response security headers enabled.
- Set `AIHUB_MAX_UPLOAD_MB` to an acceptable limit and align the reverse proxy's `client_max_body_size`.
- Keep the login limiter's single-worker constraint in mind; add shared throttling before scaling workers.
- Use graceful shutdown so queued usage events can drain.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| CLI reports `cannot reach hub` | Confirm `aihub config` shows the correct hub URL, that DNS/TLS and proxy routing work, and that `GET /api/v1/healthz` succeeds. Run `aihub doctor`. |
| CLI reports `sha256 mismatch for download` | The archive received differs from the hash returned by the hub. Check proxy/CDN caching and that metadata and `files/` were restored from a consistent backup. Retry after correcting the source. |
| Upload returns `413` | Increase `AIHUB_MAX_UPLOAD_MB` and set nginx `client_max_body_size` at least as high; restart/reload both. |
| Login returns `429` | The in-process limiter saw at least eight failed attempts for that IP and username within five minutes. Wait for the window to reset and verify credentials. |
| Usage hook is not firing | Run `aihub doctor`, then `aihub hooks install`. Confirm the package component is registered with the tool and the CLI can reach the hub. Codex does not provide a general tool-call hook. |
