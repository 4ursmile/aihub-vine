"""SQL for pub/sub topics (mixed into `Repos`). A topic is a dotted name (`orders`, `orders.eu`, `orders.eu.paid`). A subject is published to
the NEAREST topic record among its prefixes: `orders.eu.paid` lands in `orders.eu` when that record exists, else in `orders`. A sub topic only
needs a record of its own when it should differ (visibility, sharing, retention).

Who may do what (`topic_caps`):
  * site admins (`bypass`) and the owner of the topic or of ANY of its ancestors: admin (consume + produce + manage);
  * a PUBLIC topic: everybody may consume (signed out too); with public_access='produce' every SIGNED-IN user may publish as well;
  * shares (`topic_access`: consume | produce | both, for a user or a group) on the topic OR any ancestor flow down to the sub topics.
Visibility is per record: a private sub topic of a public topic is private (only the grants above reach it).
Rows are read by position or name, never unpacked (a Postgres Row iterates its keys)."""
import json
import threading
import time

ACCESS = ("consume", "produce", "both")
PUBLIC_ACCESS = ("consume", "produce")
TCOLS = ("t.id AS id,t.name AS name,t.owner_id AS owner_id,t.title AS title,t.description AS description,t.visibility AS visibility,"
         "t.public_access AS public_access,t.retention_hours AS retention_hours,t.max_messages AS max_messages,t.tags AS tags,t.msgs AS msgs,"
         "t.bytes AS bytes,t.last_ts AS last_ts,t.created AS created,t.updated AS updated,o.username AS owner,o.display_name AS owner_name,"
         "o.avatar_v AS owner_avatar_v")
TFROM = "FROM topics t JOIN users o ON o.id=t.owner_id"
MCOLS = ("m.id AS id,m.topic_id AS topic_id,m.subject AS subject,m.ts AS ts,m.mkey AS mkey,m.ctype AS ctype,m.body AS body,m.size AS size,"
         "m.headers AS headers,u.username AS username")
# record `a` is `%(a)s` itself or one of its ancestors (exact prefix test: `_` in a name must not act as a LIKE wildcard)
ANC = "(a.id=%(a)s.id OR substr(%(a)s.name, 1, length(a.name) + 1) = a.name || '.')"
CACHE_TTL = 3.0                                          # seconds a topic chain / a viewer's caps are reused in this process


def _trow(r):
    d = dict(r)
    d["tags"] = [t for t in (d.get("tags") or "").split(",") if t]
    d["public_access"] = d.get("public_access") if d.get("public_access") in PUBLIC_ACCESS else "consume"
    for k in ("msgs", "bytes", "retention_hours", "max_messages"):
        d[k] = int(d.get(k) or 0)
    d["last_ts"] = float(d.get("last_ts") or 0)
    return d


def prefixes(name):
    """'a.b.c' -> ['a', 'a.b', 'a.b.c']"""
    p = name.split(".")
    return [".".join(p[:i + 1]) for i in range(len(p))]


def _like(prefix):
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ".%"


def caps_str(c):
    if "admin" in c:
        return "admin"
    if "consume" in c and "produce" in c:
        return "both"
    return "consume" if "consume" in c else "produce" if "produce" in c else None


def can(level, what):
    """level (admin | both | consume | produce | None) allows `what` (consume | produce | admin)?"""
    if level == "admin":
        return True
    if what == "admin" or not level:
        return False
    return level == "both" or level == what


