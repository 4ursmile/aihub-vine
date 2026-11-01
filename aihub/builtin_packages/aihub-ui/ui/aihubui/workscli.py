"""Works without the UI: the engine of aihub-ui driven from a terminal (`aihub work ...` runs this module through the installed aihub-ui).

    aihub work pull workflow-sales-etl        install the workflow package and add its works (disabled)
    aihub work setup sales-etl                pick / create the credential sets it needs, its working folders, its secret fields
    aihub work env install sales-etl --env data         the Python packages its SQL / Spark / Python nodes need (one command)
    aihub work run sales-etl --input day=2026-10-31     one run in this terminal, live step log, exit code 0 / 1
    aihub work enable sales-etl && aihub work serve --bg     cron + package triggers in the background (supervised by `aihub run`)

Same files as the UI (~/.aihub/ui: works, runs, credentials, the encrypted secret box), so a work designed in the UI runs here as it is and
the other way round. Only ONE process fires cron / package triggers (works.Lease): the UI or `aihub work serve`, never both.
Secrets are typed at a hidden prompt or read from a JSON file / stdin, never from argv, and are never printed."""
import argparse
import getpass
import json
import os
import signal
import sys
import threading
import time

from . import aihub as A, nodeenv, store, workpkg, works as W
from .secretbox import Box

JSON = False
DONE = ("done", "failed", "stopped", "interrupted")


class Fail(Exception):
    pass


def out(msg=""):
    if not JSON:
        print(msg, flush=True)


def err(msg):
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def emit(doc):
    """The JSON document of a command (--json), or nothing."""
    if JSON:
        sys.stdout.write(json.dumps(doc, ensure_ascii=False, default=str, indent=1) + "\n")
        sys.stdout.flush()


def tty():
    return not JSON and sys.stdin.isatty()


def box():
    return Box(store.ui_dir())


def ask(prompt, default="", secret=False):
    if not tty():
        raise Fail("%s: needs an answer, but this is not a terminal (use a JSON file with --creds, or `aihub work set`)" % prompt)
    p = "%s%s: " % (prompt, " [%s]" % default if default and not secret else "")
    v = getpass.getpass(p) if secret else input(p)
    return v.strip() or default


def yes(prompt, default=True):
    if not tty():
        return default
    v = input("%s [%s] " % (prompt, "Y/n" if default else "y/N")).strip().lower()
    return default if not v else v.startswith("y")


def choose(prompt, options):
    """Numbered menu -> index. OPTIONS = labels."""
    for i, o in enumerate(options, 1):
        out("  %d) %s" % (i, o))
    while True:
        v = ask(prompt, "1")
        if v.isdigit() and 1 <= int(v) <= len(options):
            return int(v) - 1
        out("  pick a number from 1 to %d" % len(options))


# ---------- finding things

def find_work(ref):
    """A work by id, title (case does not matter, unique prefix ok) or the package it came from."""
    ref = str(ref or "").strip()
    rows = store.works()
    if store.ID.match(ref) and store.get_work(ref):
        return store.get_work(ref)
    low = ref.lower()
    for test in (lambda r: (r["title"] or "").lower() == low, lambda r: workpkg.slug(r["title"]) == low,
                 lambda r: ((store.get_work(r["id"]) or {}).get("package") or "") == ref, lambda r: (r["title"] or "").lower().startswith(low)):
        hits = [r for r in rows if test(r)]
        if len(hits) == 1:
            return store.get_work(hits[0]["id"])
        if len(hits) > 1:
            raise Fail("%r matches %d works (%s): use the id" % (ref, len(hits), ", ".join("%s %s" % (h["id"], h["title"]) for h in hits[:6])))
    raise Fail("no work %r (aihub work list)" % ref)


def find_node(w, ref):
    for n in w["nodes"]:
        if n["id"] == ref or (n.get("label") or "").lower() == str(ref).lower():
            return n
    raise Fail("no node %r in %s (nodes: %s)" % (ref, w["title"], ", ".join("%s (%s)" % (n["id"], n.get("label") or n["type"]) for n in w["nodes"])))


def find_set(ref):
    c = W.credential(ref) or next((c for c in W.credentials().values() if c["name"].lower() == str(ref).lower()), None)
    if not c:
        raise Fail("no credential set %r (aihub work cred list)" % ref)
    return c


def label(n, cat):
    return n.get("label") or (cat.get(n["type"]) or {}).get("title") or n["type"]


# ---------- list / show

def cmd_list(a):
    rows = []
    cat = W.catalogue()
    for r in store.works():
        w = store.get_work(r["id"]) or {}
        trig = sorted({n["type"] for n in w.get("nodes", []) if (cat.get(n["type"]) or {}).get("kind") == "trigger"})
        rows.append({"id": r["id"], "title": r["title"], "enabled": r["enabled"], "package": w.get("package") or "", "nodes": r["nodes"], "triggers": trig,
                     "needs": len([x for x in workpkg.needs_setup(w, cat, env=False) if x["required"]])})
    emit({"works": rows, "scheduler": W.scheduler()})
    if not rows:
        out("no works yet: design one in the UI, or `aihub work pull <workflow package>`")
    for r in rows:
        out("%s  %-3s %-34s %s%s%s" % (r["id"], "on" if r["enabled"] else "off", (r["title"] or "")[:34], ", ".join(t.replace("start.", "") for t in r["triggers"]),
                                     "  from " + r["package"] if r["package"] else "", "  NEEDS SETUP (%d)" % r["needs"] if r["needs"] else ""))
    sch = W.scheduler()
    out("\ntriggers fire in: %s" % ("%s (pid %s)" % (sch["who"], sch["pid"]) if sch else "nothing right now (start the UI or `aihub work serve --bg`)"))
    return 0


def describe(w, cat):
    order, _ = W.topo(w)
    nodes = {n["id"]: n for n in w["nodes"]}
    rows = []
    for k in order:
        n = nodes[k]
        spec = cat.get(n["type"]) or {}
        rows.append({"id": n["id"], "type": n["type"], "label": label(n, cat), "credential": n.get("credential") or "", "vault": n.get("vault") or [],
                     "cwd": (n.get("config") or {}).get("cwd") or "", "known": bool(spec)})
    return rows


