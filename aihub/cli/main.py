import argparse
import functools
import getpass
import json
import os
import re
import sys
import tempfile
import urllib.parse

from ..core import archive, ignore, manifest as M, naming, pysync, version as V
from . import api, gitx, installer, paths, publish as pub, registry, remote_config, ui, updater, workspace as ws
from .installer import _spec

def cmd_config(a):
    c = paths.load("config.json", {})
    if a.action == "set":
        if not (a.key and a.value is not None):
            raise ValueError("usage: aihub config set <key> <value>")
        c[a.key] = a.value
        paths.save("config.json", c)
        ui.good("%s = %s" % (a.key, "set" if ui.JSON and SECRET_KEY.search(a.key) else a.value))
    cfg = paths.config()
    ui.result(masked(cfg))
    if ui.JSON:
        return
    if a.action != "set" or not ui.TTY:
        print(json.dumps(cfg, indent=2))


SECRET_KEY = re.compile(r"token|secret|passw|credential|api_?key", re.I)


def masked(v):
    """A copy safe to print as JSON: any value under a key that looks secret becomes "set" (or "" when empty)."""
    if isinstance(v, dict):
        return {k: ("set" if x else "") if SECRET_KEY.search(str(k)) and not isinstance(x, (dict, list, bool)) else masked(x) for k, x in v.items()}
    if isinstance(v, list):
        return [masked(x) for x in v]
    return v


def _secret_in(a, what):
    """--password-stdin: the secret(s) on standard input, one per line (never argv, never echoed)."""
    lines = sys.stdin.read().splitlines()
    if len(lines) < what or any(not x for x in lines[:what]):
        raise ValueError("--password-stdin: expected %d non-empty line(s) on standard input" % what)
    return lines[:what]


def cmd_login(a):
    if a.password_stdin or ui.JSON:
        if not a.username:
            raise ValueError("give the USERNAME (and the password on standard input with --password-stdin)")
        pw = _secret_in(a, 1)[0]
    else:
        a.username = a.username or input("username: ")
        pw = getpass.getpass("password: ")
    u = a.username
    r = api.call("POST", "/auth/login", {"username": u, "password": pw})
    u = r.get("username") or u                               # the hub may map abc@gmail.com to the account abc (allowed sign-in domains)
    paths.save("credentials.json", {"token": r["token"], "username": u}, private=True)
    ui.result({"signed_in": True, "username": u, "hub": paths.config()["hub"]})       # never the token
    ui.celebrate("Welcome back, %s" % u)
    from . import hooks, usagesync
    hooks.verify()
    usagesync.kick()                                         # a new account: upload your usage history in the background


def cmd_logout(a):
    """Forget the saved token. Later usage events are anonymous again (attributed by OS user name)."""
    cred = paths.load("credentials.json", {})
    if not cred.get("token") and not cred.get("username"):
        return ui.info("not signed in")
    paths.save("credentials.json", {}, private=True)
    ui.result({"signed_in": False, "signed_out": cred.get("username") or None})
    ui.good("Signed out %s" % (cred.get("username") or ""))


def cmd_password(a):
    """Change the password of the signed-in account. The hub signs your other devices out; this session stays valid."""
    cred = paths.load("credentials.json", {})
    if not cred.get("token"):
        raise ValueError("not signed in: run 'aihub login' first")
    if a.password_stdin or ui.JSON:
        old, new = _secret_in(a, 2)                          # line 1 current, line 2 new
        again = new
    else:
        old = getpass.getpass("current password: ")
        new = getpass.getpass("new password: ")
        again = None
    if len(new) < 6:
        raise ValueError("new password must be at least 6 characters")
    if new == old:
        raise ValueError("new password must differ from the current one")
    if (again if again is not None else getpass.getpass("repeat new password: ")) != new:
        raise ValueError("the two new passwords do not match")
    api.call("POST", "/auth/password", {"old": old, "new": new})
    ui.result({"changed": True, "username": cred.get("username")})
    ui.good("Password changed for %s" % (cred.get("username") or "your account"))
    ui.info("your other devices were signed out; sign in again there with 'aihub login'")


def cmd_admin(a):
    """Hub administration: accounts, groups and roles (needs the admin permission on the hub)."""
    from . import admin
    return admin.cli(a)


def cmd_register(a):
    if a.password_stdin or ui.JSON:
        if not a.username:
            raise ValueError("give the USERNAME (and the password on standard input with --password-stdin)")
        u, pw = a.username, _secret_in(a, 1)[0]
    else:
        u = a.username or input("username: ")
        pw = getpass.getpass("password: ")
        if getpass.getpass("re-enter password: ") != pw:
            raise SystemExit("aihub: the two passwords do not match; nothing was created")
    api.call("POST", "/auth/register", {"username": u, "password": pw})
    ui.result({"created": True, "username": u})
    ui.good("Account created for %s" % u)
    ui.info("next: aihub login   (new accounts may need admin approval first)")


def _plain(a):
    """--plain / --name-only: bare lines on stdout for scripts and assistants (no spinner, colors, table or hints)."""
    return bool(getattr(a, "plain", False) or getattr(a, "name_only", False))


def _plain_line(a, name, version):
    return name if getattr(a, "name_only", False) else "%s %s" % (name, version or "-")


def cmd_search(a):
    if ui.JSON:
        return ui.result(registry.search(a.term))                 # every match, no 30-row cap
    if _plain(a):
        for p in registry.search(a.term):
            print(_plain_line(a, p["name"], p.get("latest_version")))
        return
    with ui.Spinner("Searching for '%s'" % a.term if a.term else "Loading packages") as sp:
        items = registry.search(a.term)[:30]
        sp.text("%d result%s" % (len(items), "" if len(items) == 1 else "s"))
    if not items:
        ui.note("Nothing matches '%s'." % a.term)
        ui.info("try a shorter word, or publish your own: aihub dev init")
        return
    ui.out()
    ui.table(["NAME", "TYPE", "VERSION", "DESCRIPTION"],
             [[ui.bold(ui.highlight(p["name"], a.term)), ui.badge(p["type"]), p.get("latest_version") or "-",
               ui.highlight((p.get("description") or "").replace("\n", " "), a.term)] for p in items])
    ui.out()
    ui.info("install one with: aihub install <name>      details: aihub info <name>")


INSTALLED_KEYS = ("version", "type", "components", "path", "bot_home", "venv", "rev", "repo", "source", "installed", "tools", "shims")


def installed_view(n, r):
    return masked(dict({k: r[k] for k in INSTALLED_KEYS if k in r}, name=n, enabled=installer.enabled(r)))


def cmd_info(a):
    with ui.Spinner("Fetching %s" % a.name):
        d = dict(registry.get(a.name))
        d["versions"] = [{"version": v} for v in reversed(registry.versions(d))]
    if not ui.TTY:
        print(json.dumps(d, indent=2))
        return
    meta = ["%s  v%s" % (ui.badge(d["type"]), d.get("latest_version") or "-")]
    r = (d.get("rating") or {}).get("avg")
    if r:
        meta.append("rating %s" % r)
    lines = [d.get("description") or "No description", "", ui.dim("  |  ").join(meta)]
    if d.get("tags"):
        lines.append(ui.dim("tags: ") + ", ".join(d["tags"]))
    vs = d.get("versions") or []
    if vs:
        lines.append(ui.dim("versions: ") + ", ".join(v["version"] + (" (yanked)" if v.get("yanked") else "") for v in vs[:6])
                     + (" ..." if len(vs) > 6 else ""))
    req = ((d.get("versions") or [{}])[0].get("manifest") or d.get("manifest") or {}).get("requires") or {}
    if req.get("packages"):
        lines.append(ui.dim("needs: ") + ", ".join(req["packages"]))
    ui.out()
    ui.card(d["name"], lines, ui.dim("install  ") + ui.code("aihub install " + d["name"]))


def read_requirements(path, _seen=()):
    """Lines of a pip-style requirements file: one `name[<spec>]` per line, `#` comments, blank lines and `-r other.txt` honored."""
    path = os.path.abspath(path)
    if path in _seen:
        raise ValueError("%s includes itself through -r" % path)
    try:
        lines = open(path, encoding="utf-8-sig").read().splitlines()
    except OSError as e:
        raise ValueError("cannot read %s: %s" % (path, e.strerror or e))
    out = []
    for ln in lines:
        ln = re.split(r"(?:^|\s)#", ln, 1)[0].strip()
        if not ln:
            continue
        if ln.startswith(("-r ", "-r=", "--requirement")):
            ref = ln.split(None, 1)[1] if " " in ln else ln.split("=", 1)[1]
            out += read_requirements(os.path.join(os.path.dirname(path), ref.strip()), _seen + (path,))
        elif ln.startswith("-"):
            raise ValueError("%s: unsupported option %r (only package names and -r are read)" % (path, ln))
        else:
            out.append(ln)
    return out


def cmd_install(a):
    """Install several packages (names given and/or -r files); one failing package never stops the rest."""
    specs = list(a.name or [])
    for f in a.requirement or []:
        specs += read_requirements(f)
    if not specs:
        raise ValueError("nothing to install: give package names or -r requirements.txt")
    specs = list(dict.fromkeys(specs))               # same line twice installs once
    registry.INDIRECT, registry.REPO_ONLY = _source_mode(a)
    ui.out()
    if len(specs) == 1:
        n, sp = _spec(specs[0])
        installer.install(n, sp, a.yes, a.tool)
        return
    failed = installer.install_many([_spec(s) for s in specs], a.yes, a.tool)    # one plan, parallel downloads
    ui.result({"failed": [{"name": n, "error": str(e)} for n, e in failed]})
    for n, e in failed:
        ui.bad("%s: %s" % (n, e))
    if failed:
        raise ValueError("%d of %d package(s) not installed: %s" % (len(failed), len(specs), ", ".join(n for n, _ in failed)))


def _source_mode(a):
    """(INDIRECT, REPO_ONLY) from `-i` and `--from archive|repo`. archive = through the hub (what -i does), repo = git only."""
    src = getattr(a, "source", None) or ""
    ind = bool(getattr(a, "indirect", False))
    if ind and src == "repo":
        raise ValueError("-i (through the hub) and --from repo (git only) contradict each other")
    return ind or src == "archive", src == "repo"


def cmd_download(a):
    """Save a package without installing it: its .tar.gz (the default for a package published to the hub, and with --archive) or its
    files in a folder (the default for a git package, and with --folder)."""
    n, s = _spec(a.name.replace("@", "==", 1) if "@" in a.name else a.name)
    r = registry.resolve(n, s)
    registry.INDIRECT, registry.REPO_ONLY = _source_mode(a)
    if a.archive and a.folder:
        raise ValueError("--archive and --folder contradict each other")
    out = os.path.abspath(a.output or ".")
    base = "%s-%s" % (r["name"], r["version"])
    if a.archive or (not a.folder and registry.on_hub(r["repo"]) and not registry.REPO_ONLY):
        if registry.REPO_ONLY:
            raise ValueError("--from repo cannot save an archive")
        os.makedirs(out, exist_ok=True)
        dest = os.path.join(out, base + ".tar.gz")
        with ui.Spinner("Downloading %s %s" % (r["name"], r["version"])) as sp:
            last = [-1]
            def prog(done, total):
                pct = int(100 * done / total) if total else 100
                if pct != last[0]:
                    last[0] = pct
                    sp.text("Downloading %s %s (%s of %s, %d%%)" % (r["name"], r["version"], ui.human(done), ui.human(total), pct))
            got = registry.fetch_archive(r, dest, progress=prog)
        ui.good("Saved %s (%s, sha256 %s)" % (dest, ui.human(os.path.getsize(dest)), got["sha256"][:16]))
        return
    dest = os.path.join(out, base)
    with ui.Spinner("Fetching %s %s" % (r["name"], r["version"])):
        registry.checkout(r, dest)
    ui.good("Saved %s" % dest)


def cmd_uninstall(a):
    users = installer.dependents(installer.state(), naming.normalize(a.name), only_enabled=False)
    if users and not a.force and ui.INTERACTIVE and not ui.confirm("%s is needed by %s. Remove anyway?" % (a.name, ", ".join(users)), False):
        ui.info("nothing changed")
        return
    installer.uninstall(a.name)


def _rc_files():
    home = os.path.expanduser("~")
    return [os.path.join(os.environ.get("ZDOTDIR") or home, ".zshrc"), os.path.join(home, ".bash_profile"), os.path.join(home, ".bashrc"),
            os.path.join(home, ".profile"), os.path.join(home, ".config", "fish", "config.fish")]


CLI_PACKAGE = "aihub-cli"          # the CLI itself, reported as a package so installs show up on the dashboard


def _cli_origin():
    """'web' when run from the aihub.pyz the hub's install script downloaded, else 'git' (pip install from a git URL)."""
    return "web" if os.path.basename(sys.argv[0] or "").endswith(".pyz") or ".pyz" in (getattr(sys.modules.get("__main__"), "__file__", "") or "") else "git"


def record_cli_install():
    """Once per machine: tell the hub this CLI was installed (web or git). Never fails a command."""
    try:
        from ..core.release import VERSION
        from . import hooks, telemetry
        marker = paths.p(".cli_installed")
        if os.path.exists(marker) or os.environ.get("AIHUB_NO_TELEMETRY"):
            return
        paths.ensure()
        open(marker, "w").write(VERSION)
        telemetry.record({"kind": "install", "package": CLI_PACKAGE, "version": VERSION, "client_id": hooks.client_id(),
                          "detail": "origin=" + _cli_origin()})
        hooks.verify()                                       # first run: be sure the usage hooks are in place...
        from . import usagesync
        usagesync.kick()                                     # ...and send what the logs already hold (no-op until a hub and a login exist)
    except Exception:
        pass


