"""Package file storage. LocalStorage (default) or S3Storage (AWS S3, MinIO, Cloudflare R2, Ceph, ...).

The interface never exposes a filesystem path, so callers work identically on both:
  put(package, filename, src_path)        store a local file (it is moved/uploaded, then the source is removed)
  exists / delete(package, filename)
  open(package, filename)                 -> binary file-like object (streams; caller closes)
  local_copy(package, filename)           -> context manager yielding a real local path (S3: temp download)
  url(package, filename, expires)         -> a short-lived direct URL, or None when the server must stream it
  size(package, filename)
"""
import contextlib
import os
import re
import shutil
import tempfile
from abc import ABC, abstractmethod

_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


def _check(package, filename):
    """Package and file names come from user input; refuse anything that could escape its folder or key prefix."""
    # Reject, never "clean up": a name that is not already a plain file name is an error, not a different valid name.
    if not _SAFE.match(package or "") or not _SAFE.match(filename or "") or ".." in package or ".." in filename:
        raise ValueError("bad path")
    return package, filename


class Storage(ABC):
    @abstractmethod
    def put(self, package, filename, src_path): ...
    @abstractmethod
    def exists(self, package, filename): ...
    @abstractmethod
    def delete(self, package, filename): ...
    @abstractmethod
    def open(self, package, filename): ...
    @abstractmethod
    def size(self, package, filename): ...

    def url(self, package, filename, expires=300):
        return None

    @contextlib.contextmanager
    def local_copy(self, package, filename):
        """A real file on disk for tools that need one (archive inspection). Cleaned up on exit."""
        d = tempfile.mkdtemp(prefix="aihub-")
        dst = os.path.join(d, os.path.basename(filename))
        try:
            with self.open(package, filename) as src, open(dst, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            yield dst
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def health(self):
        return True


class LocalStorage(Storage):
    def __init__(self, root):
        self.root = os.path.realpath(root)
        os.makedirs(self.root, exist_ok=True)

    def _path(self, package, filename):
        package, filename = _check(package, filename)
        p = os.path.realpath(os.path.join(self.root, package, filename))
        if not p.startswith(self.root + os.sep):
            raise ValueError("bad path")
        return p

    path = _path                      # kept for older callers

    def put(self, package, filename, src_path):
        dst = self._path(package, filename)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src_path, dst)
        return dst

    def exists(self, package, filename):
        try:
            return os.path.isfile(self._path(package, filename))
        except ValueError:
            return False

    def delete(self, package, filename):
        if self.exists(package, filename):
            os.remove(self._path(package, filename))

    def open(self, package, filename):
        return open(self._path(package, filename), "rb")

    def size(self, package, filename):
        return os.path.getsize(self._path(package, filename))

    @contextlib.contextmanager
    def local_copy(self, package, filename):
        yield self._path(package, filename)          # already local: no copy

    def health(self):
        return os.access(self.root, os.W_OK)


class S3Storage(Storage):
    def __init__(self, bucket, region="us-east-1", endpoint="", access_key="", secret_key="", prefix="packages/", path_style=True):
        try:
            import boto3
            from botocore.config import Config
        except ImportError as e:
            raise RuntimeError("S3 storage needs: pip install boto3  (%s)" % e)
        self.bucket, self.prefix = bucket, (prefix.strip("/") + "/") if prefix.strip("/") else ""
        kw = dict(region_name=region, config=Config(s3={"addressing_style": "path" if path_style else "auto"},
                                                    retries={"max_attempts": 4, "mode": "standard"}, signature_version="s3v4"))
        if endpoint:
            kw["endpoint_url"] = endpoint
        if access_key:                                  # otherwise boto3's normal chain: env vars, shared config, instance/IRSA role
            kw.update(aws_access_key_id=access_key, aws_secret_access_key=secret_key)
        self.s3 = boto3.client("s3", **kw)

    def _key(self, package, filename):
        package, filename = _check(package, filename)
        return "%s%s/%s" % (self.prefix, package, filename)

    def put(self, package, filename, src_path):
        # Encryption at rest is a bucket-level policy (AWS encrypts by default); not forced here so MinIO/R2 work unchanged.
        self.s3.upload_file(src_path, self.bucket, self._key(package, filename), ExtraArgs={"ContentType": "application/octet-stream"})
        os.remove(src_path)                             # same contract as LocalStorage: the source is consumed

    def exists(self, package, filename):
        from botocore.exceptions import ClientError
        try:
            self.s3.head_object(Bucket=self.bucket, Key=self._key(package, filename))
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        except ValueError:
            return False

    def delete(self, package, filename):
        self.s3.delete_object(Bucket=self.bucket, Key=self._key(package, filename))

    def open(self, package, filename):
        return self.s3.get_object(Bucket=self.bucket, Key=self._key(package, filename))["Body"]

    def size(self, package, filename):
        return self.s3.head_object(Bucket=self.bucket, Key=self._key(package, filename))["ContentLength"]

    def url(self, package, filename, expires=300):
        return self.s3.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": self._key(package, filename),
                                                                    "ResponseContentDisposition": 'attachment; filename="%s"' % os.path.basename(filename)},
                                              ExpiresIn=expires)

    def health(self):
        try:
            self.s3.head_bucket(Bucket=self.bucket)
            return True
        except Exception:
            return False


def init_storage(settings):
    if settings.storage_backend == "s3":
        return S3Storage(settings.s3_bucket, settings.s3_region, settings.s3_endpoint, settings.s3_access_key,
                         settings.s3_secret_key, settings.s3_prefix, settings.s3_path_style)
    return LocalStorage(os.path.join(settings.data_dir, "files"))