def cmd_show(a):
    w = find_work(a.work)
    cat = W.catalogue()
    needs = workpkg.needs_setup(w, cat)
    doc = {"work": {k: w.get(k) for k in ("id", "title", "description", "enabled", "package")}, "nodes": describe(w, cat), "needs": needs,
           "inputs": store.work_inputs(w), "files": W.work_files_dir(w["id"]) if os.path.isdir(W.work_files_dir(w["id"])) else ""}
    emit(doc)
    out("%s  %s  (%s)" % (w["id"], w["title"], "on" if w.get("enabled") else "off"))
    if w.get("description"):
        out("  " + w["description"])
    for r in doc["nodes"]:
        extra = [x for x in (("set " + r["credential"]) if r["credential"] else "", ("reads " + ", ".join(r["vault"])) if r["vault"] else "",
                             ("folder " + r["cwd"]) if r["cwd"] else "", "" if r["known"] else "UNKNOWN TYPE (package missing?)") if x]
        out("  %-8s %-28s %-16s %s" % (r["id"], r["label"][:28], r["type"], "  ".join(extra)))
    if doc["inputs"]:
        out("inputs: " + ", ".join("%s%s" % (i["name"], "*" if i["required"] else "") for i in doc["inputs"]) + "   (aihub work run %s --input name=value)" % w["id"])
    if doc["files"]:
        out("files it brought: " + doc["files"])
    show_needs(w, needs)
    return 0


def show_needs(w, needs):
    if not needs:
        out("ready to run: aihub work run %s" % w["id"])
        return
    out("still to set up (aihub work setup %s):" % w["id"])
    for x in needs:
        out("  %s %-20s %s%s" % ("*" if x["required"] else "-", x["label"][:20], {"credential": "credential set '%s'" % x.get("kind"), "vault": "credential set '%s' for its code" % x.get("kind"),
                                                                       "folder": "working folder", "secret": "secret field %s" % x.get("field"),
                                                                       "env": "Python packages: %s" % ", ".join(x.get("packages") or []) if x.get("packages") else "Python environment",
                                                                       "system": x.get("name") or "system"}.get(x["what"], x["what"]),
                                    ("  (" + x["hint"] + ")") if x.get("hint") else ""))
    if any(x["what"] == "env" and x.get("packages") for x in needs):
        out("  one command for the packages: aihub work env install %s   (--env NAME = a separate environment)" % w["id"])


# ---------- pull / import / export

def cmd_pull(a):
    """Install a workflow package (aihub install) and add its works to Works, disabled."""
    try:
        A.run(["install", a.package, "--yes"] + (["-i"] if a.indirect else []), timeout=1800)
    except A.CliError as e:
        raise Fail("install failed: %s" % e)
    A.invalidate()
    name = a.package.split("@")[0].split("==")[0].split(">")[0].split("<")[0].strip()
    found = [x for x in workpkg.workflows() if x["package"] == name and (not a.file or x["file"] == a.file or x["title"].lower() == a.file.lower())]
    if not found:
        raise Fail("%s has no works (a workflow package keeps them in works/*.work.json)" % name)
    added = []
    for x in found:
        old = next((r for r in store.works() if (store.get_work(r["id"]) or {}).get("package") == name and r["title"] == x["title"]), None)
        if old and not a.again:
            out("already in Works: %s %s (use --again to add a fresh copy)" % (old["id"], old["title"]))
            added.append({"work": store.get_work(old["id"]), "needs": workpkg.needs_setup(store.get_work(old["id"])), "risks": x["risks"], "files": 0, "existing": True})
            continue
        added.append(workpkg.import_file(x["_path"], name))
    return report_import(added)


def cmd_import(a):
    p = os.path.abspath(os.path.expanduser(a.path))
    files = [p] if os.path.isfile(p) else [os.path.join(dp, f) for dp, _, fs in os.walk(p) for f in fs if f.endswith(".work.json")] if os.path.isdir(p) else []
    if not files:
        raise Fail("no .work.json at %s" % p)
    return report_import([workpkg.import_file(f, a.package or "") for f in sorted(files)])


def report_import(rows):
    emit({"imported": [{"id": r["work"]["id"], "title": r["work"]["title"], "needs": r["needs"], "risks": r["risks"], "files": r["files"]} for r in rows]})
    for r in rows:
        w = r["work"]
        if not r.get("existing"):
            out("added %s  %s  (off)%s" % (w["id"], w["title"], ", %d file(s) it brought" % r["files"] if r["files"] else ""))
        for k in r["risks"]:
            out("  ! " + k)
        show_needs(w, r["needs"])
    return 0


def cmd_export(a):
    w = find_work(a.work)
    dest = os.path.abspath(os.path.expanduser(a.out or "."))
    os.makedirs(dest, exist_ok=True)
    wf = os.path.join(dest, workpkg.slug(w["title"]) + ".work.json")
    shared = workpkg.strip_for_share(w)
    import shutil
    shutil.rmtree(workpkg.files_dir_of(wf), ignore_errors=True)
    rep = workpkg.bundle(w, shared, workpkg.files_dir_of(wf), notes=not a.no_notes)
    with open(wf, "w", encoding="utf-8") as f:
        json.dump(shared, f, indent=1, ensure_ascii=False)
    emit({"path": wf, "files": rep["files"], "skipped": rep["skipped"]})
    out("wrote %s (no secrets, no credential sets)%s" % (wf, "; %d file(s) bundled" % len(rep["files"]) if rep["files"] else ""))
    for s in rep["skipped"]:
        out("  not bundled: " + s)
    return 0


# ---------- credentials

def read_json_arg(path):
    """JSON from a file, or '-' = stdin (so secrets never sit on a command line)."""
    try:
        raw = sys.stdin.read() if path == "-" else open(os.path.expanduser(path), encoding="utf-8").read()
        return json.loads(raw)
    except OSError as e:
        raise Fail("cannot read %s: %s" % (path, e.strerror or e))
    except ValueError as e:
        raise Fail("%s is not JSON: %s" % ("stdin" if path == "-" else path, e))


