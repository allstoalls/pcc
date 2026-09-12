"""Phase 4c.4: pcc-Python port of py_exc_table.c.

Re-implements py_exc_builtin_class — the lazy-bootstrap accessor for
builtin exception classes. The STATIC ARRAYS (PY_EXC_BUILTIN_NAMES
and PY_EXC_PARENT) and per-tag class cache live in substrate storage,
but this Python port reaches them directly through pcc.unsafe global
and typed memory primitives.

Returned by: PyClassObject*. Recursive on parent chain.
"""

__pcc_runtime_port__ = True

from pcc.extern import extern, c_abi_export, c_ptr, c_int32
from pcc.py_runtime.py.py_abi_constants import (
    C_POINTER_SIZE,
    PYOBJECTHEADER_FLAGS_OFFSET,
    PY_FLAG_IMMORTAL,
)
from pcc.unsafe import (
    global_addr,
    load_i32,
    load_ptr,
    malloc,
    null,
    ptr_is_null,
    store_i32,
    store_ptr,
)

py_class_new = extern(
    "py_class_new",
    (c_ptr, c_ptr, c_int32, c_ptr, c_int32),
    c_ptr,
)

# Generated ABI constants are compile-time static exports in library-object
# builds; no stripped module initializer is needed to consume them.


def _exc_name(tag: int):
    return load_ptr(global_addr("PY_EXC_BUILTIN_NAMES"), tag * C_POINTER_SIZE)


def _exc_parent(tag: int) -> int:
    return load_i32(global_addr("PY_EXC_PARENT"), tag * 4)


def _exc_cache_get(tag: int):
    return load_ptr(global_addr("py_exc_classes"), tag * C_POINTER_SIZE)


def _exc_cache_set(tag: int, cls) -> None:
    store_ptr(global_addr("py_exc_classes"), tag * C_POINTER_SIZE, cls)


@c_abi_export("py_exc_builtin_class")
def py_exc_builtin_class(tag: int):
    n_builtin: int = 34  # PY_EXC_N_BUILTIN
    if tag < 0 or tag >= n_builtin:
        tag = 1  # PY_EXC_EXCEPTION

    cached = _exc_cache_get(tag)
    if not ptr_is_null(cached):
        return cached

    parent: int = _exc_parent(tag)
    base = null()
    if parent >= 0:
        base = py_exc_builtin_class(parent)

    bases_ptr = null()
    n_bases: int = 0
    if not ptr_is_null(base):
        # One-slot pointer array; py_class_new copies it. The once-per-process
        # bootstrap allocation intentionally remains process-lifetime.
        bases_ptr = malloc(C_POINTER_SIZE)
        store_ptr(bases_ptr, 0, base)
        n_bases = 1

    name_cstr = _exc_name(tag)
    cls = py_class_new(name_cstr, bases_ptr, n_bases, null(), 0)

    if not ptr_is_null(cls):
        flags: int = load_i32(cls, PYOBJECTHEADER_FLAGS_OFFSET)
        store_i32(
            cls,
            PYOBJECTHEADER_FLAGS_OFFSET,
            flags | PY_FLAG_IMMORTAL,
        )
        _exc_cache_set(tag, cls)

    return cls
