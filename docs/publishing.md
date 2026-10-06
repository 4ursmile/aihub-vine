# Publishing to AI Hub

## The 60-second path

Create a project, replace the generated starter content, validate it, publish it, then install it to test the published archive:

```sh
aihub dev init ./my-tool --type tool --name my-tool --description "A short description"
cd my-tool
# TODO: edit aihub.toml, README.md, and the component or executable files.
aihub dev validate
aihub dev publish
aihub install my-tool
```

Install can ask before running the OS-specific install script and before registering components with detected coding tools. Test those changes, then remove them:

```sh
aihub uninstall my-tool
```

For a new release, edit the files, bump the immutable version, and publish again:

```sh
aihub dev publish --bump patch
```

The first publish requires a logged-in account with the server's `publish` permission. Publishing another version requires develop access to that repository or the site-level `manage_all` permission (held by site admins).

## Let your assistant package it for you

AI Hub ships a built-in skill, `aihub-package`, for Claude Code and Codex. It handles both new and existing projects: it picks the package type, restructures files into the standard layout, writes `aihub.toml`, creates install/uninstall scripts for each OS, then runs `aihub dev validate` and `aihub dev build`. It asks before `aihub dev publish`.

```sh
aihub skill install                   # all detected tools (the hub installer already does this)
aihub skill install --tool codex      # one tool
aihub skill install --scope project   # only for the current project
```

