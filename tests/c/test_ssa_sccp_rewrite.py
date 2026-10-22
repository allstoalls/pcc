from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.parse.c_parser import CParser
from pcc.frontends.c.passes import PassContext
from pcc.frontends.c.passes.ssa_sccp_rewrite import SSASCCPRewritePass


_PARSER = CParser(lex_optimize=True, yacc_debug=False, yacc_optimize=True)


def _run_rewrite(source: str):
    ast = _PARSER.parse(source)
    ctx = PassContext()
    out = SSASCCPRewritePass().run(ast, ctx)
    return ast, out, ctx


def test_ssa_sccp_rewrite_rewrites_join_constant_return_id():
    ast, out, ctx = _run_rewrite(
        """
        int f(int c) {
            int x = 0;
            if (c) {
                x = 7;
            } else {
                x = 7;
            }
            return x;
        }
        """
    )

    assert out is ast
    func = ast.ext[0]
    ret = func.body.block_items[2]
    assert isinstance(ret, c_ast.Return)
    assert isinstance(ret.expr, c_ast.Constant)
    assert ret.expr.value == "7"
    assert ctx.stats["ssa_sccp_rewrite.rewrite_return"] == 1


def test_ssa_sccp_rewrite_rewrites_join_constant_assignment_id():
    ast, out, ctx = _run_rewrite(
        """
        int f(int c) {
            int x = 0;
            int y = 0;
            if (c) {
                x = 7;
            } else {
                x = 7;
            }
            y = x;
            return y + 1;
        }
        """
    )

    assert out is ast
    func = ast.ext[0]
    assign = func.body.block_items[3]
    assert isinstance(assign, c_ast.Assignment)
    assert isinstance(assign.rvalue, c_ast.Constant)
    assert assign.rvalue.value == "7"
    assert ctx.stats["ssa_sccp_rewrite.rewrite_assign"] == 1


def test_ssa_sccp_rewrite_rewrites_join_constant_decl_init_id():
    ast, out, ctx = _run_rewrite(
        """
        int f(int c) {
            int x = 0;
            if (c) {
                x = 7;
            } else {
                x = 7;
            }
            int y = x;
            return y + 1;
        }
        """
    )

    assert out is ast
    func = ast.ext[0]
    decl = func.body.block_items[2]
    assert isinstance(decl, c_ast.Decl)
    assert isinstance(decl.init, c_ast.Constant)
    assert decl.init.value == "7"
    assert ctx.stats["ssa_sccp_rewrite.rewrite_decl"] == 1


def test_ssa_sccp_rewrite_skips_non_int_return_boundary():
    ast, out, ctx = _run_rewrite(
        """
        short f(int c) {
            short x = 0;
            if (c) {
                x = 7;
            } else {
                x = 7;
            }
            return x;
        }
        """
    )

    assert out is None
    func = ast.ext[0]
    ret = func.body.block_items[2]
    assert isinstance(ret, c_ast.Return)
    assert isinstance(ret.expr, c_ast.ID)
    assert "ssa_sccp_rewrite.rewrite_return" not in ctx.stats


def test_ssa_sccp_rewrite_skips_out_of_range_int_constant():
    ast, out, ctx = _run_rewrite(
        """
        int f(void) {
            int x = 50000 * 50000;
            return x;
        }
        """
    )

    assert out is None
    func = ast.ext[0]
    ret = func.body.block_items[1]
    assert isinstance(ret, c_ast.Return)
    assert isinstance(ret.expr, c_ast.ID)
    assert "ssa_sccp_rewrite.rewrite_return" not in ctx.stats


def test_ssa_sccp_rewrite_does_not_cross_wire_same_line_sites():
    ast, out, ctx = _run_rewrite("int f(int a){ int x; x=1; x=a; return x; }")

    assert out is None
    func = ast.ext[0]
    assign = func.body.block_items[2]
    ret = func.body.block_items[3]
    assert isinstance(assign, c_ast.Assignment)
    assert isinstance(assign.rvalue, c_ast.ID)
    assert isinstance(ret, c_ast.Return)
    assert isinstance(ret.expr, c_ast.ID)
    assert "ssa_sccp_rewrite.rewrite_assign" not in ctx.stats
