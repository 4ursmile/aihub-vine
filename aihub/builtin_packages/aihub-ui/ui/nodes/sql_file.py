#!/usr/bin/env python3
"""Saved SQL file node. JSON on stdin ({config, input, inputs, workdir}), JSON on stdout. Stdlib only.

Runs a SQL file saved in SQL Studio (Save button) through the running AI Hub UI, so the connection's password and Python environment stay
there. The file's `:name` parameters keep their saved values unless the node's `params` (a JSON object) overrides them; {{data.x}} is
filled in before this runs. The result is a table (markdown), JSON or CSV; rows, columns and row_count are also handed on as data."""
import csv
import io
import json
import os
import sys
import urllib.error
import urllib.request


def done(**k):
    sys.stdout.write("\n" + json.dumps(k, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()
    sys.exit(0)


def fail(msg):
    done(ok=False, error=msg)


def ui():
    home = os.environ.get("AIHUB_HOME") or os.path.join(os.path.expanduser("~"), ".aihub")
    try:
        with open(os.path.join(home, "ui", "url.txt"), encoding="utf-8") as f:
            url = f.read().strip()
    except OSError:
        fail("The AI Hub UI is not running (no ~/.aihub/ui/url.txt): start it with `aihub run start aihub-ui`.")
    base, _, tok = url.partition("/?t=")
    return base, tok


def call(path, body, timeout=330):
    base, tok = ui()
    rq = urllib.request.Request(base + "/api/" + path, data=json.dumps(body).encode(), method="POST",
                                headers={"Content-Type": "application/json", "Cookie": "aihub_ui=" + tok, "X-AIHub-UI": "1"})
    try:
        with urllib.request.urlopen(rq, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            fail(json.loads(e.read().decode()).get("error") or str(e))
        except ValueError:
            fail(str(e))
    except OSError as e:
        fail("Cannot reach the AI Hub UI: %s" % e)


def md_table(cols, rows):
    esc = lambda v: ("" if v is None else str(v)).replace("|", "\\|").replace("\n", " ")
    return "\n".join(["| " + " | ".join(esc(c) for c in cols) + " |", "|" + "---|" * len(cols)] + ["| " + " | ".join(esc(v) for v in r) + " |" for r in rows])


def main():
    d = json.loads(sys.stdin.read() or "{}")
    cfg = d.get("config") or {}
    name = str(cfg.get("file") or "").strip()
    if not name:
        fail("Set the SQL file (its name in SQL Studio).")
    params = {}
    if str(cfg.get("params") or "").strip():
        try:
            params = json.loads(cfg["params"])
        except ValueError as e:
            fail("Parameters must be a JSON object: %s" % e)
        if not isinstance(params, dict):
            fail("Parameters must be a JSON object.")
    try:
        cap = min(max(int(cfg.get("max_rows") or 1000), 1), 50000)
    except (TypeError, ValueError):
        cap = 1000
    r = call("sql/file/run", {"file": name, "conn": cfg.get("connection") or "", "params": params, "limit": cap})
    if not r.get("ok"):
        fail(r.get("error") or "the query failed")
    cols, rows = r.get("columns") or [], r.get("rows") or []
    objs = [dict(zip(cols, x)) for x in rows]
    kind = str(cfg.get("output") or "table")
    if kind == "json":
        text = json.dumps(objs, ensure_ascii=False, default=str, indent=1)
    elif kind == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        w.writerows(rows)
        text = buf.getvalue()
    elif cols:
        text = md_table(cols, rows) + ("\n\n(first %d rows)" % len(rows) if r.get("truncated") else "")
    else:
        text = "%d statement(s) ran" % (r.get("statements") or 1)
    if len(text) > 200_000:
        text = text[:200_000] + "\n... [cut; lower Max rows]"
    done(ok=True, output=text, data={"rows": objs, "columns": cols, "row_count": len(rows), "params": r.get("params") or {}},
         log="%s on %s: %d rows in %s ms%s" % (r.get("file"), r.get("conn"), len(rows), r.get("ms"), " (params: %s)" % json.dumps(r.get("params")) if r.get("params") else ""))


if __name__ == "__main__":
    main()
