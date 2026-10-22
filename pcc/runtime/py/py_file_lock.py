"""Owned stdlib locks: return None or an owned exception to ordinary Python raise.

Arguments remain registered across __index__ callbacks. flock keeps its original
file owner alive. Bounded nonblocking attempts allow stop-the-world safepoints.
"""
__pcc_runtime_port__ = True
from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    cstr, fd_control, file_flock, file_lock_region, seek_file, global_load_ptr, load_i32, load_ptr,
    null, ptr_add, ptr_is_null, stack_alloc, store_ptr,
)
py_index_i64_checked = extern("py_index_i64_checked", (c_ptr,), c_int64)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_current_exception = extern("py_current_exception", (), c_ptr)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_incref = extern("py_incref", (c_ptr,), c_void)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)
pcc_gc_scheduler_root_register_handle = extern("pcc_gc_scheduler_root_register_handle", (c_ptr,), c_ptr)
pcc_gc_scheduler_root_unregister_handle = extern("pcc_gc_scheduler_root_unregister_handle", (c_ptr,), c_void)
pcc_thread_safepoint = extern("pcc_thread_safepoint", (), c_void)
pcc_platform_sleep_ns = extern("pcc_platform_sleep_ns", (c_int64,), c_int64)


def _error_value():
    error = py_current_exception()
    if ptr_is_null(error):
        return py_exc_new(7, cstr("file-lock conversion failed without an exception"))
    # TLS keeps the object alive, but incref/clear can reach graph-lock
    # safepoints. Pin before either operation and preserve an existing pin.
    already_pinned: int = load_i32(error, 12) & 64
    if already_pinned == 0:
        pcc_gc_pin(error)
    py_incref(error)
    py_clear_exception()
    if already_pinned == 0:
        pcc_gc_unpin(error)
    return error


def _unroot(handles) -> None:
    index: int = 0
    while index < 3:
        handle = load_ptr(handles, index * 8)
        if ptr_is_null(handle) == 0:
            pcc_gc_scheduler_root_unregister_handle(handle)
        index = index + 1


def _root(slots, handles) -> int:
    index: int = 0
    while index < 3:
        store_ptr(handles, index * 8, null())
        index = index + 1
    index = 0
    while index < 3:
        handle = pcc_gc_scheduler_root_register_handle(ptr_add(slots, index * 8))
        if ptr_is_null(handle):
            _unroot(handles)
            return 0
        store_ptr(handles, index * 8, handle)
        index = index + 1
    return 1


def _lock_error(status: int):
    if status == -11:
        return py_exc_new(43, cstr("file lock would block"))
    if status == -13 or status == -1:
        return py_exc_new(36, cstr("file locking permission or lock conflict"))
    if status == -4:
        return py_exc_new(42, cstr("file locking interrupted"))
    return py_exc_new(14, cstr("file locking failed"))


def _wait_piece() -> int:
    pcc_thread_safepoint()
    if py_err_occurred() != 0:
        return 0
    pcc_platform_sleep_ns(5000000)
    pcc_thread_safepoint()
    return 1 if py_err_occurred() == 0 else 0


@c_abi_export("py_fcntl_getfl")
def py_fcntl_getfl(fd: int, keeper) -> int:
    """Read F_GETFL without changing descriptor state or its ownership.

    Ordinary Python performs integer conversions before this scalar boundary.
    The original file owner remains registered across syscall retries; only a
    scalar status crosses retirement, so no raw managed result can relocate.
    """
    slots = stack_alloc(24)
    handles = stack_alloc(24)
    store_ptr(slots, 0, keeper)
    store_ptr(slots, 8, null())
    store_ptr(slots, 16, null())
    if _root(slots, handles) == 0:
        return -12
    status: int = fd_control(fd, 3, 0)
    while status == -4:
        status = fd_control(fd, 3, 0)
    _unroot(handles)
    return status


