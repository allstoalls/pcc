"""The wheel job bundles only the Stage1 pcc1 its own passing receipt names."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import ci_stage1_compiler


ROOT = Path(__file__).resolve().parents[1]
SOURCE = {"commit": "a" * 40, "source_sha256": "b" * 64, "clean_commit": True}


def _receipt(tmp_path, **changes):
    compiler = tmp_path / "pcc1"
    compiler.write_bytes(b"tiny host-only compiler fixture; never executed\n")
    receipt = {
        "phase": "stage1", "status": "PASS", "source": dict(SOURCE),
        "compiler": {"path": str(compiler),
                     "sha256": hashlib.sha256(compiler.read_bytes()).hexdigest()},
    }
    receipt.update(changes)
    path = tmp_path / "stage1.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return path, compiler


def test_passing_receipt_names_its_compiler(tmp_path):
    path, compiler = _receipt(tmp_path)
    assert ci_stage1_compiler.stage1_compiler(path, SOURCE) == compiler


@pytest.mark.parametrize("changes", (
    {"status": "FAIL"},
    {"phase": "pcc1"},
    {"source": dict(SOURCE, commit="c" * 40)},
    {"source": dict(SOURCE, source_sha256="d" * 64)},
    {"source": dict(SOURCE, clean_commit=False)},
))
def test_receipt_for_other_source_or_result_is_refused(tmp_path, changes):
    path, _compiler = _receipt(tmp_path, **changes)
    with pytest.raises(RuntimeError):
        ci_stage1_compiler.stage1_compiler(path, SOURCE)


def test_compiler_changed_after_stage1_is_refused(tmp_path):
    path, compiler = _receipt(tmp_path)
    compiler.write_bytes(b"different bytes\n")
    with pytest.raises(RuntimeError, match="changed after Stage1"):
        ci_stage1_compiler.stage1_compiler(path, SOURCE)


def test_main_exports_the_compiler_to_github_env(tmp_path, monkeypatch, capsys):
    path, compiler = _receipt(tmp_path)
    github_env = tmp_path / "github_env"
    monkeypatch.setenv("GITHUB_ENV", str(github_env))
    monkeypatch.setattr(ci_stage1_compiler, "source_identity", lambda allow_dirty: SOURCE)
    assert ci_stage1_compiler.main(["--receipt", str(path)]) == 0
    assert github_env.read_text() == f"PCC_BUILD_PCC1={compiler}\n"
    assert capsys.readouterr().out == f"PCC_BUILD_PCC1={compiler}\n"


@pytest.mark.parametrize("workflow", ("workflow.yml", "pcc1-package-parity.yml"))
def test_every_wheel_build_step_follows_the_stage1_handoff(workflow):
    text = (ROOT / ".github/workflows" / workflow).read_text()
    builds = text.count("python -m build --wheel")
    assert builds >= 1
    assert text.count("python scripts/ci_stage1_compiler.py") == builds