def cmd_self_uninstall(a):
    """Remove aihub from this machine: installed packages, usage hooks, the PATH line install.sh added, and ~/.aihub."""
    import re
    import shutil
    from . import hooks
    home = paths.HOME()
    pk = sorted(installer.state()["packages"])
    rc = [f for f in _rc_files() if os.path.isfile(f) and "# >>> aihub" in open(f, encoding="utf-8", errors="replace").read()]
    ui.out()
    ui.out("  This removes aihub from this machine:")
    ui.out("    - %d installed package(s)%s" % (len(pk), ": " + ", ".join(pk[:6]) + (" ..." if len(pk) > 6 else "") if pk else ""))
    ui.out("    - running bots and services (aihub run) are stopped first")
    ui.out("    - usage hooks in your AI tools")
    ui.out("    - the PATH line in %s" % (", ".join(rc) if rc else "your shell config (none found)"))
    ui.out("    - %s (settings, credentials, queue, the aihub program)" % home)
    ui.out("  " + ui.dim("Packages you published and your git repositories are not touched."))
    if not a.yes and not (ui.INTERACTIVE and ui.confirm("Remove aihub?", False)):
        return ui.info("nothing changed (pass --yes to skip this question)")
    try:
        from ..core.release import VERSION
        from . import hooks as _h, telemetry
        telemetry.deliver({"kind": "uninstall", "package": CLI_PACKAGE, "version": VERSION, "client_id": _h.client_id(),
                           "detail": "origin=" + _cli_origin()}, timeout=10)      # before ~/.aihub (and the spool) is deleted
    except Exception:
        pass
    from . import runs
    runs.stop_all()                 # supervised bots and services (aihub run) end before their files and ~/.aihub go away
    for n in pk:
        try:
            installer.uninstall(n, quiet=True)
            ui.good("removed package %s" % n)
        except (api.ApiError, OSError) as e:
            ui.note("could not remove %s: %s" % (n, e))
    try:
        from . import credhelper
        credhelper.unregister()
    except Exception:
        pass
    for label, status in hooks.ensure(remove=True):
        ui.good("%-12s hook %s" % (label, status))
    for f in rc:
        try:
            text = open(f, encoding="utf-8").read()
            new = re.sub(r"\n?# >>> aihub\n.*?# <<< aihub\n?", "\n", text, flags=re.S)
            open(f, "w", encoding="utf-8", newline="").write(new)
            ui.good("cleaned PATH in %s" % f)
        except OSError as e:
            ui.note("could not edit %s: %s" % (f, e))
    if os.name == "nt":
        ui.note("Windows: remove %s from your user PATH (System > Environment Variables); the folder is deleted below if it is not in use" % os.path.join(home, "bin"))
    gitx.rmtree(home)
    ui.celebrate("aihub removed" if not os.path.exists(home) else "aihub removed, except %s (still in use; delete it by hand)" % home)
    ui.info("open a new terminal so PATH is refreshed. A pip install can be removed with: pip uninstall aihub-cli")


def cmd_list(a):
    pk = installer.state()["packages"]
    if ui.JSON:
        return ui.result([installed_view(n, r) for n, r in sorted(pk.items())])
    if _plain(a):
        for n, r in sorted(pk.items()):
            print(_plain_line(a, n, r["version"]))
        return
    if not pk:
        ui.note("Nothing installed yet.")
        ui.info("find something with: aihub search")
        return
    ui.out()
    ui.table(["NAME", "VERSION", "STATUS", "ADDS"],
             [[ui.bold(n), r["version"], ui.ok("enabled") if installer.enabled(r) else ui.warn("disabled"),
               ", ".join(r.get("components", [])) or "-"] for n, r in sorted(pk.items())])
    ui.out()
    off = sum(1 for r in pk.values() if not installer.enabled(r))
    ui.info("%d installed%s. update all with: aihub update" % (len(pk), ", %d disabled" % off if off else ""))


def cmd_run(a):
    """Bots as supervised processes (background by default). Without a target: the installed bots and the runs on this machine."""
    from . import runs
    if not a.target:
        found = installer.bots()
        if len(found) == 1 and not runs.all_runs():
            a.target = next(iter(found))
        else:
            ui.out()
            if not found:
                ui.note("No bots or services installed. Try: aihub install aihub-bot")
            else:
                ui.table(["NAME", "TYPE", "SETTINGS"], [[ui.bold(n), installer.runnable_kind(n, r) or "bot", os.path.join(r.get("bot_home", ""), ".env")]
                                                        for n, (r, _) in sorted(found.items())])
                ui.info("run one with: aihub run NAME   (--fg: in this terminal)")
            if runs.all_runs():
                runs.cmd_list(a)
            return
    if a.target not in ("_supervise",) + RUN_ACTIONS:
        a.target = naming.normalize(a.target)
    return runs.cli(a)


RUN_ACTIONS = ("start", "stop", "quit", "kill", "restart", "pause", "list", "ls", "ps", "logs", "log", "listen", "attach", "set", "rm", "remove", "status", "show")


def cmd_onboard(a):
    """Optional guided credential setup of an installed bot (runs the bot's own onboarding script)."""
    found = installer.bots()
    name = a.name and naming.normalize(a.name)
    if not name:
        withs = [n for n, (_, mm) in found.items() if M.runnable(mm)["onboard"]]
        if len(withs) != 1:
            ui.out()
            ui.info("bots/services with guided setup: %s" % (", ".join(withs) or "none"))
            ui.info("run one with: aihub onboard NAME")
            return
        name = withs[0]
    ui.out()
    installer.onboard(name, force=True)


def cmd_knowledge(a):
    from . import knowledge
    knowledge.cli(a)


def cmd_space(a):
    from . import spaces
    spaces.cli(a)


def cmd_soul(a):
    """Shortcut for `aihub knowledge get soul`: print the persona note."""
    from . import knowledge
    print(knowledge.get("soul"), end="")


def cmd_enable(a):
    ui.out()
    names = sorted(installer.state()["packages"]) if a.all else a.names
    if not names:
        raise ValueError("name a package, or use --all")
    for n in names:
        if a.all and installer.enabled(installer.state()["packages"][n]):
            continue
        installer.enable(n, a.yes, a.tool)


def cmd_disable(a):
    ui.out()
    st = installer.state()["packages"]
    names = a.names
    if a.all:
        names = [n for n, r in st.items() if installer.enabled(r)]
        a.force = True
    if not names:
        raise ValueError("name a package, or use --all")
    for n in names:
        installer.disable(n, a.force)


def cmd_tree(a):
    ui.out()
    if a.remote:
        if not a.name:
            raise ValueError("--remote needs a package name")
        n, s = _spec(a.name)
        with ui.Spinner("Resolving %s" % n):
            lines, pkgs = installer.remote_tree(n, s)
        for l in lines:
            ui.out("  " + l)
        ui.out()
        ui.info("%d package%s, %s" % (len(pkgs), "" if len(pkgs) == 1 else "s", ui.human(sum(p.get("size", 0) for p in pkgs))))
        return
    lines = installer.local_tree(a.name)
    if not lines:
        ui.note("Nothing installed yet.")
        return
    for l in lines:
        ui.out("  " + l)
    if a.name:
        users = installer.dependents(installer.state(), naming.normalize(a.name), only_enabled=False)
        if users:
            ui.out()
            ui.info("needed by: " + ", ".join(users))


