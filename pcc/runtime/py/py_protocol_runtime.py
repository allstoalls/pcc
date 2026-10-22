"""User data-model protocol dispatch authored in pcc-Python.

This owns the production ABI mirrored by ``src/py_protocol.c``: user dunder
dispatch, generic floor/in-place operators, and inherited dict-subclass
storage/methods.  The C source remains a host-C oracle.
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import (
    C_POINTER_SIZE,
    PYCLASSOBJECT_FIELD_NAMES_OFFSET,
    PYTUPLEOBJECT_ITEMS_OFFSET,
    PY_TYPE_DICT,
    PYCLASSOBJECT_N_FIELDS_OFFSET,
    PYINSTANCEOBJECT_CLS_OFFSET,
    PYINSTANCEOBJECT_FIELDS_OFFSET,
    PYOBJECTHEADER_FLAGS_OFFSET,
    PYCOMPLEXOBJECT_IMAG_OFFSET,
    PYCOMPLEXOBJECT_REAL_OFFSET,
    PY_TYPE_BOOL,
    PY_TYPE_BYTEARRAY,
    PY_TYPE_BYTES,
    PY_TYPE_COMPLEX,
    PY_TYPE_FLOAT,
    PY_TYPE_FUNC,
    PY_TYPE_INSTANCE,
    PY_TYPE_INT,
    PY_TYPE_NONE,
    PY_TYPE_STR,
    PY_TYPE_TUPLE,
    PY_TYPE_USER_CLASS_START,
)

from pcc.extern import (
    c_abi_export,
    c_double,
    c_int32,
    c_int64,
    c_ptr,
    c_void,
    extern,
)
from pcc.unsafe import (
    atomic_rmw_i32,
    call_ptr1,
    call_ptr2,
    call_ptr3,
    cstr,
    f64_div,
    f64_signbit,
    define_global_i32,
    define_thread_local_i32,
    global_addr,
    global_load_ptr,
    is_tagged_int,
    load_f64,
    load_i8,
    load_i32,
    load_i64,
    load_ptr,
    memset,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    ptr_to_int,
    stack_alloc,
    store_f64,
    store_i8,
    store_i32,
    store_i64,
    store_ptr,
)


py_class_lookup = extern("py_class_lookup", (c_ptr, c_ptr), c_ptr)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_len = extern("py_tuple_len", (c_ptr,), c_int64)
py_tuple_get = extern("py_tuple_get", (c_ptr, c_int64), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_func_call = extern("py_func_call", (c_ptr, c_ptr), c_ptr)
py_func_new_named = extern("py_func_new_named", (c_ptr, c_ptr, c_ptr), c_ptr)
py_dict_namespace_member = extern("py_dict_namespace_member", (c_ptr,), c_ptr)
py_instance_bind_method = extern("py_instance_bind_method", (c_ptr, c_ptr, c_ptr), c_ptr)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_exc_new_with_value = extern("py_exc_new_with_value", (c_int64, c_ptr), c_ptr)
py_raise_binary_type_error = extern("py_raise_binary_type_error", (c_ptr, c_ptr, c_ptr), c_ptr)
py_raise_type_named_error = extern("py_raise_type_named_error", (c_ptr, c_ptr, c_ptr), c_ptr)
py_raise_conversion_result_error = extern("py_raise_conversion_result_error", (c_ptr, c_ptr, c_ptr), c_ptr)
PyIndex_Check = extern("PyIndex_Check", (c_ptr,), c_int64)
py_runtime_error_if_unset = extern(
    "py_runtime_error_if_unset", (c_ptr, c_ptr), c_ptr
)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_tls_exc_swap_slot = extern("py_tls_exc_swap_slot", (c_ptr,), c_void)
pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
pcc_gc_foreign_lease_acquire = extern("pcc_gc_foreign_lease_acquire", (c_ptr,), c_int64)
pcc_gc_foreign_lease_release = extern("pcc_gc_foreign_lease_release", (c_ptr, c_int64), c_int64)
pcc_gc_root_copy_borrowed_lease = extern("pcc_gc_root_copy_borrowed_lease", (c_ptr, c_ptr), c_int64)
pcc_gc_note_slot_write_barrier = extern("pcc_gc_note_slot_write_barrier", (c_ptr, c_ptr, c_ptr), c_void)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)
py_obj_truthy = extern("py_obj_truthy", (c_ptr,), c_int64)
py_int_to_i64 = extern("py_int_to_i64", (c_ptr, c_ptr), c_int64)
py_int_value_i64 = extern("py_int_value_i64", (c_ptr,), c_int64)
py_int_cmp = extern("py_int_cmp", (c_ptr, c_ptr), c_int32)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_int_floordiv = extern("py_int_floordiv", (c_ptr, c_ptr), c_ptr)
py_int_mod = extern("py_int_mod", (c_ptr, c_ptr), c_ptr)
py_int_neg = extern("py_int_neg", (c_ptr,), c_ptr)
py_int_add = extern("py_int_add", (c_ptr, c_ptr), c_ptr)
py_int_sub = extern("py_int_sub", (c_ptr, c_ptr), c_ptr)
py_int_and = extern("py_int_and", (c_ptr, c_ptr), c_ptr)
py_int_pow = extern("py_int_pow", (c_ptr, c_ptr), c_ptr)
py_int_from_f64_exact = extern("py_int_from_f64_exact", (c_double,), c_ptr)
py_float_round_ndigits = extern("py_float_round_ndigits", (c_double, c_int64), c_ptr)
py_obj_iter = extern("py_obj_iter", (c_ptr,), c_ptr)
py_obj_next = extern("py_obj_next", (c_ptr,), c_ptr)
py_exc_matches = extern("py_exc_matches", (c_ptr, c_ptr), c_int64)
py_current_exception = extern("py_current_exception", (), c_ptr)
py_exc_builtin_class = extern("py_exc_builtin_class", (c_int64,), c_ptr)
py_complex_new = extern("py_complex_new", (c_double, c_double), c_ptr)
py_str_check = extern("py_str_check", (c_ptr,), c_int64)
rint_c = extern("rint", (c_double,), c_double)
py_int_xor = extern("py_int_xor", (c_ptr, c_ptr), c_ptr)
py_complex_neg = extern("py_complex_neg", (c_ptr,), c_ptr)
py_obj_type_name = extern("py_obj_type_name", (c_ptr,), c_ptr)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_str_byte_len = extern("py_str_byte_len", (c_ptr,), c_int64)
py_float_to_f64 = extern("py_float_to_f64", (c_ptr,), c_double)
py_float_from_f64 = extern("py_float_from_f64", (c_double,), c_ptr)
floor_c = extern("floor", (c_double,), c_double)
fmod_c = extern("fmod", (c_double, c_double), c_double)
pcc_numeric_float_from_bits = extern("pcc_numeric_float_from_bits", (c_int64,), c_double)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)
pcc_gc_take_pinned_slot = extern("pcc_gc_take_pinned_slot", (c_ptr, c_int64), c_ptr)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
pcc_gc_scheduler_root_register_handle = extern("pcc_gc_scheduler_root_register_handle", (c_ptr,), c_ptr)
pcc_gc_scheduler_root_unregister_handle = extern("pcc_gc_scheduler_root_unregister_handle", (c_ptr,), c_void)
pcc_capi_cext_binary_number = extern("pcc_capi_cext_binary_number", (c_ptr, c_ptr, c_int64), c_ptr)
py_obj_add = extern("py_obj_add", (c_ptr, c_ptr), c_ptr)
py_obj_sub = extern("py_obj_sub", (c_ptr, c_ptr), c_ptr)
py_obj_mul = extern("py_obj_mul", (c_ptr, c_ptr), c_ptr)
py_obj_truediv = extern("py_obj_truediv", (c_ptr, c_ptr), c_ptr)
py_obj_mod = extern("py_obj_mod", (c_ptr, c_ptr), c_ptr)
py_obj_issubclass = extern("py_obj_issubclass", (c_ptr, c_ptr), c_int64)
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
PyNumber_Index = extern("PyNumber_Index", (c_ptr,), c_ptr)
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


# Match the managed binary callback transaction below. py_func_call borrows
# callable/args and returns one NEW result owner, including aliases. Publish
# that result before tuple/input disposal can run finalizers or move objects.
# Raw C method addresses keep the separate call_ptr1 route.
_PROTOCOL_UNARY_METHOD = 0
_PROTOCOL_UNARY_SELF = 1
_PROTOCOL_UNARY_ARGS = 2
_PROTOCOL_UNARY_RESULT = 3
_PROTOCOL_UNARY_ERROR = 4
_PROTOCOL_UNARY_SLOT_COUNT = 5
_PROTOCOL_UNARY_BORROWED_COUNT = 2

define_global_i32("pcc_protocol_unary_borrowed_map", -2)
define_global_i32("pcc_protocol_unary_owned_map", 5)


def _protocol_unary_adopt(slots: c_ptr, tokens: c_ptr, index: int) -> int:
    # Every NEW producer has already stored into its registered owning slot.
    offset: int = index * C_POINTER_SIZE
    slot = ptr_add(slots, offset)
    token: int = pcc_gc_foreign_lease_acquire(slot)
    if token < 0:
        py_runtime_error_if_unset(cstr("user protocol call"), cstr("unary result owner lease failed"))
        return -1
    store_i64(tokens, offset, token)
    pcc_py_gc_minor_graph_lock()
    pcc_gc_note_slot_write_barrier(null(), slot, load_ptr(slot, 0))
    pcc_py_gc_minor_graph_unlock()
    return 0


def _protocol_unary_drop(slots: c_ptr, tokens: c_ptr, index: int) -> None:
    offset: int = index * C_POINTER_SIZE
    slot = ptr_add(slots, offset)
    if pcc_gc_foreign_lease_release(slot, load_i64(tokens, offset)) != 0:
        pcc_platform_abort()
        return
    store_i64(tokens, offset, 0)
    pcc_gc_store_root(slot, null())


def _protocol_unary_body(slots: c_ptr, tokens: c_ptr, borrowed: c_ptr) -> int:
    index: int = 0
    while index < _PROTOCOL_UNARY_BORROWED_COUNT:
        offset: int = index * C_POINTER_SIZE
        token: int = pcc_gc_root_copy_borrowed_lease(
            ptr_add(slots, offset), ptr_add(borrowed, offset),
        )
        if token < 0:
            py_runtime_error_if_unset(cstr("user protocol call"), cstr("unary input owner copy failed"))
            return -1
        store_i64(tokens, offset, token)
        index += 1
    store_ptr(slots, _PROTOCOL_UNARY_ARGS * C_POINTER_SIZE, py_tuple_new(1))
    if _protocol_unary_adopt(slots, tokens, _PROTOCOL_UNARY_ARGS) != 0:
        return -1
    if ptr_is_null(load_ptr(slots, _PROTOCOL_UNARY_ARGS * C_POINTER_SIZE)) != 0:
        _protocol_require_result(null(), cstr("py_tuple_new"), cstr("user protocol argument tuple allocation failed"))
        return -1
    py_tuple_set_item(
        load_ptr(slots, _PROTOCOL_UNARY_ARGS * C_POINTER_SIZE), 0,
        load_ptr(slots, _PROTOCOL_UNARY_SELF * C_POINTER_SIZE),
    )
    if py_err_occurred() != 0:
        return -1
    store_ptr(slots, _PROTOCOL_UNARY_RESULT * C_POINTER_SIZE, py_func_call(
        load_ptr(slots, _PROTOCOL_UNARY_METHOD * C_POINTER_SIZE),
        load_ptr(slots, _PROTOCOL_UNARY_ARGS * C_POINTER_SIZE),
    ))
    if _protocol_unary_adopt(slots, tokens, _PROTOCOL_UNARY_RESULT) != 0:
        return -1
    if ptr_is_null(load_ptr(slots, _PROTOCOL_UNARY_RESULT * C_POINTER_SIZE)) != 0:
        _protocol_require_result(null(), cstr("user protocol call"), cstr("user protocol callback returned NULL without an exception"))
        return -1
    return 0


def _protocol_unary_pin_result(slot: c_ptr) -> int:
    pcc_py_gc_minor_graph_lock()
    value = pcc_gc_load_ptr(null(), slot)
    prior: int = 0
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        prior = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & 64
        pcc_gc_pin(value)
    pcc_py_gc_minor_graph_unlock()
    return prior


def _call_unary_function(method, self_obj):
    # Precondition: the selected managed method has a live external owner and
    # a stable address through dispatch classification and this initial pin.
    # A borrowed method-table entry alone does not establish that contract;
    # lookup/replacement before this helper is a separate unresolved boundary.
    # self_obj is borrowed from a live, address-stable caller root.
    method_pin: int = load_i32(method, PYOBJECTHEADER_FLAGS_OFFSET) & 64
    pcc_gc_pin(method)
    borrowed = stack_alloc(_PROTOCOL_UNARY_BORROWED_COUNT * C_POINTER_SIZE)
    store_ptr(borrowed, _PROTOCOL_UNARY_METHOD * C_POINTER_SIZE, method)
    store_ptr(borrowed, _PROTOCOL_UNARY_SELF * C_POINTER_SIZE, self_obj)
    pcc_gc_frame_enter(global_addr("pcc_protocol_unary_borrowed_map"), borrowed)
    slots = stack_alloc(_PROTOCOL_UNARY_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_PROTOCOL_UNARY_SLOT_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _PROTOCOL_UNARY_SLOT_COUNT * C_POINTER_SIZE)
    memset(tokens, 0, _PROTOCOL_UNARY_SLOT_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_protocol_unary_owned_map"), slots)
    status: int = _protocol_unary_body(slots, tokens, borrowed)
    py_tls_exc_swap_slot(ptr_add(slots, _PROTOCOL_UNARY_ERROR * C_POINTER_SIZE))
    # The result has an independent counted owner even when it aliases method,
    # self, or the argument tuple. Restore the temporary input pin before
    # disposal; no unregistered raw result crosses this operation.
    method_slot = ptr_add(borrowed, _PROTOCOL_UNARY_METHOD * C_POINTER_SIZE)
    store_ptr(method_slot, 0, pcc_gc_take_pinned_slot(method_slot, method_pin))
    memset(borrowed, 0, _PROTOCOL_UNARY_BORROWED_COUNT * C_POINTER_SIZE)
    _protocol_unary_drop(slots, tokens, _PROTOCOL_UNARY_ARGS)
    _protocol_unary_drop(slots, tokens, _PROTOCOL_UNARY_SELF)
    _protocol_unary_drop(slots, tokens, _PROTOCOL_UNARY_METHOD)
    if status != 0:
        _protocol_unary_drop(slots, tokens, _PROTOCOL_UNARY_RESULT)
    py_clear_exception()
    py_tls_exc_swap_slot(ptr_add(slots, _PROTOCOL_UNARY_ERROR * C_POINTER_SIZE))
    result_slot = ptr_add(slots, _PROTOCOL_UNARY_RESULT * C_POINTER_SIZE)
    prior: int = _protocol_unary_pin_result(result_slot)
    if pcc_gc_foreign_lease_release(result_slot, load_i64(tokens, _PROTOCOL_UNARY_RESULT * C_POINTER_SIZE)) != 0:
        pcc_platform_abort()
        return null()
    pcc_gc_frame_leave(slots)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(result_slot, prior)


def _call_unary(method, self_obj):
    # A missing method is the lookup sentinel consumed by the caller. Once a
    # method was selected, every NULL is an error result.
    if ptr_is_null(method) != 0:
        return null()
    if _ptr_can_have_header(method) != 0 and _type_of(method) == PY_TYPE_FUNC:
        return _call_unary_function(method, self_obj)
    return _protocol_require_result(
        call_ptr1(method, self_obj),
        cstr("user protocol call"),
        cstr("user protocol callback returned NULL without an exception"),
    )


# Only the selected managed PyFunc branch enters these frames. Raw C method
# addresses keep the separate call_ptr2 route below.
_PROTOCOL_BINARY_METHOD = 0
_PROTOCOL_BINARY_SELF = 1
_PROTOCOL_BINARY_ARG = 2
_PROTOCOL_BINARY_ARGS = 3
_PROTOCOL_BINARY_RESULT = 4
_PROTOCOL_BINARY_ERROR = 5
_PROTOCOL_BINARY_SLOT_COUNT = 6
_PROTOCOL_BINARY_BORROWED_COUNT = 3

define_global_i32("pcc_protocol_binary_borrowed_map", -3)
define_global_i32("pcc_protocol_binary_owned_map", 6)


def _protocol_binary_adopt(slots: c_ptr, tokens: c_ptr, index: int) -> int:
    # Every NEW producer has already stored into its registered owning slot.
    offset: int = index * C_POINTER_SIZE
    slot = ptr_add(slots, offset)
    token: int = pcc_gc_foreign_lease_acquire(slot)
    if token < 0:
        py_runtime_error_if_unset(cstr("user protocol call"), cstr("binary result owner lease failed"))
        return -1
    store_i64(tokens, offset, token)
    pcc_py_gc_minor_graph_lock()
    pcc_gc_note_slot_write_barrier(null(), slot, load_ptr(slot, 0))
    pcc_py_gc_minor_graph_unlock()
    return 0


def _protocol_binary_drop(slots: c_ptr, tokens: c_ptr, index: int) -> None:
    offset: int = index * C_POINTER_SIZE
    slot = ptr_add(slots, offset)
    if pcc_gc_foreign_lease_release(slot, load_i64(tokens, offset)) != 0:
        pcc_platform_abort()
        return
    store_i64(tokens, offset, 0)
    pcc_gc_store_root(slot, null())


def _protocol_binary_body(slots: c_ptr, tokens: c_ptr, borrowed: c_ptr) -> int:
    index: int = 0
    while index < _PROTOCOL_BINARY_BORROWED_COUNT:
        offset: int = index * C_POINTER_SIZE
        token: int = pcc_gc_root_copy_borrowed_lease(
            ptr_add(slots, offset), ptr_add(borrowed, offset),
        )
        if token < 0:
            py_runtime_error_if_unset(cstr("user protocol call"), cstr("binary input owner copy failed"))
            return -1
        store_i64(tokens, offset, token)
        index += 1
    store_ptr(slots, _PROTOCOL_BINARY_ARGS * C_POINTER_SIZE, py_tuple_new(2))
    if _protocol_binary_adopt(slots, tokens, _PROTOCOL_BINARY_ARGS) != 0:
        return -1
    if ptr_is_null(load_ptr(slots, _PROTOCOL_BINARY_ARGS * C_POINTER_SIZE)) != 0:
        _protocol_require_result(null(), cstr("py_tuple_new"), cstr("user protocol argument tuple allocation failed"))
        return -1
    py_tuple_set_item(
        load_ptr(slots, _PROTOCOL_BINARY_ARGS * C_POINTER_SIZE), 0,
        load_ptr(slots, _PROTOCOL_BINARY_SELF * C_POINTER_SIZE),
    )
    if py_err_occurred() != 0:
        return -1
    py_tuple_set_item(
        load_ptr(slots, _PROTOCOL_BINARY_ARGS * C_POINTER_SIZE), 1,
        load_ptr(slots, _PROTOCOL_BINARY_ARG * C_POINTER_SIZE),
    )
    if py_err_occurred() != 0:
        return -1
    store_ptr(slots, _PROTOCOL_BINARY_RESULT * C_POINTER_SIZE, py_func_call(
        load_ptr(slots, _PROTOCOL_BINARY_METHOD * C_POINTER_SIZE),
        load_ptr(slots, _PROTOCOL_BINARY_ARGS * C_POINTER_SIZE),
    ))
    if _protocol_binary_adopt(slots, tokens, _PROTOCOL_BINARY_RESULT) != 0:
        return -1
    if ptr_is_null(load_ptr(slots, _PROTOCOL_BINARY_RESULT * C_POINTER_SIZE)) != 0:
        _protocol_require_result(null(), cstr("user protocol call"), cstr("user protocol callback returned NULL without an exception"))
        return -1
    return 0


def _protocol_binary_pin_result(slot: c_ptr) -> int:
    pcc_py_gc_minor_graph_lock()
    value = pcc_gc_load_ptr(null(), slot)
    prior: int = 0
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        prior = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & 64
        pcc_gc_pin(value)
    pcc_py_gc_minor_graph_unlock()
    return prior


def _call_binary_function(method, self_obj, arg):
    # Precondition: the selected managed method has a live external owner and
    # a stable address through dispatch classification and this initial pin.
    # A borrowed method-table entry alone does not establish that contract;
    # lookup/replacement before this helper is a separate unresolved boundary.
    # self_obj and arg are borrowed from live, address-stable caller roots.
    method_pin: int = load_i32(method, PYOBJECTHEADER_FLAGS_OFFSET) & 64
    pcc_gc_pin(method)
    borrowed = stack_alloc(_PROTOCOL_BINARY_BORROWED_COUNT * C_POINTER_SIZE)
    store_ptr(borrowed, _PROTOCOL_BINARY_METHOD * C_POINTER_SIZE, method)
    store_ptr(borrowed, _PROTOCOL_BINARY_SELF * C_POINTER_SIZE, self_obj)
    store_ptr(borrowed, _PROTOCOL_BINARY_ARG * C_POINTER_SIZE, arg)
    pcc_gc_frame_enter(global_addr("pcc_protocol_binary_borrowed_map"), borrowed)
    slots = stack_alloc(_PROTOCOL_BINARY_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_PROTOCOL_BINARY_SLOT_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _PROTOCOL_BINARY_SLOT_COUNT * C_POINTER_SIZE)
    memset(tokens, 0, _PROTOCOL_BINARY_SLOT_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_protocol_binary_owned_map"), slots)
    status: int = _protocol_binary_body(slots, tokens, borrowed)
    py_tls_exc_swap_slot(ptr_add(slots, _PROTOCOL_BINARY_ERROR * C_POINTER_SIZE))
    # The result has an independent counted owner even when it aliases method,
    # either operand, or the argument tuple. Restore the temporary input pin
    # before disposal; no unregistered raw result crosses this operation.
    method_slot = ptr_add(borrowed, _PROTOCOL_BINARY_METHOD * C_POINTER_SIZE)
    store_ptr(method_slot, 0, pcc_gc_take_pinned_slot(method_slot, method_pin))
    memset(borrowed, 0, _PROTOCOL_BINARY_BORROWED_COUNT * C_POINTER_SIZE)
    _protocol_binary_drop(slots, tokens, _PROTOCOL_BINARY_ARGS)
    _protocol_binary_drop(slots, tokens, _PROTOCOL_BINARY_ARG)
    _protocol_binary_drop(slots, tokens, _PROTOCOL_BINARY_SELF)
    _protocol_binary_drop(slots, tokens, _PROTOCOL_BINARY_METHOD)
    if status != 0:
        _protocol_binary_drop(slots, tokens, _PROTOCOL_BINARY_RESULT)
    py_clear_exception()
    py_tls_exc_swap_slot(ptr_add(slots, _PROTOCOL_BINARY_ERROR * C_POINTER_SIZE))
    result_slot = ptr_add(slots, _PROTOCOL_BINARY_RESULT * C_POINTER_SIZE)
    prior: int = _protocol_binary_pin_result(result_slot)
    if pcc_gc_foreign_lease_release(result_slot, load_i64(tokens, _PROTOCOL_BINARY_RESULT * C_POINTER_SIZE)) != 0:
        pcc_platform_abort()
        return null()
    pcc_gc_frame_leave(slots)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(result_slot, prior)


def _call_binary(method, self_obj, arg):
    if ptr_is_null(method) != 0:
        return null()
    if _ptr_can_have_header(method) != 0 and _type_of(method) == PY_TYPE_FUNC:
        return _call_binary_function(method, self_obj, arg)
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
    # Dict item backing uses the reserved hidden slot even for __slots__
    # subclasses. Ordinary dynamic attribute access still enforces its flag.
    if ptr_is_null(cls) != 0:
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
    selected = stack_alloc(8)
    scalar = stack_alloc(8)
    py_user_special_dispatch(obj, cstr("__len__"), null(), null(), 0, 1, scalar, selected)
    _write_handled(handled, load_i64(selected, 0))
    if load_i64(selected, 0) != 0:
        return load_i64(scalar, 0)
    if _class_is_dict_subclass(obj) != 0:
        _write_handled(handled, 1)
        backing = _dict_subclass_backing(obj, 0)
        if ptr_is_null(backing) == 0:
            return py_dict_len(backing)
    return 0


@c_abi_export("py_user_abs_dispatch")
def py_user_abs_dispatch(obj):
    return py_user_special_dispatch(obj, cstr("__abs__"), null(), null(), 0, 0, null(), null())


@c_abi_export("py_user_bool_dispatch")
def py_user_bool_dispatch(obj, handled) -> int:
    scalar = stack_alloc(8)
    py_user_special_dispatch(obj, cstr("__bool__"), null(), null(), 0, 3, scalar, handled)
    return load_i64(scalar, 0)


@c_abi_export("py_obj_index")
def py_obj_index(obj):
    """Return a new Python int via __index__, without a machine-width lane."""
    if ptr_is_null(obj) != 0:
        py_raise_owned(py_exc_new(3, cstr("object cannot be interpreted as an integer")))
        return null()
    tag: int = _type_of(obj)
    if tag == PY_TYPE_INT:
        py_incref(obj)
        return obj
    if tag == PY_TYPE_BOOL:
        if ptr_eq(obj, global_load_ptr("py_True")) != 0:
            return py_int_from_i64(1)
        return py_int_from_i64(0)
    if pcc_capi_is_cext_type_tag(tag) != 0:
        # The owned C-API number implementation owns extension nb_index slots.
        return PyNumber_Index(obj)
    method = _lookup_dunder(obj, cstr("__index__"))
    if ptr_is_null(method) != 0:
        return py_raise_type_named_error(cstr("'"), obj, cstr("' object cannot be interpreted as an integer"))
    result = _call_unary(method, obj)
    if ptr_is_null(result) != 0:
        return null()
    result_tag: int = _type_of(result)
    if result_tag == PY_TYPE_INT:
        return result
    if result_tag == PY_TYPE_BOOL:
        # CPython accepts this with a DeprecationWarning. The C-API warning
        # owner still ignores warnings, so do not silently discard that effect.
        py_decref(result)
        py_raise_owned(py_exc_new(11, cstr("pcc operator.index: non-exact __index__ results require owned warning emission")))
        return null()
    py_raise_conversion_result_error(obj, cstr(".__index__() must return an int, not "), result)
    py_decref(result)
    return null()


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
        py_raise_type_named_error(cstr("'"), obj, cstr("' object cannot be interpreted as an integer"))
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
        py_raise_owned(py_exc_new(15, cstr("Python int too large to convert to C ssize_t")))
        return 0
    py_raise_conversion_result_error(obj, cstr(".__index__() must return an int, not "), result)
    py_decref(result)
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
            py_raise_type_named_error(cstr("'"), obj, cstr("' object cannot be interpreted as an integer"))
            return 0
        value = _call_unary(method, obj)
        if ptr_is_null(value) != 0:
            return 0
        owned = 1
        if _type_of(value) != PY_TYPE_INT:
            py_raise_conversion_result_error(obj, cstr(".__index__() must return an int, not "), value)
            py_decref(value)
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
    py_raise_conversion_result_error(obj, cstr(".__index__() must return an int, not "), result)
    py_decref(result)
    return 0


@c_abi_export("py_user_contains_dispatch")
def py_user_contains_dispatch(obj, item, handled) -> int:
    selected = stack_alloc(8)
    scalar = stack_alloc(8)
    py_user_special_dispatch(obj, cstr("__contains__"), item, null(), 1, 2, scalar, selected)
    _write_handled(handled, load_i64(selected, 0))
    if load_i64(selected, 0) != 0:
        return load_i64(scalar, 0)
    if _class_is_dict_subclass(obj) != 0:
        _write_handled(handled, 1)
        backing = _dict_subclass_backing(obj, 0)
        if ptr_is_null(backing) == 0 and py_dict_contains(backing, item) != 0:
            return 1
    return 0


@c_abi_export("py_user_eq_dispatch")
def py_user_eq_dispatch(a, b) -> int:
    depth_addr = global_addr("pcc_py_protocol_eq_depth")
    depth: int = load_i32(depth_addr, 0)
    if depth >= 64:
        return -1
    selected = stack_alloc(8)
    scalar = stack_alloc(8)
    store_i32(depth_addr, 0, depth + 1)
    # Mode 2 tells two selected __eq__ methods that both returned
    # NotImplemented (-2: identity decides) from no method answering (-1: an
    # inherited builtin __eq__, which has no method entry, still applies).
    _named_binary(a, b, cstr("__eq__"), cstr("__eq__"), 2, 2, scalar, selected)
    store_i32(depth_addr, 0, depth)
    if load_i64(selected, 0) == 2:
        return -2
    if load_i64(selected, 0) == 0:
        return -1
    return load_i64(scalar, 0)


def _order_call(method, self_obj, other) -> int:
    if ptr_is_null(method) != 0:
        return -1
    result = _call_binary(method, self_obj, other)
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


@c_abi_export("py_user_order_dispatch")
def py_user_order_dispatch(a, b, op: int) -> int:
    name = cstr("__lt__")
    rname = cstr("__gt__")
    if op == 1:
        name = cstr("__le__")
        rname = cstr("__ge__")
    elif op == 4:
        name = cstr("__gt__")
        rname = cstr("__lt__")
    elif op == 5:
        name = cstr("__ge__")
        rname = cstr("__le__")
    selected = stack_alloc(8)
    scalar = stack_alloc(8)
    # Mode 2 keeps rich-comparison priority and distinguishes exhaustion of
    # two selected methods from an absent builtin ordering descriptor.
    _named_binary(a, b, name, rname, 2, 2, scalar, selected)
    if load_i64(selected, 0) == 2:
        return -2
    if load_i64(selected, 0) == 0:
        return -1
    return load_i64(scalar, 0)


@c_abi_export("py_user_getitem_dispatch")
def py_user_getitem_dispatch(obj, key):
    selected = stack_alloc(8)
    result = py_user_special_dispatch(obj, cstr("__getitem__"), key, null(), 1, 0, null(), selected)
    if load_i64(selected, 0) != 0:
        return result
    if _class_is_dict_subclass(obj) != 0:
        return py_dict_subclass_getitem(obj, key)
    return null()


@c_abi_export("py_user_matmul_dispatch")
def py_user_matmul_dispatch(a, b):
    return py_user_binop_dispatch(a, b, cstr("__matmul__"), cstr("__rmatmul__"), cstr("unsupported operand type(s) for @"))


def _unary_operand_error(value, op: int):
    # CPython: "bad operand type for unary -: 'str'".
    message = stack_alloc(160)
    prefix = cstr("bad operand type for unary ")
    length: int = 0
    while load_i8(prefix, length) != 0:
        store_i8(message, length, load_i8(prefix, length))
        length = length + 1
    store_i8(message, length, op)
    store_i8(message, length + 1, 58)
    store_i8(message, length + 2, 32)
    store_i8(message, length + 3, 39)
    length = length + 4
    name = py_obj_type_name(value)
    if ptr_is_null(name) == 0:
        text = py_str_utf8(name)
        count: int = py_str_byte_len(name)
        if count > 120:
            count = 120
        index: int = 0
        while index < count:
            store_i8(message, length, load_i8(text, index))
            length = length + 1
            index = index + 1
        py_decref(name)
    store_i8(message, length, 39)
    store_i8(message, length + 1, 0)
    py_raise_owned(py_exc_new(3, message))
    return null()


def _py_obj_unary(value, op: int):
    # ``-x`` / ``+x`` / ``~x`` (op '-', '+', '~') with CPython's dispatch.
    # The frontend used to lower a dynamically-typed operand through the i64
    # lane: the lane read a float object as 0, so ``-f`` on a dyn float was
    # the int 0 (pcc1's ``_bits_to_float64`` returned 0.0 for every negative
    # double).
    if ptr_is_null(value) != 0:
        return null()
    tag: int = _type_of(value)
    if tag == PY_TYPE_INT:
        if op == 45:
            return py_int_neg(value)
        if op == 126:
            minus_one = py_int_from_i64(-1)
            result = py_int_xor(value, minus_one)
            py_decref(minus_one)
            return result
        py_incref(value)
        return value
    if tag == PY_TYPE_BOOL:
        bit: int = 0
        if ptr_eq(value, global_load_ptr("py_True")) != 0:
            bit = 1
        if op == 45:
            return py_int_from_i64(0 - bit)
        if op == 126:
            return py_int_from_i64(-1 - bit)
        return py_int_from_i64(bit)
    if tag == PY_TYPE_FLOAT and op != 126:
        if op == 45:
            number: float = py_float_to_f64(value)
            return py_float_from_f64(-number)
        py_incref(value)
        return value
    if tag == PY_TYPE_COMPLEX and op != 126:
        if op == 45:
            return py_complex_neg(value)
        py_incref(value)
        return value
    if _is_user_instance(value) != 0:
        name = cstr("__invert__")
        if op == 45:
            name = cstr("__neg__")
        elif op == 43:
            name = cstr("__pos__")
        selected = stack_alloc(8)
        result = py_user_special_dispatch(value, name, null(), null(), 0, 0, null(), selected)
        if load_i64(selected, 0) != 0:
            return result
    return _unary_operand_error(value, op)


@c_abi_export("py_obj_neg")
def py_obj_neg(value):
    return _py_obj_unary(value, 45)


@c_abi_export("py_obj_pos")
def py_obj_pos(value):
    return _py_obj_unary(value, 43)


@c_abi_export("py_obj_invert")
def py_obj_invert(value):
    return _py_obj_unary(value, 126)


@c_abi_export("py_user_ternary_dispatch")
def py_user_ternary_dispatch(a, b, m, name, rname, handled):
    """slot_nb_power with a modulus: ``a.name(b, m)``, then ``b.rname(a, m)``,
    under the binary protocol's subclass-first rule. handled=0 (and a NULL
    result) when every selected method was absent or returned NotImplemented;
    a callback error keeps handled=1 with NULL."""
    return _named_dispatch(a, b, m, name, rname, 2, 0, 0, null(), handled)


@c_abi_export("py_user_binop_dispatch")
def py_user_binop_dispatch(a, b, name, rname, type_err_msg):
    selected = stack_alloc(8)
    result = _named_binary(a, b, name, rname, 0, 0, null(), selected)
    if load_i64(selected, 0) != 0:
        return result
    # type_err_msg is the operation text; CPython names both operand types.
    return py_raise_binary_type_error(type_err_msg, a, b)


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
            py_raise_owned(py_exc_new(9, cstr("division by zero")))
            return null()
        quotient = stack_alloc(16)
        _float_div_mod(py_float_to_f64(a), divisor, quotient)
        return py_float_from_f64(load_f64(quotient, 0))
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
    return py_raise_binary_type_error(cstr("unsupported operand type(s) for //"), a, b)


def _float_div_mod(av: float, bv: float, out) -> None:
    """CPython's _float_div_mod for a nonzero divisor: out[0] the floor
    quotient, out[1] the remainder with the divisor's sign. The quotient
    comes from the fmod remainder, so it is exact near multiples of the
    divisor (1.0 // 0.1 == 9.0, where floor(1.0 / 0.1) is 10.0), snapped to
    the nearest integral value, and a zero keeps the true quotient's sign."""
    rem: float = fmod_c(av, bv)
    div: float = f64_div(av - rem, bv)
    if rem != 0.0:
        if (bv < 0.0) != (rem < 0.0):
            rem = rem + bv
            div = div - 1.0
    elif f64_signbit(bv) != 0:
        rem = pcc_numeric_float_from_bits(-9223372036854775808)
    else:
        rem = 0.0
    quotient: float = 0.0
    if div != 0.0:
        quotient = floor_c(div)
        if div - quotient > 0.5:
            quotient = quotient + 1.0
    elif f64_signbit(f64_div(av, bv)) != 0:
        quotient = pcc_numeric_float_from_bits(-9223372036854775808)
    store_f64(out, 0, quotient)
    store_f64(out, 8, rem)


def _divmod_pack(q, r) -> c_ptr:
    out = py_tuple_new(2)
    if ptr_is_null(out) != 0:
        return _protocol_require_result(null(), cstr("divmod"), cstr("divmod result allocation failed"))
    pcc_gc_pin(out)
    py_tuple_set_item(out, 0, q)
    py_tuple_set_item(out, 1, r)
    pcc_gc_unpin(out)
    return out


def _divmod_numeric(a, b, at: int, bt: int) -> c_ptr:
    if at != PY_TYPE_FLOAT and bt != PY_TYPE_FLOAT:
        zero = py_int_from_i64(0)
        is_zero: int = py_int_cmp(b, zero)
        py_decref(zero)
        if is_zero == 0:
            py_raise_owned(py_exc_new(9, cstr("division by zero")))
            return null()
        q = py_int_floordiv(a, b)
        if ptr_is_null(q) != 0:
            return _protocol_require_result(null(), cstr("divmod"), cstr("integer quotient allocation failed"))
        pcc_gc_pin(q)
        r = py_int_mod(a, b)
        if ptr_is_null(r) != 0:
            pcc_gc_unpin(q)
            py_decref(q)
            return _protocol_require_result(null(), cstr("divmod"), cstr("integer remainder allocation failed"))
    else:
        av: float = py_float_to_f64(a)
        bv: float = py_float_to_f64(b)
        if bv == 0.0:
            py_raise_owned(py_exc_new(9, cstr("division by zero")))
            return null()
        parts = stack_alloc(16)
        _float_div_mod(av, bv, parts)
        q = py_float_from_f64(load_f64(parts, 0))
        pcc_gc_pin(q)
        r = py_float_from_f64(load_f64(parts, 8))
        if ptr_is_null(q) != 0 or ptr_is_null(r) != 0:
            pcc_gc_unpin(q)
            py_decref(q)
            py_decref(r)
            return _protocol_require_result(null(), cstr("divmod"), cstr("float divmod result allocation failed"))
    pcc_gc_pin(r)
    out = _divmod_pack(q, r)
    pcc_gc_pin(out)
    pcc_gc_unpin(r)
    py_decref(r)
    pcc_gc_unpin(q)
    py_decref(q)
    pcc_gc_unpin(out)
    return out


def _divmod_impl(a, b) -> c_ptr:
    at: int = _type_of(a)
    bt: int = _type_of(b)
    a_numeric: int = 1 if at == PY_TYPE_BOOL or at == PY_TYPE_INT or at == PY_TYPE_FLOAT else 0
    b_numeric: int = 1 if bt == PY_TYPE_BOOL or bt == PY_TYPE_INT or bt == PY_TYPE_FLOAT else 0
    if a_numeric != 0 and b_numeric != 0:
        return _divmod_numeric(a, b, at, bt)
    if pcc_capi_is_cext_type_tag(at) != 0 or pcc_capi_is_cext_type_tag(bt) != 0:
        result = pcc_capi_cext_binary_number(a, b, 4)
        if ptr_eq(result, global_load_ptr("py_NotImplemented")) == 0:
            return result
        py_decref(result)
    return py_user_binop_dispatch(
        a, b, cstr("__divmod__"), cstr("__rdivmod__"),
        cstr("unsupported operand type(s) for divmod()"),
    )


@c_abi_export("py_obj_divmod")
def py_obj_divmod(a, b) -> c_ptr:
    if ptr_is_null(a) != 0 or ptr_is_null(b) != 0:
        py_raise_owned(py_exc_new(3, cstr("unsupported operand type(s) for divmod()")))
        return null()
    # Runtime calls borrow operands. Hold updateable slots through callbacks;
    # a user dunder may mutate the owners from which either argument came.
    pins = stack_alloc(24)
    store_i64(pins, 0, 0)
    store_i64(pins, 8, 0)
    store_i64(pins, 16, 0)
    if _ptr_can_have_header(a) != 0:
        store_i64(pins, 0, load_i32(a, PYOBJECTHEADER_FLAGS_OFFSET) & 64)
    if _ptr_can_have_header(b) != 0:
        store_i64(pins, 8, load_i32(b, PYOBJECTHEADER_FLAGS_OFFSET) & 64)
    pcc_gc_pin(a)
    pcc_gc_pin(b)
    slots = stack_alloc(24)
    store_ptr(slots, 0, null())
    store_ptr(slots, 8, null())
    store_ptr(slots, 16, null())
    left_handle = pcc_gc_scheduler_root_register_handle(slots)
    right_handle = pcc_gc_scheduler_root_register_handle(ptr_add(slots, 8))
    result_handle = pcc_gc_scheduler_root_register_handle(ptr_add(slots, 16))
    pcc_gc_store_root(slots, a)
    pcc_gc_store_root(ptr_add(slots, 8), b)
    result = null()
    if ptr_is_null(left_handle) == 0 and ptr_is_null(right_handle) == 0 and ptr_is_null(result_handle) == 0:
        result = _divmod_impl(load_ptr(slots, 0), load_ptr(slots, 8))
        if _ptr_can_have_header(result) != 0:
            store_i64(pins, 16, load_i32(result, PYOBJECTHEADER_FLAGS_OFFSET) & 64)
        pcc_gc_pin(result)
        pcc_gc_store_root(ptr_add(slots, 16), result)
        py_decref(result)
    else:
        _protocol_require_result(null(), cstr("divmod"), cstr("divmod operand root registration failed"))
    prior_result_pin: int = load_i64(pins, 16)
    index: int = 2
    while index > 0:
        index = index - 1
        if ptr_eq(load_ptr(slots, 16), load_ptr(slots, index * C_POINTER_SIZE)) != 0:
            prior_result_pin = load_i64(pins, index * C_POINTER_SIZE)
    index = 2
    while index > 0:
        index = index - 1
        value = load_ptr(slots, index * C_POINTER_SIZE)
        pcc_gc_unpin(value)
        if load_i64(pins, index * C_POINTER_SIZE) != 0 and _ptr_can_have_header(value) != 0:
            atomic_rmw_i32("or", value, PYOBJECTHEADER_FLAGS_OFFSET, 64, "relaxed")
        pcc_gc_store_root(ptr_add(slots, index * C_POINTER_SIZE), null())
    pcc_gc_scheduler_root_unregister_handle(right_handle)
    pcc_gc_scheduler_root_unregister_handle(left_handle)
    result = load_ptr(slots, 16)
    if _ptr_can_have_header(result) != 0:
        atomic_rmw_i32("or", result, PYOBJECTHEADER_FLAGS_OFFSET, 64, "relaxed")
    pcc_gc_scheduler_root_unregister_handle(result_handle)
    return pcc_gc_take_pinned_slot(ptr_add(slots, 16), prior_result_pin)


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
            selected = stack_alloc(8)
            result = py_user_special_dispatch(a, name, b, null(), 1, 7, null(), selected)
            if load_i64(selected, 0) != 0:
                return result
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
    selected = stack_alloc(8)
    py_user_special_dispatch(obj, cstr("__setitem__"), key, value, 2, 5, null(), selected)
    _write_handled(handled, load_i64(selected, 0))
    if load_i64(selected, 0) != 0:
        if py_err_occurred() != 0:
            return -1
        return 0
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


@c_abi_export("py_user_delitem_dispatch")
def py_user_delitem_dispatch(obj, key, handled) -> int:
    selected = stack_alloc(8)
    py_user_special_dispatch(obj, cstr("__delitem__"), key, null(), 1, 5, null(), selected)
    _write_handled(handled, load_i64(selected, 0))
    if load_i64(selected, 0) != 0:
        if py_err_occurred() != 0:
            return -1
        return 0
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
    # dict's native members (py_class.py), bound to a dict subclass instance;
    # they act on its item storage. NULL when dict has no such member.
    if _class_is_dict_subclass(obj) == 0 or ptr_is_null(name) != 0:
        return null()
    member = py_dict_namespace_member(name)
    if ptr_is_null(member) != 0:
        return null()
    bound = py_instance_bind_method(member, obj, name)
    py_decref(member)
    return bound


@c_abi_export("py_dict_subclass_storage")
def py_dict_subclass_storage(obj, create: int):
    """A dict subclass instance's item storage (borrowed), or NULL."""
    return _dict_subclass_backing(obj, create)


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
    if py_err_occurred() == 0:
        py_raise_owned(py_exc_new_with_value(4, key))  # KeyError(key)
    return null()


# Builtin-base initialization starts from caller-owned slots. The hidden
# dictionary environment and its backing remain the generic storage owners.
pcc_gc_root_copy_lease = extern("pcc_gc_root_copy_lease", (c_ptr, c_ptr), c_int64)
pcc_gc_root_copy_borrowed_lease = extern("pcc_gc_root_copy_borrowed_lease", (c_ptr, c_ptr), c_int64)
pcc_gc_foreign_lease_acquire = extern("pcc_gc_foreign_lease_acquire", (c_ptr,), c_int64)
pcc_gc_foreign_lease_release = extern("pcc_gc_foreign_lease_release", (c_ptr, c_int64), c_int64)
pcc_gc_resolve_root_slot_unlocked = extern("pcc_gc_resolve_root_slot_unlocked", (c_ptr, c_int64), c_ptr)
pcc_gc_store_ptr_plan_init = extern("pcc_gc_store_ptr_plan_init", (c_ptr, c_ptr, c_int64), c_void)
pcc_gc_store_ptr_plan_commit_locked = extern("pcc_gc_store_ptr_plan_commit_locked", (c_ptr, c_ptr, c_ptr, c_ptr), c_int64)
pcc_gc_store_ptr_plan_finish = extern("pcc_gc_store_ptr_plan_finish", (c_ptr,), c_void)
pcc_gc_note_slot_write_barrier = extern("pcc_gc_note_slot_write_barrier", (c_ptr, c_ptr, c_ptr), c_void)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
pcc_gc_backend = extern("pcc_gc_backend", (), c_int64)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)
py_tls_exc_swap_slot = extern("py_tls_exc_swap_slot", (c_ptr,), c_void)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_dict_set_slots = extern("py_dict_set_slots", (c_ptr, c_ptr, c_ptr), c_int64)
py_dict_setdefault_slots = extern("py_dict_setdefault_slots", (c_ptr, c_ptr, c_ptr, c_ptr), c_int64)
py_dict_update_slots = extern("py_dict_update_slots", (c_ptr, c_ptr), c_int64)


