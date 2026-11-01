"""SQL for Spaces (documents, slides, reports... published with `aihub space publish`). Mixed into `Repos`.
A space is addressed `owner@slug`. Visibility works like packages: public, or private + shared with users/groups as `view`, `comment`
or `edit`; the owner and site admins (`bypass`) manage it (`admin`). ONE function, `space_visible_sql`, says who sees what: every read path
uses it; `space_level` says what a viewer may do once they see it. A public space gives everybody VIEW; its `public_access` = 'comment'
lets any SIGNED-IN viewer comment too (never an anonymous one). Comments (`space_comments`) are soft deleted; every publish adds a row to
`space_versions` (the files of older revisions live next to the space folder, see routers_spaces.SpaceStore)."""
import json
import time

KINDS = ("doc", "slides", "report", "pdf", "sheet", "dashboard", "html")
LEVELS = ("view", "comment", "edit", "admin")            # each one includes the ones before it
PUBLIC_ACCESS = ("view", "comment")
COLS = ("s.id AS id,s.slug AS slug,s.title AS title,s.description AS description,s.kind AS kind,s.visibility AS visibility,s.filename AS filename,"
        "s.size AS size,s.sha256 AS sha256,s.rev AS rev,s.settings AS settings,s.tags AS tags,s.views AS views,s.created AS created,"
        "s.updated AS updated,s.owner_id AS owner_id,o.username AS owner,o.display_name AS owner_name,o.avatar_v AS owner_avatar_v,"
        "s.public_access AS public_access,(SELECT COUNT(*) FROM space_comments k WHERE k.space_id=s.id AND k.deleted=0) AS comments")
FROM = "FROM spaces s JOIN users o ON o.id=s.owner_id"


def _row(r):
    d = dict(r)
    try:
        d["settings"] = json.loads(d.get("settings") or "{}")
    except ValueError:
        d["settings"] = {}
    d["tags"] = [t for t in (d.get("tags") or "").split(",") if t]
    d["ref"] = "%s@%s" % (d["owner"], d["slug"])
    d["public_access"] = d.get("public_access") if d.get("public_access") in PUBLIC_ACCESS else "view"
    d["comments"] = int(d.get("comments") or 0)
    return d


def rank(level):
    """view 0 < comment 1 < edit 2 < admin 3; None / unknown = -1."""
    return LEVELS.index(level) if level in LEVELS else -1


CCOLS = ("k.id AS id,k.space_id AS space_id,k.user_id AS user_id,k.parent_id AS parent_id,k.body AS body,k.anchor AS anchor,k.rev AS rev,k.mentions AS mentions,"
         "k.resolved AS resolved,k.created AS created,k.updated AS updated,k.deleted AS deleted,u.username AS username,"
         "u.display_name AS display_name,u.avatar_v AS avatar_v")
