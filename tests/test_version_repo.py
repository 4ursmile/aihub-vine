"""An OLD version of a package that later moved to another repo / branch / folder (typically into a monorepo) must still install.

The index entry keeps ONE repo (the latest publish); a version used to keep only its `ref`, so `aihub install X@1.0.0` looked for the old
commit in the new repo: "cannot find '<sha>'". Now a version published from somewhere else carries its own `repo`; databases written
before that are repaired from the publish events; the transfer route and the CLI look in the package's other known places as a last resort.
Real local git repositories."""
import base64
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from aihub.cli import gitx, registry
from aihub.core import pkgindex
from aihub.server import sync as syncmod
from aihub.server.config import Settings
from aihub.server.main import create_app


def git(*a, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def toml(ver, name="p"):
    return '[package]\nname = "%s"\nversion = "%s"\ntype = "skill"\ndescription = "d"\n' % (name, ver)


def b64(s):
    return base64.b64encode(s.encode()).decode()


class World(unittest.TestCase):
    """repo A (single package at the root) holds p 1.0.0; later p moved into the monorepo B (folder p, branch dev) as 2.0.0."""

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.td, True)
        self.a, self.b, self.c = [os.path.join(self.td, x) for x in ("single", "mono", "third")]
        for d in (self.a, self.b, self.c):
            os.makedirs(d)
        git("init", "-q", "-b", "main", cwd=self.a)
        open(os.path.join(self.a, "aihub.toml"), "w").write(toml("1.0.0"))
        git("add", ".", cwd=self.a)
        git("commit", "-qm", "v1", cwd=self.a)
        self.ca = git("rev-parse", "HEAD", cwd=self.a)
        git("init", "-q", "-b", "dev", cwd=self.b)
        os.makedirs(os.path.join(self.b, "p"))
        open(os.path.join(self.b, "p", "aihub.toml"), "w").write(toml("2.0.0"))
        git("add", ".", cwd=self.b)
        git("commit", "-qm", "v2", cwd=self.b)
        self.cb = git("rev-parse", "HEAD", cwd=self.b)
        git("init", "-q", "-b", "main", cwd=self.c)                      # a third repo that has neither commit
        open(os.path.join(self.c, "x"), "w").write("x")
        git("add", ".", cwd=self.c)
        git("commit", "-qm", "x", cwd=self.c)
        self.ua, self.ub, self.uc = ["file://" + x for x in (self.a, self.b, self.c)]
        env = {"HOME": os.path.join(self.td, "home"), "AIHUB_HOME": os.path.join(self.td, "aihub")}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        self.A = {"url": self.ua, "branch": "main", "subdir": ""}
        self.B = {"url": self.ub, "branch": "dev", "subdir": "p"}

    def proj(self, ver, name="p"):
        d = os.path.join(self.td, "proj-%s-%s" % (name, ver))
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "aihub.toml"), "w").write(toml(ver, name))
        return d


