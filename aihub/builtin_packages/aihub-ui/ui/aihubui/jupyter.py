"""Notebook: a client for Jupyter servers (JupyterHub single-user servers, plain Jupyter Server / Lab, or one this UI installs and runs locally).

Servers live in ~/.aihub/ui/jupyter.json WITHOUT secrets; the API token sits in the encrypted box under `jupyter:<id>`. The browser never sees a token:
every call goes through this module (REST for files and sessions, a WebSocket per kernel / terminal that a thread keeps buffered and the page polls).
A server is {id, name, kind: server|hub|local, url, user, verify, kernel, env, root}.
SQL cells: a code cell whose metadata `aihub.sql` = {conn, var, limit} holds SQL; it runs through the SQL Studio connection `conn` (so passwords stay here)
and, with `var`, the rows are sent into the kernel as a pandas DataFrame named `var`."""
import ast
import atexit
import base64
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import nodeenv, sqlstudio, store
from .wsclient import WS

SECRET = "jupyter:"
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
KINDS = ("server", "hub", "local")
DEFAULT_ENV = "jupyter"
LOCAL_PIPS = ["jupyter_server>=2", "ipykernel"]
MAX_FRAME_B64 = 8 * 1024 * 1024
_lock = threading.Lock()
LOCAL = {}                                         # server id -> {p, port, token}
CH = {}                                            # (server id, "k"|"t", kernel id / terminal name) -> Channel
SETTINGS = {"suggest": {"on": True, "model": "haiku", "delay": 900}, "write": {"model": ""}, "fix": {"model": ""}}


class JError(Exception):
    def __init__(self, msg, status=502):
        Exception.__init__(self, msg)
        self.status = status


# ---------- saved servers

def _path():
    return store.ui_dir("jupyter.json")


def _load():
    d = store._read(_path(), {})
    d = d if isinstance(d, dict) else {}
    if not isinstance(d.get("servers"), list):
        d["servers"] = []
    return d


def _public(r):
    o = {k: r.get(k, "") for k in ("id", "name", "kind", "url", "user", "kernel", "env", "root")}
    o["verify"] = r.get("verify", True) is not False
    o["has_secret"] = bool(r.get("has_secret"))
    return o


def servers():
    out = []
    for r in _load()["servers"]:
        o = _public(r)
        if r.get("kind") == "local":
            lc = LOCAL.get(r["id"])
            o["running"] = bool(lc and lc["p"].poll() is None)
        out.append(o)
    return out


def get(ref):
    """A saved server by id, exact name or unique name prefix. Raises ValueError listing what exists."""
    ss, ref = _load()["servers"], str(ref or "").strip()
    if not ref and len(ss) == 1:
        return ss[0]
    low = ref.lower()
    hit = [s for s in ss if s.get("id") == ref] or [s for s in ss if s.get("name", "").lower() == low] or ([s for s in ss if s.get("name", "").lower().startswith(low)] if low else [])
    if len(hit) == 1:
        return hit[0]
    raise ValueError(("Several servers match %r. " if len(hit) > 1 else "No Jupyter server %r. ") % ref + "Available: " + (", ".join("%s (%s)" % (s["name"], s["id"]) for s in ss) or "none saved yet"))


def save(box, b):
    name = str(b.get("name") or "").strip()[:60]
    if not name:
        raise ValueError("Give the server a name.")
    kind = str(b.get("kind") or "server")
    if kind not in KINDS:
        raise ValueError("Unknown server type.")
    url = str(b.get("url") or "").strip().rstrip("/")
    user = str(b.get("user") or "").strip()[:80]
    if kind != "local":
        if not re.match(r"^https?://[^\s/]+", url):
            raise ValueError("The address must start with http:// or https://")
        if kind == "hub" and not user:
            raise ValueError("JupyterHub needs your user name.")
    env = str(b.get("env") or DEFAULT_ENV).strip()
    if kind == "local":
        url = ""
        if not nodeenv.ENV_NAME.match(env) and not os.path.isabs(os.path.expanduser(env)):
            raise ValueError("Use a short lowercase environment name (letters, digits, - _).")
    sid = str(b.get("id") or "")
    with _lock:
        d = _load()
        cur = next((s for s in d["servers"] if s.get("id") == sid), None) if sid else None
        if sid and not cur:
            raise ValueError("That server no longer exists.")
        if not cur:
            cur = {"id": uuid.uuid4().hex[:8]}
            d["servers"].append(cur)
        cur.update(name=name, kind=kind, url=url, user=user, env=env, verify=b.get("verify", True) is not False,
                   kernel=str(b.get("kernel") or "").strip()[:80], root=str(b.get("root") or "").strip()[:500])
        tok = str(b.get("token") or "").strip()
        if tok:
            box.put(SECRET + cur["id"], tok)
        elif b.get("clear_token"):
            box.remove(SECRET + cur["id"])
        cur["has_secret"] = bool(box.get_all().get(SECRET + cur["id"]))
        store._write(_path(), d)
        return _public(cur)


