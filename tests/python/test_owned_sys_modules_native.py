"""Matched-runtime native qualification for the actual live module cache."""
from pathlib import Path

import pytest

from tests.python.owned_regression_support import (
    assert_owned_program, explicit_owned_runtime,
)


CASES = Path(__file__).resolve().parents[1] / "fixtures/owned_sys_modules"


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_live_module_cache_native_all_collectors(
    python_program_compiler, request, explicit_owned_runtime, tmp_path, capfd,
):
    for path in CASES.glob("*.py"):
        if path.name != "cache_entry.py":
            (tmp_path/path.name).write_bytes(path.read_bytes())
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(
        (CASES/"cache_entry.py").read_text(), "LIVE_MODULE_CACHE_OK\n", tmp_path,
        python_program_compiler, mode, explicit_owned_runtime, capfd,
        provenance_probe="2",
    )
