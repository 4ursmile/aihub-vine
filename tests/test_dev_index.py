import os
import subprocess
import tempfile
import unittest
from unittest import mock

from aihub.cli import api, main
from aihub.server import db, sync as syncmod
from aihub.server.sync import Sync

TOML = '[package]\nname = "idx-pkg"\nversion = "1.0.0"\ntype = "skill"\ndescription = "d"\ntags = []\n'


def git(*a, cwd=None):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True, capture_output=True)


class Perm(unittest.TestCase):
    def test_permission_exists_and_only_admin_has_it_by_default(self):
        self.assertIn("index", db.ALL_PERMS)
        self.assertIn("index", db.PERMS["admin"])
        for r in ("publisher", "user"):
            self.assertNotIn("index", db.PERMS[r])


class Force(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.pkg, self.remote, self.data = [os.path.join(self.t, x) for x in ("pkg", "remote", "data")]
        os.makedirs(self.pkg); os.makedirs(self.data)
        open(os.path.join(self.pkg, "aihub.toml"), "w").write(TOML)
        git("init", "-q", "-b", "main", cwd=self.pkg); git("add", "-A", cwd=self.pkg); git("commit", "-q", "-m", "p", cwd=self.pkg)
        self.commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.pkg, capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.remote], check=True)
        seed = os.path.join(self.t, "seed"); os.makedirs(seed)
        git("init", "-q", "-b", "main", cwd=seed); open(os.path.join(seed, "R"), "w").write("x")
        git("add", "-A", cwd=seed); git("commit", "-q", "-m", "s", cwd=seed); git("push", "-q", self.remote, "main", cwd=seed)
        remote, data = self.remote, self.data

        class S:
            data_dir = data
            def env_get(self, k): return {"AIHUB_INDEX_URL": "file://" + remote}.get(k, "")
        class Repos:
            def setting(self, k, d=""): return d
        self.sy = Sync(Repos(), S())
        self.ev = {"package": "idx-pkg", "version": "1.0.0", "git_url": "file://" + self.pkg, "commit": self.commit}

    def test_force_reindexes_an_existing_version(self):
        with mock.patch.object(syncmod, "SCHEMES", ("file://",)):
            res = []
            self.assertTrue(self.sy.update_index([self.ev], results=res))
            self.assertEqual(res[-1][2], "indexed")
            res = []
            self.assertFalse(self.sy.update_index([self.ev], results=res))          # event path: skipped
            self.assertEqual(res[-1][2], "already indexed")
            open(os.path.join(self.pkg, "README.md"), "w").write("new readme\n")
            git("add", "-A", cwd=self.pkg); git("commit", "-q", "-m", "r", cwd=self.pkg)
            ev2 = dict(self.ev, commit=subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.pkg, capture_output=True, text=True).stdout.strip())
            res = []
            self.assertTrue(self.sy.update_index([ev2], force=True, results=res))     # forced: new ref + readme written
            self.assertEqual(res[-1][2], "indexed")

    def test_mismatch_is_reported_not_indexed(self):
        with mock.patch.object(syncmod, "SCHEMES", ("file://",)):
            res = []
            self.assertFalse(self.sy.update_index([dict(self.ev, version="9.9.9")], force=True, results=res))
            self.assertIn("does not match", res[-1][2])


class Cli(unittest.TestCase):
    def test_command_registered(self):
        a = main.build_parser().parse_args(["dev", "index", "--commit", "abc123"])
        self.assertIs(a.fn, main.dev_index)
        self.assertEqual(a.commit, "abc123")

    def test_403_gets_a_clear_message(self):
        a = main.build_parser().parse_args(["dev", "index"])
        with tempfile.TemporaryDirectory() as t:
            open(os.path.join(t, "aihub.toml"), "w").write(TOML + '\n[git]\nurl = "https://github.com/o/r"\nbranch = "main"\n')
            git("init", "-q", "-b", "main", cwd=t); git("add", "-A", cwd=t); git("commit", "-q", "-m", "x", cwd=t)
            a.path = t
            with mock.patch.object(main.gitx, "run", side_effect=lambda args, cwd=None, check=True: "abc" if args[0] == "rev-parse" and "--show-toplevel" not in args else ("origin/main" if args[0] == "branch" else "")), \
                 mock.patch.object(main.pub, "toplevel", return_value=t), \
                 mock.patch.object(main.api, "call", side_effect=api.ApiError("403 /index/package: missing permission: index")):
                with self.assertRaises(api.ApiError) as cm:
                    main.dev_index(a)
            self.assertIn("'index' permission", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
