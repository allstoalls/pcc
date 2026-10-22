"""Scheduler-integration regression for the vthread timer min-heap.

Slice ``T-P0-VTHREAD-TIMER``: ``pcc/runtime/src/py_timer_heap.c`` (the
binary min-heap + lazy-cancel live map, previously landed *standalone* by the
mirror slice) is now wired into the coroutine/virtual-thread scheduler in
``pcc/runtime/src/pcc_threads.c``, replacing the O(n)-insert sorted
singly-linked list that used to back ``py_virtual_thread_sleep`` /
``py_virtual_thread_poll_timers`` / ``py_virtual_thread_timer_count``.

``tests/python/test_gc_coroutine_roots.py`` already drives a *single* timer
through the scheduler (root retention across GC backends). This file adds the
multi-timer ordering / boundary / retention / large-N-drain semantics that only
the heap wiring can exercise, diffed against the same invariants the CPU-only
oracle (``pcc/vthread/timer_oracle.py``) and the structure mirror
(``tests/vthread/test_timer_heap_mirror.py``) prove for the data structure in
isolation:

  * wake order is nondecreasing by deadline (out-of-order inserts still wake in
    ascending-deadline order -- pure heap ordering, independent of wall clock,
    because every entry is already due at poll time);
  * FIFO among equal deadlines (the heap's ``seq`` tiebreak reproduces the old
    sorted-list ``<= deadline`` stable insertion walk);
  * a not-yet-due timer stays registered (``timer_count`` retention) while
    earlier ones drain;
  * a large due-set (> the internal drain batch) drains completely and in order
    (exercises the batched ``pop_expired`` loop in ``py_virtual_thread_poll_timers``).

Wall-clock discipline: the scheduler computes each deadline from its *own*
``pcc_vthread_now_ms()`` read inside ``py_virtual_thread_sleep``, so on a
loaded machine (e.g. parallel xdist workers) the millisecond clock can tick
between parks and the OS can deschedule the probe between a park and a poll.
Each phase therefore either derives its expected wake order from insertion
order alone (immune to clock ticks: per-park deadlines are nondecreasing and
``seq`` breaks ties FIFO), or brackets the parks with clock reads / classifies
the poll result and retries the phase when the timing window was missed. A
real heap-ordering bug still fails every well-timed attempt, so the retries
never mask one; the count/drain invariants are clock-independent and are
asserted on every attempt.

The probe compiles against the real runtime archive (both the cc-built C
runtime and the pcc-Python runtime) and runs across all five GC backends, so it
guards the same slot-based root discipline the single-timer test does, now under
heap reordering. It is skipped (not failed) when no C compiler is available.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests.runtime_build_cache import cached_pcc_python_runtime, cached_threaded_pcc_python_runtime

REPO_ROOT = Path(__file__).absolute().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"


def _cc() -> str:
    return os.environ.get("CC", "cc")


def _have_cc() -> bool:
    return shutil.which(_cc()) is not None


def _build_runtime(
    tmp_path: Path, *, pcc_python: bool = False
) -> tuple[Path, str, list[str]]:
    if pcc_python:
        return (
            cached_threaded_pcc_python_runtime(),
            "libpy_runtime_pcc_py.a",
            ["-pthread"],
        )
    del tmp_path
    return cached_pcc_python_runtime(), "libpy_runtime_pcc_py.a", []


# The probe drives the scheduler timer path directly. Each virtual thread
# carries a single continuation slot holding its integer label (as a py int
# object), so wake order is observable by draining the ready queue after
# poll_timers. All inserted timers are made due at once by a single wait, so
# the wake ordering is decided by the min-heap (deadline, seq), not by when
# the poll happens; see the module docstring for how each phase stays immune
# to ms-clock ticks between the individual parks.
_PROBE = r"""
#include "py_runtime.h"
#include "py_internal.h"
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/time.h>
#include <unistd.h>

/* Retry budget for the phases that need a timing window (see below). Real
 * ordering bugs fail on every attempt, so retrying never masks one. */
#define PROBE_MAX_ATTEMPTS 50

static void resume_a(void) {}

/* Same clock and arithmetic as the scheduler's pcc_vthread_now_ms(). */
static int64_t probe_now_ms(void) {
    struct timeval tv;
    if (gettimeofday(&tv, NULL) != 0) return 0;
    return ((int64_t)tv.tv_sec * 1000) + ((int64_t)tv.tv_usec / 1000);
}

static PyObject *make_vthread(int64_t label) {
    int32_t frame_map[1] = {1};
    PyObject *slots[1] = {0};
    PyObject *local = py_int_from_i64(label);
    if (local == 0) return 0;
    pcc_gc_store_root(&slots[0], local);
    PyObject *cont = py_continuation_new(frame_map, slots, (void *)&resume_a);
    pcc_gc_store_root(&slots[0], 0);
    pcc_gc_release(local);
    if (cont == 0) return 0;
    PyObject *vt = py_virtual_thread_new(cont);
    pcc_gc_release(cont);
    return vt;
}

