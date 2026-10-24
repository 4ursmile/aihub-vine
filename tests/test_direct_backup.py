"""Archives published with `aihub dev publish -i` live only in <data>/direct: the git backup must carry them and recovery must put them back."""
import hashlib
import json
import os
import subprocess
import tempfile
import time
import unittest

from aihub.server import recover as R
from aihub.server.backup import Backup
from aihub.server.direct import Direct
from aihub.server.repos import Repos
from aihub.server.db import init_db
from aihub.server.sync import Sync
from tests.test_direct_publish import make_tar
from tests.test_recover import add_events, make


def store(s, name, version, raw):
    d = Direct(s.data_dir)
    tmp = d.scratch()
    with open(tmp, "wb") as f:
        f.write(raw)
    d.put(tmp, name, version, hashlib.sha256(raw).hexdigest())
    return d


def list_version(r, name, version):
    c = r.db.conn()
    c.execute("INSERT INTO packages(name,type,description,latest_version) VALUES(?,?,?,?)", (name, "skill", "d", version))
    pid = c.execute("SELECT id FROM packages WHERE name=?", (name,)).fetchone()[0]
    c.execute("INSERT INTO versions(package_id,version) VALUES(?,?)", (pid, version))
    c.commit()


class DirectBackup(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        cwd = os.getcwd()
        os.chdir(self.t)
        self.addCleanup(os.chdir, cwd)
        self.bare = os.path.join(self.t, "idx.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.bare], check=True)
        self.s, self.r = make(self.t, "a", self.bare)
        add_events(self.r, 5, time.time() - 100)
        self.raw = make_tar()
        self.direct = store(self.s, "dp-demo", "1.0.0", self.raw)
        list_version(self.r, "dp-demo", "1.0.0")

    def backup(self):
        if not hasattr(self, "b"):
            self.b = Backup(self.r, self.s, Sync(self.r, self.s))       # one instance: it remembers the last commit
        return self.b.run_once()

    def show(self, path):
        return subprocess.run(["git", "--git-dir", self.bare, "show", "main:" + path], capture_output=True).stdout

    def index(self):
        return json.loads(self.show("backups/index.json"))

    def test_archive_goes_into_the_backup_byte_for_byte(self):
        out = self.backup()
        self.assertEqual(out["archives"], 1)
        self.assertEqual(self.show("backups/direct/dp-demo/1.0.0.tar.gz"), self.raw)
        e = self.index()["archives"][0]
        self.assertEqual((e["package"], e["version"], e["sha256"]), ("dp-demo", "1.0.0", hashlib.sha256(self.raw).hexdigest()))

    def test_unchanged_archives_make_no_new_commit_and_unpublished_ones_leave(self):
        first = self.backup()["last_commit"]
        self.assertEqual(self.backup()["last_commit"], first)
        self.direct.remove("dp-demo", "1.0.0")
        self.backup()
        self.assertEqual(self.show("backups/direct/dp-demo/1.0.0.tar.gz"), b"")
        self.assertNotIn("archives", self.index())

    def test_new_version_is_added_and_old_one_stays(self):
        self.backup()
        store(self.s, "dp-demo", "1.1.0", make_tar(version="1.1.0"))
        self.backup()
        self.assertEqual(sorted(e["version"] for e in self.index()["archives"]), ["1.0.0", "1.1.0"])

    def test_archive_over_the_admin_limit_is_skipped_and_listed_not_fatal(self):
        self.r.set_setting("backup_direct_max_mb", "1")
        store(self.s, "big", "1.0.0", make_tar(name="big", extra={"blob.bin": os.urandom(1_200_000).hex()}))
        out = self.backup()
        self.assertEqual((out["archives"], out["archives_skipped"]), (1, 1))
        self.assertEqual(self.index()["archives_skipped"][0]["package"], "big")
        self.assertEqual(self.show("backups/direct/big/1.0.0.tar.gz"), b"")

    def test_lost_data_dir_gets_database_and_archives_back(self):
        self.backup()
        s2, r2 = make(self.t, "b", self.bare)                     # new machine
        r2.db.close()
        self.assertEqual(R.run(s2, "ask", interactive=False), "remote")
        d2 = Direct(s2.data_dir)
        self.assertEqual(d2.sha("dp-demo", "1.0.0"), hashlib.sha256(self.raw).hexdigest())
        with open(d2.path("dp-demo", "1.0.0"), "rb") as f:
            self.assertEqual(f.read(), self.raw)

    def test_database_survives_but_data_folder_is_new_archives_are_filled_in(self):
        self.backup()
        self.direct.remove("dp-demo", "1.0.0")                    # e.g. Postgres kept, the server's disk was replaced
        self.assertEqual(R.run(self.s, "ask", interactive=False), "none")
        self.assertTrue(self.direct.sha("dp-demo", "1.0.0"))

    def test_restore_never_overwrites_and_only_restores_listed_versions_and_checks_digests(self):
        self.backup()
        d = os.path.join(self.t, "chk")
        subprocess.run(["git", "clone", "-q", self.bare, d], check=True)
        idx = self.index()
        # a version the database does not list is not brought back
        self.direct.remove("dp-demo", "1.0.0")
        self.r.db.conn().execute("DELETE FROM versions")
        self.r.db.conn().commit()
        self.assertEqual(R.restore_archives(self.r, d, idx, self.s.data_dir), (0, []))
        self.assertEqual(self.direct.sha("dp-demo", "1.0.0"), "")
        list_version_again = self.r.db.conn()
        list_version_again.execute("INSERT INTO versions(package_id,version) SELECT id,'1.0.0' FROM packages WHERE name='dp-demo'")
        list_version_again.commit()
        # a damaged backup copy is refused
        with open(os.path.join(d, "backups/direct/dp-demo/1.0.0.tar.gz"), "ab") as f:
            f.write(b"junk")
        done, problems = R.restore_archives(self.r, d, idx, self.s.data_dir)
        self.assertEqual(done, 0)
        self.assertIn("damaged", problems[0])
        self.assertEqual(self.direct.sha("dp-demo", "1.0.0"), "")
        # a local archive that differs is kept
        other = make_tar(extra={"x.txt": "local"})
        store(self.s, "dp-demo", "1.0.0", other)
        done, problems = R.restore_archives(self.r, d, idx, self.s.data_dir)
        self.assertEqual(done, 0)
        self.assertIn("kept the local one", problems[0])
        with open(self.direct.path("dp-demo", "1.0.0"), "rb") as f:
            self.assertEqual(f.read(), other)

    def test_a_broken_index_entry_cannot_stop_the_server_from_starting(self):
        d = tempfile.mkdtemp()
        self.assertEqual(R.restore_archives(self.r, d, {"archives": [{"package": "dp-demo"}, "junk", {"path": "../../etc/passwd", "package": "dp-demo",
                                            "version": "1.0.0", "sha256": "0" * 64}]}, self.s.data_dir)[0], 0)


if __name__ == "__main__":
    unittest.main()
