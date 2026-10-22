# Chapter 8: The Exception Model

Python's semantics allow an exception to surface from almost any expression: an out-of-range subscript, a missing attribute, an exhausted iterator, a `raise` inside a user `__add__`. In an interpreter this is absorbed uniformly by the evaluation loop; once Python is compiled to native code, "how does control flow travel from the raise site, across an arbitrary number of native stack frames, to the matching `except`" becomes a design question that must be answered explicitly. pcc's answer looks like the plainest candidate on the table: `py_raise(exc)` stores the exception into a thread-local slot and **returns normally**, and after every call that may raise, generated code checks `py_err_occurred()` and branches to the error path. No unwinder, no Itanium ABI. This chapter explains the reasons behind that choice — the cost model, portability, implementability in the self backend — and its real price: propagation correctness becomes an insertion obligation scattered across dozens of lowering files, and the symptom of one missing check is not a crash but "compile succeeded with no output." The chapter covers the runtime's five C files and their five pcc-Python mirrors, the frontend's `ExceptionLoweringMixin`, and closes with two real investigations.

## Chapter Overview: Exceptions Do Not Jump Automatically

The model to keep in mind is this: raising an exception stores an error object in a thread-local slot, and generated code performs the actual branch by checking after each call that may fail. Miss one check, and an exception can turn into a silent wrong return.

- The runtime records what exception happened; lowering decides where the current function goes next.
- Cleanup paths must propagate errors and release owned references.
- Tracebacks, diagnostics, and no-libpython behavior are not decoration; they decide whether failures can be located.

## 8.1 The Problem and the Design Space

State the problem first. Function C raises a `ValueError`; the matching `except` lives three calls up, in function A. The design space holds three classic families of answers:

**Alternative one: Itanium-style zero-cost unwinding.** The C++/Rust-panic route: the compiler emits unwind tables for every function (`.eh_frame` on ELF, compact unwind on Mach-O), call sites express exception edges as `invoke` + landingpad, and at raise time libunwind drives a two-phase search (find the handler first, then clean up frame by frame), with a personality routine arbitrating each frame. "Zero-cost" means the happy path pays nothing: when no exception is raised, there are no checking instructions.

This is not a straw man — **pcc originally did exactly this**. The docstring of `_call_user()` in [pcc/frontends/python/codegen/unary_call_lowering.py](../../pcc/frontends/python/codegen/unary_call_lowering.py) preserves the migration record: "This replaces an earlier Itanium-ABI design that used `invoke` + landingpad to route exceptions via libc++abi"; the exception section comment in [pcc/runtime/src/py_internal.h](../../pcc/runtime/src/py_internal.h) likewise states "No Itanium C++ ABI symbols are exported from py_exc.c anymore." Three reasons drove the abandonment, one per axis this chapter has to answer:

1. *The cost model does not match Python's semantics.* "Zero-cost" presumes exceptions are rare. Python is not like that: every `for` loop terminates with `StopIteration`, and the generator and iterator protocols treat exceptions as ordinary control flow. Under the Itanium model, every loop exit would allocate an exception object, enter the unwinder, consult the tables, and run the two-phase search — paying the most expensive price on the hottest path. (pcc's for-loop lowering today finishes iteration via a NULL return value plus a `py_exc_matches` test against `StopIteration`, see 8.5 — iterator termination really is a hot path of the exception machinery.) Worse still is the ownership contract of Chapter 9: every stack frame holding owned locals must run refcount cleanup as unwinding passes through it, which means nearly every frame needs landingpad cleanup code — in a refcounted language, "zero-cost" degenerates into "unwinding with a landing pad in every frame," paying on both ends.
2. *Portability.* Unwind-table formats differ by platform (the divergence between `.eh_frame` and compact unwind has bled countless compiler backends), and the model drags libc++abi/libunwind into the link closure. One of pcc's north-star obligations is a self-contained no-libpython artifact (see Chapter 14); the comment in `_call_user()` names the payoff directly: "keeps libc++abi out of the runtime link." Checked-call needs only ordinary calls, one `_Thread_local` slot, and a conditional branch — instruction-for-instruction isomorphic on macOS arm64 and Linux x86_64.
3. *Implementability in the self backend.* The self backend of Chapter 13 emits machine code without going through LLVM. To support Itanium it would have to emit byte-exact CFI/unwind metadata for every function, implement landingpad semantics, and interface with the personality protocol — a large and highly error-prone surface. Under the checked-call model, an exception edge is an ordinary conditional branch: nowhere in the entire emitter family under [pcc/backend/](../../pcc/backend) is there a path that generates unwind metadata, and the backend never needs to know that "exceptions" exist as a concept. All of the exception model's complexity stays in the frontend IR layer; the backend sees only call, icmp, br.

