"""Pub/sub: lightweight topics with sub topics, wildcards and a regex filter on the message, stored in the hub database.

  * A topic is a dotted name: `orders`, `orders.eu`, `orders.eu.paid` (tokens: a-z 0-9 _ -). Creating a ROOT topic needs the permission
    `create_topics`; a sub topic record needs admin on its parent (the owner of any ancestor is admin). A subject without a record of its own
    is published to the nearest record among its prefixes (`orders.eu.x` -> `orders.eu` or `orders`).
  * Visibility per record: public (everybody may consume; public_access='produce' lets every signed-in user publish too) or private. Shares
    (consume | produce | both, for users and groups) flow down to the sub topics. See pubsub_repo for the rules.
  * Subscribe with a subject PATTERN: `orders.eu` (exact), `orders.*.paid` (`*` = one token), `orders.>` (one or more tokens below),
    `orders.#` (the topic itself and everything below). The hub filters on subjects and permissions only: a regex on the message body is
    the CLIENT's job (`aihub pubsub sub --match`, the aihub-ui node), so no request can make the hub run a user's regex.
  * Delivery is a long poll (`GET /pubsub/messages?wait=25`): stdlib clients (the CLI, the aihub-ui nodes) need nothing else. One pump
    thread per process tails new messages into memory and wakes the waiters (pubsub_feed), so a waiting subscriber costs no queries and a
    message reaches it within milliseconds (other workers' messages within a TICK). A named durable cursor (`cursor=NAME`) keeps the
    position on the hub; `commit=0` + `ack=ID` gives at-least-once processing.
  * Retention: per record `retention_hours` / `max_messages` (0 = the admin numbers pubsub_retention_hours / pubsub_max_messages)."""
import functools
import json
import re
import threading
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from .deps import current_user, require_user, viewer_of
from .pubsub_feed import Engine, answer, encode
from .pubsub_repo import ACCESS, PUBLIC_ACCESS, can, headers_json
from .routers import _avatar_url, _clean

r = APIRouter(prefix="/api/v1")
TOKEN = r"[a-z0-9][a-z0-9_-]{0,63}"
NAME = re.compile(r"^%s(?:\.%s){0,15}$" % (TOKEN, TOKEN))
PTOKEN = re.compile(r"^(?:%s|\*)$" % TOKEN)
RESERVED = ("facets", "overview")              # API paths next to /topics/{name}
WAIT_MAX = 30.0
SCAN = 500                                     # rows read per database round of a fetch
BATCH_MAX = 500                                # messages per publish call
LIMIT_MAX = 2000                               # messages per answer
INLINE = 2000                                  # ring entries a waiter filters on the event loop; more goes to a worker thread


def R(request): return request.app.state.repos


# ---------------------------------------------------------------- delivery engine
_engine_lock = threading.Lock()


def E(request):
    """This process's delivery engine (pubsub_feed.Engine), created on first use."""
    st = request.app.state
    e = getattr(st, "pubsub", None)
    if e is None:
        with _engine_lock:
            e = getattr(st, "pubsub", None)
            if e is None:
                e = st.pubsub = Engine(st.repos, lambda t, sync=st.sync: _eff_sync(sync, t))
    return e


# ---------------------------------------------------------------- names and patterns
def _name(v, what="topic"):
    n = str(v or "").strip().lower()
    if len(n) > 255 or not NAME.match(n):
        raise HTTPException(400, "%s name: dot separated parts of a-z 0-9 _ - (e.g. orders.eu.paid), at most 16 parts" % what)
    return n


@functools.lru_cache(maxsize=4096)
def _pattern(p):
    return compile_pattern(p)


