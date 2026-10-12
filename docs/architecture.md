# Architecture

AI Hub no longer hosts package files. Three systems share the work:

| System | Holds | Written by | Read by |
|---|---|---|---|
| **Git** (GitLab, GitHub, any remote) | The package files and the index (`index.json`) | `aihub dev publish` | `aihub install`, the server's index sync |
| **Git telemetry inbox** (any git remote, often the index repo) | Usage and lifecycle events as small batches on branches `telemetry/<client>/<batch>`, then an archive branch | The CLI and its hooks | The server's event sync |
| **AI Hub server** | A SQLite or Postgres cache of both, accounts, roles, reviews, groups, the dashboard | The syncs | The web UI |

Git applies its own access policy to the packages, the index and the telemetry inbox. The server adds nothing in front of it. CLIs and the server need push rights on the inbox repo.

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

- fetches the `telemetry/*` branches (and, from older CLIs, `refs/telemetry/*`) from the inbox repo and inserts the events keyed by their unique event id (`events.ext_id`), so restarts, resent batches and several servers never duplicate rows. Oversized (over 5 MB) batches and malformed lines are dropped;
- copies each raw batch to `archive/<YYYY-MM-DD>/<client>-<batch>.jsonl` on the archive branch (default `telemetry-archive`; switch off with the `telemetry_archive` admin setting), and only then deletes those branches (and legacy refs) and runs `git gc`. If any step fails they stay and the next pass retries, so nothing is lost. A failed remote delete is ignored; the batch is read again next pass and deduplicated by event id. Git keeps a durable copy of every event;
- creates the archive branch on the first batch when the repo exists but the branch does not (the CLI never touches the archive branch: it only pushes `telemetry/*` branches);
- while the server is down, CLIs keep pushing and the batch branches simply pile up in git; the next pass ingests them in order;
- when the database holds no events (lost, replaced) or an admin forces a full sync, it first replays every `archive/**/*.jsonl` from the archive branch. Inserts are idempotent, so replaying over a healthy database adds nothing. The database's own git backup covers everything else.
- re-reads the git index on the first run and whenever a publish event arrives;

A failed pass reports the error on the Sync tab.

## Telemetry from the CLI

Hooks append each event (with a unique `id`) to a local spool. A detached flusher waits `telemetry_interval` seconds (default 15, between 5 and 3600) so a burst becomes one batch, then pushes one orphan commit to the branch `telemetry/<client_id>/<unix>-<rand>`. Each client writes only its own branch names, so pushes never conflict. Every push outcome is written to `queue/status.json`. Nothing is pushed when nothing is spooled. A failed push keeps the spool; the CLI then starts no flusher for a back-off delay (30 s, doubling to 15 min, stored in `queue/backoff.json`), and `aihub flush` retries at once. `aihub dev publish` pushes its publish event immediately so the package is indexed quickly, and spools it if the push fails.

The inbox is resolved as: environment (`AIHUB_TELEMETRY_URL`, `AIHUB_TELEMETRY_BRANCH`) > `config.json` (`telemetry_url`, `telemetry_branch`) > the bundled `aihub/core/defaults.json` (`telemetry.git_url`, `telemetry.branch`, shipped in both the pip package and `aihub.pyz`) > the package index repo (URL only). The server uses the same order with its admin settings in place of `config.json`. Setup does not copy the bundled values into `config.json`, so a changed default reaches existing installs.

## Credentials for clients

`aihub setup` (and `aihub welcome`) fill every missing location silently from env, `config.json` and the bundled `defaults.json`. The only prompt is for git credentials, and only when `git ls-remote` is refused and a terminal is attached; credentials already in your git credential helper are enough. It asks the server for the index location and the telemetry inbox (url and branch). Only locations are sent, never credentials. Values are stored in `~/.aihub/config.json`. Git credentials are never shared: sign in to your git host as usual (or enter them when the CLI asks); they are kept by your own git credential helper. The CLI refreshes them on the interval the admin sets. If the server is unreachable, install the CLI with pip from git and run `aihub setup --manual`, or export `AIHUB_INDEX_URL` (and `AIHUB_TELEMETRY_URL` if usage goes to a different repo).

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
