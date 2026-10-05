"""Parse + validate aihub.toml."""
from . import naming, version

TYPES = ("skill", "agent", "mcp", "tool", "setup")

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
    req = d.get("requires") or {}
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
    }
