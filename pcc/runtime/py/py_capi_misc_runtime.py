"""pcc-Python owners for small no-libpython C-API misc symbols.

Replaces the scattered trivial blocks of py_capi_shim.c: the exception
__cause__/__context__/__traceback__ setters, the single-interpreter state
pointers, the dict-proxy no-op view, and the buffer release helper.

Owned surface (stable C ABI names):

  PyException_SetCause, PyException_SetContext, PyException_SetTraceback,
  PyInterpreterState_Main, PyThreadState_Get, PyDictProxy_New,
  PyBuffer_Release

Constants (inlined per the pcc-Python runtime-module contract):
  PY_EXC_TYPEERROR = 3
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PY_TYPE_STR

from pcc.extern import c_abi_typed_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    abi_constant,
    call_i32_ptr1,
    call_i64_ptr_i64_i64_ptr,
    cstr,
    darwin_libsystem_symbol,
    define_global_i64_array,
    define_global_i8,
    define_global_ptr_null,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_i8,
    load_ptr,
    memset,
    null,
    ptr_eq,
    ptr_is_null,
    stack_alloc,
    store_i64,
    store_i8,
    store_ptr,
)

py_decref = extern("py_decref", (c_ptr,), c_void)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_exc_set_cause = extern("py_exc_set_cause", (c_ptr, c_ptr), c_void)
py_exc_set_context = extern("py_exc_set_context", (c_ptr, c_ptr), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
free_c = extern("free", (c_ptr,), c_void)
pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)

# One main interpreter + thread state (single-interpreter no-libpython path).
# The fake-libc opaque PyThreadState / PyInterpreterState pointers are
# ABI-compatible void* returns; interp is the first field at offset 0.
define_global_i8("pcc_capi_main_interp", 0)
define_global_i64_array("pcc_capi_main_tstate_storage", 0)


def _type_error(message) -> None:
    py_raise_owned(py_exc_new(3, message))  # PY_EXC_TYPEERROR


@c_abi_typed_export("PyException_SetCause", "void", ("ptr", "ptr"))
def PyException_SetCause(self_obj, cause) -> None:
    py_exc_set_cause(self_obj, cause)


@c_abi_typed_export("PyException_SetContext", "void", ("ptr", "ptr"))
def PyException_SetContext(self_obj, context) -> None:
    py_exc_set_context(self_obj, context)


@c_abi_typed_export("PyException_SetTraceback", "i32", ("ptr", "ptr"))
def PyException_SetTraceback(self_obj, tb) -> int:
    # pcc has no traceback object (no Itanium-style unwinding); nothing to
    # attach, report success.
    return 0


@c_abi_typed_export("PyInterpreterState_Main", "ptr", ())
def PyInterpreterState_Main() -> c_ptr:
    return global_addr("pcc_capi_main_interp")


@c_abi_typed_export("PyThreadState_Get", "ptr", ())
def PyThreadState_Get() -> c_ptr:
    # The fake-libc opaque PyThreadState layout has `interp` as its first
    # field (offset 0) pointing at the main interpreter.  The storage global
    # is 8 bytes (interp pointer); stamp it on every call so the field is
    # never left zeroed (module-top initializers do not run in library mode).
    storage = global_addr("pcc_capi_main_tstate_storage")
    store_ptr(storage, 0, global_addr("pcc_capi_main_interp"))
    return storage


@c_abi_typed_export("PyDictProxy_New", "ptr", ("ptr",))
def PyDictProxy_New(mapping) -> c_ptr:
    if ptr_is_null(mapping):
        _type_error(cstr("PyDictProxy_New requires a mapping"))
        return null()
    py_incref(mapping)
    return mapping


@c_abi_typed_export("PyBuffer_Release", "void", ("ptr",))
def PyBuffer_Release(view) -> None:
    if ptr_is_null(view):
        return
    internal = load_ptr(view, 72)  # Py_buffer.internal
    if not ptr_is_null(internal):
        lease_owner = load_ptr(internal, 16)
        view_owner = load_ptr(internal, 24)
        if not ptr_is_null(lease_owner):
            pcc_py_gc_minor_graph_lock()
            node = global_load_ptr("pcc_capi_buffer_lease_head")
            previous = null()
            while not ptr_is_null(node) and ptr_eq(node, internal) == 0:
                previous = node
                node = load_ptr(node, 32)
            if ptr_eq(node, internal):
                nxt = load_ptr(internal, 32)
                if ptr_is_null(previous):
                    global_store_ptr("pcc_capi_buffer_lease_head", nxt)
                else:
                    store_ptr(previous, 32, nxt)
            scan = global_load_ptr("pcc_capi_buffer_lease_head")
            owner_still_leased: int = 0
            view_still_leased: int = 0
            while not ptr_is_null(scan):
                if ptr_eq(load_ptr(scan, 16), lease_owner) or ptr_eq(load_ptr(scan, 24), lease_owner):
                    owner_still_leased = 1
                if ptr_eq(load_ptr(scan, 16), view_owner) or ptr_eq(load_ptr(scan, 24), view_owner):
                    view_still_leased = 1
                scan = load_ptr(scan, 32)
            if owner_still_leased == 0:
                pcc_gc_unpin(lease_owner)
            if ptr_eq(view_owner, lease_owner) == 0 and view_still_leased == 0:
                pcc_gc_unpin(view_owner)
            pcc_py_gc_minor_graph_unlock()
    obj = load_ptr(view, 8)  # Py_buffer.obj at offset 8 (buf at 0)
    if not ptr_is_null(obj):
        py_decref(obj)
    if not ptr_is_null(internal):
        free_c(internal)
    memset(view, 0, 80)


define_global_ptr_null("pcc_capi_sys_flags")


# --- Py_GenericAlias / PyTuple_GetSlice ------------------------------

py_incref = extern("py_incref", (c_ptr,), c_void)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
PyTuple_Size = extern("PyTuple_Size", (c_ptr,), c_int64)
PyTuple_New = extern("PyTuple_New", (c_int64,), c_ptr)
PyTuple_GetItem = extern("PyTuple_GetItem", (c_ptr, c_int64), c_ptr)


@c_abi_typed_export("Py_GenericAlias", "ptr", ("ptr", "ptr"))
def Py_GenericAlias(origin, args) -> c_ptr:
    if not ptr_is_null(origin):
        py_incref(origin)
    return origin


@c_abi_typed_export("PyTuple_GetSlice", "ptr", ("ptr", "i64", "i64"))
def PyTuple_GetSlice(tuple_obj, lo: int, hi: int) -> c_ptr:
    if ptr_is_null(tuple_obj):
        return null()
    n = PyTuple_Size(tuple_obj)
    if lo < 0:
        lo = 0
    if hi > n:
        hi = n
    if hi < lo:
        hi = lo
    result = PyTuple_New(hi - lo)
    if ptr_is_null(result):
        return null()
    i: int = lo
    while i < hi:
        item = PyTuple_GetItem(tuple_obj, i)
        py_incref(item)
        py_tuple_set_item(result, i - lo, item)
        i += 1
    return result


# --- PyUnicode_Format / PySys_GetObject -------------------------------

py_str_mod = extern("py_str_mod", (c_ptr, c_ptr), c_ptr)
py_class_new = extern("py_class_new", (c_ptr, c_ptr, c_int64, c_ptr, c_int64), c_ptr)
PyLong_FromLong = extern("PyLong_FromLong", (c_int64,), c_ptr)
PyUnicode_FromString = extern("PyUnicode_FromString", (c_ptr,), c_ptr)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
py_obj_setattr = extern("py_obj_setattr", (c_ptr, c_ptr, c_ptr), c_int64)
py_dict_set = extern("py_dict_set", (c_ptr, c_ptr, c_ptr), c_int64)


@c_abi_typed_export("PyUnicode_Format", "ptr", ("ptr", "ptr"))
def PyUnicode_Format(format_obj, args) -> c_ptr:
    if ptr_is_null(format_obj):
        py_raise_owned(py_exc_new(3, cstr("PyUnicode_Format requires a format")))
        return null()
    return py_str_mod(format_obj, args)


def _sys_flags_class() -> c_ptr:
    cls = py_class_new(cstr("sys.flags"), null(), 0, null(), 0)
    if ptr_is_null(cls):
        return null()
    pcc_gc_pin(cls)
    zero = PyLong_FromLong(0)
    if not ptr_is_null(zero):
        # py_obj_setattr takes a C string name, not a py str object.
        py_obj_setattr(cls, cstr("optimize"), zero)
        py_decref(zero)
    return cls


@c_abi_typed_export("PySys_GetObject", "ptr", ("ptr",))
def PySys_GetObject(name) -> c_ptr:
    if ptr_is_null(name):
        return null()
    if _cstr_eq(name, cstr("flags")) != 0:
        flags = global_addr("pcc_capi_sys_flags")
        current = load_ptr(flags, 0)
        if ptr_is_null(current):
            cls = _sys_flags_class()
            if ptr_is_null(cls):
                return null()
            store_ptr(flags, 0, cls)
            current = cls
        return current
    return null()


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


# --- PyErr_SetFromErrno / WithFilenameObject -------------------------

pcc_errno_get = extern("pcc_errno_get", (), c_int32)
pcc_errno_message_into = extern(
    "pcc_errno_message_into", (c_int32, c_ptr, c_int64), c_int32
)
PyOS_snprintf = extern(
    "PyOS_snprintf",
    (c_ptr, c_int64, c_ptr),
    c_int32,
    variadic=True,
)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)


def _concat_errno_path(buf, cap: int, err, path) -> None:
    i: int = 0
    while load_i8(err, i) != 0 and i < cap - 1:
        store_i8(buf, i, load_i8(err, i))
        i += 1
    if i < cap - 2:
        store_i8(buf, i, 58)  # ':'
        i += 1
        store_i8(buf, i, 32)  # ' '
        i += 1
    j: int = 0
    while load_i8(path, j) != 0 and i < cap - 1:
        store_i8(buf, i, load_i8(path, j))
        i += 1
        j += 1
    store_i8(buf, i, 0)


PyErr_SetString = extern("PyErr_SetString", (c_ptr, c_ptr), c_void)


def py_err_set_string_direct(type_obj, message) -> None:
    PyErr_SetString(type_obj, message)


@c_abi_typed_export("PyErr_SetFromErrno", "ptr", ("ptr",))
def PyErr_SetFromErrno(type_obj) -> c_ptr:
    # Snapshot thread errno before formatting or lazy libSystem lookup.
    saved_errno: int = pcc_errno_get()
    message = stack_alloc(2048)
    pcc_errno_message_into(saved_errno, message, 2048)
    py_err_set_string_direct(type_obj, message)
    return null()


@c_abi_typed_export("PyErr_SetFromErrnoWithFilenameObject", "ptr", ("ptr", "ptr"))
def PyErr_SetFromErrnoWithFilenameObject(type_obj, filename_object) -> c_ptr:
    # Keep the operation's errno stable across path inspection and the first
    # possible libSystem symbol resolution.
    saved_errno: int = pcc_errno_get()
    err = stack_alloc(2048)
    pcc_errno_message_into(saved_errno, err, 2048)
    path = null()
    if not ptr_is_null(filename_object):
        if not is_tagged_int(filename_object) and load_i32(filename_object, 8) == PY_TYPE_STR:
            path = py_str_utf8(filename_object)
    if not ptr_is_null(path) and load_i8(path, 0) != 0:
        buf = stack_alloc(2048)
        _concat_errno_path(buf, 2048, err, path)
        py_err_set_string_direct(type_obj, buf)
    else:
        py_err_set_string_direct(type_obj, err)
    return null()


# --- PyObject_Print --------------------------------------------------

fwrite_c = extern("fwrite", (c_ptr, c_int64, c_int64, c_ptr), c_int64)
fflush_c = extern("fflush", (c_ptr,), c_int32)
PyObject_Str = extern("PyObject_Str", (c_ptr,), c_ptr)
PyObject_Repr = extern("PyObject_Repr", (c_ptr,), c_ptr)
PyUnicode_AsUTF8AndSize = extern("PyUnicode_AsUTF8AndSize", (c_ptr, c_ptr), c_ptr)


def _is_pcc_owned_file(fp) -> int:
    if ptr_is_null(fp):
        return 0
    if load_i64(fp, abi_constant("stdio.file.magic_offset")) == abi_constant(
        "stdio.file.magic"
    ):
        return 1
    return 0


@c_abi_typed_export("pcc_capi_file_write", "i64", ("ptr", "i64", "ptr"))
def pcc_capi_file_write(data, n: int, fp) -> int:
    if ptr_is_null(fp) or (ptr_is_null(data) and n > 0):
        return 0
    if n <= 0:
        return 0
    if _is_pcc_owned_file(fp) != 0:
        return fwrite_c(data, 1, n, fp)

    # A Darwin host-compiled extension owns a libSystem FILE*.  Resolve the
    # named owner explicitly so the call cannot interpose to pcc's same-named
    # freestanding fwrite.  On Linux the compiler emits NULL here and no
    # dynamic-loader import, preserving the zero-libc boundary.
    host_fwrite = darwin_libsystem_symbol(cstr("fwrite"))
    if ptr_is_null(host_fwrite):
        return 0
    return call_i64_ptr_i64_i64_ptr(host_fwrite, data, 1, n, fp)


@c_abi_typed_export("pcc_capi_file_flush", "i32", ("ptr",))
def pcc_capi_file_flush(fp) -> int:
    if ptr_is_null(fp):
        return -1
    if _is_pcc_owned_file(fp) != 0:
        return fflush_c(fp)
    host_fflush = darwin_libsystem_symbol(cstr("fflush"))
    if ptr_is_null(host_fflush):
        return -1
    return call_i32_ptr1(host_fflush, fp)


@c_abi_typed_export("PyObject_Print", "i32", ("ptr", "ptr", "i32"))
def PyObject_Print(obj, fp, flags: int) -> int:
    if ptr_is_null(fp):
        py_raise_owned(py_exc_new(3, cstr("NULL FILE pointer")))  # PY_EXC_TYPEERROR
        return -1
    if (flags & (1)) != 0:
        text = PyObject_Str(obj)
    else:
        text = PyObject_Repr(obj)
    if ptr_is_null(text):
        return -1
    size_slot = stack_alloc(8)
    store_i64(size_slot, 0, 0)
    raw = PyUnicode_AsUTF8AndSize(text, size_slot)
    if ptr_is_null(raw):
        py_decref(text)
        return -1
    written = pcc_capi_file_write(raw, load_i64(size_slot, 0), fp)
    py_decref(text)
    if written != load_i64(size_slot, 0):
        py_raise_owned(py_exc_new(7, cstr("failed to write object")))  # PY_EXC_OSERROR
        return -1
    pcc_capi_file_flush(fp)
    return 0
