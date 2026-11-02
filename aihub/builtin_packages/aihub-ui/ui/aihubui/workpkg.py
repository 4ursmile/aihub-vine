"""Works as packages: what a shared workflow carries, what it needs on the machine that installs it. Shared by the UI server (Share /
Add to Works) and the headless runner (`aihub work pull | import | setup`).

A shared work carries: its nodes and edges, the non-secret settings, and the FILES its prompts and fields mention (@file:, @folder:,
@knowledge: snapshots, mail attachments), copied next to the work file and rewritten to @file:{{work.files}}/<name>.
It never carries: secret field values, credential sets (only their kind and name: `credential_kind`, `vault_kinds`), saved nodes
(their non-secret settings are written in), or machine paths such as a Claude node's working folder (`folder_hint` keeps its name so
the installer is asked for one). A node with a Python environment (Python, SQL, Spark) carries `requires` (the pip packages its settings
and code need) and keeps a managed env NAME (created on the other machine by `aihub work env install` / the node's Install button); a
path to a python of this machine is dropped (`env_hint` keeps its name)."""
import json
import os
import re
import shutil

from . import aihub as A, nodeenv, sources, store, works as W

RISKS = {"io.read_file": "reads files on this computer", "io.request": "makes web requests", "code.python": "runs Python code written in the workflow on the installer's machine", "git.read": "reads git repositories with your Vault logins",
         "start.webhook": "listens for local webhook calls", "start.teams": "listens for messages from outside (tunnel + secret)", "send.teams": "posts to Teams", "send.slack": "posts to Slack", "start.slack": "listens for Slack calls (tunnel + signing secret)", "start.telegram": "reads messages sent to your Telegram bot", "start.pubsub": "listens to hub Pub/Sub messages", "pubsub.publish": "publishes to hub Pub/Sub", "send.telegram": "posts to Telegram", "send.discord": "posts to Discord", "send.webhook": "sends data to an outside URL",
         "send.smtp": "sends mail (or saves drafts) through a mail server", "db.trino": "runs SQL on a Trino cluster with your login", "etl.spark": "runs Spark code written in the workflow (local or on your cluster)",
         "db.sql": "runs SQL on a database with your login (and may write tables)"}
FILE_MAX, FOLDER_FILES, FOLDER_BYTES = 5_000_000, 200, 20_000_000
MENTION = re.compile(r"@(file|folder):(\S+)")
NOTE = re.compile(r"@knowledge:((?:[A-Za-z0-9][\w.-]{0,63}/)?[a-z0-9][a-z0-9._-]{0,63})")
FILES_TAG = "{{work.files}}"
HOME_ROOT = None                                              # files outside it are never bundled (tests point it elsewhere)


def slug(t):
    return re.sub(r"[^a-z0-9]+", "-", (t or "work").lower()).strip("-")[:40] or "work"


def risks(w):
    out = []
    for n in w.get("nodes", []):
        t = n.get("type", "")
        r = RISKS.get(t) or ("runs code from the node package " + t[4:].split("/")[0] if t.startswith("pkg:") else "")
        if t == "ai.claude" and (n.get("config") or {}).get("mode") == "bypass":
            r = "runs Claude with permission checks bypassed"
        if t == "ai.claude" and not r:
            r = "runs Claude Code (can use your skills, tools and MCP servers)"
        if r and r not in out:
            out.append(r)
        for k in ([n["credential_kind"]] if n.get("credential_kind") and not n.get("credential") else []) + [v.get("kind") for v in n.get("vault_kinds") or ()]:
            r2 = "needs a '%s' credential set: you pick or create one after installing (no logins are shared)" % k
            if r2 not in out:
                out.append(r2)
    return out


# ---------- export: what leaves the machine

