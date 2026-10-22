"""Backend-4 constructor publication, rehash/list-growth raw-slot retargeting, worklists and owner-wide barriers.

Split from ``test_gc_threading_substrate.py``; collected only through
that facade so pytest node ids stay stable.
"""
from _gc_substrate_common import *  # noqa: F401,F403


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_capi_borrowed_items_are_lifetime_pinned(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_capi_borrowed_item_pin",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>

            extern PyObject *PyTuple_GetItem(PyObject *, int64_t);
            extern PyObject *PyList_GetItem(PyObject *, int64_t);
            extern PyObject *PyList_GetItemRef(PyObject *, int64_t);
            extern PyObject *PyDict_GetItem(PyObject *, PyObject *);
            extern PyObject *PyDict_GetItemWithError(PyObject *, PyObject *);

            static int rejects_relocation(PyObject *obj) {
                pcc_gc_reset_relocation_set();
                return pcc_gc_backend4_relocation_set_add(obj) == 0;
            }

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 2;
                PyObject *values[4] = {
                    py_list_new(0), py_list_new(0),
                    py_list_new(0), py_list_new(0)
                };
                PyObject *list = py_list_new(0);
                PyObject *tuple = py_tuple_new(1);
                PyObject *dict = py_dict_new();
                PyObject *key = py_int_from_i64(7);
                if (
                    values[0] == NULL || values[1] == NULL
                    || values[2] == NULL || values[3] == NULL
                    || list == NULL || tuple == NULL || dict == NULL
                ) return 3;
                py_list_append(list, values[0]);
                py_tuple_set_item(tuple, 0, values[1]);
                py_dict_set(dict, key, values[2]);

                if (PyList_GetItem(list, 0) != values[0]) return 4;
                if (PyTuple_GetItem(tuple, 0) != values[1]) return 5;
                if (PyDict_GetItem(dict, key) != values[2]) return 6;
                if (PyDict_GetItemWithError(dict, key) != values[2]) return 7;
                for (int i = 0; i < 3; i++) {
                    if ((py_header(values[i])->flags & PY_FLAG_GC_PINNED) == 0) {
                        return 8 + i;
                    }
                    if (!rejects_relocation(values[i])) return 11 + i;
                }

                PyObject *owned_list = py_list_new(0);
                if (owned_list == NULL) return 14;
                py_list_append(owned_list, values[3]);
                PyObject *owned = PyList_GetItemRef(owned_list, 0);
                if (owned != values[3]) return 15;
                if ((py_header(values[3])->flags & PY_FLAG_GC_PINNED) != 0) {
                    return 16;
                }
                if (pcc_gc_backend4_relocation_set_add(values[3]) != 1) return 17;
                pcc_gc_reset_relocation_set();
                py_decref(owned);

                py_decref(owned_list);
                py_decref(dict);
                py_decref(tuple);
                py_decref(list);
                for (int i = 0; i < 4; i++) py_decref(values[i]);
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} C-API borrowed-item pin probe returned {run.returncode}: "
        + run.stdout + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_container_constructor_publication_excludes_partial_objects(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_container_constructor_publication",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>

            static int add_after_reset(PyObject *obj) {
                pcc_gc_reset_relocation_set();
                return (int)pcc_gc_backend4_relocation_set_add(obj);
            }

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 2;

                PyObject *raw = pcc_gc_alloc(
                    (int64_t)sizeof(PyListObject), PY_TYPE_LIST, 0
                );
                if (raw == NULL) return 3;
                if ((py_header(raw)->flags & PY_FLAG_GC_FRESH_ALLOC) == 0) {
                    return 4;
                }
                if (add_after_reset(raw) != 0) return 5;
                pcc_gc_publish_initialized(raw);
                if ((py_header(raw)->flags & PY_FLAG_GC_FRESH_ALLOC) != 0) {
                    return 6;
                }
                if (add_after_reset(raw) != 1) return 7;
                pcc_gc_reset_relocation_set();
                py_decref(raw);

                PyObject *partial = py_tuple_new(2);
                if (partial == NULL) return 8;
                if ((py_header(partial)->flags & PY_FLAG_GC_FRESH_ALLOC) == 0) {
                    return 9;
                }
                if (add_after_reset(partial) != 0) return 10;
                py_tuple_set_item(partial, 0, py_int_from_i64(11));
                if ((py_header(partial)->flags & PY_FLAG_GC_FRESH_ALLOC) == 0) {
                    return 11;
                }
                if (add_after_reset(partial) != 0) return 12;
                py_tuple_set_item(partial, 1, py_int_from_i64(12));
                if ((py_header(partial)->flags & PY_FLAG_GC_FRESH_ALLOC) != 0) {
                    return 13;
                }
                if (add_after_reset(partial) != 1) return 14;
                pcc_gc_reset_relocation_set();

                PyObject *objects[4] = {
                    py_list_new(0), py_dict_new(), py_set_new(), py_tuple_new(0)
                };
                for (int i = 0; i < 4; i++) {
                    if (objects[i] == NULL) return 20 + i;
                    if (
                        (py_header(objects[i])->flags
                            & PY_FLAG_GC_FRESH_ALLOC) != 0
                    ) return 30 + i;
                    if (add_after_reset(objects[i]) != 1) return 40 + i;
                    pcc_gc_reset_relocation_set();
                }

                py_decref(partial);
                for (int i = 0; i < 4; i++) py_decref(objects[i]);
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} container constructor publication probe returned "
        f"{run.returncode}: " + run.stdout + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_wrapper_constructor_publication_excludes_partial_objects(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_wrapper_constructor_publication",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>

            static int add_after_reset(PyObject *obj) {
                pcc_gc_reset_relocation_set();
                return (int)pcc_gc_backend4_relocation_set_add(obj);
            }

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 2;
                int32_t tags[3] = {
                    PY_TYPE_PROPERTY, PY_TYPE_CLASSMETHOD, PY_TYPE_WEAKREF
                };
                int64_t sizes[3] = {
                    (int64_t)sizeof(PyPropertyObject),
                    (int64_t)sizeof(PyClassMethodObject),
                    (int64_t)sizeof(PyWeakRefObject)
                };
                for (int i = 0; i < 3; i++) {
                    PyObject *raw = pcc_gc_alloc(sizes[i], tags[i], 0);
                    if (raw == NULL) return 3 + i;
                    if (
                        (py_header(raw)->flags & PY_FLAG_GC_FRESH_ALLOC) == 0
                    ) return 6 + i;
                    if (add_after_reset(raw) != 0) return 9 + i;
                    pcc_gc_publish_initialized(raw);
                    if (
                        (py_header(raw)->flags & PY_FLAG_GC_FRESH_ALLOC) != 0
                    ) return 12 + i;
                    if (add_after_reset(raw) != 1) return 15 + i;
                    pcc_gc_reset_relocation_set();
                    py_decref(raw);
                }

                PyObject *func = py_list_new(0);
                PyObject *target = py_list_new(0);
                if (func == NULL || target == NULL) return 20;
                PyObject *objects[3] = {
                    py_property_new(func, py_None, py_None),
                    py_classmethod_new(func),
                    py_weakref_new(target, py_None)
                };
                for (int i = 0; i < 3; i++) {
                    if (objects[i] == NULL) return 21 + i;
                    if (
                        (py_header(objects[i])->flags
                            & PY_FLAG_GC_FRESH_ALLOC) != 0
                    ) return 24 + i;
                    if (add_after_reset(objects[i]) != 1) return 27 + i;
                    pcc_gc_reset_relocation_set();
                }
                py_decref(objects[2]);
                py_decref(objects[1]);
                py_decref(objects[0]);
                py_decref(target);
                py_decref(func);
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} wrapper constructor publication probe returned "
        f"{run.returncode}: " + run.stdout + run.stderr
    )


