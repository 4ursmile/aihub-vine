"""CLI package management against a real in-process hub: dependency resolve, enable/disable, tree, lock, sync."""
import contextlib
import io
import json
import os
import socket
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.request

import uvicorn

from aihub.cli import api, hooks, installer, main, paths
from aihub.server.config import Settings
from aihub.server.main import create_app


def pkg(name, ver, deps=()):
    toml = '[package]\nname="%s"\nversion="%s"\ntype="skill"\ndescription="d"\n[requires]\npackages=%s\n[[skills]]\nname="%s"\npath="skills/%s"\n' % (
        name, ver, json.dumps(list(deps)), name, name)
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode="w:gz") as t:
        for fn, data in (("aihub.toml", toml.encode()), ("skills/%s/SKILL.md" % name, ("---\nname: %s\ndescription: Use when x\n---\nhi" % name).encode())):
            ti = tarfile.TarInfo(fn)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    return b.getvalue()


def run(*argv):
    b = io.StringIO()
    with contextlib.redirect_stdout(b), contextlib.redirect_stderr(b):
        rc = main.main(list(argv))
    return rc, b.getvalue()


class CliManage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = {k: os.environ.get(k) for k in ("HOME", "AIHUB_HOME", "AIHUB_URL")}
        cls.T = tempfile.mkdtemp()
        os.environ["HOME"], os.environ["AIHUB_HOME"] = cls.T, cls.T + "/.aihub"
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        s = Settings()
        s.data_dir, s.public_url, s.open_registration = cls.T + "/data", "http://127.0.0.1:%d" % port, True
        os.makedirs(s.data_dir, exist_ok=True)
        cls.srv = uvicorn.Server(uvicorn.Config(create_app(s), host="127.0.0.1", port=port, log_level="error"))
        threading.Thread(target=cls.srv.run, daemon=True).start()
        for _ in range(50):
            try:
                urllib.request.urlopen(s.public_url + "/api/v1/healthz")
                break
            except OSError:
                time.sleep(0.1)
        os.environ["AIHUB_URL"] = s.public_url

        def post(path, body=None, tok=None, raw=None):
            rq = urllib.request.Request(s.public_url + path, data=raw if raw is not None else json.dumps(body).encode(), method="POST",
                                        headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + tok} if tok else {})})
            return json.loads(urllib.request.urlopen(rq).read())
        post("/api/v1/auth/register", {"username": "admin", "password": "password123"})
        tok = post("/api/v1/auth/login", {"username": "admin", "password": "password123"})["token"]
        paths.save("credentials.json", {"token": tok, "username": "admin"})
        for n, v, d in [("base", "1.0.0", []), ("base", "1.5.0", []), ("base", "2.0.0", []),
                        ("left", "1.0.0", ["base>=1,<2"]), ("right", "1.0.0", ["base>=1.2,<3"]), ("top", "1.0.0", ["left", "right"]),
                        ("solo", "1.0.0", []), ("bad", "1.0.0", ["base>=3"]),
                        ("clash-a", "1.0.0", ["base<1.2"]), ("clash-b", "1.0.0", ["base>=1.5"]), ("clash", "1.0.0", ["clash-a", "clash-b"])]:
            post("/api/v1/upload", None, tok, pkg(n, v, d))

    @classmethod
    def tearDownClass(cls):
        cls.srv.should_exit = True
        for k, v in cls.env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def st(self): return installer.state()["packages"]
    def skill(self, n): return os.path.exists(self.T + "/.claude/skills/" + n)
    def test_all(self):
        os.makedirs(self.T + "/.claude", exist_ok=True)
        # one-round-trip diamond resolve picks base 1.5.0 (satisfies both)
        pk = installer.plan([{"name": "top", "spec": ""}], {})
        self.assertEqual([p["name"] for p in pk], ["base", "left", "right", "top"])
        self.assertEqual(pk[0]["version"], "1.5.0")
        # conflict reports who requires what
        with self.assertRaises(api.ApiError) as c: installer.plan([{"name": "clash", "spec": ""}], {})
        self.assertIn("base", str(c.exception)); self.assertIn("clash-", str(c.exception))
        with self.assertRaises(api.ApiError) as c: installer.plan([{"name": "bad", "spec": ""}], {})
        self.assertIn("no version of base", str(c.exception))
        # legacy fallback gives the same plan
        self.assertEqual([p["name"] for p in installer._legacy_plan([{"name": "top"}], {})], ["base", "left", "right", "top"])
        # install with deps
        rc, o = run("install", "top", "--yes", "--tool", "claude"); self.assertEqual(rc, 0, o)
        s = self.st(); self.assertEqual(set(s), {"base", "left", "right", "top"})
        self.assertTrue(s["top"]["requested"]); self.assertFalse(s["base"].get("requested"))
        self.assertTrue(self.skill("base") and self.skill("top"))
        rc, o = run("tree"); self.assertIn("top", o); self.assertIn("├─", o)
        rc, o = run("tree", "top", "--remote"); self.assertIn("(installed)", o)
        # disable guarded by dependents
        rc, o = run("disable", "base"); self.assertEqual(rc, 1); self.assertIn("needed by", o)
        rc, o = run("disable", "top"); self.assertEqual(rc, 0, o)
        self.assertFalse(self.skill("top")); self.assertTrue(os.path.isdir(self.st()["top"]["path"]))
        self.assertFalse(self.st()["top"]["enabled"])
        rc, o = run("list"); self.assertIn("disabled", o)
        # hook ignores disabled
        self.assertNotIn("skill:top", hooks._components()); self.assertIn("skill:base", hooks._components())
        # install again == enable (offline files, no re-download)
        rc, o = run("install", "top", "--yes", "--tool", "claude"); self.assertEqual(rc, 0, o); self.assertIn("enabled", o)
        self.assertTrue(self.skill("top")); self.assertTrue(self.st()["top"]["enabled"])
        # enable pulls disabled deps first
        run("disable", "top"); run("disable", "left"); run("disable", "right")
        rc, o = run("disable", "base"); self.assertEqual(rc, 0, o)
        rc, o = run("enable", "top", "--yes", "--tool", "claude"); self.assertEqual(rc, 0, o)
        self.assertTrue(all(self.st()[n]["enabled"] for n in ("base", "left", "right", "top")))
        # update skips disabled
        run("disable", "top"); rc, o = run("update"); self.assertNotIn("top", o)
        run("enable", "--all", "--yes", "--tool", "claude")
        # lock
        lf = self.T + "/aihub.lock"
        rc, o = run("lock", "-f", lf); self.assertEqual(rc, 0, o)
        with open(lf) as f:
            lock = json.load(f)
        self.assertEqual([x["name"] for x in lock["packages"]][-1], "top")
        self.assertEqual(run("lock", "-f", lf, "--check")[0], 0)
        # drift: extra package + lock check fails
        run("install", "solo", "--yes", "--tool", "claude")
        self.assertEqual(run("lock", "-f", lf, "--check")[0], 1)
        rc, o = run("sync", "-f", lf, "--check"); self.assertEqual(rc, 0, o)          # extras alone do not fail check
        rc, o = run("sync", "-f", lf, "--yes", "--prune", "--tool", "claude"); self.assertEqual(rc, 0, o)
        self.assertNotIn("solo", self.st())
        # simulate fresh machine: wipe state and files, sync restores identical set + enabled flags
        for n in list(self.st()): installer.uninstall(n, quiet=True)
        self.assertEqual(self.st(), {})
        rc, o = run("sync", "-f", lf, "--yes", "--tool", "claude"); self.assertEqual(rc, 0, o)
        s = self.st(); self.assertEqual({n: r["version"] for n, r in s.items()}, {x["name"]: x["version"] for x in lock["packages"]})
        self.assertTrue(s["top"]["requested"] and not s["base"]["requested"])
        # tampered lock sha refused
        lock["packages"][0]["sha256"] = "0" * 64
        with open(lf, "w") as f:
            json.dump(lock, f)
        rc, o = run("sync", "-f", lf, "--yes"); self.assertEqual(rc, 1); self.assertIn("sha256", o)
        # uninstall of a dependency warns
        rc, o = run("uninstall", "base", "--force"); self.assertEqual(rc, 0)
        self.assertIn("still needed by", o)
