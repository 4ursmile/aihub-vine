"""Self-update: fetch, conflict policy, verification, swap, probation, automatic and manual rollback, the watchdog, the admin API."""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from aihub.server import update_guard, updater as U
from aihub.server.config import Settings
from aihub.server.main import create_app
from tests.test_hub_first import admin

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ID = ["-c", "user.name=t", "-c", "user.email=t@t"]


def git(cwd, *a):
    return subprocess.run(["git"] + list(a), cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(cwd, msg):
    git(cwd, "add", "-A")
    git(cwd, *ID, "commit", "-q", "-m", msg)
    return git(cwd, "rev-parse", "HEAD")


def put(cwd, rel, text):
    p = os.path.join(cwd, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


def version(n):
    return 'VERSION = "%s"\n' % n


class Env(unittest.TestCase):
    """An upstream bare repo, a developer clone that pushes to it, and the hub's own clone with a running (test) app on top."""
    full = False                        # True: the repo holds the real aihub package (needed for the boot test)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.up = os.path.join(self.tmp, "up.git")
        self.dev = os.path.join(self.tmp, "dev")
        self.hub = os.path.join(self.tmp, "hub")
        git(self.tmp, "init", "-q", "--bare", "-b", "main", self.up)
        os.makedirs(self.dev)
        git(self.dev, "init", "-q", "-b", "main")
        if self.full:
            shutil.copytree(os.path.join(HERE, "aihub"), os.path.join(self.dev, "aihub"), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            put(self.dev, "aihub/__init__.py", "")
            put(self.dev, "aihub/server/updater.py", "# marker so the updater believes this version can watch itself\n")
            put(self.dev, "aihub/core/release.py", version("1.0.0"))
        put(self.dev, "app.txt", "one\ntwo\nthree\n")
        self.c1 = commit(self.dev, "v1")
        git(self.dev, "remote", "add", "origin", self.up)
        git(self.dev, "push", "-q", "origin", "main")
        git(self.tmp, "clone", "-q", self.up, self.hub)
        self.c = TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=os.path.join(self.tmp, "data"))))
        self.app = self.c.app
        self.u = self.app.state.updater
        self.u.root = self.hub
        self.u.smoke = self.full
        self.u.pace = 0.05
        self.u.restart_fn = mock.Mock()
        self.u.guard_fn = mock.Mock()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def push(self, msg="change"):
        sha = commit(self.dev, msg)
        git(self.dev, "push", "-q", "origin", "main")
        return sha

    def head(self):
        return git(self.hub, "rev-parse", "HEAD")

    def read(self, rel):
        with open(os.path.join(self.hub, rel)) as f:
            return f.read()

    def settings(self, **kv):
        for k, v in kv.items():
            self.app.state.repos.set_setting(k, v)


