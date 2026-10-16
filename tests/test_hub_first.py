"""Hub-first telemetry and index, the deferred one-push git writes, the gate between sync and backup, and the admin knobs."""
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import unittest.mock

from fastapi.testclient import TestClient

from aihub.cli import registry, telemetry
from aihub.server import sync as S
from aihub.server.config import Settings
from aihub.server.gitgate import Deferred, GitGate
from aihub.server.main import create_app


def ev(i, **k):
    return dict({"id": i, "kind": "use", "user": "", "package": "demo", "component": "skill:x", "client_id": "c1", "ts": time.time()}, **k)


def app(**kw):
    return TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=tempfile.mkdtemp(), **kw)))


def admin(c):
    c.post("/api/v1/auth/register", json={"username": "root", "password": "secret1"})
    return {"Authorization": "Bearer " + c.post("/api/v1/auth/login", json={"username": "root", "password": "secret1"}).json()["token"]}


class Gate(unittest.TestCase):
    def test_sync_defers_while_backup_just_ran_and_backup_waits_for_sync(self):
        g = GitGate(gap=300)
        with g.job("backup", wait_gap=True):
            pass                                                          # a backup just finished git work
        with g.job("sync") as j:
            with self.assertRaises(Deferred) as d:
                j.heavy()                                                 # sync must not touch git for ~5 minutes
            self.assertGreater(d.exception.left, 290)
        g.ended["backup"] -= 301                                          # 5 minutes later
        with g.job("sync") as j:
            j.heavy()                                                     # allowed again

    def test_idle_sync_pass_is_light_and_never_delays_the_backup(self):
        g = GitGate(gap=300)
        with g.job("sync"):
            pass                                                          # one ls-remote, no heavy() call
        t0 = time.time()
        with g.job("backup", wait_gap=True):
            pass
        self.assertLess(time.time() - t0, 1)

    def test_backup_waits_out_the_gap_after_real_sync_git_work(self):
        g = GitGate(gap=2)
        with g.job("sync") as j:
            j.heavy()
        t0 = time.time()
        with g.job("backup", wait_gap=True):
            pass
        self.assertGreaterEqual(time.time() - t0, 1.5)

    def test_force_skips_the_gap_but_not_a_running_job(self):
        g = GitGate(gap=300)
        with g.job("sync") as j:
            j.heavy()
        t0 = time.time()
        with g.job("backup", wait_gap=True, force=True):
            pass
        self.assertLess(time.time() - t0, 1)
        out = []
        with g.job("backup", wait_gap=True, force=True):
            th = threading.Thread(target=lambda: (g.job("sync", force=True).__enter__().heavy(), out.append(time.time())))
            th.start()
            time.sleep(0.4)
            self.assertEqual(out, [])                                     # the sync waits for the running backup
        th.join(5)
        self.assertTrue(out)

    def test_waiting_backup_makes_sync_step_aside(self):
        g = GitGate(gap=1)
        g.waiting["backup"] = True
        with g.job("sync") as j:
            with self.assertRaises(Deferred):
                j.heavy()


class Jitter(unittest.TestCase):
    def test_backup_delay_is_spread_but_bounded(self):
        c = app()
        bk, sy = c.app.state.backup, c.app.state.sync
        bk.delay = lambda: 3600
        vals = {round(bk.jittered()) for _ in range(40)}
        self.assertGreater(len(vals), 10)                                 # not a fixed pattern
        self.assertTrue(all(3600 - 361 <= v <= 3600 + 361 for v in vals))   # 10% of an hour
        bk.delay = lambda: 86400
        self.assertTrue(all(abs(bk.jittered() - 86400) <= 601 for _ in range(40)))   # capped at 600 s by default
        c.post("/api/v1/auth/register", json={"username": "root", "password": "secret1"})
        h = admin(c)
        self.assertEqual(c.put("/api/v1/admin/backup", json={"backup_jitter": "0"}, headers=h).status_code, 200)
        sy.forget()
        bk.delay = lambda: 3600
        self.assertEqual({round(bk.jittered()) for _ in range(10)}, {3600})
        self.assertEqual(c.put("/api/v1/admin/backup", json={"backup_jitter": "99"}, headers=h).status_code, 400)


