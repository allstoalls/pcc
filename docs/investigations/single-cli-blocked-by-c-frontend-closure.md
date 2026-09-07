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
The blocker for the first route, enumerated 2026-09-08. Compute the tightened
closure from `pcc/cli_core.py` and codegen each member:

```python
import importlib.util as iu
spec = iu.spec_from_file_location('probe', 'scripts/probe_stage1_closure.py')
m = iu.module_from_spec(spec); spec.loader.exec_module(m)
srcs, mods = m._tightened_closure('pcc/cli_core.py')
# then parse_and_lift -> infer_module -> L1CodeGen(...).generate() per member
```

At least 22 members fail, in eleven distinct capability classes. The counts
are what decide where to start:

```
7  CPython for-target representation join
     vthread_effect_analysis, c_declaration_lowering, lambda_helpers_lowering,
     method_call_expression_lowering, self_backend_x86_64_linux,
     hoist_lowering, hoist_free_names
     "requires an already-CPython binding for X" /
     "cannot join a CPython-backed for-target with a native object binding"
3  multi-pair CPython-key dict literal cannot preserve per-pair insertion
     errors        macho_obj, ast_utils, builtin_type_attr_lowering
3  native subprocess check=True requires the pcc-Python CalledProcessError
     export        pipeline_self_backend_{cache,emit,link}
2  Layer 1 slice assignment on ByteArrayType not supported
                   macho_link, elf_x86_64
1  ir.IRBuilder scaffold expects one block arg                     c_codegen
1  cannot assign value of type 'callable' to ExternFn              llvm_capi
1  multi-pair dict literal with a user-observable key cannot delay
     hash/equality dispatch                            arm64_asm_driver
1  iterable splat cannot precede following literal operands
                                                     x86_64_asm_driver
1  ir.GlobalVariable expects 2 positional args; got 3   c_initializer_lowering
1  CPython fallback **mapping requires a statically dict-typed operand
                                                                    c_lexer
1  Layer 1 slice on type NoneType not supported                     ply.lex
```

"At least" is deliberate: the enumeration was read from a truncated tail and
has not been proven exhaustive.

One class was already closed on the way here. `direct_indexed_kernel` failed
with "missing required argument 'aarch64_tail_call_ids'", which was a dataclass
`field(default_factory=...)` default that could not cross a module boundary;
that fix is what let stage1 complete at all. See
`tests/python/test_dataclass_default_factory_across_modules.py`.

## Proposals
- No.1 close the CPython for-target representation join [pending]
- No.2 close the remaining ten classes in count order [pending]
- No.3 keep the child-process C bridge until No.1 and No.2 land [applied]

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
