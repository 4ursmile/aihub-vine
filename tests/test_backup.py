import gzip
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest

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
        s = B.scrub("app_settings", {"key": "langfuse_secret_key", "value": "sk-lf-x"})
        self.assertTrue(s["value"].startswith("sha256:"))
        self.assertEqual(B.scrub("app_settings", {"key": "site_name", "value": "Hub"})["value"], "Hub")
        self.assertIn("[redacted]", B.scrub("events", {"detail": "token=abc123"})["detail"])


class Run(unittest.TestCase):
    def test_split_incremental_and_clean(self):
        with tempfile.TemporaryDirectory() as t:
            bare = os.path.join(t, "idx.git")
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare], check=True)
            s = Settings.load(data_dir=os.path.join(t, "data"), sync_enabled=False)
            r = Repos(init_db(s))
            r.set_setting("index_url", bare)
            r.set_setting("langfuse_secret_key", "sk-lf-supersecret1234567890")
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
            self.assertTrue(rows["langfuse_secret_key"].startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
