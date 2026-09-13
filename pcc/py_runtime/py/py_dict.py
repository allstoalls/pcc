"""Phase 4c.12: pcc-Python port of py_dict.c.

Compact-dict: indices[] is the open-addressing probe table holding
i64 slots that are either PY_DICT_EMPTY (-1), PY_DICT_TOMBSTONE (-2),
or an index into entries[]. entries[] is insertion-ordered for
deterministic iteration.

The concrete PyDictObject and DictEntry layouts are consumed exclusively via
the generated C-header-derived ``py_abi_constants`` module below.  Numeric
layout copies do not belong in this docstring because the generator cannot
update prose.

PY_DICT_EMPTY    = -1
PY_DICT_TOMBSTONE= -2
INITIAL_CAPACITY = 8 (must be power of 2). Public object layout and type tags
come from the generated C-header-derived py_abi_constants module.
"""

__pcc_runtime_port__ = True

from pcc.extern import extern, c_abi_export, c_ptr, c_int32, c_int64, c_void
from pcc.py_runtime.py.py_abi_constants import (
    DICTENTRY_HASH_OFFSET,
    DICTENTRY_KEY_OFFSET,
    DICTENTRY_SIZE,
    DICTENTRY_VALUE_OFFSET,
    PYDICTOBJECT_CAPACITY_OFFSET,
    PYDICTOBJECT_ENTRIES_OFFSET,
    PYDICTOBJECT_ENTRIES_USED_OFFSET,
    PYDICTOBJECT_INDICES_OFFSET,
    PYDICTOBJECT_ITEM_COUNT_OFFSET,
    PYDICTOBJECT_SIZE,
    PYOBJECTHEADER_TYPE_TAG_OFFSET,
    PY_TYPE_DICT,
    PY_TYPE_STR,
)
from pcc.unsafe import (
    cstr,
    free,
    global_addr,
    is_tagged_int,
    load_i32,
    ptr_add,
    load_i64,
    load_ptr,
    malloc,
    memset,
    null,
    ptr_eq,
    ptr_is_null,
    ptr_to_int,
    stack_alloc,
    store_i32,
    store_i64,
    store_ptr,
    untag_int,
)

py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
pcc_gc_backend4_zpage_register_owner_payload_span = extern(
    "pcc_gc_backend4_zpage_register_owner_payload_span",
    (c_ptr, c_ptr, c_int64),
    c_int64,
)
pcc_gc_backend = extern("pcc_gc_backend", (), c_int64)
pcc_gc_publish_initialized = extern(
    "pcc_gc_publish_initialized", (c_ptr,), c_void
)
pcc_gc_scheduler_root_register_handle = extern(
    "pcc_gc_scheduler_root_register_handle", (c_ptr,), c_ptr
)
pcc_gc_scheduler_root_unregister_handle = extern(
    "pcc_gc_scheduler_root_unregister_handle", (c_ptr,), c_void
)
pcc_py_gc_minor_graph_lock = extern(
    "pcc_py_gc_minor_graph_lock", (), c_void
)
pcc_py_gc_minor_graph_unlock = extern(
    "pcc_py_gc_minor_graph_unlock", (), c_void
)
pcc_gc_store_ptr_plan_init = extern(
    "pcc_gc_store_ptr_plan_init", (c_ptr, c_ptr, c_int64), c_void
)
pcc_gc_store_ptr_plan_commit_locked = extern(
    "pcc_gc_store_ptr_plan_commit_locked",
    (c_ptr, c_ptr, c_ptr, c_ptr),
    c_int64,
)
pcc_gc_store_ptr_plan_finish = extern(
    "pcc_gc_store_ptr_plan_finish", (c_ptr,), c_void
)
pcc_gc_backend4_retarget_mutator_payload_locked = extern(
    "pcc_gc_backend4_retarget_mutator_payload_locked",
    (c_ptr, c_ptr, c_int64, c_ptr, c_int64, c_ptr, c_int64),
    c_int64,
)
py_obj_hash = extern("py_obj_hash", (c_ptr,), c_int64)
py_obj_eq = extern("py_obj_eq", (c_ptr, c_ptr), c_int32)
py_str_eq = extern("py_str_eq", (c_ptr, c_ptr), c_int32)
py_str_hash = extern("py_str_hash", (c_ptr,), c_int64)
py_gc_track = extern("py_gc_track", (c_ptr,), c_void)
pcc_gc_store_ptr = extern("pcc_gc_store_ptr", (c_ptr, c_ptr, c_ptr), c_void)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_note_slot_write_barrier = extern(
    "pcc_gc_note_slot_write_barrier",
    (c_ptr, c_ptr, c_ptr),
    c_void,
)
pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_ptr)

py_list_new = extern("py_list_new", (c_int64,), c_ptr)
py_list_append = extern("py_list_append", (c_ptr, c_ptr), c_void)
py_list_len = extern("py_list_len", (c_ptr,), c_int64)
py_list_get = extern("py_list_get", (c_ptr, c_int64), c_ptr)
py_list_extend = extern("py_list_extend", (c_ptr, c_ptr), c_void)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_exc_new_with_value = extern("py_exc_new_with_value", (c_int64, c_ptr), c_ptr)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs the exception it stores in TLS, so a caller that created
# it still owns a reference.  py_raise_owned raises and releases that
# caller reference in one step, matching the C runtime.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_obj_iter = extern("py_obj_iter", (c_ptr,), c_ptr)
py_obj_next = extern("py_obj_next", (c_ptr,), c_ptr)
py_obj_getattr = extern("py_obj_getattr", (c_ptr, c_ptr), c_ptr)
py_obj_getitem = extern("py_obj_getitem", (c_ptr, c_ptr), c_ptr)
py_obj_call = extern("py_obj_call", (c_ptr, c_ptr, c_ptr), c_ptr)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_current_exception = extern("py_current_exception", (), c_ptr)
py_exc_builtin_class = extern("py_exc_builtin_class", (c_int64,), c_ptr)
py_exc_matches = extern("py_exc_matches", (c_ptr, c_ptr), c_int64)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_runtime_error_if_unset = extern(
    "py_runtime_error_if_unset", (c_ptr, c_ptr), c_ptr
)


pcc_gc_pointer_is_managed = extern(
    "pcc_gc_pointer_is_managed", (c_ptr,), c_int64
)


def _ptr_can_have_header(o) -> bool:
    return pcc_gc_pointer_is_managed(o) != 0


def _ptr_is_dict(o) -> bool:
    if not _ptr_can_have_header(o):
        return False
    return load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_DICT


def _dict_read_prepare_root(slot, value, backend: int):
    store_ptr(slot, 0, value)
    if (
        (backend == 3 or backend == 4)
        and ptr_is_null(value) == 0
        and is_tagged_int(value) == 0
    ):
        handle = pcc_gc_scheduler_root_register_handle(slot)
        if ptr_is_null(handle) == 0:
            store_ptr(slot, 0, pcc_gc_load_ptr(null(), slot))
        return handle
    return null()


def _dict_read_root_failed(value, backend: int, handle) -> int:
    if backend != 3 and backend != 4:
        return 0
    if ptr_is_null(value) != 0 or is_tagged_int(value) != 0:
        return 0
    return ptr_is_null(handle)


def _dict_read_reload_root(slot, handle):
    value = load_ptr(slot, 0)
    if ptr_is_null(handle) == 0:
        value = pcc_gc_load_ptr(null(), slot)
        store_ptr(slot, 0, value)
    return value


def _dict_read_finish_root(handle) -> None:
    if ptr_is_null(handle) == 0:
        pcc_gc_scheduler_root_unregister_handle(handle)


def _alloc_tables(d, capacity: int) -> int:
    # Returns 0 on success, -1 on alloc failure.
    indices = malloc(capacity * 8)
    if ptr_is_null(indices) != 0:
        return -1
    entries = malloc(capacity * DICTENTRY_SIZE)
    if ptr_is_null(entries) != 0:
        free(indices)
        return -1
    # Init indices[] to PY_DICT_EMPTY (-1).
    i: int = 0
    while i < capacity:
        store_i64(indices, i * 8, -1)
        i = i + 1
    # entries[] is only read for 0..entries_used, so no init.
    store_ptr(d, PYDICTOBJECT_INDICES_OFFSET, indices)
    store_ptr(d, PYDICTOBJECT_ENTRIES_OFFSET, entries)
    store_i64(d, PYDICTOBJECT_CAPACITY_OFFSET, capacity)
    store_i64(d, PYDICTOBJECT_ITEM_COUNT_OFFSET, 0)  # item count
    store_i64(d, PYDICTOBJECT_ENTRIES_USED_OFFSET, 0)  # entries_used
    pcc_gc_backend4_zpage_register_owner_payload_span(d, entries, capacity * DICTENTRY_SIZE)
    return 0


def _perturb_shift5(perturb: int) -> int:
    # Mirror ``(uint64_t)perturb >> 5`` while the pcc-Python runtime exposes
    # only signed i64 arithmetic.  Arithmetic shift differs from logical
    # shift by exactly 2**59 when the input's high bit is set.
    shifted: int = perturb >> 5
    if perturb < 0:
        shifted = shifted + 576460752303423488
    return shifted


