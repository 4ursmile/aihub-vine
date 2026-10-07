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
        mk = lambda: TestClient(create_app(Settings(data_dir=d, cache_backend="redis", redis_url="redis://127.0.0.1:%d/0" % self.port, redis_prefix="app:")))
        w1, w2 = mk(), mk()     # two app instances sharing one database + one Redis = two workers
        h = lambda c: {"Authorization": "Bearer " + c.post("/api/v1/auth/login", json={"username": "root", "password": "secret1"}).json()["token"]}
        w1.post("/api/v1/auth/register", json={"username": "root", "password": "secret1"})
        H1, H2 = h(w1), h(w2)
        self.assertEqual(w2.get("/api/v1/packages", headers=H2).json()["total"], 0)       # w2 caches the empty list
        toml = b'[package]\nname = "cc"\nversion = "1.0.0"\ntype = "skill"\ndescription = "d"\n'
        b = io.BytesIO()
        with tarfile.open(fileobj=b, mode="w:gz") as t:
            ti = tarfile.TarInfo("aihub.toml"); ti.size = len(toml); t.addfile(ti, io.BytesIO(toml))
        self.assertEqual(w1.post("/api/v1/upload", content=b.getvalue(), headers=H1).status_code, 200)     # w1 publishes
        self.assertEqual(w2.get("/api/v1/packages", headers=H2).json()["total"], 1)       # w2 must NOT serve its stale cached list


