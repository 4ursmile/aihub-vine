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
        req["readonly"] = (bool(c.get("readonly")) or bool(req.pop("force_ro", False))) if req.get("action") == "query" else False
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


# ---------- AI access: schema context across connections + guarded queries (chat tool, CLI, "Ask AI" in the studio)
# The model never sees a password or URL: it addresses a connection by name / id and the server runs the statement. What the AI runs is
# ALWAYS read-only (SELECT / WITH / SHOW / EXPLAIN...); a statement that changes data is handed back as SQL for the user to run in the studio.

_BOX = {"box": None}
_digests = {}                                  # (connection id, schema) -> (time, digest)
DIGEST_TTL = 180
AI_ROWS, AI_ROWS_MAX, AI_CELL = 30, 200, 90


# ---------- saved SQL files (Studio "Save" / the "SQL file" work node): {id, name, sql, conn, params: [{name, value}]}

NAME_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def detect_params(sql):
    """The `:name` parameters a statement uses, in order of first use."""
    from . import sqlrunner
    seen = []
    for _, _, n in sqlrunner.param_spans(str(sql or "")):
        if n not in seen:
            seen.append(n)
    return seen


def coerce_params(p):
    """Strings from forms -> numbers when they look like numbers. {name: value}; bad names are dropped."""
    out = {}
    for k, v in (p.items() if isinstance(p, dict) else []):
        if not NAME_OK.match(str(k)):
            continue
        if isinstance(v, str):
            t = v.strip()
            if re.match(r"^-?\d{1,15}$", t) and not (len(t) > 1 and t.lstrip("-").startswith("0")):
                v = int(t)
            elif re.match(r"^-?\d{0,15}\.\d{1,15}$", t):
                v = float(t)
        out[str(k)] = v if v is None or isinstance(v, (bool, int, float, str)) else str(v)
    return out


def files():
    return [f for f in _load().get("files", []) if isinstance(f, dict)]


def file_get(ref):
    """A saved SQL file by id or name (case-insensitive) -> record, or ValueError."""
    ref, fs = str(ref or "").strip(), files()
    hit = [f for f in fs if f.get("id") == ref] or [f for f in fs if str(f.get("name", "")).lower() == ref.lower()]
    if len(hit) == 1:
        return hit[0]
    raise ValueError(("Several SQL files are named %r." if len(hit) > 1 else "No saved SQL file %r.") % ref + " Saved: " + (", ".join(f["name"] for f in fs) or "none yet (Save one in SQL Studio)"))


def file_save(b):
    name = str(b.get("name") or "").strip()[:80]
    if not name:
        raise ValueError("Give the SQL file a name.")
    sql = str(b.get("sql") or "")
    if len(sql) > 500_000:
        raise ValueError("That SQL is too long to save.")
    names = detect_params(sql)
    given = {str(p.get("name")): str(p.get("value", "")) for p in (b.get("params") or []) if isinstance(p, dict) and NAME_OK.match(str(p.get("name") or ""))}
    params = [{"name": n, "value": given.get(n, "")} for n in names]
    with _lock:
        d = _load()
        fs = d.setdefault("files", [])
        cur = next((f for f in fs if f.get("id") == str(b.get("id") or "")), None) if b.get("id") else None
        if not cur:
            clash = next((f for f in fs if str(f.get("name", "")).lower() == name.lower()), None)
            if clash:
                cur = clash                                           # saving under an existing name updates it
            else:
                cur = {"id": uuid.uuid4().hex[:8]}
                fs.append(cur)
        cur.update(name=name, sql=sql, conn=str(b.get("conn") or ""), params=params, updated=int(time.time()))
        _save(d)
        return dict(cur)


def file_delete(fid):
    with _lock:
        d = _load()
        n = len(d.get("files", []))
        d["files"] = [f for f in d.get("files", []) if f.get("id") != fid]
        if len(d["files"]) == n:
            return False
        _save(d)
        return True


def bind(box):
    """The server hands over its secret box once, so tools that run inside chat turns (harnesstools) can reach saved connections."""
    _BOX["box"] = box


