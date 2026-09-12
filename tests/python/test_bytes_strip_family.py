"""`bytes`/`bytearray` no-arg strip, lstrip and rstrip.

Only `strip` was lowered; `lstrip`/`rstrip` fell through to the generic
attribute path and raised `'bytes' object has no attribute 'rstrip'`.  pcc1
hit that in its own archive reader -- `macho_archive` trims a 16-byte ar
member name with `header[0:16].rstrip()` -- so a self-hosted link could not
read the runtime archive at all.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

_PROGRAM = textwrap.dedent(
    '''\
    def take(data: bytes) -> int:
        print("chars-rstrip", data[0:16].rstrip(b"/"))
        print("chars-lstrip", b"   xy".lstrip(b" "))
        print("chars-strip", b"ab_xy_ba".strip(b"ab_"))
        print("chars-pad", data[16:24].rstrip(b"="))
        return 0


    def main() -> int:
        take(b"name/           pad=====")
        raw = b"  ar_name/      "
        print("strip", raw.strip())
        print("lstrip", raw.lstrip())
        print("rstrip", raw.rstrip())
        print("none", b"xy".lstrip(), b"xy".rstrip())
        ba = bytearray(b"\\t x \\n")
        print("ba", bytes(ba.lstrip()), bytes(ba.rstrip()))
        return 0


    main()
    '''
)


def _cpython_output(source: Path) -> str:
    run = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=120,
    )
    assert run.returncode == 0, run.stderr
    return run.stdout


def test_bytes_strip_family_matches_cpython(tmp_path):
    source = tmp_path / "strips.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    expected = _cpython_output(source)
    assert "ar_name/      " in expected, "CPython reference is not what we think"

    binary = tmp_path / "strips"
    build = subprocess.run(
        [
            sys.executable, "-m", "pcc",
            "--backend", "self", "--python-libpython", "off",
            str(source), "-o", str(binary),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert build.returncode == 0, build.stdout + build.stderr

    run = subprocess.run(
        [str(binary)], capture_output=True, text=True, timeout=120,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == expected
