"""Known-object, root gray, root resolve, and gray-counter primitives."""

from pcc import i64
from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.runtime.py.py_abi_constants import (
    PYOBJECTHEADER_FLAGS_OFFSET,
    PY_FLAG_GC_PINNED,
    PY_TYPE_BOOL,
    PY_TYPE_BYTEARRAY,
    PY_TYPE_BYTES,
    PY_TYPE_COMPLEX,
    PY_TYPE_CPY_HANDLE,
    PY_TYPE_FLOAT,
    PY_TYPE_INT,
    PY_TYPE_NONE,
    PY_TYPE_STR,
)
from pcc.unsafe import (
    atomic_cas_i32,
    atomic_load_i32,
    atomic_rmw_i32,
    atomic_store_i32,
    define_thread_local_ptr_null,
    define_global_i64,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    null,
    ptr_eq,
    ptr_is_null,
    store_i32,
    store_i64,
    store_ptr,
)


__pcc_freestanding__ = True


pcc_gc_forwarding_index_find = extern("pcc_gc_forwarding_index_find", (c_ptr,), c_ptr)
pcc_gc_object_index_find = extern("pcc_gc_object_index_find", (c_ptr,), c_ptr)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_incref = extern("py_incref", (c_ptr,), c_void)
pcc_gc_config_ensure = extern("pcc_gc_config_ensure", (), c_int64)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
pcc_gc_granule_is_object_start = extern("pcc_gc_granule_is_object_start", (c_ptr,), c_int64)
pcc_gc_managed_pointer_index_contains = extern("pcc_gc_managed_pointer_index_contains", (c_ptr,), c_int64)

# Includes counted nodes and validated no-node mode guards. Access only under
# the graph lock; backend transitions refuse while a foreign pointer is live.
define_global_i64("pcc_gc_foreign_lease_active", 0)


@c_abi_export("pcc_gc_gray_count_load_acquire")
def pcc_gc_gray_count_load_acquire() -> i64:
    return atomic_load_i32(global_addr("pcc_gc_gray_count"), 0, "acquire")


@c_abi_export("pcc_gc_gray_count_store_release")
def pcc_gc_gray_count_store_release(value: i64) -> None:
    atomic_store_i32(global_addr("pcc_gc_gray_count"), 0, value, "release")


@c_abi_export("pcc_gc_gray_count_increment_acq_rel")
def pcc_gc_gray_count_increment_acq_rel() -> None:
    atomic_rmw_i32("add", global_addr("pcc_gc_gray_count"), 0, 1, "acq_rel")


@c_abi_export("pcc_gc_gray_count_decrement_acq_rel")
def pcc_gc_gray_count_decrement_acq_rel() -> None:
    slot = global_addr("pcc_gc_gray_count")
    old: i64 = atomic_load_i32(slot, 0, "acquire")
    while old > 0:
        desired: i64 = old - 1
        observed: i64 = atomic_cas_i32(
            slot,
            0,
            old,
            desired,
            "acq_rel",
            "acquire",
        )
        if observed == old:
            return
        old = observed


@c_abi_export("pcc_gc_set_mark_color")
def pcc_gc_set_mark_color(obj, color: i64) -> None:
    # The graph lock serializes color transitions and gray-count accounting,
    # not all header writers: terminal decref and pin/unpin publish their
    # bits atomically without that lock. Retry against the current word so
    # coloring cannot erase those updates. One CAS also avoids a transient
    # colorless word, which GC1/2 refcount operations treat as uninitialized.
    old: i64 = atomic_load_i32(obj, PYOBJECTHEADER_FLAGS_OFFSET, "acquire")
    while True:
        desired: i64 = (old & ~56) | color
        observed: i64 = atomic_cas_i32(
            obj, PYOBJECTHEADER_FLAGS_OFFSET, old, desired, "acq_rel", "acquire"
        )
        if observed == old:
            return
        old = observed


@c_abi_export("pcc_gc_object_is_known_no_lock")
def pcc_gc_object_is_known_no_lock(obj) -> i64:
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return 0
    node = pcc_gc_object_index_find(obj)
    if ptr_is_null(node) != 0:
        return 0
    if load_i64(node, 32) != 0:
        return 0
    flags: i64 = load_i32(obj, 12)
    return 0 if (flags & 524288) != 0 else 1


