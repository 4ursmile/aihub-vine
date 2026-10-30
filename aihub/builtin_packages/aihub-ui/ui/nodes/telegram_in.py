#!/usr/bin/env python3
"""Start: Telegram message. Long-polls the Bot API (getUpdates): outbound HTTPS only, no tunnel and no open port.
aihub-ui writes ONE line of JSON to stdin ({"config": {...}}); every taken message is printed as {"event": ...}. Messages sent while the node was off are skipped."""
import json
import re
import sys
import time
import urllib.error
import urllib.request

cfg = json.loads(sys.stdin.readline())["config"]
TOKEN = str(cfg.get("bot_token") or "").strip()
if not re.match(r"^\d+:[\w-]{20,}$", TOKEN):
    sys.stderr.write("bot token missing or malformed (ask @BotFather)\n")
    sys.exit(2)
chats = {c.strip() for c in str(cfg.get("chat_ids") or "").replace(",", " ").split() if c.strip()}
user_rx = re.compile(cfg["from_regex"], re.I) if cfg.get("from_regex") else None


def api(method, **p):
    req = urllib.request.Request("https://api.telegram.org/bot%s/%s" % (TOKEN, method), data=json.dumps(p).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=int(p.get("timeout", 0)) + 15) as r:
        return json.load(r).get("result")


def log(msg):
    sys.stderr.write(str(msg).replace(TOKEN, "***") + "\n")
    sys.stderr.flush()


offset, wait = None, 2
try:
    last = api("getUpdates", offset=-1, timeout=0)
    offset = last[-1]["update_id"] + 1 if last else None
except (urllib.error.URLError, OSError, ValueError) as e:
    log("first call failed: %s" % e)
while True:
    try:
        for u in api("getUpdates", offset=offset, timeout=25, allowed_updates=["message", "channel_post"]) or []:
            offset = u["update_id"] + 1
            m = u.get("message") or u.get("channel_post") or {}
            text = str(m.get("text") or m.get("caption") or "").strip()
            who = m.get("from") or {}
            name = who.get("username") or " ".join(x for x in (who.get("first_name"), who.get("last_name")) if x) or str((m.get("sender_chat") or {}).get("title") or "")
            chat = m.get("chat") or {}
            if not text or (chats and str(chat.get("id")) not in chats) or (user_rx and not user_rx.search(name)) or (who.get("is_bot") and not cfg.get("include_bots")):
                continue
            print(json.dumps({"event": {"text": text, "data": {"from": name, "chat_id": str(chat.get("id", "")), "chat": chat.get("title") or chat.get("username") or name,
                  "message_id": str(m.get("message_id", ""))}}}, ensure_ascii=False), flush=True)
        wait = 2
    except urllib.error.HTTPError as e:
        log("Telegram answered %d%s" % (e.code, " (another program polls this bot, or a webhook is set)" if e.code == 409 else ""))
        time.sleep(wait)
        wait = min(wait * 2, 60)
    except (urllib.error.URLError, OSError, ValueError) as e:
        log("poll failed: %s" % e)
        time.sleep(wait)
        wait = min(wait * 2, 60)
