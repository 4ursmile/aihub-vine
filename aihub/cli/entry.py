"""Entry point. The agent hook path is handled before importing argparse/urllib so it stays fast."""
import sys


class _CachedLoader:
    """Loads one aihub module from aihub.pyz with its compiled code kept on disk. zipimport never caches bytecode, so without this
    every CLI start compiled all its modules again (~0.9 s of a ~1 s `aihub --json list`). Anything else goes to the zipimporter."""

    def __init__(self, zi, name, origin, folder):
        self._zi, self._name, self._origin, self._folder = zi, name, origin, folder

    def __getattr__(self, attr):                             # archive, prefix, get_data, get_source, is_package, get_filename, ...
        return getattr(self._zi, attr)

    def get_resource_reader(self, fullname):
        return self._zi.get_resource_reader(fullname)

    def create_module(self, spec):
        return None

    def get_code(self, fullname):
        import marshal
        from importlib.util import MAGIC_NUMBER
        import os
        path = os.path.join(self._folder, fullname + ".pyc")
        try:
            with open(path, "rb") as f:
                raw = f.read()
            if raw[:4] == MAGIC_NUMBER:
                return marshal.loads(raw[4:])
        except (OSError, ValueError, EOFError, TypeError):
            pass
        src = self._zi.get_source(fullname)
        if src is None:                                      # bytecode only in the zip: zipimport's own code
            return self._zi.get_code(fullname)
        code = compile(src, self._origin, "exec", dont_inherit=True)
        try:                                                 # atomic: CLI processes started together may write the same file
            os.makedirs(self._folder, exist_ok=True)
            tmp = "%s.%d.tmp" % (path, os.getpid())
            with open(tmp, "wb") as f:
                f.write(MAGIC_NUMBER + marshal.dumps(code))
            os.replace(tmp, path)
        except (OSError, ValueError):
            pass
        return code

    def exec_module(self, module):
        exec(self.get_code(module.__name__), module.__dict__)


class _CachedFinder:
    def __init__(self, archive, folder):
        self.archive, self.folder = archive, folder

    def find_spec(self, fullname, path=None, target=None):
        """Builds the spec zipimport would, without zipimporter.find_spec: that one calls get_filename(), which COMPILES the module
        just to learn its path (so every module was compiled twice per start)."""
        if not fullname.startswith("aihub.") or not path:
            return None
        try:
            import os
            import zipimport
            from importlib.machinery import ModuleSpec
            last = fullname.rpartition(".")[2]
            for entry in path:
                if not isinstance(entry, str) or not entry.startswith(self.archive + os.sep):
                    continue
                zi = zipimport.zipimporter(entry)            # the zip's table of contents is read once per process (zipimport caches it)
                try:
                    pkg = zi.is_package(fullname)            # only looks the name up in that table
                except zipimport.ZipImportError:
                    continue
                base = os.path.join(zi.archive, zi.prefix + last)
                origin = os.path.join(base, "__init__.py") if pkg else base + ".py"
                spec = ModuleSpec(fullname, _CachedLoader(zi, fullname, origin, self.folder), origin=origin, is_package=pkg)
                spec.has_location = True
                if pkg:
                    spec.submodule_search_locations = [base]
                return spec
            return None
        except Exception:
            return None                                      # the normal import machinery takes over


def _bytecode_cache():
    """When running from aihub.pyz: keep compiled modules in ~/.aihub/cache/pyc/<archive>-<size>-<mtime>-<python>/ (a new or updated
    pyz or another Python gets its own folder; stale folders of this archive are removed). Never fatal, does nothing for pip/source."""
    try:
        import os
        import zipimport
        zi = getattr(getattr(sys.modules.get("aihub"), "__spec__", None), "loader", None)
        if not isinstance(zi, zipimport.zipimporter) or os.environ.get("AIHUB_NO_PYC_CACHE"):
            return
        archive = zi.archive
        st = os.stat(archive)
        base = os.path.join(os.environ.get("AIHUB_HOME") or os.path.join(os.path.expanduser("~"), ".aihub"), "cache", "pyc")
        h = 0xcbf29ce484222325                               # FNV-1a of the archive path: no hashlib (OpenSSL) on the hook path
        for c in os.path.abspath(archive).encode("utf-8", "replace"):
            h = ((h ^ c) * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF
        tag = "%016x" % h
        stamp = "%s-%x-%x-" % (tag, st.st_size, st.st_mtime_ns)
        folder = os.path.join(base, stamp + (sys.implementation.cache_tag or "py"))
        if not os.path.isdir(folder):                        # first start of this pyz: drop the folders of its earlier versions
            try:
                import shutil
                for old in os.listdir(base):
                    if old.startswith(tag + "-") and not old.startswith(stamp):
                        shutil.rmtree(os.path.join(base, old), ignore_errors=True)
            except OSError:
                pass
        sys.meta_path.insert(0, _CachedFinder(archive, folder))
    except Exception:
        pass


def _handover(argv):
    """pip install: run the newer zipapp the hub delivered (~/.aihub/bin/aihub.pyz), if any. Never fatal."""
    try:
        from . import updater
        if not updater.by_pip() or argv[:1] == ["upgrade"]:
            return False
        import json, os
        from ..core import version as V
        from ..core.release import VERSION
        home = os.environ.get("AIHUB_HOME") or os.path.join(os.path.expanduser("~"), ".aihub")
        st = json.load(open(os.path.join(home, "update.json")))
        pyz = st.get("pyz")
        if not (pyz and os.path.isfile(pyz) and V.compare(st.get("pyz_version", "0"), VERSION) > 0):
            return False
        import runpy
        sys.argv = [pyz] + list(argv)
        runpy.run_path(pyz, run_name="__main__")
        return True
    except SystemExit:
        raise
    except Exception:
        return False


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    _bytecode_cache()
    if argv and argv[0] == "hook":
        from . import hooks
        src = argv[argv.index("--source") + 1] if "--source" in argv and argv.index("--source") + 1 < len(argv) else "claude"
        hooks.handle(sys.stdin.read(), src)
        return 0
    if _handover(argv):
        return 0
    from .main import main as full
    return full(argv)


if __name__ == "__main__":
    sys.exit(main())
