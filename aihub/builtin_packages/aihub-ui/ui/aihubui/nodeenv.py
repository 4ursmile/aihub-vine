"""Python environments of code nodes (Python, SQL, Spark and package nodes with an `env` field): which interpreter a node runs with,
what it needs for its settings and code, what is missing, and installing it (one click in the Inspector, `aihub work env`, or the one
pip command shown next to it).

An environment value is "" (the UI's own Python; Spark: $PYSPARK_PYTHON first), the NAME of a managed env (~/.aihub/ui/envs/<name>,
created on first install), or a path to a python / a venv folder. Requirements come from the node: SQL = sqlalchemy + the driver
of the chosen database + pandas / polars when picked; Spark = pyspark (+ Java on the system); any code field = its imports (stdlib and
the node's own helpers skipped); a package node's `packages` list. Checks run a tiny probe IN that Python (nothing is imported, only
found) and are cached for a few seconds; installs run `python -m pip install` (or `uv pip` when the env has no pip) as a background job
whose log the page polls. Only package specs pass (no options, URLs or paths)."""
import ast
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from . import store

LIB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
if LIB_DIR not in sys.path:
    sys.path.append(LIB_DIR)
import aihub_data  # noqa: E402  (the driver table, shared with the node programs)

ENV_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")
PIP_SPEC = store.REQ_SPEC                                           # a package spec: name, optional [extra], optional version
PYTHON_NAME = re.compile(r"^(python|pypy)[0-9.]*(w)?(\.exe)?$", re.I)
OWN = {"aihub_vault", "aihub_data", "_steps", "__future__"}         # importable in every node run: never "missing"
CHECK_TTL = 15
JOB_KEEP = 3600
LOG_LINES = 3000
FRAME_NOTE = {"pandas": "reads and writes with pandas", "polars": "reads and writes with polars"}
STDLIB = set(getattr(sys, "stdlib_module_names", ())) or {
    "abc", "argparse", "array", "ast", "asyncio", "base64", "bisect", "calendar", "collections", "concurrent", "contextlib", "copy", "csv", "ctypes",
    "dataclasses", "datetime", "decimal", "difflib", "email", "enum", "functools", "gc", "getpass", "glob", "gzip", "hashlib", "heapq", "hmac", "html",
    "http", "importlib", "inspect", "io", "ipaddress", "itertools", "json", "logging", "lzma", "math", "mimetypes", "multiprocessing", "operator", "os",
    "pathlib", "pickle", "platform", "pprint", "queue", "random", "re", "secrets", "shlex", "shutil", "signal", "socket", "sqlite3", "ssl", "statistics",
    "string", "struct", "subprocess", "sys", "tempfile", "textwrap", "threading", "time", "timeit", "traceback", "types", "typing", "unicodedata",
    "unittest", "urllib", "uuid", "warnings", "weakref", "xml", "zipfile", "zlib", "zoneinfo"}

PROBE = r"""
import json, sys
pairs = json.loads(sys.argv[1])
out = {"python": sys.executable, "version": "%d.%d.%d" % tuple(sys.version_info[:3]), "prefix": sys.prefix,
       "venv": sys.prefix != getattr(sys, "base_prefix", sys.prefix), "mods": {}, "managed": False}
try:
    import importlib.util as u
except ImportError:
    u = None
try:
    from importlib import metadata as md
except ImportError:
    md = None
try:
    import os, sysconfig
    out["managed"] = (not out["venv"]) and os.path.exists(os.path.join(sysconfig.get_paths()["stdlib"], "EXTERNALLY-MANAGED"))
except Exception:
    pass
for mod, dist in pairs:
    found = False
    try:
        found = u is not None and u.find_spec(mod) is not None
    except Exception:
        found = False
    v = None
    if found:
        v = ""
        if md is not None:
            for name in (dist, mod):
                try:
                    v = md.version(name)
                    break
                except Exception:
                    pass
    out["mods"][mod] = v
try:
    out["pip"] = u is not None and u.find_spec("pip") is not None
except Exception:
    out["pip"] = False
print(json.dumps(out))
"""


