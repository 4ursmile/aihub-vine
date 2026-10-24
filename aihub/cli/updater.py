"""CLI version management: `aihub version`, `aihub upgrade`, and the throttled automatic update check."""
import hashlib
import json
import os
import subprocess
import sys
import time
import zipfile

from ..core import version as V
from ..core.release import VERSION
from . import api, paths, ui

CHECK_EVERY = 24 * 3600                 # default auto_update_hours
RETRY_EVERY = 3600                      # a failed check is retried after min(this, the interval)
NEVER = ("hook", "flush", "usage-sync", "credential", "upgrade", "version", "self-uninstall", "uninstall")


def state():
    return paths.load("update.json", {})


def _state(set=None, drop=()):
    """Merge into update.json (pyz_version / pyz written by hub_install, skip_version, ...) instead of replacing it."""
    st = state()
    if not isinstance(st, dict):
        st = {}
    st.update(set or {})
    for k in drop:
        st.pop(k, None)
    try:
        paths.save("update.json", st)
    except OSError:
        pass
    return st


def mode():
    """'on' | 'notify' | 'off'. AIHUB_NO_UPDATE (any value but 0/false/no/off) > AIHUB_AUTO_UPDATE > config auto_update (default on)."""
    if str(os.environ.get("AIHUB_NO_UPDATE", "")).strip().lower() not in ("", "0", "false", "no", "off"):
        return "off"
    raw = os.environ.get("AIHUB_AUTO_UPDATE")
    if raw is None or not str(raw).strip():
        raw = paths.config().get("auto_update", "on")
    v = str(raw).strip().lower()
    if v in ("off", "false", "0", "no", "n", "never"):
        return "off"
    if v == "notify":
        return "notify"
    return "on"


def hours():
    """Check interval in hours, 1..720 (env AIHUB_AUTO_UPDATE_HOURS > config auto_update_hours > 24)."""
    raw = os.environ.get("AIHUB_AUTO_UPDATE_HOURS")
    if raw is None or not str(raw).strip():
        raw = paths.config().get("auto_update_hours", CHECK_EVERY // 3600)
    try:
        h = float(str(raw).strip())
    except ValueError:
        h = CHECK_EVERY // 3600
    return int(min(720, max(1, h)))


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
    _state({"checked": time.time(), "ok": True, "pyz_version": info["version"], "pyz": dst}, ("last_error", "available"))


def pip_upgrade():
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
    m, st = mode(), state()
    line = "auto-update: %s" % m
    if m != "off":
        line += ", every %d h" % hours()
        if st.get("checked"):
            line += ", last check %s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(st["checked"]))
    ui.info(line)
    if st.get("last_error"):
        ui.info("last auto-update error: %s" % st["last_error"])
    if st.get("skip_version"):
        ui.info("auto-update skips %s (you rolled back from it); `aihub upgrade` clears this" % st["skip_version"])
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
    if not a.rollback:
        _state(drop=("skip_version",))                  # a manual upgrade overrides an earlier rollback
    if a.rollback:
        dst = target()
        if not dst or not os.path.exists(dst + ".prev"):
            raise ValueError("no previous version to roll back to")
        os.replace(dst + ".prev", dst)
        _state({"skip_version": VERSION})               # auto-update must not put the version you just left back
        ui.good("rolled back (auto-update will not reinstall %s; a newer version is fine)" % VERSION)
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
    _state({"checked": time.time(), "ok": True}, ("last_error", "available"))
    ui.celebrate("aihub %s installed (previous kept; undo with: aihub upgrade --rollback)" % info["version"])


def _say(msg):
    """Interactive: normal output. Scripts and assistants: one line on stderr, stdout stays clean."""
    if ui.INTERACTIVE:
        ui.good(msg) if msg.startswith("updated") else ui.note(msg)
    else:
        try:
            sys.stderr.write("aihub: %s\n" % msg)
        except Exception:
            pass


def due(st, now=None):
    now = time.time() if now is None else now
    wait = hours() * 3600
    if st.get("ok") is False:
        wait = min(RETRY_EVERY, wait)
    return now - st.get("checked", 0) >= wait


def auto(cmd, a=None):
    """Called after a command finishes (last in main(), so the zipapp swap happens after the other tail calls).
    Mode on = install a newer hub version, notify = only say so, off = nothing. Once per auto_update_hours (a failed check retries
    after at most an hour). Silent on stdout when not interactive, never for hooks / machine-output commands, never fatal. Imports at
    module top only: this may replace aihub.pyz under the running process."""
    try:
        if cmd in NEVER or any(getattr(a, k, False) is True for k in ("json", "plain", "name_only")):
            return
        m = mode()
        if m == "off" or not (target() or by_pip()):
            return
        st = state()
        if not due(st):
            return
        try:
            info = remote(timeout=3)
            new = info["version"]
            if V.compare(new, VERSION) <= 0:
                _state({"checked": time.time(), "ok": True}, ("last_error", "available"))
                return
            _state({"checked": time.time(), "ok": True, "available": new}, ("last_error",))
            if new == st.get("skip_version"):
                return
            if m == "notify":
                _say("aihub %s available: run aihub upgrade" % new)
                return
            hub_install(info) if by_pip() else apply(info)
            _state({"checked": time.time(), "ok": True}, ("last_error", "available"))
            _say("updated aihub %s -> %s; takes effect on the next run (turn off: aihub config set auto_update off)" % (VERSION, new))
        except Exception as e:
            _state({"checked": time.time(), "ok": False, "last_error": str(e)[:300]})
    except Exception:
        pass