# Was @pytest.mark.skip(reason="strict GC4 FUNC/ITER allocation blocker").
# The reason was wrong: this fails on BOTH arms (rc=4), not just strict, and
# the cause is a real gap -- pcc_gc_alloc grants PY_FLAG_GC_FRESH_ALLOC only
# to the seven tags listed in py_obj.c, while backend 4 will relocate ITER.
# Tracked as GC-P1-BACKEND4-RELOCATABLE-TAGS-LACK-FRESH-ALLOC; left visible
# rather than skipped, per the repo's run-or-deselect rule.
@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_iterator_constructor_publication_excludes_partial_objects(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_iterator_constructor_publication",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>

            static int add_after_reset(PyObject *obj) {
                pcc_gc_reset_relocation_set();
                return (int)pcc_gc_backend4_relocation_set_add(obj);
            }

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 2;
                PyObject *raw = pcc_gc_alloc(32, PY_TYPE_ITER, 0);
                if (raw == NULL) return 3;
                if ((py_header(raw)->flags & PY_FLAG_GC_FRESH_ALLOC) == 0) {
                    return 4;
                }
                if (add_after_reset(raw) != 0) return 5;
                pcc_gc_publish_initialized(raw);
                if (add_after_reset(raw) != 1) return 6;
                pcc_gc_reset_relocation_set();
                pcc_gc_free_object_memory(raw);

                PyObject *seq = py_list_new(0);
                if (seq == NULL) return 7;
                PyObject *seq_iter = py_obj_iter(seq);
                PyObject *call_iter = py_iter_callable_new(
                    seq, py_int_from_i64(99)
                );
                PyObject *objects[2] = {seq_iter, call_iter};
                for (int i = 0; i < 2; i++) {
                    if (objects[i] == NULL) return 8 + i;
                    if (
                        (py_header(objects[i])->flags
                            & PY_FLAG_GC_FRESH_ALLOC) != 0
                    ) return 10 + i;
                    if (add_after_reset(objects[i]) != 1) return 12 + i;
                    pcc_gc_reset_relocation_set();
                }
                py_decref(call_iter);
                py_decref(seq_iter);
                py_decref(seq);
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} iterator constructor publication probe returned "
        f"{run.returncode}: " + run.stdout + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_selector_skips_fresh_pages_with_counter(
    tmp_path: Path,
    kind: str,
) -> None:
    """GC-P1-BACKEND4-FRESH-ALLOC-FILTER-DISAGREEMENT: the candidate
    snapshot must refuse a FRESH_ALLOC page owner itself (counted), not
    hand it to the relocation-set add to refuse invisibly after a full
    page walk."""
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_selector_fresh_skip",
        source_text=r"""
            #include "py_internal.h"
            #include <stdint.h>

            extern int64_t pcc_gc_backend4_select_relocation_pages(int64_t);
            extern int64_t pcc_gc_backend4_candidate_fresh_skips_count(void);
            extern int64_t pcc_gc_backend4_relocation_add_refusals_count(void);

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 2;
                PyObject *raw = pcc_gc_alloc(64, PY_TYPE_LIST, 0);
                if (raw == NULL) return 3;
                if ((py_header(raw)->flags & PY_FLAG_GC_FRESH_ALLOC) == 0) {
                    return 4;
                }
                if ((py_header(raw)->flags & PY_FLAG_GC_ZPAGE_ALLOC) == 0) {
                    /* Not zpage-backed: the selector cannot see it and the
                     * probe cannot conclude.  Fail loud rather than pass. */
                    return 21;
                }
                int64_t skips0 =
                    pcc_gc_backend4_candidate_fresh_skips_count();
                int64_t refusals0 =
                    pcc_gc_backend4_relocation_add_refusals_count();
                pcc_gc_backend4_select_relocation_pages(8);
                int64_t skips1 =
                    pcc_gc_backend4_candidate_fresh_skips_count();
                int64_t refusals1 =
                    pcc_gc_backend4_relocation_add_refusals_count();
                if (skips1 <= skips0) return 5;
                if (refusals1 != refusals0) return 6;
                pcc_gc_publish_initialized(raw);
                pcc_gc_reset_relocation_set();
                pcc_gc_free_object_memory(raw);
                return 0;
            }
        """,
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} selector fresh-skip probe returned "
        f"{run.returncode}: " + run.stdout + run.stderr
    )


