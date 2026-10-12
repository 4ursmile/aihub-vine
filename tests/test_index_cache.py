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
        registry._cache.clear()                                                        # next command, within the freshness window: no refetch
        self.assertEqual([p["name"] for p in registry.search("")], ["one"])            # the index is still there
        self.assertEqual(registry.get("one")["latest_version"], "1.0.0")


class NewPublishIsSeenQuickly(unittest.TestCase):
    """The CLI used to trust its index checkout for 5 minutes, so a version published a minute ago was not installable yet."""

    def test_moved_branch_is_fetched_unchanged_one_is_not(self):
        from unittest import mock
        from aihub.cli import gitx
        t = tempfile.mkdtemp()
        repo = os.path.join(t, "repo")
        g = lambda *a: subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t"] + list(a), check=True, capture_output=True, text=True)
        def pkg(ver):
            d = os.path.join(t, "src")
            os.makedirs(os.path.join(d, "skills", "one"), exist_ok=True)
            open(os.path.join(d, "aihub.toml"), "w").write('[package]\nname="one"\nversion="%s"\ntype="skill"\ndescription="d"\n[[skills]]\nname="one"\npath="skills/one"\n' % ver)
            open(os.path.join(d, "skills", "one", "SKILL.md"), "w").write("---\nname: one\ndescription: Use when x\n---\nhi")
            P.add(os.path.join(repo, "index"), d, "https://example.invalid/one.git")
            g("add", "-A"); g("commit", "-qm", ver)
        os.makedirs(repo); g("init", "-q", "-b", "main"); pkg("1.0.0")
        env = {k: os.environ.get(k) for k in ("HOME", "AIHUB_HOME", "AIHUB_INDEX_URL", "AIHUB_INDEX_PATH")}
        os.environ.update(HOME=os.path.join(t, "home"), AIHUB_HOME=os.path.join(t, "aihub"), AIHUB_INDEX_URL="file://" + repo, AIHUB_INDEX_PATH="index")
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in env.items()])
        registry._cache.clear()
        self.assertEqual(registry.get("one")["latest_version"], "1.0.0")
        pkg("1.1.0")                                                                   # published right after
        stamp = os.path.join(gitx.cache_dir("file://" + repo, os.path.join(t, "aihub", "repos", "_index")), ".aihub-index-fetched")
        old = os.path.getmtime(stamp) - registry.CHECK - 1
        os.utime(stamp, (old, old))                                                    # the short freshness window has passed
        registry._cache.clear()
        self.assertEqual(registry.get("one")["latest_version"], "1.1.0")               # seen now, not 5 minutes later
        os.utime(stamp, (old, old))
        registry._cache.clear()
        with mock.patch.object(gitx, "fetch", side_effect=AssertionError("fetched although nothing changed")):
            self.assertEqual(registry.get("one")["latest_version"], "1.1.0")           # unchanged remote: one ls-remote, no fetch


if __name__ == "__main__":
    unittest.main()
