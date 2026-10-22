from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.parse.c_parser import CParser
from pcc.frontends.c.passes import PassContext
from pcc.frontends.c.passes.lower_expect import LowerExpectPass


def _transformed_function(code: str):
    ast = CParser().parse(code)
    ctx = PassContext()
    transformed = LowerExpectPass().run(ast, ctx) or ast
    return next(ext for ext in transformed.ext if isinstance(ext, c_ast.FuncDef))


def test_lower_expect_lowers_builtin_expect_to_guarded_expr():
    func = _transformed_function(
        """
        int f(int x) {
            return __builtin_expect(x, 1);
        }
        """
    )

    ret = func.body.block_items[0]
    assert isinstance(ret, c_ast.Return)
    assert isinstance(ret.expr, c_ast.ID)
    assert ret.expr.name == "x"


def test_lower_expect_lowers_builtin_expect_with_probability():
    func = _transformed_function(
        """
        int f(int x) {
            return __builtin_expect_with_probability(x, 1, 0.9);
        }
        """
    )

    ret = func.body.block_items[0]
    assert isinstance(ret, c_ast.Return)
    assert isinstance(ret.expr, c_ast.ID)
    assert ret.expr.name == "x"


def test_lower_expect_keeps_builtin_expect_when_hint_operand_has_side_effects():
    func = _transformed_function(
        """
        int side_effect(void);
        int f(int x) {
            return __builtin_expect(x, side_effect());
        }
        """
    )

    ret = func.body.block_items[0]
    assert isinstance(ret, c_ast.Return)
    assert isinstance(ret.expr, c_ast.FuncCall)
    assert isinstance(ret.expr.name, c_ast.ID)
    assert ret.expr.name.name == "__builtin_expect"
