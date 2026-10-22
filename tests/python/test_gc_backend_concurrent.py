from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

from tests.runtime_build_cache import cached_pcc_python_runtime, cached_threaded_pcc_python_runtime


REPO_ROOT = Path(__file__).absolute().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"


def _cc() -> str:
    return os.environ.get("CC", "cc")


def _build_threaded_runtime(tmp_path: Path) -> Path:
    del tmp_path
    return cached_threaded_pcc_python_runtime()


def _build_nonthread_runtime(tmp_path: Path) -> Path:
    del tmp_path
    return cached_pcc_python_runtime()


def test_concurrent_backend_starts_worker_and_assists_allocations(tmp_path):
    work_runtime = _build_threaded_runtime(tmp_path)

    src = tmp_path / "cms_probe.c"
    exe = tmp_path / "cms_probe.out"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_runtime.h"
            #include <stdint.h>
            #include <stdio.h>
            #include <unistd.h>

            enum {
                CMS_WORKER_STARTS = 11,
                CMS_QUEUE_PUSHES = 12,
                CMS_WORKER_DRAINS = 13,
                CMS_MUTATOR_ASSISTS = 14
            };

            static int wait_for_drain(void) {
                for (int i = 0; i < 200; i++) {
                    if (pcc_gc_telemetry(CMS_WORKER_DRAINS) > 0) return 1;
                    pcc_gc_safepoint();
                    usleep(1000);
                }
                return 0;
            }

            int main(void) {
                if (pcc_threads_enabled() != 1) return 2;
                pcc_gc_telemetry_reset();

                if (pcc_gc_set_backend(PCC_GC_KIND_CONCURRENT_MARK_SWEEP) != 0) {
                    return 3;
                }
                if (pcc_gc_backend() != PCC_GC_KIND_CONCURRENT_MARK_SWEEP) {
                    return 4;
                }

                /* The worker begins a cycle once debt reaches the
                 * threshold; allocation that outruns it during the mark
                 * (debt past four thresholds) makes the mutator assist. A
                 * short burst can end before the worker's next poll, so
                 * allocate until the first assist, within a bound. As at
                 * the loop back-edges of compiled code, an application gate
                 * sweeps each mark the worker completes; the worker begins
                 * no cycle while one waits for that sweep. */
                for (int i = 0;
                     i < 2000000 && pcc_gc_telemetry(CMS_MUTATOR_ASSISTS) == 0;
                     i++) {
                    PyObject *o = pcc_gc_alloc(64, PY_TYPE_LIST, 0);
                    if (o == 0) return 5;
                    pcc_gc_release(o);
                    if ((i & 63) == 0) pcc_gc_safepoint();
                }

                printf("%lld\\n", (long long)pcc_gc_telemetry(CMS_WORKER_STARTS));
                printf("%lld\\n", (long long)pcc_gc_telemetry(CMS_QUEUE_PUSHES));
                printf("%d\\n", wait_for_drain());
                printf("%lld\\n", (long long)pcc_gc_telemetry(CMS_MUTATOR_ASSISTS));
                printf("%d\\n", pcc_gc_telemetry(PCC_GC_COUNTER_MAX_PAUSE_US) < 10000);
                return 0;
            }
            """
        ).lstrip()
    , encoding="utf-8")
    build = subprocess.run(
        [
            _cc(),
            "-std=c11",
            "-pthread",
            f"-I{work_runtime / 'include'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stderr

    env = os.environ.copy()
    env.update(
        {
            "PCC_GC_DEBT_THRESHOLD": "1024",
            "PCC_GC_STEPMUL": "200",
        }
    )
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert int(lines[0]) >= 1
    assert int(lines[1]) > 0
    assert lines[2] == "1"
    assert int(lines[3]) > 0
    assert lines[4] == "1"


def test_concurrent_backend_worker_traces_gray_barrier_work(tmp_path):
    work_runtime = _build_threaded_runtime(tmp_path)

    src = tmp_path / "cms_trace_probe.c"
    exe = tmp_path / "cms_trace_probe.out"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_runtime.h"
            #include <stdint.h>
            #include <stdio.h>
            #include <stdlib.h>
            #include <unistd.h>

            enum {
                CMS_WORKER_TRACES = 18
            };

            static int wait_for_worker_trace(void) {
                for (int i = 0; i < 200; i++) {
                    if (pcc_gc_telemetry(CMS_WORKER_TRACES) > 0) return 1;
                    pcc_thread_safepoint();
                    usleep(1000);
                }
                return 0;
            }

            int main(void) {
                if (pcc_threads_enabled() != 1) return 2;
                if (pcc_gc_set_backend(PCC_GC_KIND_CONCURRENT_MARK_SWEEP) != 0) {
                    return 3;
                }
                pcc_gc_telemetry_reset();
                if (pcc_stop_the_world() != 0) return 4;

                PyObject *root_a = pcc_gc_alloc(24, PY_TYPE_INT, 0);
                PyObject *root_b = pcc_gc_alloc(24, PY_TYPE_INT, 0);
                PyObject *owner = pcc_gc_alloc(24, PY_TYPE_INT, 0);
                PyObject *child = pcc_gc_alloc(24, PY_TYPE_INT, 0);
                PyObject **slot = (PyObject **)calloc(1, sizeof(PyObject *));
                if (
                    root_a == 0 || root_b == 0
                    || owner == 0 || child == 0 || slot == 0
                ) return 5;
                pcc_gc_pin(root_a);
                pcc_gc_pin(root_b);
                (void)pcc_gc_step(1);

                pcc_gc_store_ptr(owner, slot, child);
                if (pcc_resume_world() != 0) return 6;

                PyObject *ticket = pcc_gc_alloc(24, PY_TYPE_INT, 0);
                if (ticket == 0) return 7;
                pcc_gc_release(ticket);

                printf("%d\\n", wait_for_worker_trace());
                printf("%lld\\n", (long long)pcc_gc_telemetry(CMS_WORKER_TRACES));

                pcc_gc_store_ptr(owner, slot, 0);
                pcc_gc_unpin(root_a);
                pcc_gc_unpin(root_b);
                pcc_gc_release(root_a);
                pcc_gc_release(root_b);
                pcc_gc_release(owner);
                pcc_gc_release(child);
                free(slot);
                return 0;
            }
            """
        ).lstrip()
    , encoding="utf-8")
    build = subprocess.run(
        [
            _cc(),
            "-std=c11",
            "-pthread",
            f"-I{work_runtime / 'include'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stderr

    env = os.environ.copy()
    env["PCC_GC_DEBT_THRESHOLD"] = "1048576"
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "1"
    assert int(lines[1]) > 0


def test_concurrent_backend_batches_gray_barrier_flushes(tmp_path):
    work_runtime = _build_threaded_runtime(tmp_path)

    src = tmp_path / "cms_wb_buffer_probe.c"
    exe = tmp_path / "cms_wb_buffer_probe.out"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_runtime.h"
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>
            #include <stdlib.h>

            enum {
                CMS_QUEUE_PUSHES = 12,
                CMS_WB_FLUSHES = 23
            };

            int main(void) {
                if (pcc_threads_enabled() != 1) return 2;
                if (pcc_gc_set_backend(PCC_GC_KIND_CONCURRENT_MARK_SWEEP) != 0) {
                    return 3;
                }
                if (pcc_stop_the_world() != 0) return 4;

                PyObject *root_a = pcc_gc_alloc(24, PY_TYPE_NONE, 0);
                PyObject *root_b = pcc_gc_alloc(24, PY_TYPE_NONE, 0);
                PyObject *owner = pcc_gc_alloc(24, PY_TYPE_NONE, 0);
                PyObject **slots = (PyObject **)calloc(40, sizeof(PyObject *));
                if (root_a == 0 || root_b == 0 || owner == 0 || slots == 0) {
                    return 5;
                }
                pcc_gc_pin(root_a);
                pcc_gc_pin(root_b);
                (void)pcc_gc_step(1);
                pcc_gc_telemetry_reset();

                for (int i = 0; i < 40; i++) {
                    PyObject *child = pcc_gc_alloc(24, PY_TYPE_NONE, 0);
                    if (child == 0) return 6;
                    pcc_gc_store_ptr(owner, &slots[i], child);
                    pcc_gc_release(child);
                }
                (void)pcc_gc_step(1);

                printf("%lld\\n", (long long)pcc_gc_telemetry(CMS_WB_FLUSHES));
                printf("%lld\\n", (long long)pcc_gc_telemetry(CMS_QUEUE_PUSHES));

                for (int i = 0; i < 40; i++) {
                    pcc_gc_store_ptr(owner, &slots[i], 0);
                }
                pcc_gc_unpin(root_a);
                pcc_gc_unpin(root_b);
                pcc_gc_release(root_a);
                pcc_gc_release(root_b);
                pcc_gc_release(owner);
                free(slots);
                (void)pcc_resume_world();
                return 0;
            }
            """
        ).lstrip()
    , encoding="utf-8")
    build = subprocess.run(
        [
            _cc(),
            "-std=c11",
            "-pthread",
            f"-I{work_runtime / 'include'}",
            f"-I{work_runtime / 'src'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stderr

    env = os.environ.copy()
    env["PCC_GC_DEBT_THRESHOLD"] = "1048576"
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert int(lines[0]) >= 1
    assert int(lines[1]) >= 32


def _run_cms_probe(
    tmp_path: Path, work_runtime: Path, stem: str, source: str, *, threaded: bool
) -> subprocess.CompletedProcess[str]:
    src = tmp_path / f"{stem}.c"
    exe = tmp_path / f"{stem}.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    build = subprocess.run(
        [
            _cc(),
            "-std=c11",
            *(["-pthread"] if threaded else []),
            f"-I{work_runtime / 'include'}",
            f"-I{work_runtime / 'src'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-lm",
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stderr
    env = os.environ.copy()
    env["PCC_GC_DEBT_THRESHOLD"] = "1048576"
    return subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=30, env=env
    )


_CMS_COLOR_HELPERS = """
    #include "py_runtime.h"
    #include "py_internal.h"
    #include <stdint.h>
    #include <stdio.h>

    static int all_have_color(PyObject **objs, int count, int32_t color) {
        for (int i = 0; i < count; i++) {
            if (
                (py_header_flags_load(py_header(objs[i]))
                    & PY_FLAG_GC_COLOR_MASK) != color
            ) return 0;
        }
        return 1;
    }
"""


def test_concurrent_backend_barrier_grays_root_stores_inside_graph_lock_scope(
    tmp_path,
):
    """The pcc-Python CMS barrier shades on the store itself.

    A root store during marking grays its white value immediately -- also
    inside a caller-owned, nested graph-lock scope, where there is no later
    unlock to publish deferred work -- and the worker then traces every such
    identity to black.  Leaving and re-entering CMS restarts the worker
    exactly once.
    """
    work_runtime = _build_threaded_runtime(tmp_path)
    result = _run_cms_probe(
        tmp_path,
        work_runtime,
        "cms_barrier_nested_lock_probe",
        _CMS_COLOR_HELPERS
        + """
    int main(void) {
        if (pcc_threads_enabled() != 1) return 2;
        if (pcc_gc_set_backend(PCC_GC_KIND_CONCURRENT_MARK_SWEEP) != 0) {
            return 3;
        }
        if (pcc_stop_the_world() != 0) return 4;
        PyObject *anchor = pcc_gc_alloc(24, PY_TYPE_NONE, 0);
        if (anchor == 0) return 5;
        pcc_gc_pin(anchor);
        PyObject *children[40] = {0};
        PyObject *roots[40] = {0};
        for (int i = 0; i < 40; i++) {
            children[i] = pcc_gc_alloc(24, PY_TYPE_NONE, 0);
            if (children[i] == 0) return 6;
        }
        (void)pcc_gc_step(1);
        /* Model values the cycle has not reached yet. */
        for (int i = 0; i < 40; i++) {
            py_header_flags_update(
                py_header(children[i]), PY_FLAG_GC_COLOR_MASK, PY_FLAG_GC_WHITE
            );
        }
        pcc_gc_telemetry_reset();
        pcc_py_gc_minor_graph_lock();
        pcc_py_gc_minor_graph_lock();
        for (int i = 0; i < 40; i++) {
            pcc_gc_store_root(&roots[i], children[i]);
        }
        int gray_inside = all_have_color(children, 40, PY_FLAG_GC_GRAY);
        pcc_py_gc_minor_graph_unlock();
        pcc_py_gc_minor_graph_unlock();
        if (!gray_inside) return 7;
        if (pcc_gc_telemetry(PCC_GC_COUNTER_CMS_WB_FLUSHES) != 40) return 8;
        for (int i = 0; i < 40; i++) pcc_gc_release(children[i]);
        if (pcc_resume_world() != 0) return 9;

        int black = 0;
        for (int spin = 0; spin < 100000 && !black; spin++) {
            PyObject *ticket = pcc_gc_alloc(24, PY_TYPE_NONE, 0);
            if (ticket != 0) pcc_gc_release(ticket);
            pcc_gc_safepoint();
            black = all_have_color(children, 40, PY_FLAG_GC_BLACK);
        }
        if (!black) return 10;
        if (pcc_gc_telemetry(PCC_GC_COUNTER_CMS_WORKER_TRACES) <= 0) return 11;
        for (int i = 0; i < 40; i++) pcc_gc_store_root(&roots[i], 0);
        pcc_gc_unpin(anchor);
        pcc_gc_release(anchor);

        int64_t starts = pcc_gc_telemetry(PCC_GC_COUNTER_CMS_WORKER_STARTS);
        int64_t stops = pcc_gc_telemetry(PCC_GC_COUNTER_CMS_WORKER_STOPS);
        if (pcc_gc_set_backend(PCC_GC_KIND_REFCOUNT_CYCLE) != 0) return 12;
        if (pcc_gc_set_backend(PCC_GC_KIND_CONCURRENT_MARK_SWEEP) != 0) {
            return 13;
        }
        if (
            pcc_gc_telemetry(PCC_GC_COUNTER_CMS_WORKER_STARTS) != starts + 1
            || pcc_gc_telemetry(PCC_GC_COUNTER_CMS_WORKER_STOPS) != stops + 1
        ) return 14;
        printf("cms-barrier-nested-lock-ok\\n");
        return 0;
    }
    """,
        threaded=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "cms-barrier-nested-lock-ok"


def test_concurrent_backend_nonthread_barrier_stores_drain_to_black(tmp_path):
    """Without a worker thread, gc steps drain barrier-grayed roots."""
    work_runtime = _build_nonthread_runtime(tmp_path)
    result = _run_cms_probe(
        tmp_path,
        work_runtime,
        "cms_barrier_nonthread_probe",
        _CMS_COLOR_HELPERS
        + """
    int main(void) {
        if (pcc_threads_enabled() != 0) return 2;
        if (pcc_gc_set_backend(PCC_GC_KIND_CONCURRENT_MARK_SWEEP) != 0) {
            return 3;
        }
        PyObject *anchor = pcc_gc_alloc(24, PY_TYPE_NONE, 0);
        if (anchor == 0) return 4;
        pcc_gc_pin(anchor);
        PyObject *children[40] = {0};
        PyObject *roots[40] = {0};
        for (int i = 0; i < 40; i++) {
            children[i] = pcc_gc_alloc(24, PY_TYPE_NONE, 0);
            if (children[i] == 0) return 5;
        }
        (void)pcc_gc_step(1);
        for (int i = 0; i < 40; i++) {
            py_header_flags_update(
                py_header(children[i]), PY_FLAG_GC_COLOR_MASK, PY_FLAG_GC_WHITE
            );
        }
        pcc_gc_telemetry_reset();
        for (int i = 0; i < 40; i++) {
            pcc_gc_store_root(&roots[i], children[i]);
            pcc_gc_release(children[i]);
        }
        if (!all_have_color(children, 40, PY_FLAG_GC_GRAY)) return 6;
        if (pcc_gc_telemetry(PCC_GC_COUNTER_CMS_WB_FLUSHES) != 40) return 7;
        int black = 0;
        for (int spin = 0; spin < 1000 && !black; spin++) {
            (void)pcc_gc_step(64);
            black = all_have_color(children, 40, PY_FLAG_GC_BLACK);
        }
        if (!black) return 8;
        for (int i = 0; i < 40; i++) pcc_gc_store_root(&roots[i], 0);
        pcc_gc_unpin(anchor);
        pcc_gc_release(anchor);
        printf("cms-barrier-nonthread-ok\\n");
        return 0;
    }
    """,
        threaded=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "cms-barrier-nonthread-ok"


def test_concurrent_backend_switch_fails_closed_where_worker_join_would_deadlock(
    tmp_path,
):
    """Collector switches that must stop/start the CMS worker are refused
    from a graph-lock scope, a no-park scope, and by the stopped-world owner
    -- joining a worker that waits for that world never returns.  A refused
    switch changes nothing."""
    work_runtime = _build_threaded_runtime(tmp_path)
    result = _run_cms_probe(
        tmp_path,
        work_runtime,
        "cms_switch_guard_probe",
        """
    #include "py_runtime.h"
    #include "py_internal.h"
    #include <stdint.h>
    #include <stdio.h>

    static int unchanged(int64_t starts, int64_t stops) {
        return pcc_gc_backend() == PCC_GC_KIND_CONCURRENT_MARK_SWEEP
            && pcc_gc_telemetry(PCC_GC_COUNTER_CMS_WORKER_STARTS) == starts
            && pcc_gc_telemetry(PCC_GC_COUNTER_CMS_WORKER_STOPS) == stops;
    }

    int main(void) {
        if (pcc_threads_enabled() != 1) return 2;
        if (pcc_gc_set_backend(PCC_GC_KIND_CONCURRENT_MARK_SWEEP) != 0) {
            return 3;
        }
        /* Allocation tickets keep the worker busy with real work. */
        for (int i = 0; i < 64; i++) {
            PyObject *ticket = pcc_gc_alloc(24, PY_TYPE_NONE, 0);
            if (ticket != 0) pcc_gc_release(ticket);
        }
        int64_t starts = pcc_gc_telemetry(PCC_GC_COUNTER_CMS_WORKER_STARTS);
        int64_t stops = pcc_gc_telemetry(PCC_GC_COUNTER_CMS_WORKER_STOPS);
        if (starts < 1) return 4;

        pcc_py_gc_minor_graph_lock();
        if (pcc_gc_set_backend(PCC_GC_KIND_REFCOUNT_CYCLE) != -1) return 5;
        if (pcc_gc_set_backend(PCC_GC_KIND_CONCURRENT_MARK_SWEEP) != -1) {
            return 6;
        }
        pcc_py_gc_minor_graph_unlock();
        if (!unchanged(starts, stops)) return 7;

        pcc_thread_no_park_enter();
        if (pcc_gc_set_backend(PCC_GC_KIND_REFCOUNT_CYCLE) != -1) return 8;
        int no_park_depth = (int)pcc_thread_no_park_depth();
        pcc_thread_no_park_exit();
        if (no_park_depth != 1 || !unchanged(starts, stops)) return 9;

        if (pcc_stop_the_world() != 0) return 10;
        int64_t owner_switch = pcc_gc_set_backend(PCC_GC_KIND_REFCOUNT_CYCLE);
        int owns_world = (int)pcc_thread_owns_stopped_world();
        if (pcc_resume_world() != 0) return 11;
        if (owner_switch != -1 || owns_world != 1) return 12;
        if (!unchanged(starts, stops)) return 13;

        /* Unrestricted, the same switch commits and stops the worker. */
        if (pcc_gc_set_backend(PCC_GC_KIND_REFCOUNT_CYCLE) != 0) return 14;
        if (pcc_gc_backend() != PCC_GC_KIND_REFCOUNT_CYCLE) return 15;
        if (pcc_gc_telemetry(PCC_GC_COUNTER_CMS_WORKER_STOPS) != stops + 1) {
            return 16;
        }
        printf("cms-switch-guard-ok\\n");
        return 0;
    }
    """,
        threaded=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "cms-switch-guard-ok"


def test_concurrent_backend_worker_traces_positive_allocation_work(tmp_path):
    work_runtime = _build_threaded_runtime(tmp_path)

    src = tmp_path / "cms_positive_work_probe.c"
    exe = tmp_path / "cms_positive_work_probe.out"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_runtime.h"
            #include <stdint.h>
            #include <stdio.h>
            #include <unistd.h>

            enum {
                CMS_WORKER_DRAINS = 13,
                CMS_WORKER_TRACES = 18
            };

            static int wait_for_worker_trace(void) {
                for (int i = 0; i < 200; i++) {
                    if (pcc_gc_telemetry(CMS_WORKER_TRACES) > 0) return 1;
                    pcc_thread_safepoint();
                    usleep(1000);
                }
                return 0;
            }

            int main(void) {
                if (pcc_threads_enabled() != 1) return 2;
                if (pcc_gc_set_backend(PCC_GC_KIND_CONCURRENT_MARK_SWEEP) != 0) {
                    return 3;
                }
                pcc_gc_telemetry_reset();

                PyObject *root = pcc_gc_alloc(24, PY_TYPE_INT, 0);
                if (root == 0) return 4;
                pcc_gc_pin(root);

                PyObject *ticket = pcc_gc_alloc(64, PY_TYPE_INT, 0);
                if (ticket == 0) return 5;
                pcc_gc_release(ticket);

                printf("%d\\n", wait_for_worker_trace());
                printf("%lld\\n", (long long)pcc_gc_telemetry(CMS_WORKER_DRAINS));
                printf("%lld\\n", (long long)pcc_gc_telemetry(CMS_WORKER_TRACES));

                pcc_gc_unpin(root);
                pcc_gc_release(root);
                return 0;
            }
            """
        ).lstrip()
    , encoding="utf-8")
    build = subprocess.run(
        [
            _cc(),
            "-std=c11",
            "-pthread",
            f"-I{work_runtime / 'include'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stderr

    env = os.environ.copy()
    # The worker begins a cycle once debt reaches the threshold: these
    # 64-byte tickets make one due. A mutator assists only past four
    # thresholds of debt during a mark, which they never reach, so the
    # worker does all the marking.
    env["PCC_GC_DEBT_THRESHOLD"] = "64"
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "1"
    assert int(lines[1]) > 0
    assert int(lines[2]) > 0


def test_concurrent_backend_worker_reaches_mark_termination_without_mutator_gc_step(tmp_path):
    work_runtime = _build_threaded_runtime(tmp_path)

    src = tmp_path / "cms_mark_termination_probe.c"
    exe = tmp_path / "cms_mark_termination_probe.out"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_runtime.h"
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>
            #include <unistd.h>

            enum {
                CMS_WORKER_DRAINS = 13
            };

            /* Set by the worker when it completes a mark: the next
             * application gate sweeps it, and no cycle begins before. */
            extern int32_t pcc_gc_backend4_auto_step_pending;

            static int wait_for_worker_sweep_debt(void) {
                for (int i = 0; i < 200; i++) {
                    if (pcc_gc_has_tracing_sweep() != 0) return 1;
                    pcc_thread_safepoint();
                    usleep(1000);
                }
                return 0;
            }

            static int wait_for_worker_drain(void) {
                for (int i = 0; i < 200; i++) {
                    if (pcc_gc_telemetry(CMS_WORKER_DRAINS) > 0) return 1;
                    pcc_thread_safepoint();
                    usleep(1000);
                }
                return 0;
            }

            static int wait_for_worker_completed_mark(void) {
                for (int i = 0; i < 200; i++) {
                    if (__atomic_load_n(
                            &pcc_gc_backend4_auto_step_pending, __ATOMIC_ACQUIRE
                        ) != 0) {
                        return 1;
                    }
                    pcc_thread_safepoint();
                    usleep(1000);
                }
                return 0;
            }

            int main(void) {
                if (pcc_threads_enabled() != 1) return 2;
                if (pcc_gc_set_backend(PCC_GC_KIND_INCREMENTAL_TRICOLOR) != 0) {
                    return 3;
                }

                PyObject *cycle = py_list_new(1);
                if (cycle == 0) return 4;
                py_list_append(cycle, cycle);
                pcc_gc_release(cycle);

                pcc_gc_telemetry_reset();
                if (pcc_gc_set_backend(PCC_GC_KIND_CONCURRENT_MARK_SWEEP) != 0) {
                    return 5;
                }

            PyObject *ticket = pcc_gc_alloc(64, PY_TYPE_INT, 0);
            if (ticket == 0) return 6;
            pcc_gc_release(ticket);
            if (!wait_for_worker_drain()) return 7;
            /* The list's last store under GC1 shaded it, so the first GC2
             * mark keeps it (floating garbage). As at a loop back-edge of
             * compiled code, an application gate sweeps that mark; the next
             * ticket then begins the mark that finds the list dead. */
            if (!wait_for_worker_completed_mark()) return 9;
            pcc_gc_safepoint();

            PyObject *ticket2 = pcc_gc_alloc(64, PY_TYPE_INT, 0);
            if (ticket2 == 0) return 8;
            pcc_gc_release(ticket2);

            printf("%d\\n", wait_for_worker_sweep_debt());
            printf("%lld\\n", (long long)pcc_gc_telemetry(CMS_WORKER_DRAINS));
            printf("%lld\\n", (long long)pcc_gc_collect_tracing());
                return 0;
            }
            """
        ).lstrip()
    , encoding="utf-8")
    build = subprocess.run(
        [
            _cc(),
            "-std=c11",
            "-pthread",
            f"-I{work_runtime / 'include'}",
            f"-I{work_runtime / 'src'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stderr

    env = os.environ.copy()
    # The worker begins a cycle once debt reaches the threshold: these
    # 64-byte tickets make one due. A mutator assists only past four
    # thresholds of debt during a mark, which they never reach, so the
    # worker does all the marking.
    env["PCC_GC_DEBT_THRESHOLD"] = "64"
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "1"
    assert int(lines[1]) > 0
    assert int(lines[2]) > 0


def test_concurrent_backend_worker_stops_and_restarts_on_backend_switch(tmp_path):
    work_runtime = _build_threaded_runtime(tmp_path)

    src = tmp_path / "cms_lifecycle_probe.c"
    exe = tmp_path / "cms_lifecycle_probe.out"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_runtime.h"
            #include <stdint.h>
            #include <stdio.h>
            #include <unistd.h>

            enum {
                CMS_WORKER_STARTS = 11,
                CMS_WORKER_DRAINS = 13,
                CMS_WORKER_STOPS = 22
            };

            static int wait_for_drain(void) {
                for (int i = 0; i < 500; i++) {
                    if (pcc_gc_telemetry(CMS_WORKER_DRAINS) > 0) return 1;
                    pcc_thread_safepoint();
                    usleep(1000);
                }
                return 0;
            }

            static int drive_work(void) {
                PyObject *root = pcc_gc_alloc(24, PY_TYPE_INT, 0);
                if (root == 0) return 0;
                pcc_gc_pin(root);
                PyObject *ticket = pcc_gc_alloc(64, PY_TYPE_INT, 0);
                if (ticket == 0) return 0;
                pcc_gc_release(ticket);
                int drained = wait_for_drain();
                pcc_gc_unpin(root);
                pcc_gc_release(root);
                return drained;
            }

            int main(void) {
                if (pcc_threads_enabled() != 1) return 2;
                pcc_gc_telemetry_reset();

                if (pcc_gc_set_backend(PCC_GC_KIND_CONCURRENT_MARK_SWEEP) != 0) {
                    return 3;
                }
                if (!drive_work()) return 4;

                if (pcc_gc_set_backend(PCC_GC_KIND_REFCOUNT_CYCLE) != 0) {
                    return 5;
                }
                printf("%lld\\n", (long long)pcc_gc_telemetry(CMS_WORKER_STOPS));

                if (pcc_gc_set_backend(PCC_GC_KIND_CONCURRENT_MARK_SWEEP) != 0) {
                    return 6;
                }
                if (!drive_work()) return 7;
                printf("%lld\\n", (long long)pcc_gc_telemetry(CMS_WORKER_STARTS));

                if (pcc_gc_set_backend(PCC_GC_KIND_REFCOUNT_CYCLE) != 0) {
                    return 8;
                }
                printf("%lld\\n", (long long)pcc_gc_telemetry(CMS_WORKER_STOPS));
                return 0;
            }
            """
        ).lstrip()
    , encoding="utf-8")
    build = subprocess.run(
        [
            _cc(),
            "-std=c11",
            "-pthread",
            f"-I{work_runtime / 'include'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stderr

    env = os.environ.copy()
    env["PCC_GC_DEBT_THRESHOLD"] = "1048576"
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert int(lines[0]) >= 1
    assert int(lines[1]) >= 2
    assert int(lines[2]) >= 2
