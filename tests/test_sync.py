import json
import os
import subprocess
import tempfile
import time
import unittest
import unittest.mock

from fastapi.testclient import TestClient

from aihub.server.config import Settings
from aihub.server.main import create_app
from aihub.server import sync as S


def obs(i, kind="use", user="", **k):
    """One event as a CLI batch carries it."""
    return {"id": i, "kind": kind, "user": user, "package": "demo", "component": "skill:x", "client_id": "c1", "local_user": "dan",
            "detail": "token=hunter2 run", "ts": 1791000000, **k}


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
        self.assertIsNone(S.to_row(obs("o3", kind="other")))
        self.assertIsNone(S.to_row({"id": "x", "kind": "use"}))                        # no package
        self.assertIsNone(S.to_row({"kind": "use", "package": "p"}))                    # no id: could not be deduplicated
        self.assertIsNone(S.to_row("junk"))
        self.assertEqual(S.to_row(obs("o4", user="~dan"))["username"], None)
        self.assertLessEqual(S.to_row(obs("o5", ts=9e12))["ts"], time.time() + 86401)      # a far-future client clock is clamped


class Server(unittest.TestCase):
    def setUp(self):
        # These tests read server SETTINGS (admin value, defaults.json); the suite-wide push guard variable would override them. They never push.
        env = unittest.mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("AIHUB_TELEMETRY_URL", None)
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

    def test_admin_sync_settings_validate_schedule_and_archive_flag(self):
        h = self.admin()
        self.assertEqual(self.c.put("/api/v1/admin/sync", json={"sync_interval": "*/10 * * * *"}, headers=h).status_code, 200)
        g = self.c.get("/api/v1/admin/sync", headers=h).json()
        self.assertEqual(g["config"]["sync_interval"], "*/10 * * * *")
        self.assertNotIn("telemetry_url", g["config"])                               # no git inbox any more
        self.assertEqual(self.c.put("/api/v1/admin/sync", json={"sync_interval": "nope"}, headers=h).status_code, 400)
        self.assertEqual(self.c.put("/api/v1/admin/sync", json={"localize_users": "maybe"}, headers=h).status_code, 400)
        self.assertEqual(self.c.put("/api/v1/admin/sync", json={"localize_users": "0"}, headers=h).status_code, 200)
        self.assertEqual(self.c.get("/api/v1/admin/sync").status_code, 401)

    def test_backup_settings_api_for_the_admin_tab(self):
        h = self.admin()
        g = lambda: self.c.get("/api/v1/admin/backup", headers=h).json()
        self.assertEqual(sorted(g()), ["config", "from_env", "status", "target"])
        self.assertEqual(g()["config"]["backup_enabled"], "0")                            # off until an admin turns it on
        self.assertEqual(self.c.put("/api/v1/admin/backup", headers=h, json={"backup_enabled": "1", "backup_schedule": "0 */6 * * *", "backup_events_chunk": "5000"}).status_code, 200)
        self.assertEqual(g()["config"], {"backup_enabled": "1", "backup_schedule": "0 */6 * * *", "backup_events_chunk": "5000"})
        for bad in ({"backup_enabled": "2"}, {"backup_schedule": "nope"}, {"backup_events_chunk": "5"}):
            self.assertEqual(self.c.put("/api/v1/admin/backup", headers=h, json=bad).status_code, 400, bad)
        self.assertEqual(self.c.get("/api/v1/admin/backup").status_code, 401)             # admins only
        with unittest.mock.patch.dict(os.environ, {"AIHUB_BACKUP_ENABLED": "0"}):
            self.assertTrue(self.c.get("/api/v1/admin/backup", headers=h).json()["from_env"]["backup_enabled"])      # the UI can say "set by environment"

    def test_removed_routes_are_gone(self):
        h = self.admin()
        for method, path in (("post", "/api/v1/upload"), ("post", "/api/v1/uploads"), ("post", "/api/v1/events")):
            self.assertEqual(getattr(self.c, method)(path, json={}, headers=h).status_code, 404 if path != "/api/v1/events" else 405 if False else 404 , path)
        self.assertEqual(self.c.get("/files/a/b.tar.gz").status_code, 404)


