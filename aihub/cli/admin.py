"""`aihub admin`: manage accounts, groups and roles on the hub from the terminal (needs the hub `admin` permission).
Every action is one call (or a loop of calls) to an existing /api/v1 route; the hub does the permission checks. Stdlib only."""
import csv
import io
import json
import os
import re
import sys
import urllib.parse

from . import api, paths, ui

Q = lambda s: urllib.parse.quote(str(s), safe="")
STATES = ("active", "pending", "disabled")
CHUNK = 200                                    # the hub adds at most 200 members per request
USER_H = {"username", "account", "login", "user name", "user_name"}
ROLE_H = {"role"}
GROUP_H = {"group", "groups", "group name", "group_name", "team", "teams"}
OTHER_H = {"display_name", "title"}


class Failures(list):
    """Per-item errors: every item is tried, the command exits 1 at the end if any failed."""
    def add(self, what, err):
        self.append("%s: %s" % (what, err))

    def finish(self):
        for f in self:
            ui.bad(f)
        if self:
            raise SystemExit(1)


def _me():
    return paths.load("credentials.json", {}).get("username") or ""


def _words(v):
    return [x.strip() for x in re.split(r"[,\s]+", v or "") if x.strip()]


# ---------------- input files
def _read(path):
    if path == "-":
        return sys.stdin.read()
    with open(os.path.expanduser(path), encoding="utf-8-sig", newline="") as f:
        return f.read()


def _rows(path):
    """CSV / TSV / `;` separated text -> rows with at least one non-empty cell."""
    text = _read(path)
    first = text.splitlines()[0] if text.strip() else ""
    delim = max(",;\t", key=first.count) if any(d in first for d in ",;\t") else ","
    return [r for r in csv.reader(io.StringIO(text), delimiter=delim) if any(c.strip() for c in r)]


def _file_names(path):
    """One username per line (or the first column of a sheet; a `username` header row is skipped)."""
    return [r[0].strip() for r in _rows(path) if r[0].strip().lower() not in USER_H]


def _names(a, items=None):
    names = list(a.items if items is None else items)
    if a.file:
        names += _file_names(a.file)
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _idx(header, names):
    return next((i for i, c in enumerate(header) if c in names), -1)


def _sheet(rows):
    """Rows -> [{username, role|None, groups|None}]. With a header the columns are found by name; without one the layout is
    `username, role, groups`. A blank role or groups cell means "leave as is"."""
    cells = [c.strip().lower() for c in rows[0]] if rows else []
    known = USER_H | ROLE_H | GROUP_H | OTHER_H
    if cells and (cells[0] in USER_H or sum(c in known for c in cells) >= 2):
        ui_, ri, gi = max(_idx(cells, USER_H), 0), _idx(cells, ROLE_H), _idx(cells, GROUP_H)
        rows = rows[1:]
    else:
        ui_, ri, gi = 0, 1, 2
    cell = lambda r, i: r[i].strip() if 0 <= i < len(r) else ""
    out = []
    for r in rows:
        groups = []
        for g in re.split(r"[;,|\n]+", cell(r, gi)):
            g = g.strip().lower()
            if g and g not in groups:
                groups.append(g)
        out.append({"username": cell(r, ui_), "role": cell(r, ri).lower() or None, "groups": groups or None})
    return out


# ---------------- hub reads
def _users(q=""):
    return api.call("GET", "/admin/users?q=" + Q(q))["users"]


def _roles():
    return api.call("GET", "/admin/roles")


def _role_names(roles=None):
    return sorted(set((roles or _roles())["roles"]) - {"anonymous"})


def _groups():
    return api.call("GET", "/groups")["groups"]


def _need(args, n, usage):
    if len(args) < n:
        raise ValueError("usage: aihub admin " + usage)


