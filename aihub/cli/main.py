import argparse
import getpass
import json
import os
import sys
import tempfile
import urllib.parse

from ..core import archive, ignore, manifest as M, naming, version as V
from . import api, installer, paths, ui, updater
from .installer import _spec

def cmd_config(a):
    c = paths.load("config.json", {})
    if a.action == "set":
        if not (a.key and a.value is not None):
            raise ValueError("usage: aihub config set <key> <value>")
        c[a.key] = a.value
        paths.save("config.json", c)
        ui.good("%s = %s" % (a.key, a.value))
    cfg = paths.config()
    if a.action != "set" or not ui.TTY:
        print(json.dumps(cfg, indent=2))


def cmd_login(a):
    u = a.username or input("username: ")
    r = api.call("POST", "/auth/login", {"username": u, "password": getpass.getpass("password: ")})
    paths.save("credentials.json", {"token": r["token"], "username": u}, private=True)
    ui.celebrate("Welcome back, %s" % u)


def cmd_register(a):
    u = input("username: ")
    api.call("POST", "/auth/register", {"username": u, "password": getpass.getpass("password: ")})
    ui.good("Account created for %s" % u)
    ui.info("next: aihub login   (new accounts may need admin approval first)")


def cmd_search(a):
    with ui.Spinner("Searching for '%s'" % a.term if a.term else "Loading packages") as sp:
        r = api.call("GET", "/packages?q=%s&per_page=30" % urllib.parse.quote(a.term))
        sp.text("%d result%s" % (len(r["items"]), "" if len(r["items"]) == 1 else "s"))
    if not r["items"]:
        ui.note("Nothing matches '%s'." % a.term)
        ui.info("try a shorter word, or publish your own: aihub dev init")
        return
    ui.out()
    ui.table(["NAME", "TYPE", "VERSION", "DESCRIPTION"],
             [[ui.bold(ui.highlight(p["name"], a.term)), ui.badge(p["type"]), p["latest_version"] or "-",
               ui.highlight((p["description"] or "").replace("\n", " "), a.term)] for p in r["items"]])
    ui.out()
    ui.info("install one with: aihub install <name>      details: aihub info <name>")


def cmd_info(a):
    with ui.Spinner("Fetching %s" % a.name):
        d = api.call("GET", "/packages/" + a.name)
    if not ui.TTY:
        print(json.dumps(d, indent=2))
        return
    meta = ["%s  v%s" % (ui.badge(d["type"]), d.get("latest_version") or "-"),
            "%s downloads" % d.get("downloads", 0)]
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
    req = d.get("requires") or {}
    if req.get("packages"):
        lines.append(ui.dim("needs: ") + ", ".join(req["packages"]))
    ui.out()
    ui.card(d["name"], lines, ui.dim("install  ") + ui.code("aihub install " + d["name"]))


def cmd_install(a):
    n, s = _spec(a.name)
    ui.out()
    installer.install(n, s, a.yes, a.tool)


def cmd_download(a):
    """Save a package archive without installing it. Resumable: re-run the same command after an interruption."""
    n, s = _spec(a.name.replace("@", "==", 1) if "@" in a.name else a.name)
    r = api.call("GET", "/resolve?name=%s&spec=%s" % (n.lower(), s.replace("=", "%3D").replace(">", "%3E").replace("<", "%3C")))
    d = os.path.abspath(a.output or ".")
    os.makedirs(d, exist_ok=True)
    dest = os.path.join(d, r["url"].rsplit("/", 1)[-1])
    ui.info("%s %s (%s)" % (r["name"], r["version"], ui.human(r.get("size", 0))))
    pg = ui.Progress("downloading")
    try:
        api.download(r["url"], dest, r["sha256"], progress=pg)
    finally:
        pg.finish()
    ui.good("Saved %s (sha256 verified)" % dest)


def cmd_uninstall(a):
    users = installer.dependents(installer.state(), naming.normalize(a.name), only_enabled=False)
    if users and not a.force and ui.INTERACTIVE and not ui.confirm("%s is needed by %s. Remove anyway?" % (a.name, ", ".join(users)), False):
        ui.info("nothing changed")
        return
    installer.uninstall(a.name)


