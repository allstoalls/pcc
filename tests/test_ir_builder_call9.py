"""Bounded host regressions for the nine-operand scaffold provider.

These checks execute the real builder and emit strict owned IR. They do not
build a native compiler or claim that a Stage1 link has been rerun.
"""

import inspect
import re

import pytest

from pcc.ir import ir
from tests.owned_ir_validation import verify_ir_text


pytestmark = pytest.mark.xdist_group("ir_builder_call9")
_SYMBOL = "user_pcc_ir_ir_IRBuilder_call9"


@pytest.fixture(autouse=True)
def _text_plane(monkeypatch):
    for name in (
        "PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "PCC_DIRECT_INDEXED_KERNEL_EMIT",
        "PCC_DIRECT_INDEXED_KERNEL_VALIDATE", "PCC_TEXT_INDEXED_KERNEL_EMIT",
    ):
        monkeypatch.setenv(name, "0")


def _definition(text, symbol):
    match = re.search(
        r"^define [^\n]*@" + re.escape(symbol) + r"\(([^\n]*)\)[^{]*\{(.*?)^}",
        text, re.M | re.S,
    )
    assert match is not None, symbol
    return match


def _codegen(source, module_name):
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module

    module = infer_module(parse_and_lift(source, "call9.py", module_name))
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    codegen._skip_program_main = True
    return codegen, module


@pytest.mark.parametrize("name_mode", ["default", "positional", "keyword"])
@pytest.mark.parametrize("indirect", [False, True])
def test_call9_forwards_all_operands_and_name(name_mode, indirect):
    helper = getattr(ir, "IRBuilder_call9", None)
    assert callable(helper), "missing IRBuilder_call9 provider"

    def emit(fixed):
        module = ir.Module(name="call9")
        ptr, i64 = ir.IntType(8).as_pointer(), ir.IntType(64)
        types = [ptr, ptr, ptr, ptr, ptr, ptr, i64, ptr, ptr]
        ftype = ir.FunctionType(i64, types)
        caller_types = [ftype.as_pointer(), *types] if indirect else types
        caller = ir.Function(module, ir.FunctionType(i64, caller_types), name="probe")
        builder = ir.IRBuilder(caller.append_basic_block("entry"))
        fn = caller.args[0] if indirect else ir.Function(module, ftype, name="target")
        operands = list(caller.args[1:] if indirect else caller.args)
        if not fixed:
            result = builder.call(fn, operands, name="" if name_mode == "default" else "ninth")
        elif name_mode == "default":
            result = helper(builder, fn, *operands)
        elif name_mode == "positional":
            result = helper(builder, fn, *operands, "ninth")
        else:
            result = helper(builder, fn, *operands, name="ninth")
        builder.ret(result)
        text = str(module)
        verify_ir_text(text)
        expected = ", ".join(str(value.type) + " " + str(value) for value in operands)
        assert "(" + expected + ")" in text
        if name_mode != "default":
            assert re.fullmatch(r"%ninth\.\d+", str(result))
        return text

    assert emit(True) == emit(False)


@pytest.mark.parametrize("container", ["list", "tuple"])
def test_nine_operand_scaffold_emits_matching_twelve_handle_abi(container, tmp_path):
    operands = ", ".join("a" + str(index) for index in range(9))
    arguments = "[" + operands + "]" if container == "list" else "(" + operands + ")"
    source = (
        "from pcc.ir.compat import ir\n"
        "def probe(fn, " + operands + "):\n"
        "    builder = ir.IRBuilder()\n"
        "    return builder.call(fn, " + arguments + ", name='ninth')\n"
    )
    codegen, module = _codegen(source, "call9_consumer")
    text = str(codegen.generate(module))
    verify_ir_text(text)
    (tmp_path / (container + ".ll")).write_text(text)
    body = _definition(text, "user_call9_consumer_probe").group(2)
    calls = re.findall(r"\bcall [^\n]*@" + _SYMBOL + r"\(([^\n]*)\)", body)
    assert len(calls) == 1
    assert len(calls[0].split(",")) == 12
    declaration = re.search(r"^declare [^\n]*@" + _SYMBOL + r"\(([^\n]*)\)", text, re.M)
    assert declaration is not None
    assert declaration.group(1).split(", ") == ["ptr"] * 12
    assert "py_cpy_" not in body and "strict.nolib.stub" not in text


def test_real_call9_body_emits_exact_provider_symbol(tmp_path):
    helper = getattr(ir, "IRBuilder_call9", None)
    assert callable(helper), "missing IRBuilder_call9 provider"
    # Compile the actual wrapper body using the real shared emitter's
    # declaration. Emitting the rest of the IR provider would be a bootstrap
    # workload; its body is exercised directly by the host tests above.
    source = inspect.getsource(ir._irbuilder_call_from_args_list) + "\n" + inspect.getsource(helper)
    codegen, module = _codegen(source, "pcc.ir.ir")
    core, wrapper = module.body
    codegen._declare_user_function(core)
    codegen._declare_user_function(wrapper)
    codegen._emit_user_function(wrapper)
    text = str(codegen.module)
    verify_ir_text(text)
    (tmp_path / "provider.ll").write_text(text)
    definition = _definition(text, _SYMBOL)
    assert len(definition.group(1).split(",")) == 12
    assert all(argument.strip().startswith("ptr ") for argument in definition.group(1).split(","))
    assert "@user_pcc_ir_ir__irbuilder_call_from_args_list(" in definition.group(2)
    assert "py_cpy_" not in definition.group(2) and "strict.nolib.stub" not in text
