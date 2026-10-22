# Fresh slot-status exception: exact-ELF execution

This packet proposes two native executions of the single sealed C-fixture ELF,
one under observed GC0 and one under observed GC1. Both the former inline and
candidate reporting probes are linked into that same executable. The original
2,933-byte C fixture and its assertions are unchanged. All executions are UNRUN
at creation; compilation passed separately through ordinary CEvaluator and the
owned object writer/linker with the same 191-member threaded/atomic runtime.

The launcher is a narrow successor of the reviewed Python-callsite launcher.
It selects only the combined-probes arm, verifies status-runtime by an open
regular-file descriptor and the exact compile receipt, binds harness_sha256,
and supplies exactly one argument: the selected GC number as decimal text.
It keeps the same static ELF64/x86-64 check, unprivileged zero-capability and
NoNewPrivs checks, irreversible AS4GiB/NPROC0 limits, explicitly unblocked
10-second default SIGALRM, same-PID descriptor exec and 15-second outer known-root
pidfd guard. The existing compiler bootstrap still denies execution. No child,
FFI library load, new runtime, source change or compiler-threshold change is
introduced. These resource controls are not a filesystem/network sandbox.

Run each collector once, serially, after independent source review, preservation
and explicit admission. Stop on the first failure. The separate read-only
validator requires actual exit0, exactly `slot-status-runtime-equal\n`, empty
stderr, matching nonempty runtime collection events, unchanged ELF bytes and
CLEAN/root-reaped/ECHILD supervisor evidence. EXEC_READY alone is not success.

The original native assertions cover fresh RuntimeError class, formatted
message/source-frame equality and order, borrowed-raise refcount equality,
root-count preservation, and pending-exception pointer/frame identity. Both
reporting implementations execute inside each successful run. This is generated
probe/C-ABI coverage; the separately preserved Python-callsite checks cover its
original ordinary keyword-call, pending-error and owned-temporary paths.

GC2–4, native pcc1, macOS and full async/gateway suites remain UNRUN here. NPROC0
must not be used to claim worker-dependent collector coverage. Execution elapsed
times are diagnostics only, not a performance benchmark.

Pass the pinned manifest and its SHA, the sealed compile-output directory,
--arm combined-probes, --gc 0 or 1, and a fresh --output to launch_elf.py through
the separately reviewed shared-lock supervisor. The payload and guard outputs
must be fresh siblings. After terminal cleanup, pass those same arguments plus
--guard-output to validate_native.py. Manifest paths are relative; actual input
locations are caller-supplied and their bytes are pinned. Review/qualification
receipts are recorded separately from this immutable creation state.
