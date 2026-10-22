"""Freestanding pcc-Python memory and string substrate.

This module is compiled into raw C-ABI objects.  It deliberately uses only
``pcc.unsafe`` byte loads/stores and pointer arithmetic: no libc call, managed
Python object, allocator, exception, boxing, or GC service is available here.
The retained musl sources are differential oracles, not this implementation.
"""

from pcc import i64
from pcc.extern import c_abi_export, c_abi_typed_export, c_ptr
from pcc.unsafe import (
    load_i64,
    load_i8,
    null,
    ptr_add,
    ptr_diff,
    store_i8,
    store_i64,
    wrapping_mul_i64,
)

__pcc_freestanding__ = True


@c_abi_export("memcpy")
def pcc_memcpy(dst, src, size: i64) -> c_ptr:
    # Eight bytes per iteration.  A byte-at-a-time copy left `memmove` as the
    # #2 leaf (562 of ~10000 samples) when pcc1 compiles a real module; every
    # string, list-grow and object copy in the runtime funnels through here.
    index: i64 = 0
    limit: i64 = size - 8
    while index <= limit:
        store_i64(dst, index, load_i64(src, index))
        index = index + 8
    while index < size:
        store_i8(dst, index, load_i8(src, index))
        index = index + 1
    return dst


@c_abi_export("memmove")
def pcc_memmove(dst, src, size: i64) -> c_ptr:
    delta: i64 = ptr_diff(dst, src)
    if delta <= 0 or delta >= size:
        forward: i64 = 0
        forward_limit: i64 = size - 8
        while forward <= forward_limit:
            store_i64(dst, forward, load_i64(src, forward))
            forward = forward + 8
        while forward < size:
            store_i8(dst, forward, load_i8(src, forward))
            forward = forward + 1
        return dst
    # Overlapping and dst > src: copy downward, still eight bytes at a time.
    back: i64 = size
    while back >= 8:
        back = back - 8
        store_i64(dst, back, load_i64(src, back))
    while back > 0:
        back = back - 1
        store_i8(dst, back, load_i8(src, back))
    return dst


@c_abi_export("memset")
def pcc_memset(dst, value: i64, size: i64) -> c_ptr:
    # Fill eight bytes per iteration.  A byte-at-a-time fill is what a naive
    # port looks like and it is what the whole runtime pays for: `memset` plus
    # the allocator's zeroing loop were ~23% of all samples in a
    # `pcc1 -> pcc2` emit worker.  The destination is aligned first so every
    # wide store is 8-byte aligned; this module must not assume unaligned wide
    # stores are legal.  Constants stay inline: module-level ints are zeroed in
    # stripped freestanding objects, and helper functions are not permitted
    # here (every function must be a c_abi_export).
    byte: i64 = value & 255
    index: i64 = 0
    head: i64 = (-ptr_diff(dst, null())) & 7
    if head > size:
        head = size
    while index < head:
        store_i8(dst, index, byte)
        index = index + 1
    word: i64 = wrapping_mul_i64(byte, 72340172838076673)
    limit: i64 = size - 8
    while index <= limit:
        store_i64(dst, index, word)
        index = index + 8
    while index < size:
        store_i8(dst, index, byte)
        index = index + 1
    return dst


@c_abi_export("bzero")
def pcc_bzero(dst, size: i64) -> None:
    pcc_memset(dst, 0, size)


@c_abi_export("explicit_bzero")
def pcc_explicit_bzero(dst, size: i64) -> None:
    # The freestanding compilation contract does not run an optimizer that
    # may erase this loop. Keep it separate from memset so the secure-clear
    # symbol remains an explicit byte-store body in both backends.
    i: i64 = 0
    while i < size:
        store_i8(dst, i, 0)
        i = i + 1


