"""sys.stdin.readline / sys.stdout.write keep CPython's TextIOWrapper contract natively."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.python.pcc1_gate import repo_root

PROGRAM = '''import sys


def main():
    print(sys.stdin.readline().strip())
    long_line = sys.stdin.readline()
    print(len(long_line), long_line.endswith("\\n"), long_line[:3])
    written = sys.stdout.write("h\\u00e9llo\\n")
    print(written)
    try:
        sys.stdout.write(5)
    except TypeError as error:
        print("TypeError", error)
    print(repr(sys.stdin.readline()))


main()
'''

INPUT = "typed\n" + "x" * 10000 + "\n"


def _expected() -> str:
    run = subprocess.run(
        [sys.executable, "-c", PROGRAM], input=INPUT, text=True, capture_output=True, timeout=60,
    )
    assert run.returncode == 0, run.stderr
    return run.stdout


@pytest.mark.integration
@pytest.mark.parametrize("backend", ["0", "4"])
def test_sys_stream_methods_match_cpython(tmp_path: Path, backend: str) -> None:
    src = tmp_path / "streams.py"
    src.write_text(PROGRAM, encoding="utf-8")
    exe = tmp_path / "streams"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_GC_BACKEND"] = backend
    build = subprocess.run(
        ["uv", "run", "pcc", "--backend", "self", "--python-libpython=off", str(src), "-o", str(exe)],
        cwd=repo_root(), text=True, capture_output=True, timeout=600, env=env,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run([str(exe)], input=INPUT, text=True, capture_output=True, timeout=60, env=env)
    assert run.returncode == 0, run.stderr
    assert run.stdout == _expected()
