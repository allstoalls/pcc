import pytest

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
from pcc.frontends.c.parse.c_parser import CParser
from pcc.frontends.c.passes import PassContext, PassPipeline
from pcc.frontends.c.passes.dce import DCEPass
from pcc.driver.project import TranslationUnit


def _transformed_function(code: str):
    ast = CParser().parse(code)
    ctx = PassContext()
    PassPipeline.minimal().run_high_tier(ast, ctx)
    transformed = DCEPass().run(ast, ctx) or ast
    return next(ext for ext in transformed.ext if isinstance(ext, c_ast.FuncDef))


def test_dce_removes_overwritten_plain_dead_store():
    func = _transformed_function(
        """
        int f(int a) {
            int x = 0;
            x = a + 1;
            x = a + 2;
            return x;
        }
        """
    )

    assert len(func.body.block_items) == 3
    assign = func.body.block_items[1]
    assert isinstance(assign, c_ast.Assignment)
    assert isinstance(assign.rvalue, c_ast.BinaryOp)
    assert isinstance(assign.rvalue.right, c_ast.Constant)
    assert assign.rvalue.right.value == "2"


def test_dce_keeps_store_that_is_read_before_overwrite():
    func = _transformed_function(
        """
        int f(int a) {
            int x = 0;
            x = a + 1;
            int y = x;
            x = a + 2;
            return x + y;
        }
        """
    )

    first_assign = func.body.block_items[1]
    assert isinstance(first_assign, c_ast.Assignment)
    assert isinstance(first_assign.rvalue, c_ast.BinaryOp)
    assert isinstance(first_assign.rvalue.right, c_ast.Constant)
    assert first_assign.rvalue.right.value == "1"


def test_dce_keeps_dead_store_when_rhs_has_side_effects():
    func = _transformed_function(
        """
        int side_effect(void);
        int f(int a) {
            int x = 0;
            x = side_effect();
            x = a + 2;
            return x;
        }
        """
    )

    side_effect_stmt = func.body.block_items[1]
    assert isinstance(side_effect_stmt, c_ast.Assignment)
    assert isinstance(side_effect_stmt.rvalue, c_ast.FuncCall)


def test_dce_does_not_remove_store_to_global_that_is_observed_across_functions():
    ast = CParser().parse(
        """
        int g;
        int effect(void) {
            g = 1;
            return 1;
        }
        """
    )
    ctx = PassContext()
    PassPipeline.minimal().run_high_tier(ast, ctx)
    transformed = DCEPass().run(ast, ctx) or ast
    func = next(
        ext for ext in transformed.ext
        if isinstance(ext, c_ast.FuncDef) and ext.decl.name == "effect"
    )

    stmt = func.body.block_items[0]
    assert isinstance(stmt, c_ast.Assignment)
    assert isinstance(stmt.lvalue, c_ast.ID)
    assert stmt.lvalue.name == "g"


def test_dce_keeps_store_to_address_taken_local():
    func = _transformed_function(
        """
        int f(void) {
            int a;
            int *p;
            p = &a;
            a = 12;
            return *p;
        }
        """
    )

    assign = next(
        item
        for item in func.body.block_items
        if isinstance(item, c_ast.Assignment)
        and isinstance(item.lvalue, c_ast.ID)
        and item.lvalue.name == "a"
    )
    assert isinstance(assign, c_ast.Assignment)
    assert isinstance(assign.lvalue, c_ast.ID)
    assert assign.lvalue.name == "a"


def test_dce_does_not_mask_invalid_dead_store_cast_semantics():
    evaluator = CEvaluator()
    unit = TranslationUnit(
        "check_cast.c",
        "check_cast.c",
        """
        struct foo { int a; };
        int main(void) {
            struct foo xxx;
            int i;
            xxx = (struct foo)1;
            i = (int)xxx;
            return 0;
        }
        """,
    )

    with pytest.raises(ValueError, match="invalid cast"):
        evaluator.compile_translation_units([unit], use_compile_cache=False)
