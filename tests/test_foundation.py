import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from aihub.cli import gitx, identity, langfuse, paths
from aihub.core import __file__ as core_init


class Foundation(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"AIHUB_HOME": self.home})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_defaults_json_valid(self):
        d = json.load(open(os.path.join(os.path.dirname(core_init), "defaults.json")))
        self.assertIn("publish", d["event_names"])

    def test_anonymous_identity(self):
        i = identity.current()
        self.assertTrue(i["anonymous"])
        self.assertTrue(i["user"].startswith("~"))
        paths.save("credentials.json", {"username": "bob"})
        self.assertEqual(identity.current()["user"], "bob")

    def test_span_id_deterministic(self):
        self.assertEqual(langfuse.span_id("a", 1), langfuse.span_id("a", 1))
        self.assertNotEqual(langfuse.span_id("a", 1), langfuse.span_id("a", 2))

    def test_git_remote_info_and_cache(self):
        d = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", "-b", "main", d], check=True)
        subprocess.run(["git", "-C", d, "remote", "add", "origin", "https://u:tok@host/x.git"], check=True)
        url, br = gitx.remote_info(d)
        self.assertEqual(br, "main")
        self.assertNotIn("tok", gitx.redact_url(url))
        self.assertTrue(gitx.is_repo(d))


if __name__ == "__main__":
    unittest.main()


class Telemetry(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"AIHUB_HOME": self.home, "AIHUB_TELEMETRY_INTERVAL": "60"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def ev(self, t, comp="skill:a", **k):
        return dict({"kind": "use", "package": "p", "component": comp, "client_id": "c1", "local_user": "dan", "ts": t}, **k)

    def test_every_call_is_counted_but_one_span_per_window(self):
        from aihub.cli import telemetry
        events = [self.ev(120 + i) for i in range(50)] + [self.ev(130, comp="skill:b")]      # 50 calls + 1 other, same window
        spans, left = telemetry.aggregate(events, 60, now=1000)
        self.assertEqual((len(spans), left), (2, []))
        counts = sorted(int([a for a in s["attributes"] if a["key"] == "langfuse.observation.metadata.count"][0]["value"]["stringValue"]) for s in spans)
        self.assertEqual(counts, [1, 50])

    def test_next_window_is_a_different_span_and_resend_is_the_same(self):
        from aihub.cli import telemetry
        a, _ = telemetry.aggregate([self.ev(10), self.ev(70)], 60, now=1000)
        self.assertEqual(len({s["spanId"] for s in a}), 2)
        b, _ = telemetry.aggregate([self.ev(10), self.ev(70)], 60, now=2000)
        self.assertEqual({s["spanId"] for s in a}, {s["spanId"] for s in b})                # retries never duplicate

    def test_open_window_is_held_back_until_it_closes(self):
        from aihub.cli import telemetry
        spans, left = telemetry.aggregate([self.ev(10), self.ev(990)], 60, now=1000)      # 990 is in the still-open 960-1020 window
        self.assertEqual((len(spans), len(left)), (1, 1))
        spans, left = telemetry.aggregate(left, 60, now=1000, force=True)                    # aihub flush sends it anyway
        self.assertEqual((len(spans), left), (1, []))

    def test_other_events_are_not_merged_and_interval_is_clamped(self):
        from aihub.cli import telemetry
        spans, _ = telemetry.aggregate([{"kind": "install", "package": "p", "version": "1", "ts": 5, "client_id": "c"}] * 2, 60, now=1000)
        self.assertEqual(len(spans), 2)
        with mock.patch.dict(os.environ, {"AIHUB_TELEMETRY_INTERVAL": "1"}):
            self.assertEqual(telemetry.interval(), 10.0)
        with mock.patch.dict(os.environ, {"AIHUB_TELEMETRY_INTERVAL": "999999"}):
            self.assertEqual(telemetry.interval(), 3600.0)


class FlusherLock(unittest.TestCase):
    def test_only_one_flusher_wins_a_burst(self):
        import threading
        from aihub.cli import telemetry
        with mock.patch.dict(os.environ, {"AIHUB_HOME": tempfile.mkdtemp()}):
            paths.ensure("queue")
            wins = []
            def go():
                if telemetry._acquire():
                    wins.append(1)
            ts = [threading.Thread(target=go) for _ in range(40)]
            [t.start() for t in ts]; [t.join() for t in ts]
            self.assertEqual(len(wins), 1)
            telemetry._release()
            self.assertTrue(telemetry._acquire())                              # released: next one can take it
            os.utime(telemetry._lock(), (1, 1))                                 # heartbeat long gone: a dead flusher
            self.assertTrue(telemetry._acquire())                              # is taken over