# Scratch indices shared by the root, lease-token and registration arrays.
# Slot zero saves entry TLS; the final slot preserves an error during cleanup.
_BUILTIN_INIT_OLD_EXCEPTION_SLOT = 0
_BUILTIN_INIT_RECEIVER_SLOT = 1
_BUILTIN_INIT_FROM_CLASS_SLOT = 2
_BUILTIN_INIT_ARGS_SLOT = 3
_BUILTIN_INIT_KWARGS_SLOT = 4
_BUILTIN_INIT_CLASS_SLOT = 5
_BUILTIN_INIT_ENV_SLOT = 6
_BUILTIN_INIT_KEY_SLOT = 7
_BUILTIN_INIT_DICT_SLOT = 8
_BUILTIN_INIT_SOURCE_SLOT = 9
_BUILTIN_INIT_TEMP_SLOT = 10
_BUILTIN_INIT_ERROR_SLOT = 11
_BUILTIN_INIT_SLOT_COUNT = 12


def _builtin_init_error(message) -> int:
    py_runtime_error_if_unset(cstr("builtin-base initializer"), message)
    return -1


def _builtin_init_open(slots, tokens, handles) -> int:
    memset(slots, 0, _BUILTIN_INIT_SLOT_COUNT * C_POINTER_SIZE)
    memset(tokens, 0, _BUILTIN_INIT_SLOT_COUNT * C_POINTER_SIZE)
    memset(handles, 0, _BUILTIN_INIT_SLOT_COUNT * C_POINTER_SIZE)
    count: int = 0
    while count < _BUILTIN_INIT_SLOT_COUNT:
        handle = pcc_gc_scheduler_root_register_handle(ptr_add(slots, count * C_POINTER_SIZE))
        if ptr_is_null(handle) != 0:
            return count
        store_ptr(handles, count * C_POINTER_SIZE, handle)
        count = count + 1
    return count


