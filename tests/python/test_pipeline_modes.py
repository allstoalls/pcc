"""Contracts for mode parsing extracted from the pipeline facade."""

from __future__ import annotations

import pytest

from pcc.py_frontend import pipeline
from pcc.py_frontend import pipeline_modes


def test_pipeline_facade_reexports_mode_contracts():
    assert pipeline.PyPipelineError is pipeline_modes.PyPipelineError
    assert (
        pipeline._resolve_native_backend
        is pipeline_modes.resolve_native_backend
    )
    assert (
        pipeline._resolve_libpython_mode
        is pipeline_modes.resolve_libpython_mode
    )
    assert (
        pipeline._resolve_ir_scaffold_mode
        is pipeline_modes.resolve_ir_scaffold_mode
    )
    assert (
        pipeline._resolve_gpu_backend_kind
        is pipeline_modes.resolve_gpu_backend_kind
    )
    assert (
        pipeline._self_backend_publish_sync_enabled
        is pipeline_modes.self_backend_publish_sync_enabled
    )


def test_backend_aliases_and_unsupported_capi_mode_are_stable():
    assert pipeline_modes.normalize_native_backend_name("llvmlite") == "llvm"
    assert pipeline_modes.normalize_native_backend_name("llvm-capi") == "llvm_capi"
    with pytest.raises(pipeline_modes.PyPipelineError, match="not supported"):
        pipeline_modes.resolve_native_backend("llvm-capi")


def test_default_api_backend_is_owned_self(monkeypatch):
    monkeypatch.delenv("PCC_BACKEND", raising=False)
    assert pipeline_modes.resolve_native_backend(None) == "self"
    assert pipeline_modes.resolve_native_backend("") == "self"


def test_mixed_extension_object_models_fail_closed():
    with pytest.raises(
        pipeline_modes.PyPipelineError,
        match="cannot be combined",
    ):
        pipeline_modes.reject_mixed_extension_object_models(
            needs_libpython=True,
            needs_native_extension_exports=True,
        )

    pipeline_modes.reject_mixed_extension_object_models(
        needs_libpython=False,
        needs_native_extension_exports=True,
    )
