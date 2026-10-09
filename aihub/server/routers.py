import json
import os
from typing import Optional
import re
import tempfile
import time

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
from ..core import archive, manifest as M, naming, redact, version as V
from .sync import parse_schedule
from . import builtin, image, markdown, nethost, safehtml, sheet
from .categories import category_of
from .db import ALL_PERMS, ANON_LOCKED
from .deps import can_develop, can_manage, current_user, require_any, require_perm, require_user, viewer_of
from .security import hash_password, hash_token, new_token, verify_password

r = APIRouter(prefix="/api/v1")
TTL = 20


_fails = {}  # (ip, key) -> (count, window_start); per-process throttle for login / password checks


def R(request): return request.app.state.repos


# admin setting key -> environment variable that overrides it (environment / .env wins; the UI shows "set by environment")
CLI_GIT_ENV = {"cli_git_url": "AIHUB_CLI_GIT_URL", "cli_git_branch": "AIHUB_CLI_GIT_BRANCH", "cli_git_subdir": "AIHUB_CLI_GIT_SUBDIR"}
SYNC_ENV = dict(CLI_GIT_ENV, langfuse_host="LANGFUSE_BASE_URL", langfuse_public_key="LANGFUSE_PUBLIC_KEY",
                langfuse_secret_key="LANGFUSE_SECRET_KEY", index_url="AIHUB_INDEX_URL", index_branch="AIHUB_INDEX_BRANCH",
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
    return "/api/v1/users/%s/avatar?v=%d" % (username, v) if v else None


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
        out["maintainers"] = [_public(m) | {"role": m["role"]} for m in repos.maintainers(p["id"])]
        out["versions"] = [{k: v[k] for k in ("version", "sha256", "size", "downloads", "yanked", "created")}
                           | {"source": source_url((v["manifest"] or {}).get("repo"))} for v in vs]
    return out


def branding(request):
    """Public, admin-editable identity of this hub: name, contact details, logo."""
    rp = R(request)
    v = rp.setting("logo_v")
    theme = rp.setting("theme_color")
    return {"name": rp.setting("site_name") or "AI Hub", "theme": theme if theme in THEME_COLORS else "blue", "logo_url": "/api/v1/logo?v=%s" % v if v else None,
            "contact": {k[8:]: rp.setting(k) for k in TEXT_SETTINGS if k.startswith("contact_") and rp.setting(k)},
            "announcement": safehtml.clean(rp.setting("announcement") or "")}


@r.get("/meta")
def meta(request: Request, u=Depends(current_user)):
    b = branding(request)
    return {"name": b["name"], "theme": b["theme"], "logo_url": b["logo_url"], "contact": b["contact"], "announcement": b.get("announcement", ""), "public_url": public_url(request),
            "anonymous_review": "review" in R(request).perms("anonymous"),
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
    return "pub" if not v else ("all" if v["bypass"] else "u%d" % v["id"])


@r.get("/packages")
def packages(request: Request, q: str = "", type: str = None, tag: str = None, sort: str = "", category: str = None,
             page: int = 1, per_page: int = 20, mine: bool = False, u=Depends(current_user)):
    need_login_unless(request, u, "public_browse")
    if sort not in ("", "relevance", "updated", "created", "name", "downloads", "rating", "reviews"):
        raise HTTPException(400, "sort must be one of: relevance, updated, created, name, downloads, rating, reviews")
    v = viewer(request, u)
    per_page = min(max(per_page, 1), 100)
    key = "pkg:list:%s:%s|%s|%s|%s|%s|%s|%s|%s" % (_cache_scope(v), q, type, tag, sort, page, per_page, mine, category)
    hit = C(request).get(key)
    if hit:
        return hit
    total, rows = R(request).packages(q, type, tag, sort, page, per_page, viewer=v, mine=mine, category=category)
    res = {"total": total, "page": page, "per_page": per_page, "items": [_ser(request, p) for p in rows]}
    C(request).set(key, res, TTL)
    return res


@r.get("/facets")
def facets(request: Request, u=Depends(current_user)):
    need_login_unless(request, u, "public_browse")
    v = viewer(request, u)
    c, key = C(request), "pkg:facets:" + _cache_scope(v)
    hit = c.get(key)
    if not hit:
        hit = R(request).facets(v)
        c.set(key, hit, TTL)
    return hit


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
    hit = C(request).get(key)
    if hit:
        return hit
    items = []
    for n in R(request).related(p["name"], v):
        q = R(request).package(n)
        if q:
            items.append(_ser(request, q))
    res = {"items": items}
    C(request).set(key, res, TTL)
    return res


@r.get("/activity")
def activity(request: Request, u=Depends(current_user)):
    """Anonymised recent install/use/update feed for the home page. No usernames or client ids ever leave this endpoint."""
    need_login_unless(request, u, "public_browse")
    v = viewer(request, u)
    key = "pkg:activity:" + _cache_scope(v)
    hit = C(request).get(key)
    if hit:
        return hit
    res = {"items": R(request).recent_activity(v, 15)}
    C(request).set(key, res, 5)
    return res


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
GROUP_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,39}$")


def _can_group(request, u, g):
    return "admin" in R(request).perms(u["role"]) or g.get("owner_id") == u["id"]


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
    out = []
    for g in allg:
        owned = g.get("owner_id") == u["id"]
        if is_admin or owned or g["name"] in mine:
            # `can_manage` is decided here, on the server; owner ids never reach the browser
            out.append({k: v for k, v in g.items() if k != "owner_id"} | {"can_manage": is_admin or owned})
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
    return {"name": g["name"], "description": g["description"], "members": [dict(_public(m)) for m in R(request).group_members(g["id"])]}


@r.put("/groups/{name}")
def group_update(name: str, body: dict, request: Request, u=Depends(require_user)):
    g = _group_or_404(request, name, u)
    R(request).group_update(g["id"], _clean(body.get("description", ""), 200))
    return {"ok": True}


@r.delete("/groups/{name}")
def group_delete(name: str, request: Request, u=Depends(require_user)):
    g = _group_or_404(request, name, u)
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
    R(request).group_member_add(g["id"], [f["id"] for f in found if f])
    R(request).audit(u["username"], "group.members.add", name, ",".join(str(n) for n in names[:20]))
    C(request).invalidate("pkg")
    return {"ok": True, "missing": missing}


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

    # Groups must already exist, and the caller must be allowed to add people to them. Nothing is auto-created.
    group_ids = repos.group_ids_by_name({x for it in items for x in it["groups"]})

    def work():
        seen, plan, result = set(), [], []
        for it in items:
            row = dict(it, password="", status="", groups=", ".join(it["groups"]))
            unknown = [x for x in it["groups"] if x not in group_ids]
            if not re.match(r"^[A-Za-z0-9_.-]{2,40}$", it["username"]):
                row["status"] = "invalid username"
            elif it["role"] not in roles:
                row["status"] = "unknown role"
            elif unknown:
                row["status"] = "unknown group: " + ", ".join(unknown)
            elif it["username"].lower() in seen:
                row["status"] = "duplicate in file"
            else:
                seen.add(it["username"].lower())
                row["password"] = gen_password()
                plan.append(row)
            result.append(row)
        made = repos.users_create_many([(x["username"], hash_password(x["password"]), x["role"], x["display_name"], x["title"],
                                         [group_ids[n] for n in (y.strip() for y in x["groups"].split(",")) if n]) for x in plan])
        for x in plan:
            if x["username"] in made:
                x["status"] = "created"
            else:
                x["status"], x["password"] = "skipped: username exists", ""
        return result, len(made)

    result, created = await run_in_threadpool(work)
    repos.audit(u["username"], "users.batch", "", "created=%d rows=%d groups=%d" % (created, len(items), len(group_ids)))
    if group_ids:
        C(request).invalidate("pkg")   # group membership changes who can see private repositories
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["username", "display_name", "title", "role", "groups", "password", "status"])
    for x in result:
        w.writerow([sheet.safe_cell(x["username"]), sheet.safe_cell(x["display_name"]), sheet.safe_cell(x["title"]),
                    x["role"], sheet.safe_cell(x["groups"]), x["password"], x["status"]])
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": 'attachment; filename="new-accounts.csv"', "Cache-Control": "no-store",
        "X-Created": str(created), "X-Skipped": str(len(result) - created)})


