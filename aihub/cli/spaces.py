"""`aihub space`: publish documents, slides, reports, PDFs, sheets, dashboards and HTML pages to the hub as a Space (`author@space-name`).
Stdlib only. A folder is packed as a .tar.gz site (needs index.html); a file is sent as it is."""
import io
import os
import re
import tarfile
import urllib.parse
import webbrowser

from . import api, paths, ui

KINDS = ("doc", "slides", "report", "pdf", "sheet", "dashboard", "html")
SKIP = {".git", "node_modules", "__pycache__", ".DS_Store", ".venv"}
REF = re.compile(r"^[^@\s/]+@[a-z0-9][a-z0-9._-]{0,63}$", re.I)


def _ref(s):
    s = (s or "").strip()
    if s.startswith("http"):                                  # a pasted link: .../#/s/owner@name
        s = urllib.parse.unquote(s.split("#/s/")[-1].split("?")[0])
    if not REF.match(s):
        raise ValueError("a space is addressed author@space-name, e.g. alice@q3-review")
    return s


def link(ref, **q):
    hub = paths.config()["hub"].rstrip("/")
    return "%s/#/s/%s%s" % (hub, urllib.parse.quote(ref, safe="@"), ("?" + urllib.parse.urlencode(q)) if q else "")


def _pack(folder):
    if not os.path.isfile(os.path.join(folder, "index.html")):
        raise ValueError("%s has no index.html at its top: a folder is published as a web page" % folder)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for root, dirs, files in os.walk(folder):
            dirs[:] = sorted(d for d in dirs if d not in SKIP and not d.startswith("."))
            for f in sorted(files):
                if f in SKIP or f.startswith("."):
                    continue
                full = os.path.join(root, f)
                if os.path.islink(full):
                    continue
                t.add(full, arcname=os.path.relpath(full, folder).replace(os.sep, "/"))
    return buf.getvalue(), os.path.basename(os.path.abspath(folder)) + ".tar.gz"


def _publish(a):
    path = os.path.expanduser(a.path)
    if os.path.isdir(path):
        data, fname = _pack(path)
    elif os.path.isfile(path):
        with open(path, "rb") as f:
            data = f.read()
        fname = os.path.basename(path)
    else:
        raise ValueError("no such file or folder: " + a.path)
    q = {"filename": urllib.parse.quote(fname)}
    for k in ("kind", "title", "description", "tags", "view", "transition", "autoplay"):
        v = getattr(a, k, None)
        if v not in (None, ""):
            q[k] = v
    if a.loop is not None:
        q["loop"] = "1" if a.loop else "0"
    if a.private and a.public:
        raise ValueError("choose --public or --private, not both")
    if a.private or a.public:
        q["visibility"] = "private" if a.private else "public"
    target = a.name or ""
    if "@" in target:                                        # new version of somebody else's space (needs edit access)
        res = api._retry(lambda: api.call("PUT", "/spaces/%s/content?%s" % (urllib.parse.quote(_ref(target), safe="@"), urllib.parse.urlencode(q)),
                                         raw=data, headers={"Content-Type": "application/octet-stream"}, timeout=900))
    else:
        if target:
            q["name"] = target
        res = api._retry(lambda: api.call("POST", "/spaces?" + urllib.parse.urlencode(q), raw=data,
                                         headers={"Content-Type": "application/octet-stream"}, timeout=900))
    sp = res["space"]
    ui.result(dict(res, url=link(sp["ref"])))
    ui.out()
    ui.good("%s %s (%s, %s, rev %d)" % ("published" if res["created"] else "updated", ui.bold(sp["ref"]), sp["kind"], sp["visibility"], sp["rev"]))
    ui.info("open: " + link(sp["ref"]))
    ui.info("full screen: " + link(sp["ref"], mode="full") + "   embed without the hub frame: " + link(sp["ref"], bare=1))
    if sp["visibility"] == "private":
        ui.info("private: share it with `aihub space share %s --user NAME` or --group NAME" % sp["ref"])
    if a.open:
        webbrowser.open(link(sp["ref"]))


def _row(s):
    return [ui.bold(s["ref"]), s["kind"], "private" if s["visibility"] == "private" else "public", str(s["views"]), s["title"]]


