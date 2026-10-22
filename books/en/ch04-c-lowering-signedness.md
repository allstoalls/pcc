# Chapter 4: C Semantic Lowering and Signedness

Chapter 3 carried C source up to the line where the AST enters the code generator; this chapter covers the other side of that line: how [pcc/frontends/c/codegen/c_codegen.py](../../pcc/frontends/c/codegen/c_codegen.py) lowers C expressions to LLVM IR. The repository's [AGENTS.md](../../AGENTS.md) characterizes this roughly 11,000-line file in one sentence — "most C-side bugs land here" — and the most prolific family of those bugs orbits a single fact: LLVM's integer types have no sign, and C's integer types do. This chapter takes signedness tracking as its through-line and settles three things: why `int` and `unsigned int` are both `i32` while signedness is tracked separately; how the C standard's usual arithmetic conversions land on `_usual_arithmetic_conversion` and its five sibling helpers; and the design's classic failure mode — bit pattern correct, sign tag lost, and a downstream operator quietly choosing `sdiv`/`srem`/`ashr`/signed comparison. The closing case study comes from Lua: an eerie "sort occasionally fails" failure that eventually shrank to a single XOR that had dropped its unsigned tag.

## Chapter Overview: LLVM Integers Carry No Signedness Label

The first idea is simple: equal bit width does not mean equal semantics. Both `int` and `unsigned int` may lower to `i32`, but later division, remainder, right shift, and comparison must still know whether the C source value was signed or unsigned.

- The hard part of lowering is not emitting one LLVM instruction; it is preserving enough C semantics for the next instruction.
- Signedness can be lost across expression chains, so tests cannot inspect only the immediate result bits.
- The three metadata layers in this chapter form a debugging checklist: value tags, binding tags, and constant values.

## 4.1 The Problem and the Design Space: LLVM Integers Have No Sign

LLVM IR takes a deliberate stance: **integer values carry no signedness; signedness belongs to operations**. An `i32` is just 32 bits; the signed/unsigned distinction is pushed into instruction selection, and the same pair of operands can be fed to either of two instruction sets:

```text
C semantics       signed instruction    unsigned instruction
division /        sdiv                  udiv
remainder %       srem                  urem
right shift >>    ashr (arithmetic)     lshr (logical)
compare < <= > >= icmp slt/sle/...      icmp ult/ule/...
widening          sext (sign-extend)    zext (zero-extend)
int → float       sitofp                uitofp
float → int       fptosi                fptoui
```

This stance is honest about two's-complement machines: `+`, `-`, `*`, `&`, `|`, `^`, and `<<` never distinguished sign under two's complement in the first place — the bit patterns are identical — so LLVM has no reason to provide two instructions for any of them. But the stance hands one responsibility entirely to the frontend: **the signedness information in C's type system must be carried, by the compiler itself, from "the expression that produces a value" to "the operator that consumes it."** The type-mapping table in [pcc/frontends/c/codegen/c_codegen.py](../../pcc/frontends/c/codegen/c_codegen.py), `get_ir_type_from_names()`, is blunt about this: `"int"` and `"int unsigned"` both map to `int32_t`, `"long"` and `"long unsigned"` both map to `int64_t`, and the `signed` keyword is filtered out before the table is even consulted. At the IR-type level, signedness has already ceased to exist.

The design space had three candidates. First, pin the full C type onto every expression value — each codegen method returns not a bare IR value but a wrapper object of "value plus complete C type," in the style of clang carrying full type information on its AST. This is the most rigorous option, but pcc's code generator is architected as a family of `codegen_<NodeClassName>` methods dispatched by an MRO scan inside `codegen()`, each returning a `(value, address)` pair; the wrapper scheme would require rewriting every expression path at once, and the wrapper objects would seep into all code that talks to the llvmlite builder. Second, distinguish signedness with different IR widths — a direct violation of the LLVM model, dead on arrival. Third, pcc's actual choice: **side-channel metadata on the IR value objects, plus a disciplined set of helpers**. The value remains an llvmlite value, but it may carry an `_is_unsigned` attribute; six helpers (`_tag_unsigned`, `_clear_unsigned`, `_is_unsigned_val`, `_convert_int_value`, `_usual_arithmetic_conversion`, `_shift_operand_conversion`) constitute the only legitimate entry points for reading, writing, and converting it.

