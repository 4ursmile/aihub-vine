import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from ..core import archive, manifest as M, naming, version as V
from . import api, hooks, integrations, paths, registry, setup as setupmod, telemetry, ui

OS = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}.get(platform.system(), "linux")


SCRIPT_EXT = (".sh", ".ps1", ".cmd", ".bat", ".py")


def script_command(entry, pkg_dir, os_name=None):
    """Turn a manifest script entry into (argv|string, use_shell, description).
    - a path to a file inside the package (.sh/.ps1/.cmd/.bat/.py)  -> run with the right interpreter, no shell parsing
    - anything else                                                 -> an inline shell command (legacy behaviour)
    The path must stay inside the package: the manifest comes from the registry and is not trusted."""
    os_name = os_name or OS
    e = entry.strip()
    if e.lower().endswith(SCRIPT_EXT) and " " not in e:
        full = os.path.realpath(os.path.join(pkg_dir, e))
        if not full.startswith(os.path.realpath(pkg_dir) + os.sep):
            raise api.ApiError("script path escapes the package: %s" % e)
        if not os.path.isfile(full):
            raise api.ApiError("script not found in package: %s" % e)
        ext = full.lower().rsplit(".", 1)[-1]
        if ext == "ps1":
            return (["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", full], False, e)
        if ext in ("cmd", "bat"):
            return (["cmd", "/c", full], False, e)
        if ext == "py":
            return ([sys.executable, full], False, e)
        return (["sh", full], False, e)
    return (e, True, e)


def run_script(entry, pkg_dir, check=True):
    cmd, shell, _ = script_command(entry, pkg_dir)
    env = dict(os.environ, AIHUB_PACKAGE_DIR=pkg_dir)
    if check:
        subprocess.check_call(cmd, shell=shell, cwd=pkg_dir, env=env)
    else:
        subprocess.call(cmd, shell=shell, cwd=pkg_dir, env=env)


def _fwd(path):
    """Forward slashes: Git Bash / MSYS / WSL-style shells read `C:/x/y`, not `C:\\x\\y`."""
    return str(path).replace("\\", "/")


def _sh_twin(shim, body):
    """Windows: the extensionless sh script next to a .cmd. Git Bash (what Claude Code and Codex use on Windows) ignores .cmd files, so without it the command is 'not found'. LF endings."""
    with open(shim, "w", newline="\n") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    return shim


def write_shim(bindir, cmdname, target, venv=None):
    """Create a launcher for a [bin] command. POSIX: an sh script. Windows: a .cmd (cmd/PowerShell) AND an extensionless sh script (Git Bash); both are returned."""
    low = target.lower()
    if OS == "windows":
        shim = os.path.join(bindir, cmdname + ".cmd")
        py = os.path.join(venv, "Scripts", "python.exe") if venv else "python"
        if low.endswith(".py"):
            body = '@echo off\r\n"%s" "%s" %%*\r\n' % (py, target)
            sh = 'exec "%s" "%s" "$@"' % (_fwd(py) if venv else "python", _fwd(target))
        elif low.endswith(".ps1"):
            body = '@echo off\r\npowershell -NoProfile -ExecutionPolicy Bypass -File "%s" %%*\r\n' % target
            sh = 'exec powershell -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "%s" 2>/dev/null || echo "%s")" "$@"' % (_fwd(target), target)
        elif low.endswith((".cmd", ".bat")):
            body = '@echo off\r\ncall "%s" %%*\r\n' % target
            sh = 'exec cmd //c "$(cygpath -w "%s" 2>/dev/null || echo "%s")" "$@"' % (_fwd(target), target)
        elif low.endswith(".sh"):
            body = '@echo off\r\nsh "%s" %%*\r\n' % _fwd(target)
            sh = 'exec sh "%s" "$@"' % _fwd(target)
        else:
            body = '@echo off\r\n"%s" %%*\r\n' % target
            sh = 'exec "%s" "$@"' % _fwd(target)
        open(shim, "w", newline="").write(body)
        return [shim, _sh_twin(os.path.join(bindir, cmdname), sh)]
    shim = os.path.join(bindir, cmdname)
    interp = os.path.join(venv, "bin", "python") if venv else "/usr/bin/env python3"
    with open(shim, "w") as f:
        f.write('#!/bin/sh\nexec %s "%s" "$@"\n' % (interp, target) if low.endswith(".py") else '#!/bin/sh\nexec "%s" "$@"\n' % target)
    os.chmod(shim, os.stat(shim).st_mode | stat.S_IEXEC)
    os.chmod(target, os.stat(target).st_mode | stat.S_IEXEC)
    return [shim]


