"""Backward compatibility of what earlier versions saved (logins, git config, clones, update state) and the one wording of git errors."""
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from aihub.cli import credhelper as ch, gitx, remote_config as rc, updater, vault


class Mem:
    """A backend that behaves like the Keychain / Secret Service: addressed by key, cannot be listed."""
    name = "mem"
    data = {}

    @classmethod
    def usable(cls):
        return True

    @classmethod
    def put(cls, host, blob):
        cls.data[host] = blob
        return True

    @classmethod
    def get(cls, host):
        return cls.data.get(host)

    @classmethod
    def drop(cls, host):
        cls.data.pop(host, None)


class Home(unittest.TestCase):
    backends = [ch.File]

    def setUp(self):
        self.d = tempfile.mkdtemp()
        Mem.data = {}
        for p in (mock.patch.dict(os.environ, {"AIHUB_HOME": self.d}), mock.patch.object(ch, "backends", lambda: self.backends)):
            p.start()
            self.addCleanup(p.stop)

    def put_file(self, key, user, pw):
        d = ch._read_file()
        d[key] = json.dumps({"username": user, "password": pw})
        ch.paths.save("git-credentials.json", d, private=True)


class LegacyLogins(Home):
    def test_old_bare_key_keeps_working(self):
        self.put_file("git.example.com", "u", "tok")                       # what every earlier version wrote
        self.assertEqual(ch.lookup("git.example.com"), ("u", "tok"))
        out = io.StringIO()
        ch.run("get", "protocol=https\nhost=git.example.com\nusername=u\n", out)
        self.assertEqual(out.getvalue(), "username=u\npassword=tok\n")
        self.assertEqual(set(ch._read_file()), {"git.example.com"})          # not rewritten

    def test_old_explicit_port_key_keeps_working(self):
        self.put_file("git.example.com:8443", "u", "tok")
        self.assertEqual(ch.lookup("git.example.com:8443"), ("u", "tok"))

    def test_userinfo_keys_are_found_and_moved(self):
        for legacy in ("bob@git.example.com", "bob:s3cret@git.example.com", "Bob@Git.Example.com"):
            with self.subTest(legacy=legacy):
                ch._cache.clear()
                ch.paths.save("git-credentials.json", {}, private=True)
                self.put_file(legacy, "bob", "tok")
                self.assertEqual(ch.lookup("git.example.com"), ("bob", "tok"))
                self.assertEqual(set(ch._read_file()), {"git.example.com"})           # moved: bare key now, legacy key gone
                self.assertEqual(json.loads(ch._read_file()["git.example.com"]), {"username": "bob", "password": "tok"})

    def test_differently_cased_key_is_found(self):
        self.put_file("Git.Example.com", "u", "tok")
        self.assertEqual(ch.lookup("git.example.com"), ("u", "tok"))
        self.assertEqual(set(ch._read_file()), {"git.example.com"})

    def test_git_get_finds_and_moves_a_legacy_entry(self):
        self.put_file("bob@git.example.com", "bob", "tok")
        out = io.StringIO()
        ch.run("get", "protocol=https\nhost=git.example.com\nusername=bob\n", out)
        self.assertEqual(out.getvalue(), "username=bob\npassword=tok\n")
        self.assertEqual(set(ch._read_file()), {"git.example.com"})

    def test_other_hosts_legacy_entry_is_not_offered(self):
        self.put_file("bob@git.example.com:9999", "bob", "tok")
        self.put_file("bob@other.example.com", "bob", "tok2")
        self.assertIsNone(ch.lookup("git.example.com"))
        self.assertEqual(set(ch._read_file()), {"bob@git.example.com:9999", "bob@other.example.com"})

    def test_several_users_prefer_the_one_git_asked_for(self):
        self.put_file("amy@git.example.com", "amy", "a")
        self.put_file("bob@git.example.com", "bob", "b")
        out = io.StringIO()
        ch.run("get", "protocol=https\nhost=git.example.com\nusername=bob\n", out)
        self.assertEqual(out.getvalue(), "username=bob\npassword=b\n")
        self.assertEqual(set(ch._read_file()), {"git.example.com", "amy@git.example.com"})     # the other user is left alone

    def test_vault_remove_also_drops_a_legacy_key(self):
        self.put_file("bob@git.example.com", "bob", "tok")
        ch.paths.save("vault.json", {"git.example.com": {"scheme": "https", "username": "bob"}}, private=True)
        vault.do_remove(mock.Mock(remote="git.example.com", yes=True))
        self.assertEqual(ch._read_file(), {})

    def test_vault_list_sees_a_legacy_login(self):
        self.put_file("bob@git.example.com", "bob", "tok")
        ch.paths.save("vault.json", {"git.example.com": {"scheme": "https", "username": "bob"}}, private=True)
        self.assertEqual(vault._secret("git.example.com")["password"], "tok")


