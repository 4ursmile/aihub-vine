import argparse
import getpass
import json
import os
import sys
import tempfile

from ..core import archive, ignore, manifest as M, version as V
from . import api, installer, paths

def cmd_config(a):
    c = paths.load("config.json", {})
    if a.action == "set":
        c[a.key] = a.value
        paths.save("config.json", c)
    print(json.dumps(paths.config(), indent=2))


def cmd_login(a):
    u = a.username or input("username: ")
    r = api.call("POST", "/auth/login", {"username": u, "password": getpass.getpass("password: ")})
    paths.save("credentials.json", {"token": r["token"], "username": u}, private=True)
    print("logged in as", u)


def cmd_register(a):
    u = input("username: ")
    api.call("POST", "/auth/register", {"username": u, "password": getpass.getpass("password: ")})
    print("registered; now run: aihub login")


def cmd_search(a):
    r = api.call("GET", "/packages?q=%s&per_page=30" % a.term)
    for p in r["items"]:
        print("%-28s %-8s %-10s %s" % (p["name"], p["type"], p["latest_version"], p["description"][:60]))


def cmd_info(a):
    print(json.dumps(api.call("GET", "/packages/" + a.name), indent=2))


def cmd_install(a):
    from .installer import _spec
    n, s = _spec(a.name)
    installer.install(n, s, a.yes, a.tool)


def cmd_uninstall(a): installer.uninstall(a.name)


def cmd_list(a):
    for n, r in installer.state()["packages"].items():
        print(n, r["version"])


def cmd_update(a):
    for n, r in installer.state()["packages"].items():
        if a.name and n != a.name:
            continue
        latest = api.call("GET", "/resolve?name=" + n)["version"]
        if V.compare(latest, r["version"]) > 0:
            print("updating %s %s -> %s" % (n, r["version"], latest))
            installer.install(n, "", True, [t for t in ("claude", "codex", "opencode") if False] or None)
        else:
            print(n, "up to date")


def cmd_doctor(a):
    import shutil
    print("hub:", paths.config()["hub"])
    try:
        api.call("GET", "/healthz", timeout=5); print("hub reachable: yes")
    except Exception as e:
        print("hub reachable: NO (%s)" % e)
    from . import integrations
    for t in integrations.detected():
        print("tool %-9s usage hook: %s" % (t.name, "yes" if t.has_hook() else "no  (run: aihub hooks install)"))
    print("python3:", shutil.which("python3"))


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
    """Install the built-in packaging skill into Claude Code and/or Codex (user or project scope)."""
    from . import integrations
    for label, status in integrations.install_builtin_skill(a.tool, a.scope, a.path, remove=a.action == "remove"):
        print("%-12s %s" % (label, status))


def cmd_hooks(a):
    from . import hooks
    for label, status in hooks.ensure(a.tool, remove=a.action == "remove"):
        print("%-12s %s" % (label, status))


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
    print("created a %s project in %s" % (a.type, d))
    for m in sorted(made):
        print("  " + m)
    print("next:  edit the files, then  aihub dev validate  &&  aihub dev publish")


def _load(a):
    with open(os.path.join(_root(a), "aihub.toml")) as f:
        return M.parse(f.read())


def dev_validate(a, quiet=False):
    m = _load(a)
    errs, warns = M.lint(m, _root(a))
    for w in warns:
        print("warning:", w)
    for e in errs:
        print("error:  ", e)
    if errs:
        raise ValueError("%d problem(s) in the project; fix them and run `aihub dev validate` again" % len(errs))
    if not quiet:
        print("ok: %s %s (%s)" % (m["package"]["name"], m["package"]["version"], m["package"]["type"]))
    return m


def dev_build(a):
    m = dev_validate(a, quiet=True)
    out = os.path.join(_root(a), "dist")
    os.makedirs(out, exist_ok=True)
    dest = os.path.join(out, "%s-%s.tar.gz" % (m["package"]["name"], m["package"]["version"]))
    files = [f for f in ignore.list_files(_root(a)) if not f.startswith("dist/")]
    archive.build(_root(a), files, dest)
    print("built", dest, "(%d files)" % len(files))
    return dest


def dev_publish(a):
    if a.bump:
        m = _load(a)
        p = os.path.join(_root(a), "aihub.toml")
        t = open(p).read()
        old = m["package"]["version"]
        open(p, "w").write(t.replace('version = "%s"' % old, 'version = "%s"' % V.bump(old, a.bump), 1))
    dest = dev_build(a)
    with open(dest, "rb") as f:
        r = api.call("POST", "/upload", raw=f.read(), headers={"X-Aihub-Filename": os.path.basename(dest),
                                                              "Content-Type": "application/octet-stream"}, timeout=300)
    print("published %s %s" % (r["name"], r["version"]))


def dev_stats(a):
    print(json.dumps(api.call("GET", "/packages/%s/stats" % _load(a)["package"]["name"]), indent=2))


def build_parser():
    ap = argparse.ArgumentParser(prog="aihub")
    sp = ap.add_subparsers(dest="cmd", required=True)
    def add(name, fn, *args):
        p = sp.add_parser(name)
        for a in args:
            p.add_argument(*a[0], **a[1])
        p.set_defaults(fn=fn)
        return p
    arg = lambda *n, **k: (n, k)
    add("config", cmd_config, arg("action", nargs="?", default="show"), arg("key", nargs="?"), arg("value", nargs="?"))
    add("login", cmd_login, arg("username", nargs="?"))
    add("register", cmd_register)
    add("search", cmd_search, arg("term", nargs="?", default=""))
    add("info", cmd_info, arg("name"))
    add("install", cmd_install, arg("name"), arg("-y", "--yes", action="store_true"), arg("--tool", action="append"))
    add("uninstall", cmd_uninstall, arg("name"))
    add("list", cmd_list)
    add("update", cmd_update, arg("name", nargs="?"))
    add("doctor", cmd_doctor)
    add("hook", cmd_hook, arg("--source", default="claude"))
    add("flush", cmd_flush, arg("-v", "--verbose", action="store_true"))
    add("hooks", cmd_hooks, arg("action", nargs="?", choices=["install", "remove"], default="install"),
        arg("--tool", action="append"))
    add("skill", cmd_skill, arg("action", nargs="?", choices=["install", "remove"], default="install"),
        arg("--tool", action="append", choices=["claude", "codex"]),
        arg("--scope", choices=["user", "project"], default="user"), arg("--path", default="."))
    dev = sp.add_parser("dev")
    ds = dev.add_subparsers(dest="dcmd", required=True)
    for n, fn, extra in [("init", dev_init, [arg("--name"), arg("--type", default="skill", choices=["skill", "agent", "mcp", "tool", "setup"]),
                                           arg("--description"), arg("--force", action="store_true")]), ("validate", dev_validate, []),
                         ("build", dev_build, []), ("publish", dev_publish, [arg("--bump", choices=["major", "minor", "patch"])]),
                         ("stats", dev_stats, [])]:
        p = ds.add_parser(n)
        p.add_argument("path", nargs="?", default=".")
        for e in extra:
            p.add_argument(*e[0], **e[1])
        p.set_defaults(fn=fn)
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        a.fn(a)
    except (api.ApiError, ValueError, KeyError) as e:
        print("error:", e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