def cmd_lock(a):
    lock = installer.make_lock()
    if not lock["packages"]:
        raise ValueError("nothing installed, so there is nothing to lock")
    text = json.dumps(lock, indent=2) + "\n"
    ui.result({"file": os.path.abspath(a.file), "lock": lock})
    if a.check:
        try:
            same = open(a.file, encoding="utf-8").read() == text
        except OSError:
            raise ValueError("%s does not exist (create it with: aihub lock)" % a.file)
        if not same:
            raise ValueError("%s is out of date; run: aihub lock" % a.file)
        ui.good("%s is up to date (%d packages)" % (a.file, len(lock["packages"])))
        return
    with open(a.file, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    ui.good("Locked %d packages to %s" % (len(lock["packages"]), a.file))
    ui.info("commit it; on another machine run: aihub sync")


def cmd_sync(a):
    lock = installer.read_lock(a.file)
    ui.out()
    with ui.Spinner("Resolving %d locked packages" % len(lock["packages"])):
        pkgs, todo, flips, extras, want = installer.sync_actions(lock)
    if not todo and not flips and not (extras and a.prune):
        ui.good("Already in sync with %s (%d packages)" % (a.file, len(pkgs)))
        if extras:
            ui.info("not in the lock file: %s (use --prune to remove)" % ", ".join(extras))
        return
    for p in todo:
        ui.step("install %s %s" % (p["name"], p["version"]))
    for n, on in flips:
        ui.step("%s %s" % ("enable" if on else "disable", n))
    if extras:
        (ui.step if a.prune else ui.info)("%s: %s" % ("remove" if a.prune else "not in the lock file", ", ".join(extras)))
    if a.check:
        raise ValueError("not in sync with %s" % a.file)
    if not a.yes and not ui.confirm("Apply these changes?", True):
        ui.info("nothing changed")
        return
    roots = [n for n, x in want.items() if x.get("requested")] or list(want)
    installer.install_plan(todo, a.yes, a.tool, roots=roots)
    st = installer.state()["packages"]
    for n, x in want.items():                 # restore requested/enabled flags exactly as locked
        if n in st:
            s2 = installer.state()
            s2["packages"][n]["requested"] = bool(x.get("requested"))
            installer.save_state(s2)
    for n, on in sorted(flips + [(n, x.get("enabled", True)) for n, x in want.items() if n in {p["name"] for p in todo}]):
        cur = installer.state()["packages"].get(n)
        if not cur or installer.enabled(cur) == on:
            continue
        (installer.enable(n, a.yes, a.tool) if on else installer.disable(n, True))
    if a.prune:
        for n in extras:
            installer.uninstall(n)
    ui.celebrate("in sync with %s" % a.file)


def cmd_update(a):
    with registry.quiet():          # hub first, never ask for a git login
        return _update(a)


def _update(a):
    pk = [(n, r) for n, r in installer.state()["packages"].items() if (not a.name or n == a.name) and installer.enabled(r)]
    if not pk:
        ui.note("No matching installed packages.")
        return
    updated = 0
    st = installer.state()
    for n, r in pk:
        spec = installer.constraint_on(st, n)        # never move a package outside what its dependents allow
        with ui.Spinner("Checking %s" % n) as sp:
            latest = registry.resolve(n, spec)["version"]
            newer = V.compare(latest, r["version"]) > 0
            sp.text("%s %s" % (n, "-> " + latest if newer else "is up to date (%s)" % r["version"]))
        if newer:
            ui.step("updating %s %s %s %s" % (n, r["version"], ui.ARROW, latest))
            installer.install(n, spec, True, None)
            updated += 1
            st = installer.state()
    ui.celebrate("%d updated" % updated) if updated else ui.info("everything is current")


def cmd_doctor(a):
    import shutil
    from . import integrations
    ui.heading("aihub doctor")
    _check_backends(detail=True)
    cred = paths.load("credentials.json", {})
    (ui.good if cred.get("token") else ui.note)("signed in as %s" % cred["username"] if cred.get("token") else "not signed in (aihub login)")
    tools = integrations.detected()
    if not tools:
        ui.note("no AI tools detected (Claude Code, Codex, OpenCode)")
    for t in tools:
        if t.has_hook():
            ui.good("%s: usage hook installed" % t.label)
        else:
            ui.note("%s: usage hook missing - run: aihub hooks install" % t.label)
    py = shutil.which("python3") or shutil.which("python")
    (ui.good if py else ui.bad)("python: %s" % (py or "not found"))
    ui.good("%d package(s) installed" % len(installer.state()["packages"]))


def _hub_status(tries=3):
    """Hub first: retry /healthz a few times. Returns True when reachable."""
    import time
    for i in range(tries):
        try:
            api.call("GET", "/healthz", timeout=4)
            return True
        except Exception:
            if i < tries - 1:
                time.sleep(1 + i)
    return False


def _check_backends(detail=False):
    """The hub is the default route (checked with retries). Git (package index + telemetry inbox) is checked only when the hub cannot be reached."""
    from . import telemetry, gitx
    hub = paths.config()["hub"]
    with ui.Spinner("Checking the hub") as sp:
        ok = _hub_status()
        if ok:
            sp.text("Hub connection OK - %s" % hub)
        else:
            sp.problem("hub unreachable (%s); falling back to git" % hub)
    if ok:
        return
    idx = registry.source()
    with ui.Spinner("Checking the package index (git)") as sp:
        if not idx["url"]:
            sp.problem("no package index configured. run: aihub setup   (or: aihub config set index_url <git url>)")
        else:
            try:
                n = registry.count(refresh=True)
                sp.text("Package index OK (%d packages) - %s, branch %s, folder %s" % (n, gitx.redact_url(idx["url"]), idx["branch"], idx["path"]))
            except Exception as e:
                sp.problem("package index unreachable [%s, branch %s]: %s" % (gitx.redact_url(idx["url"]), idx["branch"], gitx.redact_url(str(e))))
    url = telemetry.target()
    with ui.Spinner("Checking the telemetry inbox (git)") as sp:
        if not url:
            sp.problem("no telemetry inbox configured, so usage reporting is paused. run: aihub setup")
        elif gitx.reachable(url):
            sp.text("Telemetry inbox reachable (%s)" % gitx.redact_url(url))
            st = telemetry.last_status()
            if st and not st.get("ok"):
                ui.note("the last usage push FAILED: %s (events are kept and retried; try: aihub flush -v)" % (st.get("error") or "unknown error"))
        else:
            sp.problem("telemetry inbox unreachable or not writable: %s" % gitx.redact_url(url))


def cmd_welcome(a):
    """First-run onboarding. Interactive on a tty, plain and safe otherwise (-y accepts the defaults)."""
    from . import hooks, integrations
    ask = ui.INTERACTIVE and not a.yes
    ui.banner()
    ui.heading("Let's get you set up")
    from . import remote_config
    remote_config.resolve_missing(a.yes)               # env > saved > defaults.json, silently
    if ask:
        remote_config.ensure_access()                  # asks only when git credentials are missing
    _check_backends()
    tools = integrations.detected()
    if tools:
        ui.good("Found: " + ", ".join(t.label for t in tools))
    else:
        ui.note("No AI tools detected yet. Install Claude Code, Codex or OpenCode and run: aihub welcome")
    do_hooks, do_skill, do_policy = bool(tools), bool(tools), True       # the policy guard is ON unless the user unticks it
    if tools and ask:
        ui.out()
        picks = ui.select("What should I connect?", [
            ("hooks", "Usage hooks - show what your team really uses"),
            ("policy", "Policy guard - block destructive tool calls (recommended)"),
            ("skill", "Recommend the packaging skills (not installed automatically)")], multi=True, preset=("hooks", "policy", "skill"))
        do_hooks, do_skill = "hooks" in picks, "skill" in picks
        do_policy = "policy" in picks
    if do_hooks:
        for label, status in hooks.ensure():
            ui.good("%s hook: %s" % (label, status))
    from . import policy
    u = policy.user()
    if u["enabled"] != do_policy:
        u["enabled"] = do_policy
        policy.save_user(u)
    ui.good("policy guard: %s" % ("ON" if do_policy else "OFF (aihub policy on)"))
    if do_skill:
        from ..core.release import BUILTIN_PACKAGES
        ui.out()
        ui.info("recommended: " + ", ".join("aihub install " + n for n in BUILTIN_PACKAGES))
    if not paths.load("credentials.json", {}).get("token"):
        ui.out()
        ui.note("an account is optional: `aihub login` shows your name in usage reports")
    _mark_welcomed()                                    # BEFORE the offer: the `aihub install` it spawns must not run the welcome again
    if getattr(a, "offer_ui", True):
        _offer_ui(ask)
    ui.celebrate("All set")
    ui.out()
    ui.out("  " + ui.dim("Try these next"))
    for cmd, why in (("aihub install aihub-ui && aihub run aihub-ui", "local chat UI (open the address it prints)"),
                     ("aihub search", "browse what is available"), ("aihub install <name>", "add a skill, agent or MCP server"),
                     ("aihub dev init", "start publishing your own")):
        ui.out("    %s  %s" % (ui.accent("%-22s" % cmd), ui.dim(why)))
    ui.out()


def _offer_ui(ask):
    import shutil
    """welcome: guide to the local UI (service package aihub-ui): install, then run. Asks only on a terminal; never fails the welcome."""
    ui.out()
    if "aihub-ui" in installer.state()["packages"]:
        return
    ui.info("aihub-ui is a local chat UI for Claude. Install: aihub install aihub-ui   Run: aihub run aihub-ui")
    if not ask:
        return
    exe = shutil.which("aihub")
    if not exe or not ui.confirm("Install and start aihub-ui now?", True):
        return
    import subprocess
    for cmd in (["install", "aihub-ui"], ["run", "aihub-ui"]):
        try:
            if subprocess.call([exe] + cmd) != 0:
                return ui.note("`aihub %s` did not finish; run it later by hand" % " ".join(cmd))
        except OSError as e:
            return ui.note("could not run aihub: %s" % e)


def _mark_welcomed():
    try:
        paths.ensure()
        open(paths.p(".welcomed"), "w").close()
    except OSError:
        pass


def cmd_hook(a):
    """Agent hot path (PreToolUse). Spools one line and exits; never blocks, never fails the agent."""
    from . import hooks
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")      # Windows text stdin is cp1252 and chokes on UTF-8 payloads
    except Exception:
        raw = ""
    if getattr(a, "use", False):
        hooks.use_event(raw, a.source)                              # aihub-ui: chat send / work run, counted like a tool use
    else:
        hooks.handle(raw, a.source)


def cmd_usage(a):
    from . import usage
    tools = tuple(a.tool) if a.tool else usage.TOOLS
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    if not (a.json or a.plain):
        from . import hooks, usagesync
        hooks.verify()                                       # looking at usage: repair our hooks if they were edited away, and refresh the hub copy
        usagesync.kick()
    recs, meta = usage.load(tools, ui, quiet=bool(a.json or a.plain or not tty))
    rep = usage.report(tools, a.days, model=a.model, recs=recs)
    p = usage.pick(rep, a.days)                           # the window --days selects (all time when 0)
    if a.session or a.sessions or a.tips:
        ss = usage.sessions_of(p["recs"], meta)
        if a.find:
            ss = usage.find_sessions(ss, a.find)
        if a.json:
            body = {"tips": usage.tips(ss)} if a.tips else {"sessions": [{k: v for k, v in s.items() if k not in ("reqs", "notes", "text")} for s in usage.sort_sessions(ss, a.sort)]}
            if a.session:
                body = {"sessions": [dict(s, notes=usage.notes_of(s)) for s in usage.find_sessions(ss, a.session)]}
            print(json.dumps(body, default=lambda o: sorted(o)))
            return 0
        if a.tips:
            ui.heading("Efficiency tips  %s" % ui.dim(p["period"]))
            usage.show_tips(usage.tips(ss), ui)
        elif a.session:
            hit = usage.find_sessions(ss, a.session)
            if not hit:
                ui.error("no session matches %r in %s" % (a.session, p["period"].lower()), "list them: aihub usage --sessions")
                return 1
            for s in usage.sort_sessions(hit, a.sort)[:1 if len(hit) == 1 else 5]:
                usage.show_session(s, ui)
            if len(hit) > 5:
                ui.info("%d sessions match, showing 5; use a longer id" % len(hit))
        else:
            ui.heading("Sessions  %s" % ui.dim("%s, sorted by %s" % (p["period"].lower(), a.sort)))
            usage.show_sessions(usage.sort_sessions(ss, a.sort), ui, a.top)
        return 0
    if a.json:
        print(json.dumps([{k: v for k, v in dict(p, by_model=[dict(x, name="/".join(x["name"])) for x in p["by_model"]]).items() if k != "recs"} for p in rep],
                         default=lambda o: sorted(o)))
        return 0
    if not a.plain and not a.by_day and tty:
        usage.interactive(rep, ui, meta, refilter=lambda m, d: usage.report(tools, d, model=m, recs=recs), start="summary", days=a.days)
    else:
        usage.show(rep, ui, a.by_day)
    return 0


def cmd_flush(a):
    from . import telemetry
    n = telemetry.flush(force=not getattr(a, "background", False))     # --background: the detached sender, one pass per window
    if a.verbose:
        print("sent %d events" % n)
        st = telemetry.last_status()
        if st and not st.get("ok"):
            print("last push failed: %s" % (st.get("error") or "unknown error"))
            print("inbox: %s" % gitx.redact_url(telemetry.target()))


def cmd_usage_sync(a):
    from . import usagesync
    if a.background:
        usagesync.run(background=True)                     # the Stop hook's detached worker: silent
        return
    if not usagesync.enabled():
        ui.error("usage upload is off", "needs a hub (aihub setup), a signed-in user (aihub login) and usage_sync not off")
        return 1
    usagesync.run.error = ""
    n = usagesync.run(full=True)
    if usagesync.run.error:
        ui.error(usagesync.run.error, "the upload is retried after the next answer turn")
        return 1
    ui.good("sent %d changed daily rows to the hub" % n if n else "the hub already has your latest usage")


def cmd_skill(a):
    """(Re)install or remove the built-in packages (aihub-guide, aihub-package) from the hub."""
    for name, status in installer.install_builtin(remove=a.action == "remove", tools=a.tool):
        (ui.bad if status.startswith("failed") else ui.good)("%-14s %s" % (name, status))


def cmd_credential(a):
    """Git credential helper protocol (get/store/erase on stdin). Registered by `aihub setup`; not meant to be typed."""
    from . import credhelper
    return credhelper.run(a.action)


def cmd_vault(a):
    """Manage git remotes and their stored logins (interactive on a terminal)."""
    from . import vault
    rc = vault.run(a)
    if rc:
        raise SystemExit(rc)           # `vault test` fails the shell when a login does not work


def cmd_policy(a):
    """The built-in tool-call guard: list / on / off / add / remove / enable / disable / test / log / update."""
    from . import policy
    return policy.cli(a)


def cmd_hooks(a):
    from . import hooks
    res = hooks.ensure(a.tool, remove=a.action == "remove")
    if not res:
        ui.note("no supported AI tools detected")
    for label, status in res:
        ui.good("%-12s %s" % (label, status))


def cmd_menu():
    """Bare `aihub` on a terminal."""
    ui.banner()
    while True:
        choice = ui.select("What would you like to do?", [
            ("search", "Search the hub"), ("list", "Installed packages"), ("update", "Update everything"), ("upgrade", "Upgrade aihub itself"),
            ("usage", "Usage, cost and cache per tool"), ("policy", "Policy guard: on/off and rules"),
            ("doctor", "Check my setup"), ("init", "Start a new package"), ("exit", "Exit")])
        if choice in (None, "exit"):
            return
        ui.out()
        try:
            if choice == "search":
                cmd_search(argparse.Namespace(term=input("  search for (blank = all): ").strip()))
            elif choice == "list":
                cmd_list(None)
            elif choice == "update":
                cmd_update(argparse.Namespace(name=None))
            elif choice == "upgrade":
                updater.cmd_upgrade(argparse.Namespace(check=False, force=False, rollback=False))
            elif choice == "usage":
                from . import usage
                recs, meta = usage.load(usage.TOOLS, ui)
                usage.interactive(usage.report(usage.TOOLS, 30, recs=recs), ui, meta, refilter=lambda m: usage.report(usage.TOOLS, 30, model=m, recs=recs))
            elif choice == "policy":
                from . import policy
                policy.menu()
            elif choice == "doctor":
                cmd_doctor(None)
            elif choice == "init":
                dev_init(argparse.Namespace(path=".", name=None, type="skill", description=None, force=False))
        except (api.ApiError, ValueError, KeyError) as e:
            ui.error(e, ui.hint_for(e))
        ui.out()


# ---- dev
def _root(a): return os.path.abspath(a.path)


def _init_conflicts(a, d, name):
    """A package folder must not sit inside another package (the parent's archive would swallow it) nor above others (it would
    swallow them). Returns the folder to create the package in: `d`, or a better one the user picked. Raises with a ready-to-run suggestion."""
    if os.path.exists(os.path.join(d, "aihub.toml")) and not a.force:
        return d                                       # re-init of this very package: handled further down (keeps the manifest)
    rel = lambda p: os.path.relpath(p) if os.path.abspath(p).startswith(os.getcwd()) else p
    parent = pub.enclosing(d)
    if parent:
        sib = os.path.join(os.path.dirname(parent), name)
        msg = "%s is inside the package at %s (its archive would include this one)" % (rel(d), rel(parent))
        if os.path.exists(sib):
            raise ValueError("%s; use another name, or move the package out of it" % msg)
        if ui.INTERACTIVE and ui.select("%s. Where should %s go?" % (msg, name), [(sib, "next to it: %s" % rel(sib)), (None, "cancel")]):
            return sib
        raise ValueError("%s. Create it beside instead: aihub dev init %s --name %s" % (msg, rel(os.path.dirname(parent)) or ".", name)
                         + (" --sub" if os.path.dirname(parent) else ""))
    below = [p for p in pub.discover(d) if p != d] if os.path.isdir(d) else []
    if below:
        inner = os.path.join(d, name)
        msg = "%s already holds package(s) below it (%s); a package here would swallow them" % (rel(d), ", ".join(sorted(os.path.basename(p) for p in below)[:4]))
        if not os.path.exists(inner) and ui.INTERACTIVE and ui.select("%s. Create %s as its own folder instead?" % (msg, name),
                                                                        [(inner, "yes: %s" % rel(inner)), (None, "cancel")]):
            return inner
        raise ValueError("%s. Make it a sibling: aihub dev init %s --sub --name %s" % (msg, rel(d) or ".", name))
    return d


def _init_mono(a):
    """One repository, several packages. Existing packages below PATH are kept and only get their [git] refreshed;
    new ones come from --packages "a,b:mcp" (name or name:type) or are asked for."""
    from ..core import naming
    root = _root(a)
    specs = [x.strip() for v in (a.packages or []) for x in v.split(",") if x.strip()]
    adopt = list(getattr(a, "adopt", None) or [])
    if not specs and not adopt and ui.INTERACTIVE:
        raw = input("  packages to create, comma separated (name or name:type, blank for none): ").strip()
        specs = [x.strip() for x in raw.split(",") if x.strip()]
    plan = []
    for s in specs:
        n, _, t = s.partition(":")
        t = t or a.type
        if t not in M.TYPES:
            raise ValueError("%r: type must be one of %s" % (s, ", ".join(M.TYPES)))
        plan.append((naming.validate(n), t))
    if len({n for n, _ in plan}) != len(plan):
        raise ValueError("--packages lists the same name twice")
    have = {os.path.basename(p): p for p in pub.discover(root)} if os.path.isdir(root) else {}
    clash = [n for n, _ in plan if n in have and have[n] != os.path.join(root, n)]
    if clash:
        raise ValueError("%s already exists elsewhere in this workspace (%s)" % (", ".join(clash), ", ".join(have[n] for n in clash)))
    if os.path.isfile(os.path.join(root, "aihub.toml")):
        raise ValueError("%s is itself a package; a monorepo folder holds packages in sub folders. Pick a parent folder" % root)
    ws.ensure_repo(root, a.remote or "", a.branch or "main")
    for n in adopt:                                         # existing sub folders become packages in place
        q = os.path.join(root, n)
        if not os.path.isdir(q):
            raise ValueError("%s is not a folder below %s" % (n, root))
        dev_init(argparse.Namespace(**dict(vars(a), path=q, name=naming.validate(n.lower().replace("_", "-")), sub=False, mono=False,
                                           wizard=False, force=False, _mono_member=True)))
    for n, t in plan:
        if n in have:
            ui.note("%s already exists; keeping it" % n)
            continue
        dev_init(argparse.Namespace(**dict(vars(a), path=root, name=n, type=t, sub=True, mono=False, wizard=False, force=False, _mono_member=True)))
    dirs = pub.discover(root)
    for d in dirs:                                          # new and pre-existing packages alike: [git] must match this repository
        rec, changes = ws.fix_git(d, root, remote=a.remote or None)
        for c in changes:
            ui.good("%s  [git] %s" % (rec["name"], c))
    ui.celebrate("Monorepo ready: %d package(s)" % len(dirs))
    ws.show_tree(root, ws.inspect(root, dirs))
    ui.info("publish everything:  aihub dev publish %s        one package:  aihub dev publish %s --only NAME" % (a.path, a.path))


def _ask_text(label, default=""):
    ans = ui._ask("  %s %s %s " % (ui.accent("?"), label, ui.dim("[%s]" % default) if default else "")).strip()
    return ans or default


def _ask_type(default="skill"):
    types = list(M.TYPES)
    return ui.select("Package type?", [(t, t) for t in [default] + [x for x in types if x != default]]) or default


def _subdirs(root):
    """Folders directly below root that could become packages: not hidden, not build output, not already packages."""
    out = []
    for n in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        q = os.path.join(root, n)
        if (os.path.isdir(q) and not n.startswith(".") and n not in pub.SKIP_DIRS
                and not os.path.exists(os.path.join(q, "aihub.toml")) and not pub.discover(q, 3)):
            out.append(n)
    return out


def _init_wizard(a):
    """`dev init -w`: ask what to make of PATH, then run the same code paths the flags use."""
    from ..core import naming
    if not ui.INTERACTIVE:
        raise ValueError("--wizard needs a terminal; pass --mono / --sub / --name / --type instead")
    root = _root(a)
    here = os.path.basename(root).lower().replace("_", "-")
    ui.heading("aihub dev init wizard")
    ui.info("folder: %s" % root)
    mode = ui.select("What should this folder become?", [
        ("single", "One package: this folder is the package"),
        ("mono", "A monorepo: this folder is one git repository, each sub folder a package"),
        ("sub", "Add one more package inside this workspace (new sub folder)")])
    if not mode:
        raise ValueError("cancelled")
    ns = dict(vars(a), wizard=False)
    if mode == "single":
        name = _ask_text("Package name?", naming.validate(here) if _valid(naming, here) else "")
        ns.update(name=name, type=_ask_type(a.type), description=_ask_text("Description?", a.description or ""), sub=False, mono=False)
        return dev_init(argparse.Namespace(**ns))
    if mode == "sub":
        name = _ask_text("New package name?")
        ns.update(name=name, type=_ask_type(a.type), description=_ask_text("Description?", a.description or ""), sub=True, mono=False)
        return dev_init(argparse.Namespace(**ns))
    adopt = []
    subs = _subdirs(root)
    if subs:
        adopt = ui.select("Turn which existing sub folders into packages?", [(n, n) for n in subs], multi=True, preset=tuple(subs))
    packages = []
    if ui.confirm("Create new (empty) packages as well?", default=not adopt):
        raw = _ask_text("New packages, comma separated (name or name:type)?")
        packages = [raw] if raw else []
    kind = a.type
    if adopt:
        kind = _ask_type(a.type)
        ui.info("existing sub folders become '%s' packages (change each type in its aihub.toml)" % kind)
    remote = a.remote or _ask_text("Git remote URL for the whole repository (blank to set later)?", "")
    branch = a.branch or _ask_text("Branch?", "main")
    ns.update(mono=True, packages=packages, adopt=adopt, remote=remote or None, branch=branch, type=kind)
    return _init_mono(argparse.Namespace(**ns))


def _valid(naming, n):
    try:
        naming.validate(n)
        return True
    except ValueError:
        return False


def dev_init(a):
    if getattr(a, "wizard", False):
        return _init_wizard(a)
    if getattr(a, "mono", False):
        return _init_mono(a)
    from ..core import templates
    d = _root(a)
    from ..core import naming
    if a.sub:                                          # PATH is a workspace folder: the package gets its own folder inside it
        if not a.name:
            raise ValueError("--sub needs --name: the package is created in PATH/<name>")
        d = os.path.join(d, naming.validate(a.name))
    name = naming.validate(a.name or os.path.basename(d).lower().replace("_", "-"))
    d = _init_conflicts(a, d, name)
    os.makedirs(d, exist_ok=True)
    if getattr(a, "_mono_member", False):
        pass                                           # the workspace repository was set up once by --mono
    f = os.path.join(d, "aihub.toml")
    existing = os.path.exists(f) and not a.force
    made = []
    if not existing:
        from . import identity
        who = identity.current()                      # signed-in hub username, else the local OS user name
        manifest, files, execs = templates.render(a.type, name, a.description or "", [who["local_user"] if who["anonymous"] else who["user"]])
        with open(f, "w") as fh:
            fh.write(manifest)
        made.append("aihub.toml")
    else:
        files, execs = {}, set()
        ui.note("aihub.toml already exists; keeping it (use --force to regenerate starter files)")
    for rel, content in files.items():
        p = os.path.join(d, rel)
        if os.path.exists(p) and not a.force:
            continue
        os.makedirs(os.path.dirname(p) or d, exist_ok=True)
        with open(p, "w", newline="\n") as fh:
            fh.write(content)
        if rel in execs:
            os.chmod(p, 0o755)
        made.append(rel)
    if pub.ensure_gitignore(d):
        made.append(".gitignore")
    m = M.parse(open(f, encoding="utf-8").read())
    if not (m["git"].get("url") and m["git"].get("branch")) and not getattr(a, "_mono_member", False):
        try:
            git = pub.detect(d, m["git"])
            pub.write_git_table(f, git)
            made.append("[git] in aihub.toml")
        except ValueError as e:
            ui.note("%s - set it before publishing" % e)
    ui.celebrate("Project ready: %s" % name)
    ui.tree(d, made)
    ui.out()
    ui.info("next: edit the files, then  aihub dev validate  &&  aihub dev publish")


def _load(a):
    with open(os.path.join(_root(a), "aihub.toml")) as f:
        return M.parse(f.read())


def dev_validate(a, quiet=False):
    m = _load(a)
    errs, warns = M.lint(m, _root(a))
    nested = []
    for n in sorted(os.listdir(_root(a))):                # packages below this one would ship inside its archive
        q = os.path.join(_root(a), n)
        if os.path.isdir(q) and not n.startswith(".") and n not in pub.SKIP_DIRS:
            nested += pub.discover(q, 3)
    if nested:
        warns.append("this package folder also contains other packages (%s); their files ship inside this one. Move them beside it"
                     % ", ".join(os.path.relpath(p, _root(a)) for p in nested[:3]))
    for f, got, v in pysync.check(_root(a), pysync.want(m)):
        errs.append("pyproject.toml %s %r does not match aihub.toml %r (fix with: aihub dev doctor)" % (f, got, v))
    for w in warns:
        ui.note(w)
    for e in errs:
        ui.bad(e)
    if errs:
        raise ValueError("%d problem(s) in the project; fix them and run `aihub dev validate` again" % len(errs))
    if not quiet:
        ui.good("%s %s (%s) looks good" % (m["package"]["name"], m["package"]["version"], m["package"]["type"]))
    return m


def dev_build(a):
    m = dev_validate(a, quiet=True)
    out = os.path.join(_root(a), "dist")
    os.makedirs(out, exist_ok=True)
    dest = os.path.join(out, "%s-%s.tar.gz" % (m["package"]["name"], m["package"]["version"]))
    files = [f for f in ignore.list_files(_root(a)) if not f.startswith("dist/")]
    with ui.Spinner("Packing %d files" % len(files)):
        archive.build(_root(a), files, dest)
    ui.good("Built %s" % dest)
    return dest


def _projects(a):
    """Package folders this command works on. PATH is a package (has aihub.toml) or a workspace folder holding several;
    --only NAME narrows a workspace to some (matched on folder name, relative path or package name)."""
    root = _root(a)
    found = pub.discover(root)
    if not found:
        raise ValueError("no aihub.toml in %s or the folders below it; run `aihub dev init <folder>` to start a package" % root)
    only = [x.strip().strip("/") for v in (getattr(a, "only", None) or []) for x in v.split(",") if x.strip()]
    if not only:
        return found
    def names(d):
        try:
            pkg = M.parse(open(os.path.join(d, "aihub.toml"), encoding="utf-8").read())["package"]["name"]
        except (OSError, ValueError, KeyError):
            pkg = ""
        return {os.path.basename(d), os.path.relpath(d, root).replace(os.sep, "/"), pkg}
    picked = [d for d in found if names(d) & set(only)]
    missing = [x for x in only if not any(x in names(d) for d in found)]
    if missing:
        raise ValueError("no package matches %s; found: %s" % (", ".join(missing), ", ".join(sorted(os.path.basename(d) for d in found))))
    return picked


def _label(a, d):
    return os.path.relpath(d, _root(a)).replace(os.sep, "/") if d != _root(a) else os.path.basename(d)


def _each(fn):
    """Run a single-package dev command on every package the PATH / --only selection covers; report failures at the end."""
    @functools.wraps(fn)
    def run(a):
        roots = _projects(a)
        if len(roots) == 1:
            return fn(argparse.Namespace(**dict(vars(a), path=roots[0])))
        failed = []
        for d in roots:
            ui.out()
            ui.info("== %s" % _label(a, d))
            try:
                fn(argparse.Namespace(**dict(vars(a), path=d)))
            except (api.ApiError, ValueError, KeyError, OSError) as e:
                ui.bad(str(e))
                failed.append(_label(a, d))
        if failed:
            raise ValueError("%d of %d packages failed: %s" % (len(failed), len(roots), ", ".join(failed)))
    return run


def dev_list(a):
    """The packages in this workspace, with their version and whether they have unpublished changes."""
    for d in _projects(a):
        try:
            pkg = M.parse(open(os.path.join(d, "aihub.toml"), encoding="utf-8").read())["package"]
        except (OSError, ValueError) as e:
            ui.bad("%s: %s" % (_label(a, d), e))
            continue
        dirty = pub.toplevel(d) and gitx.run(["status", "--porcelain", "--", "."], cwd=d)
        ui.out("  %-24s %-9s %-6s %s%s" % (pkg["name"], pkg["version"], pkg["type"], _label(a, d), "  " + ui.dim("(uncommitted changes)") if dirty else ""))


def dev_tree(a):
    """The workspace as a tree: each package, its repository, and whether its [git] is in sync."""
    roots = _projects(a)
    recs = ws.inspect(_root(a), roots)
    bad = ws.show_tree(_root(a), recs)
    if not bad:
        ui.out()
        ui.good("%d package(s), everything in sync" % len(recs))


def dev_doctor(a):
    """Make every package's [git] (url, branch, subdir) match the repository it is really in. Blocking problems are listed, not guessed."""
    root = _root(a)
    dirs = _projects(a)
    ui.out()
    stuck = pydrift = 0
    for d in dirs:
        try:
            rec, changes = ws.fix_git(d, root, remote=a.remote, write=not a.check)
        except (gitx.GitError, OSError, ValueError) as e:
            ui.bad("%s: %s" % (os.path.basename(d), e))
            stuck += 1
            continue
        if changes:
            for c in changes:
                (ui.note if a.check else ui.good)("%s  [git] %s%s" % (rec["name"], c, "  (would change)" if a.check else ""))
        elif not ws.blocking(rec):
            ui.good("%s  [git] already right (%s @ %s)" % (rec["name"], rec["actual"].get("url") or "no remote yet", rec["actual"].get("branch") or "-"))
        for code, text in ws.blocking(rec):
            ui.bad("%s  %s" % (rec["name"], text))
            stuck += 1
        try:
            want = pysync.want(_load(argparse.Namespace(**dict(vars(a), path=d))))
            for f, got, v in (pysync.check if a.check else pysync.sync)(d, want):
                (ui.note if a.check else ui.good)("%s  pyproject.toml %s %r -> %r%s" % (rec["name"], f, got, v, "  (would change)" if a.check else ""))
                pydrift += 1
        except (OSError, ValueError, KeyError) as e:
            ui.bad("%s: pyproject.toml: %s" % (os.path.basename(d), e))
            stuck += 1
    ui.out()
    if stuck:
        ui.info("nested repositories:  aihub dev tree-solve      overview:  aihub dev tree")
        raise ValueError("%d problem(s) need your decision" % stuck)
    if a.check and any(ws.fix_git(d, root, write=False)[1] for d in dirs):
        raise ValueError("[git] is out of date; run `aihub dev doctor` to fix it")
    if pydrift:
        raise ValueError("pyproject.toml differs from aihub.toml; run `aihub dev doctor` to fix it")


def dev_tree_solve(a):
    """Resolve nested repositories, then re-sync [git]. Asks per folder on a terminal; --nested merge|keep answers for all."""
    root = _root(a)
    dirs = _projects(a)
    resolved, left = ws.solve(root, dirs, mode=a.nested, remote=a.remote)
    ui.out()
    if left:
        raise ValueError("%d problem(s) still open; see `aihub dev tree`" % left)
    ui.good("workspace is clean" if not resolved else "resolved %d nested repositor%s" % (resolved, "y" if resolved == 1 else "ies"))


def _bump_version(path, part):
    """Raise `version` in the [package] table of aihub.toml (any spacing, single or double quotes). -> (old, new)."""
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    head = re.search(r"^[ \t]*\[package\][ \t]*(?:#.*)?$", text, re.M)
    start = head.end() if head else 0
    nxt = re.search(r"^[ \t]*\[", text[start:], re.M)
    end = start + nxt.start() if nxt else len(text)
    found = []

    def sub(m):
        found.append(m.group(3))
        return m.group(1) + m.group(2) + V.bump(m.group(3), part) + m.group(2)
    body = re.sub(r"^([ \t]*version[ \t]*=[ \t]*)(['\"])([^'\"\n]*)\2", sub, text[start:end], count=1, flags=re.M)
    if not found:
        raise ValueError('cannot bump: no  version = "..."  line in the [package] table of %s' % path)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text[:start] + body + text[end:])
    return found[0], V.bump(found[0], part)