class IndexFiles(World):
    def add(self, out, ver, loc, ref):
        pkgindex.add(out, self.proj(ver), loc["url"], loc["branch"], loc["subdir"], ref)

    def test_old_version_keeps_the_repo_it_was_published_from(self):
        out = os.path.join(self.td, "idx")
        self.add(out, "1.0.0", self.A, self.ca)
        self.add(out, "2.0.0", self.B, self.cb)
        e = pkgindex.Reader(out).entry("p")
        self.assertEqual(e["repo"], self.B)
        v1, v2 = e["versions"]
        self.assertEqual(v1["repo"], self.A)
        self.assertEqual(v1["repo"]["subdir"], "")                       # kept (not dropped as "empty"): an absent subdir would read as the entry's "p"
        self.assertNotIn("repo", v2)
        self.assertEqual(v1["ref"], self.ca)
        for v, want in ((v1, self.A), (v2, self.B)):                     # what an OLD cli computes: dict(entry repo, **version repo)
            got = dict(e["repo"], **(v.get("repo") or {}))
            self.assertEqual({k: got.get(k, "") for k in ("url", "branch", "subdir")}, want)

    def test_re_adding_an_old_version_does_not_move_the_entry(self):
        out = os.path.join(self.td, "idx")
        self.add(out, "1.0.0", self.A, self.ca)
        self.add(out, "2.0.0", self.B, self.cb)
        self.add(out, "1.0.0", self.A, self.ca)                          # force re-index of v1
        e = pkgindex.Reader(out).entry("p")
        self.assertEqual((e["latest_version"], e["repo"]), ("2.0.0", self.B))
        self.assertEqual(e["versions"][0]["repo"], self.A)

    def test_url_spelling_is_not_a_move(self):
        out = os.path.join(self.td, "idx")
        self.add(out, "1.0.0", {"url": "https://Example.com/o/r", "branch": "main", "subdir": ""}, "a")
        self.add(out, "1.1.0", {"url": "https://example.com/o/r.git/", "branch": "main", "subdir": ""}, "b")
        e = pkgindex.Reader(out).entry("p")
        self.assertTrue(all("repo" not in v for v in e["versions"]), e)

    def test_package_that_never_moved_is_byte_for_byte_what_it_was(self):
        out = os.path.join(self.td, "idx")
        self.add(out, "1.0.0", self.A, self.ca)
        self.add(out, "1.1.0", self.A, "r2")
        path = os.path.join(out, pkgindex.entry_rel("p"))
        new = open(path).read()
        old_shape = {"name": "p", "type": "skill", "description": "d", "tags": [], "latest_version": "1.1.0", "repo": dict(self.A),
                     "versions": [{"version": "1.0.0", "ref": self.ca, "requires": {}}, {"version": "1.1.0", "ref": "r2", "requires": {}}]}
        self.assertEqual(new, pkgindex.dumps(pkgindex.normalize_entry(old_shape)))
        self.assertNotIn('"repo"', json.dumps(json.loads(new)["versions"]))

    def test_set_version_repos_repairs_and_prunes(self):
        out = os.path.join(self.td, "idx")
        self.add(out, "1.0.0", self.B, self.ca)                          # a v1 wrongly filed under B (the old behaviour)
        self.add(out, "2.0.0", self.B, self.cb)
        self.assertNotIn("repo", pkgindex.Reader(out).entry("p")["versions"][0])
        self.assertEqual(pkgindex.set_version_repos(out, {("p", "1.0.0"): self.A}), 1)
        self.assertEqual(pkgindex.Reader(out).entry("p")["versions"][0]["repo"], self.A)
        self.assertEqual(pkgindex.set_version_repos(out, {("p", "1.0.0"): self.A}), 0)
        pkgindex.set_version_repos(out, {("p", "1.0.0"): self.B})        # back to where the entry says: nothing stored
        self.assertNotIn("repo", pkgindex.Reader(out).entry("p")["versions"][0])