def delete(box, sid):
    with _lock:
        d = _load()
        n = len(d["servers"])
        d["servers"] = [s for s in d["servers"] if s.get("id") != sid]
        if len(d["servers"]) == n:
            return False
        store._write(_path(), d)
    box.remove(SECRET + sid)
    local_stop(sid)
    return True


def settings():
    s = _load().get("settings")
    out = json.loads(json.dumps(SETTINGS))
    if isinstance(s, dict):
        for k in out:
            if isinstance(s.get(k), dict):
                out[k].update({a: s[k][a] for a in out[k] if a in s[k]})
    return out


def save_settings(b):
    cur, ok = settings(), re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\[\]@+=,-]{0,150}$")
    for k in cur:
        src = b.get(k) if isinstance(b.get(k), dict) else {}
        if "model" in src and (src["model"] == "" or ok.match(str(src["model"]))):
            cur[k]["model"] = str(src["model"])
        if k == "suggest":
            if "on" in src:
                cur[k]["on"] = bool(src["on"])
            if "delay" in src:
                try:
                    cur[k]["delay"] = max(400, min(5000, int(src["delay"])))
                except (TypeError, ValueError):
                    pass
    with _lock:
        d = _load()
        d["settings"] = cur
        store._write(_path(), d)
    return cur


# ---------- connection

class Conn:
    def __init__(self, rec, base, token, verify):
        self.id, self.rec, self.base, self.token, self.verify = rec["id"], rec, base, token, verify
        self.ready_at = 0.0

    def ctx(self):
        if not self.base.startswith("https"):
            return None
        c = ssl.create_default_context()
        if not self.verify:
            c.check_hostname, c.verify_mode = False, ssl.CERT_NONE
        return c

    def headers(self):
        return {"Authorization": "token " + self.token} if self.token else {}

    def ws_url(self, path):
        u = self.base.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + path
        return u + ("&" if "?" in u else "?") + ("token=" + urllib.parse.quote(self.token) if self.token else "")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def http(c, method, path, body=None, timeout=30, absolute=False):
    url = path if absolute else c.base + "api/" + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = dict(c.headers(), Accept="application/json")
    if data is not None:
        h["Content-Type"] = "application/json"
    rq = urllib.request.Request(url, data=data, headers=h, method=method)
    op = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=c.ctx()))
    try:
        with op.open(rq, timeout=timeout) as r:
            raw = r.read()
            ctype = r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        raw = e.read() or b""
        if 300 <= e.code < 400:
            raise JError("The server redirected to a login page: the address or the token is wrong.", 401)
        msg = ""
        try:
            j = json.loads(raw.decode("utf-8", "replace"))
            msg = str(j.get("message") or j.get("reason") or "")
        except ValueError:
            pass
        hint = {401: "token rejected", 403: "token rejected or not allowed", 404: "not found", 405: "not allowed", 409: "conflict"}.get(e.code, "")
        raise JError("Jupyter answered %d%s%s" % (e.code, (" (%s)" % hint) if hint else "", (": " + msg[:300]) if msg else ""), e.code if e.code in (404, 409) else 502)
    except (urllib.error.URLError, OSError) as e:
        raise JError("Cannot reach %s: %s" % (urllib.parse.urlparse(url).netloc, getattr(e, "reason", e)))
    if not raw.strip():
        return {}
    if "json" not in ctype:
        raise JError("The address did not answer like a Jupyter server (check the URL and the token).")
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise JError("Jupyter sent an answer that is not JSON.")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def local_status(rec):
    """-> {env, python, exists, mods: {name: version|None}, running, port, log}."""
    py, name = nodeenv.resolve(rec.get("env") or DEFAULT_ENV)
    out = {"env": rec.get("env") or DEFAULT_ENV, "python": py, "exists": os.path.isfile(py), "mods": {}, "running": False}
    if out["exists"]:
        pr = nodeenv.probe(py, [{"module": m, "pip": p} for m, p in (("jupyter_server", "jupyter_server"), ("ipykernel", "ipykernel"), ("pandas", "pandas"), ("matplotlib", "matplotlib"), ("numpy", "numpy"))], fresh=True)
        out["mods"] = pr.get("mods") or {}
        out["error"] = pr.get("error") or ""
    lc = LOCAL.get(rec["id"])
    out["running"] = bool(lc and lc["p"].poll() is None)
    out["port"] = lc["port"] if out["running"] else 0
    return out


