"""Completions for a file edited without a kernel (a .py file in the canvas or on a Jupyter server, a shell line in a terminal).

Python: jedi when it is installed in the UI's Python (static analysis, runs none of the code), else the names of the file, keywords,
builtins and the members of standard-library modules the file imports. Other languages: the words of the file.
Shell: the commands on PATH, git subcommands, the words already typed and, when the caller names the shell's folder (a local chat
terminal: CWD), the files and folders there. Without CWD files are left out (the terminal may be remote).
Never raises: a failure is an empty list."""
import builtins
import importlib
import inspect
import keyword
import os
import re
import sys

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
WORD_END = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")
DOTTED_END = re.compile(r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\.([A-Za-z_][A-Za-z0-9_]*)?$")
# standard-library modules that do something when imported (open a browser, print, start a GUI): never imported for their names
NO_IMPORT = {"antigravity", "this", "idlelib", "tkinter", "turtle", "turtledemo", "__main__", "pydoc_data", "lib2to3", "ensurepip", "venv", "webbrowser"}
MAX_CODE, MAX_OUT = 400000, 60
_jedi = None


def _kind(v):
    return "module" if inspect.ismodule(v) else "class" if inspect.isclass(v) else "function" if callable(v) else "instance"


def _stdlib(name):
    root = name.split(".")[0]
    names = getattr(sys, "stdlib_module_names", ())
    return root in names and root not in NO_IMPORT


def _imports(code):
    """{alias: dotted module} of the plain `import x`, `import x.y`, `import x as y` lines of CODE."""
    out = {}
    for m in re.finditer(r"^[ \t]*import[ \t]+([^\n#]+)", code, re.M):
        for part in m.group(1).split(","):
            bits = part.split()
            if not bits:
                continue
            mod = bits[0]
            if len(bits) == 3 and bits[1] == "as":
                out[bits[2]] = mod
            else:
                out[mod.split(".")[0]] = mod.split(".")[0]
    for m in re.finditer(r"^[ \t]*from[ \t]+([A-Za-z_][\w.]*)[ \t]+import[ \t]+([^\n#(]+)", code, re.M):
        for part in m.group(2).split(","):
            bits = part.split()
            if bits:
                out[bits[-1] if len(bits) == 3 and bits[1] == "as" else bits[0]] = m.group(1) + "." + bits[0]
    return out


def _members(dotted, aliases):
    root, _, rest = dotted.partition(".")
    mod = aliases.get(root)
    if not mod or not _stdlib(mod):
        return None
    try:
        obj = importlib.import_module(mod)
    except Exception:
        try:                                                          # from x import y: y is a member of x
            base, _, attr = mod.rpartition(".")
            obj = getattr(importlib.import_module(base), attr) if base and _stdlib(base) else None
        except Exception:
            return None
    for a in [x for x in rest.split(".") if x]:
        sub = getattr(obj, a, None)
        if sub is None and inspect.ismodule(obj):
            try:
                sub = importlib.import_module(obj.__name__ + "." + a)
            except Exception:
                sub = None
        if sub is None:
            return None
        obj = sub
    return obj


def _python(code, pos, path):
    global _jedi
    if _jedi is None:
        try:
            import jedi as _j
            _jedi = _j
        except Exception:
            _jedi = False
    before = code[:pos]
    word = WORD_END.search(before)
    start = pos - (len(word.group(0)) if word else 0)
    if _jedi:
        try:
            line = before.count("\n") + 1
            col = pos - (before.rfind("\n") + 1)
            cs = _jedi.Script(code).complete(line, col)
            if cs:
                return {"start": start, "matches": [{"t": c.name, "k": c.type} for c in cs[:MAX_OUT]], "via": "jedi"}
        except Exception:
            pass
    dot = DOTTED_END.search(before)
    if dot:
        pre = dot.group(2) or ""
        obj = _members(dot.group(1), _imports(code))
        if obj is None:
            return {"start": start, "matches": []}
        names = [n for n in dir(obj) if n.startswith(pre) and (pre.startswith("_") or not n.startswith("_"))]
        out = []
        for n in sorted(names, key=lambda x: (x.lower() != x, x.lower()))[:MAX_OUT]:
            try:
                out.append({"t": n, "k": _kind(getattr(obj, n))})
            except Exception:
                out.append({"t": n, "k": "instance"})
        return {"start": pos - len(pre), "matches": out}
    pre = word.group(0) if word else ""
    if not pre:
        return {"start": pos, "matches": []}
    seen, out = set(), []
    pools = (("", IDENT.findall(code[:pos - len(pre)] + code[pos:])), ("keyword", keyword.kwlist), ("function", dir(builtins)))
    for k, pool in pools:
        for n in pool:
            if n != pre and n.startswith(pre) and n not in seen and not n.startswith("__"):
                seen.add(n)
                out.append({"t": n, "k": k})
    return {"start": start, "matches": out[:MAX_OUT]}


_path_cache = {"key": None, "names": []}


def _commands():
    key = os.environ.get("PATH", "")
    if _path_cache["key"] != key:
        names = set()
        for d in key.split(os.pathsep):
            try:
                for n in os.listdir(d):
                    if not n.startswith(".") and os.access(os.path.join(d, n), os.X_OK):
                        names.add(n)
            except OSError:
                pass
        _path_cache.update(key=key, names=sorted(names))
    return _path_cache["names"]


GIT_SUB = ("add", "bisect", "blame", "branch", "checkout", "cherry-pick", "clean", "clone", "commit", "config", "diff", "fetch", "grep", "init", "log",
           "merge", "mv", "pull", "push", "rebase", "reflog", "remote", "reset", "restore", "revert", "rm", "show", "stash", "status", "switch", "tag", "worktree")
# commands people type most: ranked first, so the grey text after "gi" is git and not gio
COMMON = {"aihub", "brew", "cat", "cd", "claude", "code", "cp", "curl", "docker", "echo", "find", "git", "grep", "head", "kill", "less", "ls", "make", "mkdir", "mv",
          "node", "npm", "npx", "open", "pip", "pip3", "pnpm", "ps", "python", "python3", "rg", "rm", "ssh", "tail", "touch", "uv", "vim", "yarn"}
MAX_SCAN = 3000                     # entries read from one folder for path completion


def _paths(pre, cwd, dirs_only):
    """Files and folders for the word PRE, relative to CWD (~ and absolute paths too). Folders end in "/", spaces are escaped."""
    cut = pre.rfind("/") + 1
    head, base = pre[:cut], pre[cut:].replace("\\ ", " ")
    root = os.path.expanduser(head) if head.startswith("~") else head
    folder = os.path.join(cwd, root) if root else cwd
    out = []
    try:
        with os.scandir(folder) as it:
            for i, e in enumerate(it):
                if i >= MAX_SCAN:
                    break
                n = e.name
                if not n.startswith(base) or (n.startswith(".") and not base.startswith(".")):
                    continue
                try:
                    isdir = e.is_dir()
                except OSError:
                    isdir = False
                if dirs_only and not isdir:
                    continue
                out.append((not isdir, n.lower(), {"t": head + n.replace(" ", "\\ ") + ("/" if isdir else ""), "k": "folder" if isdir else "file"}))
    except OSError:
        return []
    return [x[2] for x in sorted(out)[:MAX_OUT]]


def _shell(code, pos, cwd=""):
    before = code[:pos]
    m = re.search(r"(?:\\ |[^\s|;&<>()'\"`])*$", before)
    pre = m.group(0) if m else ""
    lead = before[:len(before) - len(pre)]
    first = not lead.strip() or re.search(r"(\|\||&&|[|;])\s*$", lead)
    words = re.split(r"\|\||&&|[|;]", lead)[-1].split()
    if not pre and (first or not cwd):
        return {"start": pos, "matches": []}
    out, seen = [], set()

    def add(t, k):
        if t != pre and t.startswith(pre) and t not in seen and len(out) < MAX_OUT:
            seen.add(t)
            out.append({"t": t, "k": k})
    if not first and words == ["git"]:
        for n in GIT_SUB:
            add(n, "function")
    pathy = pre.startswith(("./", "../", "/", "~")) or "/" in pre
    if cwd and (not first or pathy) and not pre.startswith("-"):
        for x in _paths(pre, cwd, bool(words) and words[0] in ("cd", "pushd", "rmdir") and len(words) == 1):
            add(x["t"], x["k"])
    if first and not pathy and pre:
        for n in sorted((n for n in _commands() if n.startswith(pre)), key=lambda n: (n not in COMMON, len(n), n)):   # "gi" -> git, "py" -> python
            add(n, "function")
    if pre:
        for n in IDENT.findall(code):
            add(n, "")
    return {"start": pos - len(pre), "matches": out}


def complete(code, pos, lang="", path="", cwd=""):
    try:
        code = str(code or "")[:MAX_CODE]
        pos = max(0, min(int(pos if pos is not None else len(code)), len(code)))
        lang = str(lang or "").lower()
        if lang in ("python", "py") or str(path).endswith(".py"):
            return _python(code, pos, path)
        if lang in ("sh", "bash", "zsh", "shell"):
            return _shell(code, pos, str(cwd or ""))
        before = code[:pos]
        word = WORD_END.search(before)
        pre = word.group(0) if word else ""
        if not pre:
            return {"start": pos, "matches": []}
        seen, out = set(), []
        for n in IDENT.findall(code[:pos - len(pre)] + code[pos:]):
            if n != pre and n.startswith(pre) and n not in seen:
                seen.add(n)
                out.append({"t": n, "k": ""})
        return {"start": pos - len(pre), "matches": out[:MAX_OUT]}
    except Exception:
        return {"start": 0, "matches": []}
