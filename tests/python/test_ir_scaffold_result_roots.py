"""Owned IR Python helpers publish results before direct-argument cleanup."""
from pathlib import Path
import re

import pytest

from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.codegen.errors import L1CodegenError
from pcc.frontends.python.pipeline_context import build_closed_world_context
from pcc.frontends.python.type_infer import infer_module


@pytest.mark.parametrize("expression,suffix", [
    ("ir.IntType(64)", "scaffold_IntType"),
    ("ir.Constant(ir.IntType(64), 12)", "scaffold_Constant_i64"),
    ("[ir.Constant(ir.IntType(64), 12)]", "scaffold_Constant_i64"),
    ("ir.ArrayType(ir.IntType(8), 7)", "scaffold_ArrayType"),
    ("builder.add(operand, operand)", "IRBuilder_add"),
    ("builder.fcmp_ordered('==', operand, operand)", "IRBuilder_fcmp_ordered"),
])
@pytest.mark.parametrize("contextual", [False, True])
def test_scaffold_object_result_is_published_at_return(tmp_path, monkeypatch, expression, suffix, contextual):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "0")
    provider = Path(__file__).resolve().parents[2] / "pcc/ir/ir.py"
    source = tmp_path / "probe.py"
    source.write_text("from pcc.ir.compat import ir\n"
                      "def take(*, value, **extras):\n    return value\n"
                      "def probe(operand):\n    builder = ir.IRBuilder()\n"
                      "    return take(value=" + expression + ")\n")
    paths = [str(provider), str(source)] if contextual else [str(source)]
    names = ["pcc.ir.ir", "probe"] if contextual else ["probe"]
    modules, exports, derived = build_closed_world_context(paths, names)
    typed = infer_module(modules[-1], external_exports=exports, derived_class_map=derived)
    codegen = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
    codegen._native_module_exports = exports if contextual else None
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    text = str(codegen.generate(typed))
    (tmp_path / "probe.ll").write_text(text)
    body = re.search(r"^define [^\n]*@user_probe_probe\([^\n]*\).*?^}", text, re.M | re.S).group(0)
    calls = list(re.finditer(r"(%[^\s]+) = call ptr [^\n]*@user_pcc_ir_ir_" + suffix + r"\([^\n]*\)\n", body))
    assert len(calls) == 1
    assert body[calls[0].end():].lstrip().startswith("store ptr " + calls[0].group(1) + ", ptr ")
    assert not re.search(r"\bcall [^\n]*@py_cpy_", body)
    # Publication's fallible lease checks run before the callee-error check.
    # Their error target must include argument unpins, not only output roots.
    publication = body[calls[0].end():]
    cleanup_target = re.search(r"label %(scaffold\.arguments\.cleanup[^,\s]+)", publication)
    assert cleanup_target is not None
    cleanup = re.search(r"^" + re.escape(cleanup_target.group(1)) + r":\n(.*?)(?=^\S|\Z)", body, re.M | re.S)
    assert cleanup is not None
    # The error edge undoes exactly the argument pins the success edge undoes.
    # Static literals are never pinned; with the provider in view the managed
    # receiver is, so that case must unpin something.
    continuation = re.search(
        r"br i1 %err\.cmp[^,\n]*, label %" + re.escape(cleanup_target.group(1)) + r", label %([^\s,]+)\n",
        publication,
    )
    assert continuation is not None
    success = re.search(r"^" + re.escape(continuation.group(1)) + r":\n(.*?)(?=^\S|\Z)", body, re.M | re.S)
    assert success is not None
    cleanup_unpins = re.findall(r"@pcc_gc_unpin\(ptr ([^)]+)\)", cleanup.group(1))
    success_unpins = re.findall(r"@pcc_gc_unpin\(ptr ([^)]+)\)", success.group(1))
    assert sorted(cleanup_unpins) == sorted(success_unpins)
    assert not contextual or cleanup_unpins


@pytest.mark.parametrize("expression,suffix", [
    ("builder.store(operand, operand)", "IRBuilder_store"),
    ("builder.ret(operand)", "IRBuilder_ret"),
    ("builder.ret_void()", "IRBuilder_ret_void"),
    ("builder.branch(operand)", "IRBuilder_branch"),
    ("builder.cbranch(operand, operand, operand)", "IRBuilder_cbranch"),
    ("builder.unreachable()", "IRBuilder_unreachable"),
    ("builder.fence('seq_cst')", "IRBuilder_fence"),
    ("builder.store_atomic(operand, operand, 'release', 4)", "IRBuilder_store_atomic"),
])
def test_void_ir_operations_keep_provider_value_result(tmp_path, monkeypatch, expression, suffix):
    # An emitted LLVM void operation still has a managed Python Value result.
    test_scaffold_object_result_is_published_at_return(
        tmp_path, monkeypatch, expression, suffix, contextual=True,
    )


@pytest.mark.parametrize("manual", [False, True])
def test_scaffold_rejects_provider_raw_or_manual_pointer_result(tmp_path, monkeypatch, manual):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "0")
    provider = tmp_path / "provider.py"
    provider.write_text("from pcc.extern import c_ptr\nfrom pcc.unsafe import int_to_ptr\n"
                        + ("__pcc_runtime_port__ = True\n" if manual else "")
                        + "def scaffold_IntType(width: int)" + ("" if manual else " -> c_ptr")
                        + ":\n    return int_to_ptr(width)\n")
    entry = tmp_path / "probe.py"
    entry.write_text("from pcc.ir.compat import ir\n"
                    "def take(*, value, **extras):\n    return value\n"
                    "def probe():\n    return take(value=ir.IntType(64))\n")
    modules, exports, derived = build_closed_world_context(
        [str(provider), str(entry)], ["pcc.ir.ir", "probe"])
    info = exports["pcc.ir.ir"]["scaffold_IntType"]
    assert info["return_ty"] == (("dyn",) if manual else ("raw_pointer",))
    assert info["manual_pointer_abi"] is manual
    typed = infer_module(modules[1], external_exports=exports, derived_class_map=derived)
    codegen = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
    codegen._native_module_exports = exports
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    with pytest.raises(L1CodegenError, match="scaffold call has no managed result contract"):
        codegen.generate(typed)
    text = str(codegen.module)
    assert not re.search(r"\bcall [^\n]*@user_pcc_ir_ir_scaffold_IntType\(", text)


def test_unknown_pointer_declaration_does_not_imply_owned_result():
    from types import SimpleNamespace
    from pcc.ir.compat import ir
    from pcc.frontends.python.codegen.ir_scaffold_lowering import _scaffold_emit_declared_call

    module = ir.Module("unknown_pointer")
    function = ir.Function(module, ir.FunctionType(ir.IntType(8).as_pointer(), []), "foreign_pointer")
    host = SimpleNamespace(module=module, _native_module_exports=None,
                           _manual_pointer_abi_functions=set(),
                           _slot_call_result_sink=lambda expr: object())
    # Rejection precedes operand evaluation or any attempt to use a builder.
    with pytest.raises(L1CodegenError, match="no managed result contract"):
        _scaffold_emit_declared_call(host, function.name, function.function_type.return_type,
                                     (), call_expr=object())
