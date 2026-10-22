"""A function larger than a conditional branch's reach compiles and runs.

Inline error edges are ``cbnz`` (+/-1 MiB) to per-edge cold stubs that follow
the function's blocks.  In a function of more than 1 MiB of code the early
edges cannot reach them; Stage1 failed in
``self_backend_parse._parse_instruction`` with "branch to '...edgedot1' out
of range".  The AArch64 text finalizer relaxes such branches (see
``test_arm64_branch_relaxation.py``); this runs the original shape: one
function of about 1.4 MB whose calls raise through both edge kinds (cleanup
block and shared frame landing), checked against CPython.
"""

from __future__ import annotations

import os
import subprocess
import sys

_CALLS = 3000
_FAIL_CAUGHT = 700
_FAIL_UNCAUGHT = 2901


def _program() -> str:
    # Even lines release a temporary on error (edge into a cleanup block);
    # odd lines bind the result directly (edge into the shared frame landing
    # through a payload stub).
    calls = "".join(
        f"    total += check({i}, fail_at)\n" if i % 2 == 0
        else f"    last = check({i}, fail_at)\n"
        for i in range(_CALLS)
    )
    return (
        "def check(i, fail_at):\n"
        "    if i == fail_at:\n"
        "        raise ValueError('fail ' + str(i))\n"
        "    return i\n"
        "\n\n"
        "def big(fail_at):\n"
        "    total = 0\n"
        + calls
        + "    return total + last\n"
        "\n\n"
        "print(big(-1))\n"
        "try:\n"
        f"    big({_FAIL_CAUGHT})\n"
        "except ValueError as exc:\n"
        "    print('caught', exc)\n"
        f"big({_FAIL_UNCAUGHT})\n"
    )


def test_large_function_errors_match_cpython(tmp_path):
    source = tmp_path / "big.py"
    source.write_text(_program(), encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    exe = tmp_path / "big"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env.update({
        "PCC_DIRECT_INDEXED_KERNEL_CAPTURE": "1",
        "PCC_DIRECT_INDEXED_KERNEL_EMIT": "1",
        "PCC_DIRECT_INDEXED_KERNEL_FUSE_USES": "1",
        "PCC_DIRECT_INLINE_ERROR_EDGE_CAPTURE": "1",
        # With Python IR passes on, a direct worker reparses its own text and
        # the inline edges become an ordinary CFG; Stage1 and the gate run
        # with them off.
        "PCC_PYTHON_IR_PASSES": "off",
    })
    build = subprocess.run(
        ["uv", "run", "pcc", "--backend", "self", "--python-libpython=off",
         "--ir-scaffold=on", str(source), "-o", str(exe)],
        text=True, capture_output=True, timeout=900, env=env,
    )
    assert build.returncode == 0, build.stderr[-4000:]
    actual = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60, env=env)
    assert actual.returncode == expected.returncode == 1, actual.stderr
    assert actual.stdout == expected.stdout
    big_line = 9 + _FAIL_UNCAUGHT
    for fragment in (
        f"line {big_line}, in big",
        f"last = check({_FAIL_UNCAUGHT}, fail_at)",
        "line 3, in check",
        f"ValueError: fail {_FAIL_UNCAUGHT}",
    ):
        assert fragment in actual.stderr, (fragment, actual.stderr)
        assert fragment in expected.stderr, (fragment, expected.stderr)
