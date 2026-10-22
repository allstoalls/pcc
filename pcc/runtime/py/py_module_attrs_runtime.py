"""pcc-Python ownership of native compiled-module attribute storage."""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import (
    C_POINTER_SIZE, PYOBJECTHEADER_FLAGS_OFFSET, PY_FLAG_GC_PINNED, PY_TYPE_STR,
    PYSTROBJECT_BYTE_LEN_OFFSET, PYSTROBJECT_DATA_OFFSET,
)

from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    atomic_cas_i64,
    atomic_load_i64,
    cstr,
    define_global_i32,
    define_global_i64,
    define_global_ptr_null,
    free,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    is_tagged_int,
    int_to_ptr,
    load_i8,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    memcpy,
    memset,
    null,
    ptr_is_null,
    ptr_add,
    ptr_eq,
    ptr_to_int,
    stack_alloc,
    store_i32,
    store_i8,
    store_i64,
    store_ptr,
    strlen,
)


py_dict_new = extern("py_dict_new", (), c_ptr)
py_dict_set = extern("py_dict_set", (c_ptr, c_ptr, c_ptr), c_void)
py_dict_get = extern("py_dict_get", (c_ptr, c_ptr), c_ptr)
py_dict_del = extern("py_dict_del", (c_ptr, c_ptr), c_int64)
py_dict_len = extern("py_dict_len", (c_ptr,), c_int64)
py_dict_entries_used = extern("py_dict_entries_used", (c_ptr,), c_int64)
py_dict_entry_key_at = extern("py_dict_entry_key_at", (c_ptr, c_int64), c_ptr)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_obj_getattr = extern("py_obj_getattr", (c_ptr, c_ptr), c_ptr)
py_obj_len = extern("py_obj_len", (c_ptr,), c_int64)
py_obj_getitem_i64 = extern("py_obj_getitem_i64", (c_ptr, c_int64), c_ptr)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_tls_exc_swap_slot = extern("py_tls_exc_swap_slot", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
pcc_gc_note_write_barrier = extern("pcc_gc_note_write_barrier", (c_ptr, c_ptr), c_void)
pcc_gc_take_pinned_slot = extern("pcc_gc_take_pinned_slot", (c_ptr, c_int64), c_ptr)
pcc_gc_scheduler_root_register_handle = extern("pcc_gc_scheduler_root_register_handle", (c_ptr,), c_ptr)
pcc_gc_root_copy_lease = extern("pcc_gc_root_copy_lease", (c_ptr, c_ptr), c_int64)
pcc_gc_foreign_lease_acquire = extern("pcc_gc_foreign_lease_acquire", (c_ptr,), c_int64)
pcc_gc_foreign_lease_release = extern("pcc_gc_foreign_lease_release", (c_ptr, c_int64), c_int64)
pcc_gc_note_slot_write_barrier = extern("pcc_gc_note_slot_write_barrier", (c_ptr, c_ptr, c_ptr), c_void)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)
pcc_threads_enabled = extern("pcc_threads_enabled", (), c_int64)
py_obj_setattr = extern("py_obj_setattr", (c_ptr, c_ptr, c_ptr), c_int64)
pcc_mutex_new = extern("pcc_mutex_new", (), c_ptr)
pcc_mutex_free = extern("pcc_mutex_free", (c_ptr,), c_void)
pcc_mutex_lock = extern("pcc_mutex_lock", (c_ptr,), c_int64)
pcc_mutex_unlock = extern("pcc_mutex_unlock", (c_ptr,), c_int64)


define_global_ptr_null("pcc_module_attrs_cache")
# 512 (C string address, pinned str) pairs; see _cached_name_key.
define_global_ptr_null("pcc_module_name_key_cache")
define_global_ptr_null("py_func_code_class_cache")

# The public dictionary is the sole lasting owner of imported module objects.
# Namespace dictionaries remain separate: querying this map never imports or
# creates a namespace. Every NEW lookup is published before key cleanup.
define_global_ptr_null("pcc_sys_modules_cache")
define_global_ptr_null("pcc_sys_modules_root_handle")
define_global_i64("pcc_sys_modules_mutex_bits", 0)
_SM_KEY = 0
_SM_VALUE = 1
_SM_CURRENT = 2
_SM_ERROR = 3
_SM_OUTPUT = 4
_SM_CACHE = 5
_SM_COUNT = 6
define_global_i32("pcc_sys_modules_frame_map", _SM_COUNT)


