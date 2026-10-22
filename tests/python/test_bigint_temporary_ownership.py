"""Exact integer temporaries remain owned in raw-ABI compiler modules."""

import os
import subprocess


def test_boxed_integer_bounds_and_error_operands_are_released(
    tmp_path, pcc_runtime_archive, python_program_compiler, monkeypatch,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "integer_bounds.py"
    source.write_text('''import gc
from pcc.extern import extern, c_int64
heap = extern("pcc_os_heap_in_use_bytes", (), c_int64)
def check_unsigned(value: object, bits: int):
    limit = (1 << bits) - 1
    assert 0 <= value <= limit
def check_signed(value: object, bits: int):
    lower = -(1 << (bits - 1))
    upper = (1 << (bits - 1)) - 1
    assert lower <= value <= upper
def scan(count: int):
    for index in range(count):
        check_unsigned(1, 64)
        check_unsigned(2, 32)
        check_unsigned(3, 8)
        check_signed(0, 64)
def fail(bits: int):
    try:
        (1 << bits) // 0
    except ZeroDivisionError:
        return True
    return False
def main():
    scan(16)
    gc.collect()
    before = heap()
    scan(10000)
    gc.collect()
    print(heap() - before)
    large = 1 << 80
    check_unsigned(large - 1, 80)
    check_signed(large // 2 - 1, 80)
    check_signed(-(large // 2), 80)
    assert -(large // 2) == -604462909807314587353088
    assert ~large == -1208925819614629174706177
    assert fail(80)
    assert large == 1208925819614629174706176
main()
''')
    output = tmp_path / "integer_bounds"
    python_program_compiler(str(source), str(output), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        if backend == 0:
            assert int(result.stdout) < 16384, result.stdout
