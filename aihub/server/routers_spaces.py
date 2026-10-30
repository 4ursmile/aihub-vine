"""Spaces: documents, slides, reports, PDFs, sheets, dashboards and HTML pages published with `aihub space publish`, addressed `owner@slug`.
Visibility and sharing work like packages (public | private; view | edit for users and groups). Files live in `<data>/spaces/<id>/`.
Untrusted HTML never runs on the hub's origin: it is served from a signed URL with a sandbox CSP and shown in a sandboxed iframe."""
import hashlib
import hmac
import json
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

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from ..core import archive
from . import basepath, spaceconv
from .deps import current_user, require_perm, require_user, viewer_of
from .routers import C, _avatar_url, _cache_scope, _clean
from .spaceconv import SLUG, SpaceError, TRANSITIONS
from .spaces_repo import KINDS

r = APIRouter(prefix="/api/v1")
VIEWS = ("canvas", "fit", "full")
LINK_TTL = 6 * 3600
VIEW_SANDBOX = ("sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox allow-forms allow-downloads allow-modals; default-src 'none'; "
                "script-src 'unsafe-inline' 'self' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://unpkg.com; "
                "style-src 'unsafe-inline' 'self' https:; img-src 'self' data: blob: https:; font-src 'self' data: https:; media-src 'self' data: blob: https:; "
                "connect-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'self'; frame-ancestors 'self'")
VIEW_FILE = "frame-ancestors 'self'; script-src 'none'"


def R(request): return request.app.state.repos


class SpaceStore:
    """Files of every space plus the key that signs view links. Replacing a space swaps a whole folder, so a reader never sees half of it."""
    def __init__(self, data_dir):
        self.base = os.path.join(data_dir, "spaces")
        os.makedirs(self.base, exist_ok=True)
        self._key_file = os.path.join(data_dir, ".spaces.key")
        self._key = None
        self._lock = threading.Lock()
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

    def put(self, sid, built):
        dst, old = self.dir(sid), self.dir(sid) + ".old"
        shutil.rmtree(old, ignore_errors=True)
        if os.path.isdir(dst):
            os.rename(dst, old)
        os.rename(built, dst)
        shutil.rmtree(old, ignore_errors=True)

    def remove(self, sid):
        shutil.rmtree(self.dir(sid), ignore_errors=True)

    def sign(self, sid, rev, ttl=LINK_TTL):
        exp = int(time.time()) + ttl
        return "%d.%s" % (exp, hmac.new(self.key(), ("%d|%d|%d" % (sid, rev, exp)).encode(), hashlib.sha256).hexdigest()[:40])

    def check(self, sid, rev, tok):
        try:
            exp, sig = tok.split(".", 1)
            good = hmac.new(self.key(), ("%d|%d|%d" % (sid, rev, int(exp))).encode(), hashlib.sha256).hexdigest()[:40]
            return int(exp) >= time.time() and hmac.compare_digest(sig, good)
        except (ValueError, TypeError):
            return False

    def read(self, sid, name):
        p = os.path.join(self.dir(sid), name)
        try:
            with open(p, "rb") as f:
                return f.read()
        except OSError:
            return None

    def original(self, sid):
        d = os.path.join(self.dir(sid), "orig")
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
    """-> (space, level). 404 when the viewer may not even see it, 403 when they lack `need` (view < edit < admin)."""
    sp = _find(request, ref)
    level = R(request).space_level(sp, viewer_of(R(request), u))
    if not level:
        raise HTTPException(404, "space not found")
    if ("view", "edit", "admin").index(level) < ("view", "edit", "admin").index(need):
        raise HTTPException(403, "you may %s this space but not change it" % level if need == "edit" else "only the owner or an admin may do that")
    return sp, level


def _ser(sp, level=None, full=False):
    d = {"ref": sp["ref"], "owner": sp["owner"], "owner_name": sp.get("owner_name") or "", "owner_avatar": _avatar_url(sp["owner"], sp.get("owner_avatar_v") or 0),
         "slug": sp["slug"], "title": sp["title"] or sp["slug"], "description": sp["description"], "kind": sp["kind"], "visibility": sp["visibility"],
         "tags": sp["tags"], "views": sp["views"], "size": sp["size"], "rev": sp["rev"], "created": sp["created"], "updated": sp["updated"],
         "settings": sp["settings"], "filename": sp["filename"]}
    if level is not None:
        d["level"] = level
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
        sid, created = repos.space_save(owner["id"], slug, res["kind"], os.path.basename(filename)[:200], size, sha, title=_clean(title, 200) if title is not None else None,
                                        description=_clean(q.get("description"), 1000) if q.get("description") is not None else None,
                                        tags=_tags(q.get("tags")), visibility=vis, settings=settings)
        store.put(sid, built)
        _changed(request)
    except HTTPException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    shutil.rmtree(work, ignore_errors=True)
    repos.audit(u["username"], "space.publish" if created else "space.update", "%s@%s" % (owner["username"], slug), res["kind"])
    sp = repos.space_by_id(sid)
    return {"space": _ser(sp, repos.space_level(sp, viewer_of(repos, u)), full=True), "created": created, "path": "/s/" + sp["ref"]}


# ---------------------------------------------------------------- publish
@r.post("/spaces")
async def space_create(request: Request, name: str = "", u=Depends(require_perm("publish"))):
    """Body = the file (or a .zip / .tar.gz site). Query: name (the slug; default from the file name), filename, kind, title, description,
    tags, visibility, view, transition, autoplay, loop. Publishing the same name again replaces the content (and keeps what you did not send)."""
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


