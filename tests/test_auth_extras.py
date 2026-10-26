"""Sign-in email domains (login_domains), "Remember me" sessions, Admin > Listen (live server log)."""
import logging
import tempfile
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from aihub.cli import gitx
from aihub.server import loglisten, routers
from aihub.server.config import Settings
from aihub.server.main import create_app
from aihub.server.repos import Repos


class Base(unittest.TestCase):
    def setUp(self):
        routers._fails.clear()
        self.addCleanup(routers._fails.clear)
        for k in ("LOGIN", "FALLBACK", "BATCH"):
            self.addCleanup(setattr, gitx, k, getattr(gitx, k))
        self.c = TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=tempfile.mkdtemp())))
        self.addCleanup(self.c.close)
        self.addCleanup(self.c.app.state.ingest.close)
        self.c.post("/api/v1/auth/register", json={"username": "root", "password": "secret1"})
        self.h = self.auth("root", "secret1")
        self.c.post("/api/v1/auth/register", json={"username": "abc", "password": "secret2"})

    def login(self, user, pw, **kw):
        return self.c.post("/api/v1/auth/login", json=dict(username=user, password=pw, **kw))

    def auth(self, user, pw, **kw):
        return {"Authorization": "Bearer " + self.login(user, pw, **kw).json()["token"]}


class LoginDomains(Base):
    def test_allowed_domain_maps_to_the_account(self):
        self.assertEqual(self.login("abc@gmail.com", "secret2").status_code, 401)            # off by default
        r = self.c.put("/api/v1/admin/settings", json={"login_domains": "@Gmail.com yahoo.com,"}, headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.c.get("/api/v1/admin/settings", headers=self.h).json()["values"]["login_domains"], "gmail.com, yahoo.com")
        for name in ("abc@gmail.com", "abc@YAHOO.com", "abc"):
            r = self.login(name, "secret2")
            self.assertEqual((r.status_code, r.json()["username"]), (200, "abc"), name)
        self.assertEqual(self.login("abc@evil.com", "secret2").status_code, 401)
        self.assertEqual(self.login("abc@gmail.com", "wrong").status_code, 401)
        self.assertEqual(self.c.put("/api/v1/admin/settings", json={"login_domains": "not a domain!"}, headers=self.h).status_code, 400)

    def test_attempt_counter_is_shared(self):
        self.c.put("/api/v1/admin/settings", json={"login_domains": "gmail.com"}, headers=self.h)
        for i in range(8):
            self.login("abc@gmail.com" if i % 2 else "abc", "wrong")
        self.assertEqual(self.login("abc", "secret2").status_code, 429)


class Remember(Base):
    def test_short_session_expires(self):
        h = self.auth("abc", "secret2", remember=False)
        long = self.auth("abc", "secret2")
        self.assertEqual(self.c.get("/api/v1/auth/me", headers=h).status_code, 200)
        later = time.time() + Repos.SHORT_SESSION + 5
        with mock.patch("aihub.server.repos.time.time", return_value=later):
            self.assertEqual(self.c.get("/api/v1/auth/me", headers=h).status_code, 401)
            self.assertEqual(self.c.get("/api/v1/auth/me", headers=long).status_code, 200)


class Listen(Base):
    def tearDown(self):
        loglisten.TAP.stop()
        loglisten.TAP.clear()

    def test_listen_shows_new_lines_redacted_and_stops(self):
        u = "/api/v1/admin/logs"
        self.assertEqual(self.c.get(u).status_code, 401)
        logging.getLogger("aihub").warning("before listening")
        r = self.c.post(u + "/listen", json={"on": True}, headers=self.h).json()
        self.assertTrue(r["listening"])
        logging.getLogger("aihub").warning("sync pass done password=hunter2")
        got = self.c.get(u + "?after=0", headers=self.h).json()
        text = "\n".join(x["msg"] for x in got["lines"])
        self.assertIn("sync pass done", text)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("before listening", text)
        self.assertEqual(self.c.get(u + "?after=%d" % got["seq"], headers=self.h).json()["lines"], [])
        self.assertFalse(self.c.post(u + "/listen", json={"on": False}, headers=self.h).json()["listening"])
        logging.getLogger("aihub").warning("after stop")
        self.assertNotIn("after stop", str(self.c.get(u, headers=self.h).json()))

    def test_idle_listener_detaches(self):
        loglisten.TAP.start("t")
        loglisten.TAP.polled -= loglisten.IDLE + 1
        self.assertFalse(loglisten.TAP.listening)
        self.assertNotIn(loglisten.TAP.handler, logging.getLogger().handlers)


if __name__ == "__main__":
    unittest.main()