class Update(Env):
    def test_up_to_date_is_quiet(self):
        r = self.u.run(trigger="cron")
        self.assertEqual(r["state"], "up_to_date")
        self.assertEqual(self.u.runs(), [])                               # a scheduled check that finds nothing leaves no history
        self.assertEqual(self.u.run(trigger="manual")["state"], "up_to_date")
        self.assertEqual(len(self.u.runs()), 1)                           # but a manual one is shown
        self.assertIsNone(self.u.status["available"])

    def test_fast_forward_switch_and_confirm(self):
        put(self.dev, "aihub/core/release.py", version("1.1.0"))
        new = self.push("bump")
        r = self.u.run(trigger="manual")
        self.assertEqual(r["state"], "probation", r["log"])
        self.assertEqual(self.head(), new)
        self.assertEqual((r["version_from"], r["version_to"]), ("1.0.0", "1.1.0"))
        p = self.u.pending()
        self.assertEqual((p["state"], p["from"], p["to"]), ("probation", self.c1, new))
        self.u.guard_fn.assert_called_once()
        self.u.restart_fn.assert_called_once()
        # the restarted process takes over the record and, once healthy, confirms it
        self.u.probe_fn = lambda: True
        p.update(probation=0.3)
        U._write(self.u.pending_path, p)
        self.u.on_start()
        for _ in range(100):
            if not self.u.pending():
                break
            time.sleep(0.1)
        self.assertIsNone(self.u.pending())
        done = U.Run.load(self.u, r["id"]).d
        self.assertEqual(done["state"], "confirmed")
        self.assertIn("update confirmed", done["log"][-1]["m"])

    def test_check_only_reports(self):
        put(self.dev, "app.txt", "one\ntwo\nthree\nfour\n")
        self.push()
        r = self.u.run(apply=False)
        self.assertEqual(r["state"], "available")
        self.assertEqual(self.head(), self.c1)
        self.assertEqual(self.u.status["available"]["behind"], 1)
        self.u.restart_fn.assert_not_called()

    def test_conflict_remote_wins(self):
        put(self.hub, "app.txt", "one\nLOCAL\nthree\n")
        git(self.hub, "add", "-A")
        git(self.hub, *ID, "commit", "-q", "-m", "local")
        put(self.dev, "app.txt", "one\nREMOTE\nthree\n")
        self.push()
        self.settings(update_conflict="remote")
        r = self.u.run()
        self.assertEqual(r["state"], "probation", r["log"])
        self.assertEqual(self.read("app.txt"), "one\nREMOTE\nthree\n")
        self.assertTrue(any("conflict" in e["m"] and "remote" in e["m"] for e in r["log"]))

    def test_conflict_local_wins(self):
        put(self.hub, "app.txt", "one\nLOCAL\nthree\n")
        git(self.hub, "add", "-A")
        git(self.hub, *ID, "commit", "-q", "-m", "local")
        put(self.dev, "app.txt", "one\nREMOTE\nthree\n")
        put(self.dev, "other.txt", "new file\n")
        self.push()
        self.settings(update_conflict="local")
        r = self.u.run()
        self.assertEqual(r["state"], "probation", r["log"])
        self.assertEqual(self.read("app.txt"), "one\nLOCAL\nthree\n")
        self.assertEqual(self.read("other.txt"), "new file\n")             # the rest of the remote change still arrives

    def test_conflict_abort_leaves_everything_alone(self):
        put(self.hub, "app.txt", "one\nLOCAL\nthree\n")
        git(self.hub, "add", "-A")
        mine = commit(self.hub, "local")
        put(self.dev, "app.txt", "one\nREMOTE\nthree\n")
        tip = self.push()
        self.settings(update_conflict="abort")
        r = self.u.run()
        self.assertEqual(r["state"], "failed")
        self.assertIn("conflict", r["title"])
        self.assertEqual(self.head(), mine)
        self.assertEqual(git(self.hub, "worktree", "list").count("\n"), 0)   # candidate worktree removed
        self.assertEqual(self.app.state.repos.setting("update_skip"), tip)

    def test_uncommitted_local_changes_are_carried(self):
        put(self.hub, "app.txt", "one\ntwo\nthree\nLOCAL TAIL\n")           # not committed
        put(self.dev, "aihub/core/release.py", version("1.2.0"))
        self.push()
        r = self.u.run()
        self.assertEqual(r["state"], "probation", r["log"])
        self.assertIn("LOCAL TAIL", self.read("app.txt"))
        self.assertTrue(any(f.startswith("local-") for f in os.listdir(self.u.dir)))      # and a patch copy is kept

    def test_new_uncommitted_module_is_carried_into_the_candidate(self):
        """A file of an uncommitted change that is new (untracked) is code the candidate needs: `git diff`/`stash` leave it out."""
        put(self.hub, "aihub/newmod.py", "X = 1\n")
        put(self.hub, "notes.log", "not code\n")                           # untracked, outside the code folders: never swept into history
        put(self.dev, "aihub/core/release.py", version("1.2.0"))
        self.push()
        r = self.u.run()
        self.assertEqual(r["state"], "probation", r["log"])
        self.assertEqual(self.read("aihub/newmod.py"), "X = 1\n")
        tracked = git(self.hub, "ls-files").split()
        self.assertIn("aihub/newmod.py", tracked)
        self.assertNotIn("notes.log", tracked)
        self.assertTrue(os.path.isfile(os.path.join(self.hub, "notes.log")))
        self.assertTrue(any(f.startswith("local-") for f in os.listdir(self.u.dir)))

    def _clash(self, policy):
        put(self.hub, "aihub/clash.py", "LOCAL\n")                         # untracked here, and the remote adds the same path with other content
        put(self.dev, "aihub/clash.py", "REMOTE\n")
        put(self.dev, "aihub/core/release.py", version("1.2.0"))
        self.push()
        self.settings(update_conflict=policy)
        return self.u.run()

    def test_new_module_that_the_remote_also_added_remote_policy(self):
        r = self._clash("remote")
        self.assertEqual(r["state"], "probation", r["log"])
        self.assertEqual(self.read("aihub/clash.py"), "REMOTE\n")

    def test_new_module_that_the_remote_also_added_local_policy(self):
        r = self._clash("local")
        self.assertEqual(r["state"], "probation", r["log"])
        self.assertEqual(self.read("aihub/clash.py"), "LOCAL\n")

    def test_a_failed_update_leaves_untracked_files_and_the_index_alone(self):
        put(self.hub, "aihub/newmod.py", "X = 1\n")
        put(self.dev, "aihub/core/release.py", "VERSION = (\n")
        self.push("broken")
        self.assertEqual(self.u.run()["state"], "failed")
        self.assertEqual(git(self.hub, "status", "--porcelain", "--", "aihub/newmod.py"), "?? aihub/newmod.py")    # still untracked, not staged
        self.assertEqual(self.head(), self.c1)

    def test_broken_candidate_is_rejected_and_skipped(self):
        put(self.dev, "aihub/core/release.py", "VERSION = (\n")
        tip = self.push("broken")
        r = self.u.run()
        self.assertEqual(r["state"], "failed")
        self.assertIn("syntax", r["title"])
        self.assertEqual(self.head(), self.c1)
        self.u.restart_fn.assert_not_called()
        self.assertIsNone(self.u.pending())
        again = self.u.run()                                                # the same commit is not retried by the schedule
        self.assertEqual(again["state"], "skipped")
        fixed = self.u.run(force=True)                                      # "Update anyway" tries it again
        self.assertEqual(fixed["state"], "failed")
        put(self.dev, "aihub/core/release.py", version("1.3.0"))
        self.push("fix")
        self.assertEqual(self.u.run()["state"], "probation")                # the branch moved: tried again, now fine

    def test_unhealthy_new_version_rolls_itself_back(self):
        put(self.dev, "aihub/core/release.py", version("2.0.0"))
        new = self.push()
        r = self.u.run()
        self.assertEqual(self.head(), new)
        self.u.restart_fn.reset_mock()
        self.u.probe_fn = lambda: False
        self.u.on_start()
        for _ in range(100):
            if self.u.restart_fn.called:
                break
            time.sleep(0.1)
        self.assertEqual(self.head(), self.c1)                              # old commit is back
        self.u.restart_fn.assert_called_once()
        self.assertIsNone(self.u.pending())
        done = U.Run.load(self.u, r["id"]).d
        self.assertEqual(done["state"], "rolled_back")
        self.assertIn("health checks", done["title"])
        self.assertEqual(self.app.state.repos.setting("update_skip"), new)  # and it is not tried again

    def test_crash_loop_rolls_back(self):
        put(self.dev, "aihub/core/release.py", version("2.1.0"))
        self.push()
        self.u.run()
        p = self.u.pending()
        p["boots"] = 2                                                      # this would be the third start
        U._write(self.u.pending_path, p)
        self.u.restart_fn.reset_mock()
        self.u.on_start()
        for _ in range(100):
            if self.u.restart_fn.called:
                break
            time.sleep(0.1)
        self.assertEqual(self.head(), self.c1)
        self.assertEqual(U.Run.load(self.u, self.u.runs()[0]["id"]).d["state"], "rolled_back")

    def test_manual_rollback_and_guard_record(self):
        put(self.dev, "aihub/core/release.py", version("1.1.0"))
        self.push()
        self.u.run()
        os.remove(self.u.pending_path)                                      # pretend it was confirmed
        self.u.restart_fn.reset_mock()
        bad = self.u.run(action="rollback", target=self.c1[:8])
        self.assertEqual(bad["state"], "probation", bad["log"])
        self.assertEqual(self.head(), self.c1)
        self.assertEqual(self.u.pending()["from"], git(self.dev, "rev-parse", "HEAD"))
        self.u.restart_fn.assert_called_once()

    def test_rollback_to_unknown_commit(self):
        r = self.u.run(action="rollback", target="deadbeef")
        self.assertEqual(r["state"], "failed")
        self.assertIn("not found", r["title"])

    def test_rollback_before_the_feature_existed_skips_probation(self):
        git(self.dev, "rm", "-q", "aihub/server/updater.py")
        old = commit(self.dev, "no updater here")
        git(self.dev, "push", "-q", "origin", "main")
        put(self.dev, "aihub/server/updater.py", "# back\n")
        self.push()
        git(self.hub, "pull", "-q", "origin", "main")
        r = self.u.run(action="rollback", target=old)
        self.assertEqual(r["state"], "applied", r["log"])
        self.assertEqual(self.head(), old)
        self.assertIsNone(self.u.pending())

    def test_second_run_while_one_is_active_is_refused(self):
        self.u.lock.acquire()
        try:
            with self.assertRaises(U.Busy):
                self.u.run()
        finally:
            self.u.lock.release()

    def test_versions_lists_current(self):
        v = self.u.versions()
        self.assertTrue(v[0]["current"])
        self.assertEqual(v[0]["sha"], self.c1)

    def test_not_a_git_checkout(self):
        self.u.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.u.root, "aihub"))
        r = self.u.run()
        self.assertEqual(r["state"], "failed")
        self.assertIn("git checkout", r["title"])

    def test_setting_validation(self):
        for k, bad in (("update_enabled", "2"), ("update_schedule", "nope"), ("update_url", "-oProxy=x"), ("update_branch", "a..b"),
                       ("update_conflict", "maybe"), ("update_probation", "1"), ("update_restart", "reboot")):
            with self.assertRaises(ValueError, msg=k):
                U.check_setting(k, bad)
        self.assertEqual(U.check_setting("update_conflict", "local"), "local")
        self.assertEqual(U.check_setting("update_url", ""), "")


