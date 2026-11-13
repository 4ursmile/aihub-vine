#!/usr/bin/env python3
"""Jupyter notebooks for Claude Code chats: talks to the running AI Hub UI (127.0.0.1) so server tokens stay inside it. Stdlib only.

  nbcli.py servers                          list saved Jupyter servers
  nbcli.py ls SERVER [FOLDER]               files of a folder on the server
  nbcli.py run SERVER PATH [--param k=v ...] [--timeout S]    run a notebook on a fresh kernel; prints each cell's output and `result`"""
import json
import os
import sys
import time
import urllib.error
import urllib.request


def call(method, path, body=None):
    home = os.environ.get("AIHUB_HOME") or os.path.join(os.path.expanduser("~"), ".aihub")
    try:
        with open(os.path.join(home, "ui", "url.txt"), encoding="utf-8") as f:
            base, _, tok = f.read().strip().partition("/?t=")
    except OSError:
        sys.exit("The AI Hub UI is not running (no ~/.aihub/ui/url.txt). Start it with: aihub run start aihub-ui")
    req = urllib.request.Request(base + "/api/" + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Content-Type": "application/json", "Cookie": "aihub_ui=" + tok, "X-AIHub-UI": "1"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            sys.exit(json.loads(e.read().decode()).get("error") or str(e))
        except ValueError:
            sys.exit(str(e))
    except OSError as e:
        sys.exit("Cannot reach the AI Hub UI: %s" % e)


def main(a):
    if not a or a[0] in ("-h", "--help"):
        print(__doc__)
        return
    cmd, rest = a[0], a[1:]
    if cmd == "servers":
        for s in call("GET", "jupyter/servers")["servers"]:
            print("%s  id=%s  %s" % (s["name"], s["id"], s["kind"]))
    elif cmd == "ls" and rest:
        for i in call("POST", "jupyter/ls", {"server": rest[0], "path": rest[1] if len(rest) > 1 else ""})["items"]:
            print(i["path"] + ("/" if i["type"] == "directory" else ""))
    elif cmd == "run" and len(rest) >= 2:
        params, timeout, pos, i = {}, 300, [], 0
        while i < len(rest):
            if rest[i] == "--param" and i + 1 < len(rest):
                k, _, v = rest[i + 1].partition("=")
                try:
                    v = json.loads(v)
                except ValueError:
                    pass
                params[k] = v
                i += 2
            elif rest[i] == "--timeout" and i + 1 < len(rest):
                timeout, i = int(rest[i + 1]), i + 2
            else:
                pos.append(rest[i])
                i += 1
        jid = call("POST", "jupyter/run/start", {"server": pos[0], "path": pos[1], "params": params, "timeout": timeout})["id"]
        after = 0
        while True:
            v = call("GET", "jupyter/run/status?id=%s&after=%d" % (jid, after))
            after = v["next"]
            if v["state"] == "error":
                sys.exit(v["error"])
            if v["state"] == "done":
                r = v["result"]
                print("%s: %d cells in %ss%s" % ("OK" if r["ok"] else "FAILED", r["ran"], r["seconds"], ("; " + r["error"]) if r["error"] else ""))
                if r.get("result") is not None:
                    print("result =", json.dumps(r["result"], default=str)[:3000])
                print()
                print(r["text"])
                sys.exit(0 if r["ok"] else 1)
            time.sleep(1)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
