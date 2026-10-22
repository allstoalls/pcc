# Chapter 5: The Typed-Python Frontend

pcc's Python path begins with a text file and ends with an AST in which every expression carries a type annotation — lowering that tree to LLVM IR is Chapter 6's business. This chapter covers the four stages of the frontend chain: a hand-written lexer and recursive-descent parser ([pcc/frontends/python/py_lex.py](../../pcc/frontends/python/py_lex.py), `py_parse.py`), lifting into a frozen AST ([pcc/frontends/python/py_lift.py](../../pcc/frontends/python/py_lift.py) → [pcc/frontends/python/py_ast.py](../../pcc/frontends/python/py_ast.py)), pipeline assembly and mode adjudication ([pcc/frontends/python/pipeline.py](../../pcc/frontends/python/pipeline.py)), and annotation-driven type inference ([pcc/frontends/python/type_infer.py](../../pcc/frontends/python/type_infer.py)). But mechanism is only half the story; the other half is three design rulings that have to be settled first: why pcc is a typed-subset compiler rather than a full-Python JIT; why unsupported idioms fail loudly by default instead of falling back silently; and what the three-state `--ir-scaffold` flag is actually adjudicating. The answers to these three questions lock together, and between them they determine the shape of every layer of the frontend.

## Chapter Overview: Start with the Controlled Python Subset

The key question is not how much Python syntax is accepted. It is how the frontend turns a Python program into a structure the compiler can trust: lexing and parsing produce an AST, lifting reshapes it for compilation, type inference marks objects and values, and unsupported dynamic behavior is rejected explicitly.

- The typed-Python frontend serves the bootstrap path, so it rejects unclear code instead of quietly using CPython.
- `ir-scaffold` is part of the strict route: missing coverage becomes a compile-time failure, not a runtime fallback.
- Whenever the chapter says "supported," ask whether that means host pcc only, or also the pcc1/no-libpython chain.

## 5.1 The Problem and the Design Space

### 5.1.1 Why a typed-subset compiler, not a full-Python JIT

"Make Python fast" already has a mature answer: a PyPy-style tracing JIT that watches hot paths at runtime, speculates on the observed types, and deoptimizes back to the interpreter when speculation fails. If pcc's goal were to accelerate arbitrary Python programs, the JIT would plainly be the better engineering route — it demands no annotations from the user, no semantic subset, and it is naturally at home with dynamic idioms.

pcc did not take that route, because pcc's thesis is not acceleration but **owned execution** (see Chapter 1): compiling auditable native artifacts, no-libpython deployment, and a compiler that can compile itself. Each of those three goals rules out a JIT on its own:

First, a JIT's product is an in-process machine-code cache, not a distributable, auditable binary; what pcc wants is the `hello` file itself after `pcc hello.py -o hello`. Second, a JIT is an acceleration layer on top of a host runtime — its interpreter, object model, and deoptimization machinery all presuppose a complete Python runtime — whereas pcc's no-libpython obligation requires precisely that the final artifact not depend on the CPython runtime. Third, and most binding: pcc's bootstrap fixed point (pcc1→pcc2→pcc3, see Chapter 15) requires the compiler to compile **its own source** into a native binary and then have that binary repeat the same act. Only an AOT compiler can do this, and AOT-compiling a dynamic language forces you to answer "which semantic subset can be lowered statically" — the typed subset is not laziness, it is the honest answer to that question.

The subset route has one easily overlooked byproduct: it gives "is the subset large enough" a falsifiable yardstick. The source of pcc's own frontend, pipeline, type inference, and codegen must all fall inside the subset, or the bootstrap gate goes red. [README.md](../../README.md) states this bluntly: the self-hosting path is stricter than ordinary user code — pcc's own source must still avoid or quarantine runtime `getattr`/`setattr`, string-keyed method dispatch, decorators with runtime side effects, and similar dynamic idioms — and it says explicitly that "this is a real current limitation of the bootstrap track, not just a documentation gap." In other words, the boundary of the subset is not marketing copy; it is an engineering fact tested every day by the closure of a real compiler.

The costs must be booked honestly too. A typed subset means pcc's Python frontend today does not implement the full Python data model; [docs/python-limitations.md](../../docs/python-limitations.md) (snapshot 2026-04-20) lists `eval`/`exec`, `__import__` hooks, and import-time metaclasses as "never planned," and the status table in the README labels the entire Python frontend Experimental. Following the discipline of claim hygiene, this book relays those labels as written and does not promote them.

### 5.1.2 The strict-mode philosophy: why unsupported idioms fail loudly by default

When a subset compiler meets code outside its subset, it has two choices: silently bridge to CPython (link libpython and hand the unrecognized operation to `PyObject_*`), or fail hard with a diagnostic. pcc's default is the latter: `pcc hello.py` is equivalent to `--python-libpython=off --ir-scaffold=on`, and any program that would need a CPython fallback simply fails to compile.

