"""Packages published straight to the hub (`aihub dev publish -i`): the archive is kept in `<data>/direct`, no git anywhere.

  <data>/direct/<name>/<version>.tar.gz      the package folder exactly as the author packed it
  <data>/direct/<name>/<version>.sha256      its digest (also the index `ref` of that version)

The index entry of such a version names `hub://direct` as its repository (`URL`). Installs see that and download the archive from the hub
(`GET /index/packages/{name}/transfer/{version}`) instead of calling git, with or without `-i`. A version is immutable: the same bytes may
be published again (nothing changes), different bytes under the same version are refused."""
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time

from ..core import archive, manifest as M, naming, pkgindex

URL = "hub://direct"
README_MAX_BYTES = pkgindex.README_MAX * 4
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}")


class DirectError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def is_direct(repo):
    """True for a repo dict (or url) that names the hub itself."""
    url = repo.get("url") if isinstance(repo, dict) else repo
    return str(url or "").startswith("hub://")


def sha256_of(path):
    return archive.sha256_file(path)


class Direct:
    def __init__(self, data_dir):
        self.base = os.path.join(data_dir, "direct")
        self.lock = threading.Lock()

    # ----- paths
    def _dir(self, name):
        name = naming.normalize(str(name))
        if not name or name.startswith(".") or "/" in name or "\\" in name:
            raise DirectError("bad package name")
        return os.path.join(self.base, name)

    def path(self, name, version):
        version = str(version)
        if not _VERSION.fullmatch(version):
            raise DirectError("bad version")
        return os.path.join(self._dir(name), version + ".tar.gz")

    def sha(self, name, version):
        """Digest of the stored archive, or "" when this version is not stored."""
        try:
            p = self.path(name, version)
            with open(p[:-len(".tar.gz")] + ".sha256", encoding="ascii") as f:
                got = f.read().strip()
            return got if os.path.isfile(p) and re.fullmatch(r"[0-9a-f]{64}", got) else ""
        except (OSError, DirectError):
            return ""

    def scratch(self):
        """A path for an upload in progress, on the same disk as the store so `put` is a rename."""
        d = os.path.join(self.base, ".tmp")
        os.makedirs(d, exist_ok=True)
        fd, p = tempfile.mkstemp(dir=d, suffix=".upload")
        os.close(fd)
        return p

    # ----- publish
    def inspect(self, upload):
        """Check an uploaded archive without storing it -> (package dict from its aihub.toml, sha256, size, readme text or "").
        Raises DirectError: not a tar.gz, unsafe or oversized content, no (valid) aihub.toml, bad name or version."""
        d = tempfile.mkdtemp(prefix="aihub-direct-")
        try:
            try:
                root = archive.safe_extract(upload, d)
            except Exception as e:                                  # ValueError (unsafe/too big), tarfile.TarError, zlib.error, EOFError...
                raise DirectError("not a usable package archive: %s" % e)
            toml = os.path.join(root, "aihub.toml")
            if root != d:                                           # the archive had one top folder: the CLI would flatten it, we only accept a flat one
                raise DirectError("aihub.toml must be at the top of the archive (pack the package folder's contents, not the folder)")
            if not os.path.isfile(toml):
                raise DirectError("the archive has no aihub.toml at its top")
            try:
                with open(toml, encoding="utf-8") as f:
                    m = M.parse(f.read())
            except (OSError, ValueError, UnicodeError) as e:
                raise DirectError("aihub.toml: %s" % e)
            errs, _ = M.lint(m, root)
            if errs:
                raise DirectError("aihub.toml: " + "; ".join(errs[:3]))
            pkg = m["package"]
            name, ver = naming.normalize(pkg["name"]), str(pkg["version"])
            try:
                naming.validate(name)
            except ValueError as e:
                raise DirectError(str(e))
            if not _VERSION.fullmatch(ver):
                raise DirectError("version %r is not usable (letters, digits and . + _ - only)" % ver)
            readme = ""
            for n in pkgindex.README_NAMES:
                p = os.path.join(root, n)
                if os.path.isfile(p):
                    with open(p, "rb") as f:
                        readme = f.read(README_MAX_BYTES).decode("utf-8", "replace")
                    break
            return dict(pkg, name=name), sha256_of(upload), os.path.getsize(upload), readme
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def same_content(self, name, version, upload):
        """True when the archive stored for this version holds exactly the same files as `upload` (however either was packed)."""
        try:
            return bool(self.sha(name, version)) and archive.content_digest(self.path(name, version)) == archive.content_digest(upload)
        except Exception:
            return False

    def put(self, upload, name, version, sha):
        """Store an inspected upload (moved, not copied). -> True when it is new, False when the identical archive was already there.
        Different bytes under a stored version: DirectError 409."""
        dest = self.path(name, version)
        with self.lock:
            have = self.sha(name, version)
            if have and have != sha:
                raise DirectError("%s %s is already published with different content; bump the version" % (name, version), 409)
            if have:
                os.remove(upload)
                return False
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.replace(upload, dest)
            with open(dest[:-len(".tar.gz")] + ".sha256", "w", encoding="ascii") as f:
                f.write(sha + "\n")
            return True

    # ----- reading back
    def manifest_dir(self, name, version, sha):
        """Temp folder holding the stored aihub.toml and README of a version (what the index entry is built from), or None when the stored
        archive is missing or is not the one `sha` names. The caller removes the folder."""
        if not sha or self.sha(name, version) != sha:
            return None
        p = self.path(name, version)
        toml = archive.read_member(p, "aihub.toml")
        if toml is None:
            return None
        d = tempfile.mkdtemp(prefix="aihub-direct-")
        with open(os.path.join(d, "aihub.toml"), "wb") as f:
            f.write(toml)
        for n in pkgindex.README_NAMES:
            raw = archive.read_member(p, n)
            if raw is not None:
                with open(os.path.join(d, n), "wb") as f:
                    f.write(raw[:README_MAX_BYTES])
                break
        return d

    def remove(self, name, version=None):
        """Delete one stored version, or all of them (unpublish)."""
        try:
            if version:
                p = self.path(name, version)
                for f in (p, p[:-len(".tar.gz")] + ".sha256"):
                    if os.path.isfile(f):
                        os.remove(f)
                d = os.path.dirname(p)
                if os.path.isdir(d) and not os.listdir(d):
                    os.rmdir(d)
            else:
                shutil.rmtree(self._dir(name), ignore_errors=True)
        except (OSError, DirectError):
            pass


