"""CLI package management against a real in-process hub: dependency resolve, enable/disable, tree, lock, sync."""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import time
import unittest

from aihub.cli import api, hooks, installer, main, paths


def git(cwd, *a):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t"] + list(a), cwd=cwd, check=True, capture_output=True)


def run(*argv):
    b = io.StringIO()
    with contextlib.redirect_stdout(b), contextlib.redirect_stderr(b):
        rc = main.main(list(argv))
    return rc, b.getvalue()


class CliManage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = {k: os.environ.get(k) for k in ("HOME", "AIHUB_HOME", "AIHUB_INDEX_URL")}
        cls.T = tempfile.mkdtemp()
        os.environ["HOME"], os.environ["AIHUB_HOME"] = cls.T, cls.T + "/.aihub"
        repo = cls.T + "/repo"
        os.makedirs(repo)
        git(repo, "init", "-q", "-b", "main")
        specs = [("base", "1.0.0", []), ("base", "1.5.0", []), ("base", "2.0.0", []),
                 ("left", "1.0.0", ["base>=1,<2"]), ("right", "1.0.0", ["base>=1.2,<3"]), ("top", "1.0.0", ["left", "right"]),
                 ("solo", "1.0.0", []), ("bad", "1.0.0", ["base>=3"]),
                 ("clash-a", "1.0.0", ["base<1.2"]), ("clash-b", "1.0.0", ["base>=1.5"]), ("clash", "1.0.0", ["clash-a", "clash-b"])]
        pk = {}
        for n, v, d in specs:                                   # one commit per version, tagged v<name>-<ver>
            toml = '[package]\nname="%s"\nversion="%s"\ntype="skill"\ndescription="d"\n[requires]\npackages=%s\n[[skills]]\nname="%s"\npath="skills/%s"\n' % (
                n, v, json.dumps(d), n, n)
            sub = os.path.join(repo, n)
            os.makedirs(sub + "/skills/" + n, exist_ok=True)
            open(sub + "/aihub.toml", "w").write(toml)
            open(sub + "/skills/%s/SKILL.md" % n, "w").write("---\nname: %s\ndescription: Use when x\n---\nhi" % n)
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "%s %s" % (n, v))
            tag = "%s-%s" % (n, v)
            git(repo, "tag", tag)
            e = pk.setdefault(n, {"name": n, "type": "skill", "description": "d", "versions": [],
                                  "repo": {"url": repo, "branch": "main", "subdir": n}})
            e["versions"].append({"version": v, "ref": tag, "requires": {"os": [], "commands": [], "packages": d}})
            e["latest_version"] = v
        cls.index = cls.T + "/index.json"
        json.dump({"packages": list(pk.values())}, open(cls.index, "w"))
        os.environ["AIHUB_INDEX_URL"] = cls.index

    @classmethod
    def tearDownClass(cls):
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
        # a lock pinning a commit that does not exist is refused, nothing is installed
        for n in list(self.st()): installer.uninstall(n, quiet=True)
        lock["packages"][0]["rev"] = "0" * 40
        with open(lf, "w") as f:
            json.dump(lock, f)
        rc, o = run("sync", "-f", lf, "--yes", "--tool", "claude"); self.assertEqual(rc, 1)
        self.assertEqual(self.st(), {})                                            # the refused sync installed nothing
        rc, o = run("install", "top", "--yes", "--tool", "claude"); self.assertEqual(rc, 0, o)
        # uninstall of a dependency warns
        rc, o = run("uninstall", "base", "--force"); self.assertEqual(rc, 0)
        self.assertIn("still needed by", o)