def fields_from_json(kd, doc):
    """A kind's fields from a JSON document: {"fields": {...}}, the fields themselves, or (a kind with one list field) just its list / dict of entries."""
    if isinstance(doc, dict) and isinstance(doc.get("fields"), dict):
        return doc["fields"]
    lists = [f for f in kd["fields"] if f.get("type") == "list"]
    if isinstance(doc, list) and len(lists) == 1:
        return {lists[0]["name"]: doc}
    if isinstance(doc, dict) and len(lists) == 1 and lists[0]["name"] not in doc and doc and all(isinstance(v, dict) for v in doc.values()):
        return {lists[0]["name"]: doc}                                  # {"raw": {...}, "lake": {...}} = the buckets themselves
    if isinstance(doc, dict):
        return doc
    raise Fail("the JSON must be an object of fields (aihub work cred template %s)" % kd["kind"])


def prompt_fields(kd, old=None):
    """Ask every field of kind KD at the terminal (secrets hidden). Enter keeps the stored value of an existing set."""
    old = (old or {}).get("fields") or {}
    vals = {}
    for f in kd["fields"]:
        if f.get("type") == "list":
            rows = []
            have = old.get(f["name"]) if isinstance(old.get(f["name"]), list) else []
            for e in have:
                if yes("keep %s %r?" % (f["label"].rstrip("s").lower() or "entry", e.get(f.get("key") or "name")), True):
                    rows.append(dict(e))
            while yes("add %s%s?" % ("a " if not rows else "another ", (f["label"].rstrip("s") or "entry").lower()), not rows):
                e = {}
                for it in f["items"]:
                    v = ask("  %s" % it["label"], secret=it.get("secret"))
                    if v:
                        e[it["name"]] = v
                rows.append(e)
            vals[f["name"]] = rows
            continue
        cur = old.get(f["name"])
        hint = " (Enter keeps the stored one)" if cur == W.MARK else ""
        v = ask("%s%s" % (f["label"], hint), "" if f.get("secret") else (cur or ""), secret=f.get("secret"))
        vals[f["name"]] = W.MARK if (not v and cur == W.MARK) else v
    if kd.get("free"):
        out("this kind takes any names; add key=value pairs (empty line to finish, secret-looking names are hidden and encrypted)")
        while True:
            k = ask("  name", "") if tty() else ""
            if not k:
                break
            vals[k] = ask("  %s" % k, secret=bool(W.SECRETISH.search(k)))
    return vals


def save_set(kind, name, fields, cid=None):
    try:
        return W.save_credential({"id": cid, "kind": kind, "name": name, "fields": fields}, box())
    except ValueError as e:
        raise Fail(str(e))


def show_set(c, kd):
    flat = []
    for k, v in c["fields"].items():
        if isinstance(v, list):
            key = next((f.get("key") for f in kd.get("fields", []) if f["name"] == k), "name") or "name"
            flat.append("%s: %s" % (k, ", ".join(str(e.get(key) or e.get("name") or e.get("id")) for e in v)))
        else:
            flat.append("%s=%s" % (k, "(stored)" if v == W.MARK else v))
    return "; ".join(flat)


def cmd_cred(a):
    ks = W.kinds()
    act = a.action
    if act in ("list", "ls"):
        rows = W.credentials_view()
        emit({"credentials": rows})
        for c in rows:
            out("%-20s %-10s %-24s %s" % (c["id"], c["kind"], c["name"][:24], show_set(c, ks.get(c["kind"]) or {})))
            if c["used_in"]:
                out("%20s used by %s" % ("", "; ".join("%s (%s)" % (u["title"], ", ".join(u["nodes"])) for u in c["used_in"])))
        if not rows:
            out("no credential sets yet: aihub work cred add --kind s3 --name lake   (kinds: aihub work cred kinds)")
        return 0
    if act == "kinds":
        emit({"kinds": list(ks.values())})
        for k in sorted(ks.values(), key=lambda k: k["kind"]):
            out("%-10s %s%s" % (k["kind"], k["title"], " (" + k["package"] + ")" if k["package"] else ""))
            for f in k["fields"]:
                out("%12s %s%s" % ("", f["name"], " [list of: %s]" % ", ".join(i["name"] + ("*" if i.get("secret") else "") for i in f["items"]) if f.get("type") == "list" else " (secret)" if f.get("secret") else ""))
        return 0
    if act == "template":
        kd = ks.get(a.ref or "")
        if not kd:
            raise Fail("which kind? %s" % ", ".join(sorted(ks)))
        doc = W.kind_template(kd)
        emit({"kind": kd["kind"], "template": doc})
        if not JSON:
            print(json.dumps(doc, indent=2))
        return 0
    if act == "show":
        c = find_set(a.ref)
        doc = dict(c, used_in=W.credential_usage().get(c["id"], []))
        emit({"credential": doc})
        out("%s  %s  (%s)" % (c["id"], c["name"], c["kind"]))
        for k, v in c["fields"].items():
            if isinstance(v, list):
                out("  %s:" % k)
                for e in v:
                    out("    - " + ", ".join("%s=%s" % (x, "(stored)" if y == W.MARK else y) for x, y in e.items() if x != "id"))
            else:
                out("  %s = %s" % (k, "(stored)" if v == W.MARK else v))
        return 0
    if act in ("add", "set"):
        if act == "set":
            old = find_set(a.ref)
            kd, kind, name = ks.get(old["kind"]), old["kind"], a.name or old["name"]
        else:
            old, kind = None, a.kind or (a.ref if a.ref in ks else "")
            if not kind:
                if not tty():
                    raise Fail("--kind KIND (aihub work cred kinds)")
                names = sorted(ks)
                kind = names[choose("kind", ["%s  %s" % (k, ks[k]["title"]) for k in names])]
            kd = ks.get(kind)
            if not kd:
                raise Fail("unknown kind %r (aihub work cred kinds)" % kind)
            name = a.name or ask("name of the set", kd["title"])
        if a.file:
            fields = fields_from_json(kd, read_json_arg(a.file))
            if old:                                                 # a partial file changes only what it names; stored secrets stay
                fields = dict(old["fields"], **fields)
        else:
            fields = prompt_fields(kd, old)
        c = save_set(kind, name, fields, old["id"] if old else None)
        emit({"credential": c})
        out("%s credential set %s (%s): %s" % ("updated" if old else "saved", c["name"], c["id"], show_set(c, kd)))
        return 0
    if act in ("rm", "delete"):
        c = find_set(a.ref)
        use = W.credential_usage().get(c["id"], [])
        if use and not a.yes and not yes("%s is used by %s. Delete it?" % (c["name"], ", ".join(u["title"] for u in use)), False):
            return 1
        W.delete_credential(c["id"], box())
        emit({"deleted": c["id"]})
        out("deleted " + c["name"])
        return 0
    raise Fail("aihub work cred list | kinds | template KIND | show SET | add [--kind K --name N --file F|-] | set SET [--file F|-] | rm SET")


