import gzip
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from aihub.cli import gitx
from aihub.server import backup as B
from aihub.server.backup import Backup
from aihub.server.config import Settings
from aihub.server.db import init_db
from aihub.server.repos import Repos
from aihub.server.sync import Sync


class Scrub(unittest.TestCase):
    def test_credentials_are_hashed(self):
        u = B.scrub("users", {"password_hash": "plain"})
        self.assertTrue(u["password_hash"].startswith("sha256:"))
        kept = "pbkdf2$ab$cd"
        self.assertEqual(B.scrub("users", {"password_hash": kept})["password_hash"], kept)
        s = B.scrub("app_settings", {"key": "telemetry_secret_key", "value": "sk-tel-x"})
        self.assertTrue(s["value"].startswith("sha256:"))
        self.assertEqual(B.scrub("app_settings", {"key": "site_name", "value": "Hub"})["value"], "Hub")
        self.assertIn("[redacted]", B.scrub("events", {"detail": "token=abc123"})["detail"])


class Run(unittest.TestCase):
    def test_split_incremental_and_clean(self):
        with tempfile.TemporaryDirectory() as t:
            cwd = os.getcwd()
            os.chdir(t)                            # so Settings.load() cannot pick up the repo's .env
            self.addCleanup(os.chdir, cwd)
            bare = os.path.join(t, "idx.git")
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare], check=True)
            s = Settings.load(env={}, env_file=os.path.join(t, "none.env"), data_dir=os.path.join(t, "data"), sync_enabled=False)   # isolated; cwd is a temp dir (see setUp)
            r = Repos(init_db(s))
            r.set_setting("index_url", bare)
            r.set_setting("telemetry_secret_key", "sk-tel-supersecret1234567890")
            r.set_setting("backup_events_chunk", "1000")
            c = r.db.conn()
            for i in range(2500):
                c.execute("INSERT INTO events(ts,kind,package,detail) VALUES(?,?,?,?)", (i, "use", "p", "password=hunter2"))
            c.commit()
            b = Backup(r, s, Sync(r, s))
            first = b.run_once()
            self.assertEqual(b.run_once()["last_commit"], first["last_commit"])      # idle database: no new commit
            chk = os.path.join(t, "chk")
            subprocess.run(["git", "clone", "-q", bare, chk], check=True)
            idx = json.load(open(os.path.join(chk, "backups", "index.json")))
            self.assertEqual(sorted(e["rows"] for e in idx["files"] if e["table"] == "events"), [501, 999, 1000])
            raw = b""
            for e in idx["files"]:
                raw += gzip.open(os.path.join(chk, e["path"])).read()
            self.assertNotIn(b"hunter2", raw)
            self.assertNotIn(b"supersecret", raw)
            tmp = os.path.join(t, "x.db")
            open(tmp, "wb").write(gzip.open(os.path.join(chk, "backups/db/app_settings.sqlite.gz")).read())
            rows = dict(sqlite3.connect(tmp).execute("SELECT key,value FROM app_settings").fetchall())
            self.assertTrue(rows["telemetry_secret_key"].startswith("sha256:"))


