# Server configuration

AI Hub selects its database, cache, and package-file storage independently. The defaults run on one machine without external services: SQLite metadata, an in-process memory cache, and local package archives.

## Configuration sources

Settings use `AIHUB_` plus the uppercase setting name. The precedence is: command-line flags (`--data` and `--public-url`) > real process environment > the selected `.env` file > code defaults. `--env-file` is a selector for the file, not a setting override. The first existing file wins, in this order: `--env-file`, `$AIHUB_ENV_FILE`, `./.env`, then `<data dir>/.env`. The data directory for this final candidate is selected by `--data`, then the real `AIHUB_DATA_DIR`, then the default.

The selected `.env` file is not merged with later candidate files; the first existing file wins. Real environment variables override values from that file. The `.env` reader accepts `KEY=value`, optional `export `, and simple quotes; it does not expand `${VAR}` references.

For Docker or Kubernetes secrets, set a setting's `_FILE` counterpart, for example `AIHUB_PG_PASSWORD_FILE=/run/secrets/pg_password`. The loader reads and trims that file when the corresponding direct setting is not set. A direct environment value takes precedence over its `_FILE` value.

Validate settings and try the configured database, cache, and storage connections without serving requests:

```sh
python -m aihub.server --check
```

The check prints a redacted settings summary and an `ok` or `FAIL` result for each backend. It exits nonzero if a connection check fails. The database check initializes the schema, so it is not a read-only operation.

## All AIHUB settings

| Setting | Default | Meaning | Applies to |
| --- | --- | --- | --- |
| `AIHUB_DATA_DIR` | `./aihub-data` | Data root; local storage is under `<data dir>/files`, and the default SQLite database is under this directory. | Server, SQLite, local storage |
| `AIHUB_PUBLIC_URL` | `http://localhost:8000` | External base URL (set it to your reverse-proxy URL, including any path prefix). Embedded in download links, `/install.sh`, `/install.ps1`, the install commands shown on the web UI's Get started page, and the default `hub` the installer saves in the CLI. If the loaded value remains at the default and neither `--public-url` nor a process `AIHUB_PUBLIC_URL` is set, the server derives it from host and port. | Server |
| `AIHUB_MAX_UPLOAD_MB` | `200` | Maximum upload body size in MiB. | Server |
| `AIHUB_OPEN_REGISTRATION` | `true` | Default signup policy before an administrator changes the persisted signup setting. | Server/auth |
| `AIHUB_LOG_LEVEL` | `INFO` | Python server log level. | Server |
| `AIHUB_DB_BACKEND` | `sqlite` | `sqlite` or `postgres`. | Database |
| `AIHUB_SQLITE_PATH` | empty (uses `<data dir>/aihub.db`) | SQLite database file path. | SQLite |
| `AIHUB_DATABASE_URL` | empty | PostgreSQL DSN; when set, takes precedence over PostgreSQL connection parts. | PostgreSQL |
| `AIHUB_PG_HOST` | `localhost` | PostgreSQL host when no DSN is supplied. | PostgreSQL |
| `AIHUB_PG_PORT` | `5432` | PostgreSQL port. | PostgreSQL |
| `AIHUB_PG_NAME` | `aihub` | PostgreSQL database name. | PostgreSQL |
| `AIHUB_PG_USER` | `aihub` | PostgreSQL user. | PostgreSQL |
| `AIHUB_PG_PASSWORD` | empty | PostgreSQL password. | PostgreSQL |
| `AIHUB_PG_SSLMODE` | `prefer` | PostgreSQL libpq SSL mode used in the generated DSN. | PostgreSQL |
| `AIHUB_PG_POOL_SIZE` | `10` | Maximum pooled connections is at least 2 and otherwise this value. | PostgreSQL |
| `AIHUB_CACHE_BACKEND` | `memory` | `memory`, `redis`, or `none`. | Cache |
| `AIHUB_CACHE_TTL` | `20` seconds | Default cache lifetime; list/search routes also pass their own TTL. | Cache |
| `AIHUB_CACHE_MAX_ITEMS` | `2048` | Maximum in-process memory-cache entries. | Memory cache |
| `AIHUB_REDIS_URL` | empty | Redis URL, such as `redis://:password@host:6379/0`; use `rediss://` for TLS. Required for the Redis backend. | Redis cache |
| `AIHUB_REDIS_PREFIX` | `aihub:` | Prefix used for Redis cache keys and invalidation generations. | Redis cache |
| `AIHUB_STORAGE_BACKEND` | `local` | `local` or `s3`. | Package storage |
| `AIHUB_S3_BUCKET` | empty | Bucket name. Required for S3 storage. | S3-compatible storage |
| `AIHUB_S3_REGION` | `us-east-1` | S3 region. | S3-compatible storage |
| `AIHUB_S3_ENDPOINT` | empty | Custom endpoint; empty uses AWS's standard endpoint resolution. | S3-compatible storage |
| `AIHUB_S3_ACCESS_KEY` | empty | Optional explicit access key. When empty, boto3's normal credential chain is used. | S3-compatible storage |
| `AIHUB_S3_SECRET_KEY` | empty | Optional explicit secret key. | S3-compatible storage |
| `AIHUB_S3_PREFIX` | `packages/` | Prefix under which package archives are stored. | S3-compatible storage |
| `AIHUB_S3_PATH_STYLE` | `true` | Use path-style bucket addressing when true; useful for MinIO. | S3-compatible storage |
| `AIHUB_EVENT_FLUSH_ROWS` | `50` | Number of queued usage rows that triggers a database flush. | Usage event worker |
| `AIHUB_EVENT_FLUSH_SECS` | `5.0` seconds | Maximum wait between usage event flushes. | Usage event worker |
| `AIHUB_HOST` | `127.0.0.1` | Listen address when `--host` is omitted. Read from the real process environment, not `.env`. | Server startup |
| `AIHUB_PORT` | `8000` | Listen port when `--port` is omitted. Read from the real process environment, not `.env`. | Server startup |
| `AIHUB_ENV_FILE` | unset | Select a `.env` file when `--env-file` is not supplied. Read from the real process environment, not from a `.env` file. | Configuration loading |