At first glance this default looks user-hostile — `auto` mode could have compiled the program just fine. The reason for insisting on it is that **silent fallback poisons every claim downstream.** If a binary that "compiled successfully" quietly linked libpython, the no-libpython deployment claim is false; if a benchmark's hot path actually ran on the CPython bridge, the performance number measured the bridge, not pcc; if a bootstrap stage silently imported a host module, the fixed-point evidence is false. The first of pcc's seven obligations requires every compatibility claim to be mode-labeled (libpython ≠ no-libpython), and mode labeling is only enforceable when "fallback is a countable, discrete event" — which is exactly the precondition of the fallback ratchet [tests/fallback_baseline.json](../../tests/fallback_baseline.json) (see Chapter 14): you can ratchet events that are explicitly recorded; you cannot ratchet default behavior diffused through the code.

Mechanically, the philosophy lands in `_finalize_libpython_mode()` in [pcc/frontends/python/pipeline.py](../../pcc/frontends/python/pipeline.py): when the mode is `off` and a fallback need is detected, it raises `PyPipelineError` with a message that names the file, lists the reasons, and tells the user explicitly that the unlock is to write `--python-libpython=auto/on`. The failure is loud and the exit is explicit — fallback ceases to be a default behavior and becomes a documented user decision. The full semantics of the three modes are in 5.4.3.

### 5.1.3 One pipeline, two generations of parsers

The frontend directory hides a lineage. [pcc/frontends/python/parser.py](../../pcc/frontends/python/parser.py) is the first-generation parser: it used CPython's standard-library `ast` module as the backbone and lifted `ast.AST` nodes into pcc's AST. It was fast to write and complete in coverage, but it had one fatal property: it itself depends on libpython. When pcc began compiling its own pipeline, that `import ast` edge dragged libpython back into the stage1 closure. The comment in `compile_python` in `pipeline.py` records the ruling: `pcc.frontends.python.py_parse` + `pcc.frontends.python.py_lift` is the bootstrap-safe parsing path; "the earlier CPython-ast escape hatch kept a libpython import edge alive in the compiled pipeline, so the self-hosting path no longer emits it." `parser.py`'s own comment pronounces its future: once the native parser becomes the hard default, this file can be deleted wholesale. Its residual value today is as a host-side tool for a handful of source-shape analysis tests.

This lineage sets the tone for the chapter: every file discussed below — lexer, parser, lifter — is at once pcc's frontend and an **input** that pcc1 must be able to compile, and that must run correctly once compiled. Many source shapes that look overly defensive are fossils left by this double identity.

## 5.2 Parsing: py_lex and py_parse

The full frontend chain looks like this, orchestrated end to end by `compile_python()` in `pipeline.py`:

```text
source.py
   │  pcc/frontends/python/py_lex.py     hand-written lexer: INDENT/DEDENT, NAME,
   │                          NUMBER, STRING, OP, KEYWORD (longest match)
   ▼
token stream
   │  pcc/frontends/python/py_parse.py   hand-written recursive descent:
   │                          Parser._parse_stmt keyword dispatch +
   │                          expression precedence ladder → narrow AST
   │                          (_Module, _FuncDef, _Call, ... _* dataclasses)
   ▼
narrow AST
   │  pcc/frontends/python/py_lift.py    _Lifter: narrow AST → frozen py_ast; every
   │                          expression starts as ty=DynType; sentinel
   │                          encodings (_yield/_list_comp/...)
   ▼
py_ast.Module
   │  pcc/frontends/python/type_infer.py   infer_module: builds new nodes, fills ty
   ▼
typed Module  ──→  L1CodeGen.generate() (Chapter 6) ──→ LLVM IR text
```

### 5.2.1 The hand-written lexer

The module comment of `py_lex.py` states its design position outright: CPython's `Lib/tokenize.py` is **not** the parity target — "we deliver only what pcc needs." The lexer is a few hundred lines of hand-written code: an indentation stack produces `INDENT`/`DEDENT`, the keyword table is a tuple constant, multi-character operators match longest-first. There is no parser generator, and no reuse of the C side's PLY infrastructure — because this file must itself be compiled by pcc, and the narrower its dependency surface, the smaller the bootstrap closure.

### 5.2.2 Recursive descent and the narrow AST

The `Parser` in `py_parse.py` is textbook recursive descent: `_parse_stmt()` dispatches on keywords to `_parse_funcdef`, `_parse_if`, `_parse_try`, and friends; expressions walk a precedence ladder, descending from `_parse_expr` (which also accepts `lambda`, `yield`, walrus expressions, and the conditional expression) through `_parse_or`, `_parse_and`, comparisons, bitwise operators, shifts, unary operators, and `_parse_power`, down to `_parse_atom_trailer`/`_parse_atom` for call, attribute, and subscript trailers. CPython's own parser (`Parser/Python.asdl` and `parser.c`) is cited in comments as a reference, but the implementation is independent.

The parser's output is not `py_ast` but a set of narrow dataclasses meaningful only inside this file (`_Module`, `_FuncDef`, `_BinOp`, …). The two-layer AST split is deliberate: the narrow AST lets the parser evolve freely (add nodes, change fields, without touching the public contract), while the frozen `py_ast` is the interface between frontend stages (5.3.1).

Two design points deserve a pause.

**The soft keyword `match` is desugared inside the parser itself.** `match` remains a legal identifier in Python, so `_parse_stmt` first runs the lookahead test `_looks_like_match_stmt()` and only enters `_parse_match()` once it has confirmed the `match subject:` shape. And `_parse_match` constructs no match-specific node at all — it generates a temporary name `__pcc_match_N` to hold the subject, translates each `case` pattern into conditions and bindings (`_match_pattern_condition_bindings`), and folds the cases, in reverse, into an if/elif chain. The cost is that the semantic ceiling of pattern matching is whatever subset this condition translation can express; the gain is that `py_ast` and every stage after it — inference, lowering, both backends — never needs to know `match` exists. For a compiler that puts bootstrapping first, this is the right asymmetry: syntactic sugar is digested as close to the syntax as possible.

