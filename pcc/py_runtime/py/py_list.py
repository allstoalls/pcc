"""Phase 4c.13: pcc-Python port of py_list.c.

Growable PyObject* array with owned references.

PyListObject layout (from py_internal.h):
    offset  0   PyObjectHeader   (16 bytes)
    offset 16   length           (i64)
    offset 24   capacity         (i64)
    offset 32   items            (PyObject** — pointer to ptr array)
    total: 40 bytes

PyTupleObject layout:
    offset  0   PyObjectHeader   (16 bytes)
    offset 16   len              (i64)
    offset 24   items[]          (flexible-array-member of PyObject*)

Header layout and public type tags are imported from the generated
C-header-derived py_abi_constants module.
"""

__pcc_runtime_port__ = True

from pcc.extern import extern, c_abi_export, c_ptr, c_int32, c_int64, c_void
from pcc.py_runtime.py.py_abi_constants import (
    PYLISTOBJECT_CAPACITY_OFFSET,
    PYLISTOBJECT_ITEMS_OFFSET,
    PYLISTOBJECT_LENGTH_OFFSET,
    PYLISTOBJECT_SIZE,
    PYOBJECTHEADER_TYPE_TAG_OFFSET,
    PYTUPLEOBJECT_ITEMS_OFFSET,
    PYTUPLEOBJECT_LEN_OFFSET,
    PY_TYPE_INT,
    PY_TYPE_LIST,
    PY_TYPE_TUPLE,
)
from pcc.py_runtime.py.py_abi_constants import (
    PY_TYPE_DICT,
)
from pcc.unsafe import (
    cstr,
    free,
    global_load_ptr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_i8,
    load_ptr,
    malloc,
    memset,
    memmove,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    ptr_to_int,
    realloc,
    stack_alloc,
    store_i32,
    store_i64,
    store_i8,
    store_i8,
    store_ptr,
    untag_int,
)

py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_obj_eq = extern("py_obj_eq", (c_ptr, c_ptr), c_int32)
py_int_value_i64 = extern("py_int_value_i64", (c_ptr,), c_int64)
py_obj_index_i64 = extern("py_obj_index_i64", (c_ptr,), c_int64)
py_obj_iter = extern("py_obj_iter", (c_ptr,), c_ptr)
py_obj_next = extern("py_obj_next", (c_ptr,), c_ptr)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_current_exception = extern("py_current_exception", (), c_ptr)
py_exc_builtin_class = extern("py_exc_builtin_class", (c_int64,), c_ptr)
py_exc_matches = extern("py_exc_matches", (c_ptr, c_ptr), c_int64)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_gc_track = extern("py_gc_track", (c_ptr,), c_void)
pcc_gc_store_ptr = extern("pcc_gc_store_ptr", (c_ptr, c_ptr, c_ptr), c_void)
pcc_gc_try_store_ptr_take = extern("pcc_gc_try_store_ptr_take", (c_ptr, c_ptr, c_ptr), c_int64)
pcc_gc_try_take_ptr = extern("pcc_gc_try_take_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_store_ptr_fresh_native_instance = extern(
    "pcc_gc_store_ptr_fresh_native_instance",
    (c_ptr, c_ptr, c_ptr),
    c_void,
)
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
pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_ptr)
py_dict_clear = extern("py_dict_clear", (c_ptr,), c_void)
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
    return load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_LIST


def _list_is_sane(lst, code: int) -> bool:
    if ptr_is_null(lst) != 0:
        return False
    if not _ptr_is_list(lst):
        _debug_bad_container(lst, code)
        return False
    length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
    capacity: int = load_i64(lst, PYLISTOBJECT_CAPACITY_OFFSET)
    items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
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
    return load_i32(obj, PYOBJECTHEADER_TYPE_TAG_OFFSET)


def _is_none_or_null(o) -> int:
    if ptr_is_null(o):
        return 1
    if ptr_eq(o, global_load_ptr("py_None")):
        return 1
    return 0


def _int_to_i64_or_zero(o) -> int:
    if ptr_is_null(o) != 0:
        return 0
    if is_tagged_int(o) != 0:
        return untag_int(o)
    if load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_INT:
        return 0
    return py_int_value_i64(o)


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


def _grow_if_needed(l, want: int):
    if not _list_is_sane(l, -102):
        return null()
    capacity: int = load_i64(l, PYLISTOBJECT_CAPACITY_OFFSET)
    if capacity >= want:
        return l
    initial_backend: int = pcc_gc_backend()
    if initial_backend == 0:
        cap_fast: int = capacity
        if cap_fast <= 0:
            cap_fast = 4
        while cap_fast < want:
            if cap_fast > 134217728:
                return null()
            cap_fast = cap_fast * 2
        items_fast = load_ptr(l, PYLISTOBJECT_ITEMS_OFFSET)
        new_items_fast = realloc(items_fast, cap_fast * 8)
        if ptr_is_null(new_items_fast) != 0:
            return null()
        store_ptr(l, PYLISTOBJECT_ITEMS_OFFSET, new_items_fast)
        store_i64(l, PYLISTOBJECT_CAPACITY_OFFSET, cap_fast)
        return l

    owner_slot = stack_alloc(8)
    store_ptr(owner_slot, 0, l)
    owner_handle = null()
    if initial_backend == 3 or initial_backend == 4:
        owner_handle = pcc_gc_scheduler_root_register_handle(owner_slot)
        if ptr_is_null(owner_handle) != 0:
            return null()

    attempt: int = 0
    while attempt < 8:
        attempt = attempt + 1
        pcc_py_gc_minor_graph_lock()
        if pcc_gc_backend() != initial_backend:
            pcc_py_gc_minor_graph_unlock()
            break
        if ptr_is_null(owner_handle) == 0:
            l = pcc_gc_load_ptr(null(), owner_slot)
            store_ptr(owner_slot, 0, l)
        old_items = load_ptr(l, PYLISTOBJECT_ITEMS_OFFSET)
        old_capacity: int = load_i64(l, PYLISTOBJECT_CAPACITY_OFFSET)
        old_length: int = load_i64(l, PYLISTOBJECT_LENGTH_OFFSET)
        pcc_py_gc_minor_graph_unlock()
        if old_capacity >= want:
            if ptr_is_null(owner_handle) == 0:
                pcc_gc_scheduler_root_unregister_handle(owner_handle)
            return l
        if ptr_is_null(old_items) != 0 or old_capacity <= 0:
            break
        if old_length < 0 or old_length > old_capacity:
            break
        cap: int = old_capacity
        while cap < want:
            if cap > 134217728:
                cap = -1
                break
            cap = cap * 2
        if cap <= 0:
            break
        new_items = malloc(cap * 8)
        slot_pairs = null()
        if old_length > 0:
            slot_pairs = malloc(old_length * 16)
        if (
            ptr_is_null(new_items) != 0
            or (old_length > 0 and ptr_is_null(slot_pairs) != 0)
        ):
            free(slot_pairs)
            free(new_items)
            break
        memset(new_items, 0, cap * 8)
        if old_length > 0:
            memset(slot_pairs, 0, old_length * 16)

        pcc_py_gc_minor_graph_lock()
        if pcc_gc_backend() != initial_backend:
            pcc_py_gc_minor_graph_unlock()
            free(slot_pairs)
            free(new_items)
            break
        if ptr_is_null(owner_handle) == 0:
            l = pcc_gc_load_ptr(null(), owner_slot)
            store_ptr(owner_slot, 0, l)
        if (
            ptr_eq(load_ptr(l, PYLISTOBJECT_ITEMS_OFFSET), old_items) == 0
            or load_i64(l, PYLISTOBJECT_CAPACITY_OFFSET) != old_capacity
            or load_i64(l, PYLISTOBJECT_LENGTH_OFFSET) != old_length
        ):
            pcc_py_gc_minor_graph_unlock()
            free(slot_pairs)
            free(new_items)
            continue
        item_index: int = 0
        while item_index < old_length:
            old_slot = ptr_add(old_items, item_index * 8)
            new_slot = ptr_add(new_items, item_index * 8)
            item = pcc_gc_load_ptr(l, old_slot)
            store_ptr(new_slot, 0, item)
            store_ptr(slot_pairs, item_index * 16, old_slot)
            store_ptr(slot_pairs, item_index * 16 + 8, new_slot)
            item_index = item_index + 1
        retargeted: int = pcc_gc_backend4_retarget_mutator_payload_locked(
            l,
            old_items,
            old_capacity * 8,
            new_items,
            cap * 8,
            slot_pairs,
            old_length,
        )
        if retargeted == 0:
            pcc_py_gc_minor_graph_unlock()
            free(slot_pairs)
            free(new_items)
            break
        item_index = 0
        while item_index < old_length:
            new_slot = ptr_add(new_items, item_index * 8)
            pcc_gc_note_slot_write_barrier(
                l, new_slot, load_ptr(new_slot, 0)
            )
            item_index = item_index + 1
        store_ptr(l, PYLISTOBJECT_ITEMS_OFFSET, new_items)
        store_i64(l, PYLISTOBJECT_CAPACITY_OFFSET, cap)
        if retargeted == 2:
            pcc_gc_backend4_zpage_register_owner_payload_span(
                l, new_items, cap * 8
            )
        pcc_py_gc_minor_graph_unlock()
        free(old_items)
        free(slot_pairs)
        if ptr_is_null(owner_handle) == 0:
            pcc_gc_scheduler_root_unregister_handle(owner_handle)
        return l

    if ptr_is_null(owner_handle) == 0:
        pcc_gc_scheduler_root_unregister_handle(owner_handle)
    return null()


@c_abi_export("pcc_list_grow_for_mutation")
def pcc_list_grow_for_mutation(l, want: int):
    return _grow_if_needed(l, want)


def _normalize_index(i: int, length: int, clip: int) -> int:
    if clip != 0:
        if i < 0:
            i = i + length
            if i < 0:
                i = 0
        if i > length:
            i = length
        return i
    if i < 0:
        i = i + length
    if i < 0 or i >= length:
        return -1
    return i


