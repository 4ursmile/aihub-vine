import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from ..core import archive, manifest as M, naming, version as V
from . import api, hooks, integrations, paths, setup as setupmod, telemetry, ui

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


def write_shim(bindir, cmdname, target, venv=None):
    """Create a launcher for a [bin] command. POSIX: an sh script. Windows: a .cmd file."""
    if OS == "windows":
        shim = os.path.join(bindir, cmdname + ".cmd")
        if target.endswith(".py"):
            py = os.path.join(venv, "Scripts", "python.exe") if venv else "python"
            body = '@echo off\r\n"%s" "%s" %%*\r\n' % (py, target)
        elif target.lower().endswith(".ps1"):
            body = '@echo off\r\npowershell -NoProfile -ExecutionPolicy Bypass -File "%s" %%*\r\n' % target
        else:
            body = '@echo off\r\n"%s" %%*\r\n' % target
        open(shim, "w", newline="").write(body)
        return shim
    shim = os.path.join(bindir, cmdname)
    interp = os.path.join(venv, "bin", "python") if venv else "/usr/bin/env python3"
    with open(shim, "w") as f:
        f.write('#!/bin/sh\nexec %s "%s" "$@"\n' % (interp, target) if target.endswith(".py") else '#!/bin/sh\nexec "%s" "$@"\n' % target)
    os.chmod(shim, os.stat(shim).st_mode | stat.S_IEXEC)
    os.chmod(target, os.stat(target).st_mode | stat.S_IEXEC)
    return shim


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

def _legacy_plan(roots, have):
    """Resolution for hubs without /resolve/tree: one request per package."""
    seen, out = set(), []

    def go(name, spec, root):
        name = naming.normalize(name)
        if name in seen:
            return
        seen.add(name)
        r = api.call("GET", "/resolve?name=%s&spec=%s" % (name, urllib.parse.quote(spec or "")))
        deps = (r["manifest"].get("requires") or {}).get("packages") or []
        for d in deps:
            dn, ds = _spec(d)
            if naming.normalize(dn) not in have or not V.satisfies(have[naming.normalize(dn)], ds):
                go(dn, ds, False)
        out.append(dict(r, requires=list(deps), root=root))
    for x in roots:
        go(x["name"], x.get("spec", ""), True)
    return out


def plan(roots, installed=None):
    """-> [resolved package dicts, dependencies first]. One round trip on current hubs.
    roots: [{"name", "spec"}]; installed: {name: version} of packages that need no work if they satisfy a constraint."""
    try:
        return api.call("POST", "/resolve/tree", {"roots": roots, "installed": installed or {}})["packages"]
    except api.ApiError as e:
        msg = str(e)
        if msg.startswith("405 ") or (msg.startswith("404 ") and msg.rstrip().endswith("Not Found")):
            return _legacy_plan(roots, installed or {})
        raise


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
        rec["shims"].append(write_shim(bindir, cmdname, target, rec.get("venv")))
    rec["reverts"] = []
    rec["tools"] = []
    comps = (m["skills"], m["agents"], m["mcp_servers"])
    if any(comps):
        avail = integrations.detected()
        chosen = [integrations.TOOLS[t] for t in tools] if tools else (
            avail if assume_yes else [t for t in avail if input("Register into %s? [y/N] " % t.name).lower().startswith("y")])
        for t in chosen:
            try:
                reverts = []
                for s in m["skills"]:
                    reverts.append(t.add_skill(s["name"], os.path.join(dest, s["path"])))
                for a in m["agents"]:
                    reverts.append(t.add_agent(a["name"], os.path.join(dest, a["path"])))
                for s in m["mcp_servers"]:
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
                         + ["mcp:" + x["name"] for x in m["mcp_servers"]])
    rec["reverts"] = [x for x in rec["reverts"] if x]
    steps = (m["setup"] or {}).get("steps", [])
    rec["setup_reverts"] = []
    if steps:
        ctx = {"HOME": os.path.expanduser("~"), "PKG": dest, "HUB": paths.config()["hub"]}
        ok = lambda text: assume_yes or input("  setup: %s\n  apply? [y/N] " % text).lower().startswith("y")
        rec["setup_reverts"] = setupmod.apply(name, steps, ctx, ok)
    rec["enabled"] = True


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
    fn = os.path.join(td, r["name"] + "-" + r["url"].rsplit("/", 1)[-1])
    api.download(r["url"], fn, r["sha256"])
    return fn


