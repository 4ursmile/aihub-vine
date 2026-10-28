#!/usr/bin/env python3
"""HTTP request: call a URL and pass the response on. JSON on stdin, JSON on stdout. Secrets (the authorization value) arrive on stdin only."""
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

d = json.load(sys.stdin)
cfg = d["config"]
CAP = 400_000


def done(**k):
    print(json.dumps(k, ensure_ascii=False))
    sys.exit(0)


url = str(cfg.get("url") or "").strip()
if urllib.parse.urlsplit(url).scheme not in ("http", "https") or not urllib.parse.urlsplit(url).netloc:
    done(ok=False, error="the URL must start with http:// or https://")
method = str(cfg.get("method") or "GET").upper()
if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
    done(ok=False, error="unsupported method " + method)
headers = {"User-Agent": "aihub-ui-work/1", "Accept": "*/*"}
for ln in str(cfg.get("headers") or "").splitlines():
    if ":" in ln and not ln.strip().startswith("#"):
        k, v = ln.split(":", 1)
        if re.match(r"^[A-Za-z0-9-]{1,60}$", k.strip()) and "\n" not in v:
            headers[k.strip()] = v.strip()
auth = str(cfg.get("authorization") or "").strip()
if auth:
    if "\n" in auth or "\r" in auth:
        done(ok=False, error="the authorization value must be one line")
    headers["Authorization"] = auth
body = None
if method in ("POST", "PUT", "PATCH", "DELETE") and str(cfg.get("body") or ""):
    raw = str(cfg["body"])
    kind = cfg.get("body_type") or "json"
    if kind == "json":
        try:
            json.loads(raw)
        except ValueError as e:
            done(ok=False, error="the body is not valid JSON: %s" % e)
    body = raw.encode("utf-8")
    headers.setdefault("Content-Type", {"json": "application/json", "form": "application/x-www-form-urlencoded", "text": "text/plain; charset=utf-8"}.get(kind, "text/plain"))
try:
    timeout = min(max(int(cfg.get("timeout") or 30), 1), 300)
except ValueError:
    timeout = 30
req = urllib.request.Request(url, data=body, headers=headers, method=method)
status, rh, raw = 0, {}, b""
try:
    with urllib.request.urlopen(req, timeout=timeout) as r:
        status, rh, raw = r.status, dict(r.headers), r.read(CAP + 1)
except urllib.error.HTTPError as e:
    status, rh, raw = e.code, dict(e.headers), e.read(CAP + 1)
except (urllib.error.URLError, OSError, ValueError) as e:
    done(ok=False, error="request failed: %s" % getattr(e, "reason", e))
text = raw[:CAP].decode("utf-8", "replace")
if len(raw) > CAP:
    text += "\n... [cut at %d bytes]" % CAP
if cfg.get("fail_on_error") is not False and status >= 400:
    done(ok=False, error="HTTP %d: %s" % (status, text[:300].replace("\n", " ")))
data = {"status": status, "content_type": rh.get("Content-Type", "")}
try:
    j = json.loads(text)
    data["json"] = j
    path = str(cfg.get("pick") or "").strip()
    if path:                                              # data.items.0.name
        cur = j
        for k in path.split("."):
            cur = cur[int(k)] if isinstance(cur, list) and k.isdigit() and int(k) < len(cur) else cur.get(k) if isinstance(cur, dict) else None
            if cur is None:
                break
        if cur is None:
            done(ok=False, error="nothing at %r in the JSON answer" % path)
        text = cur if isinstance(cur, str) else json.dumps(cur, indent=2, ensure_ascii=False)
    elif cfg.get("pretty", True):
        text = json.dumps(j, indent=2, ensure_ascii=False)
except ValueError:
    pass
done(ok=True, output=text, data=data, log="%s %s -> HTTP %d, %d bytes" % (method, urllib.parse.urlsplit(url)._replace(query="", fragment="").geturl(), status, len(raw)))
