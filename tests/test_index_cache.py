import os
import subprocess
import tempfile
import unittest

from aihub.cli import registry
from aihub.core import pkgindex as P


class IndexRepoIsAlsoPackageRepo(unittest.TestCase):
    """Installing a package pinned to an old commit of the index repo must not replace the index checkout."""

    def test_install_checkout_keeps_index(self):
        t = tempfile.mkdtemp()
        repo = os.path.join(t, "repo")
        os.makedirs(os.path.join(repo, "one"))
        g = lambda *a: subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t"] + list(a), check=True, capture_output=True, text=True)
        g("init", "-q", "-b", "main")
        open(os.path.join(repo, "one", "aihub.toml"), "w").write(
            '[package]\nname="one"\nversion="1.0.0"\ntype="skill"\ndescription="d"\n[[skills]]\nname="one"\npath="skills/one"\n')
        os.makedirs(os.path.join(repo, "one", "skills", "one"))
        open(os.path.join(repo, "one", "skills", "one", "SKILL.md"), "w").write("---\nname: one\ndescription: Use when x\n---\nhi")
        g("add", "-A")
        g("commit", "-qm", "package")
        old = g("rev-parse", "HEAD").stdout.strip()
        proj = os.path.join(repo, "one")
        P.add(os.path.join(repo, "index"), proj, "file://" + repo, subdir="one", ref=old)         # the index lives in the same repo, one commit later
        g("add", "-A")
        g("commit", "-qm", "index")
        env = {k: os.environ.get(k) for k in ("HOME", "AIHUB_HOME", "AIHUB_INDEX_URL", "AIHUB_INDEX_PATH")}
        os.environ.update(HOME=os.path.join(t, "home"), AIHUB_HOME=os.path.join(t, "aihub"), AIHUB_INDEX_URL=repo, AIHUB_INDEX_PATH="index")
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in env.items()])
        registry._cache.clear()
        # index source must be git (not a plain directory) to exercise the cache: use a file:// URL
        os.environ["AIHUB_INDEX_URL"] = "file://" + repo
        self.assertEqual([p["name"] for p in registry.search("")], ["one"])
        r = registry.resolve("one")
        self.assertEqual(r["repo"]["ref"], old)
        registry.checkout(r, os.path.join(t, "dest"))                                  # checks out the OLD commit that has no index/
        registry._cache.clear()                                                        # next command, within the 5 minute cache window: no refetch
        self.assertEqual([p["name"] for p in registry.search("")], ["one"])            # the index is still there
        self.assertEqual(registry.get("one")["latest_version"], "1.0.0")


if __name__ == "__main__":
    unittest.main()
