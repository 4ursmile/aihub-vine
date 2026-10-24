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
        self.call("erase", base + "username=u\npassword=old\n")          # git rejected a stale password: the new one stays
        self.assertEqual(self.call("get", base), "username=u\npassword=new\n")
        self.call("erase", base + "username=u\npassword=new\n")
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


class KeepsYourGitLogins(unittest.TestCase):
    """register must never hide a helper you already use: ours first, then yours; uninstall puts back exactly what was there."""
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = dict(os.environ, HOME=self.home, USERPROFILE=self.home, GIT_CONFIG_NOSYSTEM="1", AIHUB_HOME=os.path.join(self.home, ".aihub"),
                        GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="true", SSH_ASKPASS="true")       # never an IDE/GUI login prompt
        for k in [k for k in self.env if k.startswith("VSCODE_GIT")]:
            self.env.pop(k)
        self.real = subprocess.run
        env = self.env
        p = mock.patch.object(ch.subprocess, "run", lambda args, **kw: self.real(args, env=env, **{k: v for k, v in kw.items() if k != "env"}))
        p.start()
        self.addCleanup(p.stop)
        h = mock.patch.dict(os.environ, {"AIHUB_HOME": env["AIHUB_HOME"]})
        h.start()
        self.addCleanup(h.stop)
        self.store = os.path.join(self.home, "my-creds")
        with open(self.store, "w") as f:
            f.write("https://me:my-own-token@git.example.com\n")                # a login saved by YOUR helper before aihub
        self.git("config", "--global", "credential.helper", "store --file %s" % self.store)
        self.ours = os.path.join(self.home, "ours.sh")
        self.ours_login = ""

    def git(self, *a, inp=None):
        return self.real(["git"] + list(a), env=self.env, input=inp, capture_output=True, text=True).stdout

    def fake_ours(self, login):
        with open(self.ours, "w") as f:
            f.write("#!/bin/sh\nfor a; do last=$a; done\ntest \"$last\" = get || exit 0\ncat >/dev/null\n%s\n" % ("printf 'username=vault\\npassword=%s\\n'" % login if login else "exit 0"))
        os.chmod(self.ours, 0o755)
        return "!%s aihub credential" % self.ours                          # looks like ours to _mine()

    def fill(self):
        out = self.git("credential", "fill", inp="protocol=https\nhost=git.example.com\n\n")
        return dict(l.split("=", 1) for l in out.splitlines() if "=" in l).get("password")

    @unittest.skipIf(os.name == "nt", "posix shell helper")
    def test_your_login_still_works_when_aihub_has_none(self):
        with mock.patch.object(ch, "helper_value", return_value=self.fake_ours("")):
            ch.register(["https://git.example.com/x.git"])
        self.assertEqual(self.fill(), "my-own-token")

    @unittest.skipIf(os.name == "nt", "posix shell helper")
    def test_aihub_login_answers_first_when_it_has_one(self):
        with mock.patch.object(ch, "helper_value", return_value=self.fake_ours("vault-token")):
            ch.register(["https://git.example.com/x.git"])
        self.assertEqual(self.fill(), "vault-token")

    @unittest.skipIf(os.name == "nt", "posix shell helper")
    def test_host_helpers_you_set_are_kept_and_restored(self):
        key = "credential.https://git.example.com.helper"
        self.git("config", "--global", "--add", key, "cache --timeout 60")
        with mock.patch.object(ch, "helper_value", return_value=self.fake_ours("")):
            ours = ch.helper_value()
            self.assertEqual(ch.register(["https://git.example.com/x.git"]), (["git.example.com"], []))
            got = self.git("config", "--global", "--get-all", key).splitlines()
            self.assertEqual(got, ["", ours, "store --file %s" % self.store, "cache --timeout 60"])
            self.assertEqual(ch.register(["https://git.example.com/x.git"]), ([], ["git.example.com"]))     # idempotent
            self.git("config", "--global", "credential.helper", "osxkeychain")                              # you change your helper later
            ch.register(["https://git.example.com/x.git"])
            self.assertEqual(self.git("config", "--global", "--get-all", key).splitlines(), ["", ours, "osxkeychain", "cache --timeout 60"])
            ch.unregister()
        self.assertEqual(self.git("config", "--global", "--get-all", key).splitlines(), ["cache --timeout 60"])

    @unittest.skipIf(os.name == "nt", "posix shell helper")
    def test_key_written_by_an_older_aihub_is_upgraded(self):
        key = "credential.https://git.example.com.helper"
        with mock.patch.object(ch, "helper_value", return_value=self.fake_ours("")):
            ours = ch.helper_value()
            self.git("config", "--global", "--add", key, "")
            self.git("config", "--global", "--add", key, ours)                 # old shape: reset + ours hid your helper
            self.assertFalse(self.fill())                                       # proves the old shape hid it
            ch.register(["https://git.example.com/x.git"])
            self.assertEqual(self.fill(), "my-own-token")
            ch.unregister()
        self.assertEqual(self.git("config", "--global", "--get-all", key).splitlines(), [])
        self.assertEqual(self.fill(), "my-own-token")


