"""`aihub vault`: manage the git remotes (hosts) and logins the CLI uses for index and package repos (stdlib only).

Secrets live in the git credential helper's store (credhelper backends); `~/.aihub/vault.json` only lists non-secret facts
(host -> scheme, username) so `list` works on every backend. Interactive when URL/user/token are not given on a terminal."""
import getpass
import json
import sys
import urllib.parse

from . import credhelper, paths, remote_config, ui

FILE = "vault.json"


def _load():
    d = paths.load(FILE, {})
    return d if isinstance(d, dict) else {}


def _norm(url):
    """'git.example.com' or a full URL -> (scheme, host). Raises ValueError when there is no host."""
    u = urllib.parse.urlsplit(url if "://" in url else "https://" + url)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ValueError("not an http(s) git URL: %s" % url)
    return u.scheme, u.netloc


def _secret(host):
    """The stored {username,password} for host from the first backend that has one, else None."""
    for b in credhelper.backends():
        try:
            raw = b.get(host)
            c = json.loads(raw) if raw else None
        except Exception:
            continue
        if c and c.get("password"):
            return c
    return None


def _pick(host):
    """Resolve a host argument, or ask on a terminal. Returns the vault key or None."""
    d = _load()
    if host:
        key = _norm(host)[1]
        if key not in d:
            raise ValueError("no remote %s in the vault (see: aihub vault list)" % key)
        return key
    if not d:
        raise ValueError("the vault is empty (add one: aihub vault add)")
    if not ui.INTERACTIVE:
        raise ValueError("name a remote: aihub vault <edit|remove> HOST")
    return ui.select("Which remote?", [(h, "%s  %s" % (h, ui.dim(v.get("username", "")))) for h, v in sorted(d.items())])


def _token(a, prompt, required=True):
    if getattr(a, "token_stdin", False):
        return sys.stdin.readline().rstrip("\r\n")
    if not ui.INTERACTIVE:
        if required:
            raise ValueError("no token: use --token-stdin (or run on a terminal)")
        return ""
    return getpass.getpass(prompt)


def _save(scheme, host, user, token):
    if not remote_config.store_git("%s://%s" % (scheme, host), user, token):
        raise ValueError("the credential helper refused to store the login for %s" % host)
    d = _load()
    d[host] = {"scheme": scheme, "username": user}
    paths.save(FILE, d, private=True)


def do_list(a):
    d = _load()
    if not d:
        ui.info("no remotes yet. Add one: aihub vault add")
        return 0
    ui.table(["remote", "user", "token"], [[h, v.get("username", ""), "stored" if _secret(h) else ui.warn("missing")]
                                           for h, v in sorted(d.items())])
    ui.info("secrets are kept in: " + credhelper.describe())
    return 0


def do_add(a):
    url = a.remote
    if not url:
        if not ui.INTERACTIVE:
            raise ValueError("usage: aihub vault add URL --user NAME --token-stdin")
        url = input("  git remote URL (e.g. https://git.example.com): ").strip()
    scheme, host = _norm(url)
    user = a.user or (input("  username: ").strip() if ui.INTERACTIVE else "")
    if not user:
        raise ValueError("a username is required (--user)")
    if host in _load() and ui.INTERACTIVE and not ui.confirm("%s is already in the vault. Replace its login?" % host, False):
        return 0
    _save(scheme, host, user, _token(a, "  token/password: "))
    ui.good("saved %s for %s" % (user, host))
    return 0


def do_edit(a):
    host = _pick(a.remote)
    if not host:
        return 0
    cur = _load().get(host, {})
    old = _secret(host) or {}
    user = a.user
    if not user and ui.INTERACTIVE:
        user = input("  username [%s]: " % cur.get("username", "")).strip()
    user = user or cur.get("username") or old.get("username") or ""
    tok = _token(a, "  new token/password (empty keeps the current one): ", required=False)
    tok = tok or old.get("password", "")
    if not (user and tok):
        raise ValueError("nothing stored to keep: give a username and a token")
    _save(cur.get("scheme", "https"), host, user, tok)
    ui.good("updated %s (%s)" % (host, user))
    return 0


def do_remove(a):
    host = _pick(a.remote)
    if not host:
        return 0
    if ui.INTERACTIVE and not a.yes and not ui.confirm("Remove %s and its stored login?" % host, False):
        return 0
    d = _load()
    scheme = d.pop(host, {}).get("scheme", "https")
    credhelper.run("erase", stdin="protocol=%s\nhost=%s\n\n" % (scheme, host))
    paths.save(FILE, d, private=True)
    ui.good("removed %s" % host)
    return 0


def do_menu(a):
    if not ui.INTERACTIVE:
        return do_list(a)
    while True:
        ui.heading("Vault")
        do_list(a)
        act = ui.select("What next?", [("add", "Add a remote"), ("edit", "Edit credentials"), ("remove", "Remove a remote"), ("exit", "Done")])
        if act in (None, "exit"):
            return 0
        a.remote = a.user = None
        a.yes = False
        try:
            {"add": do_add, "edit": do_edit, "remove": do_remove}[act](a)
        except ValueError as e:
            ui.bad(str(e))


ACTIONS = {"list": do_list, "add": do_add, "edit": do_edit, "remove": do_remove}


def run(a):
    act = a.action or ("list" if not ui.INTERACTIVE else "menu")
    if act == "menu":
        return do_menu(a)
    return ACTIONS[act](a)
