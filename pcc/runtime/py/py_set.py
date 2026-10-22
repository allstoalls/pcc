"""Phase 4c.8: pcc-Python port of py_set.c.

Open-addressing hash set of PyObject* (unordered, unique).

PySetObject layout (from py_internal.h):
    offset  0   PyObjectHeader  (i64 refcount, i32 tag, i32 flags = 16)
    offset 16   size            (i64)
    offset 24   capacity        (i64)
    offset 32   fill            (i64)    (live + tombstones)
    offset 40   entries         (SetEntry*)
    total: 48 bytes

SetEntry layout:
    offset  0   hash            (i64)
    offset  8   key             (PyObject*)
    total: 16 bytes

Tombstone: a slot with key == py_set_dummy (never NULL, never a real heap object).
Empty slot: key == NULL.

Initial capacity: 8 (must be power of 2). Grow at 2/3 load factor.
Probing: CPython-style perturbation (perturb = hash; j = hash & mask;
next: perturb >>= 5; j = (j*5 + perturb + 1) & mask).
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import (
    PYOBJECTHEADER_TYPE_TAG_OFFSET,
    PYOBJECTHEADER_FLAGS_OFFSET,
    PYTUPLEOBJECT_ITEMS_OFFSET,
    PYTUPLEOBJECT_LEN_OFFSET,
    PY_TYPE_SET,
)
from pcc.extern import extern, c_abi_export, c_ptr, c_int32, c_int64, c_void
from pcc.unsafe import (
    cstr,
    free,
    global_load_ptr,
    global_addr,
    define_global_i32,
    ptr_add,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    memset,
    null,
    ptr_eq,
    ptr_is_null,
    stack_alloc,
    store_i32,
    store_i64,
    store_ptr,
)

py_incref            = extern("py_incref",            (c_ptr,),                     c_void)
py_decref            = extern("py_decref",            (c_ptr,),                     c_void)
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
pcc_gc_backend4_retarget_mutator_payload_locked = extern(
    "pcc_gc_backend4_retarget_mutator_payload_locked",
    (c_ptr, c_ptr, c_int64, c_ptr, c_int64, c_ptr, c_int64),
    c_int64,
)
py_obj_hash          = extern("py_obj_hash",          (c_ptr,),                     c_int64)
py_obj_eq            = extern("py_obj_eq",            (c_ptr, c_ptr),               c_int32)
py_err_occurred      = extern("py_err_occurred",      (),                           c_int64)
py_obj_iter = extern("py_obj_iter", (c_ptr,), c_ptr)
py_obj_next = extern("py_obj_next", (c_ptr,), c_ptr)
py_current_exception = extern("py_current_exception", (), c_ptr)
py_exc_builtin_class = extern("py_exc_builtin_class", (c_int64,), c_ptr)
py_exc_matches = extern("py_exc_matches", (c_ptr, c_ptr), c_int64)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_exc_new           = extern("py_exc_new",           (c_int64, c_ptr),             c_ptr)
py_raise             = extern("py_raise",             (c_ptr,),                     c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_gc_track          = extern("py_gc_track",          (c_ptr,),                     c_void)
pcc_gc_store_ptr     = extern("pcc_gc_store_ptr",     (c_ptr, c_ptr, c_ptr),        c_void)
pcc_gc_store_ptr_plan_init = extern(
    "pcc_gc_store_ptr_plan_init", (c_ptr, c_ptr, c_int64), c_void
)
pcc_gc_store_ptr_plan_commit_locked = extern(
    "pcc_gc_store_ptr_plan_commit_locked",
    (c_ptr, c_ptr, c_ptr, c_ptr),
    c_int64,
)
pcc_gc_store_ptr_plan_commit_sentinel_aware_locked = extern(
    "pcc_gc_store_ptr_plan_commit_sentinel_aware_locked",
    (c_ptr, c_ptr, c_ptr, c_ptr, c_ptr),
    c_int64,
)
pcc_gc_store_ptr_plan_finish = extern(
    "pcc_gc_store_ptr_plan_finish", (c_ptr,), c_void
)
pcc_gc_load_ptr      = extern("pcc_gc_load_ptr",      (c_ptr, c_ptr),               c_ptr)
pcc_gc_note_slot_write_barrier = extern(
    "pcc_gc_note_slot_write_barrier", (c_ptr, c_ptr, c_ptr), c_void,
)
pcc_gc_alloc         = extern("pcc_gc_alloc",         (c_int64, c_int32, c_int32),  c_ptr)
py_list_new          = extern("py_list_new",          (c_int64,),                   c_ptr)
py_list_append       = extern("py_list_append",       (c_ptr, c_ptr),               c_void)
py_list_get          = extern("py_list_get",          (c_ptr, c_int64),             c_ptr)
py_list_len          = extern("py_list_len",          (c_ptr,),                     c_int64)
py_dict_len = extern("py_dict_len", (c_ptr,), c_int64)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
pcc_gc_root_copy_lease = extern("pcc_gc_root_copy_lease", (c_ptr, c_ptr), c_int64)
pcc_gc_root_copy_lease_prepare_locked = extern(
    "pcc_gc_root_copy_lease_prepare_locked", (c_ptr, c_ptr, c_int64, c_ptr), c_int64)
pcc_gc_root_copy_lease_finish = extern("pcc_gc_root_copy_lease_finish", (c_ptr,), c_void)
pcc_gc_resolve_root_slot_unlocked = extern(
    "pcc_gc_resolve_root_slot_unlocked", (c_ptr, c_int64), c_ptr)
pcc_gc_root_move = extern("pcc_gc_root_move", (c_ptr, c_ptr), c_int64)
pcc_gc_foreign_lease_acquire = extern("pcc_gc_foreign_lease_acquire", (c_ptr,), c_int64)
pcc_gc_foreign_lease_release = extern("pcc_gc_foreign_lease_release", (c_ptr, c_int64), c_int64)


# INITIAL_CAPACITY is intentionally NOT a module-level constant —
# pcc-Python initializes module-level integers in the auto-generated
# main(), which the Makefile strips for library .o builds. Inline 8
# at the call site instead.


def _ptr_is_set(o) -> bool:
    if ptr_is_null(o) != 0:
        return False
    if is_tagged_int(o) != 0:
        return False
    return load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_SET


def _set_read_prepare_root(slot, value, backend: int):
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


def _set_read_root_failed(value, backend: int, handle) -> int:
    if backend != 3 and backend != 4:
        return 0
    if ptr_is_null(value) != 0 or is_tagged_int(value) != 0:
        return 0
    return ptr_is_null(handle)


def _set_read_reload_root(slot, handle):
    value = load_ptr(slot, 0)
    if ptr_is_null(handle) == 0:
        value = pcc_gc_load_ptr(null(), slot)
        store_ptr(slot, 0, value)
    return value


def _set_read_finish_root(handle) -> None:
    if ptr_is_null(handle) == 0:
        pcc_gc_scheduler_root_unregister_handle(handle)


def _alloc_entries(capacity: int):
    # SetEntry is 16 bytes: i64 hash + ptr key.
    total = capacity * 16
    entries = malloc(total)
    if ptr_is_null(entries) != 0:
        return entries
    memset(entries, 0, total)
    return entries


def _entry_key(s, entries, slot_off: int):
    k = load_ptr(entries, slot_off + 8)
    if ptr_is_null(k) != 0:
        return k
    if ptr_eq(k, global_load_ptr("py_set_dummy")) != 0:
        return k
    return pcc_gc_load_ptr(s, ptr_add(entries, slot_off + 8))


def _perturb_shift5(perturb: int) -> int:
    # Mirror ``(uint64_t)perturb >> 5`` while the pcc-Python runtime exposes
    # only signed i64 arithmetic.  Arithmetic shift differs from logical
    # shift by exactly 2**59 when the input's high bit is set.
    shifted: int = perturb >> 5
    if perturb < 0:
        shifted = shifted + 576460752303423488
    return shifted


def _lookup_slot(s, entries, capacity: int, hash_val: int, key) -> int:
    # Returns slot index (>=0) if key is found, or -(slot+1) for the
    # insert target if not found (negative encoding).
    mask: int = capacity - 1
    perturb: int = hash_val
    j: int = hash_val & mask
    first_tombstone: int = -1
    dummy = global_load_ptr("py_set_dummy")
    probes: int = 0
    # Probe budget.  The old bound was ``capacity * 2``, which is NOT
    # sufficient: ``perturb`` needs 13 shifts to decay from a 64-bit value to
    # zero, and only once it IS zero does ``j = (j * 5 + 1) & mask`` become a
    # full-period generator over the table (a = 5, c = 1, m = 2**k satisfies
    # Hull-Dobell).  At capacity 8 that left three full-period probes, so a
    # run of negative pointer-aligned keys could cycle over a handful of slots
    # while free slots were never visited, and the element was dropped in
    # silence.  ``capacity + 16`` covers the 13 decay steps plus a full period
    # with margin, and is tighter than ``capacity * 2`` for large tables.
    limit: int = capacity + 16

    while probes < limit:
        slot_off: int = j * 16
        k = _entry_key(s, entries, slot_off)
        if ptr_is_null(k) != 0:
            if first_tombstone >= 0:
                return -(first_tombstone + 1)
            return -(j + 1)
        if ptr_eq(k, dummy) != 0:
            if first_tombstone < 0:
                first_tombstone = j
        else:
            slot_hash: int = load_i64(entries, slot_off)
            if slot_hash == hash_val:
                if ptr_eq(k, key) != 0:
                    return j
                if py_obj_eq(k, key) != 0:
                    return j
        perturb = _perturb_shift5(perturb)
        j = (j * 5 + perturb + 1) & mask
        probes = probes + 1

    fallback_slot: int = 0
    if first_tombstone >= 0:
        fallback_slot = first_tombstone
    return -(fallback_slot + 1)


def _set_remove_rooted_slot(
    set_slot,
    set_handle,
    entries,
    capacity: int,
    slot: int,
    expected_slot,
    expected_hash: int,
) -> int:
    s = _set_read_reload_root(set_slot, set_handle)
    plan = stack_alloc(128)
    pcc_gc_store_ptr_plan_init(plan, s, pcc_gc_backend())
    pcc_py_gc_minor_graph_lock()
    s = _set_read_reload_root(set_slot, set_handle)
    committed: int = 0
    if _ptr_is_set(s):
        if (
            ptr_eq(load_ptr(s, 40), entries) != 0
            and load_i64(s, 24) == capacity
            and slot >= 0
            and slot < capacity
        ):
            slot_off: int = slot * 16
            key = load_ptr(entries, slot_off + 8)
            dummy = global_load_ptr("py_set_dummy")
            matches: int = 1
            if ptr_is_null(expected_slot) == 0:
                # A successful lookup is not a reservation. Another mutator
                # can remove that entry and reuse its tombstone before this
                # lock is acquired, without changing table/capacity. The
                # expected owning root is counted-leased through this commit.
                matches = ptr_eq(key, load_ptr(expected_slot, 0))
                if load_i64(entries, slot_off) != expected_hash:
                    matches = 0
            if matches != 0 and ptr_is_null(key) == 0 and ptr_eq(key, dummy) == 0:
                # The tombstone is a sentinel, not a reference.  Storing it
                # through the ordinary commit path increfed it, and the incref
                # begins with the provenance probe, so every discard paid
                # pcc_gc_granule_is_object_start to learn that py_set_dummy is
                # not an object.  The sentinel path still releases the old key.
                committed = pcc_gc_store_ptr_plan_commit_sentinel_aware_locked(
                    plan,
                    s,
                    ptr_add(entries, slot_off + 8),
                    dummy,
                    dummy,
                )
                if committed != 0:
                    size: int = load_i64(s, 16)
                    store_i64(s, 16, size - 1)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_store_ptr_plan_finish(plan)
    return committed


def _set_add_rooted_slot(
    set_slot,
    set_handle,
    item_slot,
    item_handle,
    entries,
    capacity: int,
    slot: int,
    hash_val: int,
) -> int:
    s = _set_read_reload_root(set_slot, set_handle)
    item = _set_read_reload_root(item_slot, item_handle)
    plan = stack_alloc(128)
    pcc_gc_store_ptr_plan_init(plan, s, pcc_gc_backend())
    pcc_py_gc_minor_graph_lock()
    s = _set_read_reload_root(set_slot, set_handle)
    item = _set_read_reload_root(item_slot, item_handle)
    committed: int = 0
    if _ptr_is_set(s):
        if (
            ptr_eq(load_ptr(s, 40), entries) != 0
            and load_i64(s, 24) == capacity
            and slot >= 0
            and slot < capacity
        ):
            slot_off: int = slot * 16
            old = load_ptr(entries, slot_off + 8)
            dummy = global_load_ptr("py_set_dummy")
            if ptr_is_null(old) != 0 or ptr_eq(old, dummy) != 0:
                was_tombstone: int = ptr_eq(old, dummy)
                committed = pcc_gc_store_ptr_plan_commit_sentinel_aware_locked(
                    plan,
                    s,
                    ptr_add(entries, slot_off + 8),
                    item,
                    dummy,
                )
                if committed != 0:
                    store_i64(entries, slot_off, hash_val)
                    size: int = load_i64(s, 16)
                    store_i64(s, 16, size + 1)
                    if was_tombstone == 0:
                        fill: int = load_i64(s, 32)
                        store_i64(s, 32, fill + 1)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_store_ptr_plan_finish(plan)
    if committed != 0:
        s = _set_read_reload_root(set_slot, set_handle)
        _maybe_grow(s)
    return committed


def _set_lookup_rooted(s, item, mode: int, hash_val: int, hash_known: int) -> int:
    backend: int = pcc_gc_backend()
    set_slot = stack_alloc(8)
    item_slot = stack_alloc(8)
    candidate_slot = stack_alloc(8)
    set_handle = _set_read_prepare_root(set_slot, s, backend)
    if _set_read_root_failed(s, backend, set_handle) != 0:
        return 0
    item_handle = _set_read_prepare_root(item_slot, item, backend)
    if _set_read_root_failed(item, backend, item_handle) != 0:
        _set_read_finish_root(set_handle)
        return 0
    item = _set_read_reload_root(item_slot, item_handle)
    if hash_known == 0:
        hash_val = py_obj_hash(item)
    s = _set_read_reload_root(set_slot, set_handle)
    item = _set_read_reload_root(item_slot, item_handle)
    if py_err_occurred() != 0:
        _set_read_finish_root(item_handle)
        _set_read_finish_root(set_handle)
        return 0

    attempts: int = 0
    done: int = 0
    found: int = 0
    while attempts < 16 and done == 0:
        attempts = attempts + 1
        s = _set_read_reload_root(set_slot, set_handle)
        item = _set_read_reload_root(item_slot, item_handle)
        if not _ptr_is_set(s):
            done = 1
            continue
        capacity: int = load_i64(s, 24)
        entries = load_ptr(s, 40)
        if capacity <= 0 or ptr_is_null(entries) != 0:
            done = 1
            continue
        mask: int = capacity - 1
        perturb: int = hash_val
        j: int = hash_val & mask
        probes: int = 0
        restart: int = 0
        first_tombstone: int = -1
        dummy = global_load_ptr("py_set_dummy")
        while probes < capacity + 16 and done == 0 and restart == 0:
            slot_off: int = j * 16
            entry_key = _entry_key(s, entries, slot_off)
            if ptr_is_null(entry_key) != 0:
                if mode == 2:
                    target: int = j
                    if first_tombstone >= 0:
                        target = first_tombstone
                    found = _set_add_rooted_slot(
                        set_slot,
                        set_handle,
                        item_slot,
                        item_handle,
                        entries,
                        capacity,
                        target,
                        hash_val,
                    )
                done = 1
            elif ptr_eq(entry_key, dummy) != 0:
                if first_tombstone < 0:
                    first_tombstone = j
            else:
                entry_hash: int = load_i64(entries, slot_off)
                if entry_hash == hash_val:
                    if ptr_eq(entry_key, item) != 0:
                        found = 1
                        if mode == 1:
                            found = _set_remove_rooted_slot(
                                set_slot,
                                set_handle,
                                entries,
                                capacity,
                                j,
                                null(),
                                0,
                            )
                        done = 1
                    elif not (
                        is_tagged_int(entry_key) != 0
                        and is_tagged_int(item) != 0
                    ):
                        py_incref(entry_key)
                        candidate_handle = _set_read_prepare_root(
                            candidate_slot, entry_key, backend
                        )
                        if _set_read_root_failed(
                            entry_key, backend, candidate_handle
                        ) != 0:
                            py_decref(entry_key)
                            done = 1
                        else:
                            before_s = s
                            equal: int = py_obj_eq(entry_key, item)
                            s = _set_read_reload_root(set_slot, set_handle)
                            item = _set_read_reload_root(item_slot, item_handle)
                            candidate = _set_read_reload_root(
                                candidate_slot, candidate_handle
                            )
                            _set_read_finish_root(candidate_handle)
                            stable: int = 0
                            if ptr_eq(s, before_s) != 0 and _ptr_is_set(s):
                                if (
                                    load_i64(s, 24) == capacity
                                    and ptr_eq(load_ptr(s, 40), entries) != 0
                                ):
                                    current = _entry_key(s, entries, slot_off)
                                    if ptr_eq(current, candidate) != 0:
                                        stable = 1
                            py_decref(candidate)
                            if py_err_occurred() != 0:
                                _set_read_finish_root(item_handle)
                                _set_read_finish_root(set_handle)
                                return 0
                            if stable == 0:
                                restart = 1
                            elif equal != 0:
                                found = 1
                                if mode == 1:
                                    found = _set_remove_rooted_slot(
                                        set_slot,
                                        set_handle,
                                        entries,
                                        capacity,
                                        j,
                                        null(),
                                        0,
                                    )
                                done = 1
            if done == 0 and restart == 0:
                perturb = _perturb_shift5(perturb)
                j = (j * 5 + perturb + 1) & mask
                probes = probes + 1
        if done == 0 and restart == 0 and mode == 2:
            if first_tombstone >= 0:
                found = _set_add_rooted_slot(
                    set_slot,
                    set_handle,
                    item_slot,
                    item_handle,
                    entries,
                    capacity,
                    first_tombstone,
                    hash_val,
                )
            done = 1
    _set_read_finish_root(item_handle)
    _set_read_finish_root(set_handle)
    return found


def _rehash_find_empty_slot(
    entries, capacity: int, hash_value: int
) -> int:
    mask: int = capacity - 1
    perturb: int = hash_value
    slot: int = hash_value & mask
    probes: int = 0
    while probes < capacity + 16:
        if ptr_is_null(load_ptr(entries, slot * 16 + 8)) != 0:
            return slot
        perturb = _perturb_shift5(perturb)
        slot = (slot * 5 + perturb + 1) & mask
        probes = probes + 1
    return -1


def _rehash_refcount_fast(s, new_capacity: int) -> int:
    old_entries = load_ptr(s, 40)
    old_capacity: int = load_i64(s, 24)
    new_entries = _alloc_entries(new_capacity)
    if ptr_is_null(new_entries) != 0:
        return -1
    dummy = global_load_ptr("py_set_dummy")
    old_index: int = 0
    new_size: int = 0
    while old_index < old_capacity:
        old_off: int = old_index * 16
        key = load_ptr(old_entries, old_off + 8)
        if ptr_is_null(key) == 0:
            if ptr_eq(key, dummy) == 0:
                hash_value: int = load_i64(old_entries, old_off)
                target_slot: int = _rehash_find_empty_slot(
                    new_entries, new_capacity, hash_value
                )
                if target_slot < 0:
                    free(new_entries)
                    return -1
                new_off: int = target_slot * 16
                store_i64(new_entries, new_off, hash_value)
                store_ptr(new_entries, new_off + 8, key)
                new_size = new_size + 1
        old_index = old_index + 1
    store_ptr(s, 40, new_entries)
    store_i64(s, 24, new_capacity)
    store_i64(s, 16, new_size)
    store_i64(s, 32, new_size)
    free(old_entries)
    return 0


def _rehash(s, new_capacity: int) -> int:
    if ptr_is_null(s) != 0 or new_capacity <= 0:
        return -1
    initial_backend: int = pcc_gc_backend()
    if initial_backend == 0:
        return _rehash_refcount_fast(s, new_capacity)
    owner_slot = stack_alloc(8)
    store_ptr(owner_slot, 0, s)
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
            s = pcc_gc_load_ptr(null(), owner_slot)
            store_ptr(owner_slot, 0, s)
        old_entries = load_ptr(s, 40)
        old_capacity: int = load_i64(s, 24)
        old_size: int = load_i64(s, 16)
        old_fill: int = load_i64(s, 32)
        pcc_py_gc_minor_graph_unlock()
        if ptr_is_null(old_entries) != 0:
            break
        if old_capacity <= 0 or new_capacity < old_capacity:
            break
        if old_size < 0 or old_size > new_capacity:
            break
        if old_fill < old_size or old_fill > old_capacity:
            break

        new_entries = _alloc_entries(new_capacity)
        slot_pairs = malloc(old_capacity * 16)
        if ptr_is_null(new_entries) != 0 or ptr_is_null(slot_pairs) != 0:
            free(slot_pairs)
            free(new_entries)
            break
        memset(slot_pairs, 0, old_capacity * 16)

        pcc_py_gc_minor_graph_lock()
        if pcc_gc_backend() != initial_backend:
            pcc_py_gc_minor_graph_unlock()
            free(slot_pairs)
            free(new_entries)
            break
        if ptr_is_null(owner_handle) == 0:
            s = pcc_gc_load_ptr(null(), owner_slot)
            store_ptr(owner_slot, 0, s)
        if (
            ptr_eq(load_ptr(s, 40), old_entries) == 0
            or load_i64(s, 24) != old_capacity
            or load_i64(s, 16) != old_size
            or load_i64(s, 32) != old_fill
        ):
            pcc_py_gc_minor_graph_unlock()
            free(slot_pairs)
            free(new_entries)
            continue

        dummy = global_load_ptr("py_set_dummy")
        old_index: int = 0
        new_size: int = 0
        pair_count: int = 0
        copy_valid: int = 1
        while old_index < old_capacity:
            old_off: int = old_index * 16
            key = _entry_key(s, old_entries, old_off)
            if ptr_is_null(key) == 0:
                if ptr_eq(key, dummy) == 0:
                    hash_value: int = load_i64(old_entries, old_off)
                    target_slot: int = _rehash_find_empty_slot(
                        new_entries, new_capacity, hash_value
                    )
                    if target_slot < 0:
                        copy_valid = 0
                        break
                    new_off: int = target_slot * 16
                    store_i64(new_entries, new_off, hash_value)
                    store_ptr(new_entries, new_off + 8, key)
                    store_ptr(
                        slot_pairs,
                        pair_count * 16,
                        ptr_add(old_entries, old_off + 8),
                    )
                    store_ptr(
                        slot_pairs,
                        pair_count * 16 + 8,
                        ptr_add(new_entries, new_off + 8),
                    )
                    pair_count = pair_count + 1
                    new_size = new_size + 1
            old_index = old_index + 1

        retargeted: int = 0
        if copy_valid != 0:
            retargeted = pcc_gc_backend4_retarget_mutator_payload_locked(
                s,
                old_entries,
                old_capacity * 16,
                new_entries,
                new_capacity * 16,
                slot_pairs,
                pair_count,
            )
        if copy_valid == 0 or retargeted == 0:
            pcc_py_gc_minor_graph_unlock()
            free(slot_pairs)
            free(new_entries)
            break
        pair_index: int = 0
        while pair_index < pair_count:
            new_slot = load_ptr(slot_pairs, pair_index * 16 + 8)
            pcc_gc_note_slot_write_barrier(
                s, new_slot, load_ptr(new_slot, 0)
            )
            pair_index = pair_index + 1
        store_ptr(s, 40, new_entries)
        store_i64(s, 24, new_capacity)
        store_i64(s, 16, new_size)
        store_i64(s, 32, new_size)
        if retargeted == 2:
            pcc_gc_backend4_zpage_register_owner_payload_span(
                s, new_entries, new_capacity * 16
            )
        pcc_py_gc_minor_graph_unlock()
        free(old_entries)
        free(slot_pairs)
        if ptr_is_null(owner_handle) == 0:
            pcc_gc_scheduler_root_unregister_handle(owner_handle)
        return 0

    if ptr_is_null(owner_handle) == 0:
        pcc_gc_scheduler_root_unregister_handle(owner_handle)
    return -1


def _maybe_grow(s) -> int:
    capacity: int = load_i64(s, 24)
    fill: int = load_i64(s, 32)
    threshold: int = (capacity * 2) // 3
    if fill <= threshold:
        return 0
    new_cap: int = capacity
    size: int = load_i64(s, 16)
    if size > threshold // 2:
        new_cap = capacity * 2
    return _rehash(s, new_cap)


@c_abi_export("py_set_new")
def py_set_new():
    s = pcc_gc_alloc(48, PY_TYPE_SET, 0)  # sizeof(PySetObject), PY_TYPE_SET
    if ptr_is_null(s) != 0:
        return null()
    store_i64(s, 16, 0)    # size
    store_i64(s, 24, 0)    # capacity
    store_i64(s, 32, 0)    # fill
    store_ptr(s, 40, null())    # entries
    # Alloc initial entries table (capacity = 8, must be power of 2).
    entries = _alloc_entries(8)
    if ptr_is_null(entries) != 0:
        py_decref(s)
        return null()
    store_ptr(s, 40, entries)
    store_i64(s, 24, 8)
    pcc_gc_backend4_zpage_register_owner_payload_span(s, entries, 8 * 16)
    py_gc_track(s)
    pcc_gc_publish_initialized(s)
    return s


@c_abi_export("py_set_add")
def py_set_add(s, item) -> None:
    if ptr_is_null(s) != 0:
        return
    if ptr_is_null(item) != 0:
        return
    _set_lookup_rooted(s, item, 2, 0, 0)


@c_abi_export("py_set_update")
def py_set_update(dst, src) -> None:
    if ptr_is_null(dst) != 0:
        return
    if ptr_is_null(src) != 0:
        return
    backend: int = pcc_gc_backend()
    dst_slot = stack_alloc(8)
    src_slot = stack_alloc(8)
    snapshot_slot = stack_alloc(8)
    key_slot = stack_alloc(8)
    dst_handle = _set_read_prepare_root(dst_slot, dst, backend)
    if _set_read_root_failed(dst, backend, dst_handle) != 0:
        return
    src_handle = _set_read_prepare_root(src_slot, src, backend)
    if _set_read_root_failed(src, backend, src_handle) != 0:
        _set_read_finish_root(dst_handle)
        return
    src = _set_read_reload_root(src_slot, src_handle)
    if not _ptr_is_set(src):
        _set_update_iterable(_set_read_reload_root(dst_slot, dst_handle), src)
        _set_read_finish_root(src_handle)
        _set_read_finish_root(dst_handle)
        return

    source_size: int = load_i64(src, 16)
    snapshot = py_list_new(source_size if source_size > 0 else 4)
    if ptr_is_null(snapshot) != 0:
        _set_read_finish_root(src_handle)
        _set_read_finish_root(dst_handle)
        return
    snapshot_handle = _set_read_prepare_root(
        snapshot_slot, snapshot, backend
    )
    if _set_read_root_failed(snapshot, backend, snapshot_handle) != 0:
        py_decref(snapshot)
        _set_read_finish_root(src_handle)
        _set_read_finish_root(dst_handle)
        return

    src = _set_read_reload_root(src_slot, src_handle)
    source_capacity: int = load_i64(src, 24)
    dummy = global_load_ptr("py_set_dummy")
    i: int = 0
    while i < source_capacity:
        src = _set_read_reload_root(src_slot, src_handle)
        entries = load_ptr(src, 40)
        key = _entry_key(src, entries, i * 16)
        if ptr_is_null(key) == 0 and ptr_eq(key, dummy) == 0:
            snapshot = _set_read_reload_root(
                snapshot_slot, snapshot_handle
            )
            py_list_append(snapshot, key)
            if py_err_occurred() != 0:
                i = source_capacity
        i = i + 1

    snapshot = _set_read_reload_root(snapshot_slot, snapshot_handle)
    snapshot_len: int = py_list_len(snapshot)
    i = 0
    while i < snapshot_len and py_err_occurred() == 0:
        snapshot = _set_read_reload_root(snapshot_slot, snapshot_handle)
        key = py_list_get(snapshot, i)
        if ptr_is_null(key) != 0:
            i = snapshot_len
        else:
            key_handle = _set_read_prepare_root(key_slot, key, backend)
            if _set_read_root_failed(key, backend, key_handle) != 0:
                py_decref(key)
                i = snapshot_len
            else:
                dst = _set_read_reload_root(dst_slot, dst_handle)
                key = _set_read_reload_root(key_slot, key_handle)
                py_set_add(dst, key)
                key = _set_read_reload_root(key_slot, key_handle)
                _set_read_finish_root(key_handle)
                py_decref(key)
        i = i + 1

    snapshot = _set_read_reload_root(snapshot_slot, snapshot_handle)
    _set_read_finish_root(snapshot_handle)
    py_decref(snapshot)
    _set_read_finish_root(src_handle)
    _set_read_finish_root(dst_handle)


@c_abi_export("py_set_union")
def py_set_union(a, b):
    return _set_binary_owned(a, b, 0)


@c_abi_export("py_set_intersection")
def py_set_intersection(a, b):
    return _set_binary_owned(a, b, 1)


@c_abi_export("py_set_difference")
def py_set_difference(a, b):
    return _set_binary_owned(a, b, 2)


@c_abi_export("py_set_symmetric_difference")
def py_set_symmetric_difference(a, b):
    return _set_binary_owned(a, b, 6)


def _replace_contents(dst, result) -> None:
    # Drop every live key in `dst` (decref + tombstone) without freeing the
    # entries array, then re-add every live key of `result`. Preserves the
    # receiver object identity while replacing its contents.
    if not _ptr_is_set(dst):
        return
    entries = load_ptr(dst, 40)
    capacity: int = load_i64(dst, 24)
    dummy = global_load_ptr("py_set_dummy")
    i: int = 0
    while i < capacity:
        slot_off: int = i * 16
        k = _entry_key(dst, entries, slot_off)
        if ptr_is_null(k) == 0:
            if ptr_eq(k, dummy) == 0:
                py_decref(k)
                store_ptr(entries, slot_off + 8, dummy)   # tombstone
                sz: int = load_i64(dst, 16)
                store_i64(dst, 16, sz - 1)
        i = i + 1
    # py_set_update re-adds each live key of `result`.
    py_set_update(dst, result)


@c_abi_export("py_set_intersection_update")
def py_set_intersection_update(dst, other) -> None:
    result = py_set_intersection(dst, other)
    if ptr_is_null(result) != 0:
        return
    _replace_contents(dst, result)
    py_decref(result)


@c_abi_export("py_set_difference_update")
def py_set_difference_update(dst, other) -> None:
    result = py_set_difference(dst, other)
    if ptr_is_null(result) != 0:
        return
    _replace_contents(dst, result)
    py_decref(result)


@c_abi_export("py_set_symmetric_difference_update")
def py_set_symmetric_difference_update(dst, other) -> None:
    result = py_set_symmetric_difference(dst, other)
    if ptr_is_null(result) != 0:
        return
    _replace_contents(dst, result)
    py_decref(result)


def _set_predicate_hold(slots, handles, index: int, value, backend: int) -> int:
    slot = ptr_add(slots, index * 8)
    handle = _set_read_prepare_root(slot, value, backend)
    store_ptr(handles, index * 8, handle)
    if _set_read_root_failed(value, backend, handle) != 0:
        py_raise_owned(py_exc_new(19, cstr("cannot root set predicate value")))
        return 0
    return 1


def _set_predicate_load(slots, handles, index: int):
    return _set_read_reload_root(ptr_add(slots, index * 8), load_ptr(handles, index * 8))


def _set_predicate_drop(slots, handles, index: int) -> None:
    value = _set_predicate_load(slots, handles, index)
    _set_read_finish_root(load_ptr(handles, index * 8))
    store_ptr(handles, index * 8, null())
    store_ptr(slots, index * 8, null())
    if index >= 2:
        py_decref(value)


def _set_update_iterable(dst, src) -> None:
    # Borrowed destination/input, owned iterator/current item.
    slots = stack_alloc(32)
    handles = stack_alloc(32)
    memset(slots, 0, 32)
    memset(handles, 0, 32)
    backend: int = pcc_gc_backend()
    ok: int = _set_predicate_hold(slots, handles, 0, dst, backend)
    if ok != 0:
        ok = _set_predicate_hold(slots, handles, 1, src, backend)
    if ok != 0:
        iterator = py_obj_iter(_set_predicate_load(slots, handles, 1))
        ok = _set_predicate_hold(slots, handles, 2, iterator, backend)
        if ptr_is_null(iterator) != 0:
            if py_err_occurred() == 0:
                py_raise_owned(py_exc_new(3, cstr("object is not iterable")))
            ok = 0
    while ok != 0:
        item = py_obj_next(_set_predicate_load(slots, handles, 2))
        if ptr_is_null(item) != 0:
            if py_err_occurred() != 0:
                if py_exc_matches(py_current_exception(), py_exc_builtin_class(8)) != 0:
                    py_clear_exception()
            else:
                py_raise_owned(py_exc_new(7, cstr("set update iterator returned NULL without an exception")))
            break
        ok = _set_predicate_hold(slots, handles, 3, item, backend)
        if ok != 0:
            py_set_add(_set_predicate_load(slots, handles, 0),
                       _set_predicate_load(slots, handles, 3))
            if py_err_occurred() != 0:
                ok = 0
        _set_predicate_drop(slots, handles, 3)
    index: int = 3
    while index >= 0:
        _set_predicate_drop(slots, handles, index)
        index = index - 1


# Constructor input, result and pending exception keep separate actual owners.
_SET_CONSTRUCTOR_SOURCE = 0
_SET_CONSTRUCTOR_RESULT = 1
_SET_CONSTRUCTOR_ERROR = 2
_SET_CONSTRUCTOR_SLOT_COUNT = 3
_SET_CONSTRUCTOR_SLOT_BYTES = 8

define_global_i32("pcc_set_constructor_borrowed_map", -1)
define_global_i32("pcc_set_constructor_owned_map", 3)
pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
pcc_gc_root_copy_borrowed_lease = extern("pcc_gc_root_copy_borrowed_lease", (c_ptr, c_ptr), c_int64)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_take_pinned_slot = extern("pcc_gc_take_pinned_slot", (c_ptr, c_int64), c_ptr)
py_tls_exc_swap_slot = extern("py_tls_exc_swap_slot", (c_ptr,), c_void)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)


def _set_constructor_drop(slots: c_ptr, tokens: c_ptr, index: int) -> None:
    slot = ptr_add(slots, index * _SET_CONSTRUCTOR_SLOT_BYTES)
    token: int = load_i64(tokens, index * _SET_CONSTRUCTOR_SLOT_BYTES)
    if token >= 0:
        if pcc_gc_foreign_lease_release(slot, token) < 0:
            pcc_platform_abort()
            return
    store_i64(tokens, index * _SET_CONSTRUCTOR_SLOT_BYTES, -1)
    pcc_gc_store_root(slot, null())


def _set_constructor_pin(slot: c_ptr) -> int:
    pcc_py_gc_minor_graph_lock()
    value = pcc_gc_load_ptr(null(), slot)
    prior: int = 0
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        prior = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & 64
        pcc_gc_pin(value)
    pcc_py_gc_minor_graph_unlock()
    return prior


@c_abi_export("py_set_from_iterable")
def py_set_from_iterable(src):
    borrowed = stack_alloc(_SET_CONSTRUCTOR_SLOT_BYTES)
    store_ptr(borrowed, 0, src)
    pcc_gc_frame_enter(global_addr("pcc_set_constructor_borrowed_map"), borrowed)
    slots = stack_alloc(_SET_CONSTRUCTOR_SLOT_COUNT * _SET_CONSTRUCTOR_SLOT_BYTES)
    tokens = stack_alloc(_SET_CONSTRUCTOR_SLOT_COUNT * _SET_CONSTRUCTOR_SLOT_BYTES)
    memset(slots, 0, _SET_CONSTRUCTOR_SLOT_COUNT * _SET_CONSTRUCTOR_SLOT_BYTES)
    index: int = 0
    while index < _SET_CONSTRUCTOR_SLOT_COUNT:
        store_i64(tokens, index * _SET_CONSTRUCTOR_SLOT_BYTES, -1)
        index = index + 1
    pcc_gc_frame_enter(global_addr("pcc_set_constructor_owned_map"), slots)
    source_slot = ptr_add(slots, _SET_CONSTRUCTOR_SOURCE * _SET_CONSTRUCTOR_SLOT_BYTES)
    result_slot = ptr_add(slots, _SET_CONSTRUCTOR_RESULT * _SET_CONSTRUCTOR_SLOT_BYTES)
    error_slot = ptr_add(slots, _SET_CONSTRUCTOR_ERROR * _SET_CONSTRUCTOR_SLOT_BYTES)
    token: int = pcc_gc_root_copy_borrowed_lease(source_slot, borrowed)
    store_i64(tokens, _SET_CONSTRUCTOR_SOURCE * _SET_CONSTRUCTOR_SLOT_BYTES, token)
    ok: int = 1
    if token < 0:
        _set_call_error(7, cstr("cannot retain set constructor input"))
        ok = 0
    if ok != 0:
        # Empty registered owner receives the actual NEW result before any call.
        store_ptr(result_slot, 0, py_set_new())
        if ptr_is_null(load_ptr(result_slot, 0)) != 0:
            _set_call_error(19, cstr("cannot allocate set"))
            ok = 0
        else:
            token = pcc_gc_foreign_lease_acquire(result_slot)
            store_i64(tokens, _SET_CONSTRUCTOR_RESULT * _SET_CONSTRUCTOR_SLOT_BYTES, token)
            if token < 0:
                _set_call_error(7, cstr("cannot lease set constructor result"))
                ok = 0
    if ok != 0:
        py_set_update(load_ptr(result_slot, 0), load_ptr(source_slot, 0))
        if py_err_occurred() != 0:
            ok = 0
    py_tls_exc_swap_slot(error_slot)
    store_ptr(borrowed, 0, null())
    _set_constructor_drop(slots, tokens, _SET_CONSTRUCTOR_SOURCE)
    if ok == 0:
        _set_constructor_drop(slots, tokens, _SET_CONSTRUCTOR_RESULT)
    py_clear_exception()
    py_tls_exc_swap_slot(error_slot)
    # The only raw return transfer happens after every callback-capable cleanup.
    prior: int = _set_constructor_pin(result_slot)
    token = load_i64(tokens, _SET_CONSTRUCTOR_RESULT * _SET_CONSTRUCTOR_SLOT_BYTES)
    if token >= 0:
        if pcc_gc_foreign_lease_release(result_slot, token) < 0:
            pcc_platform_abort()
            return null()
    pcc_gc_frame_leave(slots)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(result_slot, prior)


def _set_predicate_iterable(a, b, superset: int) -> int:
    # Borrowed receiver/input, owned iterator/matched-set/current item.
    slots = stack_alloc(40)
    handles = stack_alloc(40)
    memset(slots, 0, 40)
    memset(handles, 0, 40)
    backend: int = pcc_gc_backend()
    ok: int = _set_predicate_hold(slots, handles, 0, a, backend)
    if ok != 0:
        ok = _set_predicate_hold(slots, handles, 1, b, backend)
    if ok != 0:
        iterator = py_obj_iter(_set_predicate_load(slots, handles, 1))
        ok = _set_predicate_hold(slots, handles, 2, iterator, backend)
        if ptr_is_null(iterator) != 0:
            ok = 0
    if ok != 0 and superset == 0:
        matched = py_set_new()
        ok = _set_predicate_hold(slots, handles, 3, matched, backend)
        if ptr_is_null(matched) != 0:
            py_raise_owned(py_exc_new(19, cstr("cannot allocate set predicate state")))
            ok = 0
    result: int = 0
    while ok != 0:
        item = py_obj_next(_set_predicate_load(slots, handles, 2))
        if ptr_is_null(item) != 0:
            if py_err_occurred() != 0:
                if py_exc_matches(py_current_exception(), py_exc_builtin_class(8)) != 0:
                    py_clear_exception()
                    if superset != 0:
                        result = 1
                    elif load_i64(_set_predicate_load(slots, handles, 3), 16) == load_i64(_set_predicate_load(slots, handles, 0), 16):
                        result = 1
            else:
                py_raise_owned(py_exc_new(7, cstr("set predicate iterator returned NULL without an exception")))
            break
        ok = _set_predicate_hold(slots, handles, 4, item, backend)
        if ok != 0:
            hash_val: int = py_obj_hash(_set_predicate_load(slots, handles, 4))
            found: int = 0
            if py_err_occurred() == 0:
                found = py_set_contains_hash(_set_predicate_load(slots, handles, 0),
                                              _set_predicate_load(slots, handles, 4), hash_val)
            if py_err_occurred() != 0:
                ok = 0
            elif superset != 0 and found == 0:
                break
            elif superset == 0 and found != 0:
                py_set_add_hash(_set_predicate_load(slots, handles, 3),
                                _set_predicate_load(slots, handles, 4), hash_val)
                if py_err_occurred() != 0:
                    ok = 0
                elif load_i64(_set_predicate_load(slots, handles, 3), 16) == load_i64(_set_predicate_load(slots, handles, 0), 16):
                    result = 1
                    break
        _set_predicate_drop(slots, handles, 4)
    index: int = 4
    while index >= 0:
        _set_predicate_drop(slots, handles, index)
        index = index - 1
    return result


@c_abi_export("py_set_issubset")
def py_set_issubset(a, b) -> int:
    if not _ptr_is_set(a):
        return 0
    if not _ptr_is_set(b):
        return _set_predicate_iterable(a, b, 0)
    size_a: int = load_i64(a, 16)
    size_b: int = load_i64(b, 16)
    if size_a > size_b:
        return 0
    entries = load_ptr(a, 40)
    capacity: int = load_i64(a, 24)
    dummy = global_load_ptr("py_set_dummy")
    i: int = 0
    while i < capacity:
        key = _entry_key(a, entries, i * 16)
        if ptr_is_null(key) == 0:
            if ptr_eq(key, dummy) == 0:
                if py_set_contains_hash(b, key, load_i64(entries, i * 16)) == 0:
                    return 0
        i = i + 1
    return 1


@c_abi_export("py_set_issuperset")
def py_set_issuperset(a, b) -> int:
    if not _ptr_is_set(a):
        return 0
    if not _ptr_is_set(b):
        return _set_predicate_iterable(a, b, 1)
    return py_set_issubset(b, a)


@c_abi_export("py_set_items")
def py_set_items(s):
    if not _ptr_is_set(s):
        return null()
    size: int = load_i64(s, 16)
    cap_hint: int = size
    if cap_hint <= 0:
        cap_hint = 4
    out = py_list_new(cap_hint)
    if ptr_is_null(out) != 0:
        return null()
    entries = load_ptr(s, 40)
    capacity: int = load_i64(s, 24)
    dummy = global_load_ptr("py_set_dummy")
    i: int = 0
    while i < capacity:
        key = _entry_key(s, entries, i * 16)
        if ptr_is_null(key) == 0:
            if ptr_eq(key, dummy) == 0:
                py_list_append(out, key)
        i = i + 1
    return out


@c_abi_export("py_set_clear")
def py_set_clear(s) -> None:
    # set.clear(): tombstone and release every live key in place, keeping the
    # receiver and its entries array (the first half of _replace_contents).
    # A key's finalizer may add to the set and rehash it, so the table is
    # re-read after every release.
    if not _ptr_is_set(s):
        return
    dummy = global_load_ptr("py_set_dummy")
    i: int = 0
    while i < load_i64(s, 24):
        entries = load_ptr(s, 40)
        slot_off: int = i * 16
        key = _entry_key(s, entries, slot_off)
        if ptr_is_null(key) == 0 and ptr_eq(key, dummy) == 0:
            store_ptr(entries, slot_off + 8, dummy)
            store_i64(s, 16, load_i64(s, 16) - 1)
            py_decref(key)
        i = i + 1


@c_abi_export("py_set_pop")
def py_set_pop(s):
    if not _ptr_is_set(s):
        return null()
    size: int = load_i64(s, 16)
    if size <= 0:
        py_raise_owned(py_exc_new(4, cstr("pop from an empty set")))
        return null()
    entries = load_ptr(s, 40)
    capacity: int = load_i64(s, 24)
    dummy = global_load_ptr("py_set_dummy")
    i: int = 0
    while i < capacity:
        slot_off: int = i * 16
        key = _entry_key(s, entries, slot_off)
        if ptr_is_null(key) == 0:
            if ptr_eq(key, dummy) == 0:
                store_ptr(entries, slot_off + 8, dummy)
                store_i64(s, 16, size - 1)
                return key
        i = i + 1
    py_raise_owned(py_exc_new(4, cstr("pop from an empty set")))
    return null()


@c_abi_export("py_set_contains")
def py_set_contains(s, item) -> int:
    if ptr_is_null(s) != 0:
        return 0
    if ptr_is_null(item) != 0:
        return 0
    return _set_lookup_rooted(s, item, 0, 0, 0)


@c_abi_export("py_set_contains_hash")
def py_set_contains_hash(s, item, hash_val: int) -> int:
    return _set_lookup_rooted(s, item, 0, hash_val, 1)


@c_abi_export("py_set_add_hash")
def py_set_add_hash(s, item, hash_val: int) -> None:
    _set_lookup_rooted(s, item, 2, hash_val, 1)


@c_abi_export("py_set_remove")
def py_set_remove(s, item) -> int:
    if ptr_is_null(s) != 0:
        return -1
    if ptr_is_null(item) != 0:
        return -1
    if _set_lookup_rooted(s, item, 1, 0, 0) != 0:
        return 0
    return -1


@c_abi_export("py_set_len")
def py_set_len(s) -> int:
    if ptr_is_null(s) != 0:
        return 0
    return load_i64(s, 16)


# Slot-based algebra-call binder. These IDs are shared with set_lowering.py:
# union/intersection/difference/update/intersection_update/difference_update/
# symmetric_difference/symmetric_difference_update = 0..7.
# Every internal slot is registered EMPTY, then receives an owning reference.
# Tokens are counted address leases, never legacy Boolean pin flags.
def _set_call_error(kind: int, message) -> None:
    if py_err_occurred() == 0:
        py_raise_owned(py_exc_new(kind, message))


def _set_call_load(slots, index: int):
    return pcc_gc_load_ptr(null(), ptr_add(slots, index * 8))


def _set_call_lease(slots, tokens, index: int) -> int:
    token: int = pcc_gc_foreign_lease_acquire(ptr_add(slots, index * 8))
    store_i64(tokens, index * 8, token)
    if token < 0:
        _set_call_error(15 if token == -2 else 7, cstr("cannot lease set call operand"))
        return 0
    return 1


def _set_call_copy(slots, tokens, index: int, source_slot) -> int:
    token: int = pcc_gc_root_copy_lease(ptr_add(slots, index * 8), source_slot)
    store_i64(tokens, index * 8, token)
    if token < 0:
        _set_call_error(15 if token == -2 else 7, cstr("cannot retain set call operand"))
        return 0
    return 1


def _set_call_copy_prepare(slots, tokens, index: int, source_slot, plan) -> int:
    # Selection from a mutable payload and retention are one graph transaction.
    # Finish (including diagnostics/finalizers) must happen after outer unlock.
    token: int = pcc_gc_root_copy_lease_prepare_locked(
        ptr_add(slots, index * 8), source_slot, 0, plan)
    store_i64(tokens, index * 8, token)
    return token


def _set_call_copy_finish(plan, token: int) -> int:
    pcc_gc_root_copy_lease_finish(plan)
    if token < 0:
        _set_call_error(15 if token == -2 else 7, cstr("cannot retain set call operand"))
        return 0
    return 1


def _set_call_drop(slots, tokens, index: int) -> None:
    slot = ptr_add(slots, index * 8)
    token: int = load_i64(tokens, index * 8)
    if token >= 0:
        status: int = pcc_gc_foreign_lease_release(slot, token)
        if status < 0:
            _set_call_error(7, cstr("set call address lease cleanup failed"))
    store_i64(tokens, index * 8, -1)
    pcc_gc_store_root(slot, null())


def _set_call_new(slots, tokens, index: int) -> int:
    # No helper, safepoint or callback between the owned return and its store.
    store_ptr(slots, index * 8, py_set_new())
    if ptr_is_null(load_ptr(slots, index * 8)) != 0:
        _set_call_error(19, cstr("cannot allocate set call result"))
        return 0
    return _set_call_lease(slots, tokens, index)


def _set_call_move(slots, tokens, destination: int, source: int) -> int:
    status: int = pcc_gc_root_move(ptr_add(slots, destination * 8),
                                  ptr_add(slots, source * 8))
    if status < 0:
        _set_call_error(7, cstr("cannot transfer set call result"))
        return 0
    store_i64(tokens, destination * 8, load_i64(tokens, source * 8))
    store_i64(tokens, source * 8, -1)
    return 1


def _set_call_insert_prepare(slots, target: int, entries, capacity: int,
                             position: int, hash_value: int, plan) -> int:
    # Lookup still owns the graph lock. Releasing it before insertion would
    # let a concurrent equal key occupy an earlier newly created tombstone,
    # even while this selected slot remains empty in the same table.
    s = _set_call_load(slots, target)
    pcc_gc_store_ptr_plan_init(plan, s, pcc_gc_backend())
    if ptr_eq(load_ptr(s, 40), entries) == 0 or load_i64(s, 24) != capacity:
        return 0
    key_slot = ptr_add(entries, position * 16 + 8)
    old = load_ptr(key_slot, 0)
    dummy = global_load_ptr("py_set_dummy")
    was_empty: int = ptr_is_null(old)
    if was_empty == 0 and ptr_eq(old, dummy) == 0:
        return 0
    committed: int = pcc_gc_store_ptr_plan_commit_sentinel_aware_locked(
        plan, s, key_slot, _set_call_load(slots, 7), dummy)
    if committed != 0:
        store_i64(entries, position * 16, hash_value)
        store_i64(s, 16, load_i64(s, 16) + 1)
        if was_empty != 0:
            store_i64(s, 32, load_i64(s, 32) + 1)
    return committed


def _set_call_insert_finish(slots, target: int, plan, committed: int) -> None:
    # Deferred diagnostics/refcounts and growth happen after outermost unlock.
    # The caller's counted target lease survives both operations.
    pcc_gc_store_ptr_plan_finish(plan)
    if committed != 0:
        _maybe_grow(_set_call_load(slots, target))
    else:
        _set_call_error(7, cstr("cannot commit set call insertion"))


def _set_call_lookup(slots, tokens, target: int, hash_value: int, mode: int) -> int:
    # The target and item (slot 7) already have counted leases. Candidate
    # equality may mutate/rehash the target; revalidate before any commit.
    # No raw table/key pointer survives an unlocked callback unprotected.
    candidate_plan = stack_alloc(256)
    insertion_plan = stack_alloc(128)
    while py_err_occurred() == 0:
        pcc_py_gc_minor_graph_lock()
        s = _set_call_load(slots, target)
        item = _set_call_load(slots, 7)
        entries = load_ptr(s, 40)
        capacity: int = load_i64(s, 24)
        mask: int = capacity - 1
        perturb: int = hash_value
        position: int = hash_value & mask
        tombstone: int = -1
        probes: int = 0
        restart: int = 0
        dummy = global_load_ptr("py_set_dummy")
        while probes < capacity + 16:
            # Peeking for sentinels/hash does not heal an owning entry through
            # the borrowed load barrier. Retain/lease below resolves ownership.
            key = load_ptr(entries, position * 16 + 8)
            if ptr_is_null(key) != 0:
                if tombstone >= 0:
                    position = tombstone
                if mode == 2:
                    committed: int = _set_call_insert_prepare(slots, target, entries,
                        capacity, position, hash_value, insertion_plan)
                    pcc_py_gc_minor_graph_unlock()
                    _set_call_insert_finish(slots, target, insertion_plan, committed)
                    if committed == 0:
                        restart = 1
                        break
                else:
                    pcc_py_gc_minor_graph_unlock()
                return 0
            if ptr_eq(key, dummy) != 0:
                if tombstone < 0:
                    tombstone = position
            elif load_i64(entries, position * 16) == hash_value:
                equal: int = ptr_eq(key, item)
                if not (is_tagged_int(key) != 0 and is_tagged_int(item) != 0):
                    token: int = _set_call_copy_prepare(slots, tokens, 9,
                        ptr_add(entries, position * 16 + 8), candidate_plan)
                    pcc_py_gc_minor_graph_unlock()
                    copied: int = _set_call_copy_finish(candidate_plan, token)
                    if copied == 0:
                        return 0
                    equal = ptr_eq(_set_call_load(slots, 9), _set_call_load(slots, 7))
                    if equal == 0:
                        equal = py_obj_eq(_set_call_load(slots, 9), _set_call_load(slots, 7))
                    pcc_py_gc_minor_graph_lock()
                    s = _set_call_load(slots, target)
                    stable: int = 0
                    if ptr_eq(load_ptr(s, 40), entries) != 0 and load_i64(s, 24) == capacity:
                        if (load_i64(entries, position * 16) == hash_value
                            and ptr_eq(load_ptr(entries, position * 16 + 8), _set_call_load(slots, 9)) != 0):
                            stable = 1
                    pcc_py_gc_minor_graph_unlock()
                    if stable != 0 and equal != 0 and py_err_occurred() == 0:
                        if mode == 1:
                            stable = _set_remove_rooted_slot(ptr_add(slots, target * 8),
                                null(), entries, capacity, position, ptr_add(slots, 72), hash_value)
                        _set_call_drop(slots, tokens, 9)
                        if stable != 0:
                            return 1
                        restart = 1
                        break
                    _set_call_drop(slots, tokens, 9)
                    if py_err_occurred() != 0:
                        return 0
                    # Candidate teardown itself may run a finalizer. Restart
                    # even on inequality; remember its proven slot only while
                    # another graph lease revalidates the same table entry.
                    pcc_py_gc_minor_graph_lock()
                    s = _set_call_load(slots, target)
                    if stable == 0 or ptr_eq(load_ptr(s, 40), entries) == 0 or load_i64(s, 24) != capacity:
                        pcc_py_gc_minor_graph_unlock()
                        restart = 1
                        break
                elif equal != 0:
                    pcc_py_gc_minor_graph_unlock()
                    if mode == 1:
                        if _set_remove_rooted_slot(ptr_add(slots, target * 8), null(),
                            entries, capacity, position, ptr_add(slots, 56), hash_value) == 0:
                            restart = 1
                            break
                    return 1
            perturb = _perturb_shift5(perturb)
            position = (position * 5 + perturb + 1) & mask
            probes = probes + 1
        if restart == 0:
            if tombstone >= 0 and mode == 2:
                committed = _set_call_insert_prepare(slots, target, entries,
                    capacity, tombstone, hash_value, insertion_plan)
                pcc_py_gc_minor_graph_unlock()
                _set_call_insert_finish(slots, target, insertion_plan, committed)
                if committed != 0:
                    return 0
            else:
                pcc_py_gc_minor_graph_unlock()
                _set_call_error(7, cstr("set call lookup exhausted its table"))
                return 0
    return 0


def _set_call_replace(slots, target: int, source: int) -> int:
    # Atomic visible replacement, including finalizer reentrancy. Keep the
    # target payload allocation; use existing slot plans to defer all old-key
    # decrefs until the entire new table/size is visible. This preserves GC4
    # remembered-slot identity without swapping two owners' payload spans.
    target_set = _set_call_load(slots, target)
    source_set = _set_call_load(slots, source)
    if ptr_eq(target_set, source_set) != 0:
        return 1
    needed: int = load_i64(source_set, 24)
    if load_i64(target_set, 24) < needed:
        if _rehash(target_set, needed) != 0:
            _set_call_error(19, cstr("cannot grow set call replacement"))
            return 0
    capacity: int = load_i64(target_set, 24)
    if capacity > 72057594037927935:
        _set_call_error(19, cstr("set call replacement is too large"))
        return 0
    replacement = _alloc_entries(capacity)
    plans = malloc(capacity * 128)
    if ptr_is_null(replacement) != 0 or ptr_is_null(plans) != 0:
        free(replacement)
        free(plans)
        _set_call_error(19, cstr("cannot allocate set call replacement"))
        return 0
    index: int = 0
    while index < capacity:
        pcc_gc_store_ptr_plan_init(ptr_add(plans, index * 128), target_set, pcc_gc_backend())
        index = index + 1
    pcc_py_gc_minor_graph_lock()
    target_set = _set_call_load(slots, target)
    source_set = _set_call_load(slots, source)
    ok: int = 1
    if load_i64(target_set, 24) != capacity or load_i64(source_set, 16) >= capacity:
        ok = 0
    dummy = global_load_ptr("py_set_dummy")
    index = 0
    source_capacity: int = load_i64(source_set, 24)
    source_entries = load_ptr(source_set, 40)
    while index < source_capacity and ok != 0:
        key_slot = ptr_add(source_entries, index * 16 + 8)
        key = load_ptr(key_slot, 0)
        if ptr_is_null(key) == 0 and ptr_eq(key, dummy) == 0:
            # Source owns the key. Transfer forwarding references before
            # staging a borrowed copy inside this same graph transaction.
            key = pcc_gc_resolve_root_slot_unlocked(key_slot, 0)
            hash_value: int = load_i64(source_entries, index * 16)
            position: int = _rehash_find_empty_slot(replacement, capacity, hash_value)
            if position < 0:
                ok = 0
            else:
                store_ptr(replacement, position * 16 + 8, key)
                store_i64(replacement, position * 16, hash_value)
        index = index + 1
    entries = load_ptr(target_set, 40)
    index = 0
    if ok != 0:
        while index < capacity:
            key = load_ptr(replacement, index * 16 + 8)
            committed: int = pcc_gc_store_ptr_plan_commit_sentinel_aware_locked(
                ptr_add(plans, index * 128), target_set,
                ptr_add(entries, index * 16 + 8), key, dummy)
            if committed == 0:
                ok = 0
            store_i64(entries, index * 16, load_i64(replacement, index * 16))
            index = index + 1
        store_i64(target_set, 16, load_i64(source_set, 16))
        store_i64(target_set, 32, load_i64(source_set, 16))
    pcc_py_gc_minor_graph_unlock()
    index = 0
    while index < capacity:
        pcc_gc_store_ptr_plan_finish(ptr_add(plans, index * 128))
        index = index + 1
    free(plans)
    free(replacement)
    if ok == 0:
        _set_call_error(7, cstr("set changed during replacement"))
    return ok


def _set_call_apply(slots, tokens, target: int, source: int, mode: int, match: int = 3) -> int:
    # Modes: add, discard, intersection into target (against match), xor,
    # difference into target (against match), full-scan binary intersection.
    # Exact sets carry cached hashes. Other iterables invoke __hash__ once
    # for each delivered item, and preserve errors/partial mutation.
    is_set: int = 1 if _ptr_is_set(_set_call_load(slots, source)) else 0
    if is_set != 0 and ptr_eq(_set_call_load(slots, target), _set_call_load(slots, source)) != 0:
        if mode == 0:
            return 1
        if mode == 1 or mode == 3:
            if _set_call_new(slots, tokens, 5) == 0:
                return 0
            ok: int = _set_call_replace(slots, target, 5)
            _set_call_drop(slots, tokens, 5)
            return ok
    if is_set == 0:
        store_ptr(slots, 48, py_obj_iter(_set_call_load(slots, source)))
        if ptr_is_null(load_ptr(slots, 48)) != 0:
            _set_call_error(3, cstr("object is not iterable"))
            return 0
        if _set_call_lease(slots, tokens, 6) == 0:
            return 0
    item_plan = stack_alloc(256)
    index: int = 0
    while py_err_occurred() == 0:
        hash_value: int = 0
        matched: int = 0
        if is_set != 0:
            pcc_py_gc_minor_graph_lock()
            other = _set_call_load(slots, source)
            capacity: int = load_i64(other, 24)
            entries = load_ptr(other, 40)
            selected: int = 0
            token: int = -1
            while index < capacity:
                position: int = index
                index = index + 1
                key = load_ptr(entries, position * 16 + 8)
                if ptr_is_null(key) == 0 and ptr_eq(key, global_load_ptr("py_set_dummy")) == 0:
                    hash_value = load_i64(entries, position * 16)
                    selected = 1
                    token = _set_call_copy_prepare(slots, tokens, 7,
                        ptr_add(entries, position * 16 + 8), item_plan)
                    break
            pcc_py_gc_minor_graph_unlock()
            if selected == 0:
                break
            if _set_call_copy_finish(item_plan, token) == 0:
                break
        else:
            store_ptr(slots, 56, py_obj_next(_set_call_load(slots, 6)))
            if ptr_is_null(load_ptr(slots, 56)) != 0:
                if py_err_occurred() != 0:
                    if py_exc_matches(py_current_exception(), py_exc_builtin_class(8)) != 0:
                        py_clear_exception()
                else:
                    _set_call_error(7, cstr("set iterator returned NULL without an exception"))
                break
            if _set_call_lease(slots, tokens, 7) == 0:
                break
            hash_value = py_obj_hash(_set_call_load(slots, 7))
        if py_err_occurred() == 0:
            if mode == 0:
                _set_call_lookup(slots, tokens, target, hash_value, 2)
            elif mode == 1:
                _set_call_lookup(slots, tokens, target, hash_value, 1)
            elif mode == 2 or mode == 4 or mode == 5:
                found: int = _set_call_lookup(slots, tokens, match, hash_value, 0)
                selected_match: int = 1 - found if mode == 4 else found
                if selected_match != 0 and py_err_occurred() == 0:
                    _set_call_lookup(slots, tokens, target, hash_value, 2)
                    matched = 1
            else:
                found = _set_call_lookup(slots, tokens, target, hash_value, 1)
                if found == 0 and py_err_occurred() == 0:
                    _set_call_lookup(slots, tokens, target, hash_value, 2)
        _set_call_drop(slots, tokens, 7)
        if mode == 2 and matched != 0 and py_err_occurred() == 0:
            if load_i64(_set_call_load(slots, target), 16) == load_i64(_set_call_load(slots, match), 16):
                break
    _set_call_drop(slots, tokens, 7)
    _set_call_drop(slots, tokens, 6)
    return 1 if py_err_occurred() == 0 else 0


def _set_call_algebra(slots, tokens, method: int) -> int:
    kwargs = _set_call_load(slots, 2)
    if ptr_is_null(kwargs) == 0 and ptr_eq(kwargs, global_load_ptr("py_None")) == 0:
        if py_dict_len(kwargs) != 0:
            _set_call_error(3, cstr("set methods take no keyword arguments"))
            return 0
    args = _set_call_load(slots, 1)
    count: int = load_i64(args, PYTUPLEOBJECT_LEN_OFFSET)
    if method < 0 or method > 7:
        _set_call_error(7, cstr("unknown set algebra method"))
        return 0
    if method >= 6 and count != 1:
        _set_call_error(3, cstr("set symmetric difference takes exactly one argument"))
        return 0
    if not _ptr_is_set(_set_call_load(slots, 0)):
        _set_call_error(3, cstr("set method requires a set receiver"))
        return 0
    if method == 3 or method == 5 or method == 7:
        if _set_call_copy(slots, tokens, 3, slots) == 0:
            return 0
    else:
        if _set_call_new(slots, tokens, 3) == 0:
            return 0
        if _set_call_replace(slots, 3, 0) == 0:
            return 0
    index: int = 0
    while index < count and py_err_occurred() == 0:
        # The argument tuple is leased and immutable, so its element slot is
        # stable while root_copy_lease acquires its own graph transaction.
        args = _set_call_load(slots, 1)
        copied: int = _set_call_copy(slots, tokens, 4,
                                    ptr_add(args, PYTUPLEOBJECT_ITEMS_OFFSET + index * 8))
        if copied == 0:
            return 0
        if method == 0 or method == 3:
            _set_call_apply(slots, tokens, 3, 4, 0)
        elif method == 1 or method == 4:
            if _set_call_new(slots, tokens, 5) == 0:
                return 0
            if (_ptr_is_set(_set_call_load(slots, 4))
                    and load_i64(_set_call_load(slots, 4), 16) > load_i64(_set_call_load(slots, 3), 16)):
                _set_call_apply(slots, tokens, 5, 3, 2, 4)
            else:
                _set_call_apply(slots, tokens, 5, 4, 2)
            if py_err_occurred() == 0:
                _set_call_drop(slots, tokens, 3)
                _set_call_move(slots, tokens, 3, 5)
        elif method == 2 or method == 5:
            _set_call_apply(slots, tokens, 3, 4, 1)
        else:
            # Repeated iterable items are deduplicated BEFORE toggling; an
            # iterator/hash failure here must leave the receiver unchanged.
            if _set_call_new(slots, tokens, 8) == 0:
                return 0
            if _ptr_is_set(_set_call_load(slots, 4)):
                _set_call_replace(slots, 8, 4)
            else:
                _set_call_apply(slots, tokens, 8, 4, 0)
            if py_err_occurred() == 0:
                if ptr_eq(_set_call_load(slots, 3), _set_call_load(slots, 4)) != 0:
                    _set_call_apply(slots, tokens, 3, 4, 3)
                else:
                    _set_call_apply(slots, tokens, 3, 8, 3)
            _set_call_drop(slots, tokens, 8)
        _set_call_drop(slots, tokens, 4)
        index = index + 1
    if py_err_occurred() != 0:
        return 0
    if method == 4:
        if _set_call_replace(slots, 0, 3) == 0:
            return 0
    return 1


@c_abi_export("py_set_call_method_slots")
def py_set_call_method_slots(receiver_slot, method: int, args_slot, kwargs_slot, result_slot) -> int:
    """Bind a set algebra call from authoritative caller slots.

    Caller owns/roots all inputs and an EMPTY output for the entire call.
    The result reference is transferred into that output before cleanup.
    Returns 0 on success, -1 on failure (and leaves output empty on failure).
    """
    slots = stack_alloc(80)
    handles = stack_alloc(80)
    tokens = stack_alloc(80)
    memset(slots, 0, 80)
    memset(handles, 0, 80)
    index: int = 0
    while index < 10:
        store_i64(tokens, index * 8, -1)
        index = index + 1
    index = 0
    ok: int = 1
    while index < 10:
        handle = pcc_gc_scheduler_root_register_handle(ptr_add(slots, index * 8))
        store_ptr(handles, index * 8, handle)
        if ptr_is_null(handle) != 0:
            _set_call_error(19, cstr("cannot register set call roots"))
            ok = 0
            break
        index = index + 1
    if ok != 0:
        ok = _set_call_copy(slots, tokens, 0, receiver_slot)
    if ok != 0:
        ok = _set_call_copy(slots, tokens, 1, args_slot)
    if ok != 0:
        ok = _set_call_copy(slots, tokens, 2, kwargs_slot)
    if ok != 0:
        ok = _set_call_algebra(slots, tokens, method)
    if ok != 0 and py_err_occurred() == 0:
        if method == 0 or method == 1 or method == 2 or method == 6:
            token: int = load_i64(tokens, 24)
            if pcc_gc_root_move(result_slot, ptr_add(slots, 24)) < 0:
                _set_call_error(7, cstr("cannot publish set call result"))
            else:
                store_i64(tokens, 24, -1)
                if pcc_gc_foreign_lease_release(result_slot, token) < 0:
                    _set_call_error(7, cstr("set result address lease cleanup failed"))
        else:
            # The immortal None object needs no lease/refcount increment.
            store_ptr(result_slot, 0, global_load_ptr("py_None"))
    index = 9
    while index >= 0:
        _set_call_drop(slots, tokens, index)
        handle = load_ptr(handles, index * 8)
        if ptr_is_null(handle) == 0:
            pcc_gc_scheduler_root_unregister_handle(handle)
        index = index - 1
    if py_err_occurred() != 0:
        pcc_gc_store_root(result_slot, null())
        return -1
    return 0


# Reuse the algebra core's first ten slots. The pending exception has its own
# extra owner so candidate-key cleanup cannot overwrite the saved exception.
_SET_BINARY_LEFT = 0
_SET_BINARY_RIGHT = 4
_SET_BINARY_RESULT = 3
_SET_BINARY_ERROR = 10
_SET_BINARY_SLOT_COUNT = 11
_SET_BINARY_INCOMING_COUNT = 2
_SET_BINARY_SLOT_BYTES = 8

define_global_i32("pcc_set_binary_borrowed_map", -2)
define_global_i32("pcc_set_binary_owned_map", 11)


def _set_binary_owned(a, b, operation: int):
    borrowed = stack_alloc(_SET_BINARY_INCOMING_COUNT * _SET_BINARY_SLOT_BYTES)
    store_ptr(borrowed, 0, a)
    store_ptr(borrowed, _SET_BINARY_SLOT_BYTES, b)
    pcc_gc_frame_enter(global_addr("pcc_set_binary_borrowed_map"), borrowed)
    slots = stack_alloc(_SET_BINARY_SLOT_COUNT * _SET_BINARY_SLOT_BYTES)
    tokens = stack_alloc(_SET_BINARY_SLOT_COUNT * _SET_BINARY_SLOT_BYTES)
    memset(slots, 0, _SET_BINARY_SLOT_COUNT * _SET_BINARY_SLOT_BYTES)
    index: int = 0
    while index < _SET_BINARY_SLOT_COUNT:
        store_i64(tokens, index * _SET_BINARY_SLOT_BYTES, -1)
        index = index + 1
    pcc_gc_frame_enter(global_addr("pcc_set_binary_owned_map"), slots)
    left_slot = ptr_add(slots, _SET_BINARY_LEFT * _SET_BINARY_SLOT_BYTES)
    right_slot = ptr_add(slots, _SET_BINARY_RIGHT * _SET_BINARY_SLOT_BYTES)
    result_slot = ptr_add(slots, _SET_BINARY_RESULT * _SET_BINARY_SLOT_BYTES)
    error_slot = ptr_add(slots, _SET_BINARY_ERROR * _SET_BINARY_SLOT_BYTES)
    token: int = pcc_gc_root_copy_borrowed_lease(left_slot, borrowed)
    store_i64(tokens, _SET_BINARY_LEFT * _SET_BINARY_SLOT_BYTES, token)
    ok: int = 1
    if token < 0:
        _set_call_error(7, cstr("cannot retain left set operand"))
        ok = 0
    if ok != 0:
        token = pcc_gc_root_copy_borrowed_lease(right_slot,
            ptr_add(borrowed, _SET_BINARY_SLOT_BYTES))
        store_i64(tokens, _SET_BINARY_RIGHT * _SET_BINARY_SLOT_BYTES, token)
        if token < 0:
            _set_call_error(7, cstr("cannot retain right set operand"))
            ok = 0
    # IDs match the slot-based algebra binder. Each operation keeps the
    # original operand owners; callback mutation must not swap source aliases.
    if ok != 0 and operation != 0 and operation != 1 and operation != 2 and operation != 6:
        _set_call_error(7, cstr("unknown binary set operation"))
        ok = 0
    if ok != 0:
        ok = _set_call_new(slots, tokens, _SET_BINARY_RESULT)
    if ok != 0 and operation == 0:
        # The dispatcher admits two exact sets. Keep update order and retain
        # both original source owners across every insertion callback.
        if not _ptr_is_set(_set_call_load(slots, _SET_BINARY_LEFT)) or not _ptr_is_set(_set_call_load(slots, _SET_BINARY_RIGHT)):
            _set_call_error(3, cstr("set union requires set operands"))
            ok = 0
        else:
            ok = _set_call_apply(slots, tokens, _SET_BINARY_RESULT,
                                 _SET_BINARY_LEFT, 0)
            if ok != 0:
                ok = _set_call_apply(slots, tokens, _SET_BINARY_RESULT,
                                     _SET_BINARY_RIGHT, 0)
    elif ok != 0 and _ptr_is_set(_set_call_load(slots, _SET_BINARY_LEFT)):
        if _ptr_is_set(_set_call_load(slots, _SET_BINARY_RIGHT)):
            # Preserve left iteration/right lookup and the left key identity.
            # Binary intersection visits the whole left table, even after the
            # result reaches the right set's size. XOR then scans the right
            # against the original left, rather than toggling a copied result.
            mode: int = 5 if operation == 1 else 4
            ok = _set_call_apply(slots, tokens, _SET_BINARY_RESULT,
                                 _SET_BINARY_LEFT, mode, _SET_BINARY_RIGHT)
        elif operation != 1:
            ok = _set_call_replace(slots, _SET_BINARY_RESULT, _SET_BINARY_LEFT)
    if ok != 0 and operation == 6 and _ptr_is_set(_set_call_load(slots, _SET_BINARY_RIGHT)):
        if _ptr_is_set(_set_call_load(slots, _SET_BINARY_LEFT)):
            ok = _set_call_apply(slots, tokens, _SET_BINARY_RESULT,
                                 _SET_BINARY_RIGHT, 4, _SET_BINARY_LEFT)
        else:
            ok = _set_call_replace(slots, _SET_BINARY_RESULT, _SET_BINARY_RIGHT)
    if py_err_occurred() != 0:
        ok = 0
    py_tls_exc_swap_slot(error_slot)
    store_ptr(borrowed, 0, null())
    store_ptr(borrowed, _SET_BINARY_SLOT_BYTES, null())
    index = _SET_BINARY_ERROR - 1
    while index >= 0:
        if index != _SET_BINARY_RESULT:
            _set_constructor_drop(slots, tokens, index)
        index = index - 1
    if ok == 0:
        _set_constructor_drop(slots, tokens, _SET_BINARY_RESULT)
    py_clear_exception()
    py_tls_exc_swap_slot(error_slot)
    prior: int = _set_constructor_pin(result_slot)
    token = load_i64(tokens, _SET_BINARY_RESULT * _SET_BINARY_SLOT_BYTES)
    if token >= 0:
        if pcc_gc_foreign_lease_release(result_slot, token) < 0:
            pcc_platform_abort()
            return null()
    pcc_gc_frame_leave(slots)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(result_slot, prior)
