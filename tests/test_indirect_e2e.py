"""End to end `aihub install NAME -i` against a REAL authenticated smart-HTTP git server (`git http-backend` as CGI).

The hub holds the only git login (AIHUB_GIT_TOKEN / vault); the client has none and must never run a git network command. Packages live in
a MONOREPO (`mono/alpha`, `mono/beta`): alpha is published at an OLDER commit than the tip, beta at the tip. Server flavours:
  lax_v2            protocol v2, everything allowed (GitHub-like)
  lax_v0            client forced to protocol v0, host allows any sha (uploadpack.allowAnySHA1InWant)
  strict_v0         client forced to protocol v0, host refuses `fetch <non-tip sha>` (GitLab/Gitea/Bitbucket default behaviour)
  strict_v2         protocol v2 with the "refuse" options set (git ignores them for v2: an unadvertised sha is still served)
  strict_oldserver  the client asks for v2, the server never answers it (old/proxied host): same as v0 for the fetch
Skipped when `git http-backend` is missing."""
import base64
import contextlib
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.request
import uuid
from unittest import mock

try:
    from fastapi.testclient import TestClient
except Exception:                                                # pragma: no cover
    TestClient = None

from aihub.cli import credhelper, gitx
from aihub.server.config import Settings
from aihub.server.main import create_app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USER, TOKEN = "svc-hub", "glpat-TOPSECRET-e2e"


def _http_backend():
    try:
        d = subprocess.run(["git", "--exec-path"], capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        return False
    return bool(d) and os.path.exists(os.path.join(d, "git-http-backend"))


SKIP = None
if not _http_backend():
    SKIP = "`git http-backend` is not available"
elif TestClient is None:
    SKIP = "fastapi TestClient is not installed"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def clean_env(home):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AIHUB_", "GIT_"))}
    env.update(HOME=home, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="true", SSH_ASKPASS="true")     # never an IDE/GUI login prompt (VS Code askpass hung the run)
    return env


# --------------------------------------------------------------------------------------------------- the git host
class GitHttp:
    """Threaded HTTP server that requires Basic auth and runs `git http-backend` per request. `log` = [(method, path?query, user-ok, Git-Protocol)]."""

    def __init__(self, root, user=USER, token=TOKEN, ignore_v2=False):
        self.root, self.user, self.token, self.ignore_v2 = root, user, token, ignore_v2
        self.log = []
        os.makedirs(root, exist_ok=True)
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _body(self):
                if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                    out = b""
                    while True:
                        n = int(self.rfile.readline().strip() or b"0", 16)
                        if n == 0:
                            self.rfile.readline()
                            return out
                        out += self.rfile.read(n)
                        self.rfile.readline()
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def do_any(self):
                body = self._body()
                auth = self.headers.get("Authorization") or ""
                ok = False
                if auth.startswith("Basic "):
                    try:
                        ok = base64.b64decode(auth[6:]).decode() == "%s:%s" % (outer.user, outer.token)
                    except Exception:
                        ok = False
                outer.log.append((self.command, self.path, ok, self.headers.get("Git-Protocol")))
                if not ok:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="git"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                path, _, query = self.path.partition("?")
                env = {"PATH": os.environ.get("PATH", ""), "HOME": outer.root, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                       "GIT_PROJECT_ROOT": outer.root, "GIT_HTTP_EXPORT_ALL": "1", "PATH_INFO": path, "QUERY_STRING": query,
                       "REQUEST_METHOD": self.command, "CONTENT_TYPE": self.headers.get("Content-Type") or "",
                       "CONTENT_LENGTH": str(len(body)), "REMOTE_USER": outer.user, "REMOTE_ADDR": "127.0.0.1"}
                if self.headers.get("Git-Protocol") and not outer.ignore_v2:
                    env["GIT_PROTOCOL"] = env["HTTP_GIT_PROTOCOL"] = self.headers["Git-Protocol"]
                if self.headers.get("Content-Encoding"):
                    env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
                r = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True)
                head, _, payload = r.stdout.partition(b"\r\n\r\n")
                if not _:
                    head, _, payload = r.stdout.partition(b"\n\n")
                status, hdrs = 200, []
                for ln in head.decode("latin-1").splitlines():
                    k, _, v = ln.partition(":")
                    if k.lower() == "status":
                        status = int(v.split()[0])
                    elif k.strip():
                        hdrs.append((k.strip(), v.strip()))
                self.send_response(status)
                for k, v in hdrs:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            do_GET = do_POST = do_any

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def host(self):
        return "127.0.0.1:%d" % self.port

    def url(self, repo="mono.git"):
        return "http://%s/%s" % (self.host, repo)

    def authed(self, repo="mono.git"):
        return "http://%s:%s@%s/%s" % (self.user, self.token, self.host, repo)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