The `_FILE` suffix is available for any `Settings` field as described above; `AIHUB_HOST`, `AIHUB_PORT`, and `AIHUB_ENV_FILE` are startup selectors and do not have `_FILE` handling.

## Database

### SQLite (default)

SQLite stores metadata in `<AIHUB_DATA_DIR>/aihub.db`, or in the explicit `AIHUB_SQLITE_PATH`. The server creates the parent directory and uses WAL mode. SQLite is suitable for a single-machine deployment with persistent writable storage.

### PostgreSQL

Set `AIHUB_DB_BACKEND=postgres`. Provide either a DSN or connection parts; the DSN wins when both are configured:

```dotenv
AIHUB_DB_BACKEND=postgres
AIHUB_DATABASE_URL=postgresql://aihub:replace-me@db:5432/aihub?sslmode=require
```

Or configure parts:

```dotenv
AIHUB_DB_BACKEND=postgres
AIHUB_PG_HOST=db
AIHUB_PG_PORT=5432
AIHUB_PG_NAME=aihub
AIHUB_PG_USER=aihub
AIHUB_PG_PASSWORD=replace-me
AIHUB_PG_SSLMODE=require
AIHUB_PG_POOL_SIZE=10
```

The generated DSN uses the database name, user, password, host, port, and SSL mode above. If using an external PostgreSQL server, choose an SSL mode appropriate to that service; `require` or `verify-full` is preferable for remote databases. PostgreSQL support dependencies are included in `requirements-all.txt`; install `psycopg[binary]` and `psycopg_pool` for a standalone install.

The server initializes the selected engine's schema and applies small in-place updates to older databases. To move existing data to another engine, use the [migration tool](#migrating-between-backends).

