"""Focused facade contracts for native link policy extraction."""

from __future__ import annotations

from pcc.frontends.python import pipeline
from pcc.frontends.python import pipeline_native_link


def test_native_link_export_policy_has_one_owner():
    assert (
        pipeline._native_extension_export_link_flags
        is pipeline_native_link.native_extension_export_link_flags
    )
    assert pipeline_native_link.native_extension_export_link_flags(
        True,
        platform="darwin",
    ) == ["-Wl,-export_dynamic"]
    assert pipeline_native_link.native_extension_export_link_flags(
        True,
        platform="linux",
    ) == ["-rdynamic"]


def test_runtime_archive_anchor_fallback_remains_platform_labeled():
    no_anchors = lambda _archive: []
    assert pipeline_native_link.runtime_archive_link_args_for_native_extensions(
        "/tmp/runtime.a",
        True,
        anchor_symbols=no_anchors,
        platform="darwin",
    ) == ["-Wl,-u,_PyArg_ParseTuple", "/tmp/runtime.a"]
    assert pipeline_native_link.runtime_archive_link_args_for_native_extensions(
        "/tmp/runtime.a",
        True,
        anchor_symbols=no_anchors,
        platform="linux",
    ) == ["-Wl,-u,PyArg_ParseTuple", "/tmp/runtime.a"]