def envs_dir(*a):
    """~/.aihub/ui/envs[/name]: never created by looking (venv makes the env folder on the first install)."""
    return os.path.join(store.ui_dir(), "envs", *a)


def _venv_python(d):
    return os.path.join(d, "Scripts", "python.exe") if os.name == "nt" else os.path.join(d, "bin", "python")


def managed(name):
    """True when NAME is (or would be) a managed env, not a program on PATH such as python3."""
    return bool(ENV_NAME.match(name)) and (os.path.isdir(envs_dir(name)) or not shutil.which(name))


def resolve(value, spark=False):
    """Environment value -> (python path, managed env name or ""). The path may not exist yet (a managed env before its first install)."""
    v = str(value or "").strip()
    if not v:
        return (os.environ.get("PYSPARK_PYTHON") if spark else "") or sys.executable, ""
    if managed(v):
        return _venv_python(envs_dir(v)), v
    p = os.path.expanduser(v)
    if os.path.isdir(p):
        return _venv_python(p), ""
    w = shutil.which(p) if os.sep not in p and "/" not in p else None
    return w or p, ""


def interpreter(value, spark=False):
    """The python a node runs with, checked. Raises ValueError with a message for the run log."""
    py, name = resolve(value, spark)
    if not os.path.isfile(py):
        if name:
            raise ValueError("the environment %r is not created yet: open the node's Environment box and press Install (or: aihub work env install --env %s ...)" % (name, name))
        raise ValueError("no Python at %s (the node's Environment field)" % py)
    if not PYTHON_NAME.match(os.path.basename(py)) and not os.path.basename(os.path.realpath(py)).lower().startswith(("python", "pypy")):
        raise ValueError("%s does not look like a Python interpreter" % py)
    return py


def envs():
    """The managed envs: [{name, python, exists}]."""
    out = []
    try:
        names = sorted(os.listdir(envs_dir()))
    except OSError:
        names = []
    for n in names:
        if ENV_NAME.match(n) and os.path.isdir(envs_dir(n)):
            py = _venv_python(envs_dir(n))
            out.append({"name": n, "python": py, "exists": os.path.isfile(py)})
    return out


# ---------- what a node needs

def _dist(pip):
    return re.split(r"[\[<>=!~ ]", pip, 1)[0]


def code_imports(code):
    """Top level modules a piece of Python imports (absolute imports only). Falls back to a line scan when the code does not parse."""
    mods = []
    try:
        for n in ast.walk(ast.parse(str(code or ""))):
            if isinstance(n, ast.Import):
                mods += [a.name.split(".")[0] for a in n.names]
            elif isinstance(n, ast.ImportFrom) and not n.level and n.module:
                mods.append(n.module.split(".")[0])
    except (SyntaxError, ValueError):
        mods = re.findall(r"^\s*(?:from|import)\s+([A-Za-z_]\w*)", str(code or ""), re.M)
    seen = []
    for m in mods:
        if m not in seen and m not in STDLIB and m not in OWN and not m.startswith("_"):
            seen.append(m)
    return seen


def code_field(spec):
    return next((f["name"] for f in spec.get("fields") or [] if f.get("type") == "code" and (f.get("lang") or "python") == "python"), "")


def env_field(spec):
    return spec.get("env") or next((f["name"] for f in spec.get("fields") or [] if f.get("type") == "env"), "")


