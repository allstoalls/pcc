"""Float precision and integer controls across both array CLI implementations."""

import contextlib
import io
import json
import math
import os
import random
import subprocess

import pytest

from pcc.driver.cli_bootstrap_array_core import _run_native_package_array_core_from_pcc1
from pcc.package.array_core import array_core_report
from pcc.package.array_numeric import float_sum, wrap_integer
from tests.integration.test_pcc1_release_features import verify_release_compiler
from tests.python.test_self_host_oracle_diff import pcc1_self_host_binary


def native_arguments(options):
    arguments = ["--json"]
    for name, value in options.items():
        arguments.append("--" + name.replace("_", "-"))
        if value is not True:
            arguments.append(str(value))
    return arguments


def native_source_report(options):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        status = _run_native_package_array_core_from_pcc1(native_arguments(options))
    return status, json.loads(output.getvalue())


def assert_same_values(actual, expected):
    if isinstance(expected, list):
        assert isinstance(actual, list)
        assert len(actual) == len(expected)
        for left, right in zip(actual, expected):
            assert_same_values(left, right)
    elif isinstance(expected, float):
        assert float(actual).hex() == expected.hex()
    else:
        assert actual == expected


CASES = [
    pytest.param(
        {
            "literal": "[0.0000001,1.1234567890123456,-0.0000001,-0.0]",
            "dtype": "float64",
        },
        id="literal-float64",
    ),
    pytest.param(
        {"literal": "[0.0000001,1.1234567,-0.0]", "dtype": "float32"},
        id="literal-float32-carrier",
    ),
    pytest.param({"literal": "[1e-100,-2e-100,1e100]"}, id="scientific-literals"),
    pytest.param({"literal": "[255,256,-129]", "dtype": "int8"}, id="integer-wrap"),
    pytest.param(
        {"literal": "[9007199254740993,-9007199254740993]", "dtype": "int64"},
        id="integer-above-float-exact-range",
    ),
    pytest.param(
        {"literal": "[9223372036854775807,9223372036854775808]", "dtype": "int64"},
        id="signed64-wrap",
    ),
    pytest.param(
        {
            "literal": "[18446744073709551615,18446744073709551616,-1]",
            "dtype": "uint64",
        },
        id="unsigned64-wrap",
    ),
    pytest.param({"literal": "[0.0000001,-0.0]", "dtype": "bool"}, id="bool-cast"),
    pytest.param({"literal": "[1.0]", "op": "div", "rhs": "[3.0]"}, id="division"),
    pytest.param(
        {"literal": "[1.1234567]", "op": "add", "rhs": "[0.0000001]"}, id="addition"
    ),
    pytest.param(
        {"literal": "[1.1234567]", "op": "sub", "rhs": "[0.0000001]"}, id="subtraction"
    ),
    pytest.param(
        {"literal": "[0.0000001]", "op": "mul", "rhs": "[0.0000002]"},
        id="multiplication",
    ),
    pytest.param({"literal": "[0.0000001,-0.0]", "unary": "neg"}, id="negative"),
    pytest.param({"literal": "[-0.0000001,-0.0]", "unary": "abs"}, id="absolute"),
    pytest.param(
        {"literal": "[0.0000001,-0.0]", "unary": "logical_not"}, id="logical-not"
    ),
    pytest.param(
        {"literal": "[0.0000001,1.1234568]", "clip": "0.00000015,1.12345675"}, id="clip"
    ),
    pytest.param({"literal": "[0.0000002,0.0000001]", "sort": True}, id="sort"),
    pytest.param({"literal": "[0.0000002,0.0000001]", "argsort": True}, id="argsort"),
    pytest.param(
        {"literal": "[0.0000001,0.0000003]", "searchsorted": "[0.0000002]"},
        id="searchsorted",
    ),
    pytest.param({"literal": "[0.0000002,0.0000001]", "partition": 0}, id="partition"),
    pytest.param(
        {"literal": "[0.0000002,0.0000001]", "argpartition": 0}, id="argpartition"
    ),
    pytest.param(
        {"literal": "[0.0000001]", "compare": "gt", "rhs": "[0.0]"}, id="comparison"
    ),
    pytest.param(
        {
            "literal": "[9007199254740993]",
            "compare": "eq",
            "rhs": "[9007199254740992.0]",
        },
        id="mixed-large-integer-comparison",
    ),
    pytest.param(
        {"literal": "[[0.0000001,1.1234567]]", "matmul": "[[0.0000001],[0.0000002]]"},
        id="matmul",
    ),
    pytest.param({"shape": "2", "fill": "0.0000001", "dtype": "float64"}, id="fill"),
    pytest.param({"eye": "2", "dtype": "float64"}, id="eye"),
    pytest.param({"arange": "0.0000001,0.0000004,0.0000001"}, id="arange"),
    pytest.param({"linspace": "0,1,4"}, id="linspace"),
    pytest.param({"literal": "[0.0000001,1.1234567]", "reduce": "sum"}, id="sum"),
    pytest.param(
        {"literal": "[1e16,1.0,-1e16]", "reduce": "sum"}, id="sum-cancellation"
    ),
    pytest.param({"literal": "[0.0000001,0.0000002]", "reduce": "prod"}, id="product"),
    pytest.param({"literal": "[0.0000001,0.0,1.0]", "reduce": "mean"}, id="mean"),
    pytest.param({"literal": "[0.0000002,0.0000001]", "reduce": "min"}, id="minimum"),
    pytest.param({"literal": "[0.0000001,0.0000002]", "reduce": "max"}, id="maximum"),
    pytest.param({"literal": "[0.0,0.0000001]", "reduce": "any"}, id="any"),
    pytest.param({"literal": "[0.0000001,0.0000002]", "reduce": "all"}, id="all"),
    pytest.param(
        {"literal": "[0.0000002,0.0000001]", "argreduce": "argmin"}, id="argmin"
    ),
    pytest.param(
        {"literal": "[0.0000001,0.0000002]", "argreduce": "argmax"}, id="argmax"
    ),
    pytest.param(
        {"literal": "[0.0,0.0000001]", "count_nonzero": True}, id="count-nonzero"
    ),
    pytest.param({"literal": "[0.0,0.0000001]", "nonzero": True}, id="nonzero"),
    pytest.param({"literal": "[0.0,0.0000001]", "argwhere": True}, id="argwhere"),
    pytest.param({"literal": "[0.0,0.0000001]", "flatnonzero": True}, id="flatnonzero"),
    pytest.param(
        {"literal": "[0.0000001,1.1234567]", "cumulative": "cumsum"}, id="cumsum"
    ),
    pytest.param(
        {"literal": "[0.0000001,0.0000002]", "cumulative": "cumprod"}, id="cumprod"
    ),
    pytest.param({"literal": "[0.0000001,-0.0]", "astype": "bool"}, id="astype"),
    pytest.param(
        {"literal": "[1e999,-1e999]", "dtype": "float64"}, id="infinity-serialization"
    ),
    pytest.param(
        {"literal": "[1e999]", "op": "sub", "rhs": "[1e999]"}, id="nan-serialization"
    ),
    pytest.param(
        {"literal": "[1e999]", "op": "sub", "rhs": "[1e999]", "astype": "bool"},
        id="nan-truth",
    ),
]


