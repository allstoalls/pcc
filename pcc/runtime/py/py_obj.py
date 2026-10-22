"""Phase 4c.11: pcc-Python port of py_obj.c.

py_bool_from_bit + py_incref + py_decref dispatch. The dealloc
implementations are separate ABI symbols, provided by py_obj_dealloc.py
in the pcc-Python runtime archive and by py_obj_dealloc.c in the C
runtime archives. The immortal singletons live in py_substrate.py for
the pcc-Python archive and in py_substrate.c for the C runtime archives.
This port only owns the "dispatch layer".

The object-header layout, flags, and public type-tag values are consumed from
the generated C-header-derived ``py_abi_constants`` module.  Numeric copies do
not belong in this prose because the generator cannot update them.
"""

__pcc_runtime_port__ = True

from pcc.extern import extern, c_abi_export, c_int32, c_int64, c_ptr, c_void
from pcc.runtime.py.py_abi_constants import (
    PYLISTOBJECT_ITEMS_OFFSET,
    PYLISTOBJECT_LENGTH_OFFSET,
    PYOBJECTHEADER_FLAGS_OFFSET,
    PYOBJECTHEADER_REFCOUNT_OFFSET,
    PYOBJECTHEADER_TYPE_TAG_OFFSET,
    PY_FLAG_GC_PINNED,
    PY_FLAG_GC_TRACKED,
    PY_FLAG_IMMORTAL,
    PY_TYPE_BOOL,
    PY_TYPE_BYTEARRAY,
    PY_TYPE_BYTES,
    PY_TYPE_CEXT_TAG_BASE,
    PY_TYPE_CLASS,
    PY_TYPE_CLASSMETHOD,
    PY_TYPE_COMPLEX,
    PY_TYPE_CONTINUATION,
    PY_TYPE_COROUTINE,
    PY_TYPE_CPY_HANDLE,
    PY_TYPE_DICT,
    PY_TYPE_ELLIPSIS,
    PY_TYPE_NOTIMPLEMENTED,
    PY_TYPE_EXC,
    PY_TYPE_FLOAT,
    PY_TYPE_FUNC,
    PY_TYPE_GEN,
    PY_TYPE_INSTANCE,
    PY_TYPE_INT,
    PY_TYPE_ITER,
    PY_TYPE_LIST,
    PY_TYPE_MEMORYVIEW,
    PY_TYPE_NONE,
    PY_TYPE_PROPERTY,
    PY_TYPE_SET,
    PY_TYPE_STATICMETHOD,
    PY_TYPE_STR,
    PY_TYPE_TASK,
    PY_TYPE_TUPLE,
    PY_TYPE_USER,
    PY_TYPE_USER_CLASS_START,
    PY_TYPE_VIRTUAL_THREAD,
    PY_TYPE_VTHREAD_CHANNEL,
    PY_TYPE_WEAKREF,
)
from pcc.unsafe import (
    cstr,
    define_global_i32,
    atomic_cas_i64,
    atomic_load_i32,
    atomic_load_i64,
    atomic_rmw_i32,
    atomic_rmw_i64,
    atomic_store_i32,
    atomic_store_i64,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    is_tagged_int,
    load_ptr,
    load_i32,
    load_i64,
    malloc,
    memmove,
    memset,
    null,
    ptr_add,
    ptr_diff,
    ptr_eq,
    ptr_is_null,
    stack_alloc,
    store_ptr,
    store_i32,
    store_i64,
)

py_dealloc_int = extern("py_dealloc_int", (c_ptr,), c_void)
py_dealloc_float = extern("py_dealloc_float", (c_ptr,), c_void)
py_dealloc_str = extern("py_dealloc_str", (c_ptr,), c_void)
py_dealloc_list = extern("py_dealloc_list", (c_ptr,), c_void)
py_dealloc_tuple = extern("py_dealloc_tuple", (c_ptr,), c_void)
py_dealloc_dict = extern("py_dealloc_dict", (c_ptr,), c_void)
py_dealloc_set = extern("py_dealloc_set", (c_ptr,), c_void)
py_dealloc_func = extern("py_dealloc_func", (c_ptr,), c_void)
py_dealloc_iter = extern("py_dealloc_iter", (c_ptr,), c_void)
py_dealloc_gen = extern("py_dealloc_gen", (c_ptr,), c_void)
py_gen_finalize_from_dealloc = extern("py_gen_finalize_from_dealloc", (c_ptr,), c_int64)
py_dealloc_coroutine = extern("py_dealloc_coroutine", (c_ptr,), c_void)
py_dealloc_continuation = extern("py_dealloc_continuation", (c_ptr,), c_void)
py_dealloc_task = extern("py_dealloc_task", (c_ptr,), c_void)
py_dealloc_memoryview = extern("py_dealloc_memoryview", (c_ptr,), c_void)
py_dealloc_weakref = extern("py_dealloc_weakref", (c_ptr,), c_void)
py_dealloc_generic = extern("py_dealloc_generic", (c_ptr,), c_void)
py_dealloc_thread_lock = extern("py_dealloc_thread_lock", (c_ptr,), c_void)
py_dealloc_thread_rlock = extern("py_dealloc_thread_rlock", (c_ptr,), c_void)
py_dealloc_thread_event = extern("py_dealloc_thread_event", (c_ptr,), c_void)
py_dealloc_thread_condition = extern("py_dealloc_thread_condition", (c_ptr,), c_void)
py_dealloc_thread_semaphore = extern("py_dealloc_thread_semaphore", (c_ptr,), c_void)
py_dealloc_thread_thread = extern("py_dealloc_thread_thread", (c_ptr,), c_void)
py_dealloc_virtual_thread = extern("py_dealloc_virtual_thread", (c_ptr,), c_void)
py_class_dealloc = extern("py_class_dealloc", (c_ptr,), c_void)
py_instance_dealloc = extern("py_instance_dealloc", (c_ptr,), c_void)
# Dynamic C-extension type tags live above the builtin range; strict
# incref/decref must accept registry-proven ones so their refcount
# lifecycle and tp_dealloc dispatch match the C owner (which already
# exempts them). The registry stays the single acceptance authority.
pcc_capi_is_cext_type_tag = extern(
    "pcc_capi_is_cext_type_tag", (c_int64,), c_int64
)
py_dealloc_exc = extern("py_dealloc_exc", (c_ptr,), c_void)
pcc_dealloc_with_trash = extern(
    "pcc_dealloc_with_trash",
    (c_ptr, c_int64),
    c_void,
)
pcc_runtime_monotonic_us = extern("pcc_platform_monotonic_us", (), c_int64)
pcc_gc_record_explicit_pause = extern(
    "pcc_gc_record_explicit_pause", (c_int64, c_int64), c_void
)
py_gc_collect = extern("py_gc_collect", (), c_int64)
py_gc_untrack = extern("py_gc_untrack", (c_ptr,), c_void)
pcc_gc_slot_is_runtime_root = extern("pcc_gc_slot_is_runtime_root", (c_ptr,), c_int64)
py_list_new = extern("py_list_new", (c_int64,), c_ptr)
py_list_append = extern("py_list_append", (c_ptr, c_ptr), c_void)
py_list_get = extern("py_list_get", (c_ptr, c_int64), c_ptr)
py_list_len = extern("py_list_len", (c_ptr,), c_int64)
py_list_remove = extern("py_list_remove", (c_ptr, c_ptr), c_void)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_dict_new = extern("py_dict_new", (), c_ptr)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_obj_call = extern("py_obj_call", (c_ptr, c_ptr, c_ptr), c_ptr)
py_obj_eq = extern("py_obj_eq", (c_ptr, c_ptr), c_int64)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_weakref_invalidate = extern("py_weakref_invalidate", (c_ptr,), c_void)
pcc_gc_note_alloc = extern("pcc_gc_note_alloc", (c_int64,), c_void)
pcc_gc_backend4_maybe_auto_step = extern("pcc_gc_backend4_maybe_auto_step", (), c_void)
pcc_thread_no_park_depth = extern("pcc_thread_no_park_depth", (), c_int64)
pcc_py_gc_minor_graph_lock_depth = extern("pcc_py_gc_minor_graph_lock_depth", (), c_int64)
pcc_gc_note_object_allocated = extern(
    "pcc_gc_note_object_allocated",
    (c_ptr,),
    c_void,
)
pcc_gc_note_object_allocated_sized = extern(
    "pcc_gc_note_object_allocated_sized",
    (c_ptr, c_int64),
    c_void,
)
pcc_gc_pointer_register = extern(
    "pcc_gc_pointer_register",
    (c_ptr,),
    c_int64,
)
pcc_gc_try_minor_alloc = extern("pcc_gc_try_minor_alloc", (c_int64,), c_ptr)
pcc_allocator_alloc_object = extern(
    "pcc_allocator_alloc_object", (c_int64,), c_ptr
)
pcc_gc_backend4_try_zpage_alloc = extern(
    "pcc_gc_backend4_try_zpage_alloc",
    (c_int64, c_int32),
    c_ptr,
)
pcc_gc_note_object_freeing = extern(
    "pcc_gc_note_object_freeing",
    (c_ptr,),
    c_void,
)
pcc_gc_free_watch_begin = extern(
    "pcc_gc_free_watch_begin", (c_ptr, c_ptr), c_void
)
pcc_gc_free_watch_end = extern("pcc_gc_free_watch_end", (c_ptr,), c_int64)
# pcc_gc_free_watch_begin's record (layout owned by py_gc_backend.py).
GC_FREE_WATCH_BYTES = 24
pcc_gc_note_load = extern("pcc_gc_note_load", (), c_void)
pcc_gc_note_relocation_read = extern(
    "pcc_gc_note_relocation_read",
    (c_ptr,),
    c_ptr,
)
pcc_gc_note_store = extern("pcc_gc_note_store", (), c_void)
pcc_gc_note_safepoint = extern("pcc_gc_note_safepoint", (), c_void)
pcc_gc_step = extern("pcc_gc_step", (c_int64,), c_int64)
pcc_gc_backend = extern("pcc_gc_backend", (), c_int64)
pcc_gc_note_frame_enter = extern(
    "pcc_gc_note_frame_enter",
    (c_ptr, c_ptr),
    c_void,
)
pcc_gc_note_frame_leave = extern(
    "pcc_gc_note_frame_leave",
    (c_ptr,),
    c_void,
)
pcc_gc_note_frame_enter_lifo = extern(
    "pcc_gc_note_frame_enter_lifo",
    (c_ptr, c_ptr),
    c_void,
)
pcc_thread_safepoint_gate = extern("pcc_thread_safepoint_gate", (), c_void)
pcc_gc_frame_slots_walked = extern("pcc_gc_frame_slots_walked", (c_ptr,), c_int64)
pcc_gc_note_frame_leave_lifo = extern(
    "pcc_gc_note_frame_leave_lifo",
    (c_ptr,),
    c_void,
)
pcc_gc_note_write_barrier = extern(
    "pcc_gc_note_write_barrier",
    (c_ptr, c_ptr),
    c_void,
)
pcc_gc_note_slot_write_barrier = extern(
    "pcc_gc_note_slot_write_barrier",
    (c_ptr, c_ptr, c_ptr),
    c_void,
)
pcc_gc_object_is_known_no_lock = extern(
    "pcc_gc_object_is_known_no_lock",
    (c_ptr,),
    c_int64,
)
pcc_gc_backend4_slot_needs_resolve = extern(
    "pcc_gc_backend4_slot_needs_resolve",
    (c_ptr,),
    c_int64,
)
pcc_gc_object_is_known = extern("pcc_gc_object_is_known", (c_ptr,), c_int64)
pcc_gc_has_tracing_sweep = extern("pcc_gc_has_tracing_sweep", (), c_int64)
pcc_gc_sweep_owed = extern("pcc_gc_sweep_owed", (), c_int64)
pcc_gc_collect_tracing = extern("pcc_gc_collect_tracing", (), c_int64)
pcc_gc_begin_explicit_tracing_collect = extern(
    "pcc_gc_begin_explicit_tracing_collect",
    (),
    c_void,
)
pcc_gc_end_explicit_tracing_collect = extern(
    "pcc_gc_end_explicit_tracing_collect",
    (),
    c_void,
)
pcc_gc_external_resource_poll = extern(
    "pcc_gc_external_resource_poll",
    (),
    c_int64,
)
pcc_thread_safepoint = extern("pcc_thread_safepoint", (), c_void)
pcc_threads_enabled = extern("pcc_threads_enabled", (), c_int64)
pcc_stop_the_world = extern("pcc_stop_the_world", (), c_int64)
pcc_gc_precise_stack_diff_if_requested = extern(
    "pcc_gc_precise_stack_diff_if_requested", (), c_void
)
pcc_resume_world = extern("pcc_resume_world", (), c_int64)
pcc_refcount_incref = extern("pcc_refcount_incref", (c_ptr,), c_int64)
pcc_refcount_decref = extern("pcc_refcount_decref", (c_ptr,), c_int64)
pcc_refcount_forget = extern("pcc_refcount_forget", (c_ptr,), c_void)
pcc_diagnostics_runtime_log_event_code = extern(
    "pcc_diagnostics_runtime_log_event_code", (c_int32, c_int32, c_int64, c_int64, c_ptr), c_void
)
pcc_debug_note_alloc_size = extern(
    "pcc_debug_note_alloc_size", (c_ptr, c_int64), c_void
)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
pcc_gc_foreign_lease_acquire = extern("pcc_gc_foreign_lease_acquire", (c_ptr,), c_int64)
pcc_gc_foreign_lease_release = extern("pcc_gc_foreign_lease_release", (c_ptr, c_int64), c_int64)
pcc_gc_resolve_root_slot_unlocked = extern(
    "pcc_gc_resolve_root_slot_unlocked", (c_ptr, c_int64), c_ptr
)


