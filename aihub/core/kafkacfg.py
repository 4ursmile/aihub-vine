"""Kafka connection settings shared by the CLI (producer) and the server (consumer). Standard library only.

`resolve(get)` builds the settings from any layered lookup: `get(name)` returns the first non-empty value for that field, or "".
The CLI layers environment > config.json > defaults.json, the server layers environment > admin settings > defaults.json.
`PUBLIC` fields are safe to hand to clients (`/client-config`); credentials never leave the machine that owns them.
"""
FIELDS = ("bootstrap", "topic", "security_protocol", "sasl_mechanism", "username", "password", "ssl_cafile")
PUBLIC = ("bootstrap", "topic", "security_protocol", "sasl_mechanism")
DEFAULT_TOPIC = "aihub.events"


def resolve(get):
    """-> dict of FIELDS ('' when unset), or {} when no bootstrap server is configured (Kafka is then off)."""
    cfg = {k: str(get(k) or "").strip() for k in FIELDS}
    if not cfg["bootstrap"]:
        return {}
    cfg["topic"] = cfg["topic"] or DEFAULT_TOPIC
    return cfg


def client_kwargs(cfg):
    """kafka-python keyword arguments for a producer or consumer (security options only when set)."""
    kw = {"bootstrap_servers": [b.strip() for b in cfg["bootstrap"].split(",") if b.strip()]}
    if cfg.get("security_protocol"):
        kw["security_protocol"] = cfg["security_protocol"].upper()
    if cfg.get("sasl_mechanism"):
        kw["sasl_mechanism"] = cfg["sasl_mechanism"].upper()
        kw["sasl_plain_username"] = cfg.get("username") or None
        kw["sasl_plain_password"] = cfg.get("password") or None
    if cfg.get("ssl_cafile"):
        kw["ssl_cafile"] = cfg["ssl_cafile"]
    return kw


def hosts(cfg):
    """[(host, port)] of the bootstrap servers, for a plain reachability check."""
    out = []
    for b in (cfg.get("bootstrap") or "").split(","):
        b = b.strip()
        if not b:
            continue
        host, _, port = b.rpartition(":")
        out.append((host, int(port)) if host and port.isdigit() else (b, 9092))
    return out
