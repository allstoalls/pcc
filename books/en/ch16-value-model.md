# Chapter 16: The Value Model — Projection, not Fixed Width

The preceding chapters built pcc's object world: every value is a heap object with a header, kept alive by reference counting and five GC backends (Chapters 7, 9, 10). That world is semantically complete and expensive on hot paths — one `Point(1, 2)` is an allocation, an object header, two boxed fields, and several indirections. The value model is pcc's answer to that identity tax, and it is where obligation 7 of the project's north star lands. Its core position compresses into this chapter's title: **borrow the projection, not the fixed width**. What pcc takes from Java's Project Valhalla is the projection model — the separation of semantic type from physical representation. What it explicitly refuses to take is Java's historical decision to define `int` as a 32-bit wrapping integer. This chapter covers `int`'s two projections (the tagged small-int lane and the boxed bignum), the contract for explicit machine-integer types, the actual state of `@pcc.valueclass` — and, under this book's honesty obligation, the complete dossier of one confirmed defect from discovery through ruling to its fix (2026-06-17): typed-int unboxed arithmetic once wrapped silently on i64 overflow.

## Chapter Overview: The Value Model Is Projection, Not a Semantic Swap

The main misunderstanding to avoid is this: the value model does not secretly turn ordinary Python classes into identity-free structs, and it does not turn Python `int` into a wrapping machine integer. It gives explicitly opted-in hot paths a denser physical representation while preserving observable Python semantics.

- Ordinary classes keep identity, `__dict__`, weakrefs, finalizers, dynamic attributes, and inheritance.
- A value class is an explicitly chosen identity-free payload with clear boxing and unboxing boundaries.
- Python `int` remains arbitrary precision; the small-int lane is a physical projection, and overflow must promote or deopt.

## 16.1 The Problem and the Design Space

State the problem precisely. Python's `int` is semantically an arbitrary-precision integer: `2**40 * 2**40` is `2**80`, with no room for negotiation. Python's objects semantically have identity: `id()` is stable, `is` is decidable, weak references can observe them, `__dict__` accepts dynamic attributes, subclassing works, and `__del__` runs at death. A compiler that wants to lower Python to native code faces these two semantic facts with three families of answers in the design space.

**Alternative one: define `int` as a machine integer.** The direction taken by Cython's `cdef long` and mypyc's native ints: an `int`-typed value *is* an i64, and addition *is* one `add` instruction. Fast, and simple to implement. The price is that the semantics get swapped out — i64 overflow wraps, `mul(2**40, 2**40)` yields 0, silently diverging from CPython. This is exactly Java's `int`: fixed-width wrapping written into the language semantics for performance. pcc's north star forbids this direction. Obligation 2 states that performance must be proven and that "a slow path preserves Python semantics when assumptions fail"; obligation 7 names the specific case: "value-lane overflow must deopt/promote, never wrap."

**Alternative two: box everything.** Every `int` is a heap bignum; every addition is a runtime call. Semantically unimpeachable; performance regresses to interpreter magnitude, and the "performance bridge" obligation 7 promises never materializes.

**Alternative three: the projection model.** This is the answer pcc distilled from Valhalla, written down in the V-track section of [goal-prompt.md](../../docs/goal/goal-prompt.md): **separate the semantic type from the physical representation**. A semantic type may have two physical projections — a value projection and an object projection — switched at explicit seams by the compiler and runtime; optimization is allowed to change the representation, never the semantics. Concretely, for three types:

```text
Python int         semantics = arbitrary precision, ALWAYS
  ├── value projection : tagged small-int lane (~i63)
  └── object projection: boxed bignum (PyIntObject)
      value-projection overflow -> deopt/promote to the object
      projection; it MUST NOT wrap

pcc.i64 / pcc.u64  semantics = explicit machine integer
                   (a contract, not yet implemented — see 16.4)
  └── raw i64/u64 projection; wrap/trap/checked/saturating is
      written into the type — the only legal home of fixed width

@pcc.valueclass C  semantics = identity-free immutable payload
  ├── value projection : LLVM aggregate payload (e.g. {i64, i64})
  └── object projection: ValueBox (PY_TYPE_VALUEBOX)
      field semantics follow the FIELD type: an int field is a
      Python bigint
```

The projection model carries one unglamorous but important corollary, the same fact Valhalla's JEP 402 acknowledges: **the seam is real; do not pretend it is not.** An `int` value may be inlined here and boxed there; a valueclass payload may be an aggregate in registers here and a heap ValueBox there. pcc does not pretend the two are indistinguishable. Instead it makes every switching point explicit and auditable: overflow promotion is an explicit branch, boxing/unboxing is an explicit emission, and identity observation is an explicit diagnostic (16.5). The rest of this chapter walks the source along these three seams — and stops, in 16.3, at the one path that has not yet honored its seam obligation.

## 16.2 `int`'s Two Projections: the Tagged Lane and the Boxed Bignum

### 16.2.1 Encoding: One Low Bit Instead of an Object Header

