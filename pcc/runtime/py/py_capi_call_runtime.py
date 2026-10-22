"""pcc-Python owners for the variadic PyObject call surface.

Replaces the PyObject_CallFunction / CallFunctionObjArgs / CallMethod /
CallMethodObjArgs / CallMethodNoArgs / CallMethodOneArg block of
py_capi_shim.c.  The variadic forms consume a va_list via the pcc.unsafe
va_* intrinsics (the same mechanism as PyTuple_Pack).

Owned surface (stable C ABI names):

  PyObject_CallMethodNoArgs, PyObject_CallMethodOneArg,
  PyObject_CallFunctionObjArgs, PyObject_CallMethodObjArgs,
  PyObject_CallFunction, PyObject_CallMethod
"""

__pcc_runtime_port__ = True

from pcc.extern import c_abi_typed_export, c_abi_variadic_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    cstr,
    load_i8,
    load_ptr,
    null,
    ptr_is_null,
    stack_alloc,
    store_ptr,
    va_arg_ptr,
    va_end,
    va_start,
)

py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
PyObject_GetAttr = extern("PyObject_GetAttr", (c_ptr, c_ptr), c_ptr)
PyObject_CallNoArgs = extern("PyObject_CallNoArgs", (c_ptr,), c_ptr)
PyObject_CallOneArg = extern("PyObject_CallOneArg", (c_ptr, c_ptr), c_ptr)
PyObject_Call = extern("PyObject_Call", (c_ptr, c_ptr, c_ptr), c_ptr)


def _type_error(message) -> None:
    py_raise_owned(py_exc_new(3, message))  # PY_EXC_TYPEERROR


def _call_objargs_v(callable, cursor) -> c_ptr:
    if ptr_is_null(callable):
        _type_error(cstr("NULL callable"))
        return null()
    # Collect variadic args into a fixed stack slot array until the NULL
    # terminator.  Max 64 args (CPython has no such hard limit but every real
    # call is far below this).
    slots = stack_alloc(512)
    count: int = 0
    while count < 64:
        item = va_arg_ptr(cursor)
        if ptr_is_null(item):
            break
        store_ptr(slots, count * 8, item)
        count += 1
    args = py_tuple_new(count)
    if ptr_is_null(args):
        _runtime_error_oom()
        return null()
    index: int = 0
    while index < count:
        item = load_ptr(slots, index * 8)
        py_tuple_set_item(args, index, item)
        index += 1
    result = PyObject_Call(callable, args, null())
    py_decref(args)
    return result


def _runtime_error_oom() -> None:
    py_raise_owned(py_exc_new(7, cstr("out of memory creating call args")))


@c_abi_typed_export("PyObject_CallMethodNoArgs", "ptr", ("ptr", "ptr"))
def PyObject_CallMethodNoArgs(obj, name) -> c_ptr:
    method = PyObject_GetAttr(obj, name)
    if ptr_is_null(method):
        return null()
    result = PyObject_CallNoArgs(method)
    py_decref(method)
    return result


@c_abi_typed_export("PyObject_CallMethodOneArg", "ptr", ("ptr", "ptr", "ptr"))
def PyObject_CallMethodOneArg(obj, name, arg) -> c_ptr:
    method = PyObject_GetAttr(obj, name)
    if ptr_is_null(method):
        return null()
    result = PyObject_CallOneArg(method, arg)
    py_decref(method)
    return result


@c_abi_typed_export("PyObject_CallFunctionObjArgs", "ptr", ("ptr",))
@c_abi_variadic_export("PyObject_CallFunctionObjArgs")
def PyObject_CallFunctionObjArgs(callable):
    cursor = va_start()
    result = _call_objargs_v(callable, cursor)
    va_end(cursor)
    return result


@c_abi_typed_export("PyObject_CallMethodObjArgs", "ptr", ("ptr", "ptr"))
@c_abi_variadic_export("PyObject_CallMethodObjArgs")
def PyObject_CallMethodObjArgs(obj, name):
    if ptr_is_null(obj) or ptr_is_null(name):
        _type_error(cstr("invalid PyObject_CallMethodObjArgs call"))
        return null()
    method = PyObject_GetAttr(obj, name)
    if ptr_is_null(method):
        return null()
    cursor = va_start()
    result = _call_objargs_v(method, cursor)
    va_end(cursor)
    py_decref(method)
    return result


pcc_capi_build_call_args = extern(
    "pcc_capi_build_call_args", (c_ptr, c_ptr), c_ptr
)


def _build_call_args(format, cursor) -> c_ptr:
    # Full Py_BuildValue format engine with force_tuple=1 (owned by
    # py_capi_buildvalue_runtime); numpy calls e.g. "Os" here, so the earlier
    # O/N-only parser silently dropped trailing arguments.
    return pcc_capi_build_call_args(format, cursor)


def cstr_to_obj(s) -> c_ptr:
    py_unicode_from_string = extern("PyUnicode_FromString", (c_ptr,), c_ptr)
    return py_unicode_from_string(s)


@c_abi_typed_export("PyObject_CallFunction", "ptr", ("ptr", "ptr"))
@c_abi_variadic_export("PyObject_CallFunction")
def PyObject_CallFunction(callable, format):
    cursor = va_start()
    args = _build_call_args(format, cursor)
    va_end(cursor)
    if ptr_is_null(args):
        return null()
    result = PyObject_Call(callable, args, null())
    py_decref(args)
    return result


@c_abi_typed_export("PyObject_CallMethod", "ptr", ("ptr", "ptr", "ptr"))
@c_abi_variadic_export("PyObject_CallMethod")
def PyObject_CallMethod(obj, name, format):
    method = PyObject_GetAttr(obj, cstr_to_obj(name))
    if ptr_is_null(method):
        return null()
    cursor = va_start()
    args = _build_call_args(format, cursor)
    va_end(cursor)
    if ptr_is_null(args):
        py_decref(method)
        return null()
    result = PyObject_Call(method, args, null())
    py_decref(args)
    py_decref(method)
    return result
