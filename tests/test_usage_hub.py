"""Token usage on the hub: upload (idempotent), per person, per group (a person in two groups counts once), group visibility, and the
background uploader fired by the Claude Code Stop hook."""
import datetime
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from aihub.cli import hooks, usagesync
from aihub.server.config import Settings
from aihub.server.main import create_app

API = "/api/v1"
TODAY = datetime.datetime.now(datetime.timezone.utc).date()
D = lambda n: (TODAY - datetime.timedelta(days=n)).isoformat()


def row(day, model="claude-opus-5", tool="claude", out=100, cost=1.0, req=2, **k):
    return dict({"day": day, "tool": tool, "model": model, "requests": req, "input": 10, "output": out, "cache_write": 5, "cache_read": 20,
                 "cost": cost, "unpriced": 0}, **k)


class HubBase(unittest.TestCase):
    """Users ann/bob/eve/root and groups red (ann,bob), blue (ann), green (eve). No tests here, so subclasses do not re-run each other's."""
    def setUp(self):
        self.c = TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=tempfile.mkdtemp())))
        self.h = {}
        for n in ("root", "ann", "bob", "eve"):
            self.c.post(API + "/auth/register", json={"username": n, "password": "secret1"})
            self.h[n] = {"Authorization": "Bearer " + self.c.post(API + "/auth/login", json={"username": n, "password": "secret1"}).json()["token"]}
        self.g = {}
        for n, members in (("red", ["ann", "bob"]), ("blue", ["ann"]), ("green", ["eve"])):
            self.c.post(API + "/groups", json={"name": n}, headers=self.h["root"])
            self.c.post(API + "/groups/%s/members" % n, json={"usernames": members}, headers=self.h["root"])
            self.g[n] = next(x["id"] for x in self.c.get(API + "/usage/groups", headers=self.h["root"]).json()["groups"] if x["name"] == n)

    def up(self, who, rows, client="dev1"):
        return self.c.post(API + "/usage/sync", json={"client_id": client, "rows": rows}, headers=self.h[who])

    def get(self, who, **q):
        self.c.app.state.usagebuf.flush()                                                # the page does this itself (read-your-writes)
        return self.c.get(API + "/usage", params=q, headers=self.h[who])


class Hub(HubBase):
    def test_upload_needs_login_and_resend_never_double_counts(self):
        self.assertEqual(self.c.post(API + "/usage/sync", json={"rows": []}).json()["matched"], False)     # anonymous may upload (view_usage), but nobody matches
        self.assertEqual(self.up("ann", [row(D(0))]).json()["stored"], 1)
        self.up("ann", [row(D(0))])                                              # same day again: overwritten
        t = self.get("ann").json()["totals"]
        self.assertEqual((t["requests"], t["outp"], t["cost"]), (2, 100, 1.0))
        self.up("ann", [row(D(0), out=250, req=5, cost=2.5)])                    # a later, larger total replaces it
        t = self.get("ann").json()["totals"]
        self.assertEqual((t["requests"], t["outp"]), (5, 250))
        self.up("ann", [row(D(0), out=50)], client="dev2")                        # a second device ADDS
        self.assertEqual(self.get("ann").json()["totals"]["outp"], 300)

    def test_a_day_never_shrinks_when_old_logs_are_deleted(self):
        self.up("ann", [row(D(40), out=900, req=9), row(D(0), out=100)])
        self.up("ann", [row(D(40), out=10, req=1)])                                             # the tool pruned its logs: smaller total
        self.assertEqual(self.get("ann", days=90).json()["totals"]["outp"], 1000)               # ...ignored, history stays
        self.up("ann", [row(D(40), out=900, req=9, cost=0.5)])                                  # equal total: replaced (new prices apply)
        self.assertEqual(self.get("ann", days=90).json()["totals"]["cost"], 1.5)

    def test_bad_rows_are_dropped_not_fatal(self):
        r = self.up("ann", [row(D(0)), {"day": "nope"}, row(D(0), model=""), row(D(1), out="x"), "str", row(D(2), out=-5)]).json()
        self.assertEqual((r["stored"], r["received"]), (2, 6))                     # the good one, and the negative number clamped to 0

    def test_group_counts_a_person_in_two_groups_once(self):
        self.up("ann", [row(D(0), out=100)])
        self.up("bob", [row(D(0), out=10, model="claude-sonnet-5")])
        d = self.get("ann", scope="groups").json()                                 # ann is in red and blue; bob in red
        self.assertEqual((d["totals"]["outp"], d["people"]), (110, 2))             # ann once, not twice
        by = {g["name"]: g for g in d["by_group"]}
        self.assertEqual((by["red"]["outp"], by["blue"]["outp"]), (110, 100))      # per group each sees its own members
        self.assertEqual({u["username"] for u in d["by_user"]}, {"ann", "bob"})
        self.assertEqual({m["model"] for m in d["by_model"]}, {"claude-opus-5", "claude-sonnet-5"})

    def test_choose_groups_and_filter(self):
        self.up("ann", [row(D(0), out=100)])
        self.up("bob", [row(D(0), out=10, model="claude-sonnet-5", tool="codex")])
        red = self.get("ann", scope="groups", group=str(self.g["red"])).json()
        blue = self.get("ann", scope="groups", group=str(self.g["blue"])).json()
        self.assertEqual((red["totals"]["outp"], blue["totals"]["outp"]), (110, 100))
        self.assertEqual(self.get("ann", scope="groups", tool="codex").json()["totals"]["outp"], 10)
        self.assertEqual(self.get("ann", scope="groups", model="claude-opus-5").json()["totals"]["outp"], 100)
        self.assertEqual(self.get("ann", scope="groups", user="bob").json()["totals"]["outp"], 10)

    def test_visibility(self):
        self.up("eve", [row(D(0), out=999)])
        self.assertEqual(self.get("ann", scope="groups", group=str(self.g["green"])).status_code, 403)   # not her group
        self.assertEqual(self.get("ann", scope="groups").json()["totals"]["outp"], 0)                  # eve's usage is invisible
        self.assertEqual(self.get("ann").json()["totals"]["outp"], 0)                                  # "me" is only me
        self.assertTrue(self.get("nobody-token" and "eve", scope="groups").json()["totals"]["outp"] == 999)
        self.assertEqual(self.c.get(API + "/usage").status_code, 401)
        self.assertEqual({g["name"] for g in self.c.get(API + "/usage/groups", headers=self.h["root"]).json()["groups"]}, {"red", "blue", "green"})  # admin: all
        self.assertEqual({g["name"] for g in self.c.get(API + "/usage/groups", headers=self.h["ann"]).json()["groups"]}, {"red", "blue"})
        self.assertEqual(self.get("ann", scope="x").status_code, 400)

    def test_window(self):
        self.up("ann", [row(D(0), out=1), row(D(10), out=2), row(D(40), out=4)])
        self.assertEqual(self.get("ann", days=7).json()["totals"]["outp"], 1)
        self.assertEqual(self.get("ann", days=30).json()["totals"]["outp"], 3)
        r = self.get("ann", **{"from": D(45), "to": D(30)}).json()
        self.assertEqual(r["totals"]["outp"], 4)
        self.assertEqual(self.get("ann", **{"from": D(1), "to": D(5)}).status_code, 400)


