import os
import zipapp
import tempfile
import shutil


def _vendor_kafka(tmp):
    """The CLI sends usage events with kafka-python, which is pure Python: copy it into the archive so `install.sh` users need no pip.
    Taken from the server's own environment (requirements-server.txt installs it). Without it the CLI still works, but cannot report usage."""
    try:
        import kafka
    except ImportError:
        return
    shutil.copytree(os.path.dirname(kafka.__file__), os.path.join(tmp, "kafka"),
                    ignore=shutil.ignore_patterns("__pycache__", "benchmarks", "*.pyc", "test*"))


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
        _vendor_kafka(tmp)
        zipapp.create_archive(tmp, out_path, main="aihub.cli.entry:main", interpreter="/usr/bin/env python3")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out_path
