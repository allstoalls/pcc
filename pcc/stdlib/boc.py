"""Behavior-Oriented Concurrency for pcc.

Inspired by Microsoft's BoCPy (https://microsoft.github.io/bocpy/), but built
on pcc's free-threaded refcount + native pthread primitives instead of
CPython sub-interpreters. The deadlock-free property comes from the same
trick BoCPy uses: cowns are acquired in a deterministic total order
(ascending object id), so no thread can ever hold-and-wait in a cycle.

The demo API is intentionally fixed-arity (Cown1 / Cown2). pcc keeps
codegen monomorphic, and an n-ary varargs decorator would force the lock
list through DynType. A 2-cown critical section is enough to demonstrate
the deadlock-by-construction property.
"""
from threading import Lock


class Cown:
    """Concurrent Owned variable: a value + the lock that protects it."""

    def __init__(self, value) -> None:
        self.value = value
        self.lock = Lock()

    def get_id(self) -> int:
        return id(self)


class _Locked2:
    def __init__(self, first: Cown, second: Cown) -> None:
        self.first = first
        self.second = second

    def __enter__(self):
        self.first.lock.acquire()
        self.second.lock.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.second.lock.release()
        self.first.lock.release()
        return None


def locked(c1: Cown, c2: Cown) -> _Locked2:
    """Context manager that locks two cowns in canonical (ascending id) order.

    This is the core BOC trick: regardless of caller-side argument order,
    locks are acquired in a single global ordering. Two threads doing
    ``locked(a, b)`` and ``locked(b, a)`` concurrently can never deadlock.
    """
    if c1.get_id() < c2.get_id():
        return _Locked2(c1, c2)
    return _Locked2(c2, c1)