def _builtin_init_copy(slots, tokens, index: int, source, borrowed: int) -> int:
    if ptr_is_null(source) != 0:
        return _builtin_init_error(cstr("initializer requires authoritative source slots"))
    token: int = 0
    if borrowed != 0:
        token = pcc_gc_root_copy_borrowed_lease(ptr_add(slots, index * C_POINTER_SIZE), source)
    else:
        token = pcc_gc_root_copy_lease(ptr_add(slots, index * C_POINTER_SIZE), source)
    if token < 0:
        return _builtin_init_error(cstr("initializer source transfer failed"))
    store_i64(tokens, index * C_POINTER_SIZE, token)
    return 0


def _builtin_init_adopt(slots, tokens, index: int) -> int:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    if ptr_is_null(load_ptr(slot, 0)) != 0:
        return _builtin_init_error(cstr("initializer allocation returned NULL without an exception"))
    token: int = pcc_gc_foreign_lease_acquire(slot)
    if token < 0:
        return _builtin_init_error(cstr("initializer result lease failed"))
    store_i64(tokens, index * C_POINTER_SIZE, token)
    pcc_py_gc_minor_graph_lock()
    pcc_gc_note_slot_write_barrier(null(), slot, load_ptr(slot, 0))
    pcc_py_gc_minor_graph_unlock()
    return -1 if py_err_occurred() != 0 else 0