def compile_pattern(p):
    """-> (literal prefix before the first wildcard, compiled regex of the whole pattern). `*` = one token, `>` = one or more tokens (last
    part only), `#` = zero or more tokens (last part only)."""
    p = str(p or "").strip().lower()
    if not p or len(p) > 255:
        raise HTTPException(400, "pattern: a subject (orders.eu), with * for one part and > or # as the last part (orders.>, orders.#, >)")
    parts = p.split(".")
    lit, rx, wild = [], [], False
    for i, t in enumerate(parts):
        last = i == len(parts) - 1
        if t in (">", "#"):
            if not last:
                raise HTTPException(400, "pattern: %s may only be the last part" % t)
            if t == ">":
                rx.append(r"[^.]+(?:\.[^.]+)*")
            else:                                  # zero or more: also the parent itself, so the dot before it is optional
                if rx:
                    rx[-1] += r"(?:\.[^.]+)*"
                    rx.append(None)
                else:
                    rx.append(r"(?:[^.]+(?:\.[^.]+)*)?")
            wild = True
            continue
        if not PTOKEN.match(t):
            raise HTTPException(400, "pattern part %r: a-z 0-9 _ -, or * / > / #" % t)
        if t == "*":
            wild = True
            rx.append(r"[^.]+")
        else:
            if not wild:
                lit.append(t)
            rx.append(re.escape(t))
    src = r"\.".join(x for x in rx if x is not None)
    return ".".join(lit), re.compile("^" + src + "$")


# ---------------------------------------------------------------- serialisation
def _eff(request, t):
    return _eff_sync(request.app.state.sync, t)


def _eff_sync(sync, t):
    return (t["retention_hours"] or sync.num("pubsub_retention_hours"), min(t["max_messages"] or sync.num("pubsub_max_messages"), sync.num("pubsub_max_messages")))


def _tser(request, t, level=None):
    hours, keep = _eff(request, t)
    d = {"name": t["name"], "title": t["title"] or "", "description": t["description"] or "", "visibility": t["visibility"],
         "public_access": t["public_access"], "owner": t["owner"], "owner_name": t.get("owner_name") or "",
         "owner_avatar": _avatar_url(t["owner"], t.get("owner_avatar_v") or 0), "tags": t["tags"], "msgs": t["msgs"], "bytes": t["bytes"],
         "last_ts": t["last_ts"], "created": t["created"], "updated": t["updated"], "retention_hours": t["retention_hours"],
         "max_messages": t["max_messages"], "effective_retention_hours": hours, "effective_max_messages": keep, "depth": t["name"].count("."),
         "root": t["name"].split(".")[0], "pub_24h": t.get("pub_24h", 0)}
    if level is not None:
        d["level"] = level
        d["can_consume"] = can(level, "consume")
        d["can_produce"] = can(level, "produce")
        d["can_manage"] = level == "admin"
    return d


def _load(request, name, u, need=None):
    """The record NAME (exactly) and the viewer's level on it. 404 when they may not see it, 403 when they lack `need`."""
    name = _name(name)
    repos = R(request)
    t = repos.topic_get(name)
    if not t:
        raise HTTPException(404, "topic not found")
    _, level = repos.topic_level(name, viewer_of(repos, u))
    if not level:
        raise HTTPException(404, "topic not found")
    if need and not can(level, need):
        raise HTTPException(403, "only the owner of the topic (or of a parent topic) or an admin may do that" if need == "admin" else "you may not %s on %s" % (need, name))
    return t, level


def _int(v, lo, hi, what):
    if v in (None, ""):
        return 0
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise HTTPException(400, "%s must be a whole number" % what)
    if n and not lo <= n <= hi:
        raise HTTPException(400, "%s must be 0 (hub default) or %d..%d" % (what, lo, hi))
    return n


def _tags(v):
    if v is None:
        return None
    items = v if isinstance(v, list) else str(v).split(",")
    return ",".join(dict.fromkeys(t for t in (re.sub(r"[^a-z0-9._-]+", "-", str(x).strip().lower()).strip("-") for x in items) if t))[:300]


def _vis(request, v):
    if v not in ("public", "private"):
        raise HTTPException(400, "visibility must be public or private")
    if v == "private" and R(request).setting("allow_private", "1") != "1":
        raise HTTPException(400, "private topics are switched off on this hub")
    return v


