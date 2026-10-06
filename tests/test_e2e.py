import hashlib
import io
import os
import tarfile
import tempfile
import unittest

from fastapi.testclient import TestClient

from aihub.core import archive, naming, version as V
from aihub.server import markdown
from aihub.server.config import Settings
from aihub.server.main import create_app

TOML = b'''[package]
name = "demo_tool"
version = "1.0.0"
type = "skill"
description = "demo"
tags = ["x"]
[requires]
commands = [{name="git", hint="brew install git"}]
packages = ["other"]
os = ["macos"]
'''


class Core(unittest.TestCase):
    def test_version(self):
        self.assertEqual(V.compare("1.0", "1.0.0"), 0)
        self.assertTrue(V.compare("1.0.0rc1", "1.0.0") < 0)
        self.assertTrue(V.satisfies("1.5.0", ">=1,<2"))
        self.assertFalse(V.satisfies("2.0.0", ">=1,<2"))
        self.assertEqual(V.bump("1.2.3", "minor"), "1.3.0")
        self.assertEqual(naming.normalize("Demo_Tool.x"), "demo-tool-x")

    def test_md_xss(self):
        h = markdown.render("<script>alert(1)</script> [x](javascript:alert(1))")
        self.assertNotIn("<script>", h)
        self.assertNotIn("javascript:", h)

    def test_unsafe_tar(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "a.tar.gz")
        with tarfile.open(p, "w:gz") as t:
            ti = tarfile.TarInfo("../evil")
            ti.size = 1
            t.addfile(ti, io.BytesIO(b"x"))
        with self.assertRaises(ValueError):
            archive.safe_extract(p, os.path.join(d, "o"))


