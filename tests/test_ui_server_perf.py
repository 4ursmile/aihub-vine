"""aihub-ui server side: list cache (single flight + invalidation), streamed files with Range, files/content conflict, 409 before attachments."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "aihub", "builtin_packages", "aihub-ui", "ui"))
from aihubui import aihub as A, officeview, secretbox, sources, store, term, usagereport  # noqa: E402


REAL_RUN = A.run                      # other test modules leave A.run patched


class ListCache(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, A, "run", A.run)
        A.run = REAL_RUN
        A.invalidate()
        self.calls = []
        self.orig = A._run

        def fake(args, stdin=None, timeout=600):
            self.calls.append(list(args))
            time.sleep(0.15)
            return {"ok": True, "data": []}
        A._run = fake
        self.addCleanup(lambda: setattr(A, "_run", self.orig))
        self.addCleanup(A.invalidate)

    def test_concurrent_calls_run_one_process(self):
        ts = [threading.Thread(target=lambda: A.run(["list"])) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(self.calls.count(["list"]), 1)
        A.run(["list"])
        self.assertEqual(self.calls.count(["list"]), 1)             # cached

    def test_install_and_login_drop_the_cache(self):
        A.run(["list"])
        A.run(["install", "x"])
        A.run(["list"])
        self.assertEqual(self.calls.count(["list"]), 2)
        A.run(["run", "list"])
        A.run(["run", "start", "x"])
        A.run(["run", "list"])
        self.assertEqual(self.calls.count(["run", "list"]), 2)
        A.run(["login", "u"], stdin="p\n")
        A.run(["list"])
        self.assertEqual(self.calls.count(["list"]), 3)

    def test_read_only_commands_keep_it(self):
        A.run(["list"])
        A.run(["run", "logs", "x"])
        A.run(["list"])
        self.assertEqual(self.calls.count(["list"]), 1)

    def test_answer_started_before_an_invalidation_is_not_kept(self):
        t = threading.Thread(target=lambda: A.run(["list"]))
        t.start()
        time.sleep(0.05)
        A.invalidate()
        t.join()
        A.run(["list"])
        self.assertEqual(self.calls.count(["list"]), 2)


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        from http.server import ThreadingHTTPServer
        cls.tmp = tempfile.mkdtemp()
        cls.old = os.environ.get("AIHUB_HOME")
        os.environ["AIHUB_HOME"] = cls.tmp
        server.BOX = secretbox.Box(cls.tmp)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        server.ADDR["port"] = cls.srv.server_address[1]
        cls.port, cls.server = server.ADDR["port"], server
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        if cls.old is None:
            os.environ.pop("AIHUB_HOME", None)
        else:
            os.environ["AIHUB_HOME"] = cls.old

    def req(self, path, method="GET", headers=None, body=None):
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(r) as x:
                return x.status, x.read(), x.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def api(self, method, path, obj=None):
        h = {"Cookie": "aihub_ui=" + self.server.TOKEN, "X-AIHub-UI": "1", "Content-Type": "application/json"}
        code, raw, _ = self.req("/api/" + path, method, h, json.dumps(obj).encode() if obj is not None else None)
        return code, json.loads(raw or b"{}")

    def chat_file(self, name, data):
        s = store.create_session("t")
        d = store.session_dir(s["id"])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "wb") as f:
            f.write(data)
        return s["id"], "/w/%s/%s/%s" % (s["id"], self.server.sig(s["id"]), name)

    def test_range_and_head(self):
        _sid, url = self.chat_file("v.bin", bytes(range(256)) * 4)
        c, body, h = self.req(url)
        self.assertEqual((c, len(body), h["Content-Length"], h["Accept-Ranges"]), (200, 1024, "1024", "bytes"))
        c, body, h = self.req(url, headers={"Range": "bytes=10-19"})
        self.assertEqual((c, body, h["Content-Range"]), (206, bytes(range(10, 20)), "bytes 10-19/1024"))
        c, body, h = self.req(url, headers={"Range": "bytes=-4"})
        self.assertEqual((c, body), (206, bytes([252, 253, 254, 255])))
        c, body, h = self.req(url, headers={"Range": "bytes=1020-"})
        self.assertEqual((c, len(body)), (206, 4))
        c, _b, h = self.req(url, headers={"Range": "bytes=5000-"})
        self.assertEqual((c, h["Content-Range"]), (416, "bytes */1024"))
        c, body, h = self.req(url, "HEAD")
        self.assertEqual((c, body, h["Content-Length"]), (200, b"", "1024"))

    def test_files_content_conflict(self):
        sid, _ = self.chat_file("n.txt", b"one")
        c, d = self.api("GET", "files/content?id=%s&path=n.txt" % sid)
        self.assertEqual(c, 200)
        self.assertIsInstance(d["mtime"], float)
        c, d2 = self.api("PUT", "files/content", {"id": sid, "path": "n.txt", "text": "two", "mtime": d["mtime"]})
        self.assertEqual(c, 200)
        c, d3 = self.api("PUT", "files/content", {"id": sid, "path": "n.txt", "text": "three", "mtime": d["mtime"] - 5})
        self.assertEqual((c, d3["error"]), (409, "changed"))
        self.assertIn("mtime", d3)
        with open(os.path.join(store.session_dir(sid), "n.txt")) as f:
            self.assertEqual(f.read(), "two")
        c, _ = self.api("PUT", "files/content", {"id": sid, "path": "n.txt", "text": "old behaviour"})
        self.assertEqual(c, 200)

    def test_busy_chat_is_refused_before_attachments_and_counting(self):
        from aihubui import chat, hubusage
        s = store.create_session("t")
        used, orig = [], hubusage.use
        hubusage.use = lambda *a, **k: used.append(a)
        oa, chat.active = chat.active, lambda sid: True
        self.addCleanup(lambda: (setattr(hubusage, "use", orig), setattr(chat, "active", oa)))
        c, d = self.api("POST", "chat", {"id": s["id"], "text": "hi", "attachments": [{"name": "a.txt", "data": "aGk="}]})
        self.assertEqual(c, 409)
        self.assertFalse(used)
        self.assertFalse(os.path.exists(os.path.join(store.session_dir(s["id"]), "attachments")))

    def test_bad_attachment_and_content_length(self):
        s = store.create_session("t")
        c, d = self.api("POST", "chat", {"id": s["id"], "text": "hi", "attachments": ["x"]})
        self.assertEqual(c, 400)
        h = {"Cookie": "aihub_ui=" + self.server.TOKEN, "X-AIHub-UI": "1", "Content-Length": "abc"}
        import socket
        so = socket.create_connection(("127.0.0.1", self.port))
        so.sendall(("POST /api/sessions HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Length: abc\r\nCookie: %s\r\n\r\n" % (self.port, h["Cookie"])).encode())
        self.assertIn(b"400", so.recv(2000).split(b"\r\n")[0])
        so.close()

    def test_early_error_closes_the_connection(self):
        import socket
        so = socket.create_connection(("127.0.0.1", self.port))
        so.sendall(("POST /api/sessions HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Length: 5\r\n\r\nhello" % self.port).encode())     # no cookie: 401, body unread
        data = so.recv(4000)
        self.assertIn(b"401", data.split(b"\r\n")[0])
        self.assertIn(b"connection: close", data.lower())
        so.close()

    def test_update_check_waits(self):
        from aihubui import selfupdate
        orig = selfupdate._check
        selfupdate._check = lambda: (time.sleep(0.2), selfupdate.state.update(checked=time.time(), latest="9.9.9"))[1]
        self.addCleanup(lambda: setattr(selfupdate, "_check", orig))
        t0 = time.time()
        c, d = self.api("POST", "update/check", {})
        self.assertEqual(c, 200)
        self.assertGreaterEqual(time.time() - t0, 0.2)
        self.assertFalse(d["running"])
        self.assertEqual(d["update"]["latest"], "9.9.9")


class Helpers(unittest.TestCase):
    def test_usage_days_and_single_flight(self):
        with self.assertRaises(ValueError):
            usagereport.report("abc")
        calls = []
        orig = A.run
        self.addCleanup(setattr, A, "run", orig)

        def fake(args, stdin=None, timeout=600):
            calls.append(args)
            time.sleep(0.2)
            return {"ok": True, "data": []}
        A.run = fake
        usagereport._cache.clear()
        try:
            ts = [threading.Thread(target=lambda: usagereport.report(7)) for _ in range(5)]
            [t.start() for t in ts]
            [t.join() for t in ts]
        finally:
            A.run = orig
        self.assertEqual(len(calls), 1)

    def test_mentions_time_out_into_a_note(self):
        orig, sources.DEADLINE = sources._space, 0.3
        sources._space = lambda ref, folder: time.sleep(2) or "late"
        try:
            t0 = time.time()
            out = sources.resolve("see @space:me@deck and @space:me@other", tempfile.mkdtemp())
        finally:
            sources._space, sources.DEADLINE = orig, 25.0
        self.assertLess(time.time() - t0, 1.5)
        self.assertEqual(out.count("timed out"), 2)

    def test_office_cache_and_sheet_rows(self):
        import zipfile
        d = tempfile.mkdtemp()
        p = os.path.join(d, "b.xlsx")
        rows = "".join('<row r="%d"><c r="A%d" t="inlineStr"><is><t>v%d</t></is></c></row>' % (i, i, i) for i in range(1, officeview.MAX_ROWS + 50))
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>%s</sheetData></worksheet>' % rows)
        a = officeview.render(p)
        self.assertIn("Showing the first", a)
        self.assertIs(officeview.render(p), a)
        time.sleep(0.01)
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>new</t></is></c></row></sheetData></worksheet>')
        os.utime(p, (time.time() + 5, time.time() + 5))
        self.assertIn(">new<", officeview.render(p))

    def test_term_reap_and_kill(self):
        t = term.start(["sleep", "60"], dict(os.environ))
        t.shell = False
        self.assertEqual(term.reap(time.time() + term.IDLE_REAP + 5), 1)
        for _ in range(60):
            if t.p.poll() is not None:
                break
            time.sleep(0.1)
        self.assertIsNotNone(t.p.poll())

    def test_term_popen_failure_closes_master(self):
        before = len(os.listdir("/dev/fd")) if os.path.isdir("/dev/fd") else None
        with self.assertRaises(OSError):
            term.start(["/definitely/not/here"], dict(os.environ))
        if before is not None:
            self.assertLessEqual(len(os.listdir("/dev/fd")), before + 1)

    def test_spacebundle_counts_a_file_once(self):
        from aihubui import spacebundle
        d = tempfile.mkdtemp()
        open(os.path.join(d, "i.png"), "wb").write(b"x" * 1000)
        open(os.path.join(d, "p.html"), "w").write('<img src="i.png"><img src="i.png"><img src="i.png">')
        spacebundle.TOTAL_MAX, old = 1500, spacebundle.TOTAL_MAX
        try:
            html, used, skipped = spacebundle.bundle(os.path.join(d, "p.html"), d)
        finally:
            spacebundle.TOTAL_MAX = old
        self.assertEqual(html.count("data:image/png"), 3)
        self.assertEqual(skipped, [])


if __name__ == "__main__":
    unittest.main()
