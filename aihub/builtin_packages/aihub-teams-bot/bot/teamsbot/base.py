"""Locate the base bot's code folder (<aihub-bot>/bot, the one holding harness/). `aihub run` lists installed packages in AIHUB_PACKAGE_DIRS."""
import os


def base_bot_dir():
    cands = [os.environ.get("AIHUB_BASE_BOT", "")]
    cands += [os.path.join(d, "bot") for d in os.environ.get("AIHUB_PACKAGE_DIRS", "").split(os.pathsep)
              if d and os.path.basename(d.rstrip("/\\")) == "aihub-bot"]
    home = os.environ.get("AIHUB_HOME") or os.path.join(os.path.expanduser("~"), ".aihub")
    cands.append(os.path.join(home, "packages", "aihub-bot", "bot"))
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "harness", "__init__.py")):
            return c
    raise SystemExit("the base bot is missing. Install it with: aihub install aihub-bot   (or point AIHUB_BASE_BOT at its bot/ folder)")
