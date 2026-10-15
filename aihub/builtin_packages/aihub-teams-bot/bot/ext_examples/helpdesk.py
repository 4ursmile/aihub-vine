"""Copy to ~/.aihub/bots/aihub-teams-bot/ext/helpdesk.py. Adds a tool the model can call and a /ticket command people can type."""
import json
import os
import time

from harness import config


def register(bot):
    path = os.path.join(config.HOME, "tickets.jsonl")

    def open_ticket(who, title):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "who": who, "title": title}) + "\n")
        return "ticket opened: %s" % title

    @bot.tool("open_ticket", "Open a helpdesk ticket when the user's problem needs a human.",
              {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]})
    def tool(args):
        return open_ticket("bot", args.get("title", "untitled"))

    @bot.command("ticket", "open a ticket: /ticket printer is jammed")      # bot.command is added by the Teams layer
    def command(ctx):
        return open_ticket(ctx["sender"].get("name") or "?", ctx["args"] or "no title")

    bot.prompt("When you cannot solve the problem, call open_ticket instead of promising a follow-up.")
