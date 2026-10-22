from __future__ import annotations

from pathlib import Path
import subprocess
import textwrap


REPO_ROOT = Path(__file__).absolute().parents[2]
RUNTIME_SRC = REPO_ROOT / "pcc" / "runtime" / "src"
RUNTIME_PY = REPO_ROOT / "pcc" / "runtime" / "py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_capi_internal_owner_slots_follow_gc_slot_contract():
    """C oracle and production pcc-Python owners share one slot contract."""

    type_expectations = {
        "pcc_capi_contextvar_type": (
            "pcc_capi_contextvar_traverse",
            "pcc_capi_contextvar_dealloc",
        ),
        "pcc_capi_seqiter_type": (
            "pcc_capi_seqiter_traverse",
            "pcc_capi_seqiter_dealloc",
        ),
        "pcc_capi_slice_obj_type": (
            "pcc_capi_slice_traverse",
            "pcc_capi_slice_dealloc",
        ),
    }

    production_expectations = {
        RUNTIME_PY / "py_capi_contextvar_runtime.py": (
            "pcc_capi_visit_slot(ptr_add(obj, 32), visit, arg)",
            "pcc_capi_visit_slot(ptr_add(obj, 40), visit, arg)",
            "pcc_gc_store_ptr(obj, ptr_add(obj, 32), def_obj)",
            "pcc_gc_load_ptr(var, ptr_add(var, 40))",
            "pcc_gc_load_ptr(var, ptr_add(var, 32))",
            "pcc_gc_note_slot_write_barrier(var, ptr_add(var, 40), value)",
            "pcc_gc_store_ptr(self, ptr_add(self, 40), previous)",
        ),
        RUNTIME_PY / "py_capi_seqiter_runtime.py": (
            "pcc_capi_visit_slot(ptr_add(obj, 24), visit, arg)",
            "pcc_gc_store_ptr(obj, ptr_add(obj, 24), seq)",
            "pcc_gc_load_ptr(obj, ptr_add(obj, 24))",
        ),
        RUNTIME_PY / "py_capi_slice_runtime.py": (
            "pcc_capi_visit_slot(ptr_add(obj, 24), visit, arg)",
            "pcc_capi_visit_slot(ptr_add(obj, 32), visit, arg)",
            "pcc_capi_visit_slot(ptr_add(obj, 40), visit, arg)",
            "pcc_gc_store_ptr(obj, ptr_add(obj, 24), start)",
            "pcc_gc_store_ptr(obj, ptr_add(obj, 32), stop)",
            "pcc_gc_store_ptr(obj, ptr_add(obj, 40), step)",
            "pcc_gc_load_ptr(r, ptr_add(r, 24))",
            "pcc_gc_load_ptr(r, ptr_add(r, 32))",
            "pcc_gc_load_ptr(r, ptr_add(r, 40))",
        ),
    }
    for path, needles in production_expectations.items():
        source = _read(path)
        for needle in needles:
            assert needle in source, f"{path.name} missing {needle!r}"


def test_tls_exception_accessors_heal_forwarded_owner_reference():

    py_src = _read(RUNTIME_PY / "py_exc_tls.py")
    helper = py_src.split("def _resolve_current_exception():", 1)[1].split(
        '@c_abi_export("py_current_exception")', 1
    )[0]
    for needle in (
        "pcc_gc_note_relocation_read(cur)",
        "py_incref(resolved)",
        "py_tls_exc_set(resolved)",
        "py_decref(cur)",
    ):
        assert needle in helper
    assert py_src.count("cur = _resolve_current_exception()") == 2
    assert "return _resolve_current_exception()" in py_src

    py_traceback = _read(RUNTIME_PY / "py_exc_traceback.py")
    assert py_traceback.count("saved_exc = py_current_exception()") == 2


