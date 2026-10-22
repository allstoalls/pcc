"""Finite freestanding stdio surface authored in pcc-Python.

This module grows one externally verified ABI slice at a time.  It is not a
claim of general POSIX ``FILE`` compatibility.
"""

from pcc import i64
from pcc.extern import (
    c_abi_export,
    c_abi_typed_export,
    c_abi_variadic_export,
    c_ptr,
    c_int32,
    c_void,
    extern,
)
from pcc.unsafe import (
    abi_constant,
    atomic_cas_i32,
    atomic_store_i32,
    close,
    cstr,
    define_global_i64_array,
    define_global_i32,
    define_global_ptr_null,
    define_global_ptr_to_global,
    free,
    f64_bits,
    f64_signbit,
    fd_control,
    global_addr,
    initial_environ,
    load_i32,
    load_i64,
    load_i8,
    load_ptr,
    logical_shift_right_i64,
    malloc,
    mul_overflow_i64,
    null,
    open_file,
    ptr_add,
    ptr_diff,
    ptr_is_null,
    read,
    seek_file,
    spawn_process_pipe,
    stack_alloc,
    store_i64,
    store_i8,
    store_ptr,
    target_sys_platform,
    unlinkat,
    unsigned_rem_i64,
    unsigned_div_i64,
    va_arg_f64,
    va_arg_i32,
    va_arg_i64,
    va_arg_ptr,
    va_arg_u32,
    va_cursor,
    va_end,
    va_start,
    waitpid,
    wrapping_mul_i64,
    write,
)

__pcc_freestanding__ = True

pcc_errno_set = extern("pcc_errno_set", (c_int32,), c_void)
pcc_errno_get = extern("pcc_errno_get", (), c_int32)

# Registry nodes are separate from the public 64-byte owned FILE layout:
# next@0, stream@8. The lock protects registry traversal and stream lifetime,
# not concurrent operations on a single FILE (that remains a separate scope).
define_global_ptr_null("pcc_stdio_registry_head")
define_global_i32("pcc_stdio_registry_lock", 0)


define_global_i64_array(
    "pcc_stdio_stdin_storage",
    abi_constant("stdio.file.magic"),
    0,
    abi_constant("stdio.flag.input_standard"),
    0,
    0,
    0,
    0,
    0,
)
define_global_i64_array(
    "pcc_stdio_stdout_storage",
    abi_constant("stdio.file.magic"),
    1,
    abi_constant("stdio.flag.output_standard"),
    0,
    0,
    0,
    0,
    0,
)
define_global_i64_array(
    "pcc_stdio_stderr_storage",
    abi_constant("stdio.file.magic"),
    2,
    abi_constant("stdio.flag.output_standard"),
    0,
    0,
    0,
    0,
    0,
)
define_global_ptr_to_global("__stdinp", "pcc_stdio_stdin_storage")
define_global_ptr_to_global("__stdoutp", "pcc_stdio_stdout_storage")
define_global_ptr_to_global("__stderrp", "pcc_stdio_stderr_storage")
define_global_ptr_to_global("stdin", "pcc_stdio_stdin_storage")
define_global_ptr_to_global("stdout", "pcc_stdio_stdout_storage")
define_global_ptr_to_global("stderr", "pcc_stdio_stderr_storage")


@c_abi_export("remove")
def remove(path) -> i64:
    # POSIX remove() first applies unlink semantics and, for an empty
    # directory, retries with AT_REMOVEDIR.  The unsafe boundary supplies the
    # target-specific AT_FDCWD value; this source owns only the portable rule.
    if unlinkat(path, 0) == 0:
        return 0
    if unlinkat(path, 1) == 0:
        return 0
    return -1


@c_abi_export("pcc_stdio_registry_acquire")
def _registry_acquire() -> None:
    lock = global_addr("pcc_stdio_registry_lock")
    while atomic_cas_i32(lock, 0, 0, 1, "acquire", "relaxed") != 0:
        pass


@c_abi_export("pcc_stdio_registry_release")
def _registry_release() -> None:
    atomic_store_i32(global_addr("pcc_stdio_registry_lock"), 0, 0, "release")


@c_abi_export("pcc_stdio_stream_unregister")
def _stream_unregister(stream) -> None:
    _registry_acquire()
    link = global_addr("pcc_stdio_registry_head")
    node = load_ptr(link, 0)
    while not ptr_is_null(node):
        if ptr_diff(load_ptr(node, 8), stream) == 0:
            store_ptr(link, 0, load_ptr(node, 0))
            _registry_release()
            free(node)
            return
        link = node
        node = load_ptr(node, 0)
    _registry_release()


@c_abi_export("pcc_stdio_stream_new")
def _stream_new(fd: i64, flags: i64, aux: i64):
    stream = malloc(abi_constant("stdio.file.size"))
    if ptr_is_null(stream):
        return null()
    buffer = null()
    buffer_capacity: i64 = 0
    if (flags & abi_constant("stdio.flag.writable")) != 0:
        buffer_capacity: i64 = 4096
        buffer = malloc(buffer_capacity)
        if ptr_is_null(buffer):
            free(stream)
            return null()
    store_i64(
        stream,
        abi_constant("stdio.file.magic_offset"),
        abi_constant("stdio.file.magic"),
    )
    store_i64(stream, abi_constant("stdio.file.fd_offset"), fd)
    store_i64(stream, abi_constant("stdio.file.flags_offset"), flags)
    store_i64(stream, abi_constant("stdio.file.aux_offset"), aux)
    store_ptr(stream, abi_constant("stdio.file.buffer_offset"), buffer)
    store_i64(
        stream,
        abi_constant("stdio.file.buffer_capacity_offset"),
        buffer_capacity,
    )
    store_i64(stream, abi_constant("stdio.file.buffer_length_offset"), 0)
    store_i64(stream, abi_constant("stdio.file.buffer_position_offset"), 0)
    node = malloc(16)
    if ptr_is_null(node):
        if not ptr_is_null(buffer):
            free(buffer)
        free(stream)
        return null()
    store_ptr(node, 8, stream)
    _registry_acquire()
    store_ptr(node, 0, load_ptr(global_addr("pcc_stdio_registry_head"), 0))
    store_ptr(global_addr("pcc_stdio_registry_head"), 0, node)
    _registry_release()
    return stream


