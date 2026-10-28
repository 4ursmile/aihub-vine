#!/usr/bin/env python3
"""Send to Teams: POSTs a message to a Teams Workflows URL ("When a Teams webhook request is received"). JSON on stdin, JSON on stdout."""
import json
import sys
import urllib.error
import urllib.request

d = json.load(sys.stdin)
cfg = d["config"]
url = str(cfg.get("webhook_url") or "").strip()
text = str(cfg.get("text") or d.get("input") or "").strip()
if not url.startswith("https://"):
    print(json.dumps({"ok": False, "error": "the Workflow URL must start with https://"}))
    sys.exit(0)
if not text:
    print(json.dumps({"ok": False, "error": "nothing to send (text is empty)"}))
    sys.exit(0)
if cfg.get("payload") == "text":
    body = {"text": text}
else:
    blocks = []
    if cfg.get("title"):
        blocks.append({"type": "TextBlock", "text": str(cfg["title"]), "weight": "Bolder", "size": "Medium", "wrap": True})
    blocks.append({"type": "TextBlock", "text": text[:20000], "wrap": True})
    body = {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None,
            "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4", "body": blocks}}]}
req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        code = r.status
except urllib.error.HTTPError as e:
    print(json.dumps({"ok": False, "error": "Teams answered %d: %s" % (e.code, e.read(300).decode("utf-8", "replace"))}))
    sys.exit(0)
except (urllib.error.URLError, OSError) as e:
    print(json.dumps({"ok": False, "error": "cannot reach the Workflow URL: %s" % getattr(e, "reason", e)}))
    sys.exit(0)
print(json.dumps({"ok": True, "output": text, "log": "Teams accepted the message (HTTP %d)" % code}))
