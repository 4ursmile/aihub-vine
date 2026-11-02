"""The hub API with the aihub CLI's own login (~/.aihub/config.json `hub`, credentials.json `token`; AIHUB_HOME / AIHUB_URL honoured), for
the Pub/Sub nodes. TLS like the CLI: ca_bundle / AIHUB_CA_BUNDLE, insecure_tls / AIHUB_INSECURE=1. Stdlib only."""
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request


class HubError(Exception):
    def __init__(self, msg, code=0):
        Exception.__init__(self, msg)
        self.code = code


def _home():
    return os.environ.get("AIHUB_HOME") or os.path.join(os.path.expanduser("~"), ".aihub")


def _load(name):
    try:
        with open(os.path.join(_home(), name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _ctx(cfg):
    if os.environ.get("AIHUB_INSECURE") == "1" or str(cfg.get("insecure_tls", "")).lower() in ("1", "true", "yes", "on"):
        c = ssl.create_default_context()
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
        return c
    ca = os.environ.get("AIHUB_CA_BUNDLE") or cfg.get("ca_bundle") or ""
    return ssl.create_default_context(cafile=ca) if ca else None


def call(method, path, body=None, timeout=30):
    cfg = _load("config.json")
    hub = (cfg.get("hub") or os.environ.get("AIHUB_URL") or "").rstrip("/")
    if not hub:
        raise HubError("no hub configured: run `aihub config set hub URL` and `aihub login`")
    h = {"Accept": "application/json", "User-Agent": "aihub-ui-pubsub/1"}
    tok = _load("credentials.json").get("token")
    if tok:
        h["Authorization"] = "Bearer " + tok
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        h["Content-Type"] = "application/json"
    rq = urllib.request.Request(hub + "/api/v1" + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(rq, timeout=timeout, context=_ctx(cfg)) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("detail", e.reason)
        except Exception:
            msg = e.reason
        raise HubError("hub answered %d: %s" % (e.code, msg), e.code)
    except (urllib.error.URLError, OSError) as e:
        raise HubError("cannot reach the hub %s: %s" % (hub, getattr(e, "reason", e)))


def qs(**kw):
    return urllib.parse.urlencode({k: v for k, v in kw.items() if v not in (None, "")})
