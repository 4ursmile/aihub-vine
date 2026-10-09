# CLI guide

The `aihub` command is a Python zipapp distributed by the server. It uses the Python standard library and supports Python 3.9 or later.

## Install and configure

Install from your AI Hub server. The web UI's **Get started** page detects your OS and suggests the matching command.

**macOS** (Terminal):

```sh
curl -fsSL https://hub.example.com/install.sh | sh
```

**Linux** (bash):

```sh
curl -fsSL https://hub.example.com/install.sh | sh
```

On macOS and Linux the installer adds `~/.aihub/bin` to your PATH by writing one marked block (`# >>> aihub` … `# <<< aihub`) to your shell profile (`~/.zshrc`, `~/.bash_profile` on macOS or `~/.bashrc` on Linux, `~/.config/fish/config.fish`, otherwise `~/.profile`). In a terminal it asks first (`[Y/n]`, default yes); run non-interactively it adds the block directly. Set `AIHUB_NO_MODIFY_PATH=1` to skip the change, in which case it prints the line to add yourself (`curl -fsSL https://hub.example.com/install.sh | AIHUB_NO_MODIFY_PATH=1 sh`). Re-running never adds a second block. Open a new terminal afterwards.

**Windows** (PowerShell):

```powershell
irm https://hub.example.com/install.ps1 | iex
```

The Windows script creates `%USERPROFILE%\.aihub\bin\aihub.cmd` and adds that folder to your user `PATH`; open a new terminal afterwards. On Windows the script finds a working Python 3.9+ (ignoring the Microsoft Store stub) and installs Python 3.12 via winget or python.org if none is found. On macOS and Linux, Python 3.9+ must already be installed.

The install scripts download `aihub.pyz`, creates an `aihub` wrapper in `~/.aihub/bin`, sets the hub URL, and attempts to install usage hooks for detected tools. 

To install the zipapp manually, use the server URL in place of `https://hub.example.com`:

```sh
mkdir -p "$HOME/.aihub/bin"
curl -fsSL https://hub.example.com/cli/aihub.pyz -o "$HOME/.aihub/bin/aihub.pyz"
printf '#!/bin/sh\nexec python3 "$HOME/.aihub/bin/aihub.pyz" "$@"\n' > "$HOME/.aihub/bin/aihub"
chmod +x "$HOME/.aihub/bin/aihub"
```

Configure a hub URL, then register or log in:

```sh
aihub config set hub https://hub.example.com
aihub register
aihub login
```

`register` prompts for username and password. `login [username]` prompts for a username if omitted and asks for the password without echoing it. Registration is subject to the server's signup policy. The first account on an empty server becomes admin.

`AIHUB_URL` supplies the default hub URL when no saved `hub` value exists. `AIHUB_HOME` changes the CLI state directory from `~/.aihub` to the specified path.

Packages are found in a git-backed package index, not on the hub. The index is a folder in that repository (default path `index`, holding `root.json`, `catalog/` and `packages/`); an older single `index.json` is still read. The CLI only reads the index; the server is the only writer. `aihub setup` asks the hub for the index location and, if your admin shares them, the Langfuse keys. Without a hub, run `aihub setup --manual`, set the index with `aihub config set index_url <git url>`, or export `AIHUB_INDEX_URL`, `LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`. Git credentials stay with your own git credential helper.

## Common commands

```sh
aihub search "code review"
aihub info package-name
aihub install package-name
aihub install package-name --yes
aihub install package-name --tool claude --tool opencode
aihub list
aihub update
aihub update package-name
aihub uninstall package-name
aihub disable package-name
aihub enable package-name
aihub tree
aihub download my-skill@1.0.0 -o ./pkgs
aihub lock
aihub sync
aihub doctor
aihub version
aihub upgrade
```

