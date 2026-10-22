"""pcc-Python owners for the no-libpython C-API import surface.

Replaces the PyImport_* block of py_capi_shim.c.  Delegates to the migrated
extension/compiled-module loaders (py_extension_loader_runtime /
py_compiled_module_runtime) and to PyUnicode_AsUTF8.

Owned surface (stable C ABI names):

  PyImport_ImportModule, PyImport_Import, py_builtin_import

Constants (inlined per the pcc-Python runtime-module contract):
  PY_EXC_RUNTIMEERROR = 7, PY_EXC_TYPEERROR = 3, PY_EXC_VALUEERROR = 2,
  PY_EXC_MODULENOTFOUNDERROR = 21
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import (
    C_POINTER_SIZE,
    PYOBJECTHEADER_FLAGS_OFFSET,
    PY_FLAG_GC_PINNED,
    PY_TYPE_STR,
)

from pcc.extern import c_abi_typed_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    cstr,
    define_global_i32,
    free,
    global_addr,
    global_load_ptr,
    is_tagged_int,
    load_i8,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    memcpy,
    memset,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    stack_alloc,
    store_i8,
    store_i64,
    store_ptr,
    strlen,
)

py_native_extension_import_by_name = extern(
    "py_native_extension_import_by_name", (c_ptr,), c_ptr
)
py_compiled_module_import_by_name = extern(
    "py_compiled_module_import_by_name", (c_ptr,), c_ptr
)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
PyUnicode_AsUTF8 = extern("PyUnicode_AsUTF8", (c_ptr,), c_ptr)


def _not_found_message(name) -> c_ptr:
    # "PCC-PYEXT-IMPORT-001 [pcc-native/no-libpython] module not found: <name>"
    prefix = cstr("PCC-PYEXT-IMPORT-001 [pcc-native/no-libpython] module not found: ")
    plen = strlen(prefix)
    nlen = strlen(name)
    buf = malloc(plen + nlen + 1)
    if ptr_is_null(buf):
        return null()
    memcpy(buf, prefix, plen)
    memcpy(ptr_add(buf, plen), name, nlen)
    store_i8(buf, plen + nlen, 0)
    return buf


def _python_not_found_message(name) -> c_ptr:
    prefix = cstr("No module named '")
    suffix = cstr("'")
    plen = strlen(prefix)
    nlen = strlen(name)
    slen = strlen(suffix)
    buf = malloc(plen + nlen + slen + 1)
    if ptr_is_null(buf):
        return null()
    memcpy(buf, prefix, plen)
    memcpy(ptr_add(buf, plen), name, nlen)
    memcpy(ptr_add(buf, plen + nlen), suffix, slen)
    store_i8(buf, plen + nlen + slen, 0)
    return buf


@c_abi_typed_export("PyImport_ImportModule", "ptr", ("ptr",))
def PyImport_ImportModule(name) -> c_ptr:
    if ptr_is_null(name) or load_i8(name, 0) == 0:
        py_raise_owned(py_exc_new(2, cstr("empty module name")))  # PY_EXC_VALUEERROR
        return null()
    module = py_native_extension_import_by_name(name)
    if ptr_is_null(module) and py_err_occurred() == 0:
        module = py_compiled_module_import_by_name(name)
    if ptr_is_null(module) and py_err_occurred() == 0:
        message = _not_found_message(name)
        if ptr_is_null(message):
            py_raise_owned(
                py_exc_new(7, cstr("PCC-PYEXT-IMPORT-001 module not found"))
            )
        else:
            exc = py_exc_new(7, message)
            free(message)
            py_raise_owned(exc)
    return module


# --- py_builtin_import ------------------------------------------------

py_type_of = extern("pcc_py_type_of", (c_ptr,), c_int64)
py_str_byte_len = extern("py_str_byte_len", (c_ptr,), c_int64)
py_obj_len = extern("py_obj_len", (c_ptr,), c_int64)
py_decref = extern("py_decref", (c_ptr,), c_void)
PyMem_Malloc = extern("PyMem_Malloc", (c_int64,), c_ptr)
PyMem_Free = extern("PyMem_Free", (c_ptr,), c_void)


# Borrowed inputs are registered before owner-copy operations may park. Each
# owned slot carries an independent counted lease, including NEW import results.
_IMPORT_NAME = 0
_IMPORT_FROMLIST = 1
_IMPORT_MODULE = 2
_IMPORT_TOP = 3
_IMPORT_ERROR = 4
_IMPORT_COUNT = 5

define_global_i32("pcc_capi_import_borrowed_map", -2)
define_global_i32("pcc_capi_import_owned_map", _IMPORT_COUNT)
pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
pcc_gc_root_copy_borrowed_lease = extern("pcc_gc_root_copy_borrowed_lease", (c_ptr, c_ptr), c_int64)
pcc_gc_foreign_lease_acquire = extern("pcc_gc_foreign_lease_acquire", (c_ptr,), c_int64)
pcc_gc_foreign_lease_release = extern("pcc_gc_foreign_lease_release", (c_ptr, c_int64), c_int64)
pcc_gc_note_slot_write_barrier = extern("pcc_gc_note_slot_write_barrier", (c_ptr, c_ptr, c_ptr), c_void)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_take_pinned_slot = extern("pcc_gc_take_pinned_slot", (c_ptr, c_int64), c_ptr)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
py_tls_exc_swap_slot = extern("py_tls_exc_swap_slot", (c_ptr,), c_void)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_runtime_error_if_unset = extern("py_runtime_error_if_unset", (c_ptr, c_ptr), c_ptr)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)


def _import_adopt(slots, tokens, index: int) -> int:
    # NEW must already be stored before lease acquisition can park.
    offset: int = index * C_POINTER_SIZE
    slot = ptr_add(slots, offset)
    token: int = pcc_gc_foreign_lease_acquire(slot)
    if token < 0:
        py_runtime_error_if_unset(cstr("import"), cstr("result owner lease failed"))
        return -1
    store_i64(tokens, offset, token)
    pcc_py_gc_minor_graph_lock()
    pcc_gc_note_slot_write_barrier(null(), slot, load_ptr(slot, 0))
    pcc_py_gc_minor_graph_unlock()
    return 0


def _import_copy_inputs(slots, tokens, borrowed) -> int:
    index: int = 0
    while index < 2:
        offset: int = index * C_POINTER_SIZE
        token: int = pcc_gc_root_copy_borrowed_lease(
            ptr_add(slots, offset), ptr_add(borrowed, offset),
        )
        if token < 0:
            py_runtime_error_if_unset(cstr("import"), cstr("argument owner copy failed"))
            return -1
        store_i64(tokens, offset, token)
        index = index + 1
    return 0


def _import_finish(slots, tokens, borrowed, keep: int) -> c_ptr:
    py_tls_exc_swap_slot(ptr_add(slots, _IMPORT_ERROR * C_POINTER_SIZE))
    store_ptr(borrowed, 0, null())
    store_ptr(borrowed, C_POINTER_SIZE, null())
    index: int = 0
    while index < _IMPORT_ERROR:
        if index != keep:
            offset: int = index * C_POINTER_SIZE
            slot = ptr_add(slots, offset)
            if pcc_gc_foreign_lease_release(slot, load_i64(tokens, offset)) != 0:
                pcc_platform_abort()
                return null()
            pcc_gc_store_root(slot, null())
        index = index + 1
    py_clear_exception()
    py_tls_exc_swap_slot(ptr_add(slots, _IMPORT_ERROR * C_POINTER_SIZE))
    if keep < 0:
        pcc_gc_frame_leave(slots)
        pcc_gc_frame_leave(borrowed)
        return null()
    result_slot = ptr_add(slots, keep * C_POINTER_SIZE)
    # A temporary terminal pin spans frame removal only; restore its old bit.
    pcc_py_gc_minor_graph_lock()
    result = pcc_gc_load_ptr(null(), result_slot)
    prior: int = 0
    if not ptr_is_null(result) and not is_tagged_int(result):
        prior = load_i32(result, PYOBJECTHEADER_FLAGS_OFFSET) & PY_FLAG_GC_PINNED
        pcc_gc_pin(result)
    pcc_py_gc_minor_graph_unlock()
    if pcc_gc_foreign_lease_release(result_slot, load_i64(tokens, keep * C_POINTER_SIZE)) != 0:
        pcc_platform_abort()
        return null()
    pcc_gc_frame_leave(slots)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(result_slot, prior)


def _builtin_import_body(slots, tokens) -> int:
    name = load_ptr(slots, _IMPORT_NAME * C_POINTER_SIZE)
    if ptr_is_null(name) or is_tagged_int(name) or py_type_of(name) != PY_TYPE_STR:
        py_raise_owned(py_exc_new(3, cstr("module name must be a string")))
        return -1
    cname = py_str_utf8(name)
    if ptr_is_null(cname):
        return -1
    if py_str_byte_len(name) == 0:
        py_raise_owned(py_exc_new(2, cstr("Empty module name")))
        return -1
    if strlen(cname) != py_str_byte_len(name):
        py_raise_owned(py_exc_new(2, cstr("module name contains a null character")))
        return -1
    # The string lease also protects its borrowed UTF-8 buffer through loaders.
    # Python-level __import__ retains its ModuleNotFoundError diagnostic.
    store_ptr(slots, _IMPORT_MODULE * C_POINTER_SIZE, py_native_extension_import_by_name(cname))
    if _import_adopt(slots, tokens, _IMPORT_MODULE) != 0:
        return -1
    if ptr_is_null(load_ptr(slots, _IMPORT_MODULE * C_POINTER_SIZE)) and py_err_occurred() == 0:
        store_ptr(slots, _IMPORT_MODULE * C_POINTER_SIZE, py_compiled_module_import_by_name(cname))
        if _import_adopt(slots, tokens, _IMPORT_MODULE) != 0:
            return -1
    if ptr_is_null(load_ptr(slots, _IMPORT_MODULE * C_POINTER_SIZE)):
        if py_err_occurred() == 0:
            message = _python_not_found_message(cname)
            if ptr_is_null(message):
                py_raise_owned(py_exc_new(21, cstr("module not found")))
            else:
                exc = py_exc_new(21, message)
                free(message)
                py_raise_owned(exc)
        return -1
    fromlist = load_ptr(slots, _IMPORT_FROMLIST * C_POINTER_SIZE)
    fromlist_count: int = 0
    if not ptr_is_null(fromlist) and not ptr_eq(fromlist, global_load_ptr("py_None")):
        fromlist_count = py_obj_len(fromlist)
        if fromlist_count < 0:
            return -1
    if fromlist_count > 0:
        return _IMPORT_MODULE
    dot = _cstr_find_char(cname, 46)  # '.'
    if dot < 0:
        return _IMPORT_MODULE
    top_len: int = dot
    top_name = PyMem_Malloc(top_len + 1)
    if ptr_is_null(top_name):
        py_raise_owned(py_exc_new(19, cstr("out of memory importing module")))
        return -1
    memcpy(top_name, cname, top_len)
    store_i8(top_name, top_len, 0)
    store_ptr(slots, _IMPORT_TOP * C_POINTER_SIZE, PyImport_ImportModule(top_name))
    status: int = _import_adopt(slots, tokens, _IMPORT_TOP)
    PyMem_Free(top_name)
    if status != 0 or ptr_is_null(load_ptr(slots, _IMPORT_TOP * C_POINTER_SIZE)):
        return -1
    return _IMPORT_TOP


def _import_with_roots(name, fromlist, builtin: int) -> c_ptr:
    borrowed = stack_alloc(2 * C_POINTER_SIZE)
    store_ptr(borrowed, 0, name)
    store_ptr(borrowed, C_POINTER_SIZE, fromlist)
    pcc_gc_frame_enter(global_addr("pcc_capi_import_borrowed_map"), borrowed)
    slots = stack_alloc(_IMPORT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_IMPORT_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _IMPORT_COUNT * C_POINTER_SIZE)
    memset(tokens, 0, _IMPORT_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_capi_import_owned_map"), slots)
    keep: int = -1
    if _import_copy_inputs(slots, tokens, borrowed) == 0:
        if builtin != 0:
            keep = _builtin_import_body(slots, tokens)
        else:
            name = load_ptr(slots, _IMPORT_NAME * C_POINTER_SIZE)
            if ptr_is_null(name):
                py_raise_owned(py_exc_new(3, cstr("import name required")))
            else:
                cname = PyUnicode_AsUTF8(name)
                if not ptr_is_null(cname):
                    store_ptr(slots, _IMPORT_MODULE * C_POINTER_SIZE, PyImport_ImportModule(cname))
                    if _import_adopt(slots, tokens, _IMPORT_MODULE) == 0:
                        keep = _IMPORT_MODULE
    return _import_finish(slots, tokens, borrowed, keep)


@c_abi_typed_export("PyImport_Import", "ptr", ("ptr",))
def PyImport_Import(name) -> c_ptr:
    return _import_with_roots(name, null(), 0)


@c_abi_typed_export("py_builtin_import", "ptr", ("ptr", "ptr"))
def py_builtin_import(name, fromlist) -> c_ptr:
    return _import_with_roots(name, fromlist, 1)


def _cstr_find_char(s, ch: int) -> int:
    i: int = 0
    while True:
        c: int = load_i8(s, i)
        if c == 0:
            return -1
        if c == ch:
            return i
        i += 1
