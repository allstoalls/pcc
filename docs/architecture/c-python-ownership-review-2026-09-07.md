# C, Python and toolchain ownership — architecture review

Date: 2026-09-07. Scope: current working source, read-only claim verification,
plus two small host-side source/API reproductions. No compiler, emitter,
bootstrap or full suite was run for this review. The earlier release and
Python-target qualification tasks remain separate.

## Decision

C remains a required first-class source language. Keep its mature parser,
semantic lowering, real-project corpus and differential oracles. The strongest
next investment is a common build/target/ABI/artifact contract that lets the
same pcc-owned C compiler serve C projects and Python extension builds.
Moving directories or forcing both languages into one semantic type checker
does not establish that contract.

Supporting C and authoring the production runtime in C are independent
decisions. AGENTS.md explicitly targets a freestanding pcc-Python runtime
over machine intrinsics, with C retained as a transition implementation and
differential oracle. Raw memory control and atomics do not inherently require
C as the implementation language.

## Claims checked against current code

| Claim | Finding |
|---|---|
| Two unrelated compilers with zero reuse | Overstated. Separate language ASTs and semantic lowering are appropriate; both already share LLVM-shaped IR infrastructure, optimizer machinery and self-backend emitters. |
| No common HIR means cross-language LTO is impossible | Unsupported conclusion. Common low-level IR and C ABI declarations exist. A demonstrated mixed C/Python post-link optimization gate was not found; that is the missing capability. |
| C cannot use self backend | False for host pcc: assembly/object/execution paths and C integration gates exist. General native-pcc1 C execution, LLVM-free frontend dependencies and fully owned final linking remain distinct gaps. |
| Extension tests using cc prove the C frontend is unused | Wrong evidence route: loader tests intentionally construct external ABI inputs. Independent production-source inspection does confirm external C compiler use in package builds. |
| Fallback inventory is a runtime execution counter | False: the historical 27,853 figure counts static emitted-IR bridge call sites. Zero calls also permits explicit unavailable-function stubs, so execution gates remain necessary. |
| Every similarly named directory is obsolete duplication | False. C PLY/native parsers are oracle/production paths, stdlib float bits remain a shared compiler dependency, and pass directories have overlapping migration roles. The isolated Kaleidoscope chain is a narrower archival candidate. |
| unsafe stubs prove a hidden accidental dialect | The compile-time intrinsic API is explicit and intentional. Its size, pointer/effect contracts and native test coverage deserve scrutiny; inability to directly execute raw intrinsics in CPython is not itself a defect. |
| Darwin bias means no genuine other-target support | Darwin dominates self-host evidence. Linux x86-64 C emission/execution exists; Windows and RISC-V emitters do not. Support must be reported by target and execution boundary. |

## Existing C/Python sharing and the real C gap

- `pcc/frontends/c/codegen/c_codegen.py:11` imports `llvm_capi.compat.ir_c`;
  `pcc/frontends/python/codegen/generation_lowering.py:11` imports `compat.ir`.
  `pcc/ir/compat.py:55` selects the common native IR implementation by
  default, with separate opt-out controls.
- Both paths can invoke `llvm_capi.binding.run_passes_on_ir`:
  `pcc/frontends/c/evaluator/c_evaluator.py:585` and
  `pcc/frontends/python/ir_pass_pipeline.py:602`.
- C self emission is implemented in `c_evaluator.py:2339`; Python uses the
  same emitter family through `pipeline_self_backend_host.py:19`.
- C translation-unit linking exists at `c_evaluator.py:1936`, and Python
  extern calls become typed external IR declarations/calls in
  `codegen/extern_lowering.py:110`. These are useful building blocks; they do
  not demonstrate a mixed-language LTO pipeline or cross-language inlining.
- C self mode still initializes LLVM target machinery
  (`c_evaluator.py:1557`); ordinary self execution invokes system cc for the
  final assembly/link step (`:2643`).
- Native pcc1 explicitly delegates C inputs to host pcc in
  `cli_bootstrap.py::_should_delegate_to_host_cli` and
  `_run_host_pcc_from_pcc1` (around lines 10380–10425). A Python bootstrap
  fixed point therefore does not prove native C frontend ownership.

