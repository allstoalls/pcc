"""Integer-arithmetic security semantics for the C frontend.

Source: *Low-Level Software Security for Compiler Developers*, "Memory
vulnerability based attacks" — integer overflow / truncation / signedness are
the foundation of a huge fraction of memory-corruption primitives (a size
computation that wraps, a length truncated on the way into ``memcpy``, a
signed index that compares "in range" after promotion). The compiler must
lower these with the exact semantics C mandates so that defensive checks the
programmer writes actually hold.

pcc lowers ``int`` and ``unsigned int`` to LLVM ``i32`` and tracks signedness
separately (see AGENTS.md "C Codegen Invariants — Signedness"); these tests
exercise the boundary cases that a signedness-metadata-loss bug would corrupt.

``CEvaluator().evaluate`` compiles ``main`` and returns its exit code; each
case returns ``0`` exactly when the security-relevant invariant holds.
"""
from __future__ import annotations

import os
import sys

this_dir = os.path.dirname(__file__)
repo_root = os.path.dirname(os.path.dirname(this_dir))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator


def _ev(source: str) -> int:
    return CEvaluator().evaluate(source, optimize=False)


def test_unsigned_wraparound_is_well_defined():
    # C: unsigned arithmetic is modulo 2^N. Defensive `x + 1 == 0` overflow
    # checks rely on this being exact.
    assert _ev(
        r"""int main(void){ unsigned x = 0xFFFFFFFFu; x = x + 1u; return x == 0u ? 0 : 1; }"""
    ) == 0


def test_signed_unsigned_comparison_promotes_to_unsigned():
    # The classic length-check bypass: a negative signed value compared against
    # an unsigned bound is converted to a huge unsigned value (C 6.3.1.8).
    # `(-1) > 1u` must be TRUE; a codegen that emitted a *signed* compare here
    # would let `-1` slip past an `if (i < bound)` guard.
    assert _ev(
        r"""int main(void){ int i = -1; unsigned u = 1u; return (i > u) ? 0 : 1; }"""
    ) == 0


def test_arithmetic_shift_right_preserves_sign():
    # Signed >> is arithmetic (sign-extending) in practice; a logical shift
    # here would corrupt masks computed from signed values.
    assert _ev(
        r"""int main(void){ int x = -8; return (x >> 1) == -4 ? 0 : 1; }"""
    ) == 0


def test_logical_shift_right_unsigned():
    assert _ev(
        r"""int main(void){ unsigned x = 8u; return (x >> 1) == 4u ? 0 : 1; }"""
    ) == 0


def test_left_shift_into_high_bit_unsigned():
    # 1u << 31 must produce 0x80000000, not be mis-lowered/overflowed.
    assert _ev(
        r"""int main(void){ unsigned x = 1u << 31; return x == 2147483648u ? 0 : 1; }"""
    ) == 0


def test_integer_truncation_is_modular():
    # Narrowing a 64-bit length to 32 bits keeps the low 32 bits (C 6.3.1.3).
    # This is exactly the truncation that turns a "checked" 64-bit size into a
    # tiny 32-bit allocation; the lowering must be deterministic.
    assert _ev(
        r"""int main(void){ unsigned long long b = 0x100000001ULL; unsigned n = (unsigned)b; return n == 1u ? 0 : 1; }"""
    ) == 0


def test_unsigned_multiply_overflow_wraps_modulo():
    # n * size overflow: 0x10000000 * 0x10 == 0x100000000 -> 0 in 32 bits.
    assert _ev(
        r"""int main(void){ unsigned a = 0x10000000u, b = 0x10u; unsigned p = a * b; return p == 0u ? 0 : 1; }"""
    ) == 0


def test_safe_allocation_size_overflow_check_idiom():
    # The defensive idiom `count && size > SIZE_MAX/count` must compute
    # correctly so the overflow is *detected* (returns 0 == overflow seen).
    assert _ev(
        r"""
        #include <stdint.h>
        int main(void){
            size_t count = (size_t)1 << 40;
            size_t size  = (size_t)1 << 40;   /* count*size overflows 64 bits */
            int overflow = (count != 0 && size > (SIZE_MAX / count));
            return overflow ? 0 : 1;
        }
        """
    ) == 0