def _builtin_init_drop(slots, tokens, index: int) -> None:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    if pcc_gc_foreign_lease_release(slot, load_i64(tokens, index * C_POINTER_SIZE)) != 0:
        pcc_platform_abort()
        return
    store_i64(tokens, index * C_POINTER_SIZE, 0)
    pcc_gc_store_root(slot, null())


def _builtin_init_close(slots, tokens, handles, count: int, suspended: int) -> None:
    if suspended != 0:
        py_tls_exc_swap_slot(ptr_add(slots, _BUILTIN_INIT_ERROR_SLOT * C_POINTER_SIZE))
    index: int = _BUILTIN_INIT_TEMP_SLOT
    while index > 0:
        _builtin_init_drop(slots, tokens, index)
        index = index - 1
    if suspended != 0:
        py_clear_exception()
        if ptr_is_null(load_ptr(slots, _BUILTIN_INIT_ERROR_SLOT * C_POINTER_SIZE)) == 0:
            pcc_gc_store_root(slots, null())
            py_clear_exception()
            py_tls_exc_swap_slot(ptr_add(slots, _BUILTIN_INIT_ERROR_SLOT * C_POINTER_SIZE))
        else:
            py_tls_exc_swap_slot(slots)
    index = 0
    while index < count:
        pcc_gc_scheduler_root_unregister_handle(load_ptr(handles, index * C_POINTER_SIZE))
        index = index + 1


def _builtin_init_env(slots, tokens) -> int:
    count: int = load_i32(load_ptr(slots, _BUILTIN_INIT_CLASS_SLOT * C_POINTER_SIZE), PYCLASSOBJECT_N_FIELDS_OFFSET)
    if count < 0:
        count = 0
    offset: int = PYINSTANCEOBJECT_FIELDS_OFFSET + count * C_POINTER_SIZE
    if _builtin_init_copy(slots, tokens, _BUILTIN_INIT_ENV_SLOT,
            ptr_add(load_ptr(slots, _BUILTIN_INIT_RECEIVER_SLOT * C_POINTER_SIZE), offset), 0) != 0:
        return -1
    if ptr_is_null(load_ptr(slots, _BUILTIN_INIT_ENV_SLOT * C_POINTER_SIZE)) == 0:
        return 0
    store_ptr(slots, _BUILTIN_INIT_TEMP_SLOT * C_POINTER_SIZE, py_dict_new())
    if _builtin_init_adopt(slots, tokens, _BUILTIN_INIT_TEMP_SLOT) != 0:
        return -1
    plan = stack_alloc(128)
    pcc_gc_store_ptr_plan_init(plan, load_ptr(slots, _BUILTIN_INIT_RECEIVER_SLOT * C_POINTER_SIZE), pcc_gc_backend())
    committed: int = 1
    pcc_py_gc_minor_graph_lock()
    receiver = load_ptr(slots, _BUILTIN_INIT_RECEIVER_SLOT * C_POINTER_SIZE)
    field = ptr_add(receiver, offset)
    # Allocation may reenter and create an environment. Never replace it.
    if ptr_is_null(pcc_gc_resolve_root_slot_unlocked(field, 0)) != 0:
        committed = pcc_gc_store_ptr_plan_commit_locked(plan, receiver, field,
            load_ptr(slots, _BUILTIN_INIT_TEMP_SLOT * C_POINTER_SIZE))
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_store_ptr_plan_finish(plan)
    _builtin_init_drop(slots, tokens, _BUILTIN_INIT_TEMP_SLOT)
    if committed == 0:
        return _builtin_init_error(cstr("initializer environment publication failed"))
    return _builtin_init_copy(slots, tokens, _BUILTIN_INIT_ENV_SLOT,
        ptr_add(load_ptr(slots, _BUILTIN_INIT_RECEIVER_SLOT * C_POINTER_SIZE), offset), 0)


