"""aihub_data: SQL (SQLAlchemy) and data frames (pandas / polars) for Works code nodes and your own scripts. Stdlib only at import time.

Nothing heavy is imported until a function needs it, and a missing package is an error that says what to install and how:

    import aihub_data as ad
    eng = ad.engine("warehouse")                         # a 'sql' credential set (granted to the node), a SQLAlchemy URL, or a dict
    rows = ad.read_sql("select * from t", eng)           # native: a list of dicts
    df = ad.read_sql("select * from t", eng, frame="polars")
    ad.write(df, "daily", eng, if_exists="replace")      # pandas / polars DataFrame, or a list of dicts
    pd = ad.need("pandas")                               # import it, or raise Missing with the pip command

Drivers (`DRIVERS`): postgresql, mssql, mssql-odbc, oracle, trino, mysql, sqlite, duckdb, or any SQLAlchemy URL (`other`)."""
import importlib
import os
import sys

try:                                                                   # py3.8+
    from urllib.parse import parse_qsl
except ImportError:                                                    # pragma: no cover
    parse_qsl = None

__all__ = ["DRIVERS", "PIP", "Missing", "need", "has", "install_command", "url", "engine", "safe_url", "read_sql", "write", "frame_rows", "is_query", "check"]

SQLALCHEMY = "sqlalchemy>=2"
# driver -> SQLAlchemy dialect, the DBAPI module it imports and the pip package that provides it
DRIVERS = {
    "postgresql": {"title": "PostgreSQL", "dialect": "postgresql+psycopg", "module": "psycopg", "pip": "psycopg[binary]", "port": 5432},
    "mssql": {"title": "SQL Server", "dialect": "mssql+pymssql", "module": "pymssql", "pip": "pymssql", "port": 1433},
    "mssql-odbc": {"title": "SQL Server (ODBC)", "dialect": "mssql+pyodbc", "module": "pyodbc", "pip": "pyodbc", "port": 1433,
                   "query": {"driver": "ODBC Driver 18 for SQL Server", "TrustServerCertificate": "yes"},
                   "note": "also needs Microsoft's ODBC Driver 18 for SQL Server installed on the system"},
    "oracle": {"title": "Oracle", "dialect": "oracle+oracledb", "module": "oracledb", "pip": "oracledb", "port": 1521,
               "note": "Database = the service name (thin mode: no Oracle client needed)"},
    "trino": {"title": "Trino / Presto", "dialect": "trino", "module": "trino", "pip": "trino", "port": 443, "note": "Database = catalog or catalog/schema"},
    "mysql": {"title": "MySQL / MariaDB", "dialect": "mysql+pymysql", "module": "pymysql", "pip": "pymysql", "port": 3306},
    "sqlite": {"title": "SQLite", "dialect": "sqlite", "module": "", "pip": "", "note": "Database = the file path (empty = in memory); no driver to install"},
    "duckdb": {"title": "DuckDB", "dialect": "duckdb", "module": "duckdb_engine", "pip": "duckdb-engine", "note": "Database = the file path (empty = in memory)"},
    "other": {"title": "Other (SQLAlchemy URL)", "dialect": "", "module": "", "pip": "", "note": "write the full SQLAlchemy URL; install its driver yourself"},
}
# import name -> pip package, when they differ (or need an extra)
PIP = {"sqlalchemy": SQLALCHEMY, "psycopg": "psycopg[binary]", "psycopg2": "psycopg2-binary", "duckdb_engine": "duckdb-engine", "yaml": "pyyaml",
       "sklearn": "scikit-learn", "PIL": "pillow", "cv2": "opencv-python", "bs4": "beautifulsoup4", "dateutil": "python-dateutil", "dotenv": "python-dotenv",
       "snowflake": "snowflake-sqlalchemy", "google": "google-cloud-bigquery", "jwt": "pyjwt", "Crypto": "pycryptodome", "attr": "attrs", "magic": "python-magic"}
QUERY_WORDS = ("select", "with", "show", "describe", "desc", "explain", "values", "table", "pragma")


