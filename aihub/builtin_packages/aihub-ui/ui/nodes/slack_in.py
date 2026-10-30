#!/usr/bin/env python3
"""Start: Slack message. Slack (Events API and slash commands) POSTs to this listener; every taken message is printed as {"event": ...}.
aihub-ui writes ONE line of JSON to stdin ({"config": {...}}). The listener binds 127.0.0.1 only: expose just this port with a tunnel
(cloudflared / ngrok / Dev Tunnels). Every request must carry a valid Slack signature (signing secret), so a stranger cannot start a run."""
import hashlib
import hmac
import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

cfg = json.loads(sys.stdin.readline())["config"]
SECRET = str(cfg.get("signing_secret") or "")
if len(SECRET) < 8:
    sys.stderr.write("signing secret missing (Slack app > Basic Information > Signing Secret)\n")
    sys.exit(2)
user_rx = re.compile(cfg["from_regex"], re.I) if cfg.get("from_regex") else None
only_ch = {c.strip() for c in str(cfg.get("channel") or "").replace(",", " ").split() if c.strip()}
BOTS = bool(cfg.get("include_bots"))
seen = []                                                     # Slack retries a request when we are slow: one event id counts once


def take(ev):
    return ev and (not only_ch or ev["data"]["channel"] in only_ch) and (not user_rx or user_rx.search(ev["data"]["from"]))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, code, obj=None, raw=None):
        b = raw if raw is not None else json.dumps(obj if obj is not None else {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain" if raw is not None else "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/slack/events", "/slack/command"):
            return self._reply(404, {"error": "use /slack/events or /slack/command"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 <= n <= 262144:
            return self._reply(400, {"error": "bad Content-Length"})
        body = self.rfile.read(n)
        ts = self.headers.get("X-Slack-Request-Timestamp") or ""
        want = "v0=" + hmac.new(SECRET.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
        try:
            fresh = abs(time.time() - int(ts)) < 300
        except ValueError:
            fresh = False
        if not fresh or not hmac.compare_digest(want, self.headers.get("X-Slack-Signature") or ""):
            return self._reply(403, {"error": "bad signature"})
        if path == "/slack/command":
            f = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
            ev = {"text": (f.get("text") or "").strip(), "data": {"from": f.get("user_id", ""), "user_name": f.get("user_name", ""), "channel": f.get("channel_id", ""),
                  "command": f.get("command", ""), "response_url": f.get("response_url", ""), "ts": "", "thread_ts": ""}}
            ok = take(ev)
            if ok:
                print(json.dumps({"event": ev}, ensure_ascii=False), flush=True)
            return self._reply(200, raw=b"Got it, working on it." if ok else b"Ignored.")
        try:
            m = json.loads(body)
        except ValueError:
            return self._reply(400, {"error": "body must be JSON"})
        if m.get("type") == "url_verification":                 # Slack checks the URL once when you save it
            return self._reply(200, raw=str(m.get("challenge") or "").encode())
        ev = m.get("event") or {}
        eid = m.get("event_id")
        if self.headers.get("X-Slack-Retry-Num") or eid in seen:
            return self._reply(200, {"taken": False, "why": "retry"})
        if eid:
            seen.append(eid)
            del seen[:-200]
        ok = (ev.get("type") in ("message", "app_mention") and not (ev.get("subtype") and ev.get("subtype") != "thread_broadcast")
              and (BOTS or not ev.get("bot_id")) and str(ev.get("text") or "").strip())
        out = {"text": str(ev.get("text") or "").strip(), "data": {"from": ev.get("user") or "", "channel": ev.get("channel") or "", "ts": ev.get("ts") or "",
               "thread_ts": ev.get("thread_ts") or ev.get("ts") or "", "team": m.get("team_id") or "", "type": ev.get("type") or ""}} if ok else None
        ok = bool(ok and take(out))
        if ok:
            print(json.dumps({"event": out}, ensure_ascii=False), flush=True)
        self._reply(200, {"taken": ok})


ThreadingHTTPServer(("127.0.0.1", int(cfg.get("port") or 8792)), H).serve_forever()
