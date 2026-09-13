"""User data-model protocol dispatch authored in pcc-Python.

This owns the production ABI mirrored by ``src/py_protocol.c``: user dunder
dispatch, generic floor/in-place operators, and inherited dict-subclass
storage/methods.  The C source remains a host-C oracle.
"""

__pcc_runtime_port__ = True

from pcc.py_runtime.py.py_abi_constants import (
    C_POINTER_SIZE,
    PYCLASSOBJECT_N_FIELDS_OFFSET,
    PYINSTANCEOBJECT_CLS_OFFSET,
    PYINSTANCEOBJECT_FIELDS_OFFSET,
    PYOBJECTHEADER_FLAGS_OFFSET,
    PY_TYPE_BOOL,
    PY_TYPE_FLOAT,
    PY_TYPE_FUNC,
    PY_TYPE_INSTANCE,
    PY_TYPE_INT,
    PY_TYPE_NONE,
    PY_TYPE_TUPLE,
    PY_TYPE_USER_CLASS_START,
)

from pcc.extern import c_abi_export, c_double, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    call_ptr1,
    call_ptr2,
    call_ptr3,
    cstr,
    define_thread_local_i32,
    global_addr,
    global_load_ptr,
    is_tagged_int,
    load_i8,
    load_i32,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    ptr_to_int,
    stack_alloc,
    store_i32,
    store_i64,
)


