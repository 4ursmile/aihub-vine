"""Spaces: documents, slides, reports, PDFs, sheets, dashboards and HTML pages published with `aihub space publish`, addressed `owner@slug`.
Visibility and sharing work like packages (public | private; view | comment | edit for users and groups). Files live in `<data>/spaces/<id>/`
(the current revision) and `<data>/spaces/<id>.v/<rev>/` (older revisions, capped by the admin number `space_versions_keep`).
Untrusted HTML never runs on the hub's origin: it is served from a signed URL with a sandbox CSP and shown in a sandboxed iframe."""
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import urllib.parse
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from ..core import archive, textsafe
from . import basepath, spaceconv
from .deps import current_user, require_perm, require_user, viewer_of
from .routers import C, _avatar_url, _cache_scope, _clean
from .spaceconv import SLUG, SpaceError, TRANSITIONS
from .spaces_repo import KINDS, LEVELS, PUBLIC_ACCESS, rank

r = APIRouter(prefix="/api/v1")
log = logging.getLogger("aihub.spaces")
VIEWS = ("canvas", "fit", "full")
LINK_TTL = 6 * 3600
COMMENT_MAX = 5000
NOTE_MAX = 500
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u202a-\u202e\u2066-\u2069]")    # control + bidi override characters (newline and tab stay)
VIEW_SANDBOX = ("sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox allow-forms allow-downloads allow-modals; default-src 'none'; "
                "script-src 'unsafe-inline' 'self' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://unpkg.com; "
                "style-src 'unsafe-inline' 'self' https:; img-src 'self' data: blob: https:; font-src 'self' data: https:; media-src 'self' data: blob: https:; "
                "connect-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'self'; frame-ancestors 'self'")
VIEW_FILE = "frame-ancestors 'self'; script-src 'none'"


def R(request): return request.app.state.repos


class SpaceStore:
    """Files of every space plus the key that signs view links. Replacing a space swaps a whole folder, so a reader never sees half of it;
    the folder it replaces moves to `<id>.v/<rev>/` (version history). Each built folder carries a `.rev` file with its revision number."""
    def __init__(self, data_dir):
        self.base = os.path.join(data_dir, "spaces")
        os.makedirs(self.base, exist_ok=True)
        self._key_file = os.path.join(data_dir, ".spaces.key")
        self._key = None
        self._lock = threading.Lock()
        self._put_lock = threading.Lock()
        for n in os.listdir(self.base):                       # leftovers of a crashed build
            if n.startswith(".tmp-"):
                shutil.rmtree(os.path.join(self.base, n), ignore_errors=True)

    def key(self):
        with self._lock:
            if self._key is None:
                try:
                    with open(self._key_file, "rb") as f:
                        self._key = f.read()
                except OSError:
                    self._key = secrets.token_bytes(32)
                    fd = os.open(self._key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "wb") as f:
                        f.write(self._key)
            return self._key

    def dir(self, sid):
        return os.path.join(self.base, str(int(sid)))

    def scratch(self):
        return tempfile.mkdtemp(prefix=".tmp-", dir=self.base)

    def vbase(self, sid):
        return os.path.join(self.base, "%d.v" % int(sid))

    def vdir(self, sid, rev):
        return os.path.join(self.vbase(sid), str(int(rev)))

    @staticmethod
    def rev_of(folder):
        try:
            with open(os.path.join(folder, ".rev")) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    def put(self, sid, built, rev, prev=None, keep=True):
        """Make `built` (revision `rev`) the current content. The folder it replaces is kept as `<id>.v/<its rev>` (`prev` = its revision when
        it has no `.rev` file: a folder from before version history), or deleted with keep=False. Two publishes finishing out of order: the
        older one goes straight into the history, the newer one stays current."""
        with open(os.path.join(built, ".rev"), "w") as f:
            f.write(str(int(rev)))
        with self._put_lock:
            dst, old = self.dir(sid), self.dir(sid) + ".old"
            have = self.rev_of(dst) if os.path.isdir(dst) else None
            if have is not None and have > rev:
                return self._archive(sid, built, rev, keep)
            shutil.rmtree(old, ignore_errors=True)
            if os.path.isdir(dst):
                os.rename(dst, old)
            os.rename(built, dst)
            if os.path.isdir(old):
                self._archive(sid, old, have if have is not None else prev, keep)

    def _archive(self, sid, folder, rev, keep):
        if not keep or not rev:
            shutil.rmtree(folder, ignore_errors=True)
            return
        to = self.vdir(sid, rev)
        os.makedirs(self.vbase(sid), exist_ok=True)
        shutil.rmtree(to, ignore_errors=True)
        os.rename(folder, to)

    def rdir(self, sid, rev=None, current=None):
        """Folder of revision `rev` (None or the `current` one = the live folder)."""
        return self.dir(sid) if rev is None or rev == current else self.vdir(sid, rev)

    def has_rev(self, sid, rev, current):
        return os.path.isdir(self.rdir(sid, rev, current))

    def drop_rev(self, sid, rev):
        shutil.rmtree(self.vdir(sid, rev), ignore_errors=True)

    def sweep(self, sid, keep_revs):
        """Delete history folders of revisions that no longer have a version row."""
        try:
            names = os.listdir(self.vbase(sid))
        except OSError:
            return
        for n in names:
            if not (n.isdigit() and int(n) in keep_revs):
                shutil.rmtree(os.path.join(self.vbase(sid), n), ignore_errors=True)

    def remove(self, sid):
        shutil.rmtree(self.dir(sid), ignore_errors=True)
        shutil.rmtree(self.vbase(sid), ignore_errors=True)

    def sign(self, sid, rev, ttl=LINK_TTL, pin=False):
        """`exp.sig` (the current revision, as before), or with pin=True `exp.sig.rev`: a link to that one revision, valid after a republish."""
        exp = int(time.time()) + ttl
        sig = hmac.new(self.key(), ("%d|%d|%d" % (sid, rev, exp)).encode(), hashlib.sha256).hexdigest()[:40]
        return "%d.%s.%d" % (exp, sig, rev) if pin else "%d.%s" % (exp, sig)

    @staticmethod
    def pinned(tok):
        """The revision a pinned link names, else None."""
        p = (tok or "").split(".")
        return int(p[2]) if len(p) == 3 and p[2].isdigit() else None

    def check(self, sid, rev, tok):
        try:
            exp, sig = tok.split(".")[:2]
            good = hmac.new(self.key(), ("%d|%d|%d" % (sid, rev, int(exp))).encode(), hashlib.sha256).hexdigest()[:40]
            return int(exp) >= time.time() and hmac.compare_digest(sig, good)
        except (ValueError, TypeError):
            return False

    def read(self, sid, name, folder=None):
        p = os.path.join(folder or self.dir(sid), name)
        try:
            with open(p, "rb") as f:
                return f.read()
        except OSError:
            return None

    def original(self, sid, folder=None):
        d = os.path.join(folder or self.dir(sid), "orig")
        try:
            n = sorted(os.listdir(d))[0]
        except (OSError, IndexError):
            return None, None
        return n, os.path.join(d, n)


