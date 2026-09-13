"""Owned functools helpers for the native standard-library provider.

LRU caches preserve finite capacity, keyword order, typed keys and statistics.
The cache_info surface currently returns the existing four-element tuple.
"""
from __future__ import annotations

from threading import RLock

_CACHE_LOCK = RLock()
WRAPPER_ASSIGNMENTS = ("__module__", "__name__", "__qualname__", "__doc__", "__annotations__", "__type_params__")
WRAPPER_UPDATES = ("__dict__",)


def update_wrapper(wrapper, wrapped, assigned=WRAPPER_ASSIGNMENTS, updated=WRAPPER_UPDATES):
    for name in assigned:
        try:
            value = getattr(wrapped, name)
        except AttributeError:
            continue
        setattr(wrapper, name, value)
    for name in updated:
        getattr(wrapper, name).update(getattr(wrapped, name, {}))
    wrapper.__wrapped__ = wrapped
    return wrapper


def wraps(wrapped, assigned=WRAPPER_ASSIGNMENTS, updated=WRAPPER_UPDATES):
    def _decorate(fn):
        return update_wrapper(fn, wrapped, assigned, updated)
    return _decorate


def reduce(fn, iterable, *initial):
    it = iter(iterable)
    if initial:
        acc = initial[0]
    else:
        acc = next(it)
    for item in it:
        acc = fn(acc, item)
    return acc


class partial:
    """Skeleton ``functools.partial`` — binds leading positional args."""

    def __init__(self, fn, *args, **kwargs) -> None:
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def __call__(self, *more_args, **more_kwargs):
        kw = dict(self._kwargs)
        kw.update(more_kwargs)
        return self._fn(*self._args, *more_args, **kw)


def _cache_key(args, kwargs, typed):
    # Keep keyword insertion order, and distinguish single int/str fast keys
    # from the general tuple keys as CPython does when typed=False.
    ordered = tuple(kwargs.items())
    if typed:
        return (args, ordered, tuple(type(value) for value in args),
                tuple(type(value) for value in kwargs.values()))
    if not kwargs and len(args) == 1 and type(args[0]) in (int, str):
        return args[0]
    return args, ordered


def lru_cache(maxsize=128, typed=False):
    """Cache results, evicting the least recently used finite-cache entry."""
    if isinstance(maxsize, int):
        if maxsize < 0:
            maxsize = 0
    elif maxsize is not None and not callable(maxsize):
        raise TypeError("Expected first argument to be an integer, a callable, or None")

    def _decorate(fn):
        cache: dict = {}
        stats = {"hits": 0, "misses": 0}

        def wrapper(*args, **kwargs):
            if maxsize == 0:
                with _CACHE_LOCK:
                    stats["misses"] += 1
                return fn(*args, **kwargs)
            key = _cache_key(args, kwargs, typed)
            with _CACHE_LOCK:
                if key in cache:
                    if maxsize is None:
                        value = cache[key]
                    else:
                        value = cache.pop(key)
                        cache[key] = value
                    stats["hits"] += 1
                    return value
                stats["misses"] += 1
            # User code may recurse or run concurrently. It runs outside the
            # cache lock; preserve any entry installed by another invocation.
            value = fn(*args, **kwargs)
            with _CACHE_LOCK:
                if key not in cache:
                    if maxsize is not None and len(cache) >= maxsize:
                        del cache[next(iter(cache))]
                    cache[key] = value
            return value

        def cache_info():
            with _CACHE_LOCK:
                return (stats["hits"], stats["misses"], maxsize, len(cache))

        def cache_clear():
            with _CACHE_LOCK:
                cache.clear()
                stats["hits"] = 0
                stats["misses"] = 0

        def cache_parameters():
            return {"maxsize": maxsize, "typed": typed}

        wrapper.cache_info = cache_info
        wrapper.cache_clear = cache_clear
        wrapper.cache_parameters = cache_parameters
        return wraps(fn)(wrapper)

    if callable(maxsize):
        fn = maxsize
        maxsize = 128
        return _decorate(fn)
    return _decorate


def cache(fn):
    """Shorthand for ``@lru_cache(maxsize=None)``."""
    return lru_cache(maxsize=None)(fn)


class cached_property:
    def __init__(self, fn) -> None:
        self.fn = fn
        self.__name__ = getattr(fn, "__name__", "")
        self.__doc__ = getattr(fn, "__doc__", None)

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        value = self.fn(obj)
        setattr(obj, self.__name__, value)
        return value


def total_ordering(cls):
    if "__le__" not in cls.__dict__:
        def __le__(self, other):
            return self < other or self == other
        cls.__le__ = __le__
    if "__gt__" not in cls.__dict__:
        def __gt__(self, other):
            return not (self < other or self == other)
        cls.__gt__ = __gt__
    if "__ge__" not in cls.__dict__:
        def __ge__(self, other):
            return not (self < other)
        cls.__ge__ = __ge__
    return cls


def cmp_to_key(mycmp):
    class K:
        def __init__(self, obj) -> None:
            self.obj = obj

        def __lt__(self, other):
            return mycmp(self.obj, other.obj) < 0

        def __gt__(self, other):
            return mycmp(self.obj, other.obj) > 0

        def __eq__(self, other):
            return mycmp(self.obj, other.obj) == 0

        def __le__(self, other):
            return mycmp(self.obj, other.obj) <= 0

        def __ge__(self, other):
            return mycmp(self.obj, other.obj) >= 0

        def __ne__(self, other):
            return mycmp(self.obj, other.obj) != 0
    return K
