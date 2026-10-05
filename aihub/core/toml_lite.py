"""Minimal TOML 1.0 reader for Python < 3.11 (no tomllib). Pure stdlib.

Supports: tables, arrays of tables, dotted keys, basic/literal/multiline strings,
ints (dec/hex/oct/bin, underscores), floats, bools, arrays, inline tables, comments.
Datetimes are returned as strings. Raises ValueError on malformed input.
"""
import re

_BARE = re.compile(r"[A-Za-z0-9_-]+")
_NUM = re.compile(r"[+-]?(?:0x[0-9A-Fa-f_]+|0o[0-7_]+|0b[01_]+|(?:inf|nan)|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d[\d_]*)?)")
_DATE = re.compile(r"\d{4}-\d\d-\d\d(?:[Tt ]\d\d:\d\d:\d\d(?:\.\d+)?(?:[Zz]|[+-]\d\d:\d\d)?)?|\d\d:\d\d:\d\d(?:\.\d+)?")
_ESC = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}


class _Parser(object):
    def __init__(self, s):
        self.s, self.i, self.n = s.replace("\r\n", "\n"), 0, len(s.replace("\r\n", "\n"))

    def err(self, msg):
        line = self.s.count("\n", 0, self.i) + 1
        raise ValueError("TOML error line %d: %s" % (line, msg))

    def peek(self, k=1):
        return self.s[self.i:self.i + k]

    def ws(self):
        while self.i < self.n and self.s[self.i] in " \t":
            self.i += 1

    def skip_ws_nl(self):
        while self.i < self.n:
            c = self.s[self.i]
            if c in " \t\n":
                self.i += 1
            elif c == "#":
                self.comment()
            else:
                break

    def comment(self):
        while self.i < self.n and self.s[self.i] != "\n":
            self.i += 1

    def end_of_line(self):
        self.ws()
        if self.i < self.n and self.s[self.i] == "#":
            self.comment()
        if self.i < self.n and self.s[self.i] != "\n":
            self.err("unexpected %r" % self.s[self.i])

    # ---- keys
    def key(self):
        parts = []
        while True:
            self.ws()
            c = self.peek()
            if c == '"':
                parts.append(self.basic_string())
            elif c == "'":
                parts.append(self.literal_string())
            else:
                m = _BARE.match(self.s, self.i)
                if not m:
                    self.err("bad key")
                parts.append(m.group(0))
                self.i = m.end()
            self.ws()
            if self.peek() == ".":
                self.i += 1
                continue
            return parts

    # ---- strings
    def basic_string(self):
        if self.peek(3) == '"""':
            self.i += 3
            if self.peek() == "\n":
                self.i += 1
            out = []
            while True:
                if self.i >= self.n:
                    self.err("unterminated string")
                if self.peek(3) == '"""':
                    self.i += 3
                    while self.peek() == '"':  # up to 2 extra quotes allowed
                        out.append('"')
                        self.i += 1
                    return "".join(out)
                c = self.s[self.i]
                if c == "\\":
                    nxt = self.s[self.i + 1:self.i + 2]
                    if nxt in (" ", "\t", "\n"):
                        self.i += 1
                        while self.i < self.n and self.s[self.i] in " \t\n":
                            self.i += 1
                        continue
                    out.append(self.escape())
                else:
                    out.append(c)
                    self.i += 1
        self.i += 1
        out = []
        while True:
            if self.i >= self.n or self.s[self.i] == "\n":
                self.err("unterminated string")
            c = self.s[self.i]
            if c == '"':
                self.i += 1
                return "".join(out)
            if c == "\\":
                out.append(self.escape())
            else:
                out.append(c)
                self.i += 1

    def escape(self):
        self.i += 1
        c = self.s[self.i:self.i + 1]
        if c in _ESC:
            self.i += 1
            return _ESC[c]
        if c in ("u", "U"):
            k = 4 if c == "u" else 8
            hx = self.s[self.i + 1:self.i + 1 + k]
            try:
                ch = chr(int(hx, 16))
            except ValueError:
                self.err("bad unicode escape")
            self.i += 1 + k
            return ch
        self.err("bad escape \\%s" % c)

    def literal_string(self):
        if self.peek(3) == "'''":
            self.i += 3
            if self.peek() == "\n":
                self.i += 1
            j = self.s.find("'''", self.i)
            if j < 0:
                self.err("unterminated string")
            while self.s[j + 3:j + 4] == "'":
                j += 1
            v = self.s[self.i:j]
            self.i = j + 3
            return v
        j = self.s.find("'", self.i + 1)
        nl = self.s.find("\n", self.i)
        if j < 0 or (0 <= nl < j):
            self.err("unterminated string")
        v = self.s[self.i + 1:j]
        self.i = j + 1
        return v

    # ---- values
    def value(self):
        self.ws()
        c = self.peek()
        if c == '"':
            return self.basic_string()
        if c == "'":
            return self.literal_string()
        if c == "[":
            return self.array()
        if c == "{":
            return self.inline_table()
        if self.s.startswith("true", self.i):
            self.i += 4
            return True
        if self.s.startswith("false", self.i):
            self.i += 5
            return False
        m = _DATE.match(self.s, self.i)
        if m:
            self.i = m.end()
            return m.group(0)
        m = _NUM.match(self.s, self.i)
        if not m:
            self.err("bad value")
        self.i = m.end()
        t = m.group(0).replace("_", "")
        low = t.lower().lstrip("+-")
        if low in ("inf", "nan"):
            return float(t)
        if low[:2] in ("0x", "0o", "0b"):
            return int(t, 0)
        if any(ch in t for ch in ".eE"):
            return float(t)
        return int(t)

    def array(self):
        self.i += 1
        out = []
        while True:
            self.skip_ws_nl()
            if self.peek() == "]":
                self.i += 1
                return out
            out.append(self.value())
            self.skip_ws_nl()
            if self.peek() == ",":
                self.i += 1
            elif self.peek() != "]":
                self.err("expected , or ] in array")

    def inline_table(self):
        self.i += 1
        d = {}
        self.ws()
        if self.peek() == "}":
            self.i += 1
            return d
        while True:
            k = self.key()
            self.ws()
            if self.peek() != "=":
                self.err("expected =")
            self.i += 1
            _assign(d, k, self.value(), self)
            self.ws()
            c = self.peek()
            self.i += 1
            if c == "}":
                return d
            if c != ",":
                self.err("expected , or } in inline table")

    # ---- document
    def doc(self):
        root = {}
        cur = root
        while True:
            self.skip_ws_nl()
            if self.i >= self.n:
                return root
            if self.peek(2) == "[[":
                self.i += 2
                k = self.key()
                if self.peek(2) != "]]":
                    self.err("expected ]]")
                self.i += 2
                parent = _descend(root, k[:-1], self)
                arr = parent.setdefault(k[-1], [])
                if not isinstance(arr, list):
                    self.err("key %s is not an array of tables" % k[-1])
                cur = {}
                arr.append(cur)
                self.end_of_line()
            elif self.peek() == "[":
                self.i += 1
                k = self.key()
                if self.peek() != "]":
                    self.err("expected ]")
                self.i += 1
                cur = _descend(root, k, self)
                self.end_of_line()
            else:
                k = self.key()
                self.ws()
                if self.peek() != "=":
                    self.err("expected =")
                self.i += 1
                _assign(cur, k, self.value(), self)
                self.end_of_line()


def _descend(d, keys, p):
    for k in keys:
        nxt = d.setdefault(k, {})
        if isinstance(nxt, list):
            nxt = nxt[-1]
        if not isinstance(nxt, dict):
            p.err("key %s is not a table" % k)
        d = nxt
    return d


def _assign(d, keys, val, p):
    d = _descend(d, keys[:-1], p)
    if keys[-1] in d:
        p.err("duplicate key %s" % keys[-1])
    d[keys[-1]] = val


def loads(text):
    return _Parser(text).doc()