@c_abi_export("fopen")
def fopen(path, mode) -> c_ptr:
    if ptr_is_null(path) or ptr_is_null(mode):
        pcc_errno_set(22)
        return null()
    first = load_i8(mode, 0)
    access: i64 = 0
    disposition: i64 = 0
    flags: i64 = 0
    if first == 114:
        flags = abi_constant("stdio.flag.readable")
    elif first == 119:
        access: i64 = 1
        disposition: i64 = 1
        flags = abi_constant("stdio.flag.writable")
    elif first == 97:
        access: i64 = 1
        disposition: i64 = 2
        flags = abi_constant("stdio.flag.writable")
    elif first == 120:
        access: i64 = 1
        disposition: i64 = 3
        flags = abi_constant("stdio.flag.writable")
    else:
        pcc_errno_set(22)
        return null()

    offset: i64 = 1
    while load_i8(mode, offset) != 0:
        marker = load_i8(mode, offset)
        if marker == 43:
            access: i64 = 2
            flags = abi_constant("stdio.flag.readable") | abi_constant(
                "stdio.flag.writable"
            )
        elif marker != 98 and marker != 116:
            pcc_errno_set(22)
            return null()
        offset = offset + 1
    if first == 97:
        flags = flags | abi_constant("stdio.flag.append")

    fd = open_file(path, access, disposition)
    if fd < 0:
        # The owned syscall ABI returns -errno. Linux does not update the
        # separate libc-style TLS slot; publish the captured failure here.
        pcc_errno_set(0 - fd)
        return null()
    append_position: i64 = 0
    if (flags & abi_constant("stdio.flag.append")) != 0:
        # C append streams start with their logical position at EOF.  Later
        # fseek calls may move that logical position even though the eventual
        # write is still forced to EOF by O_APPEND.
        append_position = seek_file(fd, 0, 2)
        if append_position < 0:
            # Darwin seek_file exposes libSystem's -1/errno ABI; Linux
            # exposes the raw -errno result. Capture before close can alter it.
            saved_errno: i64 = 0 - append_position
            if load_i8(target_sys_platform(), 0) == 100:
                saved_errno = pcc_errno_get()
            close(fd)
            # Cleanup must not replace the operation's captured error.
            pcc_errno_set(saved_errno)
            return null()
    stream = _stream_new(fd, flags, 0)
    if ptr_is_null(stream):
        close(fd)
        pcc_errno_set(12)
        return null()
    if (flags & abi_constant("stdio.flag.append")) != 0:
        store_i64(
            stream,
            abi_constant("stdio.file.buffer_position_offset"),
            append_position,
        )
    return stream


@c_abi_export("pcc_stdio_fdopen")
def pcc_stdio_fdopen(fd: i64, mode, closefd: i64, buffering: i64):
    """Adopt an existing descriptor without opening or truncating its path."""
    if fd < 0 or ptr_is_null(mode):
        pcc_errno_set(9 if fd < 0 else 22)
        return null()
    first: i64 = load_i8(mode, 0)
    flags: i64 = 0
    if first == 114:
        flags = abi_constant("stdio.flag.readable")
    elif first == 119 or first == 97 or first == 120:
        flags = abi_constant("stdio.flag.writable")
    else:
        pcc_errno_set(22)
        return null()
    offset: i64 = 1
    while load_i8(mode, offset) != 0:
        marker: i64 = load_i8(mode, offset)
        if marker == 43:
            flags = abi_constant("stdio.flag.readable") | abi_constant("stdio.flag.writable")
        elif marker != 98 and marker != 116:
            pcc_errno_set(22)
            return null()
        offset = offset + 1
    descriptor_flags: i64 = fd_control(fd, 3, 0)  # F_GETFL on supported POSIX targets.
    if descriptor_flags < 0:
        pcc_errno_set(0 - descriptor_flags)
        return null()
    descriptor_access: i64 = descriptor_flags & 3
    if ((flags & abi_constant("stdio.flag.readable")) != 0 and descriptor_access == 1
            or (flags & abi_constant("stdio.flag.writable")) != 0 and descriptor_access == 0):
        pcc_errno_set(22)
        return null()
    if first == 97:
        flags = flags | abi_constant("stdio.flag.append")
    if closefd == 0:
        flags = flags | abi_constant("stdio.flag.borrowed_descriptor")
    stream = _stream_new(fd, flags, 0)
    if ptr_is_null(stream):
        pcc_errno_set(12)
        return null()  # Ownership has not transferred on failure.
    if buffering == 0:
        _stream_release_buffer(stream)
        store_i64(stream, abi_constant("stdio.file.buffer_capacity_offset"), 0)
    elif buffering > 1 and (flags & abi_constant("stdio.flag.writable")) != 0:
        buffer = malloc(buffering)
        if ptr_is_null(buffer):
            _stream_unregister(stream)
            _stream_release_buffer(stream)
            free(stream)
            pcc_errno_set(12)
            return null()
        _stream_release_buffer(stream)
        store_ptr(stream, abi_constant("stdio.file.buffer_offset"), buffer)
        store_i64(stream, abi_constant("stdio.file.buffer_capacity_offset"), buffering)
    # Descriptor flags/position change only after every allocation succeeds.
    # A failed adoption leaves both descriptor ownership and state untouched.
    if first == 97:
        append_flag: i64 = 8 if load_i8(target_sys_platform(), 0) == 100 else 1024
        changed: i64 = fd_control(fd, 4, descriptor_flags | append_flag)
        if changed < 0:
            _stream_unregister(stream)
            _stream_release_buffer(stream)
            free(stream)
            pcc_errno_set(0 - changed)
            return null()
    position: i64 = seek_file(fd, 0, 1)
    if first == 97:
        position = seek_file(fd, 0, 2)
    if position >= 0:
        store_i64(stream, abi_constant("stdio.file.buffer_position_offset"), position)
    return stream


@c_abi_typed_export("fileno", "i32", ("ptr",))
def fileno(stream) -> i64:
    if ptr_is_null(stream) or load_i64(
        stream, abi_constant("stdio.file.magic_offset")
    ) != abi_constant("stdio.file.magic"):
        return -1
    return load_i64(stream, abi_constant("stdio.file.fd_offset"))


@c_abi_export("pcc_stdio_flush_output")
def _flush_output(stream) -> i64:
    flags = load_i64(stream, abi_constant("stdio.file.flags_offset"))
    buffer = load_ptr(stream, abi_constant("stdio.file.buffer_offset"))
    length = load_i64(
        stream, abi_constant("stdio.file.buffer_length_offset")
    )
    if ptr_is_null(buffer) or length <= 0:
        return 0
    offset: i64 = 0
    fd = load_i64(stream, abi_constant("stdio.file.fd_offset"))
    while offset < length:
        written = write(fd, ptr_add(buffer, offset), length - offset)
        while written == -4:
            written = write(fd, ptr_add(buffer, offset), length - offset)
        if written <= 0:
            if load_i8(target_sys_platform(), 0) == 108:
                pcc_errno_set(0 - written if written < 0 else 5)
            remaining = length - offset
            move_index: i64 = 0
            while move_index < remaining:
                store_i8(
                    buffer,
                    move_index,
                    load_i8(buffer, offset + move_index),
                )
                move_index = move_index + 1
            store_i64(
                stream,
                abi_constant("stdio.file.buffer_length_offset"),
                remaining,
            )
            if (flags & abi_constant("stdio.flag.append")) != 0:
                # The bytes before ``offset`` were committed through O_APPEND.
                # The shifted remainder now starts at the fd's resulting
                # position, not at the EOF captured when buffering began.
                store_i64(
                    stream,
                    abi_constant("stdio.file.buffer_position_offset"),
                    seek_file(fd, 0, 1),
                )
            store_i64(
                stream,
                abi_constant("stdio.file.flags_offset"),
                flags | abi_constant("stdio.flag.error"),
            )
            return -1
        offset = offset + written
    store_i64(stream, abi_constant("stdio.file.buffer_length_offset"), 0)
    if (flags & abi_constant("stdio.flag.append")) != 0:
        # A successful O_APPEND write advances the stream's logical position
        # to the committed EOF.  The next buffered write starts from here
        # unless an intervening seek/read changes it.
        store_i64(
            stream,
            abi_constant("stdio.file.buffer_position_offset"),
            seek_file(fd, 0, 1),
        )
    return 0