def resolve(ref):
    """A connection by id, exact name or unique name prefix (case-insensitive) -> its record, or raises ValueError listing what exists."""
    cons = _load()["connections"]
    ref = str(ref or "").strip()
    if not ref and len(cons) == 1:
        return cons[0]
    low = ref.lower()
    hit = [c for c in cons if c.get("id") == ref] or [c for c in cons if str(c.get("name", "")).lower() == low] \
        or ([c for c in cons if str(c.get("name", "")).lower().startswith(low)] if low else [])
    if len(hit) == 1:
        return hit[0]
    names = ", ".join("%s (%s)" % (c.get("name"), c.get("id")) for c in cons) or "none saved yet"
    raise ValueError(("Several connections match %r. " if len(hit) > 1 else "No connection %r. ") % ref + "Available: " + names)


def digest(box, cid, schema="", table="", fresh=False):
    """Tables, columns, keys of one connection (cached a few minutes). -> the runner's answer dict."""
    key = (cid, schema, table)
    hit = _digests.get(key)
    if hit and not fresh and time.time() - hit[0] < DIGEST_TTL:
        return hit[1]
    r = call(box, cid, {"action": "digest", "schema": schema, "table": table}, 60)
    if r.get("ok"):
        _digests[key] = (time.time(), r)
    return r


def _fmt_digest(c, r, tables_only=False):
    head = "## %s  [id %s · %s%s]" % (c.get("name"), c.get("id"), c.get("driver"), " · read-only" if c.get("readonly") else "")
    if not r.get("ok"):
        return head + "\n(schema unavailable: %s)" % str(r.get("error") or "unknown error")[:200]
    L = [head + (" schema %s" % r["schema"] if r.get("schema") else "")]
    if len(r.get("schemas") or []) > 1:
        L.append("other schemas: " + ", ".join(s for s in r["schemas"] if s != r.get("schema"))[:300])
    for t in r.get("tables") or []:
        if tables_only:
            L.append("- " + t["name"] + (" (view)" if t["kind"] == "view" else ""))
            continue
        cols = ", ".join("%s %s%s" % (x["name"], x["type"], " pk" if x["pk"] else "") for x in t.get("columns") or [])
        fk = "; ".join("%s -> %s(%s)" % (",".join(f["cols"]), f["to"], ",".join(f["to_cols"])) for f in t.get("fks") or [])
        L.append("- %s%s: %s%s%s" % (t["name"], " (view)" if t["kind"] == "view" else "", cols, " …+%d" % t["more_columns"] if t.get("more_columns") else "",
                                      " | fk: " + fk if fk else ""))
    if r.get("total", 0) > len(r.get("tables") or []):
        L.append("(%d of %d tables shown; ask for one with the table filter)" % (len(r["tables"]), r["total"]))
    return "\n".join(L)


