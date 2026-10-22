"""Backend #4 exit-teardown UAF characterization.

Historical failure boundary:

* strict Python frontend mode: backend=self, python-libpython=off,
  ir-scaffold=on
* runtime mode: PCC_GC_BACKEND=4
* workload: longrun_churn.py completes and prints ``done,`` before the
  process dies with SIGSEGV during exit-time list deallocation

The broad longrun smoke matrix checks the measurement surface. This file keeps
the old backend4 exit-UAF boundary explicit and small.
"""
from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest

from tests.python.process_timeout import run_process_group_timeout


REPO_ROOT = Path(__file__).absolute().parents[2]
CHURN_SRC = REPO_ROOT / "benchmarks" / "python" / "longrun_churn.py"
CHURN_ROUNDS = "600"
ATTEMPTS = 3


@pytest.fixture(scope="module")
def backend4_churn_binary(tmp_path_factory):
    from pcc.frontends.python.pipeline import compile_python

    base = tmp_path_factory.mktemp("backend4_exit_list_item_uaf")
    exe = base / "longrun_churn_backend4_exit_uaf"
    compile_python(
        str(CHURN_SRC),
        str(exe),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    return exe


def _signal_name(returncode: int) -> str:
    if returncode >= 0:
        return f"exit {returncode}"
    try:
        return f"{returncode} ({signal.Signals(-returncode).name})"
    except ValueError:
        return str(returncode)


def _failure_message(
    *,
    attempt: int,
    returncode: int,
    stdout: str,
    stderr: str,
) -> str:
    completed = any(line.startswith("done,") for line in stdout.splitlines())
    boundary = (
        "backend4 exit-list-item UAF boundary: "
        "backend=self python-libpython=off ir-scaffold=on "
        f"PCC_GC_BACKEND=4 {CHURN_SRC.name} {CHURN_ROUNDS}"
    )
    if completed:
        phase = (
            "workload printed done before a non-zero process exit, matching "
            "the historical exit-time stale list-item/list-object UAF shape"
        )
    else:
        phase = "workload did not reach the historical post-done exit boundary"
    return (
        f"{boundary}\n"
        f"attempt {attempt}/{ATTEMPTS} failed with {_signal_name(returncode)}: "
        f"{phase}\n"
        f"stdout:\n{stdout}\n"
        f"stderr:\n{stderr}"
    )


def test_backend4_churn_exits_cleanly_after_done(backend4_churn_binary):
    env = os.environ.copy()
    env["PCC_GC_BACKEND"] = "4"
    for attempt in range(1, ATTEMPTS + 1):
        result = run_process_group_timeout(
            [str(backend4_churn_binary), CHURN_ROUNDS],
            timeout=60.0,
            env=env,
        )
        assert result.returncode == 0, _failure_message(
            attempt=attempt,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
        lines = [line for line in result.stdout.splitlines() if line]
        assert lines, result.stdout + result.stderr
        assert lines[-1].startswith("done,"), result.stdout + result.stderr
