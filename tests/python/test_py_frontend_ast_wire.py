from __future__ import annotations

import json
import os
import subprocess
import textwrap

import pytest


NATIVE_WIRE_CONTRACT_PROGRAM = '''import json
from pcc.frontends.python.py_ast_contract import PY_AST_FIELD_NAME_OVERRIDES
from pcc.frontends.python.pipeline_ast_wire import (
    _PY_AST_FIELD_NAME_OVERRIDES, _py_ast_to_wire, _py_ast_from_wire,
)
from pcc.frontends.python.py_ast import DynType, SetType, ClassType, ValueArrayType
from pcc.frontends.python.py_lift import parse_and_lift
def main():
    assert _PY_AST_FIELD_NAME_OVERRIDES is PY_AST_FIELD_NAME_OVERRIDES
    assert PY_AST_FIELD_NAME_OVERRIDES["Name"] == ("span", "ty", "ident")
    assert len(PY_AST_FIELD_NAME_OVERRIDES) == 66
    set_type = SetType(name="set", elem=DynType(name="dyn"))
    assert _py_ast_from_wire(_py_ast_to_wire(set_type)) == set_type
    element = ClassType(name="Point", module="m", fields=(), bases=(),
                        properties=(), valueclass=True)
    array_type = ValueArrayType(name="pcc.array", elem=element, length=3)
    assert _py_ast_from_wire(_py_ast_to_wire(array_type)) == array_type
    source = "class Item:\\n    token = 23\\n    def method(self, value=token):\\n        return value\\n"
    module = parse_and_lift(source, "wire.py", "wire")
    recovered = _py_ast_from_wire(json.loads(json.dumps(_py_ast_to_wire(module))))
    assert recovered == module
    assert recovered.body[0].body[1].args[1].default.ident == "token"
    print("AST_WIRE_CONTRACT_OK")
main()
'''


def test_pipeline_facade_reexports_ast_wire_codec() -> None:
    from pcc.frontends.python import pipeline, pipeline_ast_wire

    assert pipeline._py_ast_field_names is pipeline_ast_wire._py_ast_field_names
    assert pipeline._py_ast_to_wire is pipeline_ast_wire._py_ast_to_wire
    assert pipeline._py_ast_from_wire is pipeline_ast_wire._py_ast_from_wire
    assert pipeline._write_py_ast_wire is pipeline_ast_wire._write_py_ast_wire
    assert pipeline._read_py_ast_wire is pipeline_ast_wire._read_py_ast_wire


def test_ast_wire_field_names_use_the_stable_table_and_ignore_primitive_leaves() -> None:
    from pcc.frontends.python import pipeline_ast_wire, py_ast

    node = py_ast.Name(
        span=py_ast.SourceSpan("probe.py", 1, 0, 1, 1),
        ty=py_ast.IntType("int"),
        ident="value",
    )
    assert pipeline_ast_wire._py_ast_field_names(node) == (
        "span",
        "ty",
        "ident",
    )
    assert pipeline_ast_wire._py_ast_field_names("leaf") == ()


def test_py_ast_wire_roundtrip_preserves_full_lifted_ast() -> None:
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.pipeline import _py_ast_from_wire, _py_ast_to_wire

    source = textwrap.dedent(
        r'''
        import json

        class Box:
            label: str = "line1\nline2"

            def __init__(self, value: int = 7) -> None:
                self.value = value

            def render(self) -> str:
                quote = 'a"b'
                slash = "a\\b"
                raw = b"abc"
                return self.label + quote + slash + str(raw)

        def main() -> None:
            box = Box()
            print(json.dumps({"x": box.render()}))
        '''
    ).lstrip()
    ast_mod = parse_and_lift(source, "wire_probe.py", "wire_probe")
    wire_text = json.dumps(_py_ast_to_wire(ast_mod))
    recovered = _py_ast_from_wire(json.loads(wire_text))
    assert recovered == ast_mod


