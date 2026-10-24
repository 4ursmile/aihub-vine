"""How the server's own git calls get a login and permission to open its folders (one place, used by sync, backup, transfer and recovery).

The server runs git without a person to answer a prompt (`gitx.BATCH`), so a missing login used to end as "terminal prompts disabled" and a
data folder owned by another account as "detected dubious ownership". `install()` fixes both for every later git call:
  - the data folder is trusted (`gitx.trust`);
  - a login is looked up per host (`gitx.LOGIN`): AIHUB_GIT_TOKEN, else the aihub vault of the account that runs the server.
Nothing here writes a credential to disk, to git config or to a command line."""
import logging
import os

from ..cli import gitx
from .config import parse_env_file

log = logging.getLogger("aihub")


def _token(settings):
    env = settings.env_get
    token = env("AIHUB_GIT_TOKEN")
    if not token and env("AIHUB_GIT_TOKEN_FILE"):
        try:
            with open(env("AIHUB_GIT_TOKEN_FILE"), encoding="utf-8") as f:
                token = f.read().strip()
        except OSError:
            token = ""
    return token


def _norm(host):
    return (host or "").strip().lower()


_hinted = set()           # hosts already told about a missing login (one hint per host per process)


def _vault(host):
    try:
        from ..cli import credhelper
        return credhelper.lookup(host)
    except Exception:
        return None


def login_for(settings, host, urls=()):
    """(user, secret) git should use for `host` (exact 'host' or 'host:port', as git connects), or None (then git's own helpers decide).
    AIHUB_GIT_TOKEN (+ AIHUB_GIT_USER; or AIHUB_GIT_TOKEN_FILE) applies to EXACTLY the hosts of `urls` and of AIHUB_GIT_HOSTS
    (a host without a port there means the default port): a token meant for git.corp.example is never offered to git.corp.example:9999 or to
    another server. Without AIHUB_GIT_USER the user is the one stored in the vault for that host (hosts with 2FA such as Bitbucket or Gitea
    want the real account name), else oauth2. Then the vault (`aihub vault add`), by the same exact host. The environment token wins over a
    vault entry for the same host."""
    host = _norm(host)
    token = _token(settings)
    if token:
        known = set()
        for u in urls:
            h = gitx._host_of(u)
            if h:
                known.add(h[1])
        known |= {_norm(h) for h in settings.env_get("AIHUB_GIT_HOSTS").split(",") if h.strip()}
        if host in known:
            user = settings.env_get("AIHUB_GIT_USER")
            if not user:
                v = _vault(host)
                user = (v[0] if v else "") or "oauth2"
            return user, token
    found = _vault(host)
    if not found and host and host not in _hinted:
        _hinted.add(host)
        log.warning("the hub has no working git login for %s: set AIHUB_GIT_TOKEN (+AIHUB_GIT_USER, AIHUB_GIT_HOSTS=%s) in the server env, "
                    "or run `aihub vault add https://%s` as the account that runs the server", host, host, host)
    return found


# Switches that tls.py / gitx.py read from os.environ. They are not secrets, so a `.env` value is exported for them (the real environment wins).
# The token itself is never exported: every child process would inherit it.
ENV_FILE_KEYS = ("AIHUB_GIT_ALLOW_HTTP", "AIHUB_GIT_PROXY", "AIHUB_CA_BUNDLE", "AIHUB_INSECURE")


def _from_env_file(settings):
    for k in ENV_FILE_KEYS:
        v = settings.env_get(k)
        if v and not os.environ.get(k):
            os.environ[k] = v
    f = getattr(settings, "env_file", "")
    if f and os.name == "posix" and parse_env_file(f).get("AIHUB_GIT_TOKEN"):
        try:
            if os.stat(f).st_mode & 0o077:
                log.warning("%s holds AIHUB_GIT_TOKEN but other users can read it: run `chmod 600 %s`", f, f)
        except OSError:
            pass


def install(settings, urls=lambda: ()):
    """Make git calls of this process trust the data folder and find their login. `urls` is a callable giving the configured git URLs
    (they can change at runtime in the admin settings). Every AIHUB_GIT_* value may come from the real environment or from the `.env` file."""
    _from_env_file(settings)
    gitx.BATCH = True
    gitx.trust(settings.data_dir)
    gitx.LOGIN = lambda host: login_for(settings, host, [u for u in urls() if u])
