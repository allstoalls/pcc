"""pcc-Python owner: SPSC bounded ring queue.

A single-producer single-consumer Lamport ring buffer with the cached-index
optimization: the producer caches the consumer's dequeue
index (and vice versa) so the full/empty checks rarely touch the other side's
cache line.  The pcc virtual-thread ready queue can use this as a preallocated
bounded FIFO (no per-node malloc on the hot path).

The ring is a fixed 256-slot (2^8) buffer of pointer values; head/tail are
monotonic i64 counters and the slot index is `counter & mask`.  Enqueue returns
0 on success or -1 when full; Dequeue returns the pointer or NULL when empty.
The queue is single-producer/single-consumer: no internal locking.

Owned surface (stable C ABI names):

  pcc_spsc_init, pcc_spsc_enqueue, pcc_spsc_dequeue, pcc_spsc_empty,
  pcc_spsc_full, pcc_spsc_count
"""

__pcc_runtime_port__ = True

from pcc.extern import c_abi_typed_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    load_ptr,
    cstr,
    define_global_i64,
    define_global_null_ptr_array,
    global_addr,
    global_load_ptr,
    load_i64,
    null,
    ptr_is_null,
    ptr_add,
    store_i64,
    store_ptr,
)

define_global_null_ptr_array("pcc_spsc_buffer", 256)
# head/tail/cached counters live in one global so the module is self-contained.
define_global_i64("pcc_spsc_state0", 0)  # head
define_global_i64("pcc_spsc_state1", 0)  # tail
define_global_i64("pcc_spsc_state2", 0)  # cached_head (producer view)
define_global_i64("pcc_spsc_state3", 0)  # cached_tail (consumer view)


@c_abi_typed_export("pcc_spsc_init", "i32", ())
def pcc_spsc_init() -> int:
    store_i64(global_addr("pcc_spsc_state0"), 0, 0)
    store_i64(global_addr("pcc_spsc_state1"), 0, 0)
    store_i64(global_addr("pcc_spsc_state2"), 0, 0)
    store_i64(global_addr("pcc_spsc_state3"), 0, 0)
    i: int = 0
    buf = global_addr("pcc_spsc_buffer")
    while i < 256:
        store_ptr(ptr_add(buf, i * 8), 0, null())
        i += 1
    return 0


@c_abi_typed_export("pcc_spsc_enqueue", "i32", ("ptr",))
def pcc_spsc_enqueue(value) -> int:
    if ptr_is_null(value):
        return -1
    head = global_addr("pcc_spsc_state0")
    tail = global_addr("pcc_spsc_state1")
    chead = global_addr("pcc_spsc_state2")
    tail_v: int = load_i64(tail, 0)
    if tail_v - load_i64(chead, 0) > 255:
        store_i64(chead, 0, load_i64(head, 0))
        if tail_v - load_i64(chead, 0) > 255:
            return -1  # full
    buf = global_addr("pcc_spsc_buffer")
    store_ptr(ptr_add(buf, (tail_v & 255) * 8), 0, value)
    store_i64(tail, 0, tail_v + 1)
    return 0


@c_abi_typed_export("pcc_spsc_dequeue", "ptr", ())
def pcc_spsc_dequeue() -> c_ptr:
    head = global_addr("pcc_spsc_state0")
    tail = global_addr("pcc_spsc_state1")
    ctail = global_addr("pcc_spsc_state3")
    head_v: int = load_i64(head, 0)
    if head_v >= load_i64(ctail, 0):
        store_i64(ctail, 0, load_i64(tail, 0))
        if head_v >= load_i64(ctail, 0):
            return null()  # empty
    buf = global_addr("pcc_spsc_buffer")
    slot = ptr_add(buf, (head_v & 255) * 8)
    value = load_ptr(slot, 0)
    store_ptr(slot, 0, null())
    store_i64(head, 0, head_v + 1)
    return value


@c_abi_typed_export("pcc_spsc_empty", "i32", ())
def pcc_spsc_empty() -> int:
    head = load_i64(global_addr("pcc_spsc_state0"), 0)
    tail = load_i64(global_addr("pcc_spsc_state1"), 0)
    if head >= tail:
        return 1
    return 0


@c_abi_typed_export("pcc_spsc_full", "i32", ())
def pcc_spsc_full() -> int:
    head = load_i64(global_addr("pcc_spsc_state0"), 0)
    tail = load_i64(global_addr("pcc_spsc_state1"), 0)
    if tail - head > 255:
        return 1
    return 0


@c_abi_typed_export("pcc_spsc_count", "i64", ())
def pcc_spsc_count() -> int:
    head = load_i64(global_addr("pcc_spsc_state0"), 0)
    tail = load_i64(global_addr("pcc_spsc_state1"), 0)
    return tail - head
