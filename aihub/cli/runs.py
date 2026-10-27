"""`aihub run`: bots and services (package types bot / service) as supervised processes, in the background by default.

Each run has a small supervisor process (`aihub run _supervise ID`, detached) that starts the bot, writes its output to a log, restarts it
by policy (no | on-failure | always, backoff 2 s doubling to 5 min) or on a schedule (seconds / 5m / 2h / cron), and obeys control requests.
`--fg` runs the same supervisor in this terminal (stdin attached, output shown and logged).

Files in ~/.aihub/runs/:  ID.json  spec + live state (only the live supervisor writes it; `beat` = its heartbeat)
                          ID.log   output of every start (rotated at LOG_MAX)
                          ID.ctl   control requests, one JSON line each (start, stop, restart, quit, set); read every half second
When the CLI is signed in to a hub, the supervisor also reports to `POST /runs/report` (state + new log lines) and takes the commands people
queued on the hub's Running page from the reply. Off with `aihub config set run_report off`, AIHUB_RUN_REPORT=off or `--no-hub`.
The supervisor outlives `aihub upgrade` (which swaps aihub.pyz), so every module it needs is imported here at the top, never lazily.
"""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time

from ..core.release import VERSION
from ..core.schedule import next_delay, parse_schedule
from ..core import manifest as M
from . import api, hooks, installer, paths, telemetry, ui

RESTARTS = ("no", "on-failure", "always")
ACTIONS = ("start", "stop", "restart", "quit", "set")
BEAT = 2                    # the supervisor writes its state at least this often
STALE = 10                  # ... and is gone when its heartbeat is older than this
REPORT = 15                 # seconds between hub reports; the hub answers with a shorter `next` while someone watches the run
LOG_MAX = 5 * 1024 * 1024
TAIL = 400                  # log lines kept in memory for the hub; at most this many per report
LINE_MAX = 2000
BACKOFF_MIN, BACKOFF_MAX, STEADY = 2, 300, 60   # a start that lived STEADY seconds resets the backoff
STOP_WAIT = 10
LINGER = 600                # a finished run (no restart, no schedule) keeps its supervisor this long when the hub can restart it
SCHED_LO, SCHED_HI = 10, 7 * 86400
WINDOWS = os.name == "nt"


def _dir():
    return paths.ensure("runs")


def _f(rid, ext):
    return os.path.join(_dir(), rid + ext)


def rid_of(name):
    r = re.sub(r"[^a-z0-9_.-]+", "-", str(name).strip().lower()).strip("-.")[:60]
    if not r or r.startswith("_"):
        raise api.ApiError("bad run name: %r (letters, digits, - _ .)" % name)
    return r