def strip_for_share(w, cat=None):
    """A copy of work W fit to share: no id, disabled, no secrets, credential links replaced by their kind, saved nodes written in, machine folders dropped."""
    cat = cat or W.catalogue()
    out = json.loads(json.dumps(w))
    out.pop("id", None)
    out.update(enabled=False)
    for n in out["nodes"]:
        c = W.credential(n.pop("credential", None))
        if c:                                                    # only the KIND travels: whoever installs it picks or creates their own set
            n["credential_kind"], n["credential_name"] = c["kind"], c["name"]
        kinds = list(n.pop("vault_kinds", None) or [])
        for cid in n.pop("vault", None) or ():
            v = W.credential(cid)
            if v:
                kinds.append({"kind": v["kind"], "name": v["name"]})
        if kinds:
            n["vault_kinds"] = kinds
        p = W.preset(n.pop("preset", None))
        if p and p["type"] == n["type"]:                         # saved nodes exist only here: the package gets their settings written in
            n["config"] = dict({k: v for k, v in p["config"].items() if v != W.MARK}, **{k: v for k, v in n["config"].items() if v not in (None, "")})
        for f in (cat.get(n["type"]) or {}).get("fields", []):
            if f.get("secret"):
                n["config"][f["name"]] = ""                      # never leaves the machine; the person who installs it types their own
        cfg = n.get("config") or {}
        cwd = str(cfg.get("cwd") or "").strip()
        if cwd and FILES_TAG not in cwd and "{{" not in cwd:     # a folder of this machine: the installer is asked for one (aihub work setup / the node's settings)
            n["folder_hint"] = os.path.basename(cwd.rstrip("/\\").lstrip("@")) or cwd[:60]
            cfg["cwd"] = ""
        spec = cat.get(n["type"]) or {}
        if nodeenv.has_env(spec):
            ef = nodeenv.env_field(spec)
            env = str(cfg.get(ef) or "").strip() if ef else ""
            if env and not nodeenv.ENV_NAME.match(env):                 # a python of this machine: the installer picks their own
                n["env_hint"] = os.path.basename(env.rstrip("/\\"))[:60] or env[:60]
                cfg[ef] = ""
            req = nodeenv.shared_requires(n, spec)
            if req:
                n["requires"] = req
    return out


def _safe_name(name, used):
    base = re.sub(r"[^\w.-]+", "_", name).strip("._") or "file"
    out, i = base, 1
    while out.lower() in used:
        i += 1
        stem, ext = os.path.splitext(base)
        out = "%s-%d%s" % (stem, i, ext)
    used.add(out.lower())
    return out


def _copy_folder(src, dest):
    """Copy a mentioned folder (no dot folders, node_modules, blocked files; caps FOLDER_FILES / FOLDER_BYTES). -> (files, skipped)."""
    n = size = skipped = 0
    for cur, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in sources.SKIP_DIRS)
        for f in sorted(files):
            p = os.path.join(cur, f)
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            if f.startswith(".") or sources._blocked(p) or n >= FOLDER_FILES or size + sz > FOLDER_BYTES:
                skipped += 1
                continue
            t = os.path.join(dest, os.path.relpath(p, src))
            os.makedirs(os.path.dirname(t), exist_ok=True)
            shutil.copy2(p, t)
            n, size = n + 1, size + sz
    return n, skipped


