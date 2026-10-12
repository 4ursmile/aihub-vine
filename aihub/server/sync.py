"""Pulls the world into SQLite: usage events from the git inbox, package metadata from the git index.

- Events: CLIs add files inbox/<client>-<unix>-<rand>.jsonl to the one `telemetry-inbox` branch. The server fetches them (only when
  `ls-remote` shows something new), stores the rows (events.ext_id is the event id and unique, so restarts and several servers never
  duplicate rows), copies the raw batch to the `telemetry-archive` branch, and only then removes the files. A failure at any step
  leaves them for the next pass.
- Index: the server is the only writer. A new `aihub.publish` event (public data sent by the CLI) makes the server fetch that
  package repo at the published commit, read its aihub.toml, update the index repo and push; then the index is re-read.
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

from ..cli import gitx
from ..core import naming, pkgindex, redact
from . import nethost

log = logging.getLogger("aihub")
KINDS = {"use", "install", "uninstall", "update", "error", "publish"}
ARCHIVE = "telemetry-archive"
INBOX = "telemetry-inbox"
MAX_BATCH_BYTES = 5 * 1024 * 1024       # a ref bigger than this is dropped unread (a client only ever sends ~BATCH events)
MAX_REFS = 500                          # refs ingested per pass; the rest wait for the next one


def _field(spec, lo, hi):
    out = set()
    for part in spec.split(","):
        rng, _, step = part.partition("/")
        step = int(step) if step else 1
        if step < 1:
            raise ValueError("bad step")
        if rng in ("*", ""):
            a, b = lo, hi
        elif "-" in rng:
            a, b = (int(x) for x in rng.split("-", 1))
        else:
            a = int(rng)
            b = hi if step > 1 else a
        if a < lo or b > hi or a > b:
            raise ValueError("out of range")
        out.update(range(a, b + 1, step))
    return out


def parse_schedule(text):
    """'60' (seconds, 10-86400) or a 5-field cron 'min hour dom month dow' (*, lists, ranges, steps). -> ('every', secs) | ('cron', fields)."""
    t = str(text).strip()
    if t.isdigit():
        n = int(t)
        if not 10 <= n <= 86400:
            raise ValueError("seconds must be between 10 and 86400")
        return ("every", n)
    f = t.split()
    if len(f) != 5:
        raise ValueError("use seconds (e.g. 60) or a 5-field cron expression (e.g. */5 * * * *)")
    try:
        mi, ho, dom, mo, dow = _field(f[0], 0, 59), _field(f[1], 0, 23), _field(f[2], 1, 31), _field(f[3], 1, 12), _field(f[4], 0, 7)
    except ValueError as e:
        raise ValueError("invalid cron expression: %s" % e)
    return ("cron", (mi, ho, dom, mo, {d % 7 for d in dow}, f[2] != "*", f[4] != "*"))


def next_delay(schedule, now=None):
    """Seconds until the next run."""
    kind, v = schedule
    now = now or time.time()
    if kind == "every":
        return v
    mi, ho, dom, mo, dow, dom_set, dow_set = v
    t = int(now // 60 + 1) * 60
    for _ in range(366 * 24 * 60):
        lt = time.localtime(t)
        d_ok, w_ok = lt.tm_mday in dom, (lt.tm_wday + 1) % 7 in dow
        day_ok = (d_ok or w_ok) if (dom_set and dow_set) else (d_ok and w_ok)      # standard cron: both restricted = OR
        if lt.tm_min in mi and lt.tm_hour in ho and lt.tm_mon in mo and day_ok:
            return max(1, t - now)
        t += 60
    return 3600


SCHEMES = ("https://", "http://", "ssh://", "git@")      # transports a publish event may name (tests add file://)
OS_NAMES = {"darwin": "macOS", "linux": "Linux", "win32": "Windows", "cygwin": "Windows", "freebsd": "FreeBSD"}


def _os(v):
    """sys.platform from the CLI -> a display name. Untrusted input: unknown values are kept short and scrubbed."""
    v = str(v or "").strip().lower()
    if not v:
        return None
    return OS_NAMES.get(v) or redact.text(v, 24)


def to_row(e):
    """One event from a CLI batch -> an events row (None if it is not one of ours). Everything in it is untrusted input."""
    if not isinstance(e, dict):
        return None
    kind = e.get("kind")
    if kind not in KINDS or not e.get("package") or not e.get("id"):
        return None
    uid = str(e.get("user") or "")
    anon = uid.startswith("~") or not uid
    try:
        ts = min(float(e.get("ts")), time.time() + 86400)       # a client clock in the far future must not pin itself to "now" forever
    except (TypeError, ValueError):
        ts = time.time()
    return {"ts": ts, "kind": kind, "package": naming.normalize(str(e["package"])),
            "version": redact.text(e.get("version"), 64), "client_id": redact.text(e.get("client_id"), 64),
            "username": None if anon else uid[:64], "component": str(e["component"])[:120] if e.get("component") else None,
            "duration": None,
            "source": e.get("source") if e.get("source") in ("claude", "opencode", "codex", "cli") else None,
            "local_user": redact.text(e.get("local_user"), 64),
            "host": redact.text(e.get("host"), 128), "os": _os(e.get("os")), "detail": redact.text(e.get("detail"), 2200),
            "cwd": redact.text(e.get("cwd"), 200), "ip": None, "ext_id": str(e["id"])[:64]}


class Sync:
    def __init__(self, repos, settings, cache=None):
        self.repos, self.s, self.cache = repos, settings, cache          # cache: dropped after new events so counts and the feed update now
        self.stop, self.wake = threading.Event(), threading.Event()
        self.t = None
        self.first_pass = threading.Event()                 # set once the first pass after start has finished (ok or not)
        self._lookback_checked = False
        self.index_lock = threading.Lock()                  # one index writer at a time (event sync and manual forcing share a checkout)
        self.status = {"last_run": None, "last_error": None, "events_error": None, "index_error": None, "archive_error": None, "events_total": 0, "index_packages": 0,
                       "archive_last": None, "archive_batches": 0, "archive_files": 0, "archive_commit": None, "inbox_pending": 0, "last_events": 0}

    # ----- config: env first, then admin settings
    def cfg(self, key, env, default=""):
        return self.s.env_get(env) or self.repos.setting(key, "") or default

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
            return next_delay(parse_schedule(self.repos.setting("sync_interval", "") or "60"))
        except ValueError:
            return 60

    def start(self):
        gitx.BATCH = True                                   # server: git must never wait for a prompt nobody can answer
        url, branch, path = self.index_source()
        log.info("sync: usage inbox %s (branch %s, archive %s); package index %s (branch %s, folder %s)",
                 gitx.redact_url(self.inbox()) or "-", self.inbox_branch(), self.archive_branch(), gitx.redact_url(url) or "-", branch, path)
        self.t = threading.Thread(target=self._loop, name="aihub-sync", daemon=True)
        self.t.start()

    def close(self):
        self.stop.set()
        self.wake.set()

    def _loop(self):
        while not self.stop.is_set():
            try:
                self.run_once()
            except Exception as e:                       # never die; report on the admin page
                self.status["last_error"] = str(e)[:300]
                log.warning("sync failed: %s", e)
            finally:
                self.repos.db.release()
                self.first_pass.set()
            self.wake.wait(self.delay())
            self.wake.clear()

    # ----- one pass
    def run_once(self):
        """One pass. The two halves are independent: if the usage inbox cannot be read (repo down, no access, bad credentials), the package
        index is still synced, and the other way round. Every failure is kept in `status.last_error` (shown in Admin > Sync) until a pass
        succeeds completely; a step that fails never reports success and never lets the cursor claim the pass completed."""
        first = not self.repos.setting("sync_cursor")
        errors, new_publish = [], False
        self.status["events_error"] = self.status["index_error"] = None
        if self.inbox() and not os.path.isfile(self.inbox()):
            try:
                if self._needs_replay():
                    self.replay_archive()
                new_publish = self.pull_events()
            except Exception as e:
                msg = "usage sync: " + gitx.redact_url(str(e))[:240]
                self.status["events_error"] = msg
                errors.append(msg)
                log.warning("sync: %s", msg)
            if self.status.get("archive_error"):                        # pull_events handled it and carried on; it must still be visible
                errors.append(self.status["archive_error"])
        try:
            if first or new_publish or self.repos.setting("sync_index_dirty") == "1" or self._index_moved():
                self.pull_index()
        except Exception as e:
            msg = gitx.redact_url(str(e))[:250]
            self.status["index_error"] = msg
            errors.append(msg)
            log.warning("sync: %s", msg)
        self.status["last_run"] = time.time()
        self.status["last_error"] = " | ".join(errors) if errors else None
        if errors:
            raise RuntimeError(self.status["last_error"])               # the loop and "Sync now" report it; the cursor is NOT advanced
        self.repos.set_setting("sync_cursor", time.time())             # "an events pass completed"; empty = never (first run, forced re-sync)

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
        tip = gitx.fetch_tip(d, "origin", branch, "refs/inbox/archive", depth=1, timeout=300)
        if not tip:
            return 0                                                    # nothing archived yet (or archiving was off)
        files = []
        for line in gitx.run(["ls-tree", "-r", tip, "--", "archive/"], cwd=d, check=False).splitlines():
            meta, _, path = line.partition("\t")
            if path.endswith(".jsonl"):
                files.append((path, meta.split()[2]))
        files.sort()
        total, rows = 0, []
        for i, (f, blob) in enumerate(files):
            if i % 200 == 0:
                texts = gitx.blobs(d, [b for _, b in files[i:i + 200]])
            for line in texts.get(blob, "").splitlines():
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
            gitx.run(["fetch", "-q", "--no-tags", "--depth", "1", "origin"] + specs, cwd=d, timeout=300)
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
        return sorted(out)[:MAX_REFS]

    def pull_events(self):
        """Fetch every pending batch, store its rows, archive it, and only then remove it from the inbox. -> True when a publish event arrived."""
        url = self.inbox()
        self.status["archive_error"] = None
        d = self._inbox_dir(url)
        found = self._collect(d)
        self.status["inbox_pending"] = len(found)
        if not found:
            log.debug("sync: no batches waiting on %s", self.inbox_branch())
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
            done.append((kind, where))
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
                changed = bool(self.update_index(list(pubs.values())))
            except Exception as e:                                      # events are stored; the batches stay so the next pass retries the index
                log.warning("index update failed: %s", gitx.redact_url(str(e)))
                raise RuntimeError("index update: " + gitx.redact_url(str(e))[:240])      # run_once records it: a quiet 'return' showed OK while broken
        if done and self._archive(d, batches):                          # raw copy is safe in git: the inbox entries can go
            self._remove(d, done)
            gitx.run(["gc", "-q", "--auto", "--prune=now"], cwd=d, check=False, timeout=120)     # repacks only once objects pile up
        # a retried batch (its rows were stored by a pass whose index update failed) inserts nothing, but if the index changed now it
        # must still be re-read
        return (publish and n > 0) or changed

    def _remove(self, d, done):
        """Delete exactly the batches that were archived. Per-batch branches and hidden refs are deleted on the remote; for the single inbox
        branch ONE commit removes just those files, so a batch a CLI pushed after our fetch is never touched."""
        old = []
        for kind, where in done:
            if kind == "heads":
                old.append("refs/heads/telemetry/" + where[len("refs/inbox/heads/"):])
            elif kind == "legacy":
                old.append("refs/telemetry/" + where[len("refs/inbox/legacy/"):])
        for i in range(0, len(old), 50):
            gitx.run(["push", "-q", "origin", "--delete"] + old[i:i + 50], cwd=d, check=False, timeout=60)
        files = [where for kind, where in done if kind == "inbox"]
        if files:
            self._trim_inbox(d, files)
        local = [where for _, where in done if where.startswith("refs/")]
        if local:
            gitx.run(["update-ref", "--stdin"], cwd=d, input="".join("delete %s\n" % r for r in local), check=False)

    def _trim_inbox(self, d, files):
        """Commit 'remove these files' on top of the CURRENT inbox tip and push. CLIs push to the same branch, so a rejected push means a
        newer tip exists: refetch it and redo the removal (the files still exist there, and new files are left alone)."""
        branch, gone, idx = self.inbox_branch(), set(files), os.path.join(os.path.abspath(d), "index.trim")
        env = {"GIT_INDEX_FILE": idx}
        for attempt in range(6):
            if attempt:
                time.sleep(0.2 * attempt)
                tip = gitx.fetch_tip(d, "origin", branch, "refs/inbox/tip", depth=1)
            else:
                tip = gitx.run(["rev-parse", "-q", "--verify", "refs/inbox/tip"], cwd=d, check=False)
            if not tip:
                return
            present = sorted(gone & set(gitx.run(["ls-tree", "-r", "--name-only", tip, "--", "inbox/"], cwd=d).splitlines()))
            if not present:
                return
            gitx.run(["read-tree", tip], cwd=d, extra_env=env)
            for i in range(0, len(present), 200):
                gitx.run(["update-index", "--force-remove", "--"] + present[i:i + 200], cwd=d, extra_env=env)
            tree = gitx.run(["write-tree"], cwd=d, extra_env=env)
            commit = gitx.run(["-c", "user.name=aihub-server", "-c", "user.email=aihub-server@localhost", "commit-tree", tree, "-p", tip,
                               "-m", "telemetry: archived %d batch(es)" % len(present)], cwd=d)
            try:
                gitx.run(["push", "-q", "origin", "%s:refs/heads/%s" % (commit, branch)], cwd=d, timeout=60)
                gitx.run(["update-ref", "refs/inbox/tip", commit], cwd=d, check=False)     # next pass sees the remote unchanged: no fetch
                return
            except gitx.GitError:
                continue
        self.status["archive_error"] = "could not clear ingested batches from the inbox (kept, re-read next pass and deduped by id)"
        log.warning("could not trim the telemetry inbox (they stay and are re-ingested, deduped by id)")

    def _archive(self, d, batches):
        """Write each batch as archive/<day>/<client>-<batch>.jsonl on the archive branch and push it. The path is derived from the
        batch name, so doing it twice writes the same file. Built with plumbing on a depth-1 fetch of the archive tip: no checkout, so
        the cost does not grow with the size or history of the archive. -> True when the archive holds the batches (or archiving is off)."""
        if self.repos.setting("telemetry_archive", "1") == "0":
            return True
        batches = [(key, good) for key, good in batches if good]
        if not batches:
            return True
        d = os.path.abspath(d)                                          # git runs with cwd=d: every path handed to it must be absolute
        work = os.path.join(d, ".archive")
        branch, ref, env = self.archive_branch(), "refs/inbox/archive", {"GIT_INDEX_FILE": os.path.join(d, "index.archive")}
        try:
            self._clear_worktree(d, work)                               # leftovers of an older server (it used a worktree here)
            os.makedirs(work)
            names = []
            for i, (key, good) in enumerate(batches):
                f = os.path.join(work, "%d.jsonl" % i)
                with open(f, "w") as fh:
                    fh.write("\n".join(good) + "\n")
                names.append(f)
            shas = gitx.run(["hash-object", "-w", "--stdin-paths", "--no-filters"], cwd=d,
                            input="".join(n + "\n" for n in names)).split()
            entries = []
            for (key, _), sha in zip(batches, shas):
                parts = key.split("-")                                  # <client>-<unix>-<rand>
                stamp = parts[-2] if len(parts) >= 3 else ""
                day = time.strftime("%Y-%m-%d", time.gmtime(int(stamp) if stamp.isdigit() else time.time()))
                entries.append("100644 %s\tarchive/%s/%s.jsonl\n" % (sha, day, key))
            known = getattr(self, "_archive_tip", None)
            for attempt in range(3):
                have = gitx.run(["rev-parse", "-q", "--verify", ref], cwd=d, check=False)
                if attempt or known is None or (known and known != have):
                    base = gitx.fetch_tip(d, "origin", branch, ref, depth=1)
                else:
                    base = known                                        # the ls-remote of this pass says our copy is current
                gitx.run(["read-tree", base or "--empty"], cwd=d, extra_env=env)
                gitx.run(["update-index", "--index-info"], cwd=d, input="".join(entries), extra_env=env)
                tree = gitx.run(["write-tree"], cwd=d, extra_env=env)
                if base and tree == gitx.run(["rev-parse", base + "^{tree}"], cwd=d):
                    return True                                         # already archived (a retried pass)
                commit = gitx.run(["-c", "user.name=aihub-server", "-c", "user.email=aihub-server@localhost", "commit-tree", tree,
                                   "-m", "telemetry: %d batch(es)" % len(batches)] + (["-p", base] if base else []), cwd=d)
                try:
                    gitx.run(["push", "-q", "origin", "%s:refs/heads/%s" % (commit, branch)], cwd=d, timeout=120)
                except gitx.GitError:
                    continue                                            # someone else pushed to the archive meanwhile: rebuild on the new tip
                gitx.run(["update-ref", ref, commit], cwd=d, check=False)
                self._archive_tip = commit
                self.status.update(archive_last=time.time(), archive_batches=self.status.get("archive_batches", 0) + len(batches),
                                   archive_commit=commit[:10])
                self._count_archive(d, commit)
                log.info("sync: archived %d batch(es) to %s (%s)", len(batches), branch, commit[:10])
                return True
            return False
        except Exception as e:
            self.status["archive_error"] = "archiving usage: " + gitx.redact_url(str(e))[:220]
            log.warning("telemetry archive failed (refs kept): %s", gitx.redact_url(str(e))[:200])
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

    def update_index(self, pubs, force=False, results=None):
        """Add published versions to the index repo. Nothing from the event is trusted but where to look: the package repo is
        fetched at the published commit and its aihub.toml is what goes in. -> True when the index repo changed.
        force=True re-indexes a version that is already listed. `results` (a list) receives one (name, version, status) per item."""
        results = results if results is not None else []
        with self.index_lock:
            for attempt in range(3):
                mark = len(results)
                try:
                    return self._update_index(pubs, force, results)
                except gitx.GitError as e:                              # push rejected (someone else pushed meanwhile): fetch the new tip and redo the batch
                    if attempt == 2:
                        raise
                    log.info("index push failed (%s); fetching the latest index and retrying", gitx.redact_url(str(e))[:150])
                    del results[mark:]

    def _update_index(self, pubs, force, results):
        url, branch, path = self.index_source()
        if not url or os.path.isfile(url):
            return False
        work = os.path.join(self.s.data_dir, "index-repos")
        os.makedirs(work, exist_ok=True)
        if gitx.remote_head(url, branch):
            d = gitx.fetch(url, branch, base=work)
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
            if not gurl.startswith(SCHEMES):
                results.append((name, ver, "git address not allowed"))
                continue                                                # no file:// or other odd transports from event data
            if not gurl.startswith("file://") and not nethost.allowed(gurl, url):
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
            try:
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
        if not changed or not gitx.run(["status", "--porcelain", "--", path], cwd=d):
            return False
        gitx.run(["add", "-A", "--", path], cwd=d)
        gitx.run(["-c", "user.name=aihub-server", "-c", "user.email=aihub-server@localhost", "commit", "-q", "-m",
                  "index: %d published version(s)" % changed], cwd=d)
        gitx.run(["push", "-q", "origin", "HEAD:refs/heads/" + branch], cwd=d)
        return True

    def _store(self, rows):
        return self.repos.events_insert_ext(rows) if rows else 0

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
                return pkgindex.Legacy(json.load(f)), os.path.getmtime(url)
        if os.path.isdir(url):
            rd = pkgindex.open_source(url, path)
            return rd, getattr(rd, "root", {}).get("digest")
        if not gitx.remote_head(url, branch):
            if not gitx.reachable(url):
                raise RuntimeError("cannot reach %s" % gitx.redact_url(url))
            return self._bootstrap_index()                              # empty repo or no index branch yet: create it
        try:
            d = gitx.fetch(url, branch, base=os.path.join(self.s.data_dir, "repos"))
        except gitx.GitError as e:
            raise RuntimeError("cannot fetch %s (branch '%s'): %s" % (gitx.redact_url(url), branch, e))
        try:
            return pkgindex.open_source(d, path), gitx.run(["rev-parse", "HEAD"], cwd=d)
        except FileNotFoundError:                                      # branch exists but has no index folder: create it
            return self._bootstrap_index()

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
                man["repo"] = dict(p.get("repo") or {}, ref=v.get("ref"))
                meta = man.setdefault("package", {})
                for k in ("authors", "license"):                       # entry-level (latest) values fill what the version lacks
                    if p.get(k) and not meta.get(k):
                        meta[k] = p[k]
                self.repos.version_sync(pid, v["version"], man)
        self.repos.package_hide_missing(names)
        self.repos.set_setting("sync_index_hashes", json.dumps(hashes, separators=(",", ":")))
        if rev is not None:
            self.repos.set_setting("sync_index_rev", str(rev))
        self.repos.set_setting("sync_index_dirty", "0")
        self.status["index_packages"] = len(names)
