"""Chunked, parallel, resumable upload (`publish -i`) and download (`install`, `download`) of package archives, plus the web/CLI choices
around them. The CLI talks to the real app through a bridge that turns api._req calls into TestClient requests."""
import hashlib
import os
import random
import tarfile
import tempfile
import time
import unittest
from unittest import mock

from aihub.cli import api, main as cli_main, registry
from aihub.server import direct as D
from tests.test_direct_publish import API, hub, login, make_tar, push


class Resp:
    def __init__(self, r):
        self.r, self.status, self.headers = r, r.status_code, r.headers

    def read(self, *a):
        return self.r.content

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def bridge(client, auth, log=None, hook=None):
    """api._req replacement that talks to the TestClient. `hook(method, path, headers)` may raise to simulate trouble."""
    def req(method, path, body=None, raw=None, headers=None, timeout=30):
        h = dict(auth)
        h.update(headers or {})
        if log is not None:
            log.append((method, path, dict(h)))
        if hook:
            hook(method, path, h)
        r = client.request(method, API + path, json=body, content=raw, headers=h)
        if r.status_code >= 400:
            try:
                msg = r.json().get("detail", r.reason_phrase)
            except Exception:
                msg = r.reason_phrase
            raise api.ApiError("%d %s: %s" % (r.status_code, path, msg))
        return Resp(r)
    return req


def big_tar(version="1.0.0", kb=60, name="dp-demo"):
    return make_tar(name=name, version=version, extra={"blob.bin": os.urandom(kb * 1024).hex()})


def sha(b):
    return hashlib.sha256(b).hexdigest()


