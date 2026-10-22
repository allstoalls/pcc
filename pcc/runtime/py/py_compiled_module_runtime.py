"""pcc-Python compiled-module registry and initialization ordering."""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import (
    C_POINTER_SIZE,
    PYOBJECTHEADER_FLAGS_OFFSET,
    PY_FLAG_GC_PINNED,
)

from pcc.extern import c_abi_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    calloc,
    call_void_ptr0,
    cstr,
    define_global_ptr_null,
    define_global_i32,
    global_addr,
    stack_alloc,
    memset,
    free,
    global_load_ptr,
    global_store_ptr,
    load_i8,
    load_i32,
    load_ptr,
    malloc,
    memcpy,
    null,
    ptr_add,
    ptr_is_null,
    store_i8,
    store_i32,
    store_ptr,
    strlen,
)


py_class_new = extern(
    "py_class_new", (c_ptr, c_ptr, c_int32, c_ptr, c_int32), c_ptr
)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_current_exception = extern("py_current_exception", (), c_ptr)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_tls_exc_set = extern("py_tls_exc_set", (c_ptr,), c_void)
py_module_attrs_dict = extern("py_module_attrs_dict", (c_ptr, c_int64), c_ptr)
py_module_attr_set = extern("py_module_attr_set", (c_ptr, c_ptr, c_ptr), c_int64)
py_sys_modules_import_cached = extern("py_sys_modules_import_cached", (c_ptr,), c_ptr)
py_sys_modules_publish = extern("py_sys_modules_publish", (c_ptr, c_ptr), c_int64)
py_sys_modules_rollback = extern("py_sys_modules_rollback", (c_ptr,), c_void)
py_sys_modules_finish_import = extern("py_sys_modules_finish_import", (c_ptr, c_ptr), c_ptr)
py_sys_modules_owner_adopt = extern("py_sys_modules_owner_adopt", (c_ptr, c_ptr, c_int64), c_void)
py_sys_modules_owner_drop = extern("py_sys_modules_owner_drop", (c_ptr, c_ptr, c_int64), c_void)
py_sys_modules_owner_finish = extern("py_sys_modules_owner_finish", (c_ptr, c_ptr), c_ptr)
pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
py_instance_new = extern("py_instance_new", (c_ptr,), c_ptr)
pcc_gc_store_ptr = extern("pcc_gc_store_ptr", (c_ptr, c_ptr, c_ptr), c_void)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_instance_setattr = extern("py_instance_setattr", (c_ptr, c_ptr, c_ptr), c_int64)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)


# Shared with py_sys_modules_owner_*: each published pointer owns one ref.
_MI_TEMP = 0
_MI_CREATOR = 1
_MI_AUX = 2
_MI_ERROR = 3
_MI_OUTPUT = 4
_MI_CACHE = 5
_MI_COUNT = 6
define_global_i32("pcc_compiled_module_frame_map", _MI_COUNT)
define_global_ptr_null("pcc_runtime_module_class_cache")
define_global_ptr_null("pcc_compiled_modules")
define_global_ptr_null("pcc_compiled_module_inits")

# Both registries used to be plain singly-linked lists walked with a string
# compare per node.  With the pcc closure's 500+ modules that made every
# import and every init lookup O(modules) strcmps, and it was the single
# hottest leaf in a `pcc1 -> pcc2` build.  These bucket arrays turn the same
# lookups into one hash plus (typically) one compare.  The linear `next`
# chains are kept exactly as they were so registration order and any external
# expectations are unchanged; the buckets are a pure index over them.
define_global_ptr_null("pcc_compiled_modules_index")
define_global_ptr_null("pcc_compiled_module_inits_index")


def _cstr_equal(left, right) -> bool:
    if ptr_is_null(left) or ptr_is_null(right):
        return ptr_is_null(left) and ptr_is_null(right)
    i: int = 0
    while True:
        a: int = load_i8(left, i) & 255
        b: int = load_i8(right, i) & 255
        if a != b:
            return False
        if a == 0:
            return True
        i = i + 1
    return False


def _duplicate_cstr(value):
    size: int = strlen(value) + 1
    copy = malloc(size)
    if ptr_is_null(copy):
        return null()
    memcpy(copy, value, size)
    return copy


def _raise_no_memory() -> None:
    py_raise_owned(py_exc_new(10, cstr("out of memory")))


