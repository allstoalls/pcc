from pcc.frontends.c.parse.c_parser import CParser
from pcc.frontends.c.ssa import LatticeKind, SSABuilder, SSASCCPAnalyzer


_PARSER = CParser(lex_optimize=True, yacc_debug=False, yacc_optimize=True)


def _analyze(source: str):
    ast = _PARSER.parse(source)
    func = SSABuilder().build_function(ast.ext[0])
    return SSASCCPAnalyzer().analyze(func)


def test_ssa_sccp_folds_constant_branch_and_phi():
    result = _analyze(
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

    constants = result.constant_value_names()

    assert result.reachable_blocks == {"entry", "if.then.1", "if.end.3"}
    assert result.folded_branches == {"entry": "if.then.1"}
    assert 2 in constants.values()


def test_ssa_sccp_proves_phi_constant_when_all_reachable_inputs_match():
    result = _analyze(
        """
        int normalize(int x) {
            int y = 0;
            if (x < 0) {
                y = 3 + 4;
            } else {
                y = 14 / 2;
            }
            return y;
        }
        """
    )

    constants = result.constant_value_names()

    assert result.folded_branches == {}
    assert result.reachable_blocks == {"entry", "if.then.1", "if.else.2", "if.end.3"}
    assert 7 in constants.values()
    assert result.values["$t.0"].kind == LatticeKind.OVERDEFINED


def test_ssa_sccp_tracks_constant_expression_chain():
    result = _analyze(
        """
        int folded(void) {
            int x = 4;
            int y = x + 3;
            return y * 2;
        }
        """
    )

    constants = result.constant_value_names()

    assert constants["$t.0"] == 7
    assert constants["$t.1"] == 14
