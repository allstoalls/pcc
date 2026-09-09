# Gateway memory/performance handoff — 2026-09-09

## Latest continuation: user extended stop to 10% remaining

**STOPPED at 10% remaining**, observed 2026-09-09T13:59:53.054Z. Resume only
after user review/continuation. All task artifact/build processes were absent
in the closing audit. The newest compiler build ended during that audit with
return code 1, not a quota kill: 277.44 s / 4,057,038,848 bytes peak tree RSS;
native-direct multi-codegen worker still raises Bus error. Binary SHA
`e258f906f35cdc535926fd20fa2a448fabfb38913cd44caa705d9709f70b8755` exists at
`build/gateway-calls-owner-stage1-v3-20260909/pcc1` but **is not qualified**.
Its normal-mode gates and final benchmark were not started after the stop.
Use the older tested `b45582507e80` result only for its recorded scope.

This section supersedes the earlier checkpoint instructions and artifact names
below. The user explicitly extended this round's stop to **10% weekly remaining**;
the observed reading during final validation was 13%. Use the final checkpoint
receipt for the closing reading. The low-memory compiler and C100 asyncio goals
remain open. No commit, push, installation or default-flag promotion occurred.

Canonical new evidence: sibling gateway
`benchmarks/results/2026-09-09-owner-checkpoint.json` and its
`2026-09-09-owner-evidence/` sidecars. Raw artifact root (R):
`../pcc-gateway/benchmarks/build/2026-09-09-quota20-tuple`.
`R/peek_quota.py` reads this thread's quota; do not inspect other sessions.

Current fixes and evidence:

- `py_tuple.py`, C mirror and `tuple_zip_lowering.py`: generic `tuple(generator)`
  follows the iterator protocol, roots intermediate owners and propagates errors.
  The native relocation roundtrip now retains its rows. Tuple contents/errors,
  boxed bool, relocation records and structured failure cleanup pass 20 native
  cases across GC0–4 (`final-native-validation.json`).
- `pipeline.py`: release `parallel_codegen_result` after unpacking, so the
  pre-pass IR batch does not survive through linking. Weak-reference owner
  regression and 22 focused tests pass. A complete earlier-source Stage1 now
  takes 321.94 s / 4,198,924,288 bytes peak tree RSS, but it predates later fixes.
- Checked GC0 reference operations retain validation and terminal cleanup while
  avoiding the nonterminal prepare allocation. Previously pending C/Python and
  alias/finalizer gates completed; GC1–4 keep their original path.
- `native_text_modules.py`, set/comparison lowering: own regex compile/sub
  results and temporary arguments; root receivers across argument evaluation;
  preserve dynamic equality ordering/callbacks and `KeyError(actual_key)`.
  GC0 set-failure loop retention of 59,400 bytes per 100 iterations falls to 0.
- `py_obj.py` and C mirror: GC4 graph-leaf objects deliberately lack a tracing
  index. Old root/heap slot cleanup incorrectly rejected these still-live
  allocations. The shared guard now accepts validated managed leaf objects,
  preserving stale/unregistered pointer guards. GC4 set-failure retention of
  29,700 bytes per 100 iterations falls to 0. C/Python root self-store, clear,
  heap sentinel slots and invalid-old-slot regressions pass.
- `native_ir/inline.py`: bounded ordinary-call CFG inlining (32 instructions),
  exact call type checks, cloned names and successor PHI repair. Index callee,
  namespace and PHI users once per function instead of serializing/scanning
  the entire function at each call. Default legacy route remains available.
- `native_ir/simplifycfg.py`: preserve third PHI predecessors, lexical SSA
  replacements and cached predecessor/block indexes during linear merging.
  The latest host selection passes 24 tests; full emitted native optimizer
  output matches host on the frozen real runtime IR.