def _gc_backend_fast() -> int:
    if load_i32(global_addr("pcc_gc_config_initialized"), 0) == 0:
        return pcc_gc_backend()
    return load_i32(global_addr("pcc_gc_backend_selected"), 0)


pcc_gc_pointer_is_managed = extern(
    "pcc_gc_pointer_is_managed", (c_ptr,), c_int64
)


def _ptr_can_have_header(o) -> bool:
    return pcc_gc_pointer_is_managed(o) != 0


pcc_platform_write = extern(
    "pcc_platform_write", (c_int64, c_ptr, c_int64), c_int64
)
pcc_platform_getenv = extern("pcc_platform_getenv", (c_ptr,), c_ptr)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)


def _refcount_provenance_probe_enabled() -> int:
    """Mirror of py_obj.c's pcc_gc_refcount_provenance_probe read.

    PCC_GC_REFCOUNT_PROVENANCE_PROBE: 0 trusts the caller and skips the
    managed-pointer probe on the refcount hot path, 1 probes and counts misses
    in pcc_gc_unmanaged_refcount_ops, 2 probes, counts and reports the first
    miss on stderr, 3 also aborts on that first miss so the crash report names
    the site.  Every caller reads the selected backend first, which runs
    pcc_gc_config_ensure, so the value is already configured here.
    """
    return load_i32(global_addr("pcc_gc_refcount_provenance_probe"), 0)


def _note_unmanaged_refcount_op() -> None:
    """Mirror of py_obj.c's pcc_note_unmanaged_refcount_op.

    PCC_GC_COUNTER_UNMANAGED_REFCOUNT_OPS (116).  Read the enum comment in
    py_runtime.h for why this is a ratchet and not just telemetry.
    """
    slot = global_addr("pcc_gc_unmanaged_refcount_ops")
    store_i64(slot, 0, load_i64(slot, 0) + 1)
    mode: int = _refcount_provenance_probe_enabled()
    if mode >= 2:
        reported = global_addr("pcc_gc_refcount_provenance_probe_reported")
        if load_i32(reported, 0) == 0:
            store_i32(reported, 0, 1)
            pcc_platform_write(
                2,
                cstr("pcc runtime: refcount operation on an unmanaged pointer (PCC_GC_REFCOUNT_PROVENANCE_PROBE=2)\n"),
                93,
            )
            if mode == 3:
                pcc_platform_abort()


def _gc_relocation_candidate(o) -> int:
    if not _ptr_can_have_header(o):
        return 0
    return 1 if (load_i32(o, PYOBJECTHEADER_FLAGS_OFFSET) & 2048) != 0 else 0


def _gc_forwarding_population() -> int:
    return load_i32(global_addr("pcc_gc_forwarding_population"), 0)


def _gc_backend4_should_check_slot(slot) -> int:
    if _gc_forwarding_population() > 0:
        return 1
    if ptr_is_null(global_load_ptr("pcc_gc_relocation_set_head")) != 0:
        return 0
    if pcc_gc_slot_is_runtime_root(slot) != 0:
        return 0
    return 1


# Header layout, stable flags, and public type tags come from the generated
# C-header-derived ABI module.  Collector-private state bits remain local to
# their owning implementation because they are not part of PyObjectHeader's
# public port ABI contract.


@c_abi_export("py_bool_from_bit")
def py_bool_from_bit(b: int):
    if b != 0:
        return global_load_ptr("py_True")
    return global_load_ptr("py_False")


def _gc_graph_leaf_tag(tag: int) -> int:
    if tag == PY_TYPE_ELLIPSIS or tag == PY_TYPE_NOTIMPLEMENTED:
        return 1
    if tag == PY_TYPE_NONE:
        return 1
    if tag == PY_TYPE_BOOL:
        return 1
    if tag == PY_TYPE_INT:
        return 1
    if tag == PY_TYPE_FLOAT:
        return 1
    if tag == PY_TYPE_STR:
        return 1
    if tag == PY_TYPE_COMPLEX:
        return 1
    if tag == PY_TYPE_BYTES:
        return 1
    if tag == PY_TYPE_BYTEARRAY:
        return 1
    if tag == PY_TYPE_CPY_HANDLE:
        return 1
    return 0


@c_abi_export("pcc_gc_alloc")
def pcc_gc_alloc(size: int, type_tag: int, flags: int):
    if size < 16:
        return null()
    pcc_thread_safepoint()
    pcc_gc_note_alloc(size)
    if load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) != 0:
        pcc_diagnostics_runtime_log_event_code(1, 1, size, type_tag, null())
    backend: int = _gc_backend_fast()
    obj = null()
    stored_flags: int = flags
    if backend == 4:
        if (
            type_tag == PY_TYPE_LIST
            or type_tag == PY_TYPE_TUPLE
            or type_tag == PY_TYPE_DICT
            or type_tag == PY_TYPE_SET
            or type_tag == PY_TYPE_PROPERTY
            or type_tag == PY_TYPE_CLASSMETHOD
            or type_tag == PY_TYPE_WEAKREF
            # Every remaining tag the colored-relocation accept predicate
            # admits (GC-P1-BACKEND4-RELOCATABLE-TAGS-LACK-FRESH-ALLOC);
            # mirrors py_obj.c exactly.  Constructors publish on success.
            or type_tag == PY_TYPE_FUNC
            or type_tag == PY_TYPE_ITER
            or type_tag == PY_TYPE_GEN
            or type_tag == PY_TYPE_COROUTINE
            or type_tag == PY_TYPE_CONTINUATION
            or type_tag == PY_TYPE_TASK
            or type_tag == PY_TYPE_EXC
            or type_tag == PY_TYPE_CLASS
            or type_tag == PY_TYPE_STATICMETHOD
            or type_tag == PY_TYPE_MEMORYVIEW
        ):
            stored_flags = stored_flags | 16384
    if backend == 3:
        obj = pcc_gc_try_minor_alloc(size)
    elif backend == 4:
        if _gc_graph_leaf_tag(type_tag) == 0:
            obj = pcc_gc_backend4_try_zpage_alloc(size, flags)
            if ptr_is_null(obj) == 0:
                stored_flags = stored_flags | 65536
        else:
            stored_flags = stored_flags & ~65536
    if ptr_is_null(obj) != 0:
        # Mirror the C tier's calloc (py_obj.c): fresh objects are
        # GC-visible before their constructor fills the body; visitors
        # must see NULL slots, not malloc garbage.  Objects draw from the
        # allocator's OBJECT slab family (ARCH-P0 S1) so their granules are
        # registered for exact structural provenance; raw allocations keep
        # using plain malloc and never share these slabs.
        obj = pcc_allocator_alloc_object(size)
        if ptr_is_null(obj) == 0:
            memset(obj, 0, size)
            if backend == 4:
                stored_flags = (stored_flags & ~65536) | 262144
            elif backend == 3:
                stored_flags = (stored_flags & ~4096) | 262144
    if ptr_is_null(obj):
        return obj
    store_i64(obj, PYOBJECTHEADER_REFCOUNT_OFFSET, 1)
    store_i32(obj, PYOBJECTHEADER_TYPE_TAG_OFFSET, type_tag)
    store_i32(obj, PYOBJECTHEADER_FLAGS_OFFSET, stored_flags)
    pcc_debug_note_alloc_size(obj, size)
    # Exact provenance is visible before the tracking layer can publish the
    # object.  Backend-0 objects and graph leaves intentionally stay here.
    if pcc_gc_pointer_register(obj) < 0:
        return null()
    pcc_gc_note_object_allocated_sized(obj, size)
    if load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) != 0:
        pcc_diagnostics_runtime_log_event_code(1, 2, size, type_tag, obj)
    return obj


@c_abi_export("pcc_gc_publish_initialized")
def pcc_gc_publish_initialized(obj) -> None:
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return
    if _gc_backend_fast() != 4:
        return
    pcc_py_gc_minor_graph_lock()
    flags: int = load_i32(obj, PYOBJECTHEADER_FLAGS_OFFSET)
    store_i32(obj, PYOBJECTHEADER_FLAGS_OFFSET, flags & ~16384)
    pcc_py_gc_minor_graph_unlock()


@c_abi_export("pcc_gc_retain")
def pcc_gc_retain(o):
    py_incref(o)
    return o


@c_abi_export("pcc_gc_release")
def pcc_gc_release(o) -> None:
    if ptr_is_null(o) != 0 or is_tagged_int(o) != 0:
        return
    backend: int = _gc_backend_fast()
    if backend == 3:
        if _gc_relocation_candidate(o) != 0:
            resolved = pcc_gc_note_relocation_read(o)
            # Identity, not ``!=``: a value comparison retains both operands
            # and drops them through this very function, recursing forever.
            if ptr_eq(resolved, o) == 0:
                py_decref(o)
                return
    elif backend == 4:
        if _gc_forwarding_population() > 0 and _gc_relocation_candidate(o) != 0:
            resolved = pcc_gc_note_relocation_read(o)
            if ptr_eq(resolved, o) != 0 and pcc_gc_object_is_known_no_lock(o) == 0:
                return
            o = resolved
    if backend == 3:
        flags: int = load_i32(o, PYOBJECTHEADER_FLAGS_OFFSET)
        if (flags & 4096) != 0 and (flags & 256) != 0 and load_i64(o, PYOBJECTHEADER_REFCOUNT_OFFSET) <= 0:
            return
    py_decref(o)


