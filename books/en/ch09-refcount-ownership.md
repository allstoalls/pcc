# Chapter 9: Reference Counting and Ownership

When an interpreter executes Python, reference counting is administered by a central evaluation loop: every bytecode knows what it took and what it gave back. Once pcc compiles Python to native code, that center disappears — every incref, every decref, every slot overwrite must be emitted as IR by the frontend, at compile time. The emission cannot be driven by runtime observation; it can only be driven by a contract that is decidable at compile time: which expressions produce an owned reference, which names are merely borrowed, and in which direction a reference crosses a function boundary. This chapter covers the three layers that implement that contract — the runtime primitives (`py_incref`/`py_decref`/`pcc_gc_store_ptr`), the frontend's owned-local lowering, and the callee-retain rule on the function return path — and closes with two real bootstrap regressions. The GC algorithms themselves — cycle collection, tracing, generations, relocation — belong to Chapters 10 and 11. This chapter is about refcount semantics and the ownership contract, and about why neither may be weakened to make a gate pass.

## Chapter Overview: First Ask Who Owns the Reference

You do not need to memorize `incref` and `decref` first. Treat every value as a responsibility: whoever receives an owned reference must release it at the right point; a borrowed reference is only a temporary view and cannot be treated as owned.

- Function-call results are normally handled as owned unless the helper explicitly says otherwise.
- Locals, parameters, module globals, fields, and singletons are common borrowed sources; returning them requires the callee to retain first.
- Error paths matter as much as normal paths: they must neither leak owned values nor release them too early.

## 9.1 The Problem and the Design Space

State the problem precisely first. In pcc-generated native code, a `PyObject*` flows from function A to function B and is then stored into list C. Who is responsible for releasing it after its last use? Three families of answers have appeared in the design space:

**Alternative one: no reference counting; leave everything to a tracing GC.** The Go-style answer: generated code only allocates and runs write barriers; reclamation is entirely the tracer's job. pcc rejects this for two reasons. First, the five-GC equality contract (see Chapter 10) requires that the *same* generated code run correctly under all of `PCC_GC_BACKEND=0..4`, and backend #0 — refcount plus cycle collection, the default and the rollback reference — is founded on precise reference counts. Generated code must emit the counting operations; that is a non-negotiable floor. Second, one of pcc's stated obligations is long-running efficiency (RSS, pause behavior, fragmentation over time), and reference counting provides deterministic, immediate reclamation and deterministic finalizer timing. In a comparative runtime laboratory that is a valuable reference frame, not legacy baggage.

**Alternative two: a caller-borrows convention.** Let function calls return borrowed references, and make callers copy when they need to keep the result. This direction was explicitly rejected during the bootstrap-regression investigation of 2026-06-01 (see 9.7): it would leak the return paths that naturally produce fresh references — constructors, container literals, owned locals — and it moves the "does this result need releasing?" decision from the party with information (the callee knows what it is returning) to the party without it (the caller sees only a pointer). Decades of CPython C-API practice sit on the other side as well: function results are new references.

**Alternative three: purely static ownership (the Rust route).** Prove every reference's lifetime at compile time; run zero counting at runtime. Python's semantics do not cooperate: variables rebind on arbitrary control-flow branches, exceptions can be raised from nearly every call site (see Chapter 8), and at a control-flow join the same name may be owned along one edge and borrowed or unbound along another. A purely static proof would require changing the language, and pcc's north star forbids weakening Python semantics.

pcc's answer is a hybrid: **static classification + a runtime flag + a directional boundary contract**. The static part is the expression classifier in [pcc/frontends/python/codegen/ownership_lowering.py](../../pcc/frontends/python/codegen/ownership_lowering.py); the runtime part is one `i1` flag per owned local; the boundary contract is a single sentence, worth restating from the repository rules ([AGENTS.md](../../AGENTS.md), Bootstrap regression discipline, item 5):

