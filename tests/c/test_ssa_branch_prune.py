from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
from pcc.frontends.c.parse.c_parser import CParser
from pcc.frontends.c.passes import PassContext
from pcc.frontends.c.passes.ssa_branch_prune import SSABranchPrunePass
from pcc.driver.project import TranslationUnit
from tests.owned_c_corpus import run_owned_c_corpus


_PARSER = CParser(lex_optimize=True, yacc_debug=False, yacc_optimize=True)


def _collect_ifs(node):
    found = []

    def _walk(current):
        if current is None:
            return
        if isinstance(current, c_ast.If):
            found.append(current)
        for _, child in current.children():
            if isinstance(child, c_ast.Node):
                _walk(child)
            elif isinstance(child, list):
                for item in child:
                    if isinstance(item, c_ast.Node):
                        _walk(item)

    _walk(node)
    return found


def test_ssa_branch_prune_removes_join_proven_constant_if():
    ast = _PARSER.parse(
        """
        int f(int c) {
            int x = 0;
            int y = 0;
            if (c) {
                x = 1;
            } else {
                x = 1;
            }
            if (x) {
                y = 7;
                y = y + 1;
            } else {
                y = 9;
                y = y + 1;
            }
            return y;
        }
        """
    )
    ctx = PassContext()

    out = SSABranchPrunePass().run(ast, ctx)

    assert out is ast
    func = ast.ext[0]
    ifs = _collect_ifs(func)
    assert len(ifs) == 1
    assert ctx.stats["ssa.bootstrap.success"] == 1
    assert ctx.stats["ssa.sccp.folded_branches"] == 1
    assert ctx.stats["ssa_branch_prune.fold_true"] == 1
    assert any(
        entry.pass_name == "ssa-branch-prune"
        and entry.action == "fold_true"
        for entry in ctx.log
    )


def test_ssa_branch_prune_skips_width_sensitive_unsigned_like_condition():
    ast = _PARSER.parse(
        """
        int f(void) {
            int x = 0;
            x = ~x;
            if (x != 0xffffffff) {
                return 1;
            }
            return 0;
        }
        """
    )
    ctx = PassContext()

    out = SSABranchPrunePass().run(ast, ctx)

    assert out is None
    assert "ssa_branch_prune.fold_true" not in ctx.stats
    assert "ssa_branch_prune.fold_false" not in ctx.stats


def test_ssa_branch_prune_folds_if_on_short_circuit_value_result():
    ast = _PARSER.parse(
        """
        int helper(void);

        int f(void) {
            int y = 1 || helper();
            if (y) {
                return 7;
            }
            return 0;
        }
        """
    )
    ctx = PassContext()

    out = SSABranchPrunePass().run(ast, ctx)

    assert out is ast
    func = ast.ext[1]
    ifs = _collect_ifs(func)
    assert len(ifs) == 0
    assert ctx.stats["ssa.bootstrap.success"] == 1
    assert ctx.stats["ssa.sccp.folded_branches"] >= 1
    assert ctx.stats["ssa_branch_prune.fold_true"] >= 1


def test_ssa_branch_prune_preserves_short_circuit_global_side_effect_runtime():
    source = r"""
        int g;

        int effect(void) {
            g = 1;
            return 1;
        }

        int main(void) {
            int x;

            g = 0;
            x = 0;
            if (x && effect())
                return 1;
            if (g)
                return 2;

            x = 1;
            if (x && effect()) {
                if (g != 1)
                    return 3;
            } else {
                return 4;
            }

            g = 0;
            x = 1;
            if (x || effect()) {
                if (g)
                    return 5;
            } else {
                return 6;
            }

            x = 0;
            if (x || effect()) {
                if (g != 1)
                    return 7;
            } else {
                return 8;
            }

            return 0;
        }
    """

    unit = TranslationUnit("short_circuit_global.c", "short_circuit_global.c", source)
    result = run_owned_c_corpus(CEvaluator(),
        [unit],
        optimize=True,
        base_dir=".",
        jobs=1,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_ssa_branch_prune_preserves_short_circuit_value_side_effect_runtime():
    source = r"""
        int g;

        int effect(void) {
            g = 1;
            return 1;
        }

        int main(void) {
            int y = 0 && effect();
            if (g)
                return 1;
            if (y)
                return 2;

            y = 1 || effect();
            if (g)
                return 3;
            if (!y)
                return 4;

            return 0;
        }
    """

    unit = TranslationUnit("short_circuit_value.c", "short_circuit_value.c", source)
    result = run_owned_c_corpus(CEvaluator(),
        [unit],
        optimize=True,
        base_dir=".",
        jobs=1,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