def install_command(pips, python=None):
    py = python or sys.executable
    q = (lambda s: '"%s"' % s if (" " in s or "[" in s or ">" in s or "<" in s) else s) if os.name == "nt" else (lambda s: "'%s'" % s if any(c in s for c in " []<>=*") else s)
    return "%s -m pip install %s" % (q(py), " ".join(q(p) for p in pips))


class Missing(ImportError):
    """A package the code needs is not installed for this Python. `pips` = what to install, `command` = the one line that does it."""

    def __init__(self, pips, why=""):
        self.pips, self.why = list(pips), why
        self.command = install_command(self.pips)
        super().__init__("%s %s not installed for %s%s. Install with: %s  (or press Install in the node's Environment box)"
                         % (", ".join(self.pips), "is" if len(self.pips) == 1 else "are", sys.executable, " (needed for %s)" % why if why else "", self.command))


def has(module):
    try:
        import importlib.util as u
        return u.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def need(module, pip=None, why=""):
    """Import MODULE now (lazy import). A missing one raises Missing naming the pip package; a broken dependency of it names that one."""
    try:
        return importlib.import_module(module)
    except ImportError as e:
        name = (getattr(e, "name", "") or "").split(".")[0]
        top = module.split(".")[0]
        if name and name != top and not has(name):                     # the module is there, one of ITS dependencies is not
            raise Missing([PIP.get(name, name)], "%s, which %s imports" % (why or top, top))
        raise Missing([pip or PIP.get(top, top)], why)


# ---------- SQLAlchemy

def _fields(src, kw):
    """A credential set name, a dict of fields or a URL -> plain dict of fields."""
    if isinstance(src, str) and "://" in src:
        return dict(kw, url=src)
    if isinstance(src, str) and src:
        import aihub_vault
        return dict(dict(aihub_vault.credential(src)), **kw)
    if isinstance(src, dict):
        return dict(src, **kw)
    return dict(kw)


def _qs(text):
    text = str(text or "").strip().lstrip("?")
    if not text:
        return {}
    if parse_qsl is None:                                              # pragma: no cover
        return dict(p.split("=", 1) for p in text.split("&") if "=" in p)
    return dict(parse_qsl(text, keep_blank_values=True))


def url(src=None, **kw):
    """A SQLAlchemy URL from fields: driver, host, port, user, password, database, options (key=value&...), or `url` as it is."""
    f = _fields(src, kw)
    sa = need("sqlalchemy", SQLALCHEMY, "SQL")
    if str(f.get("url") or "").strip():
        return sa.engine.make_url(str(f["url"]).strip())
    drv = str(f.get("driver") or "postgresql").strip()
    d = DRIVERS.get(drv)
    if not d:
        raise ValueError("unknown driver %r (one of %s, or set url)" % (drv, ", ".join(DRIVERS)))
    if not d["dialect"]:
        raise ValueError("driver 'other' needs the full SQLAlchemy URL (the URL field)")
    q = dict(d.get("query") or {})
    q.update(_qs(f.get("options")))
    db = str(f.get("database") or "").strip() or None
    host = str(f.get("host") or "").strip() or None
    user = str(f.get("user") or f.get("username") or "").strip() or None
    pw = str(f.get("password") or "") or None
    port = str(f.get("port") or "").strip()
    if drv in ("sqlite", "duckdb"):
        return sa.engine.URL.create(d["dialect"], database=os.path.expanduser(db) if db else None, query=q)
    if drv == "oracle" and db and "service_name" not in q and "/" not in db:
        q["service_name"], db = db, None
    if drv == "trino":
        q.setdefault("http_scheme", "http" if port == "8080" and not pw else "https")
        if not user:
            user = os.environ.get("USER") or os.environ.get("USERNAME") or "aihub"
    try:
        port_n = int(port) if port else None
    except ValueError:
        raise ValueError("port must be a number (got %r)" % port)
    return sa.engine.URL.create(d["dialect"], username=user, password=pw, host=host, port=port_n, database=db, query=q)


