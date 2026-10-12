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
        self.assertEqual(self.c.put("/api/v1/admin/sync", json={"sync_interval": "*/10 * * * *", "telemetry_url": "https://git.example/t.git"}, headers=h).status_code, 200)
        g = self.c.get("/api/v1/admin/sync", headers=h).json()
        self.assertEqual(g["config"]["sync_interval"], "*/10 * * * *")
        self.assertEqual(g["config"]["telemetry_url"], "https://git.example/t.git")
        self.assertEqual(self.c.put("/api/v1/admin/sync", json={"sync_interval": "nope"}, headers=h).status_code, 400)
        self.assertEqual(self.c.put("/api/v1/admin/sync", json={"telemetry_archive": "maybe"}, headers=h).status_code, 400)
        self.assertEqual(self.c.put("/api/v1/admin/sync", json={"telemetry_archive": "0"}, headers=h).status_code, 200)
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
        from aihub.core import defaults
        self.assertEqual(r.json()["telemetry"]["url"], defaults.load()["telemetry"]["git_url"])      # bundled default
        self.assertEqual(r.json()["telemetry"]["branch"], defaults.load()["telemetry"]["branch"])
        self.assertNotIn("credentials", r.json())
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.c.put("/api/v1/admin/sync", headers=self.h, json={"telemetry_url": "https://git.example/t.git"})
        self.assertEqual(self.c.get("/api/v1/client-config").json()["telemetry"]["url"], "https://git.example/t.git")
        self.c.put("/api/v1/admin/sync", headers=self.h, json={"telemetry_branch": "usage"})
        self.assertEqual(self.c.get("/api/v1/client-config").json()["telemetry"]["branch"], "usage")


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