Latest optimizer gate (`native-post2-gate.json`): host pcc builds a native
optimizer from **R/post2-source**, newer than the benchmark compiler snapshot.
Small differential runs pass GC0–4. On 6,164,629 bytes of real combined runtime
IR, inline-defined/instcombine/simplifycfg/dce take 19.06/6.78/17.19/8.29 s
(51.31 s total), with host-equal output. Full gate including build peaks at
2,394,390,528 bytes and completes in 115.14 s under the original 3 GiB cap;
previous variants timed out or exceeded that cap. **1,096,639,473 bytes of live
heap remain at exit.** This is a remaining owner investigation, not proof that
all residual memory is allocator retention.

Final bounded census (`R/final-heap-census.json`, 42.5 MB raw; compact public
`2026-09-09-owner-evidence/final-heap-census-summary.json`) identifies the GC0
live heap: 1,096,571,030 bytes total, 6,670,217 strings requesting 643,959,836
bytes, and 1,095,784 lists plus backing storage requesting 97,476,976 bytes.
SimplifyCFG alone grows live allocations by 698,783,971 bytes. Every checkpoint
has zero bad headers and equal live bytes before/after walking; final emitted
IR equals the uninstrumented run. External clang builds the diagnostic C
observer against current headers. String grouping overflows (4,362,110 missed
insertions at the last phase); type totals are complete but sample rankings
are partial. Do not mistake the overflow for extra uncounted string bytes.
The actual `_Block.inst_lines()` and `_drop_raw_terminator_line()` methods each
show **0 live-byte growth** across warmed 1,000-call small reproductions. Their
local temporary-list path is not established as the leak; no speculative fix
was applied there. See `R/instlines-probe-run.stdout` and build/run receipts.

### Latest generic call-owner repairs

Further emitted-IR inspection found that `_terminator_targets` stored the
NEW result of `_Block.inst_lines()` in a root without tracking its local
owner, and dynamic argument tuples retained fresh subscript/string results
without consuming their temporary owners. Both fixes are now in live core:

- `unary_call_lowering.py::_call_user` preserves the declared NEW object return
  contract on the final SSA result, including after a GC-root reload. All
  eight `root_result` call sites were checked: declared object-returning user
  functions/methods and class decorators. The existing error cleanup already
  treated this result as owned. Local regression was red at 167,000/461,968
  retained bytes, then green; 32 relevant tests pass, including GC0–4.
- `call_object_lowering.py::_emit_call_args_tuple` now delegates to ordinary
  native tuple literal lowering. That shares marshalling, temporary release,
  moving-GC protection and argument-failure cleanup. The dynamic-call string
  regression was red at 297,000/297,056 bytes; 17 focused tests pass after the
  change. A separate later-argument failure/finalizer/order case passes GC0–4.

Same-input observer comparison (not a throughput benchmark): live requested
bytes 1,096,571,030 -> 799,285,177 with the return fix -> **713,391,304** with
both fixes, a **383,179,726-byte (34.94%) reduction**. Strings fall from
643,959,836 to **343,560,366 bytes**. Full observer build/run tree peak falls
2,378,596,352 -> 1,603,682,304 bytes. Every output equals the earlier native
optimizer IR, and observer walks report zero bad headers and unchanged live
bytes. Detailed string sample tables still overflow. The combined four pass
timings with the observer total 54.92 s; do not claim a speed gain over the
earlier uninstrumented 51.31 s measurement. Remaining 713 MB is unresolved.

Receipts: `R/call-owner-heap-census.json`, `R/calls-heap-census.json`, their
watchdogs, `call-owner-regression-before-v2/after`, `dynamic-args-before/after`
and `dynamic-args-failure-gc`. Public sidecars retain compact census summaries.
The alias-only regex marker experiment had **no effect** and was not applied
to core. Ordinary standalone regex/walrus/early-return probes also had zero
growth; the actual failing scope needed the method return and dynamic tuple
boundaries. Avoid repeating those denied hypotheses.

