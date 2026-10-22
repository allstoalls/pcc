"""The one native runtime configuration: native threads are always enabled.

Every runtime archive links the threaded kernel and generated code polls for
stop-the-world requests at function entries and loop back-edges. Only the
runtime modules that implement the poll itself, or that must publish roots
before any poll can run, compile with ``PCC_IMPLICIT_THREAD_SAFEPOINTS=0``.
The single-thread runtime was removed; asking for it fails explicitly.
"""

import os

_FALSE_WORDS = ("0", "false", "no", "off")


def reject_single_thread_runtime_request() -> None:
    """Fail explicitly when a caller still selects the removed runtime."""
    requested = str(os.environ.get("PCC_WITH_THREADS", "") or "").strip().lower()
    if requested in _FALSE_WORDS:
        raise ValueError(
            "PCC_WITH_THREADS=" + requested + ": the single-thread runtime was "
            "removed; every native runtime is threaded"
        )


def implicit_thread_safepoints_enabled() -> bool:
    """Whether generated code gets function-entry and back-edge thread polls."""
    reject_single_thread_runtime_request()
    raw = str(os.environ.get("PCC_IMPLICIT_THREAD_SAFEPOINTS", "") or "").strip().lower()
    return raw not in _FALSE_WORDS
