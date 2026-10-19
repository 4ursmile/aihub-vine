# AI Hub offline reference (bundled snapshot)

This is a bundled snapshot, condensed from the hub docs (cli, manifest, publishing, publishing-examples, configuration, api, architecture). It was written for aihub-guide 2.3.0 and can be older than the running hub. Use it only when the hub cannot be reached. When the hub docs are available, they win. Tell the user this snapshot may be older than the hub.

## 1. Core model

- **Git** holds package files. The package index is a folder in a git repo (default branch `main`, path `index`) with `root.json`, `catalog/` and `packages/<xx>/<name>.json`; the hub serves it from its database, and git keeps a backup copy. An older single `index.json` is still read.
- **Only the hub server writes the index.** The CLI only reads it. Nobody edits the index by hand.
- **The telemetry inbox** (a git repo, often the index repo) holds usage and lifecycle events as files on one branch, `telemetry-inbox`. Git is the fallback: the CLI sends usage to the hub first (`POST /api/v1/telemetry`, a busy hub is waited for) and adds a file to this branch only when the hub cannot be reached; the server ingests the files, archives them to the `telemetry-archive` branch and removes them from the inbox. The CLI also reads the package index from the hub first (with ETag/304) and from git as the fallback.
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

- `aihub setup` asks the hub for the index location and the telemetry inbox. `aihub setup --manual` works without a hub (see section 3).
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
| `telemetry_url`, `telemetry_inbox_branch` | `AIHUB_TELEMETRY_URL`, `AIHUB_TELEMETRY_INBOX_BRANCH` | Git repo and branch CLIs push usage files to (default: bundled `defaults.json`, then the index repo; branch `telemetry-inbox`) |
| `telemetry_interval` | `AIHUB_TELEMETRY_INTERVAL` | Seconds per telemetry window (default 15 without a hub, 60 from the hub; allowed 5 to 3600) |
| `telemetry_via`, `index_via` | `AIHUB_TELEMETRY_VIA`, `AIHUB_INDEX_VIA` | `git` = skip the hub and use git only (default: hub first, git as the fallback) |
| `auto_update` | `AIHUB_NO_UPDATE=1` | Set `false` to turn off the daily self-update check |

- `AIHUB_HOME` moves the CLI state directory from `~/.aihub` to another path.
- Git logins are kept by `aihub credential`, a git credential helper that `aihub setup` registers for the index and telemetry hosts only (macOS Keychain, Windows DPAPI, Linux Secret Service, else a 0600 file). One entry per host; if already configured, setup changes nothing. Check: `git config --global --get-all credential.https://<host>.helper`.
- Without a hub: `aihub setup --manual`, or `aihub config set index_url <git url>`, or export `AIHUB_INDEX_URL` (and `AIHUB_TELEMETRY_URL`).

Local files under `~/.aihub/`:

