"""`aihub vault`: manage the git remotes (hosts) and logins the CLI uses for index and package repos (stdlib only).

Secrets live in the git credential helper's store (credhelper backends); `~/.aihub/vault.json` only lists non-secret facts
(host -> scheme, username) so `list` works on every backend. Interactive when URL/user/token are not given on a terminal."""
import getpass
import os
import re
import sys
import urllib.parse

from . import credhelper, paths, remote_config, ui

FILE = "vault.json"


def _load():
    d = paths.load(FILE, {})
    return d if isinstance(d, dict) else {}


def _norm(url):
    """'git.example.com' or a full URL -> (scheme, host). Raises ValueError when there is no host. The host is gitx.host_key (lowercase, no
    userinfo, IPv6 bracketed, port as written): the key git's helper protocol uses and the one credhelper and the server look up."""
    from . import gitx
    url = (url or "").strip()
    h = gitx._host_of(url if "://" in url else "https://" + url)
    if not h:
        shown = urllib.parse.urlsplit(url).hostname or ""                       # never echo a URL that may carry a token
        raise ValueError("not an http(s) git URL: %s\n%sgive the remote as https://HOST (for example: aihub vault add https://git.example.com --user NAME)"
                         % (shown, gitx.HINT))
    return h


def _secret(host):
    """The stored {username,password} for host from the first backend that has one (older keys such as user@host are found and moved), else None."""
    c, _ = credhelper._read(host, [(_load().get(host) or {}).get("username", "")])
    return c


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
    if not remote_config.store_git("%s://%s" % (scheme, host), user, token, quiet=True):
        from . import gitx
        raise ValueError("could not store the login for %s in %s, or could not read it back; nothing was saved\n%scheck that ~/.aihub is writable "
                         "and the keychain is unlocked, then retry; verify with: aihub vault test %s" % (host, credhelper.describe(), gitx.HINT, host))
    d = _load()
    d[host] = {"scheme": scheme, "username": user}
    paths.save(FILE, d, private=True)


def do_list(a):
    d = _load()
    ui.result({"backend": credhelper.describe(), "remotes": [{"host": h, "scheme": v.get("scheme", "https"), "username": v.get("username", ""),
                                                              "login": "stored" if _secret(h) else "missing"} for h, v in sorted(d.items())]})
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
    ui.result({"host": host, "scheme": scheme, "username": user, "login": "stored"})
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
    ui.result({"host": host, "username": user, "login": "stored"})
    ui.good("updated %s (%s)" % (host, user))
    return 0


def do_remove(a):
    host = _pick(a.remote)
    if not host:
        return 0
    if ui.INTERACTIVE and not a.yes and not ui.confirm("Remove %s and its stored login?" % host, False):
        return 0
    d = _load()
    d.pop(host, None)
    credhelper.forget(host)                   # git's own reject never erases a vault host; only this does
    paths.save(FILE, d, private=True)
    ui.result({"host": host, "removed": True})
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


def do_test(a):
    """Does the stored login for a remote reach it? `vault test URL` (any git URL) or `vault test HOST`; no argument tests every remote in the vault.
    Uses the same lookup the hub server does (`credhelper.lookup`), so a pass here means the server account can fetch too."""
    from . import gitx
    targets = []
    if a.remote:
        scheme, host = _norm(a.remote)
        shown = a.remote if "://" in a.remote else "%s://%s" % (scheme, host)
        targets.append((re.sub(r"//[^/@]+@", "//", shown), host))        # the login in a typed URL is not echoed back
    else:
        targets = [("%s://%s" % (v.get("scheme", "https"), h), h) for h, v in sorted(_load().items())]
    if not targets:
        raise ValueError("the vault is empty (add one: aihub vault add)")
    bad, res = 0, []
    ui.result(res)
    for url, host in targets:
        got = credhelper.lookup(host)
        res.append({"host": host, "url": url, "username": got[0] if got else None, "login": "stored" if got else "missing", "reachable": False})
        if not got:
            ui.bad("%-28s no stored login (aihub vault add %s --user NAME)" % (host, url))
            if host in _load():
                ui.info("  listed in vault.json but %s cannot read it in this process (user %s, AIHUB_HOME %s): run this as the account that "
                        "saved it, or save it again as the account that runs the hub" % (credhelper.describe(), os.environ.get("USER") or os.environ.get("USERNAME") or "?", paths.HOME()))
            bad += 1
            continue
        saved = gitx.LOGIN
        gitx.LOGIN = lambda h: credhelper.lookup(h)             # the same hook the server installs
        try:
            ok = gitx.reachable(url)
        finally:
            gitx.LOGIN = saved
        res[-1]["reachable"] = bool(ok)
        if ok:
            ui.good("%-28s %s: reachable" % (host, got[0]))
        else:
            ui.bad("%-28s %s: stored, but git could not use it (wrong token, no access to %s, or host unreachable)" % (host, got[0], url))
            ui.info("  " + (gitx.hint("auth", url) or "").replace(gitx.HINT, "hint: "))
            bad += 1
    return 1 if bad else 0


ACTIONS = {"list": do_list, "add": do_add, "edit": do_edit, "remove": do_remove, "test": do_test}


def run(a):
    act = a.action or ("list" if not ui.INTERACTIVE else "menu")
    if act == "menu":
        return do_menu(a)
    return ACTIONS[act](a)
