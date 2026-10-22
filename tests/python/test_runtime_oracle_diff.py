"""Runtime oracle corpus: compiled programs must behave exactly like CPython.

Each ``tests/runtime_oracle/*_basics.py`` program is compiled by pcc against
the production pcc-Python runtime archive (no libpython) and run with the same
arguments, working directory and environment as CPython running the source.
Return code, stdout and stderr must match byte for byte.

The corpus used to be diffed against the host-cc C runtime; that runtime is
retired, and CPython is the semantics the runtime implements.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).absolute().parents[2]
ORACLE_DIR = REPO_ROOT / "tests" / "runtime_oracle"
ARGS = ["oracle-arg-1", "oracle-arg-2"]
# Both runs see argv[0] with this basename (os_basics prints it).
PROGRAM_NAME = "ORACLE"

# Share the reduced self-GC heavy lane: every case is a full native compile.
pytestmark = pytest.mark.xdist_group(name="pcc_heavy_self")


def _corpus_programs() -> list[Path]:
    return sorted(ORACLE_DIR.glob("*_basics.py"))


def _pcc_binary() -> str:
    candidate = Path(sys.executable).parent / "pcc"
    if candidate.exists():
        return str(candidate)
    found = shutil.which("pcc")
    if found is None:
        pytest.fail("pcc CLI not on PATH")
    return found


def _run_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("LC_ALL", None)
    # path_basics echoes this variable.
    env["PCC_PATH_ORACLE_TEST"] = "oracle-value"
    return env


@pytest.mark.parametrize("program", _corpus_programs(), ids=lambda p: p.stem)
def test_corpus_program_matches_cpython(tmp_path, program, pcc_runtime_archive):
    cpython_dir = tmp_path / "cpython"
    native_dir = tmp_path / "native"
    cpython_dir.mkdir()
    native_dir.mkdir()
    script = cpython_dir / PROGRAM_NAME
    shutil.copyfile(program, script)
    executable = native_dir / PROGRAM_NAME

    compile_env = dict(os.environ)
    compile_env.pop("LC_ALL", None)
    compile_env["PCC_RUNTIME_ARCHIVE"] = str(pcc_runtime_archive)
    compiled = subprocess.run(
        [
            _pcc_binary(),
            "--python-libpython=off",
            str(program),
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(REPO_ROOT),
        env=compile_env,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr

    expected = subprocess.run(
        [sys.executable, str(script), *ARGS],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(tmp_path),
        env=_run_env(),
    )
    actual = subprocess.run(
        [str(executable), *ARGS],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(tmp_path),
        env=_run_env(),
    )
    assert (actual.returncode, actual.stdout, actual.stderr) == (
        expected.returncode,
        expected.stdout,
        expected.stderr,
    )


def test_corpus_is_not_empty():
    assert len(_corpus_programs()) >= 13