class Hub(World):
    """A real hub (TestClient). p 1.0.0 is published from A, then 2.0.0 from B, through publish events like the CLI sends."""

    def setUp(self):
        super().setUp()
        s = mock.patch.object(syncmod, "SCHEMES", syncmod.SCHEMES + ("file://",))
        s.start()
        self.addCleanup(s.stop)
        self.c = TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=os.path.join(self.td, "hubdata"))))
        self.addCleanup(self.c.close)
        self.sy, self.ing, self.repos = self.c.app.state.sync, self.c.app.state.ingest, self.c.app.state.repos
        self.addCleanup(self.ing.close)
        self.c.post("/api/v1/auth/register", json={"username": "root", "password": "secret1"})
        self.h = {"Authorization": "Bearer " + self.c.post("/api/v1/auth/login", json={"username": "root", "password": "secret1"}).json()["token"]}

    def event(self, ver, loc, commit):
        return {"id": uuid.uuid4().hex, "kind": "publish", "user": "", "package": "p", "version": ver, "component": "", "client_id": "c1",
                "ts": time.time(), "git_url": loc["url"], "git_branch": loc["branch"], "git_subdir": loc["subdir"], "commit": commit,
                "type": "skill", "description": "d", "bundle": {"toml": b64(toml(ver))}}

    def publish(self, *events):
        r = self.c.post("/api/v1/telemetry", json={"events": list(events)})
        self.assertLess(r.status_code, 300, r.text)
        self.ing.flush()
        self.sy.run_once(scheduled=True)

    def publish_both(self):
        self.publish(self.event("1.0.0", self.A, self.ca))
        self.publish(self.event("2.0.0", self.B, self.cb))

    def entry(self):
        r = self.c.get("/api/v1/index/packages/p", headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def transfer(self, ver):
        return self.c.get("/api/v1/index/packages/p/transfer/%s" % ver, headers=self.h)

    def tar_toml(self, resp):
        p = os.path.join(self.td, "got-%s.tgz" % uuid.uuid4().hex[:6])
        open(p, "wb").write(resp.content)
        with tarfile.open(p) as t:
            return t.extractfile("aihub.toml").read().decode()

    def corrupt(self):
        """The database the way the old code left it: every version points at the LATEST location."""
        c = self.repos.db.conn()
        for v, m in [(r[0], json.loads(r[1])) for r in c.execute("SELECT version,manifest FROM versions").fetchall()]:
            m["repo"] = dict(self.B, ref=m["repo"].get("ref"))
            c.execute("UPDATE versions SET manifest=? WHERE version=?", (json.dumps(m), v))
        c.commit()
        self.c.app.state.cache.invalidate("pkg")
        self.sy.invalidate_served()

    def v1_repo(self):
        return next(v for v in self.entry()["versions"] if v["version"] == "1.0.0").get("repo")


class IndexAndInstall(Hub):
    def test_index_view_gives_the_moved_version_its_own_repo(self):
        self.publish_both()
        e = self.entry()
        self.assertEqual(e["repo"], self.B)
        v1, v2 = sorted(e["versions"], key=lambda v: v["version"])
        self.assertEqual(v1["repo"], self.A)
        self.assertEqual(v1["ref"], self.ca)
        self.assertNotIn("repo", v2)

    def test_direct_install_of_the_old_version(self):
        self.publish_both()
        e = self.entry()
        r = {"name": "p", "version": "1.0.0", "repo": registry._repo_of(e, "1.0.0"), "alts": registry._alts_of(e, "1.0.0")}
        self.assertEqual(r["repo"]["url"], self.ua)
        dest = os.path.join(self.td, "dest1")
        registry._checkout_git(r, dest)
        self.assertIn('version = "1.0.0"', open(os.path.join(dest, "aihub.toml")).read())
        r2 = {"name": "p", "version": "2.0.0", "repo": registry._repo_of(e, "2.0.0"), "alts": registry._alts_of(e, "2.0.0")}
        registry._checkout_git(r2, os.path.join(self.td, "dest2"))
        self.assertIn('version = "2.0.0"', open(os.path.join(self.td, "dest2", "aihub.toml")).read())

    def test_indirect_install_of_the_old_version(self):
        self.publish_both()
        r = self.transfer("1.0.0")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers["X-Aihub-Rev"], self.ca)
        self.assertIn('version = "1.0.0"', self.tar_toml(r))
        r = self.transfer("2.0.0")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn('version = "2.0.0"', self.tar_toml(r))

    def test_tar_cache_does_not_mix_locations(self):
        self.publish_both()
        self.assertEqual(self.transfer("1.0.0").status_code, 200)
        self.assertEqual(self.transfer("2.0.0").status_code, 200)
        t = self.c.app.state.transfer
        self.assertNotEqual(t._tar_path(self.ua, "", "x")[1], t._tar_path(self.ub, "p", "x")[1])
        self.assertNotEqual(t._tar_path(self.ub, "", "x")[1], t._tar_path(self.ub, "p", "x")[1])
        self.assertIn('version = "1.0.0"', self.tar_toml(self.transfer("1.0.0")))     # still the right content after the other was cached

    def test_package_that_never_moved_has_no_per_version_repo(self):
        self.publish(self.event("1.0.0", self.A, self.ca))
        self.publish(self.event("1.1.0", self.A, self.ca))
        e = self.entry()
        self.assertEqual(e["repo"], {"url": self.ua, "branch": "main"})          # empty subdir is not stored, as always
        self.assertTrue(all("repo" not in v for v in e["versions"]), e)

    def test_publish_event_keeps_its_location(self):
        self.publish_both()
        locs = {x["version"]: x for x in self.repos.publish_locations("p")}
        self.assertEqual((locs["1.0.0"]["git_url"], locs["1.0.0"]["git_subdir"], locs["1.0.0"]["commit"]), (self.ua, "", self.ca))
        self.assertEqual((locs["2.0.0"]["git_branch"], locs["2.0.0"]["git_subdir"]), ("dev", "p"))