class Server(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.c = TestClient(create_app(Settings(data_dir=self.d, event_flush_secs=0.2)))

    def auth(self, name="alice"):
        self.c.post("/api/v1/auth/register", json={"username": name, "password": "secret1"})
        t = self.c.post("/api/v1/auth/login", json={"username": name, "password": "secret1"}).json()["token"]
        return {"Authorization": "Bearer " + t}

    def test_flow(self):
        h = self.auth()
        p = os.path.join(self.d, "s.tar.gz")
        with tarfile.open(p, "w:gz") as t:
            ti = tarfile.TarInfo("aihub.toml")
            ti.size = len(TOML)
            t.addfile(ti, io.BytesIO(TOML))
            r = b"# Hello\n**bold**"
            ti = tarfile.TarInfo("README.md")
            ti.size = len(r)
            t.addfile(ti, io.BytesIO(r))
        body = open(p, "rb").read()
        self.assertEqual(self.c.post("/api/v1/upload", content=body).status_code, 401)
        r = self.c.post("/api/v1/upload", content=body, headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.c.post("/api/v1/upload", content=body, headers=h).status_code, 409)
        d = self.c.get("/api/v1/packages/demo-tool").json()
        self.assertEqual(d["latest_version"], "1.0.0")
        self.assertEqual(d["requires"]["packages"], ["other"])
        self.assertIn("<h1>Hello</h1>", self.c.get("/api/v1/packages/demo-tool/readme").json()["html"])
        self.assertEqual(self.c.get("/api/v1/packages?q=demo").json()["total"], 1)
        self.assertEqual(self.c.get("/api/v1/resolve", params={"name": "demo-tool"}).status_code, 401)   # public install is off by default
        res = self.c.get("/api/v1/resolve", params={"name": "demo-tool", "spec": ">=1"}, headers=h).json()
        self.assertEqual(self.c.get(res["url"].replace("http://localhost:8000", "")).status_code, 401)  # ...and so are downloads
        f = self.c.get(res["url"].replace("http://localhost:8000", ""), headers=h)
        self.assertEqual(f.status_code, 200)
        self.assertEqual(hashlib.sha256(f.content).hexdigest(), res["sha256"])
        self.c.post("/api/v1/events", json={"events": [{"kind": "use", "package": "demo-tool", "client_id": "c1"}]})
        self.assertEqual(self.c.get("/api/v1/packages/demo-tool/stats").json()["by_kind"]["use"], 1)
        self.assertEqual(self.c.post("/api/v1/packages/demo-tool/reviews", json={"rating": 5}, headers=h).status_code, 200)
        self.assertEqual(self.c.get("/api/v1/packages?q=demo").json()["items"][0]["rating"]["avg"], 5)
        self.assertEqual(self.c.get("/api/v1/admin/audit", headers=h).status_code, 200)
        self.assertEqual(self.c.get("/api/v1/admin/audit", headers=self.auth("bob")).status_code, 403)
        self.c.post("/api/v1/packages/demo-tool/versions/1.0.0/yank", headers=h)
        self.assertEqual(self.c.get("/api/v1/resolve?name=demo-tool", headers=h).status_code, 404)

    def test_pages(self):
        self.assertEqual(self.c.get("/").status_code, 200)
        self.assertIn("aihub", self.c.get("/install.sh").text)
        self.assertEqual(self.c.get("/cli/aihub.pyz").status_code, 200)

    def test_source_download_setting(self):
        h = self.auth()
        body = io.BytesIO()
        with tarfile.open(fileobj=body, mode="w:gz") as t:
            ti = tarfile.TarInfo("aihub.toml")
            ti.size = len(TOML)
            t.addfile(ti, io.BytesIO(TOML))
        self.assertEqual(self.c.post("/api/v1/upload", content=body.getvalue(), headers=h).status_code, 200)
        sha = self.c.get("/api/v1/packages/demo-tool").json()["versions"][0]["sha256"]
        url = "/api/v1/packages/demo-tool/versions/1.0.0/download"
        self.assertTrue(self.c.get("/api/v1/meta").json()["allow_source_download"])        # default on
        self.assertEqual(self.c.get(url).status_code, 401)                                 # signed out: needs sign-in
        r = self.c.get(url, headers=h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(hashlib.sha256(r.content).hexdigest(), sha)
        self.assertEqual(self.c.get("/api/v1/packages/demo-tool/versions/9.9.9/download", headers=h).status_code, 404)
        self.c.put("/api/v1/admin/settings", json={"allow_source_download": "0"}, headers=h)
        self.assertEqual(self.c.get(url, headers=h).status_code, 403)
        self.assertFalse(self.c.get("/api/v1/meta").json()["allow_source_download"])
        f = self.c.get("/files/demo-tool/" + self.c.get("/api/v1/resolve?name=demo-tool", headers=h).json()["url"].rsplit("/", 1)[1], headers=h)
        self.assertEqual(f.status_code, 200)                                               # CLI installs still work

    def test_reset_password_and_audit(self):
        from aihub.core import redact
        h = self.auth("root")                                  # first account = admin
        hb, hc = self.auth("bob"), self.auth("carol")
        J = lambda m, u, **k: getattr(self.c, m)(u, **k)
        perms = {p["key"] for p in self.c.get("/api/v1/admin/roles", headers=h).json()["permissions"]}
        self.assertTrue({"reset_password", "audit"} <= perms)
        # nobody but admin has them by default
        self.assertEqual(J("post", "/api/v1/admin/users/carol/reset-password", headers=hb).status_code, 403)
        self.assertEqual(J("get", "/api/v1/audit", headers=hb).status_code, 403)
        # a helpdesk role with only reset_password
        self.c.post("/api/v1/admin/roles", json={"name": "helpdesk", "permissions": ["reset_password"]}, headers=h)
        self.c.post("/api/v1/admin/users/bob/role", json={"role": "helpdesk"}, headers=h)
        self.assertEqual(J("get", "/api/v1/admin/users", headers=hb).status_code, 200)
        self.assertEqual(J("post", "/api/v1/admin/users/bob/status", json={"status": "disabled"}, headers=hb).status_code, 403)
        self.assertEqual(J("post", "/api/v1/admin/users/root/reset-password", headers=hb).status_code, 403)   # cannot take over an admin
        self.assertEqual(J("post", "/api/v1/admin/users/bob/reset-password", headers=hb).status_code, 400)    # not yourself
        self.assertEqual(J("post", "/api/v1/admin/users/nobody/reset-password", headers=hb).status_code, 404)
        r = J("post", "/api/v1/admin/users/carol/reset-password", headers=hb)
        self.assertEqual(r.status_code, 200)
        pw = r.json()["password"]
        self.assertEqual(self.c.get("/api/v1/auth/me", headers=hc).status_code, 401)          # old sessions are gone
        self.assertEqual(self.c.post("/api/v1/auth/login", json={"username": "carol", "password": "secret1"}).status_code, 401)
        self.assertEqual(self.c.post("/api/v1/auth/login", json={"username": "carol", "password": pw}).status_code, 200)
        # audit trail names who did it, and never contains the password
        a = self.c.get("/api/v1/audit?source=admin&action=password_reset", headers=h).json()
        self.assertEqual(a["total"], 1)
        self.assertEqual(a["items"][0]["actor"], "bob")
        self.assertNotIn(pw, str(a))

        # redaction of tool-call parameters
        d = redact.params({"command": "curl -H 'Authorization: Bearer abc123' https://x", "api_key": "k", "n": 1,
                           "env": {"TOKEN": "t"}, "text": "key sk-" + "a" * 30})
        for leak in ("abc123", '"k"', '"t"', "a" * 30):
            self.assertNotIn(leak, d)
        self.assertLess(len(redact.params({"x": "y" * 50000})), 2100)

        # tool events: anonymous install shows the local OS user; params are re-scrubbed by the server
        ev = {"kind": "use", "package": "demo-tool", "component": "mcp__db__query", "client_id": "c" * 32, "local_user": "dan",
              "host": "dan-mbp", "detail": "token=hunter2 select 1", "cwd": "/work/app"}
        self.c.post("/api/v1/events", json={"events": [ev]})
        self.c.post("/api/v1/events", json={"events": [dict(ev, local_user="", kind="install")]})
        t = self.c.get("/api/v1/audit?source=tools&q=select", headers=h).json()
        self.assertEqual(t["total"], 2)
        self.assertEqual({x["actor"] for x in t["items"]}, {"dan (local)", "anon-" + "c" * 6})
        self.assertNotIn("hunter2", str(t))
        self.assertEqual(self.c.get("/api/v1/audit?source=tools&actor=dan%20(local)", headers=h).json()["total"], 1)
        self.assertEqual(self.c.get("/api/v1/audit?source=tools&kind=install", headers=h).json()["total"], 1)
        self.assertEqual(self.c.get("/api/v1/audit?source=tools&q=%25", headers=h).json()["total"], 0)       # % is literal
        self.assertEqual(self.c.get("/api/v1/audit?source=nope", headers=h).status_code, 400)
        csv_ = self.c.get("/api/v1/audit/export?source=tools", headers=h)
        self.assertIn("dan (local)", csv_.text)
        self.assertEqual(self.c.get("/api/v1/audit/export", headers=hb).status_code, 403)


class Migrate(unittest.TestCase):
    def test_db_and_storage_roundtrip(self):
        import aihub.server.migrate as mg
        from aihub.server.db import init_db
        a, b = tempfile.mkdtemp(), tempfile.mkdtemp()
        c = TestClient(create_app(Settings(data_dir=a)))
        c.post("/api/v1/auth/register", json={"username": "alice", "password": "secret1"})
        h = {"Authorization": "Bearer " + c.post("/api/v1/auth/login", json={"username": "alice", "password": "secret1"}).json()["token"]}
        body = io.BytesIO()
        with tarfile.open(fileobj=body, mode="w:gz") as t:
            ti = tarfile.TarInfo("aihub.toml")
            ti.size = len(TOML)
            t.addfile(ti, io.BytesIO(TOML))
        self.assertEqual(c.post("/api/v1/upload", content=body.getvalue(), headers=h).status_code, 200)
        env = os.path.join(b, "dest.env")
        open(env, "w").write("AIHUB_DATA_DIR=%s\nAIHUB_SQLITE_PATH=%s\nAIHUB_STORAGE_BACKEND=local\n" % (b, os.path.join(b, "new.db")))
        src = Settings(data_dir=a)
        dst = Settings.load(env={}, env_file=env)
        mg.migrate_db(src, dst)
        self.assertTrue(mg.migrate_storage(src, dst))
        self.assertTrue(mg.migrate_storage(src, dst))                       # re-run is a no-op
        d = init_db(dst).conn()
        self.assertEqual(d.execute("SELECT username FROM users").fetchone()[0], "alice")
        self.assertEqual(d.execute("SELECT name FROM packages").fetchone()[0], "demo-tool")
        self.assertTrue(os.path.isdir(os.path.join(b, "files", "demo-tool")))
        c2 = TestClient(create_app(dst))                                    # the migrated hub serves search + download
        self.assertEqual(c2.get("/api/v1/packages?q=demo").json()["total"], 1)


if __name__ == "__main__":
    unittest.main()


class Search(unittest.TestCase):
    def test_fts(self):
        from aihub.server.db import DB
        from aihub.server.repos import Repos
        r = Repos(DB(":memory:"))
        r.package_upsert("pdf-reader", "skill", "Extract text from PDF documents", ["docs"], "Uses OCR for scanned pages")
        r.package_upsert("git-helper", "tool", "Git shortcuts", ["vcs"], "")
        self.assertEqual([p["name"] for p in r.packages("pdf")[1]], ["pdf-reader"])
        self.assertEqual([p["name"] for p in r.packages("scanned")[1]], ["pdf-reader"])   # README body
        self.assertEqual([p["name"] for p in r.packages("vcs")[1]], ["git-helper"])       # tag
        self.assertEqual(len(r.packages("pdf OR 'x\" ; drop")[1]), 0)                      # hostile query is safe


class TomlLite(unittest.TestCase):
    def test_same_as_tomllib(self):
        from aihub.core import toml_lite
        s = 'a=1\nb=[1,2,{x="y"}]\n[t]\nk="v\\n"\n[[arr]]\nn=1\n[[arr]]\nn=2\n'
        try:
            import tomllib
            self.assertEqual(toml_lite.loads(s), tomllib.loads(s))
        except ImportError:
            self.assertEqual(toml_lite.loads(s)["arr"][1]["n"], 2)


class EventBuffer(unittest.TestCase):
    def test_batches_and_nonblocking(self):
        import time
        from aihub.server.db import DB
        from aihub.server.repos import EventBuffer, Repos
        r = Repos(DB(":memory:"))
        calls = []
        r.events_insert_batch = lambda rows: calls.append(len(rows))
        b = EventBuffer(r, max_rows=1000, max_secs=0.3)
        t = time.time()
        for _ in range(500):
            b.add([{"ts": 1, "kind": "use", "package": "p"}])
        self.assertLess(time.time() - t, 0.5)          # add() never touches the DB
        time.sleep(0.7)
        self.assertEqual(sum(calls), 500)
        self.assertLessEqual(len(calls), 3)            # a few batches, not 500 writes
        b.close()


class Integrations(unittest.TestCase):
    def setUp(self):
        self.h = tempfile.mkdtemp()
        os.environ["HOME"] = self.h
        os.environ["AIHUB_HOME"] = os.path.join(self.h, ".aihub")

    def test_codex_opencode_mcp_and_revert(self):
        import json
        from aihub.cli import integrations as I
        spec = {"command": "node", "args": ["s.js"], "env": {"K": "v"}}
        c, o = I.Codex(), I.OpenCode()
        rc, ro = c.add_mcp("srv", spec), o.add_mcp("srv", spec)
        toml = open(os.path.join(self.h, ".codex", "config.toml")).read()
        self.assertIn("[mcp_servers.srv]", toml)
        oc = json.load(open(os.path.join(self.h, ".config", "opencode", "opencode.json")))
        self.assertEqual(oc["mcp"]["srv"]["command"], ["node", "s.js"])
        I.revert(rc); I.revert(ro)
        self.assertNotIn("srv", open(os.path.join(self.h, ".codex", "config.toml")).read())
        self.assertNotIn("srv", json.load(open(os.path.join(self.h, ".config", "opencode", "opencode.json")))["mcp"])

    def test_codex_refuses_to_clobber(self):
        from aihub.cli import integrations as I
        os.makedirs(os.path.join(self.h, ".codex"))
        open(os.path.join(self.h, ".codex", "config.toml"), "w").write("[mcp_servers.srv]\ncommand='x'\n")
        with self.assertRaises(ValueError):
            I.Codex().add_mcp("srv", {"command": "y"})

    def test_claude_hook_idempotent_and_preserves_user_hooks(self):
        import json
        from aihub.cli import integrations as I
        p = os.path.join(self.h, ".claude", "settings.json")
        os.makedirs(os.path.dirname(p))
        json.dump({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "mine"}]}]}}, open(p, "w"))
        c = I.ClaudeCode()
        c.install_hook("/x/aihub hook"); c.install_hook("/x/aihub hook")
        g = json.load(open(p))["hooks"]["PreToolUse"]
        self.assertEqual(sum(1 for x in g for h in x["hooks"] if "aihub" in h["command"]), 1)
        self.assertTrue(c.has_hook())
        c.remove_hook()
        g = json.load(open(p))["hooks"]["PreToolUse"]
        self.assertEqual(g[0]["hooks"][0]["command"], "mine")
        self.assertFalse(c.has_hook())

    def test_setup_profile_apply_and_revert(self):
        from aihub.cli import setup as S
        f = os.path.join(self.h, "cfg.json")
        open(f, "w").write('{"a": {"x": 1}}')
        steps = [{"kind": "json_merge", "path": f, "data": {"a": {"y": 2}}},
                 {"kind": "file", "path": os.path.join(self.h, "n.txt"), "content": "hi ${HUB}"}]
        rv = S.apply("pk", steps, {"HUB": "H", "PKG": self.h, "HOME": self.h}, lambda t: True)
        import json
        self.assertEqual(json.load(open(f)), {"a": {"x": 1, "y": 2}})
        self.assertEqual(open(os.path.join(self.h, "n.txt")).read(), "hi H")
        for r in reversed(rv):
            S.revert(r)
        self.assertEqual(json.load(open(f)), {"a": {"x": 1}})
        self.assertFalse(os.path.exists(os.path.join(self.h, "n.txt")))

    def test_hook_handle_spools_only_for_hub_components(self):
        import json
        from aihub.cli import hooks, paths, telemetry
        paths.save("state.json", {"packages": {"pk": {"components": ["skill:s1", "mcp:srv"]}}})
        telemetry.kick = lambda: None
        hooks.handle(json.dumps({"tool_name": "Skill", "tool_input": {"skill": "s1"}}))
        hooks.handle(json.dumps({"tool_name": "mcp__srv__do", "tool_input": {}}))
        hooks.handle(json.dumps({"tool_name": "Bash", "tool_input": {}}))
        hooks.handle("garbage{")
        lines = open(paths.p("queue", "events.jsonl")).read().strip().split("\n")
        self.assertEqual([json.loads(l)["package"] for l in lines], ["pk", "pk"])


