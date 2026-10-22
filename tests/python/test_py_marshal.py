from pcc.ir.compat import ir
from pcc.frontends.python.codegen import marshal
from pcc.frontends.python.py_ast import Type


def _pointer_arg():
    module = ir.Module(name="marshal_type_probe")
    ptr_ty = ir.IntType(8).as_pointer()
    fn_ty = ir.FunctionType(ptr_ty, (ptr_ty,))
    fn = ir.Function(module, fn_ty, name="probe")
    block = fn.append_basic_block("entry")
    builder = ir.IRBuilder(block)
    return module, builder, fn.args[0]


def test_marshal_type_base_object_passes_pointer_through():
    module, builder, value = _pointer_arg()

    out = marshal.marshal_to_object(
        builder,
        module,
        {},
        value,
        Type(name="type"),
    )

    assert out is value


def test_marshal_from_object_type_base_passes_pointer_through():
    _module, builder, value = _pointer_arg()

    out = marshal.marshal_from_object(
        builder,
        {},
        {},
        value,
        Type(name="type"),
    )

    assert out is value


def test_marshal_type_named_duplicate_passes_pointer_through():
    class Type:
        pass

    module, builder, value = _pointer_arg()

    out = marshal.marshal_to_object(builder, module, {}, value, Type())

    assert out is value
