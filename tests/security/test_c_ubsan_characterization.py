"""Characterization of pcc's *un-instrumented* undefined-behavior lowering (C).

Source: *Low-Level Software Security for Compiler Developers* — the four
arithmetic UB classes that ``-fsanitize=undefined`` (UBSan) exists to trap:
signed integer overflow, division overflow (``INT_MIN / -1``), division by
zero, and out-of-range / negative shift amounts. A related class, signed->
unsigned truncation, is *implementation-defined* rather than UB but is pinned
here too because a future ``-fsanitize=undefined`` pass is the obvious place a
truncation-conversion check would live.

WHAT THIS FILE IS
-----------------
This is a **characterization** file for the SEC-P1-UBSAN task, NOT a set of
xfails and NOT a demand that pcc trap anything. It PINS the *current* no-guard
lowering so that:

  1. the exact gap ("no UBSan trap is emitted today") is documented in an
     executable, greppable form; and
  2. when the later SEC-P1-UBSAN slice adds an opt-in ``-fsanitize=undefined``
     pass to ``pcc/frontends/c/codegen/c_codegen.py``, these ``_assert_no_ubsan_guard``
     checks *fail* precisely for the instrumented cases — that flip is the
     measurable "trap now emitted" gate described in ``docs/design/pcc-ubsan.md``.

CLAIM BOUNDARY
--------------
pcc emits **no** UB trapping today. Nothing here claims a mitigation exists;
these tests assert the *absence* of one and pin the resulting bare-metal
arithmetic so a regression that starts *miscomputing* (rather than merely not
trapping) is still caught.

INSPECTION STYLE
----------------
Two complementary views, matching the existing security suite:

  * ``_ev(src)`` runs ``main`` in the JIT and returns its exit code — the
    behavioural pin (same idiom as ``test_c_integer_safety`` /
    ``test_c_division_trap``). Cases are written so ``0`` == "invariant held".
  * ``_emit_ir`` / ``_emit_asm`` expose the pre-opt LLVM IR (``temp.ir``) and
    the target assembly (``temp.bcode``) via ``llvmdump=True`` — the *structural*
    pin that no ``__ubsan_handle_*`` call, ``llvm.*.with.overflow`` intrinsic,
    or trap (``brk``/``ud1``/``ud2``/``.trap``) is present. This is the check
    a future UBSan pass will flip.

ARCH DEPENDENCE (important)
---------------------------
The *behavioural* pins are hardware-dependent and therefore AArch64-gated where
they would otherwise trap on x86_64:

  * signed overflow / ``INT_MIN / -1``: AArch64 wraps (two's-complement /
    ``sdiv`` yields ``INT_MIN``); x86_64 ``idiv`` raises ``#DE`` -> SIGFPE.
  * division by zero: AArch64 ``sdiv`` yields 0; x86_64 raises SIGFPE.
  * out-of-range / negative shift: the *result value* is unspecified across
    ISAs (AArch64 masks the shift amount mod register width; the C-level result
    is UB either way), so we do NOT pin an exact value — only that no shift-range
    guard was inserted, which is arch-independent.

The *structural* IR/asm pins (no ubsan handler / no overflow intrinsic / no
trap) are arch-independent: they describe what pcc's codegen emitted before the
backend chose instructions, so they run everywhere.
"""
from __future__ import annotations

import os
import platform
import re
import sys

import pytest

this_dir = os.path.dirname(__file__)
repo_root = os.path.dirname(os.path.dirname(this_dir))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator

_IS_AARCH64 = platform.machine().lower() in ("arm64", "aarch64")
_aarch64_only = pytest.mark.pcc_gate(
    unavailable=None
    if _IS_AARCH64
    else "arithmetic UB traps (SIGFPE) on x86_64; value pinned only on AArch64"
)


# --- Structural markers a UBSan pass would introduce -----------------------
#
# If any of these appears, pcc has started instrumenting UB and the
# characterization must be revisited (see docs/design/pcc-ubsan.md gate).
#
# UBSan runtime handler symbols (Clang's -fsanitize=undefined ABI).
_UBSAN_HANDLER_RE = re.compile(r"__ubsan_handle_\w+")
# Checked-arithmetic intrinsics a trap-on-overflow lowering would emit.
_OVERFLOW_INTRINSIC_RE = re.compile(
    r"llvm\.(?:s|u)(?:add|sub|mul)\.with\.overflow"
)
# Explicit trap lowerings (`__builtin_trap` / `llvm.trap` -> these mnemonics).
#   AArch64: brk #1  |  x86_64: ud1/ud2  |  generic asm text: .trap / trap
_TRAP_ASM_RE = re.compile(r"\b(?:brk\b|ud1\b|ud2\b|\.trap\b)")


