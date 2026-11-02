"""`aihub pubsub`: topics on the hub (dotted names with sub topics), publish and subscribe with wildcards and a regex on the message.
Stdlib only, Python 3.9. Subscribing is a long poll against /api/v1/pubsub/messages; a durable cursor (--durable NAME) keeps the position on
the hub (acknowledged after each handled message), and with --exec a message counts as handled only when the command succeeds (at least
once). The hub filters on subjects only: --match (a regex on the message text) runs HERE, on this machine."""
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

from . import api, paths, ui

ACCESS = ("consume", "produce", "both")
WAIT = 25
PAGE = 1000                                                  # messages asked for per poll
PUB_BATCH = 500                                              # messages per publish call (the hub's maximum)


def _q(name):
    return urllib.parse.quote(name, safe="")


def link(name):
    return "%s/#/t/%s" % (paths.config()["hub"].rstrip("/"), name)


def _when(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t or 0)) if t else "-"


def _size(n):
    return "%d B" % n if n < 1024 else "%.1f KB" % (n / 1024.0) if n < 1 << 20 else "%.1f MB" % (n / 1048576.0)


def _fields(a, body):
    for k, attr in (("title", "title"), ("description", "description"), ("tags", "tags"), ("retention_hours", "retention"), ("max_messages", "max")):
        v = getattr(a, attr, None)
        if v not in (None, ""):
            body[k] = v
    if a.private and a.public:
        raise ValueError("choose --public or --private, not both")
    if a.private or a.public:
        body["visibility"] = "private" if a.private else "public"
    if getattr(a, "open_publish", None) is not None:
        body["public_access"] = "produce" if a.open_publish else "consume"
    return body


def _row(t):
    return [("  " * t["depth"]) + ui.bold(t["name"]), t["visibility"] + (" +pub" if t["visibility"] == "public" and t["public_access"] == "produce" else ""),
            t["owner"], str(t.get("pub_24h", 0)), str(t["msgs"]), _when(t["last_ts"]) if t["last_ts"] else "idle", t["title"]]


def _list(a):
    q = {"limit": a.limit, "sort": "name"}
    if a.target:
        q["q"] = a.target
    if a.mine:
        q["scope"] = "mine"
    elif a.shared:
        q["scope"] = "shared"
    res = api.call("GET", "/topics?" + urllib.parse.urlencode(q))
    ui.result(res)
    ui.out()
    if not res["items"]:
        ui.info("no topics found" + ("" if a.target or a.mine or a.shared else ": create one with `aihub pubsub create NAME`"))
        return
    ui.table(["TOPIC", "WHO", "OWNER", "24H", "TOTAL", "LAST", "TITLE"], [_row(t) for t in res["items"]])
    if res["total"] > len(res["items"]):
        ui.info("%d of %d shown (--limit)" % (len(res["items"]), res["total"]))


def _info(name):
    d = api.call("GET", "/topics/" + _q(name))
    ui.result(d)
    ui.out()
    ui.out("%s  %s" % (ui.bold(d["name"]), d["title"]))
    for k in ("visibility", "public_access", "owner", "level", "msgs", "pub_24h", "description"):
        ui.out("  %-16s %s" % (k, d.get(k)))
    ui.out("  %-16s %s h%s" % ("retention", d["effective_retention_hours"], "" if d["retention_hours"] else " (hub default)"))
    ui.out("  %-16s %s%s" % ("max messages", d["effective_max_messages"], "" if d["max_messages"] else " (hub default)"))
    ui.out("  %-16s %s" % ("last message", _when(d["last_ts"])))
    if d["parents"]:
        ui.out("  %-16s %s" % ("under", " > ".join(d["parents"])))
    for c in d["children"]:
        ui.out("  sub topic        %s (%s)" % (c["name"], c["visibility"]))
    ui.out("  %-16s %s" % ("link", link(d["name"])))
    if d.get("can_manage"):
        acc = api.call("GET", "/topics/%s/access" % _q(name))
        for x in acc["access"]:
            ui.out("  shared           %s %s: %s" % (x["type"], x["name"], x["access"]))
        for x in acc["inherited"]:
            ui.out("  shared (from %s) %s %s: %s" % (x["topic"], x["type"], x["name"], x["access"]))


