from __future__ import annotations

from pathlib import Path


def test_c_runtime_log_symbols_are_built_and_called():
    root = Path(__file__).absolute().parents[2]
    makefile = (root / "pcc" / "runtime" / "Makefile").read_text(encoding="utf-8")
    internal = (root / "pcc" / "runtime" / "src" / "py_internal.h").read_text(encoding="utf-8")

    assert "pcc_diagnostics_runtime_log_event" in internal