class LegacyKeychain(Home):
    backends = [Mem, ch.File]

    def test_bare_key_in_the_keychain_keeps_working(self):
        Mem.data["git.example.com"] = json.dumps({"username": "u", "password": "tok"})
        self.assertEqual(ch.lookup("git.example.com"), ("u", "tok"))

    def test_user_at_host_is_tried_with_the_vault_user(self):
        Mem.data["bob@git.example.com"] = json.dumps({"username": "bob", "password": "tok"})
        ch.paths.save("vault.json", {"git.example.com": {"scheme": "https", "username": "bob"}}, private=True)
        self.assertEqual(ch.lookup("git.example.com"), ("bob", "tok"))
        self.assertEqual(set(Mem.data), {"git.example.com"})

    def test_user_at_host_is_tried_with_the_user_git_sent(self):
        Mem.data["bob@git.example.com"] = json.dumps({"username": "bob", "password": "tok"})
        out = io.StringIO()
        ch.run("get", "protocol=https\nhost=git.example.com\nusername=bob\n", out)
        self.assertEqual(out.getvalue(), "username=bob\npassword=tok\n")
        self.assertEqual(set(Mem.data), {"git.example.com"})

    def test_a_placeholder_user_is_not_a_candidate(self):
        with mock.patch.object(Mem, "get", wraps=Mem.get) as g:
            ch.run("get", "protocol=https\nhost=git.example.com\nusername=oauth2\n", io.StringIO())
        self.assertNotIn(mock.call("oauth2@git.example.com"), g.call_args_list)


