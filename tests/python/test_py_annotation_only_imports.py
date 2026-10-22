from __future__ import annotations

import textwrap
from pathlib import Path
import re

import pytest

from pcc.frontends.python.py_ast import FuncDef, FuncType, IntType, StrType


_REPO_ROOT = Path(__file__).absolute().parents[2]
_BUILD = _REPO_ROOT / "build"
_BUILD.mkdir(parents=True, exist_ok=True)


def _compile_to_ll(source: str, name: str) -> str:
    from pcc.frontends.python.pipeline import compile_python

    src = _BUILD / f"{name}.py"
    out = _BUILD / f"{name}.ll"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    return out.read_text(encoding="utf-8")


def test_llvmlite_import_used_only_in_annotations_does_not_emit_cpython_import():
    program = textwrap.dedent(
        """
        from __future__ import annotations
        import llvmlite.binding as llvm

        def f(module: llvm.ModuleRef) -> int:
            return 1
        """
    )

    ir = _compile_to_ll(program, "annotation_only_llvmlite_import")

    assert "cpy.import.llvmlite_binding" not in ir
    assert "call " not in "\n".join(
        line for line in ir.splitlines() if "@py_cpy_" in line
    )


@pytest.mark.parametrize("import_statement, expression", [
    ("import llvmlite.binding as llvm", "llvm.parse_assembly(text)"),
    ("import llvmlite.ir as llvm", "llvm.Module()"),
    ("from llvmlite import binding as llvm", "llvm.parse_assembly(text)"),
    ("from llvmlite.binding import parse_assembly", "parse_assembly(text)"),
    ("import pcc_missing_external_provider as external", "external.run(text)"),
    ("from pcc_missing_external_provider import run", "run(text)"),
])
def test_unowned_runtime_imports_use_uniform_strict_rejection(import_statement, expression):
    from pcc.backend.self_backend_parse import decode_llvm_c_string

    program = (
        "from __future__ import annotations\n" + import_statement
        + "\ndef f(text: str) -> object:\n    return " + expression + "\n"
    )
    ir = _compile_to_ll(program, "strict_runtime_import_" + str(len(import_statement)) + "_" + str(len(expression)))
    assert not re.search(r"\bcall [^\n]*@py_cpy_", ir)
    messages = [decode_llvm_c_string(token) for token in re.findall(r'c"[^"\n]*"', ir)]
    assert any(message.startswith(b"No module named ") for message in messages)
    # ModuleNotFoundError, as CPython raises for a module it cannot find.
    assert re.search(r"\bcall [^\n]*@py_exc_new\(i64 21,", ir)
    assert re.search(r"\bcall [^\n]*@py_raise\b", ir)


def test_native_lift_preserves_callable_annotation_shape():
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module

    program = textwrap.dedent(
        """
        from typing import Callable

        def apply(fn: Callable[[int], str], value: int) -> str:
            return fn(value)
        """
    )

    typed = infer_module(parse_and_lift(program, "callable_ann.py", "probe"))
    fd = next(
        stmt for stmt in typed.body
        if isinstance(stmt, FuncDef) and stmt.name == "apply"
    )
    ann = fd.args[0].annotation

    assert isinstance(ann, FuncType)
    assert ann.params == (IntType(name="int"),)
    assert ann.ret == StrType(name="str")