class Inbox(unittest.TestCase):
    """CLI -> bare git inbox -> server, with real git. No mocks between the two halves."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.inbox = os.path.join(tempfile.mkdtemp(), "inbox.git")
        subprocess.run(["git", "init", "-q", "--bare", self.inbox], check=True)
        self.env = unittest.mock.patch.dict(os.environ, {"AIHUB_HOME": self.home, "AIHUB_TELEMETRY_URL": self.inbox})
        self.env.start()
        self.c = TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=tempfile.mkdtemp())))
        self.r, self.sy = self.c.app.state.repos, self.c.app.state.sync

    def tearDown(self):
        self.env.stop()

    def git(self, *a):
        return subprocess.run(["git", "-C", self.inbox, *a], capture_output=True, text=True).stdout

    def refs(self):
        """What is still waiting for the server: files on the ONE inbox branch, plus any old-layout per-batch branch or hidden ref."""
        files = self.git("ls-tree", "-r", "--name-only", "telemetry-inbox", "--", "inbox/").split() if self.git("branch", "--list", "telemetry-inbox").strip() else []
        old = [x for x in self.git("for-each-ref", "--format=%(refname)", "refs/heads/telemetry/", "refs/telemetry/").split() if x]
        return files + old

    def branches(self):
        return sorted(b.strip("* ") for b in self.git("branch", "--list").split())

    def spool(self, n, **k):
        from aihub.cli import telemetry
        for i in range(n):
            telemetry.enqueue(dict({"kind": "use", "package": "demo", "component": "skill:x"}, **k))

    def test_batch_roundtrip_dedup_archive_and_cleanup(self):
        from aihub.cli import telemetry
        self.spool(50)
        self.assertEqual(telemetry.flush(force=True), 50)                         # 50 events -> ONE push
        self.assertEqual(len(self.refs()), 1)
        self.assertEqual(telemetry.flush(force=True), 0)                          # nothing spooled -> no push at all
        self.assertEqual(len(self.refs()), 1)
        self.sy.pull_events()
        self.assertEqual(self.r.db.conn().execute("SELECT COUNT(*) FROM events WHERE package='demo'").fetchone()[0], 50)
        self.assertEqual(self.refs(), [])                                         # ingested and archived: ref deleted
        arch = subprocess.run(["git", "-C", self.inbox, "ls-tree", "-r", "--name-only", S.ARCHIVE], capture_output=True, text=True).stdout.split()
        self.assertEqual(len(arch), 1)
        self.assertTrue(arch[0].startswith("archive/"))
        self.sy.pull_events()                                                     # idempotent
        self.assertEqual(self.r.db.conn().execute("SELECT COUNT(*) FROM events WHERE package='demo'").fetchone()[0], 50)

    def test_failed_push_keeps_events_and_resend_does_not_duplicate(self):
        from aihub.cli import telemetry
        self.spool(3)
        with unittest.mock.patch.dict(os.environ, {"AIHUB_TELEMETRY_URL": "/nonexistent/inbox.git"}):
            self.assertEqual(telemetry.flush(force=True), 0)                      # unreachable: nothing lost
        self.assertEqual(telemetry.flush(force=True), 3)                          # next pass delivers
        # crash after push, before the spool file was removed: the same events go out again
        import glob
        lines = [l for l in self.sent_lines()]
        telemetry.push(self.inbox, lines)
        self.assertEqual(len(self.refs()), 2)
        self.sy.pull_events()
        self.assertEqual(self.r.db.conn().execute("SELECT COUNT(*) FROM events WHERE package='demo'").fetchone()[0], 3)

    def sent_lines(self):
        f = self.git("ls-tree", "-r", "--name-only", "telemetry-inbox", "--", "inbox/").split()[0]
        return self.git("show", "telemetry-inbox:" + f).splitlines()

    def test_garbage_and_oversized_batches_are_dropped_without_blocking_good_ones(self):
        from aihub.cli import telemetry
        telemetry.push(self.inbox, ["not json", '{"kind":"use"}', json.dumps(obs("good1"))])
        self.sy.pull_events()
        self.assertEqual(self.r.db.conn().execute("SELECT COUNT(*) FROM events WHERE ext_id='good1'").fetchone()[0], 1)
        self.assertEqual(self.refs(), [])

    def test_archive_failure_keeps_refs_for_retry(self):
        from aihub.cli import telemetry
        telemetry.push(self.inbox, [json.dumps(obs("keep1"))])
        with unittest.mock.patch.object(S.Sync, "_archive", return_value=False):
            self.sy.pull_events()
        self.assertEqual(len(self.refs()), 1)                                     # not deleted: nothing may be lost
        self.sy.pull_events()
        self.assertEqual(self.refs(), [])
        self.assertEqual(self.r.db.conn().execute("SELECT COUNT(*) FROM events WHERE ext_id='keep1'").fetchone()[0], 1)

    def test_two_clients_push_without_conflict(self):
        from aihub.cli import telemetry
        for cid in ("aaa", "bbb"):
            with unittest.mock.patch("aihub.cli.hooks.client_id", return_value=cid):
                telemetry.push(self.inbox, [json.dumps(obs("ev-" + cid))])
        self.assertEqual(len(self.refs()), 2)
        self.sy.pull_events()
        self.assertEqual(self.r.db.conn().execute("SELECT COUNT(*) FROM events WHERE ext_id LIKE 'ev-%'").fetchone()[0], 2)

    def count(self, r=None, where="package='demo'"):
        return (r or self.r).db.conn().execute("SELECT COUNT(*) FROM events WHERE " + where).fetchone()[0]

    def test_repo_without_archive_branch_gets_it_created(self):
        from aihub.cli import telemetry
        self.assertEqual(subprocess.run(["git", "-C", self.inbox, "branch", "--list"], capture_output=True, text=True).stdout.strip(), "")
        self.spool(4)
        telemetry.flush(force=True)                                                # the CLI needs no branch at all
        self.sy.pull_events()
        branches = subprocess.run(["git", "-C", self.inbox, "branch", "--list"], capture_output=True, text=True).stdout
        self.assertIn(self.sy.archive_branch(), branches)

    def test_server_down_while_clis_keep_pushing_then_catches_up(self):
        from aihub.cli import telemetry
        for round_ in range(3):                                                    # server is "down": three batches pile up in git
            self.spool(5)
            telemetry.flush(force=True)
        self.assertEqual(len(self.refs()), 3)
        self.assertEqual(self.count(), 0)
        self.sy.run_once()                                                         # server comes back
        self.assertEqual(self.count(), 15)
        self.assertEqual(self.refs(), [])

    def test_lost_database_is_rebuilt_from_the_archive_branch(self):
        from aihub.cli import telemetry
        self.spool(7)
        telemetry.flush(force=True)
        self.sy.run_once()
        self.assertEqual(self.count(), 7)
        self.assertEqual(self.refs(), [])                                          # refs are gone: only the archive has the data now
        c2 = TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=tempfile.mkdtemp())))       # brand new, empty database
        self.assertEqual(self.count(c2.app.state.repos), 0)
        c2.app.state.sync.run_once()
        self.assertEqual(self.count(c2.app.state.repos), 7)
        c2.app.state.sync.run_once()                                               # idempotent
        self.assertEqual(self.count(c2.app.state.repos), 7)

    def test_database_wiped_but_cursor_survived_still_replays(self):
        from aihub.cli import telemetry
        self.spool(3)
        telemetry.flush(force=True)
        self.sy.run_once()
        self.r.db.conn().execute("DELETE FROM events"); self.r.db.conn().commit()
        self.sy._lookback_checked = False                                          # a restart
        self.sy.run_once()
        self.assertEqual(self.count(), 3)

    def test_forced_full_sync_replays_archive(self):
        from aihub.cli import telemetry
        self.spool(2)
        telemetry.flush(force=True)
        self.sy.run_once()
        self.r.db.conn().execute("DELETE FROM events"); self.r.db.conn().commit()
        self.r.set_setting("sync_cursor", "")                                      # admin: "re-read everything"
        self.sy.run_once()
        self.assertEqual(self.count(), 2)

    def test_all_clients_share_one_inbox_branch_however_long_the_server_is_down(self):
        """The point of the design: a long outage must not create a branch per batch."""
        from aihub.cli import telemetry
        for k in range(12):
            self.spool(2)
            telemetry.flush(force=True)
        self.assertEqual(self.branches(), ["telemetry-inbox"])                         # 12 batches, ONE branch
        self.assertEqual(len(self.refs()), 12)
        self.assertEqual(self.count(), 0)                                              # nothing ingested yet: the server is "down"
        self.sy.pull_events()
        self.assertEqual(self.count(), 24)
        self.assertEqual(self.refs(), [])                                              # inbox emptied...
        self.assertEqual(self.branches(), ["telemetry-archive", "telemetry-inbox"])    # ...and still just two branches
        arch = self.git("ls-tree", "-r", "--name-only", "telemetry-archive").split()
        self.assertEqual(len(arch), 12)

    def test_cli_fetches_only_the_tip_not_the_history(self):
        from aihub.cli import telemetry
        for k in range(5):
            self.spool(1)
            telemetry.flush(force=True)
        shallow = os.path.join(self.home, "queue", "git", "shallow")
        self.assertTrue(os.path.exists(shallow))                                       # depth-1 fetch: cost does not grow with history

    def test_racing_clients_both_land_and_server_trim_never_touches_new_files(self):
        from aihub.cli import telemetry
        real_run, raced = telemetry.gitx.run, []

        def run(args, *a, **k):
            # the instant before this client pushes, another client pushes first -> ours is rejected as non-fast-forward and must retry
            if args[:2] == ["push", "-q"] and not raced and str(args[2:3]) != "[]" and "telemetry-inbox" in " ".join(args):
                raced.append(1)
                with unittest.mock.patch("aihub.cli.hooks.client_id", return_value="other"):
                    telemetry.gitx.run = real_run
                    telemetry.push(self.inbox, [json.dumps(obs("raced-other"))])
                telemetry.gitx.run = run
            return real_run(args, *a, **k)
        telemetry.gitx.run = run
        try:
            with unittest.mock.patch("aihub.cli.hooks.client_id", return_value="mine"):
                telemetry.push(self.inbox, [json.dumps(obs("raced-mine"))])
        finally:
            telemetry.gitx.run = real_run
        self.assertEqual(raced, [1])
        self.assertEqual(len(self.refs()), 2)                                          # both files are on the one branch
        self.sy.pull_events()
        self.assertEqual(self.count(where="ext_id IN ('raced-mine','raced-other')"), 2)

    def test_batch_pushed_during_ingest_is_left_for_the_next_pass(self):
        """The server trims only the files it ingested; a file a CLI added after the server's fetch must survive."""
        from aihub.cli import telemetry
        telemetry.push(self.inbox, [json.dumps(obs("first"))])
        real = S.Sync._archive

        def archive_then_late_push(sy, d, batches):
            ok = real(sy, d, batches)
            with unittest.mock.patch("aihub.cli.hooks.client_id", return_value="late"):
                telemetry.push(self.inbox, [json.dumps(obs("late-one"))])
            return ok
        with unittest.mock.patch.object(S.Sync, "_archive", archive_then_late_push):
            self.sy.pull_events()
        self.assertEqual(self.count(where="ext_id='first'"), 1)
        self.assertEqual(len(self.refs()), 1)                                          # only the late file remains
        self.sy.pull_events()
        self.assertEqual(self.count(where="ext_id='late-one'"), 1)
        self.assertEqual(self.refs(), [])

    def test_previous_layout_branch_per_batch_is_still_ingested(self):
        """CLIs from the previous release pushed telemetry/<client>/<batch> branches: they must still be read and cleaned up."""
        from aihub.cli import telemetry
        d = telemetry._scratch()
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=d, input=json.dumps(obs("prev1")) + "\n", capture_output=True, text=True).stdout.strip()
        tree = subprocess.run(["git", "mktree"], cwd=d, input="100644 blob %s\tevents.jsonl\n" % blob, capture_output=True, text=True).stdout.strip()
        commit = subprocess.run(["git", "-c", "user.name=a", "-c", "user.email=a@b", "commit-tree", tree, "-m", "t"], cwd=d, capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "push", "-q", self.inbox, commit + ":refs/heads/telemetry/prevclient/1791000000-abcd1234"], cwd=d, check=True)
        self.spool(1)
        telemetry.flush(force=True)                                                    # plus one current-layout batch
        self.sy.pull_events()
        self.assertEqual(self.count(where="ext_id='prev1'"), 1)
        self.assertEqual(self.count(), 2)                                              # the old-layout event + the current-layout one
        self.assertEqual(self.refs(), [])
        self.assertEqual(self.branches(), ["telemetry-archive", "telemetry-inbox"])    # the old branch is gone

    def test_old_cli_refs_are_still_ingested_and_removed(self):
        """A CLI that has not updated yet pushes to refs/telemetry/*: nothing it sent may be lost."""
        from aihub.cli import telemetry
        d = telemetry._scratch()
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=d, input=json.dumps(obs("legacy1")) + "\n", capture_output=True, text=True).stdout.strip()
        tree = subprocess.run(["git", "mktree"], cwd=d, input="100644 blob %s\tevents.jsonl\n" % blob, capture_output=True, text=True).stdout.strip()
        commit = subprocess.run(["git", "-c", "user.name=a", "-c", "user.email=a@b", "commit-tree", tree, "-m", "t"], cwd=d, capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "push", "-q", self.inbox, commit + ":refs/telemetry/oldclient/1791000000-abcd1234"], cwd=d, check=True)
        self.spool(1)
        telemetry.flush(force=True)                                                # plus one new-style batch
        self.sy.pull_events()
        self.assertEqual(self.count(where="ext_id='legacy1'"), 1)
        self.assertEqual(self.count(), 2)                                          # the legacy event + the new-style one
        self.assertEqual(self.refs(), [])

    def test_failed_push_is_recorded_where_the_user_can_see_it(self):
        from aihub.cli import telemetry
        self.spool(1)
        with unittest.mock.patch.dict(os.environ, {"AIHUB_TELEMETRY_URL": "/nonexistent/inbox.git"}):
            telemetry.flush(force=True)
        st = telemetry.last_status()
        self.assertFalse(st["ok"])
        self.assertTrue(st["error"])
        telemetry.flush(force=True)                                                # back online
        self.assertTrue(telemetry.last_status()["ok"])
        self.assertEqual(telemetry.last_status()["last_sent"], 1)

    def test_unreachable_inbox_is_reported_and_does_not_stop_the_index_sync(self):
        """Repo down / no access: the admin page must say so (not 'OK'), the cursor must not claim success, and the package index still syncs."""
        self.sy.cfg = lambda key, env, default="": {"telemetry_url": "/nonexistent/inbox.git"}.get(key, default)
        indexed = []
        self.sy.pull_index = lambda: indexed.append(1)
        with self.assertRaises(RuntimeError):
            self.sy.run_once()
        self.assertEqual(indexed, [1])                                              # the other half still ran
        self.assertIn("usage sync", self.sy.status["last_error"])
        self.assertIsNotNone(self.sy.status["events_error"])
        self.assertIsNone(self.sy.status["index_error"])
        self.assertFalse(self.r.setting("sync_cursor"))                             # a failed pass is not "completed"
        self.assertTrue(self.sy.status["last_run"])

    def test_index_failure_is_reported_and_does_not_stop_usage_ingest(self):
        from aihub.cli import telemetry
        self.spool(3)
        telemetry.flush(force=True)
        def broken():
            raise RuntimeError("index: cannot reach git")
        self.sy.pull_index = broken
        with self.assertRaises(RuntimeError):
            self.sy.run_once()
        self.assertEqual(self.count(), 3)                                           # usage was still stored
        self.assertIn("cannot reach git", self.sy.status["last_error"])
        self.assertIsNotNone(self.sy.status["index_error"])
        self.assertIsNone(self.sy.status["events_error"])

    def test_error_persists_until_a_clean_pass_then_clears(self):
        from aihub.cli import telemetry
        good = self.sy.cfg
        self.sy.cfg = lambda key, env, default="": {"telemetry_url": "/nonexistent/inbox.git"}.get(key, default)
        self.sy.pull_index = lambda: None
        for _ in range(2):                                                          # repeated failures keep failing visibly
            with self.assertRaises(RuntimeError):
                self.sy.run_once()
            self.assertTrue(self.sy.status["last_error"])
        self.sy.cfg = good                                                          # repo is back
        self.spool(2)
        telemetry.flush(force=True)
        self.sy.run_once()
        self.assertIsNone(self.sy.status["last_error"])
        self.assertEqual(self.count(), 2)
        self.assertTrue(self.r.setting("sync_cursor"))

    def test_index_update_failure_after_ingest_is_reported_not_swallowed(self):
        """pull_events used to 'return False' here, so run_once cleared last_error and advanced the cursor while the package was never indexed."""
        from aihub.cli import telemetry
        telemetry.push(self.inbox, [json.dumps(dict(obs("pub1", kind="publish"), version="1.0.0", git_url="https://example.invalid/x.git"))])
        self.sy.pull_index = lambda: None
        def boom(pubs, *a, **k):
            raise RuntimeError("index repository not reachable")
        self.sy.update_index = boom
        with self.assertRaises(RuntimeError):
            self.sy.run_once()
        self.assertIn("index update", self.sy.status["last_error"])
        self.assertFalse(self.r.setting("sync_cursor"))
        self.assertEqual(self.count(where="ext_id='pub1'"), 1)                       # the event itself was stored
        self.assertEqual(len(self.refs()), 1)                                         # and the batch is still in git for the retry
        self.sy.update_index = lambda pubs, *a, **k: True                             # repo is back
        self.sy.run_once()
        self.assertIsNone(self.sy.status["last_error"])
        self.assertEqual(self.refs(), [])

    def test_archive_failure_is_visible_not_silent(self):
        from aihub.cli import telemetry
        self.sy.pull_index = lambda: None
        self.spool(2)
        telemetry.flush(force=True)
        with unittest.mock.patch.object(S.Sync, "_archive", lambda sy, d, b: sy.status.update(archive_error="archiving usage: boom") or False):
            with self.assertRaises(RuntimeError):
                self.sy.run_once()
        self.assertIn("archiving usage", self.sy.status["last_error"])                # used to be logged only; the admin page said OK
        self.assertEqual(len(self.refs()), 1)                                        # and the batch is still in git
        self.sy.run_once()                                                           # next pass succeeds and clears it
        self.assertIsNone(self.sy.status["last_error"])
        self.assertEqual(self.refs(), [])

    def test_leftover_archive_folder_from_a_killed_server_does_not_block_archiving(self):
        """A server stopped mid-archive left '.archive' behind; every later pass failed with 'already exists' and nothing was ever stored."""
        from aihub.cli import telemetry
        self.spool(3)
        telemetry.flush(force=True)
        self.sy.pull_events()                                                      # a normal pass creates the inbox clone
        d = self.sy._inbox_dir(self.sy.inbox())
        for kind in ("plain-folder", "registered-worktree"):
            stale = os.path.join(d, ".archive")
            os.makedirs(stale, exist_ok=True)
            open(os.path.join(stale, "junk.txt"), "w").write("left by a crash")
            if kind == "registered-worktree":
                subprocess.run(["git", "worktree", "add", "-q", "--detach", stale + "2"], cwd=d, capture_output=True)
            self.spool(2)
            telemetry.flush(force=True)
            self.sy.pull_events()
            self.assertEqual(self.refs(), [], kind)                                # archived, so the batch was removed
            self.assertFalse(os.path.exists(os.path.join(d, ".archive")), kind)  # and nothing is left behind
        self.assertEqual(self.count(), 3 + 2 + 2)

    def git_calls(self, fn):
        """Run fn, return the git sub-commands the server ran (gitx.run only; batched reads go through gitx._batch)."""
        real, seen = S.gitx.run, []

        def run(args, *a, **k):
            seen.append(args[0])
            return real(args, *a, **k)
        with unittest.mock.patch.object(S.gitx, "run", run):
            fn()
        return seen

    def test_idle_pass_costs_one_ls_remote_and_no_fetch(self):
        from aihub.cli import telemetry
        self.spool(2)
        telemetry.flush(force=True)
        self.sy.pull_events()
        calls = self.git_calls(self.sy.pull_events)                                   # nothing new since the last pass
        self.assertEqual(calls.count("ls-remote"), 1)
        self.assertNotIn("fetch", calls)
        self.assertNotIn("push", calls)
        self.assertNotIn("gc", calls)

    def test_many_batches_are_read_without_one_process_per_batch(self):
        from aihub.cli import telemetry
        for k in range(30):
            with unittest.mock.patch("aihub.cli.hooks.client_id", return_value="c%02d" % k):
                telemetry.push(self.inbox, [json.dumps(obs("many-%d" % k))])
        calls = self.git_calls(self.sy.pull_events)
        self.assertEqual(self.count(where="ext_id LIKE 'many-%'"), 30)
        self.assertEqual(self.refs(), [])
        self.assertLess(len(calls), 30)                                                # constant, not per batch
        self.assertEqual(len(self.git("ls-tree", "-r", "--name-only", "telemetry-archive").split()), 30)

    def test_archive_keeps_earlier_batches_and_retry_is_a_no_op(self):
        from aihub.cli import telemetry
        for k in range(3):
            telemetry.push(self.inbox, [json.dumps(obs("arch-%d" % k))])
            self.sy.pull_events()
        self.assertEqual(len(self.git("ls-tree", "-r", "--name-only", "telemetry-archive").split()), 3)
        head = self.git("rev-parse", "telemetry-archive").strip()
        d = self.sy._inbox_dir(self.sy.inbox())
        self.assertTrue(self.sy._archive(d, [("x-1790000000-ab", [json.dumps(obs("arch-0"))])]))
        self.sy._archive(d, [("x-1790000000-ab", [json.dumps(obs("arch-0"))])])     # same batch again: nothing new to commit
        self.assertEqual(len(self.git("rev-list", "telemetry-archive").split()), 4)
        self.assertNotEqual(head, self.git("rev-parse", "telemetry-archive").strip())

    def test_retried_publish_batch_still_triggers_an_index_reread(self):
        """Pass 1 stores the publish event but the index update fails; pass 2 inserts nothing new, yet the index changed and must be re-read."""
        from aihub.cli import telemetry
        telemetry.push(self.inbox, [json.dumps(dict(obs("pub2", kind="publish"), version="1.0.0", git_url="https://example.invalid/x.git"))])
        self.sy.update_index = lambda pubs, *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
        with self.assertRaises(RuntimeError):
            self.sy.pull_events()
        self.sy.update_index = lambda pubs, *a, **k: True
        self.assertTrue(self.sy.pull_events())
        self.assertEqual(self.refs(), [])

    def test_data_dir_with_a_literal_tilde_still_archives(self):
        """A .env saying AIHUB_DATA_DIR=~/.aihub/data is not expanded by any shell: git, run inside the repo folder, could not open the
        scratch files ('could not open ~/.aihub/data/.../.archive/0.jsonl'), so nothing was archived and the inbox was never emptied."""
        from aihub.cli import telemetry
        from aihub.server.config import Settings as St
        home = tempfile.mkdtemp()
        with unittest.mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}):
            st = St.load(env={"AIHUB_DATA_DIR": "~/.aihub/data", "AIHUB_TELEMETRY_URL": self.inbox}, env_file=os.devnull)
            self.assertEqual(st.data_dir, os.path.join(home, ".aihub", "data"))
            st.seed_builtin, st.sync_enabled = False, False
            c = TestClient(create_app(st))
            telemetry.push(self.inbox, [json.dumps(obs("tilde1"))])
            c.app.state.sync.pull_events()
        self.assertIsNone(c.app.state.sync.status["archive_error"])
        self.assertEqual(self.refs(), [])
        self.assertEqual(len(self.git("ls-tree", "-r", "--name-only", "telemetry-archive").split()), 1)

    def test_archive_result_is_reported_for_the_admin_page(self):
        from aihub.cli import telemetry
        self.spool(2)
        telemetry.flush(force=True)
        self.sy.pull_events()
        st = self.sy.status
        self.assertTrue(st["archive_last"])
        self.assertEqual(st["archive_batches"], 1)
        self.assertEqual(st["archive_files"], 1)
        self.assertEqual(st["archive_commit"], self.git("rev-parse", "telemetry-archive").strip()[:10])

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

    def test_deliver_pushes_now_or_spools(self):
        from aihub.cli import telemetry
        with unittest.mock.patch.object(telemetry, "kick", lambda: None):
            self.assertTrue(telemetry.deliver({"kind": "publish", "package": "demo", "version": "1.0.0"}))
            with unittest.mock.patch.dict(os.environ, {"AIHUB_TELEMETRY_URL": "/nonexistent/x.git"}):
                self.assertFalse(telemetry.deliver({"kind": "publish", "package": "demo", "version": "1.0.1"}))
        self.assertEqual(len(self.refs()), 1)
        self.assertTrue(os.path.getsize(os.path.join(self.home, "queue", "events.jsonl")) > 0)       # kept for the flusher


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
