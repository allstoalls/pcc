"""Windows child-process ownership, argument quoting and job cleanup."""

from pcc import i64
from pcc.extern import c_abi_export, c_ptr, c_int, c_int64, c_void, extern
from pcc.unsafe import (
    malloc, free, null, cstr, ptr_is_null, ptr_add, ptr_to_int, load_i8, load_i32,
    load_i64, load_ptr, store_i8, store_i32, store_i64, store_ptr, stack_alloc,
    define_global_ptr_null, global_load_ptr, global_store_ptr, define_global_i8,
    global_addr, atomic_test_and_set, atomic_clear,
)

__pcc_freestanding__ = True

CreateProcessW = extern("CreateProcessW", (c_ptr, c_ptr, c_ptr, c_ptr, c_int, c_int, c_ptr, c_ptr, c_ptr, c_ptr), c_int)
CreateJobObjectW = extern("CreateJobObjectW", (c_ptr, c_ptr), c_ptr)
SetInformationJobObject = extern("SetInformationJobObject", (c_ptr, c_int, c_ptr, c_int), c_int)
AssignProcessToJobObject = extern("AssignProcessToJobObject", (c_ptr, c_ptr), c_int)
TerminateJobObject = extern("TerminateJobObject", (c_ptr, c_int), c_int)
TerminateProcess = extern("TerminateProcess", (c_ptr, c_int), c_int)
OpenProcess = extern("OpenProcess", (c_int, c_int, c_int), c_ptr)
ResumeThread = extern("ResumeThread", (c_ptr,), c_int)
WaitForSingleObject = extern("WaitForSingleObject", (c_ptr, c_int), c_int)
GetExitCodeProcess = extern("GetExitCodeProcess", (c_ptr, c_ptr), c_int)
CloseHandle = extern("CloseHandle", (c_ptr,), c_int)
GetStdHandle = extern("GetStdHandle", (c_int,), c_ptr)
CreatePipe = extern("CreatePipe", (c_ptr, c_ptr, c_ptr, c_int), c_int)
SetHandleInformation = extern("SetHandleInformation", (c_ptr, c_int, c_int), c_int)
CreateFileW = extern("CreateFileW", (c_ptr, c_int, c_int, c_ptr, c_int, c_int, c_ptr), c_ptr)
GetCurrentProcess = extern("GetCurrentProcess", (), c_ptr)
DuplicateHandle = extern("DuplicateHandle", (c_ptr, c_ptr, c_ptr, c_ptr, c_int, c_int, c_int), c_int)
InitializeProcThreadAttributeList = extern("InitializeProcThreadAttributeList", (c_ptr, c_int, c_int, c_ptr), c_int)
UpdateProcThreadAttribute = extern("UpdateProcThreadAttribute", (c_ptr, c_int, c_int64, c_ptr, c_int64, c_ptr, c_ptr), c_int)
DeleteProcThreadAttributeList = extern("DeleteProcThreadAttributeList", (c_ptr,), c_void)
CompareStringOrdinal = extern("CompareStringOrdinal", (c_ptr, c_int, c_ptr, c_int, c_int), c_int)
utf16 = extern("pcc_win_utf16", (c_ptr,), c_ptr)
error = extern("pcc_win_error", (), c_int64)
fd_register = extern("pcc_win_fd_register", (c_ptr,), c_int64)
fd_handle = extern("pcc_win_fd_handle", (c_int64,), c_ptr)
Sleep = extern("Sleep", (c_int,), c_void)

define_global_ptr_null("pcc_win_children")
define_global_i8("pcc_win_children_lock", 0)


@c_abi_export("pcc_windows_process_lock")
def lock() -> None:
    while atomic_test_and_set(global_addr("pcc_win_children_lock"), 0, "acquire"):
        pass


@c_abi_export("pcc_windows_process_unlock")
def unlock() -> None:
    atomic_clear(global_addr("pcc_win_children_lock"), 0, "release")


@c_abi_export("pcc_windows_process_length")
def length(text: c_ptr) -> i64:
    count: i64 = 0
    while load_i8(text, count) != 0:
        count = count + 1
    return count


@c_abi_export("pcc_win_command_line")
def command_line(argv: c_ptr) -> c_ptr:
    count: i64 = 0
    capacity: i64 = 1
    while not ptr_is_null(load_ptr(argv, count * 8)):
        capacity = capacity + 2 * length(load_ptr(argv, count * 8)) + 4
        count = count + 1
    raw = malloc(capacity)
    if ptr_is_null(raw):
        return null()
    out: i64 = 0
    index: i64 = 0
    while index < count:
        if index:
            store_i8(raw, out, 32)
            out = out + 1
        store_i8(raw, out, 34)
        out = out + 1
        text = load_ptr(argv, index * 8)
        pos: i64 = 0
        while True:
            slashes: i64 = 0
            while load_i8(text, pos) == 92:
                slashes = slashes + 1
                pos = pos + 1
            byte: i64 = load_i8(text, pos)
            copies: i64 = slashes
            if byte == 34 or byte == 0:
                copies = slashes * 2
            if byte == 34:
                copies = copies + 1
            while copies:
                store_i8(raw, out, 92)
                out = out + 1
                copies = copies - 1
            if byte == 0:
                break
            store_i8(raw, out, byte)
            out = out + 1
            pos = pos + 1
        store_i8(raw, out, 34)
        out = out + 1
        index = index + 1
    store_i8(raw, out, 0)
    result = utf16(raw)
    free(raw)
    return result


