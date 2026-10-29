#!/usr/bin/env python3
"""Stand-in for nodes that moved into a package: tells the user what to install. JSON on stdin, JSON on stdout."""
import json
import sys

sys.stdin.read()
print(json.dumps({"ok": False, "error": "This node moved to the package aihub-nodes-outlook (Windows). Install it with:  aihub install aihub-nodes-outlook  -  the same node then works again."}))
