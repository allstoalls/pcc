"""Owned fixed-width overflow results, carry/borrow, and signed flags."""

import platform
import subprocess
import sys

import pytest

from pcc.backend.self_backend_aarch64_darwin import emit_aarch64_darwin_asm
from pcc.backend.owned_object_emit import emit_owned_object
from pcc.driver.project import TranslationUnit
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
from tests.c.test_owned_atomic_scalar_regressions import (
    TARGETS, _forbid_process, _run_owned,
)


def _program(width, unsigned, operation):
    ctype = ("unsigned " if unsigned else "") + ("int" if width == 32 else "long long")
    low = 0 if unsigned else -(1 << (width - 1))
    high = (1 << width) - 1 if unsigned else (1 << (width - 1)) - 1
    pairs = [(0, 0), (0, 1), (high, 1), (high, high), (low, 1), (low, high), (7, 3)]
    if not unsigned:
        pairs += [(low, -1), (-5, 3), (-5, -3), (high, -1)]
    lines = [f"typedef {ctype} word;", "int check(word a, word b, word expected, int flag) {", "word result = 17;",
             f"if (__builtin_{operation}_overflow(a, b, &result) != flag) return 1;",
             "return result == expected ? 0 : 2;", "}", "int main(void) {"]
    for index, (left, right) in enumerate(pairs):
        value = left + right if operation == "add" else left - right if operation == "sub" else left * right
        overflow = int(not low <= value <= high)
        result = value & ((1 << width) - 1)
        a, b = left & ((1 << width) - 1), right & ((1 << width) - 1)
        lines.append(f"if (check((word)0x{a:x}ULL, (word)0x{b:x}ULL, (word)0x{result:x}ULL, {overflow})) return {index + 1};")
    return "\n".join(lines + ["return 0;", "}"])


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("operation", ["add", "sub", "mul"])
@pytest.mark.parametrize("unsigned", [False, True], ids=["signed", "unsigned"])
@pytest.mark.parametrize("width", [32, 64])
def test_overflow_intrinsics_emit_without_external_symbols(monkeypatch, target, operation, unsigned, width):
    monkeypatch.setattr(subprocess, "Popen", _forbid_process)
    evaluator = CEvaluator(target_triple=target, backend="self")
    source = _program(width, unsigned, operation)
    ir = evaluator.compile_translation_units(
        [TranslationUnit("overflow.c", "overflow.c", source)],
        use_system_cpp=False, frontend_opt_level=0,
    )[0][1]
    data = emit_owned_object(ir, target)
    assert data
    assert b".with.overflow." not in data
    if "aarch64" in target or "arm64" in target:
        assembly = emit_aarch64_darwin_asm(ir, optimize=False)
        if operation in ("add", "sub"):
            condition = "vs" if not unsigned else "hs" if operation == "add" else "lo"
            assert f"  cset w12, {condition}" in assembly
            assert f"  {'adds' if operation == 'add' else 'subs'} {'w' if width == 32 else 'x'}11," in assembly


# native x86_64 Linux boundary
@pytest.mark.pcc_gate(probe=lambda: not (sys.platform != "linux" or platform.machine() != "x86_64"))
@pytest.mark.parametrize("operation", ["add", "sub", "mul"])
@pytest.mark.parametrize("unsigned", [False, True], ids=["signed", "unsigned"])
@pytest.mark.parametrize("width", [32, 64])
def test_overflow_results_and_flags_execute_owned(tmp_path, monkeypatch, operation, unsigned, width):
    result = _run_owned(_program(width, unsigned, operation), tmp_path, monkeypatch)
    assert result.returncode == 0, result
    assert result.stdout == result.stderr == ""
