"""Everything that is not the Teams SDK, so it can be tested and customised without Teams:
message cleaning, allow-list, the task prompt, one memory per chat, slash commands. `Hub.handle` is the whole behaviour."""
import hashlib
import os
import re
import threading

from harness import config, memory

from .jobs import Jobs

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))          # <package>/bot
STYLE = ("\n\nYou are answering inside Microsoft Teams: short paragraphs, simple markdown (bold, lists, code blocks), no wide tables. "
         "Messages are prefixed with the sender's name; several people may share this chat.")
AT = re.compile(r"<at>.*?</at>", re.S | re.I)


def clean(text):
    """Drop the @mention tag Teams puts in front of the text, keep the rest (including line breaks)."""
    t = AT.sub(" ", text or "").replace("&nbsp;", " ")
    return re.sub(r"[ \t]+", " ", t).strip()


def allowed(sender, spec):
    """spec = TEAMS_ALLOWED. Empty allows everyone; otherwise the sender's id / name / aad_id / email must be listed (case-insensitive)."""
    want = {x.lower() for x in re.split(r"[,;\s]+", spec or "") if x}
    if not want:
        return True
    have = {str(sender.get(k) or "").strip().lower() for k in ("id", "name", "aad_id", "email")} - {""}
    return bool(want & have)


def task_text():
    """The job description. <bot home>/task.md (yours) wins; else bot/tasks/<TEAMS_TASK>.md from the package; else general."""
    mine = os.path.join(config.HOME, "task.md")
    name = re.sub(r"[^a-z0-9_-]", "", (os.environ.get("TEAMS_TASK") or "general").lower()) or "general"
    for p in (mine, os.path.join(HERE, "tasks", name + ".md"), os.path.join(HERE, "tasks", "general.md")):
        try:
            with open(p, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            continue
    return ""


# ---- slash commands: fn(ctx) -> text. ctx = {args, sender, conv, memory, agent, hub}. Extensions add more with @bot.command(name, description)

def _help(ctx):
    return "\n".join("- `/%s` %s" % (n, d) for n, (d, _) in sorted(ctx["hub"].commands.items()))


def _reset(ctx):
    for p in (ctx["memory"].log_f, ctx["memory"].sum_f):
        try:
            os.remove(p)
        except OSError:
            pass
    return "Conversation cleared (remembered facts are kept; ask me to forget them if you want)."


def _memory(ctx):
    fs = ctx["memory"].facts()
    return "\n".join("- %s" % f["text"] for f in fs) or "I have not remembered anything in this chat yet."


def _whoami(ctx):
    s = ctx["sender"]
    return "You are %s (id `%s`). Chat `%s`." % (s.get("name") or "unknown", s.get("id") or "?", ctx["conv"] or "?")


def _about(ctx):
    return task_text()[:1500] or "No role text."


def _task(ctx):
    return ctx["hub"].jobs.start(ctx["conv"], ctx["sender"], ctx["args"])


def _tasks(ctx):
    return ctx["hub"].jobs.listing(ctx["conv"])


BUILTIN = {"help": ("this list", _help), "reset": ("clear this chat's conversation memory", _reset),
           "memory": ("what I remember in this chat", _memory), "whoami": ("who Teams says you are", _whoami),
           "about": ("what this bot is set up to do (its role)", _about),
           "task": ("run a request in the background, post only the result: /task summarize last week's tickets", _task),
           "tasks": ("your recent background tasks", _tasks)}


class Hub:
    """One shared agent (MCP servers start once), one memory folder per conversation, one message at a time."""

    def __init__(self, agent):
        self.agent, self.lock = agent, threading.Lock()
        self.commands = dict(BUILTIN)
        self.commands.update(getattr(agent.bot, "commands", {}))          # extension commands override built-ins
        self.notify = None                                                 # set by the Teams layer: notify(conversation_id, text)
        self.jobs = Jobs(self)
        self.slot = len(agent.bot.prompts)
        agent.bot.prompts.append("")                                       # refreshed per message, so editing task.md needs no restart

    def task_prompt(self):
        return task_text() + STYLE

    def store(self, conv):
        key = hashlib.sha1(str(conv or "default").encode()).hexdigest()[:16]
        return memory.Store(os.path.join(config.HOME, "memory", "chats", key))

    def handle(self, conv, sender, text):
        text = clean(text)
        if not text:
            return "Send me a message and I will help. Try `/help`."
        if not allowed(sender, os.environ.get("TEAMS_ALLOWED", "")):
            return "Sorry, you are not on this bot's allow-list. Ask its owner to add you."
        with self.lock:
            store = self.store(conv)
            self.agent.memory = store
            if text.startswith("/"):
                name, _, args = text[1:].partition(" ")
                cmd = self.commands.get(name.lower())
                if cmd:
                    return str(cmd[1]({"args": args.strip(), "sender": sender, "conv": conv, "memory": store, "agent": self.agent, "hub": self}))
            self.agent.bot.prompts[self.slot] = self.task_prompt()
            who = sender.get("name")
            return self.agent.ask("%s: %s" % (who, text) if who else text)
