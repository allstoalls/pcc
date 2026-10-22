"""G-P0-LONGRUN regression: backend #4 exit-time stale slot-value UAF.

This is the closure regression for
``docs/investigations/gc-backend4-churn-exit-list-item-uaf.md``. The
historical failure boundary is intentionally mode-labeled:

* strict Python frontend mode: ``backend=self``, ``python-libpython=off``,
  ``ir-scaffold=on``
* runtime mode: ``PCC_GC_BACKEND=4``
* workload: ``longrun_churn.py`` completes ALL work (prints ``done,<ops>``)
  and then dies with SIGSEGV during exit-time list deallocation, at
  ~2/3 of runs, on backend #4 only (backends 0-3 are clean).

Root cause (No.6/No.9 in the investigation): the backend-4 read barrier
decided "does this slot value need relocation resolution?" by loading the
value's header flags directly. Under churn a slot can hold a STALE reference
(a freed malloc'd child, or an old copy whose address the object index /
forwarding table never mapped). The address heuristic cannot tell a
plausible-but-unmapped address apart from a live one, so the header load
faulted at exit-time list dealloc.

The fix routes the backend-4 decision through
``pcc_gc_backend4_slot_needs_resolve`` (py_gc_backend.c + pcc-Python mirror),
which consults the forwarding table and object index (pointer-VALUE hash
lookups, no deref) FIRST and only reads the header of a proven-mapped
(known-live) object.

CPython reference (``python3 longrun_churn.py 600`` semantics): prints
``done,38400`` (600 rounds x 64 ops) and exits 0, no ``corrupt`` line.

Because the historical crash was intermittent (~2/3), a single lucky pass
cannot prove the fix. This runs the compiled binary REPEATEDLY under
``PCC_GC_BACKEND=4`` and requires every attempt to reach ``done,`` and exit
cleanly. It does NOT weaken the gate to green it (AGENTS.md / investigation
Notes): a stale-slot-value read must resolve or pass through without a fault,
never by disabling relocation, dropping GC tracking, or short-circuiting the
dealloc walk.
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
EXPECTED_DONE = "done,38400"  # 600 rounds * 64 ops per round
# Historical crash rate was ~2/3; enough attempts that a single lucky exit
# cannot mask an unfixed intermittent UAF.
ATTEMPTS = 8


@pytest.fixture(scope="module")
def backend4_churn_binary(tmp_path_factory):
    from pcc.frontends.python.pipeline import compile_python

    base = tmp_path_factory.mktemp("backend4_longrun_exit_uaf")
    exe = base / "longrun_churn_backend4_longrun_uaf"
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


def _failure_message(*, attempt: int, returncode: int, stdout: str, stderr: str) -> str:
    completed = any(line.startswith("done,") for line in stdout.splitlines())
    boundary = (
        "backend4 exit-slot-value UAF boundary (G-P0-LONGRUN): "
        "backend=self python-libpython=off ir-scaffold=on "
        f"PCC_GC_BACKEND=4 {CHURN_SRC.name} {CHURN_ROUNDS}"
    )
    if completed:
        phase = (
            "workload printed done before a non-zero process exit, matching "
            "the historical exit-time stale slot-value/list-item UAF shape"
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


def test_backend4_churn_exits_cleanly_every_attempt(backend4_churn_binary):
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
        # No corruption sentinel and all work completed (matches CPython
        # `done,38400`), not just a bare early clean exit.
        assert "corrupt" not in result.stdout, result.stdout + result.stderr
        assert lines[-1] == EXPECTED_DONE, result.stdout + result.stderr
