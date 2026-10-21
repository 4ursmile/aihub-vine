"""Telemetry that arrives over HTTP (`POST /api/v1/telemetry`): held in memory, written to the database in one batch.

The first event of a batch opens a window (`ingest_db_interval`, default 10 s); when it closes, everything collected is written in ONE
transaction, so a busy hub does a few large writes instead of one per request. A full buffer flushes early; a buffer that is way too
full refuses new batches with 503 + Retry-After and the CLI keeps them spooled. The raw lines are also handed to `Sync`, which
copies them to the git archive in its own (much slower) window.

No acknowledged event is lost: before a 2xx every accepted line is appended (and fsynced) to a journal file in `journal_dir`. A flush
rotates the file and deletes it only AFTER the database commit; at start the leftovers are replayed (inserts are keyed on the event id, so
replaying something already stored is harmless). If the journal cannot be written `add` raises OSError and the route answers 503, so the
CLI keeps its spool. A failed database write keeps both the rows and their journal files.
"""
import json
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict

from .sync import to_row

log = logging.getLogger("aihub")
MAX_ROWS = 5000             # flush early at this many buffered rows
HARD_ROWS = 100_000         # beyond this, refuse (503) instead of growing without bound
JOURNAL_FSYNC = True         # False: survives a killed process but not a power cut (faster)
SEEN_IDS = 200_000          # event ids remembered (LRU): a repeat upload skips the buffer and the database insert


