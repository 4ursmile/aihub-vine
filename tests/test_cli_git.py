import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from aihub.server.config import Settings
from aihub.server.main import create_app


def pip_spec(url, branch="", subdir=""):
    # Mirrors the Get started page in static/app.js
    return f"git+{url}" + (f"@{branch}" if branch else "") + (f"#subdirectory={subdir}" if subdir else "")


class CliGit(unittest.TestCase):
    def setUp(self):
        self.c = TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=tempfile.mkdtemp())))
        self.c.post("/api/v1/auth/register", json={"username": "root", "password": "secret1"})
        self.h = {"Authorization": "Bearer " + self.c.post("/api/v1/auth/login", json={"username": "root", "password": "secret1"}).json()["token"]}
        self._env = os.environ.pop("AIHUB_CLI_GIT_BRANCH", None), os.environ.pop("AIHUB_CLI_GIT_SUBDIR", None)

    def tearDown(self):
        for k, v in zip(("AIHUB_CLI_GIT_BRANCH", "AIHUB_CLI_GIT_SUBDIR"), self._env):
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def put(self, **body):
        return self.c.put("/api/v1/admin/sync", headers=self.h, json=body)

    def meta(self):
        return self.c.get("/api/v1/meta", headers=self.h).json()

    def test_empty_gives_only_url(self):
        self.put(cli_git_url="https://git.example/team/aihub.git")
        m = self.meta()
        self.assertEqual(pip_spec(m["cli_git_url"], m["cli_git_branch"], m["cli_git_subdir"]), "git+https://git.example/team/aihub.git")

    def test_branch_only(self):
        self.put(cli_git_url="https://git.example/team/aihub.git", cli_git_branch="dev")
        m = self.meta()
        self.assertEqual(pip_spec(m["cli_git_url"], m["cli_git_branch"], m["cli_git_subdir"]), "git+https://git.example/team/aihub.git@dev")

    def test_subdir_only(self):
        self.put(cli_git_url="https://git.example/team/aihub.git", cli_git_subdir="cli")
        m = self.meta()
        self.assertEqual(pip_spec(m["cli_git_url"], m["cli_git_branch"], m["cli_git_subdir"]), "git+https://git.example/team/aihub.git#subdirectory=cli")

    def test_both(self):
        self.put(cli_git_url="https://git.example/team/aihub.git", cli_git_branch="release/1.2", cli_git_subdir="packages/cli")
        m = self.meta()
        self.assertEqual(pip_spec(m["cli_git_url"], m["cli_git_branch"], m["cli_git_subdir"]),
                         "git+https://git.example/team/aihub.git@release/1.2#subdirectory=packages/cli")

    def test_values_are_stripped_and_env_fallback(self):
        self.put(cli_git_branch="  dev  ")
        self.assertEqual(self.meta()["cli_git_branch"], "dev")
        self.put(cli_git_branch="")
        os.environ["AIHUB_CLI_GIT_SUBDIR"] = "cli"
        self.assertEqual(self.meta()["cli_git_subdir"], "cli")

    def test_invalid_subdir_rejected(self):
        for bad in ("pkg dir", "cli#x", "a?b", "../etc", "/abs", "a..b"):
            self.assertEqual(self.put(cli_git_subdir=bad).status_code, 400, msg=bad)
        self.assertEqual(self.put(cli_git_subdir="  ").status_code, 200)     # blank after strip is allowed (= hidden)

    def test_invalid_branch_rejected(self):
        for bad in ("my branch", "dev\tx", "dev\x00"):
            self.assertEqual(self.put(cli_git_branch=bad).status_code, 400, msg=repr(bad))


if __name__ == "__main__":
    unittest.main()
