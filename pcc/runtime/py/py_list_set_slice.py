"""List set-slice support split out from py_list.

The common list object member is pulled into most executables for
``py_list_new``/``py_list_get``/``py_list_append``. Keep set-slice in its own
archive member so that ordinary list users do not pay for it.
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PY_TYPE_INT, PY_TYPE_LIST, PY_TYPE_TUPLE

from pcc.extern import c_abi_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    cstr,
    free,
    global_load_ptr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    memset,
    memmove,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    ptr_to_int,
    stack_alloc,
    store_i64,
    store_ptr,
    untag_int,
)


py_decref = extern("py_decref", (c_ptr,), c_void)
py_raise_zero_slice_step = extern("py_raise_zero_slice_step", (), c_void)
py_int_value_i64 = extern("py_int_value_i64", (c_ptr,), c_int64)
py_obj_index_i64 = extern("py_obj_index_i64", (c_ptr,), c_int64)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_list_new = extern("py_list_new", (c_int64,), c_ptr)
py_list_copy = extern("py_list_copy", (c_ptr,), c_ptr)
py_list_get = extern("py_list_get", (c_ptr, c_int64), c_ptr)
py_list_append = extern("py_list_append", (c_ptr, c_ptr), c_void)
py_tuple_len = extern("py_tuple_len", (c_ptr,), c_int64)
py_tuple_get = extern("py_tuple_get", (c_ptr, c_int64), c_ptr)
pcc_gc_store_ptr = extern("pcc_gc_store_ptr", (c_ptr, c_ptr, c_ptr), c_void)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_note_slot_write_barrier = extern(
    "pcc_gc_note_slot_write_barrier", (c_ptr, c_ptr, c_ptr), c_void
)
pcc_gc_backend4_zpage_register_owner_payload_span = extern(
    "pcc_gc_backend4_zpage_register_owner_payload_span",
    (c_ptr, c_ptr, c_int64),
    c_int64,
)
pcc_gc_backend = extern("pcc_gc_backend", (), c_int64)
pcc_gc_scheduler_root_register_handle = extern(
    "pcc_gc_scheduler_root_register_handle", (c_ptr,), c_ptr
)
pcc_gc_scheduler_root_unregister_handle = extern(
    "pcc_gc_scheduler_root_unregister_handle", (c_ptr,), c_void
)
pcc_list_grow_for_mutation = extern(
    "pcc_list_grow_for_mutation", (c_ptr, c_int64), c_ptr
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
pcc_gc_retain_plan_prepare_locked = extern(
    "pcc_gc_retain_plan_prepare_locked", (c_ptr, c_ptr), c_ptr
)
pcc_gc_retain_plan_finish = extern(
    "pcc_gc_retain_plan_finish", (c_ptr,), c_void
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
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)
_pcc_debug_bad_incref = extern("pcc_debug_bad_incref", (c_ptr, c_int32), c_void)
getenv = extern("pcc_platform_getenv", (c_ptr,), c_ptr)


def _debug_bad_container(o, code: int) -> None:
    if ptr_is_null(getenv(cstr("PCC_DEBUG_RUNTIME"))) == 0:
        _pcc_debug_bad_incref(o, code)


pcc_gc_pointer_is_managed = extern(
    "pcc_gc_pointer_is_managed", (c_ptr,), c_int64
)


def _ptr_can_have_header(o) -> bool:
    return pcc_gc_pointer_is_managed(o) != 0


def _ptr_is_list(o) -> bool:
    if not _ptr_can_have_header(o):
        return False
    return load_i32(o, 8) == PY_TYPE_LIST


def _list_is_sane(lst, code: int) -> bool:
    if ptr_is_null(lst) != 0:
        return False
    if not _ptr_is_list(lst):
        _debug_bad_container(lst, code)
        return False
    length: int = load_i64(lst, 16)
    capacity: int = load_i64(lst, 24)
    items = load_ptr(lst, 32)
    if length < 0:
        _debug_bad_container(lst, code)
        return False
    if capacity < length:
        _debug_bad_container(lst, code)
        return False
    if capacity > 134217728:
        _debug_bad_container(lst, code)
        return False
    if ptr_is_null(items) != 0:
        _debug_bad_container(lst, code)
        return False
    return True


def _type_of(obj) -> int:
    if not _ptr_can_have_header(obj):
        if is_tagged_int(obj):
            return PY_TYPE_INT
        return -1
    return load_i32(obj, 8)


def _is_none_or_null(o) -> int:
    if ptr_is_null(o):
        return 1
    if ptr_eq(o, global_load_ptr("py_None")):
        return 1
    return 0


def _prepare_moving_root(slot, handle_slot) -> int:
    store_ptr(handle_slot, 0, null())
    value = load_ptr(slot, 0)
    if ptr_is_null(value) != 0 or is_tagged_int(value) != 0:
        return 0
    backend: int = pcc_gc_backend()
    if backend != 3 and backend != 4:
        return 0
    handle = pcc_gc_scheduler_root_register_handle(slot)
    if ptr_is_null(handle) != 0:
        return -1
    store_ptr(handle_slot, 0, handle)
    value = pcc_gc_load_ptr(null(), slot)
    store_ptr(slot, 0, value)
    if ptr_is_null(value) != 0:
        pcc_gc_scheduler_root_unregister_handle(handle)
        store_ptr(handle_slot, 0, null())
        return -1
    return 0


def _reload_moving_root(slot, handle_slot):
    value = load_ptr(slot, 0)
    if ptr_is_null(load_ptr(handle_slot, 0)) == 0:
        value = pcc_gc_load_ptr(null(), slot)
        store_ptr(slot, 0, value)
    return value


def _finish_moving_root(handle_slot) -> None:
    handle = load_ptr(handle_slot, 0)
    if ptr_is_null(handle) == 0:
        pcc_gc_scheduler_root_unregister_handle(handle)
        store_ptr(handle_slot, 0, null())


def _finish_slice_roots(list_handle_slot, replacement_handle_slot) -> None:
    _finish_moving_root(replacement_handle_slot)
    _finish_moving_root(list_handle_slot)


def _seq_len(seq) -> int:
    if ptr_is_null(seq):
        return -1
    tag: int = _type_of(seq)
    if tag == PY_TYPE_LIST:
        if not _list_is_sane(seq, -114):
            return -1
        return load_i64(seq, 16)
    if tag == PY_TYPE_TUPLE:
        n: int = load_i64(seq, 16)
        if n < 0 or n > 134217728:
            _debug_bad_container(seq, -115)
            return -1
        return n
    return -1


def _seq_get_borrowed(seq, i: int):
    tag: int = _type_of(seq)
    if tag == PY_TYPE_LIST:
        if not _list_is_sane(seq, -116):
            return null()
        items = load_ptr(seq, 32)
        return pcc_gc_load_ptr(seq, ptr_add(items, i * 8))
    if tag == PY_TYPE_TUPLE:
        items = ptr_add(seq, 24)
        return pcc_gc_load_ptr(seq, ptr_add(items, i * 8))
    return null()


def _slice_count(lo: int, hi: int, step: int) -> int:
    n: int = 0
    if step > 0:
        i: int = lo
        while i < hi:
            n = n + 1
            i = i + step
    else:
        i: int = lo
        while i > hi:
            if i < 0:
                return n
            n = n + 1
            i = i + step
    return n


def _normalize_set_slice_scalars(
    lo_none: int,
    hi_none: int,
    raw_lo: int,
    raw_hi: int,
    step: int,
    length: int,
    lo_out,
    hi_out,
) -> None:
    lo: int = raw_lo
    hi: int = raw_hi
    if lo_none != 0:
        if step > 0:
            lo = 0
        else:
            lo = length - 1
    if hi_none != 0:
        if step > 0:
            hi = length
        else:
            hi = -1
    if step > 0:
        if lo < 0:
            lo = lo + length
            if lo < 0:
                lo = 0
        if lo > length:
            lo = length
        if hi < 0:
            hi = hi + length
            if hi < 0:
                hi = 0
        if hi > length:
            hi = length
    else:
        if lo < 0:
            lo = lo + length
            if lo < 0:
                lo = -1
        if lo >= length:
            lo = length - 1
        if hi < 0:
            if hi_none != 0:
                hi = -1
            else:
                hi = hi + length
                if hi < 0:
                    hi = -1
        if hi >= length:
            hi = length - 1
    store_i64(lo_out, 0, lo)
    store_i64(hi_out, 0, hi)


def _snapshot_set_slice_replacement(replacement):
    tag: int = _type_of(replacement)
    if tag == PY_TYPE_LIST:
        return py_list_copy(replacement)
    if tag != PY_TYPE_TUPLE:
        return null()
    source_slot = stack_alloc(8)
    source_handle_slot = stack_alloc(8)
    store_ptr(source_slot, 0, replacement)
    if _prepare_moving_root(source_slot, source_handle_slot) != 0:
        return null()
    length: int = py_tuple_len(replacement)
    if length < 0:
        _finish_moving_root(source_handle_slot)
        return null()
    out = py_list_new(length if length > 0 else 4)
    if ptr_is_null(out) != 0:
        _finish_moving_root(source_handle_slot)
        return null()
    out_slot = stack_alloc(8)
    out_handle_slot = stack_alloc(8)
    store_ptr(out_slot, 0, out)
    if _prepare_moving_root(out_slot, out_handle_slot) != 0:
        _finish_moving_root(source_handle_slot)
        py_decref(out)
        return null()
    i: int = 0
    while i < length:
        replacement = _reload_moving_root(source_slot, source_handle_slot)
        out = _reload_moving_root(out_slot, out_handle_slot)
        value = py_tuple_get(replacement, i)
        if ptr_is_null(value) != 0:
            _finish_moving_root(out_handle_slot)
            _finish_moving_root(source_handle_slot)
            py_decref(out)
            return null()
        py_list_append(out, value)
        py_decref(value)
        i = i + 1
    out = _reload_moving_root(out_slot, out_handle_slot)
    _finish_moving_root(out_handle_slot)
    _finish_moving_root(source_handle_slot)
    return out


@c_abi_export("py_list_set_slice")
def py_list_set_slice(lst, lo, hi, step, replacement) -> int:
    return _set_slice_transaction(lst, lo, hi, step, replacement)


def _set_slice_transaction(lst, lo, hi, step, replacement) -> int:
    if not _list_is_sane(lst, -111):
        return -1
    if ptr_is_null(replacement) != 0:
        return -1
    list_slot = stack_alloc(8)
    list_handle_slot = stack_alloc(8)
    replacement_slot = stack_alloc(8)
    replacement_handle_slot = stack_alloc(8)
    lo_slot = stack_alloc(8)
    lo_handle_slot = stack_alloc(8)
    hi_slot = stack_alloc(8)
    hi_handle_slot = stack_alloc(8)
    step_slot = stack_alloc(8)
    step_handle_slot = stack_alloc(8)
    store_ptr(list_slot, 0, lst)
    store_ptr(replacement_slot, 0, replacement)
    store_ptr(lo_slot, 0, lo)
    store_ptr(hi_slot, 0, hi)
    store_ptr(step_slot, 0, step)
    if _prepare_moving_root(list_slot, list_handle_slot) != 0:
        return -1
    if _prepare_moving_root(replacement_slot, replacement_handle_slot) != 0:
        _finish_moving_root(list_handle_slot)
        return -1
    if _prepare_moving_root(lo_slot, lo_handle_slot) != 0:
        _finish_slice_roots(list_handle_slot, replacement_handle_slot)
        return -1
    if _prepare_moving_root(hi_slot, hi_handle_slot) != 0:
        _finish_moving_root(lo_handle_slot)
        _finish_slice_roots(list_handle_slot, replacement_handle_slot)
        return -1
    if _prepare_moving_root(step_slot, step_handle_slot) != 0:
        _finish_moving_root(hi_handle_slot)
        _finish_moving_root(lo_handle_slot)
        _finish_slice_roots(list_handle_slot, replacement_handle_slot)
        return -1

    step = _reload_moving_root(step_slot, step_handle_slot)
    step_none: int = _is_none_or_null(step)
    step_v: int = 1
    if step_none == 0:
        step_v = py_obj_index_i64(step)
        if py_err_occurred() != 0 or step_v == 0:
            if step_v == 0:
                py_raise_zero_slice_step()
            _finish_moving_root(step_handle_slot)
            _finish_moving_root(hi_handle_slot)
            _finish_moving_root(lo_handle_slot)
            _finish_slice_roots(list_handle_slot, replacement_handle_slot)
            return -1
    lo = _reload_moving_root(lo_slot, lo_handle_slot)
    lo_none: int = _is_none_or_null(lo)
    raw_lo: int = 0
    if lo_none == 0:
        raw_lo = py_obj_index_i64(lo)
        if py_err_occurred() != 0:
            _finish_moving_root(step_handle_slot)
            _finish_moving_root(hi_handle_slot)
            _finish_moving_root(lo_handle_slot)
            _finish_slice_roots(list_handle_slot, replacement_handle_slot)
            return -1
    hi = _reload_moving_root(hi_slot, hi_handle_slot)
    hi_none: int = _is_none_or_null(hi)
    raw_hi: int = 0
    if hi_none == 0:
        raw_hi = py_obj_index_i64(hi)
        if py_err_occurred() != 0:
            _finish_moving_root(step_handle_slot)
            _finish_moving_root(hi_handle_slot)
            _finish_moving_root(lo_handle_slot)
            _finish_slice_roots(list_handle_slot, replacement_handle_slot)
            return -1
    _finish_moving_root(step_handle_slot)
    _finish_moving_root(hi_handle_slot)
    _finish_moving_root(lo_handle_slot)

    replacement = _reload_moving_root(
        replacement_slot, replacement_handle_slot
    )
    snapshot = _snapshot_set_slice_replacement(replacement)
    if ptr_is_null(snapshot) != 0:
        _finish_slice_roots(list_handle_slot, replacement_handle_slot)
        return -1
    _finish_moving_root(replacement_handle_slot)
    snapshot_slot = stack_alloc(8)
    snapshot_handle_slot = stack_alloc(8)
    store_ptr(snapshot_slot, 0, snapshot)
    if _prepare_moving_root(snapshot_slot, snapshot_handle_slot) != 0:
        _finish_moving_root(list_handle_slot)
        py_decref(snapshot)
        return -1
    repl_len: int = load_i64(snapshot, 16)
    backend: int = pcc_gc_backend()
    lo_out = stack_alloc(8)
    hi_out = stack_alloc(8)
    attempt: int = 0
    while attempt < 8:
        attempt = attempt + 1
        if backend == 0:
            lst = _reload_moving_root(list_slot, list_handle_slot)
            old_len: int = load_i64(lst, 16)
            old_capacity: int = load_i64(lst, 24)
            old_items = load_ptr(lst, 32)
        else:
            pcc_py_gc_minor_graph_lock()
            lst = _reload_moving_root(list_slot, list_handle_slot)
            old_len = load_i64(lst, 16)
            old_capacity = load_i64(lst, 24)
            old_items = load_ptr(lst, 32)
            pcc_py_gc_minor_graph_unlock()
        _normalize_set_slice_scalars(
            lo_none,
            hi_none,
            raw_lo,
            raw_hi,
            step_v,
            old_len,
            lo_out,
            hi_out,
        )
        lo_v: int = load_i64(lo_out, 0)
        hi_v: int = load_i64(hi_out, 0)
        if step_v == 1 and hi_v < lo_v:
            hi_v = lo_v
        selected: int = 0
        if step_v == 1:
            if hi_v > lo_v:
                selected = hi_v - lo_v
        else:
            selected = _slice_count(lo_v, hi_v, step_v)
            if repl_len != selected:
                snapshot = _reload_moving_root(
                    snapshot_slot, snapshot_handle_slot
                )
                _finish_moving_root(snapshot_handle_slot)
                _finish_moving_root(list_handle_slot)
                py_decref(snapshot)
                return -1
        new_len: int = old_len
        if step_v == 1:
            new_len = old_len - selected + repl_len
        if old_len < 0 or new_len < 0 or old_capacity < old_len:
            break
        new_capacity: int = old_capacity
        while new_capacity < new_len:
            if new_capacity > 134217728:
                new_capacity = -1
                break
            new_capacity = new_capacity * 2
        if new_capacity <= 0:
            break
        new_items = malloc(new_capacity * 8)
        replacement_index = malloc((old_len if old_len > 0 else 1) * 8)
        if ptr_is_null(new_items) != 0 or ptr_is_null(replacement_index) != 0:
            free(replacement_index)
            free(new_items)
            break
        memset(new_items, 0, new_capacity * 8)
        i: int = 0
        while i < old_len:
            store_i64(replacement_index, i * 8, -1)
            i = i + 1
        if step_v != 1:
            idx: int = lo_v
            i = 0
            while i < repl_len:
                if idx >= 0 and idx < old_len:
                    store_i64(replacement_index, idx * 8, i)
                idx = idx + step_v
                i = i + 1

        if backend == 0:
            built: int = 0
            i = 0
            while i < new_len:
                value = null()
                if step_v == 1:
                    if i < lo_v:
                        value = py_list_get(lst, i)
                    elif i < lo_v + repl_len:
                        value = py_list_get(snapshot, i - lo_v)
                    else:
                        value = py_list_get(
                            lst, hi_v + i - (lo_v + repl_len)
                        )
                elif load_i64(replacement_index, i * 8) >= 0:
                    value = py_list_get(
                        snapshot, load_i64(replacement_index, i * 8)
                    )
                else:
                    value = py_list_get(lst, i)
                if ptr_is_null(value) != 0:
                    i = new_len
                else:
                    store_ptr(new_items, built * 8, value)
                    built = built + 1
                    i = i + 1
            if built != new_len:
                i = 0
                while i < built:
                    py_decref(load_ptr(new_items, i * 8))
                    i = i + 1
                free(replacement_index)
                free(new_items)
                break
            store_ptr(lst, 32, new_items)
            store_i64(lst, 24, new_capacity)
            store_i64(lst, 16, new_len)
            i = 0
            while i < old_len:
                value = load_ptr(old_items, i * 8)
                if ptr_is_null(value) == 0:
                    py_decref(value)
                i = i + 1
            free(old_items)
            free(replacement_index)
            snapshot = _reload_moving_root(snapshot_slot, snapshot_handle_slot)
            _finish_moving_root(snapshot_handle_slot)
            _finish_moving_root(list_handle_slot)
            py_decref(snapshot)
            return 0

        old_plans = null()
        new_plans = null()
        slot_pairs = null()
        if old_len > 0:
            old_plans = malloc(old_len * 128)
            slot_pairs = malloc(old_len * 16)
        if new_len > 0:
            new_plans = malloc(new_len * 56)
        if (
            (old_len > 0 and (
                ptr_is_null(old_plans) != 0 or ptr_is_null(slot_pairs) != 0
            ))
            or (new_len > 0 and ptr_is_null(new_plans) != 0)
        ):
            free(slot_pairs)
            free(new_plans)
            free(old_plans)
            free(replacement_index)
            free(new_items)
            break
        if old_len > 0:
            memset(old_plans, 0, old_len * 128)
            memset(slot_pairs, 0, old_len * 16)
        if new_len > 0:
            memset(new_plans, 0, new_len * 56)
        i = 0
        while i < old_len:
            pcc_gc_store_ptr_plan_init(
                ptr_add(old_plans, i * 128), load_ptr(list_slot, 0), backend
            )
            i = i + 1

        pcc_py_gc_minor_graph_lock()
        lst = _reload_moving_root(list_slot, list_handle_slot)
        snapshot = _reload_moving_root(snapshot_slot, snapshot_handle_slot)
        if (
            pcc_gc_backend() != backend
            or load_i64(lst, 16) != old_len
            or load_i64(lst, 24) != old_capacity
            or ptr_eq(load_ptr(lst, 32), old_items) == 0
            or load_i64(snapshot, 16) != repl_len
        ):
            pcc_py_gc_minor_graph_unlock()
            i = 0
            while i < old_len:
                pcc_gc_store_ptr_plan_finish(ptr_add(old_plans, i * 128))
                i = i + 1
            free(slot_pairs)
            free(new_plans)
            free(old_plans)
            free(replacement_index)
            free(new_items)
            continue
        snapshot_items = load_ptr(snapshot, 32)
        pair_count: int = 0
        i = 0
        while i < new_len:
            old_index: int = -1
            if step_v == 1:
                if i < lo_v:
                    old_index = i
                elif i >= lo_v + repl_len:
                    old_index = hi_v + i - (lo_v + repl_len)
            elif load_i64(replacement_index, i * 8) < 0:
                old_index = i
            if old_index >= 0:
                store_ptr(
                    slot_pairs, pair_count * 16, ptr_add(old_items, old_index * 8)
                )
                store_ptr(
                    slot_pairs, pair_count * 16 + 8, ptr_add(new_items, i * 8)
                )
                pair_count = pair_count + 1
            i = i + 1
        retargeted: int = pcc_gc_backend4_retarget_mutator_payload_locked(
            lst,
            old_items,
            old_capacity * 8,
            new_items,
            new_capacity * 8,
            slot_pairs,
            pair_count,
        )
        if retargeted == 0:
            pcc_py_gc_minor_graph_unlock()
            i = 0
            while i < old_len:
                pcc_gc_store_ptr_plan_finish(ptr_add(old_plans, i * 128))
                i = i + 1
            free(slot_pairs)
            free(new_plans)
            free(old_plans)
            free(replacement_index)
            free(new_items)
            break
        i = 0
        while i < new_len:
            value = null()
            if step_v == 1:
                if i < lo_v:
                    value = pcc_gc_load_ptr(lst, ptr_add(old_items, i * 8))
                elif i < lo_v + repl_len:
                    value = pcc_gc_load_ptr(
                        snapshot, ptr_add(snapshot_items, (i - lo_v) * 8)
                    )
                else:
                    old_index = hi_v + i - (lo_v + repl_len)
                    value = pcc_gc_load_ptr(
                        lst, ptr_add(old_items, old_index * 8)
                    )
            elif load_i64(replacement_index, i * 8) >= 0:
                value = pcc_gc_load_ptr(
                    snapshot,
                    ptr_add(
                        snapshot_items,
                        load_i64(replacement_index, i * 8) * 8,
                    ),
                )
            else:
                value = pcc_gc_load_ptr(lst, ptr_add(old_items, i * 8))
            retained = pcc_gc_retain_plan_prepare_locked(
                ptr_add(new_plans, i * 56), value
            )
            store_ptr(new_items, i * 8, retained)
            pcc_gc_note_slot_write_barrier(
                lst, ptr_add(new_items, i * 8), retained
            )
            i = i + 1
        i = 0
        while i < old_len:
            committed: int = pcc_gc_store_ptr_plan_commit_locked(
                ptr_add(old_plans, i * 128),
                lst,
                ptr_add(old_items, i * 8),
                null(),
            )
            if committed == 0:
                pcc_py_gc_minor_graph_unlock()
                pcc_platform_abort()
                return -1
            i = i + 1
        store_ptr(lst, 32, new_items)
        store_i64(lst, 24, new_capacity)
        store_i64(lst, 16, new_len)
        if retargeted == 2:
            pcc_gc_backend4_zpage_register_owner_payload_span(
                lst, new_items, new_capacity * 8
            )
        pcc_py_gc_minor_graph_unlock()

        i = 0
        while i < new_len:
            pcc_gc_retain_plan_finish(ptr_add(new_plans, i * 56))
            i = i + 1
        i = 0
        while i < old_len:
            pcc_gc_store_ptr_plan_finish(ptr_add(old_plans, i * 128))
            i = i + 1
        free(old_items)
        free(slot_pairs)
        free(new_plans)
        free(old_plans)
        free(replacement_index)
        snapshot = _reload_moving_root(snapshot_slot, snapshot_handle_slot)
        _finish_moving_root(snapshot_handle_slot)
        _finish_moving_root(list_handle_slot)
        py_decref(snapshot)
        return 0
    snapshot = _reload_moving_root(snapshot_slot, snapshot_handle_slot)
    _finish_moving_root(snapshot_handle_slot)
    _finish_moving_root(list_handle_slot)
    py_decref(snapshot)
    return -1