**Diagnosability is designed for pcc1, not for the host.** `parse_module()` wraps each top-level statement in an exception context (statement index, plus the kind/text/line of the starting token), and with `PCC_DEBUG_PY_PARSE=1` it prints a breadcrumb per statement. Under host CPython this is redundant — Python's traceback is already enough; but when pcc1 (the compiled native compiler) fails while parsing one of pcc's own files during stage2, there is no host traceback to read, and these hand-built contexts are the only locating information. By the same logic, `_parse_float_literal()` evaluates float literals by hand (mantissa/exponent separation plus `_pow10f` accumulation) instead of calling `float(text)` — the compiled parser cannot assume the existence or shape of host conversion functions. The top of the file copies token-kind strings such as `TK_NEWLINE` into local constants, with a comment explaining that this avoids "pulling sibling-module constant imports through the multi-file CPython fallback path." All of these are the fossils of 5.1.3: **the source shape of the parser is molded by the requirement that it be compiled by itself.**

## 5.3 Lifting: py_lift and the Frozen py_ast

### 5.3.1 The frozen contract

[pcc/frontends/python/py_ast.py](../../pcc/frontends/python/py_ast.py) is the hub of the entire frontend, and its design can be summarized in three phrases: frozen, span-carrying, types on the nodes.

Every node is a `frozen=True` dataclass — immutable after construction; any "modification" must build a new node via `dataclasses.replace`. The direct beneficiary of this discipline is type inference: `infer_module` is a purely functional pass — tree in, new tree out — and the old tree stays valid forever. The file's docstring points to the authoritative contract, [docs/plans/python-frontend-interfaces.md](../../docs/plans/python-frontend-interfaces.md) section 2, a document frozen at v0.1 precisely so that multiple agents working in parallel could not change the interface unilaterally.

Every `Expr` carries two public fields: `span: SourceSpan` (file/line/column range, for diagnostics) and `ty: Type`. Several points in the type hierarchy are worth noting. `IntType` carries a `width: int = 64` field, which the comment calls a "tagged default" — this is the value-projection width of `int`, while `int`'s semantic type remains arbitrary precision; the overflow discipline belongs to the value model (see Chapter 16). `DynType` is the "could not determine" fallback type — not an error, but a contagious marker (5.5.3). `ClassType` separates `fields`, `bases`, and `properties` into three independent channels, and the source comment on the `properties` field cites an investigation document directly — 5.7.2 tells the story of how that field came to exist. `ValueClassType` is the optional annotation for value classes; the details belong to Chapter 16.

Just as important is what `py_ast` does **not** have: no set-literal node, no comprehension nodes, no `yield`/`await` nodes, no walrus node, no `match` node. This is the other face of the frozen contract — the smaller the node set, the fewer shapes inference and lowering must exhaustively handle, and the narrower the representation that two backends and the bootstrap chain must jointly support. Where does the excluded syntax go? The answer is in the lifter.

### 5.3.2 Sentinel encoding

The `_Lifter` in `py_lift.py` translates the parser's narrow AST into `py_ast`, filling every expression's `ty` with `DynType` for type inference to overwrite later. For syntax that has no corresponding `py_ast` node, the lifter uniformly uses a **sentinel-call** encoding — it constructs a `Call` node to a specially named function, which the lowering stage (Chapter 6) recognizes and rewrites:

| Source syntax | Lifted form |
|---|---|
| `{a, b}` | a `set([a, b])` call |
| list/dict/set/generator comprehensions | `_list_comp` / `_dict_comp` / `_set_comp` / `_gen_comp` calls, with generator clauses encoded as `_gen_clause(target, iter, (ifs,))` |
| `yield x` / `yield from x` | `_yield(x)` / `_yield_from(x)` calls |
| `await x` | an `__await__(x)` call |
| `*args` / `**kwargs` arguments | calls named `*` / `**` |
| `name := expr` | a `_walrus(target, expr)` call |
| f-strings | a combination of `format(x, spec)` calls and string concatenation |

The benefit of this encoding follows directly from 5.3.1: the frozen contract need not grow a node per piece of syntactic sugar. The drawback is a failure mode all its own — **sentinel leakage**: if a sentinel is never rewritten during lowering (because the shape it was constructed in is not one the rewriter expects), it survives to runtime as an ordinary name lookup and becomes an inexplicable `NameError: name '_yield' is not defined`. The error has moved from compile time to run time, and the reporting site is two stages removed from the root cause. The case study in 5.7.1 is this failure mode caught in the act, and it established the invariant for sentinel encoding: a sentinel may only be constructed in shapes the lowerer is guaranteed to rewrite.

### 5.3.3 Defensive lifting: the fossil layer of bootstrap input

`py_lift.py` is one of the most defensively dense files in the repository, and every defense can be traced to a real incident:

- `lift_module()` lifts top-level statements with an explicit loop rather than a generator expression, catching any exception and splicing the statement index, node type, line number, and file name into the `LiftError` it re-raises. The source comment points to [docs/investigations/pcc1-stage2-lift-expr-raw-value-leak.md](../../docs/investigations/pcc1-stage2-lift-expr-raw-value-leak.md) (5.7.3).
- `lift_stmt()` dispatches with concrete class-identity tests of the form `type(s) is pp._Pass`, and a comment explains that this beats `getattr(self, f"_s_{...}")` dynamic dispatch — because [scripts/audit_selfhost.py](../../scripts/audit_selfhost.py) flags dynamic-attribute patterns as self-host ineligible.
- `_node_ident()`/`_node_attr_name()` tolerate both field spellings — `ident`/`id`, `name`/`attr` — to stay compatible with different parser snapshots.
- `_parse_float_literal_lift()` carries an all-caps `WORKAROUND` comment: do not alias the parameter into a local that will be decref'd at scope exit; "always materialize a new owned string via slicing," pointing at the UAF investigation (5.7.3).

Translated into design language: **the lifter is the earliest complex code to execute inside the pcc1 process** — the first step of a stage2 compile is to parse+lift pcc's own source. If pcc1's codegen or runtime has any ownership, layout, or dispatch defect, the lifter is usually the first victim. Its source has therefore evolved into a superposition of two things: an AST translator, and a set of documented crash-site hardenings. When reading this file, do not delete the defensive patterns as if they were stylistic choices — nearly every one has a corresponding investigation document.

## 5.4 The Pipeline: Assembly and Mode Adjudication in pipeline.py

### 5.4.1 The single-file mainline

`compile_python()` is the single-file entry point, and its mainline is clean: read the source → `parse_and_lift()` (the `py_lift.py` wrapper that folds parse and lift exceptions into a single `LiftError`) → `_module_needs_libpython()` scans the AST to determine fallback need → `infer_module()` → `L1CodeGen.generate()` produces IR text (Chapter 6) → the IR pass pipeline → `_finalize_libpython_mode()` makes the final ruling → backend emission (Chapters 12 and 13).

Note that the fallback determination is made **twice**, with the second pass at the IR level: the AST scan can only see `import`s, but codegen may emit `py_cpy_*` calls for DynType method dispatch, `hasattr` fallback, and similar cases in source that contains no `import` at all. So the pipeline runs a second scan, `_ir_needs_libpython()`, over the IR text (matching `call` instructions rather than raw text, to avoid false positives from unconditionally emitted `declare` stubs), and the reasons from both sources are merged into `_finalize_libpython_mode`'s reason list: "imports still lower through CPython fallback," "generated IR still calls py_cpy_* helpers." When the compile fails loudly, what gets reported is not a bare "fallback needed" but the mechanism by which fallback was introduced.

### 5.4.2 Closure collection and multi-file

A single-file entry point does not mean only one file gets compiled. `compile_python` first calls `_collect_relative_module_closure()` to gather the relative-import closure — siblings reached via `from . import sibling` are folded into the same native compile; when the closure exceeds one file, or pure-Python stdlib modules must be compiled recursively, control transfers to `compile_python_multi()`. Before inference, the multi-file path runs an export pre-scan (`build_closed_world_context()`): parse+lift each module, extract each module's function signatures, class field schemas, and re-export edges, and assemble them into an `external_exports` table passed to every module's type inference — so that `from .sibling import fn` resolves to a `FuncType` at the call site instead of collapsing to `DynType`. This is the multi-file input to 5.5.

Import classification is the semantic core of this layer. `_classify_python_import()` adjudicates every import into one of five **deliberately stable strings** — stable enough that tests and bootstrap diagnostics assert on them directly:

```text
compile_time_only        erased at compile time (typing, etc.)
native_user_module       user module natively compiled in the same closure
builtin_native_dispatch  built-in native dispatch lowering
native_stdlib            resolved to a native pcc/stdlib stand-in
cpython_fallback         no native provider; hard failure unless explicitly allowed
```

Every ruling lands in structured logs via `_record_import_classification()`: with `PCC_LOG=import`, the pipeline emits JSON lines in the `pcc.import_log.v1` schema (module, classification, native or not, provider, ruling source). The classification is not log decoration — `cpython_fallback` is precisely the "countable, discrete event" of 5.1.2.

### 5.4.3 Two flags, six states

`--python-libpython` is resolved by `_resolve_libpython_mode()`, with the empty value defaulting to `off`:

- `off` (default): any fallback need → hard failure with `PyPipelineError`.
- `auto`: link libpython only when a fallback need is detected — a compatibility experiment mode, which the README explicitly warns must not be used for no-libpython claims.
- `on`: unconditionally allow and link the fallback surface.

`--ir-scaffold` is resolved by `_resolve_ir_scaffold_mode()`, and its semantics run deeper than its name. The question it adjudicates is: **when the source that pcc is compiling itself constructs LLVM IR** — that is, call sites like `self.builder.call(...)` and `ir.IntType(64)` inside pcc's own codegen modules — how do those call sites lower? This is a problem unique to self-hosting: ordinary user programs have no such call sites, but for pcc1 to run free of libpython, its own IR-construction layer must be compiled closed-world. The three states:

- `on` (default; source comments call it Path A): `IRBuilder` and `ir.*` call sites are lowered directly by the `ir_scaffold_lowering.py` mixin into native calls to external IR-builder symbols; the scaffold import set `pcc.extern`, `pcc.unsafe`, `pcc.ir`, `pcc.ir.compat` (`_SCAFFOLD_IMPORT_MODULES`) is treated as compile-time construction and does not count toward fallback; and `_filter_ir_scaffold_closure()` simultaneously rewrites the link closure — dropping `compat.py` and the LLVM-C binding `binding.py` (keeping them would drag libpython back into the self-backend path) and swapping in the real symbol provider, `pcc.ir.ir`. Builder methods not yet migrated raise `ScaffoldUnsupportedError`, which names the missing method.
- `off`: the explicit compatibility escape hatch — the old lowering path, where builder call sites still go through dynamic dispatch (and therefore usually require libpython to be allowed); `ScaffoldUnsupportedError` is never raised. The docstring of `ScaffoldUnsupportedError` draws the contrast plainly: OFF mode falls back silently to `py_cpy_*` dispatch; the error surface exists only in ON mode, **so that file-by-file migration can see exactly which symbols are still missing**.
- `auto`: a legacy hybrid mode. Today `_resolve_ir_scaffold_mode` normalizes both the empty value and `auto` to `on` — the closed world is already the default reality, and `auto` survives only as a CLI-compatible spelling.

Note the second appearance of the fail-loudly philosophy here: scaffold ON's failure mode (a `ScaffoldUnsupportedError` that names the method) and libpython OFF's failure mode (a `PyPipelineError` that names the reasons) are the same design instantiated at two levels. One more detail betrays the flag's bootstrap nature: `_ir_scaffold_enabled()` enables the scaffold **unconditionally** for three modules — `runtime_abi`, `layer1`, and `class_gen` — and not even the flag can stop it: without closed-world lowering, these modules cannot enter the stage1 closure at all.

## 5.5 Type Inference: Annotation-Driven, with DynType as the Honest Floor

### 5.5.1 A purely functional pass

The entry point of `type_infer.py`, `infer_module()`, takes a `py_ast.Module` and returns an entirely new tree: each expression's `ty` is filled with "the best type that can be determined, or `DynType` if none can," and annotations that existed at parse time as surface `Expr`s are replaced by first-class `Type` instances. Shared state is gathered in `_InferCtx` (module name, global scope, the `func_types` function-type table, the `class_types` class-type table, plus the multi-file `external_exports` and `derived_class_map`); name lookup walks the `_Scope` chain: locals → enclosing parameters → module globals → the builtins table.

Inference is annotation-driven — the fundamental difference from whole-program inference (HM-family systems): an annotated parameter takes its annotation type, an unannotated one is `DynType`; return types come from the `return_ty` annotation; local assignments take the annotation if present, otherwise the inferred type of the RHS. `_infer_funcdef()` registers the complete `FuncType` into `ctx.func_types` **before** walking the function body, so recursive calls can see their own signature; after the body is walked, if the return annotation is not `DynType`, `_check_returns()` checks every `return` for compatibility.

Expression rules are concentrated in `_infer_expr()` and `_binop_result()`. The latter is worth reading: `str + str` stays `str`, numeric `/` is always `float`, bitwise operators preserve int on int-like operands, `int ** int` is int and anything touching float is float; for clear type errors (`str +` a numeric), it raises a `PyFrontendError` with a hint via `_raise_frontend_error()`; and when no rule matches, it returns `TYPE_DYN` — not an error, a demotion.

### 5.5.2 Limited flow sensitivity: isinstance narrowing

Inference is essentially flow-insensitive, with one exception: `isinstance` narrowing. Inside the then-branch of `if isinstance(x, C):`, `_narrow_scope_for_isinstance()` pushes a child scope binding `x` to `C` (`and` chains are narrowed recursively via `_narrow_scope_for_cond()`). The conditions for narrowing are restrained: the current type must be `DynType`, or an assignable superclass of the candidate. More restrained still is how `_type_from_isinstance_arg()` handles the tuple form; the source comment reads: "Tuple forms describe a union. The frontend has no union type yet, so we deliberately leave those unnarrowed instead of guessing." That comment is the design temperament of the whole file — **the honesty of inference outranks its power**: better to let a type rest at `DynType` (and take the slower but semantically faithful path) than to introduce a refinement that might guess wrong.

### 5.5.3 How types decide lowering, and how DynType spreads

The output of inference directly determines the stratification of Chapter 6's lowering. The interface contract, [docs/plans/python-frontend-interfaces.md](../../docs/plans/python-frontend-interfaces.md) section 7, defines three execution tiers: L1 (all operands native, direct LLVM operations), L2 (native mixed with PyObject*, marshaling at the boundary), L3 (fully dynamic, everything through runtime dispatch), and lays down the rule for codegen: "if you cannot prove all operands are native, drop to L2; if you can prove nothing, drop to L3. **Never guess — emit the runtime call.**" Honesty requires noting that today's directory tree has drifted from this v0.1 plan: there are no files named `layer2.py`/`layer3.py`; the typed tiers live wholesale inside `L1CodeGen`, and the "dynamic tier" survives as DynType-driven runtime dispatch plus (where permitted) the `py_cpy_*` path. The tiers are a semantic fact, no longer a file boundary.

