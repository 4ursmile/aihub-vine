"""aihub-ui: read-only git inspection (Git tab), the changed-files list of the Diff tab, and file diffs of deleted / binary files."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "aihub", "builtin_packages", "aihub-ui", "ui")
sys.path.insert(0, UI)
from aihubui import gitinspect as G, store  # noqa: E402

HAVE_GIT = shutil.which("git") is not None


def sh(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="Ann", GIT_AUTHOR_EMAIL="ann@x", GIT_COMMITTER_NAME="Ann", GIT_COMMITTER_EMAIL="ann@x"))


def write(p, text):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.old = os.environ.get("AIHUB_HOME")
        os.environ["AIHUB_HOME"] = os.path.join(self.tmp, "home")
        self.addCleanup(lambda: os.environ.pop("AIHUB_HOME", None) if self.old is None else os.environ.__setitem__("AIHUB_HOME", self.old))


@unittest.skipUnless(HAVE_GIT, "git not installed")
class GitInspect(Base):
    """The working folder is a SUB folder of the repository: everything is limited to it and relative to it."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        self.d = os.path.join(self.repo, "app")
        os.makedirs(self.d)
        sh(self.repo, "init", "-q", "-b", "main")
        write(os.path.join(self.d, "x.py"), "a\nb\nc\n")
        write(os.path.join(self.d, "old.txt"), "bye\n")
        write(os.path.join(self.repo, "outside.txt"), "not mine\n")
        sh(self.repo, "add", "-A")
        sh(self.repo, "commit", "-qm", "first")
        write(os.path.join(self.d, "x.py"), "a\nB\nc\nd\n")
        sh(self.repo, "commit", "-qam", "second\n\nlonger body")
        sh(self.repo, "remote", "add", "origin", "https://user:secret@example.com/r.git")

    def test_info_log_commit_and_file_pair(self):
        os.remove(os.path.join(self.d, "old.txt"))
        write(os.path.join(self.d, "new.txt"), "n\n")
        write(os.path.join(self.repo, "outside.txt"), "changed\n")             # outside the folder: not listed
        i = G.info(self.d)
        self.assertEqual((i["branch"], i["prefix"]), ("main", "app"))
        self.assertEqual(sorted(x["path"] for x in i["status"]), ["new.txt", "old.txt"])
        self.assertEqual(i["counts"], {"staged": 0, "unstaged": 1, "untracked": 1, "conflicts": 0})
        self.assertEqual(i["remotes"], [{"name": "origin", "url": "https://example.com/r.git"}])     # the password is never shown
        self.assertEqual(i["head"]["subject"], "second")
        lg = G.log(self.d)
        self.assertEqual([c["subject"] for c in lg["commits"]], ["second", "first"])
        self.assertEqual([c["subject"] for c in G.log(self.d, path="old.txt")["commits"]], ["first"])
        self.assertEqual([c["subject"] for c in G.log(self.d, grep="SEC")["commits"]], ["second"])
        page = G.log(self.d, n=1)
        self.assertTrue(page["more"])
        c = G.commit(self.d, lg["commits"][0]["sha"])
        self.assertEqual(c["message"], "second\n\nlonger body")
        self.assertEqual(c["files"], [{"path": "x.py", "status": "modified", "add": 2, "del": 1, "binary": False}])
        root = G.commit(self.d, lg["commits"][1]["sha"])                                            # first commit: no parent
        self.assertEqual(sorted(f["path"] for f in root["files"]), ["old.txt", "x.py"])           # outside.txt is not in the folder
        self.assertEqual(G.file_pair(self.d, "HEAD", "x.py")["old"], "a\nb\nc\n")
        self.assertEqual(G.file_pair(self.d, lg["commits"][1]["sha"], "x.py")["old"], "")         # new in the root commit

    def test_changes_vs_head_and_committed_text(self):
        write(os.path.join(self.d, "x.py"), "a\nB\nc\nd\ne\n")
        os.remove(os.path.join(self.d, "old.txt"))
        write(os.path.join(self.d, "u.txt"), "1\n2\n")
        got = {f["path"]: (f["status"], f["add"], f["del"]) for f in G.changes(self.d)}
        self.assertEqual(got, {"x.py": ("modified", 1, 0), "old.txt": ("deleted", 0, 1), "u.txt": ("untracked", 2, 0)})
        self.assertEqual(G.committed(self.d, "old.txt"), "bye\n")                                  # deleted files still compare
        self.assertIsNone(G.committed(self.d, "u.txt"))
        self.assertIsNone(G.changes(self.tmp))                                                      # not a repository

    def test_refuses_option_like_or_escaping_input(self):
        for rev in ("--all", "HEAD~1", "main;rm", "", None, "-n1"):
            self.assertIsNone(G.commit(self.d, rev))
            self.assertIsNone(G.file_at(self.d, rev, "x.py"))
        for p in ("../outside.txt", "/etc/passwd", ".git/config", "a/../../x", ""):
            self.assertFalse(G.file_at(self.d, "HEAD", p))                                         # refused (None) or "no such file in the folder" ("")
            self.assertIsNone(G.committed(self.d, p))
        self.assertEqual(G.log(self.d, rev="--all"), {"commits": [], "more": False})
        self.assertIsNone(G.resolve_ref(self.d, "--upload-pack=x"))
        self.assertEqual(G.resolve_ref(self.d, "main"), G.head(self.d))

    def test_server_endpoints(self):
        import server
        s = store.create_session("t", "", "", cwd=self.d)
        q = {"id": s["id"]}
        self.assertEqual(server.git_info(None, q, {})["branch"], "main")
        head = server.git_log(None, dict(q, rev="main"), {})["commits"][0]
        self.assertEqual(server.git_commit(None, dict(q, rev=head["sha"]), {})["commit"]["subject"], "second")
        f = server.git_file(None, dict(q, rev=head["sha"], path="x.py"), {})
        self.assertEqual((f["old"], f["new"]), ("a\nb\nc\n", "a\nB\nc\nd\n"))
        with self.assertRaises(server.Err):
            server.git_log(None, dict(q, rev="nope"), {})
        os.remove(os.path.join(self.d, "old.txt"))
        d = server.file_diff(None, dict(q, path="old.txt", base="git"), {})
        self.assertEqual((d["deleted"], d["base"], d["now"]), (True, "bye\n", ""))
        self.assertEqual({x["path"] for x in server.file_changes(None, dict(q, base="git"), {})["files"]}, {"old.txt"})
        plain = store.create_session("p", "", "")
        self.assertEqual(server.git_info(None, {"id": plain["id"]}, {}), {"repo": False})