def S(request):
    st = getattr(request.app.state, "spaces", None)
    if st is None:
        st = request.app.state.spaces = SpaceStore(request.app.state.settings.data_dir)
    return st


# ---------------------------------------------------------------- helpers
def _split(ref):
    ref = urllib.parse.unquote(ref)
    if "@" not in ref:
        raise HTTPException(404, "space not found (address is author@space-name)")
    o, s = ref.split("@", 1)
    return o.strip(), s.strip().lower()


def _find(request, ref):
    owner, slug = _split(ref)
    sp = R(request).space_get(owner, slug) or (R(request).space_get(owner.lower(), slug) if owner != owner.lower() else None)
    if not sp:
        raise HTTPException(404, "space not found")
    return sp


def _load(request, ref, u, need="view"):
    """-> (space, level). 404 when the viewer may not even see it, 403 when they lack `need` (view < comment < edit < admin)."""
    sp = _find(request, ref)
    level = R(request).space_level(sp, viewer_of(R(request), u))
    if not level:
        raise HTTPException(404, "space not found")
    if rank(level) < rank(need):
        if need == "comment":
            raise HTTPException(401 if not u else 403, "sign in to comment" if not u else "you may view this space but not comment on it")
        raise HTTPException(403, "you may %s this space but not change it" % level if need == "edit" else "only the owner or an admin may do that")
    return sp, level


def _ser(sp, level=None, full=False):
    d = {"ref": sp["ref"], "owner": sp["owner"], "owner_name": sp.get("owner_name") or "", "owner_avatar": _avatar_url(sp["owner"], sp.get("owner_avatar_v") or 0),
         "slug": sp["slug"], "title": sp["title"] or sp["slug"], "description": sp["description"], "kind": sp["kind"], "visibility": sp["visibility"],
         "tags": sp["tags"], "views": sp["views"], "size": sp["size"], "rev": sp["rev"], "created": sp["created"], "updated": sp["updated"],
         "settings": sp["settings"], "filename": sp["filename"], "public_access": sp.get("public_access") or "view", "comments": sp.get("comments") or 0}
    if level is not None:
        d["level"] = level
        d["can_comment"] = rank(level) >= rank("comment")
        d["can_edit"] = level in ("edit", "admin")
        d["can_manage"] = level == "admin"
    if full:
        d["sha256"] = sp["sha256"]
    return d


def _settings(q):
    """Validated viewer defaults from a request (query / JSON). Unknown or bad values are an error: a typo should not silently publish."""
    out = {}
    v = q.get("view")
    if v not in (None, ""):
        if v not in VIEWS:
            raise HTTPException(400, "view must be one of: " + ", ".join(VIEWS))
        out["view"] = v
    t = q.get("transition")
    if t not in (None, ""):
        if t not in TRANSITIONS:
            raise HTTPException(400, "transition must be one of: " + ", ".join(TRANSITIONS))
        out["transition"] = t
    a = q.get("autoplay")
    if a not in (None, ""):
        try:
            n = int(a)
        except (TypeError, ValueError):
            raise HTTPException(400, "autoplay is seconds per slide (0 = off)")
        if not 0 <= n <= 3600:
            raise HTTPException(400, "autoplay must be 0..3600 seconds")
        out["autoplay"] = n
    lp = q.get("loop")
    if lp not in (None, ""):
        out["loop"] = str(lp).lower() in ("1", "true", "yes", "on")
    return out


def _vis(request, v, new=False):
    if v in (None, ""):
        return request.app.state.repos.initial_visibility() if new else None
    if v not in ("public", "private"):
        raise HTTPException(400, "visibility must be public or private")
    if v == "private" and R(request).setting("allow_private", "1") != "1":
        raise HTTPException(400, "private spaces are switched off on this hub")
    return v


def _tags(v):
    if v is None:
        return None
    items = v if isinstance(v, list) else str(v).split(",")
    return ",".join(dict.fromkeys(t for t in (re.sub(r"[^a-z0-9._-]+", "-", str(x).strip().lower()).strip("-") for x in items) if t))[:300]


def _limit(request):
    return request.app.state.sync.num("direct_max_mb") * 1024 * 1024


async def _receive(request, store):
    limit = _limit(request)
    n = request.headers.get("content-length")
    if n and n.isdigit() and int(n) > limit:
        raise HTTPException(413, "file too large (limit %d MB, admin setting direct_max_mb)" % (limit >> 20))
    tmp = os.path.join(store.scratch(), "upload")
    size, h = 0, hashlib.sha256()
    with open(tmp, "wb") as f:
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, "file too large (limit %d MB, admin setting direct_max_mb)" % (limit >> 20))
            h.update(chunk)
            await run_in_threadpool(f.write, chunk)
    if not size:
        raise HTTPException(400, "empty upload")
    return tmp, size, h.hexdigest()


def _note(v):
    return _clean(str(v), NOTE_MAX) if v not in (None, "") else ""


def _store_rev(request, sid, built, rev, prev):
    """Files of the new revision in place, the replaced one into the history, then the history cut to `space_versions_keep`."""
    repos, store = R(request), S(request)
    keep = request.app.state.sync.num("space_versions_keep")
    store.put(sid, built, rev, prev=prev, keep=keep > 1)
    repos.space_versions_prune(sid, keep)
    store.sweep(sid, {v["rev"] for v in repos.space_versions(sid)})
    _changed(request)


def _publish(request, u, owner, slug, tmp, size, sha, q):
    """Build, then store. `owner` = the account the space belongs to (the uploader for a new space)."""
    repos, store = R(request), S(request)
    filename = urllib.parse.unquote(q.get("filename") or request.headers.get("x-filename") or "") or slug
    existing = repos.space_get(owner["username"], slug)
    if existing:
        if repos.space_level(existing, viewer_of(repos, u)) not in ("edit", "admin"):
            raise HTTPException(403, "%s@%s belongs to someone else: you need edit access to publish a new version" % (owner["username"], slug))
    elif owner["id"] != u["id"] and "admin" not in repos.perms(u["role"]):
        raise HTTPException(403, "you can only create spaces under your own name")
    kind_hint = (q.get("kind") or "").lower() or None
    work = os.path.dirname(tmp)
    built = os.path.join(work, "built")
    try:
        res = spaceconv.build(built, tmp, filename, kind_hint)
    except SpaceError as e:
        shutil.rmtree(work, ignore_errors=True)
        raise HTTPException(400, str(e))
    settings = _settings(q)
    try:
        deck = None
        if res["kind"] == "slides":
            with open(os.path.join(built, "deck.json"), encoding="utf-8") as f:
                deck = json.load(f)
        title = q.get("title")
        if title is None and not existing:
            title = (deck or {}).get("title") or spaceconv.title_of(filename)
        vis = _vis(request, q.get("visibility"), new=not existing)
        sid, created, rev = repos.space_save(owner["id"], slug, res["kind"], os.path.basename(filename)[:200], size, sha,
                                             title=_clean(title, 200) if title is not None else None,
                                             description=_clean(q.get("description"), 1000) if q.get("description") is not None else None,
                                             tags=_tags(q.get("tags")), visibility=vis, settings=settings, author_id=u["id"], note=_note(q.get("note")))
        _store_rev(request, sid, built, rev, existing["rev"] if existing else None)
    except HTTPException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    shutil.rmtree(work, ignore_errors=True)
    repos.audit(u["username"], "space.publish" if created else "space.update", "%s@%s" % (owner["username"], slug), res["kind"])
    sp = repos.space_by_id(sid)
    if not created:
        _notify_version(request, sp, u, rev, _note(q.get("note")))
    return {"space": _ser(sp, repos.space_level(sp, viewer_of(repos, u)), full=True), "created": created, "path": "/s/" + sp["ref"]}


