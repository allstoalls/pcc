"""pcc-Python owner for synchronous context-manager runtime entrypoints."""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PY_TYPE_FUNC

from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    call_ptr1,
    call_ptr4,
    cstr,
    global_load_ptr,
    is_tagged_int,
    load_i32,
    null,
    ptr_is_null,
)


py_obj_getattr = extern("py_obj_getattr", (c_ptr, c_ptr), c_ptr)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_func_call = extern("py_func_call", (c_ptr, c_ptr), c_ptr)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_current_exception = extern("py_current_exception", (), c_ptr)
py_tls_exc_set = extern("py_tls_exc_set", (c_ptr,), c_void)
pcc_gc_note_relocation_read = extern(
    "pcc_gc_note_relocation_read",
    (c_ptr,),
    c_ptr,
)
py_obj_truthy = extern("py_obj_truthy", (c_ptr,), c_int64)
py_runtime_error_if_unset = extern(
    "py_runtime_error_if_unset", (c_ptr, c_ptr), c_ptr
)


def _require_result(result, helper_name, message):
    if ptr_is_null(result):
        py_runtime_error_if_unset(helper_name, message)
    return result


def _is_bound_func(method) -> bool:
    if ptr_is_null(method) or is_tagged_int(method):
        return False
    return load_i32(method, 8) == PY_TYPE_FUNC


def _call_enter_method(method, manager):
    if ptr_is_null(method):
        return _require_result(
            null(),
            cstr("call_unary_method"),
            cstr("context __enter__ dispatch received NULL method"),
        )
    if _is_bound_func(method):
        args = py_tuple_new(0)
        if ptr_is_null(args):
            return _require_result(
                null(),
                cstr("py_tuple_new"),
                cstr("context __enter__ could not allocate its argument tuple"),
            )
        out = py_func_call(method, args)
        if ptr_is_null(out):
            _require_result(
                null(),
                cstr("context __enter__"),
                cstr("context __enter__ returned NULL without setting an exception"),
            )
        py_decref(args)
        return out
    return _require_result(
        call_ptr1(method, manager),
        cstr("context __enter__"),
        cstr("context __enter__ returned NULL without setting an exception"),
    )


def _call_exit_method(method, manager, exc_type, exc, traceback):
    if ptr_is_null(method):
        return _require_result(
            null(),
            cstr("call_exit_method"),
            cstr("context __exit__ dispatch received NULL method"),
        )
    none = global_load_ptr("py_None")
    if ptr_is_null(exc_type):
        exc_type = none
    if ptr_is_null(exc):
        exc = none
    if ptr_is_null(traceback):
        traceback = none
    if _is_bound_func(method):
        args = py_tuple_new(3)
        if ptr_is_null(args):
            return _require_result(
                null(),
                cstr("py_tuple_new"),
                cstr("context __exit__ could not allocate its argument tuple"),
            )
        py_tuple_set_item(args, 0, exc_type)
        py_tuple_set_item(args, 1, exc)
        py_tuple_set_item(args, 2, traceback)
        out = py_func_call(method, args)
        if ptr_is_null(out):
            _require_result(
                null(),
                cstr("context __exit__"),
                cstr("context __exit__ returned NULL without setting an exception"),
            )
        py_decref(args)
        return out
    return _require_result(
        call_ptr4(method, manager, exc_type, exc, traceback),
        cstr("context __exit__"),
        cstr("context __exit__ returned NULL without setting an exception"),
    )


@c_abi_export("py_context_enter")
def py_context_enter(manager):
    if ptr_is_null(manager):
        return _require_result(
            null(),
            cstr("py_context_enter"),
            cstr("py_context_enter received NULL manager"),
        )
    method = py_obj_getattr(manager, cstr("__enter__"))
    if ptr_is_null(method):
        return _require_result(
            null(),
            cstr("py_obj_getattr"),
            cstr("context manager has no usable __enter__ method"),
        )
    result = _call_enter_method(method, manager)
    if ptr_is_null(result):
        _require_result(
            null(),
            cstr("py_context_enter"),
            cstr("py_context_enter returned NULL without setting an exception"),
        )
    py_decref(method)
    return result


@c_abi_export("py_context_exit")
def py_context_exit(manager, exc_type, exc, traceback) -> int:
    if ptr_is_null(manager):
        _require_result(
            null(),
            cstr("py_context_exit"),
            cstr("py_context_exit received NULL manager"),
        )
        return 0

    stashed = py_current_exception()
    if not ptr_is_null(stashed):
        py_tls_exc_set(null())
    method = py_obj_getattr(manager, cstr("__exit__"))
    if ptr_is_null(method):
        if not ptr_is_null(stashed):
            py_tls_exc_set(pcc_gc_note_relocation_read(stashed))
        return 0

    result = _call_exit_method(method, manager, exc_type, exc, traceback)
    if ptr_is_null(result):
        _require_result(
            null(),
            cstr("py_context_exit"),
            cstr("py_context_exit returned NULL without setting an exception"),
        )
        py_decref(method)
        if not ptr_is_null(stashed):
            py_decref(pcc_gc_note_relocation_read(stashed))
        return 0

    truth: int = py_obj_truthy(result)
    py_decref(result)
    py_decref(method)
    if not ptr_is_null(stashed):
        py_tls_exc_set(pcc_gc_note_relocation_read(stashed))
    return truth