@c_abi_export("pcc_stdio_stream_release_buffer")
def _stream_release_buffer(stream) -> None:
    buffer = load_ptr(stream, abi_constant("stdio.file.buffer_offset"))
    if not ptr_is_null(buffer):
        free(buffer)
        store_ptr(
            stream,
            abi_constant("stdio.file.buffer_offset"),
            null(),
        )


@c_abi_export("fwrite")
def fwrite(input_buffer, size: i64, count: i64, stream) -> i64:
    if ptr_is_null(stream) or load_i64(
        stream, abi_constant("stdio.file.magic_offset")
    ) != abi_constant("stdio.file.magic"):
        return 0
    flags = load_i64(stream, abi_constant("stdio.file.flags_offset"))
    if (flags & abi_constant("stdio.flag.writable")) == 0:
        store_i64(
            stream,
            abi_constant("stdio.file.flags_offset"),
            flags | abi_constant("stdio.flag.error"),
        )
        return 0
    if size <= 0 or count <= 0:
        return 0
    if mul_overflow_i64(size, count):
        store_i64(
            stream,
            abi_constant("stdio.file.flags_offset"),
            flags | abi_constant("stdio.flag.error"),
        )
        return 0
    total = wrapping_mul_i64(size, count)
    output_buffer = load_ptr(
        stream, abi_constant("stdio.file.buffer_offset")
    )
    capacity = load_i64(
        stream, abi_constant("stdio.file.buffer_capacity_offset")
    )
    consumed: i64 = 0
    if ptr_is_null(output_buffer) or capacity <= 0:
        fd = load_i64(stream, abi_constant("stdio.file.fd_offset"))
        while consumed < total:
            written = write(
                fd, ptr_add(input_buffer, consumed), total - consumed
            )
            while written == -4:
                written = write(
                    fd, ptr_add(input_buffer, consumed), total - consumed
                )
            if written <= 0:
                if load_i8(target_sys_platform(), 0) == 108:
                    pcc_errno_set(0 - written if written < 0 else 5)
                store_i64(
                    stream,
                    abi_constant("stdio.file.flags_offset"),
                    flags | abi_constant("stdio.flag.error"),
                )
                return unsigned_div_i64(consumed, size)
            consumed = consumed + written
        return count

    while consumed < total:
        buffered = load_i64(
            stream, abi_constant("stdio.file.buffer_length_offset")
        )
        if buffered >= capacity:
            if _flush_output(stream) != 0:
                return unsigned_div_i64(consumed, size)
            buffered: i64 = 0
        available = capacity - buffered
        chunk = total - consumed
        if chunk > available:
            chunk = available
        copy_index: i64 = 0
        while copy_index < chunk:
            store_i8(
                output_buffer,
                buffered + copy_index,
                load_i8(input_buffer, consumed + copy_index),
            )
            copy_index = copy_index + 1
        buffered = buffered + chunk
        consumed = consumed + chunk
        store_i64(
            stream,
            abi_constant("stdio.file.buffer_length_offset"),
            buffered,
        )
        if buffered >= capacity and _flush_output(stream) != 0:
            return unsigned_div_i64(consumed, size)
    return count


@c_abi_typed_export("puts", "i32", ("ptr",))
def puts(text) -> i64:
    # stdout is an owned FILE. Reuse fwrite's short-write/EINTR handling and
    # error flag; successful puts returns a nonnegative value, not a length.
    stream = load_ptr(global_addr("stdout"), 0)
    if load_i8(target_sys_platform(), 0) == 100:
        stream = load_ptr(global_addr("__stdoutp"), 0)
    length: i64 = 0
    while load_i8(text, length) != 0:
        length = length + 1
    if fwrite(text, 1, length, stream) != length:
        return -1
    if fwrite(cstr("\n"), 1, 1, stream) != 1:
        return -1
    return 0


@c_abi_export("fread")
def fread(buffer, size: i64, count: i64, stream) -> i64:
    if ptr_is_null(stream) or load_i64(
        stream, abi_constant("stdio.file.magic_offset")
    ) != abi_constant("stdio.file.magic"):
        return 0
    flags = load_i64(stream, abi_constant("stdio.file.flags_offset"))
    if (flags & abi_constant("stdio.flag.readable")) == 0:
        store_i64(
            stream,
            abi_constant("stdio.file.flags_offset"),
            flags | abi_constant("stdio.flag.error"),
        )
        return 0
    if size <= 0 or count <= 0:
        return 0
    if mul_overflow_i64(size, count):
        store_i64(
            stream,
            abi_constant("stdio.file.flags_offset"),
            flags | abi_constant("stdio.flag.error"),
        )
        return 0
    total = wrapping_mul_i64(size, count)
    received = read(
        load_i64(stream, abi_constant("stdio.file.fd_offset")), buffer, total
    )
    if received < 0:
        store_i64(
            stream,
            abi_constant("stdio.file.flags_offset"),
            flags | abi_constant("stdio.flag.error"),
        )
        return 0
    if received == 0:
        store_i64(
            stream,
            abi_constant("stdio.file.flags_offset"),
            flags | abi_constant("stdio.flag.eof"),
        )
    if (flags & abi_constant("stdio.flag.append")) != 0:
        # On an update stream a read advances the logical position used by a
        # later, standards-compliant write transition.
        store_i64(
            stream,
            abi_constant("stdio.file.buffer_position_offset"),
            seek_file(
                load_i64(stream, abi_constant("stdio.file.fd_offset")),
                0,
                1,
            ),
        )
    return unsigned_div_i64(received, size)


@c_abi_export("fgetc")
def fgetc(stream) -> i64:
    byte = stack_alloc(1)
    if fread(byte, 1, 1, stream) != 1:
        return -1
    return load_i8(byte, 0) & 255


@c_abi_typed_export("getc", "i32", ("ptr",))
def getc(stream) -> i64:
    return fgetc(stream)


@c_abi_typed_export("fgets", "ptr", ("ptr", "i32", "ptr"))
def fgets(output, count: i64, stream) -> c_ptr:
    if count <= 0:
        return null()
    index: i64 = 0
    while index < count - 1:
        byte = fgetc(stream)
        if byte < 0:
            # EOF after some input yields the partial line. Read errors and
            # EOF before any input return NULL; the latter leaves output alone.
            if index == 0 or ferror(stream) != 0:
                return null()
            break
        store_i8(output, index, byte)
        index = index + 1
        if byte == 10:
            break
    store_i8(output, index, 0)
    return output