The new compiler source is frozen at `R/calls-final-source`: post2 plus the
two generic call fixes; diagnostic drivers/probes are excluded. The latest
Stage1 attempt is `build/gateway-calls-owner-stage1-v3-20260909`, with
`R/calls-stage1-watch-v3.json`. Check its terminal receipt before using it.
The v1 and v2 attempts stopped during lock/read-only preflight, before builds;
they are retained separately. The earlier 90-run table remains bound to the
older `b45582507e80` compiler until a fresh candidate is measured.

Compiler artifacts must not be conflated:

- `build/gateway-tuple-owner-stage1-v3-20260909/pcc1`: complete earlier-source
  Stage1, SHA `7b1d7614ec18310b2b12da8fd58ca73d18c55abc838a96277952a65915c927ca`.
- Latest ordinary text-route Stage1 (`gateway-owner-qualified-stage1-20260909`)
  hit the unchanged 4.5 GiB cap at 4,855,726,080 bytes. Separately linking its
  preserved ASM produced `R/recovered-final-pcc1` at 4,155,850,752 bytes. That is
  link recovery, not full-build qualification.
- Latest direct host route (`build/gateway-direct-owner-stage1-20260909`)
  produced pcc1 at 3,978,182,656 bytes / 246.76 s, but its harness exits 1:
  the inherited native-direct emission smoke fails with a worker Bus error.
  **Do not mark this build qualified.** Its normal-mode compile/run prints 42
  and passes the 20 native cases above. Compiler SHA
  `b45582507e80dbf591f1dd2016fecdaaccd345f463193bc408bef8d09571c9a0`.
  `R/direct-pcc-final` and `R/direct-pcc1-final` bind its source snapshot and
  current application runtime. Final three-way benchmarking uses normal mode.
- Latest coherent application runtime is
  `R/py_runtime_leaf_fixed/libpy_runtime_pcc_py.a`, SHA
  `1ee0b1bb5e2b5eca1cbf0d5c7fb69ec03c6f595fa51a0eb6c81313adac698e53`.
  All 170 members use compiler checksum `a92b2af9...`; object emission remains
  external LLVM O0. Incremental runtime rebuilds must use the original frozen
  `compact-stage1-source` compiler, not mix checksums. Pin the archive always.

Coordinator diagnosis of the latest text route: 1.347 GB RSS but only 29.62 MB
live tracemalloc allocations before linking; `gc.collect()` collected nothing.
Darwin malloc reports 18.02 MB in use / 859.83 MB reserved; pressure relief
returns zero. This supports allocator retention/fragmentation in that host
process. No production GC/trim workaround was added. The direct route avoids
transporting the large text batch but its native-direct smoke remains open.

Throughput: the separate 42-run runtime experiment measures 55,894 control ->
77,154 owned second-round QPS (+38.0%), asyncio 82,354, external LLVM-O2 84,960.
It combines 12 hot runtime modules and uses host-owned optimization with
external LLVM emission. It predates the GC4 leaf fix and lacks whole-runtime
GC0–4 qualification; it is **not** the ordinary pcc1/default result. Increasing
inline budget to 64 gave only ~1% and was not promoted. Final normal-mode
90-run numbers are in gateway `2026-09-09-owner-final-three-way.json/.md`.

After the next approved continuation, prioritize the remaining large costs:
native optimizer live-owner census; native-direct smoke failure and complete
compiler build; qualify owned cross-function optimization on the current
runtime before claiming or promoting its QPS. Avoid re-running broad builds
to diagnose small failures. Keep red reproducer, owner measurement, one fix,
focused native/GC checks, then one final comparison. Live HTTP/HTTPS and the
new-source fixed point remain separate unrun gates.

Two diagnostic traps remain: a hyphen in `function-smoke.py` creates an invalid
unquoted LLVM symbol (underscore spelling works); `RuntimeError.__init__` can
mask the underlying compiler exception. The private `py_runtime_raise_trace`
runtime only exposed that error and must never enter normal benchmarks.

## Earlier checkpoints (historical)

