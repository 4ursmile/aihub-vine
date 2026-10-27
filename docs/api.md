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
| POST | `/api/v1/auth/login` | No | Log in with `{ "username", "password", "remember"? }` and receive a bearer token; the reply's `username` is the account signed in to. `remember: false` (web "Remember me" off) makes a session that ends after 12 hours. `name@domain` signs in to the account `name` when the domain is in the admin setting `login_domains` (and no account is literally called `name@domain`); both forms share one failed-attempt counter. |
| POST | `/api/v1/auth/logout` | User | Revoke the bearer token used for the request. |
| GET | `/api/v1/auth/me` | User | Return username, role, and permissions. |
| POST | `/api/v1/auth/password` | User | Change password with `{ "old", "new" }`. |
| GET | `/api/v1/auth/tokens` | User | List this user's tokens. |
| POST | `/api/v1/auth/tokens` | User | Create an API token with optional `{ "name" }`. Plaintext token is returned once. |
| DELETE | `/api/v1/auth/tokens/{tid}` | User | Delete this user's token by its listed `id`. |

## Catalogue and package discovery

| Method | Path | Auth required | Purpose |
| --- | --- | --- | --- |
| GET | `/api/v1/meta` | No | Hub name, public URL, CLI and server versions, and overview counts. Also `index_git_url` (credentials stripped), `cli_git_url`, `cli_git_branch`, `cli_git_subdir`, and `from_env` flags showing which CLI and index settings come from environment variables. |
| GET | `/api/v1/healthz` | No | Health check; returns `{ "ok": true }`. |
| GET | `/api/v1/packages` | No | Search/list packages. Query options: `q`, `type`, `tag`, `category`, `sort`, `page`, `per_page`, `mine`. `per_page` is clamped to 1–100. |
| GET | `/api/v1/facets` | No | Package type and tag counts. |
| GET | `/api/v1/highlights` | Per `public_browse` | Home page featured packages: `{style, pinned, items}`. `style` = admin setting `home_highlight` (`slides`, `cards`, `off`); `items` = the admin's `home_pinned` list in order (only packages the caller may see), else the 5 most downloaded (`pinned: false`). |
| GET | `/api/v1/packages/{name}` | No | Package metadata, manifest, maintainers, and version information. The response includes `authors` (list of `{name, email?, url?}` from the latest manifest) and `license` (string, empty if unset). |
| GET | `/api/v1/packages/{name}/versions` | No | List package versions with `version`, `sha256`, `size`, `downloads`, `yanked`, `created`, and `source`, a web link to the version's exact commit and subfolder (empty if the repo URL is not http(s)). |
| GET | `/api/v1/packages/{name}/readme` | No | Return the stored README (from the index). If none is stored, fetch it raw from a non-yanked version's repo (public hosts only; GitHub or GitLab-style). Response has `markdown` and `html`. |
| GET | `/api/v1/resolve?name={name}&spec={constraint}` | No | Resolve the newest non-yanked version matching a version constraint. |
| POST | `/api/v1/resolve/tree` | No (public install setting) | Resolve `{"roots": [{"name", "spec"}], "installed": {name: version}}` and all dependencies in one request. Returns `{"packages": [...]}` in install order, each with `name`, `version`, `sha256`, `size`, `repo`, `manifest`, `requires`, `root`. Constraints from all dependents are combined; `409` names the package and its dependents when none satisfy them all. |
| PATCH | `/api/v1/packages/{name}` | User + package manager | Change package `hidden` status, `tags`, and/or `visibility` (`public` or `private`). |
| GET | `/api/v1/packages/{name}/maintainers` | No | List package maintainers. |
| POST | `/api/v1/packages/{name}/maintainers` | User + package manager | Add an existing user with `{ "username" }`. |
| DELETE | `/api/v1/packages/{name}/maintainers/{username}` | User + package manager | Remove a package maintainer. |
| GET | `/api/v1/me/packages` | User | List packages maintained by the caller. |

## Publishing and version control

