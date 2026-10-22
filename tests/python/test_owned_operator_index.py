"""Host API semantics and owned object-returning integer-index admission."""

from pathlib import Path
import re

import pytest

from pcc.stdlib import operator as owned_operator


class _Index:
    def __init__(self, value):
        self.value = value

    def __index__(self):
        return self.value


class _IntOnly:
    def __int__(self):
        return 5


@pytest.mark.parametrize("value", [0, 1, -1, 2**200, -(2**200), False, True])
def test_host_api_returns_exact_arbitrary_precision_integer(value):
    result = owned_operator.index(value)
    assert type(result) is int
    assert result == value
    if type(value) is int:
        assert result is value


def test_host_api_uses_type_index_and_ignores_instance_shadow():
    value = _Index(2**200)
    value.__index__ = lambda: -1
    assert owned_operator.index(value) == 2**200
    with pytest.raises(TypeError):
        owned_operator.index(_IntOnly())
    with pytest.raises(TypeError):
        owned_operator.index(_Index(1.25))


def test_index_provider_has_owned_object_returning_native_body(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    output = tmp_path / "operator.ll"
    compile_python(str(Path(owned_operator.__file__)), str(output),
                   emit_llvm_only=True, python_library=True,
                   libpython_mode="off", ir_scaffold_mode="on", backend="self")
    text = output.read_text()
    body = re.search(r"^define[^\n]*@user_pcc_stdlib_operator_index\(.*?^}", text, re.M | re.S)
    assert body is not None
    assert "@py_obj_index" in body.group()
    assert "@py_cpy_" not in body.group()
    assert "strict.nolib.stub" not in body.group()


def test_runtime_index_body_keeps_objects_out_of_machine_integer_lanes(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    source = Path(__file__).resolve().parents[2] / "pcc/runtime/py/py_protocol_runtime.py"
    output = tmp_path / "protocol.ll"
    compile_python(str(source), str(output), emit_llvm_only=True, python_library=True,
                   libpython_mode="off", ir_scaffold_mode="on", backend="self")
    text = output.read_text()
    body = re.search(r"^define[^\n]*@py_obj_index\(.*?^}", text, re.M | re.S)
    assert body is not None
    assert "@user_py_protocol_runtime__lookup_dunder" in body.group()
    assert "@user_py_protocol_runtime__call_unary" in body.group()
    assert "@py_int_to_i64" not in body.group()
    assert "@py_int_value_i64" not in body.group()
    assert "@py_cpy_" not in body.group()
    assert "strict.nolib.stub" not in body.group()


def test_public_index_calls_reach_the_owned_provider(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "public_index.py"
    source.write_text(
        "import operator\n"
        "from operator import index\n"
        "class Index:\n"
        "    def __index__(self): return 1606938044258990275541962092341162602522202993782792835301376\n"
        "def plain(): return operator.index(-1606938044258990275541962092341162602522202993782792835301376)\n"
        "def protocol(): return index(Index())\n"
    )
    output = tmp_path / "public_index.ll"
    compile_python(str(source), str(output), emit_llvm_only=True, recursive_stdlib=True,
                   libpython_mode="off", ir_scaffold_mode="on", backend="self")
    text = output.read_text()
    for name in ("plain", "protocol"):
        body = re.search(r"^define[^\n]*@user_public_index_" + name + r"\(.*?^}", text, re.M | re.S)
        assert body is not None
        assert "@user_operator_index" in body.group()
        assert "@py_cpy_" not in body.group()
        assert "strict.nolib.stub" not in body.group()
