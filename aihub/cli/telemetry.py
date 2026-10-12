"""Non-blocking usage telemetry, batched through git.

`enqueue()` is a single O_APPEND write (microseconds). `kick()` starts a detached flusher process unless one is alive.
The flusher waits one short window so a burst of events becomes ONE batch, then pushes it to the inbox repo as an orphan
commit on its own ref (`refs/telemetry/<client_id>/<batch>`). Nobody else writes that ref, so pushes never conflict. The
hub fetches the refs, stores the events, archives them and deletes the refs. Nothing here may raise or block the agent.
"""
import glob
import json
import os
import subprocess
import sys
import time
import uuid

from . import gitx, paths

STALE = 30      # heartbeat age after which a flusher is considered dead (it beats every 5s)
MAX_SPOOL = 5 * 1024 * 1024
BATCH = 5000    # events per pushed commit


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


def _prep(ev):
    """Stamp an event with what the hub needs. `id` is the dedup key: a resent batch never makes a second row."""
    ev.setdefault("id", uuid.uuid4().hex)
    ev.setdefault("ts", time.time())
    ev.setdefault("local_user", who()[0])
    ev.setdefault("host", who()[1])
    ev.setdefault("os", sys.platform)
    if "user" not in ev:
        ev["user"] = paths.load("credentials.json", {}).get("username") or ""
    if "client_id" not in ev:
        from . import hooks
        ev["client_id"] = hooks.client_id()
    return json.dumps(ev, separators=(",", ":"))


def enqueue(ev):
    try:
        paths.ensure("queue")
        line = (_prep(ev) + "\n").encode()
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
    """Seconds the flusher waits to gather a batch, clamped to 5..3600. Lookup order: AIHUB_TELEMETRY_INTERVAL > `aihub config set
    telemetry_interval 30` > aihub/core/defaults.json `telemetry.interval` > 15."""
    from ..core import defaults
    try:
        v = float(os.environ.get("AIHUB_TELEMETRY_INTERVAL") or paths.config().get("telemetry_interval")
                  or defaults.load().get("telemetry", {}).get("interval") or 15)
    except (TypeError, ValueError):
        v = 15
    return max(5.0, min(v, 3600.0))


def source():
    """Where telemetry goes. Lookup order for each value: environment > ~/.aihub/config.json (`aihub config set telemetry_url|telemetry_branch`)
    > aihub/core/defaults.json (bundled in both the pip package and aihub.pyz) > the package index repo (url only).
    The CLI pushes to refs/telemetry/* of `url`; `branch` is the branch the hub archives ingested batches to."""
    from ..core import defaults
    d, c = defaults.load().get("telemetry", {}), paths.config()
    url = os.environ.get("AIHUB_TELEMETRY_URL") or c.get("telemetry_url") or d.get("git_url") or ""
    if not url:
        from . import registry
        url = registry.source()["url"]
    return {"url": url, "branch": os.environ.get("AIHUB_TELEMETRY_BRANCH") or c.get("telemetry_branch") or d.get("branch") or "telemetry-archive"}


def target():
    return source()["url"]


def _scratch():
    """A private bare repo used only to build commits; nothing is ever checked out."""
    d = paths.p("queue", "git")
    if not os.path.isdir(os.path.join(d, "objects")):
        os.makedirs(d, exist_ok=True)
        gitx.run(["init", "-q", "--bare"], cwd=d)
    return d


def push(url, lines, timeout=20):
    """Push `lines` (JSON strings) as one orphan commit on a fresh ref. Raises on any failure; the caller keeps the events."""
    from . import hooks
    d = _scratch()
    blob = gitx.run(["hash-object", "-w", "--stdin"], cwd=d, input="".join(l + "\n" for l in lines), timeout=timeout)
    tree = gitx.run(["mktree"], cwd=d, input="100644 blob %s\tevents.jsonl\n" % blob, timeout=timeout)
    commit = gitx.run(["-c", "user.name=aihub", "-c", "user.email=aihub@localhost", "commit-tree", tree, "-m", "telemetry"],
                      cwd=d, timeout=timeout)
    ref = "refs/telemetry/%s/%d-%s" % (hooks.client_id(), int(time.time()), uuid.uuid4().hex[:8])
    gitx.run(["push", "-q", url, "%s:%s" % (commit, ref)], cwd=d, timeout=timeout)


def _send_once(timeout=20):
    """Push everything spooled. Returns the number of events sent. The spool files are deleted only after the push succeeded;
    a failure leaves them for the next pass, and a crash between push and delete only causes a resend the hub dedups by id."""
    url = target()
    if not url:
        return 0                                         # keep the spool until an inbox is configured
    if os.path.exists(_spool()):
        os.replace(_spool(), paths.p("queue", "sending.%d.%d.jsonl" % (os.getpid(), time.time() * 1000)))
    files = glob.glob(paths.p("queue", "sending.*.jsonl"))
    lines = []
    for f in files:
        try:
            with open(f) as fh:
                for line in fh:
                    try:
                        json.loads(line)
                        lines.append(line.strip())
                    except ValueError:
                        pass
        except OSError:
            pass
    try:
        for i in range(0, len(lines), BATCH):
            push(url, lines[i:i + BATCH], timeout)
    except Exception as e:
        _send_once.error = gitx.redact_url(str(e))[:200]
        return 0
    for f in files:
        try:
            os.remove(f)
        except OSError:
            pass
    return len(lines)


_send_once.error = ""


def _pending():
    return (os.path.exists(_spool()) and os.path.getsize(_spool()) > 0) or bool(glob.glob(paths.p("queue", "sending.*.jsonl")))


def flush(timeout=20, linger=None, force=False):
    """The one flusher. Waits one window (a burst of events becomes one batch), pushes, and exits once nothing is left or a push
    failed (the events stay spooled; the next event or `aihub flush` retries). The lock's mtime is a heartbeat.
    force=True (aihub flush) pushes right now and returns; if a background flusher is alive it leaves the spool to it."""
    total = 0
    owned = os.environ.pop("AIHUB_FLUSH_LOCKED", "") == "1"     # kick() already took the lock for this process
    try:
        paths.ensure("queue")
        if not owned and not _acquire():
            return 0
        if force:
            return _send_once(timeout)
        while True:
            end = time.time() + interval()
            while time.time() < end:
                os.utime(_lock(), None)
                time.sleep(min(5, max(0.1, end - time.time())))
            n = _send_once(timeout)
            total += n
            if not n or not _pending():
                break
    except Exception:
        pass
    finally:
        _release()
    return total


def record(ev):
    enqueue(ev)
    kick()


def deliver(ev, timeout=20):
    """Push one event now (publish needs the hub to hear about it quickly). Falls back to the spool. -> True when it was pushed."""
    try:
        line = _prep(ev)
        url = target()
        if url:
            push(url, [line], timeout)
            return True
        deliver.error = "no telemetry inbox configured"
    except Exception as e:
        deliver.error = gitx.redact_url(str(e))[:200]
    record(ev)
    return False


deliver.error = ""