The server stores no package files and has no upload or download routes (`/upload`, `/uploads`, `/files/...` and `/dl/...` return 404). Packages are published with `aihub dev publish`, which pushes to git; see [architecture](architecture.md). The server is the only writer of the package index (a folder, default `index`). It turns `aihub.publish` events (public data the CLI pushes to the telemetry inbox) into index entries and serves them from its database. The entry is built from the event's `bundle` (the `aihub.toml` and README); without one (older CLIs) the hub fetches the package repository at the published commit. The index repo (a folder, default `index`) gets a backup copy only when the legacy git index sync is on. The CLI never writes the index.

| Method | Path | Auth required | Purpose |
| --- | --- | --- | --- |
| POST | `/api/v1/packages/{name}/versions/{version}/yank` | User + package manager | Yank a version from resolution. |
| POST | `/api/v1/packages/{name}/versions/{version}/unyank` | User + package manager | Make a yanked version resolvable again. |
| DELETE | `/api/v1/packages/{name}?version=` | User + package manager | Unpublish: remove the package (or only `version`) from the hub database and index. Needs admin access on the package (site admin, owner, maintainer); 403 otherwise. Usage events stay. A package left with no versions is removed. |
| GET | `/api/v1/resolve?name=&spec=` | Per `public_install` | Pick a version. The response carries `repo` (`url`, `branch`, `subdir`, `ref`) and the manifest, not a download URL. |
| POST | `/api/v1/resolve/tree` | Per `public_install` | Resolve several packages and their dependencies, constraints merged. |
| GET | `/api/v1/client-config` | No | Locations for the CLI: `index` (`url`, `branch`, `path`, default folder `index`), `telemetry` (`url`, `inbox`, `branch`: the git inbox repo, the one branch CLIs push to as a fallback, and the archive branch), `refresh_hours` and `hub` (`telemetry`, `index`: this hub takes events and serves the index; `telemetry_interval` and `index_ttl` in seconds, set by the admin). No credentials are stored or sent. Never cached. |
| POST | `/api/v1/telemetry` | Optional | Usage events from a CLI: `{"events": [...]}` (at most 5000 events, 4 MiB). Returns `{"accepted", "received"}`. Events are held in memory and written to the database in one transaction per `ingest_db_interval`; a signed-in sender's name is taken from the token. The body may be gzip (`Content-Encoding: gzip`, the 4 MiB cap applies after decompression; `400` bad gzip, `415` other encodings). Event ids already received are skipped before the buffer (bounded LRU, still counted in `accepted`). The answer also carries `index_etag` (the catalog ETag this sender would get from `GET /index/catalog`; absent while the index is not ready). `429` (per-address limit `telemetry_rate_limit`/min) and `503` (buffer full) carry `Retry-After`; the CLI keeps its spool and backs off. Both are counted (`rate_limited`, `busy`, plus `duplicates`, `gzip`) in `GET /admin/sync` `status.ingest` and on Admin > Sync. `400`/`413` mean the batch is malformed. |
| GET | `/api/v1/index/catalog` | Per `public_browse` | The package index as this viewer may see it (private packages are left out): `{"etag", "count", "packages": [{name,type,latest_version,description,tags,h}]}`. Sends `ETag`; `If-None-Match` answers `304` with no body. Built from the database and cached until a package changes. |
| POST | `/api/v1/index/direct/uploads` | Permission `publish` | Open or resume a chunked upload: `{size, sha256}` -> `{upload_id, chunk_size, chunks, received}`. `413` over `direct_max_mb`, `429` with 8 open uploads. |
| GET | `/api/v1/index/direct/uploads/{id}` | The session's owner | `{chunk_size, chunks, size, received:[n...]}`. Another person's id is `404`. |
| PUT | `/api/v1/index/direct/uploads/{id}/chunks/{n}` | The session's owner | Raw bytes of chunk `n` (exactly `chunk_size`, the last shorter). `X-Chunk-Sha256` is verified when sent. Idempotent. |
| POST | `/api/v1/index/direct/uploads/{id}/complete` | The session's owner | Join, verify size + sha256, then publish like `POST /index/direct` (same answer and errors). `409` when chunks are missing or the digest differs. |
| DELETE | `/api/v1/index/direct/uploads/{id}` | The session's owner | Cancel and delete the chunks. |
| POST | `/api/v1/index/direct` | Permission `publish` (signed in) | `aihub dev publish -i`: body = the package `.tar.gz` (`aihub.toml` at its top, `Content-Type: application/octet-stream`). Stored in `<data>/direct`, indexed with repository `hub://direct`, `publish` event recorded. Reply `{package, version, sha256, size, status: published \| already published \| queued}`. `400` unusable archive, `403` package belongs to others, `409` version exists with other content, `413` over admin number `direct_max_mb`. |
| GET | `/api/v1/index/packages/{name}/transfer/{version}` | Permission `install_indirect`; none for a package published with `-i` (its stored archive is sent, `X-Aihub-Cache: direct`, `X-Aihub-Sha256`). The file answers `Range` requests (`206`, `Accept-Ranges: bytes`), which the CLI and the web page use to download in parallel chunks | `aihub install -i`: tar.gz of the package, fetched from git by the hub and cached by (repo, subdir, commit). Header `X-Aihub-Rev`. `502` + `Retry-After` when git fails. |
| GET | `/api/v1/index/packages/{name}` | Per `public_browse` | One full index entry. `ETag` is the entry hash `h` from the catalog; `304` when unchanged. A private package the viewer may not see answers `404`, the same as a missing one. |

