# AI Hub

AI Hub is a command-line client and web dashboard for distributing AI coding tool packages (skills, agents, MCP server definitions, executables, setup profiles). Packages live in **git**, usage is recorded in **Langfuse**, and the server is a dashboard and catalogue that syncs from both. It stores no package files. See [docs/architecture.md](docs/architecture.md).

## Architecture

```text
Package author                         aihub CLI (Python 3.9+, stdlib only)
  aihub dev init / publish               install / search / update / hooks
        |                                   |                |
        | git push                          | git clone      | OpenTelemetry spans
        v                                   v                v
   Git remote (GitLab, GitHub, ...)  <- package files + index.json      Langfuse
        ^                                                               |
        | read index                                                    | read events
        +------------------ AI Hub server (FastAPI) --------------------+
                            SQLite or PostgreSQL, web UI, dashboard
```

## Quick start

Start a server from a Python 3.10+ environment:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-server.txt
python -m aihub.server --host 127.0.0.1 --port 8000 --data ./aihub-data --public-url http://localhost:8000
```

By default the server speaks plain HTTP. To serve HTTPS directly, pass a certificate and key (PEM) and an `https://` public URL:

```sh
python -m aihub.server --host 0.0.0.0 --port 8443 --data ./aihub-data \
  --ssl-cert ./cert.pem --ssl-key ./key.pem --public-url https://localhost:8443
# local test certificate: openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj /CN=localhost -keyout key.pem -out cert.pem
```

`AIHUB_SSL_CERT` and `AIHUB_SSL_KEY` do the same from `.env`. Both must be set together. Behind a reverse proxy that terminates TLS, leave them out, run plain HTTP and set `--public-url` to the proxy's `https://` address.

The first account registered on a new server becomes its admin. Open Admin > Sync to enter your Langfuse keys and the git URL of the package index, and set how often the server pulls (seconds or a cron expression).

Install the CLI from that server (the installer adds it to your `PATH`), or straight from git if the server is not reachable:

```sh
curl -fsSL http://localhost:8000/install.sh | sh   # asks before adding ~/.aihub/bin to your PATH; open a new terminal after
pip install "aihub-cli @ git+https://your-git-host/team/aihub.git"      # alternative, needs only git and Python
aihub setup                     # pulls the index location and, if your admin shares them, the Langfuse keys
```

Without a server, export `LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and `AIHUB_INDEX_URL`, or run `aihub setup --manual`.

Create and publish a sample package. `dev init` creates a manifest and README and fills in the git location from your repo; `dev publish` commits and pushes it.

```sh
aihub dev init ./hello-world --name hello-world --type tool
cd ./hello-world
aihub dev validate
aihub dev publish
aihub install hello-world       # once the index lists it
```

To have Claude Code or Codex package a new or existing project for you, install the `aihub-package` skill with `aihub install aihub-package` and ask "package this project for AI Hub".

## Backends

Choose SQLite or PostgreSQL for metadata and memory or Redis for cache in `.env`. See [Server configuration](docs/configuration.md) for deployment settings, [Architecture](docs/architecture.md) for how git, Langfuse and the server fit together, and [Publishing to AI Hub](docs/publishing.md) for package authoring.

## Features

- Package search and version resolution from a git-hosted index; installs pinned to a commit.
- Usage events in Langfuse (OpenTelemetry), synced into a dashboard with filters, a heatmap and an activity log.
- Package maintainers, reviews, and ratings.
- CLI support for Claude Code, Codex, and OpenCode integrations.
- Manifest-declared commands, dependencies, Python environments, scripts, binaries, and setup steps.
- Usage events and package statistics.
- Account roles, permissions, approval-based registration, and admin controls, including password reset (`reset_password` permission) and a searchable security audit of tool calls and admin actions (`audit` permission). See [CLI usage hooks](docs/cli.md#usage-hooks-and-telemetry) for exactly what is recorded and scrubbed.

## Repository layout

```text
aihub/server/   FastAPI application, API routes, SQLite, storage, security
aihub/cli/      CLI, installer, tool integrations, usage hooks
aihub/core/     Manifest, package naming/versioning, archives, zipapp
aihub/server/static/  Web interface
requirements-server.txt  Server dependencies
Dockerfile, docker-compose.yml  Container deployment
 tests/         unittest suite
 docs/          Setup, CLI, manifest, and API guides
```

## Requirements and tests

- Server: Python 3.10+; FastAPI and Uvicorn are installed from `requirements-server.txt`.
- CLI: Python 3.9+; the CLI zipapp uses the standard library only.
- Run tests from the repository root:

```sh
.venv/bin/python -m unittest discover -s tests -t .
```

## Documentation

- [Production server setup](docs/server-setup.md)
- [CLI guide](docs/cli.md)
- [Manifest reference](docs/manifest.md)
- [Publishing to AI Hub](docs/publishing.md)
- [Server configuration](docs/configuration.md)
- [REST API](docs/api.md)
