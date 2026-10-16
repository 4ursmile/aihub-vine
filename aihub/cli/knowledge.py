"""`aihub knowledge`: personal notes (markdown) kept on the hub and mirrored to ~/.aihub/knowledge for bots and tools.
`soul.md` is the persona every bot loads first. Stdlib only."""
import os
import re
import subprocess

from . import api, paths, ui

NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SOUL = "# Soul\n\nWho the assistant is for me: tone, language, what to always / never do.\n\n- Be brief and concrete.\n- Ask before anything destructive.\n"


def folder():
    return paths.ensure("knowledge")


def _file(name):
    n = name[:-3] if name.endswith(".md") else name
    if not NAME.match(n):
        raise ValueError("note name: lowercase letters, digits, . _ - (max 64)")
    return n, os.path.join(folder(), n + ".md")


def local():
    return sorted(f[:-3] for f in os.listdir(folder()) if f.endswith(".md"))


def shared_dir(owner=None):
    """Notes other people shared with me: ~/.aihub/knowledge/shared/<owner>/<note>.md (read-only copies unless the share allows write)."""
    return paths.ensure("knowledge", "shared", owner) if owner else paths.ensure("knowledge", "shared")


def pull():
    n = 0
    for it in api.call("GET", "/me/knowledge")["items"]:
        d = api.call("GET", "/me/knowledge/" + it["name"])
        with open(os.path.join(folder(), it["name"] + ".md"), "w", encoding="utf-8") as f:
            f.write(d["body"])
        n += 1
    return n


def pull_shared():
    """-> list of (owner, name, access). Removes copies whose share is gone so a revoked note does not linger."""
    got, keep = [], set()
    for it in api.call("GET", "/knowledge/shared")["items"]:
        d = api.call("GET", "/knowledge/shared/%s/%s" % (it["owner"], it["name"]))
        p = os.path.join(shared_dir(it["owner"]), it["name"] + ".md")
        with open(p, "w", encoding="utf-8") as f:
            f.write(d["body"])
        keep.add(p)
        got.append((it["owner"], it["name"], it["access"]))
    root = shared_dir()
    for o in os.listdir(root):
        d = os.path.join(root, o)
        if os.path.isdir(d):
            for f in os.listdir(d):
                if f.endswith(".md") and os.path.join(d, f) not in keep:
                    os.remove(os.path.join(d, f))
    return got


def _target(a):
    if a.user:
        return {"user": a.user}
    if a.group:
        return {"group": a.group}
    raise ValueError("say who: --user NAME or --group NAME")


def push(name):
    n, p = _file(name)
    with open(p, encoding="utf-8") as f:
        api.call("PUT", "/me/knowledge/" + n, {"body": f.read()})
    return n


def get(name):
    """Body of a note, printed as is. `owner/note` = a note shared with me. The hub is asked first (always current), the local copy is the fallback."""
    if "/" in name:
        o, n = name.split("/", 1)
        n = _file(n)[0]
        try:
            return api.call("GET", "/knowledge/shared/%s/%s" % (o, n))["body"]
        except api.ApiError:
            p = os.path.join(shared_dir(o), n + ".md")
    else:
        n, p = _file(name)
        try:
            return api.call("GET", "/me/knowledge/" + n)["body"]
        except api.ApiError:
            pass
    if not os.path.isfile(p):
        raise ValueError("no note '%s' (aihub knowledge list)" % name)
    with open(p, encoding="utf-8") as f:
        return f.read()


def cli(a):
    act = a.action
    if act == "get":
        if not a.name:
            raise ValueError("name the note: aihub knowledge get soul")
        body = get(a.name)
        print(body, end="" if body.endswith("\n") else "\n")          # plain stdout: pipe it into a prompt or a file
        return
    ui.out()
    if act == "path":
        ui.out(folder())
    elif act == "list":
        remote = {}
        try:
            remote = {i["name"]: i for i in api.call("GET", "/me/knowledge")["items"]}
        except api.ApiError as e:
            ui.note("hub not reachable or not signed in (%s); showing local notes only" % e)
        names = sorted(set(local()) | set(remote))
        if not names:
            ui.info("no notes yet. create one: aihub knowledge edit soul")
        ui.table(["NOTE", "LOCAL", "HUB"], [[ui.bold(n), "yes" if n in local() else "-", "yes" if n in remote else "-"] for n in names])
        ui.info("folder: " + folder())
    elif act == "pull":
        ui.good("%d notes pulled into %s" % (pull(), folder()))
        try:
            sh = pull_shared()
            if sh:
                ui.good("%d shared notes pulled into %s" % (len(sh), shared_dir()))
        except api.ApiError as e:
            ui.note("shared notes skipped (%s)" % e)
    elif act == "shared":
        items = api.call("GET", "/knowledge/shared")["items"]
        if not items:
            ui.info("nobody shared a note with you yet")
        ui.table(["OWNER", "NOTE", "ACCESS"], [[o["owner"], ui.bold(o["name"]), o["access"]] for o in items])
        ui.info("folder (after `aihub knowledge pull`): " + shared_dir())
    elif act == "share":
        n, _ = _file(a.name or "")
        r = api.call("PUT", "/me/knowledge/%s/shares" % n, dict(_target(a), access="write" if a.write else "read"))
        ui.good("%s shared with %s (%s)" % (n, r["shared_with"], r["access"]))
    elif act == "unshare":
        n, _ = _file(a.name or "")
        api.call("DELETE", "/me/knowledge/%s/shares" % n, _target(a))
        ui.good("%s no longer shared with %s" % (n, a.user or a.group))
    elif act == "shares":
        n, _ = _file(a.name or "")
        items = api.call("GET", "/me/knowledge/%s/shares" % n)["items"]
        if not items:
            ui.info(n + " is private")
        ui.table(["WITH", "TYPE", "ACCESS"], [[i["name"], i["type"], i["access"]] for i in items])
    elif act == "push-shared":
        if not a.name or "/" not in a.name:
            raise ValueError("name it owner/note: aihub knowledge push-shared alice/handbook")
        o, n = a.name.split("/", 1)
        with open(os.path.join(shared_dir(o), n + ".md"), encoding="utf-8") as f:
            api.call("PUT", "/knowledge/shared/%s/%s" % (o, n), {"body": f.read()})
        ui.good("pushed %s/%s" % (o, n))
    elif act == "push":
        names = [a.name] if a.name else local()
        for n in names:
            ui.good("pushed " + push(n))
    elif act == "edit":
        if not a.name:
            raise ValueError("name the note: aihub knowledge edit soul")
        n, p = _file(a.name)
        if not os.path.exists(p):
            with open(p, "w", encoding="utf-8") as f:
                f.write(SOUL if n == "soul" else "# %s\n\n" % n)
        ed = os.environ.get("EDITOR") or os.environ.get("VISUAL")
        if ed and ui.INTERACTIVE:
            subprocess.call(ed.split() + [p])
            if ui._ask("push to the hub? [Y/n] ").lower() in ("", "y", "yes"):
                ui.good("pushed " + push(n))
        else:
            ui.info("edit this file, then run: aihub knowledge push %s" % n)
            ui.out(p)
    elif act == "rm":
        n, p = _file(a.name or "")
        if os.path.exists(p):
            os.remove(p)
        try:
            api.call("DELETE", "/me/knowledge/" + n)
        except api.ApiError:
            pass
        ui.good("removed " + n)