The active broader target is to beat asyncio while preserving task cleanup and
GC0–4. This iteration completed compiler-memory fixes, scoped native validation,
a final 90-run sweep and the gateway README update; throughput parity remains open.
User priority: fix the biggest measured owner, show evidence, and avoid unbounded
investigation. Stop work when weekly quota **remaining** reaches 40%, not when
40% has been used. The last read was 51% remaining.

Source and evidence:
- Core HEAD at measurement: d87f85940fe8fc6301f428f74e9b89bdfbac061a plus preserved
  staged/unstaged changes. Do not reset, commit, push or replace installed pcc1.
- Final isolated compiler: build/asyncio-memory-walrus-stage1-20260909/pcc1,
  SHA 0f66351daf95cf9201f6d8e859a5107e32a9e7af2650fbff0dd24f54f61d1a65.
- Source manifest fafea499679be3130af8d773dc3b990a95872bec97d485c37e7553d94e630ba5.
- Current runtime archive SHA 1457c4e642b17f29661a2fe27bb012549c3e88942ba468364b909e9cb6a59821;
  external LLVM object emission, host-helper gaps, no fixed-point qualification.
- Gateway artifact root: benchmarks/build/2026-09-08-asyncio-challenge/.
  memory-walrus-qualification-source is frozen; wrappers pcc1-memory-walrus and
  pcc-memory-walrus bind source/runtime. Every heavy command has a watchdog JSON.
- Public-worktree receipts: gateway benchmarks/results/2026-09-09-*.json;
  core investigation: docs/investigations/native-http-compiler-memory.md.

Measured outcomes:
- Identical 36.26 MB server IR: native optimizer peak 815.8 MB, live strings
  275.3 MB, 52.15 s; output SHA 19c8dbd264941d80050315990f5a1bf934d3b2a6fd302def2b97e22f4b83c2fb.
- Full HTTP compile: native pcc1 187.11 s / 2.35 GB; matched host pcc
  81.46 s / 1.76 GB. Both outputs pass the local HTTP protocol/lifecycle marker.
- 63 pcc1-compiled ownership tests, 294 gateway default tests, native dashboard
  and structured failure/cancellation canaries pass. Exact native node IDs were
  used after a broad -k selector accidentally included host cases and timed out.
- Final C100/zero-wait QPS: host 48,398, pcc1 48,004, asyncio 88,736.
- Fresh exact-artifact profile: provenance/refcounts/barriers/graph locks total
  54.4% of disjoint leaf samples. The shared installed compiler was not changed.

Important fixes and unresolved boundaries:
- Generic temporary ownership in predicates, calls, constructors, indexed loops,
  subscript results, dict.get, comprehensions and literals; parser opcode guards
  and lazy SROA name collection. The investigation records each narrower A/B.
- Module-alias AST constructor no-init optimization now requires all fields;
  omitted ClassType.properties/valueclass defaults otherwise failed natively.
- New condition cleanup exposed walrus targets storing borrowed pointers. The
  shared replaceable-owner slot protocol now gives the binding and expression
  independent references. Regex/alias/finalizer GC0–4 gates pass; the formerly
  failing backend IR emits byte-identical host/native assembly.
- Residual retention, repeated text IR, 7.33 GB compiler-building memory and the
  high-concurrency gap remain open. RuntimeError.__init__ can mask a compiler
  error; recorded but not repaired. Do not revive denied frame/singleton tweaks.

Continue with current code/input identities and the process-tree watchdog and
performance lock. Recheck quota through the current session's rate-limit record;
this session's narrow reader is artifact-root/peek_quota.py. Do not infer quota
from token counts. Stop only task-owned processes and verify no task jobs remain.


## Follow-up: proven references and waiter initialization

New core work is tracked under #188 and documented in
`docs/investigations/vthread-asyncio-throughput-gap.md` (2026-09-09 updates).
The new opt-in `PCC_KNOWN_OBJECT_REFS` selects initialized-object retain/release
and private generator frame access. The generic path stays checked; GC1–4
and terminal deallocation share existing behavior. The compiler cache and
host field inventory include the new flag. Runtime C/Python mirrors and tests
are present in the worktree; no installation, commit or push was performed.

