"""aihub_vault: read AI Hub credential sets (Settings > Credentials, `aihub work cred`) from Python code. Stdlib only.

    import aihub_vault as vault
    raw = vault.bucket("raw")                 # one bucket of an S3 set: endpoint_url, bucket, access_key, secret_key, region
    raw.uri("in/2026/*.csv")                  # s3a://raw-data/in/2026/*.csv
    pd.read_csv(raw.uri("x.csv", "s3"), storage_options=raw.storage_options())
    raw.boto3().list_objects_v2(Bucket=raw.bucket)
    db = vault.credential("warehouse")        # any set by name or id: db["password"], db.kind, db.name

Two modes:
* inside a Works node run (Python, Spark, Trino ... nodes), only the sets GRANTED to that node are visible: the node's own credential set
  and the ones ticked under "Code may read" (`vault` in the work file). The engine hands them over in a private temp file
  (AIHUB_VAULT_FILE) that is deleted when the node ends. Nothing else of the vault can be read from there, and git logins are not given out.
* anywhere else (your own scripts on this machine, as your user), every set is readable, plus git logins through `aihub credential`.
A shared workflow never carries credentials: whoever runs it grants their own sets (`aihub work setup`).
Values are never printed: repr() of a set or bucket masks the secrets."""
import hashlib
import hmac
import json
import os
import subprocess

__all__ = ["credential", "sets", "get", "bucket", "buckets", "s3a_conf", "git_login", "info", "Credential", "Bucket", "VaultError"]
MARK = "__stored__"
SECRETISH = ("secret", "token", "password", "passwd", "key", "auth", "webhook")
NOT_SECRET = ("access_key", "key_id", "name", "public", "public_key")


class VaultError(KeyError):
    def __str__(self):
        return self.args[0] if self.args else "vault error"


def _home():
    return os.environ.get("AIHUB_HOME") or os.path.join(os.path.expanduser("~"), ".aihub")


def _secret_name(k):
    k = k.lower()
    return k not in NOT_SECRET and any(w in k for w in SECRETISH)


def _mask(d):
    return {k: ("***" if _secret_name(k) and v else v) for k, v in d.items()}


class Bucket(dict):
    """One bucket entry of a set: dict access plus helpers for the usual clients."""
    @property
    def name(self):
        return self.get("name") or self.get("bucket") or ""

    @property
    def bucket(self):
        return self.get("bucket") or self.get("name") or ""

    @property
    def endpoint_url(self):
        return self.get("endpoint_url") or ""

    @property
    def access_key(self):
        return self.get("access_key") or ""

    @property
    def secret_key(self):
        return self.get("secret_key") or ""

    @property
    def region(self):
        return self.get("region") or ""

    @property
    def path_style(self):
        v = self.get("path_style")
        if v in (None, ""):
            return bool(self.endpoint_url) and "amazonaws.com" not in self.endpoint_url      # MinIO and most self-hosted stores need it
        return str(v).lower() in ("1", "true", "yes", "on")

    def uri(self, path="", scheme="s3a"):
        return "%s://%s/%s" % (scheme, self.bucket, str(path).lstrip("/"))

    def storage_options(self):
        """For pandas / fsspec / s3fs: pd.read_parquet(b.uri("x", "s3"), storage_options=b.storage_options())."""
        kw = {k: v for k, v in (("endpoint_url", self.endpoint_url), ("region_name", self.region)) if v}
        out = {"key": self.access_key, "secret": self.secret_key}
        if kw:
            out["client_kwargs"] = kw
        if self.path_style:
            out["config_kwargs"] = {"s3": {"addressing_style": "path"}}
        return out

    def env(self):
        """AWS_* variables for tools that read the environment (aws cli, duckdb httpfs, many SDKs)."""
        out = {"AWS_ACCESS_KEY_ID": self.access_key, "AWS_SECRET_ACCESS_KEY": self.secret_key}
        if self.region:
            out["AWS_DEFAULT_REGION"] = out["AWS_REGION"] = self.region
        if self.endpoint_url:
            out["AWS_ENDPOINT_URL"] = out["AWS_ENDPOINT_URL_S3"] = self.endpoint_url
        return out

    def boto3(self, service="s3", **kw):
        import boto3                                     # optional: pip install boto3
        args = {"aws_access_key_id": self.access_key or None, "aws_secret_access_key": self.secret_key or None, "region_name": self.region or None}
        if self.endpoint_url:
            args["endpoint_url"] = self.endpoint_url
        if self.path_style and "config" not in kw:
            from botocore.config import Config
            kw["config"] = Config(s3={"addressing_style": "path"})
        args.update(kw)
        return boto3.client(service, **args)

    def s3a_conf(self, prefix="spark.hadoop."):
        """Per-bucket s3a settings (Hadoop `fs.s3a.bucket.<bucket>.*`), so several buckets on different stores work in one Spark job."""
        b, out = "%sfs.s3a.bucket.%s." % (prefix, self.bucket), {}
        for k, v in (("endpoint", self.endpoint_url), ("access.key", self.access_key), ("secret.key", self.secret_key), ("endpoint.region", self.region)):
            if v:
                out[b + k] = v
        if self.access_key:
            out[b + "aws.credentials.provider"] = "org.apache.hadoop.fs.s3a.SimpleAWSCredentialsProvider"
        out[b + "path.style.access"] = "true" if self.path_style else "false"
        if self.endpoint_url.startswith("http://"):
            out[b + "connection.ssl.enabled"] = "false"
        return out

    def __repr__(self):
        return "Bucket(%r)" % _mask(dict(self))

    __str__ = __repr__


