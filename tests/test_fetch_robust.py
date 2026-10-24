import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from aihub.cli import gitx


def git(*a, cwd):
    subprocess.check_call(["git", "-c", "user.email=a@b", "-c", "user.name=t"] + list(a), cwd=cwd, stdout=subprocess.DEVNULL)


def rev(cwd, what="HEAD"):
    return subprocess.check_output(["git", "rev-parse", what], cwd=cwd, text=True).strip()


class Classify(unittest.TestCase):
    def test_hosts_that_refuse_a_sha_are_missing_not_failures(self):
        for m in ("fatal: remote error: upload-pack: not our ref 0123456789abcdef0123456789abcdef01234567",
                  "fatal: Server does not allow request for unadvertised object 0123456789abcdef0123456789abcdef01234567",
                  "fatal: couldn't find remote ref v9", "error: no such remote ref refs/heads/x", "fatal: invalid refspec 'a b'"):
            self.assertEqual(gitx.fetch_failure(m), "missing", m)

    def test_login_problems(self):
        for m in ("remote: Invalid username or password.\nfatal: Authentication failed for 'https://github.com/a/b.git/'",
                  "remote: Password authentication is not supported for Git operations.", "remote: HTTP Basic: Access denied",
                  "remote: You are not allowed to download code from this project.",
                  "fatal: unable to access 'https://h/a.git/': The requested URL returned error: 403",
                  "fatal: unable to access 'https://h/a.git/': The requested URL returned error: 401",
                  "remote: Repository not found.\nfatal: repository 'https://github.com/a/b.git/' not found",
                  "fatal: could not read Username for 'https://h': terminal prompts disabled"):
            self.assertEqual(gitx.fetch_failure(m), "auth", m)

    def test_network_and_other(self):
        for m in ("git fetch timed out after 300s", "fatal: unable to access 'https://h/': Could not resolve host: h",
                  "fatal: unable to access 'https://h/': SSL certificate problem"):
            self.assertIn(gitx.fetch_failure(m), ("net", "auth"), m)
        self.assertEqual(gitx.fetch_failure("fatal: the remote end hung up unexpectedly"), "other")
        # a sha that merely contains 403 is not an HTTP status
        self.assertEqual(gitx.fetch_failure("fatal: the remote end hung up on 1403abc"), "other")


class CacheKey(unittest.TestCase):
    def test_url_variants_share_a_clone(self):
        with tempfile.TemporaryDirectory() as b:
            ds = {gitx.cache_dir(u, b) for u in ("https://GitLab.Corp/a/b/c/repo.git", "https://gitlab.corp/a/b/c/repo", "https://gitlab.corp/a/b/c/repo/",
                                                  "https://gitlab.corp/a/b/c/repo.git/")}
            self.assertEqual(len(ds), 1)
            self.assertNotEqual(gitx.cache_dir("https://gitlab.corp/a/b/c/other.git", b), ds.pop())

    def test_path_case_still_counts(self):
        with tempfile.TemporaryDirectory() as b:
            self.assertNotEqual(gitx.cache_dir("https://h/A/r", b), gitx.cache_dir("https://h/a/r", b))

    def test_an_existing_clone_under_the_old_name_is_kept(self):
        with tempfile.TemporaryDirectory() as b:
            old = os.path.join(b, gitx._cache_key("https://h/a/r.git"))
            os.makedirs(os.path.join(old, ".git"))
            self.assertEqual(gitx.cache_dir("https://h/a/r.git", b), old)


class Remote(unittest.TestCase):
    """A bare remote with a monorepo (packages p and q), three commits on main and one extra commit only on branch `feature`."""
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.src = os.path.join(self.td, "src")
        os.makedirs(self.src)
        subprocess.check_call(["git", "init", "-q", "-b", "main"], cwd=self.src)
        self.revs = []
        for v in ("1", "2", "3"):
            for n in ("p", "q"):
                os.makedirs(os.path.join(self.src, n), exist_ok=True)
                open(os.path.join(self.src, n, "aihub.toml"), "w").write('[package]\nname="%s"\nversion="%s"\n' % (n, v))
            git("add", ".", cwd=self.src)
            git("commit", "-qm", v, cwd=self.src)
            self.revs.append(rev(self.src))
        git("checkout", "-qb", "feature", cwd=self.src)
        open(os.path.join(self.src, "p", "aihub.toml"), "w").write('[package]\nname="p"\nversion="9"\n')
        git("commit", "-qam", "f", cwd=self.src)
        self.feature = rev(self.src)
        git("checkout", "-q", "main", cwd=self.src)
        self.bare = os.path.join(self.td, "o.git")
        subprocess.check_call(["git", "clone", "-q", "--bare", self.src, self.bare])
        self.url = "file://" + self.bare
        self.env = {k: os.environ.get(k) for k in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0")}
        os.environ.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="protocol.version", GIT_CONFIG_VALUE_0="0")   # like hosts that refuse `fetch <sha>`
        self.base = os.path.join(self.td, "cache")

    def tearDown(self):
        for k, v in self.env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        shutil.rmtree(self.td, ignore_errors=True)

    def head(self, d):
        return gitx.run(["rev-parse", "HEAD"], cwd=d)