def cmd_list(a):
    pk = installer.state()["packages"]
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
    pk = [(n, r) for n, r in installer.state()["packages"].items() if (not a.name or n == a.name) and installer.enabled(r)]
    if not pk:
        ui.note("No matching installed packages.")
        return
    updated = 0
    st = installer.state()
    for n, r in pk:
        spec = installer.constraint_on(st, n)        # never move a package outside what its dependents allow
        with ui.Spinner("Checking %s" % n) as sp:
            latest = api.call("GET", "/resolve?name=%s&spec=%s" % (n, urllib.parse.quote(spec)))["version"]
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
    hub = paths.config()["hub"]
    ui.good("hub: %s" % hub)
    reach = False
    with ui.Spinner("Reaching the hub") as sp:
        try:
            api.call("GET", "/healthz", timeout=5)
            reach = True
        except Exception as e:
            sp.problem("hub unreachable: %s" % e, "bad")
    if not reach:
        ui.info("fix: aihub config set hub https://your-hub.example.com")
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


def cmd_welcome(a):
    """First-run onboarding. Interactive on a tty, plain and safe otherwise (-y accepts the defaults)."""
    from . import hooks, integrations
    ask = ui.INTERACTIVE and not a.yes
    ui.banner()
    ui.heading("Let's get you set up")
    hub = paths.config()["hub"]
    with ui.Spinner("Checking the hub at %s" % hub) as sp:
        try:
            api.call("GET", "/healthz", timeout=5)
            sp.text("Hub is reachable (%s)" % hub)
        except Exception:
            sp.problem("Hub not reachable yet (%s). Change it with: aihub config set hub <url>" % hub)
    tools = integrations.detected()
    if tools:
        ui.good("Found: " + ", ".join(t.label for t in tools))
    else:
        ui.note("No AI tools detected yet. Install Claude Code, Codex or OpenCode and run: aihub welcome")
    do_hooks, do_skill = bool(tools), bool(tools)
    if tools and ask:
        ui.out()
        picks = ui.select("What should I connect?", [
            ("hooks", "Usage hooks - show what your team really uses"),
            ("skill", "Packaging skill - lets your AI tool help you publish")], multi=True, preset=("hooks", "skill"))
        do_hooks, do_skill = "hooks" in picks, "skill" in picks
    if do_hooks:
        for label, status in hooks.ensure():
            ui.good("%s hook: %s" % (label, status))
    if do_skill:
        for name, status in installer.install_builtin(tools=[t.name for t in tools]):
            (ui.good if status in ("installed", "already installed") else ui.note)("skill package %s: %s" % (name, status))
    if ask and not paths.load("credentials.json", {}).get("token"):
        ui.out()
        choice = ui.select("Account", [("login", "I have an account - sign in"), ("register", "Create an account"),
                                       ("skip", "Skip for now")])
        try:
            if choice == "login":
                cmd_login(argparse.Namespace(username=None))
            elif choice == "register":
                cmd_register(None)
        except (api.ApiError, ValueError, EOFError) as e:
            ui.note("%s (you can retry later with aihub login)" % e)
    _mark_welcomed()
    ui.celebrate("All set")
    ui.out()
    ui.out("  " + ui.dim("Try these next"))
    for cmd, why in (("aihub search", "browse what is available"), ("aihub install <name>", "add a skill, agent or MCP server"),
                     ("aihub dev init", "start publishing your own")):
        ui.out("    %s  %s" % (ui.accent("%-22s" % cmd), ui.dim(why)))
    ui.out()


def _mark_welcomed():
    try:
        paths.ensure()
        open(paths.p(".welcomed"), "w").close()
    except OSError:
        pass


def cmd_hook(a):
    """Agent hot path (PreToolUse). Spools one line and exits; never blocks, never fails the agent."""
    from . import hooks
    hooks.handle(sys.stdin.read(), a.source)


def cmd_flush(a):
    from . import telemetry
    n = telemetry.flush(linger=0 if a.verbose else 10)
    if a.verbose:
        print("sent %d events" % n)


def cmd_skill(a):
    """(Re)install or remove the built-in packages (aihub-guide, aihub-package) from the hub."""
    for name, status in installer.install_builtin(remove=a.action == "remove", tools=a.tool):
        (ui.bad if status.startswith("failed") else ui.good)("%-14s %s" % (name, status))


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
            elif choice == "doctor":
                cmd_doctor(None)
            elif choice == "init":
                dev_init(argparse.Namespace(path=".", name=None, type="skill", description=None, force=False))
        except (api.ApiError, ValueError, KeyError) as e:
            ui.error(e, ui.hint_for(e))
        ui.out()


# ---- dev
def _root(a): return os.path.abspath(a.path)


