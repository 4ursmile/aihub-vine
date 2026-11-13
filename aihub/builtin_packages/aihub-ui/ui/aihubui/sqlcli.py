#!/usr/bin/env python3
"""SQL for Claude Code chats: talks to the running AI Hub UI (127.0.0.1) so saved connections and their secrets stay inside it. Stdlib only.

  sqlcli.py connections                      list saved connections (names, ids, read-only flag)
  sqlcli.py schema [CONN ...] [--table T]    tables + columns of the connections (default: all)
  sqlcli.py query CONN "SELECT ..." [--limit N]    run a READ-ONLY statement (SQL may also come from stdin)"""
import json
import os
import sys
import urllib.error
import urllib.request


def _ui():
    home = os.environ.get("AIHUB_HOME") or os.path.join(os.path.expanduser("~"), ".aihub")
    with open(os.path.join(home, "ui", "url.txt"), encoding="utf-8") as f:
        url = f.read().strip()
    base, _, tok = url.partition("/?t=")
    return base, tok


def call(method, path, body=None):
    try:
        base, tok = _ui()
    except OSError:
        sys.exit("The AI Hub UI is not running (no ~/.aihub/ui/url.txt). Start it with: aihub run start aihub-ui")
    req = urllib.request.Request(base + "/api/" + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Content-Type": "application/json", "Cookie": "aihub_ui=" + tok, "X-AIHub-UI": "1"})
    try:
        with urllib.request.urlopen(req, timeout=330) as r:
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
    opts = {}
    for n in ("--table", "--limit"):
        if n in rest:
            i = rest.index(n)
            opts[n] = rest[i + 1] if i + 1 < len(rest) else ""
            del rest[i:i + 2]
    if cmd == "connections":
        for c in call("GET", "sql/connections").get("connections", []):
            print("%s  id=%s  %s%s" % (c["name"], c["id"], c["driver"], "  read-only" if c.get("readonly") else ""))
    elif cmd == "schema":
        print(call("POST", "sql/ai/context", {"conns": rest or None, "table": opts.get("--table", "")})["text"])
    elif cmd == "query":
        if not rest:
            sys.exit("usage: sqlcli.py query CONN \"SELECT ...\"")
        sql = " ".join(rest[1:]) if len(rest) > 1 else sys.stdin.read()
        r = call("POST", "sql/ai/query", {"conn": rest[0], "sql": sql, "limit": int(opts.get("--limit") or 30)})
        print(r["text"])
        sys.exit(0 if r.get("ok") else 1)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
