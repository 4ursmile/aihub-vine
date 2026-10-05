"""Tool adapters (Claude Code, Codex, OpenCode). Every mutation returns a revert record."""
import json
import os
import re
import shlex
import shutil

HOME = lambda: os.path.expanduser("~")
MARK = "aihub"


# ---------------------------------------------------------------- file helpers
def _copy(src, dst):
    if os.path.isdir(src):
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst)
    else:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    return {"op": "rm", "path": dst}


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except ValueError:
        raise ValueError("%s is not valid JSON; refusing to modify it" % path)


def _write_json(path, d):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".aihub.tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def block_begin(i): return "# >>> aihub:%s" % i
def block_end(i): return "# <<< aihub:%s" % i


def add_block(path, bid, text):
    """Insert/replace a managed text block (idempotent)."""
    cur = open(path).read() if os.path.exists(path) else ""
    cur = remove_block_text(cur, bid)
    if cur and not cur.endswith("\n"):
        cur += "\n"
    cur += "%s\n%s\n%s\n" % (block_begin(bid), text.rstrip("\n"), block_end(bid))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write(cur)
    return {"op": "block", "path": path, "id": bid}


def remove_block_text(text, bid):
    pat = re.compile(r"\n?%s\n.*?%s\n?" % (re.escape(block_begin(bid)), re.escape(block_end(bid))), re.S)
    return pat.sub("\n", text).lstrip("\n") if pat.search(text) else text


def revert(rec):
    if not rec:
        return
    op = rec["op"]
    try:
        if op == "rm":
            p = rec["path"]
            shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else (os.path.exists(p) and os.remove(p))
        elif op == "json_key":
            d = _read_json(rec["path"])
            tgt = d
            for k in rec["key"].split("."):
                tgt = tgt.get(k, {})
            if rec.get("prev") is None:
                tgt.pop(rec["name"], None)
            else:
                tgt[rec["name"]] = rec["prev"]
            _write_json(rec["path"], d)
        elif op == "block":
            if os.path.exists(rec["path"]):
                t = remove_block_text(open(rec["path"]).read(), rec["id"])
                open(rec["path"], "w").write(t)
        elif op == "restore":  # backup written by setup profiles
            if os.path.exists(rec["backup"]):
                shutil.copy2(rec["backup"], rec["path"])
            elif os.path.exists(rec["path"]):
                os.remove(rec["path"])
    except Exception:
        pass


def _json_key(path, key, name, value):
    d = _read_json(path)
    tgt = d
    for k in key.split("."):
        tgt = tgt.setdefault(k, {})
    prev = tgt.get(name)
    tgt[name] = value
    _write_json(path, d)
    return {"op": "json_key", "path": path, "key": key, "name": name, "prev": prev}


# ---------------------------------------------------------------- adapters
class Tool(object):
    name = ""
    label = ""
    def detect(self): return False
    def add_skill(self, name, src): return None
    def add_agent(self, name, src): return None
    def add_mcp(self, name, spec): return None          # spec: {command,args,env}
    def install_hook(self, cmd): return None             # cmd: shell command string
    def remove_hook(self): return False
    def has_hook(self): return False


class ClaudeCode(Tool):
    name, label = "claude", "Claude Code"
    def __init__(self): self.d = lambda *a: os.path.join(HOME(), ".claude", *a)
    def detect(self): return os.path.isdir(self.d()) or bool(shutil.which("claude"))
    def add_skill(self, name, src): return _copy(src, self.d("skills", name))
    def add_agent(self, name, src): return _copy(src, self.d("agents", name + ".md"))
    def add_mcp(self, name, spec):
        v = {"type": "stdio", "command": spec["command"], "args": spec.get("args", []), "env": spec.get("env", {})}
        return _json_key(os.path.join(HOME(), ".claude.json"), "mcpServers", name, v)

    def _settings(self): return self.d("settings.json")
    @staticmethod
    def _ours(h): return " hook --source " in h.get("command", "")

    def has_hook(self):
        try:
            d = _read_json(self._settings())
        except ValueError:
            return False
        return any(self._ours(h) for g in d.get("hooks", {}).get("PreToolUse", []) for h in g.get("hooks", []))

    def install_hook(self, cmd):
        d = _read_json(self._settings())
        groups = d.setdefault("hooks", {}).setdefault("PreToolUse", [])
        for g in groups:  # drop stale copies of ours, then add fresh
            g["hooks"] = [h for h in g.get("hooks", []) if not self._ours(h)]
        groups[:] = [g for g in groups if g.get("hooks")]
        groups.append({"matcher": "*", "hooks": [{"type": "command", "command": cmd + " --source claude", "timeout": 5}]})
        _write_json(self._settings(), d)
        return True

    def remove_hook(self):
        d = _read_json(self._settings())
        groups = d.get("hooks", {}).get("PreToolUse", [])
        n = sum(len(g.get("hooks", [])) for g in groups)
        for g in groups:
            g["hooks"] = [h for h in g.get("hooks", []) if not self._ours(h)]
        if "hooks" in d:
            d["hooks"]["PreToolUse"] = [g for g in groups if g.get("hooks")]
            if not d["hooks"]["PreToolUse"]:
                del d["hooks"]["PreToolUse"]
            if not d["hooks"]:
                del d["hooks"]
        _write_json(self._settings(), d)
        return n != sum(len(g.get("hooks", [])) for g in groups)


