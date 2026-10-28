#!/usr/bin/env python3
"""Start: Teams message. A Power Automate flow (Teams "Workflows") POSTs each channel/chat message to this listener.
aihub-ui writes ONE line of JSON to stdin ({"config": {...}}); every line we print as {"event": {...}} starts a run.

The listener binds 127.0.0.1 only. To let Power Automate (a cloud service) reach it, expose JUST this port with a tunnel
(Dev Tunnels / cloudflared / ngrok); the UI itself stays private. Every request must carry the shared secret."""
import hmac
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

cfg = json.loads(sys.stdin.readline())["config"]
SECRET = str(cfg.get("secret") or "")
if len(SECRET) < 12:
    sys.stderr.write("secret too short (12+ characters)\n")
    sys.exit(2)
sender_rx = re.compile(cfg["from_regex"], re.I) if cfg.get("from_regex") else None
only_chat = (cfg.get("chat") or "").strip().lower()
STRIP = cfg.get("strip_html") is not False


def pick(d, *paths):
    for p in paths:
        cur = d
        for k in p.split("."):
            cur = cur.get(k) if isinstance(cur, dict) else None
        if isinstance(cur, (str, int, float)) and str(cur).strip():
            return str(cur)
    return ""


def plain(html):
    t = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    for a, b in (("&nbsp;", " "), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"), ("&amp;", "&")):
        t = t.replace(a, b)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        u = urlsplit(self.path)
        key = self.headers.get("X-Hook-Secret") or (parse_qs(u.query).get("key") or [""])[0]
        if u.path != "/teams" or not hmac.compare_digest(key.encode(), SECRET.encode()):
            return self._reply(403, {"error": "wrong path or secret"})
        try:
            m = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 262144)) or b"{}")
        except ValueError:
            return self._reply(400, {"error": "body must be JSON"})
        if not isinstance(m, dict):
            return self._reply(400, {"error": "body must be a JSON object"})
        text = pick(m, "text", "message", "body.content", "content")
        if STRIP and re.search(r"<\w+[^>]*>", text):
            text = plain(text)
        who = pick(m, "from", "sender", "from.user.displayName", "from.user.email", "user")
        chat = pick(m, "chat", "channel", "chatName", "channelName", "team")
        taken = bool(text) and (not sender_rx or sender_rx.search(who)) and (not only_chat or chat.lower() == only_chat)
        if taken:
            print(json.dumps({"event": {"text": text, "data": {"from": who, "chat": chat, "raw": m}}}, ensure_ascii=False), flush=True)
        self._reply(200, {"taken": taken})


ThreadingHTTPServer(("127.0.0.1", int(cfg.get("port") or 8791)), H).serve_forever()
