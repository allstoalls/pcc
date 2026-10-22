# Runtime and compiler research directions

Sections 1-4: 2026-10-09, **reconstruction from prior discussion, not
validation**. Sections 5-12: 2026-10-18, concepts from other ahead-of-time
compilers; nothing in them is implemented or measured in pcc yet. The
recommended order and the current state are at the end.

Sections 1-4 preserve four technical directions and the safety contracts needed
to resume them. The original research artifacts, candidate patches, and test
evidence were unavailable during reconstruction. This document does not restore
their exact contents or establish the status of the current implementation.
Unverifiable historical pass counts and performance figures are omitted.
The gates below are proposed future work, not completed checks.

## 1. Internal lifetime verification inspired by Mojo

Use function-local dataflow and explicit call contracts inside PCC without
changing ordinary Python syntax or observable semantics. Start with a shadow
verifier that diagnoses ownership errors without changing generated code.

Safety contracts:

- Track owners, borrows, roots, leases, and moves across normal and exceptional
  control flow. Detect missing and duplicate cleanup, including at joins.
- Prove object liveness, address validity, and container-slot validity
  separately. An owned strong reference does not prove uniqueness or exclusive
  mutation rights.
- Before removing retain/root operations, describe whether a call captures a
  reference, returns a borrow, re-enters user code, changes an address, or
  suspends. Include closures and values that cross `await` or `yield`.
- Preserve aliases, late-bound closure cells, weak references, finalizers, and
  resurrection. Insufficient proof keeps the existing managed path; it must
  not reject otherwise valid Python.

Next gates: recover or explicitly reimplement the verifier in isolation; review
its state transitions; test positive cases and deliberately invalid generated
IR for normal, exceptional, and suspension paths. Establish native semantic
coverage across all five GC backends before enabling any resulting optimization.

## 2. Scope-owned buffers inspired by Confined Pools

Prioritize a waitset-owned event buffer that can be reused across waits.
Short-lived string scratch storage and temporary big-integer output slots are
separate later candidates, each requiring its own escape analysis.

Safety contracts:

- Reuse storage only after the wait or cancellation has completed and the
  backend no longer retains a pointer into it.
- Define ownership through unlock, re-entry, allocation failure, cancellation,
  and destruction. No concurrent borrower may observe recycled storage.
- Do not assume that virtual-thread pinning proves stable carrier affinity.
  Keep the proposed ownership tied to the waitset or call scope.
- Results that outlive a scratch scope need independent storage. A temporary
  output slot is stack-eligible only when the callee does not retain its address.

Next gates: review buffer lifetime and backend pointer retention; exercise
re-entry, cancellation, exceptions, concurrent waits, and thread migration in
native tests. Measure allocation counts, end-to-end throughput, tail latency,
and peak memory on frozen inputs. A simulation or external allocator
microbenchmark cannot establish PCC runtime gains.

## 3. Gateway cleanup and suspension paths

Investigate repeated exit-path root cleanup and function-construction error
handling as sources of excessive generated IR. Consider shared or bulk cleanup
only where it preserves the original ownership operations and their order.

Safety contracts:

- Preserve root coverage and the required cleanup order on normal, exceptional,
  cancellation, and generator-close exits.
- Keep values needed after suspension alive in persistent state. Do not treat
  suspension as final function exit or release a value needed on resumption.
- Preserve exception propagation and observable finalizer behavior; smaller IR
  alone cannot demonstrate equivalence.

Next gates: review the combined cleanup/suspension changes against one exact
baseline; check generated control flow and ownership balance; execute native
normal, error, cancellation, and resume paths across the five GC backends.
Then compile, link, and run the representative gateway application. Report IR
size, compile time, and runtime behavior separately, including any timeout.

## 4. Warm cache checks and runtime build usability

Investigate unnecessary directory scanning before program-cache lookup.
The proposed direction is to validate actual dependencies and import-resolution
context together with compiler and runtime identities, rather than recursively
scanning unrelated files beside the entry script.

Safety contracts:

- Reject cached results when changes to dependencies, import resolution,
  compiler options, or the matching runtime invalidate them. Avoiding work must
  not weaken invalidation.
- Keep cold-build and cache-hit paths distinguishable. Attribute startup,
  validation, compilation, linking, and execution costs separately.
- Provide concise runtime-build progress and an explicit preparation entry;
  reserve internal pass timing for requested diagnostics.