@c_abi_typed_export("fseek", "i32", ("ptr", "i64", "i32"))
def fseek(stream, offset: i64, whence: i64) -> i64:
    if ptr_is_null(stream) or load_i64(
        stream, abi_constant("stdio.file.magic_offset")
    ) != abi_constant("stdio.file.magic"):
        return -1
    flags = load_i64(stream, abi_constant("stdio.file.flags_offset"))
    if whence < 0 or whence > 2:
        store_i64(
            stream,
            abi_constant("stdio.file.flags_offset"),
            flags | abi_constant("stdio.flag.error"),
        )
        return -1
    append_seek_position: i64 = -1
    if (
        whence == 1
        and (flags & abi_constant("stdio.flag.append")) != 0
    ):
        buffered = load_i64(
            stream, abi_constant("stdio.file.buffer_length_offset")
        )
        if buffered > 0:
            append_seek_position = load_i64(
                stream,
                abi_constant("stdio.file.buffer_position_offset"),
            ) + buffered
        else:
            append_seek_position = seek_file(
                load_i64(stream, abi_constant("stdio.file.fd_offset")),
                0,
                1,
            )
        if append_seek_position < 0:
            store_i64(
                stream,
                abi_constant("stdio.file.flags_offset"),
                flags | abi_constant("stdio.flag.error"),
            )
            return -1
    if (
        (flags & abi_constant("stdio.flag.writable")) != 0
        and _flush_output(stream) != 0
    ):
        return -1
    if append_seek_position >= 0:
        # Flushing an append stream moves the kernel descriptor to physical
        # EOF.  C stdio nevertheless defines SEEK_CUR from the stream's
        # pre-flush logical position, so preserve that position explicitly.
        position = seek_file(
            load_i64(stream, abi_constant("stdio.file.fd_offset")),
            append_seek_position + offset,
            0,
        )
    else:
        position = seek_file(
            load_i64(stream, abi_constant("stdio.file.fd_offset")),
            offset,
            whence,
        )
    if position < 0:
        store_i64(
            stream,
            abi_constant("stdio.file.flags_offset"),
            flags | abi_constant("stdio.flag.error"),
        )
        return -1
    store_i64(
        stream,
        abi_constant("stdio.file.flags_offset"),
        flags & ~abi_constant("stdio.flag.eof"),
    )
    store_i64(
        stream,
        abi_constant("stdio.file.buffer_position_offset"),
        position,
    )
    return 0


@c_abi_typed_export("ftell", "i64", ("ptr",))
def ftell(stream) -> i64:
    if ptr_is_null(stream) or load_i64(
        stream, abi_constant("stdio.file.magic_offset")
    ) != abi_constant("stdio.file.magic"):
        return -1
    flags = load_i64(stream, abi_constant("stdio.file.flags_offset"))
    buffered: i64 = 0
    if (flags & abi_constant("stdio.flag.writable")) != 0:
        buffered = load_i64(
            stream, abi_constant("stdio.file.buffer_length_offset")
        )
        if (
            buffered > 0
            and (flags & abi_constant("stdio.flag.append")) != 0
        ):
            position = load_i64(
                stream,
                abi_constant("stdio.file.buffer_position_offset"),
            )
            if position < 0:
                store_i64(
                    stream,
                    abi_constant("stdio.file.flags_offset"),
                    flags | abi_constant("stdio.flag.error"),
                )
                return -1
            return position + buffered
    position = seek_file(
        load_i64(stream, abi_constant("stdio.file.fd_offset")), 0, 1
    )
    if position < 0:
        store_i64(
            stream,
            abi_constant("stdio.file.flags_offset"),
            flags | abi_constant("stdio.flag.error"),
        )
        return -1
    if buffered > 0:
        position = position + buffered
    return position


@c_abi_typed_export("fflush", "i32", ("ptr",))
def fflush(stream) -> i64:
    if ptr_is_null(stream):
        # Flush all owned output streams, retaining EOF if any one fails.
        result: i64 = 0
        standard_output = load_ptr(global_addr("stdout"), 0)
        standard_error = load_ptr(global_addr("stderr"), 0)
        if load_i8(target_sys_platform(), 0) == 100:
            standard_output = load_ptr(global_addr("__stdoutp"), 0)
            standard_error = load_ptr(global_addr("__stderrp"), 0)
        if _flush_output(standard_output) != 0:
            result = -1
        if _flush_output(standard_error) != 0:
            result = -1
        _registry_acquire()
        node = load_ptr(global_addr("pcc_stdio_registry_head"), 0)
        while not ptr_is_null(node):
            registered = load_ptr(node, 8)
            flags: i64 = load_i64(registered, abi_constant("stdio.file.flags_offset"))
            if (flags & abi_constant("stdio.flag.writable")) != 0:
                if _flush_output(registered) != 0:
                    result = -1
            node = load_ptr(node, 0)
        _registry_release()
        return result
    if load_i64(
        stream, abi_constant("stdio.file.magic_offset")
    ) != abi_constant("stdio.file.magic"):
        return -1
    flags = load_i64(stream, abi_constant("stdio.file.flags_offset"))
    if (flags & abi_constant("stdio.flag.writable")) != 0:
        return _flush_output(stream)
    return 0


@c_abi_export("ferror")
def ferror(stream) -> i64:
    if ptr_is_null(stream) or load_i64(
        stream, abi_constant("stdio.file.magic_offset")
    ) != abi_constant("stdio.file.magic"):
        return 1
    if (
        load_i64(stream, abi_constant("stdio.file.flags_offset"))
        & abi_constant("stdio.flag.error")
    ) != 0:
        return 1
    return 0


@c_abi_export("fclose")
def fclose(stream) -> i64:
    if ptr_is_null(stream) or load_i64(
        stream, abi_constant("stdio.file.magic_offset")
    ) != abi_constant("stdio.file.magic"):
        return -1
    flags = load_i64(stream, abi_constant("stdio.file.flags_offset"))
    if (flags & abi_constant("stdio.flag.standard")) != 0:
        return fflush(stream)
    _stream_unregister(stream)
    flush_result: i64 = 0
    if (flags & abi_constant("stdio.flag.writable")) != 0:
        flush_result = _flush_output(stream)
    rc: i64 = 0
    if (flags & abi_constant("stdio.flag.borrowed_descriptor")) == 0:
        rc = close(load_i64(stream, abi_constant("stdio.file.fd_offset")))
    _stream_release_buffer(stream)
    store_i64(stream, abi_constant("stdio.file.magic_offset"), 0)
    free(stream)
    if flush_result != 0 or rc != 0:
        return -1
    return 0


@c_abi_export("pcc_stdio_format_emit_char")
def _format_emit_char(
    output,
    capacity: i64,
    stream,
    index: i64,
    byte: i64,
) -> i64:
    if not ptr_is_null(stream):
        one = stack_alloc(1)
        store_i8(one, 0, byte)
        if fwrite(one, 1, 1, stream) != 1:
            return -1
    elif capacity > 0 and index < capacity - 1 and not ptr_is_null(output):
        store_i8(output, index, byte)
    return index + 1


@c_abi_export("pcc_stdio_format_emit_cstr")
def _format_emit_cstr(
    output,
    capacity: i64,
    stream,
    index: i64,
    value,
    limit: i64,
) -> i64:
    if ptr_is_null(value):
        value = cstr("(null)")
    offset: i64 = 0
    while load_i8(value, offset) != 0 and (limit < 0 or offset < limit):
        index = _format_emit_char(
            output, capacity, stream, index, load_i8(value, offset)
        )
        if index < 0:
            return -1
        offset = offset + 1
    return index