class Permission(HubBase):
    def anon_up(self, name, rows=None):
        return self.c.post(API + "/usage/sync", json={"client_id": "x", "local_user": name, "rows": rows or [row(D(0))]})

    def test_view_usage_is_in_the_matrix_and_on_for_every_role_including_anonymous(self):
        roles = {x["name"]: x["permissions"] for x in self.c.get(API + "/admin/roles", headers=self.h["root"]).json()["detail"]}
        for r in ("admin", "user", "publisher", "anonymous"):
            self.assertIn("view_usage", roles[r], r)
        self.assertIn("view_usage", self.c.get(API + "/meta").json()["anonymous_perms"])           # the web reads this when signed out
        self.assertIn("view_usage", {p["key"] for p in self.c.get(API + "/admin/roles", headers=self.h["root"]).json()["permissions"]})

    def test_switching_it_off_blocks_web_and_cli(self):
        self.c.put(API + "/admin/roles/user", json={"permissions": ["publish", "review"]}, headers=self.h["root"])
        self.assertEqual(self.get("ann").status_code, 403)
        self.assertEqual(self.c.get(API + "/usage/groups", headers=self.h["ann"]).status_code, 403)
        self.assertEqual(self.c.get(API + "/usage/sessions", headers=self.h["ann"]).status_code, 403)
        self.assertEqual(self.up("ann", [row(D(0))]).status_code, 403)
        self.assertEqual(self.get("root").status_code, 200)                                          # admin keeps it
        self.c.put(API + "/admin/roles/anonymous", json={"permissions": []}, headers=self.h["root"])
        self.assertEqual(self.anon_up("ann").status_code, 401)

    def test_cli_without_login_is_matched_by_normalized_name_and_the_group_sees_it(self):
        self.assertEqual(self.anon_up("Ann").json()["matched"], True)                                # OS user `Ann` = account ann
        self.assertEqual(self.anon_up("ann@corp.com", [row(D(1))]).json()["stored"], 1)
        self.assertEqual(self.get("ann").json()["totals"]["requests"], 4)
        red = self.get("bob", scope="groups", group=str(self.g["red"])).json()                       # bob is in ann's group
        self.assertEqual((red["totals"]["requests"], [x["username"] for x in red["by_user"]]), (4, ["ann"]))
        self.assertEqual(self.get("eve", scope="groups").json()["totals"]["requests"], 0)
        self.assertEqual(self.anon_up("stranger").json(), {"stored": 0, "matched": False, "received": 1})
        self.assertEqual(self.anon_up("").json()["matched"], False)
        self.assertEqual(self.anon_up("anonymous").json()["matched"], False)

    def test_localize_off_needs_the_exact_name(self):
        self.c.put(API + "/admin/sync", json={"localize_users": "0"}, headers=self.h["root"])
        self.assertEqual(self.anon_up("Ann").json()["matched"], False)
        self.assertEqual(self.anon_up("ann").json()["matched"], True)


class Uploader(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        for p in (mock.patch.dict(os.environ, {"HOME": self.home, "USERPROFILE": self.home, "AIHUB_HOME": os.path.join(self.home, ".aihub"),
                                               "AIHUB_URL": "http://hub.invalid"}),):
            p.start()
            self.addCleanup(p.stop)
        from aihub.cli import paths
        paths.ensure()
        paths.save("credentials.json", {"token": "t", "username": "ann"}, private=True)
        self.recs = [{"ts": time.time(), "tool": "claude", "session": "s", "model": "claude-opus-5", "input": 1, "output": 2, "cache_write": 0, "cache_read": 3}]

    def test_stop_hook_only_kicks_and_spools_nothing(self):
        with mock.patch.object(usagesync, "kick") as k, mock.patch.object(hooks, "_guard") as g, mock.patch.object(hooks.telemetry, "record") as rec:
            hooks.handle(json.dumps({"hook_event_name": "Stop", "session_id": "x"}))
            self.assertEqual((k.call_count, g.call_count, rec.call_count), (1, 0, 0))
            hooks.handle(json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "hi"}))
            self.assertEqual(k.call_count, 1)

    def test_kick_is_fast_and_starts_one_worker(self):
        with mock.patch("subprocess.Popen") as pop:
            t = time.time()
            for _ in range(5):
                usagesync.kick()
            self.assertLess(time.time() - t, 1.0)
            self.assertEqual(pop.call_count, 1)                                  # the lock stops a burst of turns from spawning more
            self.assertIn("usage-sync", pop.call_args[0][0])
        usagesync._release()

    def test_off_when_disabled_and_not_signed_in_still_uploads(self):
        from aihub.cli import paths
        paths.save("credentials.json", {}, private=True)
        with mock.patch("subprocess.Popen") as pop:
            usagesync.kick()
            self.assertEqual(pop.call_count, 1)                                  # no login: the hub matches the OS user name
        usagesync._release()
        paths.save("credentials.json", {"token": "t", "username": "ann"}, private=True)
        with mock.patch.dict(os.environ, {"AIHUB_USAGE_SYNC": "off"}), mock.patch("subprocess.Popen") as pop:
            usagesync.kick()
            self.assertEqual(pop.call_count, 0)

    def test_push_sends_only_changes(self):
        sent = []
        with mock.patch.object(usagesync, "_post", side_effect=lambda c, rows, sessions=(): sent.append((rows, sessions)) or True):
            self.assertEqual(usagesync.push(self.recs), 2)                       # one daily row + that day's one session summary
            self.assertEqual(usagesync.push(self.recs), 0)                       # nothing changed: nothing sent
            self.recs.append(dict(self.recs[0], output=5))
            self.assertEqual(usagesync.push(self.recs), 2)                       # the day (and its session) grew: sent again
        self.assertEqual([r[0][0]["output"] for r in sent], [2, 7])
        self.assertEqual(set(sent[0][0][0]), {"day", "tool", "model", "requests", "input", "output", "cache_write", "cache_read", "cost", "unpriced"})
        self.assertEqual(len(sent[0][1]), 1)

    def test_shrunk_day_is_not_sent_and_routine_passes_look_only_at_recent_days(self):
        old = dict(self.recs[0], ts=time.time() - 40 * 86400, output=50, session="s-old")
        sent = []
        with mock.patch.object(usagesync, "_post", side_effect=lambda c, rows, sessions=(): sent.append(rows) or True):
            self.assertEqual(usagesync.push(self.recs + [old]), 4)                  # first pass is full: two days and their two sessions
            self.assertEqual(usagesync.push([old], full=True), 0)                    # logs of the recent day are gone: nothing resent, hub keeps it
            gone = dict(old, output=5)                                               # an old day whose log lost lines
            self.assertEqual(usagesync.push([gone], full=True), 0)                   # smaller than what was sent: skipped
            self.recs[0]["output"] = 99
            self.assertEqual(usagesync.push(self.recs + [dict(old, output=500)]), 2)  # routine: the changed old day is NOT looked at
        self.assertEqual(len(sent), 2)

    def test_hooks_are_rechecked_at_most_every_few_minutes_and_a_deleted_one_comes_back(self):
        from aihub.cli import integrations
        with mock.patch.object(hooks, "verify") as v:
            self.assertTrue(hooks.verify_soon())
            self.assertFalse(hooks.verify_soon())                                    # a burst of hook calls checks once
            self.assertEqual(v.call_count, 1)
            os.utime(usagesync._q("hooks.checked"), (0, 0))
            self.assertTrue(hooks.verify_soon())
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        c = integrations.ClaudeCode()
        cmd = "/x/aihub hook"
        c.install_hook(cmd)
        p = os.path.join(self.home, ".claude", "settings.json")
        d = json.load(open(p))
        del d["hooks"]["Stop"]                                                         # someone hand-edits our Stop hook away
        json.dump(d, open(p, "w"))
        self.assertFalse(c.has_hook(cmd))
        os.utime(usagesync._q("hooks.checked"), (0, 0))
        with mock.patch.object(hooks, "hook_cmd", return_value=cmd), mock.patch.object(hooks.integrations, "detected", return_value=[c]):
            hooks.handle(json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "hi"}))
        self.assertTrue(c.has_hook(cmd))

    def test_failure_backs_off_and_keeps_rows_for_next_time(self):
        with mock.patch.object(usagesync, "push", side_effect=OSError("down")):
            usagesync.run(window=0)
        self.assertGreater(usagesync._state()["retry_at"], time.time())
        with mock.patch("subprocess.Popen") as pop:
            usagesync.kick()
            self.assertEqual(pop.call_count, 0)                                  # backing off: the hook does not even start a process
        self.assertFalse(os.path.exists(usagesync._q("usage.lock")))