@pytest.mark.parametrize("options", CASES)
def test_native_source_numeric_family_matches_host(options):
    expected = array_core_report(**options)
    status, actual = native_source_report(options)
    assert status == 0, actual
    assert actual["ok"] == expected["ok"] is True
    assert actual["dtype"] == expected["dtype"]
    assert actual["shape"] == expected["shape"]
    assert_same_values(actual["data"], expected["data"])
    # pcc1 once reported uint64 as [0, -1]: 2**64 read through its raw i64 lane.
    assert actual.get("dtype_range") == expected.get("dtype_range")
    assert actual["diagnostics"] == expected["diagnostics"] == []


def test_shared_float_accumulator_matches_python_sum():
    rng = random.Random(191)
    cases = [
        [],
        [-0.0],
        [1e16, 1.0, -1e16],
        [1e308, 1e308],
        [float("inf"), 1.0],
        [float("inf"), -float("inf")],
        [float("nan")],
    ]
    for _ in range(100):
        cases.append(
            [
                math.ldexp(rng.uniform(-1.0, 1.0), rng.randrange(-100, 100))
                for _ in range(30)
            ]
        )
    for values in cases:
        actual = float_sum(values)
        expected = sum(values)
        if math.isnan(expected):
            assert math.isnan(actual)
        else:
            assert actual.hex() == float(expected).hex()


@pytest.mark.parametrize("value", [1.5, "1", None])
def test_integer_wrap_requires_an_integer_carrier(value):
    with pytest.raises(TypeError, match="PCC-ARRAY-INTEGER-CARRIER-REQUIRED"):
        wrap_integer(value, 8, True)


@pytest.mark.parametrize("bits", [-1, 0, 7, 65])
def test_integer_wrap_rejects_unsupported_width(bits):
    with pytest.raises(ValueError, match="PCC-ARRAY-INTEGER-WIDTH-UNSUPPORTED"):
        wrap_integer(1, bits, True)


def test_integer_wrap_keeps_negative_and_full_width_values():
    assert wrap_integer(-1, 64, False) == 18446744073709551615
    assert wrap_integer(9223372036854775808, 64, True) == -9223372036854775808


@pytest.mark.integration
@pytest.mark.parametrize("options", CASES)
def test_current_pcc1_array_numeric_cli(options, pcc1_self_host_binary):
    """The real rebuilt CLI must execute the same numeric cases without a host."""
    verify_release_compiler(pcc1_self_host_binary)
    environment = os.environ.copy()
    environment.pop("LC_ALL", None)
    environment["PCC_HOST_PYTHON"] = "/bin/false"
    completed = subprocess.run(
        [
            str(pcc1_self_host_binary),
            "-m",
            "pcc.package",
            "array-core",
            *native_arguments(options),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    actual = json.loads(completed.stdout)
    expected = array_core_report(**options)
    assert actual["ok"] == expected["ok"] is True
    assert actual["dtype"] == expected["dtype"]
    assert actual["shape"] == expected["shape"]
    assert_same_values(actual["data"], expected["data"])
    # pcc1 once reported uint64 as [0, -1]: 2**64 read through its raw i64 lane.
    assert actual.get("dtype_range") == expected.get("dtype_range")
    assert actual["diagnostics"] == expected["diagnostics"] == []
