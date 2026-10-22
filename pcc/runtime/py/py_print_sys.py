"""Phase 4c.10: pcc-Python port of py_print_sys.c.

sys.stdout.write / sys.stderr.write helpers with TextIOWrapper.write
semantics: a non-str argument raises TypeError, every UTF-8 byte is written
through the platform write() intrinsic (a failure raises OSError) and the
result is the number of characters written.

PyStrObject layout (from py_internal.h):
    offset  0   PyObjectHeader   (16 bytes)
    offset 16   byte_len         (i64)
    data lives inline at offset 40 — but we use py_str_utf8 to get
    the pointer rather than load_ptr + add, since the whole point
    of the runtime ABI is that py_str_utf8 is stable.
"""

__pcc_runtime_port__ = True

from pcc.extern import extern, c_abi_export, c_ptr, c_int32, c_int64, c_void
from pcc.unsafe import (
    cstr,
    load_i8,
    null,
    ptr_add,
    ptr_is_null,
    stack_alloc,
    store_i8,
    target_sys_platform,
)

py_decref            = extern("py_decref",            (c_ptr,),                   c_void)
py_str_check         = extern("py_str_check",         (c_ptr,),                   c_int64)
py_str_len           = extern("py_str_len",           (c_ptr,),                   c_int64)
py_str_utf8          = extern("py_str_utf8",          (c_ptr,),                   c_ptr)
py_str_byte_len      = extern("py_str_byte_len",      (c_ptr,),                   c_int64)
py_int_from_i64      = extern("py_int_from_i64",      (c_int64,),                 c_ptr)
py_obj_type_name     = extern("py_obj_type_name",     (c_ptr,),                   c_ptr)
py_exc_new           = extern("py_exc_new",           (c_int64, c_ptr),           c_ptr)
py_raise_owned       = extern("py_raise_owned",       (c_ptr,),                   c_void)
py_os_raise_errno    = extern("py_os_raise_errno",    (c_int64,),                 c_void)
pcc_errno_get        = extern("pcc_errno_get",        (),                         c_int32)
pcc_platform_write   = extern("pcc_platform_write",   (c_int64, c_ptr, c_int64),  c_int64)


def _write_type_error(text):
    # CPython: "write() argument must be str, not int".
    message = stack_alloc(192)
    prefix = cstr("write() argument must be str, not ")
    length: int = 0
    while load_i8(prefix, length) != 0:
        store_i8(message, length, load_i8(prefix, length))
        length = length + 1
    name = py_obj_type_name(text)
    if not ptr_is_null(name):
        raw = py_str_utf8(name)
        count: int = py_str_byte_len(name)
        if count > 120:
            count = 120
        index: int = 0
        while index < count:
            store_i8(message, length, load_i8(raw, index))
            length = length + 1
            index = index + 1
        py_decref(name)
    store_i8(message, length, 0)
    py_raise_owned(py_exc_new(3, message))
    return null()


def _write_failure_errno(result: int) -> int:
    # Darwin libSystem calls return -1 and publish errno; Linux raw syscalls
    # return the negated errno.
    if result == -1 and load_i8(target_sys_platform(), 0) == 100:
        return pcc_errno_get()
    return 0 - result


def _write_fd(fd: int, text):
    if py_str_check(text) == 0:
        return _write_type_error(text)
    characters: int = py_str_len(text)
    total: int = py_str_byte_len(text)
    done: int = 0
    while done < total:
        # Re-read the payload after every call: no object is held raw
        # across the system call.
        wrote: int = pcc_platform_write(fd, ptr_add(py_str_utf8(text), done), total - done)
        if wrote < 0:
            number: int = _write_failure_errno(wrote)
            if number != 4:
                # EINTR (4 on Darwin and Linux) retries; others raise OSError.
                py_os_raise_errno(number)
                return null()
        elif wrote == 0:
            py_os_raise_errno(5)
            return null()
        else:
            done = done + wrote
    return py_int_from_i64(characters)


@c_abi_export("py_sys_stdout_write")
def py_sys_stdout_write(text):
    return _write_fd(1, text)


@c_abi_export("py_sys_stderr_write")
def py_sys_stderr_write(text):
    return _write_fd(2, text)
