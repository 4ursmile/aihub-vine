"""Turn an existing project folder into an AI Hub package: look at what is there, fill aihub.toml, and only ask about what is unclear.

Standard library only (shared by the CLI and the server). `analyze(path)` reads a folder and never writes; `build(plan)` renders the
aihub.toml text; `renames(plan)` lists the file renames that make install scripts match the names the installer looks for.

What it recognises (anything else becomes a *question*, asked only when the structure is custom):
  skills      folders holding SKILL.md                      -> [[skills]] name/path
  agents      agents/*.md or .claude/agents/*.md           -> [[agents]] name/path
  mcp         .mcp.json / mcp.json / package.json "mcp"    -> [[mcp_servers]] name/command/args
  bin         bin/* executables                            -> [bin] command = path
  scripts     install*/setup*/uninstall* .sh .ps1 .cmd .bat -> [scripts] install_<os> / uninstall_<os> (renamed to match when needed)
  requires    commands used by the scripts (git, node, ...), requirements.txt / pyproject / package.json deps
"""
import json
import os
import re

from . import naming
try:
    import tomllib as _toml
except ImportError:                                   # py3.9 / 3.10
    from . import toml_lite as _toml

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".idea", ".vscode", ".pytest_cache"}
OS_BY_EXT = {".sh": ("macos", "linux"), ".ps1": ("windows",), ".cmd": ("windows",), ".bat": ("windows",)}
OS_WORDS = {"mac": "macos", "macos": "macos", "darwin": "macos", "osx": "macos", "linux": "linux", "unix": "linux", "win": "windows",
            "windows": "windows", "win32": "windows"}
KNOWN_COMMANDS = {"git": "https://git-scm.com", "node": "https://nodejs.org", "npm": "https://nodejs.org", "npx": "https://nodejs.org",
                  "python3": "https://python.org", "python": "https://python.org", "pip": "https://python.org", "pip3": "https://python.org",
                  "uv": "https://docs.astral.sh/uv", "docker": "https://docker.com", "curl": "", "jq": "", "go": "https://go.dev",
                  "cargo": "https://rustup.rs", "java": "", "dotnet": "", "ruby": "", "gh": "https://cli.github.com", "make": ""}
INSTALL_WORDS = ("install", "setup", "bootstrap", "init")
UNINSTALL_WORDS = ("uninstall", "remove", "teardown", "cleanup")


class Plan(dict):
    """A dict with attribute access: name, type, description, skills, agents, mcp_servers, bin, scripts, requires, python, renames, questions, notes."""
    __getattr__ = dict.get


def _walk(root, max_depth=4):
    root = os.path.abspath(root)
    for dp, dns, fns in os.walk(root):
        depth = dp[len(root):].count(os.sep)
        dns[:] = sorted(d for d in dns if d not in SKIP_DIRS and not d.startswith(".") or d in (".claude", ".mcp"))
        if depth >= max_depth:
            dns[:] = []
        yield os.path.relpath(dp, root).replace(os.sep, "/").lstrip("."), sorted(dns), sorted(fns)


def _rel(p):
    return "." if p in ("", "/") else p.lstrip("/")


def _read(path, limit=200000):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def _front(text):
    """name/description from a markdown front matter block, or {}."""
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    out = {}
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            if k.strip() in ("name", "description") and v.strip():
                out[k.strip()] = v.strip().strip("\"'")
    return out


def _slug(s):
    s = re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")
    return s or "package"


def script_os(filename):
    """Which OSes a script file name targets: ('install'|'uninstall'|None, [os,...]). Windows-only for .ps1/.cmd/.bat, otherwise decided by
    an os word in the name (install-mac.sh), else every unix (macos + linux)."""
    base, ext = os.path.splitext(os.path.basename(filename).lower())
    if ext not in OS_BY_EXT:
        return None, []
    words = re.split(r"[^a-z0-9]+", base)
    kind = "uninstall" if any(w in UNINSTALL_WORDS for w in words) else "install" if any(w in INSTALL_WORDS for w in words) else None
    named = []
    for w in words:
        if w in OS_WORDS and OS_WORDS[w] not in named:
            named.append(OS_WORDS[w])
    return kind, (named or list(OS_BY_EXT[ext]))


