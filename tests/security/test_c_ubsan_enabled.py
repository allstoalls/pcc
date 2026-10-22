"""SEC-P1-UBSAN: the opt-in ``-fsanitize=undefined`` trap path, ENABLED.

This is the companion to ``test_c_ubsan_characterization.py``. That file pins
pcc's *un-instrumented* lowering (no UBSan guard emitted, flag OFF). This file
turns the opt-in flag ON and asserts the exact *opposite* for the four
arithmetic UB classes ``-fsanitize=undefined`` covers — the "trap now emitted"
gate described in ``docs/design/pcc-ubsan.md`` §6.

CLAIM BOUNDARY / MODE LABEL
---------------------------
What is proven here (and only here):

  * LLVM-backed **trap mode** (``-fsanitize-trap=undefined``): a check +
    ``llvm.trap`` (``brk`` on AArch64, ``ud2`` on x86_64) is emitted for
    signed overflow, division overflow / by zero, and out-of-range / negative
    shift — across the direct, compound-assignment, and SSA lowering paths.
  * The flag is genuinely OPT-IN: with it OFF the emitted IR is unchanged
    (that inverse is pinned by the characterization file, still green).
  * ``-fsanitize=implicit-conversion`` (signed->unsigned truncation) is NOT in
    the ``undefined`` set, so a truncation stays un-guarded even with the flag
    on.

Not proven here: handler mode / ``libubsan`` ABI (deferred — not
self-backend-safe, see design §4.2), and full behavioural in-process execution
of the trap (a ``brk`` in the JIT worker would kill the pytest process, so this
file inspects the *emitted* IR/asm — the arch-independent structural signal —
rather than running the trapping ``main``, matching the characterization file's
own arch-hazard note).

Reference behaviour (Clang ``cc -fsanitize=undefined -fsanitize-trap=undefined``
on this host): div-by-zero / ``INT_MIN / -1`` / oversized shift / negative
shift / ``INT_MAX + 1`` all abort with SIGTRAP (exit 133) and emit a ``brk``;
the truncation ``(unsigned char)-1LL`` does not trap (exit 0). Confirmed with
system ``cc`` while authoring this test.
"""
from __future__ import annotations

import os
import re
import sys

from tests.owned_ir_validation import verify_ir_text

this_dir = os.path.dirname(__file__)
repo_root = os.path.dirname(os.path.dirname(this_dir))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from pcc.frontends.c.codegen.c_codegen import CCodeGenerator, postprocess_ir_text
from pcc.frontends.c.parse import make_c_parser

# Same structural markers the characterization file greps for. A trap
# instruction (or overflow intrinsic) appearing here is the deliberate flip.
_OVERFLOW_INTRINSIC_RE = re.compile(r"llvm\.(?:s|u)(?:add|sub|mul)\.with\.overflow")
# Both accepted IR spellings denote the same trap instruction.
_LLVM_TRAP_RE = re.compile(r"call void (?:\(\) )?@llvm\.trap\b")
_TRAP_ASM_RE = re.compile(r"\b(?:brk\b|ud1\b|ud2\b|\.trap\b)")


def _codegen(source: str, *, fsanitize):
    """Lower ``source`` with the given opt-in ``-fsanitize`` set (no execution).

    Returns ``(ir_text, asm_text)``. We drive the codegen directly rather than
    ``CEvaluator.evaluate`` so the trapping ``main`` is never run in-process.
    """
    from pcc.backend.self_backend_dispatch import emit_self_asm
    from pcc.frontends.python.pipeline_targets import host_target_triple
    ast = make_c_parser().parse(source)
    cg = CCodeGenerator()
    triple = host_target_triple()
    cg.set_target_text(triple, "")
    if fsanitize:
        cg.configure_ubsan(fsanitize, mode="trap")
    cg.generate_code(ast)
    ir_text = postprocess_ir_text(str(cg.module))
    verify_ir_text(ir_text)
    asm_text = emit_self_asm(ir_text, triple)
    return ir_text, asm_text


def _assert_trap_emitted(source: str, *, what: str):
    """A trap guard appears with the flag ON but not with it OFF (same source)."""
    ir_off, asm_off = _codegen(source, fsanitize=None)
    assert not _LLVM_TRAP_RE.search(ir_off), (
        f"{what}: llvm.trap present with -fsanitize OFF — the guard is not "
        f"opt-in (it must be inert by default)"
    )
    assert not _TRAP_ASM_RE.search(asm_off), (
        f"{what}: trap instruction present in asm with -fsanitize OFF"
    )

    ir_on, asm_on = _codegen(source, fsanitize=["undefined"])
    assert _LLVM_TRAP_RE.search(ir_on), (
        f"{what}: no llvm.trap emitted with -fsanitize=undefined ON — the "
        f"UB guard was not inserted on this lowering path"
    )
    assert _TRAP_ASM_RE.search(asm_on), (
        f"{what}: no trap instruction (brk/ud2) in emitted asm with "
        f"-fsanitize=undefined ON"
    )


# ---------------------------------------------------------------------------
# 1. Signed integer overflow: INT_MAX + 1  (uses llvm.sadd.with.overflow)
# ---------------------------------------------------------------------------

def test_signed_add_overflow_traps_when_enabled():
    source = r"""
    int main(void){
        volatile int a = 2147483647;   /* INT_MAX */
        volatile int b = 1;
        int c = a + b;
        return c;
    }
    """
    _assert_trap_emitted(source, what="signed add overflow")
    # The overflow leg reuses the checked intrinsic; confirm it is now present.
    ir_on, _ = _codegen(source, fsanitize=["undefined"])
    assert _OVERFLOW_INTRINSIC_RE.search(ir_on), (
        "signed add overflow: expected an llvm.sadd.with.overflow guard"
    )


