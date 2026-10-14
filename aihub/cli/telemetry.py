"""Non-blocking usage telemetry. The hub is the way in; git is the fallback.

`enqueue()` is a single O_APPEND write (microseconds). `kick()` starts a detached flusher process unless one is alive.
The flusher waits one short window so a burst of events becomes ONE batch, then sends it:
  1. POST /api/v1/telemetry to the hub (cheap for everyone: the hub keeps events in memory and writes the database in batches);
  2. only if the hub cannot be reached (down, unreachable, no such route on an old hub) push the batch to the git inbox instead
     (one file on the `telemetry-inbox` branch). A hub that answers 429/503 is busy, not gone: the batch stays spooled and the
     flusher backs off (honoring Retry-After) instead of also hitting git.
Events leave the spool only after a success. The retry delay grows with every failure, with jitter, so clients never retry in step.
Nothing here may raise or block the agent.
"""
import glob
import gzip
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
BACKOFF_BASE = 30       # seconds to leave the network alone after the first failed send; doubles per failure
BACKOFF_MAX = 3600      # ...up to an hour; the events stay spooled meanwhile
HUB_TIMEOUT = 15
GZIP_MIN = 512          # smaller bodies are not worth compressing
GIT_AFTER = 3           # consecutive hub failures (down, not busy) before git is tried at all: a hub restart must not send everyone to git
WINDOWS = os.name == "nt"
PUSH_TRIES = 12         # a push can lose a race with another client (non-fast-forward): refetch the tip and add the file again.
                        # Many clients flushing in the same second all race for the one branch tip; jittered, growing waits spread them out
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


def _status_file():
    return paths.p("queue", "status.json")


def _status(ok, error="", sent=0, via=""):
    """Remember the outcome of the last push, because the flusher is a detached process nobody watches. `aihub flush -v` and `aihub doctor` show it."""
    try:
        cur = last_status()
        cur.update(at=time.time(), ok=bool(ok), error=error)
        if ok:
            cur.update(last_ok=cur["at"], last_sent=sent, via=via)
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


def _fail(retry_after=0):
    """A send failed (offline, no access, hub busy): wait 30s, 1m, 2m ... up to an hour before the next automatic attempt, each wait
    randomly 50-100% of that (so many clients never retry in step). The hub's Retry-After is a floor. The events stay on disk."""
    import random
    try:
        n = _backoff_state()[0] + 1
        wait = min(BACKOFF_BASE * 2 ** (n - 1), BACKOFF_MAX) * random.uniform(0.5, 1.0)
        wait = min(max(wait, float(retry_after or 0)), BACKOFF_MAX)
        tmp = _backoff_file() + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"fails": n, "next": time.time() + wait}, f)
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
                  or paths.config().get("hub_telemetry_interval") or defaults.load().get("telemetry", {}).get("interval") or 15)
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
    return {"url": url, "branch": os.environ.get("AIHUB_TELEMETRY_BRANCH") or c.get("telemetry_branch") or d.get("branch") or "telemetry-archive",
            "inbox": os.environ.get("AIHUB_TELEMETRY_INBOX_BRANCH") or c.get("telemetry_inbox_branch") or d.get("inbox_branch") or "telemetry-inbox"}


def target():
    return source()["url"]


def _scratch():
    """A private bare repo used only to build commits; nothing is ever checked out."""
    d = paths.p("queue", "git")
    if os.path.isdir(d) and not _scratch_sound(d):
        gitx.rmtree(d)                                  # half-removed leftover (e.g. after self-uninstall): start clean
    if not os.path.isdir(os.path.join(d, "objects")):
        os.makedirs(d, exist_ok=True)
        gitx.run(["init", "-q", "--bare"], cwd=d)
    return d


def _scratch_sound(d):
    """The scratch repo still has HEAD, objects and refs, and git accepts it as a bare repo."""
    return all(os.path.exists(os.path.join(d, x)) for x in ("HEAD", "objects", "refs")) \
        and gitx.run(["rev-parse", "--is-bare-repository"], cwd=d, check=False) == "true"


_RACE = ("non-fast-forward", "fetch first", "stale info", "failed to update ref", "cannot lock ref", "couldn't find remote ref")