class ChunkedUpload(unittest.TestCase):
    def setUp(self):
        self.c = hub()
        self.root = login(self.c, "root")
        self.alice = login(self.c, "alice")
        self.bob = login(self.c, "bob")
        self.c.app.state.direct_uploads.chunk_size = 2000
        self.raw = big_tar()

    def start(self, h=None, raw=None):
        raw = raw or self.raw
        return self.c.post(API + "/index/direct/uploads", json={"size": len(raw), "sha256": sha(raw)}, headers=h or self.alice)

    def put(self, uid, n, raw=None, h=None, **hd):
        raw = raw or self.raw
        cs = 2000
        data = raw[n * cs:(n + 1) * cs]
        return self.c.put(API + "/index/direct/uploads/%s/chunks/%d" % (uid, n), content=data, headers=dict(h or self.alice, **hd))

    def test_chunks_in_any_order_then_complete_publishes_the_same_bytes(self):
        init = self.start().json()
        self.assertEqual(init["chunk_size"], 2000)
        self.assertGreater(init["chunks"], 5)
        order = list(range(init["chunks"]))
        random.Random(1).shuffle(order)
        for n in order:
            r = self.put(init["upload_id"], n, **{"X-Chunk-Sha256": sha(self.raw[n * 2000:(n + 1) * 2000])})
            self.assertEqual(r.status_code, 200, r.text)
        st = self.c.get(API + "/index/direct/uploads/" + init["upload_id"], headers=self.alice).json()
        self.assertEqual(st["received"], list(range(init["chunks"])))
        done = self.c.post(API + "/index/direct/uploads/%s/complete" % init["upload_id"], headers=self.alice)
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json()["status"], "published")
        self.assertEqual(self.c.get(API + "/index/packages/dp-demo/transfer/1.0.0", headers=self.bob).content, self.raw)
        self.assertEqual(self.c.get(API + "/index/direct/uploads/" + init["upload_id"], headers=self.alice).status_code, 404)    # session cleaned up

    def test_same_upload_started_again_resumes_and_a_missing_chunk_blocks_complete(self):
        init = self.start().json()
        for n in (0, 2, 3):
            self.put(init["upload_id"], n)
        again = self.start().json()
        self.assertEqual(again["upload_id"], init["upload_id"])
        self.assertEqual(again["received"], [0, 2, 3])
        r = self.c.post(API + "/index/direct/uploads/%s/complete" % init["upload_id"], headers=self.alice)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("missing", r.text)

    def test_damaged_or_misplaced_chunks_are_refused(self):
        uid = self.start().json()["upload_id"]
        self.assertEqual(self.put(uid, 1, **{"X-Chunk-Sha256": "0" * 64}).status_code, 400)
        self.assertEqual(self.c.put(API + "/index/direct/uploads/%s/chunks/1" % uid, content=b"short", headers=self.alice).status_code, 400)
        self.assertEqual(self.put(uid, 999).status_code, 400)
        big = self.c.put(API + "/index/direct/uploads/%s/chunks/1" % uid, content=b"x" * 5000, headers=self.alice)
        self.assertEqual(big.status_code, 413)

    def test_wrong_announced_digest_is_caught_when_joined(self):
        init = self.c.post(API + "/index/direct/uploads", json={"size": len(self.raw), "sha256": "ab" * 32}, headers=self.alice).json()
        for n in range(init["chunks"]):
            self.put(init["upload_id"], n)
        r = self.c.post(API + "/index/direct/uploads/%s/complete" % init["upload_id"], headers=self.alice)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self.c.get(API + "/index/packages/dp-demo", headers=self.bob).status_code, 404)

    def test_sessions_are_private_and_need_an_account_and_can_be_cancelled(self):
        uid = self.start().json()["upload_id"]
        self.assertEqual(self.c.get(API + "/index/direct/uploads/" + uid, headers=self.bob).status_code, 404)
        self.assertEqual(self.put(uid, 0, h=self.bob).status_code, 404)
        self.assertEqual(self.c.post(API + "/index/direct/uploads", json={"size": 10, "sha256": "0" * 64}).status_code, 401)
        self.assertEqual(self.c.delete(API + "/index/direct/uploads/" + uid, headers=self.bob).status_code, 200)     # not his: nothing happens
        self.assertEqual(self.c.get(API + "/index/direct/uploads/" + uid, headers=self.alice).status_code, 200)
        self.c.delete(API + "/index/direct/uploads/" + uid, headers=self.alice)
        self.assertEqual(self.c.get(API + "/index/direct/uploads/" + uid, headers=self.alice).status_code, 404)

    def test_limit_applies_to_the_announced_size_and_old_sessions_are_swept(self):
        with mock.patch.object(type(self.c.app.state.sync), "num", lambda s, k, *a, **kw: 1 if k == "direct_max_mb" else 5):
            r = self.c.post(API + "/index/direct/uploads", json={"size": 3 * 1024 * 1024, "sha256": "0" * 64}, headers=self.alice)
        self.assertEqual(r.status_code, 413, r.text)
        ups = self.c.app.state.direct_uploads
        uid = self.start().json()["upload_id"]
        old = time.time() - D.SESSION_TTL - 10
        os.utime(os.path.join(ups.base, uid), (old, old))
        self.start(raw=big_tar(version="2.0.0"))                           # starting another one sweeps the stale session
        self.assertFalse(os.path.isdir(os.path.join(ups.base, uid)))

    def test_package_page_marks_archive_versions_with_digest_and_size_and_no_git_source(self):
        push(self.c, self.alice, self.raw)
        page = self.c.get(API + "/packages/dp-demo", headers=self.bob).json()
        v = page["versions"][0]
        self.assertTrue(v["archive"])
        self.assertEqual(v["sha256"], sha(self.raw))
        self.assertEqual(v["size"], len(self.raw))
        self.assertEqual(v["source"], "")


