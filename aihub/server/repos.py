"""Repository layer: the only module that writes SQL."""
import json
import re
import sqlite3
import threading
import time

from ..core import version as V
from .db import ALL_PERMS


def _rows(cur):
    return [dict(r) for r in cur.fetchall()]


class Repos:
    def __init__(self, db):
        self.db = db
        self.lock = threading.Lock()  # serialize writers

    # ---- users / auth
    def user_by_name(self, u):
        r = self.db.conn().execute("SELECT * FROM users WHERE username=?", (u,)).fetchone()
        return dict(r) if r else None

    def user_count(self):
        return self.db.conn().execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def user_create(self, username, pw_hash, role="user", status="active"):
        with self.lock:
            c = self.db.conn()
            cur = c.execute("INSERT INTO users(username,password_hash,role,status,created) VALUES(?,?,?,?,?)",
                            (username, pw_hash, role, status, time.time()))
            c.commit()
            return cur.lastrowid

    def user_set(self, username, **f):
        with self.lock:
            c = self.db.conn()
            for k, v in f.items():
                if k in ("role", "status", "password_hash"):
                    c.execute("UPDATE users SET %s=? WHERE username=?" % k, (v, username))
            c.commit()

    def user_delete(self, username):
        with self.lock:
            c = self.db.conn()
            c.execute("DELETE FROM avatars WHERE user_id IN (SELECT id FROM users WHERE username=?)", (username,))
            c.execute("DELETE FROM users WHERE username=?", (username,))
            c.commit()

    def users(self, q=""):
        return _rows(self.db.conn().execute(
            "SELECT id,username,display_name,title,avatar_v,role,status,auth_provider,created FROM users "
            "WHERE username LIKE ? OR display_name LIKE ? ORDER BY id", ("%" + q + "%", "%" + q + "%")))

    def profile_set(self, username, display_name=None, title=None):
        with self.lock:
            c = self.db.conn()
            if display_name is not None:
                c.execute("UPDATE users SET display_name=? WHERE username=?", (display_name, username))
            if title is not None:
                c.execute("UPDATE users SET title=? WHERE username=?", (title, username))
            c.commit()

    def avatar_set(self, uid, mime, data):
        with self.lock:
            c = self.db.conn()
            c.execute("INSERT OR REPLACE INTO avatars VALUES(?,?,?)", (uid, mime, data))
            c.execute("UPDATE users SET avatar_v=? WHERE id=?", (int(time.time()), uid))
            c.commit()

    def avatar_clear(self, uid):
        with self.lock:
            c = self.db.conn()
            c.execute("DELETE FROM avatars WHERE user_id=?", (uid,))
            c.execute("UPDATE users SET avatar_v=0 WHERE id=?", (uid,))
            c.commit()

    def avatar_get(self, username):
        r = self.db.conn().execute("SELECT a.mime,a.data FROM avatars a JOIN users u ON u.id=a.user_id WHERE u.username=?",
                                   (username,)).fetchone()
        return (r[0], bytes(r[1])) if r else None

    def tokens_revoke_others(self, uid, keep_hash):
        with self.lock:
            c = self.db.conn()
            c.execute("DELETE FROM tokens WHERE user_id=? AND hash<>?", (uid, keep_hash))
            c.commit()

    def token_add(self, h, user_id, kind, name=""):
        with self.lock:
            c = self.db.conn()
            c.execute("INSERT INTO tokens VALUES(?,?,?,?,?)", (h, user_id, kind, name, time.time()))
            c.commit()

    def token_user(self, h):
        r = self.db.conn().execute(
            "SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.hash=?", (h,)).fetchone()
        return dict(r) if r else None

    def tokens(self, user_id):
        return _rows(self.db.conn().execute(
            "SELECT hash AS id,kind,name,created FROM tokens WHERE user_id=?", (user_id,)))

    def token_delete(self, user_id, h=None, kind=None):
        with self.lock:
            c = self.db.conn()
            if h:
                c.execute("DELETE FROM tokens WHERE user_id=? AND hash=?", (user_id, h))
            elif kind:
                c.execute("DELETE FROM tokens WHERE user_id=? AND kind=?", (user_id, kind))
            c.commit()

    def perms(self, role):
        return {r[0] for r in self.db.conn().execute("SELECT permission FROM role_permissions WHERE role=?", (role,))}

    def roles(self):
        """{role: [permissions]} including roles that currently grant nothing."""
        c = self.db.conn()
        out = {r[0]: [] for r in c.execute("SELECT name FROM roles ORDER BY builtin DESC, name")}
        for r in c.execute("SELECT role,permission FROM role_permissions ORDER BY permission"):
            out.setdefault(r[0], []).append(r[1])
        return out

    def roles_detail(self):
        c = self.db.conn()
        perms = self.roles()
        counts = {r[0]: r[1] for r in c.execute("SELECT role,COUNT(*) FROM users GROUP BY role")}
        return [{"name": r["name"], "description": r["description"], "builtin": bool(r["builtin"]),
                 "permissions": perms.get(r["name"], []), "users": counts.get(r["name"], 0)}
                for r in c.execute("SELECT * FROM roles ORDER BY builtin DESC, name")]

    def role_create(self, name, description, permissions):
        with self.lock:
            c = self.db.conn()
            c.execute("INSERT INTO roles VALUES(?,?,0)", (name, description))
            for p in permissions:
                c.execute("INSERT OR IGNORE INTO role_permissions VALUES(?,?)", (name, p))
            c.commit()

    def role_update(self, name, permissions=None, description=None):
        with self.lock:
            c = self.db.conn()
            if description is not None:
                c.execute("UPDATE roles SET description=? WHERE name=?", (description, name))
            if permissions is not None:
                c.execute("DELETE FROM role_permissions WHERE role=?", (name,))
                for p in permissions:
                    c.execute("INSERT INTO role_permissions VALUES(?,?)", (name, p))
            c.commit()

    def role_delete(self, name):
        with self.lock:
            c = self.db.conn()
            c.execute("DELETE FROM role_permissions WHERE role=?", (name,))
            c.execute("DELETE FROM roles WHERE name=?", (name,))
            c.commit()

    def users_create_many(self, rows):
        """rows: [(username, pw_hash, role, display_name, title)] -> set of usernames created. Skips existing names."""
        made = set()
        with self.lock:
            c = self.db.conn()
            now = time.time()
            for u, h, role, dn, title in rows:
                try:
                    c.execute("INSERT INTO users(username,password_hash,role,status,display_name,title,created) VALUES(?,?,?,?,?,?,?)",
                              (u, h, role, "active", dn, title, now))
                    made.add(u)
                except sqlite3.IntegrityError:
                    pass
            c.commit()
        return made

    # ---- settings / audit
    def setting(self, k, default=None):
        r = self.db.conn().execute("SELECT value FROM app_settings WHERE key=?", (k,)).fetchone()
        return r[0] if r else default

    def set_setting(self, k, v):
        with self.lock:
            c = self.db.conn()
            c.execute("INSERT OR REPLACE INTO app_settings VALUES(?,?)", (k, str(v)))
            c.commit()

    def audit(self, actor, action, target="", detail=""):
        with self.lock:
            c = self.db.conn()
            c.execute("INSERT INTO audit_log(ts,actor,action,target,detail) VALUES(?,?,?,?,?)",
                      (time.time(), actor, action, target, detail))
            c.commit()

    def audit_list(self, limit=200):
        return _rows(self.db.conn().execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)))

    # ---- packages
    def _pkg(self, r):
        d = dict(r)
        d["tags"] = [t for t in (d.get("tags") or "").split(",") if t]
        return d

    # --- who may see what. ONE place defines it; every read path (list, facets, rankings, dashboard, ...) uses it.
    @staticmethod
    def visible_sql(viewer, alias="packages"):
        """-> (sql, args): rows of `alias` the viewer may see. viewer = None (anonymous) | user dict with 'bypass' flag.
        Visible = public, or viewer is a maintainer / is shared directly / is in a shared group / is a site admin."""
        if viewer and viewer.get("bypass"):
            return "1=1", []
        if not viewer:
            return "%s.visibility='public'" % alias, []
        uid = viewer["id"]
        return ("(%(a)s.visibility='public' OR EXISTS (SELECT 1 FROM package_maintainers m WHERE m.package_id=%(a)s.id AND m.user_id=?)"
                " OR EXISTS (SELECT 1 FROM package_access x WHERE x.package_id=%(a)s.id AND ((x.principal_type='user' AND x.principal_id=?)"
                " OR (x.principal_type='group' AND x.principal_id IN (SELECT group_id FROM group_members WHERE user_id=?)))))" % {"a": alias},
                [uid, uid, uid])

    def access_level(self, pkg_id, viewer):
        """-> 'admin' | 'develop' | 'view' | None for this viewer on this package."""
        if viewer and viewer.get("bypass"):
            return "admin"
        c = self.db.conn()
        vis = c.execute("SELECT visibility FROM packages WHERE id=?", (pkg_id,)).fetchone()
        best = "view" if vis and vis[0] == "public" else None
        if not viewer:
            return best
        uid = viewer["id"]
        if c.execute("SELECT 1 FROM package_maintainers WHERE package_id=? AND user_id=?", (pkg_id, uid)).fetchone():
            return "admin"
        for (a,) in c.execute(
                "SELECT access FROM package_access WHERE package_id=? AND ((principal_type='user' AND principal_id=?) OR "
                "(principal_type='group' AND principal_id IN (SELECT group_id FROM group_members WHERE user_id=?)))", (pkg_id, uid, uid)):
            if a == "develop" or best is None:
                best = a if (a == "develop" or best is None) else best
        return best

    def package(self, name):
        r = self.db.conn().execute(
            "SELECT id,name,type,description,tags,latest_version,hidden,visibility,created,updated FROM packages WHERE name=?",
            (name,)).fetchone()
        return self._pkg(r) if r else None

    def package_names_visible(self, viewer):
        sql, a = self.visible_sql(viewer)
        return {r[0] for r in self.db.conn().execute("SELECT name FROM packages WHERE " + sql, a)}

    def set_visibility(self, name, visibility):
        with self.lock:
            c = self.db.conn()
            c.execute("UPDATE packages SET visibility=? WHERE name=?", (visibility, name))
            c.commit()

    def access_list(self, pid):
        c = self.db.conn()
        out = []
        for r in c.execute("SELECT u.username AS name,u.display_name,u.avatar_v,a.access FROM package_access a "
                           "JOIN users u ON u.id=a.principal_id WHERE a.package_id=? AND a.principal_type='user' ORDER BY u.username", (pid,)):
            out.append({"type": "user", "name": r["name"], "display_name": r["display_name"], "avatar_v": r["avatar_v"], "access": r["access"]})
        for r in c.execute("SELECT g.name,a.access,(SELECT COUNT(*) FROM group_members WHERE group_id=g.id) AS members FROM package_access a "
                           "JOIN groups g ON g.id=a.principal_id WHERE a.package_id=? AND a.principal_type='group' ORDER BY g.name", (pid,)):
            out.append({"type": "group", "name": r["name"], "access": r["access"], "members": r["members"]})
        return out

    def access_grant(self, pid, ptype, principal_id, access):
        with self.lock:
            c = self.db.conn()
            c.execute("INSERT OR REPLACE INTO package_access VALUES(?,?,?,?)", (pid, ptype, principal_id, access))
            c.commit()

    def access_revoke(self, pid, ptype, principal_id):
        with self.lock:
            c = self.db.conn()
            c.execute("DELETE FROM package_access WHERE package_id=? AND principal_type=? AND principal_id=?", (pid, ptype, principal_id))
            c.commit()

    # ---- groups
    def group_by_name(self, name):
        r = self.db.conn().execute("SELECT * FROM groups WHERE name=?", (name,)).fetchone()
        return dict(r) if r else None

    def groups(self):
        return _rows(self.db.conn().execute(
            "SELECT g.id,g.name,g.description,g.created,g.owner_id,(SELECT COUNT(*) FROM group_members m WHERE m.group_id=g.id) AS members,"
            "(SELECT COUNT(*) FROM package_access a WHERE a.principal_type='group' AND a.principal_id=g.id) AS packages "
            "FROM groups g ORDER BY g.name"))

    def group_create(self, name, description, owner_id=None):
        with self.lock:
            c = self.db.conn()
            gid = c.execute("INSERT INTO groups(name,description,created,owner_id) VALUES(?,?,?,?)",
                            (name, description, time.time(), owner_id)).lastrowid
            c.commit()
            return gid

    def group_update(self, gid, description):
        with self.lock:
            c = self.db.conn()
            c.execute("UPDATE groups SET description=? WHERE id=?", (description, gid))
            c.commit()

    def group_delete(self, gid):
        with self.lock:
            c = self.db.conn()
            c.execute("DELETE FROM package_access WHERE principal_type='group' AND principal_id=?", (gid,))
            c.execute("DELETE FROM group_members WHERE group_id=?", (gid,))
            c.execute("DELETE FROM groups WHERE id=?", (gid,))
            c.commit()

    def group_members(self, gid):
        return _rows(self.db.conn().execute(
            "SELECT u.username,u.display_name,u.title,u.avatar_v FROM group_members m JOIN users u ON u.id=m.user_id "
            "WHERE m.group_id=? ORDER BY u.username", (gid,)))

    def group_member_add(self, gid, uids):
        with self.lock:
            c = self.db.conn()
            for uid in uids:
                c.execute("INSERT OR IGNORE INTO group_members VALUES(?,?)", (gid, uid))
            c.commit()

    def group_member_remove(self, gid, uid):
        with self.lock:
            c = self.db.conn()
            c.execute("DELETE FROM group_members WHERE group_id=? AND user_id=?", (gid, uid))
            c.commit()

    def groups_of(self, uid):
        return [r[0] for r in self.db.conn().execute(
            "SELECT g.name FROM groups g JOIN group_members m ON m.group_id=g.id WHERE m.user_id=? ORDER BY g.name", (uid,))]

    def packages(self, q="", type=None, tag=None, sort="", page=1, per_page=20, include_hidden=False, viewer=None, mine=False):
        vsql, va = self.visible_sql(viewer)
        w, a = [vsql], list(va)
        if not include_hidden:
            w.append("hidden=0")
        if mine and viewer:
            w.append("EXISTS (SELECT 1 FROM package_maintainers m WHERE m.package_id=packages.id AND m.user_id=?)")
            a.append(viewer["id"])
        rank = None
        fq = self.fts_query(q) if q else ""
        if q and self.db.fts and fq:
            w.append("id IN (SELECT rowid FROM packages_fts WHERE packages_fts MATCH ?)")
            a.append(fq)
            rank = "(SELECT bm25(packages_fts,10,4,3,1) FROM packages_fts WHERE packages_fts MATCH ? AND rowid=packages.id)"
        elif q:
            w.append("(name LIKE ? OR description LIKE ? OR tags LIKE ?)")
            a += ["%" + q + "%"] * 3
        if type:
            w.append("type=?"); a.append(type)
        if tag:
            w.append("(',' || tags || ',') LIKE ?"); a.append("%," + tag + ",%")
        order = {"name": "name", "updated": "updated DESC", "created": "created DESC",
                 "downloads": "(SELECT COALESCE(SUM(downloads),0) FROM versions v WHERE v.package_id=packages.id) DESC"
                 }.get(sort or "updated", "updated DESC")
        c = self.db.conn()
        total = c.execute("SELECT COUNT(*) FROM packages WHERE " + " AND ".join(w), a).fetchone()[0]
        oargs = []
        if rank and sort in ("", "relevance"):
            order, oargs = rank, [fq]
        rows = c.execute("SELECT id,name,type,description,tags,latest_version,hidden,visibility,created,updated FROM packages "
                         "WHERE %s ORDER BY %s LIMIT ? OFFSET ?" % (" AND ".join(w), order),
                         a + oargs + [per_page, (max(page, 1) - 1) * per_page])
        return total, [self._pkg(r) for r in rows]

    def facets(self, viewer=None):
        c = self.db.conn()
        vsql, va = self.visible_sql(viewer)
        types = {r[0]: r[1] for r in c.execute("SELECT type,COUNT(*) FROM packages WHERE hidden=0 AND %s GROUP BY type" % vsql, va)}
        tags = {}
        for (t,) in c.execute("SELECT tags FROM packages WHERE hidden=0 AND %s" % vsql, va):
            for x in (t or "").split(","):
                if x:
                    tags[x] = tags.get(x, 0) + 1
        return {"types": types, "tags": tags}

    def _fts_sync(self, c, pid):
        if not self.db.fts:
            return
        c.execute("DELETE FROM packages_fts WHERE rowid=?", (pid,))
        c.execute("INSERT INTO packages_fts(rowid,name,description,tags,body) "
                  "SELECT id,name,description,REPLACE(tags,',',' '),readme FROM packages WHERE id=?", (pid,))

    @staticmethod
    def fts_query(q):
        toks = re.findall(r"\w+", q, re.U)[:8]
        return " AND ".join('"%s"*' % t for t in toks)

    def package_upsert(self, name, type, description, tags, readme="", visibility=None):
        with self.lock:
            c = self.db.conn()
            now = time.time()
            r = c.execute("SELECT id FROM packages WHERE name=?", (name,)).fetchone()
            if r:
                c.execute("UPDATE packages SET type=?,description=?,tags=?,readme=?,updated=? WHERE id=?",
                          (type, description, ",".join(tags), readme[:20000], now, r[0]))
                pid = r[0]
            else:
                pid = c.execute("INSERT INTO packages(name,type,description,tags,readme,visibility,created,updated) "
                                "VALUES(?,?,?,?,?,?,?,?)",
                                (name, type, description, ",".join(tags), readme[:20000], visibility or "public", now, now)).lastrowid
            self._fts_sync(c, pid)
            c.commit()
            return pid

    def package_patch(self, name, **f):
        with self.lock:
            c = self.db.conn()
            if "hidden" in f:
                c.execute("UPDATE packages SET hidden=? WHERE name=?", (int(f["hidden"]), name))
            if "tags" in f:
                c.execute("UPDATE packages SET tags=? WHERE name=?", (",".join(f["tags"]), name))
                r = c.execute("SELECT id FROM packages WHERE name=?", (name,)).fetchone()
                if r:
                    self._fts_sync(c, r[0])
            c.commit()

    def maintainers(self, pid):
        return _rows(self.db.conn().execute(
            "SELECT u.username,u.display_name,u.title,u.avatar_v,m.role FROM package_maintainers m JOIN users u ON u.id=m.user_id "
            "WHERE package_id=? ORDER BY m.role='owner' DESC, u.username", (pid,)))

    def maintainer_add(self, pid, uid, role="maintainer"):
        with self.lock:
            c = self.db.conn()
            c.execute("INSERT OR IGNORE INTO package_maintainers VALUES(?,?,?)", (pid, uid, role))
            c.commit()

    def maintainer_remove(self, pid, uid):
        with self.lock:
            c = self.db.conn()
            c.execute("DELETE FROM package_maintainers WHERE package_id=? AND user_id=?", (pid, uid))
            c.commit()

    def packages_of(self, uid):
        return _rows(self.db.conn().execute(
            "SELECT p.name,p.type,p.latest_version,p.visibility FROM packages p JOIN package_maintainers m ON m.package_id=p.id "
            "WHERE m.user_id=?", (uid,)))

    # ---- versions
    def versions(self, pid):
        rows = _rows(self.db.conn().execute("SELECT * FROM versions WHERE package_id=?", (pid,)))
        for r in rows:
            r["manifest"] = json.loads(r["manifest"] or "{}")
        rows.sort(key=lambda r: V.parse(r["version"]), reverse=True)
        return rows

    def version_add(self, pid, version, file, sha, size, manifest, signature=None):
        with self.lock:
            c = self.db.conn()
            c.execute("INSERT INTO versions(package_id,version,file,sha256,size,manifest,signature,created) "
                      "VALUES(?,?,?,?,?,?,?,?)", (pid, version, file, sha, size, json.dumps(manifest), signature, time.time()))
            self._relatest(c, pid)
            c.commit()

    def version_yank(self, pid, version, yanked):
        with self.lock:
            c = self.db.conn()
            c.execute("UPDATE versions SET yanked=? WHERE package_id=? AND version=?", (int(yanked), pid, version))
            self._relatest(c, pid)
            c.commit()

    def _relatest(self, c, pid):
        vs = [r[0] for r in c.execute("SELECT version FROM versions WHERE package_id=? AND yanked=0", (pid,))]
        c.execute("UPDATE packages SET latest_version=? WHERE id=?", (V.latest(vs), pid))

    def version_download(self, pid, version):
        with self.lock:
            c = self.db.conn()
            c.execute("UPDATE versions SET downloads=downloads+1 WHERE package_id=? AND version=?", (pid, version))
            c.commit()

    # ---- reviews
    def reviews(self, pid):
        return _rows(self.db.conn().execute(
            "SELECT u.username,u.display_name,u.title,u.avatar_v,r.rating,r.body,r.created FROM reviews r JOIN users u ON u.id=r.user_id "
            "WHERE package_id=? ORDER BY r.created DESC", (pid,)))

    def review_upsert(self, pid, uid, rating, body):
        with self.lock:
            c = self.db.conn()
            c.execute("INSERT OR REPLACE INTO reviews VALUES(?,?,?,?,?)", (pid, uid, rating, body, time.time()))
            c.commit()

    def rating(self, pid):
        r = self.db.conn().execute("SELECT AVG(rating),COUNT(*) FROM reviews WHERE package_id=?", (pid,)).fetchone()
        return {"avg": round(r[0], 2) if r[0] else None, "count": r[1]}

    # ---- events / stats
    def events_insert_batch(self, rows):
        if not rows:
            return
        with self.lock:
            c = self.db.conn()
            for r in rows:
                r.setdefault("source", None)
            c.executemany("INSERT INTO events(ts,kind,package,version,client_id,username,component,duration,source) "
                          "VALUES(:ts,:kind,:package,:version,:client_id,:username,:component,:duration,:source)", rows)
            c.commit()

    def stats(self, package, days=30):
        since = time.time() - days * 86400
        c = self.db.conn()
        by = {r[0]: r[1] for r in c.execute(
            "SELECT kind,COUNT(*) FROM events WHERE package=? AND ts>=? GROUP BY kind", (package, since))}
        users = c.execute("SELECT COUNT(DISTINCT COALESCE(username,client_id)) FROM events WHERE package=? AND ts>=?",
                          (package, since)).fetchone()[0]
        return {"by_kind": by, "active_users": users, "days": days}

    def package_events(self, package, limit=100):
        return _rows(self.db.conn().execute("SELECT * FROM events WHERE package=? ORDER BY id DESC LIMIT ?", (package, limit)))

    def rank(self, what, days=30, limit=20, viewer=None):
        since = time.time() - days * 86400
        vsql, va = self.visible_sql(viewer, "p")
        c = self.db.conn()
        if what == "developers":
            return _rows(c.execute(
                "SELECT u.username AS name, COUNT(e.id) AS count FROM events e JOIN packages p ON p.name=e.package "
                "JOIN package_maintainers m ON m.package_id=p.id JOIN users u ON u.id=m.user_id "
                "WHERE e.ts>=? AND " + vsql + " GROUP BY u.username ORDER BY count DESC LIMIT ?", [since] + va + [limit]))
        col = {"packages": "e.package", "users": "COALESCE(e.username,e.client_id)"}[what]
        return _rows(c.execute(
            "SELECT %s AS name, COUNT(*) AS count FROM events e JOIN packages p ON p.name=e.package "
            "WHERE e.ts>=? AND %s IS NOT NULL AND %s GROUP BY 1 ORDER BY count DESC LIMIT ?" % (col, col, vsql), [since] + va + [limit]))

    def dashboard(self, days=30, package=None, viewer=None, type=None, user=None, source=None, kind=None):
        """Aggregated usage for the dashboard. All times UTC; `days` daily buckets ending today."""
        import datetime
        days = max(1, min(int(days), 365))
        now = time.time()
        today = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date()
        start_day = today - datetime.timedelta(days=days - 1)
        since = datetime.datetime(start_day.year, start_day.month, start_day.day,
                                  tzinfo=datetime.timezone.utc).timestamp()
        prev = since - days * 86400
        c = self.db.conn()
        # scope: events of packages the viewer may see (deleted/unknown packages are excluded), plus optional filters
        vsql, va = self.visible_sql(viewer, "vp")
        pf = " AND e.package IN (SELECT vp.name FROM packages vp WHERE %s%s)" % (vsql, " AND vp.type=?" if type else "")
        pa = list(va) + ([type] if type else [])
        if package:
            pf += " AND e.package=?"; pa.append(package)
        if user:
            pf += " AND COALESCE(e.username,'anon-'||substr(e.client_id,1,6))=?"; pa.append(user)
        if source:
            pf += " AND COALESCE(e.source,'cli')=?"; pa.append(source)
        if kind:
            pf += " AND e.kind=?"; pa.append(kind)
        actor = "COALESCE(e.username, e.client_id)"

        def kinds(a, b):
            return {r[0]: r[1] for r in c.execute(
                "SELECT e.kind,COUNT(*) FROM events e WHERE e.ts>=? AND e.ts<?" + pf + " GROUP BY e.kind", [a, b] + pa)}

        def distinct(a, b, col):
            return c.execute("SELECT COUNT(DISTINCT %s) FROM events e WHERE e.ts>=? AND e.ts<?%s AND %s IS NOT NULL"
                             % (col, pf, col), [a, b] + pa).fetchone()[0]

        cur, old = kinds(since, now + 1), kinds(prev, since)
        series = {}
        for d, k, n in c.execute(
                "SELECT strftime('%Y-%m-%d',e.ts,'unixepoch'),e.kind,COUNT(*) FROM events e WHERE e.ts>=?" + pf +
                " GROUP BY 1,2", [since] + pa):
            series.setdefault(d, {})[k] = n
        daily = []
        for i in range(days):
            d = (start_day + datetime.timedelta(days=i)).isoformat()
            daily.append(dict({"date": d, "install": 0, "use": 0, "update": 0, "uninstall": 0, "error": 0}, **series.get(d, {})))
        rows = lambda q, a: [dict(r) for r in c.execute(q, a)]
        vs2, va2 = self.visible_sql(viewer, "vp")
        scope = " e.package IN (SELECT vp.name FROM packages vp WHERE %s) AND e.ts>=?" % vs2
        options = {
            "users": [r[0] for r in c.execute("SELECT DISTINCT COALESCE(e.username,'anon-'||substr(e.client_id,1,6)) n FROM events e WHERE"
                                              + scope + " ORDER BY n LIMIT 200", va2 + [since])],
            "sources": [r[0] for r in c.execute("SELECT DISTINCT COALESCE(e.source,'cli') n FROM events e WHERE" + scope + " ORDER BY n", va2 + [since])],
            "types": [r[0] for r in c.execute("SELECT DISTINCT p.type FROM packages p WHERE " + vs2.replace("vp.", "p.") + " ORDER BY 1", va2)],
        }
        return {
            "days": days, "package": package,
            "filters": {"type": type, "user": user, "source": source, "kind": kind},
            "options": options,
            "totals": cur, "previous": old,
            "active_users": distinct(since, now + 1, actor), "active_users_prev": distinct(prev, since, actor),
            "active_packages": distinct(since, now + 1, "e.package"),
            "clients": distinct(since, now + 1, "e.client_id"),
            "daily": daily,
            "top_packages": rows("SELECT e.package AS name,COUNT(*) AS count,SUM(e.kind='use') AS uses,SUM(e.kind='install') AS installs,"
                                 "SUM(e.kind='error') AS errors FROM events e WHERE e.ts>=?" + pf +
                                 " GROUP BY e.package ORDER BY count DESC LIMIT 10", [since] + pa),
            "top_users": rows("SELECT COALESCE(e.username,'anon-'||substr(e.client_id,1,6)) AS name,COUNT(*) AS count FROM events e "
                              "WHERE e.ts>=? AND (e.username IS NOT NULL OR e.client_id IS NOT NULL)" + pf +
                              " GROUP BY 1 ORDER BY count DESC LIMIT 10", [since] + pa),
            "by_type": rows("SELECT COALESCE(p.type,'unknown') AS name,COUNT(*) AS count FROM events e "
                            "LEFT JOIN packages p ON p.name=e.package WHERE e.ts>=?" + pf + " GROUP BY COALESCE(p.type,'unknown') ORDER BY count DESC", [since] + pa),
            "by_source": rows("SELECT COALESCE(e.source,'cli') AS name,COUNT(*) AS count FROM events e WHERE e.ts>=?" + pf +
                              " GROUP BY 1 ORDER BY count DESC", [since] + pa),
            "recent": rows("SELECT e.ts,e.kind,e.package,e.version,e.component,COALESCE(e.username,'anon-'||substr(e.client_id,1,6)) AS actor,"
                           "e.source FROM events e WHERE e.ts>=?" + pf + " ORDER BY e.id DESC LIMIT 20", [since] + pa),
        }

    def overview(self, viewer=None):
        c = self.db.conn()
        vsql, va = self.visible_sql(viewer)
        return {"packages": c.execute("SELECT COUNT(*) FROM packages WHERE " + vsql, va).fetchone()[0],
                "versions": c.execute("SELECT COUNT(*) FROM versions v JOIN packages ON packages.id=v.package_id WHERE " + vsql, va).fetchone()[0],
                "users": c.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                "downloads": c.execute("SELECT COALESCE(SUM(v.downloads),0) FROM versions v JOIN packages ON packages.id=v.package_id WHERE " + vsql, va).fetchone()[0]}


