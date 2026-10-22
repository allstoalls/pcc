"""Shared fixtures for the low-level security test suite.

These tests are derived from the *Low-Level Software Security for Compiler
Developers* book (https://github.com/llsoftsec/llsoftsecbook). They probe the
security-relevant behavior of the code pcc actually emits — pcc lowers both C
and (no-libpython) Python down to machine instructions, so the same classes of
low-level hazard apply to both frontends.

The Python helpers compile a program through the strict no-libpython path
(``libpython_mode="off"``, ``ir_scaffold_mode="on"``) and run the produced
native binary, then compare against a CPython oracle. The ``backend`` argument
selects pcc's owned ``self`` backend, which is also the default.

Each pcc compile runs in a FRESH child process. ``compile_python`` keeps
process-level codegen state, so isolating every compile keeps the suite
deterministic regardless of fixture ordering.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

import pytest

_THIS_DIR = os.path.dirname(__file__)
_REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Child program: compile one .py to a native binary via pcc, no-libpython.
_COMPILE_CHILD = (
    "import sys\n"
    "from pcc.frontends.python.pipeline import compile_python\n"
    "compile_python(sys.argv[1], sys.argv[2], libpython_mode='off',\n"
    "               ir_scaffold_mode='on', backend=sys.argv[3])\n"
)


@pytest.fixture(scope="session")
def compile_and_run():
    """Compile a Python program through pcc (no-libpython) and run it.

    Returns the ``CompletedProcess`` of the *compiled binary*. Compilation
    happens in an isolated child interpreter; a compile failure raises with the
    child's stderr so it surfaces as a clear test error.
    """

    def _run(program: str, backend: str = "self", timeout: float = 240.0):
        d = tempfile.mkdtemp(prefix="pcc_sec_")
        src = os.path.join(d, "prog.py")
        with open(src, "w") as f:
            f.write(program)
        exe = os.path.join(d, "prog")
        # Each compile runs in a fresh child interpreter so codegen state
        # never leaks between programs in one session.
        comp = subprocess.run(
            [sys.executable, "-c", _COMPILE_CHILD, src, exe, backend],
            capture_output=True,
            text=True,
            cwd=_REPO_ROOT,
            timeout=timeout,
        )
        if comp.returncode != 0 or not os.path.exists(exe):
            raise RuntimeError(
                f"pcc compile (backend={backend}) failed:\n"
                + comp.stderr[-2000:]
            )
        return subprocess.run(
            [exe], capture_output=True, text=True, timeout=60.0
        )

    return _run


@pytest.fixture(scope="session")
def cpython_run():
    """Run a Python program under the host CPython as the semantic oracle."""

    def _run(program: str, timeout: float = 30.0):
        d = tempfile.mkdtemp(prefix="pcc_sec_oracle_")
        src = os.path.join(d, "prog.py")
        with open(src, "w") as f:
            f.write(program)
        return subprocess.run(
            [sys.executable, src], capture_output=True, text=True, timeout=timeout
        )

    return _run
