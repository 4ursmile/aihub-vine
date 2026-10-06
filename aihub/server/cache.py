"""Cache layer. Built-in memory (default), Redis, or none, chosen in settings.

Values must be JSON-serialisable (they are lists/dicts straight from the API).
`invalidate(prefix)` drops every key whose name starts with `prefix`. On Redis it bumps a generation counter
instead of scanning keys, so it is O(1), atomic, and visible to every worker at once.
A cache failure must never break a request: all backends swallow errors and behave as a miss.
"""
import functools
import json
import logging
import threading
import time
from collections import OrderedDict

log = logging.getLogger("aihub.cache")


class Cache:
    name = "none"
    def get(self, key): return None
    def set(self, key, value, ttl=None): pass
    def delete(self, key): pass
    def invalidate(self, prefix): pass
    def health(self): return True
    def stats(self): return {"backend": self.name}


class NullCache(Cache):
    pass


class MemoryCache(Cache):
    name = "memory"

    def __init__(self, size=2048):
        self.size, self.d, self.lock = size, OrderedDict(), threading.Lock()
        self.hits = self.misses = 0

    def get(self, key):
        with self.lock:
            it = self.d.get(key)
            if it is None or (it[1] and it[1] < time.time()):
                if it is not None:
                    del self.d[key]
                self.misses += 1
                return None
            self.d.move_to_end(key)
            self.hits += 1
            return it[0]

    def set(self, key, value, ttl=None):
        with self.lock:
            self.d[key] = (value, time.time() + ttl if ttl else None)
            self.d.move_to_end(key)
            while len(self.d) > self.size:
                self.d.popitem(last=False)

    def delete(self, key):
        with self.lock:
            self.d.pop(key, None)

    def invalidate(self, prefix):
        with self.lock:
            for k in [k for k in self.d if k.startswith(prefix)]:
                del self.d[k]

    def stats(self):
        with self.lock:
            return {"backend": self.name, "items": len(self.d), "hits": self.hits, "misses": self.misses}


class RedisCache(Cache):
    name = "redis"

    def __init__(self, url, prefix="aihub:", default_ttl=60):
        try:
            import redis
        except ImportError as e:
            raise RuntimeError("Redis cache needs: pip install redis  (%s)" % e)
        self.r = redis.Redis.from_url(url, socket_timeout=1.5, socket_connect_timeout=1.5, health_check_interval=30)
        self.prefix, self.default_ttl = prefix, default_ttl
        self.hits = self.misses = self.errors = 0
        self.r.ping()                                   # fail fast at startup if the URL/credentials are wrong

    def _group(self, key):
        return key.split(":", 1)[0]                     # "pkg:list:..." -> "pkg"

    def _gen(self, group):
        v = self.r.get("%sgen:%s" % (self.prefix, group))
        return int(v) if v else 0

    def _k(self, key):
        return "%sc:%d:%s" % (self.prefix, self._gen(self._group(key)), key)

    def get(self, key):
        try:
            raw = self.r.get(self._k(key))
        except Exception as e:
            self.errors += 1; log.warning("redis get failed: %s", e); return None
        if raw is None:
            self.misses += 1
            return None
        self.hits += 1
        return json.loads(raw)

    def set(self, key, value, ttl=None):
        try:
            self.r.set(self._k(key), json.dumps(value, default=str), ex=int(ttl or self.default_ttl))
        except Exception as e:
            self.errors += 1; log.warning("redis set failed: %s", e)

    def delete(self, key):
        try:
            self.r.delete(self._k(key))
        except Exception as e:
            self.errors += 1; log.warning("redis delete failed: %s", e)

    def invalidate(self, prefix):
        try:
            self.r.incr("%sgen:%s" % (self.prefix, self._group(prefix)))      # old generation's keys are now unreachable and expire by TTL
        except Exception as e:
            self.errors += 1; log.warning("redis invalidate failed: %s", e)

    def health(self):
        try:
            return bool(self.r.ping())
        except Exception:
            return False

    def stats(self):
        return {"backend": self.name, "hits": self.hits, "misses": self.misses, "errors": self.errors, "healthy": self.health()}


def init_cache(settings) -> Cache:
    if settings.cache_backend == "redis":
        return RedisCache(settings.redis_url, settings.redis_prefix, settings.cache_ttl)
    if settings.cache_backend == "memory":
        return MemoryCache(settings.cache_max_items)
    return NullCache()


def cached(cache: Cache, prefix: str, ttl: float = 30):
    """Wrap a function; key = prefix + args. Use cache.invalidate(prefix) on writes."""
    def deco(fn):
        @functools.wraps(fn)
        def w(*a, **kw):
            k = "%s:%r:%r" % (prefix, a, sorted(kw.items()))
            v = cache.get(k)
            if v is None:
                v = fn(*a, **kw)
                cache.set(k, v, ttl)
            return v
        return w
    return deco
