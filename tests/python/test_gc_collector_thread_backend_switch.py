"""The GC1/GC2 collector thread stops with its backend and is not a mutator.

GC1 runs the collector thread like GC2 since d58306406, but
pcc_gc_set_backend still joined it only when leaving GC2, and its rule that a
switch into a moving collector (GC3, GC4) needs a single live mutator
counted the collector thread: switching from GC1 to GC4 was refused in a
one-thread program, and leaving GC1 left the thread running.
"""

import subprocess
from pathlib import Path

from _gc_substrate_common import _compile_runtime_probe


PROBE = r'''
    #include "py_internal.h"
    #include <sched.h>
    #include <stdint.h>

    /* The collector thread registers itself once it runs. */
    static int wait_for_live(int64_t wanted) {
        for (int spins = 0; spins < 2000000; spins++) {
            if (pcc_thread_live_count() == wanted) return 1;
            pcc_thread_safepoint();
            sched_yield();
        }
        return 0;
    }

    int main(void) {
        if (pcc_gc_set_backend(PCC_GC_KIND_INCREMENTAL_TRICOLOR) != 0) return 2;
        if (!wait_for_live(2)) return 3;
        /* Leaving GC1 joins its collector thread. */
        if (pcc_gc_set_backend(PCC_GC_KIND_REFCOUNT_CYCLE) != 0) return 4;
        if (pcc_thread_live_count() != 1) return 5;
        /* The collector thread is not a mutator: GC1 and GC2 may switch to
         * the moving collectors in a one-thread program. */
        if (pcc_gc_set_backend(PCC_GC_KIND_INCREMENTAL_TRICOLOR) != 0) return 6;
        if (!wait_for_live(2)) return 7;
        if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 8;
        if (pcc_thread_live_count() != 1) return 9;
        if (pcc_gc_set_backend(PCC_GC_KIND_CONCURRENT_MARK_SWEEP) != 0) return 10;
        if (!wait_for_live(2)) return 11;
        if (pcc_gc_set_backend(PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) != 0) return 12;
        if (pcc_thread_live_count() != 1) return 13;
        return 0;
    }
'''


def test_collector_thread_stops_with_its_backend_and_never_blocks_a_moving_switch(tmp_path: Path) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind="pcc_python",
        threaded=True,
        stem="collector_thread_backend_switch",
        source_text=PROBE,
    )
    run = subprocess.run([str(executable)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, (run.returncode, run.stdout + run.stderr)
