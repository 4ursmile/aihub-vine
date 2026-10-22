"""Group commit for `POST /api/v1/usage/sync`.

Many CLIs upload daily token totals after every answer turn. Writing each upload on its own (a lock, a SELECT, an UPSERT and a commit per
request) would make the hub do thousands of tiny transactions. Instead the rows wait in memory, merged per key
(user, device, day, tool, model), and ONE transaction writes them all when the window closes (`usage_db_interval`, default 5 s from the
first row), or earlier when the buffer is big. Memory is bounded by the number of distinct keys, not by the number of requests.

A day never shrinks (Claude Code deletes old logs, so a later upload can hold less than what is stored): when merging, the larger token
total wins, in memory and again in `Repos.usage_upsert`. Nothing acknowledged is lost: before the 2xx the rows are appended (and
fsynced) to a journal; a flush rotates it and deletes it only after the commit; the leftovers are replayed at start. If the journal
cannot be written `add` raises OSError and the route answers 503, so the CLI keeps its state and retries. A failed database write keeps
the rows and the journal for the next window, and refuses new uploads (503) once the buffer is far too big.

LOCKING (every rule exists because the opposite deadlocks, loses a read or stalls every upload):
  * `self.lock` guards the in-memory dicts and the journal append ONLY. It is held for microseconds plus one journal fsync in `add`.
    Never call the database, `sync.num()` (it reads the database), the cache or anything else that can block while holding it.
  * `self.wlock` serializes whole flushes: one writer at a time, so writes land in order, and a caller that waited for it knows that
    everything buffered before it asked is written (read your writes). Order is always wlock -> lock (released before the database);
    nothing ever takes wlock while holding lock. Request threads take wlock with a timeout and answer from committed data when it expires.
  * The transaction itself runs with neither of our locks held; inside it only `Repos.lock` is taken, which is never held while waiting
    for ours, so there is no cycle.
"""
import json
import logging
import os
import threading
import time
import uuid

log = logging.getLogger("aihub")
MAX_KEYS = 20_000       # flush early at this many distinct keys
HARD_KEYS = 400_000     # beyond this, refuse (503 + Retry-After) instead of growing without bound
FSYNC = True
FLUSH_WAIT = 3.0         # seconds a page view waits for the writer before it answers from what is committed
FIELDS = ("requests", "inp", "outp", "cache_write", "cache_read", "cost", "unpriced")


def _tok(r):
    return r["inp"] + r["outp"] + r["cache_write"] + r["cache_read"]


