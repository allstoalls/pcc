from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from pcc1_gate import find_current_pcc1, skip_or_fail_no_current_pcc1


_UNSUPPORTED_SOURCE = """\
from pcc.ir.compat import ir


def f():
    x = ir.Argument(1, 2)
"""


def _repo_root() -> Path:
    return Path(__file__).absolute().parents[2]


def _find_pcc1() -> Path:
    pcc1 = find_current_pcc1(_repo_root())
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no pcc1 binary available for no-host self-backend fallback gate"
        )
    return pcc1


def test_self_backend_no_host_no_silent_fallback(tmp_path):
    pcc1 = _find_pcc1()
    source = tmp_path / "unsupported_trace.py"
    source.write_text(_UNSUPPORTED_SOURCE, encoding="utf-8")
    out = tmp_path / "unsupported_trace.out"
    env = os.environ.copy()
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_DEBUG_CODEGEN_PHASES"] = "1"

    proc = subprocess.run(
        [
            str(pcc1),
            str(source),
            "-o",
            str(out),
            "--backend=self",
            "--python-libpython=off",
            "--ir-scaffold=on",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=180.0,
    )

    assert proc.returncode != 0
    assert not out.exists()
    err = proc.stderr
    assert "PCC-PY-COMPILE-001" in err
    assert "ir.Argument has no scaffold lowering yet" in err
    assert "exception_type=" in err
    assert "clange" not in err
    assert "clang" not in err.lower()
