"""pcc-Python owners for the no-libpython C-API dict surface.

Replaces the PyDict_* block of py_capi_shim.c.  Every function delegates to
the existing pcc-Python dict ABIs (py_dict_new/get/set/del/...), preserving
the CPython refcount contracts the shim implemented (borrowed GetItem,
owned GetItemRef/Pop, KeyError-on-missing, TypeError on non-dict input).
PyDict_Next walks the raw compact-dict entries array through the
py_dict_entry_key_at / py_dict_entry_value_at helpers.

The PyDict_Type recognition token stays in the C shim with the rest of the
builtin type-object data tokens (a later slice needs a mixed word/pointer
raw-layout global intrinsic).

Owned surface (stable C ABI names):
  PyDict_New, PyDict_SetItem, PyDict_SetItemString, PyDict_GetItem,
  PyDict_GetItemString, PyDict_GetItemWithError, PyDict_GetItemRef,
  PyDict_GetItemStringRef, PyDict_SetDefaultRef, PyDict_Pop,
  PyDict_PopString, PyDict_DelItem, PyDict_DelItemString, PyDict_Size,
  PyDict_Contains, PyDict_ContainsString, PyDict_Next, PyDict_Keys,
  PyDict_Values, PyDict_Items, PyDict_Clear, PyDict_Check,
  PyDict_CheckExact, PyDict_Copy, PyDict_Merge

Public object type tags come from the generated ``py_abi_constants`` module.
Private exception codes remain owned by the dictionary C-API contract.
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PY_TYPE_DICT

from pcc.extern import (
    c_abi_typed_export,
    c_int64,
    c_ptr,
    c_void,
    extern,
)
from pcc.unsafe import (
    cstr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    null,
    ptr_is_null,
    stack_alloc,
    store_i64,
    store_ptr,
    strlen,
)

py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_dict_new = extern("py_dict_new", (), c_ptr)
py_dict_set = extern("py_dict_set", (c_ptr, c_ptr, c_ptr), c_void)
py_dict_get = extern("py_dict_get", (c_ptr, c_ptr), c_ptr)
py_dict_contains = extern("py_dict_contains", (c_ptr, c_ptr), c_int64)
py_dict_del = extern("py_dict_del", (c_ptr, c_ptr), c_int64)
py_dict_clear = extern("py_dict_clear", (c_ptr,), c_void)
py_dict_len = extern("py_dict_len", (c_ptr,), c_int64)
py_dict_keys = extern("py_dict_keys", (c_ptr,), c_ptr)
py_dict_values = extern("py_dict_values", (c_ptr,), c_ptr)
py_dict_items = extern("py_dict_items", (c_ptr,), c_ptr)
py_dict_update = extern("py_dict_update", (c_ptr, c_ptr), c_void)
py_dict_entries_used = extern("py_dict_entries_used", (c_ptr,), c_int64)
py_dict_entry_key_at = extern("py_dict_entry_key_at", (c_ptr, c_int64), c_ptr)
py_dict_entry_value_at = extern("py_dict_entry_value_at", (c_ptr, c_int64), c_ptr)


def _is_dict(obj) -> int:
    if ptr_is_null(obj):
        return 0
    if is_tagged_int(obj):
        return 0
    if load_i32(obj, 8) == PY_TYPE_DICT:  # PY_TYPE_DICT
        return 1
    return 0


def _type_error(message) -> None:
    py_raise_owned(py_exc_new(3, message))  # PY_EXC_TYPEERROR


def _str_from_cstr(text):
    if ptr_is_null(text):
        text = cstr("")
    return py_str_new(text, strlen(text))


@c_abi_typed_export("PyDict_New", "ptr", ())
def PyDict_New():
    return py_dict_new()


@c_abi_typed_export("PyDict_SetItem", "i32", ("ptr", "ptr", "ptr"))
def PyDict_SetItem(dict, key, value) -> int:
    if _is_dict(dict) == 0 or ptr_is_null(key) or ptr_is_null(value):
        _type_error(cstr("invalid PyDict_SetItem call"))
        return -1
    py_dict_set(dict, key, value)
    if py_err_occurred() != 0:
        return -1
    return 0


@c_abi_typed_export("PyDict_SetItemString", "i32", ("ptr", "ptr", "ptr"))
def PyDict_SetItemString(dict, key, value) -> int:
    if ptr_is_null(key):
        _type_error(cstr("NULL dict key"))
        return -1
    key_obj = _str_from_cstr(key)
    if ptr_is_null(key_obj):
        return -1
    rc: int = PyDict_SetItem(dict, key_obj, value)
    py_decref(key_obj)
    return rc


@c_abi_typed_export("PyDict_GetItem", "ptr", ("ptr", "ptr"))
def PyDict_GetItem(dict, key):
    if _is_dict(dict) == 0 or ptr_is_null(key):
        py_clear_exception()
        return null()
    item = py_dict_get(dict, key)
    if ptr_is_null(item):
        return null()
    # py_dict_get returns owned; CPython PyDict_GetItem returns borrowed.
    pcc_gc_pin(item)
    py_decref(item)
    return item


@c_abi_typed_export("PyDict_GetItemString", "ptr", ("ptr", "ptr"))
def PyDict_GetItemString(dict, key):
    if ptr_is_null(key):
        return null()
    key_obj = _str_from_cstr(key)
    if ptr_is_null(key_obj):
        return null()
    item = PyDict_GetItem(dict, key_obj)
    py_decref(key_obj)
    return item


@c_abi_typed_export("PyDict_GetItemWithError", "ptr", ("ptr", "ptr"))
def PyDict_GetItemWithError(dict, key):
    if _is_dict(dict) == 0 or ptr_is_null(key):
        _type_error(cstr("invalid PyDict_GetItemWithError call"))
        return null()
    item = py_dict_get(dict, key)
    if ptr_is_null(item):
        return null()
    pcc_gc_pin(item)
    py_decref(item)
    return item


@c_abi_typed_export("PyDict_GetItemRef", "i32", ("ptr", "ptr", "ptr"))
def PyDict_GetItemRef(dict, key, result) -> int:
    if _is_dict(dict) == 0 or ptr_is_null(key) or ptr_is_null(result):
        _type_error(cstr("invalid PyDict_GetItemRef call"))
        return -1
    item = py_dict_get(dict, key)
    if ptr_is_null(item):
        store_ptr(result, 0, null())
        if py_err_occurred() != 0:
            return -1
        return 0
    store_ptr(result, 0, item)
    return 1


@c_abi_typed_export("PyDict_GetItemStringRef", "i32", ("ptr", "ptr", "ptr"))
def PyDict_GetItemStringRef(dict, key, result) -> int:
    if ptr_is_null(key):
        _type_error(cstr("NULL dict key"))
        return -1
    key_obj = _str_from_cstr(key)
    if ptr_is_null(key_obj):
        return -1
    rc: int = PyDict_GetItemRef(dict, key_obj, result)
    py_decref(key_obj)
    return rc


@c_abi_typed_export("PyDict_SetDefaultRef", "i32", ("ptr", "ptr", "ptr", "ptr"))
def PyDict_SetDefaultRef(dict, key, default_value, result) -> int:
    if ptr_is_null(default_value):
        _type_error(cstr("NULL default value"))
        if not ptr_is_null(result):
            store_ptr(result, 0, null())
        return -1
    scratch = stack_alloc(8)
    store_ptr(scratch, 0, null())
    rc: int = PyDict_GetItemRef(dict, key, scratch)
    item = load_ptr(scratch, 0)
    if rc < 0:
        if not ptr_is_null(result):
            store_ptr(result, 0, null())
        return -1
    if rc > 0:
        if not ptr_is_null(result):
            store_ptr(result, 0, item)
        else:
            py_decref(item)
        return 1
    if PyDict_SetItem(dict, key, default_value) != 0:
        if not ptr_is_null(result):
            store_ptr(result, 0, null())
        return -1
    if not ptr_is_null(result):
        py_incref(default_value)
        store_ptr(result, 0, default_value)
    return 0


@c_abi_typed_export("PyDict_Pop", "i32", ("ptr", "ptr", "ptr"))
def PyDict_Pop(dict, key, result) -> int:
    if _is_dict(dict) == 0 or ptr_is_null(key):
        _type_error(cstr("invalid PyDict_Pop call"))
        return -1
    item = py_dict_get(dict, key)
    if ptr_is_null(item):
        if not ptr_is_null(result):
            store_ptr(result, 0, null())
        if py_err_occurred() != 0:
            return -1
        return 0
    rc: int = py_dict_del(dict, key)
    if rc != 0:
        py_decref(item)
        if not ptr_is_null(result):
            store_ptr(result, 0, null())
        if py_err_occurred() == 0:
            py_raise_owned(py_exc_new(4, cstr("missing dict key")))  # PY_EXC_KEYERROR
        return -1
    if not ptr_is_null(result):
        store_ptr(result, 0, item)
    else:
        py_decref(item)
    return 1


@c_abi_typed_export("PyDict_PopString", "i32", ("ptr", "ptr", "ptr"))
def PyDict_PopString(dict, key, result) -> int:
    if ptr_is_null(key):
        _type_error(cstr("NULL dict key"))
        return -1
    key_obj = _str_from_cstr(key)
    if ptr_is_null(key_obj):
        return -1
    rc: int = PyDict_Pop(dict, key_obj, result)
    py_decref(key_obj)
    return rc


@c_abi_typed_export("PyDict_DelItem", "i32", ("ptr", "ptr"))
def PyDict_DelItem(dict, key) -> int:
    if _is_dict(dict) == 0 or ptr_is_null(key):
        _type_error(cstr("invalid PyDict_DelItem call"))
        return -1
    rc: int = py_dict_del(dict, key)
    if rc != 0 and py_err_occurred() == 0:
        py_raise_owned(py_exc_new(4, cstr("missing dict key")))  # PY_EXC_KEYERROR
    if rc == 0:
        return 0
    return -1


@c_abi_typed_export("PyDict_DelItemString", "i32", ("ptr", "ptr"))
def PyDict_DelItemString(dict, key) -> int:
    if _is_dict(dict) == 0:
        _type_error(cstr("invalid PyDict_DelItemString call"))
        return -1
    if ptr_is_null(key):
        _type_error(cstr("NULL dict key"))
        return -1
    key_obj = _str_from_cstr(key)
    if ptr_is_null(key_obj):
        return -1
    rc: int = PyDict_DelItem(dict, key_obj)
    py_decref(key_obj)
    return rc


@c_abi_typed_export("PyDict_Size", "i64", ("ptr",))
def PyDict_Size(dict) -> int:
    if _is_dict(dict) == 0:
        _type_error(cstr("invalid PyDict_Size call"))
        return -1
    return py_dict_len(dict)


@c_abi_typed_export("PyDict_Contains", "i32", ("ptr", "ptr"))
def PyDict_Contains(dict, key) -> int:
    if _is_dict(dict) == 0 or ptr_is_null(key):
        _type_error(cstr("invalid PyDict_Contains call"))
        return -1
    rc: int = py_dict_contains(dict, key)
    if py_err_occurred() != 0:
        return -1
    if rc != 0:
        return 1
    return 0


@c_abi_typed_export("PyDict_ContainsString", "i32", ("ptr", "ptr"))
def PyDict_ContainsString(dict, key) -> int:
    if ptr_is_null(key):
        _type_error(cstr("NULL dict key"))
        return -1
    key_obj = _str_from_cstr(key)
    if ptr_is_null(key_obj):
        return -1
    rc: int = PyDict_Contains(dict, key_obj)
    py_decref(key_obj)
    return rc


@c_abi_typed_export("PyDict_Next", "i32", ("ptr", "ptr", "ptr", "ptr"))
def PyDict_Next(dict, pos, key_out, value_out) -> int:
    if _is_dict(dict) == 0 or ptr_is_null(pos):
        _type_error(cstr("invalid PyDict_Next call"))
        return 0
    i: int = load_i64(pos, 0)
    if i < 0:
        i = 0
    entries_used: int = py_dict_entries_used(dict)
    while i < entries_used:
        key = py_dict_entry_key_at(dict, i)
        if ptr_is_null(key):
            i = i + 1
            continue
        value = py_dict_entry_value_at(dict, i)
        if ptr_is_null(value):
            py_decref(key)
            i = i + 1
            continue
        i = i + 1
        store_i64(pos, 0, i)
        # PyDict_Next returns borrowed references: hand the caller the raw
        # pointers (the dict owns them) and release the owned copies.
        if not ptr_is_null(key_out):
            store_ptr(key_out, 0, key)
        py_decref(key)
        if not ptr_is_null(value_out):
            store_ptr(value_out, 0, value)
        py_decref(value)
        return 1
    return 0


@c_abi_typed_export("PyDict_Keys", "ptr", ("ptr",))
def PyDict_Keys(dict):
    if _is_dict(dict) == 0:
        _type_error(cstr("expected dict"))
        return null()
    return py_dict_keys(dict)


@c_abi_typed_export("PyDict_Values", "ptr", ("ptr",))
def PyDict_Values(dict):
    if _is_dict(dict) == 0:
        _type_error(cstr("expected dict"))
        return null()
    return py_dict_values(dict)


@c_abi_typed_export("PyDict_Items", "ptr", ("ptr",))
def PyDict_Items(dict):
    if _is_dict(dict) == 0:
        _type_error(cstr("expected dict"))
        return null()
    return py_dict_items(dict)


@c_abi_typed_export("PyDict_Clear", "void", ("ptr",))
def PyDict_Clear(dict) -> None:
    if _is_dict(dict) == 0:
        _type_error(cstr("expected dict"))
        return
    py_dict_clear(dict)


@c_abi_typed_export("PyDict_Check", "i32", ("ptr",))
def PyDict_Check(obj) -> int:
    return _is_dict(obj)


@c_abi_typed_export("PyDict_CheckExact", "i32", ("ptr",))
def PyDict_CheckExact(obj) -> int:
    return _is_dict(obj)


@c_abi_typed_export("PyDict_Copy", "ptr", ("ptr",))
def PyDict_Copy(mp):
    if ptr_is_null(mp):
        _type_error(cstr("PyDict_Copy requires a dict"))
        return null()
    copy = PyDict_New()
    if ptr_is_null(copy):
        return null()
    py_dict_update(copy, mp)
    return copy


@c_abi_typed_export("PyDict_Merge", "i32", ("ptr", "ptr", "i32"))
def PyDict_Merge(a, b, override: int) -> int:
    if ptr_is_null(a) or ptr_is_null(b):
        _type_error(cstr("PyDict_Merge requires two dicts"))
        return -1
    if override != 0:
        py_dict_update(a, b)
        return 0
    pos = stack_alloc(8)
    store_i64(pos, 0, 0)
    key_slot = stack_alloc(8)
    value_slot = stack_alloc(8)
    while True:
        store_ptr(key_slot, 0, null())
        store_ptr(value_slot, 0, null())
        found: int = PyDict_Next(b, pos, key_slot, value_slot)
        if found == 0:
            break
        key = load_ptr(key_slot, 0)
        value = load_ptr(value_slot, 0)
        if ptr_is_null(PyDict_GetItem(a, key)):
            if PyDict_SetItem(a, key, value) != 0:
                return -1
    return 0
