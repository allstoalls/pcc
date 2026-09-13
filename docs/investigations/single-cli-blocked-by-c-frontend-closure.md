# Investigation: one CLI needs eleven frontend gaps closed, not a CLI rewrite

## Status
active

## Problem Description
The repository carries three CLI surfaces (`cli_contract.ALL_CLI_SURFACES`:
`cli_core`, `cli_bootstrap`, `pcc.py`) and the stated target is one. The
obvious reading of that target, "delete `cli_bootstrap` and let `cli_core` be
the CLI", is wrong in two ways that measurement settles.

**Entry unification is already done.** All three entrypoints reach
`cli_bootstrap.bootstrap_cli_main`:

- the `pcc` console script is `pcc.cli_launcher:main`, 23 lines, which calls it;
- `python -m pcc` is `pcc/__main__.py`, which calls it;
- `pcc1` is the compiled `pcc/__main__.py`.

**`cli_bootstrap` is the larger implementation, not a subset.** 11,740 lines
and 256 top-level functions against `cli_core`'s 1,847 lines and 39. Only 14
function names appear in both, about 220 lines. The other 242 functions in
`cli_bootstrap` are the pcc1-side implementation -- package install, the pytest
harness, the array core -- that `cli_core` does not have. Deleting it would
delete the only CLI a self-hosted compiler can run.

So `cli_core` is reached for exactly one thing: C and project inputs, plus
Python inputs that ask for the full option set
(`_requires_full_compile_cli`). The single-CLI question is therefore narrow:
**how does the one CLI reach the C driver?**

## Repro
Three routes were built and measured. Only the third is viable today.

```
static `from pcc.cli_core import cli_main` in cli_bootstrap
    -> cli_core's closure (C frontend, packaging, llvm_capi) enters the pcc1
       source closure; stage1 fails outright
`importlib.import_module("pcc.cli_core")` to hide it from the closure walker
    -> 84 CPython fallback calls in cli_bootstrap, whose contract is zero
       (test_cli_bootstrap_package_schema_static_imports_stay_native)
child process to the host pcc (PCC_HOST_PCC, else PCC_HOST_PYTHON -m pcc.pcc)
    -> zero fallbacks, closure clean, and the delegated process runs the same
       cli_core in-process, so the user-visible semantics are the full CLI's
```

The second route was committed and pushed before being measured; the fallback
gate caught it and it was replaced by the third.

## Test [CONFIRMED]
The blocker for the first route, enumerated 2026-09-08 and re-measured
2026-09-11. Compute the tightened closure from `pcc/cli_core.py` and codegen
each member:

```python
import importlib.util as iu
spec = iu.spec_from_file_location('probe', 'scripts/probe_stage1_closure.py')
m = iu.module_from_spec(spec); spec.loader.exec_module(m)
srcs, mods = m._tightened_closure('pcc/cli_core.py')
# then per member: parse_and_lift -> infer_module -> L1CodeGen(typed, False, "on")
#   codegen._strict_no_libpython = True
#   codegen._prefer_native_callable_values = True
#   codegen.generate(typed)
```

**The three settings after `L1CodeGen(...)` are load-bearing and the
2026-09-08 enumeration omitted all three**, so it under-reported: it called
`L1CodeGen().generate()` with `ir_scaffold_mode` unset, and neither
`ir.IRBuilder scaffold expects one block arg` (`c_codegen`) nor
`ir.GlobalVariable expects 2 positional args; got 3`
(`c_initializer_lowering`) appeared at all -- both are real and both were hit
in ordinary compiles. `pcc/py_frontend/pipeline.py` sets exactly these for
`libpython_mode="off"`; anything else measures a different compiler.

Re-measured with them, the closure is **303 members** and the enumeration was
**27 failing in 9 classes**, not 22 in 11:

