"""Class namespace transactions and nonallocating commit callbacks.

The locked callbacks require the graph lock and its counted no-park lease.
The acquire/install entries establish that complete transaction themselves;
callers prepare plans and finish deferred cleanup outside it. Runtime config
is already initialized, and every class/created/output slot has the owning
root and address-lease contract documented by its entry.
"""

__pcc_freestanding__ = True

from pcc import (
    i64,
)
from pcc.extern import (
    c_abi_export,
    c_ptr,
    c_int64,
    c_void,
    extern,
)
from pcc.runtime.py.py_abi_constants import (
    C_POINTER_SIZE,
    PYCLASSMETHOD_FUNC_OFFSET,
    PYCLASSMETHOD_NAME_OFFSET,
    PYCLASSMETHOD_SIZE,
    PYCLASSOBJECT_ATTRS_OFFSET,
    PYCLASSOBJECT_DEL_METHOD_OFFSET,
    PYCLASSOBJECT_METHODS_OFFSET,
    PYCLASSOBJECT_METACLASS_OFFSET,
    PYCLASSOBJECT_N_METHODS_OFFSET,
    PYOBJECTHEADER_TYPE_TAG_OFFSET,
    PY_TYPE_CLASS,
    PY_TYPE_DICT,
)
from pcc.unsafe import (
    atomic_rmw_i32,
    cstr,
    global_addr,
    is_tagged_int,
    load_i8,
    load_i32,
    load_ptr,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    stack_alloc,
    store_i64,
    store_ptr,
)


# Raw internal notification context: it contains a registered owner-slot
# address and an immutable C-string address, never a managed object pointer.
# The name is NULL for a non-string dictionary key (or an embedded NUL).
_CLASS_NAMESPACE_CONTEXT_OWNER_SLOT: i64 = 0
_CLASS_NAMESPACE_CONTEXT_NAME: i64 = 1
_CLASS_NAMESPACE_CONTEXT_COUNT: i64 = 2


@c_abi_export("pcc_class_namespace_same_name")
def _class_namespace_same_name(left: c_ptr, right: c_ptr) -> i64:
    index: i64 = 0
    while True:
        a: i64 = load_i8(left, index)
        b: i64 = load_i8(right, index)
        if a != b:
            return 0
        if a == 0:
            return 1
        index += 1
    return 0


