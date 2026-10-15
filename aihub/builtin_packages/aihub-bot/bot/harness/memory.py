"""Soul (persona) and personal knowledge. Notes are markdown files in the knowledge folder (`aihub knowledge pull|edit|push`)."""
import os
import re

from . import config

NOTE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
INLINE_LIMIT = 6000          # notes up to this size (total) go straight into the prompt; the rest is read on demand
DEFAULT_SOUL = "You are a helpful, concise assistant. Ask before doing anything destructive."


def _read(p):
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def notes():
    try:
        return sorted(f[:-3] for f in os.listdir(config.KNOWLEDGE) if f.endswith(".md"))
    except OSError:
        return []


def soul():
    return _read(os.path.join(config.KNOWLEDGE, "soul.md")).strip() or DEFAULT_SOUL


def read(name):
    if not NOTE.match(name):
        return "invalid note name"
    return _read(os.path.join(config.KNOWLEDGE, name + ".md")) or "no such note"


def save(name, body):
    if not NOTE.match(name) or name == "soul":
        return "invalid note name (soul is edited by the user: aihub knowledge edit soul)"
    os.makedirs(config.KNOWLEDGE, exist_ok=True)
    with open(os.path.join(config.KNOWLEDGE, name + ".md"), "w", encoding="utf-8") as f:
        f.write(body)
    return "saved %s (the user can sync it with: aihub knowledge push %s)" % (name, name)


def prompt_block():
    names = [n for n in notes() if n != "soul"]
    out, used, later = [], 0, []
    for n in names:
        t = _read(os.path.join(config.KNOWLEDGE, n + ".md"))
        if used + len(t) <= INLINE_LIMIT:
            out.append("## note: %s\n%s" % (n, t.strip()))
            used += len(t)
        else:
            later.append(n)
    if later:
        out.append("More notes (read with read_note): " + ", ".join(later))
    return "\n\n".join(out)


# ---------- the bot's OWN memory (separate from the user's knowledge notes, which the bot only reads)
import json
import time

MAX_FACTS = 500


class Store:
    """<bot home>/memory/: facts.jsonl (things worth keeping), log.jsonl (recent turns), summary.md (older turns, compacted).
    Plain files on purpose: inspect or edit them by hand; `aihub run NAME -- --memory` prints them."""

    def __init__(self, root=None):
        self.root = root or os.path.join(config.HOME, "memory")
        self.facts_f, self.log_f, self.sum_f = (os.path.join(self.root, n) for n in ("facts.jsonl", "log.jsonl", "summary.md"))

    def _rows(self, path):
        out = []
        try:
            with open(path, encoding="utf-8") as f:
                for ln in f:
                    try:
                        out.append(json.loads(ln))
                    except ValueError:
                        pass
        except OSError:
            pass
        return out

    def _write(self, path, rows):
        os.makedirs(self.root, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, path)

    # facts
    def facts(self):
        return self._rows(self.facts_f)

    def remember(self, text, tags=()):
        text = " ".join(str(text).split())[:500]
        if not text:
            return "nothing to remember"
        rows = self.facts()
        if any(r["text"].lower() == text.lower() for r in rows):
            return "already known"
        rows.append({"id": "%x" % int(time.time() * 1000), "ts": time.time(), "text": text, "tags": list(tags)})
        self._write(self.facts_f, rows[-MAX_FACTS:])
        return "remembered"

    def recall(self, query="", k=8):
        words = {w for w in re.findall(r"\w{3,}", query.lower())}
        rows = self.facts()
        if not words:
            return rows[-k:][::-1]
        scored = sorted(((len(words & set(re.findall(r"\w{3,}", r["text"].lower()))), r["ts"], r) for r in rows), key=lambda x: (x[0], x[1]), reverse=True)
        return [r for s, _, r in scored[:k] if s]

    def forget(self, key):
        rows = self.facts()
        keep = [r for r in rows if r["id"] != key and key.lower() not in r["text"].lower()]
        self._write(self.facts_f, keep)
        return "forgot %d" % (len(rows) - len(keep))

    # conversation log + rolling summary
    def log(self, role, text):
        rows = self._rows(self.log_f)
        rows.append({"ts": time.time(), "role": role, "text": str(text)[:8000]})
        self._write(self.log_f, rows)

    def recent(self, n=12):
        rows = self._rows(self.log_f)[-n:]
        while rows and rows[0]["role"] != "user":
            rows = rows[1:]
        return rows

    def summary(self):
        return _read(self.sum_f).strip()

    def compact(self, summarize, keep=10, limit=40):
        """When the log passes `limit` turns, fold all but the last `keep` into summary.md using summarize(text) -> str."""
        rows = self._rows(self.log_f)
        if len(rows) <= limit:
            return False
        old, rest = rows[:-keep], rows[-keep:]
        while rest and rest[0]["role"] != "user":
            old.append(rest.pop(0))
        text = "Earlier summary:\n%s\n\nNew conversation:\n%s" % (self.summary() or "(none)", "\n".join("%s: %s" % (r["role"], r["text"]) for r in old))
        new = (summarize(text) or "").strip()
        if not new:
            return False
        os.makedirs(self.root, exist_ok=True)
        with open(self.sum_f, "w", encoding="utf-8") as f:
            f.write(new[:6000] + "\n")
        self._write(self.log_f, rest)
        return True

    def prompt_block(self, query="", max_facts=15):
        out = []
        if self.summary():
            out.append("## summary of earlier conversations\n" + self.summary())
        fs = self.recall(query, max_facts) or self.facts()[-max_facts:]
        if fs:
            out.append("## things I remember\n" + "\n".join("- %s" % r["text"] for r in fs))
        return "\n\n".join(out)

    def dump(self):
        return "facts: %d, log turns: %d, summary: %d chars\n%s" % (len(self.facts()), len(self._rows(self.log_f)), len(self.summary()), self.root)
