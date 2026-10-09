import json
import os
import subprocess
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from aihub.server.config import Settings
from aihub.server.main import create_app
from aihub.server import sync as S


def obs(i, name="aihub.use", user="~dan", **meta):
    m = {"package": "demo", "component": "skill:x", "client_id": "c1", "detail": "token=hunter2 run", **meta}
    return {"id": i, "name": name, "userId": user, "startTime": "2026-10-08T10:00:00.000Z", "metadata": m}


class Schedule(unittest.TestCase):
    def test_seconds_and_cron(self):
        self.assertEqual(S.parse_schedule("60"), ("every", 60))
        now = time.mktime((2026, 10, 8, 10, 2, 30, 0, 0, -1))
        self.assertEqual(round(S.next_delay(S.parse_schedule("*/5 * * * *"), now)), 150)
        for bad in ("5", "*/0 * * * *", "61 * * * *", "a b c", "* * * *"):
            with self.assertRaises(ValueError, msg=bad):
                S.parse_schedule(bad)


class Rows(unittest.TestCase):
    def test_to_row_identity_and_redaction(self):
        r = S.to_row(obs("o1"))
        self.assertIsNone(r["username"])
        self.assertEqual(r["local_user"], "dan")
        self.assertNotIn("hunter2", r["detail"])
        self.assertEqual(S.to_row(obs("o2", user="alice"))["username"], "alice")
        self.assertIsNone(S.to_row(obs("o3", name="other.thing")))
        self.assertIsNone(S.to_row({"id": "x", "name": "aihub.use", "metadata": {}}))


class Server(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.c = TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=self.d)))
        self.r = self.c.app.state.repos
        self.sy = self.c.app.state.sync

    def admin(self):
        self.c.post("/api/v1/auth/register", json={"username": "root", "password": "secret1"})
        t = self.c.post("/api/v1/auth/login", json={"username": "root", "password": "secret1"}).json()["token"]
        return {"Authorization": "Bearer " + t}

    def test_events_are_idempotent(self):
        rows = [S.to_row(obs("a")), S.to_row(obs("b"))]
        self.assertEqual(self.r.events_insert_ext([dict(x) for x in rows]), 2)
        self.assertEqual(self.r.events_insert_ext([dict(x) for x in rows]), 0)           # overlap / full re-lookback
        self.assertEqual(self.r.events_insert_ext([dict(rows[0]), S.to_row(obs("c"))]), 1)

    def test_index_sync_adds_updates_and_hides(self):
        repo = tempfile.mkdtemp()
        idx = os.path.join(repo, "index.json")
        pk = lambda n, v: {"name": n, "type": "skill", "description": "d", "latest_version": v, "tags": ["t"],
                           "requires": {"packages": []}, "repo": {"url": "u", "branch": "main"}}
        json.dump({"packages": [pk("one", "1.0.0"), pk("two", "1.0.0")]}, open(idx, "w"))
        os.environ["AIHUB_INDEX_URL"] = idx
        try:
            self.sy.pull_index()
            self.assertEqual(sorted(self.r.package_names_visible(None)), ["one", "two"])
            json.dump({"packages": [pk("one", "1.1.0")]}, open(idx, "w"))
            self.sy.pull_index()
            self.assertEqual(self.r.package("one")["latest_version"], "1.1.0")
            self.assertEqual(self.r.package("two")["hidden"], 1)                          # dropped from the index -> hidden
            self.sy.pull_index()                                                          # idempotent
            self.assertEqual(len(self.r.versions(self.r.package("one")["id"])), 2)
        finally:
            os.environ.pop("AIHUB_INDEX_URL", None)

    def test_admin_sync_settings_hide_secret_and_validate_schedule(self):
        h = self.admin()
        self.assertEqual(self.c.put("/api/v1/admin/sync", json={"sync_interval": "*/10 * * * *", "langfuse_secret_key": "sk-x"}, headers=h).status_code, 200)
        g = self.c.get("/api/v1/admin/sync", headers=h).json()
        self.assertEqual(g["config"]["sync_interval"], "*/10 * * * *")
        self.assertEqual(g["config"]["langfuse_secret_key"], "set")                       # never echoed
        self.assertNotIn("sk-x", json.dumps(g))
        self.assertEqual(self.c.put("/api/v1/admin/sync", json={"sync_interval": "nope"}, headers=h).status_code, 400)
        self.assertEqual(self.c.put("/api/v1/admin/sync", json={"langfuse_secret_key": "set"}, headers=h).status_code, 200)
        self.assertEqual(self.r.setting("langfuse_secret_key"), "sk-x")                    # echo does not overwrite
        self.assertEqual(self.c.get("/api/v1/admin/sync").status_code, 401)

    def test_removed_routes_are_gone(self):
        h = self.admin()
        for method, path in (("post", "/api/v1/upload"), ("post", "/api/v1/uploads"), ("post", "/api/v1/events")):
            self.assertEqual(getattr(self.c, method)(path, json={}, headers=h).status_code, 404 if path != "/api/v1/events" else 405 if False else 404 , path)
        self.assertEqual(self.c.get("/files/a/b.tar.gz").status_code, 404)


