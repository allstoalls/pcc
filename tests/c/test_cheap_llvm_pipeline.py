import pytest

import pcc.evaluater.c_evaluator as c_evaluator

from pcc.evaluater.c_evaluator import CEvaluator
from pcc.passes import PassContext
from pcc.project import TranslationUnit


_CHEAP_PIPELINE_SMOKE_SOURCE = r"""
    int helper(int limit) {
        int i;
        int acc;

        acc = 0;
        for (i = 0; i < limit; ++i) {
            int pair[2];
            pair[0] = i;
            pair[1] = i * i;
            if ((pair[0] & 1) == 0) {
                acc += pair[0] + pair[1];
            } else {
                acc += pair[1] - pair[0];
            }
        }
        return acc;
    }

    int main(void) {
        return helper(8) == 136 ? 0 : 1;
    }
"""


def test_cheap_llvm_pipeline_disabled_by_default(monkeypatch):
    monkeypatch.delenv("PCC_CHEAP_LLVM_PIPELINE", raising=False)

    assert c_evaluator._resolve_cheap_llvm_pipeline_passes() == ()


def test_cheap_llvm_pipeline_boolean_enable_uses_default_bundle(monkeypatch):
    monkeypatch.setenv("PCC_CHEAP_LLVM_PIPELINE", "1")

    assert (
        c_evaluator._resolve_cheap_llvm_pipeline_passes()
        == c_evaluator._DEFAULT_CHEAP_LLVM_PASSES
    )


def test_cheap_llvm_pipeline_alias_list_resolves_to_llvmlite_pass_names(
    monkeypatch,
):
    monkeypatch.setenv(
        "PCC_CHEAP_LLVM_PIPELINE",
        "sroa,instcombine,newgvn,simplifycfg,adce",
    )

    assert (
        c_evaluator._resolve_cheap_llvm_pipeline_passes()
        == c_evaluator._DEFAULT_CHEAP_LLVM_PASSES
    )


def test_cheap_llvm_pipeline_rejects_unknown_pass_name(monkeypatch):
    monkeypatch.setenv("PCC_CHEAP_LLVM_PIPELINE", "bogus-pass")

    with pytest.raises(ValueError, match="unsupported cheap LLVM pass"):
        llvmmod = c_evaluator._llvm().parse_assembly("define i32 @main() { ret i32 0 }")
        target_machine = CEvaluator().target.create_target_machine()
        c_evaluator._apply_llvm_optimizations(llvmmod, target_machine, 0)


def test_cheap_llvm_pipeline_records_backend_pass_metric(monkeypatch):
    monkeypatch.setenv("PCC_CHEAP_LLVM_PIPELINE", "1")

    llvmmod = c_evaluator._llvm().parse_assembly("define i32 @main() { ret i32 0 }")
    target_machine = CEvaluator().target.create_target_machine()
    ctx = PassContext()

    c_evaluator._apply_llvm_optimizations(
        llvmmod,
        target_machine,
        0,
        pass_ctx=ctx,
    )

    metric = ctx.pass_metrics["llvm-cheap-pipeline"]
    assert metric.tier == "backend"
    assert metric.runs == 1


def test_evaluate_runs_with_cheap_llvm_pipeline_enabled(monkeypatch):
    monkeypatch.setenv("PCC_CHEAP_LLVM_PIPELINE", "1")

    assert (
        CEvaluator().evaluate(
            _CHEAP_PIPELINE_SMOKE_SOURCE,
            optimize=False,
            use_system_cpp=False,
        )
        == 0
    )


def test_system_link_runs_with_cheap_llvm_pipeline_enabled(monkeypatch):
    monkeypatch.setenv("PCC_CHEAP_LLVM_PIPELINE", "1")

    unit = TranslationUnit(
        name="cheap_pipeline_smoke.c",
        path="cheap_pipeline_smoke.c",
        source=_CHEAP_PIPELINE_SMOKE_SOURCE,
    )

    result = CEvaluator().run_translation_units_with_system_cc(
        [unit],
        optimize=False,
        base_dir=".",
        jobs=1,
    )

    assert result.returncode == 0, result.stderr