@r.get("/spaces")
def space_list(request: Request, q: str = "", kind: str = "", sort: str = "top", owner: str = "", mine: bool = False, limit: int = 24, offset: int = 0,
               since: Optional[float] = None, u=Depends(current_user)):
    if kind and kind not in KINDS:
        raise HTTPException(400, "kind must be one of: " + ", ".join(KINDS))
    if mine and not u:
        raise HTTPException(401, "authentication required")
    if since is not None:                             # changes since a moment, oldest first: what a client mirror asks for
        repos = R(request)
        v, lim = viewer_of(repos, u), max(1, min(int(limit), 100))
        key = "sp:chg:%s:%s:%s:%s:%s:%d:%d" % (_cache_scope(v), round(float(since), 3), kind, owner, int(bool(mine)), lim, max(0, int(offset)))
        return C(request).get_or_set(key, CHANGES_TTL, lambda: _lists(request, u, kind=kind, owner=owner, mine_uid=u["id"] if mine else None,
                                                                      limit=lim, offset=max(0, int(offset)), since=float(since)))
    return _lists(request, u, q=q[:200], kind=kind, sort=sort, owner=owner, mine_uid=u["id"] if mine else None,
                  limit=max(1, min(int(limit), 100)), offset=max(0, int(offset)))


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
    visibility: the owner or an admin."""
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


# ---------------------------------------------------------------- sharing (like packages: view | edit for a user or a group)
@r.get("/spaces/{ref}/access")
def space_access(ref: str, request: Request, u=Depends(require_user)):
    sp, _ = _load(request, ref, u, "admin")
    return {"visibility": sp["visibility"], "owner": sp["owner"], "access": R(request).space_access_list(sp["id"])}


@r.put("/spaces/{ref}/access")
def space_access_put(ref: str, body: dict, request: Request, u=Depends(require_user)):
    """{type: 'user'|'group', name, access: 'view'|'edit'}, or {items: [{type, name}, ...], access} to share with many at once (all or none)."""
    sp, _ = _load(request, ref, u, "admin")
    access = body.get("access")
    if access not in ("view", "edit"):
        raise HTTPException(400, "access must be view or edit")
    items = body.get("items")
    if items is None:
        items = [{"type": body.get("type"), "name": body.get("name", "")}]
    if not isinstance(items, list) or not items or len(items) > 500:
        raise HTTPException(400, "items must be a list of 1..500 {type, name}")
    grants = []
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


@r.get("/spaces/{ref}/data")
def space_data(ref: str, request: Request, u=Depends(current_user)):
    """What the page needs to draw a slides / doc / sheet space (JSON)."""
    sp, _ = _load(request, ref, u)
    etag = '"%d-%d"' % (sp["id"], sp["rev"])
    hd = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=hd)
    st = S(request)
    name = {"slides": "deck.json", "sheet": "sheet.json", "doc": "doc.html"}.get(sp["kind"])
    raw = st.read(sp["id"], name) if name else None
    if raw is None:
        raise HTTPException(404, "this space has no page data (kind %s)" % sp["kind"])
    body = raw if name.endswith(".json") else json.dumps({"html": raw.decode("utf-8", "replace")}).encode()
    return Response(body, media_type="application/json", headers=hd)


@r.get("/spaces/{ref}/viewurl")
def space_viewurl(ref: str, request: Request, u=Depends(current_user)):
    """A short-lived link for the sandboxed frame (html kinds and PDF). The frame cannot send the sign-in token, so the link carries a signature
    for this space and revision only."""
    sp, _ = _load(request, ref, u)
    if sp["kind"] not in ("pdf",) + spaceconv.HTML_KINDS:
        raise HTTPException(400, "this kind is shown by the page itself")
    path = "file/" + urllib.parse.quote(S(request).original(sp["id"])[0] or "file.pdf") if sp["kind"] == "pdf" else "index.html"
    tok = S(request).sign(sp["id"], sp["rev"])
    return {"url": basepath.url("/api/v1/spaces/%s/v/%s/%s" % (urllib.parse.quote(sp["ref"], safe="@"), tok, path)), "expires": LINK_TTL}


@r.get("/spaces/{ref}/v/{tok}/{path:path}")
def space_frame(ref: str, tok: str, path: str, request: Request):
    sp = _find(request, ref)
    st = S(request)
    if not st.check(sp["id"], sp["rev"], tok):
        raise HTTPException(403, "this link expired; open the space again")
    if sp["kind"] == "pdf":
        n, full = st.original(sp["id"])
        if not full:
            raise HTTPException(404, "file missing")
        return FileResponse(full, media_type="application/pdf", headers={"Content-Security-Policy": VIEW_FILE, "X-Frame-Options": "SAMEORIGIN",
                                                                         "Content-Disposition": "inline", "Cache-Control": "private, max-age=300"})
    if sp["kind"] not in spaceconv.HTML_KINDS:
        raise HTTPException(404, "not found")
    root = os.path.realpath(os.path.join(st.dir(sp["id"]), "site"))
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
def space_file(ref: str, request: Request, u=Depends(current_user)):
    """The file as it was published (attachment)."""
    sp, _ = _load(request, ref, u)
    n, full = S(request).original(sp["id"])
    if not full:
        raise HTTPException(404, "file missing")
    return FileResponse(full, filename=sp["filename"] or n, headers={"Cache-Control": "private, no-cache", "X-Space-Rev": str(sp["rev"])})


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
