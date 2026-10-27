"""Pulls the world into SQLite: usage events from the git inbox, package metadata from the git index.

- Events: CLIs add files inbox/<client>-<unix>-<rand>.jsonl to the one `telemetry-inbox` branch. The server fetches them (only when
  `ls-remote` shows something new), stores the rows (events.ext_id is the event id and unique, so restarts and several servers never
  duplicate rows), copies the raw batch to the `telemetry-archive` branch, and only then removes the files. A failure at any step
  leaves them for the next pass.
- Index: the server is the only writer. A new `aihub.publish` event (public data sent by the CLI) makes the server fetch that
  package repo at the published commit, read its aihub.toml, update the index repo and push; then the index is re-read.
"""
import hashlib
import json
import logging
import os
import random
import shutil
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone

from ..cli import gitx
from ..core import naming, pkgindex, redact, textsafe, version as V
from . import direct as direct_mod, gitauth, nethost
from .gitgate import Deferred, GitGate

log = logging.getLogger("aihub")
KINDS = {"use", "install", "uninstall", "update", "error", "publish"}
ARCHIVE = "telemetry-archive"
INBOX = "telemetry-inbox"
MAX_BATCH_BYTES = 5 * 1024 * 1024       # a ref bigger than this is dropped unread (a client only ever sends ~BATCH events)
MAX_REFS = 500                          # refs ingested per pass; the rest wait for the next one
MAX_HELD = 2000                         # inbox files stored but not yet archived; at this many the git flush runs early
MAX_HELD_BYTES = 256 * 1024 * 1024      # raw batches waiting in memory for the archive push; the oldest are dropped beyond this
FLUSH_BYTES = 16 * 1024 * 1024          # ...but at this size the push runs early whatever the interval says
DEFAULT_POLL = "300"                    # git is only the fallback path now: one cheap `ls-remote` every 5 minutes is enough

# Admin-tunable numbers: key -> (default, min, max, label). One table drives validation, the admin page and Sync.num().
LIMITS = {
    "sync_interval_sec": None,          # placeholder so the name is not reused; the real schedule is the `sync_interval` text setting
    "ingest_db_interval": (10, 1, 300, "Seconds events wait in memory (from the first one) before ONE database write"),
    "usage_db_interval": (5, 1, 300, "Seconds token-usage uploads wait in memory (from the first one) before ONE database write"),
    "telemetry_git_interval": (3600, 0, 86400, "Seconds between archive + inbox clean-up pushes to git (0 = every sync pass)"),
    "index_git_interval": (1800, 0, 86400, "Seconds from the first publish until the index is pushed to git (the hub serves it at once)"),
    "git_job_gap": (300, 0, 3600, "Minimum seconds between git work of the sync job and the backup job"),
    "telemetry_rate_limit": (1200, 0, 100000, "Telemetry requests per minute per client address (0 = unlimited)"),
    "client_telemetry_interval": (60, 5, 3600, "Seconds a CLI gathers events before sending (sent to CLIs with their setup values)"),
    "client_index_ttl": (60, 5, 86400, "Seconds a CLI trusts its copy of the package index before asking the hub again"),
    "direct_max_mb": (100, 1, 2000, "Largest package archive (MB) the hub accepts from `aihub dev publish -i` (a reverse proxy may have a lower cap of its own)"),
    "transfer_repo_ttl": (120, 0, 86400, "Seconds a git checkout fetched for `aihub install -i` is trusted before the hub asks git again (a monorepo is fetched once for all its packages)"),
    "backup_direct_max_mb": (90, 1, 2000, "Largest package archive (MB, from `aihub dev publish -i`) copied into the git backup; bigger ones are skipped and listed in backups/index.json (git hosts refuse files over ~100 MB)"),
    "backup_jitter": (10, 0, 50, "Backup start time is moved by up to this percent of the wait"),
    "backup_jitter_max": (600, 0, 3600, "...but never by more than this many seconds"),
}
LIMITS.pop("sync_interval_sec")


from ..core.schedule import _field, next_delay, parse_schedule   # noqa: F401  (moved to core: the CLI uses them too)


SCHEMES = ("https://", "http://", "ssh://", "git@")      # transports a publish event may name (tests add file://)
OS_NAMES = {"darwin": "macOS", "linux": "Linux", "win32": "Windows", "cygwin": "Windows", "freebsd": "FreeBSD"}


def _os(v):
    """sys.platform from the CLI -> a display name. Untrusted input: unknown values are kept short and scrubbed."""
    v = str(v or "").strip().lower()
    if not v:
        return None
    return OS_NAMES.get(v) or redact.text(v, 24)


def _detail(e):
    """A publish event keeps WHERE it was published from as its detail (JSON), so the hub can always tell which repo, branch and folder an
    old version lives in (the index entry only keeps the latest location of a package)."""
    if e.get("kind") == "publish" and not e.get("detail") and e.get("git_url"):
        return json.dumps({"git_url": redact.text(e["git_url"], 300), "git_branch": redact.text(e.get("git_branch") or "main", 100),
                           "git_subdir": redact.text(e.get("git_subdir"), 200), "commit": redact.text(e.get("commit"), 64)},
                          separators=(",", ":"))     # each value scrubbed on its own: scrubbing the JSON text could cut or break it
    return redact.text(e.get("detail"), 2200)


def to_row(e):
    """One event from a CLI batch -> an events row (None if it is not one of ours). Everything in it is untrusted input."""
    if not isinstance(e, dict):
        return None
    raw_id = e.get("id")
    e = textsafe.deep(e)                                        # lone surrogates / NUL would fail the whole batch at the database
    if isinstance(raw_id, str) and textsafe.clean(raw_id) != raw_id:
        # "abc\udc80" and "abc\udc81" must stay two events: an id that had to be cleaned is replaced by a digest of its raw form
        e["id"] = "h" + hashlib.sha256(raw_id.encode("utf-8", "surrogatepass")).hexdigest()[:40]
    kind = e.get("kind")
    if kind not in KINDS or not e.get("package") or not e.get("id"):
        return None
    uid = str(e.get("user") or "")
    anon = uid.startswith("~") or not uid
    try:
        ts = min(float(e.get("ts")), time.time() + 86400)       # a client clock in the far future must not pin itself to "now" forever
        if ts != ts or ts in (float("inf"), float("-inf")):
            ts = time.time()                                    # NaN / infinity would poison sorting and charts
    except (TypeError, ValueError, OverflowError):
        ts = time.time()
    return {"ts": ts, "kind": kind, "package": naming.normalize(str(e["package"])),
            "version": redact.text(e.get("version"), 64), "client_id": redact.text(e.get("client_id"), 64),
            "username": None if anon else uid[:64], "component": str(e["component"])[:120] if e.get("component") else None,
            "duration": None,
            "source": e.get("source") if e.get("source") in ("claude", "opencode", "codex", "cli") else None,
            "local_user": redact.text(e.get("local_user"), 64),
            "host": redact.text(e.get("host"), 128), "os": _os(e.get("os")), "detail": _detail(e),
            "cwd": redact.text(e.get("cwd"), 200), "ip": None, "ext_id": str(e["id"])[:64]}


