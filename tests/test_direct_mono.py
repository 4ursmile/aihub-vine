"""`aihub dev publish -i` on a workspace / monorepo, and the reproducible archives that make a re-run safe."""
import gzip
import hashlib
import io
import os
import subprocess
import tarfile
import tempfile
import time
import unittest
from unittest import mock

from aihub.cli import api, main as cli_main
from aihub.core import archive, ignore
from tests.test_direct_publish import API, hub, login, make_tar, push


def git(cwd, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t"] + list(a), cwd=cwd, check=True, capture_output=True)


def package(root, name, version="1.0.0", requires=None):
    d = os.path.join(root, name)
    os.makedirs(os.path.join(d, "skills", name))
    toml = '[package]\nname="%s"\nversion="%s"\ntype="skill"\ndescription="mono %s"\n\n[[skills]]\nname="%s"\npath="skills/%s"\n' % (name, version, name, name, name)
    if requires:
        toml += '\n[requires]\npackages = ["%s"]\n' % requires
    for rel, body in (("aihub.toml", toml), ("README.md", "# %s\n" % name), ("skills/%s/SKILL.md" % name, "---\nname: %s\ndescription: x\n---\nhi\n" % name)):
        with open(os.path.join(d, rel), "w") as f:
            f.write(body)
    return d


class Reproducible(unittest.TestCase):
    def test_same_folder_packed_twice_is_byte_identical_even_when_files_are_touched(self):
        d = tempfile.mkdtemp()
        package(d, "p")
        root = os.path.join(d, "p")
        files = ignore.list_files(root)
        a = archive.build(root, files, os.path.join(d, "a.tgz"))
        for f in files:
            os.utime(os.path.join(root, f), (time.time() + 500, time.time() + 500))
        b = archive.build(root, files[::-1], os.path.join(d, "b.tgz"))
        self.assertEqual(open(a, "rb").read(), open(b, "rb").read())

    def test_content_digest_ignores_packing_but_not_content_or_exec_bit(self):
        def tgz(entries, level=1, mtime=0):
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=level, mtime=mtime) as g, tarfile.open(fileobj=g, mode="w") as t:
                for n, body, mode in entries:
                    ti = tarfile.TarInfo(n)
                    ti.size, ti.mode, ti.mtime = len(body), mode, mtime
                    t.addfile(ti, io.BytesIO(body))
            p = tempfile.mktemp()
            open(p, "wb").write(buf.getvalue())
            return p
        base = [("a.txt", b"one", 0o644), ("b.sh", b"#!/bin/sh", 0o755)]
        d = archive.content_digest
        self.assertEqual(d(tgz(base)), d(tgz(base[::-1], level=9, mtime=99999)))
        self.assertNotEqual(d(tgz(base)), d(tgz([("a.txt", b"two", 0o644), base[1]])))
        self.assertNotEqual(d(tgz(base)), d(tgz([base[0], ("b.sh", b"#!/bin/sh", 0o644)])))
        self.assertNotEqual(d(tgz(base)), d(tgz(base + [("c.txt", b"", 0o644)])))


class HubRecognisesTheSameRelease(unittest.TestCase):
    def setUp(self):
        self.c = hub()
        self.h = login(self.c, "root")

    def repack(self, raw, extra=None):
        """The same files in a differently packed archive (other timestamps and compression)."""
        out = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(raw)) as src, gzip.GzipFile(fileobj=out, mode="wb", compresslevel=9, mtime=12345) as g, \
                tarfile.open(fileobj=g, mode="w") as dst:
            for m in src.getmembers():
                m.mtime = 99999
                f = src.extractfile(m) if m.isfile() else None
                dst.addfile(m, f)
            for n, body in (extra or {}).items():
                ti = tarfile.TarInfo(n)
                ti.size = len(body)
                dst.addfile(ti, io.BytesIO(body))
        return out.getvalue()

    def test_repacked_same_files_are_already_published_but_changed_files_are_refused(self):
        raw = make_tar()
        first = push(self.c, self.h, raw)
        self.assertEqual(first.json()["status"], "published")
        other = self.repack(raw)
        self.assertNotEqual(other, raw)
        again = push(self.c, self.h, other)
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["status"], "already published")
        self.assertEqual(again.json()["sha256"], first.json()["sha256"])                  # the stored archive stays the one of record
        with open(self.c.app.state.direct.path("dp-demo", "1.0.0"), "rb") as f:
            self.assertEqual(f.read(), raw)
        changed = push(self.c, self.h, self.repack(raw, {"extra.txt": b"new"}))
        self.assertEqual(changed.status_code, 409, changed.text)


class Bump(unittest.TestCase):
    def test_any_spacing_and_quotes_only_the_package_table_and_never_silently_nothing(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "aihub.toml")
        for body, want in (('[package]\nname="x"\nversion="1.2.3"\n[[skills]]\nname="x"\n', "1.2.4"),
                           ("[package]\nname = 'x'\nversion = '0.1.9'  # c\n", "0.1.10"),
                           ('# top\n[package]\nname="x"\nversion   =   "2.0.0"\n[requires]\nversion = "9"\n', "2.0.1")):
            open(p, "w").write(body)
            old, new = cli_main._bump_version(p, "patch")
            self.assertEqual(new, want)
            self.assertIn(want, open(p).read())
        self.assertIn('version = "9"', open(p).read())                                  # another table's `version` is left alone
        open(p, "w").write('[package]\nname="x"\n')
        with self.assertRaises(ValueError):
            cli_main._bump_version(p, "patch")


