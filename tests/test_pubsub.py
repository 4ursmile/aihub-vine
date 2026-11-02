import json
import os
import subprocess
import sys
import threading
import time
import unittest

from tests.test_hub_first import admin
from tests.test_robust import client

API = "/api/v1"
NODES = os.path.join(os.path.dirname(__file__), "..", "aihub", "builtin_packages", "aihub-ui", "ui", "nodes")


def user(c, name):
    c.post(API + "/auth/register", json={"username": name, "password": "secret1"})
    return {"Authorization": "Bearer " + c.post(API + "/auth/login", json={"username": name, "password": "secret1"}).json()["token"]}


class Patterns(unittest.TestCase):
    def test_wildcards(self):
        from aihub.server.routers_pubsub import compile_pattern
        cases = {"orders": (["orders"], ["orders.eu"]), "orders.*": (["orders.eu"], ["orders", "orders.eu.paid"]),
                 "orders.>": (["orders.eu", "orders.eu.paid"], ["orders"]), "orders.#": (["orders", "orders.eu.paid"], ["ordersx", "other"]),
                 "*.*.paid": (["orders.eu.paid"], ["orders.paid"]), "#": (["a", "a.b"], []), ">": (["a", "a.b"], [])}
        for pat, (yes, no) in cases.items():
            _, rx = compile_pattern(pat)
            for s in yes:
                self.assertTrue(rx.match(s), (pat, s))
            for s in no:
                self.assertFalse(rx.match(s), (pat, s))
        self.assertEqual(compile_pattern("orders.eu.*.x")[0], "orders.eu")
        self.assertEqual(compile_pattern("*.paid")[0], "")