class Remote(unittest.TestCase):
    """The checkout step must tell "empty remote" from "remote that did not answer"."""

    def setUp(self):
        self.t = tempfile.mkdtemp()
        cwd = os.getcwd()
        os.chdir(self.t)
        self.addCleanup(os.chdir, cwd)
        self.old = (gitx.LOGIN, gitx.BATCH, list(gitx.TRUSTED))
        self.addCleanup(self._restore)
        self.s = Settings.load(env={}, env_file=os.path.join(self.t, "none.env"), data_dir=os.path.join(self.t, "data"), sync_enabled=False)
        self.r = Repos(init_db(self.s))
        self.b = Backup(self.r, self.s, Sync(self.r, self.s))
        self.b.stop.set = self.b.stop.set                       # (the event stays usable; sleeps below are patched)
        p = mock.patch.object(B, "RETRY_PAUSE", (0.0, 0.0))
        p.start()
        self.addCleanup(p.stop)

    def _restore(self):
        gitx.LOGIN, gitx.BATCH = self.old[0], self.old[1]
        gitx.TRUSTED[:] = self.old[2]

    def bare(self, name="idx.git"):
        path = os.path.join(self.t, name)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", path], check=True)
        return path

    def test_unreachable_remote_is_an_error_not_an_empty_repo(self):
        missing = os.path.join(self.t, "nope.git")
        self.r.set_setting("index_url", missing)
        with self.assertRaises(Exception) as cm:
            self.b.run_once()
        self.assertTrue(str(cm.exception).strip())
        self.assertNotIn("orphan", str(cm.exception))
        work = os.path.join(self.s.data_dir, "backup-repos")
        for d in (os.listdir(work) if os.path.isdir(work) else []):
            self.assertFalse(os.path.isdir(os.path.join(work, d, ".git")), "an orphan checkout was created for an unreachable remote")
        self.assertTrue(self.b.status["last_error"])

    def test_timeout_is_reported(self):
        with mock.patch.object(gitx, "_ls", return_value=None):
            with self.assertRaises(gitx.GitError) as cm:
                Backup._checkout("https://u:tok@git.example.com/x.git", "main", self.t)
        self.assertIn("timed out", str(cm.exception))
        self.assertNotIn("tok", str(cm.exception))

    def test_empty_remote_gets_the_first_backup(self):
        bare = self.bare()
        self.r.set_setting("index_url", bare)
        out = self.b.run_once()
        self.assertTrue(out["last_commit"])
        self.assertEqual(subprocess.run(["git", "-C", bare, "rev-parse", "--verify", "refs/heads/main"], capture_output=True).returncode, 0)

    def test_remote_with_other_branch_only_gets_an_orphan_branch(self):
        bare = self.bare()
        w = os.path.join(self.t, "w")
        subprocess.run(["git", "init", "-q", "-b", "other", w], check=True)
        subprocess.run(["git", "-C", w, "-c", "user.name=a", "-c", "user.email=a@b", "commit", "-q", "--allow-empty", "-m", "x"], check=True)
        subprocess.run(["git", "-C", w, "push", "-q", bare, "other"], check=True)
        self.r.set_setting("index_url", bare)
        self.b.run_once()
        self.assertEqual(subprocess.run(["git", "-C", bare, "rev-parse", "--verify", "refs/heads/main"], capture_output=True).returncode, 0)

    def test_login_error_is_retried_once_then_raised_with_the_cause(self):
        calls = []

        def boom(*a, **k):
            calls.append(1)
            raise gitx.GitError("fatal: Authentication failed for 'https://git.example.com/x.git/'")
        with mock.patch.object(Backup, "_attempt", side_effect=boom):
            with self.assertRaises(gitx.GitError) as cm:
                self.b._backup("u", "main")
        self.assertEqual(len(calls), 2)
        self.assertIn("Authentication failed", str(cm.exception))

    def test_push_race_is_retried_at_once_up_to_three_times(self):
        calls = []

        def race(*a, **k):
            calls.append(1)
            if len(calls) < 3:
                raise gitx.GitError("! [rejected]        HEAD -> main (fetch first)")
            return {"files": 1, "rows": 1, "last_commit": "abc"}
        with mock.patch.object(Backup, "_attempt", side_effect=race), mock.patch.object(self.b.stop, "wait", side_effect=AssertionError("no pause for a race")):
            self.assertEqual(self.b._backup("u", "main")["last_commit"], "abc")
        self.assertEqual(len(calls), 3)

    def test_remote_rejected_is_not_a_race(self):
        calls = []

        def hook(*a, **k):
            calls.append(1)
            raise gitx.GitError("! [remote rejected] HEAD -> main (pre-receive hook declined)")
        with mock.patch.object(Backup, "_attempt", side_effect=hook):
            with self.assertRaises(gitx.GitError):
                self.b._backup("u", "main")
        self.assertEqual(len(calls), 2)                  # one pause-retry, not the race loop

    def test_back_up_now_waits_for_a_running_backup(self):
        self.r.set_setting("index_url", self.bare())
        self.b.lock.acquire()
        self.b.status.update(last_error=None, files=7, rows=9, last_commit="deadbee")
        threading.Timer(0.3, self.b.lock.release).start()
        t0 = time.time()
        out = self.b.run_once()                          # from the route (not scheduled): waits, then answers with the finished status
        self.assertGreaterEqual(time.time() - t0, 0.25)
        self.assertEqual(out["last_commit"], "deadbee")

    def test_scheduled_run_does_not_wait(self):
        self.b.lock.acquire()
        self.addCleanup(self.b.lock.release)
        with self.assertRaises(RuntimeError):
            self.b.run_once(scheduled=True)


if __name__ == "__main__":
    unittest.main()
