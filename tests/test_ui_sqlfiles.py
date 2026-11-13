"""aihub-ui: pasted Jupyter addresses are normalised; SQL `:name` parameters bind safely; saved SQL files keep their defaults."""
import os
import sys
import tempfile
import unittest

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "aihub", "builtin_packages", "aihub-ui", "ui")
sys.path.insert(0, UI)
from aihubui import jupyter, sqlrunner, sqlstudio, store  # noqa: E402


class NormalizeTest(unittest.TestCase):
    def test_hub_lab_url(self):
        n = jupyter.normalize_url("https://abcd.xyz/user/bob/lab", "server")
        self.assertEqual((n["url"], n["user"], n["hub"]), ("https://abcd.xyz/user/bob", "bob", "https://abcd.xyz"))

    def test_token_scheme_and_deep_link(self):
        n = jupyter.normalize_url("abcd.xyz/user/bob/lab/tree/a.ipynb?token=xyz", "hub")
        self.assertEqual((n["url"], n["token"], n["user"]), ("https://abcd.xyz", "xyz", "bob"))
        self.assertEqual(jupyter.normalize_url("localhost:8888/lab?token=a")["url"], "http://localhost:8888")

    def test_prefix_and_plain(self):
        self.assertEqual(jupyter.normalize_url("https://h/jupyter/lab")["url"], "https://h/jupyter")
        self.assertEqual(jupyter.normalize_url("http://h:8888/")["url"], "http://h:8888")
        self.assertEqual(jupyter.normalize_url("")["url"], "")

    def test_login_hint(self):
        self.assertEqual(jupyter.login_hint("https://abcd.xyz/user/bob/"), "https://abcd.xyz/hub/token")


class ParamTest(unittest.TestCase):
    def test_spans_skip_strings_casts_comments(self):
        sql = "select a::int, ':no', \"x:y\" from t where d = :day and n > :n -- :c\n and e = :day"
        self.assertEqual(sqlstudio.detect_params(sql), ["day", "n"])

    def test_bind_only_known(self):
        text, vals = sqlrunner.bind_sql("select :a, :b, '12:30'", {"a": 1})
        self.assertEqual(text, "select :a, \\:b, '12:30'")
        self.assertEqual(vals, {"a": 1})
        self.assertIsNone(sqlrunner.bind_sql("select 1", {"a": 1}))

    def test_colons_in_quotes_and_comments_are_escaped(self):
        text, _ = sqlrunner.bind_sql("select :a, 'it''s :c' -- :d\n, x::int", {"a": 1})
        self.assertEqual(text, "select :a, 'it''s \\:c' -- \\:d\n, x::int")

    def test_coerce(self):
        self.assertEqual(sqlstudio.coerce_params({"a": "5", "b": "2.5", "c": "007", "d": "x", "bad name": "1"}), {"a": 5, "b": 2.5, "c": "007", "d": "x"})


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = store.ui_dir
        store.ui_dir = lambda *a: os.path.join(self.tmp, *a)

    def tearDown(self):
        store.ui_dir = self.old

    def test_save_get_update_delete(self):
        f = sqlstudio.file_save({"name": "Daily", "sql": "select * from t where d = :day", "conn": "c1", "params": [{"name": "day", "value": "2026-01-01"}, {"name": "junk", "value": "x"}]})
        self.assertEqual(f["params"], [{"name": "day", "value": "2026-01-01"}])
        again = sqlstudio.file_save({"name": "daily", "sql": "select :n", "params": []})
        self.assertEqual(again["id"], f["id"])
        self.assertEqual(again["params"], [{"name": "n", "value": ""}])
        self.assertEqual(sqlstudio.file_get("DAILY")["id"], f["id"])
        with self.assertRaises(ValueError):
            sqlstudio.file_get("nope")
        self.assertTrue(sqlstudio.file_delete(f["id"]))
        self.assertEqual(sqlstudio.files(), [])

    def test_connections_survive_file_saves(self):
        sqlstudio._save({"connections": [{"id": "abcdef01", "name": "x", "driver": "sqlite"}]})
        sqlstudio.file_save({"name": "a", "sql": "select 1"})
        self.assertEqual(len(sqlstudio.connections()), 1)


if __name__ == "__main__":
    unittest.main()


