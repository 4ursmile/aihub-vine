"""The tool loop: system prompt (soul + knowledge + skills + extensions) -> model -> tool calls -> model ... until a text answer."""
import json

from . import config, executors, ext as extmod, llm, mcp, memory, skills

BUILTIN = [
    {"name": "read_note", "description": "Read one of the user's knowledge notes by name.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "save_note", "description": "Create or replace a knowledge note (markdown) the user asked you to remember.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "body": {"type": "string"}}, "required": ["name", "body"]}},
    {"name": "remember", "description": "Store a short fact in your own long-term memory (preferences, decisions, names, open tasks).", "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "recall", "description": "Search your long-term memory.", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "forget", "description": "Delete remembered facts by id or by text they contain.", "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
    {"name": "read_skill", "description": "Load the full instructions of an installed skill by name.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
]


class Agent:
    def __init__(self, log=print):
        self.log = log
        self.bot = extmod.Bot()
        extmod.load(self.bot, log)
        self.servers = mcp.start_all(log)
        self.mcp_tools = {}
        for s in self.servers:
            for t in s.tools:
                key = "%s__%s" % (s.name, t["name"])
                self.mcp_tools[key] = (s, t["name"], {"name": key, "description": t.get("description", ""), "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}}})
        self.memory = memory.Store()
        self.history = []
        self.executor = executors.make(self)

    def system(self, hint="", query=""):
        parts = [memory.soul()]
        k = memory.prompt_block()
        if k:
            parts.append("# Personal knowledge\n" + k)
        m = self.memory.prompt_block(query)
        if m:
            parts.append("# Your memory\n" + m)
        s = skills.prompt_block()
        if s:
            parts.append("# Skills (load one with read_skill when it fits)\n" + s)
        parts += self.bot.prompts
        if hint:
            parts.append(hint)
        return "\n\n".join(parts)

    def tool_specs(self):
        return BUILTIN + [t["spec"] for t in self.bot.tools.values()] + [v[2] for v in self.mcp_tools.values()]

    def run_tool(self, name, args):
        try:
            if name == "read_note":
                return memory.read(args.get("name", ""))
            if name == "save_note":
                return memory.save(args.get("name", ""), args.get("body", ""))
            if name == "remember":
                return self.memory.remember(args.get("text", ""))
            if name == "recall":
                return "\n".join("- [%s] %s" % (r["id"], r["text"]) for r in self.memory.recall(args.get("query", ""))) or "nothing found"
            if name == "forget":
                return self.memory.forget(str(args.get("key", "")))
            if name == "read_skill":
                return skills.read(args.get("name", ""))
            if name in self.bot.tools:
                return str(self.bot.tools[name]["fn"](args))
            if name in self.mcp_tools:
                s, real, _ = self.mcp_tools[name]
                res = s.call("tools/call", {"name": real, "arguments": args}) or {}
                return "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text") or json.dumps(res)
            return "unknown tool " + name
        except Exception as e:
            return "tool error: %s" % e

    def ask(self, text, keep=True):
        """One user message -> answer, through the selected harness (executors.py); memory is managed around it."""
        return self.executor.ask(text, keep)

    def loop(self, text, system, keep=True):
        """The builtin harness: model <-> tools until a text answer. History is the memory log, so it survives restarts."""
        past = [{"role": r["role"], "content": r["text"]} for r in self.memory.recent(12)] if keep else []
        msgs = past + [{"role": "user", "content": text}]
        tools = self.tool_specs()
        answer = ""
        for _ in range(config.MAX_TURNS):
            r = llm.complete(system, msgs, tools)
            msgs.append({"role": "assistant", "content": r["content"]})
            answer = "".join(b.get("text", "") for b in r["content"] if b.get("type") == "text")
            calls = [b for b in r["content"] if b.get("type") == "tool_use"]
            if not calls:
                break
            msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": c["id"], "content": self.run_tool(c["name"], c.get("input") or {})} for c in calls]})
        return answer

    def close(self):
        for s in self.servers:
            s.close()