Isolated Stage1:
`build/gateway-compact-refs-stage1-full-20260909/pcc1`, SHA
`788acb37ebab17b5425105df268cdfc183b2f56837072cc141f5ed4224263001`.
The read-only source snapshot privately enables the three prior vthread flags
plus known references; live defaults remain off. Build 371.22s, peak 6.81 GB.
Compiler runtime archive SHA `0dd5e14ab490b21f3fff5ec8e90373fc0e5b463c23164a6be51ebff4dfbbd0ca`.
Do not use the initially rejected `gateway-compact-refs-stage1-20260909` tree.

Gateway artifact root:
`benchmarks/build/2026-09-09-refcount-specialization/`.
Latest application runtime: `py_runtime_waiter_fixed/libpy_runtime_pcc_py.a`,
SHA `3aa7eb02a23e4c2e426a9c96410910c0bf634d0c95e5c645d50b27a0d91a18ac`.
All 170 objects share compiler checksum `a92b2af9...`. This uses external LLVM
object emission. Its only delta from the coherent compiler runtime is the
`py_threading._waiter_clear` fix. Native/host wrappers `pcc1-waiter-fixed` and
`pcc-waiter-fixed` select this application runtime and the frozen compiler
source; older wrappers override an incoming runtime, so do not use them for A/B.

The HTTP audit found a pre-existing mirror bug: fresh malloc storage passed
through retaining root replacement. LLDB and a poisoned-node C harness prove
it; raw initialization now matches C. A stronger regression verifies that
raw bits equal to a live object address do not consume its owner. New C/Python
waiter-pool cases, poisoned storage, and the native local HTTP probe pass on
GC0–4; HTTP unmanaged-ref counter falls 1 → 0. No further waiter fix is pending.

Qualification: 19 native ownership regressions; coherent host/IR gates;
20 C/Python reference/frame cases; 11 waiter initialization/pool cases plus
strengthened alias case; native structured-failure cleanup on GC0–4;
294 gateway defaults. See gateway `2026-09-09-known-reference-qualification.json`
and `2026-09-09-waiter-initialization-audit.json` for exact artifacts/counters.
Compiler benchmark compile, handler, set cleanup and corrected HTTP diagnostic
all record zero unmanaged operations; positive control records one.

Native on/off A/B at C100: 45,459 → 52,314 QPS (+15.1%), instructions -11.5%.
Final three-way has 90 runs: native C1/C10/C100 36,013/50,244/52,213; asyncio
8,509/48,693/83,211. C10 leads in this round; C100 remains 1.59x behind.
Gateway README/results are updated. New profile still has 50.77% object
management leaves; frame-set appears in 17.26% of stacks. A next structural
experiment should measure save/restore ownership traffic before proposing
frame-resident locals, respecting moving collectors and lifetime semantics.
Do not recycle the denied bulk-save/transfer or broad unchecked-pointer ideas.

Remaining: high-concurrency asyncio parity, full source fixed point and
threaded real-wire HTTP/HTTPS qualification before default promotion;
compiler linking peak (6.81 GB), retained textual IR/string costs. The user
requires evidence and prioritizing measured large costs. Stop when weekly
quota remaining reaches 40%; the last observed remaining value was 46%.


## User-directed continuation, stopped at weekly remaining 40%

User explicitly rejected both 6.81 GB compiler build memory and 52,213 QPS.
Both objectives remain open. Stop threshold reached at
2026-09-09T05:33:49.274Z: used 60%, remaining 40%; do not resume resource-heavy
work without the user's continuation/changed quota constraint.

New worktree changes: NativeRelocation uses slots; final executable scans
relocation dictionaries lazily; native-object source validation streams rows
through the same validators (all checks retained); cold linker drops input
buffers and the prepared representation before final image construction;
private multi-module compiler handoff consumes IR after emission. New owner
and weakref tests prove phase release; 80 link/record tests and 86 combined
link/handoff tests pass (overlapping selections, not additive counts). A
2,000-mutation native/packed decoder parity check passes.

