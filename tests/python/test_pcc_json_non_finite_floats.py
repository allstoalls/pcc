"""pcc's json decoder reads the NaN and Infinity tokens its encoder writes.

``json.dumps`` spells non-finite floats ``NaN``, ``Infinity`` and
``-Infinity`` (CPython's default ``allow_nan=True``), and CPython's
``json.loads`` reads them back.  pcc's decoder fell through to number parsing
and raised "bad number", which stopped a Stage2 build: the AST wire of
``pcc.frontends.python.codegen.layer1_support`` (``return 1e309``) written by
one pcc1 worker could not be read by the next (run 38411293966).
"""

from __future__ import annotations

import importlib.util
import json as host_json
import math
import os
from pathlib import Path
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
PROVIDER = REPO_ROOT / "pcc" / "stdlib" / "json.py"

_DOCUMENTS = [
    "NaN", "Infinity", "-Infinity",
    "[NaN, Infinity, -Infinity, -1, -1.5e3, 0]",
    '{"value": Infinity, "neg": -Infinity, "nan": NaN, "n": -0.5}',
    '[{"k": [Infinity]}, -Infinity]',
]


def _provider():
    spec = importlib.util.spec_from_file_location("pcc_stdlib_json", PROVIDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _same(left, right) -> bool:
    if isinstance(left, float) and isinstance(right, float):
        if math.isnan(left) or math.isnan(right):
            return math.isnan(left) and math.isnan(right)
        return left == right and type(left) is type(right)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_same(left[k], right[k]) for k in left)
    return left == right and type(left) is type(right)


@pytest.mark.parametrize("text", _DOCUMENTS)
def test_loads_reads_non_finite_tokens_like_cpython(text):
    assert _same(_provider().loads(text), host_json.loads(text))


def test_dumps_output_round_trips_through_the_provider():
    provider = _provider()
    value = {"value": 1e309, "neg": -1e309, "nan": float("nan"), "list": [1e309, 2.5, -0.0]}
    text = provider.dumps(value)
    assert text == host_json.dumps(value)
    assert _same(provider.loads(text), value)


@pytest.mark.parametrize("text", ["Inf", "-Inf", "Nan", "-", "-NaN", "Infinit"])
def test_partial_tokens_still_raise(text):
    with pytest.raises(ValueError):
        _provider().loads(text)
    with pytest.raises(ValueError):
        host_json.loads(text)


_PROBE = '''
import json


def is_nan(value):
    return value != value


def main():
    text = json.dumps({"value": 1e309, "neg": -1e309, "nan": float("nan"), "list": [1e309, 2.5]})
    print(text)
    back = json.loads(text)
    print(back["value"] == 1e309, back["neg"] == -1e309, is_nan(back["nan"]), back["list"])
    tokens = json.loads("[NaN, Infinity, -Infinity, -1, -1.5e3, 0]")
    print(is_nan(tokens[0]), tokens[1] > 0, tokens[2] < 0, tokens[1] == -tokens[2], tokens[3:])


main()
'''


def test_native_json_reads_non_finite_tokens(tmp_path, pcc_runtime_archive, python_program_compiler):
    source = tmp_path / "json_non_finite_probe.py"
    source.write_text(_PROBE, encoding="utf-8")
    expected = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=30,
    )
    assert expected.returncode == 0, expected.stderr
    output = tmp_path / "json_non_finite_probe"
    python_program_compiler(
        str(source), str(output), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for gc in range(5):
        ran = subprocess.run(
            [str(output)], capture_output=True, text=True, timeout=60,
            env=dict(os.environ, PCC_GC_BACKEND=str(gc)),
        )
        assert ran.returncode == 0, f"GC{gc}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{gc}: {ran.stdout}"
