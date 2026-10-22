"""No-libpython copy/deepcopy and process-local pickle protocol.

This preserves the intentionally narrow ABI of ``src/py_pickle_copy.c``.
The pickle payload is a process-local registry token, not CPython pickle wire
compatibility.  The C source remains a host-C oracle.
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import C_POINTER_SIZE, PYCLASSOBJECT_N_FIELDS_OFFSET, PYINSTANCEOBJECT_CLS_OFFSET, PYINSTANCEOBJECT_FIELDS_OFFSET, PY_TYPE_BOOL, PY_TYPE_BYTES, PY_TYPE_CLASS, PY_TYPE_DICT, PY_TYPE_FLOAT, PY_TYPE_FUNC, PY_TYPE_INSTANCE, PY_TYPE_INT, PY_TYPE_LIST, PY_TYPE_NONE, PY_TYPE_SET, PY_TYPE_STR, PY_TYPE_TUPLE, PY_TYPE_USER_CLASS_START

from pcc.extern import c_abi_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    call_ptr1,
    call_ptr2,
    call_ptr3,
    call_ptr4,
    cstr,
    define_global_i64,
    define_global_ptr_null,
    free,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    is_tagged_int,
    load_i8,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    memcpy,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    ptr_to_int,
    realloc,
    stack_alloc,
    store_i8,
    store_i64,
    store_ptr,
    untag_int,
)


py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_runtime_error_if_unset = extern(
    "py_runtime_error_if_unset", (c_ptr, c_ptr), c_ptr
)
py_list_new = extern("py_list_new", (c_int64,), c_ptr)
py_list_append = extern("py_list_append", (c_ptr, c_ptr), c_void)
py_list_len = extern("py_list_len", (c_ptr,), c_int64)
py_list_get = extern("py_list_get", (c_ptr, c_int64), c_ptr)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_len = extern("py_tuple_len", (c_ptr,), c_int64)
py_tuple_get = extern("py_tuple_get", (c_ptr, c_int64), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_dict_new = extern("py_dict_new", (), c_ptr)
py_dict_set = extern("py_dict_set", (c_ptr, c_ptr, c_ptr), c_void)
py_dict_entries_used = extern("py_dict_entries_used", (c_ptr,), c_int64)
py_dict_entry_key_at = extern("py_dict_entry_key_at", (c_ptr, c_int64), c_ptr)
py_dict_entry_value_at = extern("py_dict_entry_value_at", (c_ptr, c_int64), c_ptr)
py_set_new = extern("py_set_new", (), c_ptr)
py_set_add = extern("py_set_add", (c_ptr, c_ptr), c_void)
py_set_items = extern("py_set_items", (c_ptr,), c_ptr)
py_class_lookup = extern("py_class_lookup", (c_ptr, c_ptr), c_ptr)
py_instance_new = extern("py_instance_new", (c_ptr,), c_ptr)
py_instance_copy_builtin_payload = extern("py_instance_copy_builtin_payload", (c_ptr, c_ptr), c_void)
py_instance_get_field = extern("py_instance_get_field", (c_ptr, c_int32), c_ptr)
py_instance_set_field = extern("py_instance_set_field", (c_ptr, c_int32, c_ptr), c_void)
py_func_call = extern("py_func_call", (c_ptr, c_ptr), c_ptr)
py_obj_call = extern("py_obj_call", (c_ptr, c_ptr, c_ptr), c_ptr)
py_bytes_new = extern("py_bytes_new", (c_ptr, c_int64), c_ptr)
py_bytes_len = extern("py_bytes_len", (c_ptr,), c_int64)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_str_byte_len = extern("py_str_byte_len", (c_ptr,), c_int64)
pcc_gc_retain = extern("pcc_gc_retain", (c_ptr,), c_ptr)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_store_ptr = extern("pcc_gc_store_ptr", (c_ptr, c_ptr, c_ptr), c_void)


define_global_ptr_null("pcc_pickle_entries")
define_global_i64("pcc_pickle_next_id", 1)


def _type_of(obj) -> int:
    if is_tagged_int(obj) != 0:
        return PY_TYPE_INT
    return load_i32(obj, 8)


pcc_gc_pointer_is_managed = extern(
    "pcc_gc_pointer_is_managed", (c_ptr,), c_int64
)


def _ptr_can_have_header(obj) -> int:
    return pcc_gc_pointer_is_managed(obj)


def _is_heap_obj(obj) -> int:
    return _ptr_can_have_header(obj)


def _is_instance(obj) -> int:
    if _is_heap_obj(obj) == 0:
        return 0
    tag: int = _type_of(obj)
    if tag == PY_TYPE_INSTANCE or tag >= PY_TYPE_USER_CLASS_START:
        return 1
    return 0


def _instance_class(obj):
    if _is_instance(obj) == 0:
        return null()
    return pcc_gc_load_ptr(obj, ptr_add(obj, PYINSTANCEOBJECT_CLS_OFFSET))


def _dynamic_attr_slot(instance):
    if ptr_is_null(instance) != 0:
        return null()
    cls = _instance_class(instance)
    if ptr_is_null(cls) != 0:
        return null()
    n_fields: int = load_i32(cls, PYCLASSOBJECT_N_FIELDS_OFFSET)
    if n_fields < 0:
        n_fields = 0
    return ptr_add(
        instance,
        PYINSTANCEOBJECT_FIELDS_OFFSET + n_fields * C_POINTER_SIZE,
    )


def _lookup_method(obj, name):
    cls = _instance_class(obj)
    if ptr_is_null(cls) != 0:
        return null()
    return py_class_lookup(cls, name)


def _copy_require_result(result, helper_name, message):
    if ptr_is_null(result) != 0:
        py_runtime_error_if_unset(helper_name, message)
    return result


def _call_method_with_args(method, self_obj, args):
    if ptr_is_null(method) != 0:
        return null()
    n: int = 0
    if ptr_is_null(args) == 0:
        n = py_tuple_len(args)
    if _is_heap_obj(method) != 0 and _type_of(method) == PY_TYPE_FUNC:
        full_args = py_tuple_new(n + 1)
        if ptr_is_null(full_args) != 0:
            return _copy_require_result(
                null(),
                cstr("py_tuple_new"),
                cstr("copy callback argument tuple allocation failed"),
            )
        py_tuple_set_item(full_args, 0, self_obj)
        i: int = 0
        while i < n:
            item = py_tuple_get(args, i)
            if ptr_is_null(item) != 0:
                _copy_require_result(
                    null(),
                    cstr("py_tuple_get"),
                    cstr("copy callback argument lookup failed"),
                )
                py_decref(full_args)
                return null()
            py_tuple_set_item(full_args, i + 1, item)
            py_decref(item)
            i = i + 1
        result = py_func_call(method, full_args)
        _copy_require_result(
            result,
            cstr("copy_call_method_with_args"),
            cstr("copy callback returned NULL without setting an exception"),
        )
        py_decref(full_args)
        return result
    if n == 0:
        result = call_ptr1(method, self_obj)
        return _copy_require_result(
            result,
            cstr("copy_call_method_with_args"),
            cstr("copy callback returned NULL without setting an exception"),
        )
    a0 = py_tuple_get(args, 0)
    if ptr_is_null(a0) != 0:
        return _copy_require_result(
            null(),
            cstr("py_tuple_get"),
            cstr("copy callback argument lookup failed"),
        )
    if n == 1:
        result = call_ptr2(method, self_obj, a0)
        _copy_require_result(
            result,
            cstr("copy_call_method_with_args"),
            cstr("copy callback returned NULL without setting an exception"),
        )
        py_decref(a0)
        return result
    a1 = py_tuple_get(args, 1)
    if ptr_is_null(a1) != 0:
        _copy_require_result(
            null(),
            cstr("py_tuple_get"),
            cstr("copy callback argument lookup failed"),
        )
        py_decref(a0)
        return null()
    if n == 2:
        result = call_ptr3(method, self_obj, a0, a1)
        _copy_require_result(
            result,
            cstr("copy_call_method_with_args"),
            cstr("copy callback returned NULL without setting an exception"),
        )
        py_decref(a0)
        py_decref(a1)
        return result
    a2 = py_tuple_get(args, 2)
    if ptr_is_null(a2) != 0:
        _copy_require_result(
            null(),
            cstr("py_tuple_get"),
            cstr("copy callback argument lookup failed"),
        )
        py_decref(a0)
        py_decref(a1)
        return null()
    if n == 3:
        result = call_ptr4(method, self_obj, a0, a1, a2)
        _copy_require_result(
            result,
            cstr("copy_call_method_with_args"),
            cstr("copy callback returned NULL without setting an exception"),
        )
        py_decref(a0)
        py_decref(a1)
        py_decref(a2)
        return result
    py_decref(a0)
    py_decref(a1)
    py_decref(a2)
    py_raise_owned(py_exc_new(3, cstr("too many native method args")))
    return null()


def _call_unary(method, self_obj):
    return _call_method_with_args(method, self_obj, null())


def _call_binary(method, self_obj, arg):
    args = py_tuple_new(1)
    if ptr_is_null(args) != 0:
        return _copy_require_result(
            null(),
            cstr("py_tuple_new"),
            cstr("copy callback argument tuple allocation failed"),
        )
    py_tuple_set_item(args, 0, arg)
    result = _call_method_with_args(method, self_obj, args)
    py_decref(args)
    return result


# Memo layout: key pointer array, value pointer array, len, capacity.
def _memo_init(memo) -> None:
    store_ptr(memo, 0, null())
    store_ptr(memo, 8, null())
    store_i64(memo, 16, 0)
    store_i64(memo, 24, 0)


def _memo_dispose(memo) -> None:
    keys = load_ptr(memo, 0)
    values = load_ptr(memo, 8)
    if ptr_is_null(keys) == 0:
        free(keys)
    if ptr_is_null(values) == 0:
        free(values)
    _memo_init(memo)


def _memo_get(memo, key):
    if ptr_is_null(memo) != 0 or ptr_is_null(key) != 0:
        return null()
    keys = load_ptr(memo, 0)
    values = load_ptr(memo, 8)
    length: int = load_i64(memo, 16)
    i: int = 0
    while i < length:
        if ptr_eq(load_ptr(keys, i * 8), key) != 0:
            value = load_ptr(values, i * 8)
            py_incref(value)
            return value
        i = i + 1
    return null()


def _memo_set(memo, key, value) -> int:
    if ptr_is_null(memo) != 0 or ptr_is_null(key) != 0 or ptr_is_null(value) != 0:
        return -1
    keys = load_ptr(memo, 0)
    values = load_ptr(memo, 8)
    length: int = load_i64(memo, 16)
    i: int = 0
    while i < length:
        if ptr_eq(load_ptr(keys, i * 8), key) != 0:
            store_ptr(values, i * 8, value)
            return 0
        i = i + 1
    capacity: int = load_i64(memo, 24)
    if length >= capacity:
        new_capacity: int = 16
        if capacity > 0:
            new_capacity = capacity * 2
        new_keys = realloc(keys, new_capacity * 8)
        if ptr_is_null(new_keys) != 0:
            return -1
        store_ptr(memo, 0, new_keys)
        keys = new_keys
        new_values = realloc(values, new_capacity * 8)
        if ptr_is_null(new_values) != 0:
            return -1
        store_ptr(memo, 8, new_values)
        values = new_values
        store_i64(memo, 24, new_capacity)
    store_ptr(keys, length * 8, key)
    store_ptr(values, length * 8, value)
    store_i64(memo, 16, length + 1)
    return 0


def _copy_shallow_list(obj):
    length: int = py_list_len(obj)
    out = py_list_new(length)
    if ptr_is_null(out) != 0:
        return null()
    i: int = 0
    while i < length:
        item = py_list_get(obj, i)
        py_list_append(out, item)
        if ptr_is_null(item) == 0:
            py_decref(item)
        i = i + 1
    return out


def _copy_deep_list(obj, memo):
    length: int = py_list_len(obj)
    out = py_list_new(length)
    if ptr_is_null(out) != 0:
        return null()
    if _memo_set(memo, obj, out) != 0:
        py_decref(out)
        return null()
    i: int = 0
    while i < length:
        item = py_list_get(obj, i)
        item_copy = _copy_object_deep(item, memo)
        if ptr_is_null(item) == 0:
            py_decref(item)
        if ptr_is_null(item_copy) != 0:
            py_decref(out)
            return null()
        py_list_append(out, item_copy)
        py_decref(item_copy)
        i = i + 1
    return out


def _copy_shallow_tuple(obj):
    length: int = py_tuple_len(obj)
    out = py_tuple_new(length)
    if ptr_is_null(out) != 0:
        return null()
    i: int = 0
    while i < length:
        item = py_tuple_get(obj, i)
        py_tuple_set_item(out, i, item)
        if ptr_is_null(item) == 0:
            py_decref(item)
        i = i + 1
    return out


def _copy_deep_tuple(obj, memo):
    length: int = py_tuple_len(obj)
    out = py_tuple_new(length)
    if ptr_is_null(out) != 0:
        return null()
    if _memo_set(memo, obj, out) != 0:
        py_decref(out)
        return null()
    i: int = 0
    while i < length:
        item = py_tuple_get(obj, i)
        item_copy = _copy_object_deep(item, memo)
        if ptr_is_null(item) == 0:
            py_decref(item)
        if ptr_is_null(item_copy) != 0:
            py_decref(out)
            return null()
        py_tuple_set_item(out, i, item_copy)
        py_decref(item_copy)
        i = i + 1
    return out


def _copy_shallow_dict(obj):
    out = py_dict_new()
    if ptr_is_null(out) != 0:
        return null()
    entries_used: int = py_dict_entries_used(obj)
    i: int = 0
    while i < entries_used:
        key = py_dict_entry_key_at(obj, i)
        if ptr_is_null(key) == 0:
            value = py_dict_entry_value_at(obj, i)
            py_dict_set(out, key, value)
            py_decref(key)
            if ptr_is_null(value) == 0:
                py_decref(value)
        i = i + 1
    return out


def _copy_deep_dict(obj, memo):
    out = py_dict_new()
    if ptr_is_null(out) != 0:
        return null()
    if _memo_set(memo, obj, out) != 0:
        py_decref(out)
        return null()
    entries_used: int = py_dict_entries_used(obj)
    i: int = 0
    while i < entries_used:
        key = py_dict_entry_key_at(obj, i)
        if ptr_is_null(key) == 0:
            value = py_dict_entry_value_at(obj, i)
            key_copy = _copy_object_deep(key, memo)
            value_copy = _copy_object_deep(value, memo)
            py_decref(key)
            if ptr_is_null(value) == 0:
                py_decref(value)
            if ptr_is_null(key_copy) != 0 or ptr_is_null(value_copy) != 0:
                if ptr_is_null(key_copy) == 0:
                    py_decref(key_copy)
                if ptr_is_null(value_copy) == 0:
                    py_decref(value_copy)
                py_decref(out)
                return null()
            py_dict_set(out, key_copy, value_copy)
            py_decref(key_copy)
            py_decref(value_copy)
        i = i + 1
    return out


def _copy_set(obj, memo, deep: int):
    out = py_set_new()
    if ptr_is_null(out) != 0:
        return null()
    if deep != 0 and _memo_set(memo, obj, out) != 0:
        py_decref(out)
        return null()
    items = py_set_items(obj)
    if ptr_is_null(items) != 0:
        py_decref(out)
        return null()
    length: int = py_list_len(items)
    i: int = 0
    while i < length:
        item = py_list_get(items, i)
        value = item
        if deep != 0:
            value = _copy_object_deep(item, memo)
        if ptr_is_null(value) != 0:
            if ptr_is_null(item) == 0:
                py_decref(item)
            py_decref(items)
            py_decref(out)
            return null()
        py_set_add(out, value)
        if deep != 0:
            py_decref(value)
        py_decref(item)
        i = i + 1
    py_decref(items)
    return out


def _copy_instance(obj, memo, deep: int):
    cls = _instance_class(obj)
    if ptr_is_null(cls) != 0:
        return null()
    if deep == 0:
        copy_method = _lookup_method(obj, cstr("__copy__"))
        if ptr_is_null(copy_method) == 0:
            return _call_unary(copy_method, obj)
    else:
        deepcopy_method = _lookup_method(obj, cstr("__deepcopy__"))
        if ptr_is_null(deepcopy_method) == 0:
            return _call_binary(
                deepcopy_method, obj, global_load_ptr("py_None")
            )
    out = py_instance_new(cls)
    if ptr_is_null(out) != 0:
        return null()
    py_instance_copy_builtin_payload(obj, out)
    if deep != 0 and _memo_set(memo, obj, out) != 0:
        py_decref(out)
        return null()
    getstate = _lookup_method(obj, cstr("__getstate__"))
    setstate = _lookup_method(out, cstr("__setstate__"))
    if deep == 0 and ptr_is_null(getstate) == 0 and ptr_is_null(setstate) == 0:
        state = _call_unary(getstate, obj)
        if ptr_is_null(state) != 0:
            py_decref(out)
            return null()
        result = _call_binary(setstate, out, state)
        py_decref(state)
        if ptr_is_null(result) != 0 and py_err_occurred() != 0:
            py_decref(out)
            return null()
        if ptr_is_null(result) == 0:
            py_decref(result)
        return out
    n_fields: int = load_i32(cls, PYCLASSOBJECT_N_FIELDS_OFFSET)
    i: int = 0
    while i < n_fields:
        value = py_instance_get_field(obj, i)
        if ptr_is_null(value) == 0:
            copied = value
            if deep != 0:
                copied = _copy_object_deep(value, memo)
            if ptr_is_null(copied) != 0:
                py_decref(value)
                py_decref(out)
                return null()
            py_instance_set_field(out, i, copied)
            if deep != 0:
                py_decref(copied)
            py_decref(value)
        i = i + 1
    src_slot = _dynamic_attr_slot(obj)
    dst_slot = _dynamic_attr_slot(out)
    if ptr_is_null(src_slot) == 0 and ptr_is_null(dst_slot) == 0:
        dynamic_attrs = pcc_gc_load_ptr(obj, src_slot)
        if ptr_is_null(dynamic_attrs) == 0:
            dynamic_copy = _copy_shallow_dict(dynamic_attrs)
            if deep != 0:
                dynamic_copy = _copy_object_deep(dynamic_attrs, memo)
            if ptr_is_null(dynamic_copy) != 0:
                py_decref(out)
                return null()
            pcc_gc_store_ptr(out, dst_slot, dynamic_copy)
            py_decref(dynamic_copy)
    return out


def _copy_object_shallow(obj):
    if ptr_is_null(obj) != 0:
        return null()
    if _is_heap_obj(obj) == 0:
        py_incref(obj)
        return obj
    tag: int = _type_of(obj)
    if tag == PY_TYPE_LIST:
        return _copy_shallow_list(obj)
    if tag == PY_TYPE_TUPLE:
        return _copy_shallow_tuple(obj)
    if tag == PY_TYPE_DICT:
        return _copy_shallow_dict(obj)
    if tag == PY_TYPE_SET:
        return _copy_set(obj, null(), 0)
    if _is_instance(obj) != 0:
        return _copy_instance(obj, null(), 0)
    py_incref(obj)
    return obj


def _copy_object_deep(obj, memo):
    if ptr_is_null(obj) != 0:
        return null()
    if _is_heap_obj(obj) == 0:
        py_incref(obj)
        return obj
    tag: int = _type_of(obj)
    if tag == PY_TYPE_NONE or tag == PY_TYPE_BOOL or tag == PY_TYPE_INT or tag == PY_TYPE_FLOAT or tag == PY_TYPE_STR or tag == PY_TYPE_BYTES:
        py_incref(obj)
        return obj
    memo_value = _memo_get(memo, obj)
    if ptr_is_null(memo_value) == 0:
        return memo_value
    if tag == PY_TYPE_LIST:
        return _copy_deep_list(obj, memo)
    if tag == PY_TYPE_TUPLE:
        return _copy_deep_tuple(obj, memo)
    if tag == PY_TYPE_DICT:
        return _copy_deep_dict(obj, memo)
    if tag == PY_TYPE_SET:
        return _copy_set(obj, memo, 1)
    if _is_instance(obj) != 0:
        return _copy_instance(obj, memo, 1)
    py_incref(obj)
    return obj


@c_abi_export("py_copy_copy")
def py_copy_copy(obj):
    return _copy_object_shallow(obj)


@c_abi_export("py_copy_deepcopy")
def py_copy_deepcopy(obj):
    memo = stack_alloc(32)
    _memo_init(memo)
    result = _copy_object_deep(obj, memo)
    _memo_dispose(memo)
    return result


def _pickle_call_class(callable_obj, args):
    if _is_heap_obj(callable_obj) == 0 or _type_of(callable_obj) != PY_TYPE_CLASS:
        return py_obj_call(callable_obj, args, global_load_ptr("py_None"))
    out = py_instance_new(callable_obj)
    if ptr_is_null(out) != 0:
        return null()
    init_method = py_class_lookup(callable_obj, cstr("__init__"))
    if ptr_is_null(init_method) == 0:
        result = _call_method_with_args(init_method, out, args)
        if ptr_is_null(result) != 0 and py_err_occurred() != 0:
            py_decref(out)
            return null()
        if ptr_is_null(result) == 0:
            py_decref(result)
    return out


def _pickle_clone_from_reduce(obj):
    reduce_method = _lookup_method(obj, cstr("__reduce__"))
    if ptr_is_null(reduce_method) != 0:
        return null()
    reduced = _call_unary(reduce_method, obj)
    if ptr_is_null(reduced) != 0:
        return null()
    if _is_heap_obj(reduced) == 0 or _type_of(reduced) != PY_TYPE_TUPLE or py_tuple_len(reduced) < 2:
        py_decref(reduced)
        py_raise_owned(py_exc_new(3, cstr("invalid __reduce__ result")))
        return null()
    callable_obj = py_tuple_get(reduced, 0)
    args = py_tuple_get(reduced, 1)
    if ptr_is_null(args) != 0 or _is_heap_obj(args) == 0 or _type_of(args) != PY_TYPE_TUPLE:
        if ptr_is_null(callable_obj) == 0:
            py_decref(callable_obj)
        if ptr_is_null(args) == 0:
            py_decref(args)
        py_decref(reduced)
        py_raise_owned(py_exc_new(3, cstr("invalid __reduce__ args")))
        return null()
    out = _pickle_call_class(callable_obj, args)
    py_decref(callable_obj)
    py_decref(args)
    py_decref(reduced)
    return out


def _pickle_clone_payload(obj):
    reduced = _pickle_clone_from_reduce(obj)
    if ptr_is_null(reduced) == 0 or py_err_occurred() != 0:
        return reduced
    return py_copy_deepcopy(obj)


def _pickle_store_payload(payload) -> int:
    if ptr_is_null(payload) != 0:
        return 0
    node = malloc(24)
    if ptr_is_null(node) != 0:
        return 0
    identifier: int = load_i64(global_addr("pcc_pickle_next_id"), 0)
    store_i64(global_addr("pcc_pickle_next_id"), 0, identifier + 1)
    store_i64(node, 0, identifier)
    store_ptr(node, 8, payload)
    py_incref(payload)
    store_ptr(node, 16, global_load_ptr("pcc_pickle_entries"))
    global_store_ptr("pcc_pickle_entries", node)
    return identifier


def _pickle_find_payload(identifier: int):
    node = global_load_ptr("pcc_pickle_entries")
    while ptr_is_null(node) == 0:
        if load_i64(node, 0) == identifier:
            return node
        node = load_ptr(node, 16)
    return null()


def _write_decimal(buffer, value: int) -> int:
    reverse = stack_alloc(32)
    if value <= 0:
        store_i8(buffer, 0, 48)
        return 1
    n: int = 0
    while value > 0 and n < 32:
        digit: int = value % 10
        store_i8(reverse, n, 48 + digit)
        n = n + 1
        value = value // 10
    i: int = 0
    while i < n:
        store_i8(buffer, i, load_i8(reverse, n - 1 - i))
        i = i + 1
    return n


@c_abi_export("py_pickle_dumps")
def py_pickle_dumps(obj, protocol):
    payload = _pickle_clone_payload(obj)
    if ptr_is_null(payload) != 0:
        return null()
    identifier: int = _pickle_store_payload(payload)
    py_decref(payload)
    if identifier == 0:
        py_raise_owned(py_exc_new(7, cstr("pickle registry allocation failed")))
        return null()
    buffer = stack_alloc(80)
    memcpy(buffer, cstr("PCCPICKLE:"), 10)
    digits: int = _write_decimal(ptr_add(buffer, 10), identifier)
    return py_bytes_new(buffer, 10 + digits)


def _pickle_parse_id(data, id_out) -> int:
    if ptr_is_null(data) != 0 or ptr_is_null(id_out) != 0 or _is_heap_obj(data) == 0:
        return -1
    tag: int = _type_of(data)
    text = null()
    length: int = 0
    if tag == PY_TYPE_BYTES:
        text = ptr_add(data, 24)
        length = py_bytes_len(data)
    elif tag == PY_TYPE_STR:
        text = py_str_utf8(data)
        length = py_str_byte_len(data)
    else:
        return -1
    if length <= 10:
        return -1
    prefix = cstr("PCCPICKLE:")
    i: int = 0
    while i < 10:
        if (load_i8(text, i) & 255) != (load_i8(prefix, i) & 255):
            return -1
        i = i + 1
    identifier: int = 0
    i = 10
    while i < length:
        ch: int = load_i8(text, i) & 255
        if ch < 48 or ch > 57:
            return -1
        identifier = identifier * 10 + ch - 48
        i = i + 1
    store_i64(id_out, 0, identifier)
    return 0


@c_abi_export("py_pickle_loads")
def py_pickle_loads(data):
    id_out = stack_alloc(8)
    store_i64(id_out, 0, 0)
    if _pickle_parse_id(data, id_out) != 0:
        py_raise_owned(py_exc_new(2, cstr("unsupported pickle payload")))
        return null()
    node = _pickle_find_payload(load_i64(id_out, 0))
    if ptr_is_null(node) != 0 or ptr_is_null(load_ptr(node, 8)) != 0:
        py_raise_owned(py_exc_new(2, cstr("unknown pickle payload")))
        return null()
    return py_copy_deepcopy(load_ptr(node, 8))
