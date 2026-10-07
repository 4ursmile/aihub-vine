"""Terminal UX for the CLI: colors, banner, spinners, progress, tables, prompts.
Stdlib only. Everything degrades to plain text when stdout is not a tty, NO_COLOR is set or TERM=dumb.
Never imported by the agent hook hot path."""
import os
import re
import shutil
import sys
import threading
import time

ACCENT = "38;5;75"      # sky blue, the single accent
DIM, OK, WARN, ERR = "2", "38;5;41", "38;5;214", "38;5;203"
TYPE_COLOR = {"skill": "38;5;75", "agent": "38;5;41", "mcp": "38;5;214", "tool": "38;5;250", "setup": "38;5;203"}


def _vt():
    if os.name == "nt":
        try:
            os.system("")                       # enables VT processing on Windows 10+
            import ctypes
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-11)
            m = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(m)):
                k.SetConsoleMode(h, m.value | 0x0004)
        except Exception:
            pass


def _is_tty(f):
    try:
        return f.isatty()
    except Exception:
        return False


_vt()
TTY = _is_tty(sys.stdout)
COLOR = TTY and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
FANCY = COLOR                                      # animations and cursor control
INTERACTIVE = TTY and _is_tty(sys.stdin)


def _unicode():
    try:
        "✓→⠋".encode(sys.stdout.encoding or "ascii")
        return True
    except Exception:
        return False


U = _unicode()
CHECK, CROSS, ARROW, DOT, BANG = ("✓", "✗", "→", "•", "!") if U else ("+", "x", "->", "*", "!")
FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" if U else "|/-\\"
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def c(code, s):
    return "\x1b[%sm%s\x1b[0m" % (code, s) if COLOR else str(s)


def bold(s): return c("1", s)
def dim(s): return c(DIM, s)
def accent(s): return c(ACCENT, s)
def ok(s): return c(OK, s)
def warn(s): return c(WARN, s)
def err(s): return c(ERR, s)
def vlen(s): return len(_ANSI.sub("", s))
def width(): return max(40, min(shutil.get_terminal_size((80, 24)).columns, 100))


def out(s=""):
    print(s)
    sys.stdout.flush()


def _cursor(show):
    if FANCY:
        sys.stdout.write("\x1b[?25h" if show else "\x1b[?25l")
        sys.stdout.flush()


# ---------- banner
LOGO = [
    r"  __ _(_) |__  _   _| |__  ",
    r" / _` | | '_ \| | | | '_ \ ",
    r"| (_| | | | | | |_| | |_) |",
    r" \__,_|_|_| |_|\__,_|_.__/ ",
]


def banner(tagline="skills, agents and MCP servers for your AI tools"):
    if not TTY:
        out("aihub - " + tagline)
        return
    shades = ["38;5;39", "38;5;45", "38;5;75", "38;5;111"]
    _cursor(False)
    try:
        out()
        for i, line in enumerate(LOGO):
            if FANCY:
                for n in range(0, len(line) + 1, 6):
                    sys.stdout.write("\r" + c(shades[i], line[:n]))
                    sys.stdout.flush()
                    time.sleep(0.004)
            sys.stdout.write("\r" + c(shades[i], line) + "\n")
        out(dim("  " + tagline))
        out()
    finally:
        _cursor(True)


# ---------- lines
def step(msg): out("  %s %s" % (accent(ARROW), msg))
def good(msg): out("  %s %s" % (ok(CHECK), msg))
def bad(msg): out("  %s %s" % (err(CROSS), msg))
def note(msg): out("  %s %s" % (warn(BANG), msg))
def info(msg): out("  " + dim(msg))
def heading(msg): out("\n" + bold(msg))
def code(cmd): return accent(cmd) if COLOR else "`%s`" % cmd


def error(msg, hint=None):
    sys.stderr.write("%s %s\n" % (err("error:") if COLOR else "error:", msg))
    if hint:
        sys.stderr.write("%s %s\n" % (dim("hint:") if COLOR else "hint:", hint))
    sys.stderr.flush()


def hint_for(msg):
    m = str(msg)
    if "cannot reach hub" in m:
        return "check your network, then run `aihub doctor` (or `aihub config set hub <url>`)"
    if m.startswith("401") or "sign in" in m.lower():
        return "run `aihub login` first"
    if m.startswith("404"):
        return "check the name with `aihub search <term>`"
    if m.startswith("403"):
        return "your role may not allow this; ask a hub admin"
    if "aihub.toml" in m and ("No such file" in m or "not found" in m.lower()):
        return "run `aihub dev init` to scaffold a project"
    return None