@c_abi_export("pcc_windows_process_environment_name_length")
def environment_name_length(wide: c_ptr) -> i64:
    index: i64 = 0
    while True:
        lo: i64 = load_i8(wide, index * 2)
        hi: i64 = load_i8(wide, index * 2 + 1)
        if lo == 0 and hi == 0:
            return index
        # The hidden drive-current-directory entries start with '='.
        if index > 0 and lo == 61 and hi == 0:
            return index
        index = index + 1


@c_abi_export("pcc_windows_process_environment_block")
def environment_block(envp: c_ptr) -> c_ptr:
    if ptr_is_null(envp):
        return null()
    size: i64 = 2
    index: i64 = 0
    while not ptr_is_null(load_ptr(envp, index * 8)):
        size = size + (length(load_ptr(envp, index * 8)) + 1) * 2
        index = index + 1
    count: i64 = index
    if size == 2:
        size = 4
    block = malloc(size)
    ordered = malloc((count + 1) * 8)
    if ptr_is_null(block) or ptr_is_null(ordered):
        free(block)
        free(ordered)
        return null()
    index = 0
    while index < count:
        wide = utf16(load_ptr(envp, index * 8))
        if ptr_is_null(wide):
            done: i64 = 0
            while done < index:
                free(load_ptr(ordered, done * 8))
                done = done + 1
            free(ordered)
            free(block)
            return null()
        # Windows requires name-sorted Unicode environment blocks. Use the
        # OS ordinal case mapping, including non-ASCII environment names.
        position: i64 = index
        while position > 0:
            previous = load_ptr(ordered, (position - 1) * 8)
            compared: i64 = CompareStringOrdinal(previous, environment_name_length(previous), wide, environment_name_length(wide), 1)
            if compared != 3:  # CSTR_GREATER_THAN
                break
            store_ptr(ordered, position * 8, previous)
            position = position - 1
        store_ptr(ordered, position * 8, wide)
        index = index + 1
    index = 0
    out: i64 = 0
    while index < count:
        wide = load_ptr(ordered, index * 8)
        pos: i64 = 0
        while True:
            lo: i64 = load_i8(wide, pos)
            hi: i64 = load_i8(wide, pos + 1)
            store_i8(block, out, lo)
            store_i8(block, out + 1, hi)
            out = out + 2
            pos = pos + 2
            if lo == 0 and hi == 0:
                break
        free(wide)
        index = index + 1
    free(ordered)
    store_i8(block, out, 0)
    store_i8(block, out + 1, 0)
    if out == 0:
        store_i8(block, 2, 0)
        store_i8(block, 3, 0)
    return block


@c_abi_export("pcc_windows_process_close_child_handles")
def close_child_handles(handles: c_ptr) -> None:
    index: i64 = 0
    while index < 3:
        value = load_ptr(handles, index * 8)
        if not ptr_is_null(value):
            CloseHandle(value)
        index = index + 1