- Distinguish source-development instructions from installed-package usage.
  Discover the matching runtime archive automatically in the normal workflow
  and report missing or incompatible artifacts clearly.

Next gates: restore or reimplement the candidate with dependency and
import-resolution invalidation tests; verify cold builds and repeated cache
hits using the actual supported entry points and packaging layouts. Measure
the full path on each target platform instead of transferring timing claims
between machines.

## Evidence required to resume

Freeze the baseline, candidate, inputs, effective options, compiler, runtime,
and cache identities. Keep host-model tests, IR/object checks, native execution,
and performance measurements distinct. Preserve raw outcomes and explicitly
record failures, skipped gates, and timeouts. No production-readiness or
performance claim follows from this reconstruction alone.


## Concepts from other ahead-of-time compilers (sections 5-12)

Only concepts are taken, restated in Python and pcc terms; pcc keeps ordinary
Python semantics and adopts no other language's rules or idioms. Sources, read
2026-10-18: `vercel-labs/tsc-ts` at `9ab9e28` with its compiler's contract
packages, and the draft `oven-sh/bun#45631` at `2217cacf7`. Neither publishes
evidence that transfers to pcc's speed; every gain below has to be measured in
pcc.

Shared rule: a contract is opt-in. The compiler proves it or rejects the
annotated code with a diagnostic that names the violating path. Unannotated
code keeps the current managed path, so insufficient proof never rejects
otherwise valid Python (section 1's rule). Under CPython the markers are inert
`typing.Annotated` metadata or no-op decorators.

Measured owner (`docs/status.md`, Stage2 throughput): in a pcc1 export replay,
self time was 46% GC roots/barriers, 18% allocator, 13% refcount and 6% TLS.
An imported-name constructor call with keywords cost 17 s per million calls,
5 s positional and 1 s for a same-module call (CPython about 1 s).

## 5. Program-lifetime classes

Concept: instances of a marked class live until the process exits, so the
compiler emits no reference counting, no cycle tracking and no root
registration for values statically known to be such instances. pcc already has
the runtime half: an immortal bit in the object `flags` word
(`pcc/runtime/include/py_runtime.h`) and `pcc_gc_immortalize`. Codegen still
emits refcount and root operations for these values. Immortal values need no
stack-map slot and no registered-slot protocol, which shrinks stack-map steps
1 and 4. Candidates are the compiler's own long-lived objects (AST nodes, IR
values, type and symbol records).

Safety contracts:

- No `__del__` in the class or any subclass; executable builds only; inert
  under CPython and for `import pcc` library use.
- Weak references to instances stay valid because the object stays alive;
  finalizer and resurrection behavior is unchanged because there is no
  `__del__`.
- A relocating backend allocates these objects in a non-moving space; all five
  backends treat them as permanently live and their outputs stay equal.
- Memory is never reclaimed: bound per-worker peak RSS and give any
  long-lived process a restart policy.

Next gates: measure in a pcc1 profile the refcount and root-protocol share
attributable to the candidate classes, and the RSS growth. Prototype one class
family behind an option; check IR for absent refcount and root operations on
typed values; execute natively under GC0-GC4; compare Stage1 time and peak RSS
on frozen inputs.

## 6. Interprocedural may-raise and may-allocate summaries

Concept: compute bottom-up over the call graph which functions can raise and
which can allocate; a caller of a cannot-raise function drops the exception
check, and a call to a cannot-allocate function is not a GC safepoint. pcc has
only the local `_expr_cannot_raise`
(`pcc/frontends/python/codegen/cpy_call_lowering.py`) and `may_raise` on low IR
values. Codegen has 382 `_emit_post_call_err_check` call sites; each check is a
`py_err_occurred` call plus a branch and possibly a cleanup block
(`exception_lowering.py`). One summary serves both exception-edge removal and
stack-map step 2 (safepoint pruning).

Safety contracts:

- Cannot-raise needs no `raise`, no allocation (MemoryError), no fallible
  operation (division, indexing, attribute miss, bignum promotion, user
  `__hash__`/`__eq__`) and no re-entry into user code.
- A decref can run `__del__`, which can allocate and collect; a function that
  may release a finalizable object is may-allocate.
- Recursive call-graph components are may-raise (RecursionError).
- A call that delivers pending signals (KeyboardInterrupt) is may-raise.
- Dynamic dispatch, live imports and cpython-compat calls are may-raise and
  may-allocate.