class Stages(Env):
    def keys(self, run, state=None):
        return [x["key"] for x in run["stages"] if state is None or x["state"] == state]

    def test_successful_update_walks_every_stage(self):
        put(self.dev, "aihub/core/release.py", version("1.1.0"))
        self.push()
        r = self.u.run()
        by = {x["key"]: x for x in r["stages"]}
        self.assertEqual([x["key"] for x in r["stages"]], [k for k, _ in U.STAGES])
        for k in ("fetch", "merge", "syntax", "switch"):
            self.assertEqual(by[k]["state"], "done", k)
            self.assertTrue(by[k]["started"] and by[k]["finished"])
        self.assertEqual(by["merge"]["detail"], "fast-forward")
        self.assertEqual((by["tests"]["state"], by["boot"]["state"], by["backup"]["state"]), ("skipped",) * 3)
        self.assertEqual(by["watch"]["state"], "running")                      # the new process finishes it

    def test_watch_stage_completes_on_confirm(self):
        put(self.dev, "aihub/core/release.py", version("1.1.0"))
        self.push()
        r = self.u.run()
        p = self.u.pending()
        p.update(probation=0.3)
        U._write(self.u.pending_path, p)
        self.u.probe_fn = lambda: True
        self.u.on_start()
        for _ in range(100):
            if not self.u.pending():
                break
            time.sleep(0.1)
        done = U.Run.load(self.u, r["id"]).d
        self.assertEqual({x["key"]: x["state"] for x in done["stages"]}["watch"], "done")

    def test_failure_marks_the_failing_stage_and_skips_the_rest(self):
        put(self.dev, "aihub/core/release.py", "VERSION = (\n")
        self.push()
        r = self.u.run()
        st = {x["key"]: x["state"] for x in r["stages"]}
        self.assertEqual((st["fetch"], st["merge"], st["syntax"]), ("done", "done", "failed"))
        self.assertEqual((st["boot"], st["switch"], st["watch"]), ("pending",) * 3)

    def test_up_to_date_and_rollback_stage_lists(self):
        r = self.u.run(trigger="cron")
        self.assertEqual({x["state"] for x in r["stages"] if x["key"] != "fetch"}, {"skipped"})
        put(self.dev, "aihub/core/release.py", version("1.1.0"))
        self.push()
        self.u.run()
        os.remove(self.u.pending_path)
        rb = self.u.run(action="rollback", target=self.c1)
        self.assertEqual([x["key"] for x in rb["stages"]], [k for k, _ in U.ROLLBACK_STAGES])

    def test_rolled_back_run_shows_the_watch_stage_failed(self):
        put(self.dev, "aihub/core/release.py", version("2.0.0"))
        self.push()
        r = self.u.run()
        self.u.probe_fn = lambda: False
        self.u.on_start()
        for _ in range(100):
            if self.u.restart_fn.call_count > 1:
                break
            time.sleep(0.1)
        done = U.Run.load(self.u, r["id"]).d
        self.assertEqual({x["key"]: x["state"] for x in done["stages"]}["watch"], "failed")

    def test_old_run_files_without_stages_still_load_and_change_nothing(self):
        old = {"id": "20200101-000000-aaaa", "ts": 1.0, "kind": "update", "trigger": "manual", "user": "", "state": "confirmed", "title": "Updated",
               "from": "a", "to": "b", "tip": "b", "version_from": "1", "version_to": "2", "finished": 2.0, "log": []}
        U._write(os.path.join(self.u.dir, "runs", old["id"] + ".json"), old)
        run = U.Run.load(self.u, old["id"])
        run.begin("fetch")
        run.end("fetch")
        run.progress("boot", 1, 2)
        run.finish("confirmed", "Updated")
        self.assertNotIn("stages", run.d)
        self.assertEqual(self.u.runs()[0]["id"], old["id"])


