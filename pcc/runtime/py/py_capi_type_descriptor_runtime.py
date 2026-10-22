"""pcc-Python owners for the type-descriptor surface.

Replaces the pcc_capi_type_object_getattr + the method/getset/member/richcompare
descriptor constructors + their call entries + caches block of
py_capi_shim.c.  Type-level attribute lookup returns descriptor objects
(py_func_named carriers) so extension documentation installers such as
numpy.add_docstring work.

Cache layout (fixed arrays): method descriptors store {method ptr, descriptor};
data descriptors (getset/member) store {definition ptr, descriptor};
richcompare stores {slot ptr, op, descriptor}.  All are pcc-Python globals.

Owned surface (stable C ABI names):

  pcc_capi_type_object_getattr, pcc_capi_unbound_method_call_entry,
  pcc_capi_data_descriptor_call_entry, pcc_capi_richcompare_descriptor_call_entry

Public object type tags come from the generated ``py_abi_constants`` module.
Method flags and private exception codes remain owned by this descriptor bridge.
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PY_TYPE_TUPLE

from pcc.extern import c_abi_typed_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    call_ptr2,
    call_ptr3,
    call_ptr_ptr_ptr_i32,
    cstr,
    define_global_i64_array,
    define_global_null_ptr_array,
    function_addr,
    global_addr,
    global_load_ptr,
    int_to_ptr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_i8,
    load_ptr,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    ptr_to_int,
    store_i32,
    store_i64,
    store_ptr,
)

py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_get = extern("py_tuple_get", (c_ptr, c_int64), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_tuple_len = extern("py_tuple_len", (c_ptr,), c_int64)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_int_value_i64 = extern("py_int_value_i64", (c_ptr,), c_int64)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_runtime_error_if_unset = extern(
    "py_runtime_error_if_unset", (c_ptr, c_ptr), c_ptr
)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
py_func_new_named = extern("py_func_new_named", (c_ptr, c_ptr, c_ptr), c_ptr)
pcc_capi_is_type_object = extern("pcc_capi_is_type_object", (c_ptr,), c_int64)
pcc_capi_cext_type_for_object = extern("pcc_capi_cext_type_for_object", (c_ptr,), c_ptr)

# caches: method {method, descriptor} x512, data {definition, descriptor} x512,
# richcompare {slot, op, descriptor} x256 — stored as flat word arrays + counts
define_global_null_ptr_array("pcc_capi_method_descriptors", 1024)
define_global_null_ptr_array("pcc_capi_getset_descriptors", 1024)
define_global_null_ptr_array("pcc_capi_member_descriptors", 1024)
define_global_null_ptr_array("pcc_capi_richcompare_descriptors", 768)

def _type_error(message) -> None:
    py_raise_owned(py_exc_new(3, message))  # PY_EXC_TYPEERROR


def _runtime_error(message) -> None:
    py_raise_owned(py_exc_new(7, message))  # PY_EXC_RUNTIMEERROR (6 is AttributeError)


def _descriptor_require_result(result):
    if ptr_is_null(result):
        py_runtime_error_if_unset(
            cstr("pcc_capi_unbound_method_call_entry"),
            cstr("C extension method returned NULL without setting an exception"),
        )
    return result


def _cstr_eq(a, b) -> int:
    i: int = 0
    while True:
        ca: int = load_i8(a, i)
        cb: int = load_i8(b, i)
        if ca != cb:
            return 0
        if ca == 0:
            return 1
        i += 1


def _strrchr_dot(s) -> int:
    last: int = -1
    i: int = 0
    while True:
        c: int = load_i8(s, i)
        if c == 0:
            break
        if c == 46:  # '.'
            last = i
        i += 1
    return last


# --- call entries ----------------------------------------------------

@c_abi_typed_export("pcc_capi_unbound_method_call_entry", "ptr", ("ptr", "ptr"))
def pcc_capi_unbound_method_call_entry(captures, args) -> c_ptr:
    if ptr_is_null(captures) or is_tagged_int(captures) or load_i32(captures, 8) != PY_TYPE_TUPLE:
        _type_error(cstr("invalid C method descriptor call"))
        return null()
    if py_tuple_len(captures) != 2:
        _type_error(cstr("invalid C method descriptor call"))
        return null()
    if ptr_is_null(args) or is_tagged_int(args) or load_i32(args, 8) != PY_TYPE_TUPLE:
        _type_error(cstr("invalid C method descriptor call"))
        return null()
    nargs = py_tuple_len(args)
    if nargs < 1:
        _type_error(cstr("unbound C method requires an instance"))
        return null()
    method_ptr_obj = py_tuple_get(captures, 0)
    flags_obj = py_tuple_get(captures, 1)
    self = py_tuple_get(args, 0)
    if ptr_is_null(method_ptr_obj) or ptr_is_null(flags_obj) or ptr_is_null(self):
        _type_error(cstr("invalid C method descriptor"))
        return null()
    method_int = py_int_value_i64(method_ptr_obj)
    flags: int = py_int_value_i64(flags_obj)
    result = null()
    if method_int == 0:
        _type_error(cstr("invalid C method descriptor"))
        method = null()
    else:
        method = int_to_ptr(method_int)
    if not ptr_is_null(method) and ptr_is_null(load_ptr(method, 8)):  # ml_meth
        _type_error(cstr("invalid C method descriptor"))
    elif (flags & (0x0001)) != 0:
        if nargs != 1:
            _type_error(cstr("method takes no arguments"))
        else:
            result = _descriptor_require_result(
                call_ptr2(load_ptr(method, 8), self, null())
            )
    elif (flags & (0x0008)) != 0:
        if nargs != 2:
            _type_error(cstr("method takes exactly one argument"))
        else:
            arg = py_tuple_get(args, 1)
            result = _descriptor_require_result(
                call_ptr2(load_ptr(method, 8), self, arg)
            )
            py_decref(arg)
    elif (flags & (0x0001)) != 0:
        call_args = py_tuple_new(nargs - 1)
        if not ptr_is_null(call_args):
            i: int = 1
            while i < nargs:
                arg = py_tuple_get(args, i)
                if ptr_is_null(arg):
                    py_decref(call_args)
                    call_args = null()
                    break
                py_tuple_set_item(call_args, i - 1, arg)
                i += 1
        if not ptr_is_null(call_args):
            if (flags & (0x0002)) != 0:
                result = _descriptor_require_result(
                    call_ptr3(load_ptr(method, 8), self, call_args, null())
                )
            else:
                result = _descriptor_require_result(
                    call_ptr2(load_ptr(method, 8), self, call_args)
                )
            py_decref(call_args)
    else:
        _type_error(cstr("unsupported C method flags"))
    # py_tuple_get returns owned references; release the ones we took.
    py_decref(method_ptr_obj)
    py_decref(flags_obj)
    py_decref(self)
    return result


@c_abi_typed_export("pcc_capi_data_descriptor_call_entry", "ptr", ("ptr", "ptr"))
def pcc_capi_data_descriptor_call_entry(captures, args) -> c_ptr:
    _type_error(cstr("attribute descriptor is not callable"))
    return null()


@c_abi_typed_export("pcc_capi_richcompare_descriptor_call_entry", "ptr", ("ptr", "ptr"))
def pcc_capi_richcompare_descriptor_call_entry(captures, args) -> c_ptr:
    if ptr_is_null(captures) or is_tagged_int(captures) or load_i32(captures, 8) != PY_TYPE_TUPLE:
        _type_error(cstr("invalid rich comparison descriptor call"))
        return null()
    if py_tuple_len(captures) != 2:
        _type_error(cstr("invalid rich comparison descriptor call"))
        return null()
    if ptr_is_null(args) or is_tagged_int(args) or load_i32(args, 8) != PY_TYPE_TUPLE:
        _type_error(cstr("invalid rich comparison descriptor call"))
        return null()
    if py_tuple_len(args) != 2:
        _type_error(cstr("invalid rich comparison descriptor call"))
        return null()
    fn_ptr_obj = py_tuple_get(captures, 0)
    op_obj = py_tuple_get(captures, 1)
    lhs = py_tuple_get(args, 0)
    rhs = py_tuple_get(args, 1)
    if ptr_is_null(fn_ptr_obj) or ptr_is_null(op_obj) or ptr_is_null(lhs) or ptr_is_null(rhs):
        _type_error(cstr("invalid rich comparison descriptor"))
        return null()
    richcompare_int = py_int_value_i64(fn_ptr_obj)
    op: int = py_int_value_i64(op_obj)
    if richcompare_int == 0:
        _type_error(cstr("invalid rich comparison function"))
        return null()
    result = call_ptr_ptr_ptr_i32(int_to_ptr(richcompare_int), lhs, rhs, op)
    py_decref(fn_ptr_obj)
    py_decref(op_obj)
    py_decref(lhs)
    py_decref(rhs)
    return result


# --- descriptor constructors -----------------------------------------

def _method_descriptor(method) -> c_ptr:
    if ptr_is_null(method) or ptr_is_null(load_ptr(method, 8)):
        return null()
    cache = global_addr("pcc_capi_method_descriptors")
    i: int = 0
    while i < (512):
        entry_method = load_ptr(cache, i * 16)
        if ptr_eq(entry_method, method):
            descriptor = load_ptr(cache, i * 16 + 8)
            py_incref(descriptor)
            return descriptor
        if ptr_is_null(entry_method):
            break
        i += 1
    if i >= (512):
        _runtime_error(cstr("C method descriptor cache exhausted"))
        return null()
    captures = py_tuple_new(2)
    method_ptr = py_int_from_i64(ptr_to_int(method))
    flags = py_int_from_i64(load_i64(method, 16))  # ml_flags
    if ptr_is_null(captures) or ptr_is_null(method_ptr) or ptr_is_null(flags):
        py_decref(captures)
        py_decref(method_ptr)
        py_decref(flags)
        return null()
    py_tuple_set_item(captures, 0, method_ptr)
    py_tuple_set_item(captures, 1, flags)
    py_decref(method_ptr)
    py_decref(flags)
    descriptor = py_func_new_named(
        function_addr("pcc_capi_unbound_method_call_entry"), captures, load_ptr(method, 0)
    )
    py_decref(captures)
    if ptr_is_null(descriptor):
        return null()
    pcc_gc_pin(descriptor)
    store_ptr(cache, i * 16, method)
    store_ptr(cache, i * 16 + 8, descriptor)
    py_incref(descriptor)
    return descriptor


def _data_descriptor(definition, name, is_getset: int) -> c_ptr:
    if ptr_is_null(definition) or ptr_is_null(name):
        return null()
    if is_getset != 0:
        cache = global_addr("pcc_capi_getset_descriptors")
    else:
        cache = global_addr("pcc_capi_member_descriptors")
    i: int = 0
    while i < (512):
        entry_def = load_ptr(cache, i * 16)
        if ptr_eq(entry_def, definition):
            descriptor = load_ptr(cache, i * 16 + 8)
            py_incref(descriptor)
            return descriptor
        if ptr_is_null(entry_def):
            break
        i += 1
    if i >= (512):
        _runtime_error(cstr("C data descriptor cache exhausted"))
        return null()
    descriptor = py_func_new_named(
        function_addr("pcc_capi_data_descriptor_call_entry"), global_load_ptr("py_None"), name
    )
    if ptr_is_null(descriptor):
        return null()
    pcc_gc_pin(descriptor)
    store_ptr(cache, i * 16, definition)
    store_ptr(cache, i * 16 + 8, descriptor)
    py_incref(descriptor)
    return descriptor


def _richcompare_op(name) -> int:
    if _cstr_eq(name, cstr("__lt__")) != 0:
        return 0
    if _cstr_eq(name, cstr("__le__")) != 0:
        return 1
    if _cstr_eq(name, cstr("__eq__")) != 0:
        return 2
    if _cstr_eq(name, cstr("__ne__")) != 0:
        return 3
    if _cstr_eq(name, cstr("__gt__")) != 0:
        return 4
    if _cstr_eq(name, cstr("__ge__")) != 0:
        return 5
    return -1


def _richcompare_descriptor(type_obj, name) -> c_ptr:
    op = _richcompare_op(name)
    if ptr_is_null(type_obj) or op < 0:
        return null()
    slot = load_ptr(type_obj, (208))
    if ptr_is_null(slot):
        return null()
    cache = global_addr("pcc_capi_richcompare_descriptors")
    i: int = 0
    while i < 256:
        entry_slot = load_ptr(cache, i * 24)
        if ptr_eq(entry_slot, slot) and load_i64(cache, i * 24 + 8) == op:
            descriptor = load_ptr(cache, i * 24 + 16)
            py_incref(descriptor)
            return descriptor
        if ptr_is_null(entry_slot):
            break
        i += 1
    if i >= 256:
        _runtime_error(cstr("rich comparison descriptor cache exhausted"))
        return null()
    captures = py_tuple_new(2)
    fn_ptr = py_int_from_i64(ptr_to_int(slot))
    op_obj = py_int_from_i64(op)
    if ptr_is_null(captures) or ptr_is_null(fn_ptr) or ptr_is_null(op_obj):
        py_decref(captures)
        py_decref(fn_ptr)
        py_decref(op_obj)
        return null()
    py_tuple_set_item(captures, 0, fn_ptr)
    py_tuple_set_item(captures, 1, op_obj)
    py_decref(fn_ptr)
    py_decref(op_obj)
    descriptor = py_func_new_named(
        function_addr("pcc_capi_richcompare_descriptor_call_entry"), captures, name
    )
    py_decref(captures)
    if ptr_is_null(descriptor):
        return null()
    pcc_gc_pin(descriptor)
    store_ptr(cache, i * 24, slot)
    store_i64(cache, i * 24 + 8, op)
    store_ptr(cache, i * 24 + 16, descriptor)
    py_incref(descriptor)
    return descriptor


# --- the attribute walk ----------------------------------------------

@c_abi_typed_export("pcc_capi_type_object_getattr", "ptr", ("ptr", "ptr"))
def pcc_capi_type_object_getattr(type_object, name) -> c_ptr:
    if pcc_capi_is_type_object(type_object) == 0 or ptr_is_null(name):
        return null()
    if _cstr_eq(name, cstr("__name__")) != 0:
        qualified = load_ptr(type_object, (32))
        if ptr_is_null(qualified):
            qualified = cstr("")
        short_at = _strrchr_dot(qualified)
        if short_at >= 0:
            short_name = ptr_add(qualified, short_at + 1)
        else:
            short_name = qualified
        return py_str_new(short_name, _cstr_len(short_name))
    if _cstr_eq(name, cstr("__module__")) != 0:
        qualified = load_ptr(type_object, (32))
        if ptr_is_null(qualified):
            qualified = cstr("")
        sep = _strrchr_dot(qualified)
        if sep < 0:
            return py_str_new(cstr("builtins"), 8)
        return py_str_new(qualified, sep)
    current = type_object
    while not ptr_is_null(current):
        getset = load_ptr(current, (256))
        while not ptr_is_null(getset) and not ptr_is_null(load_ptr(getset, 0)):
            gs_name = load_ptr(getset, 0)
            if _cstr_eq(name, gs_name) != 0:
                return _data_descriptor(getset, gs_name, 1)
            getset = ptr_add(getset, 40)
        member = load_ptr(current, (248))
        while not ptr_is_null(member) and not ptr_is_null(load_ptr(member, 0)):
            m_name = load_ptr(member, 0)
            if _cstr_eq(name, m_name) != 0:
                return _data_descriptor(member, m_name, 0)
            member = ptr_add(member, 40)
        methods = load_ptr(current, (240))
        method = methods
        while not ptr_is_null(method) and not ptr_is_null(load_ptr(method, 0)):
            m_name = load_ptr(method, 0)
            if _cstr_eq(name, m_name) != 0:
                return _method_descriptor(method)
            method = ptr_add(method, 32)
        if not ptr_is_null(load_ptr(current, (208))):
            descriptor = _richcompare_descriptor(current, name)
            if not ptr_is_null(descriptor) or py_err_occurred() != 0:
                return descriptor
        current = load_ptr(current, (264))
    return null()


def _cstr_len(s) -> int:
    i: int = 0
    while load_i8(s, i) != 0:
        i += 1
    return i
