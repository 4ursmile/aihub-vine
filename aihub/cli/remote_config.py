"""Pull setup values (index location, Langfuse keys) from the hub, and keep them fresh. Git credentials stay with the user."""
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from . import api, gitx, paths, tls, ui

KEYS = ("langfuse_host", "langfuse_public_key", "langfuse_secret_key")


def fetch(hub=None, code="", timeout=8):
    hub = (hub or paths.config()["hub"]).rstrip("/")
    q = "?code=" + urllib.parse.quote(code) if code else ""
    tok = paths.load("credentials.json", {}).get("token")
    rq = urllib.request.Request(hub + "/api/v1/client-config" + q, headers={"Authorization": "Bearer " + tok} if tok else {})
    if not hub.startswith("https://") and not hub.startswith("http://localhost") and not hub.startswith("http://127."):
        raise ValueError("refusing to fetch credentials over plain http (%s); use https" % hub)
    with api._opener.open(rq, timeout=timeout) as r:
        return json.loads(r.read())


def store_git(host_url, username, token):
    """Save a credential the USER typed to their own git credential helper. Never sent by the hub, never kept in our config."""
    if not (username and token and host_url):
        return False
    u = urllib.parse.urlsplit(host_url)
    payload = "protocol=%s\nhost=%s\nusername=%s\npassword=%s\n\n" % (u.scheme or "https", u.netloc, username, token)
    return subprocess.run(["git", "credential", "approve"], input=payload, text=True, capture_output=True).returncode == 0