def _install_one(r, assume_yes, tools, requested, archive_path=None):
    name = r["name"].lower()
    m = M.normalize(r["manifest"]) if r["manifest"].get("package") else None
    if not m:
        raise api.ApiError("server returned invalid manifest")
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
    root = paths.ensure("packages")
    with tempfile.TemporaryDirectory() as td:
        fn = archive_path
        if not fn:
            fn = os.path.join(td, r["url"].rsplit("/", 1)[-1])
            pg = ui.Progress("downloading %s" % name)
            try:
                api.download(r["url"], fn, r["sha256"], progress=pg)
            finally:
                pg.finish()
        ui.good("%s %s downloaded and verified (sha256)" % (name, r["version"]))
        with ui.Spinner("Unpacking"):
            dest = os.path.join(root, name)
            shutil.rmtree(dest, ignore_errors=True)
            top = archive.safe_extract(fn, os.path.join(td, "x"))
            shutil.move(top, dest)
    rec = {"version": r["version"], "path": dest, "reverts": [], "shims": [], "venv": None, "sha256": r["sha256"],
           "deps": list(req["packages"]), "requested": bool(requested or (old or {}).get("requested"))}
    py = m["python"]
    if py["requires"] or py["requirements_file"]:
        venv = paths.ensure("venvs", name)
        with ui.Spinner("Setting up Python environment"):
            subprocess.check_call([sys.executable, "-m", "venv", venv])
            pip = os.path.join(venv, "Scripts", "pip.exe") if OS == "windows" else os.path.join(venv, "bin", "pip")
            cmd = [pip, "install", "-q"] + py["requires"]
            if py["requirements_file"]:
                cmd += ["-r", os.path.join(dest, py["requirements_file"])]
            subprocess.check_call(cmd, stdout=subprocess.DEVNULL)
        rec["venv"] = venv
    script = m["scripts"].get("install_" + OS)
    if script:
        _, _, shown = script_command(script, dest)             # validates the path before asking the user to approve it
        if assume_yes or input("Run install script for %s?\n  %s\n[y/N] " % (name, shown)).lower().startswith("y"):
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
    """Install resolved packages in order. Several archives are downloaded in parallel first."""
    if not pkgs:
        return []
    roots = {naming.normalize(x) for x in roots}
    cache, done = None, []
    try:
        if len(pkgs) > 1:
            cache = tempfile.mkdtemp(prefix="aihub-dl-")
            with ui.Spinner("Downloading %d packages" % len(pkgs)) as sp:
                with ThreadPoolExecutor(max_workers=min(4, len(pkgs))) as ex:
                    files = dict(zip([p["name"] for p in pkgs], ex.map(lambda p: _fetch(p, cache), pkgs)))
                sp.text("Downloaded %d packages" % len(pkgs))
        else:
            files = {}
        for r in pkgs:
            if len(pkgs) > 1:
                ui.step("%s %s" % (ui.accent(r["name"]), r["version"]) + ("" if r["name"] in roots else ui.dim("  (dependency)")))
            _install_one(r, assume_yes, tools, r["name"] in roots, files.get(r["name"]))
            done.append(r)
    finally:
        if cache:
            shutil.rmtree(cache, ignore_errors=True)
    return done


def install(name, spec="", assume_yes=False, tools=None):
    name = naming.normalize(name)
    st = state()
    cur = st["packages"].get(name)
    if cur and not enabled(cur) and V.satisfies(cur["version"], spec) and os.path.isdir(cur["path"]):
        latest = None
        with ui.Spinner("Checking %s" % name):
            try:
                latest = api.call("GET", "/resolve?name=%s&spec=%s" % (name, urllib.parse.quote(spec)))["version"]
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
    for rec_name in [p["name"] for p in done] or [name]:
        _enable_deps(rec_name, assume_yes, tools)
    r = state()["packages"][name]
    ui.celebrate("%s %s is ready" % (name, r["version"]))
    if r.get("components"):
        ui.info("adds: " + ", ".join(r["components"]))
    if r.get("shims"):
        ui.info("commands: " + ", ".join(os.path.basename(x) for x in r["shims"]))
    ui.info("restart your AI tool to pick it up. Switch off any time with: aihub disable %s  (remove: aihub uninstall %s)" % (name, name))


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
    return tree_lines([root], lambda n: [naming.normalize(_spec(d)[0]) for d in by[n]["requires"]] if n in by else [], label), pkgs


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
        items.append({"name": n, "version": r["version"], "sha256": r.get("sha256", ""), "requested": bool(r.get("requested")),
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
    """Compare a lock with this machine. -> (resolved plan, install list, enable/disable changes, extras)."""
    want = {naming.normalize(x["name"]): x for x in lock["packages"]}
    pkgs = plan([{"name": n, "spec": "==" + x["version"]} for n, x in want.items()], {})
    by = {p["name"]: p for p in pkgs}
    for n, x in want.items():
        got = by.get(n)
        if not got or got["version"] != x["version"]:
            raise api.ApiError("hub cannot provide %s==%s" % (n, x["version"]))
        if x.get("sha256") and got["sha256"] != x["sha256"]:
            raise api.ApiError("%s %s on the hub does not match the lock file (sha256 differs); refusing to install" % (n, x["version"]))
    st = state()["packages"]
    todo = []
    for p in pkgs:
        cur = st.get(p["name"])
        if not cur or cur["version"] != p["version"] or (cur.get("sha256") and cur["sha256"] != p["sha256"]) or not os.path.isdir(cur["path"]):
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
            elif n in state()["packages"] and enabled(state()["packages"][n]) and state()["packages"][n].get("tools") \
                    and V.compare(api.call("GET", "/resolve?name=%s" % n)["version"], state()["packages"][n]["version"]) <= 0:
                out.append((n, "already installed"))
            else:
                install(n, "", True, tools)
                out.append((n, "installed"))
        except (api.ApiError, ValueError, OSError) as e:
            out.append((n, "failed: %s" % e))
    return out