# ---------------------------------------------------------------------------------------------- chunked, resumable, parallel upload
CHUNK = 4 * 1024 * 1024               # bytes per chunk (the CLI reads the real value from the session it opens)
SESSION_TTL = 24 * 3600               # an upload nobody finished is removed after a day
MAX_SESSIONS = 8                      # open uploads per person


class Uploads:
    """Upload sessions for archives bigger than a reverse proxy's body cap: the CLI sends numbered chunks (in parallel, in any order,
    any chunk again is harmless), asks which ones arrived (resume), then completes. A session is keyed by (person, size, sha256), so
    starting the same upload again finds the chunks already there. Sessions live in `<data>/direct/.uploads/<id>/` (meta.json + N.part)."""

    def __init__(self, base):
        self.base = os.path.join(base, ".uploads")
        self.chunk_size = CHUNK

    # ----- helpers
    @staticmethod
    def _id(user, size, sha):
        return hashlib.sha256(("%s\0%d\0%s" % (user, size, sha)).encode()).hexdigest()[:32]

    def _dir(self, uid):
        if not re.fullmatch(r"[0-9a-f]{32}", str(uid)):
            raise DirectError("no such upload", 404)
        return os.path.join(self.base, uid)

    def _meta(self, user, uid):
        d = self._dir(uid)
        try:
            with open(os.path.join(d, "meta.json"), encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError):
            raise DirectError("no such upload", 404)
        if m.get("user") != user:
            raise DirectError("no such upload", 404)        # somebody else's session looks like a missing one
        return d, m

    def sweep(self):
        """Remove sessions older than SESSION_TTL (called whenever one starts)."""
        try:
            names = os.listdir(self.base)
        except OSError:
            return
        for n in names:
            d = os.path.join(self.base, n)
            try:
                if time.time() - os.path.getmtime(d) > SESSION_TTL:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass

    # ----- protocol
    def start(self, user, size, sha, limit):
        """-> {upload_id, chunk_size, chunks, received}. Starting the same upload again resumes it."""
        if not isinstance(size, int) or size <= 0:
            raise DirectError("size must be a positive number of bytes")
        if size > limit:
            raise DirectError("archive too large (limit %d MB, admin setting direct_max_mb)" % (limit >> 20), 413)
        if not re.fullmatch(r"[0-9a-f]{64}", str(sha)):
            raise DirectError("sha256 must be 64 hex digits")
        self.sweep()
        uid = self._id(user, size, sha)
        d = os.path.join(self.base, uid)
        if not os.path.isdir(d):
            mine = 0
            for n in os.listdir(self.base) if os.path.isdir(self.base) else []:
                try:
                    with open(os.path.join(self.base, n, "meta.json"), encoding="utf-8") as f:
                        mine += json.load(f).get("user") == user
                except (OSError, ValueError):
                    pass
            if mine >= MAX_SESSIONS:
                raise DirectError("too many unfinished uploads; finish or cancel one", 429)
            os.makedirs(d, exist_ok=True)
            tmp = os.path.join(d, "meta.json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"user": user, "size": size, "sha256": sha, "chunk_size": self.chunk_size, "created": time.time()}, f)
            os.replace(tmp, os.path.join(d, "meta.json"))
        return self.info(user, uid)

    def info(self, user, uid):
        d, m = self._meta(user, uid)
        cs = int(m["chunk_size"])
        chunks = -(-int(m["size"]) // cs)
        got = sorted(int(f[:-5]) for f in os.listdir(d) if f.endswith(".part") and f[:-5].isdigit())
        os.utime(d, None)                                   # activity keeps the session alive
        return {"upload_id": uid, "chunk_size": cs, "chunks": chunks, "size": int(m["size"]), "received": [n for n in got if n < chunks]}

    def put(self, user, uid, n, data, chunk_sha=""):
        d, m = self._meta(user, uid)
        cs, size = int(m["chunk_size"]), int(m["size"])
        chunks = -(-size // cs)
        if not 0 <= n < chunks:
            raise DirectError("chunk %d is outside this upload (0..%d)" % (n, chunks - 1))
        want = cs if n < chunks - 1 else size - cs * (chunks - 1)
        if len(data) != want:
            raise DirectError("chunk %d must be %d bytes, got %d" % (n, want, len(data)))
        if chunk_sha and hashlib.sha256(data).hexdigest() != chunk_sha.lower():
            raise DirectError("chunk %d is damaged in transit (sha256 differs); send it again" % n)
        part = os.path.join(d, "%d.part" % n)
        tmp = part + ".%d.tmp" % threading.get_ident()
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, part)
        os.utime(d, None)

    def assemble(self, user, uid, dest):
        """Join the chunks into the file `dest`, checking size and sha256 against what the session declared."""
        d, m = self._meta(user, uid)
        info = self.info(user, uid)
        missing = [n for n in range(info["chunks"]) if n not in set(info["received"])]
        if missing:
            raise DirectError("chunks missing: %s" % ", ".join(map(str, missing[:20])), 409)
        h, total = hashlib.sha256(), 0
        with open(dest, "wb") as out:
            for n in range(info["chunks"]):
                with open(os.path.join(d, "%d.part" % n), "rb") as f:
                    for b in iter(lambda: f.read(1 << 20), b""):
                        out.write(b)
                        h.update(b)
                        total += len(b)
        if total != int(m["size"]) or h.hexdigest() != m["sha256"]:
            raise DirectError("the assembled archive does not match the size/sha256 announced for this upload; start it again", 409)

    def drop(self, user, uid):
        try:
            d, _ = self._meta(user, uid)
        except DirectError:
            return
        shutil.rmtree(d, ignore_errors=True)
