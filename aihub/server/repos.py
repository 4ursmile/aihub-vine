"""Repository layer: the only module that writes SQL."""
import json
import re
import sqlite3
import threading
import time
import zlib

from ..core import version as V
from .db import ALL_PERMS, ANON_LOCKED


# Who an event belongs to: the signed-in account, else the OS user on the machine that ran it, else a short client id.
ACTOR = ("COALESCE(e.username, CASE WHEN COALESCE(e.local_user,'')<>'' THEN e.local_user||' (local)' END, "
         "'anon-'||substr(e.client_id,1,6))")


def _rows(cur):
    return [dict(r) for r in cur.fetchall()]


def _integrity_errors():
    errs = [sqlite3.IntegrityError]
    try:
        import psycopg
        errs.append(psycopg.errors.UniqueViolation)
    except ImportError:
        pass
    return tuple(errs)


INTEGRITY = _integrity_errors()

# Bayesian average: (C*m + sum(ratings)) / (C + n). A package with few reviews is pulled toward the site-wide mean m,
# so one 5-star review can't outrank 200 reviews averaging 4.8. C = how many reviews it takes to "count" fully.
BAYES_C = 5


class Repos:
    def __init__(self, db):
        self.db = db
        self.lock = threading.Lock()  # serialize writers

    # ---- users / auth
    def user_by_name(self, u):
        r = self.db.conn().execute("SELECT * FROM users WHERE username=?", (u,)).fetchone()
        return dict(r) if r else None

    def user_count(self):
        return self.db.conn().execute("SELECT COUNT(*) FROM users WHERE role<>'anonymous'").fetchone()[0]

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
            "WHERE role<>'anonymous' AND (username LIKE ? OR display_name LIKE ?) ORDER BY id", ("%" + q + "%", "%" + q + "%")))

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
        got = {r[0] for r in self.db.conn().execute("SELECT permission FROM role_permissions WHERE role=?", (role,))}
        return got - set(ANON_LOCKED) if role == "anonymous" else got

    def anon_user(self):
        """The shared, un-loginable account that actions by signed-out visitors are attributed to."""
        u = self.user_by_name("anonymous")
        if not u:
            try:
                self.user_create("anonymous", "!", "anonymous", "disabled")
            except INTEGRITY:
                pass
            u = self.user_by_name("anonymous")
        return u

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
        counts = {r[0]: r[1] for r in c.execute("SELECT role,COUNT(*) FROM users WHERE role<>'anonymous' GROUP BY role")}
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
        """rows: [(username, pw_hash, role, display_name, title[, [group_id, ...]])] -> set of usernames created.
        Skips existing names. New users join their groups in the same transaction."""
        made = set()
        with self.lock:
            c = self.db.conn()
            now = time.time()
            for row in rows:
                u, h, role, dn, title = row[:5]
                gids = row[5] if len(row) > 5 else []
                c.execute("SAVEPOINT row_sp")          # a duplicate must undo only this row, never the earlier ones
                try:
                    cur = c.execute("INSERT INTO users(username,password_hash,role,status,display_name,title,created) VALUES(?,?,?,?,?,?,?)",
                                    (u, h, role, "active", dn, title, now))
                    for gid in gids:
                        c.execute("INSERT OR IGNORE INTO group_members VALUES(?,?)", (gid, cur.lastrowid))
                    c.execute("RELEASE SAVEPOINT row_sp")
                    made.add(u)
                except INTEGRITY:
                    c.execute("ROLLBACK TO SAVEPOINT row_sp")
                    c.execute("RELEASE SAVEPOINT row_sp")
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

    @staticmethod
    def _like(v):
        return "%" + str(v).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    def audit_search(self, source="tools", q="", actor="", action="", package="", kind="", since=0, until=0, limit=100, offset=0):
        """Security audit. source='tools': agent tool calls and installs (events). source='admin': account/role/setting changes."""
        c, args, w = self.db.conn(), [], ["1=1"]
        if source == "admin":
            t, tscol, ts = "audit_log a", "a.ts", "a.id,a.ts,a.actor,a.action,a.target,a.detail"
            if actor: w.append("a.actor=?"); args.append(actor)
            if action: w.append("a.action LIKE ? ESCAPE '\\'"); args.append(self._like(action))
            if package: w.append("a.target LIKE ? ESCAPE '\\'"); args.append(self._like(package))
            if q:
                w.append("(a.actor LIKE ? ESCAPE '\\' OR a.action LIKE ? ESCAPE '\\' OR a.target LIKE ? ESCAPE '\\' OR a.detail LIKE ? ESCAPE '\\')")
                args += [self._like(q)] * 4
        else:
            t, tscol = "events e", "e.ts"
            ts = ("e.id,e.ts,e.kind,e.package,e.version,e.component," + ACTOR + " AS actor,e.username,e.local_user,e.host,"
                  "e.source,e.detail,e.cwd,e.ip,e.client_id")
            if actor: w.append(ACTOR + "=?"); args.append(actor)
            if kind: w.append("e.kind=?"); args.append(kind)
            if package: w.append("e.package=?"); args.append(package)
            if action: w.append("e.component LIKE ? ESCAPE '\\'"); args.append(self._like(action))
            if q:
                w.append("(e.detail LIKE ? ESCAPE '\\' OR e.component LIKE ? ESCAPE '\\' OR e.package LIKE ? ESCAPE '\\' OR e.cwd LIKE ? ESCAPE '\\' "
                         "OR e.host LIKE ? ESCAPE '\\' OR " + ACTOR + " LIKE ? ESCAPE '\\')")
                args += [self._like(q)] * 6
        if since: w.append(tscol + ">=?"); args.append(since)
        if until: w.append(tscol + "<?"); args.append(until)
        where = " WHERE " + " AND ".join(w)
        total = c.execute("SELECT COUNT(*) FROM " + t + where, args).fetchone()[0]
        rows = _rows(c.execute("SELECT " + ts + " FROM " + t + where + " ORDER BY " + tscol + " DESC, " + t.split()[1] + ".id DESC LIMIT ? OFFSET ?",
                               args + [limit, offset]))
        return {"total": int(total), "items": rows}

    def audit_actors(self):
        c = self.db.conn()
        return {"tools": [r[0] for r in c.execute("SELECT DISTINCT " + ACTOR + " n FROM events e ORDER BY n LIMIT 500")],
                "admin": [r[0] for r in c.execute("SELECT DISTINCT actor FROM audit_log ORDER BY actor LIMIT 500")]}

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

    def initial_visibility(self, requested=None):
        """Visibility for a package that first appears: the request, else the site default, never private when the site disallows it."""
        vis = requested or self.setting("default_visibility") or "public"
        if vis not in ("public", "private") or (vis == "private" and self.setting("allow_private", "1") != "1"):
            return "public"
        return vis

    def package_readme(self, pid):
        r = self.db.conn().execute("SELECT readme FROM packages WHERE id=?", (pid,)).fetchone()
        return (r[0] if r else "") or ""

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

    def group_ids_by_name(self, names):
        c = self.db.conn()
        out = {}
        for n in names:
            r = c.execute("SELECT id FROM groups WHERE name=?", (n,)).fetchone()
            if r:
                out[n] = r[0]
        return out

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

    def packages(self, q="", type=None, tag=None, sort="", page=1, per_page=20, include_hidden=False, viewer=None, mine=False, category=None):
        vsql, va = self.visible_sql(viewer)
        w, a = [vsql], list(va)
        if not include_hidden:
            w.append("hidden=0")
        if mine and viewer:
            w.append("EXISTS (SELECT 1 FROM package_maintainers m WHERE m.package_id=packages.id AND m.user_id=?)")
            a.append(viewer["id"])
        rank, rank_args = None, []
        fq = self.fts_query(q) if q else ""
        if q and self.db.engine == "postgres" and fq:
            w.append("fts @@ to_tsquery('simple', ?)"); a.append(fq)
            rank, rank_args = "ts_rank(fts, to_tsquery('simple', ?)) DESC", [fq]
        elif q and self.db.fts and fq:
            w.append("id IN (SELECT rowid FROM packages_fts WHERE packages_fts MATCH ?)")
            a.append(fq)
            rank, rank_args = "(SELECT bm25(packages_fts,10,4,3,1) FROM packages_fts WHERE packages_fts MATCH ? AND rowid=packages.id)", [fq]
        elif q:
            w.append("(name LIKE ? OR description LIKE ? OR tags LIKE ?)")
            a += ["%" + q + "%"] * 3
        if type:
            w.append("type=?"); a.append(type)
        if tag:
            w.append("(',' || tags || ',') LIKE ?"); a.append("%," + tag + ",%")
        if category:
            # category is derived (tags/type), not stored: resolve the matching names first, then filter by them
            names = [n for n, cid in self.package_categories(viewer, include_hidden).items() if cid == category]
            if not names:
                w.append("1=0")
            else:
                w.append("name IN (%s)" % ",".join("?" * len(names))); a += names
        c = self.db.conn()
        rsum = "(SELECT COALESCE(SUM(rating),0) FROM reviews r WHERE r.package_id=packages.id)"
        rcnt = "(SELECT COUNT(*) FROM reviews r WHERE r.package_id=packages.id)"
        site_mean = c.execute("SELECT COALESCE(AVG(rating),0) FROM reviews").fetchone()[0] or 0
        order_args = []
        order = {"name": "name", "updated": "updated DESC", "created": "created DESC",
                 "downloads": "(SELECT COALESCE(SUM(downloads),0) FROM versions v WHERE v.package_id=packages.id) DESC",
                 # best reviewed: Bayesian average; ties (and unreviewed packages) fall back to most reviews, then name
                 # Unreviewed packages sort strictly last: with a plain Bayesian prior they would get exactly the site mean and
                 # outrank packages people actually rated poorly. Reviewed ones order by the Bayesian score, then review count.
                 "rating": "(CASE WHEN %s > 0 THEN 0 ELSE 1 END), ((%d * ? + %s) * 1.0 / (%d + %s)) DESC, %s DESC, name" % (rcnt, BAYES_C, rsum, BAYES_C, rcnt, rcnt),
                 "reviews": "%s DESC, name" % rcnt,
                 }.get(sort or "updated", "updated DESC")
        if sort == "rating":
            order_args = [site_mean]
        total = c.execute("SELECT COUNT(*) FROM packages WHERE " + " AND ".join(w), a).fetchone()[0]
        if rank and sort in ("", "relevance"):
            order, order_args = rank, rank_args
        rows = c.execute("SELECT id,name,type,description,tags,latest_version,hidden,visibility,created,updated FROM packages "
                         "WHERE %s ORDER BY %s LIMIT ? OFFSET ?" % (" AND ".join(w), order),
                         a + order_args + [per_page, (max(page, 1) - 1) * per_page])
        return total, [self._pkg(r) for r in rows]

    def package_categories(self, viewer=None, include_hidden=False):
        """{name: category id} for packages the viewer may see."""
        from .categories import category_of
        vsql, va = self.visible_sql(viewer)
        extra = "" if include_hidden else " AND hidden=0"
        return {n: category_of([x for x in (t or "").split(",") if x], ty)
                for n, ty, t in self.db.conn().execute("SELECT name,type,tags FROM packages WHERE %s%s" % (vsql, extra), va)}

    def related(self, name, viewer=None, limit=6):
        """Visible packages ranked by shared tags (x3), same category (x2), same type (x1). Excludes the package itself."""
        from .categories import category_of
        vsql, va = self.visible_sql(viewer)
        rows = self.db.conn().execute("SELECT name,type,tags,description,latest_version FROM packages WHERE hidden=0 AND %s" % vsql, va).fetchall()
        tg = lambda t: {x for x in (t or "").split(",") if x}
        me = next((r for r in rows if r[0] == name), None)
        if me is None:
            row = self.db.conn().execute("SELECT name,type,tags,description,latest_version FROM packages WHERE name=?", (name,)).fetchone()
            me = row
        if me is None:
            return []
        mt, mc = tg(me[2]), category_of(tg(me[2]), me[1])
        scored = []
        for n, ty, t, d, v in rows:
            if n == name:
                continue
            s = 3 * len(mt & tg(t)) + (2 if category_of(tg(t), ty) == mc else 0) + (1 if ty == me[1] else 0)
            if s > 0:
                scored.append((-s, n))
        scored.sort()
        return [n for _, n in scored[:limit]]

    def recent_activity(self, viewer=None, limit=15):
        """Latest install/use/update events on packages the viewer may see. Deliberately carries no user or client identifiers."""
        vsql, va = self.visible_sql(viewer, "p")
        return _rows(self.db.conn().execute(
            "SELECT e.package AS package, e.kind AS kind, e.ts AS ts, p.type AS type FROM events e JOIN packages p ON p.name=e.package "
            "WHERE e.kind IN ('install','use','update') AND p.hidden=0 AND " + vsql + " ORDER BY e.id DESC LIMIT ?", va + [limit]))

    def facets(self, viewer=None):
        from .categories import BY_ID
        c = self.db.conn()
        vsql, va = self.visible_sql(viewer)
        cats = {k: 0 for k in BY_ID}
        for cid in self.package_categories(viewer).values():
            cats[cid] += 1
        types = {r[0]: r[1] for r in c.execute("SELECT type,COUNT(*) FROM packages WHERE hidden=0 AND %s GROUP BY type" % vsql, va)}
        tags = {}
        for (t,) in c.execute("SELECT tags FROM packages WHERE hidden=0 AND %s" % vsql, va):
            for x in (t or "").split(","):
                if x:
                    tags[x] = tags.get(x, 0) + 1
        return {"types": types, "tags": tags,
                "categories": [{"id": k, "label": BY_ID[k]["label"], "desc": BY_ID[k]["desc"], "count": n} for k, n in cats.items()]}

    def _fts_sync(self, c, pid):
        if self.db.engine == "postgres" or not self.db.fts:
            return                                     # Postgres keeps its generated tsvector column up to date
        c.execute("DELETE FROM packages_fts WHERE rowid=?", (pid,))
        c.execute("INSERT INTO packages_fts(rowid,name,description,tags,body) "
                  "SELECT id,name,description,REPLACE(tags,',',' '),readme FROM packages WHERE id=?", (pid,))

    def fts_query(self, q):
        toks = re.findall(r"\w+", q, re.U)[:8]
        if self.db.engine == "postgres":
            return " & ".join("%s:*" % t for t in toks)    # to_tsquery prefix match
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
            "SELECT COALESCE(u.username,'') AS username,COALESCE(u.display_name,'') AS display_name,COALESCE(u.title,'') AS title,COALESCE(u.avatar_v,0) AS avatar_v,u.role,r.reviewer,r.rating,r.body,r.created FROM reviews r LEFT JOIN users u ON u.id=r.user_id "
            "WHERE package_id=? ORDER BY r.created DESC", (pid,)))

    def review_upsert(self, pid, uid, rating, body, reviewer=None):
        """One review per person per package. Signed-out visitors all share one account, so `reviewer` (the typed name) is stored
        with the review and a second review under the same name replaces the first, never someone else's."""
        with self.lock:
            c = self.db.conn()
            if reviewer:
                # the (package, user) primary key allows one row per account, so each anonymous name gets a stable negative key of its own
                key = -(zlib.crc32(reviewer.lower().encode()) + 1)
                c.execute("INSERT OR REPLACE INTO reviews(package_id,user_id,rating,body,created,reviewer) VALUES(?,?,?,?,?,?)",
                          (pid, key, rating, body, time.time(), reviewer))
            else:
                c.execute("INSERT OR REPLACE INTO reviews(package_id,user_id,rating,body,created) VALUES(?,?,?,?,?)", (pid, uid, rating, body, time.time()))
            c.commit()

    def rating(self, pid):
        r = self.db.conn().execute("SELECT AVG(rating),COUNT(*) FROM reviews WHERE package_id=?", (pid,)).fetchone()
        return {"avg": round(float(r[0]), 2) if r[0] else None, "count": int(r[1])}

    # ---- events / stats
    def events_insert_batch(self, rows):
        if not rows:
            return
        with self.lock:
            c = self.db.conn()
            for r in rows:
                for k in ("source", "local_user", "host", "detail", "cwd", "ip"):
                    r.setdefault(k, None)
            c.executemany("INSERT INTO events(ts,kind,package,version,client_id,username,component,duration,source,local_user,host,detail,cwd,ip) "
                          "VALUES(:ts,:kind,:package,:version,:client_id,:username,:component,:duration,:source,:local_user,:host,:detail,:cwd,:ip)", rows)
            c.commit()

    def events_insert_ext(self, rows):
        """Insert events keyed by ext_id (a Langfuse observation id). Already-seen ids are skipped. -> number inserted."""
        if not rows:
            return 0
        with self.lock:
            c = self.db.conn()
            before = c.execute("SELECT COUNT(*) FROM events WHERE ext_id IS NOT NULL").fetchone()[0]
            for r in rows:
                for k in ("source", "local_user", "host", "detail", "cwd", "ip", "version", "client_id", "component", "duration"):
                    r.setdefault(k, None)
            c.executemany("INSERT OR IGNORE INTO events(ts,kind,package,version,client_id,username,component,duration,source,local_user,host,detail,cwd,ip,ext_id) "
                          "VALUES(:ts,:kind,:package,:version,:client_id,:username,:component,:duration,:source,:local_user,:host,:detail,:cwd,:ip,:ext_id)", rows)
            c.commit()
            return c.execute("SELECT COUNT(*) FROM events WHERE ext_id IS NOT NULL").fetchone()[0] - before

    def version_sync(self, pid, version, manifest, sha="", file=None):
        """Add a version from the git index if new; refresh its manifest if it changed. -> True when new."""
        with self.lock:
            c = self.db.conn()
            row = c.execute("SELECT id,manifest FROM versions WHERE package_id=? AND version=?", (pid, version)).fetchone()
            js = json.dumps(manifest)
            if row:
                if row[1] != js:
                    c.execute("UPDATE versions SET manifest=? WHERE id=?", (js, row[0]))
                    c.commit()
                return False
            c.execute("INSERT INTO versions(package_id,version,file,sha256,size,manifest,signature,created) VALUES(?,?,?,?,?,?,?,?)",
                      (pid, version, file, sha, 0, js, None, time.time()))
            self._relatest(c, pid)
            c.commit()
            return True

    def package_hide_missing(self, keep_names):
        """Hide packages that were synced from the index earlier but are no longer in it (set recorded in app_settings)."""
        prev = [x for x in (self.setting("sync_names", "") or "").split(",") if x]
        gone = [n for n in prev if n not in keep_names]
        with self.lock:
            c = self.db.conn()
            for n in gone:
                c.execute("UPDATE packages SET hidden=1 WHERE name=?", (n,))
            for n in keep_names:
                c.execute("UPDATE packages SET hidden=0 WHERE name=? AND hidden=1", (n,))
            c.commit()
        self.set_setting("sync_names", ",".join(sorted(keep_names)))
        return gone

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
        if what in ("reviews", "reviewed"):
            return self.top_reviewed(what, limit, viewer)
        if what == "developers":
            return self._top_developers(since, " AND " + vsql, va, limit)
        col = {"packages": "e.package", "users": ACTOR}[what]
        return _rows(c.execute(
            "SELECT %s AS name, COUNT(*) AS count FROM events e JOIN packages p ON p.name=e.package "
            "WHERE e.ts>=? AND %s IS NOT NULL AND %s GROUP BY 1 ORDER BY count DESC LIMIT ?" % (col, col, vsql), [since] + va + [limit]))

    def _top_developers(self, since, flt, args, limit, until=None):
        """Usage events per package, credited to every author in the latest live manifest (each author gets the full count).
        A package that lists no authors credits its maintainers instead. `flt` is extra SQL on events e / packages p, starting
        with AND (the rankings page passes visibility; the dashboard passes its whole filter set), so both pages agree."""
        c = self.db.conn()
        used = c.execute("SELECT p.id, COUNT(e.id) FROM events e JOIN packages p ON p.name=e.package "
                         "WHERE e.ts>=?" + (" AND e.ts<?" if until else "") + flt + " GROUP BY p.id",
                         [since] + ([until] if until else []) + list(args)).fetchall()
        score = {}
        for pid, n in used:
            live = [v for v in self.versions(pid) if not v["yanked"]]
            authors = [a.get("name") or a.get("email") for a in (((live[0]["manifest"] if live else {}).get("package") or {}).get("authors") or [])]
            if not any(authors):
                authors = [m["username"] for m in self.maintainers(pid)]
            for a in {x.strip() for x in authors if x and x.strip()}:
                score[a] = score.get(a, 0) + n
        return [{"name": k, "count": v} for k, v in sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]

    def top_reviewed(self, mode, limit=20, viewer=None):
        """Best-rated packages (Bayesian average, so one 5-star review can't beat 200 reviews at 4.8) or most-reviewed.
        All-time, not windowed: reviews are a lasting quality signal. Only packages the viewer may see."""
        vsql, va = self.visible_sql(viewer, "p")
        c = self.db.conn()
        mean = c.execute("SELECT COALESCE(AVG(r.rating),0) FROM reviews r JOIN packages p ON p.id=r.package_id WHERE " + vsql, va).fetchone()[0] or 0
        order = "score DESC, n DESC, name" if mode == "reviews" else "n DESC, avg DESC, name"
        return _rows(c.execute(
            "SELECT p.name AS name, COUNT(r.rating) AS n, AVG(r.rating) AS avg, "
            "((%d * ? + SUM(r.rating)) * 1.0 / (%d + COUNT(r.rating))) AS score, COUNT(r.rating) AS count "
            "FROM reviews r JOIN packages p ON p.id=r.package_id WHERE %s AND p.hidden=0 "
            "GROUP BY p.id, p.name ORDER BY %s LIMIT ?" % (BAYES_C, BAYES_C, vsql, order), [mean] + va + [limit]))

    def _window(self, days, date_from=None, date_to=None):
        """-> (days, start_day, since, until). A custom from/to (YYYY-MM-DD, UTC) overrides the preset `days`."""
        import datetime
        now = time.time()
        today = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date()
        try:
            end = datetime.date.fromisoformat(date_to) if date_to else today
            start = datetime.date.fromisoformat(date_from) if date_from else None
        except ValueError:
            raise ValueError("dates must be YYYY-MM-DD")
        end = min(end, today)
        if start is None:
            start = end - datetime.timedelta(days=max(1, min(int(days), 365)) - 1)
        if start > end:
            raise ValueError("'from' must not be after 'to'")
        if (end - start).days >= 366:
            raise ValueError("range is limited to 366 days")
        utc = datetime.timezone.utc
        since = datetime.datetime(start.year, start.month, start.day, tzinfo=utc).timestamp()
        until = datetime.datetime(end.year, end.month, end.day, tzinfo=utc).timestamp() + 86400
        return (end - start).days + 1, start, since, until

    def _dash_filter(self, viewer, type=None, package=None, user=None, source=None, kind=None,
                     identity=None, host=None, q=None):
        """-> (sql, args) appended to `... WHERE e.ts>=? ...`. Only events of packages the viewer may see."""
        vsql, va = self.visible_sql(viewer, "vp")
        pf = " AND e.package IN (SELECT vp.name FROM packages vp WHERE %s%s)" % (vsql, " AND vp.type=?" if type else "")
        pa = list(va) + ([type] if type else [])
        if package:
            pf += " AND e.package=?"; pa.append(package)
        if user:
            pf += " AND " + ACTOR + "=?"; pa.append(user)
        if source:
            pf += " AND COALESCE(e.source,'cli')=?"; pa.append(source)
        if kind:
            pf += " AND e.kind=?"; pa.append(kind)
        if identity == "anonymous":
            pf += " AND e.username IS NULL"
        elif identity == "signed-in":
            pf += " AND e.username IS NOT NULL"
        if host:
            pf += " AND e.host=?"; pa.append(host)
        if q:
            pf += " AND (e.package LIKE ? ESCAPE '\\' OR e.component LIKE ? ESCAPE '\\')"
            pa += [self._like(q)] * 2
        return pf, pa

    def dashboard(self, days=30, package=None, viewer=None, type=None, user=None, source=None, kind=None,
                  identity=None, host=None, q=None, date_from=None, date_to=None):
        """Aggregated usage for the dashboard. All times UTC; one daily bucket per day of the window."""
        import datetime
        days, start_day, since, until = self._window(days, date_from, date_to)
        now = until - 1
        prev = since - days * 86400
        c = self.db.conn()
        pf, pa = self._dash_filter(viewer, type, package, user, source, kind, identity, host, q)
        pf += " AND e.ts<?"; pa.append(until)
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
            daily.append(dict({"date": d, "install": 0, "use": 0, "update": 0, "uninstall": 0, "error": 0, "publish": 0}, **series.get(d, {})))
        rows = lambda q, a: [dict(r) for r in c.execute(q, a)]
        vs2, va2 = self.visible_sql(viewer, "vp")
        scope = " e.package IN (SELECT vp.name FROM packages vp WHERE %s) AND e.ts>=?" % vs2
        options = {
            "users": [r[0] for r in c.execute("SELECT DISTINCT " + ACTOR + " n FROM events e WHERE"
                                              + scope + " ORDER BY n LIMIT 200", va2 + [since])],
            "sources": [r[0] for r in c.execute("SELECT DISTINCT COALESCE(e.source,'cli') n FROM events e WHERE" + scope + " ORDER BY n", va2 + [since])],
            "types": [r[0] for r in c.execute("SELECT DISTINCT p.type FROM packages p WHERE " + vs2.replace("vp.", "p.") + " ORDER BY 1", va2)],
            "hosts": [r[0] for r in c.execute("SELECT DISTINCT e.host n FROM events e WHERE e.host IS NOT NULL AND" + scope + " ORDER BY n LIMIT 200", va2 + [since])],
        }
        stamps = [r[0] for r in c.execute("SELECT e.ts FROM events e WHERE e.ts>=?" + pf + " LIMIT 50000", [since] + pa)]
        heat = [[0] * 24 for _ in range(7)]                              # [weekday Mon..Sun][hour UTC]
        for t in stamps:
            d = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
            heat[d.weekday()][d.hour] += 1
        return {
            "days": days, "package": package,
            "filters": {"type": type, "user": user, "source": source, "kind": kind, "identity": identity, "host": host, "q": q,
                        "from": start_day.isoformat(), "to": (start_day + datetime.timedelta(days=days - 1)).isoformat()},
            "heatmap": heat,
            "top_components": rows("SELECT e.component AS name,e.package AS package,COUNT(*) AS count FROM events e "
                                   "WHERE e.component IS NOT NULL AND e.ts>=?" + pf + " GROUP BY e.component,e.package ORDER BY count DESC LIMIT 10", [since] + pa),
            "by_host": rows("SELECT COALESCE(e.host,'unknown') AS name,COUNT(*) AS count FROM events e WHERE e.ts>=?" + pf +
                            " GROUP BY 1 ORDER BY count DESC LIMIT 10", [since] + pa),
            "by_identity": rows("SELECT CASE WHEN e.username IS NULL THEN 'anonymous' ELSE 'signed in' END AS name,COUNT(*) AS count FROM events e "
                                "WHERE e.ts>=?" + pf + " GROUP BY 1 ORDER BY count DESC", [since] + pa),
            "options": options,
            "totals": cur, "previous": old,
            "active_users": distinct(since, now + 1, actor), "active_users_prev": distinct(prev, since, actor),
            "active_packages": distinct(since, now + 1, "e.package"),
            "clients": distinct(since, now + 1, "e.client_id"),
            "daily": daily,
            "top_packages": rows("SELECT e.package AS name,COUNT(*) AS count,SUM(e.kind='use') AS uses,SUM(e.kind='install') AS installs,"
                                 "SUM(e.kind='error') AS errors FROM events e WHERE e.ts>=?" + pf +
                                 " GROUP BY e.package ORDER BY count DESC LIMIT 10", [since] + pa),
            "top_reviewed": [dict(x, avg=round(float(x["avg"]), 2), score=round(float(x["score"]), 2)) for x in self.top_reviewed("reviews", 10, viewer)],
            "top_developers": self._top_developers(since, pf, pa, 10),
            "top_users": rows("SELECT " + ACTOR + " AS name,COUNT(*) AS count FROM events e "
                              "WHERE e.ts>=? AND (e.username IS NOT NULL OR e.client_id IS NOT NULL)" + pf +
                              " GROUP BY 1 ORDER BY count DESC LIMIT 10", [since] + pa),
            "by_type": rows("SELECT COALESCE(p.type,'unknown') AS name,COUNT(*) AS count FROM events e "
                            "LEFT JOIN packages p ON p.name=e.package WHERE e.ts>=?" + pf + " GROUP BY COALESCE(p.type,'unknown') ORDER BY count DESC", [since] + pa),
            "by_source": rows("SELECT COALESCE(e.source,'cli') AS name,COUNT(*) AS count FROM events e WHERE e.ts>=?" + pf +
                              " GROUP BY 1 ORDER BY count DESC", [since] + pa),
            "recent": rows("SELECT e.ts,e.kind,e.package,e.version,e.component," + ACTOR + " AS actor,"
                           "e.source FROM events e WHERE e.ts>=?" + pf + " ORDER BY e.id DESC LIMIT 20", [since] + pa),
        }

    def dashboard_events(self, viewer, days=30, date_from=None, date_to=None, page=1, per_page=25, **flt):
        """Paginated activity log under the same filters. Deliberately omits detail/cwd/ip (those stay on the audit page)."""
        days, start_day, since, until = self._window(days, date_from, date_to)
        pf, pa = self._dash_filter(viewer, **flt)
        pf += " AND e.ts<?"; pa.append(until)
        c = self.db.conn()
        total = c.execute("SELECT COUNT(*) FROM events e WHERE e.ts>=?" + pf, [since] + pa).fetchone()[0]
        per_page = max(1, min(int(per_page), 100)); page = max(1, int(page))
        items = [dict(r) for r in c.execute(
            "SELECT e.ts,e.kind,e.package,e.version,e.component," + ACTOR + " AS actor,COALESCE(e.source,'cli') AS source,e.host "
            "FROM events e WHERE e.ts>=?" + pf + " ORDER BY e.ts DESC,e.id DESC LIMIT ? OFFSET ?",
            [since] + pa + [per_page, (page - 1) * per_page])]
        return {"total": total, "page": page, "per_page": per_page, "items": items}

    def overview(self, viewer=None):
        c = self.db.conn()
        vsql, va = self.visible_sql(viewer)
        vsql = "packages.hidden=0 AND " + vsql              # same set the browse page and category counts use
        return {"packages": c.execute("SELECT COUNT(*) FROM packages WHERE " + vsql, va).fetchone()[0],
                "versions": c.execute("SELECT COUNT(*) FROM versions v JOIN packages ON packages.id=v.package_id WHERE " + vsql, va).fetchone()[0],
                "users": c.execute("SELECT COUNT(*) FROM users WHERE status='active'").fetchone()[0],
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
            finally:
                self.repos.db.release()       # this thread's pooled connection goes back after every batch

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
