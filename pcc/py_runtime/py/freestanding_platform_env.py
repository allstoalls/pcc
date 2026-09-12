"""Freestanding process-environment ownership authored in pcc-Python."""

from pcc import i64
from pcc.extern import c_abi_export
from pcc.unsafe import (
    atomic_clear,
    atomic_load_i64,
    atomic_store_i64,
    atomic_test_and_set,
    define_global_i8,
    define_global_i64,
    define_global_ptr_null,
    free,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    initial_environ,
    load_i8,
    load_ptr,
    malloc,
    null,
    ptr_add,
    ptr_is_null,
    store_i8,
    store_ptr,
)

__pcc_freestanding__ = True


define_global_i8("pcc_platform_env_lock", 0)
define_global_i64("pcc_platform_env_count", 0)
define_global_i64("pcc_platform_env_capacity", 0)
define_global_i64("pcc_platform_env_ready", 0)
define_global_ptr_null("pcc_initial_envp")
define_global_ptr_null("pcc_platform_env_entries")


@c_abi_export("pcc_platform_env_lock_acquire")
def _env_lock_acquire() -> None:
    while atomic_test_and_set(
        global_addr("pcc_platform_env_lock"), 0, "acquire"
    ) != 0:
        pass


@c_abi_export("pcc_platform_env_lock_release")
def _env_lock_release() -> None:
    atomic_clear(global_addr("pcc_platform_env_lock"), 0, "release")


@c_abi_export("pcc_platform_env_cstr_len")
def _env_cstr_len(value, limit: i64) -> i64:
    if ptr_is_null(value):
        return -1
    offset: i64 = 0
    while offset < limit:
        if load_i8(value, offset) == 0:
            return offset
        offset = offset + 1
    return -1


@c_abi_export("pcc_platform_env_valid_name_len")
def _env_valid_name_len(name) -> i64:
    length = _env_cstr_len(name, 1048576)
    if length <= 0:
        return -1
    offset: i64 = 0
    while offset < length:
        if load_i8(name, offset) == 61:
            return -1
        offset = offset + 1
    return length


@c_abi_export("pcc_platform_env_entry_matches")
def _env_entry_matches(entry, name, name_len: i64) -> i64:
    if ptr_is_null(entry):
        return 0
    offset: i64 = 0
    while offset < name_len:
        if load_i8(entry, offset) != load_i8(name, offset):
            return 0
        offset = offset + 1
    if load_i8(entry, name_len) == 61:
        return 1
    return 0


@c_abi_export("pcc_platform_env_grow")
def _env_grow() -> i64:
    count = atomic_load_i64(
        global_addr("pcc_platform_env_count"), 0, "relaxed"
    )
    capacity = atomic_load_i64(
        global_addr("pcc_platform_env_capacity"), 0, "relaxed"
    )
    if count < capacity:
        return 0
    new_capacity: i64 = 16
    if capacity > 0:
        if capacity >= 1048576:
            return -1
        new_capacity = capacity * 2
    replacement = malloc(new_capacity * 8)
    if ptr_is_null(replacement):
        return -1
    old_entries = global_load_ptr("pcc_platform_env_entries")
    index: i64 = 0
    while index < count:
        store_ptr(replacement, index * 8, load_ptr(old_entries, index * 8))
        index = index + 1
    free(old_entries)
    global_store_ptr("pcc_platform_env_entries", replacement)
    atomic_store_i64(
        global_addr("pcc_platform_env_capacity"),
        0,
        new_capacity,
        "relaxed",
    )
    return 0


@c_abi_export("pcc_platform_env_copy_entry")
def _env_copy_entry(entry):
    length = _env_cstr_len(entry, 1048576)
    if length < 0:
        return null()
    owned = malloc(length + 1)
    if ptr_is_null(owned):
        return null()
    offset: i64 = 0
    while offset <= length:
        store_i8(owned, offset, load_i8(entry, offset))
        offset = offset + 1
    return owned


@c_abi_export("pcc_platform_env_make_entry")
def _env_make_entry(name, name_len: i64, value):
    value_len = _env_cstr_len(value, 1048576)
    if value_len < 0 or name_len + value_len + 2 > 2097152:
        return null()
    owned = malloc(name_len + value_len + 2)
    if ptr_is_null(owned):
        return null()
    offset: i64 = 0
    while offset < name_len:
        store_i8(owned, offset, load_i8(name, offset))
        offset = offset + 1
    store_i8(owned, name_len, 61)
    value_offset: i64 = 0
    while value_offset <= value_len:
        store_i8(owned, name_len + 1 + value_offset, load_i8(value, value_offset))
        value_offset = value_offset + 1
    return owned