# ---------- setup / set

def creds_file(path, ks):
    """--creds FILE: {"sets": [{kind, name, fields}]}, or {kind: fields | {name, fields}} -> [(kind, name, fields)]."""
    doc = read_json_arg(path)
    items = doc.get("sets") if isinstance(doc, dict) and isinstance(doc.get("sets"), list) else doc if isinstance(doc, list) else \
        [dict(v, kind=k) if isinstance(v, dict) and isinstance(v.get("fields"), dict) else {"kind": k, "fields": v} for k, v in (doc.items() if isinstance(doc, dict) else [])]
    out_ = []
    for it in items:
        k = str((it or {}).get("kind") or "")
        if k not in ks:
            raise Fail("--creds: unknown kind %r (aihub work cred kinds)" % k)
        out_.append((k, str(it.get("name") or ""), fields_from_json(ks[k], it.get("fields") if isinstance(it.get("fields"), (dict, list)) else it)))
    return out_


def pick_or_make(kind, name, ks, given, label_):
    """A credential set of KIND for LABEL_: from --creds, the one that fits, a choice, or a new one."""
    kd = ks.get(kind)
    if not kd:
        raise Fail("the kind %r is not known here: install the node package that defines it" % kind)
    for k, nm, fields in given:
        if k == kind:
            nm = nm or name or kd["title"]                              # unnamed in the file: the name the work was shared with
            have = next((c for c in W.credentials().values() if c["kind"] == kind and c["name"] == nm), None)
            return save_set(kind, nm, fields, have["id"] if have else None)
    same = [c for c in W.credentials().values() if c["kind"] == kind]
    if not tty():
        hit = next((c for c in same if c["name"] == name), same[0] if len(same) == 1 else None)
        if hit:
            return hit
        raise Fail("%s needs a '%s' credential set: aihub work cred add --kind %s --name NAME --file creds.json, or --creds FILE (template: aihub work cred template %s)" % (label_, kind, kind, kind))
    out("\n%s needs a '%s' credential set (%s)%s" % (label_, kind, kd["title"], " named '%s' where it was made" % name if name else ""))
    opts = ["use '%s'" % c["name"] for c in same] + ["create a new set (type the values)", "create a new set from a JSON file", "skip for now"]
    i = choose("choice", opts)
    if i < len(same):
        return same[i]
    i -= len(same)
    if i == 2:
        return None
    nm = ask("name of the new set", name or kd["title"])
    if i == 1:
        out("JSON template (aihub work cred template %s):\n%s" % (kind, json.dumps(W.kind_template(kd), indent=2)))
        fields = fields_from_json(kd, read_json_arg(ask("path of the JSON file")))
    else:
        fields = prompt_fields(kd)
    return save_set(kind, nm, fields)


def cmd_setup(a):
    w = find_work(a.work)
    cat, ks = W.catalogue(), W.kinds()
    given = creds_file(a.creds, ks) if a.creds else []
    needs = workpkg.needs_setup(w, cat)
    if not needs:
        emit({"work": w["id"], "needs": []})
        out("%s needs nothing more: aihub work run %s" % (w["title"], w["id"]))
        return 0
    nodes = {n["id"]: n for n in w["nodes"]}
    folders = dict(kv.split("=", 1) for kv in (a.folder or []) if "=" in kv)
    for x in needs:
        n = nodes[x["node"]]
        lab = "%s (%s)" % (x["label"], n["id"])
        if x["what"] in ("credential", "vault"):
            c = pick_or_make(x["kind"], x.get("name"), ks, given, lab)
            if not c:
                continue
            if x["what"] == "credential":
                n["credential"] = c["id"]
                n.pop("credential_kind", None)
                n.pop("credential_name", None)
            else:
                n["vault"] = [v for v in n.get("vault") or [] if v != c["id"]] + [c["id"]]
                n["vault_kinds"] = [v for v in n.get("vault_kinds") or [] if not (v.get("kind") == x["kind"] and (v.get("name") or "") == (x.get("name") or ""))]
                if not n["vault_kinds"]:
                    n.pop("vault_kinds")
            out("  %s -> set %s" % (lab, c["name"]))
        elif x["what"] == "folder":
            v = folders.get(n["id"]) or folders.get(x["label"]) or (ask("working folder for %s (was '%s'; empty = a fresh folder per run)" % (lab, x["name"]), "") if tty() else "")
            if v:
                try:
                    n.setdefault("config", {})["cwd"] = store.check_cwd(v)
                except ValueError as e:
                    raise Fail("%s: %s" % (lab, e))
                out("  %s works in %s" % (lab, n["config"]["cwd"]))
        elif x["what"] == "secret" and tty():
            v = ask("%s: %s%s" % (lab, x["name"], "" if x["required"] else " (optional, Enter skips)"), secret=True)
            if v:
                n.setdefault("config", {})[x["field"]] = v
    w = store.save_work(W.strip_secrets(w, box()))
    if any(x["what"] in ("env", "system") for x in needs) or a.env:
        if a.env or a.install or (tty() and yes("install the Python packages the nodes need now?")):   # never unasked from a script
            w = env_install(w, cat, env=a.env, node=None)
        else:
            out("  later: aihub work env install %s" % w["id"])
    left = workpkg.needs_setup(w, cat)
    emit({"work": w["id"], "needs": left})
    show_needs(w, left)
    if not [x for x in left if x["required"]] and not w.get("enabled") and any((cat.get(n["type"]) or {}).get("kind") == "trigger" and n["type"] != "start.manual" for n in w["nodes"]):
        out("it has a schedule / trigger: aihub work enable %s && aihub work serve --bg" % w["id"])
    return 0


# ---------- Python environments of the code nodes (nodeenv)

def env_nodes(w, cat, node=None):
    out_ = []
    for n in w["nodes"]:
        spec = cat.get(n["type"]) or {}
        if nodeenv.has_env(spec) and (not node or node in (n["id"], n.get("label"))):
            out_.append((n, spec))
    return out_