def _scripts(files):
    """files: relative paths. -> ({key: path}, renames[(old, new)], unmatched[path])."""
    chosen, renames, unmatched = {}, [], []
    cands = [f for f in files if script_os(f)[0]]
    cands.sort(key=lambda f: (f.count("/"), f))
    for f in cands:
        kind, oses = script_os(f)
        ext = os.path.splitext(f)[1].lower()
        for o in oses:
            key = "%s_%s" % (kind, o)
            if key in chosen:
                if chosen[key] != f:
                    unmatched.append(f)
                continue
            chosen[key] = f
    # a script must be named so a reader sees which OS it is for: <kind>-<os><ext>, in the folder it already lives in
    seen = {}
    for key, f in list(chosen.items()):
        seen.setdefault(f, []).append(key)
    for f, keys in seen.items():
        kind = keys[0].split("_")[0]
        oses = [k.split("_")[1] for k in keys]
        d, base = os.path.split(f)
        ext = os.path.splitext(base)[1].lower()
        want_os = "windows" if oses == ["windows"] else None if sorted(oses) == ["linux", "macos"] else oses[0]
        want = "%s-%s%s" % (kind, want_os, ext) if want_os else "%s%s" % (kind, ext)
        new = ("%s/%s" % (d, want)) if d else want
        if new != f and new not in files and not any(r[1] == new for r in renames):
            renames.append((f, new))
            for k in keys:
                chosen[k] = new
    return chosen, renames, sorted(set(unmatched))


def _mcp_from_json(data):
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    out = []
    if isinstance(servers, dict):
        for name, s in servers.items():
            if isinstance(s, dict) and s.get("command"):
                out.append({"name": _slug(name), "command": str(s["command"]), "args": [str(a) for a in (s.get("args") or [])],
                            "env": {str(k): str(v) for k, v in (s.get("env") or {}).items()} if isinstance(s.get("env"), dict) else {}})
    return out


def _requires(root, files, scripts):
    cmds, py, notes = [], [], []
    used = set()
    for rel in list(scripts.values()) + [f for f in files if f.endswith((".sh", ".ps1", ".cmd", ".bat")) and f.count("/") < 3]:
        text = _read(os.path.join(root, rel), 60000)
        for c in KNOWN_COMMANDS:
            if re.search(r"(?<![\w./-])%s(?![\w-])\s" % re.escape(c), text + " ") and c not in used:
                used.add(c)
    for c in sorted(used):
        hint = KNOWN_COMMANDS[c]
        cmds.append({"name": c, "hint": hint} if hint else {"name": c})
    req = os.path.join(root, "requirements.txt")
    if os.path.isfile(req):
        py = [l.strip() for l in _read(req).splitlines() if l.strip() and not l.startswith(("#", "-"))][:50]
    pj = os.path.join(root, "pyproject.toml")
    if not py and os.path.isfile(pj):
        try:
            deps = (_toml.loads(_read(pj)).get("project") or {}).get("dependencies") or []
            py = [str(d) for d in deps][:50]
        except Exception:
            notes.append("pyproject.toml could not be read; python requirements left empty")
    if os.path.isfile(os.path.join(root, "package.json")) and not any(c["name"] == "node" for c in cmds):
        cmds.append({"name": "node", "hint": KNOWN_COMMANDS["node"]})
    return cmds, py, notes