```text
config.json       hub URL, index and telemetry settings, client ID
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
aihub search [TERM] --plain|--name-only  # bare lines: "name version" or just the name
aihub info <name>                 # metadata and versions (JSON when output is not a terminal)
aihub install <name>              # resolve from the index, clone at the version ref, install
aihub install <name> --yes        # skip confirmations
aihub install <name> -i           # --indirect: get it through the hub instead of git (needs install_indirect; no git fallback, fails with a message). Git LFS files come down too (needs git-lfs where git runs); the CLI says if it was cached
aihub install a b "c>=1.0"        # several packages at once; one failing never stops the rest
aihub install -r requirements.txt # pip-style file: one name[<spec>] per line, # comments, nested -r
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
aihub doctor                      # index and telemetry inbox reachable? signed in? hooks? python3?
aihub version                     # CLI version and the one the hub offers
aihub upgrade [--check|--force|--rollback]
aihub flush                       # send queued usage events now
# bare `aihub` menu also has Usage and Policy (guard on/off, tick active rules)
aihub usage [--tool claude|codex] [--days N] [--by-day] [--plain] [--json]   # interactive in a tty; local report from the tools' own logs: sessions, models, tokens, cache, est. cost (no hub)
aihub hooks install [--tool claude]   # or: aihub hooks remove
aihub skill install [--tool claude]   # (re)install aihub-guide and aihub-package
aihub skill remove                    # uninstall both
aihub run [BOT] [-- ARGS]            # start an installed bot (type=bot); settings in ~/.aihub/bots/NAME/.env, printed after install
aihub onboard [BOT]                   # optional guided credential setup of a bot that has one ([bot] onboard); install offers it in a terminal
aihub knowledge list|get NOTE|pull|push|edit NOTE|rm NOTE|path   # personal notes + bot soul (soul.md), also on the hub page Knowledge
aihub knowledge share NOTE --user U|--group G [--write] | unshare | shares NOTE | shared | push-shared OWNER/NOTE   # shared notes land in ~/.aihub/knowledge/shared/<owner>/

**Build a bot (type = bot).** 1) `aihub dev init ./my-bot --type bot --name my-bot`. 2) In `aihub.toml`, set `[bot] run = "bot/run.py"` and `title`; declare each setting as `[[bot.env]]` (`name`, `hint`, `required`, `secret`, `default`). 3) Write `bot/run.py`: read settings from the environment, `--once`/`--info` from argv after `--`. 4) `aihub dev validate`, then `aihub dev publish --bump patch`. 5) `aihub install my-bot` prints `settings:` (`~/.aihub/bots/my-bot/.env`); fill it in. 6) `aihub run my-bot` (`-- --info` to check). Memory, harnesses and cron: see `docs/bots.md` on the hub; this snapshot has none.
aihub policy                          # guard that blocks agents from destructive calls (db/s3 delete, overwrite): on|off, enable|disable ID, add ID --pattern RE, test 'CMD', judge on|off (AI second opinion via claude -p; failure = allow), update
aihub vault                           # menu: add/edit/remove git remotes and logins (aihub vault add URL --user NAME; token via prompt or --token-stdin)
aihub logout                          # forget the saved token (usage becomes anonymous, still merged by user name)
aihub self-uninstall [--yes]          # remove aihub: packages, hooks, PATH block, ~/.aihub (not your published packages or git repos)
aihub config                      # print settings as JSON
aihub config set <key> <value>
```

Developer commands (`aihub dev`, run in a package folder or pass its path):

```sh
aihub dev init ./my-pkg --type skill --name my-pkg --description "..."
aihub dev init . --mono --packages "a,b:mcp" --remote <url> --branch main   # one repo, one package per sub folder
aihub dev init . --sub --name new-pkg          # add a package to a workspace folder
aihub dev init -w .                            # interactive wizard (terminal only): one package / monorepo (pick sub folders to adopt) / add a package
aihub dev list . / aihub dev tree .            # packages; tree adds repository and [git] status
aihub dev validate . / aihub dev build PATH    # a folder of packages is handled package by package
aihub dev publish . [--only NAME] [--bump patch]
aihub dev doctor [PATH] [--check] [--remote <url>]   # set [git] url/branch/subdir from the real repository
aihub dev tree-solve [PATH] [--nested merge|keep]    # resolve a package folder with its own .git
aihub dev index / aihub dev stats PATH
```

Constraints: `name>=1.2`, `name==1.2.0`, `name>=1.0,<2`, `~=1.4`. An omitted operator means `==`. Empty or `*` matches any version.

