#!/usr/bin/env python3
"""Send to Discord: POSTs a message to a channel webhook URL. JSON on stdin, JSON on stdout."""
import _hook

d, cfg = _hook.start()
url = str(cfg.get("webhook_url") or "").strip()
text = str(cfg.get("text") or d.get("input") or "").strip()
if not text:
    _hook.done(ok=False, error="nothing to send (text is empty)")
body = {"allowed_mentions": {"parse": []}}                  # a pasted @everyone must not ping anyone
if str(cfg.get("username") or "").strip():
    body["username"] = str(cfg["username"]).strip()[:80]
if str(cfg.get("title") or "").strip():                     # an embed holds 4096 chars, a plain message 2000
    body["embeds"] = [{"title": str(cfg["title"])[:256], "description": text[:4096]}]
else:
    body["content"] = text[:2000]
code, _ = _hook.post(url, body, "Discord")
_hook.done(ok=True, output=text, log="Discord accepted the message (HTTP %d)" % code)