Then, inside the project, ask: "package this project for AI Hub". See [CLI reference](cli.md#built-in-packaging-skill) for all options.

## Choosing a package type

| Type | Use it for | Registered where | Scaffold | Install scripts? |
| --- | --- | --- | --- | --- |
| `skill` | Reusable instructions for an assistant | Claude Code, Codex, and OpenCode skill directories | `--type skill` | No; the scaffold has none. |
| `agent` | A specialized agent prompt | Claude Code agents, Codex custom prompts, OpenCode agents | `--type agent` | No scripts in the starter scaffold; add them if your package needs external setup. |
| `mcp` | A stdio MCP server | Claude Code, Codex, and OpenCode MCP configuration | `--type mcp` | The scaffold includes OS-specific install and uninstall examples. |
| `tool` | An executable command | POSIX shell shim or Windows `.cmd` shim under the AI Hub CLI bin directory | `--type tool` | The scaffold includes OS-specific install and uninstall examples. |
| `setup` | Reversible user configuration changes | User shell config, JSON/text files, and optional commands | `--type setup` | No scripts; use confirmed, reversible `[[setup.steps]]`. |

Registration only happens for manifest component declarations (`[[skills]]`, `[[agents]]`, `[[mcp_servers]]`). A package's `type` does not itself register a component.

## Project layouts

These are the file trees printed by `aihub dev init` for `--name my-<type>`, with generated file paths shown relative to the project root. Each scaffold was validated with `aihub dev validate` without edits.

### Skill

```text
.gitignore
README.md
aihub.toml
skills/my-skill/SKILL.md
```

- `aihub.toml` declares package metadata and `[[skills]]` registration.
- `README.md` is the catalogue overview and package usage guide.
- `skills/my-skill/SKILL.md` is the skill prompt; it starts with YAML front matter for `name` and `description`.
- `.gitignore` excludes build output and common local files.

### Agent

```text
.gitignore
README.md
agents/my-agent.md
aihub.toml
```

- `aihub.toml` declares package metadata and `[[agents]]` registration.
- `README.md` is the catalogue overview and package usage guide.
- `agents/my-agent.md` is the agent prompt; it starts with YAML front matter.
- `.gitignore` excludes build output and common local files.

### MCP server

```text
.gitignore
README.md
aihub.toml
scripts/install.ps1
scripts/install.sh
scripts/uninstall.ps1
scripts/uninstall.sh
server/server.py
```

- `aihub.toml` declares package metadata, a `[[mcp_servers]]` entry, and OS-specific scripts.
- `README.md` is the catalogue overview and package usage guide.
- `server/server.py` is a minimal standard-library JSON-RPC server over stdio.
- `scripts/install.sh` and `scripts/uninstall.sh` are the macOS/Linux examples.
- `scripts/install.ps1` and `scripts/uninstall.ps1` are the Windows examples.
- `.gitignore` excludes build output and common local files.

### Tool

```text
.gitignore
README.md
aihub.toml
bin/my-tool.py
scripts/install.ps1
scripts/install.sh
scripts/uninstall.ps1
scripts/uninstall.sh
```

- `aihub.toml` declares package metadata, the `[bin]` command, and OS-specific scripts.
- `README.md` is the catalogue overview and package usage guide.
- `bin/my-tool.py` is the example Python command.
- `scripts/install.sh`, `scripts/install.ps1`, and their uninstall counterparts are examples for macOS/Linux and Windows.
- `.gitignore` excludes build output and common local files.

### Setup profile

```text
.gitignore
README.md
aihub.toml
```

- `aihub.toml` contains the generated `env` and `file` setup steps.
- `README.md` is the catalogue overview and package usage guide.
- `.gitignore` excludes build output and common local files.

## The manifest

`aihub.toml` describes package metadata, dependencies, components, scripts, binaries, and setup steps. The templates start with this common annotated section:

```toml
[package]
name = "my-tool"
version = "0.1.0"                 # immutable once published; bump it for every release (aihub dev publish --bump patch)
type = "tool"                     # skill | agent | mcp | tool | setup
description = "A short description"
tags = []                         # lowercase words people search for
readme = "README.md"
license = "MIT"

[requires]
commands = []                     # e.g. [{name = "git", hint = "brew install git"}]
packages = []                     # other hub packages this needs, e.g. ["base-tools>=1.0"]
os = []                           # empty = all. Or any of: "macos", "linux", "windows"
```

Field summary:

- `[package]` is required. `name` and `version` are required; `type` must be one of `skill`, `agent`, `mcp`, `tool`, or `setup`. `description`, tags, README path, and license are package metadata.
- `[requires]` lists required commands (optional install hints), other AI Hub packages, and supported operating systems. Empty `os` means unrestricted.
- `[python]` optionally lists pip requirements and a package-local requirements file. When either is set, the installer creates a package-specific virtual environment.
- `[[skills]]`, `[[agents]]`, and `[[mcp_servers]]` declare components to register into supported tools.
- `[bin]` maps command names to files in the package.
- `[scripts]` selects install and uninstall scripts by OS.
- `[[setup.steps]]` describes confirmed setup changes; `command` steps need `undo` to revert a command's effects.

See the [manifest reference](manifest.md) for defaults, versions, complete field behavior, and step details.

## Install and uninstall scripts

Scripts let a package perform external setup that cannot be expressed as file placement, component registration, or setup steps. The `mcp` and `tool` starter projects contain install scripts; the `agent` starter has no scripts, and an agent package may add them if it needs external setup. Skills and setup profiles generally do not need them. The validator warns if an `agent`, `mcp`, or `tool` has no `[bin]`, `[[agents]]`, or `[[mcp_servers]]` entry and lacks install scripts for supported operating systems. A skill entry alone does not suppress that warning. A component or binary declaration can suppress it even if you separately need OS installation work, so review your requirements.

The only accepted `[scripts]` keys are:

```toml
[scripts]
install_macos = "scripts/install.sh"
install_linux = "scripts/install.sh"
install_windows = "scripts/install.ps1"
uninstall_macos = "scripts/uninstall.sh"
uninstall_linux = "scripts/uninstall.sh"
uninstall_windows = "scripts/uninstall.ps1"
```

An entry is either a script path inside the package or an inline shell command. A file path ending in `.sh` runs with `sh`, `.ps1` with PowerShell, `.cmd` or `.bat` with `cmd`, and `.py` with the current Python interpreter. Script file paths must stay inside the package and should contain no spaces; entries ending in a supported extension but containing spaces are treated as inline commands. The installer runs scripts with the package directory as the working directory and sets `AIHUB_PACKAGE_DIR` to that directory. Unless the user passes `--yes`, installation asks for approval before an install script runs. Uninstall runs the matching uninstall script without a confirmation prompt; a nonzero exit does not stop removal of the package.

Good scripts are idempotent, do not use `sudo` or require administrator rights, avoid unexpected network activity, print what they do, and exit nonzero if required work fails. The package files and registered integrations are removed by AI Hub; scripts should only undo their own external changes.

The generated tool and MCP scripts are intentionally no-op examples. To install an external `myformatter` command supplied by a package manager, keep the platform-specific prerequisite checks aligned with those templates:

`scripts/install.sh` for macOS and Linux:

```sh
#!/bin/sh
set -eu
echo "my-tool: checking myformatter"
if ! command -v myformatter >/dev/null 2>&1; then
  echo "myformatter is required. Install it with your system package manager, then retry." >&2
  exit 1
fi
```

`scripts/uninstall.sh`:

```sh
#!/bin/sh
set -eu
echo "my-tool: uninstalling; myformatter is managed separately and is not removed"
```

`scripts/install.ps1` for Windows:

```powershell
$ErrorActionPreference = 'Stop'
Write-Host "my-tool: checking myformatter"
if (-not (Get-Command myformatter -ErrorAction SilentlyContinue)) {
  Write-Error 'myformatter is required. Install it with your package manager, then retry.'
  exit 1
}
```

`scripts/uninstall.ps1`:

```powershell
$ErrorActionPreference = 'Stop'
Write-Host "my-tool: uninstalling; myformatter is managed separately and is not removed"
```

If you instead package a helper binary in the archive, `[bin]` provides an AI Hub-managed launcher and does not require a script merely to make that command available.

## Binaries and registered components

A `[bin]` mapping creates a launcher in the AI Hub CLI's `bin/` directory. POSIX systems get an executable shell shim; Windows gets a `.cmd` shim. The target must be a file inside the package. Python targets run through the package virtual environment if `[python]` dependencies caused one to be created, otherwise through `python3` on POSIX or `python` on Windows.

```toml
[bin]
my-tool = "bin/my-tool.py"
```

An MCP server is registered in detected tool configurations from `[[mcp_servers]]`; `${PKG}` in arguments is replaced with the installed package path:

```toml
[[mcp_servers]]
name = "my-server"
command = "python3"
args = ["${PKG}/server/server.py"]
env = {}
```

Skill and agent paths are copied into each selected tool's configured directory:

```toml
[[skills]]
name = "my-skill"
path = "skills/my-skill"

[[agents]]
name = "my-agent"
path = "agents/my-agent.md"
```

Claude Code, Codex, and OpenCode are supported. Codex installs agent files as custom prompts, not native agents. See [CLI supported tools](cli.md#supported-tools-and-install-locations) for exact locations.

## Requirements

Declare required system commands, optional hints, package dependencies, and supported OSes under `[requires]`:

```toml
[requires]
commands = [{ name = "myformatter", hint = "Install myformatter with your system package manager" }]
packages = ["shared-helper>=1.2,<2"]
os = ["macos", "linux", "windows"]
```

The installer reports missing commands and hints but does not install those system commands. AI Hub package dependencies are resolved and installed recursively. OS labels are `macos`, `linux`, and `windows`; an empty list permits all OSes.

Python dependencies create a per-package environment:

```toml
[python]
requires = ["requests>=2"]
requirements_file = "requirements.txt"
```

## Setup profiles

A setup profile can contain `env`, `json_merge`, `file`, `block`, and `command` steps. Installation shows and confirms each step unless `--yes` is passed. Recorded changes can be reverted during uninstall; a `command` step needs an `undo` command for AI Hub to reverse that command's effects. Variables `${HOME}`, `${PKG}`, and `${HUB}` are substituted in supported strings; paths also expand `~`.

```toml
[[setup.steps]]
kind = "env"
name = "MY_TOOL_HOME"
value = "${HOME}/.my-tool"

[[setup.steps]]
kind = "file"
path = "${HOME}/.my-tool/config.txt"
content = "hub=${HUB}\n"
```

`env` adds a managed export block to `.zshrc` and to `.bashrc` if it exists. `json_merge` deep-merges JSON and backs up the prior file. `file` writes content or copies a package source file and backs up any existing file. `block` adds a managed text block. `command` runs a shell command and can record an `undo` command. AI Hub reverts recorded changes during uninstall and rolls back prior steps if a later step fails. See [manifest setup profile details](manifest.md#setup-profiles).

## Versioning and releases

Published versions are immutable. If the version exists already, the server rejects it. Make a new release by bumping one version component:

```sh
aihub dev publish --bump patch
aihub dev publish --bump minor
aihub dev publish --bump major
```

The publish command updates the manifest version before building and uploading. Review the resulting manifest change and archive before release. You can yank a release to exclude it from version resolution, then unyank it later. Yank and unyank require develop access or site-admin permission and are available through `POST /api/v1/packages/{name}/versions/{version}/yank` and `/unyank`.

Package dependency strings accept comma-separated constraints using `==`, `!=`, `>=`, `<=`, `>`, `<`, and `~=`; an omitted operator means equality. Examples:

```toml
[requires]
packages = ["shared-helper>=1.0,<2", "exact-helper==2.1"]
```

An empty constraint or `*` matches any version. See [manifest version rules](manifest.md#versions-and-constraints).

## Visibility and sharing

New repositories use the server's `default_visibility` (default `public`). The first uploader becomes the repository admin. A repository is public or private; administrators can disable private repositories globally. Public repositories are visible according to the server's public browsing/install settings; the defaults allow anonymous browsing but require sign-in to install. Private packages are visible only to their maintainers, directly shared users, members of shared groups, and site admins. Unauthorized private packages are hidden from lists and reported as not found on package lookups, so the package name is not exposed.

An uploader can request private visibility on first publish with the `X-Aihub-Visibility: private` HTTP header. `aihub dev publish` does not expose a visibility flag. To change visibility later, a repository admin or site admin sends `PATCH /api/v1/packages/{name}` with `{"visibility":"private"}` or `{"visibility":"public"}`; the access API below manages shares, not visibility. On first upload, if private repositories are disabled, a non-admin's `private` header is changed to public. On an existing package, the upload header does not change the repository's visibility.

Use `GET /api/v1/packages/{name}/access` to inspect visibility and current shares. Repository admins or site admins can use `PUT` on that path to share with a user or group at `view` or `develop`; the request body is `{"type":"user","name":"alice","access":"view"}` (use `type: "group"` for a group). Use `DELETE /api/v1/packages/{name}/access/{ptype}/{pname}` to revoke a share. The package page's access controls use the same API.

| Access | What it permits |
| --- | --- |
| `view` | See the package, read metadata and README, and install if server installation policy permits. |
| `develop` | Everything in `view`, plus publish new versions and yank or unyank versions. |
| `admin` | Repository maintainers and site admins can manage sharing, visibility, and package metadata; admins can publish and manage versions. |

## Reviews and ranking

A user with the `review` permission can submit or replace one 1–5-star review per package, with an optional text body. The catalogue's displayed rating is the arithmetic mean and count. The `rating` search sort uses a Bayesian average with a five-review prior toward the mean across all package reviews; unreviewed packages sort last. The `reviews` search sort uses review count, then package name. The `/rankings/reviews` endpoint ranks reviewed packages by Bayesian score, then review count; `/rankings/reviewed` ranks by review count, then average rating. Both review rankings are all-time, not limited by the activity window, and exclude packages without reviews. `GET /api/v1/rankings/{what}` also accepts `packages`, `developers`, and `users`, which rank usage events within the requested day window.

Package search accepts these sort values:

- `relevance` (when a query is supplied and no explicit sort is set)
- `updated`
- `created`
- `name`
- `downloads`
- `rating`
- `reviews`

With no query and no sort, results are ordered by most recently updated. With a query and no explicit sort, full-text search backends use relevance ordering; if full-text search is unavailable, the default order is most recently updated.

## Usage reporting

The CLI records `install`, `update`, and `uninstall` events. For components registered into Claude Code or OpenCode, installed usage hooks can record a `use` event with the package, component name, client ID, and source. Codex does not expose the general hook used to count arbitrary tool calls, and the current CLI does not install an equivalent Codex usage hook. The hook reads the tool name and relevant component identifier to match against installed components; it does not send arbitrary tool input content.

The CLI sends timestamps, package names, and a generated client ID. Install/update events also include version; use events include the matched component and source (`claude` or `opencode`). The server derives a username from an authenticated token, not from the client payload. The current CLI does not record client-side `error` events, populate duration, or send arbitrary tool input. Although an adapter comment mentions Codex MCP launch counting through `aihub exec-mcp`, this CLI has no `exec-mcp` command, so do not expect Codex usage events. The server accepts `error` and `duration` fields from clients, but that does not mean this CLI collects them. Events are queued locally and sent asynchronously; the spool is capped at 5 MiB.

The dashboard aggregates usage events by package, event kind, actor/client, component, source, and package type over selectable periods. Package owners can inspect package stats and recent events; the broader usage dashboard requires the `view_dashboard` permission. Do not treat client IDs as verified people: anonymous event identity is based on the generated client ID.

## Pre-publish checklist

- [ ] Use a valid, unique package name and a new version.
- [ ] Replace all scaffold placeholders in prompts, scripts, and README.
- [ ] Check every component and script path is inside the project and included in the archive.
- [ ] Declare supported operating systems and required commands accurately.
- [ ] Review scripts and setup commands for side effects; avoid elevated privileges and unexpected networking.
- [ ] Run `aihub dev validate` and resolve all errors; review every warning.
- [ ] Build with `aihub dev build`, inspect the archive (for example, `tar -tzf dist/<name>-<version>.tar.gz`), and test installation/uninstallation on the OSes you claim to support.
- [ ] Confirm login, `publish` permission, repository access, and intended visibility.
- [ ] Keep secrets and local build artifacts out of the archive.

## Troubleshooting

| Message or symptom | Meaning and next step |
| --- | --- |
| `version already exists (immutable)` | That version was published previously. Bump the version and publish a new release. |
| `package name is not available` | A package with that name exists but is not visible to you. Choose a different name or ask its owner/admin. |
| `you need develop access to publish to <name>` | You can see the repository but lack develop access. Ask a repository admin to grant it. |
| `script not found in package: <path>` | A script path ending in a supported extension is missing from the extracted archive. Include it or correct the manifest. |
| `script path escapes the package: <path>` | A script path resolves outside the installed package. Use an in-package relative path. |
| `[bin] <command> points outside the package or to a missing file: <path>` | The binary target is missing or escapes the package root. Fix the `[bin]` path. |
| `skill '<name>' has no SKILL.md in <path>` | A declared skill directory must contain `SKILL.md`. |
| `type="mcp" needs at least one [[mcp_servers]] entry` | Add an MCP server declaration. |
| `type="skill" but there is no [[skills]] entry, so nothing will be registered` (warning) | The skill type is valid, but no skill component will be registered. |
| `type="agent" but there is no [[agents]] entry, so nothing will be registered` (warning) | The agent type is valid, but no agent component will be registered. |
| `type=tool but no install script for: macos, linux, windows` (warning example) | An agent, MCP, or tool with no `[bin]`, `[[agents]]`, or `[[mcp_servers]]` entry lacks install scripts for one or more claimed OSes. Add the needed scripts or adjust `requires.os`. |
| `[package] table missing` | Add the required `[package]` table. |
| `every [[skills]] entry needs a name` | Add a non-empty `name` to each skill entry. The same message shape applies to `[[agents]]` and `[[mcp_servers]]`. |
| `duplicate name in [[skills]]` | Component names must be unique within that component table. |
| `[[mcp_servers]] '<name>' needs a command` | Set a command for the named MCP server. |
| `package.description is empty; it is what people search and read first` (warning) | Add a useful description. |
| `README '<path>' not found; the package page will have no Overview` (warning) | Add the README or correct `package.readme`. |
| `1 problem(s) in the project; fix them and run \`aihub dev validate\` again` | Validation found one or more file-level errors. Fix the `error:` lines printed immediately above it. |

TOML parsing and manifest normalization errors also stop `validate`; their exact text identifies the missing or invalid field. The validator has two stages. Manifest parsing and normalization errors stop immediately: missing `[package]`, invalid package name/version/type, unknown setup-step kind, invalid `[scripts]` key, missing or duplicate component name, missing MCP command, or unsupported `requires.os` value. File-level lint errors include missing/out-of-project component and `[bin]` paths, a skill directory without `SKILL.md`, missing in-package script files, and an MCP package without `[[mcp_servers]]`. Warnings include an empty description, missing README, skill/agent types without their component entries, and agent/MCP/tool packages with no self-installing component or binary and no install script for a claimed OS. Warnings do not prevent `dev validate` from succeeding.