class SuggestContextTest(unittest.TestCase):
    def setUp(self):
        import sys
        sys.path.insert(0, "aihub/builtin_packages/aihub-ui/ui")
        from aihubui import jupyterai
        self.j = jupyterai

    def test_digest_is_compact(self):
        cells = [{"type": "code", "src": "import pandas as pd\n" + "x = 1\n" * 500 + "def f(a):\n    return a\n"}, {"type": "code", "src": "df.head()", "out": "a b"}]
        d = self.j.digest(cells)
        self.assertIn("import pandas as pd", d)
        self.assertIn("def f(a):", d)
        self.assertLessEqual(len(d), 1600)

    def test_overlap_is_dropped(self):
        self.assertEqual(self.j._overlap("x = pri", "print(x)"), "nt(x)")
        self.assertEqual(self.j._overlap("a = ", "1"), "1")


class InspectParseTest(unittest.TestCase):
    def test_signature_and_doc(self):
        import sys
        sys.path.insert(0, "aihub/builtin_packages/aihub-ui/ui")
        from aihubui import jupyter
        r = jupyter.parse_inspect("Signature: pd.read_csv(\n    path,\n    sep=',',\n)\nDocstring:\nRead a csv.\n\nMore.\nFile: x")
        self.assertEqual(r["sig"], "pd.read_csv( path, sep=',', )")
        self.assertEqual(r["doc"], "Read a csv.")


class SuggestShapeTest(unittest.TestCase):
    def setUp(self):
        import sys
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "aihub/builtin_packages/aihub-ui/ui"))
        from aihubui import jupyterai
        self.ai = jupyterai

    def test_mode(self):
        self.assertEqual(self.ai._mode("df = pd.read_csv('a.csv')\ndf.gr")[0], "line")
        self.assertEqual(self.ai._mode("# load the data\n")[0], "block")
        self.assertEqual(self.ai._mode("def f(x):\n    ")[0], "function")

    def test_trim_block_stops_at_dedent(self):
        out = self.ai._trim("    return x\n\nprint(f(1))", "def f(x):\n    ", "", "block", 14)
        self.assertEqual(out, "return x")

    def test_trim_line_stops_at_blank_and_suffix(self):
        self.assertEqual(self.ai._trim("oupby('a').sum()\n\nnext()", "df.gr", "", "line", 3), "oupby('a').sum()")
        self.assertEqual(self.ai._trim("a, b)", "f(", ")", "line", 3), "a, b")


class CodeTreeTest(unittest.TestCase):
    def setUp(self):
        import sys
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "aihub/builtin_packages/aihub-ui/ui"))
        from aihubui import codetree, jupyterai
        self.ct, self.ai = codetree, jupyterai

    def test_symbols_scope_and_unfinished_code(self):
        cells = [{"type": "code", "src": "import pandas as pd\ndf = pd.read_csv('a.csv')\ndef load(x: int) -> list:\n    '''Load it.'''\n    return []\nclass A(B):\n    def go(self): pass"}]
        out = self.ct.context(cells, "def total(rows, tax):\n    s = 0\n    t = df.gr", "")
        for want in ("import pandas as pd", "df: DataFrame", "load(x: int) -> list # Load it.", "A(B)", "inside: total(rows, tax)", "locals: s, t"):
            self.assertIn(want, out)

    def test_broken_cell_does_not_raise(self):
        self.assertIsInstance(self.ct.context([{"type": "code", "src": "def (:\n  ))"}], "x = (", ""), str)

    def test_function_mode(self):
        self.assertEqual(self.ai._mode("def add(a, b):")[0], "function")
        self.assertEqual(self.ai._mode("for i in x:\n    ")[0], "block")
        self.assertEqual(self.ai._mode("def add(a, b):\n")[0], "function")
        out = self.ai._trim("\n    return a + b\n\nprint(add(1, 2))", "def add(a, b):", "", "function", 40)
        self.assertEqual(out, "\n    return a + b")

    def test_cache_reuses_longer_prefix(self):
        self.ai._buckets, self.ai._loaded = {}, True
        self.ai._cache_put("c1", "df.gr", "oupby('a').sum()")
        self.assertEqual(self.ai._cache_get("c1", "df.gr"), "oupby('a').sum()")
        self.assertEqual(self.ai._cache_get("c1", "df.grou"), "pby('a').sum()")
        self.assertIsNone(self.ai._cache_get("c1", "df.xyz"))
        self.ai._buckets = {}

    def test_function_body_moves_to_the_next_line(self):
        out = self.ai._trim('"""Doc."""\n    return 1', "def f():", "", "function", 40)
        self.assertEqual(out, '\n    """Doc."""\n    return 1')
