"""Monorepo support for `aihub dev`: one git repository, several packages, each handled on its own.

inspect()   look at every package: its manifest, the repository it really lives in, what [git] says, nested repositories
fix_git()   make a package's [git] (url / branch / subdir) match its working repository
show_tree() print the workspace as a tree with a status per package
solve()     resolve nested repositories (a package folder with its own .git inside another repository)
ensure_repo() give a new workspace folder a repository and a remote
"""
import os
import shutil
import time

from ..core import manifest as M
from . import gitx, installer, paths, publish as pub, ui

FIXABLE = ("stale-url", "stale-branch", "stale-subdir")
INFO = ("no-repo",)                                  # worth showing, not a problem: `aihub dev publish` creates the repository
GIT_KEYS = ("url", "branch", "subdir")


def _arrow():
    return "→" if ui.U else "->"


def _real(p):
    return os.path.realpath(p)


def _rel(path, base):
    r = os.path.relpath(_real(path), _real(base))
    return "" if r == "." else r.replace(os.sep, "/")


def _same_url(a, b):
    return pub._norm_url(a) == pub._norm_url(b)


# ------------------------------------------------------------------------------------------------ inspect
def nested_repos(pkg, boundary):
    """Folders from `pkg` upward (stopping below `boundary`) that hold their own .git while another repository exists above them.
    Such a folder is seen by the outer repository as an opaque gitlink, so the outer repo cannot publish the package's files."""
    pkg, stop = os.path.abspath(pkg), os.path.abspath(boundary)
    found, cur = [], pkg
    while True:
        if cur == stop and cur != pkg:
            break
        if os.path.isdir(os.path.join(cur, ".git")) and pub.toplevel(os.path.dirname(cur)):
            found.append(cur)
        if cur == stop or os.path.dirname(cur) == cur:
            break
        cur = os.path.dirname(cur)
    return found


def inspect_package(d, root):
    rec = {"dir": d, "rel": _rel(d, root) or os.path.basename(d), "name": os.path.basename(d), "version": "", "type": "",
           "declared": {}, "actual": {}, "top": None, "nested": [], "problems": []}
    try:
        with open(os.path.join(d, "aihub.toml"), encoding="utf-8") as f:
            m = M.parse(f.read())
    except (OSError, ValueError, KeyError) as e:
        rec["problems"].append(("manifest", "aihub.toml cannot be read: %s" % e))
        return rec
    p = m["package"]
    rec.update(name=p["name"], version=p["version"], type=p["type"], declared=dict(m["git"]))
    top = pub.toplevel(d)
    rec["top"] = top
    rec["nested"] = nested_repos(d, root)
    if not top:
        rec["problems"].append(("no-repo", "not in a git repository yet"))
    else:
        url, branch = gitx.remote_info(top)
        rec["actual"] = {"url": gitx.redact_url(url or ""), "branch": branch or "", "subdir": _rel(d, top)}
        if not url:
            rec["problems"].append(("no-remote", "the repository has no remote 'origin' (git remote add origin <url>, or aihub dev doctor --remote <url>)"))
        if not branch:
            rec["problems"].append(("detached", "HEAD is detached; check out a branch"))
        decl, act = rec["declared"], rec["actual"]
        if url and not (decl.get("url") and _same_url(decl["url"], url)):
            rec["problems"].append(("stale-url", "[git] url is %s but the remote is %s" % (decl.get("url") or "not set", act["url"])))
        if branch and decl.get("branch") != branch:
            rec["problems"].append(("stale-branch", '[git] branch is %s but the repository is on "%s"'
                                    % ('"%s"' % decl["branch"] if decl.get("branch") else "not set", branch)))
        if (decl.get("subdir") or "") != act["subdir"]:
            rec["problems"].append(("stale-subdir", '[git] subdir is "%s" but the package lives in "%s"' % (decl.get("subdir") or "", act["subdir"])))
    for n in rec["nested"]:
        rec["problems"].append(("nested", "%s has its own .git inside another repository" % (_rel(n, root) or ".")))
    return rec


def inspect(root, dirs):
    recs = [inspect_package(d, root) for d in dirs]
    seen = {}
    for r in recs:
        if r["version"]:
            first = seen.setdefault(r["name"], r)
            if first is not r:
                r["problems"].append(("duplicate", 'name "%s" is also used by %s; package names must be unique' % (r["name"], first["rel"])))
    return recs