@c_abi_export("pcc_windows_process_spawn")
def spawn(path: c_ptr, argv: c_ptr, envp: c_ptr, child_input: c_ptr,
          child_output: c_ptr, child_error: c_ptr, directory: c_ptr) -> i64:
    wide = utf16(path)
    command = command_line(argv)
    environment = environment_block(envp)
    # NULL keeps the parent's working directory.
    wide_directory = null()
    if not ptr_is_null(directory):
        wide_directory = utf16(directory)
    if (ptr_is_null(wide) or ptr_is_null(command) or (not ptr_is_null(envp) and ptr_is_null(environment))
            or (not ptr_is_null(directory) and ptr_is_null(wide_directory))):
        free(wide)
        free(command)
        free(environment)
        free(wide_directory)
        return -12
    startup = stack_alloc(112)
    info = stack_alloc(24)
    limits = stack_alloc(144)
    originals = stack_alloc(24)
    handles = stack_alloc(24)
    inherited = stack_alloc(24)
    store_ptr(originals, 0, child_input)
    store_ptr(originals, 8, child_output)
    store_ptr(originals, 16, child_error)
    index: i64 = 0
    while index < 144:
        store_i8(limits, index, 0)
        index = index + 1
    index = 0
    while index < 112:
        store_i8(startup, index, 0)
        index = index + 1
    store_i32(startup, 0, 104)
    store_i32(startup, 60, 256)
    index = 0
    while index < 3:
        store_ptr(handles, index * 8, null())
        index = index + 1
    owner = GetCurrentProcess()
    count: i64 = 0
    status: i64 = 0
    index = 0
    while index < 3:
        source = load_ptr(originals, index * 8)
        if not ptr_is_null(source) and ptr_to_int(source) != -1:
            if DuplicateHandle(owner, source, owner, ptr_add(handles, index * 8), 0, 1, 2) == 0:
                status = error()
                break
            value = load_ptr(handles, index * 8)
            store_ptr(startup, 80 + index * 8, value)
            store_ptr(inherited, count * 8, value)
            count = count + 1
        index = index + 1
    attributes = null()
    initialized: i64 = 0
    flags: i64 = 1028  # CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT
    if status == 0 and count > 0:
        size = stack_alloc(8)
        store_i64(size, 0, 0)
        InitializeProcThreadAttributeList(null(), 1, 0, size)
        size_bytes: i64 = load_i64(size, 0)
        if size_bytes <= 0:
            status = error()
        else:
            attributes = malloc(size_bytes)
            if ptr_is_null(attributes):
                status = -12
            elif InitializeProcThreadAttributeList(attributes, 1, 0, size) == 0:
                status = error()
            else:
                initialized = 1
                # PROC_THREAD_ATTRIBUTE_HANDLE_LIST restricts inheritance to
                # these private duplicates, never another worker's pipe ends.
                if UpdateProcThreadAttribute(attributes, 0, 131074, inherited, count * 8, null(), null()) == 0:
                    status = error()
                else:
                    store_i32(startup, 0, 112)
                    store_ptr(startup, 104, attributes)
                    flags = flags | 524288  # EXTENDED_STARTUPINFO_PRESENT
    if status != 0:
        if initialized:
            DeleteProcThreadAttributeList(attributes)
        free(attributes)
        close_child_handles(handles)
        free(wide)
        free(command)
        free(environment)
        free(wide_directory)
        return status
    # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE owns every descendant, including
    # compiler workers that outlive their leader after an error.
    store_i32(limits, 16, 8192)
    job = CreateJobObjectW(null(), null())
    job_error: i64 = error() if ptr_is_null(job) else 0
    record = malloc(40)
    if ptr_is_null(job) or ptr_is_null(record):
        if initialized:
            DeleteProcThreadAttributeList(attributes)
        free(attributes)
        close_child_handles(handles)
        free(wide)
        free(command)
        free(environment)
        free(wide_directory)
        free(record)
        if not ptr_is_null(job):
            CloseHandle(job)
        if job_error:
            return job_error
        return -12
    configured: i64 = SetInformationJobObject(job, 9, limits, 144)
    ok: i64 = 0
    if configured:
        ok = CreateProcessW(wide, command, null(), null(), 1 if count else 0, flags, environment, wide_directory, startup, info)
    status = error() if ok == 0 else 0
    if initialized:
        DeleteProcThreadAttributeList(attributes)
    free(attributes)
    close_child_handles(handles)
    free(wide)
    free(command)
    free(environment)
    free(wide_directory)
    if ok == 0:
        CloseHandle(job)
        free(record)
        return status
    process = load_ptr(info, 0)
    thread = load_ptr(info, 8)
    if AssignProcessToJobObject(job, process) == 0 or ResumeThread(thread) == -1:
        status = error()
        TerminateProcess(process, 127)
        WaitForSingleObject(process, -1)
        CloseHandle(thread)
        CloseHandle(process)
        CloseHandle(job)
        free(record)
        return status
    CloseHandle(thread)
    pid: i64 = load_i32(info, 16)
    store_i64(record, 8, pid)
    store_ptr(record, 16, process)
    store_ptr(record, 24, job)
    lock()
    store_ptr(record, 0, global_load_ptr("pcc_win_children"))
    global_store_ptr("pcc_win_children", record)
    unlock()
    return pid


@c_abi_export("pcc_win_spawn_process")
def spawn_process(path: c_ptr, argv: c_ptr, envp: c_ptr, capture_output: i64) -> i64:
    # This low-level flag has the same suppression contract as Darwin/Linux;
    # callers retrieving bytes use spawn_process_pipe instead.
    output = GetStdHandle(-11)
    stderr = GetStdHandle(-12)
    quiet = null()
    if capture_output:
        wide = utf16(cstr("NUL"))
        if ptr_is_null(wide):
            return -12
        quiet = CreateFileW(wide, 1073741824, 3, null(), 3, 128, null())
        status: i64 = error() if ptr_to_int(quiet) == -1 else 0
        free(wide)
        if status:
            return status
        output = quiet
        stderr = quiet
    result: i64 = spawn(path, argv, envp, GetStdHandle(-10), output, stderr, null())
    if not ptr_is_null(quiet):
        CloseHandle(quiet)
    return result


