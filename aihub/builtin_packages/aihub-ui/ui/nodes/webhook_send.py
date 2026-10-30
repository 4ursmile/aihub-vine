#!/usr/bin/env python3
"""Send to webhook: POSTs (or PUTs) JSON to any https URL. JSON on stdin, JSON on stdout."""
import json
import _hook

d, cfg = _hook.start()
url = str(cfg.get("url") or "").strip()
text = str(cfg.get("body") or d.get("input") or "")
method = str(cfg.get("method") or "POST").upper()
if method not in ("POST", "PUT", "PATCH"):
    _hook.done(ok=False, error="unsupported method " + method)
if str(cfg.get("format") or "json") == "json":
    try:
        body = json.loads(text)
    except ValueError:
        body = {"text": text}                                # plain text is wrapped, JSON is sent as it is
else:
    body = text
headers = {}
for ln in str(cfg.get("headers") or "").splitlines():
    if ":" in ln and not ln.strip().startswith("#"):
        k, v = ln.split(":", 1)
        if _hook.re.match(r"^[A-Za-z0-9-]{1,60}$", k.strip()):
            headers[k.strip()] = v.strip()
if str(cfg.get("secret") or "").strip():
    headers["Authorization"] = str(cfg["secret"]).strip().replace("\n", "")
if str(cfg.get("format") or "json") != "json":
    headers.setdefault("Content-Type", "text/plain; charset=utf-8")
code, resp = _hook.post(url, body, "the webhook", headers, method)
_hook.done(ok=True, output=resp or text, status=str(code), log="Webhook answered HTTP %d" % code)
