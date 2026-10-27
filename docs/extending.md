# Building on aihub

This page is for developers who build **on top of** aihub: a local web UI, a desktop app, an IDE panel, a script or an AI agent that manages packages, logins and bots for the user. It describes what aihub exposes, the contract each surface keeps, and the rules that keep such a tool safe.

| Surface | Use it for | Contract |
| --- | --- | --- |
| **CLI JSON output** (`aihub --json CMD`) | Everything on the user's machine: search, install, uninstall, update, vault, login, bots, knowledge, config | One JSON document per command ([below](#the-json-contract)) |
| **Hub HTTP API** (`/api/v1`) | Data on the hub: packages, stats, usage, groups, running bots of a team | [api](api.md), live schema at `/docs` and `/openapi.json` on the hub |
| **Packages** (`aihub.toml`) | Shipping your tool to other people: a skill, agent, MCP server, tool, setup or bot | [manifest](manifest.md), [publishing](publishing.md), [bots](bots.md) |
| **`aihub run`** | Keeping a long-lived program (your UI server, a bot) running in the background, with logs, restarts and a schedule | [cli](cli.md) "Bots and knowledge" |

A local tool should use the **CLI**, not reimplement it: the CLI already knows the hub, the git hosts, the stored logins, the install locations of every AI tool and the usage hooks. Calling it keeps your tool and the terminal in the same state.

## The JSON contract

Any command prints one JSON document on stdout when JSON output is asked for in any of these ways:

```sh
aihub --json list                 # global flag, before the command
aihub list --json                 # the command's own flag (every command has it)
AIHUB_JSON=1 aihub list           # environment, for a whole process tree
```

Without one of them nothing changes: human output, `--plain`, `--name-only`, `aihub info` piped to a file and `aihub usage --json` keep the exact shapes they had. (`aihub usage --json` is the one command whose own flag keeps its older shape; `aihub --json usage` wraps it like every other command.)

### The document

```json
{
  "schema": 1,
  "ok": true,
  "command": "install",
  "exit": 0,
  "error": null,
  "hint": null,
  "data": {"failed": []},
  "output": null,
  "messages": [{"level": "good", "text": "my-skill 1.2.0 fetched"}],
  "changes": {"installed": [{"name": "my-skill", "version": "1.2.0"}]},
  "context": {"hub": "https://hub.example.com", "hub_configured": true, "signed_in": true, "username": "sam",
              "cli_version": "0.8.3", "home": "/Users/sam/.aihub", "client_id": "25e0..."}
}
```

| Field | Meaning |
| --- | --- |
| `schema` | Version of this contract. New fields may be added within a version; a field is never removed or changes meaning without a new number. |
| `ok`, `exit` | Success, and the process exit code (also the real exit code). 0 = ok, 1 = failed, 2 = bad command line, 130 = interrupted. |
| `command` | The command and its sub-action (`vault list`, `run stop`, `config set`). `null` when the command line could not be parsed. |
| `error`, `hint` | One-line error and, when known, what to do (`run 'aihub login' first`). |
| `data` | The structured answer (table below). `null` for commands that only report what they did. |
| `output` | Text the command printed that is not JSON (for example `aihub knowledge get NOTE` prints the note). `null` when empty. |
| `messages` | What a person would have seen, in order: `{level, text}` with level `step`, `good`, `bad`, `note`, `info`, `heading`, `done` or `text` (table rows, cards). Plain text, no colour codes. Show them in a log or as toasts. |
| `changes` | Installed packages before vs after the command: `installed`, `removed`, `updated` (`from`, `to`), `enabled`, `disabled`. Keys appear only when something changed. Refresh your package list when it is not empty. |
| `context` | Who and where, after the command: hub URL, whether a hub was chosen, sign-in state and user name, CLI version, aihub home, device id. |

### Data per command