static int64_t vthread_label(PyObject *vthread) {
    PyVirtualThreadObject *vt = (PyVirtualThreadObject *)vthread;
    PyObject *cont = pcc_gc_load_ptr(vthread, &vt->continuation);
    if (cont == 0) return -1;
    PyObject *got = py_continuation_get_slot(cont, 0);
    if (got == 0) return -1;
    int overflow = 0;
    int64_t v = py_int_to_i64(got, &overflow);
    py_decref(got);
    if (overflow) return -1;
    return v;
}

/* Sleep `delay_ms` on a fresh vthread labelled `label`; returns 0 on success. */
static int park_timer(int64_t label, int64_t delay_ms) {
    PyObject *vt = make_vthread(label);
    if (vt == 0) return -1;
    int rc = py_virtual_thread_sleep(vt, delay_ms);
    pcc_gc_release(vt);
    return rc;
}

/* Drain every currently-ready vthread into out[] (labels), completing each.
 * Returns the count drained (<= cap). */
static int64_t drain_ready(int64_t *out, int64_t cap) {
    int64_t n = 0;
    for (;;) {
        PyObject *ready = py_virtual_thread_poll_ready();
        if (ready == 0) break;
        if (n < cap) out[n] = vthread_label(ready);
        n++;
        py_virtual_thread_complete(ready, py_None);
        py_decref(ready);
    }
    return n;
}

/* --- ordering: out-of-order inserts wake in ascending-deadline order ------ */
static int check_order(void) {
    /* Insert delays out of order; all are short so a single wait makes them
     * all due at once -> wake order is decided purely by the min-heap.
     * Each park computes deadline = now_ms + delay from its own clock read,
     * so the label-derived expected order only holds when all five parks
     * land inside a window narrower than the 10ms delay spacing. Bracket the
     * parks with clock reads: when the window is too wide (this worker was
     * descheduled), skip only the order assertion and retry; the count and
     * drain-total invariants are clock-independent and always asserted. */
    static const int64_t delays[5] = {50, 10, 40, 20, 30};
    for (int attempt = 0; attempt < PROBE_MAX_ATTEMPTS; attempt++) {
        int64_t t0 = probe_now_ms();
        for (int i = 0; i < 5; i++)
            if (park_timer(delays[i], delays[i]) != 0) return 0;
        int64_t t1 = probe_now_ms();
        if (py_virtual_thread_timer_count() != 5) return 0;
        usleep(90000); /* past 50ms (oversleeping only makes all more due) */
        if (py_virtual_thread_poll_timers() != 5) return 0;
        if (py_virtual_thread_timer_count() != 0) return 0;
        int64_t got[8];
        int64_t n = drain_ready(got, 8);
        if (n != 5) return 0;
        if (t1 - t0 >= 10) continue; /* deadlines may interleave: retry */
        /* labels == delays, so ascending deadline == 10,20,30,40,50 */
        if (got[0] != 10 || got[1] != 20 || got[2] != 30
            || got[3] != 40 || got[4] != 50) return 0;
        return 1;
    }
    return 0;
}

/* --- FIFO among equal deadlines ------------------------------------------- */
static int check_fifo_equal(void) {
    /* Same delay for all four: each deadline is its own park's now_ms + 25,
     * so deadlines are nondecreasing in insertion order and the seq tiebreak
     * keeps equal deadlines FIFO -> wake order is the insertion order
     * 100,101,102,103 no matter how the ms clock ticks between parks. */
    for (int64_t label = 100; label <= 103; label++)
        if (park_timer(label, 25) != 0) return 0;
    usleep(60000);
    if (py_virtual_thread_poll_timers() != 4) return 0;
    int64_t got[8];
    int64_t n = drain_ready(got, 8);
    if (n != 4) return 0;
    if (got[0] != 100 || got[1] != 101 || got[2] != 102 || got[3] != 103)
        return 0;
    return 1;
}

