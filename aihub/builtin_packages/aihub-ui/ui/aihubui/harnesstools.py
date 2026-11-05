"""AIHubness tools: the few file / shell tools of the UI's mini agent (harness.py), built to spend as few tokens as possible.

Every result is cut to what the model needs: big files come back as their first lines plus an outline (page on with offset/limit), a file
read again unchanged is not sent twice, edit answers with the changed lines only (no re-read needed), grep groups counts per file when there
are many hits, command output keeps head + tail with repeated lines folded and the full text saved to a file that read / grep / digest can
open. File tools stay inside the chat's working folder (and the folder of saved outputs). `run` follows the chat's permission mode."""
import base64
import fnmatch
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import threading
import time

from . import store

SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".idea", ".tox", "dist", "build", ".next", ".cache"}
IMAGES = (".png", ".jpg", ".jpeg", ".gif", ".webp")
READ_LINES = 200            # lines a read without offset/limit returns from a big file
WHOLE_LINES, WHOLE_CHARS = 400, 40_000      # a file up to this size comes back whole
MAX_LINES, MAX_CHARS = 1000, 50_000         # most one read returns
LINE_CUT = 500
OUT_CHARS = 10_000          # tool output over this goes to a file; the model gets head + tail
HEAD, TAIL = 2_500, 5_000
GREP_MAX = 120
LS_MAX = 200
READ_ONLY = {"plan", "ask"}                 # Plan / Read only: look, do not change
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
OUTLINE = re.compile(r"^\s*(?:(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:def|class|function|interface|type|enum|struct|impl|trait|module|func|fn|pub\s+fn|pub\s+struct)\b"
                     r"|(?:export\s+)?(?:const|let|var)\s+\w+\s*=\s*(?:async\s*)?(?:\(|function)|#{1,4}\s|\[[\w.-]+\]\s*$|CREATE\s+(?:TABLE|VIEW|FUNCTION))", re.I)
# ---------- permissions: what runs, what asks the user first, what is refused (see Tools.check)
CATASTROPHIC = [re.compile(p, re.I) for p in (                      # refused outright unless Bypass
    r"\brm\s+(-\w*\s+)*-\w*[rf]\w*\s+(/|~|\$HOME)(\s|$|/\*)", r"\bmkfs\b", r"\bdd\s+if=.*\bof=/dev/", r":\(\)\s*\{", r">\s*/dev/(sd|disk|nvme)", r"\bchmod\s+-R\s+777\s+/(\s|$)")]
DELETE = re.compile(                                                  # deletes or throws away work: ALWAYS asks (unless Bypass), never "always allowed"
    r"(^|[;&|(\n]\s*|\b(sudo|xargs|exec|then|do)\s+)(rm|rmdir|unlink|shred|trash|srm)\b|(^|[;&|\n]\s*)(del|erase|rd)\s|Remove-Item\b|\bgit\s+(rm|clean)\b"
    r"|\bfind\b.*\s-(delete|exec\s+rm)\b|\b(rmtree|os\.remove|os\.unlink|os\.rmdir|unlinkSync|rmSync|rimraf|Path\([^)]*\)\.unlink)\b"
    r"|\bgit\s+(reset\s+--hard|checkout\s+--\s|restore\b|stash\s+(drop|clear)|branch\s+-D\b|push\b.*(\s-f\b|--force|--delete|\s:))|\btruncate\b", re.I)
RISKY = re.compile(r"\bsudo\b|\b(shutdown|reboot|halt)\b|\b(curl|wget)\b[^|]*\|\s*(ba|z)?sh\b|\bchmod\s+-R\b|\bchown\s+-R\b|\bgit\s+push\b|\bkill(all)?\b|\bpkill\b"
                   r"|\b(npm|yarn|pnpm)\s+publish\b|\btwine\s+upload\b|\bdocker\s+(rm|rmi|system\s+prune)\b", re.I)
SAFE = {"ls", "cat", "head", "tail", "wc", "pwd", "echo", "printf", "which", "whereis", "type", "file", "stat", "du", "df", "date", "tree", "rg", "grep",
        "egrep", "fgrep", "sort", "uniq", "cut", "diff", "cmp", "basename", "dirname", "realpath", "uname", "whoami", "hostname", "true", "jq", "cd",
        "env", "printenv", "nl", "column", "tr", "md5sum", "shasum", "sha256sum", "less", "more", "awk", "dir", "where"}
