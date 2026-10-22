"""Constructor staging borrows arguments without retaining completed trees."""

import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("argument", ["", "[]", "make()"])
def test_constructor_temporaries_are_released(tmp_path: Path, pcc_runtime_archive, argument):
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "constructor_temps.py"
    source.write_text('''from dataclasses import dataclass, field
from pcc.extern import c_int64, extern
live_bytes = extern("pcc_os_heap_in_use_bytes", (), c_int64)
@dataclass
class Holder:
    values: list[int] = field(default_factory=list)
def make() -> list[int]:
    return []
def exercise() -> int:
    holder = Holder(ARGUMENT)
    holder.values.append(7)
    return len(holder.values)
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
'''.replace("ARGUMENT", argument))
    binary = tmp_path / "constructor_temps"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND="0"),
                         capture_output=True, text=True, timeout=15)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    first, second, growth1, growth2 = map(int, ran.stdout.split())
    assert first == second == 2000
    assert growth1 < 16384 and growth2 < 16384, (growth1, growth2)


def test_constructor_arguments_keep_source_order_and_cleanup_on_failure(
    tmp_path: Path, pcc_runtime_archive,
):
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "constructor_failures.py"
    source.write_text('''import gc
from pcc.extern import c_int64, extern
live_bytes = extern("pcc_os_heap_in_use_bytes", (), c_int64)
events = []
anchor = [7]
class Item:
    def __del__(self):
        gc.collect()
        events.append(1)
class Box:
    def __init__(self, left, right):
        if right:
            raise ValueError("init failed")
        self.left = left
def fail() -> int:
    raise ValueError("argument failed")
def rebind() -> int:
    global anchor
    anchor = [99]
    gc.collect()
    return 0
def exercise():
    try:
        Box(Item(), fail())
    except ValueError:
        pass
    try:
        Box(Item(), 1)
    except ValueError:
        pass
def main():
    box = Box(anchor, rebind())
    if box.left[0] != 7:
        raise RuntimeError("argument order or lifetime")
    exercise()
    gc.collect()
    if len(events) != 2:
        raise RuntimeError("failed constructor argument not released")
    print("constructor-owners-ok")
main()
''')
    binary = tmp_path / "constructor_failures"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                             capture_output=True, text=True, timeout=15)
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr
        assert ran.stdout.strip() == "constructor-owners-ok"


@pytest.mark.parametrize("arguments", [
    "tuple(left), tuple(right)",
    "left=tuple(left), right=tuple(right)",
])
def test_classmethod_constructor_consumes_temporary_tuples(
    tmp_path, monkeypatch, python_program_compiler, pcc_runtime_archive, arguments,
):
    """The NativeObject.from_sections calling shape, including kwargs."""
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "classmethod_owners.py"
    source.write_text('''import gc
from pcc.extern import extern, c_int64
heap = extern("pcc_os_heap_in_use_bytes", (), c_int64)
created = 0
destroyed = 0
class Item:
    def __init__(self):
        global created
        self.payload = b"x" * 256
        created += 1
    def __del__(self):
        global destroyed
        destroyed += 1
class Group:
    def __init__(self, left: tuple, right: tuple):
        self.left = left
        self.right = right
    @classmethod
    def make(cls, left: list, right: list):
        return cls(ARGUMENTS)
def run():
    left = [Item() for i in range(1000)]
    right = [Item() for i in range(1000)]
    result = Group.make(left, right)
    assert len(result.left) == len(result.right) == 1000
def main():
    run()
    gc.collect()
    gc.collect()
    assert destroyed == created == 2000
    before = heap()
    run()
    gc.collect()
    gc.collect()
    assert destroyed == created == 4000
    print(created, destroyed, heap() - before)
main()
'''.replace("ARGUMENTS", arguments))
    binary = tmp_path / "classmethod_owners"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        ran = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)),
        )
        assert ran.returncode == 0, (backend, ran.stdout, ran.stderr)
        created, destroyed, growth = map(int, ran.stdout.split())
        assert created == destroyed == 4000
        if backend == 0:
            assert growth < 16384, growth


def test_classmethod_constructor_arguments_survive_rebind_and_unwind(
    tmp_path, monkeypatch, python_program_compiler, pcc_runtime_archive,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "classmethod_unwind.py"
    source.write_text('''import gc
events = []
anchor = [7]
class Item:
    def __del__(self):
        gc.collect()
        events.append(1)
class Box:
    def __init__(self, left, right):
        if right:
            raise ValueError("init failed")
        self.left = left
    @classmethod
    def failed_argument(cls):
        return cls(Item(), fail())
    @classmethod
    def failed_init(cls):
        return cls(Item(), 1)
    @classmethod
    def borrowed_source(cls):
        return cls(anchor, rebind())
def fail() -> int:
    raise ValueError("argument failed")
def rebind() -> int:
    global anchor
    anchor = [99]
    gc.collect()
    return 0
def exercise():
    try:
        Box.failed_argument()
    except ValueError as exc:
        assert str(exc) == "argument failed"
    else:
        assert False
    try:
        Box.failed_init()
    except ValueError as exc:
        assert str(exc) == "init failed"
    else:
        assert False
def main():
    box = Box.borrowed_source()
    assert box.left[0] == 7
    assert anchor[0] == 99
    exercise()
    gc.collect()
    assert len(events) == 2
    print("classmethod-owners-ok")
main()
''')
    binary = tmp_path / "classmethod_unwind"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        ran = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)),
        )
        assert ran.returncode == 0, (backend, ran.stdout, ran.stderr)
        assert ran.stdout.strip() == "classmethod-owners-ok"