class Ingest:
    def __init__(self, repos, sync, cache=None, journal_dir=None):
        self.repos, self.sync, self.cache = repos, sync, cache
        self.lock = threading.Lock()
        self.hlock = threading.Lock()                   # the per-address counters only: never wait behind a journal fsync
        self.journal_dir, self.jfiles = journal_dir, []   # jfiles: rotated journal files whose rows are not in the database yet
        self.rows, self.lines, self.pubs, self.first = [], [], {}, None
        self.wake, self.stop = threading.Event(), threading.Event()
        self.stats = {"received": 0, "stored": 0, "flushes": 0, "last_flush": None, "rejected": 0, "last_error": None,
                      "duplicates": 0, "rate_limited": 0, "busy": 0, "gzip": 0, "skipped": 0, "failures": 0, "last_skipped": None}
        self.t = threading.Thread(target=self._loop, name="aihub-ingest", daemon=True)
        self._rejects_seen = repos.event_rejects
        self.seen = OrderedDict()                       # event id -> True, oldest first
        self.hits = {}                                  # ip -> (window start, count): per-client request limit
        self._replay()

    def start(self):
        with self.lock:
            if not self.t.is_alive() and not self.stop.is_set():
                self.t.start()

    # ----- journal
    def _cur(self):
        return os.path.join(self.journal_dir, "current.jsonl")

    def _append(self, lines):
        """Durably append raw lines. Caller holds self.lock, so a flush can never rotate between 'journaled' and 'buffered'."""
        if not self.journal_dir:
            return
        os.makedirs(self.journal_dir, exist_ok=True)
        with open(self._cur(), "ab") as f:
            f.write(("\n".join(lines) + "\n").encode("utf-8", "replace"))
            f.flush()
            if JOURNAL_FSYNC:
                os.fsync(f.fileno())

    def _rotate(self):
        """Caller holds self.lock. -> every journal file covering the rows being taken for this flush."""
        files, self.jfiles = self.jfiles, []
        if self.journal_dir and os.path.isfile(self._cur()):
            dst = os.path.join(self.journal_dir, "flush-%d-%s.jsonl" % (int(time.time()), uuid.uuid4().hex[:8]))
            os.replace(self._cur(), dst)
            files.append(dst)
        return files

    def _replay(self):
        """Events acknowledged before a crash: back into the buffer (and the git queue), written at the first window."""
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
                            e = json.loads(line)
                            r = to_row(e)
                        except ValueError:
                            continue                                 # a torn last line from the crash: it was never acknowledged
                        if not r:
                            continue
                        if e.get("user"):
                            r["username"] = e["user"]
                        self.seen[r["ext_id"]] = True
                        self.rows.append(r)
                        self.lines.append(line.strip())
                        if r["kind"] == "publish" and e.get("version") and e.get("git_url"):
                            self.pubs[(r["package"], str(e["version"]))] = e
                        n += 1
            except OSError as ex:
                log.warning("ingest: cannot read journal %s: %s", name, ex)
                continue
            self.jfiles.append(path)                                 # deleted only after these rows are in the database
            log.warning("ingest: replaying %d event(s) from %s", n, name)
        if self.rows:
            self.first = time.time()

    def window(self):
        return self.sync.num("ingest_db_interval", 10, 1, 300)

    # ----- limits
    def allow(self, ip):
        """Per client address request budget per minute (admin `telemetry_rate_limit`, 0 = unlimited). -> (ok, retry_after_seconds)."""
        limit = int(self.sync.num("telemetry_rate_limit", 120, 0, 100000))
        if not limit:
            return True, 0
        now = time.time()
        with self.hlock:
            if len(self.hits) > 20000:
                self.hits = {k: v for k, v in self.hits.items() if now - v[0] < 60}
            start, n = self.hits.get(ip, (now, 0))
            if now - start >= 60:
                start, n = now, 0
            self.hits[ip] = (start, n + 1)
            ok = n < limit
            if not ok:
                self.stats["rate_limited"] += 1
            return ok, max(1, int(60 - (now - start)) + 1)

    def full(self):
        with self.lock:
            if len(self.rows) >= HARD_ROWS:
                self.stats["busy"] += 1
                return True
            return False

    # ----- intake
    def add(self, events, user=None):
        """events: dicts from a CLI. -> number accepted (valid rows). Never touches the database."""
        rows, lines, pubs = [], [], {}
        valid = 0
        for e in events:
            r = to_row(e)
            if not r:
                continue
            valid += 1
            if self._seen(r["ext_id"]):
                continue                                 # already received: counts as accepted, costs nothing
            if user:
                r["username"] = user                     # a signed-in sender is who the token says, not what the body claims
                e = dict(e, user=user)
            rows.append(r)
            lines.append(json.dumps(e, separators=(",", ":")))
            if r["kind"] == "publish" and e.get("version") and e.get("git_url"):
                pubs[(r["package"], str(e["version"]))] = e
        if not rows:
            self.stats["duplicates"] += valid
            return valid
        with self.lock:
            try:
                self._append(lines)                      # durable BEFORE the caller answers 2xx
            except OSError:
                for r in rows:
                    self.seen.pop(r["ext_id"], None)     # not stored: the CLI's retry must not be dropped as a repeat
                raise
            opened = not self.rows
            if opened:
                self.first = time.time()                 # the window runs from the FIRST event of the batch
            self.rows += rows
            self.lines += lines
            self.pubs.update(pubs)
            self.stats["received"] += len(rows)
            self.stats["duplicates"] += valid - len(rows)
            if pubs:
                self.first = min(self.first, time.time() - self.window())   # a publish closes the window now: it shows in the feed at once
            big = len(self.rows) >= MAX_ROWS or bool(pubs)
        self.start()                                     # lazy: works the same under a bare TestClient (no lifespan)
        if big or opened:
            self.wake.set()                              # a new window to plan, or a full buffer to write now
        return valid

    def _seen(self, ext_id):
        """True when this event id was received before; otherwise remember it (bounded LRU)."""
        with self.lock:
            if ext_id in self.seen:
                self.seen.move_to_end(ext_id)
                return True
            self.seen[ext_id] = True
            if len(self.seen) > SEEN_IDS:
                self.seen.popitem(last=False)
            return False

    # ----- flush
    def flush(self):
        """Write what is buffered. Safe to call from anywhere (shutdown, backup, tests). -> rows newly stored."""
        with self.lock:
            rows, lines, pubs = self.rows, self.lines, self.pubs
            self.rows, self.lines, self.pubs, self.first = [], [], {}, None
            files = self._rotate()
        if not rows:
            self._drop(files)
            return 0
        try:
            n = self.repos.events_insert_ext(rows)
        except Exception as e:                           # database busy / down: keep everything for the next window
            with self.lock:
                self.rows, self.lines = rows + self.rows, lines + self.lines
                self.pubs = dict(pubs, **self.pubs)
                self.first = self.first or time.time()
                self.jfiles = files + self.jfiles        # journal stays: nothing acknowledged is forgotten
            self.stats["last_error"] = str(e)[:200]
            self.stats["failures"] += 1
            log.warning("ingest: database write failed (%d rows kept, failure %d in a row): %s", len(rows), self.stats["failures"], e)
            raise
        finally:
            self.repos.db.release()
        self._drop(files)                                # committed: the journal copy has done its job
        skipped = self.repos.event_rejects - self._rejects_seen          # rows whose own content could not be stored: dropped, never retried
        self._rejects_seen = self.repos.event_rejects
        self.stats.update(stored=self.stats["stored"] + n, flushes=self.stats["flushes"] + 1, last_flush=time.time(), last_error=None, failures=0,
                          skipped=self.stats["skipped"] + skipped, last_skipped=self.repos.event_reject_last or self.stats["last_skipped"])
        if n and self.cache is not None:
            try:
                self.cache.invalidate("pkg")             # package lists, download counts, "Happening now"
            except Exception:
                pass
        self.sync.accept(lines, list(pubs.values()))     # git archive window + index staging
        return n

    def _drop(self, files):
        for p in files:
            try:
                os.remove(p)
            except OSError:
                pass

    def _loop(self):
        while not self.stop.is_set():
            with self.lock:
                first, n = self.first, len(self.rows)
            wait = 3600.0 if first is None else (0.0 if n >= MAX_ROWS else max(0.0, first + self.window() - time.time()))
            if wait > 0 and self.wake.wait(wait):
                self.wake.clear()
                continue                                 # something changed: re-plan with the current window
            if first is None:
                continue
            try:
                self.flush()
            except Exception:
                self.stop.wait(min(30.0, 2.0 * 2 ** min(self.stats["failures"], 4)))     # database trouble: events are kept; 4 s, 8 s ... 30 s

    def close(self):
        self.stop.set()
        self.wake.set()
        try:
            self.flush()
        except Exception:
            pass
