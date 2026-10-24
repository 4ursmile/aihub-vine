import hashlib
import json
import logging
import zlib
import os
from typing import Optional
import re
import tempfile
import time
import uuid

import csv
import io
import secrets

from fastapi import Query, APIRouter, Depends, HTTPException, Request
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

from ..core.release import VERSION
from ..cli import gitx
from ..core import archive, manifest as M, naming, pkgindex, redact, version as V
from .sync import LIMITS, parse_schedule
from .transfer import TransferError
from . import basepath, builtin, direct as direct_mod, image, markdown, nethost, safehtml, sheet
from .categories import category_of
from .db import ALL_PERMS, ANON_LOCKED
from .deps import can_develop, can_manage, current_user, require_any, require_perm, require_user, user_or_anon, viewer_of
from .security import hash_password, hash_token, new_token, verify_password

r = APIRouter(prefix="/api/v1")
log_direct = logging.getLogger("aihub")
TTL = 20


_fails = {}  # (ip, key) -> (count, window_start); per-process throttle for login / password checks


def R(request): return request.app.state.repos


# admin setting key -> environment variable that overrides it (environment / .env wins; the UI shows "set by environment")
CLI_GIT_ENV = {"cli_git_url": "AIHUB_CLI_GIT_URL", "cli_git_branch": "AIHUB_CLI_GIT_BRANCH", "cli_git_subdir": "AIHUB_CLI_GIT_SUBDIR"}
SYNC_ENV = dict(CLI_GIT_ENV, telemetry_url="AIHUB_TELEMETRY_URL", telemetry_branch="AIHUB_TELEMETRY_BRANCH", telemetry_inbox_branch="AIHUB_TELEMETRY_INBOX_BRANCH", index_url="AIHUB_INDEX_URL", index_branch="AIHUB_INDEX_BRANCH",
                index_path="AIHUB_INDEX_PATH")
SETTING_DEFAULTS = {"public_browse": "1", "public_install": "0", "default_visibility": "public", "allow_private": "1", "allow_source_download": "1",
                    "signup": None}


def setting(request, key):
    """Runtime setting (admin-editable). Falls back to SETTING_DEFAULTS."""
    v = R(request).setting(key)
    return v if v is not None else SETTING_DEFAULTS.get(key)


def _cli_default(k):
    from ..core import defaults
    return defaults.load().get("cli", {}).get(k, "")


def env_first(request, key, env_name, fallback=""):
    """Deployment value with the precedence used across the Get started page and the CLI config:
    server environment (or .env) > admin-saved setting `key` > `fallback` (e.g. defaults.json). Returns (value, from_env)."""
    e = request.app.state.settings.env_get(env_name)
    if e:
        return e.strip(), True
    return (R(request).setting(key, "") or fallback or "").strip(), False


def public_url(request):
    """Address handed to CLIs: the admin's `cli_endpoint` setting, else the server's configured public URL."""
    return (R(request).setting("cli_endpoint") or request.app.state.settings.public_url).rstrip("/")


def viewer(request, u):
    return viewer_of(R(request), u)


def need_login_unless(request, u, key):
    """Anonymous access is allowed only when the admin switched `key` on."""
    if not u and setting(request, key) != "1":
        raise HTTPException(401, "sign in required")
def C(request): return request.app.state.cache


_BAD_CHARS = re.compile("[\x00-\x1f\x7f\u2028\u2029\u202a-\u202e\u2066-\u2069\u200b-\u200f]")  # controls, bidi overrides, zero-width


def _clean(v, n):
    """Single-line display text: no control / bidi-override characters, collapsed whitespace, length-capped."""
    return re.sub(r"\s+", " ", _BAD_CHARS.sub(" ", str(v or ""))).strip()[:n]


def _avatar_url(username, v):
    return basepath.url("/api/v1/users/%s/avatar?v=%d" % (username, v)) if v else None


def _public(u):
    return {"username": u["username"], "display_name": u.get("display_name") or "", "title": u.get("title") or "",
            "avatar_url": _avatar_url(u["username"], u.get("avatar_v") or 0)}


# ---------------- auth
class Cred(BaseModel):
    username: str
    password: str


class PwChange(BaseModel):
    old: str
    new: str


class TokenReq(BaseModel):
    name: str = ""


@r.get("/auth/providers")
def providers(): return {"providers": ["password"]}


@r.post("/auth/register")
def register(c: Cred, request: Request):
    repos = R(request)
    if not re.match(r"^[A-Za-z0-9_.-]{2,40}$", c.username) or len(c.password) < 6:
        raise HTTPException(400, "username 2-40 chars; password >= 6 chars")
    first = repos.user_count() == 0
    mode = repos.setting("signup", "open" if request.app.state.settings.open_registration else "closed")
    if not first and mode == "closed":
        raise HTTPException(403, "registration closed")
    if repos.user_by_name(c.username):
        raise HTTPException(409, "username taken")
    status = "active" if (first or mode == "open") else "pending"
    repos.user_create(c.username, hash_password(c.password), "admin" if first else "user", status)
    return {"username": c.username, "status": status}


@r.post("/auth/login")
def login(c: Cred, request: Request):
    repos = R(request)
    key = (request.client.host if request.client else "?", c.username.lower())
    n, first = _fails.get(key, (0, time.time()))
    if n >= 8 and time.time() - first < 300:
        raise HTTPException(429, "too many attempts; try again in a few minutes")
    if time.time() - first >= 300:
        n, first = 0, time.time()
    u = repos.user_by_name(c.username)
    if not u or not verify_password(c.password, u["password_hash"] or ""):
        _fails[key] = (n + 1, first)
        raise HTTPException(401, "bad credentials")
    _fails.pop(key, None)
    if u["status"] != "active":
        raise HTTPException(403, "account " + u["status"])
    t = new_token()
    repos.token_add(hash_token(t), u["id"], "session")
    return {"token": t, "username": u["username"], "role": u["role"]}


@r.post("/auth/logout")
def logout(request: Request, u=Depends(require_user)):
    h = request.headers["authorization"][7:].strip()
    R(request).token_delete(u["id"], h=hash_token(h))
    return {"ok": True}


@r.get("/auth/me")
def me(request: Request, u=Depends(require_user)):
    return dict(_public(u), role=u["role"], permissions=sorted(R(request).perms(u["role"])))


@r.patch("/auth/me")
def me_update(body: dict, request: Request, u=Depends(require_user)):
    dn = _clean(body["display_name"], 60) if "display_name" in body else None
    title = _clean(body["title"], 80) if "title" in body else None
    R(request).profile_set(u["username"], dn, title)
    return {"ok": True}


@r.post("/auth/password")
def password(p: PwChange, request: Request, u=Depends(require_user)):
    key = (request.client.host if request.client else "?", "pw:" + u["username"])
    n, first = _fails.get(key, (0, time.time()))
    if n >= 8 and time.time() - first < 300:
        raise HTTPException(429, "too many attempts; try again in a few minutes")
    if not verify_password(p.old, u["password_hash"] or ""):
        _fails[key] = (n + 1, first if time.time() - first < 300 else time.time())
        raise HTTPException(400, "current password is incorrect")
    _fails.pop(key, None)
    if len(p.new) < 6:
        raise HTTPException(400, "new password must be at least 6 characters")
    if p.new == p.old:
        raise HTTPException(400, "new password must differ from the current one")
    R(request).user_set(u["username"], password_hash=hash_password(p.new))
    # sign every other device out; the session that changed the password stays valid
    R(request).tokens_revoke_others(u["id"], hash_token(request.headers["authorization"][7:].strip()))
    R(request).audit(u["username"], "password.change", u["username"])
    return {"ok": True}