@c_abi_export("pcc_stdio_format_cstr_length")
def _format_cstr_length(value, limit: i64) -> i64:
    if ptr_is_null(value):
        value = cstr("(null)")
    length: i64 = 0
    while load_i8(value, length) != 0 and (limit < 0 or length < limit):
        length = length + 1
    return length


@c_abi_export("pcc_stdio_format_emit_repeat")
def _format_emit_repeat(
    output,
    capacity: i64,
    stream,
    index: i64,
    byte: i64,
    count: i64,
) -> i64:
    while count > 0:
        index = _format_emit_char(output, capacity, stream, index, byte)
        if index < 0:
            return -1
        count = count - 1
    return index


@c_abi_export("pcc_stdio_format_unsigned_digits")
def _format_unsigned_digits(value: i64, base: i64) -> i64:
    count: i64 = 1
    while unsigned_div_i64(value, base) != 0:
        count = count + 1
        value = unsigned_div_i64(value, base)
    return count


@c_abi_export("pcc_stdio_format_emit_unsigned")
def _format_emit_unsigned(
    output,
    capacity: i64,
    stream,
    index: i64,
    value: i64,
    base: i64,
    uppercase: i64,
) -> i64:
    digits = stack_alloc(66)
    count: i64 = 0
    if value == 0:
        store_i8(digits, 0, 48)
        count: i64 = 1
    else:
        while value != 0:
            digit = unsigned_rem_i64(value, base)
            if digit < 10:
                byte = 48 + digit
            elif uppercase != 0:
                byte = 55 + digit
            else:
                byte = 87 + digit
            store_i8(digits, count, byte)
            count = count + 1
            value = unsigned_div_i64(value, base)
    while count > 0:
        count = count - 1
        index = _format_emit_char(
            output, capacity, stream, index, load_i8(digits, count)
        )
        if index < 0:
            return -1
    return index


@c_abi_export("pcc_stdio_format_emit_signed")
def _format_emit_signed(
    output,
    capacity: i64,
    stream,
    index: i64,
    value: i64,
) -> i64:
    magnitude = value
    if value < 0:
        index = _format_emit_char(output, capacity, stream, index, 45)
        if index < 0:
            return -1
        magnitude = wrapping_mul_i64(value, -1)
    return _format_emit_unsigned(
        output, capacity, stream, index, magnitude, 10, 0
    )


@c_abi_export("pcc_stdio_format_emit_integer")
def _format_emit_integer(
    output,
    capacity: i64,
    stream,
    index: i64,
    value: i64,
    base: i64,
    uppercase: i64,
    signed: i64,
    width: i64,
    precision: i64,
    left: i64,
    zero_pad: i64,
    plus: i64,
    space: i64,
    prefix_kind: i64,
) -> i64:
    magnitude = value
    sign: i64 = 0
    if signed != 0:
        if value < 0:
            sign: i64 = 45
            magnitude = wrapping_mul_i64(value, -1)
        elif plus != 0:
            sign: i64 = 43
        elif space != 0:
            sign: i64 = 32

    prefix_first: i64 = 0
    prefix_second: i64 = 0
    if prefix_kind == 2:
        prefix_first: i64 = 48
        prefix_second = 88 if uppercase != 0 else 120
    elif prefix_kind == 1:
        if base == 16 and magnitude != 0:
            prefix_first: i64 = 48
            prefix_second = 88 if uppercase != 0 else 120
        elif base == 8 and (magnitude != 0 or precision == 0):
            prefix_first: i64 = 48

    digit_count = _format_unsigned_digits(magnitude, base)
    if magnitude == 0 and precision == 0:
        digit_count: i64 = 0
    zero_count: i64 = 0
    if precision > digit_count:
        zero_count = precision - digit_count
    sign_count: i64 = 1 if sign != 0 else 0
    prefix_count: i64 = 0
    if prefix_first != 0:
        prefix_count: i64 = 1
    if prefix_second != 0:
        prefix_count: i64 = 2
    if zero_pad != 0 and left == 0 and precision < 0:
        requested_zeroes = width - sign_count - prefix_count - digit_count
        if requested_zeroes > zero_count:
            zero_count = requested_zeroes
    spaces = width - sign_count - prefix_count - zero_count - digit_count
    if spaces < 0:
        spaces: i64 = 0

    if left == 0:
        index = _format_emit_repeat(
            output, capacity, stream, index, 32, spaces
        )
    if index < 0:
        return -1
    if sign != 0:
        index = _format_emit_char(output, capacity, stream, index, sign)
    if index < 0:
        return -1
    if prefix_first != 0:
        index = _format_emit_char(
            output, capacity, stream, index, prefix_first
        )
    if index < 0:
        return -1
    if prefix_second != 0:
        index = _format_emit_char(
            output, capacity, stream, index, prefix_second
        )
    if index < 0:
        return -1
    index = _format_emit_repeat(
        output, capacity, stream, index, 48, zero_count
    )
    if index < 0:
        return -1
    if digit_count > 0:
        index = _format_emit_unsigned(
            output, capacity, stream, index, magnitude, base, uppercase
        )
    if index < 0:
        return -1
    if left != 0:
        index = _format_emit_repeat(
            output, capacity, stream, index, 32, spaces
        )
    return index


@c_abi_export("pcc_stdio_format_raw_unsigned")
def _format_raw_unsigned(
    output,
    position: i64,
    value: i64,
    minimum_digits: i64,
) -> i64:
    digits = stack_alloc(32)
    count: i64 = 0
    if value == 0:
        store_i8(digits, 0, 48)
        count: i64 = 1
    else:
        while value != 0:
            store_i8(
                digits,
                count,
                48 + unsigned_rem_i64(value, 10),
            )
            count = count + 1
            value = unsigned_div_i64(value, 10)
    while count < minimum_digits:
        store_i8(output, position, 48)
        position = position + 1
        minimum_digits = minimum_digits - 1
    while count > 0:
        count = count - 1
        store_i8(output, position, load_i8(digits, count))
        position = position + 1
    return position


@c_abi_export("pcc_stdio_float_raw_capacity")
def _float_raw_capacity(precision: i64) -> i64:
    # Bytes ``pcc_stdio_format_float_raw`` may write for ``precision``: a sign,
    # up to 309 integer digits, '.', the fraction digits, an exponent of at
    # most five bytes ("e+308") and the NUL, with slack.  Callers size their
    # output buffer with this; precision is no longer clamped.
    if precision < 6:
        precision: i64 = 6
    return precision + 336


@c_abi_export("pcc_stdio_limbs_mul_small")
def _limbs_mul_small(limbs, count: i64, factor: i64) -> i64:
    # ``limbs`` is a little-endian base-1e9 natural number of ``count`` limbs.
    # Multiply it in place by ``factor`` (< 2**31) and return the new count;
    # limb * factor + carry < 1e9 * 2**31 + 2**31 fits in an i64.
    carry: i64 = 0
    index: i64 = 0
    while index < count:
        product = load_i64(limbs, index * 8) * factor + carry
        store_i64(limbs, index * 8, unsigned_rem_i64(product, 1000000000))
        carry = unsigned_div_i64(product, 1000000000)
        index = index + 1
    while carry != 0:
        store_i64(limbs, count * 8, unsigned_rem_i64(carry, 1000000000))
        carry = unsigned_div_i64(carry, 1000000000)
        count = count + 1
    return count


