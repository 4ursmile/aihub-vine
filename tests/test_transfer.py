import os
import shutil
import tarfile
import subprocess
import tempfile
import unittest

from aihub.server.transfer import Transfer, TransferError
from aihub.cli import gitx


def git(*a, cwd):
    subprocess.check_call(["git", "-c", "user.email=a@b", "-c", "user.name=t"] + list(a), cwd=cwd, stdout=subprocess.DEVNULL)


class TransferTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.src = os.path.join(self.td, "mono")
        for n in ("a", "b"):
            os.makedirs(os.path.join(self.src, n))
            open(os.path.join(self.src, n, "aihub.toml"), "w").write('[package]\nname="%s"\n' % n)
        subprocess.check_call(["git", "init", "-q", "-b", "main"], cwd=self.src)
        git("add", ".", cwd=self.src)
        git("commit", "-qm", "x", cwd=self.src)
        self.t = Transfer(self.td, lambda: 600)

    def test_monorepo_fetched_once(self):
        calls = []
        real = gitx.fetch
        gitx.fetch = lambda *a, **k: (calls.append(a), real(*a, **k))[1]
        try:
            repo = {"url": self.src, "branch": "main"}
            pa, ra = self.t.get(dict(repo, subdir="a"))
            pb, rb = self.t.get(dict(repo, subdir="b"))
            self.t.get(dict(repo, subdir="a"))
        finally:
            gitx.fetch = real
        self.assertEqual(len(calls), 1)
        self.assertEqual(ra, rb)
        self.assertNotEqual(pa, pb)

    def test_hit_reports_cache(self):
        repo = {"url": self.src, "branch": "main"}
        self.assertEqual(self.t.lookup(dict(repo, subdir="a"))[2], "git")
        self.assertEqual(self.t.lookup(dict(repo, subdir="a"))[2], "tar-cache")
        self.assertEqual(self.t.lookup(dict(repo, subdir="b"))[2], "repo-cache")

    def test_bad_subdir(self):
        with self.assertRaises(TransferError):
            self.t.get({"url": self.src, "subdir": "../x"})


@unittest.skipUnless(shutil.which("git-lfs"), "git-lfs not installed")
class LfsTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.src = os.path.join(self.td, "lfs")
        os.makedirs(os.path.join(self.src, "p"))
        open(os.path.join(self.src, "p", "aihub.toml"), "w").write('[package]\nname="p"\n')
        subprocess.check_call(["git", "init", "-q", "-b", "main"], cwd=self.src)
        git("lfs", "install", "--local", cwd=self.src)
        git("lfs", "track", "*.bin", cwd=self.src)
        open(os.path.join(self.src, "p", "big.bin"), "wb").write(b"real-content")
        git("add", ".", cwd=self.src)
        git("commit", "-qm", "x", cwd=self.src)

    def test_real_file_in_archive_and_direct(self):
        t = Transfer(self.td, lambda: 600)
        path, _ = t.get({"url": self.src, "branch": "main", "subdir": "p"})
        with tarfile.open(path) as tf:
            self.assertEqual(tf.extractfile("big.bin").read(), b"real-content")
        d = gitx.fetch(self.src, "main", base=os.path.join(self.td, "direct"), lfs=True, subdir="p")
        self.assertEqual(open(os.path.join(d, "p", "big.bin"), "rb").read(), b"real-content")
        self.assertEqual(gitx.current(self.src, "main", base=os.path.join(self.td, "direct")), d)


@unittest.skipUnless(shutil.which("git-lfs"), "git-lfs not installed")
class PublishLfsScopeTest(unittest.TestCase):
    def test_only_own_package_and_once(self):
        from aihub.cli import publish
        td = tempfile.mkdtemp()
        subprocess.check_call(["git", "init", "-q", "-b", "main"], cwd=td)
        for n in ("a", "b"):
            os.makedirs(os.path.join(td, n))
        with open(os.path.join(td, "a", "big.bin"), "wb") as f:
            f.write(b"\0" * 2048)
        old, publish.LFS_MB = publish.LFS_MB, 0.001
        try:
            self.assertEqual(publish._lfs(os.path.join(td, "b")), [])
            self.assertEqual(publish._lfs(os.path.join(td, "a")), ["big.bin"])
            self.assertEqual(publish._lfs(os.path.join(td, "a")), [])
        finally:
            publish.LFS_MB = old


if __name__ == "__main__":
    unittest.main()
