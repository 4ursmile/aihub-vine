---
name: aihub-package
description: Use when the user wants to create, convert, restructure, validate, or publish a project as an AI Hub package (aihub.toml, skills, agents, MCP servers, tools, setup profiles), new or existing.
---

# AI Hub packaging

The packaging workflow lives in the hub docs, so it is always current. Fetch it, then follow it. Work in the project root and never publish without the user's OK.

## 0. Find the CLI (Windows-safe)

Run `aihub --version`. If the shell says `aihub: command not found` (common on Windows, where the PATH only updates in new terminals, or in Git Bash, which does not run `.cmd` files), use the full path instead and keep using it for every command below:

- macOS / Linux / Git Bash: `~/.aihub/bin/aihub`
- Windows PowerShell / cmd: `%USERPROFILE%\.aihub\bin\aihub.cmd`
- Last resort (any OS): `python ~/.aihub/bin/aihub.pyz` (or `py -3 ~/.aihub/bin/aihub.pyz`)

If none exist, the CLI is not installed: ask the user to run the hub's installer (`$HUB/install.sh`, or `irm $HUB/install.ps1 | iex` on Windows).

## 1. Find the hub URL

```
aihub config        # prints JSON with "hub"; or: aihub doctor (prints "hub: <url>")
```

Use that value as `$HUB` (default `http://localhost:8000`).

## 2. Fetch the workflow and the references

```
curl -fsSL $HUB/api/v1/docs/publishing/raw     # read the "Assistant packaging workflow" section first
curl -fsSL $HUB/api/v1/docs/manifest/raw       # aihub.toml fields, setup steps, scripts
curl -fsSL $HUB/api/v1/docs/publishing-examples/raw   # worked examples to copy from
```

If `curl` is unavailable use `python3 -c "import urllib.request,sys;print(urllib.request.urlopen(sys.argv[1]).read().decode())" URL`. For protected hubs add `-H "Authorization: Bearer $TOKEN"` (create a token with `aihub login`; never print or store it in project files).

## 3. Follow it

Do the five steps of "Assistant packaging workflow" (detect, choose type, restructure, write the manifest, validate/build/publish). If the docs and this skill disagree, the docs win. Finish by reporting: package name/version/type, files moved, warnings left, and the install command `aihub install <name>`.
