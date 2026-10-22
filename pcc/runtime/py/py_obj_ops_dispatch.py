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
from pcc.runtime.py.py_abi_constants import (
    C_POINTER_SIZE,
    PYCLASSOBJECT_MRO_OFFSET,
    PYCLASSOBJECT_NAME_OFFSET,
    PYCLASSOBJECT_N_MRO_OFFSET,
    PYINSTANCEOBJECT_CLS_OFFSET,
    PYOBJECTHEADER_FLAGS_OFFSET,
    PYSTATICMETHODOBJECT_FUNC_OFFSET,
    PYTUPLEOBJECT_ITEMS_OFFSET,
    PYPROPERTYOBJECT_FGET_OFFSET,
    PYPROPERTYOBJECT_FSET_OFFSET,
    PYPROPERTYOBJECT_FDEL_OFFSET,
    PY_FLAG_EXC_SUPPRESS_CONTEXT,
    PY_FLAG_EXC_UNICODE_PAYLOAD,
    PY_FLAG_EXC_OS_PAYLOAD,
    PY_TYPE_BOOL,
    PY_TYPE_BYTEARRAY,
    PY_TYPE_BYTES,
    PY_TYPE_CEXT_TAG_BASE,
    PY_TYPE_CLASS,
    PY_TYPE_CLASSMETHOD,
    PY_TYPE_COMPLEX,
    PY_TYPE_CONTINUATION,
    PY_TYPE_COROUTINE,
    PY_TYPE_DICT,
    PY_TYPE_ELLIPSIS,
    PY_TYPE_NOTIMPLEMENTED,
    PY_TYPE_EXC,
    PY_TYPE_FILE,
    PY_TYPE_FLOAT,
    PY_TYPE_FUNC,
    PY_TYPE_INSTANCE,
    PY_TYPE_INT,
    PY_TYPE_LIST,
    PY_TYPE_MEMORYVIEW,
    PY_TYPE_NONE,
    PY_TYPE_PROPERTY,
    PY_TYPE_SET,
    PY_TYPE_STATICMETHOD,
    PY_TYPE_STR,
    PY_TYPE_THREAD_CONDITION,
    PY_TYPE_THREAD_EVENT,
    PY_TYPE_THREAD_LOCK,
    PY_TYPE_THREAD_RLOCK,
    PY_TYPE_THREAD_SEMAPHORE,
    PY_TYPE_TUPLE,
    PY_TYPE_USER_CLASS_START,
    PY_TYPE_VIRTUAL_THREAD,
    PY_TYPE_VTHREAD_CHANNEL,
    PY_TYPE_WEAKREF,
)
from pcc.unsafe import (
    store_ptr,
    stack_alloc,
    memset,
    define_global_i32,
    atomic_load_i32,
    atomic_rmw_i32,
    call_ptr1,
    call_ptr2,
    cstr,
    define_global_ptr_null,
    define_global_struct_words,
    free,
    global_addr,
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
    unsigned_div_i64,
)

pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
pcc_gc_root_copy_borrowed_lease = extern("pcc_gc_root_copy_borrowed_lease", (c_ptr, c_ptr), c_int64)
pcc_gc_root_copy_lease = extern("pcc_gc_root_copy_lease", (c_ptr, c_ptr), c_int64)
pcc_gc_foreign_lease_acquire = extern("pcc_gc_foreign_lease_acquire", (c_ptr,), c_int64)
pcc_gc_foreign_lease_release = extern("pcc_gc_foreign_lease_release", (c_ptr, c_int64), c_int64)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
pcc_gc_take_pinned_slot = extern("pcc_gc_take_pinned_slot", (c_ptr, c_int64), c_ptr)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
pcc_gc_note_slot_write_barrier = extern("pcc_gc_note_slot_write_barrier", (c_ptr, c_ptr, c_ptr), c_void)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)
py_tls_exc_swap_slot = extern("py_tls_exc_swap_slot", (c_ptr,), c_void)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_dict_update_slots = extern("py_dict_update_slots", (c_ptr, c_ptr), c_int64)
py_set_union = extern("py_set_union", (c_ptr, c_ptr), c_ptr)

py_int_value_i64 = extern("py_int_value_i64", (c_ptr,), c_int64)
strcmp = extern("strcmp", (c_ptr, c_ptr), c_int32)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_obj_as_int_object = extern("py_obj_as_int_object", (c_ptr, c_int64), c_ptr)
py_obj_as_int_object_args = extern("py_obj_as_int_object_args", (c_ptr, c_ptr), c_ptr)
py_bytes_decode_with_encoding = extern("py_bytes_decode_with_encoding", (c_ptr, c_ptr, c_ptr), c_ptr)
py_str_encode_with_encoding = extern("py_str_encode_with_encoding", (c_ptr, c_ptr, c_ptr), c_ptr)
py_bytes_new = extern("py_bytes_new", (c_ptr, c_int64), c_ptr)
py_bytes_from_obj = extern("py_bytes_from_obj", (c_ptr,), c_ptr)
py_bytearray_from_obj = extern("py_bytearray_from_obj", (c_ptr,), c_ptr)
py_complex_from_parts = extern("py_complex_from_parts", (c_ptr, c_ptr), c_ptr)
py_obj_index_i64 = extern("py_obj_index_i64", (c_ptr,), c_int64)

py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_str_len = extern("py_str_len", (c_ptr,), c_int64)
py_str_type_new = extern("py_str_type_new", (), c_ptr)
py_str_getattr = extern("py_str_getattr", (c_ptr, c_ptr), c_ptr)
py_builtin_exception_member = extern("py_builtin_exception_member", (c_ptr, c_ptr), c_ptr)
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
py_list_append = extern("py_list_append", (c_ptr, c_ptr), c_void)
py_list_del_slice = extern("py_list_del_slice", (c_ptr, c_ptr, c_ptr, c_ptr), c_int64)
py_obj_slice = extern("py_obj_slice", (c_ptr, c_ptr, c_ptr, c_ptr), c_ptr)
py_obj_set_slice = extern("py_obj_set_slice", (c_ptr, c_ptr, c_ptr, c_ptr, c_ptr), c_int64)
py_list_concat = extern("py_list_concat", (c_ptr, c_ptr), c_ptr)
py_list_new = extern("py_list_new", (c_int64,), c_ptr)
py_list_extend = extern("py_list_extend", (c_ptr, c_ptr), c_void)
py_list_insert = extern("py_list_insert", (c_ptr, c_int64, c_ptr), c_void)
py_list_remove = extern("py_list_remove", (c_ptr, c_ptr), c_void)
py_list_index_range = extern("py_list_index_range", (c_ptr, c_ptr, c_int64, c_int64), c_int64)
py_list_count = extern("py_list_count", (c_ptr, c_ptr), c_int64)
py_list_reverse = extern("py_list_reverse", (c_ptr,), c_void)
py_list_copy = extern("py_list_copy", (c_ptr,), c_ptr)
py_list_clear = extern("py_list_clear", (c_ptr,), c_void)

py_property_new = extern("py_property_new", (c_ptr, c_ptr, c_ptr), c_ptr)
py_classmethod_new = extern("py_classmethod_new", (c_ptr,), c_ptr)
py_staticmethod_new = extern("py_staticmethod_new", (c_ptr,), c_ptr)

py_tuple_get = extern("py_tuple_get", (c_ptr, c_int64), c_ptr)
py_tuple_len = extern("py_tuple_len", (c_ptr,), c_int64)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_tuple_concat = extern("py_tuple_concat", (c_ptr, c_ptr), c_ptr)
py_tuple_from_list = extern("py_tuple_from_list", (c_ptr,), c_ptr)

py_dict_get = extern("py_dict_get", (c_ptr, c_ptr), c_ptr)
py_dict_set = extern("py_dict_set", (c_ptr, c_ptr, c_ptr), c_void)
py_dict_del = extern("py_dict_del", (c_ptr, c_ptr), c_int64)
py_dict_get_default_slots = extern("py_dict_get_default_slots", (c_ptr, c_ptr, c_ptr, c_ptr), c_int64)
py_dict_getitem = extern("py_dict_getitem", (c_ptr, c_ptr), c_ptr)
py_dict_contains = extern("py_dict_contains", (c_ptr, c_ptr), c_int64)
py_dict_keys = extern("py_dict_keys", (c_ptr,), c_ptr)
py_dict_values = extern("py_dict_values", (c_ptr,), c_ptr)
py_dict_items = extern("py_dict_items", (c_ptr,), c_ptr)
py_dict_popitem = extern("py_dict_popitem", (c_ptr,), c_ptr)
py_dict_clear = extern("py_dict_clear", (c_ptr,), c_void)
py_dict_setdefault_slots = extern("py_dict_setdefault_slots", (c_ptr, c_ptr, c_ptr, c_ptr), c_int64)
py_copy_copy = extern("py_copy_copy", (c_ptr,), c_ptr)
py_dict_len = extern("py_dict_len", (c_ptr,), c_int64)
py_dict_new = extern("py_dict_new", (), c_ptr)
py_dict_update = extern("py_dict_update", (c_ptr, c_ptr), c_void)

py_set_len = extern("py_set_len", (c_ptr,), c_int64)
py_set_pop = extern("py_set_pop", (c_ptr,), c_ptr)
py_set_new = extern("py_set_new", (), c_ptr)
py_set_from_iterable = extern("py_set_from_iterable", (c_ptr,), c_ptr)
py_set_update = extern("py_set_update", (c_ptr, c_ptr), c_void)
py_set_intersection = extern("py_set_intersection", (c_ptr, c_ptr), c_ptr)
py_set_difference = extern("py_set_difference", (c_ptr, c_ptr), c_ptr)
py_threading_lock_acquire = extern(
    "py_threading_lock_acquire", (c_ptr,), c_int64
)
py_threading_lock_release = extern(
    "py_threading_lock_release", (c_ptr,), c_int64
)
py_threading_rlock_acquire = extern(
    "py_threading_rlock_acquire", (c_ptr,), c_int64
)
py_threading_rlock_release = extern(
    "py_threading_rlock_release", (c_ptr,), c_int64
)
py_threading_lock_locked = extern(
    "py_threading_lock_locked", (c_ptr,), c_int64
)
py_threading_semaphore_acquire = extern("py_threading_semaphore_acquire", (c_ptr,), c_int64)
py_threading_semaphore_release = extern("py_threading_semaphore_release", (c_ptr,), c_int64)
py_threading_event_set = extern("py_threading_event_set", (c_ptr,), c_int64)
py_threading_event_clear = extern("py_threading_event_clear", (c_ptr,), c_int64)
py_threading_event_is_set = extern("py_threading_event_is_set", (c_ptr,), c_int64)
py_threading_event_wait = extern("py_threading_event_wait", (c_ptr,), c_int64)
py_threading_condition_acquire = extern("py_threading_condition_acquire", (c_ptr,), c_int64)
py_threading_condition_release = extern("py_threading_condition_release", (c_ptr,), c_int64)
py_threading_condition_wait = extern("py_threading_condition_wait", (c_ptr,), c_int64)
py_threading_condition_wait_timeout = extern("py_threading_condition_wait_timeout", (c_ptr, c_ptr), c_int64)
py_threading_condition_notify = extern("py_threading_condition_notify", (c_ptr,), c_int64)
py_threading_condition_notify_all = extern("py_threading_condition_notify_all", (c_ptr,), c_int64)
py_threading_lock_acquire_args = extern(
    "py_threading_lock_acquire_args", (c_ptr, c_ptr, c_ptr), c_int64
)
py_threading_semaphore_acquire_args = extern(
    "py_threading_semaphore_acquire_args", (c_ptr, c_ptr, c_ptr), c_int64
)
py_threading_semaphore_release_args = extern(
    "py_threading_semaphore_release_args", (c_ptr, c_ptr), c_int64
)
py_threading_event_wait_args = extern(
    "py_threading_event_wait_args", (c_ptr, c_ptr), c_int64
)
py_set_symmetric_difference = extern(
    "py_set_symmetric_difference", (c_ptr, c_ptr), c_ptr
)

py_class_new = extern("py_class_new", (c_ptr, c_ptr, c_int32, c_ptr, c_int32), c_ptr)
py_class_new_from_objects = extern("py_class_new_from_objects", (c_ptr, c_ptr, c_ptr), c_ptr)
py_exception_subclass_init_slots = extern(
    "py_exception_subclass_init_slots", (c_ptr, c_ptr, c_ptr, c_ptr), c_int64
)
py_subs_object_root = extern("py_subs_object_root", (), c_ptr)
py_class_lookup = extern("py_class_lookup", (c_ptr, c_ptr), c_ptr)
py_class_getattr = extern("py_class_getattr", (c_ptr, c_ptr), c_ptr)
py_class_metaclass_call = extern("py_class_metaclass_call", (c_ptr, c_ptr, c_ptr), c_ptr)
py_class_setattr = extern("py_class_setattr", (c_ptr, c_ptr, c_ptr), c_int64)
py_class_delattr = extern("py_class_delattr", (c_ptr, c_ptr), c_int64)
py_instance_new = extern("py_instance_new", (c_ptr,), c_ptr)
py_tuple_type_new = extern("py_tuple_type_new", (), c_ptr)
py_tuple_getattr = extern("py_tuple_getattr", (c_ptr, c_ptr), c_ptr)
py_class_is_tuple_subclass = extern("py_class_is_tuple_subclass", (c_ptr,), c_int64)
py_tuple_class_call = extern("py_tuple_class_call", (c_ptr, c_ptr, c_ptr), c_ptr)
py_class_is_str_subclass = extern("py_class_is_str_subclass", (c_ptr,), c_int64)
py_str_subclass_new = extern("py_str_subclass_new", (c_ptr, c_ptr, c_ptr), c_ptr)
py_str_check = extern("py_str_check", (c_ptr,), c_int64)
py_instance_getattr = extern("py_instance_getattr", (c_ptr, c_ptr), c_ptr)
py_instance_getattr_default = extern(
    "py_instance_getattr_default", (c_ptr, c_ptr), c_ptr
)
py_instance_setattr = extern("py_instance_setattr", (c_ptr, c_ptr, c_ptr), c_int64)
py_instance_delattr = extern("py_instance_delattr", (c_ptr, c_ptr), c_int64)
py_isinstance = extern("py_isinstance", (c_ptr, c_ptr), c_int64)
py_exc_builtin_class = extern("py_exc_builtin_class", (c_int64,), c_ptr)
py_exc_traceback_object = extern("py_exc_traceback_object", (c_ptr,), c_ptr)
py_exc_get_args = extern("py_exc_get_args", (c_ptr,), c_ptr)
py_exc_get_legacy_value = extern("py_exc_get_legacy_value", (c_ptr,), c_ptr)
py_file_getattr = extern("py_file_getattr", (c_ptr, c_ptr), c_ptr)
py_unicode_decode_error_new = extern("py_unicode_decode_error_new", (c_ptr,), c_ptr)
py_unicode_encode_error_new = extern("py_unicode_encode_error_new", (c_ptr,), c_ptr)
py_unicode_error_get_field = extern("py_unicode_error_get_field", (c_ptr, c_int64), c_ptr)
py_unicode_error_set_field = extern("py_unicode_error_set_field", (c_ptr, c_int64, c_ptr), c_int64)
py_os_error_get_field = extern("py_os_error_get_field", (c_ptr, c_int64), c_ptr)
py_os_error_set_field = extern("py_os_error_set_field", (c_ptr, c_int64, c_ptr), c_int64)
py_os_error_new = extern("py_os_error_new", (c_int64, c_ptr), c_ptr)
py_exc_new_with_class = extern("py_exc_new_with_class", (c_ptr, c_ptr), c_ptr)
py_exc_matches = extern("py_exc_matches", (c_ptr, c_ptr), c_int64)
py_file_type_kind = extern("py_file_type_kind", (c_ptr,), c_int64)
py_class_attrs_dict = extern("py_class_attrs_dict", (c_ptr, c_int64), c_ptr)
py_class_setattr_raw = extern("py_class_setattr_raw", (c_ptr, c_ptr, c_ptr), c_int64)
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
py_func_kind = extern("py_func_kind", (c_ptr,), c_int64)
py_builtin_callable = extern("py_builtin_callable", (c_ptr,), c_ptr)
py_instance_bind_method = extern("py_instance_bind_method", (c_ptr, c_ptr, c_ptr), c_ptr)
py_current_exception = extern("py_current_exception", (), c_ptr)
py_str_payload = extern("py_str_payload", (c_ptr,), c_ptr)
py_str_ord = extern("py_str_ord", (c_ptr,), c_int64)
py_str_ord_at_i64 = extern("py_str_ord_at_i64", (c_ptr, c_int64), c_int64)
py_runtime_error_if_unset = extern(
    "py_runtime_error_if_unset", (c_ptr, c_ptr), c_ptr
)
py_gen_send = extern("py_gen_send", (c_ptr, c_ptr), c_ptr)
py_user_ternary_dispatch = extern(
    "py_user_ternary_dispatch", (c_ptr, c_ptr, c_ptr, c_ptr, c_ptr, c_ptr), c_ptr
)
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
py_complex_sub = extern("py_complex_sub", (c_ptr, c_ptr), c_ptr)
py_complex_pow = extern("py_complex_pow", (c_ptr, c_ptr), c_ptr)
py_float_pow = extern("py_float_pow", (c_ptr, c_ptr), c_ptr)
py_int_pow = extern("py_int_pow", (c_ptr, c_ptr), c_ptr)
py_int_pow_mod = extern("py_int_pow_mod", (c_ptr, c_ptr, c_ptr), c_ptr)
py_complex_mul = extern("py_complex_mul", (c_ptr, c_ptr), c_ptr)
py_complex_div = extern("py_complex_div", (c_ptr, c_ptr), c_ptr)
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
py_coroutine_bound_method = extern("py_coroutine_bound_method", (c_ptr, c_int64), c_ptr)
py_continuation_class = extern("py_continuation_class", (), c_ptr)
py_bytes_len = extern("py_bytes_len", (c_ptr,), c_int64)
py_bytes_getitem = extern("py_bytes_getitem", (c_ptr, c_ptr), c_ptr)
py_bytes_concat = extern("py_bytes_concat", (c_ptr, c_ptr), c_ptr)
py_bytearray_setitem = extern("py_bytearray_setitem", (c_ptr, c_ptr, c_ptr), c_int64)
py_bytearray_del_slice = extern(
    "py_bytearray_del_slice", (c_ptr, c_ptr, c_ptr, c_ptr), c_int64
)
py_obj_call_sync = extern("py_obj_call_sync", (c_ptr, c_ptr, c_ptr), c_ptr)
py_obj_call_default_sync = extern("py_obj_call_default_sync", (c_ptr, c_ptr, c_ptr), c_ptr)
py_obj_call_context_is_deferred = extern("py_obj_call_context_is_deferred", (), c_int64)


py_decref = extern("py_decref", (c_ptr,), c_void)
py_incref = extern("py_incref", (c_ptr,), c_void)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)
pcc_gc_store_ptr = extern("pcc_gc_store_ptr", (c_ptr, c_ptr, c_ptr), c_void)
pcc_gc_note_relocation_read = extern(
    "pcc_gc_note_relocation_read",
    (c_ptr,),
    c_ptr,
)
pcc_diagnostics_runtime_log_event_code = extern(
    "pcc_diagnostics_runtime_log_event_code",
    (c_int32, c_int32, c_int64, c_int64, c_ptr),
    c_void,
)


define_global_ptr_null("pcc_type_cls_none")
define_global_ptr_null("pcc_type_cls_ellipsis")
define_global_ptr_null("pcc_type_cls_notimplemented")
define_global_ptr_null("pcc_type_cls_function")
define_global_ptr_null("pcc_type_cls_builtin_function")
define_global_ptr_null("pcc_type_cls_method")
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
define_global_ptr_null("pcc_type_cls_staticmethod")
define_global_ptr_null("pcc_type_cls_classmethod")
define_global_ptr_null("pcc_type_cls_property")
define_global_ptr_null("pcc_type_cls_object")
define_global_ptr_null("pcc_type_cls_super")
define_global_ptr_null("pcc_type_cls_textiowrapper")
define_global_ptr_null("pcc_type_cls_bufferedreader")
define_global_ptr_null("pcc_type_cls_bufferedwriter")
define_global_ptr_null("pcc_type_cls_bufferedrandom")
define_global_ptr_null("pcc_slice_cls")


define_global_struct_words(
    "pcc_builtin_type_root_slots",
    "pcc_type_cls_none",
    "pcc_type_cls_ellipsis",
    "pcc_type_cls_notimplemented",
    "pcc_type_cls_function",
    "pcc_type_cls_builtin_function",
    "pcc_type_cls_method",
    "pcc_type_cls_bool",
    "pcc_type_cls_int",
    "pcc_type_cls_float",
    "pcc_type_cls_str",
    "pcc_type_cls_list",
    "pcc_type_cls_dict",
    "pcc_type_cls_tuple",
    "pcc_type_cls_set",
    "pcc_type_cls_type",
    "pcc_type_cls_complex",
    "pcc_type_cls_bytes",
    "pcc_type_cls_bytearray",
    "pcc_type_cls_memoryview",
    "pcc_type_cls_coroutine",
    "pcc_type_cls_staticmethod",
    "pcc_type_cls_classmethod",
    "pcc_type_cls_property",
    "pcc_type_cls_object",
    "pcc_type_cls_super",
    "pcc_type_cls_textiowrapper",
    "pcc_type_cls_bufferedreader",
    "pcc_type_cls_bufferedwriter",
    "pcc_type_cls_bufferedrandom",
    "pcc_slice_cls",
    0,
)


def _cstr_is_dunder_class(s) -> int:
    if load_i8(s, 0) != 95:
        return 0
    if load_i8(s, 1) != 95:
        return 0
    if strlen(s) != 9:
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


def _cstr_is_dunder_dict(s) -> int:
    return (load_i8(s, 0) == 95 and load_i8(s, 1) == 95
            and load_i8(s, 2) == 100 and load_i8(s, 3) == 105
            and load_i8(s, 4) == 99 and load_i8(s, 5) == 116
            and load_i8(s, 6) == 95 and load_i8(s, 7) == 95
            and load_i8(s, 8) == 0)


def _cstr_is_dunder_name(s) -> int:
    if load_i8(s, 0) != 95:
        return 0
    if load_i8(s, 1) != 95:
        return 0
    if strlen(s) != 8:
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
    if load_i8(s, 0) != 95:
        return 0
    if load_i8(s, 1) != 95:
        return 0
    if strlen(s) != 12:
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
    if load_i8(s, 0) != 95:
        return 0
    if load_i8(s, 1) != 95:
        return 0
    if strlen(s) != 7:
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
    if load_i8(s, 0) != 95:
        return 0
    if load_i8(s, 1) != 95:
        return 0
    if strlen(s) != 8:
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
    if load_i8(s, 0) != 95:
        return 0
    if load_i8(s, 1) != 95:
        return 0
    if strlen(s) != 8:
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
    if load_i8(s, 0) != 95:
        return 0
    if load_i8(s, 1) != 95:
        return 0
    if strlen(s) != 9:
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
    if load_i8(s, 0) != 95:
        return 0
    if load_i8(s, 1) != 95:
        return 0
    if strlen(s) != 11:
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


