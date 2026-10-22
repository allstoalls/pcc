# Indexed handoff publication successor (unapplied)

Apply this two-path delta after the original stage-bridge and codec-packing
patches in the preserved source-v1 packet. Exact intermediate and final byte
identities are in manifest.json. The original V1 remains unchanged and UNRUN.
No active production source, workflow, resource cap or admission equation is
changed by saving this artifact.

The correction removes concrete native-closure risks from the new handoff
helper: os.link, os.lstat, os.path.lexists, and the tempfile.mkstemp dependency.
The normal no-libpython source-closure walk discovers lazy same-package imports,
and module generation emits every top-level function. A runtime host-only flag
does not exclude these bodies from Stage1 compilation; unsupported bodies can
also become explicit native error stubs. A build alone would not prove them.

Requests now use a fixed temporary opened in exclusive binary mode, check the
destination before and after writing, and atomically replace the destination.
This prevents replacement among cooperating publishers using that same private
root and temporary protocol. It is not an atomic no-replace primitive against
an unrelated writer bypassing the protocol. A stale request temporary fails
closed. Failed exclusive creation never deletes another publisher's temporary.
After successful publication, cleanup never removes a later publisher's file.

For seals and receipts, the preceding worker must already be reaped before a
retry reuses its fixed temporary. Existing pool retirement supplies this
condition. The successor rejects symlink temporaries, preserves PIDX inputs,
and continues to publish only complete JSON. Existing owned path predicates,
exclusive open, close, and replace implement the required API surface.

Independent source review found no remaining blocker in this narrow change.
Focused host tests, actual split-child compilation, output equality, native
execution, five-GC behavior and Stage1 performance all remain UNRUN. The new
tests are host filesystem contracts and process mocks, not native qualification.
The same original three test files remain the focused scope, under the sole
execution lane's 300-second / 4-GiB AS / hard NPROC=0 guard and 4-GiB free
reserve. The manifest records the exact static collected-count expectation.

The original width bound, actual 47-component indexed shape, complete input
seals, six PIDX hash reads, one exclusive retry, and all resource margins/caps
are unchanged. Full closed-world/native qualification remains a separate gate;
removing identified unsupported APIs is source evidence, not execution proof.