@c_abi_export("pcc_gc_mark_root_gray_if_known")
def pcc_gc_mark_root_gray_if_known(obj) -> None:
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return
    forwarding = pcc_gc_forwarding_index_find(obj)
    if ptr_is_null(forwarding) == 0:
        resolved = load_ptr(forwarding, 8)
        if ptr_is_null(resolved) == 0 and ptr_eq(resolved, obj) == 0:
            obj = resolved
    if pcc_gc_object_is_known_no_lock(obj) == 0:
        return
    flags: i64 = load_i32(obj, 12)
    if (flags & 16) == 0:
        pcc_gc_gray_count_increment_acq_rel()
    pcc_gc_set_mark_color(obj, 16)


@c_abi_export("pcc_gc_resolve_root_slot_unlocked")
def pcc_gc_resolve_root_slot_unlocked(slot_base, slot_offset: i64):
    value = load_ptr(slot_base, slot_offset)
    if ptr_is_null(value) != 0 or is_tagged_int(value) != 0:
        return value
    if pcc_gc_object_is_known_no_lock(value) == 0:
        forwarding_unknown = pcc_gc_forwarding_index_find(value)
        if ptr_is_null(forwarding_unknown) == 0:
            resolved_unknown = load_ptr(forwarding_unknown, 8)
            if (
                ptr_is_null(resolved_unknown) == 0
                and ptr_eq(resolved_unknown, value) == 0
            ):
                if load_i32(global_addr("pcc_gc_backend_selected"), 0) == 4:
                    store_ptr(slot_base, slot_offset, resolved_unknown)
                    return resolved_unknown
                py_incref(resolved_unknown)
                store_ptr(slot_base, slot_offset, resolved_unknown)
                py_decref(value)
                return resolved_unknown
        return value
    flags: i64 = load_i32(value, 12)
    if (flags & 2048) == 0:
        return value
    forwarding = pcc_gc_forwarding_index_find(value)
    if ptr_is_null(forwarding) != 0:
        store_i32(value, 12, flags & ~2048)
        return value
    resolved = load_ptr(forwarding, 8)
    if ptr_is_null(resolved) != 0 or ptr_eq(resolved, value) != 0:
        store_i32(value, 12, flags & ~2048)
        return value
    if load_i32(global_addr("pcc_gc_backend_selected"), 0) == 4:
        store_ptr(slot_base, slot_offset, resolved)
        return resolved
    py_incref(resolved)
    store_ptr(slot_base, slot_offset, resolved)
    py_decref(value)
    return resolved


