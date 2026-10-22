"""Safepoint emission, colored aging and relocation selectors, cext claim unlock contracts, graph-lock no-park leases.

Split from ``test_gc_threading_substrate.py``; collected only through
that facade so pytest node ids stay stable.
"""
from _gc_substrate_common import *  # noqa: F401,F403


def test_gc_safepoint_polls_thread_gate_in_c_and_pcc_python_runtime():

    py_src = PY_OBJ_PORT.read_text(encoding="utf-8")
    py_body = py_src.split('@c_abi_export("pcc_gc_safepoint")', 1)[1]
    py_body = py_body.split('@c_abi_export("pcc_gc_collect")', 1)[0]
    assert "pcc_gc_note_safepoint()" in py_body
    # Compiled-code polls are application gates (095b286f0): GC4 relocates
    # only while every other mutator is parked at one.
    assert "pcc_thread_safepoint_gate()" in py_body


def test_gc_alloc_polls_thread_gate_without_non_threaded_gc_step():

    py_src = PY_OBJ_PORT.read_text(encoding="utf-8")
    py_body = py_src.split('@c_abi_export("pcc_gc_alloc")', 1)[1]
    py_body = py_body.split('@c_abi_export("pcc_gc_retain")', 1)[0]
    assert "pcc_thread_safepoint()" in py_body
    assert py_body.index("pcc_thread_safepoint()") < py_body.index("pcc_gc_note_alloc(size)")
    assert "pcc_gc_safepoint()" not in py_body
    assert "pcc_gc_step(" not in py_body


def test_python_codegen_emits_thread_safepoint_at_loop_backedges_and_function_entry():
    core_src = CORE_HELPERS.read_text(encoding="utf-8")
    assert "def _emit_thread_safepoint" in core_src
    assert 'self.runtime["pcc_thread_safepoint"]' in core_src
    core_poll = core_src.split("def _emit_thread_safepoint", 1)[1].split(
        "def _alloca_in_entry", 1
    )[0]
    assert "self.builder.load_atomic(" in core_poll
    assert '"acquire"' in core_poll
    assert "\n            4,\n" in core_poll
    assert "self.builder.load(" not in core_poll

    while_src = CONTROL_FLOW_LOWERING.read_text(encoding="utf-8")
    assert 'name=self._fresh("while.latch")' in while_src
    assert (
        "self.loop_stack.append((latch_bb, end_bb, self._loop_finally_base()))"
        in while_src
    )
    assert "self._emit_thread_safepoint()" in while_src
    assert "self.builder.branch(cond_bb)" in while_src

    for_src = FOR_LOOP_LOWERING.read_text(encoding="utf-8")
    assert for_src.count("self._emit_thread_safepoint()") >= 8
    for marker in [
        '"for.cpy.latch"',
        '"for.obj.latch"',
        '"for.iter.latch"',
        '"async.for.latch"',
        '"comp.latch"',
    ]:
        assert marker in for_src

    user_src = USER_FUNCTION_LOWERING.read_text(encoding="utf-8")
    assert "def _low_builder_thread_safepoint" in user_src
    assert '"pcc_thread_safepoint"' in user_src
    assert "self._emit_thread_safepoint()" in user_src
    low_poll = user_src.split("def _emit_thread_safepoint_poll_llvm", 1)[1].split(
        "def _low_builder_branch", 1
    )[0]
    assert "builder.load_atomic(" in low_poll
    assert '"acquire"' in low_poll
    assert "\n        4,\n" in low_poll
    assert "builder.load(" not in low_poll


def test_python_codegen_ir_contains_loop_and_entry_thread_safepoints(
    tmp_path,
    monkeypatch,
):
    from pcc.frontends.python.pipeline import compile_python

    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    src = tmp_path / "loop_safepoints.py"
    out_ll = tmp_path / "loop_safepoints.ll"
    src.write_text(textwrap.dedent("""
        def spin(n: int) -> int:
            acc = 0
            while acc < n:
                acc = acc + 1
                if acc == 3:
                    continue
            for i in range(0, n):
                acc = acc + i
            return acc
        """).lstrip(), encoding="utf-8")

    compile_python(
        str(src),
        str(out_ll),
        ir_scaffold_mode="on",
        libpython_mode="off",
        emit_llvm_only=True,
    )
    ir_text = out_ll.read_text(encoding="utf-8")
    assert "@pcc_thread_stop_requested = external global i32" in ir_text
    # The slow path parks at an application gate (095b286f0).
    assert (
        "declare external void @pcc_thread_safepoint_gate()" in ir_text
        or "declare void @pcc_thread_safepoint_gate()" in ir_text
    )
    assert ir_text.count("@pcc_thread_safepoint_gate()") >= 4
    stop_loads = [
        line.strip()
        for line in ir_text.splitlines()
        if "@pcc_thread_stop_requested" in line and "load" in line
    ]
    assert len(stop_loads) >= 4
    assert all("load atomic i32" in line for line in stop_loads)
    assert all(" acquire, align 4" in line for line in stop_loads)
    assert not any(" load i32" in line for line in stop_loads)
    assert "while.latch" in ir_text
    assert "for.step" in ir_text


def test_python_codegen_compiled_hot_loop_parks_via_generated_acquire_poll(
    tmp_path,
    monkeypatch,
    threaded_pcc_runtime_archive: Path,
):
    """A generated infinite loop must be the worker's only STW entry path."""
    from pcc.frontends.python.pipeline import compile_python

    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    monkeypatch.delenv("PCC_SELF_LINK", raising=False)
    src = tmp_path / "generated_stop_poll.py"
    exe = tmp_path / "generated_stop_poll"
    src.write_text(textwrap.dedent(r'''
        from pcc.extern import c_abi_typed_export, c_int64, c_ptr, extern
        from pcc.unsafe import (
            atomic_load_i32,
            atomic_store_i32,
            define_global_i32,
            function_addr,
            global_addr,
            load_ptr,
            null,
            stack_alloc,
        )

        pcc_thread_start = extern(
            "pcc_thread_start", (c_ptr, c_ptr, c_ptr), c_int64
        )
        pcc_thread_join = extern("pcc_thread_join", (c_ptr, c_ptr), c_int64)
        pcc_threads_enabled = extern("pcc_threads_enabled", (), c_int64)
        pcc_stop_the_world = extern("pcc_stop_the_world", (), c_int64)
        pcc_resume_world = extern("pcc_resume_world", (), c_int64)
        sched_yield = extern("sched_yield", (), c_int64)

        define_global_i32("generated_poll_entered", 0)
        define_global_i32("generated_poll_release", 0)
        define_global_i32("generated_poll_exited", 0)

        @c_abi_typed_export("generated_poll_worker", "ptr", ("ptr",))
        def generated_poll_worker(arg):
            atomic_store_i32(
                global_addr("generated_poll_entered"), 0, 1, "release"
            )
            # This loop has no call or finite bound that could satisfy STW.
            # Its gate load is deliberately relaxed: the generated stop poll
            # is the worker's only acquire and only safepoint after entry.
            while atomic_load_i32(
                global_addr("generated_poll_release"), 0, "relaxed"
            ) == 0:
                pass
            atomic_store_i32(
                global_addr("generated_poll_exited"), 0, 1, "release"
            )
            return null()

        def main() -> None:
            if pcc_threads_enabled() != 1:
                print("threads-disabled")
                return
            handle_out = stack_alloc(8)
            if pcc_thread_start(
                handle_out, function_addr("generated_poll_worker"), null()
            ) != 0:
                print("start-failed")
                return
            while atomic_load_i32(
                global_addr("generated_poll_entered"), 0, "acquire"
            ) == 0:
                pass

            # The gate is still closed, so this cannot succeed by natural
            # worker completion.  Only the generated loop poll can park it.
            if pcc_stop_the_world() != 0:
                print("stop-failed")
                return
            atomic_store_i32(
                global_addr("generated_poll_release"), 0, 1, "release"
            )
            turns = 0
            while turns < 256:
                sched_yield()
                turns = turns + 1
            if atomic_load_i32(
                global_addr("generated_poll_exited"), 0, "relaxed"
            ) != 0:
                print("worker-escaped-stw")
                return
            if pcc_resume_world() != 0:
                print("resume-failed")
                return
            if pcc_thread_join(load_ptr(handle_out, 0), null()) != 0:
                print("join-failed")
                return
            if atomic_load_i32(
                global_addr("generated_poll_exited"), 0, "acquire"
            ) != 1:
                print("worker-did-not-exit")
                return
            print("generated-stop-poll-ok")

        main()
        ''').lstrip(), encoding="utf-8")

    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
        runtime_archive=str(threaded_pcc_runtime_archive),
    )
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "generated-stop-poll-ok"


