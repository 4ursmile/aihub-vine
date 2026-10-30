"""aihub-ui: a chat is exported to a bundle (json or readable html), shared as a Space page and imported back as a new chat."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "aihub", "builtin_packages", "aihub-ui", "ui")
sys.path.insert(0, UI)
from aihubui import chatshare  # noqa: E402

ROWS = [{"k": "user", "text": "hello <b>there</b>"}, {"k": "msg", "blocks": [{"type": "text", "text": "hi & welcome"}, {"type": "tool", "name": "Read"}]}]


class Bundle(unittest.TestCase):
    def bundle(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "a.py"), "w") as f:
            f.write("print(1)\n")
        with open(os.path.join(d, "b.bin"), "wb") as f:
            f.write(b"\0\1")
        return chatshare.build({"title": "T", "engine": "claude"}, ROWS, d, ["a.py", "b.bin"])

    def test_only_text_files_travel(self):
        self.assertEqual([f["path"] for f in self.bundle()["files"]], ["a.py"])

    def test_json_and_html_carry_the_same_bundle(self):
        b = self.bundle()
        for text in (chatshare.to_json(b), chatshare.to_html(b)):
            p = chatshare.parse(text)
            self.assertEqual(p["rows"], ROWS)
            self.assertEqual(p["title"], "T")

    def test_page_is_escaped_and_cannot_close_the_data_block(self):
        page = chatshare.to_html(chatshare.build({"title": "</script><i>"}, [{"k": "user", "text": "</script><script>alert(1)</script>"}]))
        self.assertNotIn("<script>alert", page)
        self.assertEqual(page.count("<script"), 1)
        self.assertEqual(chatshare.parse(page)["rows"][0]["text"], "</script><script>alert(1)</script>")

    def test_garbage_is_refused_with_a_message(self):
        for bad in ("hello", "{}", json.dumps({"format": "other"}), "<html></html>", json.dumps({"format": "aihub-chat", "version": 99})):
            with self.assertRaises(ValueError):
                chatshare.parse(bad)

    def test_ref_from_link_or_name(self):
        self.assertEqual(chatshare.ref_of("https://hub.example/#/s/alice@design-review?mode=full"), "alice@design-review")
        self.assertEqual(chatshare.ref_of("alice@x1"), "alice@x1")
        self.assertIsNone(chatshare.ref_of("https://hub.example/other"))
        self.assertIsNone(chatshare.ref_of('{"format": "aihub-chat"}'))


if __name__ == "__main__":
    unittest.main()