This explains why `DynType`'s contagion is the frontend's most important performance and correctness variable: once an expression is `DynType`, every consumer of it loses eligibility for native lowering, and under no-libpython it may push the compile straight into the hard failure of 5.4.1. Type information is a supply chain — the case study in 5.7.2 shows what happens when one link breaks: the runtime's and codegen's existing support is voided wholesale.

Class types are the bulk cargo on this supply chain. `_prepopulate_module_scope()` first registers the module's top-level classes and functions into the global scope; class definitions are assembled into a `ClassType` by `_class_fields_from_def()` (field schema), `_class_bases_from_def()` (bases), and `_class_properties_from_def()` (`@property` declarations, see 5.7.2); attribute access `obj.attr` walks the MRO from `_class_mro_list()` through `_lookup_class_field()` → `_lookup_class_property()`. In the multi-file case, `external_exports` lets a cross-module `from .sibling import C` obtain the full schema at inference time; `derived_class_map` handles the mixin shape — when a base class has a unique derived class within the closure, the base's methods are inferred with the derived class as the `self` type; the origin and cost of this mechanism are detailed in Chapter 6's split history. `contextual_host_params` and `l1_codegen_host_type()` are another face of the same problem: they supply a synthetic type for helper functions that receive the `L1CodeGen` host object, so that `host.builder` does not immediately collapse to `DynType` — a targeted reinforcement that type inference makes for the sake of "compiling itself."

## 5.6 Error Stratification: Hard Errors, Fallback Routes, and Explainers

Gathering the failure surfaces scattered through the preceding sections, the frontend's error stratification is a four-tier structure, each tier with its own type, its own stage, and its own audience:

**Tier one: user type errors → `PyFrontendError`.** Defined in [pcc/frontends/python/types.py](../../pcc/frontends/python/types.py), a dataclass carrying `span`, `message`, and an optional `hint`; `format()` renders `file:line:col: error: ...` plus a hint line. Section 8 of the interface contract makes it a mandatory convention: every user-visible compile failure must be a `PyFrontendError` (or subclass); a bare `RuntimeError` must never surface from user input. It says: "your program is wrong."

**Tier two: outside the subset but semantically known → `DynType` demotion, not an error.** Inference does not raise on shapes it does not recognize; it tags them `DynType` and hands them to lowering, which emits runtime dispatch for `DynType`. This is not silent fallback — whether it is permitted is decided by the mode: under `--python-libpython=off`, if the demotion ultimately needs `py_cpy_*`, it converts at `_finalize_libpython_mode()` into **tier three: a mode hard failure → `PyPipelineError`**, with the mechanized reason list of 5.4.1. Note the elegance of the layering: when `_binop_result` returns `TYPE_DYN`, it neither knows nor needs to know the final mode; the ruling is deferred to the place that has all the information — the generated IR and the user's mode choice.

**Tier three's self-hosting variant: `ScaffoldUnsupportedError`.** Under scaffold ON, an unmigrated IRBuilder method fails with its name attached (5.4.3); the audience is not the ordinary user but the developer doing file-by-file migration.

**Tier four: route recording and explanation.** [pcc/diagnostics/fallback_routes.py](../../pcc/diagnostics/fallback_routes.py) turns the five classification strings of 5.4.2 into user-visible events: `FallbackRoute(module, classification, reason, native)`, with `route_from_classification()` assigning each classification one stable reason sentence ("no native provider found; libpython required unless disabled," etc.), and `explain_routes()` rendering text or JSON in the `pcc.diagnostics.fallback_routes.v1` schema. [pcc/diagnostics/fallback_explainer.py](../../pcc/diagnostics/fallback_explainer.py) is the more general collector: `FallbackReason(feature, phase, reason, suggestion, source)`, with `explain_import()` generating a suggestion-bearing explanation for `cpython_fallback` ("add pcc/stdlib port or enable --python-libpython=auto"). Recording the present honestly: these two modules today are a stable vocabulary and renderer with unit tests ([tests/python/test_fallback_routes.py](../../tests/python/test_fallback_routes.py), `test_fallback_explainer.py`); the pipeline's live emission channels are `_pcc_emit_import_log` (`PCC_LOG=import`) and `--explain-fallback` attaching to diagnostics via `ObservabilityOptions` in [pcc/diagnostics/compile_observability.py](../../pcc/diagnostics/compile_observability.py). Both sides share the same set of classification strings — those strings are the real contract.

The net effect of the stratification: **every failure lands at the tier that knows why it failed, and the failure itself is structured data.** Chapter 14's fallback ratchet and Chapter 18's claim-hygiene table are both built on this property.

## 5.7 History and Lessons

All three stories are taken from the live records in [docs/investigations/](../../docs/investigations), replayed in the discipline of "symptom → wrong hypothesis → evidence chain → root cause → the invariant left behind."

### 5.7.1 The `yield a, b` misparse and sentinel leakage (fixed 2026-05-27)

**Symptom.** A generator containing `yield 1, 2` compiled with zero diagnostics and crashed at runtime: `NameError: name '_yield' is not defined`. The trigger was real package surface — `yield rpath, files` in NumPy's `numpy/distutils/misc_util.py` (investigation: `python-yield-tuple-misparse-leaks-yield-sentinel.md`).

