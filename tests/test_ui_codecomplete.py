"""aihub-ui completion without a kernel (aihubui/codecomplete.py, POST code/complete) and the quick terminal probe (term/read wait=0)."""
import os
import sys
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "aihub", "builtin_packages", "aihub-ui", "ui")
sys.path.insert(0, UI)
from aihubui import codecomplete as C, term  # noqa: E402


def names(r):
    return [m["t"] for m in r["matches"]]


class PythonFallback(unittest.TestCase):
    """Without jedi: the file's names, keywords, builtins and members of the standard-library modules the file imports."""

    def setUp(self):
        p = mock.patch.object(C, "_jedi", False)
        p.start()
        self.addCleanup(p.stop)

    def at_end(self, code, lang="python"):
        return C.complete(code, len(code), lang)

    def test_members_of_an_imported_stdlib_module(self):
        r = self.at_end("import os\nos.pa")
        self.assertIn("path", names(r))
        self.assertEqual(r["start"], len("import os\nos."))

    def test_dotted_module_and_alias(self):
        self.assertIn("join", names(self.at_end("import os.path\nos.path.jo")))
        self.assertIn("dumps", names(self.at_end("import json as j\nj.du")))
        self.assertIn("fromkeys", names(self.at_end("from collections import OrderedDict as od\nod.fro")))

    def test_private_names_only_when_asked(self):
        self.assertFalse([n for n in names(self.at_end("import json\njson.")) if n.startswith("_")])

    def test_words_of_the_file_and_builtins(self):
        r = self.at_end("total_amount = 3\nprint(tot")
        self.assertEqual(names(r)[0], "total_amount")
        self.assertIn("print", names(self.at_end("x = 1\npri")))

    def test_modules_with_import_side_effects_are_never_imported(self):
        with mock.patch.object(C.importlib, "import_module") as imp:
            self.assertEqual(names(self.at_end("import antigravity\nantigravity.")), [])
            self.assertEqual(names(self.at_end("import webbrowser\nwebbrowser.op")), [])
            imp.assert_not_called()

    def test_not_imported_or_not_stdlib_gives_nothing(self):
        self.assertEqual(names(self.at_end("os.pa")), [])
        self.assertEqual(names(self.at_end("import pandas\npandas.Da")), [])

    def test_never_raises(self):
        for args in ((None, None), ("abc", "x"), ("x" * 10, -5), ("a.b.", 10 ** 9)):
            r = C.complete(*args, lang="python")
            self.assertIsInstance(r["matches"], list)


class OtherLanguages(unittest.TestCase):
    def test_shell_first_word_is_a_command_on_path(self):
        with mock.patch.object(C, "_commands", return_value=["git", "gitk", "grep"]):
            self.assertEqual(names(C.complete("gi", 2, "sh")), ["git", "gitk"])
            r = C.complete("ls | gr", 7, "sh")
            self.assertEqual((names(r), r["start"]), (["grep"], 5))

    def test_shell_files_and_folders_of_the_shell_folder(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "src", "app"))
            os.makedirs(os.path.join(d, "my dir"))
            for f in ("README.md", "src/main.py", ".hidden"):
                open(os.path.join(d, f), "w").close()
            self.assertEqual(names(C.complete("cat REA", 7, "sh", cwd=d)), ["README.md"])
            r = C.complete("vim src/m", 9, "sh", cwd=d)
            self.assertEqual((names(r), r["start"]), (["src/main.py"], 4))
            self.assertEqual(names(C.complete("cd src/", 7, "sh", cwd=d)), ["src/app/"])           # cd: folders only
            self.assertEqual(names(C.complete("ls ./s", 6, "sh", cwd=d)), ["./src/"])
            self.assertEqual(names(C.complete("ls my", 5, "sh", cwd=d)), ["my\\ dir/"])           # spaces escaped
            self.assertNotIn(".hidden", names(C.complete("ls ", 3, "sh", cwd=d)))
            self.assertIn(".hidden", names(C.complete("ls .h", 5, "sh", cwd=d)))
            self.assertEqual(names(C.complete("cat REA", 7, "sh")), [])                           # no folder named: no files (remote terminal)

    def test_shell_git_subcommands_and_common_commands_first(self):
        self.assertEqual(names(C.complete("git sta", 7, "sh"))[:2], ["stash", "status"])
        with mock.patch.object(C, "_commands", return_value=["gio", "git", "gi-tool"]):
            self.assertEqual(names(C.complete("gi", 2, "sh"))[0], "git")

    def test_plain_text_uses_its_words(self):
        r = C.complete("alpha beta\nalp", 14, "")
        self.assertEqual(names(r), ["alpha"])


class TermProbe(unittest.TestCase):
    """The chat terminal re-attaches to a shell left running: term/read with wait=0 answers at once."""

    def test_read_with_no_wait_returns_immediately(self):
        t = term.Term.__new__(term.Term)
        t.buf, t.base, t.code = bytearray(b"hello"), 0, None
        t.cv = __import__("threading").Condition()
        t.last_read = 0
        t0 = time.time()
        data, pos, code = t.read(10 ** 12, 0.0)
        self.assertLess(time.time() - t0, 0.5)
        self.assertEqual((data, pos, code), (b"", 5, None))

    def test_shells_live_longer_than_installs_when_unread(self):
        self.assertGreater(term.IDLE_REAP_SHELL, term.IDLE_REAP)

    @unittest.skipIf(os.name == "nt", "no cwd lookup on Windows")
    def test_cwd_of_follows_the_shell_and_falls_back_to_its_start_folder(self):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=d)
            self.addCleanup(p.wait)
            self.addCleanup(p.kill)
            t = term.Term.__new__(term.Term)
            t.p, t.code, t.cwd = p, None, "/nowhere"
            with mock.patch.object(term, "get", return_value=t):
                self.assertEqual(os.path.realpath(term.cwd_of("x")), os.path.realpath(d))
                t.code = 0                                          # ended: the folder it started in
                self.assertEqual(term.cwd_of("x"), "/nowhere")
        self.assertEqual(term.cwd_of("no-such-terminal"), "")


if __name__ == "__main__":
    unittest.main()