def _entry_key(d, entries, entry_off: int):
    slot = ptr_add(entries, entry_off + DICTENTRY_KEY_OFFSET)
    k = load_ptr(slot, 0)
    if ptr_is_null(k) != 0:
        return k
    if load_i32(global_addr("pcc_gc_read_barrier_enabled"), 0) == 0:
        return k
    return pcc_gc_load_ptr(d, slot)


def _entry_value(d, entries, entry_off: int):
    slot = ptr_add(entries, entry_off + DICTENTRY_VALUE_OFFSET)
    v = load_ptr(slot, 0)
    if ptr_is_null(v) != 0:
        return v
    if load_i32(global_addr("pcc_gc_read_barrier_enabled"), 0) == 0:
        return v
    return pcc_gc_load_ptr(d, slot)


def _dict_fast0_key_kind(key) -> int:
    # 1 = tagged small int, 2 = str, 0 = everything else (rooted path).
    if is_tagged_int(key) != 0:
        return 1
    if load_i32(key, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_STR:
        return 2
    return 0


def _dict_fast0_hash(key, kind: int) -> int:
    # Neither hash can raise or run user code.
    if kind == 1:
        return py_obj_hash(key)
    return py_str_hash(key)


def _dict_probe_fast0(d, key, kind: int, hash_val: int, insert_slot) -> int:
    """Refcount-backend (GC0) probe for a str or tagged-int key.

    Nothing here allocates, runs user code or collects, so no root, lock,
    reload or plan is needed: the rooted operation below spent ~10x the
    lookup itself on that protocol for a ``d[str_key]`` hit.  Returns the
    entry index of the match; -1 when the key is absent (``insert_slot``
    then holds the index slot an insert would take, or -1 when the probe
    budget ran out); -3 when an equal-hash entry of another type needs
    ``py_obj_eq`` (``True``/``1.0`` against ``1``, a user ``__eq__`` against a
    str), which only the rooted path may run.
    """
    capacity: int = load_i64(d, PYDICTOBJECT_CAPACITY_OFFSET)
    indices = load_ptr(d, PYDICTOBJECT_INDICES_OFFSET)
    entries = load_ptr(d, PYDICTOBJECT_ENTRIES_OFFSET)
    entries_used: int = load_i64(d, PYDICTOBJECT_ENTRIES_USED_OFFSET)
    store_i64(insert_slot, 0, -1)
    if capacity <= 0 or ptr_is_null(indices) != 0 or ptr_is_null(entries) != 0:
        return -1
    mask: int = capacity - 1
    perturb: int = hash_val
    j: int = hash_val & mask
    probes: int = 0
    first_tombstone: int = -1
    while probes < capacity + 16:
        ix: int = load_i64(indices, j * 8)
        if ix == -1:
            if first_tombstone >= 0:
                store_i64(insert_slot, 0, first_tombstone)
            else:
                store_i64(insert_slot, 0, j)
            return -1
        if ix == -2:
            if first_tombstone < 0:
                first_tombstone = j
        elif ix >= 0 and ix < entries_used:
            entry_off: int = ix * DICTENTRY_SIZE
            entry_key = load_ptr(entries, entry_off + DICTENTRY_KEY_OFFSET)
            if (
                ptr_is_null(entry_key) == 0
                and load_i64(entries, entry_off + DICTENTRY_HASH_OFFSET) == hash_val
            ):
                if ptr_eq(entry_key, key) != 0:
                    return ix
                entry_tagged: int = is_tagged_int(entry_key)
                if kind == 1:
                    if entry_tagged == 0:
                        return -3
                    # Two distinct tagged ints sharing a hash (-1/-2) differ.
                elif entry_tagged == 0:
                    if load_i32(entry_key, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_STR:
                        if py_str_eq(entry_key, key) != 0:
                            return ix
                    else:
                        return -3
                # A tagged entry never equals a str key.
        perturb = _perturb_shift5(perturb)
        j = (j * 5 + perturb + 1) & mask
        probes = probes + 1
    return -1


def _dict_insert_fast0(d, key, value, slot: int, hash_val: int) -> int:
    # GC0 insert into a probed-free index slot; mirrors
    # _dict_insert_rooted_slot without the plans and the lock (the barrier
    # is the balanced pcc_gc_store_ptr).  Returns 0 when the entry table has
    # no room, which the rooted path then handles.
    capacity: int = load_i64(d, PYDICTOBJECT_CAPACITY_OFFSET)
    ei: int = load_i64(d, PYDICTOBJECT_ENTRIES_USED_OFFSET)
    if ei < 0 or ei >= capacity:
        return 0
    indices = load_ptr(d, PYDICTOBJECT_INDICES_OFFSET)
    entries = load_ptr(d, PYDICTOBJECT_ENTRIES_OFFSET)
    entry_off: int = ei * DICTENTRY_SIZE
    store_i64(entries, entry_off + DICTENTRY_HASH_OFFSET, hash_val)
    store_ptr(entries, entry_off + DICTENTRY_KEY_OFFSET, null())
    store_ptr(entries, entry_off + DICTENTRY_VALUE_OFFSET, null())
    pcc_gc_store_ptr(d, ptr_add(entries, entry_off + DICTENTRY_KEY_OFFSET), key)
    pcc_gc_store_ptr(d, ptr_add(entries, entry_off + DICTENTRY_VALUE_OFFSET), value)
    store_i64(d, PYDICTOBJECT_ENTRIES_USED_OFFSET, ei + 1)
    store_i64(indices, slot * 8, ei)
    size: int = load_i64(d, PYDICTOBJECT_ITEM_COUNT_OFFSET)
    store_i64(d, PYDICTOBJECT_ITEM_COUNT_OFFSET, size + 1)
    _maybe_grow(d)
    return 1


def _dict_insert_rooted_slot(
    dict_slot,
    dict_handle,
    key_slot,
    key_handle,
    value_slot,
    value_handle,
    indices,
    entries,
    capacity: int,
    entries_used: int,
    slot: int,
    hash_val: int,
) -> int:
    # Publish key, value, index and size under one graph lock.  A store plan
    # commits exactly one slot, so key and value need one plan each; both are
    # initialized before the lock and finished after it, keeping every
    # incref/decref finalizer outside the locked transaction.
    d = _dict_read_reload_root(dict_slot, dict_handle)
    key_plan = stack_alloc(128)
    value_plan = stack_alloc(128)
    backend: int = pcc_gc_backend()
    pcc_gc_store_ptr_plan_init(key_plan, d, backend)
    pcc_gc_store_ptr_plan_init(value_plan, d, backend)
    pcc_py_gc_minor_graph_lock()
    d = _dict_read_reload_root(dict_slot, dict_handle)
    key = _dict_read_reload_root(key_slot, key_handle)
    value = _dict_read_reload_root(value_slot, value_handle)
    committed: int = 0
    if _ptr_is_dict(d):
        packed: int = load_i64(indices, slot * 8)
        if (
            ptr_eq(load_ptr(d, PYDICTOBJECT_INDICES_OFFSET), indices) != 0
            and ptr_eq(load_ptr(d, PYDICTOBJECT_ENTRIES_OFFSET), entries) != 0
            and load_i64(d, PYDICTOBJECT_CAPACITY_OFFSET) == capacity
            and load_i64(d, PYDICTOBJECT_ENTRIES_USED_OFFSET) == entries_used
            and slot >= 0
            and slot < capacity
            and entries_used >= 0
            and entries_used < capacity
            and (packed == -1 or packed == -2)
        ):
            ei: int = entries_used
            entry_off: int = ei * DICTENTRY_SIZE
            store_i64(entries, entry_off + DICTENTRY_HASH_OFFSET, hash_val)
            store_ptr(entries, entry_off + DICTENTRY_KEY_OFFSET, null())
            store_ptr(entries, entry_off + DICTENTRY_VALUE_OFFSET, null())
            key_ok: int = pcc_gc_store_ptr_plan_commit_locked(
                key_plan,
                d,
                ptr_add(entries, entry_off + DICTENTRY_KEY_OFFSET),
                key,
            )
            value_ok: int = pcc_gc_store_ptr_plan_commit_locked(
                value_plan,
                d,
                ptr_add(entries, entry_off + DICTENTRY_VALUE_OFFSET),
                value,
            )
            if key_ok != 0 and value_ok != 0:
                store_i64(d, PYDICTOBJECT_ENTRIES_USED_OFFSET, ei + 1)
                store_i64(indices, slot * 8, ei)
                size: int = load_i64(d, PYDICTOBJECT_ITEM_COUNT_OFFSET)
                store_i64(d, PYDICTOBJECT_ITEM_COUNT_OFFSET, size + 1)
                committed = 1
            else:
                # The entry was never indexed, so it stays unreachable; plan
                # finish still balances any partial store.
                store_i64(entries, entry_off + DICTENTRY_HASH_OFFSET, 0)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_store_ptr_plan_finish(key_plan)
    pcc_gc_store_ptr_plan_finish(value_plan)
    if committed != 0:
        d = _dict_read_reload_root(dict_slot, dict_handle)
        if _ptr_is_dict(d):
            _maybe_grow(d)
    return committed


def _dict_replace_value_rooted_slot(
    dict_slot,
    dict_handle,
    value_slot,
    value_handle,
    indices,
    entries,
    capacity: int,
    slot: int,
    ix: int,
    hash_val: int,
) -> int:
    # `d[k] = v` keeps the original stored key object, so this never writes the
    # key slot.  The displaced value is released in plan finish, after unlock.
    d = _dict_read_reload_root(dict_slot, dict_handle)
    plan = stack_alloc(128)
    pcc_gc_store_ptr_plan_init(plan, d, pcc_gc_backend())
    pcc_py_gc_minor_graph_lock()
    d = _dict_read_reload_root(dict_slot, dict_handle)
    value = _dict_read_reload_root(value_slot, value_handle)
    committed: int = 0
    if _ptr_is_dict(d):
        entry_off: int = ix * DICTENTRY_SIZE
        if (
            ptr_eq(load_ptr(d, PYDICTOBJECT_INDICES_OFFSET), indices) != 0
            and ptr_eq(load_ptr(d, PYDICTOBJECT_ENTRIES_OFFSET), entries) != 0
            and load_i64(d, PYDICTOBJECT_CAPACITY_OFFSET) == capacity
            and slot >= 0
            and slot < capacity
            and load_i64(indices, slot * 8) == ix
            and ix >= 0
            and ix < load_i64(d, PYDICTOBJECT_ENTRIES_USED_OFFSET)
            and load_i64(entries, entry_off + DICTENTRY_HASH_OFFSET) == hash_val
            and ptr_is_null(_entry_key(d, entries, entry_off)) == 0
        ):
            committed = pcc_gc_store_ptr_plan_commit_locked(
                plan,
                d,
                ptr_add(entries, entry_off + DICTENTRY_VALUE_OFFSET),
                value,
            )
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_store_ptr_plan_finish(plan)
    return committed


def _dict_del_rooted_slot(
    dict_slot,
    dict_handle,
    indices,
    entries,
    capacity: int,
    slot: int,
    ix: int,
) -> int:
    # Key, value, index tombstone and size all publish under one graph lock;
    # both releases run in plan finish after unlock.  The legacy path decref'd
    # key and value first, so a finalizer re-entering the dict could observe a
    # freed key behind a still-live index.
    d = _dict_read_reload_root(dict_slot, dict_handle)
    key_plan = stack_alloc(128)
    value_plan = stack_alloc(128)
    backend: int = pcc_gc_backend()
    pcc_gc_store_ptr_plan_init(key_plan, d, backend)
    pcc_gc_store_ptr_plan_init(value_plan, d, backend)
    pcc_py_gc_minor_graph_lock()
    d = _dict_read_reload_root(dict_slot, dict_handle)
    committed: int = 0
    if _ptr_is_dict(d):
        entry_off: int = ix * DICTENTRY_SIZE
        if (
            ptr_eq(load_ptr(d, PYDICTOBJECT_INDICES_OFFSET), indices) != 0
            and ptr_eq(load_ptr(d, PYDICTOBJECT_ENTRIES_OFFSET), entries) != 0
            and load_i64(d, PYDICTOBJECT_CAPACITY_OFFSET) == capacity
            and slot >= 0
            and slot < capacity
            and load_i64(indices, slot * 8) == ix
            and ix >= 0
            and ix < load_i64(d, PYDICTOBJECT_ENTRIES_USED_OFFSET)
            and ptr_is_null(_entry_key(d, entries, entry_off)) == 0
        ):
            key_ok: int = pcc_gc_store_ptr_plan_commit_locked(
                key_plan,
                d,
                ptr_add(entries, entry_off + DICTENTRY_KEY_OFFSET),
                null(),
            )
            value_ok: int = pcc_gc_store_ptr_plan_commit_locked(
                value_plan,
                d,
                ptr_add(entries, entry_off + DICTENTRY_VALUE_OFFSET),
                null(),
            )
            if key_ok != 0 and value_ok != 0:
                store_i64(indices, slot * 8, -2)  # PY_DICT_TOMBSTONE
                size: int = load_i64(d, PYDICTOBJECT_ITEM_COUNT_OFFSET)
                store_i64(d, PYDICTOBJECT_ITEM_COUNT_OFFSET, size - 1)
                committed = 1
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_store_ptr_plan_finish(key_plan)
    pcc_gc_store_ptr_plan_finish(value_plan)
    return committed


def _dict_rooted_op(d, key, value, mode: int, status_slot):
    # mode 0: get, returning an owned value.  mode 1: delete.  mode 2: set -
    # fresh insert or value replacement.  Modes 1 and 2 return null() and
    # report through status_slot when it is non-null.
    if ptr_is_null(status_slot) == 0:
        store_i64(status_slot, 0, 0)
    backend: int = pcc_gc_backend()
    dict_slot = stack_alloc(8)
    key_slot = stack_alloc(8)
    value_slot = stack_alloc(8)
    candidate_slot = stack_alloc(8)
    dict_handle = _dict_read_prepare_root(dict_slot, d, backend)
    if _dict_read_root_failed(d, backend, dict_handle) != 0:
        return null()
    key_handle = _dict_read_prepare_root(key_slot, key, backend)
    if _dict_read_root_failed(key, backend, key_handle) != 0:
        _dict_read_finish_root(dict_handle)
        return null()
    value_handle = _dict_read_prepare_root(value_slot, value, backend)
    if _dict_read_root_failed(value, backend, value_handle) != 0:
        _dict_read_finish_root(key_handle)
        _dict_read_finish_root(dict_handle)
        return null()
    key = _dict_read_reload_root(key_slot, key_handle)
    hash_val: int = py_obj_hash(key)
    d = _dict_read_reload_root(dict_slot, dict_handle)
    key = _dict_read_reload_root(key_slot, key_handle)
    if py_err_occurred() != 0:
        _dict_read_finish_root(value_handle)
        _dict_read_finish_root(key_handle)
        _dict_read_finish_root(dict_handle)
        return null()

    result = null()
    done: int = 0
    attempts: int = 0
    while done == 0 and attempts < 16:
        attempts = attempts + 1
        d = _dict_read_reload_root(dict_slot, dict_handle)
        key = _dict_read_reload_root(key_slot, key_handle)
        if not _ptr_is_dict(d):
            done = 1
            continue
        capacity: int = load_i64(d, PYDICTOBJECT_CAPACITY_OFFSET)
        indices = load_ptr(d, PYDICTOBJECT_INDICES_OFFSET)
        entries = load_ptr(d, PYDICTOBJECT_ENTRIES_OFFSET)
        entries_used: int = load_i64(d, PYDICTOBJECT_ENTRIES_USED_OFFSET)
        if capacity <= 0 or ptr_is_null(indices) != 0 or ptr_is_null(entries) != 0:
            done = 1
            continue
        mask: int = capacity - 1
        perturb: int = hash_val
        j: int = hash_val & mask
        probes: int = 0
        restart: int = 0
        first_tombstone: int = -1
        insert_slot: int = -1
        mutated: int = 0
        while probes < capacity + 16 and done == 0 and restart == 0:
            ix: int = load_i64(indices, j * 8)
            if ix == -1:
                if first_tombstone >= 0:
                    insert_slot = first_tombstone
                else:
                    insert_slot = j
                done = 1
            elif ix == -2:
                if first_tombstone < 0:
                    first_tombstone = j
            elif ix >= 0 and ix < entries_used:
                entry_off: int = ix * DICTENTRY_SIZE
                entry_key = _entry_key(d, entries, entry_off)
                entry_hash: int = load_i64(
                    entries, entry_off + DICTENTRY_HASH_OFFSET
                )
                if ptr_is_null(entry_key) == 0 and entry_hash == hash_val:
                    equal: int = 0
                    callback: int = 0
                    if ptr_eq(entry_key, key) != 0:
                        equal = 1
                    elif is_tagged_int(entry_key) != 0 and is_tagged_int(key) != 0:
                        equal = 0
                    elif (
                        is_tagged_int(entry_key) == 0
                        and is_tagged_int(key) == 0
                        and load_i32(
                            entry_key, PYOBJECTHEADER_TYPE_TAG_OFFSET
                        ) == PY_TYPE_STR
                        and load_i32(key, PYOBJECTHEADER_TYPE_TAG_OFFSET)
                            == PY_TYPE_STR
                    ):
                        equal = py_str_eq(entry_key, key)
                    else:
                        callback = 1
                        py_incref(entry_key)
                        candidate_handle = _dict_read_prepare_root(
                            candidate_slot, entry_key, backend
                        )
                        if _dict_read_root_failed(
                            entry_key, backend, candidate_handle
                        ) != 0:
                            py_decref(entry_key)
                            done = 1
                        else:
                            before_d = d
                            equal = py_obj_eq(entry_key, key)
                            d = _dict_read_reload_root(dict_slot, dict_handle)
                            key = _dict_read_reload_root(key_slot, key_handle)
                            candidate = _dict_read_reload_root(
                                candidate_slot, candidate_handle
                            )
                            _dict_read_finish_root(candidate_handle)
                            stable: int = 0
                            if ptr_eq(d, before_d) != 0 and _ptr_is_dict(d):
                                if (
                                    load_i64(d, PYDICTOBJECT_CAPACITY_OFFSET)
                                        == capacity
                                    and ptr_eq(
                                        load_ptr(d, PYDICTOBJECT_INDICES_OFFSET),
                                        indices,
                                    ) != 0
                                    and ptr_eq(
                                        load_ptr(d, PYDICTOBJECT_ENTRIES_OFFSET),
                                        entries,
                                    ) != 0
                                    and load_i64(indices, j * 8) == ix
                                ):
                                    current = _entry_key(d, entries, entry_off)
                                    if ptr_eq(current, candidate) != 0:
                                        stable = 1
                            py_decref(candidate)
                            if py_err_occurred() != 0:
                                # A raising __eq__ leaves py_obj_eq returning
                                # 0.  Treating that as "not equal" would keep
                                # probing and, in set mode, insert -- mutating
                                # the dict even though the statement raises.
                                _dict_read_finish_root(value_handle)
                                _dict_read_finish_root(key_handle)
                                _dict_read_finish_root(dict_handle)
                                return null()
                            if stable == 0:
                                restart = 1
                    if callback == 0 or restart == 0:
                        if equal != 0 and restart == 0 and done == 0:
                            if mode == 1:
                                removed: int = _dict_del_rooted_slot(
                                    dict_slot,
                                    dict_handle,
                                    indices,
                                    entries,
                                    capacity,
                                    j,
                                    ix,
                                )
                                if removed == 0:
                                    restart = 1
                                else:
                                    if ptr_is_null(status_slot) == 0:
                                        store_i64(status_slot, 0, 1)
                                    mutated = 1
                                    done = 1
                            elif mode == 2:
                                replaced: int = _dict_replace_value_rooted_slot(
                                    dict_slot,
                                    dict_handle,
                                    value_slot,
                                    value_handle,
                                    indices,
                                    entries,
                                    capacity,
                                    j,
                                    ix,
                                    hash_val,
                                )
                                if replaced == 0:
                                    restart = 1
                                else:
                                    if ptr_is_null(status_slot) == 0:
                                        store_i64(status_slot, 0, 1)
                                    mutated = 1
                                    done = 1
                            else:
                                found = _entry_value(d, entries, entry_off)
                                if ptr_is_null(found) == 0:
                                    py_incref(found)
                                    result = found
                                done = 1
            if done == 0 and restart == 0:
                perturb = _perturb_shift5(perturb)
                j = (j * 5 + perturb + 1) & mask
                probes = probes + 1
        if mode == 2 and restart == 0 and mutated == 0:
            target: int = insert_slot
            if target < 0:
                target = first_tombstone
            if target >= 0:
                inserted: int = _dict_insert_rooted_slot(
                    dict_slot,
                    dict_handle,
                    key_slot,
                    key_handle,
                    value_slot,
                    value_handle,
                    indices,
                    entries,
                    capacity,
                    entries_used,
                    target,
                    hash_val,
                )
                if inserted == 0:
                    done = 0
                    restart = 1
                elif ptr_is_null(status_slot) == 0:
                    store_i64(status_slot, 0, 1)
    _dict_read_finish_root(value_handle)
    _dict_read_finish_root(key_handle)
    _dict_read_finish_root(dict_handle)
    return result


def _rehash_find_empty_slot(indices, capacity: int, hash_val: int) -> int:
    mask: int = capacity - 1
    perturb: int = hash_val
    slot: int = hash_val & mask
    probes: int = 0
    while probes < capacity + 16:
        if load_i64(indices, slot * 8) == -1:
            return slot
        perturb = _perturb_shift5(perturb)
        slot = (slot * 5 + perturb + 1) & mask
        probes = probes + 1
    return -1


def _rehash_refcount_fast(d, new_capacity: int) -> int:
    old_entries = load_ptr(d, PYDICTOBJECT_ENTRIES_OFFSET)
    old_indices = load_ptr(d, PYDICTOBJECT_INDICES_OFFSET)
    old_entries_used: int = load_i64(d, PYDICTOBJECT_ENTRIES_USED_OFFSET)
    new_indices = malloc(new_capacity * 8)
    new_entries = malloc(new_capacity * DICTENTRY_SIZE)
    if ptr_is_null(new_indices) != 0 or ptr_is_null(new_entries) != 0:
        free(new_entries)
        free(new_indices)
        return -1
    memset(new_entries, 0, new_capacity * DICTENTRY_SIZE)
    init_index: int = 0
    while init_index < new_capacity:
        store_i64(new_indices, init_index * 8, -1)
        init_index = init_index + 1
    new_entries_used: int = 0
    old_index: int = 0
    while old_index < old_entries_used:
        old_off: int = old_index * DICTENTRY_SIZE
        key = load_ptr(old_entries, old_off + DICTENTRY_KEY_OFFSET)
        if ptr_is_null(key) == 0:
            hash_value: int = load_i64(
                old_entries, old_off + DICTENTRY_HASH_OFFSET
            )
            target_slot: int = _rehash_find_empty_slot(
                new_indices, new_capacity, hash_value
            )
            if target_slot < 0:
                free(new_entries)
                free(new_indices)
                return -1
            new_off: int = new_entries_used * DICTENTRY_SIZE
            store_i64(
                new_entries, new_off + DICTENTRY_HASH_OFFSET, hash_value
            )
            store_ptr(new_entries, new_off + DICTENTRY_KEY_OFFSET, key)
            store_ptr(
                new_entries,
                new_off + DICTENTRY_VALUE_OFFSET,
                load_ptr(old_entries, old_off + DICTENTRY_VALUE_OFFSET),
            )
            store_i64(new_indices, target_slot * 8, new_entries_used)
            new_entries_used = new_entries_used + 1
        old_index = old_index + 1
    store_ptr(d, PYDICTOBJECT_INDICES_OFFSET, new_indices)
    store_ptr(d, PYDICTOBJECT_ENTRIES_OFFSET, new_entries)
    store_i64(d, PYDICTOBJECT_CAPACITY_OFFSET, new_capacity)
    store_i64(d, PYDICTOBJECT_ITEM_COUNT_OFFSET, new_entries_used)
    store_i64(d, PYDICTOBJECT_ENTRIES_USED_OFFSET, new_entries_used)
    free(old_indices)
    free(old_entries)
    return 0


def _rehash(d, new_capacity: int) -> int:
    if ptr_is_null(d) != 0 or new_capacity <= 0:
        return -1
    initial_backend: int = pcc_gc_backend()
    if initial_backend == 0:
        return _rehash_refcount_fast(d, new_capacity)
    owner_slot = stack_alloc(8)
    store_ptr(owner_slot, 0, d)
    owner_handle = null()
    if initial_backend == 3 or initial_backend == 4:
        owner_handle = pcc_gc_scheduler_root_register_handle(owner_slot)
        if ptr_is_null(owner_handle) != 0:
            return -1

    attempt: int = 0
    while attempt < 8:
        attempt = attempt + 1
        pcc_py_gc_minor_graph_lock()
        if pcc_gc_backend() != initial_backend:
            pcc_py_gc_minor_graph_unlock()
            break
        if ptr_is_null(owner_handle) == 0:
            d = pcc_gc_load_ptr(null(), owner_slot)
            store_ptr(owner_slot, 0, d)
        old_entries = load_ptr(d, PYDICTOBJECT_ENTRIES_OFFSET)
        old_indices = load_ptr(d, PYDICTOBJECT_INDICES_OFFSET)
        old_capacity: int = load_i64(d, PYDICTOBJECT_CAPACITY_OFFSET)
        old_entries_used: int = load_i64(
            d, PYDICTOBJECT_ENTRIES_USED_OFFSET
        )
        old_size: int = load_i64(d, PYDICTOBJECT_ITEM_COUNT_OFFSET)
        pcc_py_gc_minor_graph_unlock()
        if ptr_is_null(old_entries) != 0 or ptr_is_null(old_indices) != 0:
            break
        if old_capacity <= 0 or new_capacity < old_capacity:
            break
        if old_entries_used < 0 or old_entries_used > old_capacity:
            break
        if old_size < 0 or old_size > new_capacity:
            break

        new_indices = malloc(new_capacity * 8)
        new_entries = malloc(new_capacity * DICTENTRY_SIZE)
        slot_pairs = null()
        if old_entries_used > 0:
            slot_pairs = malloc(old_entries_used * 4 * 8)
        if (
            ptr_is_null(new_indices) != 0
            or ptr_is_null(new_entries) != 0
            or (old_entries_used > 0 and ptr_is_null(slot_pairs) != 0)
        ):
            free(slot_pairs)
            free(new_entries)
            free(new_indices)
            break
        memset(new_entries, 0, new_capacity * DICTENTRY_SIZE)
        if old_entries_used > 0:
            memset(slot_pairs, 0, old_entries_used * 4 * 8)
        init_index: int = 0
        while init_index < new_capacity:
            store_i64(new_indices, init_index * 8, -1)
            init_index = init_index + 1

        pcc_py_gc_minor_graph_lock()
        if pcc_gc_backend() != initial_backend:
            pcc_py_gc_minor_graph_unlock()
            free(slot_pairs)
            free(new_entries)
            free(new_indices)
            break
        if ptr_is_null(owner_handle) == 0:
            d = pcc_gc_load_ptr(null(), owner_slot)
            store_ptr(owner_slot, 0, d)
        if (
            ptr_eq(load_ptr(d, PYDICTOBJECT_ENTRIES_OFFSET), old_entries) == 0
            or ptr_eq(load_ptr(d, PYDICTOBJECT_INDICES_OFFSET), old_indices) == 0
            or load_i64(d, PYDICTOBJECT_CAPACITY_OFFSET) != old_capacity
            or load_i64(d, PYDICTOBJECT_ENTRIES_USED_OFFSET) != old_entries_used
            or load_i64(d, PYDICTOBJECT_ITEM_COUNT_OFFSET) != old_size
        ):
            pcc_py_gc_minor_graph_unlock()
            free(slot_pairs)
            free(new_entries)
            free(new_indices)
            continue

        new_entries_used: int = 0
        new_size: int = 0
        pair_count: int = 0
        copy_valid: int = 1
        old_index: int = 0
        while old_index < old_entries_used:
            old_off: int = old_index * DICTENTRY_SIZE
            key = _entry_key(d, old_entries, old_off)
            if ptr_is_null(key) == 0:
                hash_value: int = load_i64(
                    old_entries, old_off + DICTENTRY_HASH_OFFSET
                )
                value = _entry_value(d, old_entries, old_off)
                target_slot: int = _rehash_find_empty_slot(
                    new_indices, new_capacity, hash_value
                )
                if target_slot < 0:
                    copy_valid = 0
                    break
                new_off: int = new_entries_used * DICTENTRY_SIZE
                store_i64(
                    new_entries,
                    new_off + DICTENTRY_HASH_OFFSET,
                    hash_value,
                )
                store_ptr(new_entries, new_off + DICTENTRY_KEY_OFFSET, key)
                store_ptr(new_entries, new_off + DICTENTRY_VALUE_OFFSET, value)
                store_i64(new_indices, target_slot * 8, new_entries_used)
                store_ptr(
                    slot_pairs,
                    pair_count * 16,
                    ptr_add(old_entries, old_off + DICTENTRY_KEY_OFFSET),
                )
                store_ptr(
                    slot_pairs,
                    pair_count * 16 + 8,
                    ptr_add(new_entries, new_off + DICTENTRY_KEY_OFFSET),
                )
                pair_count = pair_count + 1
                store_ptr(
                    slot_pairs,
                    pair_count * 16,
                    ptr_add(old_entries, old_off + DICTENTRY_VALUE_OFFSET),
                )
                store_ptr(
                    slot_pairs,
                    pair_count * 16 + 8,
                    ptr_add(new_entries, new_off + DICTENTRY_VALUE_OFFSET),
                )
                pair_count = pair_count + 1
                new_entries_used = new_entries_used + 1
                new_size = new_size + 1
            old_index = old_index + 1

        retargeted: int = 0
        if copy_valid != 0:
            retargeted = pcc_gc_backend4_retarget_mutator_payload_locked(
                d,
                old_entries,
                old_capacity * DICTENTRY_SIZE,
                new_entries,
                new_capacity * DICTENTRY_SIZE,
                slot_pairs,
                pair_count,
            )
        if copy_valid == 0 or retargeted == 0:
            pcc_py_gc_minor_graph_unlock()
            free(slot_pairs)
            free(new_entries)
            free(new_indices)
            break
        pair_index: int = 0
        while pair_index < pair_count:
            new_slot = load_ptr(slot_pairs, pair_index * 16 + 8)
            pcc_gc_note_slot_write_barrier(
                d, new_slot, load_ptr(new_slot, 0)
            )
            pair_index = pair_index + 1
        store_ptr(d, PYDICTOBJECT_INDICES_OFFSET, new_indices)
        store_ptr(d, PYDICTOBJECT_ENTRIES_OFFSET, new_entries)
        store_i64(d, PYDICTOBJECT_CAPACITY_OFFSET, new_capacity)
        store_i64(d, PYDICTOBJECT_ITEM_COUNT_OFFSET, new_size)
        store_i64(
            d, PYDICTOBJECT_ENTRIES_USED_OFFSET, new_entries_used
        )
        if retargeted == 2:
            pcc_gc_backend4_zpage_register_owner_payload_span(
                d, new_entries, new_capacity * DICTENTRY_SIZE
            )
        pcc_py_gc_minor_graph_unlock()
        free(old_indices)
        free(old_entries)
        free(slot_pairs)
        if ptr_is_null(owner_handle) == 0:
            pcc_gc_scheduler_root_unregister_handle(owner_handle)
        return 0

    if ptr_is_null(owner_handle) == 0:
        pcc_gc_scheduler_root_unregister_handle(owner_handle)
    return -1


def _maybe_grow(d) -> int:
    capacity: int = load_i64(d, PYDICTOBJECT_CAPACITY_OFFSET)
    entries_used: int = load_i64(d, PYDICTOBJECT_ENTRIES_USED_OFFSET)
    threshold: int = (capacity * 2) // 3
    if entries_used <= threshold:
        return 0
    new_cap: int = capacity
    size: int = load_i64(d, PYDICTOBJECT_ITEM_COUNT_OFFSET)
    if size > threshold // 2:
        new_cap = capacity * 2
    return _rehash(d, new_cap)


def _dict_new_with_capacity(capacity: int):
    d = pcc_gc_alloc(PYDICTOBJECT_SIZE, PY_TYPE_DICT, 0)
    if ptr_is_null(d) != 0:
        return null()
    store_i64(d, PYDICTOBJECT_ITEM_COUNT_OFFSET, 0)  # item count
    store_i64(d, PYDICTOBJECT_CAPACITY_OFFSET, 0)  # capacity
    store_ptr(d, PYDICTOBJECT_INDICES_OFFSET, null())  # indices
    store_ptr(d, PYDICTOBJECT_ENTRIES_OFFSET, null())  # entries
    store_i64(d, PYDICTOBJECT_ENTRIES_USED_OFFSET, 0)  # entries_used
    if _alloc_tables(d, capacity) != 0:
        py_decref(d)
        return null()
    py_gc_track(d)
    pcc_gc_publish_initialized(d)
    return d


@c_abi_export("py_dict_new")
def py_dict_new():
    return _dict_new_with_capacity(8)


@c_abi_export("py_dict_new_presized")
def py_dict_new_presized(expected_items: int):
    """Allocate for a known literal length using the ordinary growth limit.

    Four slots suffice for up to two initial entries. Larger literals avoid
    repeated table growth; subsequent mutations use the same dict machinery.
    The bound keeps table byte counts within signed machine-size arithmetic.
    """
    if expected_items > 0x10000000000000:
        return null()
    capacity: int = 4
    while expected_items > (capacity * 2) // 3:
        capacity = capacity * 2
    return _dict_new_with_capacity(capacity)


@c_abi_export("py_dict_set")
def py_dict_set(d, key, value) -> None:
    if not _ptr_is_dict(d):
        return
    if ptr_is_null(key) != 0:
        return
    if pcc_gc_backend() == 0:
        kind: int = _dict_fast0_key_kind(key)
        if kind != 0:
            insert_slot = stack_alloc(8)
            hash_val: int = _dict_fast0_hash(key, kind)
            ix: int = _dict_probe_fast0(d, key, kind, hash_val, insert_slot)
            if ix >= 0:
                # ``d[k] = v`` keeps the stored key object; replace the value.
                entries = load_ptr(d, PYDICTOBJECT_ENTRIES_OFFSET)
                pcc_gc_store_ptr(
                    d,
                    ptr_add(entries, ix * DICTENTRY_SIZE + DICTENTRY_VALUE_OFFSET),
                    value,
                )
                return
            if ix == -1:
                target: int = load_i64(insert_slot, 0)
                if target >= 0 and _dict_insert_fast0(d, key, value, target, hash_val) != 0:
                    return
    _dict_rooted_op(d, key, value, 2, null())


@c_abi_export("py_dict_from_static_pairs")
def py_dict_from_static_pairs(pairs, count: int):
    """Build a dict from a static ``[k0, v0, k1, v1, ...]`` pointer array.

    A module-level constant table lowered to one ``py_dict_set`` per pair
    *plus* the rooted-temporary protocol around each one.  Measured on
    ``pcc/parse/c_parsetab.py`` (561-entry ACTION map, 16,213 pairs): 300,891
    calls in the module initialiser, of which 16,213 were the inserts and
    263,032 -- 87% -- were pin/store_root/load_ptr/frame_leave/unpin/release
    bookkeeping around temporaries.  301 KB of source became 37.5 MB of IR
    and 4.4 MB of machine code.

    Every element here is a compile-time constant: keys are pooled static
    literal objects, small-int values are tagged immediates.  The array is
    immortal data, so the loop needs no rooting at all and the pairs are
    handed straight to the insert path.
    """
    d = py_dict_new()
    if ptr_is_null(d) != 0:
        return null()
    if count <= 0:
        return d
    # No pre-sizing: _alloc_tables overwrites the indices/entries pointers
    # without freeing what py_dict_new already allocated, and it re-registers
    # the zpage owner payload span.  Growth through the normal insert path is
    # a startup-only cost; the win here is the ~18x drop in *emitted* calls,
    # not in rehashing.
    index: int = 0
    while index < count:
        key = load_ptr(pairs, index * 16)
        value = load_ptr(pairs, index * 16 + 8)
        py_dict_set(d, key, value)
        index = index + 1
    return d


def _static_agg_header_kind(word) -> int:
    """2 = dict, 4 = tuple, 6 = list, 0 = a leaf.

    Leaves are tagged ints (odd) or 8-aligned object pointers, so the even,
    non-8-aligned values 2/4/6 are unambiguous header markers.
    """
    bits: int = ptr_to_int(word)
    if bits == 2 or bits == 4 or bits == 6:
        return bits
    return 0


def _static_agg_element(desc, cursor, owned_slot):
    """Next element at *cursor*: a borrowed leaf, or a new nested aggregate.

    owned_slot receives 1 when the result is a new reference the caller must
    release after storing it, 0 for an immortal leaf.
    """
    index: int = load_i64(cursor, 0)
    word = load_ptr(desc, index * 8)
    if _static_agg_header_kind(word) != 0:
        store_i64(owned_slot, 0, 1)
        return _static_agg_at(desc, cursor)
    store_i64(cursor, 0, index + 1)
    store_i64(owned_slot, 0, 0)
    return word


def _static_agg_dict(desc, cursor, count: int):
    backend: int = pcc_gc_backend()
    dict_slot = stack_alloc(8)
    owned_slot = stack_alloc(8)
    d = py_dict_new()
    if ptr_is_null(d) != 0:
        return null()
    dict_handle = _dict_read_prepare_root(dict_slot, d, backend)
    if _dict_read_root_failed(d, backend, dict_handle) != 0:
        py_decref(d)
        return null()
    index: int = 0
    while index < count:
        key = _static_agg_element(desc, cursor, owned_slot)
        value = _static_agg_element(desc, cursor, owned_slot)
        value_owned: int = load_i64(owned_slot, 0)
        if ptr_is_null(value) != 0:
            _dict_read_finish_root(dict_handle)
            return null()
        # The nested build above may have allocated and moved the container.
        d = _dict_read_reload_root(dict_slot, dict_handle)
        py_dict_set(d, key, value)
        if value_owned != 0:
            py_decref(value)
        index = index + 1
    d = _dict_read_reload_root(dict_slot, dict_handle)
    _dict_read_finish_root(dict_handle)
    return d


def _static_agg_sequence(desc, cursor, count: int, is_tuple: int):
    backend: int = pcc_gc_backend()
    seq_slot = stack_alloc(8)
    owned_slot = stack_alloc(8)
    if is_tuple != 0:
        seq = py_tuple_new(count)
    else:
        seq = py_list_new(count)
    if ptr_is_null(seq) != 0:
        return null()
    seq_handle = _dict_read_prepare_root(seq_slot, seq, backend)
    if _dict_read_root_failed(seq, backend, seq_handle) != 0:
        py_decref(seq)
        return null()
    index: int = 0
    while index < count:
        value = _static_agg_element(desc, cursor, owned_slot)
        value_owned: int = load_i64(owned_slot, 0)
        if ptr_is_null(value) != 0:
            _dict_read_finish_root(seq_handle)
            return null()
        seq = _dict_read_reload_root(seq_slot, seq_handle)
        if is_tuple != 0:
            py_tuple_set_item(seq, index, value)
        else:
            py_list_append(seq, value)
        if value_owned != 0:
            py_decref(value)
        index = index + 1
    seq = _dict_read_reload_root(seq_slot, seq_handle)
    _dict_read_finish_root(seq_handle)
    return seq


def _static_agg_at(desc, cursor):
    index: int = load_i64(cursor, 0)
    kind: int = _static_agg_header_kind(load_ptr(desc, index * 8))
    count: int = untag_int(load_ptr(desc, (index + 1) * 8))
    store_i64(cursor, 0, index + 2)
    if kind == 2:
        return _static_agg_dict(desc, cursor, count)
    if kind == 4:
        return _static_agg_sequence(desc, cursor, count, 1)
    if kind == 6:
        return _static_agg_sequence(desc, cursor, count, 0)
    return null()


@c_abi_export("py_static_aggregate_build")
def py_static_aggregate_build(desc):
    """Build a nested constant dict/tuple/list from a static descriptor.

    Codegen emits the descriptor as one read-only global holding only tagged
    ints, pooled static str objects and header markers -- no heap pointers --
    plus this single call.  Every level is constructed here, with the
    container under construction held in the same moving-root protocol
    `_dict_rooted_op` uses, so nothing the collector cannot see ever holds a
    live object.  On c_parsetab this replaces ~18 emitted calls per outer
    pair with 16 bytes of data.
    """
    if ptr_is_null(desc) != 0:
        return null()
    cursor = stack_alloc(8)
    store_i64(cursor, 0, 0)
    return _static_agg_at(desc, cursor)


@c_abi_export("py_dict_get")
def py_dict_get(d, key):
    if not _ptr_is_dict(d):
        return null()
    if ptr_is_null(key) != 0:
        return null()
    if pcc_gc_backend() == 0:
        kind: int = _dict_fast0_key_kind(key)
        if kind != 0:
            insert_slot = stack_alloc(8)
            ix: int = _dict_probe_fast0(
                d, key, kind, _dict_fast0_hash(key, kind), insert_slot
            )
            if ix >= 0:
                entries = load_ptr(d, PYDICTOBJECT_ENTRIES_OFFSET)
                found = load_ptr(entries, ix * DICTENTRY_SIZE + DICTENTRY_VALUE_OFFSET)
                if ptr_is_null(found) == 0:
                    py_incref(found)
                    return found
            elif ix == -1:
                return null()
    return _dict_rooted_op(d, key, null(), 0, null())


@c_abi_export("py_dict_get_default")
def py_dict_get_default(d, key, default_value):
    v = py_dict_get(d, key)
    if ptr_is_null(v) == 0:
        return v
    if py_err_occurred() != 0:
        return null()
    py_incref(default_value)
    return default_value


@c_abi_export("py_dict_getitem")
def py_dict_getitem(d, key):
    # d[key] subscript: like py_dict_get but raises KeyError (carrying the key)
    # when absent, so try/except can catch it. Mirrors py_dict_getitem in
    # py_dict.c; py_dict_get stays non-raising for dict.get()/setdefault().
    v = py_dict_get(d, key)
    if ptr_is_null(v) == 0:
        return v
    if py_err_occurred() != 0:
        return null()
    exc = py_exc_new_with_value(4, key)  # PY_EXC_KEYERROR
    py_raise_owned(exc)
    return null()


@c_abi_export("py_dict_fromkeys")
def py_dict_fromkeys(iterable, value):
    # dict.fromkeys(iterable, value): new dict, each element -> value (caller
    # passes None when omitted). Iterator protocol; clears a terminal
    # StopIteration. Mirrors py_dict_fromkeys in py_dict.c. No break -> use a
    # done flag.
    d = py_dict_new()
    if ptr_is_null(d) != 0:
        return null()
    it = py_obj_iter(iterable)
    if ptr_is_null(it) != 0:
        py_runtime_error_if_unset(
            cstr("py_obj_iter"),
            cstr("dict.fromkeys could not create an iterator"),
        )
        py_decref(d)
        return null()
    done: int = 0
    while done == 0:
        k = py_obj_next(it)
        if ptr_is_null(k) != 0:
            if py_err_occurred() != 0:
                cur = py_current_exception()
                stop = py_exc_builtin_class(8)  # PY_EXC_STOPITERATION
                if py_exc_matches(cur, stop) != 0:
                    py_clear_exception()
                    done = 1
                else:
                    py_decref(it)
                    py_decref(d)
                    return null()
            else:
                py_runtime_error_if_unset(
                    cstr("py_obj_next"),
                    cstr(
                        "dict.fromkeys iterator returned NULL without an exception"
                    ),
                )
                py_decref(it)
                py_decref(d)
                return null()
        else:
            py_dict_set(d, k, value)
            if py_err_occurred() != 0:
                py_decref(k)
                py_decref(it)
                py_decref(d)
                return null()
            py_decref(k)
    py_decref(it)
    return d


@c_abi_export("py_dict_pop")
def py_dict_pop(d, key):
    v = py_dict_get(d, key)
    if ptr_is_null(v) == 0:
        py_dict_del(d, key)
        return v
    if py_err_occurred() != 0:
        return null()
    exc = py_exc_new_with_value(4, key)  # PY_EXC_KEYERROR
    py_raise_owned(exc)
    return null()


@c_abi_export("py_dict_popitem")
def py_dict_popitem(d):
    # Remove+return the LAST-inserted (key,value) as a 2-tuple (dicts are
    # insertion-ordered); KeyError if empty. py_tuple_set_item increfs, so
    # py_dict_del's decref leaves key/value owned by the tuple. No `break`
    # (pcc-Python lacks it): a `found` flag in the loop condition exits.
    entries = load_ptr(d, PYDICTOBJECT_ENTRIES_OFFSET)
    ei: int = load_i64(d, PYDICTOBJECT_ENTRIES_USED_OFFSET)  # entries_used
    i: int = ei - 1
    found: int = 0
    result = null()
    while i >= 0 and found == 0:
        entry_off: int = i * DICTENTRY_SIZE
        key = _entry_key(d, entries, entry_off)
        if ptr_is_null(key) == 0:
            val = _entry_value(d, entries, entry_off)
            tup = py_tuple_new(2)
            py_tuple_set_item(tup, 0, key)
            py_tuple_set_item(tup, 1, val)
            py_dict_del(d, key)
            result = tup
            found = 1
        i = i - 1
    if found == 0:
        exc = py_exc_new_with_value(4, null())  # bare KeyError
        py_raise_owned(exc)
        return null()
    return result


@c_abi_export("py_dict_contains")
def py_dict_contains(d, key) -> int:
    if not _ptr_is_dict(d):
        return 0
    if ptr_is_null(key) != 0:
        return 0
    value = py_dict_get(d, key)
    if ptr_is_null(value) != 0:
        return 0
    py_decref(value)
    return 1


@c_abi_export("py_dict_del")
def py_dict_del(d, key) -> int:
    if not _ptr_is_dict(d):
        return -1
    if ptr_is_null(key) != 0:
        return -1
    status = stack_alloc(8)
    store_i64(status, 0, 0)
    _dict_rooted_op(d, key, null(), 1, status)
    if load_i64(status, 0) != 0:
        return 0
    return -1


@c_abi_export("py_dict_clear")
def py_dict_clear(d) -> None:
    if not _ptr_is_dict(d):
        return
    entries = load_ptr(d, PYDICTOBJECT_ENTRIES_OFFSET)
    entries_used: int = load_i64(d, PYDICTOBJECT_ENTRIES_USED_OFFSET)
    i: int = 0
    while i < entries_used:
        off: int = i * DICTENTRY_SIZE
        k = _entry_key(d, entries, off)
        if ptr_is_null(k) == 0:
            v = _entry_value(d, entries, off)
            py_decref(k)
            py_decref(v)
            store_i64(entries, off + DICTENTRY_HASH_OFFSET, 0)
            store_ptr(entries, off + DICTENTRY_KEY_OFFSET, null())
            store_ptr(entries, off + DICTENTRY_VALUE_OFFSET, null())
        i = i + 1
    indices = load_ptr(d, PYDICTOBJECT_INDICES_OFFSET)
    capacity: int = load_i64(d, PYDICTOBJECT_CAPACITY_OFFSET)
    j: int = 0
    while j < capacity:
        store_i64(indices, j * 8, -1)
        j = j + 1
    store_i64(d, PYDICTOBJECT_ITEM_COUNT_OFFSET, 0)
    store_i64(d, PYDICTOBJECT_ENTRIES_USED_OFFSET, 0)


@c_abi_export("py_dict_len")
def py_dict_len(d) -> int:
    if not _ptr_is_dict(d):
        return 0
    return load_i64(d, PYDICTOBJECT_ITEM_COUNT_OFFSET)


@c_abi_export("py_dict_entries_used")
def py_dict_entries_used(d) -> int:
    if not _ptr_is_dict(d):
        return 0
    return load_i64(d, PYDICTOBJECT_ENTRIES_USED_OFFSET)


@c_abi_export("py_dict_entry_key_at")
def py_dict_entry_key_at(d, i: int):
    if not _ptr_is_dict(d):
        return null()
    if i < 0:
        return null()
    entries_used: int = load_i64(d, PYDICTOBJECT_ENTRIES_USED_OFFSET)
    if i >= entries_used:
        return null()
    entries = load_ptr(d, PYDICTOBJECT_ENTRIES_OFFSET)
    k = _entry_key(d, entries, i * DICTENTRY_SIZE)
    if ptr_is_null(k) == 0:
        py_incref(k)
    return k


@c_abi_export("py_dict_entry_value_at")
def py_dict_entry_value_at(d, i: int):
    if not _ptr_is_dict(d):
        return null()
    if i < 0:
        return null()
    entries_used: int = load_i64(d, PYDICTOBJECT_ENTRIES_USED_OFFSET)
    if i >= entries_used:
        return null()
    entries = load_ptr(d, PYDICTOBJECT_ENTRIES_OFFSET)
    off: int = i * DICTENTRY_SIZE
    k = _entry_key(d, entries, off)
    if ptr_is_null(k) != 0:
        return null()
    v = _entry_value(d, entries, off)
    if ptr_is_null(v) == 0:
        py_incref(v)
    return v


@c_abi_export("py_dict_keys")
def py_dict_keys(d):
    if not _ptr_is_dict(d):
        return null()
    size: int = load_i64(d, PYDICTOBJECT_ITEM_COUNT_OFFSET)
    cap_hint: int = size
    if cap_hint <= 0:
        cap_hint = 4
    out = py_list_new(cap_hint)
    if ptr_is_null(out) != 0:
        return null()
    entries = load_ptr(d, PYDICTOBJECT_ENTRIES_OFFSET)
    entries_used: int = load_i64(d, PYDICTOBJECT_ENTRIES_USED_OFFSET)
    i: int = 0
    while i < entries_used:
        off: int = i * DICTENTRY_SIZE
        k = _entry_key(d, entries, off)
        if ptr_is_null(k) == 0:
            py_list_append(out, k)
        i = i + 1
    return out


@c_abi_export("py_dict_values")
def py_dict_values(d):
    if not _ptr_is_dict(d):
        return null()
    size: int = load_i64(d, PYDICTOBJECT_ITEM_COUNT_OFFSET)
    cap_hint: int = size
    if cap_hint <= 0:
        cap_hint = 4
    out = py_list_new(cap_hint)
    if ptr_is_null(out) != 0:
        return null()
    entries = load_ptr(d, PYDICTOBJECT_ENTRIES_OFFSET)
    entries_used: int = load_i64(d, PYDICTOBJECT_ENTRIES_USED_OFFSET)
    i: int = 0
    while i < entries_used:
        off: int = i * DICTENTRY_SIZE
        k = _entry_key(d, entries, off)
        if ptr_is_null(k) == 0:
            v = _entry_value(d, entries, off)
            py_list_append(out, v)
        i = i + 1
    return out


@c_abi_export("py_dict_items")
def py_dict_items(d):
    if not _ptr_is_dict(d):
        return null()
    size: int = load_i64(d, PYDICTOBJECT_ITEM_COUNT_OFFSET)
    cap_hint: int = size
    if cap_hint <= 0:
        cap_hint = 4
    out = py_list_new(cap_hint)
    if ptr_is_null(out) != 0:
        return null()
    entries = load_ptr(d, PYDICTOBJECT_ENTRIES_OFFSET)
    entries_used: int = load_i64(d, PYDICTOBJECT_ENTRIES_USED_OFFSET)
    i: int = 0
    while i < entries_used:
        off: int = i * DICTENTRY_SIZE
        k = _entry_key(d, entries, off)
        if ptr_is_null(k) == 0:
            v = _entry_value(d, entries, off)
            pair = py_tuple_new(2)
            if ptr_is_null(pair) != 0:
                py_decref(out)
                return null()
            py_tuple_set_item(pair, 0, k)
            py_tuple_set_item(pair, 1, v)
            py_list_append(out, pair)
            py_decref(pair)
        i = i + 1
    return out


def _dict_update_hold(slots, handles, index: int, value, backend: int) -> int:
    slot = ptr_add(slots, index * 8)
    handle = _dict_read_prepare_root(slot, value, backend)
    store_ptr(handles, index * 8, handle)
    if _dict_read_root_failed(value, backend, handle) != 0:
        py_raise_owned(py_exc_new(19, cstr("dict.update: cannot root temporary")))
        return 0
    return 1


def _dict_update_load(slots, handles, index: int):
    return _dict_read_reload_root(
        ptr_add(slots, index * 8), load_ptr(handles, index * 8)
    )


def _dict_update_drop(slots, handles, index: int) -> None:
    value = _dict_update_load(slots, handles, index)
    _dict_read_finish_root(load_ptr(handles, index * 8))
    store_ptr(handles, index * 8, null())
    store_ptr(slots, index * 8, null())
    # Destination and source are borrowed. Every later slot owns its result.
    if index >= 2:
        py_decref(value)


def _dict_update_protocol(dst, src) -> None:
    # Slots: destination, source, iterator, item/method, pair/arguments,
    # key/keys-result, value. Keep every live owner across user callbacks.
    slots = stack_alloc(56)
    handles = stack_alloc(56)
    memset(slots, 0, 56)
    memset(handles, 0, 56)
    backend: int = pcc_gc_backend()
    ok: int = _dict_update_hold(slots, handles, 0, dst, backend)
    if ok != 0:
        ok = _dict_update_hold(slots, handles, 1, src, backend)
    mapping: int = 0
    if ok != 0:
        method = py_obj_getattr(_dict_update_load(slots, handles, 1), cstr("keys"))
        if ptr_is_null(method) != 0:
            if py_err_occurred() != 0:
                if py_exc_matches(py_current_exception(), py_exc_builtin_class(6)) != 0:
                    py_clear_exception()
                else:
                    ok = 0
        else:
            mapping = 1
            ok = _dict_update_hold(slots, handles, 3, method, backend)
    if ok != 0 and mapping != 0:
        args = py_tuple_new(0)
        ok = _dict_update_hold(slots, handles, 4, args, backend)
        if ptr_is_null(args) != 0:
            ok = 0
        if ok != 0:
            keys = py_obj_call(_dict_update_load(slots, handles, 3),
                               _dict_update_load(slots, handles, 4), null())
            ok = _dict_update_hold(slots, handles, 5, keys, backend)
            if ptr_is_null(keys) != 0:
                ok = 0
    if ok != 0:
        source = _dict_update_load(slots, handles, 1)
        if mapping != 0:
            source = _dict_update_load(slots, handles, 5)
        iterator = py_obj_iter(source)
        ok = _dict_update_hold(slots, handles, 2, iterator, backend)
        if ptr_is_null(iterator) != 0:
            ok = 0
    _dict_update_drop(slots, handles, 5)
    _dict_update_drop(slots, handles, 4)
    _dict_update_drop(slots, handles, 3)
    while ok != 0:
        item = py_obj_next(_dict_update_load(slots, handles, 2))
        if ptr_is_null(item) != 0:
            if py_err_occurred() != 0:
                if py_exc_matches(py_current_exception(), py_exc_builtin_class(8)) != 0:
                    py_clear_exception()
            else:
                py_runtime_error_if_unset(cstr("py_obj_next"),
                                          cstr("dict.update iterator returned NULL"))
            ok = 0
        else:
            ok = _dict_update_hold(slots, handles, 3, item, backend)
            if ok != 0 and mapping == 0:
                pair = py_list_new(2)
                ok = _dict_update_hold(slots, handles, 4, pair, backend)
                if ptr_is_null(pair) != 0:
                    ok = 0
                if ok != 0:
                    py_list_extend(_dict_update_load(slots, handles, 4),
                                   _dict_update_load(slots, handles, 3))
                    if py_err_occurred() != 0:
                        ok = 0
                    elif py_list_len(_dict_update_load(slots, handles, 4)) != 2:
                        py_raise_owned(py_exc_new(2, cstr(
                            "dictionary update sequence element must have length 2")))
                        ok = 0
            if ok != 0:
                key = _dict_update_load(slots, handles, 3)
                if mapping != 0:
                    py_incref(key)
                else:
                    key = py_list_get(_dict_update_load(slots, handles, 4), 0)
                ok = _dict_update_hold(slots, handles, 5, key, backend)
            if ok != 0:
                value = null()
                if mapping != 0:
                    value = py_obj_getitem(_dict_update_load(slots, handles, 1),
                                           _dict_update_load(slots, handles, 5))
                else:
                    value = py_list_get(_dict_update_load(slots, handles, 4), 1)
                ok = _dict_update_hold(slots, handles, 6, value, backend)
                if ptr_is_null(value) != 0:
                    ok = 0
            if ok != 0:
                py_dict_set(_dict_update_load(slots, handles, 0),
                            _dict_update_load(slots, handles, 5),
                            _dict_update_load(slots, handles, 6))
                if py_err_occurred() != 0:
                    ok = 0
            index: int = 6
            while index >= 3:
                _dict_update_drop(slots, handles, index)
                index = index - 1
    index: int = 6
    while index >= 0:
        _dict_update_drop(slots, handles, index)
        index = index - 1


@c_abi_export("py_dict_update")
def py_dict_update(dst, src) -> None:
    # Snapshot the source before invoking destination hash/equality callbacks.
    # py_dict_set runs user code, which may relocate either dict or mutate the
    # source, so caching the source table across those calls would leave later
    # iterations reading a stale owner/table.  Mirrors py_set_update.  The
    # snapshot holds key and value alternately.
    if not _ptr_is_dict(dst):
        return
    if not _ptr_is_dict(src):
        _dict_update_protocol(dst, src)
        return
    backend: int = pcc_gc_backend()
    dst_slot = stack_alloc(8)
    src_slot = stack_alloc(8)
    snap_slot = stack_alloc(8)
    key_slot = stack_alloc(8)
    value_slot = stack_alloc(8)
    dst_handle = _dict_read_prepare_root(dst_slot, dst, backend)
    if _dict_read_root_failed(dst, backend, dst_handle) != 0:
        return
    src_handle = _dict_read_prepare_root(src_slot, src, backend)
    if _dict_read_root_failed(src, backend, src_handle) != 0:
        _dict_read_finish_root(dst_handle)
        return

    src = _dict_read_reload_root(src_slot, src_handle)
    size_hint: int = load_i64(src, PYDICTOBJECT_ITEM_COUNT_OFFSET) * 2
    if size_hint <= 0:
        size_hint = 4
    snapshot = py_list_new(size_hint)
    if ptr_is_null(snapshot) != 0:
        _dict_read_finish_root(src_handle)
        _dict_read_finish_root(dst_handle)
        return
    snap_handle = _dict_read_prepare_root(snap_slot, snapshot, backend)
    if _dict_read_root_failed(snapshot, backend, snap_handle) != 0:
        py_decref(snapshot)
        _dict_read_finish_root(src_handle)
        _dict_read_finish_root(dst_handle)
        return

    src = _dict_read_reload_root(src_slot, src_handle)
    source_used: int = load_i64(src, PYDICTOBJECT_ENTRIES_USED_OFFSET)
    i: int = 0
    stop: int = 0
    while i < source_used and stop == 0:
        src = _dict_read_reload_root(src_slot, src_handle)
        if not _ptr_is_dict(src):
            stop = 1
        elif i >= load_i64(src, PYDICTOBJECT_ENTRIES_USED_OFFSET):
            stop = 1
        else:
            entries = load_ptr(src, PYDICTOBJECT_ENTRIES_OFFSET)
            off: int = i * DICTENTRY_SIZE
            k = _entry_key(src, entries, off)
            if ptr_is_null(k) == 0:
                v = _entry_value(src, entries, off)
                snapshot = _dict_read_reload_root(snap_slot, snap_handle)
                py_list_append(snapshot, k)
                if py_err_occurred() != 0:
                    stop = 1
                else:
                    snapshot = _dict_read_reload_root(snap_slot, snap_handle)
                    py_list_append(snapshot, v)
                    if py_err_occurred() != 0:
                        stop = 1
        i = i + 1

    snapshot = _dict_read_reload_root(snap_slot, snap_handle)
    snap_len: int = py_list_len(snapshot)
    j: int = 0
    done: int = 0
    while j + 1 < snap_len and done == 0:
        if py_err_occurred() != 0:
            done = 1
        else:
            snapshot = _dict_read_reload_root(snap_slot, snap_handle)
            key = py_list_get(snapshot, j)
            snapshot = _dict_read_reload_root(snap_slot, snap_handle)
            value = py_list_get(snapshot, j + 1)
            if ptr_is_null(key) != 0 or ptr_is_null(value) != 0:
                done = 1
            else:
                key_handle = _dict_read_prepare_root(key_slot, key, backend)
                if _dict_read_root_failed(key, backend, key_handle) != 0:
                    # Mirrors the C path: an unregistered key must not cross a
                    # user hash/equality callback, and the pre-move pointer
                    # must not be decref'd afterwards.
                    py_decref(key)
                    py_decref(value)
                    done = 1
                value_handle = _dict_read_prepare_root(
                    value_slot, value, backend
                )
                if done == 0 and _dict_read_root_failed(
                    value, backend, value_handle
                ) != 0:
                    _dict_read_finish_root(key_handle)
                    py_decref(key)
                    py_decref(value)
                    done = 1
                dst = _dict_read_reload_root(dst_slot, dst_handle)
                key = _dict_read_reload_root(key_slot, key_handle)
                value = _dict_read_reload_root(value_slot, value_handle)
                if done == 0:
                    py_dict_set(dst, key, value)
                    key = _dict_read_reload_root(key_slot, key_handle)
                    value = _dict_read_reload_root(value_slot, value_handle)
                    _dict_read_finish_root(value_handle)
                    _dict_read_finish_root(key_handle)
                    py_decref(key)
                    py_decref(value)
        j = j + 2

    snapshot = _dict_read_reload_root(snap_slot, snap_handle)
    _dict_read_finish_root(snap_handle)
    py_decref(snapshot)
    _dict_read_finish_root(src_handle)
    _dict_read_finish_root(dst_handle)