def blocking(rec):
    """Problems `aihub dev doctor` cannot fix by itself."""
    return [(c, t) for c, t in rec["problems"] if c not in FIXABLE and c not in INFO]


# ------------------------------------------------------------------------------------------------ fix
def fix_git(d, root, remote=None, write=True):
    """Make aihub.toml's [git] match the working repository. -> (record after the fix, ["key: old -> new", ...]).
    With write=False nothing is changed; the list says what would be."""
    top = pub.toplevel(d)
    if remote and top and write:
        has, _ = gitx.remote_info(top)
        gitx.run(["remote", "set-url" if has else "add", "origin", remote], cwd=top)
    rec = inspect_package(d, root)
    act = rec["actual"]
    if any(c == "manifest" for c, _ in rec["problems"]) or not act.get("url"):
        return rec, []
    old = dict(rec["declared"])
    new = {"url": act["url"], "branch": act["branch"] or old.get("branch", ""), "subdir": act["subdir"]}
    changes = []
    for k in GIT_KEYS:
        same = _same_url(old.get(k) or "", new[k]) if k == "url" and old.get(k) else (old.get(k) or "") == new[k]
        if not same:
            changes.append("%s: %s %s %s" % (k, old.get(k) or "(not set)", _arrow(), new[k] or '""'))
    if changes and write:
        pub.write_git_table(os.path.join(d, "aihub.toml"), new)
        rec = inspect_package(d, root)
    return rec, changes


# ------------------------------------------------------------------------------------------------ tree
def _brief(rec):
    bad = blocking(rec)
    fix = [c for c, _ in rec["problems"] if c in FIXABLE]
    if bad:
        return ui.err("✗ " if ui.U else "x ") + bad[0][1].split(" (")[0]
    if fix:
        return ui.warn("! " ) + "[git] out of date (%s)" % ", ".join(c[6:] for c in fix)
    if any(c == "no-repo" for c, _ in rec["problems"]):
        return ui.dim("new: no repository yet")
    return ui.ok("✓ " if ui.U else "ok ") + "in sync"


def show_tree(root, recs):
    """Print the workspace tree and a problem list. -> number of packages with something to fix."""
    root = os.path.abspath(root)
    single = len(recs) == 1 and os.path.abspath(recs[0]["dir"]) == root
    by_dir = {r["dir"]: r for r in recs}

    def pkg_label(r):
        extra = []
        if r["nested"]:
            extra.append(ui.warn("own .git"))
        a = r["actual"]
        if single and a.get("url"):
            extra.append(ui.dim("%s @ %s" % (a["url"], a["branch"] or "detached")))
        return "  ".join([ui.bold(r["name"]), ui.dim("%s %s" % (r["version"], r["type"]) if r["version"] else "?"),
                          ui.dim(r["rel"]) if r["rel"] != r["name"] and not single else ""] + extra + [_brief(r)]).replace("    ", "  ")

    def label(k):
        if k in by_dir:
            return pkg_label(by_dir[k])
        top = pub.toplevel(root)
        url, branch = gitx.remote_info(top) if top else (None, None)
        where = "%s @ %s" % (gitx.redact_url(url) or "no remote", branch or "detached") if top else "not a git repository yet"
        return ui.bold(os.path.basename(root) or root) + "  " + ui.dim("monorepo · " + where if top and _real(top) == _real(root) else where)

    lines = installer.tree_lines([root] if not single else [recs[0]["dir"]],
                                 lambda k: [] if single or k != root else [r["dir"] for r in recs], label)
    ui.out()
    for ln in lines:
        ui.out("  " + ln)
    problems = [(r, c, t) for r in recs for c, t in r["problems"] if c not in INFO]
    if problems:
        ui.out()
        ui.out("  " + ui.bold("To fix"))
        for r, c, t in problems:
            ui.out("   %s %s: %s" % (ui.warn("!"), r["name"], t))
        ui.out()
        if any(c in FIXABLE for _, c, _ in problems):
            ui.info("update [git] to match the repository:   aihub dev doctor")
        if any(c == "nested" for _, c, _ in problems):
            ui.info("resolve nested repositories:             aihub dev tree-solve")
    return len({id(r) for r, _, _ in problems})


# ------------------------------------------------------------------------------------------------ solve
def _tracked(top, rel):
    return bool(gitx.run(["ls-files", "-s", "--", rel], cwd=top, check=False))


