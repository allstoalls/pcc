"""Transient string operands must not accumulate in comparison loops."""

import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("operator,container,expected", [
    ("==", "a", 50000), ("!=", "a", 50000),
    ("in", "ab", 100000), ("not in", "ab", 0),
])
def test_temporary_string_operands_release_each_iteration(
    tmp_path: Path, pcc_py_runtime_archive, operator, container, expected,
):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "string_operand.py"
    source.write_text('''from pcc.extern import c_int64, extern
live_bytes = extern("pcc_os_heap_in_use_bytes", (), c_int64)

def scan(text: str, count: int) -> int:
    total = 0
    index = 0
    while index < count:
        if text[index & 1] OPERATOR CONTAINER:
            total += 1
        index += 1
    return total

def main():
    scan("ab", 16)
    before = live_bytes()
    first = scan("ab", 100000)
    middle = live_bytes()
    second = scan("ab", 100000)
    after = live_bytes()
    print(first, second, middle - before, after - middle)

main()
'''.replace("OPERATOR", operator).replace("CONTAINER", repr(container)))
    binary = tmp_path / "string_operand"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND="0"),
                         capture_output=True, text=True, timeout=15)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    first, second, growth1, growth2 = map(int, ran.stdout.split())
    assert first == second == expected
    # Live requested bytes, not RSS: allocator capacity may remain mapped.
    assert growth1 < 16384 and growth2 < 16384, (growth1, growth2)


def test_string_predicates_preserve_order_and_lhs_across_rhs_gc(
    tmp_path: Path, pcc_py_runtime_archive,
):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "string_order.py"
    source.write_text('''import gc
events = ""
anchor = "a" * 257
keeper = ""
def left() -> str:
    global events
    events += "L"
    return "a" * 257
def right() -> str:
    global events
    events += "R"
    gc.collect()
    return "a" * 257
def rebind() -> str:
    global anchor, keeper
    anchor = "gone"
    keeper = "x" * 257
    gc.collect()
    return "a" * 257
def main():
    global events, anchor
    if not (left() == right()) or events != "LR":
        raise RuntimeError("equality order or lifetime")
    events = ""
    if not (left() in right()) or events != "LR":
        raise RuntimeError("membership order or lifetime")
    anchor = "a" * 257
    if not (anchor == rebind()):
        raise RuntimeError("borrowed lhs lost its owner")
    print("string-predicates-ok")
main()
''')
    binary = tmp_path / "string_order"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                             capture_output=True, text=True, timeout=15)
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr
        assert ran.stdout.strip() == "string-predicates-ok"


@pytest.mark.parametrize("operator", ["==", "in"])
def test_rhs_failure_releases_temporary_string(tmp_path: Path, pcc_py_runtime_archive, operator):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "string_error.py"
    source.write_text('''from pcc.extern import c_int64, extern
live_bytes = extern("pcc_os_heap_in_use_bytes", (), c_int64)
def make() -> str:
    return "x" * 1024
def fail() -> str:
    raise ValueError("expected")
def scan(count: int):
    index = 0
    while index < count:
        try:
            make() OPERATOR fail()
        except ValueError:
            pass
        index += 1
def main():
    scan(10)
    before = live_bytes()
    scan(2000)
    print(live_bytes() - before)
main()
'''.replace("OPERATOR", operator))
    binary = tmp_path / "string_error"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND="0"),
                         capture_output=True, text=True, timeout=15)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert int(ran.stdout) < 65536, ran.stdout


def test_dynamic_string_equality_order_callback_and_cleanup(tmp_path, pcc_py_runtime_archive):
    from pcc.py_frontend.pipeline import compile_python
    source = tmp_path / "dynamic_equality.py"
    source.write_text('''import gc
from typing import Any
events = []
class Equal:
    def __eq__(self, other):
        events.append(3)
        gc.collect()
        return other == "left"
    def __del__(self):
        events.append(4)
def dynamic(value: Any) -> Any:
    return value
def left() -> str:
    events.append(1)
    return "left"
def right() -> Any:
    events.append(2)
    return dynamic(Equal())
def main():
    assert left() == right()
    gc.collect()
    assert events == [1, 2, 3, 4]
    print("DYNAMIC_EQ_OK")
main()
''')
    binary = tmp_path / "dynamic_equality"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                             capture_output=True, text=True, timeout=20)
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}{ran.stderr}"
        assert ran.stdout.strip() == "DYNAMIC_EQ_OK"
