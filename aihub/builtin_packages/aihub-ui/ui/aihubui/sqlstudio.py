"""SQL Studio: saved connections, the driver catalog and running statements (in a child Python, see sqlrunner.py).

Connections live in ~/.aihub/ui/sqlstudio.json WITHOUT secrets; the password / full URL sits in the encrypted box under `sqlconn:<id>`.
A connection is {id, name, driver, host, port, user, database, options, url_masked, env, readonly}. `env` is a Python environment as in
nodeenv ("" = the UI's Python, a managed env name, or a path): drivers are installed there from the Drivers tab."""
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid

from . import nodeenv, store

RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sqlrunner.py")
LOGIN = ("driver", "host", "port", "user", "database", "options")
SECRET_PREFIX = "sqlconn:"
ID = re.compile(r"^[0-9a-f]{8}$")
MAX_OUT = 64 * 1024 * 1024
_lock = threading.Lock()
RUNS = {}                                  # run id -> Popen, for Cancel

# drivers beyond the ones the node programs know (aihub_data.DRIVERS): a SQLAlchemy URL template plus what to pip install
EXTRA = {
    "clickhouse": {"title": "ClickHouse", "module": "clickhouse_sqlalchemy", "pip": ["clickhouse-sqlalchemy"], "extra_mods": [["clickhouse_driver", "clickhouse-driver"]],
                   "url": "clickhouse+native://user:password@host:9000/database"},
    "snowflake": {"title": "Snowflake", "module": "snowflake.sqlalchemy", "pip": ["snowflake-sqlalchemy"],
                  "url": "snowflake://user:password@account/database/schema?warehouse=WH&role=ROLE"},
    "bigquery": {"title": "Google BigQuery", "module": "sqlalchemy_bigquery", "pip": ["sqlalchemy-bigquery"],
                 "url": "bigquery://project/dataset?credentials_path=/path/key.json"},
    "redshift": {"title": "Amazon Redshift", "module": "sqlalchemy_redshift", "pip": ["sqlalchemy-redshift", "redshift_connector"], "extra_mods": [["redshift_connector", "redshift_connector"]],
                 "url": "redshift+redshift_connector://user:password@cluster.region.redshift.amazonaws.com:5439/dev"},
    "databricks": {"title": "Databricks SQL", "module": "databricks.sql", "pip": ["databricks-sql-connector", "databricks-sqlalchemy"],
                   "url": "databricks://token:TOKEN@host?http_path=/sql/1.0/warehouses/ID&catalog=main&schema=default"},
    "cockroachdb": {"title": "CockroachDB", "module": "sqlalchemy_cockroachdb", "pip": ["sqlalchemy-cockroachdb", "psycopg[binary]"],
                    "url": "cockroachdb+psycopg://user:password@host:26257/defaultdb"},
    "hana": {"title": "SAP HANA", "module": "sqlalchemy_hana", "pip": ["sqlalchemy-hana"], "url": "hana://user:password@host:39015"},
    "db2": {"title": "IBM Db2", "module": "ibm_db_sa", "pip": ["ibm_db_sa"], "url": "db2+ibm_db://user:password@host:50000/database"},
    "teradata": {"title": "Teradata", "module": "teradatasqlalchemy", "pip": ["teradatasqlalchemy"], "url": "teradatasql://user:password@host"},
    "mongo": None,
}
EXTRA.pop("mongo")


def _data():
    import aihub_data                                     # nodeenv put the lib folder on sys.path
    return aihub_data


def _path():
    return store.ui_dir("sqlstudio.json")


def _load():
    d = store._read(_path(), {})
    return d if isinstance(d, dict) and isinstance(d.get("connections"), list) else {"connections": []}


def _save(d):
    store._write(_path(), d)


def _mask(url):
    return re.sub(r"(://[^:/@\s]+:)[^@/\s]+@", r"\1•••@", url or "")


def _public(c):
    r = {k: c.get(k, "") for k in ("id", "name", "driver", "host", "port", "user", "database", "options", "env")}
    r["readonly"] = bool(c.get("readonly"))
    r["url_masked"] = c.get("url_masked") or ""
    r["has_secret"] = bool(c.get("has_secret"))
    return r