@c_abi_export("py_list_new")
def py_list_new(initial_capacity: int):
    if initial_capacity > 134217728:
        _debug_bad_container(null(), -100)
        return null()
    l = pcc_gc_alloc(PYLISTOBJECT_SIZE, PY_TYPE_LIST, 0)
    if ptr_is_null(l):
        return null()
    store_i64(l, PYLISTOBJECT_LENGTH_OFFSET, 0)  # length
    cap: int = initial_capacity
    if cap < 4:
        cap = 4
    store_ptr(l, PYLISTOBJECT_ITEMS_OFFSET, null())  # items
    store_i64(l, PYLISTOBJECT_CAPACITY_OFFSET, cap)
    items = malloc(cap * 8)
    if ptr_is_null(items):
        py_decref(l)
        return null()
    store_ptr(l, PYLISTOBJECT_ITEMS_OFFSET, items)
    pcc_gc_backend4_zpage_register_owner_payload_span(l, items, cap * 8)
    py_gc_track(l)
    pcc_gc_publish_initialized(l)
    return l


@c_abi_export("py_list_append")
def py_list_append(lst, item) -> None:
    if not _list_is_sane(lst, -101):
        return
    if pcc_gc_backend() == 0:
        fast_length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
        fast_grown = _grow_if_needed(lst, fast_length + 1)
        if ptr_is_null(fast_grown) != 0:
            py_raise_owned(py_exc_new(19, cstr("list append: out of memory")))
            return
        fast_items = load_ptr(fast_grown, PYLISTOBJECT_ITEMS_OFFSET)
        store_ptr(fast_items, fast_length * 8, null())
        pcc_gc_store_ptr(
            fast_grown, ptr_add(fast_items, fast_length * 8), item
        )
        store_i64(
            fast_grown, PYLISTOBJECT_LENGTH_OFFSET, fast_length + 1
        )
        return
    list_slot = stack_alloc(8)
    list_handle_slot = stack_alloc(8)
    item_slot = stack_alloc(8)
    item_handle_slot = stack_alloc(8)
    store_ptr(list_slot, 0, lst)
    store_ptr(item_slot, 0, item)
    if _prepare_moving_root(list_slot, list_handle_slot) != 0:
        py_raise_owned(py_exc_new(19, cstr("list append: out of memory")))
        return
    if _prepare_moving_root(item_slot, item_handle_slot) != 0:
        _finish_moving_root(list_handle_slot)
        py_raise_owned(py_exc_new(19, cstr("list append: out of memory")))
        return
    lst = _reload_moving_root(list_slot, list_handle_slot)
    length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
    grown = _grow_if_needed(lst, length + 1)
    if ptr_is_null(grown) != 0:
        _finish_moving_root(item_handle_slot)
        _finish_moving_root(list_handle_slot)
        py_raise_owned(py_exc_new(19, cstr("list append: out of memory")))  # PY_EXC_MEMORYERROR
        return
    store_ptr(list_slot, 0, grown)
    commit_backend: int = pcc_gc_backend()
    store_plan = stack_alloc(128)
    pcc_gc_store_ptr_plan_init(
        store_plan, load_ptr(list_slot, 0), commit_backend
    )
    if commit_backend != 0:
        pcc_py_gc_minor_graph_lock()
    lst = _reload_moving_root(list_slot, list_handle_slot)
    item = _reload_moving_root(item_slot, item_handle_slot)
    items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
    store_ptr(items, length * 8, null())
    committed: int = pcc_gc_store_ptr_plan_commit_locked(
        store_plan, lst, ptr_add(items, length * 8), item
    )
    if committed != 0:
        store_i64(lst, PYLISTOBJECT_LENGTH_OFFSET, length + 1)
    if commit_backend != 0:
        pcc_py_gc_minor_graph_unlock()
    pcc_gc_store_ptr_plan_finish(store_plan)
    _finish_moving_root(item_handle_slot)
    _finish_moving_root(list_handle_slot)


@c_abi_export("py_list_append_fresh_native_instance")
def py_list_append_fresh_native_instance(lst, item) -> None:
    if not _list_is_sane(lst, -101):
        return
    if pcc_gc_backend() == 0:
        fast_length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
        fast_grown = _grow_if_needed(lst, fast_length + 1)
        if ptr_is_null(fast_grown) != 0:
            py_raise_owned(py_exc_new(19, cstr("list append: out of memory")))
            return
        fast_items = load_ptr(fast_grown, PYLISTOBJECT_ITEMS_OFFSET)
        store_ptr(fast_items, fast_length * 8, null())
        pcc_gc_store_ptr_fresh_native_instance(
            fast_grown,
            ptr_add(fast_items, fast_length * 8),
            item,
        )
        store_i64(
            fast_grown, PYLISTOBJECT_LENGTH_OFFSET, fast_length + 1
        )
        return
    list_slot = stack_alloc(8)
    list_handle_slot = stack_alloc(8)
    store_ptr(list_slot, 0, lst)
    if _prepare_moving_root(list_slot, list_handle_slot) != 0:
        py_raise_owned(py_exc_new(19, cstr("list append: out of memory")))
        return
    lst = _reload_moving_root(list_slot, list_handle_slot)
    length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
    grown = _grow_if_needed(lst, length + 1)
    if ptr_is_null(grown) != 0:
        _finish_moving_root(list_handle_slot)
        py_raise_owned(py_exc_new(19, cstr("list append: out of memory")))
        return
    store_ptr(list_slot, 0, grown)
    commit_backend: int = pcc_gc_backend()
    if commit_backend != 0:
        pcc_py_gc_minor_graph_lock()
    lst = _reload_moving_root(list_slot, list_handle_slot)
    items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
    store_ptr(items, length * 8, null())
    pcc_gc_store_ptr_fresh_native_instance(
        lst,
        ptr_add(items, length * 8),
        item,
    )
    store_i64(lst, PYLISTOBJECT_LENGTH_OFFSET, length + 1)
    if commit_backend != 0:
        pcc_py_gc_minor_graph_unlock()
    _finish_moving_root(list_handle_slot)


@c_abi_export("py_list_get")
def py_list_get(lst, i: int):
    if not _list_is_sane(lst, -103):
        return null()
    if pcc_gc_backend() == 0:
        fast_length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
        fast_idx: int = _normalize_index(i, fast_length, 0)
        if fast_idx < 0:
            return null()
        fast_items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
        fast_value = load_ptr(fast_items, fast_idx * 8)
        py_incref(fast_value)
        return fast_value
    list_slot = stack_alloc(8)
    list_handle_slot = stack_alloc(8)
    store_ptr(list_slot, 0, lst)
    if _prepare_moving_root(list_slot, list_handle_slot) != 0:
        return null()
    retain_plan = stack_alloc(56)
    pcc_py_gc_minor_graph_lock()
    lst = _reload_moving_root(list_slot, list_handle_slot)
    length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
    idx: int = _normalize_index(i, length, 0)
    if idx < 0:
        pcc_py_gc_minor_graph_unlock()
        _finish_moving_root(list_handle_slot)
        return null()
    items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
    v = pcc_gc_load_ptr(lst, ptr_add(items, idx * 8))
    v = pcc_gc_retain_plan_prepare_locked(retain_plan, v)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_retain_plan_finish(retain_plan)
    _finish_moving_root(list_handle_slot)
    return v


@c_abi_export("py_list_getitem")
def py_list_getitem(lst, i: int):
    # a[i] subscript: like py_list_get but raises IndexError on out-of-range so
    # try/except can catch it. Mirrors py_list_getitem in py_list.c; py_list_get
    # stays non-raising for other callers. Negative indices normalize.
    if not _list_is_sane(lst, -103):
        return null()
    if pcc_gc_backend() == 0:
        fast_length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
        fast_idx: int = _normalize_index(i, fast_length, 0)
        if fast_idx < 0:
            py_raise_owned(py_exc_new(5, cstr("list index out of range")))
            return null()
        fast_items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
        fast_value = load_ptr(fast_items, fast_idx * 8)
        py_incref(fast_value)
        return fast_value
    list_slot = stack_alloc(8)
    list_handle_slot = stack_alloc(8)
    store_ptr(list_slot, 0, lst)
    if _prepare_moving_root(list_slot, list_handle_slot) != 0:
        return null()
    retain_plan = stack_alloc(56)
    pcc_py_gc_minor_graph_lock()
    lst = _reload_moving_root(list_slot, list_handle_slot)
    length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
    idx: int = _normalize_index(i, length, 0)
    if idx < 0:
        pcc_py_gc_minor_graph_unlock()
        _finish_moving_root(list_handle_slot)
        py_raise_owned(py_exc_new(5, cstr("list index out of range")))  # PY_EXC_INDEXERROR
        return null()
    items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
    v = pcc_gc_load_ptr(lst, ptr_add(items, idx * 8))
    v = pcc_gc_retain_plan_prepare_locked(retain_plan, v)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_retain_plan_finish(retain_plan)
    _finish_moving_root(list_handle_slot)
    return v


@c_abi_export("py_list_get_i64")
def py_list_get_i64(lst, i: int) -> int:
    if ptr_is_null(lst) != 0:
        return 0
    if pcc_gc_backend() == 0:
        fast_length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
        fast_idx: int = _normalize_index(i, fast_length, 0)
        if fast_idx < 0:
            return 0
        fast_items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
        return _int_to_i64_or_zero(load_ptr(fast_items, fast_idx * 8))
    list_slot = stack_alloc(8)
    list_handle_slot = stack_alloc(8)
    store_ptr(list_slot, 0, lst)
    if _prepare_moving_root(list_slot, list_handle_slot) != 0:
        return 0
    pcc_py_gc_minor_graph_lock()
    lst = _reload_moving_root(list_slot, list_handle_slot)
    length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
    idx: int = _normalize_index(i, length, 0)
    if idx < 0:
        pcc_py_gc_minor_graph_unlock()
        _finish_moving_root(list_handle_slot)
        return 0
    items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
    v = pcc_gc_load_ptr(lst, ptr_add(items, idx * 8))
    result: int = _int_to_i64_or_zero(v)
    pcc_py_gc_minor_graph_unlock()
    _finish_moving_root(list_handle_slot)
    return result


