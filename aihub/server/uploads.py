"""Resumable chunked uploads, for hubs behind a reverse proxy that caps request bodies (nginx client_max_body_size, Cloudflare, ...).

A session lives on disk under <data_dir>/uploads/<id>/ as meta.json plus one <n>.part file per chunk. Chunks are written
atomically (tmp file, then rename), so re-sending a chunk is harmless and a crash never leaves a half-written part.
Nothing here knows about packages: `assemble` produces one verified file and the caller publishes it exactly like a plain upload.
"""
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import time

TTL_SECS = 24 * 3600          # an idle session is swept after this long
MAX_SESSIONS_PER_USER = 5
_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


class UploadError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


class UploadStore:
    def __init__(self, data_dir, chunk_size):
        self.root = os.path.join(data_dir, "uploads")
        self.data_dir = data_dir
        self.chunk_size = chunk_size
        os.makedirs(self.root, exist_ok=True)

    # ---- helpers
    def _dir(self, uid):
        if not _ID.match(uid or ""):
            raise UploadError(404, "upload not found")
        return os.path.join(self.root, uid)

    def _meta(self, uid, user_id):
        d = self._dir(uid)
        try:
            with open(os.path.join(d, "meta.json")) as f:
                m = json.load(f)
        except (OSError, ValueError):
            raise UploadError(404, "upload not found")
        if m["user"] != user_id:                       # same answer as "missing": never confirm another user's session
            raise UploadError(404, "upload not found")
        return m

    def _touch(self, uid):
        os.utime(self._dir(uid), None)

    def count(self, m):
        return (m["size"] + m["chunk_size"] - 1) // m["chunk_size"] if m["size"] else 0

    def expected_len(self, m, n):
        return min(m["chunk_size"], m["size"] - n * m["chunk_size"])

    def sweep(self):
        now = time.time()
        for name in os.listdir(self.root):
            p = os.path.join(self.root, name)
            try:
                if now - os.path.getmtime(p) > TTL_SECS:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass

    # ---- lifecycle
    def create(self, user_id, filename, size, sha256, limit, manifest=None, visibility=None):
        if not isinstance(size, int) or size <= 0:
            raise UploadError(400, "size must be a positive integer")
        if size > limit:
            raise UploadError(413, "upload too large")
        if not re.fullmatch(r"[0-9a-f]{64}", str(sha256 or "")):
            raise UploadError(400, "sha256 must be 64 hex characters")
        self.sweep()
        mine = 0
        for name in os.listdir(self.root):
            try:
                with open(os.path.join(self.root, name, "meta.json")) as f:
                    mine += json.load(f).get("user") == user_id
            except (OSError, ValueError):
                pass
        if mine >= MAX_SESSIONS_PER_USER:
            raise UploadError(429, "too many unfinished uploads; finish or abort one first")
        uid = secrets.token_urlsafe(18)
        d = self._dir(uid)
        os.makedirs(d)
        meta = {"user": user_id, "filename": filename, "size": size, "sha256": sha256, "chunk_size": self.chunk_size,
                "manifest": manifest, "visibility": visibility, "created": time.time()}
        with open(os.path.join(d, "meta.json"), "w") as f:
            json.dump(meta, f)
        return uid, meta

    def status(self, uid, user_id):
        m = self._meta(uid, user_id)
        d = self._dir(uid)
        got = sorted(int(f[:-5]) for f in os.listdir(d) if f.endswith(".part") and f[:-5].isdigit())
        return m, got

    def put_chunk(self, uid, user_id, n, data, chunk_sha=None):
        m = self._meta(uid, user_id)
        if not 0 <= n < self.count(m):
            raise UploadError(400, "chunk index out of range (0..%d)" % (self.count(m) - 1))
        if len(data) != self.expected_len(m, n):
            raise UploadError(400, "chunk %d must be exactly %d bytes" % (n, self.expected_len(m, n)))
        if chunk_sha and hashlib.sha256(data).hexdigest() != chunk_sha.lower():
            raise UploadError(400, "chunk %d checksum mismatch; resend it" % n)
        d = self._dir(uid)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, os.path.join(d, "%06d.part" % n))
        self._touch(uid)

    def assemble(self, uid, user_id):
        """-> (tmp_path, meta). Verifies every chunk is present and the whole file matches the declared size and sha256."""
        m, got = self.status(uid, user_id)
        missing = sorted(set(range(self.count(m))) - set(got))
        if missing:
            raise UploadError(400, "missing chunks: %s%s" % (missing[:20], " ..." if len(missing) > 20 else ""))
        h, total = hashlib.sha256(), 0
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, suffix=".upload")
        try:
            with os.fdopen(fd, "wb") as out:
                for n in range(self.count(m)):
                    with open(os.path.join(self._dir(uid), "%06d.part" % n), "rb") as src:
                        for b in iter(lambda: src.read(1 << 20), b""):
                            h.update(b)
                            total += len(b)
                            out.write(b)
            if total != m["size"] or h.hexdigest() != m["sha256"]:
                raise UploadError(400, "assembled file does not match the declared size/sha256; upload again")
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        return tmp, m

    def abort(self, uid, user_id):
        self._meta(uid, user_id)
        shutil.rmtree(self._dir(uid), ignore_errors=True)