def celebrate(msg):
    out("\n  %s %s" % (ok(CHECK), ok(bold(msg))))


# ---------- spinner
class Spinner(object):
    """with Spinner("Resolving") as s: ... s.text("..."); ends with a check or cross line.
    Plain mode prints one line when done. Never keep one open while calling input()."""
    def __init__(self, text):
        self.t, self.stop, self.th, self.fail = text, threading.Event(), None, None

    def text(self, t):
        self.t = t

    def problem(self, t, level="warn"):
        """Finish with a warning (or cross with level='bad') instead of a check."""
        self.t, self.fail = t, level

    def pause(self):
        """Erase the spinner line so a prompt can use the terminal; call resume() after."""
        self._halt()
        if FANCY:
            sys.stdout.write("\r\x1b[2K")

    def resume(self):
        self._start()

    def _run(self):
        i = 0
        while not self.stop.is_set():
            sys.stdout.write("\r\x1b[2K  %s %s" % (accent(FRAMES[i % len(FRAMES)]), self.t))
            sys.stdout.flush()
            i += 1
            self.stop.wait(0.08)

    def _start(self):
        if FANCY:
            self.stop.clear()
            _cursor(False)
            self.th = threading.Thread(target=self._run, daemon=True)
            self.th.start()

    def _halt(self):
        if self.th:
            self.stop.set()
            self.th.join()
            self.th = None
            _cursor(True)

    def __enter__(self):
        self._start()
        return self

    def done(self, text=None):
        self._halt()
        if FANCY:
            sys.stdout.write("\r\x1b[2K")
        good(text or self.t)

    def __exit__(self, et, ev, tb):
        self._halt()
        if FANCY:
            sys.stdout.write("\r\x1b[2K")
        if et is None and self.fail == "warn":
            note(self.t)
        elif et is None and not self.fail:
            good(self.t)
        else:
            bad(self.t)
        return False