def load(rid):
    try:
        with open(_f(rid, ".json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save(r):
    tmp = _f(r["id"], ".json.%d.tmp" % os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(r, f, indent=1)
    os.replace(tmp, _f(r["id"], ".json"))


def all_runs():
    out = []
    for n in sorted(os.listdir(_dir())):
        if n.endswith(".json"):
            r = load(n[:-5])
            if r and r.get("id") == n[:-5]:
                out.append(r)
    return out


def alive(r):
    return bool(r and r.get("sup_pid") and time.time() - r.get("beat", 0) < STALE)


def status_of(r):
    """What `list` shows: the supervisor's own status while it lives, else how it ended."""
    if alive(r):
        return r.get("status") or "starting"
    s = r.get("status") or "stopped"
    return s if s in ("stopped", "exited", "failed") else "dead"


def check_schedule(text):
    if text:
        parse_schedule(text, SCHED_LO, SCHED_HI)
    return text or ""


def check_args(args):
    args = [str(x) for x in (args or [])]
    if len(args) > 40 or any(len(x) > 2000 for x in args):
        raise ValueError("too many or too long arguments")
    return args


def hub_on():
    """Report runs to the hub: a hub is configured, the CLI is signed in, and nobody turned it off."""
    if (os.environ.get("AIHUB_RUN_REPORT") or paths.load("config.json", {}).get("run_report") or "on").lower() in ("off", "0", "false", "no"):
        return False
    return paths.hub_configured() and bool(paths.load("credentials.json", {}).get("token"))


def control(rid, action, **data):
    """Queue a request for the live supervisor (one JSON line, appended: several writers never clobber each other)."""
    line = json.dumps(dict(data, action=action, ts=time.time())) + "\n"
    with open(_f(rid, ".ctl"), "a", encoding="utf-8") as f:
        f.write(line)


def _take_controls(rid):
    p = _f(rid, ".ctl")
    if not os.path.exists(p):
        return []
    tmp = p + ".taking"
    try:
        os.replace(p, tmp)
        with open(tmp, encoding="utf-8") as f:
            text = f.read()
        os.remove(tmp)
    except OSError:
        return []
    out = []
    for ln in text.splitlines():
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


def _kill_tree(pid, force=False, group=True):
    try:
        if WINDOWS:
            subprocess.call(["taskkill", "/PID", str(pid), "/T"] + (["/F"] if force else []), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=0x08000000)
        elif group:
            os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass


# ------------------------------------------------------------------------------------------------ supervisor

class Supervisor:
    def __init__(self, rid, fg=False):
        self.rid, self.fg = rid, fg
        self.r = load(rid)
        if not self.r:
            raise api.ApiError("no run named %s" % rid)
        self.child = self.reader = self.logf = None
        self.quit = False
        self.paused = bool(self.r.get("paused"))
        self.expected = None            # "stop" / "restart" while WE end the child: not a failure
        self.backoff = BACKOFF_MIN
        self.due = None
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.lines, self.seq, self.sent = [], 0, 0
        self.hub_cmds, self.done = [], list(self.r.get("hub_done") or [])     # tokens of applied hub commands (acked in every report)
        self.hub = bool(self.r.get("hub")) and hub_on()
        self.report_now = threading.Event()
        self.saved = 0

    # --- log
    def _emit(self, data):
        """Bytes of output: to the log file, the terminal (--fg) and the hub ring."""
        with self.lock:
            if self.logf:
                try:
                    self.logf.write(data)
                    self.logf.flush()
                except (OSError, ValueError):
                    pass
            text = data.decode("utf-8", "replace")
            parts = (getattr(self, "_partial", "") + text).split("\n")
            self._partial = parts.pop()[-LINE_MAX:]
            for ln in parts:
                self.seq += 1
                self.lines.append((self.seq, time.time(), ln.rstrip("\r")[:LINE_MAX]))
            del self.lines[:-TAIL]
        if self.fg:
            try:
                sys.stdout.buffer.write(data)
                sys.stdout.flush()
            except (OSError, ValueError, AttributeError):
                pass

    def note(self, msg):
        self._emit(("[aihub %s] %s\n" % (time.strftime("%H:%M:%S"), msg)).encode())

    def _open_log(self):
        p = _f(self.rid, ".log")
        try:
            if os.path.getsize(p) > LOG_MAX:
                os.replace(p, p + ".1")
        except OSError:
            pass
        with self.lock:
            if self.logf:
                self.logf.close()
            self.logf = open(p, "ab")

    def _read(self, pipe):
        fd = pipe.fileno()
        while True:
            try:
                b = os.read(fd, 65536)
            except OSError:
                break
            if not b:
                break
            self._emit(b)
        try:
            pipe.close()
        except OSError:
            pass

    # --- state
    def save(self, force=False):
        now = time.time()
        if not force and now - self.saved < BEAT:
            return
        self.saved = now
        with self.lock:
            self.r.update(beat=now, sup_pid=os.getpid() if not self.quit else None, paused=self.paused,
                          next_at=self.due if self.child is None else None, hub_done=self.done[-50:])
            if self.quit:
                self.r["beat"] = 0
            _save(self.r)

    def _sched(self):
        s = self.r.get("schedule")
        try:
            return parse_schedule(s, SCHED_LO, SCHED_HI) if s else None
        except ValueError:
            return None

    def _set(self, status, **kw):
        self.r["status"] = status
        self.r.update(kw)

    # --- child
    def _start(self):
        now = time.time()
        try:
            cmd, shell, cwd, env = installer.bot_command(self.r["bot"], self.r.get("args") or [])
        except Exception as e:
            self.note("cannot start: %s" % e)
            self.due = None
            self._set("failed", error=str(e)[:500], last_end=now)
            return
        env = dict(env, PYTHONUNBUFFERED="1", AIHUB_RUN_ID=self.rid, AIHUB_RUN_NAME=self.r.get("name") or self.rid)
        self._open_log()
        kw = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=cwd, env=env, shell=shell)
        if not self.fg:
            kw["stdin"] = subprocess.DEVNULL
        if WINDOWS:
            kw["creationflags"] = 0x00000200 | (0 if self.fg else 0x08000000)      # NEW_PROCESS_GROUP (+ CREATE_NO_WINDOW)
        elif not self.fg:
            kw["start_new_session"] = True      # its own process group: stop ends the whole tree. --fg keeps it in ours so Ctrl-C reaches it
        try:
            self.child = subprocess.Popen(cmd, **kw)
        except OSError as e:
            self.note("cannot start: %s" % e)
            self.due = None
            self._set("failed", error=str(e)[:500], last_end=now)
            return
        self.reader = threading.Thread(target=self._read, args=(self.child.stdout,), daemon=True)
        self.reader.start()
        self.due = None
        self._set("running", pid=self.child.pid, last_start=now, runs=int(self.r.get("runs") or 0) + 1, error="")
        self.note("started %s (pid %d)%s" % (self.r["bot"], self.child.pid, " " + " ".join(self.r.get("args") or []) if self.r.get("args") else ""))
        self.report_now.set()

    def _stop_child(self, why="stop"):
        c = self.child
        if not c or c.poll() is not None:
            return
        self.expected = why
        group = not self.fg
        _kill_tree(c.pid, group=group)
        try:
            c.wait(STOP_WAIT)
        except subprocess.TimeoutExpired:
            self.note("did not stop in %d s: killing it" % STOP_WAIT)
            _kill_tree(c.pid, force=True, group=group)
            try:
                c.wait(5)
            except subprocess.TimeoutExpired:
                pass
        self._reap()

    def _reap(self):
        c = self.child
        code = c.poll()
        if code is None:
            return
        if self.reader:
            self.reader.join(2)
        now = time.time()
        lived = now - (self.r.get("last_start") or now)
        self.child = self.reader = None
        why, self.expected = self.expected, None
        self._set(self.r["status"], pid=None, last_exit=code, last_end=now)
        self.note("exited with code %s after %s" % (code, _dur(lived)))
        if why:
            self._set("stopped" if why == "stop" else "starting")
            self.due = time.time() if why == "restart" else None
            return
        if lived >= STEADY:
            self.backoff = BACKOFF_MIN
        sched, pol = self._sched(), self.r.get("restart") or "no"
        if sched:
            self.due = now + next_delay(sched, now)
            self._set("waiting")
        elif pol == "always" or (pol == "on-failure" and code != 0):
            self.due = now + self.backoff
            self.note("restarting in %s (restart=%s)" % (_dur(self.backoff), pol))
            self.backoff = min(self.backoff * 2, BACKOFF_MAX)
            self._set("waiting")
        else:
            self.due = None
            self._set("exited" if code == 0 else "failed")
        self.report_now.set()

    # --- control
    def apply(self, c, src="local"):
        a = c.get("action")
        if a == "quit":
            self.note("ending (%s request)" % src)
            self.quit = True
        elif a == "stop":
            self.note("stopping (%s request)" % src)
            self.paused = True
            self._stop_child("stop")
            self.due = None
            self._set("stopped")
        elif a == "start":
            self.paused = False
            if self.child is None:
                self.note("starting (%s request)" % src)
                self.due = time.time()
        elif a == "restart":
            self.note("restarting (%s request)" % src)
            self.paused = False
            if self.child is not None:
                self._stop_child("restart")
            self.due = time.time()
        elif a == "set":
            changed = []
            try:
                if "schedule" in c:
                    self.r["schedule"] = check_schedule(str(c["schedule"] or "").strip())
                    changed.append("schedule=%s" % (self.r["schedule"] or "none"))
                if c.get("restart") in RESTARTS:
                    self.r["restart"] = c["restart"]
                    changed.append("restart=" + c["restart"])
                if "args" in c:
                    self.r["args"] = check_args(c["args"])
                    changed.append("args=" + (" ".join(self.r["args"]) or "none"))
            except ValueError as e:
                self.note("setting refused: %s" % e)
            if changed:
                self.note("settings changed (%s): %s; args apply at the next start" % (src, ", ".join(changed)))
                if self.child is None and not self.paused:
                    s, pol, code = self._sched(), self.r.get("restart"), self.r.get("last_exit")
                    if s:
                        self.due = time.time() + next_delay(s)
                        self._set("waiting")
                    elif self.r.get("status") == "waiting" and not (pol == "always" or pol == "on-failure" and code not in (0, None)):
                        self.due = None              # the pending restart no longer applies
                        self._set("exited" if code in (0, None) else "failed")
        self.report_now.set()

    def _controls(self):
        for c in _take_controls(self.rid):
            self.apply(c, "local")
        with self.lock:
            cmds, self.hub_cmds = self.hub_cmds, []
        for c in cmds:
            t = str(c.get("token") or "")
            if t and t not in self.done:
                self.apply(c, "hub" + (" " + c["by"] if c.get("by") else ""))
                self.done = (self.done + [t])[-50:]
                self.report_now.set()

    def _tick(self):
        if self.child is not None:
            if self.child.poll() is None:
                return
            self._reap()
        now = time.time()
        if self.paused:
            self._set("stopped")
            return
        if self.due is None:
            st = self.r.get("status")
            if st in ("exited", "failed") and (self.fg or not self.hub or now - (self.r.get("last_end") or 0) > LINGER):
                self.quit = True
            return
        if now >= self.due:
            self._start()
        else:
            self._set("waiting")

    # --- hub
    def _snapshot(self):
        r = self.r
        with self.lock:
            log = [x for x in self.lines if x[0] > self.sent][-TAIL:]
        run = {k: r.get(k) for k in ("id", "name", "bot", "args", "schedule", "restart", "status", "pid", "runs", "last_exit", "last_start",
                                     "last_end", "created", "error", "mode")}
        run.update(started=r.get("sup_started"), next_at=self.due if self.child is None else None, sup_pid=None if self.quit else os.getpid(),
                   alive=not self.quit, paused=self.paused)
        return {"client_id": hooks.client_id(), "host": socket.gethostname()[:100], "os": sys.platform, "cli_version": VERSION, "run": run,
                "log": [{"seq": s, "ts": t, "line": ln} for s, t, ln in log], "done": list(self.done)}

    def _report(self, timeout=10):
        body = self._snapshot()
        reply = api.call("POST", "/runs/report", body, timeout=timeout)
        if body["log"]:
            self.sent = body["log"][-1]["seq"]
        cmds = [c for c in reply.get("commands") or [] if isinstance(c, dict)]
        if cmds:
            with self.lock:
                self.hub_cmds.extend(cmds)
            self.wake.set()
        return max(2, min(int(reply.get("next") or REPORT), 300))

    def _reporter(self):
        wait, warned = 0, False
        while not self.quit:
            self.report_now.wait(wait)
            self.report_now.clear()
            if self.quit:
                break
            try:
                wait = self._report()
                warned = False
            except api.ApiError as e:
                code = str(e).split(" ", 1)[0]
                if code in ("401", "403", "404"):
                    self.note("hub reporting off: %s" % e)
                    self.hub = False
                    return
                if not warned:
                    self.note("hub unreachable, will retry: %s" % e)
                    warned = True
                wait = 60
            except Exception:
                wait = 60

    # --- main loop
    def run(self):
        now = time.time()
        self.r.update(sup_pid=os.getpid(), sup_started=now, mode="fg" if self.fg else "bg", error="")
        self._set("starting")
        if not self.paused:
            s = self._sched()
            # an interval runs now, then every N; a cron schedule waits for its first tick
            self.due = now + next_delay(s, now) if s and s[0] == "cron" else now
        if not WINDOWS:
            for sg in (signal.SIGTERM, signal.SIGHUP):
                try:
                    signal.signal(sg, self._on_signal)
                except (ValueError, OSError):
                    pass
        self.save(True)
        rep = None
        if self.hub:
            rep = threading.Thread(target=self._reporter, daemon=True)
            rep.start()
        try:
            while not self.quit:
                self._controls()
                if self.quit:
                    break
                self._tick()
                self.save()
                self.wake.wait(0.5)
                self.wake.clear()
        except KeyboardInterrupt:
            self.note("interrupted")
        finally:
            self.quit = True
            self._stop_child("stop")
            if self.r.get("status") not in ("exited", "failed"):
                self._set("stopped")
            self.save(True)
            self.report_now.set()
            if self.hub:
                try:
                    self._report(timeout=3)
                except Exception:
                    pass
            with self.lock:
                if self.logf:
                    self.logf.close()
                    self.logf = None
        code = self.r.get("last_exit")
        return code if isinstance(code, int) and code >= 0 else 0

    def _on_signal(self, *_):
        self.quit = True
        self.wake.set()


def _dur(s):
    s = int(max(0, s or 0))
    return "%dd%dh" % (s // 86400, s % 86400 // 3600) if s >= 86400 else "%dh%dm" % (s // 3600, s % 3600 // 60) if s >= 3600 else \
        "%dm%ds" % (s // 60, s % 60) if s >= 60 else "%ds" % s


# ------------------------------------------------------------------------------------------------ commands

def spawn(rid):
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    if WINDOWS:
        kw["creationflags"] = 0x08000000 | 0x00000200          # CREATE_NO_WINDOW | NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(telemetry.self_cmd() + ["run", "_supervise", rid], **kw)


def _wait(rid, until, secs):
    end = time.time() + secs
    r = load(rid)
    while time.time() < end:
        r = load(rid)
        if until(r):
            break
        time.sleep(0.2)
    return r


def tail(rid, n=20):
    try:
        with open(_f(rid, ".log"), "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 256 * 1024))
            return f.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return []


def new_spec(rid, name, bot, args, schedule, restart, hub, fg=False):
    return {"id": rid, "name": name, "bot": bot, "args": check_args(args), "schedule": check_schedule(schedule),
            "restart": restart or ("no" if schedule or fg else "on-failure"), "hub": hub, "created": time.time(), "status": "starting",
            "runs": 0, "mode": "fg" if fg else "bg"}


def launch(r, fg=False):
    """Start the supervisor for spec `r` (already validated). Background: detached, then a short look at how it went. -> exit code."""
    r.update(paused=False, sup_pid=None, sup_started=None, beat=0, status="starting", pid=None)
    _save(r)
    for ext in (".ctl",):
        try:
            os.remove(_f(r["id"], ext))
        except OSError:
            pass
    if fg:
        return Supervisor(r["id"], fg=True).run()
    spawn(r["id"])
    settled = lambda x: x and x.get("sup_started") and (not alive(x) or x.get("status") in ("running", "waiting", "failed", "exited", "stopped"))
    done = _wait(r["id"], settled, 10)
    if not settled(done):
        raise api.ApiError("the run did not start; see %s" % _f(r["id"], ".log"))
    time.sleep(1.2)           # a bot that dies at once (bad settings, needs a terminal) shows it here instead of later
    done = load(r["id"]) or done
    st = status_of(done)
    rid = r["id"]
    if st in ("failed", "exited") or done.get("last_exit") not in (None, 0):
        again = " and restarts (restart=%s)" % r["restart"] if alive(done) and st in ("waiting", "running") else ""
        ui.note("%s %s right away (exit %s)%s. Last lines of its log:" % (rid, "failed" if st == "failed" or done.get("last_exit") else "ended",
                                                                          done.get("last_exit"), again))
        for ln in tail(rid, 12):
            ui.out("    " + ln)
        if not done.get("error"):
            ui.info("an interactive (chat) bot needs this terminal: aihub run %s --fg" % r["bot"])
        return 1 if st == "failed" or done.get("last_exit") else 0
    what = "every %s" % r["schedule"] if r.get("schedule") else "restart=%s" % r["restart"]
    ui.good("%s running in the background (%s, %s)%s" % (rid, "pid %s" % done.get("pid") if done.get("pid") else st, what,
                                                        ", shown on the hub" if r.get("hub") else ""))
    ui.info("logs:  aihub run logs %s -f    stop: aihub run stop %s    all: aihub run list" % (rid, rid))
    return 0


def start(target, args, a, resume=False):
    found = installer.bots()
    rid = rid_of(a.name or target)
    old = load(rid)
    if target in found:
        bot = target
    elif old and old.get("bot") in found:
        bot = old["bot"]
    else:
        installer.bot_command(target)      # the usual "not an installed bot" error
        return 1
    if M.runnable(found[bot][1]).get("unique"):
        other = next((x for x in all_runs() if x.get("bot") == bot and x["id"] != rid and alive(x)), None)
        if other:
            raise api.ApiError("%s allows only one run at a time (unique = true) and %s is it. Use: aihub run restart %s | stop %s"
                               % (bot, other["id"], other["id"], other["id"]))
    if old and alive(old):
        if a.fg:
            raise api.ApiError("%s is already running in the background (aihub run stop %s first, or --name OTHER)" % (rid, rid))
        raise api.ApiError("%s is already running (pid %s). Use: aihub run restart %s | logs %s -f | stop %s, or --name OTHER"
                           % (rid, old.get("pid") or old.get("sup_pid"), rid, rid, rid))
    if target == bot and not (resume and old and old.get("bot") == bot):
        if a.restart and a.restart not in RESTARTS:
            raise ValueError("--restart: no, on-failure or always")
        spec = M.runnable(found[bot][1])     # [bot] / [service] restart + schedule are the defaults; flags win
        sched = a.schedule if a.schedule is not None else ("" if a.fg else spec.get("schedule", ""))
        restart = a.restart or (None if sched or a.fg else spec.get("restart") or None)
        r = new_spec(rid, a.name or rid, bot, args, sched, restart, hub_on() and not a.no_hub, fg=a.fg)
    else:                                    # `aihub run start NAME` of a saved run: its saved settings, flags on top
        r = dict(old)
        if args:
            r["args"] = check_args(args)
        if a.schedule is not None:
            r["schedule"] = check_schedule(a.schedule)
        if a.restart:
            r["restart"] = a.restart
        r["hub"] = hub_on() and not a.no_hub
        r["mode"] = "fg" if a.fg else "bg"
    installer.bot_command(bot, r["args"])   # missing settings: say so here, in the terminal, not in a log
    return launch(r, fg=a.fg)


def _need(rest, what):
    if not rest:
        raise api.ApiError("which run? aihub run %s NAME   (see: aihub run list)" % what)
    rid = rid_of(rest[0])
    r = load(rid)
    if not r:
        raise api.ApiError("no run named %s (see: aihub run list)" % rid)
    return rid, r


def stop(rid, r, quiet=False):
    if not alive(r):
        if not quiet:
            ui.info("%s is not running" % rid)
        return
    control(rid, "quit")
    done = _wait(rid, lambda x: not alive(x), STOP_WAIT + 5)
    if alive(done) or (done and done.get("sup_pid") and done.get("beat")):
        pid = (done or r).get("sup_pid")
        if pid:
            _kill_tree(pid, force=True, group=True)
        if done and done.get("pid"):
            _kill_tree(done["pid"], force=True, group=True)
        done = dict(done or r, sup_pid=None, beat=0, status="stopped", pid=None)
        _save(done)
    if not quiet:
        ui.good("%s stopped" % rid)


def stop_all():
    """`self-uninstall`: end every run before ~/.aihub goes away."""
    try:
        for r in all_runs():
            stop(r["id"], r, quiet=True)
    except Exception:
        pass


def cmd_list(a):
    rows = all_runs()
    if getattr(a, "json", False):
        print(json.dumps([dict(r, state=status_of(r), alive=alive(r)) for r in rows], indent=2))
        return
    ui.out()
    if not rows:
        ui.info("no runs. start one: aihub run NAME   (installed bots and services: aihub run)")
        return
    now = time.time()
    color = {"running": ui.ok, "waiting": ui.warn, "starting": ui.warn, "failed": ui.err, "dead": ui.err}
    out = []
    for r in rows:
        st = status_of(r)
        up = _dur(now - r["last_start"]) if st == "running" and r.get("last_start") else \
            ("in " + _dur(r["next_at"] - now)) if st == "waiting" and r.get("next_at") else "-"
        out.append([ui.bold(r["id"]), r.get("bot", ""), color.get(st, ui.dim)(st), str(r.get("pid") or "-"), up,
                    r.get("schedule") or "-", r.get("restart") or "-", str(r.get("runs") or 0),
                    "-" if r.get("last_exit") is None else str(r["last_exit"])])
    ui.table(["NAME", "BOT", "STATUS", "PID", "UP / NEXT", "SCHEDULE", "RESTART", "STARTS", "EXIT"], out)
    ui.out()
    if not hub_on():
        ui.info(ui.dim("runs show on the hub's Running page after: aihub login"))


def cmd_logs(rid, n, follow):
    p = _f(rid, ".log")
    for ln in tail(rid, n):
        ui.out(ln)
    if not follow:
        return
    ui.info(ui.dim("listening to %s (Ctrl-C to stop)" % rid))
    try:
        pos = os.path.getsize(p) if os.path.exists(p) else 0
        while True:
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            if size < pos:
                pos = 0                                  # rotated
            if size > pos:
                with open(p, "rb") as f:
                    f.seek(pos)
                    data = f.read()
                pos += len(data)
                sys.stdout.write(data.decode("utf-8", "replace"))
                sys.stdout.flush()
            time.sleep(0.3)
    except KeyboardInterrupt:
        sys.stdout.write("\n")


def cmd_set(rid, r, a, args):
    c = {}
    if a.schedule is not None:
        c["schedule"] = check_schedule(a.schedule.strip())
    if a.restart:
        if a.restart not in RESTARTS:
            raise ValueError("--restart: no, on-failure or always")
        c["restart"] = a.restart
    if args or a.clear_args:
        c["args"] = check_args(args)
    if not c:
        raise api.ApiError("nothing to change: give --schedule, --restart, --clear-args or new args after --")
    if alive(r):
        control(rid, "set", **c)
        ui.good("%s: %s (applied by the running supervisor; args at the next start)" % (rid, ", ".join("%s=%s" % kv for kv in c.items())))
    else:
        r.update(c)
        _save(r)
        ui.good("%s: saved; used at the next: aihub run start %s" % (rid, rid))


def cmd_rm(rid, r, force):
    if alive(r):
        if not force:
            raise api.ApiError("%s is running: aihub run stop %s first (or rm --force)" % (rid, rid))
        stop(rid, r, quiet=True)
    for ext in (".json", ".log", ".log.1", ".ctl"):
        try:
            os.remove(_f(rid, ext))
        except OSError:
            pass
    ui.good("%s removed" % rid)


def cli(a):
    t, rest = a.target, list(a.args or [])
    if t == "_supervise":
        return Supervisor(rid_of(rest[0])).run()
    if t in ("list", "ls", "ps"):
        return cmd_list(a)
    if t in ("logs", "log", "listen", "attach"):
        rid, _ = _need(rest, t)
        return cmd_logs(rid, a.lines, a.follow or t in ("listen", "attach"))
    if t in ("stop", "quit", "kill"):
        if a.all:
            for r in all_runs():
                if alive(r):
                    stop(r["id"], r)
            return
        rid, r = _need(rest, "stop")
        return stop(rid, r)
    if t == "restart":
        rid, r = _need(rest, "restart")
        if alive(r):
            control(rid, "restart")
            return ui.good("%s restarting" % rid)
        a.name = None
        code = start(rid, rest[1:], a, resume=True)
        if code:
            sys.exit(code)
        return
    if t == "pause":
        rid, r = _need(rest, "pause")
        if not alive(r):
            return ui.info("%s is not running" % rid)
        control(rid, "stop")
        return ui.good("%s paused (the supervisor waits; resume: aihub run start %s)" % (rid, rid))
    if t == "set":
        rid, r = _need(rest, "set")
        return cmd_set(rid, r, a, rest[1:])
    if t in ("rm", "remove"):
        rid, r = _need(rest, "rm")
        return cmd_rm(rid, r, a.force)
    if t in ("status", "show"):
        rid, r = _need(rest, t)
        if a.json:
            return print(json.dumps(dict(r, state=status_of(r), alive=alive(r)), indent=2))
        ui.out()
        for k in ("id", "bot", "args", "schedule", "restart", "mode", "runs", "last_exit", "pid", "error"):
            ui.out("  %-9s %s" % (k, r.get(k) if r.get(k) not in (None, "", []) else "-"))
        ui.out("  %-9s %s" % ("status", status_of(r)))
        ui.out("  %-9s %s" % ("log", _f(rid, ".log")))
        return
    if t == "start":
        if not rest:
            raise api.ApiError("which bot or run? aihub run start NAME")
        r = load(rid_of(rest[0])) if re.match(r"^[\w.-]+$", rest[0]) else None
        if r and alive(r):
            if r.get("paused") or status_of(r) in ("stopped", "exited", "failed"):
                control(r["id"], "start")
                return ui.good("%s starting" % r["id"])
            return ui.info("%s is already running" % r["id"])
        code = start(rest[0], rest[1:], a, resume=True)
    else:
        code = start(t, rest, a)
    if code:
        sys.exit(code)
