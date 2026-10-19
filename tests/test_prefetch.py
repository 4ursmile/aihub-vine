import os
import subprocess
import tempfile
import unittest
from unittest import mock

from aihub.cli import gitx, registry


def git(*a, cwd):
    subprocess.check_call(["git", "-c", "user.email=a@b", "-c", "user.name=t"] + list(a), cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class PrefetchTest(unittest.TestCase):
    def test_monorepo_is_cloned_once(self):
        t = tempfile.mkdtemp()
        repo = os.path.join(t, "mono")
        for n in ("one", "two", "three"):
            os.makedirs(os.path.join(repo, n))
            open(os.path.join(repo, n, "aihub.toml"), "w").write('[package]\nname="%s"\nversion="1.0.0"\n' % n)
        subprocess.check_call(["git", "init", "-q", "-b", "main", repo])
        git("add", ".", cwd=repo)
        git("commit", "-qm", "x", cwd=repo)
        pkgs = [{"name": n, "version": "1.0.0", "repo": {"url": repo, "branch": "main", "subdir": n}} for n in ("one", "two", "three")]
        calls = []
        real = gitx.fetch

        def counting(*a, **k):
            calls.append(a)
            return real(*a, base=os.path.join(t, "cache"), **{x: y for x, y in k.items() if x != "base"})

        with mock.patch.object(gitx, "fetch", counting), mock.patch.object(gitx, "current", lambda *a, **k: None):
            out = registry.prefetch(pkgs, os.path.join(t, "dl"))
        self.assertEqual(len(calls), 1)
        for n in ("one", "two", "three"):
            folder, err = out[n]
            self.assertIsNone(err)
            self.assertTrue(os.path.isfile(os.path.join(folder, "aihub.toml")))

    def test_one_failure_does_not_stop_others(self):
        t = tempfile.mkdtemp()
        pkgs = [{"name": "x", "version": "1", "repo": {"url": os.path.join(t, "missing"), "branch": "main", "subdir": ""}}]
        out = registry.prefetch(pkgs, os.path.join(t, "dl"))
        self.assertIsNotNone(out["x"][1])


class InstallManyTest(unittest.TestCase):
    def test_one_plan_and_bad_name_skipped(self):
        from aihub.cli import installer
        seen = []

        def fake_plan(roots, have=None):
            names = [x["name"] for x in roots]
            if "bad" in names:
                from aihub.cli import api
                raise api.ApiError("no such package")
            seen.append(names)
            return [{"name": n, "version": "1", "repo": {}} for n in names]

        with mock.patch.object(installer, "plan", fake_plan), \
                mock.patch.object(installer, "state", lambda: {"packages": {}}), \
                mock.patch.object(installer, "installed_versions", lambda st=None: {}), \
                mock.patch.object(installer, "install_plan", lambda pkgs, *a, **k: pkgs), \
                mock.patch.object(installer, "_finish", lambda *a, **k: None):
            failed = installer.install_many([("a", ""), ("bad", ""), ("b", "")])
        self.assertEqual([n for n, _ in failed], ["bad"])
        self.assertEqual(seen[-1], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