STRICT = {"uploadpack.allowReachableSHA1InWant": "false", "uploadpack.allowAnySHA1InWant": "false", "uploadpack.allowTipSHA1InWant": "false"}
CONFIGS = {
    "lax_v2": dict(proto=None, cfg={}, ignore_v2=False),
    "lax_v0": dict(proto="0", cfg={"uploadpack.allowAnySHA1InWant": "true"}, ignore_v2=False),
    "strict_v0": dict(proto="0", cfg=STRICT, ignore_v2=False),
    "strict_v2": dict(proto=None, cfg=STRICT, ignore_v2=False),
    "strict_oldserver": dict(proto=None, cfg=STRICT, ignore_v2=True),
}


def toml(name, ver):
    return '[package]\nname = "%s"\nversion = "%s"\ndescription = "d %s"\ntype = "skill"\n' % (name, ver, name)


def b64(s):
    return base64.b64encode(s.encode()).decode()


# --------------------------------------------------------------------------------------------------- the world
class World:
    """Mixin: builds the git host + monorepo (self.c1 = alpha 1.0.0 commit, self.c2 = tip with beta 1.1.0) and, per test, a hub."""
    CONFIG = "lax_v2"
    BRANCH = "main"
    LOGIN = "env"                 # env | indexurl | vault | wrong | none

    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="e2e-")
        self.addCleanup(shutil.rmtree, self.td, True)
        self.base_env = clean_env(os.path.join(self.td, "home-git"))
        os.makedirs(self.base_env["HOME"], exist_ok=True)
        conf = CONFIGS[self.CONFIG]
        self.conf = conf
        self.srv = GitHttp(os.path.join(self.td, "srv"), ignore_v2=conf["ignore_v2"])
        self.addCleanup(self.srv.close)
        bare = os.path.join(self.srv.root, "mono.git")
        self.sh("init", "-q", "--bare", "-b", "main", bare, cwd=self.td)
        self.sh("config", "http.receivepack", "true", cwd=bare)
        for k, v in conf["cfg"].items():
            self.sh("config", k, v, cwd=bare)
        work = self.work = os.path.join(self.td, "work")
        os.makedirs(work)
        self.sh("init", "-q", "-b", "main", cwd=work)
        self.sh("remote", "add", "origin", self.srv.authed(), cwd=work)
        if self.BRANCH != "main":                                  # published branch is not the host's default branch
            self.write("README", "seed")
            self.commit("seed")
            self.sh("push", "-q", "origin", "main", cwd=work)
            self.sh("checkout", "-q", "-b", self.BRANCH, cwd=work)
        self.write("alpha/aihub.toml", toml("alpha", "1.0.0"))
        self.write("beta/aihub.toml", toml("beta", "1.0.0"))
        self.write("alpha/skills/alpha/SKILL.md", "---\nname: alpha\ndescription: d\n---\nalpha\n")
        self.c1 = self.commit("c1: alpha + beta")
        self.write("beta/aihub.toml", toml("beta", "1.1.0"))                  # only beta changes: alpha's commit is no longer the tip
        self.c2 = self.commit("c2: beta only")
        self.sh("push", "-q", "origin", "%s:refs/heads/%s" % (self.BRANCH, self.BRANCH), cwd=work)
        self.assertNotEqual(self.c1, self.c2)

    # ---- git helpers (a clean environment: nothing of the machine's git config or of the AIHUB_* env)
    def sh(self, *a, cwd):
        r = subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t", *a], cwd=cwd, env=self.base_env, capture_output=True, text=True)
        if r.returncode:
            raise AssertionError("git %s failed: %s" % (" ".join(a), r.stderr))
        return r.stdout.strip()

    def write(self, rel, text):
        p = os.path.join(self.work, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def commit(self, msg):
        self.sh("add", "-A", cwd=self.work)
        self.sh("commit", "-q", "-m", msg, cwd=self.work)
        return self.sh("rev-parse", "HEAD", cwd=self.work)

    # ---- the hub (in process; its own env, vault and data folder)
    def hub_env(self):
        home = os.path.join(self.td, "home-hub")
        os.makedirs(home, exist_ok=True)
        env = {"HOME": home, "AIHUB_HOME": os.path.join(home, ".aihub"), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
               "AIHUB_GIT_ALLOW_HTTP": "1"}
        if self.LOGIN in ("env", "indexurl", "wrong"):
            env["AIHUB_GIT_TOKEN"] = TOKEN if self.LOGIN != "wrong" else "not-the-token"
            env["AIHUB_GIT_USER"] = USER
        if self.LOGIN in ("env", "wrong"):
            env["AIHUB_GIT_HOSTS"] = self.srv.host
        if self.LOGIN == "indexurl":
            env["AIHUB_INDEX_URL"] = self.srv.url("index.git")        # only the index lives on this host; the package repo shares host:port
        if self.conf["proto"] is not None:
            env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="protocol.version", GIT_CONFIG_VALUE_0=self.conf["proto"])
        return env

    def start_hub(self):
        env = self.hub_env()
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        for k in [k for k in os.environ if k.startswith("AIHUB_GIT_") and k not in env]:
            del os.environ[k]
        for k in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
            if k not in env:
                os.environ.pop(k, None)
        saved = (gitx.LOGIN, gitx.BATCH, list(gitx.TRUSTED))

        def restore():
            gitx.LOGIN, gitx.BATCH = saved[0], saved[1]
            gitx.TRUSTED[:] = saved[2]
        self.addCleanup(restore)
        if self.LOGIN == "vault":                                    # the login only exists in the vault of the account that runs the server
            fb = mock.patch.object(credhelper, "backends", lambda: [credhelper.File])
            fb.start()
            self.addCleanup(fb.stop)
            self.assertTrue(credhelper.save(self.srv.host, USER, TOKEN))
        else:
            fb = mock.patch.object(credhelper, "backends", lambda: [credhelper.File])      # never the developer's real Keychain
            fb.start()
            self.addCleanup(fb.stop)
        self.c = TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=os.path.join(self.td, "hubdata"))))
        self.addCleanup(self.c.close)
        self.sy, self.ing = self.c.app.state.sync, self.c.app.state.ingest
        self.addCleanup(self.ing.close)
        self.c.post("/api/v1/auth/register", json={"username": "root", "password": "secret1"})
        self.h = {"Authorization": "Bearer " + self.c.post("/api/v1/auth/login", json={"username": "root", "password": "secret1"}).json()["token"]}

    def event(self, name, ver, commit, sub, branch=None, url=None):
        return {"id": uuid.uuid4().hex, "kind": "publish", "user": "", "package": name, "version": ver, "component": "", "client_id": "c1",
                "ts": time.time(), "git_url": url or self.srv.url(), "git_branch": branch or self.BRANCH, "git_subdir": sub, "commit": commit,
                "type": "skill", "description": "d", "bundle": {"toml": b64(toml(name, ver))}}

    def publish(self, *events):
        r = self.c.post("/api/v1/telemetry", json={"events": list(events)})
        self.assertLess(r.status_code, 300, r.text)
        self.ing.flush()
        want = {(e["package"], e["version"]) for e in events}
        for _ in range(100):                                       # a publish closes the Ingest window on its own thread: wait until Sync has it
            if want <= set(self.sy.pending_pubs):
                break
            time.sleep(0.1)
            self.ing.flush()
        else:
            self.fail("the hub never handed the publish events to Sync (pending=%s)" % sorted(self.sy.pending_pubs))
        self.sy.run_once(scheduled=True)

    def publish_default(self):
        self.publish(self.event("alpha", "1.0.0", self.c1, "alpha"), self.event("beta", "1.1.0", self.c2, "beta"))

    def transfer(self, name, version=None):
        e = self.c.get("/api/v1/index/packages/" + name, headers=self.h)
        self.assertEqual(e.status_code, 200, e.text)
        return self.c.get("/api/v1/index/packages/%s/transfer/%s" % (name, version or e.json()["latest_version"]), headers=self.h)

    def second_repo(self, ver="1.2.0", branch="main"):
        """A different repo on the same host holding alpha at `ver`. -> (url, commit)."""
        bare = os.path.join(self.srv.root, "other.git")
        self.sh("init", "-q", "--bare", "-b", "main", bare, cwd=self.td)
        self.sh("config", "http.receivepack", "true", cwd=bare)
        w = os.path.join(self.td, "work2")
        os.makedirs(os.path.join(w, "alpha"))
        with open(os.path.join(w, "alpha", "aihub.toml"), "w") as f:
            f.write(toml("alpha", ver))
        self.sh("init", "-q", "-b", branch, cwd=w)
        self.sh("add", "-A", cwd=w)
        self.sh("commit", "-q", "-m", "other", cwd=w)
        self.sh("push", "-q", self.srv.authed("other.git"), "%s:refs/heads/%s" % (branch, branch), cwd=w)
        return self.srv.url("other.git"), self.sh("rev-parse", "HEAD", cwd=w)

    def members(self, resp):
        p = os.path.join(self.td, "got-%s.tgz" % uuid.uuid4().hex[:6])
        with open(p, "wb") as f:
            f.write(resp.content)
        with tarfile.open(p) as t:
            return {m.name: (t.extractfile(m).read().decode() if m.isfile() else None) for m in t.getmembers()}