The cost of this choice has to be written down honestly: side-channel metadata is **optional and droppable**. In the wrapper scheme, "forgot to carry the type" is a type error the compiler rejects on its own; in the side-channel scheme, "forgot to apply the tag" passes silently, and the resulting IR is legal, executable, and bit-correct on most inputs — until some input makes `srem` and `urem` disagree. The implementation of `_tag_unsigned` even ships with a layer of silence of its own: it sets the attribute inside `try/except (AttributeError, TypeError)`, and if the attribute cannot be set, so be it. The safety net for the whole mechanism lives not in the type system but in the testing discipline of Section 4.4. This is a genuine engineering trade: incremental retrofittability and isomorphism with the existing architecture, bought by demoting "completeness" from a machine-checked property to a human-maintained invariant. [AGENTS.md](../../AGENTS.md) dedicates an entire section, "C Codegen Invariants — Signedness," and the debugging playbook dedicates three techniques (§10/§11/§12), precisely as the ongoing interest payment on that demotion.

## 4.2 Three Layers of Metadata: Value Tags, Binding Tags, Constant Values

Inside `CCodeGenerator`, signedness information exists in three forms, corresponding to three phases in a value's life.

### 4.2.1 Value Tags: Three Flavors

The first layer hangs on the IR value object itself, as three mutually independent tags:

- `_is_unsigned` (accessed via `_tag_unsigned`/`_clear_unsigned`/`_is_unsigned_val`): this integer value itself is interpreted as unsigned;
- `_pcc_unsigned_pointee` (via `_tag_unsigned_pointee`/`_is_unsigned_pointee`): this is a pointer, and **the value loaded through it** should be unsigned;
- `_pcc_unsigned_return` (via `_tag_unsigned_return`/`_is_unsigned_return`): this is a function or function pointer, and **the result of calling it** should be unsigned.

The existence of the latter two tags reveals the recursive shape of the problem: a pointer value has no signedness of its own, but it carries "the signedness of some future dereference"; a function pointer goes one level deeper, carrying "the signedness of some future call's result." The dereference path for `unsigned char *p` (the `*` branch of `codegen_UnaryOp`) checks `_is_unsigned_pointee` after `_safe_load` and applies `_tag_unsigned` to the result; array subscripting (`codegen_ArrayRef`) repeats the same gesture on every one of its exits — pointer subscript, array subscript, byte-offset fallback. This "every load site re-applies the tag independently" repetition is exactly where the fragility lives: add one new value-fetching path, and you have added one new place a tag can be missed.

### 4.2.2 Binding Tags: From Declaration to Use

The second layer hangs on storage bindings (allocas, globals, functions): `_mark_unsigned`/`_mark_unsigned_pointee`/`_mark_unsigned_return` prefer setting an attribute on the binding object, falling back to three sets initialized in `__init__` (`_unsigned_bindings` and friends) when the attribute cannot be set. Binding tags are born at declarations: `codegen_Decl`, after resolving the declared type, calls `_mark_unsigned(var_addr)` for unsigned scalars; the function-definition prologue (the `codegen_FuncDef` path) makes the same determination for each parameter, and additionally recognizes "parameter that is a pointer to an unsigned scalar" (`_has_unsigned_scalar_pointee`) and "function-pointer parameter whose callee returns unsigned" (`_func_decl_returns_unsigned`); function declarations and definitions apply `_mark_unsigned_return` to the function object itself.

