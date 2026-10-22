"""pcc-Python port of py_obj_dealloc.c.

Type-specific object deallocators. The refcount dispatch in py_obj.py
still calls these symbols by name; the pcc-Python runtime archive
replaces the C object with this module while preserving the ABI.
"""

__pcc_runtime_port__ = True

from pcc.extern import extern, c_abi_export, c_int32, c_int64, c_ptr, c_void
from pcc.runtime.py.py_abi_constants import DICTENTRY_KEY_OFFSET, DICTENTRY_SIZE, DICTENTRY_VALUE_OFFSET, PYDICTOBJECT_ENTRIES_OFFSET, PYDICTOBJECT_ENTRIES_USED_OFFSET, PYDICTOBJECT_INDICES_OFFSET, PYLISTOBJECT_CAPACITY_OFFSET, PYLISTOBJECT_ITEMS_OFFSET, PYLISTOBJECT_LENGTH_OFFSET, PYOBJECTHEADER_FLAGS_OFFSET, PYOBJECTHEADER_REFCOUNT_OFFSET, PYOBJECTHEADER_TYPE_TAG_OFFSET, PYTUPLEOBJECT_ITEMS_OFFSET, PYTUPLEOBJECT_LEN_OFFSET, PY_FLAG_IMMORTAL, PY_TYPE_CONTINUATION, PY_TYPE_COROUTINE, PY_TYPE_CPY_HANDLE, PY_TYPE_CLASS, PY_TYPE_CLASSMETHOD, PY_TYPE_DICT, PY_TYPE_EXC, PY_TYPE_FILE, PY_TYPE_FLOAT, PY_TYPE_FUNC, PY_TYPE_GEN, PY_TYPE_INSTANCE, PY_TYPE_INT, PY_TYPE_ITER, PY_TYPE_LIST, PY_TYPE_MEMORYVIEW, PY_TYPE_PROPERTY, PY_TYPE_SET, PY_TYPE_STR, PY_TYPE_STATICMETHOD, PY_TYPE_TASK, PY_TYPE_THREAD, PY_TYPE_THREAD_CONDITION, PY_TYPE_THREAD_EVENT, PY_TYPE_THREAD_LOCK, PY_TYPE_THREAD_RLOCK, PY_TYPE_THREAD_SEMAPHORE, PY_TYPE_TUPLE, PY_TYPE_USER_CLASS_START, PY_TYPE_VIRTUAL_THREAD, PY_TYPE_VTHREAD_CHANNEL, PY_TYPE_WEAKREF
from pcc.unsafe import (
    atomic_load_i64,
    atomic_rmw_i64,
    call_void_ptr1,
    cstr,
    define_global_i32,
    define_global_i64,
    define_global_ptr_null,
    define_thread_local_i32,
    define_thread_local_ptr_null,
    free,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    store_i32,
    store_i64,
    store_ptr,
)


