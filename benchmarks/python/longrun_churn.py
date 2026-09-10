"""G-P3-LONGRUN workload 1: steady-state allocation/death churn.

A fixed-size live set (ring of mixed small objects) is continuously
replaced, holding the live heap roughly constant while allocation and
death rates stay high. Every SAMPLE_EVERY rounds a CSV line is printed:

    elapsed_ms,rss_bytes,peak_rss_bytes,pause_n,pause_sum_us,pause_max_us,
    pause_lt_100us,pause_lt_1ms,pause_lt_10ms,pause_ge_10ms,ops,
    heap_in_use,heap_capacity,zpage_capacity,zpage_used,zpage_span,
    zpage_free_capacity

Bounded by argv[1] = total rounds (smoke tiers pass a small number;
the manual minutes-scale tier passes a large one). Runs identically
under any PCC_GC_BACKEND=0..4. Deterministic (no randomness).
"""
import sys

from pcc.extern import c_int64, extern

pcc_os_current_rss_bytes = extern("pcc_os_current_rss_bytes", (), c_int64)
pcc_os_peak_rss_bytes = extern("pcc_os_peak_rss_bytes", (), c_int64)
pcc_os_heap_in_use_bytes = extern("pcc_os_heap_in_use_bytes", (), c_int64)
pcc_os_heap_capacity_bytes = extern("pcc_os_heap_capacity_bytes", (), c_int64)
pcc_gc_telemetry = extern("pcc_gc_telemetry", (c_int64,), c_int64)
pcc_monotonic_us = extern("pcc_runtime_monotonic_us", (), c_int64)
pcc_gc_backend4_zpage_capacity_bytes = extern(
    "pcc_gc_backend4_zpage_capacity_bytes", (), c_int64
)
pcc_gc_backend4_zpage_used_bytes = extern(
    "pcc_gc_backend4_zpage_used_bytes", (), c_int64
)
pcc_gc_backend4_zpage_span_bytes = extern(
    "pcc_gc_backend4_zpage_span_bytes", (), c_int64
)
pcc_gc_backend4_zpage_free_capacity_bytes = extern(
    "pcc_gc_backend4_zpage_free_capacity_bytes", (), c_int64
)

PAUSE_COUNT = 32
PAUSE_SUM_US = 33
MAX_PAUSE_US = 7
PAUSE_LT_100US = 34
PAUSE_LT_1MS = 35
PAUSE_LT_10MS = 36
PAUSE_GE_10MS = 37

LIVE_SET = 2048
SAMPLE_EVERY = 200


class Node:
    def __init__(self, k: int):
        self.k = k
        self.tag = "n" + str(k % 97)
        self.items = [k, k + 1, k + 2]


def main() -> int:
    rounds = 1000
    if len(sys.argv) > 1:
        rounds = int(sys.argv[1])

    ring = []
    i = 0
    while i < LIVE_SET:
        ring.append(Node(i))
        i = i + 1

    start_us = pcc_monotonic_us()
    ops = 0
    r = 0
    while r < rounds:
        idx = 0
        while idx < 64:
            slot = (r * 64 + idx) % LIVE_SET
            ring[slot] = Node(r * 64 + idx)
            d = {"a": idx, "b": "s" + str(idx % 13)}
            if d["a"] != idx:
                print("corrupt")
                return 1
            ops = ops + 1
            idx = idx + 1
        if r % SAMPLE_EVERY == 0:
            elapsed = (pcc_monotonic_us() - start_us) // 1000
            # Formatting allocates and can itself trigger GC. Read the pause
            # counters together so count and histogram describe the same work.
            pause_count = pcc_gc_telemetry(PAUSE_COUNT)
            pause_sum = pcc_gc_telemetry(PAUSE_SUM_US)
            pause_max = pcc_gc_telemetry(MAX_PAUSE_US)
            pause_lt_100us = pcc_gc_telemetry(PAUSE_LT_100US)
            pause_lt_1ms = pcc_gc_telemetry(PAUSE_LT_1MS)
            pause_lt_10ms = pcc_gc_telemetry(PAUSE_LT_10MS)
            pause_ge_10ms = pcc_gc_telemetry(PAUSE_GE_10MS)
            print(
                str(elapsed)
                + ","
                + str(pcc_os_current_rss_bytes())
                + ","
                + str(pcc_os_peak_rss_bytes())
                + ","
                + str(pause_count)
                + ","
                + str(pause_sum)
                + ","
                + str(pause_max)
                + ","
                + str(pause_lt_100us)
                + ","
                + str(pause_lt_1ms)
                + ","
                + str(pause_lt_10ms)
                + ","
                + str(pause_ge_10ms)
                + ","
                + str(ops)
                + ","
                + str(pcc_os_heap_in_use_bytes())
                + ","
                + str(pcc_os_heap_capacity_bytes())
                + ","
                + str(pcc_gc_backend4_zpage_capacity_bytes())
                + ","
                + str(pcc_gc_backend4_zpage_used_bytes())
                + ","
                + str(pcc_gc_backend4_zpage_span_bytes())
                + ","
                + str(pcc_gc_backend4_zpage_free_capacity_bytes())
            )
        r = r + 1
    print("done," + str(ops))
    return 0


main()
