"""Freestanding, target-honest errno storage and message formatting.

Darwin enters the named libSystem ABI: ``__error`` is read directly before
any lazy loader work, then ``strerror_r`` is resolved from libSystem and writes
into caller-owned storage.  Linux keeps a pcc-owned native TLS errno and uses a
deterministic glibc C-locale table, so the object has no libc or loader import.

The raw syscall intrinsics continue to return negative errno values.  Wrappers
that promise libc-style errno can publish one through ``pcc_errno_set``; a
successful operation must not clear the prior value.
"""

from pcc import i64
from pcc.extern import c_abi_typed_export, c_ptr
from pcc.unsafe import (
    call_i32_i32_ptr_i64,
    cstr,
    darwin_errno_location,
    darwin_libsystem_symbol,
    define_thread_local_i32,
    global_addr,
    load_i32,
    load_i8,
    null,
    ptr_is_null,
    stack_alloc,
    store_i32,
    store_i8,
    target_sys_platform,
    unsigned_div_i64,
    unsigned_rem_i64,
)

__pcc_freestanding__ = True


define_thread_local_i32("pcc_errno_storage", 0)


@c_abi_typed_export("pcc_errno_location", "ptr", ())
def pcc_errno_location() -> c_ptr:
    host_slot = darwin_errno_location()
    if not ptr_is_null(host_slot):
        return host_slot
    return global_addr("pcc_errno_storage")


@c_abi_typed_export("pcc_errno_get", "i32", ())
def pcc_errno_get() -> i64:
    return load_i32(pcc_errno_location(), 0)


@c_abi_typed_export("pcc_errno_set", "void", ("i32",))
def pcc_errno_set(value: i64) -> None:
    store_i32(pcc_errno_location(), 0, value)


@c_abi_typed_export("pcc_errno_exception_kind", "i64", ("i32",))
def pcc_errno_exception_kind(value: i64) -> i64:
    """Map platform errno to the existing built-in OSError subclass tags.

    Non-Darwin targets use the owned Linux-style errno namespace, including
    Windows filesystem and socket wrappers (not raw Win32/WSA error codes).
    No allocation, Python callback or ambient host errno lookup occurs here.
    """
    platform = target_sys_platform()
    darwin: i64 = load_i8(platform, 0) == 100
    if value == 2:
        return 34
    if value == 17:
        return 35
    if value == 13 or value == 1:
        return 36
    if value == 21:
        return 37
    if value == 20:
        return 38
    if value == 3:
        return 39
    if value == 10:
        return 40
    if value == 4:
        return 42
    if value == 32:
        return 45
    if darwin:
        if value == 35 or value == 36 or value == 37:
            return 43
        if value == 60:
            return 41
        if value == 53:
            return 46
        if value == 61:
            return 47
        if value == 54:
            return 48
        if value == 58:
            return 45
    else:
        if value == 11 or value == 114 or value == 115:
            return 43
        if value == 110:
            return 41
        if value == 103:
            return 46
        if value == 111:
            return 47
        if value == 104:
            return 48
        if value == 108:
            return 45
    return 14


