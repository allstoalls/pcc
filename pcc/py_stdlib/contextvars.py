"""pcc.py_stdlib.contextvars - context variables without libpython.

CPython's `contextvars` is a C extension module, so importing it under
`--python-libpython=off` emits `py_cpy_ensure_init()` and the enclosing
function is replaced by a fail-closed stub. `asyncio.Task.__init__` calls
`copy_context()`, so that single import made every task construction
unavailable natively.

The surface implemented here is the one asyncio uses -- `copy_context()`,
`Context.run()`, and the `ContextVar` get/set/reset protocol with tokens.
Contexts are plain snapshots: `copy_context()` copies the active mapping, and
`run()` installs its context for the duration of one call and restores the
previous one on the way out, including on an exception. That is CPython's
observable behaviour for this surface. What is NOT implemented is the C
module's `Context` mapping protocol beyond `get`/`__contains__`/`__iter__`,
and `Token.MISSING` is a module-level sentinel object rather than the C
singleton; nothing in asyncio depends on either.
"""
from __future__ import annotations


class _Missing:
    """Sentinel type; a bare object() is not distinguishable enough here."""


_MISSING = _Missing()
_CURRENT = [None]


def _active():
    current = _CURRENT[0]
    if current is None:
        current = Context()
        _CURRENT[0] = current
    return current


class Token:
    """Undo record returned by `ContextVar.set`."""

    MISSING = _MISSING

    def __init__(self, var, old_value) -> None:
        self._var = var
        self._old_value = old_value
        self._used = False

    @property
    def var(self):
        return self._var

    @property
    def old_value(self):
        return self._old_value


class ContextVar:
    def __init__(self, name, *, default=_MISSING) -> None:
        self._name = name
        self._default = default

    @property
    def name(self):
        return self._name

    def get(self, default=_MISSING):
        data = _active()._data
        if self in data:
            return data[self]
        if not isinstance(default, _Missing):
            return default
        if not isinstance(self._default, _Missing):
            return self._default
        raise LookupError(self._name)

    def set(self, value):
        data = _active()._data
        if self in data:
            token = Token(self, data[self])
        else:
            token = Token(self, _MISSING)
        data[self] = value
        return token

    def reset(self, token) -> None:
        if token._used:
            raise RuntimeError("Token has already been used once")
        if token._var is not self:
            raise ValueError("Token was created by a different ContextVar")
        data = _active()._data
        if isinstance(token._old_value, _Missing):
            if self in data:
                del data[self]
        else:
            data[self] = token._old_value
        token._used = True

    def __repr__(self) -> str:
        return "<ContextVar name=" + str(self._name) + ">"


class Context:
    def __init__(self, data=None) -> None:
        if data is None:
            self._data = {}
        else:
            self._data = dict(data)

    def run(self, callable_, *args):
        previous = _CURRENT[0]
        _CURRENT[0] = self
        try:
            return callable_(*args)
        finally:
            _CURRENT[0] = previous

    def copy(self):
        return Context(self._data)

    def get(self, var, default=None):
        if var in self._data:
            return self._data[var]
        return default

    def __contains__(self, var) -> bool:
        return var in self._data

    def __len__(self) -> int:
        return len(self._data)

    def __iter__(self):
        return iter(self._data)


def copy_context():
    """Snapshot the active context."""
    return _active().copy()
