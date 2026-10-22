"""Host-only decoding of the process-tree guard's immutable recovery policy.

Imported only behind the scheduler's proven CPython guard. This helper is not
an owned/native strict-compilation target and performs no process or FFI work.
"""

from pcc.frontends.python.worker_resource_plan import (
    STATE_MAX_AGE_SECONDS,
    WorkerMemoryError,
)


def read_tree_recovery(raw):
    """Read the host guard's immutable retry policy, not snapshot freshness.

    Only the guard creates this versioned envelope, from the retry schedule it
    actually selected and its original absolute monotonic deadline. There is
    no recovery CLI knob. Its nominal timeout sum is not a measured bound on
    sampling, identity lookups, subprocess cleanup or scheduler delays.
    """
    if raw is None:
        return STATE_MAX_AGE_SECONDS, -1.0
    try:
        fields = raw.split("\t")
        if len(fields) != 4 or fields[0] != "pcc.worker-tree-recovery.v1":
            raise ValueError("invalid recovery envelope")
        timeouts = [float(value) for value in fields[1].split(",")]
        allowance, deadline = float(fields[2]), float(fields[3])
        if not all(0.0 < value < float("inf") for value in timeouts):
            raise ValueError("invalid retry schedule")
        if not (0.0 < allowance < float("inf") and 0.0 < deadline < float("inf")):
            raise ValueError("invalid recovery bounds")
        if allowance != sum(timeouts):
            raise ValueError("recovery allowance differs from retry schedule")
        return allowance, deadline
    except (TypeError, ValueError, OverflowError) as exc:
        raise WorkerMemoryError("invalid worker tree recovery policy") from exc