def bundle(src, out, dest, notes=True):
    """Copy what the prompts / fields of work SRC mention into DEST and rewrite the mentions in OUT (the stripped copy) to {{work.files}}/<name>.
    Credentials are never bundled (blocked paths are skipped, as the mention readers skip them). -> report {files, skipped}."""
    os.makedirs(dest, exist_ok=True)
    used, done, report, kinds = set(os.listdir(dest)), {}, {"files": [], "skipped": []}, {}
    home = os.path.realpath(HOME_ROOT or os.path.expanduser("~"))
    srcs = {n["id"]: n for n in src.get("nodes", [])}
    own = W.work_files_dir(src["id"]) if src.get("id") else ""

    def take(kind, raw, base):
        raw = raw.rstrip(".,;:)")
        if raw.startswith(FILES_TAG):                              # already bundled (a work installed from a package): bring it along again
            p = os.path.join(own, raw[len(FILES_TAG):].lstrip("/\\"))
            kind = "folder" if os.path.isdir(p) else "file"      # older shares wrote @file: for folders too
        else:
            p = raw if os.path.isabs(os.path.expanduser(raw)) else os.path.join(base or "", raw) if base else ""
            p = os.path.expanduser(p)
        if not p:
            report["skipped"].append("%s (relative to a chat folder)" % raw)
            return None
        p = os.path.realpath(p)
        if p in done:
            return done[p]
        why = sources._blocked(p) if os.path.exists(p) else "not found"
        if not why and not (p == home or p.startswith(home + os.sep)) and not (own and p.startswith(os.path.realpath(own))):
            why = "outside the home folder"
        if not why and kind == "file" and os.path.isfile(p) and os.path.getsize(p) > FILE_MAX:
            why = "larger than %d MB" % (FILE_MAX // 1_000_000)
        if not why and ((kind == "file" and not os.path.isfile(p)) or (kind == "folder" and not os.path.isdir(p))):
            why = "not a %s" % kind
        if why:
            report["skipped"].append("%s (%s)" % (raw, why))
            done[p] = None
            return None
        name = _safe_name(os.path.basename(p.rstrip(os.sep)), used)
        if kind == "file":
            shutil.copy2(p, os.path.join(dest, name))
        else:
            _, sk = _copy_folder(p, os.path.join(dest, name))
            if sk:
                report["skipped"].append("%s: %d file(s) left out (hidden, private or over the size cap)" % (raw, sk))
        done[p] = "%s/%s" % (FILES_TAG, name)
        kinds[done[p]] = kind
        report["files"].append(name)
        return done[p]

    def note(ref):
        key = "note:" + ref
        if key in done:
            return done[key]
        body = sources._note(ref)
        if body is None:
            report["skipped"].append("@knowledge:%s (not readable)" % ref)
            done[key] = None
            return None
        name = _safe_name("knowledge-%s.md" % ref.replace("/", "-"), used)
        with open(os.path.join(dest, name), "w", encoding="utf-8") as f:
            f.write(body)
        done[key] = "%s/%s" % (FILES_TAG, name)
        report["files"].append(name)
        return done[key]

    for n in out.get("nodes", []):
        cfg = n.get("config") or {}
        base = str(((srcs.get(n["id"]) or {}).get("config") or {}).get("cwd") or "").lstrip("@").strip()
        for k, v in list(cfg.items()):
            if not isinstance(v, str) or not v:
                continue
            v2 = MENTION.sub(lambda m: ("@%s:%s" % (kinds.get(got, m.group(1)), got)) if (got := take(m.group(1), m.group(2), base)) else m.group(0), v)
            if k == "path" and n.get("type") == "io.path":           # File / folder node: a line picked from the @ list (@Documents/dash/) names a path in home
                def picked(ln):
                    m = re.match(r"^(\s*)@(?!file:|folder:)(\S+)\s*$", ln)
                    if not m or "{{" in m.group(2):
                        return ln
                    raw = os.path.expanduser(m.group(2).rstrip("/\\"))
                    p = raw if os.path.isabs(raw) else os.path.join(home, raw)
                    got = take("folder" if os.path.isdir(p) else "file", p, "")
                    return "%s@%s:%s" % (m.group(1), kinds.get(got), got) if got else ln
                v2 = "\n".join(picked(ln) for ln in v2.split("\n"))
            if notes:
                v2 = NOTE.sub(lambda m: ("@file:" + got) if (got := note(m.group(1))) else m.group(0), v2)
            if k == "attachments":                                  # mail attachments: absolute paths, one per line
                v2 = "\n".join((take("file", ln.strip(), base) or ln) if ln.strip() and os.path.isabs(os.path.expanduser(ln.strip())) else ln for ln in v2.split("\n"))
            cfg[k] = v2
    if not os.listdir(dest):
        os.rmdir(dest)
    return report


def files_dir_of(work_file):
    """Where a shared work keeps its bundled files: next to it, `<name>.files/` for `<name>.work.json`."""
    return work_file[:-len(".work.json")] + ".files" if work_file.endswith(".work.json") else work_file + ".files"


# ---------- import: what the machine that installs it must add

def bind_credentials(w):
    """A shared workflow names credential kinds, never values: link a node to the set of that kind when exactly one fits (same name first).
    Covers the node's own set (`credential_kind`) and the sets its code reads (`vault_kinds`)."""
    have = W.credentials()

    def pick(kind, name):
        same = [c for c in have.values() if c["kind"] == kind]
        return next((c for c in same if c["name"] == name), same[0] if len(same) == 1 else None)
    for n in w.get("nodes", []):
        kind = n.get("credential_kind")
        if kind and not n.get("credential"):
            c = pick(kind, n.get("credential_name"))
            if c:
                n["credential"] = c["id"]
        left = []
        for v in n.get("vault_kinds") or ():
            c = pick(v.get("kind"), v.get("name"))
            if c:
                n.setdefault("vault", [])
                if c["id"] not in n["vault"]:
                    n["vault"].append(c["id"])
            else:
                left.append(v)
        if "vault_kinds" in n:
            if left:
                n["vault_kinds"] = left
            else:
                n.pop("vault_kinds")
    return w


def needs_credentials(w):
    """[{kind, title, name, nodes}] of the credential sets this work still lacks (what an import should ask the person to pick or create)."""
    ks, out = W.kinds(), {}
    for n in w.get("nodes", []):
        want = ([(n["credential_kind"], n.get("credential_name") or "")] if n.get("credential_kind") and not n.get("credential") else []) + \
               [(v.get("kind"), v.get("name") or "") for v in n.get("vault_kinds") or ()]
        for k, name in want:
            r = out.setdefault(k, {"kind": k, "title": (ks.get(k) or {}).get("title", k), "name": name, "nodes": []})
            r["nodes"].append(n.get("label") or n.get("type"))
    return list(out.values())


def needs_setup(w, cat=None, env=True):
    """Everything work W still needs on this machine, one row per thing to do:
    {node, label, what: credential | vault | folder | secret | env | system, kind?, name?, field?, packages?, command?, required, hint}.
    ENV=False skips the environment check (it runs a short probe in each node's Python)."""
    cat = cat or W.catalogue()
    rows = []
    for n in w.get("nodes", []):
        spec = cat.get(n.get("type")) or {}
        lab = n.get("label") or spec.get("title") or n.get("type")
        if n.get("credential_kind") and not n.get("credential"):
            rows.append({"node": n["id"], "label": lab, "what": "credential", "kind": n["credential_kind"], "name": n.get("credential_name") or "", "required": True,
                         "hint": "the node's login set"})
        elif n.get("credential") and not W.credential(n["credential"]):
            rows.append({"node": n["id"], "label": lab, "what": "credential", "kind": (spec.get("credential") or {}).get("kind", ""), "name": "", "required": True,
                         "hint": "its set '%s' was deleted" % n["credential"]})
        for v in n.get("vault_kinds") or ():
            rows.append({"node": n["id"], "label": lab, "what": "vault", "kind": v.get("kind"), "name": v.get("name") or "", "required": True, "hint": "a set the node's code reads with aihub_vault"})
        if n.get("folder_hint") and not str((n.get("config") or {}).get("cwd") or "").strip():
            rows.append({"node": n["id"], "label": lab, "what": "folder", "field": "cwd", "name": n["folder_hint"], "required": False,
                         "hint": "the working folder it used was '%s' (empty = a fresh folder per run)" % n["folder_hint"]})
        cmap = W.cred_fields(spec) if spec.get("credential") and (n.get("credential") or n.get("credential_kind")) else []
        for f in spec.get("fields", []):
            if f.get("secret") and not (n.get("config") or {}).get(f["name"]) and f["name"] not in cmap and not n.get("preset"):
                rows.append({"node": n["id"], "label": lab, "what": "secret", "field": f["name"], "name": f["label"], "required": bool(f.get("required")), "hint": f.get("help") or ""})
        if env and nodeenv.has_env(spec):
            rows += env_rows(n, spec, lab)
    return rows


def env_rows(n, spec, lab):
    """Rows of needs_setup for a node's Python environment: packages to install, Java for Spark."""
    try:
        c = nodeenv.check_node(n, spec)
    except Exception as e:
        return [{"node": n["id"], "label": lab, "what": "env", "name": "", "required": True, "hint": "cannot check its Python: %s" % e}]
    rows = []
    where = c["name"] or ("the UI's Python" if not c["env"] else c["python"])
    if c.get("error") and not c["missing"]:
        rows.append({"node": n["id"], "label": lab, "what": "env", "env": c["env"], "name": where, "packages": [], "required": True, "hint": c["error"]})
    elif c["missing"]:
        core = any(p["pip"] in c["missing"] and p["why"] != "imported by the code" for p in c["packages"])   # an import may be a local file: not a blocker
        rows.append({"node": n["id"], "label": lab, "what": "env", "env": c["env"], "field": c["field"], "name": where, "packages": c["missing"], "command": c.get("command", ""),
                     "required": core, "hint": "install %s into %s%s" % (", ".join(c["missing"]), where, " (it is created first)" if c["name"] and not c["exists"] else "")})
    j = c.get("java")
    if j and not j["ok"]:
        rows.append({"node": n["id"], "label": lab, "what": "system", "name": "Java", "required": True, "command": j["hint"], "hint": "Spark needs a Java runtime (17 works with every pyspark): %s" % j["hint"]})
    return rows


def install_files(src_dir, wid):
    """Copy the files a shared work brought along into its own folder (work_files_dir). -> number of files."""
    if not src_dir or not os.path.isdir(src_dir):
        return 0
    dest = W.work_files_dir(wid, make=True)
    n = 0
    for cur, dirs, files in os.walk(src_dir):
        for f in files:
            t = os.path.join(dest, os.path.relpath(os.path.join(cur, f), src_dir))
            os.makedirs(os.path.dirname(t), exist_ok=True)
            shutil.copy2(os.path.join(cur, f), t)
            n += 1
    return n


def import_file(path, package=""):
    """Add the shared work at PATH (a .work.json) to Works, disabled, with its files and the credential sets that fit. -> {work, risks, needs, files}."""
    with open(path, encoding="utf-8") as fh:
        w = json.load(fh)
    if not isinstance(w, dict) or not isinstance(w.get("nodes"), list):
        raise ValueError("%s is not a work file" % path)
    w.pop("id", None)
    w["enabled"] = False
    w["package"] = package
    w = store.save_work(bind_credentials(store.clean_work(w)))
    nf = install_files(files_dir_of(path), w["id"])
    return {"work": w, "risks": risks(w), "needs_credentials": needs_credentials(w), "needs": needs_setup(w), "files": nf}


def workflows():
    """Works shipped by installed packages of type workflow: [{package, version, file, title, description, nodes, risks, _path}]."""
    out = []
    for p in A.run(["list"]).get("data") or []:
        d = os.path.join(p.get("path") or "", "works")
        if p.get("type") not in (None, "", "workflow") or not os.path.isdir(d):        # installed records carry no type: the works/ folder decides
            continue
        for f in sorted(os.listdir(d)):
            if f.endswith(".work.json"):
                try:
                    with open(os.path.join(d, f), encoding="utf-8") as fh:
                        w = json.load(fh)
                    out.append({"package": p["name"], "version": p.get("version"), "file": f, "title": str(w.get("title") or f)[:80], "description": str(w.get("description") or p.get("description") or "")[:300],
                                "nodes": len(w.get("nodes", [])), "risks": risks(w), "_path": os.path.join(d, f)})
                except (OSError, ValueError):
                    pass
    return out
