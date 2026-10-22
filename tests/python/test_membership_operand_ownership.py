"""Membership owns evaluated operands until contains has finished."""

import os
import subprocess

import pytest


@pytest.mark.parametrize("container", ["[(True, 2)]", "((True, 2),)", "{(True, 2)}", "{(True, 2): 1}"])
def test_membership_releases_temporary_tuple_needle(
    tmp_path, pcc_runtime_archive, python_program_compiler, monkeypatch, container,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "membership_owner.py"
    source.write_text(f'''import gc
from pcc.extern import extern, c_int64
heap = extern("pcc_os_heap_in_use_bytes", (), c_int64)
def scan(container, count):
    for index in range(count):
        assert (True, 2) in container
        assert (False, 3) not in container
def main():
    container = {container}
    scan(container, 32)
    gc.collect()
    before = heap()
    scan(container, 10000)
    gc.collect()
    middle = heap()
    scan(container, 10000)
    gc.collect()
    print(middle - before, heap() - middle)
main()
''')
    binary = tmp_path / "membership_owner"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15,
                             env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert run.returncode == 0, (backend, run.stdout, run.stderr)
        growth1, growth2 = map(int, run.stdout.split())
        if backend == 0:
            assert growth1 < 16384 and growth2 < 16384, (growth1, growth2)


def test_membership_tuple_literal_evaluates_once_in_order_and_short_circuits(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "membership_order.py"
    source.write_text('''events = []
def left():
    events.append(1)
    return 7
def first():
    events.append(2)
    return 7
def second():
    events.append(3)
    return 8
class Match:
    def __eq__(self, other):
        events.append(4)
        return True
class Bomb:
    def __eq__(self, other):
        raise ValueError("must not compare after a match")
def main():
    print(left() in (first(), second()))
    print(events)
    print(0 in (Match(), Bomb()))
    print(events)
main()
''')
    binary = tmp_path / "membership_order"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "True\n[1, 2, 3]\nTrue\n[1, 2, 3, 4]\n"


def test_membership_cleans_operands_on_errors_and_retains_borrowed_needle(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "membership_errors.py"
    source.write_text('''import gc
destroyed = 0
class Needle:
    def __del__(self):
        global destroyed
        destroyed += 1
class Broken:
    def __contains__(self, value):
        raise ValueError("contains")
    def __del__(self):
        global destroyed
        destroyed += 1
def fail():
    raise ValueError("rhs")
anchor = Needle()
def rebind():
    global anchor
    previous = anchor
    anchor = None
    gc.collect()
    return [previous]
def main():
    try:
        Needle() in fail()
    except ValueError:
        pass
    gc.collect()
    print(destroyed)
    try:
        Needle() not in Broken()
    except ValueError:
        pass
    gc.collect()
    print(destroyed)
    print(anchor in rebind())
    gc.collect()
    print(destroyed)
main()
''')
    binary = tmp_path / "membership_errors"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10,
                             env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert run.returncode == 0, (backend, run.stdout, run.stderr)
        assert run.stdout == "1\n3\nTrue\n4\n", (backend, run.stdout)
