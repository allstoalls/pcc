"""Python profiling tools (CPython 3.15 ``profiling``).

``profiling.tracing`` is the deterministic profiler ``cProfile`` re-exports.
``profiling.sampling`` has no pcc provider yet; importing it fails.
"""

__all__ = ("tracing", "sampling")
