"""Pulls the world into SQLite: usage events from the git inbox, package metadata from the git index.

- Events: CLIs push batches to refs/telemetry/<client>/<batch> in the inbox repo. The server fetches them, stores the rows
  (events.ext_id is the event id and unique, so restarts and several servers never duplicate rows), copies the raw batch to
  the `telemetry-archive` branch, and only then deletes the refs. A failure at any step leaves the refs for the next pass.
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
    def __init__(self, repos, settings):
        self.repos, self.s = repos, settings
        self.stop, self.wake = threading.Event(), threading.Event()
        self.t = None
        self.first_pass = threading.Event()                 # set once the first pass after start has finished (ok or not)
        self._lookback_checked = False
        self.index_lock = threading.Lock()                  # one index writer at a time (event sync and manual forcing share a checkout)
        self.status = {"last_run": None, "last_error": None, "events_total": 0, "index_packages": 0}

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
        first = not self.repos.setting("sync_cursor")
        new_publish = False
        if self.inbox() and not os.path.isfile(self.inbox()):
            if self._needs_replay():
                self.replay_archive()
            new_publish = self.pull_events()
        if first or new_publish or self.repos.setting("sync_index_dirty") == "1":
            self.pull_index()
        self.repos.set_setting("sync_cursor", time.time())             # "an events pass completed"; empty = never (first run, forced re-sync)
        self.status.update(last_run=time.time(), last_error=None)

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
        if not gitx.remote_head(url, branch):
            return 0                                                    # nothing archived yet (or archiving was off)
        d = self._inbox_dir(url)
        gitx.run(["fetch", "-q", "--no-tags", "origin", branch], cwd=d, timeout=300)
        files = [f for f in gitx.run(["ls-tree", "-r", "--name-only", "FETCH_HEAD", "archive/"], cwd=d, check=False).splitlines()
                 if f.endswith(".jsonl")]
        total, rows = 0, []
        for f in sorted(files):
            raw = gitx.run(["show", "FETCH_HEAD:" + f], cwd=d, check=False)
            for line in raw.splitlines():
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
        work = os.path.join(self.s.data_dir, "telemetry-repo")
        os.makedirs(work, exist_ok=True)
        d = gitx.cache_dir(url, work)
        if not os.path.isdir(os.path.join(d, ".git")):
            gitx.run(["init", "-q"], cwd=d)
            gitx.run(["remote", "add", "origin", url], cwd=d)
        else:
            gitx.run(["remote", "set-url", "origin", url], cwd=d)
        return d

    def pull_events(self):
        """Fetch every pending batch, store its rows, archive it, and only then delete its ref. -> True when a publish event arrived."""
        url = self.inbox()
        d = self._inbox_dir(url)
        # Batches arrive on branches telemetry/<client>/<batch> (visible in the git host) or, from older CLIs, on refs/telemetry/<client>/<batch>.
        # Both are fetched into private local refs under refs/inbox/{heads,legacy}/ so the server's own branch names can never collide.
        for stale in gitx.run(["for-each-ref", "--format=%(refname)", "refs/inbox/"], cwd=d, check=False).splitlines():
            gitx.run(["update-ref", "-d", stale], cwd=d, check=False)
        gitx.run(["fetch", "-q", "--no-tags", "origin", "+refs/heads/telemetry/*:refs/inbox/heads/*", "+refs/telemetry/*:refs/inbox/legacy/*"],
                 cwd=d, timeout=120)
        refs = sorted(r for r in gitx.run(["for-each-ref", "--format=%(refname)", "refs/inbox/"], cwd=d).splitlines() if r)[:MAX_REFS]
        rows, pubs, publish, batches, done = [], {}, False, [], []
        for ref in refs:
            size = gitx.run(["cat-file", "-s", ref + ":events.jsonl"], cwd=d, check=False)
            raw = gitx.run(["show", ref + ":events.jsonl"], cwd=d, check=False) if size.isdigit() and int(size) <= MAX_BATCH_BYTES else ""
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
            batches.append((ref, good))
            done.append(ref)
            if len(rows) >= 1000:
                self._store(rows)
                rows = []
        n = self._store(rows)
        self.status["events_total"] += n
        if pubs:
            try:
                if self.update_index(list(pubs.values())):
                    publish = True
            except Exception as e:                                      # events are stored; the refs stay so the next pass retries the index
                self.status["last_error"] = "index update: " + gitx.redact_url(str(e))[:250]
                log.warning("index update failed: %s", gitx.redact_url(str(e)))
                return False
        if done and self._archive(d, batches):                          # raw copy is safe in git: the refs can go
            remote = [self._remote_name(r) for r in done]
            for i in range(0, len(remote), 50):
                gitx.run(["push", "-q", "origin", "--delete"] + remote[i:i + 50], cwd=d, check=False, timeout=60)
            for ref in done:
                gitx.run(["update-ref", "-d", ref], cwd=d, check=False)
            gitx.run(["gc", "-q", "--prune=now"], cwd=d, check=False, timeout=120)
        return publish and n > 0

    @staticmethod
    def _remote_name(local):
        """refs/inbox/heads/<client>/<batch> -> refs/heads/telemetry/<client>/<batch>;  refs/inbox/legacy/... -> refs/telemetry/..."""
        kind, _, rest = local[len("refs/inbox/"):].partition("/")
        return ("refs/heads/telemetry/" if kind == "heads" else "refs/telemetry/") + rest

    def _archive(self, d, batches):
        """Write each batch as archive/<day>/<client>-<batch>.jsonl on the archive branch and push it. The path is derived from the
        ref, so doing it twice writes the same file. -> True when the archive holds the batches (or archiving is off)."""
        if self.repos.setting("telemetry_archive", "1") == "0":
            return True
        batches = [(ref, good) for ref, good in batches if good]
        if not batches:
            return True
        work = os.path.join(d, ".archive")
        branch = self.archive_branch()
        try:
            for attempt in range(3):
                if gitx.remote_head(self.inbox(), branch):
                    gitx.run(["fetch", "-q", "--no-tags", "origin", branch], cwd=d, timeout=120)
                    base = gitx.run(["rev-parse", "FETCH_HEAD"], cwd=d)
                else:
                    base = ""
                gitx.run(["worktree", "prune"], cwd=d, check=False)
                if os.path.isdir(work):
                    gitx.run(["worktree", "remove", "--force", work], cwd=d, check=False)
                if base:
                    gitx.run(["worktree", "add", "-q", "--detach", work, base], cwd=d)
                else:                                                   # first batch ever: start the archive branch from nothing
                    os.makedirs(work, exist_ok=True)
                    gitx.run(["init", "-q"], cwd=work)
                    gitx.run(["checkout", "-q", "--orphan", branch], cwd=work)
                for ref, good in batches:
                    client, batch = ref.split("/")[-2:]                      # refs/inbox/<kind>/<client>/<unix>-<rand>
                    stamp = batch.split("-", 1)[0]
                    day = time.strftime("%Y-%m-%d", time.gmtime(int(stamp) if stamp.isdigit() else time.time()))
                    out = os.path.join(work, "archive", day)
                    os.makedirs(out, exist_ok=True)
                    with open(os.path.join(out, "%s-%s.jsonl" % (client, batch)), "w") as f:
                        f.write("\n".join(good) + "\n")
                gitx.run(["add", "-A"], cwd=work)
                if gitx.run(["status", "--porcelain"], cwd=work):
                    gitx.run(["-c", "user.name=aihub-server", "-c", "user.email=aihub-server@localhost", "commit", "-q", "-m",
                              "telemetry: %d batch(es)" % len(batches)], cwd=work)
                    try:
                        gitx.run(["push", "-q", self.inbox(), "HEAD:refs/heads/" + branch], cwd=work, timeout=120)
                    except gitx.GitError:
                        continue                                        # someone else pushed to the archive meanwhile: rebuild on the new tip
                return True
            return False
        except Exception as e:
            log.warning("telemetry archive failed (refs kept): %s", gitx.redact_url(str(e))[:200])
            return False
        finally:
            gitx.run(["worktree", "remove", "--force", work], cwd=d, check=False)

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
