# AI Hub offline reference (bundled snapshot)

This is a bundled snapshot, condensed from the hub docs (cli, manifest, publishing, publishing-examples, configuration, api, architecture). It was written for aihub-guide 2.3.0 and can be older than the running hub. Use it only when the hub cannot be reached. When the hub docs are available, they win. Tell the user this snapshot may be older than the hub.

## 1. Core model

- **Git** holds package files and the package index. The index is a folder in a git repo (default branch `main`, path `index`) with `root.json`, `catalog/` and `packages/<xx>/<name>.json`. An older single `index.json` is still read.
- **Only the hub server writes the index.** The CLI only reads it. Nobody edits the index by hand.
- **Langfuse** holds usage and lifecycle events (OpenTelemetry spans). The CLI writes them; the server reads them.
- **The hub server** caches the index and events, and serves accounts, reviews and the web UI. It stores no package files and has no upload routes.
- Package versions are immutable. A version that was published cannot be re-published.

## 2. Install the CLI

- macOS / Linux: `curl -fsSL https://<hub>/install.sh | sh` (Python 3.9+ must already exist). It adds `~/.aihub/bin` to PATH by writing one marked block to the shell profile. Set `AIHUB_NO_MODIFY_PATH=1` to skip that change. Open a new terminal afterwards.
- Windows (PowerShell): `irm https://<hub>/install.ps1 | iex`. It creates `%USERPROFILE%\.aihub\bin\aihub.cmd` and adds the folder to the user PATH. Open a new terminal afterwards.
- Manual: download `https://<hub>/cli/aihub.pyz` to `~/.aihub/bin/aihub.pyz` and create a wrapper `~/.aihub/bin/aihub` that runs `python3 "$HOME/.aihub/bin/aihub.pyz" "$@"`.
- If `aihub` is not on PATH yet, use the full path: `~/.aihub/bin/aihub` (macOS/Linux/Git Bash), `%USERPROFILE%\.aihub\bin\aihub.cmd` (Windows PowerShell/cmd), or `python3 ~/.aihub/bin/aihub.pyz` (Windows: `py -3 ...`).
- Then configure and sign in:

```sh
aihub config set hub https://<hub>
aihub register          # prompts for username and password; signup policy applies
aihub login             # prompts for username if omitted; password is not echoed
```

- `aihub setup` asks the hub for the index location and, if the admin shares them, the Langfuse keys. `aihub setup --manual` works without a hub (see section 3).
- `aihub welcome` runs first-time setup for detected tools. The hub installer and `welcome` also install the built-in skills.

## 3. Config precedence and keys

Settings resolve in this order (first match wins):

1. Environment variables (CLI environment)
2. `~/.aihub/config.json` (saved with `aihub config set <key> <value>`)
3. Built-in `aihub/core/defaults.json`

Keys and environment variables:

| Key | Environment | Meaning |
| --- | --- | --- |
| `hub` | `AIHUB_URL` (default when no saved `hub`) | Hub URL |
| `index_url` | `AIHUB_INDEX_URL` | Git URL of the index repo, an `https://...json` URL, or a local file |
| `index_branch` / `index_path` | - | Branch (default `main`) and folder (default `index`) in that repo |
| `langfuse_*` | `LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | Langfuse host and project keys |
| `telemetry_interval` | `AIHUB_TELEMETRY_INTERVAL` | Seconds per telemetry window (default 60, allowed 10 to 3600) |
| `auto_update` | `AIHUB_NO_UPDATE=1` | Set `false` to turn off the daily self-update check |

- `AIHUB_HOME` moves the CLI state directory from `~/.aihub` to another path.
- Git credentials are never stored by AI Hub. They come from your git credential helper.
- Without a hub: `aihub setup --manual`, or `aihub config set index_url <git url>`, or export `AIHUB_INDEX_URL`, `LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`.

Local files under `~/.aihub/`:

```text
config.json       hub URL, index and Langfuse settings, client ID
credentials.json  hub login token (restrictive permissions)
index-cache.json  cached copy of an http(s) .json index, used when it cannot be fetched
state.json        installed packages (version, rev, dependencies, enabled)
packages/<name>/  installed package files
repos/            cached git checkouts (index and packages)
venvs/<name>/     per-package Python environments for [python] dependencies
bin/              CLI wrapper and package command shims
backups/<name>/   setup-step backups
queue/            usage event spool and flusher state
```

## 4. Commands

```sh
aihub search "code review"        # up to 30 matches
aihub info <name>                 # metadata and versions (JSON when output is not a terminal)
aihub install <name>              # resolve from the index, clone at the version ref, install
aihub install <name> --yes        # skip confirmations
aihub install <name> --tool claude --tool opencode   # only these tools
aihub list                        # installed names, versions, enabled status
aihub update                      # all installed packages
aihub update <name>
aihub uninstall <name>            # remove files; revert integrations and setup changes
aihub disable <name>              # keep files, remove from tools and PATH
aihub enable <name>
aihub tree [name]                 # dependency tree
aihub tree "<name>>=1.2" --remote # resolve against the index, no install
aihub download <name>@1.0.0 -o ./pkgs
aihub lock                        # write aihub.lock (commit it)
aihub lock --check                # fail if the lock is out of date
aihub sync                        # install exactly the locked commits
aihub sync --check | --prune | --yes
aihub doctor                      # index and Langfuse reachable? signed in? hooks? python3?
aihub version                     # CLI version and the one the hub offers
aihub upgrade [--check|--force|--rollback]
aihub flush                       # send queued usage events now
aihub hooks install [--tool claude]   # or: aihub hooks remove
aihub skill install [--tool claude]   # (re)install aihub-guide and aihub-package
aihub skill remove                    # uninstall both
aihub config                      # print settings as JSON
aihub config set <key> <value>
```

Constraints: `name>=1.2`, `name==1.2.0`, `name>=1.0,<2`, `~=1.4`. An omitted operator means `==`. Empty or `*` matches any version.

- `install` walks the dependency graph (dependencies first). Constraints from every dependent are combined; a conflict names the package and who requires it.
- The index checkout is refreshed at most every 5 minutes. If the fetch fails, the last checkout is used.
- `install` on a disabled package enables it, unless the index has a newer version (then it updates).
- `disable` refuses while an enabled package depends on it. `--force` overrides; `--all` disables everything.
- `update` skips disabled packages and never moves a package outside the range its enabled dependents allow.
- `lock` records, per package: exact version, git commit, dependencies, `requested`, `enabled`. On another machine, `sync` installs those commits. It stops if the index can no longer provide a locked version.
- Enable, disable and tree are local only and send no telemetry.

Install prompts: the CLI asks before running a platform install script and before registering components with tools. `--yes` skips these. When stdin is closed (non-interactive), prompts count as "no", so use `--yes` in scripts and CI.

## 5. Tools and install locations

The CLI detects Claude Code, Codex and OpenCode when their config directory exists or their executable is on PATH. Interactive install asks which tools to use; with `--yes`, all detected tools are used unless `--tool` restricts them.

| Tool | Skills | Agents | MCP registration |
| --- | --- | --- | --- |
| Claude Code | `~/.claude/skills/<name>/` | `~/.claude/agents/<name>.md` | `mcpServers` in `~/.claude.json` |
| Codex | `${CODEX_HOME:-~/.codex}/skills/<name>/` | custom prompt `${CODEX_HOME:-~/.codex}/prompts/<name>.md` | `[mcp_servers.<name>]` in `${CODEX_HOME:-~/.codex}/config.toml` |
| OpenCode | `~/.config/opencode/skill/<name>/` | `~/.config/opencode/agent/<name>.md` | `mcp` entry in `~/.config/opencode/opencode.json` |

Codex has no native agent format, so agents become custom prompts. Skill folders are copied whole.

## 6. Usage hooks and telemetry

- Usage hooks record when an installed component is used (skill, agent, or an MCP tool of an installed package). Calls to other tools are not recorded.
- Install, update and uninstall events are recorded too; install and update include the version.
- Claude Code: a `PreToolUse` hook in `~/.claude/settings.json`. OpenCode: `~/.config/opencode/plugin/aihub-usage.js`. Codex: no tool-call hook, so only install and uninstall events are sent.
- Events are queued in `~/.aihub/queue/events.jsonl` (or `$AIHUB_HOME/queue/`). A background flusher sends them to Langfuse once per telemetry window (default 60 s). `aihub flush` sends immediately. The queue is capped at 5 MiB; extra events are dropped while it is over the cap.
- Events wait in the queue while Langfuse is unconfigured or unreachable.
- Parameters are scrubbed on your machine before queueing: values under keys like `password`, `token`, `secret`, `api_key`, `authorization`, `Bearer ...` headers, `password=...` text, passwords in URLs, and common key formats are replaced with `[redacted]`. Pattern-based scrubbing cannot catch every secret.
- Each record carries the working directory, host name and OS user name. Events go to Langfuse under the signed-in account name, or `~<OS user>` when not signed in. That name comes from the client and is not verified.
- Event names: `aihub.use`, `aihub.install`, `aihub.update`, `aihub.uninstall`, `aihub.error`, `aihub.publish`.
- Hook handling is fast, non-blocking and never fatal to the coding tool.
- Hooks: `aihub hooks install` and `aihub hooks remove` (`--tool` may repeat).

## 7. Index layout

```text
index/
  root.json            top-level index info
  catalog/             catalogue pages
  packages/<xx>/<name>.json   one entry per package
