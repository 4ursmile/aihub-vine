#!/usr/bin/env python3
"""Publish to Pub/Sub: sends one message to a subject on the hub with the aihub login of this machine. JSON on stdin, JSON on stdout."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _hubapi as H  # noqa: E402

d = json.load(sys.stdin)
cfg = d["config"]


def done(**k):
    print(json.dumps(k, ensure_ascii=False))
    sys.exit(0)


subject = str(cfg.get("subject") or "").strip().lower()
if not subject:
    done(ok=False, error="subject is empty (e.g. orders.eu.paid)")
text = cfg.get("data")
if text in (None, ""):
    text = d.get("input") or ""
text = str(text)
data = text
if cfg.get("format", "auto") != "text":
    try:
        data = json.loads(text)
    except ValueError:
        if cfg.get("format") == "json":
            done(ok=False, error="the data is not valid JSON (set Format to auto or text to send it as text)")
msg = {"subject": subject, "data": data}
if str(cfg.get("key") or "").strip():
    msg["key"] = str(cfg["key"]).strip()
try:
    r = H.call("POST", "/pubsub/publish", msg)
except H.HubError as e:
    done(ok=False, error=str(e))
mid = (r.get("ids") or [None])[0]
done(ok=True, output="published #%s to %s" % (mid, subject), data={"id": mid, "subject": subject}, log="hub stored message #%s on %s (%d bytes)" % (mid, subject, len(text.encode("utf-8"))))
