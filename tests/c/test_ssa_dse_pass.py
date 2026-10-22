from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.parse.c_parser import CParser
from pcc.frontends.c.passes import PassContext
from pcc.frontends.c.passes.ssa_dse import SSADSEPass


_PARSER = CParser(lex_optimize=True, yacc_debug=False, yacc_optimize=True)


def _run_pass(source: str):
    ast = _PARSER.parse(source)
    ctx = PassContext()
    out = SSADSEPass().run(ast, ctx)
    return ast, out, ctx


def test_ssa_dse_pass_rewrites_dead_effectful_assignment_to_expr_statement():
    ast, out, ctx = _run_pass(
        """
        int f(int a) {
            int x;
            x = side_effect();
            x = a + 2;
            return x;
        }
        """
    )

    assert out is ast
    func = ast.ext[0]
    block_items = func.body.block_items

    decl = block_items[0]
    effect_stmt = block_items[1]
    live_assign = block_items[2]

    assert isinstance(decl, c_ast.Decl)
    assert decl.init is None
    assert isinstance(effect_stmt, c_ast.FuncCall)
    assert isinstance(effect_stmt.name, c_ast.ID)
    assert effect_stmt.name.name == "side_effect"
    assert isinstance(live_assign, c_ast.Assignment)
    assert ctx.stats["ssa.dse.functions"] == 1
    assert ctx.stats["ssa_dse.preserve_effect_assign"] == 1


def test_ssa_dse_pass_rewrites_dead_effectful_initializer_to_decl_plus_call():
    ast, out, ctx = _run_pass(
        """
        int f(int a) {
            int x = side_effect();
            x = a + 2;
            return x;
        }
        """
    )

    assert out is ast
    func = ast.ext[0]
    block_items = func.body.block_items

    decl = block_items[0]
    effect_stmt = block_items[1]
    live_assign = block_items[2]

    assert isinstance(decl, c_ast.Decl)
    assert decl.init is None
    assert isinstance(effect_stmt, c_ast.FuncCall)
    assert isinstance(effect_stmt.name, c_ast.ID)
    assert effect_stmt.name.name == "side_effect"
    assert isinstance(live_assign, c_ast.Assignment)
    assert ctx.stats["ssa_dse.preserve_effect_init"] == 1


def test_ssa_dse_pass_keeps_live_effectful_initializer():
    ast, out, ctx = _run_pass(
        """
        int f(void) {
            int x = side_effect();
            return x;
        }
        """
    )

    assert out is None
    func = ast.ext[0]
    decl = func.body.block_items[0]
    assert isinstance(decl, c_ast.Decl)
    assert isinstance(decl.init, c_ast.FuncCall)
    assert "ssa_dse.preserve_effect_init" not in ctx.stats


def test_ssa_dse_pass_leaves_pure_dead_initializer_for_ssa_adce():
    ast, out, ctx = _run_pass(
        """
        int f(int a) {
            int x = a + 1;
            x = 0;
            return x;
        }
        """
    )

    assert out is None
    func = ast.ext[0]
    decl = func.body.block_items[0]
    assert isinstance(decl, c_ast.Decl)
    assert isinstance(decl.init, c_ast.BinaryOp)
    assert "ssa_dse.preserve_effect_init" not in ctx.stats


def test_ssa_dse_pass_rewrites_dead_effectful_initializer_after_if_chain():
    ast, out, ctx = _run_pass(
        """
        int f(int a, int b, int c, int d, int e) {
            int x = side_effect();
            if (a) { }
            if (b) { }
            if (c) { }
            if (d) { }
            if (e) { }
            x = 0;
            return x;
        }
        """
    )

    assert out is ast
    func = ast.ext[0]
    block_items = func.body.block_items

    decl = block_items[0]
    effect_stmt = block_items[1]
    live_assign = block_items[7]

    assert isinstance(decl, c_ast.Decl)
    assert decl.init is None
    assert isinstance(effect_stmt, c_ast.FuncCall)
    assert isinstance(effect_stmt.name, c_ast.ID)
    assert effect_stmt.name.name == "side_effect"
    assert isinstance(live_assign, c_ast.Assignment)
    assert ctx.stats["ssa_dse.preserve_effect_init"] == 1
