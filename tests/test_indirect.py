import os
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from aihub.cli import api, registry
from aihub.server import db
from aihub.server.config import Settings
from aihub.server.main import create_app

TOML = '[package]\nname = "demo-pkg"\nversion = "1.0.0"\ntype = "skill"\ndescription = "d"\n'


def git(*a, cwd=None):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True, capture_output=True)


class Permission(unittest.TestCase):
    def test_default_on_for_everyone_but_anonymous(self):
        self.assertIn("indirect", db.ALL_PERMS)
        for role in ("admin", "publisher", "user"):
            self.assertIn("indirect", db.PERMS[role])
        c = TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=tempfile.mkdtemp())))
        repos = c.app.state.repos
        self.assertIn("indirect", repos.perms("user"))
        self.assertNotIn("indirect", repos.perms("anonymous"))


class Bundle(unittest.TestCase):
    def test_hub_fetches_caches_and_sends(self):
        with tempfile.TemporaryDirectory() as t:
            pkg = os.path.join(t, "pkg")
            os.makedirs(pkg)
            open(os.path.join(pkg, "aihub.toml"), "w").write(TOML)
            git("init", "-q", "-b", "main", cwd=pkg); git("add", "-A", cwd=pkg); git("commit", "-q", "-m", "p", cwd=pkg)
            commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=pkg, capture_output=True, text=True).stdout.strip()
            data = os.path.join(t, "data")
            c = TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=data)))
            rp = c.app.state.repos
            pid = rp.package_upsert("demo-pkg", "skill", "d", [], "")
            rp.version_sync(pid, "1.0.0", {"package": {"name": "demo-pkg", "version": "1.0.0"}, "repo": {"url": "file://" + pkg, "branch": "main", "ref": commit}})
            url = "/api/v1/packages/demo-pkg/versions/1.0.0/bundle"
            self.assertEqual(c.get(url).status_code, 401)                              # anonymous holds no 'indirect'
            from aihub.server import routers
            # allow the call by granting the permission to anonymous directly in the DB for this test only
            conn = rp.db.conn()
            conn.execute("INSERT OR IGNORE INTO role_permissions VALUES('anonymous','indirect')"); conn.commit()
            with mock.patch.object(routers.gitx, "fetch", wraps=routers.gitx.fetch) as f:
                r1 = c.get(url)
                self.assertEqual(r1.status_code, 200, r1.text)
                self.assertTrue(r1.headers["X-Bundle-Sha256"])
                r2 = c.get(url)
                self.assertEqual(r2.content, r1.content)
                self.assertEqual(f.call_count, 1)                                      # second request came from the cache
            out = os.path.join(t, "b.tar.gz")
            open(out, "wb").write(r1.content)
            with tarfile.open(out) as tf:
                self.assertTrue(any(n.endswith("aihub.toml") for n in tf.getnames()))
                self.assertFalse(any(".git/" in n or n.startswith(".git") for n in tf.getnames()))
            self.assertEqual(c.get("/api/v1/packages/demo-pkg/versions/9.9.9/bundle").status_code, 404)


class Monorepo(unittest.TestCase):
    def test_packages_of_one_repo_cost_one_git_fetch(self):
        with tempfile.TemporaryDirectory() as t:
            mono = os.path.join(t, "mono")
            for n in ("pkg-a", "pkg-b"):
                os.makedirs(os.path.join(mono, n))
                open(os.path.join(mono, n, "aihub.toml"), "w").write(TOML.replace("demo-pkg", n))
            git("init", "-q", "-b", "main", cwd=mono); git("add", "-A", cwd=mono); git("commit", "-q", "-m", "p", cwd=mono)
            commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=mono, capture_output=True, text=True).stdout.strip()
            from aihub.server import routers
            data = os.path.join(t, "data")
            with mock.patch.object(routers.gitx, "fetch", wraps=routers.gitx.fetch) as f:
                for n in ("pkg-a", "pkg-b"):
                    path = routers._bundle_build(data, n, "1.0.0", {"url": "file://" + mono, "branch": "main", "ref": commit, "subdir": n})
                    with tarfile.open(path) as tf:
                        names = tf.getnames()
                    self.assertTrue(any(x.endswith("aihub.toml") for x in names))
                    self.assertFalse(any(".aihub-ready" in x or ".git" in x for x in names))
                    self.assertFalse(any("pkg-" in x and n not in x for x in names))      # only its own folder
                self.assertEqual(f.call_count, 1)


class CliSide(unittest.TestCase):
    def test_flag_is_wired(self):
        from aihub.cli import main
        p = main.build_parser() if hasattr(main, "build_parser") else None
        if p is None:
            self.skipTest("no build_parser")
        self.assertTrue(p.parse_args(["install", "x", "-i"]).indirect)
        self.assertFalse(p.parse_args(["install", "x"]).indirect)

    def test_forbidden_gives_a_clear_message(self):
        def boom(*a, **k):
            raise api.ApiError("403 /packages/x/versions/1/bundle: missing permission: indirect")
        with mock.patch.object(api, "_req", boom), mock.patch.object(api.time, "sleep"):
            with self.assertRaises(api.ApiError) as cm:
                registry.checkout_via_hub({"name": "x", "version": "1", "repo": {}}, tempfile.mkdtemp() + "/d")
        self.assertIn("indirect", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