@c_abi_export("pcc_gc_load_ptr")
def pcc_gc_load_ptr(owner, slot):
    if ptr_is_null(slot):
        return null()
    v = load_ptr(slot, 0)
    if load_i32(global_addr("pcc_gc_read_barrier_enabled"), 0) == 0:
        return v
    backend: int = 0
    if load_i32(global_addr("pcc_gc_config_initialized"), 0) == 0:
        backend = pcc_gc_backend()
    else:
        backend = load_i32(global_addr("pcc_gc_backend_selected"), 0)
    if backend == 3 or backend == 4:
        # Null and tagged values cannot relocate. Skip GC4 root membership
        # and resolution before counting a read-barrier check.
        if backend == 4 and (ptr_is_null(v) != 0 or is_tagged_int(v) != 0):
            return v
        if backend == 3 and _gc_forwarding_population() <= 0:
            return v
        if backend == 4 and _gc_backend4_should_check_slot(slot) == 0:
            return v
        pcc_gc_note_load()
        needs_resolve: int = 0
        if backend == 4:
            # G-P0-LONGRUN: decide via pointer-value lookups, never a raw
            # header deref of a possibly-stale/unmapped slot value.
            if pcc_gc_backend4_slot_needs_resolve(v) != 0:
                needs_resolve = 1
        elif _gc_relocation_candidate(v) != 0:
            needs_resolve = 1
        if needs_resolve != 0:
            resolved = pcc_gc_note_relocation_read(v)
            if ptr_eq(resolved, v) == 0:
                store_ptr(slot, 0, resolved)
                v = resolved
        return v
    return v


@c_abi_export("pcc_gc_load_borrowed_ptr")
def pcc_gc_load_borrowed_ptr(owner, slot):
    if ptr_is_null(slot):
        return null()
    v = load_ptr(slot, 0)
    if load_i32(global_addr("pcc_gc_read_barrier_enabled"), 0) == 0:
        return v
    backend: int = _gc_backend_fast()
    if backend == 3 or backend == 4:
        # Null and tagged values cannot relocate. Skip GC4 root membership
        # and resolution before counting a read-barrier check.
        if backend == 4 and (ptr_is_null(v) != 0 or is_tagged_int(v) != 0):
            return v
        if backend == 3 and _gc_forwarding_population() <= 0:
            return v
        if backend == 4 and _gc_backend4_should_check_slot(slot) == 0:
            return v
        pcc_gc_note_load()
        needs_resolve: int = 0
        if backend == 4:
            # G-P0-LONGRUN: decide via pointer-value lookups, never a raw
            # header deref of a possibly-stale/unmapped slot value.
            if pcc_gc_backend4_slot_needs_resolve(v) != 0:
                needs_resolve = 1
        elif _gc_relocation_candidate(v) != 0:
            needs_resolve = 1
        if needs_resolve != 0:
            resolved = pcc_gc_note_relocation_read(v)
            if ptr_eq(resolved, v) == 0:
                store_ptr(slot, 0, resolved)
                v = resolved
        return v
    return v


@c_abi_export("pcc_gc_resolve_owned_ptr")
def pcc_gc_resolve_owned_ptr(value):
    if ptr_is_null(value) != 0:
        return value
    if is_tagged_int(value) != 0:
        return value
    backend: int = _gc_backend_fast()
    if backend != 3 and backend != 4:
        return value
    if _gc_forwarding_population() <= 0:
        return value
    if backend == 3:
        if _gc_relocation_candidate(value) == 0:
            return value
    resolved = pcc_gc_note_relocation_read(value)
    if ptr_eq(resolved, value) == 0:
        return resolved
    return value


def _gc_incref_fresh_native_instance(o) -> None:
    if ptr_is_null(o) != 0:
        return
    if is_tagged_int(o) != 0:
        return
    tag: int = load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    if tag != PY_TYPE_INSTANCE and (tag < PY_TYPE_USER_CLASS_START or tag >= (PY_TYPE_CEXT_TAG_BASE)):
        # Keep an accidental future caller safe; the optimized frontend lane
        # proves this exact tag and therefore never takes the generic query.
        py_incref(o)
        return
    backend: int = _gc_backend_fast()
    flags: int = load_i32(o, PYOBJECTHEADER_FLAGS_OFFSET)
    if (backend == 1 or backend == 2) and flags == 0:
        return
    if backend == 4 and _gc_forwarding_population() > 0 and (flags & 2048) != 0:
        resolved = pcc_gc_note_relocation_read(o)
        if ptr_is_null(resolved) == 0 and ptr_eq(resolved, o) == 0:
            o = resolved
            tag = load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET)
            flags = load_i32(o, PYOBJECTHEADER_FLAGS_OFFSET)
    if (flags & PY_FLAG_IMMORTAL) != 0:
        return
    if backend == 3 and (flags & 4096) != 0 and (flags & 256) != 0:
        if load_i64(o, PYOBJECTHEADER_REFCOUNT_OFFSET) <= 0:
            return
    if load_i64(o, PYOBJECTHEADER_REFCOUNT_OFFSET) < 0:
        return
    new_rc: int = pcc_refcount_incref(o)
    if load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) != 0:
        pcc_diagnostics_runtime_log_event_code(3, 1, new_rc, tag, o)


@c_abi_export("pcc_gc_store_ptr")
def pcc_gc_store_ptr(owner, slot, value) -> None:
    if ptr_is_null(slot):
        return
    backend: int = 0
    if load_i32(global_addr("pcc_gc_config_initialized"), 0) == 0:
        backend = pcc_gc_backend()
    else:
        backend = load_i32(global_addr("pcc_gc_backend_selected"), 0)
    if backend == 0:
        if load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) != 0:
            pcc_diagnostics_runtime_log_event_code(2, 3, backend, 0, owner)
        old = load_ptr(slot, 0)
        # This retaining store already owns the same reference. GC0 has no
        # tracing/relocation barrier to perform for an unchanged edge.
        if ptr_eq(old, value) != 0:
            return
        py_incref(value)
        store_ptr(slot, 0, value)
        py_decref(old)
        return
    plan = stack_alloc(128)
    pcc_gc_store_ptr_plan_init(plan, owner, backend)
    pcc_py_gc_minor_graph_lock()
    pcc_gc_store_ptr_plan_commit_locked(plan, owner, slot, value)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_store_ptr_plan_finish(plan)


@c_abi_export("pcc_gc_store_ptr_fresh_native_instance")
def pcc_gc_store_ptr_fresh_native_instance(owner, slot, value) -> None:
    if ptr_is_null(slot):
        return
    backend: int = _gc_backend_fast()
    if backend == 1 or backend == 2 or backend == 3 or backend == 4:
        pcc_gc_note_store()
    if backend == 4:
        if _gc_forwarding_population() > 0 and _gc_relocation_candidate(value) != 0:
            value = pcc_gc_note_relocation_read(value)
    elif backend == 3:
        if _gc_forwarding_population() > 0 and _gc_relocation_candidate(value) != 0:
            value = pcc_gc_note_relocation_read(value)
    if backend == 1 or backend == 2 or backend == 3 or backend == 4:
        pcc_gc_note_slot_write_barrier(owner, slot, value)
    if load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) != 0:
        pcc_diagnostics_runtime_log_event_code(2, 3, backend, 0, owner)
    old = load_ptr(slot, 0)
    _gc_incref_fresh_native_instance(value)
    store_ptr(slot, 0, value)
    py_decref(old)


@c_abi_export("pcc_gc_store_root_plan_init")
def pcc_gc_store_root_plan_init(plan, backend: int) -> None:
    if ptr_is_null(plan) != 0:
        return
    memset(plan, 0, 128)
    store_i64(plan, 112, backend)
    store_i32(plan, 120, 0)


@c_abi_export("pcc_gc_store_ptr_plan_init")
def pcc_gc_store_ptr_plan_init(plan, owner, backend: int) -> None:
    pcc_gc_store_root_plan_init(plan, backend)
    if load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) != 0:
        pcc_diagnostics_runtime_log_event_code(2, 3, backend, 0, owner)


def _gc4_old_slot_is_releasable(value) -> int:
    if pcc_gc_object_is_known_no_lock(value) != 0:
        return 1
    # Graph leaves intentionally have no tracing-index node. Allocation
    # provenance still proves their header is live; their slot owner must
    # reach the ordinary decref checks just like a tracked object's owner.
    # Keep unknown/stale pointers and unregistered graph objects excluded.
    if pcc_gc_pointer_is_managed(value) == 0:
        return 0
    return _gc_graph_leaf_tag(load_i32(value, PYOBJECTHEADER_TYPE_TAG_OFFSET))


def _pcc_gc_store_plan_commit_locked(plan, owner, slot, value) -> int:
    if ptr_is_null(plan) != 0 or ptr_is_null(slot) != 0:
        return 0
    if load_i32(plan, 124) != 0:
        return 0
    store_i32(plan, 124, 1)
    backend: int = load_i64(plan, 112)
    if backend == 1 or backend == 2 or backend == 3 or backend == 4:
        pcc_gc_note_store()
    if backend == 3 or backend == 4:
        if _gc_forwarding_population() > 0 and _gc_relocation_candidate(value) != 0:
            value = pcc_gc_note_relocation_read(value)
    _py_incref_prepare(value, plan)
    if load_i64(plan, 48) != 0:
        # Debug-invalid value: the store note and the deferred diagnostic
        # run, but no slot publication or old-value release occurs.
        return 0
    if backend == 1 or backend == 2 or backend == 3 or backend == 4:
        pcc_gc_note_slot_write_barrier(
            owner, slot, load_ptr(plan, 0)
        )
    old = load_ptr(slot, 0)
    store_ptr(slot, 0, load_ptr(plan, 0))
    if ptr_is_null(old) == 0 and is_tagged_int(old) == 0:
        if backend == 4 and _gc4_old_slot_is_releasable(old) == 0:
            old = null()
    _py_decref_prepare(old, ptr_add(plan, 56))
    store_i32(plan, 124, 3)
    return 1


def _pcc_gc_store_plan_commit_sentinel_aware_locked(
    plan, owner, slot, value, sentinel
) -> int:
    """Ordinary slot store, except that ``sentinel`` is not a reference.

    A container tombstone is not a reference: nothing ever decrefs it, so
    refcounting it is pure work -- and refcount prepare's first act is the
    provenance probe, so tombstone traffic paid
    ``pcc_gc_granule_is_object_start`` to conclude that a sentinel is not an
    object.  Measured through PCC_GC_COUNTER_UNMANAGED_REFCOUNT_OPS at 5144
    such operations in a 4000-iteration set churn, and it happens in BOTH
    directions: storing the tombstone increfs it, and storing a real element
    over a tombstone decrefs it.  One entry point covers both, because both
    are the same statement -- this value is a sentinel, not a reference.

    py_dict needs none of this: its tombstone is an integer in an index array
    and never reaches the barrier.  py_set uses a pointer sentinel.  See the
    2026-09-08 update in
    docs/investigations/runtime-module-optimizer-throughput.md.

    Everything the barrier really owes is kept: the store note, the slot write
    barrier with today's arguments, the relocation read for a real value, the
    backend-4 known-object guard, and a real old value's release.
    """
    if ptr_is_null(plan) != 0 or ptr_is_null(slot) != 0:
        return 0
    if load_i32(plan, 124) != 0:
        return 0
    store_i32(plan, 124, 1)
    backend: int = load_i64(plan, 112)
    if backend == 1 or backend == 2 or backend == 3 or backend == 4:
        pcc_gc_note_store()
    if ptr_eq(value, sentinel) != 0:
        # A sentinel carries no forwarding entry and needs no incref.  The
        # reset keeps the plan's finish inert.
        _py_refcount_prepared_reset(plan, value)
    else:
        if backend == 3 or backend == 4:
            if (
                _gc_forwarding_population() > 0
                and _gc_relocation_candidate(value) != 0
            ):
                value = pcc_gc_note_relocation_read(value)
        _py_incref_prepare(value, plan)
        if load_i64(plan, 48) != 0:
            return 0
    if backend == 1 or backend == 2 or backend == 3 or backend == 4:
        pcc_gc_note_slot_write_barrier(owner, slot, load_ptr(plan, 0))
    old = load_ptr(slot, 0)
    store_ptr(slot, 0, load_ptr(plan, 0))
    if ptr_eq(old, sentinel) != 0:
        old = null()
    if ptr_is_null(old) == 0 and is_tagged_int(old) == 0:
        if backend == 4 and _gc4_old_slot_is_releasable(old) == 0:
            old = null()
    _py_decref_prepare(old, ptr_add(plan, 56))
    store_i32(plan, 124, 3)
    return 1


