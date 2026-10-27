"""CLI version management: `aihub version`, `aihub upgrade`, and the automatic update.

Auto-update runs after every command but never makes it wait: `auto()` reads update.json, prints what the last background check
found, and (when the debounce gap has passed) starts ONE detached, low-priority `aihub auto-update --background` worker (O_EXCL
lock queue/update.lock taken before the spawn, so a burst of commands starts one). The worker asks the hub, downloads and verifies
the new zipapp and swaps it in; the next command runs the new version and says so once."""
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

GAP = 60                                # default auto_update_minutes = 1: a check after (almost) every command, at most once a minute
RETRY_BASE, RETRY_MAX = 60, 3600        # failed checks back off 1 min, 2, 4, ... up to 1 h (never sooner than the gap)
STALE = 600                             # a worker lock older than this belonged to a dead worker
NOTIFY_EVERY = 24 * 3600                # notify mode repeats "X available" at most once a day per version
NEVER = ("hook", "flush", "usage-sync", "credential", "upgrade", "version", "self-uninstall", "uninstall", "auto-update")


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


def minutes():
    """Debounce between checks in minutes, 1..43200. Env AIHUB_AUTO_UPDATE_MINUTES > config auto_update_minutes >
    the older auto_update_hours (env, then config; x 60) > 1."""
    for raw, scale in ((os.environ.get("AIHUB_AUTO_UPDATE_MINUTES"), 1), (paths.config().get("auto_update_minutes"), 1),
                       (os.environ.get("AIHUB_AUTO_UPDATE_HOURS"), 60), (paths.config().get("auto_update_hours"), 60)):
        if raw is None or not str(raw).strip():
            continue
        try:
            return int(min(43200, max(1, float(str(raw).strip()) * scale)))
        except ValueError:
            continue
    return GAP // 60


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
    _state({"checked": time.time(), "ok": True, "pyz_version": info["version"], "pyz": dst}, ("last_error", "available", "fails"))


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


def remote(timeout=10):
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
    res = {"version": VERSION, "pip": by_pip(), "auto_update": m, "checked": st.get("checked"), "last_error": st.get("last_error"),
           "skip_version": st.get("skip_version"), "hub_version": None, "update_available": None}
    ui.result(res)
    line = "auto-update: %s" % m
    if m != "off":
        line += ", checked in the background after every command (at most every %d min)" % minutes()
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
    res.update(hub_version=info["version"], update_available=V.compare(info["version"], VERSION) > 0)
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
    _state({"checked": time.time(), "ok": True}, ("last_error", "available", "fails"))
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
    wait = minutes() * 60
    fails = int(st.get("fails") or (1 if st.get("ok") is False else 0))
    if fails:
        wait = max(wait, min(RETRY_MAX, RETRY_BASE * 2 ** (fails - 1)))
    return now - st.get("checked", 0) >= wait


def _lock():
    return paths.p("queue", "update.lock")


def _acquire():
    """Become the one worker (O_EXCL). A stale lock (dead worker) is taken over."""
    try:
        paths.ensure("queue")
    except OSError:
        return False
    for _ in range(2):
        try:
            os.close(os.open(_lock(), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(_lock()) < STALE:
                    return False
                os.remove(_lock())
            except OSError:
                return False
        except OSError:
            return False
    return False


def _release():
    try:
        os.remove(_lock())
    except OSError:
        pass


def _self_cmd():
    arg0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if arg0.endswith(".pyz") or os.path.isfile(arg0) and arg0.endswith("aihub"):
        return [sys.executable, arg0]
    return [sys.executable, "-m", "aihub.cli"]


def spawn():
    """Start the detached worker; the caller holds the lock. Low priority and no window, stdio closed: the command, Claude Code and
    whatever else runs never wait for it."""
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    if os.name == "nt":
        kw["creationflags"] = 0x08000000 | 0x00000200 | 0x00004000      # CREATE_NO_WINDOW | NEW_PROCESS_GROUP | BELOW_NORMAL_PRIORITY
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen(_self_cmd() + ["auto-update", "--background"], **kw)
    except Exception:
        _release()


def _fail(e):
    st = state()
    _state({"checked": time.time(), "ok": False, "fails": int(st.get("fails") or 0) + 1, "last_error": str(e)[:300]})


def check():
    """One check (the worker's body; tests call it directly). -> the version installed, else None. Never raises."""
    try:
        m = mode()
        if m == "off":
            return None
        st = state()
        info = remote()
        new = info["version"]
        if V.compare(new, VERSION) <= 0:
            _state({"checked": time.time(), "ok": True}, ("last_error", "available", "fails"))
            return None
        _state({"checked": time.time(), "ok": True, "available": new}, ("last_error", "fails"))
        if new == st.get("skip_version") or m == "notify":
            return None
        hub_install(info) if by_pip() else apply(info)
        _state({"checked": time.time(), "ok": True, "announce": {"from": VERSION, "to": new}}, ("last_error", "available", "fails"))
        return new
    except Exception as e:
        _fail(e)
        return None


def cmd_auto_update(a):
    """Hidden: the detached worker started by auto(). Without --background: check now in the foreground (lock still respected)."""
    if not a.background and not _acquire():
        ui.info("an update check is already running")
        return
    try:
        if a.background and hasattr(os, "nice"):
            try:
                os.nice(10)
            except OSError:
                pass
        new = check()
        if not a.background:
            st = state()
            if new:
                ui.good("updated aihub %s -> %s; takes effect on the next run" % (VERSION, new))
            elif st.get("ok") is False:
                ui.error(st.get("last_error") or "update check failed")
            else:
                ui.good("aihub %s is current%s" % (VERSION, " (%s available)" % st["available"] if st.get("available") else ""))
    finally:
        _release()


def _tell(st, m):
    """Print what the last background check found: once after an update, once a day per version in notify mode."""
    ann = st.get("announce")
    if isinstance(ann, dict):
        if ann.get("to") == VERSION:
            _say("updated aihub %s -> %s (turn off: aihub config set auto_update off)" % (ann.get("from"), VERSION))
        _state(drop=("announce",))
        return
    new = st.get("available")
    if not new or new == st.get("skip_version") or V.compare(new, VERSION) <= 0:
        return
    if m == "notify":
        seen = st.get("notified") or {}
        if seen.get("version") == new and time.time() - seen.get("at", 0) < NOTIFY_EVERY:
            return
        _state({"notified": {"version": new, "at": time.time()}})
        _say("aihub %s available: run aihub upgrade" % new)


def auto(cmd, a=None):
    """Called last in main() after every command. Costs one small file read (plus a process spawn when a check is due); all network
    work happens in the detached worker. Skipped for hooks / machine-output commands, never raises."""
    try:
        if cmd in NEVER or any(getattr(a, k, False) is True for k in ("json", "plain", "name_only")):
            return
        m = mode()
        if m == "off" or not (target() or by_pip()):
            return
        st = state()
        _tell(st, m)
        if due(st) and _acquire():
            spawn()
    except Exception:
        pass
