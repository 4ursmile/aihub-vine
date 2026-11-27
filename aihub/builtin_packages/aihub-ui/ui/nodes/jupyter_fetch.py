#!/usr/bin/env python3
"""Files from Jupyter: copies a file or folder from one of the saved Jupyter servers (Notebook page: JupyterHub, Jupyter Server, the local one)
to ~/.aihub/workingspace/data/jupyter/SERVER/PATH and hands the PATH on, never the content. The server's token stays in the running UI: this node
asks it (127.0.0.1) to list and read. JSON on stdin, JSON on stdout. Stdlib only."""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _remote as R  # noqa: E402


def ui():
    try:
        with open(os.path.join(R.HOME, "ui", "url.txt"), encoding="utf-8") as f:
            url = f.read().strip()
    except OSError:
        R.done(ok=False, error="The AI Hub UI is not running (no ~/.aihub/ui/url.txt): start it with `aihub run start aihub-ui`.")
    base, _, tok = url.partition("/?t=")
    return base, tok


def call(path, body, timeout=120):
    base, tok = ui()
    rq = urllib.request.Request(base + "/api/" + path, data=json.dumps(body).encode(), method="POST",
                                headers={"Content-Type": "application/json", "Cookie": "aihub_ui=" + tok, "X-AIHub-UI": "1"})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # loopback: never through a system proxy
    try:
        with op.open(rq, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8", "replace")).get("error") or str(e)
        except ValueError:
            msg = str(e)
        raise RuntimeError(msg)
    except OSError as e:
        raise RuntimeError("Cannot reach the AI Hub UI: %s" % e)


def main():
    d = json.loads(sys.stdin.read() or "{}")
    cfg = d.get("config") or {}
    server = str(cfg.get("server") or "").strip()
    rpath = str(cfg.get("path") or "").strip().strip("/")
    if not server:
        R.done(ok=False, error="Set the Jupyter server (its saved name on the Notebook page).")
    if "\0" in rpath or ".." in rpath.replace("\\", "/").split("/"):
        R.done(ok=False, error="The path must stay inside the server's folder (no ..).")
    max_file = int(R.number(cfg, "max_mb", 200, 1, 2000) * 1_000_000)
    max_files = int(R.number(cfg, "max_files", 2000, 1, 20000))
    deadline = time.time() + R.number(cfg, "timeout", 600, 10, 3600) - 15

    try:
        top = call("jupyter/file/get", {"server": server, "path": rpath}, 60) if rpath else {"type": "directory"}
    except RuntimeError as e:
        R.done(ok=False, error="%s: %s" % (rpath or "/", e))
    is_dir = top.get("type") == "directory"
    dest = R.dest_for("jupyter", server, rpath)
    tmp = R.stage(dest)
    got, skipped = [0, 0], []

    def save(item, local):
        if time.time() > deadline:
            raise TimeoutError()
        if got[0] >= max_files:
            skipped.append(item.get("path") or "")
            return
        size = item.get("size")
        if isinstance(size, int) and size > max_file:
            skipped.append("%s (%s)" % (item.get("path"), R.human(size)))
            return
        f = item if item.get("content") is not None else call("jupyter/file/get", {"server": server, "path": item["path"]})
        c, fmt = f.get("content"), f.get("format")
        if f.get("type") == "notebook" or fmt == "json":
            raw = (json.dumps(c, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
        elif fmt == "base64":
            raw = base64.b64decode(c or "")
        else:
            raw = str(c or "").encode("utf-8")
        if len(raw) > max_file:
            skipped.append("%s (%s)" % (item.get("path"), R.human(len(raw))))
            return
        with open(local, "wb") as fh:
            fh.write(raw)
        got[0] += 1
        got[1] += len(raw)
        print("got %s (%s)" % (item.get("path"), R.human(len(raw))), flush=True)

    def walk(remote, local):
        os.makedirs(local, exist_ok=True)
        for it in call("jupyter/ls", {"server": server, "path": remote}).get("items") or []:
            name = str(it.get("name") or "")
            if not name or name in (".", "..") or "/" in name or "\\" in name or name.startswith(".ipynb_checkpoints"):
                continue
            target = os.path.join(local, name)
            if it.get("type") == "directory":
                walk(it.get("path") or (remote + "/" + name).strip("/"), target)
            else:
                save(it, target)

    try:
        if is_dir:
            walk(rpath, tmp)
            out = tmp
        else:
            out = os.path.join(tmp, os.path.basename(rpath))
            save(dict(top, path=rpath), out)
            if not got[0]:
                R.drop(tmp)
                R.done(ok=False, error="%s is larger than the limit (%d MB): raise 'Max MB per file'." % (rpath, max_file // 1_000_000))
    except TimeoutError:
        R.drop(tmp)
        R.done(ok=False, error="Copying took longer than the node's time limit (got %d files so far)." % got[0])
    except (RuntimeError, OSError, ValueError) as e:
        R.drop(tmp)
        R.done(ok=False, error=str(e))
    R.swap(tmp, dest)
    path = dest if is_dir else os.path.join(dest, os.path.basename(rpath))
    data = {"path": path, "paths": [path], "server": server, "remote": "/" + rpath, "files": got[0], "bytes": got[1], "skipped": skipped[:50]}
    log = "copied %d file(s), %s from %s:/%s to %s (pointer only)" % (got[0], R.human(got[1]), server, rpath, path)
    if skipped:
        log += "; skipped %d over the limits" % len(skipped)
    R.done(ok=True, output=path, data=data, log=log)


if __name__ == "__main__":
    main()
