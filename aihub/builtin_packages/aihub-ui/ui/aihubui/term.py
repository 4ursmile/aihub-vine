"""A pseudo-terminal for ONE fixed kind of command: `aihub install NAME...`, so an install script that asks a question on stdin can be answered in the page.

This is not a shell. The page can only start an install of validated package names (server.py checks them); after that it can type into that
process, resize it, or stop it. Output is kept in memory (last MAX bytes) and read with a position, so a reload of the page loses nothing."""
import os
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
except ImportError:                                  # Windows: the install runs without a terminal
    HAVE_PTY = False

MAX = 400_000
lock = threading.Lock()
terms = {}


class Term:
    def __init__(self, argv, env):
        self.id = uuid.uuid4().hex[:12]
        self.buf = bytearray()
        self.base = 0                                # bytes dropped from the front
        self.code = None
        self.cv = threading.Condition()
        self.t0 = time.time()
        m, s = pty.openpty()
        self.fd = m
        self.set_size(24, 100)
        self.p = subprocess.Popen(argv, stdin=s, stdout=s, stderr=s, env=dict(env, TERM="xterm-256color"), start_new_session=True, close_fds=True)
        os.close(s)
        threading.Thread(target=self._pump, daemon=True).start()

    def set_size(self, rows, cols):
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
            os.close(self.fd)
        except OSError:
            pass

    def read(self, after, wait=20.0):
        """-> (bytes from absolute position AFTER, new position, exit code or None). Waits up to WAIT seconds for news."""
        end = time.time() + wait
        with self.cv:
            while True:
                top = self.base + len(self.buf)
                if after < top or self.code is not None:
                    break
                left = end - time.time()
                if left <= 0:
                    break
                self.cv.wait(left)
            top = self.base + len(self.buf)
            start = max(after, self.base)
            return bytes(self.buf[start - self.base:]), top, self.code

    def write(self, data):
        if self.code is None:
            try:
                os.write(self.fd, data[:8192])
            except OSError:
                pass

    def kill(self):
        if self.p.poll() is None:
            try:
                os.killpg(self.p.pid, signal.SIGTERM)
            except OSError:
                self.p.terminate()


def start(argv, env=None):
    if not HAVE_PTY:
        raise RuntimeError("no terminal on this system")
    with lock:
        for k in [k for k, t in terms.items() if t.code is not None and time.time() - t.t0 > 3600]:
            terms.pop(k, None)
        if len([t for t in terms.values() if t.code is None]) >= 4:
            raise RuntimeError("too many installs running")
        t = Term(argv, env or os.environ)
        terms[t.id] = t
    return t


def get(tid):
    with lock:
        return terms.get(str(tid))