```
10  CPython for-target representation join
     llvm_python_registry, vthread_effect_analysis, c_declaration_lowering,
     lambda_helpers_lowering, literal_lowering,
     method_call_expression_lowering, self_backend_x86_64_linux, ply.lex,
     hoist_lowering, hoist_free_names
 6  native subprocess check=True requires the pcc-Python
    CalledProcessError export
     pipeline_pass_driver, pipeline_runtime_archive, pipeline_native_link,
     pipeline_self_backend_{cache,emit,link}
 3  iterable splat cannot precede following literal operands
     c_evaluator, self_backend_aarch64_darwin, x86_64_asm_driver
 2  multi-pair CPython-key dict literal cannot preserve per-pair
    insertion errors        ast_utils, builtin_type_attr_lowering
 1  ir.IRBuilder scaffold expects one block arg                  c_codegen
 1  ir.GlobalVariable expects 2 positional args; got 3
                                                    c_initializer_lowering
 1  isinstance second argument ... got BinOp               uv_lock_sync
 1  cannot assign value of type 'callable' to ExternFn       llvm_capi
 1  Layer 1 slice assignment on ByteArrayType                elf_x86_64
 1  CPython fallback **mapping requires a statically dict-typed operand
                                                                c_lexer
```

Two classes from the 2026-09-08 list were already gone by then
(`macho_obj`/`macho_link` and the `arm64_asm_driver` dict-literal case), and
the counts of the surviving ones had grown.

## Closed [CONFIRMED 2026-09-11]
All nine classes are closed and the same enumeration now reports **0 failing
of 303**. Each was a missing capability implemented where it belongs, not a
rewrite of the calling code:

| class | fix | test |
|---|---|---|
| for-target representation join | a for-target may cross domains when no read can observe the pre-loop value; `_for_target_pre_value_is_dead` proves it, position-aware, with an enclosing-loop back edge forcing the conservative answer | `test_py_for_target_representation_join.py` |
| CalledProcessError provider | the provider is admitted on its own instead of through the static-native *module* allow-list, which had silently dropped every `pipeline.py` split-out | `test_native_subprocess_no_libpython.py` |
| iterable splat position | restriction removed: both consumers already replay `ops` in source order | `test_py_literal_splat_source_position.py` |
| multi-pair CPython-key dict | restriction removed: operands are all evaluated before one insert pass, which is what `BUILD_MAP` does | `test_py_dict_literal_cpython_key_pairs.py` |
| `ir.IRBuilder()` / `ir.GlobalVariable(m, ty, name)` / `builder.call(fn, args, name)` | llvmlite takes the block optionally and the name positionally; the scaffold accepts both spellings | covered by the closure test in the splat file |
| `isinstance(x, A \| B)` | a `\|` chain is flattened to the tuple form the lowering already ORs | `test_py_isinstance_pep604_union.py` |
| `x: ExternFn = extern(...)` | `ExternFn` is a declaration marker like `extern` | — |
| `bytearray` slice assignment | new `py_bytearray_set_slice`, in place, exact for equal-length, shrinking and extended slices | `test_native_bytearray_slice_assignment.py` |
| `**kwargs` splat into a CPython call | the enclosing function's own `**kwargs` parameter is a real dict | — |

Two CPython divergences were found on the way and are pinned as strict
xfails rather than left unrecorded: the native dict literal inserts pair by
pair, so an unhashable key skips the later operands' side effects; and
`isinstance(True, int)` is `False`.

## Proposals## Proposals
- No.1 close the CPython for-target representation join [applied]
- No.2 close the remaining classes in count order [applied]
- No.3 keep the child-process C bridge until No.1 and No.2 land [withdrawn]
- No.4 dispatch C inputs in-process from `cli_bootstrap` [applied]

## No.1 close the CPython for-target representation join
### Code Change
Not written. Seven of the twenty-two modules fail on one thing: a `for` target
that must join a CPython-backed binding with a native object binding, or that
needs an already-CPython binding for the loop variable. It is the largest
single class and it spans both the C frontend and pcc's own codegen mixins, so
closing it is the highest-leverage step and the one that would show whether the
rest are shallow.

## No.3 keep the child-process C bridge until No.1 and No.2 land [applied]
### Code Change
`cli_bootstrap._run_c_cli` delegates to a child host pcc. This is applied and
the reasoning is in its docstring. It changes the execution owner for C inputs
in a compiled stage, which the dependency-ownership contract in `AGENTS.md`
does not accept as an end state; it is recorded here as the interim with the
exact list of work that removes it.

### What this does not claim
That the three CLI surfaces are justified. `cli_core` and `cli_bootstrap` still
keep 14 duplicated helper functions, pinned equal by
`tests/python/test_cli_shared_helpers_contract.py`. The reason recorded there,
"a cross-module import turns those calls into getattr bridges worth 47
fallbacks", was re-measured at **84** on 2026-09-08, so the duplication is more
expensive to remove than the note says, not less. Both the duplication and the
C bridge dissolve if a call into another closure module lowers to a direct call
instead of a getattr bridge; that one capability is the common cause, and it is
also why `compiled_owned_passes` carries 30 action-level fallbacks and
`cli_bootstrap_array_core` 106.