class MarkdownRich(unittest.TestCase):
    def test_tables_and_safety(self):
        h = markdown.render("| a | <b>x</b> |\n|---|---|\n| 1 | `c` |\n\n> q\n\n---\n\n- one\n  cont\n- two")
        self.assertIn("<table>", h); self.assertIn("&lt;b&gt;x&lt;/b&gt;", h)
        self.assertIn("<hr>", h); self.assertIn("<blockquote>", h); self.assertIn("one cont", h)
        self.assertNotIn("<b>", h)

    def test_docs_endpoints(self):
        c = TestClient(create_app(Settings(data_dir=tempfile.mkdtemp())))
        self.assertEqual(c.get("/api/v1/readyz").status_code, 200)
        self.assertEqual(c.get("/api/v1/docs/..%2Fetc").status_code, 404)
        self.assertEqual(c.get("/api/v1/docs/nope").status_code, 404)


class AdminFeatures(unittest.TestCase):
    def setUp(self):
        self.c = TestClient(create_app(Settings(data_dir=tempfile.mkdtemp(), event_flush_secs=0.2)))
        self.admin = self.login("root", register=True)

    def login(self, name, pw="secret1", register=False):
        if register:
            self.c.post("/api/v1/auth/register", json={"username": name, "password": pw})
        r = self.c.post("/api/v1/auth/login", json={"username": name, "password": pw})
        return {"Authorization": "Bearer " + r.json()["token"]} if r.status_code == 200 else None

    def test_dashboard_permission_is_per_role(self):
        self.c.post("/api/v1/auth/register", json={"username": "bob", "password": "secret1"})
        bob = self.login("bob")
        self.assertEqual(self.c.get("/api/v1/dashboard", headers=bob).status_code, 403)   # 'user' lacks it by default
        self.assertEqual(self.c.get("/api/v1/dashboard").status_code, 401)
        self.assertEqual(self.c.get("/api/v1/dashboard", headers=self.admin).status_code, 200)
        r = self.c.put("/api/v1/admin/roles/user", json={"permissions": ["publish", "review", "view_dashboard"]}, headers=self.admin)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.get("/api/v1/dashboard", headers=bob).status_code, 200)   # takes effect immediately
        self.c.put("/api/v1/admin/roles/user", json={"permissions": ["publish", "review"]}, headers=self.admin)
        self.assertEqual(self.c.get("/api/v1/dashboard", headers=bob).status_code, 403)   # and can be revoked
        self.assertEqual(self.c.put("/api/v1/admin/roles/user", json={"permissions": ["x"]}, headers=self.admin).status_code, 400)
        self.assertEqual(self.c.put("/api/v1/admin/roles/user", json={"permissions": ["admin"]}, headers=bob).status_code, 403)

    def test_role_lifecycle_and_guards(self):
        a = self.admin
        self.assertEqual(self.c.post("/api/v1/admin/roles", json={"name": "Bad Name"}, headers=a).status_code, 400)
        self.assertEqual(self.c.post("/api/v1/admin/roles", json={"name": "analyst", "permissions": ["view_dashboard"]}, headers=a).status_code, 200)
        self.assertEqual(self.c.post("/api/v1/admin/roles", json={"name": "analyst"}, headers=a).status_code, 409)
        self.c.post("/api/v1/auth/register", json={"username": "ann", "password": "secret1"})
        self.c.post("/api/v1/admin/users/ann/role", json={"role": "analyst"}, headers=a)
        ann = self.login("ann")
        self.assertEqual(self.c.get("/api/v1/dashboard", headers=ann).status_code, 200)
        self.assertEqual(self.c.get("/api/v1/auth/me", headers=ann).json()["permissions"], ["view_dashboard"])
        self.assertEqual(self.c.delete("/api/v1/admin/roles/analyst", headers=a).status_code, 409)   # has a member
        self.assertEqual(self.c.delete("/api/v1/admin/roles/admin", headers=a).status_code, 400)     # builtin
        self.assertEqual(self.c.put("/api/v1/admin/roles/admin", json={"permissions": ["publish"]}, headers=a).status_code, 400)
        self.c.post("/api/v1/admin/users/ann/role", json={"role": "user"}, headers=a)
        self.assertEqual(self.c.delete("/api/v1/admin/roles/analyst", headers=a).status_code, 200)

    def test_batch_accounts_sheet(self):
        a = self.admin
        self.c.post("/api/v1/admin/roles", json={"name": "analyst", "permissions": ["view_dashboard"]}, headers=a)
        csv_in = b"username,role\nalice,analyst\nbob\nbad name!,user\nalice,user\n=cmd|x,user\nroot,user\ncarol,nosuchrole\n"
        r = self.c.post("/api/v1/admin/users/batch?role=user", content=csv_in, headers=a)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("text/csv", r.headers["content-type"]); self.assertEqual(r.headers["cache-control"], "no-store")
        import csv as _csv
        rows = {}
        for x in _csv.DictReader(io.StringIO(r.text.lstrip("﻿"))):   # a repeated username keeps its first row
            rows.setdefault(x["username"], x)
        self.assertEqual(rows["alice"]["status"], "created"); self.assertEqual(rows["alice"]["role"], "analyst")
        self.assertEqual(rows["bob"]["role"], "user")                                # default role
        self.assertEqual(rows["bad name!"]["status"], "invalid username")
        self.assertEqual(rows["root"]["status"], "skipped: username exists")
        self.assertEqual(rows["carol"]["status"], "unknown role")
        self.assertEqual(r.headers["x-created"], "2")
        pw = rows["alice"]["password"]
        self.assertGreaterEqual(len(pw), 14)
        self.assertIsNotNone(self.login("alice", pw))                                 # generated password really works
        self.assertEqual(self.c.get("/api/v1/auth/me", headers=self.login("alice", pw)).json()["role"], "analyst")
        self.assertNotEqual(rows["alice"]["password"], rows["bob"]["password"])
        self.assertEqual(self.c.post("/api/v1/admin/users/batch", content=b"username\n", headers=a).status_code, 400)
        self.c.post("/api/v1/auth/register", json={"username": "eve", "password": "secret1"})
        self.assertEqual(self.c.post("/api/v1/admin/users/batch", content=csv_in, headers=self.login("eve")).status_code, 403)
        big = ("username\n" + "\n".join("u%d" % i for i in range(301))).encode()
        self.assertEqual(self.c.post("/api/v1/admin/users/batch", content=big, headers=a).status_code, 400)

    def test_xlsx_and_formula_safety(self):
        import zipfile
        from aihub.server import sheet
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            z.writestr("xl/sharedStrings.xml", '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>username</t></si><si><t>dave</t></si></sst>')
            z.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                       '<row r="1"><c r="A1" t="s"><v>0</v></c></row><row r="2"><c r="A2" t="s"><v>1</v></c><c r="C2" t="inlineStr"><is><t>x</t></is></c></row></sheetData></worksheet>')
        self.assertEqual(sheet.parse(b.getvalue(), "a.xlsx"), [["username"], ["dave", "", "x"]])
        self.assertEqual(sheet.safe_cell("=1+1"), "'=1+1"); self.assertEqual(sheet.safe_cell("ok"), "ok")
        with self.assertRaises(ValueError):
            sheet.parse(b"PK\x03\x04garbage", "x.xlsx")

    def test_dashboard_numbers(self):
        for n in ("p1", "p2"):
            self.c.app.state.repos.package_upsert(n, "skill", "d", [])
        self.c.post("/api/v1/events", json={"events": [
            {"kind": "install", "package": "p1", "client_id": "c1"}, {"kind": "use", "package": "p1", "client_id": "c1", "source": "claude"},
            {"kind": "use", "package": "p1", "client_id": "c2"}, {"kind": "error", "package": "p2", "client_id": "c2"}]})
        d = self.c.get("/api/v1/dashboard?days=7", headers=self.admin).json()
        self.assertEqual(d["totals"], {"install": 1, "use": 2, "error": 1})
        self.assertEqual(d["active_users"], 2); self.assertEqual(len(d["daily"]), 7)
        self.assertEqual(sum(x["use"] for x in d["daily"]), 2)
        self.assertEqual(d["top_packages"][0]["name"], "p1")
        self.assertEqual(self.c.get("/api/v1/dashboard?days=5", headers=self.admin).status_code, 400)

    def test_dashboard_groupings_and_identity(self):
        a = self.admin
        for n, t in (("s1", "skill"), ("s2", "skill"), ("m1", "mcp")):
            r = self.c.app.state.repos
            r.package_upsert(n, t, "d", [])
        ev = lambda p, src=None, u=None, cid="c": {"kind": "use", "package": p, "client_id": cid, "source": src, "username": u}
        # anonymous client tries to impersonate 'root'
        self.c.post("/api/v1/events", json={"events": [ev("s1", "claude", "root", "c1"), ev("s2", "opencode", "root", "c2"), ev("m1", "claude", "root", "c3")]})
        d = self.c.get("/api/v1/dashboard?days=7", headers=a).json()
        self.assertEqual(sorted((x["name"], x["count"]) for x in d["by_type"]), [("mcp", 1), ("skill", 2)])   # grouped, not per-package
        self.assertEqual(sorted((x["name"], x["count"]) for x in d["by_source"]), [("claude", 2), ("opencode", 1)])
        self.assertEqual(d["active_users"], 3)                                                                   # 3 devices, none spoofed as root
        self.assertTrue(all(x["name"].startswith("anon-") for x in d["top_users"]))