def _builtin_init_dict_storage(slots, tokens) -> int:
    """Acquire the actual item dictionary through its traced owners."""
    if _builtin_init_env(slots, tokens) != 0:
        return -1
    store_ptr(slots, _BUILTIN_INIT_KEY_SLOT * C_POINTER_SIZE, _dict_items_key())
    if _builtin_init_adopt(slots, tokens, _BUILTIN_INIT_KEY_SLOT) != 0:
        return -1
    store_ptr(slots, _BUILTIN_INIT_TEMP_SLOT * C_POINTER_SIZE, py_dict_new())
    if _builtin_init_adopt(slots, tokens, _BUILTIN_INIT_TEMP_SLOT) != 0:
        return -1
    if py_dict_setdefault_slots(ptr_add(slots, _BUILTIN_INIT_ENV_SLOT * C_POINTER_SIZE),
            ptr_add(slots, _BUILTIN_INIT_KEY_SLOT * C_POINTER_SIZE), ptr_add(slots, _BUILTIN_INIT_TEMP_SLOT * C_POINTER_SIZE),
            ptr_add(slots, _BUILTIN_INIT_DICT_SLOT * C_POINTER_SIZE)) != 0:
        return -1
    if _builtin_init_adopt(slots, tokens, _BUILTIN_INIT_DICT_SLOT) != 0:
        return -1
    _builtin_init_drop(slots, tokens, _BUILTIN_INIT_TEMP_SLOT)
    return 0


def _builtin_init_dict(slots, tokens) -> int:
    nargs: int = py_tuple_len(load_ptr(slots, _BUILTIN_INIT_ARGS_SLOT * C_POINTER_SIZE))
    if nargs > 1:
        py_raise_owned(py_exc_new(3, cstr("dict expected at most 1 argument")))
        return -1
    if (load_i32(load_ptr(slots, _BUILTIN_INIT_CLASS_SLOT * C_POINTER_SIZE), PYOBJECTHEADER_FLAGS_OFFSET) & 4) == 0:
        py_raise_owned(py_exc_new(3, cstr("dict.__init__ requires a dict instance")))
        return -1
    if _builtin_init_dict_storage(slots, tokens) != 0:
        return -1
    if nargs == 1:
        if _builtin_init_copy(slots, tokens, _BUILTIN_INIT_SOURCE_SLOT,
                ptr_add(load_ptr(slots, _BUILTIN_INIT_ARGS_SLOT * C_POINTER_SIZE), PYTUPLEOBJECT_ITEMS_OFFSET), 0) != 0:
            return -1
        if py_dict_update_slots(ptr_add(slots, _BUILTIN_INIT_DICT_SLOT * C_POINTER_SIZE),
                                ptr_add(slots, _BUILTIN_INIT_SOURCE_SLOT * C_POINTER_SIZE)) != 0:
            return -1
    kwargs = load_ptr(slots, _BUILTIN_INIT_KWARGS_SLOT * C_POINTER_SIZE)
    if ptr_is_null(kwargs) == 0 and _type_of(kwargs) != PY_TYPE_NONE:
        return py_dict_update_slots(ptr_add(slots, _BUILTIN_INIT_DICT_SLOT * C_POINTER_SIZE),
                                    ptr_add(slots, _BUILTIN_INIT_KWARGS_SLOT * C_POINTER_SIZE))
    return 0


def _builtin_init_exception(slots, tokens) -> int:
    kwargs = load_ptr(slots, _BUILTIN_INIT_KWARGS_SLOT * C_POINTER_SIZE)
    if ptr_is_null(kwargs) == 0 and _type_of(kwargs) != PY_TYPE_NONE:
        if py_dict_len(kwargs) != 0:
            py_raise_owned(py_exc_new(3, cstr("BaseException.__init__ takes no keyword arguments")))
            return -1
    cls = load_ptr(slots, _BUILTIN_INIT_CLASS_SLOT * C_POINTER_SIZE)
    names = load_ptr(cls, PYCLASSOBJECT_FIELD_NAMES_OFFSET)
    count: int = load_i32(cls, PYCLASSOBJECT_N_FIELDS_OFFSET)
    index: int = 0
    while index < count:
        if _cstr_equal(load_ptr(names, index * C_POINTER_SIZE), cstr("args")) != 0:
            receiver = load_ptr(slots, _BUILTIN_INIT_RECEIVER_SLOT * C_POINTER_SIZE)
            pcc_gc_store_ptr(receiver,
                ptr_add(receiver, PYINSTANCEOBJECT_FIELDS_OFFSET + index * C_POINTER_SIZE),
                load_ptr(slots, _BUILTIN_INIT_ARGS_SLOT * C_POINTER_SIZE))
            return -1 if py_err_occurred() != 0 else 0
        index = index + 1
    if _builtin_init_env(slots, tokens) != 0:
        return -1
    store_ptr(slots, _BUILTIN_INIT_KEY_SLOT * C_POINTER_SIZE, py_str_new(cstr("args"), 4))
    if _builtin_init_adopt(slots, tokens, _BUILTIN_INIT_KEY_SLOT) != 0:
        return -1
    return py_dict_set_slots(ptr_add(slots, _BUILTIN_INIT_ENV_SLOT * C_POINTER_SIZE),
        ptr_add(slots, _BUILTIN_INIT_KEY_SLOT * C_POINTER_SIZE), ptr_add(slots, _BUILTIN_INIT_ARGS_SLOT * C_POINTER_SIZE))


def _builtin_init_validate(slots, tokens) -> int:
    if _is_user_instance(load_ptr(slots, _BUILTIN_INIT_RECEIVER_SLOT * C_POINTER_SIZE)) == 0:
        py_raise_owned(py_exc_new(3, cstr("builtin-base initializer requires an instance")))
        return -1
    if _builtin_init_copy(slots, tokens, _BUILTIN_INIT_CLASS_SLOT,
            ptr_add(load_ptr(slots, _BUILTIN_INIT_RECEIVER_SLOT * C_POINTER_SIZE), PYINSTANCEOBJECT_CLS_OFFSET), 1) != 0:
        return -1
    matches: int = py_obj_issubclass(load_ptr(slots, _BUILTIN_INIT_CLASS_SLOT * C_POINTER_SIZE), load_ptr(slots, _BUILTIN_INIT_FROM_CLASS_SLOT * C_POINTER_SIZE))
    if matches < 0 or py_err_occurred() != 0:
        return -1
    if matches == 0:
        py_raise_owned(py_exc_new(3, cstr("super(type, obj): obj must be an instance or subtype of type")))
        return -1
    return 0


@c_abi_export("py_builtin_super_validate_slots")
def py_builtin_super_validate_slots(receiver_slot, from_class_slot) -> int:
    # Attribute-call evaluation binds super before evaluating call operands.
    slots = stack_alloc(_BUILTIN_INIT_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_BUILTIN_INIT_SLOT_COUNT * C_POINTER_SIZE)
    handles = stack_alloc(_BUILTIN_INIT_SLOT_COUNT * C_POINTER_SIZE)
    count: int = _builtin_init_open(slots, tokens, handles)
    suspended: int = 0
    status: int = -1
    if count == _BUILTIN_INIT_SLOT_COUNT:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _builtin_init_copy(slots, tokens, _BUILTIN_INIT_RECEIVER_SLOT, receiver_slot, 0)
        if status == 0:
            status = _builtin_init_copy(slots, tokens, _BUILTIN_INIT_FROM_CLASS_SLOT, from_class_slot, 0)
        if status == 0:
            status = _builtin_init_validate(slots, tokens)
    if status != 0:
        _builtin_init_error(cstr("super binding failed without an exception"))
    _builtin_init_close(slots, tokens, handles, count, suspended)
    return status


def _builtin_init_dispatch(receiver_slot, from_slot, args_slot, kwargs_slot, kind: int) -> int:
    slots = stack_alloc(_BUILTIN_INIT_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_BUILTIN_INIT_SLOT_COUNT * C_POINTER_SIZE)
    handles = stack_alloc(_BUILTIN_INIT_SLOT_COUNT * C_POINTER_SIZE)
    count: int = _builtin_init_open(slots, tokens, handles)
    suspended: int = 0
    status: int = -1
    if count == _BUILTIN_INIT_SLOT_COUNT:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _builtin_init_copy(slots, tokens, _BUILTIN_INIT_RECEIVER_SLOT, receiver_slot, 0)
        if status == 0:
            status = _builtin_init_copy(slots, tokens, _BUILTIN_INIT_FROM_CLASS_SLOT, from_slot, 0)
        if status == 0:
            status = _builtin_init_copy(slots, tokens, _BUILTIN_INIT_ARGS_SLOT, args_slot, 0)
        if status == 0:
            status = _builtin_init_copy(slots, tokens, _BUILTIN_INIT_KWARGS_SLOT, kwargs_slot, 0)
        if status == 0:
            status = _builtin_init_validate(slots, tokens)
        if status == 0:
            args = load_ptr(slots, _BUILTIN_INIT_ARGS_SLOT * C_POINTER_SIZE)
            kwargs = load_ptr(slots, _BUILTIN_INIT_KWARGS_SLOT * C_POINTER_SIZE)
            if ptr_is_null(args) != 0 or _type_of(args) != PY_TYPE_TUPLE:
                status = _builtin_init_error(cstr("initializer args must be a tuple"))
            elif ptr_is_null(kwargs) == 0 and _type_of(kwargs) != PY_TYPE_NONE and _type_of(kwargs) != PY_TYPE_DICT:
                status = _builtin_init_error(cstr("initializer keywords must be a dict"))
        if status == 0:
            status = _builtin_init_dict(slots, tokens) if kind == 1 else _builtin_init_exception(slots, tokens)
    if status != 0:
        _builtin_init_error(cstr("builtin-base initialization failed without an exception"))
    _builtin_init_close(slots, tokens, handles, count, suspended)
    return status


@c_abi_export("py_dict_subclass_init_slots")
def py_dict_subclass_init_slots(receiver_slot, from_class_slot, args_slot, kwargs_slot) -> int:
    return _builtin_init_dispatch(receiver_slot, from_class_slot, args_slot, kwargs_slot, 1)


@c_abi_export("py_exception_subclass_init_slots")
def py_exception_subclass_init_slots(receiver_slot, from_class_slot, args_slot, kwargs_slot) -> int:
    return _builtin_init_dispatch(receiver_slot, from_class_slot, args_slot, kwargs_slot, 2)


@c_abi_export("py_dict_storage_slots")
def py_dict_storage_slots(receiver_slot, result_slot) -> int:
    """Copy real dict storage into an empty registered owning result slot.

    Exact dictionaries and native dict subclasses are accepted. This bypasses
    user mapping protocols, including keys/getitem overrides and __missing__.
    The caller owns both slots; no borrowed backing pointer escapes.
    """
    slots = stack_alloc(_BUILTIN_INIT_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_BUILTIN_INIT_SLOT_COUNT * C_POINTER_SIZE)
    handles = stack_alloc(_BUILTIN_INIT_SLOT_COUNT * C_POINTER_SIZE)
    count: int = _builtin_init_open(slots, tokens, handles)
    suspended: int = 0
    status: int = -1
    if count == _BUILTIN_INIT_SLOT_COUNT:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _builtin_init_copy(slots, tokens, _BUILTIN_INIT_RECEIVER_SLOT, receiver_slot, 0)
        if status == 0:
            receiver = load_ptr(slots, _BUILTIN_INIT_RECEIVER_SLOT * C_POINTER_SIZE)
            if ptr_is_null(receiver) == 0 and _type_of(receiver) == PY_TYPE_DICT:
                status = _builtin_init_copy(slots, tokens, _BUILTIN_INIT_DICT_SLOT,
                    ptr_add(slots, _BUILTIN_INIT_RECEIVER_SLOT * C_POINTER_SIZE), 0)
            elif _is_user_instance(receiver) != 0:
                status = _builtin_init_copy(slots, tokens, _BUILTIN_INIT_CLASS_SLOT,
                    ptr_add(load_ptr(slots, _BUILTIN_INIT_RECEIVER_SLOT * C_POINTER_SIZE), PYINSTANCEOBJECT_CLS_OFFSET), 1)
                if status == 0:
                    cls = load_ptr(slots, _BUILTIN_INIT_CLASS_SLOT * C_POINTER_SIZE)
                    if ptr_is_null(cls) != 0 or (load_i32(cls, PYOBJECTHEADER_FLAGS_OFFSET) & 4) == 0:
                        status = 1
                    else:
                        status = _builtin_init_dict_storage(slots, tokens)
            else:
                status = 1
        if status == 1:
            py_raise_owned(py_exc_new(3, cstr("type.__new__() argument 3 must be dict")))
            status = -1
        if status == 0:
            storage_slot = ptr_add(slots, _BUILTIN_INIT_DICT_SLOT * C_POINTER_SIZE)
            status = pcc_gc_root_move(result_slot, storage_slot)
            if status == 0:
                token: int = load_i64(tokens, _BUILTIN_INIT_DICT_SLOT * C_POINTER_SIZE)
                store_i64(tokens, _BUILTIN_INIT_DICT_SLOT * C_POINTER_SIZE, 0)
                if pcc_gc_foreign_lease_release(result_slot, token) != 0:
                    pcc_platform_abort()
                    status = -1
    if status != 0:
        _builtin_init_error(cstr("dictionary storage acquisition failed without an exception"))
    _builtin_init_close(slots, tokens, handles, count, suspended)
    return status


