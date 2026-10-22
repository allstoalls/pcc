"""Freestanding registry for GC-owned external device resources.

External handles deliberately remain outside the managed object graph.  This
module owns their retain/fence/release state with raw memory and explicit
atomics, and invokes foreign release callbacks only after detaching the node
from the registry.  ``src/pcc_gc_external_resource.c`` is retained solely as a
host-C differential oracle.
"""

from pcc import i64
from pcc.extern import c_abi_export, c_ptr
from pcc.unsafe import (
    atomic_load_i64,
    atomic_rmw_i32,
    atomic_rmw_i64,
    atomic_store_i32,
    atomic_store_i64,
    call_i64_i64_ptr,
    call_i64_ptr1,
    call_void_ptr1,
    calloc,
    cstr,
    define_global_i32,
    define_global_i64,
    define_global_ptr_null,
    dynamic_library_close,
    dynamic_library_open,
    dynamic_library_symbol,
    free,
    function_addr,
    gc_backend_current,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    int_to_ptr,
    load_i32,
    load_i64,
    load_i8,
    load_ptr,
    malloc,
    memcpy,
    null,
    ptr_is_null,
    store_i32,
    store_i64,
    store_ptr,
    strlen,
    thread_safepoint,
)

__pcc_freestanding__ = True


# PccGcExternalResourceNode raw layout (80 bytes):
#   0 id, 8 native handle, 16 backend, 24 kind:i32, 28 state:i32,
#   32 retain count, 40 fence complete:i32, 48 release fn,
#   56 release context, 64 context-free fn, 72 next.
define_global_ptr_null("pcc_py_gc_external_head")
define_global_i32("pcc_py_gc_external_lock", 0)
define_global_i64("pcc_py_gc_external_next_id", 1)
define_global_i64("pcc_py_gc_external_active", 0)
define_global_i64("pcc_py_gc_external_pending", 0)
define_global_i64("pcc_py_gc_external_ready", 0)
define_global_i64("pcc_py_gc_external_released", 0)
define_global_i64("pcc_py_gc_external_release_failures", 0)
define_global_i64("pcc_py_gc_external_last_release_error", 0)


@c_abi_export("pcc_freestanding_gc_external_lock_acquire")
def pcc_freestanding_gc_external_lock_acquire() -> None:
    while atomic_rmw_i32(
        "xchg", global_addr("pcc_py_gc_external_lock"), 0, 1, "acquire"
    ) != 0:
        thread_safepoint()


@c_abi_export("pcc_freestanding_gc_external_lock_release")
def pcc_freestanding_gc_external_lock_release() -> None:
    atomic_store_i32(
        global_addr("pcc_py_gc_external_lock"), 0, 0, "release"
    )


@c_abi_export("pcc_freestanding_gc_external_find_locked")
def pcc_freestanding_gc_external_find_locked(resource_id: i64) -> c_ptr:
    node = global_load_ptr("pcc_py_gc_external_head")
    while ptr_is_null(node) == 0:
        if load_i64(node, 0) == resource_id:
            return node
        node = load_ptr(node, 72)
    return null()


@c_abi_export("pcc_gc_external_resource_register")
def pcc_gc_external_resource_register(
    kind: i64,
    native_handle: i64,
    release_fn: c_ptr,
    release_context: c_ptr,
    context_free_fn: c_ptr,
) -> i64:
    if (
        (kind != 1 and kind != 2)
        or native_handle == 0
        or ptr_is_null(release_fn) != 0
    ):
        return 0
    backend: i64 = gc_backend_current()
    if backend < 0 or backend > 4:
        return 0

    node = calloc(1, 80)
    if ptr_is_null(node) != 0:
        return 0
    store_i64(node, 8, native_handle)
    store_i64(node, 16, backend)
    store_i32(node, 24, kind)
    store_i32(node, 28, 1)
    store_i64(node, 32, 1)
    store_ptr(node, 48, release_fn)
    store_ptr(node, 56, release_context)
    store_ptr(node, 64, context_free_fn)

    pcc_freestanding_gc_external_lock_acquire()
    resource_id: i64 = load_i64(
        global_addr("pcc_py_gc_external_next_id"), 0
    )
    next_id: i64 = resource_id + 1
    if resource_id == 0:
        resource_id = next_id
        next_id = next_id + 1
    store_i64(global_addr("pcc_py_gc_external_next_id"), 0, next_id)
    store_i64(node, 0, resource_id)
    store_ptr(node, 72, global_load_ptr("pcc_py_gc_external_head"))
    global_store_ptr("pcc_py_gc_external_head", node)
    atomic_rmw_i64(
        "add", global_addr("pcc_py_gc_external_active"), 0, 1, "relaxed"
    )
    pcc_freestanding_gc_external_lock_release()
    return resource_id