Next gates: compute the summaries in a shadow pass and report, on the real
closure, the share of call sites proven non-raising and non-allocating before
changing codegen. Then drop checks behind an option, add native tests for each
excluded case (MemoryError injection, `__del__` during decref, recursion,
signals), run GC0-GC4, and measure dynamic `py_err_occurred` calls and Stage2
phase time.

## 7. Closed-world write-escape proof for module and builtins bindings

Concept: a binding that no code in the closed world can write after module
initialization behaves as a constant, so reading it can bind statically; a
construct the analysis cannot see through turns the optimization off for the
affected modules or the whole program.

In pcc, imported names in managed modules are live bindings (`_emit_name`
checks `live_import_name_slot` first), and
`tests/python/test_live_import_namespace_rebinding.py` requires live semantics
for `globals()` writes, module attribute assignment, deletion through the
namespace, `global` deletion and definition rebinding.
`test_layer1_constants_cross_module_static_imports_stay_native` is red on this.

A (module, name) binding is write-free when the closed world has:

- no module-level rebinding after the first definition and no `global` write
  or delete in any function;
- no `mod.name = ...`, `del mod.name`, `setattr`/`delattr` whose target can be
  the module;
- no `globals()`, `vars()`, `__dict__`, `exec`, `eval`, `importlib.reload` or
  `sys.modules` write that can reach the namespace;
- no escape of the module object to libpython, cpython-compat or unknown
  callees.

Write-free names bind statically (direct call or constant); the others keep
the live path. The same proof covers `builtins` (a program that never writes
`builtins.len` can call `len` directly) and lets a dict or list built by
`py_static_aggregate_build` become static immortal data when no alias mutates
it; constant tuples already are (`pcc/runtime/py/py_tuple.py`).

Safety contracts:

- Every shape in the live-rebinding test still takes the live path and
  produces the same output.
- Import cycles: a name read before its defining module finishes initializing
  keeps the live path.
- Identity is preserved: the bound object is the module attribute itself.

Next gates: a shadow analysis listing the write-free names on the pcc closure;
an IR check that they lower to direct calls; the live-rebinding test stays
green and the layer1-constants test turns green; native GC0-GC4; an A/B of the
million-construction probe and of the Stage1 owned-link time. An audit run
under CPython (section 10) confirms no write ever reaches a name the proof
marked write-free.

## 8. Opt-in checked contracts with located diagnostics

Concept: a promise the programmer writes is either proven or rejected at the
line, with the path that breaks it. In pcc, degradation to dynamic lowering is
silent, and the fallback ratchet (`tests/fallback_baseline.json`) counts it per
module after the fact. Recent cases: a module-level set lost its type under
pcc1, mixin attributes aliased host slots, and a `-> None` export degraded to
`("dyn",)`.

Proposal: `typing.Annotated` markers on compiler hot code: native-only
function (no py_cpy, dyn or live-call fallback), fixed-width field (i64),
cannot-raise (section 6) and program-lifetime class (section 5). A violation is
a compile error naming the operation that falls back. Only annotated code can
be rejected, and host pcc0 and pcc1 must report the same result.

Next gates: the native-only marker on a few functions that profiles name as
hot; diagnostics checked on deliberately broken examples; a ratchet that the
marked functions stay clean in host and pcc1 builds.

## 9. Per-stage host/pcc1 oracle with first-divergence attribution

Concept: compare every compiler stage against an oracle on a large corpus, and
count a unit whose earlier stage already differs apart from later-stage
differences. pcc's oracle pair is host pcc0 and pcc1 compiling the same source.
Today's comparisons are ad hoc (the assembly shape diff that found pcc1
emitting no managed reloads), and many tests pin IR text.

Proposal: dump drivers for AST, typed IR, post-pass IR, assembly and stack
maps; run host pcc0 and pcc1 on a frozen corpus; attribute each drift to the
first differing stage and classify it with the AGENTS.md drift categories.
Limits: a same-source oracle catches self-host miscompiles, not frontend logic
errors, and every dump must be deterministic.

Next gates: pick the corpus (closure modules plus small programs from
`tests/python`); check that two host runs produce equal dumps; then add pcc1.

## 10. Test infrastructure concepts

These gate stack-map steps 3 and 4 and address the brittle IR-text pins:

- Collector stress over a program corpus: run every program as it is and with
  a collection at every Nth slow-path allocation, overwriting every freed
  object; a young-generation-only variant finds missing write barriers. It
  finds values compiled code still uses but the collector cannot see, which is
  the risk of removing the registered-slot protocol. pcc has collector knobs
  (`PCC_GC_DEBT_THRESHOLD`, `PCC_GC_MINOR_HEAP_SIZE`, `PCC_GC_PAUSE`) and
  allocator free-list poison, but no dedicated mode or corpus lane; whether the
  knobs can force a collection per allocation is unverified.
- Optimization expectations stated in the test program ("must not call
  `py_cpy_*`", "at most N calls to X", "code size below N") instead of exact
  IR text.
- Analysis scaling budgets: for each input shape that once blew up, the
  compile cost at size N minus the cost at size 0 must stay under noise plus N
  times a per-item budget, in instructions and peak bytes. pcc's history
  supplies the first shapes: dominator OOM on a 72k-block module, 27-minute
  stack-map scans, quadratic text-key recovery, dictionary lookups during
  filling.
- Variant equality: the same corpus under IR passes on/off, stack-map tables
  on/off, inline error edges versus the text oracle, GC0-GC4 and the self
  backend versus the LLVM oracle must print the same output.
- A verdict ledger for known failures that fails when a verdict changes for
  better or worse, which keeps known-broken cases executed in line with the
  no-skip doctrine.
- Seeded Python program generators and mutants of existing programs, compared
  with CPython; fixed seeds guard what worked, fresh per-commit seeds look for
  new failures.
- An audit run under CPython that checks the assumptions the compiler made
  (for example section 7's write-free bindings) while the test suite executes.
- Instruction counts of common operations snapshotted and only allowed to fall.

## 11. Further compiler concepts

- Entry-guarded specialization: when a function's arguments are known at most
  call sites, compile a specialized clone and check the arguments once at
  entry; any other argument takes the generic body. Annotations stay
  non-enforcing, as in Python.
- Budgeted per-site attribute caches for untyped code, keyed by class identity
  and the class-attribute epoch, placed at the sites the static call graph
  ranks hottest. Check what pcc already caches per site first.
- Feature-level runtime trimming: find which runtime features a closed-world
  program can reach and leave the rest out of the executable, with a
  trimmed-versus-whole differential as the gate. Measure first how much of a
  typical pcc executable is unreachable runtime.
- Compile constant regular-expression patterns at build time (low priority).

## 12. Lower-priority candidates

Measure before starting any of these:

- Build shared worker state once instead of rebuilding it in every worker.
  pcc frontend and preload workers each rebuild state from JSON wires
  (`preload_delta_worker.py`). Options: fork after the coordinator builds the
  closed world, with immortal objects so refcount writes keep copy-on-write
  pages shared (not available on Windows), or an mmap-able offset-based image.
- Acyclic classes (provably never on a reference cycle) need no cycle-collector
  bookkeeping: helps only backends with cycle-candidate tracking; the five-GC
  contract is unchanged.
- Exit without teardown: check whether pcc1 spends time freeing at exit;
  `atexit` handlers and stream flushes must still run.
- Compiler object layout: rarely set fields in a lazily allocated side object.
  Measure pcc IR object sizes first (a Stage2 emit once peaked at 54 GB).
- Scheduling: costliest units first and units sharing an expensive dependency
  on one worker. pcc already has size priors and RSS caps; grouping is new.
- Output independent of host CPU count: check pcc's sharding against
  fixed-point determinism.

Not adopted: compressed 32-bit object references (pcc keeps CPython's
`PyObject *` ABI for extensions and five collector layouts); enforcing
annotations or freezing built-ins by changing language behavior (pcc proves
instead); typed forks of third-party packages, which is ecosystem work that
must not displace the P0 goals.

## Order and current state

Recommended order: 7 (removes a measured 5-17x call gap and a red test); from
section 10, collector stress, in-program expectations and scaling budgets
(gating stack-map steps 3 and 4); 6 (feeds step 2); 5 (feeds steps 1 and 4;
measure first); 9 (replaces IR-text pins over time); 8; then sections 11 and
12 after measurement.

State at `shenming/master` `58fe18cc0` (2026-10-18): none of sections 1-12 is
in production source. The only "confined" or "Mojo" mentions under `pcc/` are
unrelated comments; model or simulation results for sections 1-2 are not
repository gains. Related work that did land: `393d7c83d` (GC4 store-buffer
membership retirement, real GC4 witness), `50b8b0979` (AArch64 packed plans
retired when stack-map tables are off), `fae9228d8` (callable publication skips
unrelated FuncDef checks) and `b00d7e918` (parallel runtime archive build).
