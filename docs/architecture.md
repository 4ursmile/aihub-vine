# Architecture

AI Hub no longer hosts package files. Three systems share the work:

| System | Holds | Written by | Read by |
|---|---|---|---|
| **Git** (GitLab, GitHub, any remote) | The package files; the index as a backup copy (a folder, default `index`) | `aihub dev publish`; the server (index backup) | `aihub install`; the server, for publishes that carry no bundle |
| **Git telemetry inbox** (any git remote, often the index repo) | Usage and lifecycle events as one file per CLI push on the single `telemetry-inbox` branch; the archive branch `telemetry-archive` keeps every event | The CLI and its hooks | The server's event sync |
| **AI Hub server** | A SQLite or Postgres cache of both, accounts, roles, reviews, groups, the dashboard | The syncs | The web UI |

Git applies its own access policy to the packages, the index and the telemetry inbox. The server adds nothing in front of it. CLIs and the server need push rights on the inbox repo.

## Identity

Signed-in users appear under their account name. Everyone else appears as `~<os-username>` (the account name of the person running the CLI), so anonymous activity is still attributable. The permission matrix has an anonymous principal for the web UI.

## Install

1. The CLI reads the index from the hub (`/index/catalog`, revalidated with ETag). It uses git only when the hub cannot serve the index (`aihub config set index_url <git url>`, or `aihub setup`).
2. It resolves versions and dependencies from the index, merging constraints from every dependent, and rejects an unsupported OS before cloning anything.
3. It clones the package repo at the entry's branch, subdir and ref, then follows the repo's own `aihub.toml` to install and register.
4. It records the commit. `aihub lock` pins that commit and `aihub sync` installs exactly it.

An index entry may point at any repo and branch. A version can override the repo, so a monorepo with one tag per version works, and so does a package that moved: the entry's `repo` is where the LATEST version lives, and a version published from somewhere else (an older repo, branch or folder) carries its own full `repo`, so `aihub install NAME@old` looks for the old commit where it exists. The hub fills this in on every publish and repairs older data (see Index format).

## Usage events

Hooks for Claude Code and OpenCode spool events locally and a detached background process sends them, so the agent is never slowed. Ids are derived from the event, so a retry or a second machine cannot create a duplicate. A skill, agent or MCP server is reported once per session and package (MCP tools collapse to their server).

Event names: `aihub.use`, `aihub.install`, `aihub.update`, `aihub.uninstall`, `aihub.error`, `aihub.publish`.

## Publish

`aihub dev init` creates `aihub.toml` and `.gitignore` if missing and fills `[git]` (url, branch, subdir) from the working repo's `origin` and current branch, asking only for what is still missing. `aihub dev publish` then:

1. checks that the remote and branch match `[git]` and refuses on a mismatch;
2. runs `git init` and adds `origin` if needed, tracks files over 50 MB with git-lfs, commits, and pushes (setting upstream the first time);
3. sends an `aihub.publish` event with package, version, commit, repo and branch, and a `bundle` holding the `aihub.toml` and README.

Nobody writes the index entry by hand or from CI. The hub builds it from the `bundle` in the publish event and serves it from its database; the index repo gets a backup copy only when the legacy git index sync is on.

## Server sync

On a schedule you set in Admin > Sync (seconds or cron) the server:

- reads the `inbox/*.jsonl` files on the `telemetry-inbox` branch (and, from older CLIs, the `telemetry/<client>/<batch>` branches and `refs/telemetry/*`) and inserts the events keyed by their unique event id (`events.ext_id`), so restarts, resent files and several servers never duplicate rows. Oversized (over 5 MB) files and malformed lines are dropped;
- copies each raw file to `archive/<YYYY-MM-DD>/<client>-<unix>-<rand>.jsonl` on the archive branch (default `telemetry-archive`; switch off with the `telemetry_archive` admin setting), and only then removes exactly those files from the inbox, deletes any legacy branches and refs, and runs `git gc --auto`. The archive commit, the inbox clean-up commit and the legacy deletions leave in a single `git push --atomic` (all or nothing, so a file is never removed without its archive copy), and everything held leaves together once per `telemetry_git_interval` rather than once per pass. A pass starts with ONE `git ls-remote`: when the inbox tip is unchanged and no legacy refs exist it fetches nothing and stops there. Batches are read with `git cat-file --batch` (one process for many files) and the archive commit is built with plumbing on a depth-1 fetch of the archive tip (no checkout), so the cost per pass does not grow with the archive. If any step fails they stay and the next pass retries, so nothing is lost. A file a CLI adds after the server's fetch is left alone. Git keeps a durable copy of every event;
- creates the archive branch on the first file when the repo exists but the branch does not (the CLI never touches the archive branch: it only adds files to `telemetry-inbox`);
- serves the package index itself: `GET /api/v1/index/catalog` and `/index/packages/{name}` are built from the database (the same data as the web UI), filtered by who is asking, cached in memory per viewer scope with `ETag`/`304`; the database is queried again only after a package changes. A publish is in the database at once, so the hub shows it immediately; if the legacy git index sync is on, the index repo is only pushed (one commit per `index_git_interval`, default 30 min from the first publish, rebuilt on the new tip if someone else pushed meanwhile), never read back, because the database is covered by the git backup. CLIs read the index from the hub first (revalidating with `If-None-Match`, trusting their copy for `client_index_ttl` seconds) and use git only when the hub cannot serve it;
- keeps the event sync and the backup apart: git work (a fetch or a push) of one starts at least `git_job_gap` seconds (default 300) after the other's last git work and never while it runs. The backup waits for that moment; the sync never blocks, it records "waiting for the backup" on the Sync page and comes back after the gap (its events are already in the database). An idle pass (one `git ls-remote`) is not git work for this rule. The backup's start time is also moved at random by up to `backup_jitter` percent of the wait (default 10, at most `backup_jitter_max` seconds, default 600), so it forms no fixed pattern. "Sync now" and "Back up now" skip the gap;
- while the server is down, CLIs keep adding files to `telemetry-inbox`, which simply grows; the next pass ingests them;
- when the database holds no events (lost, replaced) or an admin forces a full sync, it first replays every `archive/**/*.jsonl` from the archive branch. Inserts are idempotent, so replaying over a healthy database adds nothing. The database's own git backup covers everything else.
- builds each publish entry from its bundle (older CLIs: from the repository at the published commit), serves it at once, and keeps it queued until it is indexed (retry with backoff, up to 8 tries; the reason for a failure shows on the Sync tab);

