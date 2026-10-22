"""pcc.stdlib.contextlib — narrow ``contextlib`` skeleton.

Notable gap: ``@contextmanager`` needs generator support in pcc's
codegen to lower the yield suspension. Until that lands, the runtime
fallback below uses CPython's own contextlib; a native replacement
will ship with P6C.3's generator work.
"""

from __future__ import annotations


class nullcontext:
    """Context manager that returns its argument and otherwise does nothing."""

    def __init__(self, enter_result=None) -> None:
        self.enter_result = enter_result

    def __enter__(self):
        return self.enter_result

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


class suppress:
    """Context manager that suppresses a specific exception class."""

    def __init__(self, *excs) -> None:
        self._excs = excs

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return exc_type is not None and issubclass(exc_type, self._excs)


class ExitStack:
    def __init__(self) -> None:
        self._callbacks: list = []

    def callback(self, fn, *args, **kwargs):
        self._callbacks.append(lambda: fn(*args, **kwargs))
        return fn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        while self._callbacks:
            cb = self._callbacks.pop()
            try:
                cb()
            except Exception:
                pass
        return False


def contextmanager(fn):
    """Skeleton — needs pcc generator support to fully lower. Today
    returns the inner function; user code that calls the decorated
    function with ``with`` will fall through via the generic
    ``__enter__`` / ``__exit__`` dispatch added in Phase 4."""
    raise NotImplementedError(
        "@contextmanager needs generator support in codegen (P6C.3 follow-up)"
    )