# Was @pytest.mark.skip(reason="strict GC4 suspended-execution ... blocker").
# The reason was backwards: the strict arm PASSES and the C arm fails (rc=24).
# Tracked as GC-P1-BACKEND4-SUSPENDED-EXECUTION-C-ARM-PUBLICATION.
@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_suspended_execution_constructor_publication(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_suspended_execution_constructor_publication",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>

            static PyObject *dummy_resume(PyObject *gen, PyObject *frame) {
                (void)gen;
                (void)frame;
                py_incref(py_None);
                return py_None;
            }

            static void continuation_resume(void) {}

            static int add_after_reset(PyObject *obj) {
                pcc_gc_reset_relocation_set();
                return (int)pcc_gc_backend4_relocation_set_add(obj);
            }

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 2;
#if PCC_PROBE_RAW
                int32_t tags[4] = {
                    PY_TYPE_GEN, PY_TYPE_COROUTINE,
                    PY_TYPE_CONTINUATION, PY_TYPE_TASK
                };
                int64_t sizes[4] = {56, 64, 48, 48};
                for (int i = 0; i < 4; i++) {
                    PyObject *raw = pcc_gc_alloc(sizes[i], tags[i], 0);
                    if (raw == NULL) return 20 + i;
                    if (
                        (py_header(raw)->flags & PY_FLAG_GC_FRESH_ALLOC) == 0
                    ) return 24 + i;
                    if (add_after_reset(raw) != 0) return 28 + i;
                    pcc_gc_publish_initialized(raw);
                    if (add_after_reset(raw) != 1) return 32 + i;
                    pcc_gc_reset_relocation_set();
                    pcc_gc_free_object_memory(raw);
                }
#endif
                PyObject *frame = py_list_new(0);
                if (frame == NULL) return 3;
                PyObject *gen = py_gen_new(
                    (void *)(uintptr_t)dummy_resume, frame
                );
                PyObject *coro = py_coroutine_new("preflight");
                int32_t frame_map[1] = {1};
                PyObject *slots[1] = {frame};
                PyObject *cont = py_continuation_new(
                    frame_map, slots, (void *)(uintptr_t)continuation_resume
                );
                PyObject *task = py_task_new(coro);
                if (gen == NULL) return 4;
                if (coro == NULL) return 5;
                if (cont == NULL) return 6;
                if (task == NULL) return 7;
                PyObject *objects[4] = {gen, coro, cont, task};
                for (int i = 0; i < 4; i++) {
                    if (
                        (py_header(objects[i])->flags
                            & PY_FLAG_GC_FRESH_ALLOC) != 0
                    ) return 8 + i;
                    if (add_after_reset(objects[i]) != 1) return 12 + i;
                    pcc_gc_reset_relocation_set();
                }
                py_decref(task);
                py_decref(cont);
                py_decref(coro);
                py_decref(gen);
                py_decref(frame);
                return 0;
            }
        '''.replace("PCC_PROBE_RAW", "0"),
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} suspended-execution constructor publication returned "
        f"{run.returncode}: " + run.stdout + run.stderr
    )


def test_strict_decref_does_not_accept_unmanaged_cext_tags_by_range_alone():

    py_src = PY_OBJ_PORT.read_text(encoding="utf-8")
    assert "pcc_capi_is_cext_type_tag = extern(" not in py_src
    py_prepare = py_src.split("def _py_decref_prepare(o, prepared) -> None:", 1)[
        1
    ].split("def _py_decref_finish", 1)[0]
    invalid_range = py_prepare.index("tag_dbg > 500")
    invalid_return = py_prepare.index("return", invalid_range)
    assert invalid_range < invalid_return


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_store_ptr_shades_white_child_during_real_incremental_cycle(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="store_ptr_incremental_cycle",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>

            #define ROOT_COUNT 64

            int main(void) {
                static int32_t frame_map[ROOT_COUNT + 1] = {ROOT_COUNT};
                static PyObject *roots[ROOT_COUNT];
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_INCREMENTAL_TRICOLOR
                    ) != 0) {
                    fprintf(stderr, "backend selection failed\n");
                    return 2;
                }
                for (int i = 0; i < ROOT_COUNT; i++) {
                    roots[i] = py_list_new(0);
                    if (roots[i] == NULL) {
                        fprintf(stderr, "root allocation failed at %d\n", i);
                        return 3;
                    }
                }
                pcc_gc_note_frame_enter(frame_map, roots);
                int64_t processed = pcc_gc_step(1);
                if (processed != 1) {
                    fprintf(
                        stderr,
                        "incremental cycle did not retain work: processed=%lld\n",
                        (long long)processed
                    );
                    return 4;
                }

                PyObject *owner = roots[0];
                PyObject *child = roots[1];
                int32_t owner_flags = __atomic_load_n(
                    &py_header(owner)->flags, __ATOMIC_ACQUIRE
                );
                int32_t child_flags = __atomic_load_n(
                    &py_header(child)->flags, __ATOMIC_ACQUIRE
                );
                __atomic_store_n(
                    &py_header(owner)->flags,
                    (owner_flags & ~PY_FLAG_GC_COLOR_MASK) | PY_FLAG_GC_BLACK,
                    __ATOMIC_RELEASE
                );
                __atomic_store_n(
                    &py_header(child)->flags,
                    (child_flags & ~PY_FLAG_GC_COLOR_MASK) | PY_FLAG_GC_WHITE,
                    __ATOMIC_RELEASE
                );

                PyObject *slot = NULL;
                pcc_gc_store_ptr(owner, &slot, child);
                child_flags = __atomic_load_n(
                    &py_header(child)->flags, __ATOMIC_ACQUIRE
                );
                if ((child_flags & PY_FLAG_GC_GRAY) == 0) {
                    fprintf(
                        stderr,
                        "active-cycle child was not shaded: flags=%d\n",
                        child_flags
                    );
                    return 5;
                }

                pcc_gc_store_ptr(owner, &slot, NULL);
                (void)pcc_gc_collect(0);
                pcc_gc_note_frame_leave(roots);
                for (int i = 0; i < ROOT_COUNT; i++) py_decref(roots[i]);
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} active incremental store probe returned {run.returncode}: "
        + run.stdout
        + run.stderr
    )


def test_backend4_mutator_payload_retarget_is_one_locked_metadata_transaction():
    symbol = "pcc_gc_backend4_retarget_mutator_payload_locked"
    assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES[symbol] == (
        (
            "c_ptr",
            "c_ptr",
            "c_int64",
            "c_ptr",
            "c_int64",
            "c_ptr",
            "c_int64",
        ),
        "c_int64",
    )
    assert symbol not in RUNTIME_SIGNATURES
    internal_header = (
        REPO_ROOT / "pcc" / "runtime" / "src" / "py_internal.h"
    ).read_text(encoding="utf-8")
    assert symbol in internal_header

    strict_src = PY_GC_BACKEND_PORT.read_text(encoding="utf-8")
    strict_body = strict_src.split(f'@c_abi_export("{symbol}")', 1)[1].split(
        '@c_abi_export("pcc_gc_backend4_zpage_fragmentation_per_mille")', 1
    )[0]
    for forbidden in (
        "malloc(",
        "free(",
        "py_incref(",
        "py_decref(",
        "pcc_py_gc_minor_graph_lock(",
        "pcc_py_gc_minor_graph_unlock(",
        "pcc_thread_safepoint(",
    ):
        assert forbidden not in strict_body
    assert strict_body.index("_store_buffer_medium_head()") < strict_body.index(
        "_store_buffer_head()"
    ) < strict_body.index("_remembered_set_head()")
    assert strict_body.index("store_ptr(payload_span, 8, new_base)") < (
        strict_body.rindex("return 1")
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_dict_set_rehash_retargets_pending_raw_slots(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_dict_set_rehash_retarget",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>

            #define FAIL(code, message) do { \
                fprintf(stderr, "%s\n", message); \
                return code; \
            } while (0)

            static int drain_store_buffer(void) {
                for (
                    int i = 0;
                    i < 32 && pcc_gc_backend4_store_buffer_entries() > 0;
                    i++
                ) {
                    (void)pcc_gc_backend4_step_remembered_roots(64);
                }
                return pcc_gc_backend4_store_buffer_entries() == 0;
            }

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) FAIL(2, "backend selection failed");

                PyObject *set_root = NULL;
                void *set_handle = pcc_gc_scheduler_root_register_handle(
                    &set_root
                );
                if (set_handle == NULL) FAIL(3, "set root registration failed");
                PyObject *set = py_set_new();
                if (set == NULL) FAIL(4, "set allocation failed");
                pcc_gc_store_root(&set_root, set);
                py_header(set)->flags =
                    (py_header(set)->flags
                        & ~(PY_FLAG_GC_YOUNG | PY_FLAG_GC_MINOR_ARENA))
                    | PY_FLAG_GC_OLD;
                for (int i = 0; i < 5; i++) {
                    PyObject *key = py_tuple_new(1);
                    if (key == NULL) FAIL(5, "set seed key allocation failed");
                    py_tuple_set_item(key, 0, py_int_from_i64(3000 + i));
                    py_set_add(set, key);
                    pcc_gc_release(key);
                }
                if (!drain_store_buffer()) FAIL(6, "set seed drain failed");
                set = pcc_gc_load_ptr(NULL, &set_root);
                SetEntry *old_set_entries = ((PySetObject *)set)->entries;
                PyObject **old_set_slots[8];
                for (int i = 0; i < 8; i++) {
                    old_set_slots[i] = &old_set_entries[i].key;
                }
                PyObject *set_key = py_tuple_new(1);
                if (set_key == NULL) FAIL(7, "set growth key allocation failed");
                py_tuple_set_item(set_key, 0, py_int_from_i64(3005));
                pcc_gc_telemetry_reset();
                py_set_add(set, set_key);
                PySetObject *grown_set = (PySetObject *)set;
                if (grown_set->entries == old_set_entries) {
                    FAIL(8, "set did not rehash");
                }
                for (int i = 0; i < 8; i++) {
                    if (pcc_gc_backend4_remembered_page_contains_slot(
                            old_set_slots[i]
                        )) FAIL(9, "set retained an old remembered slot");
                }
                PyObject **new_set_slot = NULL;
                for (int64_t i = 0; i < grown_set->capacity; i++) {
                    if (grown_set->entries[i].key == set_key) {
                        new_set_slot = &grown_set->entries[i].key;
                        break;
                    }
                }
                if (
                    new_set_slot == NULL
                    || !pcc_gc_backend4_remembered_page_contains_slot(
                        new_set_slot
                    )
                ) FAIL(10, "set new slot is not remembered");
                if (pcc_gc_backend4_store_buffer_entries() != 1) {
                    FAIL(11, "set pending edge count changed across rehash");
                }
                if (
                    pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_STORE_BARRIERS)
                    != 1
                ) FAIL(12, "set duplicate rehash barrier was re-enqueued");
                pcc_gc_release(set_key);
                if (!drain_store_buffer()) FAIL(13, "set growth drain failed");
                set = pcc_gc_load_ptr(NULL, &set_root);
                if (py_set_len(set) != 6) FAIL(14, "set length drifted");

                PyObject *dict_root = NULL;
                void *dict_handle = pcc_gc_scheduler_root_register_handle(
                    &dict_root
                );
                if (dict_handle == NULL) FAIL(15, "dict root registration failed");
                PyObject *dict = py_dict_new();
                if (dict == NULL) FAIL(16, "dict allocation failed");
                pcc_gc_store_root(&dict_root, dict);
                py_header(dict)->flags =
                    (py_header(dict)->flags
                        & ~(PY_FLAG_GC_YOUNG | PY_FLAG_GC_MINOR_ARENA))
                    | PY_FLAG_GC_OLD;
                for (int i = 0; i < 5; i++) {
                    PyObject *key = py_tuple_new(1);
                    if (key == NULL) FAIL(17, "dict seed key allocation failed");
                    py_tuple_set_item(key, 0, py_int_from_i64(4000 + i));
                    py_dict_set(dict, key, py_int_from_i64(5000 + i));
                    pcc_gc_release(key);
                }
                if (!drain_store_buffer()) FAIL(18, "dict seed drain failed");
                dict = pcc_gc_load_ptr(NULL, &dict_root);
                PyDictObject *dict_obj = (PyDictObject *)dict;
                DictEntry *old_dict_entries = dict_obj->entries;
                PyObject **old_dict_slots[16];
                for (int i = 0; i < 8; i++) {
                    old_dict_slots[i * 2] = &old_dict_entries[i].key;
                    old_dict_slots[i * 2 + 1] = &old_dict_entries[i].value;
                }
                PyObject *dict_key = py_tuple_new(1);
                if (dict_key == NULL) FAIL(19, "dict growth key allocation failed");
                py_tuple_set_item(dict_key, 0, py_int_from_i64(4005));
                pcc_gc_telemetry_reset();
                py_dict_set(dict, dict_key, py_int_from_i64(5005));
                dict_obj = (PyDictObject *)dict;
                if (dict_obj->entries == old_dict_entries) {
                    FAIL(20, "dict did not rehash");
                }
                for (int i = 0; i < 16; i++) {
                    if (pcc_gc_backend4_remembered_page_contains_slot(
                            old_dict_slots[i]
                        )) FAIL(21, "dict retained an old remembered slot");
                }
                PyObject **new_dict_slot = NULL;
                for (int64_t i = 0; i < dict_obj->entries_used; i++) {
                    if (dict_obj->entries[i].key == dict_key) {
                        new_dict_slot = &dict_obj->entries[i].key;
                        break;
                    }
                }
                if (
                    new_dict_slot == NULL
                    || !pcc_gc_backend4_remembered_page_contains_slot(
                        new_dict_slot
                    )
                ) FAIL(22, "dict new slot is not remembered");
                if (pcc_gc_backend4_store_buffer_entries() != 1) {
                    FAIL(23, "dict pending edge count changed across rehash");
                }
                if (
                    pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_STORE_BARRIERS)
                    != 1
                ) FAIL(24, "dict duplicate rehash barrier was re-enqueued");
                pcc_gc_release(dict_key);
                if (!drain_store_buffer()) FAIL(25, "dict growth drain failed");
                dict = pcc_gc_load_ptr(NULL, &dict_root);
                if (py_dict_len(dict) != 6) FAIL(26, "dict length drifted");

                pcc_gc_store_root(&dict_root, NULL);
                pcc_gc_scheduler_root_unregister_handle(dict_handle);
                pcc_gc_store_root(&set_root, NULL);
                pcc_gc_scheduler_root_unregister_handle(set_handle);
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} dict/set rehash retarget probe returned {run.returncode}: "
        + run.stdout
        + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_list_growth_retargets_pending_raw_slots(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_list_growth_retarget",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>

            #define FAIL(code, message) do { \
                fprintf(stderr, "%s\n", message); \
                return code; \
            } while (0)

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) FAIL(2, "backend selection failed");
                PyObject *list_root = NULL;
                void *root_handle = pcc_gc_scheduler_root_register_handle(
                    &list_root
                );
                if (root_handle == NULL) FAIL(3, "list root registration failed");
                PyObject *list = py_list_new(0);
                if (list == NULL) FAIL(4, "list allocation failed");
                pcc_gc_store_root(&list_root, list);
                py_header(list)->flags =
                    (py_header(list)->flags
                        & ~(PY_FLAG_GC_YOUNG | PY_FLAG_GC_MINOR_ARENA))
                    | PY_FLAG_GC_OLD;
                for (int i = 0; i < 4; i++) {
                    PyObject *item = py_tuple_new(1);
                    if (item == NULL) FAIL(5, "seed item allocation failed");
                    py_tuple_set_item(item, 0, py_int_from_i64(7000 + i));
                    py_list_append(list, item);
                    pcc_gc_release(item);
                }
                if (pcc_gc_backend4_store_buffer_entries() != 4) {
                    FAIL(6, "seed pending-edge count is not four");
                }
                PyListObject *old_list = (PyListObject *)list;
                PyObject **old_items = old_list->items;
                PyObject **old_slots[4];
                for (int i = 0; i < 4; i++) old_slots[i] = &old_items[i];

                PyObject *growth_item = py_tuple_new(1);
                if (growth_item == NULL) FAIL(7, "growth item allocation failed");
                py_tuple_set_item(growth_item, 0, py_int_from_i64(7004));
                pcc_gc_telemetry_reset();
                py_list_append(list, growth_item);
                PyListObject *grown = (PyListObject *)list;
                if (grown->items == old_items || grown->capacity <= 4) {
                    FAIL(8, "list did not replace its item array");
                }
                for (int i = 0; i < 4; i++) {
                    if (pcc_gc_backend4_remembered_page_contains_slot(
                            old_slots[i]
                        )) FAIL(9, "list retained an old remembered slot");
                }
                for (int i = 0; i < 5; i++) {
                    if (!pcc_gc_backend4_remembered_page_contains_slot(
                            &grown->items[i]
                        )) FAIL(10, "list new slot is not remembered");
                }
                if (pcc_gc_backend4_store_buffer_entries() != 5) {
                    FAIL(11, "list pending edges changed across growth");
                }
                if (
                    pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_STORE_BARRIERS)
                    != 1
                ) FAIL(12, "list copied pending edges were re-enqueued");
                pcc_gc_release(growth_item);
                for (
                    int i = 0;
                    i < 32 && pcc_gc_backend4_store_buffer_entries() > 0;
                    i++
                ) {
                    (void)pcc_gc_backend4_step_remembered_roots(64);
                }
                list = pcc_gc_load_ptr(NULL, &list_root);
                if (list == NULL || py_list_len(list) != 5) {
                    FAIL(13, "list length drifted after drain");
                }
                for (int i = 0; i < 5; i++) {
                    PyObject *item = py_list_get(list, i);
                    if (item == NULL) FAIL(14, "list item missing after drain");
                    PyObject *value = pcc_gc_load_ptr(
                        item, &((PyTupleObject *)item)->items[0]
                    );
                    int overflow = 0;
                    int64_t number = py_int_to_i64(value, &overflow);
                    pcc_gc_release(item);
                    if (overflow || number != 7000 + i) {
                        FAIL(15, "list item content drifted");
                    }
                }
                pcc_gc_store_root(&list_root, NULL);
                pcc_gc_scheduler_root_unregister_handle(root_handle);
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} list growth retarget probe returned {run.returncode}: "
        + run.stdout
        + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_list_growth_reloads_forwarded_item_and_source_roots(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_list_growth_forwarded_inputs",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 2;
                PyObject *source = py_list_new(0);
                if (source == NULL) return 3;
                py_list_append(source, py_int_from_i64(8100));
                py_list_append(source, py_int_from_i64(8101));
                pcc_gc_reset_relocation_set();
                if (pcc_gc_backend4_relocation_set_add(source) != 1) return 4;
                PyObject *target = pcc_gc_relocate_copy(
                    source, (int64_t)sizeof(PyListObject)
                );
                if (target == NULL || target == source) return 5;

                PyObject *holder_root = NULL;
                void *holder_handle = pcc_gc_scheduler_root_register_handle(
                    &holder_root
                );
                if (holder_handle == NULL) return 6;
                PyObject *holder = py_list_new(0);
                if (holder == NULL) return 7;
                pcc_gc_store_root(&holder_root, holder);
                for (int i = 0; i < 4; i++) {
                    py_list_append(holder, py_int_from_i64(8200 + i));
                }
                py_list_append(holder, source);
                holder = pcc_gc_load_ptr(NULL, &holder_root);
                PyObject *stored = py_list_get(holder, 4);
                if (stored != target) return 8;
                pcc_gc_release(stored);

                PyObject *dst_root = NULL;
                void *dst_handle = pcc_gc_scheduler_root_register_handle(
                    &dst_root
                );
                if (dst_handle == NULL) return 9;
                PyObject *dst = py_list_new(0);
                if (dst == NULL) return 10;
                pcc_gc_store_root(&dst_root, dst);
                for (int i = 0; i < 4; i++) {
                    py_list_append(dst, py_int_from_i64(8300 + i));
                }
                py_list_extend(dst, source);
                dst = pcc_gc_load_ptr(NULL, &dst_root);
                if (py_list_len(dst) != 6) return 11;
                for (int i = 0; i < 2; i++) {
                    PyObject *value = py_list_get(dst, 4 + i);
                    int overflow = 0;
                    int64_t number = py_int_to_i64(value, &overflow);
                    pcc_gc_release(value);
                    if (overflow || number != 8100 + i) return 12;
                }
                PyObject *copied = py_list_copy(source);
                PyObject *repeated = py_list_repeat(source, 2);
                PyObject *concatenated = py_list_concat(source, target);
                if (
                    copied == NULL || repeated == NULL || concatenated == NULL
                    || py_list_len(copied) != 2
                    || py_list_len(repeated) != 4
                    || py_list_len(concatenated) != 4
                ) return 14;
                PyObject *copy_value = py_list_get(copied, 1);
                PyObject *repeat_value = py_list_get(repeated, 3);
                PyObject *concat_value = py_list_get(concatenated, 2);
                int copy_overflow = 0;
                int repeat_overflow = 0;
                int concat_overflow = 0;
                int64_t copy_number = py_int_to_i64(
                    copy_value, &copy_overflow
                );
                int64_t repeat_number = py_int_to_i64(
                    repeat_value, &repeat_overflow
                );
                int64_t concat_number = py_int_to_i64(
                    concat_value, &concat_overflow
                );
                pcc_gc_release(copy_value);
                pcc_gc_release(repeat_value);
                pcc_gc_release(concat_value);
                if (
                    copy_overflow || repeat_overflow || concat_overflow
                    || copy_number != 8101
                    || repeat_number != 8101
                    || concat_number != 8100
                ) return 15;
                pcc_gc_release(copied);
                pcc_gc_release(repeated);
                pcc_gc_release(concatenated);
                if (pcc_gc_scheduler_root_count() != 2) {
                    fprintf(
                        stderr,
                        "temporary roots leaked: count=%lld\n",
                        (long long)pcc_gc_scheduler_root_count()
                    );
                    return 16;
                }

                pcc_gc_store_root(&dst_root, NULL);
                pcc_gc_scheduler_root_unregister_handle(dst_handle);
                pcc_gc_store_root(&holder_root, NULL);
                pcc_gc_scheduler_root_unregister_handle(holder_handle);
                py_decref(target);
                py_decref(source);
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} list forwarded-input growth probe returned {run.returncode}: "
        + run.stdout
        + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_list_get_pop_reverse_reload_forwarded_owner(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_list_get_pop_reverse_forwarded",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 2;
                PyObject *source = py_list_new(0);
                if (source == NULL) return 3;
                py_list_append(source, py_int_from_i64(9100));
                py_list_append(source, py_int_from_i64(9101));
                py_list_append(source, py_int_from_i64(9102));
                pcc_gc_reset_relocation_set();
                if (pcc_gc_backend4_relocation_set_add(source) != 1) return 4;
                PyObject *target = pcc_gc_relocate_copy(
                    source, (int64_t)sizeof(PyListObject)
                );
                if (target == NULL || target == source) return 5;

                PyObject *first = py_list_get(source, 0);
                int overflow = 0;
                if (py_int_to_i64(first, &overflow) != 9100 || overflow) {
                    return 6;
                }
                pcc_gc_release(first);
                py_list_reverse(source);
                PyObject *popped = py_list_pop(source, -1);
                overflow = 0;
                if (py_int_to_i64(popped, &overflow) != 9100 || overflow) {
                    return 7;
                }
                pcc_gc_release(popped);
                if (py_list_len(target) != 2) return 8;
                PyObject *left = py_list_get(target, 0);
                PyObject *right = py_list_get(target, 1);
                int overflow_left = 0;
                int overflow_right = 0;
                int64_t left_value = py_int_to_i64(left, &overflow_left);
                int64_t right_value = py_int_to_i64(right, &overflow_right);
                pcc_gc_release(left);
                pcc_gc_release(right);
                if (
                    overflow_left || overflow_right
                    || left_value != 9102 || right_value != 9101
                ) return 9;
                if (pcc_gc_scheduler_root_count() != 0) {
                    fprintf(
                        stderr,
                        "temporary roots leaked: count=%lld\n",
                        (long long)pcc_gc_scheduler_root_count()
                    );
                    return 10;
                }
                py_decref(target);
                py_decref(source);
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} list get/pop/reverse forwarding probe returned "
        f"{run.returncode}: " + run.stdout + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_list_remove_equality_callback_relocates_and_mutates_current_index(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_list_remove_eq_reentry",
        extra_include_dirs=(REPO_ROOT / "utils" / "fake_libc_include",),
        source_text=r'''
            #include "Python.h"
            #include <stdint.h>
            #include <stdio.h>

            typedef struct ProbeEqObject {
                PyObject_HEAD
            } ProbeEqObject;

            typedef struct ProbeListObject {
                PyObjectHeader h;
                int64_t length;
                int64_t capacity;
                PyObject **items;
            } ProbeListObject;

            extern int64_t pcc_gc_backend4_relocation_set_add(PyObject *obj);

            static PyObject *list_root;
            static int64_t callback_count;

            static PyObject *probe_richcompare(
                PyObject *self, PyObject *other, int op
            ) {
                (void)self;
                (void)other;
                if (op != Py_EQ) {
                    Py_INCREF(Py_NotImplemented);
                    return Py_NotImplemented;
                }
                PyObject *list = pcc_gc_load_ptr(NULL, &list_root);
                pcc_gc_reset_relocation_set();
                if (pcc_gc_backend4_relocation_set_add(list) != 1) {
                    return NULL;
                }
                PyObject *target = pcc_gc_relocate_copy(
                    list, (int64_t)sizeof(ProbeListObject)
                );
                if (target == NULL || target == list) return NULL;
                py_decref(target);
                list = pcc_gc_load_ptr(NULL, &list_root);
                py_list_insert(list, 0, py_int_from_i64(4242));
                callback_count++;
                Py_INCREF(Py_True);
                return Py_True;
            }

            static PyTypeObject ProbeEqType = {
                PyVarObject_HEAD_INIT(NULL, 0)
                .tp_name = "pcc_probe.EqMutator",
                .tp_basicsize = sizeof(ProbeEqObject),
                .tp_flags = Py_TPFLAGS_DEFAULT,
                .tp_richcompare = probe_richcompare,
            };

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 2;
                if (PyType_Ready(&ProbeEqType) != 0) return 3;
                void *root_handle = pcc_gc_scheduler_root_register_handle(
                    &list_root
                );
                if (root_handle == NULL) return 4;
                PyObject *list = py_list_new(0);
                if (list == NULL) return 5;
                pcc_gc_store_root(&list_root, list);
                py_decref(list);
                ProbeEqObject *candidate = PyObject_New(
                    ProbeEqObject, &ProbeEqType
                );
                if (candidate == NULL) return 6;
                py_list_append(list_root, (PyObject *)candidate);
                Py_DECREF(candidate);
                py_list_append(list_root, py_int_from_i64(99));

                py_list_remove(list_root, py_int_from_i64(123456));
                if (callback_count != 1) return 7;
                list = pcc_gc_load_ptr(NULL, &list_root);
                if (list == NULL || py_list_len(list) != 2) return 8;
                PyObject *first = py_list_get(list, 0);
                PyObject *second = py_list_get(list, 1);
                int overflow = 0;
                int64_t second_value = py_int_to_i64(second, &overflow);
                if (
                    first != (PyObject *)candidate
                    || overflow || second_value != 99
                ) return 9;
                py_decref(first);
                py_decref(second);
                if (pcc_gc_scheduler_root_count() != 1) return 10;
                pcc_gc_store_root(&list_root, NULL);
                pcc_gc_scheduler_root_unregister_handle(root_handle);
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} list remove equality reentry probe returned "
        f"{run.returncode}: " + run.stdout + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_list_clear_native_finalizer_relocates_and_reenters_published_empty_list(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_list_clear_native_finalizer_reentry",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>

            static PyObject *list_root;
            static int64_t finalizer_hits;
            static int64_t observed_length = -1;

            static void probe_finalizer(PyObject *self) {
                (void)self;
                PyObject *list = pcc_gc_load_ptr(NULL, &list_root);
                observed_length = py_list_len(list);
                pcc_gc_reset_relocation_set();
                if (pcc_gc_backend4_relocation_set_add(list) != 1) return;
                PyObject *target = pcc_gc_relocate_copy(
                    list, (int64_t)sizeof(PyListObject)
                );
                if (target == NULL || target == list) return;
                py_decref(target);
                list = pcc_gc_load_ptr(NULL, &list_root);
                py_list_append(list, py_int_from_i64(777));
                finalizer_hits++;
            }

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 2;
                void *root_handle = pcc_gc_scheduler_root_register_handle(
                    &list_root
                );
                if (root_handle == NULL) return 3;
                PyObject *list = py_list_new(0);
                if (list == NULL) return 4;
                pcc_gc_store_root(&list_root, list);
                py_decref(list);

                PyClassObject *cls = py_class_new(
                    "ClearNativeFinalizer", 0, 0, 0, 0
                );
                if (cls == NULL) return 5;
                py_class_add_method(
                    cls,
                    "__del__",
                    (PyObject *)(uintptr_t)probe_finalizer
                );
                PyObject *value = py_instance_new(cls);
                if (value == NULL) return 6;
                py_list_append(list_root, value);
                py_decref(value);

                py_list_clear(list_root);
                if (finalizer_hits != 1 || observed_length != 0) return 7;
                list = pcc_gc_load_ptr(NULL, &list_root);
                if (list == NULL || py_list_len(list) != 1) return 8;
                PyObject *inserted = py_list_get(list, 0);
                int overflow = 0;
                int64_t inserted_value = py_int_to_i64(inserted, &overflow);
                py_decref(inserted);
                if (overflow || inserted_value != 777) return 9;
                if (pcc_gc_scheduler_root_count() != 1) return 10;

                py_list_clear(list);
                pcc_gc_store_root(&list_root, NULL);
                pcc_gc_scheduler_root_unregister_handle(root_handle);
                py_decref((PyObject *)cls);
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} list clear native finalizer reentry probe returned "
        f"{run.returncode}: " + run.stdout + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_list_delete_slice_finalizer_relocates_and_reenters_compacted_list(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_list_delete_slice_finalizer_reentry",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>

            static PyObject *list_root;
            static int64_t finalizer_hits;
            static int64_t observed_length = -1;

            static void probe_finalizer(PyObject *self) {
                (void)self;
                PyObject *list = pcc_gc_load_ptr(NULL, &list_root);
                observed_length = py_list_len(list);
                pcc_gc_reset_relocation_set();
                if (pcc_gc_backend4_relocation_set_add(list) != 1) return;
                PyObject *target = pcc_gc_relocate_copy(
                    list, (int64_t)sizeof(PyListObject)
                );
                if (target == NULL || target == list) return;
                py_decref(target);
                list = pcc_gc_load_ptr(NULL, &list_root);
                py_list_append(list, py_int_from_i64(777));
                finalizer_hits++;
            }

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 2;
                void *root_handle = pcc_gc_scheduler_root_register_handle(
                    &list_root
                );
                if (root_handle == NULL) return 3;
                PyObject *list = py_list_new(0);
                if (list == NULL) return 4;
                pcc_gc_store_root(&list_root, list);
                py_decref(list);

                PyClassObject *cls = py_class_new(
                    "DeleteSliceNativeFinalizer", 0, 0, 0, 0
                );
                if (cls == NULL) return 5;
                py_class_add_method(
                    cls,
                    "__del__",
                    (PyObject *)(uintptr_t)probe_finalizer
                );
                PyObject *value = py_instance_new(cls);
                if (value == NULL) return 6;
                py_list_append(list_root, py_int_from_i64(0));
                py_list_append(list_root, value);
                py_decref(value);
                py_list_append(list_root, py_int_from_i64(2));
                py_list_append(list_root, py_int_from_i64(3));

                if (py_list_del_slice(
                        list_root,
                        py_int_from_i64(1),
                        py_int_from_i64(2),
                        py_None
                    ) != 0) return 7;
                if (finalizer_hits != 1 || observed_length != 3) return 8;
                list = pcc_gc_load_ptr(NULL, &list_root);
                if (list == NULL || py_list_len(list) != 4) return 9;
                int64_t expected[4] = {0, 2, 3, 777};
                for (int64_t i = 0; i < 4; i++) {
                    PyObject *item = py_list_get(list, i);
                    int overflow = 0;
                    int64_t number = py_int_to_i64(item, &overflow);
                    py_decref(item);
                    if (overflow || number != expected[i]) return 10 + (int)i;
                }
                if (pcc_gc_backend4_verify_no_old_addresses() != 1) return 20;
                if (pcc_gc_scheduler_root_count() != 1) return 21;

                py_list_clear(list);
                pcc_gc_store_root(&list_root, NULL);
                pcc_gc_scheduler_root_unregister_handle(root_handle);
                py_decref((PyObject *)cls);
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} list delete-slice finalizer reentry probe returned "
        f"{run.returncode}: " + run.stdout + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_list_set_slice_finalizer_relocates_and_reenters_published_payload(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_list_set_slice_finalizer_reentry",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>

            static PyObject *list_root;
            static int64_t finalizer_hits;
            static int64_t observed_length = -1;

            static void probe_finalizer(PyObject *self) {
                (void)self;
                PyObject *list = pcc_gc_load_ptr(NULL, &list_root);
                observed_length = py_list_len(list);
                pcc_gc_reset_relocation_set();
                if (pcc_gc_backend4_relocation_set_add(list) != 1) return;
                PyObject *target = pcc_gc_relocate_copy(
                    list, (int64_t)sizeof(PyListObject)
                );
                if (target == NULL || target == list) return;
                py_decref(target);
                list = pcc_gc_load_ptr(NULL, &list_root);
                py_list_append(list, py_int_from_i64(777));
                finalizer_hits++;
            }

            static int expect_i64(PyObject *list, int64_t i, int64_t expected) {
                PyObject *item = py_list_get(list, i);
                int overflow = 0;
                int64_t number = py_int_to_i64(item, &overflow);
                py_decref(item);
                return !overflow && number == expected;
            }

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 2;
                void *root_handle = pcc_gc_scheduler_root_register_handle(
                    &list_root
                );
                if (root_handle == NULL) return 3;
                PyObject *list = py_list_new(0);
                if (list == NULL) return 4;
                pcc_gc_store_root(&list_root, list);
                py_decref(list);

                PyClassObject *cls = py_class_new(
                    "SetSliceNativeFinalizer", 0, 0, 0, 0
                );
                if (cls == NULL) return 5;
                py_class_add_method(
                    cls,
                    "__del__",
                    (PyObject *)(uintptr_t)probe_finalizer
                );
                PyObject *value = py_instance_new(cls);
                PyObject *replacement = py_list_new(0);
                if (value == NULL || replacement == NULL) return 6;
                py_list_append(list_root, py_int_from_i64(0));
                py_list_append(list_root, value);
                py_decref(value);
                py_list_append(list_root, py_int_from_i64(2));
                py_list_append(list_root, py_int_from_i64(3));
                py_list_append(replacement, py_int_from_i64(7));
                py_list_append(replacement, py_int_from_i64(8));

                if (py_list_set_slice(
                        list_root,
                        py_int_from_i64(1),
                        py_int_from_i64(2),
                        py_None,
                        replacement
                    ) != 0) return 7;
                py_decref(replacement);
                if (finalizer_hits != 1 || observed_length != 5) return 8;
                list = pcc_gc_load_ptr(NULL, &list_root);
                if (list == NULL || py_list_len(list) != 6) return 9;
                int64_t expected[6] = {0, 7, 8, 2, 3, 777};
                for (int64_t i = 0; i < 6; i++) {
                    if (!expect_i64(list, i, expected[i])) return 10 + (int)i;
                }

                PyObject *mismatch = py_list_new(0);
                if (mismatch == NULL) return 20;
                py_list_append(mismatch, py_int_from_i64(99));
                if (py_list_set_slice(
                        list,
                        py_int_from_i64(0),
                        py_int_from_i64(4),
                        py_int_from_i64(2),
                        mismatch
                    ) != -1) return 21;
                py_decref(mismatch);
                if (py_list_len(list) != 6) return 22;
                for (int64_t i = 0; i < 6; i++) {
                    if (!expect_i64(list, i, expected[i])) return 23 + (int)i;
                }
                if (pcc_gc_backend4_verify_no_old_addresses() != 1) return 30;
                if (pcc_gc_scheduler_root_count() != 1) return 31;

                py_list_clear(list);
                pcc_gc_store_root(&list_root, NULL);
                pcc_gc_scheduler_root_unregister_handle(root_handle);
                py_decref((PyObject *)cls);
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} list set-slice finalizer reentry probe returned "
        f"{run.returncode}: " + run.stdout + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