def test_python_codegen_zero_poll_env_disables_implicit_safepoints(
    tmp_path,
    monkeypatch,
):
    """``PCC_IMPLICIT_THREAD_SAFEPOINTS=0`` (runtime poll owners) means no polls."""
    from pcc.frontends.python.pipeline import compile_python

    monkeypatch.delenv("PCC_WITH_THREADS", raising=False)
    monkeypatch.setenv("PCC_IMPLICIT_THREAD_SAFEPOINTS", "0")
    src = tmp_path / "no_implicit_safepoints.py"
    out_ll = tmp_path / "no_implicit_safepoints.ll"
    src.write_text(
        "def identity(value: int) -> int:\n"
        "    return value\n",
        encoding="utf-8",
    )

    compile_python(
        str(src),
        str(out_ll),
        ir_scaffold_mode="on",
        libpython_mode="off",
        emit_llvm_only=True,
    )

    ir_text = out_ll.read_text(encoding="utf-8")
    assert "call void @pcc_thread_safepoint()" not in ir_text
    assert not [
        line
        for line in ir_text.splitlines()
        if "@pcc_thread_stop_requested" in line and "load" in line
    ]


def test_refcount_cycle_gc_collect_wraps_stw_gate_in_c_and_pcc_python_runtime():

    py_src = PY_OBJ_GC_PORT.read_text(encoding="utf-8")
    py_body = py_src.split('@c_abi_export("py_gc_collect")', 1)[1]
    assert 'extern("pcc_stop_the_world"' in py_src
    assert 'extern("pcc_resume_world"' in py_src
    assert "pcc_stop_the_world()" in py_body
    assert "while stw != 0:" in py_body
    assert "pcc_thread_safepoint()" in py_body
    assert "pcc_gc_default_table_lock()" in py_body
    assert "pcc_gc_default_table_unlock()" in py_body
    assert "pcc_resume_world()" in py_body
    assert py_body.index("pcc_gc_default_table_lock()") < py_body.index(
        'store_i32(collecting_slot, 0, 1)'
    )
    assert py_body.rindex("pcc_gc_default_table_unlock()") < py_body.rindex(
        "pcc_resume_world()"
    )


def test_generational_backend_step_polls_thread_safepoint_in_c_and_pcc_python_runtime():

    py_src = PY_GC_BARRIER_DISPATCHER.read_text(encoding="utf-8")
    py_body = py_src.split("def pcc_gc_step(budget: i64) -> i64:", 1)[1]
    py_body = py_body.split("def pcc_gc_note_alloc", 1)[0]
    py_body = py_body.split("if backend == 3:", 1)[1]
    py_body = py_body.split("elif backend == 4:", 1)[0]
    py_helper = PY_GC_GENERATIONAL_SCHEDULER.read_text(encoding="utf-8")
    assert 'extern("pcc_thread_safepoint"' in py_helper
    assert "pcc_gc_generational_step(budget, 1)" in py_body
    assert "pcc_thread_safepoint()" in py_helper


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_colored_generation_aging_polls_only_after_releasing_graph_lock(
    tmp_path: Path,
    kind: str,
) -> None:
    source_text = r'''
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>

            PCC_TEST_AGING_DECL

            static int64_t worker_ready;
            static int64_t worker_result;

            static void *aging_worker(void *arg) {
                (void)arg;
                __atomic_store_n(&worker_ready, 1, __ATOMIC_RELEASE);
                while (pcc_thread_stop_requested_acquire() == 0) {
                }
                worker_result = PCC_TEST_AGING_STEP(32);
                return 0;
            }

            int main(void) {
                if (pcc_threads_enabled() != 1) return 2;
                if (pcc_current_thread_id() <= 0) return 3;
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 4;

                PccThreadHandle *worker = 0;
                if (pcc_thread_start(&worker, aging_worker, 0) != 0) return 7;
                while (__atomic_load_n(&worker_ready, __ATOMIC_ACQUIRE) == 0) {
                }

                PyObject *objects[32];
                int64_t promotions_before =
                    pcc_gc_backend4_generation_promotion_score();
                for (int i = 0; i < 32; i++) {
                    objects[i] = py_list_new(0);
                    if (objects[i] == 0) return 5;
                    int32_t flags = py_header(objects[i])->flags;
                    if (
                        (flags & PY_FLAG_GC_YOUNG) == 0
                        || (flags & PY_FLAG_GC_OLD) != 0
                    ) return 6;
                }

                if (pcc_stop_the_world() != 0) return 8;
                if (pcc_thread_owns_stopped_world() != 1) return 9;

                /* The worker is parked at the first post-tenure poll.  The
                 * graph lock must already be available to the STW owner. */
                PCC_TEST_GRAPH_LOCK();
                int64_t promotions_during_stop =
                    pcc_gc_backend4_generation_promotion_score()
                    - promotions_before;
                int aged_during_stop = 0;
                for (int i = 0; i < 32; i++) {
                    int32_t flags = py_header(objects[i])->flags;
                    if (
                        (flags & PY_FLAG_GC_YOUNG) == 0
                        && (flags & PY_FLAG_GC_OLD) != 0
                    ) {
                        aged_during_stop++;
                    }
                }
                PCC_TEST_GRAPH_UNLOCK();
                if (
                    promotions_during_stop != 16
                    || aged_during_stop != 16
                ) {
                    fprintf(
                        stderr,
                        "mid-stop promotions=%lld aged=%d\n",
                        (long long)promotions_during_stop,
                        aged_during_stop
                    );
                    (void)pcc_resume_world();
                    return 10;
                }

                if (pcc_resume_world() != 0) return 11;
                void *thread_result = 0;
                if (pcc_thread_join(worker, &thread_result) != 0) return 12;
                if (thread_result != 0) return 13;
                if (worker_result != 32) return 14;
                if (
                    pcc_gc_backend4_generation_promotion_score()
                        - promotions_before
                    != 32
                ) return 15;
                for (int i = 0; i < 32; i++) {
                    int32_t flags = py_header(objects[i])->flags;
                    if (
                        (flags & PY_FLAG_GC_YOUNG) != 0
                        || (flags & PY_FLAG_GC_OLD) == 0
                    ) return 16;
                    py_decref(objects[i]);
                }
                return 0;
            }
        '''
    source_text = source_text.replace(
        "PCC_TEST_AGING_DECL",
        "extern int64_t pcc_gc_backend4_step_generation_aging(int64_t);",
    ).replace(
        "PCC_TEST_AGING_STEP",
        "pcc_gc_backend4_step_generation_aging",
    ).replace(
        "PCC_TEST_GRAPH_LOCK()", "pcc_py_gc_minor_graph_lock()"
    ).replace(
        "PCC_TEST_GRAPH_UNLOCK()", "pcc_py_gc_minor_graph_unlock()"
    )
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="colored_generation_aging_graph_unlock",
        source_text=source_text,
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=10
    )
    assert run.returncode == 0, (
        f"{kind} colored-generation aging probe returned {run.returncode}: "
        + run.stdout
        + run.stderr
    )


