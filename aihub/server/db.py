"""Database engine. SQLite (default) or PostgreSQL behind one small interface.

repos.py is the only caller. It writes SQL once, with `?` placeholders; this module adapts it:
  - placeholders (? -> %s on Postgres), skipping anything inside quoted strings
  - upserts:  INSERT OR IGNORE / INSERT OR REPLACE
  - last inserted id, bulk insert, full-text search, day bucketing, boolean sums
"""
import os
import re
import sqlite3
import contextvars
import threading

# --------------------------------------------------------------------------------------------- schema
# One schema, written in the common subset. Types that differ are substituted per engine.
TABLES = """
CREATE TABLE IF NOT EXISTS users(id {pk}, username TEXT UNIQUE NOT NULL, password_hash TEXT,
  role TEXT NOT NULL DEFAULT 'user', status TEXT NOT NULL DEFAULT 'active', auth_provider TEXT DEFAULT 'password',
  external_id TEXT, created {real}, display_name TEXT DEFAULT '', title TEXT DEFAULT '', avatar_v INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS avatars(user_id INTEGER PRIMARY KEY, mime TEXT, data {blob});
CREATE TABLE IF NOT EXISTS groups(id {pk}, name TEXT UNIQUE NOT NULL, description TEXT DEFAULT '', created {real}, owner_id INTEGER);
CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER, user_id INTEGER, PRIMARY KEY(group_id,user_id));
CREATE INDEX IF NOT EXISTS gm_user ON group_members(user_id);
CREATE TABLE IF NOT EXISTS package_access(package_id INTEGER, principal_type TEXT, principal_id INTEGER, access TEXT,
  PRIMARY KEY(package_id,principal_type,principal_id));
CREATE TABLE IF NOT EXISTS roles(name TEXT PRIMARY KEY, description TEXT DEFAULT '', builtin INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS role_permissions(role TEXT, permission TEXT, PRIMARY KEY(role,permission));
CREATE TABLE IF NOT EXISTS tokens(hash TEXT PRIMARY KEY, user_id INTEGER, kind TEXT, name TEXT, created {real});
CREATE TABLE IF NOT EXISTS packages(id {pk}, name TEXT UNIQUE NOT NULL, type TEXT, description TEXT,
  tags TEXT DEFAULT '', latest_version TEXT, hidden INTEGER DEFAULT 0, created {real}, updated {real},
  readme TEXT DEFAULT '', visibility TEXT NOT NULL DEFAULT 'public');
CREATE TABLE IF NOT EXISTS package_maintainers(package_id INTEGER, user_id INTEGER, role TEXT, PRIMARY KEY(package_id,user_id));
CREATE TABLE IF NOT EXISTS versions(id {pk}, package_id INTEGER, version TEXT, file TEXT, sha256 TEXT,
  size {bigint}, manifest TEXT, downloads INTEGER DEFAULT 0, yanked INTEGER DEFAULT 0, signature TEXT, created {real},
  UNIQUE(package_id,version));
CREATE TABLE IF NOT EXISTS events(id {pk}, ts {real}, kind TEXT, package TEXT, version TEXT,
  client_id TEXT, username TEXT, component TEXT, duration {real}, source TEXT);
CREATE INDEX IF NOT EXISTS ev_pkg ON events(package,ts);
CREATE INDEX IF NOT EXISTS ev_ts ON events(ts);
CREATE TABLE IF NOT EXISTS reviews(package_id INTEGER, user_id INTEGER, rating INTEGER, body TEXT, created {real},
  PRIMARY KEY(package_id,user_id));
CREATE TABLE IF NOT EXISTS audit_log(id {pk}, ts {real}, actor TEXT, action TEXT, target TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY, value TEXT);
"""

# Every permission the app checks. Admins grant these to roles in the UI.
ALL_PERMS = {
    "publish": "Publish packages",
    "review": "Write reviews",
    "view_dashboard": "View the usage dashboard",
    "manage_all": "Manage every package (not only own)",
    "admin": "Administer accounts, roles and settings",
    "create_groups": "Create and manage own groups",
    "reset_password": "Reset other people's passwords",
    "audit": "View the security audit (tool calls and admin actions)",
    "index": "Force a package into the index (aihub dev index)",
}
PERMS = {
    "admin": list(ALL_PERMS),
    "publisher": ["publish", "review"],
    "user": ["publish", "review"],
}
DESCR = {"admin": "Full access", "publisher": "Can publish and review", "user": "Default role for new accounts"}