@c_abi_export("py_sys_modules_owner_adopt")
def _sys_modules_adopt(slots, tokens, index: int) -> None:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    if not ptr_is_null(load_ptr(slot, 0)):
        token: int = pcc_gc_foreign_lease_acquire(slot)
        store_i64(tokens, index * C_POINTER_SIZE, token)
        if token < 0 and not py_err_occurred():
            py_raise_owned(py_exc_new(19, cstr("sys.modules owner lease failed")))


def _sys_modules_key(slots, tokens, module_name) -> None:
    store_ptr(slots, _SM_KEY * C_POINTER_SIZE, _module_name_key(module_name))
    _sys_modules_adopt(slots, tokens, _SM_KEY)
    if ptr_is_null(load_ptr(slots, _SM_KEY * C_POINTER_SIZE)) and not py_err_occurred():
        py_raise_owned(py_exc_new(19, cstr("sys.modules key allocation failed")))


@c_abi_export("py_sys_modules_owner_drop")
def _sys_modules_drop(slots, tokens, index: int) -> None:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    token: int = load_i64(tokens, index * C_POINTER_SIZE)
    if token >= 0 and pcc_gc_foreign_lease_release(slot, token) < 0:
        pcc_platform_abort()
        return
    store_i64(tokens, index * C_POINTER_SIZE, -1)
    pcc_gc_store_root(slot, null())


@c_abi_export("py_sys_modules_owner_finish")
def _sys_modules_finish(slots, tokens):
    saved = ptr_add(slots, _SM_ERROR * C_POINTER_SIZE)
    if ptr_is_null(load_ptr(saved, 0)):
        py_tls_exc_swap_slot(saved)
        store_ptr(slots, _SM_ERROR * C_POINTER_SIZE, load_ptr(saved, 0))
        _sys_modules_adopt(slots, tokens, _SM_ERROR)
    if not ptr_is_null(load_ptr(saved, 0)):
        _sys_modules_drop(slots, tokens, _SM_OUTPUT)
    index: int = 0
    while index < _SM_COUNT:
        if index != _SM_ERROR and index != _SM_OUTPUT:
            _sys_modules_drop(slots, tokens, index)
        index = index + 1
    py_clear_exception()
    token: int = load_i64(tokens, _SM_ERROR * C_POINTER_SIZE)
    if token >= 0 and pcc_gc_foreign_lease_release(saved, token) < 0:
        pcc_platform_abort()
        return null()
    py_tls_exc_swap_slot(saved)
    result = ptr_add(slots, _SM_OUTPUT * C_POINTER_SIZE)
    pcc_py_gc_minor_graph_lock()
    value = pcc_gc_load_ptr(null(), result)
    prior: int = 0
    if not ptr_is_null(value) and not is_tagged_int(value):
        prior = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & PY_FLAG_GC_PINNED
        pcc_gc_pin(value)
    pcc_py_gc_minor_graph_unlock()
    token = load_i64(tokens, _SM_OUTPUT * C_POINTER_SIZE)
    if token >= 0 and pcc_gc_foreign_lease_release(result, token) < 0:
        pcc_platform_abort()
        return null()
    pcc_gc_frame_leave(slots)
    return pcc_gc_take_pinned_slot(result, prior)


def _sys_modules_mutex():
    slot = global_addr("pcc_sys_modules_mutex_bits")
    bits: int = atomic_load_i64(slot, 0, "acquire")
    if bits != 0:
        return int_to_ptr(bits)
    candidate = pcc_mutex_new()
    if ptr_is_null(candidate):
        py_raise_owned(py_exc_new(19, cstr("sys.modules lock allocation failed")))
        return null()
    previous: int = atomic_cas_i64(slot, 0, 0, ptr_to_int(candidate), "acq_rel", "acquire")
    if previous != 0:
        pcc_mutex_free(candidate)
        return int_to_ptr(previous)
    return candidate


