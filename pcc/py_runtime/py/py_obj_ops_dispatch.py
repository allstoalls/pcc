"""Phase 4c.15a: pcc-Python port of py_obj_ops_dispatch.c.

Type-tag dispatch for the simpler generic ops. The compare/hash half
stays in py_obj_ops_compare.c — porting FNV-1a + bignum cmp to pcc-
Python is subtle and deferred.

Public object layouts and type tags come from generated C-header-derived
constants.  This module has private operation tables, but it must not carry a
second numeric copy of the public object ABI in its docstring.
"""

__pcc_runtime_port__ = True

from pcc.extern import extern, c_abi_export, c_int32, c_ptr, c_int64, c_void, c_double
from pcc.py_runtime.py.py_abi_constants import (
    C_POINTER_SIZE,
    PYCLASSOBJECT_MRO_OFFSET,
    PYCLASSOBJECT_NAME_OFFSET,
    PYCLASSOBJECT_N_MRO_OFFSET,
    PYINSTANCEOBJECT_CLS_OFFSET,
    PY_TYPE_CONTINUATION,
    PY_TYPE_VIRTUAL_THREAD,
    PY_TYPE_VTHREAD_CHANNEL,
)
from pcc.py_runtime.py.py_abi_constants import (
    PY_TYPE_BOOL,
    PY_TYPE_BYTEARRAY,
    PY_TYPE_BYTES,
    PY_TYPE_CLASS,
    PY_TYPE_COMPLEX,
    PY_TYPE_COROUTINE,
    PY_TYPE_DICT,
    PY_TYPE_EXC,
    PY_TYPE_FLOAT,
    PY_TYPE_FUNC,
    PY_TYPE_INSTANCE,
    PY_TYPE_INT,
    PY_TYPE_LIST,
    PY_TYPE_MEMORYVIEW,
    PY_TYPE_NONE,
    PY_TYPE_SET,
    PY_TYPE_STR,
    PY_TYPE_TUPLE,
    PY_TYPE_USER_CLASS_START,
    PY_TYPE_WEAKREF,
)
from pcc.unsafe import (
    call_ptr1,
    call_ptr2,
    cstr,
    define_global_ptr_null,
    free,
    global_load_ptr,
    global_store_ptr,
    is_tagged_int,
    load_i8,
    load_i32,
    load_i64,
    load_i8,
    load_ptr,
    malloc,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    store_i64,
    store_i8,
    strlen,
)

py_int_value_i64 = extern("py_int_value_i64", (c_ptr,), c_int64)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_obj_index_i64 = extern("py_obj_index_i64", (c_ptr,), c_int64)

py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_str_len = extern("py_str_len", (c_ptr,), c_int64)
py_str_index = extern("py_str_index", (c_ptr, c_ptr), c_ptr)
py_str_count = extern("py_str_count", (c_ptr, c_ptr), c_int64)
py_str_count_range = extern(
    "py_str_count_range", (c_ptr, c_ptr, c_ptr, c_ptr), c_int64
)
py_list_len = extern("py_list_len", (c_ptr,), c_int64)
py_list_get = extern("py_list_get", (c_ptr, c_int64), c_ptr)
py_list_set = extern("py_list_set", (c_ptr, c_int64, c_ptr), c_void)
py_list_setitem = extern("py_list_setitem", (c_ptr, c_int64, c_ptr), c_int64)
py_list_pop = extern("py_list_pop", (c_ptr, c_int64), c_ptr)
py_list_del_slice = extern("py_list_del_slice", (c_ptr, c_ptr, c_ptr, c_ptr), c_int64)
py_list_concat = extern("py_list_concat", (c_ptr, c_ptr), c_ptr)
py_list_new = extern("py_list_new", (c_int64,), c_ptr)
py_list_extend = extern("py_list_extend", (c_ptr, c_ptr), c_void)

py_tuple_get = extern("py_tuple_get", (c_ptr, c_int64), c_ptr)
py_tuple_len = extern("py_tuple_len", (c_ptr,), c_int64)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_tuple_concat = extern("py_tuple_concat", (c_ptr, c_ptr), c_ptr)
py_tuple_from_list = extern("py_tuple_from_list", (c_ptr,), c_ptr)

py_dict_get = extern("py_dict_get", (c_ptr, c_ptr), c_ptr)
py_dict_set = extern("py_dict_set", (c_ptr, c_ptr, c_ptr), c_void)
py_dict_del = extern("py_dict_del", (c_ptr, c_ptr), c_int64)
py_dict_len = extern("py_dict_len", (c_ptr,), c_int64)
py_dict_new = extern("py_dict_new", (), c_ptr)
py_dict_update = extern("py_dict_update", (c_ptr, c_ptr), c_void)

py_set_len = extern("py_set_len", (c_ptr,), c_int64)
py_set_pop = extern("py_set_pop", (c_ptr,), c_ptr)
py_set_new = extern("py_set_new", (), c_ptr)
py_set_update = extern("py_set_update", (c_ptr, c_ptr), c_void)
py_set_intersection = extern("py_set_intersection", (c_ptr, c_ptr), c_ptr)
py_set_symmetric_difference = extern(
    "py_set_symmetric_difference", (c_ptr, c_ptr), c_ptr
)

py_class_new = extern("py_class_new", (c_ptr, c_ptr, c_int32, c_ptr, c_int32), c_ptr)
py_class_lookup = extern("py_class_lookup", (c_ptr, c_ptr), c_ptr)
py_class_getattr = extern("py_class_getattr", (c_ptr, c_ptr), c_ptr)
py_class_setattr = extern("py_class_setattr", (c_ptr, c_ptr, c_ptr), c_int64)
py_class_delattr = extern("py_class_delattr", (c_ptr, c_ptr), c_int64)
py_instance_new = extern("py_instance_new", (c_ptr,), c_ptr)
py_instance_getattr = extern("py_instance_getattr", (c_ptr, c_ptr), c_ptr)
py_instance_getattr_default = extern(
    "py_instance_getattr_default", (c_ptr, c_ptr), c_ptr
)
py_instance_setattr = extern("py_instance_setattr", (c_ptr, c_ptr, c_ptr), c_int64)
py_instance_delattr = extern("py_instance_delattr", (c_ptr, c_ptr), c_int64)
py_isinstance = extern("py_isinstance", (c_ptr, c_ptr), c_int64)
py_exc_builtin_class = extern("py_exc_builtin_class", (c_int64,), c_ptr)
py_user_len_dispatch = extern("py_user_len_dispatch", (c_ptr, c_ptr), c_int64)
py_user_bool_dispatch = extern("py_user_bool_dispatch", (c_ptr, c_ptr), c_int64)
py_user_getitem_dispatch = extern("py_user_getitem_dispatch", (c_ptr, c_ptr), c_ptr)
py_user_setitem_dispatch = extern(
    "py_user_setitem_dispatch", (c_ptr, c_ptr, c_ptr, c_ptr), c_int64
)
py_user_delitem_dispatch = extern(
    "py_user_delitem_dispatch", (c_ptr, c_ptr, c_ptr), c_int64
)
py_func_call = extern("py_func_call", (c_ptr, c_ptr), c_ptr)
py_func_call_kwargs = extern("py_func_call_kwargs", (c_ptr, c_ptr, c_ptr), c_ptr)
py_func_new_bound = extern("py_func_new_bound", (c_ptr, c_ptr, c_ptr, c_ptr), c_ptr)
py_func_get_code_metadata = extern("py_func_get_code_metadata", (c_ptr,), c_ptr)
py_func_get_defaults_metadata = extern(
    "py_func_get_defaults_metadata", (c_ptr,), c_ptr
)
py_weakref_call = extern("py_weakref_call", (c_ptr,), c_ptr)
pcc_capi_is_cext_type_tag = extern("pcc_capi_is_cext_type_tag", (c_int64,), c_int64)
pcc_capi_call_cext_object = extern(
    "pcc_capi_call_cext_object", (c_ptr, c_ptr, c_ptr), c_ptr
)
pcc_capi_cext_subtract = extern(
    "pcc_capi_cext_subtract", (c_ptr, c_ptr), c_ptr
)
pcc_capi_cext_binary_number = extern(
    "pcc_capi_cext_binary_number", (c_ptr, c_ptr, c_int64), c_ptr
)
pcc_capi_cext_truthy = extern("pcc_capi_cext_truthy", (c_ptr,), c_int64)
pcc_capi_cext_object_getattr = extern(
    "pcc_capi_cext_object_getattr", (c_ptr, c_ptr), c_ptr
)
pcc_capi_cext_object_setattr = extern(
    "pcc_capi_cext_object_setattr", (c_ptr, c_ptr, c_ptr), c_int64
)
pcc_capi_cext_object_getitem = extern(
    "pcc_capi_cext_object_getitem", (c_ptr, c_ptr), c_ptr
)
pcc_capi_cext_object_setitem = extern(
    "pcc_capi_cext_object_setitem", (c_ptr, c_ptr, c_ptr), c_int64
)
pcc_capi_cext_object_length = extern(
    "pcc_capi_cext_object_length", (c_ptr,), c_int64
)
pcc_capi_type_object_is_callable = extern(
    "pcc_capi_type_object_is_callable", (c_ptr,), c_int64
)
pcc_capi_is_type_object_value = extern(
    "pcc_capi_is_type_object_value", (c_ptr,), c_int64
)
pcc_capi_type_object_issubclass = extern(
    "pcc_capi_type_object_issubclass", (c_ptr, c_ptr), c_int64
)
pcc_capi_type_object_getattr = extern(
    "pcc_capi_type_object_getattr", (c_ptr, c_ptr), c_ptr
)
pcc_capi_builtin_object_getattr = extern(
    "pcc_capi_builtin_object_getattr", (c_ptr, c_ptr), c_ptr
)
pcc_capi_call_type_object = extern(
    "pcc_capi_call_type_object", (c_ptr, c_ptr, c_ptr), c_ptr
)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_exc_new_with_value = extern("py_exc_new_with_value", (c_int64, c_ptr), c_ptr)
py_raise = extern("py_raise", (c_ptr,), c_void)
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_runtime_error_if_unset = extern(
    "py_runtime_error_if_unset", (c_ptr, c_ptr), c_ptr
)
py_gen_send = extern("py_gen_send", (c_ptr, c_ptr), c_ptr)
py_user_binop_dispatch = extern(
    "py_user_binop_dispatch", (c_ptr, c_ptr, c_ptr, c_ptr, c_ptr), c_ptr
)
py_int_add = extern("py_int_add", (c_ptr, c_ptr), c_ptr)
py_int_sub = extern("py_int_sub", (c_ptr, c_ptr), c_ptr)
py_int_mul = extern("py_int_mul", (c_ptr, c_ptr), c_ptr)
py_int_and = extern("py_int_and", (c_ptr, c_ptr), c_ptr)
py_int_or = extern("py_int_or", (c_ptr, c_ptr), c_ptr)
py_int_xor = extern("py_int_xor", (c_ptr, c_ptr), c_ptr)
py_int_shl = extern("py_int_shl", (c_ptr, c_ptr), c_ptr)
py_int_shr = extern("py_int_shr", (c_ptr, c_ptr), c_ptr)
py_float_add = extern("py_float_add", (c_ptr, c_ptr), c_ptr)
py_float_sub = extern("py_float_sub", (c_ptr, c_ptr), c_ptr)
py_float_mul = extern("py_float_mul", (c_ptr, c_ptr), c_ptr)
py_complex_add = extern("py_complex_add", (c_ptr, c_ptr), c_ptr)
py_str_repeat = extern("py_str_repeat", (c_ptr, c_ptr), c_ptr)
py_list_repeat = extern("py_list_repeat", (c_ptr, c_ptr), c_ptr)
py_tuple_repeat = extern("py_tuple_repeat", (c_ptr, c_ptr), c_ptr)
py_float_to_f64 = extern("py_float_to_f64", (c_ptr,), c_double)
py_float_from_f64 = extern("py_float_from_f64", (c_double,), c_ptr)
py_float_value_of = extern("py_float_value_of", (c_ptr,), c_double)
py_obj_str = extern("py_obj_str", (c_ptr,), c_ptr)
py_obj_truthy = extern("py_obj_truthy", (c_ptr,), c_int64)
py_bool_from_bit = extern("py_bool_from_bit", (c_int32,), c_ptr)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_int_from_cstr_or_raise = extern("py_int_from_cstr_or_raise", (c_ptr, c_int32), c_ptr)
py_str_concat = extern("py_str_concat", (c_ptr, c_ptr), c_ptr)
py_complex_real = extern("py_complex_real", (c_ptr,), c_ptr)
py_complex_imag = extern("py_complex_imag", (c_ptr,), c_ptr)
py_coroutine_class = extern("py_coroutine_class", (), c_ptr)
py_continuation_class = extern("py_continuation_class", (), c_ptr)
py_bytes_len = extern("py_bytes_len", (c_ptr,), c_int64)
py_bytes_getitem = extern("py_bytes_getitem", (c_ptr, c_ptr), c_ptr)
py_bytes_concat = extern("py_bytes_concat", (c_ptr, c_ptr), c_ptr)
py_bytearray_setitem = extern("py_bytearray_setitem", (c_ptr, c_ptr, c_ptr), c_int64)
py_bytearray_del_slice = extern(
    "py_bytearray_del_slice", (c_ptr, c_ptr, c_ptr, c_ptr), c_int64
)

