"""Server settings. One place decides which backends run.

Sources, highest priority first:  command-line flags  >  real environment  >  .env file  >  defaults.
The .env file is looked up at $AIHUB_ENV_FILE, then ./.env, then <data_dir>/.env. Every key is AIHUB_<NAME>.
Secrets can also come from Docker/Kubernetes secret files: set AIHUB_<NAME>_FILE=/run/secrets/xyz.
"""
import os
from dataclasses import dataclass, field, fields

SECRET_FIELDS = {"database_url", "redis_url", "s3_secret_key", "s3_access_key"}


def parse_env_file(path):
    """Tiny .env reader: KEY=value, # comments, optional quotes, optional `export `. No variable expansion."""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:]
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                elif " #" in v:
                    v = v.split(" #", 1)[0].rstrip()
                out[k] = v
    except OSError:
        pass
    return out


@dataclass
class Settings:
    # --- server
    data_dir: str = "./aihub-data"
    public_url: str = "http://localhost:8000"
    open_registration: bool = True
    sync_enabled: bool = True             # poll Langfuse + the git index (disable in tests)
    seed_builtin: bool = False             # publish the built-in packages (aihub-guide, aihub-package) into the registry at startup
    log_level: str = "INFO"
    # --- database:  sqlite (default, a file in data_dir)  |  postgres
    db_backend: str = "sqlite"
    sqlite_path: str = ""                 # default: <data_dir>/aihub.db
    database_url: str = ""                # postgres: postgresql://user:pass@host:5432/dbname  (wins over the parts below)
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_name: str = "aihub"
    pg_user: str = "aihub"
    pg_password: str = ""
    pg_sslmode: str = "prefer"
    pg_pool_size: int = 10
    # --- cache:  memory (built in, default)  |  redis  |  none
    cache_backend: str = "memory"
    cache_ttl: int = 20                   # seconds for list/search results
    cache_max_items: int = 2048           # memory backend only
    redis_url: str = ""                   # redis://[:password@]host:6379/0   (rediss:// for TLS)
    redis_prefix: str = "aihub:"
    # --- file storage:  local (default)  |  s3  (AWS S3, MinIO, R2, Ceph, ...)
    storage_backend: str = "local"
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_endpoint: str = ""                 # empty = AWS; set for MinIO / R2 / Ceph
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_prefix: str = "packages/"
    s3_path_style: bool = True            # MinIO needs True; AWS works either way
    # --- usage events
    event_flush_rows: int = 50
    event_flush_secs: float = 5.0
    # --- where these values came from (filled by load(); not user-settable)
    env_file: str = field(default="", repr=False)
    # real environment + .env file, as load() saw them (None = not loaded: env_get() reads os.environ)
    _env_map: object = field(default=None, repr=False, compare=False)

    # ---------------------------------------------------------------- loading
    @classmethod
    def load(cls, env=None, env_file=None, **overrides):
        env = dict(os.environ if env is None else env)
        data_dir = overrides.get("data_dir") or env.get("AIHUB_DATA_DIR") or "./aihub-data"
        cands = [env_file, env.get("AIHUB_ENV_FILE"), ".env", os.path.join(data_dir, ".env")]
        used, file_vals = "", {}
        for c in cands:
            if c and os.path.isfile(c):
                file_vals, used = parse_env_file(c), c
                break
        merged = dict(file_vals)
        merged.update({k: v for k, v in env.items() if k.startswith("AIHUB_")})   # real environment beats .env
        s = cls()
        s.env_file = used
        s._env_map = {**file_vals, **env}                                          # env beats .env for every key (also LANGFUSE_*)
        for f in fields(cls):
            if f.name in ("env_file", "_env_map"):
                continue
            key = "AIHUB_" + f.name.upper()
            val = merged.get(key)
            fkey = key + "_FILE"                                                   # docker/k8s secret file
            if val is None and merged.get(fkey):
                try:
                    val = open(merged[fkey], encoding="utf-8").read().strip()
                except OSError as e:
                    raise ValueError("%s=%s cannot be read: %s" % (fkey, merged[fkey], e))
            if val is not None:
                setattr(s, f.name, cls._coerce(f.name, getattr(s, f.name), val))
        for k, v in overrides.items():
            if v is not None and hasattr(s, k):
                setattr(s, k, v)
        s.validate()
        return s

    # kept for callers/tests written before the .env support existed
    @classmethod
    def from_env(cls, **over):
        return cls.load(**over)

    @staticmethod
    def _coerce(name, current, val):
        if isinstance(current, bool):
            return str(val).strip().lower() in ("1", "true", "yes", "on")
        try:
            return type(current)(str(val).strip())
        except ValueError:
            raise ValueError("AIHUB_%s must be a %s, got %r" % (name.upper(), type(current).__name__, val))

    def env_get(self, name, default=""):
        """A deployment value (LANGFUSE_*, AIHUB_INDEX_URL, AIHUB_CLI_GIT_*, ...) from the real environment or the .env file.
        Environment wins over .env. Settings built without load() read os.environ only."""
        src = self._env_map if self._env_map is not None else os.environ
        v = src.get(name)
        return v if v not in (None, "") else default

    # ---------------------------------------------------------------- derived
    @property
    def sqlite_file(self):
        return self.sqlite_path or os.path.join(self.data_dir, "aihub.db")

    @property
    def postgres_dsn(self):
        if self.database_url:
            return self.database_url
        from urllib.parse import quote
        auth = quote(self.pg_user, safe="") + (":" + quote(self.pg_password, safe="") if self.pg_password else "")
        return "postgresql://%s@%s:%d/%s?sslmode=%s" % (auth, self.pg_host, self.pg_port, quote(self.pg_name, safe=""), self.pg_sslmode)

    def validate(self):
        errs = []
        if self.db_backend not in ("sqlite", "postgres"):
            errs.append("AIHUB_DB_BACKEND must be sqlite or postgres")
        if self.cache_backend not in ("memory", "redis", "none"):
            errs.append("AIHUB_CACHE_BACKEND must be memory, redis or none")
        if self.storage_backend not in ("local", "s3"):
            errs.append("AIHUB_STORAGE_BACKEND must be local or s3")
        if self.cache_backend == "redis" and not self.redis_url:
            errs.append("AIHUB_REDIS_URL is required when AIHUB_CACHE_BACKEND=redis")
        if self.storage_backend == "s3" and not self.s3_bucket:
            errs.append("AIHUB_S3_BUCKET is required when AIHUB_STORAGE_BACKEND=s3")
        if self.db_backend == "postgres" and not (self.database_url or self.pg_password or self.pg_host):
            errs.append("set AIHUB_DATABASE_URL (or AIHUB_PG_HOST / AIHUB_PG_NAME / AIHUB_PG_USER / AIHUB_PG_PASSWORD)")
        if errs:
            raise ValueError("invalid configuration:\n  - " + "\n  - ".join(errs))

    def describe(self):
        """Human-readable summary with secrets masked, safe to log or show an admin."""
        def mask(v):
            if not v:
                return ""
            if "://" in v:                                   # a URL: keep scheme/host/db, redact only the password
                head, rest = v.split("://", 1)
                if "@" not in rest:
                    return v                                 # no credentials in it, nothing to hide
                cred, host = rest.rsplit("@", 1)
                user = cred.split(":", 1)[0]
                return "%s://%s:***@%s" % (head, user, host)
            return "***"
        return {
            "database": {"backend": self.db_backend,
                         "target": self.sqlite_file if self.db_backend == "sqlite" else mask(self.postgres_dsn)},
            "cache": {"backend": self.cache_backend, "target": mask(self.redis_url) if self.cache_backend == "redis" else "in-process"},
            "storage": {"backend": self.storage_backend,
                        "target": (os.path.join(self.data_dir, "files") if self.storage_backend == "local" else
                                   "s3://%s/%s%s" % (self.s3_bucket, self.s3_prefix, " @ " + self.s3_endpoint if self.s3_endpoint else ""))},
            "config_file": self.env_file or "(none — using environment/defaults)",
        }
