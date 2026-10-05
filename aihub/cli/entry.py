"""Entry point. The agent hook path is handled before importing argparse/urllib so it stays fast."""
import sys


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "hook":
        from . import hooks
        src = argv[argv.index("--source") + 1] if "--source" in argv and argv.index("--source") + 1 < len(argv) else "claude"
        hooks.handle(sys.stdin.read(), src)
        return 0
    from .main import main as full
    return full(argv)


if __name__ == "__main__":
    sys.exit(main())
