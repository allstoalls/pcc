# Chapter 6: Python Lowering — the Facade and the Mixins

When type inference finishes (see Chapter 5), pcc's Python frontend holds a typed AST; the LLVM and self backends (Chapters 12 and 13) consume LLVM IR. The layer that turns the former into the latter is called Layer-1 codegen, and it is the most semantically dense layer of the whole Python path: subscripts, iteration, exceptions, ownership, and string formatting are all translated here into runtime-call sequences and basic-block structure. This chapter covers two things. First, the physical organization of the layer: how [pcc/frontends/python/codegen/layer1.py](../../pcc/frontends/python/codegen/layer1.py) was split from a twenty-thousand-line monolith into a 56-line facade plus 86 mixins, and how the constraint "the compiler must be able to compile itself" reshaped the code along the way. Second, the layer's semantic discipline: why every runtime call that can raise must be followed by a `py_err_occurred()` check, and what happens when one Python semantic is scattered across several lowering paths. The two case studies in "History and Lessons" — the dual subscript path and the six division paths — are the most instructive field reports of that failure mode.

## Chapter Overview: Lowering Connects Semantics to the Runtime

Do not read lowering as "the syntax tree in another format." It decomposes Python semantics into IR control flow, runtime calls, error checks, and reference cleanup, while preserving the no-libpython boundary at every step.

- The facade dispatches work; the mixins own concrete semantic families. That split prevents one file from absorbing the whole frontend.
- Generated code must check error state after every runtime call that can raise.
- The owned-or-borrowed status of locals, return values, and temporaries directly controls when objects are released.

## 6.1 The Problem and the Design Space

### 6.1.1 Where the monolith ended up

In late April 2026 (commit `88ee9157`, release 0.1.2), [pcc/frontends/python/codegen/layer1.py](../../pcc/frontends/python/codegen/layer1.py) was a single 20,195-line file: expression dispatch, statement dispatch, subscripts, loops, exceptions, classes, and native-module lowering all lived in one `L1CodeGen` class. Today the same file is 56 lines, and contains little more than:

```python
class L1CodeGen(L1CodeGenEntrypointMixin, L1CodeGenMixinStack):
    ...
```

The actual implementation is spread across roughly a hundred files and over sixty thousand lines under [pcc/frontends/python/codegen/](../../pcc/frontends/python/codegen): mixins named `*_lowering.py`, native-module lowering in `native_*.py`, plus large dedicated modules such as `class_gen.py` and `hoist_lowering.py`.

Why split? Three reasons, only the first of which is obvious.

**Maintainability.** A twenty-thousand-line class has no legible structure. Everyone who comes in to fix a bug — human or agent — faces the same problem: the blast radius of an edit cannot be estimated. The repository rules ([AGENTS.md](../../AGENTS.md)) list "broad speculative changes to shared codegen" as the fastest way to create expensive regressions. That is statistics, not rhetoric.

**Self-hosting cost.** pcc's Python frontend must compile pcc itself (see Chapter 15). Bootstrap profiling ([docs/investigations/bootstrap-self-time-after-layer1-split-2026-05-13.md](../../docs/investigations/bootstrap-self-time-after-layer1-split-2026-05-13.md), data from 2026-05-14) showed that of the ~22.6 seconds pcc1 spent compiling [pcc/__main__.py](../../pcc/__main__.py), the single event bucket `multi_codegen_layer1` accounted for ~11.4 seconds — the largest item. The shape of the codegen layer itself — file sizes, nested functions, closure structure — directly determines bootstrap wall time. The same investigation records the honest counter-lesson: after the split brought `layer1.py` down to 603 lines, focused profiles showed that line count did not buy a proportional drop in codegen wall time; the real hot spots were the nested rewrite loops in `hoist_lowering.py` and the total bytes of generated IR. Line count is a maintainability metric, not a performance metric.

**Growth discipline.** Once the split landed, the repository rule became an invariant: new Python lowering behavior must go into the narrowest existing mixin or native module, and `layer1.py` must not grow back. A 56-line facade is the physical enforcement mechanism for that rule — if anyone tries to add a method, the diff itself is the alarm.

### 6.1.2 Why mixins

There were at least two alternatives. One is a classic visitor: one visit method per AST node class, with an explicit dispatch table. The other is a pipeline of independent passes: subscripts, exceptions, and loops each implemented as pure functions from IR to IR.

