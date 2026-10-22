"""pcc-Python substrate replacement.

This module defines the stable runtime storage symbols that used to live
in py_substrate.c, plus the small C ABI helper functions retained for
older runtime call sites.  The top-level define_global_* and
define_thread_local_* calls are compile-time pcc.unsafe intrinsics: they
create storage symbols in the object file and do not depend on the
stripped synthetic main().
"""

__pcc_runtime_port__ = True

from pcc.extern import (
    c_abi_export,
    c_abi_typed_export,
    c_int32,
    c_int64,
    c_ptr,
    c_void,
    extern,
)
from pcc.runtime.py.py_abi_constants import PY_TYPE_ELLIPSIS, PY_TYPE_NOTIMPLEMENTED
from pcc.runtime.py.py_abi_constants import (
    C_POINTER_SIZE,
    PYCLASSOBJECT_BASES_OFFSET,
    PYCLASSOBJECT_FIELD_NAMES_OFFSET,
    PYCLASSOBJECT_INSTANCE_SIZE_OFFSET,
    PYCLASSOBJECT_METHODS_OFFSET,
    PYCLASSOBJECT_MRO_OFFSET,
    PYCLASSOBJECT_NAME_OFFSET,
    PYCLASSOBJECT_N_BASES_OFFSET,
    PYCLASSOBJECT_N_FIELDS_OFFSET,
    PYCLASSOBJECT_N_METHODS_OFFSET,
    PYCLASSOBJECT_N_MRO_OFFSET,
    PYCLASSOBJECT_SIZE,
    PYCLASSOBJECT_TYPE_TAG_ALLOC_OFFSET,
    PYINSTANCEOBJECT_SIZE,
    PYOBJECTHEADER_FLAGS_OFFSET,
    PYOBJECTHEADER_REFCOUNT_OFFSET,
    PYOBJECTHEADER_TYPE_TAG_OFFSET,
    PY_FLAG_GC_MALLOC_ALLOC,
    PY_FLAG_IMMORTAL,
    PY_TYPE_CLASS,
    PY_TYPE_INSTANCE,
    PY_TYPE_USER_CLASS_START,
    PY_TYPE_VALUEBOX,
    PY_TYPE_CEXT_TAG_BASE,
)
from pcc.unsafe import (
    atomic_cas_i32,
    atomic_load_i32,
    define_global_cstr,
    define_global_header,
    define_global_i8,
    define_global_i32,
    define_global_i32_array,
    define_global_null_ptr_array,
    define_global_ptr_array,
    define_global_ptr_null,
    define_global_ptr_to_global,
    define_thread_local_ptr_null,
    free,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    is_tagged_int,
    load_i8,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    memcpy,
    memmove,
    memset,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    realloc,
    store_i32,
    store_i8,
    store_i64,
    store_ptr,
    strlen,
)

access = extern("pcc_platform_access", (c_ptr, c_int64), c_int64)
getenv = extern("pcc_platform_getenv", (c_ptr,), c_ptr)
setenv = extern("pcc_platform_setenv", (c_ptr, c_ptr, c_int64), c_int64)
unsetenv = extern("pcc_platform_unsetenv", (c_ptr,), c_int64)
write = extern("pcc_platform_write", (c_int64, c_ptr, c_int64), c_int64)
pcc_gc_scheduler_root_register_handle = extern(
    "pcc_gc_scheduler_root_register_handle", (c_ptr,), c_ptr
)
pcc_gc_scheduler_root_unregister_handle = extern(
    "pcc_gc_scheduler_root_unregister_handle", (c_ptr,), c_void
)
pcc_gc_note_slot_write_barrier = extern(
    "pcc_gc_note_slot_write_barrier", (c_ptr, c_ptr, c_ptr), c_void
)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)

pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_ptr)
pcc_gc_pointer_register = extern(
    "pcc_gc_pointer_register", (c_ptr,), c_int64
)

define_global_header("py_none_storage", 1, 0, PY_FLAG_IMMORTAL)
define_global_header("py_notimplemented_storage", 1, PY_TYPE_NOTIMPLEMENTED, PY_FLAG_IMMORTAL)
define_global_header("py_ellipsis_storage", 1, PY_TYPE_ELLIPSIS, PY_FLAG_IMMORTAL)
define_global_header("py_true_storage", 1, 1, PY_FLAG_IMMORTAL)
define_global_header("py_false_storage", 1, 1, PY_FLAG_IMMORTAL)
define_global_ptr_to_global("py_None", "py_none_storage")
define_global_ptr_to_global("py_NotImplemented", "py_notimplemented_storage")
define_global_ptr_to_global("py_Ellipsis", "py_ellipsis_storage")
define_global_ptr_to_global("py_True", "py_true_storage")
define_global_ptr_to_global("py_False", "py_false_storage")

