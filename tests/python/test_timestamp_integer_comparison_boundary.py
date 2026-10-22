"""The time_t range check must compare complete Python integer values.

The provider uses c_obj for __index__, while scaffold modules normally emit
unboxed scalar constants. Mixed dynamic/float comparisons must not narrow the
positive 2**63 bound into signed i64 before comparison. Host IR checks are not
native acceptance: the separately marked test executes the same provider body.
"""
from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import math
import operator
import os
from pathlib import Path
import re

import pytest

from tests.python.owned_regression_support import explicit_owned_runtime
from tests.python.process_timeout import run_process_group_timeout

ROOT = Path(__file__).resolve().parents[2]
TARGETS = ("arm64-apple-darwin", "x86_64-unknown-linux-gnu")


def _provider_body():
    source = (ROOT / "pcc/stdlib/time.py").read_text()
    function = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef)
                    and node.name == "_timestamp_seconds")
    return ast.get_source_segment(source, function) + "\n"


HEADER = '''from pcc.extern import c_obj, c_double, extern
_index_value = extern("py_obj_index", (c_obj,), c_obj)
_float_value = extern("py_float_to_f64", (c_obj,), c_double)
def time() -> float:
    return 0.0
'''

COMPARISONS = '''def index_upper(seconds):
    integer = _index_value(seconds)
    return integer >= 9223372036854775808

def index_lower(seconds):
    integer = _index_value(seconds)
    return integer < -9223372036854775808

def float_upper(value: float):
    return value >= 9223372036854775808

def reversed_upper(value):
    return 9223372036854775808 <= value

def negative_lower(value):
    return value < -9223372036854775809

def computed_upper(value):
    return value >= (1 << 80)
'''


def _function(text, name):
    found = re.search(r"(?ms)^define [^\n]*@user_[^\n(]*_" + re.escape(name)
                      + r"\([^\n]*\n.*?^\}", text)
    assert found is not None, name
    return found.group(0)


@pytest.mark.parametrize("target", TARGETS)
def test_timestamp_range_comparisons_keep_exact_object_ir(tmp_path, monkeypatch, target):
    from pcc.frontends.python.pipeline import compile_python
    from tests.owned_ir_validation import verify_ir_text

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "timestamp_provider.py"
    source.write_text(HEADER + _provider_body() + COMPARISONS)
    output = source.with_suffix(".ll")
    compile_python(str(source), str(output), emit_llvm_only=True, python_library=True,
                   libpython_mode="off", ir_scaffold_mode="on", backend="self",
                   target_triple=target)
    text = output.read_text()
    verify_ir_text(text)
    for name in ("index_upper", "index_lower", "float_upper", "reversed_upper", "negative_lower"):
        body = _function(text, name)
        assert re.search(r"\bcall\b[^\n]*@py_int_from_cstr\(", body), body
        assert "i64 -9223372036854775808" not in body, body
        assert re.search(r"\bcall\b[^\n]*@py_obj_(?:ge|le|lt)\(", body), body
        assert "@pcc_gc_foreign_lease_acquire" in body
        assert "@pcc_gc_foreign_lease_release" in body
        assert "@pcc_gc_store_root" in body
    index_body = _function(text, "index_upper")
    assert re.search(r"\bcall\b[^\n]*@py_obj_index\(", index_body)
    assert not re.search(r"\bcall\b[^\n]*@py_(?:int_to_i64|int_value_i64|obj_index_i64)\(", index_body)
    computed = _function(text, "computed_upper")
    assert re.search(r"\bcall\b[^\n]*@py_int_shl\(", computed), computed
    provider = _function(text, "_timestamp_seconds")
    assert re.search(r"\bcall\b[^\n]*@py_int_from_cstr\(", provider), provider
    # The mathematical lower bound is unchanged; its negative literal tree
    # now also owns the temporary heap integer through dynamic comparison.
    assert re.search(r"\bcall\b[^\n]*@py_int_neg\(", provider), provider


@pytest.fixture
def timestamp():
    namespace = {"time": lambda: 0.0, "_index_value": operator.index,
                 "_float_value": float.__float__}
    exec(compile(_provider_body(), str(ROOT / "pcc/stdlib/time.py"), "exec"), namespace)
    return namespace["_timestamp_seconds"]


