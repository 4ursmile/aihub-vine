"""Regressions for a hub behind a reverse proxy that lost events and backups:

  - `fatal: detected dubious ownership` on the server's own git folders, and git with no login (vault / token)
  - `'utf-8' codec can't encode character '\\udc8d'`: one bad event poisoned every later batch and the nightly backup
  - URLs (logo, avatars, scripts) that ignored the `/aihub` path prefix
and a stress run with hostile text mixed into heavy traffic."""
import gzip
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from aihub.cli import credhelper, gitx
from aihub.core import textsafe
from aihub.server import backup as B, basepath, gitauth
from aihub.server.config import Settings
from aihub.server.db import init_db
from aihub.server.main import create_app
from aihub.server.repos import Repos
from tests.test_hub_first import admin, ev

SURR = "/h\udc8d/a"
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8"
       b"\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x9a\xa0\xa0\x00\x00\x00\x00IEND\xaeB`\x82")


def client(**kw):
    return TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=tempfile.mkdtemp(), **kw)))


class TextSafe(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(textsafe.clean("a\udc8db\x00c"), "a�bc")
        self.assertEqual(textsafe.clean("fine ✓"), "fine ✓")
        self.assertEqual(textsafe.clean(5), 5)
        self.assertIsNone(textsafe.clean(None))

    def test_deep_cleans_keys_values_and_nested(self):
        out = textsafe.deep({"k\udc8d": ["x\ud800", {"y": "z\x00"}], "n": 1})
        self.assertEqual(out, {"k�": ["x�", {"y": "z"}], "n": 1})
        json.dumps(out).encode("utf-8")                                   # and it encodes

    def test_deep_returns_the_same_object_when_nothing_changed(self):
        d = {"a": ["b", {"c": "d"}]}
        self.assertIs(textsafe.deep(d), d)


class EventStore(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.r = Repos(init_db(Settings(data_dir=self.d)))

    def row(self, i, **k):
        return dict({"ts": time.time(), "kind": "use", "package": "p", "ext_id": "id%d" % i, "version": "1", "client_id": "c"}, **k)

    def ids(self):
        return [x["ext_id"] for x in self.r.db.conn().execute("SELECT ext_id FROM events ORDER BY id").fetchall()]

    def test_surrogate_and_nul_are_stored_cleaned(self):
        self.assertEqual(self.r.events_insert_ext([self.row(1, cwd=SURR, detail="a\x00b", host="\ud800")]), 1)
        x = self.r.db.conn().execute("SELECT cwd,detail,host FROM events").fetchone()
        self.assertEqual((x["cwd"], x["detail"], x["host"]), ("/h�/a", "ab", "�"))

    def test_a_row_that_cannot_be_stored_is_skipped_and_the_rest_land(self):
        n = self.r.events_insert_ext([self.row(1), self.row(2, ts=object()), self.row(3)])
        self.assertEqual((n, self.ids(), self.r.event_rejects), (2, ["id1", "id3"], 1))
        self.assertIn("ProgrammingError", self.r.event_reject_last)

    def test_no_transaction_is_left_open_after_a_failure(self):
        self.r.events_insert_ext([self.row(1, ts=object())])
        self.assertFalse(self.r.db.conn().raw.in_transaction)             # an open one would lock every web write

    def test_a_busy_database_still_raises_so_the_batch_is_kept(self):
        with mock.patch.object(self.r, "_bulk", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.OperationalError):
                self.r.events_insert_ext([self.row(1)])
        self.assertFalse(self.r.db.conn().raw.in_transaction)

    def test_resending_is_idempotent(self):
        rows = lambda: [self.row(1), self.row(2)]
        self.assertEqual(self.r.events_insert_ext(rows()), 2)
        self.assertEqual(self.r.events_insert_ext(rows()), 0)

    def test_stored_invalid_bytes_can_be_read_back(self):
        c = self.r.db.conn().raw
        c.execute("INSERT INTO events(ts,kind,package,ext_id,cwd) VALUES(1,'use','p','bad',CAST(x'2f68edb28d2f61' AS TEXT))")
        c.commit()
        self.assertIn("/h", self.r.db.conn().execute("SELECT cwd FROM events WHERE ext_id='bad'").fetchone()["cwd"])


class BulkAtomicity(unittest.TestCase):
    """The per-row fallback is ONE transaction: a batch that ends in an exception leaves nothing behind (so the retry cannot duplicate)."""
    def setUp(self):
        self.r = Repos(init_db(Settings(data_dir=tempfile.mkdtemp())))
        self.c = self.r.db.conn()

    def row(self, i, **k):
        return dict({"ts": time.time(), "kind": "use", "package": "p", "ext_id": i, "version": "1", "client_id": "c"}, **k)

    def n(self):
        return self.c.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def test_a_non_content_failure_in_the_fallback_phase_rolls_back_every_row(self):
        self.c.execute("CREATE TRIGGER boom BEFORE INSERT ON events WHEN NEW.ext_id='boom' BEGIN SELECT RAISE(ABORT,'no'); END")
        self.c.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.r.events_insert_ext([self.row("a"), self.row("b", ts=object()), self.row("c"), self.row("boom"), self.row("d")])
        self.assertEqual(self.n(), 0)
        self.assertFalse(self.c.raw.in_transaction)
        self.c.execute("DROP TRIGGER boom")
        self.c.commit()
        batch = [self.row("a"), self.row("boom"), self.row("c")]
        self.assertEqual(self.r.events_insert_ext(batch), 3)               # the retry stores each row exactly once
        self.assertEqual(self.r.events_insert_ext(batch), 0)
        self.assertEqual(self.n(), 3)

    def test_events_insert_batch_cannot_duplicate_on_retry(self):
        """The EventBuffer path has no ext_id to dedupe on, so atomicity is its only protection."""
        self.c.execute("CREATE TRIGGER boom BEFORE INSERT ON events WHEN NEW.package='boom' BEGIN SELECT RAISE(ABORT,'no'); END")
        self.c.commit()
        batch = [self.row("a"), self.row("b", ts=object()), self.row("c", package="boom")]
        for _ in range(2):
            with self.assertRaises(sqlite3.IntegrityError):
                self.r.events_insert_batch([dict(x) for x in batch])
        self.assertEqual(self.n(), 0)

    def test_bad_rows_anywhere_are_found_and_the_rest_stored(self):
        rows = [self.row("x%d" % i) for i in range(40)]
        for i in (0, 13, 39):
            rows[i]["ts"] = object()
        self.assertEqual(self.r.events_insert_ext(rows), 37)
        self.assertEqual(self.r.event_rejects, 3)
        allbad = [self.row("y%d" % i, ts=object()) for i in range(6)]
        self.assertEqual(self.r.events_insert_ext(allbad), 0)
        self.assertEqual(self.r.event_rejects, 9)

    def test_one_bad_row_does_not_make_a_big_window_slow(self):
        rows = [self.row("g%d" % i) for i in range(5000)]
        rows[2500]["ts"] = object()
        t0 = time.time()
        self.assertEqual(self.r.events_insert_ext(rows), 4999)
        self.assertLess(time.time() - t0, 3.0, "bisection, not one savepoint per row")


class BackupTolerance(unittest.TestCase):
    def test_export_survives_bad_bytes_already_in_the_database(self):
        d = tempfile.mkdtemp()
        r = Repos(init_db(Settings(data_dir=d)))
        c = r.db.conn().raw
        c.execute("INSERT INTO events(ts,kind,package,ext_id,cwd) VALUES(1,'use','p','x1',CAST(x'2f68edb28d2f61' AS TEXT))")
        c.execute("INSERT INTO events(ts,kind,package,ext_id,cwd) VALUES(2,'use','p','x2',CAST(x'2f618d2f' AS TEXT))")
        c.commit()
        out = tempfile.mkdtemp()
        files, rows = B.Backup(r, Settings(data_dir=d), mock.Mock()).export(out)
        self.assertGreater(rows, 1)
        name = [f for f in os.listdir(out + "/backups/db") if f.startswith("events")][0]
        raw = gzip.open(os.path.join(out, "backups", "db", name)).read()
        path = os.path.join(out, "e.sqlite")
        open(path, "wb").write(raw)
        self.assertEqual(sqlite3.connect(path).execute("SELECT COUNT(*) FROM events").fetchone()[0], 2)

    def test_write_sqlite_accepts_a_surrogate(self):
        p = os.path.join(tempfile.mkdtemp(), "t.sqlite")
        B.write_sqlite(p, "t", ["a"], [{"a": "x\udc8d"}])
        self.assertEqual(sqlite3.connect(p).execute("SELECT a FROM t").fetchone()[0], "x�")


class VaultAndEvents(unittest.TestCase):
    def test_vault_keys_are_lowercase_hosts_without_userinfo(self):
        from aihub.cli import vault
        self.assertEqual(vault._norm("https://bob:ghp_TOKEN@Git.Example.com/x.git"), ("https", "git.example.com"))
        self.assertEqual(vault._norm("git.example.com:8443"), ("https", "git.example.com:8443"))
        with self.assertRaises(ValueError) as cm:
            vault._norm("ssh://bob:ghp_TOKEN@host/x")
        self.assertNotIn("ghp_TOKEN", str(cm.exception))

    def test_a_hostile_ts_is_replaced_not_a_500(self):
        from aihub.server.sync import to_row
        for ts in (10 ** 400, float("nan"), float("inf"), float("-inf"), "9e999", "x", None, [1]):
            r = to_row({"id": "t", "kind": "use", "package": "p", "ts": ts})
            self.assertTrue(r["ts"] == r["ts"] and abs(r["ts"]) < 1e11, ts)
        c = client()
        admin(c)
        body = '{"events":[{"id":"huge","kind":"use","package":"p","ts":1' + "0" * 400 + '}]}'
        self.assertEqual(c.post("/api/v1/telemetry", content=body, headers={"Content-Type": "application/json"}).status_code, 200)


class EventIdentity(unittest.TestCase):
    def test_ids_that_needed_cleaning_do_not_collapse_into_one_event(self):
        from aihub.server.sync import to_row
        mk = lambda i: {"id": i, "kind": "use", "package": "p", "ts": 1}
        ids = {to_row(mk(i))["ext_id"] for i in ("abc\udc80", "abc\udc81", "abc", "ab\x00c", "abc\x00")}
        self.assertEqual(len(ids), 5)
        self.assertEqual(to_row(mk("plain-id"))["ext_id"], "plain-id")                  # a normal id is untouched
        self.assertEqual(to_row(mk("abc\udc80"))["ext_id"], to_row(mk("abc\udc80"))["ext_id"])      # and stable, so a resend still dedupes

    def test_a_broken_schema_keeps_the_batch_instead_of_skipping_every_row(self):
        r = Repos(init_db(Settings(data_dir=tempfile.mkdtemp())))
        r.db.conn().execute("ALTER TABLE events RENAME TO events_gone")
        r.db.conn().commit()
        row = {"ts": time.time(), "kind": "use", "package": "p", "ext_id": "s1"}
        with self.assertRaises(sqlite3.OperationalError):
            r.events_insert_ext([row])
        self.assertEqual(r.event_rejects, 0)                                              # nothing was "skipped": the caller keeps and retries
        self.assertFalse(r.db.conn().raw.in_transaction)


class HttpIngestHostileText(unittest.TestCase):
    def test_a_poison_event_does_not_block_later_events(self):
        c = client()
        h = admin(c)
        c.put("/api/v1/admin/sync", json={"telemetry_rate_limit": "0"}, headers=h)
        body = ('{"events":[{"id":"e1","kind":"install","package":"demo","version":"1.0","ts":1,"cwd":"/h\\udc8d/a","host":"h\\u0000x"},'
                '{"id":"e2","kind":"publish","package":"demo","version":"2.0","ts":2,"detail":"\\ud800 lone"}]}')
        self.assertEqual(c.post("/api/v1/telemetry", content=body, headers={"Content-Type": "application/json"}).json()["accepted"], 2)
        ing = c.app.state.ingest
        self.assertEqual(ing.flush(), 2)
        self.assertEqual(c.post("/api/v1/telemetry", json={"events": [ev("later")]}).json()["accepted"], 1)
        self.assertEqual(ing.flush(), 1)
        self.assertEqual((ing.stats["failures"], len(ing.rows)), (0, 0))
        n = c.app.state.repos.db.conn().execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(n, 3)

    def test_the_journal_accepts_text_a_client_should_not_have_sent(self):
        c = client()
        h = admin(c)
        ing = c.app.state.ingest
        ing.add([{"id": "j1", "kind": "install", "package": "demo", "ts": 1, "detail": "x\udc8d"}])    # direct, bypassing to_row's JSON round trip
        self.assertEqual(ing.flush(), 1)

    def test_the_cli_never_spools_a_surrogate(self):
        from aihub.cli import telemetry
        with mock.patch.dict(os.environ, {"AIHUB_HOME": tempfile.mkdtemp()}):
            line = telemetry._prep({"kind": "install", "package": "p", "cwd": SURR})
            line.encode("utf-8")
            self.assertNotIn("udc8d", line)


class GitTrustAndLogin(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.repo = os.path.join(self.base, "w")
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        self.env = mock.patch.dict(os.environ, {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1", "HOME": tempfile.mkdtemp()})
        self.env.start()
        self.old = list(gitx.TRUSTED)
        gitx.TRUSTED[:] = []

    def tearDown(self):
        gitx.TRUSTED[:] = self.old
        self.env.stop()

    def test_dubious_ownership_is_explained_and_then_fixed_by_trust(self):
        with self.assertRaises(gitx.GitError) as cm:
            gitx.run(["status"], cwd=self.repo)
        self.assertIn("safe.directory", str(cm.exception))
        gitx.trust(self.base)
        self.assertEqual(gitx.run(["status", "--short"], cwd=self.repo), "")

    def test_a_checkout_with_foreign_owner_is_not_deleted_as_broken(self):
        sub = os.path.join(self.base, "cache")
        os.makedirs(sub)
        subprocess.run(["git", "init", "-q", sub], check=True)
        with self.assertRaises(gitx.GitError):
            gitx._sound(sub)                                              # intact, not ours: report, never rmtree + reclone
        self.assertTrue(os.path.isdir(os.path.join(sub, ".git")))
        gitx.trust(self.base)
        self.assertTrue(gitx._sound(sub))

    def test_unrelated_folders_are_not_trusted(self):
        gitx.trust(os.path.join(self.base, "elsewhere"))
        with self.assertRaises(gitx.GitError):
            gitx.run(["status"], cwd=self.repo)

    def test_git_stdin_with_a_surrogate_does_not_crash(self):
        gitx.trust(self.base)
        gitx.run(["hash-object", "--stdin"], cwd=self.repo, input="x\udc8d\n")

    def test_login_comes_from_the_env_file(self):
        d = tempfile.mkdtemp()
        f = os.path.join(d, ".env")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("AIHUB_GIT_TOKEN=\"tok-from-file\"\nAIHUB_GIT_USER=bob\nAIHUB_GIT_HOSTS=git.example.com\nAIHUB_GIT_ALLOW_HTTP=1\n")
        os.chmod(f, 0o644)
        clean = {k: v for k, v in os.environ.items() if not k.startswith("AIHUB_")}
        with mock.patch.dict(os.environ, clean, clear=True):
            s = Settings.load(env={}, env_file=f, data_dir=d)
            with self.assertLogs("aihub", "WARNING") as logs:
                gitauth.install(s)
            self.assertTrue(any("chmod 600" in m for m in logs.output))                    # world-readable file with a token
            self.assertEqual(gitauth.login_for(s, "git.example.com"), ("bob", "tok-from-file"))
            self.assertIsNone(gitauth.login_for(s, "evil.example.com"))                    # only the listed host
            self.assertEqual(os.environ.get("AIHUB_GIT_ALLOW_HTTP"), "1")                  # non-secret switch exported
            self.assertNotIn("AIHUB_GIT_TOKEN", os.environ)                                # the secret is not
            os.environ["AIHUB_GIT_ALLOW_HTTP"] = "0"
            gitauth.install(s)
            self.assertEqual(os.environ["AIHUB_GIT_ALLOW_HTTP"], "0")                      # the real environment wins
            os.chmod(f, 0o600)

    def test_login_reaches_only_the_matching_host(self):
        with mock.patch.object(gitx, "LOGIN", lambda host: ("bob", "s3cret") if host == "git.example.com" else None):
            e = gitx._login_env(gitx._env(None, None), None, ["fetch", "https://git.example.com/a/b.git"])
            self.assertEqual((e["AIHUB_GIT_USER"], e["AIHUB_GIT_SECRET"], e["AIHUB_GIT_HOST"], e["AIHUB_GIT_PROTO"]),
                             ("bob", "s3cret", "git.example.com", "https"))
            self.assertEqual(e["GIT_CONFIG_VALUE_2"], "initial")                                # http.followRedirects
            e = gitx._login_env(gitx._env(None, None), None, ["fetch", "https://other.example.com/a.git"])
            self.assertNotIn("AIHUB_GIT_SECRET", e)
            e = gitx._login_env(gitx._env(None, None), None, ["status"])                        # not a network command
            self.assertNotIn("AIHUB_GIT_SECRET", e)
            e = gitx._login_env(gitx._env(None, None), None, ["fetch", "http://git.example.com/a.git"])
            self.assertNotIn("AIHUB_GIT_SECRET", e, "a token is not offered over plain http by default")

    def test_host_keys_follow_what_git_sends(self):
        self.assertEqual(gitx._host_of("http://LOCALHOST:8080/a"), ("http", "localhost:8080"))     # git sends the case as typed; the helper compares lowercase
        self.assertEqual(gitx._host_of("https://[::1]:8443/a"), ("https", "[::1]:8443"))           # IPv6 keeps its brackets in git's protocol
        self.assertEqual(gitx._host_of("https://git.example.com:443/a"), ("https", "git.example.com:443"))   # a port is kept as written (fails closed)
        self.assertEqual(gitx._host_of("https://git.example.com:/a"), ("https", "git.example.com"))

    def test_a_garbage_git_config_count_does_not_raise(self):
        with mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "abc"}):
            gitx.trust(self.base)
            self.assertEqual(gitx.run(["status", "--short"], cwd=self.repo), "")
        self.assertEqual(gitx._count({"GIT_CONFIG_COUNT": "-4"}), 0)

    def test_the_login_pins_proxy_and_extra_header(self):
        with mock.patch.object(gitx, "LOGIN", lambda host: ("bob", "s3cret")), mock.patch.dict(os.environ, {"AIHUB_GIT_ALLOW_HTTP": ""}):
            e = gitx._login_env(gitx._env(None, None), None, ["fetch", "https://git.example.com/a.git"])
            cfg = {e["GIT_CONFIG_KEY_%d" % i]: e["GIT_CONFIG_VALUE_%d" % i] for i in range(int(e["GIT_CONFIG_COUNT"]))}
            self.assertEqual((cfg["http.proxy"], cfg["http.extraHeader"], cfg["http.followRedirects"]), ("", "", "initial"))

    def test_tls_warning_is_given_once_per_host_when_verification_is_off(self):
        gitx._warned_tls.clear()
        with mock.patch.object(gitx.tls, "insecure", return_value=True), self.assertLogs("aihub", "WARNING") as cm:
            gitx._warn_unverified("https", "git.example.com")
            gitx._warn_unverified("https", "git.example.com")
        self.assertEqual(len(cm.records), 1)
        self.assertIn("AIHUB_CA_BUNDLE", cm.output[0])
        gitx._warned_tls.clear()
        with mock.patch.object(gitx.tls, "insecure", return_value=False), self.assertNoLogs("aihub", "WARNING"):
            gitx._warn_unverified("https", "git.example.com")

    def test_url_host_parsing_refuses_lookalikes(self):
        for bad in ("http://127.0.0.1:8765#@git.example.com/x.git", "https://evil.example?@git.example.com/x.git", "https://evil.example\\@git.example.com/x",
                    "https://git.example.com\n/x", "https://git.example.com /x", "git@git.example.com:x/y.git", "ssh://git.example.com/x", "/local/path", "",
                    None, 5):
            self.assertIsNone(gitx._host_of(bad), repr(bad))
        self.assertEqual(gitx._host_of("https://User:pw@GIT.Example.com:8443/a.git"), ("https", "git.example.com:8443"))
        self.assertEqual(gitx._host_of("https://git.example.com/a.git"), ("https", "git.example.com"))

    def test_the_helper_prints_the_secret_only_for_its_own_host(self):
        for stdin, expect in (("protocol=https\nhost=git.example.com\n\n", True), ("protocol=https\nhost=evil.example\n\n", False),
                              ("protocol=http\nhost=git.example.com\n\n", False), ("protocol=https\nhost=git.example.com:9999\n\n", False), ("", False)):
            r = subprocess.run(["sh", "-c", gitx._HELPER[1:] + " get"], input=stdin, capture_output=True, text=True,
                               env=dict(os.environ, AIHUB_GIT_PROTO="https", AIHUB_GIT_HOST="git.example.com", AIHUB_GIT_USER="bob", AIHUB_GIT_SECRET="a\\nb\\\\c%s"))
            self.assertEqual("password=" in r.stdout, expect, (stdin, r.stdout))
            if expect:
                self.assertEqual(r.stdout, "username=bob\npassword=a\\nb\\\\c%s\n")             # backslashes and % intact (printf, not echo)

    def test_the_secret_is_never_in_argv(self):
        seen = []
        real = subprocess.run
        with mock.patch.object(gitx, "LOGIN", lambda host: ("bob", "s3cret")), \
                mock.patch("subprocess.run", side_effect=lambda a, **k: (seen.append(list(a)), real(["git", "--version"], capture_output=True, text=True))[1]):
            gitx.run(["ls-remote", "https://git.example.com/a.git"], check=False)
        self.assertTrue(seen)
        self.assertFalse(any("s3cret" in x for argv in seen for x in argv))

    def test_token_applies_to_the_exact_configured_hosts_only(self):
        s = Settings.load(env={"AIHUB_DATA_DIR": tempfile.mkdtemp(), "AIHUB_GIT_TOKEN": "tok", "AIHUB_GIT_USER": "me", "AIHUB_GIT_HOSTS": "Other.Example:8443"})
        urls = ["https://git.corp.example/x/y.git"]
        self.assertEqual(gitauth.login_for(s, "git.corp.example", urls), ("me", "tok"))
        self.assertEqual(gitauth.login_for(s, "other.example:8443", urls), ("me", "tok"))
        with mock.patch.object(credhelper, "lookup", return_value=None):
            for host in ("evil.example", "git.corp.example:9999", "other.example", "corp.example", "x.git.corp.example"):
                self.assertIsNone(gitauth.login_for(s, host, urls), host)

    def test_token_file_and_default_user(self):
        f = os.path.join(tempfile.mkdtemp(), "tok")
        open(f, "w").write("filetok\n")
        s = Settings.load(env={"AIHUB_DATA_DIR": tempfile.mkdtemp(), "AIHUB_GIT_TOKEN_FILE": f})
        self.assertEqual(gitauth.login_for(s, "h.example", ["https://h.example/r.git"]), ("oauth2", "filetok"))

    def test_vault_is_the_fallback(self):
        home = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"AIHUB_HOME": home}), mock.patch.object(credhelper, "backends", lambda: [credhelper.File]):
            credhelper.File.put("git.example.com", json.dumps({"username": "u", "password": "p"}))
            s = Settings.load(env={"AIHUB_DATA_DIR": tempfile.mkdtemp()})
            self.assertEqual(gitauth.login_for(s, "git.example.com", []), ("u", "p"))
            self.assertIsNone(credhelper.lookup("git.example.com:8443"))               # exact host only: another port is another service
            self.assertIsNone(gitauth.login_for(s, "nobody.example", []))


class LoginNeverLeaks(unittest.TestCase):
    """Real git against real local listeners that record every Authorization header: the stored login must reach the host it was issued
    for and NOTHING else - not through a look-alike URL, a redirect, or a repo-controlled LFS endpoint."""
    SECRET = "TOPSECRET-%d" % os.getpid()

    @classmethod
    def setUpClass(cls):
        import http.server
        cls.seen = []
        outer = cls

        def make(redirect=None):
            class H(http.server.BaseHTTPRequestHandler):
                def log_message(self, *a):
                    pass

                def do_any(self):
                    me = self.server.server_address[1]
                    outer.seen.append((me, self.headers.get("Authorization")))
                    if redirect and "/redir/" in self.path:
                        self.send_response(302)
                        self.send_header("Location", "http://localhost:%d%s" % (redirect, self.path))
                        self.end_headers()
                        return
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="x"')
                    self.end_headers()
                do_GET = do_POST = do_any
            return H
        cls.evil = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make())
        cls.good = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make(cls.evil.server_address[1]))
        cls.threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (cls.evil, cls.good)]
        [t.start() for t in cls.threads]
        cls.gp, cls.ep = cls.good.server_address[1], cls.evil.server_address[1]

    @classmethod
    def tearDownClass(cls):
        for s in (cls.good, cls.evil):
            s.shutdown()
            s.server_close()

    def setUp(self):
        self.seen.clear()
        self.env = mock.patch.dict(os.environ, {"AIHUB_GIT_ALLOW_HTTP": "1", "HOME": tempfile.mkdtemp(),     # the listeners are plain http
                                                 "GIT_ASKPASS": "echo", "GIT_TERMINAL_PROMPT": "0"})        # as in the server (gitx.BATCH): never wait for a person
        self.env.start()
        self.old = gitx.LOGIN
        gitx.LOGIN = lambda host: ("oauth2", self.SECRET) if host == "127.0.0.1:%d" % self.gp else None

    def tearDown(self):
        gitx.LOGIN = self.old
        self.env.stop()

    def got(self, port):
        import base64
        out = []
        for p, a in list(self.seen):
            if p == port and a:
                out.append(base64.b64decode(a.split()[1]).decode("utf-8", "replace"))
        return out

    def leaked(self):
        return [a for a in self.got(self.ep) if self.SECRET in a]

    def git(self, *args, cwd=None):
        try:
            gitx.run(list(args), cwd=cwd, check=False, timeout=30)
        except Exception:
            pass

    def test_control_the_legitimate_host_still_authenticates(self):
        self.git("ls-remote", "http://127.0.0.1:%d/good.git" % self.gp)
        self.assertIn("oauth2:" + self.SECRET, self.got(self.gp))

    def test_lookalike_urls_do_not_leak(self):
        gitx.LOGIN = lambda host: ("oauth2", self.SECRET) if host == "git.example.com" else None
        for url in ("http://127.0.0.1:%d#@git.example.com/x.git", "http://127.0.0.1:%d?@git.example.com/x.git",
                    "http://git.example.com@127.0.0.1:%d/y.git", "http://127.0.0.1:%d\\@git.example.com/y.git"):
            self.git("ls-remote", url % self.ep)
        self.assertEqual(self.leaked(), [])

    def test_redirect_to_another_host_does_not_leak(self):
        self.git("ls-remote", "http://127.0.0.1:%d/redir/x.git" % self.gp)
        self.assertEqual(self.leaked(), [])
        # git may follow the redirect (its default), but the helper sees the real destination host and withholds the secret. (What the attacker
        # may receive is git's own askpass answer, "Username for 'http://...'", never the stored login.)

    @unittest.skipUnless(shutil.which("git-lfs"), "git-lfs not installed")
    def test_repo_controlled_lfs_endpoint_does_not_leak(self):
        w = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", w], check=True)
        subprocess.run(["git", "-C", w, "remote", "add", "origin", "http://127.0.0.1:%d/good.git" % self.gp], check=True)
        with open(os.path.join(w, ".lfsconfig"), "w") as f:
            f.write("[lfs]\n\turl = http://127.0.0.1:%d/evil.git/info/lfs\n" % self.ep)
        with open(os.path.join(w, ".gitattributes"), "w") as f:
            f.write("*.bin filter=lfs diff=lfs merge=lfs -text\n")
        self.git("lfs", "pull", "origin", cwd=w)
        self.assertEqual(self.leaked(), [])


