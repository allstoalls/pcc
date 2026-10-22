from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pcc.backend import BackendUnavailable, resolve_backend
from pcc.backend.self_backend_dispatch import emit_self_asm
from pcc.backend.self_backend_targets import (
    STATUS_SELF_TARGET_SUPPORTED,
    STATUS_SELF_TARGET_UNSUPPORTED,
    classify_self_backend_target_triple,
)


def test_self_backend_registry_is_supported_without_legacy_override():
    config = resolve_backend("self")
    assert config.kind == "self"
    assert config.supported is True
    assert "emit-object" in config.capabilities
    assert config.cache_signature().endswith(":support")


def test_supported_platform_verdict_is_capability_only():
    verdict = classify_self_backend_target_triple("arm64-apple-darwin23.6.0")
    assert verdict.to_dict() == {
        "triple": "arm64-apple-darwin23.6.0",
        "status": STATUS_SELF_TARGET_SUPPORTED,
        "target_identity": "self-aarch64-darwin-v0",
        "reason": "resolved to registered emitter 'self-aarch64-darwin-v0'",
        "backend_executed": False,
        "runtime_executed": False,
    }


def test_unsupported_platform_verdict_cannot_be_runtime_proof():
    verdict = classify_self_backend_target_triple("wasm32-unknown-unknown")
    assert verdict.status == STATUS_SELF_TARGET_UNSUPPORTED
    assert verdict.supported is False
    assert verdict.target_identity is None
    assert verdict.backend_executed is False
    assert verdict.runtime_executed is False
    assert verdict.skip_reason() == (
        "UNSUPPORTED[self-backend:wasm32-unknown-unknown]: no registered "
        "self-backend emitter matches 'wasm32-unknown-unknown'; "
        "backend_executed=false; runtime_executed=false"
    )


def test_vector_parity_family_uses_registry_platform_verdict_source_guard():
    path = Path(__file__).with_name("test_llvm_self_vector_parity.py")
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "classify_self_backend_target_triple"
    ]
    assert len(calls) == 1
    assert "is_supported_self_backend_target_triple" not in source
    assert "self backend target not supported" not in source


@pytest.mark.parametrize(
    "triple",
    [
        "aarch64-pc-windows-msvc",
        "x86_64-w64-mingw32",
        "x86_64-pc-freebsd-gnu",
        "x86_64-unknown-gnu",
        "x86_64-pc-notlinux-gnu",
        "x86_64-pc-windows-linux",
        "x86_64--linux-gnu",
        "x86_64-unknown-linux-gnu-",
        "x86_64-unknown-linux-gnu-extra",
        "x86_64-unknown-linux-gnu ",
        "arm64-notapple-darwin23.0",
        "arm64-apple-notdarwin",
        "arm64-apple-windows-darwin",
        "arm64-apple-darwinjunk",
        "arm64-apple-darwin23..0",
        "arm64-apple--darwin",
        "arm64-apple",
        "arm64-apple-ios17.0",
        "",
        "garbage",
    ],
)
def test_target_classifier_rejects_other_operating_systems_and_malformed_components(
    triple,
):
    verdict = classify_self_backend_target_triple(triple)
    assert verdict.status == STATUS_SELF_TARGET_UNSUPPORTED
    assert verdict.target_identity is None
    with pytest.raises(BackendUnavailable, match="no emitter for target triple"):
        emit_self_asm('target triple = "' + triple + '"\n', triple=triple)


@pytest.mark.parametrize(
    "triple, identity",
    [
        ("aarch64-unknown-linux-gnu", "self-aarch64-linux-v0"),
        ("x86_64-pc-windows-msvc", "self-x86_64-windows-v0"),
        ("x86_64-pc-windows-gnu", "self-x86_64-windows-v0"),
        ("amd64-pc-windows-gnu", "self-x86_64-windows-v0"),
        ("x86_64-linuxvendor-windows-gnu", "self-x86_64-windows-v0"),
        ("arm64-apple-darwin", "self-aarch64-darwin-v0"),
        ("aarch64-apple-darwin23.6.0", "self-aarch64-darwin-v0"),
        ("ARM64-APPLE-MACOSX14.0.0", "self-aarch64-darwin-v0"),
        ("arm64-apple-macosx12.0.0", "self-aarch64-darwin-v0"),
        ("x86_64-unknown-linux-gnu", "self-x86_64-linux-v0"),
        ("amd64-pc-linux-gnu", "self-x86_64-linux-v0"),
        ("X86_64-UNKNOWN-LINUX-MUSL", "self-x86_64-linux-v0"),
        ("x86_64-unknown-linux", "self-x86_64-linux-v0"),
        ("x86_64-linux-gnu", "self-x86_64-linux-v0"),
        ("amd64-linux-musl", "self-x86_64-linux-v0"),
        ("x86_64-linux", "self-x86_64-linux-v0"),
    ],
)
def test_explicit_target_components_preserve_linux_and_darwin_aliases(triple, identity):
    verdict = classify_self_backend_target_triple(triple)
    assert verdict.status == STATUS_SELF_TARGET_SUPPORTED
    assert verdict.target_identity == identity


def test_unsupported_windows_arch_is_rejected_before_object_emission_or_publication(
    tmp_path, monkeypatch
):
    from pcc.frontends.c.evaluator.c_evaluator import CEvaluator

    evaluator = object.__new__(CEvaluator)
    output = tmp_path / "retained.o"
    output.write_bytes(b"previous artifact")

    def unexpected_emitter(_units):
        pytest.fail("unsupported OS reached assembly emission")

    monkeypatch.setattr(evaluator, "_self_backend_asm_text", unexpected_emitter)
    units = [("windows", 'target triple = "aarch64-pc-windows-msvc"\n', None, ())]
    with pytest.raises(BackendUnavailable, match="no emitter for target triple"):
        evaluator._emit_compiled_units_self_backend(
            units, emit_obj=str(output), optimize=0
        )
    assert output.read_bytes() == b"previous artifact"
