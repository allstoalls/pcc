# Investigation: native aggregate literal masks erase 64-bit vector lanes

## Status
active

## Problem Description
Fresh native self emitters disagree with host self emission on exact LLVM O2
runtime IR. In py_list_del_slice, nonzero i64 constant vector lanes are emitted
as zero. The prior memory-register emitter reproduces the same wrong words,
so the scalar ALU change did not introduce it. Host/native owned-IR equality
alone missed this vector form. Another py_obj difference is being localized
separately; do not assume it has the same cause.

Predecessor: native-optimizer-wide-integer-projection.md describes the same
raw machine-integer mask failure mechanism in a different compiler subsystem.
int-from-bytes-byteslike-no-libpython.md concerns rejected bytes-like inputs;
this reproducer emits zero without that exception.

## Repro
Emit runtime-ir-o2-five/build_py/py_list.ll through the native indexed emitter
and compare with host pcc. py_list_del_slice differs at 18 MOVZ instructions:
host loads 1..15 and 1..3, native loads zero. This persists using the preceding
native emitter /tmp/pcc_owned_emit_memory_register_20260907/pcc-emit.

## Test [CONFIRMED]
Host/native PCO bytes differ; textual ASM narrows the difference to vector
literal materialization. tests/python/test_native_aggregate_literals.py
compiles the actual aggregate serializer and compares its output with CPython
for i8/i32/i64/i128, negative values, high unsigned values, vectors and pointers.

## Proposals
- No.1 preserve arbitrary-width payloads and masks through object projection [pending]

## No.1 preserve arbitrary-width payloads and masks through object projection
### Code Change
Pending minimal native serializer output and mask evidence. Candidate boundary
is aggregate_literal_to_bytes's (1 << bits) - 1 and its bounded integer parser
return type; retain bounded control widths while preserving payload precision.

### pending
Require focused native regression, all five LLVM-IR PCO byte comparisons,
executed vector results and the broader backend gates. No throughput claim
from the mismatched native-LLVM-IR arm is accepted.

## Update 2026-09-07 — mask hypothesis DENIED; integer-count constructors

The actual native serializer driver agrees with host pcc on individual i64
1, -1 and high unsigned values. The aggregate TypeDesc also reports the
correct kind=array, width=0, count=4 and slot_size=32 in both processes.
The array alone serializes as b''. No mask/source projection change was made.
The first temporary test entrypoint outside pcc failed import closure; the
checked-in generic owned_literal_driver.py reproduces the actual native
compiler module route (22.51s and 20.10s diagnostic runs).

Source chain: bytearray(32) reaches py_bytearray_from_obj with a boxed integer.
Both Python and C implementations lack the integer-count branch and call
_bytes_data(int), py_bytes_len(int), returning null data and zero length.
The resulting empty buffer explains the zero vector lanes; bytearray setitem
also returns -1 without setting an exception on out-of-range writes, so the
serializer continues silently. Track that error-signaling gap separately.

No.2 generic bytes/bytearray integer-count construction [pending]: implement
zero-filled sized allocation for integer and bool, negative ValueError and
integer-size overflow diagnostics in both runtime mirrors, with frontend
post-call error checks. Rebuild the affected runtime module with self and
use it to qualify a fresh native emitter. Keep unrelated runtime members and
application objects fixed for the eventual codegen comparison.

## Update 2026-09-07 — sized allocation passes; adjacent mutation gap

The direct native canary before the runtime fix prints 0, 0, b'' with status 0.
After a native pcc1 library-IR build and native self target-off emission of
py_obj_stubs, it prints 32, 32, four zero bytes. /tmp/pcc_bytes_count_20260907
contains before/after receipts. Both runtime mirrors now recognize integer
and bool counts, reject negative and overflowing lengths, guard allocation
size arithmetic and explicitly clear payload bytes. Frontend constructor
calls now check exceptions after runtime calls.

A broader test confirms negative/overflow exceptions, but finds pre-existing
negative bytearray setitem failures. No.3 normalizes negative indices and
raises IndexError/ValueError for invalid index/byte values in both mirrors.
Its old behavior returned -1 without raising. Keep this recorded separately
from allocation. A boolean multiplier used only to construct the test oracle
also misbehaves; the constructor test now uses len(immutable) as the multiplier
to isolate zero-fill from that unrelated operation. The boolean repetition
gap remains open; it is not claimed fixed.

Target-on emission of the runtime module fails because stackmap metadata's
exceptional successor if.else.4039 is removed by empty-label cleanup.
Target-off emission succeeds. The diagnostic archive is explicitly a single
native self-emitted member overlay with 169 unchanged historical members;
it has a diagnostic receipt, not a production manifest. Do not promote it.

Test naming correction: the new sized-constructor regression is now `tests/python/test_native_bytes_sized_constructor.py`. The existing `test_native_bytes_count.py` tests `.count()` and its original contents are preserved unchanged. Earlier local log commands used the temporary conflicting filename.
