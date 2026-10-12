import argparse
import os
import subprocess
import tempfile
import unittest

from aihub.cli import main as cli, publish as pub, ui


def git(*a, cwd):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Workspace(unittest.TestCase):
    def setUp(self):
        t = tempfile.mkdtemp()
        self.bare, self.w = os.path.join(t, "r.git"), os.path.join(t, "work")
        os.makedirs(self.bare); os.makedirs(self.w)
        git("init", "-q", "--bare", "-b", "main", cwd=self.bare)
        git("init", "-q", "-b", "main", cwd=self.w)
        git("remote", "add", "origin", self.bare, cwd=self.w)
        git("config", "user.email", "a@b.c", cwd=self.w); git("config", "user.name", "t", cwd=self.w)
        self._state = ui.INTERACTIVE, pub.report
        ui.INTERACTIVE, pub.report = False, lambda info: True
        for n in ("one", "two"):
            cli.main(["dev", "init", os.path.join(self.w, "builtin", n), "--type", "skill", "--name", n])
        self.ws = os.path.join(self.w, "builtin")

    def tearDown(self):
        ui.INTERACTIVE, pub.report = self._state

    def log(self):
        return git("log", "--format=%s", cwd=self.bare).splitlines()

    def test_discover_finds_packages_and_stops_at_a_manifest(self):
        self.assertEqual([os.path.basename(d) for d in pub.discover(self.ws)], ["one", "two"])
        one = os.path.join(self.ws, "one")
        self.assertEqual(pub.discover(one), [one])

    def test_publish_only_one_then_all(self):
        self.assertEqual(cli.main(["dev", "publish", self.ws, "--only", "one"]), 0)
        self.assertEqual(self.log(), ["publish one 0.1.0"])
        cli.main(["dev", "publish", self.ws])
        self.assertEqual(self.log(), ["publish two 0.1.0", "publish one 0.1.0"])      # one commit per package, one push

    def test_unknown_name_is_an_error(self):
        self.assertEqual(cli.main(["dev", "validate", self.ws, "--only", "nope"]), 1)

    def test_a_broken_package_is_reported_and_the_others_still_go_out(self):
        os.remove(os.path.join(self.ws, "two", "skills", "two", "SKILL.md"))
        self.assertEqual(cli.main(["dev", "publish", self.ws]), 1)
        self.assertEqual(self.log(), ["publish one 0.1.0"])


class InitConflicts(unittest.TestCase):
    def setUp(self):
        self.ws = os.path.join(tempfile.mkdtemp(), "builtin")
        self._state = ui.INTERACTIVE
        ui.INTERACTIVE = False
        self.assertEqual(cli.main(["dev", "init", os.path.join(self.ws, "one"), "--name", "one"]), 0)

    def tearDown(self):
        ui.INTERACTIVE = self._state

    def test_nested_inside_a_package_is_refused(self):
        self.assertEqual(cli.main(["dev", "init", os.path.join(self.ws, "one", "inner"), "--name", "inner"]), 1)
        self.assertFalse(os.path.exists(os.path.join(self.ws, "one", "inner")))

    def test_above_existing_packages_is_refused(self):
        self.assertEqual(cli.main(["dev", "init", self.ws, "--name", "top"]), 1)
        self.assertFalse(os.path.exists(os.path.join(self.ws, "aihub.toml")))

    def test_sub_creates_a_sibling_and_needs_a_name(self):
        self.assertEqual(cli.main(["dev", "init", self.ws, "--sub"]), 1)
        self.assertEqual(cli.main(["dev", "init", self.ws, "--sub", "--name", "two"]), 0)
        self.assertEqual(sorted(os.listdir(self.ws)), ["one", "two"])

    def test_reinit_of_the_same_package_is_not_a_conflict(self):
        self.assertEqual(cli.main(["dev", "init", os.path.join(self.ws, "one"), "--name", "one"]), 0)

    def test_validate_warns_when_a_package_contains_another(self):
        os.makedirs(os.path.join(self.ws, "one", "deep"))
        self.assertEqual(cli.main(["dev", "init", os.path.join(self.ws, "one", "deep"), "--name", "deep", "--force"]), 1)


class SelfUninstall(unittest.TestCase):
    def test_removes_home_and_only_the_path_block(self):
        t = tempfile.mkdtemp()
        rc = os.path.join(t, ".zshrc")
        open(rc, "w").write('export A=1\n\n# >>> aihub\nexport PATH="$HOME/.aihub/bin:$PATH"\n# <<< aihub\nexport B=2\n')
        home = os.path.join(t, "aihub-home")
        os.makedirs(os.path.join(home, "bin"))
        env = {k: os.environ.get(k) for k in ("HOME", "ZDOTDIR", "AIHUB_HOME")}
        os.environ.update(HOME=t, ZDOTDIR=t, AIHUB_HOME=home)
        try:
            self.assertEqual(cli.main(["self-uninstall", "--yes"]), 0)
        finally:
            for k, v in env.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.assertFalse(os.path.exists(home))
        self.assertEqual(open(rc).read().split(), ["export", "A=1", "export", "B=2"])


def toml_git(d):
    from aihub.core import manifest as M
    return M.parse(open(os.path.join(d, "aihub.toml"), encoding="utf-8").read())["git"]


