"""aihub-ui incognito chats: never listed or searched, removed with their files (and Claude Code's transcript) when left or at start."""
import os
import sys
import tempfile
import unittest

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "aihub", "builtin_packages", "aihub-ui", "ui")
sys.path.insert(0, UI)
from aihubui import store  # noqa: E402


class Incognito(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = {k: os.environ.get(k) for k in ("AIHUB_HOME", "HOME")}
        os.environ["AIHUB_HOME"] = os.path.join(self.tmp, "aihub")
        os.environ["HOME"] = self.tmp                                   # ~/.claude/projects lives here for the test

    def tearDown(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def claude_log(self, s):
        d = os.path.join(self.tmp, ".claude", "projects", "-some-folder")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, s["claude"] + ".jsonl")
        with open(p, "w") as f:
            f.write("{}\n")
        return p

    def test_hidden_from_the_list_and_search_but_opens(self):
        keep = store.create_session("plain words here")
        inc = store.create_session("secret words here", incognito=True)
        store.append(inc["id"], {"k": "user", "text": "secret words here"})
        ids = [s["id"] for s in store.sessions()["sessions"]]
        self.assertIn(keep["id"], ids)
        self.assertNotIn(inc["id"], ids)
        self.assertEqual([h["id"] for h in store.search("words")], [keep["id"]])
        self.assertTrue(store.get_session(inc["id"])["incognito"])

    def test_delete_removes_folder_and_every_claude_transcript(self):
        s = store.create_session(incognito=True)
        first = self.claude_log(s)
        s = store.update_session(s["id"], claude="11111111-2222-3333-4444-555555555555")   # a new Claude session (folder / soul change)
        second = self.claude_log(s)
        folder = store.work_dir(s["id"])
        self.assertTrue(os.path.isdir(folder))
        store.delete_session(s["id"])
        self.assertFalse(os.path.exists(first) or os.path.exists(second) or os.path.exists(folder))
        self.assertIsNone(store.get_session(s["id"]))

    def test_a_normal_chat_keeps_its_claude_transcript(self):
        s = store.create_session()
        p = self.claude_log(s)
        store.delete_session(s["id"])
        self.assertTrue(os.path.exists(p))

    def test_keep_makes_it_a_normal_chat(self):
        s = store.create_session("kept one", incognito=True)
        s = store.update_session(s["id"], incognito=False)
        self.assertNotIn("incognito", s)
        self.assertIn(s["id"], [x["id"] for x in store.sessions()["sessions"]])
        store.purge_incognito()
        self.assertIsNotNone(store.get_session(s["id"]))

    def test_start_purges_leftovers(self):
        a, b = store.create_session(incognito=True), store.create_session()
        store.purge_incognito()
        self.assertIsNone(store.get_session(a["id"]))
        self.assertIsNotNone(store.get_session(b["id"]))

    def test_server_only_turns_an_empty_chat_incognito(self):
        import server
        s = store.create_session()
        self.assertTrue(server.session_update(None, {}, {"id": s["id"], "incognito": True})["session"]["incognito"])
        t = store.create_session()
        store.append(t["id"], {"k": "user", "text": "hi"})
        with self.assertRaises(server.Err):
            server.session_update(None, {}, {"id": t["id"], "incognito": True})
        n = server.session_new(None, {}, {"incognito": True})["session"]
        self.assertTrue(n["incognito"])


if __name__ == "__main__":
    unittest.main()
