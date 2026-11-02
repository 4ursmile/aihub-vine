#!/usr/bin/env python3
"""Start: Pub/Sub message. Long-polls the hub (outbound HTTPS only, no open port) for messages whose subject matches the pattern, keeps
those whose text matches the optional regex (tested HERE: the hub never runs regexes), and prints each one as {"event": ...}. With 'Catch up' on, a durable cursor on the hub (named after this
work and node) keeps the position, so messages published while the node was off are delivered when it starts again.
aihub-ui writes ONE line of JSON to stdin ({"config": {...}, "run": {"work", "node"}})."""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _hubapi as H  # noqa: E402

first = json.loads(sys.stdin.readline())
cfg, run = first["config"], first.get("run") or {}
pattern = str(cfg.get("pattern") or "").strip().lower()
match = str(cfg.get("match") or "")
if not pattern:
    sys.stderr.write("pattern missing (e.g. orders.# or orders.*.paid)\n")
    sys.exit(2)
try:
    keep = re.compile(match).search if match else None
except re.error as e:
    sys.stderr.write("Message regex is not valid: %s\n" % e)
    sys.exit(2)
cursor = ""
if cfg.get("catch_up", True) not in (False, "false", "0", 0):
    cursor = re.sub(r"[^A-Za-z0-9._-]+", "-", "ui-%s-%s" % (run.get("work") or "work", run.get("node") or "node"))[:64]
RESERVED = {"subject", "key", "id", "by", "ts", "headers", "json"}


def event(m):
    d = m.get("data")
    text = d if isinstance(d, str) else json.dumps(d, ensure_ascii=False)
    data = {"subject": m["subject"], "key": m.get("key") or "", "id": m["id"], "by": m.get("by") or "", "ts": m.get("ts"), "headers": m.get("headers") or {}}
    if not isinstance(d, str):
        data["json"] = d
    if isinstance(d, dict):                                   # top-level fields of a JSON object are {{data.field}} too
        for k, v in d.items():
            if isinstance(k, str) and k not in RESERVED and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
                data[k] = v if isinstance(v, (str, int, float, bool)) or v is None else json.dumps(v, ensure_ascii=False)
    return {"text": text, "data": data}


after, wait = None, 2
start = "start" if cfg.get("from_start") else "latest"
while True:
    try:
        r = H.call("GET", "/pubsub/messages?" + H.qs(pattern=pattern, cursor=cursor, after=after, wait=25, limit=1000,
                                                      **{"from": start}), timeout=60)
        wait = 2
        if not cursor:
            after = r.get("next")
        lines = []
        for m in r.get("items") or []:
            ev = event(m)
            if keep is None or keep(ev["text"]):
                lines.append(json.dumps({"event": ev}, ensure_ascii=False))
        if lines:
            sys.stdout.write("\n".join(lines) + "\n")
            sys.stdout.flush()
    except H.HubError as e:
        sys.stderr.write("%s\n" % e)
        sys.stderr.flush()
        if e.code in (400, 404):                              # a bad pattern, or no such topic: retrying does not help soon
            time.sleep(60)
        else:
            time.sleep(wait)
            wait = min(wait * 2, 60)
