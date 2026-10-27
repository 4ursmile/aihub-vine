# aihub-teams-bot

A Microsoft Teams bot built on [aihub-bot](../aihub-bot) (it is installed automatically as a dependency). Same soul, knowledge notes, skills, MCP,
cron, extensions and harness choice (`BOT_HARNESS`); this package adds the Teams side.

```sh
aihub install aihub-teams-bot       # offers an OPTIONAL guided setup (Teams CLI login popup, app registration, fills .env)
aihub onboard aihub-teams-bot       # run that guided setup later (needs Node 22.12+); or fill CLIENT_ID / CLIENT_SECRET / TENANT_ID by hand
aihub run aihub-teams-bot --fg -- --try "hello" # test the brain without Teams
aihub run aihub-teams-bot --fg -- --info        # what it will do
aihub run aihub-teams-bot                       # serves http://localhost:3978/api/messages in the background (aihub run logs aihub-teams-bot -f)
```

Teams must reach that endpoint over https (a dev tunnel or your server); register it as the bot's messaging endpoint.

| Added here | What |
| --- | --- |
| One memory per chat | `~/.aihub/bots/aihub-teams-bot/memory/chats/<hash>/` (facts, log, summary), so channels never mix |
| Task | `TEAMS_TASK=helpdesk` (or general, standup, meeting-notes); your own `task.md` in the bot home wins |
| Allow-list | `TEAMS_ALLOWED=ada@x.org,Bob` limits who gets answers |
| Slash commands | `/help /reset /memory /whoami /about`, plus your own from extensions |
| Background tasks | `/task <request>` confirms at once, runs on a harness (default `claude -p`, `TEAMS_TASK_HARNESS`) and posts only the result; `/tasks` lists them |
| Local playground | `TEAMS_LOCAL=1` accepts unauthenticated requests: development only |

Customise it without forking: read **CUSTOMIZE.md**.