def test_pcc_python_ports_mirror_gc_store_ptr_paths():
    expectations = {
        RUNTIME_PY / "py_list.py": [
            "store_plan, lst, ptr_add(items, length * 8), item",
            "store_plan, lst, ptr_add(items, idx * 8), item",
            "store_plans, lst, ptr_add(items, i * 8), right",
            "ptr_add(store_plans, 128), lst, ptr_add(items, j * 8), left",
        ],
        RUNTIME_PY / "py_tuple.py": [
            "pcc_gc_store_ptr(tuple_ptr, slot, item)",
        ],
        # dict/set ports mirror the C transactional plan API; the barrier
        # discipline is the plan commit, so pin the commit argument shape.
        RUNTIME_PY / "py_dict.py": [
            "ptr_add(entries, entry_off + DICTENTRY_KEY_OFFSET),\n                key,",
            "ptr_add(entries, entry_off + DICTENTRY_VALUE_OFFSET),\n                value,",
        ],
        RUNTIME_PY / "py_set.py": [
            "ptr_add(entries, slot_off + 8),\n                    item,",
            "ptr_add(entries, slot_off + 8),\n                    dummy,",
        ],
        RUNTIME_PY / "py_class.py": [
            "pcc_gc_store_ptr(inst, ptr_add(fields_base, idx * C_POINTER_SIZE), value)",
            "pcc_gc_store_ptr(inst, dyn_slot, dyn)",
        ],
        RUNTIME_PY / "py_func.py": [
            "pcc_gc_store_ptr(fn, ptr_add(fn, 64), captures)",
        ],
    }
    for path, needles in expectations.items():
        src = _read(path)
        assert 'extern("pcc_gc_store_ptr"' in src
        for needle in needles:
            assert needle in src, f"{path} missing {needle!r}"


def test_list_reverse_retains_borrowed_items_across_barrier_swap():
    c_order = [
        "PyObject *left = pcc_gc_load_ptr(lst, &l->items[i]);",
        "PyObject *right = pcc_gc_load_ptr(lst, &l->items[j]);",
        "left = pcc_gc_retain_plan_prepare_locked(&left_plan, left);",
        "right = pcc_gc_retain_plan_prepare_locked(&right_plan, right);",
        "pcc_gc_store_ptr_plan_commit_locked(\n            &left_store_plan",
        "pcc_gc_store_ptr_plan_commit_locked(\n            &right_store_plan",
        "pcc_gc_root_slot_unlock();",
        "pcc_gc_store_ptr_plan_finish(&left_store_plan);",
        "pcc_gc_store_ptr_plan_finish(&right_store_plan);",
        "pcc_gc_retain_plan_finish(&left_plan);",
        "pcc_gc_retain_plan_finish(&right_plan);",
        "py_decref(left);",
        "py_decref(right);",
    ]
    pos = -1

    py_src = _read(RUNTIME_PY / "py_list.py")
    py_block = py_src.split("def py_list_reverse(lst) -> None:", 1)[1]
    py_fast = py_block.split("list_slot = stack_alloc(8)", 1)[0]
    assert py_fast.index("py_incref(fast_left)") < py_fast.index(
        "pcc_gc_store_ptr("
    ) < py_fast.index("py_decref(fast_left)")
    py_moving = py_block.split("list_slot = stack_alloc(8)", 1)[1]
    py_order = [
        "left = pcc_gc_load_ptr(lst, ptr_add(items, i * 8))",
        "right = pcc_gc_load_ptr(lst, ptr_add(items, j * 8))",
        "left = pcc_gc_retain_plan_prepare_locked(retain_plans, left)",
        "right = pcc_gc_retain_plan_prepare_locked(",
        "pcc_gc_store_ptr_plan_commit_locked(\n            store_plans",
        "pcc_gc_store_ptr_plan_commit_locked(\n            ptr_add(store_plans, 128)",
        "pcc_py_gc_minor_graph_unlock()",
        "pcc_gc_store_ptr_plan_finish(store_plans)",
        "pcc_gc_store_ptr_plan_finish(ptr_add(store_plans, 128))",
        "pcc_gc_retain_plan_finish(retain_plans)",
        "pcc_gc_retain_plan_finish(ptr_add(retain_plans, 56))",
        "py_decref(left)",
        "py_decref(right)",
    ]
    pos = -1
    for needle in py_order:
        next_pos = py_moving.find(needle)
        assert next_pos > pos, f"py_list.py reverse swap order missing/out of order: {needle!r}"
        pos = next_pos