define_global_cstr("PY_EXC_NAME_0", "BaseException")
define_global_cstr("PY_EXC_NAME_1", "Exception")
define_global_cstr("PY_EXC_NAME_2", "ValueError")
define_global_cstr("PY_EXC_NAME_3", "TypeError")
define_global_cstr("PY_EXC_NAME_4", "KeyError")
define_global_cstr("PY_EXC_NAME_5", "IndexError")
define_global_cstr("PY_EXC_NAME_6", "AttributeError")
define_global_cstr("PY_EXC_NAME_7", "RuntimeError")
define_global_cstr("PY_EXC_NAME_8", "StopIteration")
define_global_cstr("PY_EXC_NAME_9", "ZeroDivisionError")
define_global_cstr("PY_EXC_NAME_10", "NameError")
define_global_cstr("PY_EXC_NAME_11", "NotImplementedError")
define_global_cstr("PY_EXC_NAME_12", "ArithmeticError")
define_global_cstr("PY_EXC_NAME_13", "LookupError")
define_global_cstr("PY_EXC_NAME_14", "OSError")
define_global_cstr("PY_EXC_NAME_15", "OverflowError")
define_global_cstr("PY_EXC_NAME_16", "AssertionError")
define_global_cstr("PY_EXC_NAME_17", "StopAsyncIteration")
define_global_cstr("PY_EXC_NAME_18", "ReferenceError")
define_global_cstr("PY_EXC_NAME_19", "MemoryError")
define_global_cstr("PY_EXC_NAME_20", "ImportError")
define_global_cstr("PY_EXC_NAME_21", "ModuleNotFoundError")
define_global_cstr("PY_EXC_NAME_22", "Warning")
define_global_cstr("PY_EXC_NAME_23", "UserWarning")
define_global_cstr("PY_EXC_NAME_24", "DeprecationWarning")
define_global_cstr("PY_EXC_NAME_25", "PendingDeprecationWarning")
define_global_cstr("PY_EXC_NAME_26", "SyntaxWarning")
define_global_cstr("PY_EXC_NAME_27", "RuntimeWarning")
define_global_cstr("PY_EXC_NAME_28", "FutureWarning")
define_global_cstr("PY_EXC_NAME_29", "ImportWarning")
define_global_cstr("PY_EXC_NAME_30", "UnicodeWarning")
define_global_cstr("PY_EXC_NAME_31", "BytesWarning")
define_global_cstr("PY_EXC_NAME_32", "EncodingWarning")
define_global_cstr("PY_EXC_NAME_33", "ResourceWarning")
define_global_cstr("PY_EXC_NAME_34", "FileNotFoundError")
define_global_cstr("PY_EXC_NAME_35", "FileExistsError")
define_global_cstr("PY_EXC_NAME_36", "PermissionError")
define_global_cstr("PY_EXC_NAME_37", "IsADirectoryError")
define_global_cstr("PY_EXC_NAME_38", "NotADirectoryError")
define_global_cstr("PY_EXC_NAME_39", "ProcessLookupError")
define_global_cstr("PY_EXC_NAME_40", "ChildProcessError")
define_global_cstr("PY_EXC_NAME_41", "TimeoutError")
define_global_cstr("PY_EXC_NAME_42", "InterruptedError")
define_global_cstr("PY_EXC_NAME_43", "BlockingIOError")
define_global_cstr("PY_EXC_NAME_44", "ConnectionError")
define_global_cstr("PY_EXC_NAME_45", "BrokenPipeError")
define_global_cstr("PY_EXC_NAME_46", "ConnectionAbortedError")
define_global_cstr("PY_EXC_NAME_47", "ConnectionRefusedError")
define_global_cstr("PY_EXC_NAME_48", "ConnectionResetError")
define_global_cstr("PY_EXC_NAME_49", "SyntaxError")
define_global_cstr("PY_EXC_NAME_50", "IndentationError")
define_global_cstr("PY_EXC_NAME_51", "TabError")
define_global_cstr("PY_EXC_NAME_52", "EOFError")
define_global_cstr("PY_EXC_NAME_53", "SystemExit")
define_global_cstr("PY_EXC_NAME_54", "KeyboardInterrupt")
define_global_cstr("PY_EXC_NAME_55", "GeneratorExit")
define_global_cstr("PY_EXC_NAME_56", "RecursionError")
define_global_cstr("PY_EXC_NAME_57", "UnicodeError")
define_global_cstr("PY_EXC_NAME_58", "UnicodeDecodeError")
define_global_cstr("PY_EXC_NAME_59", "UnicodeEncodeError")
define_global_cstr("PY_EXC_NAME_60", "UnicodeTranslateError")
define_global_cstr("PY_EXC_NAME_61", "FloatingPointError")
define_global_cstr("PY_EXC_NAME_62", "BufferError")
define_global_cstr("PY_EXC_NAME_63", "UnboundLocalError")
define_global_cstr("PY_EXC_NAME_64", "SystemError")
define_global_ptr_array(
    "PY_EXC_BUILTIN_NAMES",
    "PY_EXC_NAME_0",
    "PY_EXC_NAME_1",
    "PY_EXC_NAME_2",
    "PY_EXC_NAME_3",
    "PY_EXC_NAME_4",
    "PY_EXC_NAME_5",
    "PY_EXC_NAME_6",
    "PY_EXC_NAME_7",
    "PY_EXC_NAME_8",
    "PY_EXC_NAME_9",
    "PY_EXC_NAME_10",
    "PY_EXC_NAME_11",
    "PY_EXC_NAME_12",
    "PY_EXC_NAME_13",
    "PY_EXC_NAME_14",
    "PY_EXC_NAME_15",
    "PY_EXC_NAME_16",
    "PY_EXC_NAME_17",
    "PY_EXC_NAME_18",
    "PY_EXC_NAME_19",
    "PY_EXC_NAME_20",
    "PY_EXC_NAME_21",
    "PY_EXC_NAME_22",
    "PY_EXC_NAME_23",
    "PY_EXC_NAME_24",
    "PY_EXC_NAME_25",
    "PY_EXC_NAME_26",
    "PY_EXC_NAME_27",
    "PY_EXC_NAME_28",
    "PY_EXC_NAME_29",
    "PY_EXC_NAME_30",
    "PY_EXC_NAME_31",
    "PY_EXC_NAME_32",
    "PY_EXC_NAME_33",
    "PY_EXC_NAME_34",
    "PY_EXC_NAME_35",
    "PY_EXC_NAME_36",
    "PY_EXC_NAME_37",
    "PY_EXC_NAME_38",
    "PY_EXC_NAME_39",
    "PY_EXC_NAME_40",
    "PY_EXC_NAME_41",
    "PY_EXC_NAME_42",
    "PY_EXC_NAME_43",
    "PY_EXC_NAME_44",
    "PY_EXC_NAME_45",
    "PY_EXC_NAME_46",
    "PY_EXC_NAME_47",
    "PY_EXC_NAME_48",
    "PY_EXC_NAME_49",
    "PY_EXC_NAME_50",
    "PY_EXC_NAME_51",
    "PY_EXC_NAME_52",
    "PY_EXC_NAME_53",
    "PY_EXC_NAME_54",
    "PY_EXC_NAME_55",
    "PY_EXC_NAME_56",
    "PY_EXC_NAME_57",
    "PY_EXC_NAME_58",
    "PY_EXC_NAME_59",
    "PY_EXC_NAME_60",
    "PY_EXC_NAME_61",
    "PY_EXC_NAME_62",
    "PY_EXC_NAME_63",
    "PY_EXC_NAME_64",
)
define_global_i32_array(
    "PY_EXC_PARENT",
    -1,
    0,
    1,
    1,
    13,
    13,
    1,
    1,
    1,
    12,
    1,
    7,
    1,
    1,
    1,
    12,
    1,
    1,
    1,
    1,
    1,
    20,
    1,
    22,
    22,
    22,
    22,
    22,
    22,
    22,
    22,
    22,
    22,
    22,

    14,
    14,
    14,
    14,
    14,
    14,
    14,
    14,
    14,
    14,
    14,
    44,
    44,
    44,
    44,
    1,
    49,
    50,
    1,
    0,
    0,
    0,
    7,
    2,
    57,
    57,
    57,
    12,
    1,
    10,
    1,
)
define_global_null_ptr_array("py_exc_classes", 65)  # PY_EXC_N_BUILTIN

define_global_i8("py_set_dummy_storage", 0)
define_global_ptr_to_global("py_set_dummy", "py_set_dummy_storage")
define_global_i32("py_next_user_tag", PY_TYPE_USER_CLASS_START)
define_global_ptr_null("py_weakref_head")
define_global_ptr_null("py_object_root_cache")
define_global_i32("py_class_attr_cache_epoch", 0)
define_thread_local_ptr_null("py_tls_current_exc_storage")
define_thread_local_ptr_null("py_tls_current_exc_root_handle")


@c_abi_export("py_mem_alloc")
def py_mem_alloc(bytes: int):
    return malloc(bytes)


@c_abi_export("py_mem_free")
def py_mem_free(p) -> None:
    free(p)


@c_abi_export("py_mem_zero")
def py_mem_zero(p, bytes: int):
    if ptr_is_null(p) == 0:
        memset(p, 0, bytes)
    return p


@c_abi_export("py_mem_copy")
def py_mem_copy(dst, src, bytes: int):
    if ptr_is_null(dst) == 0 and ptr_is_null(src) == 0:
        memmove(dst, src, bytes)
    return dst


@c_abi_export("py_mem_load_i64")
def py_mem_load_i64(p, offset: int) -> int:
    return load_i64(p, offset)


@c_abi_export("py_mem_load_i32")
def py_mem_load_i32(p, offset: int) -> int:
    return load_i32(p, offset)


@c_abi_export("py_mem_load_i8")
def py_mem_load_i8(p, offset: int) -> int:
    return load_i8(p, offset)


@c_abi_export("py_mem_load_ptr")
def py_mem_load_ptr(p, offset: int):
    return load_ptr(p, offset)


@c_abi_export("py_mem_store_i64")
def py_mem_store_i64(p, offset: int, v: int) -> None:
    store_i64(p, offset, v)


@c_abi_export("py_mem_store_i32")
def py_mem_store_i32(p, offset: int, v: int) -> None:
    store_i32(p, offset, v)


@c_abi_export("py_mem_store_i8")
def py_mem_store_i8(p, offset: int, v: int) -> None:
    store_i8(p, offset, v)


@c_abi_export("py_mem_store_ptr")
def py_mem_store_ptr(p, offset: int, v) -> None:
    store_ptr(p, offset, v)


@c_abi_export("py_mem_ptr_add")
def py_mem_ptr_add(p, offset: int):
    return ptr_add(p, offset)


@c_abi_export("py_mem_ptr_is_tagged_int")
def py_mem_ptr_is_tagged_int(p) -> int:
    if is_tagged_int(p):
        return 1
    return 0


@c_abi_export("py_mem_null_ptr")
def py_mem_null_ptr():
    return null()


@c_abi_export("py_tls_exc_get")
def py_tls_exc_get():
    return global_load_ptr("py_tls_current_exc_storage")


@c_abi_export("py_tls_exc_set")
def py_tls_exc_set(exc) -> None:
    handle = global_load_ptr("py_tls_current_exc_root_handle")
    if ptr_is_null(exc) == 0:
        if ptr_is_null(handle) != 0:
            handle = pcc_gc_scheduler_root_register_handle(
                global_addr("py_tls_current_exc_storage")
            )
            if ptr_is_null(handle) != 0:
                # An active exception may outlive the current native frame.
                # Continuing without publishing its TLS slot to the common
                # root registry would make a concurrent collector unsound.
                pcc_platform_abort()
                return
            global_store_ptr("py_tls_current_exc_root_handle", handle)
        pcc_gc_note_slot_write_barrier(
            null(), global_addr("py_tls_current_exc_storage"), exc
        )
        global_store_ptr("py_tls_current_exc_storage", exc)
        return

    # Publish the empty slot before unlinking its root node so a collector can
    # never retain the value through a node that is being retired.  Clearing
    # also makes raw-pthread exit safe without a platform-specific TLS
    # destructor: normal exception teardown leaves no address into dead TLS.
    global_store_ptr("py_tls_current_exc_storage", null())
    if ptr_is_null(handle) == 0:
        pcc_gc_scheduler_root_unregister_handle(handle)
        global_store_ptr("py_tls_current_exc_root_handle", null())


@c_abi_export("py_tls_exc_swap_slot")
def py_tls_exc_swap_slot(slot) -> None:
    """Exchange two owned roots without exposing a displaced raw reference."""
    handle = global_load_ptr("py_tls_current_exc_root_handle")
    if ptr_is_null(pcc_gc_load_ptr(null(), slot)) == 0 and ptr_is_null(handle):
        handle = pcc_gc_scheduler_root_register_handle(global_addr("py_tls_current_exc_storage"))
        if ptr_is_null(handle):
            pcc_platform_abort()
            return
        global_store_ptr("py_tls_current_exc_root_handle", handle)
    pcc_py_gc_minor_graph_lock()
    incoming = pcc_gc_load_ptr(null(), slot)
    previous = pcc_gc_load_ptr(null(), global_addr("py_tls_current_exc_storage"))
    pcc_gc_note_slot_write_barrier(null(), global_addr("py_tls_current_exc_storage"), incoming)
    pcc_gc_note_slot_write_barrier(null(), slot, previous)
    global_store_ptr("py_tls_current_exc_storage", incoming)
    store_ptr(slot, 0, previous)
    empty: int = ptr_is_null(incoming)
    pcc_py_gc_minor_graph_unlock()
    if empty != 0 and ptr_is_null(handle) == 0:
        pcc_gc_scheduler_root_unregister_handle(handle)
        global_store_ptr("py_tls_current_exc_root_handle", null())


@c_abi_export("py_subs_none")
def py_subs_none():
    return global_load_ptr("py_None")


@c_abi_export("py_subs_true")
def py_subs_true():
    return global_load_ptr("py_True")


@c_abi_export("py_subs_false")
def py_subs_false():
    return global_load_ptr("py_False")


@c_abi_export("py_subs_exc_name")
def py_subs_exc_name(tag: int):
    if tag < 0 or tag >= 65:  # PY_EXC_N_BUILTIN
        return null()
    return load_ptr(global_addr("PY_EXC_BUILTIN_NAMES"), tag * 8)


@c_abi_export("py_subs_exc_parent")
def py_subs_exc_parent(tag: int) -> int:
    if tag < 0 or tag >= 65:  # PY_EXC_N_BUILTIN
        return -1
    return load_i32(global_addr("PY_EXC_PARENT"), tag * 4)


@c_abi_export("py_subs_exc_n_builtin")
def py_subs_exc_n_builtin() -> int:
    return 65  # PY_EXC_N_BUILTIN


@c_abi_export("py_subs_exc_cache_get")
def py_subs_exc_cache_get(tag: int):
    if tag < 0 or tag >= 65:  # PY_EXC_N_BUILTIN
        return null()
    return load_ptr(global_addr("py_exc_classes"), tag * 8)


@c_abi_export("py_subs_exc_cache_set")
def py_subs_exc_cache_set(tag: int, cls) -> None:
    if tag < 0 or tag >= 65:  # PY_EXC_N_BUILTIN
        return
    store_ptr(global_addr("py_exc_classes"), tag * 8, cls)


@c_abi_export("py_subs_exc_cache_slot")
def py_subs_exc_cache_slot(tag: int):
    if tag < 0 or tag >= 65:  # PY_EXC_N_BUILTIN
        return null()
    return ptr_add(global_addr("py_exc_classes"), tag * 8)


@c_abi_export("py_subs_set_dummy")
def py_subs_set_dummy():
    return global_load_ptr("py_set_dummy")


@c_abi_export("py_mem_ptr_eq")
def py_mem_ptr_eq(a, b) -> int:
    if ptr_eq(a, b):
        return 1
    return 0


@c_abi_export("py_mem_ptr_is_null")
def py_mem_ptr_is_null(p) -> int:
    if ptr_is_null(p):
        return 1
    return 0


@c_abi_export("py_subs_getenv")
def py_subs_getenv(name):
    if ptr_is_null(name):
        return null()
    return getenv(name)


@c_abi_export("py_subs_setenv")
def py_subs_setenv(name, value) -> int:
    if ptr_is_null(name) or ptr_is_null(value):
        return -1
    return setenv(name, value, 1)


@c_abi_export("py_subs_unsetenv")
def py_subs_unsetenv(name) -> int:
    if ptr_is_null(name):
        return -1
    return unsetenv(name)


@c_abi_export("py_subs_path_exists")
def py_subs_path_exists(path) -> int:
    if ptr_is_null(path):
        return 0
    if access(path, 0) == 0:
        return 1
    return 0


@c_abi_export("py_subs_cstr_len")
def py_subs_cstr_len(s) -> int:
    if ptr_is_null(s):
        return 0
    return strlen(s)


@c_abi_export("py_subs_cstr_at")
def py_subs_cstr_at(s, i: int) -> int:
    if ptr_is_null(s):
        return 0
    return load_i8(s, i)


