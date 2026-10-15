"""Stress: the hub is the main channel (legacy git sync OFF). Every acknowledged event must end up in the database exactly once."""
import os
import random
import tempfile
import threading
import time
import unittest

from fastapi.testclient import TestClient

from aihub.server.config import Settings
from aihub.server.ingest import Ingest
from aihub.server.main import create_app
from tests.test_hub_first import admin, ev


class HubStress(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.c = TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=self.data)))
        self.h = admin(self.c)
        self.r, self.ing, self.sy = self.c.app.state.repos, self.c.app.state.ingest, self.c.app.state.sync
        self.c.put("/api/v1/admin/sync", json={"telemetry_rate_limit": "0"}, headers=self.h)     # no per-client throttle in this test
        self.assertFalse(self.sy.telemetry_git() or self.sy.index_git())                          # git sync really is off
        self.ids = lambda: [x[0] for x in self.r.db.conn().execute("SELECT ext_id FROM events WHERE package='stress'").fetchall()]

    def tearDown(self):
        self.ing.close()

    def test_many_clients_many_events_nothing_lost_nothing_doubled(self):
        clients, batches, per = 24, 20, 25                          # 12 000 events, 480 requests
        acked, lock = set(), threading.Lock()

        def client(n):
            rnd = random.Random(n)
            for b in range(batches):
                evs = [ev("c%d-b%d-%d" % (n, b, i), package="stress", client_id="cl%d" % n) for i in range(per)]
                for attempt in range(50):
                    r = self.c.post("/api/v1/telemetry", json={"events": evs + (evs[:3] if rnd.random() < .2 else [])})   # some resends
                    if r.status_code == 200:
                        with lock:
                            acked.update(e["id"] for e in evs)
                        break
                    self.assertIn(r.status_code, (429, 503))        # back-pressure is allowed, silent loss is not
                    time.sleep(0.05)
                else:
                    self.fail("hub never accepted a batch")

        stop = threading.Event()

        def flusher():                                              # the DB window firing while requests keep arriving
            while not stop.is_set():
                self.ing.flush()
                time.sleep(0.02)

        ft = threading.Thread(target=flusher)
        ft.start()
        ts = [threading.Thread(target=client, args=(n,)) for n in range(clients)]
        t0 = time.time()
        [t.start() for t in ts]
        [t.join() for t in ts]
        stop.set()
        ft.join()
        self.ing.flush()
        got = self.ids()
        print("\n%d events in %.1fs (%.0f/s)" % (len(acked), time.time() - t0, len(acked) / (time.time() - t0)))
        self.assertEqual(len(acked), clients * batches * per)
        self.assertEqual(len(got), len(set(got)))                   # no duplicates
        self.assertEqual(set(got), acked)                           # no loss
        self.assertEqual(self.ing.rows, [])

    def test_kill_in_the_middle_loses_nothing_acknowledged(self):
        acked = set()
        for b in range(40):
            evs = [ev("k%d-%d" % (b, i), package="stress") for i in range(50)]
            self.assertEqual(self.c.post("/api/v1/telemetry", json={"events": evs}).status_code, 200)
            acked.update(e["id"] for e in evs)
            if b % 10 == 9 and b < 35:
                self.ing.flush()
        self.assertTrue(self.ing.rows)                              # a tail is still only in memory + journal
        reborn = Ingest(self.r, self.sy, None, self.ing.journal_dir)     # kill -9 and restart
        reborn.flush()
        self.ing.rows = []
        got = self.ids()
        self.assertEqual(set(got), acked)
        self.assertEqual(len(got), len(set(got)))

    def test_overload_answers_503_and_a_retry_loses_nothing(self):
        from unittest import mock
        acked = set()
        with mock.patch("aihub.server.ingest.HARD_ROWS", 500):
            for b in range(60):
                evs = [ev("o%d-%d" % (b, i), package="stress") for i in range(50)]
                for _ in range(20):
                    r = self.c.post("/api/v1/telemetry", json={"events": evs})
                    if r.status_code == 200:
                        acked.update(e["id"] for e in evs)
                        break
                    self.assertEqual(r.status_code, 503)
                    self.ing.flush()                                # the window fires, then the CLI retries
        self.ing.flush()
        self.assertEqual(set(self.ids()), acked)
        self.assertEqual(len(acked), 3000)

    def test_publish_events_still_served_with_git_off_under_load(self):
        for i in range(500):
            self.c.post("/api/v1/telemetry", json={"events": [ev("x%d" % i, package="stress")]})
        self.ing.flush()
        self.assertEqual(self.c.get("/api/v1/index/catalog", headers=self.h).status_code in (200, 503), True)
        self.assertEqual(len(self.ids()), 500)
        self.assertIsNone(self.sy.status.get("last_error"))


if __name__ == "__main__":
    unittest.main()