## Reviews

| Method | Path | Auth required | Purpose |
| --- | --- | --- | --- |
| GET | `/api/v1/packages/{name}/reviews` | No | Read package rating summary and reviews. |
| POST | `/api/v1/packages/{name}/reviews` | `review` permission | Create or replace the caller's review with `{ "rating": 1..5, "body": "..." }`. |

## Usage events, statistics, and rankings

CLIs send events to `POST /api/v1/telemetry` first; if the hub is unreachable for several tries in a row they add them as files to the `telemetry-inbox` branch of a git inbox repo instead, and the server reads those on a schedule. Both paths fill the same `events` table, keyed by the event id so a resend or a re-read never duplicates. Ingested files are archived to the `telemetry-archive` branch and then removed from the inbox. Event kinds: `use`, `install`, `update`, `uninstall`, `error`, `publish`.

| Method | Path | Auth required | Purpose |
| --- | --- | --- | --- |
| GET | `/api/v1/packages/{name}/stats?days=30` | No | Aggregate package usage statistics. |
| GET | `/api/v1/packages/{name}/events` | User + package manager | List recent package events. |
| GET | `/api/v1/rankings/{what}?days=30` | No | Rank `packages`, `developers`, or `users` over the window. `developers` credits each name in a package's manifest `authors` (latest non-yanked version) with the package's full usage count; a package with no authors credits its maintainers. `reviews` and `reviewed` rank by rating, all-time, not windowed. |
| GET | `/api/v1/stats/overview` | No | Aggregate package, version, user, event, and download counts. |
| GET | `/api/v1/dashboard` | `view_dashboard` | Aggregates for the dashboard, including `top_developers` (same author-based credit as the `developers` ranking). |
| GET | `/api/v1/dashboard/events` | `view_dashboard` | Paginated activity log (no command detail or working directory). |
| POST | `/api/v1/usage/sync` | `view_usage` (signed in, or the anonymous role) | CLI upload of one device's daily token totals: `{"client_id", "local_user", "rows": [{day, tool, model, requests, input, output, cache_write, cache_read, cost, unpriced}]}` plus `"sessions": [{sid, day, tool, model, start, end, requests, input, output, cache_write, cache_read, cost, unpriced, peak, cold, compact, big, light}]` (max 20000 rows, gzip accepted). A row is the whole UTC day and overwrites the stored one (idempotent); a day or session never shrinks. Clients add up, except that a client reporting a day with 5+ requests and exactly the same numbers as another client of the same person is the same machine (a reinstall) and is merged into it (larger day total wins). Rows wait in memory and ONE transaction writes them every `usage_db_interval` s (default 5, Admin > Sync); they are journaled (fsync) before the 2xx, a read flushes the caller's own pending rows first. 429/503 carry `Retry-After`. Bad rows are dropped, not an error. Not signed in: the rows go to the hub account whose name equals `local_user` after the same normalization as `localize_users` (`Sam`, `sam@corp.com`, `sam` are one person; with the setting off the exact name); no single match = `{"stored": 0, "matched": false}`. |
| GET | `/api/v1/usage/sessions?scope=me\|groups&group=&days=&from=&to=&tool=&model=&user=&limit=100` | Signed in | Sessions (biggest cost first) with `peak` context, requests, cache hit, length, `cold` / `compact` / `big` counts, and **usage hints** (`hints`: cold cache rebuilds, huge context, low cache hit, premium model for short replies, one-shot sessions, one session dominating; each with `amount` USD affected and the short ids). Same scope, group and visibility rules as `/usage`. A session is only its short id: titles, prompts and folders never leave a machine. |
| GET | `/api/v1/usage/groups` | `view_usage` | Groups whose usage the caller may see: the ones they belong to (a site admin: all). |
| GET | `/api/v1/usage?scope=me\|groups&group=ID,ID&days=30&from=&to=&tool=&model=&user=` | Signed in | Tokens and estimated cost. `scope=me` = the caller's own usage; `scope=groups` = everybody in the chosen groups (blank = all the caller's groups; another group is 403), each person counted once even when in several groups. Returns `totals`, `daily`, `by_model`, `by_tool`, `by_user`, `by_group`, `members`, `options`. |

