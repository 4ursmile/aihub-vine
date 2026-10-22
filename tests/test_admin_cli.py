import argparse
import os
import tempfile
import unittest
from unittest import mock

from aihub.cli import admin, api


def ns(**k):
    d = dict(area="users", action=None, items=[], file=None, dry_run=False, yes=False, json=False, role="user",
             out="x.csv", mode="add", description=None, perm=None, add_perm=None, remove_perm=None)
    d.update(k)
    return argparse.Namespace(**d)


def sheet(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
    f.write(text)
    f.close()
    return f.name


class SheetTest(unittest.TestCase):
    def test_header_columns_by_name_and_blank_cells_mean_unchanged(self):
        rows = admin._sheet(admin._rows(sheet("Username;Groups;Role\nann;data|ops;dev\nbob;;\n")))
        self.assertEqual(rows[0], {"username": "ann", "role": "dev", "groups": ["data", "ops"]})
        self.assertEqual(rows[1], {"username": "bob", "role": None, "groups": None})

    def test_no_header_is_username_role_groups(self):
        rows = admin._sheet(admin._rows(sheet("ann,dev,data;ops\nuser,user\n")))
        self.assertEqual(rows[0]["groups"], ["data", "ops"])
        self.assertEqual(rows[1], {"username": "user", "role": "user", "groups": None})

    def test_name_file_skips_header_and_dedups(self):
        a = ns(items=["x", "y"], file=sheet("username\ny\nz\n"))
        self.assertEqual(admin._names(a), ["x", "y", "z"])


class BatchTest(unittest.TestCase):
    def run_batch(self, text, mode="add", dry=False, groups=None, members=None):
        calls = []

        def call(method, path, body=None, **kw):
            calls.append((method, path, body))
            if path.startswith("/admin/users?"):
                return {"users": [{"username": "ann", "role": "user", "status": "active"}, {"username": "bob", "role": "dev", "status": "active"}]}
            if path == "/admin/roles":
                return {"roles": {"user": [], "dev": [], "anonymous": []}, "detail": [], "permissions": []}
            if path == "/groups":
                return {"groups": [{"name": g} for g in (groups or ["data", "ops"])]}
            if path.startswith("/groups/") and method == "GET":
                return {"members": [{"username": u} for u in (members or {}).get(path.split("/")[-1], [])]}
            return {"ok": True}
        with mock.patch.object(admin.api, "call", call), mock.patch.object(admin, "_me", lambda: "root"):
            try:
                admin.users_batch(ns(file=sheet(text), mode=mode, dry_run=dry))
                rc = 0
            except SystemExit as e:
                rc = e.code
        return rc, [c for c in calls if c[0] != "GET"]

    def test_only_real_changes_are_sent(self):
        rc, writes = self.run_batch("username,role,groups\nann,dev,data\nbob,dev,data\n", members={"data": ["bob"]})
        self.assertEqual(rc, 0)
        self.assertEqual(writes, [("POST", "/admin/users/ann/role", {"role": "dev"}),
                                  ("POST", "/groups/data/members", {"usernames": ["ann"]})])

    def test_replace_removes_other_groups_and_dry_run_writes_nothing(self):
        rc, writes = self.run_batch("ann,,data\n", mode="replace", members={"ops": ["ann"]})
        self.assertEqual(writes, [("POST", "/groups/data/members", {"usernames": ["ann"]}), ("DELETE", "/groups/ops/members/ann", None)])
        self.assertEqual(self.run_batch("ann,,data\n", mode="replace", dry=True, members={"ops": ["ann"]})[1], [])

    def test_bad_rows_are_skipped_and_exit_is_1(self):
        rc, writes = self.run_batch("ann,nope,\nghost,user,\nbob,dev,missing\n")
        self.assertEqual((rc, writes), (1, []))

    def test_own_role_is_never_changed(self):
        with mock.patch.object(admin, "_me", lambda: "ann"), mock.patch.object(admin.api, "call") as c:
            c.side_effect = lambda m, p, b=None, **k: {"users": [{"username": "ann", "role": "user", "status": "active"}]} if "/admin/users?" in p else (
                {"roles": {"dev": []}, "detail": [], "permissions": []} if p == "/admin/roles" else {"groups": []})
            with self.assertRaises(SystemExit):
                admin.users_batch(ns(file=sheet("ann,dev\n")))
            self.assertFalse([x for x in c.call_args_list if x.args[0] == "POST"])


class RolesTest(unittest.TestCase):
    def test_add_and_remove_perm_are_relative_to_current(self):
        sent = []

        def call(method, path, body=None, **kw):
            if path == "/admin/roles":
                return {"roles": {}, "detail": [{"name": "rv", "permissions": ["a", "b"], "builtin": False, "users": 0, "description": ""}], "permissions": []}
            sent.append((method, path, body))
            return {"ok": True}
        with mock.patch.object(admin.api, "call", call):
            admin.roles_set(ns(items=["rv"], add_perm="c", remove_perm="a"))
        self.assertEqual(sent, [("PUT", "/admin/roles/rv", {"permissions": ["b", "c"]})])


if __name__ == "__main__":
    unittest.main()
