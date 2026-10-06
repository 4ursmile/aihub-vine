"""Built-in skill bundled with the CLI: teaches Claude Code / Codex how to prepare a project for AI Hub.
Installed by `aihub skill install`. Kept as Python data so it works inside the zipapp."""

NAME = "aihub-package"

SKILL_MD = '''---
name: aihub-package
description: Use when the user wants to create, convert, restructure, validate, or publish a project as an AI Hub package (aihub.toml, skills, agents, MCP servers, tools, setup profiles), new or existing.
---

# AI Hub packaging

Make a project installable with `aihub install <name>`. Work in the project root. Never publish without the user's OK.

## 1. Detect the situation

- `aihub.toml` exists -> existing package: run `aihub dev validate`, fix every error and warning, go to step 5.
- No `aihub.toml`, folder has code/skills/agents -> **existing project**: follow step 3 (restructure).
- Empty or new folder -> **new project**: pick a type (step 2) and run `aihub dev init . --type <type> --name <name>`.

## 2. Choose the package type

| type | use when | required |
| --- | --- | --- |
| skill | instructions the assistant loads on demand | `[[skills]]` + `skills/<name>/SKILL.md` |
| agent | a delegated sub-agent prompt | `[[agents]]` + `agents/<name>.md` |
| mcp | an MCP server the assistant can call | `[[mcp_servers]]` + install scripts |
| tool | a command line program | `[bin]` + install scripts |
| setup | changes env/config on the machine | `[[setup.steps]]` |

One package may combine sections (e.g. skill + tool); `type` is the main purpose.

## 3. Restructure an existing project

Do not move user code blindly: show the planned moves, then apply them (use `git mv` in a repo).

```
<project>/
  aihub.toml
  README.md                 # shown on the package page
  skills/<name>/SKILL.md    # + optional reference files next to it
  agents/<name>.md
  server/ or bin/           # MCP server / executables
  scripts/install.sh  install.ps1  uninstall.sh  uninstall.ps1
  .gitignore
```

- Existing `.claude/skills/*`, `.claude/agents/*.md`, `.codex/skills/*` -> copy into `skills/` and `agents/` and list each in `aihub.toml`.
- Existing MCP config (`.mcp.json`, `mcpServers`) -> `[[mcp_servers]]`; use `${PKG}` for paths inside the package, never absolute paths or secrets.
- Remove build output, `.env`, credentials, `node_modules`, virtualenvs from the package (`.gitignore` is honoured by `aihub dev build`).

## 4. aihub.toml standard

```toml
[package]
name = "my-package"        # lowercase letters, digits, - ; 1-64 chars; start/end alphanumeric
version = "0.1.0"          # immutable once published; bump every release
type = "skill"
description = "One sentence: what it does and when to use it"
tags = ["git"]
readme = "README.md"
license = "MIT"

[requires]
commands = [{ name = "git", hint = "brew install git" }]
packages = []              # other hub packages, e.g. "base>=1.0,<2"
os = []                    # empty = all; or "macos" "linux" "windows"

[[skills]]
name = "my-package"
path = "skills/my-package"

[[mcp_servers]]
name = "my-server"
command = "python3"
args = ["${PKG}/server/server.py"]
env = {}

[bin]
my-cmd = "bin/my_cmd.py"

[scripts]                  # required per supported OS for mcp/tool/agent packages without [bin]/[[mcp_servers]]
install_macos = "scripts/install.sh"
install_linux = "scripts/install.sh"
install_windows = "scripts/install.ps1"
uninstall_macos = "scripts/uninstall.sh"
uninstall_linux = "scripts/uninstall.sh"
uninstall_windows = "scripts/uninstall.ps1"

[python]                   # only if Python deps are needed (installed into a per-package venv)
requires = ["requests>=2"]
```

Quality rules:
- SKILL.md: front matter `name` (matches the folder) and a `description` that starts with "Use when ..."; body under ~500 lines, long references in sibling files.
- Agent .md: front matter `name`, `description`, optional `tools`.
- Scripts: idempotent, no `sudo`, no admin rights, POSIX `sh` + PowerShell twin; print what they do. `chmod +x` shell scripts and executables.
- Every `path` in the manifest must exist and stay inside the project. Declare every external command under `[requires]`.
- README: install line (`aihub install <name>`), what it does, a usage example, uninstall line.
- No secrets, tokens, or machine-specific paths anywhere in the package.
- Setup steps (`env`, `json_merge`, `file`, `block`, `command`) are shown to the user and reversible; prefer `file`/`json_merge`/`block` over `command`.

## 5. Validate, build, publish

```
aihub dev validate     # fix all errors; treat warnings as TODOs
aihub dev build        # dist/<name>-<version>.tar.gz
aihub dev publish --bump patch    # ask the user first; needs `aihub login` + publish permission
```

Finish by reporting: package name/version/type, files moved, warnings left, and the install command `aihub install <name>`.
'''

FILES = {"SKILL.md": SKILL_MD}