class Manual(Env):
    """Policy "ask me": clean merges go through, a real conflict waits for a person and nothing changes meanwhile."""
    def make_conflict(self):
        self.settings(update_conflict="manual")
        put(self.hub, "app.txt", "one\nLOCAL\nthree\n")
        self.mine = commit(self.hub, "local")
        put(self.dev, "app.txt", "one\nREMOTE\nthree\n")
        put(self.dev, "aihub/core/release.py", version("1.5.0"))
        self.tip = self.push()

    def test_clean_merge_needs_nobody(self):
        self.settings(update_conflict="manual")
        put(self.hub, "mine.txt", "mine\n")
        commit(self.hub, "local file")
        put(self.dev, "theirs.txt", "theirs\n")
        self.push()
        r = self.u.run()
        self.assertEqual(r["state"], "probation", r["log"])
        self.assertEqual((self.read("mine.txt"), self.read("theirs.txt")), ("mine\n", "theirs\n"))

    def test_conflict_waits_and_changes_nothing(self):
        self.make_conflict()
        r = self.u.run()
        self.assertEqual(r["state"], "conflict")
        self.assertIn("Manual update needed", r["title"])
        self.assertEqual(self.head(), self.mine)
        self.assertEqual(self.read("app.txt"), "one\nLOCAL\nthree\n")
        self.u.restart_fn.assert_not_called()
        self.assertIsNone(self.u.pending())
        self.assertEqual(git(self.hub, "worktree", "list").count("\n"), 0)
        self.assertEqual(self.app.state.repos.setting("update_skip", ""), "")        # waiting is not failing
        c = self.u.conflict()
        f = c["files"][0]
        self.assertEqual((f["path"], f["kind"], f["conflicts"]), ("app.txt", "text", 1))
        blocks = [x for x in f["segments"] if x["t"] == "conflict"]
        self.assertEqual((blocks[0]["ours"], blocks[0]["theirs"]), ("LOCAL\n", "REMOTE\n"))
        self.assertEqual({x["key"]: x["state"] for x in r["stages"]}["merge"], "blocked")
        view = self.u.view()["conflict"]
        self.assertEqual(view["files"][0]["path"], "app.txt")
        self.assertNotIn("segments", view["files"][0])                                 # the list is small; the content has its own route

    def test_scheduled_run_does_not_repeat_the_wait(self):
        self.make_conflict()
        self.u.run(trigger="cron")
        n = len(self.u.runs())
        again = self.u.run(trigger="cron")
        self.assertEqual(again["state"], "conflict")
        self.assertEqual(len(self.u.runs()), n)                                       # no new history row every night

    def resolve(self, files, **kw):
        return self.u.run(trigger="manual", user="root", force=True, resolutions=files, **kw)

    def test_resolving_each_way(self):
        for choice, want in (("ours", "LOCAL"), ("theirs", "REMOTE"), ("both", "LOCAL\nREMOTE")):
            with self.subTest(choice):
                self.tearDown() if hasattr(self, "_t") else None
                self.setUp()
                self._t = True
                self.make_conflict()
                self.u.run()
                r = self.resolve({"app.txt": {"mode": "hunks", "choices": [choice]}})
                self.assertEqual(r["state"], "probation", r["log"])
                self.assertEqual(self.read("app.txt"), "one\n%s\nthree\n" % want)
                self.assertEqual(self.read("aihub/core/release.py"), version("1.5.0"))   # the rest of the update arrived
                self.assertIsNone(self.u.conflict())
                self.assertEqual(r["stages"][1]["detail"], "1 file(s) resolved by root")

    def test_whole_file_and_custom_text(self):
        self.make_conflict()
        self.u.run()
        r = self.resolve({"app.txt": {"mode": "custom", "text": "hand written\n"}})
        self.assertEqual(r["state"], "probation", r["log"])
        self.assertEqual(self.read("app.txt"), "hand written\n")
        os.remove(self.u.pending_path)
        git(self.hub, "reset", "-q", "--hard", self.mine)
        put(self.dev, "app.txt", "one\nREMOTE 2\nthree\n")
        self.push()
        self.u.run()
        r = self.resolve({"app.txt": {"mode": "theirs"}})
        self.assertEqual(self.read("app.txt"), "one\nREMOTE 2\nthree\n")

    def test_stale_answers_are_refused_and_nothing_changes(self):
        self.make_conflict()
        self.u.run()
        wrong = self.resolve({"other.txt": {"mode": "ours"}})
        self.assertEqual(wrong["state"], "failed")
        self.assertEqual(self.head(), self.mine)
        self.assertEqual(self.app.state.repos.setting("update_skip", ""), "")        # a stale answer is not a bad commit
        put(self.dev, "app.txt", "one\nREMOTE AGAIN\nthree\n")                     # the branch moved after the conflict was shown
        self.push()
        moved = self.resolve({"app.txt": {"mode": "ours"}})
        self.assertEqual(moved["state"], "failed")
        self.assertIn("changed since", moved["title"])
        self.assertEqual(self.head(), self.mine)
        bad = self.resolve({"app.txt": {"mode": "hunks", "choices": ["nope"]}})
        self.assertEqual(bad["state"], "failed")

    def test_conflict_is_dropped_when_the_checkout_moves(self):
        self.make_conflict()
        self.u.run()
        self.assertIsNotNone(self.u.conflict())
        git(self.hub, "reset", "-q", "--hard", self.c1)
        self.assertIsNone(self.u.conflict())

    def test_uncommitted_edits_conflict_too_and_are_kept_safe(self):
        self.settings(update_conflict="manual")
        put(self.hub, "app.txt", "one\nUNCOMMITTED\nthree\n")
        put(self.dev, "app.txt", "one\nREMOTE\nthree\n")
        self.push()
        r = self.u.run()
        self.assertEqual(r["state"], "conflict", r["log"])
        self.assertEqual(self.read("app.txt"), "one\nUNCOMMITTED\nthree\n")
        self.assertTrue(any(f.startswith("local-") for f in os.listdir(self.u.dir)))

    def test_deleted_vs_changed_and_binary(self):
        self.settings(update_conflict="manual")
        put(self.dev, "gone.txt", "x\n")
        with open(os.path.join(self.dev, "blob.bin"), "wb") as f:
            f.write(b"\0\1\2base")
        self.push()
        git(self.hub, "pull", "-q", "origin", "main")
        git(self.hub, "rm", "-q", "gone.txt")
        with open(os.path.join(self.hub, "blob.bin"), "wb") as f:
            f.write(b"\0\1\2local")
        commit(self.hub, "local delete + binary")
        put(self.dev, "gone.txt", "changed upstream\n")
        with open(os.path.join(self.dev, "blob.bin"), "wb") as f:
            f.write(b"\0\1\2remote")
        self.push()
        self.assertEqual(self.u.run()["state"], "conflict")
        kinds = {f["path"]: f["kind"] for f in self.u.conflict()["files"]}
        self.assertEqual(kinds, {"gone.txt": "delete", "blob.bin": "binary"})
        r = self.resolve({"gone.txt": {"mode": "ours"}, "blob.bin": {"mode": "theirs"}})
        self.assertEqual(r["state"], "probation", r["log"])
        self.assertFalse(os.path.exists(os.path.join(self.hub, "gone.txt")))
        self.assertEqual(open(os.path.join(self.hub, "blob.bin"), "rb").read(), b"\0\1\2remote")

    def test_crlf_and_markers_in_content_survive(self):
        self.settings(update_conflict="manual")
        put(self.dev, "w.txt", "a\r\nb\r\nc\r\n")
        self.push()
        git(self.hub, "pull", "-q", "origin", "main")
        with open(os.path.join(self.hub, "w.txt"), "wb") as f:
            f.write(b"a\r\nLOCAL\r\nc\r\n")
        commit(self.hub, "l")
        with open(os.path.join(self.dev, "w.txt"), "wb") as f:
            f.write(b"a\r\nREMOTE\r\nc\r\n")
        self.push()
        self.u.run()
        b = [x for x in self.u.conflict()["files"][0]["segments"] if x["t"] == "conflict"][0]
        self.assertEqual((b["ours"], b["theirs"]), ("LOCAL\r\n", "REMOTE\r\n"))
        self.resolve({"w.txt": {"mode": "hunks", "choices": ["theirs"]}})
        self.assertEqual(open(os.path.join(self.hub, "w.txt"), "rb").read(), b"a\r\nREMOTE\r\nc\r\n")

    def test_parse_and_assemble_roundtrip(self):
        txt = "x\n%s ours\nA\n%s base\nB\n%s\nC\n%s theirs\ny\n" % ("<" * 25, "|" * 25, "=" * 25, ">" * 25)
        segs = U.parse_markers(txt)
        self.assertEqual([x["t"] for x in segs], ["text", "conflict", "text"])
        self.assertEqual(U.assemble(segs, ["both"]), "x\nA\nC\ny\n")
        self.assertEqual(U.assemble(segs, [{"text": "Z\n"}]), "x\nZ\ny\n")
        with self.assertRaises(ValueError):
            U.assemble(segs, [])
        with self.assertRaises(ValueError):
            U.parse_markers("%s ours\nA\n" % ("<" * 25))

    def test_dismiss(self):
        self.make_conflict()
        self.u.run()
        self.u.dismiss_conflict()
        self.assertIsNone(self.u.conflict())


