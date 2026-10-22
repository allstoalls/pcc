# Slot-status fixture: public collection dispatch successor

This is a test-only correction to the preserved cold-slot candidate. It changes
exactly two C statements from `py_gc_collect()` to `pcc_gc_collect(-1)`, at the
same collection points. Every exception, root, refcount, identity, output and
collector-selection assertion is unchanged. The strict native collection-event
validator is also unchanged.

## Why the correction is necessary

The preceding native C run returned zero with its exact success marker and empty
stderr, completing its C assertions. Its strict observation gate nevertheless
failed: the active JSON log contained 77 `store_ptr` events and no collection
events. The second collector run was held. These results remain preserved.

Source and retained emitted IR explain the mismatch. `py_gc_collect` is the
raw backend-0 cycle collector in `freestanding_gc_backend0_collector.py`; it
does not emit the required collect-start/stop events or dispatch other backends.
The public `pcc_gc_collect` wrapper in `py_obj.py` selects the active backend and
emits those events. Its public ABI is `int64_t pcc_gc_collect(int32_t reason)`,
also verified in the already-built runtime IR. The `-1` argument matches the
compiler's existing no-argument `gc.collect()` lowering. The current runtime
uses reason as telemetry; backend selection comes from `_gc_backend_fast`,
independently of that argument.

This repairs the fixture's original five-collector coverage bug. It does not
reinterpret the earlier observation failure as a pass, prove that the old raw
call swept objects, or weaken the requirement for actual collector events.

## Exact source and runtime relation

`test-only-dispatch.patch` applies to the earlier candidate's native test.
The complete new test postimage is supplied as
`test_slot_call_status_reporting_native.py`. The manifest binds both file hashes
and the patch. The compile driver requires the new test to equal the frozen old
test after exactly the two collector substitutions, and requires `status.c` to
equal its `_HARNESS` literal.

The test postimage is an explicit external input. All PCC imports and real
include paths still come from the unchanged frozen compiler source. Its full
inventory remains the original one; no new inventory is substituted into the
old runtime receipt. Production compiler/runtime bytes are unchanged, so the
same strictly admitted 191-member threaded/atomic runtime is used. No new source
copy, source overlay, runtime build or production activation is required.

The unchanged original `_sites` and `_InlineReference` definitions generate the
two reporting probes. The former inline method's AST still matches the canonical
production preimage. Both reporting implementations execute in one combined ELF.

## Root and ownership preservation

All four C slots are registered before either collection point. After each
fresh-error case, the formatted exceptions remain in the registered owning
`roots[1]` and `roots[2]` slots while TLS and `roots[0]` are cleared. The final
collection also occurs before frame leave. No managed object is moved into an
unregistered C temporary across the new public call. The exception/refcount/root
assertions are byte-identical to their predecessors. Actual GC1 behavior still
requires execution; source review alone cannot pass it.

## Compile and conditional native gates

The ordinary `CEvaluator` entrypoint, single C translation unit, real includes,
internal preprocessor, normal C frontend passes, original `optimize=False`
backend setting, owned object writer and ELF linker remain unchanged. The driver
also inspects the emitted C IR: exactly two public `i64(i32)` collection call
sites and no direct raw `py_gc_collect` call are required. Both the explicit link
argument and the emitter's actual runtime selection must use the same archive.

Compile limits: 300 seconds total, hard 4 GiB AS, hard NPROC=0, 4 GiB continuous
free-space reserve, 512 MiB output reservation and exclusive shared lock. Use the
separately reviewed bootstrap with the pinned one-shot stdlib ctypes
initialization allowance. All later process/load/symbol/call audit denials and
the swallowed-denial failure check remain active. The existing forbidden-Popen
mock must also have zero calls. No gate was executed while preparing this packet.

Driver arguments after the fixed Python `-I -S -B` bootstrap:

```text
compile_fresh_exception.py --source UNCHANGED_COMPILER_SOURCE
  --canonical CANONICAL_SOURCE --runtime-output MATCHED_RUNTIME_OUTPUT
  --output FRESH_COMPILE_OUTPUT
```

After compile PASS and a separately preserved exact-ELF binding, run the new
binary once for GC0 and once for GC1, serially. The fixed sole argument is the
selected collector. Keep the 10-second child alarm, 15-second outer guard, hard
4 GiB AS/NPROC=0 and 4 GiB free reserve. Require exit zero, exact
`slot-status-runtime-equal\n`, empty stderr, nonempty collection events whose
observed backend equals the requested one, and clean reaping. Stop on failure.

GC2–4, native pcc1, macOS, parallel frontend execution and full qualification
remain open. The new compile/native gates are UNRUN at packet creation. This is
semantic coverage and fixture repair, with no new performance claim.