def dev_init(a):
    from ..core import templates
    d = _root(a)
    os.makedirs(d, exist_ok=True)
    name = a.name or os.path.basename(d).lower().replace("_", "-")
    from ..core import naming
    name = naming.validate(name)
    f = os.path.join(d, "aihub.toml")
    if os.path.exists(f) and not a.force:
        sys.exit("aihub.toml already exists here (use --force to overwrite the starter files)")
    manifest, files, execs = templates.render(a.type, name, a.description or "")
    with open(f, "w") as fh:
        fh.write(manifest)
    made = ["aihub.toml"]
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
    ui.celebrate("Created a %s project: %s" % (a.type, name))
    ui.tree(d, made)
    ui.out()
    ui.info("next: edit the files, then  aihub dev validate  &&  aihub dev publish")


def _load(a):
    with open(os.path.join(_root(a), "aihub.toml")) as f:
        return M.parse(f.read())


def dev_validate(a, quiet=False):
    m = _load(a)
    errs, warns = M.lint(m, _root(a))
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


def dev_publish(a):
    if a.bump:
        m = _load(a)
        p = os.path.join(_root(a), "aihub.toml")
        t = open(p).read()
        old = m["package"]["version"]
        open(p, "w").write(t.replace('version = "%s"' % old, 'version = "%s"' % V.bump(old, a.bump), 1))
    dest = dev_build(a)
    pg = ui.Progress("uploading")
    try:
        r = api.upload_package(dest, progress=pg)
    finally:
        pg.finish()
    ui.celebrate("Published %s %s" % (r["name"], r["version"]))
    ui.info("page:    %s/#/package/%s" % (paths.config()["hub"].rstrip("/"), r["name"]))
    ui.info("install: aihub install %s" % r["name"])


