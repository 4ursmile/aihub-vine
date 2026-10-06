"""Starter projects for `aihub dev init --type ...`. Each is a complete, publishable project, not a stub:
files = {relative path: content}. Placeholders: {name}, {title}, {Name}. Executable files are listed in EXEC."""

README = """# {title}

{description}

## Install

```
aihub install {name}
```

## What it does

Describe what {title} does and when to use it. This page is shown on the package's Overview tab.

## Usage

Show one or two concrete examples.

## Uninstall

```
aihub uninstall {name}
```
"""

SKILL_MD = """---
name: {name}
description: Use when the user asks to <describe the trigger in one sentence>. <What it does.>
---

# {title}

## When to use

- <situation 1>
- <situation 2>

## Steps

1. <first step>
2. <second step>
3. <verify the result>

## Notes

- Keep this file under ~500 lines; put long reference material in separate files next to it.
"""

AGENT_MD = """---
name: {name}
description: <When should the assistant delegate to this agent? One sentence.>
tools: Read, Grep, Glob
---

You are {title}. <State the role and goal.>

## How you work

1. <step>
2. <step>

## Output

Return <what the caller should get back>. Be concise.
"""

MCP_SERVER_PY = '''#!/usr/bin/env python3
"""{title} - a minimal MCP server over stdio (JSON-RPC 2.0). Standard library only.
Replace the tool in TOOLS with your own. Test it:  echo '{{"jsonrpc":"2.0","id":1,"method":"tools/list"}}' | python3 server/server.py
"""
import json
import sys

TOOLS = {{
    "echo": {{
        "description": "Return the text you send.",
        "inputSchema": {{"type": "object", "properties": {{"text": {{"type": "string"}}}}, "required": ["text"]}},
        "run": lambda args: str(args.get("text", "")),
    }},
}}


def respond(id_, result=None, error=None):
    msg = {{"jsonrpc": "2.0", "id": id_}}
    msg.update({{"error": error}} if error else {{"result": result}})
    sys.stdout.write(json.dumps(msg) + "\\n")
    sys.stdout.flush()


def handle(req):
    m, id_ = req.get("method"), req.get("id")
    if m == "initialize":
        return respond(id_, {{"protocolVersion": "2024-11-05", "capabilities": {{"tools": {{}}}}, "serverInfo": {{"name": "{name}", "version": "0.1.0"}}}})
    if m == "tools/list":
        return respond(id_, {{"tools": [{{"name": k, "description": v["description"], "inputSchema": v["inputSchema"]}} for k, v in TOOLS.items()]}})
    if m == "tools/call":
        p = req.get("params", {{}})
        tool = TOOLS.get(p.get("name"))
        if not tool:
            return respond(id_, error={{"code": -32602, "message": "unknown tool"}})
        try:
            return respond(id_, {{"content": [{{"type": "text", "text": tool["run"](p.get("arguments", {{}}))}}]}})
        except Exception as e:  # report tool failures to the model, don't crash the server
            return respond(id_, {{"content": [{{"type": "text", "text": "error: %s" % e}}], "isError": True}})
    if id_ is not None:  # notifications (no id) get no reply
        respond(id_, error={{"code": -32601, "message": "method not found"}})


def main():
    for line in sys.stdin:
        line = line.strip()
        if line:
            try:
                handle(json.loads(line))
            except json.JSONDecodeError:
                respond(None, error={{"code": -32700, "message": "parse error"}})


if __name__ == "__main__":
    main()
'''

TOOL_PY = '''#!/usr/bin/env python3
"""{title} - a command line tool. Standard library only."""
import argparse


def main():
    ap = argparse.ArgumentParser(prog="{name}", description="{description}")
    ap.add_argument("name", nargs="?", default="world")
    a = ap.parse_args()
    print("hello, %s" % a.name)


if __name__ == "__main__":
    main()
'''

INSTALL_SH = """#!/bin/sh
# Runs after the package files are placed in $AIHUB_PACKAGE_DIR. Keep it idempotent (safe to run twice).
# aihub asks the user before running this. Do only what the package needs; never use sudo.
set -eu
echo "{name}: installing on $(uname -s)"
# example: check a prerequisite and say how to get it
# command -v git >/dev/null 2>&1 || {{ echo "git is required: https://git-scm.com" >&2; exit 1; }}
"""

UNINSTALL_SH = """#!/bin/sh
# Undo whatever install.sh did. Files aihub placed are removed by aihub itself.
set -eu
echo "{name}: uninstalling"
"""

