"""aihub-ui: a node mentioned in a chat (@node:<type>) is briefed to the model and run alone from a ```node card."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "aihub", "builtin_packages", "aihub-ui", "ui"))
from aihubui import works as W  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "aihub", "builtin_packages", "aihub-ui", "ui"))

CAT = {t: dict(s, type=t) for t, s in W.BUILTIN.items()}


def spec(t):
    s = dict(CAT[t])
    s["fields"] = [dict(f, secret=bool(f.get("secret")) or f.get("type") == "password") for f in s["fields"]]
    s.setdefault("dir", W.UI_DIR)
    return s


class NodeChat(unittest.TestCase):
    def test_only_action_nodes_with_a_program_are_runnable(self):
        self.assertTrue(W.runnable(spec("io.read_file")))
        self.assertFalse(W.runnable(spec("ai.claude")))
        self.assertFalse(W.runnable(spec("start.manual")))

    def test_brief_names_fields_protocol_and_never_asks_secrets(self):
        b = W.node_brief(spec("io.request"))
        self.assertIn("@node:io.request", b)
        self.assertIn("```node", b)
        self.assertIn("```form", b)
        for f in spec("io.request")["fields"]:
            self.assertIn(f["name"], b)

    def test_required_missing_is_reported_and_types_checked(self):
        s = spec("io.read_file")
        req = [f for f in s["fields"] if f.get("required")]
        if req:
            with self.assertRaises(ValueError) as c:
                W.clean_node_config(s, {})
            self.assertIn("missing", str(c.exception))
        self.assertNotIn("bogus", W.clean_node_config(s, {"path": "x", "bogus": 1}))

    def test_read_file_runs_alone(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "note.txt")
        with open(p, "w") as f:
            f.write("hello from a node")
        r = W.run_single(spec("io.read_file"), {"path": p}, "", d)
        self.assertIn("hello from a node", r["text"])

    def test_workflow_brief_lists_inputs_and_the_work_block(self):
        from aihubui import store
        w = {"id": "abcd1234", "title": "Report", "description": "d", "nodes": [{"type": "start.manual", "config": {"inputs": "repo|Repository|text||*\nfmt|Format|select|md,pdf"}}]}
        b = store.work_brief(w)
        self.assertIn("@work:abcd1234", b)
        self.assertIn("repo", b)
        self.assertIn("```work", b)
        self.assertIn("```form", b)


    def test_project_with_a_manifest_is_found_folder_and_monorepo(self):
        import server
        d = tempfile.mkdtemp()
        self.assertEqual(server.project_packages(d), [])
        os.makedirs(os.path.join(d, "sub"))
        with open(os.path.join(d, "sub", "aihub.toml"), "w") as f:
            f.write('[package]\nname = "sub-pkg"\nversion = "0.2.0"\ntype = "skill"\n')
        with open(os.path.join(d, "aihub.toml"), "w") as f:
            f.write('[package]\nname = "top"\nversion = "1.0.0"\n[bot]\nname = "no"\n')
        got = server.project_packages(d)
        self.assertEqual([(p["name"], p["version"], p["path"]) for p in got], [("top", "1.0.0", ""), ("sub-pkg", "0.2.0", "sub")])


if __name__ == "__main__":
    unittest.main()