@c_abi_export("pcc_platform_env_append_owned")
def _env_append_owned(owned) -> i64:
    if _env_grow() != 0:
        return -1
    count = atomic_load_i64(
        global_addr("pcc_platform_env_count"), 0, "relaxed"
    )
    entries = global_load_ptr("pcc_platform_env_entries")
    store_ptr(entries, count * 8, owned)
    atomic_store_i64(
        global_addr("pcc_platform_env_count"), 0, count + 1, "relaxed"
    )
    return 0


@c_abi_export("pcc_platform_env_snapshot_count")
def _env_snapshot_count() -> i64:
    """Number of environment entries, populating the table on first use."""
    _env_lock_acquire()
    if _env_ensure() != 0:
        _env_lock_release()
        return -1
    count = atomic_load_i64(
        global_addr("pcc_platform_env_count"), 0, "relaxed"
    )
    _env_lock_release()
    return count


@c_abi_export("pcc_platform_env_snapshot_entry")
def _env_snapshot_entry(index: i64):
    """A fresh copy of entry ``index`` (``NAME=VALUE``), or NULL.

    The caller owns the copy and frees it.  Returning the table's own
    pointer would hand out a reference that ``_env_clear_owned`` can free
    from another thread; copying under the lock keeps the window closed.
    """
    _env_lock_acquire()
    if _env_ensure() != 0:
        _env_lock_release()
        return null()
    count = atomic_load_i64(
        global_addr("pcc_platform_env_count"), 0, "relaxed"
    )
    if index < 0 or index >= count:
        _env_lock_release()
        return null()
    entries = global_load_ptr("pcc_platform_env_entries")
    owned = _env_copy_entry(load_ptr(entries, index * 8))
    _env_lock_release()
    return owned


@c_abi_export("pcc_platform_env_clear_owned")
def _env_clear_owned() -> None:
    count = atomic_load_i64(
        global_addr("pcc_platform_env_count"), 0, "relaxed"
    )
    entries = global_load_ptr("pcc_platform_env_entries")
    index: i64 = 0
    while index < count:
        free(load_ptr(entries, index * 8))
        index = index + 1
    free(entries)
    global_store_ptr("pcc_platform_env_entries", null())
    atomic_store_i64(
        global_addr("pcc_platform_env_count"), 0, 0, "relaxed"
    )
    atomic_store_i64(
        global_addr("pcc_platform_env_capacity"), 0, 0, "relaxed"
    )


@c_abi_export("pcc_platform_env_ensure")
def _env_ensure() -> i64:
    if atomic_load_i64(
        global_addr("pcc_platform_env_ready"), 0, "relaxed"
    ) != 0:
        return 0
    source = global_load_ptr("pcc_initial_envp")
    if ptr_is_null(source):
        source = initial_environ()
    if not ptr_is_null(source):
        index: i64 = 0
        while index < 1048576:
            entry = load_ptr(source, index * 8)
            if ptr_is_null(entry):
                break
            owned = _env_copy_entry(entry)
            if ptr_is_null(owned) or _env_append_owned(owned) != 0:
                free(owned)
                _env_clear_owned()
                return -1
            index = index + 1
        if index >= 1048576:
            _env_clear_owned()
            return -1
    atomic_store_i64(
        global_addr("pcc_platform_env_ready"), 0, 1, "release"
    )
    return 0


@c_abi_export("pcc_platform_env_init")
def pcc_platform_env_init(envp) -> i64:
    if ptr_is_null(envp):
        return -1
    _env_lock_acquire()
    if atomic_load_i64(
        global_addr("pcc_platform_env_ready"), 0, "relaxed"
    ) != 0:
        _env_lock_release()
        return -1
    global_store_ptr("pcc_initial_envp", envp)
    result = _env_ensure()
    _env_lock_release()
    return result


