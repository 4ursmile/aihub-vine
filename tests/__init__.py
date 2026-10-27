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

os.environ.setdefault("AIHUB_POLICY_JUDGE", "off")      # no test may start a real `claude -p` judge

# A test git call must never wait for a person. Run from an IDE terminal, git inherits the IDE's GUI askpass (VS Code: GIT_ASKPASS +
# VSCODE_GIT_*), which opens a login dialog nobody answers and freezes the whole run (local test listeners answer 401 on purpose).
# Tests that need their own askpass (test_git_login, test_robust) still patch it in for their duration.
for _k in [k for k in os.environ if k.startswith("VSCODE_GIT_")]:
    del os.environ[_k]
os.environ["GIT_ASKPASS"] = os.environ["SSH_ASKPASS"] = "true"
os.environ["GIT_TERMINAL_PROMPT"] = "0"
