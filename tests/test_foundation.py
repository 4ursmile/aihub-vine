import json
import os
import subprocess
import tempfile
import time
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


class EventDrivenFlusher(unittest.TestCase):
    """The flusher runs only when there is something to send, backs off while offline, and never strands an event."""

    def setUp(self):
        from aihub.cli import telemetry
        self.t = telemetry
        self.home = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"AIHUB_HOME": self.home, "AIHUB_TELEMETRY_URL": "file:///x", "AIHUB_TELEMETRY_INTERVAL": "5"})
        self.env.start()
        paths.ensure("queue")

    def tearDown(self):
        self.env.stop()

    def test_failed_push_backs_off_and_hook_does_not_start_a_flusher(self):
        self.t.enqueue({"kind": "use", "package": "p"})
        with mock.patch.object(self.t, "push", side_effect=RuntimeError("offline")):
            self.assertEqual(self.t._send_once(), 0)
        self.assertGreater(self.t._backoff(), 0)
        with mock.patch("subprocess.Popen") as popen:
            self.t.kick()
            popen.assert_not_called()                                       # offline: no process, no network attempt
        self.assertFalse(os.path.exists(self.t._lock()))

    def test_backoff_grows_is_capped_and_a_success_resets_it(self):
        waits = []
        for _ in range(8):
            self.t._fail()
            waits.append(round(self.t._backoff_state()[1] - time.time()))
        self.assertEqual(waits[:4], [30, 60, 120, 240])
        self.assertEqual(waits[-1], self.t.BACKOFF_MAX)
        self.t.enqueue({"kind": "use", "package": "p"})
        with mock.patch.object(self.t, "push"):
            self.assertEqual(self.t._send_once(), 1)
        self.assertEqual(self.t._backoff(), 0)
        self.assertEqual(self.t._backoff_state()[0], 0)

    def test_backoff_expires_and_a_clock_jump_cannot_lock_it_out(self):
        self.t._fail()
        with open(self.t._backoff_file(), "w") as f:
            json.dump({"fails": 3, "next": time.time() + 10 ** 7}, f)          # clock was wrong when this was written
        self.assertEqual(self.t._backoff(), 0)
        with open(self.t._backoff_file(), "w") as f:
            json.dump({"fails": 1, "next": time.time() - 1}, f)
        self.assertEqual(self.t._backoff(), 0)

    def test_manual_flush_ignores_the_delay(self):
        self.t.enqueue({"kind": "use", "package": "p"})
        self.t._fail()
        with mock.patch.object(self.t, "push") as push:
            self.assertEqual(self.t.flush(force=True), 1)
            push.assert_called_once()

    def test_event_recorded_during_the_final_push_is_not_stranded(self):
        """A hook records while the flusher holds the lock (so it starts none). The flusher must notice and run again."""
        self.t.enqueue({"kind": "use", "package": "first"})
        pushes = []

        def fake_push(url, lines, timeout=20):
            pushes.append(len(lines))
            if len(pushes) == 1:
                self.t.enqueue({"kind": "use", "package": "late"})             # arrives mid-push, lock is held: kick() would do nothing
        with mock.patch.object(self.t, "push", side_effect=fake_push), mock.patch.object(self.t, "interval", return_value=0.05), \
                mock.patch("time.sleep", lambda s: None):
            self.t.flush()
        self.assertEqual(pushes, [1, 1])                                        # two batches, nothing left behind
        self.assertFalse(self.t._pending())
        self.assertFalse(os.path.exists(self.t._lock()))

    def test_idle_flusher_exits_and_pushes_nothing(self):
        with mock.patch.object(self.t, "push") as push, mock.patch.object(self.t, "interval", return_value=0.05), \
                mock.patch("time.sleep", lambda s: None):
            self.assertEqual(self.t.flush(), 0)
            push.assert_not_called()

    def test_failed_acquire_does_not_delete_the_other_flushers_lock(self):
        self.assertTrue(self.t._acquire())                                      # another flusher is alive
        self.assertEqual(self.t.flush(), 0)                                     # we lose the race
        self.assertTrue(os.path.exists(self.t._lock()))                         # and must leave its lock alone
        self.t._release()

    def test_file_with_no_valid_lines_is_removed_so_the_flusher_cannot_loop(self):
        with open(paths.p("queue", "sending.1.1.jsonl"), "w") as f:
            f.write("garbage\nmore garbage\n")
        with mock.patch.object(self.t, "push") as push:
            self.assertEqual(self.t._send_once(), 0)
            push.assert_not_called()
        self.assertFalse(self.t._pending())

    def test_unremovable_leftover_cannot_make_the_flusher_spin(self):
        self.t.enqueue({"kind": "use", "package": "p"})
        runs = []
        with mock.patch.object(self.t, "push", side_effect=lambda *a, **k: runs.append(1)), mock.patch("os.remove", side_effect=OSError("read-only")), \
                mock.patch.object(self.t, "interval", return_value=0.05), mock.patch("time.sleep", lambda s: None):
            self.t.flush()
        self.assertLessEqual(len(runs), 2)                                      # bounded: one pass, maybe one re-check, never a loop

    def test_sent_file_that_cannot_be_deleted_hits_the_pass_cap(self):
        """The worst case: every pass pushes, none can delete. The flusher must still stop on its own."""
        with open(paths.p("queue", "sending.1.1.jsonl"), "w") as f:
            f.write('{"id":"a","kind":"use","package":"p"}\n')
        runs = []
        real_remove = os.remove
        with mock.patch.object(self.t, "push", side_effect=lambda *a, **k: runs.append(1)), \
                mock.patch("os.remove", side_effect=lambda p: None if "sending." in p else real_remove(p)), \
                mock.patch.object(self.t, "interval", return_value=0.01), mock.patch("time.sleep", lambda s: None):
            self.t.flush()
        self.assertEqual(len(runs), self.t.MAX_PASSES)

    def test_no_url_keeps_the_spool_and_the_flusher_exits(self):
        self.t.enqueue({"kind": "use", "package": "p"})
        with mock.patch.object(self.t, "target", return_value=""), mock.patch("time.sleep", lambda s: None):
            self.assertEqual(self.t.flush(), 0)
        self.assertTrue(self.t._pending())                                      # kept until an inbox is configured