class Repair(Hub):
    def test_backfill_repairs_a_database_written_by_the_old_code(self):
        self.publish_both()
        self.corrupt()
        self.assertIsNone(self.v1_repo())                                 # the bug: it points at B, like the entry
        self.assertEqual(self.sy.repair_version_repos(), 1)
        self.assertEqual(self.v1_repo(), self.A)
        self.assertEqual(self.transfer("1.0.0").status_code, 200)
        self.assertEqual(self.sy.repair_version_repos(), 0)              # idempotent
        self.assertEqual(self.v1_repo(), self.A)

    def test_backfill_leaves_a_version_alone_when_the_event_is_another_commit(self):
        self.publish_both()
        self.corrupt()
        self.repos.db.conn().execute("UPDATE events SET detail=replace(detail, ?, ?) WHERE version='1.0.0'", (self.ca, "f" * 40))
        self.repos.db.conn().commit()
        self.assertEqual(self.repos.version_repos_backfill(), [])

    def test_backfill_uses_extra_events_from_the_archive(self):
        self.publish_both()
        self.corrupt()
        self.repos.db.conn().execute("DELETE FROM events WHERE kind='publish'")
        self.repos.db.conn().commit()
        extra = [{"package": "p", "version": "1.0.0", "git_url": self.ua, "git_branch": "main", "git_subdir": "", "commit": self.ca}]
        self.assertEqual(self.sy.repair_version_repos(extra), 1)
        self.assertEqual(self.v1_repo(), self.A)

    def test_repair_also_fixes_the_index_folder_so_a_reimport_keeps_it(self):
        self.publish_both()
        self.corrupt()
        d = self.sy.index_dir
        entry_file = os.path.join(d, "index", pkgindex.entry_rel("p")) if os.path.isdir(os.path.join(d, "index")) else None
        self.assertTrue(entry_file and os.path.isfile(entry_file), d)
        e = json.load(open(entry_file))
        e["versions"][0].pop("repo", None)                                # the folder as the old code left it
        open(entry_file, "w").write(pkgindex.dumps(e))
        self.assertEqual(self.sy.repair_version_repos(), 1)
        self.assertEqual(json.load(open(entry_file))["versions"][0]["repo"], self.A)
        self.sy._import_local()                                           # the entry is read again whenever the package changes
        self.assertEqual(self.v1_repo(), self.A)

    def test_start_runs_the_backfill_once(self):
        self.publish_both()
        self.corrupt()
        self.repos.set_setting("version_repo_backfill", "")
        self.sy.start()
        self.addCleanup(self.sy.close)
        self.assertEqual(self.v1_repo(), self.A)
        self.assertEqual(self.repos.setting("version_repo_backfill"), "1")


class SafetyNet(Hub):
    def test_transfer_finds_the_old_commit_in_another_known_place_and_remembers_it(self):
        self.publish_both()
        self.corrupt()                                                    # nothing in the manifests points at A; only the publish event does
        self.assertEqual([x["url"] for x in self.repos.known_locations("p")], [self.ub, self.ua])     # B twice collapses to one, A from the event
        r = self.transfer("1.0.0")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers["X-Aihub-Rev"], self.ca)
        self.assertEqual(self.v1_repo(), self.A)                          # remembered: next time it is direct, and the CLIs see it
        self.assertEqual(self.transfer("1.0.0").status_code, 200)

    def test_nothing_known_fails_with_the_specific_error_and_the_hint(self):
        self.publish_both()
        self.corrupt()
        c = self.repos.db.conn()
        c.execute("DELETE FROM events WHERE kind='publish'")
        c.commit()
        r = self.transfer("1.0.0")
        self.assertEqual(r.status_code, 502)
        d = r.json()["detail"]
        self.assertIn("cannot find '%s'" % self.ca, d)
        self.assertTrue(d.splitlines()[-1].startswith(gitx.HINT), d)

    def test_other_places_tried_are_named(self):
        self.publish_both()
        self.corrupt()
        c = self.repos.db.conn()
        c.execute("DELETE FROM events WHERE kind='publish'")
        c.commit()
        self.repos.version_set_repo("p", "2.0.0", {"url": self.uc, "branch": "main", "subdir": ""})     # another place that does not have it either
        d = self.transfer("1.0.0").json()["detail"]
        self.assertIn("also tried: " + self.uc, d)
        self.assertTrue(d.splitlines()[-1].startswith(gitx.HINT), d)