# ---------------------------------------------------------------- publish
@r.post("/spaces")
async def space_create(request: Request, name: str = "", u=Depends(require_perm("publish"))):
    """Body = the file (or a .zip / .tar.gz site). Query: name (the slug; default from the file name), filename, kind, title, description,
    tags, visibility, view, transition, autoplay, loop, note (shown in the version history). Publishing the same name again adds a new
    revision (the previous ones stay in the history) and keeps the metadata you did not send."""
    if not u.get("id"):
        raise HTTPException(401, "sign in first: aihub login")
    q = dict(request.query_params)
    slug = (name or spaceconv.slugify(urllib.parse.unquote(q.get("filename") or request.headers.get("x-filename") or ""))).lower()
    if not SLUG.match(slug):
        raise HTTPException(400, "space name: lowercase letters, digits, . _ - (max 64)")
    tmp, size, sha = await _receive(request, S(request))
    try:
        return await run_in_threadpool(_publish, request, u, u, slug, tmp, size, sha, q)
    finally:
        shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)


@r.put("/spaces/{ref}/content")
async def space_replace(ref: str, request: Request, u=Depends(require_perm("publish"))):
    """New content for an existing space (needs edit access), also for one that belongs to somebody else."""
    sp, _ = _load(request, ref, u, "edit")
    q = dict(request.query_params)
    tmp, size, sha = await _receive(request, S(request))
    try:
        owner = R(request).user_by_name(sp["owner"])
        return await run_in_threadpool(_publish, request, u, owner, sp["slug"], tmp, size, sha, q)
    finally:
        shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)


# ---------------------------------------------------------------- browse
def _lists(request, u, **kw):
    repos = R(request)
    items, total = repos.space_list(viewer_of(repos, u), **kw)
    return {"items": [_ser(x) for x in items], "total": total}


CHANGES_TTL = 5                                       # `?since=` answers per viewer: clients that poll for new spaces share one query


def _changed(request):
    """Every space write drops the cached `?since=` answers, so a new or edited space is found by the very next poll."""
    C(request).invalidate("sp:chg:")
    C(request).invalidate("sp:fac:")


SORTS = ("new", "old", "top", "name")
FACET_TTL = 15                                        # filter-rail counts: a few seconds old is fine, a write drops them (`_changed`)


def _scope_args(u, mine, shared):
    if (mine or shared) and not u:
        raise HTTPException(401, "authentication required")
    return (u["id"] if mine else None), (u["id"] if shared and not mine else None)


@r.get("/spaces")
def space_list(request: Request, q: str = "", kind: str = "", sort: str = "top", owner: str = "", mine: bool = False, limit: int = 24, offset: int = 0,
               since: Optional[float] = None, tag: str = "", shared: bool = False, group: str = "", u=Depends(current_user)):
    """tag: one exact tag. shared: spaces other people shared with me or my groups. group: kind | owner keeps the rows of one group together
    (sort orders inside it). sort: new | old | top | name."""
    if kind and kind not in KINDS:
        raise HTTPException(400, "kind must be one of: " + ", ".join(KINDS))
    if group not in ("", "kind", "owner"):
        raise HTTPException(400, "group must be kind or owner")
    mine_uid, shared_uid = _scope_args(u, mine, shared)
    if since is not None:                             # changes since a moment, oldest first: what a client mirror asks for
        repos = R(request)
        v, lim = viewer_of(repos, u), max(1, min(int(limit), 100))
        key = "sp:chg:%s:%s:%s:%s:%s:%d:%d" % (_cache_scope(v), round(float(since), 3), kind, owner, int(bool(mine)), lim, max(0, int(offset)))
        return C(request).get_or_set(key, CHANGES_TTL, lambda: _lists(request, u, kind=kind, owner=owner, mine_uid=u["id"] if mine else None,
                                                                      limit=lim, offset=max(0, int(offset)), since=float(since)))
    return _lists(request, u, q=q[:200], kind=kind, sort=sort if sort in SORTS else "top", owner=owner, mine_uid=mine_uid, shared_uid=shared_uid,
                  tag=tag[:64], group=group, limit=max(1, min(int(limit), 100)), offset=max(0, int(offset)))


@r.get("/spaces/facets")
def space_facets(request: Request, q: str = "", kind: str = "", owner: str = "", tag: str = "", mine: bool = False, shared: bool = False,
                 u=Depends(current_user)):
    """Counts for the Spaces filter rail: type, tags, authors and scope (all / mine / shared with me), each counted under the other filters."""
    if kind and kind not in KINDS:
        raise HTTPException(400, "kind must be one of: " + ", ".join(KINDS))
    mine_uid, shared_uid = _scope_args(u, mine, shared)
    repos = R(request)
    v = viewer_of(repos, u)
    key = "sp:fac:%s:%s:%s:%s:%s:%d:%d" % (_cache_scope(v), q[:200].lower(), kind, owner, tag[:64], int(bool(mine_uid)), int(bool(shared_uid)))
    return C(request).get_or_set(key, FACET_TTL, lambda: repos.space_facets(v, q=q[:200], kind=kind, tag=tag[:64], owner=owner, mine_uid=mine_uid,
                                                                             shared_uid=shared_uid, uid=u["id"] if u else None))


@r.get("/spaces/overview")
def space_overview(request: Request, u=Depends(current_user)):
    """The Spaces home: carousel (most viewed), top, recently updated, counts per kind."""
    repos = R(request)
    v = viewer_of(repos, u)
    top, _ = repos.space_list(v, sort="top", limit=12)
    new, _ = repos.space_list(v, sort="new", limit=12)
    return {"carousel": [_ser(x) for x in top[:6]], "top": [_ser(x) for x in top], "recent": [_ser(x) for x in new], "kinds": repos.space_kinds(v)}


@r.get("/spaces/{ref}")
def space_get(ref: str, request: Request, u=Depends(current_user)):
    sp, level = _load(request, ref, u)
    return _ser(sp, level, full=True)