@c_abi_export("pcc_gc_store_ptr_plan_commit_sentinel_aware_locked")
def pcc_gc_store_ptr_plan_commit_sentinel_aware_locked(
    plan, owner, slot, value, sentinel
) -> int:
    return _pcc_gc_store_plan_commit_sentinel_aware_locked(
        plan, owner, slot, value, sentinel
    )


@c_abi_export("pcc_gc_store_root_plan_commit_locked")
def pcc_gc_store_root_plan_commit_locked(plan, slot, value) -> int:
    return _pcc_gc_store_plan_commit_locked(plan, null(), slot, value)


@c_abi_export("pcc_gc_store_ptr_plan_commit_locked")
def pcc_gc_store_ptr_plan_commit_locked(plan, owner, slot, value) -> int:
    return _pcc_gc_store_plan_commit_locked(plan, owner, slot, value)


def _pcc_gc_store_plan_finish(plan, emit_store_log: int) -> None:
    if ptr_is_null(plan) != 0:
        return
    state: int = load_i32(plan, 124)
    if (state & 1) == 0 or (state & 4) != 0:
        return
    store_i32(plan, 124, state | 4)
    if (
        emit_store_log != 0
        and load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) != 0
    ):
        pcc_diagnostics_runtime_log_event_code(
            2, 3, load_i64(plan, 112), 0, null()
        )
    _py_incref_finish(plan)
    if (state & 2) != 0:
        _py_decref_finish(ptr_add(plan, 56))


@c_abi_export("pcc_gc_store_root_plan_finish")
def pcc_gc_store_root_plan_finish(plan) -> None:
    _pcc_gc_store_plan_finish(plan, 1)


@c_abi_export("pcc_gc_store_ptr_plan_finish")
def pcc_gc_store_ptr_plan_finish(plan) -> None:
    _pcc_gc_store_plan_finish(plan, 0)


@c_abi_export("pcc_gc_store_root")
def pcc_gc_store_root(slot, value) -> None:
    if ptr_is_null(slot) != 0:
        return
    # py_incref's gate (one thread on backend 0, no probe, no log): the new
    # and old owners take py_incref/py_decref's own fast paths here instead
    # of two calls that repeat this gate. Any other object, and a count that
    # would reach zero, still goes through those functions.
    if (
        load_i32(global_addr("pcc_gc_refcount_fast"), 0) != 0
        and load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) == 0
    ):
        fast_old = load_ptr(slot, 0)
        if ptr_eq(fast_old, value) != 0:
            return
        if is_tagged_int(value) == 0 and ptr_is_null(value) == 0:
            counted: int = 0
            new_tag: int = load_i32(value, PYOBJECTHEADER_TYPE_TAG_OFFSET)
            if (
                (new_tag >= PY_TYPE_NONE and new_tag < PY_TYPE_CONTINUATION)
                or new_tag == PY_TYPE_CPY_HANDLE
                or (new_tag >= PY_TYPE_USER and new_tag < PY_TYPE_CEXT_TAG_BASE)
            ):
                if (load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & PY_FLAG_IMMORTAL) == 0:
                    new_rc: int = load_i64(value, PYOBJECTHEADER_REFCOUNT_OFFSET)
                    if new_rc >= 0:
                        store_i64(value, PYOBJECTHEADER_REFCOUNT_OFFSET, new_rc + 1)
                        counted = 1
            if counted == 0:
                py_incref(value)
        store_ptr(slot, 0, value)
        if is_tagged_int(fast_old) == 0 and ptr_is_null(fast_old) == 0:
            released: int = 0
            old_tag: int = load_i32(fast_old, PYOBJECTHEADER_TYPE_TAG_OFFSET)
            if (
                (old_tag >= PY_TYPE_NONE and old_tag < PY_TYPE_CONTINUATION)
                or old_tag == PY_TYPE_CPY_HANDLE
                or (old_tag >= PY_TYPE_USER and old_tag < PY_TYPE_CEXT_TAG_BASE)
            ):
                if (load_i32(fast_old, PYOBJECTHEADER_FLAGS_OFFSET) & PY_FLAG_IMMORTAL) == 0:
                    old_rc: int = load_i64(fast_old, PYOBJECTHEADER_REFCOUNT_OFFSET)
                    if old_rc > 1:
                        store_i64(fast_old, PYOBJECTHEADER_REFCOUNT_OFFSET, old_rc - 1)
                        released = 1
            if released == 0:
                py_decref(fast_old)
        return
    backend: int = _gc_backend_fast()
    if backend == 0:
        if load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) != 0:
            pcc_diagnostics_runtime_log_event_code(2, 3, backend, 0, null())
        old = load_ptr(slot, 0)
        if ptr_eq(old, value) != 0:
            return
        # Skip the refcount calls for values that cannot be refcounted.  A
        # tagged immediate and NULL both make py_incref/py_decref return
        # immediately, so the calls are pure overhead -- and codegen emits
        # ~47000 store_root sites, with `pcc_gc_store_root` measuring 17.5% of a
        # list-append loop against 5.6% for the append itself.  The slot store
        # still happens either way; only the no-op refcount calls are elided.
        if is_tagged_int(value) == 0 and ptr_is_null(value) == 0:
            py_incref(value)
        store_ptr(slot, 0, value)
        if is_tagged_int(old) == 0 and ptr_is_null(old) == 0:
            py_decref(old)
        return
    plan = stack_alloc(128)
    pcc_gc_store_root_plan_init(plan, backend)
    pcc_py_gc_minor_graph_lock()
    pcc_gc_store_root_plan_commit_locked(plan, slot, value)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_store_root_plan_finish(plan)


@c_abi_export("pcc_gc_store_root_take")
def pcc_gc_store_root_take(slot, value) -> None:
    # Ownership-transferring root store: the caller's reference to ``value``
    # moves into ``slot`` (no retain), the previous slot owner is released.
    # Replaces codegen's ``pin(v); store_root(slot, v); unpin(v); release(v)``
    # on every exact-int assignment (four calls, two of them no-ops for
    # tagged immediates).  Mirrors pcc_gc_store_root_take in py_obj.c.
    if ptr_is_null(slot) != 0:
        return
    backend: int = _gc_backend_fast()
    if backend == 0:
        if load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) != 0:
            pcc_diagnostics_runtime_log_event_code(2, 3, backend, 0, null())
        old = load_ptr(slot, 0)
        store_ptr(slot, 0, value)
        if is_tagged_int(old) == 0 and ptr_is_null(old) == 0:
            py_decref(old)
        return
    # Moving/tracing backends: commit through the relocation-aware plan (which
    # retains the stored value), then release the caller's reference through
    # the slot so a relocated address is never touched.
    pcc_gc_store_root(slot, value)
    stored = pcc_gc_load_ptr(null(), slot)
    if is_tagged_int(stored) == 0 and ptr_is_null(stored) == 0:
        py_decref(stored)


@c_abi_export("pcc_gc_root_copy_lease")
def pcc_gc_root_copy_lease(destination, source) -> int:
    """Retain an authoritative root into an empty root and lease its address.

    Destination is an already registered owning root. Source is an owning
    root, or a traced owning heap slot whose container/payload the caller has
    already leased. Both addresses remain valid throughout this operation;
    source is not a raw value copied into an unregistered local. On success
    the destination owns one extra reference
    and the returned lease token, which must be released through destination.
    NULL/tagged values return the ordinary no-op token. No Python exception is
    changed. A negative result leaves both owners unchanged.
    Caller holds no outer graph transaction; use the split form for lookup.
    """
    if ptr_is_null(destination) != 0 or ptr_is_null(source) != 0:
        return -1
    if ptr_eq(destination, source) != 0:
        return -1
    # py_incref's gate: one thread on backend 0, no probe, no log. For a
    # NULL, tagged or ordinary live object the transaction below only takes
    # the count-only lease, adds one reference and publishes the value into
    # the empty destination; do exactly that. Anything else takes it.
    if (
        load_i32(global_addr("pcc_gc_refcount_fast"), 0) != 0
        and load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) == 0
    ):
        if ptr_is_null(load_ptr(destination, 0)) == 0:
            return -4
        fast = load_ptr(source, 0)
        if ptr_is_null(fast) != 0 or is_tagged_int(fast) != 0:
            store_ptr(destination, 0, fast)
            return 0
        fast_tag: int = load_i32(fast, PYOBJECTHEADER_TYPE_TAG_OFFSET)
        if (
            (fast_tag >= PY_TYPE_NONE and fast_tag < PY_TYPE_CONTINUATION)
            or fast_tag == PY_TYPE_CPY_HANDLE
            or (fast_tag >= PY_TYPE_USER and fast_tag < PY_TYPE_CEXT_TAG_BASE)
        ):
            fast_rc: int = load_i64(fast, PYOBJECTHEADER_REFCOUNT_OFFSET)
            if (load_i32(fast, PYOBJECTHEADER_FLAGS_OFFSET) & PY_FLAG_IMMORTAL) == 0 and fast_rc >= 0:
                fast_token: int = pcc_gc_foreign_lease_acquire(source)
                if fast_token >= 0:
                    store_i64(fast, PYOBJECTHEADER_REFCOUNT_OFFSET, fast_rc + 1)
                    store_ptr(destination, 0, fast)
                return fast_token
    plan = stack_alloc(128)
    _gc_backend_fast()
    pcc_py_gc_minor_graph_lock()
    # Configuration may change while acquiring the graph lock, before our
    # first address lease exists. Use the backend of this transaction.
    backend: int = _gc_backend_fast()
    pcc_gc_store_root_plan_init(plan, backend)
    if ptr_is_null(load_ptr(destination, 0)) == 0:
        pcc_py_gc_minor_graph_unlock()
        return -4
    # Acquire reloads/resolves source after any contended outer-lock wait.
    # Its nested unlock cannot park while this transaction owns the lock.
    token: int = pcc_gc_foreign_lease_acquire(source)
    if token >= 0:
        value = load_ptr(source, 0)
        if pcc_gc_store_root_plan_commit_locked(plan, destination, value) == 0:
            released: int = pcc_gc_foreign_lease_release(source, token)
            token = -4 if released == 0 else -3
    pcc_py_gc_minor_graph_unlock()
    # Deferred diagnostics/refcount work runs only after the destination and
    # counted lease have been published, never while a borrowed raw copy is
    # the sole description of an operand.
    pcc_gc_store_root_plan_finish(plan)
    return token