class Ladder(Remote):
    def test_commit_only_on_another_branch(self):
        d = gitx.fetch(self.url, "main", self.feature, base=self.base)
        self.assertEqual(self.head(d), self.feature)
        self.assertIn('version="9"', open(os.path.join(d, "p", "aihub.toml")).read())

    def test_commit_on_another_branch_with_a_shallow_cache(self):
        gitx.fetch(self.url, "main", None, base=self.base)
        self.assertEqual(self.head(gitx.fetch(self.url, "main", self.feature, base=self.base)), self.feature)
        self.assertEqual(self.head(gitx.fetch(self.url, "main", self.revs[0], base=self.base)), self.revs[0])

    def test_renamed_branch_in_manifest(self):
        self.assertEqual(self.head(gitx.fetch(self.url, "gone", self.revs[1], base=self.base)), self.revs[1])

    def test_short_sha(self):
        d = gitx.fetch(self.url, "main", self.revs[0][:8], base=self.base)
        self.assertEqual(self.head(d), self.revs[0])
        d = gitx.fetch(self.url, "main", self.feature[:7], base=self.base)
        self.assertEqual(self.head(d), self.feature)

    def test_tag_and_branch_refs(self):
        git("tag", "v1", self.revs[0], cwd=self.bare)
        self.assertEqual(self.head(gitx.fetch(self.url, "main", "v1", base=self.base)), self.revs[0])
        self.assertEqual(self.head(gitx.fetch(self.url, "main", "feature", base=self.base)), self.feature)

    def test_cached_commit_needs_no_network(self):
        gitx.fetch(self.url, "main", self.revs[0], base=self.base)
        gitx.fetch(self.url, "main", None, base=self.base)
        shutil.rmtree(self.bare)                                            # remote gone: only the cache is left
        d = gitx.fetch(self.url, "main", self.revs[0], base=self.base)
        self.assertEqual(self.head(d), self.revs[0])
        d = gitx.fetch(self.url, "main", self.revs[0][:7], base=self.base)
        self.assertEqual(self.head(d), self.revs[0])

    def test_unknown_commit_is_an_error_with_the_git_line(self):
        with self.assertRaises(gitx.GitError) as c:
            gitx.fetch(self.url, "main", "0" * 40, base=self.base)
        self.assertIn("cannot find", str(c.exception))
        self.assertIn("git:", str(c.exception))

    def test_other_url_spellings_use_the_same_clone(self):
        a = gitx.fetch(self.url, "main", self.revs[0], base=self.base)
        b = gitx.fetch(self.url + "/", "main", self.revs[1], base=self.base)
        self.assertEqual(a, b)

    def test_checkout_never_downloads_lfs(self):
        seen = []
        real = gitx.run

        def spy(args, *a, **k):
            if args and args[0] == "checkout":
                seen.append((k.get("extra_env") or {}).get("GIT_LFS_SKIP_SMUDGE"))
            return real(args, *a, **k)
        with mock.patch.object(gitx, "run", spy):
            gitx.fetch(self.url, "main", self.revs[0], base=self.base)
            gitx.fetch(self.url, "main", None, base=self.base)
        self.assertEqual(seen, ["1", "1"])


class Failures(Remote):
    def test_unreachable_remote_is_not_reported_as_a_missing_commit(self):
        with self.assertRaises(gitx.GitError) as c:
            gitx.fetch("file://" + os.path.join(self.td, "nope.git"), "main", self.revs[0], base=self.base)
        self.assertNotIn("cannot find", str(c.exception))

    def test_auth_failure_is_raised_as_git_said_it(self):
        real = gitx.run
        msg = "remote: HTTP Basic: Access denied\nfatal: Authentication failed for 'https://h/a.git/'"

        def deny(args, *a, **k):
            if args and args[0] == "fetch":
                raise gitx.GitError(msg)
            return real(args, *a, **k)
        from aihub.cli import remote_config
        with mock.patch.object(gitx, "run", deny):
            with self.assertRaises(gitx.GitError) as c:
                gitx.fetch(self.url, "main", self.revs[0], base=self.base)
        self.assertEqual(str(c.exception), msg)
        self.assertTrue(remote_config.is_auth_error(c.exception))

    def test_timeout_is_raised(self):
        real = gitx.run

        def slow(args, *a, **k):
            if args and args[0] == "fetch":
                raise gitx.GitError("git fetch timed out after 300s")
            return real(args, *a, **k)
        with mock.patch.object(gitx, "run", slow):
            with self.assertRaises(gitx.GitError) as c:
                gitx.fetch(self.url, "main", self.revs[0], base=self.base)
        self.assertIn("timed out", str(c.exception))

    def test_missing_unpinned_branch_is_an_error(self):
        with self.assertRaises(gitx.GitError):
            gitx.fetch(self.url, "nobranch", None, base=self.base)


class Monorepo(Remote):
    def test_two_packages_pin_the_same_older_commit_from_one_clone(self):
        gitx.fetch(self.url, "main", None, base=self.base)                           # the cache is depth-1 at the tip
        d = gitx.fetch(self.url, "main", self.revs[0], base=self.base)               # one clone serves every subdir of the commit
        self.assertEqual(self.head(d), self.revs[0])
        for n in ("p", "q"):
            with open(os.path.join(d, n, "aihub.toml")) as f:
                self.assertIn('version="1"', f.read())
        self.assertEqual(gitx.fetch(self.url, "main", self.revs[0], base=self.base), d)     # second package: no change, same folder


if __name__ == "__main__":
    unittest.main()