@r.patch("/spaces/{ref}")
def space_patch(ref: str, body: dict, request: Request, u=Depends(require_user)):
    """title, description, tags, kind (report | dashboard | html only), view / transition / autoplay / loop: edit access.
    visibility, public_access (view | comment: may signed-in visitors of a PUBLIC space comment?): the owner or an admin."""
    sp, level = _load(request, ref, u, "edit")
    f = {}
    if "title" in body:
        f["title"] = _clean(body["title"], 200)
    if "description" in body:
        f["description"] = _clean(body["description"], 1000)
    if "tags" in body:
        f["tags"] = _tags(body["tags"])
    if "kind" in body:
        k = body["kind"]
        if k not in spaceconv.HTML_KINDS or sp["kind"] not in spaceconv.HTML_KINDS:
            raise HTTPException(400, "only report, dashboard and html pages can swap kind")
        f["kind"] = k
    if body.get("visibility") not in (None, "") and body["visibility"] != sp["visibility"]:
        if level != "admin":
            raise HTTPException(403, "only the owner or an admin may change who can see this space")
        f["visibility"] = _vis(request, body["visibility"])
    if body.get("public_access") not in (None, "") and body["public_access"] != sp["public_access"]:
        if body["public_access"] not in PUBLIC_ACCESS:
            raise HTTPException(400, "public_access must be view or comment")
        if level != "admin":
            raise HTTPException(403, "only the owner or an admin may change what the public may do")
        f["public_access"] = body["public_access"]
    s = _settings(body)
    if s:
        f["settings"] = s
    R(request).space_update(sp["id"], **f)
    _changed(request)
    R(request).audit(u["username"], "space.edit", sp["ref"], ",".join(sorted(list(f)))[:200])
    sp = R(request).space_by_id(sp["id"])
    return _ser(sp, level, full=True)


@r.delete("/spaces/{ref}")
def space_delete(ref: str, request: Request, u=Depends(require_user)):
    sp, _ = _load(request, ref, u, "admin")
    R(request).space_delete(sp["id"])
    _changed(request)
    S(request).remove(sp["id"])
    R(request).audit(u["username"], "space.delete", sp["ref"], "")
    return {"ok": True}


# ---------------------------------------------------------------- sharing (like packages: view | comment | edit for a user or a group)
@r.get("/spaces/{ref}/access")
def space_access(ref: str, request: Request, u=Depends(require_user)):
    sp, _ = _load(request, ref, u, "admin")
    return {"visibility": sp["visibility"], "owner": sp["owner"], "access": R(request).space_access_list(sp["id"])}


@r.put("/spaces/{ref}/access")
def space_access_put(ref: str, body: dict, request: Request, u=Depends(require_user)):
    """{type: 'user'|'group', name, access: 'view'|'comment'|'edit'}, or {items: [{type, name}, ...], access} to share with many at once
    (all or none)."""
    sp, _ = _load(request, ref, u, "admin")
    access = body.get("access")
    if access not in LEVELS[:3]:
        raise HTTPException(400, "access must be view, comment or edit")
    items = body.get("items")
    if items is None:
        items = [{"type": body.get("type"), "name": body.get("name", "")}]
    if not isinstance(items, list) or not items or len(items) > 500:
        raise HTTPException(400, "items must be a list of 1..500 {type, name}")
    grants = []
    before = {(x["type"], x["name"]): x["access"] for x in R(request).space_access_list(sp["id"])}
    for it in items:
        ptype, pname = (it or {}).get("type"), str((it or {}).get("name", ""))
        if ptype not in ("user", "group"):
            raise HTTPException(400, "type must be user or group")
        t = R(request).user_by_name(pname) if ptype == "user" else R(request).group_by_name(pname)
        if not t:
            raise HTTPException(404, "%s %s not found" % (ptype, pname))
        grants.append((ptype, t["id"], pname))
    R(request).space_access_grant_many(sp["id"], [(a, b) for a, b, _ in grants], access)
    _changed(request)
    new_ids = []                                       # people whose access just appeared or changed hear about it
    for ptype, pid, pname in grants:
        if before.get((ptype, pname)) == access:
            continue
        new_ids += _group_ids(R(request), pid) if ptype == "group" else [(pid, None)]
    if new_ids:
        _tell(request, sp, u, "share", new_ids, open_to_all=True, body=access)
    R(request).audit(u["username"], "space.grant", sp["ref"], ", ".join("%s:%s=%s" % (a, n, access) for a, _, n in grants)[:500])
    return {"ok": True}


@r.delete("/spaces/{ref}/access/{ptype}/{pname}")
def space_access_delete(ref: str, ptype: str, pname: str, request: Request, u=Depends(require_user)):
    sp, _ = _load(request, ref, u, "admin")
    t = R(request).user_by_name(pname) if ptype == "user" else R(request).group_by_name(pname) if ptype == "group" else None
    if t:
        R(request).space_access_revoke(sp["id"], ptype, t["id"])
        _changed(request)
        R(request).audit(u["username"], "space.revoke", sp["ref"], "%s:%s" % (ptype, pname))
    return {"ok": True}


# ---------------------------------------------------------------- content
_seen = {}


@r.post("/spaces/{ref}/view")
def space_viewed(ref: str, request: Request, u=Depends(current_user)):
    """One call per page open. The same visitor counts once per half hour."""
    sp, _ = _load(request, ref, u)
    who = (u or {}).get("id") or (request.client.host if request.client else "?")
    k, now = (sp["id"], who), time.time()
    if len(_seen) > 5000:
        for kk in [x for x, t in _seen.items() if now - t > 1800]:
            _seen.pop(kk, None)
    if now - _seen.get(k, 0) > 1800:
        _seen[k] = now
        R(request).space_view(sp["id"])
    return {"ok": True}


def _rev(request, sp, rev):
    """-> (folder, kind, filename, rev) of revision `rev` of a space (None = the current one). 404 when that revision or its files are gone."""
    st = S(request)
    if rev is None or int(rev) == sp["rev"]:
        return st.dir(sp["id"]), sp["kind"], sp["filename"], sp["rev"]
    v = R(request).space_version(sp["id"], rev)
    if not v or not st.has_rev(sp["id"], int(rev), sp["rev"]):
        raise HTTPException(404, "revision %s of this space is not kept" % rev)
    return st.vdir(sp["id"], rev), v["kind"], v["filename"], int(rev)


@r.get("/spaces/{ref}/data")
def space_data(ref: str, request: Request, rev: Optional[int] = None, u=Depends(current_user)):
    """What the page needs to draw a slides / doc / sheet space (JSON). `rev` = an older revision from the history."""
    sp, _ = _load(request, ref, u)
    folder, kind, _, rv = _rev(request, sp, rev)
    etag = '"%d-%d"' % (sp["id"], rv)
    hd = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=hd)
    st = S(request)
    name = {"slides": "deck.json", "sheet": "sheet.json", "doc": "doc.html"}.get(kind)
    raw = st.read(sp["id"], name, folder) if name else None
    if raw is None:
        raise HTTPException(404, "this space has no page data (kind %s)" % kind)
    body = raw if name.endswith(".json") else json.dumps({"html": raw.decode("utf-8", "replace")}).encode()
    return Response(body, media_type="application/json", headers=hd)


@r.get("/spaces/{ref}/viewurl")
def space_viewurl(ref: str, request: Request, rev: Optional[int] = None, u=Depends(current_user)):
    """A short-lived link for the sandboxed frame (html kinds and PDF). The frame cannot send the sign-in token, so the link carries a signature
    for this space and revision only (an older revision: a pinned link, `exp.sig.rev`)."""
    sp, _ = _load(request, ref, u)
    folder, kind, _, rv = _rev(request, sp, rev)
    if kind not in ("pdf",) + spaceconv.HTML_KINDS:
        raise HTTPException(400, "this kind is shown by the page itself")
    path = "file/" + urllib.parse.quote(S(request).original(sp["id"], folder)[0] or "file.pdf") if kind == "pdf" else "index.html"
    tok = S(request).sign(sp["id"], rv, pin=rv != sp["rev"])
    return {"url": basepath.url("/api/v1/spaces/%s/v/%s/%s" % (urllib.parse.quote(sp["ref"], safe="@"), tok, path)), "expires": LINK_TTL, "rev": rv}