class PathPrefix(unittest.TestCase):
    def check(self, c, pre, header=None):
        h = admin_at(c, pre)
        self.assertEqual(c.post(pre + "/api/v1/admin/logo", content=PNG, headers=h).status_code, 200)
        meta = c.get(pre + "/api/v1/meta", headers=header or {}).json()
        page = c.get(pre + "/", headers=header or {}).text
        return meta, page

    def test_no_prefix_is_unchanged(self):
        meta, page = self.check(client(), "")
        self.assertTrue(meta["logo_url"].startswith("/api/v1/logo?v="))
        self.assertIn('<base href="/">', page)

    def test_prefix_kept_by_the_proxy(self):
        c = client(public_url="https://hub.example/aihub")
        meta, page = self.check(c, "/aihub")
        self.assertTrue(meta["logo_url"].startswith("/aihub/api/v1/logo?v="))
        self.assertIn('<base href="/aihub/">', page)
        self.assertIn('content="/aihub" id="base"', page)
        self.assertEqual(c.get(meta["logo_url"]).status_code, 200)             # the very URL the browser will request
        for p in ("/aihub/static/app.js", "/aihub/static/style.css", "/aihub/static/vendor/preact-htm-standalone.umd.js"):
            self.assertEqual(c.get(p).status_code, 200, p)

    def test_prefix_stripped_by_the_proxy(self):
        c = client(public_url="https://hub.example/aihub")
        meta, page = self.check(c, "")
        self.assertTrue(meta["logo_url"].startswith("/aihub/api/v1/logo?v="))
        self.assertEqual(c.get("/static/app.js").status_code, 200)

    def test_explicit_base_path_beats_public_url(self):
        with mock.patch.dict(os.environ, {"AIHUB_BASE_PATH": "/hub2"}):
            c = client(public_url="https://hub.example")
            meta, _ = self.check(c, "")
        self.assertTrue(meta["logo_url"].startswith("/hub2/api/v1/logo"))

    def test_forwarded_prefix_header_is_ignored_unless_the_operator_trusts_it(self):
        meta, page = self.check(client(), "", {"X-Forwarded-Prefix": "/team/hub"})
        self.assertIn('<base href="/">', page)                                       # any client can send it: off by default
        self.assertTrue(meta["logo_url"].startswith("/api/v1/logo"))
        with mock.patch.dict(os.environ, {"AIHUB_TRUST_PROXY_PREFIX": "1"}):
            meta, page = self.check(client(), "", {"X-Forwarded-Prefix": "/team/hub"})
            self.assertIn('<base href="/team/hub/">', page)
            meta, page = self.check(client(public_url="https://h/aihub"), "", {"X-Forwarded-Prefix": "/other"})
            self.assertIn('<base href="/aihub/">', page)                             # configuration beats the header

    def test_a_hostile_or_oversized_forwarded_prefix_is_ignored_even_when_trusted(self):
        with mock.patch.dict(os.environ, {"AIHUB_TRUST_PROXY_PREFIX": "1"}):
            for bad in ('//evil.com/', '/"><script>alert(1)</script>', "/a/../b", "/a b", "javascript:alert(1)", "/x?y", "/" + "a" * 70, "/a/b/c/d/e", "/a\\b"):
                _, page = self.check(client(), "", {"X-Forwarded-Prefix": bad})
                self.assertIn('<base href="/">', page, bad)
                self.assertNotIn("<script>alert", page)

    def test_clean_rejects_a_newline_and_over_long_values(self):
        for bad in ("/aihub\n", "/aihub\n/", "\n/aihub", "/" + "a" * 65, "/a/b/c/d/e"):
            self.assertEqual(basepath.clean(bad), "", repr(bad))
        self.assertEqual(basepath.clean("/a/b/c/d"), "/a/b/c/d")

    def test_cache_and_log_rules_still_apply_under_a_configured_prefix(self):
        c = client(public_url="https://h/aihub")
        for pre in ("", "/aihub"):
            r = c.post(pre + "/api/v1/auth/login", json={"username": "x", "password": "y"})
            self.assertEqual(r.headers.get("cache-control"), "no-store", pre)           # token-bearing responses are never cached
            self.assertEqual(c.get(pre + "/static/app.js").headers.get("cache-control"), "no-cache", pre)
            self.assertEqual(c.get(pre + "/api/v1/packages").headers.get("cache-control"), "no-cache", pre)

    def test_the_page_varies_on_the_prefix_header_and_csp_allows_base(self):
        r = client().get("/")
        self.assertIn("X-Forwarded-Prefix", r.headers.get("vary", ""))
        self.assertIn("base-uri 'self'", r.headers["content-security-policy"])         # 'none' would make browsers ignore <base href>

    def test_a_path_that_only_starts_with_the_prefix_letters_is_not_the_prefix(self):
        c = client(public_url="https://h/aihub")
        self.assertEqual(c.get("/aihubx/api/v1/healthz").status_code, 404)

    def test_redirect_keeps_the_prefix(self):
        c = client(public_url="https://h/aihub")
        r = c.get("/aihub", follow_redirects=False)
        self.assertTrue(r.headers["location"].endswith("/aihub/"))

    def test_cached_payloads_do_not_leak_between_prefixes(self):
        c = client()
        h = admin_at(c, "")
        a = c.get("/api/v1/auth/me", headers=h).json()
        self.assertIsNone(a["avatar_url"])
        self.assertEqual(basepath.clean("/a/b/"), "/a/b")
        for bad in ("//x", "aihub", "/a//b", "/a\\b", "/a?b", "/a#b", "/a/../b", "/a b", '/a"b', "javascript:1", "/"):
            self.assertEqual(basepath.clean(bad), "", bad)
        self.assertEqual(basepath.from_url("https://h/aihub/"), "/aihub")
        self.assertEqual(basepath.from_url("https://h"), "")

    def test_the_prefix_lookup_does_not_query_per_request_or_leak_a_connection(self):
        c = client()
        calls = []
        mw = None
        real = basepath.configured
        with mock.patch.object(basepath, "configured", side_effect=lambda *a, **k: (calls.append(1), real(*a, **k))[1]):
            c2 = client()
            for _ in range(40):
                self.assertEqual(c2.get("/api/v1/healthz").status_code, 200)
        self.assertLessEqual(len(calls), 2, "the configured prefix is cached, not read on every request")

    def test_an_admin_cli_endpoint_with_a_path_becomes_the_prefix(self):
        c = client()
        h = admin_at(c, "")
        with mock.patch.object(basepath.Middleware, "TTL", 0.0):
            c.app.state.repos.set_setting("cli_endpoint", "https://hub.example/team/aihub")
            self.assertTrue(c.get("/api/v1/meta").json()["public_url"].endswith("/team/aihub"))
            page = c.get("/team/aihub/").text
            self.assertIn('<base href="/team/aihub/">', page)
            c.app.state.repos.set_setting("cli_endpoint", "")
            self.assertIn('<base href="/">', c.get("/").text)

    def test_app_js_uses_the_prefix_for_every_call(self):
        js = open(os.path.join(os.path.dirname(__file__), "..", "aihub", "server", "static", "app.js"), encoding="utf-8").read()
        bare = [m.group(0) for m in re.finditer(r"""fetch\((?!U\()[^)]{0,40}/api/v1""", js)]
        self.assertEqual(bare, [])
        html = open(os.path.join(os.path.dirname(__file__), "..", "aihub", "server", "static", "index.html"), encoding="utf-8").read()
        self.assertNotRegex(html.replace('<base href="/">', ""), r'(?:src|href)="/')


