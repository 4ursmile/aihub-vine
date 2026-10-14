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
import tarfile
import time
import urllib.request

from ..core import manifest as M, naming, pkgindex, version as V
from . import api, gitx, paths, remote_config, tls, ui

TTL = 300       # in-process cache of the reader; the on-disk checkout is checked against the remote far more often (CHECK)
CHECK = 20      # seconds a checkout counts as current without asking the remote. After that ONE `ls-remote` (cheap, no download)
                # says whether the branch moved; only then is it fetched. So a new publish shows up within seconds, not 5 minutes.


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


class HubReader:
    """The package index as the hub serves it (GET /index/catalog, /index/packages/<name>). Every answer is cached on disk with its ETag and
    revalidated with If-None-Match, so an unchanged index costs one tiny 304 and no body. Quacks like pkgindex.Reader/Legacy."""

    def __init__(self, base):
        self.base = base
        self._cat = None
        self.count = 0

    def _path(self, *a):
        return os.path.join(self.base, *a)

    def _get(self, path, cache_file, force=False):
        """-> parsed JSON. Sends If-None-Match from the cached copy; 304 reuses it. Raises ApiError/OSError when the hub cannot answer."""
        import urllib.error
        cached = None
        try:
            with open(cache_file, encoding="utf-8") as f:
                cached = json.load(f)
        except (OSError, ValueError):
            pass
        h = {"Accept": "application/json", "User-Agent": "aihub-cli"}
        tok = paths.load("credentials.json", {}).get("token")
        if tok:
            h["Authorization"] = "Bearer " + tok
        if cached and cached.get("etag") and not force:
            h["If-None-Match"] = cached["etag"]
        rq = urllib.request.Request(paths.config()["hub"].rstrip("/") + "/api/v1" + path, headers=h)
        try:
            with api._opener.open(rq, timeout=8) as r:
                data = json.loads(r.read())
                etag = r.headers.get("ETag") or ""
        except urllib.error.HTTPError as e:
            if e.code == 304 and cached:
                os.utime(cache_file, None)                       # still current: restart the trust window
                return cached["data"]
            raise OSError("hub index %d" % e.code)
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        tmp = cache_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"etag": etag, "data": data}, f)
        os.replace(tmp, cache_file)
        return data

    def load(self, ttl, force=False):
        f = self._path("catalog.json")
        if not force and os.path.isfile(f) and time.time() - os.path.getmtime(f) < ttl:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)["data"]                      # inside the trust window: no request at all
        else:
            data = self._get("/index/catalog", f, force)
        self._cat = data.get("packages", [])
        self.count = len(self._cat)
        return self

    def catalog(self):
        return list(self._cat or [])

    def entry(self, name):
        name = naming.normalize(name)
        if not any(p["name"] == name for p in self._cat or []):
            return None
        f = self._path("packages", name + ".json")
        row = next(p for p in self._cat if p["name"] == name)
        try:
            with open(f, encoding="utf-8") as fh:
                c = json.load(fh)
            if c.get("etag") == '"%s"' % row.get("h") and c.get("data"):
                return c["data"]                                  # the catalog hash says this entry did not change: no request
        except (OSError, ValueError):
            pass
        try:
            return self._get("/index/packages/" + name, f, True)
        except OSError as e:
            if "404" in str(e):
                return None
            raise

    def entries(self):
        for row in self.catalog():
            e = self.entry(row["name"])
            if e:
                yield e


def hub_ttl():
    try:
        return max(5.0, float(os.environ.get("AIHUB_INDEX_TTL") or paths.config().get("hub_index_ttl") or CHECK))
    except (TypeError, ValueError):
        return float(CHECK)


def _hub_reader(src, refresh=False):
    """The index from the hub, or None when the hub cannot serve it (down, old hub, needs a login we lack). The caller then uses git."""
    if (os.environ.get("AIHUB_INDEX_VIA") or paths.config().get("index_via") or "hub").lower() == "git":
        return None
    if os.environ.get("AIHUB_INDEX_URL") or not paths.hub_configured():
        return None                                            # the user pointed the CLI at a specific index: honor it
    try:
        return HubReader(paths.ensure("index-hub")).load(hub_ttl(), force=refresh)
    except (OSError, ValueError, api.ApiError, KeyError):
        return None


def _reader(src, refresh=False):
    """A pkgindex reader for the configured source. Git sources are fetched at most every TTL seconds; if the network is down
    the last checkout is used (a stale index beats none)."""
    u = src["url"]
    hub = _hub_reader(src, refresh)
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
    fresh = not refresh and os.path.isfile(stamp) and time.time() - os.path.getmtime(stamp) < CHECK
    if not fresh and not refresh and os.path.isfile(stamp):
        head = gitx.remote_head(u, src["branch"], timeout=15)
        if head and head == gitx.run(["rev-parse", "-q", "--verify", "HEAD"], cwd=d, check=False):
            open(stamp, "w").close()                    # remote unchanged: no fetch needed
            fresh = True
    if not fresh:
        try:
            d = remote_config.with_access(u, lambda: gitx.fetch(u, src["branch"], base=base))
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
    if refresh or key not in _cache or time.time() - _cache[key][0] > hub_ttl():
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


INDIRECT = False        # `aihub install -i`: the hub fetches from git and sends the package


def _from_hub(r, dest):
    """Download the package as tar.gz from the hub (it caches git, and a monorepo is fetched once for all its packages)."""
    import tempfile
    from ..core import archive
    tmp = tempfile.mkdtemp()
    try:
        part = os.path.join(tmp, "pkg.tar.gz")
        with api._req("GET", "/index/packages/%s/transfer/%s" % (r["name"], r["version"]), timeout=300) as resp, open(part, "wb") as f:
            rev = resp.headers.get("X-Aihub-Rev") or ""
            shutil.copyfileobj(resp, f)
        src = archive.safe_extract(part, os.path.join(tmp, "x"))
        if not os.path.isfile(os.path.join(src, "aihub.toml")):
            raise api.ApiError("%s: the hub sent no aihub.toml" % r["name"])
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(src, dest)
        return rev
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def checkout(r, dest):
    """Materialise a resolved package into dest (its subdir of the repo at the requested ref/branch)."""
    if INDIRECT:
        try:
            return _from_hub(r, dest)
        except api.ApiError as e:
            if str(e).startswith(("401", "403")):
                raise
            ui.note("hub transfer failed for %s (%s); using git" % (r["name"], e))
        except (OSError, ValueError, tarfile.TarError) as e:
            ui.note("hub transfer failed for %s (%s); using git" % (r["name"], e))
    repo = r["repo"]
    try:
        src = remote_config.with_access(repo["url"], lambda: gitx.fetch(repo["url"], repo.get("branch") or "main", repo.get("ref")))
    except gitx.GitError as e:
        raise api.ApiError("%s: %s" % (r["name"], e))
    rev = gitx.run(["rev-parse", "HEAD"], cwd=src)
    src = os.path.join(src, repo.get("subdir") or "")
    if not os.path.isfile(os.path.join(src, "aihub.toml")):
        raise api.ApiError("%s: no aihub.toml in %s" % (r["name"], gitx.redact_url(repo["url"])))
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git"))
    return rev