class Scenarios:
    """Every flavour runs these."""

    def good(self, name, commit, ver, expect_how="git"):
        r = self.transfer(name)
        self.assertEqual(r.status_code, 200, "%s: %s" % (name, r.text[:700]))
        self.assertEqual(r.headers["X-Aihub-Rev"], commit, "%s served at the wrong commit" % name)
        m = self.members(r)
        self.assertIn('name = "%s"' % name, m["aihub.toml"])
        self.assertIn('version = "%s"' % ver, m["aihub.toml"])
        self.assertFalse([n for n in m if n.startswith(("alpha/", "beta/", ".git"))], "tar must be cut at the package folder: %s" % sorted(m))
        return r

    def test_alpha_older_commit_first_then_beta(self):
        self.start_hub()
        self.publish_default()
        r = self.good("alpha", self.c1, "1.0.0")
        self.assertEqual(r.headers["X-Aihub-Cache"], "git")
        self.good("beta", self.c2, "1.1.0")
        again = self.good("alpha", self.c1, "1.0.0")
        self.assertEqual(again.headers["X-Aihub-Cache"], "tar-cache")
        self.assertEqual(self.good("beta", self.c2, "1.1.0").headers["X-Aihub-Cache"], "tar-cache")

    def test_beta_tip_first_leaves_depth1_clone_then_alpha_old_commit(self):
        self.start_hub()
        self.publish_default()
        self.good("beta", self.c2, "1.1.0")                          # the transfer cache now holds a depth-1 clone at the tip
        self.good("alpha", self.c1, "1.0.0")                         # alpha's commit is NOT in it
        self.assertEqual(self.good("alpha", self.c1, "1.0.0").headers["X-Aihub-Cache"], "tar-cache")

    def test_two_packages_at_one_commit_share_the_checkout(self):
        self.start_hub()
        self.publish(self.event("alpha", "1.0.0", self.c2, "alpha"), self.event("beta", "1.1.0", self.c2, "beta"))
        self.assertEqual(self.good("alpha", self.c2, "1.0.0").headers["X-Aihub-Cache"], "git")
        self.assertEqual(self.good("beta", self.c2, "1.1.0").headers["X-Aihub-Cache"], "repo-cache")


