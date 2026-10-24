"""Scheduled database backup into git: the same repo as the package index, under backups/.

Layout (all in the index repo, on the index branch):
  backups/index.json                    what is there: files, row counts, sha256, sizes, schema version, redaction rules
  backups/db/<table>.sqlite.gz          one small SQLite file per table
  backups/db/events-000001.sqlite.gz    big id-keyed tables are split by id range so no file grows without bound
  backups/direct/<name>/<version>.tar.gz  the archives published with `aihub dev publish -i` (they live only in <data>/direct, not in the database);
                                        listed in index.json under "archives" with their sha256. A version never changes, so a file is copied once.
Credentials never leave in clear text: see scrub(). Unchanged files are not rewritten, so git history only grows by what changed.
"""
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import random
import threading
import time

from ..cli import gitx
from ..core import redact, textsafe
from . import db as dbmod
from .direct import Direct
from .sync import LIMITS, next_delay, parse_schedule

log = logging.getLogger("aihub")
FORMAT = 1
DEFAULT_SCHEDULE = "0 3 * * *"
WAIT_RUNNING = 60                         # "Back up now" waits this long for a backup that is already running
RETRY_PAUSE = (2.0, 5.0)                  # pause before the one retry after a network / login error
RACE = re.compile(r"non-fast-forward|fetch first|\[rejected\]|stale info", re.I)    # someone else pushed in between (not "[remote rejected]")
DEFAULT_CHUNK = 20000                     # rows per file for events / audit_log
CHUNKED = ("events", "audit_log")         # tables with an integer id that only grows
SKIP = {"sqlite_sequence"}                # never dumped; FTS shadow tables are not in TABLES, so they are not either
HASHED = re.compile(r"^(pbkdf2\$[0-9a-f]+\$[0-9a-f]+|sha256:[0-9a-f]{64}|[0-9a-f]{64})$")
SECRET_SETTING = redact.SECRET_KEY        # app_settings keys matching this are stored as a hash only
SAFE_SETTINGS = {"sync_cursor", "sync_index_dirty", "sync_names", "sync_interval", "perms_seeded"}