def test_hash_rehash_move_stores_route_through_slot_write_barrier():
    """Hash-table rehash moves owned element pointers to a fresh table.

    A rehash relocates each live key (dict also its value) from the old
    backing array into a newly allocated one via a raw move-store (no
    incref/decref, since the reference is moved not copied).  That raw store
    still has to notify the collector's slot barrier so backend #3's
    generational remembered set and backend #4's relocation slot tracking
    observe the *new* slot address; otherwise a live young key reachable only
    through the moved slot can be missed by a minor/relocating collection.

    Rehash must also retarget any already-pending GC4 store-buffer and
    remembered-slot entry before the old backing table is freed.  The
    callback-free copy, side-table/span retarget, move barriers, owner-base
    publication and raw-base retirement therefore have one graph/no-park
    transaction in both runtime implementations.
    """

    dict_py = _read(RUNTIME_PY / "py_dict.py")
    dict_py_rehash = dict_py.split(
        "def _rehash(d, new_capacity: int) -> int:", 1
    )[1].split("def _maybe_grow", 1)[0]
    assert dict_py_rehash.index(
        "pcc_gc_scheduler_root_register_handle(owner_slot)"
    ) < dict_py_rehash.index("new_indices = malloc(")
    assert dict_py_rehash.index(
        "pcc_gc_backend4_retarget_mutator_payload_locked("
    ) < dict_py_rehash.index(
        "pcc_gc_note_slot_write_barrier("
    ) < dict_py_rehash.index(
        "store_ptr(d, PYDICTOBJECT_ENTRIES_OFFSET, new_entries)"
    )
    assert dict_py_rehash.index("pcc_py_gc_minor_graph_unlock()") < (
        dict_py_rehash.rindex("free(old_entries)")
    )
    dict_py_raw_lookup = dict_py.split(
        "def _rehash_find_empty_slot(", 1
    )[1].split("def _rehash(", 1)[0]
    assert "py_obj_eq" not in dict_py_raw_lookup
    assert "_lookup(" not in dict_py_raw_lookup
    dict_py_fast = dict_py.split(
        "def _rehash_refcount_fast(", 1
    )[1].split("def _rehash(", 1)[0]
    for forbidden in (
        "pcc_py_gc_minor_graph_lock",
        "pcc_gc_scheduler_root_register_handle",
        "slot_pairs",
        "pcc_gc_note_slot_write_barrier",
        "pcc_gc_backend4_retarget_mutator_payload_locked",
    ):
        assert forbidden not in dict_py_fast
    assert dict_py_rehash.index(
        "return _rehash_refcount_fast(d, new_capacity)"
    ) < dict_py_rehash.index("pcc_gc_scheduler_root_register_handle")

    set_py = _read(RUNTIME_PY / "py_set.py")
    set_py_rehash = set_py.split(
        "def _rehash(s, new_capacity: int) -> int:", 1
    )[1].split("def _maybe_grow", 1)[0]
    assert set_py_rehash.index(
        "pcc_gc_scheduler_root_register_handle(owner_slot)"
    ) < set_py_rehash.index("new_entries = _alloc_entries(")
    assert set_py_rehash.index(
        "pcc_gc_backend4_retarget_mutator_payload_locked("
    ) < set_py_rehash.index(
        "pcc_gc_note_slot_write_barrier("
    ) < set_py_rehash.index("store_ptr(s, 40, new_entries)")
    assert set_py_rehash.index("pcc_py_gc_minor_graph_unlock()") < (
        set_py_rehash.rindex("free(old_entries)")
    )
    set_py_raw_lookup = set_py.split(
        "def _rehash_find_empty_slot(", 1
    )[1].split("def _rehash(", 1)[0]
    assert "py_obj_eq" not in set_py_raw_lookup
    assert "_lookup_slot(" not in set_py_raw_lookup
    set_py_fast = set_py.split(
        "def _rehash_refcount_fast(", 1
    )[1].split("def _rehash(", 1)[0]
    for forbidden in (
        "pcc_py_gc_minor_graph_lock",
        "pcc_gc_scheduler_root_register_handle",
        "slot_pairs",
        "pcc_gc_note_slot_write_barrier",
        "pcc_gc_backend4_retarget_mutator_payload_locked",
    ):
        assert forbidden not in set_py_fast
    assert set_py_rehash.index(
        "return _rehash_refcount_fast(s, new_capacity)"
    ) < set_py_rehash.index("pcc_gc_scheduler_root_register_handle")


