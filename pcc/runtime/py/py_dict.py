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

from pcc.extern import (
    extern,
    c_abi_export,
    c_ptr,
    c_int32,
    c_int64,
    c_void,
)
from pcc.runtime.py.py_abi_constants import (
    C_POINTER_SIZE,
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
    PYOBJECTHEADER_FLAGS_OFFSET,
    PY_FLAG_GC_PINNED,
    PYLISTOBJECT_ITEMS_OFFSET,
    PYLISTOBJECT_LENGTH_OFFSET,
    PYTUPLEOBJECT_ITEMS_OFFSET,
    PYTUPLEOBJECT_LEN_OFFSET,
    PY_TYPE_DICT,
    PY_TYPE_STR,
    PY_TYPE_TUPLE,
    PY_TYPE_LIST,
)
from pcc.unsafe import (
    cstr,
    define_global_i32,
    free,
    global_addr,
    global_load_ptr,
    int_to_ptr,
    is_tagged_int,
    load_i8,
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
    store_i8,
    store_i32,
    store_i64,
    store_ptr,
    unsigned_div_i64,
    untag_int,
)

# Fixed internal C ABI notification. Its implementation only updates native
# class metadata and the cache epoch while the dictionary commit lock is held.
py_class_namespace_validate_locked = extern(
    "py_class_namespace_validate_locked", (c_ptr, c_ptr), c_int64,
)
py_class_namespace_commit_locked = extern(
    "py_class_namespace_commit_locked", (c_ptr, c_ptr), c_void,
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
    namespace_commit_context: int = 0,
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
    namespace_valid: int = 1
    if namespace_commit_context != 0:
        namespace_valid = py_class_namespace_validate_locked(int_to_ptr(namespace_commit_context), d)
    if namespace_valid == 0:
        committed = -2
    elif _ptr_is_dict(d):
        if (
            ptr_eq(load_ptr(d, PYDICTOBJECT_INDICES_OFFSET), indices) != 0
            and ptr_eq(load_ptr(d, PYDICTOBJECT_ENTRIES_OFFSET), entries) != 0
            and load_i64(d, PYDICTOBJECT_CAPACITY_OFFSET) == capacity
            and load_i64(d, PYDICTOBJECT_ENTRIES_USED_OFFSET) == entries_used
            and slot >= 0
            and slot < capacity
            and entries_used >= 0
            and entries_used < capacity
            and (load_i64(indices, slot * 8) == -1 or load_i64(indices, slot * 8) == -2)
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
                if namespace_commit_context != 0:
                    py_class_namespace_commit_locked(int_to_ptr(namespace_commit_context), d)
            else:
                # The entry was never indexed, so it stays unreachable; plan
                # finish still balances any partial store.
                store_i64(entries, entry_off + DICTENTRY_HASH_OFFSET, 0)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_store_ptr_plan_finish(key_plan)
    pcc_gc_store_ptr_plan_finish(value_plan)
    if committed > 0:
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
    namespace_commit_context: int = 0,
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
    namespace_valid: int = 1
    if namespace_commit_context != 0:
        namespace_valid = py_class_namespace_validate_locked(int_to_ptr(namespace_commit_context), d)
    if namespace_valid == 0:
        committed = -2
    elif _ptr_is_dict(d):
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
    if committed > 0:
        if namespace_commit_context != 0:
            py_class_namespace_commit_locked(int_to_ptr(namespace_commit_context), d)
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
    namespace_commit_context: int = 0,
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
    namespace_valid: int = 1
    if namespace_commit_context != 0:
        namespace_valid = py_class_namespace_validate_locked(int_to_ptr(namespace_commit_context), d)
    if namespace_valid == 0:
        committed = -2
    elif _ptr_is_dict(d):
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
                if namespace_commit_context != 0:
                    py_class_namespace_commit_locked(int_to_ptr(namespace_commit_context), d)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_store_ptr_plan_finish(key_plan)
    pcc_gc_store_ptr_plan_finish(value_plan)
    return committed


def _dict_rooted_op(d, key, value, mode: int, status_slot, namespace_commit_context: int = 0):
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
                                    namespace_commit_context,
                                )
                                if removed < 0:
                                    if ptr_is_null(status_slot) == 0:
                                        store_i64(status_slot, 0, -2)
                                    mutated = 1
                                    done = 1
                                elif removed == 0:
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
                                    namespace_commit_context,
                                )
                                if replaced < 0:
                                    if ptr_is_null(status_slot) == 0:
                                        store_i64(status_slot, 0, -2)
                                    mutated = 1
                                    done = 1
                                elif replaced == 0:
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
                    namespace_commit_context,
                )
                if inserted < 0:
                    if ptr_is_null(status_slot) == 0:
                        store_i64(status_slot, 0, -2)
                    done = 1
                elif inserted == 0:
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
            key = pcc_gc_resolve_root_slot_unlocked(ptr_add(old_entries, old_off + DICTENTRY_KEY_OFFSET), 0)
            if ptr_is_null(key) == 0:
                hash_value: int = load_i64(
                    old_entries, old_off + DICTENTRY_HASH_OFFSET
                )
                value = pcc_gc_resolve_root_slot_unlocked(ptr_add(old_entries, old_off + DICTENTRY_VALUE_OFFSET), 0)
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
    ``pcc/frontends/c/parse/c_parsetab.py`` (561-entry ACTION map, 16,213 pairs): 300,891
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

    Leaves are tagged ints (odd), 8-aligned object pointers, or the singleton
    markers 10/12/14 (None/False/True), so the even, non-8-aligned values
    2/4/6 are unambiguous header markers.
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
    bits: int = ptr_to_int(word)
    if bits == 10:
        return global_load_ptr("py_None")
    if bits == 12:
        return global_load_ptr("py_False")
    if bits == 14:
        return global_load_ptr("py_True")
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
    return _dict_materialize_raw(d, _DICT_MATERIALIZE_KEYS)


@c_abi_export("py_dict_values")
def py_dict_values(d):
    return _dict_materialize_raw(d, _DICT_MATERIALIZE_VALUES)


@c_abi_export("py_dict_items")
def py_dict_items(d):
    return _dict_materialize_raw(d, _DICT_MATERIALIZE_ITEMS)