def local_setup(rec, extras=None):
    """Create the environment and install Jupyter Server + ipykernel (+ extras). -> the install job (poll with nodeenv.job_view)."""
    pips = LOCAL_PIPS + [p for p in (extras or []) if re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", str(p))]
    return nodeenv.install(rec.get("env") or DEFAULT_ENV, pips)


def local_start(rec):
    cur = LOCAL.get(rec["id"])
    if cur and cur["p"].poll() is None:
        return cur
    py, _ = nodeenv.resolve(rec.get("env") or DEFAULT_ENV)
    if not os.path.isfile(py):
        raise JError("The Jupyter environment is not set up yet: press Set up.", 400)
    st = local_status(rec)
    if st["mods"].get("jupyter_server") is None or st["mods"].get("ipykernel") is None:
        raise JError("Jupyter Server or ipykernel is missing in environment %r: press Set up." % st["env"], 400)
    port, token = _free_port(), secrets.token_urlsafe(24)
    root = os.path.expanduser(rec.get("root") or "") or store.ui_dir("notebooks")
    os.makedirs(root, exist_ok=True)
    log = store.ui_dir("jupyter-%s.log" % rec["id"])
    argv = [py, "-m", "jupyter_server", "--no-browser", "--ip=127.0.0.1", "--port=%d" % port, "--port-retries=0", "--ServerApp.token=" + token,
            "--IdentityProvider.token=" + token, "--ServerApp.root_dir=" + root, "--ServerApp.disable_check_xsrf=True"]
    with open(log, "wb") as lf:
        p = subprocess.Popen(argv, stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=root, start_new_session=True)
    lc = {"p": p, "port": port, "token": token}
    c = Conn(rec, "http://127.0.0.1:%d/" % port, token, True)
    end = time.time() + 60
    while time.time() < end:
        if p.poll() is not None:
            break
        try:
            http(c, "GET", "status", timeout=3)
            LOCAL[rec["id"]] = lc
            return lc
        except JError:
            time.sleep(0.5)
    try:
        p.terminate()
    except OSError:
        pass
    try:
        tail = open(log, encoding="utf-8", errors="replace").read()[-600:]
    except OSError:
        tail = ""
    raise JError("The local Jupyter server did not start. " + tail)


def local_stop(sid):
    lc = LOCAL.pop(sid, None)
    if lc and lc["p"].poll() is None:
        try:
            lc["p"].terminate()
        except OSError:
            pass
        return True
    return False


atexit.register(lambda: [local_stop(k) for k in list(LOCAL)])


def conn(box, ref):
    """-> a ready Conn for the saved server REF (JupyterHub: the user's server is started when it is not running; local: started on demand)."""
    rec = get(ref)
    kind, tok = rec.get("kind"), box.get_all().get(SECRET + rec["id"], "") if box else ""
    verify = rec.get("verify", True) is not False
    if kind == "local":
        lc = local_start(rec)
        return Conn(rec, "http://127.0.0.1:%d/" % lc["port"], lc["token"], True)
    if kind == "hub":
        hub = rec["url"].rstrip("/")
        c = Conn(rec, hub + "/user/%s/" % urllib.parse.quote(rec["user"]), tok, verify)
        key = ("hub", rec["id"])
        if time.time() - _READY.get(key, 0) > 90:
            hub_ready(c, hub)
            _READY[key] = time.time()
        return c
    return Conn(rec, rec["url"].rstrip("/") + "/", tok, verify)


_READY = {}


def hub_ready(c, hub, wait=150):
    user = urllib.parse.quote(c.rec["user"])
    end, started = time.time() + wait, False
    while True:
        d = http(c, "GET", "%s/hub/api/users/%s" % (hub, user), absolute=True)
        srv = (d.get("servers") or {}).get("")
        if srv and srv.get("ready"):
            return
        if not srv and not started:
            http(c, "POST", "%s/hub/api/users/%s/server" % (hub, user), {}, absolute=True)
            started = True
        if time.time() > end:
            raise JError("Your JupyterHub server did not become ready within %d s." % wait)
        time.sleep(2)


def about(c):
    """Connection test: -> {version, kernelspecs: [...], default}."""
    v = http(c, "GET", "")                                              # /api/ -> {"version": ...}
    return dict(version=v.get("version", ""), **kernelspecs(c))


def kernelspecs(c):
    d = http(c, "GET", "kernelspecs")
    specs = [{"name": k, "display": (v.get("spec") or {}).get("display_name") or k, "language": (v.get("spec") or {}).get("language") or ""} for k, v in (d.get("kernelspecs") or {}).items()]
    return {"kernelspecs": specs, "default": d.get("default") or (specs[0]["name"] if specs else "")}


# ---------- files

def _q(p):
    return urllib.parse.quote(str(p or "").strip("/"), safe="/")


def ls(c, path=""):
    d = http(c, "GET", "contents/%s?content=1" % _q(path))
    if d.get("type") != "directory":
        raise JError("%s is not a folder" % (path or "/"), 400)
    items = [{"name": x.get("name"), "path": x.get("path"), "type": x.get("type"), "size": x.get("size"), "modified": x.get("last_modified")} for x in d.get("content") or []]
    items.sort(key=lambda x: (x["type"] != "directory", str(x["name"]).lower()))
    return {"path": d.get("path") or "", "items": items}


def read_file(c, path):
    d = http(c, "GET", "contents/%s?content=1" % _q(path), timeout=60)
    return {"type": d.get("type"), "format": d.get("format"), "content": d.get("content"), "modified": d.get("last_modified"), "size": d.get("size"), "mimetype": d.get("mimetype"), "name": d.get("name")}


def write_file(c, path, kind, content, fmt="text"):
    body = {"type": kind}
    if kind != "directory":
        body.update(content=content, format=("json" if kind == "notebook" else fmt))
    d = http(c, "PUT", "contents/%s" % _q(path), body, timeout=60)
    return {"path": d.get("path"), "modified": d.get("last_modified")}


def rename(c, path, new):
    d = http(c, "PATCH", "contents/%s" % _q(path), {"path": new.strip("/")})
    return {"path": d.get("path")}


def remove(c, path):
    http(c, "DELETE", "contents/%s" % _q(path))
    return {"ok": True}


EMPTY_NB = {"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}


def new_item(c, folder, kind, name):
    name = (name or {"notebook": "Untitled.ipynb", "file": "untitled.txt", "directory": "New folder"}[kind]).strip().strip("/")
    path = (folder.strip("/") + "/" if folder.strip("/") else "") + name
    if kind == "notebook":
        return write_file(c, path if path.endswith(".ipynb") else path + ".ipynb", "notebook", dict(EMPTY_NB))
    return write_file(c, path, kind, "" if kind == "file" else None)


# ---------- websocket channels (kernel and terminal)

KEEP = {"stream", "display_data", "update_display_data", "execute_result", "error", "clear_output", "execute_input", "execute_reply", "status", "input_request"}
MAX_BUF = 24 * 1024 * 1024


def _kernel_event(text):
    try:
        m = json.loads(text)
    except ValueError:
        return None
    t = (m.get("header") or {}).get("msg_type")
    if t not in KEEP:
        return None
    return {"p": (m.get("parent_header") or {}).get("msg_id", ""), "t": t, "c": m.get("content") or {}}


class Channel:
    """A websocket kept open by a reader thread; messages get a sequence number and wait in a bounded buffer until the page polls them."""

    def __init__(self, url, headers, ctx, transform=None):
        self.ws = WS(url, headers, ctx)
        self.items, self.seq, self.size, self.closed = [], 0, 0, False
        self.cv, self.transform, self.last = threading.Condition(), transform, time.time()
        self.session = uuid.uuid4().hex
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        try:
            while True:
                t = self.ws.recv()
                if t is None:
                    break
                v = self.transform(t) if self.transform else t
                if v is None:
                    continue
                with self.cv:
                    self.seq += 1
                    self.items.append((self.seq, v, len(t)))
                    self.size += len(t)
                    while self.size > MAX_BUF and len(self.items) > 1:
                        self.size -= self.items.pop(0)[2]
                    self.cv.notify_all()
        except (OSError, ValueError):
            pass
        with self.cv:
            self.closed = True
            self.cv.notify_all()

    def send(self, text):
        self.ws.send(text)

    def read(self, after, wait=0.0):
        """-> (items newer than AFTER as [(seq, value)], last seq, closed)."""
        self.last = time.time()
        end = time.time() + wait
        with self.cv:
            while not self.closed and not (self.items and self.items[-1][0] > after):
                left = end - time.time()
                if left <= 0:
                    break
                self.cv.wait(left)
            return [(s, v) for s, v, _ in self.items if s > after], self.seq, self.closed

    def close(self):
        self.ws.close()


def channel(c, kind, ident):
    now = time.time()
    with _lock:
        for k, ch in list(CH.items()):
            if ch.closed or now - ch.last > 1200:
                CH.pop(k, None)
                ch.close()
        ch = CH.get((c.id, kind, ident))
        if ch:
            return ch
    if kind == "k":
        url, tr = c.ws_url("api/kernels/%s/channels?session_id=%s" % (urllib.parse.quote(ident), uuid.uuid4().hex)), _kernel_event
    else:
        url, tr = c.ws_url("terminals/websocket/%s" % urllib.parse.quote(ident)), None
    try:
        ch = Channel(url, c.headers(), c.ctx(), tr)
    except OSError as e:
        raise JError("Cannot open the %s connection: %s" % ("kernel" if kind == "k" else "terminal", e))
    with _lock:
        CH[(c.id, kind, ident)] = ch
    return ch


def drop_channel(c, kind, ident):
    with _lock:
        ch = CH.pop((c.id, kind, ident), None)
    if ch:
        ch.close()


# ---------- kernels

def session(c, path, kernel=""):
    """The notebook's session (reused when the file already has one, so a reload keeps the kernel and its variables). -> {id, kernel: {id, name}}."""
    for s in http(c, "GET", "sessions") or []:
        if s.get("path") == path:
            return {"id": s["id"], "kernel": s.get("kernel") or {}, "reused": True}
    body = {"path": path, "name": os.path.basename(path), "type": "notebook", "kernel": {"name": kernel} if kernel else None}
    s = http(c, "POST", "sessions", body)
    return {"id": s["id"], "kernel": s.get("kernel") or {}, "reused": False}


def switch_kernel(c, sid, kernel):
    s = http(c, "PATCH", "sessions/" + urllib.parse.quote(sid), {"kernel": {"name": kernel}})
    return {"id": s["id"], "kernel": s.get("kernel") or {}}


def end_session(c, sid):
    http(c, "DELETE", "sessions/" + urllib.parse.quote(sid))
    return {"ok": True}


def kernel_op(c, kid, op):
    if op in ("interrupt", "restart"):
        http(c, "POST", "kernels/%s/%s" % (urllib.parse.quote(kid), op))
    return {"ok": True}


def _msg(session_id, mtype, content, mid=None):
    mid = mid or uuid.uuid4().hex
    return mid, json.dumps({"header": {"msg_id": mid, "username": "aihub", "session": session_id, "msg_type": mtype, "version": "5.3",
                                       "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, "parent_header": {}, "metadata": {}, "content": content,
                            "buffers": [], "channel": "stdin" if mtype == "input_reply" else "shell"})


FRAMES = {}                                        # id -> html of a rich output (plotly, bokeh ...), served sandboxed at /nbframe/<id>


def frame_put(html):
    fid = uuid.uuid4().hex
    with _lock:
        FRAMES[fid] = html[:8_000_000]
        while len(FRAMES) > 60:
            FRAMES.pop(next(iter(FRAMES)))
    return fid


def execute(c, kid, code, stdin=True, expr=None, silent=False, mid=None):
    """Send code to the kernel. -> the message id its output events carry as `p` (MID = chosen by the page so it can listen before sending)."""
    ch = channel(c, "k", kid)
    mid, raw = _msg(ch.session, "execute_request", {"code": code, "silent": silent, "store_history": not silent, "user_expressions": expr or {}, "allow_stdin": stdin, "stop_on_error": True}, mid)
    ch.send(raw)
    return mid


def reply_input(c, kid, value):
    ch = channel(c, "k", kid)
    ch.send(_msg(ch.session, "input_reply", {"value": value})[1])


def poll(c, kind, ident, after, wait):
    ch = channel(c, kind, ident)
    items, last, closed = ch.read(after, wait)
    return {"events": [dict(v, n=s) if isinstance(v, dict) else {"n": s, "d": v} for s, v in items], "next": last, "closed": closed, "ch": id(ch)}


def term_new(c):
    return http(c, "POST", "terminals", {})


def term_write(c, name, data=None, rows=None, cols=None):
    ch = channel(c, "t", name)
    if data:
        ch.send(json.dumps(["stdin", data]))
    if rows and cols:
        ch.send(json.dumps(["set_size", int(rows), int(cols)]))


def term_close(c, name):
    drop_channel(c, "t", name)
    try:
        http(c, "DELETE", "terminals/" + urllib.parse.quote(name))
    except JError:
        pass


# ---------- outputs as nbformat, SQL into a DataFrame, running a whole notebook

class Fold:
    """Kernel output events -> nbformat `outputs`."""

    def __init__(self):
        self.outputs, self.display = [], {}

    def add(self, e):
        t, c = e["t"], e["c"]
        o = self.outputs
        if t == "stream":
            if o and o[-1].get("output_type") == "stream" and o[-1].get("name") == c.get("name"):
                o[-1]["text"] += c.get("text", "")
            else:
                o.append({"output_type": "stream", "name": c.get("name", "stdout"), "text": c.get("text", "")})
        elif t in ("display_data", "execute_result", "update_display_data"):
            did = (c.get("transient") or {}).get("display_id")
            if t == "update_display_data" and did in self.display:
                self.display[did].update(data=c.get("data", {}), metadata=c.get("metadata", {}))
                return
            x = {"output_type": "display_data" if t == "update_display_data" else t, "data": c.get("data", {}), "metadata": c.get("metadata", {})}
            if t == "execute_result":
                x["execution_count"] = c.get("execution_count")
            if did:
                self.display[did] = x
            o.append(x)
        elif t == "error":
            o.append({"output_type": "error", "ename": c.get("ename", ""), "evalue": c.get("evalue", ""), "traceback": c.get("traceback") or []})
        elif t == "clear_output":
            del o[:]


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def outputs_text(outs, limit=4000):
    L = []
    for o in outs:
        t, d = o.get("output_type"), o.get("data") or {}
        if t == "stream":
            L.append(str(o.get("text", "")))
        elif t == "error":
            L.append(ANSI.sub("", "\n".join(o.get("traceback") or [])) or "%s: %s" % (o.get("ename"), o.get("evalue")))
        elif "text/plain" in d:
            v = d["text/plain"]
            L.append("".join(v) if isinstance(v, list) else str(v))
        elif d:
            L.append("[%s]" % ", ".join(d))
    s = "\n".join(x.rstrip("\n") for x in L).strip()
    return s if len(s) <= limit else s[:limit] + "\n…[cut]"


def frame_code(var, columns, rows):
    """Python for the kernel that builds DataFrame VAR (a list of dicts when pandas is missing). Raises ValueError."""
    if not IDENT.match(var or "") or var.startswith("__"):
        raise ValueError("%r is not a valid Python variable name." % var)
    payload = base64.b64encode(json.dumps({"columns": columns, "data": rows}, default=str).encode("utf-8")).decode()
    if len(payload) > MAX_FRAME_B64:
        raise ValueError("Too many rows to send into the kernel: lower the row limit.")
    return ("import base64 as _b, json as _j\n_d = _j.loads(_b.b64decode('%s'))\ntry:\n    import pandas as _pd\n    %s = _pd.DataFrame(_d['data'], columns=_d['columns'])\n"
            "except ImportError:\n    %s = [dict(zip(_d['columns'], _r)) for _r in _d['data']]\ndel _b, _j, _d\n") % (payload, var, var)


def sql_meta(cell):
    m = ((cell.get("metadata") or {}).get("aihub") or {}).get("sql")
    return m if isinstance(m, dict) else None


def run_sql(box, ref, sql, var="", limit=10000, c=None, kid="", deadline=None):
    """Run SQL on SQL Studio connection REF. With VAR and a kernel (C, KID) the rows become a pandas DataFrame there.
    -> {ok, columns, rows, truncated, ms, assigned: {ok, var, error?}}. Raises ValueError for setup problems."""
    cn = sqlstudio.resolve(ref)
    limit = max(1, min(int(limit or 10000), 200000))
    if var and not IDENT.match(var):
        raise ValueError("%r is not a valid Python variable name." % var)
    r = sqlstudio.call(box, cn["id"], {"action": "query", "sql": sql, "limit": limit}, 300)
    if not r.get("ok") or not var:
        return r
    r["assigned"] = {"var": var, "ok": False}
    if not r.get("columns"):
        r["assigned"]["error"] = "The statement returned no rows to assign."
        return r
    if c is None or not kid:
        r["assigned"]["error"] = "Start a kernel to assign the result to a variable."
        return r
    try:
        reply, outs = exec_wait(c, kid, frame_code(var, r["columns"], r["rows"]), deadline or time.time() + 120, silent=True)
        err = next((o for o in outs if o.get("output_type") == "error"), None)
        r["assigned"].update(ok=reply.get("status") == "ok" and not err, error=("%s: %s" % (err["ename"], err["evalue"])) if err else "")
    except (JError, ValueError) as e:
        r["assigned"]["error"] = str(e)
    return r


def exec_wait(c, kid, code, deadline, expr=None, silent=False, stop=None):
    """Run CODE and wait for it. -> (execute_reply content, nbformat outputs). Raises JError (timeout, kernel gone, stopped)."""
    ch = channel(c, "k", kid)
    pos = ch.seq
    mid = execute(c, kid, code, stdin=False, expr=expr, silent=silent)
    fold, reply, idle, grace = Fold(), None, False, None
    while not (reply is not None and idle):
        if stop and stop():
            kernel_op(c, kid, "interrupt")
            raise JError("Stopped.", 400)
        left = deadline - time.time()
        if left <= 0:
            kernel_op(c, kid, "interrupt")
            raise JError("Timed out: the cell was interrupted.", 400)
        if grace and time.time() > grace:
            break
        items, pos, closed = ch.read(pos, min(left, 1.5))
        for _, e in items:
            if e["p"] != mid:
                continue
            if e["t"] == "execute_reply":
                reply, grace = e["c"], time.time() + 3
            elif e["t"] == "status":
                idle = idle or e["c"].get("execution_state") == "idle"
            else:
                fold.add(e)
        if closed and reply is None:
            raise JError("The kernel connection closed (did the kernel die?).")
    return reply or {}, fold.outputs


def param_code(params):
    L = []
    for k, v in (params or {}).items():
        if not IDENT.match(str(k)):
            raise ValueError("Parameter name %r is not a Python identifier." % k)
        L.append("%s = %s" % (k, repr(v)))
    return "\n".join(L) + "\n"


def detect_params(nb):
    """Parameters of a notebook (papermill style): `name = literal` lines of the cell tagged `parameters`, else of the first code cell. -> [{name, default, type}]."""
    cells = [x for x in nb.get("cells") or [] if x.get("cell_type") == "code"]
    tagged = [x for x in cells if "parameters" in ((x.get("metadata") or {}).get("tags") or [])]
    cell = (tagged or cells[:1] or [None])[0]
    out = []
    for ln in "".join((cell or {}).get("source") if isinstance((cell or {}).get("source"), list) else [(cell or {}).get("source") or ""]).splitlines():
        m = re.match(r"^([A-Za-z_]\w*)\s*=\s*(.+?)\s*(?:#.*)?$", ln)
        if m:
            try:
                v = ast.literal_eval(m.group(2))
            except (ValueError, SyntaxError):
                continue
            out.append({"name": m.group(1), "default": v, "type": "number" if isinstance(v, (int, float)) and not isinstance(v, bool) else "checkbox" if isinstance(v, bool) else "text"})
    return out[:30]


def _src(cell):
    s = cell.get("source")
    return "".join(s) if isinstance(s, list) else str(s or "")


def run_notebook(box, ref, path, params=None, kernel="", timeout=600, keep_going=False, save_as="", result_var="result", progress=None, stop=None):
    """Run every code cell of a notebook on a fresh kernel of the server. -> {ok, cells: [{i, kind, ok, text}], text, result, saved, seconds, error}."""
    t0, deadline = time.time(), time.time() + max(5, int(timeout))
    c = conn(box, ref)
    f = http(c, "GET", "contents/%s?content=1&type=notebook" % _q(path), timeout=60)
    nb = f.get("content")
    if not isinstance(nb, dict):
        raise JError("%s is not a notebook." % path, 400)
    cells = nb.get("cells") or []
    spec = kernel or ((nb.get("metadata") or {}).get("kernelspec") or {}).get("name") or get(ref).get("kernel") or ""
    k = http(c, "POST", "kernels", {"name": spec} if spec else {})
    kid, report, ok, err, count = k["id"], [], True, "", 0
    inject = param_code(params) if params else ""
    tagged = any("parameters" in ((x.get("metadata") or {}).get("tags") or []) for x in cells)
    try:
        if inject and not tagged:
            exec_wait(c, kid, inject, deadline, silent=True, stop=stop)
            inject = ""
        todo = [i for i, x in enumerate(cells) if x.get("cell_type") == "code" and _src(x).strip()]
        for n, i in enumerate(todo):
            cell, sm = cells[i], sql_meta(cells[i])
            if progress:
                progress("cell %d/%d" % (n + 1, len(todo)))
            if stop and stop():
                raise JError("Stopped.", 400)
            count += 1
            row = {"i": i, "kind": "sql" if sm else "code", "ok": True, "text": ""}
            try:
                if sm:
                    r = run_sql(box, sm.get("conn") or "", _src(cell), sm.get("var") or "", sm.get("limit") or 10000, c, kid, deadline)
                    if not r.get("ok"):
                        raise JError(str(r.get("error") or "SQL failed"), 400)
                    a = r.get("assigned") or {}
                    if a and not a.get("ok"):
                        raise JError("Assigning %s failed: %s" % (a.get("var"), a.get("error")), 400)
                    cols, rows = r.get("columns") or [], r.get("rows") or []
                    row["text"] = " | ".join(cols) + "\n" + "\n".join(" | ".join("NULL" if v is None else str(v)[:60] for v in x) for x in rows[:20]) + ("\n(%d rows%s)" % (len(rows), ", assigned to " + a["var"] if a else ""))
                    cell["outputs"] = [{"output_type": "display_data", "data": {"text/plain": row["text"]}, "metadata": {}}]
                else:
                    reply, outs = exec_wait(c, kid, _src(cell), deadline, stop=stop)
                    cell["outputs"], cell["execution_count"] = outs, reply.get("execution_count")
                    row["text"] = outputs_text(outs)
                    if reply.get("status") == "error" or any(o.get("output_type") == "error" for o in outs):
                        row["ok"] = False
            except JError as e:
                row.update(ok=False, text=str(e))
            if "parameters" in ((cell.get("metadata") or {}).get("tags") or []) and inject:
                exec_wait(c, kid, inject, deadline, silent=True, stop=stop)
                inject = ""
            report.append(row)
            if not row["ok"]:
                ok, err = False, err or "cell %d failed: %s" % (i + 1, row["text"].splitlines()[-1][:300] if row["text"] else "error")
                if not keep_going:
                    break
        result = None
        if ok and result_var and IDENT.match(result_var):
            reply, _ = exec_wait(c, kid, "", deadline, expr={"r": "__import__('json').dumps(%s, default=str)" % result_var}, silent=True)
            ur = ((reply.get("user_expressions") or {}).get("r") or {})
            if ur.get("status") == "ok":
                try:
                    result = json.loads(ast.literal_eval((ur.get("data") or {}).get("text/plain", "''")))
                except (ValueError, SyntaxError):
                    result = None
        saved = ""
        if save_as:
            nb2 = dict(nb)
            nb2["cells"] = cells
            write_file(c, save_as, "notebook", nb2)
            saved = save_as
    finally:
        try:
            drop_channel(c, "k", kid)
            http(c, "DELETE", "kernels/" + urllib.parse.quote(kid))
        except JError:
            pass
    text = "\n\n".join("[cell %d%s] %s" % (r["i"] + 1, "" if r["ok"] else " FAILED", r["text"]) for r in report if r["text"])
    return {"ok": ok, "error": err, "cells": report, "text": text, "last": next((r["text"] for r in reversed(report) if r["text"]), ""), "result": result,
            "saved": saved, "seconds": round(time.time() - t0, 1), "ran": count}


# ---------- background runs (the node, chat and CLI start one and poll it)

JOBS = {}


def run_start(box, **kw):
    jid = uuid.uuid4().hex[:10]
    job = {"id": jid, "state": "running", "lines": [], "result": None, "error": "", "started": time.time(), "stop": False}
    with _lock:
        for k in [k for k, j in JOBS.items() if j["state"] != "running" and time.time() - j["started"] > 3600]:
            JOBS.pop(k, None)
        JOBS[jid] = job

    def go():
        try:
            job["result"] = run_notebook(box, progress=lambda s: job["lines"].append(s), stop=lambda: job["stop"], **kw)
            job["state"] = "done"
        except (JError, ValueError) as e:
            job["error"], job["state"] = str(e), "error"
        except Exception as e:
            job["error"], job["state"] = "%s: %s" % (type(e).__name__, e), "error"
    threading.Thread(target=go, daemon=True).start()
    return jid


def run_view(jid, after=0):
    j = JOBS.get(jid)
    if not j:
        return None
    return {"id": jid, "state": j["state"], "lines": j["lines"][int(after or 0):], "next": len(j["lines"]), "result": j["result"], "error": j["error"]}


def run_stop(jid):
    j = JOBS.get(jid)
    if j and j["state"] == "running":
        j["stop"] = True
        return True
    return False


# ---------- a notebook as a Works node

def expose(box, ref, path, title=""):
    """Save a work (switched off): Start manual (one input per notebook parameter) -> Jupyter notebook node. -> {id, params}."""
    rec = get(ref)
    c = conn(box, rec["id"])
    nb = read_file(c, path).get("content")
    params = detect_params(nb if isinstance(nb, dict) else {})
    lines, body = [], []
    for p in params:
        d = p["default"]
        d = str(d).lower() if isinstance(d, bool) else ("" if d is None else str(d)).replace("|", "/").replace("\n", " ")[:80]
        lines.append("%s|%s|%s|%s" % (p["name"], p["name"].replace("_", " ").capitalize(), p["type"], d))
        body.append('"%s": %s' % (p["name"], "{{data.%s}}" % p["name"] if p["type"] in ("number", "checkbox") else '"{{data.%s}}"' % p["name"]))
    name = title or os.path.basename(path).rsplit(".", 1)[0]
    w = {"title": name[:80], "description": "Runs the notebook %s on Jupyter server %s." % (path, rec["name"]), "enabled": False,
         "nodes": [{"id": "a", "type": "start.manual", "x": 40, "y": 120, "label": "Start", "config": {"inputs": "\n".join(lines)}},
                   {"id": "b", "type": "code.notebook", "x": 300, "y": 120, "label": name[:40],
                    "config": {"server": rec["name"], "path": path, "params": "{%s}" % ", ".join(body) if body else "", "kernel": "", "result_var": "result", "save_as": "", "continue": False, "timeout": 600}}],
         "edges": [{"from": "a", "to": "b"}]}
    w = store.save_work(store.clean_work(w))
    return {"id": w["id"], "params": params}