def env_install(w, cat, env=None, node=None):
    """Install what the env nodes of W miss: ONE pip run per Python. ENV = a managed env name to point those nodes at first. -> the saved work."""
    rows = env_nodes(w, cat, node)
    if node and not rows:
        raise Fail("no node %r with a Python environment in %s" % (node, w["title"]))
    if env:
        if not nodeenv.ENV_NAME.match(env) and not os.path.exists(os.path.expanduser(env)):
            raise Fail("--env: a name (letters, digits, - _) for a managed environment, or a path to a python / venv")
        for n, spec in rows:
            ef = nodeenv.env_field(spec)
            if ef:
                n.setdefault("config", {})[ef] = env
        w = store.save_work(w)
        out("nodes now run with the environment %s" % env)
    by, java = {}, None
    for n, spec in rows:
        c = nodeenv.check_node(n, spec, fresh=True)
        if c.get("java") and not c["java"]["ok"]:
            java = c["java"]
        if c.get("error") and not c["missing"]:
            raise Fail("%s: %s" % (label(n, cat), c["error"]))
        key = c["python"]
        g = by.setdefault(key, {"env": c["env"], "spark": n["type"] == "etl.spark", "pips": [], "nodes": []})
        g["pips"] += [p for p in c["missing"] if p not in g["pips"]]
        g["nodes"].append(label(n, cat))
    done_, failed = [], []
    for py, g in by.items():
        if not g["pips"]:
            out("  %s: nothing missing (%s)" % (", ".join(g["nodes"]), py))
            continue
        out("installing %s into %s  (for %s)" % (", ".join(g["pips"]), g["env"] or py, ", ".join(g["nodes"])))
        try:
            job = nodeenv.install(g["env"], g["pips"], spark=g["spark"])
        except ValueError as e:
            raise Fail(str(e))
        v = nodeenv.wait(job["id"], on_line=(None if JSON else (lambda ln: err("  | " + ln))))
        (done_ if v and v["state"] == "done" else failed).append({"python": py, "packages": g["pips"], "error": (v or {}).get("error", "")})
        if failed and failed[-1]["python"] == py:
            err("failed: %s\n  by hand: %s" % (failed[-1]["error"], job["command"]))
    if java:
        out("Spark also needs Java: %s" % java["hint"])
    emit({"work": w["id"], "installed": done_, "failed": failed, "java": java})
    if failed:
        raise Fail("some packages could not be installed")
    return w


def cmd_env(a):
    """aihub work env list | check WORK | install WORK  (or --type NODE_TYPE --set key=value for a node not in a work yet)."""
    act = a.action
    if act == "list":
        rows = nodeenv.envs()
        emit({"envs": rows, "dir": nodeenv.envs_dir()})
        for r in rows:
            out("  %-16s %s%s" % (r["name"], r["python"], "" if r["exists"] else "  (broken: no python)"))
        if not rows:
            out("no managed environments yet (aihub work env install WORK --env data makes one)")
        return 0
    if act not in ("check", "install"):
        raise Fail("env: list | check | install")
    cat = W.catalogue()
    if a.type:
        spec = cat.get(a.type)
        if not spec or not nodeenv.has_env(spec):
            raise Fail("%s is not a node with a Python environment (db.sql, etl.spark, code.python, or a package node with an env field)" % a.type)
        cfg = dict(kv.split("=", 1) for kv in (a.set or []) if "=" in kv)
        if a.env:
            cfg[nodeenv.env_field(spec) or "python"] = a.env
        w = {"id": "-", "title": a.type, "nodes": [{"id": "n", "type": a.type, "config": cfg}], "edges": []}
    else:
        if not a.work:
            raise Fail("name a work (or --type NODE_TYPE)")
        w = find_work(a.work)
    if act == "install":
        if a.type:
            n, spec = w["nodes"][0], cat[a.type]
            c = nodeenv.check_node(n, spec, fresh=True)
            if not c["missing"]:
                emit({"installed": [], "check": c})
                out("nothing missing in %s" % (c["env"] or c["python"]))
                return 0
            out("installing %s into %s" % (", ".join(c["missing"]), c["env"] or c["python"]))
            try:
                job = nodeenv.install(c["env"], c["missing"], spark=a.type == "etl.spark")
            except ValueError as e:
                raise Fail(str(e))
            v = nodeenv.wait(job["id"], on_line=(None if JSON else (lambda ln: err("  | " + ln))))
            emit({"job": v})
            if not v or v["state"] != "done":
                raise Fail("%s (by hand: %s)" % ((v or {}).get("error") or "install failed", job["command"]))
            return 0
        env_install(w, cat, env=a.env, node=a.node)
        return 0
    rows = []
    for n, spec in env_nodes(w, cat, a.node):
        c = nodeenv.check_node(n, spec, fresh=True)
        rows.append({"node": n["id"], "label": label(n, cat), "check": c})
        out("%s (%s): %s  Python %s" % (label(n, cat), n["type"], c["env"] or "UI's Python", c.get("version") or "?"))
        out("  " + c["python"])
        for p in c["packages"]:
            out("  %s %-18s %s%s" % ("ok" if p["installed"] else "--", p["module"], p.get("version") or "", "" if p["installed"] else "  missing (%s): %s" % (p["why"], p["pip"])))
        if c.get("java"):
            out("  %s java %s%s" % ("ok" if c["java"]["ok"] else "--", c["java"].get("version") or "", "" if c["java"]["ok"] else "  missing: " + c["java"]["hint"]))
        if c.get("error"):
            out("  " + c["error"])
        if c["missing"]:
            out("  fix: aihub work env install %s%s   or: %s" % (a.work or ("--type " + a.type), " --node " + n["id"] if a.node else "", c["command"]))
    emit({"nodes": rows})
    if not rows:
        out("no node with a Python environment")
    return 0 if all(r["check"]["ok"] for r in rows) else 1