def ai_context(box, refs=None, schema="", table="", budget=24000):
    """Text for a model: every named connection (default: all saved) with its tables and columns. Connections are read in parallel."""
    cons = [resolve(r) for r in refs] if refs else list(_load()["connections"])
    if not cons:
        return "No SQL connections are saved yet. The user adds one in SQL Studio (Connections tab)."
    res = {}

    def one(c):
        try:
            res[c["id"]] = digest(box, c["id"], schema if len(cons) == 1 else "", table)
        except Exception as e:                                       # one broken connection never hides the others
            res[c["id"]] = {"ok": False, "error": str(e)}
    ths = [threading.Thread(target=one, args=(c,), daemon=True) for c in cons]
    for t in ths:
        t.start()
    for t in ths:
        t.join(75)
    # fair share of the budget: a huge database cannot push the others out
    share = max(2500, budget // len(cons))
    out = []
    for c in cons:
        txt = _fmt_digest(c, res.get(c["id"]) or {"ok": False, "error": "timed out"})
        if len(txt) > share:
            txt = _fmt_digest(c, res.get(c["id"]) or {}, tables_only=True)
            if len(txt) > share:
                txt = txt[:share] + "\n…[cut]"
            txt += "\n(columns left out: ask for one table to see them)"
        out.append(txt)
    return "\n\n".join(out)


def _cell(v):
    s = "NULL" if v is None else str(v).replace("\n", " ")
    return s if len(s) <= AI_CELL else s[:AI_CELL] + "…"


def ai_query(box, ref, sql, limit=AI_ROWS):
    """Run SQL for the AI, read-only. -> {ok, text, columns, rows, truncated, ms, conn} (text = a compact table for the model)."""
    c = resolve(ref)
    limit = max(1, min(int(limit or AI_ROWS), AI_ROWS_MAX))
    r = call(box, c["id"], {"action": "query", "sql": sql, "limit": limit, "force_ro": True}, 120)
    r["conn"] = {"id": c["id"], "name": c.get("name")}
    if not r.get("ok"):
        r["text"] = "ERROR: " + str(r.get("error") or "failed") + ((" (install: %s)" % r["command"]) if r.get("command") else "")
        return r
    cols, rows = r.get("columns") or [], r.get("rows") or []
    if not cols:
        r["text"] = "OK, no rows returned (%s ms)" % r.get("ms")
        return r
    L = [" | ".join(cols)] + [" | ".join(_cell(v) for v in row) for row in rows]
    L.append("(%d row%s%s, %s ms, connection %s)" % (len(rows), "" if len(rows) == 1 else "s", "; more rows exist, narrow the query or raise limit" if r.get("truncated") else "", r.get("ms"), c.get("name")))
    r["text"] = "\n".join(L)
    return r


ASK_SYSTEM = """You write SQL for the user inside SQL Studio. You get the saved database connections with their tables and columns, the connection the editor
is on, the SQL now in the editor and, when it failed, its error. Reply with ONE ```sql block holding the complete statement(s) and then at most two
short sentences (assumptions, what to check). Rules:
- Use only tables and columns that appear in the schema; match the dialect of the connection (limit/top/fetch first, quoting, date functions).
- Prefer a read-only SELECT. Never invent credentials. Put a limit on exploratory queries.
- The SQL runs on ONE connection. If the question belongs to another connection, start the block with the comment `-- conn: <connection name>`.
- When asked to fix, change the existing SQL as little as needed. When asked to explain, answer in prose and add no block."""
FENCE = re.compile(r"```[ \t]*(?:sql|[a-z]*sql)?[ \t]*\n(.*?)\n?[ \t]*```", re.S | re.I)


def ask_sql(box, ask, conn="", sql="", error="", others=None, model=""):
    """Magic SQL: -> {sql, conn (id the SQL is for), note, engine}. `others` = more connection refs whose schemas the model may use."""
    from . import magic
    cur = resolve(conn) if conn else None
    refs = ([cur["id"]] if cur else []) + [resolve(o)["id"] for o in (others or []) if not cur or resolve(o)["id"] != cur["id"]]
    ctx = ai_context(box, refs or None, budget=20000)
    prompt = "# Schemas\n%s\n\n# Editor is on: %s\n" % (ctx, ("%s (%s)" % (cur["name"], cur["driver"])) if cur else "no connection")
    if sql.strip():
        prompt += "\n# SQL in the editor\n```sql\n%s\n```\n" % sql[:8000]
    if error:
        prompt += "\n# It failed with\n%s\n" % error[:1500]
    prompt += "\n# Request\n" + (ask.strip() or ("Fix the error." if error else "Improve this SQL."))
    text, used, how = magic.ask_model(ASK_SYSTEM, prompt, model, box.get_all() if box else None, "auto", 4000)
    m = FENCE.search(text)
    code = (m.group(1) if m else "").strip()
    target = ""
    cm = re.match(r"--\s*conn:\s*(.+)", code)
    if cm:
        try:
            target = resolve(cm.group(1).strip())["id"]
        except ValueError:
            target = ""
        code = code[cm.end():].lstrip("\n")
    note = (FENCE.sub("", text) if m else text).strip()[:600]
    return {"sql": code, "conn": target or (cur["id"] if cur else ""), "note": note, "engine": how, "model": used}
