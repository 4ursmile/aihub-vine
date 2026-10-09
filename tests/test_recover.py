import os
import subprocess
import tempfile
import time
import unittest

from aihub.server import recover as R
from aihub.server.backup import Backup
from aihub.server.config import Settings
from aihub.server.db import init_db
from aihub.server.repos import Repos
from aihub.server.sync import Sync


def make(t, name, bare):
    s = Settings.load(env={}, env_file=os.path.join(t, "none.env"), data_dir=os.path.join(t, name), sync_enabled=False)
    r = Repos(init_db(s))
    r.set_setting("index_url", bare)
    return s, r


def add_events(r, n, t0):
    c = r.db.conn()
    for i in range(n):
        c.execute("INSERT INTO events(ts,kind,package) VALUES(?,?,?)", (t0 + i, "use", "p"))
    c.commit()


class Recover(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        cwd = os.getcwd()
        os.chdir(self.t)
        self.addCleanup(os.chdir, cwd)
        self.bare = os.path.join(self.t, "idx.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.bare], check=True)

    def backup(self, s, r):
        return Backup(r, s, Sync(r, s)).run_once()

    def count(self, r):
        return r.db.conn().execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def test_lost_data_dir_is_restored(self):
        s, r = make(self.t, "a", self.bare)
        add_events(r, 30, time.time() - 100)
        self.backup(s, r)
        s2, r2 = make(self.t, "b", self.bare)               # new machine: empty database, same git remote
        r2.db.close()
        self.assertEqual(R.run(s2, "ask", interactive=False), "remote")
        r3 = Repos(init_db(s2))
        self.assertEqual(self.count(r3), 30)
        self.assertEqual(r3.db.conn().execute("SELECT MAX(id) FROM events").fetchone()[0], 30)

    def test_empty_database_never_overwrites_backup(self):
        s, r = make(self.t, "a", self.bare)
        add_events(r, 5, time.time())
        self.backup(s, r)
        s2, r2 = make(self.t, "b", self.bare)
        with self.assertRaises(RuntimeError):
            self.backup(s2, r2)

    def test_local_ahead_keeps_local_and_remote_ahead_is_a_choice(self):
        s, r = make(self.t, "a", self.bare)
        add_events(r, 5, time.time() - 1000)
        self.backup(s, r)
        add_events(r, 5, time.time())                         # local is newer
        self.assertEqual(R.run(s, "ask", interactive=False), "none")
        self.assertEqual(self.count(r), 10)
        s2, r2 = make(self.t, "b", self.bare)                 # other copy with older, different data
        add_events(r2, 2, time.time() - 5000)
        r2.db.close()
        self.assertEqual(R.run(s2, "ask", interactive=False), "local")        # no terminal: nothing overwritten
        self.assertEqual(R.run(s2, "latest", interactive=False), "remote")    # backup is the newer one
        r3 = Repos(init_db(s2))
        self.assertEqual(self.count(r3), 5)
        saved = [d for d in os.listdir(s2.data_dir) if d.startswith("pre-recover-")]
        self.assertEqual(len(saved), 1)

    def test_secret_setting_survives_restore(self):
        s, r = make(self.t, "a", self.bare)
        r.set_setting("langfuse_secret_key", "sk-lf-supersecret1234567890")
        add_events(r, 3, time.time())
        self.backup(s, r)
        s2, r2 = make(self.t, "b", self.bare)
        r2.set_setting("langfuse_secret_key", "sk-lf-localvalue123456789")
        r2.db.close()
        R.run(s2, "remote", interactive=False)
        r3 = Repos(init_db(s2))
        self.assertEqual(r3.setting("langfuse_secret_key"), "sk-lf-localvalue123456789")


if __name__ == "__main__":
    unittest.main()
