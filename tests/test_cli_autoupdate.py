"""CLI self-update: modes, debounce, backoff after failure, state file, skip_version, machine-output and hook exclusions, and that the
command itself never does network work (the check runs in a spawned worker; tests run the worker inline through `spawn`)."""
import argparse
import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from aihub.cli import paths, ui, updater
from aihub.core.release import VERSION

NEWER = "99.0.0"
SPAWN = updater.spawn                                 # the real one (Base replaces it with an inline worker)
KEYS = ("AIHUB_HOME", "AIHUB_NO_UPDATE", "AIHUB_AUTO_UPDATE", "AIHUB_AUTO_UPDATE_HOURS", "AIHUB_AUTO_UPDATE_MINUTES")


class Base(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in KEYS}
        self.tmp = tempfile.mkdtemp()
        os.environ["AIHUB_HOME"] = self.tmp
        for k in KEYS[1:]:
            os.environ.pop(k, None)
        self.info = {"version": NEWER, "sha256": "x"}
        self.remote_calls = []
        self.applied = []
        self.fail_remote = None

        self.spawned = 0

        def spawn():                                  # the detached worker, run inline
            self.spawned += 1
            updater.cmd_auto_update(argparse.Namespace(background=True))

        def remote(timeout=10):
            self.remote_calls.append(timeout)
            if self.fail_remote:
                raise self.fail_remote
            return dict(self.info)

        ps = [mock.patch.object(updater, "remote", remote),
              mock.patch.object(updater, "apply", lambda info, dst=None: self.applied.append(info["version"])),
              mock.patch.object(updater, "target", lambda: "/x/aihub.pyz"),
              mock.patch.object(updater, "by_pip", lambda: False),
              mock.patch.object(updater, "spawn", spawn),
              mock.patch.object(ui, "INTERACTIVE", True)]
        for p in ps:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def cfg(self, **kv):
        paths.save("config.json", dict(kv))

    def auto(self, cmd="list", a=None):
        """One command: the worker it spawns installs, the NEXT command (running the new version) tells. -> output of both."""
        o, e = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
            updater.auto(cmd, a)
            if self.applied:
                with mock.patch.object(updater, "VERSION", self.applied[-1]):
                    updater._tell(updater.state(), updater.mode())
        return o.getvalue(), e.getvalue()

    def age(self, seconds):
        st = self.st()
        st["checked"] = time.time() - seconds
        paths.save("update.json", st)

    def st(self):
        return paths.load("update.json", {})


class Modes(Base):
    def test_on_is_default_and_installs(self):
        out, _ = self.auto()
        self.assertEqual(self.applied, [NEWER])
        self.assertIn("updated aihub %s -> %s" % (VERSION, NEWER), out)
        self.assertIn("aihub config set auto_update off", out)
        st = self.st()
        self.assertTrue(st["ok"])
        self.assertNotIn("last_error", st)
        self.assertNotIn("announce", st)              # told once
        self.assertEqual(self.remote_calls, [10])
        self.assertFalse(os.path.exists(updater._lock()))

    def test_value_spellings(self):
        for v, want in (("on", "on"), ("TRUE", "on"), ("1", "on"), ("Yes", "on"), (True, "on"), ("notify", "notify"), ("Notify", "notify"),
                        ("off", "off"), ("False", "off"), ("0", "off"), ("no", "off"), (False, "off")):
            self.cfg(auto_update=v)
            self.assertEqual(updater.mode(), want, v)

    def test_off(self):
        for v in ("off", "False", "no"):
            self.cfg(auto_update=v)
            self.auto()
        self.assertEqual((self.remote_calls, self.applied), ([], []))

    def test_notify_only_prints(self):
        self.cfg(auto_update="notify")
        out, _ = self.auto()
        self.assertEqual(out, "")                     # the check ran in the worker; the next command says it
        self.age(3600)
        out, _ = self.auto()
        self.assertEqual(self.applied, [])
        self.assertIn("aihub %s available: run aihub upgrade" % NEWER, out)
        self.assertEqual(self.st()["available"], NEWER)
        self.age(3600)
        out2, _ = self.auto()                         # same version, same day: no second message
        self.assertEqual(out2, "")
        self.assertEqual(len(self.remote_calls), 3)

    def test_env_overrides_config(self):
        self.cfg(auto_update="off")
        os.environ["AIHUB_AUTO_UPDATE"] = "notify"
        self.assertEqual(updater.mode(), "notify")
        self.cfg(auto_update="on")
        os.environ["AIHUB_AUTO_UPDATE"] = "off"
        self.auto()
        self.assertEqual(self.remote_calls, [])

    def test_no_update_env_means_off(self):
        os.environ["AIHUB_NO_UPDATE"] = "1"
        os.environ["AIHUB_AUTO_UPDATE"] = "on"
        self.auto()
        self.assertEqual(self.remote_calls, [])

    def test_up_to_date_clears_available(self):
        paths.save("update.json", {"available": "1.0.0", "last_error": "old"})
        self.info["version"] = VERSION
        self.auto()
        st = self.st()
        self.assertNotIn("available", st)
        self.assertNotIn("last_error", st)
        self.assertEqual(self.applied, [])