def human(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return ("%d %s" % (n, u)) if u == "B" else ("%.1f %s" % (n, u))
        n /= 1024


class Progress(object):
    """Callable (done, total) -> redraws a bar on a tty; prints nothing in plain mode."""
    def __init__(self, label="download"):
        self.label, self.last = label, 0

    def __call__(self, done, total):
        if not FANCY:
            return
        now = time.time()
        if now - self.last < 0.05 and done != total:
            return
        self.last = now
        w = 24
        frac = (done / total) if total else 0
        fill = int(w * frac)
        bar = ("█" * fill + "░" * (w - fill)) if U else ("#" * fill + "-" * (w - fill))
        pct = ("%3d%%" % (frac * 100)) if total else "    "
        sys.stdout.write("\r\x1b[2K  %s %s %s %s" % (accent(ARROW), self.label, accent(bar), dim("%s %s" % (pct, human(done) + (" / " + human(total) if total else "")))))
        sys.stdout.flush()

    def finish(self):
        if FANCY:
            sys.stdout.write("\r\x1b[2K")


# ---------- tables and cards
def badge(t):
    return c(TYPE_COLOR.get(t, "37"), "%-5s" % t) if COLOR else "%-5s" % t


def highlight(s, term):
    if not (COLOR and term):
        return s
    return re.sub("(%s)" % re.escape(term), lambda m: c("1;4", m.group(1)), s, flags=re.I)


def clip(s, n):
    s = str(s)
    return s if len(s) <= n else s[:max(n - 1, 1)] + ("…" if U else ".")


def table(headers, rows, aligns=None):
    """rows are lists of str (may contain ANSI). Last column is clipped to the terminal."""
    cols = len(headers)
    ws = [max([len(headers[i])] + [vlen(r[i]) for r in rows]) for i in range(cols)]
    spare = width() - 2 - sum(ws[:-1]) - 2 * (cols - 1)
    ws[-1] = max(10, min(ws[-1], spare))
    def line(cells, style=None):
        parts = []
        for i, cell in enumerate(cells):
            cell = str(cell)
            if i == cols - 1 and vlen(cell) > ws[i]:
                cell = clip(cell, ws[i]) if not _ANSI.search(cell) else cell
            pad = " " * max(ws[i] - vlen(cell), 0)
            parts.append(style(cell + pad) if style else cell + pad)
        return "  " + "  ".join(parts).rstrip()
    out(line(headers, dim))
    out(dim("  " + "  ".join(("-" if not U else "─") * w for w in ws)))
    for r in rows:
        out(line(r))


def card(title, lines, footer=None):
    inner = min(width() - 4, max([vlen(title)] + [vlen(l) for l in lines] + [vlen(footer or "")] + [30]))
    h, v, tl, tr, bl, br = ("─", "│", "╭", "╮", "╰", "╯") if U else ("-", "|", "+", "+", "+", "+")
    def row(s=""):
        out("  %s %s%s %s" % (dim(v), s, " " * max(inner - vlen(s), 0), dim(v)))
    out("  %s%s%s" % (dim(tl), dim(h * (inner + 2)), dim(tr)))
    row(bold(title))
    row()
    for l in lines:
        row(l)
    if footer:
        row()
        row(footer)
    out("  %s%s%s" % (dim(bl), dim(h * (inner + 2)), dim(br)))


def tree(root, paths):
    out("  " + bold(root))
    paths = sorted(paths)
    for i, p in enumerate(paths):
        last = i == len(paths) - 1
        out("  %s %s" % (dim(("└─" if last else "├─") if U else ("`-" if last else "|-")), p))


# ---------- prompts
def _getkey():
    """One keypress -> 'up','down','space','enter','esc','ctrl-c', or the character. None if unsupported."""
    if os.name == "nt":
        import msvcrt
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return {"H": "up", "P": "down"}.get(msvcrt.getwch(), "")
        return {"\r": "enter", " ": "space", "\x03": "ctrl-c", "\x1b": "esc"}.get(ch, ch)
    import select
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = os.read(fd, 1).decode("latin1")
        if ch == "\x1b":
            if select.select([fd], [], [], 0.05)[0]:
                rest = os.read(fd, 2).decode("latin1")
                return {"[A": "up", "[B": "down"}.get(rest, "")
            return "esc"
        return {"\r": "enter", "\n": "enter", " ": "space", "\x03": "ctrl-c", "k": "up", "j": "down"}.get(ch, ch)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _ask(prompt):
    try:
        return input(prompt)
    except EOFError:
        return ""


def confirm(prompt, default=True):
    if not INTERACTIVE:
        return default
    ans = _ask("  %s %s %s " % (accent("?"), prompt, dim("[Y/n]" if default else "[y/N]"))).strip().lower()
    return default if not ans else ans.startswith("y")


def _fallback_select(title, options, multi, preset):
    out("  %s %s" % (accent("?"), title))
    for i, (_, label) in enumerate(options, 1):
        out("    %s %s" % (dim("%d)" % i), label))
    ans = _ask("  %s " % dim("numbers (comma separated)" if multi else "number")).strip()
    if not ans:
        return list(preset) if multi else (options[0][0] if preset is None and options else None)
    try:
        picks = [options[int(x) - 1][0] for x in re.split(r"[,\s]+", ans) if x]
    except (ValueError, IndexError):
        return list(preset) if multi else None
    return picks if multi else (picks[0] if picks else None)


def select(title, options, multi=False, preset=()):
    """options: [(value, label)]. Arrow keys + space/enter on a tty, numbered input otherwise.
    multi -> list of values (preset values start ticked); single -> one value or None (esc)."""
    if not options:
        return [] if multi else None
    if not INTERACTIVE:
        return list(preset) if multi else options[0][0]
    try:
        pos, on = 0, set(v for v, _ in options if v in preset)
        hint = "arrows move, space toggles, enter confirms" if multi else "arrows move, enter selects, esc cancels"
        out("  %s %s %s" % (accent("?"), bold(title), dim("(" + hint + ")")))
        n = len(options)

        def draw(first):
            if not first:
                sys.stdout.write("\x1b[%dA" % n)
            for i, (v, label) in enumerate(options):
                cur = i == pos
                mark = ""
                if multi:
                    mark = (ok("[x] ") if v in on else dim("[ ] "))
                ptr = accent(ARROW) if cur else " "
                sys.stdout.write("\r\x1b[2K    %s %s%s\n" % (ptr, mark, bold(label) if cur else label))
            sys.stdout.flush()
        _cursor(False)
        draw(True)
        try:
            while True:
                k = _getkey()
                if k == "ctrl-c":
                    raise KeyboardInterrupt
                if k == "esc":
                    return [] if multi else None
                if k == "up":
                    pos = (pos - 1) % n
                elif k == "down":
                    pos = (pos + 1) % n
                elif k == "space" and multi:
                    on.symmetric_difference_update({options[pos][0]})
                elif k == "enter":
                    return [v for v, _ in options if v in on] if multi else options[pos][0]
                draw(False)
        finally:
            _cursor(True)
    except (ImportError, OSError, ValueError):
        return _fallback_select(title, options, multi, preset)
