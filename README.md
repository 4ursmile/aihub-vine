# AI Hub

AI Hub is a registry and command-line client for distributing AI coding tool packages. Packages can contain skills, agents, MCP server definitions, executables, and setup profiles. The server stores package metadata and release archives in SQLite and local files.

## Architecture

```text
Package author
  aihub dev init / validate / build / publish
                  |
                  v
        AI Hub FastAPI server
        +-------------------+
        | REST API          |
        | SQLite (metadata) |
        | files/ (archives) |
        +-------------------+
                  ^
                  |
        aihub CLI (Python 3.9+)
          | install / update
          +--> Claude Code
          +--> Codex
          +--> OpenCode
```

## Quick start

Start a server from a Python 3.10+ environment:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-server.txt
python -m aihub.server --host 127.0.0.1 --port 8000 --data ./aihub-data --public-url http://localhost:8000
```

The first account registered on a new server becomes its admin. Install the bundled CLI zipapp from that server and add it to `PATH`:

```sh
curl -fsSL http://localhost:8000/install.sh | sh
export PATH="$HOME/.aihub/bin:$PATH"
aihub register
aihub login
```

Publish and install a sample package. `dev init` creates a manifest and README; publishing requires the account to have the `publish` permission.

```sh
aihub dev init ./hello-world --name hello-world --type tool
cd ./hello-world
aihub dev validate
aihub dev publish
aihub install hello-world
```

## Features

- Package search, version resolution, download, and SHA-256 verification.
- Immutable package versions, package maintainers, reviews, and ratings.
- CLI support for Claude Code, Codex, and OpenCode integrations.
- Manifest-declared commands, dependencies, Python environments, scripts, binaries, and setup steps.
- Usage events and package statistics.
- Account roles, permissions, approval-based registration, and admin controls.

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
- [REST API](docs/api.md)
