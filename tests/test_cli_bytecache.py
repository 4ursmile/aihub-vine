"""CLI bytecode cache (aihub/cli/entry.py): a CLI started from aihub.pyz keeps compiled modules in ~/.aihub/cache/pyc and behaves exactly
as plain zipimport does (same output, same module __file__/__path__/spec, importlib.resources, updater.target). Bad cache files are
ignored, a new pyz drops the folders of the old one, and source / pip runs are left alone."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

from aihub.cli import entry
from aihub.core import zipapp

DUMP = textwrap.dedent("""
    import atexit, json, runpy, sys
    out, pyz = sys.argv[1], sys.argv[2]
    def dump():
        import importlib.resources as r
        from aihub.cli import updater
        mods = {}
        for n, m in sorted(sys.modules.items()):
            if n == "aihub" or n.startswith("aihub."):
                s = m.__spec__
                mods[n] = [m.__file__, list(getattr(m, "__path__", None) or []), m.__package__, s.origin, s.parent, s.has_location,
                           list(s.submodule_search_locations or [])]
        doc = {"mods": mods, "target": updater.target(), "res": len(r.files("aihub.core").joinpath("defaults.json").read_bytes()),
               "loaders": sorted({type(m.__spec__.loader).__name__ for n, m in sys.modules.items() if n.startswith("aihub.")})}
        with open(out, "w") as f:
            json.dump(doc, f)
    atexit.register(dump)
    sys.argv = [pyz, "--json", "list"]
    runpy.run_path(pyz, run_name="__main__")
""")


class ByteCache(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.pyz = zipapp.build(os.path.join(cls.dir, "aihub.pyz"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.cache = os.path.join(self.home, "cache", "pyc")

    def env(self, **kw):
        e = dict(os.environ, HOME=self.home, USERPROFILE=self.home, AIHUB_HOME=self.home, AIHUB_JSON="1", AIHUB_NO_UPDATE="1")
        e.pop("AIHUB_NO_PYC_CACHE", None)
        e.update(kw)
        return e

    def cli(self, *args, pyz=None, **env):
        p = subprocess.run([sys.executable, pyz or self.pyz] + list(args), capture_output=True, text=True, env=self.env(**env), timeout=120)
        return p.returncode, p.stdout

    def norm(self, out):
        doc = json.loads(out)
        doc.get("context", {}).pop("client_id", None)
        return doc

    def folders(self):
        return sorted(os.listdir(self.cache)) if os.path.isdir(self.cache) else []

    def test_cached_run_answers_like_a_plain_one(self):
        plain = self.cli("--json", "list", AIHUB_NO_PYC_CACHE="1")
        self.assertEqual(self.folders(), [])                                   # switched off: nothing written
        first = self.cli("--json", "list")
        [folder] = self.folders()
        self.assertTrue(folder.endswith(sys.implementation.cache_tag))
        self.assertIn("aihub.cli.main.pyc", os.listdir(os.path.join(self.cache, folder)))
        second = self.cli("--json", "list")
        self.assertEqual(plain[0], 0)
        self.assertEqual([c for c, _ in (first, second)], [0, 0])
        self.assertEqual(self.norm(first[1]), self.norm(plain[1]))
        self.assertEqual(self.norm(second[1]), self.norm(plain[1]))
        self.assertEqual(self.cli("--version"), self.cli("--version", AIHUB_NO_PYC_CACHE="1"))

    def test_modules_look_the_same_as_under_zipimport(self):
        script = os.path.join(self.home, "dump.py")
        with open(script, "w") as f:
            f.write(DUMP)
        docs = []
        for extra in ({"AIHUB_NO_PYC_CACHE": "1"}, {}, {}):                    # plain, cache being filled, cache hit
            out = os.path.join(self.home, "dump%d.json" % len(docs))
            subprocess.run([sys.executable, script, out, self.pyz], capture_output=True, env=self.env(**extra), timeout=120, check=True)
            with open(out) as f:
                docs.append(json.load(f))
        plain, filling, hit = docs
        self.assertEqual(plain["loaders"], ["zipimporter"])
        self.assertEqual(hit["loaders"], ["_CachedLoader", "zipimporter"])     # aihub, aihub.cli, aihub.cli.entry load before the cache
        for d in (filling, hit):
            self.assertEqual(d["mods"], plain["mods"])
            self.assertEqual(d["target"], self.pyz)
            self.assertEqual(d["res"], plain["res"])
        self.assertIn("aihub.core.defaults", hit["mods"])

    def test_bad_cache_files_are_ignored_and_replaced(self):
        self.cli("--version")
        [folder] = self.folders()
        f = os.path.join(self.cache, folder, "aihub.cli.main.pyc")
        for junk in (b"", b"garbage", b"\x00\x00\x00\x00" + b"x" * 50):
            with open(f, "wb") as fh:
                fh.write(junk)
            self.assertEqual(self.cli("--version"), self.cli("--version", AIHUB_NO_PYC_CACHE="1"))
        with open(f, "rb") as fh:
            self.assertGreater(len(fh.read()), 100)                            # written again

    def test_an_unwritable_cache_never_fails_the_command(self):
        os.makedirs(os.path.join(self.home, "cache"))
        with open(self.cache, "w") as fh:                                      # a FILE where the folder should be
            fh.write("x")
        self.assertEqual(self.cli("--version"), self.cli("--version", AIHUB_NO_PYC_CACHE="1"))

    def test_a_new_pyz_drops_the_old_cache(self):
        other = os.path.join(self.home, "bin", "aihub.pyz")
        os.makedirs(os.path.dirname(other))
        shutil.copy(self.pyz, other)
        self.cli("--version", pyz=other)
        self.cli("--version")                                                  # another archive: its own folder, kept
        self.assertEqual(len(self.folders()), 2)
        mine = [f for f in self.folders() if os.path.isfile(os.path.join(self.cache, f, "aihub.cli.main.pyc"))]
        self.assertEqual(len(mine), 2)
        st = os.stat(other)
        os.utime(other, ns=(st.st_atime_ns, st.st_mtime_ns + 5 * 10 ** 9))      # `aihub upgrade` swapped it
        before = set(self.folders())
        self.cli("--version", pyz=other)
        after = set(self.folders())
        self.assertEqual(len(after), 2)
        self.assertEqual(len(after - before), 1)

    def test_source_runs_are_left_alone(self):
        before = list(sys.meta_path)
        entry._bytecode_cache()
        self.assertEqual(sys.meta_path, before)


if __name__ == "__main__":
    unittest.main()