py_decref = extern("py_decref", (c_ptr,), c_void)
pcc_gc_free_object_memory = extern("pcc_gc_free_object_memory", (c_ptr,), c_void)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_backend = extern("pcc_gc_backend", (), c_int64)
_backend4_sweep_deferred_recycles = extern(
    "pcc_gc_backend4_sweep_deferred_recycles", (), c_void
)
py_dealloc_func = extern("py_dealloc_func", (c_ptr,), c_void)
py_dealloc_iter = extern("py_dealloc_iter", (c_ptr,), c_void)
py_dealloc_gen = extern("py_dealloc_gen", (c_ptr,), c_void)
py_dealloc_coroutine = extern("py_dealloc_coroutine", (c_ptr,), c_void)
py_dealloc_continuation = extern("py_dealloc_continuation", (c_ptr,), c_void)
py_dealloc_task = extern("py_dealloc_task", (c_ptr,), c_void)
py_dealloc_memoryview = extern("py_dealloc_memoryview", (c_ptr,), c_void)
py_dealloc_weakref = extern("py_dealloc_weakref", (c_ptr,), c_void)
py_file_close = extern("py_file_close", (c_ptr,), c_void)
py_dealloc_thread_lock = extern("py_dealloc_thread_lock", (c_ptr,), c_void)
py_dealloc_thread_rlock = extern("py_dealloc_thread_rlock", (c_ptr,), c_void)
py_dealloc_thread_event = extern("py_dealloc_thread_event", (c_ptr,), c_void)
py_dealloc_thread_condition = extern(
    "py_dealloc_thread_condition", (c_ptr,), c_void,
)
py_dealloc_thread_semaphore = extern(
    "py_dealloc_thread_semaphore", (c_ptr,), c_void,
)
py_dealloc_thread_thread = extern("py_dealloc_thread_thread", (c_ptr,), c_void)
py_dealloc_virtual_thread = extern("py_dealloc_virtual_thread", (c_ptr,), c_void)
py_dealloc_vthread_channel = extern(
    "py_dealloc_vthread_channel", (c_ptr,), c_void
)
py_class_dealloc = extern("py_class_dealloc", (c_ptr,), c_void)
py_instance_dealloc = extern("py_instance_dealloc", (c_ptr,), c_void)
py_descriptor_dealloc = extern("py_descriptor_dealloc", (c_ptr,), c_void)
py_dealloc_exc = extern("py_dealloc_exc", (c_ptr,), c_void)
pcc_capi_dealloc_cext_object = extern(
    "pcc_capi_dealloc_cext_object",
    (c_ptr, c_int64),
    c_int64,
)
pcc_capi_is_cext_type_tag = extern("pcc_capi_is_cext_type_tag", (c_int64,), c_int64)
pcc_debug_bad_dict_slot = extern(
    "pcc_debug_bad_dict_slot", (c_ptr, c_int64, c_int64, c_ptr, c_int64), c_void,
)
pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_ptr)
pcc_runtime_tripwire_fail = extern(
    "pcc_runtime_tripwire_fail",
    (c_ptr, c_ptr, c_int32),
    c_void,
)
pcc_gc_tripwire_defer_or_fail = extern(
    "pcc_gc_tripwire_defer_or_fail",
    (c_ptr, c_ptr, c_int32),
    c_int32,
)

# Dealloc cascades are per thread, as CPython's trashcan: each thread counts
# its own nesting and queues its own deep deaths, and only it drains them.
# One process-wide nesting depth and queue let a thread drain another's
# queue, lose depth updates and judge a cascade over while another thread
# was still inside one. The process counts below are what the backend-4
# page-recycle paths read: threads inside a cascade, and queued deaths.
define_thread_local_i32("pcc_dealloc_depth", 0)
define_thread_local_ptr_null("pcc_dealloc_trash_head")
define_thread_local_ptr_null("pcc_dealloc_trash_tail")
define_global_i64("pcc_dealloc_cascades_active", 0)
define_global_i64("pcc_dealloc_trash_pending_count", 0)
define_global_ptr_null("py_cpy_handle_release_fn")


def _dealloc_should_defer(tag: int) -> bool:
    if pcc_capi_is_cext_type_tag(tag) != 0:
        return False
    if tag == PY_TYPE_LIST:
        return True
    if tag == PY_TYPE_TUPLE:
        return True
    if tag == PY_TYPE_DICT:
        return True
    if tag == PY_TYPE_SET:
        return True
    if tag == PY_TYPE_INSTANCE:
        return True
    if tag == PY_TYPE_EXC:
        return True
    if tag == PY_TYPE_ITER:
        return True
    if tag == PY_TYPE_GEN:
        return True
    if tag == PY_TYPE_COROUTINE:
        return True
    if tag == PY_TYPE_CONTINUATION:
        return True
    if tag == PY_TYPE_TASK:
        return True
    if tag == PY_TYPE_VIRTUAL_THREAD:
        return True
    if tag == PY_TYPE_VTHREAD_CHANNEL:
        return True
    if tag == PY_TYPE_PROPERTY:
        return True
    if tag == PY_TYPE_CLASSMETHOD:
        return True
    if tag == PY_TYPE_STATICMETHOD:
        return True
    if tag >= PY_TYPE_USER_CLASS_START:
        return True
    return False


