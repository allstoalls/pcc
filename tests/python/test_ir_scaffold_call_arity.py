"""Scaffold ``builder.call`` arities exist in ``pcc.ir.ir``.

``builder.call(fn, [a0, ..., aN-1])`` lowers to ``IRBuilder_call<N>``.
``py_func_new_static_def`` takes nine arguments, and ``pcc.ir.ir`` stopped
at ``IRBuilder_call8``: every host test passed (they compile user programs,
not the compiler), and Stage1 failed only when linking pcc1 on the undefined
``_user_pcc_ir_ir_IRBuilder_call9``. Each arity the lowering emits is now
defined, the compiler source never exceeds it, and a wider list fails at
compile time.
"""
import ast
from pathlib import Path
import re

import pytest

from pcc.frontends.python.codegen.ir_scaffold_lowering import ScaffoldUnsupportedError
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.pipeline_context import build_closed_world_context
from pcc.frontends.python.type_infer import infer_module
from pcc1_gate import repo_root

_MAX = L1CodeGen._IR_SCAFFOLD_CALL_MAX_ARITY


def test_every_scaffold_call_arity_is_defined_by_pcc_ir():
    tree = ast.parse((repo_root() / "pcc/ir/ir.py").read_text(encoding="utf-8"))
    defined = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    for arity in range(_MAX + 1):
        node = defined.get(f"IRBuilder_call{arity}")
        assert node is not None, f"IRBuilder_call{arity}"
        params = [arg.arg for arg in node.args.args]
        assert params == ["builder", "fn"] + [f"arg{i}" for i in range(arity)] + ["name"]
        assert len(node.args.defaults) == 1


def test_compiler_source_literal_calls_fit_the_defined_arities():
    widest = []
    for path in sorted((repo_root() / "pcc").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "call" and len(node.args) >= 2
                    and isinstance(node.args[1], (ast.List, ast.Tuple))
                    and len(node.args[1].elts) > _MAX):
                widest.append(f"{path.relative_to(repo_root())}:{node.lineno}")
    assert widest == []


def _scaffold_ir(tmp_path, monkeypatch, arity):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "0")
    source = tmp_path / "probe.py"
    operands = ", ".join(["operand"] * arity)
    source.write_text("from pcc.ir.compat import ir\n"
                      "def probe(fn, operand):\n    builder = ir.IRBuilder()\n"
                      "    return builder.call(fn, [" + operands + "])\n")
    provider = repo_root() / "pcc/ir/ir.py"
    modules, exports, derived = build_closed_world_context(
        [str(provider), str(source)], ["pcc.ir.ir", "probe"])
    assert f"IRBuilder_call{_MAX}" in exports["pcc.ir.ir"]
    typed = infer_module(modules[-1], external_exports=exports, derived_class_map=derived)
    codegen = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
    codegen._native_module_exports = exports
    codegen._strict_no_libpython = True
    return str(codegen.generate(typed))


def test_widest_literal_call_lowers_to_the_defined_variant(tmp_path, monkeypatch):
    text = _scaffold_ir(tmp_path, monkeypatch, _MAX)
    assert re.search(r"call ptr [^\n]*@user_pcc_ir_ir_IRBuilder_call%d\(" % _MAX, text)


def test_wider_literal_call_fails_at_compile_time(tmp_path, monkeypatch):
    with pytest.raises(ScaffoldUnsupportedError, match="IRBuilder_call0..IRBuilder_call%d" % _MAX):
        _scaffold_ir(tmp_path, monkeypatch, _MAX + 1)