class Interval(Base):
    def test_debounce_respected(self):
        self.auto()
        self.assertEqual(len(self.remote_calls), 1)
        self.auto()                                   # within the minute: no second check, no spawn
        self.assertEqual((len(self.remote_calls), self.spawned), (1, 1))
        self.age(61)
        self.auto()
        self.assertEqual(len(self.remote_calls), 2)

    def test_minutes_setting_and_clamp(self):
        self.assertEqual(updater.minutes(), 1)
        self.cfg(auto_update_minutes="15")
        self.assertEqual(updater.minutes(), 15)
        self.cfg(auto_update_minutes=0)
        self.assertEqual(updater.minutes(), 1)
        self.cfg(auto_update_minutes=10 ** 9)
        self.assertEqual(updater.minutes(), 43200)
        self.cfg(auto_update_minutes="junk")
        self.assertEqual(updater.minutes(), 1)
        self.cfg(auto_update_hours=6)                 # the older key still works
        self.assertEqual(updater.minutes(), 360)
        os.environ["AIHUB_AUTO_UPDATE_HOURS"] = "2"
        self.assertEqual(updater.minutes(), 120)
        os.environ["AIHUB_AUTO_UPDATE_MINUTES"] = "3"
        self.assertEqual(updater.minutes(), 3)
        paths.save("update.json", {"checked": time.time() - 4 * 60, "ok": True})
        self.auto()
        self.assertEqual(len(self.remote_calls), 1)

    def test_failed_checks_back_off(self):
        self.fail_remote = OSError("hub down")
        out, _ = self.auto()
        self.assertEqual(out, "")
        st = self.st()
        self.assertIs(st["ok"], False)
        self.assertEqual(st["fails"], 1)
        self.assertIn("hub down", st["last_error"])
        self.auto()                                   # right away: no retry
        self.assertEqual(len(self.remote_calls), 1)
        self.age(61)
        self.auto()                                   # 1 min after the first failure
        self.assertEqual((len(self.remote_calls), self.st()["fails"]), (2, 2))
        self.age(61)
        self.auto()                                   # second failure waits 2 min
        self.assertEqual(len(self.remote_calls), 2)
        self.age(121)
        self.fail_remote = None
        self.auto()
        self.assertEqual(self.applied, [NEWER])
        st = self.st()
        self.assertTrue(st["ok"])
        self.assertNotIn("last_error", st)
        self.assertNotIn("fails", st)

    def test_backoff_caps_at_an_hour(self):
        st = {"checked": time.time() - 3601, "ok": False, "fails": 30}
        self.assertTrue(updater.due(st))
        st["checked"] = time.time() - 3000
        self.assertFalse(updater.due(st))
        self.assertTrue(updater.due({"checked": time.time() - 61, "ok": False}))    # old state without `fails`

    def test_failed_apply_is_recorded(self):
        with mock.patch.object(updater, "apply", mock.Mock(side_effect=ValueError("checksum mismatch"))):
            self.auto()
        self.assertIn("checksum mismatch", self.st()["last_error"])
        self.assertIs(self.st()["ok"], False)


class Background(Base):
    def test_command_does_no_network_work(self):
        calls = []
        with mock.patch.object(updater, "spawn", lambda: calls.append(1)):
            out, err = self.auto()
        self.assertEqual((self.remote_calls, self.applied, calls, out, err), ([], [], [1], "", ""))
        self.assertTrue(os.path.exists(updater._lock()))    # held for the worker, which releases it

    def test_one_worker_at_a_time(self):
        calls = []
        with mock.patch.object(updater, "spawn", lambda: calls.append(1)):
            for _ in range(5):
                self.auto()
        self.assertEqual(calls, [1])
        os.utime(updater._lock(), (time.time() - updater.STALE - 1,) * 2)   # dead worker: taken over
        with mock.patch.object(updater, "spawn", lambda: calls.append(1)):
            self.auto()
        self.assertEqual(calls, [1, 1])

    def test_failed_spawn_releases_the_lock(self):
        self.assertTrue(updater._acquire())
        with mock.patch.object(updater.subprocess, "Popen", mock.Mock(side_effect=OSError("no"))):
            SPAWN()
        self.assertFalse(os.path.exists(updater._lock()))

    def test_spawn_is_detached_and_quiet(self):
        with mock.patch.object(updater.subprocess, "Popen") as P:
            SPAWN()
        args, kw = P.call_args
        self.assertEqual(args[0][-2:], ["auto-update", "--background"])
        self.assertIs(kw["stdout"], updater.subprocess.DEVNULL)
        self.assertTrue(kw.get("start_new_session") or kw.get("creationflags"))

    def test_announce_only_on_the_new_version(self):
        paths.save("update.json", {"checked": time.time(), "ok": True, "announce": {"from": "0.1", "to": "0.2"}})
        o = io.StringIO()
        with contextlib.redirect_stdout(o):
            updater.auto("list")                      # still the old version (swap failed later?): say nothing, drop it
        self.assertEqual(o.getvalue(), "")
        self.assertNotIn("announce", self.st())

    def test_foreground_command(self):
        o = io.StringIO()
        with contextlib.redirect_stdout(o):
            updater.cmd_auto_update(argparse.Namespace(background=False))
        self.assertEqual(self.applied, [NEWER])
        self.assertIn("updated aihub", o.getvalue())
        self.assertFalse(os.path.exists(updater._lock()))