@c_abi_typed_export("pcc_errno_linux_c_locale_message", "ptr", ("i32",))
def _linux_c_locale_message(value: i64) -> c_ptr:
    # glibc Linux C-locale strerror messages for asm-generic errno 0..133.
    # 41 and 58 are intentional holes and use the numeric fallback below.
    if value == 0:
        return cstr("Success")
    if value == 1:
        return cstr("Operation not permitted")
    if value == 2:
        return cstr("No such file or directory")
    if value == 3:
        return cstr("No such process")
    if value == 4:
        return cstr("Interrupted system call")
    if value == 5:
        return cstr("Input/output error")
    if value == 6:
        return cstr("No such device or address")
    if value == 7:
        return cstr("Argument list too long")
    if value == 8:
        return cstr("Exec format error")
    if value == 9:
        return cstr("Bad file descriptor")
    if value == 10:
        return cstr("No child processes")
    if value == 11:
        return cstr("Resource temporarily unavailable")
    if value == 12:
        return cstr("Cannot allocate memory")
    if value == 13:
        return cstr("Permission denied")
    if value == 14:
        return cstr("Bad address")
    if value == 15:
        return cstr("Block device required")
    if value == 16:
        return cstr("Device or resource busy")
    if value == 17:
        return cstr("File exists")
    if value == 18:
        return cstr("Invalid cross-device link")
    if value == 19:
        return cstr("No such device")
    if value == 20:
        return cstr("Not a directory")
    if value == 21:
        return cstr("Is a directory")
    if value == 22:
        return cstr("Invalid argument")
    if value == 23:
        return cstr("Too many open files in system")
    if value == 24:
        return cstr("Too many open files")
    if value == 25:
        return cstr("Inappropriate ioctl for device")
    if value == 26:
        return cstr("Text file busy")
    if value == 27:
        return cstr("File too large")
    if value == 28:
        return cstr("No space left on device")
    if value == 29:
        return cstr("Illegal seek")
    if value == 30:
        return cstr("Read-only file system")
    if value == 31:
        return cstr("Too many links")
    if value == 32:
        return cstr("Broken pipe")
    if value == 33:
        return cstr("Numerical argument out of domain")
    if value == 34:
        return cstr("Numerical result out of range")
    if value == 35:
        return cstr("Resource deadlock avoided")
    if value == 36:
        return cstr("File name too long")
    if value == 37:
        return cstr("No locks available")
    if value == 38:
        return cstr("Function not implemented")
    if value == 39:
        return cstr("Directory not empty")
    if value == 40:
        return cstr("Too many levels of symbolic links")
    if value == 42:
        return cstr("No message of desired type")
    if value == 43:
        return cstr("Identifier removed")
    if value == 44:
        return cstr("Channel number out of range")
    if value == 45:
        return cstr("Level 2 not synchronized")
    if value == 46:
        return cstr("Level 3 halted")
    if value == 47:
        return cstr("Level 3 reset")
    if value == 48:
        return cstr("Link number out of range")
    if value == 49:
        return cstr("Protocol driver not attached")
    if value == 50:
        return cstr("No CSI structure available")
    if value == 51:
        return cstr("Level 2 halted")
    if value == 52:
        return cstr("Invalid exchange")
    if value == 53:
        return cstr("Invalid request descriptor")
    if value == 54:
        return cstr("Exchange full")
    if value == 55:
        return cstr("No anode")
    if value == 56:
        return cstr("Invalid request code")
    if value == 57:
        return cstr("Invalid slot")
    if value == 59:
        return cstr("Bad font file format")
    if value == 60:
        return cstr("Device not a stream")
    if value == 61:
        return cstr("No data available")
    if value == 62:
        return cstr("Timer expired")
    if value == 63:
        return cstr("Out of streams resources")
    if value == 64:
        return cstr("Machine is not on the network")
    if value == 65:
        return cstr("Package not installed")
    if value == 66:
        return cstr("Object is remote")
    if value == 67:
        return cstr("Link has been severed")
    if value == 68:
        return cstr("Advertise error")
    if value == 69:
        return cstr("Srmount error")
    if value == 70:
        return cstr("Communication error on send")
    if value == 71:
        return cstr("Protocol error")
    if value == 72:
        return cstr("Multihop attempted")
    if value == 73:
        return cstr("RFS specific error")
    if value == 74:
        return cstr("Bad message")
    if value == 75:
        return cstr("Value too large for defined data type")
    if value == 76:
        return cstr("Name not unique on network")
    if value == 77:
        return cstr("File descriptor in bad state")
    if value == 78:
        return cstr("Remote address changed")
    if value == 79:
        return cstr("Can not access a needed shared library")
    if value == 80:
        return cstr("Accessing a corrupted shared library")
    if value == 81:
        return cstr(".lib section in a.out corrupted")
    if value == 82:
        return cstr("Attempting to link in too many shared libraries")
    if value == 83:
        return cstr("Cannot exec a shared library directly")
    if value == 84:
        return cstr("Invalid or incomplete multibyte or wide character")
    if value == 85:
        return cstr("Interrupted system call should be restarted")
    if value == 86:
        return cstr("Streams pipe error")
    if value == 87:
        return cstr("Too many users")
    if value == 88:
        return cstr("Socket operation on non-socket")
    if value == 89:
        return cstr("Destination address required")
    if value == 90:
        return cstr("Message too long")
    if value == 91:
        return cstr("Protocol wrong type for socket")
    if value == 92:
        return cstr("Protocol not available")
    if value == 93:
        return cstr("Protocol not supported")
    if value == 94:
        return cstr("Socket type not supported")
    if value == 95:
        return cstr("Operation not supported")
    if value == 96:
        return cstr("Protocol family not supported")
    if value == 97:
        return cstr("Address family not supported by protocol")
    if value == 98:
        return cstr("Address already in use")
    if value == 99:
        return cstr("Cannot assign requested address")
    if value == 100:
        return cstr("Network is down")
    if value == 101:
        return cstr("Network is unreachable")
    if value == 102:
        return cstr("Network dropped connection on reset")
    if value == 103:
        return cstr("Software caused connection abort")
    if value == 104:
        return cstr("Connection reset by peer")
    if value == 105:
        return cstr("No buffer space available")
    if value == 106:
        return cstr("Transport endpoint is already connected")
    if value == 107:
        return cstr("Transport endpoint is not connected")
    if value == 108:
        return cstr("Cannot send after transport endpoint shutdown")
    if value == 109:
        return cstr("Too many references: cannot splice")
    if value == 110:
        return cstr("Connection timed out")
    if value == 111:
        return cstr("Connection refused")
    if value == 112:
        return cstr("Host is down")
    if value == 113:
        return cstr("No route to host")
    if value == 114:
        return cstr("Operation already in progress")
    if value == 115:
        return cstr("Operation now in progress")
    if value == 116:
        return cstr("Stale file handle")
    if value == 117:
        return cstr("Structure needs cleaning")
    if value == 118:
        return cstr("Not a XENIX named type file")
    if value == 119:
        return cstr("No XENIX semaphores available")
    if value == 120:
        return cstr("Is a named type file")
    if value == 121:
        return cstr("Remote I/O error")
    if value == 122:
        return cstr("Disk quota exceeded")
    if value == 123:
        return cstr("No medium found")
    if value == 124:
        return cstr("Wrong medium type")
    if value == 125:
        return cstr("Operation canceled")
    if value == 126:
        return cstr("Required key not available")
    if value == 127:
        return cstr("Key has expired")
    if value == 128:
        return cstr("Key has been revoked")
    if value == 129:
        return cstr("Key was rejected by service")
    if value == 130:
        return cstr("Owner died")
    if value == 131:
        return cstr("State not recoverable")
    if value == 132:
        return cstr("Operation not possible due to RF-kill")
    if value == 133:
        return cstr("Memory page has hardware error")
    return null()


