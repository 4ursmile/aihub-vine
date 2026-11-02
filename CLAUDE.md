# AI Hub (agentforce)

Package hub for AI coding tools: packages live in git (or as hub archives); a FastAPI server indexes them and serves a web UI +
usage dashboard; a stdlib-only CLI (`aihub`) installs and publishes them. Keep this file short: update the map when structure changes.

## Map

| Path | What lives there |
| --- | --- |
| `aihub/core/` | Shared by CLI + server, **stdlib only**: `manifest.py` (parse/lint `aihub.toml`), `pkgindex.py`, `templates.py`, `toml_lite.py`, `pysync.py`, `version.py`, `naming.py`, `textsafe.py`, `schedule.py`, `archive.py`, `defaults.json` |
| `aihub/cli/` | `main.py` (argparse, all `cmd_*` / `dev_*`), `publish.py`, `workspace.py` (monorepo), `installer.py`, `registry.py`, `policy.py`, `admin.py`, `vault.py`, `credhelper.py`, `runs.py` (`aihub run`), `spaces.py`, `pubsub.py`, `workcmd.py`, `gitx.py`, `identity.py`, `hooks.py`, `telemetry.py`, `usage.py`, `usagesync.py`, `updater.py`, `ui.py` |
| `aihub/server/` | `main.py` (app), `routers.py` (`/api/v1`), `repos.py` (all SQL), `db.py` (SQLite/Postgres, `LATE_COLUMNS`), `sync.py` (git <-> DB, `LIMITS`), `ingest.py`, `usagebuf.py`, `direct.py`, `transfer.py`, `routers_spaces.py` + `spaces_repo.py` + `spaceconv.py`, `routers_pubsub.py` + `pubsub_repo.py` + `pubsub_feed.py` (topics, long-poll pub/sub; feed = per-process ring + pump thread; body regex is client side only), `gitgate.py`, `gitauth.py`, `gitlogin.py`, `updater.py` + `update_guard.py`, `basepath.py`, `loglisten.py`, `runlive.py`, `static/app.js` (Preact+htm SPA, no build, vendored libs), `static/style.css` |
| `aihub/builtin_packages/` | `aihub-guide` (`offline.md` = offline fallback, keep in sync with CLI), `aihub-package`, `aihub-prices`, `aihub-policy` (rules JSON), `aihub-knowledge`, `aihub-bot`, `aihub-teams-bot`, `aihub-ui` (local UI service: `ui/server.py`, `ui/aihubui/`, `ui/static/`, `ui/nodes/`, `ui/lib/`), `aihub-nodes-*` |
| `docs/` | `cli.md`, `manifest.md`, `publishing.md`, `api.md`, `architecture.md`, `extending.md`, `ui-nodes.md`, `bots.md`, `configuration.md` |
| `tests/` | `unittest` (no pytest); `test_backends.py` = real redis/minio/Postgres (skips if absent) |

## Run and test

```sh
python -m aihub.server --host 127.0.0.1 --port 8000 --data /tmp/hubdata
python -m unittest discover -s tests          # or: python -m unittest tests.test_x
node --check aihub/server/static/app.js       # must pass after JS edits
```

Use the venv python (`agent_store/.venv`) and run from the repo root.

## Rules (easy to break)

- CLI and `core/` stay **stdlib-only, Python 3.9**; server needs 3.10+.
- All SQL goes through `Repos`; visibility via `visible_sql` / `space_visible_sql`; new columns in `db.py` `LATE_COLUMNS`.
- Postgres `Row` iterates its **keys**: index rows by position or name, never `for a, b in rows`.
- Manifest metadata must never raise: drop bad values.
- A CLI command change touches `main.py` registration, `docs/cli.md`, `offline.md`, relevant SKILL.md.
- SPA: no root-absolute URLs (use `U()` / `basepath.url()`); no CDN (CSP). Never toggle `class` on a `.rv` element.
- Keep `hooks` import chain free of `gitx`/`tls`/`ssl`; keep `runs.py` and post-command imports at module top (zipapp swap).
- Bump the package `aihub.toml` version when changing a builtin package (aihub-ui, aihub-bot, aihub-policy).

## Notices

- `__pycache__/*.pyc` are tracked: tests dirty the tree. Never `git stash` for comparisons. Do not commit unless asked.
- Tests never push to the real hub (`tests/__init__.py` guard). Never run `term/start` with `publish` in tests: it really uploads.
- Live aihub-ui runs on :8765; don't `pkill server.py`.
- Known failures at HEAD: `test_windows_children_never_open_a_console_window` (order dependent), `test_backends...test_bundled_packages_are_valid_and_seeded`, `test_robust.ApiDocsTest...` (swagger vendor files missing), `test_hub_first.StagedPublish...` (flaky timing).