> Function calls return owned references; returning a borrowed local, parameter, module global, field, or singleton must retain in the **callee**, rather than making the caller stop releasing owned results.

That sentence is the spine of this chapter. Its two directional choices — "a returned value is owned" and "fix the callee, not the caller" — each get a section (9.5). Before descending into the layers, here is the whole contract in one picture; the rest of the chapter fills in the blocks:

```text
Caller side
  r = f(x)                       receives an owned reference;
                                 free to store, must release when done
Callee side (return_lowering.py)
  return <owned expression>      naturally owned; hand it over as-is
  return <owned local>           ownership transfer: no retain,
                                 skipped during cleanup
  return <param/global/borrow>   pcc_gc_retain first, then hand over
Slot side (py_obj.c)
  obj.f = v / lst[i] = v         pcc_gc_store_ptr, balanced:
                                 incref new -> overwrite -> decref old
                                 (does not consume the caller's ref)
```

Each block has an owner: the slot store is a runtime primitive (9.3), the classification of owned expressions and owned locals is frontend lowering (9.4), and the three-way return split is the boundary rule (9.5). Each block is simple in isolation; nearly every ownership bug lives at a seam between two of them — one side assuming an obligation the other side never discharged.

## 9.2 Runtime Primitives: py_incref, py_decref, and the Death of an Object

The reference count lives in the object header. `PyObjectHeader`, defined in [pcc/runtime/include/py_runtime.h](../../pcc/runtime/include/py_runtime.h), prefixes every heap object: `int64_t refcount` (offset 0), `int32_t type_tag` (offset 8), `int32_t flags`. Objects are born owned: `pcc_gc_alloc()` (in [pcc/runtime/src/py_obj.c](../../pcc/runtime/src/py_obj.c)) writes `h->refcount = 1` after allocation; the allocator is the first owner.

`py_incref()` and `py_decref()`, both in `py_obj.c`, open with fast paths that reveal three kinds of "objects" that do not participate in counting:

1. **NULL**: both functions return immediately, which means generated code need not null-check before every release.
2. **Tagged small ints**: values matching `PY_IS_TAGGED_INT` are the value projection of `int` (see Chapter 16); they have no object header and hence no count.
3. **Immortal singletons**: `py_None`/`py_True`/`py_False` are statically defined in [pcc/runtime/src/py_substrate.c](../../pcc/runtime/src/py_substrate.c) with `.flags = PY_FLAG_IMMORTAL`, and `py_incref`/`py_decref` return as soon as they see `PY_FLAG_IMMORTAL`. Generated code may therefore retain and release singletons uniformly with no effect — the uniformity of the contract is worth more than a few saved instructions.

When `py_decref()` drives a count to zero, a fixed death sequence runs: `pcc_refcount_forget()` retires the count, `py_weakref_invalidate(o)` invalidates weak references, `pcc_gc_note_object_freeing(o)` notifies the active GC backend, and `py_gc_untrack(o)` removes the object from the cycle collector's roster — only then does type-dispatched deallocation begin. The order is not arbitrary: weak references must be dead before any destructor side effect (including a user `__del__`) runs, or a finalizer could observe a half-dead object through a weakref.

Both functions also carry a built-in debugging perimeter. At its entry, `py_decref` calls `pcc_debug_maybe_abort_bad_decref()`: enabled only when the `PCC_DEBUG_RUNTIME` environment variable is set, it validates pointer shape (`py_pointer_can_have_header()` — non-NULL, not a tagged int, address at least 0x1000, 8-byte aligned, top 16 bits zero) and `type_tag` legality (`py_type_tag_is_valid()`), printing the pointer and tag and calling `abort()` on any violation; there is additionally a refcount-underflow assertion. In production builds the same checks let a bad pointer pass **silently** (a defensive return, with no counting) — a deliberate asymmetry. The first visible site of an ownership bug is almost always one decref too many; production mode chooses not to let a single counting error become an immediate crash, while debug mode converts silent corruption into an immediate abort with a crime scene attached. The debugging playbook of Chapter 18 leans on this perimeter repeatedly to shorten localization chains.