@c_abi_export("py_dict_update")
def py_dict_update(dst, src) -> None:
    # Legacy raw entry, retaining its existing borrowed-root admission. Slot
    # callers never use this adapter; raw callers must keep their operands
    # stable through admission. Both routes share the update implementation.
    backend: int = pcc_gc_backend()
    dst_slot = stack_alloc(C_POINTER_SIZE)
    src_slot = stack_alloc(C_POINTER_SIZE)
    dst_handle = _dict_read_prepare_root(dst_slot, dst, backend)
    if _dict_read_root_failed(dst, backend, dst_handle) != 0:
        return
    src_handle = _dict_read_prepare_root(src_slot, src, backend)
    if _dict_read_root_failed(src, backend, src_handle) != 0:
        _dict_read_finish_root(dst_handle)
        return
    _dict_slot_update_bound(dst_slot, src_slot, 1)
    _dict_read_finish_root(src_handle)
    _dict_read_finish_root(dst_handle)


# Authoritative slot entries. Raw APIs below remain compatibility boundaries;
# callers which already own roots must use these entries instead of publishing
# another raw copy in a callee. Scratch owners are registered while empty.
pcc_gc_root_copy_borrowed_lease = extern("pcc_gc_root_copy_borrowed_lease", (c_ptr, c_ptr), c_int64)
pcc_gc_root_move = extern("pcc_gc_root_move", (c_ptr, c_ptr), c_int64)
pcc_gc_root_copy_lease = extern("pcc_gc_root_copy_lease", (c_ptr, c_ptr), c_int64)
pcc_gc_root_copy_lease_prepare_locked = extern("pcc_gc_root_copy_lease_prepare_locked", (c_ptr, c_ptr, c_int64, c_ptr), c_int64)
pcc_gc_root_copy_lease_finish = extern("pcc_gc_root_copy_lease_finish", (c_ptr,), c_void)
pcc_gc_foreign_lease_acquire = extern("pcc_gc_foreign_lease_acquire", (c_ptr,), c_int64)
pcc_gc_foreign_lease_release = extern("pcc_gc_foreign_lease_release", (c_ptr, c_int64), c_int64)
pcc_gc_resolve_root_slot_unlocked = extern("pcc_gc_resolve_root_slot_unlocked", (c_ptr, c_int64), c_ptr)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
py_tls_exc_swap_slot = extern("py_tls_exc_swap_slot", (c_ptr,), c_void)
py_obj_call_slots = extern("py_obj_call_slots", (c_ptr, c_ptr, c_ptr, c_ptr), c_int64)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)


def _dict_slot_open(slots, tokens, handles, width: int) -> int:
    # A frame registers only the slots its operation names: slot 0 holds the
    # caller's suspended exception, the last slot the operation's own, and
    # every registration costs a locked register/unregister pair.
    memset(slots, 0, width * C_POINTER_SIZE)
    memset(tokens, 0, width * C_POINTER_SIZE)
    memset(handles, 0, width * C_POINTER_SIZE)
    count: int = 0
    while count < width:
        handle = pcc_gc_scheduler_root_register_handle(ptr_add(slots, count * C_POINTER_SIZE))
        if ptr_is_null(handle) != 0:
            return count
        store_ptr(handles, count * C_POINTER_SIZE, handle)
        count = count + 1
    return count


def _dict_slot_error(message) -> int:
    py_runtime_error_if_unset(cstr("dictionary slot operation"), message)
    return -1


def _dict_slot_copy(slots, tokens, index: int, source) -> int:
    if ptr_is_null(source) != 0:
        return _dict_slot_error(cstr("dictionary operation requires an authoritative source slot"))
    token: int = pcc_gc_root_copy_lease(ptr_add(slots, index * C_POINTER_SIZE), source)
    if token < 0:
        return _dict_slot_error(cstr("dictionary source root transfer failed"))
    store_i64(tokens, index * C_POINTER_SIZE, token)
    return 0


def _dict_slot_adopt(slots, tokens, index: int) -> int:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    if ptr_is_null(load_ptr(slot, 0)) != 0:
        return _dict_slot_error(cstr("dictionary callback returned NULL without an exception"))
    token: int = pcc_gc_foreign_lease_acquire(slot)
    if token < 0:
        return _dict_slot_error(cstr("dictionary result lease failed"))
    store_i64(tokens, index * C_POINTER_SIZE, token)
    pcc_py_gc_minor_graph_lock()
    pcc_gc_note_slot_write_barrier(null(), slot, load_ptr(slot, 0))
    pcc_py_gc_minor_graph_unlock()
    if py_err_occurred() != 0:
        return -1
    return 0


def _dict_slot_drop(slots, tokens, index: int) -> None:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    if pcc_gc_foreign_lease_release(slot, load_i64(tokens, index * C_POINTER_SIZE)) != 0:
        pcc_platform_abort()
        return
    store_i64(tokens, index * C_POINTER_SIZE, 0)
    pcc_gc_store_root(slot, null())


def _dict_slot_close(slots, tokens, handles, count: int, suspended: int, width: int) -> None:
    last: int = width - 1
    if suspended != 0:
        py_tls_exc_swap_slot(ptr_add(slots, last * C_POINTER_SIZE))
    index: int = last - 1
    while index > 0:
        _dict_slot_drop(slots, tokens, index)
        index = index - 1
    if suspended != 0:
        py_clear_exception()
        if ptr_is_null(load_ptr(slots, last * C_POINTER_SIZE)) == 0:
            pcc_gc_store_root(slots, null())
            py_clear_exception()
            py_tls_exc_swap_slot(ptr_add(slots, last * C_POINTER_SIZE))
        else:
            py_tls_exc_swap_slot(slots)
    index = 0
    while index < count:
        pcc_gc_scheduler_root_unregister_handle(load_ptr(handles, index * C_POINTER_SIZE))
        index = index + 1


# Read-only lookup shares the checked probe and copied-result owner.
_DICT_SLOT_GET_ONLY = 2
# Get/set frame: 0 caller's exception, 1 dict, 2 key, 3 value, 4 probed
# candidate key, 5 result, 6 the operation's exception.
_DICT_SLOT_SET_COUNT = 7


