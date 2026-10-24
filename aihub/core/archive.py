import gzip
import hashlib
import io
import os
import tarfile
import zipfile

MAX_TOTAL = 500 * 1024 * 1024
MAX_FILES = 20000


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _plain(ti):
    """Only what matters stays: name, content, executable bit. No timestamps or owners, so the same files always give the same bytes."""
    ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname = 0, 0, 0, "", ""
    ti.mode = 0o755 if ti.isdir() or ti.mode & 0o111 else 0o644
    return ti


def build(root, files, dest):
    """A reproducible .tar.gz: sorted members, normalized metadata, gzip header without a timestamp. Packing an unchanged folder twice
    gives identical files (so a re-publish is recognised as the same release)."""
    with open(dest, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="", compresslevel=9) as g, \
            tarfile.open(fileobj=g, mode="w", format=tarfile.GNU_FORMAT) as t:
        for f in sorted(files):
            t.add(os.path.join(root, f), arcname=f, recursive=False, filter=_plain)
    return dest


def content_digest(path):
    """Digest of what a .tar.gz CONTAINS (file names, bytes, executable bit, symlink targets), not of how it was packed. Two archives of the
    same files are equal here even when built on different machines, Python or zlib versions."""
    h = hashlib.sha256()
    with tarfile.open(path) as t:
        for m in sorted((m for m in t.getmembers() if not m.isdir()), key=lambda m: m.name):
            name = m.name[2:] if m.name.startswith("./") else m.name
            if m.issym() or m.islnk():
                body = "L:" + m.linkname
            else:
                fh, f = hashlib.sha256(), t.extractfile(m)
                for b in iter(lambda: f.read(1 << 20), b""):
                    fh.update(b)
                body = "F:%s:%d" % (fh.hexdigest(), 1 if m.mode & 0o111 else 0)
            h.update(("%s\0%s\n" % (name, body)).encode("utf-8", "surrogateescape"))
    return h.hexdigest()


def _names(path):
    if path.endswith(".zip") or path.endswith(".whl"):
        with zipfile.ZipFile(path) as z:
            return [i.filename for i in z.infolist()]
    with tarfile.open(path) as t:
        return t.getnames()


def read_member(path, name):
    """Read file `name` from archive root (or single top dir). None if absent."""
    names = _names(path)
    cand = [n for n in names if n == name or (n.count("/") == 1 and n.endswith("/" + name))]
    if not cand:
        return None
    n = sorted(cand, key=len)[0]
    if path.endswith((".zip", ".whl")):
        with zipfile.ZipFile(path) as z:
            return z.read(n)
    with tarfile.open(path) as t:
        f = t.extractfile(n)
        return f.read() if f else None


def _safe(dest, name):
    full = os.path.realpath(os.path.join(dest, name))
    if not (full == os.path.realpath(dest) or full.startswith(os.path.realpath(dest) + os.sep)):
        raise ValueError("unsafe path in archive: %s" % name)
    return full


def safe_extract(path, dest):
    os.makedirs(dest, exist_ok=True)
    total = 0
    if path.endswith((".zip", ".whl")):
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            if len(infos) > MAX_FILES:
                raise ValueError("too many files")
            for i in infos:
                _safe(dest, i.filename)
                total += i.file_size
                if total > MAX_TOTAL:
                    raise ValueError("archive too large")
            z.extractall(dest)
    else:
        with tarfile.open(path) as t:
            ms = t.getmembers()
            if len(ms) > MAX_FILES:
                raise ValueError("too many files")
            for m in ms:
                _safe(dest, m.name)
                if m.issym() or m.islnk():
                    _safe(dest, os.path.join(os.path.dirname(m.name), m.linkname))
                if not (m.isfile() or m.isdir() or m.issym()):
                    raise ValueError("unsupported member: %s" % m.name)
                total += m.size
                if total > MAX_TOTAL:
                    raise ValueError("archive too large")
            t.extractall(dest, members=ms)
    # flatten single top-level dir
    items = os.listdir(dest)
    if len(items) == 1 and os.path.isdir(os.path.join(dest, items[0])):
        return os.path.join(dest, items[0])
    return dest