The value projection's runtime encoding lives in [pcc/runtime/src/py_internal.h](../../pcc/runtime/src/py_internal.h). `PY_IS_TAGGED_INT(p)` tests the pointer's low bit: 1 means a value, 0 means a real `PyObject*`. Since `malloc` returns at least 8-byte-aligned pointers on every supported platform, bit 0 of a genuine pointer is always 0 — the bit is free. Encoding and decoding are one line each: `py_tag_int()` shifts left by one and sets the low bit; `py_untag_int()` casts through `intptr_t` so the right shift is arithmetic and sign-preserving. The tagged payload is therefore 63 bits:

```c
#define PY_TAGGED_INT_MIN  ((int64_t)INT64_MIN >> 1)   /* -2^62 */
#define PY_TAGGED_INT_MAX  ((int64_t)INT64_MAX >> 1)   /*  2^62 - 1 */
```

It is worth listing everything that single bit buys: no allocation (the value lives in the pointer's bit pattern), no object header, no reference counting (one of the fast paths at the top of `py_incref`/`py_decref` in Chapter 9 is precisely `PY_IS_TAGGED_INT` → return), and no GC participation. The cost is that every runtime entry point consuming a `PyObject*` must first ask "pointer or value?"

The object projection is `PyIntObject`, defined in the same header: a sign-magnitude bignum with base-2^32 digits stored little-endian, `sign` taking -1/0/+1, and a flexible array `digits[]` of length `ndigits`. The comment states two canonical-form invariants: `sign == 0` iff `ndigits == 0` (zero has no digits), and when `sign != 0` the top digit is nonzero. A third invariant, written directly above the struct, matters even more: **values that fit the tagged range should be stored as tagged ints, not as `PyIntObject`s.** The representation is canonical — a given mathematical value has exactly one legal encoding — so equality and hashing never have to reconcile two representations of the same number.

Canonicalization is enforced by two functions, both in [pcc/runtime/src/py_int_core.c](../../pcc/runtime/src/py_int_core.c). `py_int_from_i64()` is the chooser on the construction side: tag if in range, otherwise `py_bigint_from_i64` (worst case two digits; `INT64_MIN` is negated safely through unsigned arithmetic). `py_bigint_to_pyobject()` is the collapser on the computation side: if a freshly computed bignum fits the tagged range, free the bignum and return the tagged value. Promotion and collapse both exist; values do not drift one-way into the object projection.

### 16.2.2 Runtime Arithmetic: Overflow Means Promote

[pcc/runtime/src/py_int_ops.c](../../pcc/runtime/src/py_int_ops.c) is the object-level arithmetic dispatch, and every operation has the same shape — try the value projection, and on failure promote to the object projection:

```c
PyObject *py_int_add(PyObject *a, PyObject *b) {
    if (is_tagged_both(a, b)) {
        int64_t av = py_untag_int(a);
        int64_t bv = py_untag_int(b);
        int64_t r;
        if (!__builtin_add_overflow(av, bv, &r)) {
            return py_int_from_i64(r);
        }
    }
    PyIntObject *ba = promote_any(a);
    PyIntObject *bb = promote_any(b);
    ...
    PyIntObject *br = py_bigint_add(ba, bb);
    ...
    return wrap_bigint(br);
}
```

Read line by line, this *is* the projection model. When both operands are tagged, a checked i64 addition runs via `__builtin_add_overflow` — note that the check is against i64, and the result still passes through `py_int_from_i64`'s tagged-range decision, so values in the "gap" between i63 and i64 correctly land on the heap. If either check fails, `promote_any()` (i.e. `py_bigint_from_any`) promotes both sides to bignums, `py_bigint_add` (the sign-magnitude add/sub in [pcc/runtime/src/py_int_addsub.c](../../pcc/runtime/src/py_int_addsub.c)) computes the exact result, and `wrap_bigint()` collapses it back through `py_bigint_to_pyobject`. `py_int_sub`/`py_int_mul` are isomorphic; `py_int_neg` separately guards `INT64_MIN`, whose negation does not fit i64.

Several fast paths hide semantic corrections worth naming. `py_int_floordiv`/`py_int_mod`: C division truncates toward zero while Python floors, and the remainder takes the divisor's sign, so the fast paths carry an explicit quotient-minus-one / remainder-plus-divisor adjustment — and since the operands are known to be in the tagged range, the comment proves the adjustment itself cannot overflow. `py_int_shl` implements left shift as "multiply by 2^n with overflow check," falling back to `py_bigint_shl` — section 16.3 returns to how the unboxed mirror of this operation lost exactly this semantics. `py_int_truediv` returns a `PyFloatObject` and returns NULL on a zero divisor, deferring the `ZeroDivisionError` raise to the caller.

The file split is itself a design decision. `py_int_core.c`, `py_int_ops.c`, `py_int_addsub.c`, `py_int_mul.c`, `py_int_convert.c`, `py_int_bigint_convert.c`, `py_int_parse.c`, and `py_int_decimal.c` all carry a variant of the same header comment: "split from py_int.c so the pcc-Python runtime can replace it independently." Each C file has a same-named pcc-Python port under [pcc/runtime/py/](../../pcc/runtime/py) (`py_int_core.py`, `py_int_ops.py`, …). This is Chapter 14's migration — shrink the C semantic runtime, grow the pcc-Python runtime — sliced through the integer subsystem: the finer the split, the smaller the independently replaceable, independently verifiable unit. The header of `py_int_mul.c` even preserves an honest boundary note: schoolbook multiplication's `uint32*uint32` intermediate needs full unsigned 64-bit behavior, which the pcc-Python surface of the day could not comfortably express, so multiplication was split out later than add/sub.

### 16.2.3 The Value Lane in Generated Code: the Inline Tagged Fast Path

The runtime's two projections settle correctness; performance requires inlining the value projection into generated code, eliminating the call. That step is `_emit_inline_tagged_int_binop_or_call()` in [pcc/frontends/python/codegen/binary_op_lowering.py](../../pcc/frontends/python/codegen/binary_op_lowering.py). When `int` expressions flow in boxed representation (`_int_exprs_are_boxed()` is true, making `IntType`'s storage type `PyObject*`), the operators `+`/`-`/`&`/`|`/`^` do not emit a bare runtime call; they emit an inline CFG:

```text
ptrtoint both operands -> test each low bit -> and -> cbranch
fast block: ashr 1 to untag -> add/sub/and/or/xor
            (+/- additionally test result in [-2^62, 2^62-1];
             if it does not fit, branch to slow)
            shl 1 | 1 to retag -> inttoptr -> join
slow block: call py_int_add/... (full bignum capability) -> join
join block: phi
```

This IR is the value projection in its compiler form. The fast path stays entirely in registers — no allocation, no call. The `+`/`-` range check on the result is the deopt point: if the value does not fit back into 63 bits, the **original boxed operands** are handed to the slow path, which recomputes and yields a legal tagged value or bignum, bit-for-bit equal to CPython. The bitwise operators `&`/`|`/`^` are closed over 63 bits and need no check. `*` is not on the inline list: the product of two 63-bit numbers needs a 126-bit intermediate, the cost structure of an inline check is different, and the operation currently stays on the runtime call (`py_int_mul`'s `__builtin_mul_overflow` path).

Two pieces of after-care follow the slow-path call, both in `_emit_runtime_int_binop_value()`: operations that can raise (shifts) emit the `py_err_occurred()` check (Chapter 8's lowering obligation), and a NULL result from `//` or `%` goes through `_emit_zero_division_if_null()` to raise `ZeroDivisionError` — the runtime comment explicitly defers the zero-divisor raise to the caller, and the frontend must catch it.

Up to this point, the boxed-representation side of `int`'s projection is complete and honest: the value lane exists, overflow promotes, and the slow path is semantically whole. The problem is the other side.

## 16.3 A Defect Dossier: Typed-Int Unboxed Arithmetic Once Wrapped Silently on i64 Overflow

This section is the chapter's honesty obligation. The defect below was flagged by an external audit on 2026-05-30, reproduced and confirmed in-repo the same day, is recorded in [docs/investigations/typed-int-unboxed-overflow-silent-wraparound.md](../../docs/investigations/typed-int-unboxed-overflow-silent-wraparound.md), and **was fixed on 2026-06-17** (16.3.4). The section keeps the full arc from symptom to fix as a working example of claim hygiene.

### 16.3.1 Symptom and the Precise Trigger Surface

First, what does *not* trigger it. Large literals and growing accumulators do not: `x = 2**63 - 1; x = x + 1` under strict no-libpython with the self backend prints byte-identically to CPython — type inference routes those values to the boxed path of 16.2, and the IR contains `call @py_int_add`.

What triggers it is **explicitly `int`-annotated function parameters**:

```python
def mul(a: int, b: int) -> int:
    return a * b

print(mul(1099511627776, 1099511627776))   # 2^40 * 2^40
# pcc:     0
# CPython: 1208925819614629174706176
```

pcc prints 0 — 2^80 mod 2^64. Also confirmed wrapping: `+` (`addf(2**63 - 1, 5)` goes negative), the overflow value carried through a function return ABI, through a local slot, and `<<` (a raw i64 `shl` masks the shift count, so `1 << 100` computed `1 << 36`). The 2026-05-31 probes found `-` and an `a*b > <large literal>` comparison already box correctly. The defect surface is the `+`/`*`/`<<` path through the typed-int ABI — not integer arithmetic wholesale.

### 16.3.2 Root Cause: Pinning the Semantic Type to the Machine Representation

The causal chain is three source-confirmed steps:

1. `_type_is_typed_int_abi_param()` in [pcc/frontends/python/codegen/typed_int_abi.py](../../pcc/frontends/python/codegen/typed_int_abi.py) returns true unconditionally for `IntType` — an `a: int` annotation gives the parameter a native i64 ABI, fixing the signature as `define external i64 @user_..._mul(i64 %a, i64 %b)`.
2. The integer tail of `_emit_binop_value()` in `binary_op_lowering.py`: `lv = _to_int64(lhs); rv = _to_int64(rhs); return self._emit_binop_int(op, lv, rv)`.
3. `_emit_binop_int()` emits raw `builder.add`/`builder.sub`/`builder.mul` for `+`/`-`/`*` — bare i64 instructions, no overflow check, no slow path.

In the projection model's vocabulary: this path equates the Python **semantic type** `int` with the **machine representation** i64. The value projection has no deopt point, so overflow wraps. This is precisely alternative one from 16.1 — the Java-`int` direction the north star forbids by name — sneaking back in through the typed-ABI side door. The V-track in [goal-prompt.md](../../docs/goal/goal-prompt.md) calls this path out as "exactly the confusion the projection model forbids."

It is worth stressing why no local patch can fix it, because the investigation nailed this down with two failed experiments (2026-05-31): removing `*`/`<<` from the safe-op set of `_typed_int_expr_is_i64_safe` had zero effect; excluding `*` from `_expr_is_native_typed_int_shape` had zero effect. The reason is a representation constraint: a correct bignum result **does not fit in an i64 return register**. As long as `mul`'s signature is `i64(i64, i64)`, no tightening at the analysis layer changes the fact that the result has nowhere to go. The fix must be at the representation/ABI level: `int` parameters, returns, and slots move from i64 to a `PyObject*` that can carry a tagged value or a bignum.

### 16.3.3 The Design Tension: Two Proposals and Their Real Costs

The investigation recorded two candidate fixes and one cost reversal; this book reports them as found.

**Proposal No.1: overflow-checked fast path + boxed promotion.** Replace `_emit_binop_int`'s raw instructions with `llvm.sadd/ssub/smul.with.overflow`; on the overflow bit, branch to the `py_int_*` slow path; represent the result as a tagged int (i63 inline, boxed on overflow) — structurally the same shape as 16.2.3's inline fast lane. Semantically correct, and it preserves the unboxed fast lane: the overflow branch is predicted-not-taken in the common case. The cost is implementation surface: the typed-int result representation, local slot stores, return ABI, and caller conventions all move — a subproject on shared codegen and the bootstrap-critical path.

**Proposal No.2: conservative boxing.** Keep raw i64 only in provably bounded contexts (loop counters, range indices); route arbitrary-provenance `int` values — function parameters included — through the boxed path. Initially selected as the immediate fix: simpler, definitely correct. A feasibility probe (2026-05-31 (b)) then surfaced its real cost: [tests/python/test_py_typed_int_unboxed.py](../../tests/python/test_py_typed_int_unboxed.py) is a live obligation-7 gate — fourteen cases asserting that accumulator loops contain **no** `@py_int_add` in their IR and that signatures stay `define i64 @user_*_bench(i64 %n)`. An accumulator (`total = total + step(i)`) is fundamentally unboundable, so under "int = bignum unless proven safe" it must box, and the gate's assertions must be **inverted**. In other words, No.2 is not "accept some performance cost" — it removes the typed-int unboxed fast lane for non-range loops, gutting the very performance bridge obligation 7 exists to provide.

That cost reversal rewrote the recommendation: No.1 (the tagged-int fast lane) is not merely the long-term answer but the more defensible immediate one — the gates change **shape** (unboxed add plus an overflow branch) rather than invert to fully boxed. CPython's ints are always overflow-checked — it is inherent to arbitrary precision — so a checked fast lane remains strictly faster than CPython. Performance was never a reason to keep the bug.

### 16.3.4 The Ruling, the Fix, and the Binding Semantics Rule

Three durable things landed ahead of the fix. First, the **type-semantics rule** (user-pinned 2026-05-31, now the binding contract): the Python annotation `int` means arbitrary-precision integer; a raw machine integer requires an explicit pcc-owned type (e.g. `pcc.i64`); unboxed i64 is an optimization, never the user-visible meaning of `int`. Second, **five xfail regressions**: [tests/python/test_native_typed_int_overflow.py](../../tests/python/test_native_typed_int_overflow.py) pins the acceptance criteria as `xfail(strict=False)` cases — `+`/`*` parameter overflow, chained `a*b+c`, overflow through the return ABI, through a local slot, and `<<` promotion. They recorded the defect as XFAIL, waiting to flip green. Third, a **priority ruling**: P0 correctness > performance > package expansion — a silently wrong `def f(a: int, b: int)` punches a hole in strict-native credibility that outranks the package-fallback-shrinking work.

The fix landed on 2026-06-17; all five acceptance criteria flipped green and the xfail markers came off. In [pcc/frontends/python/codegen/typed_int_abi.py](../../pcc/frontends/python/codegen/typed_int_abi.py), the parameter ABI rule for `int` was corrected:

```python
# pcc/frontends/python/codegen/typed_int_abi.py
def _type_is_typed_int_abi_param(self, type_obj: Type) -> bool:
    # int defaults to boxed/tagged PyObject* ABI; raw i64 is opt-in only
    if isinstance(type_obj, IntType):
        return False
    return False
```

What landed combines both proposals of 16.3.3: on the admission side, the `int` annotation now defaults to the boxed/tagged Python-int ABI, and the raw i64 function ABI is demoted to an explicit, mode-labeled escape hatch rather than the default meaning of `int` (the regression file's module docstring is the literal text of that contract); on the arithmetic side, the tagged lane keeps its speed through inline overflow fast paths such as `llvm.smul.with.overflow.i64` (`test_tagged_int_mul_uses_inline_overflow_fast_path`). The obligation-7 gate [tests/python/test_py_typed_int_unboxed.py](../../tests/python/test_py_typed_int_unboxed.py) was rewritten to the new contract: general accumulator loops now assert that `@py_int_add` **appears** in the IR (boxed/tagged semantics), and only provably safe shapes keep the purely unboxed lane. Today `mul(2**40, 2**40)`, `2**62 * 4`, and `1 << 100` match CPython bit for bit under the strict self backend in no-libpython mode, across `PCC_GC_BACKEND=0..4`.

The existence of this section is the style contract executing itself: a known defect is written as an open problem, with its tension explained and nothing dressed up.

## 16.4 Explicit Machine Integers: the `pcc.i64` / `pcc.u64` Contract

Once the projection model assigns arbitrary precision to `int`, fixed-width semantics need a legal home. The contract is written in the V-track of [goal-prompt.md](../../docs/goal/goal-prompt.md): `pcc.i64` / `pcc.u64` are explicit machine-integer semantic types with a single raw i64/u64 projection, and **the overflow policy — wrap, trap, checked, or saturating — is written into the type itself**, visible in source. Java/C-style fixed-width behavior is permitted to live here and only here; it may never be the default meaning of `int`.

An honest label: as of this writing, `pcc.i64`/`pcc.u64` is a design contract, **not yet implemented** — a search of the [pcc/](../../pcc) source tree finds no corresponding type implementation; only the goal document carries the specification. It is not part of fixing the 16.3 defect (that fix is about making `int` stop meaning i64); it is an independent V-track type-system addition: a place where code that genuinely wants machine semantics — bit manipulation, hashing, runtime-kernel code talking to a C ABI — can say so without lying.

Writing the overflow policy into the type is the same argument, from the other side, as Chapter 4's signedness lesson in the C frontend. There, `int` and `unsigned` both lower to i32 and signedness travels as out-of-band metadata (`_tag_unsigned`/`_is_unsigned_val`); the classic failure mode is that the metadata is dropped on some expression shape and a downstream operator silently picks `sdiv`/`ashr`. Out-of-band semantics rot; type-carried semantics do not. The `pcc.i64` design absorbs that lesson directly: wrap-versus-trap is not a pass's tacit understanding — it is part of the signature.

## 16.5 Value Classes: Opt-In Identity-Free Payloads

### 16.5.1 The Marker and the Host Helpers: What [pcc/value_model.py](../../pcc/value_model.py) Is and Is Not

The `@pcc.valueclass` decorator is defined in [pcc/value_model.py](../../pcc/value_model.py) (lazily exported through [pcc/__init__.py](../../pcc/__init__.py)). On host Python it does three things: turns the class into a `frozen=True` dataclass (the host approximation of immutability), sets the `__pcc_valueclass__` marker, and records a field-layout descriptor via `value_payload_layout()`. At compile time, [pcc/frontends/python/type_infer.py](../../pcc/frontends/python/type_infer.py) recognizes the decorator and produces a `ValueClassType` (defined in [pcc/frontends/python/py_ast.py](../../pcc/frontends/python/py_ast.py)).

The boundaries of this file must be stated first, because they were once overstated — and the correction became an investigation of its own ([docs/investigations/python-valhalla-value-model-actual-state.md](../../docs/investigations/python-valhalla-value-model-actual-state.md)). The dataclasses in the file — `ValuePayload`, `ValueBox`, `SpecializedArray`, `GenericSpecialization` — are **host-side projection helpers for planning tests, not the production C runtime**. The module docstring says so, and so does `value_model_status()`, which maintains three honest lists: `implemented` (the V1 scalar-payload slices, the V2 selected pointer-field boundaries, and so on), `not_implemented` (full marshal coverage, flattened layout metadata, `pcc.array[ValueClass]` contiguous storage, monomorphization, …), and `production_runtime: False`. The status once claimed "implemented through V6"; code inspection showed V1–V6 were mostly metadata scaffolding, and the status surface was rewritten to distinguish implemented from scaffolding. In a project that treats claim hygiene as an architectural component, even the status function submits to audit.

### 16.5.2 The Value Projection: Scalar Payloads as LLVM Aggregates

The core implemented slice (V1) is direct payload lowering for scalar-field valueclasses: `p = Point(1, 2)` no longer calls `py_instance_new` but constructs an LLVM aggregate `{i64, i64}`; `p.x` is one `extractvalue`; function arguments, constructor returns, and method receivers can all use the payload ABI (`def norm2(p: Point) -> int` carries the aggregate in its signature); `p == q` lowers fieldwise to `icmp`/`fcmp` chains ahead of class dunder dispatch. V2 extends to selected pointer fields (`Bag(items: list, count: int)` carries the `list` field as a pointer in the payload) and to non-recursive nesting. The IR-shape gate [tests/python/test_py_value_class_unboxed.py](../../tests/python/test_py_value_class_unboxed.py) asserts the hot path contains no `py_instance_new` — which is what evidence for obligation 7's "performance bridge" looks like here: not a benchmark number, but allocation sites vanishing from the IR.

The payload form is only sound because the shape is restricted, and the restrictions are enforced as compile-time diagnostics. `_validate_valueclass_shape()` in `type_infer.py` rejects: subclassing (outside the V0 subset), defining `__del__` (no identity means no finalization moment; the hint says to move finalization to an owning identity object), declaring `__dict__`/`__weakref__` (including hidden inside a `__slots__` tuple — `_slots_contains_identity_slot()` scans for exactly that), and fields without type annotations. `_validate_valueclass_recursion()` rejects recursive and mutually recursive payload graphs (direct self-containment, mutual containment, container-mediated self-reference) — a flattened layout cannot hold an infinite expansion, and explicit rejection beats silent boxing. Every diagnostic carries a repair hint, and the recurring phrase among them is this chapter's stance in five words: "use a normal identity class." If you want identity, take an ordinary class; the value class does not steal it.

### 16.5.3 The Boxing Bridge: ValueBox and the Object Boundary

The moment a payload flows toward dynamic context (an `Any` parameter, a container, `print`), it crosses the seam into the object projection. The runtime side of the bridge is `py_valuebox_new()` in [pcc/runtime/src/py_class.c](../../pcc/runtime/src/py_class.c): allocate a `PyValueBoxObject` sized by the class's field count, with type tag `PY_TYPE_VALUEBOX = 200` (a public enum value in [pcc/runtime/include/py_runtime.h](../../pcc/runtime/include/py_runtime.h)). The design deliberately reuses an instance-compatible layout — `py_valuebox_get_field`/`py_valuebox_set_field` delegate directly to `py_instance_get_field`/`py_instance_set_field`, which read and write slots through `pcc_gc_load_ptr()`/`pcc_gc_store_ptr()`. That one line of delegation buys the entire infrastructure of Chapter 10: a ValueBox's pointer payload automatically participates in the slot-based trace/update contract shared by all five GC backends — `py_gc_track` registration, write barriers, relocation updates, all of it. **GC tracing of pointer-bearing payloads is not a bolt-on valueclass feature; it falls out of parasitizing the single object-graph rule set.** Equality and hashing cross the bridge too: `py_obj_eq`/`py_obj_hash` each have a `PY_TYPE_VALUEBOX` branch (in both runtime tiers, C and pcc-Python), comparing the class first and then each payload slot through GC-aware loads, so independently boxed but payload-equal values hit the same dictionary key.

The other half of the seam is an honest declaration: every boxing creates a **new** box. Hand the same `Point(1, 2)` to an `Any` boundary twice and you get two distinct heap objects. That is exactly why identity observation must be rejected rather than answered — the comparison table in the next section and the first case study in 16.7 both start from this fact.

### 16.5.4 Bootstrap and the Self Backend: the Payload ABI All the Way Down

The value projection is not an LLVM-backend-private optimization. Aggregate payloads appear in function signatures, which means the self backend (Chapter 13) — its IR text parser, ABI lowering, register allocation — must understand `{ i64, i64 }`; the third case study in 16.7 is the record of the thinnest link in that chain snapping. And every valueclass slice carries the full bootstrap gate under Chapter 15's discipline — the recurring closing line in the investigation files is "five-GC bootstrap matrix → 5 passed," mode-labeled strict no-libpython, `--backend self`.

## 16.6 Identity Must Not Be Stolen: Ordinary Classes versus Value Classes

The first half of obligation 7 is the half people skip: "**no theft of ordinary-class semantics**." Every performance benefit of the value model must come from the user explicitly surrendering identity — never from the compiler quietly deciding on the user's behalf. Here is the comparison, with every rejection in the right column traceable to a point in source:

| Semantic capability | Ordinary class (Chapter 7) | `@pcc.valueclass` |
|---|---|---|
| stable `id(x)` | kept | compile-time rejection (`type_infer.py` builtin-call branch: "id() is not supported for valueclass payloads in strict mode") |
| `x is y` | kept | compile-time rejection (Compare branch: "identity comparison is not supported…", hint: compare fields with `==`) |
| `weakref.ref(x)` | kept | compile-time rejection (Call branch) + runtime rejection (`py_weakref.c` raises TypeError on `PY_TYPE_VALUEBOX`; the CPython analogue is `weakref.ref(3)`) |
| `__dict__` / dynamic attrs | kept | compile-time rejection (`_validate_valueclass_shape`, including the `__slots__` scan) |
| field mutation | kept | immutable (frozen-dataclass semantics; payloads copy by value) |
| subclassing | kept | compile-time rejection (V0 subset) |
| `__del__` finalizer | kept (`py_user_del_dispatch`, Chapter 9) | compile-time rejection (no identity, no finalizable lifetime) |
| `==` / `hash` | identity-based by default, overridable | field-value-based (direct payload compare, or the `PY_TYPE_VALUEBOX` branches) |

The depth of the three-layer defense is deliberate: statically known payloads are stopped at compile time by `type_infer.py`, with a source span and a repair hint; boxes that escape static knowledge into Dyn are stopped at runtime by `py_weakref_new`, aligned with CPython's existing precedent for identity-free values. And not one line on the ordinary-class side concedes anything to the value model — `id`, `is`, weak references, `__dict__`, mutability, subclassing, finalizers all remain intact. That is what "opt-in" means in practice.

The relationship with Valhalla can now be closed out. pcc borrows the projection model — semantic type separated from physical representation, identity as a semantic cost, the object/value boundary managed by an explicit boxing bridge, optimization never changing semantics. It does not borrow Java's fixed-width wrapping `int` (the 16.3 defect is a live case of that red line being breached), and it does not treat "Valhalla" as a brand or design constraint — obligation 7 in [AGENTS.md](../../AGENTS.md) states it is only the reference the concept was distilled from.

## 16.7 History and Lessons

Where the value model's seams actually are, the investigation archive says more honestly than any design document. Under [docs/investigations/](../../docs/investigations) there are more than twenty files named `valuebox-valueclass-*-projection` — attribute store, membership needle, comprehension, exception argument, conditional expression, short-circuit, `dataclasses.replace`, `super` method arguments, and on. Each is one sample of the same fact: **the value/object seam appears at every emission site that can carry a value into dynamic context, and each missed site materializes an identity-bearing instance there (or crashes).** The three stories below are the most instructive segments of that map.

### Story One: `weakref.ref(Pt(1, 2))` Succeeded (2026-06-10)

The V-track design question "weak-dict key policy" reduced to a probe: what happens today if you take a weak reference to a valueclass payload? The expectation was rejection; the observation was **success** — the constructor projected to a payload, the object-boundary projection boxed it, and the runtime created a weakref to that ValueBox; the probe printed `weakref-ok 1` (strict no-libpython, self backend). This is textbook identity theft: a weak reference observes identity *lifetime*, and 16.5.3 established that every boxing makes a new box — the object behind this weakref dies at an unpredictable moment, and `r()`'s answer is a function of a representation detail. The instructive contrast: the probe's first draft also wrote `r() is p`, and the *existing* `is` diagnostic correctly stopped it. The `is` fence was standing; the weakref hole was right beside it.

The fix landed in two layers, matching the table's defense-in-depth: a compile-time diagnostic in `type_infer.py`'s Call branch (same mechanism and placement family as the `is` diagnostic), then a dynamic-path slice making `py_weakref_new` raise TypeError on `PY_TYPE_VALUEBOX`, mirrored in both the C runtime and the pcc-Python port — and `WeakKeyDictionary`/`WeakValueDictionary` inherited the rejection automatically because their runtime paths construct weakrefs through the same entry point.

Three lessons. First, the identity-escape surface is **enumerated, not derived** — plugging `is` does not plug `id()`, and neither plugs weakrefs; every identity-observing API must be probed individually. Second, the fix unearthed two buried traps along the way: a stale port `.o` nearly produced a false negative on the first verification (deleting the archive is not enough when a port `.py` changes; the cached object file must be invalidated too — extending the repository's recorded stale-archive lesson), and the lowering sites in `native_weakref.py` had never emitted `_emit_post_call_err_check`, so the correctly raised runtime exception "teleported" past the enclosing try/except — Chapter 8's "no Itanium unwinding; miss the check and exceptions teleport" failure class recurring verbatim on the value-model construction site. Third, the from-import form (`from weakref import ref`) was deliberately left uncovered and recorded as such — the bare name `ref` is too generic to match safely; a documented narrow hole beats an unreliable wide match.

### Story Two: Pointer Payloads Go Amnesiac under Backend #4 Relocation (2026-06-01)

The five-GC production equality contract (Chapter 10) gained a pointed test: box a valueclass carrying pointer fields, force backend #4 to relocate the ValueBox, then mutate and read payload fields back through the Python object path. The initial evidence said backend #0 passed and #1–#4 all raised `AttributeError: items`; after two narrowing fixes only #4 remained, printing the relocation prologue and then silently returning — none of the five payload-readback lines.

The proposal list of this investigation (`gc-5backend-valueclass-pointer-payload-roots-no-libpython.md`) is a methodology lecture by itself — seventeen proposals, the first one stamped `[REJECTED as implemented]`. The three most instructive steps:

First, strip the red herring. `AttributeError: items` was never an attribute-system failure: the dynamic valueclass getattr lowering **speculatively** emitted `py_obj_getattr(box, "field")` before deciding whether the attribute was a payload field, and the fallback branch's exception side effects contaminated the scene. Emitting the fallback only inside the fallback block made the false symptom vanish and exposed the real failure: backend #4 walked off through the error exit holding a stale pointer after the first `gc.collect()`.

Second, locate by substitution (the debugging playbook's "test hypotheses by substitution, not only inspection," Chapter 18). Change exactly one thing: in the #4 branch, call `check_payload(loaded)` instead of `check_payload(box)`, where `loaded` is the object read back through `pcc_gc_load_ptr()` from a registered root slot. All five payload lines printed. That single substitution proved two things at once: the relocated copy of the ValueBox **had preserved the payload intact** (runtime innocent), and the broken piece was the un-updated stale pointer in `check_payload`'s parameter slot `%box.addr` (frontend guilty). Root cause: borrowed object parameters were not registered as updateable GC frame roots, so when relocation eventually cleared the forwarding source, the slot held a dangling pointer. The fix registers user functions' object parameters as borrowed frame roots — updateable by #4, but ownership-neutral: no extra release at cleanup.

Third, the lesson that runs the other way. The pcc-Python runtime mirror briefly grew a `_resolve_instance()` helper to normalize forwarded pointers, but it returned existing instance pointers through normal object-return ownership and emitted a spurious `pcc_gc_retain` on borrowed receivers — inflating refcounts of cycle members under backend #0 until `gc.collect()` reported zero collected and finalizers stopped running. Proposal No.1 was marked REJECTED and the helper deleted. The value-model side of the conclusion: **value payloads do not bypass the object-graph contract** — pointer-bearing payloads must be traced, the carriers of those payloads must be rooted, and whoever fixes the roots must themselves obey Chapter 9's ownership rules, or fixing #4 breaks #0.

### Story Three: the Self Backend Does Not Understand `{ i64` (2026-06-04)

An ordinary program shape from the V2 boundary work — a boxed `Point` recovered through a tuple/list subscript and passed to `def total(p: Point) -> int` — failed to compile under strict no-libpython with the self backend: `BackendUnavailable: self backend does not understand LLVM type '{ i64'`.

The half-aggregate in the error message is the entire clue. Valueclass lowering emitted a call with an explicit aggregate signature — `call i64 ({ i64, i64 }) @...` — and `_parse_call_signature()` in [pcc/backend/self_backend_parse.py](../../pcc/backend/self_backend_parse.py) split signature arguments with a bare `inner.split(",")`, cleaving `{ i64, i64 }` at its field comma into `{ i64` and `i64 }` and feeding the first half to `_parse_type()`. The fix is one line: use `split_top_level()`, which the rest of the parser already uses to keep nested aggregates, arrays, vectors, parenthesized constants, and quoted values intact.

The lesson is not the fix's difficulty but the dependency chain it exposed. The line "value projection: LLVM aggregate payload" in 16.1's diagram means, in practice, that **everything from type inference through lowering through IR text down to every string-splitting site in the self backend's hand-written parser** must understand aggregates. The first hypothesis ("valuebox unboxing emitted malformed IR") was rejected — the IR was legal; the parser lagged. The third ("the self backend's aggregate ABI cannot pass small payloads") was rejected too — a direct nested-aggregate smoke test had long compiled and run. The failure lived in the least-suspected layer: a `split(",")`. Each square the value model advances, the whole toolchain gets re-audited one square deep — which is why every valueclass slice's definition of done includes the five-GC full bootstrap gate.

## 16.8 Summary

The value model is pcc's structured answer to the question "can Python keep its semantics and still get flat-data performance," and the shape of the answer is projection: the semantic type is constant, the physical representation is one of two, and the seam between them is explicit and audited. For `int`, the seam is complete on the runtime side — one pointer bit in `py_internal.h` buys an allocation-free 63-bit value lane, every arithmetic operation in `py_int_ops.c` promotes at overflow and never wraps, and `binary_op_lowering.py` inlines the same shape into generated code. The seam obligation on the typed-int ABI side has also been honored (2026-06-17): `a: int` parameters default to the boxed/tagged ABI, the tagged lane promotes at overflow and never wraps, and the five acceptance criteria once pinned as xfail have all flipped green (16.3.4). For value classes, the seam is a three-layer defense: compile-time shape and identity-escape diagnostics, a runtime ValueBox that reuses the single slot contract, and explicit rejection of every identity-observing API; the implementation is a set of narrow, honest slices (`value_model_status()` reports `production_runtime: False` about itself), and its boundary has been mapped point by point across twenty-odd projection investigations. Ordinary classes pay nothing for any of this — identity cannot be stolen, and value semantics can only be opted into. That is the full meaning of "projection, not fixed width": performance comes from a legal representation, never from swapped-out semantics.

## Exercises

1. **Verify by reading.** `py_int_add()` in [pcc/runtime/src/py_int_ops.c](../../pcc/runtime/src/py_int_ops.c) checks i64 overflow with `__builtin_add_overflow` on the both-tagged fast path, yet the tagged payload is only 63 bits. Explain why this is not a bug: trace an addition whose sum lands in `[2^62, 2^63)`, naming the functions it passes through and the representation it finally returns in. Then contrast with `py_int_neg()` and explain why it needs a dedicated `INT64_MIN` guard.
2. **Read the IR shape.** `_emit_inline_tagged_int_binop_or_call()` in `binary_op_lowering.py` inlines only `+`/`-`/`&`/`|`/`^`. Argue why the `&`/`|`/`^` fast path needs no range check while `+`/`-` does; then argue what additional IR adding `*` to the inline list would require (hint: the 126-bit intermediate, and how `llvm.smul.with.overflow` relates to the separate tagged-range check).
3. **Reconstruct the causal chain (on paper).** Without running anything, using only `_type_is_typed_int_abi_param()` in `typed_int_abi.py`, the integer tail of `_emit_binop_value()`, and `_emit_binop_int()` in `binary_op_lowering.py`, write down the function signature and multiply instruction IR shape for `def mul(a: int, b: int) -> int: return a * b`, and explain why the investigation's two analysis-layer tightening experiments were doomed.
4. **Argue the trade-off.** Given the cost data of 16.3.3 — Proposal No.2 inverts the fourteen unboxed assertions of `test_py_typed_int_unboxed.py` and removes the accumulator fast lane; Proposal No.1 keeps the lane but moves the typed-int result representation to tagged values, touching the return ABI and slot stores — write the strongest defense of each proposal, deliver your ruling, and state precisely what the obligation-7 IR-shape gates should assert after your chosen fix lands.
5. **Audit the table.** Section 16.6 claims every identity capability rejected for value classes has a nameable rejection point. Verify each row: find the diagnostics for `id()`, `is`, `weakref.ref`, subclassing, `__del__`, and `__dict__` (including the `__slots__` form) in [pcc/frontends/python/type_infer.py](../../pcc/frontends/python/type_infer.py), and the runtime-tier rejection in [pcc/runtime/src/py_weakref.c](../../pcc/runtime/src/py_weakref.c). Which defense has only the compile-time layer? Construct a program shape that slips past it (hint: Story One's from-import note), and explain why the repository chose to record that hole rather than close it.
