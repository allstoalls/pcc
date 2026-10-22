"""The GC1/GC2 collector thread is automatic collection: gc.disable() stops it.

CPython's gc.disable() turns off automatic collection while explicit
collection keeps working. Since d58306406 the tracing collectors run a
collector thread that advances any active cycle, including one a mutator
began explicitly, so it ignored gc.disable(). Probes that drive tracing
windows with explicit pcc_gc_step calls then met an extra finisher, whose
stop-the-world waited forever for a C thread that never polls.

The probe disables collection, selects the backend (which requests a cycle)
and allocates (which records work for the collector thread), then polls
safepoints for a while: no cycle may finish. After gc.enable() the
collector thread must run the requested cycle.
"""

import subprocess
from pathlib import Path

import pytest

from _gc_substrate_common import _compile_runtime_probe


PROBE = r'''
    #include "py_internal.h"
    #include <sched.h>
    #include <stdint.h>
    #include <time.h>

    extern int64_t pcc_gc_tracing_finish_commits;

    static int64_t now_ms(void) {
        struct timespec ts;
        clock_gettime(CLOCK_MONOTONIC, &ts);
        return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
    }

    /* A well-behaved mutator: it answers stop requests while it waits. */
    static int poll_until_finished(int64_t commits, int64_t limit_ms) {
        int64_t start = now_ms();
        while (now_ms() - start < limit_ms) {
            if (__atomic_load_n(&pcc_gc_tracing_finish_commits, __ATOMIC_ACQUIRE) != commits) {
                return 1;
            }
            pcc_thread_safepoint();
            sched_yield();
        }
        return 0;
    }

    static PyObject *kept;

    int main(void) {
        /* Disabled before the backend starts its collector thread. */
        py_gc_disable();
        if (pcc_gc_set_backend(__GC_KIND__) != 0) return 2;
        int64_t commits = pcc_gc_tracing_finish_commits;
        /* The backend switch requested a cycle, and every allocation below
         * records work for the collector thread. */
        kept = py_list_new(0);
        if (kept == 0) return 3;
        if (pcc_gc_scheduler_root_register_handle(&kept) == 0) return 3;
        for (int i = 0; i < 4096; i++) {
            PyObject *item = py_list_new(1);
            if (item == 0) return 3;
            py_list_append(kept, item);
            py_decref(item);
        }
        for (int i = 0; i < 256; i++) {
            PyObject *cycle = py_list_new(1);
            if (cycle == 0) return 3;
            py_list_append(cycle, cycle);
            py_decref(cycle);
        }
        if (poll_until_finished(commits, 300)) return 6;
        py_gc_enable();
        if (!poll_until_finished(commits, 10000)) return 7;
        return 0;
    }
'''


@pytest.mark.parametrize(
    "gc_kind",
    ["PCC_GC_KIND_INCREMENTAL_TRICOLOR", "PCC_GC_KIND_CONCURRENT_MARK_SWEEP"],
)
def test_collector_thread_pauses_while_gc_is_disabled(tmp_path: Path, gc_kind: str) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind="pcc_python",
        threaded=True,
        stem="collector_thread_disable_" + gc_kind.lower(),
        source_text=PROBE.replace("__GC_KIND__", gc_kind),
    )
    run = subprocess.run([str(executable)], capture_output=True, text=True, timeout=60)
    # 6: the collector thread finished the cycle while collection was disabled.
    assert run.returncode == 0, (gc_kind, run.returncode, run.stdout + run.stderr)
