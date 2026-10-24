"""Self-update of the hub from git: pull, resolve conflicts, verify, swap, watch, roll back.

The hub runs from a git checkout (the folder that holds `aihub/`). On a schedule (or on demand) the updater:

  1. fetches the update branch into a private ref and compares it with HEAD;
  2. builds a CANDIDATE in a throwaway `git worktree`: the remote tip (fast-forward) or HEAD + preserved local changes + the remote tip
     merged with the admin's conflict policy (remote wins | local wins | abort). The live checkout is not touched until step 4;
  3. verifies the candidate: byte-compiles it, optionally runs the test suite, and BOOTS it (`python -m aihub.server` on a free port, a
     temporary data folder, a copy of the SQLite database so migrations run on real data) until /readyz answers. Any failure discards the
     candidate, remembers the commit as bad and leaves the running hub as it was;
  4. swaps: records `updates/pending.json` (state `probation`), starts the stdlib watchdog `update_guard.py`, `git reset --hard` to the
     candidate and restarts (re-exec, or exit for a supervisor);
  5. the new process watches itself during `update_probation` seconds (probe /readyz, boot-loop counter). Unhealthy -> it resets to the old
     commit and restarts. Dead or hung -> the guard does the same from outside. Healthy -> `confirmed`.

Manual rollback to any commit uses the same path (candidate = that commit, verified, swapped, probation back to the previous version).
Every run keeps a log in `<data>/updates/runs/<id>.json`. Commits that failed are skipped until the branch moves (`update_skip`).
"""
import json
import logging
import os
import random
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

from ..cli import gitx
from .sync import next_delay, parse_schedule

log = logging.getLogger("aihub")
SRC = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))      # the folder that holds aihub/
DEFAULTS = {"update_enabled": "0", "update_schedule": "0 4 * * *", "update_auto_apply": "1", "update_url": "", "update_branch": "",
            "update_conflict": "remote", "update_tests": "0", "update_probation": "60", "update_restart": "exec", "update_pip": "0",
            "update_backup": "1"}
KEYS = tuple(DEFAULTS)
CONFLICTS = ("remote", "local", "abort", "manual")          # "manual" was added later: the first three keep their exact meaning
RESTARTS = ("exec", "exit")
SWITCHES = ("update_enabled", "update_auto_apply", "update_tests", "update_pip", "update_backup")
BRANCH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,99}$")
REV = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./@^~-]{0,99}$")
URL = re.compile(r"^(https?://|ssh://|git@|file://|/)[^\s]{1,300}$")
KEEP_RUNS = 200
BOOT_SLACK = 90            # seconds the guard waits for the new version to start, on top of the probation window
SMOKE_WAIT = 90            # seconds the candidate may take to answer /readyz
MAX_DB_COPY = 500 << 20    # a bigger SQLite database is not copied for the boot test (an empty one is used)
VERSION_RE = re.compile(r'^VERSION\s*=\s*"([^"]+)"', re.M)
DEP_FILES = ("requirements-server.txt", "requirements-all.txt", "pyproject.toml", "setup.py")


STAGES = (("fetch", "Fetch"), ("merge", "Merge"), ("syntax", "Check code"), ("tests", "Tests"), ("boot", "Boot test"), ("backup", "Backup"),
          ("switch", "Switch"), ("watch", "Watch"))
ROLLBACK_STAGES = STAGES[2:]                  # a rollback does not fetch or merge
MARK = 25                                     # conflict marker width used for the visual resolver (a file rarely contains a line like this)
MAX_CONFLICT_FILES = 40
MAX_TEXT = 512 << 10                          # a bigger conflicting file can only be resolved as a whole (current or incoming)


class Failure(Exception):
    """A step failed in a way the admin should read; the live checkout is unchanged (or already put back)."""
    skip = True                               # the commit is remembered as bad (update_skip) and not retried until the branch moves


class Conflict(Failure):
    """The merge needs a person: the conflict is saved for the resolver page, nothing is changed and nothing is marked bad."""
    skip = False


class Stale(Failure):
    """The answer no longer fits (the branch or the local files moved since the conflict was found)."""
    skip = False


class Busy(RuntimeError):
    pass


def check_setting(k, v):
    """Normalized value of an admin setting, or ValueError with the reason. Blank = back to the default."""
    v = str(v).strip()
    if v == "":
        return ""
    if k in SWITCHES:
        if v not in ("0", "1"):
            raise ValueError("%s must be 0 or 1" % k)
    elif k == "update_schedule":
        parse_schedule(v)
    elif k == "update_url":
        if not URL.match(v) or v.startswith("-"):
            raise ValueError("update_url must be an https://, ssh://, git@ or file URL (or an absolute path)")
    elif k == "update_branch":
        if not BRANCH.match(v) or ".." in v or v.endswith((".lock", "/", ".")):
            raise ValueError("update_branch is not a valid branch name")
    elif k == "update_conflict":
        if v not in CONFLICTS:
            raise ValueError("update_conflict must be one of: " + ", ".join(CONFLICTS))
    elif k == "update_restart":
        if v not in RESTARTS:
            raise ValueError("update_restart must be one of: " + ", ".join(RESTARTS))
    elif k == "update_probation":
        if not (v.isdigit() and 10 <= int(v) <= 900):
            raise ValueError("update_probation must be 10 to 900 seconds")
    return v


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def _clean(text, n=600):
    return gitx.redact_url(str(text or "")).strip()[-n:]


def parse_markers(text):
    """Split `git merge-file --diff3` output (marker width MARK) into text and conflict segments; keeps every line ending."""
    o, b, m, e = "<" * MARK, "|" * MARK, "=" * MARK, ">" * MARK

    def is_(line, mark):
        return line.startswith(mark) and (len(line.rstrip("\r\n")) == MARK or line[MARK] == " ")

    segs, buf, mode, cur = [], [], "text", None
    for line in re.findall(r"[^\n]*\n|[^\n]+", text):
        if mode == "text":
            if is_(line, o):
                if buf:
                    segs.append({"t": "text", "text": "".join(buf)})
                    buf = []
                cur, mode = {"t": "conflict", "ours": "", "base": "", "theirs": ""}, "ours"
            else:
                buf.append(line)
        elif mode == "ours":
            if is_(line, b):
                mode = "base"
            elif is_(line, m):
                mode = "theirs"
            else:
                cur["ours"] += line
        elif mode == "base":
            if is_(line, m):
                mode = "theirs"
            else:
                cur["base"] += line
        elif is_(line, e):
            segs.append(cur)
            mode = "text"
        else:
            cur["theirs"] += line
    if mode != "text":
        raise ValueError("unbalanced conflict markers")
    if buf:
        segs.append({"t": "text", "text": "".join(buf)})
    return segs


