"""Shared by the Read file and Read from git nodes: read a file or list a folder under a root, safely."""
import json
import os
import re

HOME = os.path.expanduser("~")
MAX_FILE = 5_000_000
# places a work (possibly one somebody else shared) must never read: logins, keys, tokens, this UI's own secrets
SENSITIVE_DIRS = [".ssh", ".aws", ".gnupg", ".kube", ".azure", ".docker", ".config/gcloud", ".config/gh", ".password-store", "Library/Keychains"]
SENSITIVE_NAME = re.compile(r"(^\.env($|\.)|^id_(rsa|dsa|ecdsa|ed25519)|\.pem$|\.p12$|\.pfx$|\.key$|^\.netrc$|^_netrc$|^\.git-credentials|^credentials(\.|$)|^secrets?\.|\.kdbx$|^\.npmrc$|^\.pypirc$|^shadow$)", re.I)


def blocked(path):
    """Reason string when PATH (real path) is off limits, else ''."""
    p = os.path.realpath(path)
    rel = os.path.relpath(p, HOME) if p.startswith(HOME + os.sep) else None
    if rel is not None:
        for d in SENSITIVE_DIRS:
            if rel == d or rel.startswith(d + os.sep):
                return "~/%s holds logins or keys" % d
        if rel.startswith(".aihub" + os.sep) and not re.match(r"^\.aihub/(workingspace|knowledge)(/|$)", rel.replace(os.sep, "/")):
            return "~/.aihub (except workingspace and knowledge) holds logins and secrets"
        if re.match(r"^\.claude(/settings[^/]*\.json|\.json)$", rel.replace(os.sep, "/")) or rel == ".claude.json" or ".credentials" in rel:
            return "Claude settings and credentials are private"
    if p.startswith(("/etc/shadow", "/etc/sudoers", "/private/etc/sudoers")):
        return "system secrets"
    if SENSITIVE_NAME.search(os.path.basename(p)):
        return "%s looks like a secret or key file" % os.path.basename(p)
    return ""


def read_text(path, limit):
    if os.path.getsize(path) > MAX_FILE:
        raise ValueError("file is larger than %d MB" % (MAX_FILE // 1_000_000))
    with open(path, "rb") as f:
        raw = f.read(MAX_FILE)
    if b"\0" in raw[:8192]:
        raise ValueError("binary file (not text)")
    t = raw.decode("utf-8", "replace")
    return t if len(t) <= limit else t[:limit] + "\n... [cut at %d characters]" % limit


def listing(folder, rel_root, read_files, limit, max_files=10):
    """Lines describing the entries of FOLDER; optionally the text of the first MAX_FILES text files in it."""
    rows, body, used = [], [], 0
    for n in sorted(os.listdir(folder))[:500]:
        full = os.path.join(folder, n)
        if n == ".git":
            continue
        if os.path.isdir(full):
            rows.append(n + "/")
        else:
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            rows.append("%s  (%d bytes)" % (n, size))
            if read_files and len(body) < max_files and used < limit and not blocked(full):
                try:
                    t = read_text(full, limit - used)
                except (ValueError, OSError):
                    continue
                body.append("=== %s ===\n%s" % (os.path.join(rel_root, n) if rel_root else n, t))
                used += len(t)
    out = "\n".join(rows)
    return out + ("\n\n" + "\n\n".join(body) if body else "")


def as_json(text):
    try:
        return json.loads(text)
    except ValueError:
        return None


def pointer(text, workdir):
    """The path a node should work on, found in TEXT (an upstream node's output): the last line that names an existing file or folder
    (`OUTPUT: /x/y`, backticks and quotes are ignored; relative names start in WORKDIR). '' when none."""
    for line in reversed(str(text or "").splitlines()):
        s = re.sub(r"^\s*(output|path|file|result)\s*[:=]\s*", "", line.strip(), flags=re.I).strip().strip("`'\"*")
        if not s or len(s) > 1000 or "\0" in s:
            continue
        p = os.path.expanduser(s)
        if not os.path.isabs(p):
            p = os.path.join(workdir or HOME, p)
        if os.path.exists(p):
            return os.path.realpath(p)
    return ""