py_decref = extern("py_decref", (c_ptr,), c_void)
py_incref = extern("py_incref", (c_ptr,), c_void)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_store_ptr = extern("pcc_gc_store_ptr", (c_ptr, c_ptr, c_ptr), c_void)
pcc_gc_note_relocation_read = extern(
    "pcc_gc_note_relocation_read",
    (c_ptr,),
    c_ptr,
)
pcc_runtime_log_event_code = extern(
    "pcc_runtime_log_event_code",
    (c_int32, c_int32, c_int64, c_int64, c_ptr),
    c_void,
)


define_global_ptr_null("pcc_type_cls_none")
define_global_ptr_null("pcc_type_cls_bool")
define_global_ptr_null("pcc_type_cls_int")
define_global_ptr_null("pcc_type_cls_float")
define_global_ptr_null("pcc_type_cls_str")
define_global_ptr_null("pcc_type_cls_list")
define_global_ptr_null("pcc_type_cls_dict")
define_global_ptr_null("pcc_type_cls_tuple")
define_global_ptr_null("pcc_type_cls_set")
define_global_ptr_null("pcc_type_cls_type")
define_global_ptr_null("pcc_type_cls_complex")
define_global_ptr_null("pcc_type_cls_bytes")
define_global_ptr_null("pcc_type_cls_bytearray")
define_global_ptr_null("pcc_type_cls_memoryview")
define_global_ptr_null("pcc_type_cls_coroutine")
define_global_ptr_null("pcc_type_cls_object")
define_global_ptr_null("pcc_type_cls_super")
define_global_ptr_null("pcc_slice_cls")


def _cstr_is_dunder_class(s) -> int:
    if strlen(s) != 9:
        return 0
    if load_i8(s, 0) != 95:
        return 0
    if load_i8(s, 1) != 95:
        return 0
    if load_i8(s, 2) != 99:
        return 0
    if load_i8(s, 3) != 108:
        return 0
    if load_i8(s, 4) != 97:
        return 0
    if load_i8(s, 5) != 115:
        return 0
    if load_i8(s, 6) != 115:
        return 0
    if load_i8(s, 7) != 95:
        return 0
    if load_i8(s, 8) != 95:
        return 0
    return 1


def _cstr_is_dunder_name(s) -> int:
    if strlen(s) != 8:
        return 0
    if load_i8(s, 0) != 95:
        return 0
    if load_i8(s, 1) != 95:
        return 0
    if load_i8(s, 2) != 110:
        return 0
    if load_i8(s, 3) != 97:
        return 0
    if load_i8(s, 4) != 109:
        return 0
    if load_i8(s, 5) != 101:
        return 0
    if load_i8(s, 6) != 95:
        return 0
    if load_i8(s, 7) != 95:
        return 0
    return 1


def _cstr_is_dunder_qualname(s) -> int:
    if strlen(s) != 12:
        return 0
    if load_i8(s, 0) != 95:
        return 0
    if load_i8(s, 1) != 95:
        return 0
    if load_i8(s, 2) != 113:
        return 0
    if load_i8(s, 3) != 117:
        return 0
    if load_i8(s, 4) != 97:
        return 0
    if load_i8(s, 5) != 108:
        return 0
    if load_i8(s, 6) != 110:
        return 0
    if load_i8(s, 7) != 97:
        return 0
    if load_i8(s, 8) != 109:
        return 0
    if load_i8(s, 9) != 101:
        return 0
    if load_i8(s, 10) != 95:
        return 0
    if load_i8(s, 11) != 95:
        return 0
    return 1


def _cstr_is_dunder_doc(s) -> int:
    if strlen(s) != 7:
        return 0
    if load_i8(s, 0) != 95:
        return 0
    if load_i8(s, 1) != 95:
        return 0
    if load_i8(s, 2) != 100:
        return 0
    if load_i8(s, 3) != 111:
        return 0
    if load_i8(s, 4) != 99:
        return 0
    if load_i8(s, 5) != 95:
        return 0
    if load_i8(s, 6) != 95:
        return 0
    return 1


def _cstr_is_dunder_code(s) -> int:
    if strlen(s) != 8:
        return 0
    if load_i8(s, 0) != 95:
        return 0
    if load_i8(s, 1) != 95:
        return 0
    if load_i8(s, 2) != 99:
        return 0
    if load_i8(s, 3) != 111:
        return 0
    if load_i8(s, 4) != 100:
        return 0
    if load_i8(s, 5) != 101:
        return 0
    if load_i8(s, 6) != 95:
        return 0
    if load_i8(s, 7) != 95:
        return 0
    return 1


def _cstr_is_dunder_defaults(s) -> int:
    if strlen(s) != 12:
        return 0
    if load_i8(s, 0) != 95 or load_i8(s, 1) != 95:
        return 0
    if load_i8(s, 2) != 100 or load_i8(s, 3) != 101:
        return 0
    if load_i8(s, 4) != 102 or load_i8(s, 5) != 97:
        return 0
    if load_i8(s, 6) != 117 or load_i8(s, 7) != 108:
        return 0
    if load_i8(s, 8) != 116 or load_i8(s, 9) != 115:
        return 0
    if load_i8(s, 10) != 95 or load_i8(s, 11) != 95:
        return 0
    return 1


def _cstr_is_dunder_self(s) -> int:
    if strlen(s) != 8:
        return 0
    if load_i8(s, 0) != 95:
        return 0
    if load_i8(s, 1) != 95:
        return 0
    if load_i8(s, 2) != 115:
        return 0
    if load_i8(s, 3) != 101:
        return 0
    if load_i8(s, 4) != 108:
        return 0
    if load_i8(s, 5) != 102:
        return 0
    if load_i8(s, 6) != 95:
        return 0
    if load_i8(s, 7) != 95:
        return 0
    return 1


def _cstr_is_dunder_cause(s) -> int:
    if strlen(s) != 9:
        return 0
    if load_i8(s, 0) != 95:
        return 0
    if load_i8(s, 1) != 95:
        return 0
    if load_i8(s, 2) != 99:
        return 0
    if load_i8(s, 3) != 97:
        return 0
    if load_i8(s, 4) != 117:
        return 0
    if load_i8(s, 5) != 115:
        return 0
    if load_i8(s, 6) != 101:
        return 0
    if load_i8(s, 7) != 95:
        return 0
    if load_i8(s, 8) != 95:
        return 0
    return 1


def _cstr_is_dunder_context(s) -> int:
    if strlen(s) != 11:
        return 0
    if load_i8(s, 0) != 95:
        return 0
    if load_i8(s, 1) != 95:
        return 0
    if load_i8(s, 2) != 99:
        return 0
    if load_i8(s, 3) != 111:
        return 0
    if load_i8(s, 4) != 110:
        return 0
    if load_i8(s, 5) != 116:
        return 0
    if load_i8(s, 6) != 101:
        return 0
    if load_i8(s, 7) != 120:
        return 0
    if load_i8(s, 8) != 116:
        return 0
    if load_i8(s, 9) != 95:
        return 0
    if load_i8(s, 10) != 95:
        return 0
    return 1


def _cstr_is_value(s) -> int:
    if strlen(s) != 5:
        return 0
    if load_i8(s, 0) != 118:
        return 0
    if load_i8(s, 1) != 97:
        return 0
    if load_i8(s, 2) != 108:
        return 0
    if load_i8(s, 3) != 117:
        return 0
    if load_i8(s, 4) != 101:
        return 0
    return 1


def _cstr_is_msg(s) -> int:
    if strlen(s) != 3:
        return 0
    if load_i8(s, 0) != 109:
        return 0
    if load_i8(s, 1) != 115:
        return 0
    if load_i8(s, 2) != 103:
        return 0
    return 1


def _cstr_is_args(s) -> int:
    if strlen(s) != 4:
        return 0
    if load_i8(s, 0) != 97:  # 'a'
        return 0
    if load_i8(s, 1) != 114:  # 'r'
        return 0
    if load_i8(s, 2) != 103:  # 'g'
        return 0
    if load_i8(s, 3) != 115:  # 's'
        return 0
    return 1


def _cstr_is_real(s) -> int:
    if strlen(s) != 4:
        return 0
    if load_i8(s, 0) != 114:
        return 0
    if load_i8(s, 1) != 101:
        return 0
    if load_i8(s, 2) != 97:
        return 0
    if load_i8(s, 3) != 108:
        return 0
    return 1


def _cstr_is_imag(s) -> int:
    if strlen(s) != 4:
        return 0
    if load_i8(s, 0) != 105:
        return 0
    if load_i8(s, 1) != 109:
        return 0
    if load_i8(s, 2) != 97:
        return 0
    if load_i8(s, 3) != 103:
        return 0
    return 1


def _cstr_is_send(s) -> int:
    if strlen(s) != 4:
        return 0
    if load_i8(s, 0) != 115:
        return 0
    if load_i8(s, 1) != 101:
        return 0
    if load_i8(s, 2) != 110:
        return 0
    if load_i8(s, 3) != 100:
        return 0
    return 1


def _cstr_is_pop(s) -> int:
    if strlen(s) != 3:
        return 0
    if load_i8(s, 0) != 112:
        return 0
    if load_i8(s, 1) != 111:
        return 0
    if load_i8(s, 2) != 112:
        return 0
    return 1


def _cstr_is_count(s) -> int:
    if strlen(s) != 5:
        return 0
    if load_i8(s, 0) != 99:  # 'c'
        return 0
    if load_i8(s, 1) != 111:  # 'o'
        return 0
    if load_i8(s, 2) != 117:  # 'u'
        return 0
    if load_i8(s, 3) != 110:  # 'n'
        return 0
    if load_i8(s, 4) != 116:  # 't'
        return 0
    return 1


def _type_of(o) -> int:
    if is_tagged_int(o) != 0:
        return PY_TYPE_INT  # PY_TYPE_INT
    return load_i32(o, 8)


@c_abi_export("py_obj_truthy")
def py_obj_truthy(o) -> int:
    if ptr_is_null(o) != 0:
        return 0
    if ptr_eq(o, global_load_ptr("py_None")) != 0:
        return 0
    if ptr_eq(o, global_load_ptr("py_False")) != 0:
        return 0
    if ptr_eq(o, global_load_ptr("py_True")) != 0:
        return 1
    if is_tagged_int(o) != 0:
        if py_int_value_i64(o) != 0:
            return 1
        return 0
    tag: int = load_i32(o, 8)
    if pcc_capi_is_cext_type_tag(tag) != 0:
        cext_truth: int = pcc_capi_cext_truthy(o)
        if cext_truth > 0:
            return 1
        return 0
    if tag == PY_TYPE_INT:  # PY_TYPE_INT
        if py_int_value_i64(o) != 0:
            return 1
        return 0
    if tag == PY_TYPE_FLOAT:  # PY_TYPE_FLOAT — read i64 bits at offset 16
        # Only the two signed-zero encodings have zero magnitude bits.
        if (load_i64(o, 16) & 0x7fffffffffffffff) != 0:
            return 1
        return 0
    if tag == PY_TYPE_LIST:  # PY_TYPE_LIST — length@16
        if load_i64(o, 16) != 0:
            return 1
        return 0
    if tag == PY_TYPE_TUPLE:  # PY_TYPE_TUPLE — len@16
        if load_i64(o, 16) != 0:
            return 1
        return 0
    if tag == PY_TYPE_STR:  # PY_TYPE_STR — byte_len@16
        if load_i64(o, 16) != 0:
            return 1
        return 0
    if tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY or tag == PY_TYPE_MEMORYVIEW:
        if py_bytes_len(o) != 0:
            return 1
        return 0
    if tag == PY_TYPE_DICT:  # PY_TYPE_DICT — size@16
        if load_i64(o, 16) != 0:
            return 1
        return 0
    if tag == PY_TYPE_SET:  # PY_TYPE_SET — size@16
        if load_i64(o, 16) != 0:
            return 1
        return 0
    if _is_instance_tag(tag) != 0:
        handled = malloc(8)
        if ptr_is_null(handled) == 0:
            store_i64(handled, 0, 0)
            user_bool: int = py_user_bool_dispatch(o, handled)
            if load_i64(handled, 0) != 0:
                free(handled)
                if user_bool != 0:
                    return 1
                return 0
            store_i64(handled, 0, 0)
            user_len: int = py_user_len_dispatch(o, handled)
            if load_i64(handled, 0) != 0:
                free(handled)
                if user_len != 0:
                    return 1
                return 0
            free(handled)
    return 1


