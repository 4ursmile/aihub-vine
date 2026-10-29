"""A small MCP client (stdio servers only, stdlib) so free chat can use the MCP servers that aihub packages install.

Servers come from: installed aihub packages that declare `[[mcp_servers]]` (and are enabled), the UI's own list (MCP servers page) and the user-level
servers in ~/.claude.json. Free chat starts only the ones the user ticked for that chat, for one answer, and stops them afterwards.
Protocol: newline-delimited JSON-RPC 2.0 on the child's stdin/stdout (`initialize`, `notifications/initialized`, `tools/list`, `tools/call`)."""
import json
import os
import re
import subprocess
import threading

from . import aihub as A, store

PROTOCOL = "2025-06-18"
START_TIMEOUT = 25
CALL_TIMEOUT = 90
NAME = re.compile(r"[^A-Za-z0-9_-]")


def _toml(path):
    """The [[mcp_servers]] tables of an aihub.toml. tomllib when there is one, else a reader for the simple shapes manifests use."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return []
    try:
        import tomllib
        return [x for x in tomllib.loads(raw.decode("utf-8")).get("mcp_servers", []) if isinstance(x, dict)]
    except ImportError:
        pass
    except Exception:
        return []
    out, cur = [], None
    for ln in raw.decode("utf-8", "replace").splitlines():
        ln = ln.strip()
        if ln.startswith("["):
            cur = {} if ln == "[[mcp_servers]]" else None
            if cur is not None:
                out.append(cur)
            continue
        m = re.match(r"^(\w+)\s*=\s*(.+)$", ln)
        if cur is None or not m:
            continue
        k, v = m.group(1), m.group(2).split(" #")[0].strip()
        try:
            cur[k] = json.loads(v) if v[:1] in '["' else v
        except ValueError:
            if k == "env":
                cur[k] = dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', v))
    return out


def sources():
    """name -> {command, args, env, origin} of every stdio MCP server free chat could use. URL (http/sse) servers are not supported here."""
    out = {}
    try:
        pk = A.run(["list"]).get("data") or []
    except Exception:
        pk = []
    for p in pk:
        if p.get("enabled", True) is False or not any(str(c).startswith("mcp:") for c in p.get("components") or []):
            continue
        for s in _toml(os.path.join(p.get("path") or "", "aihub.toml")):
            if s.get("command") and s.get("name"):
                out.setdefault(str(s["name"]), {"command": str(s["command"]), "args": [str(a).replace("${PKG}", p.get("path") or "") for a in s.get("args", [])],
                                                "env": {str(k): str(v) for k, v in (s.get("env") or {}).items()}, "origin": "package " + p["name"]})
    for n, v in (store.ui_mcp().get("mcpServers") or {}).items():
        if isinstance(v, dict) and v.get("command"):
            out.setdefault(n, {"command": v["command"], "args": [str(a) for a in v.get("args", [])], "env": {str(k): str(x) for k, x in (v.get("env") or {}).items()}, "origin": "UI list"})
    try:
        with open(os.path.join(os.path.expanduser("~"), ".claude.json"), encoding="utf-8") as f:
            for n, v in (json.load(f).get("mcpServers") or {}).items():
                if isinstance(v, dict) and v.get("command") and v.get("type", "stdio") == "stdio":
                    out.setdefault(n, {"command": v["command"], "args": [str(a) for a in v.get("args", [])], "env": {str(k): str(x) for k, x in (v.get("env") or {}).items()}, "origin": "Claude Code"})
    except (OSError, ValueError, AttributeError):
        pass
    return out


def public():
    st = store.mcp_state()
    return [{"name": n, "origin": s["origin"], "command": s["command"], "off": n in st["off"], "default": n in st["default_on"], "tools_off": st["tools_off"].get(n, [])}
            for n, s in sorted(sources().items())]


class Server:
    def __init__(self, name, spec):
        self.name, self.spec = name, spec
        self.p = None
        self.id = 0
        self.wait = {}
        self.lock = threading.Lock()
        self.err = []
        self.tools = []

    def start(self):
        env = dict(os.environ)
        env.update(self.spec.get("env") or {})
        try:
            self.p = subprocess.Popen([self.spec["command"]] + list(self.spec.get("args") or []), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      env=env, cwd=os.path.expanduser("~"), start_new_session=True)
        except OSError as e:
            raise RuntimeError("cannot start %s: %s" % (self.spec["command"], e.strerror or e))
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=lambda: [self.err.append(x.decode("utf-8", "replace")) or self.err.__delitem__(slice(0, -20)) for x in self.p.stderr], daemon=True).start()
        self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "aihub-ui", "version": "1"}}, START_TIMEOUT)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        cursor = None
        for _ in range(10):
            r = self.request("tools/list", {"cursor": cursor} if cursor else {}, START_TIMEOUT)
            self.tools += [t for t in r.get("tools", []) if isinstance(t, dict) and t.get("name")]
            cursor = r.get("nextCursor")
            if not cursor:
                break

    def _send(self, msg):
        try:
            self.p.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
            self.p.stdin.flush()
        except (OSError, ValueError):
            pass

    def _read(self):
        for raw in self.p.stdout:
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(m, dict):
                continue
            if "method" in m and "id" in m:                                     # a request from the server
                self._send({"jsonrpc": "2.0", "id": m["id"], **({"result": {}} if m["method"] == "ping" else {"error": {"code": -32601, "message": "not supported by this client"}})})
            elif "id" in m and m["id"] in self.wait:
                ev, box = self.wait[m["id"]]
                box.append(m)
                ev.set()
        for ev, box in list(self.wait.values()):                                 # the server ended: wake everyone
            if not box:
                box.append({"error": {"message": "the MCP server ended: " + " ".join(self.err)[-300:]}})
            ev.set()

    def request(self, method, params, timeout):
        with self.lock:
            self.id += 1
            rid = self.id
        ev, box = threading.Event(), []
        self.wait[rid] = (ev, box)
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not ev.wait(timeout):
            self.wait.pop(rid, None)
            raise RuntimeError("%s did not answer %s within %ds" % (self.name, method, timeout))
        self.wait.pop(rid, None)
        m = box[0]
        if "error" in m:
            raise RuntimeError(str((m["error"] or {}).get("message") or m["error"])[:300])
        return m.get("result") or {}

    def close(self):
        if self.p and self.p.poll() is None:
            try:
                self.p.stdin.close()
                self.p.terminate()
            except OSError:
                pass


META = "aihub_mcp"
META_SPEC = {"name": META, "input_schema": {"type": "object", "required": ["action"], "properties": {
    "action": {"type": "string", "enum": ["status", "retry", "enable", "disable"]},
    "server": {"type": "string", "description": "MCP server name"},
    "tool": {"type": "string", "description": "optional: one tool of that server (enable / disable only that tool)"}}},
    "description": "Manage the MCP servers and tools of THIS chat. status = which servers run, which failed and why, which tools are off. retry = start a server again (after a failure). "
                   "disable / enable = switch a whole server, or one tool of it (give `tool`), off or on for this chat. Use it when the user asks for any of this, or when a tool failed because its server is down."}


class Toolbox:
    """The tools of the chosen servers, named for the Messages API (`server__tool`, a-z 0-9 _ -, max 64).
    Besides them the model gets `aihub_mcp` (status / retry / enable / disable) so a user can say "retry the github server" or "turn off the delete tool" in the chat.
    OFF = servers never used in free chat (MCP settings); TOOLS_OFF = {server: [tool]} hidden from the model; PERSIST(kind, server, tool, on) saves a change for this chat."""

    def __init__(self, names, off=(), tools_off=None, persist=None):
        self.names = list(dict.fromkeys(names))
        self.had = bool(self.names)                                  # something was switched on: aihub_mcp stays, even when every server is off
        self.off = set(off)
        self.tools_off = {k: set(v) for k, v in (tools_off or {}).items()}
        self.persist = persist
        self.src = sources()
        self.servers, self.errors, self.map, self.specs, self.notes = {}, {}, {}, [], []
        ts = []
        for n in self.names:
            why = self._blocked(n)
            if why:
                self.errors[n] = why
            else:
                ts.append(threading.Thread(target=self._start, args=(n,), daemon=True))
        [t.start() for t in ts]
        [t.join(START_TIMEOUT + 5) for t in ts]
        self.rebuild()
        self.notes = ["MCP server %s: %s (ask me to retry it, or open the MCP page)" % (n, e) for n, e in self.errors.items()]

    def _blocked(self, n):
        if n in self.off:
            return "switched off in the MCP settings"
        if n not in self.src:
            return "not installed or enabled any more"
        return ""

    def _start(self, n):
        old = self.servers.pop(n, None)
        if old:
            old.close()
        s = Server(n, self.src[n])
        try:
            s.start()
            self.servers[n] = s
            self.errors.pop(n, None)
        except Exception as e:
            s.close()
            self.errors[n] = str(e)[:240]

    def rebuild(self):
        self.map, specs = {}, []
        for n in sorted(self.servers):
            s = self.servers[n]
            for t in s.tools:
                if t["name"] in self.tools_off.get(n, ()):
                    continue
                api = NAME.sub("_", "%s__%s" % (n, t["name"]))[:64]
                while api in self.map or api == META:
                    api = api[:60] + "_%d" % len(self.map)
                self.map[api] = (s, t["name"])
                specs.append({"name": api, "description": (str(t.get("description") or "") + " (MCP server %s)" % n)[:1000],
                              "input_schema": t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {"type": "object", "properties": {}}})
        if self.had:
            specs.append(META_SPEC)
        self.specs = specs

    def _meta(self, a):
        act, srv, tool = str(a.get("action") or ""), str(a.get("server") or "").strip(), str(a.get("tool") or "").strip()
        if act == "status" or not srv:
            if act not in ("status", ""):
                return "give `server`", True
            lines = []
            for n in self.names:
                if n in self.servers:
                    ts = [t["name"] for t in self.servers[n].tools]
                    off = [t for t in ts if t in self.tools_off.get(n, ())]
                    lines.append("%s: running, %d tool(s)%s: %s" % (n, len(ts), ", off: " + ", ".join(off) if off else "", ", ".join(ts)[:600]))
                else:
                    lines.append("%s: NOT running - %s" % (n, self.errors.get(n, "unknown")))
            rest = [n for n in self.src if n not in self.names]
            return "\n".join(lines or ["no MCP server is on for this chat"]) + ("\nAvailable but off in this chat: " + ", ".join(rest) if rest else ""), False
        if act not in ("retry", "enable", "disable"):
            return "unknown action " + act, True
        if srv not in self.src and srv not in self.names:
            return "no MCP server called %s (known: %s)" % (srv, ", ".join(sorted(self.src)) or "none"), True
        if tool and act != "retry":
            known = [t["name"] for t in self.servers[srv].tools] if srv in self.servers else []
            if known and tool not in known:
                return "%s has no tool %s (tools: %s)" % (srv, tool, ", ".join(known)), True
            (self.tools_off.setdefault(srv, set()).discard if act == "enable" else self.tools_off.setdefault(srv, set()).add)(tool)
            self._save("tool", srv, tool, act == "enable")
            self.rebuild()
            return "%s tool %s of %s for this chat" % ("enabled" if act == "enable" else "disabled", tool, srv), False
        if act == "disable":
            if srv in self.names:
                self.names.remove(srv)
            old = self.servers.pop(srv, None)
            old and old.close()
            self.errors.pop(srv, None)
            self._save("server", srv, "", False)
            self.rebuild()
            return "server %s is off for this chat" % srv, False
        why = self._blocked(srv)
        if why:
            return "cannot start %s: %s" % (srv, why), True
        if srv not in self.names:
            self.names.append(srv)
            self._save("server", srv, "", True)
        self._start(srv)
        self.rebuild()
        if srv in self.servers:
            return "%s is running with %d tool(s)" % (srv, len(self.servers[srv].tools)), False
        return "%s still fails: %s" % (srv, self.errors.get(srv, "unknown error")), True

    def _save(self, kind, srv, tool, on):
        if self.persist:
            try:
                self.persist(kind, srv, tool, on)
            except Exception:
                pass

    def call(self, api, args):
        """-> (text, is_error). Tool output is returned as data for the model; images and other content are named, not passed."""
        if api == META:
            return self._meta(args if isinstance(args, dict) else {})
        s, name = self.map.get(api, (None, None))
        if not s:
            return "unknown tool " + api, True
        try:
            r = s.request("tools/call", {"name": name, "arguments": args if isinstance(args, dict) else {}}, CALL_TIMEOUT)
        except RuntimeError as e:
            return "%s (if the server died, call aihub_mcp with action retry and server %s)" % (e, s.name), True
        parts = []
        for c in r.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(str(c.get("text", "")))
            elif isinstance(c, dict):
                parts.append("[%s content not shown]" % c.get("type", "other"))
        if not parts and r.get("structuredContent") is not None:
            parts.append(json.dumps(r["structuredContent"], ensure_ascii=False))
        return "\n".join(parts)[:20000], bool(r.get("isError"))

    def close(self):
        for s in list(self.servers.values()):
            s.close()


def probe(name):
    """Start one server, list its tools, stop it. -> {"ok", "error", "tools": [{name, description}]} (the MCP page's Test button)."""
    spec = sources().get(name)
    if not spec:
        return {"ok": False, "error": "not installed or enabled", "tools": []}
    s = Server(name, spec)
    try:
        s.start()
        return {"ok": True, "error": "", "tools": [{"name": t["name"], "description": str(t.get("description") or "")[:200]} for t in s.tools]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300], "tools": []}
    finally:
        s.close()
