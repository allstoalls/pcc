from pcc.frontends.c.parse.c_parser import CParser
from pcc.frontends.c.ssa import SSAADCEAnalyzer, SSABuilder


_PARSER = CParser(lex_optimize=True, yacc_debug=False, yacc_optimize=True)


def _analyze(source: str):
    ast = _PARSER.parse(source)
    builder = SSABuilder()
    builder.index_file_scope(ast)
    func = builder.build_function(ast.ext[-1])
    return func, SSAADCEAnalyzer().analyze(func)


def test_ssa_adce_marks_dead_initializer_binding_before_live_overwrite():
    func, result = _analyze(
        """
        int f(int a) {
            int x = a + 1;
            x = 0;
            return x;
        }
        """
    )

    dead_coords = {
        binding.source_coord: binding.kind
        for binding in func.bindings
        if binding.source_coord in result.dead_bindings
    }

    assert dead_coords
    assert "decl_init" in dead_coords.values()


def test_ssa_adce_marks_dead_assignment_in_cross_block_branch():
    func, result = _analyze(
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

    dead_coords = {
        binding.source_coord: binding.kind
        for binding in func.bindings
        if binding.source_coord in result.dead_bindings
    }

    assert dead_coords
    assert "assign" in dead_coords.values()


def test_ssa_adce_keeps_binding_that_reaches_return():
    _func, result = _analyze(
        """
        int f(int a) {
            int x = a + 1;
            return x;
        }
        """
    )

    assert result.dead_bindings == {}


def test_ssa_adce_keeps_binding_that_feeds_live_call_argument():
    _func, result = _analyze(
        """
        int f(int a) {
            int x = a + 1;
            sink(x);
            return 0;
        }
        """
    )

    assert result.dead_bindings == {}


def test_ssa_adce_keeps_binding_that_feeds_live_store():
    _func, result = _analyze(
        """
        struct S {
            int mode;
        };

        int f(struct S *s, int a) {
            int x = a + 1;
            s->mode = x;
            return 0;
        }
        """
    )

    assert result.dead_bindings == {}
