"""Backend tests against REAL servers (redis-server, minio, PostgreSQL). Each class skips cleanly if its binary is absent.
Run:  python -m unittest tests.test_backends -v
"""
import io
import os
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
import unittest

from aihub.server.config import Settings, parse_env_file


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def wait_port(port, secs=15):
    end = time.time() + secs
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 0.3).close(); return True
        except OSError:
            time.sleep(0.2)
    return False


class ConfigTests(unittest.TestCase):
    def test_env_file_precedence_and_secret_files(self):
        d = tempfile.mkdtemp()
        open(d + "/.env", "w").write("# x\nAIHUB_DB_BACKEND=postgres\nexport AIHUB_PG_PASSWORD='p@ss w0rd'\nAIHUB_PG_HOST=file-host # c\nAIHUB_OPEN_REGISTRATION=no\n")
        open(d + "/pw", "w").write("from-secret-file\n")
        s = Settings.load(env={"AIHUB_PG_HOST": "env-host"}, env_file=d + "/.env")
        self.assertEqual((s.db_backend, s.pg_host, s.pg_password, s.open_registration), ("postgres", "env-host", "p@ss w0rd", False))
        self.assertIn("p%40ss%20w0rd@env-host", s.postgres_dsn)
        s = Settings.load(env={"AIHUB_DB_BACKEND": "postgres", "AIHUB_PG_PASSWORD_FILE": d + "/pw"}, env_file="/nope")
        self.assertEqual(s.pg_password, "from-secret-file")
        self.assertNotIn("from-secret-file", str(s.describe()))
        self.assertNotIn("sekret", str(Settings.load(env={"AIHUB_CACHE_BACKEND": "redis", "AIHUB_REDIS_URL": "redis://u:sekret@h:1/0"}, env_file="/x").describe()))

    def test_invalid_config_fails_loudly(self):
        for env in ({"AIHUB_CACHE_BACKEND": "redis"}, {"AIHUB_STORAGE_BACKEND": "s3"}, {"AIHUB_DB_BACKEND": "mysql"}, {"AIHUB_PG_PORT": "x"}):
            with self.assertRaises(ValueError):
                Settings.load(env=env, env_file="/nope")


@unittest.skipUnless(shutil.which("redis-server"), "redis-server not installed")
class RedisCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # free_port() releases the port before redis binds it, so another process can grab it in between: retry on a new port
        for _ in range(5):
            cls.port = free_port()
            cls.p = subprocess.Popen(["redis-server", "--port", str(cls.port), "--save", "", "--appendonly", "no"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if wait_port(cls.port) and cls.p.poll() is None:
                return
            cls.p.terminate()
        raise AssertionError("redis did not start after 5 attempts")

    @classmethod
    def tearDownClass(cls):
        cls.p.terminate(); cls.p.wait()

    def make(self, prefix="t:"):
        from aihub.server.cache import RedisCache
        return RedisCache("redis://127.0.0.1:%d/0" % self.port, prefix)

    def test_roundtrip_ttl_and_prefix_invalidation(self):
        c = self.make("a:")
        c.set("pkg:list:1", {"x": [1, 2]}, ttl=30); c.set("dash:1", {"d": 1}, ttl=30)
        self.assertEqual(c.get("pkg:list:1"), {"x": [1, 2]})
        c.invalidate("pkg")
        self.assertIsNone(c.get("pkg:list:1"))                     # invalidated
        self.assertEqual(c.get("dash:1"), {"d": 1})                # other groups untouched
        c.set("k:1", 1, ttl=1); time.sleep(1.3); self.assertIsNone(c.get("k:1"))

    def test_invalidation_is_visible_to_other_workers(self):
        a, b = self.make("shared:"), self.make("shared:")          # two independent clients = two workers
        a.set("pkg:x", "old", ttl=60); self.assertEqual(b.get("pkg:x"), "old")
        b.invalidate("pkg"); self.assertIsNone(a.get("pkg:x"))     # the other worker sees the invalidation immediately

    def test_survives_redis_outage_as_cache_miss(self):
        port = free_port()
        p = subprocess.Popen(["redis-server", "--port", str(port), "--save", ""], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_port(port)
        from aihub.server.cache import RedisCache
        c = RedisCache("redis://127.0.0.1:%d/0" % port, "o:"); c.set("a:1", 1)
        p.terminate(); p.wait()
        t = time.time()
        self.assertIsNone(c.get("a:1")); c.set("a:2", 2); c.invalidate("a")      # none of these may raise
        self.assertLess(time.time() - t, 6)
        self.assertFalse(c.health()); self.assertGreater(c.errors, 0)

    def test_bad_url_fails_at_startup(self):
        from aihub.server.cache import RedisCache
        with self.assertRaises(Exception):
            RedisCache("redis://127.0.0.1:%d/0" % free_port(), "x:")

    def test_full_app_with_redis_cache_and_cross_worker_invalidation(self):
        from fastapi.testclient import TestClient
        from aihub.server.main import create_app
        d = tempfile.mkdtemp()
        mk = lambda: TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=d, cache_backend="redis", redis_url="redis://127.0.0.1:%d/0" % self.port, redis_prefix="app:")))
        w1, w2 = mk(), mk()     # two app instances sharing one database + one Redis = two workers
        h = lambda c: {"Authorization": "Bearer " + c.post("/api/v1/auth/login", json={"username": "root", "password": "secret1"}).json()["token"]}
        w1.post("/api/v1/auth/register", json={"username": "root", "password": "secret1"})
        H1, H2 = h(w1), h(w2)
        self.assertEqual(w2.get("/api/v1/packages", headers=H2).json()["total"], 0)       # w2 caches the empty list
        r = w1.app.state.repos
        pid = r.package_upsert("cc", "skill", "d", [], "")
        r.version_sync(pid, "1.0.0", {"package": {"name": "cc", "version": "1.0.0", "type": "skill"}})
        w1.app.state.cache.invalidate("pkg")                                                             # what the index sync does after a change
        self.assertEqual(w2.get("/api/v1/packages", headers=H2).json()["total"], 1)       # w2 must NOT serve its stale cached list



