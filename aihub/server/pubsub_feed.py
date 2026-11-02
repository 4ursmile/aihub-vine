"""Pub/sub delivery engine: one per server process (`app.state.pubsub`), so the cost of a waiting subscriber is memory, not database work.

  * ONE pump thread per process tails `topic_messages` (id > head) the moment a publish in this process kicks it, and every TICK seconds
    otherwise (publishes of other workers). New rows go to an in-memory ring (the newest RING messages / RING_BYTES), each already encoded
    as the JSON the API returns, and every waiting long poll is woken. A waiter then filters the ring in memory: no query per waiter, no
    JSON work per subscriber. A position older than the ring (from=start, an old cursor) and `tail` read the database as before.
  * Ids are appended in order. A missing id (a transaction of another worker that has not committed yet, Postgres) holds the rows behind
    it for up to GAP_HOLD seconds, so a slow commit is not skipped; after that the gap is treated as a rollback.
  * Consumer statistics (delivered counts, "still subscribed") are added up in memory and written every FLUSH seconds in one transaction;
    retention runs here too (every PRUNE_EVERY seconds per topic that got messages, a sweep of all topics every SWEEP_EVERY seconds), so
    neither the publish nor the subscribe request pays for it.
  * The thread stops after IDLE_EXIT seconds without publishes or subscribers (writing what is buffered) and starts again on demand."""
import asyncio
import bisect
import json
import logging
import threading
import time

RING = 200000                 # messages kept in memory per process
RING_BYTES = 256 << 20        # ... and at most this many encoded bytes
TICK = 0.2                    # seconds between looks at the database when nothing was published in this process
PULL = 5000                   # rows read per look
GAP_HOLD = 1.0
FLUSH = 2.0
PRUNE_EVERY = 30.0
SWEEP_EVERY = 600.0
IDLE_EXIT = 120.0

log = logging.getLogger("aihub.pubsub")


def encode(m):
    """A message row -> the JSON bytes of one item of the API answer. A JSON body is stored compact and valid (the publish encodes it), so it
    is spliced in without parsing it again; headers likewise."""
    head = json.dumps({"id": m["id"], "subject": m["subject"], "ts": m["ts"], "key": m["mkey"] or "", "type": m["ctype"], "size": m["size"] or 0,
                       "by": m["username"] or ""}, ensure_ascii=False, separators=(",", ":"))
    body = m["body"] or ""
    data = body if m["ctype"] == "json" and body else json.dumps(body, ensure_ascii=False)
    return (head[:-1] + ',"data":' + data + ',"headers":' + (m["headers"] or "{}") + "}").encode("utf-8", "replace")


def answer(parts, nxt, cursor):
    """The whole /pubsub/messages answer from encoded items."""
    return b'{"items":[' + b",".join(parts) + b'],"next":' + json.dumps(nxt).encode() + b',"cursor":' + json.dumps(cursor or None).encode() + b"}"


class Notifier:
    """Wakes the long polls of this process (the pump runs in a thread, the waiters on the event loop)."""
    def __init__(self):
        self.seq = 0
        self._lock = threading.Lock()
        self._waiters = set()

    def bump(self):
        with self._lock:
            self.seq += 1
            ws, self._waiters = self._waiters, set()
        for loop, fut in ws:
            try:
                loop.call_soon_threadsafe(lambda f=fut: f.done() or f.set_result(None))
            except RuntimeError:                    # that loop is closed
                pass

    def waiting(self):
        return len(self._waiters)

    async def wait(self, seq, timeout):
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        with self._lock:
            if self.seq != seq:
                return
            self._waiters.add((loop, fut))
        try:
            await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            with self._lock:
                self._waiters.discard((loop, fut))


