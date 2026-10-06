# Production server setup

## Requirements

- Python 3.10 or later.
- The server packages listed in `requirements-server.txt`: FastAPI 0.110+ and Uvicorn 0.29+.
- A persistent writable data directory for SQLite data and/or local uploaded archives. PostgreSQL and S3-compatible backends are also available; see [Server configuration](configuration.md) for backend selection.
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

For all settings, configuration precedence, secret files, and `.env` examples, see [Server configuration](configuration.md). `Registration mode` is stored in the database after it is changed in the admin UI or API; that persisted mode takes precedence over `AIHUB_OPEN_REGISTRATION`.

## First run and signup policy

The first account registered on an empty database becomes an active admin. Later registrations follow the signup mode:

- `open`: new accounts become active users.
- `approval`: new accounts are pending until an admin activates them.
- `closed`: registration is rejected.

An admin can view or update the mode in the web UI's admin area, or use `GET /api/v1/admin/settings/registration` and `PUT /api/v1/admin/settings/registration` with `{"mode":"open"}`, `{"mode":"approval"}`, or `{"mode":"closed"}`. These endpoints require an admin bearer token. The first account cannot be configured in advance via the environment to bypass initial registration; register it before opening the service to general users.

## Data, backups, and upgrades

With the default SQLite and local-storage backends, the data directory contains:

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

Back up `files/` when using local storage. With PostgreSQL or S3 storage, back up those external services using their own procedures. Keep metadata and archive backups consistent; restore them while the server is stopped. Keep backups separate from active storage.

For upgrades, stop the service gracefully, preserve a database and files backup, update the source/deployment image, and start the service again. Database initialization applies the current schema and the package `readme` column migration at startup. To change database or storage backend, see [Migrating between backends](configuration.md#migrating-between-backends). Verify `/api/v1/healthz` and run a CLI check after upgrade.

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

## Choosing backends

Choose SQLite/PostgreSQL, memory/Redis/none, and local/S3 independently in `.env`. See [Server configuration](configuration.md) for defaults, every setting, backend-specific guidance, Docker profiles, and examples.

## Docker

The repository's Dockerfile uses Python 3.12, installs `requirements-all.txt` (including PostgreSQL, Redis, and S3 backend support), runs as the unprivileged `aihub` user, and starts the server on `0.0.0.0:8000` with `/data` as its data directory. It copies `aihub/` and `docs/`, exposes port 8000, persists `/data`, and has a `/api/v1/healthz` health check.

The Compose file runs the app with SQLite, memory cache, and local files by default. Optional profiles start PostgreSQL (`postgres`), Redis (`redis`), and MinIO plus bucket initialization (`minio`). Set each profile's secret once in `.env`; the app's connection settings are constructed by Compose. The `aihub` service does not declare `depends_on`, so start-up ordering/readiness is not guaranteed by Compose. For external services, supply their explicit settings; see [Server configuration](configuration.md), including the current S3 endpoint default caveat. For example:

```sh
cp .env.example .env
# Uncomment and set strong values for each enabled service in .env.
docker compose --profile postgres --profile redis --profile minio up -d --build
```

Before starting, check the effective configuration with `docker compose config`. Compose `.env` values are not expanded inside the env file itself; do not put `${VAR}` references in those values. To use external services, leave profiles disabled and configure the real URLs and storage settings in `.env`; see [Server configuration](configuration.md).

Compose does not configure a TLS reverse proxy; put one in front of the container for public use. Confirm the public URL using `GET /api/v1/meta` and `GET /api/v1/resolve`.

## Air-gapped networks

The web interface loads the pinned Preact/htm bundle from `cdn.jsdelivr.net` in `aihub/server/static/index.html`, with a Subresource Integrity hash. The Content Security Policy in `aihub/server/main.py` permits scripts only from `'self'` and `https://cdn.jsdelivr.net`. An offline deployment must serve a local copy of the bundle by changing its script URL in `index.html` and updating the CSP in `main.py` to allow the local source instead.

## Security checklist

- Serve the public site over TLS and set `AIHUB_PUBLIC_URL` to its HTTPS URL.
- Restrict filesystem access to the service account; back up SQLite and local `files/` when those backends are enabled, or use the appropriate backup procedures for PostgreSQL and S3.
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
| Upload returns `413` | Raise the limit in Admin > Settings > Uploads (or `AIHUB_MAX_UPLOAD_MB`) and set nginx `client_max_body_size` at least as high; restart/reload both. |
| Login returns `429` | The in-process limiter saw at least eight failed attempts for that IP and username within five minutes. Wait for the window to reset and verify credentials. |
| Usage hook is not firing | Run `aihub doctor`, then `aihub hooks install`. Confirm the package component is registered with the tool and the CLI can reach the hub. Codex does not provide a general tool-call hook. |