## 2026-09-13: execution checks after the in-process dispatch change

The route descriptions above are historical. In the worktree based on
`74e9702965cc05e1f279fe5b21a678c62c16bea1`, `_run_c_cli` statically imports
`cli_core` and calls it in-process. This removes that host-driver route; it
does **not** establish a working native C gate or complete dependency ownership.

Evidence is under `/private/tmp/pcc-owned-perf-20260912-rbrioqup`. Frozen
source `source-candidate-b10` has manifest identity
`0fa7d8947629b2ad5f502d5101c286fedc71b3e65184b465fab68d66f2d652d2`.
Its `stage1-g/pcc1` is 344,753,512 bytes, SHA-256
`feda729d15832df0d1ac5c72f8f29d974a2c9bc02ad9f7bac16f652711eeed9d`.
The host build completed in 339.21 seconds, but the native function compilation
smoke timed out at 30 seconds. This binary is not a qualified stage1 or a fixed
point (`stage1-g/manifest.json`, `stage1-g-binary-diagnostic.json`).

The native C gate was run with `PCC_NO_AUTO_PCC1=1` and `PCC1_BINARY` pointing
at that exact binary, through `run_process_tree_sample.py` with a 120-second
deadline and 4-GiB tree cap. It failed in 7.89 seconds on
`no-libpython function unavailable: pcc.project.run_prepare_commands`
(`native-c-g.stdout`). An ordinary C input had unconditionally called both
optional project hooks with empty options. The worktree now calls them only
when requested. Host emitted execution and the explicit project-hook test
pass; the rebuilt full native C CLI gate remains open.

A 90-second native Python compile diagnostic reached owned signature
validation and failed in 89.42 seconds on `any(bytes)` being unresolved
(`native-gp-compile-receipt.json`). Subsequent scoped native tests exposed and
repaired the bounded `bytes.find(sub, start, end)` call and scalar default
argument/export ABI mismatches. `test_native_codesign_validation.py` now runs
the real signature parser, rebuilds its page hashes and rejects corrupted
padding under all five GCs. These are component checks, not a new pcc1 result.

The link investigation also found two ownership leaks (bytearray receiver
reads and byte-concatenation replacements) and a semantic defect:
`dict.update` silently ignored iterable pairs. The latter discarded the
undefined-symbol index during native Mach-O emission. The new iterable/mapping
protocol preserves partial updates and exceptions, and its pcc-Python and
extracted C implementations were executed against CPython under GC0..4
(`dict-update-green.stdout`, `dict-protocol-c-oracle-v3/receipt.json`).

On fixed inputs containing all 170 objects from the diagnostic runtime,
native relocatable output now matches host output byte-for-byte. The last
controlled whole-merge observation was 66.51 seconds and a 1,809,907,712-byte
tree peak (`runtime-merge-all-watch.json`); an earlier working variant used
7,850,541,056 bytes and 68.76 seconds. This establishes a memory improvement,
not a large speedup. The full pcc1 profile attributes 2,275 of 3,813 samples to
`_read_relocations` (`native-gp-early.folded`). A further relocation-decoder
change is pending native measurement; host layout/relocation checks pass.

The direct frontend worker currently bypasses the default memory-pass driver
and emits with target optimization disabled. On identical per-module `struct`
IR with one structured emitter, explicitly applying owned `mem2reg,sroa`
preserved execution but changed a 20,000-iteration run only from about 0.158 to
0.156 seconds (`struct-pass-ab-v2/receipt.json`). The earlier combined-IR
experiment lost private module scopes and is invalid
(`struct-pass-ab/control-failure.json`). The pass switch alone has not been
shown to explain the compiler gap.

Remaining boundaries include the full native C/Python CLI gates, stage2/stage3,
owned runtime construction/install/cache routes, and the complete gateway
comparison. Runtime archives used here are isolated, manifest-checked overlays
whose construction still used legacy Make/ar orchestration. They do not prove
an entirely owned runtime build. The gateway benchmark has no new completed
comparison, and its 300-second compilation deadline was not increased.

### Later scoped checks on 2026-09-13

