"""Scrub tool-call parameters before they are stored for security audit. Used by the CLI (before anything leaves the
machine) and again by the server (never trust the client)."""
import json
import re

SECRET_KEY = re.compile(r"pass(word|wd)?|secret|token|api[-_]?key|auth|credential|private[-_]?key|cookie|bearer|session", re.I)
SECRET_VAL = re.compile(
    r"(sk-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[\w-]{10,}|"
    r"eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{5,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)")
INLINE = re.compile(r"(?i)\b(authorization|password|passwd|token|secret|api[-_]?key)(\s*[:=]\s*|\s+)((?:bearer|basic)\s+)?[^\s'\"&]+|\bbearer\s+[^\s'\"]+")
URL_CRED = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]+):[^\s@/]+@")
STR_MAX, TOTAL_MAX, DEPTH_MAX = 300, 2000, 4


def _s(v):
    v = URL_CRED.sub(r"\1:[redacted]@", SECRET_VAL.sub("[redacted]", v))
    v = INLINE.sub(lambda m: (m.group(1) + m.group(2) + "[redacted]") if m.group(1) else "Bearer [redacted]", v)
    return v if len(v) <= STR_MAX else v[:STR_MAX] + "…(%d more chars)" % (len(v) - STR_MAX)


def _walk(v, depth):
    if isinstance(v, str):
        return _s(v)
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    if depth >= DEPTH_MAX:
        return "…"
    if isinstance(v, dict):
        return {str(k)[:60]: ("[redacted]" if SECRET_KEY.search(str(k)) else _walk(x, depth + 1)) for k, x in list(v.items())[:30]}
    if isinstance(v, (list, tuple)):
        return [_walk(x, depth + 1) for x in list(v)[:20]]
    return _s(str(v))


def params(obj):
    """Redacted, size-capped JSON string describing a tool call's arguments ('' if there are none)."""
    if not obj:
        return ""
    out = json.dumps(_walk(obj, 0), ensure_ascii=False, separators=(",", ":"))
    return out if len(out) <= TOTAL_MAX else out[:TOTAL_MAX] + "…"


def text(v, n=2000):
    """Redact an already-serialised string (server side re-check)."""
    return _s(str(v))[:n] if v else ""
