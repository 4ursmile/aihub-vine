"""aihub-ui: Chat / Lite turns are priced from the hub's price table (the API returns no cost; Claude Code turns carry total_cost_usd)."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "aihub", "builtin_packages", "aihub-ui", "ui"))
from aihubui import hubusage, prices, store  # noqa: E402


class Prices(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.environ.get("AIHUB_HOME")
        os.environ["AIHUB_HOME"] = self.tmp
        store._icache.update(stamp=None, data=None)
        store._counts.clear()
        store._scache.clear()
        prices._cache.clear()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if self.old is None:
            os.environ.pop("AIHUB_HOME", None)
        else:
            os.environ["AIHUB_HOME"] = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bundled_table_prices_sonnet_5_5_like_the_cli(self):
        from aihub.cli import usage as cli
        for m in ("aws/anthropic/bedrock-claude-sonnet-5-5", "claude-sonnet-5-5", "us.anthropic.claude-opus-5-5", "claude-haiku-5-5"):
            self.assertEqual(prices.price_for(m), cli.price_for(m), m)
        self.assertIsNone(prices.price_for("unknown"))
        self.assertEqual(prices.cost("unknown", {"input": 10}), 0.0)

    def test_user_override_wins(self):
        with open(os.path.join(self.tmp, "prices.json"), "w") as f:
            json.dump({"sonnet-5-5": {"input": 1, "output": 1}}, f)
        self.assertEqual(prices.cost("aws/anthropic/bedrock-claude-sonnet-5-5", {"input": 1_000_000, "output": 1_000_000}), 2.0)

    def test_free_and_harness_turns_get_a_cost_code_keeps_its_own(self):
        u = {"input": 6, "output": 159, "cache_read": 3270, "cache_write": 3461, "cost": 0}
        with mock.patch.object(hubusage, "kick"):
            for eng in ("free", "harness"):
                s = store.create_session("t")
                store.update_session(s["id"], engine=eng)
                got = store.add_usage(s["id"], u, model="aws/anthropic/bedrock-claude-sonnet-5-5")
                self.assertAlmostEqual(got, (6 * 2 + 159 * 10 + 3461 * 2.5 + 3270 * 0.1) / 1e6, places=6)
                self.assertAlmostEqual(store.get_session(s["id"])["usage"]["cost"], got, places=6)
            s = store.create_session("t")
            self.assertEqual(store.add_usage(s["id"], dict(u, cost=0.5)), 0.5)       # Claude Code: total_cost_usd as given
            self.assertEqual(store.get_session(s["id"])["usage"]["cost"], 0.5)


if __name__ == "__main__":
    unittest.main()