@r.post("/auth/avatar")
async def avatar_set(request: Request, u=Depends(require_user)):
    data = b""
    async for chunk in request.stream():
        data += chunk
        if len(data) > image.MAX_BYTES:
            raise HTTPException(413, "image too large (max %d KB)" % (image.MAX_BYTES // 1024))
    try:
        mime, _, _ = image.sniff(data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    R(request).avatar_set(u["id"], mime, data)
    return {"avatar_url": _avatar_url(u["username"], int(time.time()))}


@r.delete("/auth/avatar")
def avatar_delete(request: Request, u=Depends(require_user)):
    R(request).avatar_clear(u["id"])
    return {"ok": True}


@r.get("/users/{username}/avatar")
def avatar_get(username: str, request: Request):
    a = R(request).avatar_get(username)
    if not a:
        raise HTTPException(404, "no avatar")
    return Response(a[1], media_type=a[0], headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"})


@r.get("/auth/tokens")
def tokens(request: Request, u=Depends(require_user)):
    return {"tokens": R(request).tokens(u["id"])}


@r.post("/auth/tokens")
def token_create(t: TokenReq, request: Request, u=Depends(require_user)):
    tok = new_token()
    R(request).token_add(hash_token(tok), u["id"], "api", t.name)
    return {"token": tok}


@r.delete("/auth/tokens/{tid}")
def token_delete(tid: str, request: Request, u=Depends(require_user)):
    R(request).token_delete(u["id"], h=tid)
    return {"ok": True}


# ---------------- catalogue
def _index_public(request):
    import re
    from ..core import defaults
    u = request.app.state.settings.env_get("AIHUB_INDEX_URL") or R(request).setting("index_url", "") or defaults.load().get("index", {}).get("git_url", "")
    return re.sub(r"//[^/@]+@", "//", u) if u.startswith(("http", "ssh", "git@")) else ""


def source_url(repo):
    """Web link to the exact source of a version (host-style /tree/<ref>/<subdir>); credentials never appear."""
    import re
    repo = repo or {}
    u = re.sub(r"//[^/@]+@", "//", str(repo.get("url") or "").strip())
    m = re.match(r"^(?:ssh://)?git@([^:/]+)[:/](.+)$", u)
    if m:
        u = "https://%s/%s" % m.groups()
    u = re.sub(r"(\.git)?/?$", "", u)
    if not u.startswith(("http://", "https://")):
        return ""
    ref = repo.get("ref") or repo.get("branch") or ""
    sub = str(repo.get("subdir") or "").strip("/")
    return u if not ref else "%s/tree/%s%s" % (u, ref, "/" + sub if sub else "")


def _ser(request, p, detail=False):
    repos = R(request)
    vs = repos.versions(p["id"])
    live = [v for v in vs if not v["yanked"]]
    out = {k: p[k] for k in ("name", "type", "description", "tags", "latest_version", "hidden", "visibility", "updated")}
    out["downloads"] = sum(v["downloads"] for v in vs)
    out["category"] = category_of(p["tags"], p["type"])
    out["rating"] = repos.rating(p["id"])
    if detail:
        lm = next((v["manifest"] for v in live), {})
        out["requires"] = lm.get("requires", {})
        out["manifest"] = lm
        meta = lm.get("package") or {}
        out["authors"] = meta.get("authors") or []
        out["license"] = meta.get("license") or ""
        out["maintainers"] = [_public(m) | {"role": m["role"]} for m in repos.maintainers(p["id"])]
        out["versions"] = [{k: v[k] for k in ("version", "sha256", "size", "downloads", "yanked", "created")}
                           | {"source": source_url((v["manifest"] or {}).get("repo")),
                              "archive": direct_mod.is_direct((v["manifest"] or {}).get("repo"))} for v in vs]
    return out


def branding(request):
    """Public, admin-editable identity of this hub: name, contact details, logo."""
    rp = R(request)
    v = rp.setting("logo_v")
    theme = rp.setting("theme_color")
    return {"name": rp.setting("site_name") or "AI Hub", "theme": theme if theme in THEME_COLORS else "blue", "logo_url": basepath.url("/api/v1/logo?v=%s" % v) if v else None,
            "contact": {k[8:]: rp.setting(k) for k in TEXT_SETTINGS if k.startswith("contact_") and rp.setting(k)},
            "announcement": safehtml.clean(rp.setting("announcement") or "")}


@r.get("/meta")
def meta(request: Request, u=Depends(current_user)):
    b = branding(request)
    return {"name": b["name"], "theme": b["theme"], "logo_url": b["logo_url"], "contact": b["contact"], "announcement": b.get("announcement", ""), "public_url": public_url(request),
            "anonymous_review": "review" in R(request).perms("anonymous"), "anonymous_perms": sorted(R(request).perms("anonymous")),
            "cli_version": VERSION, "server_version": VERSION,
            "index_git_url": _index_public(request),
            "cli_git_url": env_first(request, "cli_git_url", "AIHUB_CLI_GIT_URL", _cli_default("git_url"))[0],
            "cli_git_branch": env_first(request, "cli_git_branch", "AIHUB_CLI_GIT_BRANCH", _cli_default("branch"))[0],
            "cli_git_subdir": env_first(request, "cli_git_subdir", "AIHUB_CLI_GIT_SUBDIR", _cli_default("subdir"))[0],
            "from_env": {k: env_first(request, k, e)[1] for k, e in CLI_GIT_ENV.items()},
            "overview": R(request).overview(viewer(request, u)),
            "public_browse": setting(request, "public_browse") == "1", "public_install": setting(request, "public_install") == "1",
            "allow_private": setting(request, "allow_private") == "1", "allow_source_download": setting(request, "allow_source_download") == "1", "default_visibility": setting(request, "default_visibility")}


@r.get("/version")
def version_info():
    return {"server": VERSION, "cli": VERSION}


@r.get("/logo")
def logo_get(request: Request):
    import base64
    d = R(request).setting("logo_data")
    if not d:
        raise HTTPException(404, "no logo")
    return Response(base64.b64decode(d), media_type=R(request).setting("logo_mime") or "image/png",
                    headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"})


@r.get("/healthz")
def healthz(): return {"ok": True}


@r.get("/readyz")
def readyz(request: Request):
    """Readiness: DB answers and the event queue is not backed up."""
    try:
        R(request).db.conn().execute("SELECT 1").fetchone()
    except Exception:
        raise HTTPException(503, "database unavailable")
    st = request.app.state
    checks = {"cache": st.cache.health()}
    return {"ok": True, "queued_events": len(st.events.buf), "database": st.settings.db_backend, "cache": st.cache.name, **checks}


def _cache_scope(v):
    """Cache key part: cached lists differ per viewer, so never share a private list across people."""
    who = "pub" if not v else ("all" if v["bypass"] else "u%d" % v["id"])
    return who + basepath.current().replace(":", "_")        # payloads hold URLs (avatars): one cache entry per path prefix


@r.get("/packages")
def packages(request: Request, q: str = "", type: str = None, tag: str = None, sort: str = "", category: str = None,
             page: int = 1, per_page: int = 20, mine: bool = False, u=Depends(current_user)):
    need_login_unless(request, u, "public_browse")
    if sort not in ("", "relevance", "updated", "created", "name", "downloads", "rating", "reviews"):
        raise HTTPException(400, "sort must be one of: relevance, updated, created, name, downloads, rating, reviews")
    v = viewer(request, u)
    per_page = min(max(per_page, 1), 100)
    key = "pkg:list:%s:%s|%s|%s|%s|%s|%s|%s|%s" % (_cache_scope(v), q, type, tag, sort, page, per_page, mine, category)
    def build():
        total, rows = R(request).packages(q, type, tag, sort, page, per_page, viewer=v, mine=mine, category=category)
        return {"total": total, "page": page, "per_page": per_page, "items": [_ser(request, p) for p in rows]}
    return C(request).get_or_set(key, TTL, build)


@r.get("/facets")
def facets(request: Request, u=Depends(current_user)):
    need_login_unless(request, u, "public_browse")
    v = viewer(request, u)
    return C(request).get_or_set("pkg:facets:" + _cache_scope(v), TTL, lambda: R(request).facets(v))


def _pkg_or_404(request, name, u=None, need="view"):
    """Load a package the caller may access. A package they can't see is reported as 404 (its existence is not revealed)."""
    p = R(request).package(naming.normalize(name))
    lvl = R(request).access_level(p["id"], viewer(request, u)) if p else None
    order = {"view": 1, "develop": 2, "admin": 3}
    if not p or not lvl:
        if not u and p and R(request).access_level(p["id"], None) is None:
            raise HTTPException(401, "sign in required")
        raise HTTPException(404, "package not found")
    if order[lvl] < order[need]:
        raise HTTPException(403, "requires %s access" % need)
    return p


@r.get("/packages/{name}")
def package(name: str, request: Request, u=Depends(current_user)):
    need_login_unless(request, u, "public_browse")
    p = _pkg_or_404(request, name, u)
    out = _ser(request, p, detail=True)
    out["access"] = R(request).access_level(p["id"], viewer(request, u))
    return out


@r.get("/packages/{name}/related")
def related(name: str, request: Request, u=Depends(current_user)):
    need_login_unless(request, u, "public_browse")
    p = _pkg_or_404(request, name, u)
    v = viewer(request, u)
    key = "pkg:related:%s:%s" % (_cache_scope(v), p["name"])
    def build():
        items = []
        for n in R(request).related(p["name"], v):
            q = R(request).package(n)
            if q:
                items.append(_ser(request, q))
        return {"items": items}
    return C(request).get_or_set(key, TTL, build)


@r.get("/activity")
def activity(request: Request, u=Depends(current_user)):
    """Anonymised recent install/use/update feed for the home page. No usernames or client ids ever leave this endpoint."""
    need_login_unless(request, u, "public_browse")
    v = viewer(request, u)
    key = "pkg:activity:" + _cache_scope(v)
    return C(request).get_or_set(key, 5, lambda: {"items": R(request).recent_activity(v, 15)})


@r.get("/packages/{name}/versions")
def versions(name: str, request: Request, u=Depends(current_user)):
    need_login_unless(request, u, "public_browse")
    return {"versions": _ser(request, _pkg_or_404(request, name, u), True)["versions"]}


README_NAMES = ("README.md", "readme.md", "Readme.md", "README.markdown", "README")
_readme_cache = {}                                  # raw url base -> (time, text); keeps the page fast and the git host unbothered
README_TTL, README_MAX = 600, 200000


def raw_urls(repo):
    """Raw-file URLs of the README for a repo dict {url, branch, ref?, subdir?}. GitHub: raw.githubusercontent.com;
    anything else is treated like GitLab (/-/raw/<ref>/...), which also covers self-hosted GitLab."""
    import re
    repo = repo or {}
    u = re.sub(r"//[^/@]+@", "//", str(repo.get("url") or "").strip())
    m = re.match(r"^(?:ssh://)?git@([^:/]+)[:/](.+)$", u)
    if m:
        u = "https://%s/%s" % m.groups()
    u = re.sub(r"(\.git)?/?$", "", u)
    m = re.match(r"^https://([^/]+)/(.+)$", u)
    ref = repo.get("ref") or repo.get("branch") or "HEAD"
    if not m:
        return []
    host, path = m.groups()
    sub = str(repo.get("subdir") or "").strip("/")
    sub = sub + "/" if sub else ""
    from urllib.parse import quote
    ref, sub = quote(ref, safe=""), quote(sub, safe="/")
    if host in ("github.com", "www.github.com"):
        return ["https://raw.githubusercontent.com/%s/%s/%s%s" % (path, ref, sub, n) for n in README_NAMES]
    return ["https://%s/%s/-/raw/%s/%s%s" % (host, path, ref, sub, n) for n in README_NAMES]


_public_host = nethost.public_host


def fetch_readme(repo):
    """README text from the repo itself (first name that exists), or ''."""
    import time
    import urllib.error
    import urllib.request
    for url in raw_urls(repo):
        hit = _readme_cache.get(url)
        if hit and time.time() - hit[0] < README_TTL:
            if hit[1]:
                return hit[1]
            continue
        text = ""
        if _public_host(url):
            try:
                rq = urllib.request.Request(url, headers={"User-Agent": "aihub", "Accept": "text/plain"})
                with urllib.request.urlopen(rq, timeout=6) as resp:
                    text = resp.read(README_MAX).decode("utf-8", "replace")
            except (urllib.error.URLError, OSError, ValueError):
                text = ""
        _readme_cache[url] = (time.time(), text)
        if len(_readme_cache) > 500:
            _readme_cache.pop(next(iter(_readme_cache)))
        if text:
            return text
    return ""


@r.get("/packages/{name}/readme")
def readme(name: str, request: Request, u=Depends(current_user)):
    need_login_unless(request, u, "public_browse")
    p = _pkg_or_404(request, name, u)
    repos = R(request)
    live = [v for v in repos.versions(p["id"]) if not v["yanked"]]
    md = repos.package_readme(p["id"]) or fetch_readme(((live[0]["manifest"] or {}).get("repo")) if live else None)
    return {"markdown": md, "html": markdown.render(md)}


@r.post("/resolve/tree")
def resolve_tree(body: dict, request: Request, u=Depends(current_user)):
    """Resolve several packages and all their dependencies in one request, dependencies first.
    body: {"roots": [{"name", "spec"}], "installed": {name: version}}. Constraints from every dependent are
    combined, so a package shared by two others gets one version that satisfies both. Installed packages
    that already satisfy their constraints are left out (they are not roots, so nothing to do)."""
    roots, have = body.get("roots"), body.get("installed") or {}
    if not isinstance(roots, list) or not roots or len(roots) > 200 or not isinstance(have, dict):
        raise HTTPException(400, "roots must be a list of 1-200 {name, spec}")
    if not all(isinstance(x, dict) and builtin.is_builtin(naming.normalize(str(x.get("name") or ""))) for x in roots):
        need_login_unless(request, u, "public_install")     # built-in packages install before any account exists
    base = public_url(request)
    need, chosen, queue, rootset = {}, {}, [], set()

    def add(name, spec, by):
        name = naming.normalize(str(name))
        need.setdefault(name, []).append((str(spec or "").strip(), by))
        queue.append(name)
        return name

    for x in roots:
        if not isinstance(x, dict) or not x.get("name"):
            raise HTTPException(400, "each root needs a name")
        rootset.add(add(x["name"], x.get("spec"), None))
    steps = 0
    while queue:
        steps += 1
        if steps > 5000:
            raise HTTPException(400, "dependency graph is too large")
        name = queue.pop(0)
        spec = ",".join(sp for sp, _ in need[name] if sp)
        cur = chosen.get(name)
        if cur and V.satisfies(cur["version"], spec):
            continue
        inst = have.get(name)
        if name not in rootset and inst and V.satisfies(str(inst), spec):
            chosen[name] = {"keep": True, "version": str(inst)}
            continue
        if not builtin.is_builtin(name):
            need_login_unless(request, u, "public_install")     # anonymous callers get built-ins only, dependencies included
        try:
            p = _pkg_or_404(request, name, u)
        except HTTPException as e:
            raise HTTPException(e.status_code, "%s: %s" % (name, e.detail))
        row = next((v for v in R(request).versions(p["id"]) if not v["yanked"] and V.satisfies(v["version"], spec)), None)
        if not row:
            by = ", ".join(sorted({b for _, b in need[name] if b})) or "your request"
            raise HTTPException(409, "no version of %s satisfies '%s' (required by %s)" % (name, spec or "*", by))
        for k in need:          # a re-pick drops the constraints the previous pick contributed
            need[k] = [(sp, b) for sp, b in need[k] if b != name]
        chosen[name] = {"row": row, "pkg": p, "version": row["version"]}
        for dep in ((row["manifest"].get("requires") or {}).get("packages")) or []:
            dn, ds = V.split_spec(str(dep))
            add(dn, ds, name)
    out, done = [], set()

    def visit(name):            # post-order over what is still reachable: dependencies come first
        if name in done or name not in chosen:
            return
        done.add(name)
        c = chosen[name]
        if c.get("keep"):
            return
        deps = [str(d) for d in ((c["row"]["manifest"].get("requires") or {}).get("packages")) or []]
        for d in deps:
            visit(naming.normalize(V.split_spec(d)[0]))
        v = c["row"]
        out.append({"name": c["pkg"]["name"], "version": v["version"], "sha256": v["sha256"], "size": v["size"],
                    "repo": (v["manifest"] or {}).get("repo"), "manifest": v["manifest"],
                    "requires": deps, "root": name in rootset})
    for name in sorted(rootset):
        visit(name)
    return {"packages": out}


@r.get("/resolve")
def resolve(name: str, request: Request, spec: str = "", u=Depends(current_user)):
    if not builtin.is_builtin(naming.normalize(name)):      # built-in packages install before any account exists
        need_login_unless(request, u, "public_install")
    p = _pkg_or_404(request, name, u)
    for v in R(request).versions(p["id"]):
        if not v["yanked"] and V.satisfies(v["version"], spec):
            return {"name": p["name"], "version": v["version"], "sha256": v["sha256"], "size": v["size"],
                    "repo": (v["manifest"] or {}).get("repo"),
                    "manifest": v["manifest"]}
    raise HTTPException(404, "no matching version")


@r.patch("/packages/{name}")
def patch_package(name: str, body: dict, request: Request, u=Depends(require_user)):
    p = _pkg_or_404(request, name, u, need="admin")
    f = {}
    if "hidden" in body: f["hidden"] = bool(body["hidden"])
    if "tags" in body: f["tags"] = [str(t).lower() for t in body["tags"]]
    R(request).package_patch(p["name"], **f)
    if "visibility" in body:
        _set_visibility(request, p, body["visibility"], u)
    R(request).audit(u["username"], "package.patch", p["name"], json.dumps(body)[:300])
    C(request).invalidate("pkg")
    return {"ok": True}


def _set_visibility(request, p, vis, u):
    if vis not in ("public", "private"):
        raise HTTPException(400, "visibility must be public or private")
    if vis == "private" and setting(request, "allow_private") != "1" and "admin" not in R(request).perms(u["role"]):
        raise HTTPException(403, "private repositories are disabled by the administrator")
    R(request).set_visibility(p["name"], vis)


# ---- sharing: who (users / groups) can view or develop a package. Repo admins and site admins manage it.
@r.get("/packages/{name}/access")
def access_get(name: str, request: Request, u=Depends(require_user)):
    p = _pkg_or_404(request, name, u, need="admin")
    return {"visibility": p["visibility"], "maintainers": [_public(m) | {"role": m["role"]} for m in R(request).maintainers(p["id"])],
            "access": [dict(x, avatar_v=x.get("avatar_v")) for x in R(request).access_list(p["id"])]}


@r.put("/packages/{name}/access")
def access_put(name: str, body: dict, request: Request, u=Depends(require_user)):
    """Grant or change: {type: 'user'|'group', name, access: 'view'|'develop'}"""
    p = _pkg_or_404(request, name, u, need="admin")
    ptype, pname, access = body.get("type"), str(body.get("name", "")), body.get("access")
    if access not in ("view", "develop"):
        raise HTTPException(400, "access must be view or develop")
    if ptype == "user":
        t = R(request).user_by_name(pname)
    elif ptype == "group":
        t = R(request).group_by_name(pname)
    else:
        raise HTTPException(400, "type must be user or group")
    if not t:
        raise HTTPException(404, "%s not found" % ptype)
    R(request).access_grant(p["id"], ptype, t["id"], access)
    R(request).audit(u["username"], "access.grant", p["name"], "%s:%s=%s" % (ptype, pname, access))
    C(request).invalidate("pkg")
    return {"ok": True}


@r.delete("/packages/{name}/access/{ptype}/{pname}")
def access_delete(name: str, ptype: str, pname: str, request: Request, u=Depends(require_user)):
    p = _pkg_or_404(request, name, u, need="admin")
    t = R(request).user_by_name(pname) if ptype == "user" else R(request).group_by_name(pname) if ptype == "group" else None
    if t:
        R(request).access_revoke(p["id"], ptype, t["id"])
        R(request).audit(u["username"], "access.revoke", p["name"], "%s:%s" % (ptype, pname))
        C(request).invalidate("pkg")
    return {"ok": True}


# ---------------- versions, maintainers, reviews
def _yank(request, name, version, flag, u):
    p = _pkg_or_404(request, name, u, need="develop")
    R(request).version_yank(p["id"], version, flag)
    R(request).audit(u["username"], "yank" if flag else "unyank", name, version)
    C(request).invalidate("pkg")
    return {"ok": True}


@r.post("/packages/{name}/versions/{version}/yank")
def yank(name: str, version: str, request: Request, u=Depends(require_user)):
    return _yank(request, name, version, True, u)


@r.post("/packages/{name}/versions/{version}/unyank")
def unyank(name: str, version: str, request: Request, u=Depends(require_user)):
    return _yank(request, name, version, False, u)


@r.delete("/packages/{name}")
def unpublish(name: str, request: Request, version: str = "", u=Depends(require_user)):
    """Remove a package (or one version) from the hub. Only those who manage it (site admin, owner, maintainer)."""
    p = _pkg_or_404(request, name, u, need="admin")
    repos = R(request)
    if version:
        if not any(v["version"] == version for v in repos.versions(p["id"])):
            raise HTTPException(404, "version not found")
        repos.version_delete(p["id"], version)
        if not repos.versions(p["id"]):
            repos.package_delete(p["id"])
    else:
        repos.package_delete(p["id"])
    request.app.state.sync.unindex(p["name"], version or None)
    request.app.state.direct.remove(p["name"], version or None)        # archives of packages published with `aihub dev publish -i`
    repos.audit(u["username"], "unpublish", p["name"], version)
    C(request).invalidate("pkg")
    return {"ok": True}


@r.get("/packages/{name}/maintainers")
def maintainers(name: str, request: Request, u=Depends(current_user)):
    need_login_unless(request, u, "public_browse")
    return {"maintainers": [_public(m) | {"role": m["role"]} for m in R(request).maintainers(_pkg_or_404(request, name, u)["id"])]}


@r.post("/packages/{name}/maintainers")
def maintainer_add(name: str, body: dict, request: Request, u=Depends(require_user)):
    p = _pkg_or_404(request, name, u, need="admin")
    t = R(request).user_by_name(body.get("username", ""))
    if not t:
        raise HTTPException(404, "user not found")
    R(request).maintainer_add(p["id"], t["id"])
    return {"ok": True}


@r.delete("/packages/{name}/maintainers/{username}")
def maintainer_remove(name: str, username: str, request: Request, u=Depends(require_user)):
    p = _pkg_or_404(request, name, u, need="admin")
    t = R(request).user_by_name(username)
    if t and len(R(request).maintainers(p["id"])) <= 1:
        raise HTTPException(400, "a repository needs at least one admin")
    if t:
        R(request).maintainer_remove(p["id"], t["id"])
    return {"ok": True}


@r.get("/me/packages")
def my_packages(request: Request, u=Depends(require_user)):
    return {"packages": R(request).packages_of(u["id"])}


# ---------------- personal knowledge (private to the signed-in user; the bot harness and `aihub knowledge` read it)
KNOWLEDGE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
KNOWLEDGE_MAX = 200_000
KNOWLEDGE_FILES = 200


@r.get("/me/knowledge")
def knowledge_list(request: Request, u=Depends(require_user)):
    return {"items": R(request).knowledge_list(u["id"])}


@r.get("/me/knowledge/{name}")
def knowledge_get(name: str, request: Request, u=Depends(require_user)):
    d = R(request).knowledge_get(u["id"], name)
    if not d:
        raise HTTPException(404, "no such note")
    return d


@r.put("/me/knowledge/{name}")
def knowledge_put(name: str, body: dict, request: Request, u=Depends(require_user)):
    if not KNOWLEDGE_NAME.match(name):
        raise HTTPException(400, "note name: lowercase letters, digits, . _ - (max 64)")
    text = body.get("body")
    if not isinstance(text, str) or len(text) > KNOWLEDGE_MAX:
        raise HTTPException(400, "body must be text of at most %d characters" % KNOWLEDGE_MAX)
    if not R(request).knowledge_get(u["id"], name) and len(R(request).knowledge_list(u["id"])) >= KNOWLEDGE_FILES:
        raise HTTPException(400, "at most %d notes" % KNOWLEDGE_FILES)
    R(request).knowledge_set(u["id"], name, text)
    return {"ok": True}


@r.delete("/me/knowledge/{name}")
def knowledge_delete(name: str, request: Request, u=Depends(require_user)):
    R(request).knowledge_delete(u["id"], name)
    return {"ok": True}


def _share_target(request, body):
    """-> (type, id, label) for {"user": name} or {"group": name}; 404 when it does not exist."""
    if body.get("user"):
        t = R(request).user_by_name(str(body["user"]).strip())
        if not t or t["role"] == "anonymous":
            raise HTTPException(404, "no such user")
        return "user", t["id"], t["username"]
    if body.get("group"):
        g = R(request).group_by_name(str(body["group"]).strip())
        if not g:
            raise HTTPException(404, "no such group")
        return "group", g["id"], g["name"]
    raise HTTPException(400, 'give "user" or "group"')


@r.get("/me/knowledge/{name}/shares")
def knowledge_shares(name: str, request: Request, u=Depends(require_user)):
    if not R(request).knowledge_get(u["id"], name):
        raise HTTPException(404, "no such note")
    return {"items": R(request).knowledge_shares(u["id"], name)}


@r.put("/me/knowledge/{name}/shares")
def knowledge_share(name: str, body: dict, request: Request, u=Depends(require_user)):
    if name == "soul":
        raise HTTPException(400, "soul is your persona and stays private")
    if not R(request).knowledge_get(u["id"], name):
        raise HTTPException(404, "no such note")
    access = body.get("access", "read")
    if access not in ("read", "write"):
        raise HTTPException(400, "access: read or write")
    t, pid, label = _share_target(request, body)
    if t == "user" and pid == u["id"]:
        raise HTTPException(400, "that is you")
    R(request).knowledge_share_set(u["id"], name, t, pid, access)
    return {"ok": True, "shared_with": label, "access": access}


@r.delete("/me/knowledge/{name}/shares")
def knowledge_unshare(name: str, body: dict, request: Request, u=Depends(require_user)):
    t, pid, _ = _share_target(request, body)
    R(request).knowledge_share_remove(u["id"], name, t, pid)
    return {"ok": True}


@r.get("/knowledge/shared")
def knowledge_shared(request: Request, u=Depends(require_user)):
    return {"items": R(request).knowledge_shared_with(u["id"])}


@r.get("/knowledge/shared/{owner}/{name}")
def knowledge_shared_get(owner: str, name: str, request: Request, u=Depends(require_user)):
    d = R(request).knowledge_shared_get(u["id"], owner, name)
    if not d:
        raise HTTPException(404, "no such shared note")
    return d


@r.put("/knowledge/shared/{owner}/{name}")
def knowledge_shared_put(owner: str, name: str, body: dict, request: Request, u=Depends(require_user)):
    d = R(request).knowledge_shared_get(u["id"], owner, name)
    if not d:
        raise HTTPException(404, "no such shared note")
    if d["access"] != "write":
        raise HTTPException(403, "read-only share")
    text = body.get("body")
    if not isinstance(text, str) or len(text) > KNOWLEDGE_MAX:
        raise HTTPException(400, "body must be text of at most %d characters" % KNOWLEDGE_MAX)
    o = R(request).user_by_name(owner)
    R(request).knowledge_set(o["id"], name, text)
    return {"ok": True}


# ---------------- reviews
@r.get("/packages/{name}/reviews")
def reviews(name: str, request: Request, u=Depends(current_user)):
    need_login_unless(request, u, "public_browse")
    p = _pkg_or_404(request, name, u)
    revs = [dict(_public(x), rating=x["rating"], body=x["body"], created=x["created"]) if x["role"] != "anonymous" and not x["reviewer"] else
            {"username": "", "display_name": x["reviewer"] or "Anonymous", "title": "", "avatar_url": None, "anonymous": True,
             "rating": x["rating"], "body": x["body"], "created": x["created"]} for x in R(request).reviews(p["id"])]
    return {"rating": R(request).rating(p["id"]), "reviews": revs}


@r.post("/packages/{name}/reviews")
def review_post(name: str, body: dict, request: Request, u=Depends(require_perm("review"))):
    anon = u["role"] == "anonymous"
    p = _pkg_or_404(request, name, None if anon else u)    # a signed-out visitor sees only what the public sees
    rating = int(body.get("rating", 0))
    if not 1 <= rating <= 5:
        raise HTTPException(400, "rating 1..5")
    who = _clean(body.get("name", ""), 40) if anon else None
    if anon and len(who) < 2:
        raise HTTPException(400, "please enter your name (2-40 characters) to post a review")
    R(request).review_upsert(p["id"], u["id"], rating, str(body.get("body", ""))[:2000], who)
    C(request).invalidate("pkg")
    return {"ok": True}


# ---------------- telemetry


# ---------------- telemetry
@r.get("/packages/{name}/stats")
def pkg_stats(name: str, request: Request, days: int = 30, u=Depends(current_user)):
    need_login_unless(request, u, "public_browse")
    p = _pkg_or_404(request, name, u)
    request.app.state.events.flush()
    return R(request).stats(p["name"], days)


@r.get("/packages/{name}/events")
def pkg_events(name: str, request: Request, u=Depends(require_user)):
    p = _pkg_or_404(request, name, u, need="admin")
    request.app.state.events.flush()
    return {"events": R(request).package_events(p["name"])}


@r.get("/rankings/{what}")
def rankings(what: str, request: Request, days: int = 30, u=Depends(current_user)):
    if what not in ("packages", "developers", "users", "reviews", "reviewed"):
        raise HTTPException(404)
    need_login_unless(request, u, "public_browse")
    request.app.state.events.flush()
    items = R(request).rank(what, days, viewer=viewer(request, u))
    if what in ("reviews", "reviewed"):                       # JSON-friendly numbers; reviews are all-time, not windowed
        items = [dict(x, avg=round(float(x["avg"]), 2), score=round(float(x["score"]), 2)) for x in items]
    return {"items": items, "windowed": what not in ("reviews", "reviewed")}


@r.get("/stats/overview")
def overview(request: Request, u=Depends(current_user)):
    need_login_unless(request, u, "public_browse")
    return R(request).overview(viewer(request, u))


# ---------------- admin
admin = Depends(require_perm("admin"))


@r.get("/admin/users")
def a_users(request: Request, q: str = "", u=Depends(require_any("admin", "reset_password"))):
    return {"users": [dict(_public(x), role=x["role"], status=x["status"], created=x["created"]) for x in R(request).users(q)]}


@r.post("/admin/users/{username}/status")
def a_status(username: str, body: dict, request: Request, u=admin):
    if body.get("status") not in ("active", "pending", "disabled"):
        raise HTTPException(400, "bad status")
    R(request).user_set(username, status=body["status"])
    R(request).audit(u["username"], "user.status", username, body["status"])
    return {"ok": True}


@r.post("/admin/users/{username}/role")
def a_role(username: str, body: dict, request: Request, u=admin):
    if body.get("role") == "anonymous" or body.get("role") not in R(request).roles():
        raise HTTPException(400, "unknown role")
    R(request).user_set(username, role=body["role"])
    R(request).audit(u["username"], "user.role", username, body["role"])
    return {"ok": True}


@r.post("/admin/users/{username}/reset-password")
def a_reset_password(username: str, request: Request, u=Depends(require_perm("reset_password"))):
    """Set a new random password and sign the person out everywhere. The password is returned once, never stored in clear."""
    repos = R(request)
    t = repos.user_by_name(username)
    if not t:
        raise HTTPException(404, "no such user")
    if t["id"] == u["id"]:
        raise HTTPException(400, "use Account → Password to change your own password")
    if "admin" in repos.perms(t["role"]) and "admin" not in repos.perms(u["role"]):
        raise HTTPException(403, "only an administrator can reset an administrator's password")
    pw = gen_password()
    repos.user_set(username, password_hash=hash_password(pw))
    repos.tokens_revoke_others(t["id"], "")
    repos.audit(u["username"], "user.password_reset", username, "all sessions signed out")
    return {"username": username, "password": pw}


@r.delete("/admin/users/{username}")
def a_delete(username: str, request: Request, u=admin):
    if username == u["username"]:
        raise HTTPException(400, "cannot delete yourself")
    R(request).user_delete(username)
    R(request).audit(u["username"], "user.delete", username)
    return {"ok": True}


@r.get("/admin/settings/registration")
def a_reg_get(request: Request, u=admin):
    return {"mode": R(request).setting("signup", "open" if request.app.state.settings.open_registration else "closed")}


@r.put("/admin/settings/registration")
def a_reg_set(body: dict, request: Request, u=admin):
    if body.get("mode") not in ("open", "approval", "closed"):
        raise HTTPException(400, "mode: open|approval|closed")
    R(request).set_setting("signup", body["mode"])
    R(request).audit(u["username"], "settings.registration", "", body["mode"])
    return {"ok": True}


THEME_COLORS = ("blue", "indigo", "purple", "pink", "red", "orange", "green", "teal")


# All admin-editable settings in one place: key -> (allowed values, label, help). Add a row to add a setting.
SETTINGS = {
    "signup": (("open", "approval", "closed"), "Sign-up", "Who can create their own account"),
    "public_browse": (("0", "1"), "Public browsing", "Let signed-out visitors browse public packages, rankings and docs"),
    "public_install": (("0", "1"), "Public install (no sign-in)", "Let the CLI install public packages without logging in"),
    "allow_private": (("0", "1"), "Allow private repositories", "Let people make their repositories private and share them"),
    "default_visibility": (("public", "private"), "Default visibility for new repositories", "Applied when a package is first published"),
    "allow_source_download": (("0", "1"), "Show source links", "Show a Source link for each version that points at its exact commit in the package's git repository"),
    "allow_group_creation": (("0", "1"), "Let members create groups", "People with the 'create groups' permission can make their own"),
    "theme_color": (THEME_COLORS, "Accent color", "Color of buttons, links and highlights across the site"),
}


# Numeric admin settings: key -> (min, max, label, help). The value in the database overrides the AIHUB_* environment default.
NUMERIC_SETTINGS = {
}


# Free-text admin settings: key -> (max length, kind, label, help). Shown on the site and in the footer.
TEXT_SETTINGS = {
    "site_name": (40, "text", "Site name", "Shown next to the logo and in the browser tab. Blank = AI Hub"),
    "cli_endpoint": (200, "url", "CLI default endpoint", "Hub address the CLI uses by default: written by install.sh / install.ps1 and used in package download links. Blank = the server's configured public URL"),
    "contact_name": (80, "text", "Contact name", "Person or team to reach for help with this hub"),
    "contact_email": (120, "email", "Contact email", "Shown in the footer as a mail link"),
    "contact_url": (200, "url", "Support link", "Help desk, chat or wiki address (http:// or https://)"),
    "contact_phone": (40, "text", "Contact phone", "Optional"),
    "announcement": (2000, "html", "Announcement banner", "Shown as a banner above the header on every page. Basic HTML is kept (a, b, strong, i, em, u, s, br, span, code, small, mark; links must be http/https/mailto); everything else is removed. Blank = hidden"),
}
_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


def _check_text(k, v):
    n, kind = TEXT_SETTINGS[k][:2]
    if kind == "html":
        return safehtml.clean(v, n)
    v = _clean(v, n)
    if v and kind == "email" and not _EMAIL_RE.match(v):
        raise HTTPException(400, "%s is not a valid email address" % k)
    if v and kind == "url" and not re.match(r"^https?://[^\s]+$", v, re.I):
        raise HTTPException(400, "%s must start with http:// or https://" % k)
    return v


def probe_hub(url, timeout=6):
    """Prove `url` is a reachable AI Hub that can serve the CLI. Raises HTTPException(400) with the reason otherwise."""
    import urllib.error
    import urllib.request

    def get(path):
        try:
            with urllib.request.urlopen(urllib.request.Request(url + path, headers={"User-Agent": "aihub-endpoint-check"}), timeout=timeout) as resp:
                return json.loads(resp.read(65536) or b"{}")
        except urllib.error.HTTPError as e:
            raise HTTPException(400, "CLI endpoint check failed: %s%s returned HTTP %s" % (url, path, e.code))
        except (urllib.error.URLError, OSError) as e:
            raise HTTPException(400, "CLI endpoint check failed: cannot reach %s (%s)" % (url, getattr(e, "reason", e)))
        except ValueError:
            raise HTTPException(400, "CLI endpoint check failed: %s%s did not return JSON, so this does not look like an AI Hub" % (url, path))
    info = get("/api/v1/version")
    if not isinstance(info, dict) or "server" not in info:
        raise HTTPException(400, "CLI endpoint check failed: %s is reachable but is not an AI Hub" % url)
    cli = get("/cli/version")
    if not isinstance(cli, dict) or not cli.get("sha256"):
        raise HTTPException(400, "CLI endpoint check failed: %s does not serve the CLI download (/cli/version)" % url)


@r.get("/admin/settings")
def a_settings(request: Request, u=admin):
    cur = {"signup": R(request).setting("signup", "open" if request.app.state.settings.open_registration else "closed")}
    for k in SETTINGS:
        if k == "theme_color":
            cur[k] = branding(request)["theme"]
        elif k != "signup":
            cur[k] = setting(request, k) if setting(request, k) is not None else R(request).setting(k, "0")
    for k in TEXT_SETTINGS:
        cur[k] = R(request).setting(k) or ""
    schema = [{"key": k, "options": list(v[0]), "label": v[1], "help": v[2]} for k, v in SETTINGS.items()]
    schema += [{"key": k, "type": "number", "min": v[0], "max": v[1], "label": v[2], "help": v[3]} for k, v in NUMERIC_SETTINGS.items()]
    schema += [{"key": k, "type": "text", "kind": v[1], "max": v[0], "label": v[2], "help": v[3]} for k, v in TEXT_SETTINGS.items()]
    return {"values": cur, "schema": schema, "logo_url": branding(request)["logo_url"], "server_version": VERSION}


@r.put("/admin/settings")
def a_settings_put(body: dict, request: Request, u=admin):
    done = {}
    for k, v in body.items():
        if k in TEXT_SETTINGS:
            done[k] = _check_text(k, v)
            if k == "cli_endpoint" and done[k]:
                done[k] = done[k].rstrip("/")
                probe_hub(done[k])  # blocking, but this route runs in the threadpool; nothing is saved if it fails
            continue
        if k in NUMERIC_SETTINGS:
            lo, hi = NUMERIC_SETTINGS[k][:2]
            if not str(v).isdigit() or not lo <= int(v) <= hi:
                raise HTTPException(400, "%s must be a whole number from %d to %d" % (k, lo, hi))
            done[k] = str(int(v))
            continue
        if k not in SETTINGS or str(v) not in SETTINGS[k][0]:
            raise HTTPException(400, "invalid setting: %s" % k)
        done[k] = str(v)
    for k, v in done.items():
        R(request).set_setting(k, v)
    R(request).audit(u["username"], "settings.update", "", json.dumps(done))
    C(request).invalidate("pkg")
    return {"ok": True}


@r.post("/admin/logo")
async def logo_set(request: Request, u=admin):
    import base64
    data = b""
    async for chunk in request.stream():
        data += chunk
        if len(data) > image.MAX_BYTES:
            raise HTTPException(413, "image too large (max %d KB)" % (image.MAX_BYTES // 1024))
    try:
        mime, _, _ = image.sniff(data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    rp = R(request)
    rp.set_setting("logo_data", base64.b64encode(data).decode())
    rp.set_setting("logo_mime", mime)
    rp.set_setting("logo_v", int(time.time()))
    rp.audit(u["username"], "settings.logo", "", "set")
    return {"logo_url": branding(request)["logo_url"]}


@r.delete("/admin/logo")
def logo_clear(request: Request, u=admin):
    rp = R(request)
    for k in ("logo_data", "logo_mime", "logo_v"):
        rp.set_setting(k, "")
    rp.audit(u["username"], "settings.logo", "", "removed")
    return {"ok": True}


# ---------------- groups. Site admins manage all groups; people with `create_groups` manage the ones they made.
# A group manager (flag on the membership, granted by an admin, the owner or another manager) runs the member list
# like the owner does, including who else is a manager; only an admin or the owner can delete the group.
GROUP_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,39}$")


def _can_group(request, u, g):
    repos = R(request)
    return "admin" in repos.perms(u["role"]) or g.get("owner_id") == u["id"] or g["name"] in repos.managed_groups(u["id"])


@r.get("/groups/capabilities")
def groups_caps(request: Request, u=Depends(require_user)):
    repos = R(request)
    perms = repos.perms(u["role"])
    mine = any(g.get("owner_id") == u["id"] for g in repos.groups())
    can_create = "admin" in perms or ("create_groups" in perms and setting(request, "allow_group_creation") == "1")
    return {"can_create": can_create, "is_admin": "admin" in perms, "owns_groups": mine,
            "member_of": len(repos.groups_of(u["id"])), "show_page": can_create or mine or bool(repos.groups_of(u["id"]))}


@r.get("/groups")
def groups_list(request: Request, u=Depends(require_user)):
    """Admins see every group; everyone else sees the groups they belong to or own."""
    repos = R(request)
    allg = repos.groups()
    is_admin = "admin" in repos.perms(u["role"])
    mine = set(repos.groups_of(u["id"]))
    managed = repos.managed_groups(u["id"])
    out = []
    for g in allg:
        owned = g.get("owner_id") == u["id"]
        if is_admin or owned or g["name"] in mine:
            # `can_manage` is decided here, on the server; owner ids never reach the browser
            out.append({k: v for k, v in g.items() if k != "owner_id"}
                       | {"can_manage": is_admin or owned or g["name"] in managed, "manager": g["name"] in managed,
                          "can_delete": is_admin or owned})
    return {"groups": out}


@r.get("/groups/lookup")
def groups_lookup(request: Request, q: str = "", u=Depends(require_user)):
    """Names only, for the share dialog: any signed-in person can pick a group or user to share a repo with."""
    q = q.strip().lower()[:40]
    g = [x["name"] for x in R(request).groups() if q in x["name"]][:8]
    us = [dict(_public(x)) for x in R(request).users(q)[:8] if x["status"] == "active"]
    return {"groups": g, "users": us}


@r.post("/groups")
def group_create(body: dict, request: Request, u=Depends(require_user)):
    repos = R(request)
    is_admin = "admin" in repos.perms(u["role"])
    if not is_admin and not ("create_groups" in repos.perms(u["role"]) and setting(request, "allow_group_creation") == "1"):
        raise HTTPException(403, "you cannot create groups")
    name = str(body.get("name", "")).strip().lower()
    if not GROUP_RE.match(name):
        raise HTTPException(400, "group name: 2-40 chars, lowercase letters, digits, . _ -")
    if repos.group_by_name(name):
        raise HTTPException(409, "group already exists")
    gid = repos.group_create(name, _clean(body.get("description", ""), 200), u["id"])
    repos.group_member_add(gid, [u["id"]])
    repos.audit(u["username"], "group.create", name)
    return {"ok": True, "name": name}


def _group_or_404(request, name, u):
    g = R(request).group_by_name(name)
    if not g or not _can_group(request, u, g):
        raise HTTPException(404, "group not found")
    return g


@r.get("/groups/{name}")
def group_get(name: str, request: Request, u=Depends(require_user)):
    g = _group_or_404(request, name, u)
    return {"name": g["name"], "description": g["description"],
            "can_delete": "admin" in R(request).perms(u["role"]) or g.get("owner_id") == u["id"],
            "members": [dict(_public(m)) | {"manager": bool(m.get("manager"))} for m in R(request).group_members(g["id"])]}


@r.put("/groups/{name}")
def group_update(name: str, body: dict, request: Request, u=Depends(require_user)):
    g = _group_or_404(request, name, u)
    R(request).group_update(g["id"], _clean(body.get("description", ""), 200))
    return {"ok": True}


@r.delete("/groups/{name}")
def group_delete(name: str, request: Request, u=Depends(require_user)):
    g = _group_or_404(request, name, u)
    if not ("admin" in R(request).perms(u["role"]) or g.get("owner_id") == u["id"]):
        raise HTTPException(403, "only an admin or the group owner can delete a group")
    R(request).group_delete(g["id"])
    R(request).audit(u["username"], "group.delete", name)
    C(request).invalidate("pkg")
    return {"ok": True}


@r.post("/groups/{name}/members")
def group_members_add(name: str, body: dict, request: Request, u=Depends(require_user)):
    g = _group_or_404(request, name, u)
    names = body.get("usernames") if isinstance(body.get("usernames"), list) else [body.get("username", "")]
    found = [R(request).user_by_name(str(n)) for n in names[:200]]
    missing = [str(n) for n, f in zip(names, found) if not f]
    mgr = body.get("manager") if isinstance(body.get("manager"), bool) else None      # true = also make them managers
    R(request).group_member_add(g["id"], [f["id"] for f in found if f], mgr)
    R(request).audit(u["username"], "group.members.add", name, ",".join(str(n) for n in names[:20]) + (" (manager)" if mgr else ""))
    C(request).invalidate("pkg")
    return {"ok": True, "missing": missing}


@r.put("/groups/{name}/members/{username}/manager")
def group_manager_set(name: str, username: str, body: dict, request: Request, u=Depends(require_user)):
    """Grant or take back the manager flag: {manager: true|false}. Granting to a non-member adds them first.
    Allowed for admins, the owner and the group's managers."""
    g = _group_or_404(request, name, u)
    flag = body.get("manager")
    if not isinstance(flag, bool):
        raise HTTPException(400, "give manager: true or false")
    t = R(request).user_by_name(username)
    if not t:
        raise HTTPException(404, "no such account")
    R(request).group_member_add(g["id"], [t["id"]], flag)
    R(request).audit(u["username"], "group.manager." + ("grant" if flag else "revoke"), name, t["username"])
    return {"ok": True}


@r.delete("/groups/{name}/members/{username}")
def group_member_remove(name: str, username: str, request: Request, u=Depends(require_user)):
    g = _group_or_404(request, name, u)
    t = R(request).user_by_name(username)
    if t:
        R(request).group_member_remove(g["id"], t["id"])
        C(request).invalidate("pkg")
    return {"ok": True}


# ---------------- roles (permission matrix)
ROLE_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")


def _perm_list(v):
    if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
        raise HTTPException(400, "permissions must be a list of strings")
    bad = [x for x in v if x not in ALL_PERMS]
    if bad:
        raise HTTPException(400, "unknown permission: " + ", ".join(bad))
    return sorted(set(v))


@r.get("/admin/roles")
def a_roles(request: Request, u=admin):
    return {"roles": R(request).roles(), "detail": R(request).roles_detail(),
            "permissions": [{"key": k, "label": v} for k, v in ALL_PERMS.items()], "anon_locked": list(ANON_LOCKED)}


@r.post("/admin/roles")
def a_role_create(body: dict, request: Request, u=admin):
    name = str(body.get("name", "")).strip().lower()
    if not ROLE_RE.match(name):
        raise HTTPException(400, "role name: 2-32 chars, lowercase letters, digits, - or _, starting with a letter")
    if name == "anonymous" or name in R(request).roles():
        raise HTTPException(409, "role already exists")
    perms = _perm_list(body.get("permissions", []))
    R(request).role_create(name, str(body.get("description", ""))[:200], perms)
    R(request).audit(u["username"], "role.create", name, ",".join(perms))
    return {"ok": True}


@r.put("/admin/roles/{name}")
def a_role_update(name: str, body: dict, request: Request, u=admin):
    repos = R(request)
    if name not in repos.roles():
        raise HTTPException(404, "role not found")
    perms = _perm_list(body["permissions"]) if "permissions" in body else None
    if name == "anonymous" and perms and set(perms) & set(ANON_LOCKED):
        raise HTTPException(400, "anonymous cannot hold: " + ", ".join(sorted(set(perms) & set(ANON_LOCKED))))
    if name == "admin" and perms is not None and "admin" not in perms:
        raise HTTPException(400, "the admin role must keep the admin permission")
    if perms is not None and name == u["role"] and "admin" not in perms:
        raise HTTPException(400, "you cannot remove your own admin access")
    desc = str(body["description"])[:200] if "description" in body else None
    repos.role_update(name, perms, desc)
    repos.audit(u["username"], "role.update", name, ",".join(perms) if perms is not None else "description")
    return {"ok": True}


@r.delete("/admin/roles/{name}")
def a_role_delete(name: str, request: Request, u=admin):
    repos = R(request)
    d = next((x for x in repos.roles_detail() if x["name"] == name), None)
    if not d:
        raise HTTPException(404, "role not found")
    if d["builtin"]:
        raise HTTPException(400, "built-in roles cannot be deleted")
    if d["users"]:
        raise HTTPException(409, "%d account(s) still have this role; move them first" % d["users"])
    repos.role_delete(name)
    repos.audit(u["username"], "role.delete", name)
    return {"ok": True}


# ---------------- batch account import -> generated password sheet
PW_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no look-alikes (0/O, 1/l/I)
MAX_BATCH = 300


def gen_password(n=14):
    while True:
        p = "".join(secrets.choice(PW_ALPHABET) for _ in range(n))
        if any(c.islower() for c in p) and any(c.isupper() for c in p) and any(c.isdigit() for c in p):
            return p


_U_H = {"username", "account", "login", "user name", "user_name"}
_D_H = {"display_name", "display name", "displayname", "full name", "full_name", "fullname", "name"}
_T_H = {"title", "job title", "job_title", "position"}
_R_H = {"role"}
_G_H = {"group", "groups", "group name", "group_name", "team", "teams"}


def _idx(header, names, exclude=()):
    for i, h in enumerate(header):
        if h in names and i not in exclude:
            return i
    return -1


def _batch_rows(rows, default_role):
    """Sheet -> [{username, role, display_name, title}]. With a header row, columns are found by name
    (username, display_name, title, role); without one the layout is `username, role`."""
    header = []
    if rows and rows[0]:
        cells = [c.strip().lower() for c in rows[0]]
        # A header row is recognised by its column names. `user` is deliberately not an alias: it is also a
        # role value, so "dan,user" is a data row. Two or more known names, or a known username column, mark a header.
        known = _U_H | _D_H | _T_H | _R_H | _G_H
        if sum(c in known for c in cells) >= 2 or cells[0] in _U_H:
            header, rows = cells, rows[1:]
    if header:
        ui = max(_idx(header, _U_H), 0)
        di, ti, ri, gi = _idx(header, _D_H, (ui,)), _idx(header, _T_H, (ui,)), _idx(header, _R_H, (ui,)), _idx(header, _G_H, (ui,))
    else:
        ui, di, ti, ri, gi = 0, -1, -1, 1, -1
    g = lambda r, i: r[i].strip() if 0 <= i < len(r) else ""
    out = []
    for r in rows:
        if not r or not any(c.strip() for c in r):
            continue
        # several groups in one cell: "data-team; design", "a,b" or "a | b"
        groups = []
        for x in re.split(r"[;,|\n]+", g(r, gi)):
            x = x.strip().lower()
            if x and x not in groups:
                groups.append(x)
        out.append({"username": g(r, ui), "role": g(r, ri).lower() or default_role,
                    "display_name": _clean(g(r, di), 60), "title": _clean(g(r, ti), 80), "groups": groups})
    return out


@r.post("/admin/users/batch")
async def a_batch(request: Request, role: str = "user", u=admin):
    repos = R(request)
    roles = repos.roles()
    roles.pop("anonymous", None)
    if role not in roles:
        raise HTTPException(400, "unknown default role: " + role)
    data = await request.body()
    try:
        items = _batch_rows(sheet.parse(data, request.headers.get("x-aihub-filename", "")), role)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not items:
        raise HTTPException(400, "no accounts found in the file")
    if len(items) > MAX_BATCH:
        raise HTTPException(400, "too many rows (max %d per upload)" % MAX_BATCH)

    # A group named in the sheet that does not exist yet is created (owned by the admin who uploads), then the people are added. No row is skipped for it.
    wanted = {x for it in items for x in it["groups"]}
    bad_group = {n for n in wanted if not GROUP_RE.match(n)}
    new_groups = []

    def work():
        seen, plan, result = set(), [], []
        for it in items:
            row = dict(it, password="", status="", groups=", ".join(it["groups"]))
            invalid = [x for x in it["groups"] if x in bad_group]
            if not re.match(r"^[A-Za-z0-9_.-]{2,40}$", it["username"]):
                row["status"] = "invalid username"
            elif it["role"] not in roles:
                row["status"] = "unknown role"
            elif invalid:
                row["status"] = "invalid group name: " + ", ".join(invalid)
            elif it["username"].lower() in seen:
                row["status"] = "duplicate in file"
            else:
                seen.add(it["username"].lower())
                row["password"] = gen_password()
                plan.append(row)
            result.append(row)
        need = {n for x in plan for n in (y.strip() for y in x["groups"].split(",")) if n}   # only groups a created account will join
        group_ids = repos.group_ids_by_name(need)
        for n in sorted(need - set(group_ids)):
            try:
                group_ids[n] = repos.group_create(n, "", u["id"])
                new_groups.append(n)
            except Exception:                                       # created meanwhile by someone else: use theirs
                gid = repos.group_by_name(n)
                if not gid:
                    raise
                group_ids[n] = gid["id"]
        made = repos.users_create_many([(x["username"], hash_password(x["password"]), x["role"], x["display_name"], x["title"],
                                         [group_ids[n] for n in (y.strip() for y in x["groups"].split(",")) if n]) for x in plan])
        for x in plan:
            if x["username"] in made:
                x["status"] = "created"
            else:
                x["status"], x["password"] = "skipped: username exists", ""
        return result, len(made)

    result, created = await run_in_threadpool(work)
    repos.audit(u["username"], "users.batch", "", "created=%d rows=%d new_groups=%d" % (created, len(items), len(new_groups)))
    if wanted:
        C(request).invalidate("pkg")   # group membership changes who can see private repositories
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["username", "display_name", "title", "role", "groups", "password", "status"])
    for x in result:
        w.writerow([sheet.safe_cell(x["username"]), sheet.safe_cell(x["display_name"]), sheet.safe_cell(x["title"]),
                    x["role"], sheet.safe_cell(x["groups"]), x["password"], x["status"]])
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": 'attachment; filename="new-accounts.csv"', "Cache-Control": "no-store",
        "X-Created": str(created), "X-Skipped": str(len(result) - created), "X-Groups-Created": str(len(new_groups))})


audit_dep = Depends(require_perm("audit"))


@r.get("/admin/audit")
def a_audit(request: Request, u=audit_dep): return {"audit": R(request).audit_list()}


@r.get("/audit")
def audit_search(request: Request, source: str = "tools", q: str = "", actor: str = "", action: str = "", package: str = "", kind: str = "",
                 days: float = 7, start: float = 0, end: float = 0, page: int = 1, per_page: int = 50, host: str = "", os: str = "", u=audit_dep):
    """Security audit with filters. `source=tools` = agent tool calls / installs; `source=admin` = account, role and setting changes."""
    if source not in ("tools", "admin"):
        raise HTTPException(400, "source must be tools or admin")
    if kind and kind not in ("install", "update", "uninstall", "use", "error"):
        raise HTTPException(400, "bad kind")
    request.app.state.events.flush()
    now = time.time()
    since = start or (now - days * 86400 if days > 0 else 0)
    per_page = max(1, min(per_page, 200))
    res = R(request).audit_search(source, q.strip()[:200], actor, action.strip()[:200], package, kind, since, end, per_page, (max(page, 1) - 1) * per_page, host[:128], os[:24])
    return dict(res, page=max(page, 1), per_page=per_page, actors=R(request).audit_actors()[source], **R(request).audit_devices())


@r.get("/audit/export")
def audit_export(request: Request, source: str = "tools", q: str = "", actor: str = "", action: str = "", package: str = "", kind: str = "",
                 days: float = 7, start: float = 0, end: float = 0, host: str = "", os: str = "", u=audit_dep):
    if source not in ("tools", "admin"):
        raise HTTPException(400, "source must be tools or admin")
    request.app.state.events.flush()
    now = time.time()
    since = start or (now - days * 86400 if days > 0 else 0)
    res = R(request).audit_search(source, q.strip()[:200], actor, action.strip()[:200], package, kind, since, end, 5000, 0, host[:128], os[:24])
    cols = (["ts", "actor", "action", "target", "detail"] if source == "admin" else
            ["ts", "actor", "kind", "package", "version", "component", "source", "host", "os", "local_user", "ip", "cwd", "detail"])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for x in res["items"]:
        w.writerow([sheet.safe_cell(time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(x["ts"])) + "Z") if c == "ts" else sheet.safe_cell(x.get(c) or "") for c in cols])
    R(request).audit(u["username"], "audit.export", source, "%d rows" % len(res["items"]))
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": 'attachment; filename="audit-%s.csv"' % source, "Cache-Control": "no-store"})


# ---------------- docs (rendered from the repo's docs/ folder)
_DOCS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "docs")


def _doc_files():
    if not os.path.isdir(_DOCS):
        return {}
    return {f[:-3]: os.path.join(_DOCS, f) for f in sorted(os.listdir(_DOCS)) if f.endswith(".md") and re.match(r"^[\w-]+\.md$", f)}


@r.get("/docs")
def docs_list():
    out = []
    for slug, path in _doc_files().items():
        first = open(path, encoding="utf-8").readline().lstrip("# ").strip()
        out.append({"slug": slug, "title": first or slug, "raw_url": basepath.url("/api/v1/docs/%s/raw" % slug)})
    return {"docs": out}


@r.get("/docs/{slug}")
def docs_get(slug: str):
    path = _doc_files().get(slug)
    if not path:
        raise HTTPException(404, "doc not found")
    return {"slug": slug, "html": markdown.render(open(path, encoding="utf-8").read())}


@r.get("/docs/{slug}/raw")
def docs_raw(slug: str):
    """Plain Markdown source, for agents and scripts that want the latest docs without HTML."""
    path = _doc_files().get(slug)
    if not path:
        raise HTTPException(404, "doc not found")
    return PlainTextResponse(open(path, encoding="utf-8").read(), media_type="text/markdown; charset=utf-8")


# ---------------- usage dashboard (permission: view_dashboard, grantable per role)
_DASH_KINDS = ("install", "update", "uninstall", "use", "error", "publish")


def _dash_args(days, kind, identity, date_from, date_to):
    if days not in (7, 14, 30, 90, 180, 365):
        raise HTTPException(400, "days must be one of 7, 14, 30, 90, 180, 365")
    if kind and any(k not in _DASH_KINDS for k in kind.split(",")):
        raise HTTPException(400, "unknown event kind")
    if identity and identity not in ("anonymous", "signed-in"):
        raise HTTPException(400, "identity must be anonymous or signed-in")


@r.get("/dashboard")
def dashboard(request: Request, days: int = 30, package: str = None, type: str = None, user: str = None, source: str = None,
              kind: str = None, identity: str = None, host: str = None, os: str = None, q: str = None, date_from: str = Query(None, alias="from"),
              date_to: str = Query(None, alias="to"), u=Depends(require_perm("view_dashboard"))):
    _dash_args(days, kind, identity, date_from, date_to)
    v = viewer(request, u)
    if package:
        package = _pkg_or_404(request, package, u)["name"]      # also enforces that the viewer may see it
    key = "dash:%s:%s" % (_cache_scope(v), "|".join(str(x) for x in (days, package, type, user, source, kind, identity, host, os, q, date_from, date_to)))

    def build():
        request.app.state.events.flush()                          # only on a miss: a cached answer needs no write
        return R(request).dashboard(days, package, viewer=v, type=type, user=user, source=source, kind=kind,
                                    identity=identity, host=host, os=os, q=(q or "").strip()[:80] or None, date_from=date_from, date_to=date_to)
    try:
        return C(request).get_or_set(key, 15, build)
    except ValueError as e:
        raise HTTPException(400, str(e))


@r.get("/dashboard/events")
def dashboard_events(request: Request, days: int = 30, page: int = 1, per_page: int = 25, package: str = None, type: str = None,
                     user: str = None, source: str = None, kind: str = None, identity: str = None, host: str = None, os: str = None, q: str = None,
                     date_from: str = Query(None, alias="from"), date_to: str = Query(None, alias="to"),
                     u=Depends(require_perm("view_dashboard"))):
    _dash_args(days, kind, identity, date_from, date_to)
    v = viewer(request, u)
    if package:
        package = _pkg_or_404(request, package, u)["name"]
    request.app.state.events.flush()
    try:
        return R(request).dashboard_events(v, days, date_from, date_to, page, per_page, type=type, package=package, user=user,
                                           source=source, kind=kind, identity=identity, host=host, os=os, q=(q or "").strip()[:80] or None)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------------- sync (git telemetry inbox + git index -> SQLite)
SYNC_KEYS = ("telemetry_url", "telemetry_inbox_branch", "telemetry_branch", "telemetry_archive", "localize_users", "live_kinds", "git_telemetry_sync", "git_index_sync", "index_url", "index_branch", "index_path", "sync_interval",
             "cli_git_url", "cli_git_branch", "cli_git_subdir", "client_refresh_hours") + tuple(LIMITS)
SECRET_KEYS = ()


def _check_cli_git(k, v):
    """Branch and sub folder end up inside a pip spec (git+URL@branch#subdirectory=dir), so reject anything that would break it."""
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in v):
        raise HTTPException(400, f"{k} must not contain spaces or control characters")
    if k == "cli_git_subdir" and (any(ch in v for ch in "#?") or ".." in v or v.startswith("/")):
        raise HTTPException(400, "cli_git_subdir must be a relative folder without '#', '?', '..' or a leading '/'")


def _check_limit(k, v):
    """Admin number from LIMITS: whole seconds/percent within its range. Blank = back to the default."""
    if v == "":
        return ""
    lo, hi = LIMITS[k][1], LIMITS[k][2]
    if not re.fullmatch(r"\d{1,7}", v) or not lo <= int(v) <= hi:
        raise HTTPException(400, "%s must be a whole number from %d to %d" % (k, lo, hi))
    return str(int(v))


@r.get("/admin/sync")
def sync_status(request: Request, u=admin):
    sy, repos = request.app.state.sync, R(request)
    cfg = {k: repos.setting(k, "") for k in SYNC_KEYS}
    envget = request.app.state.settings.env_get
    for k, e in SYNC_ENV.items():
        if envget(e):
            cfg[k] = envget(e)                                                   # what is really used: environment / .env wins
    for k in SECRET_KEYS:
        cfg[k] = "set" if cfg[k] else ""                                         # never echo secrets
    cfg["sync_interval"] = cfg["sync_interval"] or "300"
    for k in LIMITS:
        cfg[k] = str(sy.num(k))                                                  # the value really in use (stored, environment or default)
    limits = [{"key": k, "label": v[3], "min": v[1], "max": v[2], "default": v[0]} for k, v in LIMITS.items()]
    ing = request.app.state.ingest
    st = dict(sy.status, next_in=round(sy.delay()), git_flush_in=sy.git_flush_in(), index_push_in=sy.index_push_in(),
              held_batches=len(sy.held), staged_publishes=len(sy.staged), ingest=dict(ing.stats, buffered=len(ing.rows)))
    return {"config": cfg, "limits": limits, "status": st, "cursor": repos.setting("sync_cursor"),
            "from_env": {"index": bool(request.app.state.settings.env_get("AIHUB_INDEX_URL")),
                         **{k: bool(request.app.state.settings.env_get(e)) for k, e in SYNC_ENV.items()}}}


@r.put("/admin/sync")
def sync_update(body: dict, request: Request, u=admin):
    repos = R(request)
    for k in SYNC_KEYS:
        if k in body and body[k] is not None:
            v = str(body[k]).strip()
            if k in SECRET_KEYS and v in ("", "set"):
                continue                                 # blank/echo = keep the stored secret
            if k in ("telemetry_archive", "localize_users", "git_telemetry_sync", "git_index_sync") and v not in ("0", "1"):
                raise HTTPException(400, "%s must be 0 or 1" % k)
            if k == "live_kinds" and (any(x not in _DASH_KINDS for x in v.split(",") if x)):
                raise HTTPException(400, "live_kinds must be a comma list of: " + ", ".join(_DASH_KINDS))
            if k in ("cli_git_branch", "cli_git_subdir"):
                _check_cli_git(k, v)
            if k == "sync_interval":
                try:
                    parse_schedule(v)
                except ValueError as e:
                    raise HTTPException(400, str(e))
            if k in LIMITS:
                v = _check_limit(k, v)
            repos.set_setting(k, v)
    repos.audit(u["username"], "settings.sync", "", ",".join(k for k in SYNC_KEYS if k in body))
    request.app.state.sync.forget()
    request.app.state.sync.wake.set()                    # re-plan with the new schedule now
    return sync_status(request, u)


BACKUP_KEYS = ("backup_enabled", "backup_schedule", "backup_events_chunk", "backup_jitter", "backup_jitter_max")


def _backup_view(request, u):
    bk, repos = request.app.state.backup, R(request)
    sy = request.app.state.sync
    return {"config": {"backup_enabled": "1" if bk.enabled() else "0", "backup_schedule": bk.schedule_text(), "backup_events_chunk": str(bk.chunk()),
                       "backup_jitter": str(sy.num("backup_jitter")), "backup_jitter_max": str(sy.num("backup_jitter_max"))},
            "status": dict(bk.status, next_in=round(bk.delay())),
            "from_env": {"backup_enabled": bool(request.app.state.settings.env_get("AIHUB_BACKUP_ENABLED"))},     # the environment wins over the saved switch
            "target": {"url": gitx.redact_url(request.app.state.sync.index_source()[0]), "branch": request.app.state.sync.index_source()[1]}}


@r.get("/admin/backup")
def backup_status(request: Request, u=admin):
    return _backup_view(request, u)


@r.put("/admin/backup")
def backup_update(body: dict, request: Request, u=admin):
    repos = R(request)
    for k in BACKUP_KEYS:
        if k in body and body[k] is not None:
            v = str(body[k]).strip()
            if k == "backup_enabled" and v not in ("0", "1"):
                raise HTTPException(400, "backup_enabled must be 0 or 1")
            if k == "backup_schedule":
                try:
                    parse_schedule(v)
                except ValueError as e:
                    raise HTTPException(400, str(e))
            if k == "backup_events_chunk" and not (v.isdigit() and 1000 <= int(v) <= 1000000):
                raise HTTPException(400, "backup_events_chunk must be between 1000 and 1000000")
            if k in LIMITS:
                v = _check_limit(k, v)
            repos.set_setting(k, v)
    repos.audit(u["username"], "settings.backup", "", ",".join(k for k in BACKUP_KEYS if k in body))
    request.app.state.sync.forget()
    request.app.state.backup.wake.set()
    return _backup_view(request, u)


@r.post("/admin/backup/run")
def backup_run(request: Request, u=admin):
    """Back up now (to the index git repo), whether or not the schedule is enabled."""
    try:
        request.app.state.ingest.flush()                  # events still in memory go into this backup
        request.app.state.backup.run_once()
    except Exception as e:
        raise HTTPException(502, "backup failed: %s" % str(e)[:300])
    R(request).audit(u["username"], "backup.run", "", "")
    return _backup_view(request, u)


def _updater(request):
    return request.app.state.updater


def _update_view(request):
    from . import updater as U
    up = _updater(request)
    v = up.view()
    v["config"] = {k: up.cfg(k) for k in U.KEYS}
    v["from_env"] = {"update_enabled": bool(request.app.state.settings.env_get("AIHUB_UPDATE_ENABLED"))}
    v["restart_hint"] = "exec" if up.cfg("update_restart") == "exec" else "exit"
    return v


@r.get("/admin/update")
def update_status(request: Request, u=admin):
    return _update_view(request)


@r.put("/admin/update")
def update_settings(body: dict, request: Request, u=admin):
    from . import updater as U
    up, repos = _updater(request), R(request)
    for k in U.KEYS:
        if k in body and body[k] is not None:
            try:
                v = U.check_setting(k, body[k])
            except ValueError as e:
                raise HTTPException(400, str(e))
            repos.set_setting(k, v)
    repos.audit(u["username"], "settings.update", "", ",".join(k for k in U.KEYS if k in body))
    up.wake.set()
    return _update_view(request)


def _update_go(request, u, action, label, target="", apply=True, force=False, wait=False, resolutions=None):
    from . import updater as U
    up = _updater(request)
    R(request).audit(u["username"], "update." + label, target[:40], "force" if force else "")
    kw = dict(action=action, trigger="manual", user=u["username"], apply=apply, force=force, target=target)
    if resolutions is not None:
        kw["resolutions"] = resolutions
    try:
        if wait:
            return {"run": up.run(**kw)}
        up.start_async(**kw)
    except U.Busy as e:
        raise HTTPException(409, str(e))
    return _update_view(request)


@r.post("/admin/update/check")
def update_check(request: Request, wait: bool = False, u=admin):
    """Fetch and report whether a newer version is on the update branch; nothing is changed."""
    return _update_go(request, u, "update", "check", apply=False, wait=wait)


@r.post("/admin/update/run")
def update_run(request: Request, force: bool = False, wait: bool = False, u=admin):
    """Update now: fetch, merge, verify, switch and restart (force = also try a commit that failed before)."""
    return _update_go(request, u, "update", "run", force=force, wait=wait)


@r.post("/admin/update/rollback")
def update_rollback(body: dict, request: Request, wait: bool = False, u=admin):
    """Go back to a commit (full or short hash, tag, branch) of this checkout: verified, switched, restarted and watched."""
    target = str(body.get("target") or "").strip()
    if not target:
        raise HTTPException(400, "target is required")
    return _update_go(request, u, "rollback", "rollback", target=target, wait=wait)


@r.get("/admin/update/conflict")
def update_conflict(request: Request, u=admin):
    """The conflict that waits for a person (policy "manual"): every file with its three sides and the merge blocks, for the resolver page."""
    c = _updater(request).conflict()
    if not c:
        raise HTTPException(404, "no conflict is waiting")
    return c


@r.post("/admin/update/resolve")
def update_resolve(body: dict, request: Request, wait: bool = False, u=admin):
    """Apply the admin's answers and continue the update (verify, switch, restart, watch).
    body: {"files": {"<path>": {"mode": "ours"|"theirs"} | {"mode": "hunks", "choices": ["ours"|"theirs"|"both"|{"text": ...}, ...]} | {"mode": "custom", "text": "..."}}}
    "ours" = the file as it is on this hub, "theirs" = the incoming version."""
    files = body.get("files")
    if not isinstance(files, dict) or not files:
        raise HTTPException(400, "files is required")
    if not _updater(request).conflict():
        raise HTTPException(409, "no conflict is waiting (the branch or the local files changed): press Update now to look again")
    return _update_go(request, u, "update", "resolve", force=True, wait=wait, resolutions=files)


@r.delete("/admin/update/conflict")
def update_conflict_dismiss(request: Request, u=admin):
    """Drop the waiting conflict (the next check looks again)."""
    _updater(request).dismiss_conflict()
    R(request).audit(u["username"], "update.dismiss", "", "")
    return _update_view(request)


@r.get("/admin/update/versions")
def update_versions(request: Request, limit: int = Query(40, ge=1, le=200), u=admin):
    return {"versions": _updater(request).versions(limit)}


@r.get("/admin/update/runs")
def update_runs(request: Request, limit: int = Query(50, ge=1, le=200), u=admin):
    return {"runs": _updater(request).runs(limit)}


@r.get("/admin/update/runs/{rid}")
def update_run_get(rid: str, request: Request, u=admin):
    from . import updater as U
    run = U.Run.load(_updater(request), rid)
    if not run:
        raise HTTPException(404, "run not found")
    return run.d


@r.post("/admin/sync/run")
def sync_run(request: Request, full: bool = False, u=admin):
    """Poll now. full=true forgets the cursor and looks back over everything (safe: events are deduplicated)."""
    repos = R(request)
    if full:
        repos.set_setting("sync_cursor", "")
        repos.set_setting("sync_index_dirty", "1")
        repos.set_setting("sync_index_hashes", "")
    try:
        request.app.state.ingest.flush()                  # what is buffered in memory goes to the database and the git queue first
        request.app.state.sync.run_once()
    except Exception as e:
        raise HTTPException(502, "sync failed: %s" % str(e)[:300])
    return sync_status(request, u)


@r.post("/index/package")
def index_package(body: dict, request: Request, u=Depends(require_perm("index"))):
    """Force one package version into the index (for a publish event that was lost or never reached the hub).
    Same checks as the event path: public host (or the index host), and the repo's aihub.toml at that commit decides what is stored."""
    g = lambda k: str(body.get(k) or "").strip()
    name, ver, gurl = naming.normalize(g("package")), g("version"), g("git_url")
    if not name or not ver or not gurl:
        raise HTTPException(400, "package, version and git_url are required")
    ev = {"package": name, "version": ver, "git_url": gurl, "git_branch": g("git_branch") or "main",
          "git_subdir": g("git_subdir"), "commit": g("commit")}
    results = []
    try:
        changed = request.app.state.sync.update_index([ev], force=True, results=results)
    except Exception as e:
        raise HTTPException(502, "index update failed: %s" % gitx.redact_url(str(e))[:300])
    R(request).audit(u["username"], "index.force", name, ver)
    status = results[-1][2] if results else "nothing to do"
    if changed:
        request.app.state.sync.repos.set_setting("sync_index_dirty", "1")
        try:
            request.app.state.sync.pull_index()
        except Exception as e:
            raise HTTPException(502, "indexed, but the hub could not re-read the index: %s" % gitx.redact_url(str(e))[:200])
    if status not in ("indexed", "already indexed"):
        raise HTTPException(422, "not indexed: %s" % status)
    return {"package": name, "version": ver, "status": status, "index_changed": bool(changed)}


def _direct_user(u):
    if not u.get("id"):
        raise HTTPException(401, "sign in first: aihub login")


def _direct_limit(request):
    return request.app.state.sync.num("direct_max_mb") * 1024 * 1024


@r.post("/index/direct/uploads")
def direct_upload_start(body: dict, request: Request, u=Depends(require_perm("publish"))):
    """Open (or resume) a chunked upload of a package archive: {size, sha256} -> {upload_id, chunk_size, chunks, received}.
    For archives bigger than a proxy lets through in one request; chunks may be sent in parallel."""
    _direct_user(u)
    try:
        return request.app.state.direct_uploads.start(u["username"], body.get("size"), str(body.get("sha256") or "").lower(), _direct_limit(request))
    except direct_mod.DirectError as e:
        raise HTTPException(e.code, str(e))


@r.get("/index/direct/uploads/{uid}")
def direct_upload_status(uid: str, request: Request, u=Depends(require_perm("publish"))):
    _direct_user(u)
    try:
        return request.app.state.direct_uploads.info(u["username"], uid)
    except direct_mod.DirectError as e:
        raise HTTPException(e.code, str(e))


@r.put("/index/direct/uploads/{uid}/chunks/{n}")
async def direct_upload_chunk(uid: str, n: int, request: Request, u=Depends(require_perm("publish"))):
    """One chunk of exactly `chunk_size` bytes (the last one is shorter). `X-Chunk-Sha256` is checked when sent. Sending a chunk again is fine."""
    _direct_user(u)
    ups = request.app.state.direct_uploads
    data, cap = bytearray(), ups.chunk_size + 1
    async for part in request.stream():
        data += part
        if len(data) > cap:
            raise HTTPException(413, "chunk too large")
    try:
        await run_in_threadpool(ups.put, u["username"], uid, n, bytes(data), request.headers.get("x-chunk-sha256", ""))
    except direct_mod.DirectError as e:
        raise HTTPException(e.code, str(e))
    except OSError as e:
        raise HTTPException(503, "cannot store the chunk: %s" % e, headers={"Retry-After": "10"})
    return {"ok": True, "chunk": n}


@r.post("/index/direct/uploads/{uid}/complete")
async def direct_upload_complete(uid: str, request: Request, u=Depends(require_perm("publish"))):
    """Join the chunks, check size and sha256, then publish exactly like `POST /index/direct`. The session is removed unless the hub was
    only temporarily unable (5xx), so a retry of `complete` is possible."""
    _direct_user(u)
    ups, direct = request.app.state.direct_uploads, request.app.state.direct
    try:
        tmp = direct.scratch()
    except OSError as e:
        raise HTTPException(503, "cannot store the upload: %s" % e, headers={"Retry-After": "30"})
    try:
        try:
            await run_in_threadpool(ups.assemble, u["username"], uid, tmp)
        except direct_mod.DirectError as e:
            raise HTTPException(e.code, str(e))
        try:
            res = await run_in_threadpool(_publish_direct, request, u, tmp)
        except HTTPException as e:
            if e.status_code < 500:
                ups.drop(u["username"], uid)
            raise
        ups.drop(u["username"], uid)
        return res
    except OSError as e:
        raise HTTPException(503, "cannot store the upload: %s" % e, headers={"Retry-After": "30"})
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


@r.delete("/index/direct/uploads/{uid}")
def direct_upload_cancel(uid: str, request: Request, u=Depends(require_perm("publish"))):
    _direct_user(u)
    request.app.state.direct_uploads.drop(u["username"], uid)
    return {"ok": True}


@r.post("/index/direct")
async def publish_direct(request: Request, u=Depends(require_perm("publish"))):
    """`aihub dev publish -i`: the body is the package as a .tar.gz (aihub.toml at its top). The hub keeps it in its data folder
    (`<data>/direct`), lists it in the index with the repository `hub://direct` and records a `publish` event. No git, no git account:
    installs download the archive from the hub. A new package belongs to the publisher; an existing one only to those who manage it."""
    _direct_user(u)
    direct = request.app.state.direct
    limit = _direct_limit(request)
    n = request.headers.get("content-length")
    if n and n.isdigit() and int(n) > limit:
        raise HTTPException(413, "archive too large (limit %d MB, admin setting direct_max_mb)" % (limit >> 20))
    try:
        tmp = direct.scratch()
    except OSError as e:
        raise HTTPException(503, "cannot store the upload: %s" % e, headers={"Retry-After": "30"})
    try:
        size = 0
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, "archive too large (limit %d MB, admin setting direct_max_mb)" % (limit >> 20))
                await run_in_threadpool(f.write, chunk)
        return await run_in_threadpool(_publish_direct, request, u, tmp)
    except OSError as e:
        raise HTTPException(503, "cannot store the upload: %s" % e, headers={"Retry-After": "30"})
    finally:
        try:
            os.remove(tmp)                                            # moved away when stored; this is only the leftover of a refusal
        except OSError:
            pass