# Foreign-address leases are separate from legacy Boolean pin/unpin. The slot
# is already traced and owns its object. Lock acquisition can park, so reload
# only AFTER it succeeds. Return codes never alter Python exception state.
@c_abi_export("pcc_gc_foreign_lease_acquire")
def pcc_gc_foreign_lease_acquire(slot) -> i64:
    if ptr_is_null(slot) != 0:
        return -1
    # GC0/1/2 never move an object, and their sweeps take liveness from
    # owners and pins, never from a counted lease (status 2 only counted a
    # global for backend transitions). The no-op token is the whole
    # contract there: no lock and no shared write, so threads do not
    # serialize on every rooted runtime call. Transitions into a moving
    # collector are refused while another thread runs (pcc_gc_set_backend).
    if load_i32(global_addr("pcc_gc_config_initialized"), 0) != 0:
        selected: i64 = load_i32(global_addr("pcc_gc_backend_selected"), 0)
        if selected >= 0 and selected <= 2:
            return 0
    # py_incref's gate (backend 0, no provenance probe, one thread): nothing
    # moves, the forwarding and object indexes are empty, and no other thread
    # can take the transition sentinel. For an ordinary non-immortal managed
    # object the path below only adds one counted lease; immortal and
    # specially flagged objects still take it.
    if load_i32(global_addr("pcc_gc_refcount_fast"), 0) != 0:
        fast = load_ptr(slot, 0)
        if ptr_is_null(fast) != 0 or is_tagged_int(fast) != 0:
            return 0
        if (load_i32(fast, PYOBJECTHEADER_FLAGS_OFFSET) & (1 | 524288)) == 0:
            fast_active: i64 = load_i64(global_addr("pcc_gc_foreign_lease_active"), 0)
            if fast_active >= 0 and fast_active < 9223372036854775807:
                store_i64(global_addr("pcc_gc_foreign_lease_active"), 0, fast_active + 1)
                return 2
    pcc_gc_config_ensure()
    pcc_py_gc_minor_graph_lock()
    if load_i64(global_addr("pcc_gc_foreign_lease_active"), 0) < 0:
        pcc_py_gc_minor_graph_unlock()
        return -1
    obj = pcc_gc_resolve_root_slot_unlocked(slot, 0)
    status: i64 = -1
    node = null()
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        status = 0
    else:
        # ABI precondition: a compiler-proven initialized managed object.
        # Literal/immortal objects need no allocator entry and never move.
        flags: i64 = load_i32(obj, PYOBJECTHEADER_FLAGS_OFFSET)
        node = pcc_gc_object_index_find(obj)
        if (flags & 524288) == 0 and ptr_is_null(node) == 0:
            # Tracked immortals still participate in relocation. Only
            # untracked intrinsic immortals qualify for a no-op token.
            if load_i64(node, 32) == 0:
                count: i64 = load_i64(node, 80)
                if count >= 0 and count < 9223372036854775807:
                    status = 1
                else:
                    status = -2
        elif ((flags & 1) != 0 and (flags & (2048 | 524288)) == 0
              and pcc_gc_granule_is_object_start(obj) != 1
              and pcc_gc_managed_pointer_index_contains(obj) == 0):
            # Only an unregistered static immortal is intrinsically stable.
            # Heap immortals still need a mode guard: a backend transition
            # may register/move an allocator-backed nonleaf object later.
            status = 0
        elif (flags & 524288) == 0 and (pcc_gc_granule_is_object_start(obj) == 1
                  or pcc_gc_managed_pointer_index_contains(obj) != 0):
            backend: i64 = load_i32(global_addr("pcc_gc_backend_selected"), 0)
            tag: i64 = load_i32(obj, 8)
            # GC0/1/2 never move. GC3/4 no-node malloc graph leaves are
            # intentionally refcount-only; nursery/zpage owners are not.
            # A no-node immortal (the malloc'd standard streams and object
            # class roots) is stable storage outside the object graph, so
            # GC3/4 never move it either.
            if backend >= 0 and backend <= 2:
                status = 2
            elif (backend == 3 or backend == 4) and (flags & (4096 | 65536)) == 0:
                if (
                    tag == PY_TYPE_NONE
                    or tag == PY_TYPE_BOOL
                    or tag == PY_TYPE_INT
                    or tag == PY_TYPE_FLOAT
                    or tag == PY_TYPE_STR
                    or tag == PY_TYPE_COMPLEX
                    or tag == PY_TYPE_BYTES
                    or tag == PY_TYPE_BYTEARRAY
                    or tag == PY_TYPE_CPY_HANDLE
                    or (flags & 1) != 0
                ):
                    status = 2
    if status > 0:
        active: i64 = load_i64(global_addr("pcc_gc_foreign_lease_active"), 0)
        if active < 0 or active == 9223372036854775807:
            status = -2
        else:
            if status == 1:
                store_i64(node, 80, load_i64(node, 80) + 1)
            store_i64(global_addr("pcc_gc_foreign_lease_active"), 0, active + 1)
    pcc_py_gc_minor_graph_unlock()
    return status


@c_abi_export("pcc_gc_foreign_lease_release")
def pcc_gc_foreign_lease_release(slot, acquired: i64) -> i64:
    # A no-op token must never decrement a counted node registered later.
    if acquired == 0:
        return 0
    if ptr_is_null(slot) != 0 or (acquired != 1 and acquired != 2):
        return -1
    # As in acquisition, one thread on backend 0 only counts leases.
    if acquired == 2 and load_i32(global_addr("pcc_gc_refcount_fast"), 0) != 0:
        fast_active: i64 = load_i64(global_addr("pcc_gc_foreign_lease_active"), 0)
        if fast_active > 0:
            store_i64(global_addr("pcc_gc_foreign_lease_active"), 0, fast_active - 1)
            return 0
    pcc_py_gc_minor_graph_lock()
    status: i64 = -3
    active: i64 = load_i64(global_addr("pcc_gc_foreign_lease_active"), 0)
    if active > 0:
        if acquired == 2:
            store_i64(global_addr("pcc_gc_foreign_lease_active"), 0, active - 1)
            status = 0
        else:
            obj = pcc_gc_resolve_root_slot_unlocked(slot, 0)
            node = pcc_gc_object_index_find(obj)
            if ptr_is_null(node) == 0 and load_i64(node, 32) == 0:
                count: i64 = load_i64(node, 80)
                if count > 0:
                    store_i64(node, 80, count - 1)
                    store_i64(global_addr("pcc_gc_foreign_lease_active"), 0, active - 1)
                    status = 0
    pcc_py_gc_minor_graph_unlock()
    return status


