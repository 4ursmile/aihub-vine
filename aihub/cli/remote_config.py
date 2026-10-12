"""Pull setup values (index location, telemetry inbox) from the hub, and keep them fresh. Git credentials stay with the user."""
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from . import api, gitx, paths, tls, ui

def fetch(hub=None, timeout=8):
    hub = (hub or paths.config()["hub"]).rstrip("/")
    tok = paths.load("credentials.json", {}).get("token")
    rq = urllib.request.Request(hub + "/api/v1/client-config", headers={"Authorization": "Bearer " + tok} if tok else {})
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
    return subprocess.run(["git", "credential", "approve"], input=payload, text=True, capture_output=True, **gitx.popen_kw()).returncode == 0


def apply(cfg, index_host=None):
    """Write server-provided values into config.json. Environment variables still take priority at use time."""
    c = paths.load("config.json", {})
    idx = cfg.get("index") or {}
    env_for = {"index_url": "AIHUB_INDEX_URL", "index_branch": "AIHUB_INDEX_BRANCH", "index_path": "AIHUB_INDEX_PATH"}
    for k_src, k_dst in (("url", "index_url"), ("branch", "index_branch"), ("path", "index_path")):
        if idx.get(k_src) and not os.environ.get(env_for[k_dst]):          # environment wins over what the hub sends
            c[k_dst] = idx[k_src]
    got = []
    tel = cfg.get("telemetry") or {}
    for k_src, k_dst, env in (("url", "telemetry_url", "AIHUB_TELEMETRY_URL"), ("branch", "telemetry_branch", "AIHUB_TELEMETRY_BRANCH"),
                                  ("inbox", "telemetry_inbox_branch", "AIHUB_TELEMETRY_INBOX_BRANCH")):
        if tel.get(k_src) and not os.environ.get(env):                    # environment wins over what the hub sends
            c[k_dst] = tel[k_src]
            got.append(k_dst)
    c["remote_config_at"] = time.time()
    c["remote_config_hours"] = cfg.get("refresh_hours", 24)
    paths.save("config.json", c)
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
        apply(fetch(timeout=3))
    except Exception:
        c["remote_config_at"] = time.time() - (float(c.get("remote_config_hours", 24)) * 3600) + 3600   # retry in an hour
        paths.save("config.json", c)


def ensure_git_access(url):
    """Make sure `git ls-remote url` works; if it needs credentials we do not have, ask for them once."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", **tls.git_env())
    r = subprocess.run(["git", "ls-remote", "--exit-code", "-h", url, "HEAD"], env=env, capture_output=True, text=True, **gitx.popen_kw())
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
    return subprocess.run(["git", "ls-remote", "--exit-code", "-h", url, "HEAD"], env=env, capture_output=True, **gitx.popen_kw()).returncode in (0, 2)


def cmd_setup(a):
    if a.manual:
        return _manual(a)
    hub = a.hub or paths.config()["hub"]
    if a.hub:
        c = paths.load("config.json", {})
        c["hub"] = a.hub.rstrip("/")
        paths.save("config.json", c)
    try:
        with ui.Spinner("Asking %s for setup values" % hub):
            cfg = fetch(hub)
    except (urllib.error.URLError, OSError, ValueError) as e:
        ui.bad("cannot reach the hub: %s" % e)
        ui.info("manual setup:  aihub config set index_url <git url>  |  aihub config set telemetry_url <git url> (defaults to the index repo)")
        return _manual(a)
    got = apply(cfg)
    ui.good("index: %s" % (cfg.get("index", {}).get("url") or "not set"))
    if got:
        ui.good("received: " + ", ".join(got))
    resolve_missing(getattr(a, "yes", False))
    ensure_access()
    _verify()


SETUP_FIELDS = (("index_url", "AIHUB_INDEX_URL", "Package index (git URL)", False, ("index", "git_url")),
                ("index_branch", "AIHUB_INDEX_BRANCH", "Package index branch", False, ("index", "branch")),
                ("index_path", "AIHUB_INDEX_PATH", "Package index folder", False, ("index", "path")))
# Telemetry url/branch/interval are NOT copied into config.json: telemetry.source() reads defaults.json live, so a new default reaches
# every install, while an explicit `aihub config set` or what the hub sends (apply) still wins.


def resolve_missing(yes=False):
    """Fill every setup value that is still missing, silently. Order: environment variable > value already saved in config.json >
    defaults.json. Nothing is typed or confirmed: locations are not secrets and the bundled defaults are the intended ones.
    The only thing a person may be asked for is git access (see ensure_access). Returns the keys that are still empty."""
    from ..core import defaults
    c = paths.load("config.json", {})
    d = defaults.load()
    for key, env, label, secret, dpath in SETUP_FIELDS:
        if os.environ.get(env) or c.get(key):
            continue                                       # already provided; the environment is read at use time
        dv = d.get(dpath[0], {}).get(dpath[1], "") if dpath else ""
        if dv:
            c[key] = dv
    paths.save("config.json", c)
    return [k for k, env, *_ in SETUP_FIELDS if not (os.environ.get(env) or c.get(k))]


def ensure_access(interactive=None):
    """Check git access to the index and the telemetry inbox. Credentials already in the user's git credential helper are enough
    and nothing is asked. Only when access fails for lack of credentials, and a person is at a terminal, ask for them once."""
    from . import registry, telemetry
    interactive = ui.INTERACTIVE if interactive is None else interactive
    urls = []
    for u in (registry.source()["url"], telemetry.target()):
        if u and u.startswith("http") and u not in urls:
            urls.append(u)
    for u in urls:
        try:
            ensure_git_access(u)
        except gitx.GitError as e:
            ui.note(str(e))


def _manual(a):
    resolve_missing(getattr(a, "yes", False))
    ensure_access()
    _verify()


def _verify():
    from . import telemetry
    url = telemetry.target()
    if not url:
        ui.note("no telemetry inbox yet; usage reporting is paused")
    elif gitx.reachable(url):
        ui.good("telemetry inbox reachable (%s)" % gitx.redact_url(url))
    else:
        ui.note("telemetry inbox not reachable or no access: %s" % gitx.redact_url(url))