def _complex_numeric_pair(at: int, bt: int) -> int:
    """A complex operand against int, bool, float or complex.

    Only these pairs promote to complex; ``complex - str`` and friends must
    still reach the protocol and its TypeError.
    """
    if at != PY_TYPE_COMPLEX and bt != PY_TYPE_COMPLEX:
        return 0
    if at != PY_TYPE_INT and at != PY_TYPE_BOOL and at != PY_TYPE_FLOAT and at != PY_TYPE_COMPLEX:
        return 0
    if bt != PY_TYPE_INT and bt != PY_TYPE_BOOL and bt != PY_TYPE_FLOAT and bt != PY_TYPE_COMPLEX:
        return 0
    return 1


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
    if ptr_eq(o, global_load_ptr("py_NotImplemented")) != 0:
        # Python 3.14+: NotImplementedType.__bool__ raises.
        py_raise_owned(py_exc_new(3, cstr("NotImplemented should not be used in a boolean context")))
        return 0
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
    # A class's truth is its metaclass's __bool__/__len__: the special lookup
    # on a class object resolves its metaclass, as CPython's type slots do.
    if _is_instance_tag(tag) != 0 or tag == PY_TYPE_CLASS:
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
    # A builtin numeric operand must not consume a user instance before
    # its forward/reflected protocol gets a chance to answer. The
    # numeric helpers below only implement builtin numeric pairs.
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
    if (at == PY_TYPE_INT or at == PY_TYPE_BOOL) and (bt == PY_TYPE_INT or bt == PY_TYPE_BOOL):
        return py_int_add(a, b)
    if _complex_numeric_pair(at, bt) != 0:
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
    return py_raise_binary_type_error(cstr("unsupported operand type(s) for +"), a, b)


@c_abi_export("py_obj_sub")
def py_obj_sub(a, b):
    # Generic ``a - b`` for dynamically-typed operands. Mirrors py_obj_add:
    # int/bool -> py_int_sub (bignum); any float -> py_float_sub (coerces the
    # other numeric operand); set - set -> py_set_difference.  Fixes
    # boxed-float ``-`` (e.g. ``obj.attr - n`` where attr is a float) which fell
    # to the i64 path and misread the boxed pointer.
    #
    # Subtraction is NOT numeric-only: `a - b` on sets is difference, and the
    # sibling bitwise dispatcher already handles set `&`, `|` and `^`.  Leaving
    # `-` out meant a set whose static type had widened to dyn -- e.g. after
    # `provided |= member` with a dynamically-typed `member` -- raised
    # "unsupported operand type(s) for -" while both operands were sets.  pcc1
    # hit exactly that selecting archive members.
    if ptr_is_null(a) != 0 or ptr_is_null(b) != 0:
        py_raise_owned(py_exc_new(3, cstr("unsupported operand type(s) for -")))
        return null()
    at: int = _type_of(a)
    bt: int = _type_of(b)
    if (at == PY_TYPE_INT or at == PY_TYPE_BOOL) and (bt == PY_TYPE_INT or bt == PY_TYPE_BOOL):
        return py_int_sub(a, b)
    if (at == PY_TYPE_FLOAT or at == PY_TYPE_INT or at == PY_TYPE_BOOL) and (bt == PY_TYPE_FLOAT or bt == PY_TYPE_INT or bt == PY_TYPE_BOOL):
        return py_float_sub(a, b)
    if _complex_numeric_pair(at, bt) != 0:
        return py_complex_sub(a, b)
    if at == PY_TYPE_SET and bt == PY_TYPE_SET:
        return py_set_difference(a, b)
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
    return py_raise_binary_type_error(cstr("unsupported operand type(s) for -"), a, b)


def _unsupported_operands_text(operator, a, b):
    """CPython: ``unsupported operand type(s) for <op>: 'A' and 'B'``.

    Returns a malloc'd C string (the caller frees it) or NULL.
    """
    left = _obj_type_name_cstr(a)
    right = _obj_type_name_cstr(b)
    total: int = strlen(operator) + strlen(left) + strlen(right) + 48
    buf = malloc(total)
    if ptr_is_null(buf) != 0:
        return null()
    pos: int = _cstr_append(buf, 0, cstr("unsupported operand type(s) for "))
    pos = _cstr_append(buf, pos, operator)
    pos = _cstr_append(buf, pos, cstr(": '"))
    pos = _cstr_append(buf, pos, left)
    pos = _cstr_append(buf, pos, cstr("' and '"))
    pos = _cstr_append(buf, pos, right)
    pos = _cstr_append(buf, pos, cstr("'"))
    store_i8(buf, pos, 0)
    return buf


@c_abi_export("py_raise_binary_type_error")
def py_raise_binary_type_error(prefix, a, b):
    """Raise CPython's ``<prefix>: 'A' and 'B'`` TypeError; returns NULL.

    ``prefix`` names the operation (``unsupported operand type(s) for +``);
    the type names come from the operands without allocating.
    """
    left = _obj_type_name_cstr(a)
    right = _obj_type_name_cstr(b)
    buf = malloc(strlen(prefix) + strlen(left) + strlen(right) + 16)
    if ptr_is_null(buf) != 0:
        py_raise_owned(py_exc_new(3, prefix))
        return null()
    pos: int = _cstr_append(buf, 0, prefix)
    pos = _cstr_append(buf, pos, cstr(": '"))
    pos = _cstr_append(buf, pos, left)
    pos = _cstr_append(buf, pos, cstr("' and '"))
    pos = _cstr_append(buf, pos, right)
    pos = _cstr_append(buf, pos, cstr("'"))
    store_i8(buf, pos, 0)
    py_raise_owned(py_exc_new(3, buf))
    free(buf)
    return null()


@c_abi_export("py_raise_type_named_error")
def py_raise_type_named_error(prefix, o, suffix):
    """Raise TypeError ``<prefix><type(o).__name__><suffix>``; returns NULL."""
    name = _obj_type_name_cstr(o)
    buf = malloc(strlen(prefix) + strlen(name) + strlen(suffix) + 1)
    if ptr_is_null(buf) != 0:
        py_raise_owned(py_exc_new(3, prefix))
        return null()
    pos: int = _cstr_append(buf, 0, prefix)
    pos = _cstr_append(buf, pos, name)
    pos = _cstr_append(buf, pos, suffix)
    store_i8(buf, pos, 0)
    py_raise_owned(py_exc_new(3, buf))
    free(buf)
    return null()


@c_abi_export("py_raise_conversion_result_error")
def py_raise_conversion_result_error(receiver, method_text, result):
    """CPython: ``X.__index__() must return an int, not float``; NULL.

    ``method_text`` is the part after the receiver's type name, for example
    ``.__index__() must return an int, not ``.
    """
    receiver_name = _obj_type_name_cstr(receiver)
    result_name = _obj_type_name_cstr(result)
    buf = malloc(strlen(receiver_name) + strlen(method_text) + strlen(result_name) + 1)
    if ptr_is_null(buf) != 0:
        py_raise_owned(py_exc_new(3, method_text))
        return null()
    pos: int = _cstr_append(buf, 0, receiver_name)
    pos = _cstr_append(buf, pos, method_text)
    pos = _cstr_append(buf, pos, result_name)
    store_i8(buf, pos, 0)
    py_raise_owned(py_exc_new(3, buf))
    free(buf)
    return null()


def _raise_unsupported_operands(operator, a, b):
    text = _unsupported_operands_text(operator, a, b)
    if ptr_is_null(text) != 0:
        py_raise_owned(py_exc_new(3, cstr("unsupported operand type(s)")))
        return null()
    py_raise_owned(py_exc_new(3, text))
    free(text)
    return null()


@c_abi_export("py_obj_pow")
def py_obj_pow(a, b):
    """Generic ``a ** b`` (two-argument ``pow``) for dynamically typed operands.

    Integers keep exact power (a negative exponent goes to float_pow as in
    CPython), any float operand uses CPython's float_pow, complex operands
    complex power, and user classes ``__pow__``/``__rpow__`` with reflected
    and subclass-first dispatch.
    """
    if ptr_is_null(a) != 0 or ptr_is_null(b) != 0:
        return _raise_unsupported_operands(cstr("** or pow()"), a, b)
    at: int = _type_of(a)
    bt: int = _type_of(b)
    if (at == PY_TYPE_INT or at == PY_TYPE_BOOL) and (bt == PY_TYPE_INT or bt == PY_TYPE_BOOL):
        return py_int_pow(a, b)
    if (at == PY_TYPE_FLOAT or at == PY_TYPE_INT or at == PY_TYPE_BOOL) and (bt == PY_TYPE_FLOAT or bt == PY_TYPE_INT or bt == PY_TYPE_BOOL):
        return py_float_pow(a, b)
    if _complex_numeric_pair(at, bt) != 0:
        return py_complex_pow(a, b)
    if (
        at == PY_TYPE_INSTANCE
        or at >= PY_TYPE_USER_CLASS_START
        or bt == PY_TYPE_INSTANCE
        or bt >= PY_TYPE_USER_CLASS_START
    ):
        return py_user_binop_dispatch(
            a, b, cstr("__pow__"), cstr("__rpow__"),
            cstr("unsupported operand type(s) for ** or pow()"),
        )
    return _raise_unsupported_operands(cstr("** or pow()"), a, b)


def _pow_slot_kind(tag: int) -> int:
    """The nb_power slot CPython's ternary_op finds on a type.

    1 long_pow (int, bool), 2 float_pow, 3 complex_pow, 4 slot_nb_power (a
    Python class; it answers NotImplemented without __pow__/__rpow__), 0 none.
    """
    if tag == PY_TYPE_INT or tag == PY_TYPE_BOOL:
        return 1
    if tag == PY_TYPE_FLOAT:
        return 2
    if tag == PY_TYPE_COMPLEX:
        return 3
    if _is_instance_tag(tag) != 0:
        return 4
    return 0


def _instance_extends_builtin(o, tag: int) -> int:
    """Whether instance ``o``'s class has the builtin type ``tag`` on its MRO."""
    if is_tagged_int(o) != 0 or _is_instance_tag(_type_of(o)) == 0:
        return 0
    cls = pcc_gc_load_ptr(o, ptr_add(o, PYINSTANCEOBJECT_CLS_OFFSET))
    if ptr_is_null(cls) != 0:
        return 0
    n_mro: int = load_i32(cls, PYCLASSOBJECT_N_MRO_OFFSET)
    mro = load_ptr(cls, PYCLASSOBJECT_MRO_OFFSET)
    found: int = 0
    index: int = 0
    while found == 0 and index < n_mro:
        owner = pcc_gc_load_ptr(cls, ptr_add(mro, index * C_POINTER_SIZE))
        if py_builtin_type_class_tag(owner) == tag:
            found = 1
        index = index + 1
    return found


def _complex_pow_operand(o) -> int:
    """complex_pow's TO_COMPLEX: 1 converts, 0 NotImplemented, -1 raised."""
    tag: int = _type_of(o)
    if tag == PY_TYPE_COMPLEX or tag == PY_TYPE_FLOAT or tag == PY_TYPE_BOOL:
        return 1
    if tag != PY_TYPE_INT:
        return 0
    if is_tagged_int(o) == 0:
        number: float = py_float_to_f64(o)
        if number != 0.0 and number == number * 2.0:
            py_raise_owned(py_exc_new(15, cstr("int too large to convert to float")))
            return -1
    return 1


def _ternary_pow_slot(kind: int, a, b, m, outcome):
    """Run one nb_power slot; outcome 1 result, 0 NotImplemented, -1 raised."""
    store_i64(outcome, 0, 0)
    if kind == 1:
        # long_pow answers NotImplemented unless all three are ints.
        if (_pow_slot_kind(_type_of(a)) == 1 and _pow_slot_kind(_type_of(b)) == 1
                and _pow_slot_kind(_type_of(m)) == 1):
            result = py_int_pow_mod(a, b, m)
            store_i64(outcome, 0, -1 if ptr_is_null(result) != 0 else 1)
            return result
        return null()
    if kind == 2:
        # float_pow refuses a modulus before it converts either operand.
        py_raise_owned(py_exc_new(3, cstr("pow() 3rd argument not allowed unless all arguments are integers")))
        store_i64(outcome, 0, -1)
        return null()
    if kind == 3:
        # complex_pow converts both operands first, then refuses the modulus.
        converted: int = _complex_pow_operand(a)
        if converted == 1:
            converted = _complex_pow_operand(b)
        if converted == 1:
            py_raise_owned(py_exc_new(2, cstr("complex modulo")))
            converted = -1
        if converted < 0:
            store_i64(outcome, 0, -1)
        return null()
    if kind == 4:
        handled = stack_alloc(8)
        store_i64(handled, 0, 0)
        result = py_user_ternary_dispatch(a, b, m, cstr("__pow__"), cstr("__rpow__"), handled)
        if load_i64(handled, 0) == 0:
            return null()
        store_i64(outcome, 0, -1 if ptr_is_null(result) != 0 else 1)
        return result
    return null()


def _raise_unsupported_ternary(a, b, m):
    """CPython: ``unsupported operand type(s) for ** or pow(): 'A', 'B', 'C'``."""
    first = _obj_type_name_cstr(a)
    second = _obj_type_name_cstr(b)
    third = _obj_type_name_cstr(m)
    buf = malloc(strlen(first) + strlen(second) + strlen(third) + 64)
    if ptr_is_null(buf) != 0:
        py_raise_owned(py_exc_new(3, cstr("unsupported operand type(s) for ** or pow()")))
        return null()
    pos: int = _cstr_append(buf, 0, cstr("unsupported operand type(s) for ** or pow(): '"))
    pos = _cstr_append(buf, pos, first)
    pos = _cstr_append(buf, pos, cstr("', '"))
    pos = _cstr_append(buf, pos, second)
    pos = _cstr_append(buf, pos, cstr("', '"))
    pos = _cstr_append(buf, pos, third)
    pos = _cstr_append(buf, pos, cstr("'"))
    store_i8(buf, pos, 0)
    py_raise_owned(py_exc_new(3, buf))
    free(buf)
    return null()


@c_abi_export("py_obj_ternary_pow")
def py_obj_ternary_pow(a, b, m):
    """``pow(base, exp, mod)``: CPython's ternary_op over nb_power.

    A None modulus is the binary protocol. Otherwise the base's slot runs
    first (the exponent's first when its class derives from the base's
    type), then the exponent's, then the modulus's when it differs from
    both. float_pow and complex_pow refuse any modulus; a Python class's
    __pow__/__rpow__ receive it (3.14+); the error names all three types.
    """
    if ptr_is_null(a) != 0 or ptr_is_null(b) != 0 or ptr_is_null(m) != 0:
        py_runtime_error_if_unset(cstr("pow"), cstr("missing operand"))
        return null()
    if ptr_eq(m, global_load_ptr("py_None")) != 0:
        return py_obj_pow(a, b)
    at: int = _type_of(a)
    slot_v: int = _pow_slot_kind(at)
    slot_w: int = _pow_slot_kind(_type_of(b))
    if slot_w == slot_v:
        slot_w = 0
    outcome = stack_alloc(8)
    store_i64(outcome, 0, 0)
    result = null()
    if slot_v != 0:
        if slot_w == 4 and _instance_extends_builtin(b, at) != 0:
            result = _ternary_pow_slot(slot_w, a, b, m, outcome)
            if load_i64(outcome, 0) != 0:
                return result
            slot_w = 0
        result = _ternary_pow_slot(slot_v, a, b, m, outcome)
        if load_i64(outcome, 0) != 0:
            return result
    if slot_w != 0:
        result = _ternary_pow_slot(slot_w, a, b, m, outcome)
        if load_i64(outcome, 0) != 0:
            return result
    slot_z: int = _pow_slot_kind(_type_of(m))
    if slot_z != 0 and slot_z != slot_v and slot_z != slot_w:
        result = _ternary_pow_slot(slot_z, a, b, m, outcome)
        if load_i64(outcome, 0) != 0:
            return result
    return _raise_unsupported_ternary(a, b, m)


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
    if _complex_numeric_pair(at, bt) != 0:
        return py_complex_mul(a, b)
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
    return py_raise_binary_type_error(cstr("unsupported operand type(s) for *"), a, b)


def _union_operand_len(obj, tag: int) -> int:
    # A PEP 604 union member: a class object counts as one, and a tuple is an
    # already-built union being extended (`A | B | C` folds left).
    if tag == PY_TYPE_CLASS:
        return 1
    if tag == PY_TYPE_TUPLE:
        return py_tuple_len(obj)
    return -1


_BITWISE_LEFT = 0
_BITWISE_RIGHT = 1
_BITWISE_RESULT = 2
_BITWISE_ITEM = 3
_BITWISE_ERROR = 4
_BITWISE_SLOT_COUNT = 5
_BITWISE_INCOMING_COUNT = 2

define_global_i32("pcc_bitwise_borrowed_map", -2)
define_global_i32("pcc_bitwise_owned_map", 5)


def _bitwise_error(kind: int, message: c_ptr) -> int:
    if py_err_occurred() == 0:
        py_raise_owned(py_exc_new(kind, message))
    return -1


def _bitwise_adopt(slots: c_ptr, tokens: c_ptr, index: int) -> int:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    token: int = pcc_gc_foreign_lease_acquire(slot)
    if token < 0:
        return _bitwise_error(7, cstr("binary bitwise result lease failed"))
    store_i64(tokens, index * C_POINTER_SIZE, token)
    pcc_py_gc_minor_graph_lock()
    pcc_gc_note_slot_write_barrier(null(), slot, load_ptr(slot, 0))
    pcc_py_gc_minor_graph_unlock()
    if ptr_is_null(load_ptr(slot, 0)) != 0:
        return _bitwise_error(19, cstr("binary bitwise result allocation failed"))
    return -1 if py_err_occurred() != 0 else 0


def _bitwise_clear(slots: c_ptr, tokens: c_ptr, index: int) -> None:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    token: int = load_i64(tokens, index * C_POINTER_SIZE)
    if token >= 0:
        if pcc_gc_foreign_lease_release(slot, token) != 0:
            pcc_platform_abort()
            return
    store_i64(tokens, index * C_POINTER_SIZE, -1)
    pcc_gc_store_root(slot, null())


def _bitwise_drop(slots: c_ptr, tokens: c_ptr, index: int) -> None:
    error = ptr_add(slots, _BITWISE_ERROR * C_POINTER_SIZE)
    py_tls_exc_swap_slot(error)
    _bitwise_clear(slots, tokens, index)
    py_clear_exception()
    py_tls_exc_swap_slot(error)


def _bitwise_union_join(slots: c_ptr, tokens: c_ptr, a_len: int, b_len: int, at: int, bt: int) -> int:
    # Preserve the existing tuple representation and member order. Each NEW
    # tuple getter owns a separate temporary until the destination retains it.
    result = ptr_add(slots, _BITWISE_RESULT * C_POINTER_SIZE)
    item = ptr_add(slots, _BITWISE_ITEM * C_POINTER_SIZE)
    store_ptr(result, 0, py_tuple_new(a_len + b_len))
    if _bitwise_adopt(slots, tokens, _BITWISE_RESULT) != 0:
        return -1
    side: int = 0
    position: int = 0
    while side < _BITWISE_INCOMING_COUNT:
        source = ptr_add(slots, side * C_POINTER_SIZE)
        tag: int = at if side == _BITWISE_LEFT else bt
        length: int = a_len if side == _BITWISE_LEFT else b_len
        index: int = 0
        while index < length:
            if tag == PY_TYPE_CLASS:
                py_tuple_set_item(load_ptr(result, 0), position, load_ptr(source, 0))
            else:
                store_ptr(item, 0, py_tuple_get(load_ptr(source, 0), index))
                if _bitwise_adopt(slots, tokens, _BITWISE_ITEM) != 0:
                    return -1
                py_tuple_set_item(load_ptr(result, 0), position, load_ptr(item, 0))
                _bitwise_drop(slots, tokens, _BITWISE_ITEM)
            if py_err_occurred() != 0:
                return -1
            position = position + 1
            index = index + 1
        side = side + 1
    return 0


def _bitwise_body(slots: c_ptr, tokens: c_ptr, op: int) -> int:
    left = ptr_add(slots, _BITWISE_LEFT * C_POINTER_SIZE)
    right = ptr_add(slots, _BITWISE_RIGHT * C_POINTER_SIZE)
    result = ptr_add(slots, _BITWISE_RESULT * C_POINTER_SIZE)
    a = load_ptr(left, 0)
    b = load_ptr(right, 0)
    if ptr_is_null(a) != 0 or ptr_is_null(b) != 0:
        return _bitwise_unsupported(op)
    at: int = _type_of(a)
    bt: int = _type_of(b)
    if pcc_capi_is_cext_type_tag(at) != 0 or pcc_capi_is_cext_type_tag(bt) != 0:
        cext_op: int = 8
        if op == 1:
            cext_op = 10
        elif op == 2:
            cext_op = 9
        store_ptr(result, 0, pcc_capi_cext_binary_number(a, b, cext_op))
        return _bitwise_adopt(slots, tokens, _BITWISE_RESULT)
    if at == PY_TYPE_BOOL and bt == PY_TYPE_BOOL:
        # Both actual bool operands produce the canonical bool singleton.
        av: int = 1 if ptr_eq(a, global_load_ptr("py_True")) != 0 else 0
        bv: int = 1 if ptr_eq(b, global_load_ptr("py_True")) != 0 else 0
        value: int = av & bv
        if op == 1:
            value = av | bv
        elif op == 2:
            value = av ^ bv
        chosen = global_load_ptr("py_True") if value != 0 else global_load_ptr("py_False")
        py_incref(chosen)
        store_ptr(result, 0, chosen)
        return _bitwise_adopt(slots, tokens, _BITWISE_RESULT)
    if (at == PY_TYPE_INT or at == PY_TYPE_BOOL) and (bt == PY_TYPE_INT or bt == PY_TYPE_BOOL):
        if op == 0:
            store_ptr(result, 0, py_int_and(a, b))
        elif op == 1:
            store_ptr(result, 0, py_int_or(a, b))
        else:
            store_ptr(result, 0, py_int_xor(a, b))
        return _bitwise_adopt(slots, tokens, _BITWISE_RESULT)
    if op == 1:
        a_union: int = _union_operand_len(a, at)
        b_union: int = _union_operand_len(b, bt)
        if a_union >= 0 and b_union >= 0 and (at == PY_TYPE_CLASS or bt == PY_TYPE_CLASS):
            return _bitwise_union_join(slots, tokens, a_union, b_union, at, bt)
    if at == PY_TYPE_SET and bt == PY_TYPE_SET:
        if op == 0:
            store_ptr(result, 0, py_set_intersection(a, b))
        elif op == 1:
            store_ptr(result, 0, py_set_union(a, b))
        else:
            store_ptr(result, 0, py_set_symmetric_difference(a, b))
        return _bitwise_adopt(slots, tokens, _BITWISE_RESULT)
    if op == 1 and at == PY_TYPE_DICT and bt == PY_TYPE_DICT:
        store_ptr(result, 0, py_dict_new())
        if _bitwise_adopt(slots, tokens, _BITWISE_RESULT) != 0:
            return -1
        if py_dict_update_slots(result, left) != 0:
            return -1
        return py_dict_update_slots(result, right)
    if at == PY_TYPE_INSTANCE or at >= PY_TYPE_USER_CLASS_START or bt == PY_TYPE_INSTANCE or bt >= PY_TYPE_USER_CLASS_START:
        if op == 0:
            store_ptr(result, 0, py_user_binop_dispatch(a, b, cstr("__and__"), cstr("__rand__"), cstr("unsupported operand type(s) for &")))
        elif op == 1:
            store_ptr(result, 0, py_user_binop_dispatch(a, b, cstr("__or__"), cstr("__ror__"), cstr("unsupported operand type(s) for |")))
        else:
            store_ptr(result, 0, py_user_binop_dispatch(a, b, cstr("__xor__"), cstr("__rxor__"), cstr("unsupported operand type(s) for ^")))
        return _bitwise_adopt(slots, tokens, _BITWISE_RESULT)
    if op == 0:
        py_raise_binary_type_error(cstr("unsupported operand type(s) for &"), a, b)
    elif op == 1:
        py_raise_binary_type_error(cstr("unsupported operand type(s) for |"), a, b)
    else:
        py_raise_binary_type_error(cstr("unsupported operand type(s) for ^"), a, b)
    return -1


