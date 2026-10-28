"""Files the UI owns under ~/.aihub/ui and the per-chat working folders under ~/.aihub/workingspace/sessions/<id>."""
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid

from .aihub import HOME
from .secretbox import write_private

_lock = threading.RLock()
ID = re.compile(r"^[0-9a-f]{8}$")
MAX_HISTORY = 4000                 # events kept per chat file read back to the page


def ui_dir(*a):
    d = os.path.join(HOME(), "ui", *a)
    os.makedirs(os.path.dirname(d) if a and "." in os.path.basename(d) else d, exist_ok=True)
    return d


def work_dir(sid=None):
    base = os.path.join(HOME(), "workingspace", "sessions")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, sid) if sid else base


def session_dir(sid):
    """The folder a chat works in: the one the user chose (`cwd`), else the private folder the UI made for it."""
    c = (get_session(sid) or {}).get("cwd")
    return c if c else work_dir(sid)


def check_cwd(path):
    """Absolute, existing (made when missing) folder for a chat to work in. Raises ValueError with a message for the page."""
    raw = str(path or "").strip()
    if not raw:
        raise ValueError("give a folder path")
    full = os.path.abspath(os.path.expanduser(raw))
    if os.path.dirname(full) == full:
        raise ValueError("pick a folder, not the root of the disk")
    if os.path.exists(full) and not os.path.isdir(full):
        raise ValueError("that path is a file: %s" % full)
    try:
        os.makedirs(full, exist_ok=True)
    except OSError as e:
        raise ValueError("cannot create %s: %s" % (full, e.strerror or e))
    if not os.access(full, os.R_OK | os.W_OK | os.X_OK):
        raise ValueError("no permission to use %s" % full)
    return full


def link_folder(cwd):
    """Name of the sidebar folder for a working folder (made when new, remembered in `folder_dirs`): its base name, numbered when another
    folder of that name already stands for a different path."""
    base = (os.path.basename(cwd.rstrip("/\\")) or cwd)[:50] or "folder"
    with _lock:
        d = _index()
        dirs = d.setdefault("folder_dirs", {})
        name, n = base, 1
        while name in dirs and dirs[name] != cwd:
            n += 1
            name = "%s (%d)" % (base, n)
        dirs[name] = cwd
        if name not in d["folders"]:
            d["folders"] = sorted(set(d["folders"]) | {name})
        _save_index(d)
    return name


def folder_dir(name):
    return (_index().get("folder_dirs") or {}).get(name or "", "")


def _read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_private(path, json.dumps(data, indent=1).encode("utf-8"))


# ---- chats

def _index():
    return _read(ui_dir("sessions.json"), {"sessions": {}, "folders": []})


def _save_index(d):
    _write(os.path.join(ui_dir(), "sessions.json"), d)


def sessions():
    with _lock:
        d = _index()
        return {"folders": d["folders"], "folder_dirs": d.get("folder_dirs") or {}, "sessions": sorted(d["sessions"].values(), key=lambda s: -s.get("updated", 0))}


def get_session(sid):
    if not ID.match(str(sid)):
        return None
    with _lock:
        return _index()["sessions"].get(sid)