class RegisterLegacy(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = dict(os.environ, HOME=self.home, USERPROFILE=self.home, GIT_CONFIG_NOSYSTEM="1", AIHUB_HOME=self.home)
        self.real = subprocess.run
        p = mock.patch.object(ch.subprocess, "run", lambda args, **kw: self.real(args, env=self.env, **{k: v for k, v in kw.items() if k != "env"}))
        p.start()
        self.addCleanup(p.stop)

    def cfg(self, *a):
        return self.real(["git", "config", "--global"] + list(a), env=self.env, capture_output=True, text=True).stdout

    def keys(self):
        return set(self.cfg("--name-only", "--get-regexp", r"^credential\.").split())

    def test_old_userinfo_entries_of_ours_are_removed_others_stay(self):
        ours = ch.helper_value()
        for k in ("credential.https://bob@git.example.com.helper", "credential.https://amy:tok@git.example.com.helper"):
            self.cfg("--add", k, "")
            self.cfg("--add", k, ours)
        self.cfg("--add", "credential.https://zed@git.example.com.helper", "store")            # someone else's helper for that host: stays
        self.cfg("--add", "credential.https://bob@other.example.com.helper", ours)             # another host: stays
        self.cfg("--add", "credential.https://git.example.com.helper", "cache")                # the plain key held another helper before: reset below
        mixed = "credential.https://kim@git.example.com.helper"
        self.cfg("--add", mixed, ours)
        self.cfg("--add", mixed, "osxkeychain")                                                # ours + foreign in one key: left alone
        ch.register(["https://git.example.com/x.git"])
        self.assertEqual(self.keys(), {"credential.https://git.example.com.helper", "credential.https://zed@git.example.com.helper",
                                       "credential.https://bob@other.example.com.helper", mixed})
        self.assertEqual(self.cfg("--get-all", "credential.https://zed@git.example.com.helper").split(), ["store"])

    def test_already_registered_host_still_cleans_old_entries(self):
        ours = ch.helper_value()
        ch.register(["https://git.example.com/x.git"])
        self.cfg("--add", "credential.https://bob@git.example.com.helper", ours)
        self.assertEqual(ch.register(["https://git.example.com/x.git"]), ([], ["git.example.com"]))
        self.assertEqual(self.keys(), {"credential.https://git.example.com.helper"})


class OldClone(unittest.TestCase):
    def test_a_clone_made_under_the_old_key_is_reused_without_a_download(self):
        td = tempfile.mkdtemp()
        src = os.path.join(td, "src")
        os.makedirs(src)
        git = lambda *a, cwd=src: subprocess.check_call(["git", "-c", "user.email=a@b", "-c", "user.name=t"] + list(a), cwd=cwd, stdout=subprocess.DEVNULL)
        git("init", "-q", "-b", "main")
        open(os.path.join(src, "f"), "w").write("1")
        git("add", ".")
        git("commit", "-qm", "1")
        bare = os.path.join(td, "o.git")
        subprocess.check_call(["git", "clone", "-q", "--bare", src, bare])
        url = "file://" + bare
        base = os.path.join(td, "cache")
        old = os.path.join(base, gitx._cache_key(url))                  # the folder name earlier versions used
        os.makedirs(base)
        subprocess.check_call(["git", "clone", "-q", url, old])
        self.assertNotEqual(old, os.path.join(base, gitx._cache_key(gitx._norm_url(url))))
        calls = []
        real = gitx.run

        def spy(args, *a, **k):
            calls.append(args[0])
            return real(args, *a, **k)
        with mock.patch.object(gitx, "run", spy):
            d = gitx.fetch(url, "main", None, base=base)
        self.assertEqual(d, old)
        self.assertNotIn("clone", calls)
        self.assertEqual(os.listdir(base), [os.path.basename(old)])


class UpdateState(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        for p in (mock.patch.dict(os.environ, {"AIHUB_HOME": self.d}),):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("AIHUB_NO_UPDATE", None)
        os.environ.pop("AIHUB_AUTO_UPDATE", None)
        os.environ.pop("AIHUB_AUTO_UPDATE_HOURS", None)
        os.environ.pop("AIHUB_AUTO_UPDATE_MINUTES", None)

    def config(self, **kw):
        ch.paths.save("config.json", kw)

    def test_old_update_json_shapes_are_read(self):
        now = time.time()
        updater.paths.save("update.json", {"checked": now - 30})
        self.assertFalse(updater.due(updater.state(), now))
        updater.paths.save("update.json", {"checked": now - 30, "pyz_version": "0.7.0", "pyz": "/x/aihub.pyz"})
        self.assertFalse(updater.due(updater.state(), now))
        self.assertTrue(updater.due({"checked": now - 25 * 3600}, now))
        self.assertTrue(updater.due({}, now))
        st = updater._state({"checked": now, "ok": True})
        self.assertEqual((st["pyz_version"], st["pyz"]), ("0.7.0", "/x/aihub.pyz"))        # merged, not replaced

    def test_old_auto_update_values(self):
        for raw, want in ((True, "on"), (False, "off"), ("true", "on"), ("false", "off"), ("1", "on"), ("0", "off"), ("off", "off"), ("on", "on"),
                          ("notify", "notify"), ("", "on")):
            self.config(auto_update=raw)
            self.assertEqual(updater.mode(), want, repr(raw))
        self.config()
        self.assertEqual(updater.mode(), "on")                                                # unset = on, as before
        with mock.patch.dict(os.environ, {"AIHUB_NO_UPDATE": "1"}):
            self.assertEqual(updater.mode(), "off")


class HostKey(unittest.TestCase):
    TABLE = [("https://git.example.com/a/b.git", "git.example.com"), ("https://Git.Example.COM/a", "git.example.com"),
             ("https://bob@git.example.com/a", "git.example.com"), ("https://bob:tok@Git.example.com:8443/a", "git.example.com:8443"),
             ("https://git.example.com:443/a", "git.example.com:443"), ("http://[::1]:9/x", "[::1]:9"), ("https://[2001:DB8::1]/x", "[2001:db8::1]"),
             ("git.example.com", "git.example.com"), ("Git.Example.com:8443", "git.example.com:8443"), ("bob@git.example.com", "git.example.com"),
             ("https://git.example.com", "git.example.com"), ("https://git.example.com/", "git.example.com")]
    BAD = ["", "file:///tmp/x", "ssh://git@h/x", "https://", "https://evil:80#@good/x", "https://h/a b"]

    def test_the_three_agree(self):
        for url, want in self.TABLE:
            with self.subTest(url=url):
                self.assertEqual(ch.host_of(url), want)
                self.assertEqual(vault._norm(url)[1], want)
                full = url if "://" in url else "https://" + url
                self.assertEqual(gitx._host_of(full)[1], want)
                self.assertEqual(gitx.host_key(url), want)

    def test_the_three_agree_on_refusals(self):
        for url in self.BAD:
            with self.subTest(url=url):
                self.assertEqual(ch.host_of(url), "")
                with self.assertRaises(ValueError):
                    vault._norm(url)
                self.assertIsNone(gitx._host_of(url if "://" in url else "https://" + url) if url else None)


class Hints(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(gitx, "LOGIN", None)
        p.start()
        self.addCleanup(p.stop)

    AUTH = "remote: HTTP Basic: Access denied\nfatal: Authentication failed for 'https://bob:tok3n@Git.Example.com/a/b.git/'"

    def hints(self, text):
        return [ln for ln in text.splitlines() if ln.startswith("[aihub hint]")]

    def test_auth_hint_uses_the_host_from_the_message_and_hides_secrets(self):
        out = gitx.explain(self.AUTH)
        self.assertTrue(out.startswith(self.AUTH))
        self.assertEqual(self.hints(out), ["[aihub hint] git login for git.example.com was refused or is missing. Run: aihub vault add "
                                           "https://git.example.com --user NAME  (2FA accounts: use a personal access token, not the "
                                           "password). Check it: aihub vault test git.example.com"])
        self.assertNotIn("tok3n", self.hints(out)[0])

    def test_auth_hint_from_the_url_argument(self):
        out = gitx.explain("fatal: could not read Username", "https://u:pw@Git.Example.com:8443/a.git")
        self.assertIn("aihub vault add https://git.example.com:8443 --user NAME", out)
        self.assertNotIn("pw", self.hints(out)[0])

    def test_server_wording(self):
        with mock.patch.object(gitx, "LOGIN", lambda h: None):
            out = gitx.explain(self.AUTH)
        self.assertEqual(self.hints(out), ["[aihub hint] the hub has no working git login for git.example.com: set AIHUB_GIT_TOKEN (+AIHUB_GIT_USER, "
                                           "AIHUB_GIT_HOSTS=git.example.com) in the server env, or run `aihub vault add https://git.example.com` "
                                           "as the account that runs the server"])

    def test_network_hint(self):
        out = gitx.explain("fatal: unable to access 'https://git.example.com/a/b.git/': Could not resolve host: git.example.com")
        self.assertEqual(self.hints(out), ["[aihub hint] cannot reach git.example.com (network, proxy or TLS). Check: git ls-remote "
                                           "https://git.example.com/a/b.git/; behind a proxy set AIHUB_GIT_PROXY; private CA: AIHUB_CA_BUNDLE=<ca.pem>"])

    def test_missing_ref_hint(self):
        out = gitx.explain("fatal: couldn't find remote ref refs/heads/gone")
        self.assertEqual(self.hints(out), ["[aihub hint] the published commit/branch is not on the remote (force-push or branch deleted?). "
                                           "Re-publish: aihub dev publish, or install another version: aihub install NAME@VERSION"])

    def test_one_hint_only_and_idempotent(self):
        once = gitx.explain(self.AUTH)
        self.assertEqual(gitx.explain(once), once)
        self.assertEqual(len(self.hints(gitx.explain(once, "https://other.example/x"))), 1)
        d = "fatal: detected dubious ownership in repository at '/srv/x'\nfatal: Authentication failed for 'https://h.example/a'"
        self.assertEqual(len(self.hints(gitx.explain(d))), 1)
        self.assertIn("safe.directory /srv/x", gitx.explain(d))

    def test_no_hint_without_a_host_or_for_plain_errors(self):
        for msg in ("fatal: not a git repository",
                    "error: unable to create file x: Permission denied", "fatal: Could not read from remote repository."):
            self.assertEqual(gitx.explain(msg), msg)

    def test_a_hint_never_changes_the_classification(self):
        for msg in (self.AUTH, "fatal: unable to access 'https://git.example.com:403/a.git/': Connection refused",
                    "fatal: couldn't find remote ref refs/heads/gone"):
            with self.subTest(msg=msg):
                full = gitx.explain(msg)
                self.assertNotEqual(full, msg)
                self.assertEqual(gitx.fetch_failure(full), gitx.fetch_failure(msg))
                self.assertEqual(rc.is_auth_error(full), rc.is_auth_error(msg))

    def test_run_attaches_the_hint_for_the_url_it_was_given(self):
        r = subprocess.CompletedProcess([], 128, "", self.AUTH)
        with mock.patch.object(gitx.subprocess, "run", return_value=r):
            with self.assertRaises(gitx.GitError) as c:
                gitx.run(["ls-remote", "https://bob@git.example.com/a/b.git"])
        self.assertEqual(len(self.hints(str(c.exception))), 1)
        self.assertTrue(rc.is_auth_error(c.exception))

    def test_run_finds_the_host_from_origin_for_a_bare_fetch(self):
        td = tempfile.mkdtemp()
        subprocess.check_call(["git", "init", "-q"], cwd=td)
        subprocess.check_call(["git", "remote", "add", "origin", "https://tok@git.example.com:8443/a.git"], cwd=td)
        r = subprocess.CompletedProcess([], 128, "", "fatal: Authentication failed")
        real = subprocess.run
        with mock.patch.object(gitx.subprocess, "run", side_effect=lambda a, **k: r if a[:2] == ["git", "fetch"] else real(a, **k)):
            with self.assertRaises(gitx.GitError) as c:
                gitx.run(["fetch", "origin", "main"], cwd=td)
        self.assertIn("aihub vault test git.example.com:8443", str(c.exception))
        self.assertNotIn("tok@", str(c.exception))

    def test_a_dead_port_gets_the_network_hint(self):
        with self.assertRaises(gitx.GitError) as c:
            gitx.run(["ls-remote", "https://127.0.0.1:1/x.git"], timeout=30)
        self.assertIn("cannot reach 127.0.0.1:1", str(c.exception))

    def test_timeout_gets_the_network_hint(self):
        with mock.patch.object(gitx.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 1)):
            with self.assertRaises(gitx.GitError) as c:
                gitx.run(["fetch", "https://git.example.com/a.git"], timeout=1)
        self.assertIn("timed out after 1s", str(c.exception))
        self.assertIn("cannot reach git.example.com", str(c.exception))

    def test_cannot_find_error(self):
        td = tempfile.mkdtemp()
        bare = os.path.join(td, "o.git")
        subprocess.check_call(["git", "init", "-q", "--bare", bare])
        with self.assertRaises(gitx.GitError) as c:
            gitx.fetch("file://" + bare, "main", "a" * 40, base=os.path.join(td, "c"))
        text = str(c.exception)
        self.assertTrue(text.startswith("cannot find '%s'" % ("a" * 40)))
        self.assertEqual(len(self.hints(text)), 1)
        self.assertNotIn("[aihub hint]", text.splitlines()[0])
        self.assertIn("aihub install NAME@VERSION", text.splitlines()[-1])

    def test_ensure_git_access_errors_carry_the_hint(self):
        r = subprocess.CompletedProcess([], 128, "", "fatal: unable to access 'https://tok@git.example.com/a.git/': Failed to connect")
        with mock.patch.object(rc.subprocess, "run", return_value=r):
            with self.assertRaises(gitx.GitError) as c:
                rc.ensure_git_access("https://tok@git.example.com/a.git")
        self.assertIn("cannot reach git.example.com", str(c.exception))
        self.assertNotIn("tok@", str(c.exception))
        r = subprocess.CompletedProcess([], 128, "", "fatal: Authentication failed for 'https://git.example.com/a.git/'")
        with mock.patch.object(rc.subprocess, "run", return_value=r), mock.patch.object(rc.ui, "INTERACTIVE", False):
            with self.assertRaises(gitx.GitError) as c:
                rc.ensure_git_access("https://git.example.com/a.git")
        self.assertIn("git credentials are needed for https://git.example.com/a.git", str(c.exception))
        self.assertIn("aihub vault add https://git.example.com --user NAME", str(c.exception))

    def test_vault_save_failure_uses_the_same_wording(self):
        d = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"AIHUB_HOME": d}), mock.patch.object(rc, "store_git", return_value=False):
            with self.assertRaises(ValueError) as c:
                vault._save("https", "h.example", "u", "t")
        self.assertIn("\n[aihub hint] ", str(c.exception))
        self.assertIn("aihub vault test h.example", str(c.exception))

    def test_backup_ls_remote_errors_carry_the_hint(self):
        from aihub.server import backup
        r = subprocess.CompletedProcess([], 128, "", "fatal: Authentication failed for 'https://tok@git.example.com/a.git/'")
        with mock.patch.object(gitx, "_ls", return_value=r):
            with self.assertRaises(gitx.GitError) as c:
                backup.Backup._checkout("https://tok@git.example.com/a.git", "main", tempfile.mkdtemp())
        self.assertEqual(len(self.hints(str(c.exception))), 1)
        self.assertNotIn("tok@", str(c.exception))
        with mock.patch.object(gitx, "_ls", return_value=None):
            with self.assertRaises(gitx.GitError) as c:
                backup.Backup._checkout("https://git.example.com/a.git", "main", tempfile.mkdtemp())
        self.assertIn("cannot reach git.example.com", str(c.exception))


if __name__ == "__main__":
    unittest.main()
