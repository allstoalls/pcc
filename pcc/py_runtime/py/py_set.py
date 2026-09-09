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

from pcc.py_runtime.py.py_abi_constants import (
    PYOBJECTHEADER_TYPE_TAG_OFFSET,
    PY_TYPE_SET,
)
from pcc.extern import extern, c_abi_export, c_ptr, c_int32, c_int64, c_void
from pcc.unsafe import (
    cstr,
    free,
    global_load_ptr,
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
            key = _entry_key(s, entries, slot_off)
            dummy = global_load_ptr("py_set_dummy")
            if ptr_is_null(key) == 0 and ptr_eq(key, dummy) == 0:
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
            old = _entry_key(s, entries, slot_off)
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


def _set_lookup_rooted(s, item, mode: int) -> int:
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
    hash_val: int = py_obj_hash(item)
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
    _set_lookup_rooted(s, item, 2)


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


@c_abi_export("py_set_intersection")
def py_set_intersection(a, b):
    out = py_set_new()
    if ptr_is_null(out) != 0:
        return null()
    if not _ptr_is_set(a):
        return out
    if not _ptr_is_set(b):
        return out
    entries = load_ptr(a, 40)
    capacity: int = load_i64(a, 24)
    dummy = global_load_ptr("py_set_dummy")
    i: int = 0
    while i < capacity:
        key = _entry_key(a, entries, i * 16)
        if ptr_is_null(key) == 0:
            if ptr_eq(key, dummy) == 0:
                if py_set_contains(b, key) != 0:
                    py_set_add(out, key)
        i = i + 1
    return out


@c_abi_export("py_set_difference")
def py_set_difference(a, b):
    out = py_set_new()
    if ptr_is_null(out) != 0:
        return null()
    if not _ptr_is_set(a):
        return out
    b_is_set: bool = _ptr_is_set(b)
    entries = load_ptr(a, 40)
    capacity: int = load_i64(a, 24)
    dummy = global_load_ptr("py_set_dummy")
    i: int = 0
    while i < capacity:
        key = _entry_key(a, entries, i * 16)
        if ptr_is_null(key) == 0:
            if ptr_eq(key, dummy) == 0:
                if not b_is_set:
                    py_set_add(out, key)
                elif py_set_contains(b, key) == 0:
                    py_set_add(out, key)
        i = i + 1
    return out


@c_abi_export("py_set_symmetric_difference")
def py_set_symmetric_difference(a, b):
    # a ^ b = (a - b) | (b - a); mirrors py_set.c::py_set_symmetric_difference.
    out = py_set_new()
    if ptr_is_null(out) != 0:
        return null()
    a_is_set: bool = _ptr_is_set(a)
    b_is_set: bool = _ptr_is_set(b)
    dummy = global_load_ptr("py_set_dummy")
    if a_is_set:
        entries_a = load_ptr(a, 40)
        capacity_a: int = load_i64(a, 24)
        i: int = 0
        while i < capacity_a:
            key = _entry_key(a, entries_a, i * 16)
            if ptr_is_null(key) == 0:
                if ptr_eq(key, dummy) == 0:
                    if not b_is_set:
                        py_set_add(out, key)
                    elif py_set_contains(b, key) == 0:
                        py_set_add(out, key)
            i = i + 1
    if b_is_set:
        entries_b = load_ptr(b, 40)
        capacity_b: int = load_i64(b, 24)
        j: int = 0
        while j < capacity_b:
            key2 = _entry_key(b, entries_b, j * 16)
            if ptr_is_null(key2) == 0:
                if ptr_eq(key2, dummy) == 0:
                    if not a_is_set:
                        py_set_add(out, key2)
                    elif py_set_contains(a, key2) == 0:
                        py_set_add(out, key2)
            j = j + 1
    return out


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


@c_abi_export("py_set_issubset")
def py_set_issubset(a, b) -> int:
    if not _ptr_is_set(a):
        return 0
    if not _ptr_is_set(b):
        return 0
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
                if py_set_contains(b, key) == 0:
                    return 0
        i = i + 1
    return 1


@c_abi_export("py_set_issuperset")
def py_set_issuperset(a, b) -> int:
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
    return _set_lookup_rooted(s, item, 0)


@c_abi_export("py_set_remove")
def py_set_remove(s, item) -> int:
    if ptr_is_null(s) != 0:
        return -1
    if ptr_is_null(item) != 0:
        return -1
    if _set_lookup_rooted(s, item, 1) != 0:
        return 0
    return -1


@c_abi_export("py_set_len")
def py_set_len(s) -> int:
    if ptr_is_null(s) != 0:
        return 0
    return load_i64(s, 16)