def create_session(title="", folder="", system="", cwd="", extra=""):
    sid = uuid.uuid4().hex[:8]
    if not cwd:
        os.makedirs(work_dir(sid), exist_ok=True)
    now = time.time()
    s = {"id": sid, "title": title or "New chat", "folder": folder, "created": now, "updated": now, "claude": str(uuid.uuid4()),
         "started": False, "system": system, "extra": extra, "cwd": cwd, "soul": None, "engine": "claude", "mcp": [], "model": "", "effort": "", "mode": "auto", "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0, "turns": 0}}
    with _lock:
        d = _index()
        d["sessions"][sid] = s
        _save_index(d)
    return s


def update_session(sid, **kw):
    with _lock:
        d = _index()
        s = d["sessions"].get(sid)
        if not s:
            return None
        s.update(kw)
        _save_index(d)
        return s


def add_usage(sid, u):
    with _lock:
        d = _index()
        s = d["sessions"].get(sid)
        if not s:
            return
        t = s["usage"]
        for k in ("input", "output", "cache_read", "cache_write"):
            t[k] = t.get(k, 0) + int(u.get(k) or 0)
        t["cost"] = round(t.get("cost", 0.0) + float(u.get("cost") or 0), 6)
        t["turns"] = t.get("turns", 0) + 1
        s["updated"] = time.time()
        _save_index(d)
        engine, model = s.get("engine") or "claude", s.get("model") or prefs().get("free_model") or ""
    try:
        from . import hubusage
        hubusage.note(sid, engine, model, u)                      # every turn reaches the hub; never fails the turn
    except Exception:
        pass


def delete_session(sid):
    if not ID.match(str(sid)):
        return
    with _lock:
        d = _index()
        d["sessions"].pop(sid, None)
        _save_index(d)
    for p in (os.path.join(ui_dir("history"), sid + ".jsonl"),):
        try:
            os.remove(p)
        except OSError:
            pass
    shutil.rmtree(work_dir(sid), ignore_errors=True)                 # never the folder the user chose (cwd)
    shutil.rmtree(os.path.join(ui_dir("snap"), sid), ignore_errors=True)


def set_folders(names):
    with _lock:
        d = _index()
        d["folders"] = sorted({str(n).strip()[:60] for n in names if str(n).strip()})
        d["folder_dirs"] = {k: v for k, v in (d.get("folder_dirs") or {}).items() if k in d["folders"]}
        for s in d["sessions"].values():
            if s.get("folder") and s["folder"] not in d["folders"]:
                s["folder"] = ""
        _save_index(d)


def append(sid, ev):
    if not ID.match(str(sid)):
        return
    p = os.path.join(ui_dir("history"), sid + ".jsonl")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with _lock, open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def history(sid, upto=None):
    """Stored events of a chat (last MAX_HISTORY). UPTO = only the first N lines: a running turn replays its own events to a page that comes back."""
    if not ID.match(str(sid)):
        return []
    out = []
    try:
        with open(os.path.join(ui_dir("history"), sid + ".jsonl"), encoding="utf-8") as f:
            for n, ln in enumerate(f):
                if upto is not None and n >= upto:
                    break
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    pass
    except OSError:
        pass
    return out[-MAX_HISTORY:]


def count(sid):
    """Lines in the history file (what `history(upto=)` counts)."""
    if not ID.match(str(sid)):
        return 0
    try:
        with open(os.path.join(ui_dir("history"), sid + ".jsonl"), encoding="utf-8") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def transcript(rows, limit=60000):
    """Plain text of a conversation (user text + answers, tools only named). A branch or an edited chat starts a NEW Claude session, which
    gets this in its first prompt, because Claude Code cannot cut a session at an earlier message."""
    out = []
    for e in rows:
        k = e.get("k")
        if k == "user":
            out.append("User: " + str(e.get("text", "")))
        elif k == "msg":
            bl = [b for b in e.get("blocks") or [] if isinstance(b, dict)]
            t = "\n".join(b.get("text", "") for b in bl if b.get("type") == "text")
            tools = sorted({str(b.get("name")) for b in bl if b.get("type") == "tool"})
            if t or tools:
                out.append("Assistant: " + t + ("\n[used tools: %s]" % ", ".join(tools) if tools else ""))
    return "\n\n".join(out)[-limit:]


def cut(sid, index, keep_index=False):
    """Rows of the chat before the page row INDEX (including it with KEEP_INDEX), or None when out of range."""
    if not ID.match(str(sid)):
        return None
    try:
        with open(os.path.join(ui_dir("history"), sid + ".jsonl"), encoding="utf-8") as f:
            rows = f.read().splitlines()
    except OSError:
        return None
    index += max(0, len(rows) - MAX_HISTORY)
    if not 0 <= index < len(rows):
        return None
    return rows[: index + (1 if keep_index else 0)]


def write_rows(sid, rows):
    if not ID.match(str(sid)):
        return
    p = os.path.join(ui_dir("history"), sid + ".jsonl")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with _lock:
        write_private(p, ("\n".join(rows) + ("\n" if rows else "")).encode("utf-8"))


def parse_rows(rows):
    out = []
    for r in rows:
        try:
            out.append(json.loads(r))
        except ValueError:
            pass
    return out


def delete_event(sid, index):
    """Drop one entry (and a tool result that belongs to it) from the stored history of a chat. The Claude session itself is not edited:
    this only hides the entry in the page, so the next turn still has the context. -> new length or None."""
    if not ID.match(str(sid)):
        return None
    p = os.path.join(ui_dir("history"), sid + ".jsonl")
    with _lock:
        try:
            with open(p, encoding="utf-8") as f:
                rows = f.read().splitlines()
        except OSError:
            return None
        index += max(0, len(rows) - MAX_HISTORY)          # the page sees only the last MAX_HISTORY rows
        if not 0 <= index < len(rows):
            return None
        del rows[index]
        write_private(p, ("\n".join(rows) + ("\n" if rows else "")).encode("utf-8"))
        return len(rows)


# ---- files of a chat's working folder

def safe_join(root, rel):
    """Absolute path of REL inside ROOT, or None when it escapes (.., absolute, symlink out)."""
    root = os.path.realpath(root)
    p = os.path.realpath(os.path.join(root, rel.lstrip("/")))
    return p if p == root or p.startswith(root + os.sep) else None


SKIP_DIRS = {"node_modules", "__pycache__", ".git", ".venv", "venv", ".idea", ".vscode", "target", ".next", ".cache"}


def tree(sid, limit=None):
    root = session_dir(sid)
    custom = root != work_dir(sid)
    limit = limit or (3000 if custom else 500)
    out = []
    for d, dirs, fs in os.walk(root):
        dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x not in SKIP_DIRS)
        for f in sorted(fs):
            if f.startswith("."):
                continue
            p = os.path.join(d, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append({"path": os.path.relpath(p, root).replace(os.sep, "/"), "size": st.st_size, "mtime": st.st_mtime})
            if len(out) >= limit:
                return out
    return out


# ---- reading, editing and comparing one file of the working folder
MAX_TEXT = 1_500_000
SNAP_FILES, SNAP_FILE_MAX, SNAP_TOTAL = 400, 256_000, 20_000_000


def _file(sid, rel):
    """(absolute path, clean relative path) of an existing regular file inside the chat's folder, else (None, None). Nothing under .git."""
    rel = str(rel or "").replace("\\", "/").lstrip("/")
    parts = [x for x in rel.split("/") if x]
    if not parts or ".git" in parts or ".." in parts:
        return None, None
    p = safe_join(session_dir(sid), "/".join(parts))
    return (p, "/".join(parts)) if p and os.path.isfile(p) else (None, None)


def _looks_binary(b):
    return b"\0" in b[:4096]


def read_text(sid, rel):
    """-> {"text": str} or {"binary": True} / {"big": size}; None when there is no such file."""
    p, rel = _file(sid, rel)
    if not p:
        return None
    size = os.path.getsize(p)
    if size > MAX_TEXT:
        return {"big": size}
    with open(p, "rb") as f:
        b = f.read()
    if _looks_binary(b):
        return {"binary": True}
    return {"text": b.decode("utf-8", "replace"), "size": size, "mtime": os.path.getmtime(p)}


def write_text(sid, rel, text):
    p, rel = _file(sid, rel)
    if not p:
        raise ValueError("no such file in this chat's folder")
    data = str(text).encode("utf-8")
    if len(data) > 2_000_000:
        raise ValueError("file too large to edit here (2 MB)")
    tmp = p + ".aihub-tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    try:
        os.chmod(tmp, os.stat(p).st_mode & 0o777)
    except OSError:
        pass
    os.replace(tmp, p)
    return {"size": len(data), "mtime": os.path.getmtime(p)}


def snapshot(sid, files):
    """Copy the small text files of the folder as they are NOW (before a turn), so Diff can show what the turn changed. One snapshot per chat."""
    root, dst = session_dir(sid), os.path.join(ui_dir("snap"), sid)
    shutil.rmtree(dst, ignore_errors=True)
    n = total = 0
    for f in files:
        if n >= SNAP_FILES or total >= SNAP_TOTAL:
            break
        if f["size"] > SNAP_FILE_MAX:
            continue
        src = os.path.join(root, f["path"])
        try:
            with open(src, "rb") as fh:
                b = fh.read(SNAP_FILE_MAX + 1)
        except OSError:
            continue
        if _looks_binary(b):
            continue
        out = os.path.join(dst, "files", f["path"])
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as fh:
            fh.write(b)
        n += 1
        total += len(b)
    os.makedirs(dst, exist_ok=True)


def snap_text(sid, rel):
    """Content of a file before the last turn: str, "" for a file that did not exist then, None when no snapshot was taken."""
    base = os.path.join(ui_dir("snap"), sid)
    if not os.path.isdir(base):
        return None
    rel = str(rel).replace("\\", "/").lstrip("/")
    p = safe_join(os.path.join(base, "files"), rel)
    if p and os.path.isfile(p):
        with open(p, "rb") as f:
            return f.read().decode("utf-8", "replace")
    return ""


def git_text(sid, rel):
    """Committed (HEAD) content of a file when the folder is in a git repo and the file is tracked, else None."""
    p, rel = _file(sid, rel)
    if not p:
        return None
    try:
        r = subprocess.run(["git", "show", "HEAD:./" + rel], cwd=session_dir(sid), capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or _looks_binary(r.stdout):
        return None
    return r.stdout.decode("utf-8", "replace")


# ---- search across chats (title + what was said)

def search(q, limit=40):
    q = str(q or "").strip().lower()
    if len(q) < 2:
        return []
    out = []
    for s in sessions()["sessions"]:
        hit, snippet = q in s.get("title", "").lower(), ""
        try:
            with open(os.path.join(ui_dir("history"), s["id"] + ".jsonl"), encoding="utf-8") as f:
                for ln in f:
                    if q not in ln.lower():
                        continue
                    try:
                        e = json.loads(ln)
                    except ValueError:
                        continue
                    if e.get("k") == "user":
                        txt = str(e.get("text", ""))
                    elif e.get("k") == "msg":
                        txt = "\n".join(b.get("text", "") for b in e.get("blocks") or [] if isinstance(b, dict) and b.get("type") == "text")
                    else:
                        continue
                    i = txt.lower().find(q)
                    if i >= 0:
                        a = max(0, i - 40)
                        snippet = (("…" if a else "") + " ".join(txt[a:i + len(q) + 80].split()))[:160]
                        hit = True
                        break
        except OSError:
            pass
        if hit:
            out.append({"id": s["id"], "title": s.get("title", ""), "folder": s.get("folder", ""), "updated": s.get("updated", 0), "snippet": snippet})
            if len(out) >= limit:
                break
    return out


def unique_name(folder, name):
    name = re.sub(r"[^\w.\- ]", "_", os.path.basename(name or "file"))[:100].strip(". ") or "file"
    base, ext = os.path.splitext(name)
    n, cand = 1, name
    while os.path.exists(os.path.join(folder, cand)):
        n += 1
        cand = "%s-%d%s" % (base, n, ext)
    return cand


# ---- works (node graphs) and the UI's own MCP list

INPUT_TYPES = ("text", "textarea", "number", "select", "checkbox")


def parse_inputs(text):
    """The inputs a work asks for when it is run from a chat: one per line, `name|Label|type|options or default|*` (* = required).
    type: text, textarea, number, select (options comma separated, first is the default), checkbox (default true/false)."""
    out = []
    for ln in str(text or "").splitlines()[:30]:
        p = [x.strip() for x in ln.split("|")]
        if not re.match(r"^[A-Za-z_]\w{0,30}$", p[0]) or any(f["name"] == p[0] for f in out):
            continue
        t = p[2] if len(p) > 2 and p[2] in INPUT_TYPES else "text"
        extra = p[3] if len(p) > 3 else ""
        f = {"name": p[0], "label": (p[1] if len(p) > 1 and p[1] else p[0])[:60], "type": t, "required": len(p) > 4 and p[4] == "*"}
        if t == "select":
            f["options"] = [o.strip() for o in extra.split(",") if o.strip()][:30]
            f["default"] = f["options"][0] if f["options"] else ""
        elif t == "checkbox":
            f["default"] = extra.lower() in ("1", "true", "yes", "on")
        else:
            f["default"] = extra[:200]
        out.append(f)
    return out


def work_inputs(w):
    n = next((n for n in (w or {}).get("nodes", []) if n.get("type") == "start.manual"), None)
    return parse_inputs(((n or {}).get("config") or {}).get("inputs"))


def works():
    d = ui_dir("works")
    out = []
    for f in sorted(os.listdir(d)):
        if f.endswith(".json"):
            w = _read(os.path.join(d, f), None)
            if isinstance(w, dict):
                out.append({"id": w.get("id"), "title": w.get("title"), "description": w.get("description", ""), "enabled": bool(w.get("enabled")), "nodes": len(w.get("nodes", [])), "updated": w.get("updated", 0),
                        "inputs": work_inputs(w), "output": any(n.get("type") == "flow.output" for n in w.get("nodes", []))})
    return sorted(out, key=lambda w: -w["updated"])


def get_work(wid):
    return _read(os.path.join(ui_dir("works"), wid + ".json"), None) if ID.match(str(wid)) else None


def clean_work(w):
    """Validated copy of a work (new id when missing). Writes nothing."""
    src = w
    wid = w.get("id")
    if not wid or not ID.match(str(wid)):
        wid = uuid.uuid4().hex[:8]
    nodes = []
    for n in list(w.get("nodes", []))[:200]:
        if isinstance(n, dict) and re.match(r"^[A-Za-z0-9_-]{1,20}$", str(n.get("id", ""))) and isinstance(n.get("config", {}), dict):
            nodes.append({"id": n["id"], "type": str(n.get("type", "flow.note"))[:80], "x": int(n.get("x", 0) or 0), "y": int(n.get("y", 0) or 0), "label": str(n.get("label", ""))[:60], "config": n.get("config", {})})
    ids = {n["id"] for n in nodes}
    edges = [{"from": e["from"], "to": e["to"]} for e in list(w.get("edges", []))[:400] if isinstance(e, dict) and e.get("from") in ids and e.get("to") in ids and e["from"] != e["to"]]
    w = {"id": wid, "title": str(w.get("title") or "Untitled")[:80], "enabled": bool(w.get("enabled")), "nodes": nodes, "edges": edges, "updated": time.time(),
         "description": " ".join(str(w.get("description") or "").split())[:300]}
    try:
        w["keep"] = min(500, max(1, int(src.get("keep") or 40)))
    except (TypeError, ValueError):
        w["keep"] = 40
    return w


def save_work(w):
    """Write an already cleaned work (see clean_work)."""
    w = clean_work(w)
    _write(os.path.join(ui_dir("works"), w["id"] + ".json"), w)
    return w


def delete_work(wid):
    if ID.match(str(wid)):
        shutil.rmtree(os.path.join(ui_dir(), "runs", wid), ignore_errors=True)
        try:
            os.remove(os.path.join(ui_dir("works"), wid + ".json"))
        except OSError:
            pass


def ui_mcp():
    return _read(os.path.join(ui_dir(), "mcp.json"), {"mcpServers": {}})


def save_ui_mcp(d):
    _write(os.path.join(ui_dir(), "mcp.json"), d)


# ---- preferences (not secret): policy guard for UI chats, auto-compact
PREF_DEFAULTS = {"policy": "off", "autocompact": True, "compact_pct": 80, "claude_settings": "", "window": 1_000_000, "free_model": "", "model_picker": True, "alerts": []}
ALERT_PERIODS = ("today", "week", "month", "quarter", "last")


def clean_alerts(raw):
    """Price alert rules {id, period, n, limit, at}: at most 12; a bad entry is a ValueError the page shows."""
    if not isinstance(raw, list) or len(raw) > 12:
        raise ValueError("alerts: a list of at most 12 rules")
    out = []
    for i, r in enumerate(raw):
        if not isinstance(r, dict) or r.get("period") not in ALERT_PERIODS:
            raise ValueError("alert %d: period must be one of %s" % (i + 1, ", ".join(ALERT_PERIODS)))
        try:
            lim = round(float(r.get("limit")), 2)
            n = int(r.get("n") or 7)
            at = r.get("at")
            at = [int(x) for x in (at if isinstance(at, list) else str(at or "80,100").replace(";", ",").split(",")) if str(x).strip()]
        except (TypeError, ValueError):
            raise ValueError("alert %d: limit is a number of US dollars, thresholds are whole percents like 50, 80, 100" % (i + 1))
        if not 0 < lim < 1e7:
            raise ValueError("alert %d: the limit must be above 0" % (i + 1))
        at = sorted({min(500, max(1, x)) for x in at})[:6] or [80, 100]
        out.append({"id": re.sub(r"[^a-z0-9]", "", str(r.get("id") or "").lower())[:12] or secrets.token_hex(4), "period": r["period"],
                    "n": min(365, max(1, n)), "limit": lim, "at": at})
    return out


def prefs():
    d = dict(PREF_DEFAULTS)
    d.update({k: v for k, v in _read(os.path.join(ui_dir(), "prefs.json"), {}).items() if k in PREF_DEFAULTS})
    return d


def save_prefs(new):
    d = prefs()
    if new.get("policy") in ("on", "off"):
        d["policy"] = new["policy"]
    if isinstance(new.get("autocompact"), bool):
        d["autocompact"] = new["autocompact"]
    if "claude_settings" in new:
        c = str(new["claude_settings"] or "").strip()
        if c:
            full = os.path.abspath(os.path.expanduser(c))
            if not full.lower().endswith(".json") or not os.path.isfile(full):
                raise ValueError("that is not an existing .json file: %s" % full)
            try:
                with open(full, encoding="utf-8") as f:
                    if not isinstance(json.loads(f.read(2_000_000)), dict):
                        raise ValueError("it must hold a JSON object")
            except (OSError, ValueError) as e:
                raise ValueError("cannot use %s: %s" % (full, e))
            c = full
        d["claude_settings"] = c
    if isinstance(new.get("model_picker"), bool):
        d["model_picker"] = new["model_picker"]
    try:
        d["compact_pct"] = min(95, max(40, int(new.get("compact_pct", d["compact_pct"]))))
    except (TypeError, ValueError):
        pass
    if "free_model" in new:
        m = str(new["free_model"] or "").strip()
        if m and not re.match(r"^[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,150}$", m):
            raise ValueError("model id: letters, digits and . _ : / @ - only")
        d["free_model"] = m
    if "alerts" in new:
        d["alerts"] = clean_alerts(new["alerts"])
    if "window" in new:
        try:
            d["window"] = min(10_000_000, max(50_000, int(new["window"])))
        except (TypeError, ValueError):
            raise ValueError("context window: a number of tokens, for example 1000000")
    _write(os.path.join(ui_dir(), "prefs.json"), d)
    return d


def prefs_env():
    """Environment the Claude process of a chat or work node gets from the UI preferences.
    policy off: AIHUB_JUDGE=1 makes aihub's hook return at once for THIS process only (the global policy and the terminal are untouched)."""
    p = prefs()
    env = {}
    if p["policy"] == "off":
        env["AIHUB_JUDGE"] = "1"
    env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(p["window"])
    if p["autocompact"]:
        env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(p["compact_pct"])
    else:
        env["DISABLE_AUTO_COMPACT"] = "1"
    return env


# ---- themes for slides and documents (colors + fonts; made by hand or imported from a .pptx in the page)
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
FONT = re.compile(r"^[\w \-,.'\"]{1,100}$")
THEME_COLORS = ("bg", "fg", "muted", "accent", "accent2", "surface")


def themes_dir():
    return ui_dir("themes")


def themes():
    out = []
    d = themes_dir()
    for f in sorted(os.listdir(d)):
        t = _read(os.path.join(d, f), None) if f.endswith(".json") else None
        if isinstance(t, dict) and t.get("id"):
            out.append(t)
    return out


def get_theme(tid):
    return next((t for t in themes() if t["id"] == str(tid)), None)


def save_theme(t):
    name = str(t.get("name") or "").strip()[:60]
    if not name:
        raise ValueError("give the theme a name")
    tid = re.sub(r"[^a-z0-9]+", "-", str(t.get("id") or name).lower()).strip("-")[:40] or "theme"
    colors = {}
    for k in THEME_COLORS:
        v = str((t.get("colors") or {}).get(k, ""))
        if not HEX.match(v):
            raise ValueError("color %s must look like #1a2b3c" % k)
        colors[k] = v.lower()
    fonts = {}
    for k in ("head", "body"):
        v = str((t.get("fonts") or {}).get(k) or "system-ui, sans-serif").strip()
        if not FONT.match(v):
            raise ValueError("font %s: letters, digits, spaces, commas, quotes only" % k)
        fonts[k] = v
    doc = {"id": tid, "name": name, "colors": colors, "fonts": fonts, "notes": str(t.get("notes") or "")[:600], "source": str(t.get("source") or "")[:80], "updated": time.time()}
    _write(os.path.join(themes_dir(), tid + ".json"), doc)
    return doc


def delete_theme(tid):
    if re.match(r"^[a-z0-9-]{1,40}$", str(tid)):
        try:
            os.remove(os.path.join(themes_dir(), tid + ".json"))
        except OSError:
            pass


def theme_prompt(t):
    c, f = t["colors"], t["fonts"]
    return ('[Theme "%s" - use it for the slides or documents you create now. Define on :root: --bg:%s; --fg:%s; --muted:%s; --accent:%s; --accent2:%s; --surface:%s; '
            '--font-head:%s; --font-body:%s;%s]' % (t["name"], c["bg"], c["fg"], c["muted"], c["accent"], c["accent2"], c["surface"], f["head"], f["body"], (" Notes: " + t["notes"]) if t.get("notes") else ""))
