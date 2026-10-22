from pcc.frontends.c.parse.c_parser import CParser
from pcc.frontends.c.passes import PassContext
from pcc.frontends.c.passes.ssa_sccp import SSASCCPPass


_PARSER = CParser(lex_optimize=True, yacc_debug=False, yacc_optimize=True)


def test_ssa_sccp_pass_bootstraps_and_records_constants():
    ast = _PARSER.parse(
        """
        int choose(void) {
            int y = 1;
            if (1) {
                y = 2;
            } else {
                y = 3;
            }
            return y;
        }
        """
    )
    ctx = PassContext()

    out = SSASCCPPass().run(ast, ctx)

    assert out is None
    assert ctx.stats["ssa.bootstrap.success"] == 1
    assert ctx.stats["ssa.sccp.functions"] == 1
    assert ctx.stats["ssa.sccp.folded_branches"] == 1
    assert 2 in ctx.ssa_sccp_results["choose"].constant_value_names().values()
    assert any(
        entry.pass_name == "ssa-sccp"
        and entry.action == "analyzed"
        and entry.target == "choose"
        for entry in ctx.log
    )


def test_ssa_sccp_pass_ignores_unsupported_functions_after_bootstrap_skip():
    # Use a goto/label pattern the SSA builder explicitly rejects (Switch
    # is now lowered, so it no longer qualifies as a skip trigger).
    ast = _PARSER.parse(
        """
        int dispatch(int n) {
            if (n) goto out;
        out:
            return n;
        }
        """
    )
    ctx = PassContext()

    out = SSASCCPPass().run(ast, ctx)

    assert out is None
    assert ctx.stats["ssa.bootstrap.skipped"] == 1
    assert "ssa.sccp.functions" not in ctx.stats
    assert ctx.ssa_sccp_results == {}
