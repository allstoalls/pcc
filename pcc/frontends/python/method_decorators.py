"""Method decorators: lowered by class_gen, or applied at class creation.

A decorator on a method is ``m = dec(m)`` executed in the class body: the
decorators are applied to the new function object and the result is bound
to the name. The native class lowering (class_gen ``_declare_method``)
compiles a few decorators into the method itself -- staticmethod,
classmethod, the property family, and decorators it treats as no-ops --
and native_method_decorator names exactly those. Every other decorator is
applied when the class is created: py_lift splits such a method into the
undecorated method, compiled under HIDDEN_METHOD_PREFIX + name as a
class-body temporary that class_gen does not publish, and an assignment
binding the decorated value to the name (split_general_method_decorators).
"""

from __future__ import annotations

from dataclasses import replace

from pcc.frontends.python.py_ast import Assign, Attr, Call, DynType, FuncDef, Name


HIDDEN_METHOD_PREFIX = "__pcc_decorated__"

# class_gen ``_declare_method`` turns these into the method's kind.
_KIND_DECORATORS = frozenset({
    "staticmethod",
    "classmethod",
    "property",
    "cached_property",
    "functools.cached_property",
})
# class_gen ``_declare_method`` compiles these as no-ops.
_IGNORED_DECORATORS = frozenset({
    "abstractmethod",
    "abc.abstractmethod",
    "abstractclassmethod",
    "abc.abstractclassmethod",
    "abstractstaticmethod",
    "abc.abstractstaticmethod",
    "abstractproperty",
    "abc.abstractproperty",
    "errstate",
    "cache",
    "functools.cache",
    "lru_cache",
    "functools.lru_cache",
})
_IGNORED_SUFFIXES = (".errstate", ".cache", ".cached")
_TOKEN_CALLS = frozenset({"TOKEN", "lex.TOKEN"})


def decorator_name(dec):
    """``name``, ``obj.name`` or the called one of those; else None."""
    if isinstance(dec, Name):
        return dec.ident
    if isinstance(dec, Attr) and isinstance(dec.obj, Name):
        return dec.obj.ident + "." + dec.name
    if isinstance(dec, Call):
        return decorator_name(dec.func)
    return None


def native_method_decorator(dec) -> bool:
    """Whether class_gen lowers ``dec`` on a method itself."""
    name = decorator_name(dec)
    if name is None:
        return False
    if name in _KIND_DECORATORS or name in _IGNORED_DECORATORS:
        return True
    if name.endswith(".setter") or name.endswith(".deleter"):
        return True
    if name.endswith(_IGNORED_SUFFIXES):
        return True
    if "Cache" in name and name.endswith(".me"):
        return True
    # class_gen recognizes pcc's continuation_factory and transparent_call by
    # their resolved builtin values; any spelling of those names stays with
    # class_gen.
    if name.endswith("continuation_factory") or name.endswith("transparent_call"):
        return True
    return isinstance(dec, Call) and name in _TOKEN_CALLS


# A function defined inside a class body's compound statement, hoisted by
# class_body_statements.lower_class_body_statements: HIDDEN_DEFINITION_PREFIX,
# a number, "__", the source name.
HIDDEN_DEFINITION_PREFIX = "__pcc_cbdef"


def hidden_definition_name(index: int, name: str) -> str:
    return HIDDEN_DEFINITION_PREFIX + str(index) + "__" + name


def public_method_name(name: str) -> str:
    """The source name of a class-body function class_gen keeps hidden.

    Those are the undecorated functions split_general_method_decorators
    splits off and the functions class_body_statements hoists out of a
    compound statement; class_gen compiles them without publishing them.
    """
    if name.startswith(HIDDEN_METHOD_PREFIX):
        return name[len(HIDDEN_METHOD_PREFIX):]
    if name.startswith(HIDDEN_DEFINITION_PREFIX):
        rest = name[len(HIDDEN_DEFINITION_PREFIX):]
        digits = 0
        while digits < len(rest) and rest[digits].isdigit():
            digits += 1
        if digits > 0 and rest[digits:digits + 2] == "__":
            return rest[digits + 2:]
    return name


def _splittable(fd) -> bool:
    if not fd.decorators:
        return False
    # Instances and operators find dunder methods statically.
    if fd.name.startswith("__") and fd.name.endswith("__"):
        return False
    # A method mixing native and general decorators keeps all of them.
    for dec in fd.decorators:
        if native_method_decorator(dec):
            return False
    return True


def split_general_method_decorators(body):
    """A class body with each generally decorated method split in two."""
    out = []
    dyn = DynType(name="dyn")
    for stmt in body:
        if isinstance(stmt, FuncDef) and _splittable(stmt):
            hidden = HIDDEN_METHOD_PREFIX + stmt.name
            out.append(replace(stmt, name=hidden, decorators=()))
            value = Name(stmt.span, dyn, hidden)
            index = len(stmt.decorators) - 1
            while index >= 0:
                value = Call(stmt.span, dyn, stmt.decorators[index], (value,))
                index = index - 1
            out.append(Assign(stmt.span, (Name(stmt.span, dyn, stmt.name),), value))
        else:
            out.append(stmt)
    return tuple(out)