@c_abi_export("py_obj_type_tag")
def py_obj_type_tag(o) -> int:
    if ptr_is_null(o) != 0:
        return -1
    return _type_of(o)


@c_abi_export("py_obj_add")
def py_obj_add(a, b):
    if ptr_is_null(a) != 0 or ptr_is_null(b) != 0:
        py_raise_owned(py_exc_new(3, cstr("unsupported operand type(s) for +")))
        return null()
    at: int = _type_of(a)
    bt: int = _type_of(b)
    # C-extension slots take precedence over builtin numeric fast paths for
    # mixed operands (for example ndarray + native float).
    if (
        pcc_capi_is_cext_type_tag(at) != 0
        or pcc_capi_is_cext_type_tag(bt) != 0
    ):
        return pcc_capi_cext_binary_number(a, b, 0)
    if (at == PY_TYPE_INT or at == PY_TYPE_BOOL) and (bt == PY_TYPE_INT or bt == PY_TYPE_BOOL):
        return py_int_add(a, b)
    if at == PY_TYPE_COMPLEX or bt == PY_TYPE_COMPLEX:
        return py_complex_add(a, b)
    if at == PY_TYPE_FLOAT or bt == PY_TYPE_FLOAT:
        return py_float_add(a, b)
    if at == PY_TYPE_STR and bt == PY_TYPE_STR:
        return py_str_concat(a, b)
    if (at == PY_TYPE_BYTES or at == PY_TYPE_BYTEARRAY) and (bt == PY_TYPE_BYTES or bt == PY_TYPE_BYTEARRAY):
        return py_bytes_concat(a, b)
    if at == PY_TYPE_LIST and bt == PY_TYPE_LIST:
        return py_list_concat(a, b)
    if at == PY_TYPE_TUPLE and bt == PY_TYPE_TUPLE:
        return py_tuple_concat(a, b)
    if (
        at == PY_TYPE_INSTANCE
        or at >= PY_TYPE_USER_CLASS_START
        or bt == PY_TYPE_INSTANCE
        or bt >= PY_TYPE_USER_CLASS_START
    ):
        return py_user_binop_dispatch(
            a,
            b,
            cstr("__add__"),
            cstr("__radd__"),
            cstr("unsupported operand type(s) for +"),
        )
    py_raise_owned(py_exc_new(3, cstr("unsupported operand type(s) for +")))
    return null()


@c_abi_export("py_obj_sub")
def py_obj_sub(a, b):
    # Generic ``a - b`` for dynamically-typed operands. Mirrors py_obj_add:
    # int/bool -> py_int_sub (bignum); any float -> py_float_sub (coerces the
    # other numeric operand). Subtraction is numeric-only in Python. Fixes
    # boxed-float ``-`` (e.g. ``obj.attr - n`` where attr is a float) which fell
    # to the i64 path and misread the boxed pointer.
    if ptr_is_null(a) != 0 or ptr_is_null(b) != 0:
        py_raise_owned(py_exc_new(3, cstr("unsupported operand type(s) for -")))
        return null()
    at: int = _type_of(a)
    bt: int = _type_of(b)
    if (at == PY_TYPE_INT or at == PY_TYPE_BOOL) and (bt == PY_TYPE_INT or bt == PY_TYPE_BOOL):
        return py_int_sub(a, b)
    if (at == PY_TYPE_FLOAT or at == PY_TYPE_INT or at == PY_TYPE_BOOL) and (bt == PY_TYPE_FLOAT or bt == PY_TYPE_INT or bt == PY_TYPE_BOOL):
        return py_float_sub(a, b)
    if (
        pcc_capi_is_cext_type_tag(at) != 0
        or pcc_capi_is_cext_type_tag(bt) != 0
    ):
        return pcc_capi_cext_subtract(a, b)
    if (
        at == PY_TYPE_INSTANCE
        or at >= PY_TYPE_USER_CLASS_START
        or bt == PY_TYPE_INSTANCE
        or bt >= PY_TYPE_USER_CLASS_START
    ):
        return py_user_binop_dispatch(
            a,
            b,
            cstr("__sub__"),
            cstr("__rsub__"),
            cstr("unsupported operand type(s) for -"),
        )
    py_raise_owned(py_exc_new(3, cstr("unsupported operand type(s) for -")))
    return null()


@c_abi_export("py_obj_mul")
def py_obj_mul(a, b):
    # Generic ``a * b`` for dynamically-typed operands. int/bool -> py_int_mul;
    # any-float-numeric -> py_float_mul; sequence * int -> repetition (str via
    # py_str_repeat which takes a PyObject count, list/tuple via py_*_repeat
    # which take an i64 count -> unbox with py_int_value_i64). Fixes boxed-float
    # ``*`` (``obj.attr * n``) which fell to the i64 path.
    if ptr_is_null(a) != 0 or ptr_is_null(b) != 0:
        py_raise_owned(py_exc_new(3, cstr("unsupported operand type(s) for *")))
        return null()
    at: int = _type_of(a)
    bt: int = _type_of(b)
    if (at == PY_TYPE_INT or at == PY_TYPE_BOOL) and (bt == PY_TYPE_INT or bt == PY_TYPE_BOOL):
        return py_int_mul(a, b)
    if (at == PY_TYPE_FLOAT or at == PY_TYPE_INT or at == PY_TYPE_BOOL) and (bt == PY_TYPE_FLOAT or bt == PY_TYPE_INT or bt == PY_TYPE_BOOL):
        return py_float_mul(a, b)
    if at == PY_TYPE_STR and (bt == PY_TYPE_BOOL or bt == PY_TYPE_INT):
        return py_str_repeat(a, b)
    if bt == PY_TYPE_STR and (at == PY_TYPE_BOOL or at == PY_TYPE_INT):
        return py_str_repeat(b, a)
    if at == PY_TYPE_LIST and (bt == PY_TYPE_BOOL or bt == PY_TYPE_INT):
        return py_list_repeat(a, py_int_value_i64(b))
    if bt == PY_TYPE_LIST and (at == PY_TYPE_BOOL or at == PY_TYPE_INT):
        return py_list_repeat(b, py_int_value_i64(a))
    if at == PY_TYPE_TUPLE and (bt == PY_TYPE_BOOL or bt == PY_TYPE_INT):
        return py_tuple_repeat(a, py_int_value_i64(b))
    if bt == PY_TYPE_TUPLE and (at == PY_TYPE_BOOL or at == PY_TYPE_INT):
        return py_tuple_repeat(b, py_int_value_i64(a))
    if (
        pcc_capi_is_cext_type_tag(at) != 0
        or pcc_capi_is_cext_type_tag(bt) != 0
    ):
        return pcc_capi_cext_binary_number(a, b, 2)
    if (
        at == PY_TYPE_INSTANCE
        or at >= PY_TYPE_USER_CLASS_START
        or bt == PY_TYPE_INSTANCE
        or bt >= PY_TYPE_USER_CLASS_START
    ):
        return py_user_binop_dispatch(
            a,
            b,
            cstr("__mul__"),
            cstr("__rmul__"),
            cstr("unsupported operand type(s) for *"),
        )
    py_raise_owned(py_exc_new(3, cstr("unsupported operand type(s) for *")))
    return null()


def _py_obj_bitwise_dispatch(a, b, op: int):
    if ptr_is_null(a) != 0 or ptr_is_null(b) != 0:
        if op == 0:
            py_raise_owned(py_exc_new(3, cstr("unsupported operand type(s) for &")))
        elif op == 1:
            py_raise_owned(py_exc_new(3, cstr("unsupported operand type(s) for |")))
        else:
            py_raise_owned(py_exc_new(3, cstr("unsupported operand type(s) for ^")))
        return null()
    at: int = _type_of(a)
    bt: int = _type_of(b)
    if (
        pcc_capi_is_cext_type_tag(at) != 0
        or pcc_capi_is_cext_type_tag(bt) != 0
    ):
        cext_op: int = 8
        if op == 1:
            cext_op = 10
        elif op == 2:
            cext_op = 9
        return pcc_capi_cext_binary_number(a, b, cext_op)
    if (at == PY_TYPE_INT or at == PY_TYPE_BOOL) and (bt == PY_TYPE_INT or bt == PY_TYPE_BOOL):
        if op == 0:
            return py_int_and(a, b)
        if op == 1:
            return py_int_or(a, b)
        return py_int_xor(a, b)
    if at == PY_TYPE_SET and bt == PY_TYPE_SET:
        if op == 0:
            return py_set_intersection(a, b)
        if op == 2:
            return py_set_symmetric_difference(a, b)
        out = py_set_new()
        if ptr_is_null(out) != 0:
            return null()
        py_set_update(out, a)
        if py_err_occurred() != 0:
            py_decref(out)
            return null()
        py_set_update(out, b)
        if py_err_occurred() != 0:
            py_decref(out)
            return null()
        return out
    if op == 1 and at == PY_TYPE_DICT and bt == PY_TYPE_DICT:
        out = py_dict_new()
        if ptr_is_null(out) != 0:
            return null()
        py_dict_update(out, a)
        if py_err_occurred() != 0:
            py_decref(out)
            return null()
        py_dict_update(out, b)
        if py_err_occurred() != 0:
            py_decref(out)
            return null()
        return out
    if (
        at == PY_TYPE_INSTANCE
        or at >= PY_TYPE_USER_CLASS_START
        or bt == PY_TYPE_INSTANCE
        or bt >= PY_TYPE_USER_CLASS_START
    ):
        if op == 0:
            return py_user_binop_dispatch(
                a,
                b,
                cstr("__and__"),
                cstr("__rand__"),
                cstr("unsupported operand type(s) for &"),
            )
        if op == 1:
            return py_user_binop_dispatch(
                a,
                b,
                cstr("__or__"),
                cstr("__ror__"),
                cstr("unsupported operand type(s) for |"),
            )
        return py_user_binop_dispatch(
            a,
            b,
            cstr("__xor__"),
            cstr("__rxor__"),
            cstr("unsupported operand type(s) for ^"),
        )
    if op == 0:
        py_raise_owned(py_exc_new(3, cstr("unsupported operand type(s) for &")))
    elif op == 1:
        py_raise_owned(py_exc_new(3, cstr("unsupported operand type(s) for |")))
    else:
        py_raise_owned(py_exc_new(3, cstr("unsupported operand type(s) for ^")))
    return null()


@c_abi_export("py_obj_and")
def py_obj_and(a, b):
    return _py_obj_bitwise_dispatch(a, b, 0)


@c_abi_export("py_obj_or")
def py_obj_or(a, b):
    return _py_obj_bitwise_dispatch(a, b, 1)


@c_abi_export("py_obj_xor")
def py_obj_xor(a, b):
    return _py_obj_bitwise_dispatch(a, b, 2)


def _py_obj_shift_dispatch(a, b, op: int):
    if ptr_is_null(a) != 0 or ptr_is_null(b) != 0:
        if op == 0:
            py_raise_owned(py_exc_new(3, cstr("unsupported operand type(s) for <<")))
        else:
            py_raise_owned(py_exc_new(3, cstr("unsupported operand type(s) for >>")))
        return null()
    at: int = _type_of(a)
    bt: int = _type_of(b)
    if (
        pcc_capi_is_cext_type_tag(at) != 0
        or pcc_capi_is_cext_type_tag(bt) != 0
    ):
        if op == 0:
            return pcc_capi_cext_binary_number(a, b, 6)
        return pcc_capi_cext_binary_number(a, b, 7)
    if (at == PY_TYPE_INT or at == PY_TYPE_BOOL) and (bt == PY_TYPE_INT or bt == PY_TYPE_BOOL):
        if op == 0:
            return py_int_shl(a, b)
        return py_int_shr(a, b)
    if (
        at == PY_TYPE_INSTANCE
        or at >= PY_TYPE_USER_CLASS_START
        or bt == PY_TYPE_INSTANCE
        or bt >= PY_TYPE_USER_CLASS_START
    ):
        if op == 0:
            return py_user_binop_dispatch(
                a,
                b,
                cstr("__lshift__"),
                cstr("__rlshift__"),
                cstr("unsupported operand type(s) for <<"),
            )
        return py_user_binop_dispatch(
            a,
            b,
            cstr("__rshift__"),
            cstr("__rrshift__"),
            cstr("unsupported operand type(s) for >>"),
        )
    if op == 0:
        py_raise_owned(py_exc_new(3, cstr("unsupported operand type(s) for <<")))
    else:
        py_raise_owned(py_exc_new(3, cstr("unsupported operand type(s) for >>")))
    return null()