def _split_users(a, names):
    """-> ({lowercase name: account}, [names without an account]). The hub ignores a role/status change for an unknown name."""
    have = {u["username"].lower(): u for u in _users()}
    found, missing = {}, []
    for n in names:
        (found.__setitem__(n.lower(), have[n.lower()]) if n.lower() in have else missing.append(n))
    return found, missing


def _json(a, data):
    if a.json:
        print(json.dumps(data, indent=2))
        return True


# ---------------- users
def users_list(a):
    users = _users(a.items[0] if a.items else "")
    if _json(a, users):
        return
    if not users:
        return ui.note("no accounts match")
    ui.table(["username", "name", "role", "status"],
             [[u["username"], u.get("display_name") or "", u["role"], u["status"]] for u in users])
    ui.info("%d account%s" % (len(users), "" if len(users) == 1 else "s"))


def users_role(a):
    _need(a.items, 2, "users role ROLE USER... [-f FILE]")
    role, names = a.items[0].lower(), _names(a, a.items[1:])
    if role == "anonymous" or role not in _role_names():
        raise ValueError("unknown role '%s' (aihub admin roles list)" % role)
    _each_user(a, names, "role", lambda u: api.call("POST", "/admin/users/%s/role" % Q(u), {"role": role}), role)


def users_status(a):
    _need(a.items, 2, "users status active|pending|disabled USER... [-f FILE]")
    state, names = a.items[0].lower(), _names(a, a.items[1:])
    if state not in STATES:
        raise ValueError("status must be one of: " + ", ".join(STATES))
    _each_user(a, names, "status", lambda u: api.call("POST", "/admin/users/%s/status" % Q(u), {"status": state}), state)


def _each_user(a, names, what, fn, value):
    if not names:
        raise ValueError("no usernames given")
    found, missing = _split_users(a, names)
    bad = Failures("%s: no such account" % n for n in missing)
    for key, u in found.items():
        name = u["username"]
        if name == _me():
            bad.add(name, "you cannot change your own %s" % what)
        elif a.dry_run:
            ui.info("would set %s of %s to %s" % (what, name, value))
        else:
            try:
                fn(name)
                ui.good("%s: %s -> %s" % (name, what, value))
            except api.ApiError as e:
                bad.add(name, e)
    bad.finish()


def users_reset(a):
    _need(a.items, 1, "users reset-password USER...")
    bad = Failures()
    for n in a.items:
        try:
            r = api.call("POST", "/admin/users/%s/reset-password" % Q(n))
            print("%s\t%s" % (r["username"], r["password"]))        # shown once; everything else goes to stderr-style notes
        except api.ApiError as e:
            bad.add(n, e)
    if not bad:
        ui.info("passwords are shown once; those people are signed out everywhere")
    bad.finish()


def users_delete(a):
    names = _names(a)
    if not names:
        raise ValueError("usage: aihub admin users delete USER... [-f FILE] [--yes]")
    found, missing = _split_users(a, names)
    bad = Failures("%s: no such account" % n for n in missing)
    if found and not a.yes and not a.dry_run and not ui.confirm("Delete %d account(s): %s ?" % (len(found), ", ".join(u["username"] for u in found.values())), default=False):
        raise ValueError("cancelled (use --yes to skip the question)")
    for u in found.values():
        name = u["username"]
        if a.dry_run:
            ui.info("would delete " + name)
            continue
        try:
            api.call("DELETE", "/admin/users/" + Q(name))
            ui.good("deleted " + name)
        except api.ApiError as e:
            bad.add(name, e)
    bad.finish()


