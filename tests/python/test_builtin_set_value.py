"""A first-class set constructor stays on the owned execution path."""

from __future__ import annotations

import os
import re
import subprocess

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python.pipeline import compile_python


PROGRAM = '''from builtins import set as Set
from dataclasses import dataclass, field
@dataclass(slots=True)
class Box:
    values: object = field(default_factory=set)
def construct(factory, values):
    return factory(values)
def main():
    assert Set is set
    constructor = Set
    empty = constructor()
    assert type(empty) is set
    assert len(empty) == 0
    populated = construct(constructor, [1, 2, 1])
    assert populated == {1, 2}
    left = Box()
    right = Box()
    assert left.values is not right.values
    left.values.add(3)
    assert right.values == set()
    assert Box(values=None).values is None
    try:
        constructor([1], [2])
    except TypeError:
        pass
    else:
        raise AssertionError("set accepted two operands")
    try:
        constructor(values=[1])
    except TypeError:
        pass
    else:
        raise AssertionError("set accepted a keyword")
    print("OWNED_SET_VALUE_OK")
main()
'''


def test_set_value_and_factory_reach_owned_object_without_cpython(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "set_value.py"
    output = tmp_path / "set_value.ll"
    source.write_text(PROGRAM)
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", emit_llvm_only=True)
    text = output.read_text()
    cpython_call = re.search(r"\bcall[^\n]*@py_cpy_", text)
    assert cpython_call is None
    assert "strict.nolib.stub" not in text
    assert emit_owned_object(text, "arm64-apple-darwin")[:4] == b"\xcf\xfa\xed\xfe"


@pytest.mark.integration
def test_native_set_value_and_factory_all_gcs(tmp_path, pcc_runtime_archive,
                                           python_program_compiler):
    source = tmp_path / "set_value.py"
    binary = tmp_path / "set_value"
    source.write_text(PROGRAM)
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for gc in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(gc))
        environment.pop("LC_ALL", None)
        result = subprocess.run([str(binary)], env=environment, capture_output=True,
                                text=True, timeout=20)
        assert result.returncode == 0, (gc, result.stderr)
        assert result.stdout == "OWNED_SET_VALUE_OK\n"
        assert result.stderr == ""