def driver_of(u):
    """Our driver key for a SQLAlchemy URL (or '')."""
    name = str(getattr(u, "drivername", u) or "").split("+")[0]
    return {"postgresql": "postgresql", "postgres": "postgresql", "mssql": "mssql", "oracle": "oracle", "trino": "trino", "mysql": "mysql", "mariadb": "mysql",
            "sqlite": "sqlite", "duckdb": "duckdb"}.get(name, "")


def safe_url(u):
    """The URL with the password hidden (for logs)."""
    try:
        return u.render_as_string(hide_password=True)
    except AttributeError:
        return str(u)


def engine(src=None, **kw):
    """A SQLAlchemy Engine. SRC = a 'sql' credential set (name or id, granted to the node), a dict of fields, or a URL; keywords add/override
    fields (driver, host, port, user, password, database, options, url). `connect_args` (dict) and `verify` (false = no TLS check, Trino) pass through."""
    f = _fields(src, kw)
    sa = need("sqlalchemy", SQLALCHEMY, "SQL")
    u = url(f)
    args = dict(f.get("connect_args") or {})
    if str(f.get("verify", "")).strip().lower() in ("false", "0", "no", "off") and driver_of(u) == "trino":
        args["verify"] = False
    d = DRIVERS.get(driver_of(u) or str(f.get("driver") or ""), {})
    if d.get("module") and not has(d["module"]):                      # SQLAlchemy would only say "No module named ..." later
        raise Missing([d["pip"]], "the %s driver" % d["title"])
    try:
        return sa.create_engine(u, connect_args=args) if args else sa.create_engine(u)
    except ImportError as e:
        name = (getattr(e, "name", "") or "").split(".")[0]
        raise Missing([PIP.get(name, name) if name else (d.get("pip") or "the database driver")], "the %s driver" % (d.get("title") or u.drivername))


def is_query(sql):
    """True when a statement returns rows (SELECT, WITH, SHOW ...): what a frame library may read."""
    s = str(sql or "").lstrip()
    while s.startswith("--") or s.startswith("/*"):
        s = (s.split("\n", 1)[1] if s.startswith("--") and "\n" in s else s.split("*/", 1)[1] if "*/" in s else "").lstrip()
    s = s.lstrip("(").lower()
    return s.split(None, 1)[0] in QUERY_WORDS if s else False


def _conn(eng):
    return eng.connect() if hasattr(eng, "connect") and not hasattr(eng, "exec_driver_sql") else _Same(eng)


class _Same:
    """`with` on a Connection that is already open: leave it open."""

    def __init__(self, c):
        self.c = c

    def __enter__(self):
        return self.c

    def __exit__(self, *a):
        return False


def read_sql(sql, eng, frame="native", params=None, limit=None):
    """Run one query. frame = native (a list of dicts), pandas or polars (a DataFrame). ENG = an Engine, a Connection, or what engine() takes."""
    if not hasattr(eng, "connect") and not hasattr(eng, "exec_driver_sql"):
        eng = engine(eng)
    sa = need("sqlalchemy", SQLALCHEMY, "SQL")
    with _conn(eng) as c:
        if frame == "pandas":
            pd = need("pandas", why="frame = pandas")
            df = pd.read_sql_query(sa.text(sql), c, params=params or None)
            return df.head(limit) if limit else df
        if frame == "polars":
            pl = need("polars", why="frame = polars")
            kw = {"execute_options": {"parameters": params}} if params else {}
            df = pl.read_database(query=sa.text(sql) if params else sql, connection=c, **kw)
            return df.head(limit) if limit else df
        r = c.execute(sa.text(sql), params or {})
        if not r.returns_rows:
            return []
        cols = list(r.keys())
        rows = r.fetchmany(limit) if limit else r.fetchall()
        return [dict(zip(cols, x)) for x in rows]