def _sys_modules_cache_slot():
    # The registered physical slot owns the dictionary. Header tokens held by
    # user aliases are temporary and are never the lasting ownership proof.
    slot = global_addr("pcc_sys_modules_cache")
    if not ptr_is_null(pcc_gc_load_ptr(null(), slot)):
        return slot
    mutex = _sys_modules_mutex()
    if ptr_is_null(mutex):
        return null()
    if pcc_mutex_lock(mutex) != 0:
        py_raise_owned(py_exc_new(7, cstr("sys.modules lock failed")))
        return null()
    # Lock acquisition can park; reload the authoritative slot afterward.
    if ptr_is_null(pcc_gc_load_ptr(null(), slot)):
        handle = global_load_ptr("pcc_sys_modules_root_handle")
        if ptr_is_null(handle):
            handle = pcc_gc_scheduler_root_register_handle(slot)
            if ptr_is_null(handle):
                pcc_mutex_unlock(mutex)
                py_raise_owned(py_exc_new(19, cstr("sys.modules root registration failed")))
                return null()
            global_store_ptr("pcc_sys_modules_root_handle", handle)
        global_store_ptr("pcc_sys_modules_cache", py_dict_new())
        token: int = pcc_gc_foreign_lease_acquire(slot)
        if token < 0:
            pcc_gc_store_root(slot, null())
            pcc_mutex_unlock(mutex)
            py_raise_owned(py_exc_new(19, cstr("sys.modules dictionary allocation failed")))
            return null()
        value = pcc_gc_load_ptr(null(), slot)
        if ptr_is_null(value):
            pcc_gc_foreign_lease_release(slot, token)
            pcc_mutex_unlock(mutex)
            if not py_err_occurred():
                py_raise_owned(py_exc_new(19, cstr("sys.modules dictionary allocation failed")))
            return null()
        pcc_gc_note_slot_write_barrier(null(), slot, value)
        if pcc_gc_foreign_lease_release(slot, token) < 0:
            pcc_platform_abort()
            return null()
    pcc_mutex_unlock(mutex)
    return slot


def _sys_modules_acquire_cache(slots, tokens) -> bool:
    source = _sys_modules_cache_slot()
    if ptr_is_null(source):
        return False
    token: int = pcc_gc_root_copy_lease(ptr_add(slots, _SM_CACHE * C_POINTER_SIZE), source)
    store_i64(tokens, _SM_CACHE * C_POINTER_SIZE, token)
    if token < 0:
        if not py_err_occurred():
            py_raise_owned(py_exc_new(19, cstr("sys.modules cache copy failed")))
        return False
    return True