def _make(cfg, branch, login="env"):
    name = "E2E_%s_%s_%s" % (cfg, branch, login)
    ns = {"CONFIG": cfg, "BRANCH": branch, "LOGIN": login}
    return type(name, (World, Scenarios, unittest.TestCase), ns)


for _cfg in CONFIGS:
    globals()["E2E_%s_main" % _cfg] = _make(_cfg, "main")
for _cfg in ("lax_v2", "strict_v0", "strict_oldserver"):
    globals()["E2E_%s_release_branch" % _cfg] = _make(_cfg, "release")
for _login in ("indexurl", "vault"):
    globals()["E2E_strict_v0_login_%s" % _login] = _make("strict_v0", "main", _login)
for _k in [k for k in list(globals()) if k.startswith("E2E_")]:
    globals()[_k] = unittest.skipIf(SKIP, SKIP or "")(globals()[_k])
    globals()[_k].__qualname__ = _k


# --------------------------------------------------------------------------------------------------- versions that live in different repos
@unittest.skipIf(SKIP, SKIP or "")
class PerVersionRepo(World, unittest.TestCase):
    """The repo/branch of an index entry is stored once per PACKAGE (pkgindex.add overwrites it on every publish), but a version's `ref` only exists
    in the repo it was published from. A later publish from another repo/branch, or an OLD version published late, must not break the others."""
    CONFIG = "strict_v0"

    def test_new_version_in_another_repo_old_and_latest_both_install(self):
        self.start_hub()
        other_url, d1 = self.second_repo("1.2.0")
        self.publish(self.event("alpha", "1.0.0", self.c1, "alpha"))
        self.publish(self.event("alpha", "1.2.0", d1, "alpha", url=other_url))
        r = self.transfer("alpha")                                           # latest = 1.2.0 @ other.git
        self.assertEqual(r.status_code, 200, r.text[:700])
        self.assertEqual(r.headers["X-Aihub-Rev"], d1)
        r = self.transfer("alpha", "1.0.0")                                  # the older version still lives in mono.git
        self.assertEqual(r.status_code, 200, r.text[:700])
        self.assertEqual(r.headers["X-Aihub-Rev"], self.c1)

    def test_old_version_published_late_from_another_repo_keeps_latest_working(self):
        self.start_hub()
        other_url, d1 = self.second_repo("1.2.0")
        self.publish(self.event("alpha", "1.2.0", d1, "alpha", url=other_url))
        self.publish(self.event("alpha", "1.0.0", self.c1, "alpha"))          # back-fill of an older release from the monorepo
        e = self.c.get("/api/v1/index/packages/alpha", headers=self.h).json()
        self.assertEqual(e["latest_version"], "1.2.0")
        r = self.transfer("alpha")
        self.assertEqual(r.status_code, 200, "latest 1.2.0 is in other.git but the entry now points at mono.git: " + r.text[:700])
        self.assertEqual(r.headers["X-Aihub-Rev"], d1)

    def test_new_version_on_another_branch_old_and_latest_both_install(self):
        self.start_hub()
        self.publish(self.event("alpha", "1.0.0", self.c1, "alpha"))
        self.write("alpha/aihub.toml", toml("alpha", "1.3.0"))
        self.sh("checkout", "-q", "-b", "next", cwd=self.work)
        c3 = self.commit("alpha 1.3.0 on branch next")
        self.sh("push", "-q", "origin", "next:refs/heads/next", cwd=self.work)
        self.publish(self.event("alpha", "1.3.0", c3, "alpha", branch="next"))
        for ver, rev in (("1.3.0", c3), ("1.0.0", self.c1)):
            r = self.transfer("alpha", ver)
            self.assertEqual(r.status_code, 200, "%s: %s" % (ver, r.text[:700]))
            self.assertEqual(r.headers["X-Aihub-Rev"], rev)


