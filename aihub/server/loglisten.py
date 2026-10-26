"""Admin > Listen: the server log, live in the browser.

A logging handler is attached to the root logger (and to uvicorn's own loggers, which do not propagate) only while an admin listens; it keeps
the last MAX lines in memory with a growing sequence number, and the page polls `GET /admin/logs?after=<seq>`. Nobody polling for IDLE
seconds = the handler detaches itself, so a forgotten tab costs nothing. Lines are redacted (`redact.text`) before they are kept, and the
poll's own access-log lines are dropped. Nothing is written to disk."""
import collections
import logging
import threading
import time

from ..core import redact

MAX = 2000
IDLE = 120
LOGGERS = ("", "uvicorn", "uvicorn.error", "uvicorn.access")
QUIET = ("/admin/logs",)                 # access lines of the poll itself


class _Handler(logging.Handler):
    def __init__(self, tap):
        super().__init__(logging.DEBUG)
        self.tap = tap

    def emit(self, rec):
        try:
            msg = rec.getMessage()
            if rec.exc_info:
                msg += "\n" + logging.Formatter().formatException(rec.exc_info)
        except Exception:
            msg = str(rec.msg)
        if any(q in msg for q in QUIET):
            return
        self.tap.add(rec.created, rec.levelname, rec.name, msg)


class Tap:
    def __init__(self):
        self.lock = threading.Lock()
        self.lines = collections.deque(maxlen=MAX)
        self.seq = 0
        self.handler = None
        self.polled = 0.0
        self.since = 0.0
        self.by = ""

    def add(self, ts, level, name, msg):
        with self.lock:
            self.seq += 1
            self.lines.append({"seq": self.seq, "ts": ts, "level": level, "name": name or "root", "msg": redact.text(msg, 4000)})

    def _attached(self):
        out = []
        for n in LOGGERS:
            lg = logging.getLogger(n)
            if n and lg.propagate:
                continue                  # reaches a handler above it already (no line twice)
            out.append(lg)
        return out

    def start(self, by=""):
        with self.lock:
            self.polled = time.time()
            if self.handler:
                return
            self.handler = _Handler(self)
            self.since, self.by = time.time(), by
            targets = self._attached()
        for lg in targets:
            lg.addHandler(self.handler)
        self.add(time.time(), "INFO", "aihub", "listening to the server log (started by %s)" % (by or "admin"))

    def stop(self):
        with self.lock:
            h, self.handler = self.handler, None
        if h:
            for n in LOGGERS:
                logging.getLogger(n).removeHandler(h)

    @property
    def listening(self):
        if self.handler and time.time() - self.polled > IDLE:
            self.stop()
        return self.handler is not None

    def read(self, after=0, limit=500):
        on = self.listening
        with self.lock:
            if on:
                self.polled = time.time()
            got = [x for x in self.lines if x["seq"] > after][-limit:]
            return {"listening": on, "seq": self.seq, "since": self.since if on else 0, "by": self.by if on else "", "lines": got}

    def clear(self):
        with self.lock:
            self.lines.clear()


TAP = Tap()
