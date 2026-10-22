"""pcc-Python owner: bounded buffer pools.

Size-bucketed bounded buffer pools.  A buffer of a requested
size is served from the smallest bucket that fits it; when the bucket is at
its bound the allocation fails rather than growing (bounded memory).  Freeing
returns the buffer to its bucket for reuse, so hot I/O paths avoid per-call
malloc.

Buckets: 32/64/128/256/512/1024 bytes, each with a fixed slot count.  A buffer
is a (ptr, size) pair; the pool returns a pointer whose first 8 bytes hold the
size so Free can recover the bucket.

Owned surface (stable C ABI names):

  pcc_iobuf_pool_init, pcc_iobuf_alloc, pcc_iobuf_free, pcc_iobuf_alloc_count,
  pcc_iobuf_free_count, pcc_iobuf_bucket_used
"""

__pcc_runtime_port__ = True

from pcc.extern import c_abi_typed_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    load_ptr,
    malloc,
    cstr,
    define_global_i64,
    define_global_i64_array,
    define_global_null_ptr_array,
    global_addr,
    load_i64,
    null,
    ptr_add,
    ptr_is_null,
    store_i64,
    store_ptr,
)

def _counter_add(addr, delta: int) -> None:
    cur: int = load_i64(addr, 0)
    store_i64(addr, 0, cur + delta)


define_global_null_ptr_array("pcc_iobuf_buckets", 96)
define_global_i64_array("pcc_iobuf_bucket_counts", 0, 0, 0, 0, 0, 0)
define_global_i64("pcc_iobuf_alloc_total", 0)
define_global_i64("pcc_iobuf_free_total", 0)


def _bucket_size_literal(b: int) -> int:
    if b == 0:
        return 32
    if b == 1:
        return 64
    if b == 2:
        return 128
    if b == 3:
        return 256
    if b == 4:
        return 512
    return 1024


def _bucket_for(size: int) -> int:
    if size <= 32:
        return 0
    if size <= 64:
        return 1
    if size <= 128:
        return 2
    if size <= 256:
        return 3
    if size <= 512:
        return 4
    return 5


@c_abi_typed_export("pcc_iobuf_pool_init", "i32", ())
def pcc_iobuf_pool_init() -> int:
    b = 0
    while b < (6):
        store_i64(ptr_add(global_addr("pcc_iobuf_bucket_counts"), b * 8), 0, 0)
        b += 1
    store_i64(global_addr("pcc_iobuf_alloc_total"), 0, 0)
    store_i64(global_addr("pcc_iobuf_free_total"), 0, 0)
    return 0


@c_abi_typed_export("pcc_iobuf_alloc", "ptr", ("i64",))
def pcc_iobuf_alloc(size: int) -> c_ptr:
    if size <= 0:
        return null()
    b = _bucket_for(size)
    bucket_base = ptr_add(global_addr("pcc_iobuf_buckets"), b * (16) * 8)
    counts = global_addr("pcc_iobuf_bucket_counts")
    used: int = load_i64(counts, b * 8)
    if used >= (16):
        return null()  # bounded: bucket exhausted
    slot = ptr_add(bucket_base, used * 8)
    buf = load_ptr(slot, 0)
    if ptr_is_null(buf):
        # allocate the backing block: 8-byte header (size) + payload
        buf = malloc(_bucket_size_literal(b) + 8)
        if ptr_is_null(buf):
            return null()
        store_i64(buf, 0, _bucket_size_literal(b))
        store_ptr(slot, 0, buf)
        _counter_add(global_addr("pcc_iobuf_alloc_total"), 1)
    store_ptr(slot, 0, null())  # remove from free list
    store_i64(counts, b * 8, used + 1)
    return ptr_add(buf, 8)


@c_abi_typed_export("pcc_iobuf_free", "i32", ("ptr",))
def pcc_iobuf_free(buf) -> int:
    if ptr_is_null(buf):
        return -1
    raw = ptr_add(buf, -8)
    size: int = load_i64(raw, 0)
    b = _bucket_for(size)
    counts = global_addr("pcc_iobuf_bucket_counts")
    used: int = load_i64(counts, b * 8)
    if used <= 0:
        return -1
    used = used - 1
    store_i64(counts, b * 8, used)
    bucket_base = ptr_add(global_addr("pcc_iobuf_buckets"), b * (16) * 8)
    store_ptr(ptr_add(bucket_base, used * 8), 0, raw)
    _counter_add(global_addr("pcc_iobuf_free_total"), 1)
    return 0


@c_abi_typed_export("pcc_iobuf_alloc_count", "i64", ())
def pcc_iobuf_alloc_count() -> int:
    return load_i64(global_addr("pcc_iobuf_alloc_total"), 0)


@c_abi_typed_export("pcc_iobuf_free_count", "i64", ())
def pcc_iobuf_free_count() -> int:
    return load_i64(global_addr("pcc_iobuf_free_total"), 0)


@c_abi_typed_export("pcc_iobuf_bucket_used", "i64", ("i32",))
def pcc_iobuf_bucket_used(b: int) -> int:
    if b < 0 or b >= (6):
        return -1
    return load_i64(global_addr("pcc_iobuf_bucket_counts"), b * 8)