Deallocation itself carries a mechanism worth explaining: the **deferred trash queue**. Imagine `list -> list -> list -> ...` nested a hundred thousand deep. Naively, `py_dealloc_list()` releasing its children calls `py_decref`, which re-enters `py_dealloc_list`, and the stack blows. `py_decref` uses a thread-local `pcc_trash_dealloc_depth` counter to detect nested deallocation: at depth greater than zero, container and user-instance types (per `pcc_trash_should_defer()`) are not destroyed immediately but appended via `pcc_trash_enqueue()`; the outermost deallocator runs `pcc_trash_drain()`, flattening the recursion into iteration. This is pcc's counterpart of CPython's "trashcan" mechanism.

Finally, the file organization itself encodes a design decision. `py_obj.c` keeps only counting and dispatch; the type-specific deallocators are split into [pcc/runtime/src/py_obj_dealloc.c](../../pcc/runtime/src/py_obj_dealloc.c). The split's header comment states the reason: the counting logic is meant to be wholesale-replaced by the pcc-Python port ([pcc/runtime/py/py_obj.py](../../pcc/runtime/py/py_obj.py)) as self-hosting advances, while the deallocators touch flexible-array-member tails and raw struct fields that the current pcc-Python surface cannot express, so they remain C. On the port side, `py_obj.py` exports the same C ABI symbols via decorators such as `@c_abi_export("pcc_gc_store_ptr")`, mirroring the C implementation step for step. This is the C↔pcc-Python mirroring discipline of Chapter 14 instantiated on reference counting: one set of object-graph rules, two implementation languages, no semantic drift allowed.

## 9.3 The Balanced Slot Store: pcc_gc_store_ptr

Storing a reference into another object's slot — a list element, an instance field, a dict entry — is the moment ownership goes wrong most easily. pcc funnels it through one function, `pcc_gc_store_ptr()` in `py_obj.c`, whose core is four lines:

```c
PyObject *old = *slot;
py_incref(value);
*slot = value;
py_decref(old);
```

Three design points.

**First, ordering.** Incref the new value before decrefing the old. Reversed, a self-store (`x.f = x.f`, or new and old reaching the same object via different paths) lets the old value's decref drop the count to zero and destroy the object, after which the incref touches a dangling pointer. Incref-first makes that path safe by construction; callers never need an identity check.

**Second, balance.** The function is **neutral** with respect to the reference in the caller's hand: the slot increfs the new value for itself and decrefs the old value for itself; it does not consume the caller's reference. Corollary: a caller that passes in an owned temporary (say, a freshly built string) must still release its own reference after the store. Storing into a container *copies* ownership; it does not *transfer* it. Reading this function's source rather than inferring the contract from a memory of CPython's `PyList_SET_ITEM` (which steals references) is itself a repository lesson — pcc's slot store points the other way from CPython's macro.

**Third, the barrier attachment point.** Before the four core lines, the function notifies the active backend according to `pcc_gc_backend()`: `pcc_gc_note_store()` accounting, the relocation-read resolution for backends #3/#4, and `pcc_gc_note_slot_write_barrier()`. For this chapter only one fact matters: every GC-side store-time obligation rides on this same function, which is why a plain slot write (`obj->slot = x`) happens to work on backend #0 and reliably breaks on #3/#4. The barrier semantics themselves are Chapters 10 and 11.

Global root slots have the analogous `pcc_gc_store_root()`, performing the same balanced write under the root-slot lock. Both functions have line-for-line mirrors in the `py_obj.py` port.

## 9.4 The Frontend: Who Owns This Reference?

