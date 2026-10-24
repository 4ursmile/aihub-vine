"""Folder package index: small files that stay small as the registry grows. Standard library only (CLI and server share it).

  index/root.json                  {"format": 2, "count": N, "catalog": [{"file", "rows"}...]}
  index/catalog/0001.json ...      {"cols": [...], "rows": [[...]]}   search data only, CATALOG_ROWS packages per file
  index/packages/<xx>/<name>.json  the full entry of one package; xx = first two hex digits of sha1(name)

A lookup by name opens exactly one file (the path is computed, nothing is scanned). Search reads the catalog chunks and never the
entries. A publish rewrites one entry file and one catalog chunk, so git history and diffs stay small. The old single
index.json ({"packages": [...]}) is still read (Legacy) and can be converted with `python -m aihub.core.pkgindex migrate`.

Entry = {"name", "type", "description", "tags", "authors"?, "license"?, "latest_version", "readme"?, "repo": {"url","branch","subdir"?},
         "python"?, "versions": [{"version", "ref"?, "repo"?, "requires"?: {"os","commands","packages"}, "manifest"?}]}
Empty values are left out. `requires` lives on each version (most have none), never on the entry.
Entry `repo` = where the LATEST version lives. A version that was published from somewhere else (the package moved to another repo,
branch or subfolder later) carries its own full `repo` {"url","branch","subdir"} (`subdir` stays "" when the entry's is not), so an old
`ref` is looked for where it exists. A version that lives where the entry says carries none (entries of packages that never moved
are byte for byte what they always were). Readers merge: dict(entry["repo"], **version["repo"]).
"""
import argparse
import hashlib
import json
import os
import re
import sys

from . import naming

FORMAT = 2
CATALOG_ROWS = 500
DESC_MAX = 200
COLS = ["name", "type", "latest", "desc", "tags", "h"]


# ----------------------------------------------------------------------------------------------- layout
def shard(name):
    return hashlib.sha1(name.encode()).hexdigest()[:2]


def entry_rel(name):
    return "packages/%s/%s.json" % (shard(name), name)


def url_key(url):
    """Same repository, same key: host case, credentials, a trailing "/" and a trailing ".git" do not matter."""
    u = re.sub(r"//[^/@]+@", "//", str(url or "")).strip()
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*://)([^/]*)(.*)$", u)
    if m:
        u = m.group(1).lower() + m.group(2).lower() + m.group(3)
    u = u.rstrip("/")
    return u[:-4].rstrip("/") if u.lower().endswith(".git") else u


def repo_key(r):
    r = r or {}
    return (url_key(r.get("url")), str(r.get("branch") or "main"), str(r.get("subdir") or "").strip("/"))


def same_repo(a, b):
    return repo_key(a) == repo_key(b)


def location(r):
    """The three fields that say where a version lives, with defaults filled in (branch "main", subdir "")."""
    r = r or {}
    return {"url": r.get("url") or "", "branch": r.get("branch") or "main", "subdir": str(r.get("subdir") or "").strip("/")}


_LOC = {"url", "branch", "subdir"}


def _clean(v):
    if isinstance(v, dict):
        out = {k: _clean(x) for k, x in v.items()}
        return {k: x for k, x in out.items() if x not in ("", None, [], {})}
    if isinstance(v, list):
        return [_clean(x) for x in v]
    return v


def dumps(obj, indent=None):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=indent,
                      separators=None if indent else (",", ":")) + "\n"


def _write(path, text):
    """Write only when the bytes differ, so an unchanged file never shows up in git."""
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    return True


# ----------------------------------------------------------------------------------------------- reading
def _row(r):
    return {"name": r[0], "type": r[1], "latest_version": r[2], "description": r[3], "tags": r[4], "h": r[5]}


class Reader:
    """A folder index on disk (a git checkout of the index repo, or any directory)."""

    def __init__(self, base):
        self.base = base
        self._root = None

    def _load(self, rel):
        with open(os.path.join(self.base, rel), encoding="utf-8") as f:
            return json.load(f)

    @property
    def root(self):
        if self._root is None:
            self._root = self._load("root.json")
        return self._root

    @property
    def count(self):
        return int(self.root.get("count", 0))

    def catalog(self):
        """One light dict per package (name, type, latest_version, description, tags, h): enough for search and listings."""
        out = []
        for c in self.root.get("catalog", []):
            out += [_row(r) for r in self._load(c["file"])["rows"]]
        return out

    def entry(self, name):
        try:
            return self._load(entry_rel(naming.normalize(name)))
        except FileNotFoundError:
            return None

    def entries(self):
        for row in self.catalog():
            e = self.entry(row["name"])
            if e:
                yield e


class Legacy:
    """The old single-file index, behind the same calls."""

    def __init__(self, data):
        self._by = {naming.normalize(p["name"]): p for p in (data or {}).get("packages", [])}
        self.count = len(self._by)

    def catalog(self):
        return sorted(self._by.values(), key=lambda p: p["name"])

    def entry(self, name):
        return self._by.get(naming.normalize(name))

    def entries(self):
        return iter(self.catalog())


