"""Explicit machine arithmetic stays raw inside ordinary boxed Python modules.

IR checks use the real pipeline and owned verifier only. The native node is a
separate, archive-backed execution gate; preparing this file does not run it.
"""
from __future__ import annotations

import os
import re
import subprocess

import pytest

MACHINE_FUNCTIONS = """from pcc import i64, u64
def signed_add(a: i64, b: i64) -> i64:
    return a + b
def signed_sub(a: i64, b: i64) -> i64:
    return a - b
def signed_mul(a: i64, b: i64) -> i64:
    return a * b
def signed_div(a: i64, b: i64) -> i64:
    return a // b
def signed_mod(a: i64, b: i64) -> i64:
    return a % b
def signed_left(a: i64, b: i64) -> i64:
    return a << b
def signed_right(a: i64, b: i64) -> i64:
    return a >> b
def signed_pos(a: i64) -> i64:
    return +a
def signed_neg(a: i64) -> i64:
    return -a
def signed_invert(a: i64) -> i64:
    return ~a
def unsigned_add(a: u64, b: u64) -> u64:
    return a + b
def unsigned_sub(a: u64, b: u64) -> u64:
    return a - b
def unsigned_mul(a: u64, b: u64) -> u64:
    return a * b
def unsigned_div(a: u64, b: u64) -> u64:
    return a // b
def unsigned_mod(a: u64, b: u64) -> u64:
    return a % b
def unsigned_left(a: u64, b: u64) -> u64:
    return a << b
def unsigned_right(a: u64, b: u64) -> u64:
    return a >> b
def unsigned_pos(a: u64) -> u64:
    return +a
def unsigned_neg(a: u64) -> u64:
    return -a
def unsigned_invert(a: u64) -> u64:
    return ~a
"""


ORDINARY_FUNCTIONS = """def exact_add(a: int, b: int) -> int:
    return a + b
def exact_mul(a: int, b: int) -> int:
    return a * b
def exact_shift(a: int, b: int) -> int:
    return a << b
def exact_neg(a: int) -> int:
    return -a
def exact_invert(a: int) -> int:
    return ~a
"""


RAW_LITERAL_FUNCTIONS = """def unsigned_identity(value: u64) -> u64:
    return value
def unsigned_high_mask(value: u64) -> u64:
    return value & 9223372036854775808
def unsigned_max_literal() -> u64:
    return 18446744073709551615
def signed_min_literal() -> i64:
    return -9223372036854775808
def unsigned_literal_argument() -> u64:
    return unsigned_identity(18446744073709551615)
"""


RAW_LITERAL_CHECKS = """def check_raw_literals(one: u64, zero: u64, minimum: i64) -> bool:
    maximum: u64 = unsigned_invert(zero)
    high: u64 = unsigned_left(one, 63)
    assert unsigned_high_mask(maximum) == high
    assert unsigned_max_literal() == maximum
    assert unsigned_literal_argument() == maximum
    assert signed_min_literal() == minimum
    return True
def main():
    assert check_raw_literals(1, 0, -9223372036854775808)
    print("RAW_LITERAL_BOUNDARIES_OK")
main()
"""


NATIVE_CHECKS = """def check_signed(maximum: i64, minimum: i64, one: i64, zero: i64, count: i64) -> bool:
    two: i64 = one + one
    assert signed_add(maximum, one) == minimum
    assert signed_sub(minimum, one) == maximum
    assert signed_mul(maximum, two) == -two
    assert signed_neg(minimum) == minimum
    assert signed_pos(minimum) == minimum
    assert signed_invert(zero) == -one
    assert signed_left(one, count) == two
    assert signed_right(minimum, count) == minimum // two
    assert signed_left(one, -one) == minimum
    assert signed_right(minimum, -one) == -one
    assert signed_div(-7, 3) == -2
    assert signed_mod(-7, 3) == -1
    return True
def check_unsigned(one: u64, zero: u64, count: u64) -> bool:
    maximum: u64 = unsigned_invert(zero)
    two: u64 = one + one
    high: u64 = unsigned_left(one, 63)
    assert unsigned_add(maximum, one) == zero
    assert unsigned_sub(zero, one) == maximum
    assert unsigned_mul(high, two) == zero
    assert unsigned_pos(maximum) == maximum
    assert unsigned_neg(one) == maximum
    assert unsigned_invert(maximum) == zero
    assert unsigned_left(one, count) == two
    assert unsigned_right(high, count) == high // two
    assert unsigned_right(maximum, 63) == one
    assert unsigned_left(one, maximum) == high
    assert unsigned_right(high, maximum) == one
    assert unsigned_div(maximum, two) == maximum >> one
    assert unsigned_mod(maximum, two) == one
    return True
def main():
    assert check_signed(9223372036854775807, -9223372036854775808, 1, 0, 65)
    assert check_unsigned(1, 0, 65)
    huge = 1267650600228229401496703205376
    assert exact_add(huge, 1) == 1267650600228229401496703205377
    assert exact_mul(huge, huge) == 1606938044258990275541962092341162602522202993782792835301376
    assert exact_shift(1, 100) == huge
    assert exact_neg(huge) == -1267650600228229401496703205376
    assert exact_invert(huge) == -1267650600228229401496703205377
    print("MACHINE_ARITHMETIC_BOUNDARIES_OK")
main()
"""