def requirements(spec, cfg, extra=None):
    """[{module, pip, why, optional}] of a node with these settings. EXTRA = the node's own `requires` (a shared work carries them)."""
    cfg, t, req = cfg or {}, spec.get("type"), []

    def add(mod, why, pip=None, optional=False):
        if mod and not any(r["module"] == mod for r in req):
            req.append({"module": mod, "pip": pip or aihub_data.PIP.get(mod, mod), "why": why, "optional": optional})
    if t == "db.sql":
        add("sqlalchemy", "runs the SQL", aihub_data.SQLALCHEMY)
        url = str(cfg.get("url") or "")
        drv = aihub_data.driver_of(url.split("://")[0]) if "://" in url else str(cfg.get("driver") or "postgresql")
        d = aihub_data.DRIVERS.get(drv) or {}
        if d.get("module"):
            add(d["module"], "%s driver" % d["title"], d["pip"])
        fr = str(cfg.get("frame") or "native")
        if fr in FRAME_NOTE:
            add(fr, FRAME_NOTE[fr])
        if fr == "polars" and str(cfg.get("write_table") or "").strip():
            add("pandas", "polars writes tables through pandas")
            add("pyarrow", "polars writes tables through pyarrow")
        if str(cfg.get("output") or "") == "path" and str(cfg.get("file_format") or "") == "parquet" and fr != "polars":
            add("pyarrow", "parquet files")
    elif t == "etl.spark":
        add("pyspark", "runs the Spark job")
    elif t == "code.python" and "pydantic" in str(cfg.get("code") or ""):
        add("pydantic", "typed argument / return")
    for p in list(spec.get("packages") or []) + list(extra or []):   # a package node.json: "packages": ["pandas>=2", ...]; a shared node: `requires`
        if isinstance(p, str) and PIP_SPEC.match(p.strip()):
            mod = next((m for m, pp in aihub_data.PIP.items() if _dist(pp).lower() == _dist(p).lower()), _dist(p).replace("-", "_").lower())
            add(mod, "the node needs it", p.strip())
    cf = code_field(spec)
    if cf:
        for m in code_imports(cfg.get(cf)):
            add(m, "imported by the code")
    return req


# ---------- checking

_cache, _clock = {}, threading.Lock()


