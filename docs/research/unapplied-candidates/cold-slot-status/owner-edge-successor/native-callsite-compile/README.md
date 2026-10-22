# Cold-slot reporter: real callsite compile stages

This isolated harness prepares actual executables from the exact preserved
native fixture's Python program. It changes no production/test file and never
executes an emitted executable. Each compile arm is an independently guarded
single-process stage, at most **300 seconds / hard 4 GiB AS / NPROC=0**, with
**4 GiB free disk reserve**. Runtime construction is a separate, previously
admitted 1200-second stage; a complete source-matched PASS is a prerequisite.

The prerequisite has since completed: the manifest pins the actual191-member
archive `a6645f903e03f6766ecc7466b76f2993318582d287a393831aa5522158421915`,
compiler identity `17e126ba45ec048242e47739bd39bf8c564ea4e8ad03a27a416588af0cfd1137`
and complete runtime result/provenance hashes. This packet's compile and native
stages remain unrun.

## Inputs and semantics

`program.py` is byte-for-byte the `_PROGRAM` literal from the frozen
`tests/python/test_slot_call_status_reporting_native.py`. The driver verifies
that relation rather than inventing a smaller fixture. It retains successful
keyword calls, GC during the callee, an existing ValueError's identity/message,
a binding TypeError, an owned temporary with a finalizer, and exact cleanup
observations. Expected stdout is `slot-status-owned-callsite-ok` plus newline;
stderr must be empty. The unchanged program also runs as a captured same-process
CPython reference before each compile. This is a test oracle, not native output.

The public `compile_python` executable route remains intact: explicit actual
x86_64 Linux target, self backend, `libpython_mode="off"`, IR scaffold on and
an explicitly admitted runtime. The original native fixture's program pass
configuration is retained (`PCC_PYTHON_IR_PASSES=off`). Runtime construction
uses its separate normal/default and runtime pass lists; these modes are not
interchangeable or described as a default-program-pass qualification.

The only input import is `gc`. The real owned `pcc/stdlib/gc.py` is found by
normal discovery, while builtin-native policy excludes it from the compiled
provider closure. Its existence still selects the ordinary recursive
multi-source discovery path. The harness observes that completed production
closure and requires exactly the original program module, rather than
supplying an invented export/context dictionary. An unexpected shadow module
or additional provider stops the gate. Explicit target selection chooses the
supported in-process frontend; it does not disable backend workers.

The production emitter's normal selection remains unchanged. If the actual
IR crosses its existing worker threshold or any operation attempts a child or
FFI call, the enclosing audit gate fails, even if the production code catches
the denial. There is no threshold override, new no-worker flag, direct-emitter
replacement or reduced-input retry.

## Arms and actual-edge proof

Run `candidate` first. It calls the unmodified candidate status lowering. The
three owner-scoped witness/verification definitions are loaded unchanged from
the hash-bound native test by selecting their AST definitions. This avoids
importing that test module's unrelated CEvaluator/ctypes dependency; no
compiler implementation or assertion body is rewritten. The resulting check
proves each real `main` object-slot status producer, signed comparison,
error-only helper call, exact source-frame arguments, unchanged cleanup edge,
managed root clears and exception-preserving slot/leave order.

The optional admitted second arm is `inline-reference`. It uses the existing
host test's `_InlineReference._slot_call_check_status`, whose body must match
the exact canonical former production method. It changes that one method only
in the current host process and restores it after compilation. All other
compiler, runtime and emitter inputs are shared. It is a controlled old-inline
oracle on the candidate compiler/runtime, **not** an independently built
production818 compiler/runtime baseline. Do not weaken runtime provenance or
relabel the candidate runtime to claim the latter.

The inline-reference arm requires absence of the actual module-owned reporter
key after generation. It does not rely on an inaccurate literal emitted-name
substring. V2 changes only this reference-arm assertion; V1 was source-reviewed
and never executed.

Both arms preserve public frontend/discovery behavior. Observation wrappers
return the original results unchanged. Capture/verification overhead makes
this a semantic gate, not a performance benchmark. Candidate real-edge proof
requires exactly three selected sites; the owner may contain other object-slot
calls, which are counted rather than incorrectly assumed absent.

## Coordinator invocation and preserved evidence

After the runtime's actual final source/cleanup seal, pass the following to a
separately admitted hash-bound single-process compile bootstrap:

```text
compile_callsite.py
  --source EXACT_CANDIDATE_SOURCE
  --canonical EXACT_CANONICAL_SOURCE
  --runtime-output SEALED_RUNTIME_PAYLOAD
  --runtime-result-sha256 EXACT_RUNTIME_RESULT_SHA256
  --arm candidate
  --output FRESH_CANDIDATE_OUTPUT
```

Only after complete candidate compile/contract PASS, the same invocation may
be admitted with `--arm inline-reference` and a fresh independent output.
Each arm receives its own300-second bound; neither budget is extended by the
other. The supervisor preserves both live worker-tree reservation variables,
controls the exclusive lock, enforces the free-space floor and records
CLEAN/ECHILD. Its compile scope and script hashes require separate admission;
the runtime-build bootstrap's receipt alone does not authorize this stage.

Full compiler/canonical inventories must be sealed by the coordinator before
and after each stage. The payload also verifies the complete inventory
identities, all named changed/import-policy/test owners, exact fixture bytes,
the completed runtime result/hash sidecars and strict191-member source/codegen
admission. It forbids all unbound PCC environment keys, keeps native
provisioning disabled, retains the original no-auto-pcc1 setting, and leaves
live supervisor reservation variables untouched.

Retain result/reference/closure/profile JSON, real generated IR, candidate
observed and verified status edges, executable bytes/hash, stdout/stderr and
all supervisor/source seals. A compile failure or worker denial stops the
corresponding dependency chain. No automatic retry, alternative pipeline,
runtime rebuild or executable run is part of this packet.

## Later native execution, still separate

The current Python bootstrap prohibits exec and cannot be reused as a native
launcher without a separately reviewed, exact-ELF admission. A future stage
must supervise the known PID across replacement by the hash-bound executable,
keep hard AS/NPROC constraints, persist stdout/stderr directly, and enforce the
existing10-second child /15-second outer bounds. It must observe, rather than
assume, the selected GC backend in real runtime events.

If admitted, execute each successfully compiled arm under GC0 and GC1 only.
Require exact expected stdout, empty stderr, zero exit, and observed GC equal
to the requested backend; compare both arms. GC2/GC3/GC4 remain unrun under
this no-thread boundary. The original full five-GC test remains unchanged and
is not reported as passed by this partial harness.

This real program exercises status failures with an already-pending
ValueError/TypeError. It does not dynamically cover fresh RuntimeError creation
or its added source frame. The preserved separate C harness covers that branch,
but its established C owner imports ctypes. Reassembling private C compiler
components or changing the fixture is not a substitute. Fresh-exception native
coverage, general native supervision, pcc1, full Stage1 and fixed-point
qualification remain open even if all proposed callsite stages pass.
