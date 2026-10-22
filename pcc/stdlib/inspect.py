"""pcc.stdlib.inspect — narrow runtime-friendly subset."""
from __future__ import annotations

_empty = object()


class Parameter:
    POSITIONAL_ONLY = 0
    POSITIONAL_OR_KEYWORD = 1
    VAR_POSITIONAL = 2
    KEYWORD_ONLY = 3
    VAR_KEYWORD = 4
    empty = _empty

    def __init__(self, name, kind=POSITIONAL_OR_KEYWORD, default=_empty, annotation=_empty):
        self.name = name
        self.kind = kind
        self.default = default
        self.annotation = annotation

    def replace(self, **kwargs):
        return Parameter(
            kwargs.get("name", self.name),
            kwargs.get("kind", self.kind),
            kwargs.get("default", self.default),
            kwargs.get("annotation", self.annotation),
        )

    def __repr__(self):
        return "<Parameter " + self.name + ">"


class Signature:
    empty = _empty

    def __init__(self, parameters=(), return_annotation=_empty):
        self.parameters = {p.name: p for p in parameters}
        self.return_annotation = return_annotation

    def replace(self, parameters=None, return_annotation=_empty):
        if parameters is None:
            parameters = tuple(self.parameters.values())
        if return_annotation is _empty:
            return_annotation = self.return_annotation
        return Signature(parameters, return_annotation)

    def bind(self, *args, **kwargs):
        return BoundArguments(self, dict(kwargs))

    def __repr__(self):
        return "<Signature>"


class BoundArguments:
    def __init__(self, signature, arguments):
        self.signature = signature
        self.arguments = arguments

    @property
    def args(self):
        return ()

    @property
    def kwargs(self):
        return self.arguments


def signature(obj, *, follow_wrapped=True):
    params = []
    code = getattr(obj, "__code__", None)
    if code is not None:
        names = getattr(code, "co_varnames", ())
        argc = getattr(code, "co_argcount", 0)
        for name in names[:argc]:
            params.append(Parameter(name))
    return Signature(params, getattr(obj, "__annotations__", {}).get("return", _empty))


def isfunction(obj):
    return callable(obj) and hasattr(obj, "__code__")


def ismethod(obj):
    return callable(obj) and hasattr(obj, "__self__")


def isclass(obj):
    return isinstance(obj, type)


def ismodule(obj):
    return hasattr(obj, "__name__") and hasattr(obj, "__dict__") and not callable(obj)


def isroutine(obj):
    return callable(obj)


def isgeneratorfunction(obj):
    return bool(getattr(obj, "_is_generator_function", False))


def iscoroutinefunction(obj):
    return bool(getattr(obj, "_is_coroutine_function", False))


def isawaitable(obj):
    return hasattr(obj, "__await__")


def getmodule(obj):
    return None


def getmembers(obj, predicate=None):
    out = []
    for name in dir(obj):
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if predicate is None or predicate(value):
            out.append((name, value))
    out.sort(key=lambda x: x[0])
    return out


def getattr_static(obj, attr, default=_empty):
    try:
        return getattr(obj, attr)
    except AttributeError:
        if default is not _empty:
            return default
        raise


def cleandoc(doc):
    if doc is None:
        return ""
    lines = str(doc).expandtabs().splitlines()
    while lines and lines[0].strip() == "":
        lines.pop(0)
    while lines and lines[-1].strip() == "":
        lines.pop()
    return "\n".join(line.strip() for line in lines)


def getdoc(obj):
    return cleandoc(getattr(obj, "__doc__", None))


def unwrap(func, *, stop=None):
    seen = set()
    while hasattr(func, "__wrapped__"):
        if stop is not None and stop(func):
            break
        if id(func) in seen:
            break
        seen.add(id(func))
        func = func.__wrapped__
    return func


def currentframe():
    return None


def stack(context=1):
    return []
