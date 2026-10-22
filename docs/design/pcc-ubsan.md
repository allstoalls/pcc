# Design: opt-in `-fsanitize=undefined`-style UB trapping for the C frontend

**Task:** SEC-P1-UBSAN
**Status of this document:** design + characterization slice only. **No UB
trapping is implemented in this slice; none is claimed.** The instrumentation
pass described below is a *later* task that will own edits to
`pcc/frontends/c/codegen/c_codegen.py`; this document specifies its contract in advance so
the characterization tests written in the same slice have a precise
"trap emitted" gate to flip against.

---

## 1. Motivation and claim boundary

Source: *Low-Level Software Security for Compiler Developers* — undefined
behavior in C arithmetic is the substrate of a large class of memory-safety
primitives (a size computation that silently wraps, an index shift that becomes
UB and is then folded to something the programmer never wrote, a divisor that
is attacker-controlled and zero). Clang/GCC mitigate this with the opt-in
`-fsanitize=undefined` (UBSan) family, which inserts an explicit check +
diagnostic-or-trap at each UB site.

pcc today emits **no** such guard. The C frontend lowers signed overflow to a
plain `add`, `INT_MIN / -1` and `x / 0` to a bare `sdiv`, and out-of-range /
negative shifts to a bare `shl` — the observable result is whatever the target
hardware does (AArch64 wraps / masks / returns 0; x86_64 `idiv` raises
`#DE` → SIGFPE). This is pinned, in executable form, by
[`tests/security/test_c_ubsan_characterization.py`](../../tests/security/test_c_ubsan_characterization.py).

**Explicit claim boundary.** This slice delivers (1) that characterization and
(2) this design. It does **not** implement, enable, or claim any UB trapping.
"pcc has a UBSan mode" is a claim that may only be made after the
implementation slice lands the pass described in §4, wires the flag in §5, and
flips the gate in §6.

---

## 2. UB classes in scope (and their UBSan check kind)

| # | UB class | Example | UBSan handler (Clang ABI) | LLVM primitive used to detect |
|---|----------|---------|---------------------------|-------------------------------|
| 1 | Signed integer overflow | `INT_MAX + 1`, `a*b`, `-INT_MIN` | `__ubsan_handle_add/sub/mul/negate_overflow` | `llvm.sadd/ssub/smul.with.overflow.iN` |
| 2 | Division overflow | `INT_MIN / -1`, `INT_MIN % -1` | `__ubsan_handle_divrem_overflow` | compare `lhs == INT_MIN && rhs == -1` |
| 3 | Division by zero | `x / 0`, `x % 0` | `__ubsan_handle_divrem_overflow` | compare `rhs == 0` |
| 4 | Out-of-range / negative shift | `x << 40`, `x << -1`, `x >> 32` | `__ubsan_handle_shift_out_of_bounds` | compare `(unsigned)amt >= bitwidth` |
| 5 | Pointer arithmetic overflow | `p + huge`, `&a[i]` past object | `__ubsan_handle_pointer_overflow` | `llvm.uadd/usub.with.overflow` on the `ptrtoint` |

A sixth, **signed→unsigned / narrowing truncation**, is *implementation-defined*
in C (not UB) and corresponds to Clang's separate `-fsanitize=implicit-conversion`
group, not `-fsanitize=undefined`. It is characterized in the test file (pinned
as "no conversion guard emitted") because a future truncation check is the
natural neighbor of this pass, but it is **out of scope** for the
`-fsanitize=undefined` implementation and must not be conflated with it.

Two lowering styles are supported by the pass and both must be covered — see
the [six division lowering paths](../../CLAUDE.md) lesson: the direct
`codegen_BinaryOp` path *and* the SSA path *and* the compound-assignment
dispatch each emit division/shift independently, so instrumentation that touches
only one leaves the others silently un-guarded.

---

## 3. Where pcc emits these operations today (insertion sites)

All line numbers are approximate anchors in `pcc/frontends/c/codegen/c_codegen.py` at the
time of writing; the implementer must re-grep, since the file moves.

* **Direct expression path** — `codegen_BinaryOp` (~L6957):
  * `/` `%` → `self.builder.sdiv` / `srem` / `udiv` / `urem` (~L7002–7076)
  * `<<` `>>` → `self.builder.shl` / `lshr` / `ashr` (~L7120–7129)
  * `+` `-` `*` → the plain `add`/`sub`/`mul` builder calls in the same method