def users_create(a):
    """Create accounts from a sheet (the hub's bulk import). The passwords come back once, in a file only you can read."""
    path = a.file or (a.items[0] if a.items else None)
    if not path:
        raise ValueError("usage: aihub admin users create FILE [--role ROLE] [--out new-accounts.csv]")
    out = os.path.expanduser(a.out)
    if os.path.exists(out):
        raise ValueError("%s already exists; choose another --out (it would overwrite passwords)" % out)
    with open(os.path.expanduser(path), "rb") as f:
        data = f.read()
    with api._req("POST", "/admin/users/batch?role=" + Q(a.role), raw=data, timeout=300,
                  headers={"X-Aihub-Filename": os.path.basename(path), "Content-Type": "application/octet-stream"}) as r:
        body, h = r.read(), r.headers
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(body)
    ui.good("%s account(s) created, %s skipped, %s group(s) created" % (h.get("X-Created", "?"), h.get("X-Skipped", "?"), h.get("X-Groups-Created", "0")))
    ui.info("usernames, groups and first passwords are in %s (mode 0600): hand them out, then delete the file" % out)


def users_batch(a):
    """Change roles and group membership of existing accounts from a sheet: `username, role, groups` (header optional)."""
    path = a.file or (a.items[0] if a.items else None)
    if not path:
        raise ValueError("usage: aihub admin users batch FILE [--mode add|replace] [--dry-run]\n  columns: username, role, groups (header row optional; several groups separated by ; or |; blank cell = leave as is)")
    rows = _sheet(_rows(path))
    if not rows:
        raise ValueError("no rows found in " + path)
    users = {u["username"].lower(): u for u in _users()}
    roles = _role_names()
    gnames = {g["name"] for g in _groups()}
    mentioned = {g for r in rows for g in (r["groups"] or []) if g in gnames}
    current = _memberships(gnames if a.mode == "replace" else mentioned)      # so the plan counts real changes only

    bad, role_to, add, drop, seen = Failures(), [], {}, {}, set()
    for r in rows:
        n = r["username"]
        u = users.get(n.lower())
        if not u:
            bad.add(n or "(empty name)", "no such account")
            continue
        if u["username"].lower() in seen:
            bad.add(n, "listed twice")
            continue
        seen.add(u["username"].lower())
        name = u["username"]
        unknown = [g for g in (r["groups"] or []) if g not in gnames]
        if r["role"] and r["role"] not in roles:
            bad.add(name, "unknown role '%s'" % r["role"])
        elif unknown:
            bad.add(name, "unknown group %s (aihub admin groups create)" % ", ".join(unknown))
        elif name == _me() and r["role"] and r["role"] != u["role"]:
            bad.add(name, "you cannot change your own role")
        else:
            if r["role"] and r["role"] != u["role"]:
                role_to.append((name, r["role"]))
            if r["groups"] is not None:
                want, have = set(r["groups"]), current.get(name, set())
                for g in sorted(want - have):
                    add.setdefault(g, []).append(name)
                for g in sorted(have - want) if a.mode == "replace" else []:
                    drop.setdefault(g, []).append(name)

    ui.info("%d row(s): %d role change(s), %d group add(s), %d group removal(s), %d skipped" % (
        len(rows), len(role_to), sum(map(len, add.values())), sum(map(len, drop.values())), len(bad)))
    if a.dry_run:
        for n, role in role_to:
            ui.info("role   %s -> %s" % (n, role))
        for g, ns in sorted(add.items()):
            ui.info("add    %s += %s" % (g, ", ".join(ns)))
        for g, ns in sorted(drop.items()):
            ui.info("remove %s -= %s" % (g, ", ".join(ns)))
        return bad.finish()
    done = 0
    for n, role in role_to:
        try:
            api.call("POST", "/admin/users/%s/role" % Q(n), {"role": role})
            done += 1
        except api.ApiError as e:
            bad.add(n + " role", e)
    for g, ns in sorted(add.items()):
        for i in range(0, len(ns), CHUNK):
            try:
                r = api.call("POST", "/groups/%s/members" % Q(g), {"usernames": ns[i:i + CHUNK]})
                done += len(ns[i:i + CHUNK]) - len(r.get("missing") or [])
            except api.ApiError as e:
                bad.add("group " + g, e)
    for g, ns in sorted(drop.items()):
        for n in ns:
            try:
                api.call("DELETE", "/groups/%s/members/%s" % (Q(g), Q(n)))
                done += 1
            except api.ApiError as e:
                bad.add("group %s -%s" % (g, n), e)
    ui.good("%d change(s) applied" % done)
    bad.finish()


