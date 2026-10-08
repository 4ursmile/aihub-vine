# Publishing examples

These examples assume `aihub` is configured with an index and Langfuse (`aihub setup`) and that your git credentials can push to each project's `[git]` repository. The generated project trees were compared with `aihub dev init` output and each starter passed `aihub dev validate`. Replace the generated placeholder content before publishing.

## Skill package

Create a starter project:

```sh
aihub dev init ./release-notes --type skill --name release-notes --description "Instructions for writing release notes"
cd release-notes
aihub dev validate
```

Generated files:

```text
.gitignore
README.md
aihub.toml
skills/release-notes/SKILL.md
```

The generated manifest includes package metadata, `[requires]`, and this registration:

```toml
[[skills]]
name = "release-notes"
path = "skills/release-notes"
```

Replace the placeholder in `skills/release-notes/SKILL.md`. For example, keep its YAML front matter and write a concrete trigger, steps, and expected output. Update `README.md` with examples of when and how to use the skill. Then validate and publish:

```sh
aihub dev validate
aihub dev publish
```

During `aihub install release-notes`, the CLI asks which detected tools should receive the skill. It does not run an install script for this starter. Uninstall removes the installed copy and package files; it does not remove general usage hooks installed for the tool.

## MCP server package

Create the stdio MCP starter:

```sh
aihub dev init ./echo-mcp --type mcp --name echo-mcp --description "A small stdio echo MCP server"
cd echo-mcp
aihub dev validate
```

Generated files:

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

The generated manifest's MCP and script sections are:

```toml
[[mcp_servers]]
name = "echo-mcp"
command = "python3"
args = ["${PKG}/server/server.py"]
env = {}

[scripts]
install_macos = "scripts/install.sh"
install_linux = "scripts/install.sh"
install_windows = "scripts/install.ps1"
uninstall_macos = "scripts/uninstall.sh"
uninstall_linux = "scripts/uninstall.sh"
uninstall_windows = "scripts/uninstall.ps1"
```

`server/server.py` is a minimal standard-library JSON-RPC-over-stdio example with an `echo` tool. Replace its example tool with your implementation and update the README. Make sure the command and runtime named in the manifest are installed on every supported OS. The generated install and uninstall scripts only print a message.

Validate and publish:

```sh
aihub dev validate
aihub dev publish
```

On install, AI Hub asks before running the matching OS install script. It replaces `${PKG}` in MCP arguments with the installed package directory, then asks which detected tools should receive the MCP registration. `aihub uninstall echo-mcp` runs the matching uninstall script without prompting, removes recorded MCP registrations, and removes the package files. The generated uninstall script is a no-op message.

## Cross-platform command-line tool

Generate a Python command starter:

```sh
aihub dev init ./greet-tool --type tool --name greet-tool --description "Print a greeting"
cd greet-tool
aihub dev validate
```

Generated files:

```text
.gitignore
README.md
aihub.toml
bin/greet-tool.py
scripts/install.ps1
scripts/install.sh
scripts/uninstall.ps1
scripts/uninstall.sh
```

The manifest contains this command mapping and platform script selection:

```toml
[bin]
greet-tool = "bin/greet-tool.py"

[scripts]
install_macos = "scripts/install.sh"
install_linux = "scripts/install.sh"
install_windows = "scripts/install.ps1"
uninstall_macos = "scripts/uninstall.sh"
uninstall_linux = "scripts/uninstall.sh"
uninstall_windows = "scripts/uninstall.ps1"
```

The generated command accepts an optional name and prints a greeting. The scripts are examples, not a package-manager installer. If the command needs an external prerequisite, edit all platform scripts. For example, these scripts check for `myformatter` but leave its installation to the user.

`scripts/install.sh` for macOS and Linux:

```sh
#!/bin/sh
set -eu
echo "greet-tool: checking myformatter"
if ! command -v myformatter >/dev/null 2>&1; then
  echo "myformatter is required. Install it with your system package manager, then retry." >&2
  exit 1
fi
```

`scripts/uninstall.sh`:

```sh
#!/bin/sh
set -eu
echo "greet-tool: myformatter is managed separately and is not removed"
```

`scripts/install.ps1` for Windows:

```powershell
$ErrorActionPreference = 'Stop'
Write-Host "greet-tool: checking myformatter"
if (-not (Get-Command myformatter -ErrorAction SilentlyContinue)) {
  Write-Error 'myformatter is required. Install it with your package manager, then retry.'
  exit 1
}
```

`scripts/uninstall.ps1`:

```powershell
$ErrorActionPreference = 'Stop'
Write-Host "greet-tool: myformatter is managed separately and is not removed"
```

These script examples use file paths inside the package and run with its directory as the working directory. AI Hub sets `AIHUB_PACKAGE_DIR` in their environment. Installation asks before running an install script unless `--yes` is supplied. Validate and publish:

```sh
aihub dev validate
aihub dev publish
```

The installer creates `greet-tool` in the AI Hub CLI bin directory. POSIX systems receive a shell shim; Windows receives `greet-tool.cmd`. Add the CLI bin directory to `PATH` to invoke it. Uninstall runs the matching uninstall script, removes the shim and package directory, and removes any package virtual environment. It does not remove the separately managed `myformatter` executable.

## Setup profile

Generate a profile with reversible starter changes:

```sh
aihub dev init ./editor-basics --type setup --name editor-basics --description "Basic editor environment setup"
cd editor-basics
aihub dev validate
```

Generated files:

```text
.gitignore
README.md
aihub.toml
```

The generated manifest contains an `env` step and a `file` step. These are the generated steps with the placeholder values filled in:

```toml
[[setup.steps]]
kind = "env"
name = "EDITOR_BASICS_HOME"
value = "${HOME}/.editor-basics"

[[setup.steps]]
kind = "file"
path = "${HOME}/.editor-basics/config.txt"
content = "hub=${HUB}\n"
```

Edit these values and describe the effects in `README.md`. Do not put credentials in a profile. AI Hub asks before each step unless `--yes` is supplied. The environment step adds a managed export block to `~/.zshrc` and to `~/.bashrc` if that file exists. The file step saves a backup when replacing an existing file. On uninstall, AI Hub removes its managed shell blocks and restores the previous config file, or deletes the new file if there was no previous one. Keep the CLI's install state until uninstall is complete.

Validate and publish:

```sh
aihub dev validate
aihub dev publish
```
