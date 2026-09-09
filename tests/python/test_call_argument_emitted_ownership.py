"""ABI argument cleanup must retain the emitter's NEW-ref evidence."""

import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("call", ["consume(holder.value)", "reader.consume(holder.value)"])
def test_owned_dynamic_field_argument_is_consumed(tmp_path: Path, pcc_py_runtime_archive, call):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "field_argument.py"
    source.write_text('''from pcc.extern import c_int64, extern
live_bytes = extern("pcc_os_heap_in_use_bytes", (), c_int64)
class Item:
    def __init__(self):
        self.number = 7
class Holder:
    def __init__(self, value):
        self.value = value
class Reader:
    def consume(self, value) -> int:
        return value.number
def consume(value) -> int:
    return value.number
def exercise() -> int:
    holder = Holder(Item())
    reader = Reader()
    return CALL
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
'''.replace("CALL", call))
    binary = tmp_path / "field_argument"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND="0"),
                         capture_output=True, text=True, timeout=15)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    first, second, growth1, growth2 = map(int, ran.stdout.split())
    assert first == second == 14000
    assert growth1 < 16384 and growth2 < 16384, (growth1, growth2)


def test_direct_method_result_keeps_owner_through_root_reload(tmp_path, pcc_py_runtime_archive):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "method_result_owner.py"
    source.write_text('''from __future__ import annotations
from dataclasses import dataclass
from pcc.extern import c_int64, extern
live = extern("pcc_os_heap_in_use_bytes", (), c_int64)
@dataclass
class _Block:
    label: str
    lines: list[str]
    def inst_lines(self) -> list[str]:
        out: list[str] = []
        for line in self.lines:
            code = line.split(";", 1)[0].strip()
            if code:
                out.append(code)
        return out
def consume(block: _Block) -> int:
    items = block.inst_lines()
    return len(items)
def scan():
    block = _Block("entry", ["  br label %next\\n"])
    index = 0
    total = 0
    while index < 1000:
        total += consume(block)
        index += 1
    return total
def main():
    scan()
    before = live()
    first = scan()
    middle = live()
    second = scan()
    print(first, second, middle-before, live()-middle)
main()
''')
    binary = tmp_path / "method_result_owner"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                             capture_output=True, text=True, timeout=20)
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}{ran.stderr}"
        first, second, growth1, growth2 = map(int, ran.stdout.split())
        assert first == second == 1000
        if backend == 0:
            assert growth1 < 16384 and growth2 < 16384, (growth1, growth2)


def test_dynamic_callable_consumes_argument_temporaries(tmp_path, pcc_py_runtime_archive):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "dynamic_argument_owner.py"
    source.write_text('''from pcc.extern import c_int64, extern
live = extern("pcc_os_heap_in_use_bytes", (), c_int64)
class Reader:
    def accept(self, text):
        return len(text)
def call(reader, words):
    return reader.accept(words[0].strip())
def scan():
    reader = Reader()
    words = [" " + "x" * 256 + " "]
    index = 0
    total = 0
    while index < 1000:
        total += call(reader, words)
        index += 1
    return total
def main():
    scan()
    before = live()
    first = scan()
    middle = live()
    second = scan()
    print(first, second, middle-before, live()-middle)
main()
''')
    binary = tmp_path / "dynamic_argument_owner"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                             capture_output=True, text=True, timeout=20)
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}{ran.stderr}"
        first, second, growth1, growth2 = map(int, ran.stdout.split())
        assert first == second == 256000
        if backend == 0:
            assert growth1 < 16384 and growth2 < 16384, (growth1, growth2)


def test_dynamic_arguments_keep_order_and_unwind_after_later_failure(tmp_path, pcc_py_runtime_archive):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "dynamic_argument_failure.py"
    source.write_text('''import gc
events = []
class Argument:
    def __init__(self):
        events.append(1)
    def __del__(self):
        events.append(4)
class Reader:
    def accept(self, first, second):
        events.append(9)
def fail():
    events.append(2)
    gc.collect()
    raise ValueError("later argument")
def invoke(callback):
    try:
        callback(Argument(), fail())
    except ValueError:
        events.append(3)
def main():
    reader = Reader()
    invoke(reader.accept)
    gc.collect()
    assert events[:2] == [1, 2]
    assert len(events) == 4
    assert 3 in events and 4 in events and 9 not in events
    print("ARGUMENT_UNWIND_OK")
main()
''')
    binary = tmp_path / "dynamic_argument_failure"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                             capture_output=True, text=True, timeout=20)
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}{ran.stderr}"
        assert ran.stdout.strip() == "ARGUMENT_UNWIND_OK"