/* --- partial expiry leaves a future timer registered, then drains it ------ */
static int check_partial_retention(void) {
    /* usleep(40ms) only guarantees a minimum: a descheduled worker can blow
     * past timer 3's 120ms deadline before the first poll. Waking all three
     * then means the retention window was missed, not violated -- drain and
     * retry. Any other unexpected wake count is a real failure. */
    for (int attempt = 0; attempt < PROBE_MAX_ATTEMPTS; attempt++) {
        if (park_timer(1, 10) != 0) return 0;
        if (park_timer(2, 10) != 0) return 0;
        if (park_timer(3, 120) != 0) return 0; /* later, but drainable in-test */
        if (py_virtual_thread_timer_count() != 3) return 0;
        usleep(40000); /* past 10ms, before 120ms (unless descheduled) */
        int64_t woken = py_virtual_thread_poll_timers();
        int64_t got[8];
        if (woken == 3) { /* stalled past 120ms: window missed, retry */
            if (py_virtual_thread_timer_count() != 0) return 0;
            if (drain_ready(got, 8) != 3) return 0;
            continue;
        }
        if (woken != 2) return 0;
        /* id 3 not yet due -> still registered (root retained across the
         * poll). Wake order {1, 2} is deadline/FIFO-robust: deadlines are
         * nondecreasing in insertion order (10, 10, 120ms delays). */
        if (py_virtual_thread_timer_count() != 1) return 0;
        int64_t n = drain_ready(got, 8);
        if (n != 2) return 0;
        if (got[0] != 1 || got[1] != 2) return 0;
        /* Now drain the retained future timer so global state resets cleanly. */
        usleep(120000); /* past 120ms */
        if (py_virtual_thread_poll_timers() != 1) return 0;
        if (py_virtual_thread_timer_count() != 0) return 0;
        n = drain_ready(got, 8);
        if (n != 1 || got[0] != 3) return 0;
        return 1;
    }
    return 0;
}

/* --- large-N drain crosses the internal drain batch (64) ------------------ */
static int check_large_n(void) {
    const int64_t N = 200; /* > PCC_VTHREAD_TIMER_DRAIN_BATCH */
    for (int64_t label = 0; label < N; label++) {
        /* One shared delay for every timer: each deadline is that park's own
         * now_ms + 5, so deadlines are nondecreasing in insertion order no
         * matter how often the ms clock ticks during this loop, and the
         * heap's seq tiebreak makes the wake order exactly the insertion
         * order 0..N-1. (Distinct per-label delays would re-derive expected
         * deadlines from the labels, silently assuming all N parks share one
         * clock read -- the insert loop routinely spans a ms boundary.) */
        if (park_timer(label, 5) != 0) return 0;
    }
    if (py_virtual_thread_timer_count() != N) return 0;
    usleep(40000);
    if (py_virtual_thread_poll_timers() != N) return 0;
    if (py_virtual_thread_timer_count() != 0) return 0;
    int64_t got[256];
    int64_t n = drain_ready(got, 256);
    if (n != N) return 0;
    /* Exactly insertion order: nondecreasing deadlines with FIFO seq
     * tiebreak inside each equal-deadline run. */
    for (int64_t i = 0; i < N; i++)
        if (got[i] != i) return 0;
    return 1;
}

static int check_backend(int64_t backend) {
    if (pcc_gc_set_backend(backend) != 0) return 0;
    pcc_gc_telemetry_reset();
    /* Each sub-check fully drains its own timers (timer_count back to 0), so
     * the global scheduler heap starts each phase clean. */
    if (!check_order()) return 0;
    if (!check_fifo_equal()) return 0;
    if (!check_partial_retention()) return 0;
    if (!check_large_n()) return 0;
    return py_virtual_thread_timer_count() == 0
        && py_virtual_thread_ready_count() == 0;
}

int main(void) {
    for (int64_t backend = 0; backend <= 4; backend++) {
        int ok = check_backend(backend);
        printf("%lld:%d\n", (long long)backend, ok);
        if (!ok) return (int)(40 + backend);
    }
    return 0;
}
"""


def _run_probe(
    tmp_path: Path,
    work_runtime: Path,
    archive: str,
    *,
    extra_link_args: list[str],
) -> None:
    src = tmp_path / f"{archive}_timer_heap_sched_probe.c"
    exe = tmp_path / f"{archive}_timer_heap_sched_probe.out"
    src.write_text(textwrap.dedent(_PROBE).lstrip(), encoding="utf-8")
    link = [
        _cc(),
        "-std=c11",
        f"-I{work_runtime / 'include'}",
        f"-I{work_runtime / 'src'}",
        str(src),
        str(work_runtime / archive),
    ]
    link.extend(extra_link_args)
    link.extend(["-o", str(exe)])
    build = subprocess.run(link, capture_output=True, text=True, timeout=60)
    assert build.returncode == 0, build.stderr
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines() == [
        "0:1",
        "1:1",
        "2:1",
        "3:1",
        "4:1",
    ]


@pytest.mark.pcc_gate(unavailable=None if _have_cc() else "no C compiler available")
def test_timer_heap_scheduler_ordering_c_runtime(tmp_path):
    work_runtime, archive, extra_link_args = _build_runtime(tmp_path)
    _run_probe(tmp_path, work_runtime, archive, extra_link_args=extra_link_args)


@pytest.mark.pcc_gate(unavailable=None if _have_cc() else "no C compiler available")
def test_timer_heap_scheduler_ordering_pcc_python_runtime(tmp_path):
    work_runtime, archive, extra_link_args = _build_runtime(
        tmp_path, pcc_python=True
    )
    _run_probe(tmp_path, work_runtime, archive, extra_link_args=extra_link_args)