- `install` walks the dependency graph (dependencies first). Constraints from every dependent are combined; a conflict names the package and who requires it.
- With `[python] install = true` the package is pip-installed into its venv from its folder (its `pyproject.toml`), and its console scripts are linked into PATH. `uninstall` removes those shims and the venv.
- The index checkout is refreshed at most every 5 minutes. If the fetch fails, the last checkout is used.
- `install` on a disabled package enables it, unless the index has a newer version (then it updates).
- `disable` refuses while an enabled package depends on it. `--force` overrides; `--all` disables everything.
- `update` skips disabled packages and never moves a package outside the range its enabled dependents allow.
- `lock` records, per package: exact version, git commit, dependencies, `requested`, `enabled`. On another machine, `sync` installs those commits. It stops if the index can no longer provide a locked version.
- Enable, disable and tree are local only and send no telemetry.
- `usage` prices: keys in `~/.aihub/prices.json` match a model name as a substring, ignoring case and the separators space, `_`, `.`, `/`, `:` (all treated as `-`); the longest matching key wins (`deepseek-v4-flash` prices `DeepSeek-V4-Flash-Vision-Exp` unless `deepseek-v4-flash-vision` exists). Bundled `aihub-prices` patterns are regexes tried against the raw and normalized name. `--model` is a case-insensitive regex.
- `self-uninstall` removes every installed package, the usage hooks, the PATH block the installer added and `~/.aihub` (or `$AIHUB_HOME`). It asks first unless `--yes`. For a pip install, also run `pip uninstall aihub-cli`.

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
- Claude Code: `PreToolUse` and `UserPromptSubmit` hooks in `~/.claude/settings.json`. OpenCode: `~/.config/opencode/plugin/aihub-usage.js`. Codex: no tool-call hook, so only install and uninstall events are sent.
- Events are queued in `~/.aihub/queue/events.jsonl` (or `$AIHUB_HOME/queue/`). A background flusher waits one telemetry window (default 15 s) so a burst becomes one batch, then pushes it to the inbox repo as one commit. Nothing is pushed when the queue is empty. `aihub flush` pushes immediately. After a failed push the CLI waits 30 s before the next automatic try, doubling up to 15 min; `aihub flush` skips that wait. The queue is capped at 5 MiB; extra events are dropped while it is over the cap.
- Events wait in the queue (and are retried) while the inbox is unconfigured, unreachable or not writable.
- Parameters are scrubbed on your machine before queueing: values under keys like `password`, `token`, `secret`, `api_key`, `authorization`, `Bearer ...` headers, `password=...` text, passwords in URLs, and common key formats are replaced with `[redacted]`. Pattern-based scrubbing cannot catch every secret.
- Each record carries the working directory, host name and OS user name. Events are recorded under the signed-in account name, or `~<OS user>` when not signed in. That name comes from the client and is not verified.
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
- The server writes the index only from publish events. It stores the entries in its database and serves them there; git push access to the index repo is needed only for the backup copy.

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
4. Sends a public `aihub.publish` event (package, version, commit, repo, branch, and a `bundle` with the `aihub.toml` and README) to the telemetry inbox right away. It is queued if the inbox is unreachable.

It does not edit any index and never needs to. The hub reads the event and builds the index entry from the bundled `aihub.toml` and README, without fetching the repository (an event with no bundle, from an older CLI, is built from the repo at that commit). The package then appears in search and in the "Happening now" feed, and its version shows a Source link to the commit. If indexing fails, the hub retries up to 8 times and Admin > Sync shows the reason. Do not edit the index by hand.

Published versions cannot be reused, so bump the version for every release.

Several packages in one folder (monorepo): `aihub dev init . --mono --packages "a,b:mcp"` makes the folder one git repository and creates one package per sub folder (packages already there are kept, and their `[git]` is refreshed). `aihub dev tree` shows each package's repository and `[git]` status. `aihub dev doctor` sets `[git]` url, branch and subdir from the real repository and syncs `name`, `version` and `description` in `pyproject.toml` (if any) to `aihub.toml`, changing nothing else (`--check` only reports; `validate` errors on a mismatch, `publish` syncs it). A package folder with its own `.git` inside another repository is resolved with `aihub dev tree-solve --nested merge` (its `.git` moves to `~/.aihub/backups`) or `--nested keep` (the outer repository ignores it). Never put a package inside another package. Publishing several packages: each is prepared and validated on its own, a failing one is reported and skipped, the others are committed and pushed (one push per repository); `--only NAME` selects one. Exit status is non-zero if any failed.

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
authors = ["Ada Lovelace <ada@example.com>"]   # "Name", "Name <email>" or {name, email, url}; shown on the package page; credits developer rankings. dev init fills it from your hub username or OS user

