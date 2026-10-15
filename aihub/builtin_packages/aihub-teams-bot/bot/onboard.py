#!/usr/bin/env python3
"""OPTIONAL guided setup, offered by `aihub install aihub-teams-bot` and run by `aihub onboard aihub-teams-bot`. Interactive, stdlib only.
  1. Teams CLI present?  (offers: npm install -g @microsoft/teams.cli)   2. signed in?  (teams login: a browser popup, once)
  3. teams app create --name ... --env <temp file>                       4. merge CLIENT_ID / CLIENT_SECRET / TENANT_ID into the bot's .env
  5. pick the task, optional allow-list.   Safe to re-run: it never replaces working credentials without asking.
After this the bot runs on its own app identity; nobody has to stay signed in. You can skip all of it and edit .env by hand."""
import json
import os
import shutil
import subprocess
import sys
import tempfile

CLI = "@microsoft/teams.cli"
KEYS = ("CLIENT_ID", "CLIENT_SECRET", "TENANT_ID")
TASKS = ("general", "helpdesk", "standup", "meeting-notes")


def parse_env(path):
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    k, v = ln.split("=", 1)
                    out[k.strip()] = v.strip().strip("\"'")
    except OSError:
        pass
    return out


def set_env(path, updates):
    """Update KEY=value lines in place (keeps comments and other keys), append the missing ones, chmod 600."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    todo = dict(updates)
    for i, ln in enumerate(lines):
        k = ln.split("=", 1)[0].strip()
        if "=" in ln and not ln.lstrip().startswith("#") and k in todo:
            lines[i] = "%s=%s" % (k, todo.pop(k))
    lines += ["%s=%s" % kv for kv in todo.items()]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def logged_in(teams):
    """`teams status --json` -> True/False; None when the answer cannot be read."""
    try:
        p = subprocess.run([teams, "status", "--json"], capture_output=True, text=True, timeout=60)
        return bool(json.loads(p.stdout).get("loggedIn"))
    except Exception:
        return None


def ask(q, default=""):
    try:
        a = input("  ? %s%s " % (q, " [%s]" % default if default else "")).strip()
    except EOFError:
        a = ""
    return a or default


def yes(q, default=True):
    a = ask(q + (" [Y/n]" if default else " [y/N]")).lower()
    return default if not a else a.startswith("y")


def find_teams():
    t = shutil.which("teams")
    if t:
        return t
    print("  The Teams Developer CLI (`teams`) creates the bot registration for you.")
    if not (shutil.which("npm") and shutil.which("node")):
        print("  It needs Node.js 22.12+ and npm: https://nodejs.org\n  Then run:  npm install -g %s\n  and re-run:  aihub onboard" % CLI)
        return None
    if not yes("Install it now with `npm install -g %s`?" % CLI):
        print("  Install it yourself:  npm install -g %s   then:  aihub onboard" % CLI)
        return None
    if subprocess.call(["npm", "install", "-g", CLI]):
        print("  npm failed (permissions?). Try:  sudo npm install -g %s   or a Node version manager, then: aihub onboard" % CLI)
        return None
    return shutil.which("teams")


def create_app(teams, name, endpoint):
    """-> (exit code, dict with the CLIENT_ID/CLIENT_SECRET/TENANT_ID the CLI wrote). The CLI keeps the terminal (it may ask questions)."""
    fd, tmp = tempfile.mkstemp(prefix="teams-creds-", suffix=".env")
    os.close(fd)
    os.remove(tmp)                                    # let the CLI create the file itself
    try:
        cmd = [teams, "app", "create", "--name", name, "--env", tmp] + (["--endpoint", endpoint] if endpoint else [])
        code = subprocess.call(cmd)
        return code, {k: v for k, v in parse_env(tmp).items() if k in KEYS and v}
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def main():
    envf = os.environ.get("AIHUB_BOT_ENV") or os.path.join(os.getcwd(), ".env")
    print("\n  Teams bot setup (optional)  settings file: %s\n" % envf)
    cur = parse_env(envf)
    if cur.get("CLIENT_ID") and cur.get("CLIENT_SECRET"):
        if not yes("Credentials are already set (client id %s...). Create a NEW app registration anyway?" % cur["CLIENT_ID"][:8], False):
            print("  Keeping them.")
            return finish(envf, cur)
    teams = find_teams()
    if not teams:
        return 1
    if logged_in(teams) is not True:
        print("  Signing in to Microsoft 365 (a browser window opens; over SSH use `teams login --device-code` first).")
        if subprocess.call([teams, "login"]):
            print("  Sign-in failed or was cancelled. Run again:  aihub onboard")
            return 1
    name = ask("Name of the bot as people will see it", "AI Hub Bot")
    endpoint = ask("Public https messaging endpoint, e.g. https://xyz.devtunnels.ms/api/messages (blank = set it later)")
    if endpoint and not endpoint.startswith("https://"):
        print("  Teams needs https; ignoring %r (you can set it later in the Teams Developer Portal)." % endpoint)
        endpoint = ""
    code, got = create_app(teams, name, endpoint)
    if code or not (got.get("CLIENT_ID") and got.get("TENANT_ID")):
        print("  `teams app create` did not give usable credentials (exit %d, got: %s)." % (code, ", ".join(sorted(got)) or "nothing"))
        print("  Run it yourself:  teams app create --name \"%s\" --env %s   then: aihub onboard" % (name, envf))
        return 1
    if not got.get("CLIENT_SECRET"):
        got["CLIENT_SECRET"] = ask("The CLI wrote no client secret. Paste it (blank = managed identity, set later)")
    set_env(envf, {k: v for k, v in got.items() if v})
    print("  Saved %s to %s (private file, mode 600)." % (", ".join(sorted(k for k in got if got[k])), envf))
    return finish(envf, parse_env(envf))


def finish(envf, cur):
    upd = {}
    task = ask("What should this bot do? %s" % " / ".join(TASKS), cur.get("TEAMS_TASK") or "general")
    if task in TASKS and task != cur.get("TEAMS_TASK"):
        upd["TEAMS_TASK"] = task
    if not cur.get("TEAMS_ALLOWED"):
        allow = ask("Limit who may use it? comma separated names or e-mails (blank = everyone who can reach it)")
        if allow:
            upd["TEAMS_ALLOWED"] = allow
    if upd:
        set_env(envf, upd)
    name = os.environ.get("AIHUB_BOT", "aihub-teams-bot")
    print("\n  Done. Next:\n    aihub run %s -- --info              check what it will do\n    aihub run %s -- --try \"hello\"       test the brain without Teams\n"
          "    aihub run %s                        serve http://localhost:3978/api/messages\n"
          "  Expose that port over https (dev tunnel) and set it as the messaging endpoint. The client secret expires: renew it before then.\n"
          "  Customise: CUSTOMIZE.md in %s\n" % (name, name, name, os.environ.get("AIHUB_BOT_DIR", "the package folder")))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  Cancelled. Run again any time:  aihub onboard")
        sys.exit(130)