# --------------------------------------------------------------------------------------------------- error paths
@unittest.skipIf(SKIP, SKIP or "")
class ErrorPaths(World, unittest.TestCase):
    CONFIG = "strict_v0"

    def fail(self, login):
        self.LOGIN = login
        self.start_hub()
        self.publish_default()
        r = self.transfer("alpha")
        self.assertEqual(r.status_code, 502, r.text)
        return r.json()["detail"]

    def test_wrong_server_token_is_an_auth_error_with_the_hub_hint(self):
        d = self.fail("wrong")
        self.assertIn("[aihub hint] the hub has no working git login", d)
        self.assertNotIn("cannot find", d)
        self.assertNotIn("not-the-token", d)
        self.assertNotIn(TOKEN, d)

    def test_no_login_at_all_is_an_auth_error_with_the_hub_hint(self):
        d = self.fail("none")
        self.assertIn("[aihub hint] the hub has no working git login", d)
        self.assertNotIn("cannot find", d)

    def test_missing_sha_stays_a_missing_error(self):
        self.LOGIN = "env"
        self.start_hub()
        self.publish(self.event("alpha", "1.0.0", "0" * 40, "alpha"))
        r = self.transfer("alpha")
        self.assertEqual(r.status_code, 502, r.text)
        self.assertIn("cannot find", r.json()["detail"])
        self.assertNotIn("no working git login", r.json()["detail"])