try:
    import pixeltable_pgserver as _pgs
except ImportError:
    _pgs = None


@unittest.skipUnless(_pgs, "pixeltable-pgserver (bundled PostgreSQL) not installed")
class PostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = _pgs.get_server(tempfile.mkdtemp(), cleanup_mode="stop"); cls.uri = cls.srv.get_uri()

    @classmethod
    def tearDownClass(cls):
        cls.srv.cleanup()

    def test_full_app_on_postgres(self):
        from fastapi.testclient import TestClient
        from aihub.server.main import create_app
        c = TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=tempfile.mkdtemp(), db_backend="postgres", database_url=self.uri)))
        c.post("/api/v1/auth/register", json={"username": "root", "password": "secret1"})
        H = {"Authorization": "Bearer " + c.post("/api/v1/auth/login", json={"username": "root", "password": "secret1"}).json()["token"]}
        r = c.app.state.repos
        pid = r.package_upsert("pgpkg", "skill", "invoices tool", ["fin"], "")
        r.version_sync(pid, "1.0.0", {"package": {"name": "pgpkg", "version": "1.0.0", "type": "skill"}})
        self.assertEqual(c.post("/api/v1/auth/register", json={"username": "root", "password": "secret1"}).status_code, 409)
        self.assertEqual(c.get("/api/v1/packages?q=invoice", headers=H).json()["total"], 1)       # full text, prefix
        self.assertEqual(c.get("/api/v1/packages?q=nomatch", headers=H).json()["total"], 0)
        self.assertEqual(c.post("/api/v1/packages/pgpkg/reviews", json={"rating": 5}, headers=H).status_code, 200)
        self.assertEqual(c.get("/api/v1/packages?sort=rating", headers=H).json()["items"][0]["rating"], {"avg": 5.0, "count": 1})
        ev = {"ts": __import__("time").time(), "kind": "use", "package": "pgpkg", "client_id": "c", "username": None, "ext_id": "obs-1"}
        self.assertEqual(r.events_insert_ext([dict(ev)]), 1)
        self.assertEqual(r.events_insert_ext([dict(ev)]), 0)                      # same Langfuse observation twice: skipped
        self.assertEqual(c.get("/api/v1/dashboard?days=7", headers=H).json()["totals"], {"use": 1})
        self.assertEqual(c.get("/api/v1/readyz").json()["database"], "postgres")

    def test_two_app_instances_share_one_postgres_and_migrate_safely(self):
        from aihub.server.db import DB
        a = DB(Settings(db_backend="postgres", database_url=self.uri)); b = DB(Settings(db_backend="postgres", database_url=self.uri))   # re-running migrations is idempotent
        self.assertEqual(a.conn().execute("SELECT COUNT(*) FROM roles").fetchone()[0], b.conn().execute("SELECT COUNT(*) FROM roles").fetchone()[0])


if __name__ == "__main__":
    unittest.main()


class BuiltinPackageTest(unittest.TestCase):
    def test_bundled_packages_are_valid_and_seeded(self):
        import os, tempfile
        from aihub.core import manifest as M
        from aihub.core.release import BUILTIN_PACKAGES
        from aihub.server import builtin
        self.assertEqual(sorted(BUILTIN_PACKAGES), builtin.names())
        for n in builtin.names():
            root = os.path.join(builtin.DIR, n)
            m = M.parse(open(os.path.join(root, "aihub.toml")).read())
            errs, _ = M.lint(m, root)
            self.assertEqual(errs, [], n)
            self.assertTrue(os.path.isfile(os.path.join(root, "skills", n, "SKILL.md")))