SAFE_GIT = {"status", "diff", "log", "show", "rev-parse", "ls-files", "blame", "describe", "shortlog", "grep", "remote", "branch", "tag", "config"}
BUILD = re.compile(r"^(pytest|py\.test|tox|nox|mypy|ruff|flake8|pylint|black|isort|eslint|prettier|tsc|jest|vitest|mocha|make|cmake|ctest|gradle|mvn|dotnet\s+(build|test)"
                   r"|cargo\s+(build|test|check|clippy|fmt|run)|go\s+(build|test|vet|run|fmt)|(python3?|py)\s+-m\s+(pytest|unittest|mypy|ruff|black|compileall|py_compile)"
                   r"|(npm|pnpm|yarn|bun)\s+(test|run|build|lint)|node\s+--check|deno\s+(test|check|lint))\b")
SEGMENT = re.compile(r"&&|\|\||[;|\n]")

SPECS = [
    {"name": "read", "description": "Read a text file (line-numbered). Big files: first lines + outline; continue with offset/limit. Images are shown.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer", "description": "first line, 1-based"},
                                                       "limit": {"type": "integer", "description": "lines, max 1000"}}, "required": ["path"]}},
    {"name": "ls", "description": "List a folder, or find files by glob (e.g. **/*.py). Skips .git, node_modules, venvs.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "glob": {"type": "string"}}}},
    {"name": "grep", "description": "Regex search in files -> path:line:text (capped; counts per file when many).",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string", "description": "file filter, e.g. *.ts"},
                                                       "ignore_case": {"type": "boolean"}}, "required": ["pattern"]}},
    {"name": "edit", "description": "Replace exact text in a file. `old` must match once (or set all). Returns the changed lines.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}, "all": {"type": "boolean"}},
                      "required": ["path", "old", "new"]}},
    {"name": "write", "description": "Create or overwrite a file (folders are made).",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "run", "description": "Run a shell command in the working folder. Returns exit code + output (long output: head/tail, full text saved to a file).",
     "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}, "timeout": {"type": "integer", "description": "seconds, default 120, max 600"}}, "required": ["cmd"]}},
    {"name": "digest", "description": "Answer a question about a big file or saved output with a small fast model, without reading it all yourself.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "question": {"type": "string"}}, "required": ["path", "question"]}},
    {"name": "memory", "description": "Your notes kept across chats. add: save a durable fact; remove: drop note n; list: show them.",
     "input_schema": {"type": "object", "properties": {"action": {"type": "string", "enum": ["add", "remove", "list"]}, "text": {"type": "string"}, "n": {"type": "integer"}},
                      "required": ["action"]}},
]
CHANGES = {"edit", "write", "run"}


# ---------- memory: one Markdown list, shared by every AIHubness chat

MEM_MAX, MEM_LINE = 60, 300


def memory_path():
    return os.path.join(store.ui_dir("harness"), "memory.md")


def memory_items():
    try:
        with open(memory_path(), encoding="utf-8") as f:
            return [ln[2:].strip() for ln in f.read().splitlines() if ln.startswith("- ") and ln[2:].strip()]
    except OSError:
        return []


def _memory_save(items):
    p = memory_path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("".join("- %s\n" % i for i in items))
    os.replace(tmp, p)


def memory(action, text="", n=0):
    items = memory_items()
    if action == "add":
        t = " ".join(str(text or "").split())[:MEM_LINE]
        if not t:
            return "nothing to add", True
        if t in items:
            return "already noted", False
        items.append(t)
        dropped = items[:-MEM_MAX]
        _memory_save(items[-MEM_MAX:])
        return "noted (%d notes)%s" % (min(len(items), MEM_MAX), "; the oldest was dropped" if dropped else ""), False
    if action == "remove":
        try:
            i = int(n) - 1
            gone = items.pop(i) if 0 <= i < len(items) else None
        except (TypeError, ValueError):
            gone = None
        if gone is None:
            return "no note %s (there are %d)" % (n, len(items)), True
        _memory_save(items)
        return "removed: " + gone, False
    return ("\n".join("%d. %s" % (i + 1, t) for i, t in enumerate(items)) or "no notes yet"), False


# ---------- helpers

def _num(lines, start):
    out = []
    for i, ln in enumerate(lines, start):
        out.append("%5d\t%s" % (i, ln if len(ln) <= LINE_CUT else ln[:LINE_CUT] + " …[+%d chars]" % (len(ln) - LINE_CUT)))
    return "\n".join(out)


def outline(lines, most=80, after=0):
    """Line numbers of definitions / headings after line AFTER (what the model was not shown): a map of a big file for a few hundred
    tokens. Too many: the top-level ones, then an even sample from start to end (the last one included)."""
    hits = [(i, ln.strip()) for i, ln in enumerate(lines, 1) if i > after and len(ln) < 400 and OUTLINE.match(ln)]
    if len(hits) > most:
        top = [h for h in hits if not lines[h[0] - 1][:1].isspace()]
        hits = top if len(top) >= most // 2 else hits
        if len(hits) > most:
            n = len(hits)
            hits = [hits[round(k * (n - 1) / (most - 1))] for k in range(most)]
    return "\n".join("  L%d: %s" % (i, t[:120]) for i, t in hits)


def fold(text):
    """ANSI codes out, runs of the same line (progress bars, repeated warnings) folded to one line + a count."""
    text = ANSI.sub("", text).replace("\r\n", "\n")
    text = "\n".join(ln.rsplit("\r", 1)[-1] for ln in text.split("\n"))     # a progress bar redrawn with \r: its last state
    out, prev, n = [], None, 0
    for ln in text.split("\n"):
        if ln == prev:
            n += 1
            continue
        if n:
            out.append("   …(same line %d more times)" % n)
        out.append(ln)
        prev, n = ln, 0
    if n:
        out.append("   …(same line %d more times)" % n)
    return "\n".join(out)


def glob_rx(pat):
    """Glob -> regex on /-paths: ** spans folders, * and ? stay inside one."""
    out, i = [], 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        else:
            c = pat[i]
            out.append("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z")


class Tools:
    """The tools of one AIHubness turn. call(name, args) -> (content, is_error): content is a string or a list of content blocks."""

    def __init__(self, root, sid, mode, state, digest=None, allowed=None):
        self.root = os.path.realpath(root)
        self.sid = sid
        self.mode = mode if mode in ("auto", "edits", "plan", "ask", "bypass") else "auto"
        self.state = state                                             # {"cancel": Event, "proc": ...}: Stop kills a running command
        self.digest_fn = digest
        self.out_dir = store.ui_dir("harness", "out", sid)
        self.seen = {}                                                 # (path, offset, limit) -> (mtime, size) of what the model already has
        self.allowed = set(allowed or ())                              # command prefixes the user said "Always allow" for in this chat
        self.n_out = 0

    def specs(self):
        drop = CHANGES if self.mode in READ_ONLY else set()
        return [s for s in SPECS if s["name"] not in drop]

    def check(self, name, a):
        """Permission for one call, like Claude Code's: -> (verdict, info). verdict: "allow" | "ask" | "deny".
        info: {kind: run|delete|risky, text: what the user is asked about, prefix: what "Always allow" would allow, why}.
          Bypass: everything runs (catastrophic commands too). Plan / Read only: changing tools are not even offered.
          Deletes (rm, git clean, find -delete, git reset --hard, a memory note...) ALWAYS ask, and "Always allow" never covers them.
          Risky commands (sudo, git push, kill, publish...) ask every time too. Read-only commands (ls, cat, rg, git status...) run.
          Auto also runs test / build / lint commands; Accept edits asks for them. Any other command asks, unless the user said
          "Always allow" for its prefix in this chat. File edits inside the working folder run in both."""
        a = a if isinstance(a, dict) else {}
        if self.mode == "bypass" or name not in CHANGES | {"memory"}:
            return "allow", {}
        if name == "memory":
            if str(a.get("action")) != "remove":
                return "allow", {}
            items = memory_items()
            try:
                what = items[int(a.get("n")) - 1]
            except (TypeError, ValueError, IndexError):
                return "allow", {}                                           # no such note: the tool says so
            return "ask", {"kind": "delete", "text": "Forget memory note %s: %s" % (a.get("n"), what), "why": "removes a memory note"}
        if name in ("edit", "write"):
            return "allow", {}
        cmd = str(a.get("cmd") or "").strip()
        if any(rx.search(cmd) for rx in CATASTROPHIC):
            return "deny", {"why": "this command could wipe the system or a disk; only Bypass mode runs it"}
        if DELETE.search(cmd):
            return "ask", {"kind": "delete", "text": cmd, "why": "deletes files or throws away changes"}
        if RISKY.search(cmd):
            return "ask", {"kind": "risky", "text": cmd, "why": "needs admin rights, stops processes or changes something outside this folder"}
        segs = [x.strip() for x in SEGMENT.split(cmd) if x.strip()]
        unsafe = [x for x in segs if not self._safe(x)]
        if not unsafe:
            return "allow", {}
        pre = [prefix(x) for x in unsafe]
        if all(p in self.allowed for p in pre):
            return "allow", {}
        return "ask", {"kind": "run", "text": cmd, "prefix": pre[0] if len(set(pre)) == 1 else "", "why": "runs a command"}

    def _safe(self, seg):
        if re.search(r"\$\(|`|>(?!\s*(/dev/null|&\d))", seg):              # command substitution or writing to a file
            return False
        words = [w for w in seg.split() if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w)]
        if not words:
            return True
        w0 = os.path.basename(words[0]).lower()
        if w0 in SAFE:
            return not (w0 in ("awk",) and "system(" in seg)
        if w0 == "find":
            return not re.search(r"\s-(exec|execdir|ok|delete|fprint)\b", seg)
        if w0 == "sed":
            return not re.search(r"\s-i\b|\s--in-place", seg)
        if w0 == "git" and len(words) > 1:
            sub = next((w for w in words[1:] if not w.startswith("-")), "")
            return sub in SAFE_GIT and not re.search(r"\s-(d|D|m|M|c|C)\b|--(delete|move|unset|add|replace-all)\b", seg) and (sub != "config" or "--get" in seg or "--list" in seg or " -l" in seg)
        if len(words) == 2 and words[1] in ("--version", "-V", "--help", "-h", "version"):
            return True
        return self.mode == "auto" and bool(BUILD.match(" ".join([w0] + words[1:])))

    def names(self):
        return {s["name"] for s in self.specs()}

    def forget(self):
        """Old results were cut out of the conversation: a read must send the text again."""
        self.seen.clear()

    # --- paths

    def _path(self, p, write=False):
        p = str(p or ".").strip() or "."
        full = os.path.realpath(p if os.path.isabs(p) else os.path.join(self.root, p))
        if store.inside(full, self.root) or (not write and store.inside(full, os.path.realpath(self.out_dir))):
            return full
        raise ValueError("%s is outside the working folder %s" % (p, self.root))

    def rel(self, full):
        try:
            r = os.path.relpath(full, self.root)
        except ValueError:
            return full
        return full if r.startswith("..") else r.replace(os.sep, "/")

    def save_out(self, text, label):
        """Long output -> a file under the chat's output folder. -> its path (for read / grep / digest)."""
        self.n_out += 1
        p = os.path.join(self.out_dir, "%s-%d-%d.txt" % (label, int(time.time()), self.n_out))
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
            return p
        except OSError:
            return ""

    def clip(self, text, label="out"):
        """Text over OUT_CHARS -> head + tail, the whole of it saved to a file the model can open."""
        if len(text) <= OUT_CHARS:
            return text
        p = self.save_out(text, label)
        lines = text.count("\n") + 1
        return (text[:HEAD] + "\n\n…[%d chars / %d lines cut%s. Use grep, read with offset, or digest on it]…\n\n" % (
            len(text) - HEAD - TAIL, lines, "; full text: " + p if p else "") + text[-TAIL:])

    # --- the tools

    def call(self, name, a):
        a = a if isinstance(a, dict) else {}
        if name not in self.names():
            if name in CHANGES:
                return "%s is not allowed in %s mode: nothing may be changed; propose the change instead" % (name, self.mode), True
            return "unknown tool " + str(name), True
        try:
            return getattr(self, "t_" + name)(a)
        except ValueError as e:
            return str(e), True
        except OSError as e:
            return "%s: %s" % (type(e).__name__, e), True

    def t_read(self, a):
        p = self._path(a.get("path"))
        if os.path.isdir(p):
            return "%s is a folder: use ls" % a.get("path"), True
        st = os.stat(p)
        off = max(1, int(a.get("offset") or 1))
        lim = a.get("limit")
        lim = max(1, min(MAX_LINES, int(lim))) if lim else None
        key = (p, off, lim)
        if self.seen.get(key) == (st.st_mtime, st.st_size):
            return "[%s is unchanged since you read it in this task: use that result]" % self.rel(p), False
        if p.lower().endswith(IMAGES) and st.st_size <= 3_750_000:
            with open(p, "rb") as f:
                data = f.read()
            self.seen[key] = (st.st_mtime, st.st_size)
            return [{"type": "image", "source": {"type": "base64", "media_type": mimetypes.guess_type(p)[0] or "image/png", "data": base64.b64encode(data).decode()}}], False
        with open(p, "rb") as f:
            raw = f.read(8_000_001)
        if b"\0" in raw[:8192]:
            return "%s is a binary file (%d bytes): not shown" % (self.rel(p), st.st_size), True
        text = raw.decode("utf-8", "replace")
        lines = text.splitlines()
        total = len(lines)
        self.seen[key] = (st.st_mtime, st.st_size)
        if not total:
            return "(empty file)", False
        if lim is None and off == 1 and total <= WHOLE_LINES and len(text) <= WHOLE_CHARS:
            return _num(lines, 1), False
        if lim is None and off == 1:                                     # big file, no range asked: the start + a map of the rest
            ol = outline(lines, after=READ_LINES)
            return (_num(lines[:READ_LINES], 1) + "\n\n[%s: %d lines, %d KB; showing 1-%d.%s Read more with offset/limit, or grep.]" % (
                self.rel(p), total, len(raw) // 1024, READ_LINES, "\nOutline:\n" + ol + "\n" if ol else "")), False
        end = min(total, off - 1 + (lim or READ_LINES))
        if off > total:
            return "offset %d is past the end (%d lines)" % (off, total), True
        body, chars, last = [], 0, off - 1
        for i in range(off - 1, end):
            chars += min(len(lines[i]), LINE_CUT) + 8
            if chars > MAX_CHARS and body:
                break
            body.append(lines[i])
            last = i + 1
        more = " [lines %d-%d of %d%s]" % (off, last, total, "; more with offset=%d" % (last + 1) if last < total else "")
        return _num(body, off) + "\n" + more, False

    def t_ls(self, a):
        base = self._path(a.get("path") or ".")
        pat = str(a.get("glob") or "").strip()
        if not os.path.isdir(base):
            return "%s is not a folder" % (a.get("path") or "."), True
        out, more = [], 0
        if pat:
            rx = glob_rx(pat)
            for d, dirs, fs in os.walk(base):
                dirs[:] = sorted(x for x in dirs if x not in SKIP)
                for fn in sorted(fs):
                    r = os.path.relpath(os.path.join(d, fn), base).replace(os.sep, "/")
                    if rx.match(r) or ("/" not in pat and fnmatch.fnmatch(fn, pat)):
                        if len(out) < LS_MAX:
                            out.append(self.rel(os.path.join(d, fn)))
                        else:
                            more += 1
            return ("\n".join(out) or "no match") + ("\n…and %d more: narrow the glob" % more if more else ""), False
        for e in sorted(os.scandir(base), key=lambda e: (not e.is_dir(), e.name.lower())):
            if len(out) >= LS_MAX:
                more += 1
                continue
            try:
                if e.is_dir():
                    out.append(e.name + "/" + (" (skipped)" if e.name in SKIP else ""))
                else:
                    sz = e.stat().st_size
                    out.append("%s  %s" % (e.name, "%d B" % sz if sz < 1024 else "%d KB" % (sz // 1024)))
            except OSError:
                out.append(e.name)
        return ("\n".join(out) or "(empty folder)") + ("\n…and %d more" % more if more else ""), False

    def t_grep(self, a):
        pat = str(a.get("pattern") or "")
        if not pat:
            return "pattern is empty", True
        base = self._path(a.get("path") or ".")
        glob, icase = str(a.get("glob") or ""), bool(a.get("ignore_case"))
        hits = self._rg(pat, base, glob, icase) if shutil.which("rg") else None
        if hits is None:
            hits = self._pygrep(pat, base, glob, icase)
        if isinstance(hits, str):
            return hits, True
        if not hits:
            return "no match", False
        show = [("%s:%s:%s" % (self.rel(f), n, t.strip()[:240])) for f, n, t in hits[:GREP_MAX]]
        txt = "\n".join(show)
        if len(hits) > GREP_MAX:
            per = {}
            for f, _, _ in hits:
                per[f] = per.get(f, 0) + 1
            top = sorted(per.items(), key=lambda x: -x[1])[:30]
            txt += "\n\n[%d matches in %d files; first %d shown. Per file:]\n%s" % (len(hits), len(per), GREP_MAX, "\n".join("  %s: %d" % (self.rel(f), c) for f, c in top))
        return self.clip(txt, "grep"), False

    def _rg(self, pat, base, glob, icase):
        cmd = ["rg", "-n", "--no-heading", "--color", "never", "--max-columns", "300", "--max-columns-preview", "-m", "50"] + (["-i"] if icase else [])
        one = os.path.isfile(base)
        cmd += sum((["-g", "!" + s] for s in SKIP), []) + (["-g", glob] if glob else []) + ["-e", pat, "--", os.path.relpath(base, self.root) if not one else base]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=60, stdin=subprocess.DEVNULL, cwd=self.root)    # relative paths: no drive letter before the first ':'

        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode == 2 and not r.stdout:
            return "bad pattern: " + r.stderr.decode("utf-8", "replace").strip()[-300:]
        out = []
        for ln in r.stdout.decode("utf-8", "replace").splitlines()[:5000]:
            f, _, rest = ln.partition(":") if not one else (base, "", ln)
            n, _, t = rest.partition(":")
            out.append((f if one else os.path.join(self.root, f), n, t))
        return out

    def _pygrep(self, pat, base, glob, icase):
        try:
            rx = re.compile(pat, re.I if icase else 0)
        except re.error as e:
            return "bad pattern: %s" % e
        files = [base] if os.path.isfile(base) else []
        if not files:
            for d, dirs, fs in os.walk(base):
                dirs[:] = sorted(x for x in dirs if x not in SKIP)
                files += [os.path.join(d, f) for f in sorted(fs) if not glob or fnmatch.fnmatch(f, glob)]
        out, t0 = [], time.time()
        for fp in files:
            if len(out) >= 5000 or time.time() - t0 > 30:
                break
            try:
                with open(fp, "rb") as f:
                    raw = f.read(2_000_000)
            except OSError:
                continue
            if b"\0" in raw[:4096]:
                continue
            for i, ln in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
                if rx.search(ln):
                    out.append((fp, str(i), ln))
        return out

    def t_write(self, a):
        p = self._path(a.get("path"), write=True)
        content = str(a.get("content") if a.get("content") is not None else "")
        new = not os.path.exists(p)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        self._drop(p)
        return "%s %s (%d lines, %d bytes)" % ("created" if new else "overwrote", self.rel(p), content.count("\n") + (0 if content.endswith("\n") else 1), len(content.encode("utf-8"))), False

    def t_edit(self, a):
        p = self._path(a.get("path"), write=True)
        old, new = str(a.get("old") or ""), str(a.get("new") if a.get("new") is not None else "")
        if not old:
            return "old is empty: use write to create a file", True
        with open(p, encoding="utf-8", newline="") as f:
            text = f.read()
        n = text.count(old)
        if not n and "\r\n" in text and "\r\n" not in old:              # a Windows file: the model writes \n
            old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
            n = text.count(old)
        if not n:
            first = old.strip().splitlines()[0].strip() if old.strip() else ""
            near = [i for i, ln in enumerate(text.splitlines(), 1) if first and first in ln][:5]
            return "old text not found in %s%s. Read those lines again and copy them exactly." % (
                self.rel(p), "; its first line is near line %s" % ", ".join(map(str, near)) if near else ""), True
        if n > 1 and not a.get("all"):
            return "old text is in %s %d times: add surrounding lines to make it unique, or set all" % (self.rel(p), n), True
        at = text.index(old)
        out = text.replace(old, new) if a.get("all") else text[:at] + new + text[at + len(old):]
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(out)
        self._drop(p)
        lines = out.splitlines()
        first = text[:at].count("\n")                                   # 0-based line of the change
        span = new.count("\n") + 1
        lo, hi = max(0, first - 2), min(len(lines), first + span + 2)
        return "edited %s (%d replacement%s). Now:\n%s" % (self.rel(p), n if a.get("all") else 1, "s" if a.get("all") and n > 1 else "",
                                                            _num(lines[lo:hi][:60], lo + 1)), False

    def _drop(self, p):
        for k in [k for k in self.seen if k[0] == p]:
            self.seen.pop(k, None)

    def t_run(self, a):
        cmd = str(a.get("cmd") or "").strip()
        if not cmd:
            return "cmd is empty", True
        if self.mode != "bypass" and any(rx.search(cmd) for rx in CATASTROPHIC):
            return "refused: this command could wipe the system or a disk (only Bypass mode runs it)", True
        try:
            limit = max(1, min(600, int(a.get("timeout") or 120)))
        except (TypeError, ValueError):
            limit = 120
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        env.update({"AIHUB_UI_SESSION": self.sid, "PAGER": "cat", "GIT_PAGER": "cat", "NO_COLOR": "1", "TERM": "dumb"})
        kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)} if os.name == "nt" else {"start_new_session": True}
        p = subprocess.Popen(cmd, shell=True, cwd=self.root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
        self.state["proc"] = p
        chunks, size = [], [0]

        def pump():
            for b in iter(lambda: p.stdout.read(65536), b""):
                if size[0] < 20_000_000:
                    chunks.append(b)
                    size[0] += len(b)
        t = threading.Thread(target=pump, daemon=True)
        t.start()
        t0, why = time.time(), ""
        while p.poll() is None:
            if self.state["cancel"].wait(0.2):
                why = "stopped by the user"
                break
            if time.time() - t0 > limit:
                why = "timed out after %ds" % limit
                break
        if why:
            kill(p)
        t.join(5)
        try:
            p.stdout.close()
        except OSError:
            pass
        self.state["proc"] = None
        rc = p.poll()
        text = fold(b"".join(chunks).decode("utf-8", "replace")).strip()
        head = "exit %s%s (%.1fs)" % (rc, ", " + why if why else "", time.time() - t0)
        return head + ("\n" + self.clip(text, "run") if text else "\n(no output)"), bool(why) or rc != 0

    def t_digest(self, a):
        p = self._path(a.get("path"))
        q = str(a.get("question") or "").strip()
        if not q:
            return "ask a question", True
        if not self.digest_fn:
            return "digest is not available here: use grep / read", True
        with open(p, "rb") as f:
            raw = f.read(4_000_000)
        if b"\0" in raw[:8192]:
            return "binary file: digest needs text", True
        text = fold(raw.decode("utf-8", "replace"))
        if len(text) > 400_000:                                         # logs: the end matters most
            text = text[:120_000] + "\n…[middle cut]…\n" + text[-280_000:]
        return self.digest_fn(q, self.rel(p), text)

    def t_memory(self, a):
        return memory(str(a.get("action") or "list"), a.get("text"), a.get("n"))


def prefix(seg):
    """What "Always allow" remembers for a command: its program and subcommand ("npm install", "git commit", "python"), not its arguments."""
    words = [w for w in seg.split() if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w)]
    if not words:
        return ""
    w0 = os.path.basename(words[0])
    if len(words) > 1 and re.match(r"^[a-z][a-z0-9_-]*$", words[1]) and w0 in ("git", "npm", "pnpm", "yarn", "bun", "cargo", "go", "docker", "kubectl", "pip",
                                                                           "pip3", "uv", "poetry", "brew", "apt", "gh", "make", "dotnet", "aihub"):
        return w0 + " " + words[1]
    if len(words) > 2 and words[1] == "-m" and w0.startswith("python"):
        return w0 + " -m " + words[2]
    return w0


def kill(p):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10)
        else:
            os.killpg(p.pid, signal.SIGKILL)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass
