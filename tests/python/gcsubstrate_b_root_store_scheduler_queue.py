"""Root store transactions, scheduler queue transfer/pop finalizers, refcount debug boundaries.

Split from ``test_gc_threading_substrate.py``; collected only through
that facade so pytest node ids stay stable.
"""
from _gc_substrate_common import *  # noqa: F401,F403


def test_root_store_prepares_inside_and_finishes_after_its_own_lock_scope():

    header_src = RUNTIME_HEADER.read_text(encoding="utf-8")
    for helper in [
        "pcc_incref_prepare",
        "pcc_incref_finish",
        "pcc_decref_prepare",
        "pcc_decref_finish",
    ]:
        assert helper not in header_src
        assert helper not in RUNTIME_SIGNATURES

    py_src = PY_OBJ_PORT.read_text(encoding="utf-8")
    for helper in [
        "_py_incref_prepare",
        "_py_incref_finish",
        "_py_decref_prepare",
        "_py_decref_finish",
    ]:
        assert f"def {helper}(" in py_src
    py_incref_prepare = py_src.split("def _py_incref_prepare(", 1)[1].split(
        "def _py_incref_finish(", 1
    )[0]
    assert "pcc_refcount_incref(" in py_incref_prepare
    assert "pcc_diagnostics_runtime_log_event_code" not in py_incref_prepare
    assert "_pcc_debug_bad_incref(" not in py_incref_prepare
    py_incref_finish = py_src.split("def _py_incref_finish(", 1)[1].split(
        '@c_abi_export("py_incref")', 1
    )[0]
    assert py_incref_finish.count("pcc_diagnostics_runtime_log_event_code(") == 1
    assert "\n            3,\n            1," in py_incref_finish
    py_decref_prepare = py_src.split("def _py_decref_prepare(", 1)[1].split(
        "def _py_decref_finish(", 1
    )[0]
    assert "pcc_refcount_decref(" in py_decref_prepare
    assert "524288" in py_decref_prepare
    for forbidden in [
        "pcc_diagnostics_runtime_log_event_code",
        "_pcc_debug_bad_incref(",
        "py_weakref_invalidate",
        "pcc_gc_note_object_freeing",
        "py_gc_untrack",
        "pcc_dealloc_with_trash",
    ]:
        assert forbidden not in py_decref_prepare
    py_underflow_prepare = py_decref_prepare.split(
        "pre_rc: int = load_i64(o, PYOBJECTHEADER_REFCOUNT_OFFSET)", 1
    )[1].split("new_rc: int = pcc_refcount_decref(o)", 1)[0]
    assert "if pre_rc <= 0:" in py_underflow_prepare
    assert "store_i64(prepared, 48, 1)" in py_underflow_prepare
    assert "return" in py_underflow_prepare
    assert "pcc_refcount_decref(" not in py_underflow_prepare
    py_decref_finish = py_src.split("def _py_decref_finish(", 1)[1].split(
        '@c_abi_export("py_decref")', 1
    )[0]
    assert "pcc_refcount_decref(" not in py_decref_finish
    assert "pcc_gc_note_relocation_read(" not in py_decref_finish
    assert "py_weakref_invalidate(" in py_decref_finish
    assert "pcc_gc_note_object_freeing(" in py_decref_finish
    py_underflow_finish = py_decref_finish.split(
        "if load_i64(prepared, 48) != 0:", 1
    )[1].split("if load_i64(prepared, 40) == 0:", 1)[0]
    assert "_pcc_debug_bad_incref(" in py_underflow_finish
    assert "return" in py_underflow_finish
    py_nonterminal_finish = py_decref_finish.split("if new_rc > 0:", 1)[1].split(
        "delay_zpage_freeing_note: int = 0", 1
    )[0]
    py_terminal_finish = py_decref_finish.split(
        "delay_zpage_freeing_note: int = 0", 1
    )[1]
    assert py_nonterminal_finish.count("pcc_diagnostics_runtime_log_event_code(") == 1
    assert py_nonterminal_finish.count(
        "pcc_diagnostics_runtime_log_event_code(3, 2"
    ) == 1
    assert "return" in py_nonterminal_finish
    assert py_terminal_finish.count("pcc_diagnostics_runtime_log_event_code(") == 2
    assert py_terminal_finish.count("pcc_diagnostics_runtime_log_event_code(3, 2") == 1
    assert py_terminal_finish.count("pcc_diagnostics_runtime_log_event_code(3, 3") == 1

    py_incref_public = py_src.split('@c_abi_export("py_incref")', 1)[1].split(
        "def _py_decref_prepare(", 1
    )[0]
    # Plan-based collectors delegate to the prepare/finish pair before any
    # direct kernel call; standalone GC0 needs no prepare record on the
    # nonterminal path and increments through the kernel itself.
    delegate = py_incref_public.index("if _gc_backend_fast() != 0:")
    assert py_incref_public.count("_py_incref_prepare(o, prepared)") == 1
    assert py_incref_public.count("_py_incref_finish(prepared)") == 1
    assert delegate < py_incref_public.index("_py_incref_prepare(o, prepared)")
    assert py_incref_public.index("_py_incref_finish(prepared)") < py_incref_public.index("pcc_refcount_incref(")
    py_decref_public = py_src.split('@c_abi_export("py_decref")', 1)[1]
    delegate = py_decref_public.index("if _gc_backend_fast() != 0:")
    first_prepare = py_decref_public.index("_py_decref_prepare(o, prepared)")
    assert delegate < first_prepare < py_decref_public.index("_py_decref_finish(prepared)")
    # A GC0 count that would reach zero shares the terminal finish too.
    assert py_decref_public.index("_py_decref_finish(prepared)") < py_decref_public.index("pcc_refcount_decref(")
    for helper in [
        "_py_incref_prepare",
        "_py_incref_finish",
        "_py_decref_prepare",
        "_py_decref_finish",
    ]:
        assert f'@c_abi_export("{helper}")' not in py_src
        assert helper not in RUNTIME_SIGNATURES

    # Only pcc_gc_store_root's own body: later exports (store_root_take and
    # others) run their own plans.
    py_root = py_src.split('@c_abi_export("pcc_gc_store_root")', 1)[1].split(
        "\n@c_abi_export(", 1
    )[0]
    assert (
        py_root.index("pcc_gc_store_root_plan_init(plan, backend)")
        < py_root.index("pcc_py_gc_minor_graph_lock()")
    )
    py_locked = py_root.split("pcc_py_gc_minor_graph_lock()", 1)[1].split(
        "pcc_py_gc_minor_graph_unlock()", 1
    )[0]
    assert py_locked.count(
        "pcc_gc_store_root_plan_commit_locked(plan, slot, value)"
    ) == 1
    for forbidden in [
        "pcc_diagnostics_runtime_log_event_code",
        "py_incref(",
        "py_decref(",
        "_py_incref_finish(",
        "_py_decref_finish(",
    ]:
        assert forbidden not in py_locked
    py_tail = py_root.split("pcc_py_gc_minor_graph_unlock()", 1)[1]
    assert py_tail.count("pcc_gc_store_root_plan_finish(plan)") == 1