Dashboard filters (query parameters): `days` (7, 14, 30, 90, 180, 365) or a custom `from` / `to` (`YYYY-MM-DD`, UTC, at most 366 days); `package`, `type`, `user`, `source`, `kind` (one kind, or several joined with commas, e.g. `use,install`), `identity` (`anonymous` or `signed-in`), `host` (device), `os` (`macOS`, `Linux`, `Windows`, …), and `q` (substring of package or component). The log also takes `page` and `per_page` (max 100). Results only include packages the caller may see.

### Sync administration

| Method | Path | Auth required | Purpose |
| --- | --- | --- | --- |
| GET | `/api/v1/admin/sync` | `admin` | Sync status and settings. Secrets are reported as `"set"`, never returned. |
| PUT | `/api/v1/admin/sync` | `admin` | Update the telemetry inbox (`telemetry_url`, `telemetry_inbox_branch`, `telemetry_branch` for the archive, `telemetry_archive`: `0` or `1`) and index settings, the CLI git source (`cli_git_url`, `cli_git_branch`, `cli_git_subdir`), the schedule (`sync_interval`: seconds or a 5-field cron expression, default 300) and the CLI refresh interval. `live_kinds` is a comma list of the event kinds shown in the home page's "Happening now" feed (`install`, `use`, `update`, `uninstall`, `error`, `publish`); blank means the default `install,use,update,publish`. It also takes the throughput numbers (blank = default; whole numbers within the range): `ingest_db_interval` (1-300 s, default 10: how long events wait in memory before one database write, counted from the first), `telemetry_git_interval` (0-86400 s, default 3600: archive copy and inbox clean-up leave in ONE atomic push per window), `index_git_interval` (0-86400 s, default 1800: from the first publish until the index repo is pushed; the hub serves the publish at once), `git_job_gap` (0-3600 s, default 300: minimum time between git work of the sync and of the backup), `telemetry_rate_limit` (0-100000 requests/min per address, default 1200, 0 = off), `client_telemetry_interval` (5-3600 s, default 60) and `client_index_ttl` (5-86400 s, default 60), which CLIs pick up with their setup values. `GET` returns the values in use, a `limits` list (key, label, min, max, default) and live queue status. |
| POST | `/api/v1/admin/sync/run?full=false` | `admin` | Run a sync now. `full=true` forgets the cursor and looks back over everything (duplicates are skipped). |

### Backup administration