The runtime primitives are merely the abacus; the frontend does the arithmetic. `OwnershipLoweringMixin` in [pcc/frontends/python/codegen/ownership_lowering.py](../../pcc/frontends/python/codegen/ownership_lowering.py) answers the compile-time question at the heart of the system: **does the current function own the result of evaluating this expression?**

The decision procedure is `_expr_returns_owned_object()`. Its taxonomy is worth enumerating, because every entry corresponds to a runtime fact:

- **Calls**: user-function calls are judged by the callee's declared return type (`_return_type_is_owned_object()` — `None`/`bool`/`int`/`float` are unboxed scalars, not objects); class construction (a `ClassType` or a registered class name) is always owned; the builtin constructors `list`/`dict`/`set`/`tuple`/`str`/`bytes`/`frozenset` are always owned.
- **Literals and subscripts**: `ListExpr`/`DictExpr`/`TupleExpr`/`StrLit`/`BytesLit`/`Subscript` are always owned — the runtime getitem APIs return new references.
- **Binary operations**: a `BinOp` whose result type is `str`/`list`/`tuple`/dyn (concatenation, repetition) produces a fresh object.
- **Attribute loads**: `_attr_expr_returns_owned_object()` returns true only for known fields with declared object types — field reads go through accessors that produce new references.
- **The counterexamples**: the raw-pointer intrinsics of [pcc/unsafe/](../../pcc/unsafe) (the `_UNSAFE_RAW_POINTER_RETURNS` set: `malloc`, `cstr`, `global_addr`, and friends) do not return `PyObject*` at all and never participate in counting.

Static classification is not enough; a runtime flag is also needed. The reason is control flow: after `if c: s = build()`, at the join point `s` owns a fresh object along one edge and is unbound along the other. pcc gives each owned local an `i1` alloca in the entry block (`_ensure_owned_local_flag()`, IR name of the form `name.owned`), set to 1 when an assignment stores an owned value and cleared after release. The release site (`_emit_release_owned_local_if_flagged()`) emits a conditional branch: only if the flag is true is the current value loaded through `pcc_gc_load_ptr` and passed to `pcc_gc_release`. The static set (`_owned_local_names`) decides **which names** are managed; the runtime flag decides **whether one is actually held right now**.

Around this pair sits a family of lifecycle rules:

- **Rebinding**: the assignment path in [pcc/frontends/python/codegen/assignment_statement_lowering.py](../../pcc/frontends/python/codegen/assignment_statement_lowering.py) first runs `_emit_release_owned_local_if_flagged()` on the old value, then stores the new one, then re-sets the flag. Omit the first step and every loop iteration leaks one object.
- **Scope exit**: `_emit_owned_local_cleanup()` releases every flagged owned local before each `return`, then leaves GC frame roots in **reverse** registration order (`_gc_rooted_local_order`). It accepts a `skip_name` parameter — Section 9.5 explains why.
- **GC root registration**: each owned local is simultaneously registered as a frame root via `_ensure_owned_local_gc_root()` (`pcc_gc_frame_enter`/`pcc_gc_frame_leave`) so that tracing backends can see stack-held references. The root machinery belongs to Chapter 10; this chapter only notes that ownership and root registration are coupled in the same lowering — a coupling that bites in the second case study of 9.7.
- **Expression temporaries**: ownership is not confined to named locals. Evaluating `a.b.c` produces intermediate receiver temporaries; binary operations produce operand temporaries; each owned intermediate should be released as soon as it is consumed. The uniform entry point is `_gc_release_if_owned()`: it double-checks via `_raw_scaffold_object_rhs_is_owned()` and `_expr_returns_owned_object()` that the source expression really produced an owned reference, excludes CPython-bridge tagged values (`_cpy_values`), and only then emits the release. Its call sites are scattered across a dozen lowering locations — `attr_load_lowering.py`, `expr_dispatch_lowering.py`, `exact_int_lowering.py`, `assignment_statement_lowering.py`, and more. Every release carries a context label (`_release_expr_label()` encodes the function name, expression type, and source span), which `pcc_debug_check_release` uses in debug builds to answer "which release at which line drove the count through the floor".
- **Discard assignments**: `_ = expr` is handled specially by `_maybe_emit_discard_assignment()` — evaluate, immediately release the owned result, and remove `_` from the env and the hint tables, leaving no binding that could dangle.
- **Honest conservatism**: tuple-unpack targets are judged by `_unpack_target_value_is_owned()`, and `DynType` unpack results are **not** managed as owned objects — dynamic unpack sites can carry pointer-shaped native values (dataclass/AST string fields inside the bootstrap compiler), and releasing one as an object is a segfault. Likewise, raw-int-scaffold modules with C-ABI exports (the runtime ports themselves) are treated as managing references by hand; the frontend tracks only object-producing expressions it can identify with high confidence (`_raw_scaffold_object_rhs_is_owned()`). The classifier prefers under-management (a leak, which is measurable) to over-management (a double free, which is a crash).