class RangeDownload(unittest.TestCase):
    def setUp(self):
        self.c = hub()
        self.alice = login(self.c, "root")
        self.raw = big_tar(kb=80)
        push(self.c, self.alice, self.raw)
        self.path = "/index/packages/dp-demo/transfer/1.0.0"
        self.dest = os.path.join(tempfile.mkdtemp(), "out.tar.gz")

    def get(self, hook=None, log=None, **kw):
        with mock.patch.object(api, "_req", bridge(self.c, self.alice, log, hook)):
            return api.download_ranges(self.path, self.dest, chunk=3000, **kw)

    def test_the_hub_answers_ranges_and_tells_the_digest(self):
        r = self.c.get(API + self.path, headers=dict(self.alice, Range="bytes=0-99"))
        self.assertEqual(r.status_code, 206)
        self.assertEqual(r.headers["Content-Range"], "bytes 0-99/%d" % len(self.raw))
        self.assertEqual(r.headers["X-Aihub-Sha256"], sha(self.raw))
        self.assertEqual(r.content, self.raw[:100])

    def test_parallel_chunks_give_the_same_file_and_report_progress(self):
        seen, log = [], []
        head = self.get(log=log, progress=lambda d, t: seen.append((d, t)))
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), self.raw)
        ranges = [h.get("Range") for _, _, h in log]
        self.assertGreater(len(ranges), 5)                                  # many Range requests, not one body
        self.assertEqual(seen[-1], (len(self.raw), len(self.raw)))
        self.assertEqual(head.get("x-aihub-cache"), "direct")               # headers are case-insensitive
        self.assertFalse(os.path.exists(self.dest + ".part"))
        self.assertFalse(os.path.exists(self.dest + ".part.json"))

    def test_a_flaky_chunk_is_retried(self):
        hits = {"n": 0}

        def hook(method, path, h):
            if h.get("Range", "").startswith("bytes=6000-") and hits["n"] == 0:
                hits["n"] += 1
                raise api.ApiError("503 %s: busy" % path)
        with mock.patch.object(api.time, "sleep", lambda s: None):
            self.get(hook=hook)
        self.assertEqual(hits["n"], 1)
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), self.raw)

    def test_an_interrupted_download_continues_without_refetching_finished_chunks(self):
        def die(method, path, h):
            if h.get("Range", "").startswith("bytes=9000-"):
                raise api.ApiError("404 %s: gone" % path)                   # not transient: the download stops here
        with self.assertRaises(api.ApiError):
            self.get(hook=die)
        self.assertTrue(os.path.exists(self.dest + ".part.json"))
        self.assertFalse(os.path.exists(self.dest))
        log = []
        self.get(log=log)
        asked = [h["Range"] for _, _, h in log]
        self.assertIn("bytes=0-2999", asked)                                 # the probe
        self.assertEqual(len([a for a in asked if a.startswith("bytes=3000-")]), 0)    # finished before, not asked again
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), self.raw)

    def test_a_file_that_changed_meanwhile_is_refused_and_a_bad_digest_never_lands(self):
        class Moved(Exception):
            pass
        real = bridge(self.c, self.alice)

        def req(method, path, body=None, raw=None, headers=None, timeout=30):
            r = real(method, path, body, raw, headers, timeout)
            if (headers or {}).get("Range", "").startswith("bytes=3000-"):
                r.headers = dict(r.headers, **{"X-Aihub-Rev": "somethingelse"})
            return r
        with mock.patch.object(api, "_req", req), self.assertRaises(api.ApiError) as e:
            api.download_ranges(self.path, self.dest, chunk=3000)
        self.assertIn("changed", str(e.exception))
        os.remove(self.dest + ".part.json") if os.path.exists(self.dest + ".part.json") else None
        with self.assertRaises(api.ApiError) as e2:
            self.get(sha256="0" * 64)
        self.assertIn("sha256", str(e2.exception))
        self.assertFalse(os.path.exists(self.dest))

    def test_small_files_and_hubs_without_range_are_one_plain_download(self):
        real = bridge(self.c, self.alice)
        log = []

        def req(method, path, body=None, raw=None, headers=None, timeout=30):
            log.append(1)
            return real(method, path, body, raw, {k: v for k, v in (headers or {}).items() if k != "Range"}, timeout)   # server ignores Range
        with mock.patch.object(api, "_req", req):
            api.download_ranges(self.path, self.dest, chunk=3000)
        self.assertEqual(len(log), 1)
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), self.raw)


