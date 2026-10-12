# AI Hub (agentforce)

Package hub for AI coding tools: packages (skills, agents, MCP servers, tools, setup profiles) live in **git**; a FastAPI server
indexes them and serves a web UI + usage dashboard; a stdlib-only CLI (`aihub`) installs and publishes them. Usage events go
through git too: the CLI pushes batches to `refs/telemetry/<client>/<batch>` of an inbox repo, the server ingests, archives and deletes them. Read this file instead of the whole repo; **update it in the same change** whenever you alter structure,
commands, behavior rules or gotchas below.

## Map

| Path | What lives there |
| --- | --- |
| `aihub/core/` | Shared by CLI and server, **stdlib only**: `manifest.py` (parse/normalize/lint `aihub.toml`), `pkgindex.py` (folder package index), `templates.py` (`dev init` starters), `toml_lite.py` (py3.9/3.10 TOML fallback), `version.py`, `naming.py` |
| `aihub/cli/` | `main.py` (argparse, every command: `cmd_*`, `dev_*`), `publish.py` (git publish, `discover`, `enclosing`, `push`), `workspace.py` (monorepo: inspect/fix_git/tree/solve), `installer.py`, `registry.py`, `gitx.py`, `identity.py`, `hooks.py`, `telemetry.py`, `ui.py` |
| `aihub/server/` | `main.py` (app + install.sh/ps1 text), `routers.py` (all `/api/v1`), `repos.py` (all SQL, `Repos`), `db.py` (SQLite/Postgres layer, `LATE_COLUMNS`), `sync.py` (index -> DB), `static/app.js` (Preact+htm SPA, no build step), `static/style.css` |
| `aihub/builtin_packages/` | `aihub-guide`, `aihub-package`: ship with the server. `aihub-guide/skills/aihub-guide/offline.md` is the **offline fallback**: keep it self-contained and in sync with the CLI |
| `docs/` | `cli.md`, `manifest.md`, `publishing.md` (also served to assistants), `api.md`, `architecture.md` |
| `tests/` | `unittest`; `test_dev_workspace.py` = monorepo/self-uninstall, `test_sync.py` = dashboard/sync, `test_backends.py` = real redis/minio/Postgres (skips if absent) |

## Run and test

```sh
python -m aihub.server --host 127.0.0.1 --port 8000 --data /tmp/hubdata     # add --env-file/--ssl-cert/--ssl-key as needed
python -m unittest discover -s tests          # ~2 min, 150 tests; or name modules: python -m unittest tests.test_dev_workspace
```

- Use the venv python (`agent_store/.venv`), not system python. No pytest. `node` is broken on this machine: `app.js` cannot be syntax-checked, so re-read JS edits carefully.
- Test server: data `/tmp/hubdata`, login in `aihub-data/ADMIN_LOGIN.txt` (git-ignored). Stop it before restarting; stale `aihub.cli flush --background` workers pile up and can be `pkill`ed.
- `__pycache__/*.pyc` are **tracked** in git, so every test run dirties the tree. Never `git stash` for comparisons (pop fails on pyc). Do not commit unless asked.

## Rules that are easy to break

- **CLI and `core/` stay stdlib-only** and Python 3.9 compatible. Server code needs 3.10+.
- **DB rows**: on Postgres a `Row` iterates its **keys**. Never `for a, b in cursor.fetchall()`; index by position (`row[0]`) or name. SQLite hides this bug, `test_backends` catches it.
- **All SQL goes through `Repos`** (`repos.py`); visibility is one function, `visible_sql`. New read paths must use it. New columns go in `db.py` `LATE_COLUMNS`.
- **Manifest metadata must never raise** (`authors`, display fields): a bad value is dropped, not an error, or installs and server sync break.
- Manifest fields: add to `manifest.normalize`, `docs/manifest.md`, `templates.HEAD`, and, if shown on the web, `pkgindex.add` -> `sync.py` -> `routers._ser` -> `app.js`.
- A CLI command change touches: handler + `add(...)`/`dev_commands` registration in `main.py`, `docs/cli.md`, `offline.md`, and the aihub-guide/aihub-package SKILL.md where relevant.
- Subparser wiring: `dev` commands run through `_each` (once per package) except `init/list/publish/tree/doctor/tree-solve`. `_each` uses `functools.wraps`; tests reach the raw handler via `.__wrapped__`.

## Feature notes