class PubSubRepos:
    _ps_lock = threading.Lock()
    _ps_cache = {}                                       # key -> (expires, value): topic chains and viewer caps, cleared by every topic write
    _ps_stamp = [0]

    # ---------------------------------------------------------------- cache
    def _ps_get(self, key, fn):
        now = time.time()
        with self._ps_lock:
            hit = self._ps_cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
            stamp = self._ps_stamp[0]
        v = fn()
        with self._ps_lock:
            if stamp == self._ps_stamp[0]:               # a write in between: do not store what it may have changed
                if len(self._ps_cache) > 5000:
                    self._ps_cache.clear()
                self._ps_cache[key] = (now + CACHE_TTL, v)
        return v

    def _ps_peek(self, key):
        """The cached value or None, without loading it (the event loop must not wait for the database)."""
        with self._ps_lock:
            hit = self._ps_cache.get(key)
            return hit[1] if hit and hit[0] > time.time() else None

    def topic_cache_clear(self):
        with self._ps_lock:
            self._ps_cache.clear()
            self._ps_stamp[0] += 1

    # ---------------------------------------------------------------- lookups
    def topic_get(self, name):
        r = self.db.conn().execute("SELECT " + TCOLS + " " + TFROM + " WHERE t.name=?", (name,)).fetchone()
        return _trow(r) if r else None

    def topic_by_id(self, tid):
        r = self.db.conn().execute("SELECT " + TCOLS + " " + TFROM + " WHERE t.id=?", (tid,)).fetchone()
        return _trow(r) if r else None

    def topic_chain(self, subject):
        """The topic records among the prefixes of SUBJECT, root first. [] = no topic covers it. Cached for a few seconds."""
        def load():
            ps = prefixes(subject)
            rows = self.db.conn().execute("SELECT " + TCOLS + " " + TFROM + " WHERE t.name IN (%s)" % ",".join("?" * len(ps)), ps).fetchall()
            by = {r["name"]: _trow(r) for r in rows}
            return [by[p] for p in ps if p in by]
        return self._ps_get(("chain", subject), load)

    def _viewer_grants(self, viewer, ids):
        """{topic_id: set of caps} granted to this viewer (user or one of their groups) on these topics."""
        out = {}
        if not viewer or not ids:
            return out
        ids = list(ids)
        uid = viewer["id"]
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            for r in self.db.conn().execute(
                    "SELECT topic_id, access FROM topic_access WHERE topic_id IN (%s) AND ((principal_type='user' AND principal_id=?) OR "
                    "(principal_type='group' AND principal_id IN (SELECT group_id FROM group_members WHERE user_id=?)))" % ",".join("?" * len(part)),
                    part + [uid, uid]).fetchall():
                a = r[1] if r[1] in ACCESS else "consume"
                out.setdefault(r[0], set()).update(("consume", "produce") if a == "both" else (a,))
        return out

    @staticmethod
    def _caps(chain, viewer, grants):
        """Caps of a viewer on the LAST record of CHAIN (root first)."""
        if not chain:
            return set()
        if viewer and (viewer.get("bypass") or any(t["owner_id"] == viewer["id"] for t in chain)):
            return {"admin", "consume", "produce"}
        t, c = chain[-1], set()
        if t["visibility"] == "public":
            c.add("consume")
            if viewer and t["public_access"] == "produce":
                c.add("produce")
        for x in chain:
            c |= grants.get(x["id"], set())
        return c

    def topic_level(self, subject, viewer):
        """-> (record the subject belongs to or None, level: admin | both | consume | produce | None)."""
        chain = self.topic_chain(subject)
        if not chain:
            return None, None
        key = ("lvl", subject, viewer["id"] if viewer else 0, bool(viewer and viewer.get("bypass")))
        lvl = self._ps_get(key, lambda: caps_str(self._caps(chain, viewer, self._viewer_grants(viewer, [t["id"] for t in chain]))))
        return chain[-1], lvl

    def topic_scope(self, viewer, prefix):
        """Every record a subscription under PREFIX (literal tokens, '' = everything) may read from: the ancestors of the prefix, the prefix
        and everything below it. -> ({name: record}, {name: caps}). Each record's caps use its own chain, which lies inside this set."""
        c = self.db.conn()
        if prefix:
            ps = prefixes(prefix)
            rows = c.execute("SELECT " + TCOLS + " " + TFROM + " WHERE t.name IN (%s) OR t.name LIKE ? ESCAPE '\\' LIMIT 5000" % ",".join("?" * len(ps)),
                             ps + [_like(prefix)]).fetchall()
        else:
            rows = c.execute("SELECT " + TCOLS + " " + TFROM + " LIMIT 5000").fetchall()
        by = {r["name"]: _trow(r) for r in rows}
        grants = self._viewer_grants(viewer, [t["id"] for t in by.values()])
        caps = {}
        for n in by:
            caps[n] = self._caps([by[p] for p in prefixes(n) if p in by], viewer, grants)
        return by, caps

    @staticmethod
    def _scope_key(viewer, prefix):
        return ("scope", viewer["id"] if viewer else 0, bool(viewer and viewer.get("bypass")), prefix)

    def topic_scope_cached(self, viewer, prefix):
        """topic_scope for delivery, cached a few seconds and shared by every waiter of this viewer and prefix ->
        (records {name: record}, readable names, ids of all those records, allowed(subject, topic_id) with a memo)."""
        def load():
            by, caps = self.topic_scope(viewer, prefix)
            readable = frozenset(n for n, c in caps.items() if "consume" in c)
            ids = frozenset(t["id"] for t in by.values())
            memo = {}

            def allowed(subject, tid):
                if tid not in ids:                        # the message's record still exists (a deleted topic's messages stay in memory a while) ...
                    return False
                v = memo.get(subject)
                if v is None:                             # ... and the nearest record above the subject decides
                    v = False
                    for p in reversed(prefixes(subject)):
                        if p in by:
                            v = p in readable
                            break
                    if len(memo) > 20000:
                        memo.clear()
                    memo[subject] = v
                return v
            return by, readable, ids, allowed
        return self._ps_get(self._scope_key(viewer, prefix), load)

    def topic_scope_peek(self, viewer, prefix):
        return self._ps_peek(self._scope_key(viewer, prefix))

    # ---------------------------------------------------------------- list
    def topic_visible_sql(self, viewer, alias="t"):
        """Who may SEE a topic in lists: public, owner (or owner of an ancestor), or a grant on it or an ancestor."""
        if viewer and viewer.get("bypass"):
            return "1=1", []
        if not viewer:
            return "%s.visibility='public'" % alias, []
        uid = viewer["id"]
        return ("(%(a)s.visibility='public' OR %(a)s.owner_id=? OR EXISTS (SELECT 1 FROM topics a WHERE a.owner_id=? AND " + ANC + ") OR "
                "EXISTS (SELECT 1 FROM topics a JOIN topic_access x ON x.topic_id=a.id WHERE " + ANC + " AND "
                "((x.principal_type='user' AND x.principal_id=?) OR (x.principal_type='group' AND x.principal_id IN "
                "(SELECT group_id FROM group_members WHERE user_id=?)))))") % {"a": alias}, [uid, uid, uid, uid]

    def _topic_where(self, viewer, q="", owner="", mine_uid=None, shared_uid=None, under=""):
        vs, va = self.topic_visible_sql(viewer)
        where, args = [vs], list(va)
        if owner:
            where.append("o.username=?")
            args.append(owner)
        if mine_uid:
            where.append("t.owner_id=?")
            args.append(mine_uid)
        if shared_uid:
            where.append("t.owner_id<>? AND EXISTS (SELECT 1 FROM topics a JOIN topic_access x ON x.topic_id=a.id WHERE " + (ANC % {"a": "t"}) + " "
                         "AND ((x.principal_type='user' AND x.principal_id=?) OR (x.principal_type='group' AND x.principal_id IN "
                         "(SELECT group_id FROM group_members WHERE user_id=?))))")
            args += [shared_uid] * 3
        if under:
            where.append("(t.name=? OR t.name LIKE ? ESCAPE '\\')")
            args += [under, _like(under)]
        for w in [x for x in (q or "").lower().split() if x][:6]:
            like = "%" + w.replace("%", "").replace("_", "") + "%"
            where.append("(lower(t.name) LIKE ? OR lower(t.title) LIKE ? OR lower(t.description) LIKE ? OR lower(t.tags) LIKE ? OR lower(o.username) LIKE ?)")
            args += [like] * 5
        return where, args

    ORDER_T = {"name": "t.name", "new": "t.created DESC", "active": "t.last_ts DESC", "busy": "t.msgs DESC"}

    def topic_list(self, viewer, q="", owner="", mine_uid=None, shared_uid=None, under="", sort="name", limit=100, offset=0):
        where, args = self._topic_where(viewer, q, owner, mine_uid, shared_uid, under)
        c = self.db.conn()
        w = " WHERE " + " AND ".join(where)
        total = c.execute("SELECT COUNT(*) " + TFROM + w, args).fetchone()[0]
        rows = c.execute("SELECT " + TCOLS + " " + TFROM + w + " ORDER BY " + self.ORDER_T.get(sort, "t.name") + ", t.id LIMIT ? OFFSET ?",
                         args + [int(limit), int(offset)]).fetchall()
        items = [_trow(r) for r in rows]
        if items:                                        # messages in the last 24 h, one query for the page
            ids = [t["id"] for t in items]
            h = int(time.time() // 3600) - 24
            day = {r[0]: int(r[1] or 0) for r in c.execute("SELECT topic_id, SUM(pub) FROM topic_stats WHERE hour>? AND topic_id IN (%s) GROUP BY topic_id"
                                                           % ",".join("?" * len(ids)), [h] + ids).fetchall()}
            for t in items:
                t["pub_24h"] = day.get(t["id"], 0)
        return items, total

    def topic_facets(self, viewer, q="", uid=None):
        c = self.db.conn()
        w, a = self._topic_where(viewer, q)
        owners = [{"username": r[0], "name": r[1] or "", "n": r[2]} for r in c.execute(
            "SELECT o.username, o.display_name, COUNT(*) " + TFROM + " WHERE " + " AND ".join(w) + " GROUP BY o.username, o.display_name ORDER BY COUNT(*) DESC, o.username LIMIT 200", a).fetchall()]
        vis = {r[0]: r[1] for r in c.execute("SELECT t.visibility, COUNT(*) " + TFROM + " WHERE " + " AND ".join(w) + " GROUP BY t.visibility", a).fetchall()}
        scopes = {}
        if uid:
            for name, kw in (("all", {}), ("mine", {"mine_uid": uid}), ("shared", {"shared_uid": uid})):
                ww, aa = self._topic_where(viewer, q, **kw)
                scopes[name] = c.execute("SELECT COUNT(*) " + TFROM + " WHERE " + " AND ".join(ww), aa).fetchone()[0]
        return {"owners": owners, "visibility": vis, "scopes": scopes}

    # ---------------------------------------------------------------- write
    def topic_create(self, name, owner_id, title="", description="", visibility="public", public_access="consume", retention_hours=0, max_messages=0, tags=""):
        now = time.time()
        with self.lock:
            c = self.db.conn()
            cur = c.execute("INSERT INTO topics(name,owner_id,title,description,visibility,public_access,retention_hours,max_messages,tags,msgs,bytes,last_ts,created,updated) "
                            "VALUES(?,?,?,?,?,?,?,?,?,0,0,0,?,?)", (name, owner_id, title or "", description or "", visibility or "public", public_access or "consume",
                                                                  int(retention_hours or 0), int(max_messages or 0), tags or "", now, now))
            c.commit()
        self.topic_cache_clear()
        return cur.lastrowid

    def topic_update(self, tid, **f):
        sets, args = [], []
        for k in ("title", "description", "visibility", "public_access", "retention_hours", "max_messages", "tags", "owner_id"):
            if k in f and f[k] is not None:
                sets.append(k + "=?")
                args.append(f[k])
        if not sets:
            return
        sets.append("updated=?")
        args.append(time.time())
        with self.lock:
            c = self.db.conn()
            c.execute("UPDATE topics SET " + ",".join(sets) + " WHERE id=?", args + [tid])
            c.commit()
        self.topic_cache_clear()

    def topic_delete(self, name):
        """The record, every record below it, their grants, messages and statistics. -> number of records removed."""
        with self.lock:
            c = self.db.conn()
            ids = [r[0] for r in c.execute("SELECT id FROM topics WHERE name=? OR name LIKE ? ESCAPE '\\'", (name, _like(name))).fetchall()]
            hw = self._msg_max(c)
            for i in range(0, len(ids), 500):
                part = ids[i:i + 500]
                q = ",".join("?" * len(part))
                for t in ("topic_access", "topic_messages", "topic_stats", "topic_clients"):
                    c.execute("DELETE FROM %s WHERE topic_id IN (%s)" % (t, q), part)
                c.execute("DELETE FROM topics WHERE id IN (%s)" % q, part)
            self._pin_head(c, hw)
            c.commit()
        self.topic_cache_clear()
        return len(ids)

    def topic_access_list(self, tid):
        c = self.db.conn()
        out = []
        for r in c.execute("SELECT u.username AS name,u.display_name AS display_name,a.access AS access FROM topic_access a "
                           "JOIN users u ON u.id=a.principal_id WHERE a.topic_id=? AND a.principal_type='user' ORDER BY u.username", (tid,)).fetchall():
            out.append({"type": "user", "name": r["name"], "display_name": r["display_name"], "access": r["access"]})
        for r in c.execute("SELECT g.name AS name,a.access AS access,(SELECT COUNT(*) FROM group_members WHERE group_id=g.id) AS members FROM topic_access a "
                           "JOIN groups g ON g.id=a.principal_id WHERE a.topic_id=? AND a.principal_type='group' ORDER BY g.name", (tid,)).fetchall():
            out.append({"type": "group", "name": r["name"], "access": r["access"], "members": r["members"]})
        return out

    def topic_access_grant_many(self, tid, grants, access):
        with self.lock:
            c = self.db.conn()
            try:
                for ptype, pid in grants:
                    c.execute("INSERT OR REPLACE INTO topic_access VALUES(?,?,?,?)", (tid, ptype, pid, access))
                c.commit()
            except Exception:
                c.rollback()
                raise
        self.topic_cache_clear()

    def topic_access_revoke(self, tid, ptype, pid):
        with self.lock:
            c = self.db.conn()
            c.execute("DELETE FROM topic_access WHERE topic_id=? AND principal_type=? AND principal_id=?", (tid, ptype, pid))
            c.commit()
        self.topic_cache_clear()

    # ---------------------------------------------------------------- messages
    def topic_publish(self, uid, msgs):
        """msgs = [(topic_id, subject, key, ctype, body, headers_json)] -> [new ids]. GROUP COMMIT: publishes that arrive while another one is
        writing are written together in ONE transaction (rows, counters, hourly statistics, producers) by whoever holds the lead, so the
        database does one commit (one fsync) for many requests. All or none per call: when a joint transaction fails, each call is retried
        alone so one bad call does not fail the others."""
        job = {"uid": uid, "msgs": msgs, "ev": threading.Event(), "ids": None, "err": None, "lead": False}
        q = self._ps_queue()
        with q["lock"]:
            q["jobs"].append(job)
            if not q["busy"]:
                q["busy"] = job["lead"] = True
        if not job["lead"]:
            job["ev"].wait()
            if not job["lead"]:                          # written by the leader
                if job["err"] is not None:
                    raise job["err"]
                return job["ids"]
        while job["ids"] is None and job["err"] is None:  # lead: write what is queued (this call included) ...
            with q["lock"]:
                jobs, q["jobs"] = q["jobs"][:256], q["jobs"][256:]
            self._publish_jobs(jobs)
        with q["lock"]:                                   # ... then hand the lead to the next waiting call, so nobody writes forever
            if q["jobs"]:
                nxt = q["jobs"][0]
                nxt["lead"] = True
                nxt["ev"].set()
            else:
                q["busy"] = False
        if job["err"] is not None:
            raise job["err"]
        return job["ids"]

    _ps_qlock = threading.Lock()

    def _ps_queue(self):
        q = self.__dict__.get("_ps_q")
        if q is None:
            with self._ps_qlock:
                q = self.__dict__.setdefault("_ps_q", {"lock": threading.Lock(), "jobs": [], "busy": False})
        return q

    def _publish_jobs(self, jobs):
        try:
            res = self._publish_tx(jobs)
            for j, ids in zip(jobs, res):
                j["ids"] = ids
        except Exception:
            for j in jobs:                               # one at a time: only the bad call fails
                try:
                    j["ids"] = self._publish_tx([j])[0]
                except Exception as e:
                    j["err"] = e
        for j in jobs:
            if not j["lead"]:
                j["ev"].set()

    def _publish_tx(self, jobs):
        now = time.time()
        hour = int(now // 3600)
        out, per, who = [], {}, {}
        with self.lock:
            c = self.db.conn()
            try:
                for j in jobs:
                    ids = []
                    for tid, subject, key, ctype, body, headers in j["msgs"]:
                        size = len(body.encode("utf-8"))
                        cur = c.execute("INSERT INTO topic_messages(topic_id,subject,ts,user_id,mkey,ctype,body,size,headers) VALUES(?,?,?,?,?,?,?,?,?)",
                                        (tid, subject, now, j["uid"], key or "", ctype, body, size, headers or ""))
                        ids.append(cur.lastrowid)
                        n, b = per.get(tid, (0, 0))
                        per[tid] = (n + 1, b + size)
                        k = (tid, j["uid"] or 0)
                        who[k] = who.get(k, 0) + 1
                    out.append(ids)
                for tid, (n, b) in per.items():
                    c.execute("UPDATE topics SET msgs=msgs+?, bytes=bytes+?, last_ts=? WHERE id=?", (n, b, now, tid))
                    c.execute("INSERT INTO topic_stats(topic_id,hour,pub,bytes,deliv) VALUES(?,?,?,?,0) ON CONFLICT(topic_id,hour) DO UPDATE SET "
                              "pub=topic_stats.pub+excluded.pub, bytes=topic_stats.bytes+excluded.bytes", (tid, hour, n, b))
                for (tid, u), n in who.items():
                    c.execute("INSERT INTO topic_clients(topic_id,user_id,role,pattern,n,last) VALUES(?,?,'produce','',?,?) ON CONFLICT(topic_id,user_id,role,pattern) "
                              "DO UPDATE SET n=topic_clients.n+excluded.n, last=excluded.last", (tid, u, n, now))
                c.commit()
            except Exception:
                c.rollback()
                raise
        return out

    def _msg_max(self, c):
        if self.db.engine != "sqlite":
            return 0
        r = c.execute("SELECT MAX(id) FROM topic_messages").fetchone()
        return int((r[0] if r else 0) or 0)

    def _pin_head(self, c, hw):
        """SQLite reuses the highest rowid once that row is deleted: a new message would then get an id at or below what subscribers have
        already passed and never be delivered. Keep one empty marker row (topic 0, never readable) at the old highest id instead."""
        if hw and self._msg_max(c) < hw:
            c.execute("DELETE FROM topic_messages WHERE topic_id=0")
            c.execute("INSERT INTO topic_messages(id,topic_id,subject,ts,user_id,mkey,ctype,body,size,headers) VALUES(?,0,'',0,0,'','text','',0,'')", (hw,))

    def topic_prune(self, tid, hours, keep):
        """Drop messages older than HOURS and all but the newest KEEP of one topic record. -> rows removed."""
        with self.lock:
            c = self.db.conn()
            hw = self._msg_max(c)
            n = c.execute("DELETE FROM topic_messages WHERE topic_id=? AND ts<?", (tid, time.time() - hours * 3600)).rowcount or 0
            r = c.execute("SELECT id FROM topic_messages WHERE topic_id=? ORDER BY id DESC LIMIT 1 OFFSET ?", (tid, int(keep))).fetchone()
            if r:
                n += c.execute("DELETE FROM topic_messages WHERE topic_id=? AND id<=?", (tid, r[0])).rowcount or 0
            c.execute("DELETE FROM topic_stats WHERE topic_id=? AND hour<?", (tid, int(time.time() // 3600) - 24 * 400))
            if n:
                self._pin_head(c, hw)
            c.commit()
        return n

    def topic_scan(self, ids, prefix, after, limit, desc=False):
        """Raw rows (oldest first unless DESC) with id > AFTER (DESC: id < AFTER when AFTER) whose topic is in IDS (None = any) and whose subject
        is PREFIX or below it ('' = any). The caller applies the exact pattern, the permissions and the message regex."""
        where, args = [], []
        if ids is not None:
            if not ids:
                return []
            where.append("m.topic_id IN (%s)" % ",".join("?" * len(ids)))
            args += list(ids)
        if prefix:
            where.append("(m.subject=? OR m.subject LIKE ? ESCAPE '\\')")
            args += [prefix, _like(prefix)]
        if desc:
            if after:
                where.append("m.id<?")
                args.append(int(after))
        else:
            where.append("m.id>?")
            args.append(int(after or 0))
        w = (" WHERE " + " AND ".join(where)) if where else ""
        return [dict(r) for r in self.db.conn().execute(
            "SELECT " + MCOLS + " FROM topic_messages m LEFT JOIN users u ON u.id=m.user_id" + w + " ORDER BY m.id" + (" DESC" if desc else "") + " LIMIT ?",
            args + [int(limit)]).fetchall()]

    def topic_head(self):
        r = self.db.conn().execute("SELECT MAX(id) FROM topic_messages").fetchone()
        return int((r[0] if r else 0) or 0)

    def topic_clients_flush(self, deliv, seen):
        """Consumer statistics added up by the delivery engine: deliv = {(topic_id, uid, pattern): messages handed out}, seen = {(topic_id,
        uid, pattern)} of subscribers that polled. ONE transaction."""
        now = time.time()
        hour = int(now // 3600)
        per = {}
        for (tid, _, _), n in deliv.items():
            per[tid] = per.get(tid, 0) + n
        with self.lock:
            c = self.db.conn()
            try:
                for tid, n in per.items():
                    c.execute("INSERT INTO topic_stats(topic_id,hour,pub,bytes,deliv) VALUES(?,?,0,0,?) ON CONFLICT(topic_id,hour) DO UPDATE SET "
                              "deliv=topic_stats.deliv+excluded.deliv", (tid, hour, n))
                for (tid, uid, pattern), n in deliv.items():
                    c.execute("INSERT INTO topic_clients(topic_id,user_id,role,pattern,n,last) VALUES(?,?,'consume',?,?,?) ON CONFLICT(topic_id,user_id,role,pattern) "
                              "DO UPDATE SET n=topic_clients.n+excluded.n, last=excluded.last", (tid, uid, pattern, n, now))
                for tid, uid, pattern in seen:
                    if (tid, uid, pattern) not in deliv:
                        c.execute("INSERT INTO topic_clients(topic_id,user_id,role,pattern,n,last) VALUES(?,?,'consume',?,0,?) ON CONFLICT(topic_id,user_id,role,pattern) "
                                  "DO UPDATE SET last=excluded.last", (tid, uid, pattern, now))
                c.commit()
            except Exception:
                c.rollback()
                raise

    # ---------------------------------------------------------------- durable cursors
    def topic_cursor(self, uid, name):
        r = self.db.conn().execute("SELECT last_id, pattern FROM topic_cursors WHERE user_id=? AND name=?", (uid, name)).fetchone()
        return (int(r[0] or 0), r[1] or "") if r else (None, None)

    def topic_cursor_set(self, uid, name, pattern, last_id):
        with self.lock:
            c = self.db.conn()
            c.execute("INSERT OR REPLACE INTO topic_cursors VALUES(?,?,?,?,?)", (uid, name, pattern, int(last_id), time.time()))
            c.commit()

    def topic_cursors(self, uid):
        return [{"name": r[0], "pattern": r[1], "last_id": int(r[2] or 0), "updated": r[3]} for r in self.db.conn().execute(
            "SELECT name, pattern, last_id, updated FROM topic_cursors WHERE user_id=? ORDER BY name", (uid,)).fetchall()]

    def topic_cursor_delete(self, uid, name):
        with self.lock:
            c = self.db.conn()
            n = c.execute("DELETE FROM topic_cursors WHERE user_id=? AND name=?", (uid, name)).rowcount
            c.commit()
        return n

    # ---------------------------------------------------------------- analytics
    def topic_stats(self, name, days=7):
        """Statistics of a record AND everything below it: per day (or per hour for one day), totals, top subjects, producers, consumers."""
        c = self.db.conn()
        ids = [r[0] for r in c.execute("SELECT id FROM topics WHERE name=? OR name LIKE ? ESCAPE '\\'", (name, _like(name))).fetchall()]
        if not ids:
            return None
        q = ",".join("?" * len(ids))
        now = time.time()
        h0 = int(now // 3600) - days * 24
        rows = c.execute("SELECT hour, SUM(pub), SUM(bytes), SUM(deliv) FROM topic_stats WHERE topic_id IN (%s) AND hour>? GROUP BY hour" % q, ids + [h0 - days * 24]).fetchall()
        hourly = days <= 1
        series, cur, prev = {}, {"pub": 0, "bytes": 0, "deliv": 0}, {"pub": 0, "bytes": 0, "deliv": 0}
        for r in rows:
            hr, pub, b, dv = int(r[0]), int(r[1] or 0), int(r[2] or 0), int(r[3] or 0)
            tgt = cur if hr > h0 else prev
            tgt["pub"] += pub
            tgt["bytes"] += b
            tgt["deliv"] += dv
            if hr > h0:
                k = time.strftime("%Y-%m-%d %H:00" if hourly else "%Y-%m-%d", time.gmtime(hr * 3600))
                s = series.setdefault(k, {"date": k, "pub": 0, "deliv": 0, "bytes": 0})
                s["pub"] += pub
                s["deliv"] += dv
                s["bytes"] += b
        keys = []
        for i in range(days * 24 if hourly else days):     # every bucket, empty ones too, so the chart has a real time axis
            t = (h0 + 1 + i) * 3600 if hourly else now - (days - 1 - i) * 86400
            k = time.strftime("%Y-%m-%d %H:00" if hourly else "%Y-%m-%d", time.gmtime(t))
            if k not in keys:
                keys.append(k)
        daily = [series.get(k, {"date": k, "pub": 0, "deliv": 0, "bytes": 0}) for k in keys]
        stored = c.execute("SELECT COUNT(*), COALESCE(SUM(size),0), MIN(ts) FROM topic_messages WHERE topic_id IN (%s)" % q, ids).fetchone()
        subjects = [{"name": r[0], "count": int(r[1])} for r in c.execute(
            "SELECT subject, COUNT(*) FROM topic_messages WHERE topic_id IN (%s) GROUP BY subject ORDER BY COUNT(*) DESC LIMIT 20" % q, ids).fetchall()]
        clients = []
        for r in c.execute("SELECT u.username, u.display_name, k.role, k.pattern, SUM(k.n), MAX(k.last) FROM topic_clients k LEFT JOIN users u ON u.id=k.user_id "
                           "WHERE k.topic_id IN (%s) AND k.last>? GROUP BY u.username, u.display_name, k.role, k.pattern ORDER BY MAX(k.last) DESC LIMIT 100" % q,
                           ids + [now - days * 86400]).fetchall():
            clients.append({"username": r[0] or "anonymous", "display_name": r[1] or "", "role": r[2], "pattern": r[3] or "", "n": int(r[4] or 0),
                            "last": r[5], "active": (r[5] or 0) > now - 600})
        return {"days": days, "hourly": hourly, "series": daily, "totals": cur, "prior": prev,
                "stored": {"messages": int(stored[0] or 0), "bytes": int(stored[1] or 0), "oldest": stored[2]},
                "subjects": subjects, "clients": clients, "topics": len(ids)}

    def topic_overview(self, viewer):
        """Hub-wide numbers for the Pub/Sub page head: topics I can see, messages in the last 24 h on them."""
        vs, va = self.topic_visible_sql(viewer)
        c = self.db.conn()
        n = c.execute("SELECT COUNT(*) " + TFROM + " WHERE " + vs, va).fetchone()[0]
        h = int(time.time() // 3600) - 24
        r = c.execute("SELECT COALESCE(SUM(s.pub),0), COALESCE(SUM(s.deliv),0) FROM topic_stats s JOIN topics t ON t.id=s.topic_id JOIN users o ON o.id=t.owner_id "
                      "WHERE s.hour>? AND " + vs, [h] + va).fetchone()
        return {"topics": n, "pub_24h": int(r[0] or 0), "deliv_24h": int(r[1] or 0)}


def headers_json(h):
    return json.dumps(h, ensure_ascii=False, separators=(",", ":")) if h else ""