# Index entry owns slot addresses, including across the compiled thread-entry
# poll before this function's body. Passing the address of a raw callee copy
# does not satisfy this ABI: both caller slots must already be registered.
# The older raw index/slice APIs above remain compatibility boundaries.
pcc_gc_root_move = extern("pcc_gc_root_move", (c_ptr, c_ptr), c_int64)
py_obj_special_call_slots = extern("py_obj_special_call_slots", (c_ptr, c_ptr, c_ptr, c_ptr, c_ptr, c_ptr), c_int64)


# Scratch indices shared by the root, lease-token and registration arrays.
# Slot zero saves entry TLS; the final slot preserves an error during cleanup.
_INDEX_OLD_EXCEPTION_SLOT = 0
_INDEX_RECEIVER_SLOT = 1
_INDEX_RESULT_SLOT = 2
_INDEX_ERROR_SLOT = 3
_INDEX_SLOT_COUNT = 4


def _index_slot_error(message) -> int:
    py_runtime_error_if_unset(cstr("slot-based index"), message)
    return -1


def _index_slot_open(slots, tokens, handles) -> int:
    # old TLS, receiver, integer result, new TLS. No input value is loaded
    # until every potentially parking registration has finished.
    memset(slots, 0, _INDEX_SLOT_COUNT * C_POINTER_SIZE)
    memset(tokens, 0, _INDEX_SLOT_COUNT * C_POINTER_SIZE)
    memset(handles, 0, _INDEX_SLOT_COUNT * C_POINTER_SIZE)
    count: int = 0
    while count < _INDEX_SLOT_COUNT:
        handle = pcc_gc_scheduler_root_register_handle(ptr_add(slots, count * C_POINTER_SIZE))
        if ptr_is_null(handle) != 0:
            return count
        store_ptr(handles, count * C_POINTER_SIZE, handle)
        count = count + 1
    return count


def _index_slot_copy(slots, tokens, index: int, source) -> int:
    token: int = pcc_gc_root_copy_lease(ptr_add(slots, index * C_POINTER_SIZE), source)
    if token < 0:
        return _index_slot_error(cstr("index source root copy failed"))
    store_i64(tokens, index * C_POINTER_SIZE, token)
    return 0


def _index_slot_adopt(slots, tokens) -> int:
    # A producer has already published its NEW result directly into this
    # registered empty root. Lease acquisition reloads it after any wait.
    slot = ptr_add(slots, _INDEX_RESULT_SLOT * C_POINTER_SIZE)
    token: int = pcc_gc_foreign_lease_acquire(slot)
    if token < 0:
        return _index_slot_error(cstr("index result lease acquisition failed"))
    store_i64(tokens, _INDEX_RESULT_SLOT * C_POINTER_SIZE, token)
    pcc_py_gc_minor_graph_lock()
    pcc_gc_note_slot_write_barrier(null(), slot, load_ptr(slot, 0))
    pcc_py_gc_minor_graph_unlock()
    return 0


def _index_slot_close(slots, tokens, handles, count: int, suspended: int) -> None:
    if suspended != 0:
        py_tls_exc_swap_slot(ptr_add(slots, _INDEX_ERROR_SLOT * C_POINTER_SIZE))
    index: int = _INDEX_RESULT_SLOT
    while index > 0:
        if index < count:
            slot = ptr_add(slots, index * C_POINTER_SIZE)
            if pcc_gc_foreign_lease_release(slot, load_i64(tokens, index * C_POINTER_SIZE)) != 0:
                pcc_platform_abort()
                return
            pcc_gc_store_root(slot, null())
        index = index - 1
    if suspended != 0:
        # A callback/validation error wins over old TLS and finalizer errors.
        # On success restore the entry exception after releasing operands.
        py_clear_exception()
        if ptr_is_null(load_ptr(slots, _INDEX_ERROR_SLOT * C_POINTER_SIZE)) == 0:
            pcc_gc_store_root(slots, null())
            py_clear_exception()
            py_tls_exc_swap_slot(ptr_add(slots, _INDEX_ERROR_SLOT * C_POINTER_SIZE))
        else:
            py_tls_exc_swap_slot(slots)
    index = 0
    while index < count:
        pcc_gc_scheduler_root_unregister_handle(load_ptr(handles, index * C_POINTER_SIZE))
        index = index + 1


def _index_slot_dispatch(slots, tokens) -> int:
    receiver_slot = ptr_add(slots, _INDEX_RECEIVER_SLOT * C_POINTER_SIZE)
    result_slot = ptr_add(slots, _INDEX_RESULT_SLOT * C_POINTER_SIZE)
    receiver = load_ptr(receiver_slot, 0)
    if ptr_is_null(receiver) != 0:
        py_raise_owned(py_exc_new(3, cstr("object cannot be interpreted as an integer")))
        return -1
    tag: int = _type_of(receiver)
    if tag == PY_TYPE_INT:
        return _index_slot_copy(slots, tokens, _INDEX_RESULT_SLOT, receiver_slot)
    if tag == PY_TYPE_BOOL:
        truth: int = ptr_eq(receiver, global_load_ptr("py_True"))
        store_ptr(result_slot, 0, py_int_from_i64(1 if truth != 0 else 0))
    elif pcc_capi_is_cext_type_tag(tag) != 0:
        # The input owns an independent address lease through the raw C-API
        # call; the NEW result's first operation is publication in its root.
        store_ptr(result_slot, 0, PyNumber_Index(load_ptr(receiver_slot, 0)))
    else:
        handled = stack_alloc(8)
        store_i64(handled, 0, 0)
        status: int = py_obj_special_call_slots(receiver_slot, cstr("__index__"), null(), null(), result_slot, handled)
        if status != 0:
            return -1
        if load_i64(handled, 0) == 0:
            py_raise_type_named_error(cstr("'"), load_ptr(receiver_slot, 0),
                                      cstr("' object cannot be interpreted as an integer"))
            return -1
    if _index_slot_adopt(slots, tokens) != 0:
        return -1
    if ptr_is_null(load_ptr(result_slot, 0)) != 0:
        return _index_slot_error(cstr("index callback returned NULL without an exception"))
    if py_err_occurred() != 0:
        return -1
    result_tag: int = _type_of(load_ptr(result_slot, 0))
    if result_tag == PY_TYPE_INT:
        return 0
    if result_tag == PY_TYPE_BOOL:
        # Preserve the existing explicit capability boundary: CPython emits
        # a DeprecationWarning here, and owned warning emission is not ready.
        py_raise_owned(py_exc_new(11, cstr("pcc operator.index: non-exact __index__ results require owned warning emission")))
        return -1
    py_raise_conversion_result_error(load_ptr(receiver_slot, 0),
                                     cstr(".__index__() must return an int, not "),
                                     load_ptr(result_slot, 0))
    return -1


def _round_ndigits_clipped(ndigits, out) -> int:
    """PyNumber_AsSsize_t(ndigits, NULL) into out[0]: the index value, an
    out-of-range index clipped to the i64 range by its sign. 0, or -1 with
    the index protocol's error."""
    index = py_obj_index(ndigits)
    if ptr_is_null(index) != 0:
        return -1
    overflow = stack_alloc(4)
    store_i32(overflow, 0, 0)
    value: int = py_int_to_i64(index, overflow)
    if load_i32(overflow, 0) != 0:
        value = 9223372036854775807
        if py_int_cmp(index, py_int_from_i64(0)) < 0:
            value = -9223372036854775807 - 1
    py_decref(index)
    store_i64(out, 0, value)
    return 0


def _float_round(number, ndigits):
    """float.__round__: without ndigits the nearest int, ties to even
    (ValueError for NaN, OverflowError for an infinity); with it
    double_round at ndigits' clipped index."""
    value: float = py_float_to_f64(number)
    if ptr_is_null(ndigits) != 0:
        return py_int_from_f64_exact(rint_c(value))
    digits = stack_alloc(8)
    if _round_ndigits_clipped(ndigits, digits) != 0:
        return null()
    return py_float_round_ndigits(value, load_i64(digits, 0))


def _int_round_exact(number):
    """CPython's long_long: the value as an exact int (a bool answers 0/1)."""
    if is_tagged_int(number) == 0 and _type_of(number) == PY_TYPE_BOOL:
        if ptr_eq(number, global_load_ptr("py_True")) != 0:
            return py_int_from_i64(1)
        return py_int_from_i64(0)
    py_incref(number)
    return number


def _int_round_negative(value, index):
    """int.__round__ for a negative ndigits ``index``: value minus the
    remainder of _PyLong_DivmodNear(value, 10**-ndigits), the nearest
    multiple of the power with a tie going to the even multiple."""
    exponent = py_int_neg(index)
    if ptr_is_null(exponent) != 0:
        return null()
    power = py_int_pow(py_int_from_i64(10), exponent)
    py_decref(exponent)
    if ptr_is_null(power) != 0:
        return null()
    quotient = py_int_floordiv(value, power)
    remainder = py_int_mod(value, power)
    result = null()
    if ptr_is_null(quotient) == 0 and ptr_is_null(remainder) == 0:
        twice = py_int_add(remainder, remainder)
        odd = py_int_and(quotient, py_int_from_i64(1))
        if ptr_is_null(twice) == 0 and ptr_is_null(odd) == 0:
            order: int = py_int_cmp(twice, power)
            if order > 0 or (order == 0 and py_int_cmp(odd, py_int_from_i64(0)) != 0):
                nearer = py_int_sub(remainder, power)
                py_decref(remainder)
                remainder = nearer
            if ptr_is_null(remainder) == 0:
                result = py_int_sub(value, remainder)
        py_decref(twice)
        py_decref(odd)
    py_decref(quotient)
    py_decref(remainder)
    py_decref(power)
    return result


def _int_round(number, ndigits):
    """int.__round__: the value as an exact int, except that a negative
    ndigits rounds to the nearest multiple of 10**-ndigits."""
    if ptr_is_null(ndigits) != 0:
        return _int_round_exact(number)
    index = py_obj_index(ndigits)
    if ptr_is_null(index) != 0:
        return null()
    if py_int_cmp(index, py_int_from_i64(0)) >= 0:
        py_decref(index)
        return _int_round_exact(number)
    value = _int_round_exact(number)
    result = _int_round_negative(value, index)
    py_decref(value)
    py_decref(index)
    return result


@c_abi_export("py_builtin_round")
def py_builtin_round(number, ndigits):
    """round(number, ndigits) as CPython's builtin_round_impl; a NULL or
    None ``ndigits`` is the one-argument form. Returns NEW.

    The number's type answers through __round__: float.__round__ and
    int.__round__ (a bool is an int) for the builtin numbers, a class's own
    __round__ (with no argument for the one-argument form) otherwise, and a
    type without one raises CPython's TypeError naming it.
    """
    digits_argument = ndigits
    if ptr_is_null(digits_argument) == 0 and ptr_eq(digits_argument, global_load_ptr("py_None")) != 0:
        digits_argument = null()
    if is_tagged_int(number) != 0:
        return _int_round(number, digits_argument)
    tag: int = _type_of(number)
    if tag == PY_TYPE_INT or tag == PY_TYPE_BOOL:
        return _int_round(number, digits_argument)
    if tag == PY_TYPE_FLOAT:
        return _float_round(number, digits_argument)
    method = _lookup_dunder(number, cstr("__round__"))
    if ptr_is_null(method) != 0:
        if py_err_occurred() != 0:
            return null()
        return py_raise_type_named_error(cstr("type "), number, cstr(" doesn't define __round__ method"))
    if ptr_is_null(digits_argument) != 0:
        return _call_unary(method, number)
    return _call_binary(method, number, digits_argument)


def _sum_abs(x: float) -> float:
    if x < 0.0:
        return 0.0 - x
    return x


def _sum_cs_add(sums, index: int, x: float) -> None:
    """CPython's cs_add: Neumaier summation into the (hi, lo) pair stored at
    sums[index] and sums[index + 1]."""
    hi: float = load_f64(sums, index * 8)
    lo: float = load_f64(sums, index * 8 + 8)
    t: float = hi + x
    if _sum_abs(hi) >= _sum_abs(x):
        lo = lo + ((hi - t) + x)
    else:
        lo = lo + ((x - t) + hi)
    store_f64(sums, index * 8, t)
    store_f64(sums, index * 8 + 8, lo)


def _sum_cs_value(sums, index: int) -> float:
    """CPython's cs_to_double: the compensation joins only when it is nonzero
    and finite, which keeps a negative zero and an overflowed sum."""
    hi: float = load_f64(sums, index * 8)
    lo: float = load_f64(sums, index * 8 + 8)
    if lo != 0.0 and lo - lo == 0.0:
        return hi + lo
    return hi


def _sum_kind(o) -> int:
    """1 an exact int or a bool, 2 an exact float, 3 an exact complex, else 0."""
    if is_tagged_int(o) != 0:
        return 1
    tag: int = _type_of(o)
    if tag == PY_TYPE_INT or tag == PY_TYPE_BOOL:
        return 1
    if tag == PY_TYPE_FLOAT:
        return 2
    if tag == PY_TYPE_COMPLEX:
        return 3
    return 0


def _sum_int_double(item, out) -> int:
    """PyLong_AsDouble of an int or bool item into out[0]: 0, or -1 with
    CPython's OverflowError when the int is beyond the double range."""
    value: float = py_float_to_f64(item)
    if py_err_occurred() != 0:
        return -1
    if value != 0.0 and value == value * 2.0:
        py_raise_owned(py_exc_new(15, cstr("int too large to convert to float")))
        return -1
    store_f64(out, 0, value)
    return 0


