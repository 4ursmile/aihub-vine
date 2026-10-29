import os
import sys
import tempfile
import threading
import time
import unittest

UI = os.path.join(os.path.dirname(__file__), "..", "aihub", "builtin_packages", "aihub-ui", "ui")
sys.path.insert(0, os.path.abspath(UI))

from aihubui import auth, freechat, mcpclient  # noqa: E402

FAKE = r'''
import sys, json, time
for ln in sys.stdin:
    m = json.loads(ln)
    if "id" not in m:
        continue
    if m["method"] == "initialize":
        r = {"protocolVersion": "x", "capabilities": {}}
    elif m["method"] == "tools/list":
        r = {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}
    elif m["method"] == "tools/call":
        if m["params"]["arguments"].get("die"):
            sys.exit(3)
        if m["params"]["arguments"].get("hang"):
            time.sleep(60)
        r = {"content": [{"type": "text", "text": "pid %d" % __import__("os").getpid()}]}
    else:
        r = {}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
'''


class Pool(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.script = os.path.join(self.d, "fake.py")
        with open(self.script, "w") as f:
            f.write(FAKE)
        self.spec = {"command": sys.executable, "args": [self.script], "env": {}, "origin": "t"}
        self._src = mcpclient.sources
        mcpclient.sources = lambda fresh=False: {"fake": self.spec}

    def tearDown(self):
        mcpclient.sources = self._src
        mcpclient.shutdown()

    def box(self, **kw):
        return mcpclient.Toolbox(["fake"], **kw)

    def test_reused_across_toolboxes_and_replaced_when_dead(self):
        a = self.box()
        t1, _ = a.call(list(a.map)[0], {})
        a.close()
        b = self.box()
        t2, _ = b.call(list(b.map)[0], {})
        self.assertEqual(t1, t2)                               # same process
        out, bad = b.call(list(b.map)[0], {"die": 1})
        self.assertTrue(bad)
        t0 = time.time()
        out, bad = b.call(list(b.map)[0], {})
        self.assertTrue(bad)
        self.assertLess(time.time() - t0, 2)                   # dead server fails at once
        b.close()
        c = self.box()
        t3, bad = c.call(list(c.map)[0], {})
        self.assertFalse(bad)
        self.assertNotEqual(t1, t3)                            # replaced

    def test_idle_server_is_swept(self):
        a = self.box()
        s = list(a.servers.values())[0]
        a.close()
        mcpclient._sweep(time.time() + mcpclient.IDLE_TTL + 5)
        s.p.wait(10)
        self.assertIsNotNone(s.p.poll())

    def test_cancel_stops_a_hanging_call_fast(self):
        ev = threading.Event()
        a = self.box(cancel=ev)
        threading.Timer(0.5, ev.set).start()
        t0 = time.time()
        out, bad = a.call(list(a.map)[0], {"hang": 1})
        self.assertTrue(bad)
        self.assertLess(time.time() - t0, 3)

    def test_close_kills_group(self):
        s = mcpclient.Server("x", self.spec)
        s.start()
        s.close()
        self.assertIsNotNone(s.p.poll())

    def test_persist_error_is_a_warning(self):
        def bad(*a):
            raise OSError("disk full")
        a = self.box(persist=bad)
        txt, err = a.call(mcpclient.META, {"action": "disable", "server": "fake", "tool": "echo"})
        self.assertFalse(err)
        self.assertIn("could not be saved", txt)


class Messages(unittest.TestCase):
    def rows(self, n):
        out = []
        for i in range(n):
            out.append({"k": "user", "text": "q%d " % i + "x" * 100})
            out.append({"k": "msg", "blocks": [{"type": "text", "text": "a%d " % i + "y" * 100}]})
        return out

    def test_trim_keeps_alternation_and_adds_note(self):
        m = freechat.messages_from(self.rows(20), budget=1000)
        self.assertEqual(m[0]["role"], "user")
        self.assertIn(freechat.LEFT_OUT, m[0]["content"][0]["text"])
        self.assertLess(len(m), 40)
        for a, b in zip(m, m[1:]):
            self.assertNotEqual(a["role"], b["role"])
        self.assertIn("a19", m[-1]["content"][0]["text"])

    def test_no_trim_under_budget(self):
        m = freechat.messages_from(self.rows(3), budget=10 ** 6)
        self.assertEqual(len(m), 6)
        self.assertNotIn(freechat.LEFT_OUT, m[0]["content"][0]["text"])


class Auth(unittest.TestCase):
    def test_timeout_kills_group(self):
        t0 = time.time()
        with self.assertRaises(Exception):
            auth.run_helper("sleep 30 & sleep 30", 1)
        self.assertLess(time.time() - t0, 8)

    def test_key_cached(self):
        d = tempfile.mkdtemp()
        cnt = os.path.join(d, "n")
        cmd = "echo x >> %s; echo sk-ant-abc" % cnt
        freechat.drop_key()
        self.assertEqual(freechat._helper_key(cmd), "sk-ant-abc")
        self.assertEqual(freechat._helper_key(cmd), "sk-ant-abc")
        with open(cnt) as f:
            self.assertEqual(len(f.read().split()), 1)
        freechat.drop_key()

    def test_tls_context_cached(self):
        e = {"NODE_TLS_REJECT_UNAUTHORIZED": "0"}
        self.assertIs(freechat.tls_context(e), freechat.tls_context(dict(e)))


if __name__ == "__main__":
    unittest.main()