# --------------------------------------------------------------------------------------------------- the real CLI against a real hub
class HubProcess:
    """`python -m aihub.server` on a free port. The port is picked right before each start attempt; a start that dies (port taken meanwhile) is
    retried on another port, and ONLY our own child answering /api/v1/readyz with 200 counts as ready. Never leaves a child behind, even when
    the start fails (the caller registers `close` before `start`)."""
    TRIES, WAIT = 4, 60

    def __init__(self, td, env_extra):
        self.td, self.p, self.log, self.port, self.base = td, None, None, 0, ""
        self.home = os.path.join(td, "home-hubproc")
        os.makedirs(self.home, exist_ok=True)
        self.env = clean_env(self.home)
        self.env.update(AIHUB_HOME=os.path.join(self.home, ".aihub"), PYTHONPATH=ROOT, AIHUB_GIT_ALLOW_HTTP="1", PYTHONDONTWRITEBYTECODE="1",
                        AIHUB_TELEMETRY_URL=os.path.join(td, "no-such-telemetry.git"), AIHUB_INDEX_URL=os.path.join(td, "no-such-index.git"))
        self.env.update(env_extra)

    def start(self):
        last = ""
        for attempt in range(self.TRIES):
            self._stop()
            self.port = free_port()
            self.base = "http://127.0.0.1:%d" % self.port
            self.log = open(os.path.join(self.td, "hub-%d.log" % attempt), "w")
            self.p = subprocess.Popen([sys.executable, "-m", "aihub.server", "--host", "127.0.0.1", "--port", str(self.port), "--data",
                                       os.path.join(self.td, "hubproc-data"), "--recover", "skip"], cwd=self.td, env=self.env,
                                      stdout=self.log, stderr=subprocess.STDOUT)
            deadline = time.time() + self.WAIT
            while time.time() < deadline:
                if self.p.poll() is not None:
                    break                                           # died (e.g. the port was taken in between): try another port
                try:
                    with urllib.request.urlopen(self.base + "/api/v1/readyz", timeout=2) as r:
                        if r.status == 200 and self.p.poll() is None:
                            return self
                except Exception:
                    pass
                time.sleep(0.2)
            self.log.flush()
            last = open(self.log.name).read()[-800:]
        raise AssertionError("hub did not start after %d tries: %s" % (self.TRIES, last))

    @property
    def log_text(self):
        try:
            self.log.flush()
            return open(self.log.name).read()
        except Exception:
            return ""

    def call(self, method, path, body=None, token=None):
        rq = urllib.request.Request(self.base + "/api/v1" + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                    headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})})
        try:
            with urllib.request.urlopen(rq, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def _stop(self):
        if self.p is not None and self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(10)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait(10)
        if self.log is not None:
            self.log.close()
            self.log = None

    def close(self):
        self._stop()


GIT_SHIM = """#!/bin/sh
# records every git call of the client, then runs the real git
printf '%s\\n' "$*" >> "$AIHUB_E2E_GITLOG"
exec "$AIHUB_E2E_REALGIT" "$@"
"""


class _CliBase(World):
    LOGIN = "env"

    def start_hub_proc(self):
        env = {"AIHUB_GIT_TOKEN": TOKEN, "AIHUB_GIT_USER": USER, "AIHUB_GIT_HOSTS": self.srv.host}
        if self.conf["proto"] is not None:
            env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="protocol.version", GIT_CONFIG_VALUE_0=self.conf["proto"])
        self.hub = HubProcess(self.td, env)
        self.addCleanup(self.hub.close)                              # registered BEFORE start: a failed start still kills the child
        self.hub.start()
        self.hub.call("POST", "/auth/register", {"username": "root", "password": "secret1"})
        code, out = self.hub.call("POST", "/auth/login", {"username": "root", "password": "secret1"})
        self.assertEqual(code, 200, out)
        self.token = out["token"]
        code, out = self.hub.call("POST", "/telemetry", {"events": [self.event("alpha", "1.0.0", self.c1, "alpha"),
                                                                    self.event("beta", "1.1.0", self.c2, "beta")]})
        self.assertLess(code, 300, out)
        for name in ("alpha", "beta"):
            for _ in range(150):
                code, _out = self.hub.call("GET", "/index/packages/" + name, token=self.token)
                if code == 200:
                    break
                time.sleep(0.2)
            else:
                raise AssertionError("hub never indexed %s: %s\n%s" % (name, _out, self.hub.log_text[-800:]))

    def client_env(self):
        home = os.path.join(self.td, "home-client")
        os.makedirs(os.path.join(home, ".aihub"), exist_ok=True)
        shim = os.path.join(self.td, "shim")
        os.makedirs(shim, exist_ok=True)
        self.gitlog = os.path.join(self.td, "client-git.log")
        open(self.gitlog, "w").close()
        with open(os.path.join(shim, "git"), "w") as f:
            f.write(GIT_SHIM)
        os.chmod(os.path.join(shim, "git"), 0o755)
        real = shutil.which("git")
        env = clean_env(home)
        env.update(AIHUB_HOME=os.path.join(home, ".aihub"), AIHUB_URL=self.hub.base, PYTHONPATH=ROOT, PYTHONDONTWRITEBYTECODE="1",
                   PATH=shim + os.pathsep + env.get("PATH", ""), AIHUB_E2E_GITLOG=self.gitlog, AIHUB_E2E_REALGIT=real,
                   AIHUB_NO_UPDATE="1", AIHUB_NO_TELEMETRY="1", AIHUB_POLICY_JUDGE="off")
        with open(os.path.join(home, ".aihub", "credentials.json"), "w") as f:
            json.dump({"token": self.token, "username": "root"}, f)
        return env

    def git_calls(self):
        return [ln for ln in open(self.gitlog).read().splitlines() if ln.strip()]


