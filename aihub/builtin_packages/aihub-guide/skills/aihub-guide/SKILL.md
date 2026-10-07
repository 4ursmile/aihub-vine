---
name: aihub-guide
description: Use when the user asks how to use AI Hub (aihub CLI, install/search/update packages, publish, hub API, admin settings) or wants a project set up for a specific case that is not covered by a fixed recipe; fetches the latest docs as Markdown from the hub API first.
---

# AI Hub guide (always read the live docs first)

## 0. Find the CLI (Windows-safe)

Run `aihub --version`. If the shell says `aihub: command not found` (common on Windows, where the PATH only updates in new terminals, or in Git Bash, which does not run `.cmd` files), use the full path instead and keep using it for every command below:

- macOS / Linux / Git Bash: `~/.aihub/bin/aihub`
- Windows PowerShell / cmd: `%USERPROFILE%\.aihub\bin\aihub.cmd`
- Last resort (any OS): `python ~/.aihub/bin/aihub.pyz` (or `py -3 ~/.aihub/bin/aihub.pyz`)

If none exist, the CLI is not installed: ask the user to run the hub's installer (`$HUB/install.sh`, or `irm $HUB/install.ps1 | iex` on Windows).

The docs change with each hub release. Do not answer from memory: fetch the current Markdown from the hub, then follow it.

## 1. Find the hub URL

```
aihub config        # prints JSON with "hub"; or: aihub doctor (prints "hub: <url>")
```

Use that value as `$HUB` below (default `http://localhost:8000`). If the CLI is missing, ask the user for the hub address; the installer is `$HUB/install.sh`.

## 2. Fetch the docs (no sign-in needed for docs)

```
curl -fsSL $HUB/api/v1/docs                  # JSON list: slug, title, raw_url
curl -fsSL $HUB/api/v1/docs/<slug>/raw       # one document as Markdown
```

Typical slugs and when to read them:

| slug | read it for |
| --- | --- |
| cli | every `aihub` command and flag, tool integration paths |
| manifest | `aihub.toml` fields, setup steps, scripts, requirements |
| publishing | project layouts per package type, visibility, release steps |
| publishing-examples | complete worked examples to copy from |
| api | REST endpoints, auth, upload protocol |
| configuration | server environment variables |
| server-setup | deploying and operating a hub |

Fetch only what the task needs (usually `cli` plus one of `manifest` / `publishing`). If a slug is missing, use the list from the first call. If `curl` is unavailable use `aihub` output or `python3 -c "import urllib.request,sys;print(urllib.request.urlopen(sys.argv[1]).read().decode())" URL`. For protected hubs add `-H "Authorization: Bearer $TOKEN"` (create a token with `aihub login`; never print or store it in project files).

The live endpoint list is also at `$HUB/openapi.json`.

## 3. Pick the workflow

- **Use the hub**: search, info, install, update, uninstall - take exact flags from the `cli` doc and run them; confirm with the user before installing anything with scripts.
- **Package a project**: if the `aihub-package` skill is installed, follow it; otherwise follow `publishing` and `manifest`.
- **Dynamic case** (the user's project does not fit a standard recipe, e.g. several components, an MCP server plus a skill, a setup profile for a team): read `manifest` and `publishing-examples`, choose the closest example, adapt it, then run `aihub dev validate` until clean.
- **Admin/server questions**: read `server-setup` and `configuration`; admin settings (sign-up, visibility, accent color, upload limit) live in the web UI under Admin > Settings.

## 4. Rules

- Quote the doc you relied on (slug) when you explain a choice, so the user can check it.
- If the docs and this skill disagree, the docs win.
- Never publish, delete, or change server settings without the user's explicit OK.
- Never put tokens or secrets into files you create.