## 9.5 The Return Path: the Callee Retains

Now the boundary clause of the contract. The first half — **function calls return owned references** — fixes the caller's behavior: it may store the result and release it when done, with no knowledge of the callee's internals. Once that half is fixed, the entire obligation lands on the callee: **whatever the return expression is, the value that crosses the function boundary must be owned.**

Most returns satisfy this naturally: constructors, literals, owned locals — already owned. The trouble is borrows:

```python
def common_type(a, b):
    if ...:
        return a        # parameter — borrowed
    return TYPE_DYN     # module global — borrowed
```

A parameter is lent to the callee by the caller; a module global belongs to the module. Return either directly and the caller, per contract, treats the result as owned and eventually releases it — releasing a reference it never owned. The count underflows; something double-frees.

The fix lives in [pcc/frontends/python/codegen/return_lowering.py](../../pcc/frontends/python/codegen/return_lowering.py). `_return_value_needs_retain()` decides whether the returned value is borrowed in the callee: a return expression that is a `Name` hitting `_current_param_names` (a parameter), `_module_globals` (a module global), or an env-bound local not in the owned set is borrowed; anything for which `_expr_returns_owned_object()` holds is not. When the verdict is "borrowed", `_retain_borrowed_return_value()` emits one `pcc_gc_retain` (IR name `ret.retain`), promoting the borrow to ownership before handing it out. On the runtime side `pcc_gc_retain()` is `py_incref` plus returning the same pointer — and singletons and tagged ints pass through it as harmless no-ops, which is exactly the payoff of the uniform contract in 9.2: [AGENTS.md](../../AGENTS.md) item 5 lists "field or singleton" among the borrows, and generated code needs no special case for them.

Returning an owned local takes the symmetric path: **ownership transfer**. `_emit_return()` passes the returned name as `skip_name` to `_emit_owned_local_cleanup()` — cleanup releases every other owned local but skips the one being returned; it is neither retained nor released, and the reference moves to the caller as-is. One transfer, net count change zero.

`_emit_return()` arranges these steps in a fixed order: evaluate the return expression → type coercion → **retain (borrow promotion)** → `_emit_pending_finally_blocks()` (emit the pending `finally` bodies, innermost outward) → **owned-local cleanup (with `skip_name`)** → `ret`. Two of the orderings are quiet correctness decisions. Retain happens before the finally blocks: a `finally` body may rebind or even release locals, but a return value already promoted to owned no longer depends on any local binding and is unaffected. Cleanup happens after the finally blocks: a `finally` body may still read or write owned locals, and releasing them early would be a use-after-free.

One more detail on the return path: after the retain and before cleanup, `_enter_return_cleanup_root()` parks the return value in a temporary GC root (`ret.tmp.root`), and `_leave_return_cleanup_root()` reads it back once cleanup finishes. Cleanup itself calls runtime functions, and on a relocating backend the object may move in the meantime; the read-back goes through `pcc_gc_load_ptr` and observes the post-move address. Details in Chapter 11.