def test_list_extend_element_store_matches_append_slot_write_barrier():
    """list.extend must route grown-slot element stores through the barrier.

    ``py_list_append`` writes each appended element into freshly grown capacity
    by NULL-initing the slot (``py_list_new``/grow leave ``items[]`` unzeroed,
    so the slot holds garbage) and then calling ``pcc_gc_store_ptr`` — which
    internally emits ``pcc_gc_note_slot_write_barrier`` and does incref-new /
    decref-old.  ``py_list_extend``'s fast paths (list source and tuple source)
    used to bypass that with ``py_incref(v)`` + a raw ``items[len++] = v`` store,
    so backend #3's remembered set / backend #4's relocation slot tracking never
    observed the new slot — an asymmetry with its own sibling ``py_list_append``.

    This guards that both extend fast paths now use append's exact idiom
    (NULL-init the fresh slot, then barrier-store, no separate ``py_incref`` so
    the net accounting stays +1 owned ref) in both the C runtime and the
    pcc-Python port, and that the old raw store is gone.  The iterator fallback
    branch already delegates to ``py_list_append`` and is unaffected.
    """
    # --- C runtime: py_list.c -------------------------------------------

    # The template sibling must still route append's grown-slot store through
    # the barrier helper (this is the idiom extend copies).

    # Both fast paths (list + tuple source) NULL-init then barrier-store.
    # The old raw store (and its double-incref) must be gone.

    # --- pcc-Python port: py_list.py ------------------------------------
    py_src = _read(RUNTIME_PY / "py_list.py")
    assert 'extern("pcc_gc_store_ptr"' in py_src

    # Template sibling (append) idiom in the port.
    assert "store_ptr(items, length * 8, null())" in py_src
    assert "store_plan, lst, ptr_add(items, length * 8), item" in py_src

    py_extend = py_src.split("def py_list_extend(a, b) -> None:", 1)[1]
    py_extend = py_extend.split("def py_list_insert", 1)[0]

    assert py_extend.count("store_ptr(a_items, (la + i) * 8, null())") == 2, (
        "py_list.py extend must NULL-init the fresh slot in both fast paths"
    )
    assert py_extend.count("pcc_gc_store_ptr_plan_commit_locked(") == 2, (
        "py_list.py extend must locked-plan-store in both moving paths"
    )
    assert py_extend.count("pcc_gc_store_ptr_plan_finish(store_plan)") == 2
    assert "store_ptr(a_items, (la + i) * 8, v)" not in py_extend, (
        "py_list.py extend still has the raw, unbarriered element store"
    )
    assert "py_incref(v)" not in py_extend, (
        "py_list.py extend must not py_incref(v) once pcc_gc_store_ptr increfs it"
    )


def test_list_capacity_growth_retargets_raw_slots_and_preserves_backend0_fast_path():

    strict_src = _read(RUNTIME_PY / "py_list.py")
    strict_grow = strict_src.split("def _grow_if_needed(l, want: int):", 1)[
        1
    ].split("def _normalize_index", 1)[0]
    strict_fast = strict_grow.split("if initial_backend == 0 and threads == 0:", 1)[1].split(
        "owner_slot = stack_alloc(8)", 1
    )[0]
    assert "realloc(" in strict_fast
    for forbidden in (
        "pcc_py_gc_minor_graph_lock",
        "pcc_gc_scheduler_root_register_handle",
        "slot_pairs",
        "pcc_gc_note_slot_write_barrier",
        "pcc_gc_backend4_retarget_mutator_payload_locked",
    ):
        assert forbidden not in strict_fast
    assert strict_grow.index(
        "pcc_gc_scheduler_root_register_handle(owner_slot)"
    ) < strict_grow.index("new_items = malloc(")
    assert strict_grow.index("pcc_py_gc_minor_graph_unlock()") < (
        strict_grow.index("new_items = malloc(")
    ) < strict_grow.index(
        "pcc_py_gc_minor_graph_lock()",
        strict_grow.index("new_items = malloc("),
    )
    strict_retarget = strict_grow.index(
        "pcc_gc_backend4_retarget_mutator_payload_locked("
    )
    strict_barrier = strict_grow.index(
        "pcc_gc_note_slot_write_barrier(", strict_retarget
    )
    strict_publish = strict_grow.index(
        "store_ptr(l, PYLISTOBJECT_ITEMS_OFFSET, new_items)", strict_barrier
    )
    assert strict_retarget < strict_barrier < strict_publish < strict_grow.rindex(
        "pcc_py_gc_minor_graph_unlock()"
    ) < strict_grow.index("free(old_items)")
    assert "store_ptr(list_slot, 0, grown)" in strict_src
    strict_push = strict_src.split("def _push_to_list(out, v):", 1)[1].split(
        "def _seq_len", 1
    )[0]
    assert "pcc_gc_store_ptr_plan_commit_locked(" in strict_push
    assert strict_push.index("pcc_py_gc_minor_graph_unlock()") < (
        strict_push.index("pcc_gc_store_ptr_plan_finish(store_plan)")
    )
    assert "py_incref(v)" not in strict_push


