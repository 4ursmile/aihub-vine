"""aihub-ui: picker models from a commented settings.json, the Python function node, Office previews."""
import json
import os
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "aihub", "builtin_packages", "aihub-ui", "ui")
sys.path.insert(0, UI)
from aihubui import aihub as A, officeview, store, works as W  # noqa: E402


class Settings(unittest.TestCase):
    def test_commented_settings_and_option_shapes_still_list_models(self):
        tmp = tempfile.mkdtemp()
        old = os.environ.get("AIHUB_HOME")
        os.environ["AIHUB_HOME"] = tmp
        self.addCleanup(lambda: os.environ.pop("AIHUB_HOME", None) if old is None else os.environ.__setitem__("AIHUB_HOME", old))
        f = os.path.join(tmp, "s.json")
        with open(f, "w") as fh:
            fh.write('{\n // comment\n "modelPicker": {"options": [{"modelId": "a/b-1", "label": "B"}, "plain-1",],},\n "x": "http://u//v"\n}')
        store.save_prefs({"claude_settings": f})
        ids = [m["id"] for m in A.models()["models"]]
        self.assertIn("a/b-1", ids)
        self.assertIn("plain-1", ids)
        with open(f, "w") as fh:
            json.dump({"modelPicker": {"options": {"m/one": "One", "m/two": {"label": "Two"}}}}, fh)
        ids = [m["id"] for m in A.models()["models"]]
        self.assertTrue({"m/one", "m/two"} <= set(ids))


class PythonNode(unittest.TestCase):
    spec = dict(W.BUILTIN["code.python"], type="code.python", dir=W.UI_DIR)

    def go(self, code, inp="", args="", data=None):
        out, _ = W.run_package_node(self.spec, {"code": code, "args": args, "timeout": 30}, [{"text": inp, "data": data or {}}], {}, tempfile.gettempdir())
        return out["data"]

    def test_dict_in_dict_out_with_arguments_field(self):
        self.assertEqual(self.go("def main(a: dict) -> dict:\n    return {'n': a['n'] * 2}", args='{"n": 4}'), {"n": 8})

    def test_input_json_and_upstream_data_fill_the_argument(self):
        code = "from typing import TypedDict\nclass T(TypedDict):\n    x: int\ndef run(a: T) -> T:\n    return {'x': a['x'] + 1}"
        self.assertEqual(self.go(code, data={"x": 1}), {"x": 2})
        self.assertEqual(self.go(code, inp='```json\n{"x": 5}\n```'), {"x": 6})

    def test_pydantic_sample(self):
        try:
            import pydantic  # noqa: F401
        except ImportError:
            self.skipTest("pydantic not installed")
        sample = self.spec["fields"][0]["default"]
        self.assertEqual(self.go(sample, inp='{"text": "ab", "times": 3}'), {"result": "ababab", "length": 6})
        self.assertEqual(self.go(sample, inp="hi")["result"], "hihi")                      # plain text fills `text`
        with self.assertRaises(RuntimeError) as c:
            self.go(sample, inp='{"times": "x"}')
        self.assertIn("does not match", str(c.exception))

    def test_untyped_argument_or_return_is_refused(self):
        with self.assertRaises(RuntimeError) as c:
            self.go("def main(a):\n    return {}")
        self.assertIn("annotate the argument", str(c.exception))
        with self.assertRaises(RuntimeError) as c:
            self.go("def main(a: dict) -> int:\n    return 1")
        self.assertIn("annotate the return", str(c.exception))


class Office(unittest.TestCase):
    def mk(self, name, files):
        p = os.path.join(tempfile.mkdtemp(), name)
        with zipfile.ZipFile(p, "w") as z:
            for k, v in files.items():
                z.writestr(k, v)
        return p

    def test_docx_xlsx_pptx_render_and_escape(self):
        d = self.mk("a.docx", {"word/document.xml": '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>a&lt;script&gt;</w:t></w:r></w:p></w:body></w:document>'})
        h = officeview.render(d)
        self.assertIn("a&lt;script&gt;", h)
        self.assertNotIn("<script>a", h)
        x = self.mk("a.xlsx", {"xl/worksheets/sheet1.xml": '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="B1"><v>5</v></c></row></sheetData></worksheet>'})
        self.assertIn("<td>5</td>", officeview.render(x))
        with self.assertRaises(ValueError):
            officeview.render(self.mk("bad.pptx", {"x": "y"}))


if __name__ == "__main__":
    unittest.main()
