"""Package registry backed by a folder index in git (see core/pkgindex.py; the old single index.json still works).

Index entry = the package row derived from its manifest, plus where its files live:
  {"name", "type", "description", "tags", "latest_version", "readme",
   "requires": {"os": ["macos","linux","windows"], "commands": [str | {"name","hint"}], "packages": ["dep>=1"]},
   "python": {"requires": [str], "requirements_file": str},
   "versions": [{"version", "requires"?, "manifest"?, "ref"?}],   # per-version values override the entry's
   (the aihub.toml in the repo is authoritative at install time; the index copy is for planning/filtering)
   "repo": {"url", "branch", "subdir"?}}                    # any repo, any branch
"""
import json
import os
import shutil
import time
import urllib.request

from ..core import manifest as M, naming, pkgindex, version as V
from . import api, gitx, paths, tls

CHECK = 300     # seconds the index counts as current without asking anyone (hub or git), env AIHUB_INDEX_TTL. After that the hub is asked
                # with an ETag (304 = unchanged, nothing downloaded); only if the hub cannot be reached is git asked: ONE `ls-remote`,
                # and the branch is fetched only when it moved. 5 minutes keeps a fleet of CLIs from hammering either.
HUB_RETRY = 60  # seconds to leave an unreachable hub alone before trying it again (git is used meanwhile)


def _defaults():
    from ..core import defaults
    return defaults.load()["index"]


def source():
    """Where the index lives. Lookup order: environment > ~/.aihub/config.json (`aihub config set index_url|index_branch|index_path`)
    > aihub/core/defaults.json. index_url may be a git URL, an http(s) URL to the json itself, or a local file."""
    d, c = _defaults(), paths.config()
    return {"url": os.environ.get("AIHUB_INDEX_URL") or c.get("index_url") or d.get("git_url") or "",
            "branch": os.environ.get("AIHUB_INDEX_BRANCH") or c.get("index_branch") or d.get("branch") or "main",
            "path": os.environ.get("AIHUB_INDEX_PATH") or c.get("index_path") or d.get("path") or "index"}


def ttl():
    try:
        return max(0.0, float(os.environ.get("AIHUB_INDEX_TTL") or paths.config().get("index_ttl") or CHECK))
    except (TypeError, ValueError):
        return float(CHECK)


def _hub_index(refresh=False):
    """The package index as the hub serves it (GET /index), cached in ~/.aihub/index-hub.json. Within the TTL no request is made at all;
    after it, one conditional request (If-None-Match): 304 keeps the cache and restarts the TTL, 200 replaces it. -> a Legacy reader,
    or None when the hub cannot be asked (not configured, down, refused): the caller then falls back to git."""
    if os.environ.get("AIHUB_INDEX_FROM") == "git" or paths.config().get("index_from") == "git":
        return None
    hub = paths.config()["hub"].rstrip("/")
    cache = paths.load("index-hub.json", {})
    mine = cache.get("hub") == hub and isinstance(cache.get("data"), dict)
    now = time.time()
    if mine and not refresh and now - cache.get("at", 0) < ttl():
        return pkgindex.Legacy(cache["data"])
    if not refresh and now - cache.get("failed", 0) < HUB_RETRY:
        return None                                       # the hub was down a moment ago: do not wait for it again on every command
    try:
        rq = api._req("GET", "/index", headers={"If-None-Match": cache["etag"]} if mine and cache.get("etag") else None, timeout=8)
        with rq as r:
            etag, data = r.headers.get("ETag", ""), json.loads(r.read() or b"{}")
        if not isinstance(data.get("packages"), list):
            raise api.ApiError("hub returned no index")
        paths.save("index-hub.json", {"hub": hub, "at": now, "etag": etag, "data": data})
        return pkgindex.Legacy(data)
    except api.ApiError as e:
        if str(e).startswith("304") and mine:
            cache["at"] = now
            cache.pop("failed", None)
            paths.save("index-hub.json", cache)
            return pkgindex.Legacy(cache["data"])
        cache["failed"] = now
        paths.save("index-hub.json", cache)
        return None
    except (OSError, ValueError):
        cache["failed"] = now
        paths.save("index-hub.json", cache)
        return None