class Credential(dict):
    """A credential set: its fields as a dict (secrets filled in), plus .id / .name / .kind and .buckets for list fields named buckets."""
    def __init__(self, rec):
        super().__init__(rec.get("fields") or {})
        self.id, self.name, self.kind = rec.get("id", ""), rec.get("name", ""), rec.get("kind", "")

    @property
    def buckets(self):
        return [Bucket(e) for e in (self.get("buckets") or []) if isinstance(e, dict)]

    def entries(self, field):
        """The entries of any list field, as dicts."""
        return [dict(e) for e in (self.get(field) or []) if isinstance(e, dict)]

    def __repr__(self):
        shown = {k: ([_mask(e) for e in v if isinstance(e, dict)] if isinstance(v, list) else ("***" if _secret_name(k) and v else v)) for k, v in self.items()}
        return "Credential(%s, kind=%s, %r)" % (self.name, self.kind, shown)

    __str__ = __repr__


# ---- loading

_cache = {}


def _granted():
    return os.environ.get("AIHUB_VAULT_FILE")


def _unseal(master, blob):
    """Same scheme as aihub-ui's secretbox (keystream = HMAC-SHA256(enc, nonce|counter), encrypt-then-MAC)."""
    head, nonce_n, tag_n = b"AHUI" + bytes([1]), 16, 32
    if len(blob) < len(head) + nonce_n + tag_n or not blob.startswith(head):
        raise VaultError("the secrets file is not readable")
    ek = hmac.new(master, b"aihub-ui enc v1", hashlib.sha256).digest()
    mk = hmac.new(master, b"aihub-ui mac v1", hashlib.sha256).digest()
    nonce, ct, tag = blob[len(head):len(head) + nonce_n], blob[len(head) + nonce_n:-tag_n], blob[-tag_n:]
    if not hmac.compare_digest(tag, hmac.new(mk, head + nonce + ct, hashlib.sha256).digest()):
        raise VaultError("the secrets file failed its integrity check")
    ks, i = bytearray(), 0
    while len(ks) < len(ct):
        ks += hmac.new(ek, nonce + i.to_bytes(8, "big"), hashlib.sha256).digest()
        i += 1
    return bytes(a ^ b for a, b in zip(ct, ks))