Source snapshot `source-candidate-b17` has identity
`1cc27b3277be982d5ff1005d4479bd688c93374f387690fcc66ad16fd3947db4`.
The scoped C API compiler `c-api-probe/compiler-v9` is 136,735,784 bytes,
SHA-256 `cd8e5bd70f721e19d14e69259384e0cf9e4b6134979226895776d1875421729e`.
It now completes the owned preprocessing of the tiny C input, then fails
importing `functools` from `pcc.passes.llvm_python_registry`, before C AST
passes run (`c-api-v9-boundary.json`, `c-api-tiny-v9.stderr`). This is still
not a working C gate. The import policy excludes a compiled `functools`
provider while its `lru_cache` from-import requires one; the existing LRU
provider/decorator implementation also has semantic gaps. An import-only
placeholder would not close this boundary.

The preprocessing fixes cover callable regex replacements, bounded string
prefix/suffix checks and lazy `Pattern.finditer` / `re.finditer`. The latter
uses the existing callable iterator and a checked integer-protocol conversion:
regex bounds raise `OverflowError`, whereas string slice bounds saturate.
Each changed runtime slice has native GC0..4 execution and a labeled C oracle
check (`re-callback-c-oracle/receipt.json`,
`tailmatch-c-oracle-isolated/receipt.json`, `finditer-c-oracle/receipt.json`).
The real comment scanner and macro expander also execute under GC0..4.
Empty-match iteration/replacement and broader regex compatibility remain open.
The runtime archive is `runtime-candidate-b17/libpy_runtime_pcc_py.a`, SHA-256
`b287d28ef63af888abc3cacce37975b1ebb84f706084363196df5d5880fe6eb5`.
Its legacy construction remains a separate ownership gap. The 166 PCO inputs
to the diagnostic compiler's final link are preserved and hash-checked in
`c-api-v9-link-inputs/manifest.json` for runtime-only relinking.

The relocation decoder's first native component comparison now has execution
evidence: 63.48 seconds becomes 59.00 seconds (1.076x), retired instructions
fall 7.3%, and tree-RSS peak falls from 1,829,666,816 to 1,164,836,864 bytes.
Both arms merge the same 170 objects to SHA-256
`020cfdcbc47ad5df14190a317e01918399c08de5c77d3f0335b771ef2c8da543`
(`relocation-ab/first-pair.json`). A separate five-second sample beginning
eight seconds into the candidate run has 1,743 of 3,824 samples in stack-map
scanning and 1,673 through `Struct.unpack_from`; those overlapping counts
describe that sampled interval, not full-process owner shares. Precise internal
plan annotations remove generic iteration/getitem from the hot IR but preserve
dynamic comparisons. Their first additional run takes 56.75 seconds with the
same output, and the 74-line integer-layout fixture matches CPython under all
five GCs (`struct-plan-type-audit/receipt.json`,
`struct-plan-execution/receipt.json`). These small component gains do not close
the end-to-end compiler gap.


## 2026-09-13: native archive verification and SHA owner correction

The current scoped native verifier reaches successful verification, after fixes to
Path copying/byte reads, ASCII decoding/error propagation, and iterable set
predicates. Its initial successful GC0 run took 33.36 seconds. Sampling the actual
verifier found SHA compression dominant, with bigint multiplication and GC below
the runtime rotate helper. The supposedly native helper still expressed its high
bits as Python multiplication by a shifted integer.

With identical captured application inputs and the same 170-member b24 archive,
ABBA execution times were 34.956 / 0.691 / 0.662 / 34.776 seconds. Only the rebuilt
runtime rotate changed: existing `logical_shift_left_i64` replaces the Python
shift/multiplication expression. The helper's generated IR changed from five
allocas and bigint/GC calls to zero allocas and no calls. This approximately 52x
improvement measures archive verification, not whole-compiler throughput.

The following source revision also connects hashlib's incremental SHA256 to the
existing native init/update/final core through immutable GC-owned state snapshots.
Copy shares a snapshot; update replaces it; digest finalizes a stack copy. The
`_oneshot` and `_native_digest` workarounds are removed. SHA now resides in
`py_hash_runtime.py`; its C differential oracle resides in `src/py_hash.c` and is
excluded from the production archive. SHA224 and SHA1 compression are unchanged.
An added regression exposed an existing MD5 placeholder returning truncated SHA256;
this was replaced with an actual MD5 implementation and checked against CPython.
MD5 machine-intrinsic acceleration is still open.

