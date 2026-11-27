"""aihub-ui Works: the remote fetch nodes (Files from Jupyter, Files from SSH) copy into workingspace and hand on the path only."""
import base64
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODES = os.path.join(ROOT, "aihub", "builtin_packages", "aihub-ui", "ui", "nodes")
sys.path.insert(0, os.path.dirname(NODES))


def run(script, cfg, env):
    p = subprocess.run([sys.executable, os.path.join(NODES, script)], input=json.dumps({"config": cfg}), capture_output=True, text=True, timeout=60, env=env)
    return json.loads(p.stdout.strip().splitlines()[-1])


class Remote(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = dict(os.environ, AIHUB_HOME=self.home)

    def test_catalogue_has_both_nodes_and_the_ssh_kind(self):
        from aihubui import works as W
        cat = W.catalogue(force=True)
        self.assertEqual((cat["jupyter.fetch"]["out"], cat["ssh.fetch"]["out"]), ("path", "path"))
        self.assertEqual(cat["ssh.fetch"]["credential"]["kind"], "ssh")
        self.assertIn("ssh", W.kinds())

    def test_ssh_refuses_paths_that_reach_a_shell(self):
        for bad in ("/data/$(id)", "/x;rm -rf ~", "-oProxyCommand=x", "a`b`", "/x|y"):
            self.assertFalse(run("ssh_fetch.py", {"host": "box", "path": bad}, self.env)["ok"], bad)
        self.assertIn("host", run("ssh_fetch.py", {"host": "-oX", "path": "/a"}, self.env)["error"])

    @unittest.skipIf(os.name == "nt", "fake scp is a shell script")
    def test_ssh_copies_with_scp_and_replaces_the_last_copy(self):
        src = tempfile.mkdtemp()
        os.makedirs(os.path.join(src, "exp"))
        open(os.path.join(src, "exp", "a.csv"), "w").write("1,2")
        bin_ = tempfile.mkdtemp()
        log = os.path.join(bin_, "args")
        with open(os.path.join(bin_, "scp"), "w") as f:      # fake scp: remote "host:/path" -> copy the local /path; records argv and the askpass answer
            f.write('#!/bin/sh\nprintf "%%s\\n" "$@" > %s\n[ -n "$SSH_ASKPASS" ] && "$SSH_ASKPASS" >> %s\nfor a; do last2=$last; last=$a; done\n'
                    'cp -R "${last2#*:}" "$last"\n' % (log, log))
        os.chmod(os.path.join(bin_, "scp"), stat.S_IRWXU)
        env = dict(self.env, PATH=bin_ + os.pathsep + os.environ["PATH"])
        r = run("ssh_fetch.py", {"host": "me@box", "path": os.path.join(src, "exp"), "password": "pw w$x"}, env)
        self.assertTrue(r["ok"], r)
        p = r["data"]["path"]
        self.assertTrue(p.startswith(os.path.join(self.home, "workingspace", "data", "ssh", "me@box")))
        self.assertEqual(open(os.path.join(p, "a.csv")).read(), "1,2")
        self.assertEqual(os.path.basename(p), "exp")
        self.assertEqual(os.listdir(os.path.dirname(p)), ["exp"])
        argv = open(log).read()
        self.assertIn("me@box:" + os.path.join(src, "exp"), argv)
        self.assertIn("pw w$x", argv.splitlines()[-1])                   # askpass answered with the password; it is not in argv
        self.assertNotIn("pw w$x", "\n".join(argv.splitlines()[:-1]))
        os.remove(os.path.join(src, "exp", "a.csv"))
        open(os.path.join(src, "exp", "b.csv"), "w").write("3")
        r = run("ssh_fetch.py", {"host": "box", "path": os.path.join(src, "exp")}, env)
        self.assertTrue(r["ok"], r)
        self.assertEqual(sorted(os.listdir(r["data"]["path"])), ["b.csv"])
        self.assertIn("BatchMode=yes", open(log).read())

    def test_jupyter_copies_a_folder_through_the_ui(self):
        files = {"": [{"name": "data", "path": "data", "type": "directory"}],
                 "data": [{"name": "a.txt", "path": "data/a.txt", "type": "file", "size": 5}, {"name": "b.bin", "path": "data/b.bin", "type": "file", "size": 3},
                          {"name": "n.ipynb", "path": "data/n.ipynb", "type": "notebook", "size": 10}, {"name": "big", "path": "data/big", "type": "file", "size": 9_000_000}]}
        content = {"data": {"type": "directory"}, "data/a.txt": {"type": "file", "format": "text", "content": "hello"},
                   "data/b.bin": {"type": "file", "format": "base64", "content": base64.b64encode(b"\0\1\2").decode()},
                   "data/n.ipynb": {"type": "notebook", "format": "json", "content": {"cells": [], "nbformat": 4}}}
        seen = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append((self.path, self.headers.get("Cookie"), b.get("server")))
                out = {"items": files.get(b.get("path"), [])} if self.path == "/api/jupyter/ls" else content.get(b["path"])
                raw = json.dumps(out or {"error": "not found"}).encode()
                self.send_response(200 if out else 404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(raw)
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        os.makedirs(os.path.join(self.home, "ui"))
        open(os.path.join(self.home, "ui", "url.txt"), "w").write("http://127.0.0.1:%d/?t=tok" % srv.server_port)
        r = run("jupyter_fetch.py", {"server": "My Hub", "path": "data", "max_mb": 1}, self.env)
        self.assertTrue(r["ok"], r)
        p = r["data"]["path"]
        self.assertEqual(sorted(os.listdir(p)), ["a.txt", "b.bin", "n.ipynb"])
        self.assertEqual(open(os.path.join(p, "b.bin"), "rb").read(), b"\0\1\2")
        self.assertEqual(json.load(open(os.path.join(p, "n.ipynb")))["nbformat"], 4)
        self.assertEqual(len(r["data"]["skipped"]), 1)
        self.assertNotIn("hello", r["output"])
        self.assertEqual({(c, s) for _p, c, s in seen}, {("aihub_ui=tok", "My Hub")})
        self.assertIn("not found", run("jupyter_fetch.py", {"server": "My Hub", "path": "nope"}, self.env)["error"])
        self.assertFalse(run("jupyter_fetch.py", {"server": "x", "path": "a/../../b"}, self.env)["ok"])


if __name__ == "__main__":
    unittest.main()