class PubSub(unittest.TestCase):
    def setUp(self):
        self.c = client()
        self.h = admin(self.c)
        self.bob = user(self.c, "bob")

    def pub(self, subject, data, h=None, **kw):
        return self.c.post(API + "/pubsub/publish", json=dict(subject=subject, data=data, **kw), headers=h or self.h)

    def subjects(self, pattern, h=None, **q):
        r = self.c.get(API + "/pubsub/messages", params=dict(pattern=pattern, **{"from": "start"}, **q), headers=h or {})
        self.assertEqual(r.status_code, 200, r.text)
        return [m["subject"] for m in r.json()["items"]]

    def test_root_needs_permission_sub_topic_needs_owner(self):
        self.assertEqual(self.c.post(API + "/topics", json={"name": "orders"}, headers=self.h).status_code, 200)
        r = self.c.post(API + "/topics", json={"name": "orders.us"}, headers=self.bob)
        self.assertEqual(r.status_code, 403)
        self.assertIn("only its owner", r.json()["detail"])
        self.assertEqual(self.c.post(API + "/topics", json={"name": "bob.events"}, headers=self.bob).status_code, 200)
        self.c.put(API + "/admin/roles/user", json={"permissions": ["publish", "review"]}, headers=self.h)
        r = self.c.post(API + "/topics", json={"name": "other"}, headers=self.bob)
        self.assertEqual((r.status_code, r.json()["detail"]), (403, "missing permission: create_topics"))
        self.assertEqual(self.c.post(API + "/topics", json={"name": "bob.events.more"}, headers=self.bob).status_code, 200)   # owner of the parent
        self.assertEqual(self.c.post(API + "/topics", json={"name": "facets"}, headers=self.h).status_code, 400)

    def test_space_creation_has_its_own_permission(self):
        self.c.put(API + "/admin/roles/user", json={"permissions": ["publish", "review"]}, headers=self.h)
        r = self.c.post(API + "/spaces", params={"filename": "a.md"}, content=b"# hi", headers=self.bob)
        self.assertEqual((r.status_code, r.json()["detail"]), (403, "missing permission: create_spaces"))
        self.c.put(API + "/admin/roles/user", json={"permissions": ["create_spaces"]}, headers=self.h)
        self.assertEqual(self.c.post(API + "/spaces", params={"filename": "a.md"}, content=b"# hi", headers=self.bob).status_code, 200)
        keys = [p["key"] for p in self.c.get(API + "/admin/roles", headers=self.h).json()["permissions"]]
        self.assertIn("create_topics", keys)
        self.assertIn("create_spaces", keys)

    def test_private_sub_topic_shares_and_wildcards(self):
        self.c.post(API + "/topics", json={"name": "orders"}, headers=self.h)
        self.c.post(API + "/topics", json={"name": "orders.eu", "visibility": "private"}, headers=self.h)
        self.assertEqual(self.pub("orders.us.paid", {"n": 1}).status_code, 200)
        self.pub("orders.eu.paid", "secret")
        self.pub("orders", "root")
        self.assertEqual(self.subjects("orders.#", self.h), ["orders.us.paid", "orders.eu.paid", "orders"])
        self.assertEqual(self.subjects("orders.#"), ["orders.us.paid", "orders"])                    # signed out: public only
        self.assertEqual(self.subjects("*.*.paid", self.bob), ["orders.us.paid"])
        self.assertEqual(self.pub("orders.x", "no", self.bob).status_code, 403)
        self.assertEqual(self.c.get(API + "/topics/orders.eu", headers=self.bob).status_code, 404)
        self.c.put(API + "/topics/orders/access", json={"type": "user", "name": "bob", "access": "both"}, headers=self.h)     # flows down
        self.assertEqual(self.subjects("orders.eu.>", self.bob), ["orders.eu.paid"])
        self.assertEqual(self.pub("orders.eu.new", "yes", self.bob).status_code, 200)
        self.assertEqual(self.c.get(API + "/topics/orders.eu/access", headers=self.h).json()["inherited"][0]["name"], "bob")

    def test_regex_is_client_side_tail_and_validation(self):
        from aihub.cli.pubsub import matcher
        self.c.post(API + "/topics", json={"name": "logs"}, headers=self.h)
        self.c.post(API + "/pubsub/publish", json={"messages": [{"subject": "logs.app", "data": t} for t in ("ok 1", "error 2", "ok 3", "error 4")]
                                                   + [{"subject": "logs.app", "data": {"amount": 1200}}]}, headers=self.h)
        r = self.c.get(API + "/pubsub/messages", params={"pattern": "logs.#", "from": "start", "match": "^error"})
        self.assertEqual(r.status_code, 400)                                       # the hub never runs a regex on messages
        self.assertIn("filter on the client", r.json()["detail"])
        items = self.c.get(API + "/pubsub/messages", params={"pattern": "logs.#", "from": "start"}).json()["items"]
        self.assertEqual([m["data"] for m in items if matcher("^error")(m)], ["error 2", "error 4"])
        self.assertEqual([m["data"] for m in items if matcher('"amount": [0-9]{4,}')(m)], [{"amount": 1200}])
        self.assertIsNone(matcher(""))
        with self.assertRaises(ValueError):
            matcher("(")
        r = self.c.get(API + "/pubsub/messages", params={"pattern": "logs.#", "tail": 2}).json()
        self.assertEqual([m["data"] for m in r["items"]], ["error 4", {"amount": 1200}])
        self.assertEqual(self.c.get(API + "/pubsub/messages", params={"pattern": "logs.>.x"}).status_code, 400)
        self.assertEqual(self.pub("Bad Name", "x").status_code, 400)
        self.assertEqual(self.pub("nothing.here", "x").status_code, 404)
        r = self.c.post(API + "/pubsub/publish", content=b'{"subject": "logs.app", "data": {"x": NaN}}', headers=dict(self.h, **{"Content-Type": "application/json"}))
        self.assertEqual(r.status_code, 400)

    def test_long_poll_wakes_on_publish(self):
        self.c.post(API + "/topics", json={"name": "jobs"}, headers=self.h)

        def later():
            time.sleep(0.5)
            self.pub("jobs.new", {"id": 1})
        threading.Thread(target=later).start()
        t = time.time()
        r = self.c.get(API + "/pubsub/messages", params={"pattern": "jobs.#", "wait": 10}, headers=self.h).json()
        self.assertLess(time.time() - t, 5)
        self.assertEqual(r["items"][0]["data"], {"id": 1})

    def test_durable_cursor_and_ack(self):
        self.c.post(API + "/topics", json={"name": "q"}, headers=self.h)
        for i in range(5):
            self.pub("q.x", i)
        g = lambda **p: [m["data"] for m in self.c.get(API + "/pubsub/messages", params=dict(pattern="q.#", cursor="w", limit=2, **p), headers=self.h).json()["items"]]
        self.assertEqual(g(**{"from": "start"}), [0, 1])
        self.assertEqual(g(), [2, 3])                                   # auto commit
        self.assertEqual(g(commit="0"), [4])
        self.assertEqual(g(commit="0"), [4])                            # not acknowledged: delivered again
        ids = self.c.get(API + "/pubsub/messages", params={"pattern": "q.#", "tail": 1}).json()["items"]
        self.assertEqual(g(commit="0", ack=ids[0]["id"]), [])
        self.assertEqual(self.c.get(API + "/pubsub/cursors", headers=self.h).json()["items"][0]["name"], "w")

    def test_retention_keeps_the_newest(self):
        self.c.post(API + "/topics", json={"name": "small", "max_messages": 10}, headers=self.h)
        self.c.post(API + "/pubsub/publish", json={"messages": [{"subject": "small", "data": i} for i in range(25)]}, headers=self.h)
        self.c.app.state.pubsub.maintain()                                         # retention runs in the engine thread
        r = self.c.get(API + "/pubsub/messages", params={"pattern": "small", "from": "start", "limit": 100}).json()
        self.assertEqual([m["data"] for m in r["items"]], list(range(15, 25)))

    def test_stats_list_delete(self):
        self.c.post(API + "/topics", json={"name": "s", "title": "Stats"}, headers=self.h)
        self.c.post(API + "/topics", json={"name": "s.a"}, headers=self.h)
        self.pub("s.a.b", 1)
        self.pub("s", 2)
        self.subjects("s.#", self.h)
        st = self.c.get(API + "/topics/s/stats", headers=self.h).json()
        self.assertEqual((st["totals"]["pub"], st["totals"]["deliv"], st["stored"]["messages"], st["topics"]), (2, 2, 2, 2))
        self.assertEqual(len(st["series"]), 7)
        self.assertEqual({c["role"] for c in st["clients"]}, {"produce", "consume"})
        self.assertEqual(self.c.get(API + "/topics/s/stats").json()["clients"][0]["username"], "")      # names for the owner only
        lst = self.c.get(API + "/topics", headers=self.h).json()
        self.assertEqual([t["name"] for t in lst["items"]], ["s", "s.a"])
        self.assertEqual(lst["items"][0]["pub_24h"], 1)
        self.assertEqual(self.c.get(API + "/topics/s", headers=self.h).json()["children"][0]["name"], "s.a")
        self.assertEqual(self.c.delete(API + "/topics/s", headers=self.bob).status_code, 403)
        self.assertEqual(self.c.delete(API + "/topics/s", headers=self.h).json()["removed"], 2)
        self.assertEqual(self.c.get(API + "/topics", headers=self.h).json()["total"], 0)


    def test_live_reads_come_from_memory(self):
        self.c.post(API + "/topics", json={"name": "fast"}, headers=self.h)
        self.c.post(API + "/topics", json={"name": "fast.secret", "visibility": "private"}, headers=self.h)
        nxt = self.c.get(API + "/pubsub/messages", params={"pattern": "fast.#"}).json()["next"]
        eng = self.c.app.state.pubsub
        eng.step()                                                                 # the ring has its start position (the thread may be first)
        self.pub("fast.a", 1)
        self.pub("fast.secret.b", 2)
        self.pub("fast.c", {"k": "ü"}, headers={"h": "v"})
        eng.step()
        self.assertIsNotNone(eng.backlog(nxt))                                    # covered by the ring: no database scan
        scans = []
        real = self.c.app.state.repos.topic_scan
        self.c.app.state.repos.topic_scan = lambda *a, **k: scans.append(a) or real(*a, **k)
        try:
            r = self.c.get(API + "/pubsub/messages", params={"pattern": "fast.#", "after": nxt}).json()
        finally:
            self.c.app.state.repos.topic_scan = real
        self.assertEqual(scans, [])
        self.assertEqual([(m["subject"], m["data"]) for m in r["items"]], [("fast.a", 1), ("fast.c", {"k": "ü"})])    # private one hidden
        self.assertEqual(r["items"][1]["headers"], {"h": "v"})
        self.assertEqual(r["next"], eng.head)
        r = self.c.get(API + "/pubsub/messages", params={"pattern": "fast.#", "after": nxt}, headers=self.h).json()
        self.assertEqual(len(r["items"]), 3)
        self.assertEqual(r["items"][0].keys(), {"id", "subject", "ts", "key", "type", "data", "headers", "size", "by"})
        self.assertTrue(r["items"][0]["by"])

    def test_group_commit_under_concurrency(self):
        self.c.post(API + "/topics", json={"name": "load"}, headers=self.h)
        ids, errs = [], []

        def go(i):
            for j in range(10):
                r = self.pub("load.w%d" % i, j)
                (ids.extend(r.json()["ids"]) if r.status_code == 200 else errs.append(r.text))
        ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        self.assertEqual(len(set(ids)), 80)
        self.assertEqual(self.c.get(API + "/topics/load", headers=self.h).json()["msgs"], 80)
        got = self.c.get(API + "/pubsub/messages", params={"pattern": "load.#", "from": "start", "limit": 1000}).json()["items"]
        self.assertEqual(sorted(m["id"] for m in got), sorted(ids))

    def test_deleting_the_newest_does_not_reuse_ids(self):
        self.c.post(API + "/topics", json={"name": "keep"}, headers=self.h)
        self.c.post(API + "/topics", json={"name": "gone"}, headers=self.h)
        self.pub("keep", 1)
        top = self.pub("gone", 2).json()["ids"][0]
        nxt = self.c.get(API + "/pubsub/messages", params={"pattern": "#"}).json()["next"]
        self.c.delete(API + "/topics/gone", headers=self.h)
        new = self.pub("keep", 3).json()["ids"][0]
        self.assertGreater(new, top)
        r = self.c.get(API + "/pubsub/messages", params={"pattern": "#", "after": nxt}, headers=self.h).json()
        self.assertEqual([m["data"] for m in r["items"]], [3])