def _bump_and_sync(a, d, label, with_path):
    """--bump (rewrites the version in aihub.toml), then pyproject.toml follows aihub.toml."""
    if a.bump:
        old, new = _bump_version(os.path.join(d, "aihub.toml"), a.bump)
        ui.good("%s: version %s -> %s" % (label, old, new))
    for f, got, v in pysync.sync(d, pysync.want(_load(with_path(d)))):
        ui.good("%s: pyproject.toml %s %r -> %r" % (label, f, got, v))


def _publish_direct(a, roots):
    """`dev publish -i`: pack each package and upload the archive to the hub, which keeps it in its own data folder and lists it.
    No git repository, remote or git account is needed. Packages are independent: a failure is reported, the rest still go out."""
    import shutil, tempfile
    with_path = lambda d: argparse.Namespace(**dict(vars(a), path=d))
    failed, sent = [], []
    for n, d in enumerate(roots, 1):
        label = _label(a, d)
        tag = "[%d/%d] " % (n, len(roots)) if len(roots) > 1 else ""
        sp = ui.Spinner("%sPreparing %s" % (tag, label))
        sp.__enter__()
        tmp = tempfile.mkdtemp(prefix="aihub-publish-")
        try:
            _bump_and_sync(a, d, label, with_path)
            m = dev_validate(with_path(d), quiet=True)
            pkg = m["package"]
            files = [f for f in ignore.list_files(d, upto=_root(a)) if not f.startswith("dist/")]     # ignore files between the package and the workspace apply
            dest = os.path.join(tmp, "%s-%s.tar.gz" % (pkg["name"], pkg["version"]))
            archive.build(d, files, dest)
            sp.text("%sUploading %s %s (%s)" % (tag, pkg["name"], pkg["version"], ui.human(os.path.getsize(dest))))
            last = [-1]

            def prog(done, total, pkg=pkg, tag=tag):                      # chunks of a big archive finish in any order: show the bytes that are in
                pct = int(100 * done / total) if total else 100
                if pct != last[0]:
                    last[0] = pct
                    sp.text("%sUploading %s %s (%s of %s, %d%%)" % (tag, pkg["name"], pkg["version"], ui.human(done), ui.human(total), pct))
            res = api.publish_direct(dest, progress=prog)
            sp.text("%sSent %s %s" % (tag, pkg["name"], pkg["version"]))
            sp.__exit__(None, None, None)
            sent.append((pkg, res))
        except (api.ApiError, ValueError, KeyError, OSError) as e:
            sp.text("%s: %s" % (label, e))
            sp.__exit__(ValueError, e, None)
            failed.append(label)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    for pkg, res in sent:
        if res.get("status") == "already published":
            ui.good("%s %s is already on the hub with these files (nothing to do)" % (pkg["name"], pkg["version"]))
            continue
        ui.celebrate("Published %s %s to the hub" % (pkg["name"], pkg["version"]))
        ui.info("stored on the hub (%s, sha256 %s), no git involved" % (res.get("status", "published"), str(res.get("sha256", ""))[:12]))
        ui.info("install: aihub install %s" % pkg["name"])
        if res.get("status") == "queued":
            ui.note("the hub will list it in a moment")
    if failed:
        raise ValueError("%d of %d package(s) not published: %s%s" % (len(failed), len(roots), ", ".join(failed),
                                                                 "; the other %d went out" % len(sent) if sent else ""))


