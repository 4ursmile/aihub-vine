#!/usr/bin/env python3
"""SQL node (SQLAlchemy). JSON on stdin ({config, input, inputs, workdir}), JSON on stdout. Runs in the node's Python environment.

Any database SQLAlchemy reaches: PostgreSQL, SQL Server (pymssql or ODBC), Oracle (oracledb thin), Trino, MySQL, SQLite, DuckDB, or a URL.
Login: a 'sql' credential set (driver, host, port, user, password, database, options or url) or the node's fields (a field wins).
`Read and write with` = native (SQLAlchemy rows), pandas or polars: the frame library reads the result query and writes `Write table`.
Several statements (`;` between them, `-- %% name` names one) run one after the other on ONE connection, each committed; the LAST
query (SELECT, WITH, SHOW ...) is the result: a table (markdown), JSON, CSV or a file in the node folder (csv, json, jsonl, parquet).
`Write table`: rows coming in (data.rows of the node before, a JSON list, or a csv / json / jsonl / parquet path) are written into
that table BEFORE the SQL runs. Every package is imported only when it is needed; a missing one fails with the pip command."""
import csv
import io
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trino_query import md_table, statements  # noqa: E402  (same splitter as the Trino node: quotes, comments, `-- %% name`)

TEXT_CAP = 200_000
MAX_FILE_ROWS = 50_000_000
LOGIN = ("driver", "url", "host", "port", "user", "password", "database", "options", "verify")