def _bitwise_unsupported(op: int) -> int:
    if op == 0:
        return _bitwise_error(3, cstr("unsupported operand type(s) for &"))
    if op == 1:
        return _bitwise_error(3, cstr("unsupported operand type(s) for |"))
    return _bitwise_error(3, cstr("unsupported operand type(s) for ^"))


def _bitwise_pin_result(slot: c_ptr) -> int:
    pcc_py_gc_minor_graph_lock()
    value = pcc_gc_load_ptr(null(), slot)
    prior: int = 0
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        prior = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & 64
        pcc_gc_pin(value)
    pcc_py_gc_minor_graph_unlock()
    return prior


def _py_obj_bitwise_dispatch(a, b, op: int):
    # Incoming values are borrowed from live caller owners. Register their
    # updateable slots before any new owner frame or input-copy operation.
    borrowed = stack_alloc(_BITWISE_INCOMING_COUNT * C_POINTER_SIZE)
    store_ptr(borrowed, 0, a)
    store_ptr(borrowed, C_POINTER_SIZE, b)
    pcc_gc_frame_enter(global_addr("pcc_bitwise_borrowed_map"), borrowed)
    slots = stack_alloc(_BITWISE_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_BITWISE_SLOT_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _BITWISE_SLOT_COUNT * C_POINTER_SIZE)
    index: int = 0
    while index < _BITWISE_SLOT_COUNT:
        store_i64(tokens, index * C_POINTER_SIZE, -1)
        index = index + 1
    pcc_gc_frame_enter(global_addr("pcc_bitwise_owned_map"), slots)
    status: int = 0
    index = 0
    while index < _BITWISE_INCOMING_COUNT and status == 0:
        token: int = pcc_gc_root_copy_borrowed_lease(ptr_add(slots, index * C_POINTER_SIZE), ptr_add(borrowed, index * C_POINTER_SIZE))
        store_i64(tokens, index * C_POINTER_SIZE, token)
        if token < 0:
            status = _bitwise_error(7, cstr("binary bitwise input owner copy failed"))
        index = index + 1
    if status == 0:
        status = _bitwise_body(slots, tokens, op)
    if py_err_occurred() != 0:
        status = -1
    error = ptr_add(slots, _BITWISE_ERROR * C_POINTER_SIZE)
    result = ptr_add(slots, _BITWISE_RESULT * C_POINTER_SIZE)
    py_tls_exc_swap_slot(error)
    memset(borrowed, 0, _BITWISE_INCOMING_COUNT * C_POINTER_SIZE)
    _bitwise_clear(slots, tokens, _BITWISE_ITEM)
    _bitwise_clear(slots, tokens, _BITWISE_RIGHT)
    _bitwise_clear(slots, tokens, _BITWISE_LEFT)
    if status != 0:
        _bitwise_clear(slots, tokens, _BITWISE_RESULT)
    py_clear_exception()
    py_tls_exc_swap_slot(error)
    prior: int = _bitwise_pin_result(result)
    token = load_i64(tokens, _BITWISE_RESULT * C_POINTER_SIZE)
    if token >= 0 and pcc_gc_foreign_lease_release(result, token) != 0:
        pcc_platform_abort()
        return null()
    pcc_gc_frame_leave(slots)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(result, prior)


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
        return py_raise_binary_type_error(cstr("unsupported operand type(s) for <<"), a, b)
    return py_raise_binary_type_error(cstr("unsupported operand type(s) for >>"), a, b)


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
    if _complex_numeric_pair(at, bt) != 0:
        return py_complex_div(a, b)
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
    if tag == PY_TYPE_ELLIPSIS:
        return cstr("ellipsis")
    if tag == PY_TYPE_FUNC:
        return cstr("function")
    if tag == -4:
        return cstr("builtin_function_or_method")
    if tag == -5:
        return cstr("method")
    if tag == PY_TYPE_NOTIMPLEMENTED:
        return cstr("NotImplementedType")
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
    if tag == PY_TYPE_STATICMETHOD:
        return cstr("staticmethod")
    if tag == PY_TYPE_CLASSMETHOD:
        return cstr("classmethod")
    if tag == PY_TYPE_PROPERTY:
        return cstr("property")
    if tag == PY_TYPE_CONTINUATION:
        return cstr("continuation")
    if tag == PY_TYPE_VIRTUAL_THREAD:
        return cstr("virtual_thread")
    if tag == PY_TYPE_VTHREAD_CHANNEL:
        return cstr("vthread_channel")
    return cstr("object")


def _obj_type_name_cstr(o):
    """``type(o).__name__`` as a C string owned by the class or a constant.

    It allocates nothing, so callers holding borrowed operands can use it.
    """
    if ptr_is_null(o) != 0:
        return cstr("NoneType")
    tag: int = _type_of(o)
    if _is_instance_tag(tag) != 0:
        cls = pcc_gc_load_ptr(o, ptr_add(o, PYINSTANCEOBJECT_CLS_OFFSET))
        if ptr_is_null(cls) == 0:
            cls_name = load_ptr(cls, PYCLASSOBJECT_NAME_OFFSET)
            if ptr_is_null(cls_name) == 0:
                return cls_name
    if tag == PY_TYPE_EXC:  # PY_TYPE_EXC
        cls = pcc_gc_load_ptr(o, ptr_add(o, 16))
        if ptr_is_null(cls) == 0:
            cls_name = load_ptr(cls, PYCLASSOBJECT_NAME_OFFSET)
            if ptr_is_null(cls_name) == 0:
                return cls_name
    if tag == PY_TYPE_FILE:
        kind: int = py_file_type_kind(o)
        if kind == 1:
            return cstr("BufferedReader")
        if kind == 2:
            return cstr("BufferedWriter")
        if kind == 3:
            return cstr("BufferedRandom")
        return cstr("TextIOWrapper")
    if tag == PY_TYPE_FUNC:
        return _type_name_cstr_for_tag(_func_type_tag(o))
    return _type_name_cstr_for_tag(tag)


def _func_type_tag(o) -> int:
    """PY_TYPE_FUNC, or the synthetic builtin (-4) / bound method (-5) tag."""
    kind: int = py_func_kind(o)
    if kind == 1:
        return -4
    if kind == 2:
        return -5
    return PY_TYPE_FUNC


@c_abi_export("py_obj_type_name")
def py_obj_type_name(o):
    name = _obj_type_name_cstr(o)
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
    if _is_instance_tag(tag) != 0 or tag == PY_TYPE_CLASS:
        # len(cls) calls the metaclass's __len__ (enum classes).
        return py_user_len_dispatch(o, null())
    return 0


@c_abi_export("py_builtin_len")
def py_builtin_len(o) -> int:
    """len(o): the length, or -1 with CPython's TypeError when o has none.

    py_obj_len answers 0 for an unsized object, which internal walkers rely
    on; the builtin raises "object of type 'int' has no len()".
    """
    if ptr_is_null(o) == 0:
        tag: int = _type_of(o)
        if (tag == PY_TYPE_LIST or tag == PY_TYPE_TUPLE or tag == PY_TYPE_STR
                or tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY or tag == PY_TYPE_MEMORYVIEW
                or tag == PY_TYPE_DICT or tag == PY_TYPE_SET):
            return py_obj_len(o)
        if pcc_capi_is_cext_type_tag(tag) != 0:
            cext_len: int = pcc_capi_cext_object_length(o)
            if cext_len >= 0 or py_err_occurred() != 0:
                return cext_len
        elif _is_instance_tag(tag) != 0 or tag == PY_TYPE_CLASS:
            handled = stack_alloc(8)
            store_i64(handled, 0, 0)
            length: int = py_user_len_dispatch(o, handled)
            if py_err_occurred() != 0:
                return -1
            if load_i64(handled, 0) != 0:
                return length
    message = stack_alloc(160)
    prefix = cstr("object of type '")
    size: int = 0
    while load_i8(prefix, size) != 0:
        store_i8(message, size, load_i8(prefix, size))
        size = size + 1
    shown = _obj_type_name_cstr(o)
    index: int = 0
    while load_i8(shown, index) != 0 and index < 100:
        store_i8(message, size, load_i8(shown, index))
        size = size + 1
        index = index + 1
    suffix = cstr("' has no len()")
    index = 0
    while load_i8(suffix, index) != 0:
        store_i8(message, size, load_i8(suffix, index))
        size = size + 1
        index = index + 1
    store_i8(message, size, 0)
    py_raise_owned(py_exc_new(3, message))
    return -1


def _ord_append_decimal(message, size: int, value: int) -> int:
    digits = stack_alloc(24)
    count: int = 0
    if value == 0:
        store_i8(digits, 0, 48)
        count = 1
    while value > 0:
        store_i8(digits, count, 48 + value % 10)
        value = value // 10
        count = count + 1
    while count > 0:
        count = count - 1
        store_i8(message, size, load_i8(digits, count))
        size = size + 1
    return size


def _ord_append_text(message, size: int, text) -> int:
    index: int = 0
    while load_i8(text, index) != 0 and index < 100:
        store_i8(message, size, load_i8(text, index))
        size = size + 1
        index = index + 1
    return size


@c_abi_export("py_builtin_ord")
def py_builtin_ord(o) -> int:
    """ord(o): the code point of a one-character str (or str subclass), or
    the value of a one-byte bytes/bytearray; -1 with CPython's TypeError."""
    message = stack_alloc(200)
    size: int = 0
    if ptr_is_null(o) == 0 and is_tagged_int(o) == 0:
        length: int = -1
        payload = py_str_payload(o)
        if ptr_is_null(payload) == 0:
            length = py_str_len(payload)
            if length == 1:
                return py_str_ord(payload)
        else:
            tag: int = _type_of(o)
            if tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY:
                length = py_bytes_len(o)
                if length == 1:
                    item = py_obj_getitem_i64(o, 0)
                    value: int = py_int_value_i64(item)
                    py_decref(item)
                    return value
        if length >= 0:
            size = _ord_append_text(message, size, cstr("ord() expected a character, but string of length "))
            size = _ord_append_decimal(message, size, length)
            size = _ord_append_text(message, size, cstr(" found"))
            store_i8(message, size, 0)
            py_raise_owned(py_exc_new(3, message))
            return -1
    size = _ord_append_text(message, size, cstr("ord() expected string of length 1, but "))
    size = _ord_append_text(message, size, _obj_type_name_cstr(o))
    size = _ord_append_text(message, size, cstr(" found"))
    store_i8(message, size, 0)
    py_raise_owned(py_exc_new(3, message))
    return -1


@c_abi_export("py_builtin_ord_at")
def py_builtin_ord_at(s, index: int) -> int:
    """ord(s[index]) for a str: IndexError when index is out of range."""
    code: int = py_str_ord_at_i64(s, index)
    if code < 0 and ptr_is_null(s) == 0:
        py_raise_owned(py_exc_new(5, cstr("string index out of range")))  # IndexError
    return code


def _key_is_slice(k) -> int:
    """Whether a subscript key is a slice object (``x[slice(1, 3)]``)."""
    if is_tagged_int(k) != 0 or _type_of(k) == PY_TYPE_INT:
        return 0
    return py_obj_is_slice(k)


def _slice_key_bounds(k, bounds) -> int:
    """A slice key's start, stop and step as NEW references in ``bounds``."""
    store_ptr(bounds, 0, py_instance_getattr(k, cstr("start")))
    store_ptr(bounds, 8, py_instance_getattr(k, cstr("stop")))
    store_ptr(bounds, 16, py_instance_getattr(k, cstr("step")))
    if (ptr_is_null(load_ptr(bounds, 0)) == 0 and ptr_is_null(load_ptr(bounds, 8)) == 0
            and ptr_is_null(load_ptr(bounds, 16)) == 0):
        return 0
    _slice_key_release(bounds)
    return -1


def _slice_key_release(bounds) -> None:
    index: int = 0
    while index < 3:
        bound = load_ptr(bounds, index * 8)
        if ptr_is_null(bound) == 0:
            py_decref(bound)
        index = index + 1


def _sequence_slice_key_getitem(o, k):
    # CPython's sequence __getitem__ takes a slice object as it takes a[lo:hi:step].
    bounds = stack_alloc(24)
    if _slice_key_bounds(k, bounds) != 0:
        return null()
    result = py_obj_slice(o, load_ptr(bounds, 0), load_ptr(bounds, 8), load_ptr(bounds, 16))
    _slice_key_release(bounds)
    return result


@c_abi_export("py_obj_getitem")
def py_obj_getitem(o, k):
    if ptr_is_null(o) != 0:
        return null()
    if ptr_is_null(k) != 0:
        return null()
    tag: int = _type_of(o)
    pcc_diagnostics_runtime_log_event_code(7, 1, tag, _type_of(k), o)
    if pcc_capi_is_cext_type_tag(tag) != 0:
        return pcc_capi_cext_object_getitem(o, k)
    if (tag == PY_TYPE_LIST or tag == PY_TYPE_TUPLE or tag == PY_TYPE_STR
            or tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY or tag == PY_TYPE_MEMORYVIEW):
        if _key_is_slice(k) != 0:
            return _sequence_slice_key_getitem(o, k)
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
    if _is_instance_tag(tag) != 0 or tag == PY_TYPE_CLASS:
        # cls[key] calls the metaclass's __getitem__ (enum classes).
        return py_user_getitem_dispatch(o, k)
    return null()


@c_abi_export("py_obj_getitem_i64")
def py_obj_getitem_i64(o, idx: int):
    if ptr_is_null(o) != 0:
        return null()
    tag: int = _type_of(o)
    pcc_diagnostics_runtime_log_event_code(7, 1, tag, 2, o)
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
    if _is_instance_tag(tag) != 0 or tag == PY_TYPE_CLASS:
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
    pcc_diagnostics_runtime_log_event_code(7, 3, tag, _type_of(k), o)
    if pcc_capi_is_cext_type_tag(tag) != 0:
        return pcc_capi_cext_object_setitem(o, k, v)
    if (tag == PY_TYPE_LIST or tag == PY_TYPE_BYTEARRAY) and _key_is_slice(k) != 0:
        bounds = stack_alloc(24)
        if _slice_key_bounds(k, bounds) != 0:
            return -1
        status: int = py_obj_set_slice(o, load_ptr(bounds, 0), load_ptr(bounds, 8), load_ptr(bounds, 16), v)
        _slice_key_release(bounds)
        return status
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
    pcc_diagnostics_runtime_log_event_code(7, 3, tag, 2, o)
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
    pcc_diagnostics_runtime_log_event_code(7, 4, tag, _type_of(k), o)
    if (tag == PY_TYPE_LIST or tag == PY_TYPE_BYTEARRAY) and _key_is_slice(k) != 0:
        bounds = stack_alloc(24)
        if _slice_key_bounds(k, bounds) != 0:
            return -1
        deleted: int = py_obj_del_slice(o, load_ptr(bounds, 0), load_ptr(bounds, 8), load_ptr(bounds, 16))
        _slice_key_release(bounds)
        return deleted
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


@c_abi_export("py_obj_assign_subscript")
def py_obj_assign_subscript(o, key, value) -> int:
    status: int = py_obj_setitem(o, key, value)
    if status < 0 and py_err_occurred() == 0:
        py_raise_owned(py_exc_new(3, cstr("object does not support item assignment")))
    return status


@c_abi_export("py_obj_delete_subscript")
def py_obj_delete_subscript(o, key) -> int:
    status: int = py_obj_delitem(o, key)
    if status < 0 and py_err_occurred() == 0:
        if _type_of(o) == PY_TYPE_DICT:
            py_raise_owned(py_exc_new_with_value(4, key))
        else:
            py_raise_owned(py_exc_new(3, cstr("object does not support item deletion")))
    return status


def _is_instance_tag(tag: int) -> int:
    if tag == PY_TYPE_INSTANCE:  # PY_TYPE_INSTANCE
        return 1
    if tag >= PY_TYPE_USER_CLASS_START:
        return 1
    return 0