def frame_rows(obj, limit=None):
    """(columns, rows as lists) of a pandas / polars DataFrame or a list of dicts, for previews and JSON."""
    if hasattr(obj, "to_dicts") and hasattr(obj, "columns"):           # polars
        df = obj.head(limit) if limit else obj
        return list(df.columns), [list(r.values()) for r in df.to_dicts()]
    if hasattr(obj, "to_dict") and hasattr(obj, "columns") and hasattr(obj, "iloc"):   # pandas
        df = obj.head(limit) if limit else obj
        return [str(c) for c in df.columns], df.astype(object).where(df.notna(), None).values.tolist()
    rows = list(obj or [])[:limit] if limit else list(obj or [])
    cols = []
    for r in rows:
        for k in (r.keys() if isinstance(r, dict) else []):
            if k not in cols:
                cols.append(k)
    return cols, [[r.get(k) for k in cols] if isinstance(r, dict) else list(r) for r in rows]


def _col_type(sa, values):
    import datetime
    seen = [v for v in values if v is not None][:200]
    if seen and all(isinstance(v, bool) for v in seen):
        return sa.Boolean()
    if seen and all(isinstance(v, int) and not isinstance(v, bool) for v in seen):
        return sa.BigInteger()
    if seen and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in seen):
        return sa.Float()
    if seen and all(isinstance(v, datetime.datetime) for v in seen):
        return sa.DateTime()
    if seen and all(isinstance(v, datetime.date) for v in seen):
        return sa.Date()
    return sa.String(4000) if not any(isinstance(v, str) and len(v) > 4000 for v in seen) else sa.UnicodeText()


def write(obj, table, eng, if_exists="append", schema=None, chunk=5000):
    """Write rows into TABLE. OBJ = a pandas DataFrame (to_sql), a polars DataFrame (write_database) or a list of dicts (native: the table
    is created from the values when it does not exist). if_exists = append | replace | fail. -> rows written."""
    if if_exists not in ("append", "replace", "fail"):
        raise ValueError("if_exists must be append, replace or fail")
    if not hasattr(eng, "connect") and not hasattr(eng, "exec_driver_sql"):
        eng = engine(eng)
    sa = need("sqlalchemy", SQLALCHEMY, "SQL")
    if hasattr(obj, "to_sql") and hasattr(obj, "iloc"):                 # pandas
        obj.to_sql(table, eng, schema=schema or None, if_exists=if_exists, index=False, chunksize=chunk)
        return len(obj)
    if hasattr(obj, "write_database") and hasattr(obj, "to_dicts"):     # polars: its SQLAlchemy writer goes through pandas + pyarrow
        for m, why in (("pandas", "polars write_database"), ("pyarrow", "polars write_database")):
            need(m, why=why)
        obj.write_database(table_name=("%s.%s" % (schema, table)) if schema else table, connection=eng, if_table_exists=if_exists, engine="sqlalchemy")
        return obj.height
    cols, rows = frame_rows(obj)
    if not cols:
        return 0
    with eng.begin() as c:
        exists = sa.inspect(c).has_table(table, schema=schema or None)
        if exists and if_exists == "fail":
            raise ValueError("table %s already exists (if_exists = fail)" % table)
        md = sa.MetaData()
        if exists and if_exists == "replace":
            sa.Table(table, md, schema=schema or None, autoload_with=c).drop(c)
            md, exists = sa.MetaData(), False
        if exists:
            t = sa.Table(table, md, schema=schema or None, autoload_with=c)
        else:
            t = sa.Table(table, md, *[sa.Column(str(k), _col_type(sa, [r[i] for r in rows])) for i, k in enumerate(cols)], schema=schema or None)
            t.create(c)
        for i in range(0, len(rows), chunk):
            c.execute(t.insert(), [dict(zip(cols, r)) for r in rows[i:i + chunk]])
    return len(rows)


def check(modules):
    """{module: version or None} for this Python (what is installed)."""
    out = {}
    for m in modules:
        if not has(m):
            out[m] = None
            continue
        try:
            from importlib import metadata
            out[m] = metadata.version(PIP.get(m, m).split("[")[0].split(">")[0].split("=")[0])
        except Exception:
            out[m] = ""
    return out

