# Customising aihub-teams-bot

Rule of thumb: change files in the **bot home** (`~/.aihub/bots/aihub-teams-bot/`), they survive updates. Fork the package only when you
need to ship a new bot to others (last section).

## 1. Choose the job (no code)

| Want | Do |
| --- | --- |
| A ready task | `.env`: `TEAMS_TASK=helpdesk` (also `standup`, `meeting-notes`, `general`) |
| Your own wording | create `task.md` in the bot home: the bot's job, rules, tone. Edited live, no restart |
| Who it is | `aihub knowledge edit soul` (persona, shared with every bot) |
| What it knows | `aihub knowledge edit faq`, `runbook`, ... (notes are read as context) |
| Which brain | `.env`: `BOT_HARNESS=builtin|claude|codex|command` (see aihub-bot docs) |
| Who may talk to it | `.env`: `TEAMS_ALLOWED=ada@x.org,Bob` |

A good `task.md` has: the role in one line, 4-8 rules, the output format, and what to do when unsure.

## 2. Add abilities (a small Python file)

Put `*.py` files with `register(bot)` into `ext/` in the bot home. `bot/ext_examples/helpdesk.py` is a complete example.

```python
def register(bot):
    @bot.tool("lookup_order", "Find an order by id", {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]})
    def lookup(args): return my_api(args["id"])            # the model calls it (builtin harness)

    @bot.command("order", "show an order: /order 123")      # users type it; no model involved
    def order(ctx): return my_api(ctx["args"])

    bot.prompt("Always give order ids in `code` format.")   # extra instructions
    bot.cron("digest", "09:00", "Summarise open tickets")   # scheduled prompt (see aihub-bot)
```

`ctx` in a command: `args`, `sender` (`id`, `name`, `aad_id`), `conv` (chat id), `memory` (this chat's store), `agent`.
Secrets for your own APIs: add `NAME=value` lines to `.env` and read them with `os.environ`.
Another MCP server for this bot only: edit its entry in the package manifest (`[[mcp_servers]]`) or the bot home `mcp.json`.

## 3. Where behaviour lives (what to read)

| File (package `bot/teamsbot/`) | Role |
| --- | --- |
| `core.py` | no SDK: `clean`, `allowed`, `task_text`, slash commands, `Hub.handle` (the whole behaviour, unit-testable) |
| `app.py` | Teams SDK glue: receives a message, shows typing, calls `Hub.handle`, splits long replies |
| `base.py` | finds the installed aihub-bot harness |
| `../tasks/*.md` | built-in task prompts |

To test a change without Teams: `aihub run aihub-teams-bot -- --try "your message"`.

## 4. Make your own team bot (derive and publish)

```sh
cp -r <source folder printed by aihub install> my-team-bot      # or: aihub dev init --type bot my-team-bot, then copy bot/
# aihub.toml: change [package] name/description, keep  requires.packages = ["aihub-bot>=1.1.0"]  and  python.requires
# add your tasks to bot/tasks/NAME.md, your tools to ext/ (ship them in the package: <package>/ext/*.py is loaded too)
aihub dev validate && aihub dev publish
```

Keep `teamsbot/` as is so you can take upstream fixes; put your changes in `tasks/`, `ext/` and `[[bot.env]]`.

## 5. Going live checklist

1. Optional: `aihub onboard aihub-teams-bot` signs you in once (Teams CLI popup), registers the bot and fills `.env`; the running bot then needs no sign-in, only the client secret (which expires: renew it before then). Or register by hand in the Teams Developer Portal and note client id, secret, tenant.
2. Fill `.env`; run `-- --info`: credentials must say `set`.
3. Expose port 3978 over https and set the messaging endpoint to `https://HOST/api/messages`.
4. Set `TEAMS_ALLOWED` if the bot reaches a whole tenant. Never set `TEAMS_LOCAL=1` in production.
5. Keep the process running (service, container). `/reset` clears one chat; `/memory` shows what it remembers there.

## 6. Background tasks (`/task`)

`/task summarize last week's tickets` answers "Task #3 started" and returns control to the chat. A worker runs the request on the task harness and
posts **only the result** (or the error) into the same chat. `/tasks` lists the latest ones.

| Want | Do |
| --- | --- |
| Another harness for tasks | `.env`: `TEAMS_TASK_HARNESS=claude` (default, `claude -p` with its own tools) / `codex` / `builtin` / `command` / an extension name. Missing CLI: falls back to `BOT_HARNESS` |
| More or fewer at once | `.env`: `TEAMS_TASK_MAX=2` |
| Tell the task harness more | `task.md` and the knowledge notes are part of its prompt, like for chat |
| Fixed jobs with a name | `@bot.command("report", "weekly report")` in `ext/`, and inside call `ctx["hub"].jobs.start(ctx["conv"], ctx["sender"], "write the weekly report from my notes")` |

Jobs are files in `<bot home>/jobs/` (state, request, result); a restart marks unfinished ones `interrupted`. Tasks run with the harness's own permissions: the
`claude` harness can use whatever tools your Claude Code allows, so restrict who may call it with `TEAMS_ALLOWED`.