def _publish_direct(request, u, tmp):
    repos, direct, sy = R(request), request.app.state.direct, request.app.state.sync
    try:
        pkg, sha, size, _ = direct.inspect(tmp)
    except direct_mod.DirectError as e:
        raise HTTPException(e.code, str(e))
    name, ver = pkg["name"], str(pkg["version"])
    stored = direct.sha(name, ver)
    if stored and stored != sha and direct.same_content(name, ver, tmp):
        sha = stored                      # the same files packed again (other timestamps, tool or zlib): the same release, keep the stored archive
    p = repos.package(name)
    listed = False
    if p:
        if not can_manage(repos, u, p):
            raise HTTPException(403, "%s belongs to someone else: only its maintainers or an admin may publish a new version" % name)
        listed = any(v["version"] == ver for v in repos.versions(p["id"]))
        if listed and direct.sha(name, ver) != sha:
            raise HTTPException(409, "%s %s is already published (%s); bump the version" % (
                name, ver, "with different content" if direct.sha(name, ver) else "from git"))
    try:
        fresh = direct.put(tmp, name, ver, sha) if sha != stored else False       # an identical re-publish stores nothing
    except direct_mod.DirectError as e:
        raise HTTPException(e.code, str(e))
    if fresh or not listed:
        ev = {"id": uuid.uuid4().hex, "kind": "publish", "package": name, "version": ver, "ts": time.time(), "client_id": "hub-direct",
              "git_url": direct_mod.URL, "git_branch": "main", "git_subdir": "", "commit": sha, "type": pkg["type"],
              "description": pkg["description"], "tags": pkg["tags"], "user": u["username"]}
        sy.accept([], [ev])                                           # queue it for the index HERE: the ingest flusher may be mid-write, so its own
                                                                      # hand-over to Sync.accept could still be pending when we answer
        try:
            request.app.state.ingest.add([ev], u["username"])         # audit trail + live feed (the same event reaching accept() again is a no-op)
            request.app.state.ingest.flush()
        except OSError:
            raise HTTPException(503, "cannot record the publish right now", headers={"Retry-After": "30"})
        try:
            sy.stage_now()
        except Exception as e:                                         # stays pending and is retried by the sync thread
            log_direct.warning("direct publish of %s %s is not indexed yet: %s", name, ver, e)
    queued = sy.is_pending(name, ver)
    p = repos.package(name)
    if p and not repos.maintainers(p["id"]):
        repos.maintainer_add(p["id"], u["id"], "owner")               # first direct publish of a new package: the publisher owns it
    repos.audit(u["username"], "publish.direct", name, ver)
    request.app.state.cache.invalidate("pkg")
    return {"package": name, "version": ver, "sha256": sha, "size": size, "status": "queued" if queued else ("published" if fresh else "already published")}


