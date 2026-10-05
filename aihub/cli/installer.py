import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile

from ..core import archive, manifest as M, version as V
from . import api, hooks, integrations, paths, setup as setupmod, telemetry

OS = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}.get(platform.system(), "linux")


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
    r = api.call("GET", "/resolve?name=%s&spec=%s" % (name, spec.replace("=", "%3D").replace(">", "%3E").replace("<", "%3C")))
    m = M.normalize(r["manifest"]) if r["manifest"].get("package") else None
    if not m:
        raise api.ApiError("server returned invalid manifest")
    req = m["requires"]
    if req["os"] and OS not in req["os"]:
        raise api.ApiError("%s supports only: %s" % (name, ", ".join(req["os"])))
    for c in req["commands"]:
        cn, hint = (c, "") if isinstance(c, str) else (c.get("name"), c.get("hint", ""))
        if not shutil.which(cn):
            print("! missing command '%s'. %s" % (cn, hint))
    for dep in req["packages"]:
        dn, ds = _spec(dep)
        cur = st["packages"].get(dn)
        if not (cur and V.satisfies(cur["version"], ds)):
            print("-> dependency", dep)
            install(dn, ds, assume_yes, tools, _seen)
    old = state()["packages"].get(name)
    if old:
        uninstall(name, quiet=True)
    root = paths.ensure("packages")
    with tempfile.TemporaryDirectory() as td:
        fn = os.path.join(td, r["url"].rsplit("/", 1)[-1])
        api.download(r["url"], fn, r["sha256"])
        dest = os.path.join(root, name)
        shutil.rmtree(dest, ignore_errors=True)
        top = archive.safe_extract(fn, os.path.join(td, "x"))
        shutil.move(top, dest)
    rec = {"version": r["version"], "path": dest, "reverts": [], "shims": [], "venv": None}
    py = m["python"]
    if py["requires"] or py["requirements_file"]:
        venv = paths.ensure("venvs", name)
        subprocess.check_call([sys.executable, "-m", "venv", venv])
        pip = os.path.join(venv, "bin", "pip")
        cmd = [pip, "install", "-q"] + py["requires"]
        if py["requirements_file"]:
            cmd += ["-r", os.path.join(dest, py["requirements_file"])]
        subprocess.check_call(cmd)
        rec["venv"] = venv
    script = m["scripts"].get("install_" + OS)
    if script:
        if assume_yes or input("Run install script for %s?\n  %s\n[y/N] " % (name, script)).lower().startswith("y"):
            subprocess.check_call(script, shell=True, cwd=dest)
    bindir = paths.ensure("bin")
    for cmdname, rel in m["bin"].items():
        shim = os.path.join(bindir, cmdname)
        interp = os.path.join(rec["venv"], "bin", "python") if rec["venv"] else "/usr/bin/env python3"
        target = os.path.join(dest, rel)
        with open(shim, "w") as f:
            f.write('#!/bin/sh\nexec %s "%s" "$@"\n' % (interp, target) if rel.endswith(".py")
                    else '#!/bin/sh\nexec "%s" "$@"\n' % target)
        os.chmod(shim, os.stat(shim).st_mode | stat.S_IEXEC)
        os.chmod(target, os.stat(target).st_mode | stat.S_IEXEC)
        rec["shims"].append(shim)
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
            except ValueError as e:
                print("! %s: %s" % (t.label, e))
        # usage hooks: auto-install once per tool, so `use` events flow without any manual step
        for label, status in hooks.ensure([t.name for t in chosen]):
            print("  usage hook (%s): %s" % (label, status))
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
    print("installed %s %s" % (name, r["version"]))


def uninstall(name, quiet=False):
    st = state()
    rec = st["packages"].get(name.lower())
    if not rec:
        raise api.ApiError("%s is not installed" % name)
    if rec.get("uninstall_script"):
        subprocess.call(rec["uninstall_script"], shell=True, cwd=rec["path"])
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
        print("uninstalled", name)
