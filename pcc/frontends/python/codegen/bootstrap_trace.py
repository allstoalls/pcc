"""Module filter for the PCC_DEBUG_BOOTSTRAP_TRACE codegen probes.

`docs/debugging-playbook.md` documents PCC_DEBUG_BOOTSTRAP_TRACE as a general
tool, but the probe sites used to contain a hard-coded py_lift module guard,
so the documented tool only
ever fired for one module, and only for whoever knew that. This makes the
filter part of the variable instead:

    PCC_DEBUG_BOOTSTRAP_TRACE=1                trace every module
    PCC_DEBUG_BOOTSTRAP_TRACE=pcc.frontends.python.py_lift    trace that module only
    PCC_DEBUG_BOOTSTRAP_TRACE=pcc.frontends.c.parse.,pcc.frontends.python.codegen.layer1
                                               trace anything matching one of
                                               the comma-separated prefixes

Unset or empty disables tracing, exactly as before.
"""

from __future__ import annotations

import os

_ENV = "PCC_DEBUG_BOOTSTRAP_TRACE"
_ALL = ("1", "true", "yes", "on", "all", "*")


def bootstrap_trace_enabled(module_name: object = None) -> bool:
    """True when the trace probes should fire for ``module_name``."""

    raw = str(os.environ.get(_ENV, "") or "").strip()
    if not raw:
        return False
    if raw.lower() in _ALL:
        return True
    name = str(module_name or "")
    for prefix in raw.split(","):
        prefix = prefix.strip()
        if prefix and name.startswith(prefix):
            return True
    return False