@c_abi_export("py_subs_realloc")
def py_subs_realloc(p, bytes: int):
    return realloc(p, bytes)


@c_abi_export("py_subs_write_fd")
def py_subs_write_fd(fd: int, buf, n: int) -> int:
    if ptr_is_null(buf) or n <= 0:
        return 0
    wrote: int = write(fd, buf, n)
    if wrote > 0:
        return wrote
    return 0


@c_abi_export("py_subs_strcmp")
def py_subs_strcmp(a, b) -> int:
    if ptr_is_null(a) or ptr_is_null(b):
        return -1
    i: int = 0
    while True:
        ca: int = load_i8(a, i) & 255
        cb: int = load_i8(b, i) & 255
        if ca != cb:
            return ca - cb
        if ca == 0:
            return 0
        i = i + 1


@c_abi_typed_export("py_subs_alloc_user_tag", "i32", ())
def py_subs_alloc_user_tag() -> int:
    """Allocate one ABI-valid user tag, or -1 without advancing on exhaustion.

    The counter is shared by runtime and class constructors. It names no
    published object, so relaxed CAS provides the required unique allocation
    order without imposing unrelated object-publication ordering.
    """
    slot = global_addr("py_next_user_tag")
    while True:
        observed: int = atomic_load_i32(slot, 0, "relaxed")
        if observed < PY_TYPE_USER_CLASS_START or observed >= PY_TYPE_CEXT_TAG_BASE:
            return -1
        tag: int = observed
        if tag == PY_TYPE_VALUEBOX:
            tag = tag + 1
        if tag >= PY_TYPE_CEXT_TAG_BASE:
            return -1
        next_tag: int = tag + 1
        previous: int = atomic_cas_i32(
            slot, 0, observed, next_tag, "relaxed", "relaxed",
        )
        if previous == observed:
            return tag


@c_abi_export("py_subs_object_root")
def py_subs_object_root():
    root = global_load_ptr("py_object_root_cache")
    if ptr_is_null(root) == 0:
        return root

    mro = malloc(C_POINTER_SIZE)
    if ptr_is_null(mro):
        return null()
    # This root is cached in a raw global pointer, not a relocation-updated
    # root slot.  Give it stable storage and register exact provenance.
    r = malloc(PYCLASSOBJECT_SIZE)
    if ptr_is_null(r):
        free(mro)
        return null()
    memset(r, 0, PYCLASSOBJECT_SIZE)
    store_i64(r, PYOBJECTHEADER_REFCOUNT_OFFSET, 1)
    store_i32(r, PYOBJECTHEADER_TYPE_TAG_OFFSET, PY_TYPE_CLASS)
    store_i32(
        r,
        PYOBJECTHEADER_FLAGS_OFFSET,
        # Class flag 2 means slots-only. Apply it before the root can be
        # observed through an MRO, even before the builtin alias is fetched.
        PY_FLAG_IMMORTAL | PY_FLAG_GC_MALLOC_ALLOC | 2,
    )
    if pcc_gc_pointer_register(r) < 0:
        free(r)
        free(mro)
        return null()
    store_ptr(r, PYCLASSOBJECT_NAME_OFFSET, global_addr("PY_OBJECT_NAME"))
    store_i32(r, PYCLASSOBJECT_N_BASES_OFFSET, 0)
    store_ptr(r, PYCLASSOBJECT_BASES_OFFSET, null())
    store_i32(r, PYCLASSOBJECT_N_MRO_OFFSET, 1)

    store_ptr(mro, 0, r)
    store_ptr(r, PYCLASSOBJECT_MRO_OFFSET, mro)

    store_i32(r, PYCLASSOBJECT_N_METHODS_OFFSET, 0)
    store_ptr(r, PYCLASSOBJECT_METHODS_OFFSET, null())
    store_i32(r, PYCLASSOBJECT_N_FIELDS_OFFSET, 0)
    store_ptr(r, PYCLASSOBJECT_FIELD_NAMES_OFFSET, null())
    store_i32(
        r,
        PYCLASSOBJECT_INSTANCE_SIZE_OFFSET,
        PYINSTANCEOBJECT_SIZE + C_POINTER_SIZE,
    )
    store_i32(r, PYCLASSOBJECT_TYPE_TAG_ALLOC_OFFSET, PY_TYPE_INSTANCE)

    global_store_ptr("py_object_root_cache", r)
    return r


define_global_cstr("PY_OBJECT_NAME", "object")