def dev_publish(a):
    """Publish each selected package on its own: a problem in one is reported and skipped, the others still go out."""
    roots = _projects(a)
    top_root = _root(a)
    if len(roots) > 1 and not a.only:
        roots = ui.select("Publish which packages?", [(d, _label(a, d)) for d in roots], multi=True, preset=tuple(roots))
        if not roots:
            return ui.note("nothing selected")
    if getattr(a, "indirect", False):
        return _publish_direct(a, roots)
    with_path = lambda d: argparse.Namespace(**dict(vars(a), path=d))
    failed, ready = [], []
    for n, d in enumerate(roots, 1):                 # phase 1: prepare. Nothing is pushed yet
        label = _label(a, d)
        tag = "[%d/%d] " % (n, len(roots)) if len(roots) > 1 else ""
        sp = ui.Spinner("%sPreparing %s" % (tag, label))
        sp.__enter__()
        try:
            ws.fix_git(d, top_root)                  # [git] follows the repository's real remote / branch / folder
            block = ws.blocking(ws.inspect_package(d, top_root))
            if block:
                raise ValueError("%s (fix with: aihub dev tree-solve)" % block[0][1])
            _bump_and_sync(a, d, label, with_path)               # --bump, then pyproject.toml follows aihub.toml
            m = dev_validate(with_path(d), quiet=True)
            sp.text("%sStaging %s" % (tag, label))
            ready.append((d, pub.publish(d, m, push_now=False, commit_now=False)))      # staged only: one commit per repository below
            sp.text("%sPrepared %s" % (tag, label))
            sp.__exit__(None, None, None)
        except (api.ApiError, ValueError, KeyError, OSError, gitx.GitError) as e:
            sp.text("%s: %s" % (label, e))
            sp.__exit__(ValueError, e, None)
            failed.append(label)
    pushed = []
    repos = {}
    for d, info in ready:
        repos.setdefault(pub.toplevel(d), []).append((d, info))
    for top, items in repos.items():                 # phase 2: ONE commit and one push per repository
        try:
            with ui.Spinner("Committing %s" % ", ".join(i["package"] for _, i in items)):
                pub.commit_staged(top, items)
            pub.push(items[0][0])
            pushed += items
        except (gitx.GitError, ValueError) as e:
            ui.bad("push to %s failed: %s" % (gitx.redact_url(items[0][1]["git_url"]), e))
            failed += [_label(a, d) for d, _ in items]
    queued = False
    for d, info in pushed:
        with ui.Spinner("Telling the hub about %s %s" % (info["package"], info["version"])) as sp:
            sent = pub.report(info)
            sp.text("Told the hub about %s %s" % (info["package"], info["version"])) if sent else sp.problem("Hub not reached for %s (queued)" % info["package"])
        queued = not sent or queued
        ui.celebrate("Published %s %s" % (info["package"], info["version"]))
        ui.info("repo:    %s @ %s (%s)" % (info["git_url"], info["git_branch"], info["commit"][:8]))
        ui.info("install: aihub install %s" % info["package"])
    if pushed and not queued:
        ui.note("publish event sent; the hub will add it to the package index shortly")
    if queued:
        ui.note("WARNING: the telemetry inbox is not reachable, so the hub has NOT been told about this publish and the package is not in the index yet (%s)"
                % (pub.report.error or "unknown error"))
        ui.info("the event is queued and goes out when the inbox is back (aihub flush), then the hub indexes it. To skip the wait: aihub dev index")
    if failed:
        raise ValueError("%d of %d package(s) not published: %s%s" % (len(failed), len(roots), ", ".join(failed),
                                                                 "; the other %d went out" % len(pushed) if pushed else ""))


def cmd_unpublish(a):
    name, _, ver = a.name.partition("@")
    what = "%s %s" % (name, ver) if ver else "%s (all versions)" % name
    if not a.yes and not (ui.INTERACTIVE and ui.confirm("Remove %s from the hub? Installed copies stay, new installs stop." % what, False)):
        raise ValueError("not confirmed; pass --yes to unpublish without asking")
    api.call("DELETE", "/packages/%s%s" % (urllib.parse.quote(name), "?version=" + urllib.parse.quote(ver) if ver else ""))
    ui.good("unpublished " + what)


def dev_index(a):
    """Ask the hub to index this project now (needs the `index` permission). For a publish event that was lost."""
    root = _root(a)
    m = dev_validate(a, quiet=True)
    git = pub.detect(root, m["git"])
    pkg = m["package"]
    top = pub.toplevel(root)
    commit = a.commit or (gitx.run(["rev-parse", "HEAD"], cwd=top, check=False) if top else "")
    if not commit:
        raise api.ApiError("this project is not a git repository with a commit; publish it first (aihub dev publish)")
    if top and gitx.run(["status", "--porcelain", "--", "."], cwd=root) and not a.commit:
        ui.note("you have uncommitted changes; the hub reads the pushed commit %s, not your working files" % commit[:8])
    if top and not a.commit and gitx.run(["branch", "-r", "--contains", commit], cwd=top, check=False) == "":
        raise api.ApiError("commit %s is not pushed yet; run: git push (or aihub dev publish)" % commit[:8])
    body = {"package": pkg["name"], "version": pkg["version"], "git_url": gitx.redact_url(git["url"]),
            "git_branch": git["branch"], "git_subdir": git.get("subdir", ""), "commit": commit}
    with ui.Spinner("Asking the hub to index %s %s" % (pkg["name"], pkg["version"])):
        try:
            r = api.call("POST", "/index/package", body)
        except api.ApiError as e:
            if "403" in str(e) or "missing permission" in str(e):
                raise api.ApiError("your account may not force an index; an admin can grant the 'index' permission to your role (%s)" % e)
            raise
    ui.good("%s %s: %s (commit %s)" % (r["package"], r["version"], r["status"], commit[:8]))


def dev_stats(a):
    """Usage of this package, read from the hub (the same events its dashboard shows)."""
    name = _load(a)["package"]["name"]
    with ui.Spinner("Reading usage from the hub"):
        d = api.call("GET", "/packages/%s/stats?days=30" % name)
    by, users = d.get("by_kind") or {}, d.get("active_users", 0)
    if not ui.TTY:
        print(json.dumps(d, indent=2))
        return
    ui.card("%s - last 30 days" % name,
            ["%-10s %s" % (k, v) for k, v in sorted(by.items())] or ["no events yet"],
            ui.dim("active users: ") + str(users))


class _CommandGroupAction(argparse.Action):
    def __init__(self, title, actions):
        super().__init__([], title, nargs=0, metavar=title, help=None)
        self.actions = actions

    def _get_subactions(self):
        return self.actions


class _GroupedSubParsersAction(argparse._SubParsersAction):
    def _get_subactions(self):
        groups = getattr(self, "command_groups", None)
        if not groups:
            return super()._get_subactions()
        by_name = {action.dest: action for action in self._choices_actions}
        return [_CommandGroupAction(title, [by_name[name] for name in names])
                for title, names in groups]