# ---------------------------------------------------------------- topics
@r.post("/topics")
def topic_create(body: dict, request: Request, u=Depends(require_user)):
    """{name, title, description, visibility (public | private), public_access (consume | produce), retention_hours, max_messages, tags}.
    A name whose parent (any prefix) is a topic creates a SUB topic: that needs admin on the parent; otherwise a root topic: `create_topics`."""
    repos = R(request)
    name = _name(body.get("name"))
    if name in RESERVED:
        raise HTTPException(400, "%s is a reserved name" % name)
    if repos.topic_get(name):
        raise HTTPException(409, "topic %s already exists" % name)
    viewer = viewer_of(repos, u)
    parent = repos.topic_chain(name)
    if parent:
        _, lvl = repos.topic_level(parent[-1]["name"], viewer)
        if lvl != "admin":
            raise HTTPException(403, "%s is under %s: only its owner (or an admin) may add sub topics" % (name, parent[-1]["name"]))
    elif "create_topics" not in repos.perms(u["role"]):
        raise HTTPException(403, "missing permission: create_topics")
    vis = body.get("visibility") or (parent[-1]["visibility"] if parent else repos.initial_visibility())
    pa = body.get("public_access") or "consume"
    if pa not in PUBLIC_ACCESS:
        raise HTTPException(400, "public_access must be consume or produce")
    tid = repos.topic_create(name, u["id"], title=_clean(body.get("title"), 200), description=_clean(body.get("description"), 1000),
                             visibility=_vis(request, vis), public_access=pa, retention_hours=_int(body.get("retention_hours"), 1, 8760, "retention_hours"),
                             max_messages=_int(body.get("max_messages"), 10, 10000000, "max_messages"), tags=_tags(body.get("tags")) or "")
    repos.audit(u["username"], "topic.create", name, vis)
    return _tser(request, repos.topic_by_id(tid), "admin")


def _scope(u, scope):
    if scope in ("mine", "shared") and not u:
        raise HTTPException(401, "authentication required")
    return (u["id"] if scope == "mine" else None), (u["id"] if scope == "shared" else None)


@r.get("/topics")
def topic_list(request: Request, q: str = "", scope: str = "all", owner: str = "", under: str = "", sort: str = "name", limit: int = 100, offset: int = 0,
               u=Depends(current_user)):
    """scope: all | mine | shared (with me or my groups). under: a topic name: it and its sub topics. sort: name | new | active | busy."""
    repos = R(request)
    mine, shared = _scope(u, scope)
    items, total = repos.topic_list(viewer_of(repos, u), q=q[:200], owner=owner[:64], mine_uid=mine, shared_uid=shared, under=_name(under) if under else "",
                                    sort=sort, limit=max(1, min(int(limit), 500)), offset=max(0, int(offset)))
    return {"items": [_tser(request, t) for t in items], "total": total}


@r.get("/topics/facets")
def topic_facets(request: Request, q: str = "", u=Depends(current_user)):
    repos = R(request)
    return repos.topic_facets(viewer_of(repos, u), q=q[:200], uid=u["id"] if u else None)


@r.get("/topics/overview")
def topic_overview(request: Request, u=Depends(current_user)):
    repos = R(request)
    E(request).flush()
    return repos.topic_overview(viewer_of(repos, u))


@r.get("/topics/{name}")
def topic_get(name: str, request: Request, u=Depends(current_user)):
    """The record, the viewer's level, its parents (records above it) and its sub topic records (that the viewer may see)."""
    t, level = _load(request, name, u)
    repos = R(request)
    d = _tser(request, t, level)
    d["parents"] = [x["name"] for x in repos.topic_chain(t["name"])[:-1]]
    kids, total = repos.topic_list(viewer_of(repos, u), under=t["name"], limit=300)
    d["children"] = [_tser(request, x) for x in kids if x["name"] != t["name"]]
    d["children_total"] = max(0, total - 1)
    return d


@r.patch("/topics/{name}")
def topic_patch(name: str, body: dict, request: Request, u=Depends(require_user)):
    t, _ = _load(request, name, u, "admin")
    f = {}
    if "title" in body:
        f["title"] = _clean(body["title"], 200)
    if "description" in body:
        f["description"] = _clean(body["description"], 1000)
    if "tags" in body:
        f["tags"] = _tags(body["tags"])
    if body.get("visibility") not in (None, ""):
        f["visibility"] = _vis(request, body["visibility"])
    if body.get("public_access") not in (None, ""):
        if body["public_access"] not in PUBLIC_ACCESS:
            raise HTTPException(400, "public_access must be consume or produce")
        f["public_access"] = body["public_access"]
    if "retention_hours" in body:
        f["retention_hours"] = _int(body["retention_hours"], 1, 8760, "retention_hours")
    if "max_messages" in body:
        f["max_messages"] = _int(body["max_messages"], 10, 10000000, "max_messages")
    if body.get("owner") not in (None, "") and body["owner"] != t["owner"]:
        o = R(request).user_by_name(str(body["owner"]))
        if not o:
            raise HTTPException(404, "user %s not found" % body["owner"])
        f["owner_id"] = o["id"]
    R(request).topic_update(t["id"], **f)
    R(request).audit(u["username"], "topic.edit", t["name"], ",".join(sorted(f))[:200])
    t = R(request).topic_by_id(t["id"])
    _, level = R(request).topic_level(t["name"], viewer_of(R(request), u))
    return _tser(request, t, level)


