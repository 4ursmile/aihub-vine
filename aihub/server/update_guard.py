"""Watchdog for a self-update. Stdlib only, no aihub imports: the updater copies THIS file to <data>/updates/guard.py (from the known good
version) and starts it detached right before it swaps the code and restarts the server, because a broken new version cannot roll itself back.

    python guard.py <data>/updates/pending.json

pending.json says what to watch. The new server rewrites its `state` from "probation" to something else once it has stayed healthy; the
updater also deletes the file when the swap itself failed. This script stands down on any state but "probation". If the deadline passes
with the state still "probation" (the new code crashed on boot, hung, or never came up), it puts the old commit back, stops what is left of
the broken process and, when the server was started by hand (`spawn`), starts the old version again. Under a supervisor (spawn = false)
the supervisor restarts it.
"""
import json
import os
import signal
import subprocess
import sys
import time


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save(path, st):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, path)


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop(pid):
    if not pid or pid == os.getpid() or not alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(50):
        if not alive(pid):
            return
        time.sleep(0.2)
    try:
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except OSError:
        pass


def main(path):
    while True:
        st = load(path)
        if not st or st.get("state") != "probation":
            return 0                                           # confirmed, rolled back by the server itself, or cancelled
        if time.time() > st.get("deadline", 0):
            break
        time.sleep(2)
    r = subprocess.run(["git", "-C", st["repo"], "reset", "--hard", st["from"]], capture_output=True, text=True, errors="replace")
    cur = load(path)
    if not cur or cur.get("state") != "probation":
        return 0                                               # the server confirmed in the last moment: leave things alone
    if r.returncode != 0:
        cur.update(state="rollback_failed", reason="guard could not reset the checkout: " + (r.stderr or r.stdout).strip()[:300])
        save(path, cur)
        return 1
    cur.update(state="rolled_back", reason="the new version did not confirm it was healthy in time (crash on start or hang): rolled back by the guard")
    save(path, cur)
    stop(cur.get("pid"))
    if cur.get("spawn") and cur.get("argv"):
        kw = {"creationflags": 0x00000008 | 0x00000200} if os.name == "nt" else {"start_new_session": True}
        out = open(os.path.join(os.path.dirname(path), "guard-server.log"), "ab")
        subprocess.Popen(cur["argv"], cwd=cur.get("cwd") or None, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kw)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
