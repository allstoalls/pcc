"""The raw-int ABI must not discard the native regex NEW-ref contract."""

import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("call,text,expected", [
    ("PATTERN.match(text)", "hello", 2000),
    ('re.match(r"([a-z]+)", text)', "hello", 2000),
    ('re.match(r"([a-z]+)", text)', "123", 0),
    ("OPCODE.match(text)", "%slot = alloca i64", 2000),
    ("HEADER.match(text)", "define", 2000),
])
def test_regex_results_are_owned_in_raw_scaffold_modules(
    tmp_path: Path, pcc_py_runtime_archive, call, text, expected,
):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "regex_owner.py"
    source.write_text(r'''import re
from pcc.extern import c_int64, extern
live_bytes = extern("pcc_os_heap_in_use_bytes", (), c_int64)
PATTERN = re.compile(r"([a-z]+)")
OPCODE = re.compile(r"^\s*(?:%[\w\.]+\s*=\s*)?(\w+)")
HEADER = re.compile(r" (?P<name>\w+) ", re.VERBOSE)
def scan(text: str, count: int) -> int:
    index = 0
    hits = 0
    while index < count:
        match = CALL
        if match is not None:
            hits += match.start() + 1
        index += 1
    return hits
def main():
    scan(TEXT, 8)
    before = live_bytes()
    first = scan(TEXT, 2000)
    middle = live_bytes()
    second = scan(TEXT, 2000)
    after = live_bytes()
    print(first, second, middle - before, after - middle)
main()
'''.replace("CALL", call).replace("TEXT", repr(text)))
    binary = tmp_path / "regex_owner"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND="0"),
                         capture_output=True, text=True, timeout=15)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    first, second, growth1, growth2 = map(int, ran.stdout.split())
    assert first == second == expected
    assert growth1 < 16384 and growth2 < 16384, (growth1, growth2)


def test_dynamic_regex_compile_releases_pattern_and_argument(tmp_path, pcc_py_runtime_archive):
    from pcc.py_frontend.pipeline import compile_python
    source = tmp_path / "dynamic_compile_owner.py"
    source.write_text('''import re
from pcc.extern import extern, c_int64
live = extern("pcc_os_heap_in_use_bytes", (), c_int64)
def scan(prefix: str, count: int):
    index = 0
    hits = 0
    while index < count:
        pattern = re.compile("^" + prefix + "$", 0)
        match = pattern.match(prefix)
        if match is not None:
            hits += 1
        index += 1
    return hits
def main():
    scan("alpha", 8)
    before = live()
    first = scan("alpha", 1000)
    middle = live()
    second = scan("alpha", 1000)
    after = live()
    print(first, second, middle - before, after - middle)
main()
''')
    binary = tmp_path / "dynamic_compile_owner"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                             capture_output=True, text=True, timeout=20)
        assert ran.returncode == 0, ran.stdout + ran.stderr
        first, second, growth1, growth2 = map(int, ran.stdout.split())
        assert first == second == 1000
        if backend == 0:
            assert growth1 < 16384 and growth2 < 16384, (growth1, growth2)


def test_regex_sub_consumes_temporary_operands(tmp_path, pcc_py_runtime_archive):
    from pcc.py_frontend.pipeline import compile_python
    source = tmp_path / "regex_sub_owners.py"
    source.write_text('''import re
from pcc.extern import extern, c_int64
live = extern("pcc_os_heap_in_use_bytes", (), c_int64)
def scan(prefix: str, text: str, count: int):
    index = 0
    hits = 0
    while index < count:
        result = re.sub("^" + prefix.strip() + "$", prefix.strip(), text.strip())
        if result == "alpha":
            hits += 1
        index += 1
    return hits
def main():
    scan(" alpha ", " alpha ", 8)
    before = live()
    first = scan(" alpha ", " alpha ", 1000)
    middle = live()
    second = scan(" alpha ", " alpha ", 1000)
    print(first, second, middle - before, live() - middle)
main()
''')
    binary = tmp_path / "regex_sub_owners"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND="0"),
                         capture_output=True, text=True, timeout=20)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    first, second, growth1, growth2 = map(int, ran.stdout.split())
    assert first == second == 1000
    assert growth1 < 16384 and growth2 < 16384, (growth1, growth2)


def test_regex_sub_evaluation_order_and_argument_error_cleanup(tmp_path, pcc_py_runtime_archive):
    from pcc.py_frontend.pipeline import compile_python
    source = tmp_path / "regex_sub_order.py"
    source.write_text('''import re
import gc
events = []
def part(index: int) -> str:
    events.append(index)
    gc.collect()
    if index == 2:
        return "b"
    return "a"
def count() -> int:
    events.append(4)
    gc.collect()
    return 1
def fail() -> str:
    gc.collect()
    raise ValueError("replacement failed")
def main():
    assert re.sub(part(1), part(2), part(3), count()) == "b"
    assert events == [1, 2, 3, 4]
    try:
        re.sub("a" * 256, fail(), "a")
    except ValueError:
        pass
    gc.collect()
    print("SUB_ORDER_OK")
main()
''')
    binary = tmp_path / "regex_sub_order"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                             capture_output=True, text=True, timeout=20)
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}{ran.stderr}"
        assert ran.stdout.strip() == "SUB_ORDER_OK"
