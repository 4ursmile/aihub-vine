"""Usage hooks: install into tools, and the hot-path `hook` handler."""
import json
import os
import re
import shlex
import sys

from ..core import redact
from . import integrations, paths, telemetry


def hook_cmd():
    shim = paths.p("bin", "aihub")
    base = [shim] if os.name != "nt" and os.path.exists(shim) else telemetry.self_cmd()
    return " ".join(_quote(x) for x in base) + " hook"


def _quote(x):
    """Quote one argument for the shell that runs the hook. Claude Code on Windows runs hooks in Git Bash, where an unquoted
    backslash path (C:\\Users\\me\\python.exe) loses its backslashes, so Windows paths get forward slashes and double quotes."""
    if os.name == "nt":
        x = x.replace("\\", "/")
        return '"%s"' % x if any(c in x for c in " ()&^%!$`'") else x
    return shlex.quote(x)


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


def _skill_comp(name, comps):
    """Map what the tool reported ("name", "/name", "pkg:name", "Name") to an installed skill component, or None."""
    n = str(name or "").strip().lstrip("/")
    cands = [n, n.rsplit(":", 1)[-1]]
    for c in cands:
        if "skill:" + c in comps:
            return "skill:" + c
    low = {k.lower().replace("_", "-"): k for k in comps if k.startswith("skill:")}
    for c in cands:
        k = low.get("skill:" + c.lower().replace("_", "-"))
        if k:
            return k
    return None


def handle(stdin_text, source="claude"):
    """Hot path: must be fast and must never raise. Maps a tool call to a hub package and spools it."""
    try:
        data = json.loads(stdin_text or "{}")
        tool = str(data.get("tool_name") or data.get("tool") or "")
        inp = data.get("tool_input") or {}
        comps = _components()
        if not comps:
            return
        hit = None
        prompt = data.get("prompt")
        if not tool and isinstance(prompt, str):      # UserPromptSubmit: "/skill-name args" runs a skill without a tool call
            m = re.match(r"\s*/([\w.:-]+)", prompt)
            k = _skill_comp(m.group(1), comps) if m else None
            if k:
                hit = (k, k)
                inp = {"prompt": prompt[:200]}
        elif not tool:
            return
        elif tool.lower() == "skill":
            k = _skill_comp(inp.get("skill") or inp.get("name") or inp.get("command"), comps)
            if k:
                hit = (k, k)
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
