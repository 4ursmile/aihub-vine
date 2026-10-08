# Architecture

AI Hub no longer hosts package files. Three systems share the work:

| System | Holds | Written by | Read by |
|---|---|---|---|
| **Git** (GitLab, GitHub, any remote) | The package files and the index (`index.json`) | `aihub dev publish` | `aihub install`, the server's index sync |
| **Langfuse** | Usage and lifecycle events as OpenTelemetry spans | The CLI and its hooks | The server's event sync, `aihub dev stats` |
| **AI Hub server** | A SQLite or Postgres cache of both, accounts, roles, reviews, groups, the dashboard | The syncs | The web UI |

Git and Langfuse apply their own access policy. The server adds nothing in front of them.

## Identity

Signed-in users appear under their account name. Everyone else appears as `~<os-username>` (the account name of the person running the CLI), so anonymous activity is still attributable. The permission matrix has an anonymous principal for the web UI.

## Install

1. The CLI reads the index (`aihub config set index_url <git url>`, or `aihub setup`).
2. It resolves versions and dependencies from the index, merging constraints from every dependent, and rejects an unsupported OS before cloning anything.
3. It clones the package repo at the entry's branch, subdir and ref, then follows the repo's own `aihub.toml` to install and register.
4. It records the commit. `aihub lock` pins that commit and `aihub sync` installs exactly it.

An index entry may point at any repo and branch. A version can override the repo, so a monorepo with one tag per version works.

## Usage events

Hooks for Claude Code and OpenCode spool events locally and a detached background process sends them, so the agent is never slowed. Ids are derived from the event, so a retry or a second machine cannot create a duplicate. A skill, agent or MCP server is reported once per session and package (MCP tools collapse to their server).

Event names: `aihub.use`, `aihub.install`, `aihub.update`, `aihub.uninstall`, `aihub.error`, `aihub.publish`.

## Publish

`aihub dev init` creates `aihub.toml` and `.gitignore` if missing and fills `[git]` (url, branch, subdir) from the working repo's `origin` and current branch, asking only for what is still missing. `aihub dev publish` then:

1. checks that the remote and branch match `[git]` and refuses on a mismatch;
2. runs `git init` and adds `origin` if needed, tracks files over 50 MB with git-lfs, commits, and pushes (setting upstream the first time);
3. sends an `aihub.publish` span with package, version, commit, repo and branch.

Publishing the index itself (the `index.json` entry) is done by whoever owns the index repo, for example from CI on the publish event.

## Server sync

On a schedule you set in Admin > Sync (seconds or cron) the server:

- pulls Langfuse spans since a cursor with a small overlap, and inserts them keyed by Langfuse's observation id, so overlap, restarts and several servers never duplicate or lose rows;
- re-reads the git index on the first run and whenever a publish event arrives;
- on the first run looks back over everything.

A failed pass keeps the old cursor and reports the error on the Sync tab.

## Credentials for clients

`aihub setup` asks the server for the index location and, if an admin turned sharing on, the Langfuse keys. They are sent over HTTPS only, only to signed-in users or callers with the enrollment code, and each hand-out is audited. Values are stored in `~/.aihub/config.json` (mode 0600). Git credentials are never shared: sign in to your git host as usual (or enter them when the CLI asks); they are kept by your own git credential helper. The CLI refreshes them on the interval the admin sets. If the server is unreachable, install the CLI with pip from git and run `aihub setup --manual`, or export `LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and `AIHUB_INDEX_URL`.

## Index format

```json
{"packages": [{
  "name": "hello-af", "type": "skill", "description": "...", "tags": ["sample"],
  "latest_version": "0.1.0",
  "requires": {"os": ["macos", "linux", "windows"], "commands": ["git"], "packages": ["base-lib>=1"]},
  "python": {"requires": [], "requirements_file": ""},
  "repo": {"url": "https://github.com/org/repo", "branch": "main", "subdir": "hello-af"},
  "versions": [{"version": "0.1.0", "ref": "hello-af-0.1.0"}]
}]}
```

`requires` is used for planning and filtering. The `aihub.toml` in the repo is authoritative at install time.
