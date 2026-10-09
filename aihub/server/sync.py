"""Pulls the world into SQLite: usage events from Langfuse, package metadata from the git index.

- Events: polled by observation id (events.ext_id is unique), so overlap, restarts and several servers never
  duplicate or lose rows. First run (no cursor) looks back over everything.
- Index: the server is the only writer. A new `aihub.publish` event (public data sent by the CLI) makes the server fetch that
  package repo at the published commit, read its aihub.toml, update the index repo and push; then the index is re-read.
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

from ..cli import gitx, langfuse
from ..core import naming, pkgindex, redact
from . import nethost

log = logging.getLogger("aihub")
OVERLAP = 300          # seconds re-read behind the cursor; ext_id makes the overlap harmless
KINDS = {"aihub.use": "use", "aihub.install": "install", "aihub.uninstall": "uninstall",
         "aihub.update": "update", "aihub.error": "error", "aihub.publish": "publish"}


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


def _ts(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return time.time()


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


SCHEMES = ("https://", "http://", "ssh://", "git@")      # transports a publish event may name (tests add file://)
MAX_COUNT = 1000      # calls one span may stand for; bounds rows per observation


def to_rows(o):
    """Langfuse observation -> events rows. A span carries `count` calls (the CLI rolls calls up per window), stored as
    that many rows so every dashboard query stays a plain COUNT. Row ids are <observation id>#<n>: re-reads add nothing."""
    base = to_row(o)
    if not base:
        return []
    try:
        n = max(1, min(int(float((o.get("metadata") or {}).get("count") or 1)), MAX_COUNT))
    except (TypeError, ValueError):
        n = 1
    if n == 1:
        return [base]
    return [dict(base, ext_id="%s#%d" % (o["id"], i)) for i in range(n)]


def to_row(o):
    """Langfuse observation -> one events row (None if it is not one of ours)."""
    kind = KINDS.get(o.get("name") or "")
    m = o.get("metadata") or {}
    if not kind or not m.get("package"):
        return None
    uid = o.get("userId") or m.get("user") or ""
    anon = uid.startswith("~") or not uid
    return {"ts": _ts(o.get("startTime") or ""), "kind": kind, "package": naming.normalize(str(m["package"])),
            "version": m.get("version"), "client_id": m.get("client_id"),
            "username": None if anon else str(uid)[:64], "component": str(m["component"])[:120] if m.get("component") else None,
            "duration": None,
            "source": m.get("source") if m.get("source") in ("claude", "opencode", "codex", "cli") else None,
            # Langfuse content is untrusted input: scrub it exactly as the old POST /events did
            "local_user": redact.text(uid[1:] if uid.startswith("~") else m.get("local_user"), 64),
            "host": redact.text(m.get("host"), 128), "detail": redact.text(m.get("detail"), 2200),
            "cwd": redact.text(m.get("cwd"), 200), "ip": None, "ext_id": o["id"]}


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

    def lf(self):
        return {"host": self.cfg("langfuse_host", "LANGFUSE_BASE_URL").rstrip("/") or self.s.env_get("LANGFUSE_HOST").rstrip("/"),
                "public": self.cfg("langfuse_public_key", "LANGFUSE_PUBLIC_KEY"),
                "secret": self.cfg("langfuse_secret_key", "LANGFUSE_SECRET_KEY")}

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
        lf = self.lf()
        first = not self.repos.setting("sync_cursor")
        new_publish = False
        if langfuse.configured(lf):
            new_publish = self.pull_events(lf)
        if first or new_publish or self.repos.setting("sync_index_dirty") == "1":
            self.pull_index()
        self.status.update(last_run=time.time(), last_error=None)

    def pull_events(self, lf):
        cursor = self.repos.setting("sync_cursor")
        if not self._lookback_checked:
            self._lookback_checked = True
            # Cursor says "pulled up to here" but no pulled event is stored (database replaced, partial restore):
            # trust nothing, read Langfuse's whole retention again. Inserts are idempotent on ext_id.
            if cursor and not self.repos.db.conn().execute("SELECT 1 FROM events WHERE ext_id IS NOT NULL LIMIT 1").fetchone():
                cursor = None
        since = _iso(float(cursor) - OVERLAP) if cursor else None      # None = full lookback
        started = time.time()
        rows, publish, pubs = [], False, {}
        for o in langfuse.fetch_observations("aihub.", since=since, s=lf):
            if o.get("name") == "aihub.publish":
                m = o.get("metadata") or {}
                if m.get("package") and m.get("version") and m.get("git_url"):
                    pubs[(naming.normalize(str(m["package"])), str(m["version"]))] = m      # latest event per version wins
            for r in to_rows(o):
                rows.append(r)
                publish = publish or (r["kind"] == "publish")
            if len(rows) >= 1000:
                self._store(rows)
                rows = []
        n = self._store(rows)
        if pubs:
            try:
                if self.update_index(list(pubs.values())):
                    publish = True
            except Exception as e:                                      # events stay stored; the next full sync retries
                self.status["last_error"] = "index update: " + gitx.redact_url(str(e))[:250]
                log.warning("index update failed: %s", gitx.redact_url(str(e)))
        self.repos.set_setting("sync_cursor", started - 1)             # only advanced after a fully successful pass
        new = self.status["events_total"] + n
        self.status["events_total"] = new
        return publish and (n > 0 or not cursor)

    def update_index(self, pubs, force=False, results=None):
        """Add published versions to the index repo. Nothing from the event is trusted but where to look: the package repo is
        fetched at the published commit and its aihub.toml is what goes in. -> True when the index repo changed.
        force=True re-indexes a version that is already listed. `results` (a list) receives one (name, version, status) per item."""
        with self.index_lock:
            return self._update_index(pubs, force, results if results is not None else [])

    def _update_index(self, pubs, force, results):
        url, branch, path = self.index_source()
        if not url or os.path.isfile(url):
            return False
        work = os.path.join(self.s.data_dir, "index-repos")
        os.makedirs(work, exist_ok=True)
        d = gitx.fetch(url, branch, base=work) if gitx.remote_head(url, branch) else None
        if d is None:
            results.append(("", "", "index repository not reachable"))
            return False
        folder = os.path.join(d, path[:-5] if path.endswith(".json") else path)
        changed = 0
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
        d = gitx.fetch(url, branch, base=os.path.join(self.s.data_dir, "repos"))
        return pkgindex.open_source(d, path), gitx.run(["rev-parse", "HEAD"], cwd=d)

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
                self.repos.version_sync(pid, v["version"], man)
        self.repos.package_hide_missing(names)
        self.repos.set_setting("sync_index_hashes", json.dumps(hashes, separators=(",", ":")))
        if rev is not None:
            self.repos.set_setting("sync_index_rev", str(rev))
        self.repos.set_setting("sync_index_dirty", "0")
        self.status["index_packages"] = len(names)
