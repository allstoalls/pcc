"""An indexed for-loop must own its iterable until every exit is finished."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("exit_kind", ["exhausted", "break", "return", "error"])
def test_typed_field_iteration_releases_object_tree(tmp_path: Path, pcc_py_runtime_archive, exit_kind):
    from pcc.py_frontend.pipeline import compile_python

    action = {"exhausted": "pass", "break": "break", "return": "return total",
              "error": 'raise ValueError("expected")'}[exit_kind]
    source = tmp_path / "typed_field_loop.py"
    source.write_text('''from dataclasses import dataclass, field
from pcc.extern import c_int64, extern
live_bytes = extern("pcc_os_heap_in_use_bytes", (), c_int64)
@dataclass
class Item:
    value: int
class Holder:
    def __init__(self):
        self.values: list[Item] = []
def exercise() -> int:
    holder = Holder()
    holder.values.append(Item(7))
    total = 0
    try:
        for item in holder.values:
            total += item.value
            ACTION
    except ValueError:
        pass
    return total
def scan(count: int) -> int:
    total = 0
    index = 0
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
'''.replace("ACTION", action))
    binary = tmp_path / "typed_field_loop"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND="0"),
                         capture_output=True, text=True, timeout=15)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    first, second, growth1, growth2 = map(int, ran.stdout.split())
    assert first == second == 14000
    assert growth1 < 16384 and growth2 < 16384, (growth1, growth2)


@pytest.mark.parametrize("source_kind", ["field", "local", "tuple", "dict"])
def test_indexed_iterable_survives_rebinding_and_gc(tmp_path: Path, pcc_py_runtime_archive, source_kind):
    from pcc.py_frontend.pipeline import compile_python

    setup, iterable, rebind = {
        "field": ('holder = Holder([7, 8])', 'holder.values', 'holder.values = [99]'),
        "local": ('values = [7, 8]', 'values', 'values = [99]'),
        "tuple": ('values = (7, 8)', 'values', 'values = (99, 100)'),
        "dict": ('values = {7: 1, 8: 1}', 'values', 'values = {99: 1}'),
    }[source_kind]
    source = tmp_path / "list_rebind.py"
    source.write_text('''import gc
from dataclasses import dataclass
@dataclass
class Holder:
    values: list[int]
def exercise() -> int:
    SETUP
    total = 0
    for value in ITERABLE:
        REBIND
        gc.collect()
        total += value
    return total
def main():
    print(exercise())
main()
'''.replace("SETUP", setup).replace("ITERABLE", iterable).replace("REBIND", rebind))
    oracle = subprocess.run([sys.executable, str(source)], capture_output=True,
                            text=True, check=True, timeout=10)
    binary = tmp_path / "list_rebind"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                             capture_output=True, text=True, timeout=15)
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr
        assert ran.stdout == oracle.stdout
