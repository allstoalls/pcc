"""Native string method chains must consume their temporary receiver."""

import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("annotation", [": str", ""])
@pytest.mark.parametrize("expression", [
    'text.lstrip().startswith("x")',
    'len(text.strip().upper())',
    'len(text.split("=", 1)[1].lstrip())',
])
def test_temporary_string_method_receivers_are_released(
    tmp_path: Path, pcc_py_runtime_archive, expression, annotation,
):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "string_receiver.py"
    source.write_text('''from pcc.extern import c_int64, extern
live_bytes = extern("pcc_os_heap_in_use_bytes", (), c_int64)
def scan(textANNOTATION, count: int) -> int:
    index = 0
    total = 0
    while index < count:
        total += EXPRESSION
        index += 1
    return total
def main():
    text = " x=" + "x" * 256
    scan(text, 16)
    before = live_bytes()
    first = scan(text, 2000)
    middle = live_bytes()
    second = scan(text, 2000)
    print(first, second, middle - before, live_bytes() - middle)
main()
'''.replace("EXPRESSION", expression).replace("ANNOTATION", annotation))
    binary = tmp_path / "string_receiver"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND="0"),
                         capture_output=True, text=True, timeout=15)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    first, second, growth1, growth2 = map(int, ran.stdout.split())
    expected = { 'text.lstrip().startswith("x")': 2000,
                 'len(text.strip().upper())': 516000,
                 'len(text.split("=", 1)[1].lstrip())': 512000 }[expression]
    assert first == second == expected
    assert growth1 < 16384 and growth2 < 16384, (growth1, growth2)


def test_string_receiver_survives_rhs_rebinding_and_failure(tmp_path: Path, pcc_py_runtime_archive):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "receiver_errors.py"
    source.write_text('''import gc
from pcc.extern import c_int64, extern
live_bytes = extern("pcc_os_heap_in_use_bytes", (), c_int64)
anchor = "a" * 257
def rebind() -> str:
    global anchor
    anchor = "gone"
    gc.collect()
    return "a"
def fail() -> str:
    raise ValueError("expected")
def scan(count: int):
    index = 0
    while index < count:
        try:
            ("a" * 257).index(fail())
        except ValueError:
            pass
        index += 1
def main():
    if not anchor.startswith(rebind()):
        raise RuntimeError("receiver lost across RHS")
    value = ("a" * 257).strip().strip()
    gc.collect()
    if len(value) != 257:
        raise RuntimeError("returned string lost its owner")
    scan(16)
    before = live_bytes()
    scan(2000)
    print(live_bytes() - before)
main()
''')
    binary = tmp_path / "receiver_errors"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                             capture_output=True, text=True, timeout=15)
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr
        if backend == 0:
            assert int(ran.stdout) < 65536, ran.stdout