| Command | `data` |
| --- | --- |
| `search [TERM]` | Every matching index entry (`name`, `type`, `latest_version`, `description`, `tags`, ...). No 30-row cap. |
| `info NAME` | The index entry plus `versions`. |
| `list` | Installed packages: `name`, `version`, `enabled`, `components`, `path`, and where present `type`, `bot_home`, `venv`, `rev`, `repo`, `source`, `tools`, `shims`. |
| `install ...` | Several packages: `{"failed": [{name, error}]}`. What was installed is always in `changes`; a single package that fails gives `ok: false` and its `error`. |
| `uninstall`, `update`, `enable`, `disable`, `sync` | See `changes`. |
| `config [show]`, `config set K V` | The configuration, secrets masked. |
| `login`, `logout` | `{signed_in, username, hub}` / `{signed_in: false, signed_out}`. |
| `register`, `password` | `{created, username}` / `{changed, username}`. |
| `version` | `{version, pip, auto_update, checked, last_error, skip_version, hub_version, update_available}`. |
| `vault [list]` | `{backend, remotes: [{host, scheme, username, login: stored or missing}]}`. |
| `vault add/edit/remove` | `{host, username, login}` / `{host, removed}`. |
| `vault test [REMOTE]` | `[{host, url, username, login, reachable}]`. |
| `run list`, `run status NAME` | The runs (`id`, `bot`, `args`, `schedule`, `restart`, `status`, `pid`, `runs`, `last_exit`, `state`, `alive`, ...). |
| `lock` | `{file, lock}`: the lock file path and its content. |
| Commands that print JSON themselves (`dev stats`, `usage`) | Their JSON, parsed. |
| Anything else | `null`; read `messages` and `changes`. |

Adding `data` to more commands is additive (schema stays 1); a UI should treat a `null` `data` as "nothing structured yet", not as an error.

### Rules JSON mode follows

- **stdout holds the document and nothing else.** Programs the command starts (install scripts, git, pip) write to stderr. Read stderr only for diagnostics.
- **No prompts.** JSON mode is non-interactive. A command that would ask a question takes its safe default, which is usually *do nothing* (for example `self-uninstall` refuses). Pass `--yes` when the user really confirmed in your UI. JSON mode never adds `--yes` for you.
- **Secrets come in on stdin, never on the command line** (argv is visible to every process on the machine):

  ```sh
  printf '%s\n' "$PASSWORD" | aihub login sam --password-stdin --json
  printf '%s\n' "$PASSWORD" | aihub register sam --password-stdin --json
  printf '%s\n%s\n' "$OLD" "$NEW" | aihub password --password-stdin --json
  printf '%s' "$TOKEN"       | aihub vault add https://git.example.com --user sam --token-stdin --json
  ```

- **Secrets never come out.** Tokens and passwords are never in a document: config values under keys that look secret (`token`, `secret`, `password`, `credential`, `api_key`) read `"set"`, the vault reports `login: "stored"`, login replies carry the user name, and as a last safety net the saved hub token is replaced by `***` anywhere in the document.
- **A bad command line is still a document** (`ok: false`, `exit: 2`, the argparse message in `error`). `--help` prints help text, not JSON.

## One login for the CLI and every tool on top of it

A UI that calls the CLI **shares its state**; it must not keep its own copy of anything:

| State | Lives in | Shared how |
| --- | --- | --- |
| Hub sign-in | `~/.aihub/credentials.json` (0600) | `aihub login` in your UI signs the terminal in too, and `aihub logout` in the terminal signs your UI out. Read `context.signed_in` / `context.username` from any reply. |
| Git logins | The OS credential store (Keychain, DPAPI, Secret Service, else a 0600 file); host list in `~/.aihub/vault.json` | `aihub vault ...`. git itself uses the same entries through aihub's credential helper. |
| Hub URL and settings | `~/.aihub/config.json` | `aihub config [set]`. |
| Installed packages | `~/.aihub/state.json` and `~/.aihub/packages/` | `aihub list`, plus `changes` in every reply. |
| Bots | `~/.aihub/runs/` | `aihub run ...`. |

There is deliberately **no command that prints the hub token**. A UI that needs hub data asks the CLI, or, for a page the hub already serves, links to the hub's own web UI, where the user signs in with the same account. If several programs use one machine account, set `AIHUB_HOME` the same way for all of them, or they see different states.

## Calling the CLI from your code

Python (stdlib):

```python
import json, subprocess

def aihub(*args, stdin=None):
    p = subprocess.run(["aihub", "--json", *args], input=stdin, capture_output=True, text=True)
    doc = json.loads(p.stdout)                 # always one document, also on failure
    if not doc["ok"]:
        raise RuntimeError(doc["error"] + (" (" + doc["hint"] + ")" if doc["hint"] else ""))
    return doc

pkgs = aihub("search", "sql")["data"]
aihub("install", "sql-helper", "--yes")
aihub("login", "sam", "--password-stdin", stdin=password + "\n")
```

Node:

