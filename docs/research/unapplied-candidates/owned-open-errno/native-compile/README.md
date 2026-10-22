# Compile the unchanged native open-errno fixture

Status at freeze: **UNRUN; source review pending.** This packet performs one
same-process CPython reference and one host-owned public Python-to-ELF compile.
It never executes the ELF or invokes the native pytest node.

## Exact inputs and behavior

The ordinary candidate is the 3,866-file source inventory `03538c5d`, based on
commit `761bcdaf62408d56060bb9074585218e6b8bccc4` with the two preserved OSError
patches applied. Compiler identity is `819d8a56`. Its prior host gate passed
439 cases with one declared strict XFAIL. These remain host results.

`program.py` preserves the complete `PROGRAM` in
`tests/python/test_native_open_errno.py`, including its original two host
`os.strerror` substitutions. The frozen Linux strings are rechecked against the
actual original assignments before execution; a locale/platform mismatch fails
instead of rewriting the fixture. No assertion or operand has been removed.
The effective locale and both strings are recorded. The program needs no
arguments beyond its executable name.

The fixture checks missing text and bytes paths, exact `FileNotFoundError`
handlers and sibling exclusion, `errno`, `strerror`, `args`, original filename
type, `filename2`, formatting, and exception survival after `gc.collect()`.
It also checks `FileExistsError` from exclusive creation and that existing file
contents remain intact. Success is exactly `OPEN_ERRNO_OK\n`, empty stderr and
normal completion.

Only the newly executed, complete 191-member runtime `42ed3029` is admitted:
Linux x86-64, threads enabled, atomic reference counts, source/compiler matched.
Its result, archive, provenance, source inventory and source files are checked.
The older cold-slot archive is excluded. Building an archive establishes neither
native OSError behavior nor GC correctness.

## Execution boundary

Use the unchanged reviewed compile bootstrap and adapter, with the shared
exclusive supervisor lock, 300-second total deadline, hard 4 GiB address-space
and NPROC=0 limits, 512 MiB output allowance and continuous 4 GiB free-space
reserve. Use the pinned CPython 3.15.0rc1 interpreter and locked package directory;
do not provision another environment. Both supervisor reservation variables
remain bound and checked against the live owned root.

The reference executes the original program in the same process with `sys.argv`
set to its source path. `TMPDIR` and host `tempfile.tempdir` identify a fresh
private output directory; the reference must leave it empty. No reference child
process, external compiler, FFI invocation, runtime build or native program is
admitted. No unnecessary locale variable is unset.

Compilation calls the unchanged public `pipeline.compile_python` with
`backend="self"`, `libpython_mode="off"`, `ir_scaffold_mode="on"`, the explicit
new runtime and actual host target `x86_64-unknown-linux-gnu`. The fixture's
`PCC_PYTHON_IR_PASSES=off` setting is preserved. `PCC_SELF_TARGET_PASSES` and
`PCC_SELF_TARGET_PASS_TRANSPORT` are required to be absent at admission, as in
the inspected environment; they are never overwritten. The production resolver's
actual effective pass names and transport are recorded. This does not claim
that the fixture sets those two variables or disables target passes.

Observational wrappers retain the ordinary import-discovery and IR-generation
results unchanged. The real discovered closure must consist of `program.py`;
`gc` and `tempfile` use their owned builtin providers. An unexpected closure
fails rather than being pruned. Actual generated IR is retained and verified,
including open and public default-GC callsites and absence of CPython fallback
calls. No emitter threshold, optimizer, linker or implementation is substituted.
Whole source/runtime inventories, imported module roots, archive hashes and
environment are rechecked after compilation.

Hash-bound driver arguments after the reviewed `-I -S -B` bootstrap:

```text
compile_open_errno.py --source EXACT_CANDIDATE
  --source-manifest EXACT_SOURCE_MANIFEST --runtime-output EXACT_RUNTIME_OUTPUT
  --output FRESH_COMPILE_OUTPUT --worker-state EXACT_LIVE_SUPERVISOR_STATE
```

Any later native execution requires a separately reviewed and admitted launcher
bound to the actual successful compile receipt and ELF hash. The proposed next
scope is same-PID static ELF execution under GC0, then GC1, with actual collector
telemetry, the fixture's provenance probe `2`, and fresh temporary storage.
Neither native execution is authorized by this packet alone. GC2–4, native pcc1,
Darwin, constructor-program execution, broad regressions and Stage1 remain open.
