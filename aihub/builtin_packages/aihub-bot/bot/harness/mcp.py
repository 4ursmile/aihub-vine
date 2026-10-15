"""Minimal MCP client over stdio (newline-delimited JSON-RPC). Servers come from mcp.json: {"mcpServers": {name: {command, args, env}}}."""
import json
import os
import subprocess
import threading

from . import config


class Server:
    def __init__(self, name, spec):
        self.name, self.id, self.tools = name, 0, []
        env = dict(os.environ, **{k: str(v) for k, v in (spec.get("env") or {}).items()})
        self.p = subprocess.Popen([spec["command"]] + list(spec.get("args", [])), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, env=env, text=True, bufsize=1)
        self.lock = threading.Lock()
        self.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "aihub-bot", "version": "1"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.tools = (self.call("tools/list", {}) or {}).get("tools", [])

    def _send(self, msg):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def call(self, method, params):
        with self.lock:
            self.id += 1
            self._send({"jsonrpc": "2.0", "id": self.id, "method": method, "params": params})
            while True:
                line = self.p.stdout.readline()
                if not line:
                    raise RuntimeError("mcp server %s exited" % self.name)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") == self.id:
                    if "error" in msg:
                        raise RuntimeError(msg["error"].get("message", "mcp error"))
                    return msg.get("result")

    def close(self):
        try:
            self.p.terminate()
        except OSError:
            pass


def start_all(log=print):
    """-> [Server]. A server that fails to start is reported and skipped; the bot still runs."""
    try:
        with open(config.MCP_JSON, encoding="utf-8") as f:
            specs = json.load(f).get("mcpServers", {})
    except (OSError, ValueError):
        return []
    out = []
    for n, spec in specs.items():
        try:
            out.append(Server(n, spec))
        except Exception as e:
            log("mcp %s: %s" % (n, e))
    return out
