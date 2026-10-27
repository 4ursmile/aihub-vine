"""Home page highlights: Admin > Settings > Home page (home_highlight style, ordered home_pinned list) and GET /highlights."""
import tempfile
import unittest

from fastapi.testclient import TestClient

from aihub.server import routers
from aihub.server.config import Settings
from aihub.server.main import create_app


class Highlights(unittest.TestCase):
    def setUp(self):
        routers._fails.clear()
        self.c = TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=tempfile.mkdtemp())))
        self.addCleanup(self.c.close)
        self.addCleanup(self.c.app.state.ingest.close)
        self.c.post("/api/v1/auth/register", json={"username": "root", "password": "secret1"})
        tok = self.c.post("/api/v1/auth/login", json={"username": "root", "password": "secret1"}).json()["token"]
        self.h = {"Authorization": "Bearer " + tok}
        repos = self.c.app.state.repos
        for n in ("alpha", "beta", "gamma"):
            repos.package_upsert(n, "skill", "d", [], visibility="public")
        repos.package_upsert("secret-one", "skill", "d", [], visibility="private")

    def put(self, **kw):
        return self.c.put("/api/v1/admin/settings", json=kw, headers=self.h)

    def test_defaults_to_slides_of_the_most_downloaded(self):
        d = self.c.get("/api/v1/highlights").json()
        self.assertEqual((d["style"], d["pinned"]), ("slides", False))
        self.assertTrue(d["items"])
        self.assertEqual(self.c.get("/api/v1/admin/settings", headers=self.h).json()["values"]["home_highlight"], "slides")

    def test_pinned_order_is_kept_and_unseen_names_are_skipped(self):
        r = self.put(home_pinned="Gamma, alpha gamma, secret-one, not-yet")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.c.get("/api/v1/admin/settings", headers=self.h).json()["values"]["home_pinned"], "gamma, alpha, secret-one, not-yet")
        d = self.c.get("/api/v1/highlights").json()                         # signed out: the private one is not revealed
        self.assertEqual((d["pinned"], [p["name"] for p in d["items"]]), (True, ["gamma", "alpha"]))
        self.assertEqual([p["name"] for p in self.c.get("/api/v1/highlights", headers=self.h).json()["items"]], ["gamma", "alpha", "secret-one"])

    def test_style_and_validation(self):
        self.assertEqual(self.put(home_highlight="cards").status_code, 200)
        self.assertEqual(self.c.get("/api/v1/highlights").json()["style"], "cards")
        self.assertEqual(self.put(home_highlight="off").status_code, 200)
        self.assertEqual(self.c.get("/api/v1/highlights").json(), {"style": "off", "pinned": False, "items": []})
        self.assertEqual(self.put(home_highlight="spin").status_code, 400)
        self.assertEqual(self.put(home_pinned="bad name!").status_code, 400)
        self.assertEqual(self.put(home_pinned=",".join("p%d" % i for i in range(13))).status_code, 400)


if __name__ == "__main__":
    unittest.main()
