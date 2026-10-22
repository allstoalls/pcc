"""pcc-Python owners for the no-libpython C-API exception surface.

This module replaces the C py_capi_shim.c block that defined the PyExc_*
singleton data symbols, the exception-tag mapping, every PyErr_* error
function, and the printf-style message formatter shared with
PyUnicode_FromFormat.  It is a semantic pcc-Python module (PY_MODULES), not
a freestanding one: it consumes the managed exception, class, str, and
refcount ABIs and performs raw layout reads only for the fixed
PyExceptionObject/PyObjectHeader fields.

Owned surface (stable C ABI names):

  data:  PyExc_* (33 singletons), Py_Ellipsis
  funcs: pcc_capi_exception_tag, pcc_capi_exception_class,
         PyErr_SetString, PyErr_SetNone, PyErr_SetObject, PyErr_NoMemory,
         PyErr_BadInternalCall, PyErr_Occurred, PyErr_Clear,
         PyErr_GivenExceptionMatches, PyErr_ExceptionMatches, PyErr_Fetch,
         PyErr_Restore, PyErr_NewException, PyErr_WarnEx, PyErr_WarnFormat,
         PyErr_WriteUnraisable, PyErr_Print, PyErr_CheckSignals,
         PyErr_Format, PyErr_FormatV, PyErr_NormalizeException,
         PyUnicode_FromFormat, PyUnicode_FromFormatV

The errno-driven pair PyErr_SetFromErrno / PyErr_SetFromErrnoWithFilenameObject
is owned by py_capi_misc_runtime; freestanding_errno owns native thread errno
and target-specific message formatting without a production C helper.

Public object type tags come from the generated ``py_abi_constants`` module.
Exception-table codes and exception-object payload layout remain owned here:
  PY_EXC_BASE = 0 ... PY_EXC_MODULENOTFOUNDERROR = 21 (py_runtime.h enum)
  PyExceptionObject.exc_class lives at offset 16.
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PY_TYPE_CLASS, PY_TYPE_EXC, PY_TYPE_STR, PY_TYPE_TUPLE

from pcc.extern import (
    c_abi_typed_export,
    c_abi_variadic_export,
    c_double,
    c_int32,
    c_int64,
    c_ptr,
    c_void,
    extern,
)
from pcc.unsafe import (
    cstr,
    define_global_i32,
    define_global_ptr_to_global,
    free,
    global_load_ptr,
    global_addr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    load_i8,
    malloc,
    memcpy,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    ptr_to_int,
    stack_alloc,
    store_i8,
    store_ptr,
    strlen,
    unsigned_div_i64,
    unsigned_greater_i64,
    unsigned_rem_i64,
    va_arg_f64,
    va_arg_i32,
    va_arg_i64,
    va_arg_ptr,
    va_arg_u32,
    va_cursor,
    va_end,
    va_start,
    wrapping_mul_i64,
)

py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
define_global_i32("pcc_capi_unicode_string_owned_map", 1)
py_unicode_decode_error_normalize = extern("py_unicode_decode_error_normalize", (c_ptr,), c_ptr)
py_unicode_encode_error_normalize = extern("py_unicode_encode_error_normalize", (c_ptr,), c_ptr)
py_exc_new_with_value = extern("py_exc_new_with_value", (c_int64, c_ptr), c_ptr)
py_exc_new_with_class = extern("py_exc_new_with_class", (c_ptr, c_ptr), c_ptr)
py_exc_builtin_class = extern("py_exc_builtin_class", (c_int64,), c_ptr)
py_exc_matches = extern("py_exc_matches", (c_ptr, c_ptr), c_int64)
py_current_exception = extern("py_current_exception", (), c_ptr)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_exc_print_unhandled = extern("py_exc_print_unhandled", (c_ptr,), c_void)
py_obj_str = extern("py_obj_str", (c_ptr,), c_ptr)
py_obj_repr = extern("py_obj_repr", (c_ptr,), c_ptr)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_str_byte_len = extern("py_str_byte_len", (c_ptr,), c_int64)
py_class_new = extern("py_class_new", (c_ptr, c_ptr, c_int32, c_ptr, c_int32), c_ptr)
py_tuple_len = extern("py_tuple_len", (c_ptr,), c_int64)
py_tuple_get = extern("py_tuple_get", (c_ptr, c_int64), c_ptr)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_stdio_format_float_raw = extern(
    "pcc_stdio_format_float_raw",
    (c_ptr, c_double, c_int64, c_int64, c_int64, c_int64, c_int64),
    c_int64,
)
pcc_stdio_float_raw_capacity = extern(
    "pcc_stdio_float_raw_capacity", (c_int64,), c_int64
)

# --- PyExc_* singleton data symbols -------------------------------------
# Mirror of py_capi_shim.c: each PyExc_* is a linker-visible pointer global
# whose value is the address of a stable sentinel int.  C extensions compare
# `PyErr_ExceptionMatches(PyExc_ValueError)` / pass PyExc_* into error
# setters, so the addresses must be stable and distinct per class.  IOError
# deliberately aliases the OSError sentinel (CPython aliases the type).
define_global_i32("pcc_capi_value_error_sentinel", 0)
define_global_i32("pcc_capi_type_error_sentinel", 0)
define_global_i32("pcc_capi_runtime_error_sentinel", 0)
define_global_i32("pcc_capi_key_error_sentinel", 0)
define_global_i32("pcc_capi_index_error_sentinel", 0)
define_global_i32("pcc_capi_attribute_error_sentinel", 0)
define_global_i32("pcc_capi_memory_error_sentinel", 0)
define_global_i32("pcc_capi_overflow_error_sentinel", 0)
define_global_i32("pcc_capi_system_error_sentinel", 0)
define_global_i32("pcc_capi_name_error_sentinel", 0)
define_global_i32("pcc_capi_notimplemented_error_sentinel", 0)
define_global_i32("pcc_capi_base_exception_sentinel", 0)
define_global_i32("pcc_capi_exception_sentinel", 0)
define_global_i32("pcc_capi_arithmetic_error_sentinel", 0)
define_global_i32("pcc_capi_lookup_error_sentinel", 0)
define_global_i32("pcc_capi_os_error_sentinel", 0)
define_global_i32("pcc_capi_assertion_error_sentinel", 0)
define_global_i32("pcc_capi_stop_iteration_sentinel", 0)
define_global_i32("pcc_capi_stop_async_iteration_sentinel", 0)
define_global_i32("pcc_capi_zero_division_error_sentinel", 0)
define_global_i32("pcc_capi_reference_error_sentinel", 0)
define_global_i32("pcc_capi_buffer_error_sentinel", 0)
define_global_i32("pcc_capi_import_error_sentinel", 0)
define_global_i32("pcc_capi_module_not_found_error_sentinel", 0)
define_global_i32("pcc_capi_import_warning_sentinel", 0)
define_global_i32("pcc_capi_floating_point_error_sentinel", 0)
define_global_i32("pcc_capi_recursion_error_sentinel", 0)
define_global_i32("pcc_capi_unicode_decode_error_sentinel", 0)
define_global_i32("pcc_capi_unicode_encode_error_sentinel", 0)
define_global_i32("pcc_capi_unicode_error_sentinel", 0)
define_global_i32("pcc_capi_warning_sentinel", 0)
define_global_i32("pcc_capi_user_warning_sentinel", 0)
define_global_i32("pcc_capi_runtime_warning_sentinel", 0)
define_global_i32("pcc_capi_deprecation_warning_sentinel", 0)
define_global_i32("pcc_capi_future_warning_sentinel", 0)

define_global_ptr_to_global("PyExc_ValueError", "pcc_capi_value_error_sentinel")
define_global_ptr_to_global("PyExc_TypeError", "pcc_capi_type_error_sentinel")
define_global_ptr_to_global("PyExc_RuntimeError", "pcc_capi_runtime_error_sentinel")
define_global_ptr_to_global("PyExc_KeyError", "pcc_capi_key_error_sentinel")
define_global_ptr_to_global("PyExc_IndexError", "pcc_capi_index_error_sentinel")
define_global_ptr_to_global("PyExc_AttributeError", "pcc_capi_attribute_error_sentinel")
define_global_ptr_to_global("PyExc_MemoryError", "pcc_capi_memory_error_sentinel")
define_global_ptr_to_global("PyExc_OverflowError", "pcc_capi_overflow_error_sentinel")
define_global_ptr_to_global("PyExc_SystemError", "pcc_capi_system_error_sentinel")
define_global_ptr_to_global("PyExc_NameError", "pcc_capi_name_error_sentinel")
define_global_ptr_to_global("PyExc_NotImplementedError", "pcc_capi_notimplemented_error_sentinel")
define_global_ptr_to_global("PyExc_BaseException", "pcc_capi_base_exception_sentinel")
define_global_ptr_to_global("PyExc_Exception", "pcc_capi_exception_sentinel")
define_global_ptr_to_global("PyExc_ArithmeticError", "pcc_capi_arithmetic_error_sentinel")
define_global_ptr_to_global("PyExc_LookupError", "pcc_capi_lookup_error_sentinel")
define_global_ptr_to_global("PyExc_OSError", "pcc_capi_os_error_sentinel")
define_global_ptr_to_global("PyExc_IOError", "pcc_capi_os_error_sentinel")
define_global_ptr_to_global("PyExc_AssertionError", "pcc_capi_assertion_error_sentinel")
define_global_ptr_to_global("PyExc_StopIteration", "pcc_capi_stop_iteration_sentinel")
define_global_ptr_to_global("PyExc_StopAsyncIteration", "pcc_capi_stop_async_iteration_sentinel")
define_global_ptr_to_global("PyExc_ZeroDivisionError", "pcc_capi_zero_division_error_sentinel")
define_global_ptr_to_global("PyExc_ReferenceError", "pcc_capi_reference_error_sentinel")
define_global_ptr_to_global("PyExc_BufferError", "pcc_capi_buffer_error_sentinel")
define_global_ptr_to_global("PyExc_ImportError", "pcc_capi_import_error_sentinel")
define_global_ptr_to_global("PyExc_ModuleNotFoundError", "pcc_capi_module_not_found_error_sentinel")
define_global_ptr_to_global("PyExc_ImportWarning", "pcc_capi_import_warning_sentinel")
define_global_ptr_to_global("PyExc_FloatingPointError", "pcc_capi_floating_point_error_sentinel")
define_global_ptr_to_global("PyExc_RecursionError", "pcc_capi_recursion_error_sentinel")
define_global_ptr_to_global("PyExc_UnicodeDecodeError", "pcc_capi_unicode_decode_error_sentinel")
define_global_ptr_to_global("PyExc_UnicodeEncodeError", "pcc_capi_unicode_encode_error_sentinel")
define_global_ptr_to_global("PyExc_UnicodeError", "pcc_capi_unicode_error_sentinel")
define_global_ptr_to_global("PyExc_Warning", "pcc_capi_warning_sentinel")
define_global_ptr_to_global("PyExc_UserWarning", "pcc_capi_user_warning_sentinel")
define_global_ptr_to_global("PyExc_RuntimeWarning", "pcc_capi_runtime_warning_sentinel")
define_global_ptr_to_global("PyExc_DeprecationWarning", "pcc_capi_deprecation_warning_sentinel")
define_global_ptr_to_global("PyExc_FutureWarning", "pcc_capi_future_warning_sentinel")
# Native C-API users observe the same valid object as compiled Python.
define_global_ptr_to_global("Py_Ellipsis", "py_ellipsis_storage")



@c_abi_typed_export("pcc_capi_exception_tag", "i32", ("ptr",))
def pcc_capi_exception_tag(type) -> int:
    if ptr_eq(type, global_load_ptr("PyExc_BaseException")):
        return 0
    if ptr_eq(type, global_load_ptr("PyExc_Exception")):
        return 1
    if ptr_eq(type, global_load_ptr("PyExc_ValueError")):
        return 2
    if ptr_eq(type, global_load_ptr("PyExc_TypeError")):
        return 3
    if ptr_eq(type, global_load_ptr("PyExc_KeyError")):
        return 4
    if ptr_eq(type, global_load_ptr("PyExc_IndexError")):
        return 5
    if ptr_eq(type, global_load_ptr("PyExc_AttributeError")):
        return 6
    if ptr_eq(type, global_load_ptr("PyExc_RuntimeError")) or ptr_eq(
        type, global_load_ptr("PyExc_SystemError")
    ) or ptr_eq(type, global_load_ptr("PyExc_RecursionError")):
        return 7
    if ptr_eq(type, global_load_ptr("PyExc_StopIteration")):
        return 8
    if ptr_eq(type, global_load_ptr("PyExc_ZeroDivisionError")):
        return 9
    if ptr_eq(type, global_load_ptr("PyExc_NameError")):
        return 10
    if ptr_eq(type, global_load_ptr("PyExc_NotImplementedError")):
        return 11
    if ptr_eq(type, global_load_ptr("PyExc_ArithmeticError")) or ptr_eq(
        type, global_load_ptr("PyExc_FloatingPointError")
    ):
        return 12
    if ptr_eq(type, global_load_ptr("PyExc_LookupError")):
        return 13
    if ptr_eq(type, global_load_ptr("PyExc_OSError")) or ptr_eq(
        type, global_load_ptr("PyExc_IOError")
    ):
        return 14
    if ptr_eq(type, global_load_ptr("PyExc_OverflowError")):
        return 15
    if ptr_eq(type, global_load_ptr("PyExc_AssertionError")):
        return 16
    if ptr_eq(type, global_load_ptr("PyExc_StopAsyncIteration")):
        return 17
    if ptr_eq(type, global_load_ptr("PyExc_ReferenceError")):
        return 18
    if ptr_eq(type, global_load_ptr("PyExc_MemoryError")):
        return 19
    if ptr_eq(type, global_load_ptr("PyExc_UnicodeError")):
        return 57
    if ptr_eq(type, global_load_ptr("PyExc_UnicodeDecodeError")):
        return 58
    if ptr_eq(type, global_load_ptr("PyExc_UnicodeEncodeError")):
        return 59
    if ptr_eq(type, global_load_ptr("PyExc_ImportError")):
        return 20
    if ptr_eq(type, global_load_ptr("PyExc_ModuleNotFoundError")):
        return 21
    return 1  # PY_EXC_EXCEPTION


# These helpers share the runtime's implicit pointer ABI with their callers.
# The type may be a raw PyExc sentinel or a managed class, and value is a
# manually rooted PyObject*. A physical C-ABI ptr does not establish the
# explicit c_ptr view required by raw-pointer-annotated Python parameters.
def _capi_is_unicode_sentinel(type) -> int:
    if ptr_eq(type, global_load_ptr("PyExc_UnicodeError")):
        return 1
    if ptr_eq(type, global_load_ptr("PyExc_UnicodeDecodeError")):
        return 1
    return ptr_eq(type, global_load_ptr("PyExc_UnicodeEncodeError"))


@c_abi_typed_export("pcc_capi_exception_class", "ptr", ("ptr",))
def pcc_capi_exception_class(type):
    if ptr_is_null(type):
        return null()
    if _capi_is_unicode_sentinel(type) != 0:
        return py_exc_builtin_class(pcc_capi_exception_tag(type))
    if not is_tagged_int(type) and load_i32(type, 8) == PY_TYPE_CLASS:  # PY_TYPE_CLASS
        return type
    return py_exc_builtin_class(pcc_capi_exception_tag(type))


def _capi_is_unicode_encode_type(type) -> int:
    if ptr_eq(type, global_load_ptr("PyExc_UnicodeEncodeError")):
        return 1
    # Sentinel symbols are raw addresses, not managed objects with headers.
    # A direct builtin class can only exist after its canonical cache entry.
    cached = load_ptr(global_addr("py_exc_classes"), 59 * 8)
    return 0 if ptr_is_null(cached) else ptr_eq(type, cached)


def _capi_is_unicode_decode_type(type) -> int:
    if ptr_eq(type, global_load_ptr("PyExc_UnicodeDecodeError")):
        return 1
    cached = load_ptr(global_addr("py_exc_classes"), 58 * 8)
    return 0 if ptr_is_null(cached) else ptr_eq(type, cached)


def _capi_set_unicode_decode_value(value) -> None:
    error = py_unicode_decode_error_normalize(value)
    if ptr_is_null(error) == 0:
        py_raise_owned(error)


def _capi_set_unicode_encode_value(value) -> None:
    error = py_unicode_encode_error_normalize(value)
    # Failed normalization already installed its TypeError/OverflowError.
    # Raising NULL here would replace that with a spurious RuntimeError.
    if ptr_is_null(error) == 0:
        py_raise_owned(error)


@c_abi_typed_export("PyErr_SetString", "void", ("ptr", "ptr"))
def PyErr_SetString(type, message) -> None:
    if ptr_is_null(message):
        message = cstr("")
    if _capi_is_unicode_encode_type(type) != 0:
        owned = stack_alloc(8)
        store_ptr(owned, 0, null())
        pcc_gc_frame_enter(global_addr("pcc_capi_unicode_string_owned_map"), owned)
        store_ptr(owned, 0, py_str_new(message, strlen(message)))
        if ptr_is_null(load_ptr(owned, 0)) == 0:
            _capi_set_unicode_encode_value(pcc_gc_load_ptr(null(), owned))
        pcc_gc_store_root(owned, null())
        pcc_gc_frame_leave(owned)
        return
    if _capi_is_unicode_decode_type(type) != 0:
        owned = stack_alloc(8)
        store_ptr(owned, 0, null())
        pcc_gc_frame_enter(global_addr("pcc_capi_unicode_string_owned_map"), owned)
        store_ptr(owned, 0, py_str_new(message, strlen(message)))
        if ptr_is_null(load_ptr(owned, 0)) == 0:
            _capi_set_unicode_decode_value(pcc_gc_load_ptr(null(), owned))
        pcc_gc_store_root(owned, null())
        pcc_gc_frame_leave(owned)
        return
    py_raise_owned(py_exc_new(pcc_capi_exception_tag(type), message))


@c_abi_typed_export("PyErr_SetNone", "void", ("ptr",))
def PyErr_SetNone(type) -> None:
    if _capi_is_unicode_encode_type(type) != 0:
        _capi_set_unicode_encode_value(null())
        return
    if _capi_is_unicode_decode_type(type) != 0:
        _capi_set_unicode_decode_value(null())
        return
    PyErr_SetString(type, cstr(""))


@c_abi_typed_export("PyErr_SetObject", "void", ("ptr", "ptr"))
def PyErr_SetObject(type, value) -> None:
    if _capi_is_unicode_encode_type(type) != 0:
        _capi_set_unicode_encode_value(value)
        return
    if _capi_is_unicode_decode_type(type) != 0:
        _capi_set_unicode_decode_value(value)
        return
    cls = pcc_capi_exception_class(type)
    exc = null()
    if (
        not ptr_is_null(cls)
        and not is_tagged_int(cls)
        and load_i32(cls, 8) == PY_TYPE_CLASS  # PY_TYPE_CLASS
    ):
        if (
            ptr_eq(type, global_load_ptr("PyExc_ValueError"))
            or ptr_eq(type, global_load_ptr("PyExc_TypeError"))
            or ptr_eq(type, global_load_ptr("PyExc_RuntimeError"))
            or ptr_eq(type, global_load_ptr("PyExc_KeyError"))
            or ptr_eq(type, global_load_ptr("PyExc_IndexError"))
            or ptr_eq(type, global_load_ptr("PyExc_AttributeError"))
            or ptr_eq(type, global_load_ptr("PyExc_MemoryError"))
            or ptr_eq(type, global_load_ptr("PyExc_OverflowError"))
            or ptr_eq(type, global_load_ptr("PyExc_SystemError"))
            or ptr_eq(type, global_load_ptr("PyExc_NameError"))
            or ptr_eq(type, global_load_ptr("PyExc_NotImplementedError"))
            or ptr_eq(type, global_load_ptr("PyExc_BaseException"))
            or ptr_eq(type, global_load_ptr("PyExc_Exception"))
            or ptr_eq(type, global_load_ptr("PyExc_ArithmeticError"))
            or ptr_eq(type, global_load_ptr("PyExc_LookupError"))
            or ptr_eq(type, global_load_ptr("PyExc_OSError"))
            or ptr_eq(type, global_load_ptr("PyExc_IOError"))
            or ptr_eq(type, global_load_ptr("PyExc_AssertionError"))
            or ptr_eq(type, global_load_ptr("PyExc_StopIteration"))
            or ptr_eq(type, global_load_ptr("PyExc_StopAsyncIteration"))
            or ptr_eq(type, global_load_ptr("PyExc_ZeroDivisionError"))
            or ptr_eq(type, global_load_ptr("PyExc_ReferenceError"))
            or ptr_eq(type, global_load_ptr("PyExc_FloatingPointError"))
            or ptr_eq(type, global_load_ptr("PyExc_RecursionError"))
            or ptr_eq(type, global_load_ptr("PyExc_UnicodeDecodeError"))
            or ptr_eq(type, global_load_ptr("PyExc_ImportError"))
            or ptr_eq(type, global_load_ptr("PyExc_ModuleNotFoundError"))
        ):
            exc = py_exc_new_with_value(pcc_capi_exception_tag(type), value)
        else:
            text = null()
            if not ptr_is_null(value):
                text = py_obj_str(value)
            message = cstr("")
            if (
                not ptr_is_null(text)
                and not is_tagged_int(text)
                and load_i32(text, 8) == PY_TYPE_STR  # PY_TYPE_STR
            ):
                message = py_str_utf8(text)
            exc = py_exc_new_with_class(cls, message)
            if not ptr_is_null(text):
                py_decref(text)
    if ptr_is_null(exc):
        exc = py_exc_new_with_value(1, value)  # PY_EXC_EXCEPTION
    py_raise(exc)
    py_decref(exc)


@c_abi_typed_export("PyErr_NoMemory", "ptr", ())
def PyErr_NoMemory():
    PyErr_SetString(global_load_ptr("PyExc_MemoryError"), cstr("out of memory"))
    return null()


@c_abi_typed_export("PyErr_BadInternalCall", "void", ())
def PyErr_BadInternalCall() -> None:
    PyErr_SetString(global_load_ptr("PyExc_SystemError"), cstr("bad internal call"))


@c_abi_typed_export("PyErr_Occurred", "ptr", ())
def PyErr_Occurred():
    cur = py_current_exception()
    if ptr_is_null(cur):
        return null()
    if not is_tagged_int(cur) and load_i32(cur, 8) == PY_TYPE_EXC:  # PY_TYPE_EXC
        cls = pcc_gc_load_ptr(cur, ptr_add(cur, 16))
        if not ptr_is_null(cls):
            return cls
    return global_load_ptr("PyExc_RuntimeError")


@c_abi_typed_export("PyErr_Clear", "void", ())
def PyErr_Clear() -> None:
    py_clear_exception()


@c_abi_typed_export("PyErr_GivenExceptionMatches", "i32", ("ptr", "ptr"))
def PyErr_GivenExceptionMatches(given, exc) -> int:
    if ptr_is_null(given) or ptr_is_null(exc):
        return 0
    # CPython: exc may be a tuple of exception classes; match each element.
    if not is_tagged_int(exc) and load_i32(exc, 8) == PY_TYPE_TUPLE:  # PY_TYPE_TUPLE
        n = py_tuple_len(exc)
        i: int = 0
        while i < n:
            item = py_tuple_get(exc, i)
            if PyErr_GivenExceptionMatches(given, item) != 0:
                py_decref(item)
                return 1
            py_decref(item)
            i += 1
        return 0
    cls = pcc_capi_exception_class(exc)
    if ptr_is_null(cls):
        return 0
    if _capi_is_unicode_sentinel(given) != 0:
        given = pcc_capi_exception_class(given)
    if py_exc_matches(given, cls) != 0:
        return 1
    return 0


@c_abi_typed_export("PyErr_ExceptionMatches", "i32", ("ptr",))
def PyErr_ExceptionMatches(exc) -> int:
    cur = py_current_exception()
    if ptr_is_null(cur):
        return 0
    return PyErr_GivenExceptionMatches(cur, exc)


@c_abi_typed_export("PyErr_Fetch", "void", ("ptr", "ptr", "ptr"))
def PyErr_Fetch(ptype, pvalue, ptraceback) -> None:
    cur = py_current_exception()
    type = null()
    value = null()
    if not ptr_is_null(cur):
        py_incref(cur)
        value = cur
        if not is_tagged_int(cur) and load_i32(cur, 8) == PY_TYPE_EXC:  # PY_TYPE_EXC
            type = pcc_gc_load_ptr(cur, ptr_add(cur, 16))
        if ptr_is_null(type):
            type = global_load_ptr("PyExc_RuntimeError")
        py_incref(type)
        py_clear_exception()
    if not ptr_is_null(ptype):
        store_ptr(ptype, 0, type)
    else:
        py_decref(type)
    if not ptr_is_null(pvalue):
        store_ptr(pvalue, 0, value)
    else:
        py_decref(value)
    if not ptr_is_null(ptraceback):
        store_ptr(ptraceback, 0, null())


@c_abi_typed_export("PyErr_Restore", "void", ("ptr", "ptr", "ptr"))
def PyErr_Restore(type, value, traceback) -> None:
    if not ptr_is_null(value):
        py_raise(value)
    elif not ptr_is_null(type):
        PyErr_SetString(type, cstr(""))
    else:
        py_clear_exception()
    py_decref(type)
    py_decref(value)
    py_decref(traceback)


@c_abi_typed_export("PyErr_NewException", "ptr", ("ptr", "ptr", "ptr"))
def PyErr_NewException(name, base, dict):
    if ptr_is_null(name) or load_i8(name, 0) == 0:
        PyErr_SetString(global_load_ptr("PyExc_ValueError"), cstr("empty exception name"))
        return null()
    # leaf name: text after the last '.' (or the whole name).
    leaf_start = name
    index = 0
    while load_i8(name, index) != 0:
        if load_i8(name, index) == 46:  # '.'
            leaf_start = ptr_add(name, index + 1)
        index = index + 1
    if load_i8(leaf_start, 0) == 0:
        leaf_start = name

    base_cls = null()
    n_bases = 0
    if (
        not ptr_is_null(base)
        and not ptr_eq(base, global_load_ptr("py_None"))
        and not is_tagged_int(base)
        and load_i32(base, 8) == PY_TYPE_CLASS  # PY_TYPE_CLASS
    ):
        base_cls = base
        n_bases = 1

    name_len = strlen(leaf_start)
    class_name = malloc(name_len + 1)
    if ptr_is_null(class_name):
        PyErr_SetString(
            global_load_ptr("PyExc_RuntimeError"),
            cstr("out of memory creating exception"),
        )
        return null()
    memcpy(class_name, leaf_start, name_len)
    store_i8(class_name, name_len, 0)

    bases = stack_alloc(8)
    if n_bases != 0:
        store_ptr(bases, 0, base_cls)
    else:
        # CPython PyErr_NewException defaults the base to Exception.
        store_ptr(bases, 0, global_load_ptr("PyExc_Exception"))
        n_bases = 1

    cls = py_class_new(class_name, bases, n_bases, null(), 0)
    # py_class_new keeps its own copy of the name.
    free(class_name)
    if ptr_is_null(cls):
        PyErr_SetString(
            global_load_ptr("PyExc_RuntimeError"),
            cstr("failed to create exception class"),
        )
        return null()
    return cls


@c_abi_typed_export("PyErr_WarnEx", "i32", ("ptr", "ptr", "i64"))
def PyErr_WarnEx(category, message, stack_level: int) -> int:
    return 0


@c_abi_typed_export("PyErr_WarnFormat", "i32", ("ptr", "i64", "ptr"))
@c_abi_variadic_export("PyErr_WarnFormat")
def PyErr_WarnFormat(category, stack_level: int, format) -> int:
    message = stack_alloc(2048)
    cursor = va_start()
    _format_message(message, 2048, format, cursor)
    va_end(cursor)
    return PyErr_WarnEx(category, message, stack_level)


@c_abi_typed_export("PyErr_WriteUnraisable", "void", ("ptr",))
def PyErr_WriteUnraisable(obj) -> None:
    PyErr_Clear()


@c_abi_typed_export("PyErr_Print", "void", ())
def PyErr_Print() -> None:
    cur = py_current_exception()
    if ptr_is_null(cur):
        return
    py_exc_print_unhandled(cur)
    py_clear_exception()


@c_abi_typed_export("PyErr_CheckSignals", "i32", ())
def PyErr_CheckSignals() -> int:
    return 0


@c_abi_typed_export("PyErr_NormalizeException", "void", ("ptr", "ptr", "ptr"))
def PyErr_NormalizeException(exc, val, tb) -> None:
    return


# --- printf-style message formatter -------------------------------------
# Faithful pcc-Python port of py_capi_shim.c's pcc_capi_format_message
# (C's %-mini-language: %s %R %S %U %d %i %u %x %X %o %p %c %f %e %g and
# their length/precision variants).  Numeric text is produced without a host
# snprintf dependency; float text reuses the freestanding stdio emitter.


def _append_bytes(out, cap: int, out_len: int, text, text_len: int) -> int:
    if ptr_is_null(out) or cap == 0:
        return out_len
    if ptr_is_null(text):
        text = cstr("(null)")
        text_len = 6
    while text_len > 0 and out_len + 1 < cap:
        store_i8(out, out_len, load_i8(text, 0))
        out_len = out_len + 1
        text = ptr_add(text, 1)
        text_len = text_len - 1
    store_i8(out, out_len, 0)
    return out_len


def _append_cstr(out, cap: int, out_len: int, text, precision: int) -> int:
    if ptr_is_null(text):
        text = cstr("(null)")
    n = strlen(text)
    if precision >= 0 and precision < n:
        n = precision
    return _append_bytes(out, cap, out_len, text, n)


def _append_object_format(
    out, cap: int, out_len: int, obj, use_repr: int, precision: int
) -> int:
    if ptr_is_null(obj):
        return _append_cstr(out, cap, out_len, cstr("<NULL>"), precision)
    if use_repr != 0:
        text = py_obj_repr(obj)
    else:
        text = py_obj_str(obj)
    if ptr_is_null(text):
        text = py_obj_repr(obj)
    if (
        ptr_is_null(text)
        or is_tagged_int(text)
        or load_i32(text, 8) != PY_TYPE_STR  # PY_TYPE_STR
    ):
        if not ptr_is_null(text):
            py_decref(text)
        return _append_cstr(out, cap, out_len, cstr("<object>"), precision)
    raw = py_str_utf8(text)
    n = py_str_byte_len(text)
    if precision >= 0 and precision < n:
        n = precision
    out_len = _append_bytes(out, cap, out_len, raw, n)
    py_decref(text)
    return out_len


def _append_signed(out, cap: int, out_len: int, value: int) -> int:
    tmp = stack_alloc(24)
    pos = 0
    negative = 0
    if value < 0:
        negative = 1
        value = wrapping_mul_i64(value, -1)
    if value == 0:
        store_i8(tmp, pos, 48)  # '0'
        pos = pos + 1
    while value != 0:
        digit = unsigned_rem_i64(value, 10)
        store_i8(tmp, pos, 48 + digit)
        pos = pos + 1
        value = unsigned_div_i64(value, 10)
    if negative != 0:
        store_i8(tmp, pos, 45)  # '-'
        pos = pos + 1
    while pos > 0:
        pos = pos - 1
        out_len = _append_bytes(out, cap, out_len, ptr_add(tmp, pos), 1)
    return out_len


def _append_unsigned(out, cap: int, out_len: int, value: int, conv: int) -> int:
    base = 10
    uppercase = 0
    if conv == 120 or conv == 88:  # 'x' 'X'
        base = 16
        if conv == 88:
            uppercase = 1
    elif conv == 111:  # 'o'
        base = 8
    tmp = stack_alloc(24)
    pos = 0
    if value == 0:
        store_i8(tmp, pos, 48)
        pos = pos + 1
    while value != 0:
        digit = unsigned_rem_i64(value, base)
        if digit < 10:
            store_i8(tmp, pos, 48 + digit)
        else:
            if uppercase != 0:
                store_i8(tmp, pos, 65 + digit - 10)  # 'A'
            else:
                store_i8(tmp, pos, 97 + digit - 10)  # 'a'
        pos = pos + 1
        value = unsigned_div_i64(value, base)
    while pos > 0:
        pos = pos - 1
        out_len = _append_bytes(out, cap, out_len, ptr_add(tmp, pos), 1)
    return out_len


def _append_pointer(out, cap: int, out_len: int, value) -> int:
    out_len = _append_bytes(out, cap, out_len, cstr("0x"), 2)
    raw = ptr_to_int(value)
    hex_lo = stack_alloc(17)
    pos = 0
    if raw == 0:
        store_i8(hex_lo, pos, 48)
        pos = pos + 1
    while raw != 0:
        digit = unsigned_rem_i64(raw, 16)
        if digit < 10:
            store_i8(hex_lo, pos, 48 + digit)
        else:
            store_i8(hex_lo, pos, 97 + digit - 10)
        pos = pos + 1
        raw = unsigned_div_i64(raw, 16)
    while pos > 0:
        pos = pos - 1
        out_len = _append_bytes(out, cap, out_len, ptr_add(hex_lo, pos), 1)
    return out_len


def _format_message(message, message_cap: int, format, cursor) -> None:
    out_len = 0
    if ptr_is_null(message) or message_cap == 0:
        return
    store_i8(message, 0, 0)
    if ptr_is_null(format):
        return
    p = format
    while load_i8(p, 0) != 0:
        if load_i8(p, 0) != 37:  # '%'
            out_len = _append_bytes(message, message_cap, out_len, p, 1)
            p = ptr_add(p, 1)
            continue
        p = ptr_add(p, 1)
        if load_i8(p, 0) == 37:
            out_len = _append_bytes(message, message_cap, out_len, cstr("%"), 1)
            p = ptr_add(p, 1)
            continue
        # flags: # 0 - space +
        while True:
            flag = load_i8(p, 0)
            if flag == 35 or flag == 48 or flag == 45 or flag == 32 or flag == 43:
                p = ptr_add(p, 1)
            else:
                break
        if load_i8(p, 0) == 42:  # '*'
            va_arg_i32(cursor)
            p = ptr_add(p, 1)
        else:
            while load_i8(p, 0) >= 48 and load_i8(p, 0) <= 57:
                p = ptr_add(p, 1)
        precision = -1
        if load_i8(p, 0) == 46:  # '.'
            p = ptr_add(p, 1)
            if load_i8(p, 0) == 42:
                precision = va_arg_i32(cursor)
                p = ptr_add(p, 1)
            else:
                precision = 0
                while load_i8(p, 0) >= 48 and load_i8(p, 0) <= 57:
                    precision = precision * 10 + load_i8(p, 0) - 48
                    p = ptr_add(p, 1)
        length = 0
        first = load_i8(p, 0)
        second = load_i8(p, 1)
        if first == 108 and second == 108:  # 'll'
            length = 2
            p = ptr_add(p, 2)
        elif first == 108:  # 'l'
            length = 1
            p = ptr_add(p, 1)
        elif first == 122:  # 'z'
            length = 3
            p = ptr_add(p, 1)
        elif first == 104:  # 'h'/'hh'
            if second == 104:
                p = ptr_add(p, 2)
            else:
                p = ptr_add(p, 1)
        conv = load_i8(p, 0)
        if conv == 0:
            break
        p = ptr_add(p, 1)

        if conv == 115:  # 's'
            value = va_arg_ptr(cursor)
            out_len = _append_cstr(message, message_cap, out_len, value, precision)
        elif conv == 82 or conv == 83 or conv == 85:  # 'R' 'S' 'U'
            obj = va_arg_ptr(cursor)
            use_repr = 0
            if conv == 82:
                use_repr = 1
            out_len = _append_object_format(
                message, message_cap, out_len, obj, use_repr, precision
            )
        elif conv == 100 or conv == 105:  # 'd' 'i'
            if length != 0:
                value = va_arg_i64(cursor)
            else:
                value = va_arg_i32(cursor)
            out_len = _append_signed(message, message_cap, out_len, value)
        elif conv == 117 or conv == 120 or conv == 88 or conv == 111:
            if length != 0:
                value = va_arg_i64(cursor)
            else:
                value = va_arg_u32(cursor)
            out_len = _append_unsigned(message, message_cap, out_len, value, conv)
        elif conv == 112:  # 'p'
            value = va_arg_ptr(cursor)
            out_len = _append_pointer(message, message_cap, out_len, value)
        elif conv == 99:  # 'c'
            value = va_arg_i32(cursor)
            ch = stack_alloc(1)
            store_i8(ch, 0, value & 255)
            out_len = _append_bytes(message, message_cap, out_len, ch, 1)
        elif (
            conv == 102
            or conv == 70
            or conv == 101
            or conv == 69
            or conv == 103
            or conv == 71
        ):
            value = va_arg_f64(cursor)
            raw_float = stack_alloc(512)
            raw_heap = null()
            if pcc_stdio_float_raw_capacity(precision) > 512:
                raw_heap = malloc(pcc_stdio_float_raw_capacity(precision))
                if not ptr_is_null(raw_heap):
                    raw_float = raw_heap
            if ptr_is_null(raw_heap) and pcc_stdio_float_raw_capacity(precision) > 512:
                out_len = _append_bytes(message, message_cap, out_len, cstr("?"), 1)
            else:
                raw_length = pcc_stdio_format_float_raw(
                    raw_float, value, conv, precision, 0, 0, 0
                )
                out_len = _append_bytes(
                    message, message_cap, out_len, raw_float, raw_length
                )
            if not ptr_is_null(raw_heap):
                free(raw_heap)
        else:
            out_len = _append_bytes(message, message_cap, out_len, cstr("%"), 1)
            ch = stack_alloc(1)
            store_i8(ch, 0, conv)
            out_len = _append_bytes(message, message_cap, out_len, ch, 1)


@c_abi_typed_export("PyUnicode_FromFormat", "ptr", ("ptr",))
@c_abi_variadic_export("PyUnicode_FromFormat")
def PyUnicode_FromFormat(format):
    message = stack_alloc(2048)
    cursor = va_start()
    _format_message(message, 2048, format, cursor)
    va_end(cursor)
    return py_str_new(message, strlen(message))


@c_abi_typed_export("PyUnicode_FromFormatV", "ptr", ("ptr", "ptr"))
def PyUnicode_FromFormatV(format, vargs):
    message = stack_alloc(2048)
    cursor = va_cursor(vargs)
    _format_message(message, 2048, format, cursor)
    return py_str_new(message, strlen(message))


@c_abi_typed_export("PyErr_Format", "ptr", ("ptr", "ptr"))
@c_abi_variadic_export("PyErr_Format")
def PyErr_Format(type, format):
    message = stack_alloc(2048)
    cursor = va_start()
    _format_message(message, 2048, format, cursor)
    va_end(cursor)
    PyErr_SetString(type, message)
    return null()


@c_abi_typed_export("PyErr_FormatV", "ptr", ("ptr", "ptr", "ptr"))
def PyErr_FormatV(type, format, vargs):
    message = stack_alloc(2048)
    cursor = va_cursor(vargs)
    _format_message(message, 2048, format, cursor)
    PyErr_SetString(type, message)
    return null()