def test_scheduler_queue_root_transfer_plans_finish_after_outer_graph_unlock():
    """Every C queue root transfer defers reentrant tails past its graph lock."""
    public_header = RUNTIME_HEADER.read_text(encoding="utf-8")
    internal_header = (
        REPO_ROOT / "pcc" / "runtime" / "src" / "py_internal.h"
    ).read_text(encoding="utf-8")
    plan_symbols = [
        "pcc_gc_store_root_plan_init",
        "pcc_gc_store_root_plan_commit_locked",
        "pcc_gc_store_root_plan_finish",
    ]
    assert "uint64_t opaque[16]" in internal_header
    for symbol in plan_symbols:
        assert symbol in internal_header
        assert symbol not in public_header
        assert symbol not in RUNTIME_SIGNATURES

    def locked_region(body: str) -> str:
        return body.split("pcc_gc_graph_lock();", 1)[1].split(
            "pcc_gc_graph_unlock();", 1
        )[0]

    forbidden_locked = [
        "pcc_gc_store_ptr(",
        "pcc_gc_store_root(",
        "pcc_gc_scheduler_root_register_handle(",
        "pcc_gc_scheduler_root_unregister_handle(",
        "pcc_gc_resolve_root_slot_unlocked(",
        "pcc_gc_store_root_plan_finish(",
        "py_incref(",
        "py_decref(",
        "malloc(",
        "calloc(",
        "free(",
    ]

    expected_cross_signatures = {
        "pcc_gc_store_root_plan_init": (
            ("c_ptr", "c_int64"),
            "c_void",
        ),
        "pcc_gc_store_root_plan_commit_locked": (
            ("c_ptr", "c_ptr", "c_ptr"),
            "c_int64",
        ),
        "pcc_gc_store_root_plan_finish": (("c_ptr",), "c_void"),
        "pcc_gc_scheduler_root_link_locked": (("c_ptr",), "c_void"),
        "pcc_gc_scheduler_root_unlink_locked": (
            ("c_ptr",),
            "c_int64",
        ),
    }
    for symbol, signature in expected_cross_signatures.items():
        assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES[symbol] == signature
        assert symbol not in RUNTIME_SIGNATURES

    strict_obj = PY_OBJ_PORT.read_text(encoding="utf-8")
    for symbol in plan_symbols:
        assert f'@c_abi_export("{symbol}")' in strict_obj
    strict_init = strict_obj.split(
        '@c_abi_export("pcc_gc_store_root_plan_init")', 1
    )[1].split(
        '@c_abi_export("pcc_gc_store_root_plan_commit_locked")', 1
    )[0]
    assert "memset(plan, 0, 128)" in strict_init
    assert "store_i64(plan, 112, backend)" in strict_init
    assert "store_i32(plan, 120, 0)" in strict_init
    strict_commit = strict_obj.split(
        "def _pcc_gc_store_plan_commit_locked(", 1
    )[1].split(
        '@c_abi_export("pcc_gc_store_root_plan_commit_locked")', 1
    )[0]
    for forbidden in [
        "pcc_diagnostics_runtime_log_event_code(",
        "_py_incref_finish(",
        "_py_decref_finish(",
        "py_incref(",
        "py_decref(",
        "malloc(",
        "free(",
        "pcc_gc_note_safepoint(",
    ]:
        assert forbidden not in strict_commit
    assert (
        strict_commit.index("_py_incref_prepare(value, plan)")
        < strict_commit.index("pcc_gc_note_slot_write_barrier(")
        < strict_commit.index("old = load_ptr(slot, 0)")
        < strict_commit.index("store_ptr(slot, 0, load_ptr(plan, 0))")
        < strict_commit.index(
            "_py_decref_prepare(old, ptr_add(plan, 56))"
        )
    )

    strict = PY_GC_BACKEND_PORT.read_text(encoding="utf-8")
    strict_queue_new = strict.split(
        'def pcc_gc_scheduler_queue_new():', 1
    )[1].split('@c_abi_export("pcc_gc_scheduler_queue_free")', 1)[0]
    assert strict_queue_new.count("queue = malloc(48)") == 1
    assert (
        strict_queue_new.index("store_ptr(queue, 0, mutex)")
        < strict_queue_new.index("store_ptr(queue, 8, null())")
        < strict_queue_new.index("store_ptr(queue, 16, null())")
        < strict_queue_new.index("store_i64(queue, 24, 0)")
        < strict_queue_new.index("store_ptr(queue, 32, null())")
        < strict_queue_new.index("store_i64(queue, 40, 0)")
    )
    strict_free = strict.split(
        "def _scheduler_queue_entry_free(entry) -> None:", 1
    )[1].split("def _scheduler_queue_entry_alloc(queue):", 1)[0]
    strict_alloc = strict.split(
        "def _scheduler_queue_entry_alloc(queue):", 1
    )[1].split("def _scheduler_queue_entry_recycle(queue, entry) -> None:", 1)[0]
    strict_recycle = strict.split(
        "def _scheduler_queue_entry_recycle(queue, entry) -> None:", 1
    )[1].split("def _scheduler_queue_entry_release(queue, entry) -> None:", 1)[0]
    strict_release = strict.split(
        "def _scheduler_queue_entry_release(queue, entry) -> None:", 1
    )[1].split('@c_abi_export("pcc_gc_scheduler_queue_new")', 1)[0]
    strict_push = strict.split(
        'def pcc_gc_scheduler_queue_push(queue, value) -> int:', 1
    )[1].split(
        '@c_abi_export("pcc_gc_scheduler_queue_pop_into")', 1
    )[0]
    strict_pop = strict.split(
        'def pcc_gc_scheduler_queue_pop_into(queue, out_slot) -> int:', 1
    )[1].split('@c_abi_export("pcc_gc_scheduler_queue_len")', 1)[0]

    def strict_locked_region(body: str) -> str:
        return body.split("_object_graph_lock()", 1)[1].split(
            "_object_graph_unlock()", 1
        )[0]

    strict_forbidden_locked = [
        "pcc_gc_store_root_extern(",
        "pcc_gc_scheduler_root_register_handle(",
        "pcc_gc_scheduler_root_unregister_handle(",
        "_resolve_root_slot_unlocked(",
        "pcc_gc_store_root_plan_finish(",
        "py_incref(",
        "py_decref(",
        "malloc(",
        "free(",
    ]
    for body in [strict_free, strict_release, strict_push, strict_pop]:
        locked = strict_locked_region(body)
        assert "pcc_gc_store_root_plan_commit_locked(" in locked
        for forbidden in strict_forbidden_locked:
            assert forbidden not in locked
        tail = body.split("_object_graph_unlock()", 1)[1]
        assert "pcc_gc_store_root_plan_finish(" in tail

    assert (
        strict_push.index("_scheduler_root_node_alloc(")
        < strict_push.index("pcc_gc_store_root_plan_init(")
        < strict_push.index("_object_graph_lock()")
    )
    strict_push_locked = strict_locked_region(strict_push)
    assert (
        strict_push_locked.index(
            "pcc_gc_store_root_plan_commit_locked("
        )
        < strict_push_locked.index("store_ptr(entry, 16, root_node)")
        < strict_push_locked.index("pcc_gc_scheduler_root_link_locked(")
    )
    assert strict_push_locked.count("store_ptr(entry, 16, root_node)") == 1
    assert strict_push_locked.count(
        "pcc_gc_scheduler_root_link_locked("
    ) == 1
    strict_push_tail = strict_push.split("_object_graph_unlock()", 1)[1]
    assert strict_push_tail.index(
        "pcc_gc_cycle_requested_store_release(1)"
    ) < strict_push_tail.index("pcc_gc_store_root_plan_finish(")

    for body in [strict_free, strict_release, strict_pop]:
        assert body.index("pcc_gc_store_root_plan_init(") < body.index(
            "_object_graph_lock()"
        )
        locked = strict_locked_region(body)
        assert "pcc_gc_scheduler_root_unlink_locked(" in locked
        tail = body.split("_object_graph_unlock()", 1)[1]
        assert tail.index(
            "pcc_gc_cycle_requested_store_release(1)"
        ) < tail.index("_scheduler_root_node_free(")
    assert strict_locked_region(strict_pop).count(
        "pcc_gc_store_root_plan_commit_locked("
    ) == 2
    strict_pop_tail = strict_pop.split("_object_graph_unlock()", 1)[1]
    assert strict_pop_tail.index("_scheduler_queue_entry_recycle(") < (
        strict_pop_tail.index("pcc_gc_store_root_plan_finish(")
    )
    strict_release_tail = strict_release.split("_object_graph_unlock()", 1)[1]
    assert strict_release_tail.index("_scheduler_queue_entry_recycle(") < (
        strict_release_tail.index("pcc_gc_store_root_plan_finish(")
    )
    strict_free_tail = strict_free.split("_object_graph_unlock()", 1)[1]
    assert strict_free_tail.index("free(entry)") < strict_free_tail.index(
        "pcc_gc_store_root_plan_finish("
    )
    for body in [strict_free, strict_release, strict_pop]:
        assert body.count("_scheduler_root_node_free(root_node)") == 1
    assert (
        strict_alloc.index("entry = load_ptr(queue, 32)")
        < strict_alloc.index("store_ptr(queue, 32, load_ptr(entry, 8))")
        < strict_alloc.index("store_i64(queue, 40, count - 1)")
    )
    assert strict_alloc.index(
        "store_ptr(queue, 32, load_ptr(entry, 8))"
    ) < strict_alloc.index("entry = malloc(24)")
    assert strict_recycle.count("free(entry)") == 3
    assert (
        strict_recycle.index("store_ptr(entry, 8, load_ptr(queue, 32))")
        < strict_recycle.index("store_ptr(queue, 32, entry)")
        < strict_recycle.index("store_i64(queue, 40, count + 1)")
    )
    strict_alloc_failure = strict_push.split(
        "if ptr_is_null(root_node) != 0:", 1
    )[1].split("store_plan = stack_alloc(128)", 1)[0]
    assert strict_alloc_failure.count(
        "_scheduler_queue_entry_recycle(queue, entry)"
    ) == 1
    assert strict_alloc_failure.count("return -1") == 1
    strict_push_tail = strict_push.split("_object_graph_unlock()", 1)[1]
    assert (
        strict_push_tail.index("if published == 0:")
        < strict_push_tail.index("_scheduler_root_node_free(root_node)")
        < strict_push_tail.index("_scheduler_queue_entry_recycle(queue, entry)")
        < strict_push_tail.index("pcc_gc_store_root_plan_finish(store_plan)")
        < strict_push_tail.index("if published == 0:\n        return -1")
    )
    strict_lock_failure = strict_push.split(
        "if pcc_mutex_lock(mutex) != 0:", 1
    )[1].split("tail = load_ptr(queue, 16)", 1)[0]
    assert strict_lock_failure.count(
        "_scheduler_queue_entry_release(queue, entry)"
    ) == 1
    assert strict_lock_failure.count("return -1") == 1
    strict_root = strict_obj.split(
        '@c_abi_export("pcc_gc_store_root")', 1
    )[1].split('@c_abi_export("pcc_gc_frame_enter")', 1)[0]
    assert strict_root.count("stack_alloc(128)") == 1
    assert strict_free.count("stack_alloc(128)") == 1
    assert strict_release.count("stack_alloc(128)") == 1
    assert strict_push.count("stack_alloc(128)") == 1
    assert strict_pop.count("stack_alloc(128)") == 2


