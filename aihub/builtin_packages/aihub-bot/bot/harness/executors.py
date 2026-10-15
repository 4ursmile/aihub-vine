"""Execute layer: HOW a prompt becomes an answer. The bot (soul, knowledge, skills, memory, MCP, cron) stays the same; the executor is the harness
underneath. Pick with BOT_HARNESS in .env: builtin | claude | codex | command | <name registered by an extension via bot.executor()>.

An executor implements run(text, system, keep) -> str. Memory is handled around it for every harness: recent turns are logged, old ones are
compacted into a summary, and a reply line `REMEMBER: <fact>` is stored in the bot's memory and removed from the answer."""
import os
import re
import shlex
import shutil
import subprocess

from . import config

MARK = re.compile(r"^[ \t]*REMEMBER:[ \t]*(.+?)[ \t]*$", re.M)
HINT = "To keep something for future conversations, add a line `REMEMBER: <one short fact>` at the end of your reply."


class Executor:
    name = ""
    hint = HINT                   # harnesses without their own memory tools are told the REMEMBER convention

    def __init__(self, agent):
        self.agent = agent

    def run(self, text, system, keep=True):
        raise NotImplementedError

    def transcript(self, text):
        """For stateless harnesses: recent turns + the new message as one prompt."""
        rows = self.agent.memory.recent(12)
        if not rows:
            return text
        return "Conversation so far:\n%s\n\nUser: %s" % ("\n".join("%s: %s" % (r["role"].capitalize(), r["text"]) for r in rows), text)

    def ask(self, text, keep=True):
        mem = self.agent.memory
        out = self.run(text, self.agent.system(self.hint, text), keep)
        for fact in MARK.findall(out):
            mem.remember(fact)
        out = MARK.sub("", out).strip()
        if keep:
            mem.log("user", text)
            mem.log("assistant", out)
            try:
                mem.compact(lambda t: self.run("Summarize this conversation in at most 12 short bullet points. Keep names, decisions and open tasks.\n\n" + t,
                                               "You write compact, factual summaries.", False))
            except Exception as e:
                self.agent.log("memory compaction skipped: %s" % e)
        return out


class Builtin(Executor):
    """The bundled tool loop (agent.Agent.loop): skills, MCP, extension tools and memory tools, straight on the model API."""
    name, hint = "builtin", ""

    def run(self, text, system, keep=True):
        return self.agent.loop(text, system, keep)


class Cli(Executor):
    """Wraps another agent CLI. The soul, notes, skills list and memory go in as the system prompt; the CLI brings its own tools."""
    binary = ""

    def argv(self, text, system):
        raise NotImplementedError

    def run(self, text, system, keep=True):
        if not shutil.which(self.binary):
            raise RuntimeError("harness %r needs the `%s` command on PATH (or set BOT_HARNESS=builtin)" % (self.name, self.binary))
        p = subprocess.run(self.argv(self.transcript(text) if keep else text, system), cwd=config.HOME, capture_output=True, text=True,
                           timeout=int(os.environ.get("BOT_TIMEOUT") or 600), stdin=subprocess.DEVNULL)
        if p.returncode:
            raise RuntimeError("%s exited %d: %s" % (self.binary, p.returncode, (p.stderr or p.stdout).strip()[:400]))
        return p.stdout.strip()


class Claude(Cli):
    name, binary = "claude", "claude"

    def argv(self, text, system):
        a = ["claude", "-p", text, "--append-system-prompt", system, "--output-format", "text"]
        if os.path.isfile(config.MCP_JSON) and '"command"' in open(config.MCP_JSON, encoding="utf-8").read():
            a += ["--mcp-config", config.MCP_JSON]
        return a + shlex.split(os.environ.get("BOT_CLAUDE_ARGS", ""))


class Codex(Cli):
    name, binary = "codex", "codex"

    def argv(self, text, system):
        return ["codex", "exec", system + "\n\n" + text] + shlex.split(os.environ.get("BOT_CODEX_ARGS", ""))


class Command(Cli):
    """Any program: BOT_HARNESS_CMD="python my_agent.py". The prompt arrives on stdin, the system prompt is the file $BOT_SYSTEM_FILE; print the answer on stdout."""
    name = "command"

    def run(self, text, system, keep=True):
        cmd = shlex.split(os.environ.get("BOT_HARNESS_CMD", ""))
        if not cmd:
            raise RuntimeError("BOT_HARNESS=command needs BOT_HARNESS_CMD in %s" % config.ENV_FILE)
        sysf = os.path.join(config.HOME, ".system.md")
        with open(sysf, "w", encoding="utf-8") as f:
            f.write(system)
        p = subprocess.run(cmd, input=self.transcript(text) if keep else text, cwd=config.HOME, capture_output=True, text=True,
                           env=dict(os.environ, BOT_SYSTEM_FILE=sysf), timeout=int(os.environ.get("BOT_TIMEOUT") or 600))
        if p.returncode:
            raise RuntimeError("%s exited %d: %s" % (cmd[0], p.returncode, (p.stderr or p.stdout).strip()[:400]))
        return p.stdout.strip()


class Custom(Executor):
    """An extension's own harness: bot.executor("name", lambda text, system: answer)."""

    def __init__(self, agent, name, fn):
        super().__init__(agent)
        self.name, self.fn = name, fn

    def run(self, text, system, keep=True):
        return str(self.fn(self.transcript(text) if keep else text, system))


KINDS = {"builtin": Builtin, "claude": Claude, "codex": Codex, "command": Command}


def make(agent, name=None):
    name = (name or config.HARNESS or "builtin").strip().lower()
    if name in agent.bot.executors:
        return Custom(agent, name, agent.bot.executors[name])
    if name not in KINDS:
        raise RuntimeError("unknown BOT_HARNESS %r; use one of: %s" % (name, ", ".join(sorted(set(KINDS) | set(agent.bot.executors)))))
    return KINDS[name](agent)
