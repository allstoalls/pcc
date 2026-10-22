"""pcc.stdlib.weakref — small self-host compatible subset.

This is a semantic compromise for no-libpython bootstrap: entries hold strong
references on the host runtime, while the C runtime has true weakref support for
compiled code.  The API shape is enough for caches and import-time feature
tests used by pcc itself.
"""
from __future__ import annotations


class ref:
    def __init__(self, obj, callback=None):
        self._obj = obj
        self._callback = callback

    def __call__(self):
        return self._obj

    def __repr__(self):
        return "<weakref at " + hex(id(self)) + ">"


def proxy(obj, callback=None):
    return obj


class WeakValueDictionary:
    def __init__(self, other=None, **kwargs):
        self.data = {}
        if other:
            self.update(other)
        if kwargs:
            self.update(kwargs)

    def __getitem__(self, key):
        return self.data[key]()

    def __setitem__(self, key, value):
        self.data[key] = ref(value)

    def __delitem__(self, key):
        del self.data[key]

    def __contains__(self, key):
        return key in self.data and self.data[key]() is not None

    def get(self, key, default=None):
        if key not in self:
            return default
        return self[key]

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def pop(self, key, default=None):
        if key not in self.data:
            return default
        return self.data.pop(key)()

    def clear(self):
        self.data.clear()

    def update(self, other=None, **kwargs):
        if other is not None:
            items = other.items() if hasattr(other, "items") else other
            for k, v in items:
                self[k] = v
        for k, v in kwargs.items():
            self[k] = v

    def items(self):
        for k in list(self.data.keys()):
            v = self.data[k]()
            if v is not None:
                yield (k, v)

    def keys(self):
        return self.data.keys()

    def values(self):
        for _, v in self.items():
            yield v

    def __len__(self):
        return len(self.data)


class WeakKeyDictionary:
    def __init__(self, other=None, **kwargs):
        self.data = {}
        if other:
            self.update(other)
        if kwargs:
            self.update(kwargs)

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def __delitem__(self, key):
        del self.data[key]

    def __contains__(self, key):
        return key in self.data

    def get(self, key, default=None):
        return self.data.get(key, default)

    def update(self, other=None, **kwargs):
        if other is not None:
            items = other.items() if hasattr(other, "items") else other
            for k, v in items:
                self[k] = v
        for k, v in kwargs.items():
            self[k] = v

    def items(self):
        return self.data.items()

    def keys(self):
        return self.data.keys()

    def values(self):
        return self.data.values()

    def __len__(self):
        return len(self.data)


class WeakSet:
    def __init__(self, data=None):
        self.data = set()
        if data:
            for item in data:
                self.add(item)

    def add(self, item):
        self.data.add(item)

    def discard(self, item):
        self.data.discard(item)

    def __contains__(self, item):
        return item in self.data

    def __iter__(self):
        return iter(self.data)

    def __len__(self):
        return len(self.data)


class finalize:
    def __init__(self, obj, func, *args, **kwargs):
        self._alive = True
        self._func = func
        self._args = args
        self._kwargs = kwargs

    @property
    def alive(self):
        return self._alive

    def __call__(self):
        if not self._alive:
            return None
        self._alive = False
        return self._func(*self._args, **self._kwargs)

    def detach(self):
        self._alive = False
        return (None, self._func, self._args, self._kwargs)