@pytest.mark.parametrize("kind", ["pcc_python"])
@pytest.mark.parametrize("backend", [3, 4])
def test_scheduler_queue_pop_finalizer_runs_after_outer_graph_unlock(
    tmp_path: Path,
    kind: str,
    backend: int,
) -> None:
    """Queue-pop replacement finalizers run after the queue graph transaction."""
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem=f"scheduler_queue_pop_finalizer_gc{backend}",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>

            static PccThreadHandle *contender;
            static PccGcSchedulerQueue *queue;
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
                if (pcc_gc_scheduler_queue_len(queue) != 0) {
                    return (void *)(uintptr_t)2;
                }
                if (pcc_gc_object_is_known(anchor) != 1) {
                    return (void *)(uintptr_t)3;
                }
                if (pcc_gc_scheduler_root_count() != 0) {
                    return (void *)(uintptr_t)4;
                }
                __atomic_store_n(&worker_acquired, 1, __ATOMIC_RELEASE);
                return 0;
            }

            static int start_contender(void) {
                __atomic_store_n(&worker_ready, 0, __ATOMIC_RELEASE);
                __atomic_store_n(&worker_go, 0, __ATOMIC_RELEASE);
                __atomic_store_n(&worker_acquired, 0, __ATOMIC_RELEASE);
                __atomic_store_n(&finalizer_joined, 0, __ATOMIC_RELEASE);
                if (pcc_thread_start(&contender, lock_contender, 0) != 0) {
                    return -1;
                }
                while (
                    __atomic_load_n(&worker_ready, __ATOMIC_ACQUIRE) == 0
                ) {
                }
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
                    pcc_refcount_strategy()
                    != PCC_REFCOUNT_STRATEGY_ATOMIC
                ) return 1;
                if (pcc_gc_set_backend(@BACKEND@) != 0) return 2;

                anchor = py_list_new(0);
                if (anchor == 0) return 3;
                if (start_contender() != 0) return 4;

                PyClassObject *cls = py_class_new(
                    "SchedulerQueuePopFinalizer", 0, 0, 0, 0
                );
                if (cls == 0) return 5;
                py_class_add_method(
                    cls,
                    "__del__",
                    (PyObject *)(uintptr_t)joining_finalizer
                );
                PyObject *replaced = py_instance_new(cls);
                PyObject *queued = py_list_new(0);
                queue = pcc_gc_scheduler_queue_new();
                if (replaced == 0 || queued == 0 || queue == 0) return 6;

                PyObject *out = 0;
                pcc_gc_store_root(&out, replaced);
                py_decref(replaced);
                if (pcc_gc_scheduler_queue_push(queue, queued) != 0) return 7;
                py_decref(queued);

                /* The old implementation reaches replaced's last decref
                 * while queue-pop still owns the recursive graph lock.  Its
                 * finalizer joins a real pthread whose next public operations
                 * acquire this same queue mutex and the graph lock.  The
                 * subprocess watchdog exposes either lock-held callback tail
                 * deterministically. */
                if (pcc_gc_scheduler_queue_pop_into(queue, &out) != 1) return 8;

                if (out != queued) return 9;
                if (pcc_gc_scheduler_queue_len(queue) != 0) return 10;
                if (pcc_gc_scheduler_root_count() != 0) return 11;
                if (
                    __atomic_load_n(&finalizer_calls, __ATOMIC_ACQUIRE) != 1
                ) return 12;
                if (
                    __atomic_load_n(&worker_acquired, __ATOMIC_ACQUIRE) != 1
                ) return 13;
                if (
                    __atomic_load_n(&finalizer_joined, __ATOMIC_ACQUIRE) != 1
                ) return 14;

                pcc_gc_store_root(&out, 0);

                /* A NULL output makes the queue entry clear-plan itself the
                 * terminal decrement.  Its finalizer must observe the same
                 * empty queue/root state after entry recycling. */
                if (start_contender() != 0) return 15;
                PyObject *terminal = py_instance_new(cls);
                if (terminal == 0) return 16;
                if (
                    pcc_gc_scheduler_queue_push(queue, terminal) != 0
                ) return 17;
                py_decref(terminal);
                if (
                    pcc_gc_scheduler_queue_pop_into(queue, 0) != 1
                ) return 18;
                if (
                    __atomic_load_n(&finalizer_calls, __ATOMIC_ACQUIRE) != 2
                ) return 19;
                if (
                    __atomic_load_n(&worker_acquired, __ATOMIC_ACQUIRE) != 1
                ) return 20;
                if (
                    __atomic_load_n(&finalizer_joined, __ATOMIC_ACQUIRE) != 1
                ) return 21;
                if (
                    pcc_gc_scheduler_queue_len(queue) != 0
                    || pcc_gc_scheduler_root_count() != 0
                ) return 22;

                pcc_gc_scheduler_queue_free(queue);
                py_decref((PyObject *)cls);
                py_decref(anchor);
                return 0;
            }
        '''.replace("@BACKEND@", str(backend)),
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
        timeout=20,
        env=run_env,
    )
    assert run.returncode == 0, (
        f"{kind} gc{backend} scheduler queue pop finalizer probe returned "
        f"{run.returncode}: "
        + run.stdout
        + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
@pytest.mark.parametrize("backend", [3, 4])
def test_scheduler_queue_transfer_canonicalizes_forwarded_value_and_balances_exact_counts(
    tmp_path: Path,
    kind: str,
    backend: int,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem=f"scheduler_queue_forwarded_balance_gc{backend}",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>

            _Static_assert(sizeof(void *) == 8, "queue layout needs 64-bit pointers");
            _Static_assert(sizeof(int64_t) == 8, "queue layout needs 64-bit i64");

            static void *queue_ptr_at(
                PccGcSchedulerQueue *queue, int64_t offset
            ) {
                return *(void **)((char *)queue + offset);
            }

            static int64_t queue_i64_at(
                PccGcSchedulerQueue *queue, int64_t offset
            ) {
                return *(int64_t *)((char *)queue + offset);
            }

            static int64_t refcount_of(PyObject *obj) {
                return pcc_refcount_load(&((PyObjectHeader *)obj)->refcount);
            }

            static int force_minor_refill(void) {
                for (int i = 0; i < 6; i++) {
                    PyObject *filler = pcc_gc_alloc(96, PY_TYPE_FLOAT, 0);
                    if (filler == 0) return 30 + i;
                    pcc_gc_release(filler);
                }
                return 0;
            }

            static int fail_counts(
                int code,
                PyObject *out,
                PyObject *source,
                PyObject *target
            ) {
                fprintf(
                    stderr,
                    "code=%d out_is_target=%d source_rc=%lld target_rc=%lld "
                    "roots=%lld forwarding=%lld\n",
                    code,
                    out == target,
                    (long long)(source == 0 ? -99 : refcount_of(source)),
                    (long long)(target == 0 ? -99 : refcount_of(target)),
                    (long long)pcc_gc_scheduler_root_count(),
                    (long long)pcc_gc_backend4_forwarding_entries()
                );
                return code;
            }

            int main(void) {
                if (
                    pcc_refcount_strategy()
                    != PCC_REFCOUNT_STRATEGY_ATOMIC
                ) return 1;
                if (pcc_gc_set_backend(@BACKEND@) != 0) return 2;

                const int64_t backend = @BACKEND@;
                PccGcSchedulerQueue *queue = pcc_gc_scheduler_queue_new();
                PyObject *source = backend == 3
                    ? py_str_new("x", 1)
                    : py_list_new(0);
                if (queue == 0 || source == 0) return 3;
                if (refcount_of(source) != 1) return 4;
                PyObject *target = 0;
                int64_t expected_after_relocation = 0;
                int64_t expected_with_external = 0;
                if (backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) {
                    /* Keep one owned registered root across deterministic
                     * minor refill.  The collector rewrites that root, while
                     * source remains an owned stale pointer for queue push. */
                    PyObject *promotion_root = 0;
                    pcc_gc_store_root(&promotion_root, source);
                    void *promotion_handle =
                        pcc_gc_scheduler_root_register_handle(
                            &promotion_root
                        );
                    if (promotion_handle == 0) return 5;
                    int refill_error = force_minor_refill();
                    if (refill_error != 0) return refill_error;
                    target = pcc_gc_note_relocation_read(source);
                    if (
                        target == 0
                        || target == source
                        || promotion_root != target
                        || pcc_gc_backend4_forwarding_entries() != 1
                        || refcount_of(source) != 1
                        || refcount_of(target) != 2
                    ) return fail_counts(8, promotion_root, source, target);
                    py_incref(target);
                    pcc_gc_scheduler_root_unregister_handle(
                        promotion_handle
                    );
                    pcc_gc_store_root(&promotion_root, 0);
                    if (
                        promotion_root != 0
                        || pcc_gc_scheduler_root_count() != 0
                        || refcount_of(target) != 2
                    ) return fail_counts(9, promotion_root, source, target);

                    if (
                        pcc_gc_scheduler_queue_push(queue, source) != 0
                    ) return 10;
                    if (
                        pcc_gc_scheduler_queue_len(queue) != 1
                        || pcc_gc_scheduler_root_count() != 1
                        || pcc_gc_backend4_forwarding_entries() != 1
                        || refcount_of(source) != 1
                        || refcount_of(target) != 3
                    ) return fail_counts(11, 0, source, target);
                    py_decref(source);
                    source = 0;
                    if (
                        pcc_gc_backend4_forwarding_entries() != 0
                        || refcount_of(target) != 2
                    ) return fail_counts(12, 0, source, target);
                    expected_after_relocation = 1;
                    expected_with_external = 2;
                } else {
                    pcc_gc_reset_relocation_set();
                    if (pcc_gc_select_relocation_set(1) != 1) return 13;
                    target = pcc_gc_relocate_copy(
                        source, (int64_t)sizeof(PyListObject)
                    );
                    if (target != 0) py_decref(target);
                    if (target == 0 || target == source) return 14;
                    expected_after_relocation = 2;
                    if (
                        pcc_gc_backend4_forwarding_entries() != 1
                        || refcount_of(source) != 1
                        || refcount_of(target)
                            != expected_after_relocation
                    ) return fail_counts(15, 0, source, target);
                    py_incref(target);
                    expected_with_external = 3;
                    if (refcount_of(target) != expected_with_external) {
                        return fail_counts(16, 0, source, target);
                    }
                    /* Push the stale OLD pointer only after forwarding is
                     * installed.  The queue plan must publish/retain NEW. */
                    if (
                        pcc_gc_scheduler_queue_push(queue, source) != 0
                    ) return 5;
                    if (
                        pcc_gc_scheduler_queue_len(queue) != 1
                        || pcc_gc_scheduler_root_count() != 1
                        || refcount_of(source) != 1
                        || refcount_of(target) != 4
                    ) return fail_counts(6, 0, source, target);
                    py_decref(source);
                    if (
                        refcount_of(source) != 1
                        || refcount_of(target) != expected_with_external
                    ) return fail_counts(7, 0, source, target);
                }

                /* Exact queue ownership transfers to out without changing
                 * NEW's net count; all queue roots are detached first. */
                PyObject *out = 0;
                if (pcc_gc_scheduler_queue_pop_into(queue, &out) != 1) {
                    return fail_counts(17, out, source, target);
                }
                if (
                    out != target
                    || pcc_gc_scheduler_queue_len(queue) != 0
                    || pcc_gc_scheduler_root_count() != 0
                    || pcc_gc_backend4_forwarding_entries()
                        != (backend == 3 ? 0 : 1)
                    || (backend == 4 && refcount_of(source) != 1)
                    || refcount_of(target) != expected_with_external
                ) return fail_counts(
                    18, out, source, target
                );

                /* Private differential layout contract, locked above by the
                 * static C/strict source assertions: head=8, free_head=32,
                 * free_count=40 in the shared 48-byte queue. */
                void *recycled_entry = queue_ptr_at(queue, 32);
                if (
                    recycled_entry == 0
                    || queue_ptr_at(queue, 8) != 0
                    || queue_i64_at(queue, 40) != 1
                ) return fail_counts(25, out, source, target);

                pcc_gc_store_root(&out, 0);
                if (
                    out != 0
                    || refcount_of(target) != expected_after_relocation
                ) {
                    return fail_counts(
                        19, out, source, target
                    );
                }

                /* The recycled non-tagged entry must be reusable without a
                 * duplicate root or ownership drift; NULL-pop clears it. */
                if (
                    pcc_gc_scheduler_queue_push(queue, target) != 0
                ) return fail_counts(20, out, source, target);
                if (
                    pcc_gc_scheduler_queue_len(queue) != 1
                    || pcc_gc_scheduler_root_count() != 1
                    || refcount_of(target)
                        != expected_after_relocation + 1
                    || queue_ptr_at(queue, 8) != recycled_entry
                    || queue_ptr_at(queue, 32) != 0
                    || queue_i64_at(queue, 40) != 0
                ) return fail_counts(21, out, source, target);
                if (
                    pcc_gc_scheduler_queue_pop_into(queue, 0) != 1
                    || pcc_gc_scheduler_queue_len(queue) != 0
                    || pcc_gc_scheduler_root_count() != 0
                    || refcount_of(target) != expected_after_relocation
                    || queue_ptr_at(queue, 8) != 0
                    || queue_ptr_at(queue, 32) != recycled_entry
                    || queue_i64_at(queue, 40) != 1
                ) return fail_counts(22, out, source, target);

                /* Free one live, non-tagged queue entry: entry_free must
                 * unlink the root and release exactly the queue ownership. */
                if (
                    pcc_gc_scheduler_queue_push(queue, target) != 0
                    || refcount_of(target)
                        != expected_after_relocation + 1
                    || queue_ptr_at(queue, 8) != recycled_entry
                    || queue_ptr_at(queue, 32) != 0
                    || queue_i64_at(queue, 40) != 0
                ) return fail_counts(23, out, source, target);
                pcc_gc_scheduler_queue_free(queue);
                if (
                    pcc_gc_scheduler_root_count() != 0
                    || refcount_of(target) != expected_after_relocation
                ) {
                    return fail_counts(
                        24, out, source, target
                    );
                }
                py_decref(target);
                return 0;
            }
        '''.replace("@BACKEND@", str(backend)),
    )
    run_env = {
        **os.environ,
        "PCC_GC_MINOR_HEAP_SIZE": "256",
        "PCC_GC_MINOR_ALLOC_MAX": "128",
    }
    run_env.pop("PCC_REFCOUNT_STRATEGY", None)
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=20,
        env=run_env,
    )
    assert run.returncode == 0, (
        f"{kind} gc{backend} forwarded scheduler queue balance returned "
        f"{run.returncode}: "
        + run.stdout
        + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
@pytest.mark.parametrize("backend", [3, 4])
def test_outermost_root_store_finalizer_runs_after_its_own_lock_scope(
    tmp_path: Path,
    kind: str,
    backend: int,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem=f"root_store_deferred_finalizer_gc{backend}",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>

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
                /* Deliberately non-resurrecting: A3b proves only that an
                 * outermost root helper's callback tail runs after its own
                 * lock scope, not metadata restoration for last-decref
                 * resurrection or nested outer-lock callers. */
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
                    pcc_refcount_strategy()
                    != PCC_REFCOUNT_STRATEGY_ATOMIC
                ) return 1;
                if (pcc_gc_set_backend(@BACKEND@) != 0) return 2;
                anchor = py_list_new(0);
                if (anchor == 0) return 3;
                if (pcc_thread_start(&contender, lock_contender, 0) != 0) {
                    return 4;
                }
                while (__atomic_load_n(&worker_ready, __ATOMIC_ACQUIRE) == 0) {
                }

                PyClassObject *cls = py_class_new(
                    "RootStoreDeferredFinalizer", 0, 0, 0, 0
                );
                if (cls == 0) return 5;
                py_class_add_method(
                    cls,
                    "__del__",
                    (PyObject *)(uintptr_t)joining_finalizer
                );
                PyObject *inst = py_instance_new(cls);
                if (inst == 0) return 6;
                PyObject *root = 0;
                pcc_gc_store_root(&root, inst);
                py_decref(inst);

                /* This probe invokes root-store outermost.  Old code executes
                 * its last decref/finalizer inside the helper's graph-lock
                 * scope. The finalizer joins a worker whose next transition
                 * requires that same real lock, so the subprocess watchdog
                 * deterministically catches the cycle. */
                pcc_gc_store_root(&root, 0);

                if (root != 0) return 7;
                if (
                    __atomic_load_n(&finalizer_calls, __ATOMIC_ACQUIRE) != 1
                ) return 8;
                if (
                    __atomic_load_n(&worker_acquired, __ATOMIC_ACQUIRE) != 1
                ) return 9;
                if (
                    __atomic_load_n(&finalizer_joined, __ATOMIC_ACQUIRE) != 1
                ) return 10;
                py_decref((PyObject *)cls);
                py_decref(anchor);
                return 0;
            }
        '''.replace("@BACKEND@", str(backend)),
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
        timeout=20,
        env=run_env,
    )
    assert run.returncode == 0, (
        f"{kind} gc{backend} deferred-finalizer probe returned "
        f"{run.returncode}: "
        + run.stdout
        + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
@pytest.mark.parametrize("backend", [3, 4])
def test_root_store_canonicalizes_forwarded_value_and_balances_exact_counts(
    tmp_path: Path,
    kind: str,
    backend: int,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem=f"root_store_forwarded_balance_gc{backend}",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>

            static int64_t refcount_of(PyObject *obj) {
                return pcc_refcount_load(&((PyObjectHeader *)obj)->refcount);
            }

            static int fail_counts(
                int code,
                PyObject *root,
                PyObject *target,
                PyObject *replaced
            ) {
                fprintf(
                    stderr,
                    "code=%d root_is_target=%d target_rc=%lld "
                    "replaced_rc=%lld forwarding=%lld\n",
                    code,
                    root == target,
                    (long long)(target == 0 ? -99 : refcount_of(target)),
                    (long long)(replaced == 0 ? -99 : refcount_of(replaced)),
                    (long long)pcc_gc_backend4_forwarding_entries()
                );
                return code;
            }

            int main(void) {
                const int64_t backend = @BACKEND@;
                if (
                    pcc_refcount_strategy()
                    != PCC_REFCOUNT_STRATEGY_ATOMIC
                ) return 1;
                if (pcc_gc_set_backend(backend) != 0) return 2;

                PyObject *source = backend == 3
                    ? py_str_new("x", 1)
                    : py_list_new(0);
                if (source == 0) return 3;
                PyObject *root = 0;
                pcc_gc_store_root(&root, source);
                pcc_gc_release(source);
                if (root != source || refcount_of(source) != 1) return 4;

                PyObject *target = 0;
                if (backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) {
                    if (pcc_gc_step(1024) < 1) return 5;
                    target = pcc_gc_note_relocation_read(source);
                } else {
                    pcc_gc_reset_relocation_set();
                    if (pcc_gc_select_relocation_set(1) != 1) return 6;
                    target = pcc_gc_relocate_copy(
                        source, (int64_t)sizeof(PyListObject)
                    );
                    if (target != 0) {
                        /* Drop relocate_copy's caller-owned result.  The
                         * forwarding edge and moved root ownership remain. */
                        py_decref(target);
                    }
                }
                if (target == 0 || target == source) return 7;
                if (pcc_gc_backend4_forwarding_entries() != 1) return 8;
                if (refcount_of(source) != 1) return 9;
                int64_t target_before_external = refcount_of(target);
                int64_t expected_before_external = backend == 3 ? 1 : 2;
                if (target_before_external != expected_before_external) {
                    return fail_counts(10, root, target, 0);
                }

                /* Keep target readable after GC3 retires the stale shell and
                 * after the root is later replaced. */
                py_incref(target);
                int64_t expected_with_external = backend == 3 ? 2 : 3;
                if (refcount_of(target) != expected_with_external) {
                    return fail_counts(11, root, target, 0);
                }

                /* Both the incoming value and old root are the stale source.
                 * GC3 canonicalizes the retain, then decrements/retires the
                 * shell. GC4 resolves both operations onto count-on-new. */
                pcc_gc_store_root(&root, source);
                if (root != target) return fail_counts(12, root, target, 0);
                if (refcount_of(target) != expected_with_external) {
                    return fail_counts(13, root, target, 0);
                }
                int64_t expected_forwarding = backend == 3 ? 0 : 1;
                if (
                    pcc_gc_backend4_forwarding_entries()
                    != expected_forwarding
                ) return fail_counts(14, root, target, 0);
                if (backend == 4 && refcount_of(source) != 1) {
                    return fail_counts(15, root, target, 0);
                }

                PyObject *replaced = py_list_new(0);
                if (replaced == 0 || refcount_of(replaced) != 1) return 16;
                pcc_gc_store_root(&root, replaced);
                int64_t expected_after_replace = backend == 3 ? 1 : 2;
                if (
                    root != replaced
                    || refcount_of(replaced) != 2
                    || refcount_of(target) != expected_after_replace
                ) return fail_counts(17, root, target, replaced);

                pcc_gc_store_root(&root, 0);
                if (
                    root != 0
                    || refcount_of(replaced) != 1
                    || refcount_of(target) != expected_after_replace
                ) return fail_counts(18, root, target, replaced);

                py_decref(target);
                py_decref(replaced);
                return 0;
            }
        '''.replace("@BACKEND@", str(backend)),
    )
    run_env = {
        **os.environ,
        "PCC_GC_MINOR_HEAP_SIZE": "1024",
        "PCC_GC_MINOR_ALLOC_MAX": "128",
    }
    run_env.pop("PCC_REFCOUNT_STRATEGY", None)
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=20,
        env=run_env,
    )
    assert run.returncode == 0, (
        f"{kind} gc{backend} forwarded root-store balance returned "
        f"{run.returncode}: "
        + run.stdout
        + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_root_store_zero_refcount_underflow_fails_stop_in_finish(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="root_store_zero_refcount_underflow",
        source_text=r'''
            #include "py_internal.h"

            int main(void) {
                if (
                    pcc_refcount_strategy()
                    != PCC_REFCOUNT_STRATEGY_ATOMIC
                ) return 1;
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
                    ) != 0) return 2;
                PyObject *obj = pcc_gc_alloc(64, PY_TYPE_LIST, 0);
                if (obj == 0) return 3;
                ((PyObjectHeader *)obj)->refcount = 0;
                PyObject *root = obj;
                pcc_gc_store_root(&root, 0);
                return 4;
            }
        ''',
    )
    run_env = dict(os.environ)
    run_env.pop("PCC_REFCOUNT_STRATEGY", None)
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=20,
        env=run_env,
    )
    assert run.returncode in {-int(signal.SIGABRT), 134}, (
        f"{kind} zero-refcount root underflow did not fail-stop: "
        f"{run.returncode}: "
        + run.stdout
        + run.stderr
    )


