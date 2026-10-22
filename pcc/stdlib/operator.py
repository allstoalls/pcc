"""pcc.stdlib.operator — the callable form of operators pcc uses."""
from __future__ import annotations

import sys as _native_sys

from pcc.extern import c_obj, c_ptr, extern

_index_c: "extern" = extern("py_obj_index", (c_ptr,), c_obj)


def add(a, b): return a + b
def sub(a, b): return a - b
def mul(a, b): return a * b
def truediv(a, b): return a / b
def floordiv(a, b): return a // b
def mod(a, b): return a % b
def pow(a, b): return a ** b
def neg(a): return -a
def pos(a): return +a
def abs_(a): return -a if a < 0 else a
def is_(a, b): return a is b
def is_not(a, b): return a is not b
def truth(a): return bool(a)
def index(a, /):
    """Return an exact Python int through the integer-index protocol."""
    if _native_sys.implementation.name != "pcc":
        import operator as host_operator

        return host_operator.index(a)
    return _index_c(a)

def lt(a, b): return a < b
def le(a, b): return a <= b
def eq(a, b): return a == b
def ne(a, b): return a != b
def gt(a, b): return a > b
def ge(a, b): return a >= b

def and_(a, b): return a & b
def or_(a, b): return a | b
def xor(a, b): return a ^ b
def not_(a): return not a
def inv(a): return ~a
def lshift(a, b): return a << b
def rshift(a, b): return a >> b

def getitem(c, k): return c[k]
def setitem(c, k, v): c[k] = v
def delitem(c, k): del c[k]

def contains(c, x): return x in c


def itemgetter(*items):
    if len(items) == 1:
        i = items[0]
        def g(obj): return obj[i]
        return g
    def g(obj): return tuple(obj[i] for i in items)
    return g


def attrgetter(*attrs):
    if len(attrs) == 1:
        a = attrs[0]
        def g(obj): return getattr(obj, a)
        return g
    def g(obj): return tuple(getattr(obj, a) for a in attrs)
    return g


def methodcaller(name, *args, **kwargs):
    def g(obj): return getattr(obj, name)(*args, **kwargs)
    return g
