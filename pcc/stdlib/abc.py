"""pcc.stdlib.abc — narrow ``abc`` skeleton."""
from __future__ import annotations

_cache_token = 0


def abstractmethod(fn):
    fn.__isabstractmethod__ = True
    return fn


def abstractclassmethod(fn):
    fn = classmethod(fn)
    fn.__isabstractmethod__ = True
    return fn


def abstractstaticmethod(fn):
    fn = staticmethod(fn)
    fn.__isabstractmethod__ = True
    return fn


class abstractproperty(property):
    __isabstractmethod__ = True


def _is_abstract(value) -> bool:
    return bool(getattr(value, "__isabstractmethod__", False))


class ABCMeta(type):
    def __new__(mcls, name, bases, namespace, **kwargs):
        cls = super().__new__(mcls, name, bases, namespace)
        abstracts = set()
        for base in bases:
            abstracts.update(getattr(base, "__abstractmethods__", set()))
        for key, value in namespace.items():
            if _is_abstract(value):
                abstracts.add(key)
            elif key in abstracts:
                abstracts.remove(key)
        cls.__abstractmethods__ = frozenset(abstracts)
        cls._abc_registry = set()
        return cls

    def register(cls, subclass):
        global _cache_token
        cls._abc_registry.add(subclass)
        _cache_token += 1
        return subclass

    def __instancecheck__(cls, instance):
        return cls.__subclasscheck__(type(instance))

    def __subclasscheck__(cls, subclass):
        if subclass in getattr(cls, "_abc_registry", set()):
            return True
        try:
            return issubclass(subclass, tuple(getattr(cls, "_abc_registry", ())))
        except TypeError:
            return False


class ABC(metaclass=ABCMeta):
    __slots__ = ()


def update_abstractmethods(cls):
    abstracts = set()
    for base in getattr(cls, "__bases__", ()):
        abstracts.update(getattr(base, "__abstractmethods__", set()))
    for key, value in getattr(cls, "__dict__", {}).items():
        if _is_abstract(value):
            abstracts.add(key)
        elif key in abstracts:
            abstracts.remove(key)
    cls.__abstractmethods__ = frozenset(abstracts)
    return cls


def get_cache_token():
    return _cache_token