def _debug_check_dict_dealloc_slot(
    dict_obj,
    index: int,
    offset: int,
    obj,
) -> None:
    if ptr_is_null(obj) != 0:
        return
    if is_tagged_int(obj) != 0:
        return
    flags: int = load_i32(obj, PYOBJECTHEADER_FLAGS_OFFSET)
    if (flags & PY_FLAG_IMMORTAL) != 0:
        return
    if pcc_gc_backend() == 3:
        if (flags & 4096) != 0 and (flags & 256) != 0:
            if load_i64(obj, PYOBJECTHEADER_REFCOUNT_OFFSET) <= 0:
                return
    if load_i64(obj, PYOBJECTHEADER_REFCOUNT_OFFSET) <= 0:
        pcc_debug_bad_dict_slot(dict_obj, index, offset, obj, load_i32(obj, PYOBJECTHEADER_TYPE_TAG_OFFSET))


def _dealloc_dispatch(o, tag: int) -> None:
    if tag == PY_TYPE_INT:
        py_dealloc_int(o)
        return
    if tag == PY_TYPE_FLOAT:
        py_dealloc_float(o)
        return
    if tag == PY_TYPE_STR:
        py_dealloc_str(o)
        return
    if tag == PY_TYPE_LIST:
        py_dealloc_list(o)
        return
    if tag == PY_TYPE_TUPLE:
        py_dealloc_tuple(o)
        return
    if tag == PY_TYPE_DICT:
        py_dealloc_dict(o)
        return
    if tag == PY_TYPE_SET:
        py_dealloc_set(o)
        return
    if tag == PY_TYPE_FUNC:
        py_dealloc_func(o)
        return
    if tag == PY_TYPE_ITER:
        py_dealloc_iter(o)
        return
    if tag == PY_TYPE_GEN:
        py_dealloc_gen(o)
        return
    if tag == PY_TYPE_COROUTINE:
        py_dealloc_coroutine(o)
        return
    if tag == PY_TYPE_CONTINUATION:
        py_dealloc_continuation(o)
        return
    if tag == PY_TYPE_TASK:
        py_dealloc_task(o)
        return
    if tag == PY_TYPE_VIRTUAL_THREAD:
        py_dealloc_virtual_thread(o)
        return
    if tag == PY_TYPE_VTHREAD_CHANNEL:
        py_dealloc_vthread_channel(o)
        return
    if tag == PY_TYPE_MEMORYVIEW:
        py_dealloc_memoryview(o)
        return
    if tag == PY_TYPE_WEAKREF:
        py_dealloc_weakref(o)
        return
    if tag == PY_TYPE_FILE:
        py_dealloc_file(o)
        return
    if tag == PY_TYPE_THREAD_LOCK:
        py_dealloc_thread_lock(o)
        return
    if tag == PY_TYPE_THREAD_RLOCK:
        py_dealloc_thread_rlock(o)
        return
    if tag == PY_TYPE_THREAD_EVENT:
        py_dealloc_thread_event(o)
        return
    if tag == PY_TYPE_THREAD_CONDITION:
        py_dealloc_thread_condition(o)
        return
    if tag == PY_TYPE_THREAD_SEMAPHORE:
        py_dealloc_thread_semaphore(o)
        return
    if tag == PY_TYPE_THREAD:
        py_dealloc_thread_thread(o)
        return
    if tag == PY_TYPE_CLASS:
        py_class_dealloc(o)
        return
    if tag == PY_TYPE_INSTANCE:
        py_instance_dealloc(o)
        return
    if tag == PY_TYPE_EXC:
        py_dealloc_exc(o)
        return
    if tag == PY_TYPE_CPY_HANDLE:
        py_dealloc_cpy_handle(o)
        return
    if (
        tag == PY_TYPE_PROPERTY
        or tag == PY_TYPE_CLASSMETHOD
        or tag == PY_TYPE_STATICMETHOD
    ):
        py_descriptor_dealloc(o)
        return
    if pcc_capi_dealloc_cext_object(o, tag) != 0:
        return
    if tag >= PY_TYPE_USER_CLASS_START:
        py_instance_dealloc(o)
        return
    py_dealloc_generic(o)


def _cpy_handle_tripwire(message, line: int) -> None:
    pcc_runtime_tripwire_fail(
        message,
        cstr("pcc/runtime/py/py_obj_dealloc.py"),
        line,
    )


def _cpy_handle_owner_tripwire(message, line: int) -> None:
    # PCC_GC_OWNER_TRIPWIRE: the move runs inside GC3 oldification while the
    # caller owns the graph lock, where the fatal log sink must not run.  A
    # lock owner records the violation for the outer unlock to report; an
    # unlocked caller still aborts here.  Either way the caller bails before
    # the move, which would otherwise drop an owned foreign reference.
    pcc_gc_tripwire_defer_or_fail(
        message,
        cstr("pcc/runtime/py/py_obj_dealloc.py"),
        line,
    )


