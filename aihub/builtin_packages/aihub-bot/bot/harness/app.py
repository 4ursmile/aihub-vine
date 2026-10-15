"""CLI of the bot: chat REPL (default), --once "question", --cron (run scheduled prompts), --info (what the bot sees), --memory (what it remembers)."""
import sys

from . import config, cron, memory, skills
from .agent import Agent


def info(agent):
    print("bot home : %s\nsettings : %s\nknowledge: %s (%d notes)\nmcp      : %s\nmodel    : %s" % (
        config.HOME, config.ENV_FILE, config.KNOWLEDGE, len(memory.notes()), ", ".join(s.name for s in agent.servers) or "none", config.MODEL))
    print("harness  : %s\nmemory   : %s" % (agent.executor.name, agent.memory.dump().replace("\n", " ")))
    print("skills   : %s\ntools    : %s" % (", ".join(sorted(skills.index())) or "none", ", ".join(t["name"] for t in agent.tool_specs())))
    print("extend   : drop a .py with register(bot) into %s/ext/   cron: %s/cron.json" % (config.HOME, config.HOME))


def main(argv):
    agent = Agent()
    try:
        if "--info" in argv:
            return info(agent) or 0
        if "--memory" in argv:
            print(agent.memory.dump())
            for r in agent.memory.facts():
                print("- [%s] %s" % (r["id"], r["text"]))
            print(agent.memory.summary())
            return 0
        if "--once" in argv:
            i = argv.index("--once")
            print(agent.ask(" ".join(argv[i + 1:]) or sys.stdin.read(), keep=False))
            return 0
        if "--cron" in argv:
            jobs = cron.load(agent.bot.jobs)
            if not jobs:
                print("no jobs. Add some to %s/cron.json or with bot.cron() in an extension." % config.HOME)
                return 1
            cron.loop(jobs, lambda p: agent.ask(p, keep=False))
        print("aihub-bot (%s). /info, /exit. Soul: aihub knowledge edit soul" % config.MODEL)
        while True:
            try:
                q = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if q in ("/exit", "/quit"):
                return 0
            if q == "/info":
                info(agent)
            elif q:
                try:
                    print("bot> " + agent.ask(q))
                except RuntimeError as e:
                    print("error: %s" % e)
    finally:
        agent.close()