def _data(text, as_text):
    if as_text:
        return text
    try:
        return json.loads(text)
    except ValueError:
        return text


def _headers(items):
    out = {}
    for h in items or []:
        if "=" not in h:
            raise ValueError("--header KEY=VALUE")
        k, v = h.split("=", 1)
        out[k.strip()] = v
    return out


def text_of(m):
    """The text --match is tested against (and --data-only prints): text as is, JSON as `{"a": 1}`."""
    d = m.get("data")
    return d if isinstance(d, str) else json.dumps(d, ensure_ascii=False)


def matcher(rx):
    """--match REGEX -> fn(message) -> bool, or None for no filter. Python syntax, searched anywhere in the text."""
    if not rx:
        return None
    try:
        c = re.compile(rx)
    except re.error as e:
        raise ValueError("--match is not a valid regular expression: %s" % e)
    return lambda m: c.search(text_of(m)) is not None


def _send(batch):
    return api._retry(lambda: api.call("POST", "/pubsub/publish", {"messages": batch}, timeout=60))


def _pub(a):
    subject = a.target
    if not subject:
        raise ValueError("aihub pubsub pub SUBJECT [DATA]   (DATA - or nothing = read stdin)")
    base = {"subject": subject}
    if a.key:
        base["key"] = a.key
    hd = _headers(a.header)
    if hd:
        base["headers"] = hd
    if a.lines:                                              # every stdin line is one message, sent in batches
        n, batch, last = 0, [], time.time()
        for line in sys.stdin:
            line = line.rstrip("\r\n")
            if not line:
                continue
            batch.append(dict(base, data=_data(line, a.text)))
            if len(batch) >= PUB_BATCH or time.time() - last > 0.5:
                n += _send(batch)["count"]
                batch, last = [], time.time()
        if batch:
            n += _send(batch)["count"]
        ui.result({"count": n, "subject": subject})
        ui.out()
        ui.good("published %d message%s to %s" % (n, "" if n == 1 else "s", subject))
        return
    if a.extra in (None, "-"):
        if a.extra is None and sys.stdin.isatty():
            raise ValueError("no data: aihub pubsub pub %s 'text or JSON'  (or pipe it in)" % subject)
        text = sys.stdin.read()
        if text.endswith("\n"):                              # `echo x | aihub pubsub pub ...`: the newline echo adds is not part of the message
            text = text[:-2] if text.endswith("\r\n") else text[:-1]
    else:
        text = a.extra
    res = _send([dict(base, data=_data(text, a.text))])
    ui.result(dict(res, subject=subject))
    ui.out()
    ui.good("published #%d to %s" % (res["ids"][0], subject))


def _print(m, fmt):
    if fmt == "jsonl":
        sys.stdout.write(json.dumps(m, ensure_ascii=False) + "\n")
    elif fmt == "data":
        sys.stdout.write(text_of(m) + "\n")
    else:
        body = text_of(m)
        head = "%s %s%s%s" % (ui.dim(time.strftime("%H:%M:%S", time.localtime(m["ts"]))), ui.bold(m["subject"]),
                              (" [%s]" % m["key"]) if m["key"] else "", ui.dim(" #%d %s" % (m["id"], m["by"])))
        sys.stdout.write(head + "  " + body + "\n")
    sys.stdout.flush()


def _run(cmd, m):
    """--exec: the message data on stdin, its fields in the environment. -> True when it exited 0."""
    env = dict(os.environ, AIHUB_SUBJECT=m["subject"], AIHUB_MSG_ID=str(m["id"]), AIHUB_MSG_KEY=m["key"] or "", AIHUB_MSG_BY=m["by"] or "",
               AIHUB_MSG_TS=str(m["ts"]), AIHUB_MSG_HEADERS=json.dumps(m.get("headers") or {}))
    p = subprocess.run(cmd, shell=True, input=text_of(m).encode("utf-8"), env=env)
    return p.returncode == 0


