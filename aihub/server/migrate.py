"""Move data between backends.  Source = your current config (.env / environment); destination = another .env file.

    python -m aihub.server.migrate db      --to target.env     # SQLite <-> PostgreSQL
    python -m aihub.server.migrate storage --to target.env     # local <-> S3, sha256-verified
    python -m aihub.server.migrate all     --to target.env

Safe to re-run (rows are upserted, existing files skipped). Stop the server while migrating.
"""
import argparse
import hashlib
import os
import re
import sys

from .config import Settings
from .db import TABLES, init_db
from .storage import init_storage

CHUNK = 2000


def _dest(path):
    if not os.path.isfile(path):
        raise SystemExit("migrate: no such env file: %s" % path)
    s = Settings.load(env={}, env_file=path)
    bad = s.validate()
    if bad:
        raise SystemExit("migrate: %s: %s" % (path, "; ".join(bad)))
    return s


def migrate_db(src, dst, force=False):
    a, b = init_db(src), init_db(dst)
    ca, cb = a.conn(), b.conn()
    if not force and cb.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        raise SystemExit("migrate: destination database already has users; use --force to merge into it")
    for table in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)\(", TABLES):
        cols = [c for c in b._columns(cb, table) if c in a._columns(ca, table) and c != "fts"]
        sql = "INSERT OR REPLACE INTO %s(%s) VALUES(%s)" % (table, ",".join(cols), ",".join("?" * len(cols)))
        cur, n = ca.execute("SELECT %s FROM %s" % (",".join(cols), table))._c, 0
        while True:
            rows = cur.fetchmany(CHUNK)
            if not rows:
                break
            cb.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
            cb.commit()
            n += len(rows)
        if b.engine == "postgres" and table in b.ID_TABLES:      # keep BIGSERIAL ahead of the copied ids
            cb.execute("SELECT setval(pg_get_serial_sequence('%s','id'), COALESCE((SELECT MAX(id) FROM %s),0)+1, false)" % (table, table))
            cb.commit()
        print("  %-18s %d rows" % (table, n))
    b._init_fts(cb)                                              # rebuild full-text index on the destination


def migrate_storage(src, dst):
    a, sa, sb = init_db(src), init_storage(src), init_storage(dst)
    copied = skipped = 0
    bad = []
    for r in a.conn().execute("SELECT p.name AS pkg, v.file AS file, v.sha256 AS sha FROM versions v JOIN packages p ON p.id=v.package_id").fetchall():
        pkg, f, sha = r["pkg"], r["file"], r["sha"]
        if sb.exists(pkg, f):
            skipped += 1
            continue
        if not sa.exists(pkg, f):
            bad.append("%s/%s: missing in source" % (pkg, f))
            continue
        with sa.local_copy(pkg, f) as path:
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                for blk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(blk)
            if h.hexdigest() != sha:
                bad.append("%s/%s: sha256 differs from the database; not copied" % (pkg, f))
                continue
            sb.put(pkg, f, path)
        copied += 1
    print("  files: %d copied, %d already there, %d problems" % (copied, skipped, len(bad)))
    for m in bad:
        print("  !", m)
    return not bad


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["db", "storage", "all"])
    ap.add_argument("--to", required=True, metavar="ENV_FILE", help="a .env file describing the destination backends")
    ap.add_argument("--from", dest="src", metavar="ENV_FILE", help="source .env (default: current configuration)")
    ap.add_argument("--force", action="store_true", help="merge into a destination database that already has users")
    a = ap.parse_args(argv)
    src, dst = Settings.load(env_file=a.src), _dest(a.to)
    ok = True
    if a.what in ("db", "all"):
        print("database: %s -> %s" % (src.db_backend, dst.db_backend))
        migrate_db(src, dst, a.force)
    if a.what in ("storage", "all"):
        print("storage: %s -> %s" % (src.storage_backend, dst.storage_backend))
        ok = migrate_storage(src, dst)
    print("done. Point your server at the new backends (the destination .env) and start it.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