@c_abi_export("pcc_platform_getenv")
def pcc_platform_getenv(name):
    name_len = _env_valid_name_len(name)
    if name_len < 0:
        return null()
    _env_lock_acquire()
    if _env_ensure() != 0:
        _env_lock_release()
        return null()
    count = atomic_load_i64(
        global_addr("pcc_platform_env_count"), 0, "relaxed"
    )
    entries = global_load_ptr("pcc_platform_env_entries")
    index: i64 = 0
    while index < count:
        entry = load_ptr(entries, index * 8)
        if _env_entry_matches(entry, name, name_len) != 0:
            result = ptr_add(entry, name_len + 1)
            _env_lock_release()
            return result
        index = index + 1
    _env_lock_release()
    return null()


@c_abi_export("pcc_platform_setenv")
def pcc_platform_setenv(name, value, overwrite: i64) -> i64:
    name_len = _env_valid_name_len(name)
    if name_len < 0 or ptr_is_null(value):
        return -1
    _env_lock_acquire()
    if _env_ensure() != 0:
        _env_lock_release()
        return -1
    count = atomic_load_i64(
        global_addr("pcc_platform_env_count"), 0, "relaxed"
    )
    entries = global_load_ptr("pcc_platform_env_entries")
    index: i64 = 0
    while index < count:
        old = load_ptr(entries, index * 8)
        if _env_entry_matches(old, name, name_len) != 0:
            if overwrite == 0:
                _env_lock_release()
                return 0
            replacement = _env_make_entry(name, name_len, value)
            if ptr_is_null(replacement):
                _env_lock_release()
                return -1
            store_ptr(entries, index * 8, replacement)
            free(old)
            _env_lock_release()
            return 0
        index = index + 1
    owned = _env_make_entry(name, name_len, value)
    if ptr_is_null(owned) or _env_append_owned(owned) != 0:
        free(owned)
        _env_lock_release()
        return -1
    _env_lock_release()
    return 0


@c_abi_export("pcc_platform_unsetenv")
def pcc_platform_unsetenv(name) -> i64:
    name_len = _env_valid_name_len(name)
    if name_len < 0:
        return -1
    _env_lock_acquire()
    if _env_ensure() != 0:
        _env_lock_release()
        return -1
    count = atomic_load_i64(
        global_addr("pcc_platform_env_count"), 0, "relaxed"
    )
    entries = global_load_ptr("pcc_platform_env_entries")
    index: i64 = 0
    while index < count:
        entry = load_ptr(entries, index * 8)
        if _env_entry_matches(entry, name, name_len) != 0:
            free(entry)
            shift = index
            while shift + 1 < count:
                store_ptr(
                    entries,
                    shift * 8,
                    load_ptr(entries, (shift + 1) * 8),
                )
                shift = shift + 1
            count = count - 1
            store_ptr(entries, count * 8, null())
        else:
            index = index + 1
    atomic_store_i64(
        global_addr("pcc_platform_env_count"), 0, count, "relaxed"
    )
    _env_lock_release()
    return 0


@c_abi_export("pcc_platform_env_snapshot")
def pcc_platform_env_snapshot():
    # Process creation may outlive the environment lock and concurrent writers
    # can replace or free live entries.  Return a deep-copied, NULL-terminated
    # char** rather than lending the live table.
    _env_lock_acquire()
    if _env_ensure() != 0:
        _env_lock_release()
        return null()
    count = atomic_load_i64(
        global_addr("pcc_platform_env_count"), 0, "relaxed"
    )
    snapshot = malloc((count + 1) * 8)
    if ptr_is_null(snapshot):
        _env_lock_release()
        return null()
    entries = global_load_ptr("pcc_platform_env_entries")
    index: i64 = 0
    while index < count:
        owned = _env_copy_entry(load_ptr(entries, index * 8))
        if ptr_is_null(owned):
            cleanup: i64 = 0
            while cleanup < index:
                free(load_ptr(snapshot, cleanup * 8))
                cleanup = cleanup + 1
            free(snapshot)
            _env_lock_release()
            return null()
        store_ptr(snapshot, index * 8, owned)
        index = index + 1
    store_ptr(snapshot, count * 8, null())
    _env_lock_release()
    return snapshot


@c_abi_export("pcc_platform_env_snapshot_free")
def pcc_platform_env_snapshot_free(snapshot) -> None:
    if ptr_is_null(snapshot):
        return
    index: i64 = 0
    while index < 1048576:
        entry = load_ptr(snapshot, index * 8)
        if ptr_is_null(entry):
            break
        free(entry)
        index = index + 1
    free(snapshot)
