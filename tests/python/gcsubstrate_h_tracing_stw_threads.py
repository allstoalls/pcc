"""Tracing finish claims under real pthreads, stop-the-world stress, no-libpython threading binary.

Split from ``test_gc_threading_substrate.py``; collected only through
that facade so pytest node ids stay stable.
"""
from _gc_substrate_common import *  # noqa: F401,F403


def test_tracing_finish_claim_lifts_stw_outside_graph_lock_in_c_and_strict_runtime():
    assert {
        "pcc_gc_tracing_cycle_epoch",
        "pcc_gc_tracing_finish_claim_epoch",
        "pcc_gc_tracing_finish_claim_backend",
        "pcc_gc_tracing_finish_commits",
    } <= FREESTANDING_GC_I64_GLOBALS
    assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES[
        "pcc_gc_finish_tracing_cycle"
    ] == (("c_int64", "c_int64"), "c_int64")
    assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES[
        "pcc_gc_complete_claimed_tracing_cycle"
    ] == (("c_int64", "c_int64"), "c_int64")
    assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES[
        "pcc_gc_tracing_cycle_epoch_advance_unlocked"
    ] == ((), "c_int64")
    assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES[
        "pcc_gc_tracing_finish_claim_clear_unlocked"
    ] == (("c_int64", "c_int64"), "c_void")

    strict_state = PY_GC_STATE.read_text(encoding="utf-8")
    for name in (
        "pcc_gc_tracing_cycle_epoch",
        "pcc_gc_tracing_finish_claim_epoch",
        "pcc_gc_tracing_finish_claim_backend",
        "pcc_gc_tracing_finish_commits",
    ):
        assert f'define_global_i64("{name}"' in strict_state

    strict_finish_src = PY_GC_COMMON_MARK_CYCLE.read_text(encoding="utf-8")
    strict_claim_clear = strict_finish_src.split(
        '@c_abi_export("pcc_gc_tracing_finish_claim_clear_unlocked")', 1
    )[1].split('@c_abi_export("pcc_gc_finish_tracing_cycle")', 1)[0]
    strict_clear_epoch_guard = strict_claim_clear.index("!= claim_epoch")
    strict_clear_backend_global = strict_claim_clear.index(
        'global_addr("pcc_gc_tracing_finish_claim_backend")'
    )
    strict_clear_backend_guard = strict_claim_clear.index(
        "!= claim_backend", strict_clear_backend_global
    )
    strict_clear_epoch_store = strict_claim_clear.index(
        'store_i64(global_addr("pcc_gc_tracing_finish_claim_epoch"), 0, 0)'
    )
    strict_clear_backend_store = strict_claim_clear.index(
        'store_i64(global_addr("pcc_gc_tracing_finish_claim_backend"), 0, -1)'
    )
    strict_clear_early_return = strict_claim_clear.index("return")
    assert max(
        strict_clear_epoch_guard,
        strict_clear_backend_guard,
        strict_clear_early_return,
    ) < min(strict_clear_epoch_store, strict_clear_backend_store)
    strict_finish = strict_finish_src.split(
        '@c_abi_export("pcc_gc_finish_tracing_cycle")', 1
    )[1]
    strict_finish_signature = strict_finish.split(") -> i64:", 1)[0]
    assert "claim_epoch: i64" in strict_finish_signature
    assert "claim_backend: i64" in strict_finish_signature
    assert strict_finish.index(
        'global_addr("pcc_gc_tracing_finish_claim_epoch")'
    ) < strict_finish.index("!= claim_epoch")
    claim_backend_global = strict_finish.index(
        'global_addr("pcc_gc_tracing_finish_claim_backend")'
    )
    assert claim_backend_global < strict_finish.index(
        "!= claim_backend", claim_backend_global
    )
    cycle_global = strict_finish.index(
        'global_addr("pcc_gc_tracing_cycle_epoch")'
    )
    assert cycle_global < strict_finish.index("!= claim_epoch", cycle_global)
    selected_global = strict_finish.index(
        'global_addr("pcc_gc_backend_selected")'
    )
    assert selected_global < strict_finish.index(
        "!= claim_backend", selected_global
    )
    active_global = strict_finish.index('global_addr("pcc_gc_mark_active")')
    assert active_global < strict_finish.index("== 0", active_global)
    for forbidden in (
        "pcc_stop_the_world",
        "pcc_resume_world",
        "pcc_py_gc_minor_graph_lock",
        "pcc_py_gc_minor_graph_unlock",
    ):
        assert forbidden not in strict_finish
    assert "pcc_gc_gray_current_roots()" not in strict_finish
    assert "pcc_gc_drain_all_gray" not in strict_finish
    assert "pcc_gc_cycle_requested" not in strict_finish.split(
        "pcc_gc_tracing_finish_claim_epoch", 1
    )[-1]

    strict_scheduler = PY_GC_INCREMENTAL_CONCURRENT_SCHEDULER.read_text(
        encoding="utf-8"
    )
    strict_step = strict_scheduler.split(
        '@c_abi_export("pcc_gc_tracing_step_cycle")', 1
    )[1].split("\n@c_abi_export", 1)[0]
    assert strict_step.index("pcc_py_gc_minor_graph_unlock()") < (
        strict_step.index(
            "pcc_gc_complete_claimed_tracing_cycle(claim_epoch, claim_backend)"
        )
    )
    strict_complete = strict_scheduler.split(
        "def pcc_gc_complete_claimed_tracing_cycle(", 1
    )[1].split("\n@c_abi_export", 1)[0]
    strict_complete_signature = strict_complete.split(") -> i64:", 1)[0]
    assert "claim_epoch: i64" in strict_complete_signature
    assert "claim_backend: i64" in strict_complete_signature
    assert strict_complete.count("pcc_stop_the_world()") == 1
    assert strict_complete.index("pcc_thread_owns_stopped_world()") < (
        strict_complete.index("if owns_stopped_world == 0:")
    ) < strict_complete.index("pcc_stop_the_world()")
    strict_stop_failure = strict_complete.split(
        "if pcc_stop_the_world() != 0:", 1
    )[1].split("acquired_stopped_world = 1", 1)[0]
    assert strict_stop_failure.index("pcc_py_gc_minor_graph_lock()") < (
        strict_stop_failure.index(
            "pcc_gc_tracing_finish_claim_clear_unlocked("
        )
    ) < strict_stop_failure.index("pcc_py_gc_minor_graph_unlock()") < (
        strict_stop_failure.index("return 0")
    )
    strict_failure_clear = strict_stop_failure.index(
        "pcc_gc_tracing_finish_claim_clear_unlocked("
    )
    assert "claim_epoch, claim_backend" in strict_stop_failure[
        strict_failure_clear : strict_failure_clear + 160
    ]
    assert "pcc_gc_finish_tracing_cycle" not in strict_stop_failure
    assert "pcc_resume_world" not in strict_stop_failure
    strict_finish_call = strict_complete.index("pcc_gc_finish_tracing_cycle(")
    assert "claim_epoch, claim_backend" in strict_complete[
        strict_finish_call : strict_finish_call + 130
    ]
    assert strict_finish_call < strict_complete.index(
        "pcc_py_gc_minor_graph_unlock()", strict_finish_call
    ) < strict_complete.index("pcc_resume_world()")
    strict_owns_branch = strict_complete.split(
        "if owns_stopped_world == 0:", 1
    )[1].split("\n\n    pcc_py_gc_minor_graph_lock()", 1)[0]
    assert strict_owns_branch.count("pcc_stop_the_world()") == 1
    assert "acquired_stopped_world = 1" in strict_owns_branch
    strict_resume_branch = strict_complete.split(
        "if acquired_stopped_world != 0:", 1
    )[1].split("return committed", 1)[0]
    assert strict_resume_branch.count("pcc_resume_world()") == 1

    strict_backend = PY_GC_BACKEND_PORT.read_text(encoding="utf-8")
    setter = strict_backend.split('@c_abi_export("pcc_gc_set_backend")', 1)[1].split(
        "\n@c_abi_export", 1
    )[0]
    reset_epoch = setter.index("_tracing_cycle_epoch_advance_unlocked()")
    reset_active = setter.index('global_addr("pcc_gc_mark_active")')
    reset_cursor = setter.index('global_store_ptr("pcc_gc_trace_cursor"')
    reset_gray = setter.index("_set_gray_count(0)")
    unlock = setter.rindex("_object_graph_unlock()")
    assert reset_epoch < unlock
    assert reset_active < unlock
    assert reset_cursor < unlock
    assert reset_gray < unlock
    assert "tracing_finish_claim_clear" not in setter

    strict_epoch_advance = strict_finish_src.split(
        'def pcc_gc_tracing_cycle_epoch_advance_unlocked()', 1
    )[1].split('\n@c_abi_export("pcc_gc_tracing_finish_claim_clear_unlocked")', 1)[0]
    strict_max_guard = strict_epoch_advance.index(
        "current == 9223372036854775807"
    )
    assert strict_max_guard < strict_epoch_advance.index(
        "pcc_platform_abort()", strict_max_guard
    ) < (
        strict_epoch_advance.index("current + 1")
    )
    assert "next_epoch = 1" not in strict_epoch_advance


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_tracing_finish_claim_real_pthread_windows_and_single_finisher(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="tracing_finish_claim",
        source_text=r'''
            #include "py_internal.h"
            #include <sched.h>
            #include <stdint.h>

            extern int64_t pcc_gc_tracing_cycle_epoch;
                extern int64_t pcc_gc_tracing_finish_claim_epoch;
                extern int64_t pcc_gc_tracing_finish_claim_backend;
                extern int64_t pcc_gc_tracing_finish_commits;

            static PyObject *late_owner;
            static PyObject *late_child;
            static PyObject *late_root;
            static int64_t late_ready;
            static int64_t late_registered;
            static int64_t late_preconditions;
            static int64_t late_barrier_observed;
            static int64_t late_barriers_before;
            static int64_t late_barrier_delta;
            static int64_t late_resumed;
            static int64_t late_release;

                static void spin_until(int64_t *slot, int64_t value) {
                    while (__atomic_load_n(slot, __ATOMIC_ACQUIRE) != value) {
                        sched_yield();
                    }
                }

                static void *late_root_worker(void *arg) {
                (void)arg;
                /* Enter before stop publication so strict generated entry
                 * polls cannot park before this worker mutates the window. */
                    pcc_thread_no_park_enter();
                    __atomic_store_n(&late_ready, 1, __ATOMIC_RELEASE);
                    while (pcc_thread_stop_requested_acquire() == 0) {
                        sched_yield();
                    }
                    int32_t owner_flags = py_header(late_owner)->flags;
                    int32_t child_flags = py_header(late_child)->flags;
                    int32_t root_flags = py_header(late_root)->flags;
                    if (
                        (owner_flags & PY_FLAG_GC_BLACK) == 0
                        || (child_flags & PY_FLAG_GC_WHITE) == 0
                        || (root_flags & PY_FLAG_GC_WHITE) == 0
                    ) {
                        __atomic_store_n(
                            &late_preconditions, -1, __ATOMIC_RELEASE
                        );
                    pcc_thread_no_park_exit();
                    return (void *)(intptr_t)2;
                }
                __atomic_store_n(&late_preconditions, 1, __ATOMIC_RELEASE);
                PyListObject *owner = (PyListObject *)late_owner;
                pcc_gc_store_ptr(
                    late_owner, &owner->items[0], late_child
                );
                if (
                    (py_header(late_child)->flags & PY_FLAG_GC_GRAY) != 0
                ) {
                    __atomic_store_n(
                        &late_barrier_observed, 1, __ATOMIC_RELEASE
                    );
                }
                late_barrier_delta = pcc_gc_telemetry(
                    PCC_GC_COUNTER_WRITE_BARRIERS
                ) - late_barriers_before;
                /* Register a separate aged white self-cycle.  Final root
                 * rescan and black->white insertion-barrier observations are
                 * intentionally different objects. */
                void *root_handle = pcc_gc_scheduler_root_register_handle(
                    &late_root
                );
                if (root_handle == 0) {
                    pcc_thread_no_park_exit();
                    return (void *)(intptr_t)3;
                }
                __atomic_store_n(&late_registered, 1, __ATOMIC_RELEASE);
                pcc_thread_no_park_exit();
                __atomic_store_n(&late_resumed, 1, __ATOMIC_RELEASE);
                while (__atomic_load_n(&late_release, __ATOMIC_ACQUIRE) == 0) {
                    pcc_thread_safepoint();
                    sched_yield();
                }
                pcc_gc_scheduler_root_unregister_handle(root_handle);
                return 0;
            }

            static int64_t reset_ready;
            static int64_t reset_done;
            static int64_t reset_resumed;
            static int64_t reset_release;
            static int64_t reset_result;
            static int64_t reset_claim_epoch_before;
            static int64_t reset_claim_backend_before;
            static int64_t reset_claim_epoch_after;
            static int64_t reset_claim_backend_after;

            static void *same_backend_reset_worker(void *arg) {
                (void)arg;
                    pcc_thread_no_park_enter();
                    __atomic_store_n(&reset_ready, 1, __ATOMIC_RELEASE);
                    while (pcc_thread_stop_requested_acquire() == 0) {
                        sched_yield();
                    }
                reset_claim_epoch_before =
                    pcc_gc_tracing_finish_claim_epoch;
                reset_claim_backend_before =
                    pcc_gc_tracing_finish_claim_backend;
                reset_result = pcc_gc_set_backend(
                    PCC_GC_KIND_INCREMENTAL_TRICOLOR
                );
                reset_claim_epoch_after =
                    pcc_gc_tracing_finish_claim_epoch;
                reset_claim_backend_after =
                    pcc_gc_tracing_finish_claim_backend;
                __atomic_store_n(&reset_done, 1, __ATOMIC_RELEASE);
                pcc_thread_no_park_exit();
                __atomic_store_n(&reset_resumed, 1, __ATOMIC_RELEASE);
                while (__atomic_load_n(&reset_release, __ATOMIC_ACQUIRE) == 0) {
                    pcc_thread_safepoint();
                    sched_yield();
                }
                return 0;
            }

            static int64_t finisher_ready;
            static int64_t finisher_first_go;
            static int64_t finisher_second_go;
            static int64_t finisher_second_returned;

            static void *first_finisher(void *arg) {
                (void)arg;
                __atomic_add_fetch(&finisher_ready, 1, __ATOMIC_ACQ_REL);
                spin_until(&finisher_first_go, 1);
                (void)pcc_gc_step(1);
                return 0;
            }

            static void *second_finisher(void *arg) {
                (void)arg;
                pcc_thread_no_park_enter();
                __atomic_add_fetch(&finisher_ready, 1, __ATOMIC_ACQ_REL);
                spin_until(&finisher_second_go, 1);
                (void)pcc_gc_step(1);
                __atomic_store_n(
                    &finisher_second_returned, 1, __ATOMIC_RELEASE
                );
                pcc_thread_no_park_exit();
                return 0;
            }

            static int join_ok(PccThreadHandle *thread) {
                void *result = 0;
                if (pcc_thread_join(thread, &result) != 0) return 0;
                return result == 0;
            }

            int main(void) {
                if (pcc_threads_enabled() != 1) return 2;
                if (pcc_current_thread_id() <= 0) return 3;
                /* Only the explicit steps below advance cycles: with
                 * collection disabled the GC1 collector thread stays out. */
                py_gc_disable();

                /* Window 1: first age owner+child through one rooted epoch so
                 * FRESH_ALLOC cannot make the target falsely black.  In the
                 * next epoch the owner is black and the detached self-cycle
                 * child is white.  A real slot store must gray that child in
                 * claim->STW, while a distinct late white self-cycle proves
                 * final root rescan and publishes the following-cycle request. */
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_INCREMENTAL_TRICOLOR
                    ) != 0) return 4;
                PyObject *owner = py_list_new(1);
                PyObject *child = py_list_new(1);
                PyObject *root_cycle = py_list_new(1);
                if (owner == 0 || child == 0 || root_cycle == 0) return 5;
                py_list_append(child, child);
                py_list_append(root_cycle, root_cycle);
                py_list_append(owner, child);
                py_list_append(owner, root_cycle);
                py_decref(child); /* retain owner edge + internal self-edge */
                py_decref(root_cycle);
                late_owner = owner;
                late_child = child;
                late_root = root_cycle;
                void *owner_root = pcc_gc_scheduler_root_register_handle(
                    &late_owner
                );
                if (owner_root == 0) return 6;
                int64_t warm_commits = pcc_gc_tracing_finish_commits;
                (void)pcc_gc_step(64);
                if (pcc_gc_tracing_finish_commits != warm_commits + 1) {
                    return 42;
                }
                if (
                    (py_header(owner)->flags & PY_FLAG_GC_FRESH_ALLOC) != 0
                    || (py_header(child)->flags & PY_FLAG_GC_FRESH_ALLOC) != 0
                    || (
                        py_header(root_cycle)->flags
                        & PY_FLAG_GC_FRESH_ALLOC
                    ) != 0
                ) return 43;
                pcc_gc_store_ptr(
                    owner,
                    &((PyListObject *)owner)->items[0],
                    py_int_from_i64(0)
                );
                pcc_gc_store_ptr(
                    owner,
                    &((PyListObject *)owner)->items[1],
                    py_int_from_i64(0)
                );
                        if (pcc_gc_set_backend(
                                PCC_GC_KIND_INCREMENTAL_TRICOLOR
                            ) != 0) return 44;
                        PyObject *late_stage = py_list_new(0);
                        if (late_stage == 0) return 57;
                        void *late_stage_root =
                            pcc_gc_scheduler_root_register_handle(&late_stage);
                        if (late_stage_root == 0) return 58;
                        /* Consume seed STW plus the newest rooted tail before
                         * admitting the worker that targets the final-cut STW. */
                        (void)pcc_gc_step(1);

                    PccThreadHandle *late_thread = 0;
                if (pcc_thread_start(
                        &late_thread, late_root_worker, 0
                    ) != 0) return 7;
                spin_until(&late_ready, 1);
                late_barriers_before = pcc_gc_telemetry(
                    PCC_GC_COUNTER_WRITE_BARRIERS
                    );
                    int64_t commits0 = pcc_gc_tracing_finish_commits;
                    int64_t epoch0 = pcc_gc_tracing_cycle_epoch;
                    for (
                        int i = 0;
                        i < 4 && __atomic_load_n(
                            &late_preconditions, __ATOMIC_ACQUIRE
                        ) == 0;
                        i++
                    ) {
                        (void)pcc_gc_step(65536);
                        }
                    if (__atomic_load_n(
                            &late_preconditions, __ATOMIC_ACQUIRE
                        ) != 1) {
                        return 45;
                    }
                if (__atomic_load_n(&late_registered, __ATOMIC_ACQUIRE) != 1) {
                    return 8;
                }
                if (
                    __atomic_load_n(
                        &late_barrier_observed, __ATOMIC_ACQUIRE
                    ) != 1
                    || late_barrier_delta < 1
                ) return 46;
                if (pcc_gc_tracing_finish_commits != commits0 + 1) return 9;
                if (
                    pcc_gc_tracing_finish_claim_epoch != 0
                    || pcc_gc_tracing_finish_claim_backend != -1
                ) return 10;
                int32_t child_after_cut = py_header(child)->flags;
                if (
                    (child_after_cut & PY_FLAG_GC_BLACK) == 0
                    || (child_after_cut & PY_FLAG_GC_GRAY) != 0
                    || (child_after_cut & PY_FLAG_GC_WHITE) != 0
                    || (child_after_cut & PY_FLAG_GC_SWEEP_CANDIDATE) != 0
                ) return 11;
                int32_t root_after_cut = py_header(root_cycle)->flags;
                if (
                    (root_after_cut & PY_FLAG_GC_BLACK) == 0
                    || (root_after_cut & PY_FLAG_GC_GRAY) != 0
                    || (root_after_cut & PY_FLAG_GC_WHITE) != 0
                    || (root_after_cut & PY_FLAG_GC_SWEEP_CANDIDATE) != 0
                ) return 48;
                    if (pcc_gc_tracing_cycle_epoch != epoch0) return 12;
                spin_until(&late_resumed, 1);
                int64_t epoch_after_late = pcc_gc_tracing_cycle_epoch;
                for (
                    int i = 0;
                    i < 4 && pcc_gc_tracing_finish_commits != commits0 + 2;
                    i++
                ) {
                    (void)pcc_gc_step(64);
                }
                if (pcc_gc_tracing_cycle_epoch != epoch_after_late + 1) return 13;
                if (pcc_gc_tracing_finish_commits != commits0 + 2) return 14;
                if ((py_header(child)->flags & PY_FLAG_GC_SWEEP_CANDIDATE) != 0) {
                    return 15;
                }
                if (
                    (py_header(root_cycle)->flags & PY_FLAG_GC_SWEEP_CANDIDATE)
                    != 0
                ) return 49;
                __atomic_store_n(&late_release, 1, __ATOMIC_RELEASE);
                if (!join_ok(late_thread)) return 47;
                pcc_gc_scheduler_root_unregister_handle(owner_root);
                late_owner = 0;
                late_child = 0;
                late_root = 0;
                py_list_clear(owner);
                    py_decref(owner);
                    py_list_clear(child);
                    py_list_clear(root_cycle);
                    pcc_gc_scheduler_root_unregister_handle(late_stage_root);
                    py_decref(late_stage);

                /* Window 2: a same-backend reset linearizes while the old
                 * claimant waits for STW.  The old token must not cut the new
                 * epoch or clear its request; the next step owns that commit. */
                    if (pcc_gc_set_backend(
                            PCC_GC_KIND_INCREMENTAL_TRICOLOR
                        ) != 0) return 16;
                    PyObject *stage_a = py_list_new(0);
                    PyObject *stage_b = py_list_new(0);
                    if (stage_a == 0 || stage_b == 0) return 55;
                    void *stage_a_root = pcc_gc_scheduler_root_register_handle(
                        &stage_a
                    );
                    void *stage_b_root = pcc_gc_scheduler_root_register_handle(
                        &stage_b
                    );
                    if (stage_a_root == 0 || stage_b_root == 0) return 56;
                    (void)pcc_gc_step(1);
                    PccThreadHandle *reset_thread = 0;
                if (pcc_thread_start(
                        &reset_thread, same_backend_reset_worker, 0
                    ) != 0) return 17;
                spin_until(&reset_ready, 1);
                commits0 = pcc_gc_tracing_finish_commits;
                epoch0 = pcc_gc_tracing_cycle_epoch;
                (void)pcc_gc_step(1);
                if (__atomic_load_n(&reset_done, __ATOMIC_ACQUIRE) != 1) return 18;
                if (reset_result != 0) return 19;
                if (
                        reset_claim_epoch_before != epoch0
                    || reset_claim_backend_before
                        != PCC_GC_KIND_INCREMENTAL_TRICOLOR
                    || reset_claim_epoch_after != reset_claim_epoch_before
                    || reset_claim_backend_after != reset_claim_backend_before
                ) return 50;
                if (pcc_gc_tracing_finish_commits != commits0) return 20;
                if (
                    pcc_gc_tracing_finish_claim_epoch != 0
                    || pcc_gc_tracing_finish_claim_backend != -1
                ) return 21;
                    if (pcc_gc_tracing_cycle_epoch != epoch0 + 1) return 22;
                    spin_until(&reset_resumed, 1);
                    int64_t epoch_after_reset = pcc_gc_tracing_cycle_epoch;
                    for (
                        int i = 0;
                        i < 4 && pcc_gc_tracing_finish_commits != commits0 + 1;
                        i++
                    ) {
                        (void)pcc_gc_step(1);
                    }
                if (pcc_gc_tracing_cycle_epoch != epoch_after_reset + 1) return 23;
                if (pcc_gc_tracing_finish_commits != commits0 + 1) return 24;
                __atomic_store_n(&reset_release, 1, __ATOMIC_RELEASE);
                if (!join_ok(reset_thread)) return 25;

                /* Window 3: block the first claimant at its real STW with
                 * main's bounded no-park depth.  Only then admit the second
                 * tracing step.  It must return without stealing the claim;
                 * exactly one final cut commits after main exits no-park. */
                    if (pcc_gc_set_backend(
                            PCC_GC_KIND_INCREMENTAL_TRICOLOR
                        ) != 0) return 26;
                    (void)pcc_gc_step(1);
                PccThreadHandle *first = 0;
                PccThreadHandle *second = 0;
                if (pcc_thread_start(&first, first_finisher, 0) != 0) return 27;
                if (pcc_thread_start(&second, second_finisher, 0) != 0) return 28;
                spin_until(&finisher_ready, 2);
                    commits0 = pcc_gc_tracing_finish_commits;
                    pcc_thread_no_park_enter();
                    __atomic_store_n(&finisher_first_go, 1, __ATOMIC_RELEASE);
                    while (pcc_thread_stop_requested_acquire() == 0) {
                        sched_yield();
                    }
                int64_t contested_epoch =
                    pcc_gc_tracing_finish_claim_epoch;
                int64_t contested_backend =
                    pcc_gc_tracing_finish_claim_backend;
                if (
                    contested_epoch == 0
                    || contested_epoch != pcc_gc_tracing_cycle_epoch
                    || contested_backend
                        != PCC_GC_KIND_INCREMENTAL_TRICOLOR
                    || contested_backend != pcc_gc_backend()
                ) return 51;
                if (pcc_gc_tracing_finish_commits != commits0) return 29;
                __atomic_store_n(&finisher_second_go, 1, __ATOMIC_RELEASE);
                spin_until(&finisher_second_returned, 1);
                if (pcc_gc_tracing_finish_commits != commits0) return 30;
                if (
                    pcc_gc_tracing_finish_claim_epoch != contested_epoch
                    || pcc_gc_tracing_finish_claim_backend
                        != contested_backend
                ) return 52;
                pcc_thread_no_park_exit();
                if (!join_ok(first)) return 31;
                if (!join_ok(second)) return 32;
                if (pcc_gc_tracing_finish_commits != commits0 + 1) return 33;
                if (
                    pcc_gc_tracing_finish_claim_epoch != 0
                    || pcc_gc_tracing_finish_claim_backend != -1
                ) return 34;
                int64_t quiescent_epoch = pcc_gc_tracing_cycle_epoch;
                int64_t quiescent_commits = pcc_gc_tracing_finish_commits;
                (void)pcc_gc_step(1);
                    if (
                        pcc_gc_tracing_cycle_epoch != quiescent_epoch
                    || pcc_gc_tracing_finish_commits != quiescent_commits
                        || pcc_thread_stop_requested_acquire() != 0
                    ) return 53;
                    pcc_gc_scheduler_root_unregister_handle(stage_a_root);
                    pcc_gc_scheduler_root_unregister_handle(stage_b_root);
                    py_decref(stage_a);
                    py_decref(stage_b);

                /* Reusing an already-owned stopped world must not consume the
                 * caller's ownership or leave a hidden nested depth. */
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_INCREMENTAL_TRICOLOR
                    ) != 0) return 35;
                commits0 = pcc_gc_tracing_finish_commits;
                if (pcc_stop_the_world() != 0) return 36;
                if (pcc_thread_owns_stopped_world() != 1) return 37;
                (void)pcc_gc_step(1);
                if (pcc_thread_owns_stopped_world() != 1) return 38;
                if (pcc_gc_tracing_finish_commits != commits0 + 1) return 39;
                if (pcc_resume_world() != 0) return 40;
                if (pcc_resume_world() != -1) return 41;
                if (
                    pcc_thread_stop_requested_acquire() != 0
                    || pcc_gc_tracing_finish_claim_epoch != 0
                    || pcc_gc_tracing_finish_claim_backend != -1
                ) return 54;
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=40
    )
    assert run.returncode == 0, (
        f"{kind} tracing-finish probe returned {run.returncode}: "
        + run.stdout
        + run.stderr
    )


def test_tracing_gc_finalizer_handles_thread_objects_and_refcount_side_table():

    py_src = PY_GC_BACKEND_PORT.read_text(encoding="utf-8")
    extern_import = next(
        line for line in py_src.splitlines() if line.startswith("from pcc.extern import ")
    )
    for name in ["c_abi_export", "c_int64", "c_ptr", "c_void", "extern"]:
        assert name in extern_import
    # The thread-object finalizer path moved to the freestanding tracing-sweep
    # collector as the refcount-cycle collector policy migrated.
    sweep_collector = (
        RUNTIME_DIR / "py" / "freestanding_gc_tracing_sweep_collector.py"
    ).read_text(encoding="utf-8")
    assert "pcc_refcount_forget(obj)" in sweep_collector
    # The thread tag moved from the magic literal 27 to a named ABI constant.
    assert 'tag == abi_constant("object.type.thread")' in sweep_collector
    assert "py_dealloc_thread_thread(obj)" in sweep_collector

    thread_port = (REPO_ROOT / "pcc" / "runtime" / "py" / "py_threading.py").read_text(
        encoding="utf-8"
    )
    assert '@c_abi_export("py_dealloc_thread_thread")' in thread_port
    # callable/args default to None before the barriered publication.
    assert "pcc_gc_store_ptr(o, ptr_add(o, 24), callable_value)" in thread_port
    assert "pcc_gc_store_ptr(o, ptr_add(o, 32), args_value)" in thread_port
    assert "py_decref_extern(pcc_gc_load_ptr(thread, ptr_add(thread, 24)))" in thread_port


def test_no_libpython_all_backends_collect_through_thread_gate(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent("""
        from pcc.extern import extern, c_int32, c_int64, c_void

        pcc_gc_set_backend = extern("pcc_gc_set_backend", (c_int64,), c_int64)
        pcc_gc_collect = extern("pcc_gc_collect", (c_int32,), c_int64)
        pcc_gc_safepoint = extern("pcc_gc_safepoint", (), c_void)
        pcc_stop_the_world = extern("pcc_stop_the_world", (), c_int64)
        pcc_resume_world = extern("pcc_resume_world", (), c_int64)

        def main() -> None:
            b = 0
            while b < 5:
                print(pcc_gc_set_backend(b))
                pcc_gc_safepoint()
                print(pcc_gc_collect(0) >= 0)
                print(pcc_stop_the_world())
                print(pcc_resume_world())
                b = b + 1

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    expected: list[str] = []
    for _backend in range(5):
        expected.extend(["0", "True", "0", "0"])
    assert result.stdout.strip().splitlines() == expected


def test_pthread_substrate_stop_the_world_stress(tmp_path):
    """Exercise the real pthread path without exposing Python-level threads.

    The worker repeatedly enters safepoints while the main thread performs
    many STW/resume cycles. This catches the stale-parked-thread race where
    a second STW can start before a just-resumed worker clears its TLS parked
    flag.
    """
    work_runtime = _build_threaded_runtime(tmp_path)
    cc = os.environ.get("CC", "cc")
    src = tmp_path / "thread_smoke.c"
    exe = tmp_path / "thread_smoke.out"
    src.write_text(textwrap.dedent(r"""
        #include "py_internal.h"
        #include <stdint.h>
        #include <stdio.h>

        static int64_t worker_done = 0;
        static int64_t worker_seen = 0;

        static void *worker_main(void *arg) {
            (void)arg;
            (void)pcc_current_thread_id();
            while (__atomic_load_n(&worker_done, __ATOMIC_RELAXED) == 0) {
                pcc_thread_safepoint();
                __atomic_add_fetch(&worker_seen, 1, __ATOMIC_RELAXED);
            }
            return 0;
        }

        int main(void) {
            if (pcc_threads_enabled() != 1) return 2;
            (void)pcc_current_thread_id();

            PccThreadHandle *thread = 0;
            if (pcc_thread_start(&thread, worker_main, 0) != 0) return 3;

            while (__atomic_load_n(&worker_seen, __ATOMIC_RELAXED) < 1) {
                pcc_thread_safepoint();
            }

            for (int i = 0; i < 64; i++) {
                if (pcc_stop_the_world() != 0) return 10;
                if (pcc_stop_the_world() != 0) return 11;
                pcc_thread_safepoint();
                if (pcc_resume_world() != 0) return 12;
                if (pcc_resume_world() != 0) return 13;
            }

            __atomic_store_n(&worker_done, 1, __ATOMIC_RELAXED);
            void *result = 0;
            if (pcc_thread_join(thread, &result) != 0) return 14;
            if (pcc_resume_world() != -1) return 15;

            PccMutex *mutex = pcc_mutex_new();
            PccCond *cond = pcc_cond_new();
            if (mutex == 0 || cond == 0) return 16;
            if (pcc_mutex_lock(mutex) != 0) return 17;
            if (pcc_cond_signal(cond) != 0) return 18;
            if (pcc_cond_broadcast(cond) != 0) return 19;
            if (pcc_mutex_unlock(mutex) != 0) return 20;
            pcc_cond_free(cond);
            pcc_mutex_free(mutex);

            printf("ok\n");
            return 0;
        }
        """).lstrip(), encoding="utf-8")

    cmd = [
        cc,
        "-DPCC_WITH_THREADS=1",
        "-std=c11",
        "-pthread",
        f"-I{work_runtime / 'include'}",
        f"-I{work_runtime / 'src'}",
        str(src),
        str(work_runtime / "libpy_runtime_pcc_py.a"),
        "-lm",
        "-o",
        str(exe),
    ]
    build = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert build.returncode == 0, build.stderr
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_concurrent_stop_the_world_requesters_are_serialized(tmp_path):
    """A second STW requester parks for the owner, then gets its own turn."""
    work_runtime = _build_threaded_runtime(tmp_path)
    cc = os.environ.get("CC", "cc")
    src = tmp_path / "concurrent_stw.c"
    exe = tmp_path / "concurrent_stw.out"
    src.write_text(textwrap.dedent(r"""
        #include "py_internal.h"
        #include <stdint.h>
        #include <stdio.h>

        static int64_t worker_started = 0;
        static int64_t worker_owned_stop = 0;

        static void *worker_main(void *arg) {
            (void)arg;
            __atomic_store_n(&worker_started, 1, __ATOMIC_RELEASE);
            if (pcc_stop_the_world() != 0) return (void *)(intptr_t)2;
            __atomic_store_n(&worker_owned_stop, 1, __ATOMIC_RELEASE);
            if (pcc_resume_world() != 0) return (void *)(intptr_t)3;
            return 0;
        }

        int main(void) {
            if (pcc_threads_enabled() != 1) return 2;
            (void)pcc_current_thread_id();

            PccThreadHandle *thread = 0;
            if (pcc_thread_start(&thread, worker_main, 0) != 0) return 3;
            while (__atomic_load_n(&worker_started, __ATOMIC_ACQUIRE) == 0) {
            }
            while (pcc_thread_stop_requested_acquire() == 0) {
            }
            if (pcc_resume_world() != -1) return 4;

            /* The worker owns the first stop and is waiting for this live
             * thread to park. This call must serialize behind it, not fail. */
            if (pcc_stop_the_world() != 0) return 5;
            if (__atomic_load_n(&worker_owned_stop, __ATOMIC_ACQUIRE) != 1) {
                return 6;
            }
            if (pcc_resume_world() != 0) return 7;

            void *result = 0;
            if (pcc_thread_join(thread, &result) != 0) return 8;
            if (result != 0) return 9;
            printf("serialized-stw-ok\n");
            return 0;
        }
        """).lstrip(), encoding="utf-8")

    build = subprocess.run(
        [
            cc,
            "-DPCC_WITH_THREADS=1",
            "-std=c11",
            "-pthread",
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
        timeout=30,
    )
    assert build.returncode == 0, build.stderr
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "serialized-stw-ok"


def test_threaded_allocator_boundary_is_safepoint_for_stw(tmp_path):
    work_runtime = _build_threaded_runtime(tmp_path)
    cc = os.environ.get("CC", "cc")
    src = tmp_path / "alloc_safepoint.c"
    exe = tmp_path / "alloc_safepoint.out"
    src.write_text(textwrap.dedent(r"""
        #include "py_internal.h"
        #include <stdint.h>
        #include <stdio.h>

        static int64_t worker_done = 0;
        static int64_t worker_started = 0;
        static int64_t worker_iterations = 0;

        static void *worker_main(void *arg) {
            (void)arg;
            (void)pcc_current_thread_id();
            __atomic_store_n(&worker_started, 1, __ATOMIC_RELEASE);
            while (__atomic_load_n(&worker_done, __ATOMIC_ACQUIRE) == 0) {
                PyObject *obj = pcc_gc_alloc(24, PY_TYPE_INT, 0);
                if (obj == 0) return (void *)(intptr_t)2;
                pcc_gc_release(obj);
                __atomic_add_fetch(&worker_iterations, 1, __ATOMIC_RELAXED);
            }
            return 0;
        }

        int main(void) {
            if (pcc_threads_enabled() != 1) return 2;

            PccThreadHandle *thread = 0;
            if (pcc_thread_start(&thread, worker_main, 0) != 0) return 3;

            while (__atomic_load_n(&worker_started, __ATOMIC_ACQUIRE) == 0) {
                pcc_thread_safepoint();
            }
            while (__atomic_load_n(&worker_iterations, __ATOMIC_RELAXED) < 1) {
                pcc_thread_safepoint();
            }

            for (int i = 0; i < 8; i++) {
                if (pcc_stop_the_world() != 0) return 10;
                if (pcc_resume_world() != 0) return 11;
            }

            __atomic_store_n(&worker_done, 1, __ATOMIC_RELEASE);
            void *result = 0;
            if (pcc_thread_join(thread, &result) != 0) return 12;
            if (result != 0) return 13;

            printf("alloc-safepoint-ok\n");
            return 0;
        }
        """).lstrip(), encoding="utf-8")

    build = subprocess.run(
        [
            cc,
            "-std=c11",
            "-pthread",
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
    assert build.returncode == 0, build.stdout + build.stderr
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "alloc-safepoint-ok"


def test_thread_safepoint_composes_with_all_gc_backends(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent("""
        from pcc.extern import extern, c_int32, c_int64, c_void

        pcc_gc_backend = extern("pcc_gc_backend", (), c_int64)
        pcc_gc_set_backend = extern("pcc_gc_set_backend", (c_int64,), c_int64)
        pcc_gc_collect = extern("pcc_gc_collect", (c_int32,), c_int64)
        pcc_gc_safepoint = extern("pcc_gc_safepoint", (), c_void)
        pcc_stop_the_world = extern("pcc_stop_the_world", (), c_int64)
        pcc_resume_world = extern("pcc_resume_world", (), c_int64)

        def exercise(backend: int) -> None:
            print(pcc_gc_set_backend(backend))
            print(pcc_stop_the_world())
            pcc_gc_safepoint()
            print(pcc_resume_world())
            print(pcc_gc_backend())
            print(pcc_gc_collect(0) >= 0)

        def main() -> None:
            exercise(0)
            exercise(1)
            exercise(2)
            exercise(3)
            exercise(4)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    expected: list[str] = []
    for backend in range(5):
        expected.extend(["0", "0", "0", str(backend), "True"])
    assert result.stdout.strip().splitlines() == expected


def test_threading_substrate_runs_in_no_libpython_binary(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent("""
        from pcc.extern import extern, c_int64, c_void

        pcc_threads_enabled = extern("pcc_threads_enabled", (), c_int64)
        pcc_current_thread_id = extern("pcc_current_thread_id", (), c_int64)
        pcc_refcount_strategy = extern("pcc_refcount_strategy", (), c_int64)
        pcc_thread_safepoint = extern("pcc_thread_safepoint", (), c_void)
        pcc_stop_the_world = extern("pcc_stop_the_world", (), c_int64)
        pcc_resume_world = extern("pcc_resume_world", (), c_int64)

        def main() -> None:
            print(pcc_threads_enabled())
            print(pcc_refcount_strategy())
            tid1 = pcc_current_thread_id()
            tid2 = pcc_current_thread_id()
            print(tid1 == tid2)
            pcc_thread_safepoint()
            print(pcc_stop_the_world())
            pcc_thread_safepoint()
            print(pcc_resume_world())

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")

    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    # Every runtime is threaded since 560c23fdf: threads enabled, atomic
    # reference counts (PCC_REFCOUNT_STRATEGY_ATOMIC).
    assert result.stdout.strip().splitlines() == ["1", "1", "True", "0", "0"]