class Monorepo(unittest.TestCase):
    def setUp(self):
        t = tempfile.mkdtemp()
        self.t, self.bare = t, os.path.join(t, "r.git")
        self.w = os.path.join(t, "mono")
        os.makedirs(self.bare)
        git("init", "-q", "--bare", "-b", "main", cwd=self.bare)
        self._state = ui.INTERACTIVE, pub.report
        ui.INTERACTIVE, pub.report = False, lambda info: True
        self.assertEqual(cli.main(["dev", "init", self.w, "--mono", "--packages", "guide,tools:mcp", "--remote", self.bare]), 0)
        git("config", "user.email", "a@b.c", cwd=self.w); git("config", "user.name", "t", cwd=self.w)

    def tearDown(self):
        ui.INTERACTIVE, pub.report = self._state

    def test_mono_init_creates_one_repo_and_consistent_git_tables(self):
        self.assertEqual(sorted(n for n in os.listdir(self.w) if not n.startswith(".")), ["guide", "tools"])
        self.assertFalse(os.path.exists(os.path.join(self.w, "guide", ".git")))
        for n in ("guide", "tools"):
            g = toml_git(os.path.join(self.w, n))
            self.assertEqual((g["url"], g["branch"], g["subdir"]), (self.bare, "main", n))

    def test_mono_init_keeps_existing_packages_and_only_updates_git(self):
        other = os.path.join(self.t, "r2.git"); os.makedirs(other); git("init", "-q", "--bare", "-b", "main", cwd=other)
        self.assertEqual(cli.main(["dev", "init", self.w, "--mono", "--packages", "extra", "--remote", other]), 0)
        self.assertEqual(sorted(n for n in os.listdir(self.w) if not n.startswith(".")), ["extra", "guide", "tools"])
        for n in ("guide", "tools", "extra"):                      # the old packages follow the new remote, nothing else of theirs changed
            self.assertEqual(toml_git(os.path.join(self.w, n))["url"], other)
        self.assertTrue(os.path.exists(os.path.join(self.w, "guide", "skills", "guide", "SKILL.md")))

    def test_doctor_follows_branch_and_remote(self):
        git("checkout", "-q", "-b", "dev", cwd=self.w)
        new = os.path.join(self.t, "moved.git"); os.makedirs(new); git("init", "-q", "--bare", "-b", "dev", cwd=new)
        self.assertEqual(cli.main(["dev", "doctor", self.w, "--check"]), 1)            # out of date, nothing written
        self.assertEqual(toml_git(os.path.join(self.w, "guide"))["branch"], "main")
        self.assertEqual(cli.main(["dev", "doctor", self.w, "--remote", new]), 0)
        for n in ("guide", "tools"):
            g = toml_git(os.path.join(self.w, n))
            self.assertEqual((g["url"], g["branch"]), (new, "dev"))
        self.assertEqual(cli.main(["dev", "doctor", self.w, "--check"]), 0)

    def test_doctor_inside_one_package_touches_only_that_one(self):
        git("checkout", "-q", "-b", "dev", cwd=self.w)
        self.assertEqual(cli.main(["dev", "doctor", os.path.join(self.w, "guide")]), 0)
        self.assertEqual(toml_git(os.path.join(self.w, "guide"))["branch"], "dev")
        self.assertEqual(toml_git(os.path.join(self.w, "tools"))["branch"], "main")

    def test_tree_runs_and_reports(self):
        self.assertEqual(cli.main(["dev", "tree", self.w]), 0)

    def _nest(self):
        inner = os.path.join(self.w, "tools")
        git("init", "-q", "-b", "main", cwd=inner)
        git("config", "user.email", "a@b.c", cwd=inner); git("config", "user.name", "t", cwd=inner)
        return inner

    def test_tree_solve_merge_moves_dot_git_away_and_publishes(self):
        inner = self._nest()
        self.assertEqual(cli.main(["dev", "doctor", self.w]), 1)                       # a nested repo is a decision, not an auto-fix
        home = tempfile.mkdtemp(); os.environ["AIHUB_HOME"] = home
        try:
            self.assertEqual(cli.main(["dev", "tree-solve", self.w, "--nested", "merge"]), 0)
        finally:
            os.environ.pop("AIHUB_HOME", None)
        self.assertFalse(os.path.exists(os.path.join(inner, ".git")))
        self.assertTrue(any(n.startswith("git-tools") for n in os.listdir(os.path.join(home, "backups"))))
        self.assertEqual(cli.main(["dev", "publish", self.w]), 0)
        self.assertEqual(sorted(git("log", "--format=%s", cwd=self.bare).splitlines()), ["publish guide 0.1.0", "publish tools 0.1.0"])

    def test_tree_solve_keep_leaves_the_inner_repo_and_outer_ignores_it(self):
        inner = self._nest()
        self.assertEqual(cli.main(["dev", "tree-solve", self.w, "--nested", "keep"]), 0)
        self.assertTrue(os.path.isdir(os.path.join(inner, ".git")))
        self.assertIn("tools", git("status", "--porcelain", "--ignored", cwd=self.w))
        self.assertNotIn("?? tools/", git("status", "--porcelain", cwd=self.w))

    def test_one_broken_package_does_not_stop_the_others(self):
        os.remove(os.path.join(self.w, "tools", "skills", "tools", "SKILL.md")) if os.path.exists(os.path.join(self.w, "tools", "skills")) else \
            open(os.path.join(self.w, "tools", "aihub.toml"), "a").write("\n[[skills]]\nname = \"x\"\npath = \"nope\"\n")
        self.assertEqual(cli.main(["dev", "publish", self.w]), 1)                      # reports the failure...
        self.assertEqual(git("log", "--format=%s", cwd=self.bare).splitlines(), ["publish guide 0.1.0"])   # ...but guide went out


if __name__ == "__main__":
    unittest.main()
