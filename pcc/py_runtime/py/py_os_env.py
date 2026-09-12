"""Phase 4c.9: pcc-Python port of py_os_env.c.

Environment-variable helpers (getenv / putenv / unsetenv). The path
helpers stay in py_os_path.c for now because they need mutable byte-
buffer growth that pcc-Python cannot yet express cleanly.

Coercion: keys/values may be any py object; non-str gets routed
through py_obj_str() to realize a PyStrObject before reading its
UTF-8 bytes.
"""

__pcc_runtime_port__ = True

from pcc.py_runtime.py.py_abi_constants import (
    PY_TYPE_INT,
    PY_TYPE_STR,
)

from pcc.extern import extern, c_abi_export, c_ptr, c_int64, c_void
from pcc.unsafe import (
    cstr,
    free,
    global_load_ptr,
    is_tagged_int,
    load_i32,
    load_i8,
    null,
    ptr_add,
    ptr_is_null,
    strlen,
)

getenv = extern("pcc_platform_getenv", (c_ptr,), c_ptr)
setenv = extern("pcc_platform_setenv", (c_ptr, c_ptr, c_int64), c_int64)
unsetenv = extern("pcc_platform_unsetenv", (c_ptr,), c_int64)
env_snapshot_count = extern(
    "pcc_platform_env_snapshot_count", (), c_int64
)
env_snapshot_entry = extern(
    "pcc_platform_env_snapshot_entry", (c_int64,), c_ptr
)
py_dict_new = extern("py_dict_new", (), c_ptr)
py_dict_set = extern("py_dict_set", (c_ptr, c_ptr, c_ptr), c_void)

py_decref = extern("py_decref", (c_ptr,), c_void)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_obj_str = extern("py_obj_str", (c_ptr,), c_ptr)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_exc_new_with_value = extern("py_exc_new_with_value", (c_int64, c_ptr), c_ptr)
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)


# PY_TYPE_STR (=4) is inlined at every use site because pcc-Python
# initializes module-level integers in the auto-generated main(),
# which the Makefile strips for library .o builds.


def _type_of(obj) -> int:
    if is_tagged_int(obj):
        return PY_TYPE_INT  # PY_TYPE_INT
    return load_i32(obj, 8)


def _coerce_to_str(o):
    # Returns (item, owned) — item is the PyStrObject to read, owned
    # is the temporary we must decref after, or NULL.
    if ptr_is_null(o):
        return null(), null()
    if _type_of(o) == PY_TYPE_STR:  # PY_TYPE_STR
        return o, null()
    new_s = py_obj_str(o)
    return new_s, new_s


@c_abi_export("py_os_getenv")
def py_os_getenv(key, default_value):
    item, owned = _coerce_to_str(key)
    if ptr_is_null(item):
        if not ptr_is_null(owned):
            py_decref(owned)
        return default_value
    name = py_str_utf8(item)
    if ptr_is_null(name):
        if not ptr_is_null(owned):
            py_decref(owned)
        return default_value
    raw = getenv(name)
    if not ptr_is_null(owned):
        py_decref(owned)
    if ptr_is_null(raw):
        return default_value
    n: int = strlen(raw)
    return py_str_new(raw, n)


@c_abi_export("py_os_environ_snapshot")
def py_os_environ_snapshot():
    """``dict(os.environ)`` -- a real dict of the current environment.

    ``os.environ`` is a compiler-recognised special form with no object
    behind it, so iteration, ``keys``/``items`` and ``dict(...)`` had no
    lowering at all and fell through to CPython.  Under
    ``--python-libpython=off`` that stubbed out every enclosing function,
    including ``run_runtime_make`` -- which is why pcc1 could not rebuild
    its own runtime archive.

    Entries are copied out one at a time so no lock is held across the
    allocations this makes.
    """
    result = py_dict_new()
    if ptr_is_null(result) != 0:
        return null()
    count: int = env_snapshot_count()
    index: int = 0
    while index < count:
        entry = env_snapshot_entry(index)
        index = index + 1
        if ptr_is_null(entry) != 0:
            continue
        # Split at the first '='; an entry without one is not a variable.
        offset: int = 0
        while load_i8(entry, offset) != 0 and load_i8(entry, offset) != 61:
            offset = offset + 1
        if load_i8(entry, offset) != 61:
            free(entry)
            continue
        key = py_str_new(entry, offset)
        value_start = ptr_add(entry, offset + 1)
        value = py_str_new(value_start, strlen(value_start))
        free(entry)
        if ptr_is_null(key) == 0 and ptr_is_null(value) == 0:
            py_dict_set(result, key, value)
        py_decref(key)
        py_decref(value)
    return result


