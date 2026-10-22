"""Linux x86-64/AArch64 process entry for programs using pcc-Python libc.

The self backend receives the kernel's original stack pointer, reconstructs
the SysV ``argc``/``argv``/``envp`` values, initializes pcc's owned environment
table, calls the C translation unit's ``main``, and uses the owned C exit
lifecycle (callbacks, finalizers, stream flush, then ``exit_group``).  No C or assembly startup source participates.
"""

from pcc import i64
from pcc.extern import c_abi_export, c_int, c_int32, c_ptr, c_void, extern
from pcc.unsafe import (
    define_global_null_ptr_array,
    define_global_i64_array,
    store_i64,
    global_addr,
    int_to_ptr,
    load_i8,
    load_i32,
    load_i64,
    load_ptr,
    null,
    process_exit,
    ptr_add,
    ptr_is_null,
    store_i8,
    store_ptr,
    page_alloc,
    ptr_to_int,
    target_platform_machine,
    linux_set_thread_pointer,
    call_void_ptr0,
    ptr_diff,
)

__pcc_freestanding__ = True


# PT_TLS metadata shared with the owned thread substrate. Both initial and
# subsequently cloned threads copy the same template into their private TLS.
define_global_i64_array("pcc_linux_tls_image", 0, 0, 0, 1)


c_main = extern("main", (c_int, c_ptr, c_ptr), c_int)
platform_env_init = extern("pcc_platform_env_init", (c_ptr,), c_int)
c_exit = extern("exit", (c_int32,), c_void)


@c_abi_export("pcc_linux_initial_tls_setup")
def pcc_linux_initial_tls_setup(initial_stack: c_ptr) -> i64:
    # Decode argc/argv/envp just far enough to reach the kernel auxv.  The
    # bounds are corruption guards, not normal limits.
    argc: i64 = load_i64(initial_stack, 0)
    if argc < 0 or argc >= 1048576:
        return -1
    argv = ptr_add(initial_stack, 8)
    envp = ptr_add(argv, (argc + 1) * 8)
    env_index: i64 = 0
    while env_index < 1048576:
        if ptr_is_null(load_ptr(envp, env_index * 8)) != 0:
            break
        env_index = env_index + 1
    if env_index >= 1048576:
        return -1

    # Elf64_auxv_t is two u64 words.  AT_PHDR/AT_PHENT/AT_PHNUM describe the
    # final executable, so this remains correct when archive selection changes
    # the linked TLS members or their layout.
    auxv = ptr_add(envp, (env_index + 1) * 8)
    phdr_value: i64 = 0
    phent: i64 = 0
    phnum: i64 = 0
    aux_index: i64 = 0
    aux_done: i64 = 0
    while aux_index < 256:
        aux_type: i64 = load_i64(auxv, aux_index * 16)
        aux_value: i64 = load_i64(auxv, aux_index * 16 + 8)
        if aux_type == 0:  # AT_NULL
            aux_done: i64 = 1
            break
        if aux_type == 3:  # AT_PHDR
            phdr_value = aux_value
        elif aux_type == 4:  # AT_PHENT
            phent = aux_value
        elif aux_type == 5:  # AT_PHNUM
            phnum = aux_value
        aux_index = aux_index + 1
    if aux_done == 0 or phdr_value == 0:
        return -1
    if phent < 56 or phent > 4096 or phnum <= 0 or phnum > 4096:
        return -1

    # Elf64_Phdr offsets: type@0, vaddr@16, filesz@32, memsz@40, align@48.
    # GNU ld emits the x86_64 local-exec TLS variables as variant II: the
    # rounded image immediately precedes the thread pointer.
    phdr = int_to_ptr(phdr_value)
    tls_template = null()
    tls_file_size: i64 = 0
    tls_memory_size: i64 = 0
    tls_alignment: i64 = 1
    tls_found: i64 = 0
    ph_index: i64 = 0
    while ph_index < phnum:
        program_header = ptr_add(phdr, ph_index * phent)
        if load_i32(program_header, 0) == 7:  # PT_TLS
            if tls_found != 0:
                return -1
            tls_found: i64 = 1
            tls_template = int_to_ptr(load_i64(program_header, 16))
            tls_file_size = load_i64(program_header, 32)
            tls_memory_size = load_i64(program_header, 40)
            tls_alignment = load_i64(program_header, 48)
        ph_index = ph_index + 1

    metadata = global_addr("pcc_linux_tls_image")
    store_ptr(metadata, 0, tls_template)
    store_i64(metadata, 8, tls_file_size)
    store_i64(metadata, 16, tls_memory_size)
    store_i64(metadata, 24, tls_alignment)
    thread_pointer = pcc_linux_allocate_tls(null())
    if ptr_is_null(thread_pointer):
        return -1
    return linux_set_thread_pointer(thread_pointer)