def _reader(src, refresh=False):
    """A pkgindex reader for the configured source. Git sources are fetched at most every TTL seconds; if the network is down
    the last checkout is used (a stale index beats none)."""
    u = src["url"]
    if not os.path.exists(u or "-"):
        hub = _hub_index(refresh)                         # a local file/folder index is explicit: never replaced by the hub's
        if hub is not None:
            return hub
    if not u:
        raise api.ApiError("no package index configured; run: aihub config set index_url <git url>")
    if os.path.isfile(u):
        with open(u) as f:
            return pkgindex.Legacy(json.load(f))
    if os.path.isdir(u):
        return pkgindex.open_source(u, src["path"])
    if u.startswith(("http://", "https://")) and u.rstrip("/").endswith(".json"):
        try:
            with urllib.request.urlopen(u, timeout=20, context=tls.context()) as r:
                data = json.loads(r.read())
            paths.save("index-cache.json", data)
        except (OSError, ValueError):
            if not os.path.isfile(paths.p("index-cache.json")):
                raise
            data = json.load(open(paths.p("index-cache.json")))
        return pkgindex.Legacy(data)
    base = paths.ensure("repos", "_index")        # own folder: a package checkout of the same repo (pinned commit) must not replace the index
    d = gitx.cache_dir(u, base)
    stamp = os.path.join(d, ".aihub-index-fetched")
    fresh = not refresh and os.path.isfile(stamp) and time.time() - os.path.getmtime(stamp) < ttl()
    if not fresh and not refresh and os.path.isfile(stamp):
        head = gitx.remote_head(u, src["branch"], timeout=15)
        if head and head == gitx.run(["rev-parse", "-q", "--verify", "HEAD"], cwd=d, check=False):
            open(stamp, "w").close()                    # remote unchanged: no fetch needed
            fresh = True
    if not fresh:
        try:
            d = gitx.fetch(u, src["branch"], base=base)
            open(stamp, "w").close()
        except gitx.GitError as e:
            if gitx.remote_empty(u):
                return pkgindex.Legacy({})              # brand-new empty repo: a valid, empty index (the first publish fills it)
            if not os.path.isfile(stamp):               # never fetched successfully: the local folder is empty, so say why the fetch failed
                raise api.ApiError("cannot fetch package index %s (branch '%s'): %s%s" % (
                    gitx.redact_url(u), src["branch"], gitx.redact_url(str(e)), tls.hint(e)))
    try:
        return pkgindex.open_source(d, src["path"])
    except (OSError, ValueError) as e:
        raise api.ApiError("cannot read package index %s (branch '%s', folder '%s'): %s" % (gitx.redact_url(u), src["branch"], src["path"], e))


_cache = {}


def reader(refresh=False):
    src = source()
    key = (src["url"], src["branch"], src["path"])
    if refresh or key not in _cache or time.time() - _cache[key][0] > ttl():
        _cache[key] = (time.time(), _reader(src, refresh))
    return _cache[key][1]


def count(refresh=False):
    return reader(refresh).count


def get(name):
    p = reader().entry(name)
    if not p:
        raise api.ApiError("package '%s' is not in the index (aihub search)" % name)
    return p


def search(term=""):
    """Matches on name, description and tags using the catalog only (no per-package files are opened)."""
    words = (term or "").lower().split()
    out = []
    for p in reader().catalog():
        hay = " ".join([p["name"], p.get("description") or "", " ".join(p.get("tags") or [])]).lower()
        if all(w in hay for w in words):
            out.append(p)
    return sorted(out, key=lambda p: p["name"])


def versions(p):
    vs = [v["version"] for v in p.get("versions") or []]
    return vs or ([p["latest_version"]] if p.get("latest_version") else [])


def pick(p, spec=""):
    ok = [v for v in versions(p) if V.satisfies(v, spec or "")]
    if not ok:
        raise api.ApiError("no version of %s satisfies '%s' (have: %s)" % (p["name"], spec, ", ".join(versions(p)) or "none"))
    return V.latest(ok)


def _manifest_of(p, ver, checkout=None):
    for v in p.get("versions") or []:
        if v["version"] == ver and v.get("manifest"):
            return v["manifest"]
    if checkout:
        return M.parse(open(os.path.join(checkout, "aihub.toml"), encoding="utf-8").read())
    return p.get("manifest") or {}


def requires_of(p, ver):
    """{os, commands, packages} for a version, from the index (per-version overrides entry-level, then manifest)."""
    v = next((v for v in p.get("versions") or [] if v["version"] == ver), {})
    m = (v.get("manifest") or p.get("manifest") or {}).get("requires") or {}
    r = dict(m, **(p.get("requires") or {}), **(v.get("requires") or {}))
    return {"os": list(r.get("os") or []), "commands": list(r.get("commands") or []), "packages": list(r.get("packages") or [])}


def current_os():
    import platform
    return {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}.get(platform.system(), "linux")


def _repo_of(p, ver):
    """Where a version lives: the entry's repo, overridden per version (monorepos, tags, pinned commits)."""
    v = next((v for v in p.get("versions") or [] if v["version"] == ver), {})
    repo = dict(p.get("repo") or {}, **(v.get("repo") or {}))
    if v.get("ref"):
        repo["ref"] = v["ref"]
    return repo