@c_abi_export("pcc_win_spawn_process_pipe")
def spawn_process_pipe(path: c_ptr, argv: c_ptr, envp: c_ptr,
                       parent_reads: i64, fd_out: c_ptr) -> i64:
    security = stack_alloc(24)
    store_i32(security, 0, 24)
    store_ptr(security, 8, null())
    store_i32(security, 16, 0)
    handles = stack_alloc(16)
    if CreatePipe(handles, ptr_add(handles, 8), security, 0) == 0:
        return error()
    reader = load_ptr(handles, 0)
    writer = load_ptr(handles, 8)
    parent = reader if parent_reads else writer
    child = writer if parent_reads else reader
    stdin = GetStdHandle(-10) if parent_reads else child
    stdout = child if parent_reads else GetStdHandle(-11)
    status: i64 = spawn(path, argv, envp, stdin, stdout, GetStdHandle(-12), null())
    CloseHandle(child)
    if status < 0:
        CloseHandle(parent)
    else:
        descriptor: i64 = fd_register(parent)
        if descriptor < 0:
            kill(status, 9)
            waitpid(status, null(), 0)
            return descriptor
        store_i32(fd_out, 0, descriptor)
    return status


@c_abi_export("pcc_win_spawn_process_redirect")
def spawn_process_redirect(path: c_ptr, argv: c_ptr, envp: c_ptr, directory: c_ptr,
                           stdin_fd: i64, stdout_fd: i64, stderr_fd: i64) -> i64:
    # A negative descriptor inherits the parent's standard handle.
    child_input = GetStdHandle(-10) if stdin_fd < 0 else fd_handle(stdin_fd)
    child_output = GetStdHandle(-11) if stdout_fd < 0 else fd_handle(stdout_fd)
    child_error = GetStdHandle(-12) if stderr_fd < 0 else fd_handle(stderr_fd)
    if ((stdin_fd >= 0 and ptr_to_int(child_input) == -1)
            or (stdout_fd >= 0 and ptr_to_int(child_output) == -1)
            or (stderr_fd >= 0 and ptr_to_int(child_error) == -1)):
        return -9  # EBADF: not an open descriptor
    return spawn(path, argv, envp, child_input, child_output, child_error, directory)


@c_abi_export("pcc_win_waitpid")
def waitpid(pid: i64, status_out: c_ptr, options: i64) -> i64:
    while True:
        lock()
        previous = null()
        record = global_load_ptr("pcc_win_children")
        while not ptr_is_null(record) and load_i64(record, 8) != pid:
            previous = record
            record = load_ptr(record, 0)
        if ptr_is_null(record):
            unlock()
            return -10
        process = load_ptr(record, 16)
        # Poll under the list lock, sleep outside it so other workers can spawn.
        result: i64 = WaitForSingleObject(process, 0)
        if result == 258:
            unlock()
            if options & 1:
                return 0
            Sleep(1)
            continue
        if result != 0:
            failure: i64 = error()
            unlock()
            return failure
        code = stack_alloc(8)
        if GetExitCodeProcess(process, code) == 0:
            failure: i64 = error()
            unlock()
            return failure
        if not ptr_is_null(status_out):
            store_i32(status_out, 0, (load_i32(code, 0) & 255) * 256)
        successor = load_ptr(record, 0)
        if ptr_is_null(previous):
            global_store_ptr("pcc_win_children", successor)
        else:
            store_ptr(previous, 0, successor)
        CloseHandle(process)
        CloseHandle(load_ptr(record, 24))
        free(record)
        unlock()
        return pid

@c_abi_export("pcc_win_kill")
def kill(pid: i64, signal_number: i64) -> i64:
    selected: i64 = 0 - pid if pid < 0 else pid
    lock()
    record = global_load_ptr("pcc_win_children")
    while not ptr_is_null(record) and load_i64(record, 8) != selected:
        record = load_ptr(record, 0)
    if ptr_is_null(record):
        unlock()
        if pid <= 0:
            return -3
        process = OpenProcess(4096 if signal_number == 0 else 1, 0, pid)
        if ptr_is_null(process):
            return error()
        result: i64 = 1 if signal_number == 0 else TerminateProcess(process, 128 + signal_number)
        status: i64 = 0 if result else error()
        CloseHandle(process)
        return status
    result: i64 = 1
    if signal_number != 0:
        result = TerminateJobObject(load_ptr(record, 24), 128 + signal_number) if pid < 0 else TerminateProcess(load_ptr(record, 16), 128 + signal_number)
    status: i64 = 0 if result else error()
    unlock()
    return status