# py_builtin_sum's roots, each owning one reference: the iterator at offset
# 0, the running result at 8 and the current item at 16. pcc_gc_store_root
# keeps the balance on every update; registered roots follow a moving
# collector, so they are read again after every call that can park.
def _sum_set(roots, offset: int, value) -> int:
    """Move a NEW reference (NULL after an error) into the root at offset."""
    pcc_gc_store_root(ptr_add(roots, offset), value)
    if ptr_is_null(value) != 0:
        return -1
    py_decref(value)
    return 0


def _sum_next(roots) -> int:
    """next(iterator) into the item root: 1 an item, 0 exhausted, -1 raised."""
    item = py_obj_next(load_ptr(roots, 0))
    if ptr_is_null(item) == 0:
        if _sum_set(roots, 16, item) == 0:
            return 1
        return -1
    if py_err_occurred() != 0:
        if py_exc_matches(py_current_exception(), py_exc_builtin_class(8)) == 0:
            return -1
        py_clear_exception()
    return 0


def _sum_add_item(roots) -> int:
    """result = result + item through the number protocol; drops the item."""
    added = py_obj_add(load_ptr(roots, 8), load_ptr(roots, 16))
    pcc_gc_store_root(ptr_add(roots, 16), null())
    return _sum_set(roots, 8, added)


def _sum_int_run(roots) -> int:
    """Exact-int run: int and bool items add exactly; the first other item is
    added generically and ends the run. 1 exhausted, 0 left, -1 raised."""
    status: int = 2
    while status == 2:
        got: int = _sum_next(roots)
        if got <= 0:
            status = 1
            if got < 0:
                status = -1
        else:
            kind: int = _sum_kind(load_ptr(roots, 16))
            if _sum_add_item(roots) != 0:
                status = -1
            elif kind != 1:
                status = 0
    return status


def _sum_float_run(roots, sums, number) -> int:
    """Exact-float run with CPython's compensated sum; int and bool items are
    converted as PyLong_AsDouble does."""
    store_f64(sums, 0, py_float_to_f64(load_ptr(roots, 8)))
    store_f64(sums, 8, 0.0)
    status: int = 2
    while status == 2:
        got: int = _sum_next(roots)
        if got < 0:
            status = -1
        elif got == 0:
            status = 1
            if _sum_set(roots, 8, py_float_from_f64(_sum_cs_value(sums, 0))) != 0:
                status = -1
        else:
            kind: int = _sum_kind(load_ptr(roots, 16))
            if kind == 2:
                _sum_cs_add(sums, 0, py_float_to_f64(load_ptr(roots, 16)))
                pcc_gc_store_root(ptr_add(roots, 16), null())
            elif kind == 1:
                if _sum_int_double(load_ptr(roots, 16), number) != 0:
                    status = -1
                else:
                    _sum_cs_add(sums, 0, load_f64(number, 0))
                    pcc_gc_store_root(ptr_add(roots, 16), null())
            else:
                status = 0
                if _sum_set(roots, 8, py_float_from_f64(_sum_cs_value(sums, 0))) != 0:
                    status = -1
                elif _sum_add_item(roots) != 0:
                    status = -1
    return status


def _sum_complex_value(sums):
    return py_complex_new(_sum_cs_value(sums, 0), _sum_cs_value(sums, 2))


def _sum_complex_run(roots, sums, number) -> int:
    """Exact-complex run: one compensated sum per part. An int or float item
    adds to the real part only (CPython 3.15 keeps sum([1], -0j) at 1-0j)."""
    store_f64(sums, 0, load_f64(load_ptr(roots, 8), PYCOMPLEXOBJECT_REAL_OFFSET))
    store_f64(sums, 8, 0.0)
    store_f64(sums, 16, load_f64(load_ptr(roots, 8), PYCOMPLEXOBJECT_IMAG_OFFSET))
    store_f64(sums, 24, 0.0)
    status: int = 2
    while status == 2:
        got: int = _sum_next(roots)
        if got < 0:
            status = -1
        elif got == 0:
            status = 1
            if _sum_set(roots, 8, _sum_complex_value(sums)) != 0:
                status = -1
        else:
            kind: int = _sum_kind(load_ptr(roots, 16))
            if kind == 3:
                _sum_cs_add(sums, 0, load_f64(load_ptr(roots, 16), PYCOMPLEXOBJECT_REAL_OFFSET))
                _sum_cs_add(sums, 2, load_f64(load_ptr(roots, 16), PYCOMPLEXOBJECT_IMAG_OFFSET))
                pcc_gc_store_root(ptr_add(roots, 16), null())
            elif kind == 2:
                _sum_cs_add(sums, 0, py_float_to_f64(load_ptr(roots, 16)))
                pcc_gc_store_root(ptr_add(roots, 16), null())
            elif kind == 1:
                if _sum_int_double(load_ptr(roots, 16), number) != 0:
                    status = -1
                else:
                    _sum_cs_add(sums, 0, load_f64(number, 0))
                    pcc_gc_store_root(ptr_add(roots, 16), null())
            else:
                status = 0
                if _sum_set(roots, 8, _sum_complex_value(sums)) != 0:
                    status = -1
                elif _sum_add_item(roots) != 0:
                    status = -1
    return status


def _sum_generic_run(roots) -> int:
    status: int = 2
    while status == 2:
        got: int = _sum_next(roots)
        if got < 0:
            status = -1
        elif got == 0:
            status = 1
        elif _sum_add_item(roots) != 0:
            status = -1
    return status


def _sum_refused_start(start):
    """CPython's message for a str, bytes or bytearray start, else NULL."""
    if ptr_is_null(start) != 0 or is_tagged_int(start) != 0:
        return null()
    if py_str_check(start) != 0:
        return cstr("sum() can't sum strings [use ''.join(seq) instead]")
    tag: int = _type_of(start)
    if tag == PY_TYPE_BYTES:
        return cstr("sum() can't sum bytes [use b''.join(seq) instead]")
    if tag == PY_TYPE_BYTEARRAY:
        return cstr("sum() can't sum bytearray [use b''.join(seq) instead]")
    return null()


@c_abi_export("py_builtin_sum")
def py_builtin_sum(iterable, start):
    """sum(iterable, start) as CPython 3.15's builtin_sum_impl; a NULL start
    is the default 0. Returns NEW.

    The iterator comes first (sum(5, "") reports the int), then the
    str/bytes/bytearray start refusal. The exact-int, exact-float and
    exact-complex runs are entered in that order, each at most once: the
    int run adds exactly, the float run uses CPython's Neumaier compensated
    sum (int items through PyLong_AsDouble), the complex run keeps one
    compensated sum per part, and the first other item continues in the
    generic + loop.
    """
    it = py_obj_iter(iterable)
    if ptr_is_null(it) != 0:
        return null()
    refused = _sum_refused_start(start)
    if ptr_is_null(refused) == 0:
        py_decref(it)
        py_raise_owned(py_exc_new(3, refused))
        return null()
    initial = start
    if ptr_is_null(initial) != 0:
        initial = py_int_from_i64(0)
    else:
        py_incref(initial)
    roots = stack_alloc(24)
    store_ptr(roots, 0, null())
    store_ptr(roots, 8, null())
    store_ptr(roots, 16, null())
    iterator_handle = pcc_gc_scheduler_root_register_handle(roots)
    result_handle = pcc_gc_scheduler_root_register_handle(ptr_add(roots, 8))
    item_handle = pcc_gc_scheduler_root_register_handle(ptr_add(roots, 16))
    _sum_set(roots, 0, it)
    _sum_set(roots, 8, initial)
    status: int = 0
    if ptr_is_null(iterator_handle) != 0 or ptr_is_null(result_handle) != 0 or ptr_is_null(item_handle) != 0:
        _protocol_require_result(null(), cstr("sum"), cstr("sum root registration failed"))
        status = -1
    sums = stack_alloc(32)
    number = stack_alloc(8)
    if status == 0 and _sum_kind(load_ptr(roots, 8)) == 1:
        status = _sum_int_run(roots)
    if status == 0 and _sum_kind(load_ptr(roots, 8)) == 2:
        status = _sum_float_run(roots, sums, number)
    if status == 0 and _sum_kind(load_ptr(roots, 8)) == 3:
        status = _sum_complex_run(roots, sums, number)
    if status == 0:
        status = _sum_generic_run(roots)
    result = null()
    if status == 1:
        result = load_ptr(roots, 8)
        py_incref(result)
    pcc_gc_store_root(ptr_add(roots, 16), null())
    pcc_gc_store_root(ptr_add(roots, 8), null())
    pcc_gc_store_root(roots, null())
    if ptr_is_null(item_handle) == 0:
        pcc_gc_scheduler_root_unregister_handle(item_handle)
    if ptr_is_null(result_handle) == 0:
        pcc_gc_scheduler_root_unregister_handle(result_handle)
    if ptr_is_null(iterator_handle) == 0:
        pcc_gc_scheduler_root_unregister_handle(iterator_handle)
    return result


@c_abi_export("py_builtin_sum1")
def py_builtin_sum1(iterable):
    """sum(iterable) with the default start. Returns NEW."""
    return py_builtin_sum(iterable, null())


@c_abi_export("py_builtin_round1")
def py_builtin_round1(number):
    """round(number), the one-argument form. Returns NEW."""
    return py_builtin_round(number, null())


@c_abi_export("py_obj_has_index")
def py_obj_has_index(o) -> int:
    """CPython's _PyIndex_Check: an int, a bool, an extension type with
    nb_index, or a class defining __index__."""
    if ptr_is_null(o) != 0:
        return 0
    tag: int = _type_of(o)
    if tag == PY_TYPE_INT or tag == PY_TYPE_BOOL:
        return 1
    if pcc_capi_is_cext_type_tag(tag) != 0:
        return PyIndex_Check(o)
    if ptr_is_null(_lookup_dunder(o, cstr("__index__"))) != 0:
        return 0
    return 1


@c_abi_export("py_obj_index_slots")
def py_obj_index_slots(receiver_slot, result_slot) -> int:
    """Publish one arbitrary-precision int owner; status 0/-1 is success/error.

    Receiver is an authoritative registered owning CALLER root. Result is a
    distinct, empty registered owning CALLER root. No raw operand may be
    materialized into a nominal local slot just before entering this helper.
    """
    if ptr_is_null(receiver_slot) != 0 or ptr_is_null(result_slot) != 0:
        return _index_slot_error(cstr("index requires source and output root slots"))
    slots = stack_alloc(_INDEX_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_INDEX_SLOT_COUNT * C_POINTER_SIZE)
    handles = stack_alloc(_INDEX_SLOT_COUNT * C_POINTER_SIZE)
    count: int = _index_slot_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == _INDEX_SLOT_COUNT:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        if ptr_eq(receiver_slot, result_slot) != 0 or ptr_is_null(load_ptr(result_slot, 0)) == 0:
            status = _index_slot_error(cstr("index result destination must be distinct and empty"))
        else:
            status = _index_slot_copy(slots, tokens, _INDEX_RECEIVER_SLOT, receiver_slot)
            if status == 0:
                status = _index_slot_dispatch(slots, tokens)
            if status == 0:
                status = pcc_gc_root_move(result_slot, ptr_add(slots, _INDEX_RESULT_SLOT * C_POINTER_SIZE))
                if status == 0:
                    token: int = load_i64(tokens, _INDEX_RESULT_SLOT * C_POINTER_SIZE)
                    store_i64(tokens, _INDEX_RESULT_SLOT * C_POINTER_SIZE, 0)
                    if pcc_gc_foreign_lease_release(result_slot, token) != 0:
                        pcc_platform_abort()
                        return -1
    else:
        _index_slot_error(cstr("index root registration failed"))
    if status != 0:
        _index_slot_error(cstr("index conversion failed without an exception"))
    _index_slot_close(slots, tokens, handles, count, suspended)
    return status


def _index_slot_checked(receiver_slot, container_index: int) -> int:
    if ptr_is_null(receiver_slot) != 0:
        _index_slot_error(cstr("checked index requires a source root slot"))
        return 0
    slots = stack_alloc(_INDEX_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_INDEX_SLOT_COUNT * C_POINTER_SIZE)
    handles = stack_alloc(_INDEX_SLOT_COUNT * C_POINTER_SIZE)
    count: int = _index_slot_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    result: int = 0
    if count == _INDEX_SLOT_COUNT:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _index_slot_copy(slots, tokens, _INDEX_RECEIVER_SLOT, receiver_slot)
        if status == 0:
            status = _index_slot_dispatch(slots, tokens)
        if status == 0:
            overflow = stack_alloc(4)
            store_i32(overflow, 0, 0)
            result = py_int_to_i64(load_ptr(slots, _INDEX_RESULT_SLOT * C_POINTER_SIZE), overflow)
            if load_i32(overflow, 0) != 0:
                if container_index != 0:
                    py_raise_owned(py_exc_new(5, cstr("cannot fit integer into an index-sized integer")))
                else:
                    py_raise_owned(py_exc_new(15, cstr("Python int too large to convert to platform index")))
                status = -1
            elif py_err_occurred() != 0:
                status = -1
    else:
        _index_slot_error(cstr("checked index root registration failed"))
    if status != 0:
        _index_slot_error(cstr("checked index conversion failed without an exception"))
        result = 0
    _index_slot_close(slots, tokens, handles, count, suspended)
    return result


@c_abi_export("py_index_i64_checked_slots")
def py_index_i64_checked_slots(receiver_slot) -> int:
    """Checked platform conversion from an authoritative owning CALLER root.

    Unlike slice saturation, conversion overflow raises OverflowError. The
    integer remains rooted and leased through conversion/errors/cleanup.
    """
    return _index_slot_checked(receiver_slot, 0)


