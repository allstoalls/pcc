"""pcc.stdlib.types — small runtime subset.

The real CPython ``types`` module exposes many interpreter implementation
classes.  pcc self-host only needs a tiny, runtime-friendly subset: enough for
feature tests, SimpleNamespace-style records, and code that imports names such
as ModuleType / FunctionType without doing CPython-specific introspection.
"""
from __future__ import annotations


class SimpleNamespace:
    def __init__(self, **kwargs) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)

    def __repr__(self) -> str:
        parts = []
        for k, v in self.__dict__.items():
            parts.append(str(k) + "=" + repr(v))
        return "namespace(" + ", ".join(parts) + ")"

    def __eq__(self, other) -> bool:
        return hasattr(other, "__dict__") and self.__dict__ == other.__dict__


class ModuleType:
    def __init__(self, name: str, doc=None) -> None:
        self.__name__ = name
        self.__doc__ = doc
        self.__package__ = ""
        self.__loader__ = None
        self.__spec__ = None

    def __repr__(self) -> str:
        return "<module '" + self.__name__ + "'>"


class MappingProxyType:
    def __init__(self, mapping) -> None:
        self._mapping = mapping

    def __getitem__(self, key):
        return self._mapping[key]

    def get(self, key, default=None):
        return self._mapping.get(key, default)

    def keys(self):
        return self._mapping.keys()

    def values(self):
        return self._mapping.values()

    def items(self):
        return self._mapping.items()

    def __contains__(self, key) -> bool:
        return key in self._mapping

    def __iter__(self):
        return iter(self._mapping)

    def __len__(self) -> int:
        return len(self._mapping)


def new_class(name, bases=(), kwds=None, exec_body=None):
    ns = {}
    if exec_body is not None:
        exec_body(ns)
    return type(name, bases, ns)


def prepare_class(name, bases=(), kwds=None):
    return (type, {}, kwds or {})


def resolve_bases(bases):
    return bases


def coroutine(func):
    return func


class _MethodHost:
    def _method(self):
        pass


FunctionType = type(lambda: None)
LambdaType = FunctionType
BuiltinFunctionType = type(len)
BuiltinMethodType = type([].append)
MethodType = type(_MethodHost()._method)
ModuleTypeType = ModuleType
GeneratorType = type((x for x in ()))
CoroutineType = object
AsyncGeneratorType = object
CodeType = object
FrameType = object
TracebackType = object
CellType = object
NoneType = type(None)
NotImplementedType = type(NotImplemented)
EllipsisType = type(Ellipsis)
GenericAlias = object
UnionType = object