def resolve(name, spec=""):
    p = get(name)
    ver = pick(p, spec)
    return {"name": naming.normalize(p["name"]), "version": ver, "manifest": _manifest_of(p, ver),
            "requires": requires_of(p, ver), "type": p.get("type"),
            "repo": _repo_of(p, ver),
            "sha256": ""}


def plan(roots, installed=None):
    """Dependency-first list of resolved packages. Constraints from every dependent are combined, so a package
    shared by two others gets one version that satisfies both; a conflict names who required what.
    Installed packages that already satisfy their constraints are left out."""
    have = {naming.normalize(k): str(v) for k, v in (installed or {}).items()}
    need, chosen, queue, rootset = {}, {}, [], set()

    def add(name, spec, by):
        n = naming.normalize(str(name))
        need.setdefault(n, []).append((str(spec or "").strip(), by))
        queue.append(n)
        return n

    for x in roots:
        rootset.add(add(x["name"], x.get("spec"), None))
    steps = 0
    while queue:
        steps += 1
        if steps > 5000:
            raise api.ApiError("dependency graph is too large")
        name = queue.pop(0)
        spec = ",".join(sp for sp, _ in need[name] if sp)
        cur = chosen.get(name)
        if cur and V.satisfies(cur["version"], spec):
            continue
        if name not in rootset and name in have and V.satisfies(have[name], spec):
            chosen[name] = {"keep": True, "version": have[name]}
            continue
        p = get(name)
        ok = [v for v in versions(p) if V.satisfies(v, spec)]
        if not ok:
            by = ", ".join(sorted({b for _, b in need[name] if b})) or "your request"
            raise api.ApiError("no version of %s satisfies '%s' (required by %s)" % (name, spec or "*", by))
        ver = V.latest(ok)
        for k in need:                  # a re-pick drops the constraints the previous pick contributed
            need[k] = [(sp, b) for sp, b in need[k] if b != name]
        r = resolve(name, "==" + ver)
        if r["requires"]["os"] and current_os() not in r["requires"]["os"]:
            raise api.ApiError("%s supports only: %s" % (name, ", ".join(r["requires"]["os"])))
        chosen[name] = r
        for dep in r["requires"]["packages"]:
            dn, ds = V.split_spec(dep)
            add(dn, ds, name)
    out, done = [], set()

    def visit(n):
        c = chosen.get(n)
        if n in done or not c or c.get("keep"):
            return
        done.add(n)
        for dep in c["requires"]["packages"]:
            visit(naming.normalize(V.split_spec(dep)[0]))
        out.append(dict(c, root=n in rootset))

    for n in sorted(rootset):
        visit(n)
    return out


def checkout(r, dest):
    """Materialise a resolved package into dest (its subdir of the repo at the requested ref/branch)."""
    repo = r["repo"]
    try:
        src = gitx.fetch(repo["url"], repo.get("branch") or "main", repo.get("ref"))
    except gitx.GitError as e:
        raise api.ApiError("%s: %s" % (r["name"], e))
    rev = gitx.run(["rev-parse", "HEAD"], cwd=src)
    src = os.path.join(src, repo.get("subdir") or "")
    if not os.path.isfile(os.path.join(src, "aihub.toml")):
        raise api.ApiError("%s: no aihub.toml in %s" % (r["name"], gitx.redact_url(repo["url"])))
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git"))
    return rev


def checkout_via_hub(r, dest):
    """Like checkout(), but the hub fetches from git (and caches) and sends the files: for machines that cannot reach the git host.
    Needs the `indirect` permission. -> the commit the hub built it from ('' when unknown)."""
    import tarfile
    import tempfile
    from urllib.parse import quote
    from ..core import archive
    td = tempfile.mkdtemp()
    try:
        part = os.path.join(td, "bundle.tar.gz")
        sha = {}

        def get():
            with api._req("GET", "/packages/%s/versions/%s/bundle" % (quote(r["name"]), quote(r["version"])), timeout=300) as resp:
                sha["v"] = resp.headers.get("X-Bundle-Sha256") or ""
                with open(part, "wb") as f:
                    shutil.copyfileobj(resp, f, 1 << 16)
        try:
            api._retry(get)
        except api.ApiError as e:
            if str(e).startswith("403"):
                raise api.ApiError("%s: your role may not install through the hub (permission 'indirect'); ask an admin or drop -i" % r["name"])
            raise
        if sha["v"] and archive.sha256_file(part) != sha["v"]:
            raise api.ApiError("%s: download from the hub is corrupt (sha256 mismatch); try again" % r["name"])
        shutil.rmtree(dest, ignore_errors=True)
        archive.safe_extract(part, dest)
        if not os.path.isfile(os.path.join(dest, "aihub.toml")):
            raise api.ApiError("%s: the hub sent no aihub.toml" % r["name"])
        return (r.get("repo") or {}).get("ref") or ""
    finally:
        shutil.rmtree(td, ignore_errors=True)