class HttpIngest(unittest.TestCase):
    def setUp(self):
        self.c = app()
        self.h = admin(self.c)
        self.r, self.ing, self.sy = self.c.app.state.repos, self.c.app.state.ingest, self.c.app.state.sync
        self.n = lambda: self.r.db.conn().execute("SELECT COUNT(*) FROM events WHERE package='demo'").fetchone()[0]

    def tearDown(self):
        self.ing.close()

    def test_events_wait_in_memory_then_one_database_write(self):
        calls = []
        real = self.r.events_insert_ext
        self.r.events_insert_ext = lambda rows: (calls.append(len(rows)), real(rows))[1]
        for i in range(5):
            self.assertEqual(self.c.post("/api/v1/telemetry", json={"events": [ev("a%d" % i), ev("b%d" % i)]}).json()["accepted"], 2)
        self.assertEqual(self.n(), 0)                                     # nothing written per request
        self.assertEqual(len(self.ing.rows), 10)
        self.assertEqual(self.ing.flush(), 10)
        self.assertEqual(calls, [10])                                     # ONE transaction for ten events from five requests
        self.assertEqual(self.n(), 10)
        self.assertEqual(self.ing.flush(), 0)

    def test_window_runs_from_the_first_event_and_is_configurable(self):
        self.c.put("/api/v1/admin/sync", json={"ingest_db_interval": "1"}, headers=self.h)
        self.c.post("/api/v1/telemetry", json={"events": [ev("w1")]})
        self.assertEqual(self.n(), 0)
        for _ in range(40):
            if self.n():
                break
            time.sleep(0.1)
        self.assertEqual(self.n(), 1)                                     # written by the background window, no manual flush

    def test_resend_is_deduplicated_and_garbage_is_dropped(self):
        body = {"events": [ev("same"), ev("same"), {"kind": "nope", "package": "demo", "id": "x"}, "junk", ev("")]}
        self.assertEqual(self.c.post("/api/v1/telemetry", json=body).json()["accepted"], 2)
        self.ing.flush()
        self.c.post("/api/v1/telemetry", json={"events": [ev("same")]})
        self.ing.flush()
        self.assertEqual(self.n(), 1)

    def test_repeat_ids_skip_the_buffer(self):
        self.assertEqual(self.c.post("/api/v1/telemetry", json={"events": [ev("d1"), ev("d2")]}).json()["accepted"], 2)
        self.assertEqual(self.c.post("/api/v1/telemetry", json={"events": [ev("d1"), ev("d2"), ev("d3")]}).json()["accepted"], 3)
        self.assertEqual(len(self.ing.rows), 3)                           # d1, d2 were not buffered a second time
        self.assertEqual(self.ing.stats["duplicates"], 2)
        with unittest.mock.patch("aihub.server.ingest.SEEN_IDS", 2):
            self.c.post("/api/v1/telemetry", json={"events": [ev("e1"), ev("e2"), ev("e3")]})
        self.assertLessEqual(len(self.ing.seen), 3)                       # bounded: the oldest ids are forgotten

    def test_gzip_body_is_read_and_bad_gzip_is_refused(self):
        import gzip
        body = gzip.compress(json.dumps({"events": [ev("z1")]}).encode())
        r = self.c.post("/api/v1/telemetry", content=body, headers={"Content-Encoding": "gzip", "Content-Type": "application/json"})
        self.assertEqual(r.json()["accepted"], 1)
        self.assertEqual(self.ing.stats["gzip"], 1)
        bad = self.c.post("/api/v1/telemetry", content=b"not gzip", headers={"Content-Encoding": "gzip"})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.c.post("/api/v1/telemetry", content=b"{}", headers={"Content-Encoding": "br"}).status_code, 415)

    def test_answer_carries_index_etag_once_the_index_is_ready(self):
        self.assertTrue(self.c.post("/api/v1/telemetry", json={"events": []}).json()["index_etag"])   # built from the database: always there
        with unittest.mock.patch("aihub.server.routers._index_view", return_value={"etag": '"abc"'}):
            self.assertEqual(self.c.post("/api/v1/telemetry", json={"events": []}).json()["index_etag"], '"abc"')

    def test_429_and_503_are_counted(self):
        self.c.put("/api/v1/admin/sync", json={"telemetry_rate_limit": "1"}, headers=self.h)
        self.c.post("/api/v1/telemetry", json={"events": []})
        self.c.post("/api/v1/telemetry", json={"events": []})
        self.assertEqual(self.ing.stats["rate_limited"], 1)
        self.c.put("/api/v1/admin/sync", json={"telemetry_rate_limit": "0"}, headers=self.h)
        with unittest.mock.patch("aihub.server.ingest.HARD_ROWS", 1):
            self.c.post("/api/v1/telemetry", json={"events": [ev("c1"), ev("c2")]})
            self.c.post("/api/v1/telemetry", json={"events": [ev("c3")]})
        self.assertEqual(self.ing.stats["busy"], 1)
        self.assertEqual(self.c.get("/api/v1/admin/sync", headers=self.h).json()["status"]["ingest"]["busy"], 1)

    def _journal(self):
        d = self.ing.journal_dir
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def test_acknowledged_events_survive_a_crash_before_the_database_write(self):
        self.c.post("/api/v1/telemetry", json={"events": [ev("j1"), ev("j2")]})
        self.assertEqual(self.n(), 0)                                     # acknowledged, only in memory + journal
        self.assertTrue(self._journal())
        from aihub.server.ingest import Ingest
        reborn = Ingest(self.r, self.sy, None, self.ing.journal_dir)      # a new process after kill -9: the memory is gone
        self.assertEqual(len(reborn.rows), 2)
        self.assertEqual(reborn.flush(), 2)
        self.assertEqual(self.n(), 2)
        self.assertEqual(self._journal(), [])                             # journal removed only after the commit
        self.assertEqual(Ingest(self.r, self.sy, None, self.ing.journal_dir).rows, [])

    def test_journal_is_removed_after_flush_and_kept_when_the_database_fails(self):
        self.c.post("/api/v1/telemetry", json={"events": [ev("m1")]})
        with unittest.mock.patch.object(self.r, "events_insert_ext", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                self.ing.flush()
        self.assertTrue(self._journal())                                  # still on disk
        self.c.post("/api/v1/telemetry", json={"events": [ev("m2")]})     # new traffic during the outage is journaled too
        self.assertEqual(self.ing.flush(), 2)
        self.assertEqual(self._journal(), [])

    def test_torn_journal_line_is_ignored(self):
        self.c.post("/api/v1/telemetry", json={"events": [ev("t1")]})
        with open(os.path.join(self.ing.journal_dir, "current.jsonl"), "ab") as f:
            f.write(b'{"id":"half","kind":"us')                            # crash in the middle of a write: never acknowledged
        from aihub.server.ingest import Ingest
        self.assertEqual(len(Ingest(self.r, self.sy, None, self.ing.journal_dir).rows), 1)

    def test_unjournalable_batch_is_refused_not_acknowledged(self):
        with unittest.mock.patch.object(self.ing, "_append", side_effect=OSError("disk full")):
            r = self.c.post("/api/v1/telemetry", json={"events": [ev("x1")]})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(self.ing.rows, [])
        self.assertEqual(self.c.post("/api/v1/telemetry", json={"events": [ev("x1")]}).json()["accepted"], 1)   # the retry is not taken for a repeat

    def test_signed_in_sender_cannot_claim_another_name(self):
        self.c.post("/api/v1/telemetry", json={"events": [ev("u1", user="someone-else")]}, headers=self.h)
        self.ing.flush()
        self.assertEqual(self.r.db.conn().execute("SELECT username FROM events WHERE ext_id='u1'").fetchone()[0], "root")

    def test_bad_requests(self):
        self.assertEqual(self.c.post("/api/v1/telemetry", content=b"nope").status_code, 400)
        self.assertEqual(self.c.post("/api/v1/telemetry", json={"events": "x"}).status_code, 400)
        self.assertEqual(self.c.post("/api/v1/telemetry", json={"events": [0] * 5001}).status_code, 400)

    def test_rate_limit_answers_429_with_retry_after_and_is_admin_tunable(self):
        self.c.put("/api/v1/admin/sync", json={"telemetry_rate_limit": "3"}, headers=self.h)
        codes = [self.c.post("/api/v1/telemetry", json={"events": []}).status_code for _ in range(5)]
        self.assertEqual(codes, [200, 200, 200, 429, 429])
        r = self.c.post("/api/v1/telemetry", json={"events": []})
        self.assertGreaterEqual(int(r.headers["Retry-After"]), 1)
        self.c.put("/api/v1/admin/sync", json={"telemetry_rate_limit": "0"}, headers=self.h)
        self.assertEqual(self.c.post("/api/v1/telemetry", json={"events": []}).status_code, 200)

    def test_full_buffer_answers_503(self):
        with unittest.mock.patch("aihub.server.ingest.HARD_ROWS", 2):
            self.c.post("/api/v1/telemetry", json={"events": [ev("f1"), ev("f2"), ev("f3")]})
            r = self.c.post("/api/v1/telemetry", json={"events": [ev("f4")]})
        self.assertEqual(r.status_code, 503)
        self.assertIn("Retry-After", r.headers)

    def test_failed_database_write_keeps_events(self):
        self.c.post("/api/v1/telemetry", json={"events": [ev("k1"), ev("k2")]})
        with unittest.mock.patch.object(self.r, "events_insert_ext", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                self.ing.flush()
        self.assertEqual(len(self.ing.rows), 2)
        self.assertEqual(self.ing.flush(), 2)

    def test_admin_limits_validate_and_report_values_in_use(self):
        g = self.c.get("/api/v1/admin/sync", headers=self.h).json()
        self.assertEqual(g["config"]["telemetry_git_interval"], "3600")
        self.assertEqual(g["config"]["index_git_interval"], "1800")
        self.assertEqual(g["config"]["git_job_gap"], "300")
        self.assertTrue({"telemetry_git_interval", "index_git_interval", "git_job_gap", "ingest_db_interval", "telemetry_rate_limit",
                         "backup_jitter"} <= {x["key"] for x in g["limits"]})
        put = lambda k, v: self.c.put("/api/v1/admin/sync", json={k: v}, headers=self.h).status_code
        self.assertEqual(put("telemetry_git_interval", "7200"), 200)
        self.assertEqual(self.c.get("/api/v1/admin/sync", headers=self.h).json()["config"]["telemetry_git_interval"], "7200")
        self.assertEqual(put("telemetry_git_interval", "999999"), 400)
        self.assertEqual(put("index_git_interval", "-1"), 400)
        self.assertEqual(put("git_job_gap", "abc"), 400)
        self.assertEqual(put("telemetry_git_interval", ""), 200)          # blank = default again
        self.assertEqual(self.c.get("/api/v1/admin/sync", headers=self.h).json()["config"]["telemetry_git_interval"], "3600")


class SingleFlight(unittest.TestCase):
    def test_concurrent_misses_compute_once(self):
        from aihub.server.cache import MemoryCache
        c, calls, out = MemoryCache(), [], []
        def build():
            calls.append(1)
            time.sleep(0.2)
            return {"v": 1}
        ts = [threading.Thread(target=lambda: out.append(c.get_or_set("k", 30, build))) for _ in range(20)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(calls), 1)                                   # 20 readers, one database query
        self.assertEqual(out, [{"v": 1}] * 20)

    def test_value_computed_across_an_invalidate_is_not_stored(self):
        from aihub.server.cache import MemoryCache
        c = MemoryCache()
        self.assertEqual(c.get_or_set("pkg:a", 30, lambda: (c.invalidate("pkg"), "old")[1]), "old")
        self.assertIsNone(c.get("pkg:a"))                                 # a write landed mid-query: do not cache the stale answer


class Inbox(unittest.TestCase):
    """HTTP events reach git in few, large pushes."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.inbox = os.path.join(tempfile.mkdtemp(), "inbox.git")
        subprocess.run(["git", "init", "-q", "--bare", self.inbox], check=True)
        self.env = unittest.mock.patch.dict(os.environ, {"AIHUB_HOME": self.home, "AIHUB_TELEMETRY_URL": self.inbox})
        self.env.start()
        self.c = app()
        self.h = admin(self.c)
        self.sy, self.ing, self.r = self.c.app.state.sync, self.c.app.state.ingest, self.c.app.state.repos
        self.r.set_setting("git_telemetry_sync", "1"); self.r.set_setting("git_index_sync", "1")                       # legacy git sync is off by default

    def tearDown(self):
        self.ing.close()
        self.env.stop()

    def git(self, *a):
        return subprocess.run(["git", "-C", self.inbox, *a], capture_output=True, text=True).stdout

    def pushes(self):
        """Count `git push` commands the server runs."""
        real, seen = S.gitx.run, []

        def spy(args, *a, **k):
            if args and args[0] == "push":
                seen.append(args)
            return real(args, *a, **k)
        return spy, seen

    def post(self, ids):
        self.c.post("/api/v1/telemetry", json={"events": [ev(i) for i in ids]})
        self.ing.flush()

    def test_http_events_hold_until_the_git_window_then_leave_in_one_push(self):
        spy, seen = self.pushes()
        with unittest.mock.patch.object(S.gitx, "run", spy):
            for k in range(6):
                self.post(["h%d-%d" % (k, j) for j in range(3)])
                self.sy.run_once(scheduled=True)                          # the loop runs often; git must stay untouched
            self.assertEqual(seen, [])
            self.assertIsNotNone(self.sy.git_flush_in())                  # a push is queued for the window
            self.assertEqual(self.git("branch", "--list").strip(), "")    # nothing reached git yet
            self.sy.run_once()                                            # "Sync now" writes everything held
        self.assertEqual(len(seen), 1)                                    # 6 requests x 3 events -> ONE push
        files = self.git("ls-tree", "-r", "--name-only", S.ARCHIVE).split()
        self.assertEqual(len(files), 1)
        self.assertEqual(len(self.git("show", "%s:%s" % (S.ARCHIVE, files[0])).strip().splitlines()), 18)
        self.assertIsNone(self.sy.git_flush_in())

    def test_window_is_admin_configurable_and_due_flush_happens_in_the_loop(self):
        self.c.put("/api/v1/admin/sync", json={"telemetry_git_interval": "0"}, headers=self.h)
        self.post(["d1", "d2"])
        self.sy.run_once(scheduled=True)
        self.assertEqual(len(self.git("ls-tree", "-r", "--name-only", S.ARCHIVE).split()), 1)

    def test_cli_files_archive_and_trim_use_one_atomic_push(self):
        self.env2 = unittest.mock.patch.dict(os.environ, {"AIHUB_TELEMETRY_VIA": "git"})
        self.env2.start()
        try:
            for n in range(3):
                with unittest.mock.patch("aihub.cli.hooks.client_id", return_value="cli%d" % n):
                    telemetry.enqueue(ev("g%d" % n))
                    telemetry.flush(force=True)
        finally:
            self.env2.stop()
        self.assertEqual(len(self.git("ls-tree", "-r", "--name-only", "telemetry-inbox", "--", "inbox/").split()), 3)
        spy, seen = self.pushes()
        with unittest.mock.patch.object(S.gitx, "run", spy):
            self.sy.pull_events()
        self.assertEqual(len(seen), 1)                                    # archive commit + inbox clean-up in ONE push (used to be two)
        self.assertIn("--atomic", seen[0])
        self.assertEqual(self.git("ls-tree", "-r", "--name-only", "telemetry-inbox", "--", "inbox/").split(), [])
        self.assertEqual(self.r.db.conn().execute("SELECT COUNT(*) FROM events WHERE package='demo'").fetchone()[0], 3)

    def test_events_are_read_once_while_waiting_for_the_window(self):
        env = unittest.mock.patch.dict(os.environ, {"AIHUB_TELEMETRY_VIA": "git"})
        env.start()
        try:
            telemetry.enqueue(ev("once"))
            telemetry.flush(force=True)
        finally:
            env.stop()
        self.sy.pull_events(scheduled=True)
        spy, seen = self.pushes()
        with unittest.mock.patch.object(S.gitx, "run", spy):
            self.sy.pull_events(scheduled=True)                           # same file still in the inbox, already stored and held
        self.assertEqual(seen, [])
        self.assertEqual(len(self.sy.held), 1)                            # not held twice
        self.sy.pull_events()
        self.assertEqual(self.git("ls-tree", "-r", "--name-only", "telemetry-inbox", "--", "inbox/").split(), [])

    def test_backup_gap_defers_git_work_without_error_and_catches_up_later(self):
        self.post(["x1"])
        self.sy.gate.ended["backup"] = time.time()                        # a backup just finished
        self.c.put("/api/v1/admin/sync", json={"telemetry_git_interval": "0"}, headers=self.h)
        self.sy.run_once(scheduled=True)
        self.assertIsNone(self.sy.status["last_error"])                   # waiting is not a failure
        self.assertIn("backup", self.sy.status["deferred"])
        self.assertEqual(self.git("branch", "--list").strip(), "")
        self.assertEqual(self.r.db.conn().execute("SELECT COUNT(*) FROM events WHERE package='demo'").fetchone()[0], 1)    # events are safe
        self.assertGreater(self.sy.next_wait(), 1)
        self.sy.gate.ended["backup"] = 0
        self.sy.defer_until = 0
        self.sy.run_once(scheduled=True)
        self.assertEqual(len(self.git("ls-tree", "-r", "--name-only", S.ARCHIVE).split()), 1)
        self.assertIsNone(self.sy.status["deferred"])


class StagedPublish(unittest.TestCase):
    """A publish is served by the hub at once; the index repo gets all of them in ONE push after `index_git_interval`."""

    def git(self, cwd, *a):
        return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()

    def make_pkg(self, name, ver):
        d = tempfile.mkdtemp()
        open(os.path.join(d, "aihub.toml"), "w").write('[package]\nname = "%s"\nversion = "%s"\ndescription = "d"\ntype = "skill"\n' % (name, ver))
        self.git(d, "init", "-q", "-b", "main")
        self.git(d, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
        self.git(d, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "p")
        return d, self.git(d, "rev-parse", "HEAD")

    def setUp(self):
        t = tempfile.mkdtemp()
        self.remote = os.path.join(t, "index.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.remote], check=True)
        inbox = os.path.join(t, "inbox.git")
        subprocess.run(["git", "init", "-q", "--bare", inbox], check=True)
        self.env = unittest.mock.patch.dict(os.environ, {"AIHUB_INDEX_URL": "file://" + self.remote, "AIHUB_TELEMETRY_URL": inbox})
        self.env.start()
        self.c = app()
        self.h = admin(self.c)
        self.sy, self.ing = self.c.app.state.sync, self.c.app.state.ingest
        self.c.app.state.repos.set_setting("git_index_sync", "1")   # legacy git index sync is off by default
        self.allow = unittest.mock.patch.object(S, "SCHEMES", S.SCHEMES + ("file://",))
        self.allow.start()

    def tearDown(self):
        self.ing.close()
        self.allow.stop()
        self.env.stop()

    def publish(self, name, ver):
        d, commit = self.make_pkg(name, ver)
        self.c.post("/api/v1/telemetry", json={"events": [ev("p-" + name, kind="publish", package=name, version=ver, git_url="file://" + d, commit=commit)]})
        self.ing.flush()

    def commits(self):
        out = subprocess.run(["git", "-C", self.remote, "rev-list", "--count", "main"], capture_output=True, text=True)
        return int(out.stdout.strip() or 0) if out.returncode == 0 else 0

    def test_publish_is_served_now_and_pushed_once_later(self):
        self.sy.run_once()                                                # creates the empty index in git
        base = self.commits()
        for n in ("alpha-pkg", "beta-pkg", "gamma-pkg"):
            self.publish(n, "1.0.0")
        self.sy.run_once(scheduled=True)                                  # the loop: stage + serve, no push yet
        self.assertEqual(self.commits(), base)                            # git untouched
        self.assertEqual(sorted(p["name"] for p in self.c.get("/api/v1/index/catalog", headers=self.h).json()["packages"]),
                         ["alpha-pkg", "beta-pkg", "gamma-pkg"])          # but the hub already serves all three
        self.assertEqual(self.c.get("/api/v1/index/packages/beta-pkg", headers=self.h).json()["latest_version"], "1.0.0")
        self.assertEqual(len(self.sy.staged), 3)
        self.assertIsNotNone(self.sy.index_push_in())
        self.assertEqual(self.sy.status["index_error"], None)
        self.c.put("/api/v1/admin/sync", json={"index_git_interval": "0"}, headers=self.h)    # window over
        self.sy.run_once(scheduled=True)
        self.assertEqual(self.commits(), base + 1)                        # three publishes, ONE commit, ONE push
        self.assertEqual(len(self.sy.staged), 0)
        names = self.git(self.remote, "ls-tree", "-r", "--name-only", "main").split()
        self.assertEqual(len([n for n in names if n.startswith("index/packages/")]), 3)
        self.sy.run_once(scheduled=True)
        self.assertEqual(self.commits(), base + 1)                        # nothing more to push

    def test_window_runs_from_the_first_publish_and_a_manual_sync_pushes_now(self):
        self.sy.run_once()
        base = self.commits()
        self.publish("solo-pkg", "1.0.0")
        self.sy.run_once(scheduled=True)
        first = self.sy.staged_first
        time.sleep(0.05)
        self.publish("late-pkg", "1.0.0")
        self.sy.run_once(scheduled=True)
        self.assertEqual(self.sy.staged_first, first)                     # the second publish does not restart the clock
        self.assertLessEqual(self.sy.index_push_in(), 1800)
        self.sy.run_once()                                                # "Sync now"
        self.assertEqual(self.commits(), base + 1)

    def test_a_push_that_loses_a_race_is_redone_on_the_new_tip(self):
        self.sy.run_once()
        self.publish("race-pkg", "1.0.0")
        self.sy.run_once(scheduled=True)
        other = tempfile.mkdtemp()                                        # someone else pushes to the index repo meanwhile
        self.git(other, "clone", "-q", self.remote, "w")
        w = os.path.join(other, "w")
        open(os.path.join(w, "NOTE"), "w").write("x")
        self.git(w, "add", "-A")
        self.git(w, "-c", "user.name=o", "-c", "user.email=o@o", "commit", "-q", "-m", "other")
        self.git(w, "push", "-q", "origin", "HEAD:main")
        self.sy.run_once()
        names = self.git(self.remote, "ls-tree", "-r", "--name-only", "main").split()
        self.assertIn("NOTE", names)                                      # their commit survived
        self.assertTrue(any(n.endswith("race-pkg.json") for n in names))  # and ours landed on top
        self.assertEqual(len(self.sy.staged), 0)


class IndexRoutes(unittest.TestCase):
    def setUp(self):
        self.c = app()
        self.h = admin(self.c)
        self.r, self.sy = self.c.app.state.repos, self.c.app.state.sync
        self.add("pub-one")
        self.add("secret-one", visibility="private")

    def add(self, name, **kw):
        pid = self.r.package_upsert(name, "skill", "d", [], "", **kw)
        self.r.version_sync(pid, "1.0.0", {"package": {"name": name, "version": "1.0.0", "type": "skill"}, "requires": {},
                                           "repo": {"url": "https://example.com/x.git", "ref": "abc"}})
        self.c.app.state.cache.invalidate("pkg")

    def test_etag_and_304_and_visibility(self):
        a = self.c.get("/api/v1/index/catalog")
        self.assertEqual(a.status_code, 200)
        self.assertEqual([p["name"] for p in a.json()["packages"]], ["pub-one"])         # private packages are not listed to anonymous
        self.assertEqual(self.c.get("/api/v1/index/catalog", headers={"If-None-Match": a.headers["ETag"]}).status_code, 304)
        full = self.c.get("/api/v1/index/catalog", headers=self.h)
        self.assertEqual(sorted(p["name"] for p in full.json()["packages"]), ["pub-one", "secret-one"])   # the admin sees both
        self.assertNotEqual(full.headers["ETag"], a.headers["ETag"])                      # different view, different tag
        self.assertEqual(self.c.get("/api/v1/index/catalog", headers={"If-None-Match": a.headers["ETag"], **self.h}).status_code, 200)

    def test_entry_etag_304_and_private_looks_missing(self):
        e = self.c.get("/api/v1/index/packages/pub-one")
        self.assertEqual(e.status_code, 200)
        self.assertEqual(e.json()["name"], "pub-one")
        self.assertEqual(self.c.get("/api/v1/index/packages/pub-one", headers={"If-None-Match": e.headers["ETag"]}).status_code, 304)
        self.assertEqual(self.c.get("/api/v1/index/packages/secret-one").status_code, 404)
        self.assertEqual(self.c.get("/api/v1/index/packages/secret-one", headers=self.h).status_code, 200)

    def test_new_publish_changes_the_etag_only_then(self):
        t1 = self.c.get("/api/v1/index/catalog").headers["ETag"]
        self.assertEqual(self.c.get("/api/v1/index/catalog").headers["ETag"], t1)
        self.add("pub-two")
        self.assertNotEqual(self.c.get("/api/v1/index/catalog").headers["ETag"], t1)

    def test_unchanged_index_does_not_query_the_database(self):
        self.c.get("/api/v1/index/catalog")
        with unittest.mock.patch.object(type(self.r), "index_view", side_effect=AssertionError("db queried")):
            a = self.c.get("/api/v1/index/catalog")
            self.assertEqual(a.status_code, 200)
            self.assertEqual(self.c.get("/api/v1/index/catalog", headers={"If-None-Match": a.headers["ETag"]}).status_code, 304)

    def test_client_config_tells_the_cli_about_the_hub_route(self):
        j = self.c.get("/api/v1/client-config").json()
        self.assertTrue(j["hub"]["telemetry"] and j["hub"]["index"])
        self.c.put("/api/v1/admin/sync", json={"client_telemetry_interval": "300", "client_index_ttl": "120"}, headers=self.h)
        j = self.c.get("/api/v1/client-config").json()
        self.assertEqual((j["hub"]["telemetry_interval"], j["hub"]["index_ttl"]), (300, 120))


class CliSending(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = unittest.mock.patch.dict(os.environ, {"AIHUB_HOME": self.home, "AIHUB_TELEMETRY_VIA": "hub", "AIHUB_URL": "https://hub.example", "AIHUB_TELEMETRY_URL": "/some/inbox.git"})
        self.env.start()
        telemetry.paths.ensure("queue")
        telemetry.enqueue(ev("c1"))

    def tearDown(self):
        self.env.stop()

    def test_hub_first_and_git_is_not_touched(self):
        with unittest.mock.patch.object(telemetry, "post", return_value=1) as post, unittest.mock.patch.object(telemetry, "push") as push:
            self.assertEqual(telemetry._send_once(), 1)
        post.assert_called_once()
        push.assert_not_called()
        self.assertEqual(telemetry.last_status()["via"], "hub")
        self.assertFalse(os.path.exists(telemetry._spool()))

    def test_busy_hub_backs_off_with_retry_after_and_does_not_fall_back_to_git(self):
        with unittest.mock.patch.object(telemetry, "post", side_effect=telemetry.HubBusy("hub busy (429)", 900)), \
                unittest.mock.patch.object(telemetry, "push") as push:
            self.assertEqual(telemetry._send_once(), 0)
        push.assert_not_called()
        self.assertGreaterEqual(telemetry._backoff(), 800)                # Retry-After is the floor
        self.assertTrue(os.path.exists(telemetry._spool()) or telemetry.glob.glob(telemetry.paths.p("queue", "sending.*.jsonl")))   # kept

    def test_git_is_only_used_after_repeated_hub_failures(self):
        down = OSError("connection refused")
        for fails in (0, 1):
            with open(telemetry._backoff_file(), "w") as f:
                json.dump({"fails": fails, "next": 0}, f)
            with unittest.mock.patch.object(telemetry, "post", side_effect=down), unittest.mock.patch.object(telemetry, "push") as push:
                telemetry._send_once()
            push.assert_not_called()                                      # a short hub restart must not move the fleet to git
        with open(telemetry._backoff_file(), "w") as f:
            json.dump({"fails": 2, "next": 0}, f)
        with unittest.mock.patch.object(telemetry, "post", side_effect=down), unittest.mock.patch.object(telemetry, "push") as push:
            self.assertEqual(telemetry._send_once(), 1)
        push.assert_called_once()                                         # hub has been down for a while: git is the fallback
        self.assertEqual(telemetry.last_status()["via"], "git")
        self.assertEqual(telemetry._backoff_state()[0], 0)

    def test_old_hub_without_the_route_goes_straight_to_git_after_the_threshold(self):
        import io
        import urllib.error
        err = urllib.error.HTTPError("u", 404, "nf", {}, io.BytesIO(b""))
        with unittest.mock.patch("aihub.cli.api._opener.open", side_effect=err):
            with self.assertRaises(OSError):
                telemetry.post(["{}"])

    def test_events_survive_when_everything_is_down(self):
        with unittest.mock.patch.object(telemetry, "post", side_effect=OSError("down")), \
                unittest.mock.patch.object(telemetry, "push", side_effect=RuntimeError("git down")):
            for _ in range(3):
                telemetry._send_once()
        self.assertTrue(telemetry.glob.glob(telemetry.paths.p("queue", "sending.*.jsonl")) or os.path.exists(telemetry._spool()))
        self.assertGreater(telemetry._backoff_state()[0], 0)

    def test_git_only_mode(self):
        with unittest.mock.patch.dict(os.environ, {"AIHUB_TELEMETRY_VIA": "git"}):
            with unittest.mock.patch.object(telemetry, "post") as post, unittest.mock.patch.object(telemetry, "push") as push:
                self.assertEqual(telemetry._send_once(), 1)
        post.assert_not_called()
        push.assert_called_once()

    def test_backoff_never_exceeds_an_hour_and_is_jittered(self):
        seen = set()
        for _ in range(30):
            telemetry._ok()
            telemetry._fail()
            seen.add(round(telemetry._backoff_state()[1] - time.time()))
        self.assertGreater(len(seen), 5)
        self.assertTrue(all(15 <= x <= 30 for x in seen))


class CliIndex(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = unittest.mock.patch.dict(os.environ, {"AIHUB_HOME": self.home, "AIHUB_INDEX_VIA": "hub", "AIHUB_INDEX_URL": ""})
        self.env.start()
        registry._cache.clear()
        registry.paths.save("config.json", {"hub": "https://hub.example"})

    def tearDown(self):
        self.env.stop()
        registry._cache.clear()

    def fake(self, log, etag='"v1"'):
        import io
        import urllib.error

        cat = {"packages": [{"name": "one", "type": "skill", "latest_version": "1.0.0", "description": "d", "tags": [], "h": "abc"}]}

        class R(io.BytesIO):
            headers = {"ETag": etag}
            def __enter__(s): return s
            def __exit__(s, *a): pass

        def opener(rq, timeout=None):
            log.append((rq.full_url, rq.get_header("If-none-match")))
            if rq.get_header("If-none-match") == etag:
                raise urllib.error.HTTPError(rq.full_url, 304, "nm", {}, io.BytesIO(b""))
            if rq.full_url.endswith("/index/catalog"):
                return R(json.dumps(cat).encode())
            return R(json.dumps({"name": "one", "type": "skill", "latest_version": "1.0.0", "versions": [{"version": "1.0.0"}]}).encode())
        return opener

    def test_index_comes_from_the_hub_and_is_revalidated_with_304(self):
        log = []
        with unittest.mock.patch("aihub.cli.api._opener.open", self.fake(log)), \
                unittest.mock.patch.object(registry.gitx, "fetch", side_effect=AssertionError("git must not be used")):
            self.assertEqual([p["name"] for p in registry.search("")], ["one"])
            self.assertEqual(registry.get("one")["latest_version"], "1.0.0")
            self.assertEqual(len(log), 2)
            registry._cache.clear()
            reader = registry.reader()                                    # inside the trust window: no request at all
            self.assertEqual(len(log), 2)
            self.assertEqual(reader.count, 1)
            os.utime(os.path.join(self.home, "index-hub", "catalog.json"), (1, 1))      # trust window over
            registry._cache.clear()
            registry.reader()
        self.assertEqual(log[-1][1], '"v1"')                              # revalidated, not downloaded again
        self.assertEqual(len(log), 3)

    def test_falls_back_to_git_when_the_hub_cannot_serve_it(self):
        registry.paths.save("config.json", {"hub": "https://hub.example", "index_url": "https://git.example/i.git"})
        with unittest.mock.patch("aihub.cli.api._opener.open", side_effect=OSError("down")), \
                unittest.mock.patch.object(registry.gitx, "fetch", side_effect=registry.gitx.GitError("git was asked")), \
                unittest.mock.patch.object(registry.gitx, "remote_empty", return_value=False):
            with self.assertRaises(registry.api.ApiError) as e:
                registry.reader(refresh=True)
        self.assertIn("git was asked", str(e.exception))

    def test_a_pinned_index_url_in_the_environment_skips_the_hub(self):
        with unittest.mock.patch.dict(os.environ, {"AIHUB_INDEX_URL": "/some/dir"}), \
                unittest.mock.patch("aihub.cli.api._opener.open", side_effect=AssertionError("hub must not be asked")):
            self.assertIsNone(registry._hub_reader({"url": "/some/dir"}))


if __name__ == "__main__":
    unittest.main()