if __name__ == "__main__":
    unittest.main()


class Scale(unittest.TestCase):
    """A thousand people uploading at once: every acknowledged row is stored exactly once, no day shrinks, the database is written in a few
    large transactions (not one per request), and a crash between the 2xx and the write loses nothing."""

    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.c = TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=self.data)))
        self.repos, self.buf = self.c.app.state.repos, self.c.app.state.usagebuf
        self.c.post(API + "/auth/register", json={"username": "root", "password": "secret1"})
        self.c.put(API + "/admin/sync", json={"telemetry_rate_limit": "0"},
                   headers={"Authorization": "Bearer " + self.c.post(API + "/auth/login", json={"username": "root", "password": "secret1"}).json()["token"]})
        # a thousand accounts straight into the database (registering 1000 users over HTTP would measure password hashing, not usage)
        from aihub.server.security import hash_token, new_token
        self.tok, c = {}, self.repos.db.conn()
        for i in range(1000):
            uid = self.repos.user_create("u%04d" % i, "x")
            t = new_token()
            self.repos.token_add(hash_token(t), uid, "api", "t")
            self.tok[i] = (uid, {"Authorization": "Bearer " + t})

    def tearDown(self):
        self.buf.close()

    def total(self):
        return self.repos.db.conn().execute("SELECT COUNT(*),COALESCE(SUM(outp),0),COUNT(DISTINCT user_id) FROM usage_daily").fetchone()

    def test_thousand_clients_group_commit(self):
        import threading
        from collections import Counter
        writes = Counter()
        real = self.repos.usage_upsert_many
        self.repos.usage_upsert_many = lambda by, sessions=None: (writes.update(["txn"]), real(by, sessions))[1]
        days, per_day = 5, 3
        expect = {}                                                                  # what the hub acknowledged: (user, day, model) -> largest output

        def user(i):
            uid, h = self.tok[i]
            for rep in range(3):                                                    # three turns: the day total grows, with resends
                rows = [row(D(d), model="m%d" % m, out=100 * (rep + 1) + i, req=rep + 1) for d in range(days) for m in range(per_day)]
                for attempt in range(40):
                    r = self.c.post(API + "/usage/sync", json={"client_id": "dev%d" % i, "rows": rows}, headers=h)
                    if r.status_code == 200:
                        for x in rows:
                            k = (uid, x["day"], x["model"])
                            expect[k] = max(expect.get(k, 0), x["output"])
                        break
                    self.assertIn(r.status_code, (429, 503))
                    time.sleep(0.05)
                else:
                    self.fail("hub never accepted an upload")
                if rep == 1:
                    self.c.post(API + "/usage/sync", json={"client_id": "dev%d" % i, "rows": [row(D(0), model="m0", out=1)]}, headers=h)   # pruned logs: smaller

        stop = threading.Event()
        fl = threading.Thread(target=lambda: [self.buf.flush() or time.sleep(0.05) for _ in iter(stop.is_set, True)])
        fl.start()
        ts = [threading.Thread(target=lambda lo=lo: [user(i) for i in range(lo, 1000, 25)]) for lo in range(25)]
        t0 = time.time()
        [t.start() for t in ts]
        [t.join() for t in ts]
        stop.set()
        fl.join()
        self.buf.flush()
        took = time.time() - t0
        n, outp, people = self.total()
        self.assertEqual(people, 1000, "buffer=%d stats=%s" % (len(self.buf.rows), self.buf.stats))
        self.assertEqual(n, 1000 * days * per_day)                                   # one row per key: resends never double
        self.assertEqual(outp, sum(expect.values()))                                  # the largest acknowledged total per key, nothing shrank
        self.assertLess(writes["txn"], 400, writes)                                   # a few big transactions, not one per request (3000+ requests)
        print("\n1000 clients x 3 uploads x %d rows: %.1fs, %d db transactions, %d rows" % (days * per_day, took, writes["txn"], n))

    def test_crash_after_ack_loses_nothing(self):
        uid, h = self.tok[7]
        self.assertEqual(self.c.post(API + "/usage/sync", json={"client_id": "d", "rows": [row(D(0), out=777)]}, headers=h).status_code, 200)
        self.assertEqual(self.total()[0], 0)                                         # acknowledged, still only in memory + journal
        from aihub.server.usagebuf import UsageBuffer
        again = UsageBuffer(self.repos, self.c.app.state.sync, None, os.path.join(self.data, "usage-journal"))   # the process died and restarted
        self.assertEqual(again.flush(), 1)
        self.assertEqual(self.repos.db.conn().execute("SELECT outp FROM usage_daily WHERE user_id=?", (uid,)).fetchone()[0], 777)
        self.assertEqual(again.flush(), 0)
        self.assertEqual(os.listdir(os.path.join(self.data, "usage-journal")), [])   # journal removed only after the commit

    def test_failed_write_keeps_rows_and_unwritable_journal_is_503(self):
        uid, h = self.tok[3]
        self.c.post(API + "/usage/sync", json={"client_id": "d", "rows": [row(D(0), out=50)]}, headers=h)
        with mock.patch.object(self.repos, "usage_upsert_many", side_effect=OSError("db busy")):
            self.assertEqual(self.buf.flush(), 0)
        self.assertEqual(self.buf.flush(), 1)                                        # the rows were kept, the next window writes them
        with mock.patch.object(self.buf, "_append", side_effect=OSError("disk full")):
            r = self.c.post(API + "/usage/sync", json={"client_id": "d", "rows": [row(D(1), out=5)]}, headers=h)
            self.assertEqual(r.status_code, 503)                                    # not acknowledged: the CLI keeps its state and retries
            self.assertIn("Retry-After", r.headers)

    def test_overload_refuses_instead_of_growing(self):
        from aihub.server import usagebuf
        uid, h = self.tok[1]
        with mock.patch.object(usagebuf, "HARD_KEYS", 5):
            for d in range(6):
                self.buf.add(uid, "x", [dict(row(D(d)), inp=10, outp=1, cache_write=0, cache_read=0, cost=0, unpriced=0)])
            r = self.c.post(API + "/usage/sync", json={"client_id": "d", "rows": [row(D(0))]}, headers=h)
            self.assertEqual(r.status_code, 503)
        self.assertGreaterEqual(self.buf.stats["busy"], 1)