def _local():
    """{id: rec} of every set on this machine, secrets filled in (direct mode)."""
    ui = os.path.join(_home(), "ui")
    try:
        with open(os.path.join(ui, "credentials.json"), encoding="utf-8") as f:
            sets = json.load(f)
    except (OSError, ValueError):
        return {}
    box = {}
    if os.path.exists(os.path.join(ui, "secrets.bin")):
        try:
            with open(os.path.join(ui, ".key"), "rb") as f:
                key = f.read()
            with open(os.path.join(ui, "secrets.bin"), "rb") as f:
                box = json.loads(_unseal(key, f.read()).decode("utf-8"))
        except (OSError, ValueError) as e:
            raise VaultError("cannot open the encrypted secrets: %s" % e)
    out = {}
    for cid, c in (sets.items() if isinstance(sets, dict) else ()):
        if not isinstance(c, dict) or not isinstance(c.get("fields"), dict):
            continue
        fields = {}
        for k, v in c["fields"].items():
            if isinstance(v, list):
                fields[k] = [{sk: (box.get("cred:%s:%s:%s:%s" % (cid, k, e.get("id"), sk), "") if sv == MARK else sv) for sk, sv in e.items()} for e in v if isinstance(e, dict)]
            else:
                fields[k] = box.get("cred:%s:%s" % (cid, k), "") if v == MARK else v
        out[cid] = {"id": cid, "name": c.get("name", cid), "kind": c.get("kind", ""), "fields": fields}
    return out


def _load():
    g = _granted()
    key = ("grant", g) if g else ("local",)
    if key in _cache:
        return _cache[key]
    if g:
        try:
            with open(g, encoding="utf-8") as f:
                data = json.load(f).get("credentials") or {}
        except (OSError, ValueError):
            data = {}
    else:
        data = _local()
    _cache.clear()
    _cache[key] = data
    return data


def reload():
    """Forget what was read (a long-running script after `aihub work cred set`)."""
    _cache.clear()


def info():
    """-> {"mode": "node" | "local", "sets": [{id, name, kind}]}: what this code can see (no values)."""
    return {"mode": "node" if _granted() else "local", "sets": [{"id": r["id"], "name": r["name"], "kind": r["kind"]} for r in _load().values()]}


def sets(kind=None):
    """Every visible set (of KIND)."""
    return [Credential(r) for r in _load().values() if kind is None or r.get("kind") == kind]


def credential(name):
    """The set called NAME (its id or its name, case does not matter)."""
    data, n = _load(), str(name or "").strip().lower()
    hit = data.get(n) or next((r for r in data.values() if str(r.get("name", "")).lower() == n), None)
    if not hit:
        have = ", ".join(sorted(r["name"] for r in data.values())) or "none"
        raise VaultError("no credential set %r %s (visible: %s)" % (name, "granted to this node; grant it in the node's settings or with aihub work setup" if _granted() else "on this machine", have))
    return Credential(hit)


def get(name, field=None, default=None):
    """One value of a set (or the set itself when FIELD is None)."""
    c = credential(name)
    return c if field is None else c.get(field, default)


def buckets(kind=None):
    """{name: Bucket} of every bucket entry of the visible sets (a set's `buckets` list). A bucket name also finds it."""
    out = {}
    for c in sets(kind):
        for b in c.buckets:
            if b.name and b.name not in out:
                out[b.name] = b
    return out


def bucket(name):
    """The bucket entry called NAME (its entry name, else its bucket name)."""
    all_ = buckets()
    hit = all_.get(name) or next((b for b in all_.values() if b.bucket == name), None)
    if not hit:
        raise VaultError("no bucket %r in the visible credential sets (have: %s)" % (name, ", ".join(sorted(all_)) or "none"))
    return hit


def s3a_conf(names=None, prefix="spark.hadoop."):
    """s3a settings for every visible bucket (or NAMES), ready for SparkSession.builder.config(k, v) / spark-submit --conf."""
    out = {}
    for n, b in buckets().items():
        if names is None or n in names or b.bucket in names:
            out.update(b.s3a_conf(prefix))
    return out


def git_login(url_or_host):
    """{"username", "password"} the aihub Vault keeps for a git host (direct mode only; a node run uses a 'git' credential set instead)."""
    if _granted():
        raise VaultError("git logins are not readable from inside a node run: give the node a 'git' credential set")
    host = str(url_or_host).split("://", 1)[-1].split("/", 1)[0].split("@")[-1]
    cli = os.environ.get("AIHUB_CLI", "aihub").split()
    try:
        p = subprocess.run(cli + ["credential", "get"], input="protocol=https\nhost=%s\n\n" % host, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise VaultError("cannot ask aihub for the login: %s" % e)
    got = dict(ln.split("=", 1) for ln in p.stdout.splitlines() if "=" in ln)
    if not got.get("password"):
        raise VaultError("no login for %s in the aihub Vault (aihub vault add %s)" % (host, host))
    return {"username": got.get("username", ""), "password": got["password"]}