def cmd_set(a):
    """Change node settings from the terminal: key=value pairs, the credential set, sets its code reads, the working folder."""
    w = find_work(a.work)
    n = find_node(w, a.node)
    cat = W.catalogue()
    spec = cat.get(n["type"]) or {"fields": []}
    fields = {f["name"]: f for f in spec["fields"]}
    cfg = n.setdefault("config", {})
    changed = []
    for kv in a.values or []:
        if "=" not in kv:
            raise Fail("settings are key=value (got %r)" % kv)
        k, v = kv.split("=", 1)
        f = fields.get(k)
        if spec["fields"] and not f and k not in ("cwd",):
            raise Fail("%s has no field %r (fields: %s)" % (label(n, cat), k, ", ".join(fields)))
        if f and f.get("secret"):
            raise Fail("%s is a secret: aihub work set %s %s --secret %s (asked at a hidden prompt) or use a credential set" % (k, w["id"], n["id"], k))
        if f and f["type"] == "number":
            try:
                v = float(v) if v.strip() else ""
                v = int(v) if v != "" and v == int(v) else v
            except ValueError:
                raise Fail("%s must be a number" % k)
        elif f and f["type"] == "checkbox":
            v = v.strip().lower() in ("1", "true", "yes", "on")
        if k == "cwd" and v:
            try:
                v = store.check_cwd(v)
            except ValueError as e:
                raise Fail("cwd: %s" % e)
        cfg[k] = v
        changed.append(k)
    if a.folder is not None:
        cfg["cwd"] = store.check_cwd(a.folder) if a.folder else ""
        n.pop("folder_hint", None) if a.folder else None
        changed.append("cwd")
    for k in a.secret or []:
        if k not in fields or not fields[k].get("secret"):
            raise Fail("%s is not a secret field of %s" % (k, label(n, cat)))
        cfg[k] = ask("%s (hidden)" % fields[k]["label"], secret=True) if tty() else sys.stdin.readline().rstrip("\n")
        changed.append(k)
    if a.credential is not None:
        if a.credential in ("", "none"):
            n.pop("credential", None)
        else:
            c = find_set(a.credential)
            want = (spec.get("credential") or {}).get("kind")
            if want and c["kind"] != want:
                raise Fail("%s takes a '%s' set, %s is '%s'" % (label(n, cat), want, c["name"], c["kind"]))
            n["credential"] = c["id"]
            n.pop("credential_kind", None)
            n.pop("credential_name", None)
        changed.append("credential")
    for ref in a.grant or []:
        c = find_set(ref)
        n["vault"] = [v for v in n.get("vault") or [] if v != c["id"]] + [c["id"]]
        n["vault_kinds"] = [v for v in n.get("vault_kinds") or [] if v.get("kind") != c["kind"]]
        if not n["vault_kinds"]:
            n.pop("vault_kinds")
        changed.append("vault")
    for ref in a.revoke or []:
        c = find_set(ref)
        n["vault"] = [v for v in n.get("vault") or [] if v != c["id"]]
        changed.append("vault")
    if not changed:
        raise Fail("nothing to change: aihub work set WORK NODE key=value ... | --folder PATH | --credential SET | --grant SET | --revoke SET | --secret FIELD")
    w = store.save_work(W.strip_secrets(w, box()))
    emit({"work": w["id"], "node": n["id"], "changed": sorted(set(changed))})
    out("%s / %s: changed %s" % (w["title"], label(n, cat), ", ".join(sorted(set(changed)))))
    return 0


def cmd_enable(a, on):
    w = find_work(a.work)
    if on:
        need = [x for x in workpkg.needs_setup(w) if x["required"]]
        if need and not a.force:
            show_needs(w, need)
            raise Fail("set it up first (or --force)")
    w["enabled"] = on
    store.save_work(w)
    emit({"work": w["id"], "enabled": on})
    sch = W.scheduler()
    out("%s is %s%s" % (w["title"], "on" if on else "off", "" if not on or sch else "; nothing fires triggers right now: aihub work serve --bg (or start the UI)"))
    return 0


def cmd_rm(a):
    w = find_work(a.work)
    if not a.yes and not yes("delete %s with its runs and logs?" % w["title"], False):
        return 1
    W.drop_work(w["id"])
    emit({"deleted": w["id"]})
    out("deleted %s" % w["title"])
    return 0


# ---------- run

def parse_inputs(pairs):
    vals = {}
    for kv in pairs or []:
        if "=" not in kv:
            raise Fail("--input name=value (got %r)" % kv)
        k, v = kv.split("=", 1)
        if v.startswith("@") and os.path.isfile(os.path.expanduser(v[1:])):
            v = open(os.path.expanduser(v[1:]), encoding="utf-8").read()        # name=@file.txt reads the value from a file
        vals[k.strip()] = v
    return vals


class Follow:
    """Prints a run as it goes: node status changes, and the node log lines (steps of code nodes, program output)."""

    def __init__(self, w, rid, logs=True):
        self.w, self.rid, self.logs = w, rid, logs
        self.seen, self.pos = {}, {}
        cat = W.catalogue()
        self.labels = {n["id"]: label(n, cat) for n in w["nodes"]}

    def tick(self):
        r = W.get_run(self.w["id"], self.rid) or {}
        for nid, rec in (r.get("nodes") or {}).items():
            st = rec.get("status")
            if self.logs and st in ("running", "ok", "error", "stopped"):
                lg = W.read_log(self.w["id"], self.rid, nid, self.pos.get(nid, 0))
                if lg and lg["text"]:
                    self.pos[nid] = lg["next"]
                    for ln in lg["text"].rstrip("\n").split("\n"):
                        body = ln.split(" ", 1)[1] if ln[:2].isdigit() and " " in ln else ln       # lines carry a HH:MM:SS stamp
                        if body.strip() and not body.startswith(('{"ok"', "node ", "finished ok", "exit 0")):
                            out("    %s" % ln[:400])
            if self.seen.get(nid) != st:
                self.seen[nid] = st
                dur = " %.1fs" % (rec["ended"] - rec["started"]) if rec.get("ended") and rec.get("started") else ""
                mark = {"running": "..", "ok": "ok", "error": "FAILED", "stopped": "stopped", "skipped": "skipped"}.get(st, st)
                out("  [%s] %s%s%s" % (mark, self.labels.get(nid, nid), dur, (": " + str(rec.get("error"))[:600]) if st == "error" else ""))
        return r


