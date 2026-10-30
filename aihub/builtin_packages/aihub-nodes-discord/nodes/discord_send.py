#!/usr/bin/env python3
"""Send as Discord bot: REST call (stdlib only). JSON on stdin, JSON on stdout."""
import json
import sys
import urllib.error
import urllib.request

d = json.load(sys.stdin)
cfg = d["config"]


def done(**k):
    print(json.dumps(k, ensure_ascii=False))
    sys.exit(0)


token = str(cfg.get("bot_token") or "").strip()
chan = str(cfg.get("channel_id") or "").strip()
text = str(cfg.get("text") or d.get("input") or "").strip()
if not token or not chan.isdigit() or not text:
    done(ok=False, error="bot token, a numeric channel id and a message are all needed")
body = {"content": text[:2000], "allowed_mentions": {"parse": []}}
if str(cfg.get("reply_to") or "").strip().isdigit():
    body["message_reference"] = {"message_id": cfg["reply_to"].strip(), "fail_if_not_exists": False}
req = urllib.request.Request("https://discord.com/api/v10/channels/%s/messages" % chan, data=json.dumps(body).encode(),
                             headers={"Authorization": "Bot " + token, "Content-Type": "application/json", "User-Agent": "DiscordBot (aihub, 1.0)"})
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.load(r)
except urllib.error.HTTPError as e:
    done(ok=False, error="Discord answered %d: %s" % (e.code, e.read(300).decode("utf-8", "replace").replace(token, "***")))
except (urllib.error.URLError, OSError, ValueError) as e:
    done(ok=False, error="cannot reach Discord: %s" % str(getattr(e, "reason", e)).replace(token, "***"))
done(ok=True, output=text, message_id=str(res.get("id", "")), log="Discord accepted the message")
