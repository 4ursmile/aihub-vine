#!/usr/bin/env python3
"""Trigger node: a long-lived Discord bot. aihub-ui writes ONE line of JSON to stdin ({"config": {...}}); every taken message is printed as
{"event": {"text": ..., "data": {...}}}. Logs go to stderr. Needs discord.py (installed into this package's environment)."""
import json
import re
import sys

import discord

cfg = json.loads(sys.stdin.readline())["config"]
TOKEN = str(cfg.get("bot_token") or "").strip()
if not TOKEN:
    sys.stderr.write("bot token missing (Developer Portal > Bot)\n")
    sys.exit(2)
chans = {c.strip() for c in str(cfg.get("channel_ids") or "").replace(",", " ").split() if c.strip()}
guild = str(cfg.get("guild_id") or "").strip()
user_rx = re.compile(cfg["from_regex"], re.I) if cfg.get("from_regex") else None
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    sys.stderr.write("connected as %s\n" % client.user)
    sys.stderr.flush()


@client.event
async def on_message(m):
    if m.author == client.user or (m.author.bot and not cfg.get("include_bots")):
        return
    text = (m.content or "").strip()
    me = client.user
    if cfg.get("only_mentions") and m.guild and not (me and me.mentioned_in(m)):
        return
    if me:
        text = text.replace("<@%s>" % me.id, "").replace("<@!%s>" % me.id, "").strip()
    name = m.author.display_name or m.author.name
    if not text or (chans and str(m.channel.id) not in chans) or (guild and str(m.guild.id if m.guild else "") != guild) or (user_rx and not user_rx.search(name)):
        return
    print(json.dumps({"event": {"text": text, "data": {"from": name, "channel_id": str(m.channel.id), "message_id": str(m.id), "guild_id": str(m.guild.id) if m.guild else ""}}},
                     ensure_ascii=False), flush=True)


try:
    client.run(TOKEN, log_handler=None)
except discord.LoginFailure:
    sys.stderr.write("Discord refused the bot token\n")
    sys.exit(2)
except discord.PrivilegedIntentsRequired:
    sys.stderr.write("turn on Message Content Intent for the bot in the Developer Portal\n")
    sys.exit(2)