def _pip_shims(venv, dest, name):
    """Console scripts the package's pyproject.toml defines ([project.scripts]) are linked into ~/.aihub/bin, so they run directly from the venv."""
    bindir = paths.ensure("bin")
    scripts = os.path.join(venv, "Scripts" if OS == "windows" else "bin")
    try:
        with open(os.path.join(dest, "pyproject.toml"), encoding="utf-8") as f:
            text = f.read()
        from ..core import toml_lite
        d = toml_lite.loads(text)
    except Exception:
        return []
    names = list(((d.get("project") or {}).get("scripts") or {}).keys()) + list((((d.get("tool") or {}).get("poetry") or {}).get("scripts") or {}).keys())
    out = []
    for cmdname in names:
        exe = os.path.join(scripts, cmdname + (".exe" if OS == "windows" else ""))
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(cmdname)) or not os.path.isfile(exe):
            continue
        if OS == "windows":
            shim = os.path.join(bindir, cmdname + ".cmd")
            open(shim, "w", newline="").write('@echo off\r\n"%s" %%*\r\n' % exe)
            out.append(shim)
            out.append(_sh_twin(os.path.join(bindir, cmdname), 'exec "%s" "$@"' % _fwd(exe)))
        else:
            shim = os.path.join(bindir, cmdname)
            with open(shim, "w") as f:
                f.write('#!/bin/sh\nexec "%s" "$@"\n' % exe)
            os.chmod(shim, os.stat(shim).st_mode | stat.S_IEXEC)
            out.append(shim)
    return out


def state():
    return paths.load("state.json", {"packages": {}})


def save_state(st):
    paths.save("state.json", st)


_spec = V.split_spec


def enabled(rec):
    return rec.get("enabled", True)


def dep_names(rec):
    return [naming.normalize(_spec(d)[0]) for d in rec.get("deps", [])]


def constraint_on(st, name):
    """Combined version constraint that enabled installed packages put on `name` ('' = none)."""
    out = []
    for n, r in st["packages"].items():
        if enabled(r):
            for d in r.get("deps", []):
                dn, ds = _spec(d)
                if naming.normalize(dn) == name and ds:
                    out.append(ds)
    return ",".join(out)


def dependents(st, name, only_enabled=True):
    """Installed packages that declare `name` as a dependency."""
    return sorted(n for n, r in st["packages"].items() if name in dep_names(r) and (enabled(r) or not only_enabled))


# ---------- dependency resolution

def plan(roots, installed=None):
    """-> [resolved package dicts, dependencies first], from the git-backed index."""
    return registry.plan(roots, installed or {})


def installed_versions(st=None):
    return {n: r["version"] for n, r in (st or state())["packages"].items()}


# ---------- activation: everything that touches the AI tools or PATH (and so can be switched off without deleting files)

def _activate(rec, m, dest, tools, assume_yes, name):
    bindir = paths.ensure("bin")
    rec["shims"] = []
    for cmdname, rel in m["bin"].items():
        target = os.path.realpath(os.path.join(dest, rel))
        if not target.startswith(os.path.realpath(dest) + os.sep) or not os.path.isfile(target):
            raise api.ApiError("[bin] %s points outside the package or to a missing file: %s" % (cmdname, rel))
        rec["shims"].extend(write_shim(bindir, cmdname, target, rec.get("venv")))
    rec["reverts"] = []
    rec["tools"] = []
    kind = m["package"]["type"]
    is_bot = kind in M.RUNNABLE                      # bot or service: started by `aihub run`
    if is_bot:
        rec["bot_home"] = bot_home(name, kind)       # its own MCP servers are never registered into the AI tools
        write_bot_files(name, m, dest)
    comps = (m["skills"], m["agents"], [] if is_bot else m["mcp_servers"])
    if any(comps):
        avail = integrations.detected()
        chosen = [integrations.TOOLS[t] for t in tools] if tools else (
            avail if assume_yes else [t for t in avail if ui._ask("Register into %s? [y/N] " % t.name).lower().startswith("y")])
        for t in chosen:
            try:
                reverts = []
                for s in m["skills"]:
                    reverts.append(t.add_skill(s["name"], os.path.join(dest, s["path"])))
                for a in m["agents"]:
                    reverts.append(t.add_agent(a["name"], os.path.join(dest, a["path"])))
                for s in ([] if is_bot else m["mcp_servers"]):
                    spec = {"command": s["command"], "args": [x.replace("${PKG}", dest) for x in s.get("args", [])],
                            "env": s.get("env", {})}
                    reverts.append(t.add_mcp(s["name"], spec))
                rec["reverts"] += reverts
                rec["tools"].append(t.name)
                ui.good("Registered with %s" % t.label)
            except ValueError as e:
                ui.note("%s: %s" % (t.label, e))
        # usage hooks: auto-install once per tool, so `use` events flow without any manual step
        for label, status in hooks.ensure([t.name for t in chosen]):
            ui.info("usage hook (%s): %s" % (label, status))
    rec["components"] = (["skill:" + x["name"] for x in m["skills"]] + ["agent:" + x["name"] for x in m["agents"]]
                         + ["mcp:" + x["name"] for x in ([] if is_bot else m["mcp_servers"])]
                         + ([kind + ":" + name] if is_bot else []))
    rec["reverts"] = [x for x in rec["reverts"] if x]
    steps = (m["setup"] or {}).get("steps", [])
    rec["setup_reverts"] = []
    if steps:
        ctx = {"HOME": os.path.expanduser("~"), "PKG": dest, "HUB": paths.config()["hub"]}
        ok = lambda text: assume_yes or ui._ask("  setup: %s\n  apply? [y/N] " % text).lower().startswith("y")
        rec["setup_reverts"] = setupmod.apply(name, steps, ctx, ok)
    rec["enabled"] = True


