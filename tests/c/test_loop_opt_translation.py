from pcc.frontends.c.parse.c_parser import CParser
from pcc.frontends.c.passes import PassContext
from pcc.frontends.c.passes.loop_opt import LoopOptPass


def _loop_opt_context(code: str):
    ast = CParser().parse(code)
    ctx = PassContext()
    LoopOptPass().run(ast, ctx)
    return ctx


def test_loop_opt_records_memset_idiom_candidate():
    ctx = _loop_opt_context(
        """
        void fill(int *p, int n) {
            for (int i = 0; i < n; ++i) {
                p[i] = 0;
            }
        }
        """
    )

    assert ctx.stats["loop_opt.memset_idiom_candidates"] == 1


def test_loop_opt_records_memcpy_idiom_candidate():
    ctx = _loop_opt_context(
        """
        void copy(int *dst, int *src, int n) {
            for (int i = 0; i < n; ++i) {
                dst[i] = src[i];
            }
        }
        """
    )

    assert ctx.stats["loop_opt.memcpy_idiom_candidates"] == 1


def test_loop_opt_skips_non_idiom_store_loop():
    ctx = _loop_opt_context(
        """
        void fill(int *p, int n) {
            for (int i = 0; i < n; ++i) {
                p[i] = i;
            }
        }
        """
    )

    assert "loop_opt.memset_idiom_candidates" not in ctx.stats
    assert "loop_opt.memcpy_idiom_candidates" not in ctx.stats


def test_loop_opt_records_loop_sink_candidate():
    ctx = _loop_opt_context(
        """
        int f(int *p, int cond) {
            int sum = 0;
            for (int i = 0; i < 4; ++i) {
                int t = *p;
                if (cond) sum += t;
            }
            return sum;
        }
        """
    )

    assert ctx.stats["loop_opt.sink_candidates"] == 1


def test_loop_opt_skips_loop_sink_candidate_when_value_used_after_if():
    ctx = _loop_opt_context(
        """
        int f(int *p, int cond) {
            int sum = 0;
            for (int i = 0; i < 4; ++i) {
                int t = *p;
                if (cond) sum += t;
                sum += t;
            }
            return sum;
        }
        """
    )

    assert "loop_opt.sink_candidates" not in ctx.stats
