"""A small MCP client (stdio servers only, stdlib) so free chat can use the MCP servers that aihub packages install.

Servers come from: installed aihub packages that declare `[[mcp_servers]]` (and are enabled), the UI's own list (MCP servers page) and the user-level
servers in ~/.claude.json. Free chat starts only the ones the user ticked for that chat, for one answer, and stops them afterwards.
Protocol: newline-delimited JSON-RPC 2.0 on the child's stdin/stdout (`initialize`, `notifications/initialized`, `tools/list`, `tools/call`)."""
import hashlib
import json
import os
import re
import signal
import subprocess
import threading
import time

from . import aihub as A, store

PROTOCOL = "2025-06-18"
START_TIMEOUT = 25
CALL_TIMEOUT = 90
IDLE_TTL = 600                                                           # a pooled server nobody used for this long is stopped
SOURCES_TTL = 30
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


_src = {"at": 0.0, "val": {}}
_src_lock = threading.Lock()


def sources(fresh=False):
    """name -> {command, args, env, origin} of every stdio MCP server free chat could use (cached SOURCES_TTL s: it runs `aihub list`). URL (http/sse) servers are not supported here."""
    with _src_lock:
        if not fresh and _src["val"] and time.time() - _src["at"] < SOURCES_TTL:
            return {k: dict(v) for k, v in _src["val"].items()}
    out = _sources()
    with _src_lock:
        _src.update(at=time.time(), val=out)
    return {k: dict(v) for k, v in out.items()}


def _sources():
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
            for n, s in sorted(sources(fresh=True).items())]


class Server:
    def __init__(self, name, spec):
        self.name, self.spec = name, spec
        self.p = None
        self.id = 0
        self.wait = {}
        self.lock = threading.Lock()
        self.err = []
        self.tools = []
        self.dead = False
        self.last = time.time()

    def start(self):
        env = dict(os.environ)
        env.update(self.spec.get("env") or {})
        try:
            self.p = subprocess.Popen([self.spec["command"]] + list(self.spec.get("args") or []), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      env=env, cwd=os.path.expanduser("~"), start_new_session=True)
        except OSError as e:
            raise RuntimeError("cannot start %s: %s" % (self.spec["command"], e.strerror or e))
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain, args=(self.p,), daemon=True).start()
        self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "aihub-ui", "version": "1"}}, START_TIMEOUT)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        cursor = None
        for _ in range(10):
            r = self.request("tools/list", {"cursor": cursor} if cursor else {}, START_TIMEOUT)
            self.tools += [t for t in r.get("tools", []) if isinstance(t, dict) and t.get("name")]
            cursor = r.get("nextCursor")
            if not cursor:
                break

    def _drain(self, p):
        try:
            for x in p.stderr:
                self.err.append(x.decode("utf-8", "replace"))
                del self.err[:-20]
        except (OSError, ValueError):
            pass

    def alive(self):
        return not self.dead and self.p is not None and self.p.poll() is None

    def _send(self, msg):
        try:
            self.p.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
            self.p.stdin.flush()
        except (OSError, ValueError):
            pass

    def _read(self):
        try:
            for raw in self.p.stdout:
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(m, dict):
                    continue
                if "method" in m and "id" in m:                                     # a request from the server
                    self._send({"jsonrpc": "2.0", "id": m["id"], **({"result": {}} if m["method"] == "ping" else {"error": {"code": -32601, "message": "not supported by this client"}})})
                elif "id" in m:
                    with self.lock:
                        w = self.wait.get(m["id"])
                    if w:
                        w[1].append(m)
                        w[0].set()
        except (OSError, ValueError):
            pass
        with self.lock:                                                         # the server ended: later requests fail at once, current ones wake now
            self.dead = True
            waiting = list(self.wait.values())
        for ev, box in waiting:
            if not box:
                box.append({"error": {"message": "the MCP server ended: " + " ".join(self.err)[-300:]}})
            ev.set()

    def request(self, method, params, timeout, cancel=None):
        """cancel: an Event (Stop); the wait ends within 0.25 s of it being set."""
        ev, box = threading.Event(), []
        with self.lock:
            if self.dead or (self.p is not None and self.p.poll() is not None):
                self.dead = True
                raise RuntimeError("the MCP server %s is not running: %s" % (self.name, " ".join(self.err)[-300:] or "it ended"))
            self.id += 1
            rid = self.id
            self.wait[rid] = (ev, box)
        self.last = time.time()
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        end, done = time.time() + timeout, False
        while not done:
            left = end - time.time()
            if left <= 0:
                break
            done = ev.wait(min(0.25, left))
            if not done and cancel is not None and cancel.is_set():
                with self.lock:
                    self.wait.pop(rid, None)
                raise RuntimeError("stopped")
        with self.lock:
            self.wait.pop(rid, None)
        if not done:
            raise RuntimeError("%s did not answer %s within %ds" % (self.name, method, timeout))
        self.last = time.time()
        m = box[0]
        if "error" in m:
            raise RuntimeError(str((m["error"] or {}).get("message") or m["error"])[:300])
        return m.get("result") or {}

    def close(self):
        """Stop the server and everything it started: SIGTERM to its process group, 3 s grace, then SIGKILL (Windows: taskkill /T /F)."""
        self.dead = True
        p = self.p
        if not p:
            return
        try:
            p.stdin.close()
        except (OSError, ValueError):
            pass
        if os.name == "nt":
            if p.poll() is None:
                try:
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True, timeout=10)
                except (OSError, subprocess.SubprocessError):
                    pass
            return
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            p.wait(3)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(p.pid, signal.SIGKILL)                                    # the leader may be gone while children live on
        except OSError:
            pass
        try:
            p.wait(2)
        except subprocess.TimeoutExpired:
            pass


