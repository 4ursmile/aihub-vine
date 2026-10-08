---
name: aihub-guide
description: Use when the user asks how to use AI Hub (aihub CLI, install/search/update packages, publish, hub API, admin settings) or wants a project set up for a specific case that is not covered by a fixed recipe; fetches the latest docs as Markdown from the hub API first.
---

# AI Hub guide (always read the live docs first)

The docs change with each release. Do not answer from memory: fetch the current Markdown from the hub, then follow it.

## 0. Find the CLI

Run `aihub version`. If the shell says `aihub: command not found` (common on Windows right after install, or in Git Bash, which cannot run `.cmd` files), use the full path for every command:

- macOS / Linux / Git Bash: `~/.aihub/bin/aihub`
- Windows PowerShell / cmd: `%USERPROFILE%\.aihub\bin\aihub.cmd`
- Last resort: `python3 ~/.aihub/bin/aihub.pyz` (Windows: `py -3 ...`)

If none exists, the CLI is not installed: ask the user to run the hub's installer (`$HUB/install.sh`, or `irm $HUB/install.ps1 | iex` on Windows).

## 1. Find the hub URL

```
aihub config        # JSON; the "hub" key is the URL
aihub doctor        # checks the connection and prints the hub URL
```

Use that value as `$HUB` (default `http://localhost:8000`). If the CLI is missing, ask the user for the hub address.

## 2. Fetch the docs (no sign-in needed)

```
curl -fsSL $HUB/api/v1/docs                  # JSON list: slug, title, raw_url
curl -fsSL $HUB/api/v1/docs/<slug>/raw       # one document as Markdown
```

| slug | read it for |
| --- | --- |
| cli | every `aihub` command and flag, tool install locations, usage hooks, lock/sync |
| manifest | `aihub.toml` fields, setup steps, scripts, requirements |
| publishing | project layouts per package type, visibility, release steps, the assistant packaging workflow |
| publishing-examples | complete worked examples to copy from |
| api | REST endpoints, auth, admin routes |
| configuration | server environment variables |
| server-setup | deploying, upgrading and operating a hub |
| architecture | how git, the index, Langfuse and the hub fit together |

Fetch only what the task needs. If a slug is missing, use the list from the first call. Without `curl`, use `python3 -c "import urllib.request,sys;print(urllib.request.urlopen(sys.argv[1]).read().decode())" URL`. For protected hubs add `-H "Authorization: Bearer $TOKEN"` (create a token with `aihub login`; never print or store it in project files).

The live endpoint list is at `$HUB/openapi.json`.

## 3. Pick the workflow

- **Use the hub**: search, info, install, update, uninstall, enable/disable, lock/sync. Take exact flags from the `cli` doc and run them. Confirm with the user before installing anything that runs scripts.
- **Package a project**: if the `aihub-package` skill is installed, follow it; otherwise follow `publishing` and `manifest`.
- **Dynamic case** (several components, an MCP server plus a skill, a team setup profile): read `manifest` and `publishing-examples`, adapt the closest example, then run `aihub dev validate` until clean.
- **Admin/server questions**: read `server-setup` and `configuration`. Site settings are in the web UI under Admin > Settings (sign-up mode, public browse and install, private repositories, default visibility, source download, group creation, accent color, site name and contact). Admin API: `GET/PUT /api/v1/admin/settings`.
- **Backups** (server-side, scheduled): the server keeps database backups in the same git repo as the package index, as split gzipped SQLite files plus `backups/index.json`. Admin API: `GET/PUT /api/v1/admin/backup` and `POST /api/v1/admin/backup/run`. Settings: `backup_enabled`, `backup_schedule` (cron, default `0 3 * * *`), `backup_events_chunk`. Read `server-setup` for the current procedure before changing these.

## 4. Rules

- Cite the doc slug you relied on when you explain a choice.
- If the docs and this skill disagree, the docs win.
- Never publish, delete, or change server settings without the user's explicit OK.
- Never put tokens or secrets into files you create.