class Backpressure(unittest.TestCase):
    def test_default_rate_limit_answers_429_with_retry_after_and_the_cli_backs_off(self):
        c = TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=tempfile.mkdtemp())))
        c.post(API + "/auth/register", json={"username": "ann", "password": "secret1"})
        h = {"Authorization": "Bearer " + c.post(API + "/auth/login", json={"username": "ann", "password": "secret1"}).json()["token"]}
        c.app.state.ingest.hits.clear()
        c.app.state.sync.num_cache["telemetry_rate_limit"] = (time.time(), 3)         # three requests a minute for this test
        codes = [c.post(API + "/usage/sync", json={"client_id": "d", "rows": [row(D(0))]}, headers=h) for _ in range(6)]
        self.assertEqual([r.status_code for r in codes], [200, 200, 200, 429, 429, 429])
        self.assertGreaterEqual(int(codes[3].headers["Retry-After"]), 1)
        # the CLI side: a 429 is "busy", not a failure of the data, and the retry delay honours Retry-After
        import io, urllib.error
        e = urllib.error.HTTPError("u", 429, "x", {"Retry-After": "90"}, io.BytesIO(b"{}"))
        with mock.patch("urllib.request.urlopen", side_effect=e), mock.patch("aihub.cli.api._opener.open", side_effect=e):
            home = tempfile.mkdtemp()
            with mock.patch.dict(os.environ, {"HOME": home, "AIHUB_HOME": home + "/.aihub", "AIHUB_URL": "http://hub.invalid"}):
                from aihub.cli import paths
                paths.ensure()
                paths.save("credentials.json", {"token": "t", "username": "ann"}, private=True)
                with self.assertRaises(usagesync.Busy) as cm:
                    usagesync._post("c", [{"day": D(0)}])
                self.assertEqual(cm.exception.retry_after, 90.0)
                usagesync._fail(cm.exception.retry_after)
                self.assertGreaterEqual(usagesync._state()["retry_at"] - time.time(), 89)   # waits at least what the hub asked


def sess(sid, out=1000, req=10, peak=50000, **k):
    return dict({"sid": sid, "day": D(0), "tool": "claude", "model": "claude-opus-5", "start": time.time() - 600, "end": time.time(), "requests": req,
                 "input": 100, "output": out, "cache_write": 200, "cache_read": 5000, "cost": 2.0, "unpriced": 0, "peak": peak,
                 "cold": 0, "compact": 0, "big": 0, "light": 0}, **k)


class Sessions(HubBase):
    def up_s(self, who, rows, client="dev1"):
        return self.c.post(API + "/usage/sync", json={"client_id": client, "rows": [], "sessions": rows}, headers=self.h[who])

    def sessions(self, who, **q):
        self.c.app.state.usagebuf.flush()
        return self.c.get(API + "/usage/sessions", params=q, headers=self.h[who]).json()

    def test_session_shows_peak_context_and_never_shrinks(self):
        self.up_s("ann", [sess("a" * 24, out=900, peak=180000, big=4)])
        s = self.sessions("ann")
        self.assertEqual((s["sessions"][0]["peak"], s["sessions"][0]["id"], s["totals"]["big_sessions"]), (180000, "a" * 8, 1))
        self.up_s("ann", [sess("a" * 24, out=5, peak=10)])                          # the tool pruned its log: smaller, ignored
        self.assertEqual(self.sessions("ann")["sessions"][0]["outp"], 900)
        self.up_s("ann", [sess("a" * 24, out=1500, peak=90000)])                    # grew; the peak context only goes up
        x = self.sessions("ann")["sessions"][0]
        self.assertEqual((x["outp"], x["peak"]), (1500, 180000))

    def test_hints_from_the_same_rules_as_the_local_tips(self):
        rows = [sess("%024x" % i, req=1, cost=0.1) for i in range(9)]                                # nine one-shot sessions
        rows += [sess("b" * 24, cost=30.0, cold=3, big=5, peak=200000, light=0)]                      # one session dominates, cold rebuilds, big context
        self.up_s("ann", rows)
        ids = {h["id"]: h for h in self.sessions("ann")["hints"]}
        self.assertTrue({"tiny", "cold", "big", "top"} <= set(ids), set(ids))
        self.assertEqual(ids["top"]["sessions"], ["b" * 8])
        self.assertIn("Session " + "b" * 8, ids["top"]["detail"])
        self.assertNotIn("title", json.dumps(self.sessions("ann")["sessions"][0]))                  # no title, prompt or folder ever reaches the hub

    def test_bad_sessions_are_dropped_and_group_visibility_holds(self):
        r = self.up_s("eve", [sess("c" * 24, out=777), {"sid": "../../etc", "day": D(0)}, sess("d" * 24, tool=""), sess("e" * 24, out="x")]).json()
        self.assertEqual(r["received"], 4)
        self.assertEqual(len(self.sessions("eve")["sessions"]), 1)
        self.assertEqual(self.sessions("ann", scope="groups")["sessions"], [])                      # eve is not in ann's groups
        self.assertEqual(self.c.get(API + "/usage/sessions", params={"scope": "groups", "group": str(self.g["green"])}, headers=self.h["ann"]).status_code, 403)
        self.up_s("bob", [sess("f" * 24, out=42)])
        names = {x["username"] for x in self.sessions("ann", scope="groups")["sessions"]}
        self.assertEqual(names, {"bob"})                                                            # the group view lists members' sessions

    def test_cli_session_summary_is_numbers_only(self):
        home = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"HOME": home, "AIHUB_HOME": home + "/.aihub"}):
            now = time.time()
            recs = [{"ts": now - 900 + i * 400, "tool": "claude", "session": "/Users/me/secret-project/abc.jsonl", "model": "claude-opus-5",
                     "input": 10, "output": 5, "cache_write": 30000 if i == 2 else 0, "cache_read": 9000} for i in range(4)]
            out = usagesync.session_rows(recs)
        self.assertEqual(len(out), 1)
        text = json.dumps(out)
        self.assertNotIn("secret-project", text)
        self.assertNotIn("/Users", text)
        self.assertEqual((out[0]["requests"], out[0]["cold"]), (4, 1))
        self.assertRegex(out[0]["sid"], r"^[0-9a-f]{24}$")