class Engine:
    def __init__(self, repos, eff):
        self.repos = repos
        self.eff = eff                              # record -> (retention hours, messages kept)
        self.note = Notifier()
        self._lock = threading.Lock()               # ring, buffers, thread state
        self._round = threading.Lock()              # one pump round at a time
        self._kick = threading.Event()
        self._thread = None
        self._closed = False
        self._used = time.monotonic()
        self._reset()
        self._deliv, self._seen = {}, set()         # {(tid, uid, pattern): n}, {(tid, uid, pattern)}
        self._dirty, self._pruned = {}, {}          # topics with new messages {tid: record}, {tid: last prune}
        self._flushed = self._swept = time.monotonic()

    def _reset(self):
        self.ids, self.parts, self.subj, self.tids = [], [], [], []      # parallel lists; live entries are [start:]
        self.start, self.nbytes, self._evicted = 0, 0, 0
        self.floor = None                           # the ring holds every message with floor < id <= head (None = not loaded)
        self.head = 0
        self._gap = None

    # ---------------------------------------------------------------- thread
    def ensure(self):
        self._used = time.monotonic()
        with self._lock:
            if self._closed or (self._thread and self._thread.is_alive()):
                return
            self._thread = threading.Thread(target=self._loop, name="aihub-pubsub", daemon=True)
            self._thread.start()

    def kick(self):
        self.ensure()
        self._kick.set()

    def close(self):
        with self._lock:
            self._closed = True
        self._kick.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(5)
        self.flush()

    def _loop(self):
        while True:
            self._kick.wait(TICK)
            self._kick.clear()
            try:
                self.step()
            except Exception:                       # never let the pump die: the next round tries again
                log.exception("pubsub pump")
                time.sleep(1)
            finally:
                try:
                    self.repos.db.release()
                except Exception:
                    pass
            with self._lock:
                idle = time.monotonic() - self._used > IDLE_EXIT and not self.note.waiting()
                if self._closed or idle:
                    self._thread = None
                    self._reset()                   # a later start loads from the current head instead of catching up on everything
                    break
        self.flush()

    def step(self):
        """One round: new rows into the ring, then statistics and retention when due. Also called by tests and before statistics reads."""
        with self._round:
            self._pull()
            now = time.monotonic()
            if now - self._flushed >= FLUSH:
                self._flushed = now
                self.flush()
            self._prune(now)

    # ---------------------------------------------------------------- ring
    def _pull(self):
        if self.floor is None:
            h = self.repos.topic_head()
            with self._lock:
                self.floor = self.head = h
            return
        rows = self.repos.topic_scan(None, "", self.head, PULL)
        if not rows:
            return
        now = time.monotonic()
        add, head = [], self.head
        for m in rows:
            if m["id"] != head + 1:                 # a hole: an uncommitted transaction of another worker, or a rollback
                if not self._gap or self._gap[0] != head + 1:
                    self._gap = (head + 1, now)
                if now - self._gap[1] < GAP_HOLD:
                    break
            self._gap = None
            add.append(m)
            head = m["id"]
        if not add:
            return
        with self._lock:
            for m in add:
                p = encode(m)
                self.ids.append(m["id"])
                self.parts.append(p)
                self.subj.append(m["subject"])
                self.tids.append(m["topic_id"])
                self.nbytes += len(p)
            self.head = head
            n = len(self.ids)
            while n - self.start > RING or (self.nbytes > RING_BYTES and n - self.start > 1):
                k = len(self.parts[self.start])
                self.nbytes -= k
                self._evicted += k
                self.start += 1
            if self.start:
                self.floor = self.ids[self.start - 1]
            if self.start > RING // 2 or self._evicted > RING_BYTES // 4:     # compact into NEW lists: a reader holding the old ones is unaffected
                s = self.start
                self.ids, self.parts, self.subj, self.tids = self.ids[s:], self.parts[s:], self.subj[s:], self.tids[s:]
                self.start, self._evicted = 0, 0
        self.note.bump()

    def backlog(self, after):
        """Messages in the ring after AFTER; None when the ring does not cover that position (read the database)."""
        with self._lock:
            if self.floor is None or after < self.floor:
                return None
            return len(self.ids) - bisect.bisect_right(self.ids, after, self.start)

    def read(self, after, limit, want, scan=50000):
        """-> (encoded items, {topic id: n}, next position) from the ring, or None when it does not cover AFTER. want(subject, topic_id)
        decides; at most SCAN entries are looked at per call (the position moves past them)."""
        self._used = time.monotonic()
        with self._lock:
            if self.floor is None or after < self.floor:
                return None
            ids, parts, subj, tids, head = self.ids, self.parts, self.subj, self.tids, self.head
            i, end = bisect.bisect_right(ids, after, self.start), len(ids)
        out, per, pos = [], {}, max(after, head) if i >= end else after
        stop = min(end, i + scan)
        while i < stop:
            if want(subj[i], tids[i]):
                out.append(parts[i])
                per[tids[i]] = per.get(tids[i], 0) + 1
                if len(out) >= limit:
                    pos = ids[i]
                    break
            pos = ids[i]
            i += 1
        else:
            if stop >= end:
                pos = max(pos, head)
        return out, per, pos

    # ---------------------------------------------------------------- statistics and retention
    def delivered(self, uid, per, pattern):
        p = pattern[:200]
        with self._lock:
            for tid, n in per.items():
                k = (tid, uid or 0, p)
                self._deliv[k] = self._deliv.get(k, 0) + n

    def seen(self, tids, uid, pattern):
        p = pattern[:200]
        with self._lock:
            if len(self._seen) < 100000:
                self._seen.update((t, uid or 0, p) for t in tids)

    def flush(self):
        with self._lock:
            deliv, seen, self._deliv, self._seen = self._deliv, self._seen, {}, set()
        if deliv or seen:
            try:
                self.repos.topic_clients_flush(deliv, seen)
            except Exception:
                log.exception("pubsub statistics")

    def published(self, records):
        self._used = time.monotonic()
        with self._lock:
            self._dirty.update(records)

    def _prune(self, now):
        with self._lock:
            due = [(tid, rec) for tid, rec in self._dirty.items() if now - self._pruned.get(tid, 0) >= PRUNE_EVERY]
            for tid, _ in due:
                self._dirty.pop(tid, None)
                self._pruned[tid] = now
            sweep = now - self._swept >= SWEEP_EVERY
            if sweep:
                self._swept = now
        for tid, rec in due:
            hours, keep = self.eff(rec)
            self.repos.topic_prune(tid, hours, keep)
        if sweep:
            every, _ = self.repos.topic_list({"bypass": True}, limit=5000)
            for rec in every:
                hours, keep = self.eff(rec)
                self.repos.topic_prune(rec["id"], hours, keep)
            with self._lock:
                if len(self._pruned) > 20000:
                    self._pruned.clear()

    def maintain(self):
        """Everything due now: pull, statistics, retention of every topic that got messages (tests, admin tools)."""
        with self._lock:
            self._pruned.clear()
            self._flushed = 0
        self.step()
