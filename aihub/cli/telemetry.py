"""Non-blocking usage telemetry, sent to Kafka.

`enqueue()` is a single O_APPEND write (microseconds). `kick()` starts a detached flusher process unless one is alive.
The flusher waits one short window so a burst of events becomes ONE batch, then produces every event as a message on the
hub's Kafka topic (kafka-python). The hub consumes the topic and updates the web at once. A failed send keeps the events on
disk and retries with a growing delay (see `_fail`). Git is no longer used for usage events; the one exception is a `publish`
event when Kafka cannot be reached (`deliver`),.
Nothing here may raise or block the agent.
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
BACKOFF_BASE = 30       # seconds to leave the network alone after the first failed push; doubles per failure
BACKOFF_MAX = 900
WINDOWS = os.name == "nt"
MAX_PASSES = 20         # push passes one flusher process may run; a safety net so no failure mode can make it loop forever


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
    """Append one event to the spool. Never drops: a spool past MAX_SPOOL is sealed into its own `sending.*` file (kept until Kafka
    takes it) and a fresh spool is started, so a long outage only costs disk."""
    try:
        paths.ensure("queue")
        line = (_prep(ev) + "\n").encode()
        sp = _spool()
        if os.path.exists(sp) and os.path.getsize(sp) > MAX_SPOOL:
            try:
                os.replace(sp, paths.p("queue", "sending.%d.%d.jsonl" % (os.getpid(), time.time() * 1000000)))
            except OSError:
                pass
        fd = os.open(sp, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
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


def _status_file():
    return paths.p("queue", "status.json")


def _status(ok, error="", sent=0):
    """Remember the outcome of the last push, because the flusher is a detached process nobody watches. `aihub flush -v` and `aihub doctor` show it."""
    try:
        cur = last_status()
        cur.update(at=time.time(), ok=bool(ok), error=error)
        if ok:
            cur.update(last_ok=cur["at"], last_sent=sent)
        tmp = _status_file() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cur, f)
        os.replace(tmp, _status_file())
    except Exception:
        pass


def last_status():
    try:
        with open(_status_file()) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _backoff_file():
    return paths.p("queue", "backoff.json")


def _backoff_state():
    try:
        with open(_backoff_file()) as f:
            d = json.load(f)
        return int(d.get("fails", 0)), float(d.get("next", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0, 0.0


def _backoff():
    """Seconds left before an automatic push attempt is allowed again (0 = go). A `next` implausibly far ahead (clock moved) is ignored."""
    left = _backoff_state()[1] - time.time()
    return left if 0 < left <= BACKOFF_MAX else 0


def _fail():
    """A push failed (offline, no access): wait 30s, then 1m, 2m ... up to 15m before the next automatic attempt. The events stay on disk."""
    try:
        n = _backoff_state()[0] + 1
        tmp = _backoff_file() + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"fails": n, "next": time.time() + min(BACKOFF_BASE * 2 ** (n - 1), BACKOFF_MAX)}, f)
        os.replace(tmp, _backoff_file())
    except Exception:
        pass


def _ok():
    try:
        os.remove(_backoff_file())
    except OSError:
        pass


def kick():
    """Start the detached flusher unless one is alive or the last push failed recently. The lock is taken here, before the process
    starts, so a burst of hook calls spawns exactly one flusher instead of one per call."""
    try:
        paths.ensure("queue")
        if _backoff() or not _acquire():
            return
        kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                  env=dict(os.environ, AIHUB_FLUSH_LOCKED="1"))
        if WINDOWS:
            # CREATE_NO_WINDOW | NEW_PROCESS_GROUP. Not DETACHED_PROCESS: a detached process has no console, so Windows opened a NEW
            # console window for every git it ran (a window flashing on and off for each command). This one has a hidden console its
            # children share. pythonw would avoid it too but is not always next to python.
            kw["creationflags"] = 0x08000000 | 0x00000200
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


def kafka_config():
    """Kafka settings, or {} when none is configured (events then stay spooled). Lookup order for each field: environment
    AIHUB_KAFKA_<FIELD> > ~/.aihub/config.json `kafka_<field>` (the hub writes these on setup) > aihub/core/defaults.json `kafka.<field>`."""
    from ..core import defaults, kafkacfg
    d, c = defaults.load().get("kafka", {}), paths.config()
    return kafkacfg.resolve(lambda k: os.environ.get("AIHUB_KAFKA_" + k.upper()) or c.get("kafka_" + k) or d.get(k))


def enabled():
    return bool(kafka_config())


def kafka_reachable(timeout=3):
    """True when a TCP connection to any bootstrap server opens (a cheap check for `doctor` / `setup`; no Kafka protocol involved)."""
    import socket
    from ..core import kafkacfg
    for host, port in kafkacfg.hosts(kafka_config()):
        try:
            socket.create_connection((host, port), timeout=timeout).close()
            return True
        except OSError:
            continue
    return False


def send(lines, timeout=20):
    """Produce every line (one JSON event) as one Kafka message and wait until the broker has them all. Raises on any failure; the caller
    keeps the events. The message key is the client id, so one client's events stay in order on one partition."""
    cfg = kafka_config()
    if not cfg:
        raise RuntimeError("no Kafka server configured")
    from ..core import kafkacfg
    try:
        from kafka import KafkaProducer
    except ImportError:
        raise RuntimeError("kafka-python is not installed (pip install kafka-python)")
    from . import hooks
    key = hooks.client_id().encode()
    producer = KafkaProducer(acks="all", retries=3, linger_ms=50, request_timeout_ms=int(timeout * 1000),
                             max_block_ms=int(timeout * 1000), bootstrap_timeout_ms=int(timeout * 1000),
                             **kafkacfg.client_kwargs(cfg))
    try:
        futures = [producer.send(cfg["topic"], key=key, value=l.encode()) for l in lines]
        producer.flush(timeout=timeout)
        for f in futures:
            f.get(timeout=1)                             # raises the broker's error for a message that was not stored
    finally:
        producer.close(timeout=2)