# ---------------- telemetry intake (CLI -> hub; git is the CLI's fallback)
MAX_TELEMETRY_BYTES = 4 * 1024 * 1024
MAX_TELEMETRY_EVENTS = 5000


@r.post("/telemetry")
async def telemetry_in(request: Request, u=Depends(current_user)):
    """Usage events from a CLI. Nothing is written to the database here: events wait in memory and one transaction stores a whole window
    (`ingest_db_interval`). 429/503 carry Retry-After; the CLI keeps its spool, backs off and falls back to git."""
    ing = request.app.state.ingest
    ip = request.client.host if request.client else "?"
    ok, wait = ing.allow(ip)
    if not ok:
        return JSONResponse({"detail": "too many requests"}, status_code=429, headers={"Retry-After": str(wait)})
    if ing.full():                                                        # counted in ing.stats["busy"]
        return JSONResponse({"detail": "busy"}, status_code=503, headers={"Retry-After": str(30 + secrets.randbelow(30))})
    n = request.headers.get("content-length")
    if n and n.isdigit() and int(n) > MAX_TELEMETRY_BYTES:
        raise HTTPException(413, "batch too large")
    raw = await request.body()
    if len(raw) > MAX_TELEMETRY_BYTES:
        raise HTTPException(413, "batch too large")
    enc = request.headers.get("content-encoding", "").strip().lower()
    if enc == "gzip":
        try:
            d = zlib.decompressobj(31)                                    # 31 = gzip wrapper; the output cap stops a zip bomb
            raw = d.decompress(raw, MAX_TELEMETRY_BYTES + 1)
            if len(raw) > MAX_TELEMETRY_BYTES or d.unconsumed_tail:
                raise HTTPException(413, "batch too large")
        except zlib.error:
            raise HTTPException(400, "body is not valid gzip")
        ing.stats["gzip"] += 1
    elif enc not in ("", "identity"):
        raise HTTPException(415, "unsupported Content-Encoding")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, "body must be JSON")
    events = body.get("events") if isinstance(body, dict) else None
    if not isinstance(events, list) or len(events) > MAX_TELEMETRY_EVENTS:
        raise HTTPException(400, "send {\"events\": [...]} with at most %d events" % MAX_TELEMETRY_EVENTS)
    try:
        accepted = await run_in_threadpool(ing.add, events, u["username"] if u else None)   # fsync must not block the event loop
    except OSError:                                                       # cannot journal (disk full / read-only): do NOT acknowledge what could be lost
        return JSONResponse({"detail": "cannot store events right now"}, status_code=503, headers={"Retry-After": "30"})
    out = {"accepted": accepted, "received": len(events)}
    try:
        out["index_etag"] = _index_view(request, u)["etag"]               # lets the CLI see its cached index is stale without polling
    except HTTPException:
        pass                                                              # index not ready: the CLI simply keeps its own schedule
    return out