# Columns added after the first release. Existing databases get them via ALTER TABLE.
LATE_COLUMNS = [
    ("users", "display_name", "TEXT DEFAULT ''"), ("users", "title", "TEXT DEFAULT ''"), ("users", "avatar_v", "INTEGER DEFAULT 0"),
    ("groups", "owner_id", "INTEGER"),
    ("packages", "readme", "TEXT DEFAULT ''"), ("packages", "visibility", "TEXT NOT NULL DEFAULT 'public'"),
    ("events", "source", "TEXT"),
    ("events", "local_user", "TEXT"), ("events", "host", "TEXT"), ("events", "detail", "TEXT"), ("events", "cwd", "TEXT"), ("events", "ip", "TEXT"),
    ("events", "ext_id", "TEXT"),
]

# a quoted string literal | a positional ? | a named :param (not the :: cast operator)
_PARAM = re.compile(r"'(?:[^']|'')*'|\?|(?<!:):[A-Za-z_]\w*")


class Row(dict):
    """dict that also supports r[0], r[1]... and iteration over values: matches sqlite3.Row for the calls repos.py makes."""
    __slots__ = ()

    def __getitem__(self, k):
        if isinstance(k, int):
            return list(self.values())[k]
        return dict.__getitem__(self, k)

    def keys(self):
        return list(dict.keys(self))


def row_factory(cursor):
    """psycopg row factory producing Row (named + positional access)."""
    import decimal
    cols = [d.name for d in (cursor.description or [])]
    # NUMERIC (SUM/AVG) arrives as Decimal on Postgres but as int/float on SQLite, and Decimal is not JSON-serialisable
    def fix(v):
        if isinstance(v, decimal.Decimal):
            return int(v) if v == v.to_integral_value() else float(v)
        return v
    return lambda values: Row(zip(cols, (fix(v) for v in values)))


# Holds the connection for the current unit of work. A ContextVar (not threading.local) because a request's work can hop
# between threadpool workers; the context travels with it. Background threads get their own context automatically.
_unit = contextvars.ContextVar("aihub_db_unit", default=None)


class Result:
    """Uniform cursor wrapper: rows are mapping-like, lastrowid is always set after an INSERT into an id table."""
    def __init__(self, cur, lastrowid=None):
        self._c, self.lastrowid = cur, lastrowid

    def fetchone(self):
        return self._c.fetchone()

    def fetchall(self):
        return self._c.fetchall()

    def __iter__(self):
        return iter(self._c.fetchall())

    @property
    def rowcount(self):
        return self._c.rowcount


class Conn:
    """What repos.py sees. Same calls as sqlite3.Connection plus the helpers below."""
    def __init__(self, db, raw):
        self.db, self.raw = db, raw

    def execute(self, sql, args=()):
        sql = self.db.adapt(sql)
        if self.db.engine == "postgres":
            cur = self.raw.cursor()
            ins = re.match(r"\s*INSERT\s+INTO\s+(\w+)", sql, re.I)
            want_id = bool(ins) and ins.group(1).lower() in self.db.ID_TABLES and "RETURNING" not in sql.upper() and "ON CONFLICT" not in sql.upper()
            if want_id:
                sql += " RETURNING id"
            cur.execute(sql, tuple(args))
            return Result(cur, cur.fetchone()["id"] if want_id else None)
        cur = self.raw.execute(sql, tuple(args))
        return Result(cur, cur.lastrowid)

    def executemany(self, sql, rows):
        sql = self.db.adapt(sql)
        if self.db.engine == "postgres":
            cur = self.raw.cursor()
            cur.executemany(sql, [tuple(r) if not isinstance(r, dict) else r for r in rows])
            return Result(cur)
        return Result(self.raw.executemany(sql, rows))

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()