def tables():
    return re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)\(", dbmod.TABLES)


KEY_TABLES = ("users", "groups", "packages", "versions", "reviews", "events")     # what recovery compares and counts as "data"
ACTIVITY = (("events", "ts"), ("audit_log", "ts"), ("packages", "updated"), ("versions", "created"),
            ("users", "created"), ("reviews", "created"))


def table_cols(db, c, table):
    """Column names in table order (db._columns returns a set)."""
    if db.engine == "postgres":
        return [r["column_name"] for r in c.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name=? ORDER BY ordinal_position", (table,))]
    return [r[1] for r in c.execute("PRAGMA table_info(%s)" % table)]


def summary(c):
    """Row counts per table and the time of the newest thing in the database: what a human needs to pick local vs backup."""
    rows, latest = {}, 0.0
    for t in tables():
        if t not in SKIP:
            rows[t] = c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
    for t, col in ACTIVITY:
        r = c.execute("SELECT MAX(%s) FROM %s" % (col, t)).fetchone()
        if r and r[0]:
            latest = max(latest, float(r[0]))
    return {"rows": rows, "latest": latest}


def _h(v):
    return "sha256:" + hashlib.sha256(str(v).encode("utf-8", "replace")).hexdigest()


def scrub(table, rec):
    """Return rec with every credential replaced by a one-way hash (or redacted text). Values already hashed are kept,
    because they are the stored form of a password/token and cannot be reversed anyway."""
    rec = dict(rec)
    if table == "users" and rec.get("password_hash") and not HASHED.match(rec["password_hash"]):
        rec["password_hash"] = _h(rec["password_hash"])
    elif table == "tokens" and rec.get("hash") and not HASHED.match(rec["hash"]):
        rec["hash"] = _h(rec["hash"])
    elif table == "app_settings":
        k, v = rec.get("key") or "", rec.get("value")
        if v and k not in SAFE_SETTINGS and (SECRET_SETTING.search(k) or redact.SECRET_VAL.search(str(v))) and not k.endswith("_mime"):
            rec["value"] = _h(v)
    elif table == "events":
        for c in ("detail", "cwd", "host", "local_user"):
            if rec.get(c):
                rec[c] = redact.text(rec[c], 2200)
    elif table == "audit_log" and rec.get("detail"):
        rec["detail"] = redact.text(rec["detail"], 2200)
    return rec


def _gz_file(src, dst):
    """gzip with a fixed mtime so identical content gives identical bytes (no pointless git churn)."""
    with open(src, "rb") as f, open(dst, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as g:
        shutil.copyfileobj(f, g)


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def write_sqlite(path, table, cols, rows):
    """A standalone SQLite file holding one table (untyped columns: it is an archive, not the live schema)."""
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    try:
        con.execute('CREATE TABLE "%s"(%s)' % (table, ",".join('"%s"' % c for c in cols)))
        con.executemany('INSERT INTO "%s" VALUES(%s)' % (table, ",".join("?" * len(cols))),
                        ([bytes(r[c]) if isinstance(r[c], memoryview) else textsafe.clean(r[c]) for c in cols] for r in rows))
        con.commit()
        con.execute("VACUUM")
    finally:
        con.close()


class Backup:
    def __init__(self, repos, settings, sync):
        self.repos, self.s, self.sync = repos, settings, sync
        self.stop, self.wake, self.lock = threading.Event(), threading.Event(), threading.Lock()
        self.t = None
        self.status = {"last_run": None, "last_ok": None, "last_error": None, "last_commit": None, "files": 0, "rows": 0,
                       "archives": 0, "archives_skipped": 0}
        self.archive_info = {"archives": 0, "archives_skipped": 0}

    # ----- config (admin settings; environment wins for the on/off switch)
    def enabled(self):
        v = self.s.env_get("AIHUB_BACKUP_ENABLED") or self.repos.setting("backup_enabled", "") or "0"
        return v.lower() in ("1", "true", "yes", "on")

    def schedule_text(self):
        return self.repos.setting("backup_schedule", "") or DEFAULT_SCHEDULE

    def chunk(self):
        try:
            return max(1000, int(self.repos.setting("backup_events_chunk", "") or DEFAULT_CHUNK))
        except ValueError:
            return DEFAULT_CHUNK

    def delay(self):
        try:
            return next_delay(parse_schedule(self.schedule_text()))
        except ValueError:
            return next_delay(parse_schedule(DEFAULT_SCHEDULE))

    def jittered(self):
        """The wait before the next scheduled run, moved a little at random so backups never form a fixed pattern on the git host.
        Up to `backup_jitter` percent of the wait (admin, default 10), at most `backup_jitter_max` seconds (default 600), either way,
        and never so early that the run lands before the moment the schedule names by more than that."""
        base = self.delay()
        pct, cap = self.sync.num("backup_jitter") / 100.0, self.sync.num("backup_jitter_max")
        span = min(base * pct, cap)
        return max(5.0, base + random.uniform(-span, span))

    # ----- cron loop
    def start(self):
        self.t = threading.Thread(target=self._loop, name="aihub-backup", daemon=True)
        self.t.start()

    def close(self):
        self.stop.set()
        self.wake.set()

    def on_start(self):
        return self.s.env_get("AIHUB_BACKUP_ON_START", "1").lower() in ("1", "true", "yes", "on")

    def _try(self):
        try:
            self.run_once(scheduled=True)
        except Exception as e:
            log.warning("backup failed: %s", gitx.redact_url(str(e)))
        finally:
            self.repos.db.release()

    def _loop(self):
        # One backup right after the first sync pass: events pulled from the inbox while the server was down reach git
        # now, not at the next cron slot. This disk can fail later; git then still has them.
        if self.on_start() and self.sync.first_pass.wait(300) and not self.stop.is_set() and self.enabled():
            self._try()
        while not self.stop.is_set():
            if self.wake.wait(self.jittered()):          # woken early: settings changed or shutdown, not a scheduled run
                self.wake.clear()
                continue
            if self.enabled():
                self._try()

    # ----- one backup
    def run_once(self, scheduled=False):
        """scheduled=True (the cron loop) waits for the sync job's git gap; "Back up now" (False) runs at once."""
        if not self.lock.acquire(blocking=False):
            if scheduled:
                raise RuntimeError("a backup is already running")
            # "Back up now" while the on-start / cron backup still runs: wait for it and answer with its result instead of an error
            if not self.lock.acquire(timeout=WAIT_RUNNING):
                raise RuntimeError("a backup is already running")
            try:
                if self.status.get("last_error"):
                    raise RuntimeError(self.status["last_error"])
                return {k: self.status.get(k) for k in ("files", "rows", "last_commit")}
            finally:
                self.lock.release()
        self.status["last_run"] = time.time()
        try:
            url, branch, _ = self.sync.index_source()
            if not url or os.path.isfile(url):
                raise RuntimeError("backup needs a git repository: set the index URL (Admin > Sync)")
            self.sync.forget()
            self.status["waiting"] = scheduled
            with self.sync.gate.job("backup", gap=self.sync.num("git_job_gap"), stop=self.stop, wait_gap=True, force=not scheduled):
                self.status["waiting"] = False
                out = self._backup(url, branch)
            self.status.update(last_ok=time.time(), last_error=None, **out)
            return out
        except InterruptedError:
            return {}
        except Exception as e:
            self.status["last_error"] = gitx.redact_url(str(e))[:300]
            raise
        finally:
            self.status["waiting"] = False
            self.lock.release()

    def _backup(self, url, branch):
        work = os.path.join(self.s.data_dir, "backup-repos")
        os.makedirs(work, exist_ok=True)
        slept = False
        for attempt in (1, 2, 3):
            try:
                return self._attempt(url, branch, work)
            except gitx.GitError as e:
                if attempt == 3:
                    raise
                if RACE.search(str(e)):                # someone pushed in between: refetch and redo at once
                    continue
                if slept:                              # a login or network error that came back: report the real cause
                    raise
                slept = True                           # one more try after a short pause (a hiccup, an expired ticket)
                log.warning("backup git step failed (%s); retrying once", gitx.redact_url(str(e))[:200])
                if self.stop.wait(random.uniform(*RETRY_PAUSE)):
                    raise
        raise RuntimeError("unreachable")

    def _attempt(self, url, branch, work):
        d = self._checkout(url, branch, work)
        self._guard(d)
        files, rows = self.export(d)
        gitx.run(["add", "-A", "backups"], cwd=d)
        if not gitx.run(["status", "--porcelain", "backups"], cwd=d):
            return dict(self.archive_info, files=files, rows=rows, last_commit=self.status["last_commit"])
        msg = "backup: %d files, %d rows (%s)" % (files, rows, time.strftime("%Y-%m-%d %H:%M", time.gmtime()))
        gitx.run(["-c", "user.name=aihub-backup", "-c", "user.email=aihub-backup@localhost", "commit", "-q", "-m", msg], cwd=d)
        gitx.run(["push", "-q", "origin", "HEAD:refs/heads/" + branch], cwd=d)
        return dict(self.archive_info, files=files, rows=rows, last_commit=gitx.run(["rev-parse", "--short", "HEAD"], cwd=d))

    def _guard(self, d):
        """An empty database (lost data dir, wrong DATABASE_URL) must never overwrite a backup that holds data: that would
        turn the one good copy into an empty one. Recover first (start the server with --recover)."""
        try:
            with open(os.path.join(d, "backups", "index.json"), encoding="utf-8") as f:
                old = json.load(f)
        except (OSError, ValueError):
            return
        have = sum(e.get("rows", 0) for e in old.get("files", []) if e.get("table") in KEY_TABLES)
        mine = summary(self.repos.db.conn())["rows"]
        if have and not sum(mine.get(t, 0) for t in KEY_TABLES):
            raise RuntimeError("refusing to back up an empty database over a backup with %d rows: restart with --recover remote" % have)

    @staticmethod
    def _checkout(url, branch, work):
        """Latest tip of the branch. Only a remote that answers `ls-remote` and has no such branch (a brand-new empty repo) gets an orphan
        branch on first push. A remote that does NOT answer (login refused, timeout, network) is an error with git's own words: it used to
        look like "empty", so the backup built an orphan commit that the push then rejected (the "click Back up now twice" bug)."""
        r = gitx._ls(url)
        if r is None:
            raise gitx.GitError(gitx.explain("git ls-remote %s timed out" % gitx.redact_url(url), url, "net"))
        if r.returncode != 0:
            raise gitx.GitError(gitx.explain(gitx.redact_url((r.stderr or r.stdout or "").strip()) or "git ls-remote failed (exit %d)" % r.returncode, url))
        ref = "refs/heads/" + branch
        if any(ln.split("\t")[-1].strip() == ref for ln in (r.stdout or "").splitlines()):
            return gitx.fetch(url, branch, base=work)
        d = gitx.cache_dir(url, work)
        if not os.path.isdir(os.path.join(d, ".git")):
            gitx.run(["init", "-q"], cwd=d)
        gitx.run(["remote", "remove", "origin"], cwd=d, check=False)
        gitx.run(["remote", "add", "origin", url], cwd=d)
        gitx.run(["checkout", "-q", "--orphan", "bk-%d" % time.time()], cwd=d, check=False)
        gitx.run(["rm", "-rq", "--cached", "."], cwd=d, check=False)
        return d

    def _direct_limit(self):
        """Bytes: the biggest archive copied into the git backup (a git host rejects files over ~100 MB, which would fail the whole backup)."""
        default = LIMITS["backup_direct_max_mb"][0]
        try:
            mb = int(self.sync.num("backup_direct_max_mb")) if self.sync is not None else default
        except (TypeError, ValueError, AttributeError):
            mb = default
        return max(1, mb) * 1024 * 1024

    def _export_direct(self, root, prev):
        """Copy <data>/direct into backups/direct and drop what is gone (unpublished). `prev` = {path: entry} of the last index.json.
        -> (entries for index.json, skipped [{package, version, size}]). An archive that is already there with the same digest is not touched."""
        direct, base, limit = Direct(self.s.data_dir), os.path.join(root, "direct"), self._direct_limit()
        entries, skipped = [], []
        names = sorted(n for n in os.listdir(direct.base) if not n.startswith(".")) if os.path.isdir(direct.base) else []
        for name in names:
            folder = os.path.join(direct.base, name)
            for f in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
                if not f.endswith(".tar.gz"):
                    continue
                ver, src = f[:-len(".tar.gz")], os.path.join(folder, f)
                sha = direct.sha(name, ver) or _sha(src)             # no sidecar (copied in by hand): hash it
                size = os.path.getsize(src)
                if size > limit:
                    skipped.append({"package": name, "version": ver, "size": size})
                    continue
                rel = "backups/direct/%s/%s" % (name, f)
                dst = os.path.join(base, name, f)
                have = prev.get(rel)
                if not (have and have.get("sha256") == sha and os.path.isfile(dst) and os.path.getsize(dst) == size):
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    tmp = dst + ".tmp"
                    shutil.copyfile(src, tmp)
                    os.replace(tmp, dst)
                entries.append({"path": rel, "package": name, "version": ver, "sha256": sha, "size": size})
        keep = {e["path"] for e in entries}
        for d, _, files in os.walk(base, topdown=False):
            for f in files:
                if os.path.relpath(os.path.join(d, f), os.path.dirname(root)).replace(os.sep, "/") not in keep:
                    os.remove(os.path.join(d, f))
            if d != base and not os.listdir(d):
                os.rmdir(d)
        for k in skipped:
            log.warning("backup: %s %s (%d MB) is over backup_direct_max_mb and is NOT in the git backup", k["package"], k["version"], k["size"] >> 20)
        return entries, skipped

    def export(self, repo_dir, archives=True):
        """Write backups/ inside the checkout. Returns (file count, row count). archives=False leaves out <data>/direct (a local safety copy)."""
        root = os.path.join(repo_dir, "backups")
        dbdir = os.path.join(root, "db")
        os.makedirs(dbdir, exist_ok=True)
        old = {}
        try:
            with open(os.path.join(root, "index.json"), encoding="utf-8") as f:
                old = {e["path"]: e for e in json.load(f).get("files", [])}
        except (OSError, ValueError, KeyError, TypeError):
            pass
        n, c, entries, total = self.chunk(), self.repos.db.conn(), [], 0
        with tempfile.TemporaryDirectory(dir=self.s.data_dir) as tmp:
            for t in tables():
                if t in SKIP:
                    continue
                cols = self._cols(c, t)
                if t in CHUNKED:
                    ranges = self._ranges(c, t, n)
                else:
                    ranges = [(None, None)]
                for k, (lo, hi) in enumerate(ranges):
                    name = "%s-%06d.sqlite.gz" % (t, lo // n + 1) if lo is not None else "%s.sqlite.gz" % t
                    rel = "backups/db/" + name
                    prev = old.get(rel)
                    closed = lo is not None and k < len(ranges) - 1       # a later id range exists: this one only ever shrinks by deletes
                    if closed and prev and prev.get("complete") and prev.get("id_min") == lo and prev.get("id_max") == hi - 1 \
                            and os.path.exists(os.path.join(dbdir, name)):
                        entries.append(prev)                       # closed chunk: reuse, ids below the cursor are append-only
                        total += prev["rows"]
                        continue
                    rows = self._read(c, t, cols, lo, hi)
                    if not rows and lo is not None:
                        continue
                    for r in rows:
                        r.update(scrub(t, r))
                    tmpdb = os.path.join(tmp, name[:-3])
                    write_sqlite(tmpdb, t, cols, rows)
                    dst = os.path.join(dbdir, name)
                    tmpgz = os.path.join(tmp, name)
                    _gz_file(tmpdb, tmpgz)
                    sha = _sha(tmpgz)
                    if not (prev and prev.get("sha256") == sha and os.path.exists(dst)):
                        shutil.move(tmpgz, dst)
                    ent = {"path": rel, "table": t, "rows": len(rows), "sha256": sha, "size": os.path.getsize(dst),
                           "complete": closed}
                    if lo is not None:
                        ent.update(id_min=lo, id_max=hi - 1)
                    entries.append(ent)
                    total += len(rows)
                    os.remove(tmpdb)
        keep = {os.path.basename(e["path"]) for e in entries}
        for f in os.listdir(dbdir):                                # tables or chunks that no longer exist
            if f not in keep:
                os.remove(os.path.join(dbdir, f))
        try:
            with open(os.path.join(root, "index.json"), encoding="utf-8") as f:
                prev_idx = json.load(f)
        except (OSError, ValueError):
            prev_idx = {}
        arch, skipped = ([], [])
        if archives:
            arch, skipped = self._export_direct(root, {e.get("path"): e for e in prev_idx.get("archives") or [] if isinstance(e, dict)})
        self.archive_info = {"archives": len(arch), "archives_skipped": len(skipped)}
        same = prev_idx.get("files") == entries and prev_idx.get("events_chunk") == n and (prev_idx.get("archives") or []) == arch \
            and (prev_idx.get("archives_skipped") or []) == skipped
        # the timestamp only moves when something changed, so an idle database makes no commit
        created = prev_idx["created"] if same and prev_idx.get("created") else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        index = {"format": FORMAT, "app": "aihub", "engine": self.repos.db.engine, "created": created,
                 "events_chunk": n, "stats": summary(c), "tables": sorted({e["table"] for e in entries}), "files": entries,
                 **({"archives": arch} if arch else {}), **({"archives_skipped": skipped} if skipped else {}),
                 "redaction": {"users.password_hash": "kept only if already a pbkdf2/sha256 hash, else sha256",
                               "tokens.hash": "already sha256; plain values are hashed",
                               "app_settings": "values of secret-looking keys or secret-looking values are sha256 (not restorable: re-enter them)",
                               "events,audit_log": "free text passed through the credential redactor"}}
        tmpi = os.path.join(root, "index.json.tmp")
        with open(tmpi, "w", encoding="utf-8") as f:
            json.dump(textsafe.deep(index), f, indent=1, sort_keys=True)
            f.write("\n")
        os.replace(tmpi, os.path.join(root, "index.json"))
        return len(entries) + len(arch), total

    def _cols(self, c, table):
        return table_cols(self.repos.db, c, table)

    def _ranges(self, c, table, n):
        r = c.execute("SELECT MAX(id) FROM %s" % table).fetchone()
        top = r[0] if r and r[0] is not None else -1
        return [(k * n, (k + 1) * n) for k in range(top // n + 1)] if top >= 0 else []

    def _read(self, c, table, cols, lo, hi):
        if lo is None:
            cur = c.execute("SELECT * FROM %s ORDER BY 1" % table)
        else:
            cur = c.execute("SELECT * FROM %s WHERE id>=? AND id<? ORDER BY id" % table, (lo, hi))
        return [{k: r[k] for k in cols} for r in cur.fetchall()]