@c_abi_export("pcc_gc_root_move")
def pcc_gc_root_move(destination, source) -> int:
    """Move one owner between distinct registered roots, without a raw return.

    Destination must be empty. A lease already owned by source, if any, moves
    with the value and must subsequently be released through destination.
    Neither references nor lease counts change. Failure leaves owners intact.
    """
    if ptr_is_null(destination) != 0 or ptr_is_null(source) != 0:
        return -1
    if ptr_eq(destination, source) != 0:
        return -1
    _gc_backend_fast()
    pcc_py_gc_minor_graph_lock()
    if ptr_is_null(load_ptr(destination, 0)) == 0:
        pcc_py_gc_minor_graph_unlock()
        return -4
    value = pcc_gc_resolve_root_slot_unlocked(source, 0)
    pcc_gc_note_slot_write_barrier(null(), destination, value)
    store_ptr(destination, 0, value)
    store_ptr(source, 0, null())
    pcc_py_gc_minor_graph_unlock()
    return 0


@c_abi_export("pcc_gc_root_copy_borrowed_lease")
def pcc_gc_root_copy_borrowed_lease(destination, source) -> int:
    """Owning destination from an authoritative borrowed slot.

    A parameter's borrowed root must not use the owning-slot resolver: healing
    that source does not acquire/release a reference. Destination still gains
    one owner and an independently counted lease before outer graph unlock.
    Source is a registered borrowed root, or traced borrowed heap metadata
    whose containing object/payload already has a caller-owned address lease.
    The actual source owner must remain live for the invocation.
    Caller holds no outer graph transaction; use the split form for lookup.
    """
    if ptr_is_null(destination) != 0 or ptr_is_null(source) != 0:
        return -1
    if ptr_eq(destination, source) != 0:
        return -1
    if (
        load_i32(global_addr("pcc_gc_config_initialized"), 0) != 0
        and load_i32(global_addr("pcc_gc_backend_selected"), 0) == 0
        and load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) == 0
    ):
        # GC0 neither moves nor traces roots concurrently (its cycle
        # collection stops the world), and roots have no barrier: the copy
        # is one owner and a plain store, with the no-op lease token. No
        # graph lock, so concurrent threads do not serialize here.
        if ptr_is_null(load_ptr(destination, 0)) == 0:
            return -4
        copied = load_ptr(source, 0)
        if is_tagged_int(copied) == 0 and ptr_is_null(copied) == 0:
            py_incref(copied)
        store_ptr(destination, 0, copied)
        return 0
    plan = stack_alloc(128)
    rollback = stack_alloc(128)
    _gc_backend_fast()
    pcc_py_gc_minor_graph_lock()
    backend: int = _gc_backend_fast()
    pcc_gc_store_root_plan_init(plan, backend)
    pcc_gc_store_root_plan_init(rollback, backend)
    if ptr_is_null(load_ptr(destination, 0)) == 0:
        pcc_py_gc_minor_graph_unlock()
        return -4
    value = pcc_gc_load_borrowed_ptr(null(), source)
    token: int = -4
    if pcc_gc_store_root_plan_commit_locked(plan, destination, value) != 0:
        token = pcc_gc_foreign_lease_acquire(destination)
        if token < 0:
            # No raw temporary reaches the deferred release. The rollback
            # prepare clears destination under the same graph transaction.
            pcc_gc_store_root_plan_commit_locked(rollback, destination, null())
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_store_root_plan_finish(plan)
    pcc_gc_store_root_plan_finish(rollback)
    return token


@c_abi_export("pcc_gc_root_copy_lease_prepare_locked")
def pcc_gc_root_copy_lease_prepare_locked(destination, source, source_borrowed: int, plan) -> int:
    """Publish a copied owner/lease inside an existing graph transaction.

    Runtime configuration is complete and the caller owns the graph lock.
    plan has 256 writable bytes and must be finished after outermost unlock,
    including failures. Slot preconditions match the ordinary copy helpers.
    """
    backend: int = _gc_backend_fast()
    pcc_gc_store_root_plan_init(plan, backend)
    rollback = ptr_add(plan, 128)
    pcc_gc_store_root_plan_init(rollback, backend)
    if ptr_is_null(destination) != 0 or ptr_is_null(source) != 0:
        return -1
    if ptr_eq(destination, source) != 0:
        return -1
    if ptr_is_null(load_ptr(destination, 0)) == 0:
        return -4
    if source_borrowed == 0:
        token: int = pcc_gc_foreign_lease_acquire(source)
        if token < 0:
            return token
        value = load_ptr(source, 0)
        if pcc_gc_store_root_plan_commit_locked(plan, destination, value) == 0:
            if pcc_gc_foreign_lease_release(source, token) != 0:
                return -3
            return -4
        return token
    value = pcc_gc_load_borrowed_ptr(null(), source)
    if pcc_gc_store_root_plan_commit_locked(plan, destination, value) == 0:
        return -4
    token = pcc_gc_foreign_lease_acquire(destination)
    if token < 0:
        pcc_gc_store_root_plan_commit_locked(rollback, destination, null())
    return token


@c_abi_export("pcc_gc_root_copy_lease_finish")
def pcc_gc_root_copy_lease_finish(plan) -> None:
    pcc_gc_store_root_plan_finish(plan)
    pcc_gc_store_root_plan_finish(ptr_add(plan, 128))


@c_abi_export("pcc_gc_try_store_ptr_take")
def pcc_gc_try_store_ptr_take(owner, slot, value) -> int:
    if ptr_is_null(slot) != 0 or _gc_backend_fast() != 0:
        return 0
    if load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) != 0:
        pcc_diagnostics_runtime_log_event_code(2, 3, 0, 0, owner)
    old = load_ptr(slot, 0)
    store_ptr(slot, 0, value)
    # Same-value stores still consume the incoming additional owner.
    if is_tagged_int(old) == 0 and ptr_is_null(old) == 0:
        py_decref(old)
    return 1


@c_abi_export("pcc_gc_try_take_ptr")
def pcc_gc_try_take_ptr(owner, slot):
    if ptr_is_null(slot) != 0 or _gc_backend_fast() != 0:
        return null()
    if load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) != 0:
        pcc_diagnostics_runtime_log_event_code(2, 3, 0, 0, owner)
    value = load_ptr(slot, 0)
    store_ptr(slot, 0, global_load_ptr("py_None"))
    return value


def _frame_registry_inactive() -> int:
    # GC0 without frame roots registers nothing. Decide it here, in the thin
    # entry every compiled frame calls, before entering the registry body
    # (whose prologue and slot setup cost more than the early return).
    if load_i32(global_addr("pcc_gc_config_initialized"), 0) == 0:
        return 0
    if load_i32(global_addr("pcc_gc_backend_selected"), 0) != 0:
        return 0
    if load_i32(global_addr("pcc_gc_backend0_frame_roots_enabled"), 0) != 0:
        return 0
    return 1


@c_abi_export("pcc_gc_frame_enter")
def pcc_gc_frame_enter(frame_map, slots) -> None:
    if _frame_registry_inactive() != 0:
        return
    pcc_gc_note_frame_enter(frame_map, slots)
    return


@c_abi_export("pcc_gc_frame_leave")
def pcc_gc_frame_leave(slots) -> None:
    if _frame_registry_inactive() != 0:
        return
    pcc_gc_note_frame_leave(slots)
    return


@c_abi_export("pcc_gc_frame_enter_lifo")
def pcc_gc_frame_enter_lifo(frame_map, slots) -> None:
    if _frame_registry_inactive() != 0:
        return
    pcc_gc_note_frame_enter_lifo(frame_map, slots)
    return


@c_abi_export("pcc_gc_frame_leave_lifo")
def pcc_gc_frame_leave_lifo(slots) -> None:
    if _frame_registry_inactive() != 0:
        return
    pcc_gc_note_frame_leave_lifo(slots)
    return


# Mapped entries: frames of code that carries precise stack-map records. The
# stack walk roots them while it is active (main publishes the table before
# any frame enters), so they register only when no walk can see them. A
# frame's enter and leave run under the same walk state.
@c_abi_export("pcc_gc_frame_enter_mapped")
def pcc_gc_frame_enter_mapped(frame_map, slots) -> None:
    if _frame_registry_inactive() != 0:
        return
    if pcc_gc_frame_slots_walked(slots) != 0:
        return
    pcc_gc_note_frame_enter(frame_map, slots)
    return


@c_abi_export("pcc_gc_frame_leave_mapped")
def pcc_gc_frame_leave_mapped(slots) -> None:
    if _frame_registry_inactive() != 0:
        return
    if pcc_gc_frame_slots_walked(slots) != 0:
        return
    pcc_gc_note_frame_leave(slots)
    return


@c_abi_export("pcc_gc_frame_enter_lifo_mapped")
def pcc_gc_frame_enter_lifo_mapped(frame_map, slots) -> None:
    if _frame_registry_inactive() != 0:
        return
    if pcc_gc_frame_slots_walked(slots) != 0:
        return
    pcc_gc_note_frame_enter_lifo(frame_map, slots)
    return


@c_abi_export("pcc_gc_frame_leave_lifo_mapped")
def pcc_gc_frame_leave_lifo_mapped(slots) -> None:
    if _frame_registry_inactive() != 0:
        return
    if pcc_gc_frame_slots_walked(slots) != 0:
        return
    pcc_gc_note_frame_leave_lifo(slots)
    return


def _gc_tracing_automatic_collect(mark: int) -> int:
    # One automatic collection of GC1..GC4 with the world stopped, as an
    # explicit gc.collect() runs it, but a single cycle. With mark set (GC3,
    # which has no collector thread) it finishes the mark in progress or runs
    # one; otherwise it sweeps only a mark that has completed (GC1/GC2's
    # collector thread or GC4's automatic step completed it). An explicit
    # collect keeps stepping until nothing is left.
    if load_i32(global_addr("pcc_gc_explicit_collect_active"), 0) != 0:
        return 0
    _pcc_gc_fire_callbacks(cstr("start"), 5)
    stw: int = pcc_stop_the_world()
    while stw != 0:
        pcc_thread_safepoint()
        stw = pcc_stop_the_world()
    pcc_gc_begin_explicit_tracing_collect()
    collected: int = 0
    idle_rounds: int = 0
    while mark != 0 and pcc_gc_sweep_owed() == 0 and idle_rounds < 4:
        if pcc_gc_step(1024) > 0:
            idle_rounds = 0
        else:
            idle_rounds = idle_rounds + 1
    if pcc_gc_sweep_owed() != 0:
        collected = pcc_gc_collect_tracing()
    pcc_gc_end_explicit_tracing_collect()
    pcc_resume_world()
    pcc_gc_external_resource_poll()
    _pcc_gc_fire_callbacks(cstr("stop"), 4)
    return collected


def _gc_automatic_collection_enabled() -> int:
    # gc.disable() and threshold0 == 0 turn automatic collection off.
    if load_i32(global_addr("py_gc_enabled"), 0) == 0:
        return 0
    if load_i32(global_addr("py_gc_threshold0"), 0) <= 0:
        return 0
    return 1


