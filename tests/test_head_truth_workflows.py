from __future__ import annotations

import re
import subprocess
from pathlib import Path

from scripts.head_truth_manifest import REQUIRED_GATE_IDS, gate_specs

ROOT = Path(__file__).resolve().parents[1]


def _workflow(name: str) -> str:
    return (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")


def _step(workflow: str, step_name: str) -> str:
    marker = f"      - name: {step_name}\n"
    assert marker in workflow
    return workflow.split(marker, 1)[1].split("\n      - ", 1)[0]


def test_light_and_heavy_commands_are_owned_by_manifest_registry() -> None:
    specs = gate_specs(ROOT)
    suites = {spec.suite for spec in specs}
    gate_ids = {spec.gate_id for spec in specs}

    assert suites == {"light", "heavy"}
    assert "runtime-archive-preflight" in gate_ids
    assert "fallback-ratchet" in gate_ids
    assert "control-plane-ratchets" in gate_ids
    assert "gc-production-contract" in gate_ids
    assert "self-five-gc-bootstrap" in gate_ids
    assert "numpy-core-head" in gate_ids
    assert "numpy-core-head" in REQUIRED_GATE_IDS

    control = next(spec for spec in specs if spec.gate_id == "control-plane-ratchets")
    assert "tests/python/test_bootstrap_gate_baseline.py" not in control.command
    assert any("TestObligation1ModeLabeling" in arg for arg in control.command)
    assert any("TestObligation5FixedPointContract" in arg for arg in control.command)


def test_heavy_registry_prebuilds_runtime_archive_before_consumers() -> None:
    specs = gate_specs(ROOT)
    gate_ids = [spec.gate_id for spec in specs]

    assert "runtime-archive-preflight" in gate_ids
    assert "runtime-archive-preflight" in REQUIRED_GATE_IDS
    assert gate_ids.index("runtime-archive-preflight") < gate_ids.index(
        "fallback-ratchet"
    )

    preflight = next(
        spec for spec in specs if spec.gate_id == "runtime-archive-preflight"
    )
    assert preflight.suite == "heavy"
    assert preflight.kind == "command"
    assert preflight.command[:4] == ("make", "-B", "-C", "pcc/runtime")
    assert "libpy_runtime_pcc_py.a" in preflight.command
    assert "PCC=../../.venv/bin/pcc" in preflight.command
    assert "PYTHON=../../.venv/bin/python3" in preflight.command
    assert str(ROOT) not in " ".join(preflight.command)

    self_five_gc = next(
        spec for spec in specs if spec.gate_id == "self-five-gc-bootstrap"
    )
    assert self_five_gc.timeout_seconds == 1800
    assert self_five_gc.command[:6] == (
        "uv",
        "run",
        "pytest",
        "-q",
        "-m",
        "integration",
    )
    assert "-rA" in self_five_gc.command
    assert "-n0" not in self_five_gc.command

    numpy_core = next(spec for spec in specs if spec.gate_id == "numpy-core-head")
    assert numpy_core.suite == "heavy"
    assert numpy_core.timeout_seconds == 1200
    assert "scripts/numpy_head_gate.py" in numpy_core.command


def test_fallback_timeout_covers_observed_hosted_boundary() -> None:
    fallback = next(
        spec for spec in gate_specs(ROOT) if spec.gate_id == "fallback-ratchet"
    )

    assert fallback.timeout_seconds == 420