@c_abi_export("pcc_linux_allocate_tls")
def pcc_linux_allocate_tls(allocation_out: c_ptr) -> c_ptr:
    metadata = global_addr("pcc_linux_tls_image")
    tls_template = load_ptr(metadata, 0)
    tls_file_size: i64 = load_i64(metadata, 8)
    tls_memory_size: i64 = load_i64(metadata, 16)
    tls_alignment: i64 = load_i64(metadata, 24)
    if tls_file_size < 0 or tls_memory_size < 0 or tls_file_size > tls_memory_size:
        return null()
    if tls_alignment <= 0 or (tls_alignment & (tls_alignment - 1)) != 0:
        return null()
    if tls_alignment > 1048576 or tls_memory_size > 1073741824:
        return null()
    alignment: i64 = tls_alignment
    if alignment < 16:
        alignment = 16
    rounded_size: i64 = (tls_memory_size + tls_alignment - 1) & (0 - tls_alignment)
    allocation_size: i64 = rounded_size + alignment * 2 + 32
    reserve = page_alloc(allocation_size)
    if ptr_is_null(reserve):
        return null()
    base: i64 = (ptr_to_int(reserve) + alignment - 1) & (0 - alignment)
    machine = target_platform_machine()
    if load_i8(machine, 0) == 97:  # arm64/aarch64: variant I, 16-byte TCB
        thread_pointer = int_to_ptr(base)
        gap: i64 = (16 + tls_alignment - 1) & (0 - tls_alignment)
        tls_begin = ptr_add(thread_pointer, gap)
    else:  # x86-64: variant II, template immediately below TP
        tp: i64 = (base + rounded_size + alignment - 1) & (0 - alignment)
        thread_pointer = int_to_ptr(tp)
        tls_begin = ptr_add(thread_pointer, 0 - rounded_size)
    zero_index: i64 = 0
    while zero_index < rounded_size:
        store_i8(tls_begin, zero_index, 0)
        zero_index = zero_index + 1
    copy_index: i64 = 0
    while copy_index < tls_file_size:
        store_i8(
            tls_begin,
            copy_index,
            load_i8(tls_template, copy_index),
        )
        copy_index = copy_index + 1
    store_ptr(thread_pointer, 0, thread_pointer)
    if not ptr_is_null(allocation_out):
        store_ptr(allocation_out, 0, reserve)
        store_i64(allocation_out, 8, allocation_size)
    return thread_pointer


@c_abi_export("_start")
def pcc_c_linux_start(initial_stack: c_ptr) -> None:
    # This is the initial-thread half of the compiler-owned TLS contract; the
    # pthread runtime remains responsible for installing a distinct block for
    # every spawned thread.
    if pcc_linux_initial_tls_setup(initial_stack) != 0:
        process_exit(71)
    argc: i64 = load_i64(initial_stack, 0)
    status: i64 = 64
    if argc >= 0:
        argv = ptr_add(initial_stack, 8)
        envp = ptr_add(argv, (argc + 1) * 8)
        if platform_env_init(envp) == 0:
            initializers = global_addr("__init_array_start")
            init_count: i64 = ptr_diff(global_addr("__init_array_end"), initializers) // 8
            index: i64 = 0
            while index < init_count:
                call_void_ptr0(load_ptr(initializers, index * 8))
                index = index + 1
            status = c_main(argc, argv, envp)
            c_exit(status)
        else:
            status: i64 = 70
    process_exit(status)