@c_abi_typed_export("pcc_errno_copy_message", "i32", ("ptr", "i64", "ptr"))
def _copy_message(output, capacity: i64, message) -> i64:
    if ptr_is_null(output) or capacity <= 0:
        return -1
    index: i64 = 0
    while index + 1 < capacity:
        byte: i64 = load_i8(message, index)
        store_i8(output, index, byte)
        if byte == 0:
            return 0
        index += 1
    store_i8(output, capacity - 1, 0)
    return -1


@c_abi_typed_export("pcc_errno_write_unknown_error", "i32", ("ptr", "i64", "i32"))
def _write_unknown_error(output, capacity: i64, value: i64) -> i64:
    if ptr_is_null(output) or capacity <= 0:
        return -1
    prefix = cstr("Unknown error ")
    index: i64 = 0
    while load_i8(prefix, index) != 0 and index + 1 < capacity:
        store_i8(output, index, load_i8(prefix, index))
        index += 1

    number: i64 = value
    if number < 0:
        if index + 1 >= capacity:
            store_i8(output, capacity - 1, 0)
            return -1
        store_i8(output, index, 45)
        index += 1
        number = -number

    digits = stack_alloc(24)
    count: i64 = 0
    if number == 0:
        store_i8(digits, 0, 48)
        count: i64 = 1
    else:
        while number > 0 and count < 24:
            store_i8(digits, count, 48 + unsigned_rem_i64(number, 10))
            number = unsigned_div_i64(number, 10)
            count += 1
    while count > 0 and index + 1 < capacity:
        count -= 1
        store_i8(output, index, load_i8(digits, count))
        index += 1
    store_i8(output, index, 0)
    if count != 0:
        return -1
    return 0


@c_abi_typed_export("pcc_errno_message_into", "i32", ("i32", "ptr", "i64"))
def pcc_errno_message_into(value: i64, output, capacity: i64) -> i64:
    if ptr_is_null(output) or capacity <= 0:
        return -1

    # Snapshotting errno is the caller's responsibility.  This lookup is
    # intentionally after pcc_errno_get() in PyErr_SetFromErrno.
    host_strerror_r = darwin_libsystem_symbol(cstr("strerror_r"))
    if not ptr_is_null(host_strerror_r):
        result: i64 = call_i32_i32_ptr_i64(host_strerror_r, value, output, capacity)
        if result == 0:
            store_i8(output, capacity - 1, 0)
            return 0
        return _write_unknown_error(output, capacity, value)

    message = _linux_c_locale_message(value)
    if ptr_is_null(message):
        return _write_unknown_error(output, capacity, value)
    return _copy_message(output, capacity, message)
