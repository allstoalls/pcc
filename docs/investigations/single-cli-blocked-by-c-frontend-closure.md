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
