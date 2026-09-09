"""5-GC common production contract: vthread waiter-node freelist pool.

Part of the virtual-thread T-track (T-P0-VTHREAD-NODE-POOL) and the 5-GC
Production Equality Rule (docs/goal/goal-prompt.md G-track). py_threading.c wakes
virtual threads that block on a Lock / Event / Condition / Semaphore through a
per-object waiter queue whose nodes (PyThreadVThreadWaiter) used to be raw
calloc()/free() per park. This slice recycles those nodes through a bounded
slab/freelist pool (PCC_VTHREAD_WAITER_POOL_LIMIT), mirroring the ready-entry
pool in pcc_threads.c (PCC_VTHREAD_READY_ENTRY_POOL_LIMIT).

The waiter-node path is a runtime ABI surface rather than a Python source
construct, so this brick builds a focused C probe against pcc's no-libpython
runtime archive and runs the same probe under PCC_GC_BACKEND 0..4.

The probe spawns many virtual threads that each try to acquire one shared
threading.Lock while running on the scheduler. Only one holds it at a time; the
rest enqueue waiter nodes and park. A releaser vthread hands the lock along one
waiter at a time until every worker has acquired and released it. Running this
over many rounds forces the freelist to fill and be reused. The pool must
preserve the observable contract: every worker eventually acquires the lock
(mutual exclusion, FIFO wake, no lost wakeups), the scheduler queues drain to
empty, and no waiter node leaks — identically across all five GC backends.
"""
from __future__ import annotations

import os
import subprocess
import textwrap

import pytest

from tests.runtime_build_cache import cached_c_runtime

from pathlib import Path


REPO_ROOT = Path(__file__).absolute().parents[3]
RUNTIME_DIR = REPO_ROOT / "pcc" / "py_runtime"


# Number of worker vthreads that contend for the single lock per round, and the
# number of rounds. WORKERS well exceeds any single burst, and running ROUNDS
# rounds guarantees the freelist is populated by an earlier round and reused by
# a later one (the whole point of the slab pool).
_WORKERS = 64
_ROUNDS = 8