def dev_stats(a):
    name = _load(a)["package"]["name"]
    d = api.call("GET", "/packages/%s/stats" % name)
    if not ui.TTY:
        print(json.dumps(d, indent=2))
        return
    ui.card("%s - last %s days" % (name, d.get("days", 30)),
            ["%-10s %s" % (k, v) for k, v in sorted((d.get("by_kind") or {}).items())] or ["no events yet"],
            ui.dim("active users: ") + str(d.get("active_users", 0)))


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
    ap.register("action", "parsers", _GroupedSubParsersAction)
    sp = ap.add_subparsers(dest="cmd", metavar="command")
    sp.command_groups = [
        ("Discover", ["search", "info"]),
        ("Install", ["install", "uninstall", "update", "list", "download"]),
        ("Manage", ["enable", "disable", "tree", "lock", "sync"]),
        ("Account", ["config", "login", "register"]),
        ("Publish", ["dev"]),
        ("Setup and maintenance", ["welcome", "doctor", "version", "upgrade", "hook", "flush", "hooks", "skill"]),
    ]

    def add(name, fn, help, description, epilog, *args):
        p = sp.add_parser(name, help=help, description=description, epilog=epilog,
                          formatter_class=HelpFormatter)
        for a in args:
            p.add_argument(*a[0], **a[1])
        p.set_defaults(fn=fn)
        return p

    arg = lambda *n, **k: (n, k)
    add("config", cmd_config, "Show or change CLI configuration.",
        "Print the current CLI configuration, or set a configuration value. The hub URL is stored under the key 'hub'.",
        "Examples:\n  aihub config\n  aihub config set hub https://hub.example.com",
        arg("action", nargs="?", default="show", metavar="ACTION", help="'show' (default) or 'set'."),
        arg("key", nargs="?", metavar="KEY", help="Configuration key to change when ACTION is 'set'."),
        arg("value", nargs="?", metavar="VALUE", help="New value for KEY when ACTION is 'set'."))
    add("login", cmd_login, "Sign in and save an access token.",
        "Sign in to the configured hub. If USERNAME is omitted, the CLI prompts for it; the password is entered without echo and the token is saved locally.",
        "Examples:\n  aihub login\n  aihub login sam",
        arg("username", nargs="?", metavar="USERNAME", help="Account username; prompted for when omitted."))
    add("register", cmd_register, "Create an account on the configured hub.",
        "Prompt for a username and password and create an account on the configured hub. Registration is subject to the hub's signup policy; sign in separately with 'aihub login'.",
        "Example:\n  aihub register")
    add("search", cmd_search, "Search the hub for packages.",
        "Search package names and descriptions on the configured hub. Omitting TERM lists the available packages; at most 30 results are shown.",
        "Examples:\n  aihub search sql\n  aihub search",
        arg("term", nargs="?", default="", metavar="TERM", help="Words to search for; omit to browse packages."))
    add("info", cmd_info, "Show package details and available versions.",
        "Fetch package metadata, including its description, type, latest version, tags, requirements, and recent versions. Non-interactive output is JSON.",
        "Examples:\n  aihub info my-skill\n  aihub info my-mcp-server",
        arg("name", metavar="PACKAGE", help="Package name on the configured hub."))
    add("install", cmd_install, "Install a package and register its components.",
        "Resolve and install a skill, agent, MCP server, tool, or setup package. Install version constraints use comparison operators, for example 'name>=1.0' or 'name>=1.0,<2'; quote comma-separated constraints for the shell. Use 'name==1.2.0' for an exact install version. The 'name@1.2.0' shorthand is supported by download. The CLI verifies the archive, installs declared dependencies, and can register components with detected tools. Interactive installs ask before scripts and tool registration; --yes accepts those prompts' defaults.",

        "Examples:\n  aihub install my-skill\n  aihub install \"my-skill>=1.2\"\n  aihub install my-skill --yes --tool claude",
        arg("name", metavar="PACKAGE[<SPEC>]", help="Package name, optionally followed by a version constraint such as name>=1.0,<2."),
        arg("-y", "--yes", action="store_true", help="Skip install-script and tool-registration prompts; use detected tools unless --tool is given."),
        arg("--tool", action="append", metavar="TOOL", help="Register components with this tool; repeat to select multiple (claude, codex, or opencode)."))
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
        "Resolve a package version and save its archive in the output folder. Downloads resume from a partial file when possible and verify SHA-256 before the final file is saved. Use name@1.2.0 for an exact version, or comparison constraints such as name>=1.0,<2.",
        "Examples:\n  aihub download my-skill@1.0.0 -o ./pkgs\n  aihub download \"my-skill>=1.0,<2\"",
        arg("name", metavar="PACKAGE[VERSION]", help="Package name, name@1.2.0, or a constraint such as name>=1.0,<2."),
        arg("-o", "--output", metavar="DIR", help="Folder for the archive (default: current directory)."))
    add("uninstall", cmd_uninstall, "Remove an installed package and undo its changes.",
        "Remove the named installed package, its files and virtual environment, and revert recorded tool registrations and setup changes.",
        "Example:\n  aihub uninstall my-skill",
        arg("name", metavar="PACKAGE", help="Name of the installed package to remove."),
        arg("--force", action="store_true", help="Do not ask when other packages depend on it."))
    add("list", cmd_list, "List packages installed by this CLI.",
        "Show each locally installed package, its installed version, and the components it adds to supported tools.",
        "Example:\n  aihub list")
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
        "Download the CLI from the configured hub, verify its SHA-256, and replace the running copy. The previous copy is kept so --rollback can restore it. The CLI also checks once a day on its own; turn that off with 'aihub config set auto_update false' or AIHUB_NO_UPDATE=1.",
        "Examples:\n  aihub upgrade\n  aihub upgrade --check\n  aihub upgrade --rollback",
        arg("--check", action="store_true", help="Only report whether a newer version exists."),
        arg("--force", action="store_true", help="Reinstall even when already current."),
        arg("--rollback", action="store_true", help="Restore the version that was installed before the last upgrade."))
    add("welcome", cmd_welcome, "Run first-time setup for detected AI tools.",
        "Check the hub and detected tools, offer or install usage hooks and the built-in packaging skill, and optionally sign in or register. Interactive setup lets you choose; --yes accepts defaults without prompting.",
        "Examples:\n  aihub welcome\n  aihub welcome --yes",
        arg("-y", "--yes", action="store_true", help="Accept setup defaults and do not prompt."))
    add("hook", cmd_hook, "Record one AI-tool usage event from standard input.",
        "Read a tool-call event as JSON from standard input, match it to an installed package component, and queue usage telemetry. Intended for integration hooks: it spools one event and exits without blocking the AI tool.",
        "Example:\n  printf '{}' | aihub hook --source claude",
        arg("--source", default="claude", metavar="TOOL", help="Source label recorded with the event (default: claude)."))
    add("flush", cmd_flush, "Send queued usage events to the hub.",
        "Flush locally queued usage telemetry to the configured hub. The default allows a short linger for more events; --verbose skips that linger and prints the number sent.",
        "Examples:\n  aihub flush\n  aihub flush --verbose",
        arg("-v", "--verbose", action="store_true", help="Skip the linger period and print the number of events sent."))
    add("hooks", cmd_hooks, "Install or remove usage hooks for detected tools.",
        "Install or remove AI-tool hooks that report usage of components from installed packages. The optional tool filter is repeatable; only detected tools are processed, and Codex has no tool-call hook.",
        "Examples:\n  aihub hooks install\n  aihub hooks install --tool claude\n  aihub hooks remove",
        arg("action", nargs="?", choices=["install", "remove"], default="install", metavar="ACTION", help="Install hooks (default) or remove them."),
        arg("--tool", action="append", metavar="TOOL", help="Only process this detected tool; repeat to filter multiple tools."))
    add("skill", cmd_skill, "Install or remove the built-in skill packages.",
        "Install, repair or remove the built-in packages (aihub-guide and aihub-package). They are ordinary hub packages, published by the server on startup and installed automatically by the hub installer; this command re-runs that step. Use `aihub update` to upgrade them and `aihub uninstall <name>` to remove one.",
        "Examples:\n  aihub skill install\n  aihub skill install --tool claude\n  aihub skill remove",
        arg("action", nargs="?", choices=["install", "remove"], default="install", metavar="ACTION", help="Install the packages (default) or remove them."),
        arg("--tool", action="append", choices=["claude", "codex", "opencode"], metavar="TOOL", help="Register only with this tool; repeat for several (default: detected tools)."))

    dev = sp.add_parser("dev", help="Create, validate, build, publish, and inspect packages.",
                        description="Develop an AI Hub package from an aihub.toml project: generate starter files, lint the manifest, build an archive, publish releases, or inspect usage statistics.",
                        epilog="Examples:\n  aihub dev init . --type skill --name my-skill\n  aihub dev validate\n  aihub dev publish --bump patch",
                        formatter_class=HelpFormatter)
    ds = dev.add_subparsers(dest="dcmd", required=True, metavar="command")
    dev_commands = [
        ("init", dev_init, "Create starter files for a package project.",
         "Create an aihub.toml manifest and starter files for the selected package type in PATH. Existing generated files are left alone unless --force is used; --force overwrites generated starter files.",
         "Examples:\n  aihub dev init . --type skill --name my-skill\n  aihub dev init ./server --type mcp --description \"Example MCP server\"",
         [arg("--name", metavar="NAME", help="Package name (default: derived from the project directory)."),
          arg("--type", default="skill", choices=["skill", "agent", "mcp", "tool", "setup"], metavar="TYPE", help="Starter package type (default: skill)."),
          arg("--description", metavar="TEXT", help="Starter package description."),
          arg("--force", action="store_true", help="Overwrite generated starter files that already exist.")]),
        ("validate", dev_validate, "Validate and lint the package project.",
         "Parse and normalize PATH/aihub.toml, then lint the manifest and referenced package files. Report warnings and fail if validation errors are found.",
         "Example:\n  aihub dev validate ./my-skill", []),
        ("build", dev_build, "Validate and build a package archive.",
         "Validate the project, collect files selected by the project's ignore rules, and write a versioned .tar.gz archive under PATH/dist/.",
         "Example:\n  aihub dev build ./my-skill", []),
        ("publish", dev_publish, "Build and upload a package release.",
         "Optionally bump the version in aihub.toml, validate and build the package, then upload it to the configured hub. Large packages use resumable chunked uploads; smaller packages use a single upload request.",
         "Examples:\n  aihub dev publish\n  aihub dev publish --bump patch",
         [arg("--bump", choices=["major", "minor", "patch"], metavar="PART", help="Increment this version part before building (major/minor/patch; lower parts reset).")]),
        ("stats", dev_stats, "Show usage statistics for this package.",
         "Read usage statistics for the package named in PATH/aihub.toml. Non-interactive output is JSON.",
         "Example:\n  aihub dev stats ./my-skill", []),
    ]
    for n, fn, help, description, epilog, extra in dev_commands:
        p = ds.add_parser(n, help=help, description=description, epilog=epilog,
                          formatter_class=HelpFormatter)
        p.add_argument("path", nargs="?", default=".", metavar="PATH",
                       help="Project directory containing aihub.toml (default: current directory).")
        for e in extra:
            p.add_argument(*e[0], **e[1])
        p.set_defaults(fn=fn)
    return ap


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
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
        if ui.INTERACTIVE and a.cmd not in ("hook", "flush", "welcome", "config") and not os.path.exists(paths.p(".welcomed")):
            cmd_welcome(argparse.Namespace(yes=False))
        a.fn(a)
        updater.auto(a.cmd)
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
