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
        m = "claude-sonnet-5"
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
        days = rep[1]["by_day"]
        self.assertEqual([x["output"] for x in days], [5, 7])
        self.assertGreater(days[0]["name"], days[1]["name"])
        out, keys = [], iter(["2", "d", "q"])
        ui = mock.Mock(out=out.append, heading=out.append, dim=str, info=out.append, table=lambda h, r: out.append((h[0], r)))
        usage.interactive(rep, ui, read=lambda _: next(keys))
        self.assertIn("Day", [o[0] for o in out if isinstance(o, tuple)])

    def test_user_price_override(self):
        self.claude([self.msg("a", "mystery-1", self.ts, output_tokens=1_000_000)])
        with mock.patch.object(usage.paths, "load", return_value={"mystery": {"input": 1, "output": 7}}):
            self.assertAlmostEqual(usage.report(("claude",), 30, self.now)[0]["total"]["cost"], 7.0)


if __name__ == "__main__":
    unittest.main()