# ---------------- token usage per person and per group (the CLI uploads daily totals after each answer turn)
MAX_USAGE_ROWS = 20000
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_USAGE_INTS = (("requests", "requests"), ("input", "inp"), ("output", "outp"), ("cache_write", "cache_write"), ("cache_read", "cache_read"),
               ("unpriced", "unpriced"))


def _usage_rows(raw):
    """Validate uploaded daily rows; a bad row is dropped, never an error for the rest (hostile or old clients must not break a batch)."""
    out = []
    for x in raw:
        if not isinstance(x, dict) or not _DAY.fullmatch(str(x.get("day") or "")):
            continue
        row = {"day": x["day"], "tool": _clean(x.get("tool"), 32), "model": _clean(x.get("model"), 128)}
        if not row["tool"] or not row["model"]:
            continue
        try:
            for src, dst in _USAGE_INTS:
                v = int(x.get(src) or 0)
                row[dst] = v if 0 <= v < 10 ** 13 else 0
            c = float(x.get("cost") or 0)
            row["cost"] = c if 0 <= c < 1e9 else 0.0
        except (TypeError, ValueError, OverflowError):
            continue
        out.append(row)
    return out


_SESSION_INTS = ("requests", "input", "output", "cache_write", "cache_read", "unpriced", "peak", "cold", "compact", "big", "light")
_SESSION_DST = {"input": "inp", "output": "outp"}


