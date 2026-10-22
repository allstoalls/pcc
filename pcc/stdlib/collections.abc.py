"""Native-compilable core of :mod:`collections.abc`.

The classes provide the ordinary Python mixin methods used by native package
modules.  Structural/virtual-subclass registration is a separate runtime
surface; these classes do not claim CPython's complete ABC registry behavior.
"""
from __future__ import annotations


class Callable:
    pass


class Hashable:
    pass


class Iterable:
    pass


class Iterator(Iterable):
    pass


class Sized:
    pass


class Container:
    pass


class Collection(Iterable):
    pass


class Sequence(Collection):
    def __iter__(self):
        index = 0
        while index < len(self):
            yield self[index]
            index += 1

    def __contains__(self, value):
        for item in self:
            if item == value:
                return True
        return False

    def __reversed__(self):
        index = len(self) - 1
        while index >= 0:
            yield self[index]
            index -= 1

    def index(self, value):
        index = 0
        for item in self:
            if item == value:
                return index
            index += 1
        raise ValueError("sequence.index(x): x not in sequence")

    def count(self, value):
        total = 0
        for item in self:
            if item == value:
                total += 1
        return total


class Mapping(Collection):
    def get(self, key, default=None):
        for candidate in self:
            if candidate == key:
                return self[key]
        return default

    def __contains__(self, key):
        for candidate in self:
            if candidate == key:
                return True
        return False

    def keys(self):
        result = []
        for key in self:
            result.append(key)
        return result

    def items(self):
        result = []
        for key in self:
            result.append((key, self[key]))
        return result

    def values(self):
        result = []
        for key in self:
            result.append(self[key])
        return result


class Buffer:
    pass
