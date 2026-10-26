"""Admin-saved git login (server/gitlogin.py): encrypted binary store, and gitx using it ONLY as a fallback after the usual login is refused."""
import base64
import http.server
import os
import tempfile
import threading
import unittest
from unittest import mock

from aihub.cli import gitx
from aihub.server import gitlogin


def _read(p):
    with open(p, "rb") as f:
        return f.read()


def pkt(s):
    b = s.encode()
    return b"%04x" % (len(b) + 4) + b


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.st = gitlogin.Store(self.d)

    def test_round_trip_is_encrypted_binary_and_private(self):
        self.assertEqual(self.st.put("https://Git.Example.com/org/repo.git", "bob", "tok-123", "admin"), "git.example.com")
        self.assertEqual(self.st.get("git.example.com"), ("bob", "tok-123"))
        raw = _read(self.st.path)
        self.assertTrue(raw.startswith(gitlogin.MAGIC))
        self.assertNotIn(b"tok-123", raw)
        self.assertNotIn(b"bob", raw)
        if os.name == "posix":
            self.assertEqual(os.stat(self.st.path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(self.st.key_file).st_mode & 0o777, 0o600)
        self.assertEqual(gitlogin.Store(self.d).get("git.example.com"), ("bob", "tok-123"))        # a fresh process reads it back
        self.assertNotIn("secret", str(self.st.list()))
        self.assertNotIn("tok-123", str(self.st.list()))

    def test_tampered_or_rekeyed_file_is_refused(self):
        self.st.put("git.example.com", "", "tok")
        self.assertEqual(self.st.get("git.example.com"), ("oauth2", "tok"))                       # blank user = oauth2
        raw = bytearray(_read(self.st.path))
        raw[25] ^= 1
        with open(self.st.path, "wb") as f:
            f.write(bytes(raw))
        with self.assertLogs("aihub", "ERROR"):
            self.assertIsNone(self.st.get("git.example.com"))
        self.assertTrue(self.st.error)
        with self.assertRaises(ValueError):
            self.st.put("git.example.com", "", "tok2")                                            # never silently overwrites
        self.assertTrue(self.st.remove("git.example.com"))
        self.st.put("git.example.com", "", "tok2")
        self.assertEqual(self.st.get("git.example.com"), ("oauth2", "tok2"))

    def test_bad_input(self):
        for host, user, sec in (("", "u", "t"), ("git.example.com", "u", ""), ("git.example.com", "u", "a\nb"), ("git.example.com", "u\n", "t")):
            with self.assertRaises(ValueError):
                self.st.put(host, user, sec)
        self.assertFalse(self.st.remove("nothing.example.com"))


class Fallback(unittest.TestCase):
    """Real git against a local listener that accepts only GOOD credentials."""
    GOOD = "Basic " + base64.b64encode(b"admin:good-token").decode()

    @classmethod
    def setUpClass(cls):
        cls.seen = []
        outer = cls

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.seen.append(self.headers.get("Authorization"))
                if self.headers.get("Authorization") != outer.GOOD:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="x"')
                    self.end_headers()
                    return
                body = (pkt("# service=git-upload-pack\n") + b"0000" +
                        pkt("0" * 40 + " capabilities^{}\0report-status\n") + b"0000")
                self.send_response(200)
                self.send_header("Content-Type", "application/x-git-upload-pack-advertisement")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.host = "localhost:%d" % cls.srv.server_address[1]
        cls.url = "http://%s/r.git" % cls.host

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.seen.clear()
        gitx._fallback_first.clear()
        self.p = [mock.patch.dict(os.environ, {"AIHUB_GIT_ALLOW_HTTP": "1", "GIT_CONFIG_NOSYSTEM": "1", "GIT_ASKPASS": "echo", "HOME": tempfile.mkdtemp(),
                                                 "XDG_CONFIG_HOME": tempfile.mkdtemp()}), mock.patch.object(gitx, "BATCH", True)]
        [p.start() for p in self.p]

    def tearDown(self):
        [p.stop() for p in self.p]

    def fb(self, host):
        return ("admin", "good-token") if host == self.host else None

    def test_no_saved_login_changes_nothing(self):
        with mock.patch.object(gitx, "LOGIN", lambda h: ("bob", "bad")), mock.patch.object(gitx, "FALLBACK", None):
            self.assertNotEqual(gitx._ls(self.url).returncode, 0)
        self.assertNotIn(self.GOOD, self.seen)

    def test_refused_login_falls_back_then_goes_first(self):
        with mock.patch.object(gitx, "LOGIN", lambda h: ("bob", "bad")), mock.patch.object(gitx, "FALLBACK", self.fb):
            with self.assertLogs("aihub", "WARNING"):
                gitx.run(["ls-remote", self.url])
            self.assertIn(self.GOOD, self.seen)
            self.assertIn(self.host, gitx._fallback_first)
            self.seen.clear()
            self.assertEqual(gitx._ls(self.url).returncode, 0)
            self.assertNotIn("Basic " + base64.b64encode(b"bob:bad").decode(), self.seen)             # sticky: saved login first

    def test_missing_login_falls_back(self):
        with mock.patch.object(gitx, "LOGIN", lambda h: None), mock.patch.object(gitx, "FALLBACK", self.fb), self.assertLogs("aihub", "WARNING"):
            self.assertEqual(gitx._ls(self.url).returncode, 0)

    def test_working_login_never_uses_the_saved_one(self):
        with mock.patch.object(gitx, "LOGIN", lambda h: ("admin", "good-token") if h == self.host else None), \
                mock.patch.object(gitx, "FALLBACK", lambda h: ("other", "x")):
            gitx.run(["ls-remote", self.url])
        self.assertEqual(set(self.seen) - {None}, {self.GOOD})

    def test_saved_login_is_only_for_its_host(self):
        with mock.patch.object(gitx, "LOGIN", lambda h: None), mock.patch.object(gitx, "FALLBACK", lambda h: None):
            self.assertNotEqual(gitx._ls(self.url).returncode, 0)

    def test_check_uses_only_the_saved_login(self):
        st = gitlogin.Store(tempfile.mkdtemp())
        self.assertFalse(gitlogin.check(st, self.url)[0])
        st.put(self.host, "admin", "good-token")
        ok, msg = gitlogin.check(st, self.url, self.host)
        self.assertTrue(ok, msg)
        self.assertFalse(gitlogin.check(st, self.url, "other.example.com")[0])


class AdminApi(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from aihub.server.config import Settings
        from aihub.server.main import create_app
        for k in ("LOGIN", "FALLBACK", "BATCH"):
            self.addCleanup(setattr, gitx, k, getattr(gitx, k))
        self.data = tempfile.mkdtemp()
        self.c = TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=self.data)))
        self.addCleanup(self.c.close)
        self.addCleanup(self.c.app.state.ingest.close)
        self.c.post("/api/v1/auth/register", json={"username": "root", "password": "secret1"})
        self.h = {"Authorization": "Bearer " + self.c.post("/api/v1/auth/login", json={"username": "root", "password": "secret1"}).json()["token"]}

    def test_save_list_remove_never_returns_the_secret(self):
        u = "/api/v1/admin/git-login"
        self.assertEqual(self.c.get(u).status_code, 401)
        r = self.c.put(u, json={"host": "https://git.example.com/a/b.git", "user": "bob", "secret": "tok-XYZ"}, headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("tok-XYZ", r.text)
        self.assertEqual([x["host"] for x in r.json()["logins"]], ["git.example.com"])
        self.assertEqual(gitx.FALLBACK("git.example.com"), ("bob", "tok-XYZ"))              # what the server's git falls back to
        self.assertNotIn("tok-XYZ", self.c.get("/api/v1/audit", headers=self.h).text)
        self.assertEqual(self.c.put(u, json={"host": "git.example.com", "secret": "a\nb"}, headers=self.h).status_code, 400)
        self.assertEqual(self.c.delete(u + "/git.example.com", headers=self.h).json()["logins"], [])
        self.assertIsNone(gitx.FALLBACK("git.example.com"))


if __name__ == "__main__":
    unittest.main()
