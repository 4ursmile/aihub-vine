"""Extensions: every *.py in <bot home>/ext/ (yours, survives updates) and <package>/ext/ is imported and its register(bot) called.
    def register(bot):
        @bot.tool("now", "Current time", {"type": "object", "properties": {}})
        def now(args): return time.ctime()
        bot.executor("echo", lambda text, system: text)   # your own harness
        bot.prompt("Always answer in Vietnamese.")        # extra system prompt text
        bot.cron("morning", "08:30", "Summarize my notes")  # a scheduled prompt (same as cron.json)
See ext_examples/ in the package."""
import importlib.util
import os

from . import config


class Bot:
    def __init__(self):
        self.tools, self.prompts, self.jobs, self.executors = {}, [], [], {}

    def tool(self, name, description, schema=None):
        def deco(fn):
            self.tools[name] = {"spec": {"name": name, "description": description, "input_schema": schema or {"type": "object", "properties": {}}}, "fn": fn}
            return fn
        return deco

    def executor(self, name, fn):
        """Register a harness: fn(text, system) -> answer. Select it with BOT_HARNESS=<name> in .env."""
        self.executors[name] = fn

    def prompt(self, text):
        self.prompts.append(text)

    def cron(self, name, when, prompt):
        self.jobs.append({"name": name, "when": when, "prompt": prompt})


def load(bot, log=print):
    for d in (os.path.join(config.PKG, "ext"), os.path.join(config.HOME, "ext")):
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if not f.endswith(".py") or f.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location("botext_" + f[:-3], os.path.join(d, f))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                mod.register(bot)
            except Exception as e:
                log("extension %s failed: %s" % (f, e))