def assemble(segs, choices):
    """The file text for one choice per conflict block: "ours", "theirs", "both" (ours then theirs) or {"text": ...}."""
    n = sum(1 for s in segs if s["t"] == "conflict")
    if not isinstance(choices, list) or len(choices) != n:
        raise ValueError("expected %d choice(s)" % n)
    out, i = [], 0
    for s in segs:
        if s["t"] == "text":
            out.append(s["text"])
            continue
        c = choices[i]
        i += 1
        if c == "ours":
            out.append(s["ours"])
        elif c == "theirs":
            out.append(s["theirs"])
        elif c == "both":
            out.append(s["ours"] + s["theirs"])
        elif isinstance(c, dict) and isinstance(c.get("text"), str):
            out.append(c["text"])
        else:
            raise ValueError("block %d has no choice" % i)
    return "".join(out)


class Run:
    """One check / update / rollback with its log. Kept on disk only when something worth reading happened (a quiet cron check that finds
    nothing new changes `status` only)."""
    def __init__(self, up, kind, trigger, user=""):
        self.up = up
        self.d = {"id": time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2), "ts": time.time(), "kind": kind, "trigger": trigger,
                  "user": user, "state": "running", "title": "", "from": "", "to": "", "tip": "", "version_from": "", "version_to": "",
                  "finished": None, "log": [],
                  "stages": [{"key": k, "label": l, "state": "pending", "started": None, "finished": None, "detail": ""}
                             for k, l in (ROLLBACK_STAGES if kind == "rollback" else STAGES)]}
        self.keep = trigger != "cron"
        self.merge_note = ""

    @property
    def id(self):
        return self.d["id"]

    def log(self, level, msg):
        self.d["log"].append({"t": time.time(), "l": level, "m": _clean(msg, 1500)})
        log.log(logging.WARNING if level == "error" else logging.INFO, "update %s: %s", self.id, _clean(msg, 300))
        self.save()

    def info(self, msg):
        self.log("info", msg)

    def warn(self, msg):
        self.log("warn", msg)

    def save(self):
        if self.keep:
            _write(os.path.join(self.up.dir, "runs", self.id + ".json"), self.d)

    def set(self, **kw):
        self.d.update(kw)
        self.save()

    # ----- stages (older run files have none: every call is then a no-op)
    def _st(self, key):
        for s in self.d.get("stages") or []:
            if s.get("key") == key:
                return s
        return None

    def begin(self, key, detail=""):
        s = self._st(key)
        if s:
            s.update(state="running", started=time.time(), finished=None, detail=detail or s.get("detail", ""))
            self.save()

    def end(self, key, state="done", detail=""):
        s = self._st(key)
        if s:
            s.update(state=state, finished=time.time())
            s.pop("progress", None)
            if detail:
                s["detail"] = detail
            self.save()

    def skip(self, key, detail=""):
        s = self._st(key)
        if s and s.get("state") in ("pending", "running"):
            s.update(state="skipped", detail=detail or s.get("detail", ""))
            self.save()

    def progress(self, key, done, total):
        s = self._st(key)
        if s:
            s["progress"] = [int(done), int(total)]
            self.save()

    def _settle(self, state, title):
        """Stage states that follow from how the run ended."""
        for s in self.d.get("stages") or []:
            st = s.get("state")
            if state in ("up_to_date", "available", "skipped", "conflict_wait") and st == "pending":
                s["state"] = "skipped"
            elif state in ("failed", "rolled_back", "rollback_failed") and st == "running":
                s.update(state="failed", finished=time.time(), detail=s.get("detail") or _clean(title, 120))
            elif state == "conflict" and st == "running":
                s.update(state="blocked", finished=time.time(), detail=_clean(title, 120))
            elif state == "confirmed" and st == "running":
                s.update(state="done", finished=time.time())
            elif state == "applied" and s.get("key") == "watch" and st == "pending":
                s.update(state="skipped", detail="that version has no updater")
            if s.get("state") not in ("running",):
                s.pop("progress", None)

    def finish(self, state, title="", quiet=False):
        if state != "up_to_date" and not quiet:
            self.keep = True
        self.d.update(state=state, finished=time.time(), title=title or self.d["title"])
        self._settle(state, self.d["title"])
        self.save()
        self.up.prune()

    @classmethod
    def load(cls, up, rid):
        d = _read(os.path.join(up.dir, "runs", os.path.basename(str(rid)) + ".json"))
        if not d:
            return None
        r = cls(up, d.get("kind", "update"), d.get("trigger", ""), d.get("user", ""))
        r.d, r.keep = d, True
        return r