@c_abi_export("py_obj_lshift")
def py_obj_lshift(a, b):
    return _py_obj_shift_dispatch(a, b, 0)


@c_abi_export("py_obj_rshift")
def py_obj_rshift(a, b):
    return _py_obj_shift_dispatch(a, b, 1)


@c_abi_export("py_obj_truediv")
def py_obj_truediv(a, b):
    # Python true division (``a / b``) for dynamically-typed operands. Mirrors
    # py_obj_truediv in py_obj_ops_dispatch.c: numeric operands divide as
    # doubles (always producing a float, like CPython); anything else defers to
    # the ``__truediv__`` dunder. A tagged int has no ``__truediv__`` attribute,
    # so routing DynType ``/`` straight to the dunder raised AttributeError.
    if ptr_is_null(a) != 0 or ptr_is_null(b) != 0:
        py_raise_owned(py_exc_new(3, cstr("unsupported operand type(s) for /")))
        return null()
    at: int = _type_of(a)
    bt: int = _type_of(b)
    a_num: int = 0
    if at == PY_TYPE_INT or at == PY_TYPE_BOOL or at == PY_TYPE_FLOAT:
        a_num = 1
    b_num: int = 0
    if bt == PY_TYPE_INT or bt == PY_TYPE_BOOL or bt == PY_TYPE_FLOAT:
        b_num = 1
    if a_num == 1 and b_num == 1:
        bd: float = py_float_to_f64(b)
        if bd == 0.0:
            py_raise_owned(py_exc_new(9, cstr("division by zero")))
            return null()
        ad: float = py_float_to_f64(a)
        return py_float_from_f64(ad / bd)
    if (
        pcc_capi_is_cext_type_tag(at) != 0
        or pcc_capi_is_cext_type_tag(bt) != 0
    ):
        # 12 = true_divide in the port op table (3 is remainder there; the
        # old C shim table used 3 for true divide).
        return pcc_capi_cext_binary_number(a, b, 12)
    # Non-numeric: full dunder protocol (__truediv__, NotImplemented,
    # reflected __rtruediv__) — the old call_method1 defer only tried
    # the LHS.
    return py_user_binop_dispatch(
        a,
        b,
        cstr("__truediv__"),
        cstr("__rtruediv__"),
        cstr("unsupported operand type(s) for /"),
    )


def _type_name_cstr_for_tag(tag: int):
    if tag == PY_TYPE_NONE:
        return cstr("NoneType")
    if tag == PY_TYPE_BOOL:
        return cstr("bool")
    if tag == PY_TYPE_INT:
        return cstr("int")
    if tag == PY_TYPE_FLOAT:
        return cstr("float")
    if tag == PY_TYPE_STR:
        return cstr("str")
    if tag == PY_TYPE_LIST:
        return cstr("list")
    if tag == PY_TYPE_DICT:
        return cstr("dict")
    if tag == PY_TYPE_TUPLE:
        return cstr("tuple")
    if tag == PY_TYPE_SET:
        return cstr("set")
    if tag == PY_TYPE_CLASS:
        return cstr("type")
    if tag == PY_TYPE_COMPLEX:
        return cstr("complex")
    if tag == PY_TYPE_BYTES:
        return cstr("bytes")
    if tag == PY_TYPE_BYTEARRAY:
        return cstr("bytearray")
    if tag == PY_TYPE_MEMORYVIEW:
        return cstr("memoryview")
    if tag == PY_TYPE_COROUTINE:
        return cstr("coroutine")
    if tag == PY_TYPE_CONTINUATION:
        return cstr("continuation")
    if tag == PY_TYPE_VIRTUAL_THREAD:
        return cstr("virtual_thread")
    if tag == PY_TYPE_VTHREAD_CHANNEL:
        return cstr("vthread_channel")
    return cstr("object")


@c_abi_export("py_obj_type_name")
def py_obj_type_name(o):
    if ptr_is_null(o) != 0:
        name = cstr("NoneType")
        return py_str_new(name, strlen(name))
    tag: int = _type_of(o)
    if _is_instance_tag(tag) != 0:
        cls = pcc_gc_load_ptr(o, ptr_add(o, PYINSTANCEOBJECT_CLS_OFFSET))
        if ptr_is_null(cls) == 0:
            cls_name = load_ptr(cls, PYCLASSOBJECT_NAME_OFFSET)
            if ptr_is_null(cls_name) == 0:
                return py_str_new(cls_name, strlen(cls_name))
    if tag == PY_TYPE_EXC:  # PY_TYPE_EXC
        cls = pcc_gc_load_ptr(o, ptr_add(o, 16))
        if ptr_is_null(cls) == 0:
            cls_name = load_ptr(cls, PYCLASSOBJECT_NAME_OFFSET)
            if ptr_is_null(cls_name) == 0:
                return py_str_new(cls_name, strlen(cls_name))
    name = _type_name_cstr_for_tag(tag)
    return py_str_new(name, strlen(name))


@c_abi_export("py_obj_len")
def py_obj_len(o) -> int:
    if ptr_is_null(o) != 0:
        return 0
    tag: int = _type_of(o)
    if tag == PY_TYPE_LIST:
        return py_list_len(o)
    if tag == PY_TYPE_TUPLE:
        return py_tuple_len(o)
    if tag == PY_TYPE_STR:
        return py_str_len(o)
    if tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY or tag == PY_TYPE_MEMORYVIEW:
        return py_bytes_len(o)
    if tag == PY_TYPE_DICT:
        return py_dict_len(o)
    if tag == PY_TYPE_SET:
        return py_set_len(o)
    # Symmetric with py_obj_getitem: a cext object's length is its
    # mp_length/sq_length slot, not a Python __len__ (-1 = no slot).
    if pcc_capi_is_cext_type_tag(tag) != 0:
        cext_len: int = pcc_capi_cext_object_length(o)
        if cext_len >= 0:
            return cext_len
    if _is_instance_tag(tag) != 0:
        return py_user_len_dispatch(o, null())
    return 0


@c_abi_export("py_obj_getitem")
def py_obj_getitem(o, k):
    if ptr_is_null(o) != 0:
        return null()
    if ptr_is_null(k) != 0:
        return null()
    tag: int = _type_of(o)
    pcc_runtime_log_event_code(7, 1, tag, _type_of(k), o)
    if pcc_capi_is_cext_type_tag(tag) != 0:
        return pcc_capi_cext_object_getitem(o, k)
    if tag == PY_TYPE_LIST:
        idx: int = py_obj_index_i64(k)
        if py_err_occurred() != 0:
            return null()
        return py_list_get(o, idx)
    if tag == PY_TYPE_TUPLE:
        idx: int = py_obj_index_i64(k)
        if py_err_occurred() != 0:
            return null()
        return py_tuple_get(o, idx)
    if tag == PY_TYPE_DICT:
        return py_dict_get(o, k)
    if tag == PY_TYPE_STR:
        return py_str_index(o, k)
    if tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY or tag == PY_TYPE_MEMORYVIEW:
        return py_bytes_getitem(o, k)
    if _is_instance_tag(tag) != 0:
        return py_user_getitem_dispatch(o, k)
    return null()


@c_abi_export("py_obj_getitem_i64")
def py_obj_getitem_i64(o, idx: int):
    if ptr_is_null(o) != 0:
        return null()
    tag: int = _type_of(o)
    pcc_runtime_log_event_code(7, 1, tag, 2, o)
    if pcc_capi_is_cext_type_tag(tag) != 0:
        key = py_int_from_i64(idx)
        out = pcc_capi_cext_object_getitem(o, key)
        py_decref(key)
        return out
    if tag == PY_TYPE_LIST:
        return py_list_get(o, idx)
    if tag == PY_TYPE_TUPLE:
        return py_tuple_get(o, idx)
    key = py_int_from_i64(idx)
    if tag == PY_TYPE_DICT:
        out = py_dict_get(o, key)
        py_decref(key)
        return out
    if tag == PY_TYPE_STR:
        out = py_str_index(o, key)
        py_decref(key)
        return out
    if tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY or tag == PY_TYPE_MEMORYVIEW:
        out = py_bytes_getitem(o, key)
        py_decref(key)
        return out
    if _is_instance_tag(tag) != 0:
        out = py_user_getitem_dispatch(o, key)
        py_decref(key)
        return out
    py_decref(key)
    return null()


def _subscript_raise_missing(o, key) -> None:
    # Mirror of py_obj_subscript_raise_missing in py_obj_ops_dispatch.c: turn
    # the getitem primitives' silent NULL into the CPython exception.
    tag: int = _type_of(o)
    if tag == PY_TYPE_DICT:
        py_raise_owned(py_exc_new_with_value(4, key))  # PY_EXC_KEYERROR
        return
    if tag == PY_TYPE_LIST:
        py_raise_owned(py_exc_new(5, cstr("list index out of range")))  # PY_EXC_INDEXERROR
        return
    if tag == PY_TYPE_TUPLE:
        py_raise_owned(py_exc_new(5, cstr("tuple index out of range")))
        return
    if tag == PY_TYPE_STR:
        py_raise_owned(py_exc_new(5, cstr("string index out of range")))
        return
    if tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY or tag == PY_TYPE_MEMORYVIEW:
        py_raise_owned(py_exc_new(5, cstr("index out of range")))
        return
    name = py_obj_type_name(o)
    quote = py_str_new(cstr("'"), 1)
    head = py_str_concat(quote, name)
    tail = py_str_new(cstr("' object is not subscriptable"), 29)
    message = py_str_concat(head, tail)
    py_decref(quote)
    py_decref(name)
    py_decref(head)
    py_decref(tail)
    py_raise_owned(py_exc_new_with_value(3, message))  # PY_EXC_TYPEERROR
    py_decref(message)


@c_abi_export("py_obj_subscript")
def py_obj_subscript(o, k):
    # User-level o[k]; py_obj_getitem keeps its silent-NULL contract for the
    # internal callers (tuple unpack, splat, capi shims).
    out = py_obj_getitem(o, k)
    if ptr_is_null(out) == 0:
        return out
    if ptr_is_null(o) != 0:
        return null()
    if ptr_is_null(k) != 0:
        return null()
    if py_err_occurred() != 0:
        return null()
    _subscript_raise_missing(o, k)
    return null()


@c_abi_export("py_obj_subscript_i64")
def py_obj_subscript_i64(o, idx: int):
    out = py_obj_getitem_i64(o, idx)
    if ptr_is_null(out) == 0:
        return out
    if ptr_is_null(o) != 0:
        return null()
    if py_err_occurred() != 0:
        return null()
    key = py_int_from_i64(idx)
    _subscript_raise_missing(o, key)
    py_decref(key)
    return null()


@c_abi_export("py_obj_del_slice")
def py_obj_del_slice(o, lo, hi, step) -> int:
    if ptr_is_null(o) != 0:
        return -1
    tag: int = _type_of(o)
    if tag == PY_TYPE_LIST:
        return py_list_del_slice(o, lo, hi, step)
    if tag == PY_TYPE_BYTEARRAY:
        return py_bytearray_del_slice(o, lo, hi, step)
    return -1


@c_abi_export("py_obj_setitem")
def py_obj_setitem(o, k, v) -> int:
    if ptr_is_null(o) != 0:
        return -1
    if ptr_is_null(k) != 0:
        return -1
    tag: int = _type_of(o)
    pcc_runtime_log_event_code(7, 3, tag, _type_of(k), o)
    if pcc_capi_is_cext_type_tag(tag) != 0:
        return pcc_capi_cext_object_setitem(o, k, v)
    if tag == PY_TYPE_LIST:
        idx: int = py_obj_index_i64(k)
        if py_err_occurred() != 0:
            return -1
        # User-visible store: out-of-range raises catchable IndexError
        # (py_list_set stays the internal non-raising setter).
        return py_list_setitem(o, idx, v)
    if tag == PY_TYPE_DICT:
        py_dict_set(o, k, v)
        return 0
    if tag == PY_TYPE_BYTEARRAY:
        return py_bytearray_setitem(o, k, v)
    if _is_instance_tag(tag) != 0:
        return py_user_setitem_dispatch(o, k, v, null())
    return -1


@c_abi_export("py_obj_setitem_i64")
def py_obj_setitem_i64(o, idx: int, v) -> int:
    if ptr_is_null(o) != 0:
        return -1
    tag: int = _type_of(o)
    pcc_runtime_log_event_code(7, 3, tag, 2, o)
    if pcc_capi_is_cext_type_tag(tag) != 0:
        key = py_int_from_i64(idx)
        if ptr_is_null(key) != 0:
            return -1
        rc: int = pcc_capi_cext_object_setitem(o, key, v)
        py_decref(key)
        return rc
    if tag == PY_TYPE_LIST:
        # User-visible store: out-of-range raises catchable IndexError.
        return py_list_setitem(o, idx, v)
    key = py_int_from_i64(idx)
    if tag == PY_TYPE_DICT:
        py_dict_set(o, key, v)
        py_decref(key)
        return 0
    if tag == PY_TYPE_BYTEARRAY:
        rc: int = py_bytearray_setitem(o, key, v)
        py_decref(key)
        return rc
    if _is_instance_tag(tag) != 0:
        rc: int = py_user_setitem_dispatch(o, key, v, null())
        py_decref(key)
        return rc
    py_decref(key)
    return -1


