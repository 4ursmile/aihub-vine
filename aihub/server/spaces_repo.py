"""SQL for Spaces (documents, slides, reports... published with `aihub space publish`). Mixed into `Repos`.
A space is addressed `owner@slug`. Visibility works like packages: public, or private + shared with users/groups as `view` or `edit`;
the owner and site admins (`bypass`) manage it. ONE function, `space_visible_sql`, says who sees what: every read path uses it."""
import json
import time

KINDS = ("doc", "slides", "report", "pdf", "sheet", "dashboard", "html")
COLS = ("s.id AS id,s.slug AS slug,s.title AS title,s.description AS description,s.kind AS kind,s.visibility AS visibility,s.filename AS filename,"
        "s.size AS size,s.sha256 AS sha256,s.rev AS rev,s.settings AS settings,s.tags AS tags,s.views AS views,s.created AS created,"
        "s.updated AS updated,s.owner_id AS owner_id,o.username AS owner,o.display_name AS owner_name,o.avatar_v AS owner_avatar_v")
FROM = "FROM spaces s JOIN users o ON o.id=s.owner_id"


def _row(r):
    d = dict(r)
    try:
        d["settings"] = json.loads(d.get("settings") or "{}")
    except ValueError:
        d["settings"] = {}
    d["tags"] = [t for t in (d.get("tags") or "").split(",") if t]
    d["ref"] = "%s@%s" % (d["owner"], d["slug"])
    return d