def build_parser():
    class HelpFormatter(argparse.RawDescriptionHelpFormatter):
        pass

    ap = argparse.ArgumentParser(
        prog="aihub", formatter_class=HelpFormatter,
        description=("Search the AI Hub catalog, install packages into supported AI tools, "
                     "and develop or publish packages of your own."),
        epilog=("Examples:\n"
                "  aihub search sql\n"
                "  aihub install my-skill\n"
                "  aihub install \"my-skill>=1.2\"\n"
                "  aihub download my-skill@1.0.0 -o ./pkgs\n"
                "  aihub dev publish --bump patch\n\n"
                "Config and credentials: ~/.aihub/config.json and ~/.aihub/credentials.json "
                "(or $AIHUB_HOME); hub URL key: hub (default from AIHUB_URL). "
                "Docs: <hub URL>/#/docs/publishing."))
    from ..core.release import VERSION as _V
    ap.add_argument("-v", "--version", action="version", version="aihub %s" % _V)
    ap.add_argument("--json", dest="json_out", action="store_true", help="Any command: one JSON document on stdout (same as the command's own --json, or AIHUB_JSON=1).")
    ap.register("action", "parsers", _GroupedSubParsersAction)
    sp = ap.add_subparsers(dest="cmd", metavar="command")
    sp.command_groups = [
        ("Discover", ["search", "info"]),
        ("Install", ["install", "uninstall", "update", "list", "download", "unpublish"]),
        ("Manage", ["enable", "disable", "tree", "lock", "sync"]),
        ("Account", ["config", "login", "logout", "password", "register", "vault", "admin"]),
        ("Publish", ["dev", "space"]),
        ("Setup and maintenance", ["run", "onboard", "knowledge", "soul", "policy", "welcome", "doctor", "version", "upgrade", "auto-update", "hook", "flush", "usage-sync", "hooks", "skill", "self-uninstall"]),
    ]

    def add(name, fn, help, description, epilog, *args):
        p = sp.add_parser(name, help=help, description=description, epilog=epilog,
                          formatter_class=HelpFormatter)
        for a in args:
            p.add_argument(*a[0], **a[1])
        if not any("--json" in a[0] for a in args):
            p.add_argument("--json", action="store_true", help=JSON_HELP)
        p.set_defaults(fn=fn)
        return p

    arg = lambda *n, **k: (n, k)
    PLAIN = (arg("--plain", action="store_true", help="One 'name version' line per package, nothing else (for scripts)."),
             arg("--name-only", action="store_true", help="One package name per line, nothing else (implies --plain)."))
    add("config", cmd_config, "Show or change CLI configuration.",
        "Print the current CLI configuration, or set a configuration value. The hub URL is stored under the key 'hub'. Self-update keys: auto_update (on|notify|off) and auto_update_minutes (1..43200, default 1; the older auto_update_hours still works).",
        "Examples:\n  aihub config\n  aihub config set hub https://hub.example.com",
        arg("action", nargs="?", default="show", metavar="ACTION", help="'show' (default) or 'set'."),
        arg("key", nargs="?", metavar="KEY", help="Configuration key to change when ACTION is 'set'."),
        arg("value", nargs="?", metavar="VALUE", help="New value for KEY when ACTION is 'set'."))
    add("login", cmd_login, "Sign in and save an access token.",
        "Sign in to the configured hub. If USERNAME is omitted, the CLI prompts for it; the password is entered without echo and the token is saved locally.",
        "Examples:\n  aihub login\n  aihub login sam",
        arg("username", nargs="?", metavar="USERNAME", help="Account username; prompted for when omitted."),
        arg("--password-stdin", action="store_true", help="Read the password from standard input (scripts, web UIs); needs USERNAME."))
    add("logout", cmd_logout, "Sign out and forget the saved token.",
        "Delete the saved access token. Usage events are anonymous afterwards; the hub still groups them with your account by user name when the OS user name matches.",
        "Example:\n  aihub logout")
    add("password", cmd_password, "Change your hub password.",
        "Change the password of the signed-in account. Prompts (without echo) for the current password and the new one twice. The hub signs your other devices out; the token saved on this machine stays valid. Needs 'aihub login' first; a forgotten password is reset by an administrator.",
        "Examples:\n  aihub password\n  printf '%s\\n%s\\n' \"$OLD\" \"$NEW\" | aihub password --password-stdin",
        arg("--password-stdin", action="store_true", help="Read the current password (line 1) and the new one (line 2) from standard input."))
    add("admin", cmd_admin, "Manage accounts, groups and roles on the hub (admin).",
        "Administer the hub you are signed in to. Areas: 'users' (list [QUERY], role ROLE USER..., status STATE USER..., reset-password USER..., delete USER..., create FILE, batch FILE), "
        "'groups' (list, show G, create G..., describe G, delete G..., add G USER... [--manager], remove G USER..., manager G USER..., unmanager G USER...) and 'roles' (list, perms, show R, create R, set R, delete R). "
        "USER lists can also come from a file or stdin (-f FILE, '-' = stdin, one name per line or a sheet whose first column is the username). "
        "Every item is tried; failures are listed at the end and the exit status is 1. --dry-run shows what would change. Needs 'aihub login' as an administrator.",
        "Examples:\n  aihub admin users list sam\n  aihub admin users role editor -f people.txt\n  aihub admin users status disabled alice bob\n"
        "  aihub admin users create new-hires.csv --role user\n  aihub admin users batch changes.csv --mode replace --dry-run\n"
        "  aihub admin groups create backend data\n  aihub admin groups add backend alice bob -f more.csv\n  aihub admin groups remove backend carol\n"
        "  aihub admin groups manager backend alice\n  aihub admin groups unmanager backend alice\n"
        "  aihub admin roles create reviewer --perm view_usage,audit\n  aihub admin roles set reviewer --add-perm reset_password",
        arg("area", choices=["users", "groups", "roles"], help="What to manage."),
        arg("action", nargs="?", metavar="ACTION", help="Default: list. users: list|role|status|reset-password|delete|create|batch; groups: list|show|create|describe|delete|add|remove|manager|unmanager; roles: list|perms|show|create|set|delete."),
        arg("items", nargs="*", metavar="ARG", help="Names, ROLE/STATE/GROUP first where the action takes one (see examples)."),
        arg("-f", "--file", metavar="FILE", help="Read usernames (or, for 'users create'/'users batch', the sheet) from FILE; '-' = stdin."),
        arg("--dry-run", action="store_true", help="Show what would change; change nothing."),
        arg("--yes", action="store_true", help="delete: do not ask for confirmation."),
        arg("--json", action="store_true", help="list/show: print JSON."),
        arg("--role", default="user", help="users create: role for rows without one (default user)."),
        arg("--out", default="new-accounts.csv", help="users create: file for the generated passwords (default new-accounts.csv, mode 0600, never overwritten)."),
        arg("--mode", choices=["add", "replace"], default="add", help="users batch: 'add' only adds to the listed groups (default); 'replace' makes the listed groups the person's whole membership."),
        arg("--manager", action="store_true", help="groups add: also make the added people managers of the group."),
        arg("--description", help="groups create/describe, roles create/set: description text."),
        arg("--perm", help="roles create/set: comma list of permissions (set replaces the list)."),
        arg("--add-perm", help="roles set: permissions to add."),
        arg("--remove-perm", help="roles set: permissions to remove."))
    add("register", cmd_register, "Create an account on the configured hub.",
        "Prompt for a username and password and create an account on the configured hub. Registration is subject to the hub's signup policy; sign in separately with 'aihub login'.",
        "Examples:\n  aihub register\n  printf %s \"$PW\" | aihub register sam --password-stdin",
        arg("username", nargs="?", metavar="USERNAME", help="Account name; prompted for when omitted."),
        arg("--password-stdin", action="store_true", help="Read the password from standard input (no confirmation prompt); needs USERNAME."))
    add("search", cmd_search, "Search the hub for packages.",
        "Search package names and descriptions on the configured hub. Omitting TERM lists the available packages; at most 30 results are shown.",
        "Examples:\n  aihub search sql\n  aihub search --plain\n  aihub search sql --name-only",
        arg("term", nargs="?", default="", metavar="TERM", help="Words to search for; omit to browse packages."),
        *PLAIN)
    add("info", cmd_info, "Show package details and available versions.",
        "Fetch package metadata, including its description, type, latest version, tags, requirements, and recent versions. Non-interactive output is JSON.",
        "Examples:\n  aihub info my-skill\n  aihub info my-mcp-server",
        arg("name", metavar="PACKAGE", help="Package name on the configured hub."))
    add("install", cmd_install, "Install a package and register its components.",
        "Resolve and install a skill, agent, MCP server, tool, or setup package. Install version constraints use comparison operators, for example 'name>=1.0' or 'name>=1.0,<2'; quote comma-separated constraints for the shell. Use 'name==1.2.0' for an exact install version. The 'name@1.2.0' shorthand is supported by download. The CLI verifies the archive, installs declared dependencies, and can register components with detected tools. Interactive installs ask before scripts and tool registration; --yes accepts those prompts' defaults.",

        "Examples:\n  aihub install my-skill\n  aihub install \"my-skill>=1.2\"\n  aihub install a b \"c>=1.0\" --yes\n  aihub install -r requirements.txt\n  aihub install my-skill --yes --tool claude",
        arg("name", nargs="*", metavar="PACKAGE[<SPEC>]", help="One or more package names, each optionally followed by a version constraint such as name>=1.0,<2."),
        arg("-r", "--requirement", action="append", metavar="FILE", help="Install every package listed in FILE (pip style: one per line, # comments, nested -r); repeatable."),
        arg("-y", "--yes", action="store_true", help="Skip install-script and tool-registration prompts; use detected tools unless --tool is given."),
        arg("--tool", action="append", metavar="TOOL", help="Register components with this tool; repeat to select multiple (claude, codex, or opencode)."),
        arg("-i", "--indirect", action="store_true", help="Get the package through the hub instead of straight from git (needs the install_indirect permission). The hub does all git work with its own login, so no git credentials are needed on this machine; it never falls back to git."),
        arg("--from", dest="source", choices=["archive", "repo"], metavar="archive|repo", help="Where to get the package: 'archive' = download it from the hub (same as -i), 'repo' = clone it from its git repository. Default: a package published to the hub is an archive, any other comes from git. An archive package has no repository, so --from repo fails for it."))
    add("enable", cmd_enable, "Switch a disabled package back on.",
        "Re-register an installed but disabled package with your AI tools and put its commands back on PATH. Uses the files already on disk, so it works offline. Disabled dependencies are enabled first. 'aihub install' on a disabled package does the same.",
        "Examples:\n  aihub enable my-skill\n  aihub enable --all",
        arg("names", nargs="*", metavar="PACKAGE", help="Installed package(s) to enable."),
        arg("--all", action="store_true", help="Enable every disabled package."),
        arg("-y", "--yes", action="store_true", help="Skip prompts; register with all detected tools."),
        arg("--tool", action="append", metavar="TOOL", help="Register with this tool; repeat to select multiple."))
    add("disable", cmd_disable, "Switch a package off without removing it.",
        "Remove a package's skills, agents and MCP servers from your AI tools, undo its setup steps, and take its commands off PATH. The files stay, so 'aihub enable' is instant. Refuses while an enabled package depends on it unless --force.",
        "Examples:\n  aihub disable my-skill\n  aihub disable --all",
        arg("names", nargs="*", metavar="PACKAGE", help="Installed package(s) to disable."),
        arg("--all", action="store_true", help="Disable every enabled package."),
        arg("--force", action="store_true", help="Disable even if other enabled packages depend on it."))
    add("tree", cmd_tree, "Show the dependency tree.",
        "Print installed packages and what they depend on. With a package name, show just that package; with --remote, resolve it on the hub and show what installing it would add (new / already installed).",
        "Examples:\n  aihub tree\n  aihub tree my-skill\n  aihub tree \"my-skill>=1.2\" --remote",
        arg("name", nargs="?", metavar="PACKAGE", help="Package (with --remote, optionally with a version constraint)."),
        arg("--remote", action="store_true", help="Resolve on the hub instead of reading what is installed."))
    add("lock", cmd_lock, "Write a lock file of the exact installed versions.",
        "Record every installed package with its exact version, checksum, dependencies and enabled state in aihub.lock (JSON). Commit it so a teammate or CI can reproduce the same set with 'aihub sync'.",
        "Examples:\n  aihub lock\n  aihub lock --check",
        arg("-f", "--file", default="aihub.lock", metavar="FILE", help="Lock file path (default: aihub.lock)."),
        arg("--check", action="store_true", help="Do not write; fail if the file does not match what is installed."))
    add("sync", cmd_sync, "Make this machine match a lock file.",
        "Install the exact versions in the lock file (verifying checksums), enable or disable packages as locked, and optionally remove packages that are not in it. Dependencies are resolved in one request and archives download in parallel.",
        "Examples:\n  aihub sync\n  aihub sync --check\n  aihub sync --yes --prune",
        arg("-f", "--file", default="aihub.lock", metavar="FILE", help="Lock file path (default: aihub.lock)."),
        arg("--check", action="store_true", help="Only report differences; exit 1 if not in sync."),
        arg("--prune", action="store_true", help="Also uninstall packages that are not in the lock file."),
        arg("-y", "--yes", action="store_true", help="Do not ask before applying; use detected tools."),
        arg("--tool", action="append", metavar="TOOL", help="Register with this tool; repeat to select multiple."))
    add("download", cmd_download, "Download a package archive without installing it.",
        "Resolve a package version and save it in the output folder: the .tar.gz for a package published to the hub (or with --archive), the unpacked files for a git package (or with --folder). Large archives download in parallel chunks, resume after an interruption and are verified with SHA-256 before the final file is saved. Use name@1.2.0 for an exact version, or comparison constraints such as name>=1.0,<2.",
        "Examples:\n  aihub download my-skill@1.0.0 -o ./pkgs\n  aihub download \"my-skill>=1.0,<2\"\n  aihub download my-skill --folder",
        arg("name", metavar="PACKAGE[VERSION]", help="Package name, name@1.2.0, or a constraint such as name>=1.0,<2."),
        arg("-o", "--output", metavar="DIR", help="Folder to save into (default: current directory)."),
        arg("--archive", action="store_true", help="Save NAME-VERSION.tar.gz (the default for a package published to the hub). For a git package the hub builds it (needs install_indirect)."),
        arg("--folder", action="store_true", help="Save the unpacked files in NAME-VERSION/ (the default for a git package)."),
        arg("--from", dest="source", choices=["archive", "repo"], metavar="archive|repo", help="'archive' = through the hub, 'repo' = git only (fails for an archive package)."))
    add("uninstall", cmd_uninstall, "Remove an installed package and undo its changes.",
        "Remove the named installed package, its files and virtual environment, and revert recorded tool registrations and setup changes.",
        "Example:\n  aihub uninstall my-skill",
        arg("name", metavar="PACKAGE", help="Name of the installed package to remove."),
        arg("--force", action="store_true", help="Do not ask when other packages depend on it."))
    add("unpublish", cmd_unpublish, "Remove a package, or one version, from the hub (package managers only).",
        "Delete the package (or NAME@VERSION) from the hub index and database. Only an admin, owner or maintainer of the package may do it; others get 403. Git repositories and already installed copies are not touched. Needs `aihub login`.",
        "Examples:\n  aihub unpublish my-skill\n  aihub unpublish my-skill@1.0.0 --yes",
        arg("name", metavar="PACKAGE[@VERSION]", help="Package, or name@version to remove one version."),
        arg("-y", "--yes", action="store_true", help="Do not ask for confirmation."))
    add("list", cmd_list, "List packages installed by this CLI.",
        "Show each locally installed package, its installed version, and the components it adds to supported tools.",
        "Examples:\n  aihub list\n  aihub list --plain\n  aihub list --name-only",
        *PLAIN)
    add("update", cmd_update, "Update installed packages to newer versions.",
        "Check for newer releases and install them. With no name, checks every installed package; with a name, checks only that installed package.",
        "Examples:\n  aihub update\n  aihub update my-skill",
        arg("name", nargs="?", metavar="PACKAGE", help="Installed package to update; omit to check all installed packages."))
    add("doctor", cmd_doctor, "Check hub connectivity and local CLI setup.",
        "Check whether the configured hub is reachable, whether you are signed in, which supported tools are detected and have usage hooks, whether Python is available, and how many packages are installed.",
        "Example:\n  aihub doctor")
    add("version", updater.cmd_version, "Show the CLI version and whether the hub has a newer one.",
        "Print the installed CLI version and compare it with the version the hub distributes.",
        "Example:\n  aihub version")
    add("upgrade", updater.cmd_upgrade, "Upgrade the aihub CLI itself to the hub's version.",
        "Download the CLI from the configured hub, verify its SHA-256, and replace the running copy. The previous copy is kept so --rollback can restore it. The CLI also updates itself: after every command a detached background check (at most once a minute; the command never waits) installs a newer hub version, and the next run says so. 'aihub config set auto_update on|notify|off' (notify = only print that a version is available), 'aihub config set auto_update_minutes N' (gap between checks, 1..43200); AIHUB_NO_UPDATE=1 turns it off. Rolling back makes auto-update skip that version.",
        "Examples:\n  aihub upgrade\n  aihub upgrade --check\n  aihub upgrade --rollback",
        arg("--check", action="store_true", help="Only report whether a newer version exists."),
        arg("--force", action="store_true", help="Reinstall even when already current."),
        arg("--rollback", action="store_true", help="Restore the version that was installed before the last upgrade."))
    add("auto-update", updater.cmd_auto_update, "Check the hub for a newer CLI now and install it.",
        "Run the automatic update check in the foreground: ask the hub, install a newer version (respects auto_update notify/off and a rolled-back version). Normally this runs by itself in the background after every command.",
        "Example:\n  aihub auto-update",
        arg("--background", action="store_true", help=argparse.SUPPRESS))
    add("setup", remote_config.cmd_setup, "Pull the package index and telemetry settings from the hub.",
        "Ask the hub for the package index location and where usage events are sent. Values come over HTTPS, are stored in your aihub config (git tokens go to your git credential helper), and are refreshed automatically on the interval the admin sets. If the hub cannot be reached, you are asked for the values instead; environment variables AIHUB_INDEX_URL and AIHUB_TELEMETRY_URL are picked up automatically.",
        "Examples:\n  aihub setup\n  aihub setup --hub https://hub.example.com",
        arg("--hub", metavar="URL", help="Hub to ask (default: the configured hub)."),
        arg("--manual", action="store_true", help="Skip the hub and enter the values by hand."),
        arg("-y", "--yes", action="store_true", help="Accept defaults.json values without asking."))
    add("welcome", cmd_welcome, "Run first-time setup for detected AI tools.",
        "Check the hub and detected tools, offer or install usage hooks and the built-in packaging skill, and hint at the optional account. Interactive setup lets you choose; --yes accepts defaults without prompting.",
        "Examples:\n  aihub welcome\n  aihub welcome --yes",
        arg("-y", "--yes", action="store_true", help="Accept setup defaults and do not prompt."))
    add("hook", cmd_hook, "Record one AI-tool usage event from standard input.",
        "Read a tool-call event as JSON from standard input, match it to an installed package component, and queue usage telemetry. Intended for integration hooks: it spools one event and exits without blocking the AI tool.",
        "Example:\n  printf '{}' | aihub hook --source claude",
        arg("--source", default="claude", metavar="TOOL", help="Source label recorded with the event (default: claude)."),
        arg("--use", action="store_true", help="Standard input is one explicit use event {package, component, session, detail} (used by aihub-ui)."))
    add("vault", cmd_vault, "Add, edit or remove git remotes and their logins.",
        "Manage the git hosts the CLI fetches the index and packages from. Logins go to the git credential helper (Keychain, DPAPI, Secret Service or a 0600 file); vault.json keeps only host and user name. On a terminal, bare 'aihub vault' opens a menu and missing values are prompted for (token hidden). In scripts give the token on standard input.",
        "Examples:\n  aihub vault\n  aihub vault add https://git.example.com --user sam\n  aihub vault test https://git.example.com\n  printf %s \"$TOKEN\" | aihub vault edit git.example.com --token-stdin\n  aihub vault remove git.example.com --yes",
        arg("action", nargs="?", choices=["list", "add", "edit", "remove", "test"], help="Default: menu on a terminal, else list. test: check that a stored login reaches its host."),
        arg("remote", nargs="?", metavar="REMOTE", help="Git URL or host (add: URL; edit/remove: host)."),
        arg("--user", metavar="NAME", help="Git user name."),
        arg("--token-stdin", action="store_true", help="Read the token/password from standard input."),
        arg("-y", "--yes", action="store_true", help="Do not ask before removing."))
    add("run", cmd_run, "Run bots and services in the background: start, stop, list, logs, schedule.",
        "Start a bot or service package (type bot or service; a service restarts always by default, or as its [service] restart / schedule says) under a small supervisor, in the background by default: its output goes to ~/.aihub/runs/NAME.log, it restarts on failure (--restart) or runs on a schedule (--schedule: seconds, 30s/5m/2h/1d or a cron expression). --fg runs it in this terminal instead (needed for an interactive chat). The bot gets its settings from ~/.aihub/bots/BOT/.env (a service: ~/.aihub/services/NAME/.env) and sees your knowledge notes, the skills of installed packages and its own MCP servers. Signed in to a hub, every run also shows on the hub's Running page, where it can be stopped, started, restarted and changed (aihub config set run_report off to keep runs local).\n\nActions: aihub run BOT | start NAME | stop NAME|--all | pause NAME | restart NAME | list | logs NAME [-f] | listen NAME | set NAME ... | status NAME | rm NAME",
        "Examples:\n  aihub run aihub-bot -- --cron\n  aihub run aihub-bot --fg\n  aihub run aihub-bot --name digest --schedule '0 8 * * 1-5' -- --once 'summarize yesterday'\n  aihub run list\n  aihub run logs digest -f\n  aihub run set digest --schedule 2h --restart on-failure\n  aihub run stop --all",
        arg("target", nargs="?", metavar="BOT|ACTION", help="Installed bot or service to start, or an action: start, stop, pause, restart, list, logs, listen, set, status, rm."),
        arg("args", nargs="*", metavar="ARG", help="Run name for an action; for a start, passed to the bot (put -- before options)."),
        arg("--name", metavar="NAME", help="Name of the run (default: the bot name); start the same bot several times under different names."),
        arg("--fg", "--foreground", dest="fg", action="store_true", help="Run in this terminal (stdin attached, output shown and logged); Ctrl-C stops it."),
        arg("--schedule", metavar="WHEN", help="Run on a schedule: seconds, 30s/5m/2h/1d, or a cron expression ('0 8 * * 1-5'). With set: '' removes it."),
        arg("--restart", choices=["no", "on-failure", "always"], help="Restart policy when the bot ends (default: the package's [bot]/[service] restart, else on-failure (service: always); no with --schedule or --fg)."),
        arg("--no-hub", action="store_true", help="Do not report this run to the hub."),
        arg("-f", "--follow", action="store_true", help="logs: keep printing new output (Ctrl-C stops)."),
        arg("-n", "--lines", type=int, default=40, metavar="N", help="logs: lines to show (default 40)."),
        arg("--all", action="store_true", help="stop: every running run."),
        arg("--clear-args", action="store_true", help="set: remove the saved bot arguments."),
        arg("--force", action="store_true", help="rm: stop it first if it runs."),
        arg("--json", action="store_true", help="list/status: JSON output."))
    add("onboard", cmd_onboard, "Optional guided credential setup for an installed bot.",
        "Runs the bot's own onboarding script. The Teams bot signs you in with the Teams Developer CLI (`teams login`, a browser popup), creates its app registration (`teams app create`) and writes CLIENT_ID, CLIENT_SECRET and TENANT_ID into the bot's .env; after that the bot runs without you signed in. Install offers it in a terminal; you can always edit .env by hand instead.",
        "Examples:\n  aihub onboard\n  aihub onboard aihub-teams-bot",
        arg("name", nargs="?", metavar="BOT", help="Installed bot; optional when only one has guided setup."))
    add("knowledge", cmd_knowledge, "Your personal notes and bot persona (soul), synced with the hub.",
        "Notes are markdown files in ~/.aihub/knowledge, also editable on the hub web page (Knowledge). 'soul' is the persona bots load first. Share a note with a user or group: it appears for them under ~/.aihub/knowledge/shared/<owner>/ after `pull`; 'soul' is never shared.",
        "Examples:\n  aihub knowledge get soul\n  aihub knowledge get alice/handbook\n  aihub knowledge edit soul\n  aihub knowledge pull\n  aihub knowledge push\n  aihub knowledge list\n  aihub knowledge share handbook --group backend\n  aihub knowledge share runbook --user alice --write\n  aihub knowledge shared",
        arg("action", nargs="?", default="list", choices=["list", "get", "pull", "push", "edit", "rm", "path", "share", "unshare", "shares", "shared", "push-shared"], help="Default: list."),
        arg("name", nargs="?", metavar="NOTE", help="Note name (get/edit/push/rm/share/unshare/shares); owner/note for get (shared with you) and push-shared."),
        arg("--user", help="share/unshare: hub user name."),
        arg("--group", help="share/unshare: hub group name (everyone in it)."),
        arg("--write", action="store_true", help="share: let them edit the note too (default read-only)."))
    add("space", cmd_space, "Publish documents, slides, reports, PDFs, sheets and HTML pages as a Space on the hub.",
        "A Space is addressed author@space-name and opens at <hub>/#/s/author@space-name. Send a .pptx, .docx, .md, .xlsx/.csv, .pdf, an .html file (a report, dashboard, or a slide deck made of <section class=\"slide\"> parts) or a folder with index.html. Slides play directly in the browser with transitions; the viewer size (canvas | fit | full screen) defaults to --view and the reader can switch. Spaces are public or private; share a private one with a user or group as view or edit, like a package. Publishing the same name again replaces the content (needs --private/--public only to change who sees it).",
        "Examples:\n  aihub space publish deck.pptx q3-review --view fit\n  aihub space publish report.html --kind dashboard --private\n  aihub space publish ./site team-site\n  aihub space publish new.pptx alice@q3-review      # new version of a space you may edit\n  aihub space share me@q3-review --group data-team --edit\n  aihub space set me@q3-review --transition fade --autoplay 8\n  aihub space top\n  aihub space search quarterly\n  aihub space url me@q3-review --view full",
        arg("action", choices=["publish", "list", "top", "search", "info", "url", "open", "get", "rm", "set", "share", "unshare", "shares"], help="What to do."),
        arg("target", nargs="?", metavar="PATH|SPACE", help="publish: file or folder; others: author@space-name (a pasted hub link works too)."),
        arg("extra", nargs="?", metavar="NAME|WORDS", help="publish: space name (slug), or author@name to add a version to somebody else's space; search: the words."),
        arg("--kind", choices=list(("auto", "doc", "slides", "report", "pdf", "sheet", "dashboard", "html")), help="publish/list/set: kind (default: from the file)."),
        arg("--title", help="Title shown on the hub."),
        arg("--description", help="One line shown on cards."),
        arg("--tags", help="Comma list."),
        arg("--public", action="store_true", help="Anyone, even signed out, can open it."),
        arg("--private", action="store_true", help="Only you and the people/groups you share it with."),
        arg("--view", choices=["canvas", "fit", "full"], help="Default viewer size: canvas (in the page), fit (fills the screen area), full (full window). url/open: the mode in the link."),
        arg("--transition", choices=["auto", "none", "fade", "slide", "zoom", "flip"], help="Slides: transition between slides (auto = the one in the file)."),
        arg("--autoplay", type=int, metavar="SECONDS", help="Slides: advance by itself every N seconds (0 = off)."),
        arg("--loop", dest="loop", action="store_true", default=None, help="Slides: start over after the last slide."),
        arg("--no-loop", dest="loop", action="store_false", help="Slides: stop at the last slide."),
        arg("--user", help="share/unshare: hub user."),
        arg("--group", help="share/unshare: hub group."),
        arg("--edit", action="store_true", help="share: let them publish new versions and change settings (default view only)."),
        arg("--bare", action="store_true", help="url: a link without the hub header and footer (for embedding)."),
        arg("--mine", action="store_true", help="list: only my spaces."),
        arg("--limit", type=int, default=30, help="list/top/search: how many (default 30)."),
        arg("-o", "--output", help="get: file to save."),
        arg("--open", action="store_true", help="publish: open the page in the browser."),
        arg("-y", "--yes", action="store_true", help="rm: do not ask."))
    add("soul", cmd_soul, "Print your soul (persona) note.",
        "Shortcut for 'aihub knowledge get soul': the persona note from the hub (local copy if the hub is unreachable), written to stdout so an assistant or script can read it.",
        "Examples:\n  aihub soul\n  aihub soul > soul.md")
    add("policy", cmd_policy, "Built-in guard that blocks agents from destructive tool calls.",
        "aihub-policy checks every tool call of Claude Code before it runs and denies (or asks about) deleting databases or buckets, overwriting tables or protected paths. Rules are bundled, refreshed automatically from the hub package aihub-policy, and can be switched on or off, added or removed here.",
        "Examples:\n  aihub policy\n  aihub policy off\n  aihub policy disable sql-update-all\n  aihub policy add no-prod --pattern 'kubectl .* prod' --reason 'prod cluster'\n  aihub policy test 'aws s3 rm s3://b --recursive'\n  aihub policy update",
        arg("action", nargs="?", default="list", choices=["list", "on", "off", "enable", "disable", "add", "remove", "test", "judge", "log", "update"], help="Default: list."),
        arg("target", nargs="?", metavar="ID|COMMAND", help="Rule id (enable/disable/add/remove) a command line (test), or on|off|status (judge)."),
        arg("--pattern", help="add: regular expression to block."),
        arg("--reason", help="add: why it is blocked."),
        arg("--mode", choices=["deny", "ask"], default="deny", help="add: deny (default) or ask."),
        arg("--tool", default="Bash", help="add: tool name regex (default Bash)."),
        arg("--field", default="command", help="add: input field to match (command, file_path, name, *)."),
        arg("--force", action="store_true", help="update: reinstall even if current."))
    add("credential", cmd_credential, "Git credential helper used for the hub and index hosts.",
        "Speaks git's credential protocol so a login you type once is remembered: macOS Keychain, Windows DPAPI, Linux Secret Service, else a private 0600 file. 'aihub setup' registers it for your hub and index hosts only; you never call it yourself.",
        "Example:\n  git config --global --get-all credential.https://git.example.com.helper",
        arg("action", choices=["get", "store", "erase"], help="Credential protocol action."))
    add("usage", cmd_usage, "Local usage report per AI tool: sessions, models, tokens, cache and estimated cost.",
        "Read the session logs of the AI tools on this machine (Claude Code ~/.claude/projects, Codex ~/.codex/sessions) and show sessions, requests, tokens, cache use and an ESTIMATED price for today, the last N days and all time, by tool and by model. Purely local: nothing is sent to the hub. Prices are list prices per 1M tokens; add or override models in ~/.aihub/prices.json as {\"model-substring\": {\"input\": 3, \"output\": 15, \"cache_write\": 3.75, \"cache_read\": 0.3}}.",
        "Examples:\n  aihub usage\n  aihub usage --days 7 --sessions\n  aihub usage --session 3f9a1c2b\n  aihub usage --tips --days 14\n  aihub usage --by-day --days 14\n  aihub usage --tool claude --days 7 --plain\n  aihub usage --json",
        arg("--tool", action="append", choices=["claude", "codex", "aihub-ui"], help="Only this tool (repeatable)."),
        arg("--days", type=int, default=30, help="Last N days: one of the windows (with today, this month and all time), and the window of --sessions/--session/--tips (default 30, 0 = all time). In the full screen view press n to type another number."),
        arg("--sessions", action="store_true", help="List sessions (last used, tool, project, requests, peak context, cache hit, cost, title) and exit."),
        arg("--session", metavar="ID_OR_TEXT", help="Show one session: totals, context per request and cache rebuild flags. Matches an id (the last 8 characters are enough), title or folder."),
        arg("--tips", action="store_true", help="Efficiency suggestions from your own usage (cold cache rebuilds, huge context, low cache hit, model choice)."),
        arg("--find", metavar="TEXT", help="With --sessions/--tips: only sessions whose id, title, folder, tool or model contains TEXT."),
        arg("--sort", choices=["recent", "cost", "ctx", "requests"], default="recent", help="Order of --sessions (default recent)."),
        arg("--top", type=int, default=30, help="Rows shown by --sessions (default 30, 0 = all)."),
        arg("--model", help="Case-insensitive regex on 'tool model', e.g. 'opus|gpt' (in the interactive view type /regex)."),
        arg("--by-day", action="store_true", help="One row per day instead of by tool and model."),
        arg("--plain", action="store_true", help="Print every period and exit (default in a terminal is a full screen view: arrows and mouse, 1-4 period, n custom days, m summary, s sessions, d days, t tips, / model filter, f find, q quit)."),
        arg("--json", action="store_true", help="Machine readable output (includes by_day)."))
    add("flush", cmd_flush, "Send queued usage events to the hub.",
        "Push queued usage events to the telemetry inbox (a git repo) now. Events are normally pushed in the background as one batch, a few seconds after the last event (aihub config set telemetry_interval 15).",
        "Examples:\n  aihub flush\n  aihub flush --verbose",
        arg("-v", "--verbose", action="store_true", help="Print the number of events sent."),
        arg("--background", action="store_true", help=argparse.SUPPRESS))
    add("usage-sync", cmd_usage_sync, "Send your daily token totals to the hub now.",
        "Upload your usage (tokens, cache, estimated cost per day, tool and model; no prompts, titles or folders) so the hub's Usage page can show you and your groups. It normally runs by itself in the background after every answer turn of Claude Code (Stop hook), batched and only for what changed. Needs a hub and `aihub login`. Turn off with `aihub config set usage_sync off`.",
        "Examples:\n  aihub usage-sync\n  aihub config set usage_sync off",
        arg("--background", action="store_true", help=argparse.SUPPRESS))
    add("hooks", cmd_hooks, "Install or remove usage hooks for detected tools.",
        "Install or remove AI-tool hooks that report usage of components from installed packages. The optional tool filter is repeatable; only detected tools are processed, and Codex has no tool-call hook.",
        "Examples:\n  aihub hooks install\n  aihub hooks install --tool claude\n  aihub hooks remove",
        arg("action", nargs="?", choices=["install", "remove"], default="install", metavar="ACTION", help="Install hooks (default) or remove them."),
        arg("--tool", action="append", metavar="TOOL", help="Only process this detected tool; repeat to filter multiple tools."))
    add("self-uninstall", cmd_self_uninstall, "Remove aihub itself from this machine.",
        "Uninstall every installed package, remove the usage hooks from your AI tools, delete the PATH line that install.sh added to your shell config, and delete the aihub folder (~/.aihub, or $AIHUB_HOME). Asks first unless --yes. Your published packages and git repositories are not touched. To remove one package, use `aihub uninstall NAME`.",
        "Examples:\n  aihub self-uninstall\n  aihub self-uninstall --yes",
        arg("-y", "--yes", action="store_true", help="Do not ask for confirmation."))
    add("skill", cmd_skill, "Install or remove the built-in skill packages.",
        "Install, repair or remove the built-in packages (aihub-guide and aihub-package). They are ordinary hub packages, published by the server on startup and installed automatically by the hub installer; this command re-runs that step. Use `aihub update` to upgrade them and `aihub uninstall <name>` to remove one.",
        "Examples:\n  aihub skill install\n  aihub skill install --tool claude\n  aihub skill remove",
        arg("action", nargs="?", choices=["install", "remove"], default="install", metavar="ACTION", help="Install the packages (default) or remove them."),
        arg("--tool", action="append", choices=["claude", "codex", "opencode"], metavar="TOOL", help="Register only with this tool; repeat for several (default: detected tools)."))

    dev = sp.add_parser("dev", help="Create, validate, build, publish, and inspect packages.",
                        description="Develop an AI Hub package from an aihub.toml project: generate starter files, lint the manifest, build an archive, publish releases, or inspect usage statistics.",
                        epilog="Examples:\n  aihub dev init . --type skill --name my-skill\n  aihub dev validate\n  aihub dev publish --bump patch\n  aihub dev publish ./builtin --only aihub-guide   (one package of a workspace)",
                        formatter_class=HelpFormatter)
    ds = dev.add_subparsers(dest="dcmd", required=True, metavar="command")
    dev_commands = [
        ("init", dev_init, "Create starter files for a package project.",
         "Create an aihub.toml manifest and starter files for the selected package type in PATH. Existing generated files are left alone unless --force is used; --force overwrites generated starter files.",
         "Examples:\n  aihub dev init . --type skill --name my-skill\n  aihub dev init ./server --type mcp --description \"Example MCP server\"\n  aihub dev init builtin --sub --name my-skill   (-> builtin/my-skill, next to the other packages)",
         [arg("--name", metavar="NAME", help="Package name (default: derived from the project directory)."),
          arg("--type", default="skill", choices=list(M.TYPES), metavar="TYPE", help="Starter package type (default: skill)."),
          arg("--description", metavar="TEXT", help="Starter package description."),
          arg("--sub", action="store_true", help="Create the package in its own folder PATH/NAME, for a folder that holds several packages (needs --name)."),
          arg("--mono", action="store_true", help="PATH becomes one git repository holding several packages (see --packages). Packages already below PATH are kept; only their [git] is refreshed."),
          arg("--packages", action="append", metavar="LIST", help="With --mono: packages to create, comma separated, each NAME or NAME:TYPE (e.g. --packages guide,tools:mcp)."),
          arg("--remote", metavar="URL", help="With --mono: git remote for the whole repository (asked once when omitted)."),
          arg("--branch", metavar="BRANCH", help="With --mono: branch to publish (default: main)."),
          arg("-w", "--wizard", action="store_true", help="Interactive setup wizard: choose one package, a monorepo (and which sub folders become packages), or a new package in this workspace."),
          arg("--force", action="store_true", help="Overwrite generated starter files that already exist.")]),
        ("validate", dev_validate, "Validate and lint the package project.",
         "Parse and normalize PATH/aihub.toml, then lint the manifest and referenced package files. Report warnings and fail if validation errors are found.",
         "Example:\n  aihub dev validate ./my-skill", []),
        ("build", dev_build, "Validate and build a package archive.",
         "Validate the project, collect files selected by the project's ignore rules, and write a versioned .tar.gz archive under PATH/dist/.",
         "Example:\n  aihub dev build ./my-skill", []),
        ("publish", dev_publish, "Publish a package release (git push, or straight to the hub with -i).",
         "Optionally bump the version in aihub.toml, validate the package and publish it. By default the project is committed and pushed to its git remote and the hub is told. With -i the package is packed and uploaded to the hub, which stores it in its own data folder and lists it: no git repository, remote or git account is needed, and installs download it from the hub. -i needs `aihub login` with an account that may publish.",
         "Examples:\n  aihub dev publish\n  aihub dev publish --bump patch\n  aihub dev publish -i\n  aihub dev publish -i --bump patch",
         [arg("--bump", choices=["major", "minor", "patch"], metavar="PART", help="Increment this version part before building (major/minor/patch; lower parts reset)."),
          arg("-i", "--indirect", action="store_true", help="Upload the package to the hub instead of pushing to git (no git needed; installs get it from the hub).")]),
        ("index", dev_index, "Force the hub to index this project (maintainers).",
         "Ask the hub to read this project's pushed commit and add the version to the package index. Use it when the automatic step after `aihub dev publish` did not happen (lost event, telemetry inbox down). Needs `aihub login` with an account whose role has the 'index' permission. The hub checks aihub.toml at that commit; nothing from your machine is trusted but the repo location.",
         "Examples:\n  aihub dev index\n  aihub dev index --commit 1a2b3c4",
         [arg("--commit", metavar="SHA", default="", help="Index this commit instead of HEAD (must be pushed).")]),
        ("tree", dev_tree, "Show the packages here as a tree, with repository status.",
         "Show every package below PATH (or the one in PATH) with its version and type, the git repository it belongs to, and whether its [git] table matches that repository. Lists what to fix and the command that fixes it.",
         "Example:\n  aihub dev tree ./builtin", []),
        ("doctor", dev_doctor, "Fix [git] url, branch and subdir to match the real repository.",
         "For the package in PATH, or every package below it, set [git] in aihub.toml from the working repository: its origin URL, the branch it is on, and the package's folder inside it. Nothing else in the manifest changes. Problems it cannot decide for you (no repository, nested .git, duplicate names) are listed. `aihub dev publish` does this fix automatically.",
         "Examples:\n  aihub dev doctor\n  aihub dev doctor ./builtin\n  aihub dev doctor --check\n  aihub dev doctor --remote https://git.example/team/builtin.git",
         [arg("--check", action="store_true", help="Only report what would change; exit with an error if anything is out of date."),
          arg("--remote", metavar="URL", help="Point the repository's origin at this URL first, then update [git] everywhere.")]),
        ("tree-solve", dev_tree_solve, "Resolve nested git repositories in the workspace.",
         "Find package folders that hold their own .git inside another repository (the outer repository cannot publish those files) and resolve them. `merge` folds the folder into the outer repository and moves its old .git to ~/.aihub/backups; `keep` leaves it a separate repository and makes the outer one ignore it, so you manage it directly. Asks per folder on a terminal. Afterwards every [git] is refreshed.",
         "Examples:\n  aihub dev tree-solve\n  aihub dev tree-solve --nested merge",
         [arg("--nested", choices=["merge", "keep"], metavar="MODE", help="Answer for every nested repository: merge or keep."),
          arg("--remote", metavar="URL", help="Also set the outer repository's origin to this URL.")]),
        ("list", dev_list, "List the packages in this folder.",
         "Show every package (folder with an aihub.toml) in PATH or below it, with its version and whether it has uncommitted changes. Use the names with --only.",
         "Example:\n  aihub dev list ./builtin", []),
        ("stats", dev_stats, "Show usage statistics for this package.",
         "Read usage statistics for the package named in PATH/aihub.toml. Non-interactive output is JSON.",
         "Example:\n  aihub dev stats ./my-skill", []),
    ]
    for n, fn, help, description, epilog, extra in dev_commands:
        p = ds.add_parser(n, help=help, description=description, epilog=epilog,
                          formatter_class=HelpFormatter)
        p.add_argument("path", nargs="?", default=".", metavar="PATH",
                       help="Package directory containing aihub.toml, or a folder holding several packages (default: current directory).")
        for e in extra:
            p.add_argument(*e[0], **e[1])
        if not any("--json" in e[0] for e in extra):
            p.add_argument("--json", action="store_true", help=JSON_HELP)
        if n not in ("init", "list"):
            p.add_argument("--only", action="append", metavar="NAME", help="In a folder of several packages, work on just these (folder or package name; repeat or comma-separate).")
        p.set_defaults(fn=fn if n in ("init", "list", "publish", "tree", "doctor", "tree-solve") else _each(fn))
    return ap


