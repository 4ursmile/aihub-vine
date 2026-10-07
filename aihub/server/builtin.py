"""Built-in packages (aihub/builtin_packages/<name>/). They are ordinary packages: on startup the server packs each one
and publishes it into its own registry if that version is not there yet, so they are managed, versioned and updated like
any other package. Bump `version` in the package's aihub.toml to roll out a change."""
import logging
import os
import tempfile

from ..core import archive, manifest as M

DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "builtin_packages")
log = logging.getLogger("aihub")


def names():
    """Folder names of the bundled packages (a fresh CLI installs these before any account exists)."""
    try:
        return sorted(d for d in os.listdir(DIR) if os.path.isfile(os.path.join(DIR, d, "aihub.toml")))
    except OSError:
        return []


def _files(root):
    out = []
    for d, dirs, fs in os.walk(root):
        dirs[:] = [x for x in dirs if x != "__pycache__"]
        out += [os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/") for f in fs if not f.endswith(".pyc")]
    return sorted(out)


def is_builtin(name):
    return name in names()


def seed(repos, storage, data_dir):
    """Publish missing built-in package versions. Never raises: a broken bundle must not stop the server."""
    for folder in names():
        root = os.path.join(DIR, folder)
        tmp = None
        try:
            with open(os.path.join(root, "aihub.toml"), encoding="utf-8") as f:
                m = M.parse(f.read())
            pk = m["package"]
            existing = repos.package(pk["name"])
            if existing and any(v["version"] == pk["version"] for v in repos.versions(existing["id"])):
                continue
            fd, tmp = tempfile.mkstemp(suffix=".tar.gz", dir=data_dir)
            os.close(fd)
            archive.build(root, _files(root), tmp)
            sha, size = archive.sha256_file(tmp), os.path.getsize(tmp)
            readme = ""
            if os.path.isfile(os.path.join(root, pk["readme"])):
                with open(os.path.join(root, pk["readme"]), encoding="utf-8") as f:
                    readme = f.read()
            pid = repos.package_upsert(pk["name"], pk["type"], pk["description"], pk["tags"], readme,
                                       visibility=None if existing else "public")
            stored = "%s-%s.tar.gz" % (pk["name"], pk["version"])
            storage.put(pk["name"], stored, tmp)
            tmp = None
            repos.version_add(pid, pk["version"], stored, sha, size, m)
            repos.audit("system", "upload", pk["name"], pk["version"])
            log.info("built-in package %s %s published", pk["name"], pk["version"])
        except Exception:
            log.exception("could not publish built-in package %s", folder)
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
