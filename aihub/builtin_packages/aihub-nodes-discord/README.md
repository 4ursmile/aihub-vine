# aihub-nodes-discord

Node package for the aihub-ui Works editor (guide: docs/ui-nodes.md). A full Discord **bot**; for a plain channel webhook use the builtin *Send to Discord* node.

- **Start: Discord message** (`nodes/discord-message.node.json`): connects to the Discord gateway (outbound only, no tunnel) and starts the work for each message the bot can see. Filters: channel IDs, server ID, sender regex, only mentions, bots ignored by default.
- **Send as Discord bot** (`nodes/discord-send.node.json`): sends to a channel ID, or replies to the message that started the work (`channel_id` / `message_id` come from the trigger's data).

Setup: https://discord.com/developers/applications > New Application > Bot > copy the token, turn on **Message Content Intent**, then invite the bot with OAuth2 > URL Generator (scope `bot`, permissions Read/Send Messages). Create a **Discord bot** credential set in aihub-ui Settings > Credentials and pick it in both nodes.

More bot features (reactions, threads, slash commands) can be added as further nodes in this package.
