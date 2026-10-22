"""threading constructors' results are owned values for any consumer.

The native threading lowering returned the NEW object of current_thread(),
Lock(), RLock(), Event(), Condition() and Semaphore() without recording its
owner, so as a call operand (``items.append(threading.current_thread())``,
as test_threading does) the compile failed with "slot-call operand has no
authoritative source or owned result: Call". Each records it now.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


PROGRAM = r'''
import threading


def present(value):
    return value is not None


def main():
    items = []
    items.append(threading.current_thread())
    print(len(items), items[0] == threading.current_thread())
    locks = [threading.Lock(), threading.RLock()]
    print(len(locks), present(threading.Event()), present(threading.Semaphore(2)))
    print(present(threading.Condition()), present(threading.Lock()))
    lock = locks[0]
    with lock:
        print("locked", lock.locked())
    print(lock.locked())
    condition = threading.Condition(locks[1])
    with condition:
        condition.notify_all()
    print("done")


main()
'''


@pytest.mark.integration
def test_threading_constructors_as_operands_on_five_collectors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "threading_operands.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "threading_operands"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend))
        environment.pop("LC_ALL", None)
        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60, env=environment)
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{backend}: {ran.stdout}"