@c_abi_export("py_list_get_i64_nonnegative")
def py_list_get_i64_nonnegative(lst, i: int) -> int:
    if ptr_is_null(lst) != 0:
        return 0
    if pcc_gc_backend() == 0:
        fast_length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
        if i < 0 or i >= fast_length:
            return 0
        fast_items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
        return _int_to_i64_or_zero(load_ptr(fast_items, i * 8))
    list_slot = stack_alloc(8)
    list_handle_slot = stack_alloc(8)
    store_ptr(list_slot, 0, lst)
    if _prepare_moving_root(list_slot, list_handle_slot) != 0:
        return 0
    pcc_py_gc_minor_graph_lock()
    lst = _reload_moving_root(list_slot, list_handle_slot)
    length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
    if i < 0 or i >= length:
        pcc_py_gc_minor_graph_unlock()
        _finish_moving_root(list_handle_slot)
        return 0
    items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
    v = pcc_gc_load_ptr(lst, ptr_add(items, i * 8))
    result: int = _int_to_i64_or_zero(v)
    pcc_py_gc_minor_graph_unlock()
    _finish_moving_root(list_handle_slot)
    return result


def _list_set_item_transaction(lst, i: int, item) -> int:
    if not _list_is_sane(lst, -104):
        return -1
    if pcc_gc_backend() == 0:
        fast_length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
        fast_idx: int = _normalize_index(i, fast_length, 0)
        if fast_idx < 0:
            return -1
        fast_items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
        pcc_gc_store_ptr(lst, ptr_add(fast_items, fast_idx * 8), item)
        return 0
    list_slot = stack_alloc(8)
    list_handle_slot = stack_alloc(8)
    item_slot = stack_alloc(8)
    item_handle_slot = stack_alloc(8)
    store_ptr(list_slot, 0, lst)
    store_ptr(item_slot, 0, item)
    if _prepare_moving_root(list_slot, list_handle_slot) != 0:
        return -1
    if _prepare_moving_root(item_slot, item_handle_slot) != 0:
        _finish_moving_root(list_handle_slot)
        return -1
    backend: int = pcc_gc_backend()
    store_plan = stack_alloc(128)
    pcc_gc_store_ptr_plan_init(
        store_plan, load_ptr(list_slot, 0), backend
    )
    pcc_py_gc_minor_graph_lock()
    lst = _reload_moving_root(list_slot, list_handle_slot)
    item = _reload_moving_root(item_slot, item_handle_slot)
    length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
    idx: int = _normalize_index(i, length, 0)
    if idx < 0:
        pcc_py_gc_minor_graph_unlock()
        pcc_gc_store_ptr_plan_finish(store_plan)
        _finish_moving_root(item_handle_slot)
        _finish_moving_root(list_handle_slot)
        return -1
    items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
    committed: int = pcc_gc_store_ptr_plan_commit_locked(
        store_plan, lst, ptr_add(items, idx * 8), item
    )
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_store_ptr_plan_finish(store_plan)
    _finish_moving_root(item_handle_slot)
    _finish_moving_root(list_handle_slot)
    if committed == 0:
        return -1
    return 0


@c_abi_export("py_list_set")
def py_list_set(lst, i: int, item) -> None:
    # Internal non-raising setter: callers (sort/insert shifts, generator
    # frames) index within bounds by construction. User-visible subscript
    # stores go through py_list_setitem below.
    _list_set_item_transaction(lst, i, item)


@c_abi_export("py_list_get_for_frame")
def py_list_get_for_frame(lst, i: int):
    if pcc_gc_backend() == 0:
        if not _list_is_sane(lst, -103):
            return null()
        index: int = _normalize_index(i, load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET), 0)
        if index < 0:
            return null()
        items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
        return pcc_gc_try_take_ptr(lst, ptr_add(items, index * 8))
    return py_list_get(lst, i)


@c_abi_export("py_list_set_from_owned_root")
def py_list_set_from_owned_root(lst, i: int, source_slot, owned_flag) -> None:
    if ptr_is_null(source_slot) or ptr_is_null(owned_flag):
        return
    if pcc_gc_backend() == 0:
        item = load_ptr(source_slot, 0)
        if (load_i8(owned_flag, 0) & 1) != 0 and _list_is_sane(lst, -104):
            index: int = _normalize_index(i, load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET), 0)
            if index >= 0:
                items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
                if pcc_gc_try_store_ptr_take(lst, ptr_add(items, index * 8), item) != 0:
                    store_i8(owned_flag, 0, 0)
                    return
        py_list_set(lst, i, item)
        return
    py_list_set(lst, i, pcc_gc_load_ptr(null(), source_slot))


@c_abi_export("py_list_setitem")
def py_list_setitem(lst, i: int, item) -> int:
    # items[i] = v subscript store: like py_list_set but raises IndexError on
    # out-of-range so try/except can catch it (CPython: "list assignment index
    # out of range"). Mirrors py_list_setitem in py_list.c; py_list_set stays
    # non-raising for internal callers. Negative indices normalize.
    if _list_set_item_transaction(lst, i, item) != 0:
        py_raise_owned(py_exc_new(5, cstr("list assignment index out of range")))  # PY_EXC_INDEXERROR
        return -1
    return 0


@c_abi_export("py_list_len")
def py_list_len(lst) -> int:
    if ptr_is_null(lst) != 0:
        return 0
    if pcc_gc_backend() == 0:
        return load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
    list_slot = stack_alloc(8)
    list_handle_slot = stack_alloc(8)
    store_ptr(list_slot, 0, lst)
    if _prepare_moving_root(list_slot, list_handle_slot) != 0:
        return 0
    pcc_py_gc_minor_graph_lock()
    lst = _reload_moving_root(list_slot, list_handle_slot)
    length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
    pcc_py_gc_minor_graph_unlock()
    _finish_moving_root(list_handle_slot)
    return length


def _append_snapshot_items(
    out_slot, source, source_length: int, repeat_count: int
) -> int:
    if ptr_is_null(out_slot) != 0:
        return -1
    if ptr_is_null(load_ptr(out_slot, 0)) != 0 or ptr_is_null(source) != 0:
        return -1
    out_handle_slot = stack_alloc(8)
    source_slot = stack_alloc(8)
    source_handle_slot = stack_alloc(8)
    store_ptr(source_slot, 0, source)
    if _prepare_moving_root(out_slot, out_handle_slot) != 0:
        return -1
    if _prepare_moving_root(source_slot, source_handle_slot) != 0:
        _finish_moving_root(out_handle_slot)
        return -1
    repeat: int = 0
    while repeat < repeat_count:
        i: int = 0
        while i < source_length:
            source = _reload_moving_root(source_slot, source_handle_slot)
            out = _reload_moving_root(out_slot, out_handle_slot)
            value = py_list_get(source, i)
            if ptr_is_null(value) != 0:
                _finish_moving_root(source_handle_slot)
                _finish_moving_root(out_handle_slot)
                return -1
            py_list_append(out, value)
            py_decref(value)
            if py_err_occurred() != 0:
                out = _reload_moving_root(out_slot, out_handle_slot)
                store_ptr(out_slot, 0, out)
                _finish_moving_root(source_handle_slot)
                _finish_moving_root(out_handle_slot)
                return -1
            i = i + 1
        repeat = repeat + 1
    out = _reload_moving_root(out_slot, out_handle_slot)
    store_ptr(out_slot, 0, out)
    _finish_moving_root(source_handle_slot)
    _finish_moving_root(out_handle_slot)
    return 0


@c_abi_export("py_list_concat")
def py_list_concat(a, b):
    if not _list_is_sane(a, -106):
        return null()
    if not _list_is_sane(b, -106):
        return null()
    la: int = load_i64(a, PYLISTOBJECT_LENGTH_OFFSET)
    lb: int = load_i64(b, PYLISTOBJECT_LENGTH_OFFSET)
    n: int = la + lb
    cap_hint: int = n
    if cap_hint <= 0:
        cap_hint = 4
    out = py_list_new(cap_hint)
    if ptr_is_null(out):
        return null()
    if pcc_gc_backend() != 0:
        out_slot = stack_alloc(8)
        store_ptr(out_slot, 0, out)
        if _append_snapshot_items(out_slot, a, la, 1) != 0:
            py_decref(load_ptr(out_slot, 0))
            return null()
        if _append_snapshot_items(out_slot, b, lb, 1) != 0:
            py_decref(load_ptr(out_slot, 0))
            return null()
        return load_ptr(out_slot, 0)
    out_items = load_ptr(out, PYLISTOBJECT_ITEMS_OFFSET)
    a_items = load_ptr(a, PYLISTOBJECT_ITEMS_OFFSET)
    i: int = 0
    while i < la:
        v = pcc_gc_load_ptr(a, ptr_add(a_items, i * 8))
        py_incref(v)
        store_ptr(out_items, i * 8, v)
        i = i + 1
    b_items = load_ptr(b, PYLISTOBJECT_ITEMS_OFFSET)
    j: int = 0
    while j < lb:
        v = pcc_gc_load_ptr(b, ptr_add(b_items, j * 8))
        py_incref(v)
        store_ptr(out_items, (la + j) * 8, v)
        j = j + 1
    store_i64(out, PYLISTOBJECT_LENGTH_OFFSET, n)
    return out


@c_abi_export("py_list_copy")
def py_list_copy(src):
    # list.copy() — shallow copy: fresh list, same elements (element refs
    # shared with the source, incref'd once each). Mirrors py_list_copy in
    # py_list.c. Empty source -> fresh empty list.
    if not _list_is_sane(src, -106):
        return null()
    n: int = load_i64(src, PYLISTOBJECT_LENGTH_OFFSET)
    cap_hint: int = n
    if cap_hint <= 0:
        cap_hint = 4
    out = py_list_new(cap_hint)
    if ptr_is_null(out):
        return null()
    if pcc_gc_backend() != 0:
        out_slot = stack_alloc(8)
        store_ptr(out_slot, 0, out)
        if _append_snapshot_items(out_slot, src, n, 1) != 0:
            py_decref(load_ptr(out_slot, 0))
            return null()
        return load_ptr(out_slot, 0)
    out_items = load_ptr(out, PYLISTOBJECT_ITEMS_OFFSET)
    src_items = load_ptr(src, PYLISTOBJECT_ITEMS_OFFSET)
    i: int = 0
    while i < n:
        v = pcc_gc_load_ptr(src, ptr_add(src_items, i * 8))
        py_incref(v)
        store_ptr(out_items, i * 8, v)
        i = i + 1
    store_i64(out, PYLISTOBJECT_LENGTH_OFFSET, n)
    return out


