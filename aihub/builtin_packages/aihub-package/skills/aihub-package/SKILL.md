---
name: aihub-package
description: Use when the user wants to create, convert, restructure, validate, or publish a project as an AI Hub package (aihub.toml, skills, agents, MCP servers, tools, setup profiles), new or existing.
---

# AI Hub packaging

The packaging workflow lives in the hub docs, so it is always current. Fetch it, then follow it. Work in the project root and never publish without the user's OK.

## 0. Find the CLI

Run `aihub version`. If the shell says `aihub: command not found` (common on Windows right after install, or in Git Bash, which cannot run `.cmd` files), use the full path for every command:

- macOS / Linux / Git Bash: `~/.aihub/bin/aihub`
- Windows PowerShell / cmd: `%USERPROFILE%\.aihub\bin\aihub.cmd`
- Last resort: `python3 ~/.aihub/bin/aihub.pyz` (Windows: `py -3 ...`)

If none exists, the CLI is not installed: ask the user to run the hub's installer (`$HUB/install.sh`, or `irm $HUB/install.ps1 | iex` on Windows).

## 1. Find the hub URL

```
aihub config        # JSON; the "hub" key is the URL
```

Use that value as `$HUB` (default `http://localhost:8000`).

## 2. Fetch the workflow and references

```
curl -fsSL $HUB/api/v1/docs/publishing/raw            # read "Assistant packaging workflow" first
curl -fsSL $HUB/api/v1/docs/manifest/raw              # aihub.toml fields, setup steps, scripts
curl -fsSL $HUB/api/v1/docs/publishing-examples/raw   # worked examples to copy from
```

**HTTPS with a private or self-signed certificate:** if `curl` reports a certificate error, add `--cacert /path/ca.pem` (preferred) or `-k` to the `curl` calls above, and for Python use `ssl._create_unverified_context()` as `context=`. Only do this for a hub the user trusts; `aihub config set ca_bundle <ca.pem>` / `insecure_tls true` makes the aihub CLI itself accept it.

Without `curl`, use `python3 -c "import urllib.request,sys;print(urllib.request.urlopen(sys.argv[1]).read().decode())" URL`. For protected hubs add `-H "Authorization: Bearer $TOKEN"` (create a token with `aihub login`; never print or store it in project files).

## 3. Follow it

Do the five steps of "Assistant packaging workflow": detect the situation, choose the type (skill, agent, mcp, tool, setup), restructure the project, write `aihub.toml` and the files, then validate, build and publish. Use `aihub dev init . --type <type> --name <name> --description "..."` for a new project and `aihub dev validate` for any project.

**type = bot (chat bot).** Create it with `aihub dev init . --type bot --name <name>`. It needs `[bot] run = "bot/run.py"` (a file inside the package that `aihub run` starts), optional `title`, and one `[[bot.env]]` entry per setting (`name`, `hint`, `required`, `secret`, `default`). Optional: `[[mcp_servers]]` (used only by the bot), `[python] requires`, install scripts. Never put keys in the package; users fill `~/.aihub/bots/<name>/.env`. Full guide (layout, memory, harnesses, cron, tests): the `bots` doc on the hub.

**Interactive wizard.** `aihub dev init -w [path]` (terminal only) asks whether the folder becomes one package, a monorepo (and which existing sub folders become packages), or gets one more package in a workspace. Suggest it to a user who is unsure; as an assistant without a terminal use the flags below (`--mono`, `--sub`, `--name`, `--type`) instead.

**Several packages in one folder (monorepo).** If the folder should hold several packages, make it one repository with one package per sub folder: `aihub dev init . --mono --packages "name1,name2:mcp"` (each `NAME` or `NAME:TYPE`; add `--remote <url>` if known). Then run `aihub dev tree` to check the status, and `aihub dev doctor` if a `[git]` table is out of date. Run `aihub dev validate` on the workspace folder to check every package. Never create a package inside another package (a package folder must not sit within another). To work on one package, add `--only NAME` to `validate`, `build`, `publish` or `stats`. A nested `.git` shown by `aihub dev tree` is fixed with `aihub dev tree-solve`; ask the user which mode (merge or keep) they want. Publish only with `aihub dev publish --bump patch` (or `minor`/`major`) after the user agrees. It needs no hub account, only push access to the `[git]` repository. It pushes the repo and sends an `aihub.publish` event that carries the `aihub.toml` and README; never edit the package index by hand. The hub builds the index entry from that bundle and retries if indexing fails.

**Python packages.** `[python] requires` lists pip dependencies only. Set `[python] install = true` (default `false`) only when the package ships a `pyproject.toml` with console scripts (`[project.scripts]`): the installer then pip-installs the package folder into its venv and puts those commands on PATH. Without a `pyproject.toml` in the folder, validation fails, so use `requires` alone for plain dependencies.

If the docs and this skill disagree, the docs win.

Finish by reporting: package name, version and type; files moved; warnings left; and the install command `aihub install <name>`.