def open_source(base, path="index"):
    """Reader for `path` inside the directory `base`: a folder index, or (for old repos) an index.json file."""
    path = path or "index"
    folder = os.path.join(base, path[:-5] if path.endswith(".json") else path)      # "index.json" (old default) -> the "index" folder
    if os.path.isfile(os.path.join(folder, "root.json")):
        return Reader(folder)
    for legacy in (os.path.join(base, path), os.path.join(base, "index.json")):
        if os.path.isfile(legacy):
            with open(legacy, encoding="utf-8") as f:
                return Legacy(json.load(f))
    raise FileNotFoundError("no package index at %s (looked for %s/root.json and index.json)" % (base, path or "index"))


# ----------------------------------------------------------------------------------------------- writing
def _requires_of(entry, v):
    m = (v.get("manifest") or entry.get("manifest") or {}).get("requires") or {}
    return dict(m, **(entry.get("requires") or {}), **(v.get("requires") or {}))


def normalize_entry(e):
    """Legacy/hand-written entry -> compact stored form (entry-level requires folded into each version)."""
    e = dict(e)
    e["name"] = naming.normalize(e["name"])
    seen, versions = set(), []
    for v in e.get("versions") or ([{"version": e["latest_version"]}] if e.get("latest_version") else []):
        if v["version"] in seen:
            continue
        seen.add(v["version"])
        v = dict(v)
        v["requires"] = _requires_of(e, v)
        v.pop("manifest", None)               # planning data only; the aihub.toml in the repo is authoritative at install time
        versions.append(v)
    er = e.get("repo") if isinstance(e.get("repo"), dict) else {}
    explicit = []                                  # versions whose own repo keeps an empty subdir (the entry's is not empty)
    for v in versions:
        vr = v.get("repo")
        explicit.append(False)
        if not isinstance(vr, dict) or not vr:
            v.pop("repo", None)
        elif set(vr) <= _LOC and same_repo(dict(er, **vr), er):
            v.pop("repo")                          # it lives where the entry says: nothing to store
        elif vr.get("url"):
            vr = dict(vr, **location(dict(er, **vr)))      # a whole location: old readers merge it over the entry's, so no field may be missing
            explicit[-1] = vr["subdir"] == "" and bool(str(er.get("subdir") or "").strip("/"))
            v["repo"] = vr
    e["versions"] = versions
    for k in ("requires", "manifest", "rating"):
        e.pop(k, None)
    e["tags"] = [str(t).lower() for t in e.get("tags") or []]
    out = _clean(e)
    for v, keep in zip(out.get("versions") or [], explicit):
        if keep and isinstance(v.get("repo"), dict):
            v["repo"]["subdir"] = ""               # _clean drops "", and an absent subdir would be read as the entry's
    return out


def write_entry(out, entry):
    e = normalize_entry(entry)
    _write(os.path.join(out, entry_rel(e["name"])), dumps(e))
    return e


