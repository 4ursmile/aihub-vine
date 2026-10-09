"""Minimal Langfuse client (stdlib): idempotent span writes, paged reads."""
import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import paths


def settings():
    """Lookup order for each value: environment > ~/.aihub/config.json > aihub/core/defaults.json (langfuse.host)."""
    from ..core import defaults
    c = paths.config()
    host = (os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST") or c.get("langfuse_host")
            or defaults.load().get("langfuse", {}).get("host") or "")
    return {
        "host": host.rstrip("/"),
        "public": os.environ.get("LANGFUSE_PUBLIC_KEY") or c.get("langfuse_public_key") or "",
        "secret": os.environ.get("LANGFUSE_SECRET_KEY") or c.get("langfuse_secret_key") or "",
    }


def configured(s=None):
    s = s or settings()
    return bool(s["host"] and s["public"] and s["secret"])


def now_minus(seconds):
    return datetime.fromtimestamp(time.time() - seconds, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def span_id(*parts):
    """Deterministic id so retries / multiple pollers never duplicate."""
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:32]


def _ctx():
    from . import tls
    return tls.context(certifi_default=True)


def _req(s, method, path, body=None, timeout=10):
    auth = base64.b64encode(("%s:%s" % (s["public"], s["secret"])).encode()).decode()
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(s["host"] + path, data=data, method=method,
                               headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout, context=_ctx()) as resp:
        return json.loads(resp.read() or b"{}")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _attr(k, v):
    if isinstance(v, bool):
        return {"key": k, "value": {"boolValue": v}}
    if isinstance(v, (int, float)):
        return {"key": k, "value": {"stringValue": str(v)}}
    if not isinstance(v, str):
        v = json.dumps(v, separators=(",", ":"), default=str)
    return {"key": k, "value": {"stringValue": v}}


def make_span(name, key, metadata, user, session=None, ts=None):
    """One OTLP span. `key` is the dedup identity: same key -> same trace/span ids, so a retry, a second
    machine or a poller re-read can never create a second copy."""
    t = ts or time.time()
    ns = str(int(t * 1e9))
    at = [_attr("langfuse.user.id", user), _attr("langfuse.environment", "aihub"),
          _attr("langfuse.observation.type", "span")]
    if session:
        at.append(_attr("langfuse.session.id", session))
    for k, v in metadata.items():
        if v not in (None, ""):
            at.append(_attr("langfuse.observation.metadata." + k, v))
            at.append(_attr("langfuse.trace.metadata." + k, v))
    return {"traceId": span_id("trace", name, *key[:2])[:32], "spanId": span_id(name, *key)[:16], "name": name, "kind": 1,
            "startTimeUnixNano": ns, "endTimeUnixNano": ns, "attributes": at}


def send(spans, s=None, timeout=10):
    """Write spans through OpenTelemetry (OTLP/HTTP JSON) ingestion. Raises on failure.
    Ids are derived from the dedup key, so resending the same event never makes a second observation."""
    s = s or settings()
    body = {"resourceSpans": [{"resource": {"attributes": [_attr("service.name", "aihub")]},
                               "scopeSpans": [{"scope": {"name": "aihub"}, "spans": spans}]}]}
    auth = base64.b64encode(("%s:%s" % (s["public"], s["secret"])).encode()).decode()
    r = urllib.request.Request(s["host"] + "/api/public/otel/v1/traces", data=json.dumps(body).encode(), method="POST",
                               headers={"Authorization": "Basic " + auth, "Content-Type": "application/json",
                                        "x-langfuse-ingestion-version": "4"})
    with urllib.request.urlopen(r, timeout=timeout, context=_ctx()) as resp:
        return resp.status


def _norm_legacy(o):
    return dict(o, userId=o.get("userId"), sessionId=o.get("sessionId"), metadata=o.get("metadata") or {})


def fetch_observations(name_prefix=None, since=None, s=None, page_size=500, name=None):
    """Yield observations, oldest-first not guaranteed. Uses the v2 cursor API; falls back to the legacy
    paged API on servers that lack it. since=None means full lookback."""
    s = s or settings()
    try:
        yield from _fetch_v2(s, name_prefix, since, page_size, name)
    except urllib.error.HTTPError as e:
        if e.code not in (404, 405):
            raise
        yield from _fetch_legacy(s, name_prefix, since, min(page_size, 100), name)


def _fetch_v2(s, name_prefix, since, page_size, name):
    cursor = None
    while True:
        q = {"limit": page_size, "fields": "core,basic,metadata,time", "type": "SPAN",
             "expandMetadata": "detail,cwd,description,requires,tags,git_url,commit"}
        if since:
            q["fromStartTime"] = since
        if name:
            q["name"] = name
        if cursor:
            q["cursor"] = cursor
        r = _req(s, "GET", "/api/public/v2/observations?" + urllib.parse.urlencode(q), timeout=60)
        for o in r.get("data", []):
            if not name_prefix or (o.get("name") or "").startswith(name_prefix):
                yield o
        cursor = (r.get("meta") or {}).get("cursor")
        if not cursor:
            return


def _fetch_legacy(s, name_prefix, since, page_size, name):
    page = 1
    while True:
        q = {"page": page, "limit": page_size, "type": "SPAN"}
        if since:
            q["fromStartTime"] = since
        if name:
            q["name"] = name
        r = _req(s, "GET", "/api/public/observations?" + urllib.parse.urlencode(q), timeout=60)
        for o in r.get("data", []):
            if not name_prefix or (o.get("name") or "").startswith(name_prefix):
                yield _norm_legacy(o)
        if page >= (r.get("meta") or {}).get("totalPages", 1):
            return
        page += 1
