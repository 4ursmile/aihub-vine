import os
import re
import subprocess

ALWAYS = [".git", "__pycache__", "*.pyc", ".venv", ".DS_Store", "node_modules", "dist"]


def _glob_re(pat):
    """Translate one gitignore glob (no leading '/', '!' or trailing '/') to a regex source."""
    i, n, out = 0, len(pat), []
    while i < n:
        c = pat[i]
        if c == "*":
            if pat[i:i + 2] == "**":
                j = i + 2
                if pat[j:j + 1] == "/":          # '**/' : zero or more directories
                    out.append("(?:.*/)?")
                    i = j + 1
                else:                            # trailing or bare '**'
                    out.append(".*")
                    i = j
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pat.find("]", i + 2)
            if j == -1:
                out.append(re.escape(c))
            else:
                body = pat[i + 1:j]
                if body[:1] == "!":
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j
        elif c == "\\" and i + 1 < n:
            i += 1
            out.append(re.escape(pat[i]))
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def _rule(line):
    line = line.rstrip("\n")
    if not line.strip() or line.startswith("#"):
        return None
    if not line.endswith("\\ "):
        line = line.rstrip()
    neg = line.startswith("!")
    if neg:
        line = line[1:]
    elif line.startswith(("\\!", "\\#")):
        line = line[1:]
    dir_only = line.endswith("/")
    line = line.rstrip("/")
    if not line:
        return None
    anchored = "/" in line
    line = line.lstrip("/")
    body = _glob_re(line)
    rx = re.compile(("^" if anchored else "^(?:.*/)?") + body + "$")
    return rx, neg, dir_only


def _load(root, names):
    rules = []
    for f in names:
        p = os.path.join(root, f)
        if os.path.isfile(p):
            with open(p, encoding="utf-8", errors="replace") as fh:
                rules += [r for r in map(_rule, fh) if r]
    return rules


def _ignored(rules, rel, is_dir):
    """Last matching rule wins, as in git."""
    res = False
    for rx, neg, dir_only in rules:
        if dir_only and not is_dir:
            continue
        if rx.match(rel):
            res = not neg
    return res


def _excluded(rules, rel):
    """A path is excluded if it or any parent directory is (a parent can't be re-included)."""
    parts = rel.split("/")
    for k in range(1, len(parts)):
        if _ignored(rules, "/".join(parts[:k]), True):
            return True
    return _ignored(rules, rel, False)


def list_files(root: str):
    """Relative file paths to ship. Honors .gitignore and .aihubignore, plus built-in excludes.

    Inside a git repo, git decides what .gitignore excludes (including nested and global
    ignore files); otherwise the root .gitignore is emulated.
    """
    always = [r for r in map(_rule, ALWAYS) if r]
    try:
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=root,
                             capture_output=True, text=True, check=True).stdout.split("\n")
        files = [f for f in out if f and os.path.isfile(os.path.join(root, f))]
        if files:
            rules = always + _load(root, [".aihubignore"])
            return sorted(f for f in files if not _excluded(rules, f))
    except Exception:
        pass
    rules = always + _load(root, [".gitignore", ".aihubignore"])
    res = []
    for d, dirs, fs in os.walk(root):
        rel = os.path.relpath(d, root)
        rel = "" if rel == "." else rel.replace(os.sep, "/")
        dirs[:] = [x for x in dirs if not _ignored(rules, (rel + "/" + x) if rel else x, True)]
        for f in fs:
            r = (rel + "/" + f) if rel else f
            if not _excluded(rules, r):
                res.append(r)
    return sorted(res)
