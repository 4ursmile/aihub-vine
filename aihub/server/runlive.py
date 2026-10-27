"""Live side of `aihub run` on the hub (memory only, lost on restart): the last log lines each run sent, and who is looking.

A supervisor reports every 15 s; while someone has the Running page or a run's log open (`look`), the report reply asks for one every
2 s (`watched`), so stop/start from the web and new log lines show within seconds without the hub ever connecting to the machine."""
import collections
import threading
import time

KEEP = 1000          # log lines kept per run
WATCH = 30           # a look keeps the run "watched" this long
FAST, SLOW = 2, 15   # report interval asked for: watched / not


class RunLive:
    def __init__(self):
        self.lock = threading.Lock()
        self.logs = {}            # key -> deque[(seq, ts, line)]  (seq is the hub's own counter per run)
        self.last = {}            # key -> (supervisor start, last supervisor seq) already stored
        self.seq = {}
        self.looks = {}

    def add(self, key, sup, lines):
        with self.lock:
            q = self.logs.setdefault(key, collections.deque(maxlen=KEEP))
            s0, l0 = self.last.get(key, (None, 0))
            if s0 != sup:
                l0 = 0                                   # a new supervisor counts its lines from 1 again
            n = self.seq.get(key, 0)
            for x in lines:
                if x["seq"] <= l0:
                    continue                             # a report sent twice
                n += 1
                q.append((n, x["ts"], x["line"]))
                l0 = x["seq"]
            self.seq[key], self.last[key] = n, (sup, l0)

    def get(self, key, after=0):
        with self.lock:
            q = list(self.logs.get(key, ()))
            return {"lines": [{"seq": s, "ts": t, "line": ln} for s, t, ln in q if s > after], "seq": self.seq.get(key, 0)}

    def drop(self, key):
        with self.lock:
            for d in (self.logs, self.last, self.seq, self.looks):
                d.pop(key, None)

    def look(self, keys):
        now = time.time()
        with self.lock:
            for k in keys:
                self.looks[k] = now

    def watched(self, key):
        return time.time() - self.looks.get(key, 0) < WATCH

    def next(self, key, busy=False):
        return FAST if busy or self.watched(key) else SLOW