@c_abi_export("py_list_repeat")
def py_list_repeat(src, count: int):
    if not _list_is_sane(src, -107):
        return null()
    sl: int = load_i64(src, PYLISTOBJECT_LENGTH_OFFSET)
    real_count: int = count
    if real_count < 0:
        real_count = 0
    out_len: int = sl * real_count
    cap_hint: int = out_len
    if cap_hint <= 0:
        cap_hint = 4
    out = py_list_new(cap_hint)
    if ptr_is_null(out):
        return null()
    if pcc_gc_backend() != 0:
        out_slot = stack_alloc(8)
        store_ptr(out_slot, 0, out)
        if _append_snapshot_items(
            out_slot, src, sl, real_count
        ) != 0:
            py_decref(load_ptr(out_slot, 0))
            return null()
        return load_ptr(out_slot, 0)
    out_items = load_ptr(out, PYLISTOBJECT_ITEMS_OFFSET)
    src_items = load_ptr(src, PYLISTOBJECT_ITEMS_OFFSET)
    pos: int = 0
    k: int = 0
    while k < real_count:
        i: int = 0
        while i < sl:
            v = pcc_gc_load_ptr(src, ptr_add(src_items, i * 8))
            py_incref(v)
            store_ptr(out_items, pos * 8, v)
            pos = pos + 1
            i = i + 1
        k = k + 1
    store_i64(out, PYLISTOBJECT_LENGTH_OFFSET, out_len)
    return out


def _list_eq_at_callback(
    list_slot,
    list_handle_slot,
    query_slot,
    query_handle_slot,
    candidate_slot,
    index: int,
) -> int:
    retain_plan = stack_alloc(56)
    pcc_py_gc_minor_graph_lock()
    lst = _reload_moving_root(list_slot, list_handle_slot)
    length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
    if index < 0 or index >= length:
        pcc_py_gc_minor_graph_unlock()
        return 2
    items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
    candidate = pcc_gc_load_ptr(lst, ptr_add(items, index * 8))
    candidate = pcc_gc_retain_plan_prepare_locked(retain_plan, candidate)
    store_ptr(candidate_slot, 0, candidate)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_retain_plan_finish(retain_plan)

    query = _reload_moving_root(query_slot, query_handle_slot)
    equal: int = py_obj_eq(load_ptr(candidate_slot, 0), query)
    had_error: int = py_err_occurred()

    pcc_py_gc_minor_graph_lock()
    candidate = pcc_gc_load_ptr(null(), candidate_slot)
    store_ptr(candidate_slot, 0, null())
    pcc_py_gc_minor_graph_unlock()
    py_decref(candidate)
    if had_error != 0:
        return -1
    if equal != 0:
        return 1
    return 0


def _list_equality_scan(
    lst, item, start: int, stop: int, mode: int
) -> int:
    list_slot = stack_alloc(8)
    list_handle_slot = stack_alloc(8)
    query_slot = stack_alloc(8)
    query_handle_slot = stack_alloc(8)
    candidate_slot = stack_alloc(8)
    store_ptr(list_slot, 0, lst)
    store_ptr(query_slot, 0, item)
    store_ptr(candidate_slot, 0, null())
    if _prepare_moving_root(list_slot, list_handle_slot) != 0:
        return -1 if mode == 1 else 0
    if _prepare_moving_root(query_slot, query_handle_slot) != 0:
        _finish_moving_root(list_handle_slot)
        return -1 if mode == 1 else 0
    candidate_handle = pcc_gc_scheduler_root_register_handle(candidate_slot)
    if ptr_is_null(candidate_handle) != 0:
        _finish_moving_root(query_handle_slot)
        _finish_moving_root(list_handle_slot)
        return -1 if mode == 1 else 0
    index: int = start
    total: int = 0
    while stop < 0 or index < stop:
        compared: int = _list_eq_at_callback(
            list_slot,
            list_handle_slot,
            query_slot,
            query_handle_slot,
            candidate_slot,
            index,
        )
        if compared < 0:
            pcc_gc_scheduler_root_unregister_handle(candidate_handle)
            _finish_moving_root(query_handle_slot)
            _finish_moving_root(list_handle_slot)
            return -1 if mode == 1 else 0
        if compared == 2:
            break
        if compared == 1:
            if mode == 0:
                pcc_gc_scheduler_root_unregister_handle(candidate_handle)
                _finish_moving_root(query_handle_slot)
                _finish_moving_root(list_handle_slot)
                return 1
            if mode == 1:
                pcc_gc_scheduler_root_unregister_handle(candidate_handle)
                _finish_moving_root(query_handle_slot)
                _finish_moving_root(list_handle_slot)
                return index
            total = total + 1
        index = index + 1
    pcc_gc_scheduler_root_unregister_handle(candidate_handle)
    _finish_moving_root(query_handle_slot)
    _finish_moving_root(list_handle_slot)
    if mode == 1:
        return -1
    return total


@c_abi_export("py_list_contains")
def py_list_contains(lst, item) -> int:
    if not _list_is_sane(lst, -108):
        return 0
    if pcc_gc_backend() != 0:
        return _list_equality_scan(lst, item, 0, -1, 0)
    length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
    items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
    i: int = 0
    while i < length:
        v = pcc_gc_load_ptr(lst, ptr_add(items, i * 8))
        equal: int = py_obj_eq(v, item)
        if py_err_occurred() != 0:
            return 0
        if equal != 0:
            return 1
        i = i + 1
    return 0


def _push_to_list(out, v):
    # Helper: append v to out with grow-check; return canonical out or NULL.
    if not _list_is_sane(out, -109):
        return null()
    if pcc_gc_backend() == 0:
        fast_length: int = load_i64(out, PYLISTOBJECT_LENGTH_OFFSET)
        fast_grown = _grow_if_needed(out, fast_length + 1)
        if ptr_is_null(fast_grown) != 0:
            return null()
        fast_items = load_ptr(fast_grown, PYLISTOBJECT_ITEMS_OFFSET)
        store_ptr(fast_items, fast_length * 8, null())
        pcc_gc_store_ptr(
            fast_grown, ptr_add(fast_items, fast_length * 8), v
        )
        store_i64(
            fast_grown, PYLISTOBJECT_LENGTH_OFFSET, fast_length + 1
        )
        return fast_grown
    out_slot = stack_alloc(8)
    out_handle_slot = stack_alloc(8)
    value_slot = stack_alloc(8)
    value_handle_slot = stack_alloc(8)
    store_ptr(out_slot, 0, out)
    store_ptr(value_slot, 0, v)
    if _prepare_moving_root(out_slot, out_handle_slot) != 0:
        return null()
    if _prepare_moving_root(value_slot, value_handle_slot) != 0:
        _finish_moving_root(out_handle_slot)
        return null()
    out = _reload_moving_root(out_slot, out_handle_slot)
    out_len: int = load_i64(out, PYLISTOBJECT_LENGTH_OFFSET)
    grown = _grow_if_needed(out, out_len + 1)
    if ptr_is_null(grown) != 0:
        _finish_moving_root(value_handle_slot)
        _finish_moving_root(out_handle_slot)
        return null()
    store_ptr(out_slot, 0, grown)
    store_plan = stack_alloc(128)
    pcc_gc_store_ptr_plan_init(
        store_plan, load_ptr(out_slot, 0), pcc_gc_backend()
    )
    pcc_py_gc_minor_graph_lock()
    out = _reload_moving_root(out_slot, out_handle_slot)
    v = _reload_moving_root(value_slot, value_handle_slot)
    out_items = load_ptr(out, PYLISTOBJECT_ITEMS_OFFSET)
    store_ptr(out_items, out_len * 8, null())
    committed: int = pcc_gc_store_ptr_plan_commit_locked(
        store_plan, out, ptr_add(out_items, out_len * 8), v
    )
    if committed != 0:
        store_i64(out, PYLISTOBJECT_LENGTH_OFFSET, out_len + 1)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_store_ptr_plan_finish(store_plan)
    _finish_moving_root(value_handle_slot)
    _finish_moving_root(out_handle_slot)
    return out


def _seq_len(seq) -> int:
    if ptr_is_null(seq):
        return -1
    tag: int = _type_of(seq)
    if tag == PY_TYPE_LIST:  # PY_TYPE_LIST
        if not _list_is_sane(seq, -114):
            return -1
        return load_i64(seq, PYLISTOBJECT_LENGTH_OFFSET)
    if tag == PY_TYPE_TUPLE:  # PY_TYPE_TUPLE
        n: int = load_i64(seq, PYTUPLEOBJECT_LEN_OFFSET)
        if n < 0 or n > 134217728:
            _debug_bad_container(seq, -115)
            return -1
        return n
    return -1


def _seq_get_borrowed(seq, i: int):
    tag: int = _type_of(seq)
    if tag == PY_TYPE_LIST:  # PY_TYPE_LIST
        if not _list_is_sane(seq, -116):
            return null()
        items = load_ptr(seq, PYLISTOBJECT_ITEMS_OFFSET)
        return pcc_gc_load_ptr(seq, ptr_add(items, i * 8))
    if tag == PY_TYPE_TUPLE:  # PY_TYPE_TUPLE
        items = ptr_add(seq, PYTUPLEOBJECT_ITEMS_OFFSET)
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


def _list_delete_index(lst, idx: int) -> None:
    length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
    items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
    slot = ptr_add(items, idx * 8)
    old = pcc_gc_load_ptr(lst, slot)
    store_ptr(items, idx * 8, null())
    if ptr_is_null(old) == 0:
        py_decref(old)
    if idx < length - 1:
        src = ptr_add(items, (idx + 1) * 8)
        dst = ptr_add(items, idx * 8)
        memmove(dst, src, (length - idx - 1) * 8)
    store_i64(lst, PYLISTOBJECT_LENGTH_OFFSET, length - 1)


