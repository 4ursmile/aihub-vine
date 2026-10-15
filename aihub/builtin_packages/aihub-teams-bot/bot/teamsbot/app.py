"""Teams SDK glue (https://microsoft.github.io/teams-sdk/). The SDK is imported only when serving, so --info / --try work without it."""
import asyncio
import os
import sys

from harness import config, ext as extmod
from harness.agent import Agent

from .core import Hub

LIMIT = 12000                  # characters per Teams message


def _command(self, name, description=""):
    """For extensions: @bot.command("ticket", "Open a ticket") def ticket(ctx): return "text". ctx has args, sender, conv, memory, agent."""
    def deco(fn):
        self.__dict__.setdefault("commands", {})[name.lstrip("/").lower()] = (description, fn)
        return fn
    return deco


def build():
    extmod.Bot.command = _command             # must exist before the extensions are loaded by Agent()
    agent = Agent()
    return agent, Hub(agent)


def sender_of(activity):
    f = getattr(activity, "from_", None) or getattr(activity, "from", None)
    g = lambda k: str(getattr(f, k, "") or "")
    return {"id": g("id"), "name": g("name"), "aad_id": g("aad_object_id")}


def info(agent, hub):
    print("teams bot  : task=%s harness=%s allow-list=%s" % (os.environ.get("TEAMS_TASK") or "general", agent.executor.name,
                                                             os.environ.get("TEAMS_ALLOWED") or "everyone"))
    print("settings   : %s\nbot home   : %s\ncommands   : %s" % (config.ENV_FILE, config.HOME, ", ".join("/" + c for c in sorted(hub.commands))))
    print("tools      : %s\ncredentials: %s" % (", ".join(t["name"] for t in agent.tool_specs()),
                                                  "set" if os.environ.get("CLIENT_ID") and os.environ.get("CLIENT_SECRET") else "NOT set (edit the settings file)"))


def serve(hub):
    if not os.environ.get("TEAMS_LOCAL") and not (os.environ.get("CLIENT_ID") and os.environ.get("CLIENT_SECRET")):
        raise SystemExit("Teams credentials are missing. Put CLIENT_ID, CLIENT_SECRET and TENANT_ID in:\n  %s\n(or TEAMS_LOCAL=1 for the local playground)" % config.ENV_FILE)
    try:
        from microsoft_teams.api import MessageActivity, TypingActivityInput
        from microsoft_teams.apps import ActivityContext, App
    except ImportError:
        raise SystemExit("the Teams SDK is not installed here. Reinstall the bot: aihub install aihub-teams-bot   (or pip install microsoft-teams-apps)")
    from microsoft_teams.api import MessageActivityInput
    app = App(dangerously_allow_unauthenticated_requests=True) if os.environ.get("TEAMS_LOCAL") == "1" else App()

    @app.on_message
    async def on_message(ctx: ActivityContext[MessageActivity]):
        a = ctx.activity
        await ctx.reply(TypingActivityInput())
        conv = str(getattr(getattr(a, "conversation", None), "id", "") or "")
        try:
            reply = await asyncio.to_thread(hub.handle, conv, sender_of(a), a.text or "")
        except Exception as e:                                    # never leave the user without an answer
            reply = "Sorry, I could not answer that (%s)." % str(e)[:200]
        for i in range(0, max(len(reply), 1), LIMIT):
            await ctx.send(reply[i:i + LIMIT] or "(empty answer)")

    async def run():
        loop = asyncio.get_running_loop()

        def notify(conv, text):                                   # called from a task thread: post the result into the chat
            for i in range(0, max(len(text), 1), LIMIT):
                asyncio.run_coroutine_threadsafe(app.send(conv, MessageActivityInput(text=text[i:i + LIMIT])), loop).result(60)
        hub.notify = notify
        await app.start()

    print("aihub-teams-bot: messaging endpoint http://localhost:3978/api/messages (expose it over https for Teams)")
    asyncio.run(run())


def main(argv):
    agent, hub = build()
    try:
        if "--info" in argv:
            return info(agent, hub) or 0
        if "--try" in argv:
            i = argv.index("--try")
            hub.notify = lambda conv, text: print(text)
            print(hub.handle("try", {"id": "local", "name": "You"}, " ".join(argv[i + 1:])))
            hub.jobs.wait()                                       # `--try "/task ..."` prints the result when the job ends
            return 0
        serve(hub)
        return 0
    finally:
        agent.close()
