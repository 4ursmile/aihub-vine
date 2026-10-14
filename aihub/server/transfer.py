"""Indirect install: the hub fetches a package from git once, caches it, and hands the CLI a tar.gz (`aihub install -i`).

A monorepo is fetched ONCE: every package of that repo is cut from the same cached checkout, so a sibling is served
without another git request while the checkout is fresh (admin number `transfer_repo_ttl`; a pinned `ref` is trusted longer).
Archives are cached on disk by (repo, subdir, commit), so the second client of a package costs no git and no tar work."""
import hashlib
import os
import re
import tarfile
import threading
import time

from ..cli import gitx

PINNED_TTL = 3600
_SKIP = (".git",)


class TransferError(Exception):
    pass


class Transfer:
    def __init__(self, data_dir, ttl):
        self.base = os.path.join(data_dir, "transfer")
        self.repos = os.path.join(self.base, "repos")
        self.tars = os.path.join(self.base, "tars")
        os.makedirs(self.tars, exist_ok=True)
        self.ttl = ttl                          # callable -> seconds
        self.fresh = {}                         # (url, branch, ref) -> (time, rev)
        self.locks = {}
        self.guard = threading.Lock()

    def _lock(self, url):
        with self.guard:
            return self.locks.setdefault(url, threading.Lock())

    @staticmethod
    def _subdir(sub):
        sub = (sub or "").strip().strip("/")
        if sub and (sub.startswith(("/", "\\")) or ".." in re.split(r"[\\/]+", sub)):
            raise TransferError("bad subdir")
        return sub

    def _tar_path(self, url, sub, rev):
        key = hashlib.sha256((url + "\0" + sub).encode()).hexdigest()[:24]
        return os.path.join(self.tars, "%s-%s.tar.gz" % (key, rev)), key

    def get(self, repo):
        """-> (path of the tar.gz, commit). Raises TransferError."""
        url, branch, ref = repo.get("url"), repo.get("branch") or "main", repo.get("ref") or None
        if not url:
            raise TransferError("package has no git location")
        sub = self._subdir(repo.get("subdir"))
        fk = (url, branch, ref)
        hit = self.fresh.get(fk)
        limit = PINNED_TTL if ref else self.ttl()
        if hit and time.time() - hit[0] < limit:
            p, _ = self._tar_path(url, sub, hit[1])
            if os.path.isfile(p):
                return p, hit[1]
        with self._lock(url):
            hit = self.fresh.get(fk)
            fresh = bool(hit and time.time() - hit[0] < limit)
            p = self._tar_path(url, sub, hit[1])[0] if hit else None
            if fresh and os.path.isfile(p):
                return p, hit[1]
            try:
                if fresh and self._head(url) == hit[1]:      # sibling of a package already fetched: same checkout, no network
                    d = self._dir(url)
                else:
                    d = gitx.fetch(url, branch, ref, base=self.repos)
                rev = gitx.run(["rev-parse", "HEAD"], cwd=d)
            except gitx.GitError as e:
                raise TransferError("git: %s" % e)
            self.fresh[fk] = (time.time(), rev)
            p, key = self._tar_path(url, sub, rev)
            if not os.path.isfile(p):
                self._build(os.path.join(d, sub), p, key)
            return p, rev

    def _dir(self, url):
        return gitx.cache_dir(url, self.repos)

    def _head(self, url):
        try:
            return gitx.run(["rev-parse", "HEAD"], cwd=self._dir(url), check=False) or ""
        except gitx.GitError:
            return ""

    def _build(self, src, dest, key):
        if not os.path.isfile(os.path.join(src, "aihub.toml")):
            raise TransferError("no aihub.toml in the package folder")
        tmp = dest + ".%d.tmp" % threading.get_ident()
        with tarfile.open(tmp, "w:gz") as t:
            for name in sorted(os.listdir(src)):
                if name not in _SKIP:
                    t.add(os.path.join(src, name), arcname=name, filter=lambda ti: None if os.path.basename(ti.name) == ".git" else ti)
        os.replace(tmp, dest)
        for f in os.listdir(self.tars):         # older commits of this package are never asked for again
            if f.startswith(key + "-") and os.path.join(self.tars, f) != dest:
                try:
                    os.remove(os.path.join(self.tars, f))
                except OSError:
                    pass
