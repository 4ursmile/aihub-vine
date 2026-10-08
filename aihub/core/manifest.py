"""Parse + validate aihub.toml."""
from . import naming, version

import os
import re

TYPES = ("skill", "agent", "mcp", "tool", "setup")
OSES = ("macos", "linux", "windows")
SCRIPT_EXT = (".sh", ".ps1", ".cmd", ".bat", ".py")


def lint(m: dict, root: str):
    """File-level checks against the project folder (normalize() only sees the TOML). -> (errors, warnings)."""
    errs, warns = [], []
    base = os.path.realpath(root)

    def inside(rel):
        full = os.path.realpath(os.path.join(base, rel))
        return full if full == base or full.startswith(base + os.sep) else None

    def need(rel, what):
        full = inside(rel)
        if full is None:
            errs.append("%s %r points outside the project" % (what, rel))
        elif not os.path.exists(full):
            errs.append("%s %r does not exist" % (what, rel))
        return full

    for kind, label in (("skills", "skill"), ("agents", "agent")):
        for x in m[kind]:
            full = need(x.get("path", ""), "%s path" % label)
            if full and label == "skill" and os.path.isdir(full) and not os.path.isfile(os.path.join(full, "SKILL.md")):
                errs.append("skill %r has no SKILL.md in %s" % (x["name"], x["path"]))
    for cmd, rel in m["bin"].items():
        need(rel, "[bin] %s" % cmd)
    for key, entry in m["scripts"].items():
        e = str(entry).strip()
        if e.lower().endswith(SCRIPT_EXT) and " " not in e:
            need(e, "[scripts] %s" % key)
    pkg = m["package"]
    typ = pkg["type"]
    # agent/mcp/tool packages that run something need a way to install it on every OS they claim to support
    self_installing = bool(m["bin"] or m["mcp_servers"] or m["agents"])
    if typ in ("agent", "mcp", "tool") and not self_installing:
        missing = [o for o in (m["requires"]["os"] or OSES) if ("install_" + o) not in m["scripts"]]
        if missing:
            warns.append("type=%s but no install script for: %s" % (typ, ", ".join(missing)))
    if typ == "mcp" and not m["mcp_servers"]:
        errs.append('type="mcp" needs at least one [[mcp_servers]] entry')
    if typ == "skill" and not m["skills"]:
        warns.append('type="skill" but there is no [[skills]] entry, so nothing will be registered')
    if typ == "agent" and not m["agents"]:
        warns.append('type="agent" but there is no [[agents]] entry, so nothing will be registered')
    if not pkg["description"].strip():
        warns.append("package.description is empty; it is what people search and read first")
    if not os.path.isfile(os.path.join(base, pkg["readme"])):
        warns.append("README %r not found; the package page will have no Overview" % pkg["readme"])
    return errs, warns

try:
    import tomllib as _toml  # py3.11+
except ImportError:  # py3.9 / 3.10
    from . import toml_lite as _toml


def parse(text: str) -> dict:
    return normalize(_toml.loads(text))


def normalize(d: dict) -> dict:
    p = d.get("package") or {}
    if not p:
        raise ValueError("[package] table missing")
    name = naming.validate(str(p.get("name", "")))
    ver = str(p.get("version", ""))
    version.parse(ver)
    typ = p.get("type", "tool")
    if typ not in TYPES:
        raise ValueError("package.type must be one of %s" % (TYPES,))
    setup = d.get("setup") or {}
    for st in setup.get("steps", []):
        if st.get("kind") not in ("env", "json_merge", "file", "block", "command"):
            raise ValueError("setup step has unknown kind: %r" % st.get("kind"))
    scripts = d.get("scripts") or {}
    for k in scripts:
        m = re.match(r"^(install|uninstall)_(macos|linux|windows)$", k)
        if not m:
            raise ValueError("[scripts] key %r is not valid; use install_macos, install_linux, install_windows (and uninstall_*)" % k)
    for kind in ("skills", "agents", "mcp_servers"):
        names = [str(x.get("name", "")) for x in d.get(kind, [])]
        if any(not n for n in names):
            raise ValueError("every [[%s]] entry needs a name" % kind)
        if len(set(names)) != len(names):
            raise ValueError("duplicate name in [[%s]]" % kind)
    for x in d.get("mcp_servers", []):
        if not x.get("command"):
            raise ValueError("[[mcp_servers]] %r needs a command" % x.get("name"))
    req = d.get("requires") or {}
    if req.get("os") and any(o not in OSES for o in req["os"]):
        raise ValueError("requires.os values must be among %s" % (OSES,))
    py = d.get("python") or {}
    return {
        "package": {
            "name": name, "version": ver, "type": typ,
            "description": str(p.get("description", "")),
            "tags": [str(t).lower() for t in p.get("tags", [])],
            "readme": p.get("readme", "README.md"),
            "license": p.get("license", ""),
        },
        "requires": {
            "commands": req.get("commands", []),  # [{name, hint}] or [str]
            "packages": req.get("packages", []),  # ["dep", "dep>=1"]
            "os": req.get("os", []),
        },
        "python": {"requires": py.get("requires", []), "requirements_file": py.get("requirements_file", "")},
        "skills": d.get("skills", []),           # [{name, path}]
        "agents": d.get("agents", []),           # [{name, path}]
        "mcp_servers": d.get("mcp_servers", []), # [{name, command, args, env}]
        "bin": d.get("bin", {}),                 # {cmd: relative/path}
        "scripts": d.get("scripts", {}),         # {install_<os>: "...", uninstall_<os>: "..."}
        "setup": setup,
        "git": {k: str(g.get(k, "")) for k in ("url", "branch", "subdir")} if (g := d.get("git") or {}) is not None else {},
    }