def connections():
    return [_public(c) for c in _load()["connections"]]


def get(cid):
    return next((c for c in _load()["connections"] if c.get("id") == cid), None)


def save(box, b):
    """Create or update a connection. A blank `password` / `url` keeps the stored secret. -> the public record. Raises ValueError."""
    name = str(b.get("name") or "").strip()[:60]
    if not name:
        raise ValueError("Give the connection a name.")
    driver = str(b.get("driver") or "sqlite").strip()
    ad = _data()
    if driver != "other" and driver not in ad.DRIVERS:
        raise ValueError("Unknown driver %r." % driver)
    url = str(b.get("url") or "").strip()
    if driver == "other" and "://" not in url and not (b.get("id") and get(str(b["id"]))):
        raise ValueError("Paste a SQLAlchemy URL such as postgresql+psycopg://user:password@host/db.")
    env = str(b.get("env") or "").strip()
    if env:
        try:
            nodeenv.resolve(env)
        except Exception as e:
            raise ValueError(str(e))
    cid = str(b.get("id") or "")
    with _lock:
        d = _load()
        cur = next((c for c in d["connections"] if c.get("id") == cid), None) if cid else None
        if cid and not cur:
            raise ValueError("That connection no longer exists.")
        if not cur:
            cur = {"id": uuid.uuid4().hex[:8]}
            d["connections"].append(cur)
        cur.update(name=name, driver=driver, env=env, readonly=bool(b.get("readonly")))
        for k in ("host", "port", "user", "database", "options"):
            cur[k] = str(b.get(k) or "").strip()[:300]
        sec = _secret(box, cur["id"])
        pw = str(b.get("password") or "")
        if driver == "other":
            if url:
                sec["url"] = url
                cur["url_masked"] = _mask(url)
            sec.pop("password", None)
        else:
            sec.pop("url", None)
            cur["url_masked"] = ""
            if pw:
                sec["password"] = pw
            if b.get("clear_password"):
                sec.pop("password", None)
        cur["has_secret"] = bool(sec.get("password") or sec.get("url"))
        if sec:
            box.put(SECRET_PREFIX + cur["id"], json.dumps(sec))
        else:
            box.remove(SECRET_PREFIX + cur["id"])
        _save(d)
        return _public(cur)


def delete(box, cid):
    with _lock:
        d = _load()
        n = len(d["connections"])
        d["connections"] = [c for c in d["connections"] if c.get("id") != cid]
        if len(d["connections"]) == n:
            return False
        _save(d)
    box.remove(SECRET_PREFIX + cid)
    return True


def _secret(box, cid):
    try:
        return json.loads(box.get_all().get(SECRET_PREFIX + cid) or "{}")
    except ValueError:
        return {}


def conn_fields(box, c):
    """The full login (with the secret) for the runner. Never leaves the server."""
    f = {k: c.get(k, "") for k in LOGIN}
    f.update(_secret(box, c["id"]))
    if f.get("driver") == "other":
        f.pop("host", None)
    return f


# ---------- running

def _python(env):
    return nodeenv.interpreter(env)