def _sub(a):
    pattern = a.target
    if not pattern:
        raise ValueError("aihub pubsub sub PATTERN   (orders.eu, orders.*.paid, orders.>, orders.#)")
    fmt = "jsonl" if a.jsonl else ("data" if a.data_only else "text")
    keep = matcher(a.match)                                 # client side: the hub sends every message the pattern allows
    q = {"pattern": pattern}
    if a.durable:
        q["cursor"] = a.durable
        q["commit"] = "0"                                   # the position moves by ack, only past what was handled here
    after = a.after
    got, seen = [], 0
    deadline = time.time() + a.timeout if a.timeout else None
    if ui.JSON and not a.count and not a.timeout:
        deadline = time.time() + WAIT                      # one JSON document: wait for one poll at most

    def show(m):
        got.append(m) if ui.JSON else _print(m, fmt)
    if a.tail:
        res = api.call("GET", "/pubsub/messages?" + urllib.parse.urlencode(dict(pattern=pattern, tail=a.tail)), timeout=60)
        after = res["next"]
        for m in res["items"]:
            if not keep or keep(m):
                show(m)
                seen += 1
    elif after is None and a.from_start:
        q["from"] = "start"                                # with --durable: only matters the first time the cursor is used
    if not ui.JSON and fmt == "text" and sys.stderr.isatty():
        sys.stderr.write(ui.dim("  listening on %s%s (Ctrl+C to stop)\n" % (pattern, (" /%s/ (filtered here)" % a.match) if a.match else "")))
    ack, acked, fails = None, None, 0
    try:
        while not (a.count and seen >= a.count):
            left = (deadline - time.time()) if deadline else WAIT
            if left <= 0:
                break
            want = PAGE if keep or not a.count else min(PAGE, max(1, a.count - seen))
            p = dict(q, wait=int(max(1, min(WAIT, left))), limit=want)
            if after is not None and not a.durable:
                p["after"] = after
            elif after is not None and acked is None and ack is None:
                p["after"] = after                         # --durable --after ID: start there once, the cursor takes over
            if ack is not None:
                p["ack"] = acked = ack
            try:
                res = api.call("GET", "/pubsub/messages?" + urllib.parse.urlencode(p), timeout=p["wait"] + 20)
                fails = 0
            except (api.ApiError, OSError) as e:
                code = str(e).split(" ", 1)[0]
                if code in ("400", "401", "403", "404"):
                    raise
                fails += 1
                if not ui.JSON:
                    sys.stderr.write("  hub not reachable (%s), retrying\n" % e)
                time.sleep(min(30, 2 ** min(fails, 5)))
                continue
            items = res["items"]
            after = res["next"]
            for m in items:
                if keep and not keep(m):                   # not for us: handled by skipping it
                    ack = m["id"]
                    continue
                if a.exec_cmd:
                    ok = _run(a.exec_cmd, m)
                    if not ok and a.durable:                 # stop here: the next start delivers this message again
                        raise ValueError("--exec failed on message #%d (%s): stopped; it is delivered again next time" % (m["id"], m["subject"]))
                    if not ok:
                        ui.note("--exec failed on message #%d" % m["id"])
                if ui.JSON or not a.exec_cmd or a.verbose:
                    show(m)
                seen += 1
                ack = m["id"]
                if a.count and seen >= a.count:
                    after = m["id"]                        # `next` = right after the last message handed out
                    break
    except KeyboardInterrupt:
        pass
    finally:
        if a.durable and ack is not None and ack != acked:  # remember what was handled
            try:
                api.call("GET", "/pubsub/messages?" + urllib.parse.urlencode(dict(q, ack=ack, limit=1)), timeout=30)
            except (api.ApiError, OSError):
                pass
    ui.result({"items": got, "next": after})


def _share(a, act):
    name = a.target
    e = _q(name)
    if act == "shares":
        res = api.call("GET", "/topics/%s/access" % e)
        ui.result(res)
        ui.out()
        ui.out("%s is %s%s" % (name, res["visibility"], " (signed-in people may publish)" if res["visibility"] == "public" and res["public_access"] == "produce" else ""))
        for x in res["access"]:
            ui.out("  %-6s %-20s %s" % (x["type"], x["name"], x["access"]))
        for x in res["inherited"]:
            ui.out("  %-6s %-20s %s  (from %s)" % (x["type"], x["name"], x["access"], x["topic"]))
        return
    who = [("user", u) for u in (a.user or [])] + [("group", g) for g in (a.group or [])]
    if not who:
        raise ValueError("name who: --user NAME or --group NAME (repeatable)")
    ui.out()
    if act == "share":
        level = a.access or "consume"
        api.call("PUT", "/topics/%s/access" % e, {"items": [{"type": t, "name": n} for t, n in who], "access": level})
        for t, n in who:
            ui.good("shared %s with %s %s (%s)" % (name, t, n, level))
    else:
        for t, n in who:
            api.call("DELETE", "/topics/%s/access/%s/%s" % (e, t, urllib.parse.quote(n)))
            ui.good("removed %s %s from %s" % (t, n, name))