## Cache

### Memory (default)

`AIHUB_CACHE_BACKEND=memory` uses a bounded per-process cache. `AIHUB_CACHE_MAX_ITEMS` sets the maximum entry count. This needs no external service, but caches are not shared across server workers.

### Redis

Set `AIHUB_CACHE_BACKEND=redis` and `AIHUB_REDIS_URL`. Add a prefix with `AIHUB_REDIS_PREFIX` if multiple AI Hub instances share a Redis database. TLS URLs use `rediss://`. `AIHUB_CACHE_TTL` is the default Redis expiration; request handlers may specify shorter lifetimes.

Cache invalidation uses a Redis generation counter per key group. Invalidating a group increments its counter so all workers stop reading entries from the previous generation; old entries expire by TTL. This avoids scanning keys. Redis failures during ordinary get/set/delete/invalidation operations are logged and treated as cache misses/no-ops so they do not fail the request. The Redis cache pings during initialization, so an unavailable Redis service at startup prevents server initialization; if Redis goes down after startup, cache operations degrade to misses/no-ops. Use `--check` to verify the URL and connection.

### No cache

`AIHUB_CACHE_BACKEND=none` disables caching. This can be useful for debugging or small deployments.

## Package archive storage

### Local (default)

`AIHUB_STORAGE_BACKEND=local` stores archives below `<AIHUB_DATA_DIR>/files/<package>/`. Persist and back up this directory along with the database.

### S3-compatible storage

Set `AIHUB_STORAGE_BACKEND=s3` and `AIHUB_S3_BUCKET`. AWS S3 can use the normal boto3 credential chain, including an instance or workload IAM role; leave both explicit access-key settings empty in that case. `AIHUB_S3_ENDPOINT` selects an S3-compatible service such as MinIO, Cloudflare R2, or Ceph. Use `AIHUB_S3_PATH_STYLE=true` for MinIO; set region, prefix, endpoint, and credentials to match the provider.

S3 downloads use a short-lived, signed object URL and redirect the client to the object store. With local storage, the server streams the archive. The storage interface returns streams, temporary copies, and optional URLs to callers; API download responses do not expose server filesystem paths.

## Migrating between backends

`python -m aihub.server.migrate` copies your data from the current configuration to a destination described by a second `.env` file. Stop the server first.

```sh
cp .env target.env            # edit target.env: AIHUB_DB_BACKEND=postgres, AIHUB_DATABASE_URL=..., AIHUB_STORAGE_BACKEND=s3, AIHUB_S3_*
python -m aihub.server.migrate all --to target.env        # or: db | storage
python -m aihub.server --env-file target.env              # start on the new backends
```

- `db` copies every table (accounts, roles, packages, versions, reviews, events, settings), keeps ids, resets PostgreSQL sequences and rebuilds the search index. It refuses a destination that already has users unless you pass `--force`. Works in both directions.
- `storage` copies each package archive, checks its SHA-256 against the database before writing, and skips files already present. Works local to S3 and S3 to local.
- Both are safe to re-run. `--from other.env` migrates from a configuration other than the current one. The cache holds nothing to migrate.

## Ready-to-copy examples

### A. Single-machine defaults

```dotenv
AIHUB_PUBLIC_URL=https://hub.example.com
AIHUB_DATA_DIR=./aihub-data
AIHUB_DB_BACKEND=sqlite
AIHUB_CACHE_BACKEND=memory
AIHUB_STORAGE_BACKEND=local
```

For a local-only test, replace the URL with `http://localhost:8000`. SQLite and local archive storage need persistent writable storage and backups.

### B. PostgreSQL, Redis, and S3 production services