def _list_delete_range(lst, lo: int, hi: int) -> int:
    if hi <= lo:
        return 0
    length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
    items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
    i: int = lo
    while i < hi:
        slot = ptr_add(items, i * 8)
        old = pcc_gc_load_ptr(lst, slot)
        store_ptr(items, i * 8, null())
        if ptr_is_null(old) == 0:
            py_decref(old)
        i = i + 1
    if hi < length:
        src = ptr_add(items, hi * 8)
        dst = ptr_add(items, lo * 8)
        memmove(dst, src, (length - hi) * 8)
    store_i64(lst, PYLISTOBJECT_LENGTH_OFFSET, length - (hi - lo))
    return 0


def _normalize_delete_slice_scalars(
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


@c_abi_export("py_list_slice")
def py_list_slice(lst, lo, hi, step):
    if not _list_is_sane(lst, -110):
        return null()
    source_slot = stack_alloc(8)
    source_handle_slot = stack_alloc(8)
    store_ptr(source_slot, 0, lst)
    if _prepare_moving_root(source_slot, source_handle_slot) != 0:
        return null()
    lst = _reload_moving_root(source_slot, source_handle_slot)
    length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)

    step_v: int = 1
    if _is_none_or_null(step) == 0:
        step_v = py_obj_index_i64(step)
        if py_err_occurred() != 0:
            _finish_moving_root(source_handle_slot)
            return null()
        if step_v == 0:
            _finish_moving_root(source_handle_slot)
            return null()

    lo_v: int = 0
    hi_v: int = length
    if step_v > 0:
        if _is_none_or_null(lo) != 0:
            lo_v = 0
        else:
            lo_v = py_obj_index_i64(lo)
            if py_err_occurred() != 0:
                _finish_moving_root(source_handle_slot)
                return null()
        if _is_none_or_null(hi) != 0:
            hi_v = length
        else:
            hi_v = py_obj_index_i64(hi)
            if py_err_occurred() != 0:
                _finish_moving_root(source_handle_slot)
                return null()
    else:
        if _is_none_or_null(lo) != 0:
            lo_v = length - 1
        else:
            lo_v = py_obj_index_i64(lo)
            if py_err_occurred() != 0:
                _finish_moving_root(source_handle_slot)
                return null()
        if _is_none_or_null(hi) != 0:
            hi_v = -1
        else:
            hi_v = py_obj_index_i64(hi)
            if py_err_occurred() != 0:
                _finish_moving_root(source_handle_slot)
                return null()

    if step_v > 0:
        if lo_v < 0:
            lo_v = lo_v + length
            if lo_v < 0:
                lo_v = 0
        if lo_v > length:
            lo_v = length
        if hi_v < 0:
            hi_v = hi_v + length
            if hi_v < 0:
                hi_v = 0
        if hi_v > length:
            hi_v = length
    else:
        if lo_v < 0:
            lo_v = lo_v + length
            if lo_v < 0:
                lo_v = -1
        if lo_v >= length:
            lo_v = length - 1
        if hi_v < 0:
            if _is_none_or_null(hi) != 0:
                hi_v = -1
            else:
                hi_v = hi_v + length
                if hi_v < 0:
                    hi_v = -1
        if hi_v >= length:
            hi_v = length - 1

    out = py_list_new(4)
    if ptr_is_null(out):
        _finish_moving_root(source_handle_slot)
        return null()

    if step_v > 0:
        i: int = lo_v
        while i < hi_v:
            lst = _reload_moving_root(source_slot, source_handle_slot)
            items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
            v = pcc_gc_load_ptr(lst, ptr_add(items, i * 8))
            grown_out = _push_to_list(out, v)
            if ptr_is_null(grown_out) != 0:
                py_decref(out)
                _finish_moving_root(source_handle_slot)
                return null()
            out = grown_out
            i = i + step_v
    else:
        i: int = lo_v
        while i > hi_v:
            if i < 0 or i >= length:
                # break loop
                hi_v = i  # force exit
            else:
                lst = _reload_moving_root(source_slot, source_handle_slot)
                items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
                v = pcc_gc_load_ptr(lst, ptr_add(items, i * 8))
                grown_out = _push_to_list(out, v)
                if ptr_is_null(grown_out) != 0:
                    py_decref(out)
                    _finish_moving_root(source_handle_slot)
                    return null()
                out = grown_out
                i = i + step_v
    _finish_moving_root(source_handle_slot)
    return out


@c_abi_export("py_list_del_slice")
def py_list_del_slice(lst, lo, hi, step) -> int:
    if not _list_is_sane(lst, -112):
        return -1
    list_slot = stack_alloc(8)
    list_handle_slot = stack_alloc(8)
    lo_slot = stack_alloc(8)
    lo_handle_slot = stack_alloc(8)
    hi_slot = stack_alloc(8)
    hi_handle_slot = stack_alloc(8)
    step_slot = stack_alloc(8)
    step_handle_slot = stack_alloc(8)
    store_ptr(list_slot, 0, lst)
    store_ptr(lo_slot, 0, lo)
    store_ptr(hi_slot, 0, hi)
    store_ptr(step_slot, 0, step)
    if _prepare_moving_root(list_slot, list_handle_slot) != 0:
        return -1
    if _prepare_moving_root(lo_slot, lo_handle_slot) != 0:
        _finish_moving_root(list_handle_slot)
        return -1
    if _prepare_moving_root(hi_slot, hi_handle_slot) != 0:
        _finish_moving_root(lo_handle_slot)
        _finish_moving_root(list_handle_slot)
        return -1
    if _prepare_moving_root(step_slot, step_handle_slot) != 0:
        _finish_moving_root(hi_handle_slot)
        _finish_moving_root(lo_handle_slot)
        _finish_moving_root(list_handle_slot)
        return -1

    step = _reload_moving_root(step_slot, step_handle_slot)
    step_none: int = _is_none_or_null(step)
    step_v: int = 1
    if step_none == 0:
        step_v = py_obj_index_i64(step)
        if py_err_occurred() != 0 or step_v == 0:
            _finish_moving_root(step_handle_slot)
            _finish_moving_root(hi_handle_slot)
            _finish_moving_root(lo_handle_slot)
            _finish_moving_root(list_handle_slot)
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
            _finish_moving_root(list_handle_slot)
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
            _finish_moving_root(list_handle_slot)
            return -1
    _finish_moving_root(step_handle_slot)
    _finish_moving_root(hi_handle_slot)
    _finish_moving_root(lo_handle_slot)

    backend: int = pcc_gc_backend()
    lo_out = stack_alloc(8)
    hi_out = stack_alloc(8)
    if backend == 0:
        lst = _reload_moving_root(list_slot, list_handle_slot)
        length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
        _normalize_delete_slice_scalars(
            lo_none,
            hi_none,
            raw_lo,
            raw_hi,
            step_v,
            length,
            lo_out,
            hi_out,
        )
        lo_v: int = load_i64(lo_out, 0)
        hi_v: int = load_i64(hi_out, 0)
        if step_v == 1:
            result: int = _list_delete_range(lst, lo_v, hi_v)
            _finish_moving_root(list_handle_slot)
            return result
        count: int = _slice_count(lo_v, hi_v, step_v)
        if count > 0:
            if step_v > 0:
                idx: int = lo_v + (count - 1) * step_v
                n: int = 0
                while n < count:
                    _list_delete_index(lst, idx)
                    idx = idx - step_v
                    n = n + 1
            else:
                idx = lo_v
                n = 0
                while n < count:
                    _list_delete_index(lst, idx)
                    idx = idx + step_v
                    n = n + 1
        _finish_moving_root(list_handle_slot)
        return 0

    attempt: int = 0
    while attempt < 8:
        attempt = attempt + 1
        pcc_py_gc_minor_graph_lock()
        lst = _reload_moving_root(list_slot, list_handle_slot)
        length = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
        capacity: int = load_i64(lst, PYLISTOBJECT_CAPACITY_OFFSET)
        pcc_py_gc_minor_graph_unlock()
        _normalize_delete_slice_scalars(
            lo_none,
            hi_none,
            raw_lo,
            raw_hi,
            step_v,
            length,
            lo_out,
            hi_out,
        )
        lo_v = load_i64(lo_out, 0)
        hi_v = load_i64(hi_out, 0)
        count = _slice_count(lo_v, hi_v, step_v)
        if count <= 0:
            _finish_moving_root(list_handle_slot)
            return 0
        if length <= 0 or count > length:
            _finish_moving_root(list_handle_slot)
            return -1
        if length > 576460752303423487 or count > 72057594037927935:
            _finish_moving_root(list_handle_slot)
            return -1
        remove_mask = malloc(length)
        plans = malloc(count * 128)
        slot_pairs = malloc(length * 16)
        if (
            ptr_is_null(remove_mask) != 0
            or ptr_is_null(plans) != 0
            or ptr_is_null(slot_pairs) != 0
        ):
            free(slot_pairs)
            free(plans)
            free(remove_mask)
            _finish_moving_root(list_handle_slot)
            return -1
        memset(remove_mask, 0, length)
        memset(plans, 0, count * 128)
        memset(slot_pairs, 0, length * 16)
        idx = lo_v
        i: int = 0
        while i < count:
            if idx >= 0 and idx < length:
                store_i8(remove_mask, idx, 1)
            idx = idx + step_v
            i = i + 1
        i = 0
        while i < count:
            pcc_gc_store_ptr_plan_init(
                ptr_add(plans, i * 128), load_ptr(list_slot, 0), backend
            )
            i = i + 1

        pcc_py_gc_minor_graph_lock()
        lst = _reload_moving_root(list_slot, list_handle_slot)
        if (
            pcc_gc_backend() != backend
            or load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET) != length
            or load_i64(lst, PYLISTOBJECT_CAPACITY_OFFSET) != capacity
        ):
            pcc_py_gc_minor_graph_unlock()
            i = 0
            while i < count:
                pcc_gc_store_ptr_plan_finish(ptr_add(plans, i * 128))
                i = i + 1
            free(slot_pairs)
            free(plans)
            free(remove_mask)
            continue
        items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
        dst: int = 0
        pair_count: int = 0
        src: int = 0
        while src < length:
            if load_i8(remove_mask, src) == 0:
                if dst != src:
                    store_ptr(slot_pairs, pair_count * 16, ptr_add(items, src * 8))
                    store_ptr(
                        slot_pairs, pair_count * 16 + 8, ptr_add(items, dst * 8)
                    )
                    pair_count = pair_count + 1
                dst = dst + 1
            src = src + 1
        if pcc_gc_backend4_retarget_mutator_payload_locked(
            lst,
            items,
            capacity * 8,
            items,
            capacity * 8,
            slot_pairs,
            pair_count,
        ) == 0:
            pcc_py_gc_minor_graph_unlock()
            i = 0
            while i < count:
                pcc_gc_store_ptr_plan_finish(ptr_add(plans, i * 128))
                i = i + 1
            free(slot_pairs)
            free(plans)
            free(remove_mask)
            _finish_moving_root(list_handle_slot)
            return -1
        plan_i: int = 0
        src = 0
        while src < length:
            if load_i8(remove_mask, src) != 0:
                committed: int = pcc_gc_store_ptr_plan_commit_locked(
                    ptr_add(plans, plan_i * 128),
                    lst,
                    ptr_add(items, src * 8),
                    null(),
                )
                if committed == 0:
                    pcc_py_gc_minor_graph_unlock()
                    pcc_platform_abort()
                    return -1
                plan_i = plan_i + 1
            src = src + 1
        dst = 0
        src = 0
        while src < length:
            if load_i8(remove_mask, src) == 0:
                if dst != src:
                    value = pcc_gc_load_ptr(lst, ptr_add(items, src * 8))
                    store_ptr(items, dst * 8, value)
                    pcc_gc_note_slot_write_barrier(
                        lst, ptr_add(items, dst * 8), value
                    )
                dst = dst + 1
            src = src + 1
        i = dst
        while i < length:
            store_ptr(items, i * 8, null())
            i = i + 1
        store_i64(lst, PYLISTOBJECT_LENGTH_OFFSET, dst)
        pcc_py_gc_minor_graph_unlock()

        i = 0
        while i < count:
            pcc_gc_store_ptr_plan_finish(ptr_add(plans, i * 128))
            i = i + 1
        free(slot_pairs)
        free(plans)
        free(remove_mask)
        _finish_moving_root(list_handle_slot)
        return 0
    _finish_moving_root(list_handle_slot)
    return -1