class Workspace(unittest.TestCase):
    """A git monorepo with three packages, a root .gitignore and one package that has its own nested .git."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        git(self.root, "init", "-q", "-b", "main")
        with open(os.path.join(self.root, ".gitignore"), "w") as f:
            f.write("*.log\nsecret.env\n")
        package(self.root, "mono-a")
        package(self.root, "mono-b", requires="mono-a>=1.0")
        package(self.root, "mono-c")
        a = os.path.join(self.root, "mono-a")
        for n, body in (("debug.log", "noise"), ("secret.env", "K=1"), ("notes.txt", "keep")):
            open(os.path.join(a, n), "w").write(body)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "init")
        c = os.path.join(self.root, "mono-c")
        git(c, "init", "-q", "-b", "main")
        git(c, "add", "-A")
        git(c, "commit", "-qm", "c")
        self.sent = {}

    def run_publish(self, *argv, fail=()):
        def fake(path, progress=None):
            with tarfile.open(path) as t:
                names = sorted(t.getnames())
                toml = t.extractfile("aihub.toml").read().decode()
            name = toml.split('name="')[1].split('"')[0]
            if name in fail:
                raise api.ApiError("409 /index/direct: %s is already published (with different content)" % name)
            self.sent[name] = names
            return {"status": "published", "sha256": "ab" * 32}
        a = cli_main.build_parser().parse_args(["dev", "publish", "-i", self.root] + list(argv))
        with mock.patch.object(api, "publish_direct", fake), mock.patch.object(cli_main.gitx, "run", side_effect=AssertionError("git was called")):
            return a.fn(a)

    def test_every_package_is_sent_on_its_own_with_only_its_files(self):
        self.run_publish()
        self.assertEqual(sorted(self.sent), ["mono-a", "mono-b", "mono-c"])
        self.assertEqual(self.sent["mono-a"], ["README.md", "aihub.toml", "notes.txt", "skills/mono-a/SKILL.md"])      # root .gitignore applied
        self.assertEqual(self.sent["mono-c"], ["README.md", "aihub.toml", "skills/mono-c/SKILL.md"])                   # nested .git left out
        for names in self.sent.values():
            self.assertFalse([n for n in names if n.startswith("..") or "mono-" in n.split("/")[0]])

    def test_only_and_bump_touch_just_the_selected_package(self):
        self.run_publish("--only", "mono-b", "--bump", "minor")
        self.assertEqual(list(self.sent), ["mono-b"])
        read = lambda n: open(os.path.join(self.root, n, "aihub.toml")).read()
        self.assertIn('version="1.1.0"', read("mono-b"))
        self.assertIn('version="1.0.0"', read("mono-a"))

    def test_one_refused_package_does_not_stop_the_others_and_the_command_fails(self):
        with self.assertRaises(ValueError) as e:
            self.run_publish(fail=("mono-a",))
        self.assertIn("mono-a", str(e.exception))
        self.assertIn("the other 2 went out", str(e.exception))
        self.assertEqual(sorted(self.sent), ["mono-b", "mono-c"])

    def test_works_without_git_too(self):
        import shutil
        shutil.rmtree(os.path.join(self.root, ".git"))
        shutil.rmtree(os.path.join(self.root, "mono-c", ".git"))
        self.run_publish()
        self.assertEqual(sorted(self.sent), ["mono-a", "mono-b", "mono-c"])
        # no git to apply the workspace .gitignore: the CLI does it, so secrets next to a package never ship
        self.assertEqual(self.sent["mono-a"], ["README.md", "aihub.toml", "notes.txt", "skills/mono-a/SKILL.md"])

    def test_workspace_level_aihubignore_applies_to_every_package_with_git(self):
        with open(os.path.join(self.root, ".aihubignore"), "w") as f:
            f.write("notes.txt\nprivate/\n")
        os.makedirs(os.path.join(self.root, "mono-b", "private"))
        open(os.path.join(self.root, "mono-b", "private", "x.txt"), "w").write("x")
        git(self.root, "add", "-A")
        self.run_publish()
        self.assertNotIn("notes.txt", self.sent["mono-a"])
        self.assertFalse([n for n in self.sent["mono-b"] if n.startswith("private")])
        self.assertIn("skills/mono-b/SKILL.md", self.sent["mono-b"])

    def test_a_package_given_directly_ignores_folders_above_it(self):
        one = os.path.join(self.root, "mono-a")
        a = cli_main.build_parser().parse_args(["dev", "publish", "-i", one])
        self.assertEqual(ignore.list_files(one, upto=one), ignore.list_files(one))


if __name__ == "__main__":
    unittest.main()