class SpaceRepos:
    @staticmethod
    def space_visible_sql(viewer, alias="s"):
        if viewer and viewer.get("bypass"):
            return "1=1", []
        if not viewer:
            return "%s.visibility='public'" % alias, []
        uid = viewer["id"]
        return ("(%(a)s.visibility='public' OR %(a)s.owner_id=? OR EXISTS (SELECT 1 FROM space_access x WHERE x.space_id=%(a)s.id AND "
                "((x.principal_type='user' AND x.principal_id=?) OR (x.principal_type='group' AND x.principal_id IN "
                "(SELECT group_id FROM group_members WHERE user_id=?)))))" % {"a": alias}, [uid, uid, uid])

    def space_level(self, sp, viewer):
        """-> 'admin' | 'edit' | 'view' | None for this viewer on space `sp` (a dict from space_get)."""
        if viewer and viewer.get("bypass"):
            return "admin"
        best = "view" if sp["visibility"] == "public" else None
        if not viewer:
            return best
        uid = viewer["id"]
        if sp["owner_id"] == uid:
            return "admin"
        for (a,) in self.db.conn().execute(
                "SELECT access FROM space_access WHERE space_id=? AND ((principal_type='user' AND principal_id=?) OR "
                "(principal_type='group' AND principal_id IN (SELECT group_id FROM group_members WHERE user_id=?)))", (sp["id"], uid, uid)).fetchall():
            if a == "edit":
                return "edit"
            best = best or "view"
        return best

    def space_get(self, owner, slug):
        r = self.db.conn().execute("SELECT " + COLS + " " + FROM + " WHERE o.username=? AND s.slug=?", (owner, slug)).fetchone()
        return _row(r) if r else None

    def space_by_id(self, sid):
        r = self.db.conn().execute("SELECT " + COLS + " " + FROM + " WHERE s.id=?", (sid,)).fetchone()
        return _row(r) if r else None

    def space_list(self, viewer, q="", kind="", sort="top", owner="", mine_uid=None, limit=24, offset=0):
        vs, va = self.space_visible_sql(viewer)
        where, args = [vs], list(va)
        if kind:
            where.append("s.kind=?")
            args.append(kind)
        if owner:
            where.append("o.username=?")
            args.append(owner)
        if mine_uid:
            where.append("s.owner_id=?")
            args.append(mine_uid)
        for w in [x for x in (q or "").lower().split() if x][:6]:
            like = "%" + w.replace("%", "").replace("_", "") + "%"
            where.append("(lower(s.slug) LIKE ? OR lower(s.title) LIKE ? OR lower(s.description) LIKE ? OR lower(s.tags) LIKE ? OR lower(o.username) LIKE ?)")
            args += [like] * 5
        order = {"new": "s.updated DESC", "top": "s.views DESC, s.updated DESC", "name": "s.slug"}.get(sort, "s.views DESC, s.updated DESC")
        c = self.db.conn()
        w = " WHERE " + " AND ".join(where)
        total = c.execute("SELECT COUNT(*) " + FROM + w, args).fetchone()[0]
        rows = c.execute("SELECT " + COLS + " " + FROM + w + " ORDER BY " + order + " LIMIT ? OFFSET ?", args + [int(limit), int(offset)]).fetchall()
        return [_row(r) for r in rows], total

    def space_kinds(self, viewer):
        vs, va = self.space_visible_sql(viewer)
        return {r[0]: r[1] for r in self.db.conn().execute("SELECT s.kind, COUNT(*) " + FROM + " WHERE " + vs + " GROUP BY s.kind", va).fetchall()}

    def space_save(self, owner_id, slug, kind, filename, size, sha, title=None, description=None, tags=None, visibility=None, settings=None):
        """Create the space, or replace its content (rev + 1). Metadata given as None is kept. -> (id, created: bool)"""
        now = time.time()
        with self.lock:
            c = self.db.conn()
            r = c.execute("SELECT id,rev,settings FROM spaces WHERE owner_id=? AND slug=?", (owner_id, slug)).fetchone()
            if r:
                sets, args = ["kind=?", "filename=?", "size=?", "sha256=?", "rev=?", "updated=?"], [kind, filename, size, sha, (r[1] or 1) + 1, now]
                for col, v in (("title", title), ("description", description), ("tags", tags), ("visibility", visibility)):
                    if v is not None:
                        sets.append(col + "=?")
                        args.append(v)
                if settings is not None:
                    old = json.loads(r[2] or "{}") if r[2] else {}
                    old.update(settings)
                    sets.append("settings=?")
                    args.append(json.dumps(old))
                c.execute("UPDATE spaces SET " + ",".join(sets) + " WHERE id=?", args + [r[0]])
                c.commit()
                return r[0], False
            cur = c.execute("INSERT INTO spaces(owner_id,slug,title,description,kind,visibility,filename,size,sha256,rev,settings,tags,views,created,updated) "
                            "VALUES(?,?,?,?,?,?,?,?,?,1,?,?,0,?,?)",
                            (owner_id, slug, title or "", description or "", kind, visibility or "public", filename, size, sha,
                             json.dumps(settings or {}), tags or "", now, now))
            c.commit()
            return cur.lastrowid, True

    def space_update(self, sid, **f):
        """Metadata only (title, description, tags, visibility, settings, kind)."""
        sets, args = [], []
        for k in ("title", "description", "tags", "visibility", "kind"):
            if k in f and f[k] is not None:
                sets.append(k + "=?")
                args.append(f[k])
        with self.lock:
            c = self.db.conn()
            if "settings" in f and f["settings"] is not None:
                r = c.execute("SELECT settings FROM spaces WHERE id=?", (sid,)).fetchone()
                old = json.loads(r[0] or "{}") if r and r[0] else {}
                old.update(f["settings"])
                sets.append("settings=?")
                args.append(json.dumps(old))
            if sets:
                sets.append("updated=?")
                args.append(time.time())
                c.execute("UPDATE spaces SET " + ",".join(sets) + " WHERE id=?", args + [sid])
                c.commit()

    def space_delete(self, sid):
        with self.lock:
            c = self.db.conn()
            c.execute("DELETE FROM space_access WHERE space_id=?", (sid,))
            c.execute("DELETE FROM spaces WHERE id=?", (sid,))
            c.commit()

    def space_view(self, sid):
        with self.lock:
            c = self.db.conn()
            c.execute("UPDATE spaces SET views=views+1 WHERE id=?", (sid,))
            c.commit()

    def space_access_list(self, sid):
        c = self.db.conn()
        out = []
        for r in c.execute("SELECT u.username AS name,u.display_name AS display_name,a.access AS access FROM space_access a "
                           "JOIN users u ON u.id=a.principal_id WHERE a.space_id=? AND a.principal_type='user' ORDER BY u.username", (sid,)).fetchall():
            out.append({"type": "user", "name": r["name"], "display_name": r["display_name"], "access": r["access"]})
        for r in c.execute("SELECT g.name AS name,a.access AS access,(SELECT COUNT(*) FROM group_members WHERE group_id=g.id) AS members FROM space_access a "
                           "JOIN groups g ON g.id=a.principal_id WHERE a.space_id=? AND a.principal_type='group' ORDER BY g.name", (sid,)).fetchall():
            out.append({"type": "group", "name": r["name"], "access": r["access"], "members": r["members"]})
        return out

    def space_access_grant(self, sid, ptype, pid, access):
        with self.lock:
            c = self.db.conn()
            c.execute("INSERT OR REPLACE INTO space_access VALUES(?,?,?,?)", (sid, ptype, pid, access))
            c.commit()

    def space_access_grant_many(self, sid, grants, access):
        """All grants [(ptype, pid)] in ONE transaction: all or none."""
        with self.lock:
            c = self.db.conn()
            try:
                for ptype, pid in grants:
                    c.execute("INSERT OR REPLACE INTO space_access VALUES(?,?,?,?)", (sid, ptype, pid, access))
                c.commit()
            except Exception:
                c.rollback()
                raise

    def space_access_revoke(self, sid, ptype, pid):
        with self.lock:
            c = self.db.conn()
            c.execute("DELETE FROM space_access WHERE space_id=? AND principal_type=? AND principal_id=?", (sid, ptype, pid))
            c.commit()