@c_abi_export("memcmp")
def pcc_memcmp(lhs, rhs, size: i64) -> i64:
    i: i64 = 0
    while i < size:
        a: i64 = load_i8(lhs, i) & 255
        b: i64 = load_i8(rhs, i) & 255
        if a != b:
            return a - b
        i = i + 1
    return 0


@c_abi_export("memchr")
def pcc_memchr(ptr, value: i64, size: i64) -> c_ptr:
    target: i64 = value & 255
    i: i64 = 0
    while i < size:
        if (load_i8(ptr, i) & 255) == target:
            return ptr_add(ptr, i)
        i = i + 1
    return null()


@c_abi_export("memrchr")
def pcc_memrchr(ptr, value: i64, size: i64) -> c_ptr:
    target: i64 = value & 255
    i: i64 = size
    while i > 0:
        i = i - 1
        if (load_i8(ptr, i) & 255) == target:
            return ptr_add(ptr, i)
    return null()


@c_abi_export("strlen")
def pcc_strlen(ptr) -> i64:
    size: i64 = 0
    while load_i8(ptr, size) != 0:
        size = size + 1
    return size


@c_abi_export("strnlen")
def pcc_strnlen(ptr, limit: i64) -> i64:
    size: i64 = 0
    while size < limit and load_i8(ptr, size) != 0:
        size = size + 1
    return size


@c_abi_export("strcpy")
def pcc_strcpy(dst, src) -> c_ptr:
    index: i64 = 0
    while True:
        byte: i64 = load_i8(src, index) & 255
        store_i8(dst, index, byte)
        if byte == 0:
            return dst
        index = index + 1


@c_abi_typed_export("strncpy", "ptr", ("ptr", "ptr", "i64"))
def pcc_strncpy(dst, src, size: i64) -> c_ptr:
    # Copy at most size source bytes. Once NUL is reached, pad the remaining
    # destination without reading past the source. A full copy adds no NUL.
    index: i64 = 0
    while index < size:
        byte: i64 = load_i8(src, index) & 255
        if byte == 0:
            break
        store_i8(dst, index, byte)
        index = index + 1
    while index < size:
        store_i8(dst, index, 0)
        index = index + 1
    return dst


@c_abi_export("strcat")
def pcc_strcat(dst, src) -> c_ptr:
    length = pcc_strlen(dst)
    pcc_strcpy(ptr_add(dst, length), src)
    return dst


@c_abi_export("strchrnul")
def pcc_strchrnul(ptr, value: i64) -> c_ptr:
    target: i64 = value & 255
    i: i64 = 0
    while True:
        byte: i64 = load_i8(ptr, i) & 255
        if byte == target or byte == 0:
            return ptr_add(ptr, i)
        i = i + 1


@c_abi_export("strchr")
def pcc_strchr(ptr, value: i64) -> c_ptr:
    found = pcc_strchrnul(ptr, value)
    target: i64 = value & 255
    if (load_i8(found, 0) & 255) == target:
        return found
    return null()


@c_abi_export("strrchr")
def pcc_strrchr(ptr, value: i64) -> c_ptr:
    target: i64 = value & 255
    last = null()
    i: i64 = 0
    while True:
        byte: i64 = load_i8(ptr, i) & 255
        if byte == target:
            last = ptr_add(ptr, i)
        if byte == 0:
            return last
        i = i + 1


@c_abi_export("strcmp")
def pcc_strcmp(lhs, rhs) -> i64:
    i: i64 = 0
    while True:
        a: i64 = load_i8(lhs, i) & 255
        b: i64 = load_i8(rhs, i) & 255
        if a != b:
            return a - b
        if a == 0:
            return 0
        i = i + 1


@c_abi_export("strncmp")
def pcc_strncmp(lhs, rhs, size: i64) -> i64:
    i: i64 = 0
    while i < size:
        a: i64 = load_i8(lhs, i) & 255
        b: i64 = load_i8(rhs, i) & 255
        if a != b:
            return a - b
        if a == 0:
            return 0
        i = i + 1
    return 0
