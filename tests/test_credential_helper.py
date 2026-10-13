import io
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from aihub.cli import credhelper as ch, remote_config as rc


class Roundtrip(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"AIHUB_HOME": self.d})
        self.env.start()
        self.bk = mock.patch.object(ch, "backends", lambda: [ch.File])      # same code path on every OS, no real keychain
        self.bk.start()

    def tearDown(self):
        self.bk.stop()
        self.env.stop()

    def call(self, action, text):
        out = io.StringIO()
        ch.run(action, text, out)
        return out.getvalue()

    def test_store_get_replace_erase(self):
        base = "protocol=https\nhost=git.example.com\n"
        self.call("store", base + "username=u\npassword=old\n")
        self.call("store", base + "username=u\npassword=new\n")           # replaces, no duplicate
        self.assertEqual(self.call("get", base), "username=u\npassword=new\n")
        self.assertEqual(len(ch._read_file()), 1)
        self.call("erase", base)
        self.assertEqual(self.call("get", base), "")

    def test_other_host_not_returned(self):
        self.call("store", "host=a.com\nusername=u\npassword=p\n")
        self.assertEqual(self.call("get", "host=b.com\n"), "")

    def test_file_is_private(self):
        self.call("store", "host=a.com\nusername=u\npassword=p\n")
        if os.name != "nt":
            self.assertEqual(os.stat(os.path.join(self.d, "git-credentials.json")).st_mode & 0o077, 0)


class Register(unittest.TestCase):
    def test_idempotent_and_scoped(self):
        home = tempfile.mkdtemp()
        env = dict(os.environ, HOME=home, USERPROFILE=home, GIT_CONFIG_NOSYSTEM="1", AIHUB_HOME=home)
        real = subprocess.run

        def run(args, **kw):
            return real(args, env=env, **{k: v for k, v in kw.items() if k != "env"})
        with mock.patch.object(ch.subprocess, "run", run):
            self.assertEqual(ch.register(["https://git.example.com/team/x.git", "file:///nope"]), (["git.example.com"], []))
            self.assertEqual(ch.register(["https://git.example.com/other.git"]), ([], ["git.example.com"]))   # already right: untouched
            got = real(["git", "config", "--global", "--get-all", "credential.https://git.example.com.helper"], env=env, capture_output=True, text=True).stdout.splitlines()
            self.assertEqual(len(got), 2)                                   # "" reset + ours, never repeated
            self.assertEqual(got[0], "")
            ch.unregister()
            left = real(["git", "config", "--global", "--get-all", "credential.https://git.example.com.helper"], env=env, capture_output=True, text=True).stdout.splitlines()
            self.assertEqual([x for x in left if x], [])


class Store(unittest.TestCase):
    def test_store_git_rejects_then_approves(self):
        seen = []

        def fake(args, **kw):
            seen.append(args[2])
            return mock.Mock(returncode=0, stdout="")
        with mock.patch.object(rc, "ensure_helper"), mock.patch.object(rc.subprocess, "run", fake):
            self.assertTrue(rc.store_git("https://h.example/x.git", "u", "t"))
        self.assertEqual(seen, ["reject", "approve"])


if __name__ == "__main__":
    unittest.main()