def _usage_sessions(raw):
    """Validate uploaded session summaries (numbers only). A bad one is dropped, never an error for the rest."""
    out = []
    for x in raw if isinstance(raw, list) else []:
        if not isinstance(x, dict) or not re.fullmatch(r"[0-9a-f]{8,64}", str(x.get("sid") or "")) or not _DAY.fullmatch(str(x.get("day") or "")):
            continue
        row = {"sid": x["sid"], "day": x["day"], "tool": _clean(x.get("tool"), 32), "model": _clean(x.get("model"), 128)}
        if not row["tool"] or not row["model"]:
            continue
        try:
            for k in _SESSION_INTS:
                v = int(x.get(k) or 0)
                row[_SESSION_DST.get(k, k)] = v if 0 <= v < 10 ** 13 else 0
            row["started"], row["ended"] = float(x.get("start") or 0), float(x.get("end") or 0)
            c = float(x.get("cost") or 0)
            row["cost"] = c if 0 <= c < 1e9 else 0.0
        except (TypeError, ValueError, OverflowError):
            continue
        out.append(row)
    return out


def usage_user(request: Request, u=Depends(current_user)):
    """A signed-in user whose role holds `view_usage` (Admin > Roles; on for every role by default). Reading usage needs an account."""
    if not u:
        raise HTTPException(401, "authentication required")
    if "view_usage" not in R(request).perms(u["role"]):
        raise HTTPException(403, "missing permission: view_usage")
    return u