Source snapshot b25 is
`f2381792e07c19ed3c45ee0e95d89b38b612c1fc05bff4b0192eb10ed49e3f63`;
the 171-member diagnostic runtime archive is
`1c76029a31514026a4cbd7ebdfe24299ae0af338fb852a3bf5ebaa4b118aa53e`.
Nine focused tests passed, including emitted SHA/MD5/signature checks across all
five GCs and actual archive-symbol ownership. A C-hash-only oracle also passed
all five GCs. The rebuilt native verifier v9 checks the same b24 archive in
0.590/0.740/0.711/0.749/0.832 seconds for GC0..4, returning 170 members each time.

Repro scripts and receipts are under
`/private/tmp/pcc-owned-perf-20260912-rbrioqup`: `run_sha_rotation_abba.py`,
`sha-rotation-abba.json`, `sha-rotation-ir-audit.json`,
`sha-module-tests.stdout`, `sha_state_c_oracle.py`,
`sha-state-c-oracle/receipt.json`, `build_native_provenance_probe_v9.py`,
`run_native_provenance_v9.py`, and `native-provenance-v9-execution.json`.
Builds/tests used `run_process_tree_sample.py` with the shared lock, durable logs,
timeouts and tree-RSS caps. Runtime overlay construction still used diagnostic
Make/ar orchestration and cannot establish full toolchain ownership.
The full stage1-i2 qualification is pending; the C functools boundary, stage2/3,
full compiler performance and gateway results remain open.


## 2026-09-13: native C frontend produces executable C functions

This scoped experiment used frozen source
`3732392c83a0d9a2692c10d0f68414d5e27f5f690aeb84294e16871b78cbcf48`
(`source-candidate-b35`) and runtime archive
`1612fe0a79c1a39c8c181d350fc09b532dc6f25bdbe1ea070932e1fe993d9a14`
(`runtime-candidate-b33`, 171 members). Evidence is under
`/private/tmp/pcc-owned-perf-20260912-rbrioqup/`.

`build_native_c_frontend_phases_v6.py` compiled the actual C parser, pass
pipeline and C code generator with the host self backend and libpython off.
That emitted frontend ran under GC0–4 and produced IR for
`add(int a, int b)` and `main()` calling `add(20, 22)`. The host-owned assembler
and Mach-O linker consumed each IR result; all five C executables returned 42
and had SHA-256
`f545995e34b74b0f8b9df79e032ce368baa207c4af47b377043c05586d21d62a`.
The frontend executable SHA-256 is
`fc70416cbd8309a33bdfe14749070cad3f3cc2400d0af9382eb2b1bad5fd0d74`.
Read `c-frontend-phases-probe-v6/receipt.json`, its five `.ll` files and
`c-frontend-phases-probe-v6-watch.json` for commands/results. The watchdog was
180 seconds with a 4 GiB tree-RSS cap and the shared performance lock.

The failed earlier attempts exposed generic implementation gaps:

- Methods were present in class dispatch tables but absent from class
  namespaces. Publishing ordinary method objects made MRO-based C action
  collection work; the driver was not replaced with a C-specific shortcut.
- Cross-module subclass overrides were absent from the local declaration
  table used for devirtualization. An annotated pass list called abstract
  `Base.run`, returning `None` while the pass report still said the pass ran.
  The override check now also uses the existing class export graph.
- `type(node).__name__` was folded to the inferred `NoneType`, so the C visitor
  selected its empty handler for real AST nodes. The same optimization dropped
  calls and their exceptions in `type(f()).__name__`. Runtime evaluation now
  preserves these operations and temporary ownership.
- A mixin calling a helper supplied by its concrete subclass was bridged to
  CPython despite a known native receiver. Native lookup now handles this
  shape, including lookup failure before argument evaluation.
- The owned `ChainMap` provider lacked scope mutation and parent operations.
  Its completed core scope operations exposed a separate callable ABI defect:
  a sole starred list was passed as though it were a tuple. Dynamic calls now
  normalize iterables and use one owned argument-tuple contract at all 16
  callers, retaining the existing tuple fast path.
- Subscript mutation primitives can report failure by status. Python syntax
  now uses raising wrappers, preserving missing dictionary keys in `KeyError`
  and propagating callback errors. The C/Python object, protocol and tuple
  mirrors passed the focused GC0–4 differential in
  `mutation-c-oracle-v2/receipt.json`; external cc was an oracle only.