def test_list_growth_callers_root_and_reload_retained_managed_inputs():

    strict_src = _read(RUNTIME_PY / "py_list.py")
    strict_append = strict_src.split("def py_list_append(lst, item) -> None:", 1)[
        1
    ].split('@c_abi_export("py_list_append_fresh_native_instance")', 1)[0]
    strict_append_fast = strict_append.split("if pcc_gc_backend() == 0 and pcc_threads_enabled() == 0:", 1)[
        1
    ].split("if _append_rooted", 1)[0]
    for forbidden in (
        "_prepare_moving_root",
        "pcc_py_gc_minor_graph_lock",
        "pcc_gc_scheduler_root_register_handle",
    ):
        assert forbidden not in strict_append_fast
    assert "_append_rooted(lst, item, 0)" in strict_append
    strict_append = strict_src.split("def _append_rooted(lst, item, fresh: int) -> int:", 1)[1].split(
        '@c_abi_export("py_list_append")', 1
    )[0]
    strict_list_prepare = strict_append.index(
        "_prepare_moving_root(list_slot, list_handle_slot)"
    )
    strict_item_prepare = strict_append.index(
        "_prepare_moving_root(item_slot, item_handle_slot)"
    )
    strict_grow_call = strict_append.index(
        "_grow_if_needed(", strict_item_prepare
    )
    strict_lock = strict_append.index("pcc_py_gc_minor_graph_lock()")
    strict_reload = strict_append.index(
        "item = _reload_moving_root(item_slot, item_handle_slot)", strict_lock
    )
    strict_store = strict_append.index(
        "pcc_gc_store_ptr_plan_commit_locked(", strict_reload
    )
    strict_unlock = strict_append.index(
        "pcc_py_gc_minor_graph_unlock()", strict_store
    )
    assert strict_list_prepare < strict_item_prepare < strict_lock < (
        strict_reload
    ) < strict_store < strict_unlock < strict_append.index(
        "pcc_gc_store_ptr_plan_finish(store_plan)", strict_unlock
    ) < strict_append.rindex("_finish_moving_root(")
    assert strict_grow_call > strict_append.rindex(
        "pcc_py_gc_minor_graph_unlock()", 0, strict_grow_call
    )
    assert strict_append.index("length: int = load_i64(") > strict_lock
    assert strict_append.index("capacity: int = load_i64(") > strict_lock

    strict_extend = strict_src.split("def py_list_extend(a, b) -> None:", 1)[
        1
    ].split('@c_abi_export("py_list_insert")', 1)[0]
    assert "_prepare_moving_root(list_slot, list_handle_slot)" in strict_extend
    assert "_prepare_moving_root(source_slot, source_handle_slot)" in strict_extend
    assert "a = _reload_moving_root(list_slot, list_handle_slot)" in strict_extend
    assert "b = _reload_moving_root(source_slot, source_handle_slot)" in strict_extend
    assert "pcc_py_gc_minor_graph_lock()" in strict_extend
    strict_push = strict_src.split("def _push_to_list(out, v):", 1)[1].split(
        "def _seq_len", 1
    )[0]
    assert "_prepare_moving_root(out_slot, out_handle_slot)" in strict_push
    assert "_prepare_moving_root(value_slot, value_handle_slot)" in strict_push
    assert "pcc_py_gc_minor_graph_lock()" in strict_push
    strict_slice = strict_src.split("def py_list_slice(lst, lo, hi, step):", 1)[
        1
    ].split('@c_abi_export("py_list_del_slice")', 1)[0]
    assert "_prepare_moving_root(source_slot, source_handle_slot)" in strict_slice
    assert "lst = _reload_moving_root(source_slot, source_handle_slot)" in strict_slice
    strict_set_slice_src = _read(RUNTIME_PY / "py_list_set_slice.py")
    strict_set_slice = strict_set_slice_src.split(
        "def py_list_set_slice(lst, lo, hi, step, replacement) -> int:", 1
    )[1]
    assert "realloc(" not in strict_set_slice_src
    assert "_snapshot_set_slice_replacement(replacement)" in strict_set_slice
    assert "new_items = malloc(new_capacity * 8)" in strict_set_slice
    assert "pcc_gc_backend4_retarget_mutator_payload_locked(" in strict_set_slice
    assert "_prepare_moving_root(list_slot, list_handle_slot)" in strict_set_slice
    assert (
        "_prepare_moving_root(replacement_slot, replacement_handle_slot)"
        in strict_set_slice
    )
    assert "lst = _reload_moving_root(list_slot, list_handle_slot)" in strict_set_slice
    assert "replacement = _reload_moving_root(" in strict_set_slice


