# CLI guide

The `aihub` command is a Python zipapp distributed by the server. It uses the Python standard library and supports Python 3.9 or later.

## Install and configure

Install from your AI Hub server:

```sh
curl -fsSL https://hub.example.com/install.sh | sh
export PATH="$HOME/.aihub/bin:$PATH"
```

The install script downloads `aihub.pyz`, creates an `aihub` wrapper in `~/.aihub/bin`, sets the hub URL, and attempts to install usage hooks for detected tools. Add the `PATH` line to your shell startup file to keep it across sessions.

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
aihub doctor
```

- `search [term]` prints up to 30 matching packages.
- `info <name>` prints package metadata and versions as JSON.
- `install <name>` resolves and downloads a matching package, verifies its SHA-256, installs dependencies, and can register components with detected tools. Interactive mode asks for confirmation. `--yes` skips prompts; `--tool` can be repeated to select tools explicitly.
- `list` shows installed package names and versions.
- `update [name]` updates all installed packages or the named one.
- `uninstall <name>` removes package files and reverts registered integration and setup changes where recorded.
- `doctor` checks the hub connection, detected hook status, and whether `python3` is available.

During installation, missing required commands are reported with any manifest hint. Package dependencies are recursively installed. Python dependencies in `[python]` are installed into a package-specific virtual environment.

## Supported tools and install locations

The CLI detects Claude Code, Codex, and OpenCode when their configuration directory exists or their executable is on `PATH`. With interactive installation, it asks which detected tools should receive package components. With `--yes`, all detected tools are selected unless `--tool` restricts them.

| Tool | Skills | Agents | MCP server registration |
| --- | --- | --- | --- |
| Claude Code | `~/.claude/skills/<name>/` | `~/.claude/agents/<name>.md` | `mcpServers` in `~/.claude.json` |
| Codex | `${CODEX_HOME:-~/.codex}/skills/<name>/` | Custom prompt in `${CODEX_HOME:-~/.codex}/prompts/<name>.md` | Managed `[mcp_servers.<name>]` section in `${CODEX_HOME:-~/.codex}/config.toml` |
| OpenCode | `~/.config/opencode/skill/<name>/` | `~/.config/opencode/agent/<name>.md` | `mcp` entry in `~/.config/opencode/opencode.json` |

Codex has no native agent format, so agent files are installed as custom prompts. Codex also has no general tool-call usage hook; its adapter reports MCP launch counting only. `aihub hooks install` does not create a Codex tool hook.

## Usage hooks and telemetry

Usage hooks record package component use; they do not collect arbitrary tool input content. Hook handling is intended to be fast, non-blocking, and non-fatal to the coding tool.

- Claude Code uses a `PreToolUse` hook in `~/.claude/settings.json`. It matches tool calls, identifies installed skills, agent tasks, and MCP tools, then invokes the CLI hook handler.
- OpenCode uses `~/.config/opencode/plugin/aihub-usage.js`, listening for `tool.execute.before` and launching the CLI in the background.
- Codex does not expose a general tool-call hook, so there is no equivalent hook integration.

The hook writes events to `~/.aihub/queue/events.jsonl` (or `$AIHUB_HOME/queue/events.jsonl`). A background flusher sends queued events to the configured hub. The queue is capped at 5 MiB; if it exceeds that size, additional events are dropped until it shrinks. Hook failures are suppressed to avoid blocking the coding tool.

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
  config.json          Hub URL and client ID
  credentials.json     Login token and username
  state.json            Installed package state
  packages/<name>/      Extracted package files
  venvs/<name>/         Optional package virtual environments
  bin/                  CLI wrapper and package executable shims
  backups/<name>/        Setup-step backups
  queue/                 Usage event spool and flusher state
```

The `credentials.json` file is written with restrictive permissions. Keep the directory private.

## Package development

`aihub dev` creates and validates manifests, builds a source archive, publishes it, and reads package statistics. Run commands from the project directory or pass its path.

```sh
aihub dev init ./my-package --name my-package --type skill
aihub dev validate ./my-package
aihub dev build ./my-package
aihub dev publish ./my-package
aihub dev publish ./my-package --bump patch
aihub dev stats ./my-package
```

`dev init` creates `aihub.toml` and appends a README heading. `dev build` writes `dist/<name>-<version>.tar.gz`. `dev publish` uploads that archive; the server requires a logged-in user with publish permission, and publishing another version of an existing package also requires package maintainer access or admin-level `manage_all` permission. Version numbers cannot be reused once published. `--bump` supports `major`, `minor`, and `patch`.

See the [manifest reference](manifest.md) for package fields and setup step behavior.
