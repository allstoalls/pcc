# Cold-slot reporter: owned runtime build gate

This is an unapplied research harness for the preserved cold-slot reporter
candidate. It invokes the unchanged production runtime builder exactly once.
Preparation and source review do not establish a successful build or native
behavior. No production or regression-test file is changed by this packet.

## Frozen source and admission

The compiler input is the complete ordinary candidate formed from production
commit `818187ba7dfff54df589b93bc10594f87786fe22`, canonical preservation commit
`fe054fd02931c1e616a9c287b6d32362290b5e1d`, the archived cold-slot patch, and the
owner-edge test successor. The complete candidate inventory is SHA-256
`ea395e18a173ec4172c8badd68bbccb53a66390e4c130b8a78f6702fbc177635`.
The archive compiler identity is computed by the unchanged production owner
at execution time and must be a complete, current SHA-256 value.

Run only after exclusive build-lane admission through the reviewed single-host
bootstrap: fixed Python 3.15.0rc1, isolated `-I -S -B`, hard address-space limit
4 GiB, hard `RLIMIT_NPROC=0`, no capabilities or privilege gain, and audit
rejection of process creation/exec and ctypes imports/FFI. The supervisor owns
the known root pidfd, wall-clock deadline, durable logs, wait and cleanup.
The admitted total runtime-build budget is **1200 seconds**, including source
copy, build and deep verification. Keep **4 GiB free disk reserve** throughout;
initial admission also budgets 4 GiB for outputs (8 GiB minimum initial free).
The output estimate is a reservation, not an assertion about actual disk usage.

The coordinator must intentionally start this build without the test-only
`PCC_TEST_NO_NATIVE_PROVISIONING` environment variable. The payload rejects
its presence, even if false. It never removes a reservation marker, unsets a
provisioning guard or changes a capability. The actual production
`require_native_provisioning_allowed()` runs before output creation and again
inside the builder. An existing `build/.pcc-test-no-native-provisioning` marker
stops the build. `PCC_NO_AUTO_PCC1=1` is retained.

The payload rejects every inherited `PCC_*` variable outside its fixed,
recorded allowlist and the two required live supervisor reservation variables, and rejects conflicting allowed values. It binds the
ordinary owned/default passes, library/no-libpython mode, and full threaded,
atomic-refcount runtime configuration. No worker threshold or fallback is
changed. Bootstrap deepwire disabling is explicitly part of this host scope.

## Exact production route

`owned_runtime_build.build_runtime_archive(runtime_source, archive, target)`
uses the real Makefile inventory and `runtime_build_config()`. The expected
native host target is `x86_64-unknown-linux-gnu`; a different actual host stops
this gate. All **191** members are required, including the normal thread-kernel
selection and all managed/GC modules. `PCC_WITH_THREADS=1` controls generated
code, not host threads. The kernel process limit does not select a smaller or
thread-disabled runtime.

For each member the unchanged builder calls the public Python frontend with
`emit_llvm_only=True`, `python_library=True`, `libpython_mode="off"`,
`backend="self"` and the explicit target. That genuine runtime-library route
returns before executable linking and object-worker dispatch. It uses the
normal runtime ABI/import context. The source-prescribed no-implicit-poll
exceptions for six runtime modules remain unchanged and scoped by the builder.

Frontend passes are `default`. The subsequent runtime pass sequence is
`mem2reg,sroa,instsimplify,inline-defined,instsimplify,instcombine,dce`, with the
normal thread-kernel exemption. Object emission calls `emit_owned_object`
directly, followed by the owned archive writer. No Make, host C compiler,
external assembler/archiver, LLVM, FFI or generated native execution is used.
Harmless imports of modules that also define subprocess routes are allowed;
any attempted process/FFI audit event fails the enclosing gate even if caught
inside an import or production function.

Because the builder owns a transient `.pcc-runtime-build.lock` in its runtime
root, the payload first copies the complete runtime subtree into its fresh
output directory. All 220 regular files, 5,136,492 source bytes and modes must
match the frozen compiler inventory before and after the build. The complete
compiler source also receives exact byte/mode/path-set seals before and after.
The copied runtime contains no reduced inventory, generated stub or patched
production file. All temporary files and output members remain available for
inspection; this payload does not clean away partial failure evidence.

## Coordinator invocation

V2 preserves and binds `PCC_WORKER_TREE_STATE_PATH` and
`PCC_WORKER_TREE_BUDGET_BYTES`, rather than clearing or rejecting the existing
supervisor reservation. The state path must match the explicit admitted
`--worker-state` argument; its current schema, 4 GiB budget, freshness, this
process/parent row and actual supervisor receipt must agree. Both reservation
variables stay unchanged throughout. V1 was stopped at source review before
execution because its strict allowlist omitted these two existing variables.

Pass these payload arguments to the hash-bound script mechanism of the reviewed
bootstrap, with a fresh output directory whose parent already exists:

```text
build_owned_runtime.py
  --source CANDIDATE_SOURCE
  --source-manifest CANDIDATE_SOURCE_INVENTORY_JSON
  --output FRESH_OUTPUT
  --worker-state SUPERVISOR_OUTPUT/worker-rss.tsv
```

The packet manifest supplies the exact script hash and input identities. The
supervisor, rather than this payload, enforces the 1200-second deadline and
continuous free-space reserve. A direct unguarded invocation is not admitted.
No environment reservation may be cleared by this command or the payload.

## Acceptance and scope

A result is PASS only after the production strict runtime-admission helper
verifies archive/source/codegen/target/configuration provenance and complete
inventory. The driver also checks ordered member names, all 191 retained object
and IR hashes against their per-object receipts, equality of retained and
archive-manifest receipts, original runtime-source bytes, real ELF members,
and the C-API inventory recomputed from those objects. The production verifier
already checks every actual archived object against those same receipts.

Keep `result.json`, the original archive and provenance/C-API sidecars,
`deep-member-verification.json`, copied-source inventory and all original
member `.ll`, `.o` and receipt files. Phase updates are flushed durably before
copy/build/admission/deep-verification/seal transitions. A timeout leaves its
last phase visible; the supervisor records the actual timeout and cleanup.
Failure does not trigger a retry, fallback or a native run.

Even a successful runtime build proves construction/provenance only. The
cold-slot helper's actual native call-edge, exception/frame identity, GC-root
lifetime and cleanup semantics still need separately admitted compiled-program
execution. A later same-PID native gate may initially cover GC0/GC1 only under
the process limit. GC2/GC3/GC4, parallel compilation, pcc1, complete Stage1 and
fixed-point qualification remain outside this gate. The original full five-GC
native fixture remains unchanged and unexecuted.