[git]                         # optional; set by dev init
url = "https://git.example.com/team/my-tool.git"
branch = "main"
subdir = ""                   # package folder inside the repo, if not the root

[requires]
commands = [{ name = "git", hint = "Install Git first" }, "jq"]
packages = ["shared-helper>=1.2,<2"]   # other AI Hub packages, with constraints
os = ["macos", "linux", "windows"]     # empty = all

[python]                      # optional: creates a per-package venv
install = false               # true: also pip-install the package folder (needs pyproject.toml; its [project.scripts] become commands)
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
- `[python] install = true` needs a `pyproject.toml` in the package folder (lint error otherwise). Use `requires` alone for plain dependencies.

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
- Client config: `GET /client-config` (index and telemetry locations, never credentials).
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
| `AIHUB_TELEMETRY_URL`, `AIHUB_TELEMETRY_INBOX_BRANCH`, `AIHUB_TELEMETRY_BRANCH` | defaults.json, then the index repo / `telemetry-inbox`, `telemetry-archive` | Telemetry inbox repo, its inbox branch (CLIs add files there) and the archive branch (CLIs need write access to `telemetry-inbox` only, not `main`; the server also removes the ingested files and pushes the archive branch) |
| `AIHUB_CLI_GIT_URL`, `_BRANCH`, `_SUBDIR` | empty | Where `pip install` gets the CLI when the server is unreachable |
| `AIHUB_BACKUP_ENABLED` | `0` | Backup schedule on or off (real environment only; else admin `backup_enabled`) |

- Environment values override Admin settings. Order for each value: environment or `.env` > Admin settings > `defaults.json`.
- Storage backends: `AIHUB_STORAGE_BACKEND=local|s3` is legacy and only used by the migration tool (`python -m aihub.server.migrate all|db|storage --to target.env`).
- Backups: the server writes database backups into the index git repo under `backups/` (`index.json` plus `db/<table>.sqlite.gz`; `events` and `audit_log` split into chunks). Passwords and tokens are stored as one-way hashes. Admin settings: `backup_schedule` (cron, default `0 3 * * *`) and `backup_events_chunk` (default 20000).
- Site settings are in the web UI under Admin > Settings. Site identity (name, logo, contact) is in Admin > Settings > Site.

## 14. Architecture in one paragraph

The CLI reads the index from git and clones each package repo at the entry's ref, then follows that repo's `aihub.toml` to install and register. It records the commit; `lock` pins it and `sync` reinstalls exactly it. Usage events are pushed to a git inbox repo in batches. The hub fetches those batches and the index on a schedule and serves the catalogue, accounts and dashboard. Git applies its own access rules; the hub adds none in front of it.

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
| Package missing from search after publish | The hub indexes a publish from the event's bundle, normally at once. If indexing fails it retries (up to 8 tries) and Admin > Sync shows the reason. Fix the cause, or bump the version and publish again |
| `[python] install = true needs a pyproject.toml in the package folder` | Add a `pyproject.toml` with the console scripts, or set `install = false` |
| Usage events missing from the dashboard | Events are queued locally. After a failed push the CLI retries automatically after 30 s, growing to 15 min; run `aihub flush` to retry now, then wait for the hub sync |
| Prompt in CI hangs or install skipped | stdin is closed and counts as "no"; use `--yes` |
| `codex` MCP name already defined | `~/.codex/config.toml` already has `[mcp_servers.<name>]`; the CLI will not overwrite it |
| Usage events missing for Codex | Codex has no tool-call hook; only install and uninstall events are sent |
| Events not appearing in the dashboard | Check `aihub doctor` and `aihub flush`; events wait in the queue while the telemetry inbox is unreachable or not writable. The hub ingests on its sync schedule |