@r.get("/spaces/{ref}/v/{tok}/{path:path}")
def space_frame(ref: str, tok: str, path: str, request: Request):
    sp = _find(request, ref)
    st = S(request)
    pin = st.pinned(tok)
    rv = sp["rev"] if pin is None else pin
    if not st.check(sp["id"], rv, tok):
        raise HTTPException(403, "this link expired; open the space again")
    folder, kind = st.dir(sp["id"]), sp["kind"]
    if rv != sp["rev"]:
        v = R(request).space_version(sp["id"], rv)
        if not v or not os.path.isdir(st.vdir(sp["id"], rv)):
            raise HTTPException(404, "this revision is no longer kept")
        folder, kind = st.vdir(sp["id"], rv), v["kind"]
    if kind == "pdf":
        n, full = st.original(sp["id"], folder)
        if not full:
            raise HTTPException(404, "file missing")
        return FileResponse(full, media_type="application/pdf", headers={"Content-Security-Policy": VIEW_FILE, "X-Frame-Options": "SAMEORIGIN",
                                                                         "Content-Disposition": "inline", "Cache-Control": "private, max-age=300"})
    if kind not in spaceconv.HTML_KINDS:
        raise HTTPException(404, "not found")
    root = os.path.realpath(os.path.join(folder, "site"))
    full = os.path.realpath(os.path.join(root, path or "index.html"))
    if os.path.isdir(full):
        full = os.path.join(full, "index.html")
    if not (full == root or full.startswith(root + os.sep)) or not os.path.isfile(full):
        raise HTTPException(404, "not found")
    mt = mimetypes.guess_type(full)[0] or "application/octet-stream"
    hd = {"Content-Security-Policy": VIEW_SANDBOX, "X-Frame-Options": "SAMEORIGIN", "Cache-Control": "private, max-age=300"}
    if mt.startswith("text/html") or mt in ("image/svg+xml", "application/xhtml+xml"):
        mt = "text/html; charset=utf-8" if mt.startswith("text/html") else mt
    return FileResponse(full, media_type=mt, headers=hd)


@r.get("/spaces/{ref}/file")
def space_file(ref: str, request: Request, rev: Optional[int] = None, u=Depends(current_user)):
    """The file as it was published (attachment); `rev` = an older revision."""
    sp, _ = _load(request, ref, u)
    folder, _, fname, rv = _rev(request, sp, rev)
    n, full = S(request).original(sp["id"], folder)
    if not full:
        raise HTTPException(404, "file missing")
    return FileResponse(full, filename=fname or n, headers={"Cache-Control": "private, no-cache", "X-Space-Rev": str(rv)})


@r.get("/spaces/{ref}/open")
def space_open(ref: str, request: Request):
    """A plain link anyone can paste: public spaces only. Opens the page; html and PDF spaces go straight to the content."""
    sp = _find(request, ref)
    if sp["visibility"] != "public":
        raise HTTPException(404, "space not found")
    if sp["kind"] in ("pdf",) + spaceconv.HTML_KINDS:
        path = "file/x.pdf" if sp["kind"] == "pdf" else "index.html"
        tok = S(request).sign(sp["id"], sp["rev"])
        return RedirectResponse(basepath.url("/api/v1/spaces/%s/v/%s/%s" % (urllib.parse.quote(sp["ref"], safe="@"), tok, path)), status_code=302)
    return RedirectResponse(basepath.url("/") + "#/s/" + urllib.parse.quote(sp["ref"], safe="@"), status_code=302)



# ---------------------------------------------------------------- comments (view: read; comment: write, reply, resolve; author / space admin: delete)
MAX_DEPTH = 6                                         # a reply deeper than this joins its parent's thread instead (keeps nesting readable)
ANCHOR_KEY = re.compile(r"^[a-z_]{1,16}$")


def _text(v):
    """Comment body: multi-line text, cleaned (lone surrogates, NUL, control / bidi characters), 1..COMMENT_MAX characters."""
    if not isinstance(v, str):
        raise HTTPException(400, "body must be text")
    t = _CTRL.sub("", textsafe.clean(v).replace("\r\n", "\n").replace("\r", "\n")).strip()
    if not t:
        raise HTTPException(400, "write something first")
    if len(t) > COMMENT_MAX:
        raise HTTPException(400, "a comment is at most %d characters" % COMMENT_MAX)
    return t


def _anchor(v):
    """Where a comment points: a small object such as {"slide": 3}, {"page": 2} or {"sheet": "Q3", "cell": "B4"}. Stored as JSON text."""
    if v in (None, "", {}):
        return ""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            raise HTTPException(400, "anchor must be an object, e.g. {\"slide\": 3}")
    if not isinstance(v, dict) or len(v) > 6:
        raise HTTPException(400, "anchor must be an object with at most 6 keys, e.g. {\"slide\": 3}")
    out = {}
    for k, x in v.items():
        if not isinstance(k, str) or not ANCHOR_KEY.match(k):
            raise HTTPException(400, "anchor keys: lowercase letters and _ (max 16)")
        if isinstance(x, bool) or not isinstance(x, (int, str)) or (isinstance(x, int) and not 0 <= x <= 10 ** 9):
            raise HTTPException(400, "anchor values: a whole number or a short text")
        out[k] = x if isinstance(x, int) else _clean(textsafe.clean(x), 64)
    return json.dumps(out, separators=(",", ":"), sort_keys=True)


# ---------------------------------------------------------------- mentions + notifications
MENTION = re.compile(r"(?<![\w@.+/-])@([A-Za-z0-9][A-Za-z0-9._-]{0,63})")      # `bob@corp.com` is not a mention: a word character sits before the @
MENTION_MAX = 20                                      # names looked at per comment
NOTIFY_MAX = 300                                      # people told per comment, however many groups it names
SNIPPET = 160


CHIP = re.compile(r"\[(date|space):([^\]\n]{1,160})\]")     # chips the comment box writes: [date:2026-11-05], [space:author@name|Title]


def _plain(t):
    """A comment with its chips as words (the date, the space's title or address), for alerts."""
    return CHIP.sub(lambda m: (m.group(2).split("|", 1)[1].strip() if m.group(1) == "space" and "|" in m.group(2) else m.group(2).split("|", 1)[0]) or m.group(2), t or "")


def _snippet(t):
    t = " ".join(_plain(t).split())
    return t if len(t) <= SNIPPET else t[:SNIPPET - 1].rstrip() + "…"


def _mention_names(text):
    out = []
    for m in MENTION.finditer(text or ""):
        n = m.group(1).rstrip("._-")
        if n and n not in out:
            out.append(n)
    return out[:MENTION_MAX]