pcc chose mixins, and the central reason is **the cost of shared state**. Every operation in Layer-1 lowering touches one large mutable context: `self.builder` (the IR builder and its insertion point), `self.env` (the local-variable slot table), `self.runtime` (the table of declared runtime functions), `self._try_err_block` (the current try's error-target block), `self.loop_stack`, the generator context stack, the GC-root records, and more. Both the visitor and the pass pipeline would require threading that state through every signature, or collapsing it into a context object passed to every function — which is `self` under another name. Mixins let each concern (exceptions, subscripts, ownership) live in its own file as methods that read and write the shared context directly; the price is a very long inheritance chain.

In an ordinary Python project that price is an aesthetic complaint. In pcc it was very nearly fatal, because **pcc's own type inference and lowering did not understand mixins at the time**. Section 6.3 tells that story. For now, fix in mind the theme that recurs throughout this chapter: every architectural decision in Layer-1 is constrained by the fact that this code must itself be compiled by pcc, and many odd-looking source shapes are fossils left behind by the self-hosting constraint.

## 6.2 Facade, Stack, and Dispatch

### 6.2.1 An entry point made of three files

The facade in `layer1.py` does exactly three things: import `L1CodeGenMixinStack` from `layer1_mixins.py`, import `L1CodeGenEntrypointMixin` from `layer1_entrypoints.py`, and compose them into `L1CodeGen`. `L1CodeGenMixinStack` is an inheritance list of 86 base classes, from `TypedIntAbiMixin` down to `NativeWeakrefLoweringMixin`, one per line, in MRO order.

The facade contains a comment worth quoting in full:

```python
class L1CodeGen(L1CodeGenEntrypointMixin, L1CodeGenMixinStack):
    # Class-local copies are required for the self-hosted stage compiler:
    # several host orchestration paths in layer1.py read these attrs directly,
    # and pcc1 does not yet reliably resolve class attrs through mixin bases.
    _EXTERN_SCAFFOLD_MODULES = EXTERN_SCAFFOLD_MODULES
    ...
```

The module-level constants from `layer1_constants.py` are copied one by one into class attributes, because pcc1 — the compiler that pcc compiles out of itself — cannot yet reliably resolve class attributes through a chain of mixin bases. This is the first concrete instance of the theme from the end of 6.1.2: those "redundant" lines are not an oversight; they are the current capability boundary of pcc1 projected into the source. Delete them and everything still works under host CPython — and the bootstrap chain breaks.

The entrypoint mixin (`layer1_entrypoints.py`) exposes four public methods — `generate()`, `_emit_stmts()`, `_emit_stmt()`, `_emit_expr()` — each of which is a thin wrapper: the real work is delegated to `GenerationLoweringMixin._generate_impl`, `StmtDispatchLoweringMixin._emit_stmt_impl`, and `ExprDispatchLoweringMixin._emit_expr_impl`. The wrappers exist for **diagnosability**. When tracing is enabled, every statement and expression emission pushes a breadcrumb (module, function, statement index, node kind, source span) into a ring buffer (`_codegen_trace_ring`); if any exception escapes lowering, `_codegen_trace_dump()` writes a `PCC_CODEGEN_EXCEPTION` header followed by the recent `PCC_CODEGEN_BREADCRUMB` entries to stderr. For a compiler that may crash at the seventy-thousandth line of a hundred-thousand-line closure, this ring is far more useful than a stack trace: the stack trace tells you which function of the lowerer crashed; the breadcrumbs tell you which line of the *user's source* it was lowering at the time.

### 6.2.2 A duck-typed dispatcher

`_emit_expr_impl` in `expr_dispatch_lowering.py` is the master expression dispatcher, and its predicates look like this:

```python
def _expr_is_subscript(expr: Expr, kind: str) -> bool:
    return isinstance(expr, Subscript) or kind == "Subscript" or (
        _expr_has_attr(expr, "obj") and _expr_has_attr(expr, "idx")
    )
```

`isinstance`, then a type-name string compare, then a structural `hasattr` check — triple redundancy. In an ordinary project this is a code smell; here it is another self-hosting fossil. These predicates must also hold inside the binary that pcc1 compiles, and pcc1's `isinstance` over cross-module dataclasses has been through unreliable phases (the `dict.get` incident in 6.3.3 belongs to the same family). The structural check is semantically redundant and operationally insurance. `stmt_dispatch_lowering.py` does the same for statements: `_stmt_is_assign(stmt)` is decided by `hasattr(stmt, "targets") and hasattr(stmt, "value")`.

Below the dispatcher, the mixins cooperate through one uniform protocol: **a specialized lowering function returns `Optional[ir.Value]`, where `None` means "this shape is not mine"; the caller tries the next candidate until the general path or a `NotImplementedError`.** `_maybe_emit_literal_str_format`, `_emit_native_weakref_call`, and `_maybe_emit_exact_int_object` all have this signature. The protocol carries an unwritten but strict discipline: a function must not emit any IR before deciding to return `None` — the bail-out decision has to happen before side effects, or "not mine" leaves orphaned half-emitted instructions in the IR stream. The format lowering in 6.4.5 is the textbook example of that discipline.

The strict-mode philosophy (see Chapter 5) shows up in the dispatcher's final branch: an unrecognized expression raises `NotImplementedError("Layer 1 does not handle expression ...")` — it does not silently fall back. Whether a fallback is available at all, and to what extent, is decided by the `--python-libpython` mode, not by the dispatcher on its own initiative.

## 6.3 The Split: from 20,195 Lines to 56

The split was not one refactor; it was a month-long campaign that fell through the self-hosting floor twice. The full record is in [docs/investigations/codegen-mixin-self-cross-module-types.md](../../docs/investigations/codegen-mixin-self-cross-module-types.md) and [docs/investigations/layer1-host-helper-context-gap.md](../../docs/investigations/layer1-host-helper-context-gap.md). The main line matters here because it directly defines today's split rules.

### 6.3.1 First fall: the pure refactor that wasn't

On 2026-05-08, fifteen commits moved native-module lowering out of `layer1.py` into eleven `native_*.py` mixins. Under host CPython this was a pure refactor: same behavior, same IR. But the no-libpython self-host gate went red on the spot: `tests/test_fallback_baseline.py` reported the fallback count jumping from a baseline of 0 to 1636.

The root cause was in type inference: `type_infer.py` assigned a method's implicit `self` parameter the type of **the class the method physically lives in**. For a method body inside `class NativeTextModulesLoweringMixin:`, `self` was typed as that mixin — an empty class with no fields. Every `self.builder`, `self.runtime[...]`, and `self._fresh(...)` inside the body failed to resolve, collapsed to `DynType`, and lowered as `py_cpy_getattr` / `py_cpy_call*` — that is, the libpython fallback. Eleven mixins times roughly 150 dynamic calls each ≈ 1636. The numbers matched exactly.

The investigation records why all three "obvious" workarounds were non-solutions: a `self: "L1CodeGen"` forward-reference annotation (the class table is per-module; `L1CodeGen` is not visible from the mixin's module), `if TYPE_CHECKING:` imports (the inferencer does not process the block, and the target module is circular anyway), and a direct import (a genuine circular import at host-CPython level). The real fix had two halves. On the inference side, a `derived_class_map`: when a mixin is a unique base of `L1CodeGen` within the closed-world closure, its methods' `self` is inferred as `L1CodeGen`. On the codegen side, `self.attr` loads, `self.attr = value` stores, and `self.method(...)` calls were all changed to resolve against **the inferred receiver type** rather than `current_class`, the class physically being lowered.

How the last fragment was found is worth retelling, because it shows the typical shape of this bug class. After moving three tiny try-err-block helpers into `ExceptionLoweringMixin`, pcc1 crashed compiling stage2 in an unrelated module. The decisive clue: `_push_try_err_block` succeeded while `_restore_try_err_block` failed. The former always stores a non-null block; the latter may store back `None` — and with the receiver type wrong, `self._try_err_block = prev_err_block` fell into the generic attribute-store path instead of the concrete field slot. The same assignment statement, different value ranges, different lowering paths. This value-dependent forking is a miniature of the multi-path theme of Section 6.6.

### 6.3.2 Second fall, second mechanism: the contextual host param

A later attempt to extract `isinstance` lowering as plain helper functions — first parameter `host`, receiving the `L1CodeGen` instance — fell through the same floor again: `host` was inferred as `DynType` and the whole function body fell back ([docs/investigations/layer1-host-helper-context-gap.md](../../docs/investigations/layer1-host-helper-context-gap.md)). The fix this time was a new mechanism: the pipeline automatically enables `contextual_host_params` for top-level functions under `pcc.frontends.python.codegen.*` whose first parameter is named `host`, binding `host` to a synthetic `L1CodeGen` host type; in parallel, the syntactic recognizer in `ir_scaffold_lowering.py` learned to accept `host.builder.*` as an IR-scaffold receiver. Since then Layer-1 has two legitimate split shapes: mixin methods (via receiver-aware inference) and host-param helper functions (via the contextual host type), each pinned by dedicated fallback-ratchet tests.

### 6.3.3 A semantic landmine exposed along the way: `dict.get` on a missing key

The `isinstance` extraction also detonated a landmine unrelated to the split itself, visible only on the bootstrap chain (recorded in the 2026-05-14 update of `bootstrap-self-time-after-layer1-split-2026-05-13.md`): the compiler built by pcc1 treated the missing-key result of `_BUILTIN_TYPE_TAGS.get(class_ident)` as the integer `0` rather than `None`, so every user-defined class's `isinstance` check walked into the builtin-type-tag branch and uniformly returned `False`. The minimal reproducer is five lines. The fix avoids the fragile shape on the bootstrap path, using explicit membership instead:

```python
if class_ident not in _BUILTIN_TYPE_TAGS:
    return None
tag = _BUILTIN_TYPE_TAGS[class_ident]
```

The lesson hardened into a repository idiom: on bootstrap-critical paths, do not rely on "missing returns None" idioms that are sensitive to boxing representation. Green host tests are not evidence here — this landmine can only be stepped on during the pcc1→pcc2 stage.

### 6.3.4 The split rules

Three rules, all reverse-engineered from the incidents above:

1. **Every split is a candidate semantic change and must pass the bootstrap gate.** In a self-hosting system, "pure refactor" is a proposition that needs proof, not a default assumption.
2. **Line count is not performance.** A split's justification is maintainability and growth discipline; performance comes from profiling the real hot spots (IR byte volume, the hoist rewrite loops), not from shorter files.
3. **New behavior goes into the narrowest mixin.** This is written in [AGENTS.md](../../AGENTS.md): "choose the narrowest existing mixin or native module; do not grow `layer1.py` again."

## 6.4 Representative Mixins, Read Closely

Eighty-six mixins cannot all be covered. The five below were chosen because each represents a class of design problem.

### 6.4.1 exception_lowering: the emission side of the checked-call model

pcc has no Itanium-style stack unwinding: `py_raise(exc)` stores the exception in TLS and **returns normally** (the runtime side is Chapter 8). Exception control flow is therefore woven entirely by codegen at call sites, and `_emit_post_call_err_check()` in `exception_lowering.py` is the needle:

```python
def _emit_post_call_err_check(self, span=None) -> None:
    """After any call that could raise a Python exception, emit
    `if (py_err_occurred()) goto err_target` ..."""
```

The error target has two levels: the current try's err block (`_current_try_err_block()`), otherwise the function-level error exit `_ensure_fn_err_exit()` — which emits a sentinel return by type (NULL for pointer returns, 0 for integers, and a special case for `main`: print the unhandled exception and return 1). `_emit_try` expands try/except/else/finally into a basic-block graph: in the err block, each handler is tested in turn with `py_exc_matches`; if none match, control branches to the outer err target. A handler binding (`except E as e`) must be retained and registered in `_except_binding_names` — a registration that feeds directly into GC frame roots (see the exc-referent case study in Chapter 10).

Two details show the model's cost-consciousness. First, a span-carrying err check routes through an intermediate block that appends a traceback frame, and `_ensure_post_call_frame_block` deduplicates those blocks by a (function, target, location) key — otherwise every call site grows its own frame block and IR size runs away. Second, `_emit_post_call_err_check` is **suppressed** inside functions marked `@c_abi_export`: such runtime functions may be invoked while TLS already holds a pending exception (for instance during except-handler dispatch), and an indiscriminate check would misattribute someone else's exception to the local call. Runtime functions keep the C runtime's explicit NULL-return protocol instead.

This is where the chapter's most important lowering obligation originates: **after any runtime call that may raise, the emission site must insert an err check (or an equivalent NULL-routing).** The symptom of a missing check is deceptive — not a crash, but a *late-detonating* exception: it skips the try/except that should have caught it and surfaces at the next site that happens to check, or at process teardown. The audit in 6.6.3 is the systematic settling of accounts for this obligation.

### 6.4.2 subscript_lowering and exact_int_lowering: one `d[k]`, two paths

`_emit_subscript_load()` in `subscript_lowering.py` is the "main road" for subscript loads: it branches on `expr.obj.ty` — `ListType` calls `py_list_getitem` (IndexError), `DictType` calls `py_dict_getitem` (KeyError), both followed by `_emit_post_call_err_check`; `TupleType`, `StrType`, the bytes family, and `DynType` each get a branch; the result is unboxed against the static element type via `_coerce_from_object`.

But it is not the only road. `exact_int_lowering.py` exists because of obligation 7 (see Chapter 16): pcc's `int` is an arbitrary-precision *semantic* type, with a value projection (the i64 lane) and an object projection (the boxed bignum). When an int expression must participate as an exact object — a literal beyond i64, a `**`, or an environment flag saying the variable already lives in the object lane — `_maybe_emit_exact_int_object()` takes over and computes in the object lane with runtime helpers like `py_int_add` and `py_int_floordiv`. And when such an expression is a subscript load, this path has **its own** subscript lowering: `_emit_subscript_load_object()`, which internally branches over List/Tuple/Dict/Dyn just the same and likewise calls `py_dict_getitem` with the err check.

So the same source shape `d[k]` lowers through two functions in two files depending on consuming context: `x = d[k]` goes through `_emit_subscript_load`, while `print(d[k])` — an object context where `print_lowering.py` first tries `_maybe_emit_exact_int_object` — goes through `_emit_subscript_load_object`. In the IR the two are distinguishable by result names: `dict.getitem`/`list.getitem` versus `dict.getitem.obj`/`list.getitem.obj`. That pair of names is the cheapest forensic tool for this class of problem, and the dual-path structure itself is the stage for the case study in 6.6.1.

### 6.4.3 for_loop_lowering: one semantic, N specializations

`_emit_for()` in `for_loop_lowering.py` is a specialization dispatch table. Before dispatch, three normalizations run: for-else desugaring (introduce a `__forelse_broke__` flag, rewrite every `break` in the body to "set flag; break", and guard the else body with a native i1 compare after the loop — the comment explains why this must not route through a Python-level bool comparison: it would box), `enumerate`/`zip` rewritten to indexed iteration, and tuple targets rewritten to a scalar target plus an unpacking assignment. Then dispatch by iterable shape: `range(...)` takes the i64 induction-variable fast path; CPython-backed values go to `_emit_for_cpython_iter`; `ListType`/`TupleType` iterate by length and indexed element access; `DictType` materializes `py_dict_keys` and reuses the list path; `StrType` iterates codepoints by slicing; a known user class with `__next__` goes through `_emit_for_native_iterator`; `DynType` uses the generic object iterator protocol in `_emit_for_obj_iterator`.

The generic protocol path's block structure deserves a close look, because it is the canonical sample of "an err check in equivalent form." `py_obj_next` returning NULL means one of two things: normal exhaustion (StopIteration) or a genuinely raising iterator. The lowering:

```text
for.obj.next:      item = py_obj_next(it); item == NULL ? maybe_end : body
for.obj.maybe_end: py_exc_matches(cur_exc, StopIteration) ? clear : propagate
for.obj.clear:     py_clear_exception(); br end
for.obj.propagate: br <try err block or err.exit>
```

The block-name pair `maybe_end`/`propagate` later became the recognized signature of "reviewed, equivalent routing" in the err-check audit (6.6.3).

Specializations also intersect generator semantics: inside a generator body, a `range` loop is **forbidden** from taking the induction-variable fast path, because the fast path keeps its counter in a raw entry-block alloca that is not part of the persisted generator frame; after a `yield`, resume re-enters with a reset counter and the loop terminates after one item. The source comment pins this bug and its regression test (`test_generator_range_loop_resumes`) right at the dispatch point. This is the built-in tax of a specialized structure: every fast path must individually justify its interaction with every semantic dimension — generators, exceptions, GC.

### 6.4.4 ownership_lowering: the emission side of owned/borrowed

The runtime reference contract — calls return owned references; callees retain when returning borrowed values (see Chapter 9) — requires codegen to make a static judgment at every consumption site: do I own this value, and should I release it when done? The core of `ownership_lowering.py` is the predicate `_expr_returns_owned_object()`: call expressions are judged by the callee's return type; list/dict/tuple/string literals and subscript results are owned; bare `Name` references are borrowed; `self.attr` loads are judged by the declared field type. The consumption side balances with `_gc_release_if_owned(obj, source_expr)`, which emits `pcc_gc_release` only after confirming the value is a pointer, the source expression owns it, and it is not a CPython-bridged value.

The other half of the job is GC roots: `_mark_owned_local_if_object()` registers owned-object locals in `_owned_local_names` and roots them via `_ensure_owned_local_gc_root()` (slot-granularity `pcc_gc_frame_enter`, one contract shared by all five GC backends — see Chapter 10). One easily missed corner: the function-level error exit must also handle root-leave for already-rooted locals, which is why `_ensure_fn_err_exit` ends by calling `_patch_fn_err_exit_gc_root_leave` for each name in `_gc_rooted_local_names`. A frame-root leak on the exception path never shows up in host tests; it only becomes a dangling root in long runs under a tracing GC backend.

Misjudgment on the emission side has a punishment in each direction: treat borrowed as owned and you over-release — use-after-free; treat owned as borrowed and you under-release — a leak. Rule 5 of the bootstrap-regression discipline in [AGENTS.md](../../AGENTS.md) exists precisely for this: ownership failures must be fixed by verifying the callee/caller contract and retaining in the **callee**, never by making the caller stop releasing.

### 6.4.5 format_lowering: compile-time parsing and side-effect-free bailout

`format_lowering.py` shows the `Optional` protocol at its cleanest. `_maybe_emit_literal_str_format()` handles `"...{}...".format(args)`: `_parse_auto_format_literal()` parses the format string **at compile time** — brace escapes, field-reference classification (auto/index/name), format-spec splitting — and enforces CPython's rule that automatic and manual numbering must not mix (mixing returns `None` rather than guessing a semantic). On success the whole format call lowers to a chain of `py_obj_format` and `py_str_concat`; on failure it returns `None` and the caller takes the general path.

The supporting `_resolve_str_literal_value()` embodies the methodology of "grow specializations toward real code patterns": format strings are often not literals but constant variables (`fmt = "{x}"; fmt.format(...)`), so this function searches the current function body, then the module body, for **exactly one** `Name = StrLit` binding, giving up on any other rebinding shape (AugAssign, a for target, an except binding, a with-as). The docstring credits the idiom to NumPy's `numpy/__init__.py` — the direction of specialization growth is driven by real packages, while the implementation stays generic, with no package-name special case (obligation 3).

`_emit_format_spec_builtin()` opens fast paths for common specs — `format(v, "08x")`, `","`, `".3f"` — calling `py_int_format_hex` / `py_int_format_decimal` / `py_float_format_fixed` directly. One small but characteristic comment: the thousands-grouping parameter carries the separator's **byte value** (44 for `,`, 95 for `_`) because "passing a bare 1 here would make the runtime emit chr(1)". Comments like this exist because each of them used to be a bug.

## 6.5 native_*.py: Native Module Lowering

Twelve `native_*.py` files (asyncio, dataclasses, files, gc, math, modules, os, system, text_modules, threading, virtual_thread, weakref) handle the lowering of "import a stdlib module and call it". The uniform pattern, with `native_weakref.py` as the sample:

```python
def _emit_native_weakref_call(self, expr: Call) -> Optional[ir.Value]:
    attr = expr.func
    if (not isinstance(attr.obj, Name)
            or self._native_builtin_module_for_name(attr.obj.ident) != "weakref"):
        return None
    return self._emit_native_weakref_value_call("weakref." + attr.name, ...)
```

First confirm via the alias table that the receiver really is the module (import aliases and from-imports are normalized into this table), then dispatch on a `"module.member"` string to a concrete runtime call; unknown members and unsupported argument shapes return `None`. `native_modules.py` is the group's coordinator and grab bag, and it also hosts a class of pure compile-time folding: constants like `string.ascii_lowercase` are inlined as string literals and `codecs.BOM_*` as bytes — the module need not exist at runtime at all.

The relationship between native-module lowering and "ecosystem support must be generic" (obligation 3) needs spelling out. Dispatching on a **module name** looks like a special case, but what it special-cases is the lowering target of standard-library semantics (weakref maps to `py_weakref_new`, gc maps to `pcc_gc_*`) — part of the compiler's knowledge of its own language runtime, not favoritism toward a third-party package. What is forbidden is the `if package == "numpy"` branch added to pass a gate; the package machinery of Chapter 17 runs on an entirely different, generic path.

In `native_weakref.py`, the constructor calls for `weakref.ref` and `weakref.proxy` are each followed by `_emit_post_call_err_check`, with a comment noting that `py_weakref_new` can raise (TypeError on valueclass payloads) and that without the check the pending exception skips enclosing try/except blocks. Those two lines were among the first true-positive fixes of the audit in 6.6.3 — and the native-module group is exactly where the err-check obligation is most easily forgotten: dozens of emission sites per file, written by different authors months apart.

## 6.6 History and Lessons

### 6.6.1 The dual subscript path: fixing half equals half-broken (2026-05-30)

**Symptom.** Under strict no-libpython (`--backend self --python-libpython=off`), `d['missing']` did not raise KeyError but printed `<null>`; an out-of-range `a[9]` was equally silent. An enclosing `try/except KeyError` caught nothing. A CPython diff (the reference-oracle technique from the debugging playbook) immediately confirmed this as a semantic defect rather than an unimplemented feature: the program ran "successfully" and produced wrong output.

**Root cause.** The statically-typed subscript path was calling `py_dict_get` / `py_list_get` — the runtime's **non-raising** query primitives, which return NULL on a missing key or out-of-range index. Codegen neither checked for NULL nor expected an exception, and the NULL flowed straight into print.

**Fix, and the trap.** The fix (the docstring of [tests/python/test_native_subscript_raise.py](../../tests/python/test_native_subscript_raise.py) is the authoritative record) has three layers. The runtime gained raising variants — `py_dict_getitem` (a KeyError carrying the key, via `py_exc_new_with_value`) and `py_list_getitem` (IndexError) — mirrored in both C (`py_dict.c` / `py_list.c`) and the pcc-Python ports (`py_dict.py` / `py_list.py`; see Chapter 14 for the mirroring discipline). `py_dict_get` / `py_list_get` keep their non-raising semantics, because `dict.get()` / `pop()` / `setdefault()` and a set of internal callers depend on them. The trap was in the frontend: subscript loads have **two** lowering paths (6.4.2). Switching only `_emit_subscript_load` in `subscript_lowering.py` fixes `x = d[k]`, while `print(d[k])` — the exact-int object context, through `_emit_subscript_load_object` in `exact_int_lowering.py` — keeps calling the old primitive and keeps printing `<null>`. A half-fix is observationally hard to distinguish from no fix, because the most common verification shape (assign, then print) may exercise only one of the two paths. In the end both functions were switched to `py_*_getitem`, both gained `_emit_post_call_err_check`, and both sites carry nearly word-for-word identical comments (`was py_dict_get, which returned NULL silently -> "<null>"`) — that deliberate parallelism in the comments is the path-coupling warning left for the next editor.

**Proof of recurrence.** This is not a one-off but a structural tax. The valueclass key-projection fix of 2026-06-05 (recorded in [docs/current-goal-state.md](../../docs/current-goal-state.md) at the time) again touched both places: "`subscript_lowering.py` and the object-form helper in `exact_int_lowering.py` now route direct valueclass constructor dict/object getitem keys through ValueBox projection." Any new semantic that flows through subscript keys or values must assume, by default, that both paths need the change.

**Invariant left behind.** The regression [tests/python/test_native_subscript_raise.py](../../tests/python/test_native_subscript_raise.py) verifies both shapes in the default runtime mode (linking the pcc-Python ports, not `PCC_RUNTIME_CC=cc`), and the IR result names `dict.getitem` versus `dict.getitem.obj` entered team memory as the path discriminator.

### 6.6.2 The six division lowering paths (2026-05-30)

**Symptom.** Found the same day, by the same methodology (diffing a realistic program's output against CPython), one layer deeper: under strict no-libpython, `10 // 0` returned `0`, `10 % 0` was undefined behavior, the dyn-path `%` printed `<null>`, and `try/except ZeroDivisionError` was wholesale inert. This violates obligation 2 directly: silently returning a wrong value is not a performance tradeoff; it is semantic corruption.

**Not one root cause — six.** The investigation ([docs/investigations/zero-division-silent-no-libpython-six-paths.md](../../docs/investigations/zero-division-silent-no-libpython-six-paths.md)) enumerated every frontend lowering path for integer and float division: ① unboxed i64 `//` and `%` (ARM64 `SDIV` by zero silently yields 0); ② the boxed runtime binop path; ③ the exact-int boxed path (`py_int_floordiv`/`py_int_mod` return NULL on zero without setting an exception — the runtime comment defers the raise to the caller, and no caller picked it up); ④ the dyn object path through `py_obj_mod`; ⑤ float `fdiv`/`fmod` (inf/nan, no trap); ⑥ the low_ir pure-leaf scaffold — the fast path generated for proven-pure functions, which has no error exit at all (`post_call_error_check=None`) and emits a bare `sdiv`, making a raise **structurally impossible**.

**The asymmetry of the fix.** The first five paths were fixed isomorphically: a zero-divisor guard (`_emit_zero_division_check`) or a NULL post-check (`_emit_zero_division_if_null`), spread across `binary_op_lowering.py`, `expr_helper_lowering.py`, and `exact_int_lowering.py`. The sixth could not be fixed in place — a fast path without an error exit cannot emit a raise — so the solution was **exclusion**: `_low_ir_nonzero_literal()` keeps a division on the fast path only when the divisor is a provably nonzero literal (`x // 2`, `n % 256`); a variable or zero divisor bails the entire function back to the guarded full lowering. This is the general principle for conflicts between a specialized fast path and a semantic obligation: **when the fast path cannot honor the obligation, shrink the fast path's admission condition; never discount the obligation.** Constant-divisor hot paths lost nothing.

**Lesson.** When one semantic is spread over N paths, the unit of repair is the path set, not the path that happened to show the bug; the first artifact an investigation should produce is the path enumeration, not a patch. The regression ([tests/python/test_native_zero_division.py](../../tests/python/test_native_zero_division.py)) is parameterized by path-triggering shapes: inline dyn, typed cross-function, bignum, float `//`, plus the inverse assertion that the constant-divisor fast path is still taken. The repository's recurring counts — two subscript paths, six division paths, four-plus float-to-string paths — keep confirming the same fact: multiple paths are the standing cost of a specializing compiler, and the only countermeasure is a path inventory plus all-path regressions.

### 6.6.3 From spot fixes to a systematic audit: the emission-site err-check audit (2026-06-11)

**Origin.** Three independent bugs in one evening shared one shape — a runtime function calls `py_raise`, the emission site lacks `_emit_post_call_err_check`, and the exception detonates late, skipping try/except (native weakref constructors, weak-dict subscript stores, generator `throw`). After the third, the right move was not to fix a fourth but to settle the whole class ([docs/investigations/emission-site-err-check-audit.md](../../docs/investigations/emission-site-err-check-audit.md)).

**Method.** A two-step mechanical scan: collect the C runtime functions whose bodies contain `py_raise(` (78 of them); then flag every `self.runtime["<fn>"]` emission site in [pcc/frontends/python/codegen/](../../pcc/frontends/python/codegen) with no err check within the next 8 lines — 58 suspects. Then the audit's key discipline: **a suspect is not a bug.** Each family first gets a "red probe" (confirm the expected behavior under CPython and the wrong behavior under pcc) before any code changes. The distribution of outcomes is instructive: the `py_obj_next` family — five sites — was entirely false positives (the `maybe_end`/`propagate` equivalent routing of 6.4.3, which the 8-line window simply cannot see); the regex-engine family — three sites — false positives too (the checks sat past multi-line argument lists, beyond the window); the binary-op family was a true positive ×2 — and its red probe dug up a deeper runtime hole along the way: `py_obj_add/sub/mul` never dispatched user `__add__`-family dunders at all; the generator `throw`/`close` family was a true positive whose newly inserted check immediately exposed a second hole, `py_gen_close` leaving its injected GeneratorExit pending in TLS.

**Lessons.** First, the late-detonation symptom makes missing err checks naturally resistant to case-by-case discovery; the class deserves periodic mechanical audits. Second, the audit heuristic's false-positive patterns must be written back into the audit document (the block-name signature `maybe_end`/`propagate` means reviewed-equivalent routing; the scan window should extend to the end of the enclosing statement, not a fixed line count). Third, adding a missing check frequently makes a deeper runtime hole visible on the spot — the check is not just a fix; it is the instrument that makes bugs observable.

## 6.7 Summary

Nothing in Layer-1 codegen's physical shape — the 56-line facade, the 86 mixins, the 12 native modules, the duck-typed dispatch, the copied class attributes — is a free-floating aesthetic choice. Almost all of it is shaped by two forces acting together: semantic density (every Python expression shape needs explicit basic-block weaving) and the self-hosting constraint (this code must be compiled by the very compiler it describes). The split history proves that in a self-hosting system "pure refactor" is a claim requiring gate-level proof. The dual subscript path and the six division paths prove that the real cost of specialized fast paths is not writing them but multiplying every future semantic change by the path count. The err-check audit proves that for bug classes with late-detonating symptoms, systematic settling beats case-by-case firefighting. Two threads continue into later chapters: the runtime half of the checked-call exception model in Chapter 8, and the runtime half of the ownership contract in Chapter 9.

## Exercises

1. **Read and verify.** Compare `subscript_lowering.py::_emit_subscript_load` with `exact_int_lowering.py::_emit_subscript_load_object`: list every difference in err checking, `_gc_release_if_owned`, and result unboxing (`_coerce_from_object`). Neither function's `TupleType` branch is followed by an err check — go to the runtime sources under [pcc/runtime/src/](../../pcc/runtime/src) and establish what `py_tuple_get` does on an out-of-range index, then judge whether the missing check is equivalent routing, a deliberate exemption, or an open hole.
2. **IR forensics.** Write a small program containing both `x = d["k"]` and `print(d["k"])`, compile it in strict mode with `--emit-llvm`, find the `dict.getitem` and `dict.getitem.obj` calls in the IR, and mark the `py_err_occurred` check block that follows each.
3. **Design tradeoff.** Propose a refactor that merges the dual subscript paths into one shared helper. Identify at least three points of resistance (hint: different unboxing needs on the result, different ownership-release points, the exact-int path's object-form requirement on keys), and argue how your design would pass the five-GC bootstrap gates without introducing a third path.
4. **Audit practice.** Find the `maybe_end`/`propagate` block structure of `_emit_for_obj_iterator` in `for_loop_lowering.py` and explain why the absence of `_emit_post_call_err_check` after `py_obj_next` does not violate the obligation of 6.4.1; then locate the isomorphic structure in `comprehension_lowering.py` and assess the drift risk between the two.
5. **Argue a position.** Section 6.3 ends with two legitimate split shapes: mixin methods (receiver-aware inference) and host-param helper functions (contextual host typing). Compare them along three axes — inference cost, testability, and exposure to pcc1's capability boundary — and argue why `isinstance_lowering.py` was the right candidate for the latter.