def _gc_maybe_auto_collect() -> None:
    # The automatic collection pending for GC0..GC3 (they share the flag
    # that compiled gates poll with GC4's automatic step). py_gc_track
    # schedules GC0's and GC3's from tracking growth; GC1/GC2's collector
    # thread requests the sweep of each mark it completes. One thread takes
    # the request and collects with the world stopped, as an explicit
    # gc.collect() does. A collection already in progress (its finalizers
    # reach gates too) or a disabled collector drops it; the next growth or
    # completed mark requests another.
    if pcc_thread_no_park_depth() != 0 or pcc_py_gc_minor_graph_lock_depth() != 0:
        return
    pending = global_addr("pcc_gc_backend4_auto_step_pending")
    if atomic_rmw_i32("xchg", pending, 0, 0, "acq_rel") == 0:
        return
    if _gc_automatic_collection_enabled() == 0:
        return
    if load_i32(global_addr("py_gc_collecting"), 0) != 0:
        return
    backend: int = _gc_backend_fast()
    if backend == 0:
        pcc_gc_collect(0)
    elif backend == 3:
        _gc_tracing_automatic_collect(1)
    else:
        _gc_tracing_automatic_collect(0)
    atomic_store_i64(
        global_addr("pcc_gc_tracked_after_collect"),
        0,
        load_i32(global_addr("py_gc_tracked_count"), 0),
        "relaxed",
    )


def _gc4_maybe_auto_step() -> None:
    # GC4 steps from allocation debt. Its steps mark, relocate and remap;
    # reclaiming what a completed mark found unreachable is a collection
    # like an explicit gc.collect(), which sweeps that mark.
    if _gc_automatic_collection_enabled() == 0:
        atomic_store_i32(
            global_addr("pcc_gc_backend4_auto_step_pending"), 0, 0, "release"
        )
        return
    commits: int = load_i64(global_addr("pcc_gc_tracing_finish_commits"), 0)
    pcc_gc_backend4_maybe_auto_step()
    if load_i64(global_addr("pcc_gc_tracing_finish_commits"), 0) == commits:
        return
    if pcc_thread_no_park_depth() != 0 or pcc_py_gc_minor_graph_lock_depth() != 0:
        return
    _gc_tracing_automatic_collect(0)


@c_abi_export("pcc_gc_safepoint")
def pcc_gc_safepoint() -> None:
    pcc_gc_note_safepoint()
    # Emitted only at application gates of compiled program code.
    pcc_thread_safepoint_gate()
    if _gc_backend_fast() == 4:
        _gc4_maybe_auto_step()
    else:
        _gc_maybe_auto_collect()
    pcc_gc_external_resource_poll()
    return


def _py_gc_callbacks_ensure():
    callbacks = global_load_ptr("py_gc_callbacks")
    if ptr_is_null(callbacks) != 0:
        callbacks = py_list_new(0)
        if ptr_is_null(callbacks) != 0:
            return null()
        global_store_ptr("py_gc_callbacks", callbacks)
        pcc_gc_pin(callbacks)
    return callbacks


@c_abi_export("py_gc_callbacks_list")
def py_gc_callbacks_list():
    callbacks = _py_gc_callbacks_ensure()
    if ptr_is_null(callbacks) == 0:
        py_incref(callbacks)
    return callbacks


@c_abi_export("py_gc_callbacks_append")
def py_gc_callbacks_append(callback) -> None:
    callbacks = _py_gc_callbacks_ensure()
    if ptr_is_null(callbacks) != 0:
        return
    py_list_append(callbacks, callback)
    return


def _pcc_gc_callback_eq(a, b) -> int:
    if ptr_eq(a, b) != 0:
        return 1
    if ptr_is_null(a) != 0:
        return 0
    if ptr_is_null(b) != 0:
        return 0
    if is_tagged_int(a) != 0 or is_tagged_int(b) != 0:
        return py_obj_eq(a, b)
    a = pcc_gc_note_relocation_read(a)
    b = pcc_gc_note_relocation_read(b)
    if ptr_eq(a, b) != 0:
        return 1
    if load_i32(a, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_FUNC:
        if load_i32(b, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_FUNC:
            if ptr_eq(load_ptr(a, 56), load_ptr(b, 56)) != 0:
                a_captures = pcc_gc_load_ptr(a, ptr_add(a, 64))
                b_captures = pcc_gc_load_ptr(b, ptr_add(b, 64))
                return py_obj_eq(a_captures, b_captures)
    return py_obj_eq(a, b)


@c_abi_export("py_gc_callbacks_remove")
def py_gc_callbacks_remove(callback) -> None:
    callbacks = _py_gc_callbacks_ensure()
    if ptr_is_null(callbacks) != 0:
        return
    length: int = load_i64(callbacks, PYLISTOBJECT_LENGTH_OFFSET)
    items = load_ptr(callbacks, PYLISTOBJECT_ITEMS_OFFSET)
    i: int = 0
    while i < length:
        existing = pcc_gc_load_ptr(callbacks, ptr_add(items, i * 8))
        equal: int = _pcc_gc_callback_eq(existing, callback)
        if py_err_occurred() != 0:
            return
        if equal != 0:
            if i < length - 1:
                src = ptr_add(items, (i + 1) * 8)
                dst = ptr_add(items, i * 8)
                memmove(dst, src, (length - i - 1) * 8)
            store_i64(callbacks, PYLISTOBJECT_LENGTH_OFFSET, length - 1)
            py_decref(existing)
            return
        i = i + 1
    py_list_remove(callbacks, callback)
    return


def _pcc_gc_fire_callbacks(phase, phase_len: int) -> None:
    callbacks = global_load_ptr("py_gc_callbacks")
    if ptr_is_null(callbacks) != 0:
        return
    if load_i32(global_addr("py_gc_callbacks_firing"), 0) != 0:
        return
    n: int = py_list_len(callbacks)
    if n <= 0:
        return

    store_i32(global_addr("py_gc_callbacks_firing"), 0, 1)
    phase_obj = py_str_new(phase, phase_len)
    info = py_dict_new()
    if ptr_is_null(phase_obj) != 0 or ptr_is_null(info) != 0:
        if ptr_is_null(phase_obj) == 0:
            py_decref(phase_obj)
        if ptr_is_null(info) == 0:
            py_decref(info)
        store_i32(global_addr("py_gc_callbacks_firing"), 0, 0)
        return

    i: int = 0
    while i < n:
        callback = py_list_get(callbacks, i)
        if ptr_is_null(callback) == 0:
            args = py_tuple_new(2)
            if ptr_is_null(args) == 0:
                py_tuple_set_item(args, 0, phase_obj)
                py_tuple_set_item(args, 1, info)
                result = py_obj_call(callback, args, global_load_ptr("py_None"))
                if ptr_is_null(result) == 0:
                    py_decref(result)
                py_decref(args)
            py_decref(callback)
            py_clear_exception()
        i = i + 1

    py_decref(info)
    py_decref(phase_obj)
    store_i32(global_addr("py_gc_callbacks_firing"), 0, 0)
    return


@c_abi_export("pcc_gc_collect")
def pcc_gc_collect(reason: int) -> int:
    backend: int = _gc_backend_fast()
    # Reentrancy guard (CPython gc.collecting semantics): a gc.collect() invoked
    # from a finalizer (__del__) that runs DURING an in-progress tracing collect
    # must be a no-op. Otherwise the reentrant mark re-whitens objects the outer
    # sweep is mid-iteration on (clobbering its GC_SWEEP_CANDIDATE/1024 flags) and
    # the reentrant sweep frees nodes the outer _sweep_unreachable still holds ->
    # use-after-free segfault on #1/#2/#3/#4. The outer collect keeps
    # pcc_gc_explicit_collect_active set across its whole begin..end (mark+sweep)
    # window, so a non-zero value here means we are reentrant; return 0 before any
    # STW/callbacks (re-entering STW on an already-stopped world would also hang).
    # #0 (refcount+cycle) never sets the flag and is reentrancy-safe already.
    # See gc-5backend-reentrant-collect-during-finalizer-no-libpython.md.
    if backend != 0:
        if load_i32(global_addr("pcc_gc_explicit_collect_active"), 0) != 0:
            return 0
    if load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) != 0:
        pcc_diagnostics_runtime_log_event_code(2, 1, reason, backend, null())
    _pcc_gc_fire_callbacks(cstr("start"), 5)
    collected: int = 0
    if backend == 0:
        # G-P3: time backend 0's explicit cycle collect (its only
        # pause-like window) so pause telemetry covers all backends.
        pause_t0: int = pcc_runtime_monotonic_us()
        collected = py_gc_collect()
        pcc_gc_record_explicit_pause(pause_t0, pcc_runtime_monotonic_us())
    else:
        stw: int = pcc_stop_the_world()
        while stw != 0:
            pcc_thread_safepoint()
            stw = pcc_stop_the_world()
        if backend == 3 or backend == 4:
            # Stack-map step 3: with PCC_GC_STACKMAP_DIFF and a published table,
            # compare the stopped stack's mapped roots with the registered ones.
            pcc_gc_precise_stack_diff_if_requested()
        pcc_gc_begin_explicit_tracing_collect()
        # Sweep only when a mark cycle has actually finished.  The gate is
        # pcc_gc_sweep_owed(), not pcc_gc_has_tracing_sweep(): the latter
        # ignores mark_active, so candidates left over from a previous
        # unfinished sweep read true mid-mark, and sweeping there frees live
        # objects (measured).
        #
        # The round bound is a LIVENESS backstop only and is not load bearing
        # for correctness: a step legitimately reports zero progress at a phase
        # boundary (measured 1, 0, 6 on backend 1), so breaking on the first
        # zero returns with work outstanding, while looping unbounded would spin
        # if the collector never converges.  Sweeping stays gated either way.
        idle_rounds: int = 0
        draining: int = 1
        while draining != 0:
            if pcc_gc_sweep_owed() != 0:
                swept: int = pcc_gc_collect_tracing()
                collected = collected + swept
                if swept == 0 and pcc_gc_sweep_owed() != 0:
                    draining = 0
                else:
                    idle_rounds = 0
            else:
                stepped: int = pcc_gc_step(1024)
                if stepped > 0:
                    idle_rounds = 0
                else:
                    idle_rounds = idle_rounds + 1
                    if idle_rounds >= 4:
                        draining = 0
        pcc_gc_end_explicit_tracing_collect()
        pcc_resume_world()
    # Release callbacks run after the tracing world is resumed. The shared
    # C-kernel registry has a zero-ready fast path and contains no PyObjects.
    pcc_gc_external_resource_poll()
    _pcc_gc_fire_callbacks(cstr("stop"), 4)
    if load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) != 0:
        pcc_diagnostics_runtime_log_event_code(2, 2, collected, backend, null())
    return collected


pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)


@c_abi_export("pcc_gc_immortalize")
def pcc_gc_immortalize(o) -> None:
    # Mirror of C pcc_gc_immortalize: pin + set PY_FLAG_IMMORTAL (0x1) so
    # py_incref/py_decref stop touching this object's refcount cache line.
    if ptr_is_null(o) != 0:
        return
    if is_tagged_int(o) != 0:
        return
    pcc_gc_pin(o)
    flags: int = load_i32(o, PYOBJECTHEADER_FLAGS_OFFSET)
    store_i32(o, PYOBJECTHEADER_FLAGS_OFFSET, flags | PY_FLAG_IMMORTAL)
    return


_pcc_debug_bad_incref = extern("pcc_debug_bad_incref", (c_ptr, c_int32), c_void)
_pcc_debug_runtime_enabled = extern("pcc_debug_runtime_enabled_py", (), c_int64)


def _py_refcount_prepared_reset(prepared, o) -> None:
    store_ptr(prepared, 0, o)
    store_i64(prepared, 8, -1)
    store_i64(prepared, 16, 0)
    store_i64(prepared, 24, -1)
    store_i64(prepared, 32, 0)
    store_i64(prepared, 40, 0)
    store_i64(prepared, 48, 0)


