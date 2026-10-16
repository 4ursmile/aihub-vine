import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from aihub.cli import installer, paths
from aihub.core import manifest as M

TOML = '[package]\nname = "pyhello"\nversion = "0.1.0"\ntype = "tool"\ndescription = "d"\n[python]\ninstall = true\n'
PYPROJECT = ('[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n'
             '[project]\nname = "pyhello"\nversion = "0.1.0"\n[project.scripts]\npyhello = "pyhello:main"\n'
             '[tool.setuptools]\npy-modules = ["pyhello"]\n')


class PythonInstall(unittest.TestCase):
    def test_manifest_flag(self):
        self.assertTrue(M.normalize(M.parse(TOML))["python"]["install"])
        self.assertFalse(M.normalize(M.parse(TOML.replace("install = true", "")))["python"]["install"])

    def test_pip_install_links_console_scripts(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}):
            src = os.path.join(home, "pkg")
            os.makedirs(src)
            open(os.path.join(src, "pyproject.toml"), "w").write(PYPROJECT)
            open(os.path.join(src, "pyhello.py"), "w").write("def main():\n    print('hello from pip')\n")
            venv = paths.ensure("venvs", "pyhello")
            subprocess.check_call([sys.executable, "-m", "venv", venv])
            pip = os.path.join(venv, "bin", "pip")
            if installer.OS == "windows" or subprocess.call([pip, "install", "-q", src], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL):
                self.skipTest("pip could not build the sample (offline or no setuptools)")
            shims = installer._pip_shims(venv, src, "pyhello")
            self.assertEqual([os.path.basename(x) for x in shims], ["pyhello"])
            self.assertIn("hello from pip", subprocess.check_output([shims[0]]).decode())

    def test_missing_pyproject_is_an_error(self):
        m = M.normalize(M.parse(TOML))
        self.assertTrue(m["python"]["install"])
        self.assertEqual(installer._pip_shims("/nonexistent", "/nonexistent", "x"), [])


if __name__ == "__main__":
    unittest.main()
