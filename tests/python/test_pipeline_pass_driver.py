"""Focused facade contracts for isolated frontend IR-pass orchestration."""

from __future__ import annotations

from pcc.frontends.python import pipeline
from pcc.frontends.python import pipeline_pass_driver


def test_pipeline_pass_driver_facade_has_one_policy_owner():
    assert (
        pipeline._split_large_modules_for_python_ir_passes
        is pipeline_pass_driver.split_large_modules_for_passes
    )
    assert (
        pipeline._default_python_ir_pass_raw_for_backend
        is pipeline_pass_driver.default_raw_for_backend
    )
    assert (
        pipeline._default_python_ir_pass_raw_for_request
        is pipeline_pass_driver.default_raw_for_request
    )


def test_self_backend_default_selects_only_bounded_default_manifest():
    assert pipeline_pass_driver.default_raw_for_backend("self") == "default"
    assert pipeline_pass_driver.default_raw_for_backend("llvm") is None
    assert (
        pipeline_pass_driver.default_raw_for_request(
            None,
            emit_llvm_only=True,
            backend="self",
        )
        == "default"
    )
