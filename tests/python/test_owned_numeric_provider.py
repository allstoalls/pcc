"""Differential evidence for owned real-number and integer-index protocols."""

import math
import struct
from pathlib import Path
import re

import pytest

from pcc.stdlib import math as owned_math


def _float_record(value):
    if math.isnan(value):
        return "nan", math.copysign(1.0, value)
    return struct.pack("!d", value)


@pytest.mark.parametrize("value", [
    0.0, -0.0, 0.25, -0.25, 1.0, -1.0, 1.25, -1.25,
    5e-324, -5e-324, 1.7976931348623157e308, -1.7976931348623157e308,
    float("inf"), -float("inf"), float("nan"), -float("nan"),
    False, True, 2**100, -2**100,
])
def test_modf_values_and_signed_zeros_match_cpython(value):
    assert tuple(map(_float_record, owned_math.modf(value))) == tuple(
        map(_float_record, math.modf(value))
    )


class _FloatValue:
    def __float__(self):
        return -1.25


class _IndexValue:
    def __index__(self):
        return 5


class _IntOnly:
    def __int__(self):
        return 5


class _BadFloat:
    def __float__(self):
        return 5


class _FloatText(str):
    def __float__(self):
        return 1.25


@pytest.mark.parametrize("value", [_FloatValue(), _IndexValue(), _FloatText("text")])
def test_modf_uses_real_number_conversion_protocol(value):
    assert owned_math.modf(value) == math.modf(value)


@pytest.mark.parametrize("value", [
    "1.25", b"1.25", bytearray(b"1.25"), None, [], {}, 1j,
    _IntOnly(), _BadFloat(), 10**400,
])
def test_modf_rejects_invalid_or_overflowing_real_conversion(value):
    errors = []
    for function in (owned_math.modf, math.modf):
        try:
            function(value)
        except Exception as exc:
            errors.append(type(exc))
        else:
            errors.append(None)
    assert errors[0] is errors[1]
    assert errors[0] in (TypeError, OverflowError)


def test_modf_requires_exactly_one_positional_argument():
    for function in (owned_math.modf, math.modf):
        with pytest.raises(TypeError):
            function()
        with pytest.raises(TypeError):
            function(1, 2)
        with pytest.raises(TypeError):
            function(x=1)


def test_modf_provider_has_real_no_libpython_body(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    output = tmp_path / "math.ll"
    compile_python(
        str(Path(owned_math.__file__)), str(output),
        emit_llvm_only=True, python_library=True,
        libpython_mode="off", ir_scaffold_mode="on", backend="self",
    )
    text = output.read_text()
    body = re.search(r"^define .*@user_pcc_stdlib_math_modf\(.*?^}", text, re.M | re.S)
    assert body is not None
    assert "strict.nolib.stub" not in body.group()
    assert "@py_cpy_" not in body.group()
