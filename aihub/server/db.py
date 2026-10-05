"""SQLite engine + schema. Only repos.py should use get_conn()."""
import os
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT,
  role TEXT NOT NULL DEFAULT 'user', status TEXT NOT NULL DEFAULT 'active', auth_provider TEXT DEFAULT 'password',
  external_id TEXT, created REAL);
CREATE TABLE IF NOT EXISTS avatars(user_id INTEGER PRIMARY KEY, mime TEXT, data BLOB);
CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT DEFAULT '', created REAL);
CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER, user_id INTEGER, PRIMARY KEY(group_id,user_id));
CREATE INDEX IF NOT EXISTS gm_user ON group_members(user_id);
-- principal_type: 'user' | 'group'. access: 'view' (see+install) | 'develop' (also publish new versions)
CREATE TABLE IF NOT EXISTS package_access(package_id INTEGER, principal_type TEXT, principal_id INTEGER, access TEXT,
  PRIMARY KEY(package_id,principal_type,principal_id));
CREATE TABLE IF NOT EXISTS roles(name TEXT PRIMARY KEY, description TEXT DEFAULT '', builtin INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS role_permissions(role TEXT, permission TEXT, PRIMARY KEY(role,permission));
CREATE TABLE IF NOT EXISTS tokens(hash TEXT PRIMARY KEY, user_id INTEGER, kind TEXT, name TEXT, created REAL);
CREATE TABLE IF NOT EXISTS packages(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, type TEXT, description TEXT,
  tags TEXT DEFAULT '', latest_version TEXT, hidden INTEGER DEFAULT 0, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS package_maintainers(package_id INTEGER, user_id INTEGER, role TEXT, PRIMARY KEY(package_id,user_id));
CREATE TABLE IF NOT EXISTS versions(id INTEGER PRIMARY KEY, package_id INTEGER, version TEXT, file TEXT, sha256 TEXT,
  size INTEGER, manifest TEXT, downloads INTEGER DEFAULT 0, yanked INTEGER DEFAULT 0, signature TEXT, created REAL,
  UNIQUE(package_id,version));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL, kind TEXT, package TEXT, version TEXT,
  client_id TEXT, username TEXT, component TEXT, duration REAL);
CREATE INDEX IF NOT EXISTS ev_pkg ON events(package,ts);
CREATE TABLE IF NOT EXISTS reviews(package_id INTEGER, user_id INTEGER, rating INTEGER, body TEXT, created REAL,
  PRIMARY KEY(package_id,user_id));
CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY, ts REAL, actor TEXT, action TEXT, target TEXT, detail TEXT);
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
}
PERMS = {
    "admin": list(ALL_PERMS),
    "publisher": ["publish", "review"],
    "user": ["publish", "review"],
}
DESCR = {"admin": "Full access", "publisher": "Can publish and review", "user": "Default role for new accounts"}


class DB:
    def __init__(self, path):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._local = threading.local()
        c = self.conn()
        c.executescript(SCHEMA)
        ucols = [r[1] for r in c.execute("PRAGMA table_info(users)")]
        for col, ddl in (("display_name", "TEXT DEFAULT ''"), ("title", "TEXT DEFAULT ''"), ("avatar_v", "INTEGER DEFAULT 0")):
            if col not in ucols:
                c.execute("ALTER TABLE users ADD COLUMN %s %s" % (col, ddl))
        if "owner_id" not in [r[1] for r in c.execute("PRAGMA table_info(groups)")]:
            c.execute("ALTER TABLE groups ADD COLUMN owner_id INTEGER")
        pk_cols = [r[1] for r in c.execute("PRAGMA table_info(packages)")]
        if "visibility" not in pk_cols:   # existing packages stay public: nothing disappears on upgrade
            c.execute("ALTER TABLE packages ADD COLUMN visibility TEXT NOT NULL DEFAULT 'public'")
        cols = [r[1] for r in c.execute("PRAGMA table_info(packages)")]
        if "readme" not in cols:
            c.execute("ALTER TABLE packages ADD COLUMN readme TEXT DEFAULT ''")
        self.fts = True
        try:
            c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS packages_fts USING fts5("
                      "name, description, tags, body, tokenize='unicode61 remove_diacritics 2', prefix='2 3')")
            n_fts = c.execute("SELECT COUNT(*) FROM packages_fts").fetchone()[0]
            n_pkg = c.execute("SELECT COUNT(*) FROM packages").fetchone()[0]
            if n_fts != n_pkg:  # (re)build index
                c.execute("DELETE FROM packages_fts")
                c.execute("INSERT INTO packages_fts(rowid,name,description,tags,body) "
                          "SELECT id,name,description,tags,readme FROM packages")
        except sqlite3.OperationalError:
            self.fts = False  # SQLite built without FTS5 -> LIKE fallback
        ecols = [r[1] for r in c.execute("PRAGMA table_info(events)")]
        if "source" not in ecols:
            c.execute("ALTER TABLE events ADD COLUMN source TEXT")
        # Seed defaults once. Afterwards admins own the matrix: only the admin role is kept complete.
        if not c.execute("SELECT 1 FROM app_settings WHERE key='perms_seeded'").fetchone():
            for r, ps in PERMS.items():
                c.execute("INSERT OR IGNORE INTO roles VALUES(?,?,1)", (r, DESCR[r]))
                for p in ps:
                    c.execute("INSERT OR IGNORE INTO role_permissions VALUES(?,?)", (r, p))
            c.execute("INSERT INTO app_settings VALUES('perms_seeded','1')")
        for p in ALL_PERMS:
            c.execute("INSERT OR IGNORE INTO role_permissions VALUES('admin',?)", (p,))
        c.commit()

    def conn(self):
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            self._local.c = c
        return c


def init_db(settings) -> DB:
    return DB(os.path.join(settings.data_dir, "aihub.db"))