# ---- pool: one running server per (name, spec), shared by every free-chat message until idle for IDLE_TTL

_pool, _plocks = {}, {}
_pool_lock = threading.Lock()
_reaper = {"t": None}


def _key(name, spec):
    return (name, hashlib.sha1(json.dumps(spec, sort_keys=True, default=str).encode("utf-8")).hexdigest())


def _sweep(now=None):
    now = now or time.time()
    with _pool_lock:
        gone = [(k, s) for k, s in _pool.items() if not s.alive() or now - s.last > IDLE_TTL]
        for k, _ in gone:
            _pool.pop(k, None)
    for _, s in gone:
        threading.Thread(target=s.close, daemon=True).start()


def _reap():
    while True:
        time.sleep(30)
        _sweep()


def acquire(name, spec, force=False):
    """A started Server for (name, spec): the pooled one when it still runs, else a new one (a dead one is replaced). Raises RuntimeError when it cannot start."""
    k = _key(name, spec)
    with _pool_lock:
        lk = _plocks.setdefault(k, threading.Lock())
        if _reaper["t"] is None:
            _reaper["t"] = threading.Thread(target=_reap, daemon=True)
            _reaper["t"].start()
    _sweep()
    with lk:
        with _pool_lock:
            s = _pool.get(k)
        if s and s.alive() and not force:
            s.last = time.time()
            return s
        if s:
            with _pool_lock:
                _pool.pop(k, None)
            s.close()
        s = Server(name, spec)
        try:
            s.start()
        except Exception:
            s.close()
            raise
        with _pool_lock:
            _pool[k] = s
        return s


