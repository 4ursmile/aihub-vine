import os
from dataclasses import dataclass


@dataclass
class Settings:
    data_dir: str = "./aihub-data"
    public_url: str = "http://localhost:8000"
    cache_backend: str = "memory"   # null | memory
    event_flush_rows: int = 50
    event_flush_secs: float = 5.0
    max_upload_mb: int = 200
    open_registration: bool = True

    @classmethod
    def from_env(cls, **over):
        s = cls()
        for k in s.__dataclass_fields__:
            v = os.environ.get("AIHUB_" + k.upper())
            if v is not None:
                t = type(getattr(s, k))
                setattr(s, k, (v.lower() in ("1", "true", "yes")) if t is bool else t(v))
        for k, v in over.items():
            if v is not None:
                setattr(s, k, v)
        return s