def _usage_owner(request, u, body):
    """Whose usage an upload is: the signed-in account (its role needs `view_usage`), else, for a CLI that is not signed in, the hub account
    its OS user name belongs to - same rule as the dashboard's `localize_users` (`Sam` / `sam@corp.com` / `sam` are one person), so that
    person's groups see it. Needs `view_usage` on the anonymous role (on by default). None = nobody to attribute it to."""
    repos = R(request)
    if u:
        if "view_usage" not in repos.perms(u["role"]):
            raise HTTPException(403, "missing permission: view_usage")
        return u
    if "view_usage" not in repos.perms("anonymous"):
        raise HTTPException(401, "authentication required")
    name = _clean(body.get("local_user"), 64) if isinstance(body, dict) else ""
    return repos.user_by_actor(name, repos.localize())


@r.post("/usage/sync")
async def usage_sync(request: Request, u=Depends(current_user)):
    """Daily token totals of one device: {"client_id", "local_user", "rows": [{day, tool, model, requests, input, output, cache_write,
    cache_read, cost, unpriced}]}. A row is the whole day, so sending it again only overwrites it. Signed in: the account's usage. Not signed
    in (permission `view_usage` on the anonymous role): the hub account whose name matches `local_user`, else `{"stored": 0, "matched": false}`."""
    ing, buf = request.app.state.ingest, request.app.state.usagebuf
    ok, wait = ing.allow(request.client.host if request.client else "?")
    if not ok:
        return JSONResponse({"detail": "too many requests"}, status_code=429, headers={"Retry-After": str(wait)})
    if buf.full():                                                        # counted in buf.stats["busy"]
        return JSONResponse({"detail": "busy"}, status_code=503, headers={"Retry-After": str(30 + secrets.randbelow(30))})
    raw = await request.body()
    if len(raw) > MAX_TELEMETRY_BYTES:
        raise HTTPException(413, "batch too large")
    if request.headers.get("content-encoding", "").strip().lower() == "gzip":
        try:
            d = zlib.decompressobj(31)
            raw = d.decompress(raw, MAX_TELEMETRY_BYTES + 1)
            if len(raw) > MAX_TELEMETRY_BYTES or d.unconsumed_tail:
                raise HTTPException(413, "batch too large")
        except zlib.error:
            raise HTTPException(400, "body is not valid gzip")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, "body must be JSON")
    rows = body.get("rows") if isinstance(body, dict) else None
    if not isinstance(rows, list) or len(rows) > MAX_USAGE_ROWS:
        raise HTTPException(400, "send {\"client_id\": ..., \"rows\": [...]} with at most %d rows" % MAX_USAGE_ROWS)
    owner = await run_in_threadpool(_usage_owner, request, u, body)
    if not owner:
        return {"stored": 0, "matched": False, "received": len(rows)}   # nobody to attribute it to: not an error, the CLI must not retry for ever
    client = _clean(body.get("client_id"), 64) or "-"
    good = _usage_rows(rows)
    sess = _usage_sessions(body.get("sessions"))[:MAX_USAGE_ROWS]
    try:
        stored = await run_in_threadpool(buf.add, owner["id"], client, good, sess)  # journal fsync must not block the event loop
    except OSError:                                                       # cannot make it durable: do NOT acknowledge, the CLI retries
        return JSONResponse({"detail": "cannot store usage right now"}, status_code=503, headers={"Retry-After": "30"})
    return {"stored": stored, "matched": True, "received": len(rows) + (len(body.get("sessions")) if isinstance(body.get("sessions"), list) else 0)}


