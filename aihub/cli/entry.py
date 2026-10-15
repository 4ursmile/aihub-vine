"""Entry point. The agent hook path is handled before importing argparse/urllib so it stays fast."""
import sys


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
