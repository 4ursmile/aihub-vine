"""aihub-ui Works: a chat changes an existing work (works/make with an id), its backup and Undo (works/restore), and works/rename."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "aihub", "builtin_packages", "aihub-ui", "ui")
EXAMPLE = os.path.join(ROOT, "aihub", "builtin_packages", "aihub-nodes-teams")
sys.path.insert(0, UI)
from aihubui import secretbox, store, works as W  # noqa: E402


class UpdateWork(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.environ.get("AIHUB_HOME")
        os.environ["AIHUB_HOME"] = self.tmp
        os.makedirs(os.path.join(self.tmp, "ui"), exist_ok=True)
        self.box = secretbox.Box(os.path.join(self.tmp, "ui"))
        W.A.run = lambda args, **k: {"data": [{"name": "aihub-nodes-teams", "path": EXAMPLE, "enabled": True}]}
        W._cat.update(t=0.0, v={})
        import server
        self.server = server
        server.BOX, server.ENGINE = self.box, None

    def tearDown(self):
        if self.old is None:
            os.environ.pop("AIHUB_HOME", None)
        else:
            os.environ["AIHUB_HOME"] = self.old

    def existing(self):
        c = W.save_credential({"kind": "smtp", "name": "Mail", "fields": {"host": "smtp.x.com", "password": "set-pw"}}, self.box)
        w = store.clean_work({"title": "Mail me", "enabled": True, "nodes": [
            {"id": "s", "type": "start.manual", "x": 40, "y": 40},
            {"id": "m", "type": "send.smtp", "x": 340, "y": 40, "label": "Send", "credential": c["id"], "config": {"to": "a@x.com", "password": "node-pw"}}],
            "edges": [{"from": "s", "to": "m"}]})
        W.strip_secrets(w, self.box)
        return store.save_work(w), c

    def test_update_keeps_id_state_stored_secret_and_set_and_drops_typed_secrets(self):
        w, c = self.existing()
        self.assertEqual(w["nodes"][1]["config"]["password"], W.MARK)
        prop = {"id": w["id"], "title": "Mail the team", "nodes": [
            {"id": "s", "type": "start.manual"},
            {"id": "m", "type": "send.smtp", "label": "Send to team", "config": {"to": "team@x.com", "password": "LEAKED"}},
            {"id": "m2", "type": "send.smtp", "config": {"to": "b@x.com", "password": "LEAKED2"}}],
            "edges": [{"from": "s", "to": "m"}, {"from": "s", "to": "m2"}]}
        r = self.server.work_make(None, {}, {"work": prop})
        nw = r["work"]
        self.assertTrue(r["updated"])
        self.assertEqual((nw["id"], nw["enabled"], nw["title"]), (w["id"], True, "Mail the team"))
        m = next(n for n in nw["nodes"] if n["id"] == "m")
        self.assertEqual((m["config"]["password"], m["config"]["to"], m["credential"], m["label"]), (W.MARK, "team@x.com", c["id"], "Send to team"))
        m2 = next(n for n in nw["nodes"] if n["id"] == "m2")
        self.assertNotIn("password", m2["config"])
        self.assertEqual(m2.get("credential"), c["id"])                     # the one set of its kind
        saved = json.dumps(store.get_work(w["id"]))
        self.assertNotIn("LEAKED", saved)
        self.assertEqual(self.box.get_all()[W._skey(w["id"], "m", "password")], "node-pw")    # the box still holds the stored one

    def test_unknown_type_is_refused_and_nothing_changes(self):
        w, _ = self.existing()
        with self.assertRaises(self.server.Err):
            self.server.work_make(None, {}, {"work": {"id": w["id"], "nodes": [{"id": "s", "type": "start.manual"}, {"id": "x", "type": "no.such.node"}], "edges": []}})
        self.assertEqual(store.get_work(w["id"])["title"], "Mail me")

    def test_backup_is_written_and_restore_puts_the_old_version_back(self):
        w, _ = self.existing()
        r = self.server.work_make(None, {}, {"work": {"id": w["id"], "title": "Changed", "nodes": [{"id": "s", "type": "start.manual"}], "edges": []}})
        self.assertTrue(os.path.isfile(os.path.join(store.ui_dir("works-history"), r["backup"])))
        self.assertEqual(len(store.get_work(w["id"])["nodes"]), 1)
        back = self.server.work_restore(None, {}, {"id": w["id"], "backup": r["backup"]})["work"]
        self.assertEqual((back["title"], len(back["nodes"])), ("Mail me", 2))
        with self.assertRaises(self.server.Err):
            self.server.work_restore(None, {}, {"id": w["id"], "backup": "../../etc-1234567890123.json"})
        with self.assertRaises(self.server.Err):
            self.server.work_restore(None, {}, {"id": "other", "backup": r["backup"]})

    def test_backups_are_capped(self):
        w, _ = self.existing()
        for i in range(W.HISTORY_KEEP + 3):
            W.backup_work(dict(w, title="v%d" % i))
            os.utime(store.ui_dir("works-history"))
        names = [f for f in os.listdir(store.ui_dir("works-history")) if f.startswith(w["id"] + "-")]
        self.assertLessEqual(len(names), W.HISTORY_KEEP)

    def test_a_new_id_still_creates_a_work_switched_off(self):
        r = self.server.work_make(None, {}, {"work": {"id": "nosuch12", "title": "New", "nodes": [{"id": "s", "type": "start.manual"}], "edges": []}})
        self.assertNotIn("updated", r)
        self.assertFalse(r["work"]["enabled"])

    def test_rename(self):
        w, _ = self.existing()
        self.assertEqual(self.server.work_rename(None, {}, {"id": w["id"], "title": "  Weekly   mail "})["work"]["title"], "Weekly mail")
        self.assertEqual(store.get_work(w["id"])["nodes"][1]["config"]["password"], W.MARK)
        with self.assertRaises(self.server.Err):
            self.server.work_rename(None, {}, {"id": w["id"], "title": "  "})


if __name__ == "__main__":
    unittest.main()
