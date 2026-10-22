from pcc.frontends.c.parse.c_parser import CParser
from pcc.frontends.c.ssa import SSABuilder, SSAGVNAnalyzer


_PARSER = CParser(lex_optimize=True, yacc_debug=False, yacc_optimize=True)


def _analyze(source: str):
    ast = _PARSER.parse(source)
    func = SSABuilder().build_function(ast.ext[0])
    return SSAGVNAnalyzer().analyze(func)


def test_ssa_gvn_finds_redundant_expression_in_same_block():
    result = _analyze(
        """
        int same(int a, int b) {
            int x = a + b;
            int y = a + b;
            return y - x;
        }
        """
    )

    assert result.expressions_seen == 3
    assert result.redundant_value_names() == {"$t.1": "$t.0"}


def test_ssa_gvn_uses_dominator_scope_across_child_blocks():
    result = _analyze(
        """
        int nested(int a, int b, int flag) {
            int x = a + b;
            if (flag) {
                return a + b;
            }
            return x;
        }
        """
    )

    assert result.redundant_value_names() == {"$t.1": "$t.0"}


def test_ssa_gvn_does_not_treat_phi_dependent_expression_as_redundant():
    result = _analyze(
        """
        int merge(int a, int b, int flag) {
            int x = a + b;
            if (flag) {
                a = a + 1;
            }
            return a + b;
        }
        """
    )

    assert result.redundant_value_names() == {}
