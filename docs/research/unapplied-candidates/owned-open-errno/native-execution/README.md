# Run the unchanged native open-errno fixture

Status at freeze: **UNRUN; source review pending.** This packet admits only the
exact ELF produced by the preserved open-errno compile packet. Compilation and
the same-process CPython reference passed; neither establishes native behavior.

## Exact scope

The 1,792-byte original PROGRAM and all its assertions are unchanged. The ELF
checks missing text and bytes filenames, exact FileNotFoundError dispatch and
sibling exclusion, errno/args/strerror/filename/filename2/formatting, exception
survival after collection, and exclusive creation preserving existing content.
Expected stdout is exactly `OPEN_ERRNO_OK\n`, stderr is empty, and the program
must return zero. No command-line arguments are added.

The manifest binds compiler `819d8a56`, source inventory `03538c5d`, the newly
qualified 191-member threads/atomic runtime `42ed3029`, compile result `6571f3e0`,
and static ELF `68c60e69` (27,793,384 bytes). The older runtime is excluded.

Run GC0 first. Only after its complete successful validation may GC1 run once.
Each invocation uses the same reviewed known-root supervisor and exclusive lock,
15-second outer deadline, hard 4 GiB address-space/NPROC=0 limits, continuous
4 GiB free-space reserve and 64 MiB filesystem-growth threshold. The launcher
checks an unprivileged UID, zero capabilities and NoNewPrivs; verifies a regular
owned 0755 ELF64 x86-64 executable without PT_INTERP through an open descriptor;
and executes that same descriptor in the same supervised PID. SIGALRM is set to
default, explicitly unblocked and armed for 10 seconds before exec. No child,
thread, compiler or runtime rebuild is admitted by this packet.

The minimal native environment retains threads/atomic configuration, JSON GC
logging and the original fixture's refcount-provenance probe `2`. TMPDIR points
to a fresh mode-0700 `tmp` directory directly under this invocation's private
output directory. This environment pin changes no PROGRAM byte or assertion.
The fixture must clean that directory completely. A failure keeps its output
and any remaining private temporary entries for diagnosis.

`EXEC_READY` records only preflight. Native PASS requires actual zero exit, exact
stdout, empty stderr, nonempty real collection events matching the requested
collector, unchanged ELF, empty private temporary storage, and terminal
COMPLETE/CLEAN/root-reaped/ECHILD supervisor evidence. Full compiler-source and
runtime inventories are checked again by the coordinator after the group.

## Commands and interpretation

Use the separately reviewed strict adapter with the unchanged supervisor. Its
exact child is the pinned host Python with `-I -S -B`, then `launch_elf.py` and
`--manifest`, `--manifest-sha256`, `--compile-output`, `--arm candidate`,
`--gc 0` or `--gc 1`, and `--output`. The output is a fresh sibling of that
invocation's guard directory. After the supervisor terminates,
`validate_native.py` reads those arguments plus `--guard-output`; it does not
execute the program. Preserve any failure and stop without replay or cap changes.

This is a Linux x86-64 pcc0-produced native fixture with observed GC0/GC1 only.
Constructor PROGRAM, GC2-4, Darwin, native pcc1, full async/gateway, broad
regressions and Stage1 remain unqualified. NPROC=0 cannot establish the threaded
collector behavior. There is no performance claim.