def _resolve(request, text):
    """Names in `text` that are real, active accounts or groups: [{type, name, display_name, who: [(id, role)]}]. An account wins over a group of
    the same name. Unknown names stay plain text."""
    repos, found, seen = R(request), [], set()
    for n in _mention_names(text):
        usr = repos.mention_user(n)
        if usr:
            k, ent = ("user", usr["username"]), {"type": "user", "name": usr["username"], "display_name": usr["display_name"] or "", "who": [(usr["id"], usr["role"])]}
        else:
            g = repos.group_by_name(n.lower())
            if not g:
                continue
            ms = repos.group_member_users(g["id"])
            k, ent = ("group", g["name"]), {"type": "group", "name": g["name"], "display_name": "", "members": len(ms), "who": [(m["id"], m["role"]) for m in ms]}
        if k not in seen:
            seen.add(k)
            found.append(ent)
    return found


def _stored(found):
    """What is kept on the comment: type + name (+ display name), no ids."""
    return json.dumps([{k: e[k] for k in ("type", "name", "display_name") if e.get(k)} for e in found], separators=(",", ":")) if found else ""


def _can_see(request, sp, uid, role):
    repos = R(request)
    bypass = bool({"admin", "manage_all"} & set(repos.perms(role)))
    return bool(repos.space_level(sp, {"id": uid, "bypass": bypass}))


def _tell(request, sp, actor, kind, ids, open_to_all=False, **kw):
    """Notify `ids` ([(user id, role)]) once each: never the actor, and only people who can open the space (`open_to_all`: the caller just
    granted them access). -> the ids told. Never raises: a notification problem must not undo the comment / publish / share that caused it."""
    try:
        rows, told = [], set()
        for uid, role in ids:
            if uid in told or uid == actor["id"] or len(rows) >= NOTIFY_MAX:
                continue
            if not open_to_all and not _can_see(request, sp, uid, role):
                continue
            told.add(uid)
            rows.append(dict(kw, user_id=uid, kind=kind, actor_id=actor["id"], item_id=sp["id"], rev=kw.get("rev") or sp["rev"]))
        R(request).notify(rows)
        return told
    except Exception:
        log.exception("notification failed (%s on %s)", kind, sp.get("ref"))
        return set()


def _notify_comment(request, sp, actor, cid, rev, text, found, parent=None, skip=(), also=True):
    """After a comment is stored: mentions (people and groups; `skip` = (type, name) already told on an earlier edit), then, when `also`, the
    author of the comment replied to and the space owner. Each person gets ONE alert, the strongest reason first.
    -> report [{type, name, notified, no_access: [names]}] for the page."""
    repos, report, told = R(request), [], {actor["id"]}
    snip = _snippet(text)
    for e in found:
        if (e["type"], e["name"]) in skip:
            continue
        ok, miss = [], []
        for uid, role in e["who"]:
            if uid in told or len(told) > NOTIFY_MAX:
                continue
            (ok if _can_see(request, sp, uid, role) else miss).append(uid)
        told.update(ok)
        try:
            repos.notify([dict(user_id=uid, kind="mention", actor_id=actor["id"], item_id=sp["id"], comment_id=cid, rev=rev, body=snip,
                               via=e["name"] if e["type"] == "group" else "") for uid in ok])
        except Exception:
            log.exception("mention notification failed on %s", sp.get("ref"))
        report.append({"type": e["type"], "name": e["name"], "notified": len(ok), "no_access": [e["name"]] if e["type"] == "user" and miss else []})
    if also:
        for kind, uid in (("reply", (parent or {}).get("user_id")), ("comment", sp["owner_id"])):
            if not uid or uid in told:
                continue
            row = repos.user_row(uid)
            if row and row["status"] == "active" and _can_see(request, sp, uid, row["role"]):
                told.add(uid)
                try:
                    repos.notify([dict(user_id=uid, kind=kind, actor_id=actor["id"], item_id=sp["id"], comment_id=cid, rev=rev, body=snip)])
                except Exception:
                    log.exception("%s notification failed on %s", kind, sp.get("ref"))
    return report


def _notify_version(request, sp, actor, rev, note):
    """A new revision: the owner and everyone who commented are told (unless they published it themselves)."""
    repos = R(request)
    ids = []
    for uid in dict.fromkeys([sp["owner_id"]] + repos.space_commenter_ids(sp["id"])):
        row = repos.user_row(uid)
        if row and row["status"] == "active":
            ids.append((uid, row["role"]))
    _tell(request, sp, actor, "version", ids, body=_snippet(note) or ("Revision %d" % rev), rev=rev)


def _group_ids(repos, gid):
    return [(m["id"], m["role"]) for m in repos.group_member_users(gid)]



def _cser(c, uid, level):
    dead = bool(c["deleted"])
    try:
        anchor = json.loads(c["anchor"]) if c.get("anchor") else None
    except ValueError:
        anchor = None
    mine = uid is not None and c["user_id"] == uid
    may = rank(level) >= rank("comment")
    try:
        ment = [] if dead or not c.get("mentions") else [m for m in json.loads(c["mentions"]) if isinstance(m, dict)]
    except ValueError:
        ment = []
    who = None if dead or not c.get("username") else {"username": c["username"], "display_name": c.get("display_name") or "",
                                                       "avatar_url": _avatar_url(c["username"], c.get("avatar_v") or 0)}
    return {"id": c["id"], "parent_id": c["parent_id"], "body": "" if dead else c["body"], "anchor": anchor, "rev": c["rev"],
            "resolved": bool(c["resolved"]), "created": c["created"], "updated": c["updated"], "mentions": ment,
            "edited": not dead and (c["updated"] or 0) - (c["created"] or 0) > 1, "deleted": dead, "author": who,
            "can_edit": mine and may and not dead, "can_resolve": may and not dead, "can_delete": not dead and (mine or level == "admin"), "replies": []}


def _thread(rows, uid, level):
    """Flat rows (oldest first) -> top-level comments with nested `replies`. A deleted comment stays only while it has live replies."""
    by = {r["id"]: _cser(r, uid, level) for r in rows}
    roots = []
    for c in by.values():
        p = by.get(c["parent_id"]) if c["parent_id"] else None
        (p["replies"] if p else roots).append(c)

    def live(c):
        c["replies"] = [x for x in c["replies"] if live(x)]
        return not c["deleted"] or bool(c["replies"])
    return [c for c in roots if live(c)]


def _uid(u):
    return (u or {}).get("id") or None


@r.get("/spaces/{ref}/comments")
def space_comments(ref: str, request: Request, u=Depends(current_user)):
    sp, level = _load(request, ref, u)
    rows = R(request).space_comments(sp["id"])
    items = _thread(rows, _uid(u), level)
    live = [x for x in rows if not x["deleted"]]
    return {"items": items, "total": len(live), "open": sum(1 for x in live if not x["resolved"] and not x["parent_id"]), "level": level,
            "can_comment": rank(level) >= rank("comment"), "signed_in": bool(_uid(u)), "public_access": sp["public_access"]}


