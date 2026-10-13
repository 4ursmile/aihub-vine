# Architecture

AI Hub no longer hosts package files. Three systems share the work:

| System | Holds | Written by | Read by |
|---|---|---|---|
| **Git** (GitLab, GitHub, any remote) | The package files and the index (a folder, `index/`, of JSON) | `aihub dev publish` (package repos); the server (index commits) | `aihub install` (package repos, and the index as a fallback) |
| **Kafka topic** (`aihub.events` by default) | Usage, lifecycle and publish events, one message per event | The CLI, its hooks and `aihub dev publish` | The server's Kafka consumer |
| **AI Hub server** | A SQLite or Postgres database holding the events and the package index, accounts, roles, reviews, groups, the dashboard | The Kafka consumer, the syncs and the index queue | The web UI, and the CLI's index lookup (`GET /api/v1/index`) |

Git applies its own access policy to the packages and the index. The server adds nothing in front of it. Usage and publish events need no git access at all: CLIs produce them to the Kafka topic. The server needs push rights on the index repo.

## Identity

Signed-in users appear under their account name. Everyone else appears as `~<os-username>` (the account name of the person running the CLI), so anonymous activity is still attributable. The permission matrix has an anonymous principal for the web UI.

## Install

1. The CLI asks the hub for the index (`GET /api/v1/index`, cached for 5 minutes and revalidated with an ETag). If the hub cannot be reached it reads the git index (`aihub config set index_url <git url>`, or `aihub setup`), cached for 5 minutes too. The hub index includes versions not yet committed to git.
2. It resolves versions and dependencies from the index, merging constraints from every dependent, and rejects an unsupported OS before cloning anything.
3. It clones the package repo at the entry's branch, subdir and ref, then follows the repo's own `aihub.toml` to install and register.
4. It records the commit. `aihub lock` pins that commit and `aihub sync` installs exactly it.

An index entry may point at any repo and branch. A version can override the repo, so a monorepo with one tag per version works.

## Usage events

Hooks for Claude Code and OpenCode spool events locally and a detached background process sends them to Kafka, so the agent is never slowed. Each event has a unique id, so a retry or a second machine cannot create a duplicate. A skill, agent or MCP server is reported once per session and package (MCP tools collapse to their server).

Event names: `aihub.use`, `aihub.install`, `aihub.update`, `aihub.uninstall`, `aihub.error`, `aihub.publish`.

## Publish

`aihub dev init` creates `aihub.toml` and `.gitignore` if missing and fills `[git]` (url, branch, subdir) from the working repo's `origin` and current branch, asking only for what is still missing. `aihub dev publish` then:

1. checks that the remote and branch match `[git]` and refuses on a mismatch;
2. runs `git init` and adds `origin` if needed, tracks files over 50 MB with git-lfs, commits, and pushes (setting upstream the first time);
3. sends an `aihub.publish` event with package, version, commit, repo and branch to Kafka (queued and retried if Kafka is unreachable).

The CLI does not write the index. When the server ingests a publish event for a version it does not have yet (or at a different commit), it shows the version on the web at once and queues it for the git index (see Server sync).

## Server sync

The server takes usage events in two ways and writes the package index on its own schedule.

- **Kafka consumer.** A consumer thread (group `aihub-server`) reads the configured topic and writes each event to the database as it arrives, so the dashboard updates at once. Offsets are committed only after the rows are stored, so delivery is at-least-once; `events.ext_id` is unique, so a redelivered message never creates a duplicate. Messages over 64 KB or that are not JSON objects are dropped. Admin > Sync shows whether the consumer is connected, how many messages it has consumed and its last error;
- **Git index.** Publish events for a version the database does not have yet are shown at once as a provisional package/version row; an event does not change an existing package's description, type or README. Each such version is also kept in a persisted queue (the `index_queue` setting). Once per git index commit interval (Admin > Sync, default 60 seconds, or cron) the server drains the queue: it fetches each package repo at the published commit, reads its `aihub.toml`, and makes one commit and one push for the whole batch. If the push fails the queue is kept and retried at the next interval; a version whose repo cannot be read is retried up to 5 times. A publish of an already-known version at the same commit causes no git work.

Event durability comes from the database backup (see [configuration](configuration.md#database-backup)). No event is written to git.

A failed pass reports the error on the Sync tab.

## Telemetry from the CLI

Hooks append each event (with a unique `id`) to a local spool. A detached flusher waits `telemetry_interval` seconds (default 15, between 5 and 3600) so a burst becomes one batch, then produces each event as one message to the Kafka topic (`kafka-python`, `acks=all`, message key = client id, value = the event JSON). A failed send keeps the spool; the CLI then starts no flusher for a back-off delay (30 s, doubling to 15 min, stored in `queue/backoff.json`), and `aihub flush` sends at once. Every send outcome is written to `queue/status.json`. Nothing is sent when nothing is spooled. Without a configured Kafka server the events stay spooled and usage reporting is paused.

`aihub dev publish` sends its publish event at once through the same path. If Kafka is unreachable, the event is spooled and retried with the same back-off.

The Kafka settings are resolved as: environment (`AIHUB_KAFKA_BOOTSTRAP`, `AIHUB_KAFKA_TOPIC`, `AIHUB_KAFKA_SECURITY_PROTOCOL`, `AIHUB_KAFKA_SASL_MECHANISM`, `AIHUB_KAFKA_USERNAME`, `AIHUB_KAFKA_PASSWORD`, `AIHUB_KAFKA_SSL_CAFILE`) > `config.json` (`kafka_bootstrap`, `kafka_topic`, ...) > the bundled `aihub/core/defaults.json` (`kafka`). The server uses environment > Admin > Sync > `defaults.json`. `/api/v1/client-config` returns the bootstrap, topic, security protocol and SASL mechanism, never the username or password, and `aihub setup` stores them in `config.json`. The CLI's producer connects anonymously by default, with no username, password or SASL login; those are added only when an operator sets them. Only the server's consumer holds Kafka credentials.

## Credentials for clients

`aihub setup` (and `aihub welcome`) fill every missing location silently from env, `config.json` and the bundled `defaults.json`. The only prompt is for git credentials, and only when `git ls-remote` is refused and a terminal is attached; credentials already in your git credential helper are enough. It asks the server for the index location and the Kafka settings (bootstrap, topic, security protocol, SASL mechanism). Only locations are sent, never credentials. Values are stored in `~/.aihub/config.json`. Git credentials are never shared: sign in to your git host as usual (or enter them when the CLI asks); they are kept by your own git credential helper. Kafka usernames and passwords are never sent to clients. The CLI refreshes the locations on the interval the admin sets. If the server is unreachable, install the CLI with pip from git and run `aihub setup --manual`, or export `AIHUB_INDEX_URL` and `AIHUB_KAFKA_BOOTSTRAP`.

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