def _usage_window(request, days, date_from, date_to):
    try:
        n, start, since, until = R(request)._window(days, date_from, date_to)
    except ValueError as e:
        raise HTTPException(400, str(e))
    import datetime
    return n, start.isoformat(), (datetime.datetime.fromtimestamp(until - 1, datetime.timezone.utc).date()).isoformat()


@r.get("/usage/groups")
def usage_groups(request: Request, u=Depends(usage_user)):
    """Groups whose usage the caller may see: the ones they belong to (a site admin: all)."""
    repos = R(request)
    return {"groups": repos.user_groups(u["id"], every="admin" in repos.perms(u["role"]))}


@r.get("/usage/sessions")
def usage_sessions(request: Request, scope: str = "me", group: str = "", days: int = 30, tool: str = None, model: str = None, user: str = None,
                   limit: int = 100, date_from: str = Query(None, alias="from"), date_to: str = Query(None, alias="to"), u=Depends(usage_user)):
    """Sessions (biggest cost first, with peak context, cold rebuilds and so on) and usage hints for the same scope and filters as /usage."""
    ids, mine = _usage_scope(request, u, scope, group)
    if scope == "groups" and not ids:
        return {"sessions": [], "hints": [], "totals": {}}
    n, lo, hi = _usage_window(request, days, date_from, date_to)
    limit = max(1, min(int(limit), 500))
    _usage_fresh(request, u, ids)
    key = "usage:s:%s:%s" % (u["id"], "|".join(str(x) for x in (scope, ids, lo, hi, tool, model, user, limit)))
    return C(request).get_or_set(key, 15, lambda: R(request).usage_sessions(lo, hi, uid=u["id"], group_ids=ids, tool=tool or None, model=model or None,
                                                                            user=(user or None) if scope == "groups" else None, limit=limit))


def _usage_fresh(request, u, ids):
    """Read-your-writes: write what is still buffered for the people being looked at (the caller, or the members of the chosen groups)."""
    buf = request.app.state.usagebuf
    if ids is None:
        buf.flush_for([u["id"]])
    elif buf.pending():
        who = {m for g in ids for m in R(request).group_member_ids(g)}
        buf.flush_for(who | {u["id"]})


def _usage_scope(request, u, scope, group):
    """-> (group ids or None for 'me', the caller's groups). 403 for a group the caller is not in (a site admin may see all)."""
    if scope not in ("me", "groups"):
        raise HTTPException(400, "scope must be me or groups")
    if scope == "me":
        return None, []
    repos = R(request)
    mine = repos.user_groups(u["id"], every="admin" in repos.perms(u["role"]))
    allowed = {g["id"] for g in mine}
    try:
        asked = [int(x) for x in group.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "group must be a comma list of group ids")
    if any(g not in allowed for g in asked):
        raise HTTPException(403, "you can only see groups you belong to")
    return sorted(asked or allowed), mine


@r.get("/usage")
def usage_report(request: Request, scope: str = "me", group: str = "", days: int = 30, tool: str = None, model: str = None, user: str = None,
                 date_from: str = Query(None, alias="from"), date_to: str = Query(None, alias="to"), u=Depends(usage_user)):
    """scope=me: the caller's own usage (all devices). scope=groups: everybody in the chosen groups (`group` = comma list of group ids; blank = all
    the caller's groups), each person counted once even when in several of them, with by_group / by_user / by_model. Filters: tool, model, user."""
    if scope not in ("me", "groups"):
        raise HTTPException(400, "scope must be me or groups")
    repos = R(request)
    n, lo, hi = _usage_window(request, days, date_from, date_to)
    ids = None
    mine = []
    if scope == "groups":
        mine = repos.user_groups(u["id"], every="admin" in repos.perms(u["role"]))
        allowed = {g["id"] for g in mine}
        try:
            asked = [int(x) for x in group.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(400, "group must be a comma list of group ids")
        if any(g not in allowed for g in asked):
            raise HTTPException(403, "you can only see groups you belong to")
        ids = sorted(asked or allowed)
        if not ids:
            return {"scope": scope, "groups": [], "filters": {"from": lo, "to": hi, "days": n}, "empty": True}
    _usage_fresh(request, u, ids)
    key = "usage:%s:%s" % (u["id"], "|".join(str(x) for x in (scope, ids, lo, hi, tool, model, user)))
    res = C(request).get_or_set(key, 15, lambda: repos.usage_query(lo, hi, uid=u["id"], group_ids=ids, tool=tool or None, model=model or None,
                                                                   user=(user or None) if scope == "groups" else None))
    return dict(res, scope=scope, groups=mine, selected=ids or [], filters={"from": lo, "to": hi, "days": n})


# ---------------- the package index, served by the hub (CLI asks here first; git is the fallback). ETag + 304.
def _index_view(request, u):
    """{"etag","rows","h","entries"} of the index as THIS viewer may see it, built from the database. Cached per viewer scope; every package
    change drops it ('pkg' prefix), so the database is only queried after a change and an unchanged index answers 304 from the cache."""
    v = viewer(request, u)
    def build():
        view = R(request).index_view(v)
        rows = view["rows"]
        etag = '"%s"' % hashlib.sha256(",".join(x["name"] + x["h"] for x in rows).encode()).hexdigest()[:20]
        return {"etag": etag, "rows": rows, "h": {x["name"]: x["h"] for x in rows}, "entries": view["entries"]}
    return C(request).get_or_set("pkg:idx:%s" % _cache_scope(v), 3600, build)


def _not_modified(request, etag):
    inm = request.headers.get("if-none-match", "")
    return etag in [x.strip() for x in inm.split(",")] or inm.strip() == "*"


@r.get("/index/catalog")
def index_catalog(request: Request, u=Depends(current_user)):
    need_login_unless(request, u, "public_browse")
    view = _index_view(request, u)
    hdr = {"ETag": view["etag"], "Cache-Control": "private, max-age=0, must-revalidate"}
    if _not_modified(request, view["etag"]):
        return Response(status_code=304, headers=hdr)
    return JSONResponse({"etag": view["etag"], "count": len(view["rows"]), "packages": view["rows"]}, headers=hdr)


@r.get("/index/packages/{name}")
def index_entry(name: str, request: Request, u=Depends(current_user)):
    need_login_unless(request, u, "public_browse")
    name = naming.normalize(name)
    view = _index_view(request, u)
    h = view["h"].get(name)
    if h is None:
        raise HTTPException(404, "package not found")                  # same answer for "private" and "missing"
    etag = '"%s"' % h
    hdr = {"ETag": etag, "Cache-Control": "private, max-age=0, must-revalidate"}
    if _not_modified(request, etag):
        return Response(status_code=304, headers=hdr)
    return JSONResponse(view["entries"][name], headers=hdr)


MAX_ELSEWHERE = 5          # other locations tried for an old commit that is not where the index says (git hosts are rate limited)


def found_error(e):
    return getattr(e, "tried_note", None) or str(e)


def _transfer_elsewhere(request, name, version, repo, err):
    """The commit of an old version is not in the repo the index names (the package moved since). Try the other places this package was
    ever published from, newest first. On success the place is recorded for that version (next time it is direct, and the index the
    CLIs read shows it). -> (tar path, rev, how) or None; `err.tried_note` then names what was tried."""
    repos = R(request)
    tr = request.app.state.transfer
    tried = [repo]
    for loc in repos.known_locations(name):
        if len(tried) > MAX_ELSEWHERE:
            break
        if any(pkgindex.same_repo(loc, t) for t in tried) or direct_mod.is_direct(loc):
            continue
        cand = dict(loc, ref=repo["ref"])
        tried.append(cand)
        try:
            got = tr.lookup(cand)
        except TransferError:
            continue
        if repos.version_set_repo(name, version, loc):
            sy = request.app.state.sync
            try:
                sy.stamp_index({(name, version): loc})
            except Exception:
                pass
            sy.invalidate_served()
            request.app.state.cache.invalidate("pkg")
        return got
    if len(tried) > 1:
        err.tried_note = gitx.add_note(str(err), "also tried: " + "; ".join(
            "%s%s" % (gitx.redact_url(t.get("url")), " (folder %s)" % t["subdir"] if t.get("subdir") else "") for t in tried[1:]))
    return None


@r.get("/index/packages/{name}/transfer/{version}")
def index_transfer(name: str, version: str, request: Request, u=Depends(user_or_anon)):
    """`aihub install -i`: the hub fetches the package from git (cached; a monorepo once for all its packages) and sends a tar.gz.
    A package published with `aihub dev publish -i` has no git at all: its stored archive is sent, to everyone who may see it (git was never
    needed to install a public package, so neither is a permission here). Everything else needs the `install_indirect` permission."""
    name = naming.normalize(name)
    entry = _index_view(request, u)["entries"].get(name)
    if not entry:
        raise HTTPException(404, "package not found")
    if version not in [v["version"] for v in entry.get("versions") or []] + [entry.get("latest_version")]:
        raise HTTPException(404, "version not found")
    v = next((x for x in entry.get("versions") or [] if x["version"] == version), {})
    repo = dict(entry.get("repo") or {}, **(v.get("repo") or {}))
    if v.get("ref"):
        repo["ref"] = v["ref"]
    if direct_mod.is_direct(repo):
        d = request.app.state.direct
        sha = d.sha(name, version)
        if not sha:
            raise HTTPException(404, "the hub no longer holds the archive of %s %s" % (name, version))
        return FileResponse(d.path(name, version), media_type="application/gzip", filename="%s-%s.tar.gz" % (name, version),
                            headers={"X-Aihub-Rev": sha, "X-Aihub-Cache": "direct", "X-Aihub-Sha256": sha, "ETag": '"%s"' % sha})
    if "install_indirect" not in R(request).perms(u["role"]):
        raise HTTPException(401 if u["role"] == "anonymous" else 403, "authentication required" if u["role"] == "anonymous" else "missing permission: install_indirect")
    tr = request.app.state.transfer
    try:
        path, rev, how = tr.lookup(repo)
    except TransferError as e:
        found = _transfer_elsewhere(request, name, version, repo, e) if repo.get("ref") and gitx.is_missing(e) else None
        if not found:
            raise HTTPException(502, "cannot fetch %s: %s" % (name, found_error(e)), headers={"Retry-After": "30"})
        path, rev, how = found
    return FileResponse(path, media_type="application/gzip", filename="%s-%s.tar.gz" % (name, version), headers={"X-Aihub-Rev": rev, "X-Aihub-Cache": how, "ETag": '"%s"' % rev})


# ---------------- client config (what the CLI pulls to get set up)
@r.get("/client-config")
def client_config(request: Request):
    """Settings the CLI needs: where the package index and the telemetry inbox are. Locations only, never credentials
    (git access stays with the user's own git credential helper). Never cached."""
    repos = R(request)
    env = request.app.state.settings.env_get
    from ..core import defaults
    dx = defaults.load().get("index", {})
    idx_url = env("AIHUB_INDEX_URL") or repos.setting("index_url", "") or dx.get("git_url", "")
    out = {"index": {"url": idx_url,
                     "branch": env("AIHUB_INDEX_BRANCH") or repos.setting("index_branch", "") or dx.get("branch", "main"),
                     "path": env("AIHUB_INDEX_PATH") or repos.setting("index_path", "") or dx.get("path", "index")},
           "telemetry": {"url": env("AIHUB_TELEMETRY_URL") or repos.setting("telemetry_url", "")
                         or defaults.load().get("telemetry", {}).get("git_url", "") or idx_url,
                         "inbox": env("AIHUB_TELEMETRY_INBOX_BRANCH") or repos.setting("telemetry_inbox_branch", "")
                         or defaults.load().get("telemetry", {}).get("inbox_branch", "") or "telemetry-inbox",
                         "branch": env("AIHUB_TELEMETRY_BRANCH") or repos.setting("telemetry_branch", "")
                         or defaults.load().get("telemetry", {}).get("branch", "") or "telemetry-archive"},
           "refresh_hours": int(repos.setting("client_refresh_hours", "") or 24),
           "hub": {"telemetry": True, "index": True,                                  # the hub takes events and serves the index; git is the CLI's fallback
                   "telemetry_interval": request.app.state.sync.num("client_telemetry_interval"),
                   "index_ttl": request.app.state.sync.num("client_index_ttl")}}
    from fastapi.responses import JSONResponse
    return JSONResponse(out, headers={"Cache-Control": "no-store"})