**The tempting wrong turn.** "The generator wasn't recognized." The evidence refutes it: `_funcdef_has_yield_sentinel` found the `_yield` sentinel even in its nested position, and the resume function was emitted normally — the generator machinery was working the whole time.

**Evidence chain.** Both backends (self and llvm) failed identically while CPython printed `(1, 2)` → backend-independent, a frontend problem. Reading the emitted IR: the resume function constructed a two-tuple whose element 0 was a dynamic call to the name `_yield` and whose element 1 was the literal `2`, with the string constant `"name '_yield' is not defined"` lying beside it — leakage confirmed.

**Root cause.** In CPython, `yield` is greedy with respect to the testlist: `yield a, b` ≡ `yield (a, b)`. `_parse_yield_expr` in `py_parse.py` called `_parse_expr()` exactly once, producing `_Yield(value=a)` and leaving `, b` to the enclosing testlist, yielding `(_yield(a), b)`. Generator lowering rewrites only an `ExprStmt` whose expression is **directly** `_yield(...)`; the tuple-wrapped sentinel slipped past the rewrite and survived to runtime as an ordinary name.

**Fix and invariants.** `_parse_yield_expr` now mirrors `_parse_return`'s implicit-tuple handling and consumes the entire testlist (with the terminator set that is safe in expression position); `yield from` deliberately stays single-expression per PEP 380. The fix comment writes the whole incident into the source. Two invariants remain: first, sentinel encoding defers the cost of a misparse from a compile-time ParseError to a runtime NameError — so any change to a parse path that produces sentinels must carry an end-to-end (compile-and-run) regression; this one landed in `tests/python/test_python_generator_parity.py::test_generator_yields_implicit_tuple`. Second, a change to `py_parse.py` may not be declared fixed until the full three-stage bootstrap gate has run — the investigation record for this fix archives both.

### 5.7.2 `@property` return types failing to propagate: a break in the type supply chain

**Symptom.** A multi-file closed-world compile of [pcc/stdlib/pathlib.py](../../pcc/stdlib/pathlib.py) tripped the no-libpython hard failure. The minimized shape (investigation: `pcc-py-type-infer-property-return-type.md`):

```python
@property
def suffix(self) -> str:
    n = self.name       # n is inferred DynType, not str
    i = n.rfind(".")    # dynamic dispatch via py_cpy_getattr → trips the gate
```

**The counterintuitive part.** The runtime had `py_str_rfind` all along; codegen had the corresponding lowering all along; even a direct single-file property read `c.name` **was** typed `str` (the baseline test passed). The gap was exactly one hop away: store the property result into a local and then call a method on it, and the type was already gone. Every downstream link of the supply chain was ready; what broke was one upstream link — at inference time, `ClassType` had no view of `@property` whatsoever, so attribute access fell into the generic `DynType` path.