**Alternative two: setjmp/longjmp.** Lua's route (readable in [projects/lua-5.5.0/](../../projects/lua-5.5.0)): the protected call site does a `setjmp`, the raise site `longjmp`s straight to it. It avoids unwind tables, but `longjmp` skips every intervening frame without executing any of its code — Chapter 9's owned-local cleanup and Chapter 10's GC frame-root deregistration are all bypassed, and reference counts leak straight through. Repairing that would require snapshot-style resource bookkeeping at every try boundary, which amounts to reinventing a more fragile unwinder. Lua can afford the model because Lua's C stack carries no per-frame counting obligations; pcc's does.

**Alternative three: checked-call (return-code style).** The file header of [pcc/runtime/src/py_exc_tls.c](../../pcc/runtime/src/py_exc_tls.c) states the policy in two lines: `py_raise(exc)` only stores `exc` into a thread-local slot and returns normally; after every call that may raise, the caller checks `py_err_occurred()` and, if true, branches to the error-propagation path. The comment labels its own ancestry: "return-code style, CPython-inspired" — CPython's C-API is exactly this model (functions return NULL/-1; `PyErr_Occurred()` queries thread state).

The checked-call ledger is honest:

```text
                       Itanium unwinding         checked-call
happy-path cost        zero instructions         after every raising call:
                                                 one TLS read + one compare
                                                 + one branch
raise cost             allocation + two-phase    one TLS write + an ordinary
                       table-driven unwind       return
refcount cleanup       a landingpad per frame    error path shares the cleanup
                                                 code of the normal path (Ch. 9)
link dependencies      libunwind/libc++abi       none
self-backend burden    CFI/unwind tables/        none (exception edge =
                       personality               ordinary branch)
debuggability          unwinder-internal state   a pending exception is
                                                 inspectable memory (lldb
                                                 breakpoint on py_raise)
correctness risk       one wrong table byte =    one missing check = silent
                       undefined behavior        error (8.6)
```

The last row is this chapter's undercurrent. Checked-call converts propagation correctness from "one centralized runtime mechanism" into "an insertion obligation at every emission site in the frontend" — `_emit_post_call_err_check()` has roughly 80 call sites under [pcc/frontends/python/codegen/](../../pcc/frontends/python/codegen), spread across more than twenty lowering files (counted 2026-06). The obligation is distributed, with no central enforcer; a missing check raises no error and causes no crash, it merely lets the exception "teleport" into the wrong context. Sections 8.6 and 8.7 show that price through real incidents, along with the audits and gates the repository built to pay it.

## 8.2 The TLS Slot and py_raise: The Model's Entire Runtime State

The exception model's entire runtime state is a single word: `static _Thread_local void *g_tls_current_exc` in [pcc/runtime/src/py_substrate.c](../../pcc/runtime/src/py_substrate.c), accessed through the two bare accessors `py_tls_exc_get()`/`py_tls_exc_set()`. The slot lives in the substrate rather than in `py_exc_tls.c` itself because the mirroring discipline demands it (see Chapter 14): the pcc-Python port [pcc/runtime/py/py_exc_tls.py](../../pcc/runtime/py/py_exc_tls.py) is meant to replace `py_exc_tls.o` wholesale, but it cannot declare C thread-local storage, so the storage stays in the substrate — the part that is "always C" — and the port reaches the same pair of accessors through `pcc.extern.extern`. Logic is replaceable; state does not move.

In [pcc/runtime/src/py_exc_tls.c](../../pcc/runtime/src/py_exc_tls.c), raising and querying take only a few lines of C:

```c
// pcc/runtime/src/py_exc_tls.c
void py_raise(PyObject *exc) {
    PyObject *cur = py_resolve_current_exception();
    if (cur != NULL && exc != NULL && cur != exc && py_type_of(exc) == PY_TYPE_EXC) {
        PyExceptionObject *new_exc = (PyExceptionObject *)exc;
        PyObject *existing_context = pcc_gc_load_ptr(exc, &new_exc->context);
        if (existing_context == NULL) {
            pcc_gc_store_ptr(exc, &new_exc->context, cur);
        }
    }
    if (exc != NULL && !exc_owned) py_incref(exc);
    if (cur != NULL) py_decref(cur);
    py_tls_exc_set(exc);
}

int64_t py_err_occurred(void) {
    return py_tls_exc_get() != NULL ? 1 : 0;
}
```

On the frontend codegen side, [pcc/frontends/python/codegen/exception_lowering.py](../../pcc/frontends/python/codegen/exception_lowering.py) emits the post-call error check:

```python
# pcc/frontends/python/codegen/exception_lowering.py
def _emit_post_call_err_check(self) -> None:
    cur_fn = self.current_function
    if cur_fn is not None and cur_fn.name in self._c_abi_export_symbols:
        return
    err_target = self._current_try_err_block()
    if err_target is None:
        err_target = self._ensure_fn_err_exit()
    err_fn = self.runtime.get("py_err_occurred")
    if err_fn is None:
        err_ty = ir.FunctionType(_I64, [])
        err_fn = ir.Function(self.module, err_ty, name="py_err_occurred")
        err_fn.linkage = "external"
        self.runtime["py_err_occurred"] = err_fn
    is_err = self.builder.call(err_fn, [])
```