class DB:
    ID_TABLES = {"users", "groups", "packages", "versions", "events", "audit_log"}

    def __init__(self, settings=None, path=None):
        from .config import Settings
        if isinstance(settings, str):            # legacy call style: DB(":memory:") / DB("/path/aihub.db")
            settings, path = None, settings
        s = settings or Settings()
        self.settings = s
        self.engine = "postgres" if s.db_backend == "postgres" else "sqlite"
        self.path = path or s.sqlite_file
        self._local = threading.local()
        self.fts = True
        self._pool = None
        if self.engine == "postgres":
            self._open_pool()
        elif self.path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------ connections
    def _open_pool(self):
        try:
            import psycopg
            from psycopg_pool import ConnectionPool
        except ImportError as e:
            raise RuntimeError("PostgreSQL support needs: pip install 'psycopg[binary]' psycopg_pool  (%s)" % e)
        self._pool = ConnectionPool(self.settings.postgres_dsn, min_size=1, max_size=max(2, self.settings.pg_pool_size),
                                    kwargs={"row_factory": row_factory, "autocommit": False}, open=True, timeout=30)

    def conn(self):
        """SQLite: one handle per thread (cheap, file-based).  Postgres: one pooled connection per unit of work (see begin())."""
        if self.engine == "postgres":
            unit = _unit.get()
            if unit is None:                         # not inside begin(): a background thread or a script. Own it per thread.
                unit = getattr(self._local, "unit", None)
                if unit is None:
                    unit = self._local.unit = {}
            if "c" not in unit:
                unit["c"] = Conn(self, self._pool.getconn())
            return unit["c"]
        c = getattr(self._local, "c", None)
        if c is None:
            raw = sqlite3.connect(self.path, timeout=30)
            raw.row_factory = sqlite3.Row
            raw.execute("PRAGMA journal_mode=WAL")
            c = self._local.c = Conn(self, raw)
        return c

    def begin(self):
        """Start a unit of work (one HTTP request). Returns a token for end()."""
        return _unit.set({}) if self.engine == "postgres" else None

    def end(self, token):
        """Finish the unit: roll back anything uncommitted and return the connection to the pool."""
        if self.engine != "postgres":
            return
        unit = _unit.get()
        if unit and "c" in unit:
            try:
                unit["c"].raw.rollback()
            except Exception:
                pass
            self._pool.putconn(unit["c"].raw)
            unit.pop("c", None)
        _unit.reset(token)

    def release(self):
        """For background threads: hand this thread's connection back after each batch."""
        if self.engine != "postgres":
            return
        unit = getattr(self._local, "unit", None)
        if unit and "c" in unit:
            try:
                unit["c"].raw.rollback()
            except Exception:
                pass
            self._pool.putconn(unit["c"].raw)
            unit.pop("c", None)

    def close(self):
        c = getattr(self._local, "c", None)
        if c is not None:
            c.raw.close()
            self._local.c = None
        self.release()
        if self._pool:
            self._pool.close()

    # ------------------------------------------------------------------ SQL adaptation
    def adapt(self, sql):
        """Rewrite the one SQL dialect repos.py uses for this engine."""
        if self.engine == "sqlite":
            return sql
        if re.match(r"\s*INSERT\s+OR\s+IGNORE\s+INTO\s+events\b.*\bext_id\b", sql, re.I | re.S):
            # events dedupe on their unique ext_id (not the primary key); the generic rewrite below only knows primary keys
            sql = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", sql, count=1, flags=re.I).rstrip() + " ON CONFLICT (ext_id) DO NOTHING"
        m = re.match(r"\s*INSERT\s+OR\s+(IGNORE|REPLACE)\s+INTO\s+(\w+)(\s*\([^)]*\))?\s*VALUES\s*(\(.*\))\s*$", sql, re.I | re.S)
        if m:
            kind, table, cols, vals = m.groups()
            sql = "INSERT INTO %s%s VALUES %s%s" % (table, cols or "", vals, self._conflict(kind.upper(), table, cols))
        # SUM(boolean) is an error on Postgres; coerce comparisons to ints
        sql = re.sub(r"SUM\((\w+\.\w+)\s*=\s*('[^']*')\)", r"SUM(CASE WHEN \1=\2 THEN 1 ELSE 0 END)", sql)
        sql = sql.replace("strftime('%Y-%m-%d',e.ts,'unixepoch')", "to_char(to_timestamp(e.ts) AT TIME ZONE 'UTC','YYYY-MM-DD')")
        def one(m):
            t = m.group(0)
            if t == "?":
                return "%s"
            if t[0] == ":":
                return "%(" + t[1:] + ")s"
            return t.replace("%", "%%")
        out, last = [], 0
        for m in _PARAM.finditer(sql):                       # text between tokens may also contain a literal %
            out.append(sql[last:m.start()].replace("%", "%%"))
            out.append(one(m))
            last = m.end()
        out.append(sql[last:].replace("%", "%%"))
        return "".join(out)

    # Columns to update on conflict for INSERT OR REPLACE (everything except the key), read from the catalog so
    # adding a table or column never needs a matching edit here.
    _keycache = {}

    def _pk_cols(self, table):
        if table not in self._keycache:
            c = self.conn()
            rows = c.execute(
                "SELECT a.attname AS col FROM pg_index i JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum = ANY(i.indkey) "
                "WHERE i.indrelid = ?::regclass AND i.indisprimary ORDER BY array_position(i.indkey, a.attnum)", (table,)).fetchall()
            self._keycache[table] = [r["col"] for r in rows]
            c.commit()
        return self._keycache[table]

    def _all_cols(self, table):
        c = self.conn()
        rows = c.execute("SELECT column_name AS col FROM information_schema.columns WHERE table_name=? ORDER BY ordinal_position", (table,)).fetchall()
        c.commit()
        return [r["col"] for r in rows]

    def _conflict(self, kind, table, cols):
        key = self._pk_cols(table)
        if not key:
            raise RuntimeError("table %s has no primary key; cannot upsert" % table)
        target = "(%s)" % ",".join(key)
        if kind == "IGNORE":
            return " ON CONFLICT %s DO NOTHING" % target
        sets = [c for c in self._all_cols(table) if c not in key and c != "fts"]
        if not sets:
            return " ON CONFLICT %s DO NOTHING" % target
        return " ON CONFLICT %s DO UPDATE SET %s" % (target, ",".join("%s=EXCLUDED.%s" % (c, c) for c in sets))

    # ------------------------------------------------------------------ schema / migrations
    def _ddl(self):
        if self.engine == "postgres":
            m = dict(pk="BIGSERIAL PRIMARY KEY", real="DOUBLE PRECISION", blob="BYTEA", bigint="BIGINT")
        else:
            m = dict(pk="INTEGER PRIMARY KEY", real="REAL", blob="BLOB", bigint="INTEGER")
        return TABLES.format(**m)

    def _columns(self, c, table):
        if self.engine == "postgres":
            return {r["column_name"] for r in c.execute("SELECT column_name FROM information_schema.columns WHERE table_name=?", (table,))}
        return {r[1] for r in c.execute("PRAGMA table_info(%s)" % table)}

    def _init_schema(self):
        c = self.conn()
        if self.engine == "postgres":
            c.execute("SELECT pg_advisory_lock(727401)")     # one process migrates at a time
        try:
            for stmt in [s.strip() for s in self._ddl().split(";") if s.strip()]:
                c.execute(stmt)
            c.commit()
            for table, col, ddl in LATE_COLUMNS:
                if col not in self._columns(c, table):
                    c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, ddl))
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ev_ext ON events(ext_id)")   # Langfuse observation id: re-polling never duplicates
            c.commit()
            self._init_fts(c)
            if not c.execute("SELECT 1 FROM app_settings WHERE key='perms_seeded'").fetchone():
                for r, ps in PERMS.items():
                    c.execute("INSERT OR IGNORE INTO roles VALUES(?,?,1)", (r, DESCR[r]))
                    for p in ps:
                        c.execute("INSERT OR IGNORE INTO role_permissions VALUES(?,?)", (r, p))
                c.execute("INSERT OR IGNORE INTO app_settings VALUES('perms_seeded','1')")
            for p in ALL_PERMS:
                c.execute("INSERT OR IGNORE INTO role_permissions VALUES('admin',?)", (p,))
            c.commit()
        finally:
            if self.engine == "postgres":
                c.execute("SELECT pg_advisory_unlock(727401)")
                c.commit()

    def _init_fts(self, c):
        if self.engine == "postgres":
            # generated tsvector column + GIN index; Postgres keeps it in sync for us
            if "fts" not in self._columns(c, "packages"):
                c.execute("ALTER TABLE packages ADD COLUMN fts tsvector GENERATED ALWAYS AS ("
                          "setweight(to_tsvector('simple', coalesce(name,'')),'A') || setweight(to_tsvector('simple', coalesce(description,'')),'B') || "
                          "setweight(to_tsvector('simple', replace(coalesce(tags,''),',',' ')),'C') || setweight(to_tsvector('simple', coalesce(readme,'')),'D')) STORED")
            c.execute("CREATE INDEX IF NOT EXISTS packages_fts_idx ON packages USING GIN (fts)")
            c.commit()
            return
        try:
            c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS packages_fts USING fts5("
                      "name, description, tags, body, tokenize='unicode61 remove_diacritics 2', prefix='2 3')")
            n_fts = c.execute("SELECT COUNT(*) FROM packages_fts").fetchone()[0]
            n_pkg = c.execute("SELECT COUNT(*) FROM packages").fetchone()[0]
            if n_fts != n_pkg:
                c.execute("DELETE FROM packages_fts")
                c.execute("INSERT INTO packages_fts(rowid,name,description,tags,body) SELECT id,name,description,tags,readme FROM packages")
            c.commit()
        except sqlite3.OperationalError:
            self.fts = False


def init_db(settings):
    return DB(settings)
