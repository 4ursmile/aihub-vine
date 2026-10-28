"""Usage of this UI -> the hub. Called after every answer turn (chat send, work run, free chat or Claude Code).

Claude Code turns are already in Claude's own session logs, which `aihub usage-sync` totals per day and uploads. Free chat talks to the
API directly, so nothing is logged for it: `note()` appends one line (numbers and the model name only) to ~/.aihub/ui/usage.jsonl, which
the CLI reads as the tool `aihub-ui`. Then `kick()` starts ONE `aihub usage-sync` after a short quiet window (a burst of turns, or a work
with many AI nodes, becomes a single upload). Nothing here may raise or block a turn."""
import json
import os
import subprocess
import threading
import time

from . import aihub as A

WINDOW = 6.0                       # seconds of quiet before the upload starts
_lock = threading.Lock()
_timer = None


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


def kick():
    global _timer
    with _lock:
        if _timer is not None:
            return                                                  # an upload is already scheduled: it will carry this turn too
        _timer = threading.Timer(WINDOW, _run)
        _timer.daemon = True
        _timer.start()


def _run():
    global _timer
    with _lock:
        _timer = None
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
