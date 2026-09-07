# Investigation: an owned rebind consumes a borrowed local's source reference

## Status
active

## Problem Description
Native optimizer execution on the real py_gen IR crashes in pcc_gc_unpin.
The exact generated caller IR and LR at the failure locate unpin after
simplify_module_text returns. That function initially borrows ir_text into
current, then assigns an owned local copy to current. The fast
pcc_gc_store_root_take path releases current's previous pointer even though
that slot never owned the borrowed value. A sufficiently large input string
is unmapped, exposing the premature release at the caller's unpin.

Predecessor: runtime-module-optimizer-throughput.md. This is a generic compiler
ownership defect, not a reason to rewrite the optimizer's accumulator or to
skip large inputs. The distinct IfExpr ownership case is documented in
pcc1-owned-ifexpr-local-transfer.md.

## Repro
tests/python/test_borrowed_local_owned_rebind.py retains a 200,000-character
caller string, borrows it into current in a callee, then rebinds current from
an owned replacement local. CPython prints `200000 200000 a b`. Native pcc1
2b08f3a7aac1 emits an executable that exits 139 without output.

## Test [CONFIRMED]
The minimized native executable fails. Exact optimizer LR evidence:
/tmp/pcc_owned_optimizer_runtime_probe_20260907/lldb-lr.log. Actual linked
compiler IR (not a standalone emit-only stub):
/tmp/pcc_owned_optimizer_actual_ir/self_backend_input_1.ll, function
user_pcc_native_ir_instsimplify_simplify_module_text.

## Proposals
- No.1 restrict ownership-transferring replacement to slots that cannot hold
  borrowed values [pending]

## No.1 preserve borrowed slots on first owned replacement
### Code Change
Keep the established flag-guarded release/store protocol for a local that can
hold a borrowed root. The unconditional take operation may replace only a
slot whose old pointer is owned or null. Preserve the existing exact-int
protocol fast path for qualified owned slots. Gate all five GC backends and
the native optimizer before resuming optimization experiments.

### pending
No compiler implementation change yet. Both repositories remain uncommitted
at the maintainer's request.

## Update: focused fix verified
assignment_statement_lowering excludes a potentially borrowed local from the
unconditional store_root_take fast path. Its existing ownership flag controls
the replacement instead. The regression first failed with native returncode
-11 (2.73 s); after the fix it passes all five GC modes. The exact-int loop
protocol/promotion packet also passes, including its C runtime reference:
4 tests, 8.61 s. This preserves the existing qualified integer-loop fast path.

The owned optimizer now passes this original unpin site. It exposed a separate
regex-result ownership defect, documented in
native-re-sub-owned-result-raw-scaffold.md. The previously built pcc1 binary
still contains the old compiler source; fresh pcc1 verification remains open.
