"""pcc.py_stdlib.collections — skeleton for pcc's self-host path.

Only the types pcc imports: ``OrderedDict``, ``defaultdict``,
``Counter``, ``deque``, ``namedtuple`` and scoped ``ChainMap`` mappings. Implementations lean on
dict/list primitives pcc already ships natively.
"""
from __future__ import annotations


class OrderedDict(dict):
    """pcc's native dict is already insertion-ordered (phase 2
    contract), so ``OrderedDict`` is a subtype with the same storage
    plus the few extra methods real OrderedDict has."""

    def move_to_end(self, key, last: bool = True) -> None:
        v = self[key]
        if last:
            del self[key]
            self[key] = v
        else:
            keys = list(self)
            saved = []
            for k in keys:
                if k != key:
                    saved.append((k, self[k]))
            for k in keys:
                del self[k]
            self[key] = v
            for pair in saved:
                self[pair[0]] = pair[1]

    def popitem(self, last: bool = True):
        if not self:
            raise KeyError("dictionary is empty")
        keys = list(self)
        if last:
            k = keys[len(keys) - 1]
        else:
            k = keys[0]
        v = self[k]
        del self[k]
        return k, v


class defaultdict(dict):
    def __init__(self, default_factory=None, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.default_factory = default_factory

    def __missing__(self, key):
        if self.default_factory is None:
            raise KeyError(key)
        v = self.default_factory()
        self[key] = v
        return v


class Counter(dict):
    def __init__(self, iterable=None) -> None:
        super().__init__()
        if iterable is not None:
            self.update(iterable)

    def __missing__(self, key):
        return 0

    def __getitem__(self, key):
        return self.get(key, 0)

    def update(self, iterable=None, **kwargs) -> None:
        if iterable is not None:
            if isinstance(iterable, dict):
                for key in iterable:
                    self[key] = self.get(key, 0) + iterable[key]
            else:
                for item in iterable:
                    self[item] = self.get(item, 0) + 1
        for key in kwargs:
            self[key] = self.get(key, 0) + kwargs[key]

    def subtract(self, iterable=None, **kwargs) -> None:
        if iterable is not None:
            if isinstance(iterable, dict):
                for key in iterable:
                    self[key] = self.get(key, 0) - iterable[key]
            else:
                for item in iterable:
                    self[item] = self.get(item, 0) - 1
        for key in kwargs:
            self[key] = self.get(key, 0) - kwargs[key]

    def elements(self):
        for key in self:
            value = self[key]
            i = 0
            while i < value:
                yield key
                i += 1

    def most_common(self, n=None):
        items = []
        for key in self:
            items.append((key, self[key]))
        i = 0
        while i < len(items):
            best = i
            j = i + 1
            while j < len(items):
                if items[j][1] > items[best][1]:
                    best = j
                j += 1
            if best != i:
                tmp = items[i]
                items[i] = items[best]
                items[best] = tmp
            i += 1
        if n is None:
            return items
        return items[:n]


class deque:
    def __init__(self, iterable=None, maxlen=None) -> None:
        self._data = [] if iterable is None else list(iterable)
        self.maxlen = maxlen

    def append(self, v) -> None:
        self._data.append(v)
        if self.maxlen is not None and len(self._data) > self.maxlen:
            self._data.pop(0)

    def appendleft(self, v) -> None:
        self._data.insert(0, v)
        if self.maxlen is not None and len(self._data) > self.maxlen:
            self._data.pop()

    def pop(self):
        return self._data.pop()

    def popleft(self):
        return self._data.pop(0)

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, value) -> bool:
        return value in self._data

    def rotate(self, n: int = 1) -> None:
        length = len(self._data)
        if length == 0:
            return
        n = n % length
        if n:
            self._data[:] = self._data[-n:] + self._data[:-n]


class ChainMap:
    def __init__(self, *maps) -> None:
        self.maps = list(maps) if maps else [{}]

    def __getitem__(self, key):
        for mapping in self.maps:
            try:
                return mapping[key]
            except KeyError:
                pass
        return self.__missing__(key)

    def __missing__(self, key):
        raise KeyError(key)

    def __setitem__(self, key, value):
        self.maps[0][key] = value

    def __delitem__(self, key):
        del self.maps[0][key]

    def __contains__(self, key):
        for mapping in self.maps:
            if key in mapping:
                return True
        return False

    def __iter__(self):
        keys = {}
        for mapping in reversed(self.maps):
            for key in mapping:
                keys[key] = None
        return iter(keys)

    def __len__(self):
        keys = set()
        for mapping in self.maps:
            for key in mapping:
                keys.add(key)
        return len(keys)

    def __bool__(self):
        for mapping in self.maps:
            if mapping:
                return True
        return False

    def get(self, key, default=None):
        return self[key] if key in self else default

    def new_child(self, m=None, **kwargs):
        child = {} if m is None else m
        if kwargs:
            child.update(kwargs)
        return type(self)(child, *self.maps)

    @property
    def parents(self):
        return type(self)(*self.maps[1:])


class _NamedTupleType:
    def __init__(self, name: str, fields) -> None:
        self.__name__ = name
        self._name = name
        self._fields = fields

    def __call__(self, *args, **kwargs):
        return _NamedTupleInstance(self, args, kwargs)


class _NamedTupleInstance:
    def __init__(self, owner, args, kwargs) -> None:
        self._owner = owner
        vals = list(args)
        fields = owner._fields
        i = 0
        for f in fields:
            if i < len(vals):
                i += 1
                continue
            if f in kwargs:
                vals.append(kwargs[f])
            else:
                raise TypeError(f"missing field {f!r}")
            i += 1
        if len(vals) > len(fields):
            raise TypeError("too many positional arguments")
        self._values = tuple(vals)

    def __len__(self) -> int:
        return len(self._values)

    def __getitem__(self, index: int):
        return self._values[index]

    def __iter__(self):
        return iter(self._values)

    def __getattr__(self, attr):
        if attr == "_fields":
            return self._owner._fields
        fields = self._owner._fields
        i = 0
        while i < len(fields):
            if fields[i] == attr:
                return self._values[i]
            i += 1
        raise AttributeError(attr)

    def __repr__(self):
        fields = self._owner._fields
        parts = ", ".join(
            f"{f}={v!r}" for f, v in zip(fields, self._values)
        )
        return f"{self._owner._name}({parts})"

    def _asdict(self):
        fields = self._owner._fields
        return {f: v for f, v in zip(fields, self._values)}


def namedtuple(name, field_spec):
    """Minimal ``namedtuple`` — supports space-or-comma separated
    field specs; returns a class whose instances are tuples with
    named attribute access."""
    if isinstance(field_spec, str):
        fields = tuple(
            f.strip() for f in field_spec.replace(",", " ").split() if f.strip()
        )
    else:
        fields = tuple(field_spec)

    return _NamedTupleType(name, fields)
