"""Usage of this UI -> the hub. Called after every answer turn (chat send, work run, free chat or Claude Code).

Claude Code turns are already in Claude's own session logs, which `aihub usage-sync` totals per day and uploads. Free chat talks to the
API directly, so nothing is logged for it: `note()` appends one line (numbers and the model name only) to ~/.aihub/ui/usage.jsonl, which
the CLI reads as the tool `aihub-ui`. Then `kick()` starts ONE `aihub usage-sync` after a short quiet window (a burst of turns, or a work
with many AI nodes, becomes a single upload). `use()` counts a chat send / finished work run / free-chat MCP call as a `use` event of a package (the hub's
use numbers): it appends to the CLI's telemetry spool and the same quiet window runs one `aihub flush`. Nothing here may raise or block a turn."""
import json
import os
import subprocess
import sys
import threading
import time
import uuid

from . import aihub as A

WINDOW = 6.0                       # seconds of quiet before the upload starts
_lock = threading.Lock()
_timer = None
_pending = False                    # use events were spooled since the last flush


def log_path():
    return os.path.join(A.HOME(), "ui", "usage.jsonl")


def note(sid, engine, model, u):
    """Free chat only: record the turn (Claude Code logs its own). Then schedule the upload."""
    try:
        if engine == "free" and u and any(int(u.get(k) or 0) for k in ("input", "output", "cache_read", "cache_write")):
            os.makedirs(os.path.dirname(log_path()), exist_ok=True)
            line = {"ts": time.time(), "session": str(sid), "model": str(model or "unknown")[:128]}
            for k in ("input", "output", "cache_read", "cache_write"):
                line[k] = max(0, int(u.get(k) or 0))
            with open(log_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(line, separators=(",", ":")) + "\n")
    except (OSError, TypeError, ValueError):
        pass
    kick()


def _json(name):
    try:
        with open(os.path.join(A.HOME(), name), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def use(component, package="aihub-ui", session="", detail=None):
    """One `use` event for the hub's counts (component: chat, work:<id>, mcp:<server>). The line goes straight into the CLI's telemetry
    spool (one O_APPEND write, no process, any CLI version reads it: `aihub hook --use` is missing from older installs, which made every
    count vanish silently). The next `kick()` window then runs ONE `aihub flush` for all of them. Never raises."""
    global _pending
    try:
        cid = _json("config.json").get("client_id")
        if not cid:
            return                                                  # the CLI has not named this machine yet (first hook run does)
        user = os.environ.get("USER") or os.environ.get("USERNAME") or os.environ.get("LOGNAME") or ""
        try:
            import socket
            host = socket.gethostname()
        except OSError:
            host = ""
        ev = {"id": uuid.uuid4().hex, "ts": time.time(), "kind": "use", "package": str(package or "aihub-ui")[:128],
              "component": str(component)[:128], "session": str(session or "")[:128], "client_id": cid, "source": "aihub-ui",
              "detail": {str(k)[:40]: (v if isinstance(v, (int, float, bool)) else str(v)[:200]) for k, v in (detail or {}).items()},
              "local_user": user, "host": host, "os": sys.platform, "user": _json("credentials.json").get("username") or "", "cwd": ""}
        line = (json.dumps(ev, separators=(",", ":")) + "\n").encode("utf-8", "replace")
        q = os.path.join(A.HOME(), "queue")
        os.makedirs(q, exist_ok=True)
        spool = os.path.join(q, "events.jsonl")
        if os.path.exists(spool) and os.path.getsize(spool) > 5 * 1024 * 1024:
            return                                                  # same cap as the CLI's own spool
        fd = os.open(spool, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        _pending = True
        kick()
    except Exception:
        pass


def kick():
    global _timer
    with _lock:
        if _timer is not None:
            return                                                  # an upload is already scheduled: it will carry this turn too
        _timer = threading.Timer(WINDOW, _run)
        _timer.daemon = True
        _timer.start()


def _run():
    global _timer, _pending
    with _lock:
        _timer = None
        flush, _pending = _pending, False
    env = dict(os.environ, AIHUB_NO_UPDATE="1")
    if flush:                                                       # all use events of the window leave in ONE batch
        try:
            subprocess.run(A.command() + ["flush"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=120, env=env)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        subprocess.run(A.command() + ["usage-sync"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=120, env=dict(os.environ, AIHUB_NO_UPDATE="1"))
    except (OSError, subprocess.SubprocessError):
        pass


def sync_state():
    """What the last upload did, for the Usage page: {ok: unix time or 0, fails, retry_at}."""
    try:
        with open(os.path.join(A.HOME(), "usage-sync.json"), encoding="utf-8") as f:
            d = json.load(f)
        return {"ok": d.get("ok") or 0, "fails": d.get("fails") or 0, "retry_at": d.get("retry_at") or 0}
    except (OSError, ValueError):
        return {"ok": 0, "fails": 0, "retry_at": 0}
