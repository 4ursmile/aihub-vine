"""CLI version management: `aihub version`, `aihub upgrade`, and the throttled automatic update check."""
import hashlib
import os
import sys
import time
import zipfile

from ..core import version as V
from ..core.release import VERSION
from . import api, paths, ui

CHECK_EVERY = 24 * 3600


def by_pip():
    """True when the CLI runs from a pip install (site-packages), where there is no aihub.pyz to replace."""
    f = os.path.abspath(__file__)
    return "site-packages" in f or "dist-packages" in f


def pyz_path():
    return os.path.join(os.path.expanduser("~"), ".aihub", "bin", "aihub.pyz")


def hub_install(info):
    """pip install, hub route: put the hub's zipapp in ~/.aihub/bin; the pip entry point hands over to it when it is newer."""
    dst = pyz_path()
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    apply(info, dst)
    paths.save("update.json", {"checked": time.time(), "pyz_version": info["version"], "pyz": dst})


def pip_upgrade():
    import subprocess
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "aihub-cli"])
    if r.returncode:
        raise ValueError("pip could not upgrade aihub-cli (exit %d); run: %s -m pip install --upgrade aihub-cli" % (r.returncode, sys.executable))


def target():
    """Path of the installed zipapp, or None when run from source (nothing to replace). Tries the running
    file, the zip this module was imported from, then the install script's ~/.aihub/bin/aihub.pyz."""
    if by_pip():
        return None
    cands = []
    if sys.argv and sys.argv[0]:
        cands.append(os.path.abspath(sys.argv[0]))
    here = os.path.abspath(__file__)
    while here and os.path.dirname(here) != here:
        if os.path.isfile(here):
            cands.append(here)
            break
        here = os.path.dirname(here)
    cands.append(os.path.join(os.path.expanduser("~"), ".aihub", "bin", "aihub.pyz"))
    for p in cands:
        if os.path.isfile(p) and zipfile.is_zipfile(p):
            return p
    return None


def remote(timeout=5):
    with api._req("GET", paths.config()["hub"].rstrip("/") + "/cli/version", timeout=timeout) as r:
        import json
        return json.loads(r.read())


def _hub_url(path):
    return paths.config()["hub"].rstrip("/") + path


def apply(info, dst=None):
    """Download the hub's zipapp, verify its checksum, keep the old one as <name>.prev, then swap it in."""
    dst = dst or target()
    if not dst:
        raise ValueError("not running from an installed aihub.pyz; reinstall with the hub's install script")
    new = dst + ".new"
    try:
        api.download(_hub_url(info.get("url", "/cli/aihub.pyz")), new, info["sha256"])
        if not zipfile.is_zipfile(new):
            raise ValueError("downloaded file is not a valid aihub package")
        try:
            if os.path.exists(dst):
                os.replace(dst, dst + ".prev")
            os.replace(new, dst)
        except OSError as e:
            if os.path.exists(dst + ".prev") and not os.path.exists(dst):
                os.replace(dst + ".prev", dst)
            raise ValueError("could not replace %s (%s); re-run the install script" % (dst, e))
        try:
            os.chmod(dst, 0o755)
        except OSError:
            pass
    finally:
        for f in (new, new + ".part"):
            if os.path.exists(f):
                os.remove(f)


def cmd_version(a):
    ui.out("  aihub CLI  %s%s" % (VERSION, " (pip)" if by_pip() else ""))
    try:
        info = remote()
    except Exception:
        ui.info("hub: unreachable, cannot compare versions")
        return
    ui.out("  hub offers %s" % info["version"])
    if V.compare(info["version"], VERSION) > 0:
        ui.note("update available. run: aihub upgrade")
    else:
        ui.good("up to date")


def cmd_upgrade(a):
    if a.rollback:
        dst = target()
        if not dst or not os.path.exists(dst + ".prev"):
            raise ValueError("no previous version to roll back to")
        os.replace(dst + ".prev", dst)
        ui.good("rolled back")
        return
    with ui.Spinner("Checking for a new version") as sp:
        info = remote()
        newer = V.compare(info["version"], VERSION) > 0
        sp.text("%s -> %s" % (VERSION, info["version"]) if newer else "already on the latest (%s)" % VERSION)
    if not newer and not a.force:
        return
    if a.check:
        ui.info("run `aihub upgrade` to install it")
        return
    if by_pip():                                        # hub first; pip only when the hub download fails
        try:
            hub_install(info)
            ui.celebrate("aihub %s installed from the hub (%s); this pip command now hands over to it" % (info["version"], pyz_path()))
        except Exception as e:
            ui.note("hub download failed (%s); falling back to pip" % e)
            pip_upgrade()
            ui.good("pip finished; run `aihub -v` to see the version")
        return
    apply(info)
    paths.save("update.json", {"checked": time.time()})
    ui.celebrate("aihub %s installed (previous kept; undo with: aihub upgrade --rollback)" % info["version"])


def auto(cmd):
    """Called after a command finishes. At most once a day, never for hooks, never fatal."""
    if cmd in ("hook", "flush", "upgrade", "version") or os.environ.get("AIHUB_NO_UPDATE") or not ui.INTERACTIVE:
        return
    if paths.config().get("auto_update", "true") in ("false", "0", "off", False) or not (target() or by_pip()):
        return
    st = paths.load("update.json", {})
    if time.time() - st.get("checked", 0) < CHECK_EVERY:
        return
    try:
        paths.save("update.json", {"checked": time.time()})     # even on failure, so an offline hub is not retried every command
        info = remote(timeout=3)
        if V.compare(info["version"], VERSION) > 0:
            ui.step("updating aihub %s %s %s" % (VERSION, ui.ARROW, info["version"]))
            hub_install(info) if by_pip() else apply(info)
            ui.good("updated; takes effect on the next run (disable: aihub config set auto_update false)")
    except Exception:
        pass