| Method | Path | Auth required | Purpose |
| --- | --- | --- | --- |
| GET | `/api/v1/admin/backup` | `admin` | Backup settings (`backup_enabled`, `backup_schedule`, `backup_events_chunk`), run status, `from_env.backup_enabled` (true when `AIHUB_BACKUP_ENABLED` overrides the saved switch) and `target` (index repo URL and branch the backup goes to). |
| PUT | `/api/v1/admin/backup` | `admin` | Update those settings. `backup_enabled` is `0` or `1`; `backup_events_chunk` is 1000–1000000. |
| POST | `/api/v1/admin/backup/run` | `admin` | Back up now to the index git repo, whether or not the schedule is enabled. Returns the same view as GET. |
| GET | `/api/v1/admin/update` | `admin` | Self-update view: `git` (false + `why` when the hub does not run from a git checkout), `config` (every `update_*` setting), `source` (url, branch, checkout path, running commit, `dirty`, `skip`), `status` (`running` run id, `last_check`, `last_result`, `last_error`, `available` = commits to pull, `next_in`), `pending` (a switch that is still being watched) and `from_env.update_enabled`. |
| PUT | `/api/v1/admin/update` | `admin` | Update settings (blank = default): `update_enabled` 0/1, `update_schedule` (seconds or cron, default `0 4 * * *`), `update_auto_apply` 0/1, `update_url`, `update_branch`, `update_conflict` (`remote` default, `local`, `abort`, or `manual` = ask me: a real conflict waits for the resolver), `update_tests` 0/1, `update_probation` (10-900 s), `update_restart` (`exec`, `exit`), `update_pip` 0/1, `update_backup` 0/1. |
| POST | `/api/v1/admin/update/check` | `admin` | Fetch and report; changes nothing. Runs in the background (poll `/admin/update/runs`); `?wait=1` waits and returns `{run}`. 409 when a run is active. |
| POST | `/api/v1/admin/update/run` | `admin` | Fetch, merge, verify, switch and restart. `?force=1` also tries a commit that failed before. `?wait=1` as above. |
| POST | `/api/v1/admin/update/rollback` | `admin` | Body `{"target": "<commit, tag or branch>"}`. Verified like an update, switched, restarted and watched. |
| GET | `/api/v1/admin/update/conflict` | `admin` | The conflict waiting for a person (policy `manual`): `files[]` with `path`, `kind` (`text`, `binary`, `delete`), `note` and, for text, `segments` (`{t:"text",text}` or `{t:"conflict",ours,base,theirs}`). 404 when none. `GET /admin/update` carries the same list without content under `conflict` (null when none). |
| POST | `/api/v1/admin/update/resolve` | `admin` | Body `{"files": {path: answer}}` with one answer for EVERY conflicting file: `{"mode":"ours"}` (keep this hub's file), `{"mode":"theirs"}` (incoming), `{"mode":"hunks","choices":["ours"\|"theirs"\|"both"\|{"text":..}, ...]}` (one per conflict block) or `{"mode":"custom","text":"..."}`. Continues the update (verify, switch, restart, watch). A failed run with an answer that no longer fits (the branch or local files moved) changes nothing and does not mark the commit bad. `?wait=1` as above. |
| DELETE | `/api/v1/admin/update/conflict` | `admin` | Forget the waiting conflict; the next check looks again. |
| GET | `/api/v1/admin/update/versions` | `admin` | Commits of the checkout (`current`, `deployed`) to roll back to. |
| GET | `/api/v1/admin/update/runs` | `admin` | History (no log lines). `GET /api/v1/admin/update/runs/{id}` returns one run with its `log` and `stages` (fetch, merge, syntax, tests, boot, backup, switch, watch; each `pending|running|done|skipped|failed|blocked|warn`, with `started`, `finished`, `detail` and, while running, `progress` = [done, total]; a rollback has no fetch/merge; runs from before stages existed have none). |

## Administration

Most administration routes require the `admin` permission. The rows name any other permission they accept.

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
| GET | `/api/v1/admin/git-login` | `admin` | Git logins saved for the server's git: `logins` (`host`, `user`, `by`, `ts`; never the secret), `error` (file unreadable), `suggest` (index / inbox hosts). |
| PUT | `/api/v1/admin/git-login` | `admin` | Save `{ "host", "user"?, "secret" }` (host or repo URL; blank user = `oauth2`). Encrypted in the data folder; used only as a fallback (see configuration.md). |
| DELETE | `/api/v1/admin/git-login/{host}` | `admin` | Remove a saved login. |
| POST | `/api/v1/admin/git-login/test` | `admin` | `{ "host" }`: `git ls-remote` of the backup (index) repo with only the saved login. Returns `{ok, message}`. |
| POST | `/api/v1/admin/logs/listen` | `admin` | `{ "on": true|false, "clear"? }`: start or stop listening to the server log (Admin > Listen). |
| GET | `/api/v1/admin/logs?after={seq}` | `admin` | Log lines after `seq` while listening (`listening`, `seq`, `lines` of `{seq, ts, level, name, msg}`, redacted, last 2000 kept in memory). Listening stops by itself 2 minutes after the last poll. |
| GET | `/api/v1/admin/audit` | `audit` | Read recent admin-action records. |
| GET | `/api/v1/audit` | `audit` | Security audit search. `source=tools` (agent tool calls and installs) or `admin` (account, role and setting changes). Filters: `q` (free text over parameters, tool, package, path, host, person), `actor`, `action` (tool/component or action name), `package`, `kind`, `host` (device), `os`, `days` (0 = all time) or `start`/`end` (unix seconds), `page`, `per_page` (max 200). `%` and `_` in text are literal. |
| GET | `/api/v1/audit/export` | `audit` | Same filters, up to 5,000 rows as CSV. The export itself is audited. |

## Running bots (`aihub run`)

The CLI's run supervisors report here; the hub never connects to a machine. A command waits in the database until the run's next report (every 15 s; 2 s while someone looks at the run). Owner or admin only; another person's run answers 404.

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| POST | `/api/v1/runs/report` | signed in | Body `{client_id, host, os, cli_version, run: {id, name, bot, args, schedule, restart, status, pid, runs, last_exit, last_start, last_end, next_at, started, alive, paused, mode, error}, log: [{seq, ts, line}], done: [command tokens applied]}`. Reply `{commands: [{id, token, action, by, schedule?, restart?, args?}], next}`. |
| GET | `/api/v1/runs` | signed in | `scope=mine` (default) or `all` (admins). Items carry `key` (`uid/client_id/rid`), `owner`, `host`, `os`, `status` (`offline` after 60 s without a report), `online`, `pending`, and the run fields. |
| GET | `/api/v1/runs/{uid}/{client_id}/{rid}/log` | owner/admin | `after=SEQ`: log lines kept in memory (last 1000 per run, lost on a hub restart). |
| POST | `/api/v1/runs/{uid}/{client_id}/{rid}/command` | owner/admin | `{action: start\|stop\|restart\|quit\|set}`; `set` takes `schedule` (`""` removes it), `restart`, `args` (list; owner only, 403 for an admin acting on someone else's run). `stop` stops the bot and keeps the supervisor (so `start` works later); `quit` ends both. 409 when the run is not connected. Admin actions on someone else's run are audited (`run_<action>`). |
| DELETE | `/api/v1/runs/{uid}/{client_id}/{rid}` | owner/admin | Remove the entry (a live run that reports again comes back). |

## Other server routes

These routes are outside the `/api/v1` API prefix, except for the repository documentation endpoints at the end of the table:

| Method | Path | Auth required | Purpose |
| --- | --- | --- | --- |
| GET | `/` | No | Web interface. |
| GET | `/install.sh` | No | CLI bootstrap script, with configured public URL. |
| GET | `/cli/version` | No | CLI version information. |
| GET | `/cli/aihub.pyz` | No | Download generated CLI zipapp. |
| GET | `/static/*` | No | Static web application assets. |
| GET | `/docs` | No | FastAPI Swagger UI (interactive OpenAPI docs). |
| GET | `/redoc` | No | FastAPI ReDoc UI. |
| GET | `/openapi.json` | No | OpenAPI schema. |
| GET | `/api/v1/docs` | No | List Markdown documentation available from the repository docs directory. |
| GET | `/api/v1/docs/{slug}` | No | Render a repository Markdown document. |
| GET | `/api/v1/docs/{slug}/raw` | No | The same document as plain Markdown (`text/markdown`), for agents and scripts. |

The FastAPI routes `/docs`, `/redoc`, and `/openapi.json` are enabled by default in `create_app`. They are separate from the repository documentation endpoints under `/api/v1/docs`.