```dotenv
AIHUB_PUBLIC_URL=https://hub.example.com
AIHUB_DATA_DIR=/var/lib/aihub
AIHUB_DB_BACKEND=postgres
AIHUB_DATABASE_URL=postgresql://aihub:replace-me@postgres.example.net:5432/aihub?sslmode=verify-full
AIHUB_PG_POOL_SIZE=10
AIHUB_CACHE_BACKEND=redis
AIHUB_REDIS_URL=rediss://:replace-me@redis.example.net:6379/0
AIHUB_REDIS_PREFIX=aihub:
AIHUB_CACHE_TTL=20
AIHUB_STORAGE_BACKEND=s3
AIHUB_S3_BUCKET=aihub-packages
AIHUB_S3_REGION=us-east-1
AIHUB_S3_PREFIX=packages/
AIHUB_S3_PATH_STYLE=false
# Omit access and secret keys when the service has an IAM/workload role.
# Otherwise provide AIHUB_S3_ACCESS_KEY and AIHUB_S3_SECRET_KEY (or their _FILE forms).
```

### C. Self-hosted MinIO

```dotenv
AIHUB_PUBLIC_URL=https://hub.example.com
AIHUB_DB_BACKEND=sqlite
AIHUB_CACHE_BACKEND=memory
AIHUB_STORAGE_BACKEND=s3
AIHUB_S3_BUCKET=aihub
AIHUB_S3_REGION=us-east-1
AIHUB_S3_ENDPOINT=http://minio:9000
AIHUB_S3_ACCESS_KEY=aihub
AIHUB_S3_SECRET_KEY=replace-with-a-long-secret
AIHUB_S3_PREFIX=packages/
AIHUB_S3_PATH_STYLE=true
```

For an external MinIO instance, use its endpoint as reachable from the AI Hub server. Ensure the bucket already exists; the server does not create it automatically.

## Docker Compose

The Compose file runs the AI Hub service by default with SQLite, memory cache, and local storage. It defines optional profiles:

- `postgres` starts PostgreSQL 16.
- `redis` starts Redis 7 with a required password.
- `minio` starts MinIO and a one-shot `minio-init` service that creates the configured bucket.

Copy `.env.example` to `.env`, set only the secrets for the services you enable, select the backends, then start them:

```sh
cp .env.example .env
# TODO: edit .env and set strong passwords for enabled profiles.
docker compose --profile postgres --profile redis --profile minio up -d --build
```

For bundled services, uncomment and set each needed secret once in `.env`: `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `MINIO_ROOT_USER`, and `MINIO_ROOT_PASSWORD`. Compose derives the app's service connection values from those secrets. The Compose `.env` file is also used for Compose variable substitution, but the `env_file` contents passed to a container are not expanded; do not write values like `AIHUB_DATABASE_URL=postgresql://user:${PASSWORD}@...` expecting expansion inside `.env`. The Compose file itself uses Compose interpolation to construct bundled service URLs.

Docker was not run in this environment, so the Compose file has not been runtime-validated. Before starting services, run this with the Docker Compose version used at deployment and inspect the resolved configuration:

```sh
docker compose config
```

An external-services deployment does not need profiles. Put the external database, Redis, and S3-compatible endpoints in `.env`, set the backends to use them, then run just the app service:

```sh
docker compose up -d --build
```

The Compose file's bundled-service fallback variables are intended for the matching profiles; for external services, supply explicit `AIHUB_DATABASE_URL` (recommended for external PostgreSQL), `AIHUB_REDIS_URL`, and S3-compatible endpoint/credentials in `.env`. Compose explicitly sets `AIHUB_PG_PASSWORD` from `POSTGRES_PASSWORD`, so when using external PostgreSQL connection parts instead of `AIHUB_DATABASE_URL`, set `POSTGRES_PASSWORD` in the Compose `.env` file as well as `AIHUB_PG_HOST` and other parts. `AIHUB_S3_ENDPOINT` is empty by default (AWS S3); set it to `http://minio:9000` in `.env` when you use the bundled MinIO profile, or to your provider's URL for R2/Ceph. Review `docker compose config` to see the effective configuration before launch. Keep credentials out of shell history and version control.