VCOLS = ("v.space_id AS space_id,v.rev AS rev,v.kind AS kind,v.filename AS filename,v.size AS size,v.sha256 AS sha256,v.author_id AS author_id,"
         "v.note AS note,v.created AS created,u.username AS author,u.display_name AS author_name,u.avatar_v AS author_avatar_v")


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
        """-> 'admin' | 'edit' | 'comment' | 'view' | None for this viewer on space `sp` (a dict from space_get): the highest that applies."""
        if viewer and viewer.get("bypass"):
            return "admin"
        public = sp["visibility"] == "public"
        if not viewer:
            return "view" if public else None                 # signed out: never more than view, whatever public_access says
        uid = viewer["id"]
        if sp["owner_id"] == uid:
            return "admin"
        best = ("comment" if sp.get("public_access") == "comment" else "view") if public else None
        for r in self.db.conn().execute(
                "SELECT access FROM space_access WHERE space_id=? AND ((principal_type='user' AND principal_id=?) OR "
                "(principal_type='group' AND principal_id IN (SELECT group_id FROM group_members WHERE user_id=?)))", (sp["id"], uid, uid)).fetchall():
            a = r[0] if r[0] in LEVELS[:3] else "view"             # by position: a Postgres Row iterates its KEYS
            if rank(a) > rank(best):
                best = a
        return best

    def space_get(self, owner, slug):
        r = self.db.conn().execute("SELECT " + COLS + " " + FROM + " WHERE o.username=? AND s.slug=?", (owner, slug)).fetchone()
        return _row(r) if r else None

    def space_by_id(self, sid):
        r = self.db.conn().execute("SELECT " + COLS + " " + FROM + " WHERE s.id=?", (sid,)).fetchone()
        return _row(r) if r else None

    def _space_where(self, viewer, q="", kind="", tag="", owner="", mine_uid=None, shared_uid=None, since=None):
        """The ONE filter builder of the list and the facet counts (so both agree). TAG is an exact tag; SHARED_UID = spaces somebody
        else owns that were shared with this user or one of their groups (public ones are not 'shared')."""
        vs, va = self.space_visible_sql(viewer)
        where, args = [vs], list(va)
        if since is not None:
            where.append("s.updated>=?")
            args.append(float(since))
        if kind:
            where.append("s.kind=?")
            args.append(kind)
        if owner:
            where.append("o.username=?")
            args.append(owner)
        if tag:
            where.append("(',' || lower(s.tags) || ',') LIKE ? ESCAPE '\\'")
            args.append("%," + tag.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ",%")
        if mine_uid:
            where.append("s.owner_id=?")
            args.append(mine_uid)
        if shared_uid:
            where.append("s.owner_id<>? AND EXISTS (SELECT 1 FROM space_access x WHERE x.space_id=s.id AND ((x.principal_type='user' AND x.principal_id=?) "
                         "OR (x.principal_type='group' AND x.principal_id IN (SELECT group_id FROM group_members WHERE user_id=?))))")
            args += [shared_uid] * 3
        for w in [x for x in (q or "").lower().split() if x][:6]:
            like = "%" + w.replace("%", "").replace("_", "") + "%"
            where.append("(lower(s.slug) LIKE ? OR lower(s.title) LIKE ? OR lower(s.description) LIKE ? OR lower(s.tags) LIKE ? OR lower(o.username) LIKE ?)")
            args += [like] * 5
        return where, args

    ORDER = {"new": "s.updated DESC", "old": "s.updated, s.id", "top": "s.views DESC, s.updated DESC",
             "name": "lower(CASE WHEN s.title<>'' THEN s.title ELSE s.slug END), s.slug"}
    GROUP = {"kind": "s.kind, ", "owner": "o.username, "}

    def space_list(self, viewer, q="", kind="", sort="top", owner="", mine_uid=None, limit=24, offset=0, since=None, tag="", shared_uid=None, group=""):
        """SINCE (unix time): only spaces changed at or after it, OLDEST change first (a cursor: the next call passes the last `updated`;
        `>=` so equal timestamps on a page edge are not lost, callers upsert). Uses the spaces_updated index; no COUNT for it.
        GROUP ('kind' | 'owner') puts the rows of one group together first, SORT orders inside it; the id is the last key, so pages never overlap."""
        where, args = self._space_where(viewer, q, kind, tag, owner, mine_uid, shared_uid, since)
        c = self.db.conn()
        w = " WHERE " + " AND ".join(where)
        if since is not None:
            rows = c.execute("SELECT " + COLS + " " + FROM + w + " ORDER BY s.updated, s.id LIMIT ? OFFSET ?", args + [int(limit), int(offset)]).fetchall()
            return [_row(r) for r in rows], len(rows)
        total = c.execute("SELECT COUNT(*) " + FROM + w, args).fetchone()[0]
        order = self.GROUP.get(group, "") + self.ORDER.get(sort, self.ORDER["top"]) + ", s.id"
        rows = c.execute("SELECT " + COLS + " " + FROM + w + " ORDER BY " + order + " LIMIT ? OFFSET ?", args + [int(limit), int(offset)]).fetchall()
        return [_row(r) for r in rows], total

    def space_facets(self, viewer, q="", kind="", tag="", owner="", mine_uid=None, shared_uid=None, uid=None):
        """Counts for the filter rail. Each facet is counted with every OTHER filter applied but not its own, so a chosen type still shows
        what the other types would give. `scopes` ignores the scope itself. Rows are read by position (Postgres rows iterate their keys)."""
        c = self.db.conn()
        base = dict(q=q, kind=kind, tag=tag, owner=owner, mine_uid=mine_uid, shared_uid=shared_uid)

        def where(*off):
            kw = dict(base)
            for k in off:
                kw[k] = None if k.endswith("_uid") else ""
            w, a = self._space_where(viewer, **kw)
            return " WHERE " + " AND ".join(w), a
        w, a = where()
        total = c.execute("SELECT COUNT(*) " + FROM + w, a).fetchone()[0]
        w, a = where("kind")
        kinds = {r[0]: r[1] for r in c.execute("SELECT s.kind, COUNT(*) " + FROM + w + " GROUP BY s.kind", a).fetchall()}
        w, a = where("owner")
        owners = [{"username": r[0], "name": r[1] or "", "n": r[2]} for r in c.execute(
            "SELECT o.username, o.display_name, COUNT(*) " + FROM + w + " GROUP BY o.username, o.display_name ORDER BY COUNT(*) DESC, o.username LIMIT 300", a).fetchall()]
        w, a = where("tag")
        counts = {}
        for r in c.execute("SELECT s.tags, COUNT(*) " + FROM + w + " AND s.tags<>'' GROUP BY s.tags", a).fetchall():
            for t in (r[0] or "").split(","):
                if t:
                    counts[t] = counts.get(t, 0) + r[1]
        tags = [{"tag": t, "n": n} for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:300]]
        scopes = {}
        if uid:
            for name, kw in (("all", {}), ("mine", {"mine_uid": uid}), ("shared", {"shared_uid": uid})):
                k = {**base, "mine_uid": None, "shared_uid": None, **kw}
                ww, aa = self._space_where(viewer, **k)
                scopes[name] = c.execute("SELECT COUNT(*) " + FROM + " WHERE " + " AND ".join(ww), aa).fetchone()[0]
        return {"total": total, "kinds": kinds, "owners": owners, "tags": tags, "scopes": scopes}

    def space_kinds(self, viewer):
        vs, va = self.space_visible_sql(viewer)
        return {r[0]: r[1] for r in self.db.conn().execute("SELECT s.kind, COUNT(*) " + FROM + " WHERE " + vs + " GROUP BY s.kind", va).fetchall()}

    def space_save(self, owner_id, slug, kind, filename, size, sha, title=None, description=None, tags=None, visibility=None, settings=None,
                   author_id=None, note=""):
        """Create the space, or replace its content (rev + 1). Metadata given as None is kept. Records the new revision in space_versions
        (and the one it replaces, when a space from before version history has no row for it yet). -> (id, created: bool, rev)"""
        now = time.time()
        with self.lock:
            c = self.db.conn()
            r = c.execute("SELECT id,rev,settings FROM spaces WHERE owner_id=? AND slug=?", (owner_id, slug)).fetchone()
            if r:
                self._version_ensure(c, r[0])
                rev = max((r[1] or 1), self._version_max(c, r[0])) + 1      # never reuse a number, even after a pruned / deleted revision
                sets, args = ["kind=?", "filename=?", "size=?", "sha256=?", "rev=?", "updated=?"], [kind, filename, size, sha, rev, now]
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
                c.execute("INSERT INTO space_versions(space_id,rev,kind,filename,size,sha256,author_id,note,created) VALUES(?,?,?,?,?,?,?,?,?)",
                          (r[0], rev, kind, filename, size, sha, author_id or owner_id, note or "", now))
                c.commit()
                return r[0], False, rev
            cur = c.execute("INSERT INTO spaces(owner_id,slug,title,description,kind,visibility,filename,size,sha256,rev,settings,tags,views,created,updated) "
                            "VALUES(?,?,?,?,?,?,?,?,?,1,?,?,0,?,?)",
                            (owner_id, slug, title or "", description or "", kind, visibility or "public", filename, size, sha,
                             json.dumps(settings or {}), tags or "", now, now))
            c.execute("INSERT INTO space_versions(space_id,rev,kind,filename,size,sha256,author_id,note,created) VALUES(?,1,?,?,?,?,?,?,?)",
                      (cur.lastrowid, kind, filename, size, sha, author_id or owner_id, note or "", now))
            c.commit()
            return cur.lastrowid, True, 1

    def space_update(self, sid, **f):
        """Metadata only (title, description, tags, visibility, public_access, settings, kind)."""
        sets, args = [], []
        for k in ("title", "description", "tags", "visibility", "public_access", "kind"):
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
            c.execute("DELETE FROM notifications WHERE item_id=?", (sid,))
            c.execute("DELETE FROM space_comments WHERE space_id=?", (sid,))
            c.execute("DELETE FROM space_versions WHERE space_id=?", (sid,))
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

    # ---------------------------------------------------------------- comments
    def space_comments(self, sid):
        """Every comment of a space (soft-deleted ones too: a thread keeps its shape), oldest first."""
        rows = self.db.conn().execute("SELECT " + CCOLS + " FROM space_comments k LEFT JOIN users u ON u.id=k.user_id WHERE k.space_id=? "
                                      "ORDER BY k.created, k.id", (sid,)).fetchall()
        return [dict(r) for r in rows]

    def space_comment_revs(self, sid):
        """{rev: number of live comments written about it}"""
        return {int(r[0]): int(r[1]) for r in self.db.conn().execute(
            "SELECT rev, COUNT(*) FROM space_comments WHERE space_id=? AND deleted=0 GROUP BY rev", (sid,)).fetchall()}

    def space_comment(self, sid, cid):
        r = self.db.conn().execute("SELECT " + CCOLS + " FROM space_comments k LEFT JOIN users u ON u.id=k.user_id WHERE k.space_id=? AND k.id=?",
                                   (sid, int(cid))).fetchone()
        return dict(r) if r else None

    def space_comment_add(self, sid, uid, body, parent_id=None, anchor="", rev=1, mentions=""):
        now = time.time()
        with self.lock:
            c = self.db.conn()
            cur = c.execute("INSERT INTO space_comments(space_id,user_id,parent_id,body,anchor,rev,mentions,resolved,created,updated,deleted) VALUES(?,?,?,?,?,?,?,0,?,?,0)",
                            (sid, uid, parent_id, body, anchor or "", rev, mentions or "", now, now))
            c.commit()
            return cur.lastrowid

    def space_comment_edit(self, sid, cid, body=None, resolved=None, mentions=None):
        """body: new text (`updated` moves, so the page can say 'edited'); resolved: True / False. None = keep."""
        sets, args = [], []
        if body is not None:
            sets += ["body=?", "updated=?", "mentions=?"]
            args += [body, time.time(), mentions or ""]
        if resolved is not None:
            sets.append("resolved=?")
            args.append(1 if resolved else 0)
        if not sets:
            return
        with self.lock:
            c = self.db.conn()
            c.execute("UPDATE space_comments SET " + ",".join(sets) + " WHERE space_id=? AND id=?", args + [sid, int(cid)])
            c.commit()

    def space_comment_delete(self, sid, cid):
        """Soft delete: the text goes, the row stays so its replies keep their place."""
        with self.lock:
            c = self.db.conn()
            c.execute("UPDATE space_comments SET deleted=1, body='', mentions='', updated=? WHERE space_id=? AND id=?", (time.time(), sid, int(cid)))
            c.execute("DELETE FROM notifications WHERE item_id=? AND comment_id=?", (sid, int(cid)))          # nobody is pointed at text that is gone
            c.commit()

    # ---------------------------------------------------------------- versions
    @staticmethod
    def _version_max(c, sid):
        r = c.execute("SELECT MAX(rev) FROM space_versions WHERE space_id=?", (sid,)).fetchone()
        return int(r[0] or 0) if r else 0

    @staticmethod
    def _version_ensure(c, sid):
        """A space published before version history has no row for its current revision: add one from the space itself (no commit)."""
        r = c.execute("SELECT rev,kind,filename,size,sha256,owner_id,updated FROM spaces WHERE id=?", (sid,)).fetchone()
        if not r:
            return False
        if c.execute("SELECT 1 FROM space_versions WHERE space_id=? AND rev=?", (sid, r[0] or 1)).fetchone():
            return False
        c.execute("INSERT INTO space_versions(space_id,rev,kind,filename,size,sha256,author_id,note,created) VALUES(?,?,?,?,?,?,?,?,?)",
                  (sid, r[0] or 1, r[1], r[2] or "", r[3] or 0, r[4] or "", r[5], "", r[6] or time.time()))
        return True

    def space_versions_backfill(self):
        """Once (marker `space_versions_v1`): every existing space gets a version row for its current revision. -> how many were added."""
        if self.setting("space_versions_v1") == "1":
            return 0
        n = 0
        with self.lock:
            c = self.db.conn()
            for r in c.execute("SELECT id FROM spaces").fetchall():
                n += 1 if self._version_ensure(c, r[0]) else 0
            c.execute("INSERT OR REPLACE INTO app_settings VALUES('space_versions_v1','1')")
            c.commit()
        return n

    def space_versions(self, sid):
        """Revisions of a space, newest first (the current one included; a row is added for it if it is missing)."""
        with self.lock:
            c = self.db.conn()
            if self._version_ensure(c, sid):
                c.commit()
        rows = self.db.conn().execute("SELECT " + VCOLS + " FROM space_versions v LEFT JOIN users u ON u.id=v.author_id WHERE v.space_id=? "
                                      "ORDER BY v.rev DESC", (sid,)).fetchall()
        return [dict(r) for r in rows]

    def space_version(self, sid, rev):
        r = self.db.conn().execute("SELECT " + VCOLS + " FROM space_versions v LEFT JOIN users u ON u.id=v.author_id WHERE v.space_id=? AND v.rev=?",
                                   (sid, int(rev))).fetchone()
        return dict(r) if r else None

    def space_version_delete(self, sid, rev):
        with self.lock:
            c = self.db.conn()
            c.execute("DELETE FROM space_versions WHERE space_id=? AND rev=?", (sid, int(rev)))
            c.commit()

    def space_versions_prune(self, sid, keep):
        """Keep the newest `keep` revisions (at least 1); the CURRENT one is never dropped. -> the revision numbers removed."""
        keep = max(1, int(keep))
        with self.lock:
            c = self.db.conn()
            r = c.execute("SELECT rev FROM spaces WHERE id=?", (sid,)).fetchone()
            cur = int(r[0] or 1) if r else None
            revs = [int(x[0]) for x in c.execute("SELECT rev FROM space_versions WHERE space_id=? ORDER BY rev DESC", (sid,)).fetchall()]
            kept = set(revs[:keep]) | ({cur} if cur is not None else set())
            gone = [v for v in revs if v not in kept]
            for v in gone:
                c.execute("DELETE FROM space_versions WHERE space_id=? AND rev=?", (sid, v))
            if gone:
                c.commit()
            return gone

    # ---------------------------------------------------------------- people a comment can point at, and who is told
    def mention_user(self, name):
        """The ACTIVE account called `name` (exact, else lower case). -> {id, username, display_name, role, avatar_v} or None."""
        c = self.db.conn()
        for n in dict.fromkeys((name, name.lower())):
            r = c.execute("SELECT id,username,display_name,role,avatar_v FROM users WHERE username=? AND status='active' AND role<>'anonymous'", (n,)).fetchone()
            if r:
                return dict(r)
        return None

    def group_member_users(self, gid, cap=500):
        """Active members of a group: [{id, username, role}]."""
        rows = self.db.conn().execute("SELECT u.id AS id,u.username AS username,u.role AS role FROM group_members m JOIN users u ON u.id=m.user_id "
                                      "WHERE m.group_id=? AND u.status='active' ORDER BY u.id LIMIT ?", (gid, int(cap))).fetchall()
        return [dict(r) for r in rows]

    def space_commenter_ids(self, sid):
        """People with a live comment on a space."""
        return [r[0] for r in self.db.conn().execute("SELECT DISTINCT user_id FROM space_comments WHERE space_id=? AND deleted=0 AND user_id IS NOT NULL", (sid,)).fetchall()]

    def user_row(self, uid):
        r = self.db.conn().execute("SELECT id,username,display_name,role,status FROM users WHERE id=?", (uid,)).fetchone()
        return dict(r) if r else None

    def mention_candidates(self, q, limit=8):
        """Active accounts and groups whose name starts with / contains `q` (prefix matches first). -> (users, groups)"""
        q = q.replace("%", "").replace("_", "").lower()
        like, pre = "%" + q + "%", q + "%"
        c = self.db.conn()
        us = c.execute("SELECT id,username,display_name,avatar_v,role FROM users WHERE role<>'anonymous' AND status='active' AND "
                       "(lower(username) LIKE ? OR lower(display_name) LIKE ?) ORDER BY CASE WHEN lower(username) LIKE ? THEN 0 ELSE 1 END, username LIMIT ?",
                       (like, like, pre, int(limit))).fetchall()
        gs = c.execute("SELECT g.id AS id,g.name AS name,(SELECT COUNT(*) FROM group_members m WHERE m.group_id=g.id) AS members FROM groups g "
                       "WHERE lower(g.name) LIKE ? ORDER BY CASE WHEN lower(g.name) LIKE ? THEN 0 ELSE 1 END, g.name LIMIT ?", (like, pre, int(limit))).fetchall()
        return [dict(x) for x in us], [dict(x) for x in gs]

    def space_group_granted(self, sid, gid):
        return bool(self.db.conn().execute("SELECT 1 FROM space_access WHERE space_id=? AND principal_type='group' AND principal_id=?", (sid, gid)).fetchone())

    # ---------------------------------------------------------------- notifications (one row per person and event; `seen` 0 / 1)
    NCOLS = ("n.id AS id,n.kind AS kind,n.item_id AS item_id,n.comment_id AS comment_id,n.rev AS rev,n.body AS body,n.via AS via,n.created AS created,"
             "n.seen AS seen,a.username AS actor,a.display_name AS actor_name,a.avatar_v AS actor_avatar_v,"
             "s.slug AS slug,s.title AS title,s.rev AS current_rev,o.username AS owner")
    NFROM = ("FROM notifications n LEFT JOIN users a ON a.id=n.actor_id LEFT JOIN spaces s ON s.id=n.item_id LEFT JOIN users o ON o.id=s.owner_id")

    def notify(self, rows):
        """rows = [{user_id, kind, actor_id, item_id, comment_id, rev, body, via}]. One transaction; a failure here must never break the caller."""
        rows = [r for r in rows if r.get("user_id")]
        if not rows:
            return 0
        now = time.time()
        with self.lock:
            c = self.db.conn()
            try:
                for r in rows:
                    if r["kind"] == "share":                           # the same space shared again (or through a group too): one alert, the newest
                        c.execute("DELETE FROM notifications WHERE user_id=? AND item_id=? AND kind='share' AND seen=0", (r["user_id"], r.get("item_id") or 0))
                    c.execute("INSERT INTO notifications(user_id,kind,actor_id,item_id,comment_id,rev,body,via,created,seen) VALUES(?,?,?,?,?,?,?,?,?,0)",
                              (r["user_id"], r["kind"], r.get("actor_id"), r.get("item_id") or 0, r.get("comment_id") or 0, r.get("rev") or 0,
                               (r.get("body") or "")[:300], r.get("via") or "", now))
                for uid in {r["user_id"] for r in rows}:               # read ones older than 90 days go; unread ones stay until seen
                    c.execute("DELETE FROM notifications WHERE user_id=? AND seen=1 AND created<?", (uid, now - 90 * 86400))
                c.commit()
            except Exception:
                c.rollback()
                raise
        return len(rows)

    def _nvis(self, viewer):
        """Visible-space filter for notifications: a space that went private (or was unshared) no longer shows its alerts."""
        vs, va = self.space_visible_sql(viewer, "s")
        return "(n.item_id=0 OR (s.id IS NOT NULL AND %s))" % vs, va

    def notifications(self, viewer, unread=False, kinds=(), limit=30, before=None):
        """Newest first. `before` = a notification id (page back). -> (rows, more)"""
        vs, va = self._nvis(viewer)
        where, args = ["n.user_id=?", vs], [viewer["id"]] + list(va)
        if unread:
            where.append("n.seen=0")
        if kinds:
            where.append("n.kind IN (%s)" % ",".join("?" * len(kinds)))
            args += list(kinds)
        if before:
            where.append("n.id<?")
            args.append(int(before))
        rows = self.db.conn().execute("SELECT " + self.NCOLS + " " + self.NFROM + " WHERE " + " AND ".join(where) + " ORDER BY n.id DESC LIMIT ?",
                                      args + [int(limit) + 1]).fetchall()
        rows = [dict(r) for r in rows]
        return rows[:int(limit)], len(rows) > int(limit)

    def notifications_unread(self, viewer):
        vs, va = self._nvis(viewer)
        r = self.db.conn().execute("SELECT COUNT(*) " + self.NFROM + " WHERE n.user_id=? AND n.seen=0 AND " + vs, [viewer["id"]] + list(va)).fetchone()
        return int(r[0] or 0)

    def notifications_mark(self, uid, ids=None, seen=True):
        """ids None = every one of this person's."""
        with self.lock:
            c = self.db.conn()
            if ids is None:
                c.execute("UPDATE notifications SET seen=? WHERE user_id=?", (1 if seen else 0, uid))
            else:
                for i in ids[:500]:
                    c.execute("UPDATE notifications SET seen=? WHERE user_id=? AND id=?", (1 if seen else 0, uid, int(i)))
            c.commit()

    def notification_delete(self, uid, nid):
        with self.lock:
            c = self.db.conn()
            c.execute("DELETE FROM notifications WHERE user_id=? AND id=?", (uid, int(nid)))
            c.commit()