if __name__ == "__main__":
    unittest.main()


class ClientConfig(unittest.TestCase):
    def setUp(self):
        self.c = TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=tempfile.mkdtemp())))
        self.c.post("/api/v1/auth/register", json={"username": "root", "password": "secret1"})
        self.h = {"Authorization": "Bearer " + self.c.post("/api/v1/auth/login", json={"username": "root", "password": "secret1"}).json()["token"]}
        self.c.put("/api/v1/admin/sync", headers=self.h, json={
            "index_url": "https://git.example/x.git", "langfuse_host": "https://lf", "langfuse_public_key": "pk-1",
            "langfuse_secret_key": "sk-SECRET", "git_username": "bot", "git_token": "glpat-TOKEN", "enroll_code": "TEAM1"})   # git keys are no longer accepted

    def test_credentials_are_gated(self):
        g = lambda **k: self.c.get("/api/v1/client-config", **k)
        r = g()                                                    # sharing off (default): index only
        self.assertEqual(r.json()["index"]["url"], "https://git.example/x.git")
        self.assertIsNone(r.json()["credentials"])
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.c.put("/api/v1/admin/sync", headers=self.h, json={"share_credentials": "1"})
        self.assertIsNone(g().json()["credentials"])               # anonymous, no code
        self.assertIsNone(g(params={"code": "WRONG"}).json()["credentials"])
        ok = g(params={"code": "TEAM1"}).json()["credentials"]
        self.assertEqual(ok["langfuse"]["secret_key"], "sk-SECRET")
        self.assertNotIn("git", ok)                                                       # git credentials are never shared
        self.assertNotIn("glpat-TOKEN", json.dumps(g(params={"code": "TEAM1"}).json()))
        self.assertIsNone(self.c.app.state.repos.setting("git_token"))                                    # ...and not even stored
        self.assertIsNotNone(g(headers=self.h).json()["credentials"])   # signed-in user needs no code
        self.assertNotIn("SECRET", json.dumps(self.c.get("/api/v1/admin/sync", headers=self.h).json()))
        self.assertNotIn("TOKEN", json.dumps(self.c.get("/api/v1/admin/sync", headers=self.h).json()))
        self.assertIn("client-config.credentials", [r[0] for r in self.c.app.state.repos.db.conn().execute("select action from audit_log")])   # every hand-out is audited


