"""`aihub dev publish -i`: a package goes straight to the hub's data folder (no git), is listed, and installs download it from the hub."""
import io
import os
import tarfile
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from aihub.cli import api, main as cli_main, registry
from aihub.server.config import Settings
from aihub.server.direct import URL
from aihub.server.main import create_app

API = "/api/v1"


def make_tar(name="dp-demo", version="1.0.0", extra=None, nest=None):
    toml = ('[package]\nname="%s"\nversion="%s"\ntype="skill"\ndescription="direct demo"\n\n[[skills]]\nname="%s"\npath="skills/%s"\n'
            % (name, version, name, name))
    files = {"aihub.toml": toml, "README.md": "# %s\nhello\n" % name, "skills/%s/SKILL.md" % name: "---\nname: %s\ndescription: x\n---\nhi\n" % name}
    files.update(extra or {})
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for n, body in files.items():
            raw = body.encode()
            ti = tarfile.TarInfo((nest + "/" if nest else "") + n)
            ti.size = len(raw)
            t.addfile(ti, io.BytesIO(raw))
    return buf.getvalue()


def hub():
    return TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=tempfile.mkdtemp())))


def login(c, name, pw="secret1"):
    c.post(API + "/auth/register", json={"username": name, "password": pw})
    t = c.post(API + "/auth/login", json={"username": name, "password": pw}).json()["token"]
    return {"Authorization": "Bearer " + t}


def push(c, h, raw):
    return c.post(API + "/index/direct", content=raw, headers=dict(h or {}, **{"Content-Type": "application/octet-stream"}))


class DirectPublish(unittest.TestCase):
    def setUp(self):
        self.c = hub()
        self.root = login(self.c, "root")                       # first account = admin
        self.alice = login(self.c, "alice")                     # ordinary user (role `user` may publish)
        self.bob = login(self.c, "bob")

    def test_publish_lists_the_package_and_the_hub_sends_the_same_bytes_without_git(self):
        raw = make_tar()
        r = push(self.c, self.alice, raw)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "published")
        entry = self.c.get(API + "/index/packages/dp-demo", headers=self.bob).json()
        self.assertEqual(entry["repo"]["url"], URL)
        self.assertEqual(entry["latest_version"], "1.0.0")
        self.assertIn("hello", entry.get("readme", ""))
        t = self.c.get(API + "/index/packages/dp-demo/transfer/1.0.0", headers=self.bob)
        self.assertEqual(t.status_code, 200, t.text)
        self.assertEqual(t.content, raw)
        self.assertEqual(t.headers["X-Aihub-Cache"], "direct")
        # shows in the web package list / page too, and the publisher owns it
        page = self.c.get(API + "/packages/dp-demo", headers=self.alice).json()
        self.assertEqual(page["latest_version"], "1.0.0")
        self.assertEqual([m["username"] for m in page["maintainers"]], ["alice"])

    def test_transfer_needs_no_install_indirect_permission_for_a_direct_package(self):
        push(self.c, self.alice, make_tar())
        for perm in ("install_indirect",):                      # take it away from the `user` role: git packages lose `-i`, direct ones do not
            self.c.put(API + "/admin/roles/user", json={"permissions": ["publish", "review"]}, headers=self.root)
        t = self.c.get(API + "/index/packages/dp-demo/transfer/1.0.0", headers=self.bob)
        self.assertEqual(t.status_code, 200, t.text)

    def test_same_bytes_again_is_fine_other_bytes_under_the_same_version_are_refused(self):
        raw = make_tar()
        self.assertEqual(push(self.c, self.alice, raw).status_code, 200)
        again = push(self.c, self.alice, raw)
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["status"], "already published")
        r = push(self.c, self.alice, make_tar(extra={"notes.txt": "changed"}))
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("bump", r.text)

    def test_next_version_becomes_latest_and_both_download(self):
        a, b = make_tar(version="1.0.0"), make_tar(version="1.1.0")
        push(self.c, self.alice, a)
        self.assertEqual(push(self.c, self.alice, b).status_code, 200)
        entry = self.c.get(API + "/index/packages/dp-demo", headers=self.bob).json()
        self.assertEqual(entry["latest_version"], "1.1.0")
        self.assertEqual(self.c.get(API + "/index/packages/dp-demo/transfer/1.0.0", headers=self.bob).content, a)
        self.assertEqual(self.c.get(API + "/index/packages/dp-demo/transfer/1.1.0", headers=self.bob).content, b)

    def test_only_those_who_manage_a_package_publish_a_new_version(self):
        push(self.c, self.alice, make_tar())
        r = push(self.c, self.bob, make_tar(version="2.0.0"))
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(push(self.c, self.root, make_tar(version="2.0.0")).status_code, 200)        # an admin may

    def test_listed_even_when_the_ingest_flusher_is_busy_with_the_event(self):
        """The background flusher may already hold the publish event (mid database write): the answer must not depend on it."""
        with mock.patch.object(type(self.c.app.state.ingest), "flush", lambda s: 0):
            r = push(self.c, self.alice, make_tar())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "published")
        self.assertEqual(self.c.get(API + "/index/packages/dp-demo", headers=self.bob).status_code, 200)
        self.assertEqual(self.c.get(API + "/index/packages/dp-demo/transfer/1.0.0", headers=self.bob).status_code, 200)

    def test_needs_an_account(self):
        self.assertEqual(push(self.c, None, make_tar()).status_code, 401)

    def test_bad_uploads_are_refused_and_nothing_is_stored(self):
        for raw, why in ((b"not a tar", "garbage"), (make_tar(nest="top"), "inside a folder"),
                         (make_tar(extra={"../evil.txt": "x"}), "path escape")):
            r = push(self.c, self.alice, raw)
            self.assertEqual(r.status_code, 400, "%s: %s" % (why, r.text))
        self.assertEqual(self.c.get(API + "/index/packages/dp-demo", headers=self.bob).status_code, 404)

    def test_size_limit_is_an_admin_number(self):
        with mock.patch.object(type(self.c.app.state.sync), "num", lambda s, k, *a, **kw: 1 if k == "direct_max_mb" else 5):
            r = push(self.c, self.alice, make_tar(extra={"big.bin": os.urandom(1_200_000).hex()}))
        self.assertEqual(r.status_code, 413, r.text)

    def test_unpublish_removes_the_stored_archive(self):
        push(self.c, self.alice, make_tar())
        d = self.c.app.state.direct
        self.assertTrue(os.path.isfile(d.path("dp-demo", "1.0.0")))
        self.assertEqual(self.c.delete(API + "/packages/dp-demo", headers=self.alice).status_code, 200)
        self.assertFalse(os.path.isfile(d.path("dp-demo", "1.0.0")))

    def test_forged_publish_event_cannot_point_the_index_at_a_missing_archive(self):
        ev = {"id": "forged-1", "kind": "publish", "package": "ghost", "version": "1.0.0", "git_url": URL, "git_branch": "main",
              "commit": "0" * 64, "type": "skill", "description": "x", "client_id": "c",
              "bundle": {"toml": __import__("base64").b64encode(b'[package]\nname="ghost"\nversion="1.0.0"\ntype="skill"\ndescription="x"\n').decode()}}
        self.c.post(API + "/telemetry", json={"events": [ev]}, headers=self.bob)
        self.c.app.state.ingest.flush()
        try:
            self.c.app.state.sync.stage_now()
        except Exception:
            pass
        self.assertEqual(self.c.get(API + "/index/packages/ghost", headers=self.bob).status_code, 404)


