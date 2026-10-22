from __future__ import annotations

from pathlib import Path

from pcc1_gate import repo_root

import pytest

from pcc1_gate import find_current_pcc1, require_current_pcc1_enabled


REPO = repo_root()


@pytest.mark.pcc_gate(env="PCC_REQUIRE_CURRENT_PCC1")
def test_current_pcc1_exists_when_package_parity_is_required():
    if not require_current_pcc1_enabled():
        pytest.fail("PCC_REQUIRE_CURRENT_PCC1 must be 1 when this gate is selected")
    pcc1 = find_current_pcc1(REPO)
    assert pcc1 is not None, (
        "pcc1 package parity was required, but no pcc1 binary newer than "
        "pcc/driver/cli_bootstrap.py was found"
    )