INSTALL_PS1 = """# Runs after the package files are placed in $env:AIHUB_PACKAGE_DIR. Keep it idempotent. Never require admin rights.
$ErrorActionPreference = 'Stop'
Write-Host "{name}: installing on Windows"
# example: check a prerequisite
# if (-not (Get-Command git -ErrorAction SilentlyContinue)) {{ Write-Error 'git is required: https://git-scm.com'; exit 1 }}
"""

UNINSTALL_PS1 = """$ErrorActionPreference = 'Stop'
Write-Host "{name}: uninstalling"
"""

GITIGNORE = "dist/\n__pycache__/\n*.pyc\n.DS_Store\n.venv/\nnode_modules/\n"
EXEC = {"scripts/install.sh", "scripts/uninstall.sh", "server/server.py", "bin/{name}.py"}

SCRIPT_TOML = """
[scripts]
install_macos = "scripts/install.sh"
install_linux = "scripts/install.sh"
install_windows = "scripts/install.ps1"
uninstall_macos = "scripts/uninstall.sh"
uninstall_linux = "scripts/uninstall.sh"
uninstall_windows = "scripts/uninstall.ps1"
"""

HEAD = """# aihub.toml - the manifest. Docs: /#/docs/publishing
[package]
name = "{name}"
version = "0.1.0"                 # immutable once published; bump it for every release (aihub dev publish --bump patch)
type = "{type}"                   # skill | agent | mcp | tool | setup
description = "{description}"
tags = []                         # lowercase words people search for
readme = "README.md"
license = "MIT"

[requires]
commands = []                     # e.g. [{{name = "git", hint = "brew install git"}}]
packages = []                     # other hub packages this needs, e.g. ["base-tools>=1.0"]
os = []                           # empty = all. Or any of: "macos", "linux", "windows"
"""

TEMPLATES = {
    "skill": {
        "manifest": HEAD + '\n[[skills]]\nname = "{name}"\npath = "skills/{name}"\n',
        "files": {"skills/{name}/SKILL.md": SKILL_MD, "README.md": README, ".gitignore": GITIGNORE},
    },
    "agent": {
        "manifest": HEAD + '\n[[agents]]\nname = "{name}"\npath = "agents/{name}.md"\n',
        "files": {"agents/{name}.md": AGENT_MD, "README.md": README, ".gitignore": GITIGNORE},
    },
    "mcp": {
        "manifest": HEAD.replace('os = []', 'os = []') + '\n[[mcp_servers]]\nname = "{name}"\ncommand = "python3"\nargs = ["${{PKG}}/server/server.py"]\nenv = {{}}\n'
                    + SCRIPT_TOML,
        "files": {"server/server.py": MCP_SERVER_PY, "scripts/install.sh": INSTALL_SH, "scripts/uninstall.sh": UNINSTALL_SH,
                  "scripts/install.ps1": INSTALL_PS1, "scripts/uninstall.ps1": UNINSTALL_PS1, "README.md": README, ".gitignore": GITIGNORE},
    },
    "tool": {
        "manifest": HEAD + '\n[bin]\n{name} = "bin/{name}.py"\n' + SCRIPT_TOML,
        "files": {"bin/{name}.py": TOOL_PY, "scripts/install.sh": INSTALL_SH, "scripts/uninstall.sh": UNINSTALL_SH,
                  "scripts/install.ps1": INSTALL_PS1, "scripts/uninstall.ps1": UNINSTALL_PS1, "README.md": README, ".gitignore": GITIGNORE},
    },
    "setup": {
        "manifest": HEAD + '''
# A setup profile changes the user's machine/config. Each step is shown to the user and applied only if they approve it,
# and every step is undone by `aihub uninstall`. Kinds: env, json_merge, file, block, command.
[[setup.steps]]
kind = "env"
name = "{Name}_HOME"
value = "${{HOME}}/.{name}"

[[setup.steps]]
kind = "file"
path = "${{HOME}}/.{name}/config.txt"
content = "hub=${{HUB}}\\n"
''',
        "files": {"README.md": README, ".gitignore": GITIGNORE},
    },
}


def render(typ, name, description=""):
    """-> (manifest_text, {path: content}, set_of_executable_paths) for a starter project."""
    if typ not in TEMPLATES:
        raise ValueError("unknown type %r; choose from %s" % (typ, ", ".join(sorted(TEMPLATES))))
    title = name.replace("-", " ").replace("_", " ").title()
    desc = description or "%s - describe what it does in one sentence" % title
    sub = dict(name=name, title=title, Name=name.replace("-", "_").upper(), type=typ, description=desc.replace('"', "'"))
    t = TEMPLATES[typ]
    files = {p.format(**sub): c.format(**sub) for p, c in t["files"].items()}
    execs = {p.format(**sub) for p in EXEC} & set(files)
    return t["manifest"].format(**sub), files, execs