class Compat(Env):
    """Settings, files and behaviour from before the visual resolver keep working."""
    def test_old_policies_unchanged_and_new_one_validates(self):
        for v in ("remote", "local", "abort", "manual"):
            self.assertEqual(U.check_setting("update_conflict", v), v)
        self.assertEqual(U.DEFAULTS["update_conflict"], "remote")
        self.assertEqual(U.CONFLICTS[:3], ("remote", "local", "abort"))

    def test_abort_still_fails_and_marks_the_commit(self):
        put(self.hub, "app.txt", "one\nLOCAL\nthree\n")
        commit(self.hub, "l")
        put(self.dev, "app.txt", "one\nREMOTE\nthree\n")
        tip = self.push()
        self.settings(update_conflict="abort")
        self.assertEqual(self.u.run()["state"], "failed")
        self.assertEqual(self.app.state.repos.setting("update_skip"), tip)

    def test_old_pending_json_without_new_fields_still_works(self):
        put(self.dev, "aihub/core/release.py", version("1.1.0"))
        self.push()
        self.u.run()
        p = self.u.pending()
        for k in ("kind",):
            p.pop(k, None)
        p["probation"] = 0.2
        U._write(self.u.pending_path, p)
        self.u.probe_fn = lambda: True
        self.u.on_start()
        for _ in range(100):
            if not self.u.pending():
                break
            time.sleep(0.1)
        self.assertIsNone(self.u.pending())

    def test_view_keeps_every_old_field(self):
        v = self.u.view()
        for k in ("git", "why", "status", "pending", "source"):
            self.assertIn(k, v)
        for k in ("running", "last_check", "last_result", "last_error", "available", "next_in"):
            self.assertIn(k, v["status"])
        self.assertIsNone(v["conflict"])

    def test_guard_script_is_unchanged_in_interface(self):
        self.assertEqual(update_guard.main.__code__.co_argcount, 1)