def irow(day, model="claude-opus-5", tool="claude", out=100, cost=1.0, req=2):
    """A row as the buffer and Repos see it (after the route renamed input/output to inp/outp)."""
    return {"day": day, "tool": tool, "model": model, "requests": req, "inp": 10, "outp": out, "cache_write": 5, "cache_read": 20, "cost": cost, "unpriced": 0}


def isess(sid, out=1000, peak=50000):
    return {"sid": sid, "day": D(0), "tool": "claude", "model": "claude-opus-5", "started": 1.0, "ended": 2.0, "requests": 5, "inp": 10, "outp": out,
            "cache_write": 5, "cache_read": 20, "cost": 1.0, "unpriced": 0, "peak": peak, "cold": 0, "compact": 0, "big": 0, "light": 0}


def within(seconds, fn, *a):
    """Run fn in a thread; fail (instead of hanging the whole run) if it does not finish. A deadlock becomes a clear test failure."""
    import threading
    out, err = [], []
    def run():
        try:
            out.append(fn(*a))
        except BaseException as e:
            err.append(e)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise AssertionError("DEADLOCK or stall: %s did not finish in %ss" % (getattr(fn, "__name__", fn), seconds))
    if err:
        raise err[0]
    return out[0] if out else None


class NoDeadlock(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.c = TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=self.data)))
        self.repos, self.buf = self.c.app.state.repos, self.c.app.state.usagebuf
        from aihub.server.security import hash_token, new_token
        self.users = []
        for i in range(8):
            uid = self.repos.user_create("d%d" % i, "x")
            t = new_token()
            self.repos.token_add(hash_token(t), uid, "api", "t")
            self.users.append((uid, {"Authorization": "Bearer " + t}))
        self.c.app.state.ingest.hits.clear()
        self.c.app.state.sync.num_cache["telemetry_rate_limit"] = (time.time() + 1e9, 0)

    def tearDown(self):
        within(15, self.buf.close)                                                # a hung close() is reported, not waited for

    def rows(self, uid, n, out=10):
        return [irow(D(d), model="m%d" % (d % 3), out=out) for d in range(n)]

    def test_writers_in_opposite_orders_never_deadlock(self):
        """Two flushes of overlapping keys, submitted in opposite orders, plus a page reading all the while (the Postgres failure mode)."""
        import threading
        (u1, _), (u2, _) = self.users[0], self.users[1]
        stop, errs = threading.Event(), []

        def writer(a, b, rep):
            try:
                for i in range(rep):
                    self.repos.usage_upsert_many({(a, "x"): self.rows(a, 30, i), (b, "x"): self.rows(b, 30, i)}, {(a, "%024x" % i): isess("%024x" % i, out=i)})
            except Exception as e:
                errs.append(e)

        def reader():
            while not stop.is_set():
                self.repos.usage_query(D(40), D(0), uid=u1)

        ts = [threading.Thread(target=writer, args=(u1, u2, 60), daemon=True), threading.Thread(target=writer, args=(u2, u1, 60), daemon=True),
              threading.Thread(target=reader, daemon=True)]
        [t.start() for t in ts]
        [t.join(40) for t in ts[:2]]
        stop.set()
        ts[2].join(5)
        self.assertFalse(any(t.is_alive() for t in ts), "a writer or the reader is stuck: deadlock")
        self.assertEqual(errs, [])

    def test_concurrent_flushes_are_serialized_and_read_your_writes_holds(self):
        """A slow write is in flight (it already took the rows) when a page view asks. The view must WAIT for that write and only then
        return, so what it reads includes its own rows. Without one-writer-at-a-time the second flush finds an empty buffer, returns at
        once, and the view reads data that is not committed yet."""
        import threading
        uid, h = self.users[0]
        real = self.repos.usage_upsert_many
        in_write, release = threading.Event(), threading.Event()
        order = []

        def slow(by, sessions=None):
            order.append("write-start")
            in_write.set()
            release.wait(10)
            n = real(by, sessions)
            order.append("write-done")
            return n
        self.repos.usage_upsert_many = slow
        self.buf.add(uid, "view", [irow(D(25), model="mv", out=777)])
        a = threading.Thread(target=self.buf.flush, daemon=True)                  # the loop's flush: takes the rows, then is slow
        a.start()
        self.assertTrue(in_write.wait(5))
        seen = []

        def viewer():
            self.buf.flush_for([uid])
            order.append("view-returned")
            seen.append(self.repos.db.conn().execute("SELECT COALESCE(SUM(outp),0) FROM usage_daily WHERE user_id=? AND client_id='view'", (uid,)).fetchone()[0])
        v = threading.Thread(target=viewer, daemon=True)
        v.start()
        time.sleep(0.4)                                                           # the view is now asking, with the write still in flight
        # the buffer is empty here (the writer took the rows), so pending() is False and flush_for returns at once: that is the gap
        release.set()
        a.join(10)
        v.join(10)
        self.assertFalse(a.is_alive() or v.is_alive(), "deadlock between the writer and a page view")
        within(10, self.buf.flush)
        self.assertEqual(seen, [777], "a page view returned before the in-flight write committed; order=%s" % order)

    def test_a_stuck_database_cannot_hang_a_page_view_or_block_uploads(self):
        """The writer is blocked inside the database. Uploads must still be accepted (journaled), and a page view must give up after
        FLUSH_WAIT and answer, not wait for ever. A regression fails in seconds with a message; it never hangs the run."""
        import threading
        from aihub.server import usagebuf
        uid, h = self.users[0]
        gate, entered = threading.Event(), threading.Event()
        real = self.repos.usage_upsert_many

        def stuck(by, sessions=None):
            entered.set()
            gate.wait(30)
            return real(by, sessions)
        self.repos.usage_upsert_many = stuck
        self.addCleanup(gate.set)                                                 # whatever happens below, the stuck writer is released
        self.buf.add(uid, "a", [irow(D(0), out=5)])
        w = threading.Thread(target=self.buf.flush, daemon=True)
        w.start()
        self.assertTrue(entered.wait(5))
        t0 = time.time()
        for i in range(20):                                                       # uploads keep working while the writer is stuck
            within(3, self.buf.add, uid, "b", [irow(D(1), out=i + 1)])
        with mock.patch.object(usagebuf, "FLUSH_WAIT", 0.3):
            within(3, self.buf.flush_for, [uid])                                  # the page view gives up after FLUSH_WAIT and answers
        self.assertLess(time.time() - t0, 4, "uploads or the page view waited for the stuck writer")
        gate.set()
        w.join(10)
        self.assertFalse(w.is_alive())
        within(10, self.buf.flush)
        self.assertEqual(self.repos.db.conn().execute("SELECT COUNT(*) FROM usage_daily WHERE user_id=?", (uid,)).fetchone()[0], 2)

    def test_failed_write_then_concurrent_upload_loses_nothing(self):
        import threading
        uid, h = self.users[0]
        self.buf.add(uid, "a", [irow(D(0), out=100)])
        real = self.repos.usage_upsert_many
        started, release = threading.Event(), threading.Event()

        def boom(by, sessions=None):
            started.set()
            release.wait(10)
            raise OSError("db down")
        self.repos.usage_upsert_many = boom
        w = threading.Thread(target=self.buf.flush, daemon=True)
        w.start()
        self.assertTrue(started.wait(5))
        self.buf.add(uid, "a", [irow(D(0), out=250)])                               # a newer upload lands for the same key mid-failure
        self.buf.add(uid, "z", [irow(D(2), out=9)])
        release.set()
        w.join(10)
        self.repos.usage_upsert_many = real
        within(10, self.buf.flush)
        got = dict(((r[0], r[1]), r[2]) for r in self.repos.db.conn().execute("SELECT client_id,day,outp FROM usage_daily WHERE user_id=?", (uid,)))
        self.assertEqual(got[("a", D(0))], 250)                                    # the newer total won over the rolled-back older one
        self.assertEqual(got[("z", D(2))], 9)
        self.assertEqual(self.buf.stats["failures"], 0)                            # a recovered outage resets the back-off

    def test_close_while_busy_terminates(self):
        import threading
        uid, h = self.users[0]
        self.buf.start()
        stop = threading.Event()
        up = threading.Thread(target=lambda: [self.buf.add(uid, "c", [irow(D(i % 10), out=i + 1)]) for i in range(300) if not stop.is_set()], daemon=True)
        up.start()
        within(20, self.buf.close)
        stop.set()
        up.join(5)
        self.assertFalse(self.buf.t.is_alive())