@c_abi_export("py_list_extend")
def py_list_extend(a, b) -> None:
    if not _list_is_sane(a, -113):
        return
    if ptr_is_null(b):
        return
    if pcc_gc_backend() == 0:
        fast_tag: int = _type_of(b)
        if fast_tag == PY_TYPE_LIST:
            if not _list_is_sane(b, -117):
                return
            fast_bl: int = load_i64(b, PYLISTOBJECT_LENGTH_OFFSET)
            fast_la: int = load_i64(a, PYLISTOBJECT_LENGTH_OFFSET)
            fast_grown = _grow_if_needed(a, fast_la + fast_bl)
            if ptr_is_null(fast_grown) != 0:
                py_raise_owned(py_exc_new(19, cstr("list extend: out of memory")))
                return
            fast_a_items = load_ptr(fast_grown, PYLISTOBJECT_ITEMS_OFFSET)
            fast_b_items = load_ptr(b, PYLISTOBJECT_ITEMS_OFFSET)
            fast_i: int = 0
            while fast_i < fast_bl:
                fast_v = load_ptr(fast_b_items, fast_i * 8)
                store_ptr(fast_a_items, (fast_la + fast_i) * 8, null())
                pcc_gc_store_ptr(
                    fast_grown,
                    ptr_add(fast_a_items, (fast_la + fast_i) * 8),
                    fast_v,
                )
                fast_i = fast_i + 1
            store_i64(
                fast_grown, PYLISTOBJECT_LENGTH_OFFSET, fast_la + fast_bl
            )
            return
        if fast_tag == PY_TYPE_TUPLE:
            fast_bl: int = load_i64(b, PYTUPLEOBJECT_LEN_OFFSET)
            if fast_bl < 0 or fast_bl > 134217728:
                _debug_bad_container(b, -118)
                return
            fast_la: int = load_i64(a, PYLISTOBJECT_LENGTH_OFFSET)
            fast_grown = _grow_if_needed(a, fast_la + fast_bl)
            if ptr_is_null(fast_grown) != 0:
                py_raise_owned(py_exc_new(19, cstr("list extend: out of memory")))
                return
            fast_a_items = load_ptr(fast_grown, PYLISTOBJECT_ITEMS_OFFSET)
            fast_b_items = ptr_add(b, PYTUPLEOBJECT_ITEMS_OFFSET)
            fast_i: int = 0
            while fast_i < fast_bl:
                fast_v = load_ptr(fast_b_items, fast_i * 8)
                store_ptr(fast_a_items, (fast_la + fast_i) * 8, null())
                pcc_gc_store_ptr(
                    fast_grown,
                    ptr_add(fast_a_items, (fast_la + fast_i) * 8),
                    fast_v,
                )
                fast_i = fast_i + 1
            store_i64(
                fast_grown, PYLISTOBJECT_LENGTH_OFFSET, fast_la + fast_bl
            )
            return
        fast_it = py_obj_iter(b)
        if ptr_is_null(fast_it):
            return
        while True:
            fast_item = py_obj_next(fast_it)
            if ptr_is_null(fast_item):
                if py_err_occurred() != 0:
                    fast_cur = py_current_exception()
                    fast_stop = py_exc_builtin_class(8)
                    if py_exc_matches(fast_cur, fast_stop) != 0:
                        py_clear_exception()
                        break
                py_decref(fast_it)
                return
            py_list_append(a, fast_item)
            py_decref(fast_item)
        py_decref(fast_it)
        return

    list_slot = stack_alloc(8)
    list_handle_slot = stack_alloc(8)
    source_slot = stack_alloc(8)
    source_handle_slot = stack_alloc(8)
    store_ptr(list_slot, 0, a)
    store_ptr(source_slot, 0, b)
    if _prepare_moving_root(list_slot, list_handle_slot) != 0:
        py_raise_owned(py_exc_new(19, cstr("list extend: out of memory")))
        return
    if _prepare_moving_root(source_slot, source_handle_slot) != 0:
        _finish_moving_root(list_handle_slot)
        py_raise_owned(py_exc_new(19, cstr("list extend: out of memory")))
        return
    a = _reload_moving_root(list_slot, list_handle_slot)
    b = _reload_moving_root(source_slot, source_handle_slot)
    btag: int = _type_of(b)
    if btag == PY_TYPE_LIST:  # PY_TYPE_LIST
        if not _list_is_sane(b, -117):
            _finish_moving_root(source_handle_slot)
            _finish_moving_root(list_handle_slot)
            return
        bl: int = load_i64(b, PYLISTOBJECT_LENGTH_OFFSET)
        la: int = load_i64(a, PYLISTOBJECT_LENGTH_OFFSET)
        grown = _grow_if_needed(a, la + bl)
        if ptr_is_null(grown) != 0:
            _finish_moving_root(source_handle_slot)
            _finish_moving_root(list_handle_slot)
            py_raise_owned(py_exc_new(19, cstr("list extend: out of memory")))  # PY_EXC_MEMORYERROR
            return
        store_ptr(list_slot, 0, grown)
        i: int = 0
        store_plan = stack_alloc(128)
        while i < bl:
            pcc_gc_store_ptr_plan_init(
                store_plan, load_ptr(list_slot, 0), pcc_gc_backend()
            )
            pcc_py_gc_minor_graph_lock()
            a = _reload_moving_root(list_slot, list_handle_slot)
            b = _reload_moving_root(source_slot, source_handle_slot)
            a_items = load_ptr(a, PYLISTOBJECT_ITEMS_OFFSET)
            b_items = load_ptr(b, PYLISTOBJECT_ITEMS_OFFSET)
            v = pcc_gc_load_ptr(b, ptr_add(b_items, i * 8))
            # Match py_list_append's grown-slot store: NULL-init the fresh
            # (unzeroed) capacity slot, then route through the collector
            # barrier. pcc_gc_store_ptr increfs v, so the prior manual incref
            # is dropped to keep the net accounting (+1 owned ref) identical.
            store_ptr(a_items, (la + i) * 8, null())
            committed: int = pcc_gc_store_ptr_plan_commit_locked(
                store_plan, a, ptr_add(a_items, (la + i) * 8), v
            )
            if committed != 0:
                store_i64(a, PYLISTOBJECT_LENGTH_OFFSET, la + i + 1)
            pcc_py_gc_minor_graph_unlock()
            pcc_gc_store_ptr_plan_finish(store_plan)
            i = i + 1
        _finish_moving_root(source_handle_slot)
        _finish_moving_root(list_handle_slot)
        return
    if btag == PY_TYPE_TUPLE:  # PY_TYPE_TUPLE
        # PyTupleObject layout: header(16) + len(16) + items(24, flex array)
        bl: int = load_i64(b, PYTUPLEOBJECT_LEN_OFFSET)
        if bl < 0 or bl > 134217728:
            _debug_bad_container(b, -118)
            _finish_moving_root(source_handle_slot)
            _finish_moving_root(list_handle_slot)
            return
        la: int = load_i64(a, PYLISTOBJECT_LENGTH_OFFSET)
        grown = _grow_if_needed(a, la + bl)
        if ptr_is_null(grown) != 0:
            _finish_moving_root(source_handle_slot)
            _finish_moving_root(list_handle_slot)
            py_raise_owned(py_exc_new(19, cstr("list extend: out of memory")))  # PY_EXC_MEMORYERROR
            return
        store_ptr(list_slot, 0, grown)
        i: int = 0
        store_plan = stack_alloc(128)
        while i < bl:
            pcc_gc_store_ptr_plan_init(
                store_plan, load_ptr(list_slot, 0), pcc_gc_backend()
            )
            pcc_py_gc_minor_graph_lock()
            a = _reload_moving_root(list_slot, list_handle_slot)
            b = _reload_moving_root(source_slot, source_handle_slot)
            a_items = load_ptr(a, PYLISTOBJECT_ITEMS_OFFSET)
            b_items_base = ptr_add(b, PYTUPLEOBJECT_ITEMS_OFFSET)
            v = pcc_gc_load_ptr(b, ptr_add(b_items_base, i * 8))
            # Same grown-slot store idiom as the list branch / py_list_append:
            # NULL-init the fresh (unzeroed) capacity slot, then barrier-store.
            # pcc_gc_store_ptr increfs v, so the manual incref is dropped (net +1).
            store_ptr(a_items, (la + i) * 8, null())
            committed: int = pcc_gc_store_ptr_plan_commit_locked(
                store_plan, a, ptr_add(a_items, (la + i) * 8), v
            )
            if committed != 0:
                store_i64(a, PYLISTOBJECT_LENGTH_OFFSET, la + i + 1)
            pcc_py_gc_minor_graph_unlock()
            pcc_gc_store_ptr_plan_finish(store_plan)
            i = i + 1
        _finish_moving_root(source_handle_slot)
        _finish_moving_root(list_handle_slot)
        return
    b = _reload_moving_root(source_slot, source_handle_slot)
    it = py_obj_iter(b)
    if ptr_is_null(it):
        _finish_moving_root(source_handle_slot)
        _finish_moving_root(list_handle_slot)
        return
    while True:
        item = py_obj_next(it)
        if ptr_is_null(item):
            if py_err_occurred() != 0:
                cur = py_current_exception()
                stop = py_exc_builtin_class(8)  # StopIteration
                if py_exc_matches(cur, stop) != 0:
                    py_clear_exception()
                    break
            py_decref(it)
            _finish_moving_root(source_handle_slot)
            _finish_moving_root(list_handle_slot)
            return
        a = _reload_moving_root(list_slot, list_handle_slot)
        py_list_append(a, item)
        py_decref(item)
    py_decref(it)
    _finish_moving_root(source_handle_slot)
    _finish_moving_root(list_handle_slot)


