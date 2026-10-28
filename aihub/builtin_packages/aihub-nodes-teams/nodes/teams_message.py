#!/usr/bin/env python3
"""Trigger node: a long-lived process. aihub-ui writes ONE line of JSON to stdin ({"config": {...}, "run": {...}}) and then reads our stdout:
every line {"event": {"text": ..., "data": {...}}} starts a run (after the generic `filter` regex of the trigger). Logs go to stderr (ignored)."""
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

cfg = json.loads(sys.stdin.readline())["config"]
sender_rx = re.compile(cfg.get("from_regex") or "", re.I) if cfg.get("from_regex") else None
only_chat = cfg.get("chat") or ""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        if self.path != "/message":
            return self.send_error(404)
        try:
            m = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 65536)))
            text, who, chat = str(m["text"]), str(m.get("from", "")), str(m.get("chat", ""))
        except (ValueError, KeyError, TypeError):
            return self.send_error(400)
        taken = (not sender_rx or sender_rx.search(who)) and (not only_chat or chat == only_chat)
        if taken:
            print(json.dumps({"event": {"text": text, "data": {"from": who, "chat": chat}}}), flush=True)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"taken": %s}' % (b"true" if taken else b"false"))


# 127.0.0.1 only: this port accepts messages from your own machine, nothing else
ThreadingHTTPServer(("127.0.0.1", int(cfg.get("port") or 8791)), H).serve_forever()
