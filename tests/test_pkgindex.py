import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from aihub.core import pkgindex as P
from aihub.server.config import Settings
from aihub.server.db import init_db
from aihub.server.repos import Repos
from aihub.server.sync import Sync


def pk(n, v="1.0.0", deps=()):
    return {"name": n, "type": "skill", "description": "does " + n, "tags": ["Tag", n], "latest_version": v,
            "requires": {"packages": list(deps), "os": [], "commands": []},
            "repo": {"url": "u", "branch": "main", "subdir": n}, "versions": [{"version": v, "ref": n + "-" + v}], "rating": {"avg": 5}}


class Layout(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.legacy = os.path.join(self.t, "old.json")
        json.dump({"packages": [pk("alpha"), pk("beta", deps=["alpha>=1"]), pk("Gamma_X")]}, open(self.legacy, "w"))
        self.out = os.path.join(self.t, "index")
        P.migrate(self.legacy, self.out)

    def test_files_and_lookup(self):
        root = json.load(open(os.path.join(self.out, "root.json")))
        self.assertEqual((root["format"], root["count"]), (2, 3))
        r = P.Reader(self.out)
        e = r.entry("Gamma.X")                                          # normalised: gamma-x
        self.assertEqual(e["name"], "gamma-x")
        self.assertTrue(os.path.isfile(os.path.join(self.out, P.entry_rel("gamma-x"))))
        self.assertEqual(r.entry("beta")["versions"][0]["requires"], {"packages": ["alpha>=1"]})   # folded into the version, empties dropped
        self.assertNotIn("rating", e)
        self.assertIsNone(r.entry("missing"))

    def test_search_reads_no_entry_files(self):
        r = P.Reader(self.out)
        with mock.patch.object(P.Reader, "entry", side_effect=AssertionError("opened an entry")):
            self.assertEqual([c["name"] for c in r.catalog()], ["alpha", "beta", "gamma-x"])

    def test_catalog_splits_and_is_idempotent(self):
        with mock.patch.object(P, "CATALOG_ROWS", 2):
            P.rebuild(self.out)
            root = json.load(open(os.path.join(self.out, "root.json")))
            self.assertEqual([c["rows"] for c in root["catalog"]], [2, 1])
            self.assertEqual(len(P.Reader(self.out).catalog()), 3)
        P.rebuild(self.out)                                             # back to one chunk: the stale second file is removed
        self.assertEqual(os.listdir(os.path.join(self.out, "catalog")), ["0001.json"])
        files = ("root.json", "catalog/0001.json", P.entry_rel("alpha"))
        before = {f: os.stat(os.path.join(self.out, f)).st_ino for f in files}
        P.migrate(self.legacy, self.out)
        self.assertEqual(before, {f: os.stat(os.path.join(self.out, f)).st_ino for f in files})   # unchanged files are not rewritten

    def test_migrate_removes_dropped_and_open_source_reads_both(self):
        json.dump({"packages": [pk("alpha")]}, open(self.legacy, "w"))
        P.migrate(self.legacy, self.out)
        self.assertEqual([c["name"] for c in P.open_source(self.t, "index").catalog()], ["alpha"])
        self.assertEqual([c["name"] for c in P.open_source(self.t, "index.json").catalog()], ["alpha"])   # old default path name finds the folder
        self.assertIsNone(P.Reader(self.out).entry("beta"))
        old = P.open_source(self.t, "old.json")
        self.assertIsInstance(old, P.Legacy)
        self.assertEqual(old.count, 1)

    def test_add_from_manifest(self):
        proj = os.path.join(self.t, "proj")
        os.makedirs(proj)
        open(os.path.join(proj, "aihub.toml"), "w").write(
            '[package]\nname="delta"\nversion="1.2.0"\ntype="skill"\ndescription="d"\ntags=["x"]\n[requires]\npackages=["alpha"]\n')
        P.add(self.out, proj, "https://x/y.git", ref="delta-1.2.0")
        P.add(self.out, proj, "https://x/y.git", ref="delta-1.2.0")      # same version again: still one entry
        e = P.Reader(self.out).entry("delta")
        self.assertEqual([v["version"] for v in e["versions"]], ["1.2.0"])
        self.assertEqual(e["versions"][0]["requires"]["packages"], ["alpha"])
        self.assertEqual(P.Reader(self.out).count, 4)


class ServerSync(unittest.TestCase):
    def test_only_changed_entries_are_read(self):
        t = tempfile.mkdtemp()
        repo = os.path.join(t, "repo")
        os.makedirs(repo)
        g = lambda *a: subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t"] + list(a), check=True, capture_output=True)
        g("init", "-q", "-b", "main")
        idx = os.path.join(repo, "index")
        for n in ("a", "b"):
            P.write_entry(idx, pk(n))
        P.rebuild(idx)
        g("add", "-A")
        g("commit", "-qm", "1")
        s = Settings.load(data_dir=os.path.join(t, "data"), sync_enabled=False)
        r = Repos(init_db(s))
        r.set_setting("index_url", repo)
        sy = Sync(r, s)
        sy.pull_index()
        self.assertEqual(sorted(r.package_names_visible(None)), ["a", "b"])
        P.write_entry(idx, pk("a", "1.1.0"))
        P.rebuild(idx)
        g("add", "-A")
        g("commit", "-qm", "2")
        opened, real = [], P.Reader.entry
        with mock.patch.object(P.Reader, "entry", lambda self, n: (opened.append(n), real(self, n))[1]):
            sy.pull_index()
        self.assertEqual(opened, ["a"])                                  # b's catalog hash is unchanged: its file is never opened
        self.assertEqual(r.package("a")["latest_version"], "1.1.0")
        with mock.patch.object(P.Reader, "catalog", side_effect=AssertionError("re-read an unchanged commit")):
            sy.pull_index()                                              # same commit as last time: nothing is read at all


if __name__ == "__main__":
    unittest.main()