def test_signed_mul_overflow_traps_when_enabled():
    source = r"""
    int main(void){
        volatile int a = 100000;
        volatile int b = 100000;
        int c = a * b;
        return c;
    }
    """
    _assert_trap_emitted(source, what="signed mul overflow")


# ---------------------------------------------------------------------------
# 2/3. Division overflow (INT_MIN / -1) and division by zero
# ---------------------------------------------------------------------------

def test_intmin_div_minus_one_traps_when_enabled():
    source = r"""
    int main(void){
        volatile int a = -2147483647 - 1;  /* INT_MIN */
        volatile int b = -1;
        int c = a / b;
        return c;
    }
    """
    _assert_trap_emitted(source, what="INT_MIN / -1 overflow")


def test_div_by_zero_traps_when_enabled():
    source = r"""
    int main(void){
        volatile int a = 1;
        volatile int b = 0;
        int c = a / b;
        return c;
    }
    """
    _assert_trap_emitted(source, what="divide by zero")


def test_mod_by_zero_traps_when_enabled():
    source = r"""
    int main(void){
        volatile int a = 7;
        volatile int b = 0;
        int c = a % b;
        return c;
    }
    """
    _assert_trap_emitted(source, what="modulo by zero")


# ---------------------------------------------------------------------------
# 4/5. Out-of-range and negative shift amounts
# ---------------------------------------------------------------------------

def test_oob_left_shift_traps_when_enabled():
    source = r"""
    int main(void){
        volatile int x = 1;
        volatile int s = 40;   /* >= 32 */
        int c = x << s;
        return c;
    }
    """
    _assert_trap_emitted(source, what="oversized left shift")


def test_negative_left_shift_traps_when_enabled():
    source = r"""
    int main(void){
        volatile int x = 1;
        volatile int s = -1;   /* negative shift */
        int c = x << s;
        return c;
    }
    """
    _assert_trap_emitted(source, what="negative left shift")


def test_right_shift_out_of_range_traps_when_enabled():
    source = r"""
    int main(void){
        volatile int x = 1024;
        volatile int s = 99;
        int c = x >> s;
        return c;
    }
    """
    _assert_trap_emitted(source, what="oversized right shift")


# ---------------------------------------------------------------------------
# Multiple lowering PATHS (six-path hazard, docs/design/pcc-ubsan.md §7):
# the guard must reach the compound-assignment path too, not only `a / b`.
# ---------------------------------------------------------------------------

def test_compound_div_assign_traps_when_enabled():
    source = r"""
    int main(void){
        volatile int a = 1;
        volatile int b = 0;
        a /= b;      /* compound-assignment dispatch path */
        return a;
    }
    """
    _assert_trap_emitted(source, what="compound /= by zero")


def test_compound_shift_assign_traps_when_enabled():
    source = r"""
    int main(void){
        volatile int x = 1;
        volatile int s = 40;
        x <<= s;     /* compound-assignment dispatch path */
        return x;
    }
    """
    _assert_trap_emitted(source, what="compound <<= out of range")


# ---------------------------------------------------------------------------
# 6. Signed -> unsigned truncation stays un-guarded (NOT in `undefined`).
# ---------------------------------------------------------------------------

def test_truncation_not_trapped_by_undefined_set():
    # -fsanitize=undefined must NOT trap an implementation-defined narrowing;
    # that belongs to the separate -fsanitize=implicit-conversion group.
    source = r"""
    int main(void){
        volatile long long b = -1;
        unsigned char n = (unsigned char)b;   /* 0xFF, impl-defined not UB */
        return n;
    }
    """
    ir_on, asm_on = _codegen(source, fsanitize=["undefined"])
    assert not _LLVM_TRAP_RE.search(ir_on), (
        "truncation: -fsanitize=undefined must not guard an "
        "implementation-defined narrowing conversion"
    )
    assert not _TRAP_ASM_RE.search(asm_on), (
        "truncation: unexpected trap instruction for a narrowing conversion"
    )


# ---------------------------------------------------------------------------
# Opt-in scoping: a single-group request only guards that group.
# ---------------------------------------------------------------------------

def test_only_divide_group_guards_only_division():
    # `integer-divide-by-zero` alone must guard `/ 0` but NOT signed add
    # overflow (which needs `signed-integer-overflow`).
    div_src = r"""
    int main(void){
        volatile int a = 1;
        volatile int b = 0;
        int c = a / b;
        return c;
    }
    """
    add_src = r"""
    int main(void){
        volatile int a = 2147483647;
        volatile int b = 1;
        int c = a + b;
        return c;
    }
    """
    div_ir, _ = _codegen(div_src, fsanitize=["integer-divide-by-zero"])
    add_ir, _ = _codegen(add_src, fsanitize=["integer-divide-by-zero"])
    assert _LLVM_TRAP_RE.search(div_ir), (
        "integer-divide-by-zero should guard `/ 0`"
    )
    assert not _LLVM_TRAP_RE.search(add_ir), (
        "integer-divide-by-zero must NOT guard signed add overflow"
    )


def test_unsigned_division_not_guarded_for_overflow():
    # Unsigned division cannot overflow; only its zero-divisor is UB. With the
    # overflow group alone (not the divide group), no guard should appear.
    source = r"""
    int main(void){
        volatile unsigned int a = 5u;
        volatile unsigned int b = 2u;
        unsigned int c = a / b;
        return (int)c;
    }
    """
    ir_on, _ = _codegen(source, fsanitize=["signed-integer-overflow"])
    assert not _LLVM_TRAP_RE.search(ir_on), (
        "unsigned division must not get a signed-overflow guard"
    )
