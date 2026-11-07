"""Read-only git inspection of a chat's working folder, for the canvas Git tab and the Diff tab "since last commit".

Everything here only READS: no fetch, no checkout, no index refresh (GIT_OPTIONAL_LOCKS=0 so `git status` never takes the index
lock a running `git commit` needs). Each git call has a timeout, no stdin, no prompt and no console window on Windows.
Paths given and returned are relative to the working folder (which may be a sub folder of the repository); nothing outside it
and nothing under .git is shown. Revisions are hex object ids or HEAD only; a branch name is resolved to its id first."""
import difflib
import os
import re
import subprocess
import time

TIMEOUT = 10
MAX_TEXT = 1_500_000                  # a bigger file is not sent as text
MAX_FILES = 1000                      # changed files listed at most
COUNT_BYTES = 400_000                 # line counts of untracked files: only up to this size
LOG_MAX = 200
REV_RX = re.compile(r"^(HEAD|[0-9a-fA-F]{4,64})$")
REF_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]{0,200}$")
US, RS = "\x1f", "\x1e"


def _env():
    e = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat")
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        e.pop(k, None)
    return e


def git(cwd, *args, timeout=TIMEOUT):
    """-> (returncode, stdout bytes); (-1, b"") when git is missing, the folder is gone or it timed out."""
    try:
        r = subprocess.run(["git", "--no-pager", "-c", "core.quotepath=off", "-c", "color.ui=false", *args], cwd=cwd, capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=timeout, env=_env(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError, ValueError):
        return -1, b""
    return r.returncode, r.stdout


def _out(cwd, *args):
    rc, b = git(cwd, *args)
    return b.decode("utf-8", "replace") if rc == 0 else None


def clean(rel):
    """A safe relative path ("a/b.py"), or None for "", "..", absolute-looking or .git paths."""
    parts = [x for x in str(rel or "").replace("\\", "/").split("/") if x and x != "."]
    if not parts or ".." in parts or ".git" in parts or ":" in parts[0]:
        return None
    return "/".join(parts)


def good_rev(rev):
    return isinstance(rev, str) and bool(REV_RX.match(rev))


def repo(cwd):
    """{"top", "prefix"} of the repository holding CWD, or None (not a repo, or git missing)."""
    if not cwd or not os.path.isdir(cwd):
        return None
    o = _out(cwd, "rev-parse", "--show-toplevel", "--show-prefix")
    if o is None:
        return None
    lines = o.split("\n")
    return {"top": lines[0].strip(), "prefix": (lines[1] if len(lines) > 1 else "").strip()}


def head(cwd):
    o = _out(cwd, "rev-parse", "-q", "--verify", "HEAD^{commit}")
    return o.strip() if o else None


def resolve_ref(cwd, ref):
    """Commit id of a local branch, tag or remote branch name; None when there is no such ref."""
    if not isinstance(ref, str) or not REF_RX.match(ref) or ".." in ref:
        return None
    for full in ("refs/heads/" + ref, "refs/tags/" + ref, "refs/remotes/" + ref):
        o = _out(cwd, "rev-parse", "-q", "--verify", full + "^{commit}")
        if o:
            return o.strip()
    return None


def _strip_auth(url):
    return re.sub(r"(://)[^/@\s]+@", r"\1", url)


def _status(cwd, prefix):
    """Changed paths of the working folder from `git status --porcelain=v2`: branch info + entries."""
    rc, b = git(cwd, "status", "--porcelain=v2", "--branch", "-z", "--untracked-files=normal", "--", ".")
    if rc != 0:
        return None
    br, items = {}, []
    recs = b.decode("utf-8", "replace").split("\0")
    i = 0
    while i < len(recs):
        r = recs[i]
        i += 1
        if not r:
            continue
        if r.startswith("# "):
            k, _, v = r[2:].partition(" ")
            br[k] = v
            continue
        kind = r[0]
        if kind == "?":
            path, xy = r[2:], "??"
        elif kind == "1":
            f = r.split(" ", 8)
            xy, path = f[1], f[8] if len(f) > 8 else ""
        elif kind == "2":
            f = r.split(" ", 9)
            xy, path = f[1], f[9] if len(f) > 9 else ""
            i += 1                                                  # the original path follows as its own record
        elif kind == "u":
            f = r.split(" ", 10)
            xy, path = f[1], f[10] if len(f) > 10 else ""
        else:
            continue
        if prefix and path.startswith(prefix):
            path = path[len(prefix):]
        if not clean(path):
            continue
        items.append({"path": path, "x": xy[0], "y": xy[1], "conflict": kind == "u", "untracked": kind == "?"})
    return br, items


def info(cwd):
    """What the Git tab header shows: branch, HEAD, upstream and ahead/behind, counts, branches, remotes, stashes. None = not a repo."""
    rp = repo(cwd)
    if not rp:
        return None
    st = _status(cwd, rp["prefix"]) or ({}, [])
    br, items = st
    out = {"repo": True, "top": rp["top"], "prefix": rp["prefix"].rstrip("/"), "head": None, "branch": None, "detached": False,
           "upstream": br.get("branch.upstream"), "ahead": 0, "behind": 0}
    hd = br.get("branch.head")
    if hd and hd != "(detached)":
        out["branch"] = hd
    elif hd == "(detached)":
        out["detached"] = True
    m = re.match(r"\+(\d+) -(\d+)", br.get("branch.ab", ""))
    if m:
        out["ahead"], out["behind"] = int(m.group(1)), int(m.group(2))
    oid = br.get("branch.oid")
    if oid and oid != "(initial)":
        lg = log(cwd, n=1)
        out["head"] = lg["commits"][0] if lg["commits"] else {"sha": oid, "short": oid[:7]}
    out["counts"] = {"staged": sum(1 for x in items if x["x"] not in ".?" and not x["conflict"]),
                     "unstaged": sum(1 for x in items if x["y"] not in ".?" and not x["conflict"]),
                     "untracked": sum(1 for x in items if x["untracked"]), "conflicts": sum(1 for x in items if x["conflict"])}
    out["status"] = items[:MAX_FILES]
    out["status_more"] = len(items) > MAX_FILES
    o = _out(cwd, "for-each-ref", "--sort=-committerdate", "--count=100",
             "--format=%(refname:short)%1f%(objectname)%1f%(committerdate:unix)%1f%(upstream:short)%1f%(subject)", "refs/heads")
    out["branches"] = []
    for ln in (o or "").splitlines():
        f = ln.split(US)
        if len(f) >= 5:
            out["branches"].append({"name": f[0], "sha": f[1], "short": f[1][:7], "date": int(f[2] or 0), "upstream": f[3], "subject": f[4][:200],
                                    "current": f[0] == out["branch"]})
    rem = {}
    for ln in (_out(cwd, "remote", "-v") or "").splitlines():
        f = ln.split()
        if len(f) >= 2 and f[0] not in rem:
            rem[f[0]] = _strip_auth(f[1])
    out["remotes"] = [{"name": k, "url": v} for k, v in rem.items()]
    o = _out(cwd, "tag", "--list")
    out["tags"] = len((o or "").split())
    o = _out(cwd, "stash", "list", "--format=%h")
    out["stashes"] = len((o or "").split())
    return out


def _commits(text):
    out = []
    for rec in text.split(RS):
        f = rec.strip("\n").split(US)
        if len(f) < 8:
            continue
        out.append({"sha": f[0], "short": f[1], "author": f[2], "email": f[3], "date": int(f[4] or 0), "refs": [x.strip() for x in f[5].split(",") if x.strip()],
                    "parents": f[6].split(), "subject": f[7]})
    return out


LOG_FMT = "--format=%H%x1f%h%x1f%an%x1f%ae%x1f%at%x1f%D%x1f%P%x1f%s%x1e"


def log(cwd, path=None, skip=0, n=50, rev="HEAD", grep=None):
    """Commits touching the working folder (or one PATH in it), newest first: {"commits": [...], "more": bool}."""
    n = max(1, min(int(n or 50), LOG_MAX))
    skip = max(0, int(skip or 0))
    if not good_rev(rev):
        return {"commits": [], "more": False}
    args = ["log", LOG_FMT, "-n", str(n + 1), "--skip", str(skip)]
    if grep:
        args += ["-i", "--fixed-strings", "--grep=" + str(grep)[:200]]
    args += [rev, "--"]
    if path:
        p = clean(path)
        if not p:
            return {"commits": [], "more": False}
        args += ["--follow", p] if not grep else [p]
    else:
        args.append(".")
    o = _out(cwd, *args)
    c = _commits(o or "")
    return {"commits": c[:n], "more": len(c) > n}


def _numstat(raw):
    """{path: (add, del) | None for binary} from `--numstat -z` output (renames are off)."""
    out = {}
    for rec in raw.decode("utf-8", "replace").split("\0"):
        f = rec.split("\t", 2)
        if len(f) == 3 and f[2]:
            out[f[2]] = None if f[0] == "-" else (int(f[0]), int(f[1]))
    return out


def _namestatus(raw):
    """[(letter, path)] from `--name-status -z` output (renames are off)."""
    recs = raw.decode("utf-8", "replace").split("\0")
    return [(recs[i][:1], recs[i + 1]) for i in range(0, len(recs) - 1, 2) if recs[i]]


STATUS_WORD = {"A": "added", "D": "deleted", "M": "modified", "T": "modified", "U": "conflict"}


def _files(cwd, diff_args):
    """Changed files of a `git diff` / `git diff-tree` with DIFF_ARGS, limited to and relative to the working folder."""
    common = ["--no-renames", "--relative", "-z"]
    rc1, ns = git(cwd, *diff_args, "--name-status", *common, "--", ".")
    rc2, nm = git(cwd, *diff_args, "--numstat", *common, "--", ".")
    if rc1 != 0:
        return None
    counts = _numstat(nm) if rc2 == 0 else {}
    out = []
    for letter, path in _namestatus(ns):
        if not clean(path):
            continue
        c = counts.get(path, (0, 0))
        out.append({"path": path, "status": STATUS_WORD.get(letter, "modified"), "add": c[0] if c else None, "del": c[1] if c else None, "binary": c is None})
    return out


def commit(cwd, rev):
    """One commit: author, dates, full message, parents, refs and the files it changed in the working folder (vs its first parent)."""
    if not good_rev(rev):
        return None
    o = _out(cwd, "show", "-s", "--format=%H%x1f%h%x1f%an%x1f%ae%x1f%at%x1f%cn%x1f%ct%x1f%P%x1f%D%x1f%B", rev + "^{commit}")
    if o is None:
        return None
    f = o.split(US, 9)
    if len(f) < 10:
        return None
    c = {"sha": f[0], "short": f[1], "author": f[2], "email": f[3], "date": int(f[4] or 0), "committer": f[5], "cdate": int(f[6] or 0),
         "parents": f[7].split(), "refs": [x.strip() for x in f[8].split(",") if x.strip()], "message": f[9].rstrip()[:20000]}
    c["subject"] = c["message"].split("\n", 1)[0]
    if c["parents"]:
        files = _files(cwd, ["diff", c["parents"][0], c["sha"]])
    else:
        files = _files(cwd, ["diff-tree", "-r", "--root", "--no-commit-id", c["sha"]])
    files = files or []
    c["files"], c["files_more"] = files[:MAX_FILES], len(files) > MAX_FILES
    return c


def _blob(cwd, rev, rel):
    """Text of REL (relative to the folder) at REV: str; "" when the file is not there; {"binary": True} / {"big": size}."""
    rc, b = git(cwd, "cat-file", "-s", rev + ":./" + rel)
    if rc != 0:
        return ""
    try:
        size = int(b.strip() or 0)
    except ValueError:
        return ""
    if size > MAX_TEXT:
        return {"big": size}
    rc, b = git(cwd, "cat-file", "blob", rev + ":./" + rel)
    if rc != 0:
        return ""
    if b"\0" in b[:4096]:
        return {"binary": True}
    return b.decode("utf-8", "replace")


def file_at(cwd, rev, rel):
    """Text of a file at a commit (see _blob), or None for a bad path / revision."""
    rel = clean(rel)
    if not rel or not good_rev(rev):
        return None
    return _blob(cwd, rev, rel)


def file_pair(cwd, rev, rel):
    """{"old", "new"} of a file in commit REV: before (its first parent; "" for a new file) and after ("" when it was deleted)."""
    rel = clean(rel)
    if not rel or not good_rev(rev):
        return None
    sha = _out(cwd, "rev-parse", "-q", "--verify", rev + "^{commit}")
    if not sha:
        return None
    sha = sha.strip()
    par = _out(cwd, "rev-parse", "-q", "--verify", sha + "^1")
    return {"old": _blob(cwd, par.strip(), rel) if par else "", "new": _blob(cwd, sha, rel), "rev": sha, "parent": par.strip() if par else None}


def committed(cwd, rel):
    """Text of a file at HEAD when it is tracked there, else None (no repo, no commit yet, untracked, binary or too big)."""
    rel = clean(rel)
    if not rel or not repo(cwd) or not head(cwd):
        return None
    t = _blob(cwd, "HEAD", rel)
    if not isinstance(t, str) or (t == "" and git(cwd, "cat-file", "-e", "HEAD:./" + rel)[0] != 0):
        return None
    return t


def _lines_of(path):
    try:
        if os.path.getsize(path) > COUNT_BYTES:
            return None
        with open(path, "rb") as f:
            b = f.read()
    except OSError:
        return None
    if b"\0" in b[:4096]:
        return None
    return b.count(b"\n") + (1 if b and not b.endswith(b"\n") else 0)


def changes(cwd):
    """Files of the working folder that differ from HEAD (staged or not) plus the untracked ones: [{path, status, add, del, binary}].
    None when this is not a repository or it has no commit yet."""
    if not repo(cwd) or not head(cwd):
        return None
    out = _files(cwd, ["diff", "HEAD"]) or []
    rc, b = git(cwd, "ls-files", "--others", "--exclude-standard", "-z", "--", ".")
    if rc == 0:
        for p in b.decode("utf-8", "replace").split("\0"):
            if not clean(p) or len(out) >= MAX_FILES * 2:
                continue
            n = _lines_of(os.path.join(cwd, p))
            out.append({"path": p, "status": "untracked", "add": n, "del": 0 if n is not None else None, "binary": False})
    return out


# ---- line counts for the "since last message" list (git counts its own)

def counts(a, b, deadline=None):
    """(+added, -deleted) lines between texts A and B; None when it would take too long."""
    if deadline is not None and time.monotonic() > deadline:
        return None
    x, y = a.splitlines(), b.splitlines()
    s = 0
    while s < len(x) and s < len(y) and x[s] == y[s]:
        s += 1
    ex, ey = len(x), len(y)
    while ex > s and ey > s and x[ex - 1] == y[ey - 1]:
        ex -= 1
        ey -= 1
    x, y = x[s:ex], y[s:ey]
    if not x or not y:
        return len(y), len(x)
    if len(x) * len(y) > 4_000_000:
        return len(y), len(x)
    add = dele = 0
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, x, y, autojunk=False).get_opcodes():
        if op != "equal":
            dele += i2 - i1
            add += j2 - j1
    return add, dele