@c_abi_export("py_cpy_handle_set_release_fn")
def py_cpy_handle_set_release_fn(fn) -> None:
    global_store_ptr("py_cpy_handle_release_fn", fn)


@c_abi_export("py_cpy_handle_new")
def py_cpy_handle_new(cpy_ref):
    if ptr_is_null(cpy_ref):
        _cpy_handle_tripwire(
            cstr("py_cpy_handle_new: cannot own a NULL foreign reference"),
            229,
        )
        return null()
    box = pcc_gc_alloc(24, PY_TYPE_CPY_HANDLE, 0)
    if ptr_is_null(box):
        return null()
    store_ptr(box, 16, cpy_ref)
    return box


@c_abi_export("py_cpy_handle_get")
def py_cpy_handle_get(o):
    if ptr_is_null(o) or is_tagged_int(o):
        return null()
    if load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_CPY_HANDLE:
        return null()
    return load_ptr(o, 16)


@c_abi_export("pcc_cpy_handle_move_owned_ref")
def pcc_cpy_handle_move_owned_ref(from_obj, to_obj) -> None:
    valid: bool = False
    if (
        not ptr_is_null(from_obj)
        and not ptr_is_null(to_obj)
        and not ptr_eq(from_obj, to_obj)
        and not is_tagged_int(from_obj)
        and not is_tagged_int(to_obj)
    ):
        valid = (
            load_i32(from_obj, PYOBJECTHEADER_TYPE_TAG_OFFSET)
            == PY_TYPE_CPY_HANDLE
            and load_i32(to_obj, PYOBJECTHEADER_TYPE_TAG_OFFSET)
            == PY_TYPE_CPY_HANDLE
        )
    if not valid:
        _cpy_handle_owner_tripwire(
            cstr("pcc_cpy_handle_move_owned_ref: invalid native-handle move"),
            258,
        )
        return
    owned = load_ptr(from_obj, 16)
    if ptr_is_null(owned):
        _cpy_handle_owner_tripwire(
            cstr(
                "pcc_cpy_handle_move_owned_ref: source has no owned foreign reference"
            ),
            268,
        )
        return
    destination = load_ptr(to_obj, 16)
    if not ptr_is_null(destination) and not ptr_eq(destination, owned):
        _cpy_handle_owner_tripwire(
            cstr(
                "pcc_cpy_handle_move_owned_ref: destination owns a different foreign reference"
            ),
            277,
        )
        return
    store_ptr(from_obj, 16, null())
    store_ptr(to_obj, 16, owned)


@c_abi_export("py_dealloc_cpy_handle")
def py_dealloc_cpy_handle(o) -> None:
    valid: bool = False
    if not ptr_is_null(o) and not is_tagged_int(o):
        valid = load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_CPY_HANDLE
    if not valid:
        _cpy_handle_tripwire(
            cstr("py_dealloc_cpy_handle: invalid native-handle object"),
            258,
        )
        return

    foreign = load_ptr(o, 16)
    release = global_load_ptr("py_cpy_handle_release_fn")
    if not ptr_is_null(foreign) and ptr_is_null(release):
        _cpy_handle_tripwire(
            cstr("py_dealloc_cpy_handle: owned foreign reference has no release hook"),
            268,
        )
        return
    if not ptr_is_null(foreign):
        call_void_ptr1(release, foreign)
    store_ptr(o, 16, null())
    pcc_gc_free_object_memory(o)


def _trash_enqueue(o, tag: int) -> bool:
    node = malloc(24)
    if ptr_is_null(node) != 0:
        return False
    store_ptr(node, 0, o)
    store_i64(node, 8, tag)
    store_ptr(node, 16, null())
    tail = global_load_ptr("pcc_dealloc_trash_tail")
    if ptr_is_null(tail) == 0:
        store_ptr(tail, 16, node)
    else:
        global_store_ptr("pcc_dealloc_trash_head", node)
    global_store_ptr("pcc_dealloc_trash_tail", node)
    atomic_rmw_i64("add", global_addr("pcc_dealloc_trash_pending_count"), 0, 1, "acq_rel")
    return True


