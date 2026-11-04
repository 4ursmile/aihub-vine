"""aihub-ui: a work's own working folder, VS Code / file manager launchers (Windows too), draw.io native viewer page."""
import json
import os
import sys
import tempfile
import unittest
import unittest.mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "aihub", "builtin_packages", "aihub-ui", "ui")
sys.path.insert(0, UI)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aihubui import drawioview, store, workpkg, works as W  # noqa: E402
from test_ui_works import Base  # noqa: E402


class WorkFolder(Base):
    def test_clean_work_keeps_the_folder_and_sharing_drops_it(self):
        w = store.clean_work({"title": "t", "nodes": [], "edges": [], "workdir": "  ~/proj  "})
        self.assertEqual(w["workdir"], "~/proj")
        self.assertNotIn("workdir", store.clean_work({"title": "t", "nodes": [], "edges": []}))
        self.assertNotIn("workdir", workpkg.strip_for_share(w))

    def test_runs_work_in_the_folder_and_list_only_what_they_changed(self):
        proj = os.path.join(self.tmp, "proj")
        os.makedirs(proj)
        with open(os.path.join(proj, "old.txt"), "w") as f:
            f.write("before")
        os.utime(os.path.join(proj, "old.txt"), (1, 1))
        nodes = [{"id": "a", "type": "start.manual", "config": {}},
                 {"id": "p", "type": "code.python", "config": {"code": "# %% w\nimport os\nopen(os.path.join(workdir, 'out.txt'), 'w').write('hi')\nresult['wd'] = workdir"}}]
        w = store.clean_work({"title": "t", "nodes": nodes, "edges": [{"from": "a", "to": "p"}], "workdir": proj})
        w = store.save_work(w)
        self.engine.fire(w, w["nodes"][0], {"text": "go"}, "test")
        r = W.get_run(w["id"], self.final(w["id"])[0]["id"])
        self.assertEqual(r["status"], "done", r)
        self.assertTrue(r["workdir"])
        self.assertEqual(os.path.realpath(r["dir"]), os.path.realpath(proj))
        with open(os.path.join(proj, "out.txt")) as f:
            self.assertEqual(f.read(), "hi")
        self.assertEqual([x["path"] for x in W.run_files(w["id"], r["id"])], ["out.txt"])          # old.txt was there before the run
        self.assertEqual(W.run_root(w["id"], r["id"]), r["dir"])

    def test_without_a_folder_runs_keep_their_own_run_folder(self):
        w = self.save([{"id": "a", "type": "start.manual", "config": {}}, {"id": "o", "type": "flow.output", "config": {"template": "x"}}], [{"from": "a", "to": "o"}], enabled=False)
        self.engine.fire(w, w["nodes"][0], {"text": "go"}, "test")
        r = W.get_run(w["id"], self.final(w["id"])[0]["id"])
        self.assertFalse(r.get("workdir"))
        self.assertEqual(W.run_root(w["id"], r["id"]), W.run_dir(w["id"], r["id"], make=False))

    def test_an_unusable_folder_fails_the_run_with_a_message(self):
        f = os.path.join(self.tmp, "afile")
        open(f, "w").close()
        w = store.save_work(store.clean_work({"title": "t", "nodes": [{"id": "a", "type": "start.manual", "config": {}}], "edges": [], "workdir": f}))
        self.engine.fire(w, w["nodes"][0], {"text": "go"}, "test")
        r = W.get_run(w["id"], self.final(w["id"])[0]["id"])
        self.assertEqual(r["status"], "failed")
        self.assertIn("working folder of this work", r["error"])