def analyze(path, exclude=()):
    """Read PATH and return a Plan. Never writes. `questions` lists the things only the user can decide (empty for a standard layout)."""
    root = os.path.abspath(path)
    files, dirs = [], []
    for d, dns, fns in _walk(root):
        for fn in fns:
            files.append(("%s/%s" % (d, fn)) if d else fn)
        dirs += [("%s/%s" % (d, x)) if d else x for x in dns]
    files = [f[2:] if f.startswith("./") else f for f in files]
    ex = tuple(e.strip("/") + "/" for e in exclude)
    files = [f for f in files if not f.startswith(ex)] if ex else files
    dirs = [d for d in dirs if not (ex and (d + "/").startswith(ex))]
    fset = set(files)
    notes, questions = [], []

    skills, agents, mcp = [], [], []
    for f in files:
        if os.path.basename(f) == "SKILL.md":
            folder = os.path.dirname(f) or "."
            fm = _front(_read(os.path.join(root, f)))
            skills.append({"name": _slug(fm.get("name") or (os.path.basename(folder) if folder != "." else os.path.basename(root))),
                           "path": folder, "_desc": fm.get("description", "")})
    for f in files:
        if f.endswith(".md") and re.search(r"(^|/)agents/[^/]+\.md$", f) and f not in ("README.md",):
            fm = _front(_read(os.path.join(root, f)))
            if fm or "/agents/" in "/" + f:
                agents.append({"name": _slug(fm.get("name") or os.path.splitext(os.path.basename(f))[0]), "path": f, "_desc": fm.get("description", "")})
    for cand in (".mcp.json", "mcp.json", ".claude/mcp.json"):
        if cand in fset:
            try:
                mcp += _mcp_from_json(json.loads(_read(os.path.join(root, cand))))
            except ValueError:
                questions.append({"id": "mcp-json", "text": "%s is not valid JSON; describe the MCP server by hand" % cand})
    pkgjson = {}
    if "package.json" in fset:
        try:
            pkgjson = json.loads(_read(os.path.join(root, "package.json")))
        except ValueError:
            notes.append("package.json is not valid JSON")
        mcp += _mcp_from_json(pkgjson.get("mcp") if isinstance(pkgjson.get("mcp"), dict) else {})
    bins = {}
    for f in files:
        if re.match(r"^bin/[^/]+$", f) and not f.endswith((".md", ".txt")):
            bins[os.path.splitext(os.path.basename(f))[0]] = f
    scripts, renames, extra = _scripts(files)
    scripts_files = [f for f in files if script_os(f)[0]]
    for f in extra:
        questions.append({"id": "script:" + f, "text": "%s looks like a second %s script for the same OS; which one should the installer run?" % (f, script_os(f)[0])})

    # type: what the project mostly is
    if mcp:
        typ = "mcp"
    elif skills and not agents:
        typ = "skill"
    elif agents and not skills:
        typ = "agent"
    elif skills or agents:
        typ = "setup" if "setup" in {os.path.basename(d) for d in dirs} else "skill"
    elif bins or scripts:
        typ = "tool"
    else:
        typ = None
        questions.append({"id": "type", "text": "Nothing here matches a known layout (no SKILL.md, agents/, .mcp.json, bin/ or install scripts). What kind of package is it?",
                          "choices": ["skill", "agent", "mcp", "tool", "setup"]})
    if (agents or mcp) and not scripts and not bins and typ in ("agent", "tool"):
        notes.append("no install script found; fine if the package only registers files")
    cmds, py, rnotes = _requires(root, files, scripts)
    notes += rnotes

    name = _slug(pkgjson.get("name") or os.path.basename(root))
    try:
        name = naming.validate(name)
    except ValueError:
        questions.append({"id": "name", "text": "%r is not a valid package name; pick one (lowercase letters, digits, - _)" % name})
    desc = str(pkgjson.get("description") or "")
    if not desc:
        readme = _read(os.path.join(root, "README.md"), 4000)
        for line in readme.splitlines():
            t = line.strip()
            if t and not t.startswith(("#", "!", "[", "<", "`", "-", ">")):
                desc = t[:200]
                break
    if not desc and (skills or agents):
        desc = (skills + agents)[0].get("_desc", "")[:200]
    return Plan(path=root, name=name, type=typ, description=desc, license=str(pkgjson.get("license") or ""), skills=skills, agents=agents,
                mcp_servers=mcp, bin=bins, scripts=scripts, renames=renames, requires={"commands": cmds}, python={"requires": py},
                questions=questions, notes=notes, os=sorted({k.split("_")[1] for k in scripts}), scripts_found=scripts_files)


