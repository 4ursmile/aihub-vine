import os
import subprocess
import tempfile
import unittest

from aihub.core import pkgindex
from aihub.server import routers
from aihub.server.sync import Sync

TOML = '[package]\nname = "demo-pkg"\nversion = "1.0.0"\ntype = "skill"\ndescription = "d"\ntags = ["x"]\n'


def git(*a, cwd=None):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True, capture_output=True)


class SourceUrl(unittest.TestCase):
    def test_links(self):
        f = routers.source_url
        self.assertEqual(f({"url": "https://tok@github.com/o/r.git", "branch": "main", "ref": "abc", "subdir": "p"}), "https://github.com/o/r/tree/abc/p")
        self.assertEqual(f({"url": "git@github.com:o/r.git", "branch": "dev"}), "https://github.com/o/r/tree/dev")
        self.assertEqual(f({"url": "/local/path"}), "")


class ServerWritesIndex(unittest.TestCase):
    def test_publish_event_updates_index_repo(self):
        with tempfile.TemporaryDirectory() as t:
            pkg, remote, data = [os.path.join(t, x) for x in ("pkg", "remote", "data")]
            os.makedirs(pkg); os.makedirs(data)
            open(os.path.join(pkg, "aihub.toml"), "w").write(TOML); open(os.path.join(pkg, "README.md"), "w").write("# Demo readme\n")
            git("init", "-q", "-b", "main", cwd=pkg); git("add", "-A", cwd=pkg); git("commit", "-q", "-m", "p", cwd=pkg)
            commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=pkg, capture_output=True, text=True).stdout.strip()
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", remote], check=True)
            seed = os.path.join(t, "seed"); os.makedirs(seed)
            git("init", "-q", "-b", "main", cwd=seed); open(os.path.join(seed, "README"), "w").write("x")
            git("add", "-A", cwd=seed); git("commit", "-q", "-m", "s", cwd=seed)
            git("push", "-q", remote, "main", cwd=seed)

            class S:
                data_dir = data
                def env_get(self, k): return {"AIHUB_INDEX_URL": "file://" + remote}.get(k, "")
            class Repos:
                def setting(self, k, d=""): return d
            sy = Sync(Repos(), S())
            self.assertFalse(sy.update_index([{"package": "demo-pkg", "version": "1.0.0", "git_url": "file://" + pkg, "commit": commit}]))   # file:// refused by default
            from unittest import mock
            from aihub.server import sync as syncmod
            with mock.patch.object(syncmod, "SCHEMES", ("file://",)):
                self.assertTrue(sy.update_index([{"package": "demo-pkg", "version": "1.0.0", "git_url": "file://" + pkg, "commit": commit},
                                                 {"package": "demo-pkg", "version": "7.7.7", "git_url": "file://" + pkg, "commit": commit}]))
                self.assertFalse(sy.update_index([{"package": "demo-pkg", "version": "1.0.0", "git_url": "file://" + pkg, "commit": commit}]))   # already there
            c2 = os.path.join(t, "c2")
            subprocess.run(["git", "clone", "-q", remote, c2], check=True, capture_output=True)
            rd = pkgindex.open_source(c2, "index")
            self.assertEqual([v["version"] for v in rd.entry("demo-pkg")["versions"]], ["1.0.0"])    # 7.7.7 does not match the toml: refused
            self.assertEqual(rd.entry("demo-pkg")["readme"], "# Demo readme\n")                       # stored from the checkout
            self.assertEqual(rd.entry("demo-pkg")["repo"]["url"], "file://" + pkg)
            gurl = "https://example.invalid/x"
            self.assertFalse(sy.update_index([{"package": "demo-pkg", "version": "9.9.9", "git_url": gurl}]))       # unreachable: nothing written


if __name__ == "__main__":
    unittest.main()


class ReadmeUrls(unittest.TestCase):
    def test_github_and_gitlab(self):
        f = routers.raw_urls
        self.assertEqual(f({"url": "https://github.com/o/r.git", "ref": "abc", "subdir": "p q"})[0],
                         "https://raw.githubusercontent.com/o/r/abc/p%20q/README.md")
        self.assertEqual(f({"url": "git@gitlab.com:g/sub/r.git", "branch": "main"})[0], "https://gitlab.com/g/sub/r/-/raw/main/README.md")
        self.assertEqual(f({"url": "https://tok@git.corp.example/g/r", "branch": "dev", "subdir": "x"})[1],
                         "https://git.corp.example/g/r/-/raw/dev/x/readme.md")
        self.assertEqual(f({"url": "/local/repo"}), [])

    def test_private_hosts_refused(self):
        self.assertFalse(routers._public_host("https://127.0.0.1/x"))
        self.assertFalse(routers._public_host("https://localhost/x"))
        self.assertEqual(routers.fetch_readme({"url": "https://127.0.0.1/o/r"}), "")


class Hosts(unittest.TestCase):
    def test_allowed(self):
        from aihub.server import nethost as n
        self.assertEqual(n.host_of("git@gitlab.corp:g/r.git"), "gitlab.corp")
        self.assertEqual(n.host_of("https://u:p@Git.Example.com/o/r"), "git.example.com")
        self.assertFalse(n.allowed("https://127.0.0.1/o/r"))
        self.assertFalse(n.allowed("http://10.0.0.5/o/r"))
        self.assertTrue(n.allowed("https://10.0.0.5/o/r", "https://10.0.0.5/index.git"))     # same host as the configured index
        self.assertFalse(n.allowed("https://10.0.0.6/o/r", "https://10.0.0.5/index.git"))