class Launchers(unittest.TestCase):
    def setUp(self):
        import server
        self.server = server

    def test_windows_starts_code_exe_not_the_cmd_shim(self):
        tmp = os.path.realpath(tempfile.mkdtemp())
        inst = os.path.join(tmp, "Microsoft VS Code")
        os.makedirs(os.path.join(inst, "bin"))
        for p in (os.path.join(inst, "Code.exe"), os.path.join(inst, "bin", "code.cmd")):
            open(p, "w").close()
        s = self.server
        with unittest.mock.patch.object(s.os, "name", "nt"), unittest.mock.patch.object(s.shutil, "which", lambda n: os.path.join(inst, "bin", "code.cmd") if n == "code" else None), \
                unittest.mock.patch.dict(os.environ, {"LOCALAPPDATA": "", "ProgramFiles": ""}):
            self.assertEqual(s._vscode_cmd(), [os.path.join(inst, "Code.exe")])
        with unittest.mock.patch.object(s.os, "name", "nt"), unittest.mock.patch.object(s.shutil, "which", lambda n: None), \
                unittest.mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp, "ProgramFiles": ""}):
            os.makedirs(os.path.join(tmp, "Programs", "Microsoft VS Code"))
            exe = os.path.join(tmp, "Programs", "Microsoft VS Code", "Code.exe")
            open(exe, "w").close()
            self.assertEqual(s._vscode_cmd(), [exe])

    def test_gui_children_get_no_console_and_no_electron_node_mode(self):
        s = self.server
        with unittest.mock.patch.object(s.os, "name", "nt"), unittest.mock.patch.object(s.subprocess, "Popen") as popen, \
                unittest.mock.patch.dict(os.environ, {"ELECTRON_RUN_AS_NODE": "1"}):
            s._spawn_gui(["x"])
        kw = popen.call_args.kwargs
        self.assertTrue(kw["creationflags"] & 0x08000000)
        self.assertNotIn("ELECTRON_RUN_AS_NODE", kw["env"])

    def test_reveal_selects_a_file_and_opens_a_folder(self):
        s, d = self.server, tempfile.mkdtemp()
        f = os.path.join(d, "a.txt")
        open(f, "w").close()
        with unittest.mock.patch.object(s, "_spawn_gui") as sp:
            with unittest.mock.patch.object(s.os, "name", "nt"):
                s._reveal(f)
                self.assertEqual(sp.call_args.args[0], ["explorer", "/select," + os.path.normpath(f)])
            with unittest.mock.patch.object(s.sys, "platform", "darwin"):
                s._reveal(f)
                self.assertEqual(sp.call_args.args[0], ["open", "-R", os.path.normpath(f)])
                s._reveal(d)
                self.assertEqual(sp.call_args.args[0], ["open", os.path.normpath(d)])


class DrawioNative(unittest.TestCase):
    def test_native_page_embeds_the_file_and_falls_back(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.drawio")
        xml = '<mxfile><diagram name="A &amp; B"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0" value="say &quot;hi&quot; &lt;/script&gt;"/></root></mxGraphModel></diagram></mxfile>'
        with open(p, "w") as f:
            f.write(xml)
        page = drawioview.native_page(p, "/pub/viewer-static.min.js", "/pub/")
        self.assertIn("<script src='/pub/viewer-static.min.js' onerror='aihubBasic()'>", page)
        self.assertNotIn("</script>\"", page)
        import html as H
        import re
        cfg = json.loads(H.unescape(re.search(r'data-mxgraph="([^"]*)"', page).group(1)))
        self.assertEqual(cfg["xml"], xml)
        self.assertFalse(cfg["lightbox"])
        with open(p, "w") as f:
            f.write("not xml")
        with self.assertRaises(ValueError):
            drawioview.native_page(p, "/v.js", "/")

    def test_server_uses_the_hosted_viewer_when_none_is_vendored(self):
        import server
        p = os.path.join(tempfile.mkdtemp(), "x.drawio")
        with open(p, "w") as f:
            f.write("<mxfile/>")
        with unittest.mock.patch.object(server, "PUB_DIR", tempfile.mkdtemp()):
            page = server.drawio_native(p)
            self.assertIn("src='https://www.draw.io/js/viewer.min.js'", page)
            self.assertNotIn("STENCIL_PATH", page)                              # the hosted viewer keeps its own paths
            self.assertIn("https://www.draw.io", server.DRAWIO_CSP)
            with unittest.mock.patch.object(server, "DRAWIO_REMOTE", ""):
                self.assertIsNone(server.drawio_native(p))


if __name__ == "__main__":
    unittest.main()