def resolve_nested(path, mode):
    """mode 'merge': fold the folder into the outer repository (its .git moves to ~/.aihub/backups, nothing is lost).
    mode 'keep': leave it as its own repository and make the outer one ignore it. -> one-line result."""
    parent_top = pub.toplevel(os.path.dirname(path))
    rel = _rel(path, parent_top)
    if mode == "merge":
        dest = os.path.join(paths.ensure("backups"), "git-%s-%d" % (os.path.basename(path), int(time.time())))
        shutil.move(os.path.join(path, ".git"), dest)
        if _tracked(parent_top, rel):                       # the outer repo recorded it as an opaque gitlink: track the files instead
            gitx.run(["rm", "-q", "--cached", "-f", "--", rel], cwd=parent_top)
        return "%s now belongs to the outer repository; its old history is kept in %s" % (rel, dest)
    exclude = os.path.join(parent_top, gitx.run(["rev-parse", "--git-path", "info/exclude"], cwd=parent_top))
    os.makedirs(os.path.dirname(exclude), exist_ok=True)
    line = "/%s/" % rel
    have = open(exclude, encoding="utf-8").read().splitlines() if os.path.exists(exclude) else []
    if line not in have:
        with open(exclude, "a", encoding="utf-8", newline="\n") as f:
            f.write(("\n" if have and have[-1] else "") + "# aihub: %s is its own repository\n%s\n" % (rel, line))
    if _tracked(parent_top, rel):
        gitx.run(["rm", "-q", "--cached", "-f", "--", rel], cwd=parent_top)
    return "%s stays its own repository; the outer repository now ignores it" % rel


def solve(root, dirs, mode=None, remote=None):
    """Resolve nested repositories (asking when `mode` is None and a person is at the terminal), then refresh every [git].
    -> (resolved, unresolved)."""
    recs = inspect(root, dirs)
    groups = {}
    for r in recs:
        for n in r["nested"]:
            groups.setdefault(n, []).append(r["name"])
    resolved = unresolved = 0
    for path, names in sorted(groups.items()):
        where = _rel(path, root) or os.path.basename(path)
        choice = mode
        if not choice and ui.INTERACTIVE:
            ui.out()
            ui.note("%s has its own .git (%s) inside another repository" % (where, ", ".join(names)))
            choice = ui.select("What should happen to it?", [
                ("merge", "Merge into the outer repository (its .git is moved to ~/.aihub/backups; history is kept there)"),
                ("keep", "Keep it as its own repository (the outer one ignores it; you manage it directly)"),
                (None, "Skip for now")])
        if not choice:
            ui.note("%s: not resolved; run `aihub dev tree-solve --nested merge` or `--nested keep`" % where)
            unresolved += 1
            continue
        try:
            ui.good(resolve_nested(path, choice))
            resolved += 1
        except (gitx.GitError, OSError) as e:
            ui.bad("%s: %s" % (where, e))
            unresolved += 1
    for d in dirs:                                            # whatever the repositories look like now, make [git] say it
        try:
            rec, changes = fix_git(d, root, remote=remote)
            for c in changes:
                ui.good("%s  [git] %s" % (rec["name"], c))
        except (gitx.GitError, OSError, ValueError) as e:
            ui.bad("%s: %s" % (os.path.basename(d), e))
    return resolved, unresolved + len([1 for r in inspect(root, dirs) for c, _ in r["problems"] if c in ("duplicate", "manifest")])


# ------------------------------------------------------------------------------------------------ init
def ensure_repo(root, remote="", branch="main"):
    """A workspace folder needs one repository and one remote. Create them if missing; ask for the URL once (never per package)."""
    os.makedirs(root, exist_ok=True)
    if not gitx.is_repo(root):
        gitx.run(["init", "-q", "-b", branch], cwd=root)
        ui.good("created a git repository in %s" % root)
    top = pub.toplevel(root)
    has, _ = gitx.remote_info(top)
    if remote:
        gitx.run(["remote", "set-url" if has else "add", "origin", remote], cwd=top)
        ui.good("remote origin = %s" % gitx.redact_url(remote))
    elif not has and ui.INTERACTIVE:
        try:
            url = input("  git remote URL for the whole workspace (blank to set it later): ").strip()
        except EOFError:
            url = ""
        if url:
            gitx.run(["remote", "add", "origin", url], cwd=top)
    return top
