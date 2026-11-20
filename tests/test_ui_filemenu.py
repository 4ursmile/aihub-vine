"""aihub-ui: the canvas file menu (new, rename / move, copy / paste, delete, upload, zip) and tqdm output folding (\\r, ipywidgets)."""
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "aihub", "builtin_packages", "aihub-ui", "ui"))
from aihubui import jupyter, store  # noqa: E402


class FileMenu(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dirs = {"a": os.path.join(self.tmp, "a"), "b": os.path.join(self.tmp, "b")}
        for d in self.dirs.values():
            os.makedirs(d)
        orig = store.session_dir
        store.session_dir = lambda sid: self.dirs[sid]
        self.addCleanup(setattr, store, "session_dir", orig)

    def w(self, sid, rel, text="x"):
        p = os.path.join(self.dirs[sid], rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def ex(self, sid, rel):
        return os.path.lexists(os.path.join(self.dirs[sid], rel))

    def test_new_file_and_folder_and_empty_dirs(self):
        self.assertEqual(store.fs_new("a", "sub/deep/n.txt"), {"path": "sub/deep/n.txt"})
        self.assertTrue(os.path.isfile(os.path.join(self.dirs["a"], "sub/deep/n.txt")))
        store.fs_new("a", "empty", folder=True)
        self.assertEqual(store.empty_dirs("a"), ["empty"])
        with self.assertRaises(FileExistsError):
            store.fs_new("a", "sub/deep/n.txt")

    def test_paths_never_leave_the_folder(self):
        self.w("a", "f.txt")
        for bad in ("../b/x", "/../x", "", ".git/config", "sub/../../x"):
            with self.assertRaises(ValueError):
                store.fs_new("a", bad)
        os.symlink(self.dirs["b"], os.path.join(self.dirs["a"], "out"))
        with self.assertRaises(ValueError):
            store.fs_new("a", "out/evil.txt")                     # a parent that resolves outside
        store.fs_delete("a", "out")                                # the link itself goes, never what it points to
        self.assertTrue(os.path.isdir(self.dirs["b"]))
        self.assertFalse(self.ex("a", "out"))

    def test_rename_and_move(self):
        self.w("a", "d/f.txt")
        self.w("a", "g.txt")
        self.assertEqual(store.fs_rename("a", "d/f.txt", "d/h.txt"), {"path": "d/h.txt"})
        with self.assertRaises(FileExistsError):
            store.fs_rename("a", "g.txt", "d/h.txt")
        with self.assertRaises(ValueError):
            store.fs_rename("a", "d", "d/inner")
        with self.assertRaises(LookupError):
            store.fs_rename("a", "nope.txt", "x.txt")
        self.assertEqual(store.fs_rename("a", "d", "e/d2"), {"path": "e/d2"})
        self.assertTrue(self.ex("a", "e/d2/h.txt"))

    def test_copy_paste_names_and_cut(self):
        self.w("a", "r.txt", "one")
        self.w("a", "d/k.txt")
        self.assertEqual(store.fs_copy("a", "r.txt", "a", "")["path"], "r copy.txt")
        self.assertEqual(store.fs_copy("a", "r.txt", "a", "")["path"], "r copy 2.txt")
        self.assertEqual(store.fs_copy("a", "d", "a", "")["path"], "d copy")
        self.assertTrue(self.ex("a", "d copy/k.txt"))
        with self.assertRaises(ValueError):
            store.fs_copy("a", "d", "a", "d")                         # into itself
        self.assertEqual(store.fs_copy("a", "r.txt", "a", "", move=True), {"path": "r.txt"})   # same folder: nothing
        self.assertEqual(store.fs_copy("a", "r.txt", "a", "d", move=True), {"path": "d/r.txt"})
        self.assertFalse(self.ex("a", "r.txt"))
        self.assertEqual(store.fs_copy("a", "d/r.txt", "b", ""), {"path": "r.txt"})            # into another chat's folder
        with open(os.path.join(self.dirs["b"], "r.txt")) as f:
            self.assertEqual(f.read(), "one")
        with self.assertRaises(ValueError):
            store.fs_copy("a", "d/k.txt", "a", "d/k.txt")             # not a folder

    def test_delete_upload_zip(self):
        self.w("a", "d/k.txt", "k")
        self.w("a", "d/.secret", "s")
        self.assertEqual(store.fs_upload("a", "d", "u.bin", b"\x00\x01"), {"path": "d/u.bin"})
        self.assertEqual(store.fs_upload("a", "d", "u.bin", b"\x02"), {"path": "d/u (2).bin"})
        self.assertEqual(store.fs_upload("a", "d", "u.bin", b"\x03", replace=True), {"path": "d/u.bin"})
        with open(os.path.join(self.dirs["a"], "d/u.bin"), "rb") as f:
            self.assertEqual(f.read(), b"\x03")
        self.assertEqual(store.fs_upload("a", "", "../../x/evil.txt", b"e"), {"path": "evil.txt"})   # only the base name counts
        tmp, name = store.fs_zip("a", "d")
        self.addCleanup(os.remove, tmp)
        self.assertEqual(name, "d.zip")
        with zipfile.ZipFile(tmp) as z:
            self.assertEqual(sorted(z.namelist()), ["d/k.txt", "d/u (2).bin", "d/u.bin"])   # hidden files stay out
        store.fs_delete("a", "d")
        self.assertFalse(self.ex("a", "d"))


class Tqdm(unittest.TestCase):
    def test_carriage_returns_overwrite_the_line(self):
        self.assertEqual(jupyter.fix_cr("\r  0%|   | 0/3\r 33%|█  | 1/3\r100%|███| 3/3\n"), "100%|███| 3/3\n")
        self.assertEqual(jupyter.fix_cr("abc\rx"), "xbc")
        self.assertEqual(jupyter.fix_cr(jupyter.fix_cr("abc\r") + "xy"), "xyc")      # a trailing \r waits for the next chunk
        self.assertEqual(jupyter.fix_cr("a\r\nb"), "a\nb")
        f = jupyter.Fold()
        for i in range(500):
            f.add({"t": "stream", "c": {"name": "stderr", "text": "\r%3d%%|" % (i // 5)}})
        self.assertEqual(f.outputs[0]["text"], " 99%|")
        self.assertEqual(jupyter.outputs_text([{"output_type": "stream", "text": "a\rb\r"}]), "b")

    def test_notebook_progress_bar_is_written_as_text(self):
        f = jupyter.Fold()
        op = lambda cid, st: f.add({"t": "comm_open", "c": {"comm_id": cid, "target_name": "jupyter.widget", "data": {"state": st}}})
        upd = lambda cid, st: f.add({"t": "comm_msg", "c": {"comm_id": cid, "data": {"method": "update", "state": st}}})
        op("l", {"_model_name": "HTMLModel", "value": ""})
        op("p", {"_model_name": "FloatProgressModel", "value": 0, "min": 0, "max": 4})
        op("r", {"_model_name": "HTMLModel", "value": ""})
        op("box", {"_model_name": "HBoxModel", "children": ["IPY_MODEL_l", "IPY_MODEL_p", "IPY_MODEL_r"]})
        f.add({"t": "display_data", "c": {"data": {"text/plain": "HBox(children=(HTML(value=''), FloatProgress(value=0.0, max=4.0), HTML(value='')))",
                                                    jupyter.W_MIME: {"model_id": "box", "version_major": 2}}}})
        upd("p", {"value": 2})
        upd("l", {"value": " 50%"})
        upd("r", {"value": " 2/4 [00:01&lt;00:01,  1.9it/s]"})
        self.assertEqual(f.snap()[0]["data"]["text/plain"], " 50%|█████     | 2/4 [00:01<00:01,  1.9it/s]")
        self.assertEqual(len(f.outputs), 1)                                             # comm messages are not outputs
        f.add({"t": "comm_close", "c": {"comm_id": "box"}})
        self.assertEqual(f.snap()[0]["data"]["text/plain"], " 50%|█████     | 2/4 [00:01<00:01,  1.9it/s]")   # closed: the last text stays


if __name__ == "__main__":
    unittest.main()
