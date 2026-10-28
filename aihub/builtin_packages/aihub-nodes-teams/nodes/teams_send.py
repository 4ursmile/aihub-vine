#!/usr/bin/env python3
"""Action node: ONE JSON document on stdin ({config, input, inputs, run, workdir}); print ONE JSON line on stdout: {"ok": true, "output": "..."}
or {"ok": false, "error": "..."} (a non-zero exit also fails the node)."""
import json
import sys
import urllib.request

req = json.load(sys.stdin)
cfg = req["config"]
url = cfg.get("webhook_url", "")
if not url.startswith("https://") and not url.startswith("http://127.0.0.1"):
    print(json.dumps({"ok": False, "error": "webhook_url must be https"}))
    sys.exit(0)
text = (cfg.get("text") or "{{input}}").replace("{{input}}", req.get("input", ""))
r = urllib.request.Request(url, data=json.dumps({"text": text}).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
try:
    with urllib.request.urlopen(r, timeout=20) as x:
        print(json.dumps({"ok": True, "output": text, "log": "HTTP %d" % x.status}))
except Exception as e:
    print(json.dumps({"ok": False, "error": "post failed: %s" % e}))