class Store(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"AIHUB_HOME": self.d})
        self.env.start()
        self.bk = mock.patch.object(ch, "backends", lambda: [ch.File])
        self.bk.start()
        self.ui = [mock.patch.object(rc.ui, "bad"), mock.patch.object(rc, "ensure_helper")]
        self.bad, _ = [p.start() for p in self.ui]

    def tearDown(self):
        for p in self.ui:
            p.stop()
        self.bk.stop()
        self.env.stop()

    def test_store_git_stores_in_process_and_verifies(self):
        with mock.patch.object(rc.subprocess, "run", side_effect=AssertionError("no git credential subprocess")):
            self.assertTrue(rc.store_git("https://Bob:x@H.example:8443/x.git", "u", "t"))
        self.assertEqual(ch.lookup("h.example:8443"), ("u", "t"))
        self.assertTrue(rc.store_git("https://h.example:8443", "u", "t2"))          # replaced, still one entry
        self.assertEqual(ch.lookup("h.example:8443"), ("u", "t2"))
        self.assertEqual(len(ch._read_file()), 1)

    def test_store_git_reports_a_backend_that_does_not_keep_it(self):
        class Lossy(ch.File):
            @classmethod
            def put(cls, host, blob):
                return True                       # claims success, keeps nothing
        with mock.patch.object(ch, "backends", lambda: [Lossy]):
            self.assertFalse(rc.store_git("https://h.example", "u", "t"))
        self.bad.assert_called()

        class Broken(ch.File):
            @classmethod
            def put(cls, host, blob):
                raise OSError("locked")
        with mock.patch.object(ch, "backends", lambda: [Broken]):
            self.assertFalse(rc.store_git("https://h.example", "u", "t", quiet=True))

    def test_vault_save_raises_when_not_stored(self):
        from aihub.cli import vault
        with mock.patch.object(rc, "store_git", return_value=False):
            with self.assertRaises(ValueError):
                vault._save("https", "h.example", "u", "t")
        self.assertEqual(vault._load(), {})
        vault._save("https", "h.example", "u", "t")
        self.assertEqual(vault._load()["h.example"]["username"], "u")
        self.assertEqual(ch.lookup("h.example"), ("u", "t"))


class Erase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"AIHUB_HOME": self.d})
        self.env.start()
        self.bk = mock.patch.object(ch, "backends", lambda: [ch.File])
        self.bk.start()
        ch.save("git.example.com", "u", "tok")

    def tearDown(self):
        self.bk.stop()
        self.env.stop()

    def erase(self, text):
        ch.run("erase", text, io.StringIO())

    def test_missing_or_other_password_keeps_the_entry(self):
        self.erase("protocol=https\nhost=git.example.com\nusername=u\n")
        self.erase("protocol=https\nhost=git.example.com\nusername=u\npassword=other\n")
        self.assertEqual(ch.lookup("git.example.com"), ("u", "tok"))

    def test_the_rejected_password_erases(self):
        self.erase("protocol=https\nhost=git.example.com\nusername=u\npassword=tok\n")
        self.assertIsNone(ch.lookup("git.example.com"))
        self.assertEqual(ch._read_file(), {})

    def test_vault_host_is_never_erased_by_git_but_vault_remove_does(self):
        from aihub.cli import vault
        vault._save("https", "git.example.com", "u", "tok")
        self.erase("protocol=https\nhost=git.example.com\nusername=u\npassword=tok\n")
        self.assertEqual(ch.lookup("git.example.com"), ("u", "tok"))
        vault.do_remove(mock.Mock(remote="git.example.com", yes=True))
        self.assertIsNone(ch.lookup("git.example.com"))
        self.assertNotIn("git.example.com", vault._load())