def probe(py, req, fresh=False):
    """Run PROBE in PY. -> dict, or {"error": ...}."""
    key = (py, tuple(sorted(r["module"] for r in req)))
    with _clock:
        hit = _cache.get(key)
    if hit and not fresh and time.time() - hit[0] < CHECK_TTL:
        return hit[1]
    pairs = [[r["module"], _dist(r["pip"])] for r in req]
    try:
        p = subprocess.run([py, "-I", "-c", PROBE, json.dumps(pairs)], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        res = json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 and p.stdout.strip() else {"error": (p.stderr.strip() or "exit %s" % p.returncode)[-400:]}
    except subprocess.TimeoutExpired:
        res = {"error": "the Python did not answer within 30 s"}
    except (OSError, ValueError, IndexError) as e:
        res = {"error": "cannot run %s: %s" % (py, e)}
    with _clock:
        _cache[key] = (time.time(), res)
    return res


def forget():
    with _clock:
        _cache.clear()


def java():
    """Spark needs a Java runtime: {ok, path, version, hint}."""
    home = os.environ.get("JAVA_HOME")
    cand = os.path.join(home, "bin", "java.exe" if os.name == "nt" else "java") if home else ""
    path = cand if cand and os.path.isfile(cand) else shutil.which("java") or ""
    hint = ("brew install openjdk@17" if sys.platform == "darwin" else "winget install Microsoft.OpenJDK.17" if os.name == "nt"
            else "sudo apt install openjdk-17-jre-headless   (or your system's package for Java 17)")
    if not path:
        return {"ok": False, "path": "", "version": "", "hint": hint}
    try:
        p = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
        txt = (p.stderr or p.stdout or "").strip()
        m = re.search(r'version "([^"]+)"', txt) or re.search(r"\b(\d+(?:\.\d+)*)\b", txt)
        ver = m.group(1) if m else ""
    except (OSError, subprocess.TimeoutExpired):
        return {"ok": False, "path": path, "version": "", "hint": hint}
    if sys.platform == "darwin" and not ver:                          # the /usr/bin/java stub without a JDK installed
        return {"ok": False, "path": path, "version": "", "hint": hint}
    return {"ok": True, "path": path, "version": ver, "hint": ""}


def command(value, pips, spark=False):
    """The one shell line that sets the env up (create it first when it is a managed env that does not exist)."""
    py, name = resolve(value, spark)
    q = subprocess.list2cmdline if os.name == "nt" else (lambda a: " ".join(shlex.quote(x) for x in a))
    pre = ""
    if name and not os.path.isfile(py):
        pre = q([sys.executable, "-m", "venv", envs_dir(name)]) + (" && " if os.name != "nt" else " & ")
    return pre + q([py, "-m", "pip", "install"] + list(pips)) if pips else pre.rstrip(" &")


def check(spec, cfg, fresh=False, extra=None):
    """What the node needs and what is there. -> {env, python, name, exists, version, venv, managed_python, pip, packages, missing, command, ok,
    java (Spark), error}."""
    cfg = cfg or {}
    spark = spec.get("type") == "etl.spark"
    ef = env_field(spec)
    value = str(cfg.get(ef) or "") if ef else ""
    py, name = resolve(value, spark)
    req = requirements(spec, cfg, extra)
    out = {"env": value, "field": ef, "python": py, "name": name, "exists": os.path.isfile(py), "packages": [], "missing": [], "ok": False, "error": ""}
    if spark:
        out["java"] = java()
    if not out["exists"]:
        out["missing"] = [r["pip"] for r in req if not r["optional"]]
        out["packages"] = [dict(r, installed=False, version=None) for r in req]
        out["error"] = "" if name else "no Python at %s" % py
        out["command"] = command(value, out["missing"], spark)
        return out
    res = probe(py, req, fresh)
    if res.get("error"):
        out["error"] = res["error"]
        return out
    mods = res.get("mods") or {}
    for r in req:
        v = mods.get(r["module"])
        out["packages"].append(dict(r, installed=v is not None, version=v or ""))
    out.update(version=res.get("version", ""), venv=bool(res.get("venv")), managed_python=bool(res.get("managed")), pip=bool(res.get("pip")))
    out["missing"] = [p["pip"] for p in out["packages"] if not p["installed"] and not p["optional"]]
    out["command"] = command(value, out["missing"], spark)
    out["ok"] = not out["missing"] and (not spark or out["java"]["ok"])
    return out


def has_env(spec):
    return bool(spec) and bool(env_field(spec) or spec.get("type") in ("db.sql", "etl.spark", "code.python"))


def check_node(n, spec, fresh=False):
    """check() of a work's node (its config + its `requires`)."""
    return check(spec, n.get("config") or {}, fresh, n.get("requires"))


def shared_requires(n, spec):
    """The pip specs a node needs, written into a shared work (never versions of this machine: the installer's pip picks)."""
    return [r["pip"] for r in requirements(spec, n.get("config") or {}, n.get("requires")) if not r["optional"]]


# ---------- installing (background jobs)

JOBS = {}
_jlock = threading.Lock()


def _valid(pips):
    out = []
    for p in pips or []:
        p = str(p or "").strip()
        if not p:
            continue
        if not PIP_SPEC.match(p):
            raise ValueError("not a package name: %r (only names with an optional [extra] and version, e.g. polars or psycopg[binary]>=3)" % p[:80])
        out.append(p)
    if len(out) > 40:
        raise ValueError("too many packages at once (40 at most)")
    return out


def install(value, pips, spark=False):
    """Start installing PIPS into the env VALUE (a managed env is created first). -> the job {id, state, ...}. Raises ValueError."""
    pips = _valid(pips)
    py, name = resolve(value, spark)
    if not name:
        interpreter(value, spark)                                    # an existing python only; a managed env may be made below
    if not pips and not (name and not os.path.isfile(py)):
        raise ValueError("nothing to install")
    job = {"id": uuid.uuid4().hex[:12], "state": "running", "env": value, "python": py, "packages": pips, "lines": [], "started": time.time(),
           "command": command(value, pips, spark), "error": "", "proc": None, "cancel": False}
    with _jlock:
        for k in [k for k, j in JOBS.items() if j["state"] != "running" and time.time() - j["started"] > JOB_KEEP]:
            JOBS.pop(k, None)
        if any(j["state"] == "running" and j["python"] == py for j in JOBS.values()):
            raise ValueError("an install into this environment is already running")
        JOBS[job["id"]] = job
    threading.Thread(target=_run_job, args=(job, name), daemon=True).start()
    return public(job)


def _say(job, line):
    with _jlock:
        job["lines"].append(line.rstrip("\n"))
        drop = len(job["lines"]) - LOG_LINES
        if drop > 0:                                                 # offsets stay logical: `base` lines were dropped from the front
            del job["lines"][:drop]
            job["base"] = job.get("base", 0) + drop


def _stream(job, argv):
    _say(job, "$ " + (subprocess.list2cmdline(argv) if os.name == "nt" else " ".join(shlex.quote(a) for a in argv)))
    env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONUNBUFFERED="1", PIP_NO_INPUT="1")
    kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)} if os.name == "nt" else {"start_new_session": True}
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=env, **kw)
    job["proc"] = p
    for ln in p.stdout:
        _say(job, ln)
    return p.wait()


