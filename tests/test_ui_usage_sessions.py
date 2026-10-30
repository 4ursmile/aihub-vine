"""aihub-ui Usage page, By session view: the report layer pages/sorts/filters the CLI's session list (fields are additive)."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "aihub", "builtin_packages", "aihub-ui", "ui"))
from aihubui import aihub as A, usagereport  # noqa: E402


def row(i, peak, cost, end, flags=None, title=None):
    return {"tool": "claude", "session": "sess-%04d" % i, "id": "%04d" % i, "title": title or "chat %d" % i, "cwd": "/w/p%d" % (i % 3), "project": "p%d" % (i % 3),
            "start": end - 600, "end": end, "requests": i + 1, "input": 10, "output": 20, "cache_write": 30, "cache_read": 40, "cost": cost, "unpriced": 0,
            "peak": peak, "last": peak // 2, "cache_hit": 0.5, "model": "m", "models": {"claude-sonnet-5": {"requests": 1, "cost": cost}},
            **({"flags": flags} if flags else {}), "notes_ignored": 1}


class SessionsView(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.rows = [row(i, peak=i * 1000, cost=float(i % 7), end=1000 + i, flags={"cold": 1, "compact": 0, "big": int(i * 1000 > 150000)} if i % 2 else None,
                         title="needle" if i == 5 else None) for i in range(300)]
        orig = A.run
        self.addCleanup(setattr, A, "run", orig)
        self.addCleanup(usagereport._scache.clear)
        usagereport._scache.clear()

        def fake(args, stdin=None, timeout=600):
            self.calls.append(args)
            if "--sessions" in args:
                return {"ok": True, "data": {"sessions": self.rows}}
            s = dict(self.rows[7], series=[[1, 1000, 5, 0.1, []], [2, 160000, 6, 0.2, ["big"]]], tips=[{"id": "big", "level": "info", "title": "t", "detail": "d", "amount": 1.0}])
            return {"ok": True, "data": {"sessions": [s]}}
        A.run = fake

    def test_pages_sorts_filters_and_defaults_missing_flags(self):
        d = usagereport.sessions(30, sort="ctx", limit=50)
        self.assertEqual((d["total"], len(d["rows"]), d["max_peak"]), (300, 50, 299000))
        self.assertEqual(d["rows"][0]["peak"], 299000)
        self.assertEqual(d["rows"][0]["flags"], {"cold": 1, "compact": 0, "big": 1})
        self.assertEqual(usagereport.sessions(30, q="sess-0000")["rows"][0]["flags"], {"cold": 0, "compact": 0, "big": 0})
        self.assertNotIn("notes_ignored", d["rows"][0])
        self.assertEqual([r["peak"] for r in usagereport.sessions(30, sort="ctx", offset=50, limit=2)["rows"]], [249000, 248000])
        self.assertEqual(usagereport.sessions(30, sort="recent", limit=1)["rows"][0]["end"], 1299)
        self.assertEqual(usagereport.sessions(30, sort="cost", limit=1)["rows"][0]["cost"], 6.0)
        q = usagereport.sessions(30, q="NEEDLE")
        self.assertEqual((q["total"], q["rows"][0]["id"]), (1, "0005"))
        self.assertEqual(usagereport.sessions(30, q="claude-sonnet")["total"], 300)
        self.assertEqual(len([c for c in self.calls if "--sessions" in c]), 1)      # one CLI run, cached for every page / sort / search
        with self.assertRaises(ValueError):
            usagereport.sessions("x")
        with self.assertRaises(ValueError):
            usagereport.sessions(30, offset="x")

    def test_detail_has_series_and_tips(self):
        s = usagereport.session("sess-0007", 30)
        self.assertEqual(s["series"][1], [2, 160000, 6, 0.2, ["big"]])
        self.assertEqual(s["tips"][0]["id"], "big")
        self.assertIn("--session", self.calls[-1])
        with self.assertRaises(ValueError):
            usagereport.session("")


if __name__ == "__main__":
    unittest.main()
