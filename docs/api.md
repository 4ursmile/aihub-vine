# REST API reference

All API routes use the `/api/v1` prefix. Requests and responses are JSON unless noted otherwise. Protected routes use:

```http
Authorization: Bearer <token>
```

Routes marked **No** are public. **User** means an active account token is required. Permission-gated endpoints also require the named permission; package management endpoints additionally require package maintainer access or `manage_all` permission.

## Authentication and tokens

| Method | Path | Auth required | Purpose |
| --- | --- | --- | --- |
| GET | `/api/v1/auth/providers` | No | List configured login providers. Currently returns `password`. |
| POST | `/api/v1/auth/register` | No | Register with `{ "username", "password" }`. First account becomes admin. |
| POST | `/api/v1/auth/login` | No | Log in with username/password and receive a bearer token. |
| POST | `/api/v1/auth/logout` | User | Revoke the bearer token used for the request. |
| GET | `/api/v1/auth/me` | User | Return username, role, and permissions. |
| POST | `/api/v1/auth/password` | User | Change password with `{ "old", "new" }`. |
| GET | `/api/v1/auth/tokens` | User | List this user's tokens. |
| POST | `/api/v1/auth/tokens` | User | Create an API token with optional `{ "name" }`. Plaintext token is returned once. |
| DELETE | `/api/v1/auth/tokens/{tid}` | User | Delete this user's token by its listed `id`. |

## Catalogue and package discovery

| Method | Path | Auth required | Purpose |
| --- | --- | --- | --- |
| GET | `/api/v1/meta` | No | Hub name, public URL, CLI version, and overview counts. |
| GET | `/api/v1/healthz` | No | Health check; returns `{ "ok": true }`. |
| GET | `/api/v1/packages` | No | Search/list packages. Query options: `q`, `type`, `tag`, `sort`, `page`, `per_page`. `per_page` is clamped to 1–100. |
| GET | `/api/v1/facets` | No | Package type and tag counts. |
| GET | `/api/v1/packages/{name}` | No | Package metadata, manifest, maintainers, and version information. |
| GET | `/api/v1/packages/{name}/versions` | No | List package versions. |
| GET | `/api/v1/packages/{name}/readme` | No | Return latest non-yanked README as Markdown and rendered HTML. |
| GET | `/api/v1/resolve?name={name}&spec={constraint}` | No | Resolve the newest non-yanked version matching a version constraint. |
| POST | `/api/v1/resolve/tree` | No (public install setting) | Resolve `{"roots": [{"name", "spec"}], "installed": {name: version}}` and all dependencies in one request. Returns `{"packages": [...]}` in install order, each with `name`, `version`, `sha256`, `size`, `url`, `manifest`, `requires`, `root`. Constraints from all dependents are combined; `409` names the package and its dependents when none satisfy them all. |
| PATCH | `/api/v1/packages/{name}` | User + package manager | Change package `hidden` status and/or `tags`. |
| GET | `/api/v1/packages/{name}/maintainers` | No | List package maintainers. |
| POST | `/api/v1/packages/{name}/maintainers` | User + package manager | Add an existing user with `{ "username" }`. |
| DELETE | `/api/v1/packages/{name}/maintainers/{username}` | User + package manager | Remove a package maintainer. |
| GET | `/api/v1/me/packages` | User | List packages maintained by the caller. |

## Publishing and version control

The server stores no package files and has no upload or download routes (`/upload`, `/uploads`, `/files/...` and `/dl/...` return 404). Packages are published with `aihub dev publish`, which pushes to git; see [architecture](architecture.md).

| Method | Path | Auth required | Purpose |
| --- | --- | --- | --- |
| POST | `/api/v1/packages/{name}/versions/{version}/yank` | User + package manager | Yank a version from resolution. |
| POST | `/api/v1/packages/{name}/versions/{version}/unyank` | User + package manager | Make a yanked version resolvable again. |
| GET | `/api/v1/resolve?name=&spec=` | Per `public_install` | Pick a version. The response carries `repo` (`url`, `branch`, `subdir`, `ref`) and the manifest, not a download URL. |
| POST | `/api/v1/resolve/tree` | Per `public_install` | Resolve several packages and their dependencies, constraints merged. |
| GET | `/api/v1/client-config` | No (credentials gated) | Index location for the CLI. Langfuse credentials (never git credentials) are included only when an admin enabled sharing and the caller is signed in or sends `?code=<enrollment code>`. Never cached. |

## Reviews

| Method | Path | Auth required | Purpose |
| --- | --- | --- | --- |
| GET | `/api/v1/packages/{name}/reviews` | No | Read package rating summary and reviews. |
| POST | `/api/v1/packages/{name}/reviews` | `review` permission | Create or replace the caller's review with `{ "rating": 1..5, "body": "..." }`. |

## Usage events, statistics, and rankings

