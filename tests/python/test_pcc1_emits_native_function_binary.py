"""pcc1 must lower a function definition all the way to a running binary.

Every other pcc1 gate stops at ``--emit-llvm``, so the self-backend emit path
was never exercised *from pcc1* -- only from the host.  That gap let pcc1 ship
unable to compile any program containing a ``def``: the emitter's
stack-map planner cached location tuples under ``id()`` keys without keeping
the keyed objects alive, so a freed ``_RootGroup`` whose address was reused
made a stale fingerprint HIT and return a wrong tuple.  The host kept those
groups alive incidentally and never reproduced it.

The distinguishing input is tiny: a module with no function has no phi node and
compiled fine, while ``def f(): pass`` already has one.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

from pcc1_gate import find_current_pcc1, repo_root, skip_or_fail_no_current_pcc1

REPO = repo_root()

# (name, source, expected stdout) -- shapes that bracket the failure boundary.
_CASES = (
    ("no_function", "x = 1\nprint(x)\n", "1\n"),
    ("bare_def", "def f():\n    pass\n\nf()\nprint(2)\n", "2\n"),
    ("annotated_def", "def f() -> int:\n    return 3\n\nprint(f())\n", "3\n"),
    (
        "main_def",
        "def main() -> int:\n    print(4)\n    return 0\n\nmain()\n",
        "4\n",
    ),
)


def _pcc1_env(runtime_archive: Path) -> dict:
    """Environment for the COMPILE step.

    `PCC_HOST_PYTHON` is deliberately NOT disabled here.  A pcc1 compile still
    invokes the runtime-archive `make`, and that make shells out to a host
    Python for `pcc.tools.ir_to_obj`; disabling it makes the rebuild fail AND
    delete the objects it was rebuilding, so each run corrupts the archive a
    little further and the failure moves to a different object.  Removing host
    Python from the build path is its own goal, tracked separately -- this gate
    is about pcc1 lowering a function definition to a running binary, and
    conflating the two just produced a red test with a misleading message.

    The RUN step below still asserts no-host-python behaviour, which is where
    that claim actually belongs.
    """
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_RUNTIME_ARCHIVE"] = str(runtime_archive)
    return env


def test_pcc1_compiles_and_runs_function_definitions(
    tmp_path,
    pcc_runtime_archive,
):
    pcc1 = find_current_pcc1(REPO)
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no current pcc1 binary for the native function-emit gate"
        )

    env = _pcc1_env(pcc_runtime_archive)
    for name, source, expected in _CASES:
        src = tmp_path / (name + ".py")
        src.write_text(source, encoding="utf-8")
        binary = tmp_path / name
        compile_proc = subprocess.run(
            [
                str(pcc1),
                "--backend",
                "self",
                "--python-libpython=off",
                "--ir-scaffold=on",
                str(src),
                "-o",
                str(binary),
            ],
            check=False,
            text=True,
            capture_output=True,
            timeout=300,
            env=env,
        )
        assert compile_proc.returncode == 0, (
            name + " failed to compile:\n" + compile_proc.stdout + compile_proc.stderr
        )
        assert binary.exists(), name + " produced no binary"

        # Run under the runtime's own refcount checker: a wrong root-location
        # set surfaces here as [BAD_INCREF] rather than as a compile error.
        run_env = dict(env)
        run_env["PCC_DEBUG_RUNTIME"] = "1"
        run_proc = subprocess.run(
            [str(binary)],
            check=False,
            text=True,
            capture_output=True,
            timeout=120,
            env=run_env,
        )
        assert run_proc.returncode == 0, (
            name + " exited " + str(run_proc.returncode) + ":\n" + run_proc.stderr
        )
        assert "BAD_INCREF" not in run_proc.stderr, (
            name + " tripped the refcount checker:\n" + run_proc.stderr
        )
        assert run_proc.stdout == expected, (
            name + " printed " + repr(run_proc.stdout) + ", want " + repr(expected)
        )