def push(url, lines, timeout=20):
    """Add one file, inbox/<client>-<unix>-<rand>.jsonl, on top of the tip of the single inbox branch (default `telemetry-inbox`).
    Only the tip is fetched (depth 1), so the cost does not grow with history. Every client writes a different file name, so two
    clients racing never conflict in content: the loser is rejected as non-fast-forward, refetches the new tip and adds its file again.
    However long the server is down, the repo still has this ONE branch; the files just pile up inside it.
    Raises on any failure; the caller keeps the events."""
    import random
    from . import hooks
    d = _scratch()
    branch = source()["inbox"]
    name = "inbox/%s-%d-%s.jsonl" % (hooks.client_id(), int(time.time()), uuid.uuid4().hex[:8])
    blob = gitx.run(["hash-object", "-w", "--stdin"], cwd=d, input="".join(l + "\n" for l in lines), timeout=timeout)
    env = {"GIT_INDEX_FILE": os.path.join(d, "index.push")}
    for attempt in range(PUSH_TRIES):
        parent = gitx.fetch_tip(d, url, branch, "refs/inbox/tip", depth=1, timeout=timeout)     # "" = no branch yet: this push creates it
        gitx.run(["read-tree", parent or "--empty"], cwd=d, extra_env=env)
        gitx.run(["update-index", "--add", "--cacheinfo", "100644,%s,%s" % (blob, name)], cwd=d, extra_env=env)
        tree = gitx.run(["write-tree"], cwd=d, extra_env=env)
        commit = gitx.run(["-c", "user.name=aihub", "-c", "user.email=aihub@localhost", "commit-tree", tree, "-m", "telemetry"]
                          + (["-p", parent] if parent else []), cwd=d, timeout=timeout)
        try:
            gitx.run(["push", "-q", url, "%s:refs/heads/%s" % (commit, branch)], cwd=d, timeout=timeout)
            return
        except gitx.GitError as e:
            if attempt == PUSH_TRIES - 1 or not any(w in str(e).lower() for w in _RACE):
                raise                                                      # not a lost race (no access, offline, protected branch): do not hammer
            time.sleep(random.uniform(0.1, 1.0) * min(2 ** attempt, 8))         # full jitter, capped: ~30 s worst case for 12 tries


class HubBusy(Exception):
    """The hub answered but asked us to slow down (429/503) or is overloaded. Not a reason to use git."""
    def __init__(self, msg, retry_after=0):
        super().__init__(msg)
        self.retry_after = retry_after


def hub_url():
    return paths.config()["hub"].rstrip("/") + "/api/v1/telemetry"


def post(lines, timeout=HUB_TIMEOUT):
    """Send events to the hub. -> number accepted. Raises HubBusy (429/503: hub alive, wait), ValueError (hub says the batch is bad:
    retrying cannot help) or OSError (cannot reach it / no such route: git is the way)."""
    import urllib.error
    import urllib.request
    from . import api
    events = []
    for l in lines:
        try:
            events.append(json.loads(l))
        except ValueError:
            pass
    body = json.dumps({"events": events}, separators=(",", ":")).encode()
    h = {"Content-Type": "application/json", "User-Agent": "aihub-cli"}
    tok = paths.load("credentials.json", {}).get("token")
    if tok:
        h["Authorization"] = "Bearer " + tok
    zipped = len(body) >= GZIP_MIN
    for attempt in (0, 1):
        data, hh = (gzip.compress(body, 6, mtime=0), dict(h, **{"Content-Encoding": "gzip"})) if zipped else (body, h)
        rq = urllib.request.Request(hub_url(), data=data, headers=hh, method="POST")
        try:
            with api._opener.open(rq, timeout=timeout) as r:
                ans = json.loads(r.read() or b"{}")
            _note_index(ans.get("index_etag"))
            return int(ans.get("accepted", len(events)))
        except urllib.error.HTTPError as e:
            if zipped and not attempt and e.code in (400, 415):
                zipped = False                                           # an older hub that cannot read gzip: once more, plain
                continue
            return _hub_error(e)
    raise OSError("hub unreachable")


def _note_index(etag):
    """The hub tells us the current index ETag with every answer. If our cached catalog is another one, expire its trust window so the
    next command revalidates (one small request) instead of waiting out the TTL."""
    if not etag:
        return
    try:
        f = paths.p("index-hub", "catalog.json")
        with open(f, encoding="utf-8") as fh:
            if json.load(fh).get("etag") != etag:
                os.utime(f, (0, 0))
    except (OSError, ValueError, AttributeError):
        pass