audit_dep = Depends(require_perm("audit"))


@r.get("/admin/audit")
def a_audit(request: Request, u=audit_dep): return {"audit": R(request).audit_list()}


@r.get("/audit")
def audit_search(request: Request, source: str = "tools", q: str = "", actor: str = "", action: str = "", package: str = "", kind: str = "",
                 days: float = 7, start: float = 0, end: float = 0, page: int = 1, per_page: int = 50, u=audit_dep):
    """Security audit with filters. `source=tools` = agent tool calls / installs; `source=admin` = account, role and setting changes."""
    if source not in ("tools", "admin"):
        raise HTTPException(400, "source must be tools or admin")
    if kind and kind not in ("install", "update", "uninstall", "use", "error"):
        raise HTTPException(400, "bad kind")
    request.app.state.events.flush()
    now = time.time()
    since = start or (now - days * 86400 if days > 0 else 0)
    per_page = max(1, min(per_page, 200))
    res = R(request).audit_search(source, q.strip()[:200], actor, action.strip()[:200], package, kind, since, end, per_page, (max(page, 1) - 1) * per_page)
    return dict(res, page=max(page, 1), per_page=per_page, actors=R(request).audit_actors()[source])


@r.get("/audit/export")
def audit_export(request: Request, source: str = "tools", q: str = "", actor: str = "", action: str = "", package: str = "", kind: str = "",
                 days: float = 7, start: float = 0, end: float = 0, u=audit_dep):
    if source not in ("tools", "admin"):
        raise HTTPException(400, "source must be tools or admin")
    request.app.state.events.flush()
    now = time.time()
    since = start or (now - days * 86400 if days > 0 else 0)
    res = R(request).audit_search(source, q.strip()[:200], actor, action.strip()[:200], package, kind, since, end, 5000, 0)
    cols = (["ts", "actor", "action", "target", "detail"] if source == "admin" else
            ["ts", "actor", "kind", "package", "version", "component", "source", "host", "local_user", "ip", "cwd", "detail"])
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
        out.append({"slug": slug, "title": first or slug, "raw_url": "/api/v1/docs/%s/raw" % slug})
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
    if kind and kind not in _DASH_KINDS:
        raise HTTPException(400, "unknown event kind")
    if identity and identity not in ("anonymous", "signed-in"):
        raise HTTPException(400, "identity must be anonymous or signed-in")