@r.delete("/topics/{name}")
def topic_delete(name: str, request: Request, u=Depends(require_user)):
    """The topic, every sub topic record below it, their messages, shares and statistics."""
    t, _ = _load(request, name, u, "admin")
    n = R(request).topic_delete(t["name"])
    R(request).audit(u["username"], "topic.delete", t["name"], "%d record(s)" % n)
    return {"ok": True, "removed": n}


# ---------------------------------------------------------------- sharing
@r.get("/topics/{name}/access")
def topic_access(name: str, request: Request, u=Depends(require_user)):
    t, _ = _load(request, name, u, "admin")
    repos = R(request)
    inherited = []
    for p in repos.topic_chain(t["name"])[:-1]:
        inherited += [dict(x, topic=p["name"]) for x in repos.topic_access_list(p["id"])]
    return {"visibility": t["visibility"], "public_access": t["public_access"], "owner": t["owner"], "access": repos.topic_access_list(t["id"]),
            "inherited": inherited}


@r.put("/topics/{name}/access")
def topic_access_put(name: str, body: dict, request: Request, u=Depends(require_user)):
    """{type: 'user'|'group', name, access: 'consume'|'produce'|'both'}, or {items: [{type, name}...], access} (all or none)."""
    t, _ = _load(request, name, u, "admin")
    access = body.get("access")
    if access not in ACCESS:
        raise HTTPException(400, "access must be consume, produce or both")
    items = body.get("items")
    if items is None:
        items = [{"type": body.get("type"), "name": body.get("name", "")}]
    if not isinstance(items, list) or not items or len(items) > 500:
        raise HTTPException(400, "items must be a list of 1..500 {type, name}")
    repos, grants = R(request), []
    for it in items:
        ptype, pname = (it or {}).get("type"), str((it or {}).get("name", ""))
        if ptype not in ("user", "group"):
            raise HTTPException(400, "type must be user or group")
        x = repos.user_by_name(pname) if ptype == "user" else repos.group_by_name(pname)
        if not x:
            raise HTTPException(404, "%s %s not found" % (ptype, pname))
        grants.append((ptype, x["id"], pname))
    repos.topic_access_grant_many(t["id"], [(a, b) for a, b, _ in grants], access)
    repos.audit(u["username"], "topic.grant", t["name"], ", ".join("%s:%s=%s" % (a, n, access) for a, _, n in grants)[:500])
    return {"ok": True}


@r.delete("/topics/{name}/access/{ptype}/{pname}")
def topic_access_delete(name: str, ptype: str, pname: str, request: Request, u=Depends(require_user)):
    t, _ = _load(request, name, u, "admin")
    repos = R(request)
    if ptype not in ("user", "group"):
        raise HTTPException(400, "type must be user or group")
    x = repos.user_by_name(pname) if ptype == "user" else repos.group_by_name(pname)
    if not x:
        raise HTTPException(404, "%s %s not found" % (ptype, pname))
    repos.topic_access_revoke(t["id"], ptype, x["id"])
    repos.audit(u["username"], "topic.revoke", t["name"], "%s:%s" % (ptype, pname))
    return {"ok": True}


@r.get("/topics/{name}/stats")
def topic_stats(name: str, request: Request, days: int = 7, u=Depends(current_user)):
    """Analytics of the topic and its sub topics: published / delivered per day (per hour for days=1), stored messages, top subjects,
    producers and consumers (active = seen in the last 10 minutes). Consumers and producers are listed for people who may manage it."""
    t, level = _load(request, name, u)
    E(request).flush()                                       # this process's buffered consumer numbers first
    d = R(request).topic_stats(t["name"], max(1, min(int(days), 90)))
    if level != "admin":
        d["clients"] = [dict(c, username="", display_name="", pattern="") for c in d["clients"]]
    return d


