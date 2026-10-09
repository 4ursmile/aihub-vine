# Manifest reference

Each package archive contains an `aihub.toml` manifest. The CLI's `aihub dev init` command creates a starter file. The server and CLI parse TOML and normalize known fields; fields not described below are not necessarily preserved.

## Package metadata

`[package]` is required. `name` and `version` must be present and valid; other metadata fields have defaults. The package index entry is built by the server from this file at the published commit, so the entry's name and version come from here.

| Field | Default | Meaning |
| --- | --- | --- |
| `name` | required | Canonical package name. Names are trimmed, lowercased, and runs of `-`, `_`, or `.` are converted to `-`. The normalized name must be 1–64 characters and start and end with a lowercase letter or digit. |
| `version` | required | Version string, such as `1.2.0` or `1.2.0rc1`. See [version rules](#versions-and-constraints). |
| `type` | `tool` | One of `skill`, `agent`, `mcp`, `tool`, or `setup`. |
| `description` | `""` | Short package description. |
| `tags` | `[]` | Tags are converted to lowercase strings. |
| `readme` | `"README.md"` | Path of the README inside the package; lint warns if the file is missing. The index entry does not use this path: it stores the text of the first README found in the package root (`README.md`, `readme.md`, `Readme.md`, `README.markdown`, or `README`), capped at 20000 characters. |
| `license` | `""` | License identifier or text. |
| `authors` | `[]` | Who made the package, shown on its web page. Each entry is `"Name"`, `"Name <email>"`, or a table `{ name = "Name", email = "...", url = "https://..." }`. |

Example:

```toml
[package]
name = "git-helper"
version = "1.0.0"
type = "tool"
description = "Small git workflow helpers"
tags = ["git", "workflow"]
readme = "README.md"
license = "MIT"
authors = ["Ada Lovelace <ada@example.com>"]
```

## Git source

`[git]` is optional. It tells the server where to fetch the package: `url`, `branch`, and `subdir` (the package folder inside the repository). Only public git hosts, or the index's own host, are followed.

```toml
[git]
url = "https://git.example.com/team/git-helper.git"
branch = "main"
subdir = "packages/git-helper"
```

## Requirements and Python dependencies

`[requires]` is optional. The installer warns if a required command is missing, recursively installs package dependencies, and checks supported operating systems. In the package index, `requires` is stored on each version, not on the package.

```toml
[requires]
commands = [
  { name = "git", hint = "Install Git from your system package manager" },
  "jq",
]
packages = ["common-lib>=1.0,<2"]
os = ["macos", "linux"]
```

- `commands`: command names as strings or objects with `name` and optional `hint`.
- `packages`: package names, optionally followed by version constraints.
- `os`: zero or more of `macos`, `linux`, and `windows`. An empty list means no OS restriction.

Python dependencies are installed into a package-specific virtual environment if either field is set:

```toml
[python]
requires = ["requests>=2"]
requirements_file = "requirements.txt"
```

`requires` is a list passed to pip. `requirements_file` names a file inside the extracted package. This section is not deeply validated during manifest parsing; errors can surface during installation.

## Skills, agents, and MCP servers

Each component section is an array of tables. Paths are relative to the package archive's extracted root.

```toml
[[skills]]
name = "git-helper"
path = "skills/git-helper"

[[agents]]
name = "reviewer"
path = "agents/reviewer.md"

[[mcp_servers]]
name = "repo-tools"
command = "python3"
args = ["${PKG}/server.py"]
env = { LOG_LEVEL = "info" }
```

MCP server entries provide `name`, `command`, optional `args` (default `[]`), and optional `env` (default `{}`). The `${PKG}` variable is replaced with the installed package directory in MCP arguments.

## Binaries and scripts

`[bin]` maps a command name to a path inside the package. The installer creates a shim under the CLI's `bin/` directory.

```toml
[bin]
repo-report = "bin/repo_report.py"
```

OS-specific scripts use `install_<os>` and `uninstall_<os>` keys, where `<os>` is `macos`, `linux`, or `windows`:

```toml
[scripts]
install_macos = "./scripts/install-macos.sh"
uninstall_macos = "./scripts/uninstall-macos.sh"
```

The install script asks for confirmation unless install is run with `--yes`. The uninstall script is run during uninstall. These are shell commands executed from the package directory.

## Setup profiles

`[setup]` can contain one or more `[[setup.steps]]` records. Each kind asks for confirmation unless the user runs installation with `--yes`. `${HOME}`, `${PKG}`, and `${HUB}` are substituted in supported string values; paths also expand `~`.

```toml
[[setup.steps]]
kind = "env"
name = "MY_TOOL_HOME"
value = "${PKG}"

[[setup.steps]]
kind = "json_merge"
path = "${HOME}/.config/my-tool/settings.json"
data = { packages = ["${PKG}"] }

[[setup.steps]]
kind = "file"
path = "${HOME}/.config/my-tool/managed.txt"
content = "Hub: ${HUB}\nPackage: ${PKG}\n"
mode = "600"

[[setup.steps]]
kind = "file"
path = "${HOME}/.config/my-tool/defaults.json"
source = "config/defaults.json"

[[setup.steps]]
kind = "block"
path = "${HOME}/.config/my-tool/config.toml"
id = "hub-entry"
content = "# managed by AI Hub"

[[setup.steps]]
kind = "command"
run = "${PKG}/scripts/setup.sh"
undo = "${PKG}/scripts/teardown.sh"
```

| Kind | Fields used by the installer | Behavior |
| --- | --- | --- |
| `env` | `name`, `value` | Adds a managed `export NAME="value"` block to `.zshrc` and to `.bashrc` if it exists. |
| `json_merge` | `path`, `data` | Deep-merges objects into a JSON file; appends only list items not already present. Backs up existing content for uninstall. |
| `file` | `path`, `content` or `source`, optional `mode` | Writes content, or copies text from a source file within the package. Existing content is backed up. `mode` is parsed as an octal string, such as `"600"`. |
| `block` | `path`, optional `id`, `content` | Adds or replaces an AI Hub managed text block in a file. The default ID is the step index. |
| `command` | `run`, optional `undo` | Runs a shell command. An `undo` shell command is recorded for uninstall. |

The setup table itself is passed through by manifest normalization. The parser checks that each step kind is one of the five values above, but does not validate every required kind-specific field or its type in advance. Missing or invalid fields can fail during installation. If applying a sequence fails, previously recorded steps are reverted in reverse order where possible.

## Full example

```toml
[package]
name = "git-helper"
version = "1.0.0"
type = "tool"
description = "Git workflow commands and agent helpers"
tags = ["git", "workflow"]
readme = "README.md"
license = "MIT"

[requires]
commands = [{ name = "git", hint = "Install Git first" }]
packages = []
os = ["macos", "linux"]

[python]
requires = []
requirements_file = ""

[[skills]]
name = "git-helper"
path = "skills/git-helper"

[[agents]]
name = "reviewer"
path = "agents/reviewer.md"

[[mcp_servers]]
name = "repo-tools"
command = "python3"
args = ["${PKG}/server.py"]
env = { LOG_LEVEL = "info" }

[bin]
repo-report = "bin/repo_report.py"

[scripts]
install_macos = "./scripts/install-macos.sh"
uninstall_macos = "./scripts/uninstall-macos.sh"

[[setup.steps]]
kind = "block"
path = "${HOME}/.config/git-helper/README.txt"
content = "Managed by AI Hub. Package files: ${PKG}"
```

## Versions and constraints

Version parsing accepts an optional `v`, dotted numeric components, and an optional `a`, `b`, `rc`, or `dev` prerelease suffix with optional digits. Examples include `v1.2.0`, `1.2`, `1.2.0rc1`, and `2.0dev`.

Resolver constraints are comma-separated comparisons using `==`, `!=`, `>=`, `<=`, `>`, `<`, or `~=`. An omitted operator means `==`. Empty string and `*` match any version. For example:

```text
>=1.0,<2
~=1.4
```

The `~=` implementation uses the parsed dotted numeric components to compare the shared prefix. A version already published for a package is immutable; publishing the same version again returns a conflict. Bump commands support `major`, `minor`, and `patch`.