class UsageBuffer:
    def __init__(self, repos, sync=None, cache=None, journal_dir=None):
        self.repos, self.sync, self.cache, self.journal_dir = repos, sync, cache, journal_dir
        self.lock = threading.Lock()
        self.wlock = threading.Lock()                           # one flush at a time (see LOCKING)
        self._win = 5.0                                         # window seconds, refreshed OUTSIDE the lock
        self.rows, self.sess, self.first, self.jfiles = {}, {}, None, []
        self.inflight = set()                                     # user ids whose rows a running flush has taken but not yet committed        # key -> row; time of the first buffered row; journals not in the DB yet
        self.wake, self.stop = threading.Event(), threading.Event()
        self.stats = {"received": 0, "stored": 0, "flushes": 0, "last_flush": None, "last_error": None, "busy": 0, "failures": 0, "keys": 0}
        self.t = threading.Thread(target=self._loop, name="aihub-usage", daemon=True)
        self._replay()

    def window(self):
        """The cached window. Reading the setting hits the database, so only the loop thread refreshes it (`_refresh`), never a caller that
        holds `self.lock`."""
        return self._win

    def _refresh(self):
        try:
            self._win = float(self.sync.num("usage_db_interval", 5, 1, 300)) if self.sync else 5.0
        except Exception:
            pass

    def start(self):
        with self.lock:
            if not self.t.is_alive() and not self.stop.is_set():
                self.t.start()

    # ----- journal
    def _cur(self):
        return os.path.join(self.journal_dir, "current.jsonl")

    def _append(self, uid, client, rows, sessions=()):
        if not self.journal_dir:
            return
        os.makedirs(self.journal_dir, exist_ok=True)
        with open(self._cur(), "ab") as f:
            f.write((json.dumps({"u": uid, "c": client, "r": rows, "s": list(sessions)}, separators=(",", ":")) + "\n").encode("utf-8", "replace"))
            f.flush()
            if FSYNC:
                os.fsync(f.fileno())

    def _rotate(self):
        files, self.jfiles = self.jfiles, []
        if self.journal_dir and os.path.isfile(self._cur()):
            dst = os.path.join(self.journal_dir, "flush-%d-%s.jsonl" % (int(time.time()), uuid.uuid4().hex[:8]))
            os.replace(self._cur(), dst)
            files.append(dst)
        return files

    def _replay(self):
        if not self.journal_dir or not os.path.isdir(self.journal_dir):
            return
        for name in sorted(os.listdir(self.journal_dir)):
            path = os.path.join(self.journal_dir, name)
            if not name.endswith(".jsonl"):
                continue
            n = 0
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        try:
                            j = json.loads(line)
                            self._merge(int(j["u"]), str(j["c"]), j["r"], j.get("s") or ())
                            n += 1
                        except (ValueError, KeyError, TypeError):
                            continue                                # a torn last line from a crash: it was never acknowledged
            except OSError as ex:
                log.warning("usage: cannot read journal %s: %s", name, ex)
                continue
            self.jfiles.append(path)
            log.warning("usage: replaying %d upload(s) from %s", n, name)
        if self.rows or self.sess:
            self.first = time.time()

    # ----- intake
    def _merge(self, uid, client, rows, sessions=()):
        """Caller holds the lock (or is single threaded at start). Keeps the larger token total per key; a day or session never shrinks."""
        for r in rows:
            k = (uid, client, r["day"], r["tool"], r["model"])
            old = self.rows.get(k)
            if old is None or _tok(r) >= _tok(old):
                self.rows[k] = r
        for r in sessions:
            k = (uid, r["sid"])
            old = self.sess.get(k)
            if old is None or _tok(r) >= _tok(old):
                self.sess[k] = dict(r, peak=max(r["peak"], (old or {}).get("peak", 0)))

    def pending(self, uids=None):
        """True when rows of these users (anybody's when uids is None) are buffered OR are being written right now. The second half matters:
        a flush that already took the rows has an empty buffer, but its transaction has not committed, so a reader must still wait for it."""
        with self.lock:
            if uids is None:
                return bool(self.rows or self.sess or self.inflight)
            return (any(k[0] in uids for k in self.rows) or any(k[0] in uids for k in self.sess) or bool(self.inflight & set(uids)))

    def flush_for(self, uids):
        """Read-your-writes: a person who just finished a turn and opens the Usage page must see it. Writes the buffer only when it holds
        something of these users, so a busy hub still batches everybody else's rows."""
        if self.pending(set(uids)):
            self.flush(wait=FLUSH_WAIT)

    def full(self):
        with self.lock:
            if len(self.rows) + len(self.sess) >= HARD_KEYS:
                self.stats["busy"] += 1
                return True
            return False

    def add(self, uid, client, rows, sessions=()):
        """Journal, then buffer already validated rows (see routers._usage_rows / _usage_sessions). -> rows accepted. OSError = could not make
        them durable."""
        if not rows and not sessions:
            return 0
        with self.lock:
            self._append(uid, client, rows, sessions)
            self._merge(uid, client, rows, sessions)
            self.stats["received"] += len(rows) + len(sessions)
            if self.first is None:
                self.first = time.time()
            if len(self.rows) + len(self.sess) >= MAX_KEYS:
                self.wake.set()
        return len(rows) + len(sessions)

    # ----- write
    def flush(self, wait=None):
        """One transaction for everything buffered, one flush at a time. -> rows written (0 when nothing was buffered, the write failed, or
        another flush is still running and `wait` seconds passed). A failure keeps the rows and the journal for the next window.
        wait=None waits for a running flush (the loop, close); request threads pass a timeout so a stuck database cannot hold them."""
        if not (self.wlock.acquire() if wait is None else self.wlock.acquire(timeout=wait)):
            return 0
        try:
            with self.lock:
                if not self.rows and not self.sess:
                    return 0
                rows, sess, self.rows, self.sess, self.first = self.rows, self.sess, {}, {}, None
                files = self._rotate()
                self.inflight = {k[0] for k in rows} | {k[0] for k in sess}
            by = {}
            for (uid, client, *_), r in rows.items():
                by.setdefault((uid, client), []).append(r)
            try:
                n = self.repos.usage_upsert_many(by, sess)          # neither of our locks is held here
            except Exception as e:
                with self.lock:                                    # put the rows back (newer uploads for a key win)
                    self.inflight = set()
                    back = dict(rows)
                    back.update({k: v for k, v in self.rows.items() if k not in back or _tok(v) >= _tok(back[k])})
                    self.rows = back
                    sb = dict(sess)
                    sb.update({k: v for k, v in self.sess.items() if k not in sb or _tok(v) >= _tok(sb[k])})
                    self.sess = sb
                    self.jfiles = files + self.jfiles
                    self.first = self.first or time.time()
                self.stats["failures"] += 1
                self.stats["last_error"] = str(e)[:200]
                log.warning("usage: write failed, kept %d row(s): %s", len(rows), e)
                return 0
            with self.lock:
                self.inflight = set()
            for f in files:
                try:
                    os.remove(f)
                except OSError:
                    pass
            n += len(sess)
            self.stats.update(stored=self.stats["stored"] + n, flushes=self.stats["flushes"] + 1, last_flush=time.time(), last_error=None,
                              failures=0, keys=len(rows))
            if self.cache:
                self.cache.invalidate("usage")
            return n
        finally:
            with self.lock:
                self.inflight = set()                               # whatever happened above, nobody waits for a write that is over
            self.wlock.release()

    def _loop(self):
        while not self.stop.is_set():
            self._refresh()                                         # database read: never under self.lock
            with self.lock:
                wait = max(0.2, self.window() - (time.time() - self.first)) if self.first else 1.0
            self.wake.wait(min(wait, 2.0))
            self.wake.clear()
            if self.stop.is_set():
                break
            with self.lock:
                due = bool(self.rows or self.sess) and (len(self.rows) + len(self.sess) >= MAX_KEYS or time.time() - (self.first or time.time()) >= self.window())
            if due:
                try:
                    self.flush()
                except Exception:
                    log.exception("usage: flush crashed")
                    time.sleep(2)
                if self.stats["failures"] and self.stats["last_error"]:
                    time.sleep(min(30, 2 * self.stats["failures"]))

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.t.is_alive():
            self.t.join(timeout=5)
        try:
            self.flush()
        except Exception:
            pass
