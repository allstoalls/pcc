# Indexed-emission phase totals: unapplied candidate

This is a source-only diagnostic increment against production commit
`b68d422cf134357a16ea79be004c1174c7e39fac` (pcc subtree
`dc63c2a946f533f7b31ce63db2f759a044fdc37f`). It is not a performance fix.
The exact eight-path patch is recoverable from `candidate.patch`; `manifest.json`
binds every baseline/postimage and the patch/evidence bytes. Production remains
unchanged until a separately approved application.

## Why this observation is needed

`CI-READBACK.json` records all four actual Stage1 deadlines from run
[38009075780](https://github.com/ikshengmin/pcc/actions/runs/38009075780), with
artifact-member hashes, completed/input-module counts and censored modules.

| Target host | Completed / 456 | In flight | Not started | Indexed emit / completed codegen |
|---|---:|---:|---:|---:|
| macOS ARM64 | 412 | 1 | 43 | 82.29% |
| Linux ARM64 | 440 | 2 | 14 | 62.62% |
| Linux x86-64 | 384 | 4 | 68 | 46.97% |
| Windows x86-64 | 410 | 4 | 42 | 43.10% |

These are overlapping worker elapsed totals, not CPU or stage wall time. Inference
sums to only 7.6–21.4 worker-seconds per platform. Windows call-expression and
AArch64-call input modules finished indexed emission but not their workers;
Linux x86-64's macho_obj worker did the same. Later assembly, object encoding,
writing or subsequent worker work remains unseparated. Input-module names are
not compiler hot-function identities. Fourteen modules, including class_gen,
never started codegen on any platform; no final pcc1 output was produced.

The old indexed-emission timer includes post-backend frontend release/GC/freeze
when enabled; macOS's actual guard records that flag enabled. Mac native-section
emission also already assembles machine words and builds sections internally.
Its later assemble_sections call merely copies finalized section lists. Linux
ARM and both x86 targets assemble emitted text later. Identical timer names
therefore do not mean identical architectural work across these routes.

Production has no elapsed backend subphase telemetry. The existing ARM debug
trace prints untimestamped per-function markers. Existing general profile
recorders retain event lists and are not wired into these backend calls.

## Minimal instrumentation

Reuse `PCC_PY_FRONTEND_WORKER_TIMING` without a new environment knob. A timed
module owns one accumulator containing exactly thirteen integer nanosecond sums.
Emit one best-effort stderr line after the existing codegen timer stops, including
its exception path. The line names module, resolved target, selected direct
route, codegen-scope completion and clock validity. No stdout, TSV/checkpoint or
object format changes are made.

The default disabled path creates no accumulator/list, makes no new clock call,
and performs no new environment lookup or counter traversal. Existing function
loops add guarded scalar timing only; no per-function/instruction records,
printing, rankings, global cache or IR retention are added.

| Field | Scope / relation |
|---|---|
| frontend_generate | Existing codegen.generate call; contains old capture timing |
| frontend_release_pre | Existing first frontend clear/GC call, before indexed emit |
| backend_call_inclusive | Actual selected backend call, excluding diagnostic formatting and later GC |
| prepare_verify | Existing parsed/indexed verifier |
| prepare_layout | Existing function preparation, slots and module symbols |
| stackmap_plan | Actual plan calls, accumulated in unchanged execution order |
| function_emit_inclusive | Actual function emission and existing chunk/native-sink consumption |
| backend_finalize | Post-function stackmap/unwind/section finalization and text join |
| frontend_release_post | Existing graph deletion, GC and optional freeze |
| object_assemble | Existing owned assembler; finalized Mac route is section-list extraction |
| object_encode | Existing ELF/COFF/native-object container encoder |
| object_write | Existing direct object file write and close |
| function_setup | Prologue and kernel acquisition; nested inside function_emit_inclusive |

The backend child fields are inside backend_call_inclusive. function_setup is
also inside function_emit_inclusive; never add both parent and child as disjoint
work. Backend residual is backend_call minus prepare_verify, prepare_layout,
stackmap_plan, function_emit_inclusive and backend_finalize. It includes globals,
ordering, target passes, capture cleanup and uninstrumented bookkeeping; it is
not named register allocation. Existing codegen_ms and indexed-emit definitions
are unchanged and include the small enabled diagnostic overhead.

No algorithm, root/ownership equation, mutation/GC ordering, release boundary,
pass, resource guard, thread policy or timeout changes. X86 retains per-function
plan → emit → retire order. ARM capture/arena finally blocks remain in place.
Native perf_counter_ns already has the owned time import, ABI and implementation;
its zero-on-failure sentinel is now treated as invalid diagnostic timing.

Unfinished scopes contribute no invented duration. An ordinary compiler failure
reports codegen_complete=0; a killed process may emit no summary at all. The
summary precedes later .ll/checkpoint work, so codegen_complete=1 is not worker
or Stage1 success. Clock/report failure cannot replace compiler results/errors.

## Review and first gate

Independent source review is complete with no remaining concrete blocker. It
identified the owned native clock's zero failure sentinel; the frozen helper and
focused test explicitly cover it. Source-only AST and patch reproduction checks
do not count as execution. All 36 new focused host cases and native/CI behavior
are UNRUN. The tests use fake clocks and stubbed
backend/object functions; they do not invoke a compiler, native program or FFI.
They cover fixed sums, zero/failing clocks, once-only/error-safe reporting,
existing timing-policy placement, four-target forwarding, packed-stackmap
options, assembler/encoder order, result identity and exception identity.

Coordinator's bounded first gate, in one ordinary independent exact-source copy:

```sh
python -m pytest -x -n0 -q tests/python/test_indexed_phase_timing.py
```

Keep the existing shared lock, 300-second host deadline, 4 GiB AS/NPROC=0
single-process boundary and 4 GiB free reserve. Actual collection must confirm
36 selected cases; retain any failure. No local heavy profile, runtime rebuild,
full-module measurement or additional rounds are part of this packet.

After source review and the focused gate, the intended next evidence is ordinary
real CI on the exact applied source. It must reveal which backend/assembler
phase actually dominates before another optimization is selected. Host stubs do
not qualify native helper behavior, all-GC semantics, complete Stage1 or speedup.
No denied pcc1 compact-span or enhanced cold-GC4 diagnostic is proposed.
