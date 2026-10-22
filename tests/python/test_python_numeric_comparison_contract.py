"""Python numeric predicates preserve NaNs and exact integer comparisons."""

import ast
import os
from pathlib import Path
import subprocess
import struct
import sys

import pytest

from pcc.frontends.python.pipeline import compile_python
from tests.runtime_build_cache import cached_pcc_python_runtime


@pytest.fixture(scope="module")
def numeric_c_runtime():
    return cached_pcc_python_runtime() / "libpy_runtime_pcc_py.a"


def native_matches_python(tmp_path, monkeypatch, numeric_c_runtime, source, reference_source=None):
    path = tmp_path / "numeric_contract.py"
    output = tmp_path / "numeric_contract"
    path.write_text(source)
    reference_path = path
    if reference_source is not None:
        reference_path = tmp_path / "reference.py"
        reference_path.write_text(reference_source)
    reference = subprocess.run(
        [sys.executable, str(reference_path)], capture_output=True, text=True, timeout=20
    )
    assert reference.returncode == 0, reference.stderr
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(numeric_c_runtime))
    compile_python(
        str(path), str(output), backend="self", libpython_mode="off",
        ir_scaffold_mode="on",
    )
    environment = dict(os.environ, PCC_HOST_PYTHON="/usr/bin/false")
    result = subprocess.run(
        [str(output)], capture_output=True, text=True, timeout=20, env=environment
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == reference.stdout


NAN_PROGRAM = """class Box:
    def __init__(self, value):
        self.value = value

class NeverEqual:
    def __eq__(self, other):
        return False

def typed(a: float, b: float):
    print(bool(a), a == b, a != b, a < b, a <= b, a > b, a >= b)

def boxed(a, b):
    print(bool(a.value), a.value == b.value, a.value != b.value,
          a.value < b.value, a.value <= b.value,
          a.value > b.value, a.value >= b.value)

def main():
    n = float('nan')
    values = [n, 0.0, -0.0, 1.0, -1.0, float('inf'), float('-inf')]
    for value in values:
        print(repr(value))
        typed(value, value)
        typed(value, 0.0)
        typed(0.0, value)
        cell = Box(value)
        boxed(cell, cell)
        boxed(cell, Box(0.0))
        boxed(Box(0.0), cell)
    cell = Box(n)
    other = Box(float('nan'))
    boxed(cell, other)
    items = [cell.value]
    print(cell.value in items, items.count(cell.value), items.index(cell.value))
    print(cell.value in (cell.value,))
    print(cell.value in {cell.value}, {cell.value: 9}[cell.value])
    print([cell.value] == [cell.value], (cell.value,) == (cell.value,))
    print({'v': cell.value} == {'v': cell.value})
    print([cell.value, 0] < [other.value, 1], [cell.value, 0] <= [other.value, 1])
    print([cell.value, 0] > [other.value, 1], [cell.value, 0] >= [other.value, 1])
    ordered = sorted([cell.value, other.value])
    print(ordered[0] is cell.value, ordered[1] is other.value)
    print(sorted([3.0, 1.0, 2.0]))
    nonreflexive = Box(NeverEqual())
    print(nonreflexive.value == nonreflexive.value)
    print(nonreflexive.value in [nonreflexive.value])

main()
"""


def test_nan_predicates_and_container_identity_match_python(
    tmp_path, monkeypatch, numeric_c_runtime
):
    native_matches_python(tmp_path, monkeypatch, numeric_c_runtime, NAN_PROGRAM)


def test_typed_nan_truth_and_inequality_match_python(
    tmp_path, monkeypatch, numeric_c_runtime
):
    native_matches_python(tmp_path, monkeypatch, numeric_c_runtime, """def check(value: float):
    print(bool(value), value == value, value != value, value < 0.0,
          value <= 0.0, value > 0.0, value >= 0.0)

def main():
    for value in [float('nan'), 0.0, -0.0, 1.0, -1.0, float('inf'), float('-inf')]:
        check(value)

main()
""")


def test_boxed_nan_equality_preserves_container_identity(
    tmp_path, monkeypatch, numeric_c_runtime
):
    native_matches_python(tmp_path, monkeypatch, numeric_c_runtime, """class Box:
    def __init__(self, value):
        self.value = value
class NeverEqual:
    def __eq__(self, other):
        return False
def main():
    cell = Box(float('nan'))
    other = Box(float('nan'))
    print(cell.value == cell.value, cell.value != cell.value)
    print(cell.value == other.value, cell.value != other.value)
    items = [cell.value]
    print(cell.value in items, items.count(cell.value), items.index(cell.value))
    print(cell.value in (cell.value,))
    print(cell.value in {cell.value}, {cell.value: 9}[cell.value])
    print([cell.value] == [cell.value], (cell.value,) == (cell.value,))
    print({'v': cell.value} == {'v': cell.value})
    obj = Box(NeverEqual())
    print(obj.value == obj.value, obj.value in [obj.value])
main()
""")


def test_capi_nan_richcompare_and_bool_have_distinct_identity_contracts(
    tmp_path, monkeypatch, numeric_c_runtime
):
    source = """from pcc.extern import extern, c_obj, c_int
compare: 'extern' = extern('PyObject_RichCompare', (c_obj, c_obj, c_int), c_obj)
compare_bool: 'extern' = extern('PyObject_RichCompareBool', (c_obj, c_obj, c_int), c_int)
class Box:
    def __init__(self, value):
        self.value = value
def main():
    cell = Box(float('nan'))
    print(compare(cell.value, cell.value, 2), compare(cell.value, cell.value, 3))
    print(compare_bool(cell.value, cell.value, 2), compare_bool(cell.value, cell.value, 3))
main()
"""
    reference = """import ctypes
compare = ctypes.pythonapi.PyObject_RichCompare
compare.argtypes = [ctypes.py_object, ctypes.py_object, ctypes.c_int]
compare.restype = ctypes.py_object
compare_bool = ctypes.pythonapi.PyObject_RichCompareBool
compare_bool.argtypes = [ctypes.py_object, ctypes.py_object, ctypes.c_int]
compare_bool.restype = ctypes.c_int
value = float('nan')
print(compare(value, value, 2), compare(value, value, 3))
print(compare_bool(value, value, 2), compare_bool(value, value, 3))
"""
    native_matches_python(tmp_path, monkeypatch, numeric_c_runtime, source, reference)


def test_python_port_float_truth_matches_ieee_values():
    from pcc.runtime.py.py_abi_constants import PY_TYPE_FLOAT, PY_TYPE_INT

    path = Path(__file__).resolve().parents[2] / "pcc/runtime/py/py_obj_ops_dispatch.py"
    tree = ast.parse(path.read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "py_obj_truthy")
    function.decorator_list = []
    globals_by_name = {name: object() for name in ("py_None", "py_False", "py_True")}
    namespace = {
        "ptr_is_null": lambda value: int(value is None),
        "ptr_eq": lambda left, right: int(left is right),
        "global_load_ptr": globals_by_name.__getitem__,
        "is_tagged_int": lambda value: 0,
        "pcc_capi_is_cext_type_tag": lambda tag: 0,
        "load_i32": lambda value, offset: PY_TYPE_FLOAT,
        "load_i64": lambda value, offset: int.from_bytes(struct.pack("<d", value), "little", signed=True),
        "PY_TYPE_FLOAT": PY_TYPE_FLOAT,
        "PY_TYPE_INT": PY_TYPE_INT,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
    for value in (float("nan"), float("inf"), float("-inf"), 0.0, -0.0, 1.0, -1.0):
        assert namespace["py_obj_truthy"](value) == int(bool(value))