if __name__ == "__main__":
    unittest.main()


class ClientConfig(unittest.TestCase):
    def setUp(self):
        env = unittest.mock.patch.dict(os.environ)                  # see Server.setUp: settings tests must not see the suite-wide push guard
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("AIHUB_TELEMETRY_URL", None)
        self.c = TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=tempfile.mkdtemp())))
        self.c.post("/api/v1/auth/register", json={"username": "root", "password": "secret1"})
        self.h = {"Authorization": "Bearer " + self.c.post("/api/v1/auth/login", json={"username": "root", "password": "secret1"}).json()["token"]}
        self.c.put("/api/v1/admin/sync", headers=self.h, json={"index_url": "https://git.example/x.git"})

    def test_locations_only_never_credentials(self):
        r = self.c.get("/api/v1/client-config")
        self.assertEqual(r.json()["index"]["url"], "https://git.example/x.git")
        self.assertNotIn("telemetry", r.json())
        self.assertNotIn("credentials", r.json())
        self.assertEqual(r.headers["cache-control"], "no-store")


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
                                  "component": "skill:a", "host": "mbp", "os": "macOS", "source": "claude", "detail": "SECRET-DETAIL", "cwd": "/priv/path"}, **k)
        self.r.events_insert_ext([ev(1), ev(2, host="pc", os="Windows", package="beta", component="mcp:db"), ev(3, username="alice", host="mbp"),
                                  ev(4, kind="publish", ts=now - 40 * 86400)])

    def get(self, path, **p):
        r = self.c.get("/api/v1" + path, params=p, headers=self.h)
        return r

    def test_cli_events_listed_not_counted(self):
        self.r.events_insert_ext([{"ts": time.time() - 60, "kind": "install", "package": "aihub-cli", "client_id": "c9", "username": None, "ext_id": "cli1"}])
        self.assertNotIn("install", self.get("/dashboard", days=30).json()["totals"])
        self.assertIn("aihub-cli", [i["package"] for i in self.get("/dashboard/events", days=30).json()["items"]])

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

    def test_os_filter_chart_and_audit(self):
        d = self.get("/dashboard", days=30).json()
        self.assertEqual({x["name"]: x["count"] for x in d["by_os"]}, {"macOS": 2, "Windows": 1})
        self.assertEqual(sorted(d["by_os_daily"]["series"]), ["Windows", "macOS"])
        self.assertEqual(sum(sum(v for k, v in x.items() if k != "date") for x in d["by_os_daily"]["daily"]), 3)
        self.assertEqual(sorted(d["options"]["oses"]), ["Windows", "macOS"])
        self.assertEqual(self.get("/dashboard", os="Windows").json()["totals"]["use"], 1)
        self.assertEqual(self.get("/dashboard/events", os="macOS").json()["total"], 2)
        a = self.get("/audit", days=0, os="Windows").json()
        self.assertEqual(a["total"], 1)
        self.assertEqual(a["items"][0]["os"], "Windows")
        self.assertEqual(self.get("/audit", days=0, host="mbp").json()["total"], 3)
        self.assertEqual(self.get("/audit", days=0, host="mbp", os="Windows").json()["total"], 0)
        self.assertIn("pc", a["hosts"])
        self.assertIn("macOS", a["oses"])

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