def _trash_drain() -> None:
    while True:
        node = global_load_ptr("pcc_dealloc_trash_head")
        if ptr_is_null(node) != 0:
            return
        nxt = load_ptr(node, 16)
        global_store_ptr("pcc_dealloc_trash_head", nxt)
        if ptr_is_null(nxt) != 0:
            global_store_ptr("pcc_dealloc_trash_tail", null())
        obj = load_ptr(node, 0)
        tag: int = load_i64(node, 8)
        free(node)
        _dealloc_dispatch(obj, tag)
        # Pending until its fields are torn down: see pcc_dealloc_trash_pending.
        atomic_rmw_i64("sub", global_addr("pcc_dealloc_trash_pending_count"), 0, 1, "acq_rel")


@c_abi_export("pcc_dealloc_cascade_active")
def pcc_dealloc_cascade_active() -> int:
    # Read by the backend-4 zpage recycle paths: while any thread is inside a
    # dealloc cascade, a count-0 page may still have objects sitting in that
    # thread's trash queue (their zpage accounting was already decremented
    # in note_object_freeing), so page recycles defer (flag at page+104) and
    # complete in pcc_gc_backend4_sweep_deferred_recycles once the last
    # cascade has drained.
    if atomic_load_i64(global_addr("pcc_dealloc_cascades_active"), 0, "acquire") > 0:
        return 1
    return 0


@c_abi_export("pcc_dealloc_trash_pending")
def pcc_dealloc_trash_pending() -> int:
    """Deaths queued by any thread whose fields are not torn down yet.

    Their freeing notes may already have run, so a zpage free path must not
    rewind page bytes those objects still occupy while this is nonzero.
    """
    return atomic_load_i64(global_addr("pcc_dealloc_trash_pending_count"), 0, "acquire")


@c_abi_export("pcc_dealloc_with_trash")
def pcc_dealloc_with_trash(o, tag: int) -> None:
    depth_slot = global_addr("pcc_dealloc_depth")
    depth: int = load_i32(depth_slot, 0)
    # Shallow-recursion allowance, every backend (CPython trashcan precedent,
    # level 50): cascades up to this depth dealloc inline, depth-first, which
    # is also CPython's __del__ order; only deeper chains go through the
    # queue, which keeps the C stack bounded.  Queueing every nested death
    # paid a 24-byte node malloc/free per contained object (GC4 already
    # regressed Stage2 past its watchdog that way).  The backend-4 recycle-UAF
    # protection is independent: page recycles defer on
    # pcc_dealloc_cascade_active() (depth > 0), not on this threshold.
    defer_depth: int = 48
    if depth > defer_depth:
        if _dealloc_should_defer(tag):
            # zpage-resident objects (header flag 0x10000) defer like
            # everything else: excluding them turned every deep zpage
            # dealloc cascade into direct recursion and overflowed the
            # stack under PCC_GC_BACKEND=4 (100k __del__-chain segfault,
            # gc4-trashcan-del-chain-dealloc-recursion-overflow.md). The
            # recycle UAF the old exclusion guarded against is closed at
            # the source instead: while pcc_dealloc_cascade_active(),
            # backend-4 page recycles defer and are completed by the
            # post-drain sweep below, so a queued object's span cannot be
            # recycled/reset under it.
            if _trash_enqueue(o, tag):
                return
    if depth == 0:
        atomic_rmw_i64("add", global_addr("pcc_dealloc_cascades_active"), 0, 1, "acq_rel")
    store_i32(depth_slot, 0, depth + 1)
    _dealloc_dispatch(o, tag)
    depth_after: int = load_i32(depth_slot, 0)
    if depth_after == 1:
        _trash_drain()
    store_i32(depth_slot, 0, depth_after - 1)
    if depth_after == 1:
        # The last thread to leave a cascade completes the deferred page
        # recycles; the sweep rechecks under the graph lock that no cascade
        # began meanwhile (that thread then completes them instead).
        left: int = atomic_rmw_i64(
            "sub", global_addr("pcc_dealloc_cascades_active"), 0, 1, "acq_rel"
        ) - 1
        if left == 0 and pcc_gc_backend() == 4:
            _backend4_sweep_deferred_recycles()