def test_list_get_pop_reverse_use_callback_free_graph_transactions():

    strict_src = _read(RUNTIME_PY / "py_list.py")
    strict_get = strict_src.split("def py_list_get(lst, i: int):", 1)[1].split(
        '@c_abi_export("py_list_getitem")', 1
    )[0]
    strict_get_fast = strict_get.split("if pcc_gc_backend() == 0:", 1)[1].split(
        "list_slot = stack_alloc(8)", 1
    )[0]
    assert "pcc_py_gc_minor_graph_lock" not in strict_get_fast
    assert "_prepare_moving_root" not in strict_get_fast
    strict_get_lock = strict_get.index("pcc_py_gc_minor_graph_lock()")
    strict_get_load = strict_get.index("pcc_gc_load_ptr(", strict_get_lock)
    strict_get_prepare = strict_get.index(
        "pcc_gc_retain_plan_prepare_locked(", strict_get_load
    )
    strict_get_unlock = strict_get.index(
        "pcc_py_gc_minor_graph_unlock()", strict_get_prepare
    )
    assert strict_get_lock < strict_get_load < strict_get_prepare < (
        strict_get_unlock
    ) < strict_get.index("pcc_gc_retain_plan_finish(", strict_get_unlock)

    strict_pop = strict_src.split("def py_list_pop(lst, i: int):", 1)[1].split(
        '@c_abi_export("py_list_remove")', 1
    )[0]
    strict_result_register = strict_pop.index(
        "pcc_gc_scheduler_root_register_handle(result_slot)"
    )
    strict_pop_lock = strict_pop.index(
        "pcc_py_gc_minor_graph_lock()", strict_result_register
    )
    strict_result_assign = strict_pop.index(
        "store_ptr(result_slot, 0, v)", strict_pop_lock
    )
    strict_pop_move = strict_pop.index("memmove(", strict_result_assign)
    strict_pop_unlock = strict_pop.index(
        "pcc_py_gc_minor_graph_unlock()", strict_result_assign
    )
    strict_list_finish = strict_pop.index(
        "_finish_moving_root(list_handle_slot)", strict_pop_unlock
    )
    assert strict_result_register < strict_pop_lock < strict_result_assign < (
        strict_pop_move
    ) < strict_pop_unlock < strict_list_finish < strict_pop.rindex(
        "pcc_gc_scheduler_root_unregister_handle(result_handle)"
    )

    strict_reverse = strict_src.split("def py_list_reverse(lst) -> None:", 1)[1]
    strict_reverse_lock = strict_reverse.index("pcc_py_gc_minor_graph_lock()")
    strict_reverse_prepare = strict_reverse.index(
        "pcc_gc_retain_plan_prepare_locked(", strict_reverse_lock
    )
    strict_reverse_store = strict_reverse.index(
        "pcc_gc_store_ptr_plan_commit_locked(", strict_reverse_prepare
    )
    strict_reverse_unlock = strict_reverse.index(
        "pcc_py_gc_minor_graph_unlock()", strict_reverse_store
    )
    strict_reverse_finish = strict_reverse.index(
        "pcc_gc_retain_plan_finish(", strict_reverse_unlock
    )
    assert strict_reverse_lock < strict_reverse_prepare < strict_reverse_store < (
        strict_reverse_unlock
    ) < strict_reverse_finish < strict_reverse.index(
        "py_decref(left)", strict_reverse_finish
    )
    strict_snapshot = strict_src.split("def _append_snapshot_items(", 1)[1].split(
        '@c_abi_export("py_list_concat")', 1
    )[0]
    assert "_prepare_moving_root(out_slot, out_handle_slot)" in strict_snapshot
    assert "_prepare_moving_root(source_slot, source_handle_slot)" in strict_snapshot
    assert "value = py_list_get(source, i)" in strict_snapshot
    assert "py_list_append(out, value)" in strict_snapshot
    for fn, next_export in (
        ("py_list_concat", "py_list_copy"),
        ("py_list_copy", "py_list_repeat"),
        ("py_list_repeat", "py_list_contains"),
    ):
        body = strict_src.split(f'def {fn}(', 1)[1].split(
            f'@c_abi_export("{next_export}")', 1
        )[0]
        assert "_append_snapshot_items(" in body