Finally, back to the second half of the contract: **why fix the callee and not the caller?** When this goes wrong there appear to be two symmetric repairs: add a retain in the callee, or stop the release in the caller. The repository rules forbid the latter by name, and the reason is the location of information. The callee, while compiling its own return statement, knows exactly "this is my parameter" or "this is a module global". The caller sees only a call result and must treat all calls alike. Making the caller discriminate between callees would require whole-program analysis or degenerate into per-function special-casing — the ownership analogue of the `if package == "numpy"` special case that pcc bans by name on the packaging side. Worse, "caller stops releasing" also applies to genuinely owned returns (constructors, literals), converting the double free into a leak. The asymmetry of the contract is deliberate: whoever has the information bears the obligation.

## 9.6 Finalizers and Resurrection: PY_FLAG_FINALIZED

A reference count reaching zero triggers deallocation, and deallocating a user instance may run `__del__` — arbitrary Python code, which can store `self` anywhere it likes. This is the darkest corner of refcount semantics: **resurrection**.

pcc handles it through the cooperation of two functions. The sequence in `py_instance_dealloc()` ([pcc/runtime/src/py_class.c](../../pcc/runtime/src/py_class.c)): first `py_weakref_invalidate()`, then `py_user_del_dispatch(o)` to run the finalizer, then the check —

```c
if (py_header(o)->refcount > 0) {
    py_gc_track(o);
    return;
}
```

If the finalizer stored `self` into some live structure, the balanced write of `pcc_gc_store_ptr` has already pushed the count back above zero. Seeing a nonzero count, the deallocator abandons the free, re-registers the object with the cycle collector, and returns normally. The object is legitimately resurrected.

The guard sits in `py_user_del_dispatch()` ([pcc/runtime/src/py_dunder.c](../../pcc/runtime/src/py_dunder.c)):

```c
if ((h->flags & PY_FLAG_FINALIZED) != 0) {
    return;                          /* already finalized: skip */
}
...
h->flags |= PY_FLAG_FINALIZED;       /* set the flag first */
meth(o);                             /* then call __del__ */
```

`PY_FLAG_FINALIZED` ([pcc/runtime/src/py_internal.h](../../pcc/runtime/src/py_internal.h), bit 0x4) guarantees that `__del__` runs at most once — matching CPython's PEP 442 semantics. The flag is set **before** the call: if something inside `__del__` pushes the count across the life/death line again and re-enters deallocation, the re-entrant path checks the flag and returns, so finalizers never nest inside themselves. When a resurrected object dies a second time, the same flag routes it straight to the free path. While looking up `__del__`, the function also caches the result in the `del_method` slot of `PyClassObject` (offset 96 in the class object's 120-byte layout; see Chapter 7), sparing subsequent instance deallocations the method resolution.

One honestly recorded incompleteness: after the finalizer returns, `py_user_del_dispatch()` calls `py_clear_exception()`, and the source comment admits this is a placeholder for CPython's "unraisable exception" channel — the exception is swallowed so that stale TLS exception state cannot poison the caller (see Chapter 8), but the warning-reporting channel is a later diagnostics task. That is an open problem, not a design.

Death inside a cycle (the object sits in a reference loop; its count never reaches zero) is the collector's job: backend #0's `py_gc_maybe_finalize_unreachable()` ([pcc/runtime/src/py_obj_gc.c](../../pcc/runtime/src/py_obj_gc.c)) and the tracing backends' pre-sweep finalizer pass both call the same `py_user_del_dispatch()`, and the same flag enforces at-most-once across all paths. Notably, the flag carries a second role on the collector side: `py_gc_maybe_finalize_unreachable()` compares the `PY_FLAG_FINALIZED` bit before and after each dispatch to learn whether this pass **actually ran** any new finalizer — and if so, the collector must recompute reachability, because an arbitrary `__del__` may have rewritten the object graph. How the multi-pass sweep interleaves with finalizers is Chapter 10's subject.