```

Entry shape (written by the server, not by hand):

```json
{"name": "hello-af", "type": "skill", "description": "...", "tags": ["sample"],
 "latest_version": "0.1.0",
 "requires": {"os": ["macos", "linux", "windows"], "commands": ["git"], "packages": ["base-lib>=1"]},
 "python": {"requires": [], "requirements_file": ""},
 "repo": {"url": "https://github.com/org/repo", "branch": "main", "subdir": "hello-af"},
 "versions": [{"version": "0.1.0", "ref": "hello-af-0.1.0"}]}
```

- The index entry's `requires` is used for planning. The `aihub.toml` in the package repo is authoritative at install time.
- The entry's README text is the first README found in the package root (`README.md`, `readme.md`, `Readme.md`, `README.markdown`, or `README`), capped at 20000 characters.
- Each version has a Source link on the package page that points to the exact commit.
- The server writes the index only from publish events, committing and pushing to the index branch. It needs git push access to the index repo.

## 8. Publishing a package, step by step

No hub account is needed to publish: it uses your own git access. The git remote must be writable by you.

**Step 1. Create or detect the project.**
- No `aihub.toml` and an existing project: restructure it (step 3). Show planned moves before moving files.
- New project: `aihub dev init ./my-pkg --type <type> --name <name> --description "..."`

**Step 2. Pick the type.**

| type | use when | required |
| --- | --- | --- |
| skill | instructions the assistant loads on demand | `[[skills]]` and `skills/<name>/SKILL.md` |
| agent | a delegated sub-agent prompt | `[[agents]]` and `agents/<name>.md` |
| mcp | an MCP server the assistant calls | `[[mcp_servers]]` and install scripts |
| tool | a command-line program | `[bin]` and install scripts |
| setup | changes environment or config | `[[setup.steps]]` |

One package can combine sections; `type` is its main purpose. Registration happens only for `[[skills]]`, `[[agents]]` and `[[mcp_servers]]`.

**Step 3. Restructure an existing project.**

```text
<project>/
  aihub.toml
  README.md                 # shown on the package page
  skills/<name>/SKILL.md    # plus optional reference files next to it
  agents/<name>.md
  server/ or bin/           # MCP server or executables
  scripts/install.sh  install.ps1  uninstall.sh  uninstall.ps1
  .gitignore