def _method_type_call(args, nargs: int, nkwargs: int):
    """types.MethodType(function, instance): bind ``function`` to ``instance``."""
    if nkwargs != 0:
        py_raise_owned(py_exc_new(3, cstr("method() takes no keyword arguments")))
        return null()
    if nargs != 2:
        message = stack_alloc(64)
        prefix = cstr("method expected 2 arguments, got ")
        length: int = 0
        while load_i8(prefix, length) != 0:
            store_i8(message, length, load_i8(prefix, length))
            length = length + 1
        digits = stack_alloc(24)
        count: int = 0
        value: int = nargs
        if value == 0:
            store_i8(digits, 0, 48)
            count = 1
        while value > 0:
            store_i8(digits, count, 48 + value % 10)
            value = value // 10
            count = count + 1
        while count > 0:
            count = count - 1
            store_i8(message, length, load_i8(digits, count))
            length = length + 1
        store_i8(message, length, 0)
        py_raise_owned(py_exc_new(3, message))
        return null()
    # No allocation separates these reads from the bind call, which roots
    # its borrowed operands itself; the tuple keeps both alive meanwhile.
    function = py_tuple_get(args, 0)
    instance = py_tuple_get(args, 1)
    result = null()
    if ptr_eq(py_builtin_callable(function), global_load_ptr("py_True")) == 0:
        py_raise_owned(py_exc_new(3, cstr("first argument must be callable")))
    elif ptr_eq(instance, global_load_ptr("py_None")) != 0:
        py_raise_owned(py_exc_new(3, cstr("instance must not be None")))
    else:
        result = py_instance_bind_method(function, instance, cstr("method"))
    py_decref(instance)
    py_decref(function)
    return result


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
    if tag == PY_TYPE_ELLIPSIS:
        cls = global_load_ptr("pcc_type_cls_ellipsis")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("ellipsis"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_ellipsis", cls)
        return _return_builtin_type(cls)
    if tag == PY_TYPE_NOTIMPLEMENTED:
        cls = global_load_ptr("pcc_type_cls_notimplemented")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("NotImplementedType"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_notimplemented", cls)
        return _return_builtin_type(cls)
    if tag == PY_TYPE_FUNC:
        cls = global_load_ptr("pcc_type_cls_function")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("function"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_function", cls)
        return _return_builtin_type(cls)
    if tag == -4:
        cls = global_load_ptr("pcc_type_cls_builtin_function")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("builtin_function_or_method"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_builtin_function", cls)
        return _return_builtin_type(cls)
    if tag == -5:
        cls = global_load_ptr("pcc_type_cls_method")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("method"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_method", cls)
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
            # bool's base is int: bool.__mro__ == (bool, int, object), so
            # issubclass(bool, int) and int's class attributes reach bool.
            int_cls = _builtin_type_class_for_tag(PY_TYPE_INT)
            if ptr_is_null(int_cls) != 0:
                return null()
            bases = stack_alloc(8)
            store_ptr(bases, 0, int_cls)
            cls = py_class_new(cstr("bool"), bases, 1, null(), 0)
            py_decref(int_cls)
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
        return py_str_type_new()
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
        return py_tuple_type_new()
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
    if tag == PY_TYPE_STATICMETHOD:
        cls = global_load_ptr("pcc_type_cls_staticmethod")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("staticmethod"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_staticmethod", cls)
        return _return_builtin_type(cls)
    if tag == PY_TYPE_CLASSMETHOD:
        cls = global_load_ptr("pcc_type_cls_classmethod")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("classmethod"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_classmethod", cls)
        return _return_builtin_type(cls)
    if tag == PY_TYPE_PROPERTY:
        cls = global_load_ptr("pcc_type_cls_property")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("property"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_property", cls)
        return _return_builtin_type(cls)
    cls = global_load_ptr("pcc_type_cls_object")
    if ptr_is_null(cls) != 0:
        # The builtin object is the same immortal, stable root that terminates
        # every native class MRO. A fresh class named object splits identity.
        cls = py_subs_object_root()
        if ptr_is_null(cls) == 0:
            # Base object has no instance dictionary. Ordinary subclasses
            # receive independent flags from py_class_new and may have one.
            atomic_rmw_i32("or", cls, PYOBJECTHEADER_FLAGS_OFFSET, 2, "relaxed")
            global_store_ptr("pcc_type_cls_object", cls)
    return _return_builtin_type(cls)


def _file_type_class(o):
    """``type(f)`` for a file object: io's concrete class names."""
    kind: int = py_file_type_kind(o)
    cls = null()
    if kind == 0:
        cls = global_load_ptr("pcc_type_cls_textiowrapper")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("TextIOWrapper"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_textiowrapper", cls)
    elif kind == 1:
        cls = global_load_ptr("pcc_type_cls_bufferedreader")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("BufferedReader"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_bufferedreader", cls)
    elif kind == 2:
        cls = global_load_ptr("pcc_type_cls_bufferedwriter")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("BufferedWriter"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_bufferedwriter", cls)
    else:
        cls = global_load_ptr("pcc_type_cls_bufferedrandom")
        if ptr_is_null(cls) != 0:
            cls = py_class_new(cstr("BufferedRandom"), null(), 0, null(), 0)
            if ptr_is_null(cls) == 0:
                global_store_ptr("pcc_type_cls_bufferedrandom", cls)
    if ptr_is_null(cls) == 0 and ptr_is_null(_class_own_module(cls)) != 0:
        module = py_str_new(cstr("_io"), 3)
        if ptr_is_null(module) == 0:
            py_class_setattr_raw(cls, cstr("__module__"), module)
            py_decref(module)
    return _return_builtin_type(cls)


def _class_own_module(cls):
    """Borrowed-or-NULL probe: does ``cls`` already carry ``__module__``?"""
    attrs = py_class_attrs_dict(cls, 0)
    if ptr_is_null(attrs) != 0:
        return null()
    key = py_str_new(cstr("__module__"), 10)
    if ptr_is_null(key) != 0:
        return null()
    value = py_dict_get(attrs, key)
    py_decref(key)
    if ptr_is_null(value) == 0:
        py_decref(value)
    return value


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
    if tag == PY_TYPE_FILE:
        return _file_type_class(o)
    if tag == PY_TYPE_FUNC:
        return _builtin_type_class_for_tag(_func_type_tag(o))
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
    if ptr_eq(value, global_load_ptr("pcc_type_cls_ellipsis")) != 0:
        return PY_TYPE_ELLIPSIS
    if ptr_eq(value, global_load_ptr("pcc_type_cls_notimplemented")) != 0:
        return PY_TYPE_NOTIMPLEMENTED
    if ptr_eq(value, global_load_ptr("pcc_type_cls_function")) != 0:
        return PY_TYPE_FUNC
    if ptr_eq(value, global_load_ptr("pcc_type_cls_builtin_function")) != 0:
        return -4
    if ptr_eq(value, global_load_ptr("pcc_type_cls_method")) != 0:
        return -5
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
    if ptr_eq(value, global_load_ptr("pcc_type_cls_staticmethod")) != 0:
        return PY_TYPE_STATICMETHOD
    if ptr_eq(value, global_load_ptr("pcc_type_cls_classmethod")) != 0:
        return PY_TYPE_CLASSMETHOD
    if ptr_eq(value, global_load_ptr("pcc_type_cls_property")) != 0:
        return PY_TYPE_PROPERTY
    if ptr_eq(value, global_load_ptr("py_object_root_cache")) != 0:
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
            elif tag == PY_TYPE_EXC:
                # Every exception shares one tag; its class names the type.
                cls = pcc_gc_load_ptr(o, ptr_add(o, 16))
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


def _cstr_is_dunder_enter(s) -> int:
    # "__enter__" — 9 bytes, compared explicitly like the other dunder probes
    # in this module (no strcmp on the port tier).
    if strlen(s) != 9:
        return 0
    if load_i8(s, 0) != 95 or load_i8(s, 1) != 95:
        return 0
    if load_i8(s, 2) != 101 or load_i8(s, 3) != 110:
        return 0
    if load_i8(s, 4) != 116 or load_i8(s, 5) != 101:
        return 0
    if load_i8(s, 6) != 114:
        return 0
    if load_i8(s, 7) != 95 or load_i8(s, 8) != 95:
        return 0
    return 1


def _cstr_is_dunder_exit(s) -> int:
    # "__exit__" — 8 bytes.
    if strlen(s) != 8:
        return 0
    if load_i8(s, 0) != 95 or load_i8(s, 1) != 95:
        return 0
    if load_i8(s, 2) != 101 or load_i8(s, 3) != 120:
        return 0
    if load_i8(s, 4) != 105 or load_i8(s, 5) != 116:
        return 0
    if load_i8(s, 6) != 95 or load_i8(s, 7) != 95:
        return 0
    return 1


def _cstr_is_acquire(s) -> int:
    # "acquire" — 7 bytes.
    if strlen(s) != 7:
        return 0
    if load_i8(s, 0) != 97 or load_i8(s, 1) != 99:
        return 0
    if load_i8(s, 2) != 113 or load_i8(s, 3) != 117:
        return 0
    if load_i8(s, 4) != 105 or load_i8(s, 5) != 114:
        return 0
    if load_i8(s, 6) != 101:
        return 0
    return 1


def _cstr_is_release(s) -> int:
    # "release" — 7 bytes.
    if strlen(s) != 7:
        return 0
    if load_i8(s, 0) != 114 or load_i8(s, 1) != 101:
        return 0
    if load_i8(s, 2) != 108 or load_i8(s, 3) != 101:
        return 0
    if load_i8(s, 4) != 97 or load_i8(s, 5) != 115:
        return 0
    if load_i8(s, 6) != 101:
        return 0
    return 1


def _py_lock_acquire_entry(captures, args):
    # acquire(blocking=True, timeout=-1): the signature binder supplies both.
    lock = py_tuple_get(captures, 0)
    if ptr_is_null(lock) != 0:
        return null()
    blocking = py_tuple_get(args, 0)
    timeout = py_tuple_get(args, 1)
    rc: int = py_threading_lock_acquire_args(lock, blocking, timeout)
    py_decref(timeout)
    py_decref(blocking)
    py_decref(lock)
    return _py_sync_rc_bool(rc)


def _py_lock_release_rc(lock) -> int:
    if load_i32(lock, 8) == PY_TYPE_THREAD_RLOCK:
        return py_threading_rlock_release(lock)
    return py_threading_lock_release(lock)


def _py_lock_release_entry(captures, args):
    lock = py_tuple_get(captures, 0)
    if ptr_is_null(lock) != 0:
        return null()
    rc: int = _py_lock_release_rc(lock)
    py_decref(lock)
    if rc != 0:
        return _py_sync_rc_bool(-1)
    return global_load_ptr("py_None")


def _py_lock_locked_entry(captures, args):
    lock = py_tuple_get(captures, 0)
    if ptr_is_null(lock) != 0:
        return null()
    held: int = py_threading_lock_locked(lock)
    py_decref(lock)
    if held < 0:
        py_raise_owned(py_exc_new(7, cstr("lock state is unavailable")))
        return null()
    if held != 0:
        return global_load_ptr("py_True")
    return global_load_ptr("py_False")


def _py_lock_method_bound(o, which: int):
    # `lock.acquire()` / `lock.release()` / `lock.locked()` on a dynamically
    # typed lock, the sibling of the `__enter__`/`__exit__` case below:
    # `freeze()` walks `self._locks` and calls `acquire()` on each element,
    # which the static lowering cannot type.
    # Every allocation below may run a moving collection: the receiver, the
    # captures and the method stay pinned until the method owns them, the
    # protocol of _py_sync_method_bound. An unpinned fresh captures tuple
    # moved under py_func_new_bound left the method holding its old address.
    prior: int = _py_sync_pin(o)
    captures = py_tuple_new(1)
    if ptr_is_null(captures) != 0:
        _py_sync_unpin(o, prior)
        return null()
    pcc_gc_pin(captures)
    py_tuple_set_item(captures, 0, o)
    if which == 0:
        wrapped = _py_sync_signature_captures(
            captures, 2, cstr("blocking"), 8, global_load_ptr("py_True"),
            cstr("timeout"), 7, py_int_from_i64(-1),
        )
        pcc_gc_pin(wrapped)
        pcc_gc_unpin(captures)
        py_decref(captures)
        captures = wrapped
        if ptr_is_null(captures):
            _py_sync_unpin(o, prior)
            return null()
    if which == 2:
        fn = py_func_new_bound(_py_lock_locked_entry, captures, cstr("locked"), o)
    elif which != 0:
        fn = py_func_new_bound(_py_lock_release_entry, captures, cstr("release"), o)
    else:
        fn = py_func_new_bound(_py_lock_acquire_entry, captures, cstr("acquire"), o)
    pcc_gc_pin(fn)
    pcc_gc_unpin(captures)
    py_decref(captures)
    _py_sync_unpin(o, prior)
    pcc_gc_unpin(fn)
    return fn


def _py_lock_enter_entry(captures, args):
    lock = py_tuple_get(captures, 0)
    if ptr_is_null(lock) != 0:
        return null()
    rc: int = 0
    if load_i32(lock, 8) == PY_TYPE_THREAD_RLOCK:
        rc = py_threading_rlock_acquire(lock)
    else:
        rc = py_threading_lock_acquire(lock)
    if rc != 0:
        py_decref(lock)
        return _py_sync_rc_bool(-1)
    return lock


def _py_lock_exit_entry(captures, args):
    lock = py_tuple_get(captures, 0)
    if ptr_is_null(lock) != 0:
        return null()
    rc: int = _py_lock_release_rc(lock)
    py_decref(lock)
    if rc != 0:
        return _py_sync_rc_bool(-1)
    return global_load_ptr("py_None")


# ``threading`` Semaphore / Event / Condition methods on a dynamically typed
# receiver.  The static lowering (native_threading) covers a receiver whose
# type is proven; one that arrives as a plain parameter reached this getattr
# and raised AttributeError.  Results follow that lowering: return code 0 is
# True, a positive code (a wait that did not complete) is False, and a
# negative code is a failed primitive.

_SYNC_ACQUIRE = 0
_SYNC_RELEASE = 1
_SYNC_ENTER = 2
_SYNC_EXIT = 3
_SYNC_SET = 4
_SYNC_CLEAR = 5
_SYNC_IS_SET = 6
_SYNC_WAIT = 7
_SYNC_NOTIFY = 8
_SYNC_NOTIFY_ALL = 9


def _py_sync_rc_bool(rc: int):
    if rc < 0:
        if py_err_occurred() == 0:
            py_raise_owned(py_exc_new(7, cstr("threading primitive failed")))
        return null()
    if rc == 0:
        return global_load_ptr("py_True")
    return global_load_ptr("py_False")


def _py_sync_invoke(o, op: int):
    tag: int = load_i32(o, 8)
    none = global_load_ptr("py_None")
    if tag == PY_TYPE_THREAD_SEMAPHORE:
        if op == _SYNC_ACQUIRE:
            return _py_sync_rc_bool(py_threading_semaphore_acquire(o))
        if op == _SYNC_ENTER:
            if ptr_is_null(_py_sync_rc_bool(py_threading_semaphore_acquire(o))):
                return null()
            return o
        if py_threading_semaphore_release(o) != 0:
            return _py_sync_rc_bool(-1)
        return none
    if tag == PY_TYPE_THREAD_EVENT:
        if op == _SYNC_SET:
            py_threading_event_set(o)
            return none
        if op == _SYNC_CLEAR:
            py_threading_event_clear(o)
            return none
        if op == _SYNC_IS_SET:
            if py_threading_event_is_set(o) != 0:
                return global_load_ptr("py_True")
            return global_load_ptr("py_False")
        return _py_sync_rc_bool(py_threading_event_wait(o))
    if op == _SYNC_ACQUIRE:
        return _py_sync_rc_bool(py_threading_condition_acquire(o))
    if op == _SYNC_ENTER:
        if ptr_is_null(_py_sync_rc_bool(py_threading_condition_acquire(o))):
            return null()
        return o
    if op == _SYNC_WAIT:
        return _py_sync_rc_bool(py_threading_condition_wait(o))
    if op == _SYNC_NOTIFY:
        if ptr_is_null(_py_sync_rc_bool(py_threading_condition_notify(o))):
            return null()
        return none
    if op == _SYNC_NOTIFY_ALL:
        if ptr_is_null(_py_sync_rc_bool(py_threading_condition_notify_all(o))):
            return null()
        return none
    py_threading_condition_release(o)
    return none


def _py_sync_captured(captures):
    return py_tuple_get(captures, 0)


def _py_sync_pin(o) -> int:
    prior: int = 64
    if ptr_is_null(o) == 0 and is_tagged_int(o) == 0:
        prior = load_i32(o, 12) & 64
        if prior == 0:
            pcc_gc_pin(o)
    return prior


def _py_sync_unpin(o, prior: int) -> None:
    if prior == 0:
        pcc_gc_unpin(o)


def _py_sync_call_captured(captures, op: int):
    o = _py_sync_captured(captures)
    if ptr_is_null(o):
        return null()
    prior: int = _py_sync_pin(o)
    result = _py_sync_invoke(o, op)
    _py_sync_unpin(o, prior)
    # tuple_get owns its result; __enter__ transfers that reference, other
    # methods consume it. Do not leak a receiver for every dynamic call.
    if ptr_eq(result, o) == 0:
        py_decref(o)
    return result


def _py_sync_acquire_entry(captures, args):
    return _py_sync_call_captured(captures, _SYNC_ACQUIRE)


def _py_sync_release_entry(captures, args):
    return _py_sync_call_captured(captures, _SYNC_RELEASE)


def _py_sync_enter_entry(captures, args):
    return _py_sync_call_captured(captures, _SYNC_ENTER)


def _py_sync_exit_entry(captures, args):
    return _py_sync_call_captured(captures, _SYNC_EXIT)


def _py_sync_set_entry(captures, args):
    return _py_sync_call_captured(captures, _SYNC_SET)


def _py_sync_clear_entry(captures, args):
    return _py_sync_call_captured(captures, _SYNC_CLEAR)


def _py_sync_is_set_entry(captures, args):
    return _py_sync_call_captured(captures, _SYNC_IS_SET)


def _py_semaphore_acquire_entry(captures, args):
    # acquire(blocking=True, timeout=None): the signature binder supplies both.
    o = _py_sync_captured(captures)
    if ptr_is_null(o):
        return null()
    blocking = py_tuple_get(args, 0)
    timeout = py_tuple_get(args, 1)
    rc: int = py_threading_semaphore_acquire_args(o, blocking, timeout)
    py_decref(timeout)
    py_decref(blocking)
    py_decref(o)
    return _py_sync_rc_bool(rc)


def _py_semaphore_release_entry(captures, args):
    # release(n=1)
    o = _py_sync_captured(captures)
    if ptr_is_null(o):
        return null()
    count = py_tuple_get(args, 0)
    rc: int = py_threading_semaphore_release_args(o, count)
    py_decref(count)
    py_decref(o)
    if rc != 0:
        return _py_sync_rc_bool(-1)
    return global_load_ptr("py_None")


def _py_sync_wait_entry(captures, args):
    o = _py_sync_captured(captures)
    if ptr_is_null(o):
        return null()
    prior: int = _py_sync_pin(o)
    timeout = py_tuple_get(args, 0)
    timeout_prior: int = _py_sync_pin(timeout)
    if load_i32(o, 8) == PY_TYPE_THREAD_CONDITION:
        result = _py_sync_rc_bool(py_threading_condition_wait_timeout(o, timeout))
    else:
        result = _py_sync_rc_bool(py_threading_event_wait_args(o, timeout))
    _py_sync_unpin(timeout, timeout_prior)
    py_decref(timeout)
    _py_sync_unpin(o, prior)
    py_decref(o)
    return result


def _py_sync_notify_entry(captures, args):
    return _py_sync_call_captured(captures, _SYNC_NOTIFY)


def _py_sync_notify_all_entry(captures, args):
    return _py_sync_call_captured(captures, _SYNC_NOTIFY_ALL)


def _py_sync_signature_parameter(names, kinds, has_defaults, defaults, index: int, name, name_len: int, default) -> int:
    formal = py_str_new(name, name_len)
    if ptr_is_null(formal):
        return 0
    pcc_gc_pin(formal)
    py_tuple_set_item(names, index, formal)
    py_tuple_set_item(kinds, index, py_int_from_i64(0))
    py_tuple_set_item(has_defaults, index, global_load_ptr("py_True"))
    py_tuple_set_item(defaults, index, default)
    pcc_gc_unpin(formal)
    py_decref(formal)
    return 1


def _py_sync_signature_captures(captures, count: int, first, first_len: int, first_default, second, second_len: int, second_default):
    """``(captures, signature)`` for a bound sync method with ``count`` (1 or
    2) optional positional-or-keyword parameters.

    The ordinary native function signature binder then accepts keywords and
    passes the entry every parameter, defaults filled in; there is no
    threading-only kwargs transport. NULL on allocation failure.
    """
    names = py_tuple_new(count)
    pcc_gc_pin(names)
    kinds = py_tuple_new(count)
    pcc_gc_pin(kinds)
    has_defaults = py_tuple_new(count)
    pcc_gc_pin(has_defaults)
    defaults = py_tuple_new(count)
    pcc_gc_pin(defaults)
    signature = null()
    wrapper = null()
    if ptr_is_null(names) == 0 and ptr_is_null(kinds) == 0 and ptr_is_null(has_defaults) == 0 and ptr_is_null(defaults) == 0:
        complete: int = _py_sync_signature_parameter(
            names, kinds, has_defaults, defaults, 0, first, first_len, first_default
        )
        if complete != 0 and count > 1:
            complete = _py_sync_signature_parameter(
                names, kinds, has_defaults, defaults, 1, second, second_len, second_default
            )
        if complete != 0:
            signature = py_tuple_new(5)
            pcc_gc_pin(signature)
            if ptr_is_null(signature) == 0:
                magic = py_str_new(cstr("__pcc_func_signature_v1__"), 25)
                pcc_gc_pin(magic)
                if ptr_is_null(magic) == 0:
                    py_tuple_set_item(signature, 0, magic)
                    py_tuple_set_item(signature, 1, names)
                    py_tuple_set_item(signature, 2, kinds)
                    py_tuple_set_item(signature, 3, has_defaults)
                    py_tuple_set_item(signature, 4, defaults)
                    wrapper = py_tuple_new(2)
                    pcc_gc_pin(wrapper)
                    if ptr_is_null(wrapper) == 0:
                        py_tuple_set_item(wrapper, 0, captures)
                        py_tuple_set_item(wrapper, 1, signature)
                pcc_gc_unpin(magic)
                py_decref(magic)
    pcc_gc_unpin(signature)
    py_decref(signature)
    pcc_gc_unpin(defaults)
    py_decref(defaults)
    pcc_gc_unpin(has_defaults)
    py_decref(has_defaults)
    pcc_gc_unpin(kinds)
    py_decref(kinds)
    pcc_gc_unpin(names)
    py_decref(names)
    pcc_gc_unpin(wrapper)
    return wrapper


def _py_condition_wait_captures(captures):
    # wait(timeout=None) / Event.wait(timeout=None): one bound argument.
    return _py_sync_signature_captures(
        captures, 1, cstr("timeout"), 7, global_load_ptr("py_None"), null(), 0, null()
    )


def _py_sync_method_op(tag: int, name) -> int:
    if tag != PY_TYPE_THREAD_EVENT:
        if _cstr_is_dunder_enter(name) != 0:
            return _SYNC_ENTER
        if _cstr_is_dunder_exit(name) != 0:
            return _SYNC_EXIT
        if _cstr_is_acquire(name) != 0:
            return _SYNC_ACQUIRE
        if _cstr_is_release(name) != 0:
            return _SYNC_RELEASE
    if tag == PY_TYPE_THREAD_EVENT:
        if strcmp(name, cstr("set")) == 0:
            return _SYNC_SET
        if strcmp(name, cstr("clear")) == 0:
            return _SYNC_CLEAR
        if strcmp(name, cstr("is_set")) == 0:
            return _SYNC_IS_SET
    if tag != PY_TYPE_THREAD_SEMAPHORE and strcmp(name, cstr("wait")) == 0:
        return _SYNC_WAIT
    if tag == PY_TYPE_THREAD_CONDITION:
        # Same primitive as the static lowering's notify/notify_all.
        if strcmp(name, cstr("notify")) == 0:
            return _SYNC_NOTIFY
        if strcmp(name, cstr("notify_all")) == 0:
            return _SYNC_NOTIFY_ALL
    return -1


def _py_sync_method_bound(o, tag: int, name):
    """The bound method for ``name`` on a sync primitive, NULL if none."""
    op: int = _py_sync_method_op(tag, name)
    if op < 0:
        return null()
    prior: int = _py_sync_pin(o)
    captures = py_tuple_new(1)
    if ptr_is_null(captures) != 0:
        _py_sync_unpin(o, prior)
        return null()
    pcc_gc_pin(captures)
    py_tuple_set_item(captures, 0, o)
    # Methods with arguments bind them through a native signature, as
    # CPython's positional-or-keyword parameters with their defaults.
    semaphore_call: int = 0
    if tag == PY_TYPE_THREAD_SEMAPHORE and (op == _SYNC_ACQUIRE or op == _SYNC_RELEASE):
        semaphore_call = 1
    wrapped = null()
    wrap: int = 0
    if (tag == PY_TYPE_THREAD_CONDITION or tag == PY_TYPE_THREAD_EVENT) and op == _SYNC_WAIT:
        wrapped = _py_condition_wait_captures(captures)
        wrap = 1
    elif semaphore_call != 0 and op == _SYNC_ACQUIRE:
        wrapped = _py_sync_signature_captures(
            captures, 2, cstr("blocking"), 8, global_load_ptr("py_True"),
            cstr("timeout"), 7, global_load_ptr("py_None"),
        )
        wrap = 1
    elif semaphore_call != 0:
        wrapped = _py_sync_signature_captures(
            captures, 1, cstr("n"), 1, py_int_from_i64(1), null(), 0, null()
        )
        wrap = 1
    if wrap != 0:
        pcc_gc_pin(wrapped)
        pcc_gc_unpin(captures)
        py_decref(captures)
        captures = wrapped
        if ptr_is_null(captures):
            _py_sync_unpin(o, prior)
            return null()
    # Each entry is named directly in the extern call: that is what lowers a
    # function to its code address (as a pcc argument it becomes an object).
    if op == _SYNC_ENTER:
        fn = py_func_new_bound(_py_sync_enter_entry, captures, cstr("__enter__"), o)
    elif op == _SYNC_EXIT:
        fn = py_func_new_bound(_py_sync_exit_entry, captures, cstr("__exit__"), o)
    elif op == _SYNC_ACQUIRE and semaphore_call != 0:
        fn = py_func_new_bound(_py_semaphore_acquire_entry, captures, cstr("acquire"), o)
    elif op == _SYNC_ACQUIRE:
        fn = py_func_new_bound(_py_sync_acquire_entry, captures, cstr("acquire"), o)
    elif op == _SYNC_RELEASE and semaphore_call != 0:
        fn = py_func_new_bound(_py_semaphore_release_entry, captures, cstr("release"), o)
    elif op == _SYNC_RELEASE:
        fn = py_func_new_bound(_py_sync_release_entry, captures, cstr("release"), o)
    elif op == _SYNC_SET:
        fn = py_func_new_bound(_py_sync_set_entry, captures, cstr("set"), o)
    elif op == _SYNC_CLEAR:
        fn = py_func_new_bound(_py_sync_clear_entry, captures, cstr("clear"), o)
    elif op == _SYNC_IS_SET:
        fn = py_func_new_bound(_py_sync_is_set_entry, captures, cstr("is_set"), o)
    elif op == _SYNC_WAIT:
        fn = py_func_new_bound(_py_sync_wait_entry, captures, cstr("wait"), o)
    elif op == _SYNC_NOTIFY_ALL:
        fn = py_func_new_bound(_py_sync_notify_all_entry, captures, cstr("notify_all"), o)
    else:
        fn = py_func_new_bound(_py_sync_notify_entry, captures, cstr("notify"), o)
    pcc_gc_pin(fn)
    pcc_gc_unpin(captures)
    py_decref(captures)
    _py_sync_unpin(o, prior)
    pcc_gc_unpin(fn)
    return fn


def _py_lock_context_bound(o, want_exit: int):
    # `with lock:` on a *dynamically typed* lock.  The static lowering in
    # `native_threading` already emits acquire/release when the receiver's
    # type is known, but a lock read out of an untyped container
    # (`self._locks[shard]`) reaches the generic attribute path -- which had
    # no `__enter__`, so a self-hosted parallel link died with
    # "'object' object has no attribute '__enter__'".
    # Pinned across the allocations exactly as in _py_lock_method_bound.
    prior: int = _py_sync_pin(o)
    captures = py_tuple_new(1)
    if ptr_is_null(captures) != 0:
        _py_sync_unpin(o, prior)
        return null()
    pcc_gc_pin(captures)
    py_tuple_set_item(captures, 0, o)
    if want_exit != 0:
        fn = py_func_new_bound(_py_lock_exit_entry, captures, cstr("__exit__"), o)
    else:
        fn = py_func_new_bound(_py_lock_enter_entry, captures, cstr("__enter__"), o)
    pcc_gc_pin(fn)
    pcc_gc_unpin(captures)
    py_decref(captures)
    _py_sync_unpin(o, prior)
    pcc_gc_unpin(fn)
    return fn


# Both stored bound-method construction and invocation own their scratch values
# through the same slot/lease contract used by ordinary call boundaries.
_BOUND_LIST_INPUT0 = 0
_BOUND_LIST_INPUT1 = 1
_BOUND_LIST_VALUE = 2
_BOUND_LIST_ITEM = 3
_BOUND_LIST_RESULT = 4
_BOUND_LIST_ERROR = 5
_BOUND_LIST_SLOT_COUNT = 6
_BOUND_LIST_INPUT_COUNT = 2

define_global_i32("pcc_bound_list_borrowed_map", -2)
define_global_i32("pcc_bound_list_owned_map", 6)


def _bound_list_adopt(slots, tokens, index: int) -> int:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    token: int = pcc_gc_foreign_lease_acquire(slot)
    store_i64(tokens, index * C_POINTER_SIZE, token)
    if token < 0:
        if py_err_occurred() == 0:
            py_raise_owned(py_exc_new(7, cstr("bound list method owner lease failed")))
        return -1
    pcc_py_gc_minor_graph_lock()
    pcc_gc_note_slot_write_barrier(null(), slot, load_ptr(slot, 0))
    pcc_py_gc_minor_graph_unlock()
    if ptr_is_null(load_ptr(slot, 0)) != 0:
        if py_err_occurred() == 0:
            py_raise_owned(py_exc_new(19, cstr("bound list method allocation failed")))
        return -1
    return -1 if py_err_occurred() != 0 else 0


def _bound_list_begin(borrowed, slots, tokens) -> int:
    pcc_gc_frame_enter(global_addr("pcc_bound_list_borrowed_map"), borrowed)
    memset(slots, 0, _BOUND_LIST_SLOT_COUNT * C_POINTER_SIZE)
    index: int = 0
    while index < _BOUND_LIST_SLOT_COUNT:
        store_i64(tokens, index * C_POINTER_SIZE, -1)
        index = index + 1
    pcc_gc_frame_enter(global_addr("pcc_bound_list_owned_map"), slots)
    index = 0
    while index < _BOUND_LIST_INPUT_COUNT:
        token: int = pcc_gc_root_copy_borrowed_lease(
            ptr_add(slots, index * C_POINTER_SIZE),
            ptr_add(borrowed, index * C_POINTER_SIZE),
        )
        store_i64(tokens, index * C_POINTER_SIZE, token)
        if token < 0:
            if py_err_occurred() == 0:
                py_raise_owned(py_exc_new(7, cstr("bound list method input owner failed")))
            return -1
        index = index + 1
    return 0


def _bound_list_clear(slots, tokens, index: int) -> None:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    token: int = load_i64(tokens, index * C_POINTER_SIZE)
    if token >= 0:
        if pcc_gc_foreign_lease_release(slot, token) != 0:
            pcc_platform_abort()
            return
    store_i64(tokens, index * C_POINTER_SIZE, -1)
    pcc_gc_store_root(slot, null())


def _bound_list_finish(borrowed, slots, tokens, status: int):
    if py_err_occurred() != 0:
        status = -1
    error = ptr_add(slots, _BOUND_LIST_ERROR * C_POINTER_SIZE)
    result = ptr_add(slots, _BOUND_LIST_RESULT * C_POINTER_SIZE)
    py_tls_exc_swap_slot(error)
    memset(borrowed, 0, _BOUND_LIST_INPUT_COUNT * C_POINTER_SIZE)
    _bound_list_clear(slots, tokens, _BOUND_LIST_ITEM)
    _bound_list_clear(slots, tokens, _BOUND_LIST_VALUE)
    _bound_list_clear(slots, tokens, _BOUND_LIST_INPUT1)
    _bound_list_clear(slots, tokens, _BOUND_LIST_INPUT0)
    if status != 0:
        _bound_list_clear(slots, tokens, _BOUND_LIST_RESULT)
    py_clear_exception()
    py_tls_exc_swap_slot(error)
    pcc_py_gc_minor_graph_lock()
    value = pcc_gc_load_ptr(null(), result)
    prior: int = 0
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        prior = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & 64
        pcc_gc_pin(value)
    pcc_py_gc_minor_graph_unlock()
    token: int = load_i64(tokens, _BOUND_LIST_RESULT * C_POINTER_SIZE)
    if token >= 0 and pcc_gc_foreign_lease_release(result, token) != 0:
        pcc_platform_abort()
        return null()
    pcc_gc_frame_leave(slots)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(result, prior)


def _py_list_append_bound_entry(captures, args):
    borrowed = stack_alloc(_BOUND_LIST_INPUT_COUNT * C_POINTER_SIZE)
    store_ptr(borrowed, _BOUND_LIST_INPUT0 * C_POINTER_SIZE, captures)
    store_ptr(borrowed, _BOUND_LIST_INPUT1 * C_POINTER_SIZE, args)
    slots = stack_alloc(_BOUND_LIST_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_BOUND_LIST_SLOT_COUNT * C_POINTER_SIZE)
    status: int = _bound_list_begin(borrowed, slots, tokens)
    if status == 0:
        nargs: int = py_tuple_len(load_ptr(slots, _BOUND_LIST_INPUT1 * C_POINTER_SIZE))
        if nargs != 1:
            py_raise_owned(py_exc_new(3, cstr("list.append expected exactly 1 argument")))
            status = -1
    if status == 0:
        store_ptr(slots, _BOUND_LIST_VALUE * C_POINTER_SIZE,
                  py_tuple_get(load_ptr(slots, _BOUND_LIST_INPUT0 * C_POINTER_SIZE), 0))
        status = _bound_list_adopt(slots, tokens, _BOUND_LIST_VALUE)
    if status == 0:
        store_ptr(slots, _BOUND_LIST_ITEM * C_POINTER_SIZE,
                  py_tuple_get(load_ptr(slots, _BOUND_LIST_INPUT1 * C_POINTER_SIZE), 0))
        status = _bound_list_adopt(slots, tokens, _BOUND_LIST_ITEM)
    if status == 0:
        py_list_append(load_ptr(slots, _BOUND_LIST_VALUE * C_POINTER_SIZE),
                       load_ptr(slots, _BOUND_LIST_ITEM * C_POINTER_SIZE))
        if py_err_occurred() != 0:
            status = -1
        else:
            value = global_load_ptr("py_None")
            py_incref(value)
            store_ptr(slots, _BOUND_LIST_RESULT * C_POINTER_SIZE, value)
            status = _bound_list_adopt(slots, tokens, _BOUND_LIST_RESULT)
    return _bound_list_finish(borrowed, slots, tokens, status)


def _py_list_append_bound(o):
    borrowed = stack_alloc(_BOUND_LIST_INPUT_COUNT * C_POINTER_SIZE)
    store_ptr(borrowed, _BOUND_LIST_INPUT0 * C_POINTER_SIZE, o)
    store_ptr(borrowed, _BOUND_LIST_INPUT1 * C_POINTER_SIZE, null())
    slots = stack_alloc(_BOUND_LIST_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_BOUND_LIST_SLOT_COUNT * C_POINTER_SIZE)
    status: int = _bound_list_begin(borrowed, slots, tokens)
    if status == 0:
        store_ptr(slots, _BOUND_LIST_VALUE * C_POINTER_SIZE, py_tuple_new(1))
        status = _bound_list_adopt(slots, tokens, _BOUND_LIST_VALUE)
    if status == 0:
        py_tuple_set_item(load_ptr(slots, _BOUND_LIST_VALUE * C_POINTER_SIZE), 0,
                          load_ptr(slots, _BOUND_LIST_INPUT0 * C_POINTER_SIZE))
        if py_err_occurred() != 0:
            status = -1
    if status == 0:
        store_ptr(slots, _BOUND_LIST_RESULT * C_POINTER_SIZE, py_func_new_bound(
            _py_list_append_bound_entry,
            load_ptr(slots, _BOUND_LIST_VALUE * C_POINTER_SIZE), cstr("append"),
            load_ptr(slots, _BOUND_LIST_INPUT0 * C_POINTER_SIZE),
        ))
        status = _bound_list_adopt(slots, tokens, _BOUND_LIST_RESULT)
    return _bound_list_finish(borrowed, slots, tokens, status)


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
    # Pinned across both allocations as in _py_lock_method_bound.
    prior: int = _py_sync_pin(o)
    captures = py_tuple_new(1)
    if ptr_is_null(captures) != 0:
        _py_sync_unpin(o, prior)
        return null()
    pcc_gc_pin(captures)
    py_tuple_set_item(captures, 0, o)
    fn = py_func_new_bound(_py_list_pop_bound_entry, captures, cstr("pop"), o)
    pcc_gc_pin(fn)
    pcc_gc_unpin(captures)
    py_decref(captures)
    _py_sync_unpin(o, prior)
    pcc_gc_unpin(fn)
    return fn


def _py_dict_pop_bound(o):
    # Pinned across both allocations as in _py_lock_method_bound.
    prior: int = _py_sync_pin(o)
    captures = py_tuple_new(1)
    if ptr_is_null(captures) != 0:
        _py_sync_unpin(o, prior)
        return null()
    pcc_gc_pin(captures)
    py_tuple_set_item(captures, 0, o)
    fn = py_func_new_bound(_py_dict_pop_bound_entry, captures, cstr("pop"), o)
    pcc_gc_pin(fn)
    pcc_gc_unpin(captures)
    py_decref(captures)
    _py_sync_unpin(o, prior)
    pcc_gc_unpin(fn)
    return fn


# Every other dict and list method binds here when the compiler could not
# type the receiver (d.get, d.items, l.extend, getattr(d, "keys"), ...), as
# CPython's method descriptors do. A call roots its receiver and arguments
# in one frame and runs the runtime entry the typed lowering uses.
_BOUND_METHOD_INPUT0 = 0
_BOUND_METHOD_INPUT1 = 1
_BOUND_METHOD_RECEIVER = 2
_BOUND_METHOD_ARG0 = 3
_BOUND_METHOD_ARG1 = 4
_BOUND_METHOD_ARG2 = 5
_BOUND_METHOD_RESULT = 6
_BOUND_METHOD_ERROR = 7
_BOUND_METHOD_SLOT_COUNT = 8
_BOUND_METHOD_INPUT_COUNT = 2

define_global_i32("pcc_bound_method_borrowed_map", -2)
define_global_i32("pcc_bound_method_owned_map", 8)


def _bound_method_adopt(slots, tokens, index: int) -> int:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    token: int = pcc_gc_foreign_lease_acquire(slot)
    store_i64(tokens, index * C_POINTER_SIZE, token)
    if token < 0:
        if py_err_occurred() == 0:
            py_raise_owned(py_exc_new(7, cstr("bound method owner lease failed")))
        return -1
    pcc_py_gc_minor_graph_lock()
    pcc_gc_note_slot_write_barrier(null(), slot, load_ptr(slot, 0))
    pcc_py_gc_minor_graph_unlock()
    if ptr_is_null(load_ptr(slot, 0)) != 0:
        if py_err_occurred() == 0:
            py_raise_owned(py_exc_new(19, cstr("bound method allocation failed")))
        return -1
    return -1 if py_err_occurred() != 0 else 0


def _bound_method_begin(borrowed, slots, tokens) -> int:
    pcc_gc_frame_enter(global_addr("pcc_bound_method_borrowed_map"), borrowed)
    memset(slots, 0, _BOUND_METHOD_SLOT_COUNT * C_POINTER_SIZE)
    index: int = 0
    while index < _BOUND_METHOD_SLOT_COUNT:
        store_i64(tokens, index * C_POINTER_SIZE, -1)
        index = index + 1
    pcc_gc_frame_enter(global_addr("pcc_bound_method_owned_map"), slots)
    index = 0
    while index < _BOUND_METHOD_INPUT_COUNT:
        token: int = pcc_gc_root_copy_borrowed_lease(
            ptr_add(slots, index * C_POINTER_SIZE),
            ptr_add(borrowed, index * C_POINTER_SIZE),
        )
        store_i64(tokens, index * C_POINTER_SIZE, token)
        if token < 0:
            if py_err_occurred() == 0:
                py_raise_owned(py_exc_new(7, cstr("bound method input owner failed")))
            return -1
        index = index + 1
    return 0


def _bound_method_clear(slots, tokens, index: int) -> None:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    token: int = load_i64(tokens, index * C_POINTER_SIZE)
    if token >= 0:
        if pcc_gc_foreign_lease_release(slot, token) != 0:
            pcc_platform_abort()
            return
    store_i64(tokens, index * C_POINTER_SIZE, -1)
    pcc_gc_store_root(slot, null())


def _bound_method_finish(borrowed, slots, tokens, status: int):
    if py_err_occurred() != 0:
        status = -1
    error = ptr_add(slots, _BOUND_METHOD_ERROR * C_POINTER_SIZE)
    result = ptr_add(slots, _BOUND_METHOD_RESULT * C_POINTER_SIZE)
    py_tls_exc_swap_slot(error)
    memset(borrowed, 0, _BOUND_METHOD_INPUT_COUNT * C_POINTER_SIZE)
    _bound_method_clear(slots, tokens, _BOUND_METHOD_ARG2)
    _bound_method_clear(slots, tokens, _BOUND_METHOD_ARG1)
    _bound_method_clear(slots, tokens, _BOUND_METHOD_ARG0)
    _bound_method_clear(slots, tokens, _BOUND_METHOD_RECEIVER)
    _bound_method_clear(slots, tokens, _BOUND_METHOD_INPUT1)
    _bound_method_clear(slots, tokens, _BOUND_METHOD_INPUT0)
    if status != 0:
        _bound_method_clear(slots, tokens, _BOUND_METHOD_RESULT)
    py_clear_exception()
    py_tls_exc_swap_slot(error)
    pcc_py_gc_minor_graph_lock()
    value = pcc_gc_load_ptr(null(), result)
    prior: int = 0
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        prior = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & 64
        pcc_gc_pin(value)
    pcc_py_gc_minor_graph_unlock()
    token: int = load_i64(tokens, _BOUND_METHOD_RESULT * C_POINTER_SIZE)
    if token >= 0 and pcc_gc_foreign_lease_release(result, token) != 0:
        pcc_platform_abort()
        return null()
    pcc_gc_frame_leave(slots)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(result, prior)


# Operations: dict 1..11, list 21..28.
def _bound_method_operation(tag: int, name) -> int:
    if tag == PY_TYPE_DICT:
        if strcmp(name, cstr("get")) == 0:
            return 1
        if strcmp(name, cstr("keys")) == 0:
            return 2
        if strcmp(name, cstr("values")) == 0:
            return 3
        if strcmp(name, cstr("items")) == 0:
            return 4
        if strcmp(name, cstr("setdefault")) == 0:
            return 5
        if strcmp(name, cstr("popitem")) == 0:
            return 6
        if strcmp(name, cstr("update")) == 0:
            return 7
        if strcmp(name, cstr("copy")) == 0:
            return 8
        if strcmp(name, cstr("clear")) == 0:
            return 9
        if strcmp(name, cstr("__contains__")) == 0:
            return 10
        if strcmp(name, cstr("__getitem__")) == 0:
            return 11
    elif tag == PY_TYPE_LIST:
        if strcmp(name, cstr("extend")) == 0:
            return 21
        if strcmp(name, cstr("insert")) == 0:
            return 22
        if strcmp(name, cstr("remove")) == 0:
            return 23
        if strcmp(name, cstr("index")) == 0:
            return 24
        if strcmp(name, cstr("count")) == 0:
            return 25
        if strcmp(name, cstr("reverse")) == 0:
            return 26
        if strcmp(name, cstr("copy")) == 0:
            return 27
        if strcmp(name, cstr("clear")) == 0:
            return 28
    return 0


def _bound_method_name(operation: int):
    if operation == 1:
        return cstr("get")
    if operation == 2:
        return cstr("keys")
    if operation == 3:
        return cstr("values")
    if operation == 4:
        return cstr("items")
    if operation == 5:
        return cstr("setdefault")
    if operation == 6:
        return cstr("popitem")
    if operation == 7:
        return cstr("update")
    if operation == 8 or operation == 27:
        return cstr("copy")
    if operation == 9 or operation == 28:
        return cstr("clear")
    if operation == 10:
        return cstr("__contains__")
    if operation == 11:
        return cstr("__getitem__")
    if operation == 21:
        return cstr("extend")
    if operation == 22:
        return cstr("insert")
    if operation == 23:
        return cstr("remove")
    if operation == 24:
        return cstr("index")
    if operation == 25:
        return cstr("count")
    return cstr("reverse")


def _bound_method_arity(operation: int) -> int:
    # kind * 100 + minimum * 10 + maximum. Kinds select CPython's texts:
    # 0 "takes no arguments", 1 "takes exactly one argument",
    # 2 "expected at least/at most", 3 "expected N arguments".
    if operation == 1 or operation == 5:
        return 212
    if operation == 7:
        return 201
    if operation == 10 or operation == 11 or operation == 21 or operation == 23 or operation == 25:
        return 111
    if operation == 22:
        return 322
    if operation == 24:
        return 213
    return 0


def _bound_method_decimal(message, at: int, value: int) -> int:
    digits = stack_alloc(24)
    count: int = 0
    while count == 0 or value > 0:
        quotient: int = unsigned_div_i64(value, 10)
        store_i8(digits, 23 - count, 48 + value - quotient * 10)
        value = quotient
        count = count + 1
    while count > 0:
        store_i8(message, at, load_i8(digits, 24 - count))
        at = at + 1
        count = count - 1
    return at


def _builtin_arity_error(owner, maximum: int, given: int):
    """The vectorcall check: "str expected at most 3 arguments, got 4"."""
    message = stack_alloc(96)
    at: int = _cstr_append(message, 0, owner)
    at = _cstr_append(message, at, cstr(" expected at most "))
    at = _bound_method_decimal(message, at, maximum)
    at = _cstr_append(message, at, cstr(" argument, got ") if maximum == 1 else cstr(" arguments, got "))
    at = _bound_method_decimal(message, at, given)
    store_i8(message, at, 0)
    py_raise_owned(py_exc_new(3, message))
    return null()


def _bound_method_arity_error(operation: int, kind: int, minimum: int,
                              maximum: int, given: int) -> int:
    message = stack_alloc(160)
    at: int = 0
    if kind == 0 or kind == 1:
        at = _cstr_append(message, at, cstr("dict.") if operation < 20 else cstr("list."))
        at = _cstr_append(message, at, _bound_method_name(operation))
        if kind == 0:
            at = _cstr_append(message, at, cstr("() takes no arguments ("))
        else:
            at = _cstr_append(message, at, cstr("() takes exactly one argument ("))
        at = _bound_method_decimal(message, at, given)
        at = _cstr_append(message, at, cstr(" given)"))
    else:
        at = _cstr_append(message, at, _bound_method_name(operation))
        bound: int = minimum
        if kind == 3:
            at = _cstr_append(message, at, cstr(" expected "))
        elif given < minimum:
            at = _cstr_append(message, at, cstr(" expected at least "))
        else:
            at = _cstr_append(message, at, cstr(" expected at most "))
            bound = maximum
        at = _bound_method_decimal(message, at, bound)
        if bound == 1:
            at = _cstr_append(message, at, cstr(" argument, got "))
        else:
            at = _cstr_append(message, at, cstr(" arguments, got "))
        at = _bound_method_decimal(message, at, given)
    store_i8(message, at, 0)
    py_raise_owned(py_exc_new(3, message))
    return -1


def _bound_method_body(slots, tokens) -> int:
    captures = load_ptr(slots, _BOUND_METHOD_INPUT0 * C_POINTER_SIZE)
    # The operation is a small int held by the rooted captures tuple.
    operation: int = py_int_value_i64(
        load_ptr(captures, PYTUPLEOBJECT_ITEMS_OFFSET + C_POINTER_SIZE))
    args = load_ptr(slots, _BOUND_METHOD_INPUT1 * C_POINTER_SIZE)
    nargs: int = 0
    if ptr_is_null(args) == 0 and is_tagged_int(args) == 0:
        if load_i32(args, 8) == PY_TYPE_TUPLE:
            nargs = py_tuple_len(args)
    arity: int = _bound_method_arity(operation)
    kind: int = unsigned_div_i64(arity, 100)
    minimum: int = unsigned_div_i64(arity, 10) - kind * 10
    maximum: int = arity - unsigned_div_i64(arity, 10) * 10
    if nargs < minimum or nargs > maximum:
        return _bound_method_arity_error(operation, kind, minimum, maximum, nargs)
    store_ptr(slots, _BOUND_METHOD_RECEIVER * C_POINTER_SIZE, py_tuple_get(captures, 0))
    if _bound_method_adopt(slots, tokens, _BOUND_METHOD_RECEIVER) != 0:
        return -1
    if nargs >= 1:
        store_ptr(slots, _BOUND_METHOD_ARG0 * C_POINTER_SIZE, py_tuple_get(args, 0))
        if _bound_method_adopt(slots, tokens, _BOUND_METHOD_ARG0) != 0:
            return -1
    if nargs >= 2:
        store_ptr(slots, _BOUND_METHOD_ARG1 * C_POINTER_SIZE, py_tuple_get(args, 1))
        if _bound_method_adopt(slots, tokens, _BOUND_METHOD_ARG1) != 0:
            return -1
    elif operation == 1 or operation == 5:
        none = global_load_ptr("py_None")
        py_incref(none)
        store_ptr(slots, _BOUND_METHOD_ARG1 * C_POINTER_SIZE, none)
        if _bound_method_adopt(slots, tokens, _BOUND_METHOD_ARG1) != 0:
            return -1
    if nargs >= 3:
        store_ptr(slots, _BOUND_METHOD_ARG2 * C_POINTER_SIZE, py_tuple_get(args, 2))
        if _bound_method_adopt(slots, tokens, _BOUND_METHOD_ARG2) != 0:
            return -1
    receiver = load_ptr(slots, _BOUND_METHOD_RECEIVER * C_POINTER_SIZE)
    argument = load_ptr(slots, _BOUND_METHOD_ARG0 * C_POINTER_SIZE)
    result = null()
    returns_none: int = 0
    if operation == 1 or operation == 5:
        status: int = 0
        if operation == 1:
            status = py_dict_get_default_slots(
                ptr_add(slots, _BOUND_METHOD_RECEIVER * C_POINTER_SIZE),
                ptr_add(slots, _BOUND_METHOD_ARG0 * C_POINTER_SIZE),
                ptr_add(slots, _BOUND_METHOD_ARG1 * C_POINTER_SIZE),
                ptr_add(slots, _BOUND_METHOD_RESULT * C_POINTER_SIZE))
        else:
            status = py_dict_setdefault_slots(
                ptr_add(slots, _BOUND_METHOD_RECEIVER * C_POINTER_SIZE),
                ptr_add(slots, _BOUND_METHOD_ARG0 * C_POINTER_SIZE),
                ptr_add(slots, _BOUND_METHOD_ARG1 * C_POINTER_SIZE),
                ptr_add(slots, _BOUND_METHOD_RESULT * C_POINTER_SIZE))
        if status != 0:
            return -1
        return _bound_method_adopt(slots, tokens, _BOUND_METHOD_RESULT)
    elif operation == 2:
        result = py_dict_keys(receiver)
    elif operation == 3:
        result = py_dict_values(receiver)
    elif operation == 4:
        result = py_dict_items(receiver)
    elif operation == 6:
        result = py_dict_popitem(receiver)
    elif operation == 7:
        if nargs == 1:
            if py_dict_update_slots(
                    ptr_add(slots, _BOUND_METHOD_RECEIVER * C_POINTER_SIZE),
                    ptr_add(slots, _BOUND_METHOD_ARG0 * C_POINTER_SIZE)) != 0:
                return -1
        returns_none = 1
    elif operation == 8:
        result = py_copy_copy(receiver)
    elif operation == 9:
        py_dict_clear(receiver)
        returns_none = 1
    elif operation == 10:
        found: int = py_dict_contains(receiver, argument)
        if py_err_occurred() != 0:
            return -1
        result = py_bool_from_bit(found)
    elif operation == 11:
        result = py_dict_getitem(receiver, argument)
    elif operation == 21:
        py_list_extend(receiver, argument)
        returns_none = 1
    elif operation == 22:
        position: int = py_obj_index_i64(argument)
        if py_err_occurred() != 0:
            return -1
        py_list_insert(load_ptr(slots, _BOUND_METHOD_RECEIVER * C_POINTER_SIZE), position,
                       load_ptr(slots, _BOUND_METHOD_ARG1 * C_POINTER_SIZE))
        returns_none = 1
    elif operation == 23:
        py_list_remove(receiver, argument)
        returns_none = 1
    elif operation == 24:
        # __index__ may run user code; the bounds stay in their rooted slots.
        start: int = 0
        stop: int = 9223372036854775807
        if nargs >= 2:
            start = py_obj_index_i64(load_ptr(slots, _BOUND_METHOD_ARG1 * C_POINTER_SIZE))
            if py_err_occurred() != 0:
                return -1
        if nargs >= 3:
            stop = py_obj_index_i64(load_ptr(slots, _BOUND_METHOD_ARG2 * C_POINTER_SIZE))
            if py_err_occurred() != 0:
                return -1
        found_at: int = py_list_index_range(
            load_ptr(slots, _BOUND_METHOD_RECEIVER * C_POINTER_SIZE),
            load_ptr(slots, _BOUND_METHOD_ARG0 * C_POINTER_SIZE), start, stop)
        if py_err_occurred() != 0:
            return -1
        result = py_int_from_i64(found_at)
    elif operation == 25:
        counted: int = py_list_count(receiver, argument)
        if py_err_occurred() != 0:
            return -1
        result = py_int_from_i64(counted)
    elif operation == 26:
        py_list_reverse(receiver)
        returns_none = 1
    elif operation == 27:
        result = py_list_copy(receiver)
    elif operation == 28:
        py_list_clear(receiver)
        returns_none = 1
    if py_err_occurred() != 0:
        return -1
    if returns_none != 0:
        result = global_load_ptr("py_None")
        py_incref(result)
    store_ptr(slots, _BOUND_METHOD_RESULT * C_POINTER_SIZE, result)
    return _bound_method_adopt(slots, tokens, _BOUND_METHOD_RESULT)


def _py_bound_method_entry(captures, args):
    borrowed = stack_alloc(_BOUND_METHOD_INPUT_COUNT * C_POINTER_SIZE)
    store_ptr(borrowed, _BOUND_METHOD_INPUT0 * C_POINTER_SIZE, captures)
    store_ptr(borrowed, _BOUND_METHOD_INPUT1 * C_POINTER_SIZE, args)
    slots = stack_alloc(_BOUND_METHOD_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_BOUND_METHOD_SLOT_COUNT * C_POINTER_SIZE)
    status: int = _bound_method_begin(borrowed, slots, tokens)
    if status == 0:
        status = _bound_method_body(slots, tokens)
    return _bound_method_finish(borrowed, slots, tokens, status)


def _py_bound_method(o, operation: int):
    borrowed = stack_alloc(_BOUND_METHOD_INPUT_COUNT * C_POINTER_SIZE)
    store_ptr(borrowed, _BOUND_METHOD_INPUT0 * C_POINTER_SIZE, o)
    store_ptr(borrowed, _BOUND_METHOD_INPUT1 * C_POINTER_SIZE, null())
    slots = stack_alloc(_BOUND_METHOD_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_BOUND_METHOD_SLOT_COUNT * C_POINTER_SIZE)
    status: int = _bound_method_begin(borrowed, slots, tokens)
    if status == 0:
        store_ptr(slots, _BOUND_METHOD_ARG0 * C_POINTER_SIZE, py_tuple_new(2))
        status = _bound_method_adopt(slots, tokens, _BOUND_METHOD_ARG0)
    if status == 0:
        captures = load_ptr(slots, _BOUND_METHOD_ARG0 * C_POINTER_SIZE)
        py_tuple_set_item(captures, 0, load_ptr(slots, _BOUND_METHOD_INPUT0 * C_POINTER_SIZE))
        py_tuple_set_item(captures, 1, py_int_from_i64(operation))
        if py_err_occurred() != 0:
            status = -1
    if status == 0:
        store_ptr(slots, _BOUND_METHOD_RESULT * C_POINTER_SIZE, py_func_new_bound(
            _py_bound_method_entry,
            load_ptr(slots, _BOUND_METHOD_ARG0 * C_POINTER_SIZE), _bound_method_name(operation),
            load_ptr(slots, _BOUND_METHOD_INPUT0 * C_POINTER_SIZE),
        ))
        status = _bound_method_adopt(slots, tokens, _BOUND_METHOD_RESULT)
    return _bound_method_finish(borrowed, slots, tokens, status)


def _py_set_pop_bound(o):
    # Pinned across both allocations as in _py_lock_method_bound.
    prior: int = _py_sync_pin(o)
    captures = py_tuple_new(1)
    if ptr_is_null(captures) != 0:
        _py_sync_unpin(o, prior)
        return null()
    pcc_gc_pin(captures)
    py_tuple_set_item(captures, 0, o)
    fn = py_func_new_bound(_py_set_pop_bound_entry, captures, cstr("pop"), o)
    pcc_gc_pin(fn)
    pcc_gc_unpin(captures)
    py_decref(captures)
    _py_sync_unpin(o, prior)
    pcc_gc_unpin(fn)
    return fn


_PROPERTY_ACCESSOR_OWNER = 0
_PROPERTY_ACCESSOR_RESULT = 1
_PROPERTY_ACCESSOR_ERROR = 2
_PROPERTY_ACCESSOR_SLOT_COUNT = 3

define_global_i32("pcc_property_accessor_borrowed_map", -1)
define_global_i32("pcc_property_accessor_owned_map", 3)


def _property_accessor_clear(slots, tokens, index: int) -> None:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    token: int = load_i64(tokens, index * C_POINTER_SIZE)
    if token >= 0:
        if pcc_gc_foreign_lease_release(slot, token) != 0:
            pcc_platform_abort()
            return
    store_i64(tokens, index * C_POINTER_SIZE, -1)
    pcc_gc_store_root(slot, null())


def _property_accessor_get(descriptor, field_offset: int):
    # Retain from the actual traced field while its container is leased.
    # No borrowed raw child survives a lock wait or deferred retain logging.
    borrowed = stack_alloc(C_POINTER_SIZE)
    store_ptr(borrowed, 0, descriptor)
    pcc_gc_frame_enter(global_addr("pcc_property_accessor_borrowed_map"), borrowed)
    slots = stack_alloc(_PROPERTY_ACCESSOR_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_PROPERTY_ACCESSOR_SLOT_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _PROPERTY_ACCESSOR_SLOT_COUNT * C_POINTER_SIZE)
    index: int = 0
    while index < _PROPERTY_ACCESSOR_SLOT_COUNT:
        store_i64(tokens, index * C_POINTER_SIZE, -1)
        index = index + 1
    pcc_gc_frame_enter(global_addr("pcc_property_accessor_owned_map"), slots)
    owner = ptr_add(slots, _PROPERTY_ACCESSOR_OWNER * C_POINTER_SIZE)
    result = ptr_add(slots, _PROPERTY_ACCESSOR_RESULT * C_POINTER_SIZE)
    error = ptr_add(slots, _PROPERTY_ACCESSOR_ERROR * C_POINTER_SIZE)
    token: int = pcc_gc_root_copy_borrowed_lease(owner, borrowed)
    store_i64(tokens, _PROPERTY_ACCESSOR_OWNER * C_POINTER_SIZE, token)
    if token >= 0:
        source = ptr_add(load_ptr(owner, 0), field_offset)
        if ptr_is_null(load_ptr(source, 0)) != 0:
            source = global_addr("py_None")
        token = pcc_gc_root_copy_lease(result, source)
        store_i64(tokens, _PROPERTY_ACCESSOR_RESULT * C_POINTER_SIZE, token)
    status: int = 0
    if token < 0:
        status = -1
        if py_err_occurred() == 0:
            py_raise_owned(py_exc_new(7, cstr("property accessor owner lease failed")))
    if py_err_occurred() != 0:
        status = -1
    py_tls_exc_swap_slot(error)
    store_ptr(borrowed, 0, null())
    _property_accessor_clear(slots, tokens, _PROPERTY_ACCESSOR_OWNER)
    if status != 0:
        _property_accessor_clear(slots, tokens, _PROPERTY_ACCESSOR_RESULT)
    py_clear_exception()
    py_tls_exc_swap_slot(error)
    pcc_py_gc_minor_graph_lock()
    value = pcc_gc_load_ptr(null(), result)
    prior: int = 0
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        prior = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & 64
        pcc_gc_pin(value)
    pcc_py_gc_minor_graph_unlock()
    token = load_i64(tokens, _PROPERTY_ACCESSOR_RESULT * C_POINTER_SIZE)
    if token >= 0 and pcc_gc_foreign_lease_release(result, token) != 0:
        pcc_platform_abort()
        return null()
    pcc_gc_frame_leave(slots)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(result, prior)


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
    pcc_diagnostics_runtime_log_event_code(7, 5, tag, 0, o)

    # A pcc instance is never a C-API type object, list, lock or str, so the
    # hooks below cannot answer for it; C-extension objects carry tags from
    # PY_TYPE_CEXT_TAG_BASE up and keep the full walk.
    if tag == PY_TYPE_INSTANCE or (
        tag >= PY_TYPE_USER_CLASS_START and tag < PY_TYPE_CEXT_TAG_BASE
    ):
        result = py_instance_getattr(o, name)
        if ptr_is_null(result) == 0:
            return result
        if py_err_occurred() != 0:
            return result
        return _raise_attribute_error(o, name)

    if tag == PY_TYPE_PROPERTY:
        field_offset: int = -1
        if strcmp(name, cstr("fget")) == 0:
            field_offset = PYPROPERTYOBJECT_FGET_OFFSET
        elif strcmp(name, cstr("fset")) == 0:
            field_offset = PYPROPERTYOBJECT_FSET_OFFSET
        elif strcmp(name, cstr("fdel")) == 0:
            field_offset = PYPROPERTYOBJECT_FDEL_OFFSET
        if field_offset >= 0:
            return _property_accessor_get(o, field_offset)

    if tag == PY_TYPE_STATICMETHOD:
        if strcmp(name, cstr("__func__")) == 0 or strcmp(name, cstr("__wrapped__")) == 0:
            func = pcc_gc_load_ptr(o, ptr_add(o, PYSTATICMETHODOBJECT_FUNC_OFFSET))
            py_incref(func)
            return func

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

    if tag == PY_TYPE_THREAD_LOCK or tag == PY_TYPE_THREAD_RLOCK:
        if _cstr_is_dunder_enter(name) != 0:
            return _py_lock_context_bound(o, 0)
        if _cstr_is_dunder_exit(name) != 0:
            return _py_lock_context_bound(o, 1)
        if _cstr_is_acquire(name) != 0:
            return _py_lock_method_bound(o, 0)
        if _cstr_is_release(name) != 0:
            return _py_lock_method_bound(o, 1)
        if strcmp(name, cstr("locked")) == 0:
            return _py_lock_method_bound(o, 2)
    if (
        tag == PY_TYPE_THREAD_SEMAPHORE
        or tag == PY_TYPE_THREAD_EVENT
        or tag == PY_TYPE_THREAD_CONDITION
    ):
        sync_method = _py_sync_method_bound(o, tag, name)
        if ptr_is_null(sync_method) == 0:
            return sync_method
    if tag == PY_TYPE_LIST and strcmp(name, cstr("append")) == 0:
        return _py_list_append_bound(o)
    if _cstr_is_pop(name) != 0:
        if tag == PY_TYPE_LIST:  # PY_TYPE_LIST
            return _py_list_pop_bound(o)
        if tag == PY_TYPE_DICT:  # PY_TYPE_DICT
            return _py_dict_pop_bound(o)
        if tag == PY_TYPE_SET:  # PY_TYPE_SET
            return _py_set_pop_bound(o)
    if tag == PY_TYPE_DICT or tag == PY_TYPE_LIST:
        bound_operation: int = _bound_method_operation(tag, name)
        if bound_operation != 0:
            return _py_bound_method(o, bound_operation)

    if tag == PY_TYPE_STR:
        result = py_str_getattr(o, name)
        if ptr_is_null(result) == 0 or py_err_occurred() != 0:
            return result

    if tag == PY_TYPE_TUPLE:
        result = py_tuple_getattr(o, name)
        if ptr_is_null(result) == 0 or py_err_occurred() != 0:
            return result
        return _raise_attribute_error(o, name)

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
        if _cstr_is_dunder_dict(name) != 0:
            if ptr_is_null(attrs) != 0:
                attrs = py_dict_new()
                if ptr_is_null(attrs) != 0:
                    return null()
                pcc_gc_store_ptr(o, ptr_add(o, 88), attrs)
                return attrs
            py_incref(attrs)
            return attrs
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
            return py_coroutine_bound_method(o, 0)
        elif strcmp(name, cstr("throw")) == 0:
            return py_coroutine_bound_method(o, 1)
        elif strcmp(name, cstr("close")) == 0:
            return py_coroutine_bound_method(o, 2)
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
    if tag == PY_TYPE_FILE:
        result = py_file_getattr(o, name)
        if ptr_is_null(result) == 0 or py_err_occurred() != 0:
            return result
        return _raise_attribute_error(o, name)
    if tag == PY_TYPE_COMPLEX:  # PY_TYPE_COMPLEX
        if _cstr_is_real(name) != 0:
            return py_complex_real(o)
        if _cstr_is_imag(name) != 0:
            return py_complex_imag(o)
        return _raise_attribute_error(o, name)
    if tag == PY_TYPE_EXC:  # PY_TYPE_EXC
        if _cstr_is_args(name) != 0:
            return py_exc_get_args(o)
        if _cstr_is_value(name) != 0:
            result = py_exc_get_legacy_value(o)
            if ptr_is_null(result) == 0:
                return result
            return _raise_attribute_error(pcc_gc_note_relocation_read(o), name)
        os_field: int = _os_error_attribute_index(name)
        if (load_i32(o, 12) & PY_FLAG_EXC_OS_PAYLOAD) != 0:
            if os_field >= 0:
                return py_os_error_get_field(o, os_field)
        elif os_field > 0 and _is_os_error(o) != 0:
            return py_os_error_get_field(pcc_gc_note_relocation_read(o), os_field)
        if (load_i32(o, 12) & PY_FLAG_EXC_UNICODE_PAYLOAD) != 0:
            field: int = _unicode_error_attribute_index(name)
            if field >= 0:
                return py_unicode_error_get_field(o, field)
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
        elif strcmp(name, cstr("__suppress_context__")) == 0:
            o = pcc_gc_note_relocation_read(o)
            suppressed: int = atomic_load_i32(o, 12, "relaxed") & PY_FLAG_EXC_SUPPRESS_CONTEXT
            return py_bool_from_bit(1 if suppressed != 0 else 0)
        elif strcmp(name, cstr("__traceback__")) == 0:
            # A NEW reference already (built from the frame records).
            return py_exc_traceback_object(o)
        elif strcmp(name, cstr("code")) == 0 and py_exc_matches(
            o, py_exc_builtin_class(53)  # PY_EXC_SYSTEMEXIT
        ) != 0:
            # SystemExit.code: its argument (None when raised bare).
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
        if ptr_is_null(result) == 0:
            py_incref(result)
            return result
        if py_err_occurred() != 0:
            return result
        # The builtin exception classes' native members (BaseException.__str__,
        # add_note, ...), bound to this exception.
        member = py_builtin_exception_member(o, name)
        if ptr_is_null(member) == 0 or py_err_occurred() != 0:
            return member
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
    pcc_diagnostics_runtime_log_event_code(7, 5, tag, 1, o)

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
    pcc_diagnostics_runtime_log_event_code(7, 5, tag, 2, o)

    # As in py_obj_getattr: a pcc instance is never a C-API type object or a
    # builtin the hooks below answer for.  `getattr(node, field, None)` over
    # AST nodes reaches here millions of times per native frontend worker.
    if tag == PY_TYPE_INSTANCE or (
        tag >= PY_TYPE_USER_CLASS_START and tag < PY_TYPE_CEXT_TAG_BASE
    ):
        return py_instance_getattr(o, name)

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


def _builtin_attr_name_text(name):
    """C text of a str (or str subclass) attribute name; NULL after TypeError."""
    payload = py_str_payload(name)
    if ptr_is_null(payload) == 0:
        return py_str_utf8(payload)
    message = stack_alloc(160)
    prefix = cstr("attribute name must be string, not '")
    length: int = 0
    while load_i8(prefix, length) != 0:
        store_i8(message, length, load_i8(prefix, length))
        length = length + 1
    shown = _obj_type_name_cstr(name)
    index: int = 0
    while load_i8(shown, index) != 0 and index < 100:
        store_i8(message, length, load_i8(shown, index))
        length = length + 1
        index = index + 1
    store_i8(message, length, 39)
    store_i8(message, length + 1, 0)
    py_raise_owned(py_exc_new(3, message))
    return null()


@c_abi_export("py_builtin_hasattr")
def py_builtin_hasattr(o, name) -> int:
    """hasattr(o, name): 1 or 0; -1 with any exception but AttributeError."""
    text = _builtin_attr_name_text(name)
    if ptr_is_null(text) != 0:
        return -1
    got = py_obj_getattr_maybe(o, text)
    if ptr_is_null(got) == 0:
        py_decref(got)
        return 1
    if py_err_occurred() == 0:
        return 0
    if py_exc_matches(py_current_exception(), py_exc_builtin_class(6)) != 0:  # AttributeError
        py_clear_exception()
        return 0
    return -1


def _unicode_error_attribute_index(name) -> int:
    if strcmp(name, cstr("args")) == 0:
        return 0
    if strcmp(name, cstr("encoding")) == 0:
        return 1
    if strcmp(name, cstr("object")) == 0:
        return 2
    if strcmp(name, cstr("start")) == 0:
        return 3
    if strcmp(name, cstr("end")) == 0:
        return 4
    if strcmp(name, cstr("reason")) == 0:
        return 5
    return -1


def _is_os_error(o) -> int:
    # A live OSError subclass guarantees its canonical base class was cached.
    # Read it without lazy class construction while operands are borrowed.
    cls = pcc_gc_load_ptr(null(), ptr_add(global_addr("py_exc_classes"), 14 * C_POINTER_SIZE))
    if ptr_is_null(cls):
        return 0
    return py_exc_matches(o, cls)


def _os_error_attribute_index(name) -> int:
    if _cstr_is_args(name) != 0:
        return 0
    if strcmp(name, cstr("errno")) == 0:
        return 1
    if strcmp(name, cstr("strerror")) == 0:
        return 2
    if strcmp(name, cstr("filename")) == 0:
        return 3
    if strcmp(name, cstr("filename2")) == 0:
        return 4
    return -1


@c_abi_export("py_obj_setattr")
def py_obj_setattr(o, name, v) -> int:
    if ptr_is_null(o) != 0:
        return _raise_attribute_status(o, name)
    if ptr_is_null(name) != 0:
        return _raise_attribute_status(o, name)
    if is_tagged_int(o) != 0:
        return _raise_attribute_status(o, name)
    tag: int = load_i32(o, 8)
    pcc_diagnostics_runtime_log_event_code(7, 6, tag, 0, o)

    if tag == PY_TYPE_EXC:
        os_field: int = _os_error_attribute_index(name)
        if os_field >= 0 and _is_os_error(o) != 0:
            return py_os_error_set_field(pcc_gc_note_relocation_read(o), os_field, pcc_gc_note_relocation_read(v))

    if tag == PY_TYPE_EXC and (load_i32(o, 12) & PY_FLAG_EXC_UNICODE_PAYLOAD) != 0:
        field: int = _unicode_error_attribute_index(name)
        if field >= 0:
            return py_unicode_error_set_field(o, field, v)

    if tag == PY_TYPE_EXC and strcmp(name, cstr("__suppress_context__")) == 0:
        if ptr_eq(v, global_load_ptr("py_True")) != 0:
            o = pcc_gc_note_relocation_read(o)
            atomic_rmw_i32("or", o, 12, PY_FLAG_EXC_SUPPRESS_CONTEXT, "relaxed")
            return 0
        if ptr_eq(v, global_load_ptr("py_False")) != 0:
            o = pcc_gc_note_relocation_read(o)
            atomic_rmw_i32("and", o, 12, ~PY_FLAG_EXC_SUPPRESS_CONTEXT, "relaxed")
            return 0
        py_raise_owned(py_exc_new(3, cstr("attribute value type must be bool")))
        return -1

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
        if _cstr_is_dunder_dict(name) != 0:
            if _type_of(v) != PY_TYPE_DICT:
                py_raise_owned(py_exc_new(3, cstr("function __dict__ must be set to a dictionary")))
                return -1
            pcc_gc_store_ptr(o, ptr_add(o, 88), v)
            return 0
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
    pcc_diagnostics_runtime_log_event_code(7, 7, tag, 0, o)

    if tag == PY_TYPE_EXC and strcmp(name, cstr("__suppress_context__")) == 0:
        py_raise_owned(py_exc_new(3, cstr("can't delete numeric/char attribute")))
        return -1

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
    if tag == PY_TYPE_ELLIPSIS:
        return cstr("'ellipsis' object is not callable")
    if tag == PY_TYPE_NOTIMPLEMENTED:
        return cstr("'NotImplementedType' object is not callable")
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
    pcc_diagnostics_runtime_log_event_code(7, 10, tag, 0, callable)
    py_raise_owned(py_exc_new(3, _not_callable_message(tag)))
    return null()


def _instance_is_of_class(obj, cls) -> int:
    """1 when ``obj`` is an instance whose class is exactly ``cls``."""
    if ptr_is_null(obj) != 0 or ptr_is_null(cls) != 0:
        return 0
    if is_tagged_int(obj) != 0:
        return 0
    if _is_instance_tag(load_i32(obj, 8)) == 0:
        return 0
    owner = pcc_gc_load_ptr(obj, ptr_add(obj, PYINSTANCEOBJECT_CLS_OFFSET))
    if ptr_eq(owner, cls) != 0:
        return 1
    return 0


def _builtin_exception_class_tag(cls) -> int:
    """Index of ``cls`` in the builtin exception class table, or -1.

    Reads the cache directly so the probe never materializes a class.
    """
    tag: int = 0
    while tag < 65:  # PY_EXC_N_BUILTIN
        if ptr_eq(load_ptr(global_addr("py_exc_classes"), tag * 8), cls) != 0:
            return tag
        tag = tag + 1
    return -1


def _builtin_exception_call(cls, args, nargs: int):
    """``category(message)`` for a builtin exception class value.

    Structured builtin payloads use dedicated constructors. Remaining classes
    retain the message-only path used by static ``ValueError("x")`` calls;
    generic class allocation would lose their message and ``args``.
    """
    exception_tag: int = _builtin_exception_class_tag(cls)
    if exception_tag == 14 or (exception_tag >= 34 and exception_tag <= 48):
        # The contiguous FileNotFoundError..ConnectionResetError family.
        # Only OSError itself remaps errno; explicit subclasses keep tag.
        return py_os_error_new(exception_tag, args)
    if exception_tag == 58:
        return py_unicode_decode_error_new(args)
    if exception_tag == 59:
        return py_unicode_encode_error_new(args)
    if nargs == 0:
        return py_exc_new_with_class(cls, null())
    e = py_exc_new_with_class(cls, null())
    if ptr_is_null(e) != 0:
        return null()
    value = py_tuple_get(args, 0)
    pcc_gc_store_ptr(e, ptr_add(e, 24), value)
    py_decref(value)
    return e


_DESCRIPTOR_ARGS = 0
_DESCRIPTOR_KWARGS = 1
_DESCRIPTOR_FGET = 2
_DESCRIPTOR_FSET = 3
_DESCRIPTOR_FDEL = 4
_DESCRIPTOR_RESULT = 5
_DESCRIPTOR_KEY = 6
_DESCRIPTOR_ERROR = 7
_DESCRIPTOR_SLOT_COUNT = 8
_DESCRIPTOR_INPUT_COUNT = 2

define_global_i32("pcc_descriptor_call_borrowed_map", -2)
define_global_i32("pcc_descriptor_call_owned_map", 8)


def _descriptor_call_adopt(slots, tokens, index: int) -> int:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    token: int = pcc_gc_foreign_lease_acquire(slot)
    store_i64(tokens, index * C_POINTER_SIZE, token)
    if token < 0:
        if py_err_occurred() == 0:
            py_raise_owned(py_exc_new(7, cstr("descriptor constructor owner lease failed")))
        return -1
    pcc_py_gc_minor_graph_lock()
    pcc_gc_note_slot_write_barrier(null(), slot, load_ptr(slot, 0))
    pcc_py_gc_minor_graph_unlock()
    return -1 if py_err_occurred() != 0 else 0


def _descriptor_call_clear(slots, tokens, index: int) -> None:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    token: int = load_i64(tokens, index * C_POINTER_SIZE)
    if token >= 0:
        if pcc_gc_foreign_lease_release(slot, token) != 0:
            pcc_platform_abort()
            return
    store_i64(tokens, index * C_POINTER_SIZE, -1)
    pcc_gc_store_root(slot, null())


# Builtin value constructors share the descriptor frame: CPython's argument
# clinic unpacks int/str/bytes/bytearray/complex/dict arguments into its
# three parameter slots, each holding its own counted lease, so a callback
# such as __index__ or __complex__ cannot move an operand under the
# constructor. The parameters are the FGET/FSET/FDEL slots; a literal,
# since a port module constant initialised from another name reads zero.
_CONSTRUCTOR_PARAMETER = 2
_CONSTRUCTOR_PARAMETER_COUNT = 3


def _constructor_tag(tag: int) -> int:
    if tag == PY_TYPE_INT or tag == PY_TYPE_STR or tag == PY_TYPE_DICT:
        return 1
    if tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY or tag == PY_TYPE_COMPLEX:
        return 1
    return 0


def _constructor_owner(tag: int):
    if tag == PY_TYPE_INT:
        return cstr("int")
    if tag == PY_TYPE_STR:
        return cstr("str")
    if tag == PY_TYPE_BYTES:
        return cstr("bytes")
    if tag == PY_TYPE_BYTEARRAY:
        return cstr("bytearray")
    if tag == PY_TYPE_COMPLEX:
        return cstr("complex")
    return cstr("dict")


def _constructor_maximum(tag: int) -> int:
    if tag == PY_TYPE_INT or tag == PY_TYPE_COMPLEX:
        return 2
    if tag == PY_TYPE_DICT:
        return 1
    return _CONSTRUCTOR_PARAMETER_COUNT


def _constructor_parameter_name(tag: int, index: int):
    """The keyword of a constructor's ``index``-th parameter; NULL when it
    is positional-only (int's x, dict's source) or past the signature."""
    if tag == PY_TYPE_INT:
        if index == 1:
            return cstr("base")
        return null()
    if tag == PY_TYPE_STR or tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY:
        if index == 0:
            if tag == PY_TYPE_STR:
                return cstr("object")
            return cstr("source")
        if index == 1:
            return cstr("encoding")
        if index == 2:
            return cstr("errors")
        return null()
    if tag == PY_TYPE_COMPLEX:
        if index == 0:
            return cstr("real")
        if index == 1:
            return cstr("imag")
    return null()


def _constructor_append_type_name(message, at: int, value) -> int:
    name = _obj_type_name_cstr(value)
    index: int = 0
    while index < 100 and load_i8(name, index) != 0:
        store_i8(message, at, load_i8(name, index))
        at = at + 1
        index = index + 1
    return at


def _constructor_clinic_arity_error(owner, maximum: int, nargs: int, given: int) -> int:
    """Argument clinic's "bytes() takes at most 3 arguments (4 given)"; a
    call without positional arguments counts keyword arguments."""
    message = stack_alloc(120)
    at: int = _cstr_append(message, 0, owner)
    at = _cstr_append(message, at, cstr("() takes at most "))
    at = _bound_method_decimal(message, at, maximum)
    if nargs == 0:
        at = _cstr_append(message, at, cstr(" keyword"))
    at = _cstr_append(message, at, cstr(" argument (") if maximum == 1 else cstr(" arguments ("))
    at = _bound_method_decimal(message, at, given)
    at = _cstr_append(message, at, cstr(" given)"))
    store_i8(message, at, 0)
    py_raise_owned(py_exc_new(3, message))
    return -1


def _constructor_keyword_lookup(slots, tokens, name):
    """A NEW reference to the call's keyword argument ``name``, or NULL --
    with the exception set when the lookup itself failed."""
    store_ptr(slots, _DESCRIPTOR_KEY * C_POINTER_SIZE, py_str_new(name, strlen(name)))
    if _descriptor_call_adopt(slots, tokens, _DESCRIPTOR_KEY) != 0:
        return null()
    key = load_ptr(slots, _DESCRIPTOR_KEY * C_POINTER_SIZE)
    if ptr_is_null(key) != 0:
        if py_err_occurred() == 0:
            py_raise_owned(py_exc_new(19, cstr("constructor keyword allocation failed")))
        return null()
    value = py_dict_get(load_ptr(slots, _DESCRIPTOR_KWARGS * C_POINTER_SIZE), key)
    _descriptor_call_clear(slots, tokens, _DESCRIPTOR_KEY)
    return value


def _constructor_accepts_keyword(tag: int, maximum: int, text) -> int:
    index: int = 0
    while index < maximum:
        name = _constructor_parameter_name(tag, index)
        if ptr_is_null(name) == 0 and strcmp(name, text) == 0:
            return 1
        index = index + 1
    return 0


def _constructor_keyword_error(slots, tokens, tag: int, owner, nargs: int, maximum: int) -> int:
    """The TypeError for keywords left after unpacking: a parameter given by
    name and by position, else the first keyword outside the signature."""
    message = stack_alloc(240)
    at: int = 0
    length: int = 0
    offset: int = 0
    index: int = 0
    while index < nargs:
        name = _constructor_parameter_name(tag, index)
        if ptr_is_null(name) == 0:
            value = _constructor_keyword_lookup(slots, tokens, name)
            if ptr_is_null(value) == 0:
                py_decref(value)
                at = _cstr_append(message, 0, cstr("argument for "))
                at = _cstr_append(message, at, owner)
                at = _cstr_append(message, at, cstr("() given by name ('"))
                at = _cstr_append(message, at, name)
                at = _cstr_append(message, at, cstr("') and position ("))
                at = _bound_method_decimal(message, at, index + 1)
                at = _cstr_append(message, at, cstr(")"))
                store_i8(message, at, 0)
                py_raise_owned(py_exc_new(3, message))
                return -1
            if py_err_occurred() != 0:
                return -1
        index = index + 1
    # The key list stays in the result slot, which the frame releases on
    # this failure path.
    store_ptr(slots, _DESCRIPTOR_RESULT * C_POINTER_SIZE,
              py_dict_keys(load_ptr(slots, _DESCRIPTOR_KWARGS * C_POINTER_SIZE)))
    if _descriptor_call_adopt(slots, tokens, _DESCRIPTOR_RESULT) != 0:
        return -1
    count: int = 0
    if ptr_is_null(load_ptr(slots, _DESCRIPTOR_RESULT * C_POINTER_SIZE)) == 0:
        count = py_list_len(load_ptr(slots, _DESCRIPTOR_RESULT * C_POINTER_SIZE))
    status: int = 0
    index = 0
    while index < count and status == 0:
        key = py_list_get(load_ptr(slots, _DESCRIPTOR_RESULT * C_POINTER_SIZE), index)
        if ptr_is_null(key) == 0:
            # The message is copied out before the key is released, and the
            # key before the exception allocates.
            if _type_of(key) != PY_TYPE_STR:
                py_decref(key)
                py_raise_owned(py_exc_new(3, cstr("keywords must be strings")))
                status = -1
            elif _constructor_accepts_keyword(tag, maximum, py_str_utf8(key)) == 0:
                text = py_str_utf8(key)
                at = _cstr_append(message, 0, owner)
                at = _cstr_append(message, at, cstr("() got an unexpected keyword argument '"))
                length = strlen(text)
                if length > 100:
                    length = 100
                offset = 0
                while offset < length:
                    store_i8(message, at, load_i8(text, offset))
                    at = at + 1
                    offset = offset + 1
                at = _cstr_append(message, at, cstr("'"))
                store_i8(message, at, 0)
                py_decref(key)
                py_raise_owned(py_exc_new(3, message))
                status = -1
            else:
                py_decref(key)
        index = index + 1
    if status == 0:
        py_runtime_error_if_unset(
            cstr("builtin constructor"), cstr("builtin constructor left keyword arguments unmatched"),
        )
    return -1


def _constructor_text_argument(slots, tag: int, owner, index: int) -> int:
    """str/bytes take encoding and errors as str: 0, or -1 with CPython's
    "bytes() argument 'encoding' must be str, not int"."""
    value = load_ptr(slots, (_CONSTRUCTOR_PARAMETER + index) * C_POINTER_SIZE)
    if ptr_is_null(value) != 0 or _type_of(value) == PY_TYPE_STR:
        return 0
    message = stack_alloc(200)
    at: int = _cstr_append(message, 0, owner)
    at = _cstr_append(message, at, cstr("() argument '"))
    at = _cstr_append(message, at, _constructor_parameter_name(tag, index))
    at = _cstr_append(message, at, cstr("' must be str, not "))
    at = _constructor_append_type_name(message, at, value)
    store_i8(message, at, 0)
    py_raise_owned(py_exc_new(3, message))
    return -1


def _constructor_publish(slots, tokens, result) -> int:
    store_ptr(slots, _DESCRIPTOR_RESULT * C_POINTER_SIZE, result)
    if _descriptor_call_adopt(slots, tokens, _DESCRIPTOR_RESULT) != 0:
        return -1
    if ptr_is_null(result) != 0:
        if py_err_occurred() == 0:
            py_raise_owned(py_exc_new(19, cstr("builtin constructor returned NULL without an exception")))
        return -1
    return 0


def _constructor_bytes(slots, tokens, tag: int, owner) -> int:
    """bytes_new / bytearray___init__: encode a str source, else convert."""
    if _constructor_text_argument(slots, tag, owner, 1) != 0:
        return -1
    if _constructor_text_argument(slots, tag, owner, 2) != 0:
        return -1
    source = load_ptr(slots, _CONSTRUCTOR_PARAMETER * C_POINTER_SIZE)
    encoding = load_ptr(slots, (_CONSTRUCTOR_PARAMETER + 1) * C_POINTER_SIZE)
    errors = load_ptr(slots, (_CONSTRUCTOR_PARAMETER + 2) * C_POINTER_SIZE)
    if ptr_is_null(source) == 0 and _type_of(source) == PY_TYPE_STR:
        if ptr_is_null(encoding) != 0:
            py_raise_owned(py_exc_new(3, cstr("string argument without an encoding")))
            return -1
        if tag == PY_TYPE_BYTES:
            return _constructor_publish(slots, tokens, py_str_encode_with_encoding(source, encoding, errors))
        # The encoded bytes stay in the scratch slot until the bytearray copy
        # is published.
        store_ptr(slots, _DESCRIPTOR_KEY * C_POINTER_SIZE,
                  py_str_encode_with_encoding(source, encoding, errors))
        if _descriptor_call_adopt(slots, tokens, _DESCRIPTOR_KEY) != 0:
            return -1
        encoded = load_ptr(slots, _DESCRIPTOR_KEY * C_POINTER_SIZE)
        if ptr_is_null(encoded) != 0:
            return _constructor_publish(slots, tokens, null())
        if _constructor_publish(slots, tokens, py_bytearray_from_obj(encoded)) != 0:
            return -1
        _descriptor_call_clear(slots, tokens, _DESCRIPTOR_KEY)
        return 0
    if ptr_is_null(encoding) == 0:
        py_raise_owned(py_exc_new(3, cstr("encoding without a string argument")))
        return -1
    if ptr_is_null(errors) == 0:
        py_raise_owned(py_exc_new(3, cstr("errors without a string argument")))
        return -1
    if tag == PY_TYPE_BYTEARRAY:
        return _constructor_publish(slots, tokens, py_bytearray_from_obj(source))
    if ptr_is_null(source) != 0:
        return _constructor_publish(slots, tokens, py_bytes_new(null(), 0))
    return _constructor_publish(slots, tokens, py_bytes_from_obj(source))


def _constructor_dict(slots, tokens, nkwargs: int) -> int:
    """dict(source, **kwargs): the update protocol, then the keywords."""
    if _constructor_publish(slots, tokens, py_dict_new()) != 0:
        return -1
    source = load_ptr(slots, _CONSTRUCTOR_PARAMETER * C_POINTER_SIZE)
    if ptr_is_null(source) == 0:
        py_dict_update(load_ptr(slots, _DESCRIPTOR_RESULT * C_POINTER_SIZE), source)
        if py_err_occurred() != 0:
            return -1
    if nkwargs != 0:
        py_dict_update(load_ptr(slots, _DESCRIPTOR_RESULT * C_POINTER_SIZE),
                       load_ptr(slots, _DESCRIPTOR_KWARGS * C_POINTER_SIZE))
        if py_err_occurred() != 0:
            return -1
    return 0


def _constructor_result(slots, tokens, tag: int, owner) -> int:
    first = load_ptr(slots, _CONSTRUCTOR_PARAMETER * C_POINTER_SIZE)
    second = load_ptr(slots, (_CONSTRUCTOR_PARAMETER + 1) * C_POINTER_SIZE)
    if tag == PY_TYPE_INT:
        if ptr_is_null(first) != 0:
            if ptr_is_null(second) == 0:
                py_raise_owned(py_exc_new(3, cstr("int() missing string argument")))
                return -1
            return _constructor_publish(slots, tokens, py_int_from_i64(0))
        if ptr_is_null(second) != 0:
            # One owner for int(x): exact floats, __int__/__index__ and
            # CPython's TypeError.
            return _constructor_publish(slots, tokens, py_obj_as_int_object(first, 10))
        return _constructor_publish(slots, tokens, py_obj_as_int_object_args(first, second))
    if tag == PY_TYPE_STR:
        if _constructor_text_argument(slots, tag, owner, 1) != 0:
            return -1
        if _constructor_text_argument(slots, tag, owner, 2) != 0:
            return -1
        third = load_ptr(slots, (_CONSTRUCTOR_PARAMETER + 2) * C_POINTER_SIZE)
        if ptr_is_null(first) != 0:
            return _constructor_publish(slots, tokens, py_str_new(cstr(""), 0))
        if ptr_is_null(second) != 0 and ptr_is_null(third) != 0:
            return _constructor_publish(slots, tokens, py_obj_str(first))
        return _constructor_publish(slots, tokens, py_bytes_decode_with_encoding(first, second, third))
    if tag == PY_TYPE_COMPLEX:
        return _constructor_publish(slots, tokens, py_complex_from_parts(first, second))
    return _constructor_bytes(slots, tokens, tag, owner)


def _constructor_call_body(slots, tokens, tag: int) -> int:
    """Construct a builtin value from this frame's leased call arguments:
    CPython's vectorcall arity check, then the argument clinic's unpacking
    (positions, then keywords, then any keyword left over)."""
    args = load_ptr(slots, _DESCRIPTOR_ARGS * C_POINTER_SIZE)
    nargs: int = 0
    if ptr_is_null(args) == 0:
        nargs = py_tuple_len(args)
    kwargs = load_ptr(slots, _DESCRIPTOR_KWARGS * C_POINTER_SIZE)
    nkwargs: int = 0
    if ptr_is_null(kwargs) == 0 and _type_of(kwargs) == PY_TYPE_DICT:
        nkwargs = py_dict_len(kwargs)
    owner = _constructor_owner(tag)
    maximum: int = _constructor_maximum(tag)
    if tag == PY_TYPE_DICT:
        if nargs > maximum:
            _builtin_arity_error(owner, maximum, nargs)
            return -1
    elif (tag == PY_TYPE_INT or tag == PY_TYPE_STR) and nkwargs == 0 and nargs > maximum:
        _builtin_arity_error(owner, maximum, nargs)
        return -1
    elif nargs + nkwargs > maximum:
        return _constructor_clinic_arity_error(owner, maximum, nargs, nargs + nkwargs)
    index: int = 0
    while index < nargs:
        store_ptr(slots, (_CONSTRUCTOR_PARAMETER + index) * C_POINTER_SIZE,
                  py_tuple_get(load_ptr(slots, _DESCRIPTOR_ARGS * C_POINTER_SIZE), index))
        if _descriptor_call_adopt(slots, tokens, _CONSTRUCTOR_PARAMETER + index) != 0:
            return -1
        index = index + 1
    if tag == PY_TYPE_DICT:
        return _constructor_dict(slots, tokens, nkwargs)
    remaining: int = nkwargs
    while index < maximum and remaining > 0:
        name = _constructor_parameter_name(tag, index)
        if ptr_is_null(name) == 0:
            value = _constructor_keyword_lookup(slots, tokens, name)
            if ptr_is_null(value) == 0:
                store_ptr(slots, (_CONSTRUCTOR_PARAMETER + index) * C_POINTER_SIZE, value)
                if _descriptor_call_adopt(slots, tokens, _CONSTRUCTOR_PARAMETER + index) != 0:
                    return -1
                remaining = remaining - 1
            elif py_err_occurred() != 0:
                return -1
        index = index + 1
    if remaining > 0:
        return _constructor_keyword_error(slots, tokens, tag, owner, nargs, maximum)
    return _constructor_result(slots, tokens, tag, owner)


def _descriptor_call_body(slots, tokens, tag: int) -> int:
    if _constructor_tag(tag) != 0:
        return _constructor_call_body(slots, tokens, tag)
    args = load_ptr(slots, _DESCRIPTOR_ARGS * C_POINTER_SIZE)
    nargs: int = 0
    if ptr_is_null(args) == 0:
        nargs = py_tuple_len(args)
    kwargs = load_ptr(slots, _DESCRIPTOR_KWARGS * C_POINTER_SIZE)
    nkwargs: int = 0
    if ptr_is_null(kwargs) == 0 and ptr_eq(kwargs, global_load_ptr("py_None")) == 0:
        nkwargs = py_dict_len(kwargs)
    if tag == PY_TYPE_FLOAT:
        if nkwargs != 0:
            py_raise_owned(py_exc_new(3, cstr("float() takes no keyword arguments")))
            return -1
        if nargs > 1:
            _builtin_arity_error(cstr("float"), 1, nargs)
            return -1
    elif tag != PY_TYPE_PROPERTY:
        if nkwargs != 0:
            py_raise_owned(py_exc_new(3, cstr("descriptor constructor takes no keyword arguments")))
            return -1
        if nargs != 1:
            py_raise_owned(py_exc_new(3, cstr("descriptor constructor expected exactly one argument")))
            return -1
    elif nargs > 3:
        # The existing native property layout has three accessor slots. Do
        # not silently discard Python's optional doc argument.
        py_raise_owned(py_exc_new(11, cstr("native property doc metadata is not implemented")))
        return -1
    index: int = 0
    while index < nargs:
        store_ptr(slots, (_DESCRIPTOR_FGET + index) * C_POINTER_SIZE,
                  py_tuple_get(load_ptr(slots, _DESCRIPTOR_ARGS * C_POINTER_SIZE), index))
        if _descriptor_call_adopt(slots, tokens, _DESCRIPTOR_FGET + index) != 0:
            return -1
        index = index + 1
    if tag == PY_TYPE_PROPERTY and nkwargs != 0:
        matched: int = 0
        index = 0
        while index < 3:
            name = cstr("fget")
            if index == 1:
                name = cstr("fset")
            elif index == 2:
                name = cstr("fdel")
            store_ptr(slots, _DESCRIPTOR_KEY * C_POINTER_SIZE, py_str_new(name, 4))
            if _descriptor_call_adopt(slots, tokens, _DESCRIPTOR_KEY) != 0:
                return -1
            # Keep positional accessors intact until duplicate checking.
            value = py_dict_get(load_ptr(slots, _DESCRIPTOR_KWARGS * C_POINTER_SIZE),
                                load_ptr(slots, _DESCRIPTOR_KEY * C_POINTER_SIZE))
            if ptr_is_null(value) == 0:
                if index < nargs:
                    py_decref(value)
                    py_raise_owned(py_exc_new(3, cstr("property argument supplied by name and position")))
                    return -1
                store_ptr(slots, (_DESCRIPTOR_FGET + index) * C_POINTER_SIZE, value)
                if _descriptor_call_adopt(slots, tokens, _DESCRIPTOR_FGET + index) != 0:
                    return -1
                matched = matched + 1
            _descriptor_call_clear(slots, tokens, _DESCRIPTOR_KEY)
            index = index + 1
        if matched != nkwargs:
            py_raise_owned(py_exc_new(3, cstr("unsupported property keyword argument")))
            return -1
    if tag == PY_TYPE_FLOAT:
        # Reuse this constructor's argument/result leases: a lease on args
        # alone cannot keep its element's address stable across __float__.
        number: float = 0.0
        if nargs == 1:
            number = py_float_value_of(load_ptr(slots, _DESCRIPTOR_FGET * C_POINTER_SIZE))
            if py_err_occurred() != 0:
                return -1
        store_ptr(slots, _DESCRIPTOR_RESULT * C_POINTER_SIZE,
                  py_float_from_f64(number))
    elif tag == PY_TYPE_STATICMETHOD:
        store_ptr(slots, _DESCRIPTOR_RESULT * C_POINTER_SIZE,
                  py_staticmethod_new(load_ptr(slots, _DESCRIPTOR_FGET * C_POINTER_SIZE)))
    elif tag == PY_TYPE_CLASSMETHOD:
        store_ptr(slots, _DESCRIPTOR_RESULT * C_POINTER_SIZE,
                  py_classmethod_new(load_ptr(slots, _DESCRIPTOR_FGET * C_POINTER_SIZE)))
    else:
        store_ptr(slots, _DESCRIPTOR_RESULT * C_POINTER_SIZE,
                  py_property_new(load_ptr(slots, _DESCRIPTOR_FGET * C_POINTER_SIZE),
                                  load_ptr(slots, _DESCRIPTOR_FSET * C_POINTER_SIZE),
                                  load_ptr(slots, _DESCRIPTOR_FDEL * C_POINTER_SIZE)))
    if _descriptor_call_adopt(slots, tokens, _DESCRIPTOR_RESULT) != 0:
        return -1
    if ptr_is_null(load_ptr(slots, _DESCRIPTOR_RESULT * C_POINTER_SIZE)) != 0:
        if py_err_occurred() == 0:
            py_raise_owned(py_exc_new(19, cstr("descriptor constructor allocation failed")))
        return -1
    return 0


# Mirrors py_class.py: set by py_class_mark_exception_subclass.
_EXCEPTION_SUBCLASS_CLASS_FLAG = 16777216

_EXC_NEW_RECEIVER = 0
_EXC_NEW_BASE = 1
_EXC_NEW_ARGS = 2
_EXC_NEW_KWARGS = 3
_EXC_NEW_SLOT_COUNT = 4

define_global_i32("pcc_exception_new_borrowed_map", -4)


def _exception_new_args(inst, args):
    """Return the owned instance after storing ``args``, or NULL on error.

    BaseException.__new__ keeps the constructor arguments as ``args`` before
    any __init__ runs; an __init__ calling super().__init__ replaces them.
    Keywords belong to __init__, so __new__ passes none. The initializer
    copies from registered roots, so the borrowed inputs form one frame, and
    the instance is read back from that frame: the initializer can relocate
    it. On error the instance's reference is released.
    """
    slots = stack_alloc(_EXC_NEW_SLOT_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _EXC_NEW_SLOT_COUNT * C_POINTER_SIZE)
    store_ptr(slots, _EXC_NEW_RECEIVER * C_POINTER_SIZE, inst)
    store_ptr(slots, _EXC_NEW_BASE * C_POINTER_SIZE, py_exc_builtin_class(0))  # PY_EXC_BASE
    pcc_gc_frame_enter(global_addr("pcc_exception_new_borrowed_map"), slots)
    status: int = 0
    if ptr_is_null(args) != 0:
        store_ptr(slots, _EXC_NEW_ARGS * C_POINTER_SIZE, py_tuple_new(0))
        if ptr_is_null(load_ptr(slots, _EXC_NEW_ARGS * C_POINTER_SIZE)) != 0:
            status = -1
    else:
        py_incref(args)
        store_ptr(slots, _EXC_NEW_ARGS * C_POINTER_SIZE, args)
    if status == 0:
        status = py_exception_subclass_init_slots(
            ptr_add(slots, _EXC_NEW_RECEIVER * C_POINTER_SIZE),
            ptr_add(slots, _EXC_NEW_BASE * C_POINTER_SIZE),
            ptr_add(slots, _EXC_NEW_ARGS * C_POINTER_SIZE),
            ptr_add(slots, _EXC_NEW_KWARGS * C_POINTER_SIZE),
        )
    owned_args = load_ptr(slots, _EXC_NEW_ARGS * C_POINTER_SIZE)
    if ptr_is_null(owned_args) == 0:
        py_decref(owned_args)
    result = load_ptr(slots, _EXC_NEW_RECEIVER * C_POINTER_SIZE)
    pcc_gc_frame_leave(slots)
    if status != 0:
        py_decref(result)
        return null()
    return result


def _type_object_call(args, nargs: int, nkwargs: int):
    # ``type`` reached as a value (``kind = type``, ``type(cls)(...)``) shares
    # the owners of the direct type(x) and type(name, bases, namespace)
    # spellings; the generic class path built an instance of type instead.
    if nargs == 1:
        if nkwargs != 0:
            py_raise_owned(py_exc_new(3, cstr("type() takes no keyword arguments")))
            return null()
        value = py_tuple_get(args, 0)
        out = py_type_builtin(value)
        py_decref(value)
        return out
    if nargs != 3:
        py_raise_owned(py_exc_new(3, cstr("type() takes 1 or 3 arguments")))
        return null()
    if nkwargs != 0:
        # CPython hands class keywords to __init_subclass__; this entry has none.
        py_raise_owned(py_exc_new(11, cstr("pcc type(name, bases, namespace) does not take class keywords")))
        return null()
    name = py_tuple_get(args, 0)
    bases = py_tuple_get(args, 1)
    namespace = py_tuple_get(args, 2)
    out = py_class_new_from_objects(name, bases, namespace)
    py_decref(namespace)
    py_decref(bases)
    py_decref(name)
    return out


def _descriptor_type_call(args, kwargs, tag: int):
    # Descriptor and float constructors share this owning call frame. Every
    # argument and result keeps a counted lease across callbacks and cleanup.
    borrowed = stack_alloc(_DESCRIPTOR_INPUT_COUNT * C_POINTER_SIZE)
    store_ptr(borrowed, _DESCRIPTOR_ARGS * C_POINTER_SIZE, args)
    store_ptr(borrowed, _DESCRIPTOR_KWARGS * C_POINTER_SIZE, kwargs)
    pcc_gc_frame_enter(global_addr("pcc_descriptor_call_borrowed_map"), borrowed)
    slots = stack_alloc(_DESCRIPTOR_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_DESCRIPTOR_SLOT_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _DESCRIPTOR_SLOT_COUNT * C_POINTER_SIZE)
    index: int = 0
    while index < _DESCRIPTOR_SLOT_COUNT:
        store_i64(tokens, index * C_POINTER_SIZE, -1)
        index = index + 1
    pcc_gc_frame_enter(global_addr("pcc_descriptor_call_owned_map"), slots)
    status: int = 0
    index = 0
    while index < _DESCRIPTOR_INPUT_COUNT and status == 0:
        token: int = pcc_gc_root_copy_borrowed_lease(
            ptr_add(slots, index * C_POINTER_SIZE),
            ptr_add(borrowed, index * C_POINTER_SIZE),
        )
        store_i64(tokens, index * C_POINTER_SIZE, token)
        if token < 0:
            if py_err_occurred() == 0:
                py_raise_owned(py_exc_new(7, cstr("descriptor constructor input owner failed")))
            status = -1
        index = index + 1
    if status == 0:
        status = _descriptor_call_body(slots, tokens, tag)
    if py_err_occurred() != 0:
        status = -1
    error = ptr_add(slots, _DESCRIPTOR_ERROR * C_POINTER_SIZE)
    result = ptr_add(slots, _DESCRIPTOR_RESULT * C_POINTER_SIZE)
    py_tls_exc_swap_slot(error)
    memset(borrowed, 0, _DESCRIPTOR_INPUT_COUNT * C_POINTER_SIZE)
    index = _DESCRIPTOR_KEY
    while index >= 0:
        if index != _DESCRIPTOR_RESULT or status != 0:
            _descriptor_call_clear(slots, tokens, index)
        index = index - 1
    py_clear_exception()
    py_tls_exc_swap_slot(error)
    pcc_py_gc_minor_graph_lock()
    value = pcc_gc_load_ptr(null(), result)
    prior: int = 0
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        prior = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & 64
        pcc_gc_pin(value)
    pcc_py_gc_minor_graph_unlock()
    token = load_i64(tokens, _DESCRIPTOR_RESULT * C_POINTER_SIZE)
    if token >= 0 and pcc_gc_foreign_lease_release(result, token) != 0:
        pcc_platform_abort()
        return null()
    pcc_gc_frame_leave(slots)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(result, prior)


def _class_call_new(callable_obj, args, kwargs):
    """``cls.__new__(cls, *args)``, or a plain allocation when undefined.

    ``py_class_lookup`` finds only user-declared methods, so a class without
    its own ``__new__`` takes the allocation path unchanged.
    """
    new_method = py_class_lookup(callable_obj, cstr("__new__"))
    if ptr_is_null(new_method) != 0 and py_class_is_str_subclass(callable_obj) != 0:
        return py_str_subclass_new(callable_obj, args, kwargs)
    if ptr_is_null(new_method) != 0 or is_tagged_int(new_method) != 0:
        return _require_call_result(
            py_instance_new(callable_obj),
            cstr("py_instance_new"),
            cstr("py_instance_new returned NULL without setting an exception"),
        )
    if load_i32(new_method, 8) != PY_TYPE_FUNC:
        return _require_call_result(
            py_instance_new(callable_obj),
            cstr("py_instance_new"),
            cstr("py_instance_new returned NULL without setting an exception"),
        )
    argc: int = 0
    if ptr_is_null(args) == 0:
        argc = py_tuple_len(args)
    full_args = py_tuple_new(argc + 1)
    if ptr_is_null(full_args) != 0:
        return _require_call_result(
            null(),
            cstr("py_tuple_new"),
            cstr("class __new__ could not allocate its argument tuple"),
        )
    py_tuple_set_item(full_args, 0, callable_obj)
    index: int = 0
    while index < argc:
        item = py_tuple_get(args, index)
        py_tuple_set_item(full_args, index + 1, item)
        py_decref(item)
        index = index + 1
    created = py_func_call_kwargs(new_method, full_args, kwargs)
    py_decref(full_args)
    return _require_call_result(
        created,
        cstr("class __new__"),
        cstr("class __new__ returned NULL without setting an exception"),
    )


@c_abi_export("py_obj_call")
def py_obj_call(callable, args, kwargs):
    # Raw compatibility entry. Its existing caller-address contract is not
    # repaired by the slot ABI; keep its semantics while callers migrate.
    return _py_obj_call_body(callable, args, kwargs, 1)


@c_abi_export("py_obj_call_default")
def py_obj_call_default(callable, args, kwargs):
    # Only the slot dispatcher selects this after authoritative special
    # lookup. Inputs have caller-owned address leases for the entire call.
    return _py_obj_call_body(callable, args, kwargs, 0)


def _py_obj_call_body(callable, args, kwargs, include_metaclass: int):
    if ptr_is_null(callable) != 0:
        return py_runtime_error_if_unset(
            cstr("py_obj_call"),
            cstr("py_obj_call received NULL callable"),
        )
    if is_tagged_int(callable) != 0:
        return _raise_not_callable(callable, 2)
    tag: int = load_i32(callable, 8)
    pcc_diagnostics_runtime_log_event_code(7, 8, tag, 0, callable)

    if tag == PY_TYPE_STATICMETHOD:
        func = pcc_gc_load_ptr(callable, ptr_add(callable, PYSTATICMETHODOBJECT_FUNC_OFFSET))
        if include_metaclass == 0:
            return py_obj_call_default(func, args, kwargs)
        return py_obj_call(func, args, kwargs)

    if pcc_capi_type_object_is_callable(callable) != 0:
        # Construction/extension invocation is itself a semantic callee,
        # unlike transparent staticmethod or instance __call__ dispatch.
        # Consume defer here so every internal ordinary call starts sync.
        if py_obj_call_context_is_deferred() != 0:
            if include_metaclass == 0:
                return py_obj_call_default_sync(callable, args, kwargs)
            return py_obj_call_sync(callable, args, kwargs)
        checked_result = pcc_capi_call_type_object(callable, args, kwargs)
        # Keep a fresh non-NULL result out of a polling diagnostic helper.
        if ptr_is_null(checked_result):
            py_runtime_error_if_unset(cstr('pcc_capi_call_type_object'), cstr('pcc_capi_call_type_object returned NULL without setting an exception'))
        return checked_result

    if tag == PY_TYPE_CLASS:  # PY_TYPE_CLASS
        # Construction/extension invocation is itself a semantic callee,
        # unlike transparent staticmethod or instance __call__ dispatch.
        # Consume defer here so every internal ordinary call starts sync.
        if py_obj_call_context_is_deferred() != 0:
            if include_metaclass == 0:
                return py_obj_call_default_sync(callable, args, kwargs)
            return py_obj_call_sync(callable, args, kwargs)
        if include_metaclass != 0:
            metaclass_result = py_class_metaclass_call(callable, args, kwargs)
            if ptr_is_null(metaclass_result) == 0:
                return metaclass_result
            if py_err_occurred() != 0:
                return null()
        nargs: int = 0
        if ptr_is_null(args) == 0:
            nargs = py_tuple_len(args)
        nkwargs: int = 0
        if ptr_is_null(kwargs) == 0 and ptr_eq(kwargs, global_load_ptr("py_None")) == 0:
            if _type_of(kwargs) == PY_TYPE_DICT:
                nkwargs = py_dict_len(kwargs)
        if ptr_eq(callable, global_load_ptr("pcc_type_cls_ellipsis")) != 0:
            if nargs != 0 or nkwargs != 0:
                py_raise_owned(py_exc_new(3, cstr("EllipsisType takes no arguments")))
                return null()
            # This immutable static object cannot move or be reclaimed.
            return global_load_ptr("py_Ellipsis")
        if ptr_eq(callable, global_load_ptr("pcc_type_cls_notimplemented")) != 0:
            if nargs != 0 or nkwargs != 0:
                py_raise_owned(py_exc_new(3, cstr("NotImplementedType takes no arguments")))
                return null()
            return global_load_ptr("py_NotImplemented")
        if ptr_eq(callable, global_load_ptr("pcc_type_cls_builtin_function")) != 0:
            py_raise_owned(py_exc_new(3, cstr("cannot create 'builtin_function_or_method' instances")))
            return null()
        if ptr_eq(callable, global_load_ptr("pcc_type_cls_function")) != 0:
            # A function is built from a code object; pcc compiles code ahead
            # of time and has no runtime code objects to build one from.
            if nargs == 0 and nkwargs == 0:
                py_raise_owned(py_exc_new(3, cstr("function() missing required argument 'code' (pos 1)")))
            else:
                py_raise_owned(py_exc_new(3, cstr("function() argument 'code' must be code")))
            return null()
        if ptr_eq(callable, global_load_ptr("pcc_type_cls_method")) != 0:
            return _method_type_call(args, nargs, nkwargs)
        descriptor_tag: int = py_builtin_type_class_tag(callable)
        if (descriptor_tag == PY_TYPE_STATICMETHOD
                or descriptor_tag == PY_TYPE_CLASSMETHOD
                or descriptor_tag == PY_TYPE_PROPERTY):
            return _descriptor_type_call(args, kwargs, descriptor_tag)
        if descriptor_tag == PY_TYPE_CLASS:
            return _type_object_call(args, nargs, nkwargs)
        # int, float, str, dict, bytes, bytearray and complex parse their
        # arguments in the leased constructor frame; bool, list, tuple and
        # set take at most one positional argument.
        if descriptor_tag == PY_TYPE_FLOAT or _constructor_tag(descriptor_tag) != 0:
            return _descriptor_type_call(args, kwargs, descriptor_tag)
        owner = null()
        if descriptor_tag == PY_TYPE_BOOL:
            owner = cstr("bool")
        elif descriptor_tag == PY_TYPE_LIST:
            owner = cstr("list")
        elif descriptor_tag == PY_TYPE_TUPLE:
            owner = cstr("tuple")
        elif descriptor_tag == PY_TYPE_SET:
            owner = cstr("set")
        if ptr_is_null(owner) == 0:
            if nkwargs != 0:
                keyword_message = stack_alloc(64)
                keyword_end: int = _cstr_append(keyword_message, 0, owner)
                keyword_end = _cstr_append(keyword_message, keyword_end, cstr("() takes no keyword arguments"))
                store_i8(keyword_message, keyword_end, 0)
                py_raise_owned(py_exc_new(3, keyword_message))
                return null()
            if nargs > 1:
                return _builtin_arity_error(owner, 1, nargs)
            arg = null()
            if nargs == 1:
                arg = py_tuple_get(args, 0)
            out = null()
            if descriptor_tag == PY_TYPE_BOOL:
                truth: int = 0
                if ptr_is_null(arg) == 0:
                    truth = py_obj_truthy(arg)
                out = py_bool_from_bit(truth)
            elif descriptor_tag == PY_TYPE_LIST:
                out = py_list_new(0)
                if ptr_is_null(out) == 0 and ptr_is_null(arg) == 0:
                    py_list_extend(out, arg)
                    if py_err_occurred() != 0:
                        py_decref(out)
                        out = null()
            elif descriptor_tag == PY_TYPE_SET:
                if ptr_is_null(arg) != 0:
                    out = py_set_new()
                else:
                    out = py_set_from_iterable(arg)
            else:
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
        exception_tag: int = _builtin_exception_class_tag(callable)
        if nkwargs != 0 and (exception_tag == 14 or (exception_tag >= 34 and exception_tag <= 48)):
            # Static table strings do not move while constructing the error.
            # The bounded builtin names plus this suffix fit in 96 bytes.
            name = load_ptr(global_addr("PY_EXC_BUILTIN_NAMES"), exception_tag * C_POINTER_SIZE)
            message = stack_alloc(96)
            end: int = _cstr_append(message, 0, name)
            end = _cstr_append(message, end, cstr("() takes no keyword arguments"))
            store_i8(message, end, 0)
            py_raise_owned(py_exc_new(3, message))
            return null()
        if nkwargs != 0 and exception_tag == 58:
            py_raise_owned(py_exc_new(3, cstr("UnicodeDecodeError() takes no keyword arguments")))
            return null()
        if nkwargs != 0 and exception_tag == 59:
            py_raise_owned(py_exc_new(3, cstr("UnicodeEncodeError() takes no keyword arguments")))
            return null()
        if nkwargs == 0 and exception_tag >= 0:
            return _builtin_exception_call(callable, args, nargs)
        # CPython: ``obj = cls.__new__(cls, *args)`` first.  Going straight
        # to py_instance_new skipped every user ``__new__``, so interning and
        # singleton classes handed back a fresh object each call -- and a
        # ``__new__`` that reads ``cls._cache`` ran against a class that had
        # never been passed in.
        if py_class_is_tuple_subclass(callable) != 0:
            return py_tuple_class_call(callable, args, kwargs)
        inst = _class_call_new(callable, args, kwargs)
        if ptr_is_null(inst) != 0:
            return null()
        if _instance_is_of_class(inst, callable) == 0:
            # ``__new__`` returned something else; CPython skips ``__init__``
            # and hands that object back untouched.
            return inst
        # py_class_mark_exception_subclass set this bit when the class was
        # created. A per-call MRO walk here (read barriers on every class
        # call) left test_classmethod_constructor_consumes_temporary_tuples
        # with undestroyed objects under GC4.
        if (load_i32(callable, PYOBJECTHEADER_FLAGS_OFFSET) & _EXCEPTION_SUBCLASS_CLASS_FLAG) != 0:
            inst = _exception_new_args(inst, args)
            if ptr_is_null(inst) != 0:
                return null()
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
        elif nkwargs != 0 and (load_i32(callable, PYOBJECTHEADER_FLAGS_OFFSET) & _EXCEPTION_SUBCLASS_CLASS_FLAG) != 0:
            # No class defines __init__, so BaseException.__init__ runs, and
            # it takes no keyword arguments.
            class_name = load_ptr(callable, PYCLASSOBJECT_NAME_OFFSET)
            if ptr_is_null(class_name) != 0:
                class_name = cstr("BaseException")
            text = malloc(strlen(class_name) + 32)
            if ptr_is_null(text) == 0:
                end: int = _cstr_append(text, 0, class_name)
                end = _cstr_append(text, end, cstr("() takes no keyword arguments"))
                store_i8(text, end, 0)
                py_raise_owned(py_exc_new(3, text))
                free(text)
            else:
                py_raise_owned(py_exc_new(3, cstr("BaseException.__init__() takes no keyword arguments")))
            py_decref(inst)
            return null()
        return inst
    if tag == PY_TYPE_FUNC:  # PY_TYPE_FUNC
        checked_result = py_func_call_kwargs(callable, args, kwargs)
        # Keep a fresh non-NULL result out of a polling diagnostic helper.
        if ptr_is_null(checked_result):
            py_runtime_error_if_unset(cstr('py_func_call_kwargs'), cstr('py_func_call_kwargs returned NULL without setting an exception'))
        return checked_result
    if tag == PY_TYPE_WEAKREF:  # PY_TYPE_WEAKREF
        checked_result = py_weakref_call(callable)
        # Keep a fresh non-NULL result out of a polling diagnostic helper.
        if ptr_is_null(checked_result):
            py_runtime_error_if_unset(cstr('py_weakref_call'), cstr('py_weakref_call returned NULL without setting an exception'))
        return checked_result
    if pcc_capi_is_cext_type_tag(tag) != 0:
        # Construction/extension invocation is itself a semantic callee,
        # unlike transparent staticmethod or instance __call__ dispatch.
        # Consume defer here so every internal ordinary call starts sync.
        if py_obj_call_context_is_deferred() != 0:
            if include_metaclass == 0:
                return py_obj_call_default_sync(callable, args, kwargs)
            return py_obj_call_sync(callable, args, kwargs)
        checked_result = pcc_capi_call_cext_object(callable, args, kwargs)
        # Keep a fresh non-NULL result out of a polling diagnostic helper.
        if ptr_is_null(checked_result):
            py_runtime_error_if_unset(cstr('pcc_capi_call_cext_object'), cstr('pcc_capi_call_cext_object returned NULL without setting an exception'))
        return checked_result
    if include_metaclass != 0 and _is_instance_tag(tag) != 0:
        cls = pcc_gc_load_ptr(
            callable, ptr_add(callable, PYINSTANCEOBJECT_CLS_OFFSET)
        )
        method = py_class_lookup(cls, cstr("__call__"))
        if ptr_is_null(method) == 0:
            checked_result = _dispatch_call_method_with_args(method, callable, args, kwargs)
            # Keep a fresh non-NULL result out of a polling diagnostic helper.
            if ptr_is_null(checked_result):
                py_runtime_error_if_unset(cstr('instance __call__'), cstr('instance __call__ returned NULL without setting an exception'))
            return checked_result
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
        # Internal callers use NULL for an uninitialized lazy class cache
        # (e.g. CodeType). Python None is a non-NULL object validated below.
        return 0
    if is_tagged_int(cls) != 0:
        py_raise_owned(py_exc_new(3, cstr("isinstance() arg 2 must be a type, a tuple of types, or a union")))
        return -1
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
        if pcc_capi_is_type_object_value(cls) != 0:
            # Keep the existing extension-class path separate from invalid
            # classinfo; native object identity is handled below.
            return 0
        py_raise_owned(py_exc_new(3, cstr("isinstance() arg 2 must be a type, a tuple of types, or a union")))
        return -1
    # Every Python value, including tagged integers and class objects, is an
    # object. Those values do not carry an instance-class/MRO payload.
    if ptr_eq(cls, global_load_ptr("py_object_root_cache")) != 0:
        return 1
    tag: int = _type_of(o)
    if ptr_eq(cls, global_load_ptr("pcc_type_cls_type")) != 0:
        if tag == PY_TYPE_CLASS:
            return 1
        return pcc_capi_is_type_object_value(o)
    if ptr_eq(cls, global_load_ptr("pcc_type_cls_ellipsis")) != 0:
        return 1 if tag == PY_TYPE_ELLIPSIS else 0
    if ptr_eq(cls, global_load_ptr("pcc_type_cls_notimplemented")) != 0:
        return 1 if tag == PY_TYPE_NOTIMPLEMENTED else 0
    if ptr_eq(cls, global_load_ptr("pcc_type_cls_function")) != 0:
        return 1 if tag == PY_TYPE_FUNC and py_func_kind(o) == 0 else 0
    if ptr_eq(cls, global_load_ptr("pcc_type_cls_builtin_function")) != 0:
        return 1 if tag == PY_TYPE_FUNC and py_func_kind(o) == 1 else 0
    if ptr_eq(cls, global_load_ptr("pcc_type_cls_method")) != 0:
        return 1 if tag == PY_TYPE_FUNC and py_func_kind(o) == 2 else 0
    if ptr_eq(cls, global_load_ptr("pcc_type_cls_staticmethod")) != 0:
        return 1 if tag == PY_TYPE_STATICMETHOD else 0
    if ptr_eq(cls, global_load_ptr("pcc_type_cls_classmethod")) != 0:
        return 1 if tag == PY_TYPE_CLASSMETHOD else 0
    if ptr_eq(cls, global_load_ptr("pcc_type_cls_property")) != 0:
        return 1 if tag == PY_TYPE_PROPERTY else 0
    if ptr_eq(cls, global_load_ptr("pcc_type_cls_bool")) != 0:
        return 1 if tag == PY_TYPE_BOOL else 0
    if ptr_eq(cls, global_load_ptr("pcc_type_cls_int")) != 0:
        return 1 if tag == PY_TYPE_BOOL or tag == PY_TYPE_INT else 0
    if ptr_eq(cls, global_load_ptr("pcc_type_cls_float")) != 0:
        return 1 if tag == PY_TYPE_FLOAT else 0
    if ptr_eq(cls, global_load_ptr("pcc_type_cls_str")) != 0:
        return py_str_check(o)
    if ptr_eq(cls, global_load_ptr("pcc_type_cls_list")) != 0:
        return 1 if tag == PY_TYPE_LIST else 0
    if ptr_eq(cls, global_load_ptr("pcc_type_cls_dict")) != 0:
        return 1 if tag == PY_TYPE_DICT else 0
    if ptr_eq(cls, global_load_ptr("pcc_type_cls_tuple")) != 0:
        return 1 if tag == PY_TYPE_TUPLE else 0
    # The remaining value classes match their own tag; anything else (a user
    # subclass instance, say) keeps the MRO check below.
    cls_tag: int = py_builtin_type_class_tag(cls)
    if cls_tag == PY_TYPE_COMPLEX or cls_tag == PY_TYPE_SET or cls_tag == PY_TYPE_BYTES:
        if tag == cls_tag:
            return 1
    elif cls_tag == PY_TYPE_BYTEARRAY or cls_tag == PY_TYPE_MEMORYVIEW or cls_tag == PY_TYPE_NONE:
        if tag == cls_tag:
            return 1
    pcc_diagnostics_runtime_log_event_code(7, 9, _type_of(o), _type_of(cls), o)
    return py_isinstance(o, cls)


@c_abi_export("py_obj_issubclass")
def py_obj_issubclass(derived, cls) -> int:
    # An empty tuple has no candidate to validate, even for an invalid first
    # argument. Nonempty/nested tuples validate in their short-circuit order.
    if ptr_is_null(cls) == 0 and is_tagged_int(cls) == 0:
        if load_i32(cls, 8) == PY_TYPE_TUPLE:
            count: int = py_tuple_len(cls)
            index: int = 0
            while index < count:
                candidate = py_tuple_get(cls, index)
                matched: int = py_obj_issubclass(derived, candidate)
                py_decref(candidate)
                if matched != 0:
                    return matched
                index += 1
            return 0
    if ptr_is_null(derived) != 0 or is_tagged_int(derived) != 0:
        py_raise_owned(py_exc_new(3, cstr("issubclass() arg 1 must be a class")))
        return -1
    derived_is_capi_type: int = pcc_capi_is_type_object_value(derived)
    if derived_is_capi_type == 0 and load_i32(derived, 8) != PY_TYPE_CLASS:  # PY_TYPE_CLASS
        py_raise_owned(py_exc_new(3, cstr("issubclass() arg 1 must be a class")))
        return -1
    if ptr_is_null(cls) != 0 or is_tagged_int(cls) != 0:
        py_raise_owned(py_exc_new(3, cstr("issubclass() arg 2 must be a class, a tuple of classes, or a union")))
        return -1
    cls_is_capi_type: int = pcc_capi_is_type_object_value(cls)
    if cls_is_capi_type == 0 and load_i32(cls, 8) != PY_TYPE_CLASS:  # PY_TYPE_CLASS
        py_raise_owned(py_exc_new(3, cstr("issubclass() arg 2 must be a class, a tuple of classes, or a union")))
        return -1
    # Validate derived before this universal match, including extension types.
    if ptr_eq(cls, global_load_ptr("py_object_root_cache")) != 0:
        return 1
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