# ---------------------------------------------------------------- publish
def _publish(request, u, items):
    repos = R(request)
    viewer = viewer_of(repos, u)
    max_b = request.app.state.sync.num("pubsub_max_kb") * 1024
    rows, records = [], {}
    for it in items:
        if not isinstance(it, dict):
            raise HTTPException(400, "each message is an object {subject, data, key, headers}")
        subject = _name(it.get("subject") or it.get("topic"), "subject")
        rec, level = repos.topic_level(subject, viewer)
        if not rec:
            raise HTTPException(404, "no topic covers %s: create %s first (aihub pubsub create %s)" % (subject, subject.split(".")[0], subject.split(".")[0]))
        if not level:
            raise HTTPException(404, "topic not found: %s" % subject)
        if not can(level, "produce"):
            raise HTTPException(403, "you may not publish to %s" % subject)
        data = it.get("data", "")
        if isinstance(data, str):
            ctype, body = "text", data
        else:
            try:
                ctype, body = "json", json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            except ValueError:
                raise HTTPException(400, "data: NaN and Infinity are not JSON")
        if len(body.encode("utf-8")) > max_b:
            raise HTTPException(413, "message too large (limit %d KB, admin setting pubsub_max_kb)" % (max_b >> 10))
        key = _clean(it.get("key"), 200)
        hd = it.get("headers") or {}
        if not isinstance(hd, dict) or len(hd) > 20:
            raise HTTPException(400, "headers: an object with at most 20 entries")
        hd = {_clean(k, 64): _clean(v, 500) for k, v in hd.items() if _clean(k, 64)}
        rows.append((rec["id"], subject, key, ctype, body, headers_json(hd)))
        records[rec["id"]] = rec
    ids = repos.topic_publish(u["id"], rows)
    eng = E(request)
    eng.published(records)                                  # retention runs in the engine's thread, not in this request
    eng.kick()                                              # the pump reads the new rows now and wakes the subscribers
    return ids


@r.post("/pubsub/publish")
def ps_publish(body: dict, request: Request, u=Depends(require_user)):
    """One message {subject, data, key?, headers?} or {messages: [...]} (1..500, all or none). `data` is any JSON value; a string is stored as
    text. -> {ids, count}"""
    items = body.get("messages") if "messages" in body else [body]
    if not isinstance(items, list) or not items or len(items) > BATCH_MAX:
        raise HTTPException(400, "messages must be a list of 1..%d" % BATCH_MAX)
    ids = _publish(request, u, items)
    return {"ids": ids, "count": len(ids)}


# ---------------------------------------------------------------- consume
def _want(prx, prefix, allowed):
    lead = prefix + "."

    def want(subject, tid):
        if prefix and subject != prefix and not subject.startswith(lead):
            return False
        return prx.match(subject) is not None and allowed(subject, tid)
    return want


def _scope_load(repos, viewer, prefix):
    return repos.topic_scope_cached(viewer, prefix)


def _fetch_db(repos, want, rids, prefix, after, limit, tail, head):
    """The database path: positions older than the in-memory ring, and `tail`. -> (encoded items, {topic id: n}, next)."""
    ids = list(rids) if prefix else None
    out, per, pos = [], {}, (after if not tail else head + 1)
    for _ in range(10):                                      # at most 10 * SCAN rows per call; the position moves past what was read
        rows = repos.topic_scan(ids, prefix, pos, SCAN, desc=bool(tail))
        for m in rows:
            pos = m["id"]
            if want(m["subject"], m["topic_id"]):
                out.append(m)
                if len(out) >= limit:
                    break
        if len(out) >= limit or len(rows) < SCAN:
            break
    if tail:
        out.reverse()
        pos = head
    elif out and len(out) >= limit:
        pos = out[-1]["id"]
    for m in out:
        per[m["topic_id"]] = per.get(m["topic_id"], 0) + 1
    return [encode(m) for m in out], per, pos