def _list(a, sort):
    q = {"sort": sort, "limit": a.limit}
    if getattr(a, "kind", None):
        q["kind"] = a.kind
    if getattr(a, "query", None):
        q["q"] = a.query
    if getattr(a, "mine", False):
        q["mine"] = "true"
    res = api.call("GET", "/spaces?" + urllib.parse.urlencode(q))
    ui.result(res)
    ui.out()
    if not res["items"]:
        ui.info("no spaces found")
        return
    ui.table(["SPACE", "KIND", "WHO", "VIEWS", "TITLE"], [_row(s) for s in res["items"]])
    if res["total"] > len(res["items"]):
        ui.info("%d of %d shown (--limit)" % (len(res["items"]), res["total"]))


def cli(a):
    act = a.action
    if act == "publish":
        if not a.target:
            raise ValueError("aihub space publish PATH [NAME]   (a file, or a folder with index.html)")
        a.path, a.name = a.target, a.extra
        return _publish(a)
    if act in ("list", "top", "search"):
        a.query = a.extra if act == "search" else None
        if act == "search" and not a.query:
            raise ValueError("aihub space search WORDS")
        return _list(a, "top" if act == "top" else "new" if act == "list" else "top")
    ref = _ref(a.target)
    e = urllib.parse.quote(ref, safe="@")
    if act == "info":
        d = api.call("GET", "/spaces/" + e)
        ui.result(d)
        ui.out()
        ui.out("%s  %s" % (ui.bold(d["ref"]), d["title"]))
        for k in ("kind", "visibility", "views", "rev", "filename", "size", "tags", "description"):
            ui.out("  %-12s %s" % (k, ", ".join(d[k]) if isinstance(d[k], list) else d[k]))
        ui.out("  %-12s %s" % ("viewer", json_line(d["settings"]) or "(defaults)"))
        ui.out("  %-12s %s" % ("link", link(ref)))
        if d.get("can_manage"):
            for x in api.call("GET", "/spaces/%s/access" % e)["access"]:
                ui.out("  shared       %s %s: %s" % (x["type"], x["name"], x["access"]))
    elif act == "url":
        u = link(ref, **({"mode": a.view} if a.view else {}), **({"bare": 1} if a.bare else {}))
        ui.result({"url": u})
        print(u)
    elif act == "open":
        u = link(ref, **({"mode": a.view} if a.view else {}))
        ui.result({"url": u})
        ui.out()
        ui.out(u)
        webbrowser.open(u)
    elif act == "get":
        out = a.output or api.call("GET", "/spaces/" + e)["filename"] or (ref.split("@")[1] + ".bin")
        with api._req("GET", "/spaces/%s/file" % e, timeout=600) as r, open(out, "wb") as f:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)
        ui.result({"saved": out})
        ui.out()
        ui.good("saved " + out)
    elif act == "rm":
        if not a.yes and not ui.confirm("Delete %s for everyone?" % ref, default=False):
            raise ValueError("not deleted (use --yes to skip the question)")
        api.call("DELETE", "/spaces/" + e)
        ui.out()
        ui.good("deleted " + ref)
    elif act == "set":
        body = {}
        for k in ("title", "description", "tags", "view", "transition", "autoplay", "kind"):
            v = getattr(a, k, None)
            if v not in (None, ""):
                body[k] = v
        if a.loop is not None:
            body["loop"] = a.loop
        if a.private or a.public:
            body["visibility"] = "private" if a.private else "public"
        if not body:
            raise ValueError("nothing to change: pass --title, --view, --transition, --public, ...")
        d = api.call("PATCH", "/spaces/" + e, body)
        ui.result(d)
        ui.out()
        ui.good("updated " + ref)
    elif act in ("share", "unshare", "shares"):
        if act == "shares":
            res = api.call("GET", "/spaces/%s/access" % e)
            ui.result(res)
            ui.out()
            ui.out("%s is %s" % (ref, res["visibility"]))
            for x in res["access"]:
                ui.out("  %-6s %-20s %s" % (x["type"], x["name"], x["access"]))
            return
        who = [("user", a.user)] if a.user else []
        who += [("group", a.group)] if a.group else []
        if not who:
            raise ValueError("name who: --user NAME or --group NAME")
        ui.out()
        for t, n in who:
            if act == "share":
                api.call("PUT", "/spaces/%s/access" % e, {"type": t, "name": n, "access": "edit" if a.edit else "view"})
                ui.good("shared %s with %s %s (%s)" % (ref, t, n, "edit" if a.edit else "view"))
            else:
                api.call("DELETE", "/spaces/%s/access/%s/%s" % (e, t, urllib.parse.quote(n)))
                ui.good("removed %s %s from %s" % (t, n, ref))
    else:
        raise ValueError("unknown action " + act)


def json_line(d):
    return " ".join("%s=%s" % (k, v) for k, v in sorted((d or {}).items()))
