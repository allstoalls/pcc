from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.parse.c_parser import CParser
from pcc.frontends.c.passes import PassContext
from pcc.frontends.c.passes.propagation import ExpressionReassociationPass


def _reassociated_return_expr(code: str, *, opt_level=None):
    ast = CParser().parse(code)
    ctx = PassContext(opt_level=opt_level)
    transformed = ExpressionReassociationPass().run(ast, ctx) or ast
    func = next(ext for ext in transformed.ext if isinstance(ext, c_ast.FuncDef))
    return func.body.block_items[0].expr


def test_reassociate_flattens_add_tree_and_combines_constants():
    expr = _reassociated_return_expr("int f(int x){ return 4 + (x + 5); }")

    assert isinstance(expr, c_ast.BinaryOp)
    assert expr.op == "+"
    assert isinstance(expr.left, c_ast.ID)
    assert expr.left.name == "x"
    assert isinstance(expr.right, c_ast.Constant)
    assert expr.right.value == "9"


def test_reassociate_flattens_mul_tree_and_combines_constants():
    expr = _reassociated_return_expr("int f(int a){ return 2 * (a * 3); }")

    assert isinstance(expr, c_ast.BinaryOp)
    assert expr.op == "*"
    assert isinstance(expr.left, c_ast.ID)
    assert expr.left.name == "a"
    assert isinstance(expr.right, c_ast.Constant)
    assert expr.right.value == "6"


def test_reassociate_keeps_side_effecting_calls_in_place():
    expr = _reassociated_return_expr(
        "int side(void); int f(void){ return 4 + (side() + 5); }"
    )

    assert isinstance(expr, c_ast.BinaryOp)
    assert expr.op == "+"
    assert isinstance(expr.left, c_ast.Constant)
    assert expr.left.value == "4"
    assert isinstance(expr.right, c_ast.BinaryOp)
    assert isinstance(expr.right.left, c_ast.FuncCall)
    assert isinstance(expr.right.right, c_ast.Constant)
    assert expr.right.right.value == "5"


def test_reassociate_skips_o0_frontend_pipeline():
    expr = _reassociated_return_expr(
        "int f(int x){ return 4 + (x + 5); }",
        opt_level=0,
    )

    assert isinstance(expr, c_ast.BinaryOp)
    assert expr.op == "+"
    assert isinstance(expr.left, c_ast.Constant)
    assert expr.left.value == "4"
    assert isinstance(expr.right, c_ast.BinaryOp)
    assert isinstance(expr.right.left, c_ast.ID)
    assert expr.right.left.name == "x"
    assert isinstance(expr.right.right, c_ast.Constant)
    assert expr.right.right.value == "5"
