"""Minimal pcc-Python port of weakref.ref runtime support."""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import (
    PYINSTANCEOBJECT_CLS_OFFSET,
    PYOBJECTHEADER_TYPE_TAG_OFFSET,
    PY_TYPE_INSTANCE,
    PY_TYPE_USER_CLASS_START,
    PY_TYPE_VALUEBOX,
    PY_TYPE_WEAKREF,
)

from pcc.extern import extern, c_abi_export, c_ptr, c_int32, c_int64, c_void
from pcc.unsafe import (
    calloc,
    cstr,
    free,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    is_tagged_int,
    load_i32,
    load_ptr,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    store_i32,
    store_i64,
    store_ptr,
)


py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_dict_del = extern("py_dict_del", (c_ptr, c_ptr), c_int64)
py_dict_get = extern("py_dict_get", (c_ptr, c_ptr), c_ptr)
py_dict_keys = extern("py_dict_keys", (c_ptr,), c_ptr)
py_dict_new = extern("py_dict_new", (), c_ptr)
py_dict_set = extern("py_dict_set", (c_ptr, c_ptr, c_ptr), c_void)
py_list_append = extern("py_list_append", (c_ptr, c_ptr), c_void)
py_list_get = extern("py_list_get", (c_ptr, c_int64), c_ptr)
py_list_len = extern("py_list_len", (c_ptr,), c_int64)
py_list_new = extern("py_list_new", (c_int64,), c_ptr)
py_list_pop = extern("py_list_pop", (c_ptr, c_int64), c_ptr)
py_list_set = extern("py_list_set", (c_ptr, c_int64, c_ptr), c_void)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_get = extern("py_tuple_get", (c_ptr, c_int64), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_obj_call = extern("py_obj_call", (c_ptr, c_ptr, c_ptr), c_ptr)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
pcc_diagnostics_runtime_log_event_code = extern("pcc_diagnostics_runtime_log_event_code", (c_int32, c_int32, c_int64, c_int64, c_ptr), c_void)
pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_ptr)
pcc_gc_publish_initialized = extern(
    "pcc_gc_publish_initialized", (c_ptr,), c_void
)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_note_relocation_read = extern(
    "pcc_gc_note_relocation_read", (c_ptr,), c_ptr,
)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_store_ptr = extern("pcc_gc_store_ptr", (c_ptr, c_ptr, c_ptr), c_void)
pcc_gc_free_object_memory = extern("pcc_gc_free_object_memory", (c_ptr,), c_void)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
pcc_capi_is_cext_type_tag = extern("pcc_capi_is_cext_type_tag", (c_int64,), c_int64)


