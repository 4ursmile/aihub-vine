"""Shared by the Slack / Discord / webhook send nodes: read the JSON on stdin, POST JSON, answer with one JSON line."""
import json
import re
import sys
import urllib.error
import urllib.request


def start():
    d = json.load(sys.stdin)
    return d, d["config"]


def done(**k):
    print(json.dumps(k, ensure_ascii=False))
    sys.exit(0)


def post(url, body, who, headers=None, method="POST", ok_codes=(200, 201, 202, 204)):
    """POST `body` (dict or str). Returns (status, text). Ends the program with an error line on failure; the URL is never echoed."""
    if not str(url).startswith("https://"):
        done(ok=False, error="the %s URL must start with https://" % who)
    data = body.encode("utf-8") if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode("utf-8")
    h = {"Content-Type": "application/json", "User-Agent": "aihub-ui-work/1"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read(4000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        done(ok=False, error="%s answered %d: %s" % (who, e.code, e.read(300).decode("utf-8", "replace").replace(url, "***")))
    except (urllib.error.URLError, OSError) as e:
        done(ok=False, error="cannot reach %s: %s" % (who, str(getattr(e, "reason", e)).replace(url, "***")))