JSON_HELP = "Print one JSON document on stdout: {ok, command, data, messages, changes, context, error, hint} (docs/cli.md, JSON output)."
NO_ENVELOPE = ("hook", "flush", "usage-sync", "credential")        # git / hooks / workers read these streams themselves
NATIVE_JSON = ("usage",)                                          # its own --json keeps its own shape; `aihub --json usage` wraps it
JSON_SCHEMA = 1


def json_wanted(a):
    if not getattr(a, "cmd", None) or a.cmd in NO_ENVELOPE or a.cmd == "run" and getattr(a, "target", None) == "_supervise":
        return False
    if a.cmd == "auto-update" and getattr(a, "background", False):
        return False
    if getattr(a, "json_out", False) or (os.environ.get("AIHUB_JSON") or "").lower() in ("1", "true", "yes", "on"):
        return True
    return bool(getattr(a, "json", False)) and a.cmd not in NATIVE_JSON


def _context():
    """Who and where: shared with any UI that drives the CLI (same credentials.json, same vault). Never a token or password."""
    from ..core.release import VERSION
    cred, cfg = paths.load("credentials.json", {}), paths.config()
    return {"hub": cfg.get("hub"), "hub_configured": paths.hub_configured(), "signed_in": bool(cred.get("token")),
            "username": cred.get("username") or None, "cli_version": VERSION, "home": paths.HOME(), "client_id": cfg.get("client_id")}