@c_abi_export("pcc_stdio_limb_digits")
def _limb_digits(digits, count: i64, limb: i64, width: i64) -> i64:
    # Append the decimal digit values of ``limb`` to ``digits`` at ``count``:
    # exactly ``width`` digits with leading zeros, or as many as the limb
    # needs when ``width`` is 0.
    scratch = stack_alloc(9)
    used: i64 = 0
    while limb != 0 or used < width:
        store_i8(scratch, used, unsigned_rem_i64(limb, 10))
        limb = unsigned_div_i64(limb, 10)
        used = used + 1
    while used > 0:
        used = used - 1
        store_i8(digits, count, load_i8(scratch, used))
        count = count + 1
    return count


@c_abi_export("pcc_stdio_float_exact_digits")
def _float_exact_digits(bits: i64, digits, point_out) -> i64:
    # Exact decimal expansion of the finite, positive double whose bit
    # pattern is ``bits``.  Writes digit values 0..9 to ``digits`` (at most
    # 770 of them) with no leading or trailing zeros, stores the decimal point
    # position -- the number of integer digits, possibly <= 0 -- at
    # ``point_out``, and returns the digit count.  A double is m * 2**e
    # exactly: for e >= 0 that is the integer m << e, and for e < 0 it is
    # (m * 5**-e) / 10**-e.  Both are big-integer digit strings, so rounding
    # them needs no floating-point arithmetic.  The old generator repeatedly
    # multiplied by 0.1 and 10.0 and printed 2.0**60 as 1152921504606848169.
    exponent_bits = logical_shift_right_i64(bits, 52) & 2047
    mantissa = bits & 4503599627370495
    binary_exponent: i64 = -1074
    if exponent_bits != 0:
        mantissa = mantissa | 4503599627370496
        binary_exponent = exponent_bits - 1075
    # m * 5**1074 has 767 decimal digits: 86 limbs, and m << 971 needs 35.
    limbs = stack_alloc(800)
    store_i64(limbs, 0, unsigned_rem_i64(mantissa, 1000000000))
    store_i64(limbs, 8, unsigned_div_i64(mantissa, 1000000000))
    count: i64 = 2
    if load_i64(limbs, 8) == 0:
        count: i64 = 1
    shift: i64 = 0
    if binary_exponent >= 0:
        remaining = binary_exponent
        while remaining >= 28:
            count = _limbs_mul_small(limbs, count, 268435456)
            remaining = remaining - 28
        if remaining > 0:
            count = _limbs_mul_small(limbs, count, 1 << remaining)
    else:
        remaining = 0 - binary_exponent
        shift = remaining
        while remaining >= 13:
            count = _limbs_mul_small(limbs, count, 1220703125)
            remaining = remaining - 13
        factor: i64 = 1
        while remaining > 0:
            factor = factor * 5
            remaining = remaining - 1
        if factor > 1:
            count = _limbs_mul_small(limbs, count, factor)
    total: i64 = _limb_digits(digits, 0, load_i64(limbs, (count - 1) * 8), 0)
    index = count - 2
    while index >= 0:
        total = _limb_digits(digits, total, load_i64(limbs, index * 8), 9)
        index = index - 1
    store_i64(point_out, 0, total - shift)
    while total > 0 and load_i8(digits, total - 1) == 0:
        total = total - 1
    return total


@c_abi_export("pcc_stdio_float_round_digits")
def _float_round_digits(digits, total: i64, point_box, requested: i64) -> i64:
    # Round the exact expansion ``digits[0:total]`` (no trailing zeros, as
    # produced by pcc_stdio_float_exact_digits) to ``requested`` significant
    # digits, half to even on the exact binary value -- C printf and
    # CPython's dtoa agree.  Returns the retained digit count, at most
    # ``total``; positions past it are zeros.  A carry out of the leading
    # digit leaves a single 1 and bumps the point stored at ``point_box``.
    if requested >= total:
        return total
    if requested < 0:
        return 0
    round_up: i64 = 0
    guard = load_i8(digits, requested)
    if guard > 5:
        round_up: i64 = 1
    elif guard == 5:
        # With no trailing zeros, any digit after the guard puts the
        # discarded tail above one half; an exact tie advances only an odd
        # retained digit (nothing retained counts as 0, even).
        if requested + 1 < total:
            round_up: i64 = 1
        elif requested > 0 and (load_i8(digits, requested - 1) & 1) != 0:
            round_up: i64 = 1
    if round_up == 0:
        return requested
    carry_index = requested - 1
    while carry_index >= 0:
        rounded = load_i8(digits, carry_index) + 1
        if rounded < 10:
            store_i8(digits, carry_index, rounded)
            return requested
        store_i8(digits, carry_index, 0)
        carry_index = carry_index - 1
    # Every retained digit was 9, or none was retained.
    store_i8(digits, 0, 1)
    store_i64(point_box, 0, load_i64(point_box, 0) + 1)
    return 1


