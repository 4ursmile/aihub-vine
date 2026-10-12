import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from aihub.cli import gitx, identity, paths
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

    def test_interval_is_clamped_and_defaults_to_15s(self):
        from aihub.cli import telemetry
        with mock.patch.dict(os.environ, {"AIHUB_TELEMETRY_INTERVAL": ""}):
            self.assertEqual(telemetry.interval(), 15.0)
            with mock.patch("aihub.core.defaults.load", return_value={"telemetry": {"interval": 40}}):
                self.assertEqual(telemetry.interval(), 40.0)                         # defaults.json is honoured
                paths.save("config.json", {"telemetry_interval": "25"})
                self.assertEqual(telemetry.interval(), 25.0)                         # config.json beats it
                with mock.patch.dict(os.environ, {"AIHUB_TELEMETRY_INTERVAL": "7"}):
                    self.assertEqual(telemetry.interval(), 7.0)                      # environment beats both
        with mock.patch.dict(os.environ, {"AIHUB_TELEMETRY_INTERVAL": "1"}):
            self.assertEqual(telemetry.interval(), 5.0)
        with mock.patch.dict(os.environ, {"AIHUB_TELEMETRY_INTERVAL": "999999"}):
            self.assertEqual(telemetry.interval(), 3600.0)

    def test_every_event_gets_a_unique_id_and_identity(self):
        import json
        from aihub.cli import telemetry
        telemetry.enqueue({"kind": "use", "package": "p"})
        telemetry.enqueue({"kind": "use", "package": "p"})
        a, b = [json.loads(l) for l in open(paths.p("queue", "events.jsonl"))]
        self.assertNotEqual(a["id"], b["id"])                                     # the hub dedups on this
        self.assertTrue(a["client_id"] and a["ts"] and a["os"])

    def test_nothing_is_pushed_when_nothing_is_spooled(self):
        from aihub.cli import telemetry
        with mock.patch.object(telemetry, "push") as push, mock.patch.object(telemetry, "target", return_value="file:///x"):
            self.assertEqual(telemetry._send_once(), 0)
            push.assert_not_called()


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