def _dict_slot_set_core(slots, tokens, known_hash: int, hash_value: int, keep_existing: int, namespace_commit_context: int = 0) -> int:
    # 1 destination, 2 key, 3 value, 4 collision candidate. All four own
    # independently counted address leases whenever nonempty.
    if not _ptr_is_dict(load_ptr(slots, C_POINTER_SIZE)):
        py_raise_owned(py_exc_new(3, cstr("dictionary destination must be a dict")))
        return -1
    if ptr_is_null(load_ptr(slots, 2 * C_POINTER_SIZE)) != 0:
        return _dict_slot_error(cstr("dictionary key is NULL"))
    if known_hash == 0:
        hash_value = py_obj_hash(load_ptr(slots, 2 * C_POINTER_SIZE))
    if py_err_occurred() != 0:
        return -1
    plan = stack_alloc(256)
    write_plan = stack_alloc(256)
    while True:
        pcc_py_gc_minor_graph_lock()
        owner = load_ptr(slots, C_POINTER_SIZE)
        capacity: int = load_i64(owner, PYDICTOBJECT_CAPACITY_OFFSET)
        entries = load_ptr(owner, PYDICTOBJECT_ENTRIES_OFFSET)
        indices = load_ptr(owner, PYDICTOBJECT_INDICES_OFFSET)
        used: int = load_i64(owner, PYDICTOBJECT_ENTRIES_USED_OFFSET)
        pcc_py_gc_minor_graph_unlock()
        if capacity <= 0:
            return _dict_slot_error(cstr("dictionary has no hash table"))
        mask: int = capacity - 1
        perturb: int = hash_value
        bucket: int = hash_value & mask
        tombstone: int = -1
        restart: int = 0
        probes: int = 0
        while probes < capacity + 16 and restart == 0:
            prepared: int = 0
            token: int = 0
            entry: int = -3
            pcc_py_gc_minor_graph_lock()
            owner = load_ptr(slots, C_POINTER_SIZE)
            if (ptr_eq(load_ptr(owner, PYDICTOBJECT_ENTRIES_OFFSET), entries) == 0
                or ptr_eq(load_ptr(owner, PYDICTOBJECT_INDICES_OFFSET), indices) == 0
                or load_i64(owner, PYDICTOBJECT_CAPACITY_OFFSET) != capacity
                or load_i64(owner, PYDICTOBJECT_ENTRIES_USED_OFFSET) != used):
                restart = 1
            else:
                entry = load_i64(indices, bucket * 8)
                if entry >= 0 and entry < used:
                    offset: int = entry * DICTENTRY_SIZE
                    if load_i64(entries, offset + DICTENTRY_HASH_OFFSET) == hash_value:
                        token = pcc_gc_root_copy_lease_prepare_locked(
                            ptr_add(slots, 4 * C_POINTER_SIZE),
                            ptr_add(entries, offset + DICTENTRY_KEY_OFFSET), 0, plan)
                        prepared = 1
                        if token >= 0:
                            store_i64(tokens, 4 * C_POINTER_SIZE, token)
            pcc_py_gc_minor_graph_unlock()
            if prepared != 0:
                pcc_gc_root_copy_lease_finish(plan)
                if token < 0:
                    return _dict_slot_error(cstr("dictionary candidate root transfer failed"))
            if restart != 0:
                break
            if entry == -1:
                if keep_existing == _DICT_SLOT_GET_ONLY:
                    return _dict_slot_copy(slots, tokens, 5, ptr_add(slots, 3 * C_POINTER_SIZE))
                if used >= capacity:
                    if _maybe_grow(load_ptr(slots, C_POINTER_SIZE)) != 0:
                        py_raise_owned(py_exc_new(19, cstr("dictionary entry storage allocation failed")))
                        return -1
                    restart = 1
                    break
                target: int = bucket if tombstone < 0 else tombstone
                inserted: int = _dict_insert_rooted_slot(
                    ptr_add(slots, C_POINTER_SIZE), null(),
                    ptr_add(slots, 2 * C_POINTER_SIZE), null(),
                    ptr_add(slots, 3 * C_POINTER_SIZE), null(),
                    indices, entries, capacity, used, target, hash_value, namespace_commit_context)
                if inserted < 0:
                    return -2
                if inserted > 0:
                    if py_err_occurred() != 0:
                        return -1
                    if keep_existing != 0:
                        return _dict_slot_copy(slots, tokens, 5, ptr_add(slots, 3 * C_POINTER_SIZE))
                    return 0
                restart = 1
            elif entry == -2:
                if tombstone < 0:
                    tombstone = bucket
            elif prepared != 0:
                equal: int = py_obj_eq(load_ptr(slots, 4 * C_POINTER_SIZE), load_ptr(slots, 2 * C_POINTER_SIZE))
                if py_err_occurred() != 0:
                    return -1
                pcc_gc_store_ptr_plan_init(write_plan, load_ptr(slots, C_POINTER_SIZE), pcc_gc_backend())
                copied: int = 0
                committed: int = 0
                pcc_py_gc_minor_graph_lock()
                owner = load_ptr(slots, C_POINTER_SIZE)
                namespace_valid: int = 1
                if namespace_commit_context != 0:
                    namespace_valid = py_class_namespace_validate_locked(int_to_ptr(namespace_commit_context), owner)
                if namespace_valid == 0:
                    committed = -2
                elif (ptr_eq(load_ptr(owner, PYDICTOBJECT_ENTRIES_OFFSET), entries) == 0
                    or ptr_eq(load_ptr(owner, PYDICTOBJECT_INDICES_OFFSET), indices) == 0
                    or load_i64(owner, PYDICTOBJECT_CAPACITY_OFFSET) != capacity
                    or load_i64(indices, bucket * 8) != entry):
                    restart = 1
                elif ptr_eq(pcc_gc_resolve_root_slot_unlocked(
                    ptr_add(entries, entry * DICTENTRY_SIZE + DICTENTRY_KEY_OFFSET), 0),
                    load_ptr(slots, 4 * C_POINTER_SIZE)) == 0:
                    restart = 1
                elif equal != 0:
                    if keep_existing != 0:
                        result_token: int = pcc_gc_root_copy_lease_prepare_locked(
                            ptr_add(slots, 5 * C_POINTER_SIZE),
                            ptr_add(entries, entry * DICTENTRY_SIZE + DICTENTRY_VALUE_OFFSET), 0, write_plan)
                        copied = 1
                        if result_token >= 0:
                            store_i64(tokens, 5 * C_POINTER_SIZE, result_token)
                            committed = 1
                    else:
                        committed = pcc_gc_store_ptr_plan_commit_locked(write_plan, owner,
                            ptr_add(entries, entry * DICTENTRY_SIZE + DICTENTRY_VALUE_OFFSET),
                            load_ptr(slots, 3 * C_POINTER_SIZE))
                    if committed == 0:
                        restart = 1
                if committed > 0 and keep_existing == 0 and namespace_commit_context != 0:
                    py_class_namespace_commit_locked(int_to_ptr(namespace_commit_context), owner)
                pcc_py_gc_minor_graph_unlock()
                if copied != 0:
                    pcc_gc_root_copy_lease_finish(write_plan)
                else:
                    pcc_gc_store_ptr_plan_finish(write_plan)
                _dict_slot_drop(slots, tokens, 4)
                if committed < 0:
                    return -2
                if copied != 0 and committed == 0:
                    return _dict_slot_error(cstr("dictionary existing value transfer failed"))
                if committed != 0:
                    return -1 if py_err_occurred() != 0 else 0
            perturb = _perturb_shift5(perturb)
            bucket = (bucket * 5 + perturb + 1) & mask
            probes = probes + 1
        if restart == 0:
            if keep_existing == _DICT_SLOT_GET_ONLY:
                return _dict_slot_copy(slots, tokens, 5, ptr_add(slots, 3 * C_POINTER_SIZE))
            if used >= capacity:
                if _maybe_grow(load_ptr(slots, C_POINTER_SIZE)) != 0:
                    py_raise_owned(py_exc_new(19, cstr("dictionary entry storage allocation failed")))
                    return -1
            else:
                return _dict_slot_error(cstr("dictionary probe found no insertion slot"))
    return -1


