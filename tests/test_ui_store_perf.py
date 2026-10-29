"""aihub-ui store/chat: slim sessions.json with side files, tail history, cached counts, stop before claude, env filter."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "aihub", "builtin_packages", "aihub-ui", "ui"))
from aihubui import chat, store  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.environ.get("AIHUB_HOME")
        os.environ["AIHUB_HOME"] = self.tmp
        store._icache.update(stamp=None, data=None)
        store._counts.clear()
        store._scache.clear()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if self.old is None:
            os.environ.pop("AIHUB_HOME", None)
        else:
            os.environ["AIHUB_HOME"] = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)


class SlimStore(Base):
    def test_slim_file_list_and_full_get(self):
        s = store.create_session("t", system="SYS" * 1000, extra="EX" * 500)
        store.update_session(s["id"], seed="SEED")
        raw = open(os.path.join(store.ui_dir(), "sessions.json")).read()
        for k in ("SYS", "EX", "SEED"):
            self.assertNotIn(k, raw)
        row = store.sessions()["sessions"][0]
        self.assertFalse(any(k in row for k in store.SIDE))
        g = store.get_session(s["id"])
        self.assertEqual((g["system"], g["extra"], g["seed"]), ("SYS" * 1000, "EX" * 500, "SEED"))
        self.assertEqual(store.update_session(s["id"], title="x")["system"], "SYS" * 1000)
        self.assertEqual(store.sessions(full=True)["sessions"][0]["seed"], "SEED")
        store.update_session(s["id"], seed="")
        self.assertEqual(store.get_session(s["id"])["seed"], "")
        store.delete_session(s["id"])
        self.assertFalse(os.path.exists(os.path.join(store.ui_dir("system"), s["id"] + ".md")))

    def test_migrates_fat_file(self):
        fat = {"sessions": {"aaaaaaaa": {"id": "aaaaaaaa", "title": "old", "system": "S", "extra": "E", "seed": "D", "updated": 1, "usage": {}}}, "folders": []}
        json.dump(fat, open(os.path.join(store.ui_dir(), "sessions.json"), "w"))
        self.assertNotIn("system", store.sessions()["sessions"][0])
        g = store.get_session("aaaaaaaa")
        self.assertEqual((g["system"], g["extra"], g["seed"]), ("S", "E", "D"))
        self.assertNotIn('"system"', open(os.path.join(store.ui_dir(), "sessions.json")).read())

    def test_no_write_when_unchanged(self):
        s = store.create_session("t")
        with mock.patch.object(store, "_save_index", wraps=store._save_index) as sv:
            store.update_session(s["id"], ctx=5)
            store.update_session(s["id"], ctx=5)
            self.assertEqual(sv.call_count, 1)

    def test_history_tail_and_count(self):
        s = store.create_session("t")
        sid = s["id"]
        self.assertEqual(store.count(sid), 0)
        for i in range(5):
            store.append(sid, {"k": "user", "text": "m%d" % i})
        self.assertEqual(store.count(sid), 5)
        self.assertEqual([e["text"] for e in store.history(sid, upto=2)], ["m0", "m1"])
        with mock.patch.object(store, "MAX_HISTORY", 2):
            self.assertEqual([e["text"] for e in store.history(sid)], ["m3", "m4"])
        store.delete_event(sid, 0)
        self.assertEqual(store.count(sid), 4)

    def test_search_cached_and_partial(self):
        s = store.create_session("hello")
        store.append(s["id"], {"k": "user", "text": "find the needle here"})
        r = store.search("needle")
        self.assertEqual(len(r), 1)
        self.assertFalse(r.partial)
        self.assertIn("needle", r[0]["snippet"])
        with mock.patch.object(store, "SEARCH_SECONDS", -1):
            self.assertTrue(store.search("needle").partial)

    def test_snapshot_incremental(self):
        s = store.create_session("t")
        d = store.session_dir(s["id"])
        for n, t in (("a.txt", "one"), ("b.txt", "two")):
            open(os.path.join(d, n), "w").write(t)
        store.snapshot(s["id"], store.tree(s["id"]))
        self.assertEqual(store.snap_text(s["id"], "a.txt"), "one")
        os.remove(os.path.join(d, "b.txt"))
        open(os.path.join(d, "a.txt"), "w").write("three!")
        store.snapshot(s["id"], store.tree(s["id"]))
        self.assertEqual(store.snap_text(s["id"], "a.txt"), "three!")
        self.assertEqual(store.snap_text(s["id"], "b.txt"), "")

    def test_write_text_unique_temp(self):
        s = store.create_session("t")
        d = store.session_dir(s["id"])
        open(os.path.join(d, "a.txt"), "w").write("x")
        store.write_text(s["id"], "a.txt", "yy")
        self.assertEqual(sorted(os.listdir(d)), ["a.txt"])


class ChatTurn(Base):
    def test_env_filter(self):
        got = chat.clean_env({"OPENAI_KEY": "a", "node:w:n:f": "secret", "bad-name": "x", "lower": "y", "_OK1": "z"})
        self.assertEqual(got, {"OPENAI_KEY": "a", "_OK1": "z"})

    def _sess(self):
        return store.create_session("t")

    def test_env_not_passed_to_child(self):
        s = self._sess()
        seen = {}

        def boom(cmd, **kw):
            seen.update(kw["env"])
            raise OSError("no claude")
        with mock.patch.object(chat.auth, "ready", return_value={"ok": True}), mock.patch.object(chat.subprocess, "Popen", boom):
            list(chat.turn(store.get_session(s["id"]), "hi", extra_env={"node:a:b:c": "SECRET", "GOOD": "1"}))
        self.assertEqual(seen.get("GOOD"), "1")
        self.assertNotIn("node:a:b:c", seen)

    def test_stop_before_popen(self):
        s = self._sess()
        run = chat.Run(0)
        chat.runs[s["id"]] = run
        run.cancel.set()
        with mock.patch.object(chat.auth, "ready", return_value={"ok": True}), mock.patch.object(chat.subprocess, "Popen") as po:
            evs = list(chat.turn(store.get_session(s["id"]), "hi"))
        po.assert_not_called()
        self.assertEqual(evs[-1], {"k": "done", "stopped": True})
        chat.runs.pop(s["id"], None)
        chat.stopped.discard(s["id"])

    def test_stop_marks_a_preparing_run(self):
        s = self._sess()
        run = chat.Run(0)
        chat.runs[s["id"]] = run
        self.assertTrue(chat.stop(s["id"]))
        self.assertTrue(run.cancel.is_set())
        chat.runs.pop(s["id"], None)
        chat.stopped.discard(s["id"])

    def test_deleted_session_makes_nothing(self):
        s = self._sess()
        sess = store.get_session(s["id"])
        store.delete_session(s["id"])
        with mock.patch.object(chat.auth, "ready", return_value={"ok": True}), mock.patch.object(chat.subprocess, "Popen") as po:
            evs = list(chat.turn(sess, "hi"))
        po.assert_not_called()
        self.assertEqual(evs, [{"k": "done"}])
        self.assertFalse(os.path.exists(store.work_dir(s["id"])))

    def test_ctx_written_once(self):
        s = self._sess()
        lines = [json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": n}, "content": []}}) + "\n" for n in (10, 20, 30)]

        class P:
            pid = 1
            returncode = 0
            stdin = mock.Mock()
            stderr = mock.Mock(readlines=lambda: [])
            stdout = lines

            def poll(self): return 0
            def wait(self, timeout=None): return 0
        with mock.patch.object(chat.auth, "ready", return_value={"ok": True}), mock.patch.object(chat.subprocess, "Popen", return_value=P()), \
                mock.patch.object(store, "update_session", wraps=store.update_session) as up:
            list(chat.turn(store.get_session(s["id"]), "hi"))
        ctxs = [c for c in up.call_args_list if "ctx" in c.kwargs]
        self.assertEqual(len(ctxs), 1)
        self.assertEqual(store.get_session(s["id"])["ctx"], 30)


if __name__ == "__main__":
    unittest.main()