def admin_at(c, pre):
    c.post(pre + "/api/v1/auth/register", json={"username": "root", "password": "secret1"})
    return {"Authorization": "Bearer " + c.post(pre + "/api/v1/auth/login", json={"username": "root", "password": "secret1"}).json()["token"]}


class Stress(unittest.TestCase):
    """Heavy traffic with hostile text and database trouble mixed in: every acknowledged good event is stored exactly once."""

    def test_hostile_and_normal_events_under_load(self):
        c = client()
        h = admin(c)
        c.put("/api/v1/admin/sync", json={"telemetry_rate_limit": "0"}, headers=h)
        ing, repos = c.app.state.ingest, c.app.state.repos
        hostile = ["/h\\udc8d/x", "a\\u0000b", "\\ud800", "x\\udfff", "plain", "✓ ünï", "😀"]
        clients, batches, per = 16, 15, 20
        acked, lock = set(), threading.Lock()

        def run(n):
            rnd = random.Random(n)
            for b in range(batches):
                evs = ['{"id":"c%d-b%d-%d","kind":"use","package":"stress","ts":%f,"client_id":"cl%d","cwd":"%s","detail":"%s","host":"%s"}'
                       % (n, b, i, time.time(), n, rnd.choice(hostile), rnd.choice(hostile), rnd.choice(hostile)) for i in range(per)]
                for _ in range(80):
                    r = c.post("/api/v1/telemetry", content='{"events":[' + ",".join(evs) + "]}", headers={"Content-Type": "application/json"})
                    if r.status_code == 200:
                        with lock:
                            acked.update("c%d-b%d-%d" % (n, b, i) for i in range(per))
                        break
                    self.assertIn(r.status_code, (429, 503))
                    time.sleep(0.02)
                else:
                    self.fail("batch never accepted")

        stop = threading.Event()
        errors = []

        def flusher():
            while not stop.is_set():
                try:
                    ing.flush()
                except Exception as e:                       # the flusher must not see data errors at all
                    errors.append(e)
                time.sleep(0.01)

        def reader():                                          # web traffic reading while ingest writes
            while not stop.is_set():
                self.assertEqual(c.get("/api/v1/dashboard", headers=h).status_code, 200)
                self.assertEqual(c.get("/api/v1/packages").status_code, 200)
                time.sleep(0.02)

        ft, rt = threading.Thread(target=flusher), threading.Thread(target=reader)
        ft.start()
        rt.start()
        ts = [threading.Thread(target=run, args=(n,)) for n in range(clients)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        stop.set()
        ft.join()
        rt.join()
        ing.flush()
        self.assertEqual(errors, [])
        got = {x["ext_id"] for x in repos.db.conn().execute("SELECT ext_id FROM events WHERE package='stress'").fetchall()}
        self.assertEqual(got, acked)
        self.assertEqual(len(acked), clients * batches * per)
        self.assertFalse(repos.db.conn().raw.in_transaction)
        self.assertEqual(ing.stats["failures"], 0)
        ing.close()

    def test_ingest_recovers_after_the_database_was_locked(self):
        c = client()
        admin(c)
        ing, repos = c.app.state.ingest, c.app.state.repos
        c.post("/api/v1/telemetry", json={"events": [ev("r1"), ev("r2")]})
        real = repos.events_insert_ext
        repos.events_insert_ext = mock.Mock(side_effect=sqlite3.OperationalError("database is locked"))
        with self.assertRaises(sqlite3.OperationalError):
            ing.flush()
        self.assertEqual((len(ing.rows), ing.stats["failures"]), (2, 1))   # kept, counted
        repos.events_insert_ext = real
        self.assertEqual(ing.flush(), 2)                                   # next window stores them
        self.assertEqual(ing.stats["failures"], 0)
        ing.close()


if __name__ == "__main__":
    unittest.main()