* **SSA path** — `_lower_ssa_binop` (~L1674):
  * `/` `%` → `udiv`/`sdiv`/`urem`/`srem` (~L1806–1808)
  * `<<` `>>` → `shl`/`lshr`/`ashr` (~L1823–1825)
* **Compound assignment dispatch** — the operator→builder table (~L3504–3544)
  binds `/=`, `%=`, `<<=`, `>>=` to the same builder methods; these route
  through a shared helper but must be audited so the guard is not bypassed.

**Reusable primitives that already exist** (the pass should call these, not
re-derive them):

* `self._get_or_declare_intrinsic("llvm.trap", ir.VoidType(), [])` and the
  terminate-with-`unreachable` idiom in `_codegen_builtin_trap` (~L8357). This
  is the exact trap-then-dead-block shape a `-fsanitize=undefined -fsanitize-trap`
  guard needs.
* The `llvm.{s,u}{add,sub,mul}.with.overflow.iN` declaration + `extract_value`
  pattern already implemented for `__builtin_*_overflow` (~L8620–8643). The
  overflow-check leg of UB class #1 and #5 is *already written here*; the pass
  wraps it in a branch instead of returning the flag to the user.
* `_is_unsigned_val` / `_tag_unsigned` / `_clear_unsigned` for the signedness
  metadata that decides `sadd` vs `uadd` and `sdiv`-overflow vs none. Unsigned
  division/shift-by-large is *not* UB for the wrap, but unsigned shift ≥ width
  still is → the shift check keys off width, not signedness.

---

## 4. The instrumentation pass

### 4.1 Shape

A guard is a *check → branch → (trap | continue)* triad inserted immediately
before the offending builder call, gated by a per-compilation flag
(`self._ubsan_enabled`, default `False`). Pseudocode for the shared helper the
pass would add (e.g. `_maybe_ubsan_guard_div`, `_maybe_ubsan_guard_shift`,
`_maybe_ubsan_guard_arith`):

```text
def _maybe_ubsan_guard_div(self, lhs, rhs, signed):
    if not self._ubsan_enabled:
        return                      # OFF by default -> no code change
    # class #3: divisor == 0
    is_zero = builder.icmp_signed('==', rhs, Constant(ty, 0))
    cond = is_zero
    if signed:
        # class #2: lhs == INT_MIN && rhs == -1
        is_min  = builder.icmp_signed('==', lhs, Constant(ty, INT_MIN))
        is_neg1 = builder.icmp_signed('==', rhs, Constant(ty, -1))
        cond = builder.or_(is_zero, builder.and_(is_min, is_neg1))
    self._emit_ubsan_branch(cond, kind="divrem_overflow")
```

`_emit_ubsan_branch(cond, kind)` splits the current block: on `cond` true, jump
to a fail block; otherwise fall through to a `cont` block and continue lowering
the real `sdiv`/`shl`/etc. into `cont`.

### 4.2 Two fail-block modes (mirror Clang)

* **trap mode** (`-fsanitize-trap=undefined`, the default we should ship first
  because it needs no runtime): the fail block calls `llvm.trap` and then
  `unreachable`, reusing the `_codegen_builtin_trap` idiom verbatim. This is
  self-contained, needs no `libubsan`, and is compatible with `--backend self`
  (the self backend can lower `llvm.trap` to `brk #1` / `ud2`; it cannot parse
  an external handler call, so trap mode is the only self-backend-safe mode —
  same constraint that killed the inline-asm barrier in
  `test_c_stack_protection.py`).
* **handler mode** (full UBSan ABI): the fail block calls
  `__ubsan_handle_<kind>(&static_data, lhs, rhs)` where `static_data` is a
  `{ SourceLocation, TypeDescriptor }` constant. This is a follow-on; it pulls
  in `libubsan` and is **not** self-backend-safe. Defer until trap mode is
  proven.

### 4.3 Overflow classes reuse existing intrinsics

For class #1 (`+ - *`) and #5 (pointer add/sub), the check *is* the
`llvm.{s,u}{add,sub,mul}.with.overflow` call already present at ~L8620: call it,
`extract_value` the `i1` flag, branch on it. No new detection logic — only the
branch wrapper and the fail block are new.