def _hub_error(e):
    import urllib.error
    if e.code in (429, 502, 503, 504):
        try:
            ra = float(e.headers.get("Retry-After") or 0)
        except (TypeError, ValueError):
            ra = 0
        raise HubBusy("hub busy (%d)" % e.code, min(ra, BACKOFF_MAX))
    if e.code in (404, 405, 501):
        raise OSError("hub has no telemetry route (%d)" % e.code)    # an older hub: use git
    if e.code == 401:
        raise OSError("hub refused the token (401)")                 # stale login: events are still wanted, anonymous sending is not offered
    if e.code in (400, 413):
        raise ValueError("hub rejected the batch (%d)" % e.code)
    raise OSError("hub error %d" % e.code)


def send(url, lines, timeout=20):
    """Hub first, git as the fallback. -> "hub" | "git". Raises when neither worked (the caller keeps the events).
    Git is tried only when the hub is unreachable (not when it is merely busy) and not before GIT_AFTER failures in a row, so a short
    hub restart never moves the whole fleet onto the git host."""
    last = None
    try:
        post(lines, min(timeout, HUB_TIMEOUT))
        return "hub"
    except HubBusy:
        raise
    except ValueError:
        raise
    except Exception as e:
        last = e
    if _backoff_state()[0] + 1 < GIT_AFTER and not os.environ.get("AIHUB_TELEMETRY_GIT_ONLY"):
        raise OSError("hub unreachable (%s); will retry before using git" % gitx.redact_url(str(last))[:100])
    if not url:
        raise last
    for i in range(0, len(lines), BATCH):
        push(url, lines[i:i + BATCH], timeout)
    return "git"


def _send_once(timeout=20):
    """Push everything spooled. Returns the number of events sent. The spool files are deleted only after the push succeeded;
    a failure leaves them for the next pass, and a crash between push and delete only causes a resend the hub dedups by id."""
    url = target()
    if not url and not hub_enabled():
        return 0                                         # keep the spool until the hub or an inbox is configured
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
            via = send(url, lines, timeout) if hub_enabled() else (push_all(url, lines, timeout) or "git")
        except HubBusy as e:
            _send_once.error = str(e)
            _status(False, _send_once.error)
            _fail(e.retry_after)
            return 0
        except Exception as e:
            _send_once.error = gitx.redact_url(str(e))[:200]
            _status(False, _send_once.error)
            _fail()
            return 0
        _ok()
        _status(True, "", len(lines), via)
    for f, size in sizes.items():                        # nothing valid in a file is also "done": it must not keep the flusher looping
        try:
            if os.path.getsize(f) == size:               # a hook that opened the spool just before the rename may still have appended: keep that file
                os.remove(f)
        except OSError:
            pass
    return len(lines)


_send_once.error = ""


def hub_enabled():
    """Send to the hub first. Off with AIHUB_TELEMETRY_VIA=git, or `aihub config set telemetry_via git`."""
    via = (os.environ.get("AIHUB_TELEMETRY_VIA") or paths.config().get("telemetry_via") or "hub").lower()
    return via != "git" and paths.hub_configured()


def push_all(url, lines, timeout=20):
    for i in range(0, len(lines), BATCH):
        push(url, lines[i:i + BATCH], timeout)


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
            if not target() and not hub_enabled():
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
    """Push one event now (publish needs the hub to hear about it quickly). Falls back to the spool. -> True when it was pushed."""
    try:
        line = _prep(ev)
        url = target()
        if hub_enabled():
            try:
                post([line], min(timeout, HUB_TIMEOUT))
                _ok()
                _status(True, "", 1, "hub")
                return True
            except HubBusy as e:
                raise OSError(str(e))                                    # busy hub: spool it, do not add load to git
            except ValueError:
                raise
            except Exception:
                pass                                                     # unreachable: a publish is rare and wants to arrive, try git below
        if url:
            push(url, [line], timeout)
            _ok()
            _status(True, "", 1, "git")
            return True
        deliver.error = "no telemetry inbox configured"
    except Exception as e:
        deliver.error = gitx.redact_url(str(e))[:200]
        _status(False, deliver.error)
        _fail(getattr(e, "retry_after", 0))
    record(ev)
    return False


deliver.error = ""
