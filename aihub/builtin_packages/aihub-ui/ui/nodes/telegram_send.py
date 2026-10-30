#!/usr/bin/env python3
"""Send to Telegram: sendMessage through the Bot API. JSON on stdin, JSON on stdout."""
import json
import urllib.error
import urllib.request

import _hook

d, cfg = _hook.start()
token = str(cfg.get("bot_token") or "").strip()
chat = str(cfg.get("chat_id") or "").strip()
text = str(cfg.get("text") or d.get("input") or "").strip()
if not token or not chat or not text:
    _hook.done(ok=False, error="bot token, chat id and message are all needed")
body = {"chat_id": chat, "text": text[:4096], "disable_web_page_preview": True}
if cfg.get("format") in ("MarkdownV2", "HTML"):
    body["parse_mode"] = cfg["format"]
if str(cfg.get("reply_to") or "").strip().isdigit():
    body["reply_to_message_id"] = int(cfg["reply_to"])
req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % token, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.load(r)
except urllib.error.HTTPError as e:
    _hook.done(ok=False, error="Telegram answered %d: %s" % (e.code, e.read(300).decode("utf-8", "replace").replace(token, "***")))
except (urllib.error.URLError, OSError, ValueError) as e:
    _hook.done(ok=False, error="cannot reach Telegram: %s" % str(getattr(e, "reason", e)).replace(token, "***"))
_hook.done(ok=True, output=text, message_id=str((res.get("result") or {}).get("message_id", "")), log="Telegram accepted the message")
