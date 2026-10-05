import functools
import threading
import time
from collections import OrderedDict


class Cache:
    def get(self, key): return None
    def set(self, key, value, ttl=None): pass
    def delete(self, key): pass
    def invalidate(self, prefix): pass


class NullCache(Cache):
    pass


class MemoryCache(Cache):
    def __init__(self, size=2048):
        self.size, self.d, self.lock = size, OrderedDict(), threading.Lock()

    def get(self, key):
        with self.lock:
            it = self.d.get(key)
            if not it:
                return None
            if it[1] and it[1] < time.time():
                del self.d[key]
                return None
            self.d.move_to_end(key)
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


def init_cache(settings) -> Cache:
    return MemoryCache() if settings.cache_backend == "memory" else NullCache()


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
