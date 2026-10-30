"""Keeps aihub-ui itself current. A thread asks the hub for the newest aihub-ui every UI_UPDATE_MINUTES (default 60); when there is one, and
NOTHING is running (no chat answering, no work run, no install terminal) for IDLE_FOR seconds, it runs `aihub update aihub-ui` and restarts the service.
Pref `auto_update`: on (default) | notify (only say so on the Settings card) | off. A restart is a clean exit under `aihub run` (the supervisor restarts a
service, restart = always) and an in-place exec otherwise. A failed update waits for the next round; the running copy is never touched until it succeeded."""
import os
import re
import sys
import threading
import time

from . import aihub as A, chat, freechat, store, term

PKG = "aihub-ui"
IDLE_FOR = 60
state = {"checked": 0.0, "installed": "", "latest": "", "error": "", "updated": "", "waiting": "", "restarting": False, "checking": False, "stage": "", "stage_at": 0.0}
_ctl = {"engine": None, "restart": None}              # set by start(): what "Update now" / "Restart now" on the Settings card call
_lock = threading.Lock()
_now = threading.Event()
_checking = threading.Lock()                # one version check at a time


def every():
    try:
        return max(int(os.environ.get("UI_UPDATE_MINUTES") or 60), 5) * 60
    except ValueError:
        return 3600


def vkey(v):
    return [int(x) for x in re.findall(r"\d+", str(v or ""))[:4]]


def busy(engine):
    """Why a restart now would cut something off ('' = nothing is running)."""
    with chat.plock:
        if any(not r.done for r in chat.runs.values()):
            return "a chat is answering"
    if freechat.live:
        return "a free chat is answering"
    if engine is not None and getattr(engine, "running", 0):
        return "a work is running"
    with term.lock:
        if any(t.code is None and not getattr(t, "shell", False) for t in term.terms.values()):      # chat shells do not count, installs and publishes do
            return "an install is running"
    return ""


def stage(name):
    """Progress step shown on the Settings card: '' | checking | waiting | updating | restarting | error."""
    state.update(stage=name, stage_at=time.time())


def check():
    """Ask the CLI for the installed and the newest version. Sets state; -> True when a newer one exists."""
    with _checking:
        state["checking"] = True
        if state["stage"] in ("", "error"):
            stage("checking")
        try:
            return _check()
        finally:
            state["checking"] = False
            if state["stage"] == "checking":
                stage("")


def _check():
    try:
        have = next((p.get("version") for p in (A.run(["list"], timeout=60).get("data") or []) if p.get("name") == PKG), "")
        latest = (A.run(["info", PKG], timeout=120).get("data") or {}).get("latest_version") or ""
        state.update(installed=have or "", latest=latest, error="", checked=time.time())
    except Exception as e:
        state.update(error=str(e)[:200], checked=time.time())
        return False
    return bool(have and latest and vkey(latest) > vkey(have))


def apply(engine, restart, force=False):
    """Update and restart. Only when idle unless `force`; -> True when it restarted."""
    why = "" if force else busy(engine)
    if why:
        state["waiting"] = why
        stage("waiting")
        return False
    state["waiting"] = ""
    stage("updating")
    try:
        A.run(["update", PKG], timeout=1800)
    except Exception as e:
        state["error"] = "update failed: %s" % str(e)[:200]
        stage("error")
        return False
    state.update(updated=state["latest"], installed=state["latest"], restarting=True)
    stage("restarting")
    time.sleep(1)
    if not force and busy(engine):                                    # something started while the files were replaced: the new version loads at the next restart
        state["restarting"] = False
        state["waiting"] = "restart postponed: " + busy(engine)
        stage("waiting")
        return False
    restart()
    return True


def loop(engine, restart):
    idle_since = 0.0
    while True:
        mode = store.prefs().get("auto_update", "on")
        due = time.time() - state["checked"] >= every() or _now.is_set()
        if mode != "off" and due:
            _now.clear()
            check()
        newer = bool(state["installed"] and state["latest"] and vkey(state["latest"]) > vkey(state["installed"]))
        if newer and mode == "on":
            if busy(engine):
                idle_since = 0.0
                state["waiting"] = busy(engine)
            else:
                idle_since = idle_since or time.time()
                if time.time() - idle_since >= IDLE_FOR and apply(engine, restart):
                    return
        else:
            idle_since, state["waiting"] = 0.0, ""
        _now.wait(15)
        if mode == "off":
            _now.clear()


def start(engine, restart):
    _ctl.update(engine=engine, restart=restart)
    threading.Thread(target=loop, args=(engine, restart), daemon=True, name="ui-selfupdate").start()


def check_now(wait=20.0):
    """Run a check now (in its own thread, also when auto update is off) and wait for it, at most WAIT seconds. -> True when it finished;
    False = still running, `status()["checking"]` says so and the page can ask again."""
    th = threading.Thread(target=check, daemon=True, name="ui-update-check")
    th.start()
    th.join(wait)
    return not th.is_alive()


def act(what):
    """Settings buttons, run in a thread so the HTTP reply goes out first. what = 'update' (install the newest, then restart) | 'restart'.
    Both ignore running chats/works: the user asked for it. -> '' or why nothing was started."""
    if not _ctl["restart"]:
        return "not available"
    if state["stage"] in ("updating", "restarting"):
        return "already in progress"

    def run():
        if what == "update":
            apply(_ctl["engine"], _ctl["restart"], force=True)
            return
        state.update(restarting=True)
        stage("restarting")
        time.sleep(1)
        _ctl["restart"]()
    threading.Thread(target=run, daemon=True, name="ui-update-act").start()
    return ""


def status():
    p = store.prefs().get("auto_update", "on")
    return dict(state, mode=p, newer=bool(state["installed"] and state["latest"] and vkey(state["latest"]) > vkey(state["installed"])))


def restarter(srv, engine):
    """The function the loop calls to restart: leave to the supervisor of `aihub run`, else exec this program again in place."""
    def go():
        engine.shutdown()
        if os.environ.get("AIHUB_RUN_ID"):
            os._exit(0)
        try:
            srv.server_close()
        except OSError:
            pass
        os.environ["UI_OPEN"] = "0"                       # same address and key: no new browser tab
        os.execv(sys.executable, [sys.executable] + sys.argv)
    return go
