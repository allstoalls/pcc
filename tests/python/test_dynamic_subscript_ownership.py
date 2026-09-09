"""Public native getitem returns a new ref even in raw scaffold modules."""

import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("source,key", [("[Item(7)]", "0"), ('{"key": Item(7)}', '"key"')])
def test_dynamic_subscript_result_does_not_retain_its_tree(
    tmp_path: Path, pcc_py_runtime_archive, source, key,
):
    from pcc.py_frontend.pipeline import compile_python

    program = tmp_path / "dynamic_item.py"
    program.write_text('''from pcc.extern import c_int64, extern
live_bytes = extern("pcc_os_heap_in_use_bytes", (), c_int64)
class Item:
    def __init__(self, value: int):
        self.value = value
def read(values) -> int:
    item = values[KEY]
    return item.value
def exercise() -> int:
    return read(SOURCE)
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
'''.replace("KEY", key).replace("SOURCE", source))
    binary = tmp_path / "dynamic_item"
    compile_python(str(program), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND="0"),
                         capture_output=True, text=True, timeout=15)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    first, second, growth1, growth2 = map(int, ran.stdout.split())
    assert first == second == 14000
    assert growth1 < 16384 and growth2 < 16384, (growth1, growth2)