@pytest.mark.parametrize("value", (
    0, -1, -(1 << 63), (1 << 63) - 1, (1 << 53) + 1,
    0.0, -0.0, -0.25, -1.25, 1.25,
    -9223372036854775808.0, 9223372036854774784.0,
))
def test_timestamp_accepts_full_int64_range_and_floors_floats(timestamp, value):
    result = timestamp(value)
    assert type(result) is int
    assert result == math.floor(value)


@pytest.mark.parametrize("value", (
    1 << 63, -(1 << 63) - 1, 1 << 80, -(1 << 80),
    9223372036854775808.0, -9223372036854777856.0,
    float("inf"), -float("inf"),
))
def test_timestamp_rejects_out_of_range_before_conversion(timestamp, value):
    with pytest.raises(OverflowError, match="timestamp out of range for platform time_t"):
        timestamp(value)


CHECKS = '''class Indexed:
    def __init__(self, value):
        self.value = value
    def __index__(self):
        return self.value
    def __float__(self):
        raise AssertionError("index must precede float")

class Real:
    def __float__(self):
        return -1.25

class BadIndex:
    def __index__(self):
        return "invalid"

def accepted(value, expected):
    actual = _timestamp_seconds(value)
    assert type(actual) is int
    assert actual == expected, (value, actual, expected)

def rejected(value, error):
    try:
        _timestamp_seconds(value)
    except error:
        return
    raise AssertionError("timestamp range error lost")

def main():
    accepted(0, 0)
    accepted(-1, -1)
    accepted(-(1 << 63), -(1 << 63))
    accepted((1 << 63) - 1, (1 << 63) - 1)
    accepted((1 << 53) + 1, (1 << 53) + 1)
    accepted(None, 0)
    accepted(-0.25, -1)
    accepted(-1.25, -2)
    accepted(1.25, 1)
    accepted(-9223372036854775808.0, -(1 << 63))
    accepted(9223372036854774784.0, (1 << 63) - 1024)
    accepted(Indexed(-1), -1)
    accepted(Indexed((1 << 53) + 1), (1 << 53) + 1)
    accepted(Indexed((1 << 63) - 1), (1 << 63) - 1)
    accepted(Real(), -2)
    for value in (1 << 63, -(1 << 63) - 1, 1 << 80, -(1 << 80)):
        rejected(value, OverflowError)
        rejected(Indexed(value), OverflowError)
    for value in (9223372036854775808.0, -9223372036854777856.0,
                  float("inf"), -float("inf")):
        rejected(value, OverflowError)
    rejected(float("nan"), ValueError)
    rejected("1", TypeError)
    rejected(BadIndex(), TypeError)
    print("TIMESTAMP_RANGE_OK")
main()
'''


def _reference_output():
    namespace = {"time": lambda: 0.0, "_index_value": operator.index,
                 "_float_value": float.__float__}
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(compile(_provider_body() + CHECKS, "timestamp_reference.py", "exec"), namespace)
    return output.getvalue()


def test_timestamp_native_regression_reference():
    assert _reference_output() == "TIMESTAMP_RANGE_OK\n"


@pytest.mark.integration
def test_timestamp_range_native_all_gc(
    tmp_path, python_program_compiler, explicit_owned_runtime, request,
):
    expected = _reference_output()
    source = tmp_path / "timestamp_provider.py"
    source.write_text(HEADER + _provider_body() + CHECKS)
    binary = source.with_suffix(".out")
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            ir_scaffold_mode="on", runtime_archive=str(explicit_owned_runtime))
    executions = []
    receipt = {"compiler": request.node.callspec.params["python_program_compiler"],
               "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
               "provider_body_sha256": hashlib.sha256(_provider_body().encode()).hexdigest(),
               "runtime_sha256": hashlib.sha256(explicit_owned_runtime.read_bytes()).hexdigest(),
               "executions": executions}
    for collector in range(5):
        result = run_process_group_timeout(
            [str(binary)], timeout=30,
            env=dict(os.environ, PCC_GC_BACKEND=str(collector),
                     PCC_HOST_PYTHON="/unavailable/host-python", PATH=""),
        )
        executions.append({"collector": collector, "returncode": result.returncode,
                           "stdout": result.stdout, "stderr": result.stderr})
        (tmp_path / "timestamp-native.json").write_text(json.dumps(receipt, indent=2) + "\n")
        assert (result.returncode, result.stdout, result.stderr) == (0, expected, ""), executions[-1]