_SOURCE = r"""
#include "py_runtime.h"
#include "py_internal.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#define WORKERS %(workers)d
#define ROUNDS %(rounds)d

/* Shared lock for the current round and bookkeeping the probe checks. */
static PyObject *g_lock = NULL;
static int g_acquired_total = 0;   /* how many workers acquired the lock */
static int g_max_concurrent = 0;   /* peak simultaneous holders (must stay 1) */
static int g_held_now = 0;         /* current holders (mutual-exclusion check) */

/* Worker resume: slot 0 holds a small-int marker (stage) across resumes.
 *   stage 0 -> first entry: try to acquire the lock.
 *   stage 1 -> we hold the lock: do the mutual-exclusion accounting, release
 *              (which wakes the next waiter -> node recycle), and finish.
 * py_threading_lock_acquire_vthread returns 0 when it takes the lock
 * immediately, or 1 when it enqueued a waiter node and parked.
 *
 * The first worker to grab the lock deliberately yields (sleep(0)) while still
 * holding it, re-queueing itself at the tail. That lets every other ready
 * worker run and pile onto the waiter queue (rc == 1), which is what forces the
 * freelist pool to fill up and later be reused. */
static int64_t worker_resume(PyObject *vthread, PyObject *continuation) {
    PyObject *marker = py_continuation_get_slot(continuation, 0);
    int64_t stage = marker != NULL ? py_int_value_i64(marker) : 0;
    py_decref(marker);

    if (stage == 0) {
        int64_t rc = py_threading_lock_acquire_vthread(g_lock);
        if (rc < 0) return -1;
        /* Remember that our next resume must release the lock. */
        PyObject *held = py_int_from_i64(1);
        if (held == NULL) return -1;
        (void)py_continuation_set_slot(continuation, 0, held);
        py_decref(held);
        if (rc == 1) {
            /* Enqueued a waiter node + parked; the scheduler resumes us when a
             * releaser hands the lock over. Leave state PARKED. */
            return 0;
        }
        /* rc == 0: took the lock uncontended. Yield while holding so the other
         * queued workers contend and enqueue waiter nodes; we come back at
         * stage 1 to release. */
        if (py_virtual_thread_sleep(vthread, 0) != 0) return -1;
        return 0;
    }

    /* stage == 1: we own the lock now. */
    g_held_now++;
    if (g_held_now > g_max_concurrent) g_max_concurrent = g_held_now;
    g_acquired_total++;
    g_held_now--;
    if (py_threading_lock_release(g_lock) < 0) return -1;
    return 0;
}

static PyObject *make_worker(void) {
    int32_t frame_map[1] = {1};   /* one slot; holds a tagged small-int marker */
    PyObject *slots[1] = {0};
    PyObject *zero = py_int_from_i64(0);
    if (zero == NULL) return NULL;
    slots[0] = zero;
    PyObject *cont = py_continuation_new_typed(
        frame_map, slots, (void *)&worker_resume
    );
    py_decref(zero);
    if (cont == NULL) return NULL;
    PyObject *vt = py_virtual_thread_new(cont);
    py_decref(cont);
    return vt;
}

static int run_one_round(void) {
    g_lock = py_threading_lock_new();
    if (g_lock == NULL) return 0;
    g_acquired_total = 0;
    g_max_concurrent = 0;
    g_held_now = 0;

    for (int i = 0; i < WORKERS; i++) {
        PyObject *vt = make_worker();
        if (vt == NULL) { py_decref(g_lock); g_lock = NULL; return 0; }
        if (py_virtual_thread_start(vt) != 0) {
            py_decref(vt);
            py_decref(g_lock);
            g_lock = NULL;
            return 0;
        }
        py_decref(vt);
    }

    /* Drive the scheduler to completion. Each acquire/park + release/wake is a
     * step; give generous headroom. */
    int64_t ran = py_virtual_thread_run_until_idle((int64_t)(WORKERS * 8 + 64));
    if (ran < 0) { py_decref(g_lock); g_lock = NULL; return 0; }

    int ok = g_acquired_total == WORKERS
        && g_max_concurrent == 1
        && g_held_now == 0
        && py_virtual_thread_ready_count() == 0
        && py_virtual_thread_timer_count() == 0
        && py_virtual_thread_io_wait_count() == 0;

    py_decref(g_lock);
    g_lock = NULL;
    return ok;
}

int main(int argc, char **argv) {
    if (argc != 2) return 2;
    int64_t backend = (int64_t)atoll(argv[1]);
    if (pcc_gc_set_backend(backend) != 0) return 3;
    pcc_gc_telemetry_reset();

    int all_ok = 1;
    for (int r = 0; r < ROUNDS; r++) {
        if (!run_one_round()) { all_ok = 0; break; }
    }

    /* A final gc.collect() after the pool has been exercised must not crash or
     * leave scheduler state behind. */
    (void)pcc_gc_collect(0);
    int drained = py_virtual_thread_ready_count() == 0
        && py_virtual_thread_timer_count() == 0
        && py_virtual_thread_io_wait_count() == 0;
    int64_t allocations = py_virtual_thread_node_pool_stat(
        PCC_VTHREAD_NODE_WAITER, PCC_VTHREAD_POOL_ALLOCATIONS
    );
    int64_t reuses = py_virtual_thread_node_pool_stat(
        PCC_VTHREAD_NODE_WAITER, PCC_VTHREAD_POOL_REUSES
    );
    int64_t cached = py_virtual_thread_node_pool_stat(
        PCC_VTHREAD_NODE_WAITER, PCC_VTHREAD_POOL_CACHED
    );
    int reused = allocations > 0 && reuses > 0 && cached > 0 && cached <= 4096;

    printf("%%lld:%%d:%%d:%%d:%%lld:%%lld:%%lld\n",
        (long long)backend, all_ok, drained, reused,
        (long long)allocations, (long long)reuses, (long long)cached);
    return all_ok && drained && reused ? 0 : (int)(10 + backend);
}
""" % {"workers": _WORKERS, "rounds": _ROUNDS}


@pytest.fixture(scope="module", params=["c", "py"])
def _vthread_waiter_pool_exe(tmp_path_factory, request):
    tmp = tmp_path_factory.mktemp("gc_vthread_waiter_pool")
    if request.param == "c":
        work_runtime = cached_c_runtime()
        archive = work_runtime / "libpy_runtime.a"
    else:
        archive = request.getfixturevalue("pcc_py_runtime_archive")
        work_runtime = archive.parent

    src = tmp / "vthread_waiter_node_pool.c"
    src.write_text(textwrap.dedent(_SOURCE).lstrip(), encoding="utf-8")
    exe = tmp / "vthread_waiter_node_pool_bin"
    cc = os.environ.get("CC", "cc")
    build = subprocess.run(
        [
            cc,
            "-std=c11",
            f"-I{work_runtime / 'include'}",
            f"-I{work_runtime / 'src'}",
            str(src),
            str(archive),
            "-lm",
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return str(exe)


@pytest.mark.parametrize("backend", ["0", "1", "2", "3", "4"])
def test_vthread_waiter_node_pool(_vthread_waiter_pool_exe, backend):
    run = subprocess.run(
        [_vthread_waiter_pool_exe, backend],
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert run.returncode == 0, (
        f"backend #{backend} rc={run.returncode}: {run.stderr.strip()[:200]}"
    )
    fields = run.stdout.strip().split(":")
    assert fields[:4] == [backend, "1", "1", "1"], run.stdout
    assert all(int(value) > 0 for value in fields[4:]), run.stdout