- `ir.values.Constant` and runtime helpers imported from `compat` had no
  closed-world binding. The existing owned value aliases now reuse scaffold
  lowering; `add_raw_function_attribute` and `set_struct_body` bind real
  implementations in the owned IR provider, including function-value aliases.

This is **native frontend plus host-owned backend** evidence, not the complete
native pcc1 C CLI gate. The probe selects frontend opt level 0, and its IR
still contains the parameter allocas/loads/stores; the result is not an
optimizer performance claim. The runtime archive still came from diagnostic
Make/ar construction. Full runtime/toolchain ownership, stage1 qualification,
stage2/3 fixed point and fresh gateway comparison remain open.

## 2026-09-13: complete native C diagnostic reaches execution; cache and descriptors

Stage1-P used frozen source b37
`469af50457364e5fad90ea3ba193f20bf8f134fdfe0acc691890d9e55ffa8124`
and the b33 runtime above. Its binary is
`5a359b1a3f2b009c57e4d2c42a475d19577bf9ddf4b235cba036afe223c0915d`.
Host construction took 375.02 seconds; the unchanged 30-second Python function
compilation smoke timed out. The stage1 manifest is ERROR, not qualification.

That pcc1 nevertheless compiled and linked `tiny.c` through its complete native
C pipeline with `--backend=self --python-libpython=off --ir-scaffold=on
--no-cache`, and the emitted executable returned 42. Read
`native-p-c-no-cache-watch.json`, `native-p-c-no-cache-execution.json` and
`native-p-tiny-c-no-cache` under the evidence root above. The emitted binary
SHA-256 is `f545995e34b74b0f8b9df79e032ce368baa207c4af47b377043c05586d21d62a`.
Disabling cache is a diagnostic exception: the default C gate remained blocked
by the strict `_load_compiled_artifact` stub.

The default-cache blocker followed correction of exception-class filtering:
the redundant qualified `json.JSONDecodeError` reference had no native binding.
The cache now catches `(OSError, ValueError)`, including their subclasses, and
the owned JSON provider correctly derives `JSONDecodeError` from `ValueError`.
Malformed object keys also exposed `_parse_string` indexing past EOF or asserting;
it now raises `JSONDecodeError`. `json-cache-error-native.stdout` records 20
passing focused checks, including owned JSON execution on GC0–4 and host C cache
regressions. This does not yet prove the rebuilt pcc1 default-cache route.

The larger C probe containing stdio, arrays, a struct and a loop then failed
with `unsupported operand type(s) for +`. The relocated LLDB breakpoints in
`native-p-c-plus-offset-lldb.stdout` locate the error in
`LLVMCodeGenerator._tag_type_key`, called through an instance-method wrapper.
It is a static method. `test_native_staticmethod_descriptor.py` reproduced the
same failure with `getattr(obj, "key")("x")`: direct compiler dispatch knew
the method kind, but the runtime class namespace had no staticmethod descriptor.

The generic repair publishes a real staticmethod descriptor with the normal
function signature binder; its runtime getter returns the function without a
receiver. Explicit `staticmethod(function)` constructs the descriptor too.
Callable dispatch, `__func__` and `__wrapped__` use the same owned function slot.
A further identity regression found that `Base.key` synthesized a different
function object from `getattr(Base, "key")`; static method value lowering now
loads the class-owned object. Descriptor construction uses the existing tag,
slot/barrier/deallocation contract and publishes its initialized GC slots.
`staticmethod-fixed.stdout` records 14 checks and
`staticmethod-identity-fixed.stdout` records 15 checks, including emitted GC0–4
execution, descriptor precedence, defaults, keyword arguments and errors.

The four C class/class-attrs/object/dunder mirrors were substituted into the
otherwise identical Python runtime for a labeled external-cc oracle. The
replacement also supplies the exact `pcc_class_del_defined_count` global from
C substrate, since replacing the Python class object removes its definition.
`staticmethod-c-oracle-v2/receipt.json` records matching execution on all five
GCs for staticmethods, class namespaces and exception classes. This is a
differential oracle, not an owned runtime construction claim.