def apply(cfg, index_host=None):
    """Write server-provided values into config.json. Environment variables still take priority at use time."""
    c = paths.load("config.json", {})
    idx = cfg.get("index") or {}
    env_for = {"index_url": "AIHUB_INDEX_URL", "index_branch": "AIHUB_INDEX_BRANCH", "index_path": "AIHUB_INDEX_PATH"}
    for k_src, k_dst in (("url", "index_url"), ("branch", "index_branch"), ("path", "index_path")):
        if idx.get(k_src) and not os.environ.get(env_for[k_dst]):          # environment wins over what the hub sends
            c[k_dst] = idx[k_src]
    got = []
    lf_host = (cfg.get("langfuse") or {}).get("host")
    if lf_host and not (os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST")):
        c["langfuse_host"] = lf_host
        got.append("langfuse_host")
    cred = cfg.get("credentials")
    if cred:
        lf = cred.get("langfuse") or {}
        for src, dst in (("public_key", "langfuse_public_key"), ("secret_key", "langfuse_secret_key")):
            if lf.get(src):
                c[dst] = lf[src]
                got.append(dst)
    c["remote_config_at"] = time.time()
    c["remote_config_hours"] = cfg.get("refresh_hours", 24)
    paths.save("config.json", c)
    if "langfuse_secret_key" in got:
        try:
            os.chmod(paths.p("config.json"), 0o600)
        except OSError:
            pass
    return got


def refresh_due():
    c = paths.load("config.json", {})
    if not c.get("remote_config_at"):
        return False                      # never set up from a hub: nothing to refresh
    return time.time() - c["remote_config_at"] > float(c.get("remote_config_hours", 24)) * 3600


def auto(cmd):
    """After a command: re-pull on the admin-set interval. Silent, short timeout, keeps old values on failure."""
    if cmd in ("hook", "flush", "setup") or os.environ.get("AIHUB_NO_UPDATE") or not refresh_due():
        return
    c = paths.load("config.json", {})
    try:
        apply(fetch(timeout=3, code=c.get("enroll_code", "")))
    except Exception:
        c["remote_config_at"] = time.time() - (float(c.get("remote_config_hours", 24)) * 3600) + 3600   # retry in an hour
        paths.save("config.json", c)


def ensure_git_access(url):
    """Make sure `git ls-remote url` works; if it needs credentials we do not have, ask for them once."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", **tls.git_env())
    r = subprocess.run(["git", "ls-remote", "--exit-code", "-h", url, "HEAD"], env=env, capture_output=True, text=True)
    if r.returncode in (0, 2):
        return True
    err = (r.stderr or "").lower()
    if not any(w in err for w in ("authentication", "could not read", "denied", "403", "401", "terminal prompts disabled")):
        raise gitx.GitError(gitx.redact_url(r.stderr.strip()))
    if not ui.INTERACTIVE or not url.startswith("http"):
        raise gitx.GitError("git credentials are needed for %s; sign in with your git host (for example `git credential approve`, or run this in a terminal and enter them when asked)" % gitx.redact_url(url))
    import getpass
    ui.note("Git needs credentials for %s" % urllib.parse.urlsplit(url).netloc)
    user = input("username: ").strip()
    tok = getpass.getpass("token/password: ")
    store_git(url, user, tok)
    return subprocess.run(["git", "ls-remote", "--exit-code", "-h", url, "HEAD"], env=env, capture_output=True).returncode in (0, 2)


def cmd_setup(a):
    if a.manual:
        return _manual(a)
    hub = a.hub or paths.config()["hub"]
    code = a.code or paths.load("config.json", {}).get("enroll_code", "")
    if a.hub:
        c = paths.load("config.json", {})
        c["hub"] = a.hub.rstrip("/")
        paths.save("config.json", c)
    try:
        with ui.Spinner("Asking %s for setup values" % hub):
            cfg = fetch(hub, code)
    except (urllib.error.URLError, OSError, ValueError) as e:
        ui.bad("cannot reach the hub: %s" % e)
        ui.info("manual setup:  aihub config set langfuse_host <url>  |  set langfuse_public_key  |  set langfuse_secret_key")
        ui.info("or export LANGFUSE_BASE_URL, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, and: aihub config set index_url <git url>")
        return _manual(a)
    if a.code:
        c = paths.load("config.json", {})
        c["enroll_code"] = a.code
        paths.save("config.json", c)
    got = apply(cfg)
    ui.good("index: %s" % (cfg.get("index", {}).get("url") or "not set"))
    if got:
        ui.good("received: " + ", ".join(got))
    if any(k.endswith("_key") for k in got):
        pass
    elif cfg.get("credentials_hint"):
        ui.note("%s. ask your admin, then: aihub setup --code <code>" % cfg["credentials_hint"])
    else:
        ui.note("the hub shares only the Langfuse URL; set your Langfuse keys before using aihub (aihub setup --manual, or LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY)")
    resolve_missing(getattr(a, "yes", False))
    _verify()


SETUP_FIELDS = (("langfuse_host", "LANGFUSE_BASE_URL", "Langfuse URL", False, ("langfuse", "host")),
                ("langfuse_public_key", "LANGFUSE_PUBLIC_KEY", "Langfuse public key", False, None),
                ("langfuse_secret_key", "LANGFUSE_SECRET_KEY", "Langfuse secret key", True, None),
                ("index_url", "AIHUB_INDEX_URL", "Package index (git URL)", False, ("index", "git_url")))


def resolve_missing(yes=False):
    """Fill every setup value that is still missing. Order: environment variable > value already saved in config.json >
    defaults.json (shown and confirmed first) > typed by the user. Non-interactive runs never invent a value: a
    defaults.json value is accepted only with yes=True (or a typed 'yes'), anything else stays unset. Returns the keys that are still empty."""
    from ..core import defaults
    import getpass
    c = paths.load("config.json", {})
    d = defaults.load()
    for key, env, label, secret, dpath in SETUP_FIELDS:
        if os.environ.get(env) or c.get(key):
            continue                                       # already provided; the environment is read at use time
        dv = d.get(dpath[0], {}).get(dpath[1], "") if dpath else ""
        if dv:
            if yes or (ui.INTERACTIVE and ui.confirm("%s: use the default %s ?" % (label, dv))):
                c[key] = dv
                continue
        if ui.INTERACTIVE and not yes:
            v = (getpass.getpass if secret else input)("%s: " % label).strip()
            if v:
                c[key] = v
    paths.save("config.json", c)
    try:
        os.chmod(paths.p("config.json"), 0o600)
    except OSError:
        pass
    return [k for k, env, *_ in SETUP_FIELDS if not (os.environ.get(env) or c.get(k))]


def _manual(a):
    resolve_missing(getattr(a, "yes", False))
    c = paths.load("config.json", {})
    if ui.INTERACTIVE and (os.environ.get("AIHUB_INDEX_URL") or c.get("index_url", "")).startswith("http"):
        c = dict(c, index_url=os.environ.get("AIHUB_INDEX_URL") or c["index_url"])
        try:
            ensure_git_access(c["index_url"])
        except gitx.GitError as e:
            ui.note(str(e))
    _verify()


def _verify():
    from . import langfuse
    s = langfuse.settings()
    if not langfuse.configured(s):
        ui.note("Langfuse is not configured yet; usage reporting is paused")
        return
    try:
        langfuse._req(s, "GET", "/api/public/projects", timeout=6)
        ui.good("Langfuse reachable (%s)" % s["host"])
    except Exception as e:
        ui.note("Langfuse check failed: %s" % e)