```

- `.claude/skills/*`, `.claude/agents/*.md`, `.codex/skills/*`: copy into `skills/` and `agents/`, and list each in `aihub.toml`.
- MCP config (`.mcp.json`, `mcpServers`): becomes `[[mcp_servers]]`. Use `${PKG}` for paths inside the package, never absolute paths or secrets.
- Remove build output, `.env`, credentials, `node_modules` and virtualenvs from the package. `aihub dev build` honours `.gitignore`.

**Step 4. Write `aihub.toml` and the files.**
- SKILL.md: front matter `name` (matches the folder) and a `description` that starts with "Use when ..."; keep the body under about 500 lines and put long references in sibling files.
- Agent .md: front matter with `name`, `description`, optional `tools`.
- Scripts: idempotent, no `sudo` or admin rights, POSIX `sh` plus a PowerShell twin, print what they do, `chmod +x` shell scripts.
- Every path in the manifest must exist and stay inside the project. Declare every external command under `[requires]`.
- README: install line (`aihub install <name>`), what it does, a usage example, uninstall line.
- No secrets, tokens or machine-specific paths.

**Step 5. Validate, build, publish.**

```sh
aihub dev validate                 # fix every error; treat warnings as TODOs
aihub dev build                    # writes dist/<name>-<version>.tar.gz
aihub dev publish --bump patch     # or --bump minor / --bump major; ask the user first
aihub install <name>               # test the install
aihub uninstall <name>
```

`aihub dev publish`:
1. Reads `[git]` (`url`, `branch`, `subdir`) from `aihub.toml`. `dev init` fills it from `origin` and the current branch.
2. Checks that the repo's remote and branch still match `[git]`, and stops if they do not.
3. Runs `git init` and adds `origin` if needed, tracks files over 50 MB with git-lfs (stops if git-lfs is missing), commits, and pushes (setting upstream the first time).
4. Sends a public `aihub.publish` event (package, version, commit, repo, branch) through Langfuse. It is queued if Langfuse is unreachable.

It does not edit any index and never needs to. Afterwards the hub reads the event on its next sync (about 60 s, so 1 to 2 minutes), fetches the repo at that commit, checks `aihub.toml`, writes the index entry with its README, and pushes an `index: ...` commit. The package then appears in search, and its version shows a Source link to the commit. Do not edit the index by hand.

Published versions cannot be reused, so bump the version for every release.

Final report to give the user: package name, version, type, files moved, warnings left, and `aihub install <name>`.

## 9. Manifest (`aihub.toml`)

```toml
[package]
name = "my-tool"              # lowercase; runs of - _ . become -; 1 to 64 chars; starts and ends with letter or digit
version = "0.1.0"             # immutable once published; bump with dev publish --bump
type = "tool"                 # skill | agent | mcp | tool | setup (default tool)
description = "A short description"
tags = []                     # lowercase search words
readme = "README.md"          # lint warns if missing
license = "MIT"

[git]                         # optional; set by dev init
url = "https://git.example.com/team/my-tool.git"
branch = "main"
subdir = ""                   # package folder inside the repo, if not the root

[requires]
commands = [{ name = "git", hint = "Install Git first" }, "jq"]
packages = ["shared-helper>=1.2,<2"]   # other AI Hub packages, with constraints
os = ["macos", "linux", "windows"]     # empty = all

[python]                      # optional: creates a per-package venv
requires = ["requests>=2"]
requirements_file = "requirements.txt"

[[skills]]
name = "my-skill"
path = "skills/my-skill"

[[agents]]
name = "my-agent"
path = "agents/my-agent.md"

[[mcp_servers]]
name = "my-server"
command = "python3"
args = ["${PKG}/server/server.py"]   # ${PKG} = installed package directory
env = {}

[bin]
my-tool = "bin/my-tool.py"    # creates a shim (POSIX shell, Windows .cmd) in the CLI bin folder; target must be inside the package

[scripts]
install_macos = "scripts/install.sh"
install_linux = "scripts/install.sh"
install_windows = "scripts/install.ps1"
uninstall_macos = "scripts/uninstall.sh"
uninstall_linux = "scripts/uninstall.sh"
uninstall_windows = "scripts/uninstall.ps1"

[[setup.steps]]
kind = "env"
name = "MY_TOOL_HOME"
value = "${HOME}/.my-tool"
```

Rules:
- Only these `[scripts]` keys are accepted: `install_` and `uninstall_` plus `macos`, `linux` or `windows`.
- A script value is a path inside the package or an inline command. Paths ending in `.sh` run with `sh`, `.ps1` with PowerShell, `.cmd`/`.bat` with `cmd`, `.py` with Python. Paths must not contain spaces.
- The installer sets the working directory to the package folder and `AIHUB_PACKAGE_DIR`.
- Uninstall runs the uninstall script without a prompt; a nonzero exit does not stop removal.
- `${HOME}`, `${PKG}` and `${HUB}` are substituted in setup and MCP strings; paths also expand `~`.
- Setup step kinds (only these five):

| kind | fields | behaviour |
| --- | --- | --- |
| `env` | `name`, `value` | managed `export NAME="value"` block in `.zshrc` and `.bashrc` (if present) |
| `json_merge` | `path`, `data` | deep-merge into JSON; lists get only missing items; backs up prior content |
| `file` | `path`, `content` or `source`, optional `mode` (octal string, e.g. `"600"`) | writes content or copies a package file; backs up existing content |
| `block` | `path`, optional `id`, `content` | managed text block (default id is the step index) |
| `command` | `run`, optional `undo` | runs a shell command; `undo` is recorded and run on uninstall |

- Every step asks for confirmation unless `--yes`. Recorded changes are reverted on uninstall; if a step fails, earlier steps roll back. Prefer `file`, `json_merge` and `block` over `command`.
- Do not put credentials in setup steps.

Versions: an optional `v`, dotted numbers, and an optional `a`, `b`, `rc` or `dev` suffix (`1.2`, `v1.2.0`, `1.2.0rc1`, `2.0dev`). Constraints are comma-separated: `==`, `!=`, `>=`, `<=`, `>`, `<`, `~=`.

## 10. Package types and layouts

Scaffolds from `aihub dev init` (each passes `aihub dev validate`):

- **skill**: `skills/<name>/SKILL.md`, `README.md`, `aihub.toml` (with `[[skills]]`), `.gitignore`. No scripts.
- **agent**: `agents/<name>.md`, `README.md`, `aihub.toml` (with `[[agents]]`), `.gitignore`. No scripts in the starter.
- **mcp**: `server/server.py` (stdio JSON-RPC example), `scripts/install.sh|ps1`, `scripts/uninstall.sh|ps1`, `aihub.toml` (with `[[mcp_servers]]` and `[scripts]`), `README.md`, `.gitignore`.
- **tool**: `bin/<name>.py`, the four scripts above, `aihub.toml` (with `[bin]` and `[scripts]`), `README.md`, `.gitignore`.
- **setup**: `aihub.toml` with an `env` and a `file` step, `README.md`, `.gitignore`.

Validator warnings: an agent, mcp or tool with no `[bin]`, `[[agents]]` or `[[mcp_servers]]` and no install script for a claimed OS; a skill or agent type with no matching component entry; an empty description; a missing README.

Minimal script examples (macOS/Linux `scripts/install.sh`):

```sh
#!/bin/sh
set -eu
echo "my-tool: checking myformatter"
if ! command -v myformatter >/dev/null 2>&1; then
  echo "myformatter is required. Install it with your system package manager, then retry." >&2
  exit 1
fi
```

Windows `scripts/install.ps1`:

```powershell
$ErrorActionPreference = 'Stop'
Write-Host "my-tool: checking myformatter"
if (-not (Get-Command myformatter -ErrorAction SilentlyContinue)) {
  Write-Error 'myformatter is required. Install it with your package manager, then retry.'
  exit 1
}
```

Uninstall scripts only print a message. AI Hub removes package files and registrations itself; scripts should undo only their own external changes.

## 11. Visibility and sharing

- Repos are public or private. New repos use the server's `default_visibility` (default `public`). Admins may disable private repos site-wide.
- Anonymous browsing is allowed by default; installing needs sign-in by default (server settings decide).
- Private packages are visible only to maintainers, directly shared users, members of shared groups and site admins. Others get "not found".
- Visibility is changed with `PATCH /api/v1/packages/{name}` with `{"visibility":"public"|"private"}`. Shares use `GET /api/v1/packages/{name}/access`, `PUT` (`{"type":"user"|"group","name":"...","access":"view"|"develop"}`) and `DELETE /api/v1/packages/{name}/access/{ptype}/{pname}`.
- Access levels: `view` (see and install), `develop` (plus publish and yank), `admin` (manage sharing and metadata).
- Yanking a version excludes it from resolution. It is a hub operation: `POST /api/v1/packages/{name}/versions/{version}/yank` and `/unyank`. The CLI cannot yank.

## 12. Hub API (quick reference)

All routes use `/api/v1`. Send `Authorization: Bearer <token>` on protected routes. Create a token with `aihub login` or `POST /api/v1/auth/tokens`.

- Public: `GET /meta`, `GET /healthz`, `GET /packages?q=&type=&tag=&sort=&page=&per_page=` (per_page 1 to 100), `GET /facets`, `GET /packages/{name}`, `GET /packages/{name}/versions`, `GET /packages/{name}/readme`, `GET /resolve?name=&spec=`, `POST /resolve/tree`, `GET /packages/{name}/reviews`, `GET /packages/{name}/stats?days=30`, `GET /rankings/{what}?days=30`, `GET /stats/overview`.
- Docs: `GET /api/v1/docs` (list), `GET /api/v1/docs/{slug}/raw` (Markdown). Live endpoint list: `/openapi.json`.
- Auth: `POST /auth/register`, `POST /auth/login`, `POST /auth/logout`, `GET /auth/me`, `POST /auth/password`, `GET|POST /auth/tokens`, `DELETE /auth/tokens/{id}`.
- Client config: `GET /client-config` (index location; Langfuse keys only if an admin shares them and the caller is signed in or sends `?code=<enrollment code>`).
- Maintainers and access (signed in, maintainer or admin): `GET|POST /packages/{name}/maintainers`, `DELETE /packages/{name}/maintainers/{username}`, `PATCH /packages/{name}`, `GET /me/packages`.
- Reviews: `POST /packages/{name}/reviews` with `{"rating":1..5,"body":"..."}` (needs `review` permission).
- Admin (needs `admin`): `GET|PUT /admin/sync`, `POST /admin/sync/run?full=false`, `GET|PUT /admin/backup`, `POST /admin/backup/run`, `GET|PUT /admin/settings/registration` (`open`, `approval`, `closed`), `GET|PUT /admin/settings`, `GET /admin/roles`, `GET /admin/users?q=`, `POST /admin/users/{u}/status|role|reset-password`, `DELETE /admin/users/{u}`, `GET /admin/audit`.
- Audit (needs `audit`): `GET /audit` and `GET /audit/export` (CSV, up to 5000 rows).
- Dashboard (needs `view_dashboard`): `GET /dashboard`, `GET /dashboard/events`.
- Not present: upload, download and `/files/...` routes return 404. The server accepts no events.

## 13. Server configuration (admin)

- Settings use the `AIHUB_` prefix. Precedence: command-line flags (`--data`, `--public-url`) > real process environment > the selected `.env` file > code defaults. Any setting may also be read from a `<NAME>_FILE` path (for Docker or Kubernetes secrets).
- Check the setup without serving: `python -m aihub.server --check` (prints a redacted summary and `ok` or `FAIL` for the database and cache; it initializes the schema).

| Setting | Default | Meaning |
| --- | --- | --- |
| `AIHUB_PUBLIC_URL` | `http://localhost:8000` | External base URL, including any path prefix |
| `AIHUB_DATA_DIR` | `./aihub-data` | Data root (SQLite and local files) |
| `AIHUB_DB_BACKEND` | `sqlite` | `sqlite` or `postgres` |
| `AIHUB_SQLITE_PATH` | `<data dir>/aihub.db` | SQLite file |
| `AIHUB_DATABASE_URL` | empty | PostgreSQL DSN (wins over the parts below) |
| `AIHUB_PG_HOST`, `_PORT`, `_NAME`, `_USER`, `_PASSWORD`, `_SSLMODE`, `_POOL_SIZE` | `localhost`, `5432`, `aihub`, `aihub`, empty, `prefer`, `10` | PostgreSQL parts |
| `AIHUB_CACHE_BACKEND` | `memory` | `memory`, `redis` or `none` |
| `AIHUB_CACHE_TTL` | `20` | Cache lifetime in seconds |
| `AIHUB_CACHE_MAX_ITEMS` | `2048` | Memory cache entries |
| `AIHUB_REDIS_URL`, `AIHUB_REDIS_PREFIX` | empty, `aihub:` | Redis (`rediss://` for TLS) |
| `AIHUB_SEED_BUILTIN` | `false` | Publish the built-in packages at startup if their version is missing |
| `AIHUB_OPEN_REGISTRATION` | `true` | Default signup policy before an admin changes it |
| `AIHUB_LOG_LEVEL` | `INFO` | Server log level |
| `AIHUB_HOST`, `AIHUB_PORT` | `127.0.0.1`, `8000` | Listen address and port (real environment only) |
| `AIHUB_ENV_FILE` | unset | Selects a `.env` file |
| `AIHUB_INDEX_URL` | empty | Index repo URL (environment wins over Admin > Sync) |
| `AIHUB_INDEX_BRANCH`, `AIHUB_INDEX_PATH` | `main`, `index` | Branch and folder of the index |
| `LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | empty | Langfuse host and keys (the server only reads from it) |
| `AIHUB_CLI_GIT_URL`, `_BRANCH`, `_SUBDIR` | empty | Where `pip install` gets the CLI when the server is unreachable |
| `AIHUB_BACKUP_ENABLED` | `0` | Backup schedule on or off (real environment only; else admin `backup_enabled`) |

- Environment values override Admin settings. Order for each value: environment or `.env` > Admin settings > `defaults.json`.
- Storage backends: `AIHUB_STORAGE_BACKEND=local|s3` is legacy and only used by the migration tool (`python -m aihub.server.migrate all|db|storage --to target.env`).
- Backups: the server writes database backups into the index git repo under `backups/` (`index.json` plus `db/<table>.sqlite.gz`; `events` and `audit_log` split into chunks). Passwords and tokens are stored as one-way hashes. Admin settings: `backup_schedule` (cron, default `0 3 * * *`) and `backup_events_chunk` (default 20000).
- Site settings are in the web UI under Admin > Settings. Site identity (name, logo, contact) is in Admin > Settings > Site.

## 14. Architecture in one paragraph

The CLI reads the index from git and clones each package repo at the entry's ref, then follows that repo's `aihub.toml` to install and register. It records the commit; `lock` pins it and `sync` reinstalls exactly it. Usage events go to Langfuse as spans. The hub pulls those spans and the index on a schedule and serves the catalogue, accounts and dashboard. Git and Langfuse apply their own access rules; the hub adds none in front of them.

## 15. Troubleshooting

| Message or symptom | Meaning and fix |
| --- | --- |
| `aihub: command not found` after install | Open a new terminal, or use the full path (section 2) |
| `Same version pushed twice` | Versions are immutable; bump with `aihub dev publish --bump patch` |
| `git is not installed` | Install git; `dev publish` needs it |
| `git-lfs is not installed` (file over 50 MB) | Install git-lfs or shrink the file |
| `cannot find '<ref>' in <repo>` or `cannot fetch package index` | Index or repo unreachable, or ref missing. Check the URL and git credentials |
| `git remote origin is X but aihub.toml says Y` | Fix `[git] url` or the remote, then publish again |
| `you are on branch 'X' but aihub.toml publishes 'Y'` | Switch branch or change `[git] branch` |
| `script not found in package: <path>` | Include the script in the package or fix the path |
| `script path escapes the package` | Use an in-package relative path |
| `[bin] <cmd> points outside the package or to a missing file` | Fix the `[bin]` path |
| `skill '<name>' has no SKILL.md` | The skill folder must contain `SKILL.md` |
| `type="mcp" needs at least one [[mcp_servers]] entry` | Add an MCP server |
| `every [[skills]] entry needs a name` | Add a non-empty `name` (same for agents and MCP servers) |
| `duplicate name in [[skills]]` | Component names must be unique in their table |
| `[[mcp_servers]] '<name>' needs a command` | Set `command` |
| `package.description is empty` (warning) | Add a description |
| `README '<path>' not found` (warning) | Add the README or fix `package.readme` |
| Package missing from search after publish | Wait 1 to 2 minutes for the hub sync, then retry |
| Prompt in CI hangs or install skipped | stdin is closed and counts as "no"; use `--yes` |
| `codex` MCP name already defined | `~/.codex/config.toml` already has `[mcp_servers.<name>]`; the CLI will not overwrite it |
| Usage events missing for Codex | Codex has no tool-call hook; only install and uninstall events are sent |
| Events not appearing in Langfuse | Check `aihub doctor` and `aihub flush`; events wait in the queue while Langfuse is unreachable |