class CliChoices(unittest.TestCase):
    def setUp(self):
        self.c = hub()
        self.h = login(self.c, "root")
        self.raw = big_tar(kb=80)
        push(self.c, self.h, self.raw)
        self.r = {"name": "dp-demo", "version": "1.0.0", "repo": {"url": D.URL, "branch": "main"}}
        self.out = tempfile.mkdtemp()

    def run_cmd(self, argv):
        a = cli_main.build_parser().parse_args(argv)
        with mock.patch.object(api, "_req", bridge(self.c, self.h)), mock.patch.object(registry, "resolve", return_value=dict(self.r)):
            a.fn(a)

    def test_publish_dash_i_sends_a_big_archive_in_parallel_chunks_and_it_installs_back(self):
        self.c.app.state.direct_uploads.chunk_size = 3000
        raw = big_tar(version="2.0.0", kb=70)
        path = os.path.join(self.out, "pkg.tar.gz")
        with open(path, "wb") as f:
            f.write(raw)
        log, seen = [], []
        with mock.patch.object(api, "_req", bridge(self.c, self.h, log)), mock.patch.object(api, "SINGLE_MAX", 1000):
            res = api.publish_direct(path, progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(res["status"], "published")
        puts = [p for m, p, _ in log if m == "PUT"]
        self.assertGreater(len(puts), 5)
        self.assertEqual(seen[-1], (len(raw), len(raw)))
        self.assertEqual(self.c.get(API + "/index/packages/dp-demo/transfer/2.0.0", headers=self.h).content, raw)
        with mock.patch.object(api, "_req", bridge(self.c, self.h)), mock.patch.object(api, "SINGLE_MAX", 1000):
            again = api.publish_direct(path)                                  # same bytes: the whole thing is idempotent
        self.assertEqual(again["status"], "already published")

    def test_small_archive_is_one_request(self):
        path = os.path.join(self.out, "p.tar.gz")
        with open(path, "wb") as f:
            f.write(make_tar(version="3.0.0"))
        log = []
        with mock.patch.object(api, "_req", bridge(self.c, self.h, log)):
            api.publish_direct(path)
        self.assertEqual([(m, p) for m, p, _ in log], [("POST", "/index/direct")])

    def test_download_of_an_archive_package_saves_the_tar_gz(self):
        self.run_cmd(["download", "dp-demo", "-o", self.out])
        got = os.path.join(self.out, "dp-demo-1.0.0.tar.gz")
        with open(got, "rb") as f:
            self.assertEqual(f.read(), self.raw)
        self.run_cmd(["download", "dp-demo", "-o", self.out, "--folder"])
        self.assertTrue(os.path.isfile(os.path.join(self.out, "dp-demo-1.0.0", "aihub.toml")))

    def test_install_source_choices(self):
        self.assertEqual(cli_main._source_mode(cli_main.build_parser().parse_args(["install", "x", "--from", "archive"])), (True, False))
        self.assertEqual(cli_main._source_mode(cli_main.build_parser().parse_args(["install", "x", "--from", "repo"])), (False, True))
        self.assertEqual(cli_main._source_mode(cli_main.build_parser().parse_args(["install", "x", "-i"])), (True, False))
        with self.assertRaises(ValueError):
            cli_main._source_mode(cli_main.build_parser().parse_args(["install", "x", "-i", "--from", "repo"]))
        registry.INDIRECT, registry.REPO_ONLY = False, True
        try:
            with self.assertRaises(api.ApiError) as e:
                registry.checkout(self.r, os.path.join(self.out, "x"))
            self.assertIn("archive package", str(e.exception))
        finally:
            registry.INDIRECT = registry.REPO_ONLY = False

    def test_install_from_archive_package_downloads_and_unpacks(self):
        registry.INDIRECT = registry.REPO_ONLY = False
        dest = os.path.join(self.out, "pkg")
        with mock.patch.object(api, "_req", bridge(self.c, self.h)):
            rev = registry.checkout(self.r, dest)
        self.assertEqual(rev, sha(self.raw))
        self.assertTrue(os.path.isfile(os.path.join(dest, "aihub.toml")))


if __name__ == "__main__":
    unittest.main()