@c_abi_export("pcc_gc_external_resource_retain")
def pcc_gc_external_resource_retain(resource_id: i64) -> i64:
    result: i64 = -1
    pcc_freestanding_gc_external_lock_acquire()
    node = pcc_freestanding_gc_external_find_locked(resource_id)
    if ptr_is_null(node) == 0:
        retain_count: i64 = load_i64(node, 32)
        if load_i32(node, 28) == 1 and retain_count > 0:
            retain_count = retain_count + 1
            store_i64(node, 32, retain_count)
            result = retain_count
    pcc_freestanding_gc_external_lock_release()
    return result


@c_abi_export("pcc_gc_external_resource_release_after_fence")
def pcc_gc_external_resource_release_after_fence(resource_id: i64) -> i64:
    result: i64 = -1
    pcc_freestanding_gc_external_lock_acquire()
    node = pcc_freestanding_gc_external_find_locked(resource_id)
    if ptr_is_null(node) == 0:
        retain_count: i64 = load_i64(node, 32)
        if load_i32(node, 28) == 1 and retain_count > 0:
            retain_count = retain_count - 1
            store_i64(node, 32, retain_count)
            result = retain_count
            if retain_count == 0:
                store_i32(node, 28, 2)
                atomic_rmw_i64(
                    "add",
                    global_addr("pcc_py_gc_external_pending"),
                    0,
                    1,
                    "relaxed",
                )
                if load_i32(node, 40) != 0:
                    atomic_rmw_i64(
                        "add",
                        global_addr("pcc_py_gc_external_ready"),
                        0,
                        1,
                        "release",
                    )
    pcc_freestanding_gc_external_lock_release()
    return result


@c_abi_export("pcc_gc_external_resource_mark_fence_complete")
def pcc_gc_external_resource_mark_fence_complete(resource_id: i64) -> i64:
    result: i64 = -1
    pcc_freestanding_gc_external_lock_acquire()
    node = pcc_freestanding_gc_external_find_locked(resource_id)
    if ptr_is_null(node) == 0:
        result: i64 = 0
        if load_i32(node, 40) == 0:
            store_i32(node, 40, 1)
            if load_i32(node, 28) == 2:
                atomic_rmw_i64(
                    "add",
                    global_addr("pcc_py_gc_external_ready"),
                    0,
                    1,
                    "release",
                )
    pcc_freestanding_gc_external_lock_release()
    return result


@c_abi_export("pcc_gc_external_resource_poll")
def pcc_gc_external_resource_poll() -> i64:
    if (
        atomic_load_i64(
            global_addr("pcc_py_gc_external_ready"), 0, "acquire"
        )
        == 0
    ):
        return 0

    processed: i64 = 0
    while True:
        pcc_freestanding_gc_external_lock_acquire()
        previous = null()
        node = global_load_ptr("pcc_py_gc_external_head")
        while ptr_is_null(node) == 0:
            if load_i32(node, 28) == 2 and load_i32(node, 40) != 0:
                break
            previous = node
            node = load_ptr(node, 72)
        if ptr_is_null(node) != 0:
            pcc_freestanding_gc_external_lock_release()
            break

        next_node = load_ptr(node, 72)
        if ptr_is_null(previous) != 0:
            global_store_ptr("pcc_py_gc_external_head", next_node)
        else:
            store_ptr(previous, 72, next_node)
        store_ptr(node, 72, null())
        atomic_rmw_i64(
            "sub", global_addr("pcc_py_gc_external_active"), 0, 1, "relaxed"
        )
        atomic_rmw_i64(
            "sub", global_addr("pcc_py_gc_external_pending"), 0, 1, "relaxed"
        )
        atomic_rmw_i64(
            "sub", global_addr("pcc_py_gc_external_ready"), 0, 1, "release"
        )
        pcc_freestanding_gc_external_lock_release()

        release_rc: i64 = call_i64_i64_ptr(
            load_ptr(node, 48), load_i64(node, 8), load_ptr(node, 56)
        )
        context_free_fn = load_ptr(node, 64)
        if ptr_is_null(context_free_fn) == 0:
            call_void_ptr1(context_free_fn, load_ptr(node, 56))
        if release_rc != 0:
            atomic_rmw_i64(
                "add",
                global_addr("pcc_py_gc_external_release_failures"),
                0,
                1,
                "relaxed",
            )
            atomic_store_i64(
                global_addr("pcc_py_gc_external_last_release_error"),
                0,
                release_rc,
                "relaxed",
            )
        atomic_rmw_i64(
            "add", global_addr("pcc_py_gc_external_released"), 0, 1, "relaxed"
        )
        processed = processed + 1
        free(node)
    return processed


