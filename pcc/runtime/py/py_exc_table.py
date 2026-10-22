"""Phase 4c.4: pcc-Python port of py_exc_table.c.

Re-implements py_exc_builtin_class — the lazy-bootstrap accessor for
builtin exception classes. The STATIC ARRAYS (PY_EXC_BUILTIN_NAMES
and PY_EXC_PARENT) and per-tag class cache live in substrate storage,
but this Python port reaches them directly through pcc.unsafe global
and typed memory primitives.

Returned by: PyClassObject*. Recursive on parent chain.
"""

__pcc_runtime_port__ = True

from pcc.extern import extern, c_abi_export, c_ptr, c_int32, c_void
from pcc.runtime.py.py_abi_constants import C_POINTER_SIZE, PYOBJECTHEADER_FLAGS_OFFSET, PY_FLAG_IMMORTAL
from pcc.unsafe import (
    atomic_cas_i64,
    atomic_load_i64,
    atomic_rmw_i32,
    define_global_i32,
    free,
    global_addr,
    int_to_ptr,
    load_i32,
    load_ptr,
    malloc,
    null,
    ptr_is_null,
    ptr_to_int,
    stack_alloc,
    store_ptr,
)

py_class_new = extern(
    "py_class_new",
    (c_ptr, c_ptr, c_int32, c_ptr, c_int32),
    c_ptr,
)
pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
define_global_i32("pcc_exc_cache_candidate_frame_map", 1)

# Generated ABI constants are compile-time static exports in library-object
# builds; no stripped module initializer is needed to consume them.


def _exc_name(tag: int):
    return load_ptr(global_addr("PY_EXC_BUILTIN_NAMES"), tag * C_POINTER_SIZE)


def _exc_parent(tag: int) -> int:
    return load_i32(global_addr("PY_EXC_PARENT"), tag * 4)


def _exc_cache_get(tag: int):
    bits: int = atomic_load_i64(
        global_addr("py_exc_classes"), tag * C_POINTER_SIZE, "acquire"
    )
    return int_to_ptr(bits)


def _exc_cache_publish(tag: int, candidate_slot):
    # Strong CAS transfers the completed immortal candidate's owner to the
    # existing mapped cache slot. Every reader acquires that publication;
    # recursively building parents holds no lock and returns canonical bases.
    # The caller registers this owner before calling us: even the compiler's
    # implicit function-entry poll must see the candidate in a traced slot.
    cls = pcc_gc_load_ptr(null(), candidate_slot)
    atomic_rmw_i32(
        "or", cls, PYOBJECTHEADER_FLAGS_OFFSET, PY_FLAG_IMMORTAL, "relaxed"
    )
    observed: int = atomic_cas_i64(
        global_addr("py_exc_classes"), tag * C_POINTER_SIZE, 0,
        ptr_to_int(cls), "acq_rel", "acquire",
    )
    if observed == 0:
        # The CAS transferred this sole owner to the mapped cache. Raw clear
        # transfers the owner instead of releasing it a second time.
        store_ptr(candidate_slot, 0, null())
    else:
        # The candidate can move at any runtime call. Drop immortality on
        # the current slot value, then release that owning slot normally.
        cls = pcc_gc_load_ptr(null(), candidate_slot)
        atomic_rmw_i32(
            "and", cls, PYOBJECTHEADER_FLAGS_OFFSET, ~PY_FLAG_IMMORTAL, "relaxed"
        )
        pcc_gc_store_root(candidate_slot, null())
    pcc_gc_frame_leave(candidate_slot)
    # Both disposal and frame leave may relocate the winner. Its raw CAS
    # address is only a publication result, never a surviving borrowed value.
    return _exc_cache_get(tag)


@c_abi_export("py_exc_builtin_class")
def py_exc_builtin_class(tag: int):
    n_builtin: int = 65  # PY_EXC_N_BUILTIN
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
        # py_class_new copies this temporary one-slot bases array.
        bases_ptr = malloc(C_POINTER_SIZE)
        store_ptr(bases_ptr, 0, base)
        n_bases = 1

    name_cstr = _exc_name(tag)
    cls = py_class_new(name_cstr, bases_ptr, n_bases, null(), 0)

    if not ptr_is_null(cls):
        # Register before temp-array free or publication entry can safepoint.
        # A literal-sized slot preserves precise stack-map alloca identity.
        candidate_slot = stack_alloc(8)
        store_ptr(candidate_slot, 0, cls)
        pcc_gc_frame_enter(global_addr("pcc_exc_cache_candidate_frame_map"), candidate_slot)
        free(bases_ptr)
        return _exc_cache_publish(tag, candidate_slot)

    free(bases_ptr)
    return cls
