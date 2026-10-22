"""Host-only compact-stackmap admission using an already active tree guard.

This is an allocation estimate, not a replacement RSS limit or guaranteed free
memory. The guard still owns actual process-tree RSS and its absolute deadline.
No environment value is interpreted as permission to increase either limit.
"""

import os
import struct
import sys

from .precise_stackmap import CompactStackMapBudget, PreciseStackMapError


def host_compact_stackmap_budget():
    """Return one operation budget, or None to preserve the old capped route.

    The model is for ordinary 64-bit, GIL-enabled CPython 3.13--3.15. It uses
    actual small-object sizes and the reviewed CPython insertion-only set
    growth rule, including old/new table overlap. It does not model allocator
    fragmentation, delayed reclamation, unrelated compiler state or native GC.
    Unsupported implementations keep the old cap rather than borrowing this
    host estimate. No project code, build, or calibration is run here.
    """
    if (sys.implementation.name != "cpython"
            or sys.version_info[:2] not in ((3, 13), (3, 14), (3, 15))
            or struct.calcsize("P") != 8
            or hasattr(sys, "gettotalrefcount")
            or os.environ.get("PYTHONMALLOC", "") == "debug"
            or os.environ.get("PYTHONMALLOC", "").endswith("_debug")):
        return None
    if hasattr(sys, "_is_gil_enabled") and not sys._is_gil_enabled():
        return None
    import sysconfig
    # A free-threaded build with its GIL temporarily enabled still has
    # different allocation/retirement machinery. Runtime state is not enough.
    if sysconfig.get_config_var("Py_GIL_DISABLED") != 0:
        return None
    raw = str(os.environ.get("PCC_WORKER_TREE_BUDGET_BYTES", "") or "")
    path = str(os.environ.get("PCC_WORKER_TREE_STATE_PATH", "") or "")
    if not raw or not path:
        return None
    if not raw.isdigit():
        raise PreciseStackMapError("invalid existing stack-map tree budget")
    ceiling = int(raw)
    if not 0 < ceiling <= 0x7FFFFFFFFFFFFFFF:
        raise PreciseStackMapError("invalid existing stack-map tree budget")

    from pcc.frontends.python.worker_resource_plan import read_tree_state
    observation = read_tree_state(path, ceiling, os.getpid(), (), include_owner=True)
    if observation is None:
        return None
    outside, resident = observation
    owner = resident[os.getpid()]
    # Host Stage1 is in-process. A prior high-water mark can over-reserve;
    # it is never relabeled as the current tree's available RSS.
    if sys.platform not in ("darwin", "linux"):
        return None
    import resource
    peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform != "darwin":
        peak *= 1024
    if peak <= 0 or outside < 0 or owner <= 0:
        return None
    available_estimate = ceiling - max(owner, peak) - outside
    if available_estimate < 32:
        return None

    word = struct.calcsize("P")
    quantum = 2 * word

    def rounded(value):
        return ((value + quantum - 1) // quantum) * quantum

    integer = rounded(max(sys.getsizeof((1 << 64) - 1), sys.getsizeof(-(1 << 63))))
    header = rounded(sys.getsizeof(set()))
    # Conservative container-entry estimate: up to eight slots per entry,
    # two simultaneous tables and four words per slot, plus key tuple,
    # three maximum-width integer owners, and one bytes-object header.
    # Root-key payload bytes are accounted separately, never hidden here.
    metadata = (64 * word + rounded(sys.getsizeof((0, 0, 0)))
                + 3 * integer + rounded(sys.getsizeof(b"")))
    # The owned Mach-O operation performs one merge visit and the existing
    # two validation visits per output record. Each record occupies 32 wire
    # bytes; the admitted input/output byte ceiling bounds this work count.
    budget = CompactStackMapBudget(
        available_estimate, 3 * (available_estimate // 32),
        set_header_bytes=header, set_entry_bytes=2 * word,
        integer_bytes=integer, metadata_entry_bytes=metadata,
        allocation_quantum=quantum,
    )
    return budget
