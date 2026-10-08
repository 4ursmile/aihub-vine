"""`aihub dev publish` as a git wrapper: verify remote+branch, commit, push (LFS if needed), report to Langfuse."""
import os
import re
import shutil

from ..core import manifest as M
from . import api, gitx, identity, paths, telemetry, ui

LFS_MB = 50


def _norm_url(u):
    u = re.sub(r"^(https?://|ssh://|git@)", "", (u or "").strip())
    u = re.sub(r"^[^/@]+@", "", u).replace(":", "/", 1) if "://" not in (u or "") and "@" not in u else u
    return re.sub(r"(\.git)?/?$", "", u).lower()


def toplevel(root):
    return gitx.run(["rev-parse", "--show-toplevel"], cwd=root, check=False) if gitx.is_repo(root) else None


def write_git_table(toml_path, git):
    """Add or replace the [git] table in aihub.toml, leaving everything else untouched."""
    text = open(toml_path, encoding="utf-8").read()
    block = "[git]\n" + "".join('%s = "%s"\n' % (k, git[k]) for k in ("url", "branch", "subdir") if git.get(k))
    if re.search(r"(?m)^\[git\]\s*$", text):
        text = re.sub(r"(?ms)^\[git\]\s*\n(?:(?!\[).*\n?)*", block + "\n", text, count=1)
    else:
        text = text.rstrip("\n") + "\n\n" + block
    open(toml_path, "w", encoding="utf-8", newline="\n").write(text)


def detect(root, git):
    """Fill missing url/branch/subdir from the working repo; ask the user for what is still missing."""
    git = dict(git)
    url, branch = gitx.remote_info(root) if gitx.is_repo(root) else (None, None)
    git["url"] = git.get("url") or url or ""
    git["branch"] = git.get("branch") or branch or ""
    top = toplevel(root)
    if top and not git.get("subdir"):
        rel = os.path.relpath(os.path.realpath(root), os.path.realpath(top))
        git["subdir"] = "" if rel == "." else rel.replace(os.sep, "/")
    for key, label, default in (("url", "git repository URL", ""), ("branch", "branch", "main")):
        if not git[key]:
            if not ui.INTERACTIVE:
                raise ValueError("aihub.toml has no [git] %s; add it or run `aihub dev init` interactively" % key)
            git[key] = (input("%s%s: " % (label, " [%s]" % default if default else "")).strip() or default)
            if not git[key]:
                raise ValueError("%s is required" % label)
    return git


def ensure_gitignore(root):
    f = os.path.join(root, ".gitignore")
    want = ["dist/", ".DS_Store", "__pycache__/", "*.pyc", ".venv/", "node_modules/"]
    have = open(f).read().splitlines() if os.path.exists(f) else []
    add = [w for w in want if w not in have]
    if add or not os.path.exists(f):
        with open(f, "a", newline="\n") as fh:
            fh.write(("\n" if have and have[-1] else "") + "\n".join(add) + "\n")
        return True
    return False


def _verify(root, git):
    """Remote and branch must match aihub.toml; never silently publish somewhere else."""
    url, branch = gitx.remote_info(root)
    if url and _norm_url(url) != _norm_url(git["url"]):
        raise ValueError("git remote origin is %s but aihub.toml says %s; fix one of them"
                         % (gitx.redact_url(url), gitx.redact_url(git["url"])))
    if branch and branch != git["branch"]:
        raise ValueError("you are on branch '%s' but aihub.toml publishes '%s'; switch branch or edit [git] branch"
                         % (branch, git["branch"]))


def _lfs(top):
    big = gitx.large_files(top, LFS_MB)
    if not big:
        return []
    if gitx.run(["lfs", "version"], cwd=top, check=False) == "":
        raise ValueError("%d file(s) over %d MB (%s) but git-lfs is not installed; install it or shrink them"
                         % (len(big), LFS_MB, ", ".join(big[:3])))
    gitx.run(["lfs", "install", "--local"], cwd=top)
    for f in big:
        gitx.run(["lfs", "track", f], cwd=top)
    return big


def publish(root, m, bump=None):
    """Returns the info dict reported to Langfuse."""
    toml = os.path.join(root, "aihub.toml")
    if not shutil.which("git"):
        raise ValueError("git is not installed")
    git = detect(root, m["git"])
    if git != m["git"]:
        write_git_table(toml, git)
        ui.good("saved [git] to aihub.toml (%s @ %s)" % (gitx.redact_url(git["url"]), git["branch"]))
    created = False
    if not gitx.is_repo(root):
        gitx.run(["init", "-q", "-b", git["branch"]], cwd=root)
        created = True
    top = toplevel(root)
    origin, _ = gitx.remote_info(top)
    if not origin:
        gitx.run(["remote", "add", "origin", git["url"]], cwd=top)
    if gitx.run(["symbolic-ref", "--short", "-q", "HEAD"], cwd=top, check=False) == "" or created:
        gitx.run(["checkout", "-q", "-B", git["branch"]], cwd=top, check=False)
    _verify(root, git)
    m = M.parse(open(os.path.join(root, "aihub.toml"), encoding="utf-8").read())
    ensure_gitignore(root)
    lfs = _lfs(top)
    if lfs:
        ui.note("tracking %d large file(s) with git-lfs" % len(lfs))
    gitx.run(["add", "-A", "--", "."], cwd=root)
    pkg = m["package"]
    if gitx.run(["status", "--porcelain", "--", "."], cwd=root):
        gitx.run(["commit", "-q", "-m", "publish %s %s" % (pkg["name"], pkg["version"])], cwd=root)
    has_up = gitx.run(["rev-parse", "--abbrev-ref", "@{u}"], cwd=top, check=False)
    with ui.Spinner("Pushing to %s" % git["branch"]):
        gitx.run(["push", "-q"] + ([] if has_up else ["-u", "origin", git["branch"]]), cwd=top)
    commit = gitx.run(["rev-parse", "HEAD"], cwd=top)
    who = identity.current()
    info = {"kind": "publish", "package": pkg["name"], "version": pkg["version"], "commit": commit,
            "client_id": "", "git_url": gitx.redact_url(git["url"]), "git_branch": git["branch"],
            "git_subdir": git.get("subdir", ""), "type": pkg["type"], "description": pkg["description"],
            "tags": pkg["tags"], "requires": m["requires"], "python": m["python"], "lfs": bool(lfs),
            "host": who["host"], "cwd": who["cwd"], "local_user": who["local_user"]}
    from . import hooks
    info["client_id"] = hooks.client_id()
    return info


def report(info):
    """Send the publish span now; spool it for the background flusher if Langfuse is unreachable."""
    from . import langfuse
    try:
        if not langfuse.configured():
            raise RuntimeError("langfuse not configured")
        info = dict(info, ts=__import__("time").time())
        langfuse.send([telemetry.to_items(info)])
        return True
    except Exception:
        telemetry.record(info)
        return False