def cmd_run(a):
    w = find_work(a.work)
    need = [x for x in workpkg.needs_setup(w) if x["required"]]
    if need:
        show_needs(w, need)
        raise Fail("set it up first: aihub work setup %s" % w["id"])
    try:
        node, ev = W.manual_event(w, a.node, a.text, parse_inputs(a.input))
    except ValueError as e:
        raise Fail(str(e))
    eng = W.Engine(box(), schedule=False, who="cli")
    rid = eng.fire(w, node, ev, "cli")
    out("run %s of %s" % (rid, w["title"]))
    f = Follow(w, rid, logs=not a.quiet)
    stop = {"n": 0}

    def on_int(*_):
        stop["n"] += 1
        if stop["n"] > 1:
            os._exit(130)
        err("stopping the run (Ctrl-C again to quit at once)")
        W.stop_run(rid)
    signal.signal(signal.SIGINT, on_int)
    deadline = time.time() + a.timeout if a.timeout else None
    r = {}
    while True:
        r = f.tick()
        if r.get("status") in DONE:
            break
        if deadline and time.time() > deadline:
            err("timed out after %ds: stopping" % a.timeout)
            W.stop_run(rid)
            deadline = None
        time.sleep(0.4)
    time.sleep(0.2)
    r = f.tick()
    res = r.get("result") or {}
    emit({"run": rid, "status": r.get("status"), "error": r.get("error") or "", "result": res,
          "nodes": {k: {x: v.get(x) for x in ("status", "error", "started", "ended", "label")} for k, v in (r.get("nodes") or {}).items()},
          "dir": r.get("dir") or ""})
    if res.get("text") and not JSON:
        out("\n" + (("# " + res["title"] + "\n") if res.get("title") else "") + res["text"])
    out("\nrun %s %s%s (files: %s)" % (rid, r.get("status"), ": " + r["error"] if r.get("error") else "", r.get("dir") or W.run_dir(w["id"], rid, make=False)))
    eng.shutdown()
    return 0 if r.get("status") == "done" else 1


def cmd_runs(a):
    w = find_work(a.work)
    rows = W.list_runs(w["id"])[:a.n]
    emit({"runs": rows})
    for r in rows:
        dur = "%5.1fs" % (r["ended"] - r["started"]) if r.get("ended") and r.get("started") else "     "
        out("%s  %-11s %s %-8s %s" % (r["id"], r["status"], dur, r.get("via") or "", (r.get("error") or (r.get("result") or {}).get("text") or r.get("text") or "").replace("\n", " ")[:80]))
    return 0


def cmd_logs(a):
    w = find_work(a.work)
    rid = a.run or next(iter(W.list_runs(w["id"])), {}).get("id")
    if not rid:
        raise Fail("no runs yet")
    r = W.get_run(w["id"], rid)
    if not r:
        raise Fail("no run %s" % rid)
    order, _ = W.topo(w)
    nids = [a.node_id] if a.node_id else [n for n in order if n in (r.get("nodes") or {})]
    docs = {}
    for nid in nids:
        lg = W.read_log(w["id"], rid, nid, 0, 2_000_000) or {"text": ""}
        docs[nid] = lg["text"]
        out("== %s (%s)" % (nid, (r["nodes"].get(nid) or {}).get("status")))
        out(lg["text"].rstrip() or "(no log)")
    emit({"run": rid, "status": r.get("status"), "logs": docs})
    return 0


# ---------- serve: the scheduler without the UI