class SnapChanges(Base):
    def test_added_modified_deleted_and_binary_since_the_snapshot(self):
        proj = os.path.join(self.tmp, "p")
        write(os.path.join(proj, "src", "a.py"), "x = 1\n")
        write(os.path.join(proj, "gone.txt"), "bye\n")
        write(os.path.join(proj, "same.txt"), "same\n")
        with open(os.path.join(proj, "bin.dat"), "wb") as f:
            f.write(b"\0\1")
        s = store.create_session("t", "", "", cwd=proj)
        sid = s["id"]
        self.assertIsNone(store.snap_changes(sid, store.tree(sid)))                                # no snapshot yet
        store.snapshot(sid, store.tree(sid))
        self.assertEqual(store.snap_changes(sid, store.tree(sid)), [])
        store.write_text(sid, "src/a.py", "x = 2\ny = 3\n")
        os.remove(os.path.join(proj, "gone.txt"))
        write(os.path.join(proj, "new.md"), "hi\n")
        with open(os.path.join(proj, "bin.dat"), "wb") as f:
            f.write(b"\0\1\2")
        got = {c["path"]: (c["status"], c["add"], c["del"], c["binary"]) for c in store.snap_changes(sid, store.tree(sid))}
        self.assertEqual(got, {"src/a.py": ("modified", 2, 1, False), "gone.txt": ("deleted", 0, 1, False), "new.md": ("added", 1, 0, False),
                               "bin.dat": ("modified", None, None, True)})
        self.assertIsNone(store.snap_text(sid, "bin.dat"))                                          # there before, but not kept: nothing to compare
        self.assertEqual(store.snap_text(sid, "new.md"), "")
        self.assertEqual(store.snap_text(sid, "gone.txt"), "bye\n")


class LineCounts(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(G.counts("a\nb\nc\n", "a\nB\nc\nd\n"), (2, 1))
        self.assertEqual(G.counts("", "x\ny\n"), (2, 0))
        self.assertEqual(G.counts("same", "same"), (0, 0))


if __name__ == "__main__":
    unittest.main()