def candidates(root):
    """Direct sub folders of ROOT that look like a package (SKILL.md, agents, .mcp.json, install scripts, bin/, aihub.toml), as
    [(folder, plan_or_None, already_package)]. Used for the monorepo checklist."""
    out = []
    root = os.path.abspath(root)
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if name in SKIP_DIRS or name.startswith(".") or not os.path.isdir(d):
            continue
        if os.path.isfile(os.path.join(d, "aihub.toml")):
            out.append((name, None, True))
            continue
        pl = analyze(d)
        if pl["type"] or pl["skills"] or pl["agents"] or pl["mcp_servers"] or pl["bin"] or pl["scripts"]:
            out.append((name, pl, False))
    return out


def _q(s):
    return json.dumps(str(s), ensure_ascii=False)


def _arr(items):
    return "[" + ", ".join(_q(i) for i in items) + "]"


def build(plan, version="0.1.0", authors=(), git=None):
    """Render aihub.toml text from a Plan (answers to `questions` already merged in: name/type/description may be edited by the caller)."""
    p = plan
    out = ["# aihub.toml - generated by `aihub dev init` from the existing project; review it, then: aihub dev validate && aihub dev publish",
           "[package]", "name = %s" % _q(p["name"]), "version = %s" % _q(version), "type = %s" % _q(p["type"] or "tool"),
           "description = %s" % _q(p.get("description") or "%s - describe what it does in one sentence" % p["name"]),
           "tags = []", 'readme = "README.md"']
    if p.get("license"):
        out.append("license = %s" % _q(p["license"]))
    out.append("authors = %s" % _arr(authors))
    reqs = p.get("requires", {}).get("commands", [])
    out += ["", "[requires]"]
    if reqs:
        out.append("commands = [%s]" % ", ".join(("{name = %s, hint = %s}" % (_q(c["name"]), _q(c["hint"]))) if c.get("hint") else _q(c["name"]) for c in reqs))
    else:
        out.append("commands = []")
    out.append("packages = []")
    out.append("os = []")                              # empty = every OS: a script for one OS must not block installs on the others
    py = p.get("python", {}).get("requires", [])
    if py:
        out += ["", "[python]", "requires = %s" % _arr(py)]
    for s in p.get("skills", []):
        out += ["", "[[skills]]", "name = %s" % _q(s["name"]), "path = %s" % _q(s["path"])]
    for a in p.get("agents", []):
        out += ["", "[[agents]]", "name = %s" % _q(a["name"]), "path = %s" % _q(a["path"])]
    for m in p.get("mcp_servers", []):
        out += ["", "[[mcp_servers]]", "name = %s" % _q(m["name"]), "command = %s" % _q(m["command"]), "args = %s" % _arr(m.get("args", []))]
        if m.get("env"):
            out.append("env = {%s}" % ", ".join("%s = %s" % (_q(k), _q(v)) for k, v in m["env"].items()))
    if p.get("bin"):
        out += ["", "[bin]"] + ["%s = %s" % (_q(k), _q(v)) for k, v in sorted(p["bin"].items())]
    if p.get("scripts"):
        out += ["", "[scripts]"] + ["%s = %s" % (k, _q(v)) for k, v in sorted(p["scripts"].items())]
    if git and git.get("url"):
        out += ["", "[git]"] + ["%s = %s" % (k, _q(git[k])) for k in ("url", "branch", "subdir") if git.get(k) is not None]
    return "\n".join(out) + "\n"


def apply_renames(plan, root=None):
    """Perform plan['renames'] on disk (git-aware callers may prefer `git mv`). -> list of (old, new) done."""
    root = root or plan["path"]
    done = []
    for old, new in plan.get("renames", []):
        src, dst = os.path.join(root, old), os.path.join(root, new)
        if os.path.isfile(src) and not os.path.exists(dst):
            os.makedirs(os.path.dirname(dst) or root, exist_ok=True)
            os.replace(src, dst)
            done.append((old, new))
    return done