Artifact root in gateway: `benchmarks/build/2026-09-09-link-memory-frame-traffic`.
First capture timed out because it used default passes instead of the original
Stage1 harness's explicit passes=off. Exact-config capture then succeeded:
786 ASM inputs plus runtime, assembled once to immutable PCO. Its profiled
link recreates the exact original `788acb37ebab...` binary. The capture's
`.s`/`.pco`, sources, driver commands and checksums are retained for replay.

Census: 5,319,491 native relocation records; 5,323,163 raw rows cost
1,492,172,080 bytes in dictionaries/lists alone. From-sections validation also
created a full reverse projection. Fixed-input links: control 5,610,749,952
bytes; slots/row streaming 4,401,610,752; streaming-validation-only addition
4,665,802,752 (no independent gain); final phase-lifetime candidate
3,579,101,184. Every output has SHA
788acb37ebab17b5425105df268cdfc183b2f56837072cc141f5ed4224263001.
Do not attribute the combined gain to the validation-only variant.

Complete new-source Stage1:
`build/gateway-link-memory-stage1-20260909/pcc1`, SHA
3c92d7999590b10d12a5d16b0096e6120e0cb516e37afd39fa883d735580888d.
358.06 seconds, peak 4,463,919,104 bytes under a 4.5 GiB tree cap (previous
6,810,632,192 bytes). This is progress, not a fixed point or a low-memory
completion claim. Runtime remains `3aa7eb02a23e...`, external LLVM object
emission as previously disclosed. Native benchmark compilation exercises the
new consuming multi-module handoff and runs GC0–4; structured failure cleanup
also compiles/runs GC0–4. **Unresolved:** a separately compiled native object
probe exits `ImportError: No module named pcc.backend.macho_obj`; it does not
prove the new streaming record helpers execute natively. The probe and failure
are in `native-memory-checks.json`; this is partial qualification.

Throughput diagnosis: no new accepted QPS improvement this turn. Per-request
counter deltas (40,000 minus 20,000 requests): frame get 34.45; set 30.42;
changed set 7.08; non-null/non-tagged/non-None get 7.1. This does not support
assuming frame copying can close the remaining 1.59x gap. Generic refcount
entries: incref 221.75, decref 254.26; header-checked 386.51, immortal 151.95;
string 54.11, list 41.23, tuple 0, dict 23, exact INSTANCE tag 0, generator
51.18, virtual thread 81.03, other tags 135.96 (categories overlap immortal).
The diagnostic runtime changes are PRIVATE under `py_runtime_frame_probe`
and `py_runtime_object_probe`, never production changes or QPS evidence.
The runtime/scheduler repeated reference operations are a better next owner
than no-op frame stores. An untested thought was caller-proven immortal retain
elision / validated scheduler references; neither is implemented. Avoid broad
unchecked-pointer changes and honor all GC contracts.

Gateway README now records 4.46 GB and the partial native boundary.
Canonical combined receipt: `benchmarks/results/2026-09-09-link-memory-audit.json`.
No commit/push, installed compiler replacement, default known-ref promotion,
new fixed point, live socket/HTTPS qualification, or new QPS win occurred.


## User-approved 40% to 30% continuation: paused for review

Observed weekly remaining 30% at 2026-09-09T08:50:56.532Z. Stop now; resume
only after user review. Both AGENTS.md files require a pause every 10 percentage
points from the approved checkpoint. No task benchmark/watchdog/artifact
process remained in the stop audit. No commit, push or installation occurred.
Gateway README and `benchmarks/results/2026-09-09-quota30-checkpoint.json`
record this round. Artifact root (R): gateway
`benchmarks/build/2026-09-09-quota30-runtime`.