def _dict_slot_set_bound(dict_slot, key_slot, value_slot, known_hash: int, hash_value: int, keep_existing: int, result_slot, namespace_commit_context: int = 0) -> int:
    slots = stack_alloc(_DICT_SLOT_SET_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_DICT_SLOT_SET_COUNT * C_POINTER_SIZE)
    handles = stack_alloc(_DICT_SLOT_SET_COUNT * C_POINTER_SIZE)
    count: int = _dict_slot_open(slots, tokens, handles, _DICT_SLOT_SET_COUNT)
    suspended: int = 0
    status: int = -1
    if count == _DICT_SLOT_SET_COUNT:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _dict_slot_copy(slots, tokens, 1, dict_slot)
        if status == 0:
            status = _dict_slot_copy(slots, tokens, 2, key_slot)
        if status == 0:
            status = _dict_slot_copy(slots, tokens, 3, value_slot)
        if status == 0:
            status = _dict_slot_set_core(slots, tokens, known_hash, hash_value, keep_existing, namespace_commit_context)
        if status == 0 and keep_existing != 0:
            status = pcc_gc_root_move(result_slot, ptr_add(slots, 5 * C_POINTER_SIZE))
            if status == 0:
                token: int = load_i64(tokens, 5 * C_POINTER_SIZE)
                store_i64(tokens, 5 * C_POINTER_SIZE, 0)
                if pcc_gc_foreign_lease_release(result_slot, token) != 0:
                    pcc_platform_abort()
                    status = -1
    if status != 0 and status != -2:
        _dict_slot_error(cstr("dictionary set failed without an exception"))
    _dict_slot_close(slots, tokens, handles, count, suspended, _DICT_SLOT_SET_COUNT)
    return status


