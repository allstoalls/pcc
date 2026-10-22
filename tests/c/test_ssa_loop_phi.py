"""Phase 5 tests: SSA-backed loop-phi classification."""

from pcc.frontends.c.parse.c_parser import CParser
from pcc.frontends.c.passes import PassContext
from pcc.frontends.c.passes.ssa_loop_phi import SSALoopPhiPass
from pcc.frontends.c.ssa import LoopPhiKind


_PARSER = CParser(lex_optimize=True, yacc_debug=False, yacc_optimize=True)


def _analyze(src: str) -> tuple[PassContext, dict]:
    ast = _PARSER.parse(src)
    ctx = PassContext()
    SSALoopPhiPass().run(ast, ctx)
    return ctx, getattr(ctx, "ssa_loop_phi_results", {})


def test_loop_phi_identifies_basic_induction():
    ctx, results = _analyze(
        """
        int f(int n) {
            int i = 0;
            while (i < n) {
                i = i + 1;
            }
            return i;
        }
        """
    )
    assert ctx.stats.get("ssa.loop_phi.induction", 0) >= 1
    f_classes = {
        c.variable_name: c for c in results["f"].classifications
    }
    assert f_classes["i"].kind == LoopPhiKind.INDUCTION
    assert f_classes["i"].step == 1


def test_loop_phi_identifies_decrement_induction():
    ctx, results = _analyze(
        """
        int f(int n) {
            int i = n;
            while (i > 0) {
                i = i - 1;
            }
            return i;
        }
        """
    )
    assert ctx.stats.get("ssa.loop_phi.induction", 0) >= 1
    f_classes = {
        c.variable_name: c for c in results["f"].classifications
    }
    assert f_classes["i"].kind == LoopPhiKind.INDUCTION
    assert f_classes["i"].step == -1


def test_loop_phi_identifies_sum_reduction():
    ctx, results = _analyze(
        """
        int f(int n) {
            int i = 0;
            int sum = 0;
            while (i < n) {
                sum = sum + i;
                i = i + 1;
            }
            return sum;
        }
        """
    )
    assert ctx.stats.get("ssa.loop_phi.reduction", 0) >= 1
    f_classes = {
        c.variable_name: c for c in results["f"].classifications
    }
    assert f_classes["sum"].kind == LoopPhiKind.REDUCTION
    assert f_classes["sum"].op == "+"
    assert f_classes["i"].kind == LoopPhiKind.INDUCTION


def test_loop_phi_identifies_xor_reduction():
    ctx, results = _analyze(
        """
        int f(int n) {
            int i = 0;
            int r = 0;
            while (i < n) {
                r = r ^ i;
                i = i + 1;
            }
            return r;
        }
        """
    )
    f_classes = {
        c.variable_name: c for c in results["f"].classifications
    }
    assert f_classes["r"].kind == LoopPhiKind.REDUCTION
    assert f_classes["r"].op == "^"


def test_loop_phi_no_headers_no_classifications():
    ctx, results = _analyze(
        """
        int f(int a, int b) {
            return a + b;
        }
        """
    )
    # No loop headers → no classifications.
    assert not results["f"].header_blocks
    assert not results["f"].classifications