def _run_job(job, name):
    py = job["python"]
    try:
        if name and not os.path.isfile(py):
            _say(job, "creating the environment %s in %s" % (name, envs_dir(name)))
            if _stream(job, [sys.executable, "-m", "venv", envs_dir(name)]) != 0 or not os.path.isfile(py):
                raise RuntimeError("could not create the environment (this Python may lack the venv module: install python3-venv, or use uv)")
        if job["cancel"]:
            raise RuntimeError("cancelled")
        if job["packages"]:
            has_pip = probe(py, [], fresh=True).get("pip")
            uv = shutil.which("uv")
            if not has_pip and uv:
                argv = [uv, "pip", "install", "--python", py] + job["packages"]
            else:
                if not has_pip:
                    _stream(job, [py, "-m", "ensurepip", "--upgrade"])
                argv = [py, "-m", "pip", "install"] + job["packages"]
            rc = _stream(job, argv)
            if job["cancel"]:
                raise RuntimeError("cancelled")
            if rc != 0:
                tail = "\n".join(job["lines"][-30:]).lower()
                if "externally-managed-environment" in tail or "externally managed" in tail:
                    raise RuntimeError("this Python is managed by the system and refuses pip installs: pick a separate environment "
                                       "(Environment > New environment, e.g. 'data') and install there")
                raise RuntimeError("pip failed (exit %s); see the log" % rc)
        job["state"] = "done"
        _say(job, "done")
    except Exception as e:
        job["state"], job["error"] = "failed", str(e)
        _say(job, "failed: %s" % e)
    finally:
        job["ended"] = time.time()
        job["proc"] = None
        forget()


def public(job):
    return {k: v for k, v in job.items() if k not in ("proc", "lines", "cancel")}


def job_view(jid, after=0):
    with _jlock:
        j = JOBS.get(jid)
        if not j:
            return None
        base = j.get("base", 0)
        lines = j["lines"][max(0, int(after or 0) - base):]
        return dict(public(j), lines=lines, next=base + len(j["lines"]))


def cancel(jid):
    j = JOBS.get(jid)
    if not j or j["state"] != "running":
        return False
    j["cancel"] = True
    p = j.get("proc")
    if p and p.poll() is None:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, timeout=10)
            else:
                os.killpg(p.pid, signal.SIGTERM)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return True


def wait(jid, timeout=1800, on_line=None):
    """Block until the job ends (the CLI). ON_LINE gets each new log line. -> the final job view."""
    after, end = 0, time.time() + timeout
    while True:
        v = job_view(jid, after)
        if v is None:
            return None
        for ln in v["lines"]:
            if on_line:
                on_line(ln)
        after = v["next"]
        if v["state"] != "running" or time.time() > end:
            return v
        time.sleep(0.3)
