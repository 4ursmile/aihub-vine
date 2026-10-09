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

STALE = 30      # heartbeat age after which a flusher is considered dead (it beats every 5s)
MAX_SPOOL = 5 * 1024 * 1024


def _spool():
    return paths.p("queue", "events.jsonl")


_WHO = None


def who():
    """(OS user, host) of this machine. Lets anonymous installs show up under a real name in the audit trail."""
    global _WHO
    if _WHO is None:
        user = os.environ.get("USER") or os.environ.get("USERNAME") or os.environ.get("LOGNAME") or ""
        if not user:
            try:
                import pwd
                user = pwd.getpwuid(os.getuid()).pw_name
            except Exception:
                user = ""
        try:
            import socket
            host = socket.gethostname()
        except Exception:
            host = ""
        _WHO = (user[:64], host[:128])
    return _WHO


def enqueue(ev):
    try:
        paths.ensure("queue")
        ev.setdefault("ts", time.time())
        ev.setdefault("local_user", who()[0])
        ev.setdefault("host", who()[1])
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


def _lock():
    return paths.p("queue", "flush.lock")


def _acquire():
    """Atomically become the one flusher (O_EXCL). A lock whose heartbeat is stale belonged to a dead process: take it over."""
    lock = _lock()
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) < STALE:
                    return False
                os.remove(lock)                          # stale: retry once
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


def kick():
    """Start the detached flusher unless one is alive. The lock is taken here, before the process starts, so a burst
    of hook calls spawns exactly one flusher instead of one per call."""
    try:
        paths.ensure("queue")
        if not _acquire():
            return
        kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                  env=dict(os.environ, AIHUB_FLUSH_LOCKED="1"))
        if os.name == "nt":
            kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_GROUP
        else:
            kw["start_new_session"] = True
        try:
            subprocess.Popen(self_cmd() + ["flush", "--background"], **kw)
        except Exception:
            _release()
    except Exception:
        pass


def interval():
    """Seconds per flush window. `aihub config set telemetry_interval 120` or AIHUB_TELEMETRY_INTERVAL. 10..3600, default 60."""
    try:
        v = float(os.environ.get("AIHUB_TELEMETRY_INTERVAL") or paths.config().get("telemetry_interval") or 60)
    except (TypeError, ValueError):
        v = 60
    return max(10.0, min(v, 3600.0))


def _user(ev, cred):
    return cred.get("username") or "~" + (ev.get("local_user") or "unknown")


def to_items(ev, count=1, window=None):
    """One Langfuse span. Identity is the hub user, else ~<os user>. `count` calls rolled into it; `window` is the
    bucket start, part of the dedup key so a resend of the same bucket reuses the same ids."""
    from . import langfuse
    cred = paths.load("credentials.json", {})
    user = _user(ev, cred)
    kind = ev.get("kind", "use")
    meta = {k: v for k, v in ev.items() if k not in ("ts", "session") and v not in (None, "")}
    meta.update(user=user, anonymous=not cred.get("username"), os=sys.platform, count=count)
    cid = ev.get("client_id", "")
    if kind == "use":
        key = (cid, ev.get("package", ""), ev.get("component", ""), str(window))
    elif kind == "publish":
        key = (ev.get("package", ""), ev.get("version", ""), kind, ev.get("commit", ""))
    else:
        key = (cid, ev.get("package", ""), ev.get("version", ""), kind, str(ev.get("ts")))
    return langfuse.make_span(langfuse_name(kind), key, meta, user, ts=window if window is not None else ev.get("ts"))


def aggregate(events, secs, now=None, force=False):
    """Roll raw events into spans. Every `use` call is counted; calls to the same package component by the same
    client inside one window become one span with a count. Windows still open stay behind (-> leftover) so a
    window is only ever sent once with its final count. force=True sends open windows too.
    -> (spans, leftover_events)"""
    now = now or time.time()
    spans, left, groups = [], [], {}
    for e in events:
        if e.get("kind", "use") != "use":
            spans.append(to_items(e))
            continue
        w = int(float(e.get("ts") or now) // secs * secs)
        if not force and w + secs > now:
            left.append(e)
            continue
        g = groups.setdefault((e.get("client_id", ""), e.get("package", ""), e.get("component", ""), w), [])
        g.append(e)
    for (_, _, _, w), g in groups.items():
        spans.append(to_items(g[-1], count=len(g), window=w))          # last call's detail/cwd stand for the window
    return spans, left


def langfuse_name(kind):
    from ..core import defaults
    return defaults.load().get("event_names", {}).get(kind, "aihub." + kind)


def _send_once(timeout, force=False):
    """Send what is due. Returns the number of events (calls) sent. Open windows go back to the spool."""
    import glob
    from . import langfuse
    if not langfuse.configured():
        return 0                                         # keep the spool until keys are set
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
    spans, left = aggregate(events, interval(), force=force)
    try:
        for i in range(0, len(spans), 100):
            langfuse.send(spans[i:i + 100], timeout=timeout)
    except Exception:
        return 0  # keep files; retried on the next pass
    for e in left:                                       # still-open windows wait in the spool for the next pass
        enqueue(e)
    for f in files:
        os.remove(f)
    return len(events) - len(left)


def flush(timeout=5, linger=None, force=False):
    """The one flusher. Wakes once per window, sends the closed windows, and exits after a pass with nothing left.
    A burst of hook calls therefore costs one request per window. The lock's mtime is a heartbeat.
    force=True (aihub flush) sends open windows too and returns; it waits for no one, so if a background flusher is
    alive it leaves the spool to it."""
    total = 0
    owned = os.environ.pop("AIHUB_FLUSH_LOCKED", "") == "1"     # kick() already took the lock for this process
    try:
        paths.ensure("queue")
        if not owned and not _acquire():
            return 0
        if force:
            return _send_once(timeout, force=True)
        secs = interval()
        while True:
            end = time.time() + secs + 1                 # let the window close, keeping the heartbeat alive
            while time.time() < end:
                os.utime(_lock(), None)
                time.sleep(min(5, max(0.1, end - time.time())))
            n = _send_once(timeout)
            total += n
            if not n and not (os.path.exists(_spool()) and os.path.getsize(_spool())):
                break
    except Exception:
        pass
    finally:
        _release()
    return total


def record(ev):
    enqueue(ev)
    kick()