### 4.4 Ordering with the optimizer

Guards are inserted at lowering time (before `_apply_llvm_optimizations`), so
LLVM can fold provably-safe checks away (e.g. a shift by a constant `3`). This
is desirable: the guard is semantically a no-op when the optimizer can prove no
UB, so `-O2` UBSan code keeps only the *unprovable* checks. The characterization
tests inspect **pre-opt** IR (`temp.ir`) precisely so the pin does not depend on
this folding.

---

## 5. Staying OFF by default

* New CLI/`build()` flag `--fsanitize=undefined` (accepting the comma-separated
  Clang-style list; only `undefined`, `signed-integer-overflow`,
  `integer-divide-by-zero`, `shift`, `pointer-overflow` recognized initially)
  plus `--fsanitize-trap=undefined` to select trap vs handler mode.
* Threaded to `CGen` as `self._ubsan_enabled` / `self._ubsan_mode`, default
  `False` / `"trap"`. When `False`, every `_maybe_ubsan_guard_*` helper returns
  immediately — **byte-for-byte identical lowering to today**. This is the
  property the current characterization tests assert, and it must survive the
  implementation slice for every case where the flag is off.
* No environment-variable auto-enable, no per-package special case. The pass is
  inert unless the flag is explicitly passed, consistent with pcc's "no silent
  mode change" discipline.

---

## 6. The gate that flips the characterization tests

The characterization file exposes exactly one assertion helper,
`_assert_no_ubsan_guard(src, ..., what=...)`, which fails if any of
`__ubsan_handle_*`, `llvm.*.with.overflow`, or a trap mnemonic
(`brk`/`ud1`/`ud2`/`.trap`) appears in the emitted IR/asm.

The implementation slice's acceptance gate is:

1. **Default (flag off):** the existing
   `tests/security/test_c_ubsan_characterization.py` must still pass unchanged —
   proof the pass is inert by default.
2. **Flag on:** a *new* companion test (added by the implementation slice, e.g.
   `test_c_ubsan_enabled.py`) compiles the same six sources with
   `--fsanitize=undefined --fsanitize-trap=undefined` and asserts the *opposite*:
   a trap (`brk`/`ud2`) or handler call now appears for classes #1–#5, and the
   behavioural cases now trap instead of wrapping. The truncation case (#6)
   stays un-guarded (it is not in `undefined`).
3. **Self-backend safety:** trap mode compiles and runs under `--backend self`
   without an unparseable external call (regression-guard against reintroducing
   a handler call on the self path).

Only when (1)–(3) are green may the claim "pcc has an opt-in UBSan trap mode"
be made — and it must still be mode-labeled (`--fsanitize=undefined` on;
LLVM-backed trap mode vs self-backed trap mode; trap mode vs handler mode are
distinct claims).

---

## 7. Risks / notes

* **Arch-dependence of the *baseline*.** The characterization's behavioural pins
  (wrap value, div-by-zero → 0) are AArch64-only because x86_64 traps in
  hardware; the structural IR/asm pins are arch-independent. The UBSan *pass*
  removes this divergence (both ISAs trap when enabled), which is the point —
  but the implementer must not assume the current AArch64 no-trap behavior is
  the baseline on x86_64.
* **Do not weaken the wrap semantics.** Unsigned overflow is well-defined
  modular arithmetic and must **never** be guarded by the `undefined` set (only
  the separate, off-by-default `unsigned-integer-overflow` sanitizer touches it).
  The pass must key the overflow check on signedness metadata, or it will trap
  correct code.
* **Six-path hazard.** Division/shift lowering has multiple independent code
  paths (direct, SSA, compound-assign). A guard added to only one path produces
  a *false sense of coverage*; the implementation gate in §6 step 2 must cover
  each path (e.g. `a/b`, `a/=b`, and an SSA-scaffold form).
* **IR Fix Policy.** These guards are real codegen (branches + intrinsic calls
  emitted by the IR builder), **not** `postprocess_ir_text()` rewrites. Do not
  synthesize UBSan checks by text-munging serialized IR.
* **This slice claims nothing runtime.** Until §6 is green, the only truthful
  statement is: *pcc's current C lowering inserts no UB trap, this is pinned by
  characterization, and a design exists to add an opt-in one.*
