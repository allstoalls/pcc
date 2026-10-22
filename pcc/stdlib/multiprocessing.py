"""pcc.stdlib.multiprocessing — sequential-fallback skeleton.

pcc's only use of ``multiprocessing`` is the MCJIT subprocess
guard in ``pcc/frontends/c/evaluator/c_evaluator.py``: compile+run happens in a
child process so an LLVM crash doesn't take down the driver.

For self-host, we don't ship a fork/spawn runtime yet — P6C.1's
extern surface covers ``posix_spawn`` but not the full process-level
context switching the stdlib offers. This stub runs the work
in-process as a last-resort fallback, trading crash isolation for
being self-host-compilable. The existing CPython runtime keeps using
the real module, so production behaviour is unchanged.

Only the names the pcc evaluator uses (``get_context`` returning an
object with ``.Process``) are implemented; everything else is a
loud NotImplementedError.
"""
from __future__ import annotations


class _SequentialProcess:
    """In-process stand-in for ``multiprocessing.Process``. Runs the
    target synchronously on ``.start()``; ``.join()`` is a no-op.

    Exit code 0 on success, 1 on exception (matches the common
    subprocess-level ``proc.exitcode != 0`` check)."""

    def __init__(self, target=None, args=(), kwargs=None,
                 name=None, daemon=None) -> None:
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self.name = name or "SequentialProcess"
        self.daemon = daemon
        self.exitcode: int | None = None
        self.pid: int | None = None
        self._started = False

    def start(self) -> None:
        self._started = True
        self.pid = 0  # single-process sentinel
        if self._target is None:
            self.exitcode = 0
            return
        try:
            self._target(*self._args, **self._kwargs)
            self.exitcode = 0
        except BaseException:
            self.exitcode = 1
            # Don't re-raise — caller checks ``proc.exitcode`` and
            # drives a JSON-payload error protocol for the real error.

    def join(self, timeout=None) -> None:
        return None

    def is_alive(self) -> bool:
        return False

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None

    def close(self) -> None:
        return None


class _SequentialContext:
    """Stand-in for the object returned by ``get_context("spawn")``.
    Exposes only ``.Process``, which is what the evaluator uses."""

    Process = _SequentialProcess


def get_context(method: str | None = None) -> _SequentialContext:
    return _SequentialContext()


def get_start_method(allow_none: bool = False) -> str:
    return "sequential"


def set_start_method(method: str, force: bool = False) -> None:
    # No-op — the sequential fallback is single-method.
    return None


# The full ``Pool`` surface is only used by callers we don't have
# yet; keep a single placeholder so shape queries work.
class Pool:
    def __init__(self, processes: int | None = None, *args, **kwargs) -> None:
        raise NotImplementedError(
            "multiprocessing.Pool awaits a fork/spawn binding; use "
            "concurrent.futures.ThreadPoolExecutor's sequential fallback"
        )


Process = _SequentialProcess


def cpu_count() -> int:
    return 1


def current_process():
    raise NotImplementedError(
        "multiprocessing.current_process awaits pid + name plumbing"
    )


def active_children() -> list:
    return []


def freeze_support() -> None:
    # No-op: no Windows-style re-exec in the self-host runtime.
    return None
