from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.parse.c_parser import CParser
from pcc.frontends.c.passes import PassContext
from pcc.frontends.c.passes.canonicalize import CanonicalizerPass


def _canonicalized_return_expr(code: str):
    ast = CParser().parse(code)
    ctx = PassContext()
    transformed = CanonicalizerPass().run(ast, ctx) or ast
    func = next(ext for ext in transformed.ext if isinstance(ext, c_ast.FuncDef))
    stmt = func.body.block_items[0]
    assert isinstance(stmt, c_ast.Return)
    return stmt.expr


def test_instsimplify_elides_add_zero():
    expr = _canonicalized_return_expr("int f(int x){ return x + 0; }")

    assert isinstance(expr, c_ast.ID)
    assert expr.name == "x"


def test_instsimplify_folds_constant_ternary_condition():
    expr = _canonicalized_return_expr("int f(void){ return 1 ? 4 : 5; }")

    assert isinstance(expr, c_ast.Constant)
    assert expr.type == "int"
    assert expr.value == "4"


def test_instsimplify_keeps_mul_zero_when_other_side_has_side_effect():
    expr = _canonicalized_return_expr(
        "int side(void); int f(void){ return side() * 0; }"
    )

    assert isinstance(expr, c_ast.BinaryOp)
    assert expr.op == "*"
    assert isinstance(expr.left, c_ast.FuncCall)
    assert isinstance(expr.right, c_ast.Constant)
    assert expr.right.value == "0"
