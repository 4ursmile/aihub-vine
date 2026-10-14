"""Keeps the two git-heavy server jobs apart.

The event driven sync (inbox fetch, archive push, index push) and the cron backup both talk to the git host. Run close together they
look like a burst and trip the host's rate limit. Rules:

- "heavy" work is anything beyond one `ls-remote`: a fetch or a push. Heavy work of one kind may only START `gap` seconds after the
  other kind last finished heavy work, and never while the other kind is running it;
- the backup simply waits for that moment (it is a cron job, a short delay does not matter) and while it waits the sync steps aside;
- the sync never blocks: `heavy()` raises `Deferred(seconds)`, the pass stops quietly and the sync loop comes back then (events are
  already safe in the database, the git copy is only delayed);
- an idle sync pass (one `ls-remote`, nothing to fetch) is light: it never delays a backup and is never delayed by one;
- an admin pressing "Sync now" / "Back up now" (`force=True`) skips the gap but still waits for the other kind to finish.
"""
import contextlib
import threading
import time


class Deferred(BaseException):
    """A job has to wait out the gap. A BaseException so no generic `except Exception` in the sync code swallows it."""
    def __init__(self, left):
        super().__init__(left)
        self.left = left


class _Job:
    def __init__(self, gate, who, force):
        self.gate, self.who, self.force, self.heavy_on = gate, who, force, False

    def heavy(self):
        """Call before a fetch or a push (sync side). Raises Deferred when it is not allowed to start git work now."""
        if self.heavy_on:
            return
        g = self.gate
        with g.cv:
            other = g.other(self.who)
            if g.running[other]:
                if not self.force:
                    raise Deferred(30)
                while g.running[other]:
                    g.cv.wait(5)
            elif not self.force:
                if g.waiting[other]:
                    raise Deferred(30)                  # the other job is queued for the gap: let it go first
                left = g.ended[other] + g.gap - time.time()
                if left > 0:
                    raise Deferred(left)
            g.running[self.who] = self.heavy_on = True


class GitGate:
    def __init__(self, gap=300):
        self.cv = threading.Condition()
        self.gap = gap                                  # admin `git_job_gap`, refreshed by the jobs before each run
        self.running = {"sync": False, "backup": False}  # heavy git work in progress
        self.waiting = {"sync": False, "backup": False}  # queued for the gap (only the backup waits)
        self.ended = {"sync": 0.0, "backup": 0.0}       # when each kind last FINISHED heavy work

    @staticmethod
    def other(who):
        return "backup" if who == "sync" else "sync"

    def left(self, who):
        """Seconds until `who` may start heavy work (0 = now)."""
        with self.cv:
            o = self.other(who)
            return 30.0 if self.running[o] else max(0.0, self.ended[o] + self.gap - time.time())

    @contextlib.contextmanager
    def job(self, who, gap=None, stop=None, wait_gap=False, force=False):
        """Wraps one run of a job. wait_gap=True (the backup): block here until the other kind is idle and the gap is over; the whole
        run then counts as heavy. For the sync, heavy work is declared piece by piece with `job.heavy()`."""
        with self.cv:
            if gap is not None:
                self.gap = gap
            j = _Job(self, who, force)
            if wait_gap:
                o = self.other(who)
                self.waiting[who] = True
                try:
                    while True:
                        if stop is not None and stop.is_set():
                            raise InterruptedError("shutting down")
                        left = 0 if force else self.ended[o] + self.gap - time.time()
                        if not self.running[o] and left <= 0:
                            break
                        self.cv.wait(min(max(left, 1.0), 30.0))
                finally:
                    self.waiting[who] = False
                self.running[who] = j.heavy_on = True
        try:
            yield j
        finally:
            with self.cv:
                if j.heavy_on:
                    self.ended[who] = time.time()
                self.running[who] = False
                self.cv.notify_all()
