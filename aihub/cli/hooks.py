"""Usage hooks: install into tools, and the hot-path `hook` handler."""
import json
import os
import re
import shlex
import sys
import time

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
                out.append((t.label, "installed" if t.install_hook(hook_cmd()) is not False else "already installed"))
        except Exception as e:
            out.append((t.label, "failed: %s" % e))
    return out


def verify():
    """Cheap self-heal, run after commands: re-add a missing or outdated usage hook. Never raises, never prompts."""
    try:
        cmd = hook_cmd()
        for t in integrations.detected():
            if t.name == "claude" and not t.has_hook(cmd):
                t.install_hook(cmd)
            elif t.name == "opencode" and not t.has_hook():
                t.install_hook(cmd)
    except Exception:
        pass


CHECK_EVERY = 300       # seconds between settings.json checks made from the hooks themselves


def verify_soon(every=CHECK_EVERY):
    """verify(), but at most once per `every` seconds across all hook processes (one stat of a stamp file otherwise). Called from the hooks,
    so a hand edit that removed one of our entries is undone by the next tool call or prompt, without anyone running `aihub`. The stamp
    is touched BEFORE the check, so a burst of hook calls runs it once. -> True when it ran. Never raises."""
    try:
        f = paths.p("queue", "hooks.checked")
        try:
            if time.time() - os.path.getmtime(f) < every:
                return False
        except OSError:
            pass
        paths.ensure("queue")
        with open(f, "w"):
            pass
        verify()
        return True
    except Exception:
        return False


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


def _call(stdin_text):
    data = json.loads(stdin_text or "{}")
    return str(data.get("tool_name") or data.get("tool") or ""), data.get("tool_input") or {}, str(data.get("cwd") or "")


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s or "").lower()).strip("_")


def _mcp_comp(tool, comps):
    """Map `mcp__<server>__<tool>` to an installed `mcp:<name>` component, or None. Claude Code rewrites server names (`.`, `-`, spaces
    -> `_`) and prefixes plugin / connector servers (`plugin_<plugin>_<server>`, `claude_ai_<server>`), so compare normalized and accept
    a prefix. The longest match wins (`datalake` must not take `datalake-mcp` calls)."""
    parts = tool.split("__")
    srv = _norm(parts[1]) if len(parts) > 2 else ""
    best = None
    for c in comps:
        if not c.startswith("mcp:") or not srv:
            continue
        n = _norm(c[4:])
        if n and (srv == n or srv.endswith("_" + n)) and (best is None or len(n) > len(_norm(best[4:]))):
            best = c
    return best


def _emit(d):
    from . import policy
    sys.stdout.write(json.dumps(policy.hook_output(d)))
    sys.stdout.flush()


def _guard(stdin_text):
    """aihub-policy rules: answer deny/ask to the agent on stdout (Claude Code PreToolUse JSON). -> True when it answered. Never raises."""
    try:
        from . import policy
        tool, inp, _ = _call(stdin_text)
        d = policy.decide(tool, inp)
        if d:
            policy.log(tool, inp, d)
            _emit(d)
        return bool(d)
    except Exception:
        return False


def _judge(stdin_text):
    """Slow path (a model call, seconds): only after usage was spooled and only for calls no rule matched."""
    try:
        from . import policy
        tool, inp, cwd = _call(stdin_text)
        d = policy.judge(tool, inp, cwd)
        if d:
            policy.log(tool, inp, d)
            _emit(d)
    except Exception:
        pass


def _is_stop(stdin_text):
    if '"Stop"' not in (stdin_text or ""):
        return False
    try:
        return json.loads(stdin_text).get("hook_event_name") == "Stop"
    except Exception:
        return False


def handle(stdin_text, source="claude"):
    if os.environ.get("AIHUB_JUDGE"):         # inside the judge's own claude session: no guard, no usage event
        return
    if _is_stop(stdin_text):                  # end of an answer turn: queue the usage upload (background, batched), nothing else
        try:
            from . import usagesync
            usagesync.kick()
        except Exception:
            pass
        verify_soon()
        return
    hit = _guard(stdin_text)
    _usage(stdin_text, source)
    if not hit:
        _judge(stdin_text)
    verify_soon()                             # after the answer to the agent: the check never delays a guard decision


def _usage(stdin_text, source="claude"):
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
            c = _mcp_comp(tool, comps)
            if c:
                hit = (c, tool)
        else:  # OpenCode names MCP tools <server>_<tool>
            for c in comps:
                if c.startswith("mcp:") and _norm(tool).startswith(_norm(c[4:]) + "_"):
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
