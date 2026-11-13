#!/usr/bin/env python3
"""SQL Studio runner: one JSON request on stdin, one JSON answer (last stdout line). Runs in the connection's Python environment so the
drivers installed there are found. Actions: test, tables, columns, query. Never prints the password; secrets only live in memory."""
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))


def out(**k):
    sys.stdout.write("\n" + json.dumps(k, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()
    sys.exit(0)


def cell(v):
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if v == v and v not in (float("inf"), float("-inf")) else str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        return "\\x" + b[:32].hex() + ("…" if len(b) > 32 else "")
    return str(v)


def split(sql):
    """Statements split on ';' outside quotes and comments."""
    res, cur, i, n = [], [], 0, len(sql)
    while i < n:
        c = sql[i]
        if c in "'\"`":
            j = i + 1
            while j < n:
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            cur.append(sql[i:j + 1])
            i = j + 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            cur.append(sql[i:j])
            i = j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
            cur.append(sql[i:j])
            i = j
        elif c == ";":
            res.append("".join(cur))
            cur = []
            i += 1
        else:
            cur.append(c)
            i += 1
    res.append("".join(cur))
    return [s.strip() for s in res if re.sub(r"--[^\n]*|/\*.*?\*/", "", s, flags=re.S).strip()]


def main():
    req = json.loads(sys.stdin.read() or "{}")
    try:
        import aihub_data as ad
    except Exception as e:
        out(ok=False, error="cannot load the data helpers: %s" % e)
    act, c = req.get("action"), req.get("conn") or {}
    try:
        eng = ad.engine(c)
    except ad.Missing as e:
        pips = list(e.pips)
        if any(p.startswith("sqlalchemy") for p in pips):                 # one hint for everything: the core AND the driver of this database
            key = ad.driver_of(str(c.get("url") or "").split("://")[0]) if "://" in str(c.get("url") or "") else str(c.get("driver") or "")
            drv = (ad.DRIVERS.get(key) or {})
            if drv.get("pip") and not ad.has(drv.get("module") or "") and drv["pip"] not in pips:
                pips.append(drv["pip"])
        out(ok=False, error=str(e).split(" Install with")[0], missing=pips, command=ad.install_command(pips))
    except Exception as e:
        out(ok=False, error=str(e)[:600])
    sa = ad.need("sqlalchemy")
    t0 = time.time()
    try:
        if act == "test":
            with eng.connect() as cn:
                ver = ""
                try:
                    ver = str(cn.exec_driver_sql("select version()").scalar() or "")
                except Exception:
                    try:
                        cn.rollback()
                        ver = str(cn.exec_driver_sql("select sqlite_version()").scalar() or "")
                    except Exception:
                        pass
            out(ok=True, dialect=eng.dialect.name, server=ver[:160], ms=int((time.time() - t0) * 1000))
        if act == "tables":
            insp = sa.inspect(eng)
            schemas = []
            try:
                schemas = [s for s in insp.get_schema_names()][:300]
            except Exception:
                pass
            sch = req.get("schema") or None
            if sch is None and not req.get("schema_given"):
                sch = getattr(insp, "default_schema_name", None)
            tabs = [{"name": n, "kind": "table"} for n in insp.get_table_names(schema=sch)]
            try:
                tabs += [{"name": n, "kind": "view"} for n in insp.get_view_names(schema=sch)]
            except Exception:
                pass
            tabs.sort(key=lambda t: t["name"].lower())
            out(ok=True, schemas=schemas, schema=sch, tables=tabs[:2000], more=len(tabs) > 2000)
        if act == "digest":
            # tables + columns + keys of one schema in ONE process: the data context an AI needs to write correct SQL (capped, time-boxed)
            insp = sa.inspect(eng)
            schemas = []
            try:
                schemas = [s for s in insp.get_schema_names()][:60]
            except Exception:
                pass
            sch = req.get("schema") or None
            if sch is None:
                sch = getattr(insp, "default_schema_name", None)
            max_t, max_c, budget = int(req.get("max_tables") or 60), int(req.get("max_columns") or 40), float(req.get("budget") or 20)
            names = [(n, "table") for n in insp.get_table_names(schema=sch)]
            try:
                names += [(n, "view") for n in insp.get_view_names(schema=sch)]
            except Exception:
                pass
            names.sort(key=lambda t: t[0].lower())
            want = str(req.get("table") or "").lower()
            if want:
                names = [t for t in names if want in t[0].lower()]
            tabs = []
            for n, kind in names[:max_t]:
                if time.time() - t0 > budget:
                    break
                t = {"name": n, "kind": kind, "columns": []}
                try:
                    pk = set(insp.get_pk_constraint(n, schema=sch).get("constrained_columns") or [])
                    cols = insp.get_columns(n, schema=sch)
                    t["columns"] = [{"name": x["name"], "type": str(x.get("type")), "pk": x["name"] in pk} for x in cols[:max_c]]
                    t["more_columns"] = max(0, len(cols) - max_c)
                    fks = insp.get_foreign_keys(n, schema=sch)
                    t["fks"] = [{"cols": f.get("constrained_columns") or [], "to": (f.get("referred_table") or ""), "to_cols": f.get("referred_columns") or []} for f in fks][:12]
                except Exception as e:
                    t["error"] = str(e)[:120]
                tabs.append(t)
            out(ok=True, dialect=eng.dialect.name, schemas=schemas, schema=sch, tables=tabs, total=len(names), ms=int((time.time() - t0) * 1000))
        if act == "columns":
            insp = sa.inspect(eng)
            sch, tb = req.get("schema") or None, str(req.get("table") or "")
            pk = set()
            try:
                pk = set(insp.get_pk_constraint(tb, schema=sch).get("constrained_columns") or [])
            except Exception:
                pass
            cols = [{"name": x["name"], "type": str(x.get("type")), "null": bool(x.get("nullable", True)), "pk": x["name"] in pk}
                    for x in insp.get_columns(tb, schema=sch)]
            out(ok=True, columns=cols)
        if act == "query":
            limit = max(1, min(int(req.get("limit") or 1000), 50000))
            ro = bool(req.get("readonly"))
            stmts = split(str(req.get("sql") or ""))
            if not stmts:
                out(ok=False, error="Write a statement first.")
            res = {"columns": [], "rows": [], "rowcount": None, "truncated": False}
            done = 0
            with eng.connect() as cn:
                for s in stmts:
                    if ro and not ad.is_query(s):
                        out(ok=False, error="This connection is read-only: only SELECT / WITH / SHOW / EXPLAIN style statements run.")
                    r = cn.exec_driver_sql(s)
                    done += 1
                    if r.returns_rows:
                        rows = r.fetchmany(limit + 1)
                        res = {"columns": list(r.keys()), "rows": [[cell(v) for v in row] for row in rows[:limit]],
                               "rowcount": None, "truncated": len(rows) > limit}
                    else:
                        res = {"columns": [], "rows": [], "rowcount": r.rowcount, "truncated": False}
                    if not ro:
                        cn.commit()
            out(ok=True, statements=done, ms=int((time.time() - t0) * 1000), **res)
        out(ok=False, error="unknown action")
    except SystemExit:
        raise
    except Exception as e:
        msg = str(getattr(e, "orig", None) or e)
        out(ok=False, error=msg[:1500], ms=int((time.time() - t0) * 1000))


if __name__ == "__main__":
    main()