@r.get("/dashboard")
def dashboard(request: Request, days: int = 30, package: str = None, type: str = None, user: str = None, source: str = None,
              kind: str = None, identity: str = None, host: str = None, q: str = None, date_from: str = Query(None, alias="from"),
              date_to: str = Query(None, alias="to"), u=Depends(require_perm("view_dashboard"))):
    _dash_args(days, kind, identity, date_from, date_to)
    v = viewer(request, u)
    if package:
        package = _pkg_or_404(request, package, u)["name"]      # also enforces that the viewer may see it
    request.app.state.events.flush()
    key = "dash:%s:%s" % (_cache_scope(v), "|".join(str(x) for x in (days, package, type, user, source, kind, identity, host, q, date_from, date_to)))
    hit = C(request).get(key)
    if hit:
        return hit
    try:
        res = R(request).dashboard(days, package, viewer=v, type=type, user=user, source=source, kind=kind,
                                   identity=identity, host=host, q=(q or "").strip()[:80] or None, date_from=date_from, date_to=date_to)
    except ValueError as e:
        raise HTTPException(400, str(e))
    C(request).set(key, res, 15)
    return res


@r.get("/dashboard/events")
def dashboard_events(request: Request, days: int = 30, page: int = 1, per_page: int = 25, package: str = None, type: str = None,
                     user: str = None, source: str = None, kind: str = None, identity: str = None, host: str = None, q: str = None,
                     date_from: str = Query(None, alias="from"), date_to: str = Query(None, alias="to"),
                     u=Depends(require_perm("view_dashboard"))):
    _dash_args(days, kind, identity, date_from, date_to)
    v = viewer(request, u)
    if package:
        package = _pkg_or_404(request, package, u)["name"]
    request.app.state.events.flush()
    try:
        return R(request).dashboard_events(v, days, date_from, date_to, page, per_page, type=type, package=package, user=user,
                                           source=source, kind=kind, identity=identity, host=host, q=(q or "").strip()[:80] or None)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------------- sync (Langfuse + git index -> SQLite)
SYNC_KEYS = ("langfuse_host", "langfuse_public_key", "langfuse_secret_key", "index_url", "index_branch", "index_path", "sync_interval",
             "share_credentials", "enroll_code", "cli_git_url", "cli_git_branch", "cli_git_subdir", "client_refresh_hours")
SECRET_KEYS = ("langfuse_secret_key", "enroll_code")


def _check_cli_git(k, v):
    """Branch and sub folder end up inside a pip spec (git+URL@branch#subdirectory=dir), so reject anything that would break it."""
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in v):
        raise HTTPException(400, f"{k} must not contain spaces or control characters")
    if k == "cli_git_subdir" and (any(ch in v for ch in "#?") or ".." in v or v.startswith("/")):
        raise HTTPException(400, "cli_git_subdir must be a relative folder without '#', '?', '..' or a leading '/'")


@r.get("/admin/sync")
def sync_status(request: Request, u=admin):
    sy, repos = request.app.state.sync, R(request)
    cfg = {k: repos.setting(k, "") for k in SYNC_KEYS}
    for k in SECRET_KEYS:
        cfg[k] = "set" if cfg[k] else ""                                         # never echo secrets
    cfg["sync_interval"] = cfg["sync_interval"] or "60"
    return {"config": cfg, "status": dict(sy.status, next_in=round(sy.delay())), "cursor": repos.setting("sync_cursor"),
            "from_env": {"langfuse": bool(request.app.state.settings.env_get("LANGFUSE_SECRET_KEY")),
                         "index": bool(request.app.state.settings.env_get("AIHUB_INDEX_URL")),
                         **{k: bool(request.app.state.settings.env_get(e)) for k, e in SYNC_ENV.items()}}}