def done(**k):
    sys.stdout.write("\n" + json.dumps(k, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()
    sys.exit(0)


def fail(msg):
    done(ok=False, error=msg)


def say(msg):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def incoming(d):
    """(data merged from the nodes before, the input text)."""
    data = {}
    for ev in d.get("inputs") or []:
        if isinstance(ev, dict) and isinstance(ev.get("data"), dict):
            data.update(ev["data"])
    return data, str(d.get("input") or "")


def read_source(ad, frame, data, text, workdir):
    """What `Write table` writes: data.rows, a JSON list in the input, or a file path (data.path or the input). -> (object, where from)."""
    rows = data.get("rows")
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return to_frame(ad, frame, rows), "data.rows"
    t = text.strip()
    if t.startswith("["):
        try:
            v = json.loads(t)
            if isinstance(v, list) and (not v or isinstance(v[0], dict)):
                return to_frame(ad, frame, v), "the input (JSON)"
        except ValueError:
            pass
    path = str(data.get("path") or "").strip() or (t.splitlines()[-1].strip() if t and len(t) < 2000 else "")
    path = path[len("OUTPUT:"):].strip() if path.upper().startswith("OUTPUT:") else path
    if path:
        p = os.path.expanduser(path)
        p = p if os.path.isabs(p) else os.path.join(workdir, p)
        if os.path.isfile(p):
            return read_file(ad, frame, p), p
    raise ValueError("Write table: nothing to write. Connect a node that gives rows (data.rows), a JSON list, or a csv / json / jsonl / parquet file path")


def to_frame(ad, frame, rows):
    if frame == "pandas":
        return ad.need("pandas", why="frame = pandas").DataFrame(rows)
    if frame == "polars":
        return ad.need("polars", why="frame = polars").DataFrame(rows)
    return rows


def read_file(ad, frame, p):
    ext = os.path.splitext(p)[1].lower().lstrip(".")
    if frame == "pandas":
        pd = ad.need("pandas", why="frame = pandas")
        if ext == "parquet":
            ad.need("pyarrow", why="parquet files")
            return pd.read_parquet(p)
        return pd.read_json(p, lines=True) if ext in ("jsonl", "ndjson") else pd.read_json(p) if ext == "json" else pd.read_csv(p)
    if frame == "polars":
        pl = ad.need("polars", why="frame = polars")
        return pl.read_parquet(p) if ext == "parquet" else pl.read_ndjson(p) if ext in ("jsonl", "ndjson") else pl.read_json(p) if ext == "json" else pl.read_csv(p)
    if ext == "parquet":
        return ad.need("pyarrow.parquet", "pyarrow", "parquet files").read_table(p).to_pylist()
    with open(p, encoding="utf-8-sig", newline="") as f:
        if ext in ("jsonl", "ndjson"):
            return [json.loads(ln) for ln in f if ln.strip()]
        if ext == "json":
            v = json.load(f)
            return v if isinstance(v, list) else [v]
        return list(csv.DictReader(f))


class Sink:
    """The result file (output = path), written as rows arrive: nothing is kept in memory for csv / json / jsonl."""

    def __init__(self, path, fmt):
        self.path, self.fmt, self.f, self.w, self.n = path, fmt, None, None, 0
        self.parts = []

    def rows(self, cols, rows):
        if self.fmt == "parquet":
            self.parts.extend(dict(zip(cols, r)) for r in rows)
            return
        if self.f is None:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self.f = open(self.path, "w", encoding="utf-8", newline="")
            if self.fmt == "csv":
                self.w = csv.writer(self.f)
                self.w.writerow(cols)
            elif self.fmt == "json":
                self.f.write("[")
        for r in rows:
            self.n += 1
            if self.n > MAX_FILE_ROWS:
                raise RuntimeError("more than %d rows: write them with INSERT / CREATE TABLE AS instead" % MAX_FILE_ROWS)
            if self.fmt == "csv":
                self.w.writerow(r)
            elif self.fmt == "jsonl":
                self.f.write(json.dumps(dict(zip(cols, r)), ensure_ascii=False, default=str) + "\n")
            else:
                self.f.write(("," if self.n > 1 else "") + json.dumps(dict(zip(cols, r)), ensure_ascii=False, default=str))

    def close(self, ad):
        if self.fmt == "parquet":
            pa = ad.need("pyarrow", why="parquet files")
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            ad.need("pyarrow.parquet", "pyarrow", "parquet files").write_table(pa.Table.from_pylist(self.parts), self.path)
            return
        if self.f is not None:
            if self.fmt == "json":
                self.f.write("]")
            self.f.close()


def frame_to_file(df, frame, path, fmt):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if frame == "pandas":
        {"csv": lambda: df.to_csv(path, index=False), "json": lambda: df.to_json(path, orient="records", date_format="iso"),
         "jsonl": lambda: df.to_json(path, orient="records", lines=True, date_format="iso"), "parquet": lambda: df.to_parquet(path, index=False)}[fmt]()
    else:
        {"csv": lambda: df.write_csv(path), "json": lambda: df.write_json(path), "jsonl": lambda: df.write_ndjson(path), "parquet": lambda: df.write_parquet(path)}[fmt]()


def main():
    d = json.load(sys.stdin)
    cfg = d.get("config") or {}
    sql = str(cfg.get("sql") or "").strip()
    table = str(cfg.get("write_table") or "").strip()
    if not sql and not table:
        fail("write the SQL to run (or set Write table)")
    frame = str(cfg.get("frame") or "native")
    if frame not in ("native", "pandas", "polars"):
        fail("Read and write with: native, pandas or polars")
    try:
        cap = min(max(int(cfg.get("max_rows") or 1000), 1), 100_000)
    except (TypeError, ValueError):
        fail("Max rows must be a number")
    out_kind = str(cfg.get("output") or "table")
    fmt = str(cfg.get("file_format") or "csv")
    if fmt not in ("csv", "json", "jsonl", "parquet"):
        fail("File format: csv, json, jsonl or parquet")
    ndir = os.environ.get("AIHUB_NODE_DIR") or d.get("workdir") or os.getcwd()
    workdir = d.get("workdir") or ndir
    fname = re.sub(r"[^\w.-]", "_", str(cfg.get("file_name") or "result")) or "result"
    path = os.path.join(ndir, fname if fname.endswith("." + fmt) else fname + "." + fmt) if out_kind == "path" else ""
    data_in, text_in = incoming(d)
    try:
        import aihub_data as ad
    except ImportError:
        fail("aihub_data is missing from PYTHONPATH (the UI sets it for every node; for your own scripts: aihub work lib)")
    login = {k: cfg.get(k) for k in LOGIN if str(cfg.get(k) or "").strip()}
    try:
        eng = ad.engine(login)
    except ad.Missing as e:
        fail(str(e))
    except (ValueError, TypeError) as e:
        fail("cannot build the database URL: %s" % e)
    except Exception as e:                                                     # bad URL text, unknown dialect
        fail("cannot open the database: %s" % e)
    say("database: %s  (frame: %s, python %s)" % (ad.safe_url(eng.url), frame, sys.version.split()[0]))
    sa = ad.need("sqlalchemy")
    report, wrote = [], None
    if table:
        t0 = time.time()
        try:
            obj, src = read_source(ad, frame, data_in, text_in, workdir)
            schema = str(cfg.get("write_schema") or "").strip() or None
            n = ad.write(obj, table, eng, if_exists=str(cfg.get("if_exists") or "append"), schema=schema)
        except ad.Missing as e:
            fail(str(e))
        except Exception as e:
            fail("Write table %s failed: %s" % (table, str(e).split("\n")[0][:600]))
        wrote = {"table": table, "rows": n, "from": src}
        report.append({"name": "write " + table, "seconds": round(time.time() - t0, 3), "rows": n})
        say("wrote %d row%s into %s from %s (%.1fs)" % (n, "" if n == 1 else "s", table, src, time.time() - t0))
    steps = statements(sql) if sql else []
    last_q = max((i for i, (_, st) in enumerate(steps) if ad.is_query(st)), default=-1)
    last = {"cols": [], "rows": [], "count": 0, "truncated": False, "frame": None}
    try:
        conn = eng.connect()
    except ad.Missing as e:
        fail(str(e))
    except Exception as e:
        fail("cannot connect: %s" % str(e).split("\n")[0][:600])
    try:
        for k, (name, st) in enumerate(steps, 1):
            say("step %d/%d %s: running" % (k, len(steps), name))
            t0 = time.time()
            cur = {"cols": [], "rows": [], "count": 0, "truncated": False, "frame": None}
            try:
                if k - 1 == last_q and frame != "native":                     # the result query, read by the frame library
                    df = ad.read_sql(st, conn, frame=frame)
                    cols, rows = ad.frame_rows(df, cap)
                    cur.update(cols=cols, rows=rows, count=len(df), truncated=len(df) > cap, frame=df)
                    if path:
                        frame_to_file(df, frame, path, fmt)
                else:
                    r = conn.execute(sa.text(st))
                    if r.returns_rows:
                        cur["cols"] = list(r.keys())
                        sink = Sink(path, fmt) if path and k - 1 == last_q else None
                        try:
                            while True:
                                part = r.fetchmany(5000)
                                if not part:
                                    break
                                part = [list(x) for x in part]
                                cur["count"] += len(part)
                                room = cap - len(cur["rows"])
                                if room > 0:
                                    cur["rows"].extend(part[:room])
                                if len(part) > max(room, 0):
                                    cur["truncated"] = True
                                if sink:
                                    sink.rows(cur["cols"], part)
                        finally:
                            if sink:
                                sink.close(ad)
                    else:
                        cur["count"] = max(r.rowcount or 0, 0)
                conn.commit()
            except ad.Missing as e:
                fail(str(e))
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                msg = str(getattr(e, "orig", None) or e).strip().split("\n")[0][:800]
                fail("step %d/%d %s failed: %s" % (k, len(steps), name, msg))
            dt = time.time() - t0
            if cur["cols"]:
                last = cur
            report.append({"name": name, "seconds": round(dt, 3), "rows": cur["count"]})
            say("step %d/%d %s: ok (%.1fs, %d row%s)" % (k, len(steps), name, dt, cur["count"], "" if cur["count"] == 1 else "s"))
    finally:
        try:
            conn.close()
            eng.dispose()
        except Exception:
            pass
    cols, rows = last["cols"], last["rows"]
    objs = [dict(zip(cols, r)) for r in rows]
    data = {"columns": cols, "rows": objs, "row_count": last["count"], "truncated": last["truncated"], "steps": report, "frame": frame}
    if wrote:
        data["written"] = wrote
    if last["frame"] is not None:
        try:
            data["dtypes"] = {str(c): str(t) for c, t in zip(last["frame"].columns, last["frame"].dtypes)}
        except Exception:
            pass
    if path and os.path.exists(path):
        data["path"] = path
        text = path
    elif out_kind == "json":
        text = json.dumps(objs, ensure_ascii=False, default=str, indent=1)
    elif out_kind == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        w.writerows(rows)
        text = buf.getvalue()
    elif cols:
        text = md_table(cols, rows) + ("\n\n(first %d of %d rows)" % (len(rows), last["count"]) if last["truncated"] else "")
    elif wrote and not steps:
        text = "wrote %d rows into %s" % (wrote["rows"], wrote["table"])
    else:
        text = "%d statement(s) ran" % len(report)
    if len(text) > TEXT_CAP:
        text = text[:TEXT_CAP] + "\n... [cut; use output = path for big results]"
    done(ok=True, output=text, data=data, log="; ".join("%s %.1fs %d rows" % (r["name"], r["seconds"], r["rows"]) for r in report))


if __name__ == "__main__":
    main()
