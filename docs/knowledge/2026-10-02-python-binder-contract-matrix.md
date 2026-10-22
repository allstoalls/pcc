# Python binder contract matrix, 2026-10-02

## Scope and actual result

The corpus is `tests/python/test_binder_contract_matrix.py`: 21 call shapes × six
owners = 126 identical-source cells. LF/IF are local/module-qualified imported
functions; LM/IM are local/imported instance methods; LC/IC are local/imported
constructors. Each source asserts exact returned values or exact TypeError text,
body-entry count through the event trace, operand evaluation order, and applicable
identity/default side effects. No caller aliases, xfails, prefix stripping, or
weakened assertions were introduced to make the compiler pass.

- CPython **3.15.0rc1**: **126/126 pass**, using the qualified interpreter
- Owned IR on immutable **v16 + narrow imported-constructor binding fix**:
  **64 pass, 53 compilation rejections, nine operand-order/multiplicity failures**
- Native execution of this matrix: **not run**, all 126 cells remain unqualified
  for returned values, runtime error text/timing, and GC0–4
- No ELF, executable, runtime, archive, compiler build, commit, or push was done

`P` means source compiled to strict no-libpython owned IR and its unique operand
helper sequence matched the source. It is **not** a native semantics pass.
`E` means compilation rejected the source; a source-level try/except TypeError
cannot catch that rejection. `O` means IR emitted operand helpers in a different
order or more than once. LF, IF, LM, IM, LC, IC have respectively
8, 9, 7, 7, 13, 20 IR passes out of 21.

| Cell | LF | IF | LM | IM | LC | IC |
|---|---|---|---|---|---|---|
| required_mixed | P | P | P | P | P | P |
| posonly_into_kwargs | E | E | E | E | E | P |
| reordered_evaluation | O | O | O | O | O | P |
| all_kinds | P | P | P | P | P | P |
| literal_defaults | P | P | P | P | P | P |
| kwonly_under_splats | P | P | P | P | P | P |
| multiple_splats | P | P | P | P | P | P |
| missing_positional | E | P | E | E | E | P |
| missing_kwonly | E | P | E | E | E | E |
| duplicate_binding | E | E | E | E | E | P |
| unexpected_keyword | E | E | E | E | E | P |
| posonly_keyword_error | E | E | E | E | E | P |
| surplus_positional | E | E | E | E | E | P |
| star_evaluation_once | P | P | P | P | P | P |
| interleaved_mappings | E | E | E | E | P | P |
| repeated_mapping_key | E | E | E | E | P | P |
| explicit_run_before_failure | E | E | E | E | P | P |
| binding_failure_after_evaluation | P | E | E | E | P | P |
| nonstring_key_after_evaluation | E | E | E | E | P | P |
| nonmapping_stops_evaluation | O | O | O | O | P | P |
| definition_defaults_once | P | P | P | P | P | P |

## Precise findings

1. Legal `target(1, a=9)` for `def target(a, /, b=2, *items, c=3, **extras)`
   fails static resolution in LF/IF/LM/IM/LC with
   `unexpected keyword argument 'a'; formals=(a,b,items,c,extras)`.
   CPython captures the keyword `a` in `extras`; it does not bind the positional
   formal twice. IC reaches the published runtime binder and compiles.
2. LF/IF/LM/IM/LC reorder `target(step_a(1), c=step_c(3), b=step_b(2))` operands into
   helper calls `a,b,c`, while the source and oracle require `a,c,b`.
   The same AST expressions are evaluated after formal-order rearrangement.
3. `**step_bad(5), later=step_unreached(6)` emits `unreached,bad` for LF and
   `bad,bad,bad,unreached` for IF/LM/IM. The latter duplicates the mapping
   producer through per-formal subscripting in `_expand_direct_call_unpacks`.
   The oracle evaluates only `bad`, then raises TypeError.
4. Missing, duplicate, positional-only, unexpected, and surplus-argument errors
   are commonly raised as `L1CodegenError` before the native program exists.
   These are failures, not expected static rejections: every error cell catches
   runtime TypeError and checks its exact text and events.
5. Several interleaved-`**` function/method cells fail closed with
   `NotImplementedError: call cannot yet preserve source order for multiple **mapping operands interleaved with explicit keywords`.
