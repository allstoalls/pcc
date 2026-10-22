from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from pcc.backend.self_backend_dispatch import emit_self_asm
from pcc.backend.self_backend_targets import classify_self_backend_target_triple
from pcc.backend.self_backend_x86_64_linux import emit_x86_64_linux_asm
from pcc.frontends.python.pipeline_targets import host_target_triple
from tests.c_testsuite_cases import _host_cc, subprocess_env


_MASK64 = (1 << 64) - 1
_SIGN64 = 1 << 63
_INT64_MIN = -(1 << 63)
_REPO = Path(__file__).resolve().parents[2]
_INVENTORY = _REPO / "docs" / "refs_docs" / "compiler-rt-builtins-inventory.md"
_HOST_TRIPLE = host_target_triple()
_SELF_TARGET_VERDICT = classify_self_backend_target_triple(_HOST_TRIPLE)
_SELF_TARGET_GATE = (
    None
    if _SELF_TARGET_VERDICT.supported
    else _SELF_TARGET_VERDICT.skip_reason()
)


def _u64(value: int) -> int:
    return value & _MASK64


def _i64(value: int) -> int:
    value &= _MASK64
    return value - (1 << 64) if value & _SIGN64 else value


def _compiler_rt_udivmod(numerator: int, denominator: int) -> tuple[int, int]:
    """Oracle-derived restoring division used by int_div_impl.inc.

    This deliberately does not call Python ``//`` or ``%``.  It mirrors the
    compiler-rt unsigned magnitude loop so signed expectations below do not
    accidentally inherit Python's floor-division semantics.
    """

    numerator = _u64(numerator)
    denominator = _u64(denominator)
    if denominator == 0:
        raise ValueError("compiler-rt declares integer division by zero unspecified")
    quotient = 0
    remainder = 0
    for bit in range(63, -1, -1):
        remainder = (remainder << 1) | ((numerator >> bit) & 1)
        if remainder >= denominator:
            remainder -= denominator
            quotient |= 1 << bit
    return _u64(quotient), _u64(remainder)


def _compiler_rt_sdivmod(numerator: int, denominator: int) -> tuple[int, int]:
    numerator = _i64(numerator)
    denominator = _i64(denominator)
    if denominator == 0:
        raise ValueError("compiler-rt declares integer division by zero unspecified")
    numerator_negative = numerator < 0
    denominator_negative = denominator < 0
    numerator_magnitude = _u64(-numerator if numerator_negative else numerator)
    denominator_magnitude = _u64(
        -denominator if denominator_negative else denominator
    )
    quotient, remainder = _compiler_rt_udivmod(
        numerator_magnitude, denominator_magnitude
    )
    if numerator_negative != denominator_negative:
        quotient = _u64(-quotient)
    if numerator_negative:
        remainder = _u64(-remainder)
    return _i64(quotient), _i64(remainder)


_SIGNED_ROWS = (
    (_INT64_MIN, -1, _INT64_MIN, 0),
    (_INT64_MIN, 1, _INT64_MIN, 0),
    ((1 << 63) - 1, -1, -((1 << 63) - 1), 0),
    (-17, 5, -3, -2),
    (17, -5, -3, 2),
    (-17, -5, 3, -2),
    (17, 5, 3, 2),
    (0, -7, 0, 0),
)

_UNSIGNED_ROWS = (
    (0, 1, 0, 0),
    (_MASK64, 1, _MASK64, 0),
    (_MASK64, 2, (1 << 63) - 1, 1),
    (_MASK64, _MASK64, 1, 0),
    (1 << 63, 3, 3074457345618258602, 2),
    (17, 5, 3, 2),
)


def _llvm_i64(value: int) -> str:
    return str(_i64(value))


