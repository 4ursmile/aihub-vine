"""Test-suite guard: no test may reach a real git remote.

The CLI sends usage events to the inbox named by AIHUB_TELEMETRY_URL, else config.json, else the bundled defaults.json, which is the
real hub repo. Tests that install packages (test_cli_manage, test_dev_workspace, ...) record events, so without this every test run
pushed fixture events ("base", "left", "right", ...) to that repo, where the server then ingested them as if they were real usage.
Pointing the variable at a path that cannot exist makes an unconfigured test spool its events locally and push nothing.
Tests that exercise the real push (tests/test_sync.py::Inbox) set their own bare repo in a temp dir and are unaffected.
"""
import os

os.environ.setdefault("AIHUB_TELEMETRY_URL", "/nonexistent/aihub-tests-must-not-push.git")
# The CLI sends events and reads the index through the hub first; tests that exercise git set their own repos, and none may call a hub
# that happens to run on this machine.
os.environ.setdefault("AIHUB_TELEMETRY_VIA", "git")
os.environ.setdefault("AIHUB_INDEX_VIA", "git")