@r.post("/spaces/{ref}/comments")
def space_comment_add(ref: str, body: dict, request: Request, u=Depends(current_user)):
    """{body, parent_id?, anchor?, rev?}: comment access (a signed-in visitor of a public space only when its public_access is comment).
    `@name` points at an account or a group: they get an alert (if they can open the space), the reply carries `mention_report`. `rev` = the
    revision the comment is about (default: the current one)."""
    sp, level = _load(request, ref, u, "comment")
    repos = R(request)
    text, parent = _text(body.get("body")), body.get("parent_id")
    if parent not in (None, ""):
        try:
            parent = int(parent)
        except (TypeError, ValueError):
            raise HTTPException(400, "parent_id must be a comment id")
        p = repos.space_comment(sp["id"], parent)
        if not p or p["deleted"]:
            raise HTTPException(404, "the comment you reply to is gone")
        chain, depth = p, 1
        while chain["parent_id"] and depth < MAX_DEPTH + 1:
            chain, depth = repos.space_comment(sp["id"], chain["parent_id"]) or {"parent_id": None}, depth + 1
        if depth > MAX_DEPTH:
            parent = p["parent_id"] or parent
    else:
        parent = None
    rev = body.get("rev")
    rev = rev if isinstance(rev, int) and not isinstance(rev, bool) and 1 <= rev <= sp["rev"] else sp["rev"]
    found = _resolve(request, text)
    cid = repos.space_comment_add(sp["id"], u["id"], text, parent_id=parent, anchor=_anchor(body.get("anchor")), rev=rev, mentions=_stored(found))
    _changed(request)
    out = _cser(repos.space_comment(sp["id"], cid), u["id"], level)
    out["mention_report"] = _notify_comment(request, sp, u, cid, rev, text, found, parent=repos.space_comment(sp["id"], parent) if parent else None)
    return out


@r.patch("/spaces/{ref}/comments/{cid}")
def space_comment_patch(ref: str, cid: int, body: dict, request: Request, u=Depends(require_user)):
    """{body}: the author changes their text; {resolved: bool}: anyone who may comment."""
    sp, level = _load(request, ref, u)
    repos = R(request)
    c = repos.space_comment(sp["id"], cid)
    if not c or c["deleted"]:
        raise HTTPException(404, "comment not found")
    may = rank(level) >= rank("comment")
    text = res = found = None
    if "body" in body:
        if c["user_id"] != u["id"]:
            raise HTTPException(403, "only the author may change a comment")
        if not may:
            raise HTTPException(403, "you may view this space but not comment on it")
        text = _text(body["body"])
        found = _resolve(request, text)
    if "resolved" in body:
        if not may:
            raise HTTPException(403, "you may view this space but not comment on it")
        res = bool(body["resolved"])
    if text is None and res is None:
        raise HTTPException(400, "nothing to change: send body or resolved")
    repos.space_comment_edit(sp["id"], cid, body=text, resolved=res, mentions=_stored(found) if found is not None else None)
    _changed(request)
    out = _cser(repos.space_comment(sp["id"], cid), u["id"], level)
    if found is not None:                              # only NEW names are told: an edit does not ping the same people again
        try:
            before = {(m.get("type"), m.get("name")) for m in json.loads(c.get("mentions") or "[]")}
        except ValueError:
            before = set()
        out["mention_report"] = _notify_comment(request, sp, u, cid, c["rev"], text, found, skip=before, also=False) if found else []
    if res is True and c["user_id"] and c["user_id"] != u["id"]:
        row = repos.user_row(c["user_id"])
        if row and row["status"] == "active":
            _tell(request, sp, u, "resolved", [(row["id"], row["role"])], body=_snippet(c["body"]), comment_id=c["id"], rev=c["rev"])
    return out


@r.delete("/spaces/{ref}/comments/{cid}")
def space_comment_delete(ref: str, cid: int, request: Request, u=Depends(require_user)):
    """The author, or the owner / an admin of the space. Soft delete: replies keep their place under a 'deleted' marker."""
    sp, level = _load(request, ref, u)
    repos = R(request)
    c = repos.space_comment(sp["id"], cid)
    if not c or c["deleted"]:
        raise HTTPException(404, "comment not found")
    if c["user_id"] != u["id"] and level != "admin":
        raise HTTPException(403, "only the author or the owner of the space may delete a comment")
    repos.space_comment_delete(sp["id"], cid)
    _changed(request)
    if c["user_id"] != u["id"]:
        repos.audit(u["username"], "space.comment.delete", sp["ref"], "comment %d by %s" % (cid, c.get("username") or "?"))
    return {"ok": True}


@r.get("/spaces/{ref}/mentionable")
def space_mentionable(ref: str, request: Request, q: str = "", u=Depends(require_user)):
    """People and groups for the `@` picker of the comment box. No query: the owner, people already in the conversation and the groups the
    space is shared with. `access` says whether they can open this space (a mention of someone who cannot is not delivered)."""
    sp, _ = _load(request, ref, u, "comment")
    repos, q = R(request), (q or "").strip().lstrip("@")[:40]
    public = sp["visibility"] == "public"
    users, groups = [], []
    if q:
        us, gs = repos.mention_candidates(q)
    else:
        ids = list(dict.fromkeys([sp["owner_id"]] + repos.space_commenter_ids(sp["id"])))
        us = [x for x in (repos.mention_user((repos.user_row(i) or {}).get("username") or "") for i in ids) if x][:8]
        for a in repos.space_access_list(sp["id"]):
            if a["type"] == "user" and len(us) < 8 and not any(x["username"] == a["name"] for x in us):
                us += [x for x in [repos.mention_user(a["name"])] if x]
        gs = [dict(g) for g in (repos.group_by_name(a["name"]) for a in repos.space_access_list(sp["id"]) if a["type"] == "group") if g]
        for g in gs:
            g["members"] = len(repos.group_member_users(g["id"]))
    for x in us:
        if x["id"] == u["id"]:
            continue
        users.append({"username": x["username"], "display_name": x["display_name"] or "", "avatar_url": _avatar_url(x["username"], x.get("avatar_v") or 0),
                      "access": _can_see(request, sp, x["id"], x["role"])})
    for g in gs:
        if any(x["username"] == g["name"] for x in users):         # an account of that name wins the @name
            continue
        groups.append({"name": g["name"], "members": g.get("members") or 0, "access": public or repos.space_group_granted(sp["id"], g["id"])})
    users.sort(key=lambda x: not x["access"])
    groups.sort(key=lambda x: not x["access"])
    return {"users": users[:8], "groups": groups[:6]}


@r.get("/spaces/{ref}/mention-card/{kind}/{name}")
def space_mention_card(ref: str, kind: str, name: str, request: Request, u=Depends(require_user)):
    """The hover card of an `@mention` in a comment: who the person (or group) is and whether they can open this space. Needs only view access to
    the space and a signed-in viewer; an unknown or inactive name is a 404 (the chip then stays plain)."""
    sp, _ = _load(request, ref, u)
    repos = R(request)
    if kind == "group":
        g = repos.group_by_name(name.lower()) or repos.group_by_name(name)
        if not g:
            raise HTTPException(404, "group not found")
        ms = repos.group_members(g["id"])
        return {"type": "group", "name": g["name"], "description": g.get("description") or "", "members": len(ms),
                "people": [{"username": m["username"], "display_name": m["display_name"] or ""} for m in ms[:5]],
                "access": sp["visibility"] == "public" or repos.space_group_granted(sp["id"], g["id"])}
    usr = repos.mention_user(name)
    if not usr:
        raise HTTPException(404, "person not found")
    full = repos.user_by_name(usr["username"]) or {}
    return {"type": "user", "name": usr["username"], "display_name": usr["display_name"] or "", "title": full.get("title") or "", "role": usr["role"],
            "avatar_url": _avatar_url(usr["username"], usr.get("avatar_v") or 0), "since": full.get("created") or None,
            "groups": [g["name"] for g in repos.user_groups(usr["id"])][:6], "access": _can_see(request, sp, usr["id"], usr["role"])}