def call(box, cid, req, timeout=120, rid=None):
    """Run one runner action for saved connection CID (or an unsaved login in req['conn_fields']). -> dict. Raises ValueError for setup problems."""
    c = get(cid) if cid else None
    if c is None and not req.get("login"):
        raise ValueError("Pick a connection first.")
    login = req.pop("login", None)
    env = str((login or c or {}).get("env") or "")
    if login:                                                       # a draft being tested: its password may be blank = the stored one
        f = {k: str(login.get(k) or "") for k in LOGIN}
        if str(login.get("driver")) == "other":
            f["url"] = str(login.get("url") or "")
        else:
            f["password"] = str(login.get("password") or "")
        if login.get("id") and get(str(login["id"])):
            old = _secret(box, str(login["id"]))
            if not f.get("password") and old.get("password") and f.get("driver") != "other":
                f["password"] = old["password"]
            if not f.get("url") and old.get("url"):
                f["url"] = old["url"]
        fields = f
    else:
        fields = conn_fields(box, c)
        req["readonly"] = bool(c.get("readonly")) if req.get("action") == "query" else False
    try:
        py = _python(env)
    except ValueError as e:                                         # a managed env that is not created yet: installing creates it
        drv = (_data().DRIVERS.get(str(fields.get("driver") or "")) or {}).get("pip")
        pips = [_data().SQLALCHEMY] + ([drv] if drv else [])
        return {"ok": False, "error": str(e).split(":")[0] + ".", "missing": pips, "command": _data().install_command(pips)}
    req["conn"] = fields
    p = subprocess.Popen([py, "-I", RUNNER], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                         errors="replace", cwd=store.ui_dir())
    if rid:
        with _lock:
            RUNS[rid] = p
    box_out = {}

    def feed():
        try:
            box_out["o"], box_out["e"] = p.communicate(json.dumps(req), timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
            box_out["timeout"] = True
        except Exception as e:
            box_out["err"] = str(e)
    th = threading.Thread(target=feed, daemon=True)
    th.start()
    th.join()
    if rid:
        with _lock:
            RUNS.pop(rid, None)
    if box_out.get("timeout"):
        return {"ok": False, "error": "Timed out after %d s. The query was stopped." % timeout}
    if p.returncode in (-9, -15):
        return {"ok": False, "error": "Stopped.", "stopped": True}
    text = (box_out.get("o") or "").strip()
    try:
        return json.loads(text.splitlines()[-1])
    except (ValueError, IndexError):
        err = (box_out.get("e") or box_out.get("err") or text or "the runner gave no answer").strip()
        return {"ok": False, "error": err[-800:]}


def cancel(rid):
    with _lock:
        p = RUNS.get(rid)
    if p and p.poll() is None:
        p.terminate()
        return True
    return False


# ---------- drivers

def catalog():
    """Every database we can talk to: [{key, title, pips, module, mods, note, url}]. Modules are probed per env by drivers()."""
    ad = _data()
    out = []
    for k, d in ad.DRIVERS.items():
        if k == "other":
            continue
        mods = [[d["module"], d["pip"]]] if d.get("module") else []
        out.append({"key": k, "title": d["title"], "mods": mods, "builtin": not mods, "note": d.get("note") or "", "url": ""})
    for k, d in EXTRA.items():
        mods = [[d["module"], d["pip"][0]]] + [list(x) for x in d.get("extra_mods", [])]
        out.append({"key": k, "title": d["title"], "mods": mods, "pips": d["pip"], "builtin": False, "note": "", "url": d["url"], "other": True})
    return out


def drivers(env):
    """-> {env, python, ok, error, sqlalchemy:{installed,version}, drivers:[{... installed, version}], command}."""
    ad = _data()
    py, name = nodeenv.resolve(env)
    res = {"env": env, "python": py, "name": name, "exists": os.path.isfile(py), "drivers": [], "error": "", "ui": sys.executable}
    cat = catalog()
    if not res["exists"]:
        res["error"] = "" if name else "no Python at %s" % py
        res["sqlalchemy"] = {"installed": False, "version": ""}
        res["drivers"] = [dict(d, installed=d["builtin"], version="", pips=d.get("pips") or [m[1] for m in d["mods"]]) for d in cat]
        return res
    req = [{"module": "sqlalchemy", "pip": ad.SQLALCHEMY}]
    for d in cat:
        for m, p in d["mods"]:
            req.append({"module": m, "pip": p})
    pr = nodeenv.probe(py, req, fresh=True)
    if pr.get("error"):
        res["error"] = pr["error"]
        return res
    mods = pr.get("mods") or {}
    res["sqlalchemy"] = {"installed": mods.get("sqlalchemy") is not None, "version": mods.get("sqlalchemy") or ""}
    res.update(version=pr.get("version", ""), venv=bool(pr.get("venv")), managed_python=bool(pr.get("managed")), pip=bool(pr.get("pip")))
    for d in cat:
        ok = d["builtin"] or all(mods.get(m) is not None for m, _ in d["mods"])
        vers = [mods.get(m) for m, _ in d["mods"] if mods.get(m)]
        res["drivers"].append(dict(d, installed=ok, version=vers[0] if vers else "", pips=d.get("pips") or [m[1] for m in d["mods"]]))
    return res