def _stats(a):
    d = api.call("GET", "/topics/%s/stats?days=%d" % (_q(a.target), a.days))
    ui.result(d)
    ui.out()
    t, p = d["totals"], d["prior"]
    ui.out("%s, last %d day%s (and everything below it)" % (ui.bold(a.target), a.days, "" if a.days == 1 else "s"))
    ui.out("  published   %d (before: %d)" % (t["pub"], p["pub"]))
    ui.out("  delivered   %d (before: %d)" % (t["deliv"], p["deliv"]))
    ui.out("  kept now    %d messages, %s" % (d["stored"]["messages"], _size(d["stored"]["bytes"])))
    if d["subjects"]:
        ui.heading("Subjects")
        ui.table(["SUBJECT", "KEPT"], [[s["name"], str(s["count"])] for s in d["subjects"]])
    if d["clients"]:
        ui.heading("Producers and consumers")
        ui.table(["WHO", "ROLE", "PATTERN", "MESSAGES", "LAST"], [[c["username"] or "(hidden)", c["role"] + (" *" if c["active"] else ""), c["pattern"] or "-",
                                                                   str(c["n"]), _when(c["last"])] for c in d["clients"]])
        ui.info("* = active in the last 10 minutes")


def cli(a):
    act = a.action
    if act in ("list", "topics", "ls"):
        return _list(a)
    if act == "cursors":
        res = api.call("GET", "/pubsub/cursors")
        ui.result(res)
        ui.out()
        if not res["items"]:
            ui.info("no durable cursors (aihub pubsub sub PATTERN --durable NAME)")
        else:
            ui.table(["CURSOR", "PATTERN", "POSITION", "UPDATED"], [[c["name"], c["pattern"], str(c["last_id"]), _when(c["updated"])] for c in res["items"]])
        return
    if not a.target:
        raise ValueError("aihub pubsub %s NAME" % act)
    if act == "pub":
        return _pub(a)
    if act == "sub":
        return _sub(a)
    if act == "cursor-rm":
        api.call("DELETE", "/pubsub/cursors/" + _q(a.target))
        ui.out()
        ui.good("removed cursor " + a.target)
        return
    if act == "create":
        d = api.call("POST", "/topics", _fields(a, {"name": a.target}))
        ui.result(dict(d, url=link(d["name"])))
        ui.out()
        ui.good("created %s (%s%s)" % (ui.bold(d["name"]), d["visibility"], ", sub topic" if d["depth"] else ""))
        ui.info("publish: aihub pubsub pub %s '{\"hello\": 1}'    listen: aihub pubsub sub '%s.#'" % (d["name"], d["name"]))
        ui.info("open: " + link(d["name"]))
        return
    if act == "info":
        return _info(a.target)
    if act == "set":
        body = _fields(a, {})
        if a.owner:
            body["owner"] = a.owner
        if not body:
            raise ValueError("nothing to change: pass --title, --private/--public, --open-publish, --retention, --max, --owner ...")
        d = api.call("PATCH", "/topics/" + _q(a.target), body)
        ui.result(d)
        ui.out()
        ui.good("updated " + a.target)
        return
    if act == "rm":
        if not a.yes and not ui.confirm("Delete %s, its sub topics and all their messages?" % a.target, default=False):
            raise ValueError("not deleted (use --yes to skip the question)")
        d = api.call("DELETE", "/topics/" + _q(a.target))
        ui.out()
        ui.good("deleted %s (%d topic record%s)" % (a.target, d["removed"], "" if d["removed"] == 1 else "s"))
        return
    if act in ("share", "unshare", "shares"):
        return _share(a, act)
    if act == "stats":
        return _stats(a)
    raise ValueError("unknown action " + act)