def _hooks_server(eng, port):
    """POST /hook/<work>/<node>/<token> on 127.0.0.1:PORT, the same webhook URLs (and tokens) as the UI, for a machine without it."""
    import hmac
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, doc):
            body = json.dumps(doc).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            parts = self.path.split("?")[0].split("/")
            if host not in ("127.0.0.1", "localhost") or len(parts) != 5 or parts[1] != "hook" or not store.ID.match(parts[2]) or not hmac.compare_digest(parts[4], W.hook_token(parts[2], parts[3])):
                return self._send(403, {"error": "forbidden"})
            w = store.get_work(parts[2])
            node = next((n for n in (w or {}).get("nodes", []) if n["id"] == parts[3] and n.get("type") == "start.webhook"), None)
            if not w or not node or not w.get("enabled"):
                return self._send(404, {"error": "no such enabled webhook"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > 256 * 1024:
                return self._send(413, {"error": "too large"})
            raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
            try:
                d = json.loads(raw)
                ev = {"text": str(d.get("text", "")), "data": d.get("data") if isinstance(d.get("data"), dict) else {}} if isinstance(d, dict) else {"text": raw}
            except ValueError:
                ev = {"text": raw}
            rid = eng.fire(w, node, ev, "webhook")
            self._send(200, {"run": rid, "started": rid is not None})

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="hooks").start()
    return srv


def cmd_serve(a):
    """Fire cron / package triggers (and, with --hooks, webhooks) of the enabled works until stopped. `aihub work serve --bg` supervises this."""
    eng = W.Engine(box(), schedule=True, who="serve")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except (ValueError, OSError):
            pass
    hooks = _hooks_server(eng, a.hooks) if a.hooks else None
    eng.kick()
    on = [r for r in store.works() if r["enabled"]]
    out("aihub works: %d enabled work(s)%s%s" % (len(on), ": " + ", ".join(r["title"] for r in on) if on else " (aihub work enable WORK)",
                                                 "; webhooks on http://127.0.0.1:%d/hook/..." % hooks.server_address[1] if hooks else ""))
    seen, told = {}, None
    while not stop.wait(3):
        sch = eng.scheduler()
        who = "mine" if sch and sch["mine"] else ("%s (pid %s)" % (sch["who"], sch["pid"]) if sch else "none")
        if who != told:
            told = who
            out(time.strftime("%H:%M:%S ") + ("this process fires the triggers" if who == "mine" else "waiting: triggers are fired by %s; this one takes over when it stops" % who))
        for r in store.works():
            for run in W.list_runs(r["id"])[:5]:
                k = run["id"]
                if seen.get(k) != run["status"] and (k in seen or run["status"] in ("queued", "running") or (run.get("started") or 0) > time.time() - 10):
                    seen[k] = run["status"]
                    out("%s %s %-9s %s%s" % (time.strftime("%H:%M:%S"), k, run["status"], r["title"], (": " + run["error"][:200]) if run.get("error") else ""))
    out("stopping")
    if hooks:
        hooks.shutdown()
    eng.shutdown()
    return 0


def cmd_lib(a):
    emit({"lib": W.LIB_DIR})
    if not JSON:
        print(W.LIB_DIR)
        err("use it from your own scripts: PYTHONPATH=%s python my_etl.py   (import aihub_vault)" % W.LIB_DIR)
    return 0


# ---------- argv

def parser():
    p = argparse.ArgumentParser(prog="aihub work", description="Works (workflows of aihub-ui) from the terminal: pull, set up, run, serve in the background.")
    p.add_argument("--json", action="store_true", help="one JSON document on stdout")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("list", help="works on this machine")
    s = sub.add_parser("show", help="nodes, inputs and what a work still needs")
    s.add_argument("work")
    s = sub.add_parser("pull", help="install a workflow package and add its works (off)")
    s.add_argument("package")
    s.add_argument("--file", help="only this work of the package (file or title)")
    s.add_argument("--again", action="store_true", help="add a fresh copy even when it is already in Works")
    s.add_argument("-i", dest="indirect", action="store_true", help="download through the hub (aihub install -i)")
    s = sub.add_parser("import", help="add a .work.json (or every one in a folder)")
    s.add_argument("path")
    s.add_argument("--package", default="")
    s = sub.add_parser("export", help="write a work as a shareable .work.json + its files (never secrets or credential sets)")
    s.add_argument("work")
    s.add_argument("--out", "-o")
    s.add_argument("--no-notes", action="store_true", help="do not snapshot @knowledge notes into the files")
    s = sub.add_parser("setup", help="credential sets, working folders and secret fields a work needs")
    s.add_argument("work")
    s.add_argument("--creds", help="JSON file (or -) with the sets: {\"s3\": {\"buckets\": [...]}, \"trino\": {...}}")
    s.add_argument("--folder", action="append", help="NODE=PATH working folder of a node (repeat)")
    s.add_argument("--env", help="run its Python / SQL / Spark nodes in this environment (a name = ~/.aihub/ui/envs/NAME, made when missing) and install what they need")
    s.add_argument("--install", action="store_true", help="install the Python packages the nodes miss without asking")
    s = sub.add_parser("set", help="change a node: key=value, --folder, --credential, --grant/--revoke, --secret")
    s.add_argument("work")
    s.add_argument("node")
    s.add_argument("values", nargs="*")
    s.add_argument("--folder")
    s.add_argument("--credential")
    s.add_argument("--grant", action="append", help="credential set the node's code may read with aihub_vault (repeat)")
    s.add_argument("--revoke", action="append")
    s.add_argument("--secret", action="append", help="secret field, asked at a hidden prompt (or one line of stdin)")
    for name in ("enable", "disable"):
        s = sub.add_parser(name, help="switch a work's triggers %s" % ("on" if name == "enable" else "off"))
        s.add_argument("work")
        s.add_argument("--force", action="store_true")
    s = sub.add_parser("rm", help="delete a work with its runs")
    s.add_argument("work")
    s.add_argument("--yes", "-y", action="store_true")
    s = sub.add_parser("run", help="run a work once in this terminal")
    s.add_argument("work")
    s.add_argument("--input", "-I", action="append", help="name=value of a Start input (name=@file reads a file; repeat)")
    s.add_argument("--text", default="")
    s.add_argument("--node", help="the trigger node to start from (default: the manual Start)")
    s.add_argument("--quiet", "-q", action="store_true", help="statuses only, no node log lines")
    s.add_argument("--timeout", type=int, default=0)
    s = sub.add_parser("runs", help="recent runs of a work")
    s.add_argument("work")
    s.add_argument("-n", type=int, default=15)
    s = sub.add_parser("logs", help="node logs of a run (default: the last one)")
    s.add_argument("work")
    s.add_argument("run", nargs="?")
    s.add_argument("--node", dest="node_id")
    s = sub.add_parser("serve", help="fire the triggers of the enabled works until stopped")
    s.add_argument("--hooks", type=int, default=0, help="also take webhooks on 127.0.0.1:PORT")
    s = sub.add_parser("cred", help="credential sets: list | kinds | template KIND | show | add | set | rm")
    s.add_argument("action")
    s.add_argument("ref", nargs="?")
    s.add_argument("--kind")
    s.add_argument("--name")
    s.add_argument("--file", help="JSON file with the fields, or - for stdin")
    s.add_argument("--yes", "-y", action="store_true")
    sub.add_parser("lib", help="the folder holding aihub_vault (PYTHONPATH for your own scripts)")
    s = sub.add_parser("env", help="Python environments of the code nodes: list | check WORK | install WORK (pip packages, drivers)")
    s.add_argument("action")
    s.add_argument("work", nargs="?")
    s.add_argument("--node", help="only this node (id or label)")
    s.add_argument("--env", help="point the nodes at this environment first (a name = ~/.aihub/ui/envs/NAME, created when missing; or a path)")
    s.add_argument("--type", help="a node type instead of a work (db.sql, etl.spark, code.python): set up an environment before building the work")
    s.add_argument("--set", action="append", help="key=value settings for --type (e.g. driver=oracle, frame=polars)")
    return p


def main(argv):
    global JSON
    p = parser()
    argv = list(argv)
    flag = "--json" in argv                                          # accepted anywhere (aihub work list --json)
    a = p.parse_args([x for x in argv if x != "--json"])
    JSON = flag or a.json or os.environ.get("AIHUB_JSON") == "1"
    if not a.cmd:
        p.print_help()
        return 0
    fn = {"list": cmd_list, "show": cmd_show, "pull": cmd_pull, "import": cmd_import, "export": cmd_export, "setup": cmd_setup, "set": cmd_set,
          "enable": lambda a: cmd_enable(a, True), "disable": lambda a: cmd_enable(a, False), "rm": cmd_rm, "run": cmd_run, "runs": cmd_runs,
          "logs": cmd_logs, "serve": cmd_serve, "cred": cmd_cred, "lib": cmd_lib, "env": cmd_env}[a.cmd]
    try:
        return fn(a)
    except Fail as e:
        if JSON:
            emit({"error": str(e)})
        err("error: %s" % e)
        return 1
    except A.CliError as e:
        if JSON:
            emit({"error": str(e)})
        err("error: %s" % e)
        return 1
    except (KeyboardInterrupt, EOFError):
        err("cancelled")
        return 130
