"""Host and scaffold block creation share one function-local namespace."""

import pytest

from pcc.ir import ir


@pytest.mark.parametrize("route", ["function", "builder", "scaffold_function", "scaffold_builder"])
def test_block_names_are_unique_across_explicit_and_generated_names(route):
    module = ir.Module(name="block_names")
    names = ("then", "then", "then.1", "then", "", "", "bb0", "bb2", "")
    expected = ["then", "then.1", "then.1.1", "then.2", "bb0", "bb1", "bb0.1", "bb2", "bb2.1"]
    for function_name in ("first", "second"):
        fn = ir.Function(module, ir.FunctionType(ir.VoidType(), []), name=function_name)
        builder = ir.IRBuilder(fn.append_basic_block("entry"))
        blocks = []
        for name in names:
            if route == "function":
                block = fn.append_basic_block(name)
            elif route == "builder":
                block = builder.append_basic_block(name=name)
            elif route == "scaffold_function":
                block = ir.scaffold_Function_append_basic_block(fn, name)
            else:
                block = ir.scaffold_IRBuilder_append_basic_block(builder, name)
            blocks.append(block)
        assert [block.name for block in blocks] == expected
        assert len({block.name for block in fn.blocks}) == len(fn.blocks)
        assert all(block.parent is fn for block in blocks)


def test_scaffold_and_regular_calls_share_the_same_name_registry():
    fn = ir.Function(ir.Module(), ir.FunctionType(ir.VoidType(), []), name="mixed")
    first = fn.append_basic_block("then")
    builder = ir.IRBuilder(first)
    blocks = [first, ir.scaffold_Function_append_basic_block(fn, "then"),
              builder.append_basic_block("then"),
              ir.scaffold_IRBuilder_append_basic_block(builder, "then")]
    assert [block.name for block in blocks] == ["then", "then.1", "then.2", "then.3"]