@c_abi_export("pcc_stdio_format_float_raw")
def _format_float_raw(
    output,
    value: float,
    conversion: i64,
    precision: i64,
    alternate: i64,
    plus: i64,
    space: i64,
) -> i64:
    position: i64 = 0
    negative = f64_signbit(value)
    if negative != 0:
        store_i8(output, position, 45)
        position = position + 1
        value = 0.0 - value
    elif plus != 0:
        store_i8(output, position, 43)
        position = position + 1
    elif space != 0:
        store_i8(output, position, 32)
        position = position + 1

    uppercase: i64 = 0
    if conversion == 70 or conversion == 69 or conversion == 71:
        uppercase: i64 = 1
    bits = f64_bits(value)
    exponent_bits = logical_shift_right_i64(bits, 52) & 2047
    mantissa_bits = bits & 4503599627370495
    if exponent_bits == 2047 and mantissa_bits != 0:
        store_i8(output, position, 78 if uppercase != 0 else 110)
        store_i8(output, position + 1, 65 if uppercase != 0 else 97)
        store_i8(output, position + 2, 78 if uppercase != 0 else 110)
        position = position + 3
        store_i8(output, position, 0)
        return position
    if exponent_bits == 2047 and mantissa_bits == 0:
        store_i8(output, position, 73 if uppercase != 0 else 105)
        store_i8(output, position + 1, 78 if uppercase != 0 else 110)
        store_i8(output, position + 2, 70 if uppercase != 0 else 102)
        position = position + 3
        store_i8(output, position, 0)
        return position

    if precision < 0:
        precision: i64 = 6
    if (conversion == 103 or conversion == 71) and precision == 0:
        precision: i64 = 1

    # Exact digits of |value|; ``point`` is the number of integer digits.
    # Zero has no digits and formats with exponent 0.
    digits = stack_alloc(800)
    point_box = stack_alloc(8)
    total: i64 = 0
    point: i64 = 1
    if exponent_bits != 0 or mantissa_bits != 0:
        total = _float_exact_digits(bits, digits, point_box)
        point = load_i64(point_box, 0)

    requested_digits = precision + 1
    if conversion == 102 or conversion == 70:
        requested_digits = point + precision
    elif conversion == 103 or conversion == 71:
        requested_digits = precision

    store_i64(point_box, 0, point)
    digit_count = _float_round_digits(digits, total, point_box, requested_digits)
    point = load_i64(point_box, 0)
    exponent = point - 1

    scientific: i64 = 0
    fractional_digits = precision
    if conversion == 101 or conversion == 69:
        scientific: i64 = 1
    elif conversion == 103 or conversion == 71:
        if exponent < -4 or exponent >= precision:
            scientific: i64 = 1
        # '#' keeps all ``precision`` significant digits; positions past
        # ``digit_count`` read as zeros below.
        effective_digits = digit_count
        if alternate != 0:
            effective_digits = requested_digits
        if alternate == 0:
            while effective_digits > 1 and load_i8(
                digits, effective_digits - 1
            ) == 0:
                effective_digits = effective_digits - 1
        if scientific != 0:
            fractional_digits = effective_digits - 1
        else:
            fractional_digits = effective_digits - exponent - 1
            if fractional_digits < 0:
                fractional_digits: i64 = 0

    if scientific != 0:
        leading: i64 = 0
        if digit_count > 0:
            leading = load_i8(digits, 0)
        store_i8(output, position, 48 + leading)
        position = position + 1
        if fractional_digits > 0 or alternate != 0:
            store_i8(output, position, 46)
            position = position + 1
        fraction_index: i64 = 0
        while fraction_index < fractional_digits:
            source_index = fraction_index + 1
            digit: i64 = 0
            if source_index < digit_count:
                digit = load_i8(digits, source_index)
            store_i8(output, position, 48 + digit)
            position = position + 1
            fraction_index = fraction_index + 1
        store_i8(output, position, 69 if uppercase != 0 else 101)
        position = position + 1
        exponent_value = exponent
        if exponent_value < 0:
            store_i8(output, position, 45)
            exponent_value = wrapping_mul_i64(exponent_value, -1)
        else:
            store_i8(output, position, 43)
        position = position + 1
        position = _format_raw_unsigned(
            output, position, exponent_value, 2
        )
    else:
        if exponent < 0:
            store_i8(output, position, 48)
            position = position + 1
        else:
            integer_position = exponent
            while integer_position >= 0:
                source_index = exponent - integer_position
                digit: i64 = 0
                if source_index >= 0 and source_index < digit_count:
                    digit = load_i8(digits, source_index)
                store_i8(output, position, 48 + digit)
                position = position + 1
                integer_position = integer_position - 1
        if fractional_digits > 0 or alternate != 0:
            store_i8(output, position, 46)
            position = position + 1
        fraction_index: i64 = 1
        while fraction_index <= fractional_digits:
            source_index = exponent + fraction_index
            digit: i64 = 0
            if source_index >= 0 and source_index < digit_count:
                digit = load_i8(digits, source_index)
            store_i8(output, position, 48 + digit)
            position = position + 1
            fraction_index = fraction_index + 1

    store_i8(output, position, 0)
    return position


@c_abi_export("pcc_stdio_format_core")
def _format_core(output, capacity: i64, stream, format, cursor) -> i64:
    if ptr_is_null(format):
        if capacity > 0 and not ptr_is_null(output):
            store_i8(output, 0, 0)
        return -1
    index: i64 = 0
    offset: i64 = 0
    while load_i8(format, offset) != 0:
        marker = load_i8(format, offset)
        if marker != 37:
            index = _format_emit_char(output, capacity, stream, index, marker)
            if index < 0:
                return -1
            offset = offset + 1
            continue

        offset = offset + 1
        conversion = load_i8(format, offset)
        if conversion == 37:
            index = _format_emit_char(output, capacity, stream, index, 37)
            if index < 0:
                return -1
            offset = offset + 1
            continue

        alternate: i64 = 0
        zero_pad: i64 = 0
        left: i64 = 0
        space: i64 = 0
        plus: i64 = 0
        while (
            conversion == 35
            or conversion == 48
            or conversion == 45
            or conversion == 32
            or conversion == 43
        ):
            if conversion == 35:
                alternate: i64 = 1
            elif conversion == 48:
                zero_pad: i64 = 1
            elif conversion == 45:
                left: i64 = 1
            elif conversion == 32:
                space: i64 = 1
            elif conversion == 43:
                plus: i64 = 1
            offset = offset + 1
            conversion = load_i8(format, offset)

        width: i64 = 0
        if conversion == 42:
            width = va_arg_i32(cursor)
            if width < 0:
                width = wrapping_mul_i64(width, -1)
                left: i64 = 1
            offset = offset + 1
            conversion = load_i8(format, offset)
        else:
            while conversion >= 48 and conversion <= 57:
                width = width * 10 + conversion - 48
                offset = offset + 1
                conversion = load_i8(format, offset)

        precision: i64 = -1
        if conversion == 46:
            precision: i64 = 0
            offset = offset + 1
            conversion = load_i8(format, offset)
            if conversion == 42:
                precision = va_arg_i32(cursor)
                if precision < 0:
                    precision: i64 = -1
                offset = offset + 1
                conversion = load_i8(format, offset)
            else:
                while conversion >= 48 and conversion <= 57:
                    precision = precision * 10 + conversion - 48
                    offset = offset + 1
                    conversion = load_i8(format, offset)

        wide: i64 = 0
        if conversion == 108:
            wide: i64 = 1
            offset = offset + 1
            conversion = load_i8(format, offset)
            if conversion == 108:
                wide: i64 = 1
                offset = offset + 1
                conversion = load_i8(format, offset)
        elif conversion == 122:
            wide: i64 = 1
            offset = offset + 1
            conversion = load_i8(format, offset)
        elif conversion == 104:
            offset = offset + 1
            conversion = load_i8(format, offset)
            if conversion == 104:
                offset = offset + 1
                conversion = load_i8(format, offset)

        if conversion == 0:
            break
        if conversion == 115:
            string_value = va_arg_ptr(cursor)
            string_length = _format_cstr_length(string_value, precision)
            padding = width - string_length
            if padding < 0:
                padding: i64 = 0
            if left == 0:
                index = _format_emit_repeat(
                    output, capacity, stream, index, 32, padding
                )
            if index < 0:
                return -1
            index = _format_emit_cstr(
                output,
                capacity,
                stream,
                index,
                string_value,
                precision,
            )
            if index >= 0 and left != 0:
                index = _format_emit_repeat(
                    output, capacity, stream, index, 32, padding
                )
        elif conversion == 99:
            padding = width - 1
            if padding < 0:
                padding: i64 = 0
            if left == 0:
                index = _format_emit_repeat(
                    output, capacity, stream, index, 32, padding
                )
            if index < 0:
                return -1
            index = _format_emit_char(
                output, capacity, stream, index, va_arg_i32(cursor) & 255
            )
            if index >= 0 and left != 0:
                index = _format_emit_repeat(
                    output, capacity, stream, index, 32, padding
                )
        elif conversion == 100 or conversion == 105:
            if wide != 0:
                signed_value = va_arg_i64(cursor)
            else:
                signed_value = va_arg_i32(cursor)
            index = _format_emit_integer(
                output,
                capacity,
                stream,
                index,
                signed_value,
                10,
                0,
                1,
                width,
                precision,
                left,
                zero_pad,
                plus,
                space,
                0,
            )
        elif (
            conversion == 117
            or conversion == 120
            or conversion == 88
            or conversion == 111
        ):
            if wide != 0:
                unsigned_value = va_arg_i64(cursor)
            else:
                unsigned_value = va_arg_u32(cursor)
            base: i64 = 10
            uppercase: i64 = 0
            if conversion == 120 or conversion == 88:
                base: i64 = 16
                if conversion == 88:
                    uppercase: i64 = 1
            elif conversion == 111:
                base: i64 = 8
            index = _format_emit_integer(
                output,
                capacity,
                stream,
                index,
                unsigned_value,
                base,
                uppercase,
                0,
                width,
                precision,
                left,
                zero_pad,
                0,
                0,
                alternate,
            )
        elif conversion == 112:
            pointer_value = va_arg_ptr(cursor)
            index = _format_emit_integer(
                output,
                capacity,
                stream,
                index,
                ptr_diff(pointer_value, null()),
                16,
                0,
                0,
                width,
                precision,
                left,
                zero_pad,
                0,
                0,
                2,
            )
        elif (
            conversion == 102
            or conversion == 70
            or conversion == 101
            or conversion == 69
            or conversion == 103
            or conversion == 71
        ):
            raw_float = stack_alloc(512)
            raw_heap = null()
            if _float_raw_capacity(precision) > 512:
                raw_heap = malloc(_float_raw_capacity(precision))
                if ptr_is_null(raw_heap):
                    return -1
                raw_float = raw_heap
            raw_length = _format_float_raw(
                raw_float,
                va_arg_f64(cursor),
                conversion,
                precision,
                alternate,
                plus,
                space,
            )
            padding = width - raw_length
            if padding < 0:
                padding: i64 = 0
            raw_offset: i64 = 0
            if left == 0 and zero_pad == 0:
                index = _format_emit_repeat(
                    output, capacity, stream, index, 32, padding
                )
            elif left == 0 and zero_pad != 0:
                first = load_i8(raw_float, 0)
                if first == 45 or first == 43 or first == 32:
                    index = _format_emit_char(
                        output, capacity, stream, index, first
                    )
                    raw_offset: i64 = 1
                if index >= 0:
                    index = _format_emit_repeat(
                        output, capacity, stream, index, 48, padding
                    )
            if index >= 0:
                index = _format_emit_cstr(
                    output,
                    capacity,
                    stream,
                    index,
                    ptr_add(raw_float, raw_offset),
                    -1,
                )
            if index >= 0 and left != 0:
                index = _format_emit_repeat(
                    output, capacity, stream, index, 32, padding
                )
            if not ptr_is_null(raw_heap):
                free(raw_heap)
        else:
            index = _format_emit_char(output, capacity, stream, index, 37)
            if index >= 0:
                index = _format_emit_char(
                    output, capacity, stream, index, conversion
                )
        if index < 0:
            return -1
        offset = offset + 1

    if ptr_is_null(stream) and capacity > 0 and not ptr_is_null(output):
        terminator = index
        if terminator >= capacity:
            terminator = capacity - 1
        store_i8(output, terminator, 0)
    return index