On top of this slot, `py_exc_tls.c` implements four public entry points, 172 lines in total:

**`py_raise(exc)`** does four things. First, normalization: the static function `py_raise_normalize()` collapses any raised object into something that can be pended — `NULL` becomes `RuntimeError("no active exception to reraise")` (a bare `raise` with no pending exception, same as CPython); a `PY_TYPE_EXC` is kept as is; **instances of user exception subclasses are kept as is** — a comment inside the function records the history: an early implementation wrapped the instance into a fresh `PY_TYPE_EXC` carrying only the message string, silently dropping every instance attribute set in `__init__` (`self.code` and the like); after the fix, the instance is preserved intact and the matching side is responsible for projection (see 8.3); anything that is neither an exception object nor a `BaseException` instance becomes `TypeError("exceptions must derive from BaseException")`. Second, a relocation read: the old slot value is resolved through `pcc_gc_note_relocation_read()`, because after backend #4 moves objects the slot may hold a stale address (see Chapter 11). Third, implicit chaining: if the TLS already holds a pending exception `cur`, and the new exception is a `PY_TYPE_EXC` whose `context` slot is empty, `pcc_gc_store_ptr()` stores `cur` as the new exception's `__context__` — the counterpart of CPython's "During handling of the above exception, another exception occurred" chain. Fourth, the ownership handover: the borrowed argument is increfed, the old value decrefed, the slot written, and the function **returns normally**. The comment on the function's last line is the contract itself: "Caller is responsible for propagation via a post-call py_err_occurred() check."

**`py_err_occurred()`** is one line: return 1 if the slot is non-empty. The return type is deliberately `int64_t` — the declaration comment in [pcc/runtime/include/py_runtime.h](../../pcc/runtime/include/py_runtime.h) explains that this lets the pcc-Python port's `def py_err_occurred() -> int` lower with the default `int` and align naturally with the C ABI, so that the runtime ABI table (`runtime_abi.py`) and the port-emitted function signature agree.

**`py_current_exception()`** returns a borrowed reference (the TLS still owns it); **`py_clear_exception()`** decrefs and clears. All four entries have line-for-line mirrors in `py_exc_tls.py`, with the instance-class test, the offsets (`context` at offset 40), and the type tags all inlined as literals — the port's comments explain why: pcc-Python module-level integer constants come out zeroed in library `.o` builds that strip the automatic main(), so constants must be written at the point of use (Chapter 14 details this build fact).