@r.get("/pubsub/messages")
async def ps_messages(request: Request, pattern: str, after: Optional[int] = None, start: str = Query("latest", alias="from"), cursor: str = "",
                      ack: Optional[int] = None, commit: bool = True, match: str = "", limit: int = 100, wait: float = 0, tail: int = 0,
                      u=Depends(current_user)):
    """Messages whose subject matches PATTERN, oldest first, on topics you may consume. A regex on the message body is applied by the client
    (`match` is refused: aihub pubsub sub --match does it locally). Position: `after` = a message id (the `next` of the previous answer);
    without it `from` = latest (only new messages) | start (everything kept); `tail=N` = the last N matching messages. `cursor=NAME` keeps
    the position on the hub for you (durable; signed in); with `commit=0` it only moves when you pass `ack=ID` (the last id you processed)
    on the next call. `wait` (0..30 s) holds the call open until something arrives (long poll). -> {items, next, cursor}"""
    if match:
        raise HTTPException(400, "match: the hub does not run regexes on messages; filter on the client (aihub pubsub sub --match, or the "
                                 "Message regex of the aihub-ui node). Update the aihub CLI if it sent this.")
    repos = R(request)
    viewer = viewer_of(repos, u)
    prefix, prx = _pattern(str(pattern or "").strip().lower())
    limit = max(1, min(int(limit), LIMIT_MAX))
    tail = max(0, min(int(tail), 1000))
    cname = cursor.strip()[:64]
    eng = E(request)
    eng.ensure()
    if cname:
        if not u:
            raise HTTPException(401, "sign in to use a durable cursor")
        if not re.match(r"^[A-Za-z0-9._-]+$", cname):
            raise HTTPException(400, "cursor: letters, digits . _ -")
    stored = None
    if cname and after is None and not tail:
        stored, spat = await run_in_threadpool(repos.topic_cursor, u["id"], cname)
        if stored is not None and spat != pattern:
            stored = None
        if ack is not None and ack != stored:
            await run_in_threadpool(repos.topic_cursor_set, u["id"], cname, pattern, ack)
            stored = ack
        if stored is not None:
            after = stored
    if after is None and not tail:
        after = 0 if start == "start" else await run_in_threadpool(repos.topic_head)
        if cname:                                            # a new durable cursor starts where this first call starts, even with commit=0
            await run_in_threadpool(repos.topic_cursor_set, u["id"], cname, pattern, after)
            stored = after
    deadline = time.monotonic() + max(0.0, min(float(wait), WAIT_MAX))
    note = eng.note
    while True:
        seq = note.seq
        sc = repos.topic_scope_peek(viewer, prefix) or await run_in_threadpool(_scope_load, repos, viewer, prefix)
        by, readable, rids, allowed = sc
        want = _want(prx, prefix, allowed)
        if not readable:                                     # nothing to read (yet): wait; a new topic or share shows within the cache time
            parts, per = [], {}
            if tail:
                after = await run_in_threadpool(repos.topic_head)
        elif tail:
            head = await run_in_threadpool(repos.topic_head)
            parts, per, after = await run_in_threadpool(_fetch_db, repos, want, rids, prefix, 0, tail, True, head)
        else:
            n = eng.backlog(after)
            if n is None:                                    # older than the ring: the database
                parts, per, after = await run_in_threadpool(_fetch_db, repos, want, rids, prefix, after, limit, 0, 0)
            elif n > INLINE:
                parts, per, after = await run_in_threadpool(eng.read, after, limit, want)
            else:
                parts, per, after = eng.read(after, limit, want)
        if parts or tail or time.monotonic() >= deadline or await request.is_disconnected():
            break
        repos.db.yield_unit()                                # do not hold a pooled connection while waiting
        await note.wait(seq, min(5.0, max(0.05, deadline - time.monotonic())))
    uid = u["id"] if u else 0
    if per:
        eng.delivered(uid, per, pattern)
    elif readable:
        eng.seen(sorted(by[n]["id"] for n in readable)[:5], uid, pattern)
    if cname and commit and not tail and after != stored:
        await run_in_threadpool(repos.topic_cursor_set, u["id"], cname, pattern, after)
    return Response(answer(parts, after, cname), media_type="application/json")


@r.get("/pubsub/cursors")
def ps_cursors(request: Request, u=Depends(require_user)):
    return {"items": R(request).topic_cursors(u["id"])}


@r.delete("/pubsub/cursors/{name}")
def ps_cursor_delete(name: str, request: Request, u=Depends(require_user)):
    if not R(request).topic_cursor_delete(u["id"], name):
        raise HTTPException(404, "no cursor " + name)
    return {"ok": True}