class Updater:
    def __init__(self, app):
        self.app, self.stop, self.wake, self.lock = app, threading.Event(), threading.Event(), threading.Lock()
        self.t = None
        self.root = SRC
        gitx.trust(self.root)                 # the source checkout may belong to another account than the one running the hub
        self.pace = 3                         # seconds between health probes during probation
        self.smoke = True                     # tests that only exercise git turn the boot test off
        self.restart_fn = self._restart
        self.guard_fn = self._spawn_guard
        self.probe_fn = self._probe
        self.status = {"running": None, "last_check": None, "last_result": "", "last_error": None, "available": None}
        self._init_dir()

    # ----- plumbing
    @property
    def repos(self):
        return self.app.state.repos

    @property
    def settings(self):
        return self.app.state.settings

    @property
    def dir(self):
        return os.path.join(self.settings.data_dir, "updates")

    def _init_dir(self):
        os.makedirs(self.dir, exist_ok=True)
        st = _read(os.path.join(self.dir, "status.json")) or {}
        for k in ("last_check", "last_result", "last_error", "available"):
            if k in st:
                self.status[k] = st[k]

    def _save_status(self):
        keep = {k: self.status[k] for k in ("last_check", "last_result", "last_error", "available")}
        try:
            _write(os.path.join(self.dir, "status.json"), keep)
        except OSError:
            pass

    @property
    def pending_path(self):
        return os.path.join(self.dir, "pending.json")

    def git(self, *args, cwd=None, check=True, timeout=None):
        return gitx.run(list(args), cwd=cwd or self.root, check=check, timeout=timeout)

    def cfg(self, k):
        if k == "update_enabled":
            v = self.settings.env_get("AIHUB_UPDATE_ENABLED")
            if v:
                return "1" if v.lower() in ("1", "true", "yes", "on") else "0"
        return self.repos.setting(k, "") or DEFAULTS[k]

    def enabled(self):
        return self.cfg("update_enabled") == "1"

    def available(self):
        """(ok, why not): the hub can update itself only when it runs from a git checkout of its own source."""
        if not os.path.isdir(os.path.join(self.root, "aihub")):
            return False, "the source folder was not found"
        try:
            top = self.git("rev-parse", "--show-toplevel")
        except Exception:
            return False, "the hub does not run from a git checkout (container image, zip or wheel): self-update needs one"
        if os.path.realpath(top) != os.path.realpath(self.root):
            return False, "the hub folder is not the root of its git repository"
        return True, ""

    def source(self):
        """Where updates come from: the admin's URL/branch or the checkout's own origin and upstream branch."""
        url = self.cfg("update_url") or self.git("config", "--get", "remote.origin.url", check=False)
        branch = self.cfg("update_branch")
        if not branch:
            up = self.git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False)
            branch = up.split("/", 1)[1] if "/" in up else ""
            if not branch:
                cur = self.git("rev-parse", "--abbrev-ref", "HEAD", check=False)
                branch = cur if cur and cur != "HEAD" else "main"
        return url, branch

    def head(self):
        return self.git("rev-parse", "HEAD")

    def version_at(self, rev, cwd=None):
        try:
            return (VERSION_RE.search(self.git("show", "%s:aihub/core/release.py" % rev, cwd=cwd)) or [None, ""])[1]
        except Exception:
            return ""

    CODE_DIRS = ("aihub", "tests")             # new files here that are not committed yet are part of the local change (a new module is code)

    def _local_tree(self):
        """The live checkout as it is now, as a git tree: edits to tracked files AND new, not ignored files under CODE_DIRS. Built in a
        scratch copy of the index, so the real index and files are never touched. -> tree id, or "" when it equals HEAD.
        (`git diff HEAD` and `stash create` leave untracked files out: a hub running an uncommitted new module would build a candidate
        without it and the boot test would fail on its import.)"""
        scratch = os.path.join(self.dir, "index-%d-%d.tmp" % (os.getpid(), threading.get_ident()))
        env = {"GIT_INDEX_FILE": scratch}
        os.makedirs(self.dir, exist_ok=True)
        try:
            real = self.git("rev-parse", "--path-format=absolute", "--git-path", "index", check=False)
            if real and os.path.isfile(real):
                shutil.copyfile(real, scratch)                      # keeps the stat cache: only changed files are hashed again
            else:
                gitx.run(["read-tree", "HEAD"], cwd=self.root, extra_env=env)
            gitx.run(["add", "-u", "--", ".", ":(exclude)*.pyc"], cwd=self.root, extra_env=env)
            new = [d for d in self.CODE_DIRS if os.path.isdir(os.path.join(self.root, d))]
            if new:
                gitx.run(["add", "-A", "--"] + new + [":(exclude)*.pyc"], cwd=self.root, extra_env=env)
            tree = gitx.run(["write-tree"], cwd=self.root, extra_env=env)
            return "" if tree == self.git("rev-parse", "HEAD^{tree}") else tree
        finally:
            try:
                os.remove(scratch)
            except OSError:
                pass

    def _save_local_patch(self, run, tree):
        """Keep a copy of the local change (HEAD -> tree) in updates/local-<run>.patch. -> its path."""
        patch = os.path.join(self.dir, "local-%s.patch" % run.id)
        self.git("diff", "--binary", "--output=" + patch, "HEAD", tree)
        return patch

    def dirty(self):
        return bool(self.git("status", "--porcelain", "--untracked-files=no", "--", ".", ":(exclude)*.pyc", check=False))

    # ----- cron loop (same shape as the backup job)
    def start(self):
        self.on_start()
        self.t = threading.Thread(target=self._loop, name="aihub-update", daemon=True)
        self.t.start()

    def close(self):
        self.stop.set()
        self.wake.set()

    def delay(self):
        try:
            return next_delay(parse_schedule(self.cfg("update_schedule")))
        except ValueError:
            return next_delay(parse_schedule(DEFAULTS["update_schedule"]))

    def jittered(self):
        base = self.delay()
        span = min(base * 0.05, 300)
        return max(5.0, base + random.uniform(-span, span))

    def _loop(self):
        while not self.stop.is_set():
            if self.wake.wait(self.jittered()):
                self.wake.clear()
                continue
            if self.enabled():
                try:
                    self.run("update", trigger="cron", apply=self.cfg("update_auto_apply") == "1")
                except Busy:
                    pass
                except Exception as e:
                    log.warning("update failed: %s", _clean(e, 300))
                finally:
                    self.repos.db.release()

    def start_async(self, *a, **kw):
        """Run in a background thread (manual actions: the page polls the log). Raises Busy at once when a run is active."""
        if self.lock.locked():
            raise Busy("an update is already running")
        t = threading.Thread(target=self._bg, args=a, kwargs=kw, name="aihub-update-run", daemon=True)
        t.start()
        return t

    def _bg(self, *a, **kw):
        try:
            self.run(*a, **kw)
        except Busy:
            pass
        except Exception as e:
            log.warning("update failed: %s", _clean(e, 300))
        finally:
            self.repos.db.release()

    # ----- one run
    def run(self, action="update", trigger="manual", user="", apply=True, force=False, target="", resolutions=None):
        """action: update (fetch + build + verify [+ swap]) | rollback (target = commit/tag/branch). apply=False only reports."""
        if not self.lock.acquire(blocking=False):
            raise Busy("an update is already running")
        run = Run(self, action, trigger, user)
        self.status["running"] = run.id
        worktrees = []
        try:
            ok, why = self.available()
            if not ok:
                raise Failure(why)
            if (self.pending() or {}).get("state") == "probation":
                raise Failure("the previous update is still being watched (probation); wait for it to be confirmed or rolled back")
            if action == "rollback":
                self._rollback_to(run, target, worktrees)
            else:
                self._update(run, apply, force, worktrees, resolutions)
        except Conflict as e:
            run.log("warn", str(e))
            self.status.update(last_error=None, last_result="manual resolution needed")
            run.finish("conflict", _clean(e, 200))
            self.repos.audit("updater", "update.conflict", (run.d.get("tip") or "")[:8], _clean(e, 300))
        except Failure as e:
            run.log("error", str(e))
            self.status.update(last_error=_clean(e, 300))
            run.finish("failed", _clean(e, 200))
            if run.d.get("tip") and action != "rollback" and e.skip:
                self.repos.set_setting("update_skip", run.d["tip"])
                run.info("commit %s is skipped until the branch moves (or until you press Update anyway)" % run.d["tip"][:8])
        except Exception as e:
            run.log("error", "unexpected error: %s" % e)
            self.status.update(last_error=_clean(e, 300))
            run.finish("failed", "unexpected error")
            log.exception("update run failed")
        finally:
            for w in worktrees:
                self.drop_worktree(w)
            self.status["running"] = None
            self._save_status()
            self.lock.release()
        if run.d["state"] in ("probation", "applied"):
            self.restart_fn()
        return run.d

    # ----- update
    def _update(self, run, apply, force, worktrees, resolutions=None):
        url, branch = self.source()
        if not url:
            raise Failure("no update source: the checkout has no origin and update_url is empty")
        head = self.head()
        run.set(**{"from": head, "version_from": self.version_at(head)})
        run.info("checking %s (branch %s), running %s" % (gitx.redact_url(url), branch, head[:8]))
        run.begin("fetch")
        try:
            self.git("fetch", "-q", "--no-tags", "--", url, "+refs/heads/%s:refs/aihub/update/tip" % branch)
        except gitx.GitError as e:
            raise Failure("could not fetch the update source: %s" % _clean(e, 300))
        tip = self.git("rev-parse", "refs/aihub/update/tip")
        run.set(tip=tip)
        ahead, behind = [int(x) for x in self.git("rev-list", "--left-right", "--count", "HEAD...%s" % tip).split()]
        self.status.update(last_check=time.time(), last_error=None)
        run.end("fetch", detail="%d new commit(s)" % behind if behind else "nothing new")
        if behind == 0:
            self._clear_conflict()
            self.status.update(last_result="up to date", available=None)
            run.info("already up to date (%d local commit(s) ahead)" % ahead if ahead else "already up to date")
            run.finish("up_to_date", "Up to date")
            return
        pend = self.conflict()
        if pend and resolutions is None and run.d["trigger"] == "cron" and pend.get("tip") == tip:
            run.info("waiting for a person to resolve the conflict (Admin > Updates)")
            run.finish("conflict", "Waiting for manual conflict resolution", quiet=True)           # the schedule does not add a history row every night
            return
        run.keep = True
        run.save()
        subjects = self.git("log", "--format=%h %s", "-n", "10", "HEAD..%s" % tip).splitlines()
        run.info("%d new commit(s) on %s%s" % (behind, branch, ", %d local commit(s) not on the remote" % ahead if ahead else ""))
        for s in subjects:
            run.info("  " + s)
        self.status.update(last_result="%d commit(s) behind" % behind, available={"behind": behind, "ahead": ahead, "tip": tip,
                           "subjects": subjects[:5], "version": self.version_at(tip)})
        if not force and self.repos.setting("update_skip", "") == tip:
            run.info("this commit was tried before and failed: skipped (press Update anyway to retry it)")
            run.finish("skipped", "Skipped: failed before")
            return
        if not apply:
            run.info("automatic apply is off: waiting for an admin to press Update")
            run.finish("available", "%d commit(s) available" % behind)
            return
        run.begin("merge")
        cw = self._candidate(run, head, tip, ahead, worktrees, resolutions)
        run.end("merge", detail=run.merge_note)
        cand = self.git("rev-parse", "HEAD", cwd=cw)
        run.set(to=cand, version_to=self.version_at(cand))
        self._verify(run, cw, head, cand)
        self._swap(run, head, cand, tip)

    def _candidate(self, run, head, tip, ahead, worktrees, resolutions=None):
        if self.cfg("update_conflict") == "manual":
            return self._candidate_manual(run, head, tip, ahead, worktrees, resolutions)
        local = self._local_tree()
        patch = self._save_local_patch(run, local) if local else ""
        has_local = bool(local)
        strategy = self.cfg("update_conflict")
        if not ahead and not has_local:
            run.info("fast-forward: building the candidate from the remote tip")
            run.merge_note = "fast-forward"
            return self._worktree(tip, worktrees)
        cw = self._worktree(head, worktrees)
        ident = ["-c", "user.name=aihub-updater", "-c", "user.email=aihub-updater@localhost"]
        if has_local:
            run.warn("uncommitted local changes found; a copy is saved in %s" % patch)
            try:
                self.git("apply", "--3way", "--index", patch, cwd=cw)
                self.git(*ident, "commit", "-q", "-m", "local changes preserved by the updater", cwd=cw)
                run.info("local changes carried into the candidate")
            except gitx.GitError as e:
                self.git("reset", "-q", "--hard", "HEAD", cwd=cw, check=False)
                if strategy != "remote":
                    raise Failure("local changes do not apply cleanly and the conflict policy is '%s': %s" % (strategy, _clean(e, 200)))
                run.warn("local changes do not apply cleanly: dropped in favour of the remote (policy 'remote'); they stay in the patch file")
        self._merge(run, cw, tip, strategy, ident)
        return cw

    def _merge(self, run, cw, tip, strategy, ident):
        opt = {"remote": ["-X", "theirs"], "local": ["-X", "ours"]}.get(strategy, [])
        run.info("merging the remote tip (conflict policy: %s)" % strategy)
        try:
            self.git(*ident, "merge", "--no-edit", *opt, "-m", "update: merge remote (auto-resolved)", tip, cwd=cw)
            run.info("merged without manual conflicts")
            run.merge_note = "merged cleanly"
            return
        except gitx.GitError as e:
            err = _clean(e, 300)
        paths = self.git("diff", "--name-only", "--diff-filter=U", cwd=cw, check=False).splitlines()
        if strategy == "abort" or not paths:
            self.git("merge", "--abort", cwd=cw, check=False)
            raise Failure("merge conflict%s" % (" in: " + ", ".join(paths[:10]) if paths else ": " + err))
        for p in paths:
            theirs = strategy == "remote" or p.endswith(".pyc")
            side = "--theirs" if theirs else "--ours"
            try:
                self.git("checkout", side, "--", p, cwd=cw)
                self.git("add", "--", p, cwd=cw)
                run.info("conflict in %s: kept the %s version" % (p, "remote" if theirs else "local"))
            except gitx.GitError:
                self.git("rm", "-q", "-f", "--", p, cwd=cw, check=False)       # deleted on the side we chose
                run.info("conflict in %s: the %s side deleted it, so it is deleted" % (p, "remote" if theirs else "local"))
        try:
            self.git(*ident, "commit", "-q", "--no-edit", cwd=cw)
        except gitx.GitError as e:
            raise Failure("could not finish the merge: %s" % _clean(e, 300))
        run.info("%d conflict(s) resolved automatically" % len(paths))
        run.merge_note = "%d conflict(s) auto-resolved" % len(paths)

    # ----- manual conflict resolution ("Ask me" policy)
    @property
    def conflict_path(self):
        return os.path.join(self.dir, "conflict.json")

    def conflict(self):
        """The conflict that waits for a person, or None. It belongs to one running commit: a checkout that moved makes it obsolete."""
        c = _read(self.conflict_path)
        if not c:
            return None
        try:
            if c.get("head") != self.head():
                self._clear_conflict()
                return None
        except Exception:
            return None
        return c

    def _clear_conflict(self):
        try:
            os.remove(self.conflict_path)
        except OSError:
            pass

    def dismiss_conflict(self):
        self._clear_conflict()

    def _raw(self, cwd, *args):
        r = subprocess.run(["git"] + list(args), cwd=cwd, env=gitx._env(None, cwd), capture_output=True, **gitx.popen_kw())
        return r.returncode, r.stdout

    def _local_commit(self, run, head):
        """HEAD, or a commit (parent HEAD) holding the uncommitted edits of the live checkout, made without touching it."""
        tree = self._local_tree()
        if not tree:
            return head
        patch = self._save_local_patch(run, tree)
        run.warn("uncommitted local changes found; a copy is saved in %s" % patch)
        ident = ["-c", "user.name=aihub-updater", "-c", "user.email=aihub-updater@localhost"]
        return self.git(*ident, "commit-tree", tree, "-p", head, "-m", "local changes preserved by the updater")

    def _unmerged(self, cw):
        return [p for p in self.git("diff", "--name-only", "--diff-filter=U", "-z", cwd=cw, check=False).split("\0") if p]

    def _candidate_manual(self, run, head, tip, ahead, worktrees, resolutions):
        ident = ["-c", "user.name=aihub-updater", "-c", "user.email=aihub-updater@localhost"]
        local = self._local_commit(run, head)
        tree = self.git("rev-parse", local + "^{tree}")
        if resolutions is not None:
            saved = self.conflict()
            if not saved or saved.get("tip") != tip or saved.get("local_tree") != tree:
                raise Stale("the branch or the local files changed since the conflict was found: press Update now to look again")
        if local == head and not ahead:
            run.info("fast-forward: building the candidate from the remote tip")
            run.merge_note = "fast-forward"
            self._clear_conflict()
            return self._worktree(tip, worktrees)
        cw = self._worktree(local, worktrees)
        run.info("merging the remote tip (conflict policy: ask me)")
        try:
            self.git(*ident, "merge", "--no-edit", "-m", "update: merge remote", tip, cwd=cw)
            run.info("merged without conflicts")
            run.merge_note = "merged cleanly"
            self._clear_conflict()
            return cw
        except gitx.GitError as e:
            err = _clean(e, 300)
        paths = self._unmerged(cw)
        if not paths:
            self.git("merge", "--abort", cwd=cw, check=False)
            raise Failure("merge failed: %s" % err)
        for p in [x for x in paths if x.endswith(".pyc")]:             # compiled files are never worth a person's time
            try:
                self.git("checkout", "--theirs", "--", p, cwd=cw)
                self.git("add", "--", p, cwd=cw)
            except gitx.GitError:
                self.git("rm", "-q", "-f", "--", p, cwd=cw, check=False)
        paths = [p for p in paths if not p.endswith(".pyc")]
        if paths and resolutions is None:
            if len(paths) > MAX_CONFLICT_FILES:
                self.git("merge", "--abort", cwd=cw, check=False)
                raise Failure("%d files conflict: too many for the page, merge by hand in git" % len(paths))
            files = self._collect(cw, paths)
            _write(self.conflict_path, {"run": run.id, "ts": time.time(), "head": head, "tip": tip, "local_tree": tree,
                                        "version": self.version_at(tip), "files": files})
            self.git("merge", "--abort", cwd=cw, check=False)
            for f in files:
                run.warn("conflict in %s (%s%s)" % (f["path"], f["kind"], ", %d block(s)" % f["conflicts"] if f["kind"] == "text" else ""))
            raise Conflict("Manual update needed: %d file(s) conflict with local changes. The hub keeps running the current version. "
                           "Open Admin > Updates and resolve them." % len(files))
        if paths:
            self._resolve(run, cw, paths, resolutions)
        try:
            self.git(*ident, "commit", "-q", "--no-edit", cwd=cw)
        except gitx.GitError as e:
            raise Failure("could not finish the merge: %s" % _clean(e, 300))
        run.merge_note = "%d file(s) resolved by %s" % (len(paths), run.d.get("user") or "an admin")
        self._clear_conflict()
        return cw

    def _collect(self, cw, paths):
        out = []
        for p in paths:
            got = {}
            for n in (1, 2, 3):
                rc, data = self._raw(cw, "show", ":%d:%s" % (n, p))
                got[n] = data if rc == 0 else None
            base, ours, theirs = got[1], got[2], got[3]
            f = {"path": p, "kind": "text", "note": "", "conflicts": 0}
            if ours is None or theirs is None:
                f.update(kind="delete", note="deleted here, changed in the update" if ours is None else "changed here, deleted by the update")
            else:
                segs = None
                if b"\0" not in ours + theirs and len(ours) <= MAX_TEXT and len(theirs) <= MAX_TEXT:
                    segs = self._segments(ours, base or b"", theirs)
                if segs is None:
                    f.update(kind="binary", note="binary or very large: keep one side as a whole")
                else:
                    f.update(segments=segs, conflicts=sum(1 for x in segs if x["t"] == "conflict"))
            out.append(f)
        return out

    def _segments(self, ours, base, theirs):
        """[{"t": "text", "text"} | {"t": "conflict", "ours", "base", "theirs"}] from a three-way merge of the stage files, or None."""
        with tempfile.TemporaryDirectory(dir=self.dir) as t:
            names = []
            for n, data in (("ours", ours), ("base", base), ("theirs", theirs)):
                with open(os.path.join(t, n), "wb") as f:
                    f.write(data)
                names.append(os.path.join(t, n))
            rc, out = self._raw(t, "merge-file", "-p", "--diff3", "--marker-size=%d" % MARK, "-L", "ours", "-L", "base", "-L", "theirs", *names)
        if rc < 0 or rc > 127:
            return None
        try:
            return parse_markers(out.decode("utf-8"))
        except UnicodeDecodeError:
            return None

    def _resolve(self, run, cw, paths, resolutions):
        files = {f["path"]: f for f in self._collect(cw, paths)}
        if not isinstance(resolutions, dict) or set(resolutions) != set(files):
            raise Stale("the conflicting files are not the ones you resolved: press Update now to look again")
        for p, f in files.items():
            res = resolutions.get(p)
            mode = res.get("mode") if isinstance(res, dict) else None
            if mode in ("ours", "theirs"):
                try:
                    self.git("checkout", "--" + mode, "--", p, cwd=cw)
                    self.git("add", "--", p, cwd=cw)
                except gitx.GitError:
                    self.git("rm", "-q", "-f", "--", p, cwd=cw, check=False)          # that side deleted the file
            elif mode in ("hunks", "custom") and f["kind"] == "text":
                try:
                    text = res["text"] if mode == "custom" else assemble(f["segments"], res.get("choices"))
                except (ValueError, KeyError, TypeError) as e:
                    raise Stale("the answer for %s does not fit: %s" % (p, e))
                if not isinstance(text, str):
                    raise Stale("the answer for %s is not text" % p)
                with open(os.path.join(cw, p), "wb") as fh:
                    fh.write(text.encode("utf-8"))
                self.git("add", "--", p, cwd=cw)
            else:
                raise Stale("no valid answer for %s" % p)
        run.info("%d conflicting file(s) resolved with your choices" % len(files))

    def _worktree(self, rev, worktrees):
        path = tempfile.mkdtemp(prefix="cand-", dir=self.dir)
        self.git("worktree", "add", "-q", "--detach", "--force", path, rev)
        worktrees.append(path)
        return path

    def drop_worktree(self, path):
        self.git("worktree", "remove", "--force", path, check=False)
        if os.path.exists(path):
            gitx.rmtree(path)
        self.git("worktree", "prune", check=False)

    # ----- verification
    def _verify(self, run, cw, head, cand):
        run.info("verifying candidate %s (version %s)" % (cand[:8], run.d["version_to"] or "?"))
        run.begin("syntax")
        r = subprocess.run([sys.executable, "-m", "compileall", "-q", "-f", "aihub"], cwd=cw, capture_output=True, text=True, errors="replace",
                           env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), **gitx.popen_kw())
        if r.returncode != 0:
            raise Failure("syntax check failed: %s" % _clean(r.stdout + r.stderr, 500))
        run.info("syntax check passed")
        changed = [f for f in self.git("diff", "--name-only", head, cand, check=False).splitlines() if f in DEP_FILES]
        if changed:
            if self.cfg("update_pip") == "1":
                self._pip(run, cw)
            else:
                run.warn("dependency files changed (%s): install them yourself, or the boot test fails if one is missing "
                         "(or turn on 'Install dependencies')" % ", ".join(changed))
        run.end("syntax")
        if self.cfg("update_tests") == "1":
            run.begin("tests")
            self._tests(run, cw)
            run.end("tests")
        else:
            run.skip("tests", "off")
        if self.smoke:
            run.begin("boot")
            self._boot(run, cw)
            run.end("boot")
        else:
            run.warn("boot test skipped")
            run.skip("boot", "off")

    def _pip(self, run, cw):
        req = "requirements-all.txt" if os.path.isfile(os.path.join(cw, "requirements-all.txt")) else "requirements-server.txt"
        run.info("installing dependencies from %s" % req)
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", req], cwd=cw, capture_output=True, text=True, errors="replace",
                           timeout=900, **gitx.popen_kw())
        if r.returncode != 0:
            raise Failure("pip install failed: %s" % _clean(r.stdout + r.stderr, 500))

    def _tests(self, run, cw):
        run.info("running the test suite (this takes a few minutes)")
        try:
            r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."], cwd=cw, capture_output=True, text=True,
                               errors="replace", timeout=900, env=self._clean_env(cw), **gitx.popen_kw())
        except subprocess.TimeoutExpired:
            raise Failure("the test suite did not finish in 15 minutes")
        if r.returncode != 0:
            raise Failure("tests failed: %s" % _clean(r.stderr or r.stdout, 600))
        run.info("tests passed")

    @staticmethod
    def _clean_env(cw, **extra):
        """No AIHUB_* from the real deployment (a database URL, tokens) reaches the candidate."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("AIHUB_")}
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=cw, **extra)
        return env

    def _boot(self, run, cw):
        """Start the candidate for real: own port, own data folder, a copy of the live SQLite database."""
        s = self.settings
        with tempfile.TemporaryDirectory(dir=self.dir) as tmp:
            note = "empty test database"
            src = s.sqlite_file if s.db_backend == "sqlite" else ""
            if src and os.path.isfile(src) and os.path.getsize(src) <= MAX_DB_COPY:
                a, b = sqlite3.connect(src), sqlite3.connect(os.path.join(tmp, "aihub.db"))
                try:
                    a.backup(b)
                finally:
                    b.close()
                    a.close()
                note = "copy of the live database"
            envf = os.path.join(tmp, "empty.env")
            open(envf, "w").close()
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()
            env = self._clean_env(cw, AIHUB_SYNC_ENABLED="0", AIHUB_DB_BACKEND="sqlite", AIHUB_CACHE_BACKEND="memory",
                                  AIHUB_STORAGE_BACKEND="local", AIHUB_LOG_LEVEL="WARNING", AIHUB_ENV_FILE=envf)
            out = open(os.path.join(tmp, "boot.log"), "w+")
            run.info("boot test on port %d with a %s" % (port, note))
            p = subprocess.Popen([sys.executable, "-m", "aihub.server", "--host", "127.0.0.1", "--port", str(port), "--data", tmp,
                                  "--recover", "skip", "--env-file", envf], cwd=cw, env=env, stdout=out, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, **gitx.popen_kw())
            try:
                t_start = time.time()
                end, last = t_start + SMOKE_WAIT, ""
                while time.time() < end:
                    run.progress("boot", time.time() - t_start, SMOKE_WAIT)
                    if p.poll() is not None:
                        out.seek(0)
                        raise Failure("the candidate exited with code %s on start: %s" % (p.returncode, _clean(out.read(), 700)))
                    try:
                        code, body = self._get("http://127.0.0.1:%d/api/v1/readyz" % port)
                        if code == 200 and b'"ok"' in body:
                            code2, _ = self._get("http://127.0.0.1:%d/" % port)
                            if code2 != 200:
                                raise Failure("the candidate answers /readyz but the web page returned %s" % code2)
                            run.info("boot test passed: /readyz ok, web page served")
                            return
                        last = "readyz returned %s" % code
                    except (OSError, ValueError) as e:
                        last = str(e)
                    time.sleep(1)
                out.seek(0)
                raise Failure("the candidate did not become ready in %ds (%s): %s" % (SMOKE_WAIT, last, _clean(out.read(), 500)))
            finally:
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(10)
                    except subprocess.TimeoutExpired:
                        p.kill()
                out.close()

    @staticmethod
    def _get(url, timeout=5):
        ctx = ssl._create_unverified_context() if url.startswith("https") else None          # loopback to ourselves only
        try:
            with urllib.request.urlopen(url, timeout=timeout, context=ctx) as r:
                return r.status, r.read(2000)
        except urllib.error.HTTPError as e:
            return e.code, b""

    # ----- swap + probation
    def pending(self):
        return _read(self.pending_path)

    def _swap(self, run, head, cand, tip):
        bk = self.app.state.backup
        if self.cfg("update_backup") == "1" and bk.enabled():
            run.info("backing up the database first")
            run.begin("backup")
            try:
                self.app.state.ingest.flush()
                bk.run_once()
                run.info("backup done")
                run.end("backup")
            except Exception as e:
                run.warn("backup before the update failed (continuing): %s" % _clean(e, 200))
                run.end("backup", "warn", "failed, continued")
        else:
            run.skip("backup", "off")
        for name, sha in (("from", head), ("to", cand)):
            self.git("update-ref", "refs/aihub/updates/%s-%s" % (name, run.id), sha)      # keeps both commits from gc, and rollback can name them
        mode = self.cfg("update_restart")
        if not self.can_watch(cand):
            # an older version has no watcher: nothing would ever confirm it, and the guard would undo the rollback
            run.warn("%s predates the self-update feature, so it cannot watch itself: switching without probation" % cand[:8])
            run.begin("switch")
            self.git("reset", "-q", "--hard", cand)
            run.end("switch")
            run.finish("applied", "Switched to %s (no probation: that version has no updater)" % (run.d["version_to"] or cand[:8]))
            return
        window = int(self.cfg("update_probation"))
        argv = list(getattr(sys, "orig_argv", None) or [sys.executable] + sys.argv)
        argv[0] = sys.executable
        pend = {"run": run.id, "from": head, "to": cand, "tip": tip, "state": "probation", "repo": self.root, "pid": os.getpid(),
                "registered": False, "boots": 0, "probation": window, "deadline": time.time() + window + BOOT_SLACK, "mode": mode,
                "spawn": mode == "exec", "argv": argv, "cwd": os.getcwd(), "kind": run.d["kind"]}
        _write(self.pending_path, pend)
        try:
            self.guard_fn(pend)
        except Exception as e:
            run.warn("watchdog could not start (%s): only the in-process rollback is active" % _clean(e, 200))
        run.info("switching the checkout to %s and restarting (%s)" % (cand[:8], "re-exec" if mode == "exec" else "exit for the supervisor"))
        run.begin("switch")
        try:
            self.git("reset", "-q", "--hard", cand)
        except gitx.GitError as e:
            try:
                os.remove(self.pending_path)
            except OSError:
                pass
            raise Failure("could not switch the checkout: %s" % _clean(e, 300))
        run.end("switch")
        run.begin("watch", "starting")
        run.finish("probation", "Applied %s: watching the new version" % (run.d["version_to"] or cand[:8]))

    def can_watch(self, rev):
        try:
            self.git("cat-file", "-e", "%s:aihub/server/updater.py" % rev)
            return True
        except gitx.GitError:
            return False

    def _spawn_guard(self, pend):
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "update_guard.py")
        dst = os.path.join(self.dir, "guard.py")
        shutil.copyfile(src, dst)                                      # always from the version that is known to work
        out = open(os.path.join(self.dir, "guard.log"), "ab")
        kw = {"creationflags": 0x00000008 | 0x00000200} if os.name == "nt" else {"start_new_session": True}
        subprocess.Popen([sys.executable, dst, self.pending_path], stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kw)

    def _restart(self):
        """Flush what is in memory, then replace this process (exec) or stop it for a supervisor (exit)."""
        time.sleep(1.5)                                                # let a pending HTTP answer leave
        st = self.app.state
        try:
            st.ingest.flush()
            st.events.close()
        except Exception:
            pass
        if (self.pending() or {}).get("mode", self.cfg("update_restart")) == "exit":
            os.kill(os.getpid(), signal.SIGTERM)
            return
        argv = list(getattr(sys, "orig_argv", None) or [sys.executable] + sys.argv)
        os.chdir((self.pending() or {}).get("cwd") or os.getcwd())
        os.execv(sys.executable, [sys.executable] + argv[1:])

    # ----- after a restart
    def on_start(self):
        """Called as the server starts: watch a fresh update, or close the book on a rollback somebody else did."""
        st = self.pending()
        if not st:
            return
        try:
            if st.get("state") == "probation":
                if self.head() != st.get("to"):
                    log.warning("update: stale probation record (HEAD is not %s): ignored", str(st.get("to"))[:8])
                    os.remove(self.pending_path)
                    return
                st.update(pid=os.getpid(), registered=True, boots=int(st.get("boots", 0)) + 1)
                _write(self.pending_path, st)
                threading.Thread(target=self._watch, args=(st,), name="aihub-update-watch", daemon=True).start()
            elif st.get("state") in ("rolled_back", "rollback_failed"):
                self._finalize(st)
        except Exception as e:
            log.warning("update: could not read the previous update state: %s", _clean(e, 300))

    def _finalize(self, st):
        run = Run.load(self, st.get("run"))
        failed = st.get("state") == "rollback_failed"
        if run:
            run.log("error" if failed else "warn", st.get("reason") or "rolled back")
            if not failed:
                run.log("info", "back on %s (%s)" % ((st.get("from") or "")[:8], self.version_at("HEAD") or "?"))
            run.finish("rollback_failed" if failed else "rolled_back", "Rolled back: " + _clean(st.get("reason"), 160))
        if st.get("tip") and st.get("kind") != "rollback":
            self.repos.set_setting("update_skip", st["tip"])
        self.status.update(last_error=_clean(st.get("reason"), 300))
        self._save_status()
        self.repos.audit("updater", "update.rolled_back", (st.get("to") or "")[:8], _clean(st.get("reason"), 300))
        try:
            os.remove(self.pending_path)
        except OSError:
            pass

    def _probe(self):
        """True when this server answers like a healthy one."""
        b = getattr(self.app.state, "bound", None)
        if b:
            code, body = self._get("%s://%s:%d/api/v1/readyz" % b[:3], timeout=4)
            return code == 200 and b'"ok"' in body
        self.repos.db.conn().execute("SELECT 1").fetchone()
        self.repos.db.release()
        return True

    def _watch(self, st):
        run = Run.load(self, st.get("run"))
        say = run.log if run else (lambda *_: None)
        say("info", "new version started (boot %d); watching it for %d s" % (st["boots"], st["probation"]))
        if st["boots"] >= 3:
            return self._rollback_self(st, run, "the new version restarted %d times during probation (crash loop)" % st["boots"])
        t0, ok, bad = time.time(), 0, 0
        time.sleep(self.pace)
        while time.time() - t0 < st["probation"] and not self.stop.is_set():
            try:
                good = self.probe_fn()
            except Exception as e:
                good, why = False, str(e)
            else:
                why = "not ready"
            if good:
                ok, bad = ok + 1, 0
            else:
                bad += 1
                say("warn", "health check failed (%d in a row): %s" % (bad, _clean(why, 120)))
                if bad >= 3:
                    return self._rollback_self(st, run, "the new version failed 3 health checks in a row")
            if run:
                run.progress("watch", time.time() - t0, st["probation"])
            self.stop.wait(self.pace)
        if self.stop.is_set() or not ok:
            if not ok and not self.stop.is_set():
                self._rollback_self(st, run, "the new version never answered a health check")
            return
        say("info", "healthy for %d s (%d checks): update confirmed" % (st["probation"], ok))
        if run:
            run.finish("confirmed", "Updated to %s" % (run.d.get("version_to") or st["to"][:8]))
        self.status.update(last_error=None, last_result="updated to %s" % (run.d.get("version_to") if run else st["to"][:8]), available=None)
        self._save_status()
        self.repos.audit("updater", "update.confirmed", st["to"][:8], st.get("run", ""))
        try:
            os.remove(self.pending_path)
        except OSError:
            pass

    def _rollback_self(self, st, run, reason):
        """The new version found itself unhealthy: put the old commit back and restart onto it."""
        cur = self.pending() or {}
        if cur.get("state") != "probation":
            return
        try:
            self.git("reset", "-q", "--hard", st["from"])
        except Exception as e:
            st.update(state="rollback_failed", reason="%s; and the reset to the old commit failed: %s" % (reason, _clean(e, 200)))
            _write(self.pending_path, st)
            return
        st.update(state="rolled_back", reason=reason)
        if run:
            run.log("error", reason + ": rolling back to " + st["from"][:8])
        self._finalize(st)                  # closes the run, marks the commit as bad and removes pending.json (the guard stands down)
        self.restart_fn()

    # ----- rollback + lists
    def resolve(self, rev):
        rev = str(rev or "").strip()
        if not REV.match(rev):
            raise Failure("that is not a commit, tag or branch name")
        try:
            return self.git("rev-parse", "--verify", "--quiet", rev + "^{commit}")
        except gitx.GitError:
            raise Failure("commit %s not found in this checkout" % rev)

    def _rollback_to(self, run, target, worktrees):
        to = self.resolve(target)
        head = self.head()
        run.set(**{"from": head, "to": to, "version_from": self.version_at(head), "version_to": self.version_at(to)})
        if to == head:
            raise Failure("the hub is already on %s" % to[:8])
        run.info("rolling back from %s to %s (%s)" % (head[:8], to[:8], self.git("log", "-1", "--format=%s", to)))
        local = self._local_tree()
        if local:
            patch = self._save_local_patch(run, local)
            run.warn("uncommitted local changes will be discarded by the rollback; a copy is saved in %s" % patch)
        cw = self._worktree(to, worktrees)
        self._verify(run, cw, head, to)
        tip = ""
        try:
            url, branch = self.source()
            self.git("fetch", "-q", "--no-tags", "--", url, "+refs/heads/%s:refs/aihub/update/tip" % branch)
            tip = self.git("rev-parse", "refs/aihub/update/tip")
        except Exception:
            pass
        if tip and tip != to:
            self.repos.set_setting("update_skip", tip)
            run.info("automatic updates will not re-apply %s; they resume when the branch moves" % tip[:8])
        self._swap(run, head, to, "")

    def versions(self, limit=40):
        ok, _ = self.available()
        if not ok:
            return []
        head = self.head()
        marks = {}
        for r in self.runs(KEEP_RUNS):
            if r.get("to") and r.get("state") in ("confirmed", "probation"):
                marks.setdefault(r["to"], {"version": r.get("version_to"), "run": r["id"]})
        out, seen = [], set()
        for line in self.git("log", "-n", str(limit), "--format=%H%x1f%h%x1f%s%x1f%ct%x1f%an", check=False).splitlines():
            h, short, subject, ts, author = (line.split("\x1f") + [""] * 5)[:5]
            seen.add(h)
            out.append({"sha": h, "short": short, "subject": subject, "ts": int(ts or 0), "author": author, "current": h == head,
                        "deployed": marks.get(h)})
        for h, m in marks.items():                              # deployed commits that are no longer in HEAD's history
            if h not in seen:
                try:
                    subject, ts = self.git("log", "-1", "--format=%s%x1f%ct", h).split("\x1f")
                    out.append({"sha": h, "short": h[:8], "subject": subject, "ts": int(ts), "author": "", "current": False, "deployed": m})
                except Exception:
                    pass
        return out

    def runs(self, limit=50):
        d = os.path.join(self.dir, "runs")
        try:
            names = sorted((f for f in os.listdir(d) if f.endswith(".json")), reverse=True)[:limit]
        except OSError:
            return []
        out = []
        for f in names:
            r = _read(os.path.join(d, f))
            if r:
                out.append({k: v for k, v in r.items() if k != "log"})
        return out

    def prune(self):
        d = os.path.join(self.dir, "runs")
        try:
            names = sorted(f for f in os.listdir(d) if f.endswith(".json"))
        except OSError:
            return
        for f in names[:-KEEP_RUNS]:
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass

    def view(self):
        ok, why = self.available()
        out = {"git": ok, "why": why, "status": dict(self.status, next_in=round(self.delay())), "pending": None}
        p = self.pending()
        if p:
            out["pending"] = {k: p.get(k) for k in ("run", "from", "to", "state", "deadline", "boots", "probation", "reason")}
        c = self.conflict() if ok else None
        out["conflict"] = None if not c else {"run": c.get("run"), "ts": c.get("ts"), "tip": c.get("tip"), "version": c.get("version"),
                                              "files": [{k: f.get(k) for k in ("path", "kind", "note", "conflicts")} for f in c.get("files", [])]}
        if ok:
            url, branch = self.source()
            out["source"] = {"url": gitx.redact_url(url), "branch": branch, "path": self.root, "head": self.head(),
                             "short": self.head()[:8], "dirty": self.dirty(), "skip": self.repos.setting("update_skip", "")}
        return out