@c_abi_export("py_sys_modules")
def py_sys_modules():
    slots = stack_alloc(_SM_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_SM_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _SM_COUNT * C_POINTER_SIZE)
    memset(tokens, 255, _SM_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_sys_modules_frame_map"), slots)
    source = _sys_modules_cache_slot()
    if not ptr_is_null(source):
        token: int = pcc_gc_root_copy_lease(ptr_add(slots, _SM_OUTPUT * C_POINTER_SIZE), source)
        store_i64(tokens, _SM_OUTPUT * C_POINTER_SIZE, token)
        if token < 0 and not py_err_occurred():
            py_raise_owned(py_exc_new(19, cstr("sys.modules cache copy failed")))
    return _sys_modules_finish(slots, tokens)


@c_abi_export("py_sys_modules_find")
def py_sys_modules_find(module_name):
    slots = stack_alloc(_SM_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_SM_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _SM_COUNT * C_POINTER_SIZE)
    memset(tokens, 255, _SM_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_sys_modules_frame_map"), slots)
    if _sys_modules_acquire_cache(slots, tokens):
        _sys_modules_key(slots, tokens, module_name)
        if not ptr_is_null(load_ptr(slots, _SM_KEY * C_POINTER_SIZE)):
            store_ptr(slots, _SM_OUTPUT * C_POINTER_SIZE, py_dict_get(load_ptr(slots, _SM_CACHE * C_POINTER_SIZE), load_ptr(slots, _SM_KEY * C_POINTER_SIZE)))
            _sys_modules_adopt(slots, tokens, _SM_OUTPUT)
    return _sys_modules_finish(slots, tokens)


@c_abi_export("py_sys_modules_import_cached")
def py_sys_modules_import_cached(module_name):
    value = py_sys_modules_find(module_name)
    if not ptr_is_null(value) and ptr_eq(value, global_load_ptr("py_None")):
        py_decref(value)
        _sys_modules_none_error(module_name)
        return null()
    return value


def _sys_modules_none_error(module_name) -> None:
    prefix = cstr("import of ")
    suffix = cstr(" halted; None in sys.modules")
    prefix_size: int = strlen(prefix)
    name_size: int = strlen(module_name)
    suffix_size: int = strlen(suffix)
    message = malloc(prefix_size + name_size + suffix_size + 1)
    if ptr_is_null(message):
        py_raise_owned(py_exc_new(19, cstr("module import error allocation failed")))
        return
    memcpy(message, prefix, prefix_size)
    memcpy(ptr_add(message, prefix_size), module_name, name_size)
    memcpy(ptr_add(message, prefix_size + name_size), suffix, suffix_size + 1)
    py_raise_owned(py_exc_new(21, message))
    free(message)


@c_abi_export("py_sys_modules_publish")
def py_sys_modules_publish(module_name, value) -> int:
    # Import callers keep an independent counted lease until this input is copied.
    slots = stack_alloc(_SM_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_SM_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _SM_COUNT * C_POINTER_SIZE)
    memset(tokens, 255, _SM_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_sys_modules_frame_map"), slots)
    py_incref(value)
    store_ptr(slots, _SM_VALUE * C_POINTER_SIZE, value)
    _sys_modules_adopt(slots, tokens, _SM_VALUE)
    if _sys_modules_acquire_cache(slots, tokens):
        _sys_modules_key(slots, tokens, module_name)
        if not ptr_is_null(load_ptr(slots, _SM_KEY * C_POINTER_SIZE)):
            py_dict_set(load_ptr(slots, _SM_CACHE * C_POINTER_SIZE), load_ptr(slots, _SM_KEY * C_POINTER_SIZE),
                        load_ptr(slots, _SM_VALUE * C_POINTER_SIZE))
    _sys_modules_finish(slots, tokens)
    if py_err_occurred():
        return -1
    return 0


@c_abi_export("py_sys_modules_rollback")
def py_sys_modules_rollback(module_name) -> None:
    # Python removes the failed name even when initialization replaced its
    # cache value. Lookup/deletion errors and replacement finalizers must not
    # replace the initializer's original exception.
    slots = stack_alloc(_SM_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_SM_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _SM_COUNT * C_POINTER_SIZE)
    memset(tokens, 255, _SM_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_sys_modules_frame_map"), slots)
    saved = ptr_add(slots, _SM_ERROR * C_POINTER_SIZE)
    py_tls_exc_swap_slot(saved)
    store_ptr(slots, _SM_ERROR * C_POINTER_SIZE, load_ptr(saved, 0))
    _sys_modules_adopt(slots, tokens, _SM_ERROR)
    if _sys_modules_acquire_cache(slots, tokens):
        _sys_modules_key(slots, tokens, module_name)
        if not ptr_is_null(load_ptr(slots, _SM_KEY * C_POINTER_SIZE)):
            py_dict_del(load_ptr(slots, _SM_CACHE * C_POINTER_SIZE), load_ptr(slots, _SM_KEY * C_POINTER_SIZE))
    _sys_modules_finish(slots, tokens)


@c_abi_export("py_sys_modules_finish_import")
def py_sys_modules_finish_import(module_name, original):
    """Consume one creator reference and return the live entry; caller retains a lease."""
    slots = stack_alloc(_SM_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_SM_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _SM_COUNT * C_POINTER_SIZE)
    memset(tokens, 255, _SM_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_sys_modules_frame_map"), slots)
    saved = ptr_add(slots, _SM_ERROR * C_POINTER_SIZE)
    py_tls_exc_swap_slot(saved)
    store_ptr(slots, _SM_ERROR * C_POINTER_SIZE, load_ptr(saved, 0))
    _sys_modules_adopt(slots, tokens, _SM_ERROR)
    store_ptr(slots, _SM_VALUE * C_POINTER_SIZE, original)
    _sys_modules_adopt(slots, tokens, _SM_VALUE)
    store_ptr(slots, _SM_OUTPUT * C_POINTER_SIZE, py_sys_modules_find(module_name))
    _sys_modules_adopt(slots, tokens, _SM_OUTPUT)
    if ptr_is_null(load_ptr(slots, _SM_OUTPUT * C_POINTER_SIZE)) and not py_err_occurred():
        py_raise_owned(py_exc_new(4, module_name))
    if (not py_err_occurred() and ptr_is_null(load_ptr(saved, 0))
            and not ptr_is_null(load_ptr(slots, _SM_OUTPUT * C_POINTER_SIZE))):
        _sys_modules_publish_parent(slots, tokens, module_name)
    return _sys_modules_finish(slots, tokens)


def _sys_modules_publish_parent(slots, tokens, name) -> None:
    split: int = -1
    index: int = 0
    while load_i8(name, index) != 0:
        if load_i8(name, index) == 46:
            split = index
        index = index + 1
    if split < 0:
        return
    parent = malloc(split + 1)
    if ptr_is_null(parent):
        py_raise_owned(py_exc_new(19, cstr("module parent allocation failed")))
        return
    memcpy(parent, name, split)
    store_i8(parent, split, 0)
    store_ptr(slots, _SM_CURRENT * C_POINTER_SIZE, py_sys_modules_find(parent))
    _sys_modules_adopt(slots, tokens, _SM_CURRENT)
    if ptr_is_null(load_ptr(slots, _SM_CURRENT * C_POINTER_SIZE)):
        # The compiled graph can register a dotted provider without a parent
        # initializer (for example an elided compile-time package). A cache
        # miss is not a Python subscription: publish only to an existing
        # parent, preserving any real lookup error already in TLS.
        free(parent)
        return
    free(parent)
    if py_err_occurred():
        return
    status: int = py_obj_setattr(load_ptr(slots, _SM_CURRENT * C_POINTER_SIZE),
                                ptr_add(name, split + 1),
                                load_ptr(slots, _SM_OUTPUT * C_POINTER_SIZE))
    if status != 0 and not py_err_occurred():
        py_raise_owned(py_exc_new(7, cstr("module parent publication failed")))


@c_abi_export("py_sys_modules_abort_import")
def py_sys_modules_abort_import(module_name, original) -> None:
    """Rollback the failed name and retire its creator with the error preserved."""
    slots = stack_alloc(_SM_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_SM_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _SM_COUNT * C_POINTER_SIZE)
    memset(tokens, 255, _SM_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_sys_modules_frame_map"), slots)
    saved = ptr_add(slots, _SM_ERROR * C_POINTER_SIZE)
    py_tls_exc_swap_slot(saved)
    store_ptr(slots, _SM_ERROR * C_POINTER_SIZE, load_ptr(saved, 0))
    _sys_modules_adopt(slots, tokens, _SM_ERROR)
    store_ptr(slots, _SM_VALUE * C_POINTER_SIZE, original)
    _sys_modules_adopt(slots, tokens, _SM_VALUE)
    py_sys_modules_rollback(module_name)
    _sys_modules_finish(slots, tokens)


def _module_name_key(module_name):
    if ptr_is_null(module_name):
        module_name = cstr("")
    return py_str_new(module_name, strlen(module_name))


def _str_matches_cstr(key, text) -> int:
    """Whether str ``key`` holds exactly the bytes of the C string ``text``.

    Stops at the C string's terminator, so it never reads past it.
    """
    length: int = load_i64(key, PYSTROBJECT_BYTE_LEN_OFFSET)
    data = ptr_add(key, PYSTROBJECT_DATA_OFFSET)
    index: int = 0
    same: int = 1
    while same != 0 and index < length:
        byte: int = load_i8(text, index)
        if byte == 0 or byte != load_i8(data, index):
            same = 0
        index = index + 1
    if same != 0 and load_i8(text, length) != 0:
        same = 0
    return same


def _cached_name_key(text):
    """A pinned str equal to the C string ``text``, owned by the cache.

    Module and attribute names arrive as C strings, nearly always the same
    pooled constants. Building a key per lookup allocated, hashed and
    released one str per name, twice per module global read, which was most
    of py_module_attr_get in a pcc1 codegen worker. An entry is reused only
    when its address and its bytes both match, so a reused buffer address
    cannot return another name. Threaded runtimes keep per-call keys.
    """
    if pcc_threads_enabled() != 0:
        return null()
    table = global_load_ptr("pcc_module_name_key_cache")
    if ptr_is_null(table):
        table = malloc(8192)
        if ptr_is_null(table):
            return null()
        memset(table, 0, 8192)
        global_store_ptr("pcc_module_name_key_cache", table)
    slot = ptr_add(table, ((ptr_to_int(text) >> 3) & 511) * 16)
    key = load_ptr(slot, 8)
    if not ptr_is_null(key) and ptr_eq(load_ptr(slot, 0), text):
        if _str_matches_cstr(key, text) != 0:
            return key
    fresh = py_str_new(text, strlen(text))
    if ptr_is_null(fresh):
        return null()
    pcc_gc_pin(fresh)
    store_ptr(slot, 0, text)
    store_ptr(slot, 8, fresh)
    if not ptr_is_null(key):
        pcc_gc_unpin(key)
        py_decref(key)
    return fresh


@c_abi_export("pcc_cached_name_key")
def pcc_cached_name_key(text):
    """A borrowed pinned str equal to C string ``text``, or NULL.

    NULL means no cached key is available (threaded runtime or allocation
    failure); the caller then builds its own key as before.
    """
    if ptr_is_null(text):
        return null()
    return _cached_name_key(text)


def _module_cache(create: int):
    cache = global_load_ptr("pcc_module_attrs_cache")
    if ptr_is_null(cache) and create != 0:
        cache = py_dict_new()
        if ptr_is_null(cache):
            return null()
        pcc_gc_pin(cache)
        global_store_ptr("pcc_module_attrs_cache", cache)
    return cache


def _module_find(module_name):
    cache = _module_cache(0)
    if ptr_is_null(cache):
        return null()
    if not ptr_is_null(module_name):
        cached = _cached_name_key(module_name)
        if not ptr_is_null(cached):
            attrs = py_dict_get(cache, cached)
            if not ptr_is_null(attrs):
                py_decref(attrs)
            return attrs
    key = _module_name_key(module_name)
    if ptr_is_null(key):
        return null()
    attrs = py_dict_get(cache, key)
    py_decref(key)
    if not ptr_is_null(attrs):
        # The cache owns the lifetime.  Expose the same borrowed result as the
        # former linked-list node instead of leaking py_dict_get's owned ref.
        py_decref(attrs)
    return attrs


def _module_ensure(module_name):
    attrs = _module_find(module_name)
    if not ptr_is_null(attrs):
        return attrs
    cache = _module_cache(1)
    if ptr_is_null(cache):
        return null()
    key = _module_name_key(module_name)
    if ptr_is_null(key):
        return null()
    attrs = py_dict_new()
    if ptr_is_null(attrs):
        py_decref(key)
        return null()
    pcc_gc_pin(attrs)
    py_dict_set(cache, key, attrs)
    py_decref(key)
    py_decref(attrs)
    return attrs


@c_abi_export("py_module_attrs_dict")
def py_module_attrs_dict(module_name, create: int):
    if create != 0:
        return _module_ensure(module_name)
    return _module_find(module_name)


@c_abi_export("py_module_attr_set")
def py_module_attr_set(module_name, attr_name, value) -> int:
    if ptr_is_null(attr_name) or ptr_is_null(value):
        return -1
    attrs = _module_ensure(module_name)
    if ptr_is_null(attrs):
        return -1
    key = py_str_new(attr_name, strlen(attr_name))
    if ptr_is_null(key):
        return -1
    py_dict_set(attrs, key, value)
    py_decref(key)
    return 0


@c_abi_export("py_module_attr_get")
def py_module_attr_get(module_name, attr_name):
    if ptr_is_null(attr_name):
        return null()
    attrs = _module_find(module_name)
    if ptr_is_null(attrs):
        return null()
    cached = _cached_name_key(attr_name)
    if not ptr_is_null(cached):
        return py_dict_get(attrs, cached)
    key = py_str_new(attr_name, strlen(attr_name))
    if ptr_is_null(key):
        return null()
    value = py_dict_get(attrs, key)
    py_decref(key)
    return value


def _is_string(value) -> bool:
    if ptr_is_null(value) or is_tagged_int(value):
        return False
    return load_i32(value, 8) == PY_TYPE_STR


def _import_star_name(dest, source_module, source_attrs, name) -> int:
    if not _is_string(name):
        py_raise_owned(py_exc_new(3, cstr("module __all__ must contain only strings")))
        return -1
    value = py_dict_get(source_attrs, name)
    if ptr_is_null(value):
        attr_name = py_str_utf8(name)
        if not ptr_is_null(attr_name):
            value = py_obj_getattr(source_module, attr_name)
    if ptr_is_null(value):
        return -1
    py_dict_set(dest, name, value)
    py_decref(value)
    return 0


@c_abi_export("py_module_import_star")
def py_module_import_star(module_name, source_module) -> int:
    if ptr_is_null(module_name) or ptr_is_null(source_module):
        return -1
    source_attrs = py_obj_getattr(source_module, cstr("__dict__"))
    if ptr_is_null(source_attrs):
        return -1
    dest = _module_ensure(module_name)
    if ptr_is_null(dest):
        py_decref(source_attrs)
        return -1

    all_key = py_str_new(cstr("__all__"), 7)
    if ptr_is_null(all_key):
        py_decref(source_attrs)
        return -1
    all_names = py_dict_get(source_attrs, all_key)
    py_decref(all_key)
    if not ptr_is_null(all_names):
        count: int = py_obj_len(all_names)
        if count < 0:
            py_decref(all_names)
            py_decref(source_attrs)
            return -1
        i: int = 0
        while i < count:
            name = py_obj_getitem_i64(all_names, i)
            if ptr_is_null(name):
                py_decref(all_names)
                py_decref(source_attrs)
                return -1
            rc: int = _import_star_name(dest, source_module, source_attrs, name)
            py_decref(name)
            if rc != 0:
                py_decref(all_names)
                py_decref(source_attrs)
                return -1
            i = i + 1
        py_decref(all_names)
        py_decref(source_attrs)
        return 0

    entries: int = py_dict_entries_used(source_attrs)
    i = 0
    while i < entries:
        name = py_dict_entry_key_at(source_attrs, i)
        if not ptr_is_null(name) and _is_string(name):
            text = py_str_utf8(name)
            if not ptr_is_null(text) and load_i8(text, 0) != 95:
                if _import_star_name(dest, source_module, source_attrs, name) != 0:
                    py_decref(name)
                    py_decref(source_attrs)
                    return -1
        if not ptr_is_null(name):
            py_decref(name)
        i = i + 1
    py_decref(source_attrs)
    return 0


@c_abi_export("py_module_attr_value_or_default")
def py_module_attr_value_or_default(slot, default_value):
    if ptr_is_null(slot):
        return default_value
    value = pcc_gc_load_ptr(null(), slot)
    if ptr_is_null(value):
        return default_value
    if not ptr_is_null(default_value):
        py_decref(default_value)
    return value


@c_abi_export("py_module_attr_del")
def py_module_attr_del(module_name, attr_name) -> int:
    if ptr_is_null(attr_name):
        return -1
    attrs = _module_find(module_name)
    if ptr_is_null(attrs):
        return -1
    key = py_str_new(attr_name, strlen(attr_name))
    if ptr_is_null(key):
        return -1
    rc: int = py_dict_del(attrs, key)
    py_decref(key)
    return rc


@c_abi_export("py_module_attr_len")
def py_module_attr_len(module_name) -> int:
    attrs = _module_find(module_name)
    if ptr_is_null(attrs):
        return 0
    return py_dict_len(attrs)
