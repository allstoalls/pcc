"""Containers consume emitter-proven temporaries and typed method results."""
import os
from pathlib import Path
import subprocess
import pytest

@pytest.mark.parametrize("body", [
    'result = (text.strip(),)\n    return len(result)',
    'result = [text.strip()]\n    return len(result)',
    'result = {text.strip()}\n    return len(result)',
    'result = {text.strip(): text.upper()}\n    return len(result)',
    'result = []\n    result.append(text.strip())\n    return len(result)',
    'result = set()\n    result.add(text.strip())\n    return len(result)',
    'result = set()\n    result.add(text.strip())\n    result.add(text.strip())\n    return len(result)',
    'result = set()\n    result.discard(text.strip())\n    return 1',
    'result = set()\n    try:\n        result.remove(text.strip())\n    except KeyError:\n        pass\n    return 1',
    'set().add(text.strip())\n    return 1',
    'result = []\n    result.append(Node.from_text(text.strip()))\n    return len(result)',
])
def test_container_temporaries_are_released(tmp_path: Path, pcc_py_runtime_archive, body):
    from pcc.py_frontend.pipeline import compile_python
    source = tmp_path / "container_owners.py"
    source.write_text('''from pcc.extern import c_int64, extern
live_bytes = extern("pcc_os_heap_in_use_bytes", (), c_int64)
class Node:
    def __init__(self, text: str):
        self.text = text
    @classmethod
    def from_text(cls, text: str) -> "Node":
        return cls(text)
def exercise(text) -> int:
    BODY
def scan(count: int) -> int:
    index = 0
    total = 0
    while index < count:
        total += exercise(" some instruction text ")
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
'''.replace("BODY", body))
    binary = tmp_path / "container_owners"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND="0"),
                         capture_output=True, text=True, timeout=15)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    first, second, growth1, growth2 = map(int, ran.stdout.split())
    assert first == second == 2000
    assert growth1 < 16384 and growth2 < 16384, (growth1, growth2)


def test_literal_unwind_consumes_dynamic_string_temporary(tmp_path: Path, pcc_py_runtime_archive):
    from pcc.py_frontend.pipeline import compile_python
    source = tmp_path / "literal_errors.py"
    source.write_text('''import gc
from pcc.extern import c_int64, extern
live_bytes = extern("pcc_os_heap_in_use_bytes", (), c_int64)
def fail():
    gc.collect()
    raise ValueError("expected")
def scan(text, count: int):
    index = 0
    while index < count:
        try:
            values = (text.strip(), fail())
        except ValueError:
            pass
        index += 1
def main():
    text = " x" + "x" * 256 + " "
    scan(text, 16)
    gc.collect()
    before = live_bytes()
    scan(text, 200)
    gc.collect()
    print(live_bytes() - before)
main()
''')
    binary = tmp_path / "literal_errors"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                             capture_output=True, text=True, timeout=20)
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr
        assert int(ran.stdout.strip()) < 16384, f"GC{backend}: " + ran.stdout


def test_set_temporary_receiver_and_failure_cleanup_gc_backends(tmp_path, pcc_py_runtime_archive):
    from pcc.py_frontend.pipeline import compile_python
    source = tmp_path / "set_errors.py"
    source.write_text('''import gc
from pcc.extern import extern, c_int64
live = extern("pcc_os_heap_in_use_bytes", (), c_int64)
def fail():
    gc.collect()
    raise ValueError("argument failed")
def exercise(text):
    try:
        set().add(fail())
    except ValueError:
        pass
    try:
        set().add([text.strip()])
    except TypeError:
        pass
    try:
        set().remove(text.strip())
    except KeyError as error:
        assert error.args[0] == text.strip()
def scan(text):
    index = 0
    while index < 100:
        exercise(text)
        index += 1
def main():
    text = " " + "x" * 256 + " "
    scan(text)
    gc.collect()
    before = live()
    scan(text)
    gc.collect()
    print(live() - before)
main()
''')
    binary = tmp_path / "set_errors"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                             capture_output=True, text=True, timeout=20)
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}{ran.stderr}"
        assert int(ran.stdout.strip()) < 16384, f"GC{backend}: {ran.stdout}"