6. IC is 20/21 IR-pass. Its retained no-keyword positional fast path still
   statically rejects `Box(1, 2)` when `__init__(self,a,/,b,*,c)` requires `c`.
   Exact core error: `missing required argument 'c' (positional=2, raw_positional=2, raw_first=IntLit, kwargs=0, formals=3; signature=a<none>,b<none>,c<none>; supplied=)`.
7. All six defining-module default cells compile and pass the CPython oracle:
   positional and keyword-only mutable defaults are made once, retain defining
   module values after rebinding, and preserve identity/mutation across calls.
   Their native default semantics remain unexecuted.

### Oracle correction and runtime risks

The first reference run was intentionally retained: 19 cells passed, then the
nonmapping expectation used an obsolete callable-qualified diagnostic. The
actual 3.15 message for this merged-keyword shape is exactly
`Value after ** must be a mapping, not int`. Only that expected string was
corrected; all 126 reference cells then passed.

CPython 3.15 distinguishes three timing boundaries:

- Repeated mappings stop at the colliding mapping; later keyword expressions
  are not evaluated
- A run of explicit keyword values following a mapping is evaluated completely
  before that run merges, so its later values run even when an earlier key in
  the run collides
- A non-string mapping key is rejected at call binding after later keyword
  operands have been evaluated

Read-only runtime audit found `_merge_one_keyword` in
`pcc/runtime/py/py_call_splat_runtime.py` rejects non-string keys during merging,
and duplicate-key text lacks the oracle's callable-qualified prefix. These are
source-backed risks pending native execution, not a claim that this matrix ran
against the runtime. Routing to the runtime binder alone cannot certify them.

## Flatten/bind owner audit

All 13 `_resolve_call_kwargs` call sites in the frozen codegen tree were
inventoried (not just grepped for a function name):

| Owner | Sites | Boundary and classification |
|---|---:|---|
| `call_expression_lowering` | 4 | Local constructor plus three local-function/click branches flatten AST operands; plain local construction consumes native ABI slots directly |
| `user_function_lowering._emit_direct_user_function_call` | 3 | Direct/external-function branches flatten AST operands and emit formal order |
| `method_call_lowering` | 4 | Static, direct, static-pointer and direct-pointer method owners; direct-pointer dynamic branch rebinds after `_method_unflatten_call_shape` |
| `native_asyncio._emit_async_user_function_call` | 1 | Flat tuple feeds an adapter that consumes already-bound ABI slots; no second Python signature binder established |
| `assignment_statement_lowering._maybe_emit_valueclass_constructor_payload` | 1 | Flat values are stored directly into explicit value payload fields, not rebound by a Python signature |

The narrow imported-class defect was flattening before
`_emit_native_class_instantiate` invoked `_emit_direct_method_call`, resolving a
second time. The candidate removes that flattening boundary for keyword/splat
calls, preserving original operands through `_emit_compiled_module_object_call`.
Its required-mixed retained IR contains one `py_obj_call` and no direct initializer
call; the other five surfaces each call exactly one corresponding direct body.
These owner counts were checked in emitted `probe` bodies, not inferred from
test names. All six strengthened single-owner assertions subsequently passed
on the same immutable source in a separate 1.01-second guarded replay.

An early suspicion about the analogous local constructor was withdrawn after
reading the full condition: when definition-time defaults require a runtime
method call, its caller preserves original operands; otherwise `emit_instantiate`
consumes flattened ABI slots directly. No second local signature bind was proved.
Its order and static-error failures above are independently demonstrated.

`super`'s dynamic method-pointer path genuinely crosses flat ABI → Python bind,
but explicitly reconstructs keyword-only and packed vararg/varkw slots first.
Existing `test_py_super_keyword_only_call.py` covers three valid native programs;
this run did not execute them or certify their order/error/default behavior.

## Reused tests and remaining gaps

- `test_imported_constructor_binding.py`: reused its closed-world contextual
  export/infer/L1 strict-native IR approach and exact binder-count technique
- `test_constructor_unpack_binding.py`: reused source-corpus/reference/IR/native
  controller structure; its error-count tests do not prove exact error text