class Engine(unittest.TestCase):
    def test_gap_is_held_then_skipped(self):
        from aihub.server import pubsub_feed as F
        rows = []

        class Repos:
            def topic_head(self):
                return 0

            def topic_scan(self, ids, prefix, after, limit, desc=False):
                return [r for r in rows if r["id"] > after][:limit]
        row = lambda i: {"id": i, "topic_id": 1, "subject": "a", "ts": 0, "mkey": "", "ctype": "text", "body": "m%d" % i, "size": 2, "headers": "", "username": ""}
        e = F.Engine(Repos(), lambda t: (1, 1))
        e._pull()                                                                  # load the head
        rows += [row(1), row(3)]                                                   # 2 not committed yet
        e._pull()
        self.assertEqual(e.head, 1)
        rows.insert(1, row(2))
        e._pull()
        self.assertEqual(e.ids, [1, 2, 3])
        rows.append(row(5))                                                        # 4 never comes (rolled back)
        e._pull()
        self.assertEqual(e.head, 3)
        e._gap = (4, e._gap[1] - F.GAP_HOLD - 1)
        e._pull()
        self.assertEqual(e.head, 5)
        out, per, nxt = e.read(0, 10, lambda s, t: True)
        self.assertEqual([json.loads(p)["data"] for p in out], ["m1", "m2", "m3", "m5"])
        self.assertEqual((per, nxt), ({1: 4}, 5))
        out, _, nxt = e.read(1, 2, lambda s, t: True)
        self.assertEqual((len(out), nxt), (2, 3))
        self.assertIsNone(e.read(-1, 1, lambda s, t: True))


class UiNodes(unittest.TestCase):
    def test_publish_node_reports_missing_login(self):
        env = dict(os.environ, AIHUB_HOME=os.path.join(os.path.dirname(__file__), "__nohome__"), AIHUB_URL="")
        p = subprocess.run([sys.executable, os.path.join(NODES, "pubsub_pub.py")], input=json.dumps({"config": {"subject": "a.b", "data": "x"}}),
                           capture_output=True, text=True, env=env, timeout=30)
        out = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertFalse(out["ok"])
        self.assertIn("no hub configured", out["error"])

    def test_nodes_are_in_the_catalogue(self):
        sys.path.insert(0, os.path.join(NODES, ".."))
        from aihubui import works
        self.assertEqual(works.BUILTIN["start.pubsub"]["kind"], "trigger")
        self.assertIn("subject", works.BUILTIN["pubsub.publish"]["templated"])
        self.assertIn("start.pubsub", works.BUILTIN_SCHEMA)


if __name__ == "__main__":
    unittest.main()