def test_list_equality_callbacks_run_unlocked_and_remove_reloads_current_index():

    strict_src = _read(RUNTIME_PY / "py_list.py")
    strict_eq = strict_src.split("def _list_eq_at_callback(", 1)[1].split(
        "def _list_equality_scan", 1
    )[0]
    strict_eq_lock = strict_eq.index("pcc_py_gc_minor_graph_lock()")
    strict_eq_prepare = strict_eq.index(
        "pcc_gc_retain_plan_prepare_locked(", strict_eq_lock
    )
    strict_eq_unlock = strict_eq.index(
        "pcc_py_gc_minor_graph_unlock()", strict_eq_prepare
    )
    strict_eq_finish = strict_eq.index(
        "pcc_gc_retain_plan_finish(", strict_eq_unlock
    )
    strict_eq_callback = strict_eq.index("py_obj_eq(", strict_eq_finish)
    strict_eq_relock = strict_eq.index(
        "pcc_py_gc_minor_graph_lock()", strict_eq_callback
    )
    strict_eq_clear = strict_eq.index(
        "store_ptr(candidate_slot, 0, null())", strict_eq_relock
    )
    strict_eq_reunlock = strict_eq.index(
        "pcc_py_gc_minor_graph_unlock()", strict_eq_clear
    )
    assert strict_eq_lock < strict_eq_prepare < strict_eq_unlock < (
        strict_eq_finish
    ) < strict_eq_callback < strict_eq_relock < strict_eq_clear < (
        strict_eq_reunlock
    ) < strict_eq.index("py_decref(candidate)", strict_eq_reunlock)
    strict_remove = strict_src.split("def py_list_remove(lst, item) -> None:", 1)[
        1
    ].split('@c_abi_export("py_list_clear")', 1)[0]
    strict_callback = strict_remove.index("_list_eq_at_callback(")
    strict_commit_lock = strict_remove.index(
        "pcc_py_gc_minor_graph_lock()", strict_callback
    )
    strict_reload = strict_remove.index(
        "lst = _reload_moving_root(list_slot, list_handle_slot)",
        strict_commit_lock,
    )
    strict_detach = strict_remove.index(
        "store_ptr(items, index * 8, null())", strict_reload
    )
    strict_commit_unlock = strict_remove.index(
        "pcc_py_gc_minor_graph_unlock()", strict_detach
    )
    strict_decref = strict_remove.index("py_decref(removed)", strict_commit_unlock)
    assert strict_callback < strict_commit_lock < strict_reload < strict_detach < (
        strict_commit_unlock
    ) < strict_decref


def test_list_clear_publishes_empty_before_split_decref_tails():

    strict_src = _read(RUNTIME_PY / "py_list.py")
    strict_clear = strict_src.split("def py_list_clear(lst) -> None:", 1)[1].split(
        '@c_abi_export("py_obj_clear")', 1
    )[0]
    strict_fast = strict_clear.split("if pcc_gc_backend() == 0:", 1)[1].split(
        "list_slot = stack_alloc(8)", 1
    )[0]
    assert "pcc_gc_store_ptr_plan_" not in strict_fast
    strict_init = strict_clear.index("pcc_gc_store_ptr_plan_init(")
    strict_lock = strict_clear.index("pcc_py_gc_minor_graph_lock()", strict_init)
    strict_commit = strict_clear.index(
        "pcc_gc_store_ptr_plan_commit_locked(", strict_lock
    )
    strict_publish = strict_clear.index(
        "store_i64(lst, PYLISTOBJECT_LENGTH_OFFSET, 0)", strict_commit
    )
    strict_unlock = strict_clear.index(
        "pcc_py_gc_minor_graph_unlock()", strict_publish
    )
    strict_finish = strict_clear.index(
        "pcc_gc_store_ptr_plan_finish(", strict_unlock
    )
    assert strict_init < strict_lock < strict_commit < strict_publish < (
        strict_unlock
    ) < strict_finish


def test_list_delete_slice_converts_bounds_before_locked_compaction_and_decref_tails():

    strict_src = _read(RUNTIME_PY / "py_list.py")
    strict_delete = strict_src.split("def py_list_del_slice(", 1)[1].split(
        '@c_abi_export("py_list_extend")', 1
    )[0]
    strict_bound_root = strict_delete.index(
        "_prepare_moving_root(step_slot, step_handle_slot)"
    )
    strict_convert = strict_delete.index("step_v = py_obj_index_i64(step)")
    strict_plan_init = strict_delete.index(
        "pcc_gc_store_ptr_plan_init(", strict_convert
    )
    strict_lock = strict_delete.index(
        "pcc_py_gc_minor_graph_lock()", strict_plan_init
    )
    strict_retarget = strict_delete.index(
        "pcc_gc_backend4_retarget_mutator_payload_locked(", strict_lock
    )
    strict_commit = strict_delete.index(
        "pcc_gc_store_ptr_plan_commit_locked(", strict_retarget
    )
    strict_publish = strict_delete.index(
        "store_i64(lst, PYLISTOBJECT_LENGTH_OFFSET, dst)", strict_commit
    )
    strict_unlock = strict_delete.index(
        "pcc_py_gc_minor_graph_unlock()", strict_publish
    )
    strict_finish = strict_delete.index(
        "pcc_gc_store_ptr_plan_finish(", strict_unlock
    )
    assert strict_bound_root < strict_convert < strict_plan_init < (
        strict_lock
    ) < strict_retarget < strict_commit < strict_publish < strict_unlock < (
        strict_finish
    )