def _weakref_target_is_base_object(target) -> int:
    tag: int = load_i32(target, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    if tag != PY_TYPE_INSTANCE and tag < PY_TYPE_USER_CLASS_START:
        return 0
    if tag == PY_TYPE_VALUEBOX or pcc_capi_is_cext_type_tag(tag) != 0:
        return 0
    # Compare exact classes in one graph transaction. Slots-only subclasses
    # and ordinary subclasses have their own weak-reference policy; this is
    # only the canonical builtin object's unconditional exclusion.
    pcc_py_gc_minor_graph_lock()
    current = pcc_gc_note_relocation_read(target)
    cls = pcc_gc_load_ptr(current, ptr_add(current, PYINSTANCEOBJECT_CLS_OFFSET))
    base = pcc_gc_load_ptr(null(), global_addr("py_object_root_cache"))
    result: int = 0
    if ptr_is_null(base) == 0:
        result = ptr_eq(cls, base)
    pcc_py_gc_minor_graph_unlock()
    return result


def _py_none():
    return global_load_ptr("py_None")


def _unlink(wr) -> None:
    prev = load_ptr(wr, 32)
    nxt = load_ptr(wr, 40)
    if ptr_eq(prev, wr) != 0:
        store_ptr(wr, 32, null())
        store_ptr(wr, 40, null())
        return
    if ptr_is_null(prev) != 0:
        if ptr_eq(global_load_ptr("py_weakref_head"), wr) == 0:
            store_ptr(wr, 40, null())
            return
    else:
        if ptr_eq(load_ptr(prev, 40), wr) == 0:
            store_ptr(wr, 32, null())
            store_ptr(wr, 40, null())
            return
    if ptr_is_null(prev) != 0:
        global_store_ptr("py_weakref_head", nxt)
    else:
        store_ptr(prev, 40, nxt)
    if ptr_is_null(nxt) == 0:
        store_ptr(nxt, 32, prev)
    store_ptr(wr, 32, null())
    store_ptr(wr, 40, null())


@c_abi_export("py_weakref_new")
def py_weakref_new(target, callback):
    if ptr_is_null(target) != 0:
        return null()
    if is_tagged_int(target) != 0:
        return null()
    # Value projections are identity-free; a ValueBox's lifetime is
    # unpredictable (every boxing makes a new box) — mirror CPython's
    # analogue (weakref.ref(3) -> TypeError). 200 == PY_TYPE_VALUEBOX,
    # 3 == PY_EXC_TYPEERROR. The compile-time diagnostic catches the
    # static form; this covers Dyn-path boxes.
    if load_i32(target, 8) == PY_TYPE_VALUEBOX:
        py_raise_owned(
            py_exc_new(
                3,
                cstr("cannot create weak reference to a valueclass payload"),
            )
        )
        return null()
    if _weakref_target_is_base_object(target) != 0:
        py_raise_owned(py_exc_new(3, cstr("cannot create weak reference to 'object' object")))
        return null()
    if ptr_eq(callback, _py_none()) != 0:
        callback = null()
    target = pcc_gc_note_relocation_read(target)

    wr = pcc_gc_alloc(48, PY_TYPE_WEAKREF, 0)
    if ptr_is_null(wr) != 0:
        return null()
    store_i64(wr, 0, 1)
    store_i32(wr, 8, PY_TYPE_WEAKREF)        # PY_TYPE_WEAKREF
    store_ptr(wr, 16, target)
    store_ptr(wr, 24, null())
    store_ptr(wr, 32, null())
    store_ptr(wr, 40, null())
    if ptr_is_null(callback) == 0:
        pcc_gc_store_ptr(wr, ptr_add(wr, 24), callback)

    head = global_load_ptr("py_weakref_head")
    store_ptr(wr, 40, head)
    if ptr_is_null(head) == 0:
        store_ptr(head, 32, wr)
    global_store_ptr("py_weakref_head", wr)
    pcc_diagnostics_runtime_log_event_code(4, 1, load_i32(target, 8), 0 if ptr_is_null(callback) != 0 else 1, target)
    pcc_gc_publish_initialized(wr)
    return wr


@c_abi_export("py_weakref_call")
def py_weakref_call(ref):
    if ptr_is_null(ref) != 0:
        return null()
    if is_tagged_int(ref) != 0:
        return null()
    if load_i32(ref, 8) != PY_TYPE_WEAKREF:
        return null()
    target = load_ptr(ref, 16)
    if ptr_is_null(target) != 0:
        none_obj = _py_none()
        py_incref(none_obj)
        return none_obj
    resolved = pcc_gc_note_relocation_read(target)
    if ptr_eq(resolved, target) == 0:
        store_ptr(ref, 16, resolved)
        target = resolved
    py_incref(target)
    return target


@c_abi_export("py_weak_value_dict_new")
def py_weak_value_dict_new():
    return py_dict_new()


@c_abi_export("py_weak_value_dict_set")
def py_weak_value_dict_set(d, key, value) -> int:
    if ptr_is_null(d) != 0:
        return -1
    if ptr_is_null(key) != 0:
        return -1
    if ptr_is_null(value) != 0:
        return -1
    wr = py_weakref_new(value, _py_none())
    if ptr_is_null(wr) != 0:
        return -1
    py_dict_set(d, key, wr)
    py_decref(wr)
    return 0


@c_abi_export("py_weak_value_dict_contains")
def py_weak_value_dict_contains(d, key) -> int:
    if ptr_is_null(d) != 0:
        return 0
    if ptr_is_null(key) != 0:
        return 0
    wr = py_dict_get(d, key)
    if ptr_is_null(wr) != 0:
        return 0
    target = py_weakref_call(wr)
    py_decref(wr)
    if ptr_is_null(target) != 0:
        return 0
    if ptr_eq(target, _py_none()) != 0:
        py_decref(target)
        py_dict_del(d, key)
        return 0
    py_decref(target)
    return 1


@c_abi_export("py_weak_value_dict_len")
def py_weak_value_dict_len(d) -> int:
    if ptr_is_null(d) != 0:
        return 0
    keys = py_dict_keys(d)
    if ptr_is_null(keys) != 0:
        return 0
    n: int = py_list_len(keys)
    i: int = 0
    count: int = 0
    while i < n:
        key = py_list_get(keys, i)
        if ptr_is_null(key) == 0:
            if py_weak_value_dict_contains(d, key) != 0:
                count += 1
            py_decref(key)
        i += 1
    py_decref(keys)
    return count


@c_abi_export("py_weak_key_dict_new")
def py_weak_key_dict_new():
    return py_list_new(0)


def _weak_key_entry_new(key, value):
    wr = py_weakref_new(key, _py_none())
    if ptr_is_null(wr) != 0:
        return null()
    entry = py_tuple_new(2)
    if ptr_is_null(entry) != 0:
        py_decref(wr)
        return null()
    py_tuple_set_item(entry, 0, wr)
    py_tuple_set_item(entry, 1, value)
    py_decref(wr)
    return entry


@c_abi_export("py_weak_key_dict_set")
def py_weak_key_dict_set(d, key, value) -> int:
    if ptr_is_null(d) != 0:
        return -1
    if ptr_is_null(key) != 0:
        return -1
    if ptr_is_null(value) != 0:
        return -1
    n: int = py_list_len(d)
    i: int = 0
    while i < n:
        entry = py_list_get(d, i)
        if ptr_is_null(entry) == 0:
            wr = py_tuple_get(entry, 0)
            live = py_weakref_call(wr)
            same: int = 0
            if ptr_is_null(live) == 0:
                if ptr_eq(live, _py_none()) == 0:
                    if ptr_eq(live, key) != 0:
                        same = 1
                py_decref(live)
            if ptr_is_null(wr) == 0:
                py_decref(wr)
            if same != 0:
                replacement = _weak_key_entry_new(key, value)
                if ptr_is_null(replacement) != 0:
                    py_decref(entry)
                    return -1
                py_list_set(d, i, replacement)
                py_decref(replacement)
                py_decref(entry)
                return 0
            py_decref(entry)
        i += 1
    entry2 = _weak_key_entry_new(key, value)
    if ptr_is_null(entry2) != 0:
        return -1
    py_list_append(d, entry2)
    py_decref(entry2)
    return 0


@c_abi_export("py_weak_key_dict_len")
def py_weak_key_dict_len(d) -> int:
    if ptr_is_null(d) != 0:
        return 0
    count: int = 0
    i: int = 0
    while i < py_list_len(d):
        entry = py_list_get(d, i)
        if ptr_is_null(entry) != 0:
            i += 1
        else:
            wr = py_tuple_get(entry, 0)
            live = py_weakref_call(wr)
            is_live: int = 0
            if ptr_is_null(live) == 0:
                if ptr_eq(live, _py_none()) == 0:
                    is_live = 1
                py_decref(live)
            if ptr_is_null(wr) == 0:
                py_decref(wr)
            if is_live != 0:
                count += 1
                i += 1
            else:
                popped = py_list_pop(d, i)
                if ptr_is_null(popped) == 0:
                    py_decref(popped)
            py_decref(entry)
    return count


@c_abi_export("py_weakref_invalidate")
def py_weakref_invalidate(target) -> None:
    if ptr_is_null(target) != 0:
        return
    if is_tagged_int(target) != 0:
        return
    wr = global_load_ptr("py_weakref_head")
    while ptr_is_null(wr) == 0:
        nxt = load_ptr(wr, 40)
        wr_target = load_ptr(wr, 16)
        resolved = pcc_gc_note_relocation_read(wr_target)
        if ptr_eq(wr_target, target) != 0 or ptr_eq(resolved, target) != 0:
            store_ptr(wr, 16, null())
            pcc_diagnostics_runtime_log_event_code(4, 2, 0, load_i32(target, 8), target)
            callback = pcc_gc_load_ptr(wr, ptr_add(wr, 24))
            if ptr_is_null(callback) == 0:
                args = py_tuple_new(1)
                if ptr_is_null(args) == 0:
                    pcc_diagnostics_runtime_log_event_code(4, 3, 0, 0, wr)
                    py_tuple_set_item(args, 0, wr)
                    # Weakref callbacks are an unraisable boundary.  The
                    # callback result is owned even though its value is
                    # ignored; py_obj_call first classifies silent NULL, then
                    # this boundary deliberately clears that exception.
                    result = py_obj_call(callback, args, _py_none())
                    if ptr_is_null(result) == 0:
                        py_decref(result)
                    py_decref(args)
                py_clear_exception()
        elif ptr_eq(resolved, wr_target) == 0:
            store_ptr(wr, 16, resolved)
        wr = nxt


@c_abi_export("py_dealloc_weakref")
def py_dealloc_weakref(ref) -> None:
    if ptr_is_null(ref) != 0:
        return
    _unlink(ref)
    pcc_diagnostics_runtime_log_event_code(4, 4, 0, 0, ref)
    callback = pcc_gc_load_ptr(ref, ptr_add(ref, 24))
    if ptr_is_null(callback) == 0:
        py_decref(callback)
    pcc_gc_free_object_memory(ref)