class Codex(Tool):
    """Skills -> ~/.codex/skills. MCP -> managed [mcp_servers.X] block in ~/.codex/config.toml.
    Codex has no tool-call hook, so MCP servers are launched through `aihub exec-mcp` (counts sessions)."""
    name, label = "codex", "Codex"
    def __init__(self): self.d = lambda *a: os.path.join(os.environ.get("CODEX_HOME") or os.path.join(HOME(), ".codex"), *a)
    def detect(self): return os.path.isdir(self.d()) or bool(shutil.which("codex"))
    def add_skill(self, name, src): return _copy(src, self.d("skills", name))
    def add_agent(self, name, src):  # no native agent concept; expose as a custom prompt
        return _copy(src, self.d("prompts", name + ".md"))

    def add_mcp(self, name, spec):
        cfg = self.d("config.toml")
        if os.path.exists(cfg) and re.search(r"^\[mcp_servers\.%s\]" % re.escape(name), re.sub(
                r"%s.*?%s" % (re.escape(block_begin("mcp:" + name)), re.escape(block_end("mcp:" + name))), "",
                open(cfg).read(), flags=re.S), re.M):
            raise ValueError("~/.codex/config.toml already defines [mcp_servers.%s]; not overwriting" % name)
        lines = ["[mcp_servers.%s]" % name, "command = %s" % json.dumps(spec["command"]),
                 "args = %s" % json.dumps(spec.get("args", []))]
        if spec.get("env"):
            lines.append("env = { %s }" % ", ".join("%s = %s" % (json.dumps(k), json.dumps(v)) for k, v in spec["env"].items()))
        return add_block(cfg, "mcp:" + name, "\n".join(lines))

    def has_hook(self): return True
    def install_hook(self, cmd): return True


class OpenCode(Tool):
    """Skills -> skill/, agents -> agent/, MCP -> opencode.json `mcp`, usage -> plugin/aihub-usage.js"""
    name, label = "opencode", "OpenCode"
    def __init__(self): self.d = lambda *a: os.path.join(HOME(), ".config", "opencode", *a)
    def detect(self): return os.path.isdir(self.d()) or bool(shutil.which("opencode"))
    def add_skill(self, name, src): return _copy(src, self.d("skill", name))
    def add_agent(self, name, src): return _copy(src, self.d("agent", name + ".md"))
    def add_mcp(self, name, spec):
        v = {"type": "local", "command": [spec["command"]] + list(spec.get("args", [])), "enabled": True}
        if spec.get("env"):
            v["environment"] = spec["env"]
        return _json_key(self.d("opencode.json"), "mcp", name, v)

    def _plugin(self): return self.d("plugin", "aihub-usage.js")
    def has_hook(self): return os.path.exists(self._plugin())
    def install_hook(self, cmd):
        js = PLUGIN_JS.replace("__CMD__", json.dumps(shlex.split(cmd) + ["--source", "opencode"]))
        os.makedirs(os.path.dirname(self._plugin()), exist_ok=True)
        open(self._plugin(), "w").write(js)
        return True
    def remove_hook(self):
        if self.has_hook():
            os.remove(self._plugin())
            return True
        return False


PLUGIN_JS = """// Managed by aihub. Reports tool usage without ever blocking or failing the agent.
import { spawn } from "node:child_process"
const CMD = __CMD__
export const AihubUsage = async () => ({
  "tool.execute.before": async (input, output) => {
    try {
      const p = spawn(CMD[0], CMD.slice(1), { detached: true, stdio: ["pipe", "ignore", "ignore"] })
      p.on("error", () => {})
      p.stdin.on("error", () => {})
      p.stdin.end(JSON.stringify({ tool_name: input.tool, tool_input: (output && output.args) || {} }))
      p.unref()
    } catch (e) {}
  },
})
"""

TOOLS = {t.name: t for t in (ClaudeCode(), Codex(), OpenCode())}


def detected():
    return [t for t in TOOLS.values() if t.detect()]
