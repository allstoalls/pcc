"""CI runs the one interpreter .python-version names, on every platform.

Workflows used to pick interpreters per job (``--python 3.13``,
``UV_PYTHON``, ``uv_python`` matrix entries, ``python-version: "3.13"``),
so moving the host meant editing a dozen places and Linux/Windows stayed on
3.13 while macOS ran 3.15. uv reads ``.python-version`` itself and
setup-python reads it through ``python-version-file``. The Docker wheel
checks are the one deliberate exception: they install on the package's
requires-python floor.
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github/workflows").glob("*.yml"))


def _floor() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    match = re.fullmatch(r">=\s*(\d+\.\d+)", project["requires-python"])
    assert match, project["requires-python"]
    return match.group(1)


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda path: path.name)
def test_workflow_takes_its_interpreter_from_python_version(workflow):
    text = workflow.read_text(encoding="utf-8")
    for override in ("--python ", "UV_PYTHON:", "uv_python", "python-version:"):
        assert override not in text, (workflow.name, override)
    for step in text.split("uses: actions/setup-python@")[1:]:
        assert "python-version-file: .python-version" in step.split("\n      - ", 1)[0]


def test_only_the_docker_wheel_checks_name_a_python_and_it_is_the_floor():
    floor = _floor()
    for workflow in WORKFLOWS:
        for image in re.findall(r"\bpython:(\d+\.\d+)-", workflow.read_text(encoding="utf-8")):
            assert image == floor, (workflow.name, image)


def test_gate_reads_the_exact_release_from_python_version():
    sys.path.insert(0, str(ROOT))
    try:
        from scripts.ci_worker_size_prior_gate import python_version_pin
    finally:
        sys.path.remove(str(ROOT))
    assert python_version_pin("3.15.0\n") == (3, 15, 0, "final", 0)
    assert python_version_pin("3.15.0rc1") == (3, 15, 0, "candidate", 1)
    assert python_version_pin("3.14.0b2") == (3, 14, 0, "beta", 2)
    with pytest.raises(ValueError):
        python_version_pin("3.15")
    pinned = python_version_pin((ROOT / ".python-version").read_text(encoding="utf-8"))
    assert pinned[:2] >= tuple(int(part) for part in _floor().split("."))
