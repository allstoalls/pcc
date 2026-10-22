"""An idle GC4 step clears the allocation debt instead of re-arming itself.

GC4 schedules an automatic step when allocation debt crosses the pause
threshold and pays the debt only with the work a step completes. After a
large allocation into a heap with nothing to mark or move, every step did no
work, the debt stayed above the threshold and the next safepoint ran another
stop-the-world step with a full relocation-candidate ranking. Touching each
page of one 300 MB bytearray made one step per loop back-edge (5x GC3's time
on macOS; over the 20 s budget for the worker-size prior's GC4 growth case on
the Windows CI runner).
"""
from __future__ import annotations

import os
import subprocess

from pcc.frontends.python.pipeline import compile_python

_PROGRAM = """
from pcc.extern import c_int64, extern

_telemetry = extern("pcc_gc_telemetry", (c_int64,), c_int64)
_STEPS = 5


def touch(size):
    payload = bytearray(size)
    for offset in range(0, size, 4096):
        payload[offset] = 1
    payload[size - 1] = 7
    return payload


def main():
    before = _telemetry(_STEPS)
    big = touch(64 * 1024 * 1024)
    steps = _telemetry(_STEPS) - before
    print('steps', steps)
    print(big[0], big[len(big) - 1], len(big))


main()
"""


def test_idle_gc4_steps_do_not_repeat_at_every_safepoint(tmp_path):
    source = tmp_path / "idle_debt.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    binary = tmp_path / "idle_debt"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on")
    outputs = {}
    for collector in ("0", "3", "4"):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=120,
                                env=dict(os.environ, PCC_GC_BACKEND=collector))
        assert result.returncode == 0, (collector, result.stderr)
        steps_line, value_line = result.stdout.splitlines()
        assert value_line == "1 7 67108864", (collector, result.stdout)
        outputs[collector] = int(steps_line.split()[1])
    # 16,384 loop back-edges touch the pages; an idle collector may step a
    # few times, not once per back-edge.
    assert outputs["4"] < 64, outputs