Two cross-subsystem connection points deserve naming. First, the TLS slot is a GC root: `pcc_gc_promote_tls_exception_root()` in [pcc/runtime/src/py_gc_backend.c](../../pcc/runtime/src/py_gc_backend.c) promotes the slotted object during generational collection and rewrites the slot — a pending exception may live arbitrarily long between two safepoints, and if it is not registered as a root, a tracing backend will sweep it (the root contract is Chapter 10's). Second, observability is built in: the entries `py_raise`/`py_clear_exception` and friends emit "exception"-category events through `pcc_diagnostics_runtime_log_event_code()` ([pcc/runtime/src/pcc_diagnostics_runtime_log.c](../../pcc/runtime/src/pcc_diagnostics_runtime_log.c)), switched on by the `PCC_LOG` environment variable — the first floodlight for the kind of "no output" failure in 8.6.

## 8.3 Exception Objects and Matching: One Tag, One Class Table, One MRO Walk

`PyExceptionObject`, defined in [pcc/runtime/src/py_internal.h](../../pcc/runtime/src/py_internal.h), is the exception's carrier:

```text
offset  0   PyObjectHeader   (refcount@0, type_tag@8, flags — 16 bytes, see Ch. 7)
offset 16   exc_class        the concrete class (owned reference; non-NULL in steady state)
offset 24   message          args[0] — PyStrObject* or py_None (owned)
offset 32   cause            the Y of raise X from Y (owned; NULL = no explicit cause)
offset 40   context          previous exception captured by implicit chaining (owned)
offset 48   traceback        growable array of PyFrameRecord (malloc-owned)
offset 56   n_frames / 60 cap_frames (one i32 each)              — 64 bytes total
```

A design decision: **all built-in exceptions share a single type tag, `PY_TYPE_EXC`, with identity carried by the `exc_class` field**, rather than one C type per exception class. The comment in `py_internal.h` gives the reason: the isinstance walk stays uniform, the handler tests the frontend emits only need to read `exc_class` and do a class match, and attribute access inside a handler body goes through ordinary getattr. Every pointer slot is written through `pcc_gc_store_ptr()` (the barrier discipline of Chapters 9 and 10); `py_obj_visit_slots()` in [pcc/runtime/src/py_obj_gc.c](../../pcc/runtime/src/py_obj_gc.c) visits the three cycle-prone edges of a `PY_TYPE_EXC` — `message`/`cause`/`context` — because exceptions pointing at each other through `__context__` is a real scenario (a re-raise inside a handler), and the cycle collector must be able to see those edges.

The object side lives in [pcc/runtime/src/py_exc_objects.c](../../pcc/runtime/src/py_exc_objects.c): `py_exc_alloc()` allocates, zeroes, and wires up class and message; `py_exc_new()` fetches the class by built-in tag; `py_exc_new_with_class()` constructs against a user class; `py_exc_new_with_value()` lets the `message` slot carry an arbitrary object (the `StopIteration(value)` family of uses); `py_dealloc_exc()` releases the four reference slots and `free`s the traceback array.

The class side is split into two layers. The data layer is in `py_substrate.c`: `PY_EXC_BUILTIN_NAMES` (19 names), `PY_EXC_PARENT` (the parent-tag table — e.g. both `PY_EXC_KEYERROR` and `PY_EXC_INDEXERROR` have `PY_EXC_LOOKUPERROR` as their parent, and `PY_EXC_ZERODIVISIONERROR` hangs under `PY_EXC_ARITHMETICERROR`), and the per-tag cache `py_exc_classes`. The logic layer is in [pcc/runtime/src/py_exc_table.c](../../pcc/runtime/src/py_exc_table.c): `py_exc_builtin_class(tag)` returns on cache hit, otherwise constructs the parent recursively, builds the class with `py_class_new()`, marks it `PY_FLAG_IMMORTAL`, and caches it. The data/logic split is again the mirroring discipline at work: when the port `py_exc_table.py` replaces the logic layer, it reads the very same table and cache in the substrate directly through `pcc.unsafe.global_addr` — if the table lived in the `.o` being replaced, the port would have no table to read (the file-header comment of `py_exc_table.c` says exactly this).

Matching lives in [pcc/runtime/src/py_exc_match.c](../../pcc/runtime/src/py_exc_match.c), 53 lines, executed by every `except` test — it is the exception machinery's hot path. `exc_to_class()` projects an arbitrary object to a `PyClassObject*`: a class object passes through unchanged; a `PY_TYPE_EXC` yields its `exc_class`; a `PY_TYPE_INSTANCE` or any user tag (`>= PY_TYPE_USER`) yields the instance's `cls` slot — this branch is the other half of the "keep the instance intact" fix from 8.2: a raised user exception instance is projected to its class at match time, the MRO contains the `Exception` base, and both `except MyError` and `except Exception` hit. `py_exc_matches()` walks `ecls->mro`, an array, looking for pointer equality between the two projected classes; with no MRO it degrades to an identity comparison. No allocation, no string comparison, and every slot is read through `pcc_gc_load_ptr()` (the read-side obligation of backends #3/#4, see Chapter 10).

One semantic coarsening must be recorded honestly. The frontend's name table (`_BUILTIN_EXC_TAG` in `exception_lowering.py`) maps roughly fifty Python exception names onto these 19 tags: `FileNotFoundError`, `PermissionError`, `TimeoutError` and friends all map to `PY_EXC_OSERROR`, the `UnicodeError` family maps to `PY_EXC_VALUEERROR`, and `ImportError` maps to `PY_EXC_EXCEPTION`. The raise side and the except side go through the same table, so `except FileNotFoundError` actually compiles into a match against the OSError class — **catching more broadly than CPython would**. Likewise, `_emit_exception_class_ref()` falls back to the generic `Exception` class for unresolvable exception names and for attribute forms like `except json.JSONDecodeError`; the function's own docstring says "catches strictly more than requested" — a price paid for self-host compilation coverage, accounted as an open problem, not a defect being papered over.

## 8.4 Tracebacks: Built En Route During Propagation

With no unwinder there is no ability to "scan up the stack at the moment of the raise" — the traceback must be **built incrementally during propagation**. This is an unglamorous corollary of the checked-call model: stack information is not discovered, it is recorded along the way.

The mechanism is in [pcc/runtime/src/py_exc_traceback.c](../../pcc/runtime/src/py_exc_traceback.c). A `PyFrameRecord` has three fields: `func_name` and `filename` (both borrowed pointers into frontend-emitted static rodata; the exception object never frees them — the `py_internal.h` comment states this borrowed semantics explicitly) plus `line`. `py_exc_append_frame()` appends a record into the doubling array inside `PyExceptionObject` (initial capacity 8; on `realloc` failure the frame is silently dropped — OOM on the exception-handling path is not allowed to raise again).

Who calls it? The frontend. `_emit_raise()` appends the raise-site frame to the current exception immediately after `py_raise`; when `_emit_post_call_err_check()` detects a pending exception, it first jumps into an `err.frame` block memoized by `(function, error target, call site)` (`_ensure_post_call_frame_block()`), appends the frame of the function containing the **call site**, and only then jumps to the error target. As the exception propagates outward level by level, the array gains a frame per level: the raise site enters first, the outer call sites later. `py_exc_print_unhandled()` prints in array-index order, so the innermost frame appears first — derivable straight from the source, this is the visual reverse of CPython's "most recent call last" (innermost last), even though the heading reuses CPython's exact sentence. Recorded as an open presentation difference.

The rest of `py_exc_print_unhandled()` faithfully reproduces CPython's chained output: `cause` takes precedence over `context`, recursion prints oldest first, and the connecting sentences match word for word ("The above exception was the direct cause of the following exception:" / "During handling of the above exception, another exception occurred:"); defensive branches handle NULL, tagged small ints, strings, and other non-exception objects. The pcc-Python mirror `py_exc_traceback.py` replicates the identical stderr text byte for byte using `pcc.unsafe.write` instead of variadic `fprintf`, down to the hand-rolled decimal conversion in `_write_i64()` that depends on no runtime formatting — the mirror must keep working even when the runtime itself is the thing that failed.

The trigger for printing is in the frontend: the error epilogue that `_ensure_fn_err_exit()` generates for `main` calls `py_current_exception()`, `py_exc_print_unhandled()`, and `py_clear_exception()` in turn, then `ret 1`. **The process-level semantics of an unhandled exception — print the traceback, exit nonzero — is not built-in runtime behavior; it is main's error exit being explicitly lowered to do exactly that.** This point is key testimony in 8.6.

## 8.5 Frontend Lowering: raise, try/except, and the Insertion Obligation

The runtime supplies only the operators; assembling Python's `raise`/`try` semantics out of them is the job of the `ExceptionLoweringMixin` in [pcc/frontends/python/codegen/exception_lowering.py](../../pcc/frontends/python/codegen/exception_lowering.py) (the mixin architecture is Chapter 6's).

**raise.** The shape of `_emit_raise()`: construct the exception value → optional `py_exc_set_cause()` (`raise X from Y`) → `py_raise` → append the raise-site frame → unconditional branch to the current error target. The error target is decided by a compile-time stack: `_push_try_err_block()`/`_restore_try_err_block()` maintain `_try_err_block`, which inside a try body is that try's `try.err` block and otherwise is the function error epilogue from `_ensure_fn_err_exit()`. `_build_exception_value()` contains a piece of history worth quoting: `raise MyError(a, b)` with a user exception class now goes through genuine instantiation (`class_lowering.emit_instantiate`) so the user's `__init__` runs; a comment records that the old path, `py_exc_new_with_class(cls, args[0])`, skipped `__init__`, kept only args[0] as the message, dropped attributes, and could mistake a non-message first argument for the message — the same family of bug as 8.2's normalization fix, seen from the other end. A bare `raise` calls `py_current_exception()`, with the top of the `_active_handler_excs` stack as a select-based fallback — because the handler entry has already cleared the TLS (see below).

**try/except.** The control-flow skeleton `_emit_try()` generates:

```text
       body (error target = try.err)
      /                              \
 normal exit: else → finally → done   try.err: cur = py_current_exception()
                                       ├─ except.test.0: py_exc_matches(cur, C0)?
                                       │     yes → except.body.0
                                       │     no  → except.test.1 ...
                                       └─ none match → except.propagate
                                             → enclosing try.err or err.exit
```

A tuple handler (`except (A, B):`) ors together multiple `py_exc_matches` results; an untyped `except:` is always true. The order of operations at handler entry carries the semantics: first retain the exception if needed (the handler binds a name, or its body contains a bare `raise` — decided by the recursive scan `body_has_bare_raise()`), then `py_clear_exception()` to clear the TLS, then bind the name into the `e.addr` slot. Clearing keeps subsequent calls inside the handler body from being polluted by the old exception; retaining keeps an owned reference available for the binding and for re-raise after the TLS has let go. The bound name is also registered into `_except_binding_names`, so that a later copy like `saved = e` gets GC-rooted — a missing root here once let a tracing backend sweep the exception's message, with the root cause in frontend ownership lowering rather than in the runtime; that is a Chapter 10 case study (see the gc-5backend-exception-referent-roots investigation under [docs/investigations/](../../docs/investigations)). `finally` is unwound through `_finally_stack` at three places — the normal exit, the no-handler error path, and the handler exit; its interaction with `return` (emitting pending finally blocks level by level) already appeared on Chapter 9's return path.

One semantic boundary, derived from the source and recorded as is: `py_raise`'s implicit `__context__` chaining is conditioned on "the TLS still holds a pending exception," but the lowering clears the TLS at handler entry — so an ordinary `raise NewError()` inside a handler body does not automatically carry the just-caught exception as its `__context__`; the chaining takes effect on the path where a second raise happens while an exception is still pending (mid-propagation, runtime-internal secondary raises). CPython chains inside handler bodies too. This is a known gap against CPython, recorded as an open problem.

**The insertion obligation.** The model's center of gravity is `_emit_post_call_err_check()`: after every runtime call that may raise, emit a `py_err_occurred()` read, a comparison against 0, and a conditional branch to the error target (when source location is available, routed first through the `err.frame` block of 8.4). User function calls are funneled through `_call_user()` — the call, optional GC-rooting of the return value, and then the mandatory err-check; but runtime-helper calls are scattered across the lowering files, and every emission site has to remember to insert its own check: the 2026-06 count is roughly 80 call sites across 25 files — `method_call_expression_lowering.py` with 12, `native_text_modules.py` with 7, `binary_op_lowering.py` with 7, and so on. This is the "distributed obligation" that closed 8.1.

Two deliberate asymmetries complete the model. First, the **suppression rule**: inside runtime port functions marked `@c_abi_export`, no post-call checks are emitted (`_emit_post_call_err_check()` returns early against `_c_abi_export_symbols`, populated at declaration time by `user_function_decl_lowering.py`) — the exception and traceback helpers by design run in environments where "the TLS holds a pending exception" (mid handler dispatch, mid traceback printing), and a check there would misread the ambient exception as "the helper itself raised"; runtime functions follow the cc-C convention and propagate explicitly via NULL return values. Second, **equivalent routing**: not all propagation takes the post-call-check shape; for loops, comprehensions, and other consumers of `py_obj_next` route through a "NULL return value → `py_exc_matches` against `StopIteration` → terminate or propagate" maybe_end/propagate shape, semantically equivalent — and in 8.7's audit this shape becomes the signature that separates true from false positives.

`_ensure_fn_err_exit()` supplies the last piece: per function, it lazily creates the `err.exit` epilogue, emitting a sentinel by return type — NULL for pointer returns, 0 for integer returns (`main` is special: print the unhandled exception and return 1), a plain return for void — and back-patches root deregistration for already-rooted owned locals (`_patch_fn_err_exit_gc_root_leave()`, converging with the cleanup paths of Chapters 9 and 10). Note the sentinel: **the integer error path returns 0**, a value inside the legal domain — this design, compounded with a missing check, is exactly the shape of the next section's incident.

One compile-time special case is worth a mention: `_maybe_emit_optional_missing_import_try()` recognizes the idiomatic shape "`try: import X / except ImportError: X = None`" as an optional import and aliases the name to a missing marker at compile time, emitting no runtime exception path at all — try lowering is not all runtime machinery; it also includes static recognition of idioms.

## 8.6 The Failure Mode: Why One Missing Check = "compile succeeded with no output"

Now for this chapter's second mandatory question. Put the two halves of 8.2 and 8.5 together, and the failure chain of a missing check is mechanical:

```text
1. Inside a runtime function, py_raise(exc) → TLS set, function returns its
   sentinel (NULL/0) normally
2. The emission site forgot _emit_post_call_err_check
   → generated code carries the sentinel straight ahead, as if the call
     succeeded
3. The sentinel is a legal value: NULL can be stored into a slot, 0 can feed
   arithmetic or serve as an exit code
   → no immediate crash; the exception stays pending in the TLS
4a. Some later "innocent" call site happens to have a check → the exception
    detonates in the wrong context: it skipped the try/except that should
    have matched and gets caught by an outer, semantically unrelated handler
4b. Or no check ever comes → the exception stays silent forever; functions
    return sentinel 0 level by level
    → main returns 0 normally → the process exits "successfully", no output
      file, stderr clean
```

4b is the entire content of the [AGENTS.md](../../AGENTS.md) sentence "missing the check turns into 'compile succeeded with no output'." [docs/investigations/python-self-host-no-libpython-runtime-holes.md](../../docs/investigations/python-self-host-no-libpython-runtime-holes.md) records its real appearance on the bootstrap main path: a no-libpython pcc1 compiled a two-line Python file, raised a compile-time exception internally, the exception propagated to some function's error epilogue and — per 8.5's rule — returned the integer sentinel 0, and `bootstrap_cli_sys_argv_exit()` saw exit code 0 — the process exited successfully, with no artifact and not a word of text. The investigation's verdict, verbatim: "The runtime had the exception; the CLI did not print it or turn it into a nonzero exit."

This failure mode is expensive on two counts. First, it **inverts the debugging signal**: a crash leaves a scene, an error message leaves text, but here there is nothing — only an lldb breakpoint on `py_raise` reveals that the exception ever existed (this technique has been hardened into the investigation's recommended workflow and the debugging playbook; see Chapter 18). Second, it **conflates failure categories**. The same investigation distinguishes three kinds of error that must be reported separately: a *compile error* (invalid user input; pcc should print it and exit nonzero), a *compiler execution error* (pcc1 itself raised internally while compiling — should print, exit nonzero, and be labeled an internal error), and a *target execution error* (the binary pcc produced fails at run time; belongs to the target program). A missing check collapses the entire second category into "silent success" — and the second category is precisely the one bootstrap debugging most needs to see.

The systematic response has three layers, landing in different chapters: at the runtime layer, the main error epilogue of 8.4 guarantees that an exception propagating to the top is printed and exits with 1; at the invariant layer, [AGENTS.md](../../AGENTS.md) lists "check `py_err_occurred()` after raising calls" as one of the three standing causes to rule out on Python-side failures (alongside class-layout drift and missing GC barriers; see Chapter 7's three-cause checking order); at the audit layer — see the next section.

## 8.7 History and Lessons

### Case study one: clean link, working --help, "successful" compile — the silently failing pcc1 (2026-04)

Source: [docs/investigations/python-self-host-no-libpython-runtime-holes.md](../../docs/investigations/python-self-host-no-libpython-runtime-holes.md) (status snapshot 2026-04-29; Issue 1 subsequently closed 2026-05-01, baseline in [tests/bootstrap_gate_baseline.json](../../tests/bootstrap_gate_baseline.json)).

**Symptom.** Issue 1's goal: a bootstrap binary that does not link libpython. Every formal acceptance check passed — the `--python-libpython off` build succeeded, `otool -L` showed no python, `pcc1 --help` exited 0. Then pcc1 was asked to compile `def main(): return 0`: exit code 0, no output file, stderr blank.

**The wrong assumption.** "CPython-fallback count at zero + clean link = a working compiler." The investigation's central lesson is where that inference breaks: the `py_cpy_*` count is necessary but not sufficient — it measures the surface of dependence on CPython, not the correctness of the pcc runtime carrying the compiler's own execution. Nor is "the module imports successfully under CPython" evidence that "the module's initialization executes correctly inside pcc1."

**The evidence chain.** Silent failure disables every routine tool; the only move left is lldb with a breakpoint on `py_raise`: the runtime had indeed raised an exception, and the CLI neither printed it nor turned it into a nonzero exit — the pending exception was swallowed by the integer sentinel 0, exactly the 4b path of 8.6. Once the exception was surfaced, the downstream failures materialized one by one, with full bt fingerprints preserved in the investigation: in `_emit_tuple_literal`, an `ir.Constant(_I64, i)` passed a native integer through `inttoptr` as if it were an object handle into `scaffold_Constant_obj`, and `py_instance_set_field` increfing the bogus pointer crashed on the spot — the transition from "no output" to "a stack, IR, attributable" depended entirely on first making the exception visible.

**Invariants left behind.** First, an error-propagation gate: an exception raised during compilation must produce a nonzero CLI exit, text on stderr, and no artifact left behind — "status 0 with no artifact" should be an impossible state. Second, a new layer in the test pyramid: a stage1-as-compiler smoke test (build pcc1 → verify the link → run --help → compile a minimal file and execute the artifact) became a mandatory gate, today hardened into `tests/python/gc/test_pcc_bootstrap_full_gc{0..4}.py` (see Chapter 15). Third, one line of methodology: for this failure class, the first action is lldb on `py_raise`, not guessing.

### Case study two: the emission-site err-check audit — turning a distributed obligation into an enumerable list (2026-06-11)

Source: [docs/investigations/emission-site-err-check-audit.md](../../docs/investigations/emission-site-err-check-audit.md).

**Symptom.** Three independent bugs in one evening, all of the same class: a runtime function set the TLS via `py_raise`, the frontend emission site had no `_emit_post_call_err_check`, and the exception skipped the try/except that should have matched and detonated somewhere later — native `weakref.ref`/`weakref.proxy`, weak-dict subscript stores, and the cpy argument-count unpacking check, three sites of identical shape. The probe shape was uniform: expected to print `typeerror`, actually printed `ok` plus one late traceback.

**Method.** Stop colliding with them one at a time; enumerate the surface: scan the runtime C sources for functions whose bodies contain `py_raise(` — 78 of them; then scan every `self.runtime["<fn>"]` emission point under [pcc/frontends/python/codegen/](../../pcc/frontends/python/codegen), flagging those with no err-check within the following 8 lines as suspects — 58 sites (2026-06-11). The key discipline is written into the audit file's Review rules: **the heuristic produces candidates, not verdicts**; every site must either be confirmed with a minimal red probe demonstrating the wrong catching behavior, or be verified as equivalent routing and recorded as a false positive; one family per slice, no omnibus patches.

**Verdicts (family by family).** The `py_obj_next` family, 5 sites: all false positives — the maybe_end/propagate equivalent routing sits outside the 8-line window, and the audit accordingly registered "`py_exc_matches` against StopIteration + that block-name pair" as an approved signature. The re-engine family, 3 sites: false positives — the checks do exist; multi-line argument lists merely pushed them out of the window (heuristic improvement: scan to a statement boundary rather than a fixed line count). The binary-operator family: **true positives ×2, and compound** — `py_obj_add/sub/mul` lacked both the emission-side checks and user dunder dispatch (instances fell straight into the "unsupported operand" TypeError); the fix added both the C-only `py_user_binop_dispatch()` (`py_protocol.c`, the full protocol of `__op__` → NotImplemented → reflected `__rop__` → TypeError) and the emission-side checks, later extended to same-family fixes for `%`, `//`, `/` reflection and the augmented-assignment `__iadd__` protocol. The generator `send/throw/close` family: true positive — and adding the check **exposed a runtime hole the missing check had been masking**: the `GeneratorExit` injected by `py_gen_close()` stays pending in the TLS when the generator body does not catch it, while in CPython semantics that is precisely close's normal path and must be swallowed; the fix discriminates by pointer identity with the injected object (`cur == exc`) — `GeneratorExit` has no dedicated tag (it is `PY_EXC_BASE` plus a message), so identity is the only precise criterion.

**Lessons left behind.** First, checked-call's weakness can be compensated by engineering: a distributed obligation cannot be centrally enforced, but it can be periodically enumerated and audited, and the audit signatures (the equivalent-routing shapes) must be banked for the next pass. Second, a missing check **masks the runtime holes behind it** — gen.close's `GeneratorExit` defect only became visible after the check was added; fixing one class of bug routinely lifts the lid off a second, and two failures demand two evidence chains. Third, every true-positive fix closed with "red probe → fix → two-tier (port/C) verification → regression test → the five-GC matrix" (see Chapter 10's equality contract) — exception routing is object-graph behavior, and all five backends must testify.

## 8.8 Summary

pcc's exception model is a trade made with a clear sense of direction: a constant cost of one TLS read plus a branch per call site, in exchange for shedding the entire world of unwind tables, personality routines, and libc++abi. The three things bought line up with pcc's north star: portability (the no-libpython artifact carries no C++ runtime), zero self-backend burden (an exception edge is an ordinary branch; [pcc/backend/](../../pcc/backend) contains not one line of unwinding code), and debuggability (a pending exception is a piece of memory you can breakpoint and print). It also lines up with Python's semantic temperament: exceptions as frequent as `StopIteration` cannot afford two-phase unwinding, and Chapter 9's per-frame refcount cleanup already makes "zero-cost" a misnomer.

The price is equally clear: propagation correctness = an insertion obligation distributed across roughly 80 emission sites, and the symptom of missing one is a teleporting exception or silent success — "compile succeeded with no output." The repository's answer to that price is layered: the `main` error epilogue as a backstop printer, the [AGENTS.md](../../AGENTS.md) three-cause invariant, the stage1-as-compiler gate, and the emission-site audit that turns the distributed obligation into an enumerable list. On the runtime side, five C files totaling some five hundred lines, each paired with a pcc-Python mirror, with the TLS storage and the class table sunk into the substrate so logic stays replaceable while state stays put — the exception subsystem doubles as the showroom for the mirroring discipline (see Chapter 14) and the five-GC equality contract (see Chapter 10).

## Exercises

1. **Verify against the source.** Read `py_raise_normalize()` in `py_exc_tls.c` and `exc_to_class()` in `py_exc_match.c`: explain why a raised **instance** of a user exception subclass must be preserved as is rather than wrapped into a fresh `PY_TYPE_EXC`, and which half of the fix each function carries. If only the former had been changed, what would happen to `except MyError`?
2. **Trace one lowering.** For `try: f() except (A, B) as e: g()`, list — per `_emit_try()` — the runtime functions the generated code calls in order (from `py_current_exception` to handler exit), and identify: at which point is the TLS cleared? Where is `e`'s reference retained, and where released? Why must `e` be registered in `_except_binding_names`?
3. **Construct the failure.** Suppose runtime function `h()` does a `py_raise` of a `TypeError` and returns NULL, and its emission site is missing the check. Following 8.6's failure chain, write out the follow-on code shapes each of the two endings requires (4a misplaced detonation / 4b silent success), and explain why `_ensure_fn_err_exit()` choosing 0 as the integer sentinel is what makes 4b possible. Would changing the sentinel to -1 cure it? What would it break?
4. **Argue a design tradeoff.** Suppose the self backend one day supports CFI metadata emission — should pcc migrate back to Itanium unwinding? Argue from four angles: `StopIteration` frequency; where Chapter 9's owned-local cleanup goes on the unwind path; the unwind-table differences between Mach-O and ELF; and what the "missing check" failure class corresponds to in the other model (a wrong table entry).
5. **Improve the audit.** The 8-line-window heuristic of 8.7's second case study produces both false positives (the check is outside the window) and potential false negatives. Design a better static audit: how would you recognize the maybe_end/propagate equivalent routing? How would you handle the `@c_abi_export` suppression rule? What can it still not prove (hint: reachability, and whether the runtime function can actually raise on that path)?
