"""Compatibility wrapper for cProfile module (CPython 3.15).

The implementation lives in :mod:`profiling.tracing`.
"""

try:
    from pcc.stdlib.profiling.tracing import run, runctx, Profile
except ImportError:
    # pcc publishes the provider as the top-level stdlib package ``profiling``.
    from profiling.tracing import run, runctx, Profile

__all__ = ["run", "runctx", "Profile"]
