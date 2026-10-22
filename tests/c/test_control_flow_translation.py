from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.parse.c_parser import CParser
from pcc.frontends.c.passes import PassContext
from pcc.frontends.c.passes.canonicalize import CanonicalizerPass
from pcc.frontends.c.passes.control_flow import ControlFlowPass


def _transformed_function(code: str):
    ast = CParser().parse(code)
    ctx = PassContext()
    transformed = ControlFlowPass().run(ast, ctx) or ast
    return next(ext for ext in transformed.ext if isinstance(ext, c_ast.FuncDef))


def _refined_function(code: str):
    ast = CParser().parse(code)
    ctx = PassContext()
    ast = ControlFlowPass().run(ast, ctx) or ast
    ast = CanonicalizerPass().run(ast, ctx) or ast
    return next(ext for ext in ast.ext if isinstance(ext, c_ast.FuncDef))


def test_control_flow_merges_nested_if_conditions():
    func = _transformed_function(
        """
        int f(int x, int y) {
            if (x) {
                if (y) x = 1;
            }
            return x;
        }
        """
    )

    stmt = func.body.block_items[0]
    assert isinstance(stmt, c_ast.If)
    assert isinstance(stmt.cond, c_ast.BinaryOp)
    assert stmt.cond.op == "&&"
    assert isinstance(stmt.iftrue, c_ast.Assignment)
    assert stmt.iffalse is None


def test_control_flow_negates_empty_then_branch():
    func = _transformed_function(
        """
        int f(int x, int y) {
            if (x) ;
            else y = 1;
            return y;
        }
        """
    )

    stmt = func.body.block_items[0]
    assert isinstance(stmt, c_ast.If)
    assert isinstance(stmt.cond, c_ast.UnaryOp)
    assert stmt.cond.op == "!"
    assert isinstance(stmt.iftrue, c_ast.Assignment)
    assert stmt.iffalse is None


def test_control_flow_converts_simple_if_else_assignment_to_ternary():
    func = _transformed_function(
        """
        int f(int c, int a, int b) {
            int x = 0;
            if (c) x = a;
            else x = b;
            return x;
        }
        """
    )

    stmt = func.body.block_items[1]
    assert isinstance(stmt, c_ast.Assignment)
    assert isinstance(stmt.lvalue, c_ast.ID)
    assert stmt.lvalue.name == "x"
    assert isinstance(stmt.rvalue, c_ast.TernaryOp)


def test_control_flow_converts_simple_if_else_return_to_ternary_return():
    func = _transformed_function(
        """
        int f(int c, int a, int b) {
            if (c) return a;
            else return b;
        }
        """
    )

    stmt = func.body.block_items[0]
    assert isinstance(stmt, c_ast.Return)
    assert isinstance(stmt.expr, c_ast.TernaryOp)


def test_control_flow_converts_fallthrough_return_to_ternary_return():
    func = _transformed_function(
        """
        int f(int c, int a, int b) {
            if (c) return a;
            return b;
        }
        """
    )

    assert len(func.body.block_items) == 1
    stmt = func.body.block_items[0]
    assert isinstance(stmt, c_ast.Return)
    assert isinstance(stmt.expr, c_ast.TernaryOp)


def test_control_flow_elides_identical_if_arms():
    func = _transformed_function(
        """
        int f(int c, int a) {
            int x = 0;
            if (c) x = a;
            else x = a;
            return x;
        }
        """
    )

    stmt = func.body.block_items[1]
    assert isinstance(stmt, c_ast.Assignment)
    assert isinstance(stmt.lvalue, c_ast.ID)
    assert stmt.lvalue.name == "x"
    assert isinstance(stmt.rvalue, c_ast.ID)
    assert stmt.rvalue.name == "a"


def test_control_flow_dedupes_nested_same_condition():
    func = _transformed_function(
        """
        int f(int x, int y) {
            if (x) {
                if (x) y = 1;
            }
            return y;
        }
        """
    )

    stmt = func.body.block_items[0]
    assert isinstance(stmt, c_ast.If)
    assert isinstance(stmt.cond, c_ast.ID)
    assert stmt.cond.name == "x"
    assert isinstance(stmt.iftrue, c_ast.Assignment)


def test_control_flow_dedupes_nested_same_condition_inside_loop_body():
    func = _transformed_function(
        """
        int f(int x) {
            int sum = 0;
            for (int i = 0; i < 4; ++i) {
                if (x) {
                    if (x) sum += i;
                }
            }
            return sum;
        }
        """
    )

    loop = func.body.block_items[1]
    assert isinstance(loop, c_ast.For)
    loop_body = loop.stmt
    assert isinstance(loop_body, c_ast.Compound)
    stmt = loop_body.block_items[0]
    assert isinstance(stmt, c_ast.If)
    assert isinstance(stmt.cond, c_ast.ID)
    assert stmt.cond.name == "x"
    assert isinstance(stmt.iftrue, c_ast.Assignment)


def test_control_flow_collapses_repeated_same_guard_return_chain():
    func = _transformed_function(
        """
        int f(int x) {
            if (x) return 1;
            if (x) return 2;
            return 0;
        }
        """
    )

    assert len(func.body.block_items) == 1
    stmt = func.body.block_items[0]
    assert isinstance(stmt, c_ast.Return)
    assert isinstance(stmt.expr, c_ast.TernaryOp)


def test_control_flow_merges_related_comparisons_into_single_return_guard():
    func = _transformed_function(
        """
        int f(int x) {
            if (x > 3) {
                if (x > 1) return 1;
            }
            return 0;
        }
        """
    )

    assert len(func.body.block_items) == 1
    stmt = func.body.block_items[0]
    assert isinstance(stmt, c_ast.Return)
    assert isinstance(stmt.expr, c_ast.TernaryOp)
    assert isinstance(stmt.expr.cond, c_ast.BinaryOp)
    assert stmt.expr.cond.op == "&&"


def test_control_flow_and_canonicalize_collapse_guarded_pure_return():
    func = _refined_function(
        """
        int f(int c, int x) {
            if (c) return x + 0;
            return 0;
        }
        """
    )

    assert len(func.body.block_items) == 1
    stmt = func.body.block_items[0]
    assert isinstance(stmt, c_ast.Return)
    assert isinstance(stmt.expr, c_ast.TernaryOp)
    assert isinstance(stmt.expr.iftrue, c_ast.ID)
    assert stmt.expr.iftrue.name == "x"