@c_abi_export("pcc_gc_external_resource_backend")
def pcc_gc_external_resource_backend(resource_id: i64) -> i64:
    result: i64 = -1
    pcc_freestanding_gc_external_lock_acquire()
    node = pcc_freestanding_gc_external_find_locked(resource_id)
    if ptr_is_null(node) == 0:
        result = load_i64(node, 16)
    pcc_freestanding_gc_external_lock_release()
    return result


@c_abi_export("pcc_gc_external_resource_active_count")
def pcc_gc_external_resource_active_count() -> i64:
    return atomic_load_i64(
        global_addr("pcc_py_gc_external_active"), 0, "relaxed"
    )


@c_abi_export("pcc_gc_external_resource_pending_count")
def pcc_gc_external_resource_pending_count() -> i64:
    return atomic_load_i64(
        global_addr("pcc_py_gc_external_pending"), 0, "relaxed"
    )


@c_abi_export("pcc_gc_external_resource_release_count")
def pcc_gc_external_resource_release_count() -> i64:
    return atomic_load_i64(
        global_addr("pcc_py_gc_external_released"), 0, "relaxed"
    )


@c_abi_export("pcc_gc_external_resource_release_failure_count")
def pcc_gc_external_resource_release_failure_count() -> i64:
    return atomic_load_i64(
        global_addr("pcc_py_gc_external_release_failures"), 0, "relaxed"
    )


@c_abi_export("pcc_gc_external_resource_last_release_error")
def pcc_gc_external_resource_last_release_error() -> i64:
    return atomic_load_i64(
        global_addr("pcc_py_gc_external_last_release_error"), 0, "relaxed"
    )


@c_abi_export("pcc_freestanding_gc_external_metal_buffer_release")
def pcc_freestanding_gc_external_metal_buffer_release(
    native_handle: i64, opaque_context: c_ptr
) -> i64:
    if ptr_is_null(opaque_context) != 0:
        return -1
    runtime_library_path = load_ptr(opaque_context, 0)
    if ptr_is_null(runtime_library_path) != 0:
        return -1
    handle = dynamic_library_open(runtime_library_path, "darwin")
    if ptr_is_null(handle) != 0:
        return -8
    release_fn = dynamic_library_symbol(
        handle, cstr("pcc_metal_buffer_runtime_release"), "darwin"
    )
    if ptr_is_null(release_fn) != 0:
        dynamic_library_close(handle, "darwin")
        return -9
    result: i64 = call_i64_ptr1(release_fn, int_to_ptr(native_handle))
    dynamic_library_close(handle, "darwin")
    return result


@c_abi_export("pcc_freestanding_gc_external_metal_context_free")
def pcc_freestanding_gc_external_metal_context_free(
    opaque_context: c_ptr,
) -> None:
    if ptr_is_null(opaque_context) != 0:
        return
    free(load_ptr(opaque_context, 0))
    free(opaque_context)


@c_abi_export("pcc_gc_external_metal_buffer_register")
def pcc_gc_external_metal_buffer_register(
    runtime_library_path: c_ptr, native_handle: i64
) -> i64:
    if (
        ptr_is_null(runtime_library_path) != 0
        or load_i8(runtime_library_path, 0) == 0
        or native_handle == 0
    ):
        return 0
    path_nbytes: i64 = strlen(runtime_library_path) + 1
    context = calloc(1, 8)
    if ptr_is_null(context) != 0:
        return 0
    path_copy = malloc(path_nbytes)
    if ptr_is_null(path_copy) != 0:
        free(context)
        return 0
    memcpy(path_copy, runtime_library_path, path_nbytes)
    store_ptr(context, 0, path_copy)

    resource_id: i64 = pcc_gc_external_resource_register(
        1,
        native_handle,
        function_addr("pcc_freestanding_gc_external_metal_buffer_release"),
        context,
        function_addr("pcc_freestanding_gc_external_metal_context_free"),
    )
    if resource_id == 0:
        pcc_freestanding_gc_external_metal_context_free(context)
    return resource_id