class CliSide(unittest.TestCase):
    def test_a_hub_package_is_downloaded_even_without_dash_i(self):
        r = {"name": "dp-demo", "version": "1.0.0", "repo": {"url": URL, "branch": "main", "ref": "ab" * 32}}
        self.assertTrue(registry.on_hub(r["repo"]))
        self.assertFalse(registry.on_hub({"url": "https://git.example.com/x.git"}))
        with mock.patch.object(registry, "_from_hub", return_value="sha") as hubget, mock.patch.object(registry, "_checkout_git") as git:
            self.assertEqual(registry.checkout(r, "/tmp/x"), "sha")
        hubget.assert_called_once()
        git.assert_not_called()

    def test_prefetch_never_clones_a_hub_package_but_still_shares_git_clones(self):
        pkgs = [{"name": "h1", "version": "1", "repo": {"url": URL, "branch": "main"}},
                {"name": "h2", "version": "1", "repo": {"url": URL, "branch": "main"}},
                {"name": "g1", "version": "1", "repo": {"url": "https://git.example.com/m.git", "branch": "main", "subdir": "g1"}}]
        seen = []
        with mock.patch.object(registry, "checkout", side_effect=lambda r, d: seen.append(("hub", r["name"])) or "rev"), \
                mock.patch.object(registry.gitx, "current", return_value="/nonexistent"), \
                mock.patch.object(registry, "_get_repo", side_effect=lambda r, repo, src: "/nonexistent"), \
                mock.patch.object(registry, "_checkout_git", side_effect=lambda r, d, src=None: seen.append(("git", r["name"])) or "rev"), \
                mock.patch.object(registry.gitx, "fetch", return_value="/nonexistent"), \
                mock.patch.object(registry.gitx, "lfs_pull"):
            out = registry.prefetch(pkgs, tempfile.mkdtemp())
        self.assertEqual({n for k, n in seen if k == "hub"}, {"h1", "h2"})
        self.assertEqual([n for k, n in seen if k == "git"], ["g1"])
        self.assertTrue(all(v[1] is None for v in out.values()), out)

    def test_dev_publish_dash_i_packs_and_uploads_without_touching_git(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "aihub.toml"), "w") as f:
            f.write('[package]\nname="dp-cli"\nversion="1.0.0"\ntype="skill"\ndescription="cli demo"\n\n[[skills]]\nname="dp-cli"\npath="skills/dp-cli"\n')
        os.makedirs(os.path.join(d, "skills", "dp-cli"))
        with open(os.path.join(d, "skills", "dp-cli", "SKILL.md"), "w") as f:
            f.write("---\nname: dp-cli\ndescription: x\n---\nhi\n")
        sent = []

        def fake(path, progress=None):
            with tarfile.open(path) as t:
                sent.append(sorted(t.getnames()))
            return {"status": "published", "sha256": "ab" * 32}

        a = cli_main.build_parser().parse_args(["dev", "publish", "-i", d])
        with mock.patch.object(api, "publish_direct", fake), mock.patch.object(cli_main.gitx, "run", side_effect=AssertionError("git was called")):
            a.fn(a)
        self.assertEqual(len(sent), 1)
        self.assertIn("aihub.toml", sent[0])
        self.assertIn("skills/dp-cli/SKILL.md", sent[0])
        self.assertFalse(os.path.exists(os.path.join(d, ".git")))


if __name__ == "__main__":
    unittest.main()