BOOL_SLOT_TYPE_PROGRAM = """class Box:
    def __init__(self, value):
        self.value = value
def check_integer(value, expected):
    assert type(value) is int
    assert value == expected
def main():
    check_integer(Box(*(True << 0,)).value, 1)
    check_integer(Box(*(False >> 0,)).value, 0)
    check_integer(Box(*(1 << True,)).value, 2)
    check_integer(Box(*(4 >> True,)).value, 2)
    check_integer(Box(*(True ** False,)).value, 1)
    check_integer(Box(*(2 ** True,)).value, 2)
    check_integer(Box(*(+True,)).value, 1)
    check_integer(Box(*(-False,)).value, 0)
    check_integer(Box(*(~(+True),)).value, -2)
    check_integer(Box(*((True << 0) + 1,)).value, 2)
    bare = Box(*(True,)).value
    assert type(bare) is bool
    assert bare is True
    print("BOOL_SLOT_TYPE_BOUNDARIES_OK")
main()
"""



def _pipeline_ir(tmp_path, monkeypatch, source):
    from pcc.frontends.python.pipeline import compile_python
    from tests.owned_ir_validation import verify_ir_text

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    path = tmp_path / "machine_arithmetic.py"
    output = path.with_suffix(".ll")
    path.write_text(source)
    compile_python(str(path), str(output), emit_llvm_only=True, backend="self",
                   libpython_mode="off", ir_scaffold_mode="on",
                   target_triple="x86_64-unknown-linux-gnu")
    text = output.read_text()
    verify_ir_text(text)
    return text


def _function(text, name):
    found = re.search(r"(?ms)^define [^\n]*@user_[^\n(]*_" + re.escape(name)
                      + r"\([^\n]*\n.*?^\}", text)
    assert found is not None, name
    return found.group(0)


def test_explicit_machine_binary_routes_owned_ir(tmp_path, monkeypatch):
    text = _pipeline_ir(tmp_path, monkeypatch, MACHINE_FUNCTIONS)
    for prefix, division, remainder, right in (
        ("signed", "sdiv", "srem", "ashr"),
        ("unsigned", "udiv", "urem", "lshr"),
    ):
        for suffix, opcode in (("add", "add"), ("sub", "sub"), ("mul", "mul"),
                               ("div", division), ("mod", remainder),
                               ("left", "shl"), ("right", right)):
            body = _function(text, prefix + "_" + suffix)
            assert re.search(r"\b" + opcode + r" i64\b", body), body
            assert not re.search(r"\bcall\b[^\n]*@py_int_", body), body
            assert re.match(r"define (?:external |internal )?i64 @", body.splitlines()[0])
            if suffix in ("left", "right"):
                assert re.search(r"\band i64 [^\n]*, 63\b", body), body
                assert "shift.neg" not in body
            if suffix in ("div", "mod"):
                assert "@llvm.trap" in body
                assert "raw.div.zero" in body


def test_explicit_machine_unary_routes_owned_ir(tmp_path, monkeypatch):
    text = _pipeline_ir(tmp_path, monkeypatch, MACHINE_FUNCTIONS)
    for prefix in ("signed", "unsigned"):
        for suffix in ("pos", "neg", "invert"):
            body = _function(text, prefix + "_" + suffix)
            assert re.match(r"define (?:external |internal )?i64 @", body.splitlines()[0])
            assert not re.search(r"\bcall\b[^\n]*@py_int_", body), body
            if suffix == "neg":
                assert re.search(r"\bsub i64 0,", body), body
            elif suffix == "invert":
                assert re.search(r"\bxor i64 [^\n]*, -1\b", body), body