# PY_TYPE_CEXT_TAG_BASE is the first tag handed out by the extension registry.
# The generated ABI import is statically resolved by the runtime-library
# pipeline. Tags from PY_TYPE_USER_CLASS_START up to this boundary belong to
# pcc user classes and must be refcounted.
def _py_incref_prepare(o, prepared) -> None:
    _py_refcount_prepared_reset(prepared, o)
    if ptr_is_null(o) != 0:
        return
    if is_tagged_int(o) != 0:
        return
    backend: int = 0
    if load_i32(global_addr("pcc_gc_config_initialized"), 0) == 0:
        backend = pcc_gc_backend()
    else:
        backend = load_i32(global_addr("pcc_gc_backend_selected"), 0)
    if _refcount_provenance_probe_enabled() != 0 and not _ptr_can_have_header(o):
        _note_unmanaged_refcount_op()
        return
    tag: int = load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    if (
        tag < PY_TYPE_NONE
        or (tag > PY_TYPE_CPY_HANDLE and tag < PY_TYPE_USER and tag != PY_TYPE_ELLIPSIS and tag != PY_TYPE_NOTIMPLEMENTED)
        or (tag >= (PY_TYPE_CEXT_TAG_BASE) and pcc_capi_is_cext_type_tag(tag) == 0)
    ):
        if _pcc_debug_runtime_enabled() != 0:
            # Debug-invalid value: a store plan must not publish it, and the
            # diagnostic runs from finish once the caller dropped its lock.
            store_i64(prepared, 8, tag)
            store_i64(prepared, 48, 1)
        return
    flags: int = load_i32(o, PYOBJECTHEADER_FLAGS_OFFSET)
    if (backend == 1 or backend == 2) and flags == 0:
        return
    if (
        backend == 4
        and _gc_forwarding_population() > 0
        and (flags & 2048) != 0
    ):
        # Count-on-NEW model (gc4 remap design): after relocation the
        # outstanding refcount lives on the NEW copy; resolve first.
        resolved = pcc_gc_note_relocation_read(o)
        if ptr_is_null(resolved) == 0 and ptr_eq(resolved, o) == 0:
            o = resolved
            tag = load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET)
            flags = load_i32(o, PYOBJECTHEADER_FLAGS_OFFSET)
    store_ptr(prepared, 0, o)
    store_i64(prepared, 8, tag)
    store_i64(prepared, 16, flags)
    store_i64(prepared, 24, backend)
    if (
        (
            tag == PY_TYPE_CONTINUATION
            or tag == PY_TYPE_VIRTUAL_THREAD
            or tag == PY_TYPE_VTHREAD_CHANNEL
        )
        and (flags & PY_FLAG_GC_TRACKED) == 0
        and pcc_gc_object_is_known(o) == 0
    ):
        return
    if (flags & PY_FLAG_IMMORTAL) != 0:
        return
    if backend == 3 and (flags & 4096) != 0 and (flags & 256) != 0:
        if load_i64(o, PYOBJECTHEADER_REFCOUNT_OFFSET) <= 0:
            return
    if load_i64(o, PYOBJECTHEADER_REFCOUNT_OFFSET) < 0:
        return  # already freed (poisoned); stray reference, skip
    new_rc: int = pcc_refcount_incref(o)
    store_i64(prepared, 32, new_rc)
    store_i64(prepared, 40, 1)


def _py_incref_finish(prepared) -> None:
    if load_i64(prepared, 48) != 0:
        _pcc_debug_bad_incref(load_ptr(prepared, 0), load_i64(prepared, 8))
        return
    if load_i64(prepared, 40) == 0:
        return
    if load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) != 0:
        pcc_diagnostics_runtime_log_event_code(
            3,
            1,
            load_i64(prepared, 32),
            load_i64(prepared, 8),
            load_ptr(prepared, 0),
        )


@c_abi_export("pcc_gc_retain_plan_prepare_locked")
def pcc_gc_retain_plan_prepare_locked(plan, value):
    if ptr_is_null(plan) != 0:
        return null()
    _py_incref_prepare(value, plan)
    return load_ptr(plan, 0)


@c_abi_export("pcc_gc_retain_plan_finish")
def pcc_gc_retain_plan_finish(plan) -> None:
    if ptr_is_null(plan) != 0:
        return
    _py_incref_finish(plan)
    _py_refcount_prepared_reset(plan, null())


@c_abi_export("py_incref")
def py_incref(o) -> None:
    if ptr_is_null(o) != 0 or is_tagged_int(o) != 0:
        return
    # Backend 0, no probe, no logging, any number of threads: an ordinary
    # object that is neither immortal nor dying takes one relaxed atomic
    # add. The caller holds a reference, so the count cannot reach zero
    # concurrently; negative counts are collector states and keep the path
    # below.
    if (
        load_i32(global_addr("pcc_gc_refcount_atomic"), 0) != 0
        and load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) == 0
    ):
        atomic_tag: int = load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET)
        if (
            (atomic_tag >= PY_TYPE_NONE and atomic_tag < PY_TYPE_CONTINUATION)
            or atomic_tag == PY_TYPE_CPY_HANDLE
            or (atomic_tag >= PY_TYPE_USER and atomic_tag < PY_TYPE_CEXT_TAG_BASE)
        ):
            if (load_i32(o, PYOBJECTHEADER_FLAGS_OFFSET) & PY_FLAG_IMMORTAL) == 0:
                if atomic_load_i64(o, PYOBJECTHEADER_REFCOUNT_OFFSET, "relaxed") >= 0:
                    atomic_rmw_i64("add", o, PYOBJECTHEADER_REFCOUNT_OFFSET, 1, "relaxed")
                    return
    # Backend 0, no probe, one thread, no logging: an ordinary object that is
    # neither immortal nor dying takes a plain increment -- what the path
    # below does for it after its configuration loads, a dozen tag compares
    # and the kernel call.  Everything else falls through unchanged.
    if (
        load_i32(global_addr("pcc_gc_refcount_fast"), 0) != 0
        and load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) == 0
    ):
        fast_tag: int = load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET)
        if (
            (fast_tag >= PY_TYPE_NONE and fast_tag < PY_TYPE_CONTINUATION)
            or fast_tag == PY_TYPE_CPY_HANDLE
            or (fast_tag >= PY_TYPE_USER and fast_tag < PY_TYPE_CEXT_TAG_BASE)
        ):
            if (load_i32(o, PYOBJECTHEADER_FLAGS_OFFSET) & PY_FLAG_IMMORTAL) == 0:
                fast_rc: int = load_i64(o, PYOBJECTHEADER_REFCOUNT_OFFSET)
                if fast_rc >= 0:
                    store_i64(o, PYOBJECTHEADER_REFCOUNT_OFFSET, fast_rc + 1)
                    return
    # Standalone GC0 operations need no prepare record on the nonterminal
    # path. Keep provenance/type checks and share terminal finish with plans.
    if _gc_backend_fast() != 0:
        prepared = stack_alloc(56)
        _py_incref_prepare(o, prepared)
        _py_incref_finish(prepared)
        return
    if _refcount_provenance_probe_enabled() != 0 and not _ptr_can_have_header(o):
        _note_unmanaged_refcount_op()
        return
    tag: int = load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    if tag < PY_TYPE_NONE or (tag > PY_TYPE_CPY_HANDLE and tag < PY_TYPE_USER and tag != PY_TYPE_ELLIPSIS and tag != PY_TYPE_NOTIMPLEMENTED) or (tag >= (PY_TYPE_CEXT_TAG_BASE) and pcc_capi_is_cext_type_tag(tag) == 0):
        return
    flags: int = load_i32(o, PYOBJECTHEADER_FLAGS_OFFSET)
    if (tag == PY_TYPE_CONTINUATION or tag == PY_TYPE_VIRTUAL_THREAD or tag == PY_TYPE_VTHREAD_CHANNEL) and (flags & PY_FLAG_GC_TRACKED) == 0 and pcc_gc_object_is_known(o) == 0:
        return
    if (flags & PY_FLAG_IMMORTAL) != 0:
        return
    if load_i64(o, PYOBJECTHEADER_REFCOUNT_OFFSET) < 0:
        return
    new_rc: int = pcc_refcount_incref(o)
    if load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) != 0:
        pcc_diagnostics_runtime_log_event_code(3, 1, new_rc, tag, o)


def _py_decref_prepare(o, prepared) -> None:
    _py_refcount_prepared_reset(prepared, o)
    if ptr_is_null(o) != 0:
        return
    if is_tagged_int(o) != 0:
        return
    backend: int = 0
    if load_i32(global_addr("pcc_gc_config_initialized"), 0) == 0:
        backend = pcc_gc_backend()
    else:
        backend = load_i32(global_addr("pcc_gc_backend_selected"), 0)
    if _refcount_provenance_probe_enabled() != 0 and not _ptr_can_have_header(o):
        _note_unmanaged_refcount_op()
        return
    tag_dbg: int = load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    if (
        tag_dbg < PY_TYPE_NONE
        or (tag_dbg > PY_TYPE_CPY_HANDLE and tag_dbg < PY_TYPE_USER and tag_dbg != PY_TYPE_ELLIPSIS and tag_dbg != PY_TYPE_NOTIMPLEMENTED)
        or (tag_dbg >= (PY_TYPE_CEXT_TAG_BASE) and pcc_capi_is_cext_type_tag(tag_dbg) == 0)
    ):
        return
    flags: int = load_i32(o, PYOBJECTHEADER_FLAGS_OFFSET)
    if (backend == 1 or backend == 2) and flags == 0:
        return
    if (
        backend == 4
        and _gc_forwarding_population() > 0
        and (flags & 2048) != 0
    ):
        # Count-on-NEW model (gc4 remap design): resolve before
        # decrementing; old copies are immortal shells so an
        # unresolvable stray decref is a no-op below.
        resolved = pcc_gc_note_relocation_read(o)
        if ptr_is_null(resolved) == 0 and ptr_eq(resolved, o) == 0:
            o = resolved
            tag_dbg = load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET)
            flags = load_i32(o, PYOBJECTHEADER_FLAGS_OFFSET)
    store_ptr(prepared, 0, o)
    store_i64(prepared, 8, tag_dbg)
    store_i64(prepared, 16, flags)
    store_i64(prepared, 24, backend)
    if (
        (
            tag_dbg == PY_TYPE_CONTINUATION
            or tag_dbg == PY_TYPE_VIRTUAL_THREAD
            or tag_dbg == PY_TYPE_VTHREAD_CHANNEL
        )
        and (flags & PY_FLAG_GC_TRACKED) == 0
        and pcc_gc_object_is_known(o) == 0
    ):
        return
    if (flags & PY_FLAG_IMMORTAL) != 0:
        return
    if backend == 3 and (flags & 4096) != 0 and (flags & 256) != 0:
        if load_i64(o, PYOBJECTHEADER_REFCOUNT_OFFSET) <= 0:
            return
    if (
        backend == 4
        and (flags & (2048 | 8192 | 131072)) != 0
        and pcc_gc_object_is_known(o) == 0
    ):
        return
    # Match the C owner: never mutate a non-positive counter under a graph/root
    # lock.  Capture the underflow token here and fail-stop from finish after
    # the caller has released that lock.
    pre_rc: int = load_i64(o, PYOBJECTHEADER_REFCOUNT_OFFSET)
    if pre_rc <= 0:
        store_i64(prepared, 48, 1)
        return
    new_rc: int = pcc_refcount_decref(o)
    store_i64(prepared, 32, new_rc)
    store_i64(prepared, 40, 1)
    if new_rc == 0:
        # Publish terminal ownership under the root/graph lock.  The
        # potentially reentrant finalizer/deallocator tail runs in finish.
        atomic_rmw_i32("or", o, PYOBJECTHEADER_FLAGS_OFFSET, 524288, "release")


