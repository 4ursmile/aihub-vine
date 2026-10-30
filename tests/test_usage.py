import datetime
import json
import os
import tempfile
import unittest
from unittest import mock

from aihub.cli import usage


def w(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class UsageTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"HOME": self.home, "USERPROFILE": self.home})
        p.start()
        self.addCleanup(p.stop)
        self.now = datetime.datetime.now()
        self.ts = (self.now - datetime.timedelta(minutes=1)).astimezone(datetime.timezone.utc).isoformat()
        self.old = (self.now - datetime.timedelta(days=60)).astimezone(datetime.timezone.utc).isoformat()

    def claude(self, rows):
        w(os.path.join(self.home, ".claude", "projects", "p", "s.jsonl"), rows)

    def msg(self, mid, model, ts, **u):
        return {"type": "assistant", "timestamp": ts, "sessionId": "s1",
                "message": {"id": mid, "model": model, "usage": dict({"input_tokens": 0, "output_tokens": 0}, **u)}}

    def test_claude_dedupes_streamed_message_and_prices_cache(self):
        m = "claude-sonnet-4"
        self.claude([self.msg("a", m, self.ts, input_tokens=1000, output_tokens=10),
                     self.msg("a", m, self.ts, input_tokens=1000, output_tokens=2000, cache_creation_input_tokens=1_000_000,
                              cache_read_input_tokens=2_000_000)])
        rep = usage.report(("claude",), 30, self.now)
        t = rep[0]["total"]
        self.assertEqual((t["requests"], t["sessions"], t["output"], t["cache_write"], t["cache_read"]), (1, 1, 2000, 1_000_000, 2_000_000))
        self.assertAlmostEqual(t["cost"], (1000 * 3 + 2000 * 15 + 1e6 * 3.75 + 2e6 * 0.3) / 1e6)

    def test_windows_and_unknown_model(self):
        self.claude([self.msg("a", "claude-opus-5", self.ts, output_tokens=5), self.msg("b", "claude-opus-5", self.old, output_tokens=5),
                     self.msg("c", "mystery-1", self.ts, output_tokens=5)])
        rep = {p["period"]: p for p in usage.report(("claude",), 30, self.now)}
        self.assertEqual(rep["Today"]["total"]["requests"], 2)
        self.assertEqual(rep["All time"]["total"]["requests"], 3)
        self.assertEqual(rep["Today"]["total"]["unpriced"], 1)

    def test_codex_deltas_from_cumulative_totals(self):
        tc = lambda i, c, o: {"timestamp": self.ts, "type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": i, "cached_input_tokens": c, "output_tokens": o}}}}
        w(os.path.join(self.home, ".codex", "sessions", "2026", "01", "01", "r.jsonl"), [
            {"type": "session_meta", "payload": {"id": "x1"}}, {"type": "turn_context", "payload": {"model": "gpt-5-codex"}},
            tc(100, 40, 10), tc(100, 40, 10), tc(300, 140, 30)])
        t = usage.report(("codex",), 30, self.now)[0]["total"]
        self.assertEqual((t["requests"], t["input"], t["cache_read"], t["output"]), (2, 160, 140, 30))

    def test_by_day_and_interactive(self):
        d2 = (self.now - datetime.timedelta(days=2)).astimezone(datetime.timezone.utc).isoformat()
        self.claude([self.msg("a", "claude-opus-5", self.ts, output_tokens=5), self.msg("b", "claude-opus-5", d2, output_tokens=7)])
        rep = usage.report(("claude",), 30, self.now)
        days = usage.pick(rep)["by_day"]
        self.assertEqual([x["output"] for x in days], [5, 7])
        self.assertGreater(days[0]["name"], days[1]["name"])
        out, keys = [], iter(["2", "d", "q"])
        ui = mock.Mock(heading=out.append, dim=str, bold=str, accent=str, ok=str, warn=str, err=str, COLOR=False, info=out.append, out=out.append, table=lambda h, r: out.append((h[0], r)))
        usage.interactive(rep, ui, read=lambda _: next(keys))
        self.assertIn("Day", [o[0] for o in out if isinstance(o, tuple)])

    def test_regex_is_case_insensitive_and_filters(self):
        self.claude([self.msg("a", "claude-opus-5", self.ts, output_tokens=5), self.msg("b", "claude-sonnet-5", self.ts, output_tokens=7)])
        self.assertEqual(usage.price_for("CLAUDE-OPUS-5"), usage.price_for("claude-opus-5"))
        t = usage.report(("claude",), 30, self.now, model="OPUS")[0]["total"]
        self.assertEqual((t["requests"], t["output"]), (1, 5))

    def test_user_price_matches_variants_and_prefers_the_longest_key(self):
        import json
        from aihub.cli import paths
        paths.ensure()
        with open(paths.p("prices.json"), "w") as f:
            json.dump({"deepseek-v4-flash": {"input": 0.14, "output": 0.28}, "deepseek-v4-flash-vision": {"input": 0.5, "output": 1.0}}, f)
        base = usage.price_for("deepseek-v4-flash")
        self.assertEqual(base[:2], (0.14, 0.28))
        self.assertEqual(usage.price_for("DeepSeek-V4-Flash-Exp")[:2], (0.14, 0.28))          # a longer name falls back to the shorter key
        self.assertEqual(usage.price_for("DeepSeek_V4.Flash")[:2], (0.14, 0.28))              # separators and case do not matter
        self.assertEqual(usage.price_for("DeepSeek-V4-Flash-Vision-Exp")[:2], (0.5, 1.0))     # the most specific key wins
        self.assertIsNone(usage.price_for("deepseek-v3"))

    def test_cache_reuses_parsed_files(self):
        self.claude([self.msg("a", "claude-opus-5", self.ts, output_tokens=5)])
        usage.collect(("claude",))
        with mock.patch.object(usage, "parse_claude", side_effect=AssertionError("reparsed")):
            self.assertEqual(len(usage.collect(("claude",))), 1)

    def test_user_price_override(self):
        self.claude([self.msg("a", "mystery-1", self.ts, output_tokens=1_000_000)])
        with mock.patch.object(usage.paths, "load", return_value={"mystery": {"input": 1, "output": 7}}):
            self.assertAlmostEqual(usage.report(("claude",), 30, self.now)[0]["total"]["cost"], 7.0)

    def test_sessions_titles_context_and_tips(self):
        rows = [{"type": "ai-title", "aiTitle": "Fix the build", "sessionId": "s1"}]
        t0 = self.now - datetime.timedelta(hours=3)
        iso = lambda d: (t0 + datetime.timedelta(seconds=d)).astimezone(datetime.timezone.utc).isoformat()
        for i, (dt, cw, cr) in enumerate([(0, 50000, 0), (60, 1000, 50000), (3000, 60000, 0), (3060, 500, 160000), (3120, 500, 160500), (3180, 500, 161000), (9000, 60000, 0)]):
            m = self.msg("m%d" % i, "claude-sonnet-5", iso(dt), cache_creation_input_tokens=cw, cache_read_input_tokens=cr, output_tokens=10)
            m["cwd"] = "/work/proj"
            rows.append(m)
        self.claude(rows)
        meta = {}
        recs = usage.collect(("claude",), 0.0, None, meta)
        zero = usage.sessions_of([dict(recs[0], input=0, cache_write=0, cache_read=0)], meta)[0]
        self.assertEqual(zero["peak"], 0)
        ss = usage.sessions_of(recs, meta)
        self.assertEqual(len(ss), 1)
        s = ss[0]
        self.assertEqual((s["title"], s["project"], s["requests"], s["peak"], s["last"]), ("Fix the build", "proj", 7, 161500, 60000))
        self.assertEqual(usage.notes_of(s)[2], ["cold"])                       # cache rebuilt after a 50 minute gap
        self.assertEqual(usage.notes_of(s)[6], ["cold", "compact"])            # and after another, with context falling by half
        self.assertEqual([i for i, n in enumerate(usage.notes_of(s)) if "big" in n], [3, 4, 5])
        ids = [t["id"] for t in usage.tips(ss)]
        self.assertIn("cold", ids)
        self.assertIn("big", ids)
        self.assertEqual(len(usage.find_sessions(ss, "build")), 1)
        self.assertEqual(usage.find_sessions(ss, "nope"), [])
        self.assertEqual(usage.sort_sessions(ss, "cost")[0]["session"], "s1")
        self.assertEqual(usage.flags_of(s), {"cold": 2, "compact": 1, "big": 3})    # additive JSON fields: flags, series
        ser = usage.series_of(s)
        self.assertEqual(len(ser), 7)
        self.assertEqual((ser[3][1], ser[6][4]), (160500, ["cold", "compact"]))
        self.assertEqual([t["id"] for t in usage.tips([s])][:1] != [], True)

    def test_codex_session_title_from_first_message(self):
        tc = lambda i: {"timestamp": self.ts, "type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": i, "cached_input_tokens": 0, "output_tokens": 1}}}}
        w(os.path.join(self.home, ".codex", "sessions", "r.jsonl"), [
            {"type": "session_meta", "payload": {"id": "x1", "cwd": "/a/b"}}, {"type": "turn_context", "payload": {"model": "gpt-5-codex"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "## My request for Codex:\nadd   a test"}}, tc(10)])
        meta = {}
        usage.collect(("codex",), 0.0, None, meta)
        self.assertEqual(meta[("codex", "x1")], {"title": "add a test", "cwd": "/a/b"})

    def test_periods_month_and_custom_days(self):
        first = self.now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        early = (first + datetime.timedelta(hours=1)).astimezone(datetime.timezone.utc).isoformat()
        before = (first - datetime.timedelta(days=1)).astimezone(datetime.timezone.utc).isoformat()
        self.claude([self.msg("a", "claude-opus-5", early, output_tokens=5), self.msg("b", "claude-opus-5", before, output_tokens=7)])
        rep = usage.report(("claude",), 9, self.now)
        self.assertEqual([p["key"] for p in rep], ["today", "month", "last", "all"])
        self.assertEqual(rep[2]["period"], "Last 9 days")
        by = {p["key"]: p for p in rep}
        self.assertEqual(by["month"]["total"]["output"], 5 if self.now.day > 1 or self.now.hour >= 1 else 0)
        self.assertEqual(by["all"]["total"]["requests"], 2)
        self.assertEqual([p["key"] for p in usage.report(("claude",), 0, self.now)], ["today", "month", "all"])
        self.assertEqual(usage.pick(usage.report(("claude",), 0, self.now), 0)["key"], "all")

    def test_browser_opens_on_summary_and_n_sets_days(self):
        from aihub.cli import ui as realui
        self.claude([self.msg("a", "claude-opus-5", self.ts, output_tokens=5)])
        recs = usage.collect(("claude",), 0.0, None, {})
        ui = mock.Mock(COLOR=False, U=False, BANG="!", ARROW=">", bold=str, dim=str, accent=str, ok=str, warn=str, err=str, clip=realui.clip, width=lambda: 100)
        b = usage.Browser(usage.report(("claude",), 30, self.now, recs=recs), {}, ui, size=(100, 30),
                          refilter=lambda m, d: usage.report(("claude",), d, model=m, recs=recs, now=self.now))
        self.assertEqual((b.view, b.rep[b.pi]["key"]), ("summary", "last"))
        for k in ("n", "backspace", "backspace", "7", "enter"):
            b.key(k)
        self.assertEqual((b.days, b.rep[b.pi]["period"]), (7, "Last 7 days"))
        for k in ("n", "backspace", "x", "enter"):
            b.key(k)
        self.assertEqual(b.days, 7)                                              # junk is refused, the number stays
        self.assertIn("whole number", b.frame())

    def test_browser_keys_mouse_and_drill(self):
        from aihub.cli import ui as realui
        d2 = (self.now - datetime.timedelta(days=2)).astimezone(datetime.timezone.utc).isoformat()
        self.claude([self.msg("a", "claude-opus-5", self.ts, output_tokens=5), self.msg("b", "claude-opus-5", d2, output_tokens=7)])
        meta = {}
        recs = usage.collect(("claude",), 0.0, None, meta)
        ui = mock.Mock(COLOR=False, U=False, BANG="!", ARROW=">", bold=str, dim=str, accent=str, ok=str, warn=str, err=str, clip=realui.clip, width=lambda: 100)
        b = usage.Browser(usage.report(("claude",), 30, self.now, recs=recs), meta, ui, size=(100, 30))
        f = b.frame()
        self.assertIn("This month", f)                                           # opens on Summary, with the new period tabs
        b.go_view("sessions")
        f = b.frame()
        self.assertIn("Last used", f)
        self.assertEqual(f.count("\n"), 29)                                       # exactly the screen height
        b.key("enter")
        self.assertIsNotNone(b.detail)
        self.assertIn("Context per request", b.frame())
        b.key("esc")
        self.assertIsNone(b.detail)
        b.key("d")                                                               # days view, enter drills into that day's sessions
        b.key("down")
        b.key("enter")
        self.assertEqual((b.view, b.drill[0]), ("sessions", "day"))
        b.key("esc")
        self.assertIsNone(b.drill)
        self.assertTrue(b.key("tab"))
        self.assertEqual(b.view, "days")                                         # views run summary, sessions, days, tips
        b.key("t")
        self.assertIn("Suggestion", b.frame())
        b.go_view("sessions")
        b.frame()
        tab = next(h for h in b.hits if h[3] == ("period", 3))                   # click the "All time" tab
        b.key(("click", tab[1], tab[0]))
        self.assertEqual(b.pi, 3)
        row = next(h for h in b.hits if h[3][0] == "row")
        b.key(("click", 5, row[0]))
        self.assertIsNotNone(b.detail)
        b.key("esc")
        for k in ("/", "o", "p", "u", "s", "enter"):                             # `/` model regex needs no refilter callback to be safe
            b.key(k)
        self.assertEqual(b.model, "opus")
        b.key("f"); b.key("z"); b.key("enter")
        self.assertEqual(b.sessions(), [])
        self.assertIn("nothing here", b.frame())
        b.key("c")
        self.assertIsNone(b.find)
        self.assertFalse(b.key("q"))

    def test_parse_key_sequences(self):
        from aihub.cli import ui as realui
        pk = realui.parse_key
        self.assertEqual(pk(b"\x1b[A"), ("up", 3))
        self.assertEqual(pk(b"\x1b[5~"), ("pgup", 4))
        self.assertEqual(pk(b"\x1b[<0;12;5M"), (("click", 12, 5), 10))
        self.assertEqual(pk(b"\x1b[<64;1;1M")[0], ("wheel", -1))
        self.assertEqual(pk(b"\x1b[<0;12;5m")[0], "")                           # release is ignored
        self.assertIsNone(pk(b"\x1b[<0;12"))                                    # incomplete: wait for more bytes
        self.assertEqual(pk(b"\xc3\xa9"), ("\xe9", 2))


if __name__ == "__main__":
    unittest.main()