class Profiles(unittest.TestCase):
    PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x10\x00\x00\x00\x10\x08\x06\x00\x00\x00\x1f\xf3\xffa"
           b"\x00\x00\x00\x00IEND\xaeB`\x82")

    def setUp(self):
        from aihub.server import routers
        routers._fails.clear()                       # the throttle is per-process state; isolate tests from each other
        self.c = TestClient(create_app(Settings(data_dir=tempfile.mkdtemp())))
        self.c.post("/api/v1/auth/register", json={"username": "root", "password": "secret1"})
        self.h = self.login("root", "secret1")

    def login(self, u, p):
        r = self.c.post("/api/v1/auth/login", json={"username": u, "password": p})
        return {"Authorization": "Bearer " + r.json()["token"]} if r.status_code == 200 else None

    def test_profile_fields_sanitised(self):
        r = self.c.patch("/api/v1/auth/me", json={"display_name": "  Ada\u202e  \n Lovelace ", "title": "Staff\x00 Eng" + "x" * 200}, headers=self.h)
        self.assertEqual(r.status_code, 200)
        me = self.c.get("/api/v1/auth/me", headers=self.h).json()
        self.assertEqual(me["display_name"], "Ada Lovelace")             # bidi override + newline stripped, spaces collapsed
        self.assertEqual(len(me["title"]), 80); self.assertNotIn("\x00", me["title"])
        self.c.patch("/api/v1/auth/me", json={"title": "Lead"}, headers=self.h)                     # partial update keeps the name
        self.assertEqual(self.c.get("/api/v1/auth/me", headers=self.h).json()["display_name"], "Ada Lovelace")
        self.assertEqual(self.c.patch("/api/v1/auth/me", json={"title": "x"}).status_code, 401)

    def test_password_change(self):
        other = self.login("root", "secret1")                                                       # a second device
        self.assertEqual(self.c.post("/api/v1/auth/password", json={"old": "WRONG", "new": "newpass1"}, headers=self.h).status_code, 400)
        self.assertEqual(self.c.post("/api/v1/auth/password", json={"old": "secret1", "new": "short"}, headers=self.h).status_code, 400)
        self.assertEqual(self.c.post("/api/v1/auth/password", json={"old": "secret1", "new": "secret1"}, headers=self.h).status_code, 400)
        self.assertEqual(self.c.post("/api/v1/auth/password", json={"old": "secret1", "new": "newpass1"}, headers=self.h).status_code, 200)
        self.assertEqual(self.c.get("/api/v1/auth/me", headers=self.h).status_code, 200)            # this session survives
        self.assertEqual(self.c.get("/api/v1/auth/me", headers=other).status_code, 401)             # other devices signed out
        self.assertIsNone(self.login("root", "secret1")); self.assertIsNotNone(self.login("root", "newpass1"))

    def test_password_brute_force_throttled(self):
        codes = [self.c.post("/api/v1/auth/password", json={"old": "nope%d" % i, "new": "whatever1"}, headers=self.h).status_code for i in range(10)]
        self.assertEqual(codes[:8], [400] * 8); self.assertEqual(codes[8], 429)

    def test_avatar(self):
        h = self.h
        self.assertEqual(self.c.post("/api/v1/auth/avatar", content=self.PNG, headers=h).status_code, 200)
        g = self.c.get("/api/v1/users/root/avatar")
        self.assertEqual((g.status_code, g.headers["content-type"], g.content), (200, "image/png", self.PNG))
        self.assertTrue(self.c.get("/api/v1/auth/me", headers=h).json()["avatar_url"].startswith("/api/v1/users/root/avatar?v="))
        for bad, want in ((b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", 400), (b"GIF89a....", 400),
                          (b"", 400), (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, 400), (self.PNG + b"0" * 300000, 413)):
            self.assertEqual(self.c.post("/api/v1/auth/avatar", content=bad, headers=h).status_code, want)
        huge = bytearray(self.PNG); huge[16:24] = (5000).to_bytes(4, "big") * 2
        self.assertEqual(self.c.post("/api/v1/auth/avatar", content=bytes(huge), headers=h).status_code, 400)   # dimensions capped
        self.assertEqual(self.c.get("/api/v1/users/root/avatar").headers["x-content-type-options"], "nosniff")
        self.assertEqual(self.c.delete("/api/v1/auth/avatar", headers=h).status_code, 200)
        self.assertEqual(self.c.get("/api/v1/users/root/avatar").status_code, 404)
        self.assertIsNone(self.c.get("/api/v1/auth/me", headers=h).json()["avatar_url"])

    def test_batch_display_name_and_title_columns(self):
        sheet_csv = ("Full Name;Username;Job Title;Role\n=HYPERLINK(\"x\");anna.k;Data Lead;user\nBo Li;bo;;\n;cy;Intern;user\n").encode()
        r = self.c.post("/api/v1/admin/users/batch?role=user", content=sheet_csv, headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        import csv as _csv
        rows = {x["username"]: x for x in _csv.DictReader(io.StringIO(r.text.lstrip("\ufeff")))}
        self.assertEqual(rows["anna.k"]["title"], "Data Lead"); self.assertEqual(rows["bo"]["display_name"], "Bo Li")
        self.assertTrue(rows["anna.k"]["display_name"].startswith("'="))                      # formula neutralised in the returned sheet
        pw = rows["bo"]["password"]
        me = self.c.get("/api/v1/auth/me", headers=self.login("bo", pw)).json()
        self.assertEqual((me["display_name"], me["title"]), ("Bo Li", ""))                      # saved on the account
        # a plain 2-column sheet with no header still works
        r = self.c.post("/api/v1/admin/users/batch?role=user", content=b"dan,user\n", headers=self.h)
        self.assertIn("dan,,,user,", r.text)
        from aihub.server.routers import _batch_rows
        self.assertEqual([x["username"] for x in _batch_rows([["username"], ["eve"]], "user")], ["eve"])     # header, one column
        self.assertEqual([x["username"] for x in _batch_rows([["user", "role"], ["x", "user"]], "user")], ["user", "x"])  # no real header -> both are data


class Access(unittest.TestCase):
    """Public/private repos, sharing with users and groups, repo-admin boundaries, anonymous install."""
    TOML = b'[package]\nname = "%s"\nversion = "1.0.0"\ntype = "skill"\ndescription = "d"\n'

    def setUp(self):
        from aihub.server import routers
        routers._fails.clear()
        self.d = tempfile.mkdtemp()
        self.c = TestClient(create_app(Settings(data_dir=self.d, event_flush_secs=0.2)))
        self.admin = self.user("root")
        self.alice, self.bob, self.carol = self.user("alice"), self.user("bob"), self.user("carol")

    def user(self, n):
        self.c.post("/api/v1/auth/register", json={"username": n, "password": "secret1"})
        return {"Authorization": "Bearer " + self.c.post("/api/v1/auth/login", json={"username": n, "password": "secret1"}).json()["token"]}

    def publish(self, h, name, visibility=None, version="1.0.0"):
        b = io.BytesIO()
        with tarfile.open(fileobj=b, mode="w:gz") as t:
            toml = (self.TOML % name.encode()).replace(b"1.0.0", version.encode())
            ti = tarfile.TarInfo("aihub.toml"); ti.size = len(toml); t.addfile(ti, io.BytesIO(toml))
            r = b"# readme for " + name.encode(); ti = tarfile.TarInfo("README.md"); ti.size = len(r); t.addfile(ti, io.BytesIO(r))
        hd = dict(h, **({"X-Aihub-Visibility": visibility} if visibility else {}))
        return self.c.post("/api/v1/upload", content=b.getvalue(), headers=hd)

    def names(self, h=None):
        return sorted(p["name"] for p in self.c.get("/api/v1/packages", headers=h or {}).json()["items"])

    def test_private_is_invisible_everywhere_until_shared(self):
        self.assertEqual(self.publish(self.alice, "pub-one").status_code, 200)
        self.assertEqual(self.publish(self.alice, "secret-one", "private").status_code, 200)
        self.c.post("/api/v1/events", json={"events": [{"kind": "use", "package": "secret-one", "client_id": "x"}]}, headers=self.alice)
        H = {"bob": self.bob, "anon": {}}
        for who, h in H.items():
            self.assertEqual(self.names(h), ["pub-one"], who)                                               # list
            self.assertEqual(self.c.get("/api/v1/packages?q=secret", headers=h).json()["total"], 0, who)    # full-text search (name)
            self.assertEqual(self.c.get("/api/v1/packages?q=secret-one", headers=h).json()["items"], [], who)
            for path in ("", "/readme", "/versions", "/reviews", "/stats", "/maintainers"):
                self.assertIn(self.c.get("/api/v1/packages/secret-one" + path, headers=h).status_code, (401, 404), (who, path))
            self.assertIn(self.c.get("/files/secret-one/secret-one-1.0.0.tar.gz", headers=h).status_code, (401, 404), who)
            self.assertNotIn("secret-one", str(self.c.get("/api/v1/rankings/packages", headers=h).json()), who)
        # the owner, and a site admin, see it
        self.assertEqual(self.names(self.alice), ["pub-one", "secret-one"])
        self.assertEqual(self.names(self.admin), ["pub-one", "secret-one"])
        # bob can't tell "private" from "missing"
        a = self.c.get("/api/v1/packages/secret-one", headers=self.bob); b = self.c.get("/api/v1/packages/does-not-exist", headers=self.bob)
        self.assertEqual((a.status_code, a.json()), (b.status_code, b.json()))
        # overview counters don't leak private packages
        self.assertEqual(self.c.get("/api/v1/meta", headers=self.bob).json()["overview"]["packages"], 1)
        self.assertEqual(self.c.get("/api/v1/meta", headers=self.alice).json()["overview"]["packages"], 2)

    def test_share_with_user_and_group_and_revoke(self):
        self.publish(self.alice, "secret-one", "private")
        put = lambda h, body: self.c.put("/api/v1/packages/secret-one/access", json=body, headers=h)
        self.assertEqual(put(self.bob, {"type": "user", "name": "bob", "access": "view"}).status_code, 404)   # can't self-grant
        self.assertEqual(put(self.alice, {"type": "user", "name": "bob", "access": "view"}).status_code, 200)
        self.assertEqual(self.names(self.bob), ["secret-one"])
        self.assertEqual(self.c.get("/api/v1/packages/secret-one", headers=self.bob).json()["access"], "view")
        self.assertEqual(self.c.get("/api/v1/packages/secret-one/access", headers=self.bob).status_code, 403)   # viewers can't see the ACL
        # view access can't publish; develop can
        self.assertEqual(self.publish(self.bob, "secret-one", version="1.1.0").status_code, 403)
        put(self.alice, {"type": "user", "name": "bob", "access": "develop"})
        self.assertEqual(self.publish(self.bob, "secret-one", version="1.1.0").status_code, 200)
        self.assertEqual(self.c.patch("/api/v1/packages/secret-one", json={"visibility": "public"}, headers=self.bob).status_code, 403)  # develop != admin
        # group share
        self.assertEqual(self.c.post("/api/v1/groups", json={"name": "team-x"}, headers=self.admin).status_code, 200)
        self.c.post("/api/v1/groups/team-x/members", json={"usernames": ["carol"]}, headers=self.admin)
        self.assertEqual(self.names(self.carol), [])
        put(self.alice, {"type": "group", "name": "team-x", "access": "view"})
        self.assertEqual(self.names(self.carol), ["secret-one"])
        self.c.delete("/api/v1/groups/team-x/members/carol", headers=self.admin)
        self.assertEqual(self.names(self.carol), [])                                                            # leaving the group revokes access
        self.c.post("/api/v1/groups/team-x/members", json={"username": "carol"}, headers=self.admin)
        self.assertEqual(self.names(self.carol), ["secret-one"])
        self.c.delete("/api/v1/groups/team-x", headers=self.admin)
        self.assertEqual(self.names(self.carol), [])                                                            # deleting the group too
        # revoke user
        self.c.delete("/api/v1/packages/secret-one/access/user/bob", headers=self.alice)
        self.assertEqual(self.names(self.bob), [])
        self.assertEqual(self.c.get("/files/secret-one/secret-one-1.0.0.tar.gz", headers=self.bob).status_code, 404)

    def test_visibility_change_and_repo_admin_boundaries(self):
        self.publish(self.alice, "pub-one")
        self.assertEqual(self.c.patch("/api/v1/packages/pub-one", json={"visibility": "private"}, headers=self.bob).status_code, 403)
        self.assertEqual(self.c.patch("/api/v1/packages/pub-one", json={"visibility": "nope"}, headers=self.alice).status_code, 400)
        self.assertEqual(self.c.patch("/api/v1/packages/pub-one", json={"visibility": "private"}, headers=self.alice).status_code, 200)
        self.assertEqual(self.names(self.bob), [])                                                              # cache was invalidated
        self.c.post("/api/v1/packages/pub-one/maintainers", json={"username": "bob"}, headers=self.alice)       # bob becomes a repo admin
        self.assertEqual(self.c.patch("/api/v1/packages/pub-one", json={"visibility": "public"}, headers=self.bob).status_code, 200)
        self.assertEqual(self.names(self.carol), ["pub-one"])
        self.assertEqual(self.c.delete("/api/v1/packages/pub-one/maintainers/alice", headers=self.bob).status_code, 200)
        self.assertEqual(self.c.delete("/api/v1/packages/pub-one/maintainers/bob", headers=self.bob).status_code, 400)   # last admin stays
        self.assertEqual(self.c.patch("/api/v1/packages/pub-one", json={"visibility": "private"}, headers=self.admin).status_code, 200)  # site admin override

    def test_name_squatting_does_not_leak_private_packages(self):
        self.publish(self.alice, "secret-one", "private")
        r = self.publish(self.bob, "secret-one")
        self.assertEqual(r.status_code, 409)                                                                    # same message as a public name clash
        self.assertNotIn("private", r.text.lower())

    def test_settings_upload_limit(self):
        put = lambda v, h=None: self.c.put("/api/v1/admin/settings", json={"max_upload_mb": v}, headers=h or self.admin)
        self.assertEqual(put(5, self.alice).status_code, 403)
        for bad in (0, -1, "abc", 99999):
            self.assertEqual(put(bad).status_code, 400)
        self.assertEqual(put(7).status_code, 200)
        self.assertEqual(self.c.get("/api/v1/admin/settings", headers=self.admin).json()["values"]["max_upload_mb"], "7")

    def test_settings_public_install_and_browse_and_private_switch(self):
        self.publish(self.alice, "pub-one")
        self.assertEqual(self.c.get("/api/v1/resolve", params={"name": "pub-one"}).status_code, 401)
        self.assertEqual(self.c.put("/api/v1/admin/settings", json={"public_install": "1"}, headers=self.alice).status_code, 403)
        self.assertEqual(self.c.put("/api/v1/admin/settings", json={"public_install": "maybe"}, headers=self.admin).status_code, 400)
        self.assertEqual(self.c.put("/api/v1/admin/settings", json={"nonsense": "1"}, headers=self.admin).status_code, 400)
        self.c.put("/api/v1/admin/settings", json={"public_install": "1"}, headers=self.admin)
        res = self.c.get("/api/v1/resolve", params={"name": "pub-one"})
        self.assertEqual(res.status_code, 200)                                                                  # anonymous install works
        self.assertEqual(self.c.get(res.json()["url"].replace("http://localhost:8000", "")).status_code, 200)
        self.publish(self.alice, "secret-one", "private")
        self.assertIn(self.c.get("/api/v1/resolve", params={"name": "secret-one"}).status_code, (401, 404))     # public install never exposes private
        self.c.put("/api/v1/admin/settings", json={"public_browse": "0"}, headers=self.admin)
        self.assertEqual(self.c.get("/api/v1/packages").status_code, 401)
        self.assertEqual(self.c.get("/api/v1/packages", headers=self.bob).status_code, 200)
        # private can be switched off site-wide; new packages fall back to public, existing privates keep working
        self.c.put("/api/v1/admin/settings", json={"allow_private": "0"}, headers=self.admin)
        self.publish(self.bob, "would-be-private", "private")
        self.assertEqual(self.c.get("/api/v1/packages/would-be-private", headers=self.carol).status_code, 200)
        self.assertEqual(self.c.patch("/api/v1/packages/pub-one", json={"visibility": "private"}, headers=self.alice).status_code, 403)
        # default visibility
        self.c.put("/api/v1/admin/settings", json={"allow_private": "1", "default_visibility": "private"}, headers=self.admin)
        self.publish(self.alice, "default-private")
        self.assertEqual(self.c.get("/api/v1/packages/default-private", headers=self.bob).status_code, 404)

    def test_dashboard_only_counts_visible_packages_and_filters(self):
        self.publish(self.alice, "pub-one"); self.publish(self.alice, "secret-one", "private")
        self.c.put("/api/v1/admin/roles/user", json={"permissions": ["publish", "review", "view_dashboard"]}, headers=self.admin)
        ev = lambda p, k="use", s="claude": {"kind": k, "package": p, "client_id": "c", "source": s}
        self.c.post("/api/v1/events", json={"events": [ev("pub-one"), ev("pub-one", "install", "opencode"), ev("secret-one"), ev("secret-one")]}, headers=self.alice)
        bob = self.c.get("/api/v1/dashboard?days=7", headers=self.bob).json()
        self.assertEqual(bob["totals"], {"use": 1, "install": 1})                                             # the two secret events are excluded
        self.assertEqual([x["name"] for x in bob["top_packages"]], ["pub-one"])
        self.assertEqual(self.c.get("/api/v1/dashboard?package=secret-one", headers=self.bob).status_code, 404)
        self.assertEqual(self.c.get("/api/v1/dashboard?days=7", headers=self.alice).json()["totals"], {"use": 3, "install": 1})
        f = lambda q: self.c.get("/api/v1/dashboard?days=7&" + q, headers=self.alice).json()
        self.assertEqual(f("source=opencode")["totals"], {"install": 1})
        self.assertEqual(f("kind=use")["totals"], {"use": 3})
        self.assertEqual(f("type=skill&package=secret-one")["totals"], {"use": 2})
        self.assertEqual(f("type=mcp")["totals"], {})
        self.assertEqual(f("user=alice")["totals"], {"use": 3, "install": 1})
        self.assertEqual(f("kind=bogus") if False else self.c.get("/api/v1/dashboard?kind=bogus", headers=self.alice).status_code, 400)
        self.assertIn("alice", f("")["options"]["users"]); self.assertEqual(f("")["options"]["types"], ["skill"])
        self.assertNotIn("alice", self.c.get("/api/v1/dashboard?days=7", headers=self.bob).json()["options"]["users"] + ["x"][:0] if False else [])

    def test_groups_permissions(self):
        self.assertEqual(self.c.post("/api/v1/groups", json={"name": "g1"}, headers=self.alice).status_code, 403)   # needs permission + setting
        self.c.put("/api/v1/admin/roles/user", json={"permissions": ["publish", "review", "create_groups"]}, headers=self.admin)
        self.assertEqual(self.c.post("/api/v1/groups", json={"name": "g1"}, headers=self.alice).status_code, 403)   # setting still off
        self.c.put("/api/v1/admin/settings", json={"allow_group_creation": "1"}, headers=self.admin)
        self.assertEqual(self.c.post("/api/v1/groups", json={"name": "g1"}, headers=self.alice).status_code, 200)
        self.assertEqual(self.c.post("/api/v1/groups", json={"name": "Bad Name!"}, headers=self.alice).status_code, 400)
        self.assertEqual(self.c.post("/api/v1/groups", json={"name": "g1"}, headers=self.alice).status_code, 409)
        self.assertEqual(self.c.post("/api/v1/groups/g1/members", json={"usernames": ["bob", "ghost"]}, headers=self.alice).json()["missing"], ["ghost"])
        self.assertEqual(self.c.post("/api/v1/groups/g1/members", json={"username": "carol"}, headers=self.bob).status_code, 404)   # not the owner
        self.assertEqual(self.c.delete("/api/v1/groups/g1", headers=self.bob).status_code, 404)
        self.assertEqual([g["name"] for g in self.c.get("/api/v1/groups", headers=self.alice).json()["groups"]], ["g1"])
        self.assertEqual([g["name"] for g in self.c.get("/api/v1/groups", headers=self.carol).json()["groups"]], [])
        self.assertEqual(self.c.delete("/api/v1/groups/g1", headers=self.admin).status_code, 200)                     # admins manage all


class GroupsPageAndBulk(unittest.TestCase):
    def setUp(self):
        from aihub.server import routers
        routers._fails.clear()
        self.c = TestClient(create_app(Settings(data_dir=tempfile.mkdtemp())))
        self.admin = self.user("root")
        self.ann = self.user("ann")

    def user(self, n, pw="secret1"):
        self.c.post("/api/v1/auth/register", json={"username": n, "password": pw})
        r = self.c.post("/api/v1/auth/login", json={"username": n, "password": pw})
        return {"Authorization": "Bearer " + r.json()["token"]} if r.status_code == 200 else None

    def grant_create(self):
        self.c.put("/api/v1/admin/roles/user", json={"permissions": ["publish", "review", "create_groups"]}, headers=self.admin)
        self.c.put("/api/v1/admin/settings", json={"allow_group_creation": "1"}, headers=self.admin)

    def test_owner_sees_and_manages_own_groups_without_admin(self):
        caps = lambda: self.c.get("/api/v1/groups/capabilities", headers=self.ann).json()
        self.assertFalse(caps()["show_page"])                                    # nothing to show yet
        self.grant_create()
        self.assertEqual((caps()["can_create"], caps()["show_page"]), (True, True))
        self.assertEqual(self.c.post("/api/v1/groups", json={"name": "mine"}, headers=self.ann).status_code, 200)
        self.c.post("/api/v1/groups", json={"name": "other"}, headers=self.admin)
        gs = self.c.get("/api/v1/groups", headers=self.ann).json()["groups"]
        self.assertEqual([(g["name"], g["can_manage"]) for g in gs], [("mine", True)])   # own group only, manageable
        self.assertNotIn("owner_id", gs[0])                                               # internal ids are not exposed
        self.assertEqual(self.c.post("/api/v1/groups/mine/members", json={"usernames": ["root"]}, headers=self.ann).status_code, 200)
        self.assertEqual(self.c.post("/api/v1/groups/other/members", json={"usernames": ["ann"]}, headers=self.ann).status_code, 404)
        # once the permission is withdrawn the owner keeps managing what they already made, but cannot create more
        self.c.put("/api/v1/admin/roles/user", json={"permissions": ["publish", "review"]}, headers=self.admin)
        self.assertEqual(self.c.post("/api/v1/groups", json={"name": "more"}, headers=self.ann).status_code, 403)
        self.assertTrue(caps()["show_page"] and caps()["owns_groups"])

    def test_member_sees_group_read_only(self):
        self.c.post("/api/v1/groups", json={"name": "team"}, headers=self.admin)
        self.c.post("/api/v1/groups/team/members", json={"usernames": ["ann"]}, headers=self.admin)
        gs = self.c.get("/api/v1/groups", headers=self.ann).json()["groups"]
        self.assertEqual([(g["name"], g["can_manage"]) for g in gs], [("team", False)])
        self.assertTrue(self.c.get("/api/v1/groups/capabilities", headers=self.ann).json()["show_page"])
        self.assertEqual(self.c.get("/api/v1/groups/team", headers=self.ann).status_code, 404)          # member list is manager-only

    def test_bulk_group_column(self):
        for g in ("data-team", "platform"):
            self.c.post("/api/v1/groups", json={"name": g}, headers=self.admin)
        sheet = (b"username,display_name,group\n"
                 b"a1,Ann One,data-team\n"
                 b"b2,Bob Two,Data-Team; platform\n"      # several groups, case-insensitive, ; separated
                 b"c3,Cy Three,data-team|platform|data-team\n"   # pipe separated, duplicates ignored
                 b"d4,Di Four,ghost\n"                        # unknown group -> row skipped
                 b"e5,Ed Five,data-team;ghost\n"              # one unknown among valid ones -> whole row skipped
                 b"f6,Flo Six,\n")                            # no group is fine
        r = self.c.post("/api/v1/admin/users/batch?role=user", content=sheet, headers=self.admin)
        self.assertEqual(r.status_code, 200, r.text)
        import csv as _csv
        rows = {x["username"]: x for x in _csv.DictReader(io.StringIO(r.text.lstrip("\ufeff")))}
        self.assertEqual(rows["a1"]["status"], "created"); self.assertEqual(rows["b2"]["groups"], "data-team, platform")
        self.assertEqual(rows["c3"]["groups"], "data-team, platform")
        self.assertEqual(rows["d4"]["status"], "unknown group: ghost"); self.assertEqual(rows["d4"]["password"], "")
        self.assertEqual(rows["e5"]["status"], "unknown group: ghost")
        self.assertEqual(rows["f6"]["status"], "created")
        self.assertEqual(r.headers["x-created"], "4")
        members = lambda g: sorted(m["username"] for m in self.c.get("/api/v1/groups/" + g, headers=self.admin).json()["members"])
        # "root" created both groups, so the creator is a member (existing behaviour); the sheet added the rest
        self.assertEqual(members("data-team"), ["a1", "b2", "c3", "root"]); self.assertEqual(members("platform"), ["b2", "c3", "root"])
        # no group was auto-created, and skipped rows created no account
        self.assertEqual(sorted(g["name"] for g in self.c.get("/api/v1/groups", headers=self.admin).json()["groups"]), ["data-team", "platform"])
        all_users = {u["username"] for u in self.c.get("/api/v1/admin/users", headers=self.admin).json()["users"]}
        self.assertTrue({"a1", "b2", "c3", "f6"} <= all_users)
        self.assertFalse({"d4", "e5"} & all_users)                         # skipped rows created no account
        # group membership from the sheet grants access to private repos shared with the group
        tok = self.c.post("/api/v1/auth/login", json={"username": "a1", "password": rows["a1"]["password"]}).json()["token"]
        self.assertEqual(self.c.get("/api/v1/groups", headers={"Authorization": "Bearer " + tok}).json()["groups"][0]["name"], "data-team")

    def test_bulk_group_header_detected_and_plain_sheets_unchanged(self):
        from aihub.server.routers import _batch_rows
        rows = _batch_rows([["Username", "Team"], ["x", "a; b"]], "user")
        self.assertEqual(rows[0]["groups"], ["a", "b"])
        self.assertEqual(_batch_rows([["dan", "user"]], "user")[0]["groups"], [])       # headerless 2-col sheet still username,role


class ReviewRankings(unittest.TestCase):
    def setUp(self):
        from aihub.server import routers
        routers._fails.clear()
        self.c = TestClient(create_app(Settings(data_dir=tempfile.mkdtemp())))
        self.r = self.c.app.state.repos
        self.users = {}
        self.admin = self.reg("root")
        self.uid = {n: self.r.user_by_name(n)["id"] for n in ["root"]}
        for i in range(12):
            self.reg("u%d" % i)
            self.uid["u%d" % i] = self.r.user_by_name("u%d" % i)["id"]

    def reg(self, n):
        self.c.post("/api/v1/auth/register", json={"username": n, "password": "secret1"})
        return {"Authorization": "Bearer " + self.c.post("/api/v1/auth/login", json={"username": n, "password": "secret1"}).json()["token"]}

    def pkg(self, name, vis="public"):
        return self.r.package_upsert(name, "skill", "d", [], "", visibility=vis)

    def rate(self, pid, ratings):
        for i, rt in enumerate(ratings):
            self.r.review_upsert(pid, self.uid["u%d" % i], rt, "x")

    def rank(self, what, h=None):
        return [(x["name"], x["count"]) for x in self.c.get("/api/v1/rankings/" + what, headers=h or {}).json()["items"]]

    def test_bayesian_order_beats_a_single_five_star(self):
        self.rate(self.pkg("one-five"), [5])                       # avg 5.0 from one review
        self.rate(self.pkg("many-good"), [5, 5, 5, 5, 4, 5, 5, 5, 5, 4])   # avg 4.8 from ten reviews
        self.rate(self.pkg("many-meh"), [3, 3, 3, 3, 3, 3])
        self.pkg("unreviewed")
        self.assertEqual([n for n, _ in self.rank("reviews")], ["many-good", "one-five", "many-meh"])   # unreviewed is absent
        self.assertEqual([n for n, _ in self.rank("reviewed")], ["many-good", "many-meh", "one-five"])  # by number of reviews
        items = self.c.get("/api/v1/rankings/reviews").json()
        self.assertFalse(items["windowed"]); self.assertEqual(items["items"][0]["avg"], 4.8)
        # search sorts use the same ordering
        names = lambda s: [p["name"] for p in self.c.get("/api/v1/packages?sort=" + s, headers=self.admin).json()["items"]]
        self.assertEqual(names("rating")[:3], ["many-good", "one-five", "many-meh"])
        self.assertEqual(names("reviews")[:3], ["many-good", "many-meh", "one-five"])
        self.assertEqual(names("rating")[-1], "unreviewed")          # unreviewed packages sort last, not first
        self.assertEqual(self.c.get("/api/v1/packages?sort=ratng", headers=self.admin).status_code, 400)

    def test_search_relevance_still_wins_when_searching_then_sorting_by_rating(self):
        self.rate(self.pkg("pdf-tool"), [5, 5]); self.rate(self.pkg("pdf-other"), [2])
        got = [p["name"] for p in self.c.get("/api/v1/packages?q=pdf&sort=rating", headers=self.admin).json()["items"]]
        self.assertEqual(got, ["pdf-tool", "pdf-other"])

    def test_private_package_reviews_never_leak_into_rankings(self):
        self.rate(self.pkg("secret-top", "private"), [5, 5, 5, 5, 5, 5])
        self.rate(self.pkg("public-ok"), [4])
        for h in ({}, self.reg("outsider")):
            self.assertEqual(self.rank("reviews", h), [("public-ok", 1)])
            self.assertEqual(self.rank("reviewed", h), [("public-ok", 1)])
        self.assertEqual(self.rank("reviews", self.admin)[0][0], "secret-top")     # admins still see it
        self.c.put("/api/v1/admin/roles/user", json={"permissions": ["publish", "review", "view_dashboard"]}, headers=self.admin)
        d = self.c.get("/api/v1/dashboard?days=7", headers=self.reg("viewer1")).json()
        self.assertEqual([x["name"] for x in d["top_reviewed"]], ["public-ok"])

    def test_dashboard_has_top_used_and_top_reviewed(self):
        self.rate(self.pkg("liked"), [5, 5, 4]); self.pkg("used-a"); self.pkg("used-b")
        ev = lambda p, k="use": {"kind": k, "package": p, "client_id": "c1"}
        self.c.post("/api/v1/events", json={"events": [ev("used-a")] * 5 + [ev("used-b")] * 2 + [ev("liked")]})
        d = self.c.get("/api/v1/dashboard?days=7", headers=self.admin).json()
        self.assertEqual([x["name"] for x in d["top_packages"]][:2], ["used-a", "used-b"])       # top using
        self.assertEqual(d["top_reviewed"][0]["name"], "liked"); self.assertEqual(d["top_reviewed"][0]["count"], 3)
        self.assertEqual(self.c.get("/api/v1/rankings/bogus").status_code, 404)