@c_abi_export("py_obj_delitem")
def py_obj_delitem(o, k) -> int:
    if ptr_is_null(o) != 0:
        return -1
    if ptr_is_null(k) != 0:
        return -1
    tag: int = _type_of(o)
    pcc_runtime_log_event_code(7, 4, tag, _type_of(k), o)
    if tag == PY_TYPE_LIST:
        idx: int = py_obj_index_i64(k)
        if py_err_occurred() != 0:
            return -1
        popped = py_list_pop(o, idx)
        if ptr_is_null(popped) == 0:
            py_decref(popped)
        return 0
    if tag == PY_TYPE_DICT:
        return py_dict_del(o, k)
    if _is_instance_tag(tag) != 0:
        return py_user_delitem_dispatch(o, k, null())
    return -1


def _is_instance_tag(tag: int) -> int:
    if tag == PY_TYPE_INSTANCE:  # PY_TYPE_INSTANCE
        return 1
    if tag >= PY_TYPE_USER_CLASS_START:
        return 1
    return 0


def _return_builtin_type(cls):
    if ptr_is_null(cls) != 0:
        return null()
    py_incref(cls)
    return cls


def _dispatch_call_method_with_args(method, self_obj, args, kwargs):
    if ptr_is_null(method) != 0:
        return py_runtime_error_if_unset(
            cstr("dispatch_call_method_with_args"),
            cstr("dispatch_call_method_with_args received NULL method"),
        )
    n: int = 0
    if ptr_is_null(args) == 0:
        n = py_tuple_len(args)
    if is_tagged_int(method) == 0:
        if load_i32(method, 8) == PY_TYPE_FUNC:  # PY_TYPE_FUNC
            full_args = py_tuple_new(n + 1)
            if ptr_is_null(full_args) != 0:
                return py_runtime_error_if_unset(
                    cstr("py_tuple_new"),
                    cstr("bound method call could not allocate its argument tuple"),
                )
            py_tuple_set_item(full_args, 0, self_obj)
            i: int = 0
            while i < n:
                item = py_tuple_get(args, i)
                py_tuple_set_item(full_args, i + 1, item)
                py_decref(item)
                i = i + 1
            out = py_func_call_kwargs(method, full_args, kwargs)
            if ptr_is_null(out) != 0:
                py_runtime_error_if_unset(
                    cstr("py_func_call_kwargs"),
                    cstr(
                        "bound function call returned NULL without setting an exception"
                    ),
                )
            py_decref(full_args)
            return out
    if n == 0:
        out = call_ptr1(method, self_obj)
        if ptr_is_null(out) != 0:
            py_runtime_error_if_unset(
                cstr("bound native method"),
                cstr("bound native method returned NULL without setting an exception"),
            )
        return out
    if n == 1:
        a0 = py_tuple_get(args, 0)
        out = call_ptr2(method, self_obj, a0)
        if ptr_is_null(out) != 0:
            py_runtime_error_if_unset(
                cstr("bound native method"),
                cstr("bound native method returned NULL without setting an exception"),
            )
        py_decref(a0)
        return out
    return py_runtime_error_if_unset(
        cstr("dispatch_call_method_with_args"),
        cstr("pcc-Python bound native method supports at most one argument"),
    )


@c_abi_export("py_slice_new")
def py_slice_new(start, stop, step):
    none = global_load_ptr("py_None")
    if ptr_is_null(start) != 0:
        start = none
    if ptr_is_null(stop) != 0:
        stop = none
    if ptr_is_null(step) != 0:
        step = none
    cls = global_load_ptr("pcc_slice_cls")
    if ptr_is_null(cls) != 0:
        cls = py_class_new(cstr("slice"), null(), 0, null(), 0)
        if ptr_is_null(cls) != 0:
            return null()
        global_store_ptr("pcc_slice_cls", cls)
    inst = py_instance_new(cls)
    if ptr_is_null(inst) != 0:
        return null()
    py_instance_setattr(inst, cstr("start"), start)
    py_instance_setattr(inst, cstr("stop"), stop)
    py_instance_setattr(inst, cstr("step"), step)
    return inst


@c_abi_export("py_obj_is_slice")
def py_obj_is_slice(o) -> int:
    # isinstance(x, slice): a slice is an instance of the lazily-created
    # pcc_slice_cls. 0 when no slice has been created yet.
    if ptr_is_null(o) != 0:
        return 0
    cls = global_load_ptr("pcc_slice_cls")
    if ptr_is_null(cls) != 0:
        return 0
    # py_isinstance does the instance-tag check + MRO walk (an instance may
    # carry a per-class tag at or above PY_TYPE_USER_CLASS_START, so don't
    # pre-filter on PY_TYPE_INSTANCE alone).
    return py_isinstance(o, cls)