class State(Base):
    def test_pyz_keys_preserved(self):
        paths.save("update.json", {"pyz_version": "0.5", "pyz": "/h/aihub.pyz", "checked": 0})
        self.auto()
        st = self.st()
        self.assertEqual((st["pyz_version"], st["pyz"]), ("0.5", "/h/aihub.pyz"))
        self.fail_remote = OSError("x")
        self.age(10 ** 6)
        self.auto()
        self.assertEqual(self.st()["pyz"], "/h/aihub.pyz")

    def test_hub_install_keeps_other_keys(self):
        paths.save("update.json", {"skip_version": "7.7.7"})
        with mock.patch.object(updater, "apply"), mock.patch.object(updater, "pyz_path", lambda: os.path.join(self.tmp, "bin", "aihub.pyz")):
            updater.hub_install({"version": NEWER})
        st = self.st()
        self.assertEqual((st["skip_version"], st["pyz_version"]), ("7.7.7", NEWER))

    def test_skip_version_honored(self):
        paths.save("update.json", {"skip_version": NEWER})
        out, _ = self.auto()
        self.assertEqual((self.applied, out), ([], ""))
        self.assertEqual(self.st()["skip_version"], NEWER)
        self.info["version"] = "99.0.1"               # a newer one is fine
        paths.save("update.json", dict(self.st(), checked=0))
        self.auto()
        self.assertEqual(self.applied, ["99.0.1"])

    def test_rollback_records_skip_and_manual_upgrade_clears_it(self):
        dst = os.path.join(self.tmp, "aihub.pyz")
        for n in (dst, dst + ".prev"):
            open(n, "w").close()
        ns = argparse.Namespace(check=True, force=False, rollback=True)
        with mock.patch.object(updater, "target", lambda: dst), contextlib.redirect_stdout(io.StringIO()):
            updater.cmd_upgrade(ns)
        self.assertEqual(self.st()["skip_version"], VERSION)
        ns = argparse.Namespace(check=True, force=False, rollback=False)
        with contextlib.redirect_stdout(io.StringIO()):
            updater.cmd_upgrade(ns)
        self.assertNotIn("skip_version", self.st())

    def test_version_shows_settings(self):
        paths.save("update.json", {"checked": time.time(), "last_error": "boom", "ok": False})
        self.cfg(auto_update="notify", auto_update_hours=12)
        o = io.StringIO()
        with contextlib.redirect_stdout(o):
            updater.cmd_version(argparse.Namespace())
        t = o.getvalue()
        self.assertIn("auto-update: notify, checked in the background after every command (at most every 720 min), last check", t)
        self.assertIn("boom", t)


class Skips(Base):
    def test_hook_like_commands_skip(self):
        for c in updater.NEVER:
            self.auto(c)
        self.assertEqual(self.remote_calls, [])

    def test_machine_output_flags_skip(self):
        for flag in ("json", "plain", "name_only"):
            self.auto("list", argparse.Namespace(**{flag: True}))
        self.assertEqual(self.remote_calls, [])
        self.auto("list", argparse.Namespace(json=False, plain=False))
        self.assertEqual(len(self.remote_calls), 1)

    def test_non_interactive_updates_quietly(self):
        with mock.patch.object(ui, "INTERACTIVE", False):
            out, err = self.auto()
        self.assertEqual(self.applied, [NEWER])
        self.assertEqual(out, "")
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertIn("updated aihub", err)

    def test_non_interactive_notify_uses_stderr(self):
        self.cfg(auto_update="notify")
        with mock.patch.object(ui, "INTERACTIVE", False):
            self.auto()
            self.age(3600)
            out, err = self.auto()
        self.assertEqual(out, "")
        self.assertIn("available", err)

    def test_nothing_to_replace_from_source(self):
        with mock.patch.object(updater, "target", lambda: None):
            self.auto()
        self.assertEqual(self.remote_calls, [])

    def test_never_raises(self):
        with mock.patch.object(updater, "mode", mock.Mock(side_effect=RuntimeError("x"))):
            self.auto()


if __name__ == "__main__":
    unittest.main()