@r.put("/admin/sync")
def sync_update(body: dict, request: Request, u=admin):
    repos = R(request)
    for k in SYNC_KEYS:
        if k in body and body[k] is not None:
            v = str(body[k]).strip()
            if k in SECRET_KEYS and v in ("", "set"):
                continue                                 # blank/echo = keep the stored secret
            if k == "share_credentials" and v not in ("0", "1"):
                raise HTTPException(400, "share_credentials must be 0 or 1")
            if k in ("cli_git_branch", "cli_git_subdir"):
                _check_cli_git(k, v)
            if k == "sync_interval":
                try:
                    parse_schedule(v)
                except ValueError as e:
                    raise HTTPException(400, str(e))
            repos.set_setting(k, v)
    repos.audit(u["username"], "settings.sync", "", ",".join(k for k in SYNC_KEYS if k in body))
    request.app.state.sync.wake.set()                    # re-plan with the new schedule now
    return sync_status(request, u)


BACKUP_KEYS = ("backup_enabled", "backup_schedule", "backup_events_chunk")


def _backup_view(request, u):
    bk, repos = request.app.state.backup, R(request)
    return {"config": {"backup_enabled": "1" if bk.enabled() else "0", "backup_schedule": bk.schedule_text(), "backup_events_chunk": str(bk.chunk())},
            "status": dict(bk.status, next_in=round(bk.delay()))}


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
            repos.set_setting(k, v)
    repos.audit(u["username"], "settings.backup", "", ",".join(k for k in BACKUP_KEYS if k in body))
    request.app.state.backup.wake.set()
    return _backup_view(request, u)


@r.post("/admin/backup/run")
def backup_run(request: Request, u=admin):
    """Back up now (to the index git repo), whether or not the schedule is enabled."""
    try:
        request.app.state.backup.run_once()
    except Exception as e:
        raise HTTPException(502, "backup failed: %s" % str(e)[:300])
    R(request).audit(u["username"], "backup.run", "", "")
    return _backup_view(request, u)


@r.post("/admin/sync/run")
def sync_run(request: Request, full: bool = False, u=admin):
    """Poll now. full=true forgets the cursor and looks back over everything (safe: events are deduplicated)."""
    repos = R(request)
    if full:
        repos.set_setting("sync_cursor", "")
        repos.set_setting("sync_index_dirty", "1")
        repos.set_setting("sync_index_hashes", "")
    try:
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


# ---------------- client config (what the CLI pulls to get set up)
@r.get("/client-config")
def client_config(request: Request, code: str = "", u=Depends(current_user)):
    """Settings the CLI needs. The index location is public. Langfuse credentials are shared only when an admin
    turned sharing on, and only with a signed-in user or a caller that presents the enrollment code. Served over TLS;
    never cached."""
    repos = R(request)
    env = request.app.state.settings.env_get
    from ..core import defaults
    dx = defaults.load().get("index", {})
    out = {"index": {"url": env("AIHUB_INDEX_URL") or repos.setting("index_url", "") or dx.get("git_url", ""),
                     "branch": env("AIHUB_INDEX_BRANCH") or repos.setting("index_branch", "") or dx.get("branch", "main"),
                     "path": env("AIHUB_INDEX_PATH") or repos.setting("index_path", "") or dx.get("path", "index")},
           "refresh_hours": int(repos.setting("client_refresh_hours", "") or 24), "credentials": None}
    if repos.setting("share_credentials", "0") == "1":
        want = repos.setting("enroll_code", "")
        import hmac
        if u or (want and hmac.compare_digest(want.encode(), (code or "").encode())):
            out["credentials"] = {            # Langfuse only: git credentials are never stored on or sent by the server
                "langfuse": {"host": env("LANGFUSE_BASE_URL") or repos.setting("langfuse_host", "") or defaults.load().get("langfuse", {}).get("host", ""),
                             "public_key": env("LANGFUSE_PUBLIC_KEY") or repos.setting("langfuse_public_key", ""),
                             "secret_key": env("LANGFUSE_SECRET_KEY") or repos.setting("langfuse_secret_key", "")}}
            repos.audit(u["username"] if u else "enroll-code", "client-config.credentials", "", request.client.host if request.client else "")
        elif want:
            out["credentials_hint"] = "enrollment code required"
    from fastapi.responses import JSONResponse
    return JSONResponse(out, headers={"Cache-Control": "no-store"})
