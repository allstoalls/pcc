# Native gate for the public-collector fixture successor

This packet executes the exact owned ELF built from the two-call test-only
successor preserved in the parent directory. The compiler source, runtime,
generated former-inline and candidate reporting probes, all exception/root/
refcount assertions, and strict native outcome predicate are unchanged.

The preceding raw-collector fixture completed its C assertions but failed the
required actual-collector observation. Its GC0 failure and GC1 UNRUN result
remain preserved. The successor changes exactly two collection statements to
`pcc_gc_collect(-1)`. Its separately bound test postimage is not substituted into
the frozen compiler inventory or its existing runtime provenance.

## Exact execution scope

The manifest binds the successful new compile result, whole qualification,
static ELF, corrected C fixture, external native-test postimage, unchanged
compiler inventory and 191-member threaded/atomic runtime. The launcher is
byte-identical to the preceding source-reviewed C launcher. The validator changes
only its scope text; all pass/fail predicates remain unchanged.

Run the single combined-probes ELF once with GC0, then only after PASS once with
GC1. It receives exactly one decimal argument naming that collector. Both
reporting implementations are present in the same binary, so there are two runs,
not four independent compile arms. Each run retains the unprivileged same-PID
verified-descriptor exec, zero capabilities and NoNewPrivs checks, hard 4 GiB
address-space and NPROC=0 limits, explicitly unblocked 10-second SIGALRM,
15-second known-root outer guard, 4 GiB continuous disk reserve, 64 MiB disk
growth threshold and exclusive shared lock.

The exact ELF descriptor is opened without following symlinks, hash/size/mode
verified, checked as static ELF64/x86-64 without an interpreter, and executed
without creating a child. The launcher never prints the C success marker.
`EXEC_READY` is only preflight evidence.

PASS requires actual exit zero, exact `slot-status-runtime-equal\n`, empty
stderr, nonempty JSON collection events with the requested integer collector ID,
unchanged ELF bytes, and terminal CLEAN/reaped/ECHILD cleanup. Missing or wrong
collector events fail. The original semantic and observation assertions are not
relaxed. Stop on any failure; no cap widening or fallback.

After the admitted launcher finishes, `validate_native.py` reads its immutable
manifest, compile result, durable stdout/stderr/GC log and terminal guard receipt.
Both commands use the manifest-bound files and fresh output paths. Native runs
are UNRUN at packet creation.

GC2–4 require a different supported process/thread capability and remain UNRUN.
Native pcc1, macOS, full async/gateway, full Stage1 and broad native regression
qualification are also UNRUN. These bounded semantic checks make no performance
or full-bootstrap claim.