@c_abi_typed_export("sprintf", "i32", ("ptr", "ptr"))
@c_abi_variadic_export("sprintf")
def sprintf(output, format) -> i64:
    cursor = va_start()
    # sprintf has no destination bound. The largest addressable signed
    # pointer distance keeps the shared formatter's terminating-NUL contract.
    result = _format_core(output, 9223372036854775807, null(), format, cursor)
    va_end(cursor)
    if result > 2147483647:
        return -1
    return result


@c_abi_variadic_export("snprintf")
def snprintf(output, capacity: i64, format) -> i64:
    cursor = va_start()
    result = _format_core(output, capacity, null(), format, cursor)
    va_end(cursor)
    return result


@c_abi_export("vsnprintf")
def vsnprintf(output, capacity: i64, format, ap) -> i64:
    return _format_core(output, capacity, null(), format, va_cursor(ap))


@c_abi_variadic_export("fprintf")
def fprintf(stream, format) -> i64:
    cursor = va_start()
    result = _format_core(null(), 0, stream, format, cursor)
    va_end(cursor)
    return result


@c_abi_typed_export("printf", "i32", ("ptr",))
@c_abi_variadic_export("printf")
def printf(format) -> i64:
    cursor = va_start()
    stream = load_ptr(global_addr("stdout"), 0)
    # The public Darwin C header spells stdout as __stdoutp.
    if load_i8(target_sys_platform(), 0) == 100:
        stream = load_ptr(global_addr("__stdoutp"), 0)
    result = _format_core(null(), 0, stream, format, cursor)
    va_end(cursor)
    return result


@c_abi_export("popen")
def popen(command, mode) -> c_ptr:
    if ptr_is_null(command) or ptr_is_null(mode):
        return null()
    first = load_i8(mode, 0)
    if first != 114 and first != 119:
        return null()
    mode_offset: i64 = 1
    if load_i8(mode, mode_offset) == 101:
        mode_offset = mode_offset + 1
    if load_i8(mode, mode_offset) != 0:
        return null()

    argv = stack_alloc(32)
    store_ptr(argv, 0, cstr("sh"))
    store_ptr(argv, 8, cstr("-c"))
    store_ptr(argv, 16, command)
    store_ptr(argv, 24, null())
    fd_out = stack_alloc(4)
    envp = initial_environ()
    if ptr_is_null(envp):
        return null()
    parent_reads: i64 = 1 if first == 114 else 0
    pid = spawn_process_pipe(
        cstr("/bin/sh"), argv, envp, parent_reads, fd_out
    )
    if pid <= 0:
        return null()

    flags = abi_constant("stdio.flag.readable")
    if first == 119:
        flags = abi_constant("stdio.flag.writable")
    stream = _stream_new(load_i32(fd_out, 0), flags, pid)
    if ptr_is_null(stream):
        close(load_i32(fd_out, 0))
        status = stack_alloc(4)
        waited = waitpid(pid, status, 0)
        while waited == -4:
            waited = waitpid(pid, status, 0)
        return null()
    return stream


@c_abi_export("pclose")
def pclose(stream) -> i64:
    if ptr_is_null(stream) or load_i64(
        stream, abi_constant("stdio.file.magic_offset")
    ) != abi_constant("stdio.file.magic"):
        return -1
    flags = load_i64(stream, abi_constant("stdio.file.flags_offset"))
    pid = load_i64(stream, abi_constant("stdio.file.aux_offset"))
    if (flags & abi_constant("stdio.flag.standard")) != 0 or pid <= 0:
        return -1
    _stream_unregister(stream)
    fd = load_i64(stream, abi_constant("stdio.file.fd_offset"))
    flush_result: i64 = 0
    if (flags & abi_constant("stdio.flag.writable")) != 0:
        flush_result = _flush_output(stream)
    close_result = close(fd)
    _stream_release_buffer(stream)
    store_i64(stream, abi_constant("stdio.file.magic_offset"), 0)
    free(stream)

    status = stack_alloc(4)
    waited = waitpid(pid, status, 0)
    while waited == -4:
        waited = waitpid(pid, status, 0)
    if flush_result != 0 or close_result != 0 or waited != pid:
        return -1
    return load_i32(status, 0)