def rebuild(out):
    """Regenerate catalog chunks and root.json from the entry files, and drop catalog files that are no longer used."""
    rows = []
    pk = os.path.join(out, "packages")
    for d in sorted(os.listdir(pk)) if os.path.isdir(pk) else []:
        for f in sorted(os.listdir(os.path.join(pk, d))):
            if not f.endswith(".json"):
                continue
            raw = open(os.path.join(pk, d, f), "rb").read()
            e = json.loads(raw)
            rows.append([e["name"], e.get("type", ""), e.get("latest_version", ""), (e.get("description") or "")[:DESC_MAX],
                         e.get("tags", []), hashlib.sha256(raw).hexdigest()[:12]])
    rows.sort(key=lambda r: r[0])
    chunks = []
    for i in range(0, len(rows), CATALOG_ROWS):
        rel = "catalog/%04d.json" % (i // CATALOG_ROWS + 1)
        _write(os.path.join(out, rel), dumps({"cols": COLS, "rows": rows[i:i + CATALOG_ROWS]}))
        chunks.append({"file": rel, "rows": len(rows[i:i + CATALOG_ROWS]), "first": rows[i][0]})
    keep = {c["file"] for c in chunks}
    cdir = os.path.join(out, "catalog")
    for f in os.listdir(cdir) if os.path.isdir(cdir) else []:
        if "catalog/" + f not in keep:
            os.remove(os.path.join(cdir, f))
    digest = hashlib.sha256("".join(r[0] + r[5] for r in rows).encode()).hexdigest()[:16]    # changes whenever any package changes
    root = {"format": FORMAT, "count": len(rows), "digest": digest, "catalog": chunks,
            "layout": "packages/<sha1(name)[:2]>/<name>.json", "catalog_cols": COLS}
    _write(os.path.join(out, "root.json"), dumps(root, indent=1))
    return len(rows)


def migrate(src_json, out):
    """Old index.json -> folder index. Safe to re-run."""
    with open(src_json, encoding="utf-8") as f:
        pkgs = json.load(f).get("packages", [])
    names = set()
    for p in pkgs:
        names.add(write_entry(out, p)["name"])
    pk = os.path.join(out, "packages")
    for d in os.listdir(pk) if os.path.isdir(pk) else []:        # entries that were removed from the old file
        for f in os.listdir(os.path.join(pk, d)):
            if f.endswith(".json") and f[:-5] not in names:
                os.remove(os.path.join(pk, d, f))
        if not os.listdir(os.path.join(pk, d)):
            os.rmdir(os.path.join(pk, d))
    return rebuild(out)


README_MAX = 20000
README_NAMES = ("README.md", "readme.md", "Readme.md", "README.markdown", "README")


def add(out, project, url, branch="main", subdir="", ref="", readme=False):
    """Add or update one package version from its aihub.toml (what CI runs after a publish)."""
    from . import manifest as M, version as V
    with open(os.path.join(project, "aihub.toml"), encoding="utf-8") as f:
        m = M.parse(f.read())
    p = m["package"]
    name = naming.normalize(p["name"])
    path = os.path.join(out, entry_rel(name))
    try:
        with open(path, encoding="utf-8") as f:
            e = json.load(f)
    except FileNotFoundError:
        e = {}
    here = {"url": url, "branch": branch, "subdir": subdir}
    old = e.get("repo") if isinstance(e.get("repo"), dict) and e["repo"].get("url") else None
    versions = []
    for v in e.get("versions", []):
        if v["version"] == p["version"]:
            continue
        v = dict(v)
        if old and not (isinstance(v.get("repo"), dict) and v["repo"].get("url")):
            v["repo"] = location(dict(old, **(v.get("repo") or {})))      # it lives where the entry said until now; the entry is about to change
        versions.append(v)
    versions.append({"version": p["version"], "ref": ref, "requires": m["requires"], "repo": here})
    versions.sort(key=lambda v: [int(x) if x.isdigit() else 0 for x in v["version"].replace("-", ".").split(".")])
    latest = V.latest([v["version"] for v in versions])
    # the entry says where the LATEST version lives (adding an old version again must not move it); normalize_entry drops the
    # per-version repo of every version that lives there
    at = next((v for v in versions if v["version"] == latest), versions[-1])
    e.update(name=name, type=p["type"], description=p["description"], tags=p["tags"], authors=p["authors"], license=p["license"],
             latest_version=latest, versions=versions, repo=dict(at.get("repo") or here))
    if readme and e["latest_version"] == p["version"]:
        for n in README_NAMES:                       # the README of the newest version, read from the checkout (no web request)
            try:
                with open(os.path.join(project, n), encoding="utf-8", errors="replace") as f:
                    e["readme"] = f.read(README_MAX)
                break
            except OSError:
                continue
    if m.get("python", {}).get("requires") or m.get("python", {}).get("requirements_file"):
        e["python"] = m["python"]
    write_entry(out, e)
    return rebuild(out)


def set_version_repos(out, repos):
    """{(name, version): {"url","branch","subdir"}} -> record where those versions really live (repair of entries written before versions
    carried their own repo). A version that lives where the entry says ends up with none. -> number of entry files changed."""
    by = {}
    for (name, ver), loc in repos.items():
        by.setdefault(naming.normalize(name), {})[str(ver)] = loc
    changed = 0
    for name, vs in by.items():
        path = os.path.join(out, entry_rel(name))
        try:
            with open(path, encoding="utf-8") as f:
                e = json.load(f)
        except (OSError, ValueError):
            continue
        for v in e.get("versions") or []:
            if str(v.get("version")) in vs:
                v["repo"] = location(vs[str(v["version"])])
        changed += bool(_write(path, dumps(normalize_entry(e))))
    if changed:
        rebuild(out)
    return changed


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m aihub.core.pkgindex", description="Build and maintain a folder package index.")
    sp = ap.add_subparsers(dest="cmd", required=True)
    m = sp.add_parser("migrate", help="Convert an old index.json into a folder index.")
    m.add_argument("src")
    m.add_argument("out", nargs="?", default="index")
    a = sp.add_parser("add", help="Add or update one package version from its aihub.toml.")
    a.add_argument("out")
    a.add_argument("project")
    a.add_argument("--url", required=True)
    a.add_argument("--branch", default="main")
    a.add_argument("--subdir", default="")
    a.add_argument("--ref", default="", help="tag or commit that holds this version")
    r = sp.add_parser("rebuild", help="Regenerate root.json and the catalog from the entry files.")
    r.add_argument("out", nargs="?", default="index")
    x = ap.parse_args(argv)
    n = {"migrate": lambda: migrate(x.src, x.out), "rebuild": lambda: rebuild(x.out),
         "add": lambda: add(x.out, x.project, x.url, x.branch, x.subdir, x.ref)}[x.cmd]()
    print("%d package(s) in the index" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
