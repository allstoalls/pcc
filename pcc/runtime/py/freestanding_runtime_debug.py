"""Raw runtime-debug guards authored in pcc-Python.

The checks are dormant unless ``PCC_DEBUG_RUNTIME`` is present.  Allocation
sizes are retained in a simple raw linked list: this is intentionally a debug
path, while the production-disabled fast path is one cached integer load.
Fatal corruption preserves the C oracle's fail-closed abort contract.
"""

__pcc_runtime_port__ = True

from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    abi_constant,
    cstr,
    define_global_i32,
    define_global_ptr_null,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    ptr_is_null,
    ptr_to_int,
    store_i64,
    store_i32,
    store_ptr,
    strlen,
    write,
)

define_global_i32("pcc_debug_runtime_enabled_cache", -1)
define_global_ptr_null("pcc_debug_alloc_head")

pcc_platform_getenv = extern("pcc_platform_getenv", (c_ptr,), c_ptr)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)
pcc_gc_note_relocation_read = extern(
    "pcc_gc_note_relocation_read", (c_ptr,), c_ptr
)
pcc_gc_backend = extern("pcc_gc_backend", (), c_int64)
pcc_gc_pointer_is_managed = extern(
    "pcc_gc_pointer_is_managed", (c_ptr,), c_int64
)
pcc_capi_is_type_object_value = extern(
    "pcc_capi_is_type_object_value", (c_ptr,), c_int64
)
pcc_capi_is_cext_type_tag = extern(
    "pcc_capi_is_cext_type_tag", (c_int64,), c_int64
)


@c_abi_export("pcc_debug_runtime_enabled_py")
def _enabled() -> int:
    cached = load_i32(global_addr("pcc_debug_runtime_enabled_cache"), 0)
    if cached >= 0:
        return cached
    value = 0
    if ptr_is_null(pcc_platform_getenv(cstr("PCC_DEBUG_RUNTIME"))) == 0:
        value = 1
    # The default production archive is single-threaded. A benign duplicate
    # environment query is also safe if an embedding host enters concurrently.
    store_i32(global_addr("pcc_debug_runtime_enabled_cache"), 0, value)
    return value


@c_abi_export("pcc_debug_fatal_py")
def _fatal(message) -> None:
    # Diagnostics stay allocation-free and async-safe enough for corruption
    # paths; exact pointers remain available in a debugger/core dump.
    length = strlen(message)
    write(2, message, length)
    pcc_platform_abort()


@c_abi_export("pcc_debug_find_alloc_py")
def _find_alloc(ptr) -> int:
    node = global_load_ptr("pcc_debug_alloc_head")
    while ptr_is_null(node) == 0:
        if ptr_to_int(load_ptr(node, 0)) == ptr_to_int(ptr):
            return load_i64(node, 8)
        node = load_ptr(node, 16)
    return 0


@c_abi_export("pcc_debug_valid_type_tag_py")
def _valid_type_tag(tag: int) -> int:
    if tag >= abi_constant("object.type.none") and tag <= abi_constant(
        "object.type.vthread_channel"
    ):
        return 1
    if tag == abi_constant("object.type.cpy_handle"):
        return 1
    if tag >= abi_constant("object.type.user"):
        return 1
    if pcc_capi_is_cext_type_tag(tag) != 0:
        return 1
    return 0


@c_abi_export("pcc_debug_note_alloc_size")
def pcc_debug_note_alloc_size(ptr, size: int) -> None:
    if _enabled() == 0 or ptr_is_null(ptr) or size <= 0:
        return
    node = global_load_ptr("pcc_debug_alloc_head")
    while ptr_is_null(node) == 0:
        if ptr_to_int(load_ptr(node, 0)) == ptr_to_int(ptr):
            store_i64(node, 8, size)
            return
        node = load_ptr(node, 16)
    node = malloc(24)
    if ptr_is_null(node):
        return
    store_ptr(node, 0, ptr)
    store_i64(node, 8, size)
    store_ptr(node, 16, global_load_ptr("pcc_debug_alloc_head"))
    global_store_ptr("pcc_debug_alloc_head", node)


@c_abi_export("pcc_debug_bad_incref")
def pcc_debug_bad_incref(obj, tag: int) -> None:
    _fatal(cstr("[BAD_INCREF]\n"))


@c_abi_export("pcc_null_callee_trap")
def pcc_null_callee_trap() -> None:
    # The self backend lowers IR calls through a constant null callee here.
    _fatal(cstr("[NULL_CALL]\n"))


@c_abi_export("pcc_debug_bad_dict_slot")
def pcc_debug_bad_dict_slot(
    dictionary, index: int, offset: int, obj, tag: int
) -> None:
    _fatal(cstr("[DEBUG-dict-slot]\n"))


@c_abi_export("pcc_debug_check_tuple_slot")
def pcc_debug_check_tuple_slot(tuple_obj, index: int, length: int, item) -> int:
    if _enabled() == 0 or ptr_is_null(tuple_obj):
        return 0
    bad = index < 0 or length < 0 or index >= length
    if bad == 0 and length <= 115292150460684694:
        exact = _find_alloc(tuple_obj)
        if exact > 0:
            slot_end = 24 + (index + 1) * 8
            tuple_end = 24 + length * 8
            bad = slot_end > exact or tuple_end > exact
    elif bad == 0:
        bad = True
    if bad:
        _fatal(cstr("[BAD_TUPLE_SLOT]\n"))
    return 0


@c_abi_export("pcc_debug_check_release")
def pcc_debug_check_release(name, obj) -> None:
    if _enabled() == 0 or ptr_is_null(obj):
        return
    if is_tagged_int(obj) != 0:
        return
    if pcc_gc_pointer_is_managed(obj) == 0:
        _fatal(cstr("[BAD_RELEASE:unmanaged-pointer]\n"))
    resolved = pcc_gc_note_relocation_read(obj)
    if ptr_is_null(resolved) == 0:
        obj = resolved
    if pcc_capi_is_type_object_value(obj) != 0:
        return
    refcount = load_i64(obj, 0)
    tag = load_i32(obj, 8)
    flags = load_i32(obj, 12)
    # GC3 may retain an oldified minor-arena shell with a nonpositive
    # count while forwarding completes; the C oracle explicitly permits it.
    if pcc_gc_backend() == 3 and (flags & 8) != 0 and (flags & 16) != 0:
        return
    if refcount <= 0 or _valid_type_tag(tag) == 0:
        _fatal(cstr("[BAD_RELEASE]\n"))


@c_abi_export("pcc_debug_bad_str_concat")
def pcc_debug_bad_str_concat(a, b, tag_a: int, tag_b: int) -> None:
    if _enabled() != 0:
        _fatal(cstr("[BAD_STR_CONCAT]\n"))