## 9.7 History and Lessons

### Case study one: a borrowed return breaks the bootstrap (2026-06-01)

(Source: [docs/investigations/bootstrap-user-function-low-ir-fallback-2026-06-01.md](../../docs/investigations/bootstrap-user-function-low-ir-fallback-2026-06-01.md))

The long-green three-stage bootstrap gate (`--backend self --python-libpython=off`) suddenly failed twice over. The first boundary was a strict-mode libpython fallback, unrelated to ownership; once fixed, it exposed the second: the pcc1 produced by pcc0 crashed while compiling [pcc/driver/cli_bootstrap.py](../../pcc/driver/cli_bootstrap.py) into pcc2. The LLDB backtrace landed on a double free in generated code, `user_pcc_frontends_python_type_infer__infer_expr` via `pcc_gc_release` → `py_decref` → `pcc_gc_free_object_memory`, right next to a `replace(expr, ..., ty=ty)` call.

The wrong hypotheses lined up first. Tuple auto-GC-tracking was suspected — and the proposal to disable it was **denied by the user**, because that trades weakened runtime semantics for a green gate, precisely the direction the repository rules prohibit. The dataclass field copy inside `replace(...)` was suspected — and denied by source inspection: field stores go through `pcc_gc_store_ptr`, properly balanced; `replace` was merely where the under-owned object *became visible*, not where ownership was lost. The proposal to treat user-function call results as borrowed in callers was also denied — for the directional argument of Section 9.5.

The real evidence chain: the generated IR of the callee `common_type(...)` returned `%a` and `%b` (parameters) and `TYPE_STR` and `TYPE_DYN` (module globals) directly, with no retain anywhere; callers, per contract, released the results as owned. The root cause was that return lowering had never implemented the callee's side of the contract. The fix is today's `_return_value_needs_retain()` / `_retain_borrowed_return_value()` (Section 9.5); the minimized regression [tests/python/test_return_ownership.py](../../tests/python/test_return_ownership.py) pins down "returning a borrowed parameter must emit `pcc_gc_retain` in the callee", and the single-backend full bootstrap gate passed after the fix.

The invariants left behind are two-layered. Mechanism: the callee-retain rule entered `return_lowering.py` with a dedicated regression test. Process: the investigation was written into the [AGENTS.md](../../AGENTS.md) bootstrap regression discipline — for ownership failures, verify the caller/callee reference contract before touching cleanup code, and never "fix" the problem by making the caller stop releasing.

### Case study two: the owned flag cache that leaked across functions

(Source: [docs/investigations/python-generator-owned-flag-cache-leaks-across-sibling-generators.md](../../docs/investigations/python-generator-owned-flag-cache-leaks-across-sibling-generators.md))

The symptom looked far from ownership: the self backend, compiling the real numpy package, reliably refused after the 149th IR module — `BackendUnavailable: self backend expected pointer value 'pruned_directories.owned.33'`, inside some generator resume function. Earlier rounds of investigation had carried this as "the known self-backend generator emission failure" for many entries without ever minimizing it.

Minimized, the truth was small. Two sibling generator functions in the same compilation share a local name (two functions in `numpy.distutils.misc_util` both have `pruned_directories`). `_ensure_owned_local_flag()` is a per-name cache: if the name already has a flag alloca, return the cached one. In `user_function_lowering.py`, `_emit_user_function` resets `_owned_local_flag_slots` and `_gc_rooted_local_names` to fresh empties on the **normal-function** path before emitting the body — but the **generator** branch called the wrapper emitter and returned early, skipping the reset. So generator B's resume function asked the cache for the flag of `pruned_directories` and got back the alloca from generator A's body — an SSA value that simply does not exist in B. The self backend's `materialize_pointer` could not find the value and **correctly** rejected the invalid IR.