The next source snapshot b38 is
`8f7c205a43dd08a8e13db52012b857e7361d59ad0dee8a09c1db52f987bcad48`;
its diagnostic runtime archive is
`5501d92a135465cde8a0fc5ebcbd9b86e38145e0c4ff144fda50bb5ff5492911`.
The runtime receipt records correction of a copied platform stamp to the
`arm64-apple-darwin` target present in all 171 verified member receipts; archive
payload bytes were unchanged. The reproduction commands are in
`staticmethod-overlay-watch.json`, `staticmethod-c-oracle-v2-watch.json` and
`stage1-q-watch.json`. New-source pcc1 execution, default-cache C compilation,
the larger C program, normal stage1 qualification and all later stages remain
pending at this checkpoint.

## 2026-09-13: Q passes default-cache C execution; linking dominates Python compile

Stage1-Q built b38 in 386.05 seconds (980.43 user, 20.88 system), producing
`815058b51a57f3d517db5b814bd9f305508b4a774b8fb2ae97271ffb8f5b9150`.
Its normal 30-second Python compilation smoke still timed out: no qualified
stage1 receipt or stage2/3 claim.

`run_native_q_c_gate.py` now passes with the default cache enabled. Cold, warm
and corrupt-cache recompilation all emit the same tiny C executable returning
42. The warm artifact is not republished, and corrupted JSON is replaced with
valid IR. The stdio/array/struct/loop executable prints
`42`, `12`, `v0=20`, `v1=22`, `v2=42` and exits zero. Its SHA-256 is
`5bf2e50fb3254cf5988094681049579e707009b1e2f7714faefa7912e449c61d`.
Read `native-q-c-gate/receipt.json` and `native-q-c-gate-watch.json`.
This closes these native C execution regressions, not the full C language or
runtime construction boundary. Cached `func_return_types` values remain null
in these native artifacts and need separate API/metadata qualification.

Q also compiles the actual staticmethod/identity/JSON feature program, then its
emitted binary matches CPython on GC0–4. Read `native-q-features/receipt.json`;
the emitted SHA-256 is
`d3fd9364cd939954ee0a89c4d1f936c30e6bcf629c6c8da6f22de106d27f679e`.
The diagnostic run was sampled and took 57.08 seconds, so this wall time is
not an A/B performance result. Its phase profile attributes 48.484 seconds to
`link_self_pcc_driver`, 2.587 to frontend codegen and 2.270 to IR passes.
The raw late sample, `native-q-features/compile-late.sample.txt`, resolves
against Q's own image: 3,108 of 3,748 on-CPU samples (82.9%) have leaf
`py_bytes_concat` under `macho_exec.link_prepared_executable`. Read the folded
stacks and `compile-late-owners.json`; this five-second window is not a claim
that the same fraction applies to the entire link.

Source inspection confirms the C byte-concat mirror already uses two `memcpy`
calls, while its Python port copied both operands in byte-at-a-time loops.
The port now uses the existing `memcpy` intrinsic, preserving allocation,
family, terminator and ownership. `bytes-copy-tests.stdout` records 14 passing
checks, including content/family independence and ownership on GC0–4. The
test labeled `cc` in the existing join test still used the explicitly selected
Python archive; that label is not C differential evidence. Actual C evidence is
`bytes-concat-c-oracle/receipt.json`: compile the current complete C bytes
implementation as an external-cc oracle, rename its defined symbols through the
owned object linker, and execute its concat function on all five GCs.

`bytes-copy-abba-v2/receipt.json` binds one application IR/object/emitter and
two runtimes differing only in `py_obj_stubs.o`. ABBA elapsed times are
0.304 / 0.072 / 0.069 / 0.299 seconds; native instructions fall from roughly
4.53 billion to 0.85 billion (81.2%). Outputs match and RSS stays about 3.7 MB.
This is a 128 MiB copy microbenchmark, not whole-compiler acceleration. The
first attempt failed before execution because its explicit emitter target was
missing; its failed logs remain separate. `bytes-concat-ir-audit.json` confirms
the new helper contains two memcpy calls.

The new runtime is
`0ddeb7583b450da8d440d9a7030babe2b83319c77fbc5f844a8811ad01feae85`;
frozen source b39 is
`c634dcbc98d246fdedeb8780fd1fb34391be60fc77cf461955cdf7abc103a943`.
Stage1-R is the pending full-compiler validation. The compiler's direct indexed
bootstrap still selects IR passes off and emitter `optimize=False`; the copy
optimization does not close that pipeline gap. Bytearray growth also still
replaces the object in the current frontend/runtime, so alias/identity and
amortized growth semantics remain open. No linker-specific source rewrite was
used to conceal that boundary.
