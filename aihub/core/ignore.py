import fnmatch
import os
import subprocess

ALWAYS = [".git", "__pycache__", "*.pyc", ".venv", ".DS_Store", "node_modules", "dist"]


def _patterns(root):
    pats = list(ALWAYS)
    for f in (".gitignore", ".aihubignore"):
        p = os.path.join(root, f)
        if os.path.isfile(p):
            with open(p) as fh:
                pats += [l.strip().rstrip("/") for l in fh if l.strip() and not l.startswith("#")]
    return pats


def list_files(root: str):
    """Relative file paths to ship. Uses git when in a repo, else emulates."""
    try:
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=root,
                             capture_output=True, text=True, check=True).stdout.split("\n")
        files = [f for f in out if f and os.path.isfile(os.path.join(root, f))]
        if files:
            return sorted(files)
    except Exception:
        pass
    pats, res = _patterns(root), []
    for d, dirs, fs in os.walk(root):
        rel = os.path.relpath(d, root)
        dirs[:] = [x for x in dirs if not any(fnmatch.fnmatch(x, p) for p in pats)]
        for f in fs:
            r = os.path.normpath(os.path.join(rel, f))
            if not any(fnmatch.fnmatch(f, p) or fnmatch.fnmatch(r, p) for p in pats):
                res.append(r)
    return sorted(res)
