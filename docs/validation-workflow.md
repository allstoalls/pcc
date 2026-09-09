# Validation workflow

Use the relevant section after locating current source and tests. These are
procedures, not evidence that today's checkout passes. Inspect the selected
test/harness before choosing its timeout and cache budget.

## Focused diagnosis

1. Reproduce one deterministic failure with `-x -n0 -vv --tb=short`, an explicit
   timeout and a named log. Replay a worker when a wrapper hides its stderr;
   improve empty diagnostics before testing further hypotheses.
2. Inspect the failing source, generated IR and execution boundary. Fix one
   mechanism; run its regression and a sensitive integration case before
   expanding shared edits. Scan for the same shape before another costly build.
3. Verify outputs, errors and cleanup. A pcc1 fix needs a pcc1-compiled program
   exercising the changed feature through emission, linking and execution.
   Host tests, `--emit-llvm` and successful compiler exit prove narrower claims.
4. Remove temporary instrumentation or promote it to a tested feature. Record
   incomplete gates; a small reproducer does not qualify the whole toolchain.

Check these invariants in current source when the corresponding path changes:

- C signedness and compile-time evaluation are distinct from IR integer width.
  Test downstream division, shifts, comparisons and casts. Invalidate parser
  caches when grammar/lexer changes would otherwise reuse stale tables.
- Use the narrow Python lowering mixin; keep `layer1.py` a facade. Fix malformed
  CFG/type/ownership IR at its producer. Explicit owned optimization passes are
  a separate layer, not a way to conceal semantic lowering bugs.
  `postprocess_ir_text()` remains limited to the narrow `va_arg` lowering gap;
  do not add semantic repairs there.
- Calls return owned references. A returned borrowed value must obey the
  callee's retain contract; do not fix it by suppressing caller cleanup.
  An `id()`-keyed cache must keep its key objects alive.
- Derive layouts from runtime declarations. Preserve pointer barriers, error
  checks, finalization/resurrection and root state at CFG joins; an unreachable
  block can still participate in stack-map analysis.
- Each changed GC backend needs a focused gate while preserving backend 0 and
  C/pcc-Python differential equality. Keep backend-specific PR scope separate.
  Consult the relevant upstream implementation when changing its ported algorithm.
- Self-backend record-family changes require
  `tests/python/test_pcc_record_inventory_tool.py`. Classify new classes and
  adapters with their normal-path policy; do not relax the inventory to hide
  reachable representations or leave diagnostic adapters uncounted.
  Keep every concrete class visible to the stage graph, register diagnostic
  constructor owners, and require zero diagnostic-adapter uses on normal paths.

## Heavy builds, timing and bootstrap

Use the [existing tools](development-tools.md). Heavy runs hold
`build/.pcc-performance.lock`, freeze relevant source/runtime inputs and use
isolated outputs. Record the expected cache/time envelope.
`scripts/run_process_tree_sample.py` provides a process-group watchdog,
descendant RSS cap, durable logs and progress; inspect its current arguments.
Do not edit the input closure or run tests rebuilding the same runtime archive
while a measurement/bootstrap is active.

Use `gtimeout` for bounded commands when available. A fallback watchdog must
remain alive, own a child process group, and terminate then kill that group on
expiry. Alarm-then-`exec` is not an acceptable heavy-run watchdog. After timeout,
check and stop only the children of your own run.

Long pytest runs need live node IDs and failure tracebacks saved before the
summary. Use `scripts/pytest_live_report.py` or shard to fit the watchdog.
Every invocation needs `-x`; a timed-out partial run is not green evidence.

Broad validation begins after focused checks pass, implementation is complete
and overlapping inputs are stable with identities recorded. Build dependent
stages in order: pcc0 -> pcc1 -> pcc2 -> pcc3. Stop at the first failed boundary.
Do not discover bugs with five cold chains or keep widening timeouts. Diagnose
one backend/worker, then use normal independent scheduling for final validation.

Critical parser/frontend/self-backend/runtime-object/bootstrap changes require
relevant native/bootstrap qualification. The five files
`tests/python/gc/test_pcc_bootstrap_full_gc{0..4}.py` are integration gates.
Recorded baseline checks do not replace a new-source fixed point.

Commit qualification includes:

```bash
gtimeout 120s env -u LC_ALL uv run pytest tests/python/test_bootstrap_gate_baseline.py -x -n0 -vv --tb=short
gtimeout 120s env -u LC_ALL uv run pytest tests/python/test_fallback_baseline.py tests/python/test_ir_py_fallback_baseline.py -x -n0 -vv --tb=short
```

Installation/release needs the current installer/promotion gates. Keep
experimental compilers isolated until qualified. Existing shared binaries and
baseline JSON files do not automatically qualify today's source.

## Evidence and completion

Bind receipts to source, effective options, compiler, runtime/archive and
artifact hashes. Inspect actual cache invalidation rather than trusting mtimes
or an old description. Execute outputs; an early crash is not a speedup.
Profile working processes without slowing the control arm. Separate CPU from
blocked samples and validate symbol/image identities.

Measure the claim: IR effects, instructions, compile throughput, application
QPS, RSS and fixed-point agreement are different evidence. Recheck remaining
fallbacks, skipped gates and open failures before closing work. A regression
is not a correctness cost without controlled attribution; a local improvement
does not close a larger performance target.

A requested conceptual migration closes only after all supported hot-path
instances are removed. Calling a remaining representation cold/diagnostic/
unsupported requires all four: unreachable from supported normal execution,
zero normal-path uses in a counter/inventory, an exact regression for its lazy
adapter, and named evidence. A small Amdahl share or construction-only lifetime
does not waive those requirements.

Structural migration and speed acceptance are separate. A required migration
with less than a 1.05x gain may be retained only with exact output/diagnostics,
no meaningful regression in deterministic CPU/instruction/memory signals, and
removal of named architectural debt. A stable performance regression remains
rejected regardless of the representation's elegance.