@c_abi_export("py_fcntl_flock")
def py_fcntl_flock(fd_object, operation_object, keeper):
    slots = stack_alloc(24)
    handles = stack_alloc(24)
    store_ptr(slots, 0, fd_object)
    store_ptr(slots, 8, operation_object)
    store_ptr(slots, 16, keeper)
    if _root(slots, handles) == 0:
        return py_exc_new(19, cstr("could not root file-lock arguments"))
    fd: int = py_index_i64_checked(load_ptr(slots, 0))
    if py_err_occurred() != 0:
        _unroot(handles)
        return _error_value()
    if fd < 0:
        _unroot(handles)
        return py_exc_new(2, cstr("file descriptor cannot be negative"))
    if fd > 2147483647:
        _unroot(handles)
        return py_exc_new(15, cstr("file descriptor does not fit C int"))
    operation: int = py_index_i64_checked(load_ptr(slots, 8))
    if py_err_occurred() != 0:
        _unroot(handles)
        return _error_value()
    if operation < -2147483648 or operation > 2147483647:
        _unroot(handles)
        return py_exc_new(15, cstr("lock operation does not fit C int"))
    basic: int = operation & ~4
    if basic != 1 and basic != 2 and basic != 8:
        _unroot(handles)
        return _lock_error(-22)
    blocking: int = 1 if (operation & 4) == 0 and basic != 8 else 0
    attempt: int = basic | 4 if blocking != 0 else operation
    status: int = file_flock(fd, attempt)
    while status == -4 or (blocking != 0 and status == -11):
        if _wait_piece() == 0:
            _unroot(handles)
            return _error_value()
        status = file_flock(fd, attempt)
    _unroot(handles)
    if status != 0:
        return _lock_error(status)
    return global_load_ptr("py_None")


@c_abi_export("py_msvcrt_locking")
def py_msvcrt_locking(fd_object, mode_object, count_object):
    slots = stack_alloc(24)
    handles = stack_alloc(24)
    store_ptr(slots, 0, fd_object)
    store_ptr(slots, 8, mode_object)
    store_ptr(slots, 16, count_object)
    if _root(slots, handles) == 0:
        return py_exc_new(19, cstr("could not root file-lock arguments"))
    fd: int = py_index_i64_checked(load_ptr(slots, 0))
    if py_err_occurred() != 0:
        _unroot(handles)
        return _error_value()
    mode: int = py_index_i64_checked(load_ptr(slots, 8))
    if py_err_occurred() != 0:
        _unroot(handles)
        return _error_value()
    count: int = py_index_i64_checked(load_ptr(slots, 16))
    if py_err_occurred() != 0:
        _unroot(handles)
        return _error_value()
    if fd < -2147483648 or fd > 2147483647 or mode < -2147483648 or mode > 2147483647 or count < -2147483648 or count > 2147483647:
        _unroot(handles)
        return py_exc_new(15, cstr("locking argument does not fit Windows C int/long"))
    if fd < 0 or count < 0 or mode < 0 or mode > 4:
        _unroot(handles)
        return _lock_error(-22 if fd >= 0 else -9)
    offset: int = seek_file(fd, 0, 1)
    if offset < 0:
        _unroot(handles)
        return _lock_error(offset)
    # Windows CRT locks exclusively for both LCK and RLCK variants. Preserve
    # the original position across retries and leave the file pointer intact.
    operation: int = 0 if mode == 0 else 1
    status: int = file_lock_region(fd, operation, offset, count)
    tries: int = 1
    while status == -13 and (mode == 1 or mode == 3) and tries < 10:
        piece: int = 0
        while piece < 200:
            if _wait_piece() == 0:
                _unroot(handles)
                return _error_value()
            piece = piece + 1
        status = file_lock_region(fd, operation, offset, count)
        tries = tries + 1
    _unroot(handles)
    if status != 0:
        if status == -13 and (mode == 1 or mode == 3):
            return py_exc_new(14, cstr("file locking retry limit reached (EDEADLOCK)"))
        return _lock_error(status)
    return global_load_ptr("py_None")
