"""pcc-Python owners for the no-libpython C-extension method bridge.

Replaces the pcc_capi_method_* + pcc_capi_prepare_call_args +
pcc_capi_builtin_object_getattr (list.sort bridge) block of py_capi_shim.c.
A C-extension PyMethodDef is wrapped as a pcc function object whose entry is
the method_call_entry trampoline; captures carry (self, method_ptr, flags).
The trampoline decodes the capture and dispatches to the C slot through the
fixed-signature indirect-call intrinsics.

Owned surface (stable C ABI names):

  pcc_capi_builtin_object_getattr
  pcc_capi_method_func_new
  pcc_capi_method_call_entry
  pcc_capi_prepare_call_args

Public object type tags come from the generated ``py_abi_constants`` module.
Private exception and method-flag values remain owned by this bridge:
  PY_EXC_TYPEERROR = 3
  (0x0001)=0x0001, (0x0002)=0x0002, (0x0004)=0x0004,
  (0x0008)=0x0008, (0x0080)=0x0080
  PCC_FUNC_SIGNATURE_MAGIC = "__pcc_func_signature_v1__"
  (3) = 3, (4) = 4
  PyFuncObject capi_method@16, capi_self@24
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PY_TYPE_DICT, PY_TYPE_LIST, PY_TYPE_STR, PY_TYPE_TUPLE

from pcc.extern import c_abi_typed_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    call_ptr1,
    call_ptr2,
    call_ptr3,
    call_ptr4,
    call_ptr_ptr_ptr_i64_ptr,
    calloc,
    cstr,
    define_global_cstr,
    define_global_i64,
    define_global_struct_words,
    free,
    function_addr,
    global_addr,
    global_load_ptr,
    int_to_ptr,
    is_tagged_int,
    load_i32,
    load_ptr,
    null,
    ptr_add,
    ptr_is_null,
    ptr_to_int,
    store_ptr,
    strlen,
)

py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_get = extern("py_tuple_get", (c_ptr, c_int64), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_tuple_len = extern("py_tuple_len", (c_ptr,), c_int64)
py_dict_len = extern("py_dict_len", (c_ptr,), c_int64)
py_dict_items = extern("py_dict_items", (c_ptr,), c_ptr)
py_dict_get = extern("py_dict_get", (c_ptr, c_ptr), c_ptr)
py_list_len = extern("py_list_len", (c_ptr,), c_int64)
py_list_get = extern("py_list_get", (c_ptr, c_int64), c_ptr)
py_list_set = extern("py_list_set", (c_ptr, c_int64, c_ptr), c_void)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_int_value_i64 = extern("py_int_value_i64", (c_ptr,), c_int64)
py_func_new_named = extern("py_func_new_named", (c_ptr, c_ptr, c_ptr), c_ptr)
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
PyErr_NoMemory = extern("PyErr_NoMemory", (), c_ptr)
pcc_gc_store_ptr = extern("pcc_gc_store_ptr", (c_ptr, c_ptr, c_ptr), c_void)
strcmp_c = extern("strcmp", (c_ptr, c_ptr), c_int32)
pcc_capi_builtin_type_token = extern("pcc_capi_builtin_type_token", (c_ptr,), c_ptr)
py_obj_lt = extern("py_obj_lt", (c_ptr, c_ptr), c_int64)
py_obj_call = extern("py_obj_call", (c_ptr, c_ptr, c_ptr), c_ptr)
py_obj_getattr = extern("py_obj_getattr", (c_ptr, c_ptr), c_ptr)


def _py_none() -> c_ptr:
    return global_load_ptr("py_None")


def _py_false() -> c_ptr:
    return global_load_ptr("py_False")


def _type_error(message) -> None:
    py_raise_owned(py_exc_new(3, message))  # PY_EXC_TYPEERROR


def _method_require_result(result):
    if ptr_is_null(result):
        py_runtime_error_if_unset(
            cstr("pcc_capi_method_call_entry"),
            cstr("C extension method returned NULL without setting an exception"),
        )
    return result


@c_abi_typed_export("pcc_capi_prepare_call_args", "ptr", ("ptr",))
def pcc_capi_prepare_call_args(args) -> c_ptr:
    if ptr_is_null(args) or is_tagged_int(args) or load_i32(args, 8) != PY_TYPE_TUPLE:
        _type_error(cstr("C method args must be a tuple"))
        return null()
    n = py_tuple_len(args)
    changed: int = 0
    i: int = 0
    while i < n:
        item = py_tuple_get(args, i)
        if ptr_is_null(item):
            return null()
        if pcc_capi_builtin_type_token(item) != item:
            changed = 1
        py_decref(item)
        i += 1
    if changed == 0:
        py_incref(args)
        return args
    out = py_tuple_new(n)
    if ptr_is_null(out):
        return null()
    i = 0
    while i < n:
        item = py_tuple_get(args, i)
        if ptr_is_null(item):
            py_decref(out)
            return null()
        translated = pcc_capi_builtin_type_token(item)
        py_tuple_set_item(out, i, translated)
        py_decref(item)
        i += 1
    return out


def _method_capture(self_obj, method) -> c_ptr:
    captures = py_tuple_new(3)
    if ptr_is_null(captures):
        return null()
    method_ptr = py_int_from_i64(ptr_to_int(method))
    flags = py_int_from_i64(load_i32(method, 16))  # ml_flags
    if ptr_is_null(method_ptr) or ptr_is_null(flags):
        py_decref(captures)
        if not ptr_is_null(method_ptr):
            py_decref(method_ptr)
        if not ptr_is_null(flags):
            py_decref(flags)
        return null()
    if ptr_is_null(self_obj):
        self_obj = _py_none()
    py_tuple_set_item(captures, 0, self_obj)
    py_tuple_set_item(captures, 1, method_ptr)
    py_tuple_set_item(captures, 2, flags)
    py_decref(method_ptr)
    py_decref(flags)
    return captures


def _method_keyword_signature() -> c_ptr:
    sig = py_tuple_new(5)
    names = py_tuple_new(2)
    kinds = py_tuple_new(2)
    has_defaults = py_tuple_new(2)
    defaults = py_tuple_new(2)
    magic = py_str_new(cstr("__pcc_func_signature_v1__"), 25)
    args_name = py_str_new(cstr("args"), 4)
    kwargs_name = py_str_new(cstr("kwargs"), 6)
    varargs_kind = py_int_from_i64((3))
    varkw_kind = py_int_from_i64((4))
    if (
        ptr_is_null(sig) or ptr_is_null(names) or ptr_is_null(kinds)
        or ptr_is_null(has_defaults) or ptr_is_null(defaults) or ptr_is_null(magic)
        or ptr_is_null(args_name) or ptr_is_null(kwargs_name)
        or ptr_is_null(varargs_kind) or ptr_is_null(varkw_kind)
    ):
        for obj in (sig, names, kinds, has_defaults, defaults, magic, args_name,
                    kwargs_name, varargs_kind, varkw_kind):
            if not ptr_is_null(obj):
                py_decref(obj)
        return null()
    py_tuple_set_item(names, 0, args_name)
    py_tuple_set_item(names, 1, kwargs_name)
    py_tuple_set_item(kinds, 0, varargs_kind)
    py_tuple_set_item(kinds, 1, varkw_kind)
    py_tuple_set_item(has_defaults, 0, _py_false())
    py_tuple_set_item(has_defaults, 1, _py_false())
    py_tuple_set_item(defaults, 0, _py_none())
    py_tuple_set_item(defaults, 1, _py_none())
    py_tuple_set_item(sig, 0, magic)
    py_tuple_set_item(sig, 1, names)
    py_tuple_set_item(sig, 2, kinds)
    py_tuple_set_item(sig, 3, has_defaults)
    py_tuple_set_item(sig, 4, defaults)
    for obj in (names, kinds, has_defaults, defaults, magic, args_name,
                kwargs_name, varargs_kind, varkw_kind):
        py_decref(obj)
    return sig


def _method_keyword_capture(self_obj, method) -> c_ptr:
    inner = _method_capture(self_obj, method)
    signature = _method_keyword_signature()
    wrapped = py_tuple_new(2)
    if ptr_is_null(inner) or ptr_is_null(signature) or ptr_is_null(wrapped):
        if not ptr_is_null(inner):
            py_decref(inner)
        if not ptr_is_null(signature):
            py_decref(signature)
        if not ptr_is_null(wrapped):
            py_decref(wrapped)
        return null()
    py_tuple_set_item(wrapped, 0, inner)
    py_tuple_set_item(wrapped, 1, signature)
    py_decref(inner)
    py_decref(signature)
    return wrapped


def _method_func_finish(fn, self_obj, method) -> c_ptr:
    if ptr_is_null(fn):
        return null()
    store_ptr(fn, 16, method)  # capi_method
    pcc_gc_store_ptr(
        fn, ptr_add(fn, 24), self_obj if not ptr_is_null(self_obj) else _py_none()
    )
    return fn


@c_abi_typed_export("pcc_capi_method_func_new", "ptr", ("ptr", "ptr"))
def pcc_capi_method_func_new(self_obj, method) -> c_ptr:
    if ptr_is_null(method) or ptr_is_null(load_ptr(method, 8)):  # ml_meth
        return null()
    flags: int = load_i32(method, 16)
    if (flags & (0x0002)) != 0 and (flags & ((0x0001) | (0x0080))) != 0:
        captures = _method_keyword_capture(self_obj, method)
        if ptr_is_null(captures):
            return null()
        fn = py_func_new_named(
            _method_call_entry_addr(), captures, load_ptr(method, 0)
        )
        py_decref(captures)
        return _method_func_finish(fn, self_obj, method)
    if (flags & (0x0001)) != 0:
        captures = _method_capture(self_obj, method)
        if ptr_is_null(captures):
            return null()
        fn = py_func_new_named(
            _method_call_entry_addr(), captures, load_ptr(method, 0)
        )
        py_decref(captures)
        return _method_func_finish(fn, self_obj, method)
    if (
        (flags & ((0x0004) | (0x0008) | (0x0080))) != 0
        and (flags & (0x0002)) == 0
    ):
        captures = _method_capture(self_obj, method)
        if ptr_is_null(captures):
            return null()
        fn = py_func_new_named(
            _method_call_entry_addr(), captures, load_ptr(method, 0)
        )
        py_decref(captures)
        return _method_func_finish(fn, self_obj, method)
    return null()


@c_abi_typed_export("pcc_capi_method_call_entry", "ptr", ("ptr", "ptr"))
def pcc_capi_method_call_entry(captures, args) -> c_ptr:
    if (
        ptr_is_null(captures) or is_tagged_int(captures)
        or load_i32(captures, 8) != PY_TYPE_TUPLE or py_tuple_len(captures) != 3
    ):
        _type_error(cstr("invalid C method capture"))
        return null()
    if ptr_is_null(args):
        args = py_tuple_new(0)
        if ptr_is_null(args):
            return PyErr_NoMemory()
    elif is_tagged_int(args) or load_i32(args, 8) != PY_TYPE_TUPLE:
        _type_error(cstr("C method args must be a tuple"))
        return null()
    else:
        py_incref(args)

    self_obj = py_tuple_get(captures, 0)
    method_ptr_obj = py_tuple_get(captures, 1)
    flags_obj = py_tuple_get(captures, 2)
    if ptr_is_null(self_obj) or ptr_is_null(method_ptr_obj) or ptr_is_null(flags_obj):
        py_decref(args)
        if not ptr_is_null(self_obj):
            py_decref(self_obj)
        if not ptr_is_null(method_ptr_obj):
            py_decref(method_ptr_obj)
        if not ptr_is_null(flags_obj):
            py_decref(flags_obj)
        _type_error(cstr("invalid C method capture"))
        return null()

    method = int_to_ptr(py_int_value_i64(method_ptr_obj))
    flags: int = py_int_value_i64(flags_obj)
    nargs = py_tuple_len(args)
    result = null()
    args_ready: int = 1

    if (
        not ptr_is_null(method) and not ptr_is_null(load_ptr(method, 8))
        and (flags & (0x0002)) == 0
    ):
        prepared = pcc_capi_prepare_call_args(args)
        if ptr_is_null(prepared):
            args_ready = 0
        else:
            py_decref(args)
            args = prepared
            nargs = py_tuple_len(args)

    if args_ready == 0:
        if py_err_occurred() == 0:
            PyErr_NoMemory()
    elif ptr_is_null(method) or ptr_is_null(load_ptr(method, 8)):
        _type_error(cstr("invalid C method"))
    elif (flags & (0x0080)) != 0 and (flags & (0x0002)) != 0:
        if nargs != 2:
            _type_error(cstr("invalid C keyword method call"))
        else:
            call_args = py_tuple_get(args, 0)
            kwargs = py_tuple_get(args, 1)
            if (
                ptr_is_null(call_args) or ptr_is_null(kwargs)
                or is_tagged_int(call_args) or load_i32(call_args, 8) != PY_TYPE_TUPLE
                or (kwargs != _py_none()
                    and (is_tagged_int(kwargs) or load_i32(kwargs, 8) != PY_TYPE_DICT))
            ):
                _type_error(cstr("invalid C keyword method call"))
            else:
                result = _method_require_result(
                    _fast_keyword_call(self_obj, method, call_args, kwargs)
                )
            if not ptr_is_null(call_args):
                py_decref(call_args)
            if not ptr_is_null(kwargs):
                py_decref(kwargs)
    elif (flags & (0x0001)) != 0 and (flags & (0x0002)) != 0:
        if nargs != 2:
            _type_error(cstr("invalid C keyword method call"))
        else:
            call_args = py_tuple_get(args, 0)
            kwargs = py_tuple_get(args, 1)
            if (
                ptr_is_null(call_args) or ptr_is_null(kwargs)
                or is_tagged_int(call_args) or load_i32(call_args, 8) != PY_TYPE_TUPLE
                or (kwargs != _py_none()
                    and (is_tagged_int(kwargs) or load_i32(kwargs, 8) != PY_TYPE_DICT))
            ):
                _type_error(cstr("invalid C keyword method call"))
            else:
                result = _method_require_result(
                    call_ptr3(load_ptr(method, 8), self_obj, call_args, kwargs)
                )
            if not ptr_is_null(call_args):
                py_decref(call_args)
            if not ptr_is_null(kwargs):
                py_decref(kwargs)
    elif (flags & (0x0004)) != 0:
        if nargs != 0:
            _type_error(cstr("method takes no arguments"))
        else:
            result = _method_require_result(
                call_ptr1(load_ptr(method, 8), self_obj)
            )
    elif (flags & (0x0008)) != 0:
        if nargs != 1:
            _type_error(cstr("method takes exactly one argument"))
        else:
            arg = py_tuple_get(args, 0)
            result = _method_require_result(
                call_ptr2(load_ptr(method, 8), self_obj, arg)
            )
            if not ptr_is_null(arg):
                py_decref(arg)
    elif (flags & (0x0080)) != 0 and (flags & (0x0002)) == 0:
        vector = null()
        ok: int = 1
        if nargs > 0:
            vector = calloc(nargs, 8)
            if ptr_is_null(vector):
                PyErr_NoMemory()
                ok = 0
        i: int = 0
        while ok != 0 and i < nargs:
            item = py_tuple_get(args, i)
            if ptr_is_null(item):
                ok = 0
            else:
                store_ptr(vector, i * 8, item)
            i += 1
        if ok != 0:
            result = _method_require_result(
                call_ptr_ptr_ptr_i64_ptr(
                    load_ptr(method, 8), self_obj, vector, nargs, null()
                )
            )
        i = 0
        while i < nargs:
            if not ptr_is_null(vector):
                item2 = load_ptr(vector, i * 8)
                if not ptr_is_null(item2):
                    py_decref(item2)
            i += 1
        if not ptr_is_null(vector):
            free(vector)
    elif (flags & (0x0001)) != 0:
        result = _method_require_result(
            call_ptr2(load_ptr(method, 8), self_obj, args)
        )
    else:
        _type_error(cstr("unsupported C method flags"))

    py_decref(args)
    if not ptr_is_null(self_obj):
        py_decref(self_obj)
    if not ptr_is_null(method_ptr_obj):
        py_decref(method_ptr_obj)
    if not ptr_is_null(flags_obj):
        py_decref(flags_obj)
    return result




def _method_call_entry_addr() -> c_ptr:
    return function_addr("pcc_capi_method_call_entry")

def _fast_keyword_call(self_obj, method, call_args, kwargs) -> c_ptr:
    positional_count = py_tuple_len(call_args)
    if kwargs == _py_none():
        keyword_count = 0
    else:
        keyword_count = py_dict_len(kwargs)
    total_count = positional_count + keyword_count
    vector = null()
    items = null()
    keyword_names = null()
    result = null()
    ready: int = 1
    if total_count > 0:
        vector = calloc(total_count, 8)
        if ptr_is_null(vector):
            PyErr_NoMemory()
            ready = 0
    i: int = 0
    while ready != 0 and i < positional_count:
        item = py_tuple_get(call_args, i)
        if ptr_is_null(item):
            ready = 0
        else:
            store_ptr(vector, i * 8, item)
        i += 1
    if ready != 0 and keyword_count > 0:
        items = py_dict_items(kwargs)
        keyword_names = py_tuple_new(keyword_count)
        if (
            ptr_is_null(items) or ptr_is_null(keyword_names)
            or py_list_len(items) != keyword_count
        ):
            ready = 0
    i = 0
    while ready != 0 and i < keyword_count:
        pair = py_list_get(items, i)
        key = null()
        value = null()
        if (
            not ptr_is_null(pair) and not is_tagged_int(pair)
            and load_i32(pair, 8) == PY_TYPE_TUPLE and py_tuple_len(pair) == 2
        ):
            key = py_tuple_get(pair, 0)
            value = py_tuple_get(pair, 1)
        if ptr_is_null(key) or ptr_is_null(value) or is_tagged_int(key) or load_i32(key, 8) != PY_TYPE_STR:
            _type_error(cstr("keyword names must be strings"))
            ready = 0
        else:
            py_tuple_set_item(keyword_names, i, key)
            store_ptr(vector, (positional_count + i) * 8, value)
            value = null()
        if not ptr_is_null(key):
            py_decref(key)
        if not ptr_is_null(value):
            py_decref(value)
        if not ptr_is_null(pair):
            py_decref(pair)
        i += 1
    if ready != 0:
        # METH_FASTCALL|METH_KEYWORDS: nargs counts POSITIONAL args only; the
        # keyword values follow in the vector with names in keyword_names.
        result = call_ptr_ptr_ptr_i64_ptr(
            load_ptr(method, 8), self_obj, vector, positional_count, keyword_names
        )
    i = 0
    while i < total_count:
        if not ptr_is_null(vector):
            item3 = load_ptr(vector, i * 8)
            if not ptr_is_null(item3):
                py_decref(item3)
        i += 1
    if not ptr_is_null(vector):
        free(vector)
    if not ptr_is_null(items):
        py_decref(items)
    if not ptr_is_null(keyword_names):
        py_decref(keyword_names)
    return result


@c_abi_typed_export("pcc_capi_builtin_object_getattr", "ptr", ("ptr", "ptr"))
def pcc_capi_builtin_object_getattr(obj, name) -> c_ptr:
    if ptr_is_null(obj) or ptr_is_null(name) or is_tagged_int(obj):
        return null()
    if load_i32(obj, 8) == PY_TYPE_LIST and strcmp_c(name, cstr("sort")) == 0:  # PY_TYPE_LIST
        return pcc_capi_method_func_new(obj, _list_sort_def())
    return null()


# Persistent PyMethodDef: {"sort", _list_sort_method, METH_VARARGS|METH_KEYWORDS, NULL}
# Layout: ml_name@0, ml_meth@8, ml_flags@16, ml_doc@24.



define_global_cstr("pcc_capi_list_sort_name", "sort")
define_global_struct_words(
    "pcc_capi_list_sort_def",
    "pcc_capi_list_sort_name",  # ml_name
    0,  # ml_meth (patched at first use)
    3,  # ml_flags ((0x0001)|(0x0002))
    0,  # ml_doc
)



py_list_sort_call = extern("py_list_sort_call", (c_ptr, c_ptr, c_ptr), c_ptr)


@c_abi_typed_export("pcc_capi_list_sort_method_runtime", "ptr", ("ptr", "ptr", "ptr"))
def _list_sort_method(self_obj, args, kwargs) -> c_ptr:
    # CPython's list.sort (keyword-only key/reverse, stable reverse, the
    # receiver empty during the sort) is owned by py_list_sort_call.
    if ptr_is_null(self_obj) or load_i32(self_obj, 8) != PY_TYPE_LIST:  # PY_TYPE_LIST
        _type_error(cstr("list.sort receiver must be a list"))
        return null()
    return py_list_sort_call(self_obj, args, kwargs)

def _list_sort_def() -> c_ptr:
    store_ptr(ptr_add(global_addr("pcc_capi_list_sort_def"), 8), 0, function_addr("pcc_capi_list_sort_method_runtime"))
    return global_addr("pcc_capi_list_sort_def")




# --- PyMethod_New ----------------------------------------------------

py_instance_bind_method = extern("py_instance_bind_method", (c_ptr, c_ptr, c_ptr), c_ptr)


@c_abi_typed_export("PyMethod_New", "ptr", ("ptr", "ptr"))
def PyMethod_New(func, self) -> c_ptr:
    if ptr_is_null(func) or ptr_is_null(self):
        _type_error(cstr("PyMethod_New requires func and self"))
        return null()
    return py_instance_bind_method(func, self, null())
