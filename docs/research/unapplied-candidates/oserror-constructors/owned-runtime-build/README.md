# Build the exact current-source OSError runtime

This bounded compile-only gate uses the ordinary production
`owned_runtime_build.build_runtime_archive` owner. It builds a new complete
191-member Linux x86-64 runtime with threads enabled and atomic reference counts.
The previous cold-slot archive cannot qualify the changed OSError runtime and
compiler inputs and is not reused.

## Source relation

The base is commit `761bcdaf62408d56060bb9074585218e6b8bccc4`, whose production
compiler includes cold-slot increment `02b9bc2d3c071bbc3e547a63e045daa73cc6e1bc`.
The candidate applies exactly the two preserved OSError packets: owned-open-errno
V2 and the constructor/payload patch, nine paths total. Their preimages match
this base without an overlapping edit to the cold-slot change.

The candidate is an ordinary independent source closure containing the complete
pcc, scripts, tests and .github trees plus seven root Python/configuration files.
It is not a full repository checkout. All included baseline trees and root files
were matched to immutable Git objects. Its full 3,866-file inventory is hash-bound;
there are no source/import overlays or linked copies.

On that exact candidate, the host gate actually ran 440 cases: 439 PASS and one
known strict XFAIL for BlockingIOError.characters_written. This consists of
369 passing OSError body-model cases, six original frontend IR checks and
64 cold-slot host controls. These are host/IR/object-boundary results, not native
OSError behavior. The earlier old-base receipts remain separate.

## Build and guards

Use the reviewed original single-process runtime bootstrap and known-root
supervisor. Keep the exclusive shared lock, total 1,200-second deadline, hard
4 GiB address-space and NPROC=0 limits, and continuous 4 GiB free-space reserve.
Require at least 8 GiB free before admission for the 4 GiB planned output allowance
plus reserve. These limits are unchanged from the already executed owned-runtime
gate. NPROC=0 prohibits child/process or thread creation during this build; it
does not select a thread-disabled runtime.

The driver preserves both live supervisor reservation variables, checks its
actual parent/root receipt, rejects conflicting PCC variables and active
provisioning reservations, and does not clear any guard. Test-only no-provisioning
markers must be absent before an explicitly admitted runtime build. Automatic
pcc1 provisioning remains disabled. The driver copies and verifies only the
complete 220-file runtime subtree into its private output root, allowing the
production builder to own its normal lock without mutating the frozen source.

The ordinary default owned IR passes, owned object writer, archive writer and
strict production admission are unchanged. The emitted archive must have all
191 ordered members, matching source/compiler/configuration receipts, retained
IR/object hashes and real object C-API definitions. The driver checks the exact
host-qualified compiler checksum before building, and rechecks source inventories,
runtime sources, imports, environment and released builder lock afterward.

Driver arguments after the hash-bound `-I -S -B` bootstrap:

```text
build_owned_runtime.py --source EXACT_CANDIDATE
  --source-manifest EXACT_SOURCE_MANIFEST --output FRESH_OUTPUT
  --worker-state EXACT_LIVE_SUPERVISOR_STATE
```

No compiler threshold, member list, emitter, optimizer, linker or runtime owner
is substituted. No native executable is run. This packet is UNRUN at creation;
a source review or prior host pass cannot qualify its build. Later native OSError
checks require the new archive and separately admitted exact-ELF execution.
GC2–4, native pcc1, macOS and broad regression/Stage1 qualification remain open.
