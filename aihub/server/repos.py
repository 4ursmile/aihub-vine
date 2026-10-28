"""Repository layer: the only module that writes SQL."""
import hashlib
import json
import secrets
import logging
import re
import sqlite3
import threading
import time
import zlib

from ..core import naming, pkgindex, textsafe, version as V
from .db import ALL_PERMS, ANON_LOCKED
from .spaces_repo import SpaceRepos

log = logging.getLogger("aihub")


# Who an event belongs to when "localize user names" is off: the account, else the OS user, else a short client id.
ACTOR = ("COALESCE(e.username, CASE WHEN COALESCE(e.local_user,'')<>'' THEN e.local_user||' (local)' END, "
         "'anon-'||substr(e.client_id,1,6))")
# ... and when it is on: the stored `actor` key (see actor_key), so one person is one name however they ran the CLI.
ACTOR_LOCAL = "COALESCE(NULLIF(e.actor,''), 'anon-'||substr(e.client_id,1,6))"


def actor_key(username=None, local_user=None):
    """One lowercase name per person: the hub username, else the OS user. A leading ~ (anonymous marker), a "(local)" suffix and
    an e-mail domain are ignored, so `Sam`, `sam@corp.com` and the OS user `sam` are the same actor. None when nothing is known."""
    for raw in (username, local_user):
        n = str(raw or "").strip().lstrip("~").replace(" (local)", "").split("@")[0].strip().lower()
        if n:
            return n[:64]
    return None


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


def _data_errors():
    """Errors that mean THIS row's content is bad (retrying the same row can never work), as opposed to the database being busy or down."""
    errs = [UnicodeError, ValueError, TypeError, OverflowError, sqlite3.InterfaceError, sqlite3.DataError]
    try:
        import psycopg
        # NOT ProgrammingError: on Postgres that is also "undefined table/column" and syntax errors, i.e. a broken schema. Skipping every row
        # for that would drop a whole window of acknowledged events silently; it must fail the batch so the rows are kept and retried.
        errs += [psycopg.errors.DataError, psycopg.errors.NotNullViolation, psycopg.errors.CheckViolation]
    except ImportError:
        pass
    return tuple(errs)


DATA_ERRORS = _data_errors()


def row_is_bad(e):
    """True when exception `e` blames THIS row's content (retrying the same row can never work), false when the database or its schema is at
    fault (then the batch must be kept and retried, never skipped)."""
    if isinstance(e, sqlite3.ProgrammingError):          # SQLite: "Error binding parameter N: type 'x' is not supported" (a missing table is an OperationalError)
        return "binding parameter" in str(e)
    return isinstance(e, DATA_ERRORS)
EVENT_COLS = ("ts,kind,package,version,client_id,username,component,duration,source,local_user,host,os,detail,cwd,ip,actor")
EVENT_VALS = ":ts,:kind,:package,:version,:client_id,:username,:component,:duration,:source,:local_user,:host,:os,:detail,:cwd,:ip,:actor"

# Bayesian average: (C*m + sum(ratings)) / (C + n). A package with few reviews is pulled toward the site-wide mean m,
# so one 5-star review can't outrank 200 reviews averaging 4.8. C = how many reviews it takes to "count" fully.
BAYES_C = 5
# downloads of `packages` (the row in scope): legacy HTTP downloads + install/update events, as the dashboard counts them
DOWNLOADS_SQL = ("(SELECT COALESCE(SUM(v.downloads),0) FROM versions v WHERE v.package_id=packages.id)"
                 " + (SELECT COUNT(*) FROM events e WHERE e.package=packages.name AND e.kind IN ('install','update'))")


