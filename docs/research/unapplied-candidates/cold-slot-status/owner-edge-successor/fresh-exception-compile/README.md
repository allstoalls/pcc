# Fresh slot-status exception compile gate

This unapplied experiment compiles the existing C fixture without changing its
source or assertions. It compares the candidate's shared reporter with the exact
former inline method on the same candidate compiler and matched runtime. It does
not activate the candidate in production.

## Scope and inputs

- `status.c` is exactly `_HARNESS` from the frozen native regression test.
- The original `_sites` and `_InlineReference` definitions generate the two
  probe modules, including the no-frame and source-frame paths. The former
  method's AST is checked against the canonical production source.
- Ordinary `CEvaluator(backend="self", target_triple=host)` owns the single C
  translation unit, real runtime/fake-libc includes, internal preprocessing,
  normal C frontend passes, owned assembly/object emission and ELF linking.
- `optimize=False` and disabled target passes preserve the original native
  fixture configuration. No production algorithm or worker threshold changes.
- The exact 191-member, threads-enabled/atomic runtime is admitted normally.
  Both the explicit link argument and the emitter's actual `_ensure_runtime`
  selection must resolve to that archive. Native provisioning stays prohibited.
- Complete source inventories are checked by the coordinator before and after;
  this driver additionally binds its input files and imported PCC source roots.

## Process and ctypes boundary

The one-unit C path stays serial. `CEvaluator` imports ctypes declarations but
does not use its foreign-execution or library-discovery functions on this route.
The separately reviewed bootstrap must permit only the already-qualified,
hash-pinned CPython ctypes initialization, including its current-process handle.
After initialization, process creation and ctypes library/symbol/call audit
events remain denied; swallowed denials fail the outer gate. This is a narrow
trusted-source capability boundary, not a general arbitrary-code sandbox.

The previous diagnostic bootstrap is not authorized by this packet unchanged:
its pinned runner hash and scope require a separately reviewed successor.

## Execution limits and command

One compile-only run: 300 seconds total, hard 4 GiB address space, hard NPROC=0,
4 GiB continuous free-space reserve, 512 MiB output reservation, exclusive shared
performance lock. The fixed CPython 3.15 interpreter uses `-I -S -B`; its approved
bootstrap supplies the exact script hash and locked site-packages directory.

Driver arguments after that bootstrap:

```text
compile_fresh_exception.py --source CANDIDATE_SOURCE --canonical CANONICAL_SOURCE
  --runtime-output MATCHED_RUNTIME_OUTPUT --output FRESH_COMPILE_OUTPUT
```

These are explicit caller-provided paths, with identities pinned in
`manifest.json`; the output must be fresh and outside every input root. No test,
compiler or native command was run to prepare this packet.

## Conditional native gate

Compilation alone cannot pass the runtime assertions. After compile PASS, a new
exact-ELF manifest may admit two executions of the unchanged resulting binary,
one each for GC0 and GC1. The fixed argument must be exactly the selected GC
number. Use the already-reviewed same-PID descriptor execution with 10-second
child alarm, 15-second outer guard, hard 4 GiB AS/NPROC=0 and 4 GiB free reserve.
Require exit zero, exact `slot-status-runtime-equal\n` stdout, empty stderr,
nonempty collection events whose observed backend matches the requested one,
and complete reaping/cleanup receipts.

The unchanged C assertions check fresh RuntimeError class, exact formatted
message/source-frame equality and ordering, borrowed-raise refcount equality,
root-count preservation, and pending-exception pointer/frame identity. Both
reporting implementations execute inside each native run. The earlier genuine
Python callsite gate separately covers owned temporaries and pending errors.
This C fixture is generated-probe coverage; it is not a new ordinary Python
callsite or a whole-Stage1 timing measurement.

GC2–4, native pcc1, parallel frontend execution and the full qualification suite
remain outside this proposal. The original five-GC fixture remains unchanged.
All new compile/native results are UNRUN until sealed execution receipts exist.

## Successor identity

V1 and V2 remain unexecuted. The successor corrects a false-rejection assertion: unused
`py_cpy_*` runtime declarations are permitted, while actual parsed call
instructions are still rejected. The production sources, fixture, runtime,
ordinary compiler route and execution limits are unchanged.

V3 also checks the existing forbidden-Popen mock has zero calls, so a C pass
that swallowed its assertion cannot produce a false ownership PASS. This adds
an observation assertion without changing compiler behavior or limits.
