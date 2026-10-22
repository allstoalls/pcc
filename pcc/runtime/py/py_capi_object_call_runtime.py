"""pcc-Python owners for the no-libpython C-API object-call core.

Replaces the PyObject_Call / CallObject / CallNoArgs / CallOneArg block of
py_capi_shim.c.  All delegate to the existing pcc-Python object-call ABI
(py_obj_call) or pcc-Python-owned C-API siblings (PyErr_*, py_tuple_new).

Owned surface (stable C ABI names):

  PyObject_Call, PyObject_CallObject, PyObject_CallNoArgs, PyObject_CallOneArg,
  PyObject_Vectorcall, PyObject_VectorcallMethod, PyVectorcall_Call,
  PyVectorcall_NARGS
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PY_TYPE_TUPLE

from pcc.extern import c_abi_typed_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    call_ptr_ptr_ptr_i64_ptr,
    calloc,
    cstr,
    define_global_ptr_null,
    free,
    global_load_ptr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    null,
    ptr_add,
    ptr_is_null,
    stack_alloc,
    store_i64,
    store_ptr,
)

py_obj_call = extern("py_obj_call", (c_ptr, c_ptr, c_ptr), c_ptr)
pcc_diagnostics_runtime_log_event_code = extern(
    "pcc_diagnostics_runtime_log_event_code",
    (c_int32, c_int32, c_int64, c_int64, c_ptr),
    c_void,
)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_runtime_error_if_unset = extern(
    "py_runtime_error_if_unset", (c_ptr, c_ptr), c_ptr
)
pcc_capi_cext_type_for_object = extern(
    "pcc_capi_cext_type_for_object", (c_ptr,), c_ptr
)
PyTuple_Size = extern("PyTuple_Size", (c_ptr,), c_int64)
PyTuple_GetItem = extern("PyTuple_GetItem", (c_ptr, c_int64), c_ptr)
PyTuple_SetItem = extern("PyTuple_SetItem", (c_ptr, c_int64, c_ptr), c_int32)
PyDict_New = extern("PyDict_New", (), c_ptr)
PyDict_Check = extern("PyDict_Check", (c_ptr,), c_int32)
PyDict_Size = extern("PyDict_Size", (c_ptr,), c_int64)
PyDict_SetItem = extern("PyDict_SetItem", (c_ptr, c_ptr, c_ptr), c_int32)
PyDict_Next = extern("PyDict_Next", (c_ptr, c_ptr, c_ptr, c_ptr), c_int32)
PyUnicode_Check = extern("PyUnicode_Check", (c_ptr,), c_int32)
PyObject_GetAttr = extern("PyObject_GetAttr", (c_ptr, c_ptr), c_ptr)
PyErr_NoMemory = extern("PyErr_NoMemory", (), c_ptr)

def _py_none() -> c_ptr:
    return global_load_ptr("py_None")


def _type_error(message) -> None:
    py_raise_owned(py_exc_new(3, message))  # PY_EXC_TYPEERROR


def _runtime_error(message) -> None:
    py_raise_owned(py_exc_new(7, message))  # PY_EXC_RUNTIMEERROR


def _vectorcall_nargs(nargsf: int) -> int:
    return nargsf & 0x7FFFFFFFFFFFFFFF


def _cext_vectorcall_slot(callable, result_out) -> int:
    store_ptr(result_out, 0, null())
    type_obj = pcc_capi_cext_type_for_object(callable)
    if ptr_is_null(type_obj):
        return 0
    flags: int = load_i64(type_obj, 176)
    offset: int = load_i64(type_obj, 64)
    basicsize: int = load_i64(type_obj, 40)
    if (flags & (1 << 11)) == 0 or offset <= 0 or offset > basicsize - 8:
        return 0
    store_ptr(result_out, 0, load_ptr(callable, offset))
    return 1


@c_abi_typed_export("PyObject_Call", "ptr", ("ptr", "ptr", "ptr"))
def PyObject_Call(callable, args, kwargs) -> c_ptr:
    if ptr_is_null(callable):
        _type_error(cstr("NULL callable"))
        return null()
    call_args = args
    made_args: int = 0
    if ptr_is_null(call_args):
        call_args = py_tuple_new(0)
        made_args = 1
        if ptr_is_null(call_args):
            _type_error(cstr("out of memory creating call args"))
            return null()
    elif is_tagged_int(call_args) or load_i32(call_args, 8) != PY_TYPE_TUPLE:  # PY_TYPE_TUPLE
        _type_error(cstr("PyObject_Call args must be tuple or NULL"))
        return null()
    if ptr_is_null(kwargs):
        kwargs = _py_none()
    result = py_obj_call(callable, call_args, kwargs)
    if ptr_is_null(result) and py_err_occurred() == 0:
        # py_obj_call owns the user-facing callable diagnosis. Reaching this
        # guard means that runtime contract was violated, so preserve that
        # attribution instead of inventing a second, less precise TypeError.
        py_runtime_error_if_unset(
            cstr("py_obj_call"),
            cstr("py_obj_call returned NULL without setting an exception"),
        )
        tag: int = -1
        if not is_tagged_int(callable):
            tag = load_i32(callable, 8)
        pcc_diagnostics_runtime_log_event_code(7, 10, tag, 99, callable)
    if made_args != 0:
        py_decref(call_args)
    return result


@c_abi_typed_export("PyObject_CallObject", "ptr", ("ptr", "ptr"))
def PyObject_CallObject(callable, args) -> c_ptr:
    return PyObject_Call(callable, args, null())


@c_abi_typed_export("PyObject_CallNoArgs", "ptr", ("ptr",))
def PyObject_CallNoArgs(callable) -> c_ptr:
    if ptr_is_null(callable):
        _type_error(cstr("NULL callable"))
        return null()
    empty = py_tuple_new(0)
    if ptr_is_null(empty):
        return null()
    result = py_obj_call(callable, empty, _py_none())
    py_decref(empty)
    return result


@c_abi_typed_export("PyObject_CallOneArg", "ptr", ("ptr", "ptr"))
def PyObject_CallOneArg(callable, arg) -> c_ptr:
    if ptr_is_null(arg):
        _type_error(cstr("NULL call argument"))
        return null()
    args = py_tuple_new(1)
    if ptr_is_null(args):
        return null()
    py_tuple_set_item(args, 0, arg)
    result = py_obj_call(callable, args, _py_none())
    py_decref(args)
    return result


@c_abi_typed_export("PyVectorcall_NARGS", "i64", ("i64",))
def PyVectorcall_NARGS(nargsf: int) -> int:
    return _vectorcall_nargs(nargsf)


@c_abi_typed_export(
    "PyObject_Vectorcall", "ptr", ("ptr", "ptr", "i64", "ptr")
)
def PyObject_Vectorcall(callable, args, nargsf: int, kwnames) -> c_ptr:
    if ptr_is_null(callable):
        _type_error(cstr("NULL callable"))
        return null()

    slot_out = stack_alloc(8)
    if _cext_vectorcall_slot(callable, slot_out) != 0:
        vectorcall = load_ptr(slot_out, 0)
        if ptr_is_null(vectorcall):
            _type_error(cstr("vectorcall function is NULL"))
            return null()
        return call_ptr_ptr_ptr_i64_ptr(
            vectorcall, callable, args, nargsf, kwnames
        )

    nargs: int = _vectorcall_nargs(nargsf)
    nkwargs: int = 0
    if not ptr_is_null(kwnames):
        nkwargs = PyTuple_Size(kwnames)
        if nkwargs < 0:
            return null()
    if (nargs > 0 or nkwargs > 0) and ptr_is_null(args):
        _type_error(cstr("NULL vectorcall args"))
        return null()

    tuple_args = py_tuple_new(nargs)
    if ptr_is_null(tuple_args):
        _runtime_error(cstr("out of memory creating vectorcall args"))
        return null()
    i: int = 0
    while i < nargs:
        value = load_ptr(args, i * 8)
        if ptr_is_null(value):
            py_decref(tuple_args)
            _type_error(cstr("NULL vectorcall argument"))
            return null()
        py_tuple_set_item(tuple_args, i, value)
        i += 1

    kwargs = null()
    if nkwargs > 0:
        kwargs = PyDict_New()
        if ptr_is_null(kwargs):
            py_decref(tuple_args)
            return null()
        i = 0
        while i < nkwargs:
            key = PyTuple_GetItem(kwnames, i)
            value = load_ptr(args, (nargs + i) * 8)
            if ptr_is_null(key) or ptr_is_null(value) or PyUnicode_Check(key) == 0:
                py_decref(kwargs)
                py_decref(tuple_args)
                if not ptr_is_null(key) and PyUnicode_Check(key) == 0:
                    _type_error(cstr("vectorcall keyword names must be strings"))
                elif ptr_is_null(value):
                    _type_error(cstr("NULL vectorcall keyword argument"))
                return null()
            if PyDict_SetItem(kwargs, key, value) != 0:
                py_decref(kwargs)
                py_decref(tuple_args)
                return null()
            i += 1

    result = PyObject_Call(callable, tuple_args, kwargs)
    if not ptr_is_null(kwargs):
        py_decref(kwargs)
    py_decref(tuple_args)
    return result


@c_abi_typed_export(
    "PyObject_VectorcallMethod", "ptr", ("ptr", "ptr", "i64", "ptr")
)
def PyObject_VectorcallMethod(name, args, nargsf: int, kwnames) -> c_ptr:
    nargs: int = _vectorcall_nargs(nargsf)
    if (
        ptr_is_null(name)
        or ptr_is_null(args)
        or nargs == 0
        or ptr_is_null(load_ptr(args, 0))
    ):
        _type_error(cstr("invalid vectorcall method call"))
        return null()
    method = PyObject_GetAttr(load_ptr(args, 0), name)
    if ptr_is_null(method):
        return null()
    # The offset bit lives in the sign bit.  Subtracting one from nargsf
    # preserves that bit while reducing the masked argument count, and keeps
    # this ABI value in the scalar i64 lane (no object/int phi or bigint
    # boxing in this low-level dispatch path).
    result = PyObject_Vectorcall(
        method, ptr_add(args, 8), nargsf - 1, kwnames
    )
    py_decref(method)
    return result


@c_abi_typed_export("PyVectorcall_Call", "ptr", ("ptr", "ptr", "ptr"))
def PyVectorcall_Call(callable, tuple_args, dict_args) -> c_ptr:
    slot_out = stack_alloc(8)
    if _cext_vectorcall_slot(callable, slot_out) == 0:
        return PyObject_Call(callable, tuple_args, dict_args)
    vectorcall = load_ptr(slot_out, 0)
    if ptr_is_null(vectorcall):
        _type_error(cstr("vectorcall function is NULL"))
        return null()

    nargs: int = PyTuple_Size(tuple_args)
    if nargs < 0:
        return null()
    nkwargs: int = 0
    if not ptr_is_null(dict_args):
        if PyDict_Check(dict_args) == 0:
            _type_error(cstr("vectorcall kwargs must be a dict"))
            return null()
        nkwargs = PyDict_Size(dict_args)
        if nkwargs < 0:
            return null()
    if nargs > 0x7FFFFFFFFFFFFFFF - nkwargs:
        return PyErr_NoMemory()

    total: int = nargs + nkwargs
    values = null()
    if total > 0:
        values = calloc(total, 8)
        if ptr_is_null(values):
            return PyErr_NoMemory()
    i: int = 0
    while i < nargs:
        value = PyTuple_GetItem(tuple_args, i)
        if ptr_is_null(value):
            free(values)
            return null()
        store_ptr(values, i * 8, value)
        i += 1

    keyword_names = null()
    if nkwargs > 0:
        keyword_names = py_tuple_new(nkwargs)
        if ptr_is_null(keyword_names):
            free(values)
            return null()
        pos_slot = stack_alloc(8)
        key_slot = stack_alloc(8)
        value_slot = stack_alloc(8)
        store_i64(pos_slot, 0, 0)
        store_ptr(key_slot, 0, null())
        store_ptr(value_slot, 0, null())
        i = 0
        while PyDict_Next(dict_args, pos_slot, key_slot, value_slot) != 0:
            if i >= nkwargs:
                _runtime_error(cstr("kwargs changed during call"))
                py_decref(keyword_names)
                free(values)
                return null()
            key = load_ptr(key_slot, 0)
            value = load_ptr(value_slot, 0)
            store_ptr(values, (nargs + i) * 8, value)
            py_incref(key)
            if PyTuple_SetItem(keyword_names, i, key) != 0:
                py_decref(key)
                py_decref(keyword_names)
                free(values)
                return null()
            i += 1
        if i != nkwargs:
            _runtime_error(cstr("kwargs changed during call"))
            py_decref(keyword_names)
            free(values)
            return null()

    result = call_ptr_ptr_ptr_i64_ptr(
        vectorcall, callable, values, nargs, keyword_names
    )
    if not ptr_is_null(keyword_names):
        py_decref(keyword_names)
    free(values)
    return result