def _py_decref_finish(prepared) -> None:
    if load_i64(prepared, 48) != 0:
        _pcc_debug_bad_incref(load_ptr(prepared, 0), load_i64(prepared, 8))
        return
    if load_i64(prepared, 40) == 0:
        return
    o = load_ptr(prepared, 0)
    tag_dbg: int = load_i64(prepared, 8)
    flags: int = load_i64(prepared, 16)
    backend: int = load_i64(prepared, 24)
    new_rc: int = load_i64(prepared, 32)
    if new_rc < 0:
        _pcc_debug_bad_incref(o, tag_dbg)
        return
    if new_rc > 0:
        if load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) != 0:
            pcc_diagnostics_runtime_log_event_code(3, 2, new_rc, tag_dbg, o)
        return
    if tag_dbg == PY_TYPE_GEN and py_gen_finalize_from_dealloc(o) != 0:
        return
    delay_zpage_freeing_note: int = 0
    if backend == 4 and (flags & 65536) != 0:
        delay_zpage_freeing_note = 1
    delay_instance_metadata: int = 0
    if tag_dbg == PY_TYPE_INSTANCE or tag_dbg >= PY_TYPE_USER_CLASS_START:
        delay_instance_metadata = 1
    if load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) != 0:
        pcc_diagnostics_runtime_log_event_code(3, 2, new_rc, tag_dbg, o)
    pcc_refcount_forget(o)
    if load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) != 0:
        pcc_diagnostics_runtime_log_event_code(3, 3, 0, tag_dbg, o)

    # Native instances finalize while their weakrefs still identify them.
    # Their deallocator invalidates only after resurrection has been ruled
    # out. C-extension objects keep their existing generic dealloc boundary.
    if delay_instance_metadata == 0 or pcc_capi_is_cext_type_tag(tag_dbg) != 0:
        py_weakref_invalidate(o)
    if delay_zpage_freeing_note == 0 and delay_instance_metadata == 0:
        pcc_gc_note_object_freeing(o)
    if delay_instance_metadata == 0:
        py_gc_untrack(o)
    watch = stack_alloc(GC_FREE_WATCH_BYTES)
    watched: int = 0
    if delay_zpage_freeing_note != 0 and delay_instance_metadata == 0:
        pcc_gc_free_watch_begin(watch, o)
        watched = 1
    pcc_dealloc_with_trash(o, tag_dbg)
    # The delayed note for a death that did not note ``o`` (queued in the
    # trash, or its free skipped the note). A death that did note it may have
    # handed its bytes to another thread's allocation, so the address is not
    # probed then.
    if watched != 0:
        if pcc_gc_free_watch_end(watch) == 0:
            if pcc_gc_pointer_is_managed(o) != 0:
                pcc_gc_note_object_freeing(o)


def _decref_above_one_atomic(o) -> int:
    """Drop one reference while the count stays positive; 1 when done.

    A count that would reach zero is left untouched for the full release
    path (deallocation, weakrefs, resurrection). Release ordering publishes
    this thread's writes before another thread can free the object.
    """
    count: int = atomic_load_i64(o, PYOBJECTHEADER_REFCOUNT_OFFSET, "relaxed")
    trying: int = 1
    while trying != 0:
        if count <= 1:
            trying = 0
        else:
            observed: int = atomic_cas_i64(
                o, PYOBJECTHEADER_REFCOUNT_OFFSET, count, count - 1, "release", "relaxed"
            )
            if observed == count:
                return 1
            count = observed
    return 0


@c_abi_export("py_decref")
def py_decref(o) -> None:
    if ptr_is_null(o) != 0 or is_tagged_int(o) != 0:
        return
    if (
        load_i32(global_addr("pcc_gc_refcount_atomic"), 0) != 0
        and load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) == 0
    ):
        atomic_tag: int = load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET)
        if (
            (atomic_tag >= PY_TYPE_NONE and atomic_tag < PY_TYPE_CONTINUATION)
            or atomic_tag == PY_TYPE_CPY_HANDLE
            or (atomic_tag >= PY_TYPE_USER and atomic_tag < PY_TYPE_CEXT_TAG_BASE)
        ):
            if (load_i32(o, PYOBJECTHEADER_FLAGS_OFFSET) & PY_FLAG_IMMORTAL) == 0:
                if _decref_above_one_atomic(o) != 0:
                    return
    # As in py_incref; a count that would reach zero keeps the full path, so
    # deallocation, weakrefs and resurrection are untouched.
    if (
        load_i32(global_addr("pcc_gc_refcount_fast"), 0) != 0
        and load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) == 0
    ):
        fast_tag: int = load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET)
        if (
            (fast_tag >= PY_TYPE_NONE and fast_tag < PY_TYPE_CONTINUATION)
            or fast_tag == PY_TYPE_CPY_HANDLE
            or (fast_tag >= PY_TYPE_USER and fast_tag < PY_TYPE_CEXT_TAG_BASE)
        ):
            if (load_i32(o, PYOBJECTHEADER_FLAGS_OFFSET) & PY_FLAG_IMMORTAL) == 0:
                fast_rc: int = load_i64(o, PYOBJECTHEADER_REFCOUNT_OFFSET)
                if fast_rc > 1:
                    store_i64(o, PYOBJECTHEADER_REFCOUNT_OFFSET, fast_rc - 1)
                    return
    # Standalone GC0 operations need no prepare record on the nonterminal
    # path. Keep provenance/type checks and share terminal finish with plans.
    if _gc_backend_fast() != 0:
        prepared = stack_alloc(56)
        _py_decref_prepare(o, prepared)
        _py_decref_finish(prepared)
        return
    if _refcount_provenance_probe_enabled() != 0 and not _ptr_can_have_header(o):
        _note_unmanaged_refcount_op()
        return
    tag: int = load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    if tag < PY_TYPE_NONE or (tag > PY_TYPE_CPY_HANDLE and tag < PY_TYPE_USER and tag != PY_TYPE_ELLIPSIS and tag != PY_TYPE_NOTIMPLEMENTED) or (tag >= (PY_TYPE_CEXT_TAG_BASE) and pcc_capi_is_cext_type_tag(tag) == 0):
        return
    flags: int = load_i32(o, PYOBJECTHEADER_FLAGS_OFFSET)
    if (tag == PY_TYPE_CONTINUATION or tag == PY_TYPE_VIRTUAL_THREAD or tag == PY_TYPE_VTHREAD_CHANNEL) and (flags & PY_FLAG_GC_TRACKED) == 0 and pcc_gc_object_is_known(o) == 0:
        return
    if (flags & PY_FLAG_IMMORTAL) != 0:
        return
    if load_i64(o, PYOBJECTHEADER_REFCOUNT_OFFSET) <= 0:
        prepared = stack_alloc(56)
        _py_decref_prepare(o, prepared)
        _py_decref_finish(prepared)
        return
    new_rc: int = pcc_refcount_decref(o)
    if new_rc > 0:
        if load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) != 0:
            pcc_diagnostics_runtime_log_event_code(3, 2, new_rc, tag, o)
        return
    if new_rc == 0:
        atomic_rmw_i32("or", o, PYOBJECTHEADER_FLAGS_OFFSET, 524288, "release")
    prepared = stack_alloc(56)
    _py_refcount_prepared_reset(prepared, o)
    store_i64(prepared, 8, tag)
    store_i64(prepared, 16, flags)
    store_i64(prepared, 24, 0)
    store_i64(prepared, 32, new_rc)
    store_i64(prepared, 40, 1)
    _py_decref_finish(prepared)


# Verification is a diagnostic control, set before exercising a workload.
# The ordinary APIs always validate; only compiler-proven object lanes use it.
# -1 means "not yet resolved": the first proven refcount reads
# PCC_GC_KNOWN_REF_CHECKS once and stores 0 or 1.
define_global_i32("pcc_gc_known_ref_checks", -1)


@c_abi_export("pcc_gc_set_known_ref_checks")
def pcc_gc_set_known_ref_checks(enabled: int) -> None:
    atomic_store_i32(global_addr("pcc_gc_known_ref_checks"), 0, 1 if enabled != 0 else 0, "release")


def _known_ref_checks() -> int:
    """Mirror of py_obj.c's pcc_known_ref_checks.

    PCC_GC_KNOWN_REF_CHECKS=1 sends every compiler-proven refcount through
    the checked path, so an audit (PCC_GC_REFCOUNT_PROVENANCE_PROBE=2/3) also
    sees the operations the frontend believed needed no provenance.
    """
    checks: int = atomic_load_i32(global_addr("pcc_gc_known_ref_checks"), 0, "acquire")
    if checks >= 0:
        return checks
    checks = 0
    if ptr_is_null(pcc_platform_getenv(cstr("PCC_GC_KNOWN_REF_CHECKS"))) == 0:
        checks = 1
    atomic_store_i32(global_addr("pcc_gc_known_ref_checks"), 0, checks, "release")
    return checks


@c_abi_export("pcc_gc_retain_known")
def pcc_gc_retain_known(o):
    """NEW ref for a proven initialized object, immortal, tagged value or NULL.

    Verification and GC1-4 use the original checked path. GC0 shares the
    refcount primitive; it needs no transaction record for a normal increment.
    """
    if ptr_is_null(o) != 0 or is_tagged_int(o) != 0:
        return o
    if _gc_backend_fast() != 0 or _known_ref_checks() != 0:
        return pcc_gc_retain(o)
    flags: int = load_i32(o, PYOBJECTHEADER_FLAGS_OFFSET)
    if (flags & PY_FLAG_IMMORTAL) != 0:
        return o
    if load_i64(o, PYOBJECTHEADER_REFCOUNT_OFFSET) < 0:
        return o
    new_rc: int = pcc_refcount_incref(o)
    if load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) != 0:
        pcc_diagnostics_runtime_log_event_code(3, 1, new_rc, load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET), o)
    return o


@c_abi_export("pcc_gc_release_known")
def pcc_gc_release_known(o) -> None:
    """Consume one proven owner; terminal release uses the existing finish."""
    if ptr_is_null(o) != 0 or is_tagged_int(o) != 0:
        return
    if _gc_backend_fast() != 0 or _known_ref_checks() != 0:
        pcc_gc_release(o)
        return
    flags: int = load_i32(o, PYOBJECTHEADER_FLAGS_OFFSET)
    if (flags & PY_FLAG_IMMORTAL) != 0:
        return
    if load_i64(o, PYOBJECTHEADER_REFCOUNT_OFFSET) <= 0:
        # Keep the established underflow diagnostic and non-mutating failure.
        pcc_gc_release(o)
        return
    new_rc: int = pcc_refcount_decref(o)
    if new_rc > 0:
        if load_i32(global_addr("pcc_diagnostics_runtime_log_fast_state"), 0) != 0:
            pcc_diagnostics_runtime_log_event_code(3, 2, new_rc, load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET), o)
        return
    if new_rc == 0:
        atomic_rmw_i32("or", o, PYOBJECTHEADER_FLAGS_OFFSET, 524288, "release")
    # Weakrefs, resurrection, reentrant deallocation and the trash stack remain
    # owned by the same finish routine as the checked API.
    prepared = stack_alloc(56)
    _py_refcount_prepared_reset(prepared, o)
    store_i64(prepared, 8, load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET))
    store_i64(prepared, 16, flags)
    store_i64(prepared, 24, 0)
    store_i64(prepared, 32, new_rc)
    store_i64(prepared, 40, 1)
    _py_decref_finish(prepared)
