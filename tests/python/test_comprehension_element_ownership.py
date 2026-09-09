"""Comprehension insertion borrows elements; temporary owners must be consumed."""
import os
from pathlib import Path
import subprocess
import pytest

@pytest.mark.parametrize("expression", [
    '[row.text.strip() for row in rows]',
    '[f"{row.text}" for row in rows]',
    r'[f"  {row.text}\n" for row in rows]',
    '{row.text.strip() for row in rows}',
    '{row.text.strip(): row.text.upper() for row in rows}',
])
def test_comprehension_elements_do_not_accumulate(tmp_path: Path, pcc_py_runtime_archive, expression):
    from pcc.py_frontend.pipeline import compile_python
    source = tmp_path / "comp_elements.py"
    source.write_text('''from pcc.extern import c_int64, extern
live_bytes = extern("pcc_os_heap_in_use_bytes", (), c_int64)
class Row:
    def __init__(self, text: str):
        self.text = text
def exercise() -> int:
    rows = [Row(" some instruction text ")]
    result = EXPRESSION
    return len(result)
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
'''.replace("EXPRESSION", expression))
    binary = tmp_path / "comp_elements"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND="0"),
                         capture_output=True, text=True, timeout=15)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    first, second, growth1, growth2 = map(int, ran.stdout.split())
    assert first == second == 2000
    assert growth1 < 16384 and growth2 < 16384, (growth1, growth2)


def test_comprehension_failure_releases_elements(tmp_path: Path, pcc_py_runtime_archive):
    from pcc.py_frontend.pipeline import compile_python
    source = tmp_path / "comp_errors.py"
    source.write_text('''import gc
released = []
class Item:
    def __init__(self, label: str):
        self.label = label
    def __hash__(self):
        gc.collect()
        raise ValueError("bad hash")
    def __del__(self):
        released.append(self.label)
def fail():
    gc.collect()
    raise ValueError("bad value")
def exercise():
    try:
        result = {Item("set") for i in [1]}
    except ValueError:
        pass
    try:
        result = {Item("key"): Item("value") for i in [1]}
    except ValueError:
        pass
    try:
        result = {Item("early-key"): fail() for i in [1]}
    except ValueError:
        pass
    result = [Item("list") for i in [1]]
    if result[0].label != "list":
        raise RuntimeError("element freed early")
def main():
    exercise()
    gc.collect()
    print(sorted(released))
main()
''')
    binary = tmp_path / "comp_errors"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                             capture_output=True, text=True, timeout=15)
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr
        assert ran.stdout.strip() == "['early-key', 'key', 'list', 'set', 'value']"