def test_list_set_slice_snapshots_then_publishes_whole_payload_before_decref_tails():

    strict_src = _read(RUNTIME_PY / "py_list_set_slice.py")
    strict_set = strict_src.split("def _set_slice_transaction(", 1)[1]
    strict_convert = strict_set.index("step_v = py_obj_index_i64(step)")
    strict_snapshot = strict_set.index(
        "_snapshot_set_slice_replacement(replacement)", strict_convert
    )
    strict_fast = strict_set.split("if backend == 0:", 1)[1].split(
        "old_plans = null()", 1
    )[0]
    assert strict_fast.index("store_ptr(lst, 32, new_items)") < strict_fast.index(
        "py_decref(value)"
    )
    strict_plan_init = strict_set.index(
        "pcc_gc_store_ptr_plan_init(", strict_snapshot
    )
    strict_lock = strict_set.index(
        "pcc_py_gc_minor_graph_lock()", strict_plan_init
    )
    strict_retarget = strict_set.index(
        "pcc_gc_backend4_retarget_mutator_payload_locked(", strict_lock
    )
    strict_retain = strict_set.index(
        "pcc_gc_retain_plan_prepare_locked(", strict_retarget
    )
    strict_detach = strict_set.index(
        "pcc_gc_store_ptr_plan_commit_locked(", strict_retain
    )
    strict_publish = strict_set.index("store_ptr(lst, 32, new_items)", strict_detach)
    strict_unlock = strict_set.index(
        "pcc_py_gc_minor_graph_unlock()", strict_publish
    )
    strict_finish_retain = strict_set.index(
        "pcc_gc_retain_plan_finish(", strict_unlock
    )
    strict_finish_old = strict_set.index(
        "pcc_gc_store_ptr_plan_finish(", strict_finish_retain
    )
    assert strict_convert < strict_snapshot < strict_plan_init < strict_lock < (
        strict_retarget
    ) < strict_retain < strict_detach < strict_publish < strict_unlock < (
        strict_finish_retain
    ) < strict_finish_old


def test_generated_heap_stores_increment_barrier_counter_only_for_tracing_backend(tmp_path):
    """Compile real Python code and observe runtime barrier telemetry.

    backend 0 must remain the refcount-cycle fast path: store-barrier counter
    stays at zero.  backend 1 must route the same generated list/dict/instance
    stores through the GC store path and increment the counter.
    """
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "barrier_prog.py"
    exe = tmp_path / "barrier_prog.out"
    src.write_text(textwrap.dedent("""
        from pcc.extern import extern, c_int64, c_ptr, c_void, c_obj

        pcc_gc_set_backend = extern("pcc_gc_set_backend", (c_int64,), c_int64)
        pcc_gc_telemetry = extern("pcc_gc_telemetry", (c_int64,), c_int64)
        pcc_gc_telemetry_reset = extern("pcc_gc_telemetry_reset", (), c_void)
        py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_obj)
        py_set_new = extern("py_set_new", (), c_obj)
        py_set_add = extern("py_set_add", (c_ptr, c_ptr), c_void)
        py_tuple_new = extern("py_tuple_new", (c_int64,), c_obj)
        py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)

        class Box:
            pass

        def exercise() -> None:
            xs = []
            xs.append([])
            xs[0] = []
            d = {}
            d["value"] = []
            b = Box()
            b.payload = []

            # Direct runtime calls cover set and tuple construction, which are
            # not always emitted by the frontend for minimal syntax programs.
            # set elements must be hashable; a list here now correctly raises
            # TypeError and prevents the telemetry assertion from running.
            value = py_int_from_i64(7)
            s = py_set_new()
            py_set_add(s, value)
            t = py_tuple_new(1)
            py_tuple_set_item(t, 0, value)

        def run_backend(backend: int) -> int:
            pcc_gc_set_backend(backend)
            pcc_gc_telemetry_reset()
            exercise()
            return pcc_gc_telemetry(1)  # PCC_GC_COUNTER_WRITE_BARRIERS

        def main() -> None:
            print(run_backend(0))
            print(run_backend(1) > 0)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")

    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["0", "True"]
