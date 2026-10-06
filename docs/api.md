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
| PATCH | `/api/v1/packages/{name}` | User + package manager | Change package `hidden` status and/or `tags`. |
| GET | `/api/v1/packages/{name}/maintainers` | No | List package maintainers. |
| POST | `/api/v1/packages/{name}/maintainers` | User + package manager | Add an existing user with `{ "username" }`. |
| DELETE | `/api/v1/packages/{name}/maintainers/{username}` | User + package manager | Remove a package maintainer. |
| GET | `/api/v1/me/packages` | User | List packages maintained by the caller. |

## Publishing and version control

| Method | Path | Auth required | Purpose |
| --- | --- | --- | --- |
| POST | `/api/v1/upload` | `publish` permission; package manager for existing package | Upload an archive and publish a version. |
| POST | `/api/v1/packages/{name}/versions/{version}/yank` | User + package manager | Yank a version from resolution. |
| POST | `/api/v1/packages/{name}/versions/{version}/unyank` | User + package manager | Make a yanked version resolvable again. |

### Upload protocol

Send the archive as the raw HTTP request body. This is not a multipart form upload. The filename header determines the archive extension used by the server:

```sh
curl -X POST \
  -H 'Authorization: Bearer <token>' \
  -H 'X-Aihub-Filename: package-1.0.0.tar.gz' \
  -H 'Content-Type: application/octet-stream' \
  --data-binary @package-1.0.0.tar.gz \
  https://hub.example.com/api/v1/upload
```

`X-Aihub-Filename` is optional and defaults to `pkg.tar.gz`. If the archive does not contain `aihub.toml`, send a JSON manifest in `X-Aihub-Manifest`. Uploads larger than `AIHUB_MAX_UPLOAD_MB` return HTTP 413. The response contains the package `name`, `version`, and archive `sha256`. A version cannot be published twice for the same package.

## Reviews

| Method | Path | Auth required | Purpose |
| --- | --- | --- | --- |
| GET | `/api/v1/packages/{name}/reviews` | No | Read package rating summary and reviews. |
| POST | `/api/v1/packages/{name}/reviews` | `review` permission | Create or replace the caller's review with `{ "rating": 1..5, "body": "..." }`. |

## Usage events, statistics, and rankings

| Method | Path | Auth required | Purpose |
| --- | --- | --- | --- |
| POST | `/api/v1/events` | No (optional bearer token) | Queue usage events. |
| GET | `/api/v1/packages/{name}/stats?days=30` | No | Aggregate package usage statistics. |
| GET | `/api/v1/packages/{name}/events` | User + package manager | List recent package events. |
| GET | `/api/v1/rankings/{what}?days=30` | No | Rank `packages`, `developers`, or `users`. |
| GET | `/api/v1/stats/overview` | No | Aggregate package, version, user, event, and download counts. |

The event request accepts an array or an object containing an `events` array. The server considers at most 500 events. Each event needs a supported `kind` and a nonempty `package`; `ts`, `version`, `client_id`, `username`, `component`, and `duration` are optional.

```json
{
  "events": [
    {
      "kind": "use",
      "package": "git-helper",
      "version": "1.0.0",
      "client_id": "client-id",
      "component": "skill:git-helper",
      "duration": 0.4
    }
  ]
}
```

Accepted kinds are `install`, `update`, `uninstall`, `use`, and `error`. `ts` is a Unix timestamp in seconds; if omitted or falsy, server time is used. Package names are normalized. When a valid active bearer token is supplied, its username overrides `username` in the event. The response gives the number of accepted rows.

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

The FastAPI routes `/docs`, `/redoc`, and `/openapi.json` are enabled by default in `create_app`. They are separate from the repository documentation endpoints under `/api/v1/docs`.
