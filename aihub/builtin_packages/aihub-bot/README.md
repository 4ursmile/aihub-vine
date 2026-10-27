# aihub-bot

Sample chat bot for AI Hub. Install it, put your key in the settings file, run it.

```sh
aihub install aihub-bot
# edit the file the installer printed (~/.aihub/bots/aihub-bot/.env): ANTHROPIC_API_KEY=...
aihub knowledge edit soul        # who the bot is for you
aihub run aihub-bot --fg         # chat in this terminal;  aihub run aihub-bot --fg -- --once "question"
aihub run aihub-bot -- --cron     # scheduled prompts in the background (aihub run list / logs aihub-bot -f / stop aihub-bot)
```

## Layout

| Path | Role |
| --- | --- |
| `bot/run.py` | entry point (`[bot] run`) |
| `bot/harness/agent.py` | system prompt, builtin tool loop, note/memory/skill tools |
| `bot/harness/executors.py` | execute layer: picks the harness from `BOT_HARNESS` (see below) |
| `bot/harness/memory.py` | soul + knowledge notes (`~/.aihub/knowledge`), and the bot's own store in `~/.aihub/bots/aihub-bot/memory/` |
| `bot/harness/skills.py` | skills of every installed package (`AIHUB_SKILL_DIRS`), loaded on demand |
| `bot/harness/mcp.py` | MCP servers from `~/.aihub/bots/aihub-bot/mcp.json` (built from this package's `[[mcp_servers]]`) |
| `bot/harness/cron.py` | scheduled prompts: `cron.json` next to `.env`, then `-- --cron` |
| `bot/harness/ext.py` | extensions: any `*.py` with `register(bot)` in `~/.aihub/bots/aihub-bot/ext/` |
| `bot/harness/llm.py` | model API for the builtin harness; swap it for another provider |

`BOT_HARNESS` in `.env`: `builtin` (default, needs `ANTHROPIC_API_KEY`) | `claude` (`claude -p`) | `codex` (`codex exec`) | `command` (`BOT_HARNESS_CMD`) | a name from an extension (`bot.executor`).

Your files in `~/.aihub/bots/aihub-bot/` (`.env`, `cron.json`, `ext/`, `memory/`) are never overwritten by updates.
`-- --info` shows what the bot sees, `-- --memory` what it remembers. Docs: `/#/docs/bots`.