@pytest.mark.parametrize("container_kind", ["set", "list"])
def test_backend4_real_container_growth_blocks_stw_until_commit_and_retires_source(
    tmp_path: Path,
    kind: str,
    container_kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem=f"backend4_{container_kind}_growth_stw_relocation",
        source_text=(
            "#define PCC_PROBE_STRICT "
            + ("1\n")
            + "#define PCC_PROBE_LIST "
            + ("1\n" if container_kind == "list" else "0\n")
            + r'''
            #include "py_internal.h"
            #include <sched.h>
            #include <stdint.h>
            #include <stdio.h>

#if PCC_PROBE_STRICT
            extern void pcc_py_gc_minor_graph_lock(void);
            extern void pcc_py_gc_minor_graph_unlock(void);
#define PROBE_GRAPH_LOCK() pcc_py_gc_minor_graph_lock()
#define PROBE_GRAPH_UNLOCK() pcc_py_gc_minor_graph_unlock()
#else
#define PROBE_GRAPH_LOCK() pcc_gc_root_slot_lock()
#define PROBE_GRAPH_UNLOCK() pcc_gc_root_slot_unlock()
#endif

            static PyObject *set_root;
            static PyObject *growth_key;
            static PyObject *moved_target;
            static int64_t mutator_ready;
            static int64_t mutation_committed;
            static int64_t world_acquired;
            static int64_t collector_done;

            static void *mutator_main(void *arg) {
                (void)arg;
                PROBE_GRAPH_LOCK();
                if (pcc_thread_no_park_depth() != 1) {
                    return (void *)(intptr_t)2;
                }
                __atomic_store_n(&mutator_ready, 1, __ATOMIC_RELEASE);
                while (pcc_thread_stop_requested_acquire() == 0) {
                    sched_yield();
                }
                if (__atomic_load_n(&world_acquired, __ATOMIC_ACQUIRE) != 0) {
                    return (void *)(intptr_t)3;
                }
                PyObject *container = pcc_gc_load_ptr(NULL, &set_root);
#if PCC_PROBE_LIST
                py_list_append(container, growth_key);
                if (py_list_len(container) != 5) {
#else
                py_set_add(container, growth_key);
                if (py_set_len(container) != 6) {
#endif
                    return (void *)(intptr_t)4;
                }
                __atomic_store_n(
                    &mutation_committed, 1, __ATOMIC_RELEASE
                );
                if (__atomic_load_n(&world_acquired, __ATOMIC_ACQUIRE) != 0) {
                    return (void *)(intptr_t)5;
                }
                PROBE_GRAPH_UNLOCK();
                return 0;
            }

            static void *collector_main(void *arg) {
                (void)arg;
                while (__atomic_load_n(
                    &mutator_ready, __ATOMIC_ACQUIRE
                ) == 0) {
                    sched_yield();
                }
                if (pcc_stop_the_world() != 0) {
                    return (void *)(intptr_t)6;
                }
                if (__atomic_load_n(
                        &mutation_committed, __ATOMIC_ACQUIRE
                    ) != 1) {
                    (void)pcc_resume_world();
                    return (void *)(intptr_t)7;
                }
                __atomic_store_n(&world_acquired, 1, __ATOMIC_RELEASE);
                PyObject *source = set_root;
                for (
                    int i = 0;
                    i < 32 && pcc_gc_backend4_store_buffer_entries() > 0;
                    i++
                ) {
                    (void)pcc_gc_backend4_step_remembered_roots(64);
                }
                if (pcc_gc_backend4_store_buffer_entries() != 0) {
                    (void)pcc_resume_world();
                    return (void *)(intptr_t)30;
                }
                pcc_gc_reset_relocation_set();
                if (pcc_gc_backend4_relocation_set_add(source) != 1) {
                    (void)pcc_resume_world();
                    return (void *)(intptr_t)31;
                }
                if (pcc_gc_relocation_set_contains(source) != 1) {
                    (void)pcc_resume_world();
                    return (void *)(intptr_t)32;
                }
                PyObject *target = pcc_gc_relocate_copy(
                    source,
#if PCC_PROBE_LIST
                    (int64_t)sizeof(PyListObject)
#else
                    (int64_t)sizeof(PySetObject)
#endif
                );
                if (target == NULL || target == source) {
                    (void)pcc_resume_world();
                    return (void *)(intptr_t)29;
                }
                moved_target = target;
                py_decref(target);
                int64_t remapped = 0;
                for (
                    int i = 0;
                    i < 4 && pcc_gc_backend4_forwarding_entries() > 0;
                    i++
                ) {
                    remapped +=
                        pcc_gc_backend4_remap_and_retire_stopped_world();
                }
                if (
                    remapped <= 0
                    || pcc_gc_backend4_forwarding_entries() != 0
                    || set_root != moved_target
                ) {
                    (void)pcc_resume_world();
                    return (void *)(intptr_t)9;
                }
                if (pcc_resume_world() != 0) {
                    return (void *)(intptr_t)10;
                }
                __atomic_store_n(&collector_done, 1, __ATOMIC_RELEASE);
                return 0;
            }

            int main(void) {
                if (pcc_threads_enabled() != 1) return 11;
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 12;
                void *root_handle = pcc_gc_scheduler_root_register_handle(
                    &set_root
                );
                if (root_handle == NULL) return 13;
                PyObject *container = NULL;
                int seed_count = 0;
                int64_t value_base = 0;
#if PCC_PROBE_LIST
                container = py_list_new(0);
                seed_count = 4;
                value_base = 8000;
#else
                container = py_set_new();
                seed_count = 5;
                value_base = 6000;
#endif
                if (container == NULL) return 14;
                pcc_gc_store_root(&set_root, container);
                pcc_gc_release(container);
                container = set_root;
                py_header(container)->flags =
                    (py_header(container)->flags
                        & ~(PY_FLAG_GC_YOUNG | PY_FLAG_GC_MINOR_ARENA))
                    | PY_FLAG_GC_OLD;
                for (int i = 0; i < seed_count; i++) {
                    PyObject *key = py_tuple_new(1);
                    if (key == NULL) return 15;
                    py_tuple_set_item(
                        key, 0, py_int_from_i64(value_base + i)
                    );
#if PCC_PROBE_LIST
                    py_list_append(container, key);
#else
                    py_set_add(container, key);
#endif
                    pcc_gc_release(key);
                }
                for (
                    int i = 0;
                    i < 32 && pcc_gc_backend4_store_buffer_entries() > 0;
                    i++
                ) {
                    (void)pcc_gc_backend4_step_remembered_roots(64);
                }
                container = pcc_gc_load_ptr(NULL, &set_root);
#if PCC_PROBE_LIST
                if (
                    container == NULL
                    || py_list_len(container) != 4
                    || ((PyListObject *)container)->capacity != 4
                ) return 16;
#else
                if (
                    container == NULL
                    || py_set_len(container) != 5
                    || ((PySetObject *)container)->capacity != 8
                ) return 16;
#endif
                growth_key = py_tuple_new(1);
                if (growth_key == NULL) return 17;
                py_tuple_set_item(
                    growth_key, 0, py_int_from_i64(value_base + seed_count)
                );

                PccThreadHandle *mutator = NULL;
                PccThreadHandle *collector = NULL;
                if (pcc_thread_start(
                        &mutator, mutator_main, NULL
                    ) != 0) return 18;
                if (pcc_thread_start(
                        &collector, collector_main, NULL
                    ) != 0) return 19;
                void *mutator_result = NULL;
                void *collector_result = NULL;
                if (pcc_thread_join(mutator, &mutator_result) != 0) return 20;
                if (pcc_thread_join(collector, &collector_result) != 0) return 21;
                if (mutator_result != NULL) {
                    fprintf(
                        stderr,
                        "mutator result=%lld\n",
                        (long long)(intptr_t)mutator_result
                    );
                    return 22;
                }
                if (collector_result != NULL) {
                    fprintf(
                        stderr,
                        "collector result=%lld\n",
                        (long long)(intptr_t)collector_result
                    );
                    return 23;
                }
                if (
                    __atomic_load_n(&mutation_committed, __ATOMIC_ACQUIRE) != 1
                    || __atomic_load_n(&world_acquired, __ATOMIC_ACQUIRE) != 1
                    || __atomic_load_n(&collector_done, __ATOMIC_ACQUIRE) != 1
                ) return 24;
                container = pcc_gc_load_ptr(NULL, &set_root);
                if (container != moved_target) return 25;
                if (pcc_refcount_load(&py_header(container)->refcount) != 1) {
                    return 26;
                }
#if PCC_PROBE_LIST
                if (py_list_len(container) != 5) return 27;
                PyObject *last = py_list_get(container, 4);
                if (last == NULL) return 28;
                PyObject *last_value = pcc_gc_load_ptr(
                    last, &((PyTupleObject *)last)->items[0]
                );
                int overflow = 0;
                int64_t number = py_int_to_i64(last_value, &overflow);
                pcc_gc_release(last);
                if (overflow || number != 8004) return 33;
#else
                if (py_set_len(container) != 6) return 27;
                PyObject *probe = py_tuple_new(1);
                if (probe == NULL) return 28;
                py_tuple_set_item(probe, 0, py_int_from_i64(6005));
                int64_t found = py_set_contains(container, probe);
                pcc_gc_release(probe);
                if (found != 1) return 33;
#endif

                pcc_gc_release(growth_key);
                pcc_gc_store_root(&set_root, NULL);
                pcc_gc_scheduler_root_unregister_handle(root_handle);
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
            f"{kind} real {container_kind} growth/STW probe timed out: "
            f"stdout={exc.stdout!r} stderr={exc.stderr!r}"
        )
    assert run.returncode == 0, (
        f"{kind} real {container_kind} growth/STW probe returned "
        f"{run.returncode}: "
        + run.stdout
        + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_generational_owner_referent_worklist_unlocks_between_slot_batches(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="generational_owner_referent_batches",
        source_text=r'''
            #include "py_internal.h"
            #include <pthread.h>
            #include <sched.h>
            #include <stdint.h>
            #include <stdio.h>

            extern void pcc_gc_backend3_promotion_probe_config(
                int64_t pause
            );
            extern int64_t pcc_gc_backend3_promotion_probe_state(void);

            static PyObject *owner;
            static int64_t worker_result;

            static void *promote_worker(void *unused) {
                (void)unused;
                if (pcc_current_thread_id() <= 0) return (void *)1;
                worker_result = pcc_gc_step(1024);
                pcc_thread_unregister_current();
                return 0;
            }

            int main(void) {
                static const int32_t frame_map[1] = {1};
                PyObject *roots[1] = {0};
                if (pcc_threads_enabled() != 1) return 2;
                if (pcc_current_thread_id() <= 0) return 3;
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
                    ) != 0) return 4;

                roots[0] = py_list_new(0);
                if (roots[0] == 0) return 5;
                pcc_gc_frame_enter(frame_map, roots);
                (void)pcc_gc_step(1024);
                if ((py_header(roots[0])->flags & PY_FLAG_GC_YOUNG) != 0) {
                    return 6;
                }
                owner = roots[0];
                for (int i = 0; i < 40; i++) {
                    PyObject *child = py_list_new(0);
                    if (child == 0) return 10 + i;
                    py_list_append(owner, child);
                    py_decref(child);
                }

                pcc_gc_backend3_promotion_probe_config(1);
                pthread_t thread;
                if (pthread_create(&thread, 0, promote_worker, 0) != 0) {
                    return 60;
                }
                int spins = 0;
                while (
                    pcc_gc_backend3_promotion_probe_state() == 0
                    && spins < 20000000
                ) {
                    sched_yield();
                    spins++;
                }
                if (pcc_gc_backend3_promotion_probe_state() != 1) return 61;

                /* The worker is paused after the first 16 logical slots and
                 * must not retain the graph lock.  This call takes that same
                 * lock, then a tail append proves the next tenure resolves
                 * the current list payload rather than a saved raw slot. */
                if (pcc_gc_object_is_known(owner) != 1) return 62;
                PyObject *late = py_list_new(0);
                if (late == 0) return 63;
                py_list_append(owner, late);
                py_decref(late);

                pcc_gc_backend3_promotion_probe_config(0);
                void *thread_result = 0;
                if (pthread_join(thread, &thread_result) != 0) return 64;
                if (thread_result != 0) return 65;
                if (worker_result <= 16) return 66;
                if (pcc_gc_backend3_promotion_probe_state() != 2) return 67;

                PyListObject *list = (PyListObject *)owner;
                if (list->length != 41) return 68;
                for (int i = 0; i < 41; i++) {
                    PyObject *child = list->items[i];
                    if (child == 0) return 70 + i;
                    int32_t flags = py_header(child)->flags;
                    if (
                        (flags & PY_FLAG_GC_YOUNG) != 0
                        || (flags & PY_FLAG_GC_OLD) == 0
                        || (flags & PY_FLAG_GC_MINOR_ARENA) != 0
                    ) return 120 + i;
                }
                pcc_gc_frame_leave(roots);
                py_decref(owner);
                pcc_thread_unregister_current();
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} owner-referent batch probe returned {run.returncode}: "
        + run.stdout
        + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_colored_owner_wide_barrier_drains_through_logical_slot_worklist(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="colored_owner_wide_slot_worklist",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdlib.h>

            extern int64_t pcc_gc_backend4_step_remembered_roots(
                int64_t budget
            );

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 2;
                PyListObject *owner = (PyListObject *)pcc_gc_alloc(
                    sizeof(PyListObject), PY_TYPE_LIST, PY_FLAG_GC_OLD
                );
                if (owner == 0) return 3;
                owner->length = 40;
                owner->capacity = 40;
                owner->items = (PyObject **)calloc(40, sizeof(PyObject *));
                if (owner->items == 0) return 4;

                PyObject *children[40];
                for (int i = 0; i < 40; i++) {
                    children[i] = py_list_new(0);
                    if (children[i] == 0) return 10 + i;
                    py_incref(children[i]);
                    owner->items[i] = children[i];
                }
                pcc_gc_note_write_barrier(
                    (PyObject *)owner, children[0]
                );
                if (pcc_gc_backend4_store_buffer_entries() != 1) return 60;
                if (pcc_gc_backend4_step_remembered_roots(64) != 41) {
                    return 61;
                }
                if (pcc_gc_backend4_store_buffer_entries() != 0) return 62;
                for (int i = 0; i < 40; i++) {
                    int32_t flags = py_header(children[i])->flags;
                    if (
                        (flags & PY_FLAG_GC_YOUNG) != 0
                        || (flags & PY_FLAG_GC_OLD) == 0
                    ) return 70 + i;
                    pcc_gc_store_ptr(
                        (PyObject *)owner, &owner->items[i], 0
                    );
                    py_decref(children[i]);
                }
                py_decref((PyObject *)owner);
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} GC4 owner-wide worklist probe returned {run.returncode}: "
        + run.stdout
        + run.stderr
    )
