"""Copy to ~/.aihub/bots/aihub-bot/ext/clock.py to enable. One tool, one prompt line, one scheduled job."""
import time


def register(bot):
    @bot.tool("now", "Current local date and time")
    def now(args):
        return time.strftime("%A %Y-%m-%d %H:%M")

    bot.prompt("When the user mentions today / tomorrow, call the now tool first.")
    # bot.cron("standup", "09:00", "Remind me what is in my notes about today's tasks")