A failed pass reports the error on the Sync tab.

## Telemetry from the CLI

**Hub first, git as the fallback.** The flusher sends the batch to `POST /api/v1/telemetry` on the hub. A hub that answers `429`/`503` is busy, not gone: the batch stays spooled and the CLI backs off for at least the `Retry-After` it was given, and does not touch git. Git is used only when the hub cannot be reached at all (or has no such route, an older hub) on 3 consecutive attempts, so a short hub restart never sends every client to the git host. The delay after a failure is 30 s doubling to 1 h, each time randomly 50-100% of that, so clients never retry in step. `AIHUB_TELEMETRY_VIA=git` (or `aihub config set telemetry_via git`) forces git only. The hub keeps events in memory and writes the database once per `ingest_db_interval` (default 10 s, from the first event of the window); the raw lines are held and leave for git in one push per `telemetry_git_interval` (default 1 h, early at 16 MiB or 2000 inbox files). The CLI gzips bodies of 512 bytes or more (retrying once uncompressed if an older hub answers 400/415) and reads `index_etag` from the answer: if it differs from its cached catalog, the cache's trust window is expired so the next command revalidates at once. The hub skips already-seen event ids (LRU of 200k) and counts 429/503 answers on Admin > Sync. Every accepted line is first appended and fsynced to `<data_dir>/ingest-journal/`, removed only after the database commit and replayed at start, so a kill -9 loses no acknowledged event (if the journal cannot be written the hub answers `503` and the CLI keeps its spool). Hot read routes share one query per cold key (single-flight cache) so many concurrent viewers cost one database read per TTL. The CLI only deletes its spool after a `2xx`.

Hooks append each event (with a unique `id`) to a local spool. A detached flusher waits `telemetry_interval` seconds (default 15, between 5 and 3600) so a burst becomes one batch, then adds one file, `inbox/<client_id>-<unix>-<rand>.jsonl`, as a commit on top of the tip of the `telemetry-inbox` branch. Every client writes a different file, so a race is a non-fast-forward rejection: the CLI refetches the tip (depth 1, so the cost does not grow with history), adds its file again and retries (up to 12 times, with jittered back-off). Pushes never conflict in content, and while the server is down the files simply pile up in that one branch. Every push outcome is written to `queue/status.json`. Nothing is pushed when nothing is spooled. A failed push keeps the spool; the CLI then starts no flusher for a back-off delay (30 s, doubling to 15 min, stored in `queue/backoff.json`), and `aihub flush` retries at once. `aihub dev publish` pushes its publish event immediately so the package is indexed quickly, and spools it if the push fails.

The inbox is resolved as: environment (`AIHUB_TELEMETRY_URL`, `AIHUB_TELEMETRY_INBOX_BRANCH`) > `config.json` (`telemetry_url`, `telemetry_inbox_branch`) > the bundled `aihub/core/defaults.json` (`telemetry.git_url`, `telemetry.inbox_branch`, shipped in both the pip package and `aihub.pyz`) > the package index repo (URL only). The server uses the same order with its own settings in place of `config.json`. The hub does not send the inbox branch to CLIs, so change it on both sides. Setup does not copy the bundled values into `config.json`, so a changed default reaches existing installs.

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

**Per-version repo.** A version may carry `"repo": {"url","branch","subdir"}`, merged over the entry's (`dict(entry.repo, **version.repo)`). It is stored only for a version that lives somewhere else than the entry's repo (the package moved after it was published); an empty `subdir` is kept as `""` then, because an absent one would read as the entry's. A package that never moved has no per-version repo, so its entry is unchanged and old CLIs read it as before. Comparison ignores host case, a trailing `/` and `.git`. The hub keeps where each publish came from (the publish event's `detail`), repairs old databases at start from it (`version_repo_backfill`, plus the git archive when the legacy telemetry sync is on), and, when a pinned commit is not in the repo the index names, `install -i` tries the package's other known locations and remembers the one that has it; the CLI does the same with the other repos listed in the entry.
