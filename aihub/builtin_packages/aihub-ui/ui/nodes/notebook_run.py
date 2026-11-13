#!/usr/bin/env python3
"""Jupyter notebook node. JSON on stdin ({config, input, inputs, workdir}), JSON on stdout. Stdlib only.

Asks the running AI Hub UI (127.0.0.1) to run a notebook on one of its saved Jupyter servers (Notebook page): a fresh kernel, `params` set like
papermill (right after the cell tagged `parameters`, else before the first cell), every code cell in order (SQL cells run through SQL Studio and may
fill a pandas DataFrame), then the variable `result_var` is read back as JSON. The server's token never leaves the UI."""
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
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


def call(method, path, body=None, timeout=60):
    base, tok = ui()
    rq = urllib.request.Request(base + "/api/" + path, data=json.dumps(body).encode() if body is not None else None, method=method,
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


def main():
    d = json.loads(sys.stdin.read() or "{}")
    cfg = d.get("config") or {}
    server, path = str(cfg.get("server") or "").strip(), str(cfg.get("path") or "").strip()
    if not path:
        fail("Set the notebook path.")
    params = {}
    if str(cfg.get("params") or "").strip():
        try:
            params = json.loads(cfg["params"])
        except ValueError as e:
            fail("Parameters must be a JSON object: %s" % e)
        if not isinstance(params, dict):
            fail("Parameters must be a JSON object.")
    try:
        limit = max(5, int(cfg.get("timeout") or 600) - 20)       # the node's own limit leaves room to stop the kernel and report
    except (TypeError, ValueError):
        limit = 600
    jid = call("POST", "jupyter/run/start", {"server": server, "path": path, "params": params, "kernel": cfg.get("kernel") or "", "timeout": limit,
                                              "continue": bool(cfg.get("continue")), "save_as": cfg.get("save_as") or "", "result_var": cfg.get("result_var", "result")})["id"]

    def stop(*_):
        try:
            call("POST", "jupyter/run/stop", {"id": jid}, 10)
        finally:
            sys.exit(1)
    signal.signal(signal.SIGTERM, stop)
    after, end = 0, time.time() + limit + 120
    while time.time() < end:
        v = call("GET", "jupyter/run/status?id=%s&after=%d" % (jid, after))
        for ln in v["lines"]:
            sys.stdout.write(ln + "\n")
            sys.stdout.flush()
        after = v["next"]
        if v["state"] == "error":
            fail(v["error"])
        if v["state"] == "done":
            r = v["result"]
            data = {"ok": r["ok"], "cells": r["ran"], "seconds": r["seconds"], "last": r["last"]}
            if r.get("result") is not None:
                data["result"] = r["result"]
                if isinstance(r["result"], dict):
                    data.update({k: x for k, x in r["result"].items() if k not in data})
            if r.get("saved"):
                data["saved"] = r["saved"]
            if not r["ok"]:
                fail(r["error"] or "the notebook failed")
            out = r["last"] if r.get("result") is None else json.dumps(r["result"], ensure_ascii=False, default=str)
            done(ok=True, output=out, data=data, log="%d cells in %.1fs" % (r["ran"], r["seconds"]))
        time.sleep(1)
    fail("The notebook run did not finish in time.")


if __name__ == "__main__":
    main()