class EventBuffer:
    """Non-blocking usage-event queue. Requests only append; a worker thread batches into SQLite
    every `max_secs` or as soon as `max_rows` are queued. Bounded so a flood can't exhaust memory."""
    MAX_QUEUE = 100_000

    def __init__(self, repos, max_rows=50, max_secs=5.0):
        self.repos, self.max_rows, self.max_secs = repos, max_rows, max_secs
        self.buf, self.lock = [], threading.Lock()
        self.wake, self.stop = threading.Event(), threading.Event()
        self.t = threading.Thread(target=self._loop, name="event-flusher", daemon=True)
        self.t.start()

    def add(self, rows):
        with self.lock:
            self.buf += rows
            if len(self.buf) > self.MAX_QUEUE:
                del self.buf[: len(self.buf) - self.MAX_QUEUE]
            full = len(self.buf) >= self.max_rows
        if full:
            self.wake.set()

    def flush(self):
        with self.lock:
            rows, self.buf = self.buf, []
        if rows:
            try:
                self.repos.events_insert_batch(rows)
            except Exception:
                with self.lock:  # keep for retry
                    self.buf = rows + self.buf
                raise

    def _loop(self):
        while not self.stop.is_set():
            self.wake.wait(self.max_secs)
            self.wake.clear()
            try:
                self.flush()
            except Exception:
                self.stop.wait(1)

    def close(self):
        self.stop.set()
        self.wake.set()
        self.t.join(timeout=5)
        self.flush()