- `search [term]` prints up to 30 matching packages.
- `info <name>` prints package metadata and versions (as JSON when output is not a terminal).
- `install <name>` resolves the package from the git index, clones its repository at the version's ref, installs dependencies, and can register components with detected tools. Version constraints such as `"name>=1.2"` or `"name==1.2.0"` are accepted. Interactive mode asks for confirmation. `--yes` skips prompts; `--tool` can be repeated to select tools explicitly.
- `download <name>[@version]` copies a package's files out of git into `./<name>-<version>` (or the `-o` folder) without installing it.
- `list` shows installed package names, versions and enabled status.
- `update [name]` updates all installed packages or the named one.
- `uninstall <name>` removes package files and reverts registered integration and setup changes where recorded.
- `disable <name>...` switches a package off without deleting it: its skills, agents and MCP servers are removed from your AI tools, setup steps are reverted, and its commands leave PATH. `enable <name>...` (or `aihub install <name>` on a disabled package) puts it back from the files already on disk, so it works offline. See [Enable, disable, tree, lock and sync](#enable-disable-tree-lock-and-sync).
- `tree [name]` prints the dependency tree. `lock` and `sync` write and apply a lock file.
- `doctor` checks that the package index and Langfuse are reachable, whether you are signed in, which detected tools have usage hooks, and whether `python3` is available. The hub is optional and only reported.
- `welcome` runs first-time setup for detected tools; `setup` pulls index and Langfuse settings (see [Install and configure](#install-and-configure)).
- `version` shows the CLI version and the one the hub offers; `upgrade` installs it (see [Versions and self-update](#versions-and-self-update)).

During installation, missing required commands are reported with any manifest hint. Package dependencies are recursively installed. Python dependencies in `[python]` are installed into a package-specific virtual environment. Installation asks before running the platform install script and before registering components; `--yes` skips these prompts. When stdin is closed (non-interactive), those prompts count as "no".

## Enable, disable, tree, lock and sync

**Dependency resolution** happens on your machine against the package index. `install` and `sync` walk the dependency graph and produce every package to install, dependencies first. The index checkout in `~/.aihub/repos/_index/` is fetched at most every five minutes; if the fetch fails, the last checkout is used. Constraints from every dependent are combined, so a package shared by two others gets one version that satisfies both (for example `left` needs `base>=1,<2` and `right` needs `base>=1.2,<3`: `base` 1.5.0 is chosen). If nothing satisfies all constraints the error names the package and who requires it. Packages already installed at a satisfying version are skipped. Each package is then cloned from its repository at the version's ref.

**Enable and disable.** `aihub disable my-skill` keeps the files but removes everything visible to your tools. `aihub disable` refuses while an enabled package depends on it (`--force` overrides; `--all` disables everything). `aihub enable my-skill` enables disabled dependencies first. `aihub install my-skill` on a disabled package enables it, unless the index has a newer version, in which case it updates. `aihub list` shows the status, `aihub update` skips disabled packages, and usage hooks ignore them. Enable and disable are local only and send no telemetry.

**Tree.** `aihub tree` shows installed packages and what they need; `aihub tree my-skill` shows one package, and which packages need it. `aihub tree "my-skill>=1.2" --remote` resolves against the index and marks each package `(new)`, `(installed)` or `(installed 1.0.0)` without installing. A package that appears more than once is expanded once and marked `(*)` afterwards.

**Lock and sync.** `aihub lock` writes `aihub.lock` (JSON, `-f` for another path). For each installed package it records the exact version, the git commit it was installed from, its dependencies, whether you installed it directly (`requested`), and whether it is enabled. Commit it. On another machine or in CI, `aihub sync` resolves the locked versions from the index, installs exactly those commits, and restores the requested and enabled flags. It stops if the index can no longer provide a locked version. `sync --check` only reports differences (exit 1 if the machine would change), `--prune` removes packages not in the lock, and `--yes` skips the confirmation. `lock --check` fails if the file is out of date. `update` never moves a package outside the range its enabled dependents allow.

## Supported tools and install locations

The CLI detects Claude Code, Codex, and OpenCode when their configuration directory exists or their executable is on `PATH`. With interactive installation, it asks which detected tools should receive package components. With `--yes`, all detected tools are selected unless `--tool` restricts them.

| Tool | Skills | Agents | MCP server registration |
| --- | --- | --- | --- |
| Claude Code | `~/.claude/skills/<name>/` | `~/.claude/agents/<name>.md` | `mcpServers` in `~/.claude.json` |
| Codex | `${CODEX_HOME:-~/.codex}/skills/<name>/` | Custom prompt in `${CODEX_HOME:-~/.codex}/prompts/<name>.md` | Managed `[mcp_servers.<name>]` section in `${CODEX_HOME:-~/.codex}/config.toml` |
| OpenCode | `~/.config/opencode/skill/<name>/` | `~/.config/opencode/agent/<name>.md` | `mcp` entry in `~/.config/opencode/opencode.json` |

Codex has no native agent format, so agent files are installed as custom prompts. Codex has no tool-call hook, so `aihub hooks install` reports "no tool-call hook available" for it and only install and uninstall events are sent; per-use counting is not available for Codex.

## Usage hooks and telemetry

Usage hooks record when an installed package's component is used (a skill, an agent, or one of its MCP tools). Calls to tools that do not belong to an installed package are not recorded. Each record also carries the working directory, the machine's host name, and the OS user name of whoever ran it. Install, update and uninstall events are recorded as well; install and update events include the package version.

Parameters are scrubbed on your machine before anything is queued: values under keys such as `password`, `token`, `secret`, `api_key` or `authorization`, `Bearer ...` headers, `password=...` style text, passwords inside `scheme://user:pass@host` URLs, and common key formats (`sk-...`, `ghp_...`, AWS, Slack, JWT, private keys) are replaced with `[redacted]`. Scrubbing is pattern based, so it cannot catch every secret.

Events are written to Langfuse as OpenTelemetry spans, under your account name when you are signed in, or `~<OS user>` when you are not. That name comes from the client and is not verified. Hook handling is intended to be fast, non-blocking, and non-fatal to the coding tool.

- Claude Code uses a `PreToolUse` hook in `~/.claude/settings.json`. It matches tool calls, identifies installed skills, agent tasks, and MCP tools, then invokes the CLI hook handler.
- OpenCode uses `~/.config/opencode/plugin/aihub-usage.js`, listening for `tool.execute.before` and launching the CLI in the background.
- Codex does not expose a general tool-call hook, so there is no equivalent hook integration.

The hook writes events to `~/.aihub/queue/events.jsonl` (or `$AIHUB_HOME/queue/events.jsonl`). A background flusher sends them to Langfuse once per telemetry window (60 seconds by default, between 10 and 3600; `aihub config set telemetry_interval <seconds>` or `AIHUB_TELEMETRY_INTERVAL` changes it). `aihub flush` sends the queue immediately, and returns without waiting if the background flusher already holds the lock. Events wait in the queue while Langfuse is unconfigured or unreachable. The queue is capped at 5 MiB; if it exceeds that size, additional events are dropped until it shrinks. Hook failures are suppressed to avoid blocking the coding tool.

Install or remove hooks explicitly:

```sh
aihub hooks install
aihub hooks install --tool claude
aihub hooks remove
```

The `--tool` option can be repeated. Package installation can also install hooks for tools where it registered package components.

## Local files

The default CLI home is `~/.aihub`; set `AIHUB_HOME` to override it. The CLI uses these files and directories:

```text
~/.aihub/
  config.json          Hub URL, index and Langfuse settings, client ID
  credentials.json     Hub login token and username
  index-cache.json     Cached copy of an http(s) .json index, used when it cannot be fetched
  state.json           Installed package state (version, rev, dependencies, enabled flag)
  packages/<name>/     Installed package files
  repos/               Cached git checkouts of the index and packages
  venvs/<name>/        Package virtual environments for [python] dependencies
  bin/                 CLI wrapper and package executable shims
  backups/<name>/      Setup-step backups
  queue/               Usage event spool and flusher state
```

Settings resolve in this order: environment variables, then `config.json`, then the built-in `aihub/core/defaults.json` (keys such as `index_url`, `index_branch`, `index_path`, `langfuse_*` and `telemetry_interval`).

The `credentials.json` file is written with restrictive permissions. Keep the directory private.

## Package development

`aihub dev` creates and validates package projects, builds a source archive, publishes it through git, and reads package statistics. Run commands from the project directory or pass its path. Publishing needs no hub account: it uses your own git access.

```sh
aihub dev init ./my-package --type skill --name my-package --description "A short package description"
aihub dev validate ./my-package
aihub dev build ./my-package
aihub dev publish ./my-package
aihub dev publish ./my-package --bump patch
aihub dev stats ./my-package
```

`aihub dev init [path] --type skill|agent|mcp|tool|setup --name NAME --description DESCRIPTION --force` creates a starter project for the selected type and fills in the `[git]` table from the project's repository when it can. If `aihub.toml` already exists, init keeps it; `--force` overwrites the starter files it generates, and unrelated files are left in place. `aihub dev validate [path]` parses and normalizes the manifest, then lints the files it references, reporting errors and warnings. See [Publishing to AI Hub](publishing.md) for project trees, manifest examples, install scripts, visibility, and release steps. `dev build` writes `dist/<name>-<version>.tar.gz`. `dev publish` commits the project, pushes it to the `[git]` remote and branch in `aihub.toml`, and sends an `aihub.publish` event (queued if Langfuse is unreachable). It stops if the repository's remote or branch does not match `[git]`, and it tracks files over 50 MB with git-lfs. It does not edit any index: the hub lists the package on its next index sync, which can take 1-2 minutes. Published versions cannot be reused, so bump the version for each release. `--bump` supports `major`, `minor`, and `patch`.

See [Publishing to AI Hub](publishing.md) for end-to-end package authoring and the [manifest reference](manifest.md) for package fields and setup step behavior.

## Built-in skills

The two built-in skills are ordinary hub packages, not code inside the CLI: `aihub-guide` (tells the assistant to fetch the latest Markdown docs from `GET /api/v1/docs/{slug}/raw` before using AI Hub) and `aihub-package` (turns a new or existing project into a valid package). Neither contains the instructions themselves for packaging: `aihub-package` fetches the *Assistant packaging workflow* section of [publishing.md](publishing.md#assistant-packaging-workflow) from the hub, so editing that document changes the behaviour without a new release. Their sources live in `aihub/builtin_packages/<name>/`. On startup the server publishes any version the registry does not have yet (bump `version` in the package's `aihub.toml` to roll out a change), and they can be resolved and downloaded without signing in, so a brand-new CLI can fetch them. The hub installer and `aihub welcome` install them automatically with the normal installer, so `aihub list`, `aihub update`, `aihub disable` and `aihub uninstall` manage them like any other package.

```sh
aihub skill install                      # (re)install both, registered with every detected tool
aihub skill install --tool claude        # only Claude Code
aihub skill remove                       # same as: aihub uninstall aihub-guide aihub-package
```

On Windows the installer writes both `aihub.cmd` (PowerShell, cmd) and an extensionless `aihub` script (Git Bash, which Claude Code uses to run commands) into `%USERPROFILE%\.aihub\bin`. The skills also tell the assistant how to fall back to the full path if `aihub` is not on the shell's PATH yet.

Then ask the assistant, e.g. "package this project for AI Hub".

## Versions and self-update

The release number lives in `aihub/core/release.py` and is shared by the server, the CLI zipapp and the web UI (shown in the footer and in Admin > Settings).

- `aihub version` shows the installed CLI version and the version the hub offers.
- `aihub upgrade` downloads the CLI from the hub, verifies its SHA-256, and swaps it in. The old copy is kept as `aihub.pyz.prev`. `--check` only reports, `--force` reinstalls, `--rollback` restores the previous copy.
- Auto-update: at most once a day an interactive `aihub` command checks the hub and upgrades itself. Turn it off with `aihub config set auto_update false` or `AIHUB_NO_UPDATE=1`. It never runs for `hook`/`flush` or in non-interactive shells.

To ship a new CLI, bump `VERSION` in `aihub/core/release.py` and restart the server.

## Site identity (admin)

Admin > Settings > Site sets the site name, logo (PNG/JPEG/WebP, max 256 KB), and contact name/email/support link/phone. They appear in the nav and footer. API: `PUT /api/v1/admin/settings`, `POST|DELETE /api/v1/admin/logo`; public read via `GET /api/v1/meta`.


## HTTPS with a private or self-signed certificate

Certificate checks are on by default. If your hub, Langfuse or git host uses an internal CA, tell the CLI once:

| setting | environment | effect |
| --- | --- | --- |
| `aihub config set ca_bundle /path/ca.pem` | `AIHUB_CA_BUNDLE` | trust this CA file (preferred; verification stays on) |
| `aihub config set insecure_tls true` | `AIHUB_INSECURE=1` | skip certificate checks (self-signed / test hubs only) |

They apply to every CLI call: hub, Langfuse, an https index, and git (`GIT_SSL_CAINFO` / `GIT_SSL_NO_VERIFY`). The installers read the same
variables, e.g. `curl -kfsSL https://hub/install.sh | AIHUB_INSECURE=1 sh` (PowerShell: set `$env:AIHUB_INSECURE="1"` first).