@c_abi_export("py_dealloc_int")
def py_dealloc_int(o) -> None:
    pcc_gc_free_object_memory(o)


@c_abi_export("py_dealloc_float")
def py_dealloc_float(o) -> None:
    pcc_gc_free_object_memory(o)


@c_abi_export("py_dealloc_str")
def py_dealloc_str(o) -> None:
    pcc_gc_free_object_memory(o)


@c_abi_export("py_dealloc_list")
def py_dealloc_list(o) -> None:
    length: int = load_i64(o, PYLISTOBJECT_LENGTH_OFFSET)
    items = load_ptr(o, PYLISTOBJECT_ITEMS_OFFSET)
    if pcc_gc_backend() == 4:
        store_i64(o, PYLISTOBJECT_LENGTH_OFFSET, 0)
        store_i64(o, PYLISTOBJECT_CAPACITY_OFFSET, 0)
        store_ptr(o, PYLISTOBJECT_ITEMS_OFFSET, null())
    i: int = 0
    while ptr_is_null(items) == 0 and i < length:
        item = pcc_gc_load_ptr(o, ptr_add(items, i * 8))
        if ptr_is_null(item) == 0:
            py_decref(item)
        i = i + 1
    if pcc_gc_backend() != 4:
        store_i64(o, PYLISTOBJECT_LENGTH_OFFSET, 0)
        store_i64(o, PYLISTOBJECT_CAPACITY_OFFSET, 0)
        store_ptr(o, PYLISTOBJECT_ITEMS_OFFSET, null())
    free(items)
    pcc_gc_free_object_memory(o)


@c_abi_export("py_dealloc_tuple")
def py_dealloc_tuple(o) -> None:
    length: int = load_i64(o, PYTUPLEOBJECT_LEN_OFFSET)
    i: int = 0
    while i < length:
        item = pcc_gc_load_ptr(o, ptr_add(o, PYTUPLEOBJECT_ITEMS_OFFSET + i * 8))
        if ptr_is_null(item) == 0:
            py_decref(item)
        i = i + 1
    pcc_gc_free_object_memory(o)


@c_abi_export("py_dealloc_dict")
def py_dealloc_dict(o) -> None:
    entries = load_ptr(o, PYDICTOBJECT_ENTRIES_OFFSET)
    if ptr_is_null(entries) == 0:
        entries_used: int = load_i64(o, PYDICTOBJECT_ENTRIES_USED_OFFSET)
        i: int = 0
        while i < entries_used:
            off: int = i * DICTENTRY_SIZE
            key = load_ptr(entries, off + DICTENTRY_KEY_OFFSET)
            if ptr_is_null(key) == 0:
                key = pcc_gc_load_ptr(o, ptr_add(entries, off + DICTENTRY_KEY_OFFSET))
                value = pcc_gc_load_ptr(o, ptr_add(entries, off + DICTENTRY_VALUE_OFFSET))
                _debug_check_dict_dealloc_slot(o, i, 8, key)
                _debug_check_dict_dealloc_slot(o, i, 16, value)
                py_decref(key)
                if ptr_is_null(value) == 0:
                    py_decref(value)
            i = i + 1
        free(entries)
    free(load_ptr(o, PYDICTOBJECT_INDICES_OFFSET))
    pcc_gc_free_object_memory(o)


@c_abi_export("py_dealloc_set")
def py_dealloc_set(o) -> None:
    entries = load_ptr(o, 40)
    if ptr_is_null(entries) == 0:
        dummy = global_load_ptr("py_set_dummy")
        capacity: int = load_i64(o, 24)
        i: int = 0
        while i < capacity:
            key = load_ptr(entries, i * 16 + 8)
            if ptr_is_null(key) == 0:
                if ptr_eq(key, dummy) == 0:
                    key = pcc_gc_load_ptr(o, ptr_add(entries, i * 16 + 8))
                    py_decref(key)
            i = i + 1
        free(entries)
    pcc_gc_free_object_memory(o)


@c_abi_export("py_dealloc_file")
def py_dealloc_file(o) -> None:
    py_file_close(o)
    pcc_gc_free_object_memory(o)


@c_abi_export("py_dealloc_generic")
def py_dealloc_generic(o) -> None:
    pcc_gc_free_object_memory(o)