The fix was two lines: mirror, in the generator branch, the cache reset the normal path already performed. After it, the minimized reproducer matched CPython's output, all 149 numpy modules emitted (immediately hitting a different, independent link-stage bug), and the mandatory bootstrap gate passed.

Three lessons. First, the lifetime of per-function compiler state must align exactly with function emission; every early-`return` branch is a leak point, and a `finally` that restores a *saved reference* cannot save a dict that was *mutated in place*. Second, a backend error is a locator, not the disease: the symptom was in the self backend's pointer materialization, the root cause in the frontend's ownership-lowering cache discipline. Third, a long-carried known-unknown is worth the one-time cost of minimization — this "known failure" had capped the numpy track for many iterations, and it ended as a two-line fix.

## 9.8 Summary

pcc's reference counting and ownership form a three-layer contract. The runtime layer provides the primitives: objects are born with count 1; `py_incref`/`py_decref` are uniformly immune to NULL, tagged small ints, and immortal singletons; the death sequence is fixed (weakref invalidation → GC notification → untracking → deferred deallocation); and `pcc_gc_store_ptr` monopolizes slot overwrites with its incref-new-then-decref-old balanced write. The frontend layer makes the decisions: `_expr_returns_owned_object()` statically classifies owning expressions; a runtime `i1` flag resolves what control-flow joins make undecidable; rebinding releases first; scope exit cleans up uniformly; and dynamic or raw-scaffold sites get leak-over-crash conservatism. The boundary layer is one sentence: calls return owned references, borrowed returns are retained in the callee, and returning an owned local is a count-free transfer of ownership. On the finalizer side, `PY_FLAG_FINALIZED` is set before the call, guaranteeing `__del__` runs at most once, while resurrection is legitimately admitted by the deallocator's recheck of the count. Both case studies point at the same repository discipline: when ownership fails, audit the contract first, and never weaken semantics to turn a gate green — that discipline is itself part of the contract.

## Exercises

1. **Read-the-source verification**: In `pcc_gc_store_ptr()` ([pcc/runtime/src/py_obj.c](../../pcc/runtime/src/py_obj.c)), rewrite the four core lines in decref-old-then-incref-new order. Construct a Python assignment that would produce a dangling pointer under that ordering, and explain why the current ordering requires no old/new identity check from callers.

2. **Read-the-source verification**: `_expr_returns_owned_object()` ([pcc/frontends/python/codegen/ownership_lowering.py](../../pcc/frontends/python/codegen/ownership_lowering.py)) returns false for user-function calls whose return type is `IntType`. Using Chapter 16's projection model, explain why "not an object" and "borrowed" are distinct concepts, and which check inside `py_decref` would (fortuitously) absorb the mistake of treating an `int` return as an owned object.

3. **Contract tracing**: [tests/python/test_return_ownership.py](../../tests/python/test_return_ownership.py) contains `test_returning_borrowed_parameter_retains_for_owned_call_result`. Without running the test, read `return_lowering.py` and write out the sequence of functions the return path of `identity(xs)` (a direct `return xs` where `xs` is a parameter) passes through, and the name of the retain call that must appear in the generated IR.

4. **Design-tradeoff argument**: Section 9.5 argues for "fix the callee, not the caller". Construct the opposing position: under what whole-program-information assumptions could caller-side ownership decisions emit fewer retain/release pairs than callee-side ones? Why is that optimization infeasible under pcc's current per-module compilation and the five-GC equality constraint?

5. **Design-tradeoff argument**: `PY_FLAG_FINALIZED` is set before `__del__` is invoked. Consider the alternative — setting it only after the call returns successfully. What behavioral differences arise in three scenarios: the finalizer raises, the finalizer re-enters deallocation, and the finalizer resurrects the object? Which of those differences violates PEP 442 semantics?
