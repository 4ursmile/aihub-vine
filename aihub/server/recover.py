"""Startup reconciliation between the local database and the git backup (backups/ in the index repo).

Runs before the app opens its database, from `python -m aihub.server`:
  - nothing to compare (no index git URL, no backup in the repo, backup unreachable) -> start normally
  - local database empty, backup has data   -> restore (a lost data dir or a new machine)
  - local already holds everything the backup has -> start normally (the usual case)
  - otherwise they differ (backup is newer or has more rows) -> show both with their times, ask which to keep;
    the newer side is the default. Without a terminal nothing is overwritten unless asked via --recover / AIHUB_RECOVER.
Before anything is replaced the local data is saved under <data_dir>/pre-recover-<time>/.
"""
import gzip
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time

from ..cli import gitx
from .backup import KEY_TABLES, SKIP, Backup, summary, table_cols, tables
from .db import init_db

log = logging.getLogger("aihub")
MODES = ("ask", "local", "remote", "latest", "skip")
DROP_COLS = {"fts"}                                   # postgres generated column: rebuilt by the database


def ago(t):
    if not t:
        return "never"
    d = max(0, time.time() - t)
    for n, u in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if d >= n:
            return "%d %s%s ago" % (d // n, u, "" if d // n == 1 else "s")
    return "just now"


def stamp(t):
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(t)) + " (%s)" % ago(t) if t else "no activity recorded"