class Scenarios2:
    def run_cli(self, *args):
        env = self.client_env()
        r = subprocess.run([sys.executable, "-m", "aihub.cli", *args], cwd=self.td, env=env, capture_output=True, text=True, timeout=300)
        return r, env

    def test_cli_install_dash_i_needs_no_git_at_all(self):
        self.start_hub_proc()
        r, env = self.run_cli("install", "alpha", "beta", "-i", "-y")
        out = (r.stdout or "") + (r.stderr or "")
        self.assertEqual(r.returncode, 0, out[-3000:] + "\n--- hub log ---\n" + self.hub.log_text[-3000:])
        self.assertNotIn("cannot find", out)
        installed = os.path.join(env["AIHUB_HOME"], "packages")
        found = [os.path.join(b, "aihub.toml") for b, _d, fs in os.walk(env["AIHUB_HOME"]) if "aihub.toml" in fs]
        self.assertTrue([p for p in found if 'name = "alpha"' in open(p).read()], "alpha not installed under %s: %s" % (env["AIHUB_HOME"], out[-1500:]))
        self.assertTrue([p for p in found if 'name = "beta"' in open(p).read()], "beta not installed: " + out[-1500:])
        network = [c for c in self.git_calls() if c.split(" ")[0] in gitx.NETWORK + ("lfs",) or self.srv.host in c]
        self.assertEqual(network, [], "the client ran git network commands with -i:\n" + "\n".join(self.git_calls()))

    def test_cli_single_package_latest(self):
        self.start_hub_proc()
        r, env = self.run_cli("install", "alpha", "-i", "-y")
        out = (r.stdout or "") + (r.stderr or "")
        self.assertEqual(r.returncode, 0, out[-3000:] + "\n--- hub log ---\n" + self.hub.log_text[-3000:])
        self.assertNotIn("cannot find", out)
        self.assertIn("from the hub", out)
        self.assertEqual([c for c in self.git_calls() if c.split(" ")[0] in gitx.NETWORK], [])


def _make_cli(cfg, branch):
    return unittest.skipIf(SKIP, SKIP or "")(type("Cli_%s_%s" % (cfg, branch), (_CliBase, Scenarios2, unittest.TestCase), {"CONFIG": cfg, "BRANCH": branch}))


Cli_strict_v0_main = _make_cli("strict_v0", "main")
Cli_lax_v2_main = _make_cli("lax_v2", "main")
Cli_strict_oldserver_release = _make_cli("strict_oldserver", "release")


if __name__ == "__main__":
    unittest.main()