def bot_home(name, kind="bot"):
    """Per-bot (or per-service) folder that survives updates and holds the user's own settings: .env, mcp.json, memory."""
    return paths.p("services" if kind == "service" else "bots", name)


def write_bot_files(name, m, dest):
    """Create <bot_home>/.env (never overwritten, so edits survive updates) and mcp.json (rewritten from the manifest)."""
    kind = m["package"]["type"]
    home = paths.ensure("services" if kind == "service" else "bots", name)
    envf = os.path.join(home, ".env")
    if not os.path.exists(envf):
        lines = ["# %s settings. `aihub run %s` loads this file. Lines: NAME=value" % (name, name)]
        for e in M.runnable(m)["env"]:
            lines.append("# %s%s" % (e["hint"] or e["name"], "" if e["required"] else " (optional)"))
            lines.append("%s=%s" % (e["name"], e["default"]))
        with open(envf, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        try:
            os.chmod(envf, 0o600)
        except OSError:
            pass
    servers = {s["name"]: {"command": s["command"], "args": [x.replace("${PKG}", dest) for x in s.get("args", [])], "env": s.get("env", {})}
               for s in m["mcp_servers"]}
    import json
    with open(os.path.join(home, "mcp.json"), "w", encoding="utf-8") as f:
        json.dump({"mcpServers": servers}, f, indent=2)


def read_env(path):
    """KEY=value lines of a .env file (comments, blank lines and surrounding quotes handled)."""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    k, v = ln.split("=", 1)
                    out[k.strip()] = v.strip().strip("\"'")
    except OSError:
        pass
    return out


def runnable_kind(name, rec):
    """'bot' / 'service' when the installed package is one `aihub run` starts, else None."""
    comps = rec.get("components", [])
    return next((k for k in M.RUNNABLE if k + ":" + name in comps), None)


def bots():
    """-> {name: (record, manifest)} for installed, enabled bots and services (everything `aihub run` can start)."""
    out = {}
    for n, r in state()["packages"].items():
        if enabled(r) and runnable_kind(n, r):
            try:
                out[n] = (r, _load_manifest(r))
            except api.ApiError:
                pass
    return out


def bot_env(name, rec, m):
    """AIHUB_* variables every bot process (the bot itself and its onboarding script) receives."""
    kind = runnable_kind(name, rec) or "bot"
    home = rec.get("bot_home") or bot_home(name, kind)
    skill_dirs, pkg_dirs = [], []
    for n, r in state()["packages"].items():
        if not enabled(r) or n == name:
            continue
        pkg_dirs.append(r["path"])
        try:
            skill_dirs += [os.path.join(r["path"], s["path"]) for s in _load_manifest(r)["skills"]]
        except api.ApiError:
            pass
    env = {"AIHUB_BOT": name, "AIHUB_BOT_DIR": rec["path"], "AIHUB_BOT_HOME": home, "AIHUB_BOT_ENV": os.path.join(home, ".env"),
           "AIHUB_BOT_MCP": os.path.join(home, "mcp.json"), "AIHUB_KNOWLEDGE_DIR": paths.p("knowledge"),
           "AIHUB_HUB": paths.config()["hub"], "AIHUB_SKILL_DIRS": os.pathsep.join(skill_dirs), "AIHUB_PACKAGE_DIRS": os.pathsep.join(pkg_dirs)}
    if kind == "service":
        env.update({"AIHUB_SERVICE": name, "AIHUB_SERVICE_DIR": rec["path"], "AIHUB_SERVICE_HOME": home,
                    "AIHUB_SERVICE_ENV": os.path.join(home, ".env")})
    if rec.get("venv"):
        env["VIRTUAL_ENV"] = rec["venv"]
    return env


def onboard(name, assume_yes=False, force=False):
    """OPTIONAL guided credential setup: runs the bot's [bot] onboard script. Install only offers it (Y/n, terminal only, never with --yes);
    `aihub onboard` runs it on request (force). -> True when it ran and finished."""
    found = bots()
    if name not in found:
        raise api.ApiError("%s is not an installed bot or service" % name)
    rec, m = found[name]
    kind = m["package"]["type"] if m["package"]["type"] in M.RUNNABLE else "bot"
    script = M.runnable(m)["onboard"]
    if not script:
        if force:
            raise api.ApiError("%s has no guided setup ([%s] onboard)" % (name, kind))
        return False
    home = rec.get("bot_home") or bot_home(name, kind)
    if not force:
        if assume_yes or not ui.INTERACTIVE:
            ui.info("guided setup (optional):  aihub onboard %s" % name)
            return False
        if ui._ask("Set up %s credentials now (guided, optional)? [Y/n] " % name).lower() not in ("", "y", "yes"):
            ui.info("later:  aihub onboard %s   or edit %s" % (name, os.path.join(home, ".env")))
            return False
    cmd, shell, _ = script_command(script, rec["path"])
    if isinstance(cmd, list) and cmd[0] == sys.executable and rec.get("venv"):
        cmd[0] = os.path.join(rec["venv"], "Scripts" if OS == "windows" else "bin", "python")
    code = subprocess.call(cmd, shell=shell, cwd=home, env=dict(os.environ, **bot_env(name, rec, m)))
    if code:
        ui.note("setup did not finish (exit %d). Run it again: aihub onboard %s  - or edit %s by hand" % (code, name, os.path.join(home, ".env")))
    return code == 0


def _venv_bin(venv):
    return os.path.join(venv, "Scripts" if OS == "windows" else "bin")


def _venv_python(venv):
    for n in (("python.exe", "python3.exe") if OS == "windows" else ("python", "python3")):
        f = os.path.join(_venv_bin(venv), n)
        if os.path.isfile(f):
            return f
    return None


def check_venv(venv=None, python=None):
    """Validate `aihub run --venv PATH` / `--python PATH` -> {"venv": dir|None, "python": interpreter}. Raises ApiError."""
    if venv:
        d = os.path.abspath(os.path.expanduser(str(venv)))
        if not os.path.isdir(d):
            raise api.ApiError("--venv %s: no such folder" % venv)
        py = _venv_python(d)
        if not py:
            raise api.ApiError("--venv %s: no python in %s" % (venv, _venv_bin(d)))
        return {"venv": d, "python": py}
    f = os.path.abspath(os.path.expanduser(str(python)))
    if not (os.path.isfile(f) and os.access(f, os.X_OK)):
        raise api.ApiError("--python %s: not an executable file" % python)
    return {"venv": None, "python": f}


def remember_venv(name, choice):
    """Save (dict) or clear (None) the venv/python `aihub run NAME` uses for this package."""
    st = state()
    rec = st["packages"].get(name)
    if rec is None:
        return
    if choice:
        rec["run_venv"] = choice
    else:
        rec.pop("run_venv", None)
    save_state(st)


def run_python(name, rec, home, override=None):
    """Which interpreter `aihub run` uses -> {"python", "venv", "how"}. Order: explicit choice > remembered choice (rec run_venv) >
    .venv / venv in the bot home or the package folder > the package's own venv > the python aihub runs with."""
    for how, c in (("flag", override), ("remembered", rec.get("run_venv"))):
        if c and c.get("python") and os.path.isfile(c["python"]):
            return {"python": c["python"], "venv": c.get("venv"), "how": how}
    for base in (home, rec.get("path")):
        for sub in (".venv", "venv"):
            d = os.path.join(base or "", sub)
            if base and os.path.isdir(d) and _venv_python(d):
                return {"python": _venv_python(d), "venv": d, "how": "found " + sub}
    if rec.get("venv") and _venv_python(rec["venv"]):
        return {"python": _venv_python(rec["venv"]), "venv": rec["venv"], "how": "package"}
    return {"python": sys.executable, "venv": None, "how": "aihub"}


def bot_command(name, args=(), override=None):
    """-> (cmd, shell, cwd, env) that starts an installed bot with its .env, knowledge, MCP list and skill folders in the environment.
    Raises when it is not installed or a required setting is empty. `aihub run` builds it again on every (re)start, so .env edits apply."""
    found = bots()
    if name not in found:
        raise api.ApiError("%s is not an installed bot or service (find one: aihub search --type bot / --type service, then aihub install <name>)" % name)
    rec, m = found[name]
    spec = M.runnable(m)
    home = rec.get("bot_home") or bot_home(name, runnable_kind(name, rec) or "bot")
    envf = os.path.join(home, ".env")
    env = dict(os.environ)
    env.update({k: v for k, v in read_env(envf).items() if v})
    missing = [e for e in spec["env"] if e["required"] and not env.get(e["name"])]
    if missing:
        ui.note("%s needs settings before it can run. Edit this file:" % name)
        ui.out("    " + envf)
        for e in missing:
            ui.out("    %s   %s" % (e["name"], ui.dim(e["hint"])))
        raise api.ApiError("missing: %s" % ", ".join(e["name"] for e in missing))
    env.update(bot_env(name, rec, m))
    cmd, shell, _ = script_command(spec["run"], rec["path"])
    py = run_python(name, rec, home, override)
    if py["venv"]:
        env["VIRTUAL_ENV"] = py["venv"]
        env["PATH"] = _venv_bin(py["venv"]) + os.pathsep + env.get("PATH", "")
    if isinstance(cmd, list) and cmd[0] == sys.executable:
        cmd[0] = py["python"]
    if isinstance(cmd, list):
        cmd = cmd + list(args)
    return cmd, shell, home, env


def run_bot(name, args=()):
    """Run an installed bot attached to this terminal (no supervisor, no log). -> exit code."""
    cmd, shell, home, env = bot_command(name, args)
    return subprocess.call(cmd, shell=shell, cwd=home, env=env)


def source_hints(name, rec):
    """Where the installed package lives and, for bots, where its settings are. Printed after every install."""
    ui.info("source:   %s" % rec["path"])
    if rec.get("bot_home"):
        ui.info("settings: %s   (credentials go here)" % os.path.join(rec["bot_home"], ".env"))
        ui.info("run it:   aihub run %s   (background; --fg for this terminal)" % name)
    nd = os.path.join(rec["path"], "nodes")
    if os.path.isdir(nd) and any(f.endswith(".node.json") for f in os.listdir(nd)):
        ui.info("nodes:    available in aihub-ui Works > Add node (group = the node's group); secret fields are stored encrypted by the UI")
    wd = os.path.join(rec["path"], "works")
    if os.path.isdir(wd) and any(f.endswith(".work.json") for f in os.listdir(wd)):
        ui.info("workflow: in aihub-ui open Works > Workflows from packages > Add to Works (fill in its secret fields; run it from a chat with /work:NAME)")


def _deactivate(rec):
    """Undo tool registrations, setup steps and PATH shims. Package files and its Python environment stay."""
    for rv in reversed(rec.get("reverts", [])):
        integrations.revert(rv)
    for rv in reversed(rec.get("setup_reverts", [])):
        setupmod.revert(rv)
    for sh in rec.get("shims", []):
        if os.path.exists(sh):
            os.remove(sh)
    rec["reverts"], rec["setup_reverts"], rec["shims"] = [], [], []


def _load_manifest(rec):
    try:
        with open(os.path.join(rec["path"], "aihub.toml"), encoding="utf-8") as f:
            return M.parse(f.read())
    except OSError:
        raise api.ApiError("package files are missing (%s); reinstall it with: aihub install <name>" % rec["path"])


# ---------- install

def _fetch(r, td):
    dest = os.path.join(td, r["name"])
    r["rev"] = registry.checkout(r, dest)
    return dest


def _install_one(r, assume_yes, tools, requested, archive_path=None):
    name = r["name"].lower()
    td = tempfile.mkdtemp()
    try:
        if archive_path:
            src = archive_path
        else:
            with ui.Spinner("Fetching %s %s" % (name, "through the hub" if registry.via_hub() or registry.on_hub(r.get("repo")) else "from git")):
                src = _fetch(r, td)
        toml = os.path.join(src, "aihub.toml")          # the repo's aihub.toml is authoritative
        m = M.normalize(M.parse(open(toml, encoding="utf-8").read()))
        req = m["requires"]
        if req["os"] and OS not in req["os"]:
            raise api.ApiError("%s supports only: %s" % (name, ", ".join(req["os"])))
        for c in req["commands"]:
            cn, hint = (c, "") if isinstance(c, str) else (c.get("name"), c.get("hint", ""))
            if not shutil.which(cn):
                ui.note("missing command '%s'. %s" % (cn, hint))
        old = state()["packages"].get(name)
        if old:
            uninstall(name, quiet=True)
        dest = os.path.join(paths.ensure("packages"), name)
        shutil.rmtree(dest, ignore_errors=True)
        shutil.move(src, dest)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    ui.good("%s %s fetched" % (name, r["version"]))
    rec = {"version": r["version"], "path": dest, "reverts": [], "shims": [], "venv": None, "rev": r.get("rev"), "repo": r.get("repo"),
           "deps": list(req["packages"]), "requested": bool(requested or (old or {}).get("requested"))}
    py = m["python"]
    if py["install"] and not os.path.isfile(os.path.join(dest, "pyproject.toml")):
        raise api.ApiError("%s sets [python] install = true but has no pyproject.toml" % name)
    if py["requires"] or py["requirements_file"] or py["install"]:
        venv = paths.ensure("venvs", name)
        with ui.Spinner("Setting up Python environment"):
            subprocess.check_call([sys.executable, "-m", "venv", venv])
            pip = os.path.join(venv, "Scripts", "pip.exe") if OS == "windows" else os.path.join(venv, "bin", "pip")
            cmd = [pip, "install", "-q"] + py["requires"]
            if py["requirements_file"]:
                cmd += ["-r", os.path.join(dest, py["requirements_file"])]
            if len(cmd) > 3:
                subprocess.check_call(cmd, stdout=subprocess.DEVNULL)
        rec["venv"] = venv
        if py["install"]:                                      # the package itself, from its pyproject.toml (not editable: the venv owns a copy)
            with ui.Spinner("Installing %s with pip" % name):
                try:
                    subprocess.check_call([pip, "install", "-q", dest], stdout=subprocess.DEVNULL)
                except subprocess.CalledProcessError:
                    shutil.rmtree(venv, ignore_errors=True)
                    raise api.ApiError("pip could not install %s from its pyproject.toml" % name)
            rec["pip_shims"] = _pip_shims(venv, dest, name)
    script = m["scripts"].get("install_" + OS)
    if script:
        _, _, shown = script_command(script, dest)             # validates the path before asking the user to approve it
        if assume_yes or ui._ask("Run install script for %s?\n  %s\n[y/N] " % (name, shown)).lower().startswith("y"):
            ui.step("running %s" % shown)
            run_script(script, dest)
            ui.good("Install script finished")
    _activate(rec, m, dest, tools, assume_yes, name)
    rec["uninstall_script"] = m["scripts"].get("uninstall_" + OS)
    st = state()
    st["packages"][name] = rec
    save_state(st)
    telemetry.record({"kind": "update" if old else "install", "package": name, "version": r["version"],
                      "client_id": hooks.client_id()})
    return rec


def install_plan(pkgs, assume_yes=False, tools=None, roots=()):
    """Install resolved packages in order. The plan is resolved first, then every package is downloaded in parallel
    (packages of one monorepo share one clone); installing itself stays sequential, dependencies first."""
    if not pkgs:
        return []
    roots = {naming.normalize(x) for x in roots}
    done, pre, td = [], {}, None
    if len(pkgs) > 1:
        td = tempfile.mkdtemp()
        ui.step("downloading %d packages in parallel" % len(pkgs))
        pre = registry.prefetch(pkgs, td)
    bad = {}                                                  # name -> error: one broken package must not hide the others
    try:
        for r in pkgs:
            if len(pkgs) > 1:
                ui.step("%s %s" % (ui.accent(r["name"]), r["version"]) + ("" if r["name"] in roots else ui.dim("  (dependency)")))
            broken = [d for d in ((r.get("requires") or {}).get("packages") or []) if naming.normalize(V.split_spec(d)[0]) in bad]
            if broken:
                ui.note("%s skipped: needs %s, which failed" % (r["name"], ", ".join(broken)))
                bad[r["name"]] = api.ApiError("%s skipped because %s failed" % (r["name"], ", ".join(broken)))
                continue
            folder, err = pre.get(r["name"], (None, None))
            try:
                if err:
                    raise err
                _install_one(r, assume_yes, tools, r["name"] in roots, archive_path=folder)
            except (api.ApiError, ValueError, OSError) as e:
                if len(pkgs) == 1:
                    raise
                ui.note("%s failed: %s" % (r["name"], e))
                bad[r["name"]] = e
                continue
            done.append(r)
    finally:
        if td:
            shutil.rmtree(td, ignore_errors=True)
    if bad:
        first = next(iter(bad.values()))
        raise first.__class__(*first.args) if len(bad) == 1 else api.ApiError(
            "%d packages failed: %s" % (len(bad), "; ".join("%s (%s)" % (n, e) for n, e in bad.items())))
    return done


def install(name, spec="", assume_yes=False, tools=None):
    name = naming.normalize(name)
    st = state()
    cur = st["packages"].get(name)
    if cur and not enabled(cur) and V.satisfies(cur["version"], spec) and os.path.isdir(cur["path"]):
        latest = None
        with ui.Spinner("Checking %s" % name):
            try:
                latest = registry.resolve(name, spec)["version"]
            except api.ApiError:
                pass
        if latest is None or V.compare(latest, cur["version"]) <= 0:      # nothing newer: just switch it back on
            enable(name, assume_yes, tools)
            return
    with ui.Spinner("Resolving %s" % name) as sp:
        pkgs = plan([{"name": name, "spec": spec}], installed_versions(st))
        sp.text("Resolved %s" % ", ".join("%s %s" % (p["name"], p["version"]) for p in pkgs))
    deps = [p["name"] for p in pkgs if p["name"] != name]
    if deps:
        ui.step("dependencies: %s" % ", ".join(ui.accent(d) for d in deps))
    done = install_plan(pkgs, assume_yes, tools, roots=[name])
    _finish(name, [p["name"] for p in done], assume_yes, tools)


def _finish(name, done_names, assume_yes, tools):
    for rec_name in done_names or [name]:
        _enable_deps(rec_name, assume_yes, tools)
    r = state()["packages"][name]
    ui.celebrate("%s %s is ready" % (name, r["version"]))
    if r.get("components"):
        ui.info("adds: " + ", ".join(r["components"]))
    source_hints(name, r)
    if runnable_kind(name, r):
        onboard(name, assume_yes)
    if r.get("shims"):
        ui.info("commands: " + ", ".join(dict.fromkeys(os.path.splitext(os.path.basename(x))[0] if x.lower().endswith(".cmd") else os.path.basename(x) for x in r["shims"])))
    ui.info("restart your AI tool to pick it up. Switch off any time with: aihub disable %s  (remove: aihub uninstall %s)" % (name, name))



def install_many(specs, assume_yes=False, tools=None):
    """specs = [(name, spec)]. ONE combined plan (shared dependencies and monorepo clones are fetched once, all downloads run in
    parallel). A name that cannot be resolved is reported and skipped; the rest still install. -> [(name, error)] for the failures."""
    st = state()
    failed, ok = [], []
    for n, sp in specs:
        n = naming.normalize(n)
        cur = st["packages"].get(n)
        if cur and not enabled(cur):                  # a disabled copy may only need switching on: the single-package path knows how
            try:
                install(n, sp, assume_yes, tools)
                ok.append(n)
            except (api.ApiError, ValueError, OSError) as e:
                failed.append((n, e))
        else:
            ok.append((n, sp))
    todo = [x for x in ok if isinstance(x, tuple)]
    if not todo:
        return failed
    have = installed_versions(st)
    with ui.Spinner("Resolving %d packages" % len(todo)) as spn:
        try:
            pkgs = plan([{"name": n, "spec": sp} for n, sp in todo], have)
        except api.ApiError:                          # find the culprits one by one, then plan the rest together
            good = []
            for n, sp in todo:
                try:
                    plan([{"name": n, "spec": sp}], have)
                    good.append((n, sp))
                except api.ApiError as e:
                    failed.append((n, e))
            todo = good
            pkgs = plan([{"name": n, "spec": sp} for n, sp in todo], have) if todo else []
        spn.text("Resolved %d packages" % len(pkgs))
    if not pkgs:
        return failed
    roots = [n for n, _ in todo]
    ui.step("installing: %s" % ", ".join(ui.accent(p["name"]) for p in pkgs))
    try:
        done = install_plan(pkgs, assume_yes, tools, roots=roots)
    except (api.ApiError, ValueError, OSError) as e:      # a package of the plan failed; the ones before it are installed
        failed.append(("plan", e))
        done = [p for p in pkgs if p["name"] in state()["packages"]]
    names = [p["name"] for p in done]
    for n in roots:
        if n in state()["packages"]:
            _finish(n, names, assume_yes, tools)
    return failed


# ---------- enable / disable

def _enable_deps(name, assume_yes, tools, _seen=None):
    """Enable every disabled dependency of `name`, dependencies first. `_seen` guards against cycles."""
    _seen = _seen if _seen is not None else set()
    rec = state()["packages"].get(name)
    if not rec:
        return
    for d in dep_names(rec):
        dr = state()["packages"].get(d)
        if dr and not enabled(dr) and d not in _seen:
            ui.step("enabling dependency %s" % ui.accent(d))
            enable(d, assume_yes, tools, _seen)


def enable(name, assume_yes=False, tools=None, _seen=None):
    name = naming.normalize(name)
    st = state()
    rec = st["packages"].get(name)
    if not rec:
        raise api.ApiError("%s is not installed (aihub install %s)" % (name, name))
    _seen = _seen if _seen is not None else set()
    _seen.add(name)
    _enable_deps(name, assume_yes, tools, _seen)
    rec = state()["packages"][name]
    if enabled(rec):
        ui.info("%s is already enabled" % name)
        return False
    m = _load_manifest(rec)
    if m["requires"]["os"] and OS not in m["requires"]["os"]:
        raise api.ApiError("%s supports only: %s" % (name, ", ".join(m["requires"]["os"])))
    _activate(rec, m, rec["path"], tools or rec.get("disabled_tools") or None, assume_yes, name)
    rec.pop("disabled_tools", None)
    st = state()
    st["packages"][name] = rec
    save_state(st)
    ui.celebrate("%s %s enabled" % (name, rec["version"]))
    if rec.get("components"):
        ui.info("restart your AI tool to pick it up")
    return True


def disable(name, force=False):
    name = naming.normalize(name)
    st = state()
    rec = st["packages"].get(name)
    if not rec:
        raise api.ApiError("%s is not installed" % name)
    if not enabled(rec):
        ui.info("%s is already disabled" % name)
        return False
    users = dependents(st, name)
    if users and not force:
        raise api.ApiError("%s is needed by enabled package%s: %s (disable those first, or use --force)"
                           % (name, "s" if len(users) > 1 else "", ", ".join(users)))
    _deactivate(rec)
    rec["enabled"] = False
    rec["disabled_tools"] = rec.get("tools", [])
    st["packages"][name] = rec
    save_state(st)
    ui.celebrate("%s disabled" % name)
    ui.info("files are kept; switch back on with: aihub enable %s" % name)
    return True


# ---------- uninstall

def uninstall(name, quiet=False):
    st = state()
    rec = st["packages"].get(name.lower())
    if not rec:
        raise api.ApiError("%s is not installed" % name)
    users = dependents(st, name.lower(), only_enabled=False)
    if users and not quiet:
        ui.note("still needed by: %s" % ", ".join(users))
    if rec.get("uninstall_script") and enabled(rec):
        try:
            run_script(rec["uninstall_script"], rec["path"], check=False)
        except api.ApiError as e:                    # a broken uninstall script must not block removing the package
            ui.note("uninstall script skipped: %s" % e)
    _deactivate(rec)
    shutil.rmtree(rec["path"], ignore_errors=True)
    for sh in rec.get("pip_shims") or []:
        try:
            os.remove(sh)
        except OSError:
            pass
    if rec.get("venv"):
        shutil.rmtree(rec["venv"], ignore_errors=True)
    del st["packages"][name.lower()]
    save_state(st)
    if not quiet:
        telemetry.record({"kind": "uninstall", "package": name, "client_id": hooks.client_id()})
        ui.celebrate("%s uninstalled" % name)


# ---------- dependency tree

def tree_lines(roots, children, label):
    """Render a forest. children(name)->[name]; label(name)->str. Repeated subtrees are marked (*) and not expanded."""
    lines, expanded = [], set()
    U = ui.U

    def walk(name, prefix, last, top, path):
        branch = "" if top else (("└─ " if last else "├─ ") if U else ("`- " if last else "|- "))
        again = name in expanded
        cyc = name in path
        lines.append(prefix + branch + label(name) + (" (*)" if (again or cyc) and children(name) else ""))
        if again or cyc:
            return
        expanded.add(name)
        kids = children(name)
        ext = "" if top else ("   " if last else ("│  " if U else "|  "))
        for i, k in enumerate(kids):
            walk(k, prefix + ext, i == len(kids) - 1, False, path | {name})
    for r in roots:
        walk(r, "", True, True, frozenset())
    return lines


def local_tree(only=None):
    st = state()
    pk = st["packages"]
    if only:
        only = naming.normalize(only)
        if only not in pk:
            raise api.ApiError("%s is not installed (try: aihub tree %s --remote)" % (only, only))
        roots = [only]
    else:
        used = {d for r in pk.values() for d in dep_names(r)}
        roots = sorted(n for n, r in pk.items() if r.get("requested") or n not in used) or sorted(pk)

    def label(n):
        r = pk.get(n)
        if not r:
            return "%s %s" % (n, ui.err("(not installed)"))
        return "%s %s%s" % (ui.bold(n), r["version"], "" if enabled(r) else "  " + ui.warn("(disabled)"))
    return tree_lines(roots, lambda n: [d for d in dep_names(pk[n])] if n in pk else [], label)


def remote_tree(name, spec=""):
    pkgs = plan([{"name": naming.normalize(name), "spec": spec}], {})
    by = {p["name"]: p for p in pkgs}
    have = installed_versions()

    def label(n):
        p = by.get(n)
        if not p:
            return n
        mark = ui.dim("(installed)") if have.get(n) == p["version"] else (ui.dim("(installed %s)" % have[n]) if n in have else ui.accent("(new)"))
        return "%s %s  %s" % (ui.bold(n), p["version"], mark)
    root = naming.normalize(name)
    return tree_lines([root], lambda n: [naming.normalize(_spec(d)[0]) for d in by[n]["requires"]["packages"]] if n in by else [], label), pkgs


# ---------- lock / sync

LOCK_VERSION = 1


def _topo(pk):
    order, seen = [], set()

    def go(n):
        if n in seen or n not in pk:
            return
        seen.add(n)
        for d in sorted(dep_names(pk[n])):
            go(d)
        order.append(n)
    for n in sorted(pk):
        go(n)
    return order


def make_lock():
    pk = state()["packages"]
    items = []
    for n in _topo(pk):
        r = pk[n]
        items.append({"name": n, "version": r["version"], "rev": r.get("rev") or "", "requested": bool(r.get("requested")),
                      "enabled": enabled(r), "dependencies": sorted(r.get("deps", []))})
    return {"lockfile_version": LOCK_VERSION, "hub": paths.config()["hub"].rstrip("/"), "packages": items}


def read_lock(path):
    import json
    try:
        with open(path, encoding="utf-8") as f:
            lock = json.load(f)
    except OSError:
        raise api.ApiError("cannot read %s (create one with: aihub lock)" % path)
    except ValueError:
        raise api.ApiError("%s is not valid JSON" % path)
    if not isinstance(lock, dict) or lock.get("lockfile_version") != LOCK_VERSION or not isinstance(lock.get("packages"), list):
        raise api.ApiError("%s is not a supported lock file (expected lockfile_version %d)" % (path, LOCK_VERSION))
    for x in lock["packages"]:
        if not isinstance(x, dict) or not x.get("name") or not x.get("version"):
            raise api.ApiError("%s has an entry without name/version" % path)
        V.parse(str(x["version"]))
        naming.validate(x["name"])
    return lock


def sync_actions(lock):
    """Compare a lock with this machine. -> (resolved plan, install list, enable/disable changes, extras).
    The lock pins each package to the git commit that was installed."""
    want = {naming.normalize(x["name"]): x for x in lock["packages"]}
    pkgs = plan([{"name": n, "spec": "==" + x["version"]} for n, x in want.items()], {})
    by = {p["name"]: p for p in pkgs}
    for n, x in want.items():
        got = by.get(n)
        if not got or got["version"] != x["version"]:
            raise api.ApiError("the index cannot provide %s==%s" % (n, x["version"]))
        if x.get("rev"):
            got["repo"] = dict(got["repo"], ref=x["rev"])           # install exactly the locked commit
    st = state()["packages"]
    todo = []
    for p in pkgs:
        cur = st.get(p["name"])
        locked = want.get(p["name"], {}).get("rev")
        if not cur or cur["version"] != p["version"] or (locked and cur.get("rev") != locked) or not os.path.isdir(cur["path"]):
            todo.append(p)
    flips = [(n, x.get("enabled", True)) for n, x in want.items() if n in st and n not in {p["name"] for p in todo} and enabled(st[n]) != x.get("enabled", True)]
    extras = sorted(n for n in st if n not in want and n not in by)
    return pkgs, todo, flips, extras, want


# ---------- built-in packages (aihub-guide, aihub-package): ordinary hub packages installed on first run

def install_builtin(names=None, remove=False, tools=None):
    """Install (or remove) the built-in packages through the normal installer. -> [(name, status)]"""
    from ..core.release import BUILTIN_PACKAGES
    out = []
    for n in names or BUILTIN_PACKAGES:
        try:
            if remove:
                if n in state()["packages"]:
                    uninstall(n, quiet=True)
                    out.append((n, "removed"))
                else:
                    out.append((n, "not installed"))
            elif n in state()["packages"] and enabled(state()["packages"][n]) and (state()["packages"][n].get("tools") or not state()["packages"][n].get("components")) \
                    and V.compare(registry.resolve(n)["version"], state()["packages"][n]["version"]) <= 0:
                out.append((n, "already installed"))
            else:
                install(n, "", True, tools)
                out.append((n, "installed"))
        except (api.ApiError, ValueError, OSError) as e:
            out.append((n, "failed: %s" % e))
    return out
