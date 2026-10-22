from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.parse.c_parser import CParser
from pcc.frontends.c.passes import PassContext
from pcc.frontends.c.passes.ipo_boundary import DeadArgElimAnalysisPass, ElimAvailExternPass


def _parse(code: str):
    return CParser().parse(code)


def _file_scope_decl_names(ast):
    return [
        ext.name
        for ext in ast.ext
        if isinstance(ext, c_ast.Decl) and getattr(ext, "name", None)
    ]


def test_deadargelim_analysis_records_unused_parameter():
    ast = _parse(
        """
        int callee(int x) { return x; }
        int wrapper(int live, int dead) { return callee(live); }
        """
    )
    ctx = PassContext()

    DeadArgElimAnalysisPass().run(ast, ctx)

    assert ctx.stats["deadargelim.dead_params"] == 1


def test_deadargelim_analysis_ignores_used_parameters():
    ast = _parse("int f(int x) { return x + 1; }")
    ctx = PassContext()

    DeadArgElimAnalysisPass().run(ast, ctx)

    assert "deadargelim.dead_params" not in ctx.stats


def test_elim_avail_extern_removes_unused_file_scope_extern_declaration():
    ast = _parse(
        """
        extern int helper(void);
        int main(void) { return 0; }
        """
    )
    ctx = PassContext()

    transformed = ElimAvailExternPass().run(ast, ctx) or ast

    assert _file_scope_decl_names(transformed) == []
    assert ctx.stats["elim_avail_extern.removed"] == 1


def test_elim_avail_extern_keeps_referenced_extern_declaration():
    ast = _parse(
        """
        extern int helper(void);
        int main(void) { return helper(); }
        """
    )
    ctx = PassContext()

    transformed = ElimAvailExternPass().run(ast, ctx) or ast

    assert _file_scope_decl_names(transformed) == ["helper"]
    assert "elim_avail_extern.removed" not in ctx.stats