def _memberships(gnames):
    """{username: {group,...}} read from every group (needed to know what `--mode replace` has to remove)."""
    out = {}
    for g in gnames:
        for m in api.call("GET", "/groups/" + Q(g))["members"]:
            out.setdefault(m["username"], set()).add(g)
    return out


# ---------------- groups
def groups_list(a):
    gs = _groups()
    if _json(a, gs):
        return
    if not gs:
        return ui.note("no groups yet: aihub admin groups create NAME")
    ui.table(["group", "members", "packages", "description"],
             [[g["name"], str(g["members"]), str(g["packages"]), g.get("description") or ""] for g in gs])


def groups_show(a):
    _need(a.items, 1, "groups show GROUP")
    g = api.call("GET", "/groups/" + Q(a.items[0]))
    if _json(a, g):
        return
    ui.out("  %s  %s" % (g["name"], ui.dim(g.get("description") or "")))
    ui.table(["username", "name"], [[m["username"], m.get("display_name") or ""] for m in g["members"]]) if g["members"] else ui.info("no members")


def groups_create(a):
    _need(a.items, 1, "groups create GROUP... [--description TEXT]")
    bad = Failures()
    for n in a.items:
        try:
            api.call("POST", "/groups", {"name": n, "description": a.description or ""})
            ui.good("created " + n.lower())
        except api.ApiError as e:
            bad.add(n, e)
    ui.info("note: the hub makes the creating account a member; remove it with 'aihub admin groups remove GROUP %s' if unwanted" % (_me() or "YOU"))
    bad.finish()


def groups_describe(a):
    _need(a.items, 1, "groups describe GROUP --description TEXT")
    if a.description is None:
        raise ValueError("give the new text with --description")
    api.call("PUT", "/groups/" + Q(a.items[0]), {"description": a.description})
    ui.good("updated " + a.items[0])


def groups_delete(a):
    _need(a.items, 1, "groups delete GROUP... [--yes]")
    if not a.yes and not a.dry_run and not ui.confirm("Delete group(s) %s (members stay, access through the group ends) ?" % ", ".join(a.items), default=False):
        raise ValueError("cancelled (use --yes to skip the question)")
    bad = Failures()
    for n in a.items:
        if a.dry_run:
            ui.info("would delete " + n)
            continue
        try:
            api.call("DELETE", "/groups/" + Q(n))
            ui.good("deleted " + n)
        except api.ApiError as e:
            bad.add(n, e)
    bad.finish()


def groups_add(a):
    _need(a.items, 1, "groups add GROUP USER... [-f FILE]")
    group, names = a.items[0], _names(a, a.items[1:])
    if not names:
        raise ValueError("no usernames given")
    if a.dry_run:
        return ui.info("would add %d account(s) to %s: %s" % (len(names), group, ", ".join(names)))
    missing, bad = [], Failures()
    for i in range(0, len(names), CHUNK):
        try:
            missing += api.call("POST", "/groups/%s/members" % Q(group), {"usernames": names[i:i + CHUNK]}).get("missing") or []
        except api.ApiError as e:
            bad.add(group, e)
            break
    ui.good("%d added to %s" % (len(names) - len(missing), group)) if not bad else None
    for m in missing:
        bad.add(m, "no such account")
    bad.finish()


def groups_remove(a):
    _need(a.items, 1, "groups remove GROUP USER... [-f FILE]")
    group, names = a.items[0], _names(a, a.items[1:])
    if not names:
        raise ValueError("no usernames given")
    bad = Failures()
    for n in names:
        if a.dry_run:
            ui.info("would remove %s from %s" % (n, group))
            continue
        try:
            api.call("DELETE", "/groups/%s/members/%s" % (Q(group), Q(n)))
            ui.good("%s removed from %s" % (n, group))
        except api.ApiError as e:
            bad.add(n, e)
    bad.finish()


