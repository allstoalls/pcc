"""pcc-Python owner: io_uring submission/completion queue logic.

The full io_uring backend needs mmap (SQ/CQ ring mapping) + the raw Linux
syscalls (io_uring_setup/enter/register); that layer is Linux-only and lands
with the ELF/Linux toolchain.  This module owns the ABI data layer and the
submission/completion queue INDEX LOGIC — the ring-arithmetic that both the
kernel ABI and a userspace test harness share — so it is fully testable on
any host.

sqe (64 bytes):  opcode@0 u8, flags@1 u8, ioprio@2 u16, fd@4 i32,
                 off@8 i64, addr@16 i64, len@24 u32, rw_flags@28 u32,
                 user_data@32 i64, ... (tail 24 bytes opaque)
cqe (16 bytes):  user_data@0 i64, res@8 i32, flags@12 u32

Ring layout (per io_uring spec): SQ head/tail/mask/array offsets are read
from the mmap'ed params; this module works on a caller-provided ring
descriptor of 5 words: sq_ring_mask, sq_entries, sq_tail, sq_head, sq_array
pointer.

Owned surface (stable C ABI names):

  pcc_uring_sqe_init, pcc_uring_submit_sqe, pcc_uring_sq_ready,
  pcc_uring_cq_peek, pcc_uring_cq_advance, pcc_uring_cq_ready
"""

__pcc_runtime_port__ = True

from pcc.extern import c_abi_typed_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    cstr,
    store_i8,
    load_i32,
    load_i64,
    load_ptr,
    null,
    ptr_add,
    ptr_is_null,
    store_i32,
    store_i64,
    store_ptr,
)


@c_abi_typed_export("pcc_uring_sqe_init", "i32", ("ptr", "i32", "i32", "i64", "i64", "i32"))
def pcc_uring_sqe_init(sqe, opcode: int, fd: int, off: int, addr, len: int) -> int:
    if ptr_is_null(sqe):
        return -1
    store_i8(sqe, 0, opcode)  # opcode
    store_i8(sqe, 1, 0)  # flags
    store_i8(sqe, 2, 0)  # ioprio low
    store_i8(sqe, 3, 0)  # ioprio high
    store_i32(sqe, 4, fd)  # fd
    store_i64(sqe, 8, off)  # off
    store_ptr(sqe, 16, addr)  # addr
    store_i32(sqe, 24, len)  # len
    store_i32(sqe, 28, 0)  # rw_flags
    store_i64(sqe, 32, 0)  # user_data (caller sets via submit)
    return 0


@c_abi_typed_export("pcc_uring_submit_sqe", "i32", ("ptr", "ptr", "i64"))
def pcc_uring_submit_sqe(ring, sqe, user_data: int) -> int:
    """Write one sqe into the submission queue and advance SQ tail.
    ring layout: sq_ring_mask@0, sq_entries@8, sq_tail@16, sq_head@24,
    sq_array@32 (pointer to the array of sqe indices)."""
    if ptr_is_null(ring) or ptr_is_null(sqe):
        return -1
    mask: int = load_i64(ring, 0)
    tail: int = load_i64(ring, 16)
    head: int = load_i64(ring, 24)
    if tail - head > mask:
        return -1  # SQ full
    array = load_ptr(ring, 32)
    if ptr_is_null(array):
        return -1
    # index into the caller's sqe table (array entry = sqe slot index)
    slot: int = tail & mask
    store_i64(sqe, 32, user_data)
    store_i64(array, slot * 8, slot)
    store_i64(ring, 16, tail + 1)
    return 0


@c_abi_typed_export("pcc_uring_sq_ready", "i64", ("ptr",))
def pcc_uring_sq_ready(ring) -> int:
    if ptr_is_null(ring):
        return -1
    return load_i64(ring, 16) - load_i64(ring, 24)


@c_abi_typed_export("pcc_uring_cq_peek", "i32", ("ptr", "ptr", "ptr", "ptr"))
def pcc_uring_cq_peek(ring, cqe_out, res_out, user_data_out) -> int:
    """Peek the oldest completion.  ring cq layout: cq_ring_mask@40,
    cq_entries@48, cq_tail@56, cq_head@64, cqe_array@72."""
    if ptr_is_null(ring) or ptr_is_null(cqe_out) or ptr_is_null(res_out) or ptr_is_null(user_data_out):
        return -1
    mask: int = load_i64(ring, 40)
    tail: int = load_i64(ring, 56)
    head: int = load_i64(ring, 64)
    if head >= tail:
        return 0  # empty
    array = load_ptr(ring, 72)
    if ptr_is_null(array):
        return -1
    idx: int = head & mask
    cqe = ptr_add(array, idx * 16)
    store_ptr(cqe_out, 0, cqe)
    store_i64(res_out, 0, load_i32(cqe, 8))
    store_i64(user_data_out, 0, load_i64(cqe, 0))
    return 1


@c_abi_typed_export("pcc_uring_cq_advance", "i32", ("ptr",))
def pcc_uring_cq_advance(ring) -> int:
    if ptr_is_null(ring):
        return -1
    head: int = load_i64(ring, 64)
    store_i64(ring, 64, head + 1)
    return 0


@c_abi_typed_export("pcc_uring_cq_ready", "i64", ("ptr",))
def pcc_uring_cq_ready(ring) -> int:
    if ptr_is_null(ring):
        return -1
    return load_i64(ring, 56) - load_i64(ring, 64)