The determination rests on `_is_unsigned_type_names()`: it first resolves typedefs along the `__typedef_` chain, then consults the frozen set `_UNSIGNED_TYPE_NAMES` — which, beyond sorted type-name combinations like `"int unsigned"` and `"long unsigned"`, explicitly enrolls `size_t` and `uint8_t`..`uint64_t`. This means `typedef unsigned char lu_byte;` (Lua's byte type) is recognized after a single hop of chain resolution.

The bridge from binding tags to value tags is `_propagate_binding_tags(result, var)` at the end of `codegen_ID`: it copies each of the three tags, flavor for flavor, from the binding onto the freshly loaded value. So the read chain for an `unsigned int x` is: at declaration, `_mark_unsigned(alloca)` → at use, load → `_propagate_binding_tags` → value carries `_is_unsigned` → value enters the expression.

### 4.2.3 The Other Birth Sites of Tags

Beyond declarations, value tags have five more birth sites, each corresponding to one rule in the C standard that says "the type of this expression is unsigned":

1. **Literals** (`codegen_Constant`): a `u`/`U` suffix makes the literal unsigned directly; hexadecimal and octal literals exceeding `0x7FFFFFFF` land as unsigned `i32` — a faithful replica of C's literal type ladder, where the decimal ladder skips unsigned types (overflowing into `i64`, still signed) while non-decimal literals pass through `unsigned int` on the way up.
2. **`sizeof` and `_Alignof`** (`_codegen_sizeof`/`_codegen_alignof`): the result is fixed as an `i64` constant and `_tag_unsigned`'d — `size_t` is always unsigned.
3. **Struct field access** (the `codegen_StructRef` family): the field layout object `StructFieldLayout` carries `is_unsigned` and `decl_type`, and the loaded value is tagged according to its declared type via `_tag_value_from_decl_type`; bit-fields go through `BitFieldRef`, whose `is_unsigned` determines both the extraction strategy (mask-and-truncate vs. `trunc`+`sext`) and the result tag — bit-fields are the junction of data layout and expression semantics, and Section 4.4 returns to that point.
4. **Function call results** (`_extend_call_result`): the result is tagged according to the `_is_unsigned_return` mark on the callee binding; both ordinary calls and function-pointer calls pass through this gate.
5. **Casts** (`codegen_Cast`): the target type name is judged by `_is_unsigned_type_names`, and for float→integer conversions it directly decides between `fptoui` and `fptosi`.

Inside `codegen_Cast` hides the single detail in this chapter most worth lingering over. When source and destination have the exact same IR type and only the signedness needs flipping — `(unsigned)x` where `x` is an `int` — the code does not retag in place. Instead, it emits an `add x, 0` to manufacture a **new value identity**, and tags the new value. The reason lies in where the metadata is stored: tags hang on Python-level IR value objects, and the same value object may already be held by other expressions. An in-place `_tag_unsigned(x)` would retroactively rewrite what `x` means in all code already generated and yet to be generated — one cast poisoning an entire function. The `add 0` is noise at the IR level (any optimizer deletes it in passing) but necessity at the metadata level: it splits "one bit pattern, two type interpretations" into two objects that can each carry their own tag. This is a small but profound tax that the side-channel scheme must pay.

The third metadata layer, `ConstIntValue`, belongs to compile-time constant evaluation and is deferred to Section 4.5.

## 4.3 The Usual Arithmetic Conversions: From Specification to Code

The C standard calls the type-unification rules that precede binary operations the usual arithmetic conversions (C11 6.3.1.8), with the integer promotions (6.3.1.1) as their prerequisite step. pcc lands these two layers on `_integer_promotion` and `_usual_arithmetic_conversion` respectively, with shifts routed separately through `_shift_operand_conversion`.

### 4.3.1 Integer Promotion: The Invariant Is Bidirectional

```python
def _integer_promotion(self, val):
    if not isinstance(getattr(val, "type", None), ir.IntType):
        return val
    if val.type.width == 1:
        return self._clear_unsigned(self.builder.zext(val, int32_t))
    if val.type.width < int32_t.width:
        return self._convert_int_value(val, int32_t, result_unsigned=False)
    return val
```

Note `result_unsigned=False`: after promotion, `unsigned char` and `unsigned short` are **signed** `int`. This is the standard's literal rule — as long as `int` can represent all values of the original type, the promotion target is `int`, not `unsigned int`. The test `test_unsigned_char_promotes_to_signed_int_for_compare` in [tests/c/test_unsigned_loads.py](../../tests/c/test_unsigned_loads.py) nails this behavior down: `reg >= nvarstack` with `int reg = -1` and `lu_byte nvarstack = 1` — both promote to signed `int`, and `-1 >= 1` is false. If pcc reasoned by surface intuition that "unsigned char is unsigned, so the comparison is unsigned too," `-1` would be reinterpreted as `0xFFFFFFFF` and the comparison would invert.

This reveals the full shape of this chapter's invariant: it is **bidirectional**. Losing an unsigned tag is a bug (the protagonist of Section 4.4), but holding an unsigned tag too long is equally a bug. The goal of signedness tracking is not "stay unsigned as much as possible"; it is **to reproduce, at every operation site, exactly the type the C standard specifies**.

One more detail: `i1` (a comparison result) promotes to `i32` with its tag cleared — in C, the result type of a relational operator is `int`, signed. A comment after the comparison branch of `codegen_BinaryOp` names its alignment target outright: "clang CodeGen: comparison results are i32 (C int)."

### 4.3.2 Three Branches: Rank Rules Collapsed onto Widths

The standard phrases its conversion rules in terms of "integer conversion rank" and "whether one type can represent all values of the other." pcc's implementation compares only widths:

```python
if lhs_unsigned == rhs_unsigned:
    target_type = lhs.type if lhs_width >= rhs_width else rhs.type
    result_unsigned = lhs_unsigned
elif lhs_unsigned:
    if lhs_width >= rhs_width:
        target_type, result_unsigned = lhs.type, True
    else:
        target_type, result_unsigned = rhs.type, False
else:
    ...  # symmetric branch
```

This is not corner-cutting; it is a legitimate collapse. Under pcc's type mapping (`char`=i8, `short`=i16, `int`=i32, `long`/`long long`=i64), the rank ordering of the standard integer types corresponds strictly monotonically to IR width, and "can the signed type represent all values of the unsigned type" is, under fixed-width two's complement, exactly equivalent to "is the signed type strictly wider." So the standard's three sentences — same signedness takes the higher rank; the unsigned side wins if its rank is not lower; the signed side wins if it can hold everything — translate precisely into three width branches.

[tests/c/test_unsigned_loads.py](../../tests/c/test_unsigned_loads.py) pins the two critical faces of this collapse with a mirrored pair of tests: `test_unsigned_int_converts_to_signed_long_when_long_can_hold_it` verifies that `unsigned int` (i32) meeting `long` (i64) converts toward **signed** long — `(long)-2 < (unsigned)1` is true; `test_size_t_still_uses_unsigned_comparison_at_same_rank` verifies that `size_t` (i64, unsigned) meeting `long` (i64) lets **unsigned** win — `-2` is reinterpreted as a huge positive number, and `x < u` is false. The same operand shape, one notch of width difference, and the semantics flip entirely. These two tests are themselves the best commentary ever written on 6.3.1.8.

Once the target type is unified, both operands pass through `_convert_int_value`. This helper's key contract: **when widening, choose `zext`/`sext` by the signedness of the *source*; tag the result by the semantics of the *destination***. C conversions are defined by value, and under two's complement "extend by the source's sign" is exactly what implements by-value conversion; meanwhile, what type the result *is* is dictated by the caller (the conversion rules). The two concerns must stay separate. `test_unsigned_char_return_is_zero_extended` guards the first half: a function returning `lu_byte` 200 must `zext` its return value into an `int` — had the extension been chosen by the destination (signed int) as `sext`, a 200 with its top bit set would have come out negative.

### 4.3.3 Shifts: A Separate Small Channel

C11 6.5.7 carves out an exception for shifts: **no usual arithmetic conversions**. Each side undergoes integer promotion independently, and the result type is the promoted type of the left operand. `_shift_operand_conversion` reproduces this faithfully: both sides promoted independently; the right operand converted to the left operand's width to satisfy LLVM's same-type requirement (preserving the right operand's *own* signedness during the conversion, so the extension direction stays correct); the returned signedness determined by the left side alone. Thus the signedness of `u >> 1` and of `1 >> u` is decided by each expression's own left operand, independent of the other side. The consumer sits in `codegen_BinaryOp`: `>>` selects `lshr` or `ashr` by `is_unsigned`, and an unsigned result must be `_tag_unsigned`'d — `test_unsigned_right_shift_result_stays_unsigned_for_modulo` verifies the "shift feeding remainder" chain `(x >> 31) % 2`.

### 4.3.4 Consumers: Operator Selection and the No-Wrap Stance

With conversions done, `codegen_BinaryOp` and `codegen_Assignment` (compound assignment) make the final instruction selection by `is_unsigned`: `/` and `%` choose `udiv/urem` or `sdiv/srem`, comparisons choose `icmp_unsigned` or `icmp_signed`, `>>` chooses `lshr` or `ashr`. The operators `+ - * & | ^ <<` need no instruction split, but **their results must be retagged** — they are precisely the family where "same bits, different type" holds, and therefore precisely where tags are most easily dropped.

The arithmetic branch of `codegen_BinaryOp` carries a stance comment worth transcribing: by default, integer operations get **no** `nsw`/`nuw` no-wrap flags — even for signed arithmetic, a frontend needs a proof of "this cannot wrap" before it has any right to attach `nsw`; otherwise LLVM is entitled to miscompile wrap-sensitive code under that license. `test_unsigned_long_long_subtraction_wraps_without_nuw` pins this from the behavioral side: `0 - 1000ULL` must wrap modularly to `0xfffffffffffffc18`. This shares a root with the IR Fix Policy of Chapter 12, which strips `nuw`/`nneg` and similar attributes at the text layer: pcc says no to every unproven optimization promise. It is also Chapter 1's obligation 2 ("performance must be proven") made concrete at the lowest possible layer.

One contrast in passing: C's `unsigned` wraparound is **language-defined semantics**, which pcc must reproduce exactly; whereas the Python frontend's `int` is an arbitrary-precision semantic type whose value projection (the tagged small-int lane) must deopt/promote on overflow and never wrap (see Chapter 16). One codebase, two opposite overflow contracts, each faithful to its own language — there is no better footnote to the "semantics before performance" position.

## 4.4 The Classic Failure Mode: Correct Bits, Lost Sign

We can now characterize this chapter's core failure mode in full. Its anatomy has three segments:

```text
producer        xor / shl / add / ++ / compound assignment / phi merge
                bit pattern correct (these ops are sign-blind under
   │            two's complement) — but _tag_unsigned(result) forgotten
   ▼
propagation     the value flows through temporaries, phis, assignments,
                with the tag absent the whole way
   ▼
consumer        % → srem   (should be urem)
                / → sdiv   (should be udiv)
                >> → ashr  (should be lshr)
                < → icmp_signed (should be icmp_unsigned)
```

What makes it vicious is the **conditionality** of the error: as long as the value's top bit is 0, signed and unsigned instructions agree. `(rnd ^ lo ^ up) % m` is correct year after year while `rnd` stays small — until some random seed pushes the XOR result into the high-bit region. Toy tests almost never hit it; real programs (Lua, libc-heavy code, control-flow-dense programs) use all 32 bits every day.

[AGENTS.md](../../AGENTS.md) compresses the defensive discipline into three questions that anyone adding or modifying an expression form must ask:

1. Does this expression produce an integer result?
2. If yes, should that result remain unsigned?
3. Will that result later feed `%`, `/`, `>>`, a comparison, or another arithmetic conversion?

The testing methodology behind the third question is the debugging playbook's §11, "downstream-sensitive regression tests": a good signedness test does not assert a constant — it **routes the result of an unsigned producer immediately into a sign-sensitive consumer, preferably with an ordinary signed constant on the other side**. The whole of [tests/c/test_unsigned_loads.py](../../tests/c/test_unsigned_loads.py) is a specimen library of that shape: XOR into remainder, compound assignment into remainder, prefix increment/decrement into remainder, right shift into remainder, ternary into remainder. The seemingly arbitrary constant in `% 960` is deliberate — 960 is a signed literal, so if the left side's tag is lost, the mixed-signedness branch of the usual arithmetic conversion drags the entire operation back into the signed world.

The playbook's §10 supplies the other half of the localization craft: facing a real-program failure, first split **data-layout hypotheses** from **expression-semantics hypotheses**. Layout hypotheses (`sizeof`, `offsetof`, fake-libc declarations, struct layout) can be falsified as an entire family with one probe program compared against a native compiler, at trivial cost; once falsified, the remaining suspects concentrate on expression semantics — signedness, promotion, comparison, shift, division. The case study in 4.6.1 shows this bisection cutting the search space in half in live combat. The two families intersect at exactly one place: bit-fields. `BitFieldRef` carries layout information (container type, bit offset, bit width) and signedness (`is_unsigned`) simultaneously; `_load_bitfield` takes mask-plus-truncate/zero-extend for unsigned bit-fields and `trunc` to bit width followed by `sext` for signed ones — a layout off by one bit and a sign off by one judgment produce nearly identical symptoms, and only a probe can tell them apart.

One corner deserves honest labeling as open: the merge rule in `codegen_TernaryOp`. The two arms are first converted to the wider type chosen by `pick_target_type`, and the phi node's signedness is taken as "unsigned if any incoming edge is unsigned" (`any(self._is_unsigned_val(...))`). With same-width arms this agrees with the standard (unsigned wins); but when "a wider signed arm meets a narrower unsigned arm," the standard requires a signed result (the wider type can hold everything), and the any() rule mistakenly tags the phi unsigned. The existing test `test_unsigned_ternary_result_stays_unsigned_for_modulo` covers only the same-width case; the mixed-width case currently has no regression test. This is one more instance of the side-channel scheme's "approximate rules scattered across merge points" — the complete lattice rule lives in `_usual_arithmetic_conversion`, while the phi merge uses a coarser approximation. Exercise 3 asks the reader to turn this corner into a runnable counterexample.

## 4.5 Constant Folding Is a Second Semantic Subsystem

Everything so far has been the runtime path: expressions become IR instructions, and signedness decides instruction selection. But C also requires the compiler to evaluate a whole class of constant expressions **at compile time**: enumerator values, array dimensions, bit-field widths, initializers, `case` labels, `_Static_assert` conditions. That path lives in `_eval_const_expr()`, and the debugging playbook's §12 classifies it as an **independent semantic subsystem**: runtime signedness can be entirely correct while compile-time folding is entirely wrong, because folding is a second implementation of the same rules.

The vehicle of `_eval_const_expr` is `ConstIntValue` — an `int` subclass carrying `width` and `is_unsigned`. Around it, every runtime helper has a compile-time twin:

```text
runtime (IR value + tag)              compile time (ConstIntValue)
_integer_promotion                    integer_promotion (width<32 → 32-bit signed)
_usual_arithmetic_conversion          usual_arithmetic_conversion (same three branches)
_convert_int_value                    convert_int_value / cast_int_value
codegen_Constant's literal ladder     parse_int_constant (line-for-line isomorphic)
udiv/urem vs sdiv/srem                raw_bits(a) // raw_bits(b) vs c_int_div
icmp_unsigned vs icmp_signed          raw_bits comparison vs signed int comparison
```

Two of these twins best demonstrate that "this is semantics, not arithmetic." First, `cast_int_value` masks to the target width after every conversion and folds back across the sign bit — Python's `int` is unbounded precision, so without deliberate folding nothing ever wraps, while C's unsigned wraparound semantics depend precisely on fixed width. Second, `c_int_div`/`c_int_mod` implement **truncation toward zero** by hand: Python's `//` rounds toward negative infinity, so `-7 // 2 == -4`, whereas C requires `-7 / 2 == -3`. Folding C expressions with the host language's bare operators smuggles host semantics into the target language — exactly the accident family §12 exists to prevent.

Unsigned semantics at the folding layer rest on `raw_bits()`: take the low `width` bits of a value under unsigned interpretation. So the folding chain for `(size_t)(~(size_t)0)` runs: `0` cast to `size_t` (width 64, unsigned) → `~` flipped through `raw_bits` and masked, yielding `2^64 - 1` rather than Python's intuitive `-1` → the subsequent `/ sizeof(t)` takes unsigned division → the comparison takes `raw_bits` comparison. Forget the width or the signedness at any link, and the entire constant is wrong.

The **double implementation** across the folding layer and the runtime layer is a synchronization cost that must be paid continuously: every signedness bug fix must ask whether it has a twin in the other layer. §12's exact words deserve a place in engineering memory: "if a real program fails on a 'simple constant', check `_eval_const_expr()` and the post-macro-expansion source before suspecting runtime IR."

## 4.6 History and Lessons

### 4.6.1 Lua sort.lua: One Untagged XOR Brought Down Quicksort

(Source: [docs/investigations/lua-sort-random-pivot-signedness.md](../../docs/investigations/lua-sort-random-pivot-signedness.md))

The symptom began with insulting vagueness: the `sort.lua` case in the Lua integration tests failed **occasionally** — pcc-compiled `onelua.c` exited nonzero while natively `cc`-compiled identical source passed. Same source, same Lua version, different compiler: suspicion landed on the compiler side immediately. But the failure depended on the random seed, and the surface evidence pointed in a crowd of alarming directions: stack corruption? aggregate-copy errors? struct layout drift? a comparator bug?

The first step of the investigation was not reading code; it was eliminating randomness (debugging playbook §1): fix `math.randomseed`, construct deterministically failing array shapes, and eventually shrink to "reverse-sorted input, custom comparator, minimal failing size around 1921." The second step bypassed Lua's test suite while keeping the real implementation: a C helper that does `#define main pcc_onelua_main` then `#include "onelua.c"`, constructing the reverse-sorted table directly and calling the internal `auxsort` — native passes, pcc fails deterministically, proving the bug independent of the test harness. The third step was §10's bisection: `sizeof`/`offsetof` probes compared against a native compiler showed `TValue`, `CallInfo`, `Table`, and the other critical structs all identical — **the entire family of layout hypotheses eliminated**; stack-shape probes around `luaL_makeseed` showed the stack intact; substituting the comparator and `partition` one at a time showed they were merely victims.

The fourth step, substitution (§6), narrowed the field to the random-pivot path: `auxsort` with randomization removed passed, small `rnd` values passed, large `rnd` values failed. Lua could then be removed from the scene entirely, leaving a pure C reproduction:

```c
typedef unsigned int IdxT;
static IdxT choosePivot(IdxT lo, IdxT up, unsigned int rnd) {
  IdxT r4 = (up - lo) / 4;
  IdxT p = (rnd ^ lo ^ up) % (r4 * 2) + (lo + r4);
  return p;
}
```

With `lo=1, up=1921, rnd=3426782842u`: native gives `p=731`, pcc gives `p=475`. The decisive reasoning came from the wrong value itself: the legal pivot interval is `[481, 1441]`, and 475 is **exactly 6 below** the lower bound — the fingerprint of a signed remainder. The bit pattern of `rnd ^ lo ^ up` has its top bit set; under unsigned remainder it yields the correct residue, but interpreted as signed, `srem` yields `-6`, and adding `lo + r4 = 481` gives exactly 475. The three-part anatomy stood fully exposed: the XOR computed the correct bits (the producer was innocent), the result of `builder.xor` was never retagged with `_tag_unsigned` (the tag was lost), and the downstream `%` therefore selected `srem` (the consumer was innocent).

With the root cause confirmed, the investigation did not stop at a point fix; it swept the adjacent expression forms under the "audit the whole family" principle and caught an independent second bug: the expression results of unsigned prefix `++`/`--` were not retagged (the value stored back to the variable was right; the tag on the expression value was lost). The final fix covered four paths — `^` results, unsigned `>>` results, integer compound-assignment results, and unsigned prefix increment/decrement results — with regression tests all taking the downstream-sensitive shape "unsigned producer `%` signed constant," settling into what is today the second half of [tests/c/test_unsigned_loads.py](../../tests/c/test_unsigned_loads.py).

The invariant this story left behind is now written into [AGENTS.md](../../AGENTS.md): **any expression node that manufactures a new integer IR value must explicitly answer "is this result signed or unsigned under C semantics."** Its methodological legacy matters just as much: the delta between the wrong value and the right one (475 versus 731 — a residue of `-6` versus `+954`) often spells out the name of the wrong instruction directly. Reading the wrong value is faster than reading a thousand lines of IR.

### 4.6.2 The Compile-Time Twin: `MAX_SIZET` Folds to `-1`

(Source: [docs/debugging-playbook.md](../../docs/debugging-playbook.md) §12 and the regression test `test_constexpr_cast_to_size_t_keeps_unsigned_range_in_ternary`; this lesson has no standalone investigation file, and the retelling below follows those two surviving artifacts.)

Section 4.6.1 fixed the runtime path; the same family of problem had a twin on the compile-time path. The playbook's §12 records its shape: the runtime unsigned comparison was already correct, but the compile-time folding of casts and ternaries ignored width and signedness — `((size_t)(~(size_t)0))` folded to `-1`, and the real project then compiled successfully carrying a wrong constant, failing somewhere far away.

This expression is no contrived stress case; it is `MAX_SIZET` from the Lua sources, participating through a macro chain in the size ceiling computation for Lua tables. The regression test preserves the macro chain intact:

```c
#define MAX_SIZET ((size_t)(~(size_t)0))
#define luaM_limitN(n,t) \
  ((cast_sizet(n) <= MAX_SIZET/sizeof(t)) ? (n) : cast_int((MAX_SIZET/sizeof(t))))
enum { MAXHSIZE = luaM_limitN(1 << MAXHBITS, Node) };
```

Folded by Python intuition, `~0` is `-1`, `-1 / sizeof(Node)` is still negative, `cast_sizet(n) <= negative` is false, the ternary picks the wrong branch, and the enumerator `MAXHSIZE` receives a truncated wrong value — and enumerator values are the entry point of the compile-time pipeline of Section 4.5, with no runtime check anywhere downstream to intercept them. Correct folding requires every step to carry `ConstIntValue`'s width and signedness: `~` through `raw_bits` yields `2^64-1`, the division takes unsigned bit-pattern division, the comparison takes `raw_bits` comparison — only then does the ternary pick the right branch and `MAXHSIZE == 1 << MAXHBITS` hold.

Taken together, the two stories cover exactly the two implementations of one semantics: 4.6.1 lost signedness at the IR instruction-selection layer; 4.6.2 lost width and signedness at the constant-folding layer. That is the full weight of §12's sentence — **when fixing a signedness bug, ask once where its compile-time/runtime twin is**. A compiler fixed only at runtime will carry the same bug right back in, dressed as an enumerator constant.

## 4.7 Summary

The whole of this chapter folds back into a three-layer structure. The bottom layer is an external fact: LLVM integers have no sign, `int` and `unsigned int` are both `i32`, and signedness exists only in instruction selection (`sdiv/udiv`, `srem/urem`, `ashr/lshr`, `icmp_signed/icmp_unsigned`, `sext/zext`, `sitofp/uitofp`). The middle layer is pcc's design response: side-channel metadata — value tags (`_is_unsigned`, plus the pointee and return flavors), binding tags (born at declarations, bridged by `_propagate_binding_tags`), and the compile-time `ConstIntValue`; conversion logic funneled through `_convert_int_value` (extend by source, tag by destination), `_integer_promotion` (everything narrower than int promotes to **signed** int), `_usual_arithmetic_conversion` (C's rank rules collapsing to three width branches under the fixed-width two's-complement mapping), and `_shift_operand_conversion` (shifts skip unification; the result follows the left operand). The top layer is the discipline paid for the design's inherent weakness — tags can be lost silently: every manufactured integer value must answer the three questions; regression tests must be downstream-sensitive (unsigned producer fed straight into `%`/`>>`/comparison); layout hypotheses get bisected from expression-semantics hypotheses first; every fix self-checks for its compile-time twin. Lua's 475 and `MAX_SIZET`'s `-1` are this discipline's two birth certificates: the first proves that one lost tag suffices to knock over a real interpreter, the second proves that one semantics implemented twice must be fixed twice.

## Exercises

1. **Read the source and verify.** In `codegen_BinaryOp` of [pcc/frontends/c/codegen/c_codegen.py](../../pcc/frontends/c/codegen/c_codegen.py), trace the complete lowering path of the expression `(x ^ y) % m` (with `x`, `y` of type `unsigned int` and `m` of type `int`): on which line is the result of `^` tagged? Which branch of `_usual_arithmetic_conversion` runs before `%`, and what is `result_unsigned`? Which condition ultimately selects `urem`? Then read `test_unsigned_xor_result_stays_unsigned_for_modulo` and explain the intent behind writing `% 960` rather than `% 960u`.
2. **The bidirectional invariant.** `_integer_promotion` passes a fixed `result_unsigned=False` for integers narrower than 32 bits. Suppose someone "fixes" it to preserve source signedness (`unsigned char` stays unsigned after promotion): which test in [tests/c/test_unsigned_loads.py](../../tests/c/test_unsigned_loads.py) fails immediately? Write out, for both the correct implementation and the "fixed" one, the post-promotion types of the two comparison operands in that test and the comparison instruction selected.
3. **Demonstrate the open corner.** Section 4.4 notes that the phi merge in `codegen_TernaryOp` approximates signedness with an any() rule. Construct a minimal C program in which the result of a ternary with a "wider signed arm + narrower unsigned arm" flows into a sign-sensitive consumer; work the result by hand under the C standard and under the any() rule; explain why the existing test `test_unsigned_ternary_result_stays_unsigned_for_modulo` cannot catch it; and write (on paper) a downstream-sensitive regression test for it in the shape of §11.
4. **Walk the compile-time twin.** Without running any code, fold `((size_t)(~(size_t)0)) / sizeof(Node)` (taking `sizeof(Node) == 16`) by hand twice — once under `_eval_const_expr`'s `ConstIntValue` semantics and once under naive "plain Python int" semantics — and determine which branch of the `luaM_limitN` ternary each chooses. Then explain why `c_int_div` cannot be written as Python's `//`, giving a concrete constant expression on which the two disagree.
5. **Argue the design trade.** Compare "side-channel metadata + helper discipline" (pcc) against "every expression value mandatorily carries its full C type": the gains and losses of each in retrofit cost, detectability of missing tags, and coupling to the llvmlite builder. Then design a mechanical check that narrows pcc's weakness — for example, an IR post-hoc pass that scans every `sdiv/srem/ashr/icmp_signed` and warns when an operand carries the `_is_unsigned` tag — and argue which of the two case studies in Section 4.6 it would and would not have caught, and why.