# ---------------------------------------------------------------- version history (view: list / open old ones; edit: restore; admin: delete one)
def _vser(v, sp, st, counts=None):
    cur = v["rev"] == sp["rev"]
    return {"rev": v["rev"], "kind": v["kind"], "filename": v["filename"] or "", "size": v["size"] or 0, "sha256": v["sha256"] or "", "note": v["note"] or "",
            "created": v["created"], "author": v.get("author") or "", "author_name": v.get("author_name") or "",
            "author_avatar": _avatar_url(v["author"], v.get("author_avatar_v") or 0) if v.get("author") else None,
            "current": cur, "available": cur or st.has_rev(sp["id"], v["rev"], sp["rev"]), "comments": (counts or {}).get(v["rev"], 0)}


@r.get("/spaces/{ref}/versions")
def space_versions(ref: str, request: Request, u=Depends(current_user)):
    """Every kept revision, newest first. `available` = its files are still there (a space restored from a database backup has rows only)."""
    sp, level = _load(request, ref, u)
    st = S(request)
    counts = R(request).space_comment_revs(sp["id"])
    return {"items": [_vser(v, sp, st, counts) for v in R(request).space_versions(sp["id"])], "current": sp["rev"],
            "keep": request.app.state.sync.num("space_versions_keep"), "level": level,
            "can_restore": level in ("edit", "admin"), "can_delete": level == "admin"}


@r.post("/spaces/{ref}/versions/{rev}/restore")
def space_restore(ref: str, rev: int, request: Request, body: Optional[dict] = Body(None), u=Depends(require_perm("publish"))):
    """Publish revision REV again as a NEW revision (history is never rewritten). {note?}. Needs edit access."""
    sp, level = _load(request, ref, u, "edit")
    repos, st = R(request), S(request)
    if rev == sp["rev"]:
        raise HTTPException(400, "revision %d is already the current one" % rev)
    v = repos.space_version(sp["id"], rev)
    if not v or not st.has_rev(sp["id"], rev, sp["rev"]):
        raise HTTPException(404, "revision %d of this space is not kept" % rev)
    work = st.scratch()
    built = os.path.join(work, "built")
    try:
        shutil.copytree(st.vdir(sp["id"], rev), built, symlinks=True)
        note = _note((body or {}).get("note")) or "restored revision %d" % rev
        sid, _, new = repos.space_save(sp["owner_id"], sp["slug"], v["kind"], v["filename"] or sp["filename"], v["size"] or 0, v["sha256"] or "",
                                       author_id=u["id"], note=note)
        _store_rev(request, sid, built, new, sp["rev"])
    finally:
        shutil.rmtree(work, ignore_errors=True)
    repos.audit(u["username"], "space.restore", sp["ref"], "rev %d -> %d" % (rev, new))
    now = repos.space_by_id(sid)
    _notify_version(request, now, u, new, note)
    return {"space": _ser(now, level, full=True), "created": False, "restored": rev, "rev": new, "path": "/s/" + now["ref"]}


@r.delete("/spaces/{ref}/versions/{rev}")
def space_version_delete(ref: str, rev: int, request: Request, u=Depends(require_user)):
    """Forget one OLD revision and its files (owner / admin). The current one cannot be deleted."""
    sp, _ = _load(request, ref, u, "admin")
    repos = R(request)
    if rev == sp["rev"]:
        raise HTTPException(400, "the current revision cannot be deleted: restore another one first, or delete the space")
    if not repos.space_version(sp["id"], rev):
        raise HTTPException(404, "revision %d is not kept" % rev)
    repos.space_version_delete(sp["id"], rev)
    S(request).drop_rev(sp["id"], rev)
    _changed(request)
    repos.audit(u["username"], "space.version.delete", sp["ref"], "rev %d" % rev)
    return {"ok": True}


# ---------------------------------------------------------------- notifications (alerts on the account: mentions, replies, comments, shares, new revisions)
NKINDS = ("mention", "reply", "comment", "resolved", "share", "version")


def _nser(n):
    ref = "%s@%s" % (n["owner"], n["slug"]) if n.get("slug") and n.get("owner") else ""
    actor = {"username": n["actor"], "display_name": n.get("actor_name") or "", "avatar_url": _avatar_url(n["actor"], n.get("actor_avatar_v") or 0)} if n.get("actor") else None
    return {"id": n["id"], "kind": n["kind"], "ref": ref, "title": (n.get("title") or n.get("slug") or "") if ref else "", "path": "/s/" + ref if ref else "",
            "rev": n["rev"] or 0, "current_rev": n.get("current_rev") or 0, "comment_id": n["comment_id"] or 0, "body": n["body"] or "", "via": n["via"] or "",
            "created": n["created"], "seen": bool(n["seen"]), "actor": actor}


@r.get("/notifications")
def notifications(request: Request, unread: bool = False, kind: str = "", limit: int = 30, before: Optional[int] = None, u=Depends(require_user)):
    """The signed-in person's alerts, newest first. `kind` = mention | reply | comment | resolved | share | version (comma list ok); `before` = an id (page back)."""
    repos = R(request)
    kinds = [k for k in (kind or "").split(",") if k in NKINDS]
    v = viewer_of(repos, u)
    rows, more = repos.notifications(v, unread=unread, kinds=kinds, limit=max(1, min(int(limit), 100)), before=before)
    return {"items": [_nser(n) for n in rows], "more": more, "unread": repos.notifications_unread(v)}


@r.get("/notifications/count")
def notifications_count(request: Request, u=Depends(require_user)):
    """Cheap: the number the bell shows."""
    repos = R(request)
    return {"unread": repos.notifications_unread(viewer_of(repos, u))}


@r.post("/notifications/read")
def notifications_read(request: Request, body: Optional[dict] = Body(None), u=Depends(require_user)):
    """{ids: [..]} marks those, {all: true} marks every one; {seen: false} marks them unread again."""
    body, repos = body or {}, R(request)
    ids = body.get("ids")
    if ids is None and not body.get("all"):
        raise HTTPException(400, "send ids, or all: true")
    if ids is not None:
        if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
            raise HTTPException(400, "ids must be a list of notification ids")
    repos.notifications_mark(u["id"], None if ids is None else ids, seen=body.get("seen") is not False)
    return {"ok": True, "unread": repos.notifications_unread(viewer_of(repos, u))}


@r.delete("/notifications/{nid}")
def notification_delete(nid: int, request: Request, u=Depends(require_user)):
    repos = R(request)
    repos.notification_delete(u["id"], nid)
    return {"ok": True, "unread": repos.notifications_unread(viewer_of(repos, u))}
