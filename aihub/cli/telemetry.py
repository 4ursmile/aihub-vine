"""Non-blocking usage telemetry.

`enqueue()` is a single O_APPEND write (microseconds). `kick()` starts a detached flusher process at most
once per interval. The flusher batches the spool into one POST. Nothing here may raise or block the agent.
"""
import json
import os
import subprocess
import sys
import time

from . import paths

STALE = 15      # heartbeat age after which a flusher is considered dead
MAX_SPOOL = 5 * 1024 * 1024


def _spool():
    return paths.p("queue", "events.jsonl")


def enqueue(ev):
    try:
        paths.ensure("queue")
        ev.setdefault("ts", time.time())
        line = (json.dumps(ev, separators=(",", ":")) + "\n").encode()
        if os.path.exists(_spool()) and os.path.getsize(_spool()) > MAX_SPOOL:
            return
        fd = os.open(_spool(), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except Exception:
        pass


def self_cmd():
    """Command (list) that re-invokes this CLI, works for .pyz and `-m`."""
    arg0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if arg0.endswith(".pyz") or os.path.isfile(arg0) and arg0.endswith("aihub"):
        return [sys.executable, arg0]
    return [sys.executable, "-m", "aihub.cli"]


def kick():
    """Spawn a detached flusher unless one is alive."""
    try:
        lock = paths.p("queue", "flush.lock")
        if os.path.exists(lock) and time.time() - os.path.getmtime(lock) < STALE:
            return
        kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        if os.name == "nt":
            kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_GROUP
        else:
            kw["start_new_session"] = True
        subprocess.Popen(self_cmd() + ["flush"], **kw)
    except Exception:
        pass


def _send_once(timeout):
    """Move the spool aside and POST everything pending as batches. Returns count sent."""
    import glob
    from . import api
    if os.path.exists(_spool()):
        os.replace(_spool(), paths.p("queue", "sending.%d.%d.jsonl" % (os.getpid(), time.time() * 1000)))
    files = glob.glob(paths.p("queue", "sending.*.jsonl"))
    events = []
    for f in files:
        for line in open(f):
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    try:
        for i in range(0, len(events), 400):
            api.call("POST", "/events", {"events": events[i:i + 400]}, timeout=timeout)
    except Exception:
        return 0  # keep files; retried on the next pass
    for f in files:
        os.remove(f)
    return len(events)


def flush(timeout=5, linger=10):
    """Detached flusher. Sends the spool, then lingers `linger`s polling for more so a burst of hook calls
    reuses this one process instead of each spawning its own. Lock mtime is a heartbeat."""
    lock = paths.p("queue", "flush.lock")
    total = 0
    try:
        paths.ensure("queue")
        if os.path.exists(lock) and time.time() - os.path.getmtime(lock) < STALE:
            return 0
        open(lock, "w").write(str(os.getpid()))
        idle_since = time.time()
        while True:
            n = _send_once(timeout)
            total += n
            if n:
                idle_since = time.time()
            elif time.time() - idle_since > linger:
                break
            os.utime(lock, None)
            time.sleep(0.5)
    except Exception:
        pass
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass
    return total


def record(ev):
    enqueue(ev)
    kick()
