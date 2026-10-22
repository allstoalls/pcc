"""C-int range errors precede positivity checks without changing the limit."""

import ast
import os
from pathlib import Path
import subprocess
import sys

import pytest


RUNTIME = Path(__file__).resolve().parents[2] / "pcc/runtime/py/py_recursion_runtime.py"


@pytest.fixture
def recursion_setter_model():
    """Execute the raw setter source with modeled memory and exception ABI."""
    state = {"pcc_py_recursion_limit": 1000, "pcc_py_recursion_depth": 4}
    errors = []
    writes = []

    def store(address, offset, value, order):
        assert offset == 0
        assert order == "release"
        writes.append((address, value))
        state[address] = value

    namespace = {
        "c_abi_export": lambda name: lambda function: function,
        "cstr": lambda message: message,
        "global_addr": lambda name: name,
        "load_i32": lambda address, offset: state[address],
        "atomic_store_i32": store,
        "py_exc_new": lambda code, message: (code, message),
        "py_raise_owned": errors.append,
    }
    tree = ast.parse(RUNTIME.read_text(encoding="utf-8"))
    setter = next(node for node in tree.body
                  if isinstance(node, ast.FunctionDef)
                  and node.name == "py_sys_setrecursionlimit")
    exec(compile(ast.Module(body=[setter], type_ignores=[]), str(RUNTIME), "exec"), namespace)
    return namespace["py_sys_setrecursionlimit"], state, errors, writes


@pytest.mark.parametrize("limit,code", [
    (-9223372036854775808, 15),
    (-2147483649, 15),
    (-2147483648, 2),
    (-1, 2),
    (0, 2),
    (1, 56),
    (4, 56),
    (2147483648, 15),
    (9223372036854775807, 15),
])
def test_raw_recursion_limit_rejection_preserves_state(recursion_setter_model, limit, code):
    setter, state, errors, writes = recursion_setter_model
    original = dict(state)
    assert setter(limit) == 0
    assert len(errors) == 1
    assert errors[0][0] == code
    assert state == original
    assert writes == []


@pytest.mark.parametrize("limit", [5, 2147483647])
def test_raw_recursion_limit_accepts_c_int_max(recursion_setter_model, limit):
    setter, state, errors, writes = recursion_setter_model
    assert setter(limit) == 1
    assert errors == []
    assert state == {"pcc_py_recursion_limit": limit, "pcc_py_recursion_depth": 4}
    assert writes == [("pcc_py_recursion_limit", limit)]


SETTER_CALLS = ("sys.setrecursionlimit", "change")
BOUNDARY_PROGRAM = '''
import sys
from sys import setrecursionlimit as change

calls = []

class Limit:
    def __init__(self, value):
        self.value = value

    def __index__(self):
        calls.append(self.value)
        return self.value

def reject(value):
    try:
        SETTER(value)
    except ValueError:
        print("value")
    except OverflowError:
        print("overflow")
    else:
        raise AssertionError("invalid recursion limit was accepted")

def run():
    old = sys.getrecursionlimit()
    for value in (-9223372036854775808, -2147483649, -2147483648,
                  -1, 0, 2147483648, 9223372036854775807, -(1 << 80), 1 << 80):
        reject(value)
        assert sys.getrecursionlimit() == old
        count = len(calls)
        reject(Limit(value))
        assert len(calls) == count + 1
        assert calls[-1] == value
        assert sys.getrecursionlimit() == old
    try:
        SETTER(2147483647)
        print(sys.getrecursionlimit() == 2147483647)
        SETTER(old)
        SETTER(Limit(2147483647))
        print(sys.getrecursionlimit() == 2147483647)
    finally:
        SETTER(old)
    print(sys.getrecursionlimit() == old)

run()
'''
BOUNDARY_OUTPUT = "".join(value + "\n" for value in (
    "overflow", "overflow", "overflow", "overflow",
    "value", "value", "value", "value", "value", "value",
    "overflow", "overflow", "overflow", "overflow",
    "overflow", "overflow", "overflow", "overflow",
    "True", "True", "True",
))


@pytest.mark.parametrize("setter", SETTER_CALLS)
def test_recursion_limit_boundary_cpython_oracle(tmp_path, setter):
    source = tmp_path / "recursion_boundary.py"
    source.write_text(BOUNDARY_PROGRAM.replace("SETTER", setter), encoding="utf-8")
    result = subprocess.run([sys.executable, str(source)], capture_output=True,
                            text=True, timeout=10)
    assert (result.returncode, result.stdout, result.stderr) == (0, BOUNDARY_OUTPUT, "")


@pytest.mark.integration
@pytest.mark.parametrize("setter", SETTER_CALLS)
def test_native_recursion_limit_boundaries_direct_alias_and_index(
    tmp_path, pcc_runtime_archive, python_program_compiler, setter,
):
    source = tmp_path / "recursion_boundary.py"
    source.write_text(BOUNDARY_PROGRAM.replace("SETTER", setter), encoding="utf-8")
    executable = tmp_path / "recursion_boundary"
    python_program_compiler(str(source), str(executable), backend="self",
                            libpython_mode="off", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(executable)], capture_output=True, text=True,
                                timeout=30, env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert (result.returncode, result.stdout, result.stderr) == (0, BOUNDARY_OUTPUT, "")