class Sync:
    def __init__(self, repos, settings, cache=None):
        self.repos, self.s, self.cache = repos, settings, cache          # cache: dropped after new events so counts and the feed update now
        self.stop, self.wake = threading.Event(), threading.Event()
        self.t = None
        self.first_pass = threading.Event()                 # set once the first pass after start has finished (ok or not)
        self._lookback_checked = False
        self.index_lock = threading.Lock()                  # one index writer at a time (event sync and manual forcing share a checkout)
        self.gate = GitGate()                               # shared with Backup: keeps the two git-heavy jobs apart
        self._tl = threading.local()                        # the gate job the current thread runs in (None outside a pass)
        self.num_cache = {}                                 # key -> (read at, value): admin numbers are read at most every 5 s
        # Events stored in the database but not yet copied to the archive branch / removed from the inbox. They leave in ONE git push per
        # `telemetry_git_interval`, counted from the first held batch, so a busy hub does not hit the git host once per pass.
        self.hold = threading.Lock()
        self._srv_key = None
        self._trim = []                                     # (kind, where) inbox entries the next _archive push also removes
        self.held, self.held_done, self.held_seen, self.held_bytes, self.held_first = [], [], set(), 0, None
        self.pending_pubs = {}                              # publish events from HTTP waiting for the sync thread
        self.pub_tries = {}                                 # (package, version) -> failed index attempts
        self.retry_at = 0.0                                 # earliest time the next attempt for failed publishes may run
        self.staged, self.staged_first = {}, None           # publishes already served by the hub, not yet pushed to the index repo
        self.defer_until = 0.0                              # a pass that had to wait for the backup's git gap resumes then
        self.index_dir = self.index_folder = None
        self.served_version, self._served, self._served_lock, self._file_reader = 0, None, threading.Lock(), None     # what the hub serves to CLIs (GET /index/...)
        self.status = {"last_run": None, "last_error": None, "events_error": None, "index_error": None, "archive_error": None, "events_total": 0, "index_packages": 0,
                       "archive_last": None, "archive_batches": 0, "archive_files": 0, "archive_commit": None, "inbox_pending": 0, "last_events": 0,
                       "git_pushes": 0, "deferred": None}

    # ----- config: env first, then admin settings
    def cfg(self, key, env, default=""):
        return self.s.env_get(env) or self.repos.setting(key, "") or default

    def num(self, key, default=None, lo=None, hi=None):
        """An admin-tunable number (see LIMITS): environment AIHUB_<KEY> > admin setting > default, clamped. Cached for 5 s."""
        spec = LIMITS.get(key)
        if spec:
            default, lo, hi = spec[0] if default is None else default, spec[1] if lo is None else lo, spec[2] if hi is None else hi
        hit = self.num_cache.get(key)
        if hit and time.time() - hit[0] < 5:
            return hit[1]
        raw = self.s.env_get("AIHUB_" + key.upper()) or self.repos.setting(key, "")
        try:
            v = float(raw) if raw not in (None, "") else float(default)
        except (TypeError, ValueError):
            v = float(default)
        v = max(lo, min(hi, v)) if lo is not None else v
        v = int(v) if v == int(v) else v
        self.num_cache[key] = (time.time(), v)
        return v

    def forget(self):
        self.num_cache.clear()

    def _flag(self, key, env):
        v = self.s.env_get(env) or self.repos.setting(key, "")
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    def telemetry_git(self):
        """Legacy git telemetry inbox + archive: admin setting `git_telemetry_sync` (env AIHUB_GIT_TELEMETRY_SYNC), OFF unless an admin turns it on."""
        return self._flag("git_telemetry_sync", "AIHUB_GIT_TELEMETRY_SYNC")

    def index_git(self):
        """Legacy git package index sync (read + push): admin setting `git_index_sync` (env AIHUB_GIT_INDEX_SYNC), OFF by default."""
        return self._flag("git_index_sync", "AIHUB_GIT_INDEX_SYNC")

    def inbox(self):
        """Git URL holding refs/telemetry/*: AIHUB_TELEMETRY_URL > admin setting > defaults.json > the package index repo."""
        from ..core import defaults
        return (self.cfg("telemetry_url", "AIHUB_TELEMETRY_URL") or defaults.load().get("telemetry", {}).get("git_url", "")
                or self.index_source()[0])

    def archive_branch(self):
        from ..core import defaults
        return self.cfg("telemetry_branch", "AIHUB_TELEMETRY_BRANCH") or defaults.load().get("telemetry", {}).get("branch", "") or ARCHIVE

    def delay(self):
        try:
            return next_delay(parse_schedule(self.repos.setting("sync_interval", "") or DEFAULT_POLL))
        except ValueError:
            return int(DEFAULT_POLL)

    def next_wait(self):
        """Seconds until this thread has something to do: the poll schedule, or sooner for a due archive push, a due index push or a
        publish waiting to be staged. While a pass is deferred (the backup job's git gap) nothing git related is due before `defer_until`,
        so that is a floor: the loop must not spin once a second until the gap ends. Never below 1."""
        now, waits = time.time(), [self.delay()]
        with self.hold:
            if self.pending_pubs:
                waits.append(max(0, self.retry_at - now))
            if self.held_first:
                waits.append(self.held_first + self.num("telemetry_git_interval") - now)
            if self.staged_first:
                waits.append(self.staged_first + self.num("index_git_interval") - now)
        wait = min(waits)
        if self.defer_until > now:
            wait = max(wait, self.defer_until - now)
        return max(1.0, wait)

    def git_flush_in(self):
        """Seconds until held events are pushed to git (None = nothing held)."""
        with self.hold:
            return None if not self.held_first else max(0, round(self.held_first + self.num("telemetry_git_interval") - time.time()))

    def index_push_in(self):
        with self.hold:
            return None if not self.staged_first else max(0, round(self.staged_first + self.num("index_git_interval") - time.time()))

    # ----- intake from HTTP (Ingest)
    def accept(self, lines, pubs):
        """Events the hub already stored in the database. Their raw lines wait for the next archive push; publish events for the index."""
        if not self.telemetry_git():
            lines = []                                               # legacy git sync off: no git archive backlog, the database is the copy
        lines = [self._unbundle(x) for x in lines]
        with self.hold:
            if lines:
                if self.held and self.held[-1][0].startswith("srv-") and self.held_first and self.held[-1][0] == self._srv_key:
                    self.held[-1][1].extend(lines)                   # same archive window: ONE file, not one per request
                else:
                    self._srv_key = "srv-%d-%s" % (int(time.time()), uuid.uuid4().hex[:8])
                    self.held.append((self._srv_key, list(lines)))
                self.held_bytes += sum(len(x) for x in lines)
                self.held_first = self.held_first or time.time()
                self._cap_held()
            for m in pubs:
                self.pending_pubs[(naming.normalize(str(m["package"])), str(m["version"]))] = m
        if pubs:
            self.wake.set()                                  # a publish should show up on the hub now, not at the next poll

    @staticmethod
    def _unbundle(line):
        """The archive keeps the event, not the inline manifest/README bytes (those live in the index)."""
        if '"bundle"' not in line:
            return line
        try:
            e = json.loads(line)
            e.pop("bundle", None)
            return json.dumps(e, separators=(",", ":"))
        except ValueError:
            return line

    def _cap_held(self):
        """Held batches live in memory. Past the cap the oldest are dropped: they are already in the database (and its backup)."""
        while self.held_bytes > MAX_HELD_BYTES and len(self.held) > 1:
            key, lines = self.held.pop(0)
            self.held_bytes -= sum(len(x) for x in lines)
            log.warning("sync: archive backlog too large; batch %s is not archived to git (it is in the database)", key)

    def git_login(self, host):
        """The login the server's git uses for `host` (see gitauth.login_for), or None."""
        return gitauth.login_for(self.s, host, [self.index_source()[0], self.inbox()])

    def start(self):
        gitauth.install(self.s, lambda: [self.index_source()[0], self.inbox()])    # no prompts, trusted data folder, stored login (token / vault)
        url, branch, path = self.index_source()
        log.info("sync: usage inbox %s (branch %s, archive %s); package index %s (branch %s, folder %s)",
                 gitx.redact_url(self.inbox()) or "-", self.inbox_branch(), self.archive_branch(), gitx.redact_url(url) or "-", branch, path)
        try:
            if self.repos.setting("version_repo_backfill") != "1":
                self.repair_version_repos()
                self.repos.set_setting("version_repo_backfill", "1")
        except Exception as e:                                          # never stops the hub from starting; retried at the next start
            log.warning("sync: repairing version locations failed: %s", e)
        self.t = threading.Thread(target=self._loop, name="aihub-sync", daemon=True)
        self.t.start()

    def close(self):
        self.stop.set()
        self.wake.set()

    def _loop(self):
        self._repair_from_archive()
        while not self.stop.is_set():
            try:
                self.run_once(scheduled=True)                # each half is gated inside _pass; with both off only local staging happens
            except Exception as e:                       # never die; report on the admin page
                self.status["last_error"] = str(e)[:300]
                log.warning("sync failed: %s", e)
            finally:
                self.repos.db.release()
                self.first_pass.set()
            self.wake.wait(self.next_wait() if self.telemetry_git() or self.index_git() else 30)
            self.wake.clear()

    # ----- one pass
    def _heavy(self):
        """Declare a fetch or a push to the gate (see GitGate): raises Deferred when the backup job is busy with git or just was."""
        job = getattr(self._tl, "job", None)
        if job is not None:
            job.heavy()

    def run_once(self, scheduled=False):
        """One pass. The two halves are independent: if the usage inbox cannot be read (repo down, no access, bad credentials), the package
        index is still synced, and the other way round. Every failure is kept in `status.last_error` (shown in Admin > Sync) until a pass
        succeeds completely; a step that fails never reports success and never lets the cursor claim the pass completed.

        scheduled=True (the background loop): git writes wait for their windows (`telemetry_git_interval`, `index_git_interval`) and for the
        backup job's gap. scheduled=False ("Sync now", tests): everything held is written to git right now."""
        if scheduled and time.time() < self.defer_until:
            return                                                      # waiting out the backup's git gap; HTTP work below still ran in Ingest
        self.forget()                                                   # settings changed in the admin page apply to this pass
        with self.gate.job("sync", gap=self.num("git_job_gap"), stop=self.stop, force=not scheduled) as job:
            self._tl.job = job
            try:
                return self._pass(not scheduled)
            finally:
                self._tl.job = None

    def _pass(self, now_all):
        first = not self.repos.setting("sync_cursor")
        errors, new_publish, deferred = [], False, []
        self.status["events_error"] = self.status["index_error"] = None

        def guarded(fn):
            try:
                return fn()
            except Deferred as d:
                deferred.append(d.left)

        tel, idx = now_all or self.telemetry_git(), now_all or self.index_git()     # "Sync now" runs both halves
        if tel and self.inbox() and not os.path.isfile(self.inbox()):
            try:
                if self._needs_replay():
                    guarded(self.replay_archive)
                new_publish = bool(guarded(lambda: self.pull_events(scheduled=not now_all)))
            except Exception as e:
                msg = "usage sync: " + gitx.redact_url(str(e))[:240]
                self.status["events_error"] = msg
                errors.append(msg)
                log.warning("sync: %s", msg)
            if self.status.get("archive_error"):                        # pull_events handled it and carried on; it must still be visible
                errors.append(self.status["archive_error"])
        try:
            if not now_all:
                guarded(self._stage_pending)
                if tel:
                    guarded(self._flush_held_if_due)
                if idx:
                    guarded(self._flush_index_if_due)
            else:
                self.retry_at = 0.0
                self._stage_pending()
                self._flush_held(force=True)
                self._flush_index(force=True)
        except Exception as e:
            msg = gitx.redact_url(str(e))[:250]
            self.status["index_error"] = msg
            errors.append(msg)
            log.warning("sync: %s", msg)
        self.status["last_run"] = time.time()
        self.status["last_error"] = " | ".join(errors) if errors else None
        if deferred:                                                    # not an error: the git copy is only delayed; events are in the database
            wait = max(deferred) + random.uniform(0, 15)
            self.defer_until = time.time() + wait
            self.status["deferred"] = "git work waits %ds for the backup job's git gap" % wait
            log.info("sync: %s", self.status["deferred"])
        else:
            self.defer_until, self.status["deferred"] = 0.0, None
        if errors:
            raise RuntimeError(self.status["last_error"])               # the loop and "Sync now" report it; the cursor is NOT advanced
        if not deferred:
            self.repos.set_setting("sync_cursor", time.time())         # "an events pass completed"; empty = never (first run, forced re-sync)

    def _index_moved(self):
        """True when the index branch has a commit this server has not read (someone pushed to it directly, or a publish event was missed).
        One `ls-remote`: the index is no longer re-read only when a publish event arrives."""
        url, branch, _ = self.index_source()
        if not url or os.path.exists(url):
            return bool(url)                                            # local file/folder: pull_index compares mtime/digest itself
        head = gitx.remote_head(url, branch)
        return not head or head != self.repos.setting("sync_index_rev")

    def _needs_replay(self):
        """True when the cursor says events were pulled before but the table holds none (database replaced / lost), or an admin asked
        for a full re-sync (cursor cleared). The archive branch then restores everything the refs no longer carry."""
        if not self.repos.setting("sync_cursor"):
            return True
        if self._lookback_checked:
            return False
        self._lookback_checked = True
        return not self.repos.db.conn().execute("SELECT 1 FROM events WHERE ext_id IS NOT NULL LIMIT 1").fetchone()

    def replay_archive(self):
        """Re-insert every event stored on the archive branch. Idempotent (ext_id is unique), so running it over a healthy database
        adds nothing. -> number of rows inserted."""
        url, branch = self.inbox(), self.archive_branch()
        d = self._inbox_dir(url)
        self._heavy()
        tip = gitx.fetch_tip(d, "origin", branch, "refs/inbox/archive", depth=1, timeout=300)
        if not tip:
            return 0                                                    # nothing archived yet (or archiving was off)
        total, rows = 0, []
        for line in self._archive_lines(d, tip):
            try:
                r = to_row(json.loads(line))
            except ValueError:
                continue
            if r:
                rows.append(r)
            if len(rows) >= 1000:
                total += self._store(rows)
                rows = []
        total += self._store(rows)
        if total:
            log.info("restored %d event(s) from the %s branch", total, branch)
        self.status["events_total"] += total
        return total

    @staticmethod
    def _archive_lines(d, tip):
        """Every raw event line on the archive commit `tip` (blobs read 200 at a time in ONE git process)."""
        files = []
        for line in gitx.run(["ls-tree", "-r", tip, "--", "archive/"], cwd=d, check=False).splitlines():
            meta, _, path = line.partition("\t")
            if path.endswith(".jsonl"):
                files.append((path, meta.split()[2]))
        files.sort()
        for i, (f, blob) in enumerate(files):
            if i % 200 == 0:
                texts = gitx.blobs(d, [b for _, b in files[i:i + 200]])
            yield from texts.get(blob, "").splitlines()

    def _inbox_dir(self, url):
        work = os.path.abspath(os.path.join(self.s.data_dir, "telemetry-repo"))
        os.makedirs(work, exist_ok=True)
        d = gitx.cache_dir(url, work)
        if not os.path.isdir(os.path.join(d, ".git")):
            gitx.run(["init", "-q"], cwd=d)
            gitx.run(["remote", "add", "origin", url], cwd=d)
        else:
            gitx.run(["remote", "set-url", "origin", url], cwd=d)
        return d

    def inbox_branch(self):
        from ..core import defaults
        return (self.cfg("telemetry_inbox_branch", "AIHUB_TELEMETRY_INBOX_BRANCH") or defaults.load().get("telemetry", {}).get("inbox_branch", "")
                or INBOX)

    def _remote_refs(self, d):
        """{ref: sha} of everything telemetry related on the inbox remote, in ONE `ls-remote`. Raises when it cannot be read."""
        out = {}
        for line in gitx.run(["ls-remote", "origin"], cwd=d, timeout=60).splitlines():
            sha, _, ref = line.partition("\t")
            if ref.startswith(("refs/heads/", "refs/telemetry/")):
                out[ref] = sha
        return out

    def _collect(self, d):
        """Everything waiting to be ingested, as [(key, blob, kind, where)]. `key` names the batch (<client>-<unix>-<rand>), `blob` is the git
        object holding its JSON lines. Three sources, so nothing a CLI of any age has sent is lost:
          inbox   files inbox/<batch>.jsonl on the ONE inbox branch (current CLIs; the branch count never grows however long the server was down)
          heads   one branch telemetry/<client>/<batch> per batch (the previous CLI layout)
          legacy  one hidden ref refs/telemetry/<client>/<batch> (the first CLI layout)
        One `ls-remote` decides what to fetch: an idle inbox (tip unchanged since the last pass, no old-layout refs) costs no fetch at all."""
        remote = self._remote_refs(d)
        self._archive_tip = remote.get("refs/heads/" + self.archive_branch(), "")
        if self._archive_tip and not self.status.get("archive_commit"):             # after a restart: show what the archive already holds
            self.status["archive_commit"] = self._archive_tip[:10]
            if gitx.run(["rev-parse", "-q", "--verify", self._archive_tip + "^{commit}"], cwd=d, check=False):
                self._count_archive(d, self._archive_tip)                   # only when we already have it locally: no fetch for a label
        local = {}
        for line in gitx.run(["for-each-ref", "--format=%(objectname) %(refname)", "refs/inbox/"], cwd=d, check=False).splitlines():
            sha, _, ref = line.partition(" ")
            local[ref] = sha
        stale = [r for r in local if r.startswith(("refs/inbox/heads/", "refs/inbox/legacy/"))]
        if stale:
            gitx.run(["update-ref", "--stdin"], cwd=d, input="".join("delete %s\n" % r for r in stale), check=False)
        specs, tip = [], remote.get("refs/heads/" + self.inbox_branch(), "")
        if tip and local.get("refs/inbox/tip") != tip:
            specs.append("+refs/heads/%s:refs/inbox/tip" % self.inbox_branch())
        if not tip and "refs/inbox/tip" in local:
            gitx.run(["update-ref", "-d", "refs/inbox/tip"], cwd=d, check=False)
        old = [r for r in remote if r.startswith(("refs/heads/telemetry/", "refs/telemetry/"))]
        if any(r.startswith("refs/heads/telemetry/") for r in old):
            specs.append("+refs/heads/telemetry/*:refs/inbox/heads/*")
        if any(r.startswith("refs/telemetry/") for r in old):
            specs.append("+refs/telemetry/*:refs/inbox/legacy/*")
        if specs:
            self._heavy()
            gitx.run(["fetch", "-q", "--no-tags", "--depth", "1", "origin"] + specs, cwd=d, timeout=300)
        self._remote = remote
        out = []
        if tip:
            for line in gitx.run(["ls-tree", "-r", "refs/inbox/tip", "--", "inbox/"], cwd=d).splitlines():
                meta, _, path = line.partition("\t")
                if path.endswith(".jsonl"):
                    out.append((os.path.basename(path)[:-len(".jsonl")], meta.split()[2], "inbox", path))
        if old:
            refs = gitx.run(["for-each-ref", "--format=%(refname)", "refs/inbox/heads/", "refs/inbox/legacy/"], cwd=d, check=False).splitlines()
            found = gitx.objects(d, [r + ":events.jsonl" for r in refs])
            for ref in refs:
                if ref + ":events.jsonl" in found:
                    kind = "heads" if ref.startswith("refs/inbox/heads/") else "legacy"
                    out.append(("-".join(ref.split("/")[-2:]), found[ref + ":events.jsonl"][0], kind, ref))
        out = sorted(out)
        self.status["inbox_pending"] = len(out)
        with self.hold:
            skip = self.held_seen
            return [o for o in out if o[0] not in skip][:MAX_REFS]       # stored already, waiting for the next git push: not read again

    def pull_events(self, scheduled=False):
        """Fetch every pending batch, store its rows and hold the raw batch for the archive. -> True when a publish event arrived.

        Called directly (tests, "Sync now") it also writes to git right away: archive copy, inbox clean-up and index update in as few
        pushes as possible. From the background loop (scheduled=True) git writes wait for their window instead (`_flush_held_if_due`):
        the rows are in the database either way, so nothing is lost by waiting."""
        url = self.inbox()
        self.status["archive_error"] = None
        d = self._inbox_dir(url)
        found = self._collect(d)
        if not found:
            log.debug("sync: no new batches on %s", self.inbox_branch())
            if not scheduled:
                self._flush_held(force=True)                            # earlier batches whose git write failed
            return False                                                # idle inbox: nothing to read, archive, trim or gc
        sizes = gitx.objects(d, [blob for _, blob, _, _ in found])
        rows, pubs, publish, batches, done, texts = [], {}, False, [], [], {}
        for i, (key, blob, kind, where) in enumerate(found):
            if i % 50 == 0:                                             # read blobs 50 at a time: one git process each, bounded memory
                chunk = [b for _, b, _, _ in found[i:i + 50] if b in sizes and sizes[b][2] <= MAX_BATCH_BYTES]
                texts = gitx.blobs(d, chunk)
            raw = texts.get(blob, "")
            good = []
            for line in raw.splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                r = to_row(e)
                if not r:
                    continue
                good.append(line)
                rows.append(r)
                publish = publish or r["kind"] == "publish"
                if r["kind"] == "publish" and e.get("version") and e.get("git_url"):
                    pubs[(r["package"], str(e["version"]))] = e                 # latest event per version wins
            batches.append((key, good))
            done.append((kind, where, key))
            if len(rows) >= 1000:
                self._store(rows)
                rows = []
        n = self._store(rows)
        self.status["events_total"] += n
        self.status["last_events"] = n
        if n and self.cache is not None:
            try:
                self.cache.invalidate("pkg")                            # package lists, download counts, "Happening now"
            except Exception:
                pass
        log.info("sync: %d batch(es) read from %s, %d new event(s) stored", len(found), self.inbox_branch(), n)
        changed = False
        if pubs:
            try:
                if scheduled:
                    with self.hold:
                        self.pending_pubs.update(pubs)                  # staged (served at once) by this same pass; pushed after the index window
                else:
                    changed = bool(self.update_index(list(pubs.values())))
            except Exception as e:                                      # events are stored; the batches stay so the next pass retries the index
                log.warning("index update failed: %s", gitx.redact_url(str(e)))
                raise RuntimeError("index update: " + gitx.redact_url(str(e))[:240])      # run_once records it: a quiet 'return' showed OK while broken
        with self.hold:
            self.held += batches
            self.held_done += done
            self.held_seen.update(k for _, _, k in done)
            self.held_bytes += sum(len(x) for _, good in batches for x in good)
            self.held_first = self.held_first or time.time()
        if not scheduled:
            self._flush_held(force=True)
        # a retried batch (its rows were stored by a pass whose index update failed) inserts nothing, but if the index changed now it
        # must still be re-read
        return (publish and n > 0) or changed

    # ----- git writes: everything held leaves in ONE push
    def _flush_held_if_due(self):
        with self.hold:
            if not (self.held or self.held_done):
                return
            iv = self.num("telemetry_git_interval")
            due = (time.time() >= (self.held_first or 0) + iv or self.held_bytes >= FLUSH_BYTES or len(self.held_done) >= MAX_HELD
                   or self.stop.is_set())
        if due:
            self._flush_held()

    def _flush_held(self, force=False):
        """Archive every held batch, remove the ingested inbox files and old-layout refs, in one `git push --atomic`. -> True when done."""
        with self.hold:
            batches, done = list(self.held), list(self.held_done)
        if not batches and not done:
            return True
        if not self.inbox() or os.path.isfile(self.inbox()):
            return True
        self._heavy()
        d = self._inbox_dir(self.inbox())
        self._trim = [(kind, where) for kind, where, _ in done]         # removed from the inbox by the SAME push that archives them
        try:
            ok = self._archive(d, [(k, lines) for k, lines in batches])
        finally:
            self._trim = []
        if ok:
            gone, keys = {id(b) for b in batches}, {k for _, _, k in done}
            gone_d = {id(x) for x in done}
            with self.hold:
                self.held = [b for b in self.held if id(b) not in gone]
                self.held_done = [x for x in self.held_done if id(x) not in gone_d]
                self.held_seen -= keys
                self.held_bytes = sum(len(x) for _, lines in self.held for x in lines)
                self.held_first = time.time() if (self.held or self.held_done) else None
                self._srv_key = None
            gitx.run(["gc", "-q", "--auto", "--prune=now"], cwd=d, check=False, timeout=120)     # repacks only once objects pile up
        return ok

    def _archive(self, d, batches):
        """Build the archive commit and the inbox clean-up commit locally, then publish both with a single atomic push (all or nothing, so
        a batch is never deleted from the inbox without its archive copy, and the git host sees ONE request, not two or three).
        A rejected push (a CLI added a file / someone pushed to the archive meanwhile) rebuilds both on the new tips and tries again."""
        done = self._trim
        d = os.path.abspath(d)
        work = os.path.join(d, ".archive")
        abranch, ibranch, aref = self.archive_branch(), self.inbox_branch(), "refs/inbox/archive"
        archive_on = self.repos.setting("telemetry_archive", "1") != "0"
        batches = [(k, good) for k, good in batches if good] if archive_on else []
        files = sorted({w for kind, w in done if kind == "inbox"})
        olds = []
        for kind, w in done:
            if kind == "heads":
                olds.append("refs/heads/telemetry/" + w[len("refs/inbox/heads/"):])
            elif kind == "legacy":
                olds.append("refs/telemetry/" + w[len("refs/inbox/legacy/"):])
        try:
            self._clear_worktree(d, work)                               # leftovers of an older server (it used a worktree here)
            os.makedirs(work)
            names = []
            for i, (key, good) in enumerate(batches):
                f = os.path.join(work, "%d.jsonl" % i)
                with open(f, "w", encoding="utf-8", errors="replace", newline="\n") as fh:          # explicit: the platform default breaks on non-ASCII
                    fh.write(textsafe.clean("\n".join(good)) + "\n")
                names.append(f)
            shas = gitx.run(["hash-object", "-w", "--stdin-paths", "--no-filters"], cwd=d, input="".join(n + "\n" for n in names)).split() if names else []
            entries = []
            for (key, _), sha in zip(batches, shas):
                parts = key.split("-")                                  # <client>-<unix>-<rand>
                stamp = parts[-2] if len(parts) >= 3 else ""
                day = time.strftime("%Y-%m-%d", time.gmtime(int(stamp) if stamp.isdigit() else time.time()))
                entries.append("100644 %s\tarchive/%s/%s.jsonl\n" % (sha, day, key))
            known = getattr(self, "_archive_tip", None)
            remote = getattr(self, "_remote", None)
            for attempt in range(4):
                if attempt:
                    time.sleep(random.uniform(0.2, 1.0) * attempt)
                    remote = self._remote_refs(d)                       # one light ls-remote: what exists now
                specs, a_commit, t_commit = [], None, None
                if entries:
                    have = gitx.run(["rev-parse", "-q", "--verify", aref], cwd=d, check=False)
                    if attempt or known is None or (known and known != have):
                        base = gitx.fetch_tip(d, "origin", abranch, aref, depth=1)
                    else:
                        base = known                                    # the ls-remote of this pass says our copy is current
                    env = {"GIT_INDEX_FILE": os.path.join(d, "index.archive")}
                    gitx.run(["read-tree", base or "--empty"], cwd=d, extra_env=env)
                    gitx.run(["update-index", "--index-info"], cwd=d, input="".join(entries), extra_env=env)
                    tree = gitx.run(["write-tree"], cwd=d, extra_env=env)
                    if not (base and tree == gitx.run(["rev-parse", base + "^{tree}"], cwd=d)):          # unchanged tree = already archived
                        a_commit = gitx.run(["-c", "user.name=aihub-server", "-c", "user.email=aihub-server@localhost", "commit-tree", tree,
                                             "-m", "telemetry: %d batch(es)" % len(batches)] + (["-p", base] if base else []), cwd=d)
                        specs.append("%s:refs/heads/%s" % (a_commit, abranch))
                if files:
                    tip = (gitx.fetch_tip(d, "origin", ibranch, "refs/inbox/tip", depth=1) if attempt
                           else gitx.run(["rev-parse", "-q", "--verify", "refs/inbox/tip"], cwd=d, check=False))
                    present = sorted(set(files) & set(gitx.run(["ls-tree", "-r", "--name-only", tip, "--", "inbox/"], cwd=d).splitlines())) if tip else []
                    if present:
                        env = {"GIT_INDEX_FILE": os.path.join(d, "index.trim")}
                        gitx.run(["read-tree", tip], cwd=d, extra_env=env)
                        for i in range(0, len(present), 200):
                            gitx.run(["update-index", "--force-remove", "--"] + present[i:i + 200], cwd=d, extra_env=env)
                        t_commit = gitx.run(["-c", "user.name=aihub-server", "-c", "user.email=aihub-server@localhost", "commit-tree",
                                             gitx.run(["write-tree"], cwd=d, extra_env=env), "-p", tip, "-m",
                                             "telemetry: archived %d batch(es)" % len(present)], cwd=d)
                        specs.append("%s:refs/heads/%s" % (t_commit, ibranch))
                live = [o for o in olds if remote is None or o in remote]
                specs += [":" + o for o in live[:100]]                  # old layouts: delete in the same push (the rest right after)
                if specs:
                    try:
                        gitx.run(["push", "-q", "--atomic", "origin"] + specs, cwd=d, timeout=120)
                    except gitx.GitError as e:
                        if attempt == 3:
                            raise
                        log.info("telemetry push rejected (%s); rebuilding on the new tips", gitx.redact_url(str(e))[:120])
                        continue
                    self.status["git_pushes"] += 1
                for i in range(100, len(live), 100):
                    gitx.run(["push", "-q", "origin"] + [":" + o for o in live[i:i + 100]], cwd=d, check=False, timeout=60)
                if a_commit:
                    gitx.run(["update-ref", aref, a_commit], cwd=d, check=False)
                    self._archive_tip = a_commit
                    self.status.update(archive_last=time.time(), archive_batches=self.status.get("archive_batches", 0) + len(batches),
                                       archive_commit=a_commit[:10])
                    self._count_archive(d, a_commit)
                if t_commit:
                    gitx.run(["update-ref", "refs/inbox/tip", t_commit], cwd=d, check=False)    # next pass sees the remote unchanged: no fetch
                local = [w for kind, w in done if w.startswith("refs/")]
                if local:
                    gitx.run(["update-ref", "--stdin"], cwd=d, input="".join("delete %s\n" % r for r in local), check=False)
                log.info("sync: archived %d batch(es) and cleaned %d inbox file(s) with %s", len(batches), len(files), "1 push" if specs else "no push")
                return True
            return False
        except gitx.GitError as e:
            self.status["archive_error"] = "archiving usage: " + gitx.redact_url(str(e))[:220]
            log.warning("telemetry archive failed (batches kept): %s", gitx.redact_url(str(e))[:200])
            return False
        except Exception as e:
            self.status["archive_error"] = "archiving usage: " + gitx.redact_url(str(e))[:220]
            log.warning("telemetry archive failed (batches kept): %s", gitx.redact_url(str(e))[:200])
            return False
        finally:
            self._archive_tip = None
            self._clear_worktree(d, work)

    def _count_archive(self, d, rev):
        """Files on the archive branch, for the admin page (one cheap local ls-tree of an object we already have)."""
        out = gitx.run(["ls-tree", "-r", "--name-only", rev, "--", "archive/"], cwd=d, check=False)
        self.status["archive_files"] = len(out.splitlines()) if out else 0

    @staticmethod
    def _clear_worktree(d, work):
        """Leave no trace of the scratch checkout. A server stopped or killed mid-archive leaves the folder behind, sometimes registered as a
        worktree and sometimes not, and git then refuses to create it again ("already exists") on EVERY later pass: nothing would ever be
        stored, archived or deleted. So unregister it and delete the folder whatever its state."""
        import shutil
        if not os.path.exists(work) and not os.path.isdir(os.path.join(d, ".git", "worktrees")):
            return                                                      # the usual case: nothing to clean, no git process
        gitx.run(["worktree", "remove", "--force", work], cwd=d, check=False)
        shutil.rmtree(work, ignore_errors=True)
        gitx.run(["worktree", "prune"], cwd=d, check=False)

    def update_index(self, pubs, force=False, results=None, push=True):
        """Add published versions to the index repo. Nothing from the event is trusted but where to look: the package repo is
        fetched at the published commit and its aihub.toml is what goes in. -> True when the index changed.
        force=True re-indexes a version that is already listed. `results` (a list) receives one (name, version, status) per item.
        push=False only stages the change in the local checkout (the hub serves it at once; `_flush_index` pushes it later)."""
        results = results if results is not None else []
        with self.index_lock:
            for attempt in range(3):
                mark = len(results)
                try:
                    return self._update_index(pubs, force, results, push)
                except gitx.GitError as e:                              # push rejected (someone else pushed meanwhile): fetch the new tip and redo the batch
                    if attempt == 2:
                        raise
                    log.info("index push failed (%s); fetching the latest index and retrying", gitx.redact_url(str(e))[:150])
                    time.sleep(random.uniform(0.2, 1.0) * (attempt + 1))
                    del results[mark:]

    def _pubs_failed(self, pubs, msg):
        with self.hold:
            for k in pubs:
                if k in self.pending_pubs:
                    self.pub_tries[k] = self.pub_tries.get(k, 0) + 1
            self.retry_at = time.time() + min(300, 5 * 2 ** max(self.pub_tries.values() or [0]))
        self.status["publish_error"] = msg

    # ----- publishes: served by the hub at once, pushed to git in one batch
    def _stage_pending(self):
        """Apply publish events to the local index checkout (no push). The hub serves the result immediately."""
        with self.hold:
            pubs = dict(self.pending_pubs)
        if not pubs or time.time() < self.retry_at:
            return False
        results = []
        try:
            changed = self.update_index(list(pubs.values()), results=results, push=False)
        except Exception as e:                                          # e.g. index repo unreachable: keep every publish and try again
            self._pubs_failed(pubs, "%s" % gitx.redact_url(str(e))[:200])
            raise
        done = {(n, v) for n, v, st in results if st in ("indexed", "already indexed")}
        bad = [r for r in results if r[2] not in ("indexed", "already indexed")]
        with self.hold:
            for k in pubs:
                if self.pending_pubs.get(k) is not pubs[k]:
                    continue
                key = (naming.normalize(str(k[0])), str(k[1]))
                if key in done or self.pub_tries.get(k, 0) >= 8:        # indexed, or given up after 8 tries (the error stays visible)
                    del self.pending_pubs[k]
                    self.pub_tries.pop(k, None)
                else:
                    self.pub_tries[k] = self.pub_tries.get(k, 0) + 1
            if self.pending_pubs:
                self.retry_at = time.time() + min(300, 5 * 2 ** max(self.pub_tries.values() or [0]))
            if changed and self.index_git():                             # only the legacy git index has something to push later
                self.staged.update({k: m for k, m in pubs.items() if (naming.normalize(str(k[0])), str(k[1])) in done})
                self.staged_first = self.staged_first or time.time()
        self.status["publish_error"] = "; ".join("%s %s: %s" % r for r in bad)[:300] or None
        if bad:
            log.warning("publish not indexed: %s", self.status["publish_error"])
        if changed:
            self.repos.set_setting("sync_index_dirty", "1")
            self.invalidate_served()
            try:
                self._import_local()                                    # database (web UI, rankings) follows the hub's own index now
                self.invalidate_served()                                # the cached index answers are built from the database
            except Exception as e:                                      # dirty stays set: the next index read imports it
                log.warning("sync: importing the staged index failed: %s", e)
        return changed

    def stage_now(self):
        """Index the publishes waiting in `pending_pubs` right now (what the sync thread does on its next wake-up), so the caller of a direct
        publish can answer when the package is really listed. Safe next to the thread: `update_index` runs under `index_lock`."""
        return self._stage_pending()

    def is_pending(self, name, version):
        with self.hold:
            return (naming.normalize(str(name)), str(version)) in self.pending_pubs

    def _flush_index_if_due(self):
        with self.hold:
            if not self.staged:
                return
            due = time.time() >= (self.staged_first or 0) + self.num("index_git_interval")
        if due:
            self._flush_index()

    def _flush_index(self, force=False):
        """Push every staged publish to the index repo as ONE commit: fetch the latest tip, redo the batch, push."""
        with self.hold:
            pubs = dict(self.staged)
        if not pubs:
            return False
        self._heavy()
        # push=True starts from a fresh fetch of the remote tip, which drops the staged files; applying the publishes again on top of it
        # (force: the checkout it replaced already listed them) is what makes ONE commit carry all of them.
        results = []
        changed = self.update_index(list(pubs.values()), force=True, results=results, push=True)
        bad = [r for r in results if r[2] not in ("indexed", "already indexed")]
        if bad:
            log.warning("index push: %s", "; ".join("%s %s: %s" % r for r in bad)[:300])
        with self.hold:
            for k, m in pubs.items():
                if self.staged.get(k) is m:
                    del self.staged[k]
            self.staged_first = time.time() if self.staged else None
        self.status["git_pushes"] += 1
        self.repos.set_setting("sync_index_dirty", "1")
        self.invalidate_served()
        return changed

    # ----- where each version lives (see Repos.version_repos_backfill)
    def stamp_index(self, found):
        """Write where versions really live ({(name, version): {url, branch, subdir}}) into the hub's own index folder, so a later import of
        that entry (it is re-read whenever the package changes) keeps it. The git mirror is left alone: the database is the truth."""
        if not found:
            return 0
        d = self.index_dir or (None if self.index_git() else self.local_index())
        if not d:
            return 0
        path = self.index_source()[2]
        folder = os.path.join(d, path[:-5] if path.endswith(".json") else path)
        if not os.path.isfile(os.path.join(folder, "root.json")):
            return 0
        with self.index_lock:
            n = pkgindex.set_version_repos(folder, found)
        self.invalidate_served()
        return n

    def repair_version_repos(self, extra=()):
        """Give every old version the repo it was published from (publish events; `extra` = events read from the git archive). Safe to
        repeat. Logs how many versions changed. -> that number."""
        fixed = self.repos.version_repos_backfill(extra)
        if fixed:
            log.info("sync: repaired the git location of %d old package version(s) (e.g. %s %s)", len(fixed), fixed[0][0], fixed[0][1])
            try:
                self.stamp_index({(n, v): loc for n, v, loc in fixed})
            except Exception as e:
                log.warning("sync: could not record the repaired locations in the index folder: %s", e)
            self.invalidate_served()
            if self.cache is not None:
                try:
                    self.cache.invalidate("pkg")
                except Exception:
                    pass
        return len(fixed)

    def _repair_from_archive(self):
        """Once: publish events stored before they carried their location are recovered from the archive branch (needs the legacy git
        telemetry sync and a reachable inbox repo; a failure is retried at the next start)."""
        if self.repos.setting("version_repo_archive") == "1" or not (self.telemetry_git() and self.inbox()):
            return
        try:
            url, branch = self.inbox(), self.archive_branch()
            d = self._inbox_dir(url)
            tip = gitx.fetch_tip(d, "origin", branch, "refs/inbox/archive", depth=1, timeout=300)
            found = []
            for line in self._archive_lines(d, tip) if tip else []:
                if '"publish"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get("kind") == "publish" and e.get("git_url") and e.get("version"):
                    found.insert(0, dict(package=naming.normalize(str(e.get("package") or "")), version=str(e["version"]), git_url=str(e["git_url"]),
                                         git_branch=str(e.get("git_branch") or "main"), git_subdir=str(e.get("git_subdir") or ""),
                                         commit=str(e.get("commit") or "")))      # newest first, like publish_locations
            self.repair_version_repos(found)
            self.repos.set_setting("version_repo_archive", "1")
        except Exception as e:
            log.warning("sync: could not read publish locations from the archive: %s", gitx.redact_url(str(e))[:200])

    def unindex(self, name, version=None):
        """Remove a package (or one version) from the hub's index folder, so a later import does not bring it back.
        The git mirror (legacy `git_index_sync`) is not rewritten; the database and what the hub serves are the truth."""
        d = self.index_dir
        if d:
            with self.index_lock:
                folder = os.path.join(d, self.index_source()[2][:-5] if self.index_source()[2].endswith(".json") else self.index_source()[2])
                f = os.path.join(folder, pkgindex.entry_rel(name))
                try:
                    with open(f, encoding="utf-8") as fh:
                        e = json.load(fh)
                except (OSError, ValueError):
                    e = None
                if e is not None:
                    left = [v for v in e.get("versions", []) if version and v.get("version") != version]
                    if left:
                        e["versions"] = left
                        e["latest_version"] = V.latest([v["version"] for v in left])
                        pkgindex.write_entry(folder, e)
                    else:
                        os.remove(f)
                    pkgindex.rebuild(folder)
        self.invalidate_served()

    def invalidate_served(self):
        self.served_version += 1
        if self.cache is not None:
            try:
                self.cache.invalidate("pkg")
            except Exception:
                pass

    def _update_index(self, pubs, force, results, push=True):
        url, branch, path = self.index_source()
        local_only = not push and not self.index_git()                   # legacy git index sync off: the hub's index is a plain folder
        if os.path.isfile(url or "") or not (url or local_only):
            return False
        work = os.path.join(self.s.data_dir, "index-repos")
        os.makedirs(work, exist_ok=True)
        local = gitx.cache_dir(url, work)
        folder_rel = path[:-5] if path.endswith(".json") else path
        if not push and os.path.isfile(os.path.join(local, folder_rel, "root.json")) and os.path.isdir(os.path.join(local, ".git")):
            d = local                                                    # staging needs no network: build on the checkout we already have
        elif local_only:
            d = self.local_index()                                       # no git at all: the data dir (and its backup) holds the index
            os.makedirs(d, exist_ok=True)
        elif gitx.remote_head(url, branch):
            self._heavy()
            d = gitx.fetch(url, branch, base=work)                       # leaves the tree at the remote tip: an earlier unpushed staging is redone from `pubs`
        elif gitx.reachable(url):                                       # empty repo, or the index branch does not exist yet: start from scratch, push creates it
            d = gitx.cache_dir(url, work)
            if not os.path.isdir(os.path.join(d, ".git")):
                gitx.run(["init", "-q"], cwd=d)
                gitx.run(["remote", "add", "origin", url], cwd=d)
            else:
                gitx.run(["remote", "set-url", "origin", url], cwd=d)
            gitx.run(["checkout", "-q", "--orphan", "aihub-index-new"], cwd=d, check=False)
            gitx.run(["rm", "-rq", "--cached", "--ignore-unmatch", "."], cwd=d, check=False)
        else:
            d = None
        if d is None:
            results.append(("", "", "index repository not reachable"))
            raise RuntimeError("index repository not reachable: " + gitx.redact_url(url))
        folder = os.path.join(d, path[:-5] if path.endswith(".json") else path)
        changed = 0
        if not os.path.isfile(os.path.join(folder, "root.json")):      # no index yet: the server creates it
            pkgindex.rebuild(folder)
            changed += 1
        for m in pubs:
            name, ver, gurl = naming.normalize(str(m["package"])), str(m["version"]), str(m["git_url"])
            direct = gurl == direct_mod.URL                             # published straight to the hub: the archive in <data>/direct is the source
            if not direct and not gurl.startswith(SCHEMES):
                results.append((name, ver, "git address not allowed"))
                continue                                                # no file:// or other odd transports from event data
            if not direct and not m.get("bundle") and not gurl.startswith("file://") and not nethost.allowed(gurl, url):   # a bundle means no fetch from that host
                log.warning("publish event for %s names a non-public host; ignored", name)
                results.append((name, ver, "host is not public and not the index host"))
                continue
            try:
                with open(os.path.join(folder, pkgindex.entry_rel(name)), encoding="utf-8") as f:
                    have = {v["version"]: v.get("ref", "") for v in json.load(f).get("versions", [])}
            except (OSError, ValueError):
                have = {}
            ref = str(m.get("commit") or "")
            if ver in have and have[ver] == ref and not force:
                results.append((name, ver, "already indexed"))
                continue
            tmp = None
            try:
                if direct:                                               # nothing from the event is trusted: the stored archive must match its digest
                    tmp = direct_mod.Direct(self.s.data_dir).manifest_dir(name, ver, ref)
                    if not tmp:
                        results.append((name, ver, "no stored archive with that digest"))
                        continue
                else:
                    tmp = self._bundle_dir(m.get("bundle"))              # aihub.toml + README sent by the CLI: no git contact at all
                if tmp:
                    proj = tmp
                else:                                                    # old CLI / no bundle: read the package repo at the published commit
                    src = gitx.fetch(gurl, str(m.get("git_branch") or "main"), ref or None, base=os.path.join(self.s.data_dir, "repos"))
                    proj = os.path.join(src, str(m.get("git_subdir") or "").strip("/"))
                    if not os.path.realpath(proj).startswith(os.path.realpath(src)):
                        results.append((name, ver, "subdir outside the repository"))
                        continue
                from ..core import manifest as M
                with open(os.path.join(proj, "aihub.toml"), encoding="utf-8") as f:
                    pk = M.parse(f.read())["package"]
                if naming.normalize(pk["name"]) != name or str(pk["version"]) != ver:
                    log.warning("publish event for %s %s does not match its aihub.toml; ignored", name, ver)
                    results.append((name, ver, "does not match aihub.toml at that commit (found %s %s)" % (pk["name"], pk["version"])))
                    continue
                pkgindex.add(folder, proj, gitx.redact_url(gurl), str(m.get("git_branch") or "main"), str(m.get("git_subdir") or ""), ref, readme=True)
                changed += 1
                results.append((name, ver, "indexed"))
            except Exception as e:
                log.warning("cannot index %s %s: %s", name, ver, gitx.redact_url(str(e)))
                results.append((name, ver, "cannot read the repository: " + gitx.redact_url(str(e))[:200]))
            finally:
                if tmp:
                    shutil.rmtree(tmp, ignore_errors=True)
        self.index_dir = d
        if not changed:
            return False
        if not os.path.isdir(os.path.join(d, ".git")):
            return True                                                  # plain folder: nothing to commit
        if not gitx.run(["status", "--porcelain", "--", path], cwd=d):
            return False
        if not push:
            return True                                                  # staged in the working tree; the hub serves it, git gets it later
        gitx.run(["add", "-A", "--", path], cwd=d)
        gitx.run(["-c", "user.name=aihub-server", "-c", "user.email=aihub-server@localhost", "commit", "-q", "-m",
                  "index: %d published version(s)" % changed], cwd=d)
        gitx.run(["push", "-q", "origin", "HEAD:refs/heads/" + branch], cwd=d)
        if hasattr(self.repos, "set_setting"):
            self.repos.set_setting("sync_index_rev", gitx.run(["rev-parse", "HEAD"], cwd=d))     # we just wrote it: no need to fetch it back
        return True

    @staticmethod
    def _bundle_dir(b):
        """Publish event `bundle` ({toml, readme, readme_name}, base64) -> temp folder with those files, or None when absent/unusable."""
        if not isinstance(b, dict) or not b.get("toml"):
            return None
        import base64, tempfile
        try:
            toml = base64.b64decode(str(b["toml"]), validate=True)
            readme = base64.b64decode(str(b["readme"]), validate=True) if b.get("readme") else b""
        except ValueError:
            return None
        if len(toml) > 64 * 1024 or len(readme) > pkgindex.README_MAX * 4:
            return None
        d = tempfile.mkdtemp(prefix="aihub-pub-")
        with open(os.path.join(d, "aihub.toml"), "wb") as f:
            f.write(toml)
        if readme:
            name = str(b.get("readme_name") or "README.md")
            with open(os.path.join(d, name if name in pkgindex.README_NAMES else "README.md"), "wb") as f:
                f.write(readme)
        return d

    def _store(self, rows):
        return self.repos.events_insert_ext(rows) if rows else 0

    def local_index(self):
        """Folder holding the hub's own package index when the legacy git index sync is off (inside the data dir, so the backup covers it)."""
        return os.path.join(self.s.data_dir, "index-local")

    def index_source(self):
        return (self.cfg("index_url", "AIHUB_INDEX_URL"), self.cfg("index_branch", "AIHUB_INDEX_BRANCH", "main"),
                self.cfg("index_path", "AIHUB_INDEX_PATH", "index"))

    def read_index(self):
        """(reader, revision) of the package index, or (None, None) when none is configured."""
        url, branch, path = self.index_source()
        if not url:
            return None, None
        if os.path.isfile(url):
            with open(url) as f:
                self._file_reader = pkgindex.Legacy(json.load(f))
            self.index_dir = None
            self.invalidate_served()
            return self._file_reader, os.path.getmtime(url)
        if os.path.isdir(url):
            rd = pkgindex.open_source(url, path)
            self.index_dir = url
            self.invalidate_served()
            return rd, getattr(rd, "root", {}).get("digest")
        if not gitx.remote_head(url, branch):
            if not gitx.reachable(url):
                raise RuntimeError("cannot reach %s" % gitx.redact_url(url))
            return self._bootstrap_index()                              # empty repo or no index branch yet: create it
        self._heavy()
        with self.index_lock:                                           # the checkout is shared with staging; never fetch under a staging writer
            if self.staged or self.pending_pubs:
                d = self.index_dir                                      # unpushed publishes live in it: do not overwrite them with the remote tip
                if d:
                    return pkgindex.open_source(d, path), gitx.run(["rev-parse", "HEAD"], cwd=d, check=False) or None
            try:
                d = gitx.fetch(url, branch, base=os.path.join(self.s.data_dir, "repos"))
            except gitx.GitError as e:
                raise RuntimeError("cannot fetch %s (branch '%s'): %s" % (gitx.redact_url(url), branch, e))
            self.index_dir = d
            self.invalidate_served()
        try:
            return pkgindex.open_source(d, path), gitx.run(["rev-parse", "HEAD"], cwd=d)
        except FileNotFoundError:                                      # branch exists but has no index folder: create it
            return self._bootstrap_index()

    # ----- the index as the hub serves it to CLIs (GET /index/...): from the local checkout, never from a git request
    def served(self):
        """(reader, etag) of the index this hub serves, or (None, '') when none is read yet. Built once per change, then cached in memory."""
        url, branch, path = self.index_source()
        d = self.index_dir
        if not (d or self._file_reader):
            return None, ""
        key = (self.served_version, d)
        hit = self._served
        if hit and hit[0] == key:
            return hit[1], hit[2]
        with self._served_lock:
            hit = self._served
            if hit and hit[0] == key:
                return hit[1], hit[2]
            try:
                rd = self._file_reader if (self._file_reader and not d) else pkgindex.open_source(d, path)
                rows = []
                for r in rd.catalog():                                  # light rows only: a legacy index returns whole entries
                    h = r.get("h") or hashlib.sha256(json.dumps(r, sort_keys=True, default=str).encode()).hexdigest()[:12]
                    rows.append({"name": naming.normalize(r["name"]), "type": r.get("type") or "", "latest_version": r.get("latest_version") or "",
                                 "description": (r.get("description") or "")[:pkgindex.DESC_MAX], "tags": r.get("tags") or [], "h": h})
            except (OSError, ValueError):
                return None, ""
            tag = hashlib.sha256("".join(r["name"] + r["h"] for r in rows).encode()).hexdigest()[:16]
            self._served = (key, rd, '"%s.%d"' % (tag, len(rows)), rows)
            return rd, self._served[2]

    def served_catalog(self):
        """(rows, etag): the light catalog rows of the served index."""
        rd, tag = self.served()
        return (self._served[3], tag) if rd is not None else ([], "")

    def _bootstrap_index(self):
        """Create an empty index in the index repo and push it, then replay publish events so existing packages get listed."""
        log.info("no package index in the index repository; creating one")
        self.update_index([])
        self.repos.set_setting("sync_cursor", "")                       # next pass re-reads the publish events (inserts are idempotent)
        self.wake.set()
        return pkgindex.Legacy({}), None

    def pull_index(self):
        try:
            idx, rev = self.read_index()
        except Exception as e:
            self.repos.set_setting("sync_index_dirty", "1")            # retry next pass
            raise RuntimeError("index: " + gitx.redact_url(str(e)))
        if idx is None:
            return
        if rev is not None and str(rev) == self.repos.setting("sync_index_rev") and self.repos.setting("sync_index_dirty") != "1":
            return                                                     # same commit as last time: nothing to read
        self._import_index(idx, rev)

    def _import_local(self):
        """Load the local index checkout (staged publishes included) into the database. No git: this is what makes a publish show up in the
        web UI at once when the legacy git index sync is off."""
        d = self.index_dir
        if not d:
            return
        with self.index_lock:
            idx = pkgindex.open_source(d, self.index_source()[2])
            self._import_index(idx, None)

    def _import_index(self, idx, rev):
        try:
            seen = json.loads(self.repos.setting("sync_index_hashes") or "{}")
        except ValueError:
            seen = {}
        names, hashes = set(), {}
        for row in idx.catalog():
            name = naming.normalize(row["name"])
            names.add(name)
            h = row.get("h")
            hashes[name] = h or ""
            if h and seen.get(name) == h:
                continue                                               # catalog hash unchanged: this entry file is not even opened
            p = idx.entry(name) if h else row
            if not p:
                continue
            # new packages start with the site default (or the index's choice); admins change it afterwards in the UI
            vis = self.repos.initial_visibility(p.get("visibility"))
            pid = self.repos.package_upsert(name, p.get("type") or "tool", p.get("description") or "", p.get("tags") or [],
                                            p.get("readme") or "", visibility=vis)
            for v in p.get("versions") or ([{"version": p["latest_version"]}] if p.get("latest_version") else []):
                man = v.get("manifest") or {"package": {"name": name, "version": v["version"], "type": p.get("type")},
                                            "requires": v.get("requires") or p.get("requires") or {}}
                vrepo = v.get("repo") if isinstance(v.get("repo"), dict) else {}
                man["repo"] = dict(p.get("repo") or {}, **{k: x for k, x in vrepo.items() if k != "ref"}, ref=v.get("ref") or vrepo.get("ref"))   # the version's own location wins
                meta = man.setdefault("package", {})
                for k in ("authors", "license"):                       # entry-level (latest) values fill what the version lacks
                    if p.get(k) and not meta.get(k):
                        meta[k] = p[k]
                sha, size = "", 0
                if direct_mod.is_direct(man.get("repo")):               # an archive version: its digest and size are known exactly
                    sha = direct_mod.Direct(self.s.data_dir).sha(p["name"], str(v["version"]))
                    try:
                        size = os.path.getsize(direct_mod.Direct(self.s.data_dir).path(p["name"], v["version"])) if sha else 0
                    except (OSError, direct_mod.DirectError):
                        size = 0
                self.repos.version_sync(pid, v["version"], man, sha=sha, size=size)
        self.repos.package_hide_missing(names)
        self.repos.set_setting("sync_index_hashes", json.dumps(hashes, separators=(",", ":")))
        if rev is not None:
            self.repos.set_setting("sync_index_rev", str(rev))
        self.repos.set_setting("sync_index_dirty", "0")
        self.status["index_packages"] = len(names)