**The design judgment inside the fix.** The investigation's fix plan flags a detail that is easy to get wrong: do not register property getters as ordinary methods — that would let `c.prop()` wrongly pass type checking. The correct move is to open an independent `properties` channel on `ClassType`. Today's code has exactly that shape: the comment on `ClassType.properties` in `py_ast.py` explains why it is separate from `fields` and the method table; `_class_properties_from_def()` in `type_infer.py` collects `@property` declarations (the getter's return annotation, falling back to `DynType` when unannotated), and `_lookup_class_property()` searches along the MRO. Setter and descriptor introspection are explicitly scoped out.

**Lesson.** Type information is a supply chain: runtime function, lowering rule, inference annotation — lose any one of the three and the entire native path is void, and the failure presentation (the libpython gate tripping) sits two subsystems away from the root cause (inference missing one lookup channel). The regression test that locks the spec is an end-to-end assertion — no `py_cpy_*` call may appear in the multi-file IR — not a unit assertion on inference, because supply-chain problems can only be held end to end.

### 5.7.3 The lifter as pcc1's first victim: a UAF and a raw-value leak

These are two investigations joined head to tail (`pcc1-self-host-parse-float-literal-uaf.md` and `pcc1-stage2-lift-expr-raw-value-leak.md`), telling one story: when the compiler compiles itself, the frontend files become the most sensitive integration test there is.

**Act one: heap corruption.** pcc1 crashed deterministically while compiling [pcc/__main__.py](../../pcc/__main__.py), with macOS's nano allocator reporting heap corruption. A probe in `py_decref` caught the first release of a dangling pointer: `tag=2043`, not any legal `PY_TYPE_*` value — the object's memory had been freed and reused, and the dump showed a freshly created string object sitting 48 bytes past the original address. The backtrace landed in the scope-exit cleanup of `_parse_float_literal_lift`; disassembly confirmed that the else path of `if e_idx >= 0` stored the parameter `text` aliased into the `mantissa` slot **without an incref**, so the function's exit-time `pcc_gc_release(mantissa)` released once too many. The most methodologically valuable part is the failure: the exact same aliasing pattern, built into a minimal reproducer, ran clean for 100,000 iterations — the bug needed full pcc1's heap-allocation sequence to manifest. The investigation honestly records "the aliasing hypothesis is not sufficient on its own" and lists three hypotheses still on the table, rather than declaring the case closed. The countermeasure is still in the frontend source today: the `WORKAROUND` comment in `_parse_float_literal_lift` — all paths materialize a new owned string via slicing, routing around codegen's ownership blind spot (the ownership contract itself is Chapter 9).

**Act two: the crash becomes a smaller lie.** After the UAF fix (commit `18f60d6a`), stage2's next wall was a clean backtrace: the fallback in `lift_expr`, `raise LiftError(f"no expr lifter for {t.__name__}")`, itself raised an `AttributeError` — because `t` had no `__name__` at all. Diagnostic instrumentation expanded the fallback into `isinstance` tests against all 25 expression classes plus raw-value probing, and the conclusion was unambiguous: what landed in `e` was a raw Python value — a single-quote string, a left-brace string, a bare tuple — **not any parse node**. All `isinstance` tests false ruled out the "missed dispatch" hypothesis; had it been a double-import class-identity problem, `isinstance` would still have matched, ruling that out too. Host CPython running the same lifter never triggered it — the bug lived in the pcc1-compiled parser/runtime, leaking raw values into child slots that should have held nodes, with the leak site drifting with heap layout.

**The invariants left behind.** The explicit loops plus context-bearing re-raise (statement index, node type name, line number, file name) in `lift_module` and `_lift_stmt_list` are the direct legacy of these two acts: the next time a pcc1 defect picks the lifter as its first victim, the failure must arrive carrying its own coordinates. The broader lesson went into the bootstrap regression discipline ([AGENTS.md](../../AGENTS.md)): separate stacked failures — the UAF and the raw-value leak are two bugs with two evidence chains, and merging them into one "the bootstrap is broken" story fixes neither; and a minimal reproducer failing to trigger does not refute a hypothesis — heap-layout-dependent bugs make exactly that their normal state (debugging playbook §1, §5; see Chapter 18).

## 5.8 Summary

The typed-Python frontend is a supply line chained through four files: hand-written lexing and recursive descent produce a narrow AST; the lifter translates it into the frozen `py_ast` (syntactic sugar uniformly sentinel-encoded, every expression starting at `DynType`); the pipeline collects the closure and adjudicates import classification and the two big mode flags; and type inference, annotation-sourced with `DynType` as its honest floor, marks out "the provable parts" for Chapter 6's native lowering. Three design rulings run through everything: a typed subset rather than a JIT, because the thesis is ownable execution plus the bootstrap fixed point, and the subset's sufficiency is falsifiably tested every day by "the compiler must compile itself"; unsupported idioms fail loudly by default, because only fallback as a countable, explicit event can support the ratchet and mode-labeled claims; and the three-state `--ir-scaffold` (on = closed-world with gaps reported by name, off = an explicit escape hatch, auto = a legacy spelling normalized to on) elevates "how the compiler's own IR-construction layer gets compiled" from implicit behavior into a controlled contract. And the three stories of 5.7 converge on the chapter's deepest structural fact: these frontend files are simultaneously the compiler's implementation and the compiler's input, and their defensive shapes, their error stratification, even the way they evaluate literals, are the self-hosting constraint projected into source code.

## Exercises

1. **Read the source and verify.** When `pcc hello.py` is run with no flags, what are the actual values of the two key modes? Starting from `_resolve_libpython_mode()` and `_resolve_ir_scaffold_mode()` in [pcc/frontends/python/pipeline.py](../../pcc/frontends/python/pipeline.py), explain the code paths by which the empty values normalize to `off` and `on` respectively, and check against the [README.md](../../README.md) status table that documentation and code agree.
2. **Read the source and verify.** List every sentinel name the `_Lifter` in [pcc/frontends/python/py_lift.py](../../pcc/frontends/python/py_lift.py) can construct (start from `_e_Comp`, `_e_Yield`, `_e_Await`, `_e_Starred`, `_e_Assign`, `_e_Set`). Pick one, find the lowering code under [pcc/frontends/python/codegen/](../../pcc/frontends/python/codegen) that recognizes and rewrites it, and write out the concrete form that the anti-leakage invariant of 5.3.2 takes in that case.
3. **Argue the layering.** `_binop_result()` returns `TYPE_DYN` for operand combinations it does not recognize instead of raising — does this contradict "fail loudly by default"? Describe one complete path by which a `TYPE_DYN` escalates into a `PyPipelineError` hard failure (hint: the two fallback detections of 5.4.1), and argue why `_finalize_libpython_mode` — not `_binop_result` — is the correct layer for the ruling.
4. **Design tradeoff.** `_type_from_isinstance_arg()` deliberately does not narrow the tuple form `isinstance(x, (A, B))` because the frontend has no union type. Design a minimal union-type extension for `py_ast`: what nodes must the frozen contract add? What must `_narrow_scope_for_isinstance`, `_is_assignable`, and Chapter 6's lowering each take on? Finally, argue whether pcc's own bootstrap closure actually needs it — support your conclusion by using `rg` over [pcc/](../../pcc) to count the real density of tuple-form `isinstance` occurrences.
5. **Predict, then verify.** Without looking at the code, predict where each of these four imports falls in the five-way classification of 5.4.2: `import typing`, `from . import sibling`, `import pcc.unsafe`, `import numpy`. Then read `_classify_python_import()`, `_SCAFFOLD_IMPORT_MODULES`, and `_COMPILE_TIME_ONLY_IMPORT_MODULES` to verify, and compile a small file with `PCC_LOG=import` to check the JSON log (`pcc.import_log.v1`) against your predictions.
