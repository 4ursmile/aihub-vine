"""Precedence of setup values: environment > admin/user setting > aihub/core/defaults.json.

Covers the server's Get started / client-config values, the CLI lookups (registry, Langfuse), and that the
packaged CLI really ships defaults.json (wheel and zipapp).
Run:  python -m unittest tests.test_defaults_env -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

from fastapi.testclient import TestClient

from aihub.core import defaults
from aihub.server.config import Settings
from aihub.server.main import create_app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEF = json.load(open(os.path.join(ROOT, "aihub", "core", "defaults.json")))


class GetStartedPrecedence(unittest.TestCase):
    """Get started (/meta) and /client-config: env first, then admin settings, then defaults.json."""

    ENV = ("AIHUB_CLI_GIT_URL", "AIHUB_CLI_GIT_BRANCH", "AIHUB_CLI_GIT_SUBDIR", "AIHUB_INDEX_URL", "AIHUB_INDEX_BRANCH",
           "AIHUB_INDEX_PATH", "LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.env_file = os.path.join(self.d, "server.env")
        self.clean = {k: v for k, v in os.environ.items() if k not in self.ENV}
        self.patch = mock.patch.dict(os.environ, self.clean, clear=True)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.d, ignore_errors=True)

    def app(self, env_lines=()):
        # always load through an explicit temp .env, so the repo's real ./.env is never read by the test
        with open(self.env_file, "w") as f:
            f.write("\n".join(env_lines) + "\n")
        s = Settings.load(env_file=self.env_file, data_dir=self.d, seed_builtin=False, sync_enabled=False)
        c = TestClient(create_app(s))
        c.post("/api/v1/auth/register", json={"username": "root", "password": "secret1"})
        self.h = {"Authorization": "Bearer " + c.post("/api/v1/auth/login", json={"username": "root", "password": "secret1"}).json()["token"]}
        return c

    def set_admin(self, c, **body):
        self.assertEqual(c.put("/api/v1/admin/sync", headers=self.h, json=body).status_code, 200)

    def test_env_beats_admin_beats_defaults_for_cli_git(self):
        c = self.app([])                                                  # nothing in the environment or .env
        self.assertEqual(c.get("/api/v1/meta", headers=self.h).json()["cli_git_url"], DEF["cli"]["git_url"])   # defaults.json
        self.set_admin(c, cli_git_url="https://admin.example/aihub.git", cli_git_branch="admin-branch")
        m = c.get("/api/v1/meta", headers=self.h).json()
        self.assertEqual(m["cli_git_url"], "https://admin.example/aihub.git")                                  # admin beats defaults
        self.assertFalse(m["from_env"]["cli_git_url"])
        self.assertEqual(m["cli_git_branch"], "admin-branch")
        # environment (.env file) wins over the admin setting
        c = self.app(["AIHUB_CLI_GIT_URL=https://env.example/aihub.git", "AIHUB_CLI_GIT_BRANCH=env-branch"])
        self.set_admin(c, cli_git_url="https://admin.example/aihub.git", cli_git_branch="admin-branch")
        m = c.get("/api/v1/meta", headers=self.h).json()
        self.assertEqual(m["cli_git_url"], "https://env.example/aihub.git")
        self.assertEqual(m["cli_git_branch"], "env-branch")
        self.assertTrue(m["from_env"]["cli_git_url"] and m["from_env"]["cli_git_branch"])
        self.assertFalse(m["from_env"]["cli_git_subdir"])

    def test_real_environment_beats_env_file(self):
        c = self.app(["AIHUB_CLI_GIT_URL=https://file.example/aihub.git"])
        with mock.patch.dict(os.environ, {"AIHUB_CLI_GIT_URL": "https://real.example/aihub.git"}):
            s = Settings.load(env_file=self.env_file, data_dir=self.d, seed_builtin=False, sync_enabled=False)
            self.assertEqual(s.env_get("AIHUB_CLI_GIT_URL"), "https://real.example/aihub.git")

    def test_env_file_values_are_visible_to_public_reads(self):
        c = self.app(["AIHUB_INDEX_URL=https://file.example/index.git", "LANGFUSE_BASE_URL=https://lf.example",
                      "LANGFUSE_PUBLIC_KEY=pk-file", "LANGFUSE_SECRET_KEY=sk-file"])
        self.set_admin(c, share_credentials="1", index_url="https://admin.example/index.git", langfuse_host="https://admin-lf.example")
        cfg = c.get("/api/v1/client-config", headers=self.h).json()
        self.assertEqual(cfg["index"]["url"], "https://file.example/index.git")                  # env beats admin
        self.assertEqual(cfg["credentials"]["langfuse"]["host"], "https://lf.example")
        self.assertEqual(cfg["credentials"]["langfuse"]["secret_key"], "sk-file")
        sync = c.get("/api/v1/admin/sync", headers=self.h).json()
        self.assertTrue(sync["from_env"]["index"])
        self.assertTrue(sync["from_env"]["index_url"])
        self.assertTrue(sync["from_env"]["langfuse_host"])

    def test_client_config_falls_back_to_defaults_json(self):
        c = self.app([])
        cfg = c.get("/api/v1/client-config", headers=self.h).json()
        self.assertEqual(cfg["index"]["branch"], DEF["index"]["branch"])
        self.assertEqual(cfg["index"]["path"], DEF["index"]["path"])
        self.assertEqual(cfg["index"]["url"], DEF["index"]["git_url"])


class CliLookupDefaults(unittest.TestCase):
    """The CLI resolves with nothing set: environment > ~/.aihub/config.json > defaults.json."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        keep = {k: v for k, v in os.environ.items() if not k.startswith(("AIHUB_", "LANGFUSE_"))}
        keep["AIHUB_HOME"] = os.path.join(self.home, ".aihub")
        self.patch = mock.patch.dict(os.environ, keep, clear=True)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.home, ignore_errors=True)

    def test_registry_uses_defaults_json_when_nothing_else_set(self):
        from aihub.cli import registry
        src = registry.source()
        self.assertEqual(src["url"], DEF["index"]["git_url"])
        self.assertEqual(src["branch"], DEF["index"]["branch"])
        self.assertEqual(src["path"], DEF["index"]["path"])

    def test_langfuse_and_event_names_come_from_defaults_json(self):
        from aihub.cli import langfuse, telemetry
        self.assertEqual(langfuse.settings()["host"], DEF["langfuse"]["host"].rstrip("/"))
        self.assertEqual(telemetry.langfuse_name("use"), DEF["event_names"]["use"])

    def test_config_set_and_environment_still_override(self):
        from aihub.cli import registry
        from aihub.core import defaults as D
        from aihub.cli import paths
        paths.save("config.json", {"index_url": "https://cfg.example/idx.git", "index_branch": "dev"})
        src = registry.source()
        self.assertEqual(src["url"], "https://cfg.example/idx.git")
        self.assertEqual(src["branch"], "dev")
        with mock.patch.dict(os.environ, {"AIHUB_INDEX_URL": "https://env.example/x.git", "AIHUB_INDEX_BRANCH": "envb"}):
            src = registry.source()
            self.assertEqual(src["url"], "https://env.example/x.git")
            self.assertEqual(src["branch"], "envb")
        self.assertEqual(D.load()["index"]["git_url"], DEF["index"]["git_url"])   # defaults themselves untouched

    def test_defaults_load_without_a_file_path(self):
        # importlib.resources read: works from a zipapp too (no open() on a path inside an archive)
        self.assertIn("event_names", defaults.load())


