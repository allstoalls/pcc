# Compile the unchanged OSError constructor fixture

Status at freeze: **UNRUN; source review pending.** This packet performs one
same-process CPython reference and one host-owned public Python-to-ELF compile.
It never executes the ELF or invokes the native five-GC pytest node.

## Preserved fixture and inputs

`program.py` is the complete 3,515-byte `PROGRAM` literal from
`tests/python/test_os_error_constructors.py`. The driver verifies exact equality
with that original single assignment. No assertion, operand, function, class,
call or collection is changed. Expected stdout is exactly
`OS_ERROR_CONSTRUCTORS_OK\n`, stderr is empty and completion must be normal.
The program takes no command-line arguments.

Coverage includes direct and dynamically held constructors, IOError and
EnvironmentError aliases, starred arguments, operand evaluation order across
collection, arbitrary object identity without eager formatting, argument arities,
None and bytes filenames, filename2, very large and noninteger errno values,
explicit FileNotFoundError construction, hierarchy and sibling-handler exclusion,
formatting, keyword rejection, and args mutation independent of metadata.

This uses the same ordinary 3,866-file candidate inventory `03538c5d`, compiler
`819d8a56`, and newly built 191-member runtime `42ed3029` as the prior open-errno
gate. The runtime is Linux x86-64, threads enabled and atomic reference counts.
The runtime result, archive, provenance, private runtime source inventory and
whole candidate are hash-bound. No runtime rebuild or source-tree copy is made.
The prior open-errno GC0/GC1 pass does not qualify constructor behavior.

## Narrow successor

The driver derives from the preserved, executed open-errno compile driver
`e3049494`. Executable changes are limited to the selected fixture, checking its
single literal assignment instead of two errno-message substitutions, and
requiring actual object-call/GC callsites instead of file-open/GC callsites.
`driver-only.diff` records this delta. All public compile, runtime admission,
source/import/environment seals, reference, observer and guard logic is retained.

The unchanged public `pipeline.compile_python` route uses `backend="self"`,
`libpython_mode="off"`, `ir_scaffold_mode="on"`, the explicit runtime and actual
host target `x86_64-unknown-linux-gnu`. Ordinary import discovery must retain the
single program module; unexpected expansion fails without pruning. The exact
`gc` import uses its existing owned builtin provider. Actual IR is retained and
verified without CPython fallback calls.

The original helper's `PCC_PYTHON_IR_PASSES=off` is preserved. Both target-pass
environment variables must be absent, never cleared or overwritten; their
actual production-resolved pass names and transport are recorded. Current
source defaults are an empty target-pass list and text transport. No emitter
threshold, optimizer, linker or production implementation is substituted.

## Admission and remaining gates

Use the same reviewed compile bootstrap and adapter with the pinned CPython
3.15.0rc1 interpreter and locked package directory. Preserve the exclusive lane
lock, 300-second total deadline, hard 4 GiB address-space/NPROC=0 limits,
512 MiB output allowance and continuous 4 GiB free-space reserve. Live
supervisor identity and reservation variables remain checked. A private exact
argument wrapper must pin the shared lock, write roots and disk allowance.

The reference executes the complete original program in the same process with
its own source path as `sys.argv`. Its temporary-directory setting remains
private to the fresh output. No reference child, native execution, FFI invocation,
external compiler or new provisioning is admitted. No locale variable is unset.

Hash-bound driver arguments after the reviewed `-I -S -B` bootstrap:

```text
compile_oserror_constructors.py --source EXACT_CANDIDATE
  --source-manifest EXACT_SOURCE_MANIFEST --runtime-output EXACT_RUNTIME_OUTPUT
  --output FRESH_COMPILE_OUTPUT --worker-state EXACT_LIVE_SUPERVISOR_STATE
```

After a successful compile and fresh ELF receipt, native GC0 then GC1 require a
separate reviewed launcher and admission. Retain the original provenance probe
`2`, exact stdout/stderr/exit checks and observed collector telemetry. Any
failure must be preserved; do not weaken a fixture assertion or substitute a
smaller program to obtain a pass.

Native constructor behavior is UNRUN. GC2–4, Darwin, native pcc1, broad
regressions and Stage1 remain open. BlockingIOError.characters_written, Windows
winerror and broader subclass-constructor parity remain outside this slice.
