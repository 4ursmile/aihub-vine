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
from collections import deque
from itertools import islice

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
    raw = str(path or "").strip().strip("\"'").strip()
    if not raw:
        raise ValueError("give a folder path")
    full = os.path.abspath(os.path.expanduser(os.path.expandvars(raw)))
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
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(path, data, compact=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_private(path, (json.dumps(data, separators=(",", ":")) if compact else json.dumps(data, indent=1)).encode("utf-8"))


# ---- chats

_icache = {"stamp": None, "data": None}


def _stamp(path):
    try:
        st = os.stat(path)
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


SIDE = ("system", "extra", "seed")          # big per-chat texts: kept out of sessions.json, in a file each


def _side_path(sid, k):
    if k == "system":
        return os.path.join(ui_dir("system"), sid + ".md")       # the file chat.turn passes to claude as-is
    return os.path.join(ui_dir("side"), "%s.%s.txt" % (sid, k))


def _side_get(sid, k):
    try:
        with open(_side_path(sid, k), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _side_put(sid, k, v):
    p = _side_path(sid, k)
    v = "" if v is None else str(v)
    if v:
        if _side_get(sid, k) != v:
            write_private(p, v.encode("utf-8"))
    else:
        try:
            os.remove(p)
        except OSError:
            pass


def _full(s):
    """Copy of a slim session row with system / extra / seed read back from their files."""
    s = dict(s)
    for k in SIDE:
        s[k] = _side_get(s["id"], k)
    return s


def _migrate(d):
    """Old sessions.json kept system / extra / seed inside every row (94% of the file): move them to files. -> True when it changed."""
    fat = [s for s in d["sessions"].values() if isinstance(s, dict) and any(k in s for k in SIDE)]
    for s in fat:
        for k in SIDE:
            v = s.pop(k, None)
            if v and not _side_get(s["id"], k):
                _side_put(s["id"], k, v)
    return bool(fat)


def _index(shared=False):
    """sessions.json. SHARED=True: the parsed copy kept until the file changes (read only: do not modify it); else a private parse to edit and save."""
    path = ui_dir("sessions.json")
    st = _stamp(path)
    if shared and st is not None and _icache["stamp"] == st:
        return _icache["data"]
    d = _read(path, {"sessions": {}, "folders": []})
    if st is not None and isinstance(d.get("sessions"), dict):
        with _lock:
            if _migrate(d):
                _save_index(d)
                st = _stamp(path)
    if shared:
        _icache["stamp"], _icache["data"] = st, d
    return d


def _save_index(d):
    p = os.path.join(ui_dir(), "sessions.json")
    _write(p, d, compact=True)
    _icache["stamp"] = None


def sessions(full=False):
    """Folders + chats for the list (newest first). Rows are SLIM (no system / extra / seed); FULL=True loads those too."""
    with _lock:
        d = _index(True)
        rows = sorted(d["sessions"].values(), key=lambda s: -s.get("updated", 0))
        return {"folders": d["folders"], "folder_dirs": d.get("folder_dirs") or {}, "sessions": [_full(s) for s in rows] if full else rows}


def get_session(sid, full=True):
    """One chat. FULL=False skips the side files (system / extra / seed): cheaper for callers that only need cwd, engine, ..."""
    if not ID.match(str(sid)):
        return None
    with _lock:
        s = _index(True)["sessions"].get(sid)
        if not s:
            return None
        return _full(s) if full else dict(s)


def create_session(title="", folder="", system="", cwd="", extra=""):
    sid = uuid.uuid4().hex[:8]
    if not cwd:
        os.makedirs(work_dir(sid), exist_ok=True)
    now = time.time()
    s = {"id": sid, "title": title or "New chat", "folder": folder, "created": now, "updated": now, "claude": str(uuid.uuid4()),
         "started": False, "cwd": cwd, "soul": None, "engine": "claude", "mcp": [], "model": "", "effort": "", "mode": "auto", "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0, "turns": 0}}
    for k, v in (("system", system), ("extra", extra)):
        _side_put(sid, k, v)
    with _lock:
        d = _index()
        d["sessions"][sid] = s
        _save_index(d)
    return dict(s, system=system or "", extra=extra or "", seed="")


def update_session(sid, **kw):
    """Change fields of a chat -> the full row (system / extra / seed included), None when it does not exist. Nothing is written when
    nothing changed."""
    with _lock:
        d = _index()
        s = d["sessions"].get(sid)
        if not s:
            return None
        for k in SIDE:
            if k in kw:
                _side_put(sid, k, kw.pop(k))
        if any(k not in s or s[k] != v for k, v in kw.items()):
            s.update(kw)
            _save_index(d)
        return _full(s)


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
    _counts.pop(sid, None)
    _scache.pop(sid, None)
    for p in [os.path.join(ui_dir("history"), sid + ".jsonl")] + [_side_path(sid, k) for k in SIDE]:
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


_counts = {}                      # sid -> lines in the history file (kept in memory; set once, then bumped by append)


def append(sid, ev):
    if not ID.match(str(sid)):
        return
    p = os.path.join(ui_dir("history"), sid + ".jsonl")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with _lock, open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        if sid in _counts:
            _counts[sid] += 1


def history(sid, upto=None):
    """Stored events of a chat (last MAX_HISTORY). UPTO = only the first N lines: a running turn replays its own events to a page that comes back.
    Only the tail lines are parsed."""
    if not ID.match(str(sid)):
        return []
    try:
        with open(os.path.join(ui_dir("history"), sid + ".jsonl"), encoding="utf-8") as f:
            lines = deque(islice(f, upto) if upto is not None else f, maxlen=MAX_HISTORY)
    except OSError:
        return []
    return parse_rows(lines)


def count(sid):
    """Lines in the history file (what `history(upto=)` counts). Counted from the file once per chat, then kept in memory."""
    if not ID.match(str(sid)):
        return 0
    with _lock:
        n = _counts.get(sid)
        if n is None:
            try:
                with open(os.path.join(ui_dir("history"), sid + ".jsonl"), encoding="utf-8") as f:
                    n = sum(1 for _ in f)
            except OSError:
                n = 0
            _counts[sid] = n
        return n


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
        _counts[sid] = len(rows)


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
        _counts[sid] = len(rows)
        return len(rows)


# ---- files of a chat's working folder

def inside(p, root):
    """P is ROOT or below it (case-insensitive on Windows, where C:\\Users and c:\\users are the same folder)."""
    p, root = os.path.normcase(os.path.normpath(p)), os.path.normcase(os.path.normpath(root))
    return p == root or p.startswith(root.rstrip("\\/") + os.sep)


def safe_join(root, rel):
    """Absolute path of REL inside ROOT, or None when it escapes (.., absolute, symlink out)."""
    root = os.path.realpath(root)
    p = os.path.realpath(os.path.join(root, rel.lstrip("/")))
    return p if inside(p, root) else None


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
    tmp = "%s.aihub-%s.tmp" % (p, secrets.token_hex(4))              # unique: two saves at once cannot share a temp file
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        try:
            os.chmod(tmp, os.stat(p).st_mode & 0o777)
        except OSError:
            pass
        os.replace(tmp, p)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return {"size": len(data), "mtime": os.path.getmtime(p)}


def snapshot(sid, files):
    """Copy the small text files of the folder as they are NOW (before a turn), so Diff can show what the turn changed. One snapshot per chat.
    Incremental: a file whose copy has the same size and mtime is left alone, copies of files that are gone are deleted."""
    root, dst = session_dir(sid), os.path.join(ui_dir("snap"), sid)
    base = os.path.join(dst, "files")
    keep = set()
    n = total = 0
    for f in files:
        if n >= SNAP_FILES or total >= SNAP_TOTAL:
            break
        if f["size"] > SNAP_FILE_MAX:
            continue
        rel = f["path"]
        out = os.path.join(base, rel)
        try:
            st = os.stat(os.path.join(root, rel))
            ot = os.stat(out)
            same = ot.st_size == st.st_size and ot.st_mtime_ns == st.st_mtime_ns
        except OSError:
            same = False
        if same:
            keep.add(os.path.normcase(out))
            n += 1
            total += ot.st_size
            continue
        try:
            with open(os.path.join(root, rel), "rb") as fh:
                b = fh.read(SNAP_FILE_MAX + 1)
            mt = os.stat(os.path.join(root, rel)).st_mtime_ns
        except OSError:
            continue
        if _looks_binary(b) or len(b) > SNAP_FILE_MAX:
            continue
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as fh:
            fh.write(b)
        try:
            os.utime(out, ns=(mt, mt))
        except OSError:
            pass
        keep.add(os.path.normcase(out))
        n += 1
        total += len(b)
    for d, dirs, fs in os.walk(base, topdown=False):
        for x in fs:
            p = os.path.join(d, x)
            if os.path.normcase(p) not in keep:
                try:
                    os.remove(p)
                except OSError:
                    pass
        if d != base:
            try:
                os.rmdir(d)
            except OSError:
                pass
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

SEARCH_FILE_BYTES = 2_000_000       # only the last bytes of a long history are searched
SEARCH_SECONDS = 1.5
_scache = {}                        # sid -> ((mtime_ns, size), [texts])


class Hits(list):
    """Search result; `.partial` is True when the time budget ran out before every chat was looked at."""
    partial = False


def _texts(sid):
    """Searchable texts (what the user said, what the assistant wrote) of a chat, cached until its history file changes."""
    p = os.path.join(ui_dir("history"), sid + ".jsonl")
    st = _stamp(p)
    if st is None:
        _scache.pop(sid, None)
        return []
    c = _scache.get(sid)
    if c and c[0] == st:
        return c[1]
    out = []
    try:
        with open(p, "rb") as f:
            if st[1] > SEARCH_FILE_BYTES:
                f.seek(st[1] - SEARCH_FILE_BYTES)
                f.readline()                                    # the first line is cut: skip it
            raw = f.read()
    except OSError:
        return []
    for ln in raw.decode("utf-8", "replace").splitlines():
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        if e.get("k") == "user":
            out.append(str(e.get("text", "")))
        elif e.get("k") == "msg":
            out.append("\n".join(b.get("text", "") for b in e.get("blocks") or [] if isinstance(b, dict) and b.get("type") == "text"))
    _scache[sid] = (st, out)
    return out


def search(q, limit=40):
    """Chats whose title or text contains Q -> Hits (a list; `.partial` when the 1.5 s budget ended early)."""
    q = str(q or "").strip().lower()
    out = Hits()
    if len(q) < 2:
        return out
    t0 = time.time()
    for s in sessions()["sessions"]:
        if time.time() - t0 > SEARCH_SECONDS:
            out.partial = True
            break
        hit, snippet = q in s.get("title", "").lower(), ""
        for txt in _texts(s["id"]):
            i = txt.lower().find(q)
            if i >= 0:
                a = max(0, i - 40)
                snippet = (("…" if a else "") + " ".join(txt[a:i + len(q) + 80].split()))[:160]
                hit = True
                break
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


def work_brief(w):
    """Prompt text for a mentioned workflow: its inputs and the protocol (ask for the missing ones with a form, else ONE ```work block)."""
    ins = work_inputs(w)
    lines = ["- %s (%s; %s%s%s)" % (f["name"], f["label"], f["type"], ", required" if f["required"] else "", (", options: " + ", ".join(f["options"])) if f.get("options") else "") for f in ins]
    need = [f["name"] for f in ins if f["required"]]
    return ("[The user wants the workflow \"%s\" (@work:%s) run from this chat. %s\nInputs:\n%s\n"
            "How to run it: work out each input from the user's message and this conversation. If a required input (%s) is still unknown, ask for ONLY the missing ones with a ```form block "
            "(fields named exactly like the inputs) and stop; do not guess. Once they are known, answer with ONE ```work block and nothing that depends on its result: "
            "{\"id\":\"%s\",\"data\":{\"input\":\"value\"},\"text\":\"optional text for the start\"}. The page shows a card where the user presses Run; the result comes back as the next message. Do not run it yourself.]"
            % (w.get("title") or "Workflow", w["id"], w.get("description") or "", "\n".join(lines) or "(none)", ", ".join(need) or "none", w["id"]))


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
    if re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", str(src.get("package") or "")):
        w["package"] = src["package"]                               # imported from a workflow package: its runs count as uses of it
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


def _names(v):
    return [str(x)[:64] for x in v if isinstance(x, str)][:100] if isinstance(v, list) else []


def mcp_state():
    """Free chat MCP settings: `off` = servers never used in free chat, `default_on` = ticked in new free chats, `tools_off` = {server: [tool]} never offered."""
    d = _read(os.path.join(ui_dir(), "mcp_state.json"), {})
    d = d if isinstance(d, dict) else {}
    t = d.get("tools_off") if isinstance(d.get("tools_off"), dict) else {}
    return {"off": _names(d.get("off")), "default_on": _names(d.get("default_on")), "tools_off": {str(k)[:64]: _names(v) for k, v in t.items() if _names(v)}}


def save_mcp_state(new):
    d = mcp_state()
    for k in ("off", "default_on"):
        if k in new:
            d[k] = _names(new[k])
    if isinstance(new.get("tools_off"), dict):
        d["tools_off"] = {str(k)[:64]: _names(v) for k, v in new["tools_off"].items() if _names(v)}
    _write(os.path.join(ui_dir(), "mcp_state.json"), d)
    return d


def save_ui_mcp(d):
    _write(os.path.join(ui_dir(), "mcp.json"), d)


# ---- preferences (not secret): policy guard for UI chats, auto-compact
PREF_DEFAULTS = {"policy": "off", "autocompact": True, "compact_pct": 80, "claude_settings": "", "window": 1_000_000, "free_model": "", "model_picker": True, "alerts": [], "auto_update": "on"}
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
            from .aihub import user_path, _jsonc
            full = user_path(c)
            if not full.lower().endswith(".json") or not os.path.isfile(full):
                raise ValueError("that is not an existing .json file: %s" % full)
            try:
                with open(full, encoding="utf-8-sig") as f:
                    if not isinstance(_jsonc(f.read(2_000_000)), dict):
                        raise ValueError("it must hold a JSON object")
            except (OSError, ValueError) as e:
                raise ValueError("cannot use %s: %s" % (full, e))
            c = full
        d["claude_settings"] = c
    if new.get("auto_update") in ("on", "notify", "off"):
        d["auto_update"] = new["auto_update"]
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
    policy off: AIHUB_NO_GUARD=1 makes aihub's hook skip the guard and the judge for THIS process only (the global policy and the terminal are
    untouched). Usage counting (skill / agent / MCP `use` events, token upload) stays on: do NOT use AIHUB_JUDGE here, it silences those too."""
    p = prefs()
    env = {}
    if p["policy"] == "off":
        env["AIHUB_NO_GUARD"] = "1"
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