# ---------------- roles
def roles_list(a):
    d = _roles()
    if _json(a, d["detail"]):
        return
    ui.table(["role", "accounts", "permissions"],
             [[r["name"], str(r["users"]), ", ".join(r["permissions"]) or "-"] for r in d["detail"]])


def roles_perms(a):
    d = _roles()
    if _json(a, d["permissions"]):
        return
    ui.table(["permission", "meaning"], [[p["key"], p["label"]] for p in d["permissions"]])


def _role(name):
    d = next((r for r in _roles()["detail"] if r["name"] == name), None)
    if not d:
        raise ValueError("no such role '%s' (aihub admin roles list)" % name)
    return d


def roles_show(a):
    _need(a.items, 1, "roles show ROLE")
    d = _role(a.items[0])
    if _json(a, d):
        return
    ui.out("  %s  %s" % (d["name"], ui.dim("%s, %d account(s)" % ("built-in" if d["builtin"] else "custom", d["users"]))))
    ui.out("  " + (d["description"] or ""))
    for p in d["permissions"]:
        ui.out("    " + p)


def roles_create(a):
    _need(a.items, 1, "roles create ROLE [--perm a,b] [--description TEXT]")
    api.call("POST", "/admin/roles", {"name": a.items[0], "description": a.description or "", "permissions": _words(a.perm)})
    ui.good("created role " + a.items[0].lower())


def roles_set(a):
    """--perm replaces the permission list; --add-perm / --remove-perm change it relative to what the role has now."""
    _need(a.items, 1, "roles set ROLE [--perm a,b | --add-perm a --remove-perm b] [--description TEXT]")
    name = a.items[0]
    body = {}
    if a.perm is not None or a.add_perm or a.remove_perm:
        have = _role(name)["permissions"]
        perms = set(_words(a.perm)) if a.perm is not None else set(have)
        perms |= set(_words(a.add_perm))
        perms -= set(_words(a.remove_perm))
        body["permissions"] = sorted(perms)
    if a.description is not None:
        body["description"] = a.description
    if not body:
        raise ValueError("nothing to change: give --perm, --add-perm, --remove-perm or --description")
    if a.dry_run:
        return ui.info("would update %s: %s" % (name, json.dumps(body)))
    api.call("PUT", "/admin/roles/" + Q(name), body)
    ui.good("updated role " + name)


def roles_delete(a):
    _need(a.items, 1, "roles delete ROLE [--yes]")
    if not a.yes and not a.dry_run and not ui.confirm("Delete role %s ?" % a.items[0], default=False):
        raise ValueError("cancelled (use --yes to skip the question)")
    if a.dry_run:
        return ui.info("would delete role " + a.items[0])
    api.call("DELETE", "/admin/roles/" + Q(a.items[0]))
    ui.good("deleted role " + a.items[0])


ACTIONS = {
    "users": {"list": users_list, "role": users_role, "status": users_status, "reset-password": users_reset,
              "delete": users_delete, "create": users_create, "batch": users_batch},
    "groups": {"list": groups_list, "show": groups_show, "create": groups_create, "describe": groups_describe,
               "delete": groups_delete, "add": groups_add, "remove": groups_remove},
    "roles": {"list": roles_list, "perms": roles_perms, "show": roles_show, "create": roles_create,
              "set": roles_set, "delete": roles_delete},
}


def cli(a):
    fn = ACTIONS[a.area].get(a.action or "list")
    if not fn:
        raise ValueError("unknown action '%s' for %s; one of: %s" % (a.action, a.area, ", ".join(ACTIONS[a.area])))
    return fn(a)
