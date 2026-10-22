from __future__ import annotations

from pcc.ir import ir


def test_function_is_value_for_type_inference_and_builder_apis():
    assert issubclass(ir.Function, ir.Value)


def test_function_instances_can_be_used_as_operands():
    module = ir.Module(name="m")
    fn_ty = ir.FunctionType(ir.VoidType(), ())
    fn = ir.Function(module, fn_ty, name="f")
    assert isinstance(fn, ir.Value)
    assert str(fn).startswith("@")
    assert hasattr(fn, "type")
