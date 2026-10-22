"""Active native Python calls enforce limits and leave on every exit."""

import os
import subprocess

import pytest


PROGRAM = '''
from sys import getrecursionlimit
from pcc.extern import extern, c_int64
set_limit = extern("py_sys_setrecursionlimit", (c_int64,), c_int64)
depth = extern("py_recursion_depth", (), c_int64)

class Item:
    def __init__(self, value) -> None:
        self.value = value

def recurse(count):
    if count == 0:
        return 0
    return recurse(count - 1) + 1

def values():
    yield depth()
    yield depth()

def run():
    assert getrecursionlimit() == 1000
    index = 0
    while index < 32:
        value = Item(index)
        assert value.value == index
        index += 1
    assert depth() == 1
    iterator = values()
    assert depth() == 1
    assert next(iterator) == 2
    assert depth() == 1
    assert next(iterator) == 2
    assert depth() == 1
    assert set_limit(16) == 1
    try:
        recurse(100)
    except RecursionError:
        pass
    else:
        raise AssertionError("missing RecursionError")
    assert depth() == 1
    assert set_limit(1000) == 1
    assert recurse(8) == 8

run()
assert depth() == 0
print("RECURSION_OK")
'''


def test_from_sys_recursion_limit_reaches_owned_emitter(tmp_path, monkeypatch):
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python.pipeline import compile_python

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "recursion_alias.py"
    source.write_text("from sys import getrecursionlimit as limit\n"
                      "def read_limit():\n    return limit()\n"
                      "print(read_limit())\n")
    output = tmp_path / "recursion_alias.ll"
    compile_python(str(source), str(output), emit_llvm_only=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    ir_text = output.read_text()
    calls = [line for line in ir_text.splitlines() if "call " in line]
    assert any("call i64" in line and "@py_sys_getrecursionlimit(" in line for line in calls)
    assert not any("@py_cpy_import(" in line for line in calls)
    assert emit_owned_object(ir_text, "arm64-apple-darwin")[:4] == b"\xcf\xfa\xed\xfe"


@pytest.mark.integration
def test_native_recursion_unwinds_methods_generators_and_errors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "recursion.py"
    source.write_text(PROGRAM)
    output = tmp_path / "native"
    python_program_compiler(str(source), str(output), backend="self",
                            libpython_mode="off", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(output)], timeout=30, capture_output=True, text=True,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == "RECURSION_OK\n"
