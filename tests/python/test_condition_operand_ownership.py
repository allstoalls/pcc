"""Truth tests consume temporary field reads on every short-circuit path."""

import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("condition,expected", [
    ("holder.items", 2000), ("not holder.items", 0),
    ("holder.items or holder.empty", 2000),
    ("holder.empty or holder.items", 2000),
    ("holder.items and holder.empty", 0),
    ("[1 for row in [holder] if row.items]", 2000),
    ("[1 for row in [holder] if row.empty]", 0),
])
def test_condition_field_reads_release_their_tree(
    tmp_path: Path, pcc_py_runtime_archive, condition, expected,
):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "condition_owner.py"
    source.write_text('''from pcc.extern import c_int64, extern
live_bytes = extern("pcc_os_heap_in_use_bytes", (), c_int64)
class Item:
    def __init__(self):
        self.value = 7
class Holder:
    def __init__(self, items):
        self.items = items
        self.empty = []
def exercise() -> int:
    holder = Holder([Item()])
    if CONDITION:
        return 1
    return 0
def scan(count: int) -> int:
    index = 0
    total = 0
    while index < count:
        total += exercise()
        index += 1
    return total
def main():
    scan(16)
    before = live_bytes()
    first = scan(2000)
    middle = live_bytes()
    second = scan(2000)
    print(first, second, middle - before, live_bytes() - middle)
main()
'''.replace("CONDITION", condition))
    binary = tmp_path / "condition_owner"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND="0"),
                         capture_output=True, text=True, timeout=15)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    first, second, growth1, growth2 = map(int, ran.stdout.split())
    assert first == second == expected
    assert growth1 < 16384 and growth2 < 16384, (growth1, growth2)


def test_truth_tests_short_circuit_once_and_release_after_errors(tmp_path: Path, pcc_py_runtime_archive):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "truth_order.py"
    source.write_text('''import gc
events = []
released = 0
class Probe:
    def __init__(self, name: str, truth: bool, fail: bool = False):
        self.name = name
        self.truth = truth
        self.fail = fail
    def __bool__(self):
        events.append(self.name)
        gc.collect()
        if self.fail:
            raise ValueError("truth failed")
        return self.truth
    def __del__(self):
        global released
        released += 1
def exercise():
    if Probe("A", True) or Probe("B", True):
        pass
    if Probe("C", False) and Probe("D", True):
        pass
    value = 7 if Probe("T", True) else 9
    try:
        if Probe("F", True, True):
            pass
    except ValueError:
        pass
    return value
def main():
    value = exercise()
    gc.collect()
    print(events, value, released)
main()
''')
    binary = tmp_path / "truth_order"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                             capture_output=True, text=True, timeout=15)
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr
        assert ran.stdout.strip() == "['A', 'C', 'T', 'F'] 7 4"