def test_c_root_store_debug_off_invalid_new_preserves_benign_update(
    tmp_path: Path,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind="pcc_python",
        threaded=True,
        stem="root_store_invalid_new_token",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
                    ) != 0) return 2;
                PyObject *old = py_list_new(0);
                if (old == 0) return 3;
                PyObject *root = 0;
                pcc_gc_store_root(&root, old);
                if (
                    root != old
                    || pcc_refcount_load(
                        &((PyObjectHeader *)old)->refcount
                    ) != 2
                ) return 4;

                PyObject *invalid = (PyObject *)(uintptr_t)0x12340;
                pcc_gc_store_root(&root, invalid);
                if (
                    root != invalid
                    || pcc_refcount_load(
                        &((PyObjectHeader *)old)->refcount
                    ) != 1
                ) return 5;
                pcc_gc_store_root(&root, 0);
                if (root != 0) return 6;
                py_decref(old);
                return 0;
            }
        ''',
    )
    run_env = dict(os.environ)
    run_env.pop("PCC_DEBUG_RUNTIME", None)
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=20,
        env=run_env,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_c_public_refcount_debug_on_accepts_legitimate_cpy_handle(
    tmp_path: Path,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind="pcc_python",
        threaded=True,
        stem="public_refcount_debug_on_cpy_handle",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>

            static int64_t releases;

            static void release_foreign(void *value) {
                if (value == (void *)(uintptr_t)0x12340) releases++;
            }

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
                    ) != 0) return 1;
                py_cpy_handle_set_release_fn(release_foreign);
                PyObject *handle = py_cpy_handle_new(
                    (void *)(uintptr_t)0x12340
                );
                if (handle == 0) return 2;
                if (py_type_of(handle) != PY_TYPE_CPY_HANDLE) return 3;
                py_incref(handle);
                if (
                    pcc_refcount_load(
                        &((PyObjectHeader *)handle)->refcount
                    ) != 2
                ) return 4;
                py_decref(handle);
                if (
                    pcc_refcount_load(
                        &((PyObjectHeader *)handle)->refcount
                    ) != 1
                ) return 5;
                py_decref(handle);
                return releases == 1 ? 0 : 6;
            }
        ''',
    )
    run_env = dict(os.environ)
    run_env["PCC_DEBUG_RUNTIME"] = "1"
    run_env.pop("PCC_REFCOUNT_STRATEGY", None)
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=20,
        env=run_env,
    )
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.parametrize(
    "mode", ["invalid_new", "invalid_old", "invalid_tag_int32_min"]
)
def test_c_root_store_debug_on_invalid_value_traps_after_expected_boundary(
    tmp_path: Path,
    mode: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind="pcc_python",
        threaded=True,
        stem="root_store_debug_on_invalid_boundary",
        source_text=r'''
            #include "py_internal.h"
            #include <signal.h>
            #include <stdint.h>
            #include <unistd.h>

            static PyObject *volatile observed_root = 0;
            static PyObject *volatile expected_root = 0;
            static PyObject *volatile count_object = 0;
            static volatile sig_atomic_t expected_refcount = 0;

            static void on_fatal(int signo) {
                if (
                    signo != SIGTRAP
                    && signo != SIGILL
                    && signo != SIGABRT
                ) _exit(30);
                if (observed_root != expected_root) _exit(31);
                if (count_object == 0) _exit(32);
                if (
                    ((PyObjectHeader *)count_object)->refcount
                    != expected_refcount
                ) _exit(33);
                _exit(0);
            }

            int main(int argc, char **argv) {
                if (argc != 2) return 1;
                if (signal(SIGTRAP, on_fatal) == SIG_ERR) return 2;
                if (signal(SIGILL, on_fatal) == SIG_ERR) return 3;
                if (signal(SIGABRT, on_fatal) == SIG_ERR) return 4;
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
                    ) != 0) return 5;
                PyObject *invalid = (PyObject *)(uintptr_t)0x12340;

                if (argv[1][0] == 'n' || argv[1][0] == 't') {
                    PyObject *old = py_list_new(0);
                    if (old == 0) return 6;
                    pcc_gc_store_root(
                        (PyObject **)&observed_root,
                        old
                    );
                    if (
                        observed_root != old
                        || ((PyObjectHeader *)old)->refcount != 2
                    ) return 7;
                    expected_root = old;
                    count_object = old;
                    expected_refcount = 2;
                    if (argv[1][0] == 't') {
                        invalid = py_list_new(0);
                        if (invalid == 0) return 8;
                        ((PyObjectHeader *)invalid)->type_tag = INT32_MIN;
                    }
                    pcc_gc_store_root(
                        (PyObject **)&observed_root,
                        invalid
                    );
                    return 11;
                }

                PyObject *value = py_list_new(0);
                if (value == 0) return 9;
                observed_root = invalid;
                expected_root = value;
                count_object = value;
                expected_refcount = 2;
                pcc_gc_store_root(
                    (PyObject **)&observed_root,
                    value
                );
                return 10;
            }
        ''',
    )
    run_env = dict(os.environ)
    run_env["PCC_DEBUG_RUNTIME"] = "1"
    run_env.pop("PCC_REFCOUNT_STRATEGY", None)
    if mode == "invalid_new":
        argument = "new"
    elif mode == "invalid_old":
        argument = "old"
    else:
        argument = "tag"
    run = subprocess.run(
        [str(executable), argument],
        capture_output=True,
        text=True,
        timeout=20,
        env=run_env,
    )
    assert run.returncode == 0, (
        f"debug-on {mode} boundary returned {run.returncode}: "
        + run.stdout
        + run.stderr
    )