class Guard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "r")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        put(self.repo, "f", "1")
        self.a = commit(self.repo, "a")
        put(self.repo, "f", "2")
        self.b = commit(self.repo, "b")
        self.path = os.path.join(self.tmp, "pending.json")

    def state(self):
        with open(self.path) as f:
            return json.load(f)["state"]

    def rec(self, **kw):
        d = {"state": "probation", "repo": self.repo, "from": self.a, "to": self.b, "pid": 0, "deadline": time.time() - 1, "spawn": False}
        d.update(kw)
        with open(self.path, "w") as f:
            json.dump(d, f)

    def test_resets_when_the_deadline_passes_unconfirmed(self):
        self.rec()
        self.assertEqual(update_guard.main(self.path), 0)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.a)
        self.assertEqual(self.state(), "rolled_back")

    def test_stands_down_when_confirmed_or_cancelled(self):
        self.rec(state="confirmed")
        update_guard.main(self.path)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.b)
        os.remove(self.path)
        self.assertEqual(update_guard.main(self.path), 0)


    def test_guard_failure_is_reported(self):
        self.rec(repo=os.path.join(self.tmp, "missing"))
        self.assertEqual(update_guard.main(self.path), 1)
        self.assertEqual(self.state(), "rollback_failed")


class GuardThenServer(Env):
    def test_server_closes_the_books_after_a_guard_rollback(self):
        put(self.dev, "aihub/core/release.py", version("3.0.0"))
        new = self.push()
        r = self.u.run()
        self.assertEqual(self.head(), new)
        p = self.u.pending()
        p["deadline"] = time.time() - 1                                     # the new version hung: nobody confirmed
        U._write(self.u.pending_path, p)
        update_guard.main(self.u.pending_path)
        self.assertEqual(self.head(), self.c1)
        self.u.on_start()                                                   # the old version starts again and reads the record
        self.assertIsNone(self.u.pending())
        done = U.Run.load(self.u, r["id"]).d
        self.assertEqual(done["state"], "rolled_back")
        self.assertEqual(self.app.state.repos.setting("update_skip"), new)
        self.assertIn("update.rolled_back", [a["action"] for a in self.app.state.repos.audit_list()])