class OldShapeAndCli(World):
    def entry(self, **over):
        e = {"name": "p", "type": "skill", "description": "d", "latest_version": "2.0.0", "repo": dict(self.B),
             "versions": [{"version": "1.0.0", "ref": self.ca}, {"version": "2.0.0", "ref": self.cb}]}
        e.update(over)
        return e

    def r(self, e, ver):
        return {"name": "p", "version": ver, "repo": registry._repo_of(e, ver), "alts": registry._alts_of(e, ver)}

    def test_old_shape_entry_still_installs_the_latest(self):
        e = self.entry()
        out = os.path.join(self.td, "idx")
        pkgindex.write_entry(out, e)
        pkgindex.rebuild(out)
        saved = json.load(open(os.path.join(out, pkgindex.entry_rel("p"))))
        self.assertTrue(all("repo" not in v for v in saved["versions"]))
        r = self.r(saved, "2.0.0")
        self.assertEqual(r["alts"], [])
        dest = os.path.join(self.td, "d")
        registry._checkout_git(r, dest)
        self.assertIn('version = "2.0.0"', open(os.path.join(dest, "aihub.toml")).read())

    def test_cli_looks_in_the_other_places_the_entry_lists(self):
        e = self.entry(versions=[{"version": "1.0.0", "ref": self.ca}, {"version": "1.5.0", "ref": "0" * 40, "repo": self.A},
                                 {"version": "2.0.0", "ref": self.cb}])
        r = self.r(e, "1.0.0")                                           # an index that has no per-version repo for 1.0.0, but lists A for 1.5.0
        self.assertEqual([a["url"] for a in r["alts"]], [self.ua])
        dest = os.path.join(self.td, "d")
        registry._checkout_git(r, dest)
        self.assertIn('version = "1.0.0"', open(os.path.join(dest, "aihub.toml")).read())
        self.assertEqual(r["repo"]["url"], self.ua)                       # recorded where it was found

    def test_prefetch_group_falls_back_per_package(self):
        e = self.entry(versions=[{"version": "1.0.0", "ref": self.ca}, {"version": "1.5.0", "ref": "0" * 40, "repo": self.A}])
        r = self.r(e, "1.0.0")
        out = registry.prefetch([r], os.path.join(self.td, "pf"))
        self.assertIsNone(out["p"][1], out["p"][1])
        self.assertIn('version = "1.0.0"', open(os.path.join(out["p"][0], "aihub.toml")).read())

    def test_cli_error_names_the_places_tried_and_keeps_the_hint_last(self):
        e = self.entry(versions=[{"version": "1.0.0", "ref": self.ca}, {"version": "1.5.0", "ref": "0" * 40,
                                                                          "repo": {"url": self.uc, "branch": "main", "subdir": ""}}])
        with self.assertRaises(Exception) as cm:
            registry._checkout_git(self.r(e, "1.0.0"), os.path.join(self.td, "d"))
        msg = str(cm.exception)
        self.assertIn("cannot find '%s'" % self.ca, msg)
        self.assertIn("also tried: " + self.uc, msg)
        self.assertTrue(msg.splitlines()[-1].startswith(gitx.HINT), msg)

    def test_error_without_alternatives_is_the_plain_specific_one(self):
        with self.assertRaises(Exception) as cm:
            registry._checkout_git(self.r(self.entry(), "1.0.0"), os.path.join(self.td, "d"))
        msg = str(cm.exception)
        self.assertIn("cannot find", msg)
        self.assertNotIn("also tried", msg)
        self.assertTrue(msg.splitlines()[-1].startswith(gitx.HINT), msg)


if __name__ == "__main__":
    unittest.main()
