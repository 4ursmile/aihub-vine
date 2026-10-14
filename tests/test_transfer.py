import os
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

    def test_bad_subdir(self):
        with self.assertRaises(TransferError):
            self.t.get({"url": self.src, "subdir": "../x"})


if __name__ == "__main__":
    unittest.main()