def _builtin_type_class_for_tag(tag: int):
    cls = null()
    # Synthetic tag for the first-class ``super`` type object. It is outside
    # the object-header tag enum because no native object carries this tag.
    if tag == -3:
        cls = global_load_ptr("pcc_type_cls_super")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("super"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_super", cls)
        return _return_builtin_type(cls)
    if tag == PY_TYPE_NONE:  # PY_TYPE_NONE
        cls = global_load_ptr("pcc_type_cls_none")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("NoneType"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_none", cls)
        return _return_builtin_type(cls)
    if tag == PY_TYPE_BOOL:  # PY_TYPE_BOOL
        cls = global_load_ptr("pcc_type_cls_bool")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("bool"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_bool", cls)
        return _return_builtin_type(cls)
    if tag == PY_TYPE_INT:  # PY_TYPE_INT
        cls = global_load_ptr("pcc_type_cls_int")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("int"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_int", cls)
        return _return_builtin_type(cls)
    if tag == PY_TYPE_FLOAT:  # PY_TYPE_FLOAT
        cls = global_load_ptr("pcc_type_cls_float")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("float"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_float", cls)
        return _return_builtin_type(cls)
    if tag == PY_TYPE_STR:  # PY_TYPE_STR
        cls = global_load_ptr("pcc_type_cls_str")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("str"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_str", cls)
        return _return_builtin_type(cls)
    if tag == PY_TYPE_LIST:  # PY_TYPE_LIST
        cls = global_load_ptr("pcc_type_cls_list")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("list"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_list", cls)
        return _return_builtin_type(cls)
    if tag == PY_TYPE_DICT:  # PY_TYPE_DICT
        cls = global_load_ptr("pcc_type_cls_dict")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("dict"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_dict", cls)
        return _return_builtin_type(cls)
    if tag == PY_TYPE_TUPLE:  # PY_TYPE_TUPLE
        cls = global_load_ptr("pcc_type_cls_tuple")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("tuple"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_tuple", cls)
        return _return_builtin_type(cls)
    if tag == PY_TYPE_SET:  # PY_TYPE_SET
        cls = global_load_ptr("pcc_type_cls_set")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("set"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_set", cls)
        return _return_builtin_type(cls)
    if tag == PY_TYPE_CLASS:  # PY_TYPE_CLASS
        cls = global_load_ptr("pcc_type_cls_type")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("type"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_type", cls)
        return _return_builtin_type(cls)
    if tag == PY_TYPE_COMPLEX:  # PY_TYPE_COMPLEX
        cls = global_load_ptr("pcc_type_cls_complex")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("complex"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_complex", cls)
        return _return_builtin_type(cls)
    if tag == PY_TYPE_BYTES:  # PY_TYPE_BYTES
        cls = global_load_ptr("pcc_type_cls_bytes")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("bytes"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_bytes", cls)
        return _return_builtin_type(cls)
    if tag == PY_TYPE_BYTEARRAY:  # PY_TYPE_BYTEARRAY
        cls = global_load_ptr("pcc_type_cls_bytearray")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("bytearray"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_bytearray", cls)
        return _return_builtin_type(cls)
    if tag == PY_TYPE_MEMORYVIEW:  # PY_TYPE_MEMORYVIEW
        cls = global_load_ptr("pcc_type_cls_memoryview")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("memoryview"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_memoryview", cls)
        return _return_builtin_type(cls)
    if tag == PY_TYPE_COROUTINE:  # PY_TYPE_COROUTINE
        cls = global_load_ptr("pcc_type_cls_coroutine")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("coroutine"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_coroutine", cls)
        return _return_builtin_type(cls)
    cls = global_load_ptr("pcc_type_cls_object")
    if ptr_is_null(cls) != 0:
        cls = py_class_new(cstr("object"), null(), 0, null(), 0)
        if ptr_is_null(cls) == 0:
            global_store_ptr("pcc_type_cls_object", cls)
    return _return_builtin_type(cls)


@c_abi_export("py_type_builtin")
def py_type_builtin(o):
    if ptr_is_null(o) != 0:
        return _builtin_type_class_for_tag(0)
    if is_tagged_int(o) != 0:
        return _builtin_type_class_for_tag(2)
    tag: int = _type_of(o)
    if tag == PY_TYPE_EXC:  # PY_TYPE_EXC
        cls = pcc_gc_load_ptr(o, ptr_add(o, 16))
        if ptr_is_null(cls) == 0:
            py_incref(cls)
            return cls
    if _is_instance_tag(tag) != 0:
        cls = pcc_gc_load_ptr(o, ptr_add(o, PYINSTANCEOBJECT_CLS_OFFSET))
        if ptr_is_null(cls) == 0:
            py_incref(cls)
            return cls
    return _builtin_type_class_for_tag(tag)


@c_abi_export("py_builtin_type_for_tag")
def py_builtin_type_for_tag(tag: int):
    return _builtin_type_class_for_tag(tag)


@c_abi_export("py_builtin_type_class_tag")
def py_builtin_type_class_tag(value) -> int:
    if ptr_is_null(value) != 0 or is_tagged_int(value) != 0:
        return -2
    if ptr_eq(value, global_load_ptr("pcc_type_cls_super")) != 0:
        return -3
    if ptr_eq(value, global_load_ptr("pcc_type_cls_none")) != 0:
        return PY_TYPE_NONE
    if ptr_eq(value, global_load_ptr("pcc_type_cls_bool")) != 0:
        return PY_TYPE_BOOL
    if ptr_eq(value, global_load_ptr("pcc_type_cls_int")) != 0:
        return PY_TYPE_INT
    if ptr_eq(value, global_load_ptr("pcc_type_cls_float")) != 0:
        return PY_TYPE_FLOAT
    if ptr_eq(value, global_load_ptr("pcc_type_cls_str")) != 0:
        return PY_TYPE_STR
    if ptr_eq(value, global_load_ptr("pcc_type_cls_list")) != 0:
        return PY_TYPE_LIST
    if ptr_eq(value, global_load_ptr("pcc_type_cls_dict")) != 0:
        return PY_TYPE_DICT
    if ptr_eq(value, global_load_ptr("pcc_type_cls_tuple")) != 0:
        return PY_TYPE_TUPLE
    if ptr_eq(value, global_load_ptr("pcc_type_cls_set")) != 0:
        return PY_TYPE_SET
    if ptr_eq(value, global_load_ptr("pcc_type_cls_type")) != 0:
        return PY_TYPE_CLASS
    if ptr_eq(value, global_load_ptr("pcc_type_cls_complex")) != 0:
        return PY_TYPE_COMPLEX
    if ptr_eq(value, global_load_ptr("pcc_type_cls_bytes")) != 0:
        return PY_TYPE_BYTES
    if ptr_eq(value, global_load_ptr("pcc_type_cls_bytearray")) != 0:
        return PY_TYPE_BYTEARRAY
    if ptr_eq(value, global_load_ptr("pcc_type_cls_memoryview")) != 0:
        return PY_TYPE_MEMORYVIEW
    if ptr_eq(value, global_load_ptr("pcc_type_cls_object")) != 0:
        return -1
    return -2


def _cstr_append(buf, at: int, text) -> int:
    """Copy a NUL-terminated cstr into ``buf`` at ``at``; return the new end."""
    pos: int = at
    index: int = 0
    while True:
        ch: int = load_i8(text, index)
        if ch == 0:
            return pos
        store_i8(buf, pos, ch)
        pos = pos + 1
        index = index + 1


def _raise_attribute_error(o, name):
    """Raise CPython's ``'<type>' object has no attribute '<name>'``.

    The message used to be the bare attribute name. That is a real diagnostic
    defect and not only a cosmetic one: ``str(exc)`` was a single word, so any
    caller doing ``raise SomeError(str(exc))`` reported a missing
    ``.returncode`` as just "returncode", with no hint that the failure was an
    attribute miss at all -- which is exactly how a self-host link failure got
    chased through the linker for a long time before anyone read it as an
    AttributeError.

    ``py_exc_alloc`` copies the cstr into a str object, so the scratch buffer
    is released immediately after.
    """
    if py_err_occurred() != 0:
        return null()
    attr = name
    if ptr_is_null(attr) != 0:
        attr = cstr("?")
    type_name = cstr("object")
    if ptr_is_null(o) == 0:
        if is_tagged_int(o) != 0:
            type_name = cstr("int")
        else:
            tag: int = load_i32(o, 8)
            # A user instance carries its class name; the tag table only knows
            # the builtins, so consulting it alone reported every instance as
            # "object". Same lookup order as py_obj_type_name. Track the hit
            # with a flag rather than comparing against a second
            # ``cstr("object")`` -- two identical literals need not be the same
            # pointer, and that comparison silently lost NoneType.
            resolved: int = 0
            if _is_instance_tag(tag) != 0:
                cls = pcc_gc_load_ptr(o, ptr_add(o, PYINSTANCEOBJECT_CLS_OFFSET))
                if ptr_is_null(cls) == 0:
                    cls_name = load_ptr(cls, PYCLASSOBJECT_NAME_OFFSET)
                    if ptr_is_null(cls_name) == 0:
                        type_name = cls_name
                        resolved = 1
            if resolved == 0:
                candidate = _type_name_cstr_for_tag(tag)
                if ptr_is_null(candidate) == 0:
                    type_name = candidate
    total: int = strlen(type_name) + strlen(attr) + 32
    buf = malloc(total)
    if ptr_is_null(buf) != 0:
        # Out of memory while formatting: the bare name still identifies it.
        py_raise_owned(py_exc_new(6, attr))  # PY_EXC_ATTRIBUTEERROR
        return null()
    pos: int = _cstr_append(buf, 0, cstr("'"))
    pos = _cstr_append(buf, pos, type_name)
    pos = _cstr_append(buf, pos, cstr("' object has no attribute '"))
    pos = _cstr_append(buf, pos, attr)
    pos = _cstr_append(buf, pos, cstr("'"))
    store_i8(buf, pos, 0)
    py_raise_owned(py_exc_new(6, buf))  # PY_EXC_ATTRIBUTEERROR
    free(buf)
    return null()


def _raise_attribute_status(o, name) -> int:
    _raise_attribute_error(o, name)
    return -1


def _py_coroutine_send_bound_entry(captures, args):
    coro = py_tuple_get(captures, 0)
    if ptr_is_null(coro) != 0:
        return null()
    value = null()
    if ptr_is_null(args) == 0:
        if py_tuple_len(args) > 0:
            value = py_tuple_get(args, 0)
    if ptr_is_null(value) != 0:
        value = global_load_ptr("py_None")
        py_incref(value)
    out = py_gen_send(coro, value)
    py_decref(value)
    py_decref(coro)
    return out


def _py_coroutine_bound_send(coro):
    captures = py_tuple_new(1)
    if ptr_is_null(captures) != 0:
        return null()
    py_tuple_set_item(captures, 0, coro)
    fn = py_func_new_bound(
        _py_coroutine_send_bound_entry,
        captures,
        cstr("send"),
        coro,
    )
    py_decref(captures)
    return fn


def _py_list_pop_bound_entry(captures, args):
    lst = py_tuple_get(captures, 0)
    if ptr_is_null(lst) != 0:
        return null()
    nargs: int = 0
    if ptr_is_null(args) == 0:
        if is_tagged_int(args) == 0:
            if load_i32(args, 8) == PY_TYPE_TUPLE:  # PY_TYPE_TUPLE
                nargs = py_tuple_len(args)
    if nargs > 1:
        py_decref(lst)
        exc = py_exc_new(3, cstr("list.pop expected at most 1 argument"))
        py_raise_owned(exc)
        return null()
    idx: int = -1
    if nargs == 1:
        idx_obj = py_tuple_get(args, 0)
        if ptr_is_null(idx_obj) != 0:
            py_decref(lst)
            return null()
        idx = py_int_value_i64(idx_obj)
        py_decref(idx_obj)
        if py_err_occurred() != 0:
            py_decref(lst)
            return null()
    out = py_list_pop(lst, idx)
    py_decref(lst)
    return out


def _py_dict_pop_bound_entry(captures, args):
    d = py_tuple_get(captures, 0)
    if ptr_is_null(d) != 0:
        return null()
    nargs: int = 0
    if ptr_is_null(args) == 0:
        if is_tagged_int(args) == 0:
            if load_i32(args, 8) == PY_TYPE_TUPLE:  # PY_TYPE_TUPLE
                nargs = py_tuple_len(args)
    if nargs < 1:
        py_decref(d)
        exc = py_exc_new(3, cstr("dict.pop expected at least 1 argument"))
        py_raise_owned(exc)
        return null()
    if nargs > 2:
        py_decref(d)
        exc = py_exc_new(3, cstr("dict.pop expected at most 2 arguments"))
        py_raise_owned(exc)
        return null()
    key = py_tuple_get(args, 0)
    if ptr_is_null(key) != 0:
        py_decref(d)
        return null()
    out = py_dict_get(d, key)
    if ptr_is_null(out) == 0:
        py_dict_del(d, key)
    elif nargs == 2:
        out = py_tuple_get(args, 1)
    else:
        exc = py_exc_new_with_value(4, key)  # PY_EXC_KEYERROR
        py_raise_owned(exc)
    py_decref(key)
    py_decref(d)
    return out


def _py_set_pop_bound_entry(captures, args):
    s = py_tuple_get(captures, 0)
    if ptr_is_null(s) != 0:
        return null()
    nargs: int = 0
    if ptr_is_null(args) == 0:
        if is_tagged_int(args) == 0:
            if load_i32(args, 8) == PY_TYPE_TUPLE:  # PY_TYPE_TUPLE
                nargs = py_tuple_len(args)
    if nargs > 0:
        py_decref(s)
        exc = py_exc_new(3, cstr("set.pop expected no arguments"))
        py_raise_owned(exc)
        return null()
    out = py_set_pop(s)
    py_decref(s)
    return out


def _py_list_pop_bound(o):
    captures = py_tuple_new(1)
    if ptr_is_null(captures) != 0:
        return null()
    py_tuple_set_item(captures, 0, o)
    fn = py_func_new_bound(_py_list_pop_bound_entry, captures, cstr("pop"), o)
    py_decref(captures)
    return fn


def _py_dict_pop_bound(o):
    captures = py_tuple_new(1)
    if ptr_is_null(captures) != 0:
        return null()
    py_tuple_set_item(captures, 0, o)
    fn = py_func_new_bound(_py_dict_pop_bound_entry, captures, cstr("pop"), o)
    py_decref(captures)
    return fn


def _py_set_pop_bound(o):
    captures = py_tuple_new(1)
    if ptr_is_null(captures) != 0:
        return null()
    py_tuple_set_item(captures, 0, o)
    fn = py_func_new_bound(_py_set_pop_bound_entry, captures, cstr("pop"), o)
    py_decref(captures)
    return fn


def _py_str_count_bound_entry(captures, args):
    s = py_tuple_get(captures, 0)
    if ptr_is_null(s) != 0:
        return null()
    nargs: int = 0
    if ptr_is_null(args) == 0:
        if is_tagged_int(args) == 0:
            if load_i32(args, 8) == PY_TYPE_TUPLE:  # PY_TYPE_TUPLE
                nargs = py_tuple_len(args)
    if nargs < 1 or nargs > 3:
        py_decref(s)
        exc = py_exc_new(3, cstr("str.count expected 1 to 3 arguments"))
        py_raise_owned(exc)
        return null()

    sub = py_tuple_get(args, 0)
    if ptr_is_null(sub) != 0:
        py_decref(s)
        return null()
    if is_tagged_int(sub) != 0 or load_i32(sub, 8) != PY_TYPE_STR:  # PY_TYPE_STR
        py_decref(sub)
        py_decref(s)
        exc = py_exc_new(3, cstr("str.count argument must be str"))
        py_raise_owned(exc)
        return null()

    count: int = 0
    if nargs >= 2:
        start = py_tuple_get(args, 1)
        end = null()
        if nargs == 3:
            end = py_tuple_get(args, 2)
        if ptr_is_null(start) != 0 or (nargs == 3 and ptr_is_null(end) != 0):
            if ptr_is_null(start) == 0:
                py_decref(start)
            if ptr_is_null(end) == 0:
                py_decref(end)
            py_decref(sub)
            py_decref(s)
            return null()
        count = py_str_count_range(s, sub, start, end)
        py_decref(start)
        if ptr_is_null(end) == 0:
            py_decref(end)
    else:
        count = py_str_count(s, sub)

    out = py_int_from_i64(count)
    py_decref(s)
    py_decref(sub)
    return out


def _py_str_count_bound(o):
    captures = py_tuple_new(1)
    if ptr_is_null(captures) != 0:
        return null()
    py_tuple_set_item(captures, 0, o)
    fn = py_func_new_bound(
        _py_str_count_bound_entry,
        captures,
        cstr("count"),
        o,
    )
    py_decref(captures)
    return fn


@c_abi_export("py_obj_getattr")
def py_obj_getattr(o, name):
    if ptr_is_null(o) != 0:
        return _raise_attribute_error(o, name)
    if ptr_is_null(name) != 0:
        return _raise_attribute_error(o, name)
    if _cstr_is_dunder_class(name) != 0:
        return py_type_builtin(o)
    if is_tagged_int(o) != 0:
        return _raise_attribute_error(o, name)

    tag: int = load_i32(o, 8)
    pcc_runtime_log_event_code(7, 5, tag, 0, o)

    type_attr = pcc_capi_type_object_getattr(o, name)
    if ptr_is_null(type_attr) == 0 or py_err_occurred() != 0:
        return type_attr

    builtin_attr = pcc_capi_builtin_object_getattr(o, name)
    if ptr_is_null(builtin_attr) == 0 or py_err_occurred() != 0:
        return builtin_attr

    if pcc_capi_is_cext_type_tag(tag) != 0:
        result = pcc_capi_cext_object_getattr(o, name)
        if ptr_is_null(result) == 0 or py_err_occurred() != 0:
            return result
        return _raise_attribute_error(o, name)

    if _cstr_is_pop(name) != 0:
        if tag == PY_TYPE_LIST:  # PY_TYPE_LIST
            return _py_list_pop_bound(o)
        if tag == PY_TYPE_DICT:  # PY_TYPE_DICT
            return _py_dict_pop_bound(o)
        if tag == PY_TYPE_SET:  # PY_TYPE_SET
            return _py_set_pop_bound(o)

    if tag == PY_TYPE_STR and _cstr_is_count(name) != 0:  # PY_TYPE_STR
        return _py_str_count_bound(o)

    if _is_instance_tag(tag) != 0:
        result = py_instance_getattr(o, name)
        if ptr_is_null(result) == 0:
            return result
        if py_err_occurred() != 0:
            return result
        return _raise_attribute_error(o, name)
    if tag == PY_TYPE_CLASS:  # PY_TYPE_CLASS
        result = py_class_getattr(o, name)
        if ptr_is_null(result) == 0:
            return result
        if py_err_occurred() != 0:
            return result
        return _raise_attribute_error(o, name)
    if tag == PY_TYPE_FUNC:  # PY_TYPE_FUNC
        attrs = pcc_gc_load_ptr(o, ptr_add(o, 88))
        if ptr_is_null(attrs) == 0:
            key = py_str_new(name, strlen(name))
            if ptr_is_null(key) != 0:
                return null()
            value = py_dict_get(attrs, key)
            py_decref(key)
            if ptr_is_null(value) == 0:
                return value
        func_name = load_ptr(o, 72)
        if (
            _cstr_is_dunder_name(name) != 0
            or _cstr_is_dunder_qualname(name) != 0
        ) and ptr_is_null(func_name) == 0:
            return py_str_new(func_name, strlen(func_name))
        if _cstr_is_dunder_code(name) != 0:
            code = py_func_get_code_metadata(o)
            if ptr_is_null(code) == 0 or py_err_occurred() != 0:
                return code
        if _cstr_is_dunder_defaults(name) != 0:
            defaults = py_func_get_defaults_metadata(o)
            if ptr_is_null(defaults) == 0 or py_err_occurred() != 0:
                return defaults
        if _cstr_is_dunder_doc(name) != 0:
            none = global_load_ptr("py_None")
            py_incref(none)
            return none
        if _cstr_is_dunder_self(name) != 0:
            if ptr_is_null(load_ptr(o, 16)) == 0:
                self_obj = pcc_gc_load_ptr(o, ptr_add(o, 24))
            else:
                self_obj = pcc_gc_load_ptr(o, ptr_add(o, 80))
            if ptr_is_null(self_obj) == 0:
                py_incref(self_obj)
                return self_obj
        return _raise_attribute_error(o, name)
    if tag == PY_TYPE_WEAKREF:  # PY_TYPE_WEAKREF
        target = py_weakref_call(o)
        if ptr_is_null(target) != 0:
            exc = py_exc_new(
                18,  # PY_EXC_REFERENCEERROR
                cstr("weakly-referenced object no longer exists"),
            )
            py_raise_owned(exc)
            return null()
        if ptr_eq(target, global_load_ptr("py_None")) != 0:
            py_decref(target)
            exc = py_exc_new(
                18,
                cstr("weakly-referenced object no longer exists"),
            )
            py_raise_owned(exc)
            return null()
        result = py_obj_getattr(target, name)
        py_decref(target)
        return result
    if tag == PY_TYPE_COROUTINE:  # PY_TYPE_COROUTINE
        result = null()
        if _cstr_is_dunder_class(name) != 0:
            result = py_coroutine_class()
        elif _cstr_is_send(name) != 0:
            return _py_coroutine_bound_send(o)
        if ptr_is_null(result) == 0:
            py_incref(result)
            return result
        if py_err_occurred() != 0:
            return result
        return _raise_attribute_error(o, name)
    if tag == PY_TYPE_CONTINUATION:
        result = null()
        if _cstr_is_dunder_class(name) != 0:
            result = py_continuation_class()
        if ptr_is_null(result) == 0:
            py_incref(result)
            return result
        if py_err_occurred() != 0:
            return result
        return _raise_attribute_error(o, name)
    if tag == PY_TYPE_COMPLEX:  # PY_TYPE_COMPLEX
        if _cstr_is_real(name) != 0:
            return py_complex_real(o)
        if _cstr_is_imag(name) != 0:
            return py_complex_imag(o)
        return _raise_attribute_error(o, name)
    if tag == PY_TYPE_EXC:  # PY_TYPE_EXC
        result = null()
        if _cstr_is_dunder_class(name) != 0:
            result = pcc_gc_load_ptr(o, ptr_add(o, 16))
        elif _cstr_is_dunder_cause(name) != 0:
            result = pcc_gc_load_ptr(o, ptr_add(o, 32))
            if ptr_is_null(result) != 0:
                result = global_load_ptr("py_None")
        elif _cstr_is_dunder_context(name) != 0:
            result = pcc_gc_load_ptr(o, ptr_add(o, 40))
            if ptr_is_null(result) != 0:
                result = global_load_ptr("py_None")
        elif _cstr_is_value(name) != 0:
            result = pcc_gc_load_ptr(o, ptr_add(o, 24))
            if ptr_is_null(result) != 0:
                result = global_load_ptr("py_None")
        elif _cstr_is_msg(name) != 0:
            # CPython exposes `.msg` on ImportError/ModuleNotFoundError only
            # (it is args[0]); numpy's `_core` re-init recovery reads it. Keep
            # it scoped so a bare RuntimeError does not grow a `.msg`.
            imp = py_exc_builtin_class(20)  # PY_EXC_IMPORTERROR
            if ptr_is_null(imp) == 0 and py_isinstance(o, imp) != 0:
                result = pcc_gc_load_ptr(o, ptr_add(o, 24))
                if ptr_is_null(result) != 0:
                    result = global_load_ptr("py_None")
            else:
                return _raise_attribute_error(o, name)
        elif _cstr_is_args(name) != 0:
            # args tuple. Only args[0] is stored (as `message` at offset 24);
            # capturing args[1:] needs a dedicated field (documented follow-up,
            # shared with multi-arg str(exc)). Return () or (message,).
            msg = pcc_gc_load_ptr(o, ptr_add(o, 24))
            none = global_load_ptr("py_None")
            empty: int = 0
            if ptr_is_null(msg) != 0 or ptr_eq(msg, none) != 0:
                empty = 1
            elif _type_of(msg) == PY_TYPE_STR and load_i64(msg, 16) == 0:
                # PY_TYPE_STR(4) with byte_len 0: a no-arg exception stores ""
                # as message, so args == () like CPython.  ``_type_of`` is
                # tag-aware: KeyError(-1) stores a tagged small int here and a
                # raw header read of it dereferenced the tag (mirrors
                # py_type_of in py_obj_ops_dispatch.c).
                empty = 1
            if empty != 0:
                return py_tuple_new(0)
            t = py_tuple_new(1)
            if ptr_is_null(t) == 0:
                py_tuple_set_item(t, 0, msg)
            return t
        if ptr_is_null(result) == 0:
            py_incref(result)
            return result
        if py_err_occurred() != 0:
            return result
        return _raise_attribute_error(o, name)
    return _raise_attribute_error(o, name)


@c_abi_export("py_obj_getattr_default")
def py_obj_getattr_default(o, name):
    if ptr_is_null(o) != 0:
        return _raise_attribute_error(o, name)
    if ptr_is_null(name) != 0:
        return _raise_attribute_error(o, name)
    if _cstr_is_dunder_class(name) != 0:
        return py_type_builtin(o)
    if is_tagged_int(o) != 0:
        return _raise_attribute_error(o, name)

    tag: int = load_i32(o, 8)
    pcc_runtime_log_event_code(7, 5, tag, 1, o)

    if _is_instance_tag(tag) != 0:
        result = py_instance_getattr_default(o, name)
        if ptr_is_null(result) == 0:
            return result
        if py_err_occurred() != 0:
            return result
        return _raise_attribute_error(o, name)
    if tag == PY_TYPE_CLASS:  # PY_TYPE_CLASS
        result = py_class_getattr(o, name)
        if ptr_is_null(result) == 0:
            return result
        if py_err_occurred() != 0:
            return result
        return _raise_attribute_error(o, name)
    return py_obj_getattr(o, name)


# No-raise attribute probe for ``hasattr`` and 3-arg ``getattr``: identical
# probe order to py_obj_getattr, but a plain not-found terminal returns NULL
# WITHOUT constructing the AttributeError those callers immediately clear.
# User __getattr__ still runs and its real exceptions still surface (NULL
# with the error set).  Other tags fall back to the raising py_obj_getattr,
# whose exception the callers clear exactly as before.  Mirror of the C
# implementation in src/py_obj_ops_dispatch.c; keep both in sync.
@c_abi_export("py_obj_getattr_maybe")
def py_obj_getattr_maybe(o, name):
    if ptr_is_null(o) != 0:
        return null()
    if ptr_is_null(name) != 0:
        return null()
    if _cstr_is_dunder_class(name) != 0:
        return py_type_builtin(o)
    if is_tagged_int(o) != 0:
        return null()

    tag: int = load_i32(o, 8)
    pcc_runtime_log_event_code(7, 5, tag, 2, o)

    type_attr = pcc_capi_type_object_getattr(o, name)
    if ptr_is_null(type_attr) == 0 or py_err_occurred() != 0:
        return type_attr

    builtin_attr = pcc_capi_builtin_object_getattr(o, name)
    if ptr_is_null(builtin_attr) == 0 or py_err_occurred() != 0:
        return builtin_attr

    if pcc_capi_is_cext_type_tag(tag) != 0:
        return pcc_capi_cext_object_getattr(o, name)

    if _is_instance_tag(tag) != 0:
        return py_instance_getattr(o, name)
    if tag == PY_TYPE_CLASS:  # PY_TYPE_CLASS
        return py_class_getattr(o, name)
    return py_obj_getattr(o, name)


@c_abi_export("py_obj_setattr")
def py_obj_setattr(o, name, v) -> int:
    if ptr_is_null(o) != 0:
        return _raise_attribute_status(o, name)
    if ptr_is_null(name) != 0:
        return _raise_attribute_status(o, name)
    if is_tagged_int(o) != 0:
        return _raise_attribute_status(o, name)
    tag: int = load_i32(o, 8)
    pcc_runtime_log_event_code(7, 6, tag, 0, o)

    if pcc_capi_is_cext_type_tag(tag) != 0:
        rc: int = pcc_capi_cext_object_setattr(o, name, v)
        if rc == 0:
            return rc
        if py_err_occurred() != 0:
            return rc
        return _raise_attribute_status(o, name)
    if _is_instance_tag(tag) != 0:
        rc: int = py_instance_setattr(o, name, v)
        if rc == 0:
            return rc
        if py_err_occurred() != 0:
            return rc
        return _raise_attribute_status(o, name)
    if tag == PY_TYPE_CLASS:  # PY_TYPE_CLASS
        rc: int = py_class_setattr(o, name, v)
        if rc == 0:
            return rc
        if py_err_occurred() != 0:
            return rc
    if tag == PY_TYPE_FUNC:  # PY_TYPE_FUNC
        attrs = pcc_gc_load_ptr(o, ptr_add(o, 88))
        attrs_created: int = 0
        if ptr_is_null(attrs) != 0:
            attrs = py_dict_new()
            if ptr_is_null(attrs) != 0:
                return _raise_attribute_status(o, name)
            pcc_gc_store_ptr(o, ptr_add(o, 88), attrs)
            attrs_created = 1
        key = py_str_new(name, strlen(name))
        if ptr_is_null(key) != 0:
            if attrs_created != 0:
                py_decref(attrs)
            return _raise_attribute_status(o, name)
        py_dict_set(attrs, key, v)
        py_decref(key)
        if attrs_created != 0:
            py_decref(attrs)
        return 0
    return _raise_attribute_status(o, name)


@c_abi_export("py_obj_delattr")
def py_obj_delattr(o, name) -> int:
    if ptr_is_null(o) != 0:
        return -1
    if ptr_is_null(name) != 0:
        return -1
    if is_tagged_int(o) != 0:
        return -1
    tag: int = load_i32(o, 8)
    pcc_runtime_log_event_code(7, 7, tag, 0, o)

    if _is_instance_tag(tag) != 0:
        return py_instance_delattr(o, name)
    if tag == PY_TYPE_CLASS:  # PY_TYPE_CLASS
        return py_class_delattr(o, name)
    return -1


def _require_call_result(result, callee, message):
    if ptr_is_null(result) != 0 and py_err_occurred() == 0:
        py_runtime_error_if_unset(callee, message)
    return result


def _not_callable_message(tag: int):
    if tag == PY_TYPE_NONE:
        return cstr("'NoneType' object is not callable")
    if tag == PY_TYPE_BOOL:
        return cstr("'bool' object is not callable")
    if tag == PY_TYPE_INT:
        return cstr("'int' object is not callable")
    if tag == PY_TYPE_FLOAT:
        return cstr("'float' object is not callable")
    if tag == PY_TYPE_STR:
        return cstr("'str' object is not callable")
    if tag == PY_TYPE_LIST:
        return cstr("'list' object is not callable")
    if tag == PY_TYPE_DICT:
        return cstr("'dict' object is not callable")
    if tag == PY_TYPE_TUPLE:
        return cstr("'tuple' object is not callable")
    if tag == PY_TYPE_SET:
        return cstr("'set' object is not callable")
    if tag == PY_TYPE_BYTES:
        return cstr("'bytes' object is not callable")
    if tag == PY_TYPE_BYTEARRAY:
        return cstr("'bytearray' object is not callable")
    if tag == PY_TYPE_MEMORYVIEW:
        return cstr("'memoryview' object is not callable")
    if tag == PY_TYPE_INSTANCE or tag >= PY_TYPE_USER_CLASS_START:
        return cstr("instance has no __call__ method")
    return cstr("object type has no callable protocol")


def _raise_not_callable(callable, tag: int):
    pcc_runtime_log_event_code(7, 10, tag, 0, callable)
    py_raise_owned(py_exc_new(3, _not_callable_message(tag)))
    return null()


@c_abi_export("py_obj_call")
def py_obj_call(callable, args, kwargs):
    if ptr_is_null(callable) != 0:
        return py_runtime_error_if_unset(
            cstr("py_obj_call"),
            cstr("py_obj_call received NULL callable"),
        )
    if is_tagged_int(callable) != 0:
        return _raise_not_callable(callable, 2)
    tag: int = load_i32(callable, 8)
    pcc_runtime_log_event_code(7, 8, tag, 0, callable)

    if pcc_capi_type_object_is_callable(callable) != 0:
        return _require_call_result(
            pcc_capi_call_type_object(callable, args, kwargs),
            cstr("pcc_capi_call_type_object"),
            cstr(
                "pcc_capi_call_type_object returned NULL without setting an exception"
            ),
        )

    if tag == PY_TYPE_CLASS:  # PY_TYPE_CLASS
        nargs: int = 0
        if ptr_is_null(args) == 0:
            nargs = py_tuple_len(args)
        nkwargs: int = 0
        if ptr_is_null(kwargs) == 0 and ptr_eq(kwargs, global_load_ptr("py_None")) == 0:
            if _type_of(kwargs) == PY_TYPE_DICT:
                nkwargs = py_dict_len(kwargs)
        is_builtin: int = 0
        if ptr_eq(callable, global_load_ptr("pcc_type_cls_bool")) != 0:
            is_builtin = 1
        if ptr_eq(callable, global_load_ptr("pcc_type_cls_int")) != 0:
            is_builtin = 1
        if ptr_eq(callable, global_load_ptr("pcc_type_cls_float")) != 0:
            is_builtin = 1
        if ptr_eq(callable, global_load_ptr("pcc_type_cls_str")) != 0:
            is_builtin = 1
        if ptr_eq(callable, global_load_ptr("pcc_type_cls_list")) != 0:
            is_builtin = 1
        if ptr_eq(callable, global_load_ptr("pcc_type_cls_dict")) != 0:
            is_builtin = 1
        if ptr_eq(callable, global_load_ptr("pcc_type_cls_tuple")) != 0:
            is_builtin = 1
        if is_builtin != 0:
            if nkwargs != 0 or nargs > 1:
                py_raise_owned(
                    py_exc_new(
                        3,
                        cstr(
                            "native builtin constructor accepts at most one positional argument"
                        ),
                    )
                )
                return null()
            arg = null()
            if nargs == 1:
                arg = py_tuple_get(args, 0)
            out = null()
            if ptr_eq(callable, global_load_ptr("pcc_type_cls_bool")) != 0:
                truth: int = 0
                if ptr_is_null(arg) == 0:
                    truth = py_obj_truthy(arg)
                out = py_bool_from_bit(truth)
            elif ptr_eq(callable, global_load_ptr("pcc_type_cls_int")) != 0:
                if ptr_is_null(arg) != 0:
                    out = py_int_from_i64(0)
                elif _type_of(arg) == PY_TYPE_INT:
                    py_incref(arg)
                    out = arg
                elif _type_of(arg) == PY_TYPE_BOOL:
                    out = py_int_from_i64(py_obj_truthy(arg))
                elif _type_of(arg) == PY_TYPE_STR:
                    out = py_int_from_cstr_or_raise(py_str_utf8(arg), 10)
                else:
                    py_raise_owned(
                        py_exc_new(
                            3, cstr("int() argument must be a string or a real number")
                        )
                    )
            elif ptr_eq(callable, global_load_ptr("pcc_type_cls_float")) != 0:
                value: float = 0.0
                if ptr_is_null(arg) == 0:
                    value = py_float_value_of(arg)
                out = py_float_from_f64(value)
            elif ptr_eq(callable, global_load_ptr("pcc_type_cls_str")) != 0:
                if ptr_is_null(arg) != 0:
                    out = py_str_new(cstr(""), 0)
                else:
                    out = py_obj_str(arg)
            elif ptr_eq(callable, global_load_ptr("pcc_type_cls_list")) != 0:
                out = py_list_new(0)
                if ptr_is_null(out) == 0 and ptr_is_null(arg) == 0:
                    py_list_extend(out, arg)
                    if py_err_occurred() != 0:
                        py_decref(out)
                        out = null()
            elif ptr_eq(callable, global_load_ptr("pcc_type_cls_tuple")) != 0:
                if ptr_is_null(arg) != 0:
                    out = py_tuple_new(0)
                elif _type_of(arg) == PY_TYPE_TUPLE:
                    py_incref(arg)
                    out = arg
                else:
                    items = py_list_new(0)
                    if ptr_is_null(items) == 0:
                        py_list_extend(items, arg)
                        if py_err_occurred() == 0:
                            out = py_tuple_from_list(items)
                        py_decref(items)
            else:
                out = py_dict_new()
                if ptr_is_null(out) == 0 and ptr_is_null(arg) == 0:
                    if _type_of(arg) != PY_TYPE_DICT:
                        py_decref(out)
                        py_raise_owned(
                            py_exc_new(
                                11,
                                cstr("pcc dict(iterable) currently requires a dict"),
                            )
                        )
                        out = null()
                    else:
                        py_dict_update(out, arg)
            _require_call_result(
                out,
                cstr("native builtin constructor"),
                cstr(
                    "native builtin constructor returned NULL without setting an exception"
                ),
            )
            if ptr_is_null(arg) == 0:
                py_decref(arg)
            return out
        inst = py_instance_new(callable)
        if ptr_is_null(inst) != 0:
            return _require_call_result(
                null(),
                cstr("py_instance_new"),
                cstr("py_instance_new returned NULL without setting an exception"),
            )
        init_method = py_class_lookup(callable, cstr("__init__"))
        if ptr_is_null(init_method) == 0:
            if is_tagged_int(init_method) == 0:
                if load_i32(init_method, 8) == PY_TYPE_FUNC:  # PY_TYPE_FUNC
                    n: int = 0
                    if ptr_is_null(args) == 0:
                        n = py_tuple_len(args)
                    full_args = py_tuple_new(n + 1)
                    if ptr_is_null(full_args) != 0:
                        _require_call_result(
                            null(),
                            cstr("py_tuple_new"),
                            cstr(
                                "bound method call could not allocate its argument tuple"
                            ),
                        )
                        py_decref(inst)
                        return null()
                    py_tuple_set_item(full_args, 0, inst)
                    i: int = 0
                    while i < n:
                        item = py_tuple_get(args, i)
                        py_tuple_set_item(full_args, i + 1, item)
                        py_decref(item)
                        i = i + 1
                    out = py_func_call_kwargs(init_method, full_args, kwargs)
                    if ptr_is_null(out) != 0:
                        _require_call_result(
                            null(),
                            cstr("class __init__"),
                            cstr(
                                "class __init__ returned NULL without setting an exception"
                            ),
                        )
                    py_decref(full_args)
                    if ptr_is_null(out) != 0:
                        py_decref(inst)
                        return null()
                    py_decref(out)
        return inst
    if tag == PY_TYPE_FUNC:  # PY_TYPE_FUNC
        return _require_call_result(
            py_func_call_kwargs(callable, args, kwargs),
            cstr("py_func_call_kwargs"),
            cstr("py_func_call_kwargs returned NULL without setting an exception"),
        )
    if tag == PY_TYPE_WEAKREF:  # PY_TYPE_WEAKREF
        return _require_call_result(
            py_weakref_call(callable),
            cstr("py_weakref_call"),
            cstr("py_weakref_call returned NULL without setting an exception"),
        )
    if pcc_capi_is_cext_type_tag(tag) != 0:
        return _require_call_result(
            pcc_capi_call_cext_object(callable, args, kwargs),
            cstr("pcc_capi_call_cext_object"),
            cstr(
                "pcc_capi_call_cext_object returned NULL without setting an exception"
            ),
        )
    if _is_instance_tag(tag) != 0:
        cls = pcc_gc_load_ptr(
            callable, ptr_add(callable, PYINSTANCEOBJECT_CLS_OFFSET)
        )
        method = py_class_lookup(cls, cstr("__call__"))
        if ptr_is_null(method) == 0:
            return _require_call_result(
                _dispatch_call_method_with_args(method, callable, args, kwargs),
                cstr("instance __call__"),
                cstr("instance __call__ returned NULL without setting an exception"),
            )
    return _raise_not_callable(callable, tag)


@c_abi_export("py_obj_call_method1")
def py_obj_call_method1(o, name, arg):
    if ptr_is_null(o) != 0:
        return py_runtime_error_if_unset(
            cstr("py_obj_call_method1"),
            cstr("py_obj_call_method1 received NULL object"),
        )
    if ptr_is_null(name) != 0:
        return py_runtime_error_if_unset(
            cstr("py_obj_call_method1"),
            cstr("py_obj_call_method1 received NULL method name"),
        )
    if ptr_is_null(arg) != 0:
        return py_runtime_error_if_unset(
            cstr("py_obj_call_method1"),
            cstr("py_obj_call_method1 received NULL argument"),
        )
    method = py_obj_getattr(o, name)
    if ptr_is_null(method) != 0:
        return _require_call_result(
            null(),
            cstr("py_obj_getattr"),
            cstr("py_obj_getattr returned NULL without setting an exception"),
        )
    args = py_tuple_new(2)
    if ptr_is_null(args) != 0:
        _require_call_result(
            null(),
            cstr("py_tuple_new"),
            cstr("py_obj_call_method1 could not allocate its argument tuple"),
        )
        py_decref(method)
        return null()
    py_tuple_set_item(args, 0, o)
    py_tuple_set_item(args, 1, arg)
    out = py_obj_call(method, args, global_load_ptr("py_None"))
    _require_call_result(
        out,
        cstr("py_obj_call"),
        cstr(
            "py_obj_call_method1 callee returned NULL without setting an exception"
        ),
    )
    py_decref(method)
    py_decref(args)
    return out


@c_abi_export("py_obj_isinstance")
def py_obj_isinstance(o, cls) -> int:
    if ptr_is_null(o) != 0:
        return 0
    if ptr_is_null(cls) != 0:
        return 0
    if is_tagged_int(cls) != 0:
        return 0
    if load_i32(cls, 8) == PY_TYPE_TUPLE:
        count: int = py_tuple_len(cls)
        index: int = 0
        while index < count:
            candidate = py_tuple_get(cls, index)
            matched: int = py_obj_isinstance(o, candidate)
            py_decref(candidate)
            if matched != 0:
                return matched
            index += 1
        return 0
    if load_i32(cls, 8) != PY_TYPE_CLASS:  # PY_TYPE_CLASS
        return 0
    tag: int = _type_of(o)
    if ptr_eq(cls, global_load_ptr("pcc_type_cls_bool")) != 0:
        return 1 if tag == PY_TYPE_BOOL else 0
    if ptr_eq(cls, global_load_ptr("pcc_type_cls_int")) != 0:
        return 1 if tag == PY_TYPE_BOOL or tag == PY_TYPE_INT else 0
    if ptr_eq(cls, global_load_ptr("pcc_type_cls_float")) != 0:
        return 1 if tag == PY_TYPE_FLOAT else 0
    if ptr_eq(cls, global_load_ptr("pcc_type_cls_str")) != 0:
        return 1 if tag == PY_TYPE_STR else 0
    if ptr_eq(cls, global_load_ptr("pcc_type_cls_list")) != 0:
        return 1 if tag == PY_TYPE_LIST else 0
    if ptr_eq(cls, global_load_ptr("pcc_type_cls_dict")) != 0:
        return 1 if tag == PY_TYPE_DICT else 0
    if ptr_eq(cls, global_load_ptr("pcc_type_cls_tuple")) != 0:
        return 1 if tag == PY_TYPE_TUPLE else 0
    pcc_runtime_log_event_code(7, 9, _type_of(o), _type_of(cls), o)
    return py_isinstance(o, cls)


@c_abi_export("py_obj_issubclass")
def py_obj_issubclass(derived, cls) -> int:
    if ptr_is_null(derived) != 0 or is_tagged_int(derived) != 0:
        py_raise_owned(py_exc_new(3, cstr("issubclass() arg 1 must be a class")))
        return -1
    derived_is_capi_type: int = pcc_capi_is_type_object_value(derived)
    if derived_is_capi_type == 0 and load_i32(derived, 8) != PY_TYPE_CLASS:  # PY_TYPE_CLASS
        py_raise_owned(py_exc_new(3, cstr("issubclass() arg 1 must be a class")))
        return -1
    if ptr_is_null(cls) != 0 or is_tagged_int(cls) != 0:
        py_raise_owned(py_exc_new(3, cstr("issubclass() arg 2 must be a class")))
        return -1
    cls_is_capi_type: int = pcc_capi_is_type_object_value(cls)
    if cls_is_capi_type == 0 and load_i32(cls, 8) != PY_TYPE_CLASS:  # PY_TYPE_CLASS
        py_raise_owned(py_exc_new(3, cstr("issubclass() arg 2 must be a class")))
        return -1
    if derived_is_capi_type != 0 or cls_is_capi_type != 0:
        if derived_is_capi_type == 0 or cls_is_capi_type == 0:
            return 0
        return pcc_capi_type_object_issubclass(derived, cls)
    derived = pcc_gc_note_relocation_read(derived)
    cls = pcc_gc_note_relocation_read(cls)
    if ptr_eq(derived, cls) != 0:
        return 1
    n_mro: int = load_i32(derived, PYCLASSOBJECT_N_MRO_OFFSET)
    mro = load_ptr(derived, PYCLASSOBJECT_MRO_OFFSET)
    i: int = 0
    while i < n_mro:
        candidate = pcc_gc_load_ptr(
            derived, ptr_add(mro, i * C_POINTER_SIZE)
        )
        if ptr_eq(candidate, cls) != 0:
            return 1
        i += 1
    return 0
