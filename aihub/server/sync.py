"""Pulls the world into the database: usage events from Kafka, package metadata from the git index.

- Events: CLIs produce every event (publish included) to a Kafka topic; `kafka_in.KafkaIngest` consumes it and calls `Sync.ingest`, so the
  web updates at once. events.ext_id is the event id and unique, so redelivery never duplicates rows. There is no git inbox or archive:
  the database backup keeps the events.
- Index: the server is the only writer, and it writes in BATCHES. A `publish` event with a version the database does not know yet is
  shown on the web immediately (a provisional row) and put in a persisted queue (`index_queue`). Once per sync interval the queue is
  drained: each package repo is fetched at the published commit, its aihub.toml is read, and ONE commit/push updates the index repo.
  Nothing else touches the index remote: it is not polled, so many publishes never become many git requests.
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
INDEX_QUEUE = "index_queue"             # app_settings key: publish events waiting for the next batched index commit
INDEX_TRIES = 5                         # passes a version whose repo could not be read stays queued


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
        self.index_lock = threading.Lock()                  # one index writer at a time (event sync and manual forcing share a checkout)
        self.queue_lock = threading.Lock()                  # the persisted index queue is read-modify-written by the consumer and the sync thread
        self.kafka = None                                   # kafka_in.KafkaIngest once started
        self.status = {"last_run": None, "last_error": None, "index_error": None, "events_total": 0, "index_packages": 0,
                       "last_events": 0,
                       "index_queued": 0, "index_last_push": None, "kafka": {}}

    # ----- config: env first, then admin settings
    def cfg(self, key, env, default=""):
        return self.s.env_get(env) or self.repos.setting(key, "") or default

    def delay(self):
        try:
            return next_delay(parse_schedule(self.repos.setting("sync_interval", "") or "60"))
        except ValueError:
            return 60

    def kafka_cfg(self):
        """Kafka settings, or {} when off. Environment (AIHUB_KAFKA_<FIELD>) > admin settings (kafka_<field>) > defaults.json `kafka`."""
        from ..core import defaults, kafkacfg
        d = defaults.load().get("kafka", {})
        return kafkacfg.resolve(lambda k: self.cfg("kafka_" + k, "AIHUB_KAFKA_" + k.upper()) or d.get(k))

    def start(self):
        gitx.BATCH = True                                   # server: git must never wait for a prompt nobody can answer
        from .kafka_in import KafkaIngest
        self.kafka = KafkaIngest(self)
        self.kafka.start()
        url, branch, path = self.index_source()
        log.info("sync: package index %s (branch %s, folder %s)", gitx.redact_url(url) or "-", branch, path)
        self.t = threading.Thread(target=self._loop, name="aihub-sync", daemon=True)
        self.t.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.kafka:
            self.kafka.close()

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
        """One pass. The two halves are independent: if Kafka is down, the package
        index is still synced, and the other way round. Every failure is kept in `status.last_error` (shown in Admin > Sync) until a pass
        succeeds completely; a step that fails never reports success and never lets the cursor claim the pass completed."""
        first = not self.repos.setting("sync_cursor")
        errors = []
        self.status["index_error"] = None
        try:
            changed = self.flush_index()                                # the batched index commit: the only routine write to the index remote
            if first or changed or self.repos.setting("sync_index_dirty") == "1":
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

    # ----- events from Kafka, provisional packages, the batched index queue
    def ingest(self, events):
        """Store events that arrived from Kafka (dicts as the CLI sent them) and show new package versions on the web now.
        Idempotent: a redelivered message changes nothing. -> number of new event rows."""
        rows, pubs = [], {}
        for e in events:
            r = to_row(e)
            if not r:
                continue
            rows.append(r)
            if r["kind"] == "publish" and e.get("version") and e.get("git_url"):
                pubs[(r["package"], str(e["version"]))] = e                 # latest event per version wins
        n = self._store(rows)
        self.status["events_total"] += n
        self.status["last_events"] = n
        shown = self.ingest_publishes(list(pubs.values())) if pubs else 0
        self._drop_caches(n, shown)
        return n

    def _drop_caches(self, events, versions):
        """New events change counts and the feed ("pkg"); only a new/changed version or package changes the CLI index ("index"), so usage
        events never make the hub rebuild the index from the database."""
        if self.cache is None:
            return
        try:
            if events or versions:
                self.cache.invalidate("pkg")
            if versions:
                self.cache.invalidate("index")
        except Exception:
            pass

    def ingest_publishes(self, pubs):
        """A publish event for a version the database does not know (or knows at another commit): add it to the web NOW as a provisional
        row, and queue it for the next batched index commit. Event data is untrusted: it is bounded here, and the index commit re-reads the
        real aihub.toml from the repo, which replaces these values. -> number of versions added or changed."""
        from ..core import manifest as M
        url = self.index_source()[0]
        queue, shown = [], 0
        for e in pubs:
            try:
                name, ver, gurl = naming.normalize(str(e["package"])), redact.text(e["version"], 64), str(e.get("git_url") or "")
                commit = redact.text(e.get("commit"), 64)
                known = self.repos.version_ref(name, ver)
                if known is not None and (known == commit or not commit):
                    continue                                                # same version, same commit: nothing differs, no git work at all
                if not gurl.startswith(SCHEMES) or (not gurl.startswith("file://") and not nethost.allowed(gurl, url)):
                    log.warning("publish event for %s names a git address that is not allowed; ignored", name)
                    continue
                req = e.get("requires") if isinstance(e.get("requires"), dict) else {}
                if len(json.dumps(req)) > 20000:
                    req = {}
                repo = {"url": redact.text(gurl, 300), "branch": redact.text(e.get("git_branch") or "main", 100),
                        "subdir": redact.text(e.get("git_subdir"), 200), "ref": commit}
                tags = [redact.text(t, 40) for t in (e.get("tags") if isinstance(e.get("tags"), list) else [])[:30] if t]
                typ = e.get("type") if e.get("type") in M.TYPES else "tool"
                old = self.repos.package(name)
                # An existing package keeps its metadata: an event may add a version but never rewrites what the package says about itself
                # (the index commit reads the real aihub.toml and updates it properly). Only a package that is new is created from the event.
                pid = old["id"] if old else self.repos.package_upsert(name, typ, redact.text(e.get("description"), 1000), tags, "",
                                                                      visibility=self.repos.initial_visibility())
                item = {"package": name, "version": ver, "git_url": gurl, "git_branch": repo["branch"],
                        "git_subdir": repo["subdir"], "commit": commit}
                self.queue_index([item])                                    # queued BEFORE it is shown: a failure in between is redelivered, never lost
                self.repos.version_sync(pid, ver, {"package": {"name": name, "version": ver, "type": typ}, "requires": req, "repo": repo})
                shown += 1
                queue.append(item)
            except (ValueError, KeyError, TypeError) as ex:                 # a malformed event is dropped; the rest of the batch goes on
                log.warning("publish event ignored: %s", ex)
            # any other error (database down, ...) propagates: the Kafka consumer then does not commit and the batch is redelivered
        if queue:
            log.info("sync: %d new package version(s) shown, queued for the next index commit", len(queue))
        return shown

    def _queue_read(self):
        try:
            q = json.loads(self.repos.setting(INDEX_QUEUE) or "[]")
            return q if isinstance(q, list) else []
        except ValueError:
            return []

    def _queue_write(self, q):
        self.repos.set_setting(INDEX_QUEUE, json.dumps(q, separators=(",", ":")))
        self.status["index_queued"] = len(q)

    def queue_index(self, items):
        with self.queue_lock:
            q = {(i["package"], i["version"]): i for i in self._queue_read()}
            for i in items:
                q[(i["package"], i["version"])] = dict(i, tries=0)          # a newer event for the same version replaces the older one
            self._queue_write(list(q.values()))

    def queued_names(self):
        return {i.get("package") for i in self._queue_read()}

    def flush_index(self):
        """Write everything queued to the index repo in ONE commit and push. A git failure keeps the whole queue for the next interval;
        a version whose repo could not be read is retried a few times. -> True when the queue held something (the index is then re-read)."""
        with self.queue_lock:
            items = self._queue_read()
            self.status["index_queued"] = len(items)
        if not items:
            return False
        results = []
        changed = self.update_index(items, results=results)
        retry = {(n, v) for n, v, st in results if st.startswith("cannot read")}
        with self.queue_lock:
            done = {(i["package"], i["version"]): i for i in items}
            keep = [i for i in self._queue_read() if (i["package"], i["version"]) not in done or done[(i["package"], i["version"])].get("commit") != i.get("commit")]
            keep += [dict(i, tries=int(i.get("tries") or 0) + 1) for i in items
                     if (i["package"], i["version"]) in retry and int(i.get("tries") or 0) + 1 < INDEX_TRIES]
            self._queue_write(keep)
        if changed:
            self.status["index_last_push"] = time.time()
        try:                                                            # re-read these entries from git next: the real aihub.toml replaces the provisional rows
            seen = json.loads(self.repos.setting("sync_index_hashes") or "{}")
            for i in items:
                seen.pop(i["package"], None)
            self.repos.set_setting("sync_index_hashes", json.dumps(seen, separators=(",", ":")))
        except ValueError:
            self.repos.set_setting("sync_index_hashes", "")
        return True                                                     # something was processed: pull_index must run (it is a no-op when nothing moved)

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
        self.repos.package_hide_missing(names | self.queued_names())      # shown from a publish event, not in git yet: not "removed"
        self.repos.set_setting("sync_index_hashes", json.dumps(hashes, separators=(",", ":")))
        if rev is not None:
            self.repos.set_setting("sync_index_rev", str(rev))
        self.repos.set_setting("sync_index_dirty", "0")
        self.status["index_packages"] = len(names)
        self._drop_caches(0, 1)                                         # git held something the database did not: rebuild the cached index once
