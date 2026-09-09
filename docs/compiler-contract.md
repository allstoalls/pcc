# Compiler contracts

Maintainer requirements, not a report of current implementation coverage.
Use current source and matching execution receipts to determine what works.
The root [AGENTS.md](../AGENTS.md) is the startup guide; consult the relevant
section here when changing an execution, language, runtime or public boundary.

## Dependency ownership contract (maintainer directive, 2026-09-07)

**Host pcc depends only on CPython and the Python standard library. Native
pcc1 requires no external toolchain or language-runtime dependencies.** This
contract applies to the entire supported workflow, including C processing,
runtime construction, optimization, object emission, linking, cache checks,
bootstrap and installed use. It overrides historical implementation notes
below that describe external execution owners.

- Remove LLVM, libLLVM, llvmlite, cc/clang/gcc and equivalent dependencies
  from the product paths. Host pcc must also remove third-party Python
  dependencies such as pycparser, PLY and cffi; internal pcc implementations
  may use CPython and its standard library only.
- pcc1 must not invoke host Python/CPython, load libpython, or require
  externally installed compiler, preprocessor, assembler, linker, archiver
  or signing tools. These operations must use pcc-owned native code, compiled
  into pcc1 or supplied as part of the pcc toolchain. Moving an external
  dependency into a subprocess, build script, cache helper, bundled LLVM
  library or compatibility wrapper does not remove the dependency.
- C is part of this contract: preprocessing, parsing, semantic analysis,
  optimization, code generation, assembly and linking must be owned by pcc.
  C inputs, runtime builds and extension builds are not exceptions. Do not
  route C processing to host cc or LLVM, or label that route self-hosted.
- Reuse and complete pcc's existing implementations. A Python-authored
  algorithm that still uses LLVM to parse, verify, optimize or emit its IR
  has not completed this migration. Host execution of pcc's own code satisfies
  the host-pcc contract only when it uses the standard library; the same host
  subprocess remains an unfinished dependency in pcc1.
- Existing external routes are migration defects to track and eliminate,
  not evidence that those dependencies are necessary. Fail explicitly at an
  unimplemented boundary; never silently restore an external owner to make a
  test pass or improve a benchmark. Preserve Python/C semantics and all GC
  contracts while completing the native path.
- LLVM/cc measurements may be retained as explicitly labeled external
  reference experiments. They cannot be required by the shipped compiler or
  counted as proof of pcc1-owned optimization. Prove the owned route with
  dependency-denial checks and execution of its emitted programs; checking
  only dynamic library links or a successful compiler exit is insufficient.

The target OS's kernel/platform ABI is the execution boundary, not a license
to rely on an external compiler or language runtime. This is the required
end state, not a claim that every current implementation already meets it.

## CPython library contract (maintainer directive, 2026-09-08)

Priority: native pcc1 self-hosting and the five-GC program remain P0. CPython
embedding, public tooling and ecosystem packages are P1 and must not displace
or block that spine, change native execution ownership, or weaken GC contracts.

`pcc` is also an executable capability library embedded in CPython. After
`import pcc`, its public APIs must provide real compiler, native execution,
artifact-inspection, binding-generation and accelerator capabilities. `pcc1`
is the standalone entrypoint, not a prerequisite for calling every public
library API. Upper-level packages that depend on pcc must be able to consume
these working APIs from CPython as well.

Operations requiring native code must compile/load/call the owned native
implementation through a specified boundary. Import success, declaration
stubs, metadata-only results and CPU test oracles are not proof that the public
capability executes. Explicit Metal requests require real device execution or
an explicit unavailable-capability error. Keep shared semantics and validate
both CPython-callable and native-pcc1 entrypoints; neither proves the other.
This is a required product contract, not a claim that the current implementation
has completed every surface. The dependency ownership contract above still
applies; do not implement the native entrypoint by delegating to CPython.

## Public CLI parity contract (maintainer directive, 2026-09-08)

`pcc`, `python -m pcc`, and `pcc1` must expose the same declared user-facing
command semantics. The first two run the compiler driver under CPython; pcc1
runs the native compiler. That implementation distinction must not change
defaults, input interpretation, supported options, diagnostics or exit status.

- The production default is the owned self backend.
- C files and projects enter the full pcc-owned C compilation pipeline;
  native C frontend gaps are unfinished capabilities, not permission to
  delegate to host Python, cc or LLVM.
- Python scripts and ordinary `-m` modules compile and execute native code.
  Host `runpy` execution is not an equivalent implementation of that command.
- Tool commands use the corresponding owned native tool execution path and
  share argument, output and error contracts across the entrypoints.
- Environment selection, install paths, cache behavior, program arguments and
  `-o` behavior must agree. Internal bootstrap worker modes may remain private.

Current differences in cli_core/cli_bootstrap and the legacy adapter are
migration gaps, even where historical code calls them intentional divergences.
This paragraph specifies the target, not current completion. The maintainer
promoted entry convergence to P0 for immediate work on 2026-09-08. It runs
alongside native pcc1 self-host and five-GC P0 work, without editing another
session's performance optimization. Do not replace
the bootstrap entry or alter defaults without the relevant execution/parity
and source-frozen bootstrap gates. Reuse capability-parity issue #171 for the
underlying native C owner rather than duplicating its implementation scope.

## Project Intent

The complete project direction, six pillars, seven obligations, value model,
industrial/research relationship, accelerator scope and runtime layering live
in [Project Intent](project-intent.md). This file keeps the detailed dependency,
CPython-library and CLI contracts; it does not replace the project intent.
