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

Without `curl`, use `python3 -c "import urllib.request,sys;print(urllib.request.urlopen(sys.argv[1]).read().decode())" URL`. For protected hubs add `-H "Authorization: Bearer $TOKEN"` (create a token with `aihub login`; never print or store it in project files).

## 3. Follow it

Do the five steps of "Assistant packaging workflow": detect the situation, choose the type (skill, agent, mcp, tool, setup), restructure the project, write `aihub.toml` and the files, then validate, build and publish. Use `aihub dev init . --type <type> --name <name> --description "..."` for a new project and `aihub dev validate` for any project. Publish only with `aihub dev publish --bump patch` (or `minor`/`major`) after the user agrees, and only when signed in with publish permission.

If the docs and this skill disagree, the docs win.

Finish by reporting: package name, version and type; files moved; warnings left; and the install command `aihub install <name>`.