def _parse_iso(z):
    try:
        import calendar
        return float(calendar.timegm(time.strptime(z, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return 0.0


def remote_summary(d):
    """What the backup checkout holds, from backups/index.json -> dict or None when it holds no backup."""
    try:
        with open(os.path.join(d, "backups", "index.json")) as f:
            idx = json.load(f)
    except (OSError, ValueError):
        return None
    rows = {}
    for e in idx.get("files", []):
        rows[e["table"]] = rows.get(e["table"], 0) + e.get("rows", 0)
    st = idx.get("stats") or {}
    taken = _parse_iso(idx.get("created"))
    return {"rows": rows, "latest": st.get("latest") or taken, "taken": taken, "index": idx}


def _empty(s):
    return not any(s["rows"].get(t, 0) for t in KEY_TABLES)


def _covers(local, remote):
    """True when the local database has nothing to gain from the backup: no key table has fewer rows and nothing is older."""
    return local["latest"] >= remote["latest"] and all(local["rows"].get(t, 0) >= remote["rows"].get(t, 0) for t in KEY_TABLES)


def show(local, remote, url, newer):
    w = max(len(t) for t in KEY_TABLES)
    print("\nThe local database and the git backup differ.\n  backup: %s" % gitx.redact_url(url))
    print("\n  %-*s  %10s  %10s" % (w, "", "local", "backup"))
    for t in KEY_TABLES:
        a, b = local["rows"].get(t, 0), remote["rows"].get(t, 0)
        print("  %-*s  %10d  %10d%s" % (w, t, a, b, "   <- differs" if a != b else ""))
    tag = lambda side: "   (latest, default)" if newer == side else ""
    print("\n  [l] keep LOCAL   last activity: %s%s" % (stamp(local["latest"]), tag("local")))
    print("  [b] use BACKUP   last activity: %s%s" % (stamp(remote["latest"]), tag("remote")))
    print("      backup written: %s" % stamp(remote["taken"]))
    print("\n  Whichever you do not pick stays recoverable: local data is copied to <data>/pre-recover-*/ first,")
    print("  and keeping local makes the next backup a new git commit on top of the old one.\n")


def choose(local, remote, url, mode, interactive):
    """-> 'local' | 'remote'."""
    newer = "remote" if remote["latest"] > local["latest"] else "local"
    if _empty(local):
        newer = "remote"
    if mode in ("local", "remote"):
        return mode
    if mode == "latest":
        return newer
    if interactive:
        show(local, remote, url, newer)
        while True:
            a = input("Keep which? [l]ocal / [b]ackup (Enter = %s): " % ("backup" if newer == "remote" else "local")).strip().lower()
            if not a:
                return newer
            if a[0] in "lb":
                return "local" if a[0] == "l" else "remote"
    if _empty(local):
        return "remote"                                   # restoring into an empty database loses nothing
    log.warning("local database and git backup differ (backup latest %s, local latest %s) and there is no terminal to ask: "
                "keeping local. Restart with --recover remote|latest to take the backup.", stamp(remote["latest"]), stamp(local["latest"]))
    return "local"


def _safety_copy(s, repos):
    out = os.path.join(s.data_dir, "pre-recover-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime()))
    os.makedirs(out, exist_ok=True)
    if repos.db.engine == "sqlite" and os.path.isfile(s.sqlite_file):
        c = repos.db.conn()
        dst = sqlite3.connect(os.path.join(out, "aihub.db"))
        try:
            c.raw.backup(dst)                             # consistent copy even while WAL is active
        finally:
            dst.close()
    else:
        Backup(repos, s, None).export(out)                # scrubbed archive: the best a remote database allows
    return out


def _read_table(path, table):
    with tempfile.TemporaryDirectory() as t:
        tmp = os.path.join(t, "x.db")
        with gzip.open(path) as g, open(tmp, "wb") as f:
            shutil.copyfileobj(g, f)
        con = sqlite3.connect(tmp)
        try:
            cur = con.execute('SELECT * FROM "%s"' % table)
            cols = [d[0] for d in cur.description]
            return cols, cur.fetchall()
        finally:
            con.close()


def _usable(table, cols, rows, current):
    """Drop what must not be written back: generated columns, and credentials that were stored only as a hash.
    A hashed secret would replace a working one; without a local value it is skipped (re-enter it in the admin page)."""
    if table == "app_settings":
        ki, vi = cols.index("key"), cols.index("value")
        keep = []
        for r in rows:
            v = r[vi]
            if isinstance(v, str) and v.startswith("sha256:") and r[ki] not in ("sync_names",):
                if r[ki] in current:
                    r = tuple(current[r[ki]] if i == vi else x for i, x in enumerate(r))
                else:
                    continue
            keep.append(r)
        rows = keep
    return rows


def restore(repos, d, idx):
    """Replace the content of every backed-up table with the backup, all in one transaction."""
    db, c = repos.db, repos.db.conn()
    have = set(tables())
    by_table = {}
    for e in idx["files"]:
        if e["table"] in have and e["table"] not in SKIP:
            by_table.setdefault(e["table"], []).append(e["path"])
    current = {r[0]: r[1] for r in c.execute("SELECT key,value FROM app_settings")}
    try:
        total = 0
        for t in tables():
            if t not in by_table:
                continue
            ours = [x for x in table_cols(db, c, t) if x not in DROP_COLS]
            c.execute("DELETE FROM %s" % t)
            for rel in sorted(by_table[t]):
                cols, rows = _read_table(os.path.join(d, rel), t)
                use = [x for x in cols if x in ours]
                pick = [cols.index(x) for x in use]
                data = _usable(t, use, [tuple(r[i] for i in pick) for r in rows], current)
                if data:
                    c.executemany("INSERT INTO %s(%s) VALUES(%s)" % (t, ",".join(use), ",".join("?" * len(use))), data)
                total += len(data)
        if db.engine == "postgres":
            for t in db.ID_TABLES:
                c.execute("SELECT setval(pg_get_serial_sequence('%s','id'), COALESCE((SELECT MAX(id) FROM %s),1), "
                          "(SELECT MAX(id) FROM %s) IS NOT NULL)" % (t, t, t))
        elif db.fts:
            c.execute("DELETE FROM packages_fts")
            c.execute("INSERT INTO packages_fts(rowid,name,description,tags,body) SELECT id,name,description,tags,readme FROM packages")
        c.execute("INSERT OR REPLACE INTO app_settings VALUES('sync_index_dirty','1')")
        c.commit()
        return total
    except Exception:
        c.rollback()
        raise


def run(s, mode="ask", interactive=None):
    """Reconcile before serving. Never raises for remote trouble (an unreachable git host must not stop the server).
    -> 'none' | 'local' | 'remote'."""
    mode = (mode or s.env_get("AIHUB_RECOVER") or "ask").lower()
    if mode not in MODES:
        raise SystemExit("aihub: --recover must be one of %s" % ", ".join(MODES))
    if mode == "skip":
        return "none"
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    repos = None
    try:
        from .repos import Repos
        repos = Repos(init_db(s))
        url = s.env_get("AIHUB_INDEX_URL") or repos.setting("index_url", "")
        branch = s.env_get("AIHUB_INDEX_BRANCH") or repos.setting("index_branch", "") or "main"
        if not url or os.path.isfile(url):
            return "none"
        try:
            if not gitx.remote_head(url, branch):
                return "none"                             # empty remote: the first backup will create it
            d = gitx.fetch(url, branch, base=os.path.join(s.data_dir, "recover-repos"))
        except Exception as e:
            log.warning("recovery check skipped, cannot read %s: %s", gitx.redact_url(url), gitx.redact_url(str(e))[:200])
            return "none"
        remote = remote_summary(d)
        if not remote or not any(remote["rows"].get(t, 0) for t in KEY_TABLES):
            return "none"
        local = summary(repos.db.conn())
        if _covers(local, remote) and not _empty(local):
            return "none"
        pick = choose(local, remote, url, mode, interactive)
        if pick == "local":
            log.info("recovery: keeping the local database")
            return "local"
        keep = _safety_copy(s, repos)
        n = restore(repos, d, remote["index"])
        msg = "recovery: restored %d rows from the git backup (local copy saved in %s)" % (n, keep)
        log.info(msg)
        print(msg)
        return "remote"
    finally:
        if repos is not None:
            try:
                repos.db.release()
                repos.db.close()
            except Exception:
                pass
