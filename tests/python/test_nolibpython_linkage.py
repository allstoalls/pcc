from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).absolute().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "verify_nolibpython.py"


def test_verify_nolibpython_uses_the_system_cc_without_clang_flags():
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'os.environ.get("CC")' in text
    assert '"clang"' not in text


def test_verify_nolibpython_script_is_importable():
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_verify_nolibpython_reports_libraries_without_host_tools():
    """The linkage inspection parses the artifact, not ldd/readelf/nm."""

    text = SCRIPT.read_text(encoding="utf-8")
    assert "_elf_needed_and_undefined" in text
    assert "_macho_dylibs_and_undefined" in text
    assert "_pe_imports" in text
    for host_tool in ("ldd", "readelf", "otool", "objdump"):
        assert f'"{host_tool}"' not in text
