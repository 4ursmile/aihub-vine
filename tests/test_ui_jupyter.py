"""aihub-ui Notebook: the WebSocket client, the Jupyter client (against a tiny fake server with a fake kernel), parameters, DataFrame code, saved servers."""
import base64
import hashlib
import json
import os
import struct
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "aihub", "builtin_packages", "aihub-ui", "ui")
sys.path.insert(0, UI)
from aihubui import jupyter, store  # noqa: E402
from aihubui.wsclient import WS  # noqa: E402

NB = {"nbformat": 4, "nbformat_minor": 5, "metadata": {"kernelspec": {"name": "python3"}}, "cells": [
    {"cell_type": "code", "metadata": {"tags": ["parameters"]}, "source": ["n = 1\n", "name = 'x'  # who"], "outputs": []},
    {"cell_type": "markdown", "metadata": {}, "source": ["# title"]},
    {"cell_type": "code", "metadata": {}, "source": ["print(n)"], "outputs": []},
    {"cell_type": "code", "metadata": {}, "source": ["boom"], "outputs": []},
    {"cell_type": "code", "metadata": {}, "source": ["never"], "outputs": []}]}
SAVED = {}


def frames(sock_file, write):
    """Server side of a websocket: read masked client frames, hand each text to write()."""
    while True:
        h = sock_file.read(2)
        if len(h) < 2:
            return
        op, n = h[0] & 15, h[1] & 127
        if n == 126:
            n = struct.unpack(">H", sock_file.read(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", sock_file.read(8))[0]
        mask = sock_file.read(4)
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(sock_file.read(n)))
        if op == 8:
            return
        if op == 1:
            write(data.decode())


def frame(text):
    b = text.encode()
    head = bytes([0x81]) + (bytes([len(b)]) if len(b) < 126 else bytes([126]) + struct.pack(">H", len(b)))
    return head + b


class Fake(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def reply(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.headers.get("Authorization") != "token sekret" and "token=sekret" not in self.path:
            return self.reply({"message": "no"}, 403)
        if "/channels" in self.path:
            return self.ws()
        if self.path.startswith("/api/kernelspecs"):
            return self.reply({"default": "python3", "kernelspecs": {"python3": {"spec": {"display_name": "Python 3", "language": "python"}}}})
        if self.path.startswith("/api/sessions"):
            return self.reply([])
        if self.path.startswith("/api/contents/?"):
            return self.reply({"type": "directory", "path": "", "content": [{"name": "n.ipynb", "path": "n.ipynb", "type": "notebook"}]})
        if self.path.startswith("/api/contents/dir"):
            return self.reply({"type": "directory", "path": "dir", "content": [{"name": "b.txt", "path": "dir/b.txt", "type": "file"}, {"name": "a", "path": "dir/a", "type": "directory"}]})
        if self.path.startswith("/api/contents/"):
            return self.reply({"type": "notebook", "content": json.loads(json.dumps(NB)), "name": "n.ipynb"})
        self.reply({"version": "2.0"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        if self.path.startswith("/api/sessions"):
            return self.reply({"id": "s1", "kernel": {"id": "k1", "name": "python3"}}, 201)
        self.reply({"id": "k1", "name": "python3"}, 201)

    def do_DELETE(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_PUT(self):
        n = int(self.headers.get("Content-Length") or 0)
        SAVED[self.path] = json.loads(self.rfile.read(n))
        self.reply({"path": "out.ipynb", "last_modified": "now"})

    def ws(self):
        key = self.headers["Sec-WebSocket-Key"]
        acc = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.wfile.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n" % acc).encode())
        self.wfile.flush()

        def out(mtype, parent, content):
            self.wfile.write(frame(json.dumps({"header": {"msg_type": mtype, "msg_id": "x"}, "parent_header": {"msg_id": parent}, "content": content})))
            self.wfile.flush()

        def on(text):
            m = json.loads(text)
            mid, code = m["header"]["msg_id"], m["content"]["code"]
            out("status", mid, {"execution_state": "busy"})
            expr = m["content"].get("user_expressions") or {}
            if code.strip() == "boom":
                out("error", mid, {"ename": "NameError", "evalue": "boom", "traceback": ["\x1b[31mNameError\x1b[0m: boom"]})
                out("execute_reply", mid, {"status": "error"})
            else:
                if code.startswith("print"):
                    out("stream", mid, {"name": "stdout", "text": "out:" + code + "\n"})
                if "n = 7" in code:
                    out("stream", mid, {"name": "stdout", "text": "PARAMS\n"})
                out("execute_reply", mid, {"status": "ok", "execution_count": 3, "user_expressions": {k: {"status": "ok", "data": {"text/plain": repr('{"total": 5}')}} for k in expr}})
            out("status", mid, {"execution_state": "idle"})
        frames(self.rfile, on)
        self.close_connection = True


class JupyterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = store.ui_dir
        store.ui_dir = lambda *a: os.path.join(self.tmp, *a)

        class Box(dict):
            put = dict.__setitem__

            def remove(self, k):
                self.pop(k, None)

            def get_all(self):
                return dict(self)
        self.box = Box()
        self.rec = jupyter.save(self.box, {"name": "Lab", "kind": "server", "url": self.url, "token": "sekret"})

    def tearDown(self):
        store.ui_dir = self.old

    def test_secret_not_in_file_or_public(self):
        with open(jupyter._path()) as f:
            self.assertNotIn("sekret", f.read())
        self.assertNotIn("sekret", json.dumps(jupyter.servers()))
        self.assertTrue(self.rec["has_secret"])
        self.assertEqual(jupyter.get("lab")["id"], self.rec["id"])

    def test_bad_token_and_files(self):
        self.box.clear()
        with self.assertRaises(jupyter.JError):
            jupyter.about(jupyter.conn(self.box, "Lab"))

    def test_rest(self):
        c = jupyter.conn(self.box, "Lab")
        a = jupyter.about(c)
        self.assertEqual(a["default"], "python3")
        items = jupyter.ls(c, "dir")["items"]
        self.assertEqual([i["name"] for i in items], ["a", "b.txt"])        # folders first

    def test_ws_roundtrip(self):
        c = jupyter.conn(self.box, "Lab")
        ws = WS(c.ws_url("api/kernels/k1/channels"), c.headers())
        ws.send(json.dumps({"header": {"msg_id": "m1"}, "content": {"code": "print(1)"}}))
        got = []
        while True:
            got.append(json.loads(ws.recv())["header"]["msg_type"])
            if got[-1] == "status" and len(got) > 1:
                break
        ws.close()
        self.assertEqual(got, ["status", "stream", "execute_reply", "status"])

    def test_run_notebook(self):
        r = jupyter.run_notebook(self.box, "Lab", "n.ipynb", params={"n": 7}, timeout=30, save_as="out.ipynb")
        self.assertFalse(r["ok"])
        self.assertEqual([x["ok"] for x in r["cells"]], [True, True, False])    # stops at the failing cell, "never" is not run
        self.assertIn("out:print(n)", r["text"])
        self.assertIn("NameError", r["text"])
        self.assertNotIn("\x1b", r["text"])
        self.assertIn("cell 4 failed", r["error"])
        self.assertEqual(r["saved"], "out.ipynb")
        self.assertEqual(SAVED["/api/contents/out.ipynb"]["content"]["cells"][2]["outputs"][0]["output_type"], "stream")

    def test_keep_going_and_result(self):
        r = jupyter.run_notebook(self.box, "Lab", "n.ipynb", keep_going=True, timeout=30, result_var="")
        self.assertEqual(len(r["cells"]), 4)
        self.assertFalse(r["ok"])

    def test_params_and_detect(self):
        self.assertEqual(jupyter.param_code({"n": 3, "s": "a'b", "ok": True}), "n = 3\ns = \"a'b\"\nok = True\n")
        with self.assertRaises(ValueError):
            jupyter.param_code({"bad name": 1})
        self.assertEqual([(p["name"], p["default"], p["type"]) for p in jupyter.detect_params(NB)], [("n", 1, "number"), ("name", "x", "text")])

    def test_frame_code(self):
        code = jupyter.frame_code("df", ["a", "b"], [[1, "x"], [2, None]])
        ns = {}
        try:
            import pandas  # noqa: F401
            exec(code, ns)
            self.assertEqual(list(ns["df"].columns), ["a", "b"])
            self.assertEqual(len(ns["df"]), 2)
        except ImportError:
            exec(code, ns)
            self.assertEqual(ns["df"][1], {"a": 2, "b": None})
        for bad in ("1x", "a b", "__x", ""):
            with self.assertRaises(ValueError):
                jupyter.frame_code(bad, ["a"], [])

    def test_fold(self):
        f = jupyter.Fold()
        f.add({"t": "stream", "c": {"name": "stdout", "text": "a"}})
        f.add({"t": "stream", "c": {"name": "stdout", "text": "b"}})
        f.add({"t": "display_data", "c": {"data": {"text/plain": "x"}, "transient": {"display_id": "d"}}})
        f.add({"t": "update_display_data", "c": {"data": {"text/plain": "y"}, "transient": {"display_id": "d"}}})
        self.assertEqual(len(f.outputs), 2)
        self.assertEqual(f.outputs[0]["text"], "ab")
        self.assertEqual(f.outputs[1]["data"]["text/plain"], "y")
        f.add({"t": "clear_output", "c": {}})
        self.assertEqual(f.outputs, [])

    def test_settings(self):
        s = jupyter.save_settings({"suggest": {"on": False, "model": "opus", "delay": 99999}, "fix": {"model": "bad model!"}})
        self.assertFalse(s["suggest"]["on"])
        self.assertEqual(s["suggest"]["delay"], 5000)
        self.assertEqual(s["fix"]["model"], "")


if __name__ == "__main__":
    unittest.main()