@c_abi_export("py_obj_index_i64_slots")
def py_obj_index_i64_slots(receiver_slot) -> int:
    """Container index conversion; only integer narrowing raises IndexError.

    Callback exceptions, including OverflowError from __index__, propagate
    unchanged. This is not a saturating slice-bound conversion.
    """
    return _index_slot_checked(receiver_slot, 1)


# Raw compatibility callers must hold live address leases for every operand
# through entry. Publish those borrowed inputs before the first polling call;
# class/descriptor selection then happens only from independent owning slots.
# Never pass a borrowed py_class_lookup result into this boundary.
py_obj_binary_special_call_slots = extern(
    "py_obj_binary_special_call_slots",
    (c_ptr, c_ptr, c_ptr, c_ptr, c_int64, c_ptr, c_ptr), c_int64,
)
py_obj_ternary_special_call_slots = extern(
    "py_obj_ternary_special_call_slots",
    (c_ptr, c_ptr, c_ptr, c_ptr, c_ptr, c_ptr, c_ptr), c_int64,
)
py_obj_hash = extern("py_obj_hash", (c_ptr,), c_int64)
py_str_payload = extern("py_str_payload", (c_ptr,), c_ptr)

_NAMED_OLD_ERROR = 0
_NAMED_RECEIVER = 1
_NAMED_ARG0 = 2
_NAMED_ARG1 = 3
_NAMED_ARGS = 4
_NAMED_RESULT = 5
_NAMED_ERROR = 6
_NAMED_COUNT = 7
_NAMED_BORROWED_COUNT = 3

define_global_i32("pcc_named_protocol_borrowed_map", -3)
define_global_i32("pcc_named_protocol_owned_map", 7)


def _named_adopt(slots, tokens, index: int) -> int:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    token: int = pcc_gc_foreign_lease_acquire(slot)
    if token < 0:
        py_runtime_error_if_unset(cstr("named protocol"), cstr("result owner lease failed"))
        return -1
    store_i64(tokens, index * C_POINTER_SIZE, token)
    pcc_py_gc_minor_graph_lock()
    pcc_gc_note_slot_write_barrier(null(), slot, load_ptr(slot, 0))
    pcc_py_gc_minor_graph_unlock()
    return 0


def _named_drop(slots, tokens, index: int) -> None:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    if pcc_gc_foreign_lease_release(slot, load_i64(tokens, index * C_POINTER_SIZE)) != 0:
        pcc_platform_abort()
        return
    store_i64(tokens, index * C_POINTER_SIZE, 0)
    pcc_gc_store_root(slot, null())


def _named_convert(slots, conversion: int, scalar) -> int:
    result_slot = ptr_add(slots, _NAMED_RESULT * C_POINTER_SIZE)
    result = load_ptr(result_slot, 0)
    if conversion == 0 or conversion == 5 or conversion == 7:
        return 0
    tag: int = _type_of(result)
    value: int = 0
    if conversion == 1:
        # Python accepts an index-protocol result from __len__. The slot
        # conversion keeps both that result and a callback-produced integer
        # owned across narrowing, including overflow and descriptor failures.
        value = py_index_i64_checked_slots(result_slot)
        if py_err_occurred() != 0:
            return -1
        if value < 0:
            py_raise_owned(py_exc_new(2, cstr("__len__() should return >= 0")))
            return -1
    elif conversion == 2:
        value = py_obj_truthy(load_ptr(result_slot, 0))
    elif conversion == 3:
        if tag != PY_TYPE_BOOL:
            py_raise_owned(py_exc_new(3, cstr("__bool__ should return bool")))
            return -1
        value = ptr_eq(load_ptr(result_slot, 0), global_load_ptr("py_True"))
    elif conversion == 4:
        if tag != PY_TYPE_INT and tag != PY_TYPE_BOOL:
            py_raise_owned(py_exc_new(3, cstr("__hash__ method should return an integer")))
            return -1
        overflow = stack_alloc(4)
        store_i32(overflow, 0, 0)
        value = py_int_to_i64(load_ptr(result_slot, 0), overflow)
        if load_i32(overflow, 0) != 0:
            value = py_obj_hash(load_ptr(result_slot, 0))
        if value == -1:
            value = -2
    elif conversion == 6:
        if ptr_is_null(py_str_payload(load_ptr(result_slot, 0))) != 0:
            py_raise_owned(py_exc_new(3, cstr("string special method returned non-string")))
            return -1
    elif conversion == 8 or conversion == 9:
        if conversion == 8 and tag != PY_TYPE_FLOAT:
            py_raise_conversion_result_error(load_ptr(slots, _NAMED_RECEIVER * C_POINTER_SIZE),
                                             cstr(".__float__() must return a float, not "),
                                             load_ptr(result_slot, 0))
            return -1
        if conversion == 9 and tag != PY_TYPE_INT:
            if tag == PY_TYPE_BOOL:
                py_raise_owned(py_exc_new(11, cstr("pcc float: non-exact __index__ results require owned warning emission")))
            else:
                py_raise_conversion_result_error(load_ptr(slots, _NAMED_RECEIVER * C_POINTER_SIZE),
                                                 cstr(".__index__() must return an int, not "),
                                                 load_ptr(result_slot, 0))
            return -1
        # Convert while the callback result still owns its counted lease.
        number: float = py_float_to_f64(load_ptr(result_slot, 0))
        if py_err_occurred() != 0:
            return -1
        if conversion == 9 and number != 0.0 and number == number * 2.0:
            # Raw bigint conversion produces infinity on overflow. Only the
            # integer fallback must reject it; __float__ may return infinity.
            py_raise_owned(py_exc_new(15, cstr("int too large to convert to float")))
            return -1
        if ptr_is_null(scalar) == 0:
            store_f64(scalar, 0, number)
        return 0
    if ptr_is_null(scalar) == 0:
        store_i64(scalar, 0, value)
    if py_err_occurred() != 0:
        return -1
    return 0


def _named_body(slots, tokens, borrowed, name, rname, nargs: int, mode: int, conversion: int, scalar, handled) -> int:
    index: int = 0
    while index < _NAMED_BORROWED_COUNT:
        token: int = pcc_gc_root_copy_borrowed_lease(
            ptr_add(slots, (_NAMED_RECEIVER + index) * C_POINTER_SIZE),
            ptr_add(borrowed, index * C_POINTER_SIZE),
        )
        if token < 0:
            py_runtime_error_if_unset(cstr("named protocol"), cstr("input owner copy failed"))
            return -1
        store_i64(tokens, (_NAMED_RECEIVER + index) * C_POINTER_SIZE, token)
        index = index + 1
    receiver_slot = ptr_add(slots, _NAMED_RECEIVER * C_POINTER_SIZE)
    result_slot = ptr_add(slots, _NAMED_RESULT * C_POINTER_SIZE)
    status: int = 0
    if ptr_is_null(rname) == 0 and nargs == 2:
        status = py_obj_ternary_special_call_slots(
            receiver_slot, ptr_add(slots, _NAMED_ARG0 * C_POINTER_SIZE),
            ptr_add(slots, _NAMED_ARG1 * C_POINTER_SIZE),
            name, rname, result_slot, handled,
        )
    elif ptr_is_null(rname) == 0:
        status = py_obj_binary_special_call_slots(
            receiver_slot, ptr_add(slots, _NAMED_ARG0 * C_POINTER_SIZE),
            name, rname, mode, result_slot, handled,
        )
    else:
        args_slot = null()
        if nargs != 0:
            args_slot = ptr_add(slots, _NAMED_ARGS * C_POINTER_SIZE)
            store_ptr(args_slot, 0, py_tuple_new(nargs))
            if _named_adopt(slots, tokens, _NAMED_ARGS) != 0:
                return -1
            if ptr_is_null(load_ptr(args_slot, 0)) != 0:
                py_runtime_error_if_unset(cstr("named protocol"), cstr("argument tuple allocation failed"))
                return -1
            index = 0
            while index < nargs:
                py_tuple_set_item(load_ptr(args_slot, 0), index, load_ptr(slots, (_NAMED_ARG0 + index) * C_POINTER_SIZE))
                if py_err_occurred() != 0:
                    return -1
                index = index + 1
        status = py_obj_special_call_slots(receiver_slot, name, args_slot, null(), result_slot, handled)
        if status == 0 and conversion == 8 and load_i64(handled, 0) == 0:
            # float falls back only on absence, never on a selected method's
            # error or invalid result. Keep the same receiver owner across
            # both lookups; instance attributes are deliberately bypassed.
            status = py_obj_special_call_slots(receiver_slot, cstr("__index__"), null(), null(), result_slot, handled)
            conversion = 9
    if status != 0:
        return -1
    if load_i64(handled, 0) == 0:
        return 0
    if mode == 2 and load_i64(handled, 0) == 2:
        # Both ordering descriptors returned NotImplemented. There is no
        # result owner to adopt or truth-convert; preserve the marker through
        # the ordinary frame/TLS cleanup for the comparison entrypoint.
        return 0
    if _named_adopt(slots, tokens, _NAMED_RESULT) != 0:
        return -1
    if ptr_is_null(load_ptr(result_slot, 0)) != 0:
        py_runtime_error_if_unset(cstr("named protocol"), cstr("selected callback returned NULL"))
        return -1
    if conversion == 7 and ptr_eq(load_ptr(result_slot, 0), global_load_ptr("py_NotImplemented")) != 0:
        _named_drop(slots, tokens, _NAMED_RESULT)
        store_i64(handled, 0, 0)
        return 0
    return _named_convert(slots, conversion, scalar)


def _named_dispatch(obj, arg0, arg1, name, rname, nargs: int, mode: int, conversion: int, scalar, handled):
    borrowed = stack_alloc(_NAMED_BORROWED_COUNT * C_POINTER_SIZE)
    store_ptr(borrowed, 0, obj)
    store_ptr(borrowed, C_POINTER_SIZE, arg0)
    store_ptr(borrowed, 2 * C_POINTER_SIZE, arg1)
    pcc_gc_frame_enter(global_addr("pcc_named_protocol_borrowed_map"), borrowed)
    slots = stack_alloc(_NAMED_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_NAMED_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _NAMED_COUNT * C_POINTER_SIZE)
    memset(tokens, 0, _NAMED_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_named_protocol_owned_map"), slots)
    local_handled = stack_alloc(8)
    store_i64(local_handled, 0, 1)
    if ptr_is_null(scalar) == 0:
        store_i64(scalar, 0, 0)
    py_tls_exc_swap_slot(ptr_add(slots, _NAMED_OLD_ERROR * C_POINTER_SIZE))
    status: int = _named_body(slots, tokens, borrowed, name, rname, nargs, mode, conversion, scalar, local_handled)
    if status != 0:
        py_runtime_error_if_unset(cstr("named protocol"), cstr("protocol dispatch failed without an exception"))
        if ptr_is_null(scalar) == 0:
            store_i64(scalar, 0, 0)
        store_i64(local_handled, 0, 1)
    _write_handled(handled, load_i64(local_handled, 0))
    py_tls_exc_swap_slot(ptr_add(slots, _NAMED_ERROR * C_POINTER_SIZE))
    memset(borrowed, 0, _NAMED_BORROWED_COUNT * C_POINTER_SIZE)
    index: int = _NAMED_ARGS
    while index >= _NAMED_RECEIVER:
        _named_drop(slots, tokens, index)
        index = index - 1
    if status != 0 or (conversion != 0 and conversion != 6 and conversion != 7):
        _named_drop(slots, tokens, _NAMED_RESULT)
    py_clear_exception()
    if ptr_is_null(load_ptr(slots, _NAMED_ERROR * C_POINTER_SIZE)) == 0:
        pcc_gc_store_root(ptr_add(slots, _NAMED_OLD_ERROR * C_POINTER_SIZE), null())
        py_clear_exception()
        py_tls_exc_swap_slot(ptr_add(slots, _NAMED_ERROR * C_POINTER_SIZE))
    else:
        py_tls_exc_swap_slot(ptr_add(slots, _NAMED_OLD_ERROR * C_POINTER_SIZE))
    result_slot = ptr_add(slots, _NAMED_RESULT * C_POINTER_SIZE)
    prior: int = _protocol_unary_pin_result(result_slot)
    if pcc_gc_foreign_lease_release(result_slot, load_i64(tokens, _NAMED_RESULT * C_POINTER_SIZE)) != 0:
        pcc_platform_abort()
        return null()
    pcc_gc_frame_leave(slots)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(result_slot, prior)


@c_abi_export("py_user_special_dispatch")
def py_user_special_dispatch(obj, name, arg0, arg1, nargs: int, conversion: int, scalar, handled):
    """Leased raw operands to owning named dispatch; 0/6/7 return NEW results.

    Scalar conversions: 1 length, 2 truth, 3 bool, 4 hash, 5 discard.
    Conversion 6 validates a string; 7 treats NotImplemented as absence.
    Conversion 8 validates __float__, with __index__ fallback, into an f64.
    An absent method is handled=0; selected
    None, descriptor errors and callback failures all remain handled=1.
    """
    return _named_dispatch(obj, arg0, arg1, name, null(), nargs, 0, conversion, scalar, handled)


def _named_binary(a, b, name, rname, mode: int, conversion: int, scalar, handled):
    return _named_dispatch(a, b, null(), name, rname, 1, mode, conversion, scalar, handled)