def test_ordinary_large_integer_routes_keep_exact_fallbacks(tmp_path, monkeypatch):
    text = _pipeline_ir(tmp_path, monkeypatch, MACHINE_FUNCTIONS + ORDINARY_FUNCTIONS + NATIVE_CHECKS)
    for suffix, helper in (("add", "py_int_add"), ("mul", "py_int_mul"),
                           ("shift", "py_int_shl"), ("neg", "py_int_neg"),
                           ("invert", "py_int_xor")):
        body = _function(text, "exact_" + suffix)
        assert re.match(r"define (?:external |internal )?ptr @", body.splitlines()[0])
        assert re.search(r"\bcall\b[^\n]*@" + helper + r"\(", body), body
        if suffix != "shift":
            assert not re.search(r"\bcall\b[^\n]*@py_int_to_i64_lane\(", body), body
        # The shift-count check may use its checked scalar projection; the
        # shifted value itself still reaches the exact py_int_shl kernel.
    assert re.search(r"\bcall\b[^\n]*@py_int_from_cstr\(", text)


def test_contextualized_raw_integer_literals_owned_ir(tmp_path, monkeypatch):
    text = _pipeline_ir(tmp_path, monkeypatch, MACHINE_FUNCTIONS + RAW_LITERAL_FUNCTIONS)
    for name in ("unsigned_high_mask", "unsigned_max_literal", "signed_min_literal"):
        body = _function(text, name)
        assert re.match(r"define (?:external |internal )?i64 @", body.splitlines()[0])
        assert not re.search(r"\bcall\b[^\n]*@py_int_", body), body
    mask = _function(text, "unsigned_high_mask")
    assert re.search(r"\band i64 [^\n]*, (9223372036854775808|-9223372036854775808)\b", mask)
    maximum = _function(text, "unsigned_max_literal")
    assert re.search(r"\bret i64 (18446744073709551615|-1)\b", maximum)
    minimum = _function(text, "signed_min_literal")
    assert re.search(r"\bret i64 -9223372036854775808\b", minimum)


def test_direct_raw_unsigned_literal_argument_owned_ir(tmp_path, monkeypatch):
    text = _pipeline_ir(tmp_path, monkeypatch, MACHINE_FUNCTIONS + RAW_LITERAL_FUNCTIONS)
    body = _function(text, "unsigned_literal_argument")
    assert re.search(r"\bcall i64(?: \(i64\))? @user_[^\n(]*_unsigned_identity\(i64 (18446744073709551615|-1)\)", body)
    assert not re.search(r"\bcall\b[^\n]*@py_int_", body), body


@pytest.mark.integration
def test_explicit_machine_arithmetic_native_all_gc(
    tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler,
):
    _run_native_all_gc(tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler,
                       MACHINE_FUNCTIONS + ORDINARY_FUNCTIONS + NATIVE_CHECKS,
                       "MACHINE_ARITHMETIC_BOUNDARIES_OK")


@pytest.mark.integration
def test_explicit_raw_literal_native_all_gc(
    tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler,
):
    _run_native_all_gc(tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler,
                       MACHINE_FUNCTIONS + RAW_LITERAL_FUNCTIONS + RAW_LITERAL_CHECKS,
                       "RAW_LITERAL_BOUNDARIES_OK")


@pytest.mark.integration
def test_literal_bool_integer_slot_results_native_all_gc(
    tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler,
):
    _run_native_all_gc(tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler,
                       BOOL_SLOT_TYPE_PROGRAM, "BOOL_SLOT_TYPE_BOUNDARIES_OK")


def _run_native_all_gc(tmp_path, monkeypatch, archive, compiler, program, expected):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "machine_arithmetic.py"
    executable = source.with_suffix("")
    source.write_text(program)
    compiler(str(source), str(executable), backend="self", libpython_mode="off",
             ir_scaffold_mode="on", runtime_archive=str(archive))
    for backend in range(5):
        ran = subprocess.run(
            [str(executable)], capture_output=True, text=True, timeout=30,
            env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)),
        )
        (tmp_path / ("gc" + str(backend) + ".stdout")).write_text(ran.stdout)
        (tmp_path / ("gc" + str(backend) + ".stderr")).write_text(ran.stderr)
        assert ran.returncode == 0, (backend, ran.stdout, ran.stderr)
        assert ran.stdout == expected + "\n"
        assert ran.stderr == ""