@c_abi_export("pcc_runtime_module_class")
def pcc_runtime_module_class():
    cls = global_load_ptr("pcc_runtime_module_class_cache")
    if not ptr_is_null(cls):
        return cls
    cls = py_class_new(cstr("module"), null(), 0, null(), 0)
    if not ptr_is_null(cls):
        pcc_gc_pin(cls)
        global_store_ptr("pcc_runtime_module_class_cache", cls)
    return cls


def _cstr_hash_bucket(text) -> int:
    """djb2 over a NUL-terminated name, masked to 512 buckets.

    512 keeps the pcc closure's ~500 modules at roughly one node per bucket.
    """
    value: int = 5381
    index: int = 0
    byte: int = load_i8(text, 0)
    while byte != 0:
        value = ((value * 33) + byte) & 4294967295
        index = index + 1
        byte = load_i8(text, index)
    return value & 511


def _lookup_init_node(name):
    index = global_load_ptr("pcc_compiled_module_inits_index")
    if ptr_is_null(index):
        return null()
    node = load_ptr(ptr_add(index, _cstr_hash_bucket(name) * 8), 0)
    while not ptr_is_null(node):
        if _cstr_equal(load_ptr(node, 0), name):
            return node
        node = load_ptr(node, 32)
    return null()


@c_abi_export("py_compiled_module_register_init")
def py_compiled_module_register_init(name, init_fn) -> int:
    if ptr_is_null(name) or load_i8(name, 0) == 0 or ptr_is_null(init_fn):
        return -1
    existing = _lookup_init_node(name)
    if not ptr_is_null(existing):
        store_ptr(existing, 8, init_fn)
        return 0

    index = global_load_ptr("pcc_compiled_module_inits_index")
    if ptr_is_null(index):
        index = calloc(512, 8)
        if ptr_is_null(index):
            return -1
        global_store_ptr("pcc_compiled_module_inits_index", index)

    node = malloc(40)
    if ptr_is_null(node):
        return -1
    name_copy = _duplicate_cstr(name)
    if ptr_is_null(name_copy):
        free(node)
        return -1
    store_ptr(node, 0, name_copy)
    store_ptr(node, 8, init_fn)
    store_i32(node, 16, 0)
    store_ptr(node, 24, global_load_ptr("pcc_compiled_module_inits"))
    global_store_ptr("pcc_compiled_module_inits", node)
    bucket_slot = ptr_add(index, _cstr_hash_bucket(name_copy) * 8)
    store_ptr(node, 32, load_ptr(bucket_slot, 0))
    store_ptr(bucket_slot, 0, node)
    return 0


def _run_compiled_module_init(name) -> int:
    node = _lookup_init_node(name)
    if ptr_is_null(node):
        return 0
    if load_i32(node, 16) != 0:
        return 0
    store_i32(node, 16, 1)
    call_void_ptr0(load_ptr(node, 8))
    if py_err_occurred() != 0:
        # Generated globals and their initializer guards are per compiled
        # module, not per instance. Retrying partial execution would reuse
        # stale state. Preserve a tombstone until per-instance globals exist.
        store_i32(node, 16, -1)
        return -1
    store_i32(node, 16, 2)
    return 0


def _compiled_module_has_init(name) -> bool:
    if ptr_is_null(name) or load_i8(name, 0) == 0:
        return False
    return not ptr_is_null(_lookup_init_node(name))


def _compiled_module_ensure_parents_into(module_name, slots, tokens) -> None:
    if ptr_is_null(module_name):
        return
    index: int = 0
    while load_i8(module_name, index) != 0:
        if load_i8(module_name, index) == 46:
            parent = malloc(index + 1)
            if ptr_is_null(parent):
                _raise_no_memory()
                return
            memcpy(parent, module_name, index)
            store_i8(parent, index, 0)
            store_ptr(slots, _MI_OUTPUT * C_POINTER_SIZE, py_compiled_module_import_by_name(parent))
            py_sys_modules_owner_adopt(slots, tokens, _MI_OUTPUT)
            free(parent)
            if py_err_occurred():
                return
            py_sys_modules_owner_drop(slots, tokens, _MI_OUTPUT)
        index = index + 1