@c_abi_export("py_class_namespace_validate_locked")
def py_class_namespace_validate_locked(context: c_ptr, dictionary: c_ptr) -> i64:
    """Validate the exact namespace before any dictionary commit writes.

    The caller holds the graph lock. The context points to a live registered
    class owner slot; it does not contain a borrowed class address. A mismatch
    leaves the dictionary untouched so the writer can rebuild its plan.
    """
    owner_slot = load_ptr(context, _CLASS_NAMESPACE_CONTEXT_OWNER_SLOT * C_POINTER_SIZE)
    cls = load_ptr(owner_slot, 0)
    if ptr_is_null(cls) != 0:
        return 0
    if load_i32(cls, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_CLASS:
        return 0
    return ptr_eq(load_ptr(cls, PYCLASSOBJECT_ATTRS_OFFSET), dictionary)


@c_abi_export("py_class_namespace_commit_locked")
def py_class_namespace_commit_locked(context: c_ptr, dictionary: c_ptr) -> None:
    """Invalidate native aliases in the already validated dict transaction.

    Managed callables live only in the owning namespace. A namespace write
    retires every same-name raw table fallback before displaced-owner disposal.
    NULL stores need neither a managed pointer probe nor a shading barrier.
    This helper does not allocate, park, release references or invoke Python.
    """
    owner_slot = load_ptr(context, _CLASS_NAMESPACE_CONTEXT_OWNER_SLOT * C_POINTER_SIZE)
    cls = load_ptr(owner_slot, 0)
    name = load_ptr(context, _CLASS_NAMESPACE_CONTEXT_NAME * C_POINTER_SIZE)
    methods = load_ptr(cls, PYCLASSOBJECT_METHODS_OFFSET)
    count: i64 = load_i32(cls, PYCLASSOBJECT_N_METHODS_OFFSET)
    index: i64 = 0
    if ptr_is_null(name) == 0 and ptr_is_null(methods) == 0:
        while index < count:
            offset: i64 = index * PYCLASSMETHOD_SIZE
            candidate = load_ptr(methods, offset + PYCLASSMETHOD_NAME_OFFSET)
            if _class_namespace_same_name(candidate, name) != 0:
                store_ptr(methods, offset + PYCLASSMETHOD_FUNC_OFFSET, null())
            index += 1
    if ptr_is_null(name) == 0 and _class_namespace_same_name(name, cstr("__del__")) != 0:
        store_ptr(cls, PYCLASSOBJECT_DEL_METHOD_OFFSET, null())
    # Invalidate before deferred decrefs can reenter ordinary attribute lookup.
    atomic_rmw_i32("add", global_addr("py_class_attr_cache_epoch"), 0, 1, "release")


pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
pcc_gc_root_copy_lease_prepare_locked = extern(
    "pcc_gc_root_copy_lease_prepare_locked", (c_ptr, c_ptr, c_int64, c_ptr), c_int64
)
pcc_gc_store_ptr_plan_commit_locked = extern(
    "pcc_gc_store_ptr_plan_commit_locked", (c_ptr, c_ptr, c_ptr, c_ptr), c_int64
)

# Owning namespace selection and publish-if-empty installation.
# The caller registers and leases every managed input/output slot. These
# entries own the complete graph transaction so their scalar status returns
# cannot introduce managed arithmetic between lock and unlock in a caller.


@c_abi_export("pcc_class_namespace_source_locked")
def _class_namespace_source_locked(class_slot: c_ptr) -> c_ptr:
    cls = load_ptr(class_slot, 0)
    if ptr_is_null(cls) != 0 or is_tagged_int(cls) != 0:
        return null()
    if load_i32(cls, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_CLASS:
        return null()
    return ptr_add(cls, PYCLASSOBJECT_ATTRS_OFFSET)


@c_abi_export("pcc_class_namespace_copy_locked")
def _class_namespace_copy_locked(source: c_ptr, output: c_ptr, token_slot: c_ptr, plan: c_ptr, prepared_slot: c_ptr) -> i64:
    if ptr_is_null(load_ptr(source, 0)) != 0:
        return 0
    store_i64(prepared_slot, 0, 1)
    token: i64 = pcc_gc_root_copy_lease_prepare_locked(output, source, 0, plan)
    if token < 0:
        return -2
    store_i64(token_slot, 0, token)
    return 1


@c_abi_export("pcc_class_namespace_acquire_slots")
def pcc_class_namespace_acquire_slots(class_slot: c_ptr, output: c_ptr, token_slot: c_ptr, plan: c_ptr, prepared_slot: c_ptr) -> i64:
    """Select the live owning namespace slot; caller finishes a prepared plan."""
    store_i64(prepared_slot, 0, 0)
    pcc_py_gc_minor_graph_lock()
    source = _class_namespace_source_locked(class_slot)
    status: i64 = -1
    if ptr_is_null(source) == 0:
        status = _class_namespace_copy_locked(source, output, token_slot, plan, prepared_slot)
    pcc_py_gc_minor_graph_unlock()
    return status


@c_abi_export("pcc_class_namespace_install_slots")
def pcc_class_namespace_install_slots(class_slot: c_ptr, created_slot: c_ptr, output: c_ptr, token_slot: c_ptr, write_plan: c_ptr, copy_plan: c_ptr, prepared_slot: c_ptr) -> i64:
    """Publish if empty and select that namespace in one graph transaction.

    write_plan was initialized outside the lock. The candidate dictionary is
    already owned/address-leased. Losing a creation race preserves the winning
    namespace; no earlier unchecked empty observation authorizes replacement.
    """
    store_i64(prepared_slot, 0, 0)
    pcc_py_gc_minor_graph_lock()
    source = _class_namespace_source_locked(class_slot)
    status: i64 = -1
    if ptr_is_null(source) == 0:
        ready: i64 = 1
        if ptr_is_null(load_ptr(source, 0)) != 0:
            created = load_ptr(created_slot, 0)
            ready = 0
            if ptr_is_null(created) == 0 and is_tagged_int(created) == 0:
                if load_i32(created, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_DICT:
                    ready = pcc_gc_store_ptr_plan_commit_locked(
                        write_plan, load_ptr(class_slot, 0), source, created,
                    )
        if ready != 0:
            status = _class_namespace_copy_locked(source, output, token_slot, copy_plan, prepared_slot)
    pcc_py_gc_minor_graph_unlock()
    return status


_CLASS_METACLASS_STORE_PLAN_BYTES: i64 = 128

pcc_gc_store_root_plan_init = extern(
    "pcc_gc_store_root_plan_init", (c_ptr, c_int64), c_void
)
pcc_gc_store_ptr_plan_finish = extern(
    "pcc_gc_store_ptr_plan_finish", (c_ptr,), c_void
)


@c_abi_export("pcc_class_retire_metaclass")
def pcc_class_retire_metaclass(cls: c_ptr) -> None:
    """Consume the counted relation while the deallocator owns cls.

    Refcount/tracing disposal has published terminal ownership; GC0 cycle
    disposal owns the stopped world. Thus this raw receiver cannot relocate
    or be concurrently freed. Its metaclass remains in the authoritative slot
    until the graph transaction transfers that owner to the decref plan.
    Runtime configuration is already initialized by class allocation.
    """
    plan = stack_alloc(_CLASS_METACLASS_STORE_PLAN_BYTES)
    pcc_py_gc_minor_graph_lock()
    backend: i64 = load_i32(global_addr("pcc_gc_backend_selected"), 0)
    # Root-plan initialization has no logging or allocation; the graph lock
    # also owns the no-park lease across all existing prepare/commit entries.
    pcc_gc_store_root_plan_init(plan, backend)
    pcc_gc_store_ptr_plan_commit_locked(
        plan, cls, ptr_add(cls, PYCLASSOBJECT_METACLASS_OFFSET), null(),
    )
    pcc_py_gc_minor_graph_unlock()
    # The old count was consumed under lock. A terminal old value is already
    # nonrelocatable; a surviving old value needs no further pointer access.
    pcc_gc_store_ptr_plan_finish(plan)


_CLASS_DEFINITION_ABORT_PLAN_BYTES: i64 = 128


@c_abi_export("pcc_class_abort_definition_slots")
def pcc_class_abort_definition_slots(class_slot: c_ptr) -> None:
    """Retire definition owners of a fresh, unexposed builtin class.

    Only the compiler's pre-publication error edge may call this entry. Its
    class slot remains registered throughout. A custom metaclass result or a
    class already passed to a Python callback is outside this contract.
    Keep the immortal class shell, tag, bases, MRO and field layout intact:
    their wider dynamic-reclamation contract is independent of this rollback.
    Detaching the namespace releases this owner, never clears a shared dict.
    """
    plan = stack_alloc(_CLASS_DEFINITION_ABORT_PLAN_BYTES)
    pcc_py_gc_minor_graph_lock()
    backend: i64 = load_i32(global_addr("pcc_gc_backend_selected"), 0)
    pcc_gc_store_root_plan_init(plan, backend)
    source = _class_namespace_source_locked(class_slot)
    if ptr_is_null(source) == 0:
        cls = load_ptr(class_slot, 0)
        methods = load_ptr(cls, PYCLASSOBJECT_METHODS_OFFSET)
        count: i64 = load_i32(cls, PYCLASSOBJECT_N_METHODS_OFFSET)
        index: i64 = 0
        if ptr_is_null(methods) == 0:
            while index < count:
                store_ptr(methods, index * PYCLASSMETHOD_SIZE + PYCLASSMETHOD_FUNC_OFFSET, null())
                index += 1
        store_ptr(cls, PYCLASSOBJECT_DEL_METHOD_OFFSET, null())
        # Every method-table pointer is a borrowed alias. Retire aliases and
        # cached outcomes before the namespace owner can invoke finalizers.
        atomic_rmw_i32("add", global_addr("py_class_attr_cache_epoch"), 0, 1, "release")
        pcc_gc_store_ptr_plan_commit_locked(plan, cls, source, null())
    pcc_py_gc_minor_graph_unlock()
    # A terminal namespace is stable; a surviving one may have moved and the
    # existing plan finish does not dereference its old address.
    pcc_gc_store_ptr_plan_finish(plan)