@c_abi_export("py_os_environ_contains")
def py_os_environ_contains(key) -> int:
    if ptr_is_null(key) != 0:
        py_raise_owned(py_exc_new(3, cstr("str expected")))  # PY_EXC_TYPEERROR
        return -1
    if _type_of(key) != PY_TYPE_STR:  # PY_TYPE_STR
        py_raise_owned(py_exc_new(3, cstr("str expected")))  # PY_EXC_TYPEERROR
        return -1
    name = py_str_utf8(key)
    if ptr_is_null(name) != 0:
        py_raise_owned(py_exc_new(3, cstr("str expected")))  # PY_EXC_TYPEERROR
        return -1
    if ptr_is_null(getenv(name)) != 0:
        return 0
    return 1


@c_abi_export("py_os_putenv")
def py_os_putenv(key, value):
    k_item, k_owned = _coerce_to_str(key)
    v_item, v_owned = _coerce_to_str(value)
    if ptr_is_null(k_item):
        if not ptr_is_null(k_owned):
            py_decref(k_owned)
        if not ptr_is_null(v_owned):
            py_decref(v_owned)
        return global_load_ptr("py_None")
    if ptr_is_null(v_item):
        if not ptr_is_null(k_owned):
            py_decref(k_owned)
        if not ptr_is_null(v_owned):
            py_decref(v_owned)
        return global_load_ptr("py_None")
    k_raw = py_str_utf8(k_item)
    v_raw = py_str_utf8(v_item)
    if not ptr_is_null(k_raw):
        if not ptr_is_null(v_raw):
            setenv(k_raw, v_raw, 1)
    if not ptr_is_null(k_owned):
        py_decref(k_owned)
    if not ptr_is_null(v_owned):
        py_decref(v_owned)
    return global_load_ptr("py_None")


@c_abi_export("py_os_environ_getitem")
def py_os_environ_getitem(key):
    # os.environ[key]: CPython mapping semantics — the key must be a
    # str (TypeError otherwise, like CPython's encodekey()) and a
    # missing variable raises KeyError carrying the key. Mirrors
    # py_os_environ_getitem in py_os_env.c; py_os_getenv stays
    # non-raising for os.getenv() / os.environ.get().
    if ptr_is_null(key) != 0:
        py_raise_owned(py_exc_new(3, cstr("str expected")))  # PY_EXC_TYPEERROR
        return null()
    if _type_of(key) != PY_TYPE_STR:  # PY_TYPE_STR
        py_raise_owned(py_exc_new(3, cstr("str expected")))  # PY_EXC_TYPEERROR
        return null()
    name = py_str_utf8(key)
    if ptr_is_null(name) != 0:
        py_raise_owned(py_exc_new_with_value(4, key))  # PY_EXC_KEYERROR
        return null()
    raw = getenv(name)
    if ptr_is_null(raw) != 0:
        py_raise_owned(py_exc_new_with_value(4, key))  # PY_EXC_KEYERROR
        return null()
    n: int = strlen(raw)
    return py_str_new(raw, n)


@c_abi_export("py_os_environ_setitem")
def py_os_environ_setitem(key, value):
    # os.environ[key] = value: CPython mapping semantics — both key
    # and value must be str (TypeError otherwise); the store is
    # visible to the process environment (setenv), matching CPython's
    # putenv-backed __setitem__. Mirrors py_os_environ_setitem in
    # py_os_env.c; py_os_putenv stays coercing/non-raising.
    if ptr_is_null(key) != 0:
        py_raise_owned(py_exc_new(3, cstr("str expected")))  # PY_EXC_TYPEERROR
        return null()
    if _type_of(key) != PY_TYPE_STR:  # PY_TYPE_STR
        py_raise_owned(py_exc_new(3, cstr("str expected")))  # PY_EXC_TYPEERROR
        return null()
    if ptr_is_null(value) != 0:
        py_raise_owned(py_exc_new(3, cstr("str expected")))  # PY_EXC_TYPEERROR
        return null()
    if _type_of(value) != PY_TYPE_STR:  # PY_TYPE_STR
        py_raise_owned(py_exc_new(3, cstr("str expected")))  # PY_EXC_TYPEERROR
        return null()
    k_raw = py_str_utf8(key)
    v_raw = py_str_utf8(value)
    if ptr_is_null(k_raw) == 0:
        if ptr_is_null(v_raw) == 0:
            setenv(k_raw, v_raw, 1)
    return global_load_ptr("py_None")


@c_abi_export("py_os_unsetenv")
def py_os_unsetenv(key):
    item, owned = _coerce_to_str(key)
    if ptr_is_null(item):
        if not ptr_is_null(owned):
            py_decref(owned)
        return global_load_ptr("py_None")
    raw = py_str_utf8(item)
    if not ptr_is_null(raw):
        unsetenv(raw)
    if not ptr_is_null(owned):
        py_decref(owned)
    return global_load_ptr("py_None")