class PackagedDefaults(unittest.TestCase):
    """The built artifacts must carry defaults.json: a wheel (pip install git+...) and the zipapp install.sh ships."""

    def test_zipapp_contains_defaults_and_resolves_them(self):
        from aihub.core import zipapp
        tmp = tempfile.mkdtemp()
        try:
            pyz = zipapp.build(os.path.join(tmp, "aihub.pyz"))
            with zipfile.ZipFile(pyz) as z:
                self.assertIn("aihub/core/defaults.json", z.namelist())
            out = subprocess.run([sys.executable, "-I", "-c",
                                  "import sys; sys.path.insert(0, %r); from aihub.cli import registry; print(registry.source()['url'])" % pyz],
                                 capture_output=True, text=True, env=dict(os.environ, AIHUB_HOME=tmp), timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.strip(), DEF["index"]["git_url"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_wheel_contains_defaults_json(self):
        try:
            import setuptools  # noqa: F401  (pip wheel needs the build backend)
        except ImportError:
            self.skipTest("setuptools not installed; cannot build a wheel here")
        tmp = tempfile.mkdtemp()
        try:
            r = subprocess.run([sys.executable, "-m", "pip", "wheel", ROOT, "--no-deps", "--no-build-isolation", "-q", "-w", tmp],
                               capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                last = (r.stderr or r.stdout).strip().splitlines()
                self.skipTest("wheel build unavailable: " + (last[-1] if last else "pip wheel failed"))
            wheels = [f for f in os.listdir(tmp) if f.endswith(".whl")]
            self.assertEqual(len(wheels), 1)
            with zipfile.ZipFile(os.path.join(tmp, wheels[0])) as z:
                self.assertIn("aihub/core/defaults.json", z.namelist())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
