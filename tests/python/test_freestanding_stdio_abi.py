from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_stdio_abi_outputs_are_generated_from_one_spec():
    check = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "gen_freestanding_stdio_abi.py"),
            "--check",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert check.returncode == 0, check.stdout + check.stderr

    from pcc.frontends.python.codegen.freestanding_abi_constants import ABI_CONSTANTS
    from pcc.runtime.freestanding_abi_spec import ABI_SPEC

    assert ABI_CONSTANTS == ABI_SPEC
    assert ABI_CONSTANTS["stdio.file.size"] == 64
    assert ABI_CONSTANTS["stdio.file.fd_offset"] == 8
    assert ABI_CONSTANTS["stdio.file.flags_offset"] == 16
    assert ABI_CONSTANTS["stdio.flag.append"] == 32
    header = (
        REPO_ROOT / "pcc" / "runtime" / "include" / "pcc_stdio_abi.h"
    ).read_text(encoding="utf-8")
    assert "typedef struct PccOwnedFile" in header
    assert "PCC_STDIO_FILE_SIZE 64" in header
    assert "offsetof(PccOwnedFile, fd) == 8" in header
    assert "offsetof(PccOwnedFile, flags) == 16" in header
    assert "offsetof(PccOwnedFile, buffer) == 32" in header
    assert "offsetof(PccOwnedFile, buffer_length) == 48" in header
    assert "int64_t buffer_position;" in header
    assert "PCC_STDIO_FLAG_APPEND 32" in header


def test_freestanding_stdio_consumes_generated_abi_constants():
    source = (
        REPO_ROOT / "pcc" / "runtime" / "py" / "freestanding_stdio.py"
    ).read_text(encoding="utf-8")
    assert "5783538579059651889" not in source
    for name in (
        "stdio.file.magic",
        "stdio.file.size",
        "stdio.file.magic_offset",
        "stdio.file.fd_offset",
        "stdio.file.flags_offset",
        "stdio.file.aux_offset",
        "stdio.file.buffer_offset",
        "stdio.file.buffer_capacity_offset",
        "stdio.file.buffer_length_offset",
        "stdio.file.buffer_position_offset",
        "stdio.flag.readable",
        "stdio.flag.writable",
        "stdio.flag.error",
        "stdio.flag.eof",
        "stdio.flag.standard",
        "stdio.flag.append",
    ):
        assert f'abi_constant("{name}")' in source