@c_abi_export("py_list_insert")
def py_list_insert(lst, i: int, item) -> None:
    if not _list_is_sane(lst, -119):
        return
    if pcc_gc_backend() == 0:
        fast_length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
        fast_idx: int = _normalize_index(i, fast_length, 1)
        fast_grown = _grow_if_needed(lst, fast_length + 1)
        if ptr_is_null(fast_grown) != 0:
            py_raise_owned(py_exc_new(19, cstr("list insert: out of memory")))
            return
        fast_items = load_ptr(fast_grown, PYLISTOBJECT_ITEMS_OFFSET)
        if fast_idx < fast_length:
            memmove(
                ptr_add(fast_items, (fast_idx + 1) * 8),
                ptr_add(fast_items, fast_idx * 8),
                (fast_length - fast_idx) * 8,
            )
        store_ptr(fast_items, fast_idx * 8, null())
        pcc_gc_store_ptr(
            fast_grown, ptr_add(fast_items, fast_idx * 8), item
        )
        store_i64(
            fast_grown, PYLISTOBJECT_LENGTH_OFFSET, fast_length + 1
        )
        return
    list_slot = stack_alloc(8)
    list_handle_slot = stack_alloc(8)
    item_slot = stack_alloc(8)
    item_handle_slot = stack_alloc(8)
    store_ptr(list_slot, 0, lst)
    store_ptr(item_slot, 0, item)
    if _prepare_moving_root(list_slot, list_handle_slot) != 0:
        py_raise_owned(py_exc_new(19, cstr("list insert: out of memory")))
        return
    if _prepare_moving_root(item_slot, item_handle_slot) != 0:
        _finish_moving_root(list_handle_slot)
        py_raise_owned(py_exc_new(19, cstr("list insert: out of memory")))
        return
    lst = _reload_moving_root(list_slot, list_handle_slot)
    length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
    idx: int = _normalize_index(i, length, 1)
    grown = _grow_if_needed(lst, length + 1)
    if ptr_is_null(grown) != 0:
        _finish_moving_root(item_handle_slot)
        _finish_moving_root(list_handle_slot)
        py_raise_owned(py_exc_new(19, cstr("list insert: out of memory")))  # PY_EXC_MEMORYERROR
        return
    store_ptr(list_slot, 0, grown)
    commit_backend: int = pcc_gc_backend()
    store_plan = stack_alloc(128)
    pcc_gc_store_ptr_plan_init(
        store_plan, load_ptr(list_slot, 0), commit_backend
    )
    if commit_backend != 0:
        pcc_py_gc_minor_graph_lock()
    lst = _reload_moving_root(list_slot, list_handle_slot)
    item = _reload_moving_root(item_slot, item_handle_slot)
    items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
    if idx < length:
        # Shift tail [idx, length) right by one.
        src = ptr_add(items, idx * 8)
        dst = ptr_add(items, (idx + 1) * 8)
        memmove(dst, src, (length - idx) * 8)
    store_ptr(items, idx * 8, null())
    committed: int = pcc_gc_store_ptr_plan_commit_locked(
        store_plan, lst, ptr_add(items, idx * 8), item
    )
    if committed != 0:
        store_i64(lst, PYLISTOBJECT_LENGTH_OFFSET, length + 1)
    if commit_backend != 0:
        pcc_py_gc_minor_graph_unlock()
    pcc_gc_store_ptr_plan_finish(store_plan)
    _finish_moving_root(item_handle_slot)
    _finish_moving_root(list_handle_slot)


@c_abi_export("py_list_pop")
def py_list_pop(lst, i: int):
    if not _list_is_sane(lst, -120):
        return null()
    if pcc_gc_backend() == 0:
        fast_length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
        if fast_length == 0:
            py_raise_owned(py_exc_new(5, cstr("pop from empty list")))
            return null()
        fast_idx: int = 0
        if i == -1:
            fast_idx = fast_length - 1
        else:
            fast_idx = _normalize_index(i, fast_length, 0)
            if fast_idx < 0:
                py_raise_owned(py_exc_new(5, cstr("pop index out of range")))
                return null()
        fast_items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
        fast_value = load_ptr(fast_items, fast_idx * 8)
        if fast_idx < fast_length - 1:
            memmove(
                ptr_add(fast_items, fast_idx * 8),
                ptr_add(fast_items, (fast_idx + 1) * 8),
                (fast_length - fast_idx - 1) * 8,
            )
        store_i64(lst, PYLISTOBJECT_LENGTH_OFFSET, fast_length - 1)
        return fast_value
    list_slot = stack_alloc(8)
    list_handle_slot = stack_alloc(8)
    store_ptr(list_slot, 0, lst)
    if _prepare_moving_root(list_slot, list_handle_slot) != 0:
        return null()
    result_slot = stack_alloc(8)
    store_ptr(result_slot, 0, null())
    result_handle = pcc_gc_scheduler_root_register_handle(result_slot)
    if ptr_is_null(result_handle) != 0:
        _finish_moving_root(list_handle_slot)
        return null()
    pcc_py_gc_minor_graph_lock()
    lst = _reload_moving_root(list_slot, list_handle_slot)
    length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
    if length == 0:
        pcc_py_gc_minor_graph_unlock()
        pcc_gc_scheduler_root_unregister_handle(result_handle)
        _finish_moving_root(list_handle_slot)
        py_raise_owned(py_exc_new(5, cstr("pop from empty list")))  # PY_EXC_INDEXERROR
        return null()
    idx: int = 0
    if i == -1:
        idx = length - 1
    else:
        idx = _normalize_index(i, length, 0)
        if idx < 0:
            pcc_py_gc_minor_graph_unlock()
            pcc_gc_scheduler_root_unregister_handle(result_handle)
            _finish_moving_root(list_handle_slot)
            py_raise_owned(py_exc_new(5, cstr("pop index out of range")))  # PY_EXC_INDEXERROR
            return null()
    items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
    v = pcc_gc_load_ptr(lst, ptr_add(items, idx * 8))
    store_ptr(result_slot, 0, v)
    if idx < length - 1:
        src = ptr_add(items, (idx + 1) * 8)
        dst = ptr_add(items, idx * 8)
        memmove(dst, src, (length - idx - 1) * 8)
    store_i64(lst, PYLISTOBJECT_LENGTH_OFFSET, length - 1)
    pcc_py_gc_minor_graph_unlock()
    _finish_moving_root(list_handle_slot)
    pcc_gc_scheduler_root_unregister_handle(result_handle)
    return load_ptr(result_slot, 0)


@c_abi_export("py_list_remove")
def py_list_remove(lst, item) -> None:
    if not _list_is_sane(lst, -121):
        return
    if pcc_gc_backend() == 0:
        fast_length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
        fast_items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
        fast_i: int = 0
        while fast_i < fast_length:
            fast_value = load_ptr(fast_items, fast_i * 8)
            equal: int = py_obj_eq(fast_value, item)
            if py_err_occurred() != 0:
                return
            if equal != 0:
                store_ptr(fast_items, fast_i * 8, null())
                py_decref(fast_value)
                if fast_i < fast_length - 1:
                    memmove(
                        ptr_add(fast_items, fast_i * 8),
                        ptr_add(fast_items, (fast_i + 1) * 8),
                        (fast_length - fast_i - 1) * 8,
                    )
                store_i64(
                    lst, PYLISTOBJECT_LENGTH_OFFSET, fast_length - 1
                )
                return
            fast_i = fast_i + 1
        exc = py_exc_new(12, null())
        py_raise_owned(exc)
        return
    list_slot = stack_alloc(8)
    list_handle_slot = stack_alloc(8)
    query_slot = stack_alloc(8)
    query_handle_slot = stack_alloc(8)
    candidate_slot = stack_alloc(8)
    store_ptr(list_slot, 0, lst)
    store_ptr(query_slot, 0, item)
    store_ptr(candidate_slot, 0, null())
    if _prepare_moving_root(list_slot, list_handle_slot) != 0:
        return
    if _prepare_moving_root(query_slot, query_handle_slot) != 0:
        _finish_moving_root(list_handle_slot)
        return
    candidate_handle = pcc_gc_scheduler_root_register_handle(candidate_slot)
    if ptr_is_null(candidate_handle) != 0:
        _finish_moving_root(query_handle_slot)
        _finish_moving_root(list_handle_slot)
        return
    index: int = 0
    while True:
        compared: int = _list_eq_at_callback(
            list_slot,
            list_handle_slot,
            query_slot,
            query_handle_slot,
            candidate_slot,
            index,
        )
        if compared < 0:
            pcc_gc_scheduler_root_unregister_handle(candidate_handle)
            _finish_moving_root(query_handle_slot)
            _finish_moving_root(list_handle_slot)
            return
        if compared == 2:
            break
        if compared == 0:
            index = index + 1
            continue
        pcc_py_gc_minor_graph_lock()
        lst = _reload_moving_root(list_slot, list_handle_slot)
        length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
        if index >= 0 and index < length:
            items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
            removed = pcc_gc_load_ptr(lst, ptr_add(items, index * 8))
            store_ptr(candidate_slot, 0, removed)
            store_ptr(items, index * 8, null())
            if index < length - 1:
                memmove(
                    ptr_add(items, index * 8),
                    ptr_add(items, (index + 1) * 8),
                    (length - index - 1) * 8,
                )
            store_i64(lst, PYLISTOBJECT_LENGTH_OFFSET, length - 1)
        pcc_py_gc_minor_graph_unlock()
        _finish_moving_root(query_handle_slot)
        _finish_moving_root(list_handle_slot)
        pcc_gc_scheduler_root_unregister_handle(candidate_handle)
        removed = load_ptr(candidate_slot, 0)
        if ptr_is_null(removed) == 0:
            py_decref(removed)
        return
    pcc_gc_scheduler_root_unregister_handle(candidate_handle)
    _finish_moving_root(query_handle_slot)
    _finish_moving_root(list_handle_slot)
    # Not found: raise ValueError. py_exc_new takes the private exception-table
    # tag, not a public object type tag.
    exc = py_exc_new(12, null())
    py_raise_owned(exc)


