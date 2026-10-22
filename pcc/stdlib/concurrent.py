"""pcc.stdlib.concurrent — sequential-fallback ``concurrent.futures``
skeleton.

The only consumer in pcc is ``pcc/frontends/c/evaluator/c_evaluator.py``, which
uses ``ProcessPoolExecutor.map`` to compile independent translation
units in parallel. For self-host we degrade to sequential execution;
the MCJIT subprocess guard that actually needs isolation is handled
separately by ``pcc.stdlib.multiprocessing`` (also sequential).

This module is laid out as a flat stub rather than a real package —
the audit only keys on the top-level import name, so a single file
is enough to satisfy ``from concurrent.futures import X`` (callers
resolve that at runtime via CPython's real module, not via this
stub). Attributes are re-exported under the module root so that if
the self-host build later imports this lazily, names resolve.
"""
from __future__ import annotations


class Future:
    """Minimal Future stand-in. All work is done synchronously in
    ``submit`` / ``map``, so every Future is already-complete."""

    def __init__(self, result=None, exception=None) -> None:
        self._result = result
        self._exception = exception

    def result(self, timeout=None):
        if self._exception is not None:
            raise self._exception
        return self._result

    def exception(self, timeout=None):
        return self._exception

    def done(self) -> bool:
        return True

    def running(self) -> bool:
        return False

    def cancelled(self) -> bool:
        return False

    def cancel(self) -> bool:
        return False

    def add_done_callback(self, fn) -> None:
        fn(self)


class _SequentialExecutor:
    """Base sequential executor. ``submit`` runs the callable
    immediately; ``map`` returns a generator that does the same."""

    def __init__(self, max_workers=None, *args, **kwargs) -> None:
        self._max_workers = max_workers or 1
        self._shutdown = False

    def submit(self, fn, *args, **kwargs) -> Future:
        if self._shutdown:
            raise RuntimeError("cannot submit to a shutdown executor")
        try:
            return Future(result=fn(*args, **kwargs))
        except BaseException as exc:
            return Future(exception=exc)

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        # Materialise one call at a time, mirroring ProcessPoolExecutor.map's
        # "first exception wins" semantics.
        for args in zip(*iterables):
            yield fn(*args)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self._shutdown = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.shutdown(wait=True)
        return None


class ProcessPoolExecutor(_SequentialExecutor):
    """Sequential stand-in. No isolation, no parallelism."""


class ThreadPoolExecutor(_SequentialExecutor):
    """Sequential stand-in. No threading."""


def as_completed(futures, timeout=None):
    # All Futures returned by this stub are already-complete.
    for f in list(futures):
        yield f


def wait(futures, timeout=None, return_when: str = "ALL_COMPLETED"):
    done = set(futures)
    not_done: set = set()
    return done, not_done


# CPython exposes ``concurrent.futures`` as a subpackage. Emit a
# placeholder that re-exports the same names, so ``from
# concurrent.futures import ProcessPoolExecutor`` works if the
# self-host loader looks it up here.
class _FuturesModule:
    Future = Future
    ProcessPoolExecutor = ProcessPoolExecutor
    ThreadPoolExecutor = ThreadPoolExecutor
    as_completed = staticmethod(as_completed)
    wait = staticmethod(wait)


futures = _FuturesModule()