class Repos(SpaceRepos):
    def __init__(self, db):
        self.db = db
        self.lock = threading.Lock()  # serialize writers

    def localize(self):
        """Admin setting `localize_users` (default on): count one person once, by name, whatever way they ran the CLI."""
        return self.setting("localize_users", "1") != "0"

    def actor_sql(self):
        return ACTOR_LOCAL if self.localize() else ACTOR

    def backfill_actors(self):
        """Fill events.actor for rows stored before the column existed (cheap no-op once done)."""
        with self.lock:
            c = self.db.conn()
            rows = c.execute("SELECT DISTINCT username,local_user FROM events WHERE actor IS NULL").fetchall()
            for r in rows:
                c.execute("UPDATE events SET actor=? WHERE actor IS NULL AND username IS ? AND local_user IS ?",
                          (actor_key(r[0], r[1]) or "", r[0], r[1])) if self.db.engine == "sqlite" else c.execute(
                    "UPDATE events SET actor=? WHERE actor IS NULL AND username IS NOT DISTINCT FROM ? AND local_user IS NOT DISTINCT FROM ?",
                    (actor_key(r[0], r[1]) or "", r[0], r[1]))
            c.commit()

    # ---- users / auth
    def user_by_actor(self, name, localize=True):
        """The active hub account a CLI user name belongs to (an anonymous CLI sends its OS user). localize on: same rule as `actor_key`, so
        `Sam`, `sam@corp.com` and the account `sam` are one person; off: the exact name. None when nobody (or more than one person) matches."""
        key = actor_key(name) if localize else str(name or "").strip()
        if not key or key == "anonymous":
            return None
        rows = self.db.conn().execute("SELECT * FROM users WHERE " + ("lower(username)=?" if localize else "username=?") + " AND status='active' "
                                      "AND role<>'anonymous' LIMIT 2", (key,)).fetchall()
        return dict(rows[0]) if len(rows) == 1 else None

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

    # personal knowledge: markdown notes per user (name "soul" is the bot persona); `aihub knowledge pull` mirrors them to ~/.aihub/knowledge
    def knowledge_list(self, uid):
        return _rows(self.db.conn().execute("SELECT name,updated,LENGTH(body) AS size FROM knowledge WHERE user_id=? ORDER BY name", (uid,)))

    def knowledge_get(self, uid, name):
        r = self.db.conn().execute("SELECT name,body,updated FROM knowledge WHERE user_id=? AND name=?", (uid, name)).fetchone()
        return {"name": r[0], "body": r[1], "updated": r[2]} if r else None

    def knowledge_set(self, uid, name, body):
        with self.lock:
            c = self.db.conn()
            c.execute("INSERT OR REPLACE INTO knowledge VALUES(?,?,?,?)", (uid, name, body, time.time()))
            c.commit()

    def knowledge_delete(self, uid, name):
        with self.lock:
            c = self.db.conn()
            c.execute("DELETE FROM knowledge WHERE user_id=? AND name=?", (uid, name))
            c.execute("DELETE FROM knowledge_share WHERE owner_id=? AND name=?", (uid, name))
            c.commit()

    # `aihub run` supervisors report here (one row per user, device and run); people queue commands the supervisor takes from its next report
    def run_report(self, uid, client_id, host, os_name, run, done):
        """Store the run's state; drop the commands it has applied (`done` = their tokens: SQLite reuses ids). -> commands still pending."""
        rid = run["id"]
        with self.lock:
            c = self.db.conn()
            c.execute("INSERT OR REPLACE INTO runs(user_id,client_id,rid,name,bot,host,os,status,spec,seen) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (uid, client_id, rid, run.get("name") or rid, run.get("bot") or "", host, os_name, run.get("status") or "", json.dumps(run), time.time()))
            for t in done[:200]:
                c.execute("DELETE FROM run_commands WHERE user_id=? AND client_id=? AND rid=? AND token=?", (uid, client_id, rid, t))
            c.commit()
        return self.run_commands(uid, client_id, rid)

    def run_commands(self, uid, client_id, rid):
        return [{**json.loads(r[2] or "{}"), "id": r[0], "action": r[1], "by": r[3], "token": r[4]} for r in self.db.conn().execute(
            "SELECT id,action,data,by_user,token FROM run_commands WHERE user_id=? AND client_id=? AND rid=? ORDER BY id", (uid, client_id, rid)).fetchall()]

    def runs_list(self, uid=None):
        """Runs of one person (uid) or everybody (None), newest report first, with the owner's name and how many commands wait."""
        q = ("SELECT r.user_id,r.client_id,r.rid,r.name,r.bot,r.host,r.os,r.status,r.spec,r.seen,u.username,"
             "(SELECT COUNT(*) FROM run_commands k WHERE k.user_id=r.user_id AND k.client_id=r.client_id AND k.rid=r.rid) AS pending "
             "FROM runs r LEFT JOIN users u ON u.id=r.user_id")
        rows = self.db.conn().execute(q + (" WHERE r.user_id=?" if uid is not None else "") + " ORDER BY r.seen DESC",
                                      (uid,) if uid is not None else ()).fetchall()
        out = []
        for r in rows:
            try:
                spec = json.loads(r[8] or "{}")
            except ValueError:
                spec = {}
            out.append({"user_id": r[0], "client_id": r[1], "rid": r[2], "name": r[3], "bot": r[4], "host": r[5], "os": r[6], "status": r[7],
                        "spec": spec, "seen": r[9], "owner": r[10], "pending": r[11]})
        return out

    def run_get(self, uid, client_id, rid):
        return next((x for x in self.runs_list(uid) if x["client_id"] == client_id and x["rid"] == rid), None)

    def run_command_add(self, uid, client_id, rid, action, data, by):
        with self.lock:
            c = self.db.conn()
            cur = c.execute("INSERT INTO run_commands(user_id,client_id,rid,action,data,created,by_user,token) VALUES(?,?,?,?,?,?,?,?)",
                            (uid, client_id, rid, action, json.dumps(data or {}), time.time(), by, secrets.token_hex(8)))
            c.commit()
            return cur.lastrowid

    def run_delete(self, uid, client_id, rid):
        with self.lock:
            c = self.db.conn()
            c.execute("DELETE FROM runs WHERE user_id=? AND client_id=? AND rid=?", (uid, client_id, rid))
            c.execute("DELETE FROM run_commands WHERE user_id=? AND client_id=? AND rid=?", (uid, client_id, rid))
            c.commit()

    # sharing: a note goes to a user or a group, read or write; the owner keeps it, recipients see it under shared/<owner>/<name>
    def knowledge_shares(self, uid, name):
        return _rows(self.db.conn().execute(
            "SELECT s.principal_type AS type, s.access, CASE WHEN s.principal_type='user' THEN (SELECT username FROM users WHERE id=s.principal_id) "
            "ELSE (SELECT name FROM groups WHERE id=s.principal_id) END AS name FROM knowledge_share s WHERE s.owner_id=? AND s.name=? ORDER BY 1,3",
            (uid, name)))

    def knowledge_share_set(self, uid, name, ptype, pid, access):
        with self.lock:
            c = self.db.conn()
            c.execute("DELETE FROM knowledge_share WHERE owner_id=? AND name=? AND principal_type=? AND principal_id=?", (uid, name, ptype, pid))
            c.execute("INSERT INTO knowledge_share VALUES(?,?,?,?,?)", (uid, name, ptype, pid, access))
            c.commit()

    def knowledge_share_remove(self, uid, name, ptype, pid):
        with self.lock:
            c = self.db.conn()
            c.execute("DELETE FROM knowledge_share WHERE owner_id=? AND name=? AND principal_type=? AND principal_id=?", (uid, name, ptype, pid))
            c.commit()

    _SHARED = ("FROM knowledge_share s JOIN knowledge k ON k.user_id=s.owner_id AND k.name=s.name JOIN users o ON o.id=s.owner_id "
               "WHERE s.owner_id<>? AND ((s.principal_type='user' AND s.principal_id=?) OR (s.principal_type='group' AND s.principal_id IN "
               "(SELECT group_id FROM group_members WHERE user_id=?)))")

    def knowledge_shared_with(self, uid):
        """Notes other people shared with `uid` (directly or through a group). Write wins over read when both apply."""
        rows = _rows(self.db.conn().execute(
            "SELECT o.username AS owner,k.name AS name,s.access AS access,k.updated AS updated,LENGTH(k.body) AS size " + self._SHARED +
            " ORDER BY o.username,k.name", (uid, uid, uid)))
        best = {}
        for r in rows:
            key = (r["owner"], r["name"])
            if key not in best or r["access"] == "write":
                best[key] = r
        return list(best.values())

    def knowledge_shared_get(self, uid, owner, name):
        """-> {owner,name,body,updated,access} or None when `uid` has no share on owner's note."""
        c = self.db.conn()
        rows = c.execute("SELECT k.body AS body,k.updated AS updated,s.access AS access " + self._SHARED + " AND o.username=? AND k.name=?",
                         (uid, uid, uid, owner, name)).fetchall()
        if not rows:
            return None
        acc = "write" if any(r["access"] == "write" for r in rows) else "read"
        return {"owner": owner, "name": name, "body": rows[0]["body"], "updated": rows[0]["updated"], "access": acc}

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

    SHORT_SESSION = 12 * 3600          # a web sign-in without "Remember me" (token kind 'session-short') ends after this many seconds

    def token_user(self, h):
        r = self.db.conn().execute(
            "SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.hash=? AND NOT (t.kind='session-short' AND t.created<?)",
            (h, time.time() - self.SHORT_SESSION)).fetchone()
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
                        c.execute("INSERT OR IGNORE INTO group_members(group_id,user_id) VALUES(?,?)", (gid, cur.lastrowid))
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

    def audit_search(self, source="tools", q="", actor="", action="", package="", kind="", since=0, until=0, limit=100, offset=0, host="", os=""):
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
            ts = ("e.id,e.ts,e.kind,e.package,e.version,e.component," + self.actor_sql() + " AS actor,e.username,e.local_user,e.host,e.os,"
                  "e.source,e.detail,e.cwd,e.ip,e.client_id")
            if actor: w.append(self.actor_sql() + "=?"); args.append(actor)
            if kind: w.append("e.kind=?"); args.append(kind)
            if host: w.append("e.host=?"); args.append(host)
            if os: w.append("e.os=?"); args.append(os)
            if package: w.append("e.package=?"); args.append(package)
            if action: w.append("e.component LIKE ? ESCAPE '\\'"); args.append(self._like(action))
            if q:
                w.append("(e.detail LIKE ? ESCAPE '\\' OR e.component LIKE ? ESCAPE '\\' OR e.package LIKE ? ESCAPE '\\' OR e.cwd LIKE ? ESCAPE '\\' "
                         "OR e.host LIKE ? ESCAPE '\\' OR " + self.actor_sql() + " LIKE ? ESCAPE '\\')")
                args += [self._like(q)] * 6
        if since: w.append(tscol + ">=?"); args.append(since)
        if until: w.append(tscol + "<?"); args.append(until)
        where = " WHERE " + " AND ".join(w)
        total = c.execute("SELECT COUNT(*) FROM " + t + where, args).fetchone()[0]
        rows = _rows(c.execute("SELECT " + ts + " FROM " + t + where + " ORDER BY " + tscol + " DESC, " + t.split()[1] + ".id DESC LIMIT ? OFFSET ?",
                               args + [limit, offset]))
        return {"total": int(total), "items": rows}

    def audit_devices(self):
        c = self.db.conn()
        return {"hosts": [r[0] for r in c.execute("SELECT DISTINCT host FROM events WHERE host IS NOT NULL AND host<>'' ORDER BY 1 LIMIT 500")],
                "oses": [r[0] for r in c.execute("SELECT DISTINCT os FROM events WHERE os IS NOT NULL AND os<>'' ORDER BY 1 LIMIT 50")]}

    def audit_actors(self):
        c = self.db.conn()
        return {"tools": [r[0] for r in c.execute("SELECT DISTINCT " + self.actor_sql() + " n FROM events e ORDER BY n LIMIT 500")],
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
            "SELECT u.username,u.display_name,u.title,u.avatar_v,COALESCE(m.manager,0) AS manager FROM group_members m JOIN users u ON u.id=m.user_id "
            "WHERE m.group_id=? ORDER BY u.username", (gid,)))

    def group_member_add(self, gid, uids, manager=None):
        """Add members (existing ones stay). manager True/False also sets or clears the manager flag; None leaves it as is."""
        with self.lock:
            c = self.db.conn()
            for uid in uids:
                c.execute("INSERT OR IGNORE INTO group_members(group_id,user_id) VALUES(?,?)", (gid, uid))
                if manager is not None:
                    c.execute("UPDATE group_members SET manager=? WHERE group_id=? AND user_id=?", (1 if manager else 0, gid, uid))
            c.commit()

    def managed_groups(self, uid):
        """Names of the groups this account manages (manager flag, not ownership)."""
        return {r[0] for r in self.db.conn().execute(
            "SELECT g.name FROM groups g JOIN group_members m ON m.group_id=g.id WHERE m.user_id=? AND m.manager=1", (uid,))}

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
                 "downloads": "(" + DOWNLOADS_SQL + ") DESC",
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

    LIVE_KINDS = ("install", "use", "update", "publish")

    def live_kinds(self):
        """Event kinds the home page "Happening now" feed shows (admin setting `live_kinds`, comma list; blank = default)."""
        got = [k for k in (self.setting("live_kinds", "") or "").split(",") if k in ("install", "use", "update", "uninstall", "error", "publish")]
        return got or list(self.LIVE_KINDS)

    def _unindexed_ok(self):
        """A just published package has events before the index import creates its row; show those when new packages start public."""
        return self.initial_visibility() == "public"

    def recent_activity(self, viewer=None, limit=15):
        """Latest events on packages the viewer may see. Deliberately carries no user or client identifiers.
        A publish of a package that is not in the table yet is listed too, so a new package appears at once."""
        vsql, va = self.visible_sql(viewer, "p")
        kinds = self.live_kinds()
        mark = ",".join("?" * len(kinds))
        new = " OR (p.name IS NULL AND e.kind='publish')" if self._unindexed_ok() else ""
        return _rows(self.db.conn().execute(
            "SELECT e.package AS package, e.kind AS kind, e.ts AS ts, COALESCE(p.type,'new') AS type, e.version AS version FROM events e "
            "LEFT JOIN packages p ON p.name=e.package WHERE e.kind IN (" + mark + ") AND ((p.hidden=0 AND " + vsql + ")" + new + ") "
            "ORDER BY e.id DESC LIMIT ?", kinds + va + [limit]))

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
        """Versions of a package, newest first. `downloads` = legacy HTTP downloads (versions.downloads) + install/update events, the same
        events the dashboard and rankings count. An event without a known version counts on the latest version, so the total matches."""
        c = self.db.conn()
        rows = _rows(c.execute("SELECT * FROM versions WHERE package_id=?", (pid,)))
        ev = {}
        for r in c.execute("SELECT COALESCE(e.version,''),COUNT(*) FROM events e JOIN packages p ON p.name=e.package "
                           "WHERE p.id=? AND e.kind IN ('install','update') GROUP BY 1", (pid,)).fetchall():
            ev[r[0]] = r[1]
        latest = (c.execute("SELECT latest_version FROM packages WHERE id=?", (pid,)).fetchone() or [None])[0]
        known = {r["version"] for r in rows}
        orphan = sum(n for v, n in ev.items() if v not in known)
        for r in rows:
            r["manifest"] = json.loads(r["manifest"] or "{}")
            r["downloads"] = (r["downloads"] or 0) + ev.get(r["version"], 0) + (orphan if r["version"] == latest else 0)
        rows.sort(key=lambda r: V.parse(r["version"]), reverse=True)
        return rows

    def index_view(self, viewer):
        """The package index as THIS viewer may see it, straight from the database: {"rows": light catalog rows, "entries": {name: entry}}.
        Two queries however many packages there are. The caller caches it until something changes; nothing here touches git."""
        sql, a = self.visible_sql(viewer)
        c = self.db.conn()
        pk = _rows(c.execute("SELECT id,name,type,description,tags,latest_version,readme FROM packages WHERE hidden=0 AND " + sql + " ORDER BY name", a))
        vers = {}
        for r in c.execute("SELECT v.package_id,v.version,v.manifest FROM versions v JOIN packages ON packages.id=v.package_id WHERE packages.hidden=0 AND " + sql, a).fetchall():
            vers.setdefault(r[0], []).append((r[1], json.loads(r[2] or "{}")))
        rows, entries = [], {}
        for p in pk:
            vs = sorted(vers.get(p["id"], []), key=lambda x: V.parse(x[0]))          # oldest first, like the folder index
            if not vs:
                continue
            latest = p["latest_version"] or vs[-1][0]
            man = next((m for v, m in vs if v == latest), vs[-1][1])
            meta = man.get("package") or {}
            e = {"name": p["name"], "type": p["type"] or "tool", "description": p["description"] or "",
                 "tags": [t for t in (p["tags"] or "").split(",") if t], "readme": p["readme"] or "", "latest_version": latest,
                 "repo": {k: v for k, v in (man.get("repo") or {}).items() if k != "ref"},
                 "versions": [dict({"version": v, "ref": (m.get("repo") or {}).get("ref") or "", "requires": m.get("requires") or {}},
                                   **({"repo": pkgindex.location(m["repo"])} if isinstance(m.get("repo"), dict) and m["repo"].get("url") else {}))
                              for v, m in vs]}      # normalize_entry drops a version's repo when it is the entry's (only moved packages carry one)
            for k in ("authors", "license"):
                if meta.get(k):
                    e[k] = meta[k]
            e = pkgindex.normalize_entry(e)
            h = hashlib.sha256(pkgindex.dumps(e).encode()).hexdigest()[:12]
            entries[e["name"]] = e
            rows.append({"name": e["name"], "type": e.get("type", ""), "latest_version": latest,
                         "description": (e.get("description") or "")[:pkgindex.DESC_MAX], "tags": e.get("tags", []), "h": h})
        return {"rows": rows, "entries": entries}

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

    def version_delete(self, pid, version):
        with self.lock:
            c = self.db.conn()
            c.execute("DELETE FROM versions WHERE package_id=? AND version=?", (pid, version))
            self._relatest(c, pid)
            c.commit()

    def package_delete(self, pid):
        """Remove a package and everything attached to it. Usage events stay (history, rankings of the past)."""
        with self.lock:
            c = self.db.conn()
            for t in ("versions", "package_access", "package_maintainers", "reviews"):
                c.execute("DELETE FROM %s WHERE package_id=?" % t, (pid,))
            c.execute("DELETE FROM packages WHERE id=?", (pid,))
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
    event_rejects = 0                  # rows dropped because their own content could not be stored (never the database being busy)
    event_reject_last = ""

    def _bulk(self, c, sql, rows):
        """executemany that a single bad row cannot break. The batch goes in as one statement; if that fails, everything is ROLLED BACK (an
        open SQLite transaction would hold the write lock and make every web write answer "database is locked") and the rows are inserted
        again by BISECTION: a half that inserts cleanly is kept, a half that fails is split further, until the single bad rows are found.
        One bad row among 5000 costs about 2*log2(5000) statements, not 5000 savepoints.

        All of it is ONE transaction (explicit BEGIN on SQLite, whose sqlite3 module would otherwise commit each outermost SAVEPOINT as it is
        released): if anything other than a bad row's content fails (database busy or down, broken schema), the whole transaction is rolled
        back and the error is raised, so the caller keeps the complete batch and retries it. -> list of (row, error) that were skipped."""
        try:
            c.executemany(sql, rows)
            return []
        except Exception as first:
            self._abort(c)
            if not row_is_bad(first):
                raise
        bad = []
        try:
            if self.db.engine != "postgres":
                c.execute("BEGIN")
            self._split(c, sql, list(rows), bad, 0)
        except BaseException:
            self._abort(c)                                       # nothing from this phase survives: the caller retries the whole batch
            raise
        if bad:
            self.event_rejects += len(bad)
            self.event_reject_last = "%s: %s" % (type(bad[-1][1]).__name__, str(bad[-1][1])[:160])
            log.warning("events: %d row(s) could not be stored and were skipped (%s)", len(bad), self.event_reject_last.replace("\n", " "))
        return bad

    @staticmethod
    def _abort(c):
        try:
            c.rollback()
        except Exception:
            pass

    def _split(self, c, sql, rows, bad, depth):
        """Insert `rows` under a savepoint; on a content error split in two and recurse; a single failing row is recorded in `bad`."""
        if not rows:
            return
        name = "bulk_sp%d" % depth
        c.execute("SAVEPOINT " + name)
        try:
            c.executemany(sql, rows)
            c.execute("RELEASE SAVEPOINT " + name)
            return
        except Exception as e:
            c.execute("ROLLBACK TO SAVEPOINT " + name)           # a failure of THIS statement is undone, earlier good halves stay
            c.execute("RELEASE SAVEPOINT " + name)
            if not row_is_bad(e):
                raise
            if len(rows) == 1:
                bad.append((rows[0], e))
                return
        mid = len(rows) // 2
        self._split(c, sql, rows[:mid], bad, depth + 1)
        self._split(c, sql, rows[mid:], bad, depth + 1)

    @staticmethod
    def _event_defaults(rows):
        for r in rows:
            for k in ("version", "client_id", "username", "component", "duration", "source", "local_user", "host", "os", "detail", "cwd", "ip"):
                r.setdefault(k, None)
            for k, v in list(r.items()):
                if isinstance(v, str):
                    r[k] = textsafe.clean(v)            # a lone surrogate / NUL must never reach the driver
            r["actor"] = actor_key(r.get("username"), r.get("local_user")) or ""

    def events_insert_batch(self, rows):
        if not rows:
            return
        with self.lock:
            c = self.db.conn()
            self._event_defaults(rows)
            self._bulk(c, "INSERT INTO events(%s) VALUES(%s)" % (EVENT_COLS, EVENT_VALS), rows)
            c.commit()

    def events_insert_ext(self, rows):
        """Insert events keyed by ext_id (the event id the CLI generated). Already-seen ids are skipped. -> number inserted."""
        if not rows:
            return 0
        with self.lock:
            c = self.db.conn()
            try:
                before = c.execute("SELECT COUNT(*) FROM events WHERE ext_id IS NOT NULL").fetchone()[0]
                self._event_defaults(rows)
                # one fixed lock order: two transactions (another worker process, the sync thread) inserting overlapping ext_ids in
                # different orders would each wait for the other's unique-index entry and deadlock on Postgres
                rows = sorted(rows, key=lambda r: str(r.get("ext_id") or ""))
                self._bulk(c, "INSERT OR IGNORE INTO events(%s,ext_id) VALUES(%s,:ext_id)" % (EVENT_COLS, EVENT_VALS), rows)
                c.commit()
                return c.execute("SELECT COUNT(*) FROM events WHERE ext_id IS NOT NULL").fetchone()[0] - before
            except Exception:
                try:
                    c.rollback()                           # never leave a write transaction open on this thread's connection
                except Exception:
                    pass
                raise

    # ---- token usage per person (uploaded by the CLI after each answer turn; see cli/usagesync.py)
    USAGE_AGG = ("COALESCE(SUM(d.requests),0) AS requests, COALESCE(SUM(d.inp),0) AS inp, COALESCE(SUM(d.outp),0) AS outp, "
                 "COALESCE(SUM(d.cache_write),0) AS cache_write, COALESCE(SUM(d.cache_read),0) AS cache_read, "
                 "COALESCE(SUM(d.cost),0) AS cost, COALESCE(SUM(d.unpriced),0) AS unpriced")

    def usage_upsert(self, uid, client_id, rows):
        """One device of one user (tests, small callers). -> rows stored. See usage_upsert_many."""
        return self.usage_upsert_many({(uid, client_id): rows})

    def usage_upsert_many(self, by, sessions=None):
        """by = {(user_id, client_id): [row, ...]}. ONE transaction for all of it. A row is the day's total, so a resend overwrites it and never
        counts twice. A day never SHRINKS: Claude Code deletes old session logs, so a later upload can hold less than what is stored; that
        row is ignored (history stays). An equal or larger total replaces the whole row, cost included (prices may have changed). The old
        totals are read with one query per (user, device) over the days involved, not one per row. -> rows stored."""
        if not by and not sessions:
            return 0
        now = time.time()
        with self.lock:
            c = self.db.conn()
            try:
                data = []
                self._sessions_upsert(c, sessions or {}, now)
                for (uid, client), rows in by.items():
                    if not rows:
                        continue
                    old, days = {}, sorted({r["day"] for r in rows})
                    for i in range(0, len(days), 400):
                        part = days[i:i + 400]
                        for x in c.execute("SELECT day,tool,model,inp+outp+cache_write+cache_read FROM usage_daily WHERE user_id=? AND client_id=? AND day IN (%s)"
                                           % ",".join("?" * len(part)), [uid, client] + part):
                            old[(x[0], x[1], x[2])] = x[3]
                    data += [(uid, client, r["day"], r["tool"], r["model"], r["requests"], r["inp"], r["outp"], r["cache_write"], r["cache_read"],
                              r["cost"], r["unpriced"], now) for r in sorted(rows, key=lambda r: (r["day"], r["tool"], r["model"]))
                             if r["inp"] + r["outp"] + r["cache_write"] + r["cache_read"] >= old.get((r["day"], r["tool"], r["model"]), 0)]
                if data:
                    data.sort(key=lambda t: (t[0], t[1], t[2], t[3], t[4]))      # one fixed lock order: two workers can never deadlock on Postgres
                    c.executemany("INSERT OR REPLACE INTO usage_daily(user_id,client_id,day,tool,model,requests,inp,outp,cache_write,cache_read,cost,unpriced,updated) "
                                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", data)
                for (uid, client), rows in by.items():                      # after the insert (sees a twin of this same batch); arrival order: the latest client keeps the rows
                    if rows:
                        self._usage_fold(c, uid, client, rows)
                c.commit()
                return len(data)
            except Exception:
                try:
                    c.rollback()
                except Exception:
                    pass
                raise

    DUP_MIN_REQUESTS = 5      # a day this busy reported identically by two clients = the same logs (a reinstall), not two devices
    _DUP_COLS = "day,tool,model,requests,inp,outp,cache_write,cache_read"

    def _usage_fold(self, c, uid, client, rows):
        """A reinstalled CLI has a new client_id and re-uploads every day its logs still hold, which would count those days twice (clients
        add up). Another client of this person reporting a busy day (>= DUP_MIN_REQUESTS requests) with exactly the same numbers is the
        same machine: it is folded into `client`. -> clients folded."""
        mine = {(r["day"], r["tool"], r["model"], r["requests"], r["inp"], r["outp"], r["cache_write"], r["cache_read"])
                for r in rows if r["requests"] >= self.DUP_MIN_REQUESTS}
        if not mine:
            return 0
        days, dup = sorted({k[0] for k in mine}), set()
        for i in range(0, len(days), 400):
            part = days[i:i + 400]
            for x in c.execute("SELECT client_id,%s FROM usage_daily WHERE user_id=? AND client_id<>? AND requests>=? AND day IN (%s)"
                               % (self._DUP_COLS, ",".join("?" * len(part))), [uid, client, self.DUP_MIN_REQUESTS] + part):
                if tuple(x[i] for i in range(1, 9)) in mine:
                    dup.add(x[0])
        for old in sorted(dup):
            self._usage_move(c, uid, old, client)
        return len(dup)

    def _usage_move(self, c, uid, src, dst):
        """Rows of client `src` go to `dst` where they hold more tokens than `dst` has for that day (history of pruned logs is kept), then
        `src` is gone."""
        have = {(x[0], x[1], x[2]): x[3] for x in c.execute("SELECT day,tool,model,inp+outp+cache_write+cache_read FROM usage_daily "
                                                             "WHERE user_id=? AND client_id=?", (uid, dst))}
        data = [(uid, dst) + tuple(x[i] for i in range(11)) for x in c.execute(
            "SELECT %s,cost,unpriced,updated FROM usage_daily WHERE user_id=? AND client_id=?" % self._DUP_COLS, (uid, src))
            if x[4] + x[5] + x[6] + x[7] > have.get((x[0], x[1], x[2]), -1)]
        if data:
            data.sort(key=lambda t: (t[2], t[3], t[4]))
            c.executemany("INSERT OR REPLACE INTO usage_daily(user_id,client_id,day,tool,model,requests,inp,outp,cache_write,cache_read,cost,unpriced,updated) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", data)
        c.execute("DELETE FROM usage_daily WHERE user_id=? AND client_id=?", (uid, src))

    def usage_dedupe(self):
        """One-time repair of days doubled before `_usage_fold` existed (marker `usage_dedupe_v1`): per person, clients sharing an identical
        busy day are one machine, folded into the one updated last. -> clients folded."""
        if self.setting("usage_dedupe_v1") == "1":
            return 0
        n = 0
        with self.lock:
            c = self.db.conn()
            try:
                first, parent, last = {}, {}, {}

                def root(k):
                    while parent.get(k, k) != k:
                        k = parent[k]
                    return k
                for x in c.execute("SELECT user_id,client_id,%s,updated FROM usage_daily WHERE requests>=?" % self._DUP_COLS, (self.DUP_MIN_REQUESTS,)):
                    k, sig = (x[0], x[1]), (x[0],) + tuple(x[i] for i in range(2, 10))
                    last[k] = max(last.get(k, 0), x[10] or 0)
                    if sig in first and root(first[sig]) != root(k):
                        parent[root(k)] = root(first[sig])
                    first.setdefault(sig, k)
                groups = {}
                for k in last:
                    groups.setdefault(root(k), []).append(k)
                for ks in groups.values():
                    if len(ks) < 2:
                        continue
                    ks.sort(key=lambda k: (last[k], k[1]))
                    for uid, src in ks[:-1]:
                        self._usage_move(c, uid, src, ks[-1][1])
                        n += 1
                c.commit()
            except Exception:
                try:
                    c.rollback()
                except Exception:
                    pass
                raise
        self.set_setting("usage_dedupe_v1", "1")
        return n

    SESSION_COLS = ("tool", "model", "day", "started", "ended", "requests", "inp", "outp", "cache_write", "cache_read", "cost", "unpriced",
                    "peak", "cold", "compact", "big", "light")

    def _sessions_upsert(self, c, sessions, now):
        """sessions = {(user_id, sid): row}. A session never shrinks either (same rule as a day): the larger token total wins, and the peak
        context only goes up. One query reads the old totals of all of them."""
        if not sessions:
            return
        keys = sorted(sessions)
        old = {}
        for uid in {k[0] for k in keys}:
            sids = [k[1] for k in keys if k[0] == uid]
            for i in range(0, len(sids), 400):
                part = sids[i:i + 400]
                for x in c.execute("SELECT sid,inp+outp+cache_write+cache_read,peak FROM usage_session WHERE user_id=? AND sid IN (%s)" % ",".join("?" * len(part)),
                                   [uid] + part):
                    old[(uid, x[0])] = (x[1], x[2])
        data = []
        for k in keys:
            r = sessions[k]
            tot = r["inp"] + r["outp"] + r["cache_write"] + r["cache_read"]
            if k in old and tot < old[k][0]:
                continue
            peak = max(r["peak"], old.get(k, (0, 0))[1])
            data.append((k[0], k[1]) + tuple(r[x] if x != "peak" else peak for x in self.SESSION_COLS) + (now,))
        if data:
            c.executemany("INSERT OR REPLACE INTO usage_session(user_id,sid,%s,updated) VALUES(?,?,%s?)" % (",".join(self.SESSION_COLS), "?," * len(self.SESSION_COLS)), data)

    @staticmethod
    def _usage_fix(r):
        r = dict(r)
        for k in ("requests", "inp", "outp", "cache_write", "cache_read", "unpriced"):
            r[k] = int(r.get(k) or 0)
        r["cost"] = round(float(r.get("cost") or 0), 6)
        r["tokens"] = r["inp"] + r["outp"] + r["cache_write"] + r["cache_read"]
        ctx = r["inp"] + r["cache_write"] + r["cache_read"]
        r["cache_hit"] = round(r["cache_read"] / ctx, 4) if ctx else 0.0
        return r

    _USAGE_SUM = ("requests", "inp", "outp", "cache_write", "cache_read", "cost", "unpriced")

    @classmethod
    def _usage_sum(cls, rows):
        return {k: sum(r.get(k) or 0 for r in rows) for k in cls._USAGE_SUM}

    @classmethod
    def _usage_regroup(cls, rows, key):
        g = {}
        for r in rows:
            g.setdefault(r[key], []).append(r)
        return [dict(cls._usage_sum(v), **{key: k}) for k, v in g.items()]

    BIG_CTX, COLD_MIN_REQ = 150000, 2

    def usage_sessions(self, since_day, until_day, uid=None, group_ids=None, tool=None, model=None, user=None, limit=200):
        """Sessions in the window of one person or of everybody in the groups, biggest cost first, plus usage hints computed from the SAME
        numbers the local `aihub usage` tips use (cold cache rebuilds, huge context, low cache hit, premium model for short replies, one-shot
        sessions, one session dominating). -> {sessions, hints, totals}. Titles and folders never reach the hub, so a session is its short id."""
        c = self.db.conn()
        if group_ids is not None:
            if not group_ids:
                return {"sessions": [], "hints": [], "totals": {}}
            scope, sa = "s.user_id IN (SELECT user_id FROM group_members WHERE group_id IN (%s))" % ",".join("?" * len(group_ids)), list(group_ids)
        else:
            scope, sa = "s.user_id=?", [uid]
        w, a = "s.day>=? AND s.day<=? AND " + scope, [since_day, until_day] + sa
        for col, v in (("s.tool", tool), ("s.model", model)):
            if v:
                w += " AND %s=?" % col
                a.append(v)
        if user:
            w += " AND s.user_id IN (SELECT id FROM users WHERE username=?)"
            a.append(user)
        rows = []
        for x in c.execute("SELECT s.sid,s.tool,s.model,s.day,s.started,s.ended,s.requests,s.inp,s.outp,s.cache_write,s.cache_read,s.cost,s.unpriced,s.peak,"
                           "s.cold,s.compact,s.big,s.light,u.username,u.display_name FROM usage_session s JOIN users u ON u.id=s.user_id WHERE %s "
                           "ORDER BY s.cost DESC, s.ended DESC LIMIT 20000" % w, a):
            r = dict(zip(("sid", "tool", "model", "day", "started", "ended", "requests", "inp", "outp", "cache_write", "cache_read", "cost", "unpriced",
                          "peak", "cold", "compact", "big", "light", "username", "display_name"), tuple(x)))
            r["id"] = r["sid"][-8:]
            r["tokens"] = r["inp"] + r["outp"] + r["cache_write"] + r["cache_read"]
            ctx = r["inp"] + r["cache_write"] + r["cache_read"]
            r["cache_hit"] = round(r["cache_read"] / ctx, 4) if ctx else 0.0
            r["minutes"] = max(0, int((r["ended"] - r["started"]) // 60))
            rows.append(r)
        tot = {"sessions": len(rows), "peak": max([r["peak"] for r in rows] or [0]), "cost": round(sum(r["cost"] for r in rows), 6),
               "requests": sum(r["requests"] for r in rows), "avg_requests": round(sum(r["requests"] for r in rows) / len(rows), 1) if rows else 0,
               "big_sessions": sum(1 for r in rows if r["peak"] > self.BIG_CTX), "cold": sum(r["cold"] for r in rows),
               "median_peak": sorted(r["peak"] for r in rows)[len(rows) // 2] if rows else 0}
        return {"sessions": rows[:limit], "hints": self._usage_hints(rows, tot), "totals": tot, "truncated": len(rows) > limit}

    def _usage_hints(self, rows, tot):
        """[{id, level, title, detail, amount (USD affected), sessions: [sid...]}], biggest first. Thresholds mirror cli/usage.tips."""
        out, total = [], tot["cost"]

        def add(tid, title, detail, amount, sids):
            out.append({"id": tid, "level": "warn" if total and amount >= 0.15 * total else "info", "title": title, "detail": detail,
                        "amount": round(amount, 4), "sessions": [s[-8:] for s in sids][:50], "count": len(sids)})
        money = lambda v: "$%.2f" % v if v >= 0.01 or not v else "$%.3f" % v
        cold = [r for r in rows if r["cold"]]
        if sum(r["cold"] for r in cold) >= 2:
            add("cold", "Cache rebuilt after idle gaps", "%d requests in %d sessions re-wrote the prompt cache after more than 5 idle minutes. The cache lives about "
                "5 minutes: send follow-ups in one go, and /compact or /clear before a long break." % (sum(r["cold"] for r in cold), len(cold)),
                sum(r["cost"] for r in cold) * 0.15, [r["sid"] for r in cold])
        big = [r for r in rows if r["big"]]
        if sum(r["big"] for r in big) >= 3:
            add("big", "Very large context", "%d requests carried more than 150k tokens of context, in %d sessions. Every request re-reads it: use /compact, or start a "
                "new session when the task changes." % (sum(r["big"] for r in big), len(big)), sum(r["cost"] for r in big), [r["sid"] for r in big])
        low = [r for r in rows if r["requests"] >= 5 and r["inp"] + r["cache_write"] + r["cache_read"] >= 200000 and r["cache_hit"] < 0.5]
        if low:
            add("lowhit", "Low cache hit rate", "%d sessions read less than half of their input from cache. Changing the model, tools or system prompt "
                "mid-session, or long idle gaps, break the cache." % len(low), sum(r["cost"] for r in low), [r["sid"] for r in low])
        light = [r for r in rows if r["light"]]
        if sum(r["light"] for r in light) >= 10:
            add("light", "Premium model for short replies", "%d requests on an Opus-class model produced under 300 output tokens with a small context. A "
                "smaller model is usually enough for quick questions." % sum(r["light"] for r in light), sum(r["cost"] for r in light) * 0.5, [r["sid"] for r in light])
        tiny = [r for r in rows if r["requests"] <= 2]
        if len(tiny) >= 8:
            add("tiny", "Many one-shot sessions", "%d sessions have one or two requests. Each starts with a cold cache and re-sends the system prompt and tools; "
                "keep related questions in one session." % len(tiny), sum(r["cost"] for r in tiny), [r["sid"] for r in tiny])
        if rows and total >= 1.0:
            top = max(rows, key=lambda r: r["cost"])
            if top["cost"] >= 0.4 * total:
                add("top", "One session dominates the cost", "Session %s (%s) is %s of %s (%d%%). Check whether it is a long-running thread that could be split or "
                    "compacted." % (top["id"], top["username"], money(top["cost"]), money(total), round(100 * top["cost"] / total)), top["cost"], [top["sid"]])
        return sorted(out, key=lambda h: -h["amount"])

    def group_member_ids(self, gid):
        return [r[0] for r in self.db.conn().execute("SELECT user_id FROM group_members WHERE group_id=?", (gid,))]

    def user_groups(self, uid, every=False):
        """[{id,name,members}] the groups a person belongs to (every=True: all groups, for a site admin)."""
        q = ("SELECT g.id AS id,g.name AS name,(SELECT COUNT(*) FROM group_members m2 WHERE m2.group_id=g.id) AS members FROM groups g "
             + ("" if every else "JOIN group_members m ON m.group_id=g.id WHERE m.user_id=? ") + "ORDER BY g.name")
        return _rows(self.db.conn().execute(q, () if every else (uid,)))

    def usage_query(self, since_day, until_day, uid=None, group_ids=None, tool=None, model=None, user=None):
        """Usage between two UTC days (YYYY-MM-DD, inclusive) of one person (`uid`) or of everybody in the groups `group_ids` (each person once,
        even when in several of them). tool / model / user (username) narrow it. -> totals, daily, by_model, by_tool, by_user, by_group, options."""
        c = self.db.conn()
        if group_ids is not None:
            if not group_ids:
                return None
            ph = ",".join("?" * len(group_ids))
            scope, sa = "d.user_id IN (SELECT user_id FROM group_members WHERE group_id IN (%s))" % ph, list(group_ids)
        else:
            scope, sa = "d.user_id=?", [uid]
        base = "d.day>=? AND d.day<=? AND " + scope
        ba = [since_day, until_day] + sa
        extra, ea = "", []
        for col, v in (("d.tool", tool), ("d.model", model)):
            if v:
                extra += " AND %s=?" % col
                ea.append(v)
        if user:
            extra += " AND d.user_id IN (SELECT id FROM users WHERE username=?)"
            ea.append(user)
        w, wa = base + extra, ba + ea
        agg, fix = self.USAGE_AGG, self._usage_fix

        def rows(sel, frm, where, args, tail):
            return [fix(x) for x in c.execute("SELECT %s, %s FROM usage_daily d %s WHERE %s %s" % (sel, agg, frm, where, tail), args)]

        daily = rows("d.day AS date", "", w, wa, "GROUP BY d.day ORDER BY d.day")
        by_model = rows("d.tool AS tool, d.model AS model", "", w, wa, "GROUP BY d.tool, d.model")
        by_user = rows("u.username AS username, u.display_name AS display_name", "JOIN users u ON u.id=d.user_id", w, wa, "GROUP BY u.id, u.username, u.display_name")
        # totals, by_tool and the people count come from rows already read: no extra statements
        out = {"totals": fix(self._usage_sum(daily)), "daily": daily, "by_model": by_model, "by_user": by_user, "people": len(by_user),
               "by_tool": [fix(x) for x in self._usage_regroup(by_model, "tool")]}
        if group_ids is not None:
            ph = ",".join("?" * len(group_ids))
            gw, ga = "d.day>=? AND d.day<=? AND g.id IN (%s)%s" % (ph, extra), [since_day, until_day] + list(group_ids) + ea
            out["by_group"] = rows("g.id AS id, g.name AS name, (SELECT COUNT(*) FROM group_members m2 WHERE m2.group_id=g.id) AS members",
                                   "JOIN group_members m ON m.user_id=d.user_id JOIN groups g ON g.id=m.group_id", gw, ga, "GROUP BY g.id, g.name")
            out["members"] = _rows(c.execute(
                "SELECT DISTINCT u.username AS username, u.display_name AS display_name FROM group_members m JOIN users u ON u.id=m.user_id "
                "WHERE m.group_id IN (%s) ORDER BY u.username" % ph, list(group_ids)))
        opt = c.execute("SELECT DISTINCT d.tool,d.model FROM usage_daily d WHERE %s" % base, ba).fetchall()
        out["options"] = {"tools": sorted({r[0] for r in opt}), "models": sorted({r[1] for r in opt})}
        return out

    def version_sync(self, pid, version, manifest, sha="", file=None, size=0):
        """Add a version from the git index if new; refresh its manifest if it changed. -> True when new.
        `sha`/`size` (known only for archive versions) also fill in a row that has none yet."""
        with self.lock:
            c = self.db.conn()
            row = c.execute("SELECT id,manifest,sha256 FROM versions WHERE package_id=? AND version=?", (pid, version)).fetchone()
            js = json.dumps(manifest)
            if row:
                if row[1] != js:
                    c.execute("UPDATE versions SET manifest=? WHERE id=?", (js, row[0]))
                    c.commit()
                if sha and not row[2]:
                    c.execute("UPDATE versions SET sha256=?,size=? WHERE id=?", (sha, size, row[0]))
                    c.commit()
                return False
            c.execute("INSERT INTO versions(package_id,version,file,sha256,size,manifest,signature,created) VALUES(?,?,?,?,?,?,?,?)",
                      (pid, version, file, sha, size, js, None, time.time()))
            self._relatest(c, pid)
            c.commit()
            return True

    # ---- where each version lives (a package may have moved to another repo, branch or folder since an old version was published)
    def publish_locations(self, package=None):
        """Where publish events say a version was published from, newest first: [{package, version, git_url, git_branch, git_subdir, commit}].
        Only events stored since publish events carry their location in `detail` (older ones are recovered from the git archive, see Sync)."""
        sql, a = "SELECT package,version,detail FROM events WHERE kind='publish' AND detail LIKE ?", ['{"git_url":%']
        if package:
            sql, a = sql + " AND package=?", a + [package]
        out = []
        for r in self.db.conn().execute(sql + " ORDER BY ts DESC, id DESC", a).fetchall():
            try:
                d = json.loads(r[2])
            except (TypeError, ValueError):
                continue
            if isinstance(d, dict) and d.get("git_url") and r[1]:
                out.append({"package": r[0], "version": r[1], "git_url": str(d["git_url"]), "git_branch": str(d.get("git_branch") or "main"),
                            "git_subdir": str(d.get("git_subdir") or ""), "commit": str(d.get("commit") or "")})
        return out

    def known_locations(self, package):
        """Every distinct {url, branch, subdir} this package was ever seen in: publish events first (newest first), then its versions'
        manifests (newest version first). For finding an old commit after the package moved."""
        out, seen = [], set()

        def add(loc):
            if loc.get("url") and pkgindex.repo_key(loc) not in seen:
                seen.add(pkgindex.repo_key(loc))
                out.append(pkgindex.location(loc))

        for e in self.publish_locations(package):
            add({"url": e["git_url"], "branch": e["git_branch"], "subdir": e["git_subdir"]})
        pid = (self.db.conn().execute("SELECT id FROM packages WHERE name=?", (package,)).fetchone() or [None])[0]
        for v in (self.versions(pid) if pid else []):
            add((v.get("manifest") or {}).get("repo") or {})
        return out

    def version_repos_backfill(self, extra=()):
        """Repair version manifests whose repo is not where the version was published: before versions carried their own location every
        old version got the latest one (a package moved into a monorepo made `install X@old` look for an old commit in the new repo).
        The truth is the publish event (`publish_locations`, plus `extra`: events recovered from the git archive). Changed only when the
        event's commit is the version's ref (or the version has none). Idempotent. -> [(package, version, {url, branch, subdir})]."""
        locs = {}
        for e in list(self.publish_locations()) + [x for x in extra if isinstance(x, dict)]:
            locs.setdefault((naming.normalize(str(e.get("package") or "")), str(e.get("version") or "")), e)    # first = newest
        fixed = []
        if not locs:
            return fixed
        with self.lock:
            c = self.db.conn()
            for r in c.execute("SELECT v.id,p.name,v.version,v.manifest FROM versions v JOIN packages p ON p.id=v.package_id").fetchall():
                ev = locs.get((r[1], r[2]))
                if not ev or not ev.get("git_url"):
                    continue
                try:
                    man = json.loads(r[3] or "{}")
                except ValueError:
                    continue
                repo = man.get("repo") if isinstance(man.get("repo"), dict) else {}
                ref, com = str(repo.get("ref") or ""), str(ev.get("commit") or "")
                if not repo.get("url") or (ref and com and ref != com):
                    continue                                    # not a git version, or another publish of that version than the one indexed
                want = {"url": ev["git_url"], "branch": ev.get("git_branch") or "main", "subdir": ev.get("git_subdir") or ""}
                if pkgindex.same_repo(repo, want):
                    continue
                man["repo"] = dict(repo, **pkgindex.location(want))
                c.execute("UPDATE versions SET manifest=? WHERE id=?", (json.dumps(man), r[0]))
                fixed.append((r[1], r[2], pkgindex.location(want)))
            if fixed:
                c.commit()
        return fixed

    def version_set_repo(self, package, version, loc):
        """Record where one version really lives (found by the transfer safety net). -> True when it changed."""
        with self.lock:
            c = self.db.conn()
            row = c.execute("SELECT v.id,v.manifest FROM versions v JOIN packages p ON p.id=v.package_id WHERE p.name=? AND v.version=?",
                            (package, version)).fetchone()
            if not row:
                return False
            man = json.loads(row[1] or "{}")
            repo = man.get("repo") if isinstance(man.get("repo"), dict) else {}
            if repo.get("url") and pkgindex.same_repo(repo, loc):
                return False
            man["repo"] = dict(repo, **pkgindex.location(loc))
            c.execute("UPDATE versions SET manifest=? WHERE id=?", (json.dumps(man), row[0]))
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
        users = c.execute("SELECT COUNT(DISTINCT " + self.actor_sql() + ") FROM events e WHERE e.package=? AND e.ts>=?",
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
        col = {"packages": "e.package", "users": self.actor_sql()}[what]
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
        score, names = {}, {}
        for row in used:
            pid, n = row[0], row[1]                  # by position: a Postgres Row iterates its keys, not its values
            live = [v for v in self.versions(pid) if not v["yanked"]]
            authors = [a.get("name") or a.get("email") for a in (((live[0]["manifest"] if live else {}).get("package") or {}).get("authors") or [])]
            if not any(authors):
                authors = [m["username"] for m in self.maintainers(pid)]
            loc = self.localize()
            seen = {}
            for x in authors:
                if x and x.strip():
                    seen.setdefault(actor_key(x) if loc else x.strip(), x.strip())     # localized: "Sam", "sam@corp.com" = one developer
            for k, shown in seen.items():
                if k:
                    names.setdefault(k, shown)
                    score[k] = score.get(k, 0) + n
        return [{"name": names[k], "count": v} for k, v in sorted(score.items(), key=lambda kv: (-kv[1], names[kv[0]]))[:limit]]

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
                     identity=None, host=None, q=None, os=None, with_cli=False):
        """-> (sql, args) appended to `... WHERE e.ts>=? ...`. Only events of packages the viewer may see."""
        vsql, va = self.visible_sql(viewer, "vp")
        pf = " AND e.package IN (SELECT vp.name FROM packages vp WHERE %s%s)" % (vsql, " AND vp.type=?" if type else "")
        pa = list(va) + ([type] if type else [])
        if not type and self._unindexed_ok():                # publishes of packages the index import has not created yet still count
            pf = " AND (e.package IN (" + pf[len(" AND e.package IN ("):] + " OR (e.kind='publish' AND e.package NOT IN (SELECT np.name FROM packages np)))"
        if with_cli and not type:                           # the CLI's own install/uninstall events: listed in the event log only, never counted
            pf = " AND (e.package='aihub-cli' OR" + pf[4:] + ")"
        if package:
            pf += " AND e.package=?"; pa.append(package)
        if user:
            pf += " AND " + self.actor_sql() + "=?"; pa.append(user)
        if source:
            pf += " AND COALESCE(e.source,'cli')=?"; pa.append(source)
        kinds = [k for k in (kind or "").split(",") if k]       # one kind or several joined with commas (combined)
        if kinds:
            pf += " AND e.kind IN (%s)" % ",".join("?" * len(kinds)); pa += kinds
        if identity == "anonymous":
            pf += " AND e.username IS NULL"
        elif identity == "signed-in":
            pf += " AND e.username IS NOT NULL"
        if host:
            pf += " AND e.host=?"; pa.append(host)
        if os:
            pf += " AND e.os=?"; pa.append(os)
        if q:
            pf += " AND (e.package LIKE ? ESCAPE '\\' OR e.component LIKE ? ESCAPE '\\')"
            pa += [self._like(q)] * 2
        return pf, pa

    def dashboard(self, days=30, package=None, viewer=None, type=None, user=None, source=None, kind=None,
                  identity=None, host=None, q=None, date_from=None, date_to=None, os=None):
        """Aggregated usage for the dashboard. All times UTC; one daily bucket per day of the window."""
        import datetime
        days, start_day, since, until = self._window(days, date_from, date_to)
        now = until - 1
        prev = since - days * 86400
        c = self.db.conn()
        pf, pa = self._dash_filter(viewer, type, package, user, source, kind, identity, host, q, os)
        pf += " AND e.ts<?"; pa.append(until)
        actor = self.actor_sql()

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
        by_day = {}
        for d, o, n in c.execute("SELECT strftime('%Y-%m-%d',e.ts,'unixepoch'),COALESCE(e.os,'unknown'),COUNT(*) FROM events e WHERE e.ts>=?" + pf +
                                 " GROUP BY 1,2", [since] + pa):
            by_day.setdefault(d, {})[o] = n
        os_names = sorted({o for v in by_day.values() for o in v})
        os_daily = {"series": os_names, "daily": [dict({"date": x["date"]}, **by_day.get(x["date"], {})) for x in daily]}
        vs2, va2 = self.visible_sql(viewer, "vp")
        scope = " e.package IN (SELECT vp.name FROM packages vp WHERE %s) AND e.ts>=?" % vs2
        options = {
            "users": [r[0] for r in c.execute("SELECT DISTINCT " + self.actor_sql() + " n FROM events e WHERE"
                                              + scope + " ORDER BY n LIMIT 200", va2 + [since])],
            "sources": [r[0] for r in c.execute("SELECT DISTINCT COALESCE(e.source,'cli') n FROM events e WHERE" + scope + " ORDER BY n", va2 + [since])],
            "types": [r[0] for r in c.execute("SELECT DISTINCT p.type FROM packages p WHERE " + vs2.replace("vp.", "p.") + " ORDER BY 1", va2)],
            "oses": [r[0] for r in c.execute("SELECT DISTINCT e.os n FROM events e WHERE e.os IS NOT NULL AND" + scope + " ORDER BY n LIMIT 50", va2 + [since])],
            "hosts": [r[0] for r in c.execute("SELECT DISTINCT e.host n FROM events e WHERE e.host IS NOT NULL AND" + scope + " ORDER BY n LIMIT 200", va2 + [since])],
        }
        stamps = [r[0] for r in c.execute("SELECT e.ts FROM events e WHERE e.ts>=?" + pf + " LIMIT 50000", [since] + pa)]
        heat = [[0] * 24 for _ in range(7)]                              # [weekday Mon..Sun][hour UTC]
        for t in stamps:
            d = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
            heat[d.weekday()][d.hour] += 1
        return {
            "days": days, "package": package,
            "filters": {"type": type, "user": user, "source": source, "kind": kind, "identity": identity, "host": host, "os": os, "q": q,
                        "from": start_day.isoformat(), "to": (start_day + datetime.timedelta(days=days - 1)).isoformat()},
            "heatmap": heat,
            "top_components": rows("SELECT e.component AS name,e.package AS package,COUNT(*) AS count FROM events e "
                                   "WHERE e.component IS NOT NULL AND e.ts>=?" + pf + " GROUP BY e.component,e.package ORDER BY count DESC LIMIT 10", [since] + pa),
            "by_host": rows("SELECT COALESCE(e.host,'unknown') AS name,COUNT(*) AS count FROM events e WHERE e.ts>=?" + pf +
                            " GROUP BY 1 ORDER BY count DESC LIMIT 10", [since] + pa),
            "by_os": rows("SELECT COALESCE(e.os,'unknown') AS name,COUNT(*) AS count FROM events e WHERE e.ts>=?" + pf +
                          " GROUP BY 1 ORDER BY count DESC LIMIT 10", [since] + pa),
            "by_os_daily": os_daily,
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
            "top_users": rows("SELECT " + self.actor_sql() + " AS name,COUNT(*) AS count FROM events e "
                              "WHERE e.ts>=? AND (e.username IS NOT NULL OR e.client_id IS NOT NULL)" + pf +
                              " GROUP BY 1 ORDER BY count DESC LIMIT 10", [since] + pa),
            "by_type": rows("SELECT COALESCE(p.type,'unknown') AS name,COUNT(*) AS count FROM events e "
                            "LEFT JOIN packages p ON p.name=e.package WHERE e.ts>=?" + pf + " GROUP BY COALESCE(p.type,'unknown') ORDER BY count DESC", [since] + pa),
            "by_source": rows("SELECT COALESCE(e.source,'cli') AS name,COUNT(*) AS count FROM events e WHERE e.ts>=?" + pf +
                              " GROUP BY 1 ORDER BY count DESC", [since] + pa),
            "recent": rows("SELECT e.ts,e.kind,e.package,e.version,e.component," + self.actor_sql() + " AS actor,"
                           "e.source FROM events e WHERE e.ts>=?" + pf + " ORDER BY e.id DESC LIMIT 20", [since] + pa),
        }

    def dashboard_events(self, viewer, days=30, date_from=None, date_to=None, page=1, per_page=25, **flt):
        """Paginated activity log under the same filters. Deliberately omits detail/cwd/ip (those stay on the audit page)."""
        days, start_day, since, until = self._window(days, date_from, date_to)
        pf, pa = self._dash_filter(viewer, with_cli=True, **flt)
        pf += " AND e.ts<?"; pa.append(until)
        c = self.db.conn()
        total = c.execute("SELECT COUNT(*) FROM events e WHERE e.ts>=?" + pf, [since] + pa).fetchone()[0]
        per_page = max(1, min(int(per_page), 100)); page = max(1, int(page))
        items = [dict(r) for r in c.execute(
            "SELECT e.ts,e.kind,e.package,e.version,e.component," + self.actor_sql() + " AS actor,COALESCE(e.source,'cli') AS source,e.host,e.os "
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
                "downloads": c.execute("SELECT COALESCE(SUM(" + DOWNLOADS_SQL + "),0) FROM packages WHERE " + vsql, va).fetchone()[0]}


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
