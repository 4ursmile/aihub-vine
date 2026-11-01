#!/usr/bin/env python3
"""Trino node: runs SQL on a Trino (or Presto) coordinator over its HTTP protocol. Stdlib only, no driver to install.

JSON on stdin ({config, input, inputs, workdir}), JSON on stdout. Several statements (`;` between them) run one after the other in ONE
session: SET SESSION / USE / START TRANSACTION carry over, like in the Trino CLI. `-- %% name` before a statement names that step.
The LAST statement that returns rows is the result: a table (markdown), JSON, CSV, or a file in the node folder (`path`, for big results:
rows are streamed to the file, nothing is kept in memory). Login: a 'trino' credential set (host, port, user, password ...) or the fields."""
import base64
import csv
import http.client
import io
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

MAX_FILE_ROWS = 50_000_000
TEXT_CAP = 200_000
NAME = re.compile(r"^\s*--\s*%%\s*(.*)$")


def done(**k):
    sys.stdout.write("\n" + json.dumps(k, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()
    sys.exit(0)


def fail(msg):
    done(ok=False, error=msg)


def say(msg):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def statements(sql):
    """Split on `;` outside quotes and comments. -> [(name, statement)]. A `-- %% name` line names the statement after it."""
    out, buf, name, i, n = [], [], "", 0, len(sql)
    q = None
    while i < n:
        c = sql[i]
        if q:
            buf.append(c)
            if c == q:
                if i + 1 < n and sql[i + 1] == q:                     # '' or "" inside a quoted string
                    buf.append(sql[i + 1])
                    i += 1
                else:
                    q = None
        elif c in "'\"":
            q = c
            buf.append(c)
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            m = NAME.match(sql[i:j])
            if m and not "".join(buf).strip():
                name = m.group(1).strip()
            elif not m:
                buf.append(sql[i:j])
            i = j
            continue
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
            buf.append(sql[i:j])
            i = j
            continue
        elif c == ";":
            st = "".join(buf).strip()
            if st:
                out.append((name, st))
            buf, name = [], ""
        else:
            buf.append(c)
        i += 1
    st = "".join(buf).strip()
    if st:
        out.append((name, st))
    return [(nm or " ".join(st.split()[:3])[:40], st) for nm, st in out]


class Trino:
    """A tiny client: one session (catalog, schema, session properties, transaction) kept across statements."""

    def __init__(self, cfg, deadline):
        scheme = str(cfg.get("http_scheme") or "").strip().lower() or ("http" if str(cfg.get("port") or "") == "8080" and not cfg.get("password") else "https")
        host = str(cfg.get("host") or "").strip()
        if "://" in host:                                          # a full URL in the host field
            scheme, host = host.split("://", 1)
        host = host.rstrip("/")
        if not host:
            fail("set the Trino host (a 'trino' credential set or the Host field)")
        port = str(cfg.get("port") or "").strip()
        self.base = "%s://%s%s" % (scheme, host, (":" + port) if port and ":" not in host else "")
        self.user = str(cfg.get("user") or os.environ.get("USER") or "aihub")
        self.password = str(cfg.get("password") or "")
        if self.password and scheme != "https":
            fail("Trino sends a password only over https: set the scheme to https (or remove the password)")
        self.catalog, self.schema = str(cfg.get("catalog") or ""), str(cfg.get("schema") or "")
        self.session, self.txn, self.prepared = {}, None, {}
        verify = str(cfg.get("verify", "true")).strip().lower() not in ("false", "0", "no", "off")
        self.ctx = ssl.create_default_context() if verify else ssl._create_unverified_context()
        self.deadline = deadline

    def _headers(self):
        h = {"X-Trino-User": self.user, "X-Trino-Source": "aihub-works", "Content-Type": "text/plain; charset=utf-8", "Accept": "application/json"}
        if self.catalog:
            h["X-Trino-Catalog"] = self.catalog
        if self.schema:
            h["X-Trino-Schema"] = self.schema
        if self.session:
            h["X-Trino-Session"] = ",".join("%s=%s" % (k, urllib.request.quote(v)) for k, v in self.session.items())
        if self.txn:
            h["X-Trino-Transaction-Id"] = self.txn
        if self.prepared:
            h["X-Trino-Prepared-Statement"] = ",".join("%s=%s" % (k, urllib.request.quote(v)) for k, v in self.prepared.items())
        if self.password:
            h["Authorization"] = "Basic " + base64.b64encode(("%s:%s" % (self.user, self.password)).encode()).decode()
        return h

    def _call(self, method, url, body=None):
        for attempt in range(60):
            left = self.deadline - time.time()
            if left <= 0:
                raise TimeoutError("timed out")
            req = urllib.request.Request(url, data=body.encode("utf-8") if body is not None else None, method=method, headers=self._headers())
            try:
                with urllib.request.urlopen(req, timeout=min(60, max(1, left)), context=self.ctx if url.startswith("https") else None) as r:
                    self._track(r.headers)
                    raw = r.read()
                    return json.loads(raw.decode("utf-8")) if raw else {}
            except urllib.error.HTTPError as e:
                if e.code in (502, 503, 504):                         # the coordinator is busy: the protocol says retry
                    time.sleep(min(2, 0.1 * (attempt + 1)))
                    continue
                txt = e.read().decode("utf-8", "replace")[:500]
                if e.code == 401:
                    raise RuntimeError("Trino refused the login (401): check user / password in the credential set")
                raise RuntimeError("Trino answered %s: %s" % (e.code, txt.strip() or e.reason))
            except urllib.error.URLError as e:
                raise RuntimeError("cannot reach Trino at %s: %s" % (self.base, getattr(e, "reason", e)))
            except (OSError, http.client.HTTPException, ValueError) as e:      # dropped connection, garbled answer
                if isinstance(e, TimeoutError) or "timed out" in str(e):
                    raise TimeoutError("timed out")
                raise RuntimeError("Trino at %s: %s" % (self.base, e or type(e).__name__))
        raise RuntimeError("Trino stayed busy")

    def _track(self, h):
        """Session changes the server asks the client to remember (USE, SET SESSION, transactions, PREPARE)."""
        if h.get("X-Trino-Set-Catalog"):
            self.catalog = h["X-Trino-Set-Catalog"]
        if h.get("X-Trino-Set-Schema"):
            self.schema = h["X-Trino-Set-Schema"]
        for v in h.get_all("X-Trino-Set-Session") or []:
            k, _, val = v.partition("=")
            self.session[k.strip()] = urllib.request.unquote(val.strip())
        for v in h.get_all("X-Trino-Clear-Session") or []:
            self.session.pop(v.strip(), None)
        if h.get("X-Trino-Started-Transaction-Id"):
            self.txn = h["X-Trino-Started-Transaction-Id"]
        if h.get("X-Trino-Clear-Transaction-Id"):
            self.txn = None
        for v in h.get_all("X-Trino-Added-Prepare") or []:
            k, _, val = v.partition("=")
            self.prepared[k.strip()] = urllib.request.unquote(val.strip())
        for v in h.get_all("X-Trino-Deallocated-Prepare") or []:
            self.prepared.pop(urllib.request.unquote(v.strip()), None)

    def run(self, sql, on_rows):
        """Run one statement; ON_ROWS(columns, rows) gets each page of rows. -> (columns, stats)."""
        doc = self._call("POST", self.base + "/v1/statement", sql)
        cols = None
        while True:
            if doc.get("error"):
                e = doc["error"]
                loc = e.get("errorLocation") or {}
                raise RuntimeError("%s%s: %s" % (e.get("errorName") or "query failed", " (line %s, column %s)" % (loc.get("lineNumber"), loc.get("columnNumber")) if loc else "", e.get("message") or ""))
            if doc.get("columns") and cols is None:
                cols = [c.get("name") for c in doc["columns"]]
            if doc.get("data"):
                on_rows(cols or [], doc["data"])
            nxt = doc.get("nextUri")
            if not nxt:
                return cols, doc.get("stats") or {}
            try:
                doc = self._call("GET", nxt)
            except TimeoutError:
                try:
                    self._call("DELETE", nxt)                      # cancel it on the cluster too
                except Exception:
                    pass
                raise


def md_table(cols, rows):
    esc = lambda v: ("" if v is None else str(v)).replace("|", "\\|").replace("\n", " ")
    return "\n".join(["| " + " | ".join(esc(c) for c in cols) + " |", "|" + "---|" * len(cols)] + ["| " + " | ".join(esc(v) for v in r) + " |" for r in rows])


def main():
    d = json.load(sys.stdin)
    cfg = d.get("config") or {}
    sql = str(cfg.get("sql") or "").strip()
    if not sql:
        fail("write the SQL to run")
    try:
        limit = min(max(int(cfg.get("timeout") or 600), 5), 6 * 3600)
        cap = min(max(int(cfg.get("max_rows") or 1000), 1), 100_000)
    except (TypeError, ValueError):
        fail("timeout and max rows must be numbers")
    out_kind = str(cfg.get("output") or "table")
    db = Trino(cfg, time.time() + limit)
    steps = statements(sql)
    if not steps:
        fail("no SQL statement found")
    ndir = os.environ.get("AIHUB_NODE_DIR") or d.get("workdir") or os.getcwd()
    fmt = str(cfg.get("file_format") or "csv")
    fname = re.sub(r"[^\w.-]", "_", str(cfg.get("file_name") or "result")) or "result"
    path = os.path.join(ndir, fname if fname.endswith("." + fmt) else fname + "." + fmt) if out_kind == "path" else ""
    last = {"cols": [], "rows": [], "count": 0, "truncated": False}
    report = []
    for k, (name, st) in enumerate(steps, 1):
        say("step %d/%d %s: running" % (k, len(steps), name))
        t0 = time.time()
        cur = {"cols": [], "rows": [], "count": 0, "truncated": False}
        is_last = k == len(steps)
        sink = {"f": None, "w": None}

        def on_rows(cols, rows, cur=cur, sink=sink, is_last=is_last):
            cur["cols"] = cols
            cur["count"] += len(rows)
            if path and is_last:                                   # stream the result into the file
                if sink["f"] is None:
                    os.makedirs(ndir, exist_ok=True)
                    sink["f"] = open(path, "w", encoding="utf-8", newline="")
                    if fmt == "csv":
                        sink["w"] = csv.writer(sink["f"])
                        sink["w"].writerow(cols)
                    elif fmt == "json":
                        sink["f"].write("[")
                for r in rows:
                    if cur["count"] > MAX_FILE_ROWS:
                        raise RuntimeError("more than %d rows: write them with INSERT / CREATE TABLE AS instead" % MAX_FILE_ROWS)
                    if fmt == "csv":
                        sink["w"].writerow(r)
                    elif fmt == "jsonl":
                        sink["f"].write(json.dumps(dict(zip(cols, r)), ensure_ascii=False, default=str) + "\n")
                    else:
                        sink["f"].write(("," if sink.get("n") else "") + json.dumps(dict(zip(cols, r)), ensure_ascii=False, default=str))
                        sink["n"] = True
            room = cap - len(cur["rows"])
            if room > 0:
                cur["rows"].extend(rows[:room])
            if len(rows) > max(room, 0):
                cur["truncated"] = True
        try:
            cols, stats = db.run(st, on_rows)
        except TimeoutError:
            fail("step %d/%d %s: timed out after %ds (the query was cancelled)" % (k, len(steps), name, limit))
        except RuntimeError as e:
            fail("step %d/%d %s failed: %s" % (k, len(steps), name, e))
        finally:
            if sink["f"] is not None:
                if fmt == "json":
                    sink["f"].write("]")
                sink["f"].close()
        dt = time.time() - t0
        if cols is not None:
            cur["cols"] = cols
            last = cur
        info = {"name": name, "seconds": round(dt, 3), "rows": cur["count"], "state": stats.get("state", ""), "processed_rows": stats.get("processedRows")}
        report.append(info)
        say("step %d/%d %s: ok (%.1fs, %d row%s)" % (k, len(steps), name, dt, cur["count"], "" if cur["count"] == 1 else "s"))
    cols, rows = last["cols"], last["rows"]
    objs = [dict(zip(cols, r)) for r in rows]
    data = {"columns": cols, "rows": objs, "row_count": last["count"], "truncated": last["truncated"], "steps": report}
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
    else:
        text = "%d statement(s) ran" % len(report)
    if len(text) > TEXT_CAP:
        text = text[:TEXT_CAP] + "\n... [cut; use output = path for big results]"
    done(ok=True, output=text, data=data, log="; ".join("%s %.1fs %d rows" % (r["name"], r["seconds"], r["rows"]) for r in report))


if __name__ == "__main__":
    main()