@c_abi_export("py_compiled_module_ensure_parent_packages")
def py_compiled_module_ensure_parent_packages(module_name) -> int:
    slots = stack_alloc(_MI_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_MI_COUNT * C_POINTER_SIZE)
    memset(slots, _MI_TEMP * C_POINTER_SIZE, 48)
    memset(tokens, 255, _MI_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_compiled_module_frame_map"), slots)
    _compiled_module_ensure_parents_into(module_name, slots, tokens)
    py_sys_modules_owner_finish(slots, tokens)
    if py_err_occurred():
        return -1
    return 0


def _lookup_module_node(name):
    index = global_load_ptr("pcc_compiled_modules_index")
    if ptr_is_null(index):
        return null()
    node = load_ptr(ptr_add(index, _cstr_hash_bucket(name) * 8), 0)
    while not ptr_is_null(node):
        if _cstr_equal(load_ptr(node, 0), name):
            return node
        node = load_ptr(node, 24)
    return null()


def _discard_module_node(name, node) -> None:
    # Only the initializing import owns removal. Circular imports have returned
    # their own references, which remain valid after this cache owner is gone.
    index = global_load_ptr("pcc_compiled_modules_index")
    slot = ptr_add(index, _cstr_hash_bucket(name) * 8)
    current = load_ptr(slot, 0)
    while not ptr_is_null(current):
        if _cstr_equal(load_ptr(current, 0), name):
            store_ptr(slot, 0, load_ptr(current, 24))
            break
        slot = ptr_add(current, 24)
        current = load_ptr(slot, 0)
    current = global_load_ptr("pcc_compiled_modules")
    previous = null()
    while not ptr_is_null(current):
        if _cstr_equal(load_ptr(current, 0), name):
            if ptr_is_null(previous):
                global_store_ptr("pcc_compiled_modules", load_ptr(current, 16))
            else:
                store_ptr(previous, 16, load_ptr(current, 16))
            break
        previous = current
        current = load_ptr(current, 16)
    free(load_ptr(node, 0))
    free(node)
    # The caller's registered creator owns cleanup. Raw registry nodes never
    # own a managed reference, and rollback preserves the initializer error.
    py_sys_modules_rollback(name)


def _publish_compiled_module_parent(name, module) -> int:
    split: int = -1
    index: int = 0
    while load_i8(name, index) != 0:
        if load_i8(name, index) == 46:
            split = index
        index = index + 1
    if split < 0:
        return 0
    parent = malloc(split + 1)
    if ptr_is_null(parent):
        _raise_no_memory()
        return -1
    memcpy(parent, name, split)
    store_i8(parent, split, 0)
    # The parent's live namespace is also its module object's dynamic dict.
    # Publish after successful initialization, never from the cached/circular
    # return path, so later user assignments to parent.child are preserved.
    rc: int = py_module_attr_set(parent, ptr_add(name, split + 1), module)
    free(parent)
    if rc != 0 or py_err_occurred() != 0:
        return -1
    return 0


def _create_compiled_module_node(name, slots, tokens):
    # Publish each NEW result before the lease helper can park. Borrowed
    # namespace/class values become explicit references in the same frame.
    store_ptr(slots, _MI_TEMP * C_POINTER_SIZE, py_module_attrs_dict(name, 1))
    py_incref(load_ptr(slots, _MI_TEMP * C_POINTER_SIZE))
    py_sys_modules_owner_adopt(slots, tokens, _MI_TEMP)
    if ptr_is_null(load_ptr(slots, _MI_TEMP * C_POINTER_SIZE)) or py_err_occurred():
        return null()
    store_ptr(slots, _MI_AUX * C_POINTER_SIZE, pcc_runtime_module_class())
    py_incref(load_ptr(slots, _MI_AUX * C_POINTER_SIZE))
    py_sys_modules_owner_adopt(slots, tokens, _MI_AUX)
    if ptr_is_null(load_ptr(slots, _MI_AUX * C_POINTER_SIZE)) or py_err_occurred():
        return null()
    store_ptr(slots, _MI_CREATOR * C_POINTER_SIZE, py_instance_new(load_ptr(slots, _MI_AUX * C_POINTER_SIZE)))
    py_sys_modules_owner_adopt(slots, tokens, _MI_CREATOR)
    if ptr_is_null(load_ptr(slots, _MI_CREATOR * C_POINTER_SIZE)) or py_err_occurred():
        return null()
    pcc_gc_store_ptr(load_ptr(slots, _MI_CREATOR * C_POINTER_SIZE), ptr_add(load_ptr(slots, _MI_CREATOR * C_POINTER_SIZE), 24), load_ptr(slots, _MI_TEMP * C_POINTER_SIZE))
    py_sys_modules_owner_drop(slots, tokens, _MI_AUX)
    store_ptr(slots, _MI_AUX * C_POINTER_SIZE, py_str_new(name, strlen(name)))
    py_sys_modules_owner_adopt(slots, tokens, _MI_AUX)
    if ptr_is_null(load_ptr(slots, _MI_AUX * C_POINTER_SIZE)) or py_err_occurred():
        return null()
    if py_instance_setattr(load_ptr(slots, _MI_CREATOR * C_POINTER_SIZE), cstr("__name__"), load_ptr(slots, _MI_AUX * C_POINTER_SIZE)) != 0:
        return null()

    index = global_load_ptr("pcc_compiled_modules_index")
    if ptr_is_null(index):
        index = calloc(512, 8)
        if ptr_is_null(index):
            return null()
        global_store_ptr("pcc_compiled_modules_index", index)
    node = malloc(32)
    if ptr_is_null(node):
        return null()
    name_copy = _duplicate_cstr(name)
    if ptr_is_null(name_copy):
        free(node)
        return null()
    store_ptr(node, 0, name_copy)
    store_ptr(node, 8, null())
    store_ptr(node, 16, global_load_ptr("pcc_compiled_modules"))
    global_store_ptr("pcc_compiled_modules", node)
    bucket_slot = ptr_add(index, _cstr_hash_bucket(name_copy) * 8)
    store_ptr(node, 24, load_ptr(bucket_slot, 0))
    store_ptr(bucket_slot, 0, node)
    if py_sys_modules_publish(name, load_ptr(slots, _MI_CREATOR * C_POINTER_SIZE)) != 0:
        _discard_module_node(name, node)
        return null()
    return node


def _compiled_module_import_into(name, slots, tokens) -> None:
    if ptr_is_null(name) or load_i8(name, 0) == 0:
        return
    store_ptr(slots, _MI_OUTPUT * C_POINTER_SIZE, py_sys_modules_import_cached(name))
    py_sys_modules_owner_adopt(slots, tokens, _MI_OUTPUT)
    if not ptr_is_null(load_ptr(slots, _MI_OUTPUT * C_POINTER_SIZE)) or py_err_occurred() != 0:
        return

    # py_module_attrs_dict is create-on-write. Unknown names must not become
    # successful empty modules merely because an attribute table can be made.
    if not _compiled_module_has_init(name):
        return
    init_node = _lookup_init_node(name)
    if load_i32(init_node, 16) != 0:
        py_raise_owned(py_exc_new(7, cstr(
            "compiled module reinitialization requires per-instance globals after sys.modules deletion"
        )))
        return
    if py_compiled_module_ensure_parent_packages(name) != 0:
        return

    # A package initializer can import this child itself. Reuse that exact
    # object, including its completed publication, rather than initializing or
    # manufacturing another module when the outer import resumes.
    store_ptr(slots, _MI_OUTPUT * C_POINTER_SIZE, py_sys_modules_import_cached(name))
    py_sys_modules_owner_adopt(slots, tokens, _MI_OUTPUT)
    if not ptr_is_null(load_ptr(slots, _MI_OUTPUT * C_POINTER_SIZE)) or py_err_occurred() != 0:
        return
    # Parent initialization can import and then delete this child. Recheck
    # the execution owner after that callback before creating any new object.
    init_node = _lookup_init_node(name)
    if load_i32(init_node, 16) != 0:
        py_raise_owned(py_exc_new(7, cstr(
            "compiled module reinitialization requires per-instance globals after sys.modules deletion"
        )))
        return
    node = _create_compiled_module_node(name, slots, tokens)
    if ptr_is_null(node):
        return
    if _run_compiled_module_init(name) != 0:
        _discard_module_node(name, node)
        return
    # Completion consumes its own reference. This frame retains the creator
    # lease until live-entry publication and every callback have completed.
    py_incref(load_ptr(slots, _MI_CREATOR * C_POINTER_SIZE))
    store_ptr(slots, _MI_OUTPUT * C_POINTER_SIZE, py_sys_modules_finish_import(name, load_ptr(slots, _MI_CREATOR * C_POINTER_SIZE)))
    py_sys_modules_owner_adopt(slots, tokens, _MI_OUTPUT)


@c_abi_export("py_compiled_module_import_by_name")
def py_compiled_module_import_by_name(name):
    slots = stack_alloc(_MI_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_MI_COUNT * C_POINTER_SIZE)
    memset(slots, _MI_TEMP * C_POINTER_SIZE, 48)
    memset(tokens, 255, _MI_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_compiled_module_frame_map"), slots)
    _compiled_module_import_into(name, slots, tokens)
    return py_sys_modules_owner_finish(slots, tokens)
