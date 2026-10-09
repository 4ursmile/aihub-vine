import os
import zipapp
import tempfile
import shutil


def build(out_path):
    pkg = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # aihub/
    tmp = tempfile.mkdtemp()
    try:
        dst = os.path.join(tmp, "aihub")
        os.makedirs(dst)
        for sub in ("core", "cli"):
            shutil.copytree(os.path.join(pkg, sub), os.path.join(dst, sub),
                            ignore=shutil.ignore_patterns("__pycache__"))
        # core/defaults.json is read through importlib.resources (zip-safe), and it must be inside the archive
        assert os.path.isfile(os.path.join(dst, "core", "defaults.json")), "defaults.json missing from the CLI build"
        open(os.path.join(dst, "__init__.py"), "w").close()
        zipapp.create_archive(tmp, out_path, main="aihub.cli.entry:main", interpreter="/usr/bin/env python3")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out_path