The server no longer accepts events (`POST /api/v1/events` is gone). The CLI writes them to Langfuse as OpenTelemetry spans, and the server pulls them on a schedule into the same `events` table, keyed by Langfuse's observation id so re-reads never duplicate. Event kinds: `use`, `install`, `update`, `uninstall`, `error`, `publish`.

| Method | Path | Auth required | Purpose |
| --- | --- | --- | --- |
| GET | `/api/v1/packages/{name}/stats?days=30` | No | Aggregate package usage statistics. |
| GET | `/api/v1/packages/{name}/events` | User + package manager | List recent package events. |
| GET | `/api/v1/rankings/{what}?days=30` | No | Rank `packages`, `developers`, or `users`. |
| GET | `/api/v1/stats/overview` | No | Aggregate package, version, user, event, and download counts. |
| GET | `/api/v1/dashboard` | `view_dashboard` | Aggregates for the dashboard. |
| GET | `/api/v1/dashboard/events` | `view_dashboard` | Paginated activity log (no command detail or working directory). |

Dashboard filters (query parameters): `days` (7, 14, 30, 90, 180, 365) or a custom `from` / `to` (`YYYY-MM-DD`, UTC, at most 366 days); `package`, `type`, `user`, `source`, `kind`, `identity` (`anonymous` or `signed-in`), `host`, and `q` (substring of package or component). The log also takes `page` and `per_page` (max 100). Results only include packages the caller may see.

### Sync administration

| Method | Path | Auth required | Purpose |
| --- | --- | --- | --- |
| GET | `/api/v1/admin/sync` | `admin` | Sync status and settings. Secrets are reported as `"set"`, never returned. |
| PUT | `/api/v1/admin/sync` | `admin` | Update Langfuse and index settings, the schedule (`sync_interval`: seconds or a 5-field cron expression), credential sharing, the enrollment code and the CLI refresh interval. Sending `""` or `"set"` for a secret keeps the stored value. |
| POST | `/api/v1/admin/sync/run?full=false` | `admin` | Run a sync now. `full=true` forgets the cursor and looks back over everything (duplicates are skipped). |

## Administration

All administration routes require the `admin` permission.

| Method | Path | Auth required | Purpose |
| --- | --- | --- | --- |
| GET | `/api/v1/admin/users?q={query}` | `admin` or `reset_password` | List or filter accounts. |
| POST | `/api/v1/admin/users/{username}/status` | `admin` | Set account status to `active`, `pending`, or `disabled`. |
| POST | `/api/v1/admin/users/{username}/role` | `admin` | Assign an existing role. |
| POST | `/api/v1/admin/users/{username}/reset-password` | `reset_password` | Set a new random password and sign that person out everywhere. The password is returned once in the response. Not allowed on yourself, and only an administrator can reset an administrator. |
| DELETE | `/api/v1/admin/users/{username}` | `admin` | Delete an account; an admin cannot delete their own account. |
| GET | `/api/v1/admin/settings/registration` | `admin` | Get current signup mode. |
| PUT | `/api/v1/admin/settings/registration` | `admin` | Set mode to `open`, `approval`, or `closed`. |
| GET | `/api/v1/admin/roles` | `admin` | List roles and permissions. |
| GET | `/api/v1/admin/audit` | `audit` | Read recent admin-action records. |
| GET | `/api/v1/audit` | `audit` | Security audit search. `source=tools` (agent tool calls and installs) or `admin` (account, role and setting changes). Filters: `q` (free text over parameters, tool, package, path, host, person), `actor`, `action` (tool/component or action name), `package`, `kind`, `days` (0 = all time) or `start`/`end` (unix seconds), `page`, `per_page` (max 200). `%` and `_` in text are literal. |
| GET | `/api/v1/audit/export` | `audit` | Same filters, up to 5,000 rows as CSV. The export itself is audited. |

## Other server routes

These routes are outside the `/api/v1` API prefix, except for the repository documentation endpoints at the end of the table:

| Method | Path | Auth required | Purpose |
| --- | --- | --- | --- |
| GET | `/` | No | Web interface. |
| GET | `/install.sh` | No | CLI bootstrap script, with configured public URL. |
| GET | `/cli/version` | No | CLI version information. |
| GET | `/cli/aihub.pyz` | No | Download generated CLI zipapp. |
| GET | `/files/{name}/{filename}` | No | Download a published package archive. |
| GET | `/static/*` | No | Static web application assets. |
| GET | `/docs` | No | FastAPI Swagger UI (interactive OpenAPI docs). |
| GET | `/redoc` | No | FastAPI ReDoc UI. |
| GET | `/openapi.json` | No | OpenAPI schema. |
| GET | `/api/v1/docs` | No | List Markdown documentation available from the repository docs directory. |
| GET | `/api/v1/docs/{slug}` | No | Render a repository Markdown document. |
| GET | `/api/v1/docs/{slug}/raw` | No | The same document as plain Markdown (`text/markdown`), for agents and scripts. |

The FastAPI routes `/docs`, `/redoc`, and `/openapi.json` are enabled by default in `create_app`. They are separate from the repository documentation endpoints under `/api/v1/docs`.