def shutdown():
    """Stop every pooled server (tests, process exit)."""
    with _pool_lock:
        old = list(_pool.values())
        _pool.clear()
    for s in old:
        s.close()


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

    def __init__(self, names, off=(), tools_off=None, persist=None, cancel=None):
        self.names = list(dict.fromkeys(names))
        self.had = bool(self.names)                                  # something was switched on: aihub_mcp stays, even when every server is off
        self.off = set(off)
        self.tools_off = {k: set(v) for k, v in (tools_off or {}).items()}
        self.persist = persist
        self.cancel = cancel
        self.closed = False
        self.src = sources()
        self.servers, self.errors, self.map, self.specs, self.notes = {}, {}, {}, [], []
        ts = []
        for n in self.names:
            why = self._blocked(n)
            if why:
                self.errors[n] = why
            elif self._stopped():
                self.errors[n] = "stopped"
            else:
                ts.append(threading.Thread(target=self._start, args=(n,), daemon=True))
                ts[-1].start()
        deadline = time.time() + START_TIMEOUT + 5                    # one shared deadline, not one per server
        for t in ts:
            while t.is_alive() and time.time() < deadline and not self._stopped():
                t.join(min(0.2, max(0.0, deadline - time.time())))
        self.closed = self.closed or self._stopped() or any(t.is_alive() for t in ts)
        for n in self.names:
            if n not in self.servers and n not in self.errors:
                self.errors[n] = "stopped" if self._stopped() else "did not start in time"
        self.rebuild()
        self.notes = ["MCP server %s: %s (ask me to retry it, or open the MCP page)" % (n, e) for n, e in self.errors.items()]

    def _stopped(self):
        return self.cancel is not None and self.cancel.is_set()

    def _blocked(self, n):
        if n in self.off:
            return "switched off in the MCP settings"
        if n not in self.src:
            return "not installed or enabled any more"
        return ""

    def _start(self, n, force=False):
        self.servers.pop(n, None)                                    # pooled: not stopped here, the pool reaps it when idle (or replaces it on force)
        try:
            s = acquire(n, self.src[n], force=force)
        except Exception as e:
            self.errors[n] = str(e)[:240]
            return
        if self.closed:                                              # the toolbox was closed while this was starting: do not hand the server to nobody
            return
        self.servers[n] = s
        self.errors.pop(n, None)

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
            warn = self._save("tool", srv, tool, act == "enable")
            self.rebuild()
            return "%s tool %s of %s for this chat%s" % ("enabled" if act == "enable" else "disabled", tool, srv, warn), False
        if act == "disable":
            if srv in self.names:
                self.names.remove(srv)
            self.servers.pop(srv, None)
            self.errors.pop(srv, None)
            warn = self._save("server", srv, "", False)
            self.rebuild()
            return "server %s is off for this chat%s" % (srv, warn), False
        why = self._blocked(srv)
        if why:
            return "cannot start %s: %s" % (srv, why), True
        warn = ""
        if srv not in self.names:
            self.names.append(srv)
            warn = self._save("server", srv, "", True)
        self._start(srv, force=True)
        self.rebuild()
        if srv in self.servers:
            return "%s is running with %d tool(s)%s" % (srv, len(self.servers[srv].tools), warn), False
        return "%s still fails: %s" % (srv, self.errors.get(srv, "unknown error")), True

    def _save(self, kind, srv, tool, on):
        """-> "" or a warning to add to the tool result when the change could not be kept for later messages."""
        if self.persist:
            try:
                self.persist(kind, srv, tool, on)
            except Exception as e:
                return " (warning: this change applies now but could not be saved for the chat: %s)" % str(e)[:150]
        return ""

    def call(self, api, args):
        """-> (text, is_error). Tool output is returned as data for the model; images and other content are named, not passed."""
        if api == META:
            return self._meta(args if isinstance(args, dict) else {})
        s, name = self.map.get(api, (None, None))
        if not s:
            return "unknown tool " + api, True
        try:
            r = s.request("tools/call", {"name": name, "arguments": args if isinstance(args, dict) else {}}, CALL_TIMEOUT, self.cancel)
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
        """Release the servers: they stay in the pool for the next message and are stopped when idle (IDLE_TTL)."""
        self.closed = True
        self.servers = {}


def probe(name):
    """Start one server, list its tools, stop it. -> {"ok", "error", "tools": [{name, description}]} (the MCP page's Test button)."""
    spec = sources(fresh=True).get(name)
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