@c_abi_export("py_list_clear")
def py_list_clear(lst) -> None:
    if not _list_is_sane(lst, -122):
        return
    if pcc_gc_backend() == 0:
        fast_length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
        store_i64(lst, PYLISTOBJECT_LENGTH_OFFSET, 0)
        fast_items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
        fast_i: int = 0
        while fast_i < fast_length:
            fast_value = load_ptr(fast_items, fast_i * 8)
            store_ptr(fast_items, fast_i * 8, null())
            if ptr_is_null(fast_value) == 0:
                py_decref(fast_value)
            fast_i = fast_i + 1
        return

    list_slot = stack_alloc(8)
    list_handle_slot = stack_alloc(8)
    store_ptr(list_slot, 0, lst)
    if _prepare_moving_root(list_slot, list_handle_slot) != 0:
        return
    backend: int = pcc_gc_backend()
    attempt: int = 0
    while attempt < 8:
        attempt = attempt + 1
        pcc_py_gc_minor_graph_lock()
        lst = _reload_moving_root(list_slot, list_handle_slot)
        length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
        pcc_py_gc_minor_graph_unlock()
        if length <= 0:
            _finish_moving_root(list_handle_slot)
            return
        if length > 72057594037927935:
            _finish_moving_root(list_handle_slot)
            return
        plans = malloc(length * 128)
        if ptr_is_null(plans) != 0:
            _finish_moving_root(list_handle_slot)
            return
        memset(plans, 0, length * 128)
        plan_i: int = 0
        while plan_i < length:
            pcc_gc_store_ptr_plan_init(
                ptr_add(plans, plan_i * 128), load_ptr(list_slot, 0), backend
            )
            plan_i = plan_i + 1

        pcc_py_gc_minor_graph_lock()
        if pcc_gc_backend() != backend:
            pcc_py_gc_minor_graph_unlock()
            plan_i = 0
            while plan_i < length:
                pcc_gc_store_ptr_plan_finish(ptr_add(plans, plan_i * 128))
                plan_i = plan_i + 1
            free(plans)
            _finish_moving_root(list_handle_slot)
            return
        lst = _reload_moving_root(list_slot, list_handle_slot)
        if load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET) != length:
            pcc_py_gc_minor_graph_unlock()
            plan_i = 0
            while plan_i < length:
                pcc_gc_store_ptr_plan_finish(ptr_add(plans, plan_i * 128))
                plan_i = plan_i + 1
            free(plans)
            continue
        items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
        plan_i = 0
        while plan_i < length:
            committed: int = pcc_gc_store_ptr_plan_commit_locked(
                ptr_add(plans, plan_i * 128),
                lst,
                ptr_add(items, plan_i * 8),
                null(),
            )
            if committed == 0:
                pcc_py_gc_minor_graph_unlock()
                pcc_platform_abort()
                return
            plan_i = plan_i + 1
        store_i64(lst, PYLISTOBJECT_LENGTH_OFFSET, 0)
        pcc_py_gc_minor_graph_unlock()

        plan_i = 0
        while plan_i < length:
            pcc_gc_store_ptr_plan_finish(ptr_add(plans, plan_i * 128))
            plan_i = plan_i + 1
        free(plans)
        _finish_moving_root(list_handle_slot)
        return
    _finish_moving_root(list_handle_slot)


@c_abi_export("py_obj_clear")
def py_obj_clear(obj) -> None:
    tag: int = _type_of(obj)
    if tag == PY_TYPE_LIST:  # PY_TYPE_LIST
        py_list_clear(obj)
    elif tag == PY_TYPE_DICT:  # PY_TYPE_DICT
        py_dict_clear(obj)


@c_abi_export("py_list_index")
def py_list_index(lst, item) -> int:
    if not _list_is_sane(lst, -123):
        return -1
    if pcc_gc_backend() != 0:
        return _list_equality_scan(lst, item, 0, -1, 1)
    length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
    items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
    i: int = 0
    while i < length:
        v = pcc_gc_load_ptr(lst, ptr_add(items, i * 8))
        equal: int = py_obj_eq(v, item)
        if py_err_occurred() != 0:
            return -1
        if equal != 0:
            return i
        i = i + 1
    return -1


@c_abi_export("py_list_index_range")
def py_list_index_range(lst, item, start: int, end: int) -> int:
    # Range-aware list.index(item, start, end). Negative bounds offset by the
    # length once, then both clamp into [0, length]; the half-open window
    # [start, end) is scanned. Raise ValueError (PY_EXC_VALUEERROR == 2) and
    # return -1 when absent, matching CPython list.index. The frontend checks
    # py_err_occurred() after the call.
    length: int = 0
    if _list_is_sane(lst, -126):
        length = py_list_len(lst)
    if start < 0:
        start = start + length
        if start < 0:
            start = 0
    elif start > length:
        start = length
    if end < 0:
        end = end + length
        if end < 0:
            end = 0
    elif end > length:
        end = length
    if pcc_gc_backend() != 0:
        found: int = _list_equality_scan(lst, item, start, end, 1)
        if found >= 0:
            return found
        if py_err_occurred() != 0:
            return -1
    elif length > 0:
        items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
        i: int = start
        while i < end:
            v = pcc_gc_load_ptr(lst, ptr_add(items, i * 8))
            equal: int = py_obj_eq(v, item)
            if py_err_occurred() != 0:
                return -1
            if equal != 0:
                return i
            i = i + 1
    py_raise_owned(py_exc_new(2, cstr("list.index(x): x not in list")))
    return -1


@c_abi_export("py_list_count")
def py_list_count(lst, item) -> int:
    if not _list_is_sane(lst, -124):
        return 0
    if pcc_gc_backend() != 0:
        return _list_equality_scan(lst, item, 0, -1, 2)
    length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
    items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
    total: int = 0
    i: int = 0
    while i < length:
        v = pcc_gc_load_ptr(lst, ptr_add(items, i * 8))
        equal: int = py_obj_eq(v, item)
        if py_err_occurred() != 0:
            return 0
        if equal != 0:
            total = total + 1
        i = i + 1
    return total


@c_abi_export("py_list_reverse")
def py_list_reverse(lst) -> None:
    if not _list_is_sane(lst, -125):
        return
    if pcc_gc_backend() == 0:
        fast_length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
        fast_items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
        fast_i: int = 0
        fast_j: int = fast_length - 1
        while fast_i < fast_j:
            fast_left = load_ptr(fast_items, fast_i * 8)
            fast_right = load_ptr(fast_items, fast_j * 8)
            py_incref(fast_left)
            py_incref(fast_right)
            pcc_gc_store_ptr(
                lst, ptr_add(fast_items, fast_i * 8), fast_right
            )
            pcc_gc_store_ptr(
                lst, ptr_add(fast_items, fast_j * 8), fast_left
            )
            py_decref(fast_left)
            py_decref(fast_right)
            fast_i = fast_i + 1
            fast_j = fast_j - 1
        return
    list_slot = stack_alloc(8)
    list_handle_slot = stack_alloc(8)
    store_ptr(list_slot, 0, lst)
    if _prepare_moving_root(list_slot, list_handle_slot) != 0:
        return
    lst = _reload_moving_root(list_slot, list_handle_slot)
    length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
    i: int = 0
    j: int = length - 1
    retain_plans = stack_alloc(112)
    store_plans = stack_alloc(256)
    while i < j:
        backend: int = pcc_gc_backend()
        pcc_gc_store_ptr_plan_init(
            store_plans, load_ptr(list_slot, 0), backend
        )
        pcc_gc_store_ptr_plan_init(
            ptr_add(store_plans, 128), load_ptr(list_slot, 0), backend
        )
        pcc_py_gc_minor_graph_lock()
        lst = _reload_moving_root(list_slot, list_handle_slot)
        items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
        left = pcc_gc_load_ptr(lst, ptr_add(items, i * 8))
        right = pcc_gc_load_ptr(lst, ptr_add(items, j * 8))
        left = pcc_gc_retain_plan_prepare_locked(retain_plans, left)
        right = pcc_gc_retain_plan_prepare_locked(
            ptr_add(retain_plans, 56), right
        )
        pcc_gc_store_ptr_plan_commit_locked(
            store_plans, lst, ptr_add(items, i * 8), right
        )
        pcc_gc_store_ptr_plan_commit_locked(
            ptr_add(store_plans, 128), lst, ptr_add(items, j * 8), left
        )
        pcc_py_gc_minor_graph_unlock()
        pcc_gc_store_ptr_plan_finish(store_plans)
        pcc_gc_store_ptr_plan_finish(ptr_add(store_plans, 128))
        pcc_gc_retain_plan_finish(retain_plans)
        pcc_gc_retain_plan_finish(ptr_add(retain_plans, 56))
        py_decref(left)
        py_decref(right)
        i = i + 1
        j = j - 1
    _finish_moving_root(list_handle_slot)
