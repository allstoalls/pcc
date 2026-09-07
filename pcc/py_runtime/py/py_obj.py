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
from pcc.py_runtime.py.py_abi_constants import (
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
    PY_TYPE_COMPLEX,
    PY_TYPE_CONTINUATION,
    PY_TYPE_CPY_HANDLE,
    PY_TYPE_EXC,
    PY_TYPE_FLOAT,
    PY_TYPE_FUNC,
    PY_TYPE_GEN,
    PY_TYPE_INT,
    PY_TYPE_ITER,
    PY_TYPE_MEMORYVIEW,
    PY_TYPE_STATICMETHOD,
    PY_TYPE_TASK,
    PY_TYPE_COROUTINE,
    PY_TYPE_INSTANCE,
    PY_TYPE_LIST,
    PY_TYPE_CLASS,
    PY_TYPE_CLASSMETHOD,
    PY_TYPE_NONE,
    PY_TYPE_DICT,
    PY_TYPE_SET,
    PY_TYPE_PROPERTY,
    PY_TYPE_STR,
    PY_TYPE_TUPLE,
    PY_TYPE_WEAKREF,
    PY_TYPE_USER,
    PY_TYPE_USER_CLASS_START,
    PY_TYPE_VIRTUAL_THREAD,
    PY_TYPE_VTHREAD_CHANNEL,
)
from pcc.unsafe import (
    cstr,
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
pcc_gc_note_load = extern("pcc_gc_note_load", (), c_void)
pcc_gc_note_relocation_read = extern(
    "pcc_gc_note_relocation_read",
    (c_ptr,),
    c_ptr,
)
pcc_gc_note_store = extern("pcc_gc_note_store", (), c_void)
pcc_gc_note_safepoint = extern("pcc_gc_note_safepoint", (), c_void)
pcc_gc_note_pin = extern("pcc_gc_note_pin", (c_int32,), c_void)
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
pcc_resume_world = extern("pcc_resume_world", (), c_int64)
pcc_refcount_incref = extern("pcc_refcount_incref", (c_ptr,), c_int64)
pcc_refcount_decref = extern("pcc_refcount_decref", (c_ptr,), c_int64)
pcc_refcount_forget = extern("pcc_refcount_forget", (c_ptr,), c_void)
pcc_runtime_log_event_code = extern(
    "pcc_runtime_log_event_code", (c_int32, c_int32, c_int64, c_int64, c_ptr), c_void
)
pcc_debug_note_alloc_size = extern(
    "pcc_debug_note_alloc_size", (c_ptr, c_int64), c_void
)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)


def _gc_backend_fast() -> int:
    if load_i32(global_addr("pcc_gc_config_initialized"), 0) == 0:
        return pcc_gc_backend()
    return load_i32(global_addr("pcc_gc_backend_selected"), 0)


pcc_gc_pointer_is_managed = extern(
    "pcc_gc_pointer_is_managed", (c_ptr,), c_int64
)


