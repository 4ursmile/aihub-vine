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


def build(root, files, dest):
    with tarfile.open(dest, "w:gz") as t:
        for f in files:
            t.add(os.path.join(root, f), arcname=f, recursive=False)
    return dest


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
