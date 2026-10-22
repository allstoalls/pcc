from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.parse.c_parser import CParser
from pcc.frontends.c.passes import PassContext
from pcc.frontends.c.passes.ssa_adce import SSAADCEPass


_PARSER = CParser(lex_optimize=True, yacc_debug=False, yacc_optimize=True)


def _run_pass(source: str):
    ast = _PARSER.parse(source)
    ctx = PassContext()
    out = SSAADCEPass().run(ast, ctx)
    return ast, out, ctx


def test_ssa_adce_pass_clears_dead_initializer_before_live_overwrite():
    ast, out, ctx = _run_pass(
        """
        int f(int a) {
            int x = a + 1;
            x = 0;
            return x;
        }
        """
    )

    assert out is ast
    func = ast.ext[0]
    decl = func.body.block_items[0]
    assert isinstance(decl, c_ast.Decl)
    assert decl.init is None
    assert ctx.stats["ssa.adce.functions"] == 1
    assert ctx.stats["ssa_adce.drop_init"] == 1


def test_ssa_adce_pass_removes_dead_assignment_in_cross_block_branch():
    ast, out, ctx = _run_pass(
        """
        int f(int a, int flag) {
            int x;
            if (flag) {
                x = a + 1;
            }
            return 0;
        }
        """
    )

    assert out is ast
    func = ast.ext[0]
    branch = func.body.block_items[1]
    assert isinstance(branch, c_ast.If)
    assert branch.iftrue is None or not branch.iftrue.block_items
    assert ctx.stats["ssa_adce.drop_assign"] == 1


def test_ssa_adce_pass_keeps_live_initializer():
    ast, out, ctx = _run_pass(
        """
        int f(int a) {
            int x = a + 1;
            return x;
        }
        """
    )

    assert out is None
    func = ast.ext[0]
    decl = func.body.block_items[0]
    assert isinstance(decl, c_ast.Decl)
    assert isinstance(decl.init, c_ast.BinaryOp)
    assert "ssa_adce.drop_init" not in ctx.stats


def test_ssa_adce_pass_keeps_initializer_used_via_call_argument():
    ast, out, ctx = _run_pass(
        """
        int id(int x);

        int f(void) {
            int z = 7;
            return id(z);
        }
        """
    )

    assert out is None
    func = ast.ext[1]
    decl = func.body.block_items[0]
    assert isinstance(decl, c_ast.Decl)
    assert isinstance(decl.init, c_ast.Constant)
    assert decl.init.value == "7"
    assert "ssa_adce.drop_init" not in ctx.stats
