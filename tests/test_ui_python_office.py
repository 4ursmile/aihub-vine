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
from aihubui import aihub as A, drawioview, officeview, store, works as W  # noqa: E402


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


class DrawioPreview(unittest.TestCase):
    MODEL = ('<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
             '<mxCell id="a" value="A &lt;b&gt;bold&lt;/b&gt;&lt;script&gt;bad()&lt;/script&gt;&lt;img src=x onerror=bad()&gt;" style="rounded=1;html=1;fillColor=#dae8fc;" vertex="1" parent="1">'
             '<mxGeometry x="10" y="10" width="120" height="60" as="geometry"/></mxCell>'
             '<mxCell id="g" value="Lane" style="swimlane;" vertex="1" parent="1"><mxGeometry x="200" y="0" width="200" height="120" as="geometry"/></mxCell>'
             '<mxCell id="b" value="in&lt;group" style="ellipse;" vertex="1" parent="g"><mxGeometry x="40" y="40" width="80" height="40" as="geometry"/></mxCell>'
             '<mxCell id="e" value="go" style="edgeStyle=orthogonalEdgeStyle;endArrow=block;" edge="1" parent="1" source="a" target="b"><mxGeometry relative="1" as="geometry"/></mxCell>'
             '</root></mxGraphModel>')

    def write(self, name, text):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        p = os.path.join(d, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_plain_and_compressed_pages_render_safely(self):
        import base64
        import urllib.parse
        import zlib
        c = zlib.compressobj(9, zlib.DEFLATED, -15)
        packed = base64.b64encode(c.compress(urllib.parse.quote(self.MODEL.replace("Lane", "Second lane")).encode()) + c.flush()).decode()
        h = officeview.render(self.write("d.drawio", '<mxfile><diagram name="One">%s</diagram><diagram name="Two">%s</diagram></mxfile>' % (self.MODEL, packed)))
        self.assertIn("<b>bold</b>", h)
        self.assertNotIn("bad()", h)
        self.assertIn("in&lt;group", h)
        self.assertIn("Second lane", h)
        self.assertIn(">One</button>", h)
        self.assertIn("marker-end=", h)
        self.assertIn('<ellipse cx="280" cy="60"', h)               # child of the swimlane placed at parent (200,0) + (40,40)
        self.assertIn('d="M130,55 L241.27,55"', h)                      # boxes overlap vertically: one straight orthogonal run, clipped at both borders

    def test_bad_files_raise_value_error(self):
        for name, text in (("x.drawio", "<html/>"), ("y.dio", "<mxfile><diagram>!!notbase64</diagram></mxfile>"), ("z.drawio", "not xml"),
                           ("e.drawio", '<!DOCTYPE x [<!ENTITY a "b">]><mxfile/>')):
            with self.assertRaises(ValueError, msg=name):
                drawioview.page(self.write(name, text))


if __name__ == "__main__":
    unittest.main()