def test_parallel_self_codegen_enables_ast_wire_sidecars(tmp_path, monkeypatch) -> None:
    from pcc.frontends.python.pipeline import compile_python_multi

    entry = tmp_path / "entry.py"
    lib = tmp_path / "lib.py"
    out_ll = tmp_path / "out.ll"
    entry.write_text(
        textwrap.dedent(
            """
            def main() -> None:
                print(1)

            if __name__ == "__main__":
                main()
            """
        ).lstrip()
    , encoding="utf-8")
    lib.write_text(
        textwrap.dedent(
            """
            def helper() -> int:
                return 42
            """
        ).lstrip()
    , encoding="utf-8")
    monkeypatch.setenv("PCC_PY_FRONTEND_JOBS", "2")
    monkeypatch.setenv("PCC_PY_FRONTEND_AST_WIRE", "1")
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    profile = {}
    compile_python_multi(
        [str(entry), str(lib)],
        str(out_ll),
        module_names=["entry", "lib"],
        entry_module="entry",
        backend="self",
        libpython_mode="off",
        emit_llvm_only=True,
        profile=profile,
    )
    assert out_ll.exists()
    counters = profile.get("counters", {})
    assert counters.get("multi_frontend_ast_wire_enabled") == 1
    assert counters.get("multi_frontend_chunks") == 2


def test_py_ast_wire_is_opt_in_by_default(monkeypatch) -> None:
    from pcc.frontends.python.pipeline import _python_frontend_ast_wire_enabled

    monkeypatch.delenv("PCC_PY_FRONTEND_AST_WIRE", raising=False)
    assert _python_frontend_ast_wire_enabled() is False

    monkeypatch.setenv("PCC_PY_FRONTEND_AST_WIRE", "1")
    assert _python_frontend_ast_wire_enabled() is True


def test_py_ast_wire_normalizes_legacy_classtype_null_fields() -> None:
    from pcc.frontends.python.pipeline import _py_ast_from_wire, _py_ast_to_wire
    from pcc.frontends.python.py_ast import ClassType

    cls = ClassType("PointerType", "", (), (), None, None)
    recovered = _py_ast_from_wire(_py_ast_to_wire(cls))
    assert recovered.fields == ()
    assert recovered.bases == ()
    assert recovered.properties == ()
    assert recovered.valueclass is False


def test_py_ast_wire_roundtrips_first_class_set_type() -> None:
    from pcc.frontends.python.pipeline import _py_ast_from_wire, _py_ast_to_wire
    from pcc.frontends.python.py_ast import DynType, SetType

    set_ty = SetType(name="set", elem=DynType(name="dyn"))

    assert _py_ast_from_wire(_py_ast_to_wire(set_ty)) == set_ty


def test_py_ast_wire_roundtrips_value_array_type() -> None:
    # A module annotating pcc.array[...] failed in the frontend worker with
    # "unknown py_ast wire node kind ValueArrayType".
    from pcc.frontends.python.pipeline import _py_ast_from_wire, _py_ast_to_wire
    from pcc.frontends.python.py_ast import ClassType, ValueArrayType

    elem = ClassType(name="Point", module="m", fields=(), bases=(), properties=(),
                     valueclass=True)
    array_ty = ValueArrayType(name="pcc.array", elem=elem, length=3)

    assert _py_ast_from_wire(_py_ast_to_wire(array_ty)) == array_ty


def test_stdlib_ast_lifter_preserves_interleaved_call_operand_order() -> None:
    from pcc.frontends.python.parser import parse
    from pcc.frontends.python.py_ast import Call, ExprStmt

    module = parse(
        "target(named=value, *items, **mapping)\n",
        "operand_order_probe.py",
    )
    stmt = module.body[0]
    assert isinstance(stmt, ExprStmt)
    assert isinstance(stmt.expr, Call)
    assert stmt.expr.operand_order == (
        ("kw", 0),
        ("arg", 0),
        ("kw", 1),
    )


@pytest.mark.integration
def test_native_ast_wire_uses_shared_contract_all_gcs(
    tmp_path, pcc_runtime_archive, python_program_compiler,
) -> None:
    source = tmp_path / "wire_contract.py"
    binary = tmp_path / "wire_contract"
    source.write_text(NATIVE_WIRE_CONTRACT_PROGRAM)
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for gc in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(gc))
        environment.pop("LC_ALL", None)
        result = subprocess.run(
            [str(binary)], env=environment, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (gc, result.stderr)
        assert result.stdout == "AST_WIRE_CONTRACT_OK\n"
        assert result.stderr == ""
