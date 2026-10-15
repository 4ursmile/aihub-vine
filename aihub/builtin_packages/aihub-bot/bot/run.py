#!/usr/bin/env python3
"""Entry point of the bot (what `aihub run` starts). Usage: aihub run aihub-bot [-- --once "question" | --cron]."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