@c_abi_export("pcc_gc_object_is_address_pinned")
def pcc_gc_object_is_address_pinned(obj) -> i64:
    # Collector callers hold the graph lock (or own stopped-world traversal).
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return 0
    if (load_i32(obj, PYOBJECTHEADER_FLAGS_OFFSET) & PY_FLAG_GC_PINNED) != 0:
        return 1
    node = pcc_gc_object_index_find(obj)
    if ptr_is_null(node) == 0:
        return 1 if load_i64(node, 80) != 0 else 0
    return 0


# Pin acquisition/release must not park before touching their raw object arg.
# Keep this existing header/metric protocol in the strict primitive owner.
@c_abi_export("pcc_gc_pin")
def pcc_gc_pin(o) -> None:
    if ptr_is_null(o) != 0:
        return
    if is_tagged_int(o) != 0:
        return
    # Other threads and collector workers update the same flags word: the
    # pin bit is one atomic or, never a lost read-modify-write.
    atomic_rmw_i32("or", o, PYOBJECTHEADER_FLAGS_OFFSET, PY_FLAG_GC_PINNED, "relaxed")
    # pcc_gc_note_pin(1), inline: codegen pins around most calls.
    atomic_rmw_i32("add", global_addr("pcc_gc_metric_pin"), 0, 1, "relaxed")
    return


@c_abi_export("pcc_gc_unpin")
def pcc_gc_unpin(o) -> None:
    if ptr_is_null(o) != 0:
        return
    if is_tagged_int(o) != 0:
        return
    atomic_rmw_i32("and", o, PYOBJECTHEADER_FLAGS_OFFSET, ~PY_FLAG_GC_PINNED, "relaxed")
    # pcc_gc_note_pin(-1), inline.
    atomic_rmw_i32("sub", global_addr("pcc_gc_metric_pin"), 0, 1, "relaxed")
    return




@c_abi_export("pcc_gc_take_pinned_slot")
def pcc_gc_take_pinned_slot(slot, prior_pin: i64):
    # The caller owns one pinned reference stored in this stable slot and has
    # finished every operation that can park, including frame unregistration.
    # Transfer that reference without another callback/park or refcount change.
    if ptr_is_null(slot) != 0:
        return null()
    value = load_ptr(slot, 0)
    store_ptr(slot, 0, null())
    pcc_gc_unpin(value)
    if (prior_pin & PY_FLAG_GC_PINNED) != 0:
        if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
            atomic_rmw_i32("or", value, PYOBJECTHEADER_FLAGS_OFFSET, PY_FLAG_GC_PINNED, "relaxed")
    return value


# These links refer only to live native activation records. Managed exception
# values stay in the caller's ordinary collector-visible root/frame slots.
# Freestanding entrypoints never park or acquire managed owners.
define_thread_local_ptr_null("pcc_tls_handled_exception_context")


@c_abi_export("py_handled_context_push")
def py_handled_context_push(record: c_ptr, exception_slot: c_ptr) -> None:
    store_ptr(record, 0, global_load_ptr("pcc_tls_handled_exception_context"))
    store_ptr(record, 8, exception_slot)
    global_store_ptr("pcc_tls_handled_exception_context", record)


@c_abi_export("py_handled_context_pop")
def py_handled_context_pop(record: c_ptr) -> i64:
    current = global_load_ptr("pcc_tls_handled_exception_context")
    if ptr_eq(current, record) == 0:
        # Shared exceptional cleanup may revisit an already retired scope.
        return 0
    global_store_ptr("pcc_tls_handled_exception_context", load_ptr(record, 0))
    store_ptr(record, 0, null())
    store_ptr(record, 8, null())
    return 1


@c_abi_export("py_handled_exception_slot")
def py_handled_exception_slot() -> c_ptr:
    current = global_load_ptr("pcc_tls_handled_exception_context")
    while ptr_is_null(current) == 0:
        slot = load_ptr(current, 8)
        if ptr_is_null(slot) == 0:
            if ptr_is_null(load_ptr(slot, 0)) == 0:
                return slot
        current = load_ptr(current, 0)
    return null()


@c_abi_export("py_handled_context_swap")
def py_handled_context_swap(context: c_ptr) -> c_ptr:
    previous = global_load_ptr("pcc_tls_handled_exception_context")
    global_store_ptr("pcc_tls_handled_exception_context", context)
    return previous
