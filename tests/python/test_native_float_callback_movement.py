"""Require actual GC4 movement during canonical float callback/reentry paths."""
from __future__ import annotations

import hashlib
import json
import os

import pytest

from tests.python.owned_regression_support import explicit_owned_runtime
from tests.python.process_timeout import run_process_group_timeout


PROGRAM = '''import gc
from pcc.extern import extern, c_int64
backend = extern("pcc_gc_backend", (), c_int64)
metric = extern("pcc_gc_telemetry", (c_int64,), c_int64)
select_moving = extern("pcc_gc_select_relocation_set", (c_int64,), c_int64)
step = extern("pcc_gc_step", (c_int64,), c_int64)
evidence = []
events = []
converter = float

def move_live_objects(phase):
    # Keep this phase's small, initialized lists alive without an address
    # lease. Reusing an old ballast container enqueues fresh young children
    # and can leave the collector at its remembered-root gate, before copy.
    ballast = []
    for index in range(96):
        ballast.append([index, index + 1000])
    pending_before = metric(42)
    assert 0 <= pending_before <= 4096
    pending = pending_before
    drain_steps = 0
    # GC4 drains at most eight remembered entries per step. Spend only that
    # batch's budget here, so preparation does not request evacuation. A
    # non-converging queue is a failed precondition, never a retry for moves.
    for index in range((pending_before + 7) // 8):
        if pending == 0:
            break
        budget = min(pending, 8)
        step(budget)
        remaining = metric(42)
        assert 0 <= remaining < pending
        pending = remaining
        drain_steps += 1
    assert pending == 0
    before = metric(15)
    selected = select_moving(4096)
    processed = step(4096)
    moved = metric(15) - before
    # The roots and payload must survive the measured relocation window.
    assert len(ballast) == 96
    for index in range(96):
        assert ballast[index] == [index, index + 1000]
    evidence.append((phase, selected, processed, moved,
                     pending_before, pending, drain_steps))

class Nested:
    def __float__(self):
        events.append("nested")
        return 7.25

class Number:
    def __float__(self):
        events.append("callback-enter")
        move_live_objects("callback")
        assert converter(Nested()) == 7.25
        events.append("callback-exit")
        return 3.5

class InvalidReturn:
    def __del__(self):
        # The named dispatcher releases its receiver lease before dropping
        # this invalid return. The canonical float caller must still own an
        # independent element lease while cleanup reenters user code/GC.
        events.append("cleanup-enter")
        move_live_objects("cleanup")
        assert converter(Nested()) == 7.25
        events.append("cleanup-exit")

class BadNumber:
    def __float__(self):
        return InvalidReturn()
    def __del__(self):
        events.append("receiver-finalized")

def main():
    assert backend() == 4
    gc.disable()
    assert converter(Number()) == 3.5
    caught = False
    try:
        converter(BadNumber())
    except TypeError:
        caught = True
    assert caught
    gc.collect()
    for row in evidence:
        print(row[0], row[1], row[2], row[3], row[4], row[5], row[6])
    assert events == ["callback-enter", "nested", "callback-exit", "cleanup-enter", "nested", "cleanup-exit", "receiver-finalized"]
    assert len(evidence) == 2
    for row in evidence:
        # A callback, selected set, or successful collector call is not a
        # movement witness. Require production relocation-forward progress
        # independently in both the successful callback and error cleanup.
        assert row[1] > 0
        assert row[3] > 0
        assert row[5] == 0
    gc.enable()
    print("FLOAT_CALLBACK_MOVEMENT_OK")
main()
'''


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_canonical_float_callback_movement_gc4(
    python_program_compiler, request, explicit_owned_runtime, tmp_path, capfd,
):
    # This is an instrumented native correctness witness, not a CPython
    # differential program or a claim that the protected receiver must move.
    source = tmp_path / "float_callback_movement.py"
    binary = tmp_path / "float_callback_movement.out"
    source.write_text(PROGRAM, encoding="utf-8")
    mode = request.node.callspec.params["python_program_compiler"]
    if python_program_compiler.__module__ == "tests.pcc1_route":
        mode = "pcc1-routed"
    receipt = {
        "status": "RUNNING", "compiler_mode": mode,
        "backend": "self", "libpython": "off", "gc_backend": 4,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "runtime_archive": str(explicit_owned_runtime),
        "runtime_sha256": hashlib.sha256(explicit_owned_runtime.read_bytes()).hexdigest(),
    }
    receipt_path = tmp_path / "float-callback-movement.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    try:
        python_program_compiler(
            str(source), str(binary), backend="self", libpython_mode="off",
            ir_scaffold_mode="on", runtime_archive=str(explicit_owned_runtime),
        )
    except Exception as error:
        receipt.update(status="COMPILE_FAILED", error=type(error).__name__ + ": " + str(error))
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        raise
    finally:
        captured = capfd.readouterr()
        (tmp_path / "compiler.stdout").write_text(captured.out)
        (tmp_path / "compiler.stderr").write_text(captured.err)
    assert binary.is_file()
    receipt["binary_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
    environment = dict(
        os.environ, PCC_GC_BACKEND="4", PCC_GC_MINOR_ALLOC_MAX="4096",
        PCC_GC_REFCOUNT_PROVENANCE_PROBE="2", PATH="",
        PCC_HOST_PYTHON="/nonexistent/host-python", PCC_HOST_PCC="/nonexistent/host-pcc",
        PCC_TEST_NO_NATIVE_PROVISIONING="1", PCC_NO_AUTO_PCC1="1",
    )
    environment.pop("LC_ALL", None)
    result = run_process_group_timeout([str(binary)], env=environment, timeout=20)
    (tmp_path / "gc4.stdout").write_text(result.stdout)
    (tmp_path / "gc4.stderr").write_text(result.stderr)
    receipt.update(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)
    lines = result.stdout.splitlines()
    success = result.returncode == 0 and result.stderr == "" and len(lines) == 3
    if success:
        success = lines[-1] == "FLOAT_CALLBACK_MOVEMENT_OK"
    if success:
        for phase, line in zip(("callback", "cleanup"), lines[:2]):
            fields = line.split()
            success = (len(fields) == 7 and fields[0] == phase
                       and all(field.lstrip("-").isdigit() for field in fields[1:])
                       and int(fields[1]) > 0 and int(fields[3]) > 0
                       and 0 <= int(fields[4]) <= 4096 and int(fields[5]) == 0
                       and 0 <= int(fields[6]) <= (int(fields[4]) + 7) // 8)
            if not success:
                break
    receipt["status"] = "PASS" if success else "NATIVE_EXECUTION_FAILED"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    assert success, receipt