def _ev(source: str) -> int:
    return CEvaluator().evaluate(source, optimize=False)


def _emit_ir(source: str, monkeypatch, tmp_path) -> str:
    """Pre-optimization LLVM IR text (``temp.ir``) for ``source``.

    We inspect the *unoptimized* IR because a UBSan pass inserts its checks at
    lowering time; asserting on pre-opt IR keeps the pin independent of what the
    optimizer later folds away.
    """
    monkeypatch.chdir(tmp_path)
    CEvaluator().evaluate(source, optimize=False, llvmdump=tmp_path)
    return (tmp_path / "temp.ir").read_text(encoding="utf-8")


def _emit_asm(source: str, monkeypatch, tmp_path) -> str:
    """Emitted target assembly (``temp.bcode``) for ``source``."""
    monkeypatch.chdir(tmp_path)
    CEvaluator().evaluate(source, optimize=False, llvmdump=tmp_path)
    return (tmp_path / "temp.bcode").read_text(encoding="utf-8")


def _assert_no_ubsan_guard(source: str, monkeypatch, tmp_path, *, what: str) -> None:
    """Pin that ``source`` lowers with NO UBSan-style guard, in IR *and* asm.

    This is the assertion the future SEC-P1-UBSAN pass is expected to flip: once
    ``-fsanitize=undefined`` is wired in and enabled for these cases, a handler
    call / overflow intrinsic / trap WILL appear and this call will fail — that
    failure is the "trap now emitted" signal, not a regression.
    """
    ir = _emit_ir(source, monkeypatch, tmp_path)
    assert not _UBSAN_HANDLER_RE.search(ir), (
        f"{what}: unexpected __ubsan_handle_* call in IR — UB instrumentation "
        f"appears to have been added; update the SEC-P1-UBSAN characterization"
    )
    assert not _OVERFLOW_INTRINSIC_RE.search(ir), (
        f"{what}: unexpected llvm.*.with.overflow intrinsic in IR — a "
        f"trap-on-overflow lowering appears to be present"
    )
    asm = _emit_asm(source, monkeypatch, tmp_path)
    assert not _TRAP_ASM_RE.search(asm), (
        f"{what}: unexpected trap instruction (brk/ud1/ud2/.trap) in emitted "
        f"assembly — UB trapping appears to have been added"
    )


# ---------------------------------------------------------------------------
# 1. Signed integer overflow: INT_MAX + 1
# ---------------------------------------------------------------------------

def test_signed_overflow_emits_no_ubsan_guard(monkeypatch, tmp_path):
    # C: signed overflow is UB. UBSan would trap via
    # __ubsan_handle_add_overflow (built on llvm.sadd.with.overflow). pcc emits
    # a plain `add` with no check today — pin that absence.
    source = r"""
    int main(void){
        volatile int a = 2147483647;   /* INT_MAX */
        volatile int b = 1;
        int c = a + b;
        return c;
    }
    """
    _assert_no_ubsan_guard(source, monkeypatch, tmp_path, what="signed add overflow")


@_aarch64_only
def test_signed_overflow_wraps_twos_complement_on_aarch64():
    # No guard -> bare two's-complement wrap: INT_MAX + 1 == INT_MIN on AArch64.
    # (x86_64 would also wrap here — plain `add` does not trap — but the value
    # comparison stays AArch64-gated to keep the suite's arch policy uniform.)
    assert _ev(
        r"""
        int main(void){
            volatile int a = 2147483647;   /* INT_MAX */
            volatile int b = 1;
            int c = a + b;
            return c == (-2147483647 - 1) ? 0 : 1;   /* == INT_MIN */
        }
        """
    ) == 0


# ---------------------------------------------------------------------------
# 2. Division overflow: INT_MIN / -1   (complements test_c_division_trap.py,
#    which pins the *behavioural* AArch64 result; here we pin the IR/asm shape)
# ---------------------------------------------------------------------------

