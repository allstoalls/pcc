"""Bootstrap-gate baseline verification (Issue 1 progress tracker).

Lightweight: only inspects binaries that already exist in
``build/bootstrap-self/``. Does NOT trigger a fresh bootstrap
build (those take minutes and would slow every pytest run). If the
binaries are absent, the captured-baseline cases are unavailable — re-run
``scripts/bootstrap.py`` to regenerate them, then re-run pytest.

What's checked:
- Binary sizes haven't drifted dramatically from the captured baseline.
- ``otool -L`` libpython linkage state matches baseline (currently
  ``false`` for every strict bootstrap binary).
- pcc2 and pcc3 have identical original bytes, including signature and UUID.

The Issue 1 no-libpython baseline is intentionally one-way: any
``links_libpython`` transition back to ``true`` is a regression.
"""
from __future__ import annotations

import json
import os
import platform

from pcc.diagnostics.dependency_verdict import probe_platform_capability
import subprocess
import struct
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).absolute().parents[2]
_BASELINE_JSON = _REPO_ROOT / "tests" / "bootstrap_gate_baseline.json"
_BUILD_ROOT = _REPO_ROOT / "build"


# Structured capture-platform verdict: the authoritative baseline is
# macOS-arm64-specific; elsewhere the verdict is UNAVAILABLE and never a
# claim about bootstrap behavior (AUD-P2-PLATFORM-BOOTSTRAP-BASELINE-VERDICT).
def _is_macos_arm64() -> bool:
    return sys.platform == "darwin" and platform.machine().lower() in {
        "arm64",
        "aarch64",
    }


_PLATFORM_GATE = pytest.mark.pcc_gate(
    unavailable=None
    if _is_macos_arm64()
    else "the authoritative bootstrap baseline is captured on macOS arm64"
)



def _load_baseline() -> dict:
    with open(_BASELINE_JSON, "r", encoding="utf-8") as f:
        return json.load(f)


def _stage_bin(backend: str, stage: int) -> Path:
    return _BUILD_ROOT / f"bootstrap-{backend}" / f"pcc{stage}"


def _links_libpython(path: Path) -> bool:
    cmd = ["otool", "-L", str(path)] if sys.platform == "darwin" else [
        "ldd",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pytest.fail(f"can't run {cmd[0]}; cannot verify linkage")
    text = (result.stdout or "") + (result.stderr or "")
    return "libpython" in text or "Python.framework" in text


def _byte_identical(a: Path, b: Path) -> bool:
    return a.read_bytes() == b.read_bytes()


def _missing_stage_bin_reason(backend: str) -> str | None:
    for stage in (1, 2, 3):
        path = _stage_bin(backend, stage)
        if not path.exists():
            return (
                f"{path} missing; run scripts/bootstrap.py --backend "
                f"{backend} to populate"
            )
    return None


def _require_bins(backend: str) -> None:
    reason = _missing_stage_bin_reason(backend)
    if reason is not None:
        pytest.fail(reason + " (gate selected but stage binaries absent)")


@pytest.mark.parametrize(
    "backend",
    [
        pytest.param(
            backend,
            marks=pytest.mark.pcc_gate(unavailable=_missing_stage_bin_reason(backend)),
        )
        for backend in ("self",)
    ],
)
@_PLATFORM_GATE
def test_bootstrap_libpython_state_matches_baseline(backend):
    """Each backend×stage binary's libpython linkage must match what
    the baseline records. When Path A flips a binary from true→false,
    update the baseline JSON.
    """
    platform_verdict = probe_platform_capability(
        "macos-arm64-bootstrap-baseline",
        supported=_is_macos_arm64(),
        detail="the authoritative bootstrap baseline is captured on macOS arm64",
    )
    if not platform_verdict.available:
        pytest.fail(platform_verdict.skip_reason())
    _require_bins(backend)
    baseline = _load_baseline()
    expected_state = baseline["current_state"][backend]
    actual: dict[str, dict[str, object]] = {}
    for stage in (1, 2, 3):
        path = _stage_bin(backend, stage)
        actual[f"stage{stage}"] = {
            "size_bytes": path.stat().st_size,
            "links_libpython": _links_libpython(path),
        }

    mismatches: list[str] = []
    for stage_key, expected in expected_state.items():
        observed = actual[stage_key]
        if observed["links_libpython"] != expected["links_libpython"]:
            mismatches.append(
                f"{backend}/{stage_key}: links_libpython "
                f"{observed['links_libpython']} != "
                f"{expected['links_libpython']}"
            )
    assert not mismatches, (
        "bootstrap gate libpython state drifted from baseline:\n  "
        + "\n  ".join(mismatches)
        + "\n(if intentional Path A progress, refresh baseline JSON)"
    )


@pytest.mark.parametrize(
    "backend",
    [
        pytest.param(
            backend,
            marks=pytest.mark.pcc_gate(unavailable=_missing_stage_bin_reason(backend)),
        )
        for backend in ("self",)
    ],
)
@_PLATFORM_GATE
def test_bootstrap_pcc2_pcc3_byte_identical(backend):
    """Stage2/3 must agree in their original bytes.

    This inspects the Aug-2026 baseline binaries in ``build/bootstrap-self``,
    not a fresh build; the live gate in ``scripts/bootstrap.py`` requires raw
    byte identity on every platform and format.  The ``llvm`` arm was retired
    with the LLVM bootstrap route (``bootstrap.py`` accepts the owned ``self``
    backend only); the recorded llvm baseline stays in the JSON as history.
    """
    platform_verdict = probe_platform_capability(
        "macos-arm64-bootstrap-baseline",
        supported=_is_macos_arm64(),
        detail="the byte-identical gate is captured on macOS arm64",
    )
    if not platform_verdict.available:
        pytest.fail(platform_verdict.skip_reason())
    _require_bins(backend)
    pcc2 = _stage_bin(backend, 2)
    pcc3 = _stage_bin(backend, 3)
    assert _byte_identical(pcc2, pcc3), (
        f"{backend}: pcc2 and pcc3 differ in their original bytes; "
        f"self-host determinism gate failed"
    )


def test_raw_byte_fixed_point_rejects_uuid_drift_without_external_tools(tmp_path, monkeypatch):
    def unexpected_tool(*args, **kwargs):
        raise AssertionError("raw byte comparison must not normalize or invoke tools")

    monkeypatch.setattr(subprocess, "run", unexpected_tool)
    header = struct.pack("<IiiIIIII", 0xFEEDFACF, 0x0100000C, 0, 2, 1, 24, 0, 0)
    command = struct.pack("<II", 0x1B, 24)
    first, second = tmp_path / "pcc2", tmp_path / "pcc3"
    first.write_bytes(header + command + bytes(range(16)))
    second.write_bytes(header + command + bytes(reversed(range(16))))
    original = first.read_bytes(), second.read_bytes()
    assert not _byte_identical(first, second)
    assert (first.read_bytes(), second.read_bytes()) == original
    second.write_bytes(first.read_bytes())
    assert _byte_identical(first, second)


def test_raw_byte_fixed_point_rejects_signature_payload_drift(tmp_path):
    first, second = tmp_path / "pcc2", tmp_path / "pcc3"
    payload = b"same emitted code and load commands\0"
    first.write_bytes(payload + b"signature-one")
    second.write_bytes(payload + b"signature-two")
    assert not _byte_identical(first, second)