def _ptr_can_have_header(o) -> bool:
    return pcc_gc_pointer_is_managed(o) != 0


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
    if load_i32(global_addr("pcc_runtime_log_fast_state"), 0) != 0:
        pcc_runtime_log_event_code(1, 1, size, type_tag, null())
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
    if load_i32(global_addr("pcc_runtime_log_fast_state"), 0) != 0:
        pcc_runtime_log_event_code(1, 2, size, type_tag, obj)
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
            if resolved != o:
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
    if tag != PY_TYPE_INSTANCE and (tag < PY_TYPE_USER_CLASS_START or tag > 500):
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
    if load_i32(global_addr("pcc_runtime_log_fast_state"), 0) != 0:
        pcc_runtime_log_event_code(3, 1, new_rc, tag, o)


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
        if load_i32(global_addr("pcc_runtime_log_fast_state"), 0) != 0:
            pcc_runtime_log_event_code(2, 3, backend, 0, owner)
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
    if load_i32(global_addr("pcc_runtime_log_fast_state"), 0) != 0:
        pcc_runtime_log_event_code(2, 3, backend, 0, owner)
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
    if load_i32(global_addr("pcc_runtime_log_fast_state"), 0) != 0:
        pcc_runtime_log_event_code(2, 3, backend, 0, owner)


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
    if backend == 1 or backend == 2 or backend == 3 or backend == 4:
        pcc_gc_note_slot_write_barrier(
            owner, slot, load_ptr(plan, 0)
        )
    old = load_ptr(slot, 0)
    store_ptr(slot, 0, load_ptr(plan, 0))
    if ptr_is_null(old) == 0 and is_tagged_int(old) == 0:
        if backend == 4 and pcc_gc_object_is_known_no_lock(old) == 0:
            old = null()
    _py_decref_prepare(old, ptr_add(plan, 56))
    store_i32(plan, 124, 3)
    return 1


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
        and load_i32(global_addr("pcc_runtime_log_fast_state"), 0) != 0
    ):
        pcc_runtime_log_event_code(
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
    backend: int = _gc_backend_fast()
    if backend == 0:
        if load_i32(global_addr("pcc_runtime_log_fast_state"), 0) != 0:
            pcc_runtime_log_event_code(2, 3, backend, 0, null())
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
        if load_i32(global_addr("pcc_runtime_log_fast_state"), 0) != 0:
            pcc_runtime_log_event_code(2, 3, backend, 0, null())
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


@c_abi_export("pcc_gc_try_store_ptr_take")
def pcc_gc_try_store_ptr_take(owner, slot, value) -> int:
    if ptr_is_null(slot) != 0 or _gc_backend_fast() != 0:
        return 0
    if load_i32(global_addr("pcc_runtime_log_fast_state"), 0) != 0:
        pcc_runtime_log_event_code(2, 3, 0, 0, owner)
    old = load_ptr(slot, 0)
    store_ptr(slot, 0, value)
    # Same-value stores still consume the incoming additional owner.
    if is_tagged_int(old) == 0 and ptr_is_null(old) == 0:
        py_decref(old)
    return 1


@c_abi_export("pcc_gc_frame_enter")
def pcc_gc_frame_enter(frame_map, slots) -> None:
    pcc_gc_note_frame_enter(frame_map, slots)
    return


@c_abi_export("pcc_gc_frame_leave")
def pcc_gc_frame_leave(slots) -> None:
    pcc_gc_note_frame_leave(slots)
    return


@c_abi_export("pcc_gc_frame_enter_lifo")
def pcc_gc_frame_enter_lifo(frame_map, slots) -> None:
    pcc_gc_note_frame_enter_lifo(frame_map, slots)
    return


@c_abi_export("pcc_gc_frame_leave_lifo")
def pcc_gc_frame_leave_lifo(slots) -> None:
    pcc_gc_note_frame_leave_lifo(slots)
    return


@c_abi_export("pcc_gc_safepoint")
def pcc_gc_safepoint() -> None:
    pcc_gc_note_safepoint()
    pcc_thread_safepoint()
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
    if load_i32(global_addr("pcc_runtime_log_fast_state"), 0) != 0:
        pcc_runtime_log_event_code(2, 1, reason, backend, null())
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
    if load_i32(global_addr("pcc_runtime_log_fast_state"), 0) != 0:
        pcc_runtime_log_event_code(2, 2, collected, backend, null())
    return collected


@c_abi_export("pcc_gc_pin")
def pcc_gc_pin(o) -> None:
    if ptr_is_null(o) != 0:
        return
    if is_tagged_int(o) != 0:
        return
    flags: int = load_i32(o, PYOBJECTHEADER_FLAGS_OFFSET)
    store_i32(o, PYOBJECTHEADER_FLAGS_OFFSET, flags | PY_FLAG_GC_PINNED)
    pcc_gc_note_pin(1)
    return


@c_abi_export("pcc_gc_unpin")
def pcc_gc_unpin(o) -> None:
    if ptr_is_null(o) != 0:
        return
    if is_tagged_int(o) != 0:
        return
    flags: int = load_i32(o, PYOBJECTHEADER_FLAGS_OFFSET)
    store_i32(o, PYOBJECTHEADER_FLAGS_OFFSET, flags & ~PY_FLAG_GC_PINNED)
    pcc_gc_note_pin(-1)
    return


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


def _py_refcount_prepared_reset(prepared, o) -> None:
    store_ptr(prepared, 0, o)
    store_i64(prepared, 8, -1)
    store_i64(prepared, 16, 0)
    store_i64(prepared, 24, -1)
    store_i64(prepared, 32, 0)
    store_i64(prepared, 40, 0)
    store_i64(prepared, 48, 0)


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
    if not _ptr_can_have_header(o):
        return
    tag: int = load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    if (
        tag < PY_TYPE_NONE
        or (tag > PY_TYPE_CPY_HANDLE and tag < PY_TYPE_USER)
        or (tag > 500 and pcc_capi_is_cext_type_tag(tag) == 0)
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
    if load_i64(prepared, 40) == 0:
        return
    if load_i32(global_addr("pcc_runtime_log_fast_state"), 0) != 0:
        pcc_runtime_log_event_code(
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
    prepared = stack_alloc(56)
    _py_incref_prepare(o, prepared)
    _py_incref_finish(prepared)


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
    if not _ptr_can_have_header(o):
        return
    tag_dbg: int = load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    if (
        tag_dbg < PY_TYPE_NONE
        or (tag_dbg > PY_TYPE_CPY_HANDLE and tag_dbg < PY_TYPE_USER)
        or (tag_dbg > 500 and pcc_capi_is_cext_type_tag(tag_dbg) == 0)
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
        store_i32(o, PYOBJECTHEADER_FLAGS_OFFSET, flags | 524288)


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
        if load_i32(global_addr("pcc_runtime_log_fast_state"), 0) != 0:
            pcc_runtime_log_event_code(3, 2, new_rc, tag_dbg, o)
        return
    delay_zpage_freeing_note: int = 0
    if backend == 4 and (flags & 65536) != 0:
        delay_zpage_freeing_note = 1
    delay_instance_metadata: int = 0
    if tag_dbg == PY_TYPE_INSTANCE or tag_dbg >= PY_TYPE_USER_CLASS_START:
        delay_instance_metadata = 1
    if load_i32(global_addr("pcc_runtime_log_fast_state"), 0) != 0:
        pcc_runtime_log_event_code(3, 2, new_rc, tag_dbg, o)
    pcc_refcount_forget(o)
    if load_i32(global_addr("pcc_runtime_log_fast_state"), 0) != 0:
        pcc_runtime_log_event_code(3, 3, 0, tag_dbg, o)

    py_weakref_invalidate(o)
    if delay_zpage_freeing_note == 0 and delay_instance_metadata == 0:
        pcc_gc_note_object_freeing(o)
    if delay_instance_metadata == 0:
        py_gc_untrack(o)
    pcc_dealloc_with_trash(o, tag_dbg)
    if (
        delay_zpage_freeing_note != 0
        and delay_instance_metadata == 0
        and pcc_gc_pointer_is_managed(o) != 0
    ):
        pcc_gc_note_object_freeing(o)


@c_abi_export("py_decref")
def py_decref(o) -> None:
    prepared = stack_alloc(56)
    _py_decref_prepare(o, prepared)
    _py_decref_finish(prepared)