class Get(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"AIHUB_HOME": self.d})
        self.env.start()
        self.bk = mock.patch.object(ch, "backends", lambda: [ch.File])
        self.bk.start()
        ch.save("git.example.com", "alice", "tok")

    def tearDown(self):
        self.bk.stop()
        self.env.stop()

    def get(self, user):
        out = io.StringIO()
        ch.run("get", "protocol=https\nhost=git.example.com\nusername=%s\n" % user, out)
        return out.getvalue()

    def test_placeholder_user_gets_the_stored_login(self):
        for u in ("oauth2", "x-access-token", "PersonalAccessToken"):
            self.assertEqual(self.get(u), "username=alice\npassword=tok\n")

    def test_other_user_still_gets_nothing(self):
        self.assertEqual(self.get("bob"), "")
        self.assertEqual(self.get("alice"), "username=alice\npassword=tok\n")


class Cache(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"AIHUB_HOME": self.d})
        self.env.start()
        self.bk = mock.patch.object(ch, "backends", lambda: [ch.File])
        self.bk.start()
        ch.save("h.example", "u", "tok")

    def tearDown(self):
        self.bk.stop()
        self.env.stop()

    def test_last_good_value_when_the_backend_raises(self):
        self.assertEqual(ch.lookup("h.example"), ("u", "tok"))
        with mock.patch.object(ch, "CACHE_TTL", 0), mock.patch.object(ch.File, "get", side_effect=OSError("keychain busy")):
            self.assertEqual(ch.lookup("h.example"), ("u", "tok"))
            self.assertIsNone(ch.lookup("other.example"))               # nothing cached for it: still None

    def test_clean_not_found_forgets(self):
        self.assertEqual(ch.lookup("h.example"), ("u", "tok"))
        os.remove(os.path.join(self.d, "git-credentials.json"))
        with mock.patch.object(ch, "CACHE_TTL", 0):
            self.assertIsNone(ch.lookup("h.example"))
            with mock.patch.object(ch.File, "get", side_effect=OSError("x")):
                self.assertIsNone(ch.lookup("h.example"))               # dropped, so a failure has nothing to fall back on

    def test_save_and_forget_invalidate(self):
        self.assertEqual(ch.lookup("h.example"), ("u", "tok"))
        ch.save("h.example", "u", "new")
        self.assertEqual(ch.lookup("h.example"), ("u", "new"))
        ch.forget("h.example")
        self.assertIsNone(ch.lookup("h.example"))

    def test_host_of(self):
        self.assertEqual(ch.host_of("https://Bob:tok@Git.Example.com:8443/x.git"), "git.example.com:8443")
        self.assertEqual(ch.host_of("https://[::1]:9/x"), "[::1]:9")
        self.assertEqual(ch.host_of("git.example.com"), "git.example.com")


class RegisterUserinfo(unittest.TestCase):
    def test_key_has_no_userinfo(self):
        home = tempfile.mkdtemp()
        env = dict(os.environ, HOME=home, USERPROFILE=home, GIT_CONFIG_NOSYSTEM="1", AIHUB_HOME=home)
        real = subprocess.run

        def run(args, **kw):
            return real(args, env=env, **{k: v for k, v in kw.items() if k != "env"})
        with mock.patch.object(ch.subprocess, "run", run):
            self.assertEqual(ch.register(["https://bob:tok@git.example.com/x.git"]), (["git.example.com"], []))
            self.assertEqual(ch.register(["https://git.example.com/y.git", "https://amy@git.example.com/z.git"]), ([], ["git.example.com"]))
            keys = real(["git", "config", "--global", "--name-only", "--get-regexp", r"^credential\."], env=env, capture_output=True, text=True).stdout.split()
        self.assertEqual(set(keys), {"credential.https://git.example.com.helper"})


if __name__ == "__main__":
    unittest.main()
