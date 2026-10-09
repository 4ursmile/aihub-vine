"""One place for the CLI's HTTPS trust settings, used by every call it makes (hub, Langfuse, index over https, git).

Verification is ON by default. Two opt-in overrides, each from the environment or `aihub config set`:
  ca_bundle    / AIHUB_CA_BUNDLE   path to a PEM file with your internal CA (preferred: keeps verification)
  insecure_tls / AIHUB_INSECURE=1  skip certificate checks (self-signed hubs, test setups only)
"""
import os
import ssl

from . import paths

TRUE = ("1", "true", "yes", "on")


def insecure():
    v = os.environ.get("AIHUB_INSECURE")
    if v is None:
        v = paths.load("config.json", {}).get("insecure_tls", "")
    return str(v).strip().lower() in TRUE


def ca_bundle():
    return os.environ.get("AIHUB_CA_BUNDLE") or paths.load("config.json", {}).get("ca_bundle") or ""


def context(certifi_default=False):
    """An ssl context for the current settings, or None for Python's default behaviour (system trust store).
    certifi_default=True keeps the old Langfuse behaviour of using certifi's CA list when nothing is configured."""
    if insecure():
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    ca = ca_bundle()
    if ca:
        return ssl.create_default_context(cafile=os.path.expanduser(ca))
    if not certifi_default:
        return None
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


def git_env():
    """Extra environment for git so it follows the same trust settings."""
    if insecure():
        return {"GIT_SSL_NO_VERIFY": "1"}
    ca = ca_bundle()
    return {"GIT_SSL_CAINFO": os.path.expanduser(ca)} if ca else {}


def hint(err):
    """A short next step when an error looks like a certificate problem, else ''."""
    t = str(err)
    if "CERTIFICATE_VERIFY_FAILED" in t or "certificate" in t.lower() and "verify" in t.lower() or "SSL" in t and "self" in t:
        return " (certificate not trusted: set AIHUB_CA_BUNDLE=<ca.pem> or `aihub config set ca_bundle <ca.pem>`; as a last resort `aihub config set insecure_tls true`)"
    return ""