@unittest.skipUnless(shutil.which("minio"), "minio not installed")
class S3StorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = tempfile.mkdtemp()
        env = dict(os.environ, MINIO_ROOT_USER="testkey", MINIO_ROOT_PASSWORD="testsecret123")
        for _ in range(5):
            cls.port = free_port()
            cls.p = subprocess.Popen(["minio", "server", cls.data, "--address", "127.0.0.1:%d" % cls.port, "--console-address", "127.0.0.1:%d" % free_port()],
                                     env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if wait_port(cls.port, 30) and cls.p.poll() is None:
                break
            cls.p.terminate()
        else:
            raise AssertionError("minio did not start after 5 attempts")
        import boto3
        cls.endpoint = "http://127.0.0.1:%d" % cls.port
        boto3.client("s3", endpoint_url=cls.endpoint, aws_access_key_id="testkey", aws_secret_access_key="testsecret123", region_name="us-east-1").create_bucket(Bucket="aihub-test")

    @classmethod
    def tearDownClass(cls):
        cls.p.terminate(); cls.p.wait(); shutil.rmtree(cls.data, ignore_errors=True)

    def st(self, prefix="packages/"):
        from aihub.server.storage import S3Storage
        return S3Storage("aihub-test", "us-east-1", self.endpoint, "testkey", "testsecret123", prefix)

    def test_put_open_exists_size_delete_and_path_safety(self):
        st = self.st()
        f = tempfile.mktemp(); open(f, "wb").write(b"hello" * 1000)
        st.put("pkg-a", "pkg-a-1.0.0.tar.gz", f)
        self.assertFalse(os.path.exists(f))                                    # source consumed, like LocalStorage
        self.assertTrue(st.exists("pkg-a", "pkg-a-1.0.0.tar.gz")); self.assertFalse(st.exists("pkg-a", "nope.tgz"))
        self.assertEqual(st.size("pkg-a", "pkg-a-1.0.0.tar.gz"), 5000)
        with st.open("pkg-a", "pkg-a-1.0.0.tar.gz") as r:
            self.assertEqual(r.read(), b"hello" * 1000)
        with st.local_copy("pkg-a", "pkg-a-1.0.0.tar.gz") as p:
            self.assertEqual(open(p, "rb").read()[:5], b"hello")
        self.assertFalse(os.path.exists(p))                                    # temp copy cleaned up
        for bad in (("../x", "f"), ("a", "../../etc/passwd"), ("a/b", "f"), ("", "f"), ("a", "")):
            with self.assertRaises(ValueError): st.put(bad[0], bad[1], f)
            self.assertFalse(st.exists(*bad))
        st.delete("pkg-a", "pkg-a-1.0.0.tar.gz"); self.assertFalse(st.exists("pkg-a", "pkg-a-1.0.0.tar.gz"))

    def test_signed_url_downloads_and_wrong_credentials_fail(self):
        import urllib.request
        from aihub.server.storage import S3Storage
        st = self.st(); f = tempfile.mktemp(); open(f, "wb").write(b"payload"); st.put("pkg-b", "pkg-b-1.0.0.tar.gz", f)
        url = st.url("pkg-b", "pkg-b-1.0.0.tar.gz", expires=60)
        self.assertEqual(urllib.request.urlopen(url).read(), b"payload")
        self.assertIn("X-Amz-Signature", url)
        bad = S3Storage("aihub-test", "us-east-1", self.endpoint, "testkey", "WRONG", "packages/")
        self.assertFalse(bad.health())
        self.assertTrue(st.health())

    def test_full_app_on_s3_publish_search_readme_download(self):
        from fastapi.testclient import TestClient
        from aihub.server.main import create_app
        d = tempfile.mkdtemp()
        c = TestClient(create_app(Settings(data_dir=d, storage_backend="s3", s3_bucket="aihub-test", s3_endpoint=self.endpoint,
                                           s3_access_key="testkey", s3_secret_key="testsecret123", s3_prefix="app/", public_url="http://t")))
        c.post("/api/v1/auth/register", json={"username": "root", "password": "secret1"})
        H = {"Authorization": "Bearer " + c.post("/api/v1/auth/login", json={"username": "root", "password": "secret1"}).json()["token"]}
        toml = b'[package]\nname = "s3pkg"\nversion = "1.0.0"\ntype = "skill"\ndescription = "d"\n'
        b = io.BytesIO()
        with tarfile.open(fileobj=b, mode="w:gz") as t:
            for n, data in (("aihub.toml", toml), ("README.md", b"# from s3 readme")):
                ti = tarfile.TarInfo(n); ti.size = len(data); t.addfile(ti, io.BytesIO(data))
        self.assertEqual(c.post("/api/v1/upload", content=b.getvalue(), headers=H).status_code, 200)
        self.assertEqual(os.listdir(d).count("files"), 0 if not os.path.isdir(d + "/files") else len(os.listdir(d + "/files")) and 0)   # nothing stored on local disk
        self.assertIn("<h1>from s3 readme</h1>", c.get("/api/v1/packages/s3pkg/readme", headers=H).json()["html"])
        r = c.get("/files/s3pkg/s3pkg-1.0.0.tar.gz", headers=H, follow_redirects=False)
        self.assertEqual(r.status_code, 302); self.assertIn("X-Amz-Signature", r.headers["location"])           # signed redirect after the access check
        import urllib.request, hashlib
        body = urllib.request.urlopen(r.headers["location"]).read()
        self.assertEqual(hashlib.sha256(body).hexdigest(), c.get("/api/v1/resolve", params={"name": "s3pkg"}, headers=H).json()["sha256"])
        self.assertEqual(c.get("/files/s3pkg/s3pkg-1.0.0.tar.gz", follow_redirects=False).status_code, 401)   # no access check bypass
        self.assertEqual(c.get("/api/v1/readyz").json()["storage"], True)


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
        c = TestClient(create_app(Settings(data_dir=tempfile.mkdtemp(), db_backend="postgres", database_url=self.uri)))
        c.post("/api/v1/auth/register", json={"username": "root", "password": "secret1"})
        H = {"Authorization": "Bearer " + c.post("/api/v1/auth/login", json={"username": "root", "password": "secret1"}).json()["token"]}
        toml = b'[package]\nname = "pgpkg"\nversion = "1.0.0"\ntype = "skill"\ndescription = "invoices tool"\ntags = ["fin"]\n'
        b = io.BytesIO()
        with tarfile.open(fileobj=b, mode="w:gz") as t:
            ti = tarfile.TarInfo("aihub.toml"); ti.size = len(toml); t.addfile(ti, io.BytesIO(toml))
        self.assertEqual(c.post("/api/v1/upload", content=b.getvalue(), headers=H).status_code, 200)
        self.assertEqual(c.post("/api/v1/auth/register", json={"username": "root", "password": "secret1"}).status_code, 409)
        self.assertEqual(c.get("/api/v1/packages?q=invoice", headers=H).json()["total"], 1)       # full text, prefix
        self.assertEqual(c.get("/api/v1/packages?q=nomatch", headers=H).json()["total"], 0)
        self.assertEqual(c.post("/api/v1/packages/pgpkg/reviews", json={"rating": 5}, headers=H).status_code, 200)
        self.assertEqual(c.get("/api/v1/packages?sort=rating", headers=H).json()["items"][0]["rating"], {"avg": 5.0, "count": 1})
        self.assertEqual(c.post("/api/v1/events", json={"events": [{"kind": "use", "package": "pgpkg", "client_id": "c"}]}, headers=H).status_code, 200)
        self.assertEqual(c.get("/api/v1/dashboard?days=7", headers=H).json()["totals"], {"use": 1})
        self.assertEqual(c.get("/api/v1/readyz").json()["database"], "postgres")

    def test_two_app_instances_share_one_postgres_and_migrate_safely(self):
        from aihub.server.db import DB
        a = DB(Settings(db_backend="postgres", database_url=self.uri)); b = DB(Settings(db_backend="postgres", database_url=self.uri))   # re-running migrations is idempotent
        self.assertEqual(a.conn().execute("SELECT COUNT(*) FROM roles").fetchone()[0], b.conn().execute("SELECT COUNT(*) FROM roles").fetchone()[0])


if __name__ == "__main__":
    unittest.main()


class BuiltinSkillTest(unittest.TestCase):
    def test_install_and_remove(self):
        import os, tempfile
        from aihub.cli import integrations
        with tempfile.TemporaryDirectory() as d:
            r = integrations.install_builtin_skill(["claude", "codex"], "project", d)
            self.assertEqual(len(r), 4)
            for sub in (".claude/skills", ".agents/skills"):
                for name in ("aihub-package", "aihub-guide"):
                    self.assertTrue(os.path.isfile(os.path.join(d, sub, name, "SKILL.md")))
            integrations.install_builtin_skill(["claude"], "project", d, remove=True)
            for name in ("aihub-package", "aihub-guide"):
                self.assertFalse(os.path.exists(os.path.join(d, ".claude/skills", name)))
