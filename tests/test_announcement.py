import unittest

from fastapi import HTTPException

from aihub.server import routers, safehtml


class Clean(unittest.TestCase):
    def test_keeps_basic_markup(self):
        self.assertEqual(safehtml.clean("<b>Maintenance</b> at <i>5pm</i><br>"), "<b>Maintenance</b> at <i>5pm</i><br>")
        self.assertEqual(safehtml.clean('see <a href="https://x.io/a?b=1&c=2">docs</a>'),
                         'see <a href="https://x.io/a?b=1&amp;c=2" target="_blank" rel="noopener noreferrer">docs</a>')

    def test_removes_dangerous(self):
        for bad, want in [("<script>alert(1)</script>hi", "hi"), ("<img src=x onerror=alert(1)>ok", "ok"),
                          ('<a href="javascript:alert(1)">x</a>', "<a>x</a>"), ('<a href=" java\nscript:alert(1)">x</a>', "<a>x</a>"),
                          ('<b onclick="x()" style="color:red" class="c">t</b>', "<b>t</b>"), ("<iframe src=//e.com>z</iframe>after", "after"),
                          ("<style>*{display:none}</style>v", "v"), ("a < b & c", "a &lt; b &amp; c")]:
            self.assertEqual(safehtml.clean(bad), want, bad)

    def test_closes_open_tags_and_caps(self):
        self.assertEqual(safehtml.clean("<b>bold <i>both"), "<b>bold <i>both</i></b>")
        self.assertLessEqual(len(safehtml.clean("x" * 5000)), 2000)

    def test_empty(self):
        self.assertEqual(safehtml.clean(""), "")
        self.assertEqual(safehtml.clean(None), "")


class Setting(unittest.TestCase):
    def test_saved_value_is_sanitised(self):
        self.assertEqual(routers._check_text("announcement", "<script>x</script><b>Hi</b>"), "<b>Hi</b>")
        self.assertEqual(routers._check_text("announcement", "   "), "")
        self.assertIn("announcement", routers.TEXT_SETTINGS)


if __name__ == "__main__":
    unittest.main()