def _pkg_snapshot():
    try:
        return {n: (r.get("version"), installer.enabled(r)) for n, r in installer.state()["packages"].items()}
    except Exception:
        return {}


def _changes(before, after):
    out = {"installed": [], "removed": [], "updated": [], "enabled": [], "disabled": []}
    for n, (v, on) in sorted(after.items()):
        if n not in before:
            out["installed"].append({"name": n, "version": v})
        else:
            v0, on0 = before[n]
            if v != v0:
                out["updated"].append({"name": n, "from": v0, "to": v})
            if on != on0:
                out["enabled" if on else "disabled"].append(n)
    out["removed"] = [{"name": n, "version": before[n][0]} for n in sorted(before) if n not in after]
    return {k: v for k, v in out.items() if v}


def _json_envelope(a, body):
    """Run body() with nothing reaching stdout but ONE JSON document. ui lines are recorded; stray prints are captured (and parsed as the
    data when they are JSON, e.g. `info`, `run list`); child processes (install scripts, git, pip) write to stderr instead of stdout."""
    import io
    ui.json_mode()
    if hasattr(a, "json"):
        a.json = True                       # commands with their own JSON output use it; updater/welcome stay quiet
    # never forced to --yes: without a terminal each command takes its safe non-interactive default (self-uninstall refuses, ...)
    sys.stdout.flush()
    real = os.dup(1)
    os.dup2(2, 1)
    cap, keep = io.StringIO(), sys.stdout
    sys.stdout = cap
    before = _pkg_snapshot()
    sub = next((getattr(a, k) for k in ("dev_cmd", "action", "target", "area") if isinstance(getattr(a, k, None), str)), None)
    env = {"schema": JSON_SCHEMA, "ok": True, "command": a.cmd + (" " + sub if sub else ""), "exit": 0, "error": None, "hint": None}
    try:
        body()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        if not isinstance(e.code, (int, type(None))):
            env["error"] = str(e.code)
        env.update(exit=code, ok=code == 0)
    except (api.ApiError, ValueError, KeyError, OSError) as e:
        env.update(ok=False, exit=1, error=str(e), hint=ui.hint_for(e))
    except KeyboardInterrupt:
        env.update(ok=False, exit=130, error="interrupted")
    except Exception as e:                  # a bug must still produce a document the UI can read
        env.update(ok=False, exit=1, error="%s: %s" % (type(e).__name__, e))
    finally:
        sys.stdout = keep
        os.dup2(real, 1)
        os.close(real)
    if ui.DATA.get("error") and env["ok"]:
        env.update(ok=False, exit=env["exit"] or 1)
    env["error"] = env["error"] or ui.DATA.get("error")
    env["hint"] = env["hint"] or ui.DATA.get("hint")
    text = cap.getvalue()
    data = ui.DATA.get("data")
    if "data" not in ui.DATA and text.strip():
        try:
            data = json.loads(text)
            text = ""
        except ValueError:
            pass
    env.update(data=masked(data), output=text or None, messages=list(ui.LOG), changes=_changes(before, _pkg_snapshot()), context=_context())
    _emit_json(env)
    return env["exit"]


def _emit_json(env):
    doc = json.dumps(env, indent=2, default=str)
    tok = paths.load("credentials.json", {}).get("token")
    if tok and len(tok) >= 8:
        doc = doc.replace(tok, "***")         # safety net: the hub token never leaves in a JSON answer, whatever printed it
    sys.stdout.write(doc + "\n")
    sys.stdout.flush()
    return env["exit"]


def _json_argv(argv):
    args = sys.argv[1:] if argv is None else list(argv)
    return "--json" in args[:args.index("--")] if "--" in args else "--json" in args or \
        (os.environ.get("AIHUB_JSON") or "").lower() in ("1", "true", "yes", "on")


def main(argv=None):
    ap = build_parser()
    if _json_argv(argv):
        import io
        err, keep = io.StringIO(), sys.stderr
        sys.stderr = err
        try:
            a = ap.parse_args(argv)
        except SystemExit as e:           # bad command line: still one JSON document (help text with --help)
            sys.stderr = keep
            if not e.code:
                return 0
            msg = [x for x in err.getvalue().splitlines() if x.strip()]
            _emit_json({"schema": JSON_SCHEMA, "ok": False, "command": None, "exit": 2, "error": (msg[-1] if msg else "bad arguments").split("error: ", 1)[-1],
                        "hint": "aihub CMD --help", "data": None, "output": err.getvalue() or None, "messages": [], "changes": {}, "context": _context()})
            return 2
        finally:
            sys.stderr = keep
    else:
        a = ap.parse_args(argv)
    from . import hooks, integrations, policy, telemetry, tls, usage, usagesync
    from ..core import release                  # `aihub upgrade` swaps aihub.pyz under this process: load every module the tail of main needs BEFORE it
    if json_wanted(a):
        def body():
            code = _main(a, ap, hooks, policy, tls)
            if code:
                raise SystemExit(code)
        return _json_envelope(a, body)
    return _main(a, ap, hooks, policy, tls)


def _main(a, ap, hooks, policy, tls):
    try:
        if not getattr(a, "cmd", None):
            if not ui.INTERACTIVE:
                ap.print_help()
                return 0
            if not os.path.exists(paths.p(".welcomed")):
                cmd_welcome(argparse.Namespace(yes=False))
                return 0
            cmd_menu()
            return 0
        if ui.INTERACTIVE and a.cmd not in ("hook", "flush", "usage-sync", "auto-update", "welcome", "config", "credential", "vault", "policy", "run") and not os.path.exists(paths.p(".welcomed")):
            cmd_welcome(argparse.Namespace(yes=False, offer_ui=a.cmd not in ("install", "download", "sync")))   # the user is already installing
        if a.cmd == "credential" or a.cmd == "auto-update" and a.background or a.cmd == "run" and a.target == "_supervise":
            return a.fn(a)              # git calls this on every fetch / the detached update worker: no welcome, updater or network
        if a.cmd not in ("hook", "flush", "usage-sync", "credential"):
            tls.warn_if_insecure()
        a.fn(a)
        if a.cmd not in ("hook", "flush", "usage-sync", "credential", "self-uninstall"):
            record_cli_install()
        if a.cmd not in ("hook", "flush", "usage-sync", "credential", "hooks", "self-uninstall", "uninstall"):
            hooks.verify()      # install/use/update/...: make sure our usage hooks are still in place
        policy.auto(a.cmd)
        remote_config.auto(a.cmd)
        updater.auto(a.cmd, a)          # last: it may swap aihub.pyz under this process
    except (api.ApiError, ValueError, KeyError, OSError) as e:
        ui.error(e, ui.hint_for(e))
        return 1
    except KeyboardInterrupt:
        ui._cursor(True)
        sys.stdout.write("\n")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
