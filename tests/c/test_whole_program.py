"""Phase 6 tests: whole-program / cross-TU analysis."""

from pcc.frontends.c.parse.c_parser import CParser
from pcc.frontends.c.passes import PassContext
from pcc.frontends.c.passes.whole_program import WholeProgramAnalyzer
from pcc.frontends.c.passes.whole_program_pass import WholeProgramAnalysisPass


_PARSER = CParser(lex_optimize=True, yacc_debug=False, yacc_optimize=True)


def _parse(src: str):
    return _PARSER.parse(src)


def test_whole_program_detects_specialization_candidate_same_constant():
    tu1 = _parse(
        """
        static int scale(int v, int factor) { return v * factor; }
        int compute_a(int x) { return scale(x, 10); }
        int compute_b(int y) { return scale(y, 10); }
        int main(void) { return compute_a(1) + compute_b(2); }
        """
    )
    result = WholeProgramAnalyzer().analyze([("tu1.c", tu1)])

    assert "scale" in result.specialization_candidates
    assert result.specialization_candidates["scale"] == {1: 10}


def test_whole_program_rejects_specialization_on_mixed_constants():
    tu1 = _parse(
        """
        static int scale(int v, int factor) { return v * factor; }
        int compute_a(int x) { return scale(x, 10); }
        int compute_b(int y) { return scale(y, 20); }
        int main(void) { return compute_a(1) + compute_b(2); }
        """
    )
    result = WholeProgramAnalyzer().analyze([("tu1.c", tu1)])

    # factor has two distinct constants → not specializable to one value.
    assert "scale" not in result.specialization_candidates
    assert result.const_args["scale"][1] == {10, 20}


def test_whole_program_detects_dead_internal_function():
    tu1 = _parse(
        """
        static int unused_helper(int x) { return x * 2; }
        int main(void) { return 0; }
        """
    )
    result = WholeProgramAnalyzer().analyze([("tu1.c", tu1)])

    assert "unused_helper" in result.dead_internal_functions


def test_whole_program_pass_records_stats_on_ctx():
    tu1 = _parse(
        """
        static int scale(int v, int factor) { return v * factor; }
        int compute_a(int x) { return scale(x, 10); }
        int compute_b(int y) { return scale(y, 10); }
        int main(void) { return compute_a(1) + compute_b(2); }
        """
    )
    ctx = PassContext()
    ctx.whole_program_asts = [("tu1.c", tu1)]
    WholeProgramAnalysisPass().run(tu1, ctx)

    assert ctx.stats.get("whole_program.functions") == 4
    assert ctx.stats.get("whole_program.specialization_candidates") == 1
    assert hasattr(ctx, "whole_program_result")
    assert "scale" in ctx.whole_program_result.specialization_candidates


def test_whole_program_handles_cross_tu_calls():
    tu1 = _parse(
        """
        int shared(int a, int b);
        int caller_a(int x) { return shared(x, 100); }
        """
    )
    tu2 = _parse(
        """
        int shared(int a, int b) { return a + b; }
        int caller_b(int y) { return shared(y, 100); }
        """
    )
    result = WholeProgramAnalyzer().analyze([("tu1.c", tu1), ("tu2.c", tu2)])

    # Call sites from both TUs to `shared`:
    shared_calls = [c for c in result.call_sites if c.callee == "shared"]
    assert len(shared_calls) == 2
    # Both pass 100 as second arg.
    assert result.const_args["shared"][1] == {100}
    # But `shared` has default linkage, so it's NOT a specialization
    # candidate (only internal linkage functions are).
    assert "shared" not in result.specialization_candidates
