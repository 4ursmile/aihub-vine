# Building chat bots

A **bot** is a package with `type = "bot"`. `aihub install NAME` puts its files in place and creates a settings file; `aihub run NAME` starts it. A bot is the fourth kind of assistant next to skills, agents and MCP servers: it can use all of them, plus its own memory, notes, tools and scheduled jobs.

## What a bot is

| Part | What it is | Where it lives |
| --- | --- | --- |
| Program | Any program started by `[bot] run` (Python, shell, or a command) | Package folder, `~/.aihub/packages/NAME` |
| Settings | Values from `[[bot.env]]`, written once to `.env` | `~/.aihub/bots/NAME/.env` (0600) |
| Soul and notes | The user's personal markdown notes (`soul` = persona) | `~/.aihub/knowledge/` and the hub |
| Bot memory | Facts and conversation the bot keeps for itself | `~/.aihub/bots/NAME/memory/` |
| Skills | Skills of every installed package, loaded on demand | Each package's own folder |
| Extensions | Your tools, prompts, cron jobs and harnesses | `~/.aihub/bots/NAME/ext/*.py` |

The reference bot is `aihub-bot` (built in). Its layout is in [Layout recommendation](#layout-recommendation).

## Quick start

```sh
aihub install aihub-bot                   # prints: source, settings (.env), run it
# edit ~/.aihub/bots/aihub-bot/.env       # set ANTHROPIC_API_KEY (or pick another BOT_HARNESS)
aihub knowledge edit soul                 # who the assistant is for you
aihub run aihub-bot --fg                  # chat in this terminal; add -- --once "question" for one answer
aihub run aihub-bot -- --cron             # scheduled prompts, in the background
aihub run list                            # what runs; aihub run logs aihub-bot -f to watch it
```

`aihub run` starts a bot in the background under a supervisor (log in `~/.aihub/runs/NAME.log`, restart on failure, optional `--schedule`); `--fg` keeps it in the terminal, which an interactive chat needs. With no name it starts the only installed bot; with several, it lists them and the runs. Signed in to a hub, runs appear on the hub's **Running** page, where they can be stopped, started, restarted and changed. Full command list: `docs/cli.md` (Bots and knowledge).

## Build your own bot

1. Create the project (a folder with `aihub.toml`, `bot/run.py`, `README.md`, `.gitignore`):

   ```sh
   aihub dev init ./my-notes-bot --type bot --name my-notes-bot --description "Answers from my notes"
   cd my-notes-bot
   ```

2. Edit `aihub.toml`: set `[bot] title`, and replace the `[[bot.env]]` example with the settings your bot needs (see [Manifest reference](#manifest-reference)).
3. Write `bot/run.py`. It receives settings as environment variables and its arguments after `--`.
4. Optional: `[python] requires = [...]` (a venv is built at install and the bot runs with it), and `[scripts] install_macos` / `install_linux` / `install_windows` for setup, as in any package.
5. Check and publish:

   ```sh
   aihub dev validate                    # the run file must exist inside the package
   aihub dev publish --bump patch        # first release: no bump needed
   ```

6. Install and run it like any user:

   ```sh
   aihub install my-notes-bot            # prints source:, settings:, run it:
   # edit ~/.aihub/bots/my-notes-bot/.env
   aihub run my-notes-bot
   ```

Minimal complete example.

`aihub.toml`:

```toml
[package]
name = "my-notes-bot"
version = "0.1.0"
type = "bot"
description = "Answers questions from my knowledge notes"
tags = ["bot", "notes"]
readme = "README.md"
license = "MIT"

[bot]
run = "bot/run.py"
title = "My notes bot"

[[bot.env]]
name = "MY_API_KEY"
hint = "API key for the service the bot calls"
required = true
secret = true
```

`bot/run.py`:

```python
#!/usr/bin/env python3
"""Answers from the notes folder. Started by `aihub run my-notes-bot`."""
import os
import sys

KNOWLEDGE = os.environ["AIHUB_KNOWLEDGE_DIR"]    # set by aihub run
API_KEY = os.environ["MY_API_KEY"]               # from ~/.aihub/bots/my-notes-bot/.env


def notes():
    return sorted(f[:-3] for f in os.listdir(KNOWLEDGE) if f.endswith(".md"))


def main(argv):
    if "--info" in argv:
        print("notes:", ", ".join(notes()) or "none", "| key set:", bool(API_KEY))
        return 0
    question = " ".join(argv[argv.index("--once") + 1:]) if "--once" in argv else input("you> ")
    # Replace this with a call to your model or service (use API_KEY).
    print("question:", question, "| notes available:", len(notes()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

`README.md`: one install line (`aihub install my-notes-bot`), what it does, a usage line, and an uninstall line (`aihub uninstall my-notes-bot`).

## Guided setup (`onboard`, optional)

A bot can ship an interactive script that creates its credentials and fills `.env`: `[bot] onboard = "bot/onboard.py"` (same script kinds as `run`). It is always optional: `aihub install` offers it in a terminal (`Set up ... now? [Y/n]`; with `--yes` or without a terminal it only prints `aihub onboard NAME`), and `aihub onboard NAME` runs it any time. Editing `.env` by hand keeps working. The script gets the same `AIHUB_BOT_*` environment as the bot, so it should write to `$AIHUB_BOT_ENV`, keep other lines, `chmod 600`, and never overwrite working values without asking. `aihub-teams-bot/bot/onboard.py` is the reference.

## Manifest reference

`[package]` is the same as any package with `type = "bot"`. The bot-specific tables:

| Key | Required | Meaning |
| --- | --- | --- |
| `[bot] run` | yes | Path inside the package that `aihub run` starts. `.py`, `.sh`, `.ps1`, `.cmd`, `.bat` run with the matching interpreter; anything else is a shell command. `aihub dev validate` checks that a file path exists. |
| `[bot] title` | no | Display name for the bot. Defaults to empty; the CLI does not print it. |
| `[[bot.env]] name` | yes (per entry) | Environment variable name, `[A-Za-z_][A-Za-z0-9_]*`. Entries with an invalid name are dropped. |
| `[[bot.env]] hint` | no | Text printed next to the name in `.env` and when `aihub run` reports a missing value. |
| `[[bot.env]] required` | no | Default `true`. A required value that is empty stops `aihub run`. |
| `[[bot.env]] secret` | no | Marks the value as a secret. Nothing else changes: `.env` is always 0600. |
| `[[bot.env]] default` | no | Written into `.env` at first install. The manifest is public, so never put a real secret here. |
| `[[mcp_servers]]` | no | MCP servers for this bot only. Written to `~/.aihub/bots/NAME/mcp.json`. Not registered into Claude Code, Codex or OpenCode. `${PKG}` in `args` becomes the package folder. |
| `[python]`, `[scripts]`, `[requires]` | no | Same as any package. The venv is `~/.aihub/venvs/NAME`. |

```toml
[bot]
run = "bot/run.py"
title = "My bot"

[[bot.env]]
name = "SLACK_TOKEN"
hint = "Slack bot token (https://api.slack.com/apps)"
secret = true

[[bot.env]]
name = "BOT_MODEL"
hint = "model id"
required = false
default = "claude-sonnet-5-5"

[[mcp_servers]]
name = "files"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "."]
```

Install behavior for `[[bot.env]]`: `.env` is created once. It holds one `NAME=value` line per entry, preceded by its hint. Later installs and updates never overwrite it, so a new `[[bot.env]]` entry added in a later version is **not** added to an existing `.env`; add it by hand.

## Services (`type = "service"`)

A **service** is a long-running program (web server, worker, sync daemon) started the same way: `aihub install NAME`, then `aihub run NAME`. It uses the bot machinery with three differences:

- the table is `[service]` (same keys as `[bot]`: `run`, `title`, `onboard`, `[[service.env]]`) plus two run defaults: `restart = "no" | "on-failure" | "always"` (default **always**; an invalid value is ignored) and `schedule = "5m"` (seconds, 30s/5m/2h/1d or cron; empty = keep it up). `[bot]` takes the same two keys (bot default stays on-failure). Flags (`--restart`, `--schedule`, `--fg`) always win;
- settings live in `~/.aihub/services/NAME/.env` (+ `mcp.json`); the component is `service:NAME`;
- the process gets `AIHUB_SERVICE`, `AIHUB_SERVICE_DIR`, `AIHUB_SERVICE_HOME`, `AIHUB_SERVICE_ENV` in addition to the usual `AIHUB_BOT_*` set.

```sh
aihub dev init ./my-svc --type service --name my-svc    # starter: service/run.py, restart = "always", a PORT setting
```

```toml
[service]
run = "service/run.py"
restart = "always"

[[service.env]]
name = "PORT"
default = "8080"
required = false
```

## Where things live

| What | Where |
| --- | --- |
| Package source (printed after install as `source:`) | `~/.aihub/packages/NAME` |
| Settings and credentials (printed as `settings:`) | `~/.aihub/bots/NAME/.env` (0600, created once, never overwritten) |
| The bot's MCP servers | `~/.aihub/bots/NAME/mcp.json` (rewritten on each install) |
| Your extensions and cron | `~/.aihub/bots/NAME/ext/*.py`, `~/.aihub/bots/NAME/cron.json` |
| Bot memory | `~/.aihub/bots/NAME/memory/` (`facts.jsonl`, `log.jsonl`, `summary.md`) |
| Your knowledge notes (soul included) | `~/.aihub/knowledge/*.md` (`aihub knowledge path`) |
| Python venv (only with `[python]`) | `~/.aihub/venvs/NAME` |
| Command harness system prompt | `~/.aihub/bots/NAME/.system.md` (written before each `command` harness call) |

All of these sit under `AIHUB_HOME` when it is set instead of `~/.aihub`. Your files in the bot home are never removed by updates.

## Environment the bot receives

`aihub run` starts the bot with the current environment, then sets:

| Variable | Value |
| --- | --- |
| (every `.env` entry) | Values from `.env`. Non-empty values override shell variables of the same name. |
| `AIHUB_BOT` | Bot package name |
| `AIHUB_BOT_DIR` | Package source folder |
| `AIHUB_BOT_HOME` | Bot home (`~/.aihub/bots/NAME`) |
| `AIHUB_BOT_ENV` | Path of `.env` |
| `AIHUB_BOT_MCP` | Path of `mcp.json` |
| `AIHUB_KNOWLEDGE_DIR` | Knowledge folder (`~/.aihub/knowledge`) |
| `AIHUB_HUB` | Hub URL from the CLI config |
| `AIHUB_SKILL_DIRS` | Skill folders of every enabled installed package (`os.pathsep` separated) |
| `AIHUB_PACKAGE_DIRS` | Folders of every enabled installed package except the bot itself |
| `VIRTUAL_ENV` | The package venv, when `[python]` is set |

- Working directory: the bot home.
- Arguments after `--` on the command line are passed to the program: `aihub run NAME -- --once "question"`.
- Required check: a required value counts as set when it is non-empty in `.env` or in the shell environment.

## Memory

Three layers, each with its own owner:

| Layer | Who writes it | Where | Used by |
| --- | --- | --- | --- |
| (a) Personal knowledge | The user: `aihub knowledge edit|push|pull`, or the hub page **Knowledge** | `~/.aihub/knowledge/*.md` and the hub | Bot reads it (`soul.md` is the persona, loaded first; other notes are inlined up to 6000 characters, the rest is read with `read_note`). The bot may only **save** notes with `save_note`; `soul` is read-only for the bot. |
| (b) The bot's own memory | The bot, through tools `remember`, `recall`, `forget`, and automatic compaction | `~/.aihub/bots/NAME/memory/` | Up to 500 facts; 15 relevant facts go into the prompt. The last 12 turns are the history. |
| (c) Skills of installed packages | Each package | Its own folder | Listed in the prompt by name and description; the body is loaded with `read_skill`. |

Bot memory details:

| Item | Behavior |
| --- | --- |
| `facts.jsonl` | One fact per line, max 500 chars each. Duplicates (case-insensitive) are ignored. |
| `log.jsonl` | Conversation turns. When it passes 40 turns, all but the last 10 are folded into `summary.md` (about 12 bullets, capped at 6000 chars) by a model call. |
| `summary.md` | Rolling summary of older conversations, put in the prompt. |
| `aihub run NAME -- --memory` | Prints facts, recent turns and the summary. Files are plain text: you can edit them by hand. |

The `REMEMBER:` convention: a harness without memory tools is told to end its reply with `REMEMBER: <one short fact>`. The line is stored as a fact and removed from the answer. Every harness honors it, so a custom harness only has to return the text.

## Execute layer: choosing a harness

`BOT_HARNESS` in `.env` picks what turns a prompt into an answer. Memory, notes, skills and extensions are the same for all of them.

| `BOT_HARNESS` | Runs | Needs | Extra settings |
| --- | --- | --- | --- |
| `builtin` (default) | The bundled tool loop, straight on the Anthropic Messages API (`harness/llm.py`) | `ANTHROPIC_API_KEY` | `BOT_MODEL`, `BOT_MAX_TURNS` (default 12) |
| `claude` | `claude -p <prompt> --append-system-prompt <system>` | `claude` on PATH, its own login | `BOT_CLAUDE_ARGS`; uses `mcp.json` when it has servers |
| `codex` | `codex exec <system + prompt>` | `codex` on PATH, its own login | `BOT_CODEX_ARGS` |
| `command` | Your program: prompt on stdin, system prompt in the file `$BOT_SYSTEM_FILE`, answer on stdout | `BOT_HARNESS_CMD`, e.g. `python my_agent.py` | `BOT_HARNESS_CMD` |
| a name registered by an extension | `bot.executor("name", fn)`, `fn(text, system) -> str` | The extension | Set `BOT_HARNESS=name` |

Shared settings: `BOT_TIMEOUT` (seconds for CLI and command harnesses, default 600).

Stateless harnesses (`claude`, `codex`, `command`, custom) get the last 12 turns as "Conversation so far" inside the prompt. `builtin` sends them as real messages.

Add your own harness in an extension file (`~/.aihub/bots/NAME/ext/myharness.py`):

```python
def register(bot):
    def answer(text, system):
        return "echo: " + text          # call your agent here; return a string
    bot.executor("echo", answer)
```

Then set `BOT_HARNESS=echo` in `.env`. Use `command` instead when the program is not Python code you want to load into the bot.

## Extensions and cron

Every `*.py` in `<package>/ext/` and `~/.aihub/bots/NAME/ext/` (not starting with `_`) is loaded and its `register(bot)` is called. A file that fails is reported and skipped; the bot still starts.

| Call | Adds |
| --- | --- |
| `@bot.tool(name, description, schema=None)` | A tool the model can call. `fn(args) -> str`. |
| `bot.prompt(text)` | Extra text in the system prompt. |
| `bot.cron(name, when, prompt)` | A scheduled prompt. |
| `bot.executor(name, fn)` | A harness, selected with `BOT_HARNESS`. |

Example: `aihub-bot` ships `ext_examples/clock.py`. Copy it to `ext/` to enable it.

Scheduled prompts come from `cron.json` (in the bot home) and from `bot.cron`:

```json
[{"name": "morning", "when": "08:30", "prompt": "Summarize my notes for today"},
 {"name": "review", "when": "every 2h", "prompt": "Check my open tasks"}]
```

| `when` | Fires |
| --- | --- |
| `"HH:MM"` | Once a day, local time. If the process starts after that time, it fires at once that day |
| `"every 30m"` or `"every 2h"` | Repeatedly, the first run one interval after start |

Run them with `aihub run NAME -- --cron`. The process stays up and checks the jobs about every 20 seconds. A failed job prints its error and the loop continues. Without any job it exits with a message.

## Credentials checklist

- Put every key in `~/.aihub/bots/NAME/.env` (or in the shell for a one-off run), never in `aihub.toml`, source files, `cron.json` or the README.
- `[[bot.env]] default` is published with the package. Never use a real secret as a default.
- `.env` is created with mode 0600. Keep it that way; do not copy it into the package folder.
- A package folder that keeps a local `.env` must ignore it. The scaffold `.gitignore` does not list `.env`, so add it: `.env`.
- Use the vendor's login where one exists: `claude` and `codex` harnesses use their own login, so they need no key in `.env`.
- Revoke and replace a key that was ever pasted into a shared file or published package.

## Layout recommendation

Copy the layout of `aihub-bot` (`aihub install aihub-bot`, then read its folder). Modules, all standard library:

| File | Does |
| --- | --- |
| `bot/run.py` | Entry point: adds `harness/` to the path and calls `harness.app.main` |
| `bot/harness/config.py` | Reads the `AIHUB_*` and `BOT_*` environment (paths, model, harness, turn limit) |
| `bot/harness/app.py` | Command line: chat loop, `--once`, `--cron`, `--info`, `--memory` |
| `bot/harness/agent.py` | Builds the system prompt; the builtin tool loop; built-in tools (notes, memory, skills) |
| `bot/harness/executors.py` | Execute layer: `builtin`, `claude`, `codex`, `command`, extension harnesses; memory around each call |
| `bot/harness/memory.py` | Soul and knowledge notes (user's) and `Store` (bot's own facts, log, summary) |
| `bot/harness/skills.py` | Lists skills from `AIHUB_SKILL_DIRS`; loads one on demand |
| `bot/harness/mcp.py` | Minimal stdio MCP client; starts the servers in `mcp.json` |
| `bot/harness/llm.py` | Model API call for the builtin harness; replace it to use another provider |
| `bot/harness/cron.py` | Parses `when`, runs the scheduled prompts |
| `bot/harness/ext.py` | Loads extensions; `Bot` object with `tool`, `prompt`, `cron`, `executor` |
| `bot/ext_examples/clock.py` | Example extension (one tool, one prompt line, one cron line in a comment) |

Keep the bot in `bot/` with `run.py` as the only entry point; put the rest in `bot/harness/` so `aihub.toml` stays small.

## Testing a bot

Run the bot's own output checks first, then unit tests:

```sh
aihub run my-notes-bot -- --info        # what the bot sees: home, settings, knowledge, memory, tools
aihub run my-notes-bot -- --memory      # what it remembers
aihub run my-notes-bot -- --once "hello"
```

Unit tests for the bot's modules (standard library `unittest`, no pytest). Set the environment before importing, because `config` reads it at import time:

```python
# tests/test_memory.py
import os
import sys
import tempfile
import unittest

os.environ["AIHUB_BOT_HOME"] = tempfile.mkdtemp()     # before importing harness
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
from harness import memory                            # noqa: E402


class MemoryTest(unittest.TestCase):
    def test_remember_once(self):
        store = memory.Store(os.path.join(os.environ["AIHUB_BOT_HOME"], "memory"))
        self.assertEqual(store.remember("likes tea"), "remembered")
        self.assertEqual(store.remember("likes tea"), "already known")


if __name__ == "__main__":
    unittest.main()
```

Run with `python3 -m unittest discover -s tests`. Test the run path end to end with `aihub run NAME -- --once "..."` after `aihub install`, once the `.env` values are set.