def _division_ir(triple: str) -> str:
    instructions: list[str] = []
    predicates: list[str] = []
    row = 0
    for numerator, denominator, quotient, remainder in _SIGNED_ROWS:
        instructions.extend(
            [
                f"  %sq{row} = sdiv i64 {_llvm_i64(numerator)}, {_llvm_i64(denominator)}",
                f"  %sr{row} = srem i64 {_llvm_i64(numerator)}, {_llvm_i64(denominator)}",
                f"  %sq_ok{row} = icmp eq i64 %sq{row}, {_llvm_i64(quotient)}",
                f"  %sr_ok{row} = icmp eq i64 %sr{row}, {_llvm_i64(remainder)}",
                f"  %signed_ok{row} = and i1 %sq_ok{row}, %sr_ok{row}",
            ]
        )
        predicates.append(f"%signed_ok{row}")
        row += 1
    for numerator, denominator, quotient, remainder in _UNSIGNED_ROWS:
        instructions.extend(
            [
                f"  %uq{row} = udiv i64 {_llvm_i64(numerator)}, {_llvm_i64(denominator)}",
                f"  %ur{row} = urem i64 {_llvm_i64(numerator)}, {_llvm_i64(denominator)}",
                f"  %uq_ok{row} = icmp eq i64 %uq{row}, {_llvm_i64(quotient)}",
                f"  %ur_ok{row} = icmp eq i64 %ur{row}, {_llvm_i64(remainder)}",
                f"  %unsigned_ok{row} = and i1 %uq_ok{row}, %ur_ok{row}",
            ]
        )
        predicates.append(f"%unsigned_ok{row}")
        row += 1
    current = predicates[0]
    for index, predicate in enumerate(predicates[1:], 1):
        combined = f"%all_ok{index}"
        instructions.append(f"  {combined} = and i1 {current}, {predicate}")
        current = combined
    instructions.extend(
        [
            f"  %success = zext i1 {current} to i32",
            "  %status = xor i32 %success, 1",
            "  ret i32 %status",
        ]
    )
    return (
        f'target triple = "{triple}"\n\n'
        "define i32 @main() {\nentry:\n"
        + "\n".join(instructions)
        + "\n}\n"
    )


def test_checked_in_inventory_names_every_finite_family_and_oracle() -> None:
    text = _INVENTORY.read_text(encoding="utf-8")
    required_tokens = {
        "memory": ("`memcpy`", "`memmove`", "freestanding_mem_str.py"),
        "strings": ("`strlen`", "`strncmp`", "freestanding_mem_str.py"),
        "i64": ("`__divdi3`", "`__udivmoddi4`", "int_div_impl.inc"),
        "i128": ("`__divti3`", "`__udivmodti4`", "**absent**"),
        "fp-to-int": ("`__fixdfdi`", "fp_fixint_impl.inc"),
        "int-to-fp": ("`__floatdidf`", "`__floatundidf`"),
    }
    for family, tokens in required_tokens.items():
        assert all(token in text for token in tokens), (family, tokens)
    assert "INT64_MIN / -1" in text


def test_compiler_rt_derived_integer_rows_are_locked() -> None:
    for numerator, denominator, quotient, remainder in _SIGNED_ROWS:
        assert _compiler_rt_sdivmod(numerator, denominator) == (
            quotient,
            remainder,
        )
    for numerator, denominator, quotient, remainder in _UNSIGNED_ROWS:
        assert _compiler_rt_udivmod(numerator, denominator) == (
            _u64(quotient),
            _u64(remainder),
        )


def test_x86_self_backend_guards_compiler_rt_signed_overflow_shape() -> None:
    asm = emit_x86_64_linux_asm(_division_ir("x86_64-unknown-linux-gnu"))
    assert "mov r11, -9223372036854775808" in asm
    assert re.search(r"cmp r10, -1\n\s+je \.L.*signed_div_overflow", asm)
    assert "xor edx, edx" in asm
    assert "idiv r10" in asm


@pytest.mark.pcc_gate(unavailable=_SELF_TARGET_GATE)
def test_self_backend_i64_divmod_matches_compiler_rt_rows(tmp_path: Path) -> None:
    asm_path = tmp_path / "compiler_rt_i64_divmod.s"
    exe_path = tmp_path / "compiler_rt_i64_divmod.out"
    asm_path.write_text(
        emit_self_asm(_division_ir(_HOST_TRIPLE)), encoding="utf-8"
    )
    compiled = subprocess.run(
        [_host_cc(), str(asm_path), "-o", str(exe_path)],
        env=subprocess_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert compiled.returncode == 0, compiled.stderr
    executed = subprocess.run(
        [str(exe_path)],
        env=subprocess_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert executed.returncode == 0, (
        executed.returncode,
        executed.stdout,
        executed.stderr,
    )