class DashboardV2(unittest.TestCase):
    def setUp(self):
        self.c = TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=tempfile.mkdtemp())))
        self.r = self.c.app.state.repos
        self.c.post("/api/v1/auth/register", json={"username": "root", "password": "secret1"})
        self.h = {"Authorization": "Bearer " + self.c.post("/api/v1/auth/login", json={"username": "root", "password": "secret1"}).json()["token"]}
        self.r.package_upsert("alpha", "skill", "d", [], "")
        self.r.package_upsert("beta", "mcp", "d", [], "")
        now = time.time()
        ev = lambda i, **k: dict({"ts": now - 3600, "kind": "use", "package": "alpha", "client_id": "c%d" % i, "username": None, "ext_id": "e%d" % i,
                                  "component": "skill:a", "host": "mbp", "source": "claude", "detail": "SECRET-DETAIL", "cwd": "/priv/path"}, **k)
        self.r.events_insert_ext([ev(1), ev(2, host="pc", package="beta", component="mcp:db"), ev(3, username="alice", host="mbp"),
                                  ev(4, kind="publish", ts=now - 40 * 86400)])

    def get(self, path, **p):
        r = self.c.get("/api/v1" + path, params=p, headers=self.h)
        return r

    def test_filters_and_aggregates(self):
        d = self.get("/dashboard", days=30).json()
        self.assertEqual(d["totals"].get("use"), 3)
        self.assertNotIn("publish", d["totals"])                                       # 40 days ago: outside the window
        self.assertEqual(self.get("/dashboard", days=90).json()["totals"].get("publish"), 1)
        self.assertEqual(self.get("/dashboard", identity="anonymous").json()["totals"]["use"], 2)
        self.assertEqual(self.get("/dashboard", identity="signed-in").json()["totals"]["use"], 1)
        self.assertEqual(self.get("/dashboard", host="pc").json()["totals"]["use"], 1)
        self.assertEqual(self.get("/dashboard", q="mcp:db").json()["totals"]["use"], 1)   # matches component
        self.assertEqual(self.get("/dashboard", q="alph").json()["totals"]["use"], 2)     # matches package
        self.assertEqual({x["name"]: x["count"] for x in d["by_identity"]}, {"anonymous": 2, "signed in": 1})
        self.assertEqual(sum(sum(row) for row in d["heatmap"]), 3)
        self.assertIn("mbp", d["options"]["hosts"])
        self.assertEqual(d["top_components"][0]["name"], "skill:a")

    def test_custom_range_and_validation(self):
        import datetime
        today = datetime.datetime.now(datetime.timezone.utc).date()
        d = self.get("/dashboard", **{"from": (today - datetime.timedelta(days=2)).isoformat(), "to": today.isoformat()}).json()
        self.assertEqual(len(d["daily"]), 3)
        self.assertEqual(self.get("/dashboard", **{"from": "nope"}).status_code, 400)
        self.assertEqual(self.get("/dashboard", **{"from": today.isoformat(), "to": (today - datetime.timedelta(days=3)).isoformat()}).status_code, 400)
        self.assertEqual(self.get("/dashboard", **{"from": "2020-01-01"}).status_code, 400)           # > 366 days
        self.assertEqual(self.get("/dashboard", identity="robot").status_code, 400)
        self.assertEqual(self.get("/dashboard", kind="bogus").status_code, 400)

    def test_event_log_is_paged_filtered_and_private(self):
        r = self.get("/dashboard/events", per_page=2)
        j = r.json()
        self.assertEqual((j["total"], len(j["items"])), (3, 2))
        self.assertEqual(len(self.get("/dashboard/events", per_page=2, page=2).json()["items"]), 1)
        self.assertEqual(self.get("/dashboard/events", host="pc").json()["total"], 1)
        self.assertNotIn("SECRET-DETAIL", r.text)                                         # detail / cwd stay on the audit page
        self.assertNotIn("/priv/path", r.text)
        self.assertEqual(self.c.get("/api/v1/dashboard/events").status_code, 401)
        self.c.post("/api/v1/auth/register", json={"username": "bob", "password": "secret1"})
        bh = {"Authorization": "Bearer " + self.c.post("/api/v1/auth/login", json={"username": "bob", "password": "secret1"}).json()["token"]}
        self.assertEqual(self.c.get("/api/v1/dashboard/events", headers=bh).status_code, 403)   # needs view_dashboard
        self.assertEqual(self.c.get("/api/v1/dashboard", headers=bh).status_code, 403)

    def test_top_developers_come_from_manifest_authors(self):
        pid = {n: self.r.package("alpha" if n == "a" else "beta")["id"] for n in "ab"}
        pkg = lambda authors: {"package": {"name": "x", "version": "1.0.0", "authors": authors}}
        self.r.version_sync(pid["a"], "1.0.0", pkg([{"name": "Ann"}, {"name": "Bo", "email": "b@x.org"}]))
        self.r.version_sync(pid["b"], "1.0.0", pkg([{"name": "Ann"}]))
        self.c.app.state.cache.invalidate("dash")
        want = [{"name": "Ann", "count": 3}, {"name": "Bo", "count": 2}]          # alpha: 2 events, beta: 1
        self.assertEqual(self.get("/dashboard", days=30).json()["top_developers"], want)
        self.assertEqual(self.c.get("/api/v1/rankings/developers?days=30").json()["items"], want)
        self.assertEqual(self.get("/dashboard", days=30, package="beta").json()["top_developers"], [{"name": "Ann", "count": 1}])

    def test_private_package_events_hidden_from_non_members(self):
        self.r.package_upsert("hush", "skill", "d", [], "", visibility="private")
        self.r.events_insert_ext([{"ts": time.time(), "kind": "use", "package": "hush", "client_id": "z", "username": None, "ext_id": "p1"}])
        self.c.post("/api/v1/auth/register", json={"username": "viewer", "password": "secret1"})
        self.r.db.conn().execute("INSERT OR IGNORE INTO role_permissions VALUES('user','view_dashboard')"); self.r.db.conn().commit()
        vh = {"Authorization": "Bearer " + self.c.post("/api/v1/auth/login", json={"username": "viewer", "password": "secret1"}).json()["token"]}
        self.c.app.state.cache.invalidate("dash")
        seen = self.c.get("/api/v1/dashboard/events?per_page=100", headers=vh).json()
        self.assertNotIn("hush", [x["package"] for x in seen["items"]])
        self.assertIn("hush", [x["package"] for x in self.get("/dashboard/events", per_page=100).json()["items"]])   # admin sees it


class CountedSpans(unittest.TestCase):
    def test_count_expands_to_rows_capped_and_idempotent(self):
        o = obs("o9", count="25")
        rows = S.to_rows(o)
        self.assertEqual(len(rows), 25)
        self.assertEqual(len({r["ext_id"] for r in rows}), 25)
        self.assertEqual(len(S.to_rows(obs("o8", count="999999"))), S.MAX_COUNT)           # a bogus count can't flood the table
        self.assertEqual(len(S.to_rows(obs("o7", count="junk"))), 1)
        self.assertEqual(len(S.to_rows(obs("o6"))), 1)
        d = tempfile.mkdtemp()
        c = TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=d)))
        r = c.app.state.repos
        self.assertEqual(r.events_insert_ext([dict(x) for x in rows]), 25)
        self.assertEqual(r.events_insert_ext([dict(x) for x in rows]), 0)