py_class_lookup = extern("py_class_lookup", (c_ptr, c_ptr), c_ptr)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_len = extern("py_tuple_len", (c_ptr,), c_int64)
py_tuple_get = extern("py_tuple_get", (c_ptr, c_int64), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_func_call = extern("py_func_call", (c_ptr, c_ptr), c_ptr)
py_func_new_named = extern("py_func_new_named", (c_ptr, c_ptr, c_ptr), c_ptr)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_exc_new_with_value = extern("py_exc_new_with_value", (c_int64, c_ptr), c_ptr)
py_runtime_error_if_unset = extern(
    "py_runtime_error_if_unset", (c_ptr, c_ptr), c_ptr
)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_obj_truthy = extern("py_obj_truthy", (c_ptr,), c_int64)
py_int_to_i64 = extern("py_int_to_i64", (c_ptr, c_ptr), c_int64)
py_int_value_i64 = extern("py_int_value_i64", (c_ptr,), c_int64)
py_int_cmp = extern("py_int_cmp", (c_ptr, c_ptr), c_int32)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_int_floordiv = extern("py_int_floordiv", (c_ptr, c_ptr), c_ptr)
py_float_to_f64 = extern("py_float_to_f64", (c_ptr,), c_double)
py_float_from_f64 = extern("py_float_from_f64", (c_double,), c_ptr)
floor_c = extern("floor", (c_double,), c_double)
py_obj_add = extern("py_obj_add", (c_ptr, c_ptr), c_ptr)
py_obj_sub = extern("py_obj_sub", (c_ptr, c_ptr), c_ptr)
py_obj_mul = extern("py_obj_mul", (c_ptr, c_ptr), c_ptr)
py_obj_truediv = extern("py_obj_truediv", (c_ptr, c_ptr), c_ptr)
py_obj_mod = extern("py_obj_mod", (c_ptr, c_ptr), c_ptr)
py_dict_new = extern("py_dict_new", (), c_ptr)
py_dict_len = extern("py_dict_len", (c_ptr,), c_int64)
py_dict_contains = extern("py_dict_contains", (c_ptr, c_ptr), c_int64)
py_dict_get = extern("py_dict_get", (c_ptr, c_ptr), c_ptr)
py_dict_get_default = extern("py_dict_get_default", (c_ptr, c_ptr, c_ptr), c_ptr)
py_dict_set = extern("py_dict_set", (c_ptr, c_ptr, c_ptr), c_void)
py_dict_del = extern("py_dict_del", (c_ptr, c_ptr), c_int64)
py_dict_keys = extern("py_dict_keys", (c_ptr,), c_ptr)
py_dict_values = extern("py_dict_values", (c_ptr,), c_ptr)
py_dict_items = extern("py_dict_items", (c_ptr,), c_ptr)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_store_ptr = extern("pcc_gc_store_ptr", (c_ptr, c_ptr, c_ptr), c_void)
pcc_capi_is_cext_type_tag = extern("pcc_capi_is_cext_type_tag", (c_int64,), c_int64)
pcc_capi_cext_inplace_number = extern(
    "pcc_capi_cext_inplace_number", (c_ptr, c_ptr, c_int64), c_ptr
)


define_thread_local_i32("pcc_py_protocol_eq_depth", 0)


def _type_of(obj) -> int:
    if is_tagged_int(obj) != 0:
        return PY_TYPE_INT
    return load_i32(obj, 8)


pcc_gc_pointer_is_managed = extern(
    "pcc_gc_pointer_is_managed", (c_ptr,), c_int64
)


def _ptr_can_have_header(obj) -> int:
    return pcc_gc_pointer_is_managed(obj)


def _is_user_instance(obj) -> int:
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return 0
    tag: int = _type_of(obj)
    # Dynamic C-extension tags occupy the same high numeric range as pcc user
    # classes but do not have the PyInstanceObject ``cls`` field consumed by
    # _lookup_dunder.
    if pcc_capi_is_cext_type_tag(tag) != 0:
        return 0
    if tag == PY_TYPE_INSTANCE or tag >= PY_TYPE_USER_CLASS_START:
        return 1
    return 0


def _instance_class(obj):
    if _is_user_instance(obj) == 0:
        return null()
    return pcc_gc_load_ptr(obj, ptr_add(obj, PYINSTANCEOBJECT_CLS_OFFSET))


def _lookup_dunder(obj, name):
    cls = _instance_class(obj)
    if ptr_is_null(cls) != 0:
        return null()
    return py_class_lookup(cls, name)


def _protocol_require_result(result, helper_name, message):
    if ptr_is_null(result) != 0:
        py_runtime_error_if_unset(helper_name, message)
    return result


def _call_unary(method, self_obj):
    # A missing method is the lookup sentinel consumed by the caller.  Once a
    # method was selected, every NULL is an error result.
    if ptr_is_null(method) != 0:
        return null()
    if _ptr_can_have_header(method) != 0 and _type_of(method) == PY_TYPE_FUNC:
        args = py_tuple_new(1)
        if ptr_is_null(args) != 0:
            return _protocol_require_result(
                null(),
                cstr("py_tuple_new"),
                cstr("user protocol argument tuple allocation failed"),
            )
        py_tuple_set_item(args, 0, self_obj)
        result = py_func_call(method, args)
        _protocol_require_result(
            result,
            cstr("user protocol call"),
            cstr("user protocol callback returned NULL without an exception"),
        )
        py_decref(args)
        return result
    return _protocol_require_result(
        call_ptr1(method, self_obj),
        cstr("user protocol call"),
        cstr("user protocol callback returned NULL without an exception"),
    )


def _call_binary(method, self_obj, arg):
    if ptr_is_null(method) != 0:
        return null()
    if _ptr_can_have_header(method) != 0 and _type_of(method) == PY_TYPE_FUNC:
        args = py_tuple_new(2)
        if ptr_is_null(args) != 0:
            return _protocol_require_result(
                null(),
                cstr("py_tuple_new"),
                cstr("user protocol argument tuple allocation failed"),
            )
        py_tuple_set_item(args, 0, self_obj)
        py_tuple_set_item(args, 1, arg)
        result = py_func_call(method, args)
        _protocol_require_result(
            result,
            cstr("user protocol call"),
            cstr("user protocol callback returned NULL without an exception"),
        )
        py_decref(args)
        return result
    return _protocol_require_result(
        call_ptr2(method, self_obj, arg),
        cstr("user protocol call"),
        cstr("user protocol callback returned NULL without an exception"),
    )


def _call_ternary(method, self_obj, a, b):
    if ptr_is_null(method) != 0:
        return null()
    if _ptr_can_have_header(method) != 0 and _type_of(method) == PY_TYPE_FUNC:
        args = py_tuple_new(3)
        if ptr_is_null(args) != 0:
            return _protocol_require_result(
                null(),
                cstr("py_tuple_new"),
                cstr("user protocol argument tuple allocation failed"),
            )
        py_tuple_set_item(args, 0, self_obj)
        py_tuple_set_item(args, 1, a)
        py_tuple_set_item(args, 2, b)
        result = py_func_call(method, args)
        _protocol_require_result(
            result,
            cstr("user protocol call"),
            cstr("user protocol callback returned NULL without an exception"),
        )
        py_decref(args)
        return result
    return _protocol_require_result(
        call_ptr3(method, self_obj, a, b),
        cstr("user protocol call"),
        cstr("user protocol callback returned NULL without an exception"),
    )


def _write_handled(handled, value: int) -> None:
    if ptr_is_null(handled) == 0:
        store_i64(handled, 0, value)


def _int_result_value(result, valid_nonnegative: int) -> int:
    overflow = stack_alloc(4)
    store_i32(overflow, 0, 0)
    value: int = py_int_to_i64(result, overflow)
    if load_i32(overflow, 0) != 0:
        return 0
    if valid_nonnegative != 0 and value < 0:
        return 0
    return value


def _class_is_dict_subclass(obj) -> int:
    cls = _instance_class(obj)
    if ptr_is_null(cls) != 0:
        return 0
    if (load_i32(cls, PYOBJECTHEADER_FLAGS_OFFSET) & 4) != 0:
        return 1
    return 0


def _dict_subclass_env(obj, create: int):
    if _class_is_dict_subclass(obj) == 0:
        return null()
    cls = _instance_class(obj)
    if (
        ptr_is_null(cls) != 0
        or (load_i32(cls, PYOBJECTHEADER_FLAGS_OFFSET) & 2) != 0
    ):
        return null()
    n_fields: int = load_i32(cls, PYCLASSOBJECT_N_FIELDS_OFFSET)
    if n_fields < 0:
        n_fields = 0
    slot = ptr_add(
        obj,
        PYINSTANCEOBJECT_FIELDS_OFFSET + n_fields * C_POINTER_SIZE,
    )
    env = pcc_gc_load_ptr(obj, slot)
    if ptr_is_null(env) != 0 and create != 0:
        env = py_dict_new()
        if ptr_is_null(env) != 0:
            return null()
        pcc_gc_store_ptr(obj, slot, env)
        py_decref(env)
        env = pcc_gc_load_ptr(obj, slot)
    return env


def _dict_items_key():
    return py_str_new(cstr("\x00pcc.dict.items"), 15)


def _dict_subclass_backing(obj, create: int):
    env = _dict_subclass_env(obj, create)
    if ptr_is_null(env) != 0:
        return null()
    key = _dict_items_key()
    if ptr_is_null(key) != 0:
        return null()
    backing = py_dict_get(env, key)
    if ptr_is_null(backing) != 0 and create != 0:
        backing = py_dict_new()
        if ptr_is_null(backing) != 0:
            py_decref(key)
            return null()
        py_dict_set(env, key, backing)
        py_decref(key)
        py_decref(backing)
        return _dict_subclass_backing(obj, 0)
    py_decref(key)
    if ptr_is_null(backing) == 0:
        py_decref(backing)
    return backing


@c_abi_export("py_user_len_dispatch")
def py_user_len_dispatch(obj, handled) -> int:
    _write_handled(handled, 0)
    method = _lookup_dunder(obj, cstr("__len__"))
    if ptr_is_null(method) != 0:
        if _class_is_dict_subclass(obj) != 0:
            _write_handled(handled, 1)
            backing = _dict_subclass_backing(obj, 0)
            if ptr_is_null(backing) == 0:
                return py_dict_len(backing)
        return 0
    _write_handled(handled, 1)
    result = _call_unary(method, obj)
    if ptr_is_null(result) != 0:
        return 0
    value: int = _int_result_value(result, 1)
    py_decref(result)
    return value


@c_abi_export("py_user_abs_dispatch")
def py_user_abs_dispatch(obj):
    method = _lookup_dunder(obj, cstr("__abs__"))
    if ptr_is_null(method) != 0:
        return null()
    return _call_unary(method, obj)


@c_abi_export("py_user_bool_dispatch")
def py_user_bool_dispatch(obj, handled) -> int:
    _write_handled(handled, 0)
    method = _lookup_dunder(obj, cstr("__bool__"))
    if ptr_is_null(method) != 0:
        return 0
    _write_handled(handled, 1)
    result = _call_unary(method, obj)
    if ptr_is_null(result) != 0:
        return 0
    truth: int = py_obj_truthy(result)
    py_decref(result)
    if truth != 0:
        return 1
    return 0


@c_abi_export("py_obj_index_i64")
def py_obj_index_i64(obj) -> int:
    if ptr_is_null(obj) != 0:
        py_raise_owned(py_exc_new(3, cstr("object cannot be interpreted as an integer")))
        return 0
    if is_tagged_int(obj) != 0:
        return py_int_value_i64(obj)
    tag: int = _type_of(obj)
    if tag == PY_TYPE_INT:
        return py_int_value_i64(obj)
    if tag == PY_TYPE_BOOL:
        if ptr_eq(obj, global_load_ptr("py_True")) != 0:
            return 1
        return 0
    method = _lookup_dunder(obj, cstr("__index__"))
    if ptr_is_null(method) != 0:
        py_raise_owned(py_exc_new(3, cstr("object cannot be interpreted as an integer")))
        return 0
    result = _call_unary(method, obj)
    if ptr_is_null(result) != 0:
        return 0
    if is_tagged_int(result) != 0 or _type_of(result) == PY_TYPE_INT:
        overflow = stack_alloc(4)
        store_i32(overflow, 0, 0)
        value: int = py_int_to_i64(result, overflow)
        py_decref(result)
        if load_i32(overflow, 0) == 0:
            return value
    else:
        py_decref(result)
    py_raise_owned(py_exc_new(3, cstr("__index__ returned non-int")))
    return 0


@c_abi_export("py_index_i64_checked")
def py_index_i64_checked(obj) -> int:
    """Integer protocol for APIs whose platform index conversion must overflow."""
    if ptr_is_null(obj) != 0:
        py_raise_owned(py_exc_new(3, cstr("object cannot be interpreted as an integer")))
        return 0
    value = obj
    owned: int = 0
    tag: int = _type_of(obj)
    if tag == PY_TYPE_BOOL:
        if ptr_eq(obj, global_load_ptr("py_True")) != 0:
            return 1
        return 0
    if tag != PY_TYPE_INT:
        method = _lookup_dunder(obj, cstr("__index__"))
        if ptr_is_null(method) != 0:
            py_raise_owned(py_exc_new(3, cstr("object cannot be interpreted as an integer")))
            return 0
        value = _call_unary(method, obj)
        if ptr_is_null(value) != 0:
            return 0
        owned = 1
        if _type_of(value) != PY_TYPE_INT:
            py_decref(value)
            py_raise_owned(py_exc_new(3, cstr("__index__ returned non-int")))
            return 0
    overflow = stack_alloc(4)
    store_i32(overflow, 0, 0)
    result: int = py_int_to_i64(value, overflow)
    if owned != 0:
        py_decref(value)
    if load_i32(overflow, 0) != 0:
        py_raise_owned(py_exc_new(15, cstr("Python int too large to convert to platform index")))
        return 0
    return result


def _slice_integer_i64(value) -> int:
    overflow = stack_alloc(4)
    store_i32(overflow, 0, 0)
    result: int = py_int_to_i64(value, overflow)
    if load_i32(overflow, 0) == 0:
        return result
    zero = py_int_from_i64(0)
    sign: int = py_int_cmp(value, zero)
    py_decref(zero)
    if sign < 0:
        return -9223372036854775808
    return 9223372036854775807


@c_abi_export("py_slice_index_i64")
def py_slice_index_i64(obj, default: int) -> int:
    """Optional __index__ value, saturated to the platform index range."""
    if ptr_is_null(obj) != 0 or _type_of(obj) == PY_TYPE_NONE:
        return default
    if is_tagged_int(obj) != 0 or _type_of(obj) == PY_TYPE_INT:
        return _slice_integer_i64(obj)
    if _type_of(obj) == PY_TYPE_BOOL:
        if ptr_eq(obj, global_load_ptr("py_True")) != 0:
            return 1
        return 0
    method = _lookup_dunder(obj, cstr("__index__"))
    if ptr_is_null(method) != 0:
        py_raise_owned(py_exc_new(3, cstr("slice indices must be integers or None or have an __index__ method")))
        return 0
    result = _call_unary(method, obj)
    if ptr_is_null(result) != 0:
        return 0
    if is_tagged_int(result) != 0 or _type_of(result) == PY_TYPE_INT:
        value: int = _slice_integer_i64(result)
        py_decref(result)
        return value
    py_decref(result)
    py_raise_owned(py_exc_new(3, cstr("__index__ returned non-int")))
    return 0


@c_abi_export("py_user_contains_dispatch")
def py_user_contains_dispatch(obj, item, handled) -> int:
    _write_handled(handled, 0)
    method = _lookup_dunder(obj, cstr("__contains__"))
    if ptr_is_null(method) != 0:
        if _class_is_dict_subclass(obj) != 0:
            _write_handled(handled, 1)
            backing = _dict_subclass_backing(obj, 0)
            if ptr_is_null(backing) == 0 and py_dict_contains(backing, item) != 0:
                return 1
        return 0
    _write_handled(handled, 1)
    result = _call_binary(method, obj, item)
    if ptr_is_null(result) != 0:
        return 0
    truth: int = py_obj_truthy(result)
    py_decref(result)
    if truth != 0:
        return 1
    return 0


@c_abi_export("py_user_eq_dispatch")
def py_user_eq_dispatch(a, b) -> int:
    depth_addr = global_addr("pcc_py_protocol_eq_depth")
    depth: int = load_i32(depth_addr, 0)
    if depth >= 64:
        return -1
    method = _lookup_dunder(a, cstr("__eq__"))
    if ptr_is_null(method) == 0:
        store_i32(depth_addr, 0, depth + 1)
        result = _call_binary(method, a, b)
        store_i32(depth_addr, 0, depth)
        if ptr_is_null(result) != 0:
            return 0
        if ptr_eq(result, global_load_ptr("py_NotImplemented")) == 0:
            truth: int = py_obj_truthy(result)
            py_decref(result)
            if truth != 0:
                return 1
            return 0
        py_decref(result)
    if _type_of(a) == _type_of(b):
        return -1
    method = _lookup_dunder(b, cstr("__eq__"))
    if ptr_is_null(method) != 0:
        return -1
    store_i32(depth_addr, 0, depth + 1)
    result = _call_binary(method, b, a)
    store_i32(depth_addr, 0, depth)
    if ptr_is_null(result) != 0:
        return 0
    if ptr_eq(result, global_load_ptr("py_NotImplemented")) != 0:
        py_decref(result)
        return -1
    truth: int = py_obj_truthy(result)
    py_decref(result)
    if truth != 0:
        return 1
    return 0


@c_abi_export("py_user_getitem_dispatch")
def py_user_getitem_dispatch(obj, key):
    method = _lookup_dunder(obj, cstr("__getitem__"))
    if ptr_is_null(method) != 0:
        if _class_is_dict_subclass(obj) != 0:
            return py_dict_subclass_getitem(obj, key)
        return null()
    return _call_binary(method, obj, key)


@c_abi_export("py_user_matmul_dispatch")
def py_user_matmul_dispatch(a, b):
    method = _lookup_dunder(a, cstr("__matmul__"))
    if ptr_is_null(method) == 0:
        result = _call_binary(method, a, b)
        if ptr_eq(result, global_load_ptr("py_NotImplemented")) == 0:
            return result
        py_decref(result)
    method = _lookup_dunder(b, cstr("__rmatmul__"))
    if ptr_is_null(method) == 0:
        result = _call_binary(method, b, a)
        if ptr_eq(result, global_load_ptr("py_NotImplemented")) == 0:
            return result
        py_decref(result)
    py_raise_owned(py_exc_new(3, cstr("unsupported operand type(s) for @")))
    return null()


@c_abi_export("py_user_binop_dispatch")
def py_user_binop_dispatch(a, b, name, rname, type_err_msg):
    method = _lookup_dunder(a, name)
    if ptr_is_null(method) == 0:
        result = _call_binary(method, a, b)
        if ptr_eq(result, global_load_ptr("py_NotImplemented")) == 0:
            return result
        py_decref(result)
    method = _lookup_dunder(b, rname)
    if ptr_is_null(method) == 0:
        result = _call_binary(method, b, a)
        if ptr_eq(result, global_load_ptr("py_NotImplemented")) == 0:
            return result
        py_decref(result)
    py_raise_owned(py_exc_new(3, type_err_msg))
    return null()


@c_abi_export("py_obj_floordiv")
def py_obj_floordiv(a, b):
    if ptr_is_null(a) != 0 or ptr_is_null(b) != 0:
        py_raise_owned(py_exc_new(3, cstr("unsupported operand type(s) for //")))
        return null()
    at: int = _type_of(a)
    bt: int = _type_of(b)
    if (at == PY_TYPE_INT or at == PY_TYPE_BOOL) and (bt == PY_TYPE_INT or bt == PY_TYPE_BOOL):
        return py_int_floordiv(a, b)
    a_numeric: int = 1 if at == PY_TYPE_BOOL or at == PY_TYPE_INT or at == PY_TYPE_FLOAT else 0
    b_numeric: int = 1 if bt == PY_TYPE_BOOL or bt == PY_TYPE_INT or bt == PY_TYPE_FLOAT else 0
    if a_numeric != 0 and b_numeric != 0:
        divisor: float = py_float_to_f64(b)
        if divisor == 0.0:
            py_raise_owned(py_exc_new(9, cstr("float floor division by zero")))
            return null()
        return py_float_from_f64(floor_c(py_float_to_f64(a) / divisor))
    if (
        at == PY_TYPE_INSTANCE
        or at >= PY_TYPE_USER_CLASS_START
        or bt == PY_TYPE_INSTANCE
        or bt >= PY_TYPE_USER_CLASS_START
    ):
        return py_user_binop_dispatch(
            a,
            b,
            cstr("__floordiv__"),
            cstr("__rfloordiv__"),
            cstr("unsupported operand type(s) for //"),
        )
    py_raise_owned(py_exc_new(3, cstr("unsupported operand type(s) for //")))
    return null()


@c_abi_export("py_obj_inplace_op")
def py_obj_inplace_op(a, b, op_code: int):
    if (
        ptr_is_null(a) == 0
        and is_tagged_int(a) == 0
        and op_code >= 0
        and op_code < 6
    ):
        at: int = _type_of(a)
        if pcc_capi_is_cext_type_tag(at) != 0:
            result = pcc_capi_cext_inplace_number(a, b, op_code)
            if ptr_is_null(result) != 0:
                return null()
            if ptr_eq(result, global_load_ptr("py_NotImplemented")) == 0:
                return result
            py_decref(result)
        elif _is_user_instance(a) != 0:
            name = cstr("__iadd__")
            if op_code == 1:
                name = cstr("__isub__")
            elif op_code == 2:
                name = cstr("__imul__")
            elif op_code == 3:
                name = cstr("__itruediv__")
            elif op_code == 4:
                name = cstr("__ifloordiv__")
            elif op_code == 5:
                name = cstr("__imod__")
            method = _lookup_dunder(a, name)
            if ptr_is_null(method) == 0:
                result = _call_binary(method, a, b)
                if ptr_eq(result, global_load_ptr("py_NotImplemented")) == 0:
                    return result
                py_decref(result)
    if op_code == 0:
        return py_obj_add(a, b)
    if op_code == 1:
        return py_obj_sub(a, b)
    if op_code == 2:
        return py_obj_mul(a, b)
    if op_code == 3:
        return py_obj_truediv(a, b)
    if op_code == 4:
        return py_obj_floordiv(a, b)
    if op_code == 5:
        return py_obj_mod(a, b)
    py_raise_owned(py_exc_new(3, cstr("unsupported in-place operand")))
    return null()


@c_abi_export("py_user_setitem_dispatch")
def py_user_setitem_dispatch(obj, key, value, handled) -> int:
    _write_handled(handled, 0)
    method = _lookup_dunder(obj, cstr("__setitem__"))
    if ptr_is_null(method) != 0:
        if _class_is_dict_subclass(obj) != 0:
            backing = _dict_subclass_backing(obj, 1)
            if ptr_is_null(backing) != 0:
                return -1
            py_dict_set(backing, key, value)
            _write_handled(handled, 1)
            if py_err_occurred() != 0:
                return -1
            return 0
        return -1
    _write_handled(handled, 1)
    result = _call_ternary(method, obj, key, value)
    if ptr_is_null(result) != 0:
        return -1
    py_decref(result)
    return 0


@c_abi_export("py_user_delitem_dispatch")
def py_user_delitem_dispatch(obj, key, handled) -> int:
    _write_handled(handled, 0)
    method = _lookup_dunder(obj, cstr("__delitem__"))
    if ptr_is_null(method) != 0:
        if _class_is_dict_subclass(obj) != 0:
            backing = _dict_subclass_backing(obj, 0)
            _write_handled(handled, 1)
            if ptr_is_null(backing) != 0:
                py_raise_owned(py_exc_new_with_value(4, key))
                return -1
            status: int = py_dict_del(backing, key)
            if status < 0 and py_err_occurred() == 0:
                py_raise_owned(py_exc_new_with_value(4, key))
            return status
        return -1
    _write_handled(handled, 1)
    result = _call_binary(method, obj, key)
    if ptr_is_null(result) != 0:
        return -1
    py_decref(result)
    return 0


def _dictsub_nargs(args) -> int:
    if ptr_is_null(args) != 0 or is_tagged_int(args) != 0 or _type_of(args) != PY_TYPE_TUPLE:
        return 0
    return py_tuple_len(args)


def _dictsub_get_entry(captures, args):
    self_obj = py_tuple_get(captures, 0)
    if ptr_is_null(self_obj) != 0:
        return null()
    nargs: int = _dictsub_nargs(args)
    key = null()
    if nargs >= 1:
        key = py_tuple_get(args, 0)
    default = global_load_ptr("py_None")
    if nargs >= 2:
        default = py_tuple_get(args, 1)
    else:
        py_incref(default)
    backing = _dict_subclass_backing(self_obj, 0)
    if ptr_is_null(backing) != 0:
        out = default
        py_incref(out)
    else:
        out = py_dict_get_default(backing, key, default)
    py_decref(self_obj)
    if ptr_is_null(key) == 0:
        py_decref(key)
    py_decref(default)
    return out


def _dictsub_keys_entry(captures, args):
    self_obj = py_tuple_get(captures, 0)
    if ptr_is_null(self_obj) != 0:
        return null()
    backing = _dict_subclass_backing(self_obj, 1)
    out = null()
    if ptr_is_null(backing) == 0:
        out = py_dict_keys(backing)
    py_decref(self_obj)
    return out


def _dictsub_values_entry(captures, args):
    self_obj = py_tuple_get(captures, 0)
    if ptr_is_null(self_obj) != 0:
        return null()
    backing = _dict_subclass_backing(self_obj, 1)
    out = null()
    if ptr_is_null(backing) == 0:
        out = py_dict_values(backing)
    py_decref(self_obj)
    return out


def _dictsub_items_entry(captures, args):
    self_obj = py_tuple_get(captures, 0)
    if ptr_is_null(self_obj) != 0:
        return null()
    backing = _dict_subclass_backing(self_obj, 1)
    out = null()
    if ptr_is_null(backing) == 0:
        out = py_dict_items(backing)
    py_decref(self_obj)
    return out


def _dictsub_pop_entry(captures, args):
    self_obj = py_tuple_get(captures, 0)
    if ptr_is_null(self_obj) != 0:
        return null()
    nargs: int = _dictsub_nargs(args)
    key = null()
    if nargs >= 1:
        key = py_tuple_get(args, 0)
    backing = _dict_subclass_backing(self_obj, 0)
    existing = null()
    if ptr_is_null(backing) == 0 and ptr_is_null(key) == 0:
        existing = py_dict_get(backing, key)
    if ptr_is_null(existing) == 0:
        out = existing
        py_dict_del(backing, key)
    elif nargs >= 2:
        out = py_tuple_get(args, 1)
    else:
        py_raise_owned(py_exc_new(4, cstr("pop(): key not found")))
        out = null()
    py_decref(self_obj)
    if ptr_is_null(key) == 0:
        py_decref(key)
    return out


def _dictsub_setdefault_entry(captures, args):
    self_obj = py_tuple_get(captures, 0)
    if ptr_is_null(self_obj) != 0:
        return null()
    nargs: int = _dictsub_nargs(args)
    key = null()
    if nargs >= 1:
        key = py_tuple_get(args, 0)
    default = global_load_ptr("py_None")
    if nargs >= 2:
        default = py_tuple_get(args, 1)
    else:
        py_incref(default)
    backing = _dict_subclass_backing(self_obj, 1)
    out = null()
    if ptr_is_null(backing) == 0 and ptr_is_null(key) == 0:
        out = py_dict_get(backing, key)
        if ptr_is_null(out) != 0:
            py_dict_set(backing, key, default)
            out = default
            py_incref(out)
    py_decref(self_obj)
    if ptr_is_null(key) == 0:
        py_decref(key)
    py_decref(default)
    return out


def _dictsub_clear_entry(captures, args):
    self_obj = py_tuple_get(captures, 0)
    if ptr_is_null(self_obj) != 0:
        return null()
    env = _dict_subclass_env(self_obj, 0)
    if ptr_is_null(env) == 0:
        key = _dict_items_key()
        if ptr_is_null(key) == 0:
            fresh = py_dict_new()
            if ptr_is_null(fresh) == 0:
                py_dict_set(env, key, fresh)
                py_decref(fresh)
            py_decref(key)
    py_decref(self_obj)
    none = global_load_ptr("py_None")
    py_incref(none)
    return none


def _cstr_equal(value, literal) -> int:
    if ptr_is_null(value) != 0 or ptr_is_null(literal) != 0:
        return 0
    i: int = 0
    while True:
        a: int = load_i8(value, i) & 255
        b: int = load_i8(literal, i) & 255
        if a != b:
            return 0
        if a == 0:
            return 1
        i = i + 1
    return 0


@c_abi_export("py_dict_subclass_getattr")
def py_dict_subclass_getattr(obj, name):
    if _class_is_dict_subclass(obj) == 0 or ptr_is_null(name) != 0:
        return null()
    captures = py_tuple_new(1)
    if ptr_is_null(captures) != 0:
        return null()
    py_tuple_set_item(captures, 0, obj)
    fn = null()
    if _cstr_equal(name, cstr("get")) != 0:
        fn = py_func_new_named(_dictsub_get_entry, captures, name)
    elif _cstr_equal(name, cstr("keys")) != 0:
        fn = py_func_new_named(_dictsub_keys_entry, captures, name)
    elif _cstr_equal(name, cstr("values")) != 0:
        fn = py_func_new_named(_dictsub_values_entry, captures, name)
    elif _cstr_equal(name, cstr("items")) != 0:
        fn = py_func_new_named(_dictsub_items_entry, captures, name)
    elif _cstr_equal(name, cstr("pop")) != 0:
        fn = py_func_new_named(_dictsub_pop_entry, captures, name)
    elif _cstr_equal(name, cstr("setdefault")) != 0:
        fn = py_func_new_named(_dictsub_setdefault_entry, captures, name)
    elif _cstr_equal(name, cstr("clear")) != 0:
        fn = py_func_new_named(_dictsub_clear_entry, captures, name)
    py_decref(captures)
    return fn


@c_abi_export("py_dict_subclass_getitem")
def py_dict_subclass_getitem(obj, key):
    backing = _dict_subclass_backing(obj, 0)
    if ptr_is_null(backing) == 0 and ptr_is_null(key) == 0:
        value = py_dict_get(backing, key)
        if ptr_is_null(value) == 0:
            return value
    missing = _lookup_dunder(obj, cstr("__missing__"))
    if ptr_is_null(missing) == 0:
        return _call_binary(missing, obj, key)
    py_raise_owned(py_exc_new(4, cstr("key not found")))
    return null()