class BootTest(Env):
    full = True

    def test_good_candidate_boots_and_bad_runtime_is_caught(self):
        put(self.dev, "aihub/core/release.py", open(os.path.join(HERE, "aihub/core/release.py")).read().replace("VERSION = ", "VERSION = ", 1))
        put(self.dev, "docs/note.md", "x\n")
        self.push("harmless")
        r = self.u.run()
        self.assertEqual(r["state"], "probation", r["log"])
        self.assertTrue(any("boot test passed" in e["m"] for e in r["log"]))
        os.remove(self.u.pending_path)
        # compiles, but raises on import: only a real start finds this
        main = os.path.join(self.dev, "aihub/server/main.py")
        with open(main) as f:
            src = f.read()
        put(self.dev, "aihub/server/main.py", src + "\nraise RuntimeError('boom at import')\n")
        tip = self.push("breaks at runtime")
        r = self.u.run()
        self.assertEqual(r["state"], "failed", r["log"])
        self.assertIn("boom at import", json.dumps(r["log"]))
        self.assertNotEqual(self.head(), tip)


class Api(Env):
    def test_admin_only_and_flow(self):
        self.assertEqual(self.c.get("/api/v1/admin/update").status_code, 401)
        h = admin(self.c)
        v = self.c.get("/api/v1/admin/update", headers=h).json()
        self.assertTrue(v["git"])
        self.assertEqual(v["config"]["update_conflict"], "remote")
        self.assertEqual(self.c.put("/api/v1/admin/update", json={"update_conflict": "x"}, headers=h).status_code, 400)
        ok = self.c.put("/api/v1/admin/update", json={"update_enabled": "1", "update_conflict": "local"}, headers=h).json()
        self.assertEqual(ok["config"]["update_enabled"], "1")
        self.assertEqual(ok["config"]["update_conflict"], "local")
        put(self.dev, "app.txt", "one\ntwo\nthree\nfour\n")
        self.push()
        res = self.c.post("/api/v1/admin/update/run?wait=1", headers=h).json()["run"]
        self.assertEqual(res["state"], "probation")
        runs = self.c.get("/api/v1/admin/update/runs", headers=h).json()["runs"]
        self.assertEqual(runs[0]["id"], res["id"])
        self.assertNotIn("log", runs[0])
        one = self.c.get("/api/v1/admin/update/runs/" + res["id"], headers=h).json()
        self.assertTrue(one["log"])
        self.assertEqual(self.c.get("/api/v1/admin/update/runs/nope", headers=h).status_code, 404)
        self.assertEqual(self.c.post("/api/v1/admin/update/rollback", json={}, headers=h).status_code, 400)
        self.assertEqual(self.c.get("/api/v1/admin/update/versions", headers=h).status_code, 200)
        acts = [a["action"] for a in self.app.state.repos.audit_list()]
        self.assertIn("update.run", acts)

    def test_conflict_routes(self):
        h = admin(self.c)
        self.assertEqual(self.c.get("/api/v1/admin/update/conflict", headers=h).status_code, 404)
        self.assertEqual(self.c.post("/api/v1/admin/update/resolve", json={}, headers=h).status_code, 400)
        self.assertEqual(self.c.post("/api/v1/admin/update/resolve", json={"files": {"a": {"mode": "ours"}}}, headers=h).status_code, 409)
        self.c.put("/api/v1/admin/update", json={"update_conflict": "manual"}, headers=h)
        put(self.hub, "app.txt", "one\nLOCAL\nthree\n")
        commit(self.hub, "l")
        put(self.dev, "app.txt", "one\nREMOTE\nthree\n")
        self.push()
        run = self.c.post("/api/v1/admin/update/run?wait=1", headers=h).json()["run"]
        self.assertEqual(run["state"], "conflict")
        self.assertEqual(self.c.get("/api/v1/admin/update", headers=h).json()["conflict"]["files"][0]["path"], "app.txt")
        c = self.c.get("/api/v1/admin/update/conflict", headers=h).json()
        self.assertEqual(c["files"][0]["segments"][1]["t"], "conflict")
        self.assertEqual(self.c.get("/api/v1/admin/update/conflict").status_code, 401)
        res = self.c.post("/api/v1/admin/update/resolve?wait=1", json={"files": {"app.txt": {"mode": "hunks", "choices": ["theirs"]}}}, headers=h).json()["run"]
        self.assertEqual(res["state"], "probation", res["log"])
        self.assertEqual(self.read("app.txt"), "one\nREMOTE\nthree\n")
        self.assertIn("update.resolve", [a["action"] for a in self.app.state.repos.audit_list()])
        self.assertEqual(self.c.delete("/api/v1/admin/update/conflict", headers=h).json()["conflict"], None)

    def test_busy_is_409(self):
        h = admin(self.c)
        self.u.lock.acquire()
        try:
            self.assertEqual(self.c.post("/api/v1/admin/update/run", headers=h).status_code, 409)
        finally:
            self.u.lock.release()


if __name__ == "__main__":
    unittest.main()