class Pipeline(unittest.TestCase):
    """CLI -> (fake Kafka topic) -> server. The real spool, flusher, batching and backoff; only the broker is replaced by a list that
    `drain()` hands to Sync.ingest, exactly what the consumer thread does. The real broker is covered by test_kafka_e2e."""

    def setUp(self):
        self.schemes = unittest.mock.patch.object(S, "SCHEMES", S.SCHEMES + ("file://",))      # a file:// repo stands in for a public host
        self.schemes.start()
        self.home = tempfile.mkdtemp()
        self.env = unittest.mock.patch.dict(os.environ, {"AIHUB_HOME": self.home})
        self.env.start()
        from aihub.cli import telemetry
        self.topic, self.down = [], False
        def send(lines, timeout=20):
            if self.down:
                raise RuntimeError("broker down")
            self.topic.extend(json.loads(l) for l in lines)
        self.tp = [unittest.mock.patch.object(telemetry, "enabled", return_value=True), unittest.mock.patch.object(telemetry, "send", side_effect=send)]
        for t in self.tp:
            t.start()
        self.c = TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=tempfile.mkdtemp())))
        self.r, self.sy = self.c.app.state.repos, self.c.app.state.sync

    def tearDown(self):
        for t in self.tp:
            t.stop()
        self.schemes.stop()
        self.env.stop()

    def drain(self):
        msgs, self.topic = self.topic, []
        return self.sy.ingest(msgs)

    def spool(self, n, **k):
        from aihub.cli import telemetry
        for i in range(n):
            telemetry.enqueue(dict({"kind": "use", "package": "demo", "component": "skill:x"}, **k))

    def count(self, r=None, where="package='demo'"):
        return (r or self.r).db.conn().execute("SELECT COUNT(*) FROM events WHERE " + where).fetchone()[0]

    def test_batch_roundtrip_and_redelivery_is_idempotent(self):
        from aihub.cli import telemetry
        self.spool(50)
        self.assertEqual(telemetry.flush(force=True), 50)
        self.assertEqual(len(self.topic), 50)
        self.assertEqual(telemetry.flush(force=True), 0)                          # nothing spooled -> nothing sent
        msgs = list(self.topic)
        self.assertEqual(self.drain(), 50)
        self.assertEqual(self.count(), 50)
        self.assertEqual(self.sy.ingest(msgs), 0)                                 # Kafka redelivers: no duplicate rows
        self.assertEqual(self.count(), 50)

    def test_broker_down_keeps_events_and_resend_does_not_duplicate(self):
        from aihub.cli import telemetry
        self.spool(3)
        self.down = True
        self.assertEqual(telemetry.flush(force=True), 0)
        self.assertEqual(self.topic, [])
        self.down = False
        self.assertEqual(telemetry.flush(force=True), 3)
        self.drain()
        self.sy.ingest(self.topic or [])
        self.assertEqual(self.count(), 3)

    def test_garbage_messages_are_dropped_without_blocking_good_ones(self):
        self.assertEqual(self.sy.ingest(["not json", {"kind": "use"}, obs("good1")]), 1)
        self.assertEqual(self.count(where="ext_id='good1'"), 1)

    def test_failed_send_is_recorded_where_the_user_can_see_it(self):
        from aihub.cli import telemetry
        self.spool(1)
        self.down = True
        telemetry.flush(force=True)
        st = telemetry.last_status()
        self.assertFalse(st["ok"])
        self.assertTrue(st["error"])
        self.down = False
        telemetry.flush(force=True)
        self.assertTrue(telemetry.last_status()["ok"])
        self.assertEqual(telemetry.last_status()["last_sent"], 1)

    def test_index_failure_is_reported_and_usage_is_still_stored(self):
        from aihub.cli import telemetry
        self.spool(3)
        telemetry.flush(force=True)
        self.drain()
        def broken():
            raise RuntimeError("index: cannot reach git")
        self.sy.pull_index = broken
        with self.assertRaises(RuntimeError):
            self.sy.run_once()
        self.assertEqual(self.count(), 3)
        self.assertIn("cannot reach git", self.sy.status["last_error"])
        self.assertIsNotNone(self.sy.status["index_error"])

    def test_error_persists_until_a_clean_pass_then_clears(self):
        calls = {"n": 0}
        def flaky():
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("index: down")
        self.sy.pull_index = flaky
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                self.sy.run_once()
            self.assertTrue(self.sy.status["last_error"])
        self.sy.run_once()
        self.assertIsNone(self.sy.status["last_error"])
        self.assertTrue(self.r.setting("sync_cursor"))


    def test_publish_is_shown_at_once_and_the_index_commit_is_batched(self):
        """A new version is on the web the moment the event is ingested; git is written once per interval, for the whole queue."""
        pubs = [dict(obs("pub%d" % i, kind="publish", package="pk%d" % i), version="1.0.0", git_url="file:///tmp/x.git", commit="c%d" % i,
                     description="d%d" % i, type="skill") for i in range(3)]
        self.assertEqual(self.sy.ingest(pubs), 3)
        for i in range(3):
            self.assertEqual(self.r.package("pk%d" % i)["latest_version"], "1.0.0")        # visible with no git work at all
        self.assertEqual(len(self.sy._queue_read()), 3)
        calls = []
        self.sy.update_index = lambda items, *a, **k: calls.append(len(items)) or True
        self.sy.pull_index = lambda: None
        self.sy.flush_index()
        self.assertEqual(calls, [3])                                                        # ONE index commit for three publishes
        self.assertEqual(self.sy._queue_read(), [])
        self.sy.flush_index()
        self.assertEqual(calls, [3])                                                        # empty queue: no git at all
        self.assertEqual(self.sy.ingest(pubs), 0)                                           # redelivery: same version and commit, nothing queued
        self.assertEqual(self.sy._queue_read(), [])

    def test_same_version_same_commit_never_touches_git_but_a_new_commit_does(self):
        pub = dict(obs("p1", kind="publish", package="same"), version="1.0.0", git_url="file:///tmp/x.git", commit="aaa", type="tool")
        self.sy.ingest([pub])
        self.sy._queue_write([])
        self.sy.ingest([dict(pub, id="p2")])                                                # a "use" style repeat: nothing differs
        self.assertEqual(self.sy._queue_read(), [])
        self.sy.ingest([dict(pub, id="p3", commit="bbb")])                                  # the same version re-published at another commit
        self.assertEqual([i["commit"] for i in self.sy._queue_read()], ["bbb"])

    def test_publish_event_cannot_rewrite_an_existing_package(self):
        pid = self.r.package_upsert("owned", "skill", "the real description", ["a"], "readme", visibility="public")
        self.sy.ingest([dict(obs("p9", kind="publish", package="owned"), version="2.0.0", git_url="file:///tmp/x.git", commit="c",
                             description="HIJACKED", type="mcp")])
        p = self.r.package("owned")
        self.assertEqual((p["description"], p["type"]), ("the real description", "skill"))
        self.assertEqual(self.r.package_readme(pid), "readme")
        self.assertEqual(p["latest_version"], "2.0.0")

    def test_provisional_package_survives_an_index_pull_until_git_has_it(self):
        self.sy.ingest([dict(obs("p5", kind="publish", package="fresh"), version="1.0.0", git_url="file:///tmp/x.git", commit="c", type="tool")])
        self.sy.update_index = lambda *a, **k: False
        self.sy.read_index = lambda: (S.pkgindex.Legacy({}), "rev1")
        self.sy.pull_index()                                                                # git index does not list it yet
        self.assertEqual(self.r.package("fresh")["hidden"], 0)                              # still shown: it is queued, not removed

    def test_index_commit_failure_keeps_the_queue_and_is_reported(self):
        self.sy.ingest([dict(obs("p6", kind="publish", package="later"), version="1.0.0", git_url="file:///tmp/x.git", commit="c", type="tool")])
        self.sy.pull_index = lambda: None
        def boom(items, *a, **k):
            raise RuntimeError("index repository not reachable")
        self.sy.update_index = boom
        with self.assertRaises(RuntimeError):
            self.sy.run_once()
        self.assertIn("index repository not reachable", self.sy.status["last_error"])
        self.assertFalse(self.r.setting("sync_cursor"))
        self.assertEqual(self.r.package("later")["latest_version"], "1.0.0")                # the web still has it
        self.assertEqual(len(self.sy._queue_read()), 1)                                     # and the commit is retried next interval
        self.sy.update_index = lambda items, *a, **k: True
        self.sy.run_once()
        self.assertIsNone(self.sy.status["last_error"])
        self.assertEqual(self.sy._queue_read(), [])


    def test_windows_children_never_open_a_console_window(self):
        from aihub.cli import gitx, telemetry
        with unittest.mock.patch.object(gitx, "NO_WINDOW", 0x08000000):
            self.assertEqual(gitx.popen_kw(), {"creationflags": 0x08000000})
            with unittest.mock.patch.object(gitx.subprocess, "run") as fake:
                fake.return_value = unittest.mock.Mock(returncode=0, stdout="", stderr="")
                gitx.run(["--version"], check=False)
                self.assertEqual(fake.call_args.kwargs.get("creationflags"), 0x08000000)
        with unittest.mock.patch.object(telemetry, "WINDOWS", True), unittest.mock.patch.object(telemetry, "_acquire", return_value=True), \
                unittest.mock.patch.object(telemetry, "_backoff", return_value=False), \
                unittest.mock.patch.object(telemetry.subprocess, "Popen") as popen:
            telemetry.kick()
        flags = popen.call_args.kwargs["creationflags"]
        self.assertTrue(flags & 0x08000000)                                         # hidden console, shared by every git it runs
        self.assertFalse(flags & 0x00000008)                                        # NOT detached: that made each git open its own window


    def test_deliver_uses_kafka_else_the_spool_and_never_git(self):
        from aihub.cli import telemetry
        with unittest.mock.patch.object(telemetry, "kick", lambda: None):
            self.assertTrue(telemetry.deliver({"kind": "publish", "package": "demo", "version": "1.0.0"}))
            self.assertEqual(len(self.topic), 1)
            self.down = True
            self.assertFalse(telemetry.deliver({"kind": "publish", "package": "demo", "version": "1.0.1"}))
        self.assertIn("kafka", telemetry.deliver.error)
        self.assertTrue(os.path.getsize(os.path.join(self.home, "queue", "events.jsonl")) > 0)       # kept for the flusher
        self.assertFalse(hasattr(telemetry, "push_git"))

    def test_events_are_retried_with_backoff_when_kafka_is_down(self):
        from aihub.cli import telemetry
        self.spool(3)
        self.down = True
        self.assertEqual(telemetry.flush(force=True), 0)
        self.assertGreater(telemetry._backoff(), 0)
        self.down = False
        self.assertEqual(telemetry.flush(force=True), 3)

    def test_a_spool_past_its_cap_is_sealed_not_dropped(self):
        from aihub.cli import telemetry
        with unittest.mock.patch.object(telemetry, "MAX_SPOOL", 200):
            self.down = True
            self.spool(40)                                                                   # far more than 200 bytes
            self.assertEqual(telemetry.flush(force=True), 0)
            self.down = False
            os.path.exists(telemetry._backoff_file()) and os.remove(telemetry._backoff_file())
            self.assertEqual(telemetry.flush(force=True), 40)
        self.assertEqual(len(self.topic), 40)                                                # not one event lost

    def test_hub_username_and_local_user_are_recorded_as_sent(self):
        self.sy.ingest([obs("a1", user="alice"), obs("a2", user="~dan", local_user="dan"), obs("a3", user="")])
        rows = {r[0]: (r[1], r[2]) for r in self.r.db.conn().execute("SELECT ext_id, username, local_user FROM events WHERE ext_id IN ('a1','a2','a3')").fetchall()}
        self.assertEqual(rows["a1"], ("alice", "dan"))            # signed in: hub username AND the OS user are both kept
        self.assertEqual(rows["a2"], (None, "dan"))               # anonymous: no hub name, local user kept
        self.assertEqual(rows["a3"], (None, "dan"))

    def test_a_database_failure_on_a_publish_is_not_swallowed_so_kafka_redelivers(self):
        pub = dict(obs("p77", kind="publish", package="lostpkg"), version="1.0.0", git_url="file:///tmp/x.git", commit="c", type="tool")
        with unittest.mock.patch.object(type(self.r), "version_sync", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                self.sy.ingest([pub])
        self.assertEqual([i["package"] for i in self.sy._queue_read()], ["lostpkg"])        # already queued for git
        self.sy.ingest([pub])                                                                # redelivery completes it (the event row already exists)
        self.assertEqual(self.r.package("lostpkg")["latest_version"], "1.0.0")

    def test_usage_events_do_not_drop_the_cached_index_but_a_publish_does(self):
        cache = self.c.app.state.cache
        cache.set("index:anon", {"body": "{}"}, ttl=3600)
        cache.set("pkg:list:x", {"a": 1}, ttl=60)
        self.sy.ingest([obs("u1")])
        self.assertIsNone(cache.get("pkg:list:x"))                                     # counts/feed refreshed
        self.assertIsNotNone(cache.get("index:anon"))                                  # the CLI index is untouched
        self.sy.ingest([dict(obs("p1", kind="publish", package="newpkg"), version="1.0.0", git_url="file:///tmp/x.git", commit="c", type="tool")])
        self.assertIsNone(cache.get("index:anon"))

    def test_the_index_is_built_from_the_database_once_then_served_from_the_cache(self):
        self.sy.ingest([dict(obs("p1", kind="publish", package="cached"), version="1.0.0", git_url="file:///tmp/x.git", commit="c", type="tool")])
        n = {"c": 0}
        real = type(self.r).index_export
        def counted(repo, viewer=None):
            n["c"] += 1
            return real(repo, viewer)
        with unittest.mock.patch.object(type(self.r), "index_export", counted):
            first = self.c.get("/api/v1/index")
            for _ in range(5):
                self.c.get("/api/v1/index")
            self.sy.ingest([obs("u%d" % i) for i in range(5)])                          # usage only
            self.c.get("/api/v1/index")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(n["c"], 1)



class CountedSpans(unittest.TestCase):
    def test_package_page_and_home_count_installs_like_the_dashboard(self):
        """The package page / home read versions.downloads, which ingested installs never touched: they showed 0 while the dashboard
        and rankings (which count events) were right. Now all of them count install/update events, old ones included."""
        c = TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=tempfile.mkdtemp())))
        r = c.app.state.repos
        pid = r.package_upsert("demo", "skill", "d", [], visibility="public")
        r.version_sync(pid, "1.0.0", {"package": {"name": "demo", "version": "1.0.0"}})
        rows = [S.to_row(obs("i1", kind="install", version="1.0.0")), S.to_row(obs("i2", kind="install")), S.to_row(obs("u1"))]
        r.events_insert_ext([dict(x) for x in rows])
        r.events_insert_ext([dict(x) for x in rows])                   # a resent batch: deduped, no double count
        self.assertEqual(r.versions(pid)[0]["downloads"], 2)           # two installs (one without a version -> latest); a use does not count
        self.assertEqual(r.overview()["downloads"], 2)
        page = c.get("/api/v1/packages/demo").json()                   # what the package page and home cards show
        self.assertEqual(page["downloads"], 2)
        self.assertEqual(page["versions"][0]["downloads"], 2)
        self.assertEqual(c.get("/api/v1/packages?sort=downloads").json()["items"][0]["downloads"], 2)

    def test_events_are_idempotent_rows(self):
        rows = [S.to_row(obs("o9")), S.to_row(obs("o8"))]
        self.assertEqual(len(rows), 2)
        d = tempfile.mkdtemp()
        c = TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=d)))
        r = c.app.state.repos
        self.assertEqual(r.events_insert_ext([dict(x) for x in rows]), 2)
        self.assertEqual(r.events_insert_ext([dict(x) for x in rows]), 0)
