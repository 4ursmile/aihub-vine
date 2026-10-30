#!/usr/bin/env python3
"""Send to Slack: POSTs a message to an Incoming Webhook URL. JSON on stdin, JSON on stdout."""
import _hook

d, cfg = _hook.start()
url = str(cfg.get("webhook_url") or "").strip()
text = str(cfg.get("text") or d.get("input") or "").strip()
if not text:
    _hook.done(ok=False, error="nothing to send (text is empty)")
body = {"text": text[:39000]}
for k in ("username", "channel", "icon_emoji"):
    if str(cfg.get(k) or "").strip():
        body[k] = str(cfg[k]).strip()
if str(cfg.get("title") or "").strip():
    body["blocks"] = [{"type": "header", "text": {"type": "plain_text", "text": str(cfg["title"])[:150]}},
                      {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}]
code, _ = _hook.post(url, body, "Slack")
_hook.done(ok=True, output=text, log="Slack accepted the message (HTTP %d)" % code)