Existing gates to retain include `tests/c/test_self_backend.py`,
`test_c_testsuite_self.py`, `test_lz4.py`, and
`tests/integration/test_self_backend_x86_64_linux.py`. They cover different
mode boundaries; this review did not rerun them.

`test_pcc_native_extension_loader.py:25` uses cc to create C ABI test artifacts.
Replacing every such oracle with pcc would reduce independence. Add a separate
pcc-produced extension gate instead. The production dependency is established
directly: `cli_bootstrap.py::_native_build_exec_json` selects cc/clang/gcc at
about line 4522 and uses it for compilation/linking; the host counterpart does
the same in `pcc/package/build_exec.py:994,1266`.

Current `build_ownership="owned"`/`host_free_build_claim` fields primarily
describe interpreter orchestration, not complete compiler ownership. Reports
should independently identify the Python executor, C frontend, assembler,
linker and runtime libraries. `pcc-native` is an ABI mode, not proof that pcc
compiled the extension. Native C capability parity is already tracked in
[issue 171](https://github.com/allstoalls/pcc/issues/171).

## Two concrete defects found during this review

The reproducible outputs are retained in
`build/correctness-20260906-a/architecture-concrete-repros.json`.

### Array CLI numeric precision

`cli_bootstrap_array_core.py:521–581` scales numbers by 1,000,000 and truncates
fractional digits beyond six places. The native CLI source calls that path
while declaring float64 and success. Executing both source implementations
under CPython gives:

| Input | Host array front door | Native-shim source implementation |
|---|---|---|
| float64 `[0.0000001, 1.1234567]` | `[1e-7, 1.1234567]` | `[0.0, 1.123456]` |
| float64 `[1.0] / [3.0]` | `[0.3333333333333333]` | `[0.333333]` |

Both report success and no diagnostic. No native binary was executed here;
this is already a source-level semantic discrepancy, independent of compiler
miscompilation. Consolidating numeric semantics and adding actual host/pcc1
differential gates is higher priority than reducing the number of functions.
The current file has 104 top-level functions, 97 beginning `_native_array`,
not 344. It is a live generic array substrate/CLI, not a replacement proving
that `import numpy` works.

### Target admission

`self_backend_target_match.py:15–20` accepts x86-64 triples containing `gnu` as
Linux. The public classification API reports `x86_64-pc-windows-gnu` as
`SUPPORTED`, with target `self-x86_64-linux-v0`. Its execution flags remain
false; this was a matcher/API probe, not a compilation. Explicit OS/ABI target
parsing must reject that route rather than advertising a Linux emitter for
Windows. Directory rearrangement would not repair it.

## Fallbacks, unsafe and migration debt

The full investigation `bootstrap-types-rsplit-libpython-fallback.md` really
records replacing `rsplit` with a manual scan; that scan remains in
`pcc/frontends/python/types.py:157`. Generic native rsplit lowering and both runtime
implementations now exist. The existing differential test uses the C runtime,
so a current pcc1/pcc-Python gate is still needed before restoring the ordinary
source spelling. The fallback ratchet should stay, accompanied by a separate
workaround inventory and actual feature execution gates. Lowering a metric by
retaining unavailable stubs is not capability completion.

`pcc/unsafe/__init__.py:1–6` explicitly says it is a compiler-recognized API,
not an ordinary runtime library. `__pcc_freestanding__ = True` identifies the
kernel subset. Pointer contracts already distinguish `c_obj` and `c_rawptr`
in `pcc/extern/__init__.py:97–106`; those distinctions should be extended and
enforced where raw addresses, managed roots and ownership cross interfaces.
The C/Python mirrors should converge on shared ABI/layout/trace contracts,
then retire C from production linking after differential and fixed-point
gates, retaining independent C oracles.

There are real native tests: `test_unsafe_atomics.py` exercises LLVM/self
semantics and invalid orderings; `test_unsafe_runtime_boundaries.py` checks
callbacks/loaders; `test_freestanding_module.py` checks absence of runtime
dependencies; `test_runtime_oracle_diff.py` compares cc-C, pcc-C and pcc-Python
runtime paths. Some use system compilers to assemble/link oracle probes and
must not be cited as fully pcc-owned build proof.

The 85-mixin stack is a real maintenance concern because many mixins share
mutable code-generation state. The next structural step should isolate
function/module/target contexts and their invariants with execution tests.
Moving 85 classes to different directories does not make that state local.

## Counts and historical code

Count scope: 841 authored `.py` files under `pcc`, pruning hidden, vendor,
`_native`, build and cache directories; checked-in generated Python tables and
in-tree PLY remain included. Numbers can change with the worktree.

- 85 direct bases in `L1CodeGenMixinStack` is reproducible.
- 755 raw `PCC_*` occurrences are not 755 configuration options. The scoped
  AST inventory found 147 distinct literal/constant-aliased PCC environment
  read names; dynamic/wrapped reads are outside that narrower count.
- 1,367 exception handlers include 186 pass-only handlers. Exact
  `except Exception: pass` is 118; including bare/BaseException pass-only
  handlers gives 120. Each catch still needs contextual assessment.
- The collector snapshot contained 548 investigation files and 1,528
  deduplicated confirmation entries across 441 files. The generator collects
  confirmed lines/context and deduplicates `(file, text)`, not unique defects.
  A test, proposal and result can describe the same mechanism. Those counts
  cannot establish 1,528 independent bugs or a regression rate.

The Kaleidoscope chain (`parse/parser.py`, `lex/lexer.py`, `ast/ast.py`) appears
isolated from current executable callers/tests. It is a narrow archival
candidate. Do not remove its parent directories: the C AST, both C lexer
implementations and parity gates remain active. Likewise, `stdlib/_float_bits`
is imported by both LLVM-C IR and self-backend code; `py_stdlib` is not a
wholesale replacement for that directory. `passes` and `ir_passes` have
different execution tiers plus an explicit migration relationship.

## What to borrow from Zig

Zig's user-facing integration is a strong reference: one driver/build graph,
explicit target/C ABI types, header translation, dependency tracking, caching,
and platform/toolchain packaging. Object files can interoperate through the C
ABI without forcing source languages to share an AST or one semantic type
system. See the official [C interop documentation](https://ziglang.org/documentation/0.16.0/#C).

The implementation model must be distinguished from that experience.
[Zig 0.16 release notes](https://ziglang.org/download/0.16.0/release-notes.html)
state that `zig cc`/`zig c++` use Clang 21.1.8, while C translation changed to
Aro/translate-c and `@cImport` is being moved toward a build-system operation.
Thus Zig is not evidence that one unified HIR is necessary for C integration,
nor a proof of C compilation without Clang/LLVM ownership. Its new libc work
also shows that strong C interoperability can coexist with runtime/library
implementation in the host project's own language.

For pcc, borrowing driver/ABI/build contracts is consistent with the north
star. Shipping Clang as the permanent strict C owner would change that goal
and must not be done silently. Automatic C header import can be a later
build-stage feature; its first contract should cover declarations/layout and
constants with explicit diagnostics for unsupported constructs.

## Recommended sequence

1. Repair concrete semantic and target-admission defects; split execution
   ownership reports so green no-host-Python gates cannot imply no system cc.
2. Define one target/ABI/compile/artifact contract used by C projects, Python
   extensions and runtime builds. Introduce pcc C-object production without
   discarding the system-compiler oracle tests.
3. Prove one mixed C/Python vertical slice through compile, owned assembly/link,
   native execution and callbacks. Preserve correct struct layout, ownership,
   safepoints and failure propagation. Track host-pcc and native-pcc1 closure
   separately until issue 171 is actually complete.
4. Add common bitcode/IR linking and a genuine cross-language optimization
   canary where the ABI/effect information supports it. Preserve each
   language's semantic types: C machine integers are not Python arbitrary-
   precision integers.
5. Isolate mixin phase state and reconcile semantic duplicates. Perform
   mechanical directory cleanup after those interfaces and gates are stable.

This review authorizes no deletion, broad rewrite, new compiler-owner fallback
or release claim. Current qualification is still tracked under issues 186/187.