- `test_function_binding_errors.py`: existing runtime callable/error parity,
  but it strips qualified prefixes for mapping errors and does not prove this
  matrix's static/direct owners
- `test_python_function_features_parity.py`: actual basic defaults, keyword calls
  and positional splat bodies inspected; broad docstring claims were not counted
  as additional executed binding coverage
- `test_hoisted_default_capture_binding.py`: existing nested-definition default
  identity/capture controls; does not substitute for defining imported-module cells

Uncovered cells are explicit: from-import spelling, callable aliases/unbound
methods, static/class/super methods, custom mapping `keys()` repeated entries,
multiple-missing-name grammar, property/descriptor overrides, decorators,
async/generator adapters, and default expressions that themselves raise.
Existing tests for these shapes were not executed in this audit.

## Proposed shared repair, not implemented here

Use one ordinary-Python call-binding gate taking the original `Call`, including
`operand_order`, before any formal-order expression flattening. Publish the
callable/receiver, positional tuple, ordered keyword stream and result through
existing roots once; invoke the live signature binder once. Reuse the existing
operand split/keyword-run/root-cleanup machinery rather than separate per-owner
reconstructions. Methods must retain descriptor/override lookup, constructors
must retain class dispatch, and the result must keep its authoritative return ABI.

Keep a direct native ABI fast path only when the signature/call prove a valid
exact positional bind with no omitted default, keyword/splat, or uncertain
arity. Ordinary invalid arity must route to runtime so argument side effects
occur before catchable TypeError. Explicit runtime-port/C ABI paths require
separate, preserved machine-ABI policy; they are not ordinary-Python exceptions.

At the runtime boundary, preserve duplicate-merge timing, defer non-string-key
validation until binding, and supply callable context for exact error strings.
Do not use caller rewrites, broad assertion normalization, or five separate
package/call-site fixes. Qualify the common path on this unchanged corpus and a
matched runtime under GC0–4 before claiming semantic closure.

## Reproduction and provenance

Evidence directory:
`/workspace/scratch/ce254c0a0910/pcc-cloud-runs/binder-contract-matrix-astra`.
The immutable compiler root was
`/workspace/scratch/ce254c0a0910/pcc-cloud-runs/imported-constructor-binding-astra/source`.
This is frozen v16 plus the narrow imported repair, not the moving live tree.
The live call-expression/call-object/runtime-function sources differ and were
not used for the reported compile measurements.

- `matrix-results.json`: all 126 cells, status, exact error lines, command and log
- `source-before.json`, `source-after.json`: complete source hashes, stable
- `final/reference-identity.json`, `final/ir-remaining-identity.json`: final corpus
  and source hashes, stable
- `reference.log`: original oracle-assumption red; `final/reference-final.log`:
  126 exact oracle passes
- `ir-cells.json`, `final/ir-cells.json`: initial 18 and remaining 108 IR receipts
- `final/ir-owner-strengthening.log` and adjacent receipt/identity JSON: six
  final single-call owner assertions, all passed; prior receipts preserved
- `final/test_binder_contract_matrix.py`: exact final reusable corpus and controller

Final test SHA-256:
`07e6a0ec0e5329fe443d41cd8360cd49ba573468c1b5a754f31ffc5086a620e8`.
Frozen imported owner SHA-256:
`4744cd0524bdc0dcdfc31639d1c895fc3f82e9c20884a13cb4557b9f0c3f061b`.

Each pytest used the qualified 3.15 interpreter, `-o addopts= -x -n0 -vv
--tb=short`, explicit frozen `PYTHONPATH`/source roots, and disabled provisioning.
Independent failing cells were inventoried using separate `-x` invocations.
The existing process-tree supervisor enforced 1 GiB, 20 seconds per IR cell,
180 seconds aggregate, and 90 seconds for reference. The remaining 108 cells
completed in 87.79 seconds at 135,368,704 bytes sampled tree RSS. No performance
lock was acquired because no native/runtime/build work was launched.

The native integration node is
`test_binder_contract_native_five_gc[CELL]`. It requires an explicit
`PCC_RUNTIME_ARCHIVE`, validates its production provenance and current codegen
identity, compiles with self/no-libpython/scaffold-on, and runs GC0–4 with host
Python/PCC disabled and an empty PATH. It has **not** been launched here.
