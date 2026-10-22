"""Import-safe ``profiling.tracing`` boundary for pcc-native build tools.

CPython 3.15 moved cProfile's implementation here; ``cProfile`` re-exports
``run``, ``runctx`` and ``Profile``.  Meson imports :mod:`cProfile`
unconditionally but only enables it behind its ``--profile-self`` developer
option.  pcc does not yet expose interpreter call events or deterministic
frame sampling, so pretending that wall-clock timing is profiler data would
be misleading.  Construction and empty-state inspection are owned; every
operation that claims to collect or serialize call statistics fails closed
with a precise error.
"""
from __future__ import annotations


_UNOWNED = (
    "cProfile requires runtime-owned call/return/exception profiling events"
)


class Profile:
    def __init__(self, timer=None, timeunit=0.0, subcalls=True, builtins=True):
        if timer is not None:
            raise NotImplementedError(
                "custom cProfile timers are not runtime-owned"
            )
        if timeunit not in (0, 0.0):
            raise NotImplementedError(
                "custom cProfile time units are not runtime-owned"
            )
        self.subcalls = bool(subcalls)
        self.builtins = bool(builtins)

    def clear(self):
        return None

    def getstats(self):
        # This is the truthful state of a profiler that has never been enabled.
        return []

    def enable(self, subcalls=True, builtins=True):
        raise NotImplementedError(_UNOWNED)

    def disable(self):
        raise NotImplementedError(_UNOWNED)

    def create_stats(self):
        raise NotImplementedError(_UNOWNED)

    def snapshot_stats(self):
        raise NotImplementedError(_UNOWNED)

    def print_stats(self, sort=-1):
        raise NotImplementedError(_UNOWNED)

    def dump_stats(self, file):
        raise NotImplementedError(_UNOWNED)

    def run(self, cmd):
        raise NotImplementedError(_UNOWNED)

    def runctx(self, cmd, globals, locals):
        raise NotImplementedError(_UNOWNED)

    def runcall(self, func, /, *args, **kwargs):
        raise NotImplementedError(_UNOWNED)

    def __enter__(self):
        self.enable()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.disable()


def run(statement, filename=None, sort=-1):
    raise NotImplementedError(_UNOWNED)


def runctx(statement, globals, locals, filename=None, sort=-1):
    raise NotImplementedError(_UNOWNED)


def label(code):
    if isinstance(code, str):
        return ("~", 0, code)
    return (code.co_filename, code.co_firstlineno, code.co_name)


def main():
    raise NotImplementedError("profiler command-line parsing is not runtime-owned")


__all__ = ("run", "runctx", "Profile")