def _send_once(timeout=20):
    """Push everything spooled. Returns the number of events sent. The spool files are deleted only after the push succeeded;
    a failure leaves them for the next pass, and a crash between push and delete only causes a resend the hub dedups by id."""
    if not enabled():
        return 0                                         # keep the spool until Kafka is configured
    if os.path.exists(_spool()):
        os.replace(_spool(), paths.p("queue", "sending.%d.%d.jsonl" % (os.getpid(), time.time() * 1000)))
    files = glob.glob(paths.p("queue", "sending.*.jsonl"))
    lines, sizes = [], {}
    for f in files:
        try:
            with open(f) as fh:
                for line in fh:
                    try:
                        json.loads(line)
                        lines.append(line.strip())
                    except ValueError:
                        pass
                sizes[f] = fh.tell()
        except OSError:
            pass
    if lines:
        try:
            for i in range(0, len(lines), BATCH):
                send(lines[i:i + BATCH], timeout)
        except Exception as e:
            _send_once.error = gitx.redact_url(str(e))[:200]
            _status(False, _send_once.error)
            _fail()
            return 0
        _ok()
        _status(True, "", len(lines))
    for f, size in sizes.items():                        # nothing valid in a file is also "done": it must not keep the flusher looping
        try:
            if os.path.getsize(f) == size:               # a hook that opened the spool just before the rename may still have appended: keep that file
                os.remove(f)
        except OSError:
            pass
    return len(lines)


_send_once.error = ""


def _pending():
    return (os.path.exists(_spool()) and os.path.getsize(_spool()) > 0) or bool(glob.glob(paths.p("queue", "sending.*.jsonl")))


def flush(timeout=20, linger=None, force=False):
    """The one flusher. Waits one window (a burst of events becomes one batch), pushes, and exits once nothing is left. After a failed
    push it exits too and stays away for a growing delay (see _fail); the events stay spooled and the next event after that delay
    starts a new flusher. The lock's mtime is a heartbeat.
    force=True (aihub flush) pushes right now, ignores the delay and returns; if a background flusher is alive it leaves the spool to it."""
    total = 0
    owned = os.environ.pop("AIHUB_FLUSH_LOCKED", "") == "1"     # kick() already took the lock for this process
    held = owned
    try:
        paths.ensure("queue")
        if not owned:
            if not _acquire():
                return 0                                        # someone else is flushing: never touch their lock
            held = True
        if force:
            return _send_once(timeout)
        for _ in range(MAX_PASSES):
            if not enabled():
                break
            end = time.time() + interval()
            while time.time() < end:
                os.utime(_lock(), None)
                time.sleep(min(5, max(0.1, end - time.time())))
            n = _send_once(timeout)
            total += n
            # Let go of the lock FIRST, then look at the spool once more. A hook that recorded an event while we held the lock saw a
            # live flusher and started none; its event is already on disk by now, so this look cannot miss it.
            _release()
            held = False
            if not n or not _pending() or _backoff() or not _acquire():
                break                                   # n == 0: nothing was sent, so what is left cannot be sent either; never spin
            held = True
    except Exception:
        pass
    finally:
        if held:
            _release()
    return total


def record(ev):
    enqueue(ev)
    kick()


def deliver(ev, timeout=20):
    """Send one event now (publish needs the hub to hear about it quickly). Over Kafka; if that fails the event is spooled and the
    background flusher retries it. -> True when sent."""
    line = _prep(ev)
    errors = []
    if enabled():
        try:
            send([line], timeout)
            _ok()
            _status(True, "", 1)
            return True
        except Exception as e:
            errors.append("kafka: " + str(e))
    else:
        errors.append("no Kafka server configured")
    deliver.error = gitx.redact_url("; ".join(errors))[:300]
    _status(False, deliver.error)
    if enabled():
        _fail()
    record(ev)
    return False


deliver.error = ""