Live core changes this round:
- `class_gen.py::_classgen_unbox_into_scalar_slot`: boxed bool expected i1 now
  uses truthiness instead of integer unbox/truncate. New constructor regression
  plus existing ownership selections: 7 pass. Recovered native compiler passes
  exact dynamic bool conversion on GC0–4 (`recovered-validation.json`).
- `py_obj.py::py_incref/py_decref`: GC0 keeps pointer/type checks, skips a 56-byte
  prepare record for nonterminal operations, and retains existing terminal
  finish semantics. GC1–4 retain original path. Private prototype: 9 tests pass;
  QPS 52,832 -> 56,438, instructions -7.0%, asyncio 82,351. Live final variant
  adds early NULL/tagged guards; final matching runtime is
  `R/py_runtime_checked_final/libpy_runtime_pcc_py.a`. FINAL GATES INCOMPLETE:
  `checked-final-tests.json` exits 4 because command named nonexistent
  test_gc_refcount_ops.py; no tests ran. R/test_checked_final_mirrors.py is
  prepared but unrun. Run actual known_object_refcounts, walrus_object_ownership
  and constructor_boxed_bool_argument tests plus C/Python GC0–4 parity.

Private experiments: retain-noop yielded no gain; bulk frame creation with
PCC_DISABLE_BULK_GENERATOR_FRAME_INIT=0 reached 58,600 vs 56,836 control
(instructions -5%), not promoted. Direct completion v2 yielded only +2.3%
with 10 GC cases passing; not promoted. First direct-finish run is INVALID
(missing extern). Call-stack diagnostic runtime preserves frame pointers,
not a performance arm. R/lto_reference.py exists but was never run.

New compiler build from R/bool-fixed-source (frozen previous candidate plus
class_gen fix) exceeded unchanged 4.5 GiB tree cap: observed 4,920,393,728 bytes,
331.57 s, MEMORY_LIMIT. Do not report successful full build. Preserved 786 ASM
inputs and recovered own-linker run: peak 3,762,962,432 bytes, exit 0. Binary
R/recovered-bool-pcc1 SHA 1e8ed7b107cb326fed62daeab2ddf42f33e0adc56e994257eee9106964daf320.
Source: core build/gateway-bool-memory-stage1-20260909/source-snapshot.
The extra full-tree peak mainly overlaps waiting compiler and linker memory;
linker alone is similar to previous run. No demonstrated full-build remedy yet.

The original outside-package import probe was misplaced. Internal native
probe succeeds. Stronger real relocation probe exposed bool loss (fixed), then
`tuple(generator)` silently empty. R/tuple_generator_probe.py yields 11 and 22
but emitted execution prints (). No tuple fix written. NativeObject's new
_source_sections(True) uses tuple(_source_relocations(...)), exposing this bug.
Fix generic owner in py_tuple.py and src/py_tuple.c::py_tuple_from_splat:
current fallback assumes length/indexing. Need iterator protocol with explicit
roots and exception/cleanup semantics across GC0–4; preserve list/tuple fast
paths. Do not rewrite the compiler's generator expression as an app workaround.
Do not reuse unrooted list-append iteration lowering without ownership review.

Next after approval: tuple regression/fix and mirror gates; final refcount
gates; real relocation probe; memory owner overlap; final pcc/pcc1/asyncio
comparison. C100 asyncio target and low-memory compiler target remain open.
Always pin PCC_RUNTIME_ARCHIVE: one host probe omitted it and unintentionally
rebuilt the shared runtime this round. All performance arms used private pinned
archives. Preserve that shared artifact/user edits; do not reset. Runtime
incremental builds must use the recorded compiler checksum/source, currently
`benchmarks/build/2026-09-09-refcount-specialization/compact-stage1-source`
(checksum a92b2af96ce95efe8ded06c440b8f8349f0ba8cab8f3ec814a858fcfe4db594c),
and source must be under py_runtime*/py. Runtime objects still use external
LLVM reference emission. R/pcc-final and R/pcc1-final wrappers hard-code the
checked_final archive; do not use those wrappers for runtime A/B.
