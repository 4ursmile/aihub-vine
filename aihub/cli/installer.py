import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile

from ..core import archive, manifest as M, version as V
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


def _spec(dep):
    for i, ch in enumerate(dep):
        if ch in "<>=!~":
            return dep[:i].strip(), dep[i:].strip()
    return dep.strip(), ""


def install(name, spec="", assume_yes=False, tools=None, _seen=None):
    _seen = _seen or set()
    name = name.lower()
    if name in _seen:
        return
    _seen.add(name)
    st = state()
    with ui.Spinner("Resolving %s" % name) as sp:
        r = api.call("GET", "/resolve?name=%s&spec=%s" % (name, spec.replace("=", "%3D").replace(">", "%3E").replace("<", "%3C")))
        sp.text("Resolved %s %s" % (name, r["version"]))
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
    for dep in req["packages"]:
        dn, ds = _spec(dep)
        cur = st["packages"].get(dn)
        if not (cur and V.satisfies(cur["version"], ds)):
            ui.step("dependency %s" % ui.accent(dep))
            install(dn, ds, assume_yes, tools, _seen)
    old = state()["packages"].get(name)
    if old:
        uninstall(name, quiet=True)
    root = paths.ensure("packages")
    with tempfile.TemporaryDirectory() as td:
        fn = os.path.join(td, r["url"].rsplit("/", 1)[-1])
        pg = ui.Progress("downloading")
        try:
            api.download(r["url"], fn, r["sha256"], progress=pg)
        finally:
            pg.finish()
        ui.good("Downloaded and verified (sha256)")
        with ui.Spinner("Unpacking"):
            dest = os.path.join(root, name)
            shutil.rmtree(dest, ignore_errors=True)
            top = archive.safe_extract(fn, os.path.join(td, "x"))
            shutil.move(top, dest)
    rec = {"version": r["version"], "path": dest, "reverts": [], "shims": [], "venv": None}
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
    bindir = paths.ensure("bin")
    for cmdname, rel in m["bin"].items():
        target = os.path.realpath(os.path.join(dest, rel))
        if not target.startswith(os.path.realpath(dest) + os.sep) or not os.path.isfile(target):
            raise api.ApiError("[bin] %s points outside the package or to a missing file: %s" % (cmdname, rel))
        rec["shims"].append(write_shim(bindir, cmdname, target, rec["venv"]))
    comps = (m["skills"], m["agents"], m["mcp_servers"])
    if any(comps):
        avail = integrations.detected()
        chosen = [integrations.TOOLS[t] for t in tools] if tools else (
            avail if assume_yes else [t for t in avail if input("Register into %s? [y/N] " % t.name).lower().startswith("y")])
        for t in chosen:
            try:
                for s in m["skills"]:
                    rec["reverts"].append(t.add_skill(s["name"], os.path.join(dest, s["path"])))
                for a in m["agents"]:
                    rec["reverts"].append(t.add_agent(a["name"], os.path.join(dest, a["path"])))
                for s in m["mcp_servers"]:
                    spec = {"command": s["command"], "args": [x.replace("${PKG}", dest) for x in s.get("args", [])],
                            "env": s.get("env", {})}
                    rec["reverts"].append(t.add_mcp(s["name"], spec))
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
    if steps:
        ctx = {"HOME": os.path.expanduser("~"), "PKG": dest, "HUB": paths.config()["hub"]}
        ok = lambda text: assume_yes or input("  setup: %s\n  apply? [y/N] " % text).lower().startswith("y")
        rec["setup_reverts"] = setupmod.apply(name, steps, ctx, ok)
    rec["uninstall_script"] = m["scripts"].get("uninstall_" + OS)
    st = state()
    st["packages"][name] = rec
    paths.save("state.json", st)
    telemetry.record({"kind": "update" if old else "install", "package": name, "version": r["version"],
                      "client_id": hooks.client_id()})
    ui.celebrate("%s %s is ready" % (name, r["version"]))
    if rec["components"]:
        ui.info("adds: " + ", ".join(rec["components"]))
    if rec["shims"]:
        ui.info("commands: " + ", ".join(os.path.basename(x) for x in rec["shims"]))
    ui.info("restart your AI tool to pick it up. Remove any time with: aihub uninstall %s" % name)


def uninstall(name, quiet=False):
    st = state()
    rec = st["packages"].get(name.lower())
    if not rec:
        raise api.ApiError("%s is not installed" % name)
    if rec.get("uninstall_script"):
        try:
            run_script(rec["uninstall_script"], rec["path"], check=False)
        except api.ApiError as e:                    # a broken uninstall script must not block removing the package
            ui.note("uninstall script skipped: %s" % e)
    for rv in reversed(rec["reverts"]):
        integrations.revert(rv)
    for rv in reversed(rec.get("setup_reverts", [])):
        setupmod.revert(rv)
    for s in rec["shims"]:
        if os.path.exists(s):
            os.remove(s)
    shutil.rmtree(rec["path"], ignore_errors=True)
    if rec.get("venv"):
        shutil.rmtree(rec["venv"], ignore_errors=True)
    del st["packages"][name.lower()]
    paths.save("state.json", st)
    if not quiet:
        telemetry.record({"kind": "uninstall", "package": name, "client_id": hooks.client_id()})
        ui.celebrate("%s uninstalled" % name)
