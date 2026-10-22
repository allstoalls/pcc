"""Cext traversal outside the graph lock, registered-root promotion batching, remembered-root drains, CMS wb queue.

Split from ``test_gc_threading_substrate.py``; collected only through
that facade so pytest node ids stay stable.
"""
from _gc_substrate_common import *  # noqa: F401,F403


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_generational_cext_traverse_runs_outside_graph_lock(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="generational_cext_traverse_unlocked",
        extra_include_dirs=(REPO_ROOT / "utils" / "fake_libc_include",),
        source_text=r'''
            #include "Python.h"
            #include <sched.h>
            #include <stdint.h>

            typedef struct ProbeCextObject {
                PyObject ob_base;
                PyObject *child;
            } ProbeCextObject;

            extern int sched_yield(void);
            extern int64_t pcc_gc_object_is_known(PyObject *obj);
            extern int64_t pcc_thread_start(
                void **out, void *(*entry)(void *), void *arg
            );
            extern int64_t pcc_thread_join(void *handle, void **result);

            static void *contender;
            static PyObject *anchor;
            static int64_t worker_ready;
            static int64_t worker_go;
            static int64_t worker_acquired;
            static int64_t callback_lock_free;
            static int64_t callback_armed;

            static void *lock_contender(void *unused) {
                (void)unused;
                __atomic_store_n(&worker_ready, 1, __ATOMIC_RELEASE);
                while (__atomic_load_n(&worker_go, __ATOMIC_ACQUIRE) == 0) {
                    sched_yield();
                }
                if (pcc_gc_object_is_known(anchor) != 1) return (void *)2;
                __atomic_store_n(&worker_acquired, 1, __ATOMIC_RELEASE);
                return 0;
            }

            static int probe_traverse(
                PyObject *self, visitproc visit, void *arg
            ) {
                ProbeCextObject *obj = (ProbeCextObject *)self;
                if (__atomic_load_n(&callback_armed, __ATOMIC_ACQUIRE) != 0) {
                    __atomic_store_n(&worker_go, 1, __ATOMIC_RELEASE);
                    int spins = 0;
                    while (
                        __atomic_load_n(
                            &worker_acquired, __ATOMIC_ACQUIRE
                        ) == 0
                        && spins < 2000000
                    ) {
                        sched_yield();
                        spins++;
                    }
                    if (__atomic_load_n(
                            &worker_acquired, __ATOMIC_ACQUIRE
                        ) != 0) {
                        __atomic_store_n(
                            &callback_lock_free, 1, __ATOMIC_RELEASE
                        );
                    }
                }
                Py_VISIT(obj->child);
                return 0;
            }

            static PyTypeObject ProbeType = {
                .tp_name = "pcc_probe.GcTraverse",
                .tp_basicsize = sizeof(ProbeCextObject),
                .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
                .tp_traverse = probe_traverse,
            };

            int main(void) {
                static const int32_t frame_map[1] = {1};
                PyObject *roots[1] = {0};
                if (pcc_threads_enabled() != 1) return 2;
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
                    ) != 0) return 3;
                if (PyType_Ready(&ProbeType) != 0) return 4;

                ProbeCextObject *obj = (ProbeCextObject *)PyType_GenericAlloc(
                    &ProbeType, 0
                );
                anchor = py_list_new(0);
                if (obj == 0 || anchor == 0) return 5;
                if (pcc_gc_object_is_known((PyObject *)obj) != 1) return 14;
                roots[0] = (PyObject *)obj;
                pcc_gc_frame_enter(frame_map, roots);
                (void)pcc_gc_step(1024);
                obj = (ProbeCextObject *)roots[0];
                if ((((PyObjectHeader *)obj)->flags & 0x100) == 0) {
                    return 6;
                }

                PyObject *child = py_list_new(0);
                if (child == 0) return 7;
                if (pcc_gc_object_is_known(child) != 1) return 15;
                if ((((PyObjectHeader *)child)->flags & 0x80) == 0) return 16;
                pcc_gc_store_ptr((PyObject *)obj, &obj->child, child);
                if (pcc_thread_start(&contender, lock_contender, 0) != 0) {
                    return 8;
                }
                while (__atomic_load_n(&worker_ready, __ATOMIC_ACQUIRE) == 0) {
                    sched_yield();
                }
                __atomic_store_n(&callback_armed, 1, __ATOMIC_RELEASE);
                (void)pcc_gc_step(1024);
                int callback_ran = __atomic_load_n(
                    &worker_go, __ATOMIC_ACQUIRE
                ) != 0;
                if (!callback_ran) {
                    __atomic_store_n(&worker_go, 1, __ATOMIC_RELEASE);
                }
                void *worker_result = 0;
                if (
                    pcc_thread_join(contender, &worker_result) != 0
                    || worker_result != 0
                ) return 10;
                if (!callback_ran) return 13;
                if (__atomic_load_n(
                        &callback_lock_free, __ATOMIC_ACQUIRE
                    ) != 1) return 11;
                if (obj->child != child) return 12;
                if (
                    (((PyObjectHeader *)child)->flags & 0x80) != 0
                    || (((PyObjectHeader *)child)->flags & 0x100) == 0
                ) return 17;

                pcc_gc_frame_leave(roots);
                pcc_gc_store_ptr((PyObject *)obj, &obj->child, 0);
                py_decref(child);
                py_decref((PyObject *)obj);
                py_decref(anchor);
                return 0;
            }
        ''',
    )
    try:
        run = subprocess.run(
            [str(executable)], capture_output=True, text=True, timeout=30
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"{kind} C-extension traverse probe timed out: "
            f"stdout={exc.stdout!r} stderr={exc.stderr!r}"
        )
    assert run.returncode == 0, (
        f"{kind} C-extension traverse probe returned {run.returncode}: "
        + run.stdout
        + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_incremental_trace_cext_traverse_runs_outside_graph_lock(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="incremental_trace_cext_traverse_unlocked",
        extra_include_dirs=(REPO_ROOT / "utils" / "fake_libc_include",),
        source_text=r'''
            #include "Python.h"
            #include <sched.h>
            #include <stdint.h>

            typedef struct ProbeCextObject {
                PyObject ob_base;
                PyObject *child;
            } ProbeCextObject;

            extern int sched_yield(void);
            extern int64_t pcc_gc_object_is_known(PyObject *obj);
            extern int64_t pcc_thread_start(
                void **out, void *(*entry)(void *), void *arg
            );
            extern int64_t pcc_thread_join(void *handle, void **result);

            static void *contender;
            static PyObject *anchor;
            static int64_t worker_ready;
            static int64_t worker_go;
            static int64_t worker_acquired;
            static int64_t callback_lock_free;
            static int64_t callback_armed;

            static void *lock_contender(void *unused) {
                (void)unused;
                __atomic_store_n(&worker_ready, 1, __ATOMIC_RELEASE);
                while (__atomic_load_n(&worker_go, __ATOMIC_ACQUIRE) == 0) {
                    sched_yield();
                }
                if (pcc_gc_object_is_known(anchor) != 1) return (void *)2;
                __atomic_store_n(&worker_acquired, 1, __ATOMIC_RELEASE);
                return 0;
            }

            static int probe_traverse(
                PyObject *self, visitproc visit, void *arg
            ) {
                ProbeCextObject *obj = (ProbeCextObject *)self;
                if (__atomic_load_n(&callback_armed, __ATOMIC_ACQUIRE) != 0) {
                    __atomic_store_n(&worker_go, 1, __ATOMIC_RELEASE);
                    int spins = 0;
                    while (
                        __atomic_load_n(
                            &worker_acquired, __ATOMIC_ACQUIRE
                        ) == 0
                        && spins < 2000000
                    ) {
                        sched_yield();
                        spins++;
                    }
                    if (__atomic_load_n(
                            &worker_acquired, __ATOMIC_ACQUIRE
                        ) != 0) {
                        __atomic_store_n(
                            &callback_lock_free, 1, __ATOMIC_RELEASE
                        );
                    }
                }
                Py_VISIT(obj->child);
                return 0;
            }

            static PyTypeObject ProbeType = {
                .tp_name = "pcc_probe.TraceTraverse",
                .tp_basicsize = sizeof(ProbeCextObject),
                .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
                .tp_traverse = probe_traverse,
            };

            int main(void) {
                int32_t frame_map[18] = {17};
                PyObject *roots[17] = {0};
                if (pcc_threads_enabled() != 1) return 2;
                /* Only the explicit steps below advance the cycle: with
                 * collection disabled the GC1 collector thread stays out. */
                py_gc_disable();
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_INCREMENTAL_TRICOLOR
                    ) != 0) return 3;
                if (PyType_Ready(&ProbeType) != 0) return 4;
                ProbeCextObject *obj = (ProbeCextObject *)PyType_GenericAlloc(
                    &ProbeType, 0
                );
                PyObject *child = py_list_new(0);
                anchor = py_list_new(0);
                if (obj == 0 || child == 0 || anchor == 0) return 5;
                pcc_gc_store_ptr((PyObject *)obj, &obj->child, child);
                roots[0] = (PyObject *)obj;
                for (int i = 1; i < 17; i++) {
                    roots[i] = py_list_new(0);
                    if (roots[i] == 0) return 10 + i;
                }
                pcc_gc_frame_enter(frame_map, roots);

                /* Begin the cycle and consume at most one newest filler while
                 * the callback is disarmed. The C-extension owner was linked
                 * before all sixteen fillers, so it cannot be reached yet. */
                (void)pcc_gc_step(1);
                (void)pcc_gc_step(1);

                if (pcc_thread_start(&contender, lock_contender, 0) != 0) {
                    return 40;
                }
                while (__atomic_load_n(&worker_ready, __ATOMIC_ACQUIRE) == 0) {
                    sched_yield();
                }
                __atomic_store_n(&callback_armed, 1, __ATOMIC_RELEASE);
                int steps = 0;
                while (
                    __atomic_load_n(&worker_go, __ATOMIC_ACQUIRE) == 0
                    && steps < 64
                ) {
                    (void)pcc_gc_step(1);
                    steps++;
                }
                int callback_ran = __atomic_load_n(
                    &worker_go, __ATOMIC_ACQUIRE
                ) != 0;
                if (!callback_ran) {
                    __atomic_store_n(&worker_go, 1, __ATOMIC_RELEASE);
                }
                void *worker_result = 0;
                if (
                    pcc_thread_join(contender, &worker_result) != 0
                    || worker_result != 0
                ) return 41;
                if (!callback_ran) return 42;
                if (__atomic_load_n(
                        &callback_lock_free, __ATOMIC_ACQUIRE
                    ) != 1) return 43;

                pcc_gc_frame_leave(roots);
                pcc_gc_store_ptr((PyObject *)obj, &obj->child, 0);
                py_decref(child);
                py_decref((PyObject *)obj);
                for (int i = 1; i < 17; i++) py_decref(roots[i]);
                py_decref(anchor);
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} incremental C-extension trace probe returned "
        f"{run.returncode}: " + run.stdout + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_initial_seed_cext_traverse_owns_stw_without_graph_lock(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="initial_seed_cext_traverse_unlocked",
        extra_include_dirs=(REPO_ROOT / "utils" / "fake_libc_include",),
        source_text=(
            "#define PCC_PROBE_STRICT "
            + ("1\n")
            + r'''
            #include "Python.h"
            #include <sched.h>
            #include <stdint.h>

            typedef void *probe_pthread_t;

            typedef struct ProbeCextObject {
                PyObject ob_base;
            } ProbeCextObject;

            extern int sched_yield(void);
            extern int64_t pcc_thread_owns_stopped_world(void);
            extern int64_t pcc_thread_no_park_depth(void);
            extern int pthread_create(
                probe_pthread_t *thread,
                const void *attr,
                void *(*entry)(void *),
                void *arg
            );
            extern int pthread_join(probe_pthread_t thread, void **result);

            static PyObject *anchor;
            static int64_t callback_armed = 1;
            static int64_t callback_seen;
            static int64_t callback_stw;
            static int64_t callback_no_park;
            static int64_t callback_lock_free;
            static int64_t callback_reset_result;
            static int64_t worker_go;
            static int64_t worker_acquired;

            static void *raw_lock_contender(void *unused) {
                (void)unused;
                while (__atomic_load_n(&worker_go, __ATOMIC_ACQUIRE) == 0) {
                    sched_yield();
                }
                __atomic_store_n(&worker_acquired, 1, __ATOMIC_RELEASE);
                return 0;
            }

            static int probe_traverse(
                PyObject *self, visitproc visit, void *arg
            ) {
                (void)self;
                (void)visit;
                (void)arg;
                if (__atomic_exchange_n(
                        &callback_armed, 0, __ATOMIC_ACQ_REL
                    ) == 0) return 0;
                __atomic_store_n(&callback_seen, 1, __ATOMIC_RELEASE);
                __atomic_store_n(
                    &callback_stw,
                    pcc_thread_owns_stopped_world(),
                    __ATOMIC_RELEASE
                );
                __atomic_store_n(
                    &callback_no_park,
                    pcc_thread_no_park_depth(),
                    __ATOMIC_RELEASE
                );
                __atomic_store_n(
                    &callback_reset_result,
                    pcc_gc_set_backend(PCC_GC_KIND_INCREMENTAL_TRICOLOR),
                    __ATOMIC_RELEASE
                );
                __atomic_store_n(&worker_go, 1, __ATOMIC_RELEASE);
                int spins = 0;
                while (
                    __atomic_load_n(&worker_acquired, __ATOMIC_ACQUIRE) == 0
                    && spins < 2000000
                ) {
                    sched_yield();
                    spins++;
                }
                if (__atomic_load_n(
                        &worker_acquired, __ATOMIC_ACQUIRE
                    ) != 0) {
                    __atomic_store_n(
                        &callback_lock_free, 1, __ATOMIC_RELEASE
                    );
                }
                return 0;
            }

            static PyTypeObject ProbeType = {
                .tp_name = "pcc_probe.InitialSeedTraverse",
                .tp_basicsize = sizeof(ProbeCextObject),
                .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
                .tp_traverse = probe_traverse,
            };

            int main(void) {
                static const int32_t frame_map[1] = {1};
                PyObject *roots[1] = {0};
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_INCREMENTAL_TRICOLOR
                    ) != 0) return 2;
                if (PyType_Ready(&ProbeType) != 0) return 3;
                roots[0] = (PyObject *)PyType_GenericAlloc(&ProbeType, 0);
                anchor = py_list_new(0);
                if (roots[0] == 0 || anchor == 0) return 4;
                pcc_gc_frame_enter(frame_map, roots);

                probe_pthread_t contender;
                if (pthread_create(
                        &contender, 0, raw_lock_contender, 0
                    ) != 0) return 5;
                (void)pcc_gc_step(1);
                if (__atomic_load_n(&worker_go, __ATOMIC_ACQUIRE) == 0) {
                    __atomic_store_n(&worker_go, 1, __ATOMIC_RELEASE);
                }
                void *thread_result = 0;
                if (pthread_join(contender, &thread_result) != 0) return 6;
                if (thread_result != 0) return 7;
                if (__atomic_load_n(
                        &callback_seen, __ATOMIC_ACQUIRE
                    ) != 1) return 8;
                if (__atomic_load_n(
                        &callback_stw, __ATOMIC_ACQUIRE
                    ) != 1) return 9;
                if (__atomic_load_n(
                        &callback_no_park, __ATOMIC_ACQUIRE
                    ) != 0) return 14;
                if (__atomic_load_n(
                        &callback_lock_free, __ATOMIC_ACQUIRE
                    ) != 1) return 10;
                if (__atomic_load_n(
                        &callback_reset_result, __ATOMIC_ACQUIRE
                    ) != -1) return 12;

                pcc_gc_frame_leave(roots);
                if (pcc_gc_set_backend(PCC_GC_KIND_REFCOUNT_CYCLE) != 0) {
                    return 13;
                }
                py_decref(roots[0]);
                py_decref(anchor);
                return 0;
            }
        '''),
    )
    try:
        run = subprocess.run(
            [str(executable)], capture_output=True, text=True, timeout=30
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"{kind} initial-seed C-extension probe timed out: "
            f"stdout={exc.stdout!r} stderr={exc.stderr!r}"
        )
    assert run.returncode == 0, (
        f"{kind} initial-seed C-extension probe returned {run.returncode}: "
        + run.stdout
        + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_final_cut_cext_traverse_runs_outside_graph_lock(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="final_cut_cext_traverse_unlocked",
        extra_include_dirs=(REPO_ROOT / "utils" / "fake_libc_include",),
        source_text=(
            "#define PCC_PROBE_STRICT "
            + ("1\n")
            + r'''
            #include "Python.h"
            #include <sched.h>
            #include <stdint.h>

            typedef void *probe_pthread_t;

            typedef struct ProbeCextObject {
                PyObject ob_base;
            } ProbeCextObject;

            extern int sched_yield(void);
            extern int64_t pcc_thread_owns_stopped_world(void);
            extern int64_t pcc_thread_no_park_depth(void);
            extern int pthread_create(
                probe_pthread_t *thread,
                const void *attr,
                void *(*entry)(void *),
                void *arg
            );
            extern int pthread_join(probe_pthread_t thread, void **result);

            static PyObject *anchor;
            static int64_t traverse_count;
            static int64_t callback_armed;
            static int64_t worker_go;
            static int64_t worker_acquired;
            static int64_t callback_lock_free;
            static int64_t callback_stw;
            static int64_t callback_no_park;

            static void *raw_lock_contender(void *unused) {
                (void)unused;
                while (__atomic_load_n(&worker_go, __ATOMIC_ACQUIRE) == 0) {
                    sched_yield();
                }
                __atomic_store_n(&worker_acquired, 1, __ATOMIC_RELEASE);
                return 0;
            }

            static int probe_traverse(
                PyObject *self, visitproc visit, void *arg
            ) {
                (void)self;
                (void)visit;
                (void)arg;
                __atomic_add_fetch(&traverse_count, 1, __ATOMIC_ACQ_REL);
                if (__atomic_load_n(
                        &callback_armed, __ATOMIC_ACQUIRE
                    ) == 0) return 0;
                __atomic_store_n(
                    &callback_stw,
                    pcc_thread_owns_stopped_world(),
                    __ATOMIC_RELEASE
                );
                __atomic_store_n(
                    &callback_no_park,
                    pcc_thread_no_park_depth(),
                    __ATOMIC_RELEASE
                );
                __atomic_store_n(&worker_go, 1, __ATOMIC_RELEASE);
                int spins = 0;
                while (
                    __atomic_load_n(&worker_acquired, __ATOMIC_ACQUIRE) == 0
                    && spins < 2000000
                ) {
                    sched_yield();
                    spins++;
                }
                if (__atomic_load_n(
                        &worker_acquired, __ATOMIC_ACQUIRE
                    ) != 0) {
                    __atomic_store_n(
                        &callback_lock_free, 1, __ATOMIC_RELEASE
                    );
                }
                return 0;
            }

            static PyTypeObject ProbeType = {
                .tp_name = "pcc_probe.FinalCutTraverse",
                .tp_basicsize = sizeof(ProbeCextObject),
                .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
                .tp_traverse = probe_traverse,
            };

            int main(void) {
                int32_t frame_map[19] = {18};
                PyObject *roots[18] = {0};
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_INCREMENTAL_TRICOLOR
                    ) != 0) return 2;
                if (PyType_Ready(&ProbeType) != 0) return 3;

                /* The oldest tail keeps the trace cursor non-null after the
                 * C-extension owner's ordinary trace. Sixteen newer fillers
                 * put the owner behind deterministic budget-1 work. */
                roots[17] = py_list_new(0);
                roots[0] = (PyObject *)PyType_GenericAlloc(&ProbeType, 0);
                anchor = py_list_new(0);
                if (roots[17] == 0 || roots[0] == 0 || anchor == 0) return 4;
                for (int i = 1; i < 17; i++) {
                    roots[i] = py_list_new(0);
                    if (roots[i] == 0) return 10 + i;
                }
                pcc_gc_frame_enter(frame_map, roots);

                int pre_steps = 0;
                while (
                    __atomic_load_n(&traverse_count, __ATOMIC_ACQUIRE) < 2
                    && pre_steps < 64
                ) {
                    (void)pcc_gc_step(1);
                    pre_steps++;
                }
                if (__atomic_load_n(
                        &traverse_count, __ATOMIC_ACQUIRE
                    ) < 2) return 40;

                probe_pthread_t contender;
                if (pthread_create(
                        &contender, 0, raw_lock_contender, 0
                    ) != 0) return 41;
                __atomic_store_n(&callback_armed, 1, __ATOMIC_RELEASE);
                int finish_steps = 0;
                while (
                    __atomic_load_n(&worker_go, __ATOMIC_ACQUIRE) == 0
                    && finish_steps < 128
                ) {
                    (void)pcc_gc_step(1);
                    finish_steps++;
                }
                int callback_ran = __atomic_load_n(
                    &worker_go, __ATOMIC_ACQUIRE
                ) != 0;
                if (!callback_ran) {
                    __atomic_store_n(&worker_go, 1, __ATOMIC_RELEASE);
                }
                void *thread_result = 0;
                if (pthread_join(contender, &thread_result) != 0) return 42;
                if (thread_result != 0) return 43;
                if (!callback_ran) return 44;
                if (__atomic_load_n(
                        &callback_stw, __ATOMIC_ACQUIRE
                    ) != 1) return 45;
                if (__atomic_load_n(
                        &callback_no_park, __ATOMIC_ACQUIRE
                    ) != 0) return 48;
                if (__atomic_load_n(
                        &callback_lock_free, __ATOMIC_ACQUIRE
                    ) != 1) return 46;

                pcc_gc_frame_leave(roots);
                if (pcc_gc_set_backend(PCC_GC_KIND_REFCOUNT_CYCLE) != 0) {
                    return 47;
                }
                for (int i = 0; i < 18; i++) py_decref(roots[i]);
                py_decref(anchor);
                return 0;
            }
        '''),
    )
    try:
        run = subprocess.run(
            [str(executable)], capture_output=True, text=True, timeout=30
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"{kind} final-cut C-extension probe timed out: "
            f"stdout={exc.stdout!r} stderr={exc.stderr!r}"
        )
    assert run.returncode == 0, (
        f"{kind} final-cut C-extension probe returned {run.returncode}: "
        + run.stdout
        + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_remap_cext_callback_heals_slot_outside_graph_lock(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_remap_cext_unlocked",
        extra_include_dirs=(REPO_ROOT / "utils" / "fake_libc_include",),
        source_text=(
            "#define PCC_PROBE_STRICT "
            + ("1\n")
            + r'''
            #include "Python.h"
            #include <sched.h>
            #include <stdint.h>

            typedef void *probe_pthread_t;

            typedef struct ProbeCextObject {
                PyObject ob_base;
                PyObject *child;
            } ProbeCextObject;

            extern int sched_yield(void);
            extern int64_t pcc_gc_install_forwarding(PyObject *from, PyObject *to);
            extern int64_t pcc_gc_backend4_remap_and_retire_stopped_world(void);
            extern int64_t pcc_thread_owns_stopped_world(void);
            extern int64_t pcc_thread_no_park_depth(void);
            extern int pthread_create(
                probe_pthread_t *thread,
                const void *attr,
                void *(*entry)(void *),
                void *arg
            );
            extern int pthread_join(probe_pthread_t thread, void **result);

            static PyObject *anchor;
            static int64_t callback_armed = 1;
            static int64_t callback_seen;
            static int64_t callback_stw;
            static int64_t callback_no_park;
            static int64_t callback_lock_free;
            static int64_t callback_reset_result;
            static int64_t callback_nested_remap;
            static int64_t worker_go;
            static int64_t worker_acquired;

            static void *raw_lock_contender(void *unused) {
                (void)unused;
                while (__atomic_load_n(&worker_go, __ATOMIC_ACQUIRE) == 0) {
                    sched_yield();
                }
                __atomic_store_n(&worker_acquired, 1, __ATOMIC_RELEASE);
                return 0;
            }

            static int probe_traverse(
                PyObject *self, visitproc visit, void *arg
            ) {
                ProbeCextObject *obj = (ProbeCextObject *)self;
                if (__atomic_exchange_n(
                        &callback_armed, 0, __ATOMIC_ACQ_REL
                    ) != 0) {
                    __atomic_store_n(&callback_seen, 1, __ATOMIC_RELEASE);
                    __atomic_store_n(
                        &callback_stw,
                        pcc_thread_owns_stopped_world(),
                        __ATOMIC_RELEASE
                    );
                    __atomic_store_n(
                        &callback_no_park,
                        pcc_thread_no_park_depth(),
                        __ATOMIC_RELEASE
                    );
                    __atomic_store_n(
                        &callback_reset_result,
                        pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING),
                        __ATOMIC_RELEASE
                    );
                    __atomic_store_n(
                        &callback_nested_remap,
                        pcc_gc_backend4_remap_and_retire_stopped_world(),
                        __ATOMIC_RELEASE
                    );
                    __atomic_store_n(&worker_go, 1, __ATOMIC_RELEASE);
                    int spins = 0;
                    while (
                        __atomic_load_n(
                            &worker_acquired, __ATOMIC_ACQUIRE
                        ) == 0
                        && spins < 2000000
                    ) {
                        sched_yield();
                        spins++;
                    }
                    if (__atomic_load_n(
                            &worker_acquired, __ATOMIC_ACQUIRE
                        ) != 0) {
                        __atomic_store_n(
                            &callback_lock_free, 1, __ATOMIC_RELEASE
                        );
                    }
                }
                Py_VISIT(obj->child);
                return 0;
            }

            static PyTypeObject ProbeType = {
                .tp_name = "pcc_probe.RemapTraverse",
                .tp_basicsize = sizeof(ProbeCextObject),
                .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
                .tp_traverse = probe_traverse,
            };

            int main(void) {
                static const int32_t frame_map[4] = {3};
                PyObject *roots[3] = {0};
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 2;
                if (PyType_Ready(&ProbeType) != 0) return 3;
                roots[0] = (PyObject *)PyType_GenericAlloc(&ProbeType, 0);
                roots[1] = py_list_new(0);
                roots[2] = py_list_new(0);
                anchor = py_list_new(0);
                if (
                    roots[0] == 0 || roots[1] == 0
                    || roots[2] == 0 || anchor == 0
                ) return 4;
                ProbeCextObject *obj = (ProbeCextObject *)roots[0];
                pcc_gc_store_ptr((PyObject *)obj, &obj->child, roots[1]);
                pcc_gc_frame_enter(frame_map, roots);
                if (pcc_gc_install_forwarding(roots[1], roots[2]) != 0) {
                    return 5;
                }

                probe_pthread_t contender;
                if (pthread_create(
                        &contender, 0, raw_lock_contender, 0
                    ) != 0) return 6;
                int64_t remap =
                    pcc_gc_backend4_remap_and_retire_stopped_world();
                if (__atomic_load_n(&worker_go, __ATOMIC_ACQUIRE) == 0) {
                    __atomic_store_n(&worker_go, 1, __ATOMIC_RELEASE);
                }
                void *thread_result = 0;
                if (pthread_join(contender, &thread_result) != 0) return 7;
                if (thread_result != 0) return 8;
                if (remap <= 0) return 9;
                if (__atomic_load_n(
                        &callback_seen, __ATOMIC_ACQUIRE
                    ) != 1) return 10;
                if (__atomic_load_n(
                        &callback_stw, __ATOMIC_ACQUIRE
                    ) != 1) return 11;
                if (__atomic_load_n(
                        &callback_no_park, __ATOMIC_ACQUIRE
                    ) != 0) return 17;
                if (__atomic_load_n(
                        &callback_lock_free, __ATOMIC_ACQUIRE
                    ) != 1) return 12;
                if (__atomic_load_n(
                        &callback_reset_result, __ATOMIC_ACQUIRE
                    ) != -1) return 13;
                if (__atomic_load_n(
                        &callback_nested_remap, __ATOMIC_ACQUIRE
                    ) != 0) return 14;
                if (obj->child != roots[2]) return 15;
                if (pcc_gc_backend4_remap_and_retire_stopped_world() <= 0) {
                    return 16;
                }
                return 0;
            }
        '''),
    )
    try:
        run = subprocess.run(
            [str(executable)], capture_output=True, text=True, timeout=30
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"{kind} Backend-4 remap C-extension probe timed out: "
            f"stdout={exc.stdout!r} stderr={exc.stderr!r}"
        )
    assert run.returncode == 0, (
        f"{kind} Backend-4 remap C-extension probe returned "
        f"{run.returncode}: " + run.stdout + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_generational_registered_root_promotion_resumes_in_bounded_batches(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="generational_registered_root_batches",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>

            extern void pcc_gc_generational_promote_frame_roots(
                int64_t budget
            );
            extern void pcc_gc_generational_promote_scheduler_roots(
                int64_t budget
            );

            static int old_count(PyObject **slots, int count) {
                int old = 0;
                for (int i = 0; i < count; i++) {
                    if ((py_header(slots[i])->flags & PY_FLAG_GC_OLD) != 0) {
                        old++;
                    }
                }
                return old;
            }

            int main(void) {
                if (pcc_threads_enabled() != 1) return 2;
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
                    ) != 0) return 3;

                int32_t frame_map[41] = {40};
                PyObject *frame_slots[40];
                for (int i = 0; i < 40; i++) {
                    frame_slots[i] = py_list_new(0);
                    if (frame_slots[i] == 0) return 10 + i;
                }
                pcc_gc_note_frame_enter(frame_map, frame_slots);
                pcc_gc_generational_promote_frame_roots(16);
                int frame_old = old_count(frame_slots, 40);
                if (frame_old != 16) {
                    fprintf(
                        stderr,
                        "frame batch1 old=%d flags0=%d flags39=%d\n",
                        frame_old,
                        py_header(frame_slots[0])->flags,
                        py_header(frame_slots[39])->flags
                    );
                    return 60;
                }

                int32_t inserted_map[2] = {1};
                PyObject *inserted_slot = py_list_new(0);
                if (inserted_slot == 0) return 61;
                pcc_gc_note_frame_enter(inserted_map, &inserted_slot);
                pcc_gc_generational_promote_frame_roots(16);
                frame_old = old_count(frame_slots, 40);
                if (
                    frame_old != 32
                    || old_count(&inserted_slot, 1) != 0
                ) {
                    fprintf(
                        stderr,
                        "frame batch2 old=%d inserted_old=%d\n",
                        frame_old,
                        old_count(&inserted_slot, 1)
                    );
                    return 62;
                }
                pcc_gc_generational_promote_frame_roots(16);
                frame_old = old_count(frame_slots, 40);
                if (frame_old != 40) {
                    fprintf(stderr, "frame batch3 old=%d\n", frame_old);
                    return 63;
                }
                if (old_count(&inserted_slot, 1) != 0) return 64;
                pcc_gc_generational_promote_frame_roots(1);
                if (old_count(&inserted_slot, 1) != 1) return 65;
                pcc_gc_note_frame_leave(&inserted_slot);
                py_decref(inserted_slot);
                pcc_gc_note_frame_leave(frame_slots);
                for (int i = 0; i < 40; i++) py_decref(frame_slots[i]);

                int32_t replacement_map[2] = {1};
                PyObject *replacement_slot = py_list_new(0);
                if (replacement_slot == 0) return 66;
                pcc_gc_note_frame_enter(replacement_map, &replacement_slot);
                pcc_gc_generational_promote_frame_roots(1);
                if (old_count(&replacement_slot, 1) != 0) return 67;
                pcc_gc_generational_promote_frame_roots(1);
                if (old_count(&replacement_slot, 1) != 1) return 68;
                pcc_gc_note_frame_leave(&replacement_slot);
                py_decref(replacement_slot);

                PyObject *scheduler_slots[20];
                void *scheduler_handles[20];
                for (int i = 0; i < 20; i++) {
                    scheduler_slots[i] = py_list_new(0);
                    if (scheduler_slots[i] == 0) return 70 + i;
                    scheduler_handles[i] =
                        pcc_gc_scheduler_root_register_handle(
                            &scheduler_slots[i]
                        );
                    if (scheduler_handles[i] == 0) return 100 + i;
                }
                pcc_gc_generational_promote_scheduler_roots(16);
                if (
                    old_count(scheduler_slots, 20) != 16
                ) return 120;

                /* The retained cursor points at handle[3]. Removing it must
                 * invalidate the cursor before that node is freed. */
                pcc_gc_scheduler_root_unregister_handle(
                    scheduler_handles[3]
                );
                scheduler_handles[3] = 0;
                pcc_gc_generational_promote_scheduler_roots(16);
                if (
                    old_count(scheduler_slots, 20) != 19
                ) return 121;
                pcc_gc_generational_promote_scheduler_roots(16);
                if (
                    old_count(scheduler_slots, 20) != 19
                ) return 122;

                for (int i = 0; i < 20; i++) {
                    if (scheduler_handles[i] != 0) {
                        pcc_gc_scheduler_root_unregister_handle(
                            scheduler_handles[i]
                        );
                    }
                    py_decref(scheduler_slots[i]);
                }
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=20
    )
    assert run.returncode == 0, (
        f"{kind} bounded registered-root probe returned {run.returncode}: "
        + run.stdout
        + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_colored_remembered_root_finalizer_runs_after_graph_unlock(
    tmp_path: Path,
    kind: str,
) -> None:
    """A remembered-value finalizer may re-enter the real graph lock."""
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="remembered_root_deferred_finalizer_gc4",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdlib.h>

            typedef struct {
                PyObjectHeader h;
                int64_t length;
                int64_t capacity;
                PyObject **items;
            } ProbeListObject;

            static PccThreadHandle *contender;
            static PyObject *anchor;
            static int64_t worker_ready;
            static int64_t worker_go;
            static int64_t worker_acquired;
            static int64_t finalizer_calls;
            static int64_t finalizer_joined;

            static void *lock_contender(void *arg) {
                (void)arg;
                __atomic_store_n(&worker_ready, 1, __ATOMIC_RELEASE);
                while (__atomic_load_n(&worker_go, __ATOMIC_ACQUIRE) == 0) {
                }
                if (pcc_gc_object_is_known(anchor) != 1) {
                    return (void *)(uintptr_t)2;
                }
                __atomic_store_n(&worker_acquired, 1, __ATOMIC_RELEASE);
                return 0;
            }

            static void joining_finalizer(PyObject *self) {
                (void)self;
                __atomic_add_fetch(&finalizer_calls, 1, __ATOMIC_ACQ_REL);
                __atomic_store_n(&worker_go, 1, __ATOMIC_RELEASE);
                void *result = 0;
                if (
                    pcc_thread_join(contender, &result) == 0
                    && result == 0
                    && __atomic_load_n(
                        &worker_acquired, __ATOMIC_ACQUIRE
                    ) == 1
                ) {
                    __atomic_store_n(
                        &finalizer_joined, 1, __ATOMIC_RELEASE
                    );
                }
            }

            int main(void) {
                if (
                    pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0
                ) return 2;

                anchor = py_list_new(0);
                if (anchor == 0) return 3;
                ProbeListObject *owner = (ProbeListObject *)pcc_gc_alloc(
                    sizeof(ProbeListObject),
                    PY_TYPE_LIST,
                    PY_FLAG_GC_OLD
                );
                if (owner == 0) return 4;
                owner->length = 1;
                owner->capacity = 1;
                owner->items = (PyObject **)calloc(1, sizeof(PyObject *));
                if (owner->items == 0) return 5;

                PyClassObject *cls = py_class_new(
                    "RememberedRootFinalizer", 0, 0, 0, 0
                );
                if (cls == 0) return 6;
                py_class_add_method(
                    cls,
                    "__del__",
                    (PyObject *)(uintptr_t)joining_finalizer
                );
                PyObject *terminal = py_instance_new(cls);
                if (terminal == 0) return 7;

                /* The buffer retain becomes terminal at drain time; the raw
                 * slot deliberately stays NULL so it owns no second ref. */
                pcc_gc_note_slot_write_barrier(
                    (PyObject *)owner,
                    &owner->items[0],
                    terminal
                );
                if (pcc_gc_backend4_store_buffer_entries() != 1) return 8;
                py_decref(terminal);

                if (
                    pcc_thread_start(&contender, lock_contender, 0) != 0
                ) return 9;
                while (
                    __atomic_load_n(&worker_ready, __ATOMIC_ACQUIRE) == 0
                ) {
                }

                /* Old code performs the buffer's last decref while holding
                 * the graph lock.  The finalizer joins a real pthread whose
                 * next operation acquires that same lock. */
                if (pcc_gc_step(1) != 1) return 10;

                if (pcc_gc_backend4_store_buffer_entries() != 0) return 11;
                if (
                    __atomic_load_n(&finalizer_calls, __ATOMIC_ACQUIRE) != 1
                ) return 12;
                if (
                    __atomic_load_n(&worker_acquired, __ATOMIC_ACQUIRE) != 1
                ) return 13;
                if (
                    __atomic_load_n(&finalizer_joined, __ATOMIC_ACQUIRE) != 1
                ) return 14;

                py_decref((PyObject *)cls);
                py_decref((PyObject *)owner);
                py_decref(anchor);
                return 0;
            }
        ''',
    )
    run_env = {
        **os.environ,
        "PCC_LOG": "gc,refcount,finalizer",
        "PCC_LOG_FORMAT": "text",
        "PCC_LOG_FILE": "/dev/null",
    }
    run_env.pop("PCC_REFCOUNT_STRATEGY", None)
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=10,
        env=run_env,
    )
    assert run.returncode == 0, (
        f"{kind} GC4 remembered-root finalizer probe returned "
        f"{run.returncode}: "
        + run.stdout
        + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_colored_remembered_root_drain_counts_maintenance_work(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="remembered_root_maintenance_work_gc4",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>
            #include <stdlib.h>

            typedef struct {
                PyObjectHeader h;
                int64_t length;
                int64_t capacity;
                PyObject **items;
            } ProbeListObject;

            int main(void) {
                if (
                    pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0
                ) return 2;
                pcc_gc_telemetry_reset();
                ProbeListObject *owner = (ProbeListObject *)pcc_gc_alloc(
                    sizeof(ProbeListObject), PY_TYPE_LIST, PY_FLAG_GC_OLD
                );
                if (owner == 0) return 3;
                owner->length = 10;
                owner->capacity = 10;
                owner->items = (PyObject **)calloc(10, sizeof(PyObject *));
                PyObject **children = (PyObject **)calloc(
                    10, sizeof(PyObject *)
                );
                if (owner->items == 0 || children == 0) return 4;

                for (int i = 0; i < 10; i++) {
                    children[i] = py_list_new(0);
                    if (children[i] == 0) return 5;
                    pcc_gc_note_slot_write_barrier(
                        (PyObject *)owner,
                        &owner->items[i],
                        children[i]
                    );
                }
                if (pcc_gc_backend4_store_buffer_entries() != 10) return 6;
                if (pcc_gc_backend4_store_buffer_medium_pending() != 10) {
                    return 7;
                }

                int64_t first_work = pcc_gc_step(10);
                if (first_work != 8) {
                    fprintf(
                        stderr,
                        "first=%lld entries=%lld batches=%lld drained=%lld\n",
                        (long long)first_work,
                        (long long)pcc_gc_backend4_store_buffer_entries(),
                        (long long)pcc_gc_backend4_store_buffer_drain_batches(),
                        (long long)pcc_gc_backend4_store_buffer_drained_entries()
                    );
                    return 8;
                }
                if (pcc_gc_backend4_store_buffer_entries() != 2) return 9;
                if (pcc_gc_backend4_store_buffer_medium_pending() != 0) {
                    return 10;
                }
                if (pcc_gc_backend4_store_buffer_drain_batches() != 1) {
                    return 11;
                }
                if (pcc_gc_backend4_store_buffer_drained_entries() != 8) {
                    return 12;
                }
                if (pcc_gc_backend4_store_buffer_full_batches() != 1) {
                    return 13;
                }
                if (pcc_gc_backend4_store_buffer_incomplete_drains() != 1) {
                    return 14;
                }

                /* The two oldest snapshots remain.  Clearing REMEMBERED makes
                 * them maintenance-only; they still consume public work. */
                py_header((PyObject *)owner)->flags &= ~PY_FLAG_GC_REMEMBERED;
                if (pcc_gc_step(2) != 2) return 15;
                if (pcc_gc_backend4_store_buffer_entries() != 0) return 16;
                if (pcc_gc_backend4_store_buffer_drain_batches() != 2) {
                    return 17;
                }
                if (pcc_gc_backend4_store_buffer_drained_entries() != 10) {
                    return 18;
                }
                for (int i = 0; i < 10; i++) {
                    int32_t flags = py_header(children[i])->flags;
                    if (i < 2) {
                        if ((flags & PY_FLAG_GC_YOUNG) == 0) return 19;
                    } else {
                        if (
                            (flags & PY_FLAG_GC_YOUNG) != 0
                            || (flags & PY_FLAG_GC_OLD) == 0
                        ) return 20;
                    }
                    py_decref(children[i]);
                }
                free(children);
                py_decref((PyObject *)owner);
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=10
    )
    assert run.returncode == 0, (
        f"{kind} GC4 remembered-root maintenance probe returned "
        f"{run.returncode}: "
        + run.stdout
        + run.stderr
    )


def test_colored_remembered_root_drain_defers_blocking_tail_and_medium_flush():

    strict_managed = PY_GC_BACKEND_PORT.read_text(encoding="utf-8")
    assert '@c_abi_export("pcc_gc_backend4_step_remembered_roots")' not in (
        strict_managed
    )
    strict_scheduler = PY_GC_GENERATIONAL_SCHEDULER.read_text(encoding="utf-8")
    assert "__pcc_freestanding__ = True" in strict_scheduler
    strict_body = strict_scheduler.split(
        '@c_abi_export("pcc_gc_backend4_step_remembered_roots")', 1
    )[1].split("\n@c_abi_export", 1)[0]
    strict_locked = strict_body.split("pcc_py_gc_minor_graph_lock()", 1)[1].split(
        "pcc_py_gc_minor_graph_unlock()", 1
    )[0]
    assert 'global_addr("pcc_gc_backend4_store_buffer_medium_head")' in strict_locked
    assert 'global_addr("pcc_gc_backend4_store_buffer_head")' in strict_locked
    for forbidden in (
        "pcc_thread_safepoint(",
        "malloc(",
        "free(",
        "py_decref(",
    ):
        assert forbidden not in strict_locked
    strict_tail = strict_body.split("pcc_py_gc_minor_graph_unlock()", 1)[1]
    assert strict_tail.index("free(") < strict_tail.index("py_decref(")
    assert strict_tail.index("py_decref(") < strict_tail.index(
        "pcc_gc_backend3_drain_promotion_worklist("
    ) < strict_tail.index("pcc_thread_safepoint()")
    assert "return local_drained + promotion_examined" in strict_tail
    strict_oldify_src = (
        RUNTIME_DIR / "py" / "freestanding_gc_generational_oldification.py"
    ).read_text(encoding="utf-8")
    strict_oldify = strict_oldify_src.split(
        '@c_abi_export("pcc_gc_generational_oldify_copy")', 1
    )[1].split("\n@c_abi_export", 1)[0]
    assert strict_oldify.index(
        'load_i32(global_addr("pcc_gc_backend_selected"), 0) != 3'
    ) < strict_oldify.index("malloc(")
    strict_dispatcher = PY_GC_BARRIER_DISPATCHER.read_text(encoding="utf-8")
    strict_step = strict_dispatcher.split('@c_abi_export("pcc_gc_step")', 1)[1]
    assert (
        strict_step.index("pcc_gc_backend4_step_remembered_roots(")
        < strict_step.index(
            'global_addr("pcc_gc_backend4_store_buffer_entries_count")'
        )
        < strict_step.index("pcc_gc_backend4_step_generation_aging(")
    )


