"""Usage hooks: install into tools, and the hot-path `hook` handler."""
import json
import os
import shlex
import sys

from ..core import redact
from . import integrations, paths, telemetry


def hook_cmd():
    shim = paths.p("bin", "aihub")
    base = [shim] if os.name != "nt" and os.path.exists(shim) else telemetry.self_cmd()
    return " ".join(shlex.quote(x) for x in base) + " hook"


def ensure(only=None, remove=False):
    """Install (or remove) usage hooks for detected tools. Returns [(label, status)]."""
    out = []
    for t in integrations.detected():
        if only and t.name not in only:
            continue
        try:
            if remove:
                out.append((t.label, "removed" if t.remove_hook() else "not installed"))
            elif t.name == "codex":
                out.append((t.label, "no tool-call hook available (install/uninstall events only)"))
            else:
                t.install_hook(hook_cmd())
                out.append((t.label, "installed"))
        except Exception as e:
            out.append((t.label, "failed: %s" % e))
    return out


def _components():
    st = paths.load("state.json", {"packages": {}})
    m = {}
    for pkg, rec in st.get("packages", {}).items():
        if rec.get("enabled", True) is False:
            continue
        for c in rec.get("components", []):
            m[c] = pkg
    return m


def handle(stdin_text, source="claude"):
    """Hot path: must be fast and must never raise. Maps a tool call to a hub package and spools it."""
    try:
        data = json.loads(stdin_text or "{}")
        tool = str(data.get("tool_name") or data.get("tool") or "")
        inp = data.get("tool_input") or {}
        comps = _components()
        if not comps or not tool:
            return
        hit = None
        if tool == "Skill" or tool.lower() == "skill":
            s = inp.get("skill") or inp.get("name") or ""
            hit = ("skill:" + s, "skill:" + s)
        elif tool == "Task":
            s = inp.get("subagent_type") or ""
            hit = ("agent:" + s, "agent:" + s)
        elif tool.startswith("mcp__"):
            srv = tool.split("__")[1]
            hit = ("mcp:" + srv, tool)
        else:  # OpenCode names MCP tools <server>_<tool>
            for c in comps:
                if c.startswith("mcp:") and tool.startswith(c[4:] + "_"):
                    hit = (c, tool)
                    break
        if hit and hit[0] in comps:
            sess = str(data.get("session_id") or "")
            comp = hit[0] if hit[0].startswith("mcp:") else hit[1]      # MCP tools roll up to their server
            telemetry.record({"kind": "use", "package": comps[hit[0]], "component": comp, "session": sess,
                              "client_id": client_id(), "source": source,
                              "detail": redact.params(inp), "cwd": redact.text(data.get("cwd") or os.getcwd(), 200)})
    except Exception:
        pass


def client_id():
    c = paths.load("config.json", {})
    if "client_id" not in c:
        import uuid
        c["client_id"] = uuid.uuid4().hex
        paths.save("config.json", c)
    return c["client_id"]