class CliNoDeadlock(unittest.TestCase):
    """The uploader runs detached next to the agent. It must never block a hook, never leave a lock nobody can take, and never run twice."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"HOME": self.home, "USERPROFILE": self.home, "AIHUB_HOME": os.path.join(self.home, ".aihub"), "AIHUB_URL": "http://hub.invalid"})
        p.start()
        self.addCleanup(p.stop)
        from aihub.cli import paths
        paths.ensure()
        paths.save("credentials.json", {"token": "t", "username": "ann"}, private=True)

    def test_two_workers_never_run_at_once(self):
        import threading
        got, lock = [], threading.Lock()

        def try_take():
            ok = usagesync._acquire()
            with lock:
                got.append(ok)
        ts = [threading.Thread(target=try_take) for _ in range(40)]
        [t.start() for t in ts]
        [t.join(10) for t in ts]
        self.assertEqual(got.count(True), 1, "exactly one worker may hold the lock; got %s" % got.count(True))
        usagesync._release()
        self.assertTrue(usagesync._acquire())                                     # released: the next one can take it
        usagesync._release()

    def test_a_worker_killed_mid_run_does_not_block_the_next_one(self):
        self.assertTrue(usagesync._acquire())                                     # a worker takes the lock and is then killed: it never releases
        old = time.time() - (usagesync.STALE + 5)
        os.utime(usagesync._q("usage.lock"), (old, old))                          # its heartbeat stopped
        self.assertTrue(usagesync._acquire(), "a stale lock must be taken over, not wait for ever")
        usagesync._release()

    def test_a_live_worker_keeps_its_lock(self):
        self.assertTrue(usagesync._acquire())
        self.assertFalse(usagesync._acquire())                                    # fresh heartbeat: still running
        usagesync._release()

    def test_a_worker_stuck_on_the_network_cannot_block_the_hook(self):
        import threading
        gate = threading.Event()
        started = threading.Event()

        def hang(client, rows, sessions=()):
            started.set()
            gate.wait(30)
            return True
        self.addCleanup(gate.set)
        with mock.patch.object(usagesync, "_post", hang), mock.patch.object(usagesync, "totals", return_value={("2026-01-01", "claude", "m"): {"day": "2026-01-01", "tool": "claude", "model": "m", "requests": 1, "input": 1, "output": 1, "cache_write": 0, "cache_read": 0, "cost": 0.1, "unpriced": 0}}):
            w = threading.Thread(target=lambda: usagesync.run(window=0), daemon=True)
            w.start()
            self.assertTrue(started.wait(5), "the worker never reached the network call")
            with mock.patch("subprocess.Popen") as pop:
                t0 = time.time()
                for _ in range(30):                                               # 30 answer turns end while the worker is stuck
                    within(2, usagesync.kick)
                    within(2, hooks.verify_soon)
                self.assertLess(time.time() - t0, 2.0, "hooks waited for the stuck worker")
                self.assertEqual(pop.call_count, 0, "a second worker was started while one is running")
            gate.set()
            w.join(10)
            self.assertFalse(w.is_alive())
        self.assertFalse(os.path.exists(usagesync._q("usage.lock")), "the lock must be released when the worker ends")

    def test_lock_is_released_even_when_the_upload_raises(self):
        with mock.patch.object(usagesync, "push", side_effect=RuntimeError("boom")):
            usagesync.run(window=0)
        self.assertFalse(os.path.exists(usagesync._q("usage.lock")))
        self.assertTrue(usagesync._acquire())
        usagesync._release()

    def test_a_hook_never_waits_for_the_state_file(self):
        """Two processes writing usage-sync.json at the same time: each write is atomic (temp file + rename), so neither sees half a file."""
        import threading
        errs = []

        def w(i):
            try:
                for n in range(200):
                    usagesync._save({"sent": {"k%d" % j: [i, n] for j in range(20)}, "hub": "h", "user": "u"})
                    st = usagesync._state()
                    if st and "sent" not in st:
                        errs.append("half-written state: %s" % st)
            except Exception as e:
                errs.append(repr(e))
        ts = [threading.Thread(target=w, args=(i,), daemon=True) for i in range(4)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        self.assertFalse(any(t.is_alive() for t in ts))
        self.assertEqual(errs, [])


class SettingsFileSafe(unittest.TestCase):
    """Hooks now verify settings.json from many short processes at once. It holds the user's permissions and every other hook, so it must
    never be corrupted, never lose a concurrent change, and never make a hook wait."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"HOME": self.home, "USERPROFILE": self.home})
        p.start()
        self.addCleanup(p.stop)
        self.path = os.path.join(self.home, ".claude", "settings.json")
        os.makedirs(os.path.dirname(self.path))

    def test_overlapping_writers_never_corrupt_or_fail(self):
        import threading
        from aihub.cli import integrations as I
        big = {"permissions": {"allow": ["Bash(ls %d)" % i for i in range(3000)]}, "model": "x"}
        json.dump(big, open(self.path, "w"))
        errs = []

        def w():
            for _ in range(20):
                try:
                    with I.locked(self.path):
                        I._write_json(self.path, dict(big, hooks={}))
                    json.load(open(self.path))
                except Exception as e:
                    errs.append(type(e).__name__)
        ts = [threading.Thread(target=w, daemon=True) for _ in range(8)]
        [t.start() for t in ts]
        [t.join(60) for t in ts]
        self.assertFalse(any(t.is_alive() for t in ts))
        self.assertEqual(errs, [], "overlapping writes failed: %s" % sorted(set(errs)))
        self.assertEqual(json.load(open(self.path))["model"], "x")
        self.assertEqual([f for f in os.listdir(os.path.dirname(self.path)) if f.endswith(".tmp") or f.endswith(".lock")], [], "temp or lock files left behind")

    def test_unique_temp_names_even_without_the_lock(self):
        """Even if the lock times out and a writer goes ahead, writers must not share a temp file."""
        import threading
        from aihub.cli import integrations as I
        big = {"permissions": {"allow": ["Bash(ls %d)" % i for i in range(3000)]}}
        json.dump(big, open(self.path, "w"))
        errs = []

        def w():
            for _ in range(20):
                try:
                    I._write_json(self.path, big)
                    json.load(open(self.path))
                except Exception as e:
                    errs.append(type(e).__name__)
        ts = [threading.Thread(target=w, daemon=True) for _ in range(8)]
        [t.start() for t in ts]
        [t.join(60) for t in ts]
        self.assertEqual(errs, [], sorted(set(errs)))

    def test_two_installers_inside_the_critical_section_do_not_lose_each_others_change(self):
        """Deterministic version of the race (a real overlap lasts ~1 ms and cannot be hit by luck). Installer A reads the file, then is held
        up (its editor/disk is slow). Meanwhile the USER saves a new setting. When A finally writes, the user's setting must still be there.
        Without the file lock + re-read inside it, A writes back what it read earlier and silently erases the user's change."""
        import threading
        from aihub.cli import integrations as I
        json.dump({"model": "old"}, open(self.path, "w"))
        a_read, a_go = threading.Event(), threading.Event()
        real_read = I._read_json
        import inspect

        def slow_read(path):
            d = real_read(path)
            # the read whose result the installer will modify and write back (not has_hook's checks), found by who is asking
            callers = [f.function for f in inspect.stack()[1:4]]
            if threading.current_thread().name == "A" and "has_hook" not in callers and not a_read.is_set():
                a_read.set()
                a_go.wait(10)                                                           # A now holds stale data and stalls before it writes
            return d
        res = {}

        def installer():
            res["a"] = I.ClaudeCode().install_hook("/x/aihub hook")
        with mock.patch.object(I, "_read_json", slow_read):
            ta = threading.Thread(target=installer, name="A", daemon=True)
            ta.start()
            self.assertTrue(a_read.wait(5))
            # the user (or Claude Code itself) saves a setting while A is stalled between its read and its write
            def user_edit():
                with I.locked(self.path, wait=1.0):                                    # a well-behaved editor takes the same lock
                    d = real_read(self.path)
                    d["model"] = "user-changed-this"
                    I._write_json(self.path, d)
            tu = threading.Thread(target=user_edit, daemon=True)
            tu.start()
            time.sleep(0.3)
            a_go.set()
            ta.join(10)
            tu.join(10)
        self.assertFalse(ta.is_alive() or tu.is_alive(), "deadlock between the installer and an editor")
        d = json.load(open(self.path))
        self.assertEqual(d["model"], "user-changed-this", "the installer erased a change made while it was working")
        self.assertIn("Stop", d["hooks"])

    def test_the_second_installer_waits_then_finds_the_work_done_and_writes_nothing(self):
        """B passes its first has_hook check, then waits for the lock A holds. When it gets the lock A has finished: B must re-check inside
        the lock and not write again (no duplicate entries, no needless rewrite of the user's file)."""
        import threading
        from aihub.cli import integrations as I
        json.dump({"model": "x"}, open(self.path, "w"))
        cmd = "/x/aihub hook"
        c = I.ClaudeCode()
        writes = []
        real_write = I._write_json
        before_lock, proceed = threading.Event(), threading.Event()
        real_locked = I.locked

        def gated_locked(path, **kw):
            if threading.current_thread().name == "B":
                before_lock.set()                                  # B has passed the cheap check and is about to take the lock
                proceed.wait(10)
            return real_locked(path, **kw)
        res = {}
        with mock.patch.object(I, "_write_json", lambda p_, d: (writes.append(threading.current_thread().name), real_write(p_, d))[1]), \
                mock.patch.object(I, "locked", gated_locked):
            tb = threading.Thread(target=lambda: res.setdefault("b", c.install_hook(cmd)), name="B", daemon=True)
            tb.start()
            self.assertTrue(before_lock.wait(5))
            res["a"] = I.ClaudeCode().install_hook(cmd) if False else None
            ta = threading.Thread(target=lambda: res.setdefault("a", c.install_hook(cmd)), name="A", daemon=True)
            ta.start()
            ta.join(10)                                            # A installs everything while B is still waiting
            proceed.set()
            tb.join(10)
        self.assertFalse(ta.is_alive() or tb.is_alive())
        self.assertEqual(writes, ["A"], "B rewrote the file although A had already installed the hooks: %s" % writes)
        self.assertIs(res["b"], False)
        d = json.load(open(self.path))
        self.assertEqual(sum(1 for g in d["hooks"]["Stop"] for h in g["hooks"] if " hook --source " in h["command"]), 1)

    def test_many_processes_installing_hooks_keep_every_setting(self):
        """Real separate processes: eight install the hooks at once while the user's own settings sit in the same file. Both survive and our
        hooks appear exactly once. (The overlap is too short to hit by luck, so the deterministic test above is the one with teeth; this
        one guards against crashes and duplicates across real processes.)"""
        import subprocess, sys
        json.dump({"model": "keep-me", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "user-stop"}]}]}}, open(self.path, "w"))
        code = ("import sys; sys.path.insert(0, %r)\nfrom aihub.cli import integrations as I\nc = I.ClaudeCode()\n"
                "for _ in range(6): c.install_hook('/x/aihub hook')\n" % os.getcwd())
        env = dict(os.environ, HOME=self.home, USERPROFILE=self.home)
        ps = [subprocess.Popen([sys.executable, "-W", "ignore", "-c", code], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(8)]
        outs = [p.communicate(timeout=90) for p in ps]
        bad = [o[1][-200:] for o, p in zip(outs, ps) if p.returncode]
        self.assertEqual(bad, [], "an installer process crashed: %s" % bad[:1])
        d = json.load(open(self.path))
        self.assertEqual(d["model"], "keep-me")
        ours = lambda ev: sum(1 for g in d["hooks"].get(ev, []) for h in g["hooks"] if " hook --source " in h["command"])
        self.assertEqual((ours("PreToolUse"), ours("UserPromptSubmit"), ours("Stop")), (1, 1, 1), "duplicated or missing hook entries: %s" % d["hooks"])
        self.assertEqual(d["hooks"]["Stop"][0]["hooks"][0]["command"], "user-stop")
        self.assertEqual([f for f in os.listdir(os.path.dirname(self.path)) if f.endswith((".tmp", ".lock"))], [])

    def test_a_dead_process_lock_or_a_held_lock_never_blocks_a_hook(self):
        from aihub.cli import integrations as I
        lock = self.path + ".aihub.lock"
        open(lock, "w").close()
        old = time.time() - 120
        os.utime(lock, (old, old))                                                  # a process died holding it
        t0 = time.time()
        with I.locked(self.path) as have:
            self.assertTrue(have, "a stale lock must be taken over")
        self.assertLess(time.time() - t0, 1.0)
        open(lock, "w").close()                                                      # now a LIVE holder that never lets go
        t0 = time.time()
        with I.locked(self.path, wait=0.3) as have:
            self.assertFalse(have)                                                   # gave up waiting, did not hang
        self.assertLess(time.time() - t0, 1.5, "a hook waited too long for a held lock")
        os.remove(lock)


class Reinstall(HubBase):
    """Uninstall + install gives the CLI a new client_id and it re-uploads its whole log history: that must not count twice."""
    busy = lambda self, day, **k: row(day, req=8, **k)

    def test_reinstalled_client_is_folded_and_its_history_kept(self):
        self.up("ann", [self.busy(D(3), out=900), self.busy(D(1)), self.busy(D(0), out=100)], client="old")
        # reinstall: D(3) logs were pruned meanwhile, the rest is the same, today has grown
        self.up("ann", [self.busy(D(1)), self.busy(D(0), out=400)], client="new")
        t = self.get("ann").json()["totals"]
        self.assertEqual(t["outp"], 900 + 100 + 400)
        clients = {r[0] for r in self.c.app.state.repos.db.conn().execute("SELECT DISTINCT client_id FROM usage_daily")}
        self.assertEqual(clients, {"new"})

    def test_two_real_devices_still_add_and_quiet_identical_days_are_not_merged(self):
        self.up("ann", [self.busy(D(0), out=100)], client="mac")
        self.up("ann", [self.busy(D(0), out=150)], client="pc")             # different numbers = another machine
        self.up("ann", [row(D(1), req=2)], client="mac")
        self.up("ann", [row(D(1), req=2)], client="pc")                      # identical but tiny: could be a coincidence, kept apart
        self.up("bob", [self.busy(D(0), out=100)], client="bobs")            # same numbers, another person: never merged
        self.assertEqual(self.get("ann").json()["totals"]["outp"], 100 + 150 + 100 + 100)
        self.assertEqual(self.get("bob").json()["totals"]["outp"], 100)

    def test_start_repair_folds_rows_doubled_before_the_fix_once(self):
        repos = self.c.app.state.repos
        self.up("ann", [self.busy(D(0))], client="a")
        self.c.app.state.usagebuf.flush()
        uid = repos.db.conn().execute("SELECT user_id FROM usage_daily").fetchone()[0]
        c = repos.db.conn()
        ins = "INSERT INTO usage_daily(user_id,client_id,day,tool,model,requests,inp,outp,cache_write,cache_read,cost,unpriced,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
        for cl, day, out, ts in (("a", D(1), 70, 1), ("b", D(0), 100, 2), ("b", D(1), 70, 2), ("c", D(1), 70, 3), ("c", D(2), 5, 3)):
            c.execute(ins, (uid, cl, day, "claude", "claude-opus-5", 8, 10, out, 5, 20, 1.0, 0, ts))
        c.execute("UPDATE usage_daily SET updated=0 WHERE client_id='a'")
        c.commit()
        self.c.app.state.repos.set_setting("usage_dedupe_v1", "")
        self.assertEqual(repos.usage_dedupe(), 2)
        rows = sorted((r[0], r[1], r[2]) for r in c.execute("SELECT client_id,day,outp FROM usage_daily"))
        self.assertEqual(rows, sorted([("c", D(0), 100), ("c", D(1), 70), ("c", D(2), 5)]))
        self.assertEqual(repos.usage_dedupe(), 0)                            # marker: once


class StableClientId(unittest.TestCase):
    def test_new_install_reuses_the_id_and_an_existing_id_is_kept(self):
        home = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"AIHUB_HOME": home}), mock.patch.object(hooks, "_machine_id", return_value="m-1"):
            first = hooks.client_id()
            os.remove(os.path.join(home, "config.json"))                    # self-uninstall wipes ~/.aihub
            self.assertEqual(hooks.client_id(), first)
            with open(os.path.join(home, "config.json"), "w") as f:
                json.dump({"client_id": "legacy"}, f)
            self.assertEqual(hooks.client_id(), "legacy")                   # old installs keep theirs
        with mock.patch.dict(os.environ, {"AIHUB_HOME": tempfile.mkdtemp()}), mock.patch.object(hooks, "_machine_id", return_value=""):
            self.assertEqual(len(hooks.client_id()), 32)                    # unknown machine: random, as before