```js
import { execFile } from "node:child_process";
const aihub = (args, input) => new Promise((ok, bad) => {
  const p = execFile("aihub", ["--json", ...args], (err, stdout) => {
    try { const d = JSON.parse(stdout); d.ok ? ok(d) : bad(new Error(d.error)); } catch (e) { bad(err || e); }
  });
  if (input != null) { p.stdin.end(input); } else { p.stdin.end(); }
});
```

Always pass arguments as a list (`execFile`, `subprocess.run([...])`), never through a shell: package names, hosts and notes come from users and from the hub.

## A local web UI as a package (for example `aihub-ui`)

Nothing in the CLI needs to change to ship one:

1. Make it a **bot** package so `aihub run` manages it: `[bot] run = "ui/server.py"`, plus `[[bot.env]]` entries for its port. `aihub run aihub-ui` then starts it in the background with a log and restart on failure, `aihub run stop aihub-ui` stops it, and when the user is signed in it shows on the hub's Running page. (A `type = "tool"` package with `[bin] aihub-ui = "ui/server.py"` works too, but then you manage the process yourself.)
2. The server calls `aihub --json ...` for every action (see above) and renders `data`, `messages` and `changes`.
3. **Lock it down.** It controls installs and the user's git logins:
   - listen on `127.0.0.1` only, never `0.0.0.0`;
   - generate a random token at start, open the browser with it, and require it on every request (a cookie or header, not only in the URL);
   - reject requests whose `Origin`/`Host` is not your own address (DNS-rebinding and cross-site requests);
   - keep secrets in memory only for the one command that needs them, and send them on stdin.
4. Use the stdlib where you can (`http.server`, `json`, `subprocess`); declare anything else in `[python] requires` so `aihub install` builds its environment.

### A terminal inside the web UI

A built-in terminal works well with aihub, if it is a **real terminal**:

- Run the shell or `aihub` in a **pseudo-terminal**: Python's `pty` module on macOS/Linux (stdlib), ConPTY on Windows (for example the `pywinpty` package). Stream it over a WebSocket to **xterm.js** in the page. Pass the browser's size (rows, columns) to the PTY on resize. Set `TERM=xterm-256color`.
- With a PTY the CLI behaves exactly as in a normal terminal: colours, spinners, arrow-key menus, hidden password prompts, `aihub usage` (full screen, mouse) and chat bots (`aihub run BOT --fg`) all work. With plain pipes instead of a PTY, the CLI switches to its non-interactive mode: no prompts, no colour, and menus pick defaults. Use pipes for `--json` calls, a PTY for the terminal.
- The terminal shares the same `~/.aihub` as the rest of your UI, so a `aihub login` typed there signs the UI in too.
- **It is a remote shell.** Everything in "Lock it down" above applies, twice over: localhost only, the per-start token, an origin check on the WebSocket upgrade, and no "share this terminal" feature. The shell runs as the user, with the user's files and logins.

## Building on the hub

- The REST API is in [api](api.md); the hub also serves its live OpenAPI schema at `/openapi.json` and an explorer at `/docs`. Sign in with `POST /api/v1/auth/login` (a token for your own service account) and send `Authorization: Bearer <token>`.
- Permissions are per role (Admin > Roles). Ask for the smallest role your tool needs; an admin token can change accounts and settings.
- Hubs can run under a path prefix (`https://example.com/aihub/`): build URLs from the hub URL the user gave you, never assume `/api/v1` sits at the root of the host.
- Read-heavy tools: list routes are cached on the hub, and `/index/catalog` answers `304` to `If-None-Match`. Poll gently (seconds, not milliseconds).
- Bots started with `aihub run` report to `POST /api/v1/runs/report`; a team dashboard can read `GET /api/v1/runs?scope=all` with an admin token. The hub never connects to a machine: a command waits until the run's next report.

## Keeping the contract when you change aihub

For contributors to aihub itself:

- Print through `ui.*` (`ui.good`, `ui.note`, `ui.table`, ...), never `print()`, unless it is the command's own data output. In JSON mode the `ui` calls become `messages`.
- Give a command structured output with `ui.result(data)`, before printing for people. It does nothing in human mode, so the human output stays as it is.
- Never pass a secret to `ui.*` or `ui.result`; pass values through `main.masked(...)` when they come from a settings file.
- New prompts must check `ui.INTERACTIVE` (false in JSON mode) and offer a non-interactive path: a flag, or a `--*-stdin` for secrets.
- Add a case to `tests/test_json_output.py` for any new `data` shape. Every test there checks that the document parses, has every field, and contains no secret.