- **authors**: `[package] authors` (str or table list) -> normalized `[{name,email?,url?}]`. Flows manifest -> index entry -> version manifest -> `/packages/{name}` (`authors`, `license`) -> package page. `dev init` fills it from `identity.current()` (hub user, else OS user).
- **Top developers**: `Repos._top_developers` (shared by `/rankings/developers` and the dashboard `top_developers`, so they agree and honor dashboard filters). Credit = full package usage to each manifest author of the latest non-yanked version; no authors -> maintainers.
- **Monorepo (`aihub dev`)**: one `.git` in the parent, packages in sub folders, each with `aihub.toml`. `discover` finds them (a folder with a manifest is a leaf). `init --mono --packages a,b:mcp --remote URL`, `init --sub --name X`, `list`, `tree`, `doctor [--check] [--remote]` (sets `[git]` url/branch/subdir from the real repo), `tree-solve [--nested merge|keep]` (nested `.git`: merge moves it to `~/.aihub/backups`; keep adds `.git/info/exclude`). `publish`: prepares each package independently (auto `fix_git`, validate, commit), one push per repo, one failure never stops the rest, exit 1 if any failed. `init` refuses a package inside another or above existing ones.
- **OS / device**: CLI event field `os` (= `sys.platform`) -> `sync._os` (display name, e.g. macOS) -> `events.os` (`LATE_COLUMNS`). Filters `os` and `host` apply to `/dashboard`, `/dashboard/events` and `/audit` (+export); dashboard returns `by_os`, `by_os_daily`, `options.oses`; `/audit` returns `hosts`/`oses` for its dropdowns. Events synced before this change have `os` NULL.
- **Index bootstrap**: the server creates the index itself. `Sync.read_index`/`update_index` treat an empty repo, a missing index branch, or a branch without `index/root.json` as "create it" (empty `root.json`, pushed with `HEAD:refs/heads/<branch>`), then reset `sync_cursor` (now only a "an events pass completed" marker; empty = first run, which re-reads the index). A failed index update leaves the telemetry refs in place, so the next pass retries it. `update_index` retries the whole batch (up to 3x, fresh fetch each time) when the push is rejected. CLI publish prints "event sent; the hub will add it shortly", or a warning (package not in the index yet, event queued) when the telemetry inbox push fails.
- **Telemetry via git**: `cli/telemetry.py` stamps every event with a unique `id`, spools it (O_APPEND), and a detached flusher (lock + heartbeat) waits `telemetry_interval` (env `AIHUB_TELEMETRY_INTERVAL` > config.json > `core/defaults.json` `telemetry.interval`, default 15 s, clamp 5..3600) so a burst is ONE batch, then `push()` builds an orphan commit with git plumbing in a private bare repo (`~/.aihub/queue/git`) and pushes it to `refs/telemetry/<client_id>/<unix>-<rand>`. Nothing is pushed when nothing is spooled; a failed push keeps the spool (retried on the next event or `aihub flush`); spool files are removed only after a successful push. The flusher is event driven (no timer; `record()` -> `kick()` starts it only when events are spooled) and exits when nothing is left; after a pass it releases the lock before re-checking the spool, and runs another pass only if the last one sent events (max `MAX_PASSES` = 20). After a failed push `_fail()` writes `queue/backoff.json` and hooks start no flusher for 30 s, doubling to a 15 min cap (`BACKOFF_BASE`/`BACKOFF_MAX`); a successful push or `aihub flush` clears or ignores it. `deliver()` (used by `dev publish`) pushes one event immediately and spools on failure. `telemetry.source()` resolves url/branch: env `AIHUB_TELEMETRY_URL`/`AIHUB_TELEMETRY_BRANCH` > config.json `telemetry_url`/`telemetry_branch` > bundled `core/defaults.json` `telemetry.git_url`/`branch` (shipped in pip and aihub.pyz) > index repo (url only). Server `Sync.pull_events`: fetch `refs/telemetry/*` (up to 500 per pass, batches >5 MB or bad lines dropped), `to_row` (untrusted, redacted, `ext_id` = event id, unique), store, `_archive` raw batch to `archive/<day>/<client>-<batch>.jsonl` on the archive branch (default `telemetry-archive`; admin `telemetry_archive=0` disables), and only then delete the refs and gc. Any failure keeps the refs for the next pass. Server settings: env > admin (`telemetry_url`, `telemetry_branch`, `telemetry_archive`) > defaults.json > index repo. `/client-config` returns locations only (`index`, `telemetry`, `refresh_hours`), never credentials. `aihub dev stats` reads the hub `GET /packages/{name}/stats`. Recovery: `Sync._needs_replay` (no events in the table, or cursor cleared by a forced full sync) -> `replay_archive()` re-inserts everything on the archive branch before the refs are pulled; a missing archive branch is created by the first `_archive`. `remote_config.resolve_missing` is silent (env > config > defaults.json); only `ensure_git_access` ever prompts, and only for git credentials.
- **`aihub self-uninstall [--yes]`**: removes packages, hooks, the `# >>> aihub` PATH block, `~/.aihub`. `aihub uninstall NAME` removes one package. `setup/welcome` has no sign-in menu, only a hint.

## Doc/ownership habits

- After code changes, update the docs above in the same change; for large doc passes a Haiku agent is used (give it exact behavior and the file list; it edits markdown only).
- Keep this file short and current: change a section when its facts change, delete what stops being true.