@c_abi_export("py_dict_get_default_slots")
def py_dict_get_default_slots(dict_slot, key_slot, default_slot, result_slot) -> int:
    """Copy the found value/default into the caller's empty registered owner.

    Get-only mode never inserts, replaces, grows or notifies namespace writers.
    The legacy set/setdefault namespace context and -2 retry paths are unchanged.
    """
    # py_incref's gate (one thread on backend 0, no probe, no log): for a
    # str or tagged-int key, py_dict_get's GC0 probe runs no Python code and
    # nothing moves, so the scratch root frame, the leases and the exception
    # swap of the transaction below protect nothing. Publish the found value
    # or the default into the empty result owner directly; a probe that needs
    # __eq__ (or a dict that is not exact) still takes the transaction.
    if (
        load_i32(global_addr("pcc_gc_refcount_fast"), 0) != 0
        and load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) == 0
        and ptr_is_null(dict_slot) == 0 and ptr_is_null(key_slot) == 0
        and ptr_is_null(default_slot) == 0 and ptr_is_null(result_slot) == 0
        and ptr_is_null(load_ptr(result_slot, 0)) != 0
    ):
        fast_dict = load_ptr(dict_slot, 0)
        fast_key = load_ptr(key_slot, 0)
        if (
            ptr_is_null(fast_key) == 0
            and ptr_is_null(fast_dict) == 0
            and is_tagged_int(fast_dict) == 0
            and load_i32(fast_dict, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_DICT
        ):
            kind: int = _dict_fast0_key_kind(fast_key)
            if kind != 0:
                insert_slot = stack_alloc(8)
                ix: int = _dict_probe_fast0(
                    fast_dict, fast_key, kind, _dict_fast0_hash(fast_key, kind), insert_slot
                )
                fast_value = null()
                decided: int = 0
                if ix >= 0:
                    entries = load_ptr(fast_dict, PYDICTOBJECT_ENTRIES_OFFSET)
                    fast_value = load_ptr(entries, ix * DICTENTRY_SIZE + DICTENTRY_VALUE_OFFSET)
                    if ptr_is_null(fast_value) == 0:
                        decided = 1
                elif ix == -1:
                    fast_value = load_ptr(default_slot, 0)
                    decided = 1
                if decided != 0:
                    py_incref(fast_value)
                    store_ptr(result_slot, 0, fast_value)
                    return 0
    return _dict_slot_set_bound(
        dict_slot, key_slot, default_slot, 0, 0, _DICT_SLOT_GET_ONLY, result_slot,
    )


@c_abi_export("py_dict_set_slots")
def py_dict_set_slots(dict_slot, key_slot, value_slot) -> int:
    return _dict_slot_set_bound(dict_slot, key_slot, value_slot, 0, 0, 0, null())


@c_abi_export("py_dict_namespace_set_slots")
def py_dict_namespace_set_slots(dict_slot, key_slot, value_slot, commit_context) -> int:
    """Internal class writer: sources own leases; context is raw stack data.

    The fixed notification runs only after a successful insertion/replacement,
    inside that commit's graph transaction and before displaced-owner disposal.
    """
    return _dict_slot_set_bound(
        dict_slot, key_slot, value_slot, 0, 0, 0, null(), ptr_to_int(commit_context),
    )


@c_abi_export("py_dict_namespace_del_slots")
def py_dict_namespace_del_slots(dict_slot, key_slot, commit_context) -> int:
    # Caller holds counted leases for these actual owning input slots. There
    # is no NEW result in delete mode to cross the legacy probe's cleanup.
    status = stack_alloc(C_POINTER_SIZE)
    store_i64(status, 0, 0)
    _dict_rooted_op(
        load_ptr(dict_slot, 0), load_ptr(key_slot, 0), null(), 1, status,
        ptr_to_int(commit_context),
    )
    if py_err_occurred() != 0:
        return -1
    if load_i64(status, 0) == -2:
        return -2
    if load_i64(status, 0) == 0:
        return 1
    return 0


def _dict_slot_next(slots, tokens, iterator_index: int, output_index: int) -> int:
    # 1 item, 0 exhausted, -1 failed. NULL without StopIteration is failure.
    store_ptr(slots, output_index * C_POINTER_SIZE,
              py_obj_next(load_ptr(slots, iterator_index * C_POINTER_SIZE)))
    if ptr_is_null(load_ptr(slots, output_index * C_POINTER_SIZE)) == 0:
        return 1 if _dict_slot_adopt(slots, tokens, output_index) == 0 else -1
    if py_err_occurred() != 0:
        if py_exc_matches(py_current_exception(), py_exc_builtin_class(8)) != 0:
            py_clear_exception()
            return 0
        return -1
    return _dict_slot_error(cstr("dictionary iterator returned NULL without StopIteration"))


def _dict_decimal_append(buf, pos: int, value: int) -> int:
    digits = stack_alloc(24)
    count: int = 0
    while count == 0 or value > 0:
        quotient: int = unsigned_div_i64(value, 10)
        store_i8(digits, 23 - count, 48 + value - quotient * 10)
        value = quotient
        count = count + 1
    while count > 0:
        store_i8(buf, pos, load_i8(digits, 24 - count))
        pos = pos + 1
        count = count - 1
    return pos


def _dict_text_append(buf, pos: int, text) -> int:
    index: int = 0
    while load_i8(text, index) != 0:
        store_i8(buf, pos, load_i8(text, index))
        pos = pos + 1
        index = index + 1
    return pos


def _dict_raise_sequence_element(element: int, length: int) -> int:
    """CPython's dict_merge_from_seq2 errors for sequence element ``element``.

    A negative ``length`` is an element that is not iterable (TypeError).
    """
    buf = stack_alloc(128)
    pos: int = 0
    tag: int = 2  # ValueError
    if length < 0:
        tag = 3  # TypeError
        pos = _dict_text_append(buf, pos, cstr("cannot convert dictionary update sequence element #"))
        pos = _dict_decimal_append(buf, pos, element)
        pos = _dict_text_append(buf, pos, cstr(" to a sequence"))
    else:
        pos = _dict_text_append(buf, pos, cstr("dictionary update sequence element #"))
        pos = _dict_decimal_append(buf, pos, element)
        pos = _dict_text_append(buf, pos, cstr(" has length "))
        pos = _dict_decimal_append(buf, pos, length)
        pos = _dict_text_append(buf, pos, cstr("; 2 is required"))
    store_i8(buf, pos, 0)
    py_raise_owned(py_exc_new(tag, buf))
    return -1


def _dict_slot_pair(slots, tokens, element: int) -> int:
    # Keep every yielded object until conversion completes, including third
    # and later values. Early decref can run a finalizer while the iterator
    # is still producing values and change its behavior or exception.
    store_ptr(slots, 13 * C_POINTER_SIZE, py_list_new(0))
    if _dict_slot_adopt(slots, tokens, 13) != 0:
        return -1
    store_ptr(slots, 11 * C_POINTER_SIZE, py_obj_iter(load_ptr(slots, 8 * C_POINTER_SIZE)))
    if (ptr_is_null(load_ptr(slots, 11 * C_POINTER_SIZE)) != 0 and py_err_occurred() != 0
            and py_exc_matches(py_current_exception(), py_exc_builtin_class(3)) != 0):
        py_clear_exception()
        return _dict_raise_sequence_element(element, -1)
    if _dict_slot_adopt(slots, tokens, 11) != 0:
        return -1
    while True:
        state: int = _dict_slot_next(slots, tokens, 11, 12)
        if state < 0:
            return -1
        if state == 0:
            break
        py_list_append(load_ptr(slots, 13 * C_POINTER_SIZE), load_ptr(slots, 12 * C_POINTER_SIZE))
        if py_err_occurred() != 0:
            return -1
        _dict_slot_drop(slots, tokens, 12)
    _dict_slot_drop(slots, tokens, 11)
    length: int = py_list_len(load_ptr(slots, 13 * C_POINTER_SIZE))
    if length != 2:
        return _dict_raise_sequence_element(element, length)
    items = load_ptr(load_ptr(slots, 13 * C_POINTER_SIZE), PYLISTOBJECT_ITEMS_OFFSET)
    if _dict_slot_copy(slots, tokens, 5, items) != 0:
        return -1
    items = load_ptr(load_ptr(slots, 13 * C_POINTER_SIZE), PYLISTOBJECT_ITEMS_OFFSET)
    if _dict_slot_copy(slots, tokens, 6, ptr_add(items, C_POINTER_SIZE)) != 0:
        return -1
    _dict_slot_drop(slots, tokens, 13)
    return 0


def _dict_slot_update_protocol(slots, tokens) -> int:
    # Ordinary keys attribute lookup intentionally honors properties,
    # instance attributes and __getattr__; it is not special-method lookup.
    store_ptr(slots, 3 * C_POINTER_SIZE,
              py_obj_getattr(load_ptr(slots, 2 * C_POINTER_SIZE), cstr("keys")))
    mapping: int = 0
    if ptr_is_null(load_ptr(slots, 3 * C_POINTER_SIZE)) != 0:
        if py_err_occurred() != 0:
            if py_exc_matches(py_current_exception(), py_exc_builtin_class(6)) == 0:
                return -1
            py_clear_exception()
    else:
        mapping = 1
        if _dict_slot_adopt(slots, tokens, 3) != 0:
            return -1
        # dict.__init__/update first tests for keys, then PyMapping_Keys
        # performs a fresh ordinary lookup. A descriptor can change or fail
        # between these two observable accesses.
        _dict_slot_drop(slots, tokens, 3)
        store_ptr(slots, 3 * C_POINTER_SIZE,
                  py_obj_getattr(load_ptr(slots, 2 * C_POINTER_SIZE), cstr("keys")))
        if _dict_slot_adopt(slots, tokens, 3) != 0:
            return -1
        store_ptr(slots, 4 * C_POINTER_SIZE, py_tuple_new(0))
        if _dict_slot_adopt(slots, tokens, 4) != 0:
            return -1
        if py_obj_call_slots(ptr_add(slots, 3 * C_POINTER_SIZE),
                             ptr_add(slots, 4 * C_POINTER_SIZE), null(),
                             ptr_add(slots, 10 * C_POINTER_SIZE)) != 0:
            return -1
        if _dict_slot_adopt(slots, tokens, 10) != 0:
            return -1
    source_index: int = 10 if mapping != 0 else 2
    if mapping != 0 and (is_tagged_int(load_ptr(slots, 10 * C_POINTER_SIZE)) != 0
            or load_i32(load_ptr(slots, 10 * C_POINTER_SIZE), PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_LIST):
        # PyMapping_Keys materializes non-list method results before the
        # first __getitem__ or destination mutation. Keep its error boundary.
        store_ptr(slots, 9 * C_POINTER_SIZE, py_list_new(0))
        if _dict_slot_adopt(slots, tokens, 9) != 0:
            return -1
        store_ptr(slots, 7 * C_POINTER_SIZE, py_obj_iter(load_ptr(slots, 10 * C_POINTER_SIZE)))
        if _dict_slot_adopt(slots, tokens, 7) != 0:
            return -1
        while True:
            next_state: int = _dict_slot_next(slots, tokens, 7, 8)
            if next_state < 0:
                return -1
            if next_state == 0:
                break
            py_list_append(load_ptr(slots, 9 * C_POINTER_SIZE), load_ptr(slots, 8 * C_POINTER_SIZE))
            if py_err_occurred() != 0:
                return -1
            _dict_slot_drop(slots, tokens, 8)
        _dict_slot_drop(slots, tokens, 7)
        source_index = 9
    store_ptr(slots, 7 * C_POINTER_SIZE,
              py_obj_iter(load_ptr(slots, source_index * C_POINTER_SIZE)))
    if _dict_slot_adopt(slots, tokens, 7) != 0:
        return -1
    _dict_slot_drop(slots, tokens, 10)
    _dict_slot_drop(slots, tokens, 9)
    _dict_slot_drop(slots, tokens, 4)
    _dict_slot_drop(slots, tokens, 3)
    element: int = 0
    while True:
        state: int = _dict_slot_next(slots, tokens, 7, 8)
        if state <= 0:
            return state
        if mapping != 0:
            if _dict_slot_copy(slots, tokens, 5, ptr_add(slots, 8 * C_POINTER_SIZE)) != 0:
                return -1
            store_ptr(slots, 6 * C_POINTER_SIZE,
                      py_obj_getitem(load_ptr(slots, 2 * C_POINTER_SIZE),
                                     load_ptr(slots, 5 * C_POINTER_SIZE)))
            if _dict_slot_adopt(slots, tokens, 6) != 0:
                return -1
        elif _dict_slot_pair(slots, tokens, element) != 0:
            return -1
        if py_dict_set_slots(ptr_add(slots, C_POINTER_SIZE),
                             ptr_add(slots, 5 * C_POINTER_SIZE),
                             ptr_add(slots, 6 * C_POINTER_SIZE)) != 0:
            return -1
        _dict_slot_drop(slots, tokens, 8)
        _dict_slot_drop(slots, tokens, 6)
        _dict_slot_drop(slots, tokens, 5)
        element = element + 1
    return 0


def _dict_slot_update_exact(slots, tokens) -> int:
    # Snapshot owning entries and their cached hashes before any destination
    # equality callbacks. This also handles updating a dictionary from itself.
    if ptr_eq(load_ptr(slots, C_POINTER_SIZE), load_ptr(slots, 2 * C_POINTER_SIZE)) != 0:
        return 0
    pcc_py_gc_minor_graph_lock()
    count: int = load_i64(load_ptr(slots, 2 * C_POINTER_SIZE), PYDICTOBJECT_ITEM_COUNT_OFFSET)
    pcc_py_gc_minor_graph_unlock()
    if count == 0:
        return 0
    hashes = malloc(count * 8)
    if ptr_is_null(hashes) != 0:
        py_raise_owned(py_exc_new(19, cstr("dictionary update snapshot allocation failed")))
        return -1
    store_ptr(slots, 9 * C_POINTER_SIZE, py_tuple_new(count * 2))
    status: int = _dict_slot_adopt(slots, tokens, 9)
    index: int = 0
    output: int = 0
    key_plan = stack_alloc(256)
    value_plan = stack_alloc(256)
    while status == 0 and output < count:
        prepared: int = 0
        key_token: int = 0
        value_token: int = 0
        pcc_py_gc_minor_graph_lock()
        source = load_ptr(slots, 2 * C_POINTER_SIZE)
        used: int = load_i64(source, PYDICTOBJECT_ENTRIES_USED_OFFSET)
        if index >= used:
            status = -1
        else:
            entries = load_ptr(source, PYDICTOBJECT_ENTRIES_OFFSET)
            offset: int = index * DICTENTRY_SIZE
            if ptr_is_null(load_ptr(entries, offset + DICTENTRY_KEY_OFFSET)) == 0:
                key_token = pcc_gc_root_copy_lease_prepare_locked(
                    ptr_add(slots, 5 * C_POINTER_SIZE),
                    ptr_add(entries, offset + DICTENTRY_KEY_OFFSET), 0, key_plan)
                value_token = pcc_gc_root_copy_lease_prepare_locked(
                    ptr_add(slots, 6 * C_POINTER_SIZE),
                    ptr_add(entries, offset + DICTENTRY_VALUE_OFFSET), 0, value_plan)
                prepared = 1
                if key_token >= 0:
                    store_i64(tokens, 5 * C_POINTER_SIZE, key_token)
                if value_token >= 0:
                    store_i64(tokens, 6 * C_POINTER_SIZE, value_token)
                store_i64(hashes, output * 8, load_i64(entries, offset + DICTENTRY_HASH_OFFSET))
        pcc_py_gc_minor_graph_unlock()
        if prepared != 0:
            pcc_gc_root_copy_lease_finish(key_plan)
            pcc_gc_root_copy_lease_finish(value_plan)
            if key_token < 0 or value_token < 0:
                status = -1
            else:
                py_tuple_set_item(load_ptr(slots, 9 * C_POINTER_SIZE), output * 2,
                                  load_ptr(slots, 5 * C_POINTER_SIZE))
                py_tuple_set_item(load_ptr(slots, 9 * C_POINTER_SIZE), output * 2 + 1,
                                  load_ptr(slots, 6 * C_POINTER_SIZE))
                if py_err_occurred() != 0:
                    status = -1
                output = output + 1
            _dict_slot_drop(slots, tokens, 6)
            _dict_slot_drop(slots, tokens, 5)
        index = index + 1
    index = 0
    while status == 0 and index < output:
        snapshot = load_ptr(slots, 9 * C_POINTER_SIZE)
        status = _dict_slot_copy(slots, tokens, 5,
            ptr_add(snapshot, PYTUPLEOBJECT_ITEMS_OFFSET + index * 2 * C_POINTER_SIZE))
        if status == 0:
            snapshot = load_ptr(slots, 9 * C_POINTER_SIZE)
            status = _dict_slot_copy(slots, tokens, 6,
                ptr_add(snapshot, PYTUPLEOBJECT_ITEMS_OFFSET + (index * 2 + 1) * C_POINTER_SIZE))
        if status == 0:
            status = _dict_slot_set_bound(ptr_add(slots, C_POINTER_SIZE),
                ptr_add(slots, 5 * C_POINTER_SIZE), ptr_add(slots, 6 * C_POINTER_SIZE),
                1, load_i64(hashes, index * 8), 0, null())
        _dict_slot_drop(slots, tokens, 6)
        _dict_slot_drop(slots, tokens, 5)
        index = index + 1
    free(hashes)
    if status != 0:
        return _dict_slot_error(cstr("dictionary changed during snapshot or snapshot transfer failed"))
    return 0


def _dict_slot_update_bound(dict_slot, source_slot, borrowed: int) -> int:
    slots = stack_alloc(16 * C_POINTER_SIZE)
    tokens = stack_alloc(16 * C_POINTER_SIZE)
    handles = stack_alloc(16 * C_POINTER_SIZE)
    count: int = _dict_slot_open(slots, tokens, handles, 16)
    suspended: int = 0
    status: int = -1
    if count == 16:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        if borrowed != 0:
            first: int = pcc_gc_root_copy_borrowed_lease(ptr_add(slots, C_POINTER_SIZE), dict_slot)
            second: int = pcc_gc_root_copy_borrowed_lease(ptr_add(slots, 2 * C_POINTER_SIZE), source_slot)
            if first >= 0:
                store_i64(tokens, C_POINTER_SIZE, first)
            if second >= 0:
                store_i64(tokens, 2 * C_POINTER_SIZE, second)
            status = 0 if first >= 0 and second >= 0 else -1
        else:
            status = _dict_slot_copy(slots, tokens, 1, dict_slot)
            if status == 0:
                status = _dict_slot_copy(slots, tokens, 2, source_slot)
        if status == 0:
            if not _ptr_is_dict(load_ptr(slots, C_POINTER_SIZE)):
                py_raise_owned(py_exc_new(3, cstr("dictionary update destination must be a dict")))
                status = -1
            elif _ptr_is_dict(load_ptr(slots, 2 * C_POINTER_SIZE)):
                status = _dict_slot_update_exact(slots, tokens)
            else:
                status = _dict_slot_update_protocol(slots, tokens)
    if status != 0:
        _dict_slot_error(cstr("dictionary update failed without an exception"))
    _dict_slot_close(slots, tokens, handles, count, suspended, 16)
    return status


@c_abi_export("py_dict_update_slots")
def py_dict_update_slots(dict_slot, source_slot) -> int:
    return _dict_slot_update_bound(dict_slot, source_slot, 0)


@c_abi_export("py_dict_setdefault_slots")
def py_dict_setdefault_slots(dict_slot, key_slot, default_slot, result_slot) -> int:
    return _dict_slot_set_bound(dict_slot, key_slot, default_slot, 0, 0, 1, result_slot)


# Materialization reuses the existing dictionary slot frame and its TLS/error
# cleanup protocol. Each role has one owner and independently counted lease.
_DICT_MATERIALIZE_RECEIVER = 1
_DICT_MATERIALIZE_OUTPUT = 2
_DICT_MATERIALIZE_KEY = 3
_DICT_MATERIALIZE_VALUE = 4
_DICT_MATERIALIZE_PAIR = 5
_DICT_MATERIALIZE_KEYS = 0
_DICT_MATERIALIZE_VALUES = 1
_DICT_MATERIALIZE_ITEMS = 2
_DICT_MATERIALIZE_SLOT_COUNT = 16

pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_take_pinned_slot = extern("pcc_gc_take_pinned_slot", (c_ptr, c_int64), c_ptr)
define_global_i32("pcc_dict_materialize_borrowed_map", -1)
define_global_i32("pcc_dict_materialize_result_map", 2)


def _dict_materialize_entry(slots, tokens, index: int, key_plan, value_plan) -> int:
    # Select a key/value pair from the actual current owning table under one
    # graph transaction. No table address or borrowed child crosses unlock.
    key_slot = ptr_add(slots, _DICT_MATERIALIZE_KEY * C_POINTER_SIZE)
    value_slot = ptr_add(slots, _DICT_MATERIALIZE_VALUE * C_POINTER_SIZE)
    prepared: int = 0
    key_token: int = 0
    value_token: int = 0
    pcc_py_gc_minor_graph_lock()
    receiver = load_ptr(slots, _DICT_MATERIALIZE_RECEIVER * C_POINTER_SIZE)
    if index < load_i64(receiver, PYDICTOBJECT_ENTRIES_USED_OFFSET):
        entries = load_ptr(receiver, PYDICTOBJECT_ENTRIES_OFFSET)
        offset: int = index * DICTENTRY_SIZE
        key_source = ptr_add(entries, offset + DICTENTRY_KEY_OFFSET)
        if ptr_is_null(load_ptr(key_source, 0)) == 0:
            key_token = pcc_gc_root_copy_lease_prepare_locked(key_slot, key_source, 0, key_plan)
            prepared = 1
            if key_token >= 0:
                store_i64(tokens, _DICT_MATERIALIZE_KEY * C_POINTER_SIZE, key_token)
                value_token = pcc_gc_root_copy_lease_prepare_locked(
                    value_slot, ptr_add(entries, offset + DICTENTRY_VALUE_OFFSET), 0, value_plan)
                prepared = 2
                if value_token >= 0:
                    store_i64(tokens, _DICT_MATERIALIZE_VALUE * C_POINTER_SIZE, value_token)
    pcc_py_gc_minor_graph_unlock()
    if prepared > 0:
        pcc_gc_root_copy_lease_finish(key_plan)
    if prepared > 1:
        pcc_gc_root_copy_lease_finish(value_plan)
    if key_token < 0 or value_token < 0:
        return _dict_slot_error(cstr("dictionary materialization item transfer failed"))
    return 1 if prepared != 0 else 0


def _dict_materialize_body(slots, tokens, kind: int) -> int:
    receiver = load_ptr(slots, _DICT_MATERIALIZE_RECEIVER * C_POINTER_SIZE)
    capacity: int = load_i64(receiver, PYDICTOBJECT_ITEM_COUNT_OFFSET)
    if capacity <= 0:
        capacity = 4
    store_ptr(slots, _DICT_MATERIALIZE_OUTPUT * C_POINTER_SIZE, py_list_new(capacity))
    if _dict_slot_adopt(slots, tokens, _DICT_MATERIALIZE_OUTPUT) != 0:
        return -1
    # Preserve the existing eager materialization's bounded entry count. Each
    # later selection reloads the current table instead of retaining a stale
    # payload pointer across list/tuple allocation or an append callback.
    pcc_py_gc_minor_graph_lock()
    limit: int = load_i64(load_ptr(slots, _DICT_MATERIALIZE_RECEIVER * C_POINTER_SIZE), PYDICTOBJECT_ENTRIES_USED_OFFSET)
    pcc_py_gc_minor_graph_unlock()
    key_plan = stack_alloc(256)
    value_plan = stack_alloc(256)
    index: int = 0
    while index < limit:
        selected: int = _dict_materialize_entry(slots, tokens, index, key_plan, value_plan)
        if selected < 0:
            return -1
        if selected != 0:
            item: int = _DICT_MATERIALIZE_KEY
            if kind == _DICT_MATERIALIZE_VALUES:
                item = _DICT_MATERIALIZE_VALUE
            elif kind == _DICT_MATERIALIZE_ITEMS:
                store_ptr(slots, _DICT_MATERIALIZE_PAIR * C_POINTER_SIZE, py_tuple_new(2))
                if _dict_slot_adopt(slots, tokens, _DICT_MATERIALIZE_PAIR) != 0:
                    return -1
                py_tuple_set_item(load_ptr(slots, _DICT_MATERIALIZE_PAIR * C_POINTER_SIZE), 0,
                    load_ptr(slots, _DICT_MATERIALIZE_KEY * C_POINTER_SIZE))
                if py_err_occurred() != 0:
                    return -1
                py_tuple_set_item(load_ptr(slots, _DICT_MATERIALIZE_PAIR * C_POINTER_SIZE), 1,
                    load_ptr(slots, _DICT_MATERIALIZE_VALUE * C_POINTER_SIZE))
                if py_err_occurred() != 0:
                    return -1
                item = _DICT_MATERIALIZE_PAIR
            py_list_append(load_ptr(slots, _DICT_MATERIALIZE_OUTPUT * C_POINTER_SIZE),
                load_ptr(slots, item * C_POINTER_SIZE))
            if py_err_occurred() != 0:
                return -1
            _dict_slot_drop(slots, tokens, _DICT_MATERIALIZE_PAIR)
            _dict_slot_drop(slots, tokens, _DICT_MATERIALIZE_VALUE)
            _dict_slot_drop(slots, tokens, _DICT_MATERIALIZE_KEY)
        index = index + 1
    return 0


def _dict_materialize_bound(source_slot, result_slot, kind: int) -> int:
    slots = stack_alloc(_DICT_MATERIALIZE_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_DICT_MATERIALIZE_SLOT_COUNT * C_POINTER_SIZE)
    handles = stack_alloc(_DICT_MATERIALIZE_SLOT_COUNT * C_POINTER_SIZE)
    count: int = _dict_slot_open(slots, tokens, handles, _DICT_MATERIALIZE_SLOT_COUNT)
    suspended: int = 0
    status: int = -1
    if count == _DICT_MATERIALIZE_SLOT_COUNT:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        token: int = pcc_gc_root_copy_borrowed_lease(
            ptr_add(slots, _DICT_MATERIALIZE_RECEIVER * C_POINTER_SIZE), source_slot)
        if token >= 0:
            store_i64(tokens, _DICT_MATERIALIZE_RECEIVER * C_POINTER_SIZE, token)
            status = _dict_materialize_body(slots, tokens, kind)
        if status == 0:
            status = pcc_gc_root_move(result_slot, ptr_add(slots, _DICT_MATERIALIZE_OUTPUT * C_POINTER_SIZE))
            if status == 0:
                token = load_i64(tokens, _DICT_MATERIALIZE_OUTPUT * C_POINTER_SIZE)
                store_i64(tokens, _DICT_MATERIALIZE_OUTPUT * C_POINTER_SIZE, 0)
                if pcc_gc_foreign_lease_release(result_slot, token) != 0:
                    pcc_platform_abort()
                    return -1
    if status != 0:
        _dict_slot_error(cstr("dictionary materialization failed without an exception"))
    _dict_slot_close(slots, tokens, handles, count, suspended, _DICT_MATERIALIZE_SLOT_COUNT)
    return status


def _dict_materialize_raw(receiver, kind: int):
    # Existing raw ABI: the caller keeps this borrowed input alive and stable
    # through entry. Once linked, the borrowed frame is the authoritative input
    # across scratch registration; it is never relabeled as a NEW reference.
    if not _ptr_is_dict(receiver):
        return null()
    borrowed = stack_alloc(C_POINTER_SIZE)
    store_ptr(borrowed, 0, receiver)
    pcc_gc_frame_enter(global_addr("pcc_dict_materialize_borrowed_map"), borrowed)
    result_slot = stack_alloc(2 * C_POINTER_SIZE)
    memset(result_slot, 0, 2 * C_POINTER_SIZE)
    error_slot = ptr_add(result_slot, C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_dict_materialize_result_map"), result_slot)
    _dict_materialize_bound(borrowed, result_slot, kind)
    token: int = pcc_gc_foreign_lease_acquire(result_slot)
    if token < 0:
        _dict_slot_error(cstr("dictionary result return lease failed"))
        py_tls_exc_swap_slot(error_slot)
        pcc_gc_store_root(result_slot, null())
        py_clear_exception()
        py_tls_exc_swap_slot(error_slot)
        token = 0
    prior: int = 0
    result = load_ptr(result_slot, 0)
    if ptr_is_null(result) == 0:
        prior = load_i32(result, PYOBJECTHEADER_FLAGS_OFFSET) & PY_FLAG_GC_PINNED
        pcc_gc_pin(result)
    pcc_gc_frame_leave(result_slot)
    pcc_gc_frame_leave(borrowed)
    if pcc_gc_foreign_lease_release(result_slot, token) != 0:
        pcc_platform_abort()
        return null()
    return pcc_gc_take_pinned_slot(result_slot, prior)