def test_colored_relocation_selector_polls_after_graph_unlock_in_c_and_strict():

    strict_src = PY_GC_RELOCATION_SELECTOR.read_text(encoding="utf-8")
    strict_score = strict_src.split(
        "def _backend4_zpage_candidate_score(", 1
    )[1].split(
        '@c_abi_export("pcc_gc_relocation_selector_add_candidate_node")', 1
    )[0]
    assert "(flags & (64 | 2048 | 8192 | 524288))" in strict_score
    assert 'global_load_ptr("pcc_gc_relocation_set_head")' not in strict_score
    assert 'global_load_ptr("pcc_gc_backend4_remembered_slots_head")' not in (
        strict_score
    )
    assert "score = score + load_i64(node, 72)" in strict_score
    strict_managed = PY_GC_BACKEND_PORT.read_text(encoding="utf-8")
    strict_remembered = strict_managed.split(
        "def _backend4_zpage_note_remembered_slot(owner, delta: int)", 1
    )[1].split(
        "def _backend4_zpage_note_remembered_card(owner, delta: int)", 1
    )[0]
    assert "owner_current: int = load_i64(node, 72) + delta" in (
        strict_remembered
    )
    assert "store_i64(node, 72, owner_current)" in strict_remembered
    strict_zpage_alloc = (
        RUNTIME_DIR / "py" / "freestanding_gc_zpage_allocation.py"
    ).read_text(encoding="utf-8")
    assert "store_i64(node, 72, 0)" in strict_zpage_alloc
    strict_zpage_mechanics = (
        RUNTIME_DIR / "py" / "freestanding_gc_zpage_mechanics.py"
    ).read_text(encoding="utf-8")
    assert "return malloc(80)" in strict_zpage_mechanics

    strict_scan = strict_src.split("def _best_relocation_page_batch(", 1)[
        1
    ].split("def _selector_page_scan_reset()", 1)[0]
    assert "while ptr_is_null(cursor) == 0 and examined < 16:" in strict_scan
    assert strict_scan.index("cursor = load_ptr(node, 16)") < strict_scan.index(
        "examined = examined + 1"
    )
    assert "load_i32(page, 108)" in strict_scan
    for forbidden in ("pcc_thread_safepoint(", "malloc(", "free("):
        assert forbidden not in strict_scan

    strict_page_batch = strict_src.split(
        "def _select_page_objects_batch(", 1
    )[1].split('@c_abi_export("pcc_gc_select_relocation_set")', 1)[0]
    assert "and examined < 16" in strict_page_batch
    assert strict_page_batch.count("examined = examined + 1") == 2
    strict_page_cursor_loop = strict_page_batch.split(
        'cursor = global_load_ptr("pcc_gc_backend4_selector_page_cursor")',
        1,
    )[1]
    assert strict_page_cursor_loop.index("cursor = load_ptr(node, 48)") < (
        strict_page_cursor_loop.index("examined = examined + 1")
    )
    for forbidden in ("pcc_thread_safepoint(", "malloc(", "free("):
        assert forbidden not in strict_page_batch

    strict_objects = strict_src.split(
        "def pcc_gc_select_relocation_set(", 1
    )[1].split('@c_abi_export("pcc_gc_backend4_select_relocation_pages")', 1)[0]
    assert strict_objects.index("pcc_current_thread_id()") < (
        strict_objects.index("pcc_py_gc_minor_graph_lock()")
    )
    assert "_best_relocation_page_batch(" in strict_objects
    assert "_select_page_objects_batch(" in strict_objects
    assert strict_objects.index("pcc_py_gc_minor_graph_unlock()") < (
        strict_objects.index("pcc_thread_safepoint()")
    )

    strict_pages = strict_src.split(
        "def pcc_gc_backend4_select_relocation_pages(", 1
    )[1]
    assert "object_budget = load_i64(page_token, 32)" in strict_pages
    assert "count_node = load_ptr(page_token, 112)" not in strict_pages
    assert "_best_relocation_page_batch(" in strict_pages
    assert "_select_page_objects_batch(" in strict_pages

    strict_state = (
        RUNTIME_DIR / "py" / "freestanding_gc_state.py"
    ).read_text(encoding="utf-8")
    for name in (
        "pcc_gc_backend4_selector_scan_cursor",
        "pcc_gc_backend4_selector_scan_best",
        "pcc_gc_backend4_selector_page_cursor",
        "pcc_gc_backend4_selector_page_seed",
    ):
        assert name in strict_state
    strict_lifecycle = (
        RUNTIME_DIR / "py" / "freestanding_gc_zpage_lifecycle.py"
    ).read_text(encoding="utf-8")
    assert strict_lifecycle.index(
        'global_load_ptr("pcc_gc_backend4_selector_scan_cursor")'
    ) < strict_lifecycle.index("store_ptr(node, 16, null())")
    assert strict_lifecycle.index(
        'global_load_ptr("pcc_gc_backend4_selector_page_cursor")'
    ) < strict_lifecycle.index("store_ptr(node, 48, null())")


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_colored_relocation_selector_polls_only_after_releasing_graph_lock(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="colored_relocation_selector_graph_unlock",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>

            static int64_t worker_ready;
            static int64_t worker_result;

            static void *selector_worker(void *arg) {
                (void)arg;
                __atomic_store_n(&worker_ready, 1, __ATOMIC_RELEASE);
                while (pcc_thread_stop_requested_acquire() == 0) {
                }
                worker_result = pcc_gc_select_relocation_set(16);
                return 0;
            }

            int main(void) {
                if (pcc_threads_enabled() != 1) return 2;
                if (pcc_current_thread_id() <= 0) return 3;
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 4;

                PyObject *objects[32];
                for (int i = 0; i < 32; i++) {
                    objects[i] = py_list_new(0);
                    if (objects[i] == 0) return 5;
                    if (
                        (py_header(objects[i])->flags
                            & PY_FLAG_GC_RELOCATION_CANDIDATE) != 0
                    ) return 6;
                }
                /* Allocations link at the global ZPage-list head.  These
                 * ineligible nodes therefore form a deterministic prefix in
                 * front of every valid candidate. */
                PyObject *pinned[32];
                for (int i = 0; i < 32; i++) {
                    pinned[i] = py_list_new(0);
                    if (pinned[i] == 0) return 15;
                    py_header(pinned[i])->flags |= PY_FLAG_GC_PINNED;
                }

                PccThreadHandle *worker = 0;
                if (pcc_thread_start(&worker, selector_worker, 0) != 0) {
                    return 7;
                }
                while (__atomic_load_n(&worker_ready, __ATOMIC_ACQUIRE) == 0) {
                }

                if (pcc_stop_the_world() != 0) return 8;
                if (pcc_thread_owns_stopped_world() != 1) return 9;

                /* The selector worker must park only after its graph-lock
                 * tenure.  The stopped-world owner must be able to acquire
                 * that exact public lock before resuming the worker. */
                PCC_TEST_GRAPH_LOCK();
                int selected_during_stop = 0;
                for (int i = 0; i < 32; i++) {
                    if (
                        (py_header(objects[i])->flags
                            & PY_FLAG_GC_RELOCATION_CANDIDATE) != 0
                    ) {
                        selected_during_stop++;
                    }
                }
                PCC_TEST_GRAPH_UNLOCK();
                if (selected_during_stop != 0) {
                    fprintf(
                        stderr,
                        "mid-stop selected=%d\n",
                        selected_during_stop
                    );
                    (void)pcc_resume_world();
                    return 10;
                }

                if (pcc_resume_world() != 0) return 11;
                void *thread_result = 0;
                if (pcc_thread_join(worker, &thread_result) != 0) return 12;
                if (thread_result != 0) return 13;
                if (worker_result != 16) return 14;
                for (int i = 0; i < 32; i++) {
                    if (
                        (py_header(pinned[i])->flags
                            & PY_FLAG_GC_RELOCATION_CANDIDATE) != 0
                    ) return 16;
                }
                return 0;
            }
        '''.replace(
            "PCC_TEST_GRAPH_LOCK()",
            "pcc_py_gc_minor_graph_lock()",
        ).replace(
            "PCC_TEST_GRAPH_UNLOCK()",
            "pcc_py_gc_minor_graph_unlock()",
        ),
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=10
    )
    assert run.returncode == 0, (
        f"{kind} colored-relocation selector probe returned {run.returncode}: "
        + run.stdout
        + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_colored_relocation_selector_preserves_per_owner_remembered_pressure(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="colored_relocation_selector_owner_remembered_pressure",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>

            int main(void) {
                if (pcc_threads_enabled() != 1) return 2;
                if (pcc_current_thread_id() <= 0) return 3;
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 4;

                PyObject *dirty = py_list_new(1);
                PyObject *clean = py_list_new(1);
                PyObject *young = py_list_new(0);
                if (dirty == 0 || clean == 0 || young == 0) return 5;
                if (
                    ((PyListObject *)dirty)->items == 0
                    || ((PyListObject *)clean)->items == 0
                ) return 6;
                py_header_flags_update(
                    py_header(dirty), PY_FLAG_GC_YOUNG, PY_FLAG_GC_OLD
                );
                py_header_flags_update(
                    py_header(clean), PY_FLAG_GC_YOUNG, PY_FLAG_GC_OLD
                );
                py_header_flags_or(
                    py_header(young), PY_FLAG_GC_PINNED
                );

                pcc_gc_store_ptr(
                    dirty, &((PyListObject *)dirty)->items[0], young
                );
                if (pcc_gc_backend4_zpage_remembered_slots() < 1) return 7;
                if (pcc_gc_select_relocation_set(1) != 1) return 8;
                if (pcc_gc_relocation_set_contains(dirty) != 1) return 9;
                if (pcc_gc_relocation_set_contains(clean) != 0) return 10;

                pcc_gc_reset_relocation_set();
                pcc_gc_store_ptr(
                    dirty, &((PyListObject *)dirty)->items[0], 0
                );
                py_decref(young);
                py_decref(clean);
                py_decref(dirty);
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=10
    )
    assert run.returncode == 0, (
        f"{kind} per-owner remembered-pressure probe returned "
        f"{run.returncode}: "
        + run.stdout
        + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_colored_relocation_page_selector_polls_only_after_releasing_graph_lock(
    tmp_path: Path,
    kind: str,
) -> None:
    source_text = r'''
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>

            extern int64_t pcc_gc_backend4_select_relocation_pages(int64_t);

            static int64_t worker_ready;
            static int64_t worker_result;

            static void *selector_worker(void *arg) {
                (void)arg;
                __atomic_store_n(&worker_ready, 1, __ATOMIC_RELEASE);
                while (pcc_thread_stop_requested_acquire() == 0) {
                }
                /* The C and strict page layouts fit this exact object batch
                 * into one and two physical pages respectively.  A two-page
                 * budget keeps the ownership assertion mode-independent. */
                worker_result = pcc_gc_backend4_select_relocation_pages(2);
                return 0;
            }

            int main(void) {
                if (pcc_threads_enabled() != 1) return 2;
                if (pcc_current_thread_id() <= 0) return 3;
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 4;

                PyObject *objects[32];
                for (int i = 0; i < 32; i++) {
                    objects[i] = py_list_new(0);
                    if (objects[i] == 0) return 5;
                }

                PccThreadHandle *worker = 0;
                if (pcc_thread_start(&worker, selector_worker, 0) != 0) {
                    return 6;
                }
                while (__atomic_load_n(&worker_ready, __ATOMIC_ACQUIRE) == 0) {
                }

                if (pcc_stop_the_world() != 0) return 7;
                if (pcc_thread_owns_stopped_world() != 1) return 8;
                PCC_TEST_GRAPH_LOCK();
                int selected_during_stop = 0;
                for (int i = 0; i < 32; i++) {
                    if (
                        (py_header(objects[i])->flags
                            & PY_FLAG_GC_RELOCATION_CANDIDATE) != 0
                    ) {
                        selected_during_stop++;
                    }
                }
                PCC_TEST_GRAPH_UNLOCK();
                /* The first stopped-world handoff now occurs after the
                 * bounded candidate-discovery chunk, before page commit. */
                if (selected_during_stop != 0) {
                    fprintf(
                        stderr,
                        "mid-stop page-selected=%d\n",
                        selected_during_stop
                    );
                    (void)pcc_resume_world();
                    return 9;
                }

                if (pcc_resume_world() != 0) return 10;
                void *thread_result = 0;
                if (pcc_thread_join(worker, &thread_result) != 0) return 11;
                if (thread_result != 0) return 12;
                if (worker_result != 32) {
                    fprintf(
                        stderr,
                        "page selector result=%lld\n",
                        (long long)worker_result
                    );
                    return 13;
                }
                for (int i = 0; i < 32; i++) {
                    if (
                        (py_header(objects[i])->flags
                            & PY_FLAG_GC_RELOCATION_CANDIDATE) == 0
                    ) return 14;
                }
                return 0;
            }
        '''.replace(
        "PCC_TEST_GRAPH_LOCK()",
        "pcc_py_gc_minor_graph_lock()",
    ).replace(
        "PCC_TEST_GRAPH_UNLOCK()",
        "pcc_py_gc_minor_graph_unlock()",
    )
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="colored_relocation_page_selector_graph_unlock",
        source_text=source_text,
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=10
    )
    assert run.returncode == 0, (
        f"{kind} colored-relocation page selector probe returned "
        f"{run.returncode}: "
        + run.stdout
        + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_colored_relocation_object_drain_polls_only_after_releasing_graph_lock(
    tmp_path: Path,
    kind: str,
) -> None:
    source_text = r'''
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>

            extern int64_t pcc_gc_backend4_select_relocation_pages(int64_t);

            static int64_t worker_ready;
            static int64_t worker_result;

            static void *drain_worker(void *arg) {
                (void)arg;
                __atomic_store_n(&worker_ready, 1, __ATOMIC_RELEASE);
                while (pcc_thread_stop_requested_acquire() == 0) {
                }
                worker_result = pcc_gc_backend4_evacuation_drain(32);
                return 0;
            }

            int main(void) {
                if (pcc_threads_enabled() != 1) return 2;
                if (pcc_current_thread_id() <= 0) return 3;
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 4;

                PyObject *objects[32];
                for (int i = 0; i < 32; i++) {
                    objects[i] = py_list_new(0);
                    if (objects[i] == 0) return 5;
                }
                if (pcc_gc_backend4_select_relocation_pages(2) != 32) {
                    return 6;
                }
                if (pcc_gc_relocation_set_size() != 32) return 7;

                PccThreadHandle *worker = 0;
                if (pcc_thread_start(&worker, drain_worker, 0) != 0) {
                    return 8;
                }
                while (__atomic_load_n(&worker_ready, __ATOMIC_ACQUIRE) == 0) {
                }

                if (pcc_stop_the_world() != 0) return 9;
                if (pcc_thread_owns_stopped_world() != 1) return 10;

                /* The first destination allocation is the earliest real
                 * safepoint in the object-drain transaction.  It must occur
                 * after source snapshot and graph unlock, before any
                 * candidate is consumed. */
                PCC_TEST_GRAPH_LOCK();
                int64_t remaining = pcc_gc_relocation_set_size();
                PCC_TEST_GRAPH_UNLOCK();
                if (remaining != 32) {
                    fprintf(
                        stderr,
                        "mid-stop relocation remaining=%lld\n",
                        (long long)remaining
                    );
                    (void)pcc_resume_world();
                    return 11;
                }

                if (pcc_resume_world() != 0) return 12;
                void *thread_result = 0;
                if (pcc_thread_join(worker, &thread_result) != 0) return 13;
                if (thread_result != 0) return 14;
                if (worker_result != 32) return 15;
                if (pcc_gc_relocation_set_size() != 0) return 16;
                if (
                    pcc_gc_telemetry(PCC_GC_COUNTER_FORWARDING_ENTRIES) != 32
                ) return 17;
                return 0;
            }
        '''.replace(
        "PCC_TEST_GRAPH_LOCK()",
        "pcc_py_gc_minor_graph_lock()",
    ).replace(
        "PCC_TEST_GRAPH_UNLOCK()",
        "pcc_py_gc_minor_graph_unlock()",
    )
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="colored_relocation_object_drain_graph_unlock",
        source_text=source_text,
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=10
    )
    assert run.returncode == 0, (
        f"{kind} colored-relocation object-drain probe returned "
        f"{run.returncode}: "
        + run.stdout
        + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_colored_relocation_page_drain_polls_only_after_releasing_graph_lock(
    tmp_path: Path,
    kind: str,
) -> None:
    source_text = r'''
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>

            extern int64_t pcc_gc_backend4_select_relocation_pages(int64_t);

            static int64_t worker_ready;
            static int64_t worker_result;

            static void *drain_worker(void *arg) {
                (void)arg;
                __atomic_store_n(&worker_ready, 1, __ATOMIC_RELEASE);
                while (pcc_thread_stop_requested_acquire() == 0) {
                }
                worker_result = pcc_gc_backend4_evacuation_page_drain(2);
                return 0;
            }

            int main(void) {
                if (pcc_threads_enabled() != 1) return 2;
                if (pcc_current_thread_id() <= 0) return 3;
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 4;

                PyObject *objects[32];
                for (int i = 0; i < 32; i++) {
                    objects[i] = py_list_new(0);
                    if (objects[i] == 0) return 5;
                }
                if (pcc_gc_backend4_select_relocation_pages(2) != 32) {
                    return 6;
                }
                if (pcc_gc_relocation_set_size() != 32) return 7;

                PccThreadHandle *worker = 0;
                if (pcc_thread_start(&worker, drain_worker, 0) != 0) {
                    return 8;
                }
                while (__atomic_load_n(&worker_ready, __ATOMIC_ACQUIRE) == 0) {
                }

                if (pcc_stop_the_world() != 0) return 9;
                if (pcc_thread_owns_stopped_world() != 1) return 10;

                /* The first destination allocation is the earliest real
                 * safepoint in the copy transaction.  It must occur after
                 * source snapshot and graph unlock, before any candidate is
                 * consumed. */
                PCC_TEST_GRAPH_LOCK();
                int64_t remaining = pcc_gc_relocation_set_size();
                PCC_TEST_GRAPH_UNLOCK();
                if (remaining != 32) {
                    fprintf(
                        stderr,
                        "mid-stop relocation remaining=%lld\n",
                        (long long)remaining
                    );
                    (void)pcc_resume_world();
                    return 11;
                }

                if (pcc_resume_world() != 0) return 12;
                void *thread_result = 0;
                if (pcc_thread_join(worker, &thread_result) != 0) return 13;
                if (thread_result != 0) return 14;
                if (worker_result != 32) return 15;
                if (pcc_gc_relocation_set_size() != 0) return 16;
                if (
                    pcc_gc_telemetry(PCC_GC_COUNTER_FORWARDING_ENTRIES) != 32
                ) return 17;
                return 0;
            }
        '''.replace(
        "PCC_TEST_GRAPH_LOCK()",
        "pcc_py_gc_minor_graph_lock()",
    ).replace(
        "PCC_TEST_GRAPH_UNLOCK()",
        "pcc_py_gc_minor_graph_unlock()",
    )
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="colored_relocation_page_drain_graph_unlock",
        source_text=source_text,
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=10
    )
    assert run.returncode == 0, (
        f"{kind} colored-relocation page-drain probe returned "
        f"{run.returncode}: "
        + run.stdout
        + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_colored_generation_aging_worklist_survives_trackable_backend_switch(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="colored_generation_aging_trackable_switch",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>

            int main(void) {
                if (pcc_threads_enabled() != 1) return 2;
                if (pcc_current_thread_id() <= 0) return 3;
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 4;

                PyObject *young = py_list_new(0);
                if (young == 0) return 5;
                if (
                    (py_header(young)->flags & PY_FLAG_GC_YOUNG) == 0
                    || (py_header(young)->flags & PY_FLAG_GC_OLD) != 0
                ) return 6;
                int64_t promotions_before =
                    pcc_gc_backend4_generation_promotion_score();

                if (pcc_gc_set_backend(
                        PCC_GC_KIND_INCREMENTAL_TRICOLOR
                    ) != 0) return 7;
                PyObject *cross_backend_young = pcc_gc_alloc(
                    64, PY_TYPE_LIST, PY_FLAG_GC_YOUNG
                );
                if (cross_backend_young == 0) return 8;
                if (
                    (py_header(cross_backend_young)->flags & PY_FLAG_GC_YOUNG)
                        == 0
                    || (py_header(cross_backend_young)->flags & PY_FLAG_GC_OLD)
                        != 0
                ) return 9;
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 10;

                if (pcc_gc_step(2) != 2) return 11;
                if (
                    pcc_gc_backend4_generation_promotion_score()
                        - promotions_before
                    != 2
                ) return 12;
                if (
                    (py_header(young)->flags & PY_FLAG_GC_YOUNG) != 0
                    || (py_header(young)->flags & PY_FLAG_GC_OLD) == 0
                    || (
                        py_header(cross_backend_young)->flags
                        & PY_FLAG_GC_YOUNG
                    ) != 0
                    || (
                        py_header(cross_backend_young)->flags
                        & PY_FLAG_GC_OLD
                    ) == 0
                ) return 13;
                py_decref(young);
                pcc_gc_release(cross_backend_young);
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=10
    )
    assert run.returncode == 0, (
        f"{kind} colored-generation switch probe returned {run.returncode}: "
        + run.stdout
        + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_colored_generation_aging_counts_examined_work_not_only_promotions(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="colored_generation_aging_examined_work",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>

            int main(void) {
                if (pcc_threads_enabled() != 1) return 2;
                if (pcc_current_thread_id() <= 0) return 3;
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 4;

                int64_t young_pages_before =
                    pcc_gc_backend4_zpage_young_pages();
                int64_t old_pages_before =
                    pcc_gc_backend4_zpage_old_pages();
                int64_t promotions_before =
                    pcc_gc_backend4_generation_promotion_score();
                PyObject *objects[40];
                for (int i = 0; i < 40; i++) {
                    objects[i] = pcc_gc_alloc(4096, PY_TYPE_LIST, 0);
                    if (objects[i] == 0) return 5;
                }
                PyObject *freed = pcc_gc_alloc(4096, PY_TYPE_LIST, 0);
                PyObject *explicit_old = pcc_gc_alloc(
                    4096, PY_TYPE_LIST, PY_FLAG_GC_OLD
                );
                if (freed == 0 || explicit_old == 0) return 6;
                pcc_gc_release(freed);
                if (
                    pcc_gc_backend4_zpage_young_pages() - young_pages_before
                        != 40
                    || pcc_gc_backend4_zpage_old_pages() - old_pages_before
                        != 1
                ) return 7;

                /* Two entries become non-young after publication.  They are
                 * stale maintenance work: each consumes budget exactly once
                 * but neither increments promotion telemetry. */
                py_header_flags_update(
                    py_header(objects[0]), PY_FLAG_GC_YOUNG, PY_FLAG_GC_OLD
                );
                py_header_flags_update(
                    py_header(objects[1]), PY_FLAG_GC_YOUNG, PY_FLAG_GC_OLD
                );
                py_header_flags_or(
                    py_header(objects[2]), PY_FLAG_GC_PINNED
                );

                if (pcc_gc_step(40) != 40) return 8;
                if (
                    pcc_gc_backend4_generation_promotion_score()
                        - promotions_before
                    != 38
                ) return 9;
                if (
                    pcc_gc_backend4_zpage_young_pages() - young_pages_before
                        != 2
                    || pcc_gc_backend4_zpage_old_pages() - old_pages_before
                        != 39
                ) return 10;
                if (
                    (py_header(objects[2])->flags & PY_FLAG_GC_PINNED) == 0
                ) return 11;
                for (int i = 0; i < 40; i++) {
                    int32_t flags = py_header(objects[i])->flags;
                    if (
                        (flags & PY_FLAG_GC_YOUNG) != 0
                        || (flags & PY_FLAG_GC_OLD) == 0
                    ) return 12;
                    pcc_gc_release(objects[i]);
                }
                if (
                    (py_header(explicit_old)->flags & PY_FLAG_GC_YOUNG) != 0
                    || (py_header(explicit_old)->flags & PY_FLAG_GC_OLD) == 0
                ) return 13;
                pcc_gc_release(explicit_old);
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=10
    )
    assert run.returncode == 0, (
        f"{kind} colored-generation examined-work probe returned {run.returncode}: "
        + run.stdout
        + run.stderr
    )


def test_colored_generation_aging_has_bounded_c_and_strict_graph_tenures():

    strict_src = PY_GC_BACKEND_PORT.read_text(encoding="utf-8")
    assert '@c_abi_export("pcc_gc_backend4_step_generation_aging")' not in strict_src
    strict_scheduler = PY_GC_GENERATIONAL_SCHEDULER.read_text(encoding="utf-8")
    assert "__pcc_freestanding__ = True" in strict_scheduler
    strict_body = strict_scheduler.split(
        '@c_abi_export("pcc_gc_backend4_step_generation_aging")', 1
    )[1]
    assert "while total_examined < remaining_budget:" in strict_body
    assert "if batch_limit > 16:" in strict_body
    assert "node = pcc_gc_backend3_young_list_head()" in strict_body
    strict_locked = strict_body.split("pcc_py_gc_minor_graph_lock()", 1)[1].split(
        "pcc_py_gc_minor_graph_unlock()", 1
    )[0]
    assert strict_locked.index("pcc_gc_backend3_young_unlink(node)") < (
        strict_locked.index("batch_examined = batch_examined + 1")
    ) < strict_locked.index("total_examined = total_examined + 1")
    assert "atomic_load_i32(obj, 12, \"acquire\")" in strict_locked
    assert '(flags & 128) == 0 or (flags & 256) != 0' in strict_locked
    assert 'atomic_rmw_i32("add", obj, 12, 128, "acq_rel")' in strict_locked
    assert "atomic_cas_i32(" not in strict_locked
    assert "pcc_gc_forwarding_find(" not in strict_locked
    assert "zpage_node = load_ptr(node, 48)" in strict_locked
    assert "store_i32(page, 28, 2)" in strict_locked
    assert "_backend4_zpage_note_owner_promoted" not in strict_locked
    assert strict_locked.count("atomic_rmw_i32(") == 2
    assert 'global_addr("pcc_gc_backend4_young_promotions")' in strict_locked
    for forbidden in (
        "pcc_thread_safepoint(",
        "pcc_py_gc_minor_graph_lock(",
        "pcc_stop_the_world(",
        "pcc_diagnostics_runtime_log",
        "malloc(",
        "free(",
        "py_decref(",
    ):
        assert forbidden not in strict_locked
    strict_after_unlock = strict_body.split(
        "pcc_py_gc_minor_graph_unlock()", 1
    )[1]
    assert "pcc_thread_safepoint()" in strict_after_unlock
    assert strict_body.count("pcc_thread_safepoint()") == 1

    strict_setter = strict_src.split(
        '@c_abi_export("pcc_gc_set_backend")', 1
    )[1].split("\n@c_abi_export", 1)[0]
    assert "_backend3_young_rebuild()" not in strict_setter
    assert "_set_backend3_young_head(null())" not in strict_setter
    strict_alloc = strict_src.split(
        '@c_abi_export("pcc_gc_note_object_allocated_sized")', 1
    )[1].split('\n@c_abi_export("pcc_gc_note_object_allocated")', 1)[0]
    assert "final_generation == 128" in strict_alloc
    strict_link = strict_alloc.split("final_generation: int", 1)[1].split(
        "_backend3_young_link_head(node)", 1
    )[0]
    assert "backend" not in strict_link
    assert strict_alloc.index("final_generation == 128") < strict_alloc.index(
        "_backend3_young_link_head(node)"
    )

    pipeline_src = (
        REPO_ROOT / "pcc" / "frontends" / "python" / "pipeline.py"
    ).read_text(encoding="utf-8")
    freestanding_config = pipeline_src.split("if freestanding_module:", 1)[
        1
    ].split("if python_library:", 1)[0]
    assert "codegen._thread_safepoints_enabled = False" in freestanding_config


def test_generational_owner_referent_promotion_uses_bounded_logical_slot_worklist():

    strict_slots = PY_GC_OBJECT_SLOTS.read_text(encoding="utf-8")
    strict_promotion = PY_GC_GENERATIONAL_PROMOTION.read_text(encoding="utf-8")
    strict_drain = strict_promotion.split(
        '@c_abi_export("pcc_gc_backend3_drain_promotion_worklist")', 1
    )[1].split("\n@c_abi_export", 1)[0]
    assert '@c_abi_export("pcc_gc_visit_object_slots_slice")' in strict_slots
    strict_slice = strict_slots.split(
        '@c_abi_export("pcc_gc_visit_object_slots_slice")', 1
    )[1].split('@c_abi_export("pcc_gc_visit_object_slots")', 1)[0]
    for c_tag, strict_tag in (
        ("PY_TYPE_LIST", "object.type.list"),
        ("PY_TYPE_TUPLE", "object.type.tuple"),
        ("PY_TYPE_DICT", "object.type.dict"),
        ("PY_TYPE_SET", "object.type.set"),
        ("PY_TYPE_VTHREAD_CHANNEL", "object.type.vthread_channel"),
        ("PY_TYPE_FUNC", "object.type.func"),
        ("PY_TYPE_ITER", "object.type.iter"),
        ("PY_TYPE_GEN", "object.type.gen"),
        ("PY_TYPE_COROUTINE", "object.type.coroutine"),
        ("PY_TYPE_TASK", "object.type.task"),
        ("PY_TYPE_VIRTUAL_THREAD", "object.type.virtual_thread"),
        ("PY_TYPE_EXC", "object.type.exc"),
        ("PY_TYPE_PROPERTY", "object.type.property"),
        ("PY_TYPE_CLASSMETHOD", "object.type.classmethod"),
        ("PY_TYPE_STATICMETHOD", "object.type.staticmethod"),
        ("PY_TYPE_MEMORYVIEW", "object.type.memoryview"),
        ("PY_TYPE_THREAD", "object.type.thread"),
        ("PY_TYPE_WEAKREF", "object.type.weakref"),
        ("PY_TYPE_CONTINUATION", "object.type.continuation"),
        ("PY_TYPE_CLASS", "object.type.class"),
        ("PY_TYPE_INSTANCE", "object.type.instance"),
        ("PY_TYPE_VALUEBOX", "object.type.valuebox"),
        ("PY_TYPE_USER_CLASS_START", "object.type.user_class_start"),
    ):
        assert f'abi_constant("{strict_tag}")' in strict_slice
    assert "examined < limit" in strict_slice
    assert "store_i64(state_out, 0" in strict_slice
    strict_full = strict_slots.split(
        '@c_abi_export("pcc_gc_visit_object_slots")', 1
    )[1]
    assert "pcc_gc_visit_object_slots_slice(" in strict_full
    assert "if batch_limit > 16:" in strict_drain
    assert "pcc_gc_visit_object_slots_slice" in strict_drain
    assert "load_i64(node, 56)" in strict_drain
    assert "store_i64(node, 56, next_cursor)" in strict_drain
    strict_nodes = (
        REPO_ROOT
        / "pcc" / "runtime"
        / "py"
        / "freestanding_gc_object_nodes.py"
    ).read_text(encoding="utf-8")
    assert "malloc(80)" in strict_nodes
    assert "malloc(120)" not in strict_nodes
    assert "pcc_py_gc_minor_graph_lock()" in strict_drain
    assert "pcc_py_gc_minor_graph_unlock()" in strict_drain
    assert strict_drain.index("pcc_py_gc_minor_graph_unlock()") < (
        strict_drain.index("pcc_thread_safepoint()")
    )
    assert strict_drain.index("pcc_py_gc_minor_graph_unlock()") < (
        strict_drain.index("_promote_cext_owner_referents(callback_owner)")
    )
    for forbidden in (
        "pcc_gc_visit_object_slots(",
        "malloc(",
        "free(",
    ):
        assert forbidden not in strict_drain

    assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES[
        "pcc_gc_visit_object_slots_slice"
    ] == (
        (
            "c_ptr",
            "c_int64",
            "c_int64",
            "c_ptr",
            "c_ptr",
            "c_ptr",
        ),
        "c_int64",
    )


def test_incremental_trace_cext_claim_unlocks_callback_and_revalidates_commit():

    strict_scheduler = PY_GC_INCREMENTAL_CONCURRENT_SCHEDULER.read_text(
        encoding="utf-8"
    )
    strict_step = strict_scheduler.split(
        'def pcc_gc_tracing_step_cycle(remaining_budget: i64)', 1
    )[1].split("\n@c_abi_export", 1)[0]
    # The cext trace no longer runs inside the step cycle: the step hands off a
    # pending object and returns, and the completion runs in its own exported
    # helper.  Assert the unlock-before-unlocked-trace property where it now
    # lives, in all three places, rather than where it used to be.
    #
    # 1. The step cycle must not hand off while still holding the graph lock.
    pending_at = strict_step.index(
        'if ptr_is_null(global_load_ptr("pcc_gc_trace_cext_pending_obj")) == 0:'
    )
    assert strict_step.index(
        "pcc_py_gc_minor_graph_unlock()", pending_at
    ) < strict_step.index("return local_processed", pending_at)

    # 2. The completion helper runs the callback before taking the lock.
    strict_complete = strict_scheduler.split(
        "def _pcc_gc_trace_cext_complete_context(cext_ctx) -> i64:", 1
    )[1].split("\n@c_abi_export", 1)[0]
    assert strict_complete.index("pcc_gc_trace_cext_referents_unlocked(") < (
        strict_complete.index("pcc_py_gc_minor_graph_lock()")
    )

    # 3. Its stopped-world caller releases the lock before invoking it.
    strict_stw = strict_scheduler.split(
        "def _pcc_gc_drain_all_gray_stopped_world(", 1
    )[1].split("\n@c_abi_export", 1)[0]
    assert strict_stw.index("pcc_py_gc_minor_graph_unlock()") < (
        strict_stw.index("_pcc_gc_trace_cext_complete_context(")
    )

    # The step cycle still owns the pending-object handoff state.
    for token in (
        'global_load_ptr("pcc_gc_trace_cext_pending_obj")',
        'global_addr("pcc_gc_trace_cext_pending_epoch")',
        'global_addr("pcc_gc_trace_cext_pending_backend")',
        "pcc_gc_gray_count_decrement_acq_rel()",
    ):
        assert token in strict_step
    # The colour/flag commit moved with the callback into the completion
    # helper, so assert it there rather than dropping it.
    for token in (
        # GRAY -> BLACK through the header-preserving CAS (cd49ce610).
        "pcc_gc_set_mark_color(cext_obj, 32)",
        "pcc_gc_gray_count_decrement_acq_rel()",
        'global_store_ptr("pcc_gc_trace_cext_pending_obj", null())',
    ):
        assert token in strict_complete


def test_final_and_cms_whole_gray_cext_callbacks_split_graph_lock():

    strict_common = PY_GC_COMMON_MARK_CYCLE.read_text(encoding="utf-8")
    strict_slice = strict_common.split(
        '@c_abi_export("pcc_gc_drain_all_gray_locked_slice")', 1
    )[1].split('@c_abi_export("pcc_gc_begin_mark_cycle")', 1)[0]
    assert "pcc_capi_is_cext_type_tag" in strict_slice
    assert "global_store_ptr(" in strict_slice
    assert '"pcc_gc_trace_cext_pending_obj", obj' in strict_slice
    strict_finish = strict_common.split(
        '@c_abi_export("pcc_gc_finish_tracing_cycle")', 1
    )[1]
    assert "pcc_gc_drain_all_gray" not in strict_finish

    strict_scheduler = PY_GC_INCREMENTAL_CONCURRENT_SCHEDULER.read_text(
        encoding="utf-8"
    )
    strict_stw = strict_scheduler.split(
        "def _pcc_gc_drain_all_gray_stopped_world(", 1
    )[1].split('@c_abi_export("pcc_gc_complete_claimed_tracing_cycle")', 1)[0]
    assert strict_stw.index("pcc_py_gc_minor_graph_unlock()") < (
        strict_stw.index("_pcc_gc_trace_cext_complete_context(cext_ctx)")
    )
    strict_complete = strict_scheduler.split(
        '@c_abi_export("pcc_gc_complete_claimed_tracing_cycle")', 1
    )[1].split('@c_abi_export("pcc_gc_tracing_debt_threshold")', 1)[0]
    strict_roots = strict_complete.index("pcc_gc_gray_current_roots()")
    assert strict_roots < strict_complete.index(
        "pcc_py_gc_minor_graph_unlock()", strict_roots
    ) < strict_complete.index("_pcc_gc_drain_all_gray_stopped_world(")


def test_initial_refcount_seed_claims_stw_before_cext_callback():

    strict_common = PY_GC_COMMON_MARK_CYCLE.read_text(encoding="utf-8")
    strict_claim = strict_common.split(
        '@c_abi_export("pcc_gc_begin_mark_cycle")', 1
    )[1].split('@c_abi_export("pcc_gc_tracing_cycle_epoch_advance_unlocked")', 1)[0]
    assert 'global_addr("pcc_gc_trace_extension_roots_pending")' in strict_claim
    assert ", 0, 4" in strict_claim
    assert "pcc_gc_seed_roots()" not in strict_claim
    strict_scheduler = PY_GC_INCREMENTAL_CONCURRENT_SCHEDULER.read_text(
        encoding="utf-8"
    )
    strict_complete = strict_scheduler.split(
        "def _pcc_gc_complete_mark_cycle_seed(", 1
    )[1].split('@c_abi_export("pcc_gc_complete_claimed_tracing_cycle")', 1)[0]
    assert strict_complete.index("pcc_stop_the_world()") < strict_complete.index(
        "pcc_gc_seed_roots()"
    )
    seed_call = strict_complete.index("pcc_gc_seed_roots()")
    assert strict_complete.rindex(
        "pcc_py_gc_minor_graph_unlock()", 0, seed_call
    ) < seed_call < strict_complete.index(
        'store_i32(global_addr("pcc_gc_mark_active"), 0, 1)'
    )
    strict_backend = PY_GC_BACKEND_PORT.read_text(encoding="utf-8")
    strict_setter = strict_backend.split(
        '@c_abi_export("pcc_gc_set_backend")', 1
    )[1].split("\n@c_abi_export", 1)[0]
    pending_guard = strict_setter.index(
        'global_addr("pcc_gc_trace_extension_roots_pending")'
    )
    assert pending_guard < strict_setter.index(
        "_tracing_cycle_epoch_advance_unlocked()"
    )


def test_backend4_remap_cext_prepass_owns_stw_and_revalidates_commit():

    strict_state = PY_GC_STATE.read_text(encoding="utf-8")
    for name in (
        "pcc_gc_backend4_remap_active",
        "pcc_gc_backend4_remap_epoch",
        "pcc_gc_backend4_remap_pending_obj",
    ):
        assert name in strict_state
    strict_retirement = (
        RUNTIME_DIR / "py" / "freestanding_gc_forwarding_retirement.py"
    ).read_text(encoding="utf-8")
    strict_wrapper = strict_retirement.split(
        '@c_abi_export("pcc_gc_backend4_remap_and_retire_stopped_world")', 1
    )[1].split(
        '@c_abi_export("pcc_gc_backend4_remap_and_retire_unlocked")', 1
    )[0]
    assert strict_wrapper.index("pcc_stop_the_world()") < strict_wrapper.index(
        'global_store_ptr("pcc_gc_backend4_remap_pending_obj", obj)'
    )
    assert strict_wrapper.index("pcc_py_gc_minor_graph_unlock()") < (
        strict_wrapper.index("pcc_gc_backend4_remap_cext_referents_unlocked(")
    )
    assert strict_wrapper.index(
        "pcc_gc_backend4_remap_cext_referents_unlocked("
    ) < strict_wrapper.index("pcc_gc_backend4_remap_and_retire_unlocked(")


def test_graph_lock_outermost_ownership_is_one_no_park_lease():

    strict = (
        RUNTIME_DIR / "py" / "freestanding_runtime_high_substrate.py"
    ).read_text(encoding="utf-8")
    strict_lock = strict.split("def pcc_py_gc_minor_graph_lock() -> None:", 1)[
        1
    ].split('@c_abi_export("pcc_py_gc_minor_graph_unlock")', 1)[0]
    strict_cas = strict_lock.index("atomic_cas_i32(")
    strict_registration = strict_lock.index("pcc_current_thread_id()")
    strict_enter = strict_lock.index("pcc_thread_no_park_enter()")
    strict_depth = strict_lock.index("store_i32(depth_slot, 0, 1)")
    assert strict_registration < strict_cas < strict_enter < strict_depth
    strict_unlock = strict.split(
        "def pcc_py_gc_minor_graph_unlock() -> None:", 1
    )[1]
    strict_physical = strict_unlock.index("atomic_store_i32(")
    strict_deferred = strict_unlock.index(
        "_finish_deferred_tripwire()", strict_physical
    )
    strict_exit = strict_unlock.index(
        "pcc_thread_no_park_exit()", strict_deferred
    )
    assert strict_physical < strict_deferred < strict_exit