def test_intmin_div_minus_one_emits_no_ubsan_guard(monkeypatch, tmp_path):
    # C: INT_MIN / -1 overflows the representable range -> UB. UBSan traps via
    # __ubsan_handle_divrem_overflow. pcc emits a bare `sdiv` with no guard.
    source = r"""
    int main(void){
        volatile int a = -2147483647 - 1;  /* INT_MIN */
        volatile int b = -1;
        int c = a / b;
        return c;
    }
    """
    _assert_no_ubsan_guard(
        source, monkeypatch, tmp_path, what="INT_MIN / -1 overflow"
    )


# ---------------------------------------------------------------------------
# 3. Division by zero: 1 / 0   (complements test_c_division_trap.py behaviour
#    pin; here we pin that no divide-by-zero guard is inserted)
# ---------------------------------------------------------------------------

def test_div_by_zero_emits_no_ubsan_guard(monkeypatch, tmp_path):
    # C: x / 0 is UB. UBSan traps via __ubsan_handle_divrem_overflow (it also
    # guards the zero divisor). pcc emits a bare `sdiv` with no divisor check.
    source = r"""
    int main(void){
        volatile int a = 1;
        volatile int b = 0;
        int c = a / b;
        return c;
    }
    """
    _assert_no_ubsan_guard(
        source, monkeypatch, tmp_path, what="divide by zero"
    )


# ---------------------------------------------------------------------------
# 4. Out-of-range shift: x << 40  (shift amount >= bit width of int)
# ---------------------------------------------------------------------------

def test_oob_left_shift_emits_no_ubsan_guard(monkeypatch, tmp_path):
    # C: shifting an int by >= 32 is UB. UBSan traps via
    # __ubsan_handle_shift_out_of_bounds. pcc emits a bare `shl` with no
    # shift-amount range check. The *result value* is unspecified across ISAs
    # (AArch64 masks the amount mod 32/64), so we pin only the absence of a
    # guard here — which is arch-independent — and do NOT assert a value.
    source = r"""
    int main(void){
        volatile int x = 1;
        volatile int s = 40;   /* >= 32: out of range for int */
        int c = x << s;
        return c;
    }
    """
    _assert_no_ubsan_guard(
        source, monkeypatch, tmp_path, what="oversized left shift"
    )


# ---------------------------------------------------------------------------
# 5. Negative shift amount: x << -1
# ---------------------------------------------------------------------------

def test_negative_left_shift_emits_no_ubsan_guard(monkeypatch, tmp_path):
    # C: a negative shift amount is UB. UBSan traps via
    # __ubsan_handle_shift_out_of_bounds. pcc emits a bare `shl` with no
    # negative-amount check. Result value is unspecified across ISAs; pin only
    # the absence of a guard.
    source = r"""
    int main(void){
        volatile int x = 1;
        volatile int s = -1;   /* negative shift: UB */
        int c = x << s;
        return c;
    }
    """
    _assert_no_ubsan_guard(
        source, monkeypatch, tmp_path, what="negative left shift"
    )


# ---------------------------------------------------------------------------
# 6. Signed -> unsigned truncation (implementation-defined, not UB, but the
#    natural home for a future -fsanitize=implicit-conversion check).
# ---------------------------------------------------------------------------

def test_signed_to_unsigned_truncation_emits_no_conversion_guard(
    monkeypatch, tmp_path
):
    # Narrowing a negative long long into unsigned char keeps the low 8 bits
    # (C 6.3.1.3). Clang's -fsanitize=implicit-conversion would flag the value
    # change; pcc emits a bare `trunc` with no guard. Pin that absence.
    source = r"""
    int main(void){
        volatile long long b = -1;          /* 0xFFFFFFFFFFFFFFFF */
        unsigned char n = (unsigned char)b; /* truncates to 0xFF */
        return n;
    }
    """
    _assert_no_ubsan_guard(
        source, monkeypatch, tmp_path, what="signed->unsigned truncation"
    )


def test_signed_to_unsigned_truncation_keeps_low_bits():
    # Behavioural pin: the truncation is modular (low 8 bits), same across ISAs
    # (no trap involved), so this one is NOT arch-gated. -1 -> 0xFF == 255.
    assert _ev(
        r"""
        int main(void){
            volatile long long b = -1;
            unsigned char n = (unsigned char)b;
            return n == 255 ? 0 : 1;
        }
        """
    ) == 0
