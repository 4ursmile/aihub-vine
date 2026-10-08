"""Who is acting: the logged-in hub user, else the local OS user (anonymous)."""
import getpass
import os
import socket
import sys

from . import paths


def os_user():
    try:
        return getpass.getuser()
    except Exception:
        return os.path.basename(os.path.expanduser("~")) or "unknown"


def current():
    cred = paths.load("credentials.json", {})
    user = cred.get("username")
    anonymous = not user
    return {
        "user": user or "~" + os_user(),
        "anonymous": anonymous,
        "local_user": os_user(),
        "host": socket.gethostname(),
        "os": sys.platform,
        "cwd": os.getcwd(),
    }
