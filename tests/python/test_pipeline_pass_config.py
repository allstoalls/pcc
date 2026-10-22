"""IR-pass policy contracts extracted from the pipeline facade."""

from __future__ import annotations

from pcc.frontends.python import pipeline
from pcc.frontends.python import pipeline_pass_config


def test_pipeline_facade_reexports_pass_policy_helpers():
    assert (
        pipeline._resolve_python_ir_pass_names
        is pipeline_pass_config.resolve_python_ir_pass_names
    )
    assert (
        pipeline._python_ir_pass_timeout_seconds
        is pipeline_pass_config.python_ir_pass_timeout_seconds
    )
    assert pipeline._small_int_decimal is pipeline_pass_config.small_int_decimal
    assert (
        pipeline._python_ir_pass_should_skip_module
        is pipeline_pass_config.python_ir_pass_should_skip_module
    )


def test_pass_presets_are_deduplicated_and_off_is_empty():
    assert pipeline_pass_config.resolve_python_ir_pass_names("off") == []
    assert pipeline_pass_config.resolve_python_ir_pass_names(
        "fast,mem2reg,dce,fast"
    ) == ["mem2reg", "sroa", "dce"]


def test_pass_timeout_and_positive_integer_parsing_fail_closed(monkeypatch):
    monkeypatch.setenv(pipeline_pass_config.PYTHON_IR_PASS_TIMEOUT_ENV, "invalid")
    monkeypatch.setenv("PCC_TEST_POSITIVE_INT", "-7")

    assert pipeline_pass_config.python_ir_pass_timeout_seconds() == 120.0
    assert pipeline_pass_config.positive_int_env("PCC_TEST_POSITIVE_INT", 9) == 1


def test_bootstrap_integer_text_uses_canonical_python_semantics():
    cases = (
        (0, "0"),
        (7, "7"),
        (20, "20"),
        (21, "21"),
        (987654321, "987654321"),
        (-1, "-1"),
        (-987654321, "-987654321"),
    )
    for value, expected in cases:
        assert pipeline_pass_config.small_int_decimal(value) == expected

    assert pipeline_pass_config.seconds_debug_text(0.007) == "0.007s"
    assert pipeline_pass_config.seconds_debug_text(21.125) == "21.125s"
    assert pipeline_pass_config.seconds_debug_text(-21.125) == "-21.125s"


def test_unsafe_codegen_modules_remain_skipped():
    assert pipeline_pass_config.python_ir_pass_should_skip_module(
        "pcc.frontends.python.codegen.literal_lowering"
    )
    assert pipeline_pass_config.python_ir_pass_should_skip_module(
        "pcc.ir.ir"
    )
    assert not pipeline_pass_config.python_ir_pass_should_skip_module(
        "application.main"
    )
