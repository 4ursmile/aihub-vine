"""A pseudo-terminal for ONE fixed kind of command: `aihub install NAME...`, so an install script that asks a question on stdin can be answered in the page.

This is not a shell. The page can only start an install of validated package names (server.py checks them); after that it can type into that
process, resize it, or stop it. Output is kept in memory (last MAX bytes) and read with a position, so a reload of the page loses nothing."""
import os
import re
import signal
import subprocess
import threading
import time
import uuid

try:
    import fcntl
    import pty
    import struct
    import termios
    HAVE_PTY = True
except ImportError:                                  # Windows: Term falls back to pipes with its own line editing
    HAVE_PTY = False

MAX = 400_000
IDLE_REAP = 15 * 60                                  # a terminal nobody has read from for this long is killed (the page is gone)
KILL_GRACE = 2.0                                     # SIGTERM -> SIGKILL
lock = threading.Lock()
terms = {}


class Term:
    def __init__(self, argv, env, cwd=None):
        self.cwd = cwd
        self.id = uuid.uuid4().hex[:12]
        self.buf = bytearray()
        self.base = 0                                # bytes dropped from the front
        self.code = None
        self.cv = threading.Condition()
        self.t0 = time.time()
        self.last_read = self.t0
        self.pipe = False
        self.shell = False
        self._open(argv, env)
        threading.Thread(target=self._pump, daemon=True).start()

    def _open(self, argv, env):
        if HAVE_PTY:
            try:
                m, s = pty.openpty()
            except OSError:                          # no free pty: fall through to plain pipes
                m = None
            if m is not None:
                self.fd = m
                self.set_size(24, 100)
                try:
                    self.p = subprocess.Popen(argv, stdin=s, stdout=s, stderr=s, env=dict(env, TERM="xterm-256color"), start_new_session=True, close_fds=True, cwd=self.cwd)
                except BaseException:
                    os.close(m)                      # no child will ever read it: do not leak the master
                    raise
                finally:
                    os.close(s)
                return
        # No pseudo-terminal (Windows): the child gets pipes, this class does the line editing and the echo a terminal would do.
        self.pipe, self.line = True, bytearray()
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kw["start_new_session"] = True
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0, cwd=self.cwd,
                                  env=dict(env, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", TERM="xterm-256color"), **kw)
        self.fd = self.p.stdout.fileno()

    def set_size(self, rows, cols):
        if self.pipe:
            return
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(2, min(rows, 300)), max(10, min(cols, 400)), 0, 0))
        except OSError:
            pass

    def _pump(self):
        while True:
            try:
                d = os.read(self.fd, 4096)
            except OSError:
                d = b""
            if not d:
                break
            if self.pipe:
                d = d.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")        # a terminal page needs CR LF
            with self.cv:
                self.buf += d
                if len(self.buf) > MAX:
                    cut = len(self.buf) - MAX
                    del self.buf[:cut]
                    self.base += cut
                self.cv.notify_all()
        code = self.p.wait()
        with self.cv:
            self.code = code
            self.cv.notify_all()
        try:
            if self.pipe:
                self.p.stdout.close()
            else:
                os.close(self.fd)
        except OSError:
            pass

    def _echo(self, d):
        with self.cv:
            self.buf += d
            self.cv.notify_all()

    def read(self, after, wait=20.0):
        """-> (bytes from absolute position AFTER, new position, exit code or None). Waits up to WAIT seconds for news."""
        end = time.time() + wait
        self.last_read = time.time()
        with self.cv:
            while True:
                top = self.base + len(self.buf)
                if after < top or self.code is not None:
                    break
                left = end - time.time()
                if left <= 0:
                    break
                self.cv.wait(left)
            self.last_read = time.time()
            top = self.base + len(self.buf)
            start = max(after, self.base)
            return bytes(self.buf[start - self.base:]), top, self.code

    def write(self, data):
        if self.code is not None:
            return
        if self.pipe:
            self._write_pipe(data[:65536])
            return
        try:
            os.write(self.fd, data[:65536])
        except OSError:
            pass

    def _write_pipe(self, data):
        """Typed keys -> a line editor: echo what is typed, send the line on Enter, Backspace edits, Ctrl-C stops."""
        text = re.sub(r"\x1b\[200~|\x1b\[201~|\x1b\[[0-9;?]*[ -/]*[@-~]|\x1bO.|\x1b", "", data.decode("utf-8", "replace"))   # paste markers, arrows, F-keys
        for ch in text:
            if ch == "\x03":
                self._echo(b"^C\r\n")
                self.kill()
                return
            if ch in "\r\n":
                line, self.line = bytes(self.line), bytearray()
                self._echo(b"\r\n")
                try:
                    self.p.stdin.write(line + b"\n")
                    self.p.stdin.flush()
                except (OSError, ValueError):
                    return
            elif ch in "\x7f\b":
                self._erase(1)
            elif ch == "\x15":                                                  # Ctrl-U: whole line
                self._erase(len(self.line.decode("utf-8", "ignore")))
            elif ch == "\x17":                                                  # Ctrl-W: last word
                cur = self.line.decode("utf-8", "ignore")
                body = cur.rstrip()
                self._erase(len(cur) - (len(body.rsplit(" ", 1)[0]) + 1 if " " in body else 0))
            elif ch == "\t":
                self.line += b" "
                self._echo(b" ")
            elif ch >= " ":
                self.line += ch.encode("utf-8")
                self._echo(ch.encode("utf-8"))

    def _erase(self, n):
        cur = self.line.decode("utf-8", "ignore")
        n = min(n, len(cur))
        if n > 0:
            self.line = bytearray(cur[:len(cur) - n].encode("utf-8"))
            self._echo(b"\b \b" * n)

    def kill(self):
        """Stop the command: hang up + SIGTERM to its whole group, SIGKILL after KILL_GRACE s if it is still there. (The master side of the pty
        is closed by the pump when the child is gone; closing it here too could hit a reused descriptor.)"""
        if self.p.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.p.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10)
                else:
                    for sg in (signal.SIGHUP, signal.SIGTERM):
                        os.killpg(self.p.pid, sg)
                    tm = threading.Timer(KILL_GRACE, self._kill9)
                    tm.daemon = True
                    tm.start()
            except Exception:
                try:
                    self.p.terminate()
                except Exception:
                    pass

    def _kill9(self):
        if self.p.poll() is None:
            try:
                os.killpg(self.p.pid, signal.SIGKILL)
            except Exception:
                try:
                    self.p.kill()
                except Exception:
                    pass


_reaper = []


def _reap_loop():
    while True:
        time.sleep(60)
        reap()


def reap(now=None):
    """Kill running terminals nobody has read from for IDLE_REAP seconds. -> how many."""
    now = now or time.time()
    with lock:
        idle = [t for t in terms.values() if t.code is None and now - t.last_read > IDLE_REAP]
    for t in idle:
        t.kill()
    return len(idle)


def start(argv, env=None, cwd=None, shell=False):
    with lock:
        for k in [k for k, t in terms.items() if t.code is not None and time.time() - t.t0 > 3600]:
            terms.pop(k, None)
        if len([t for t in terms.values() if t.code is None]) >= (12 if shell else 4):
            raise RuntimeError("too many terminals open" if shell else "too many installs running")
        t = Term(argv, env or os.environ, cwd)
        t.shell = shell
        terms[t.id] = t
        if not _reaper:
            _reaper.append(threading.Thread(target=_reap_loop, daemon=True, name="term-reaper"))
            _reaper[0].start()
    return t


def get(tid):
    with lock:
        return terms.get(str(tid))
