"""int.from_bytes balances evaluated native arguments on every exit."""

import os
import subprocess


def _execute(source, tmp_path, compiler, runtime):
    path = tmp_path / "from_bytes_owner.py"
    path.write_text(source)
    binary = tmp_path / "from_bytes_owner"
    compiler(str(path), str(binary), backend="self", libpython_mode="off",
             runtime_archive=str(runtime))
    for backend in range(5):
        result = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=15,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        yield backend, result.stdout


def test_from_bytes_releases_slice_and_byteorder_temporaries(
    tmp_path, pcc_runtime_archive, python_program_compiler, monkeypatch,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = '''import gc
from pcc.extern import extern, c_int64
heap = extern("pcc_os_heap_in_use_bytes", (), c_int64)
def order():
    return "li" + "ttle".strip()
def scan(payload, count):
    for index in range(count):
        assert int.from_bytes(payload[1:5], order()) == 67305985
def main():
    payload = b"x\\x01\\x02\\x03\\x04y"
    scan(payload, 32)
    gc.collect()
    before = heap()
    scan(payload, 10000)
    gc.collect()
    middle = heap()
    scan(payload, 10000)
    gc.collect()
    print(middle-before, heap()-middle)
    large = int.from_bytes(b"\\xff" * 16, "big")
    assert str(large) == "340282366920938463463374607431768211455"
    assert large == 2 ** 128 - 1
main()
'''
    for backend, output in _execute(source, tmp_path, python_program_compiler, pcc_runtime_archive):
        growth = list(map(int, output.split()))
        if backend == 0:
            assert all(value < 16384 for value in growth), growth


def test_from_bytes_argument_errors_and_borrowed_rebinding(
    tmp_path, pcc_runtime_archive, python_program_compiler, monkeypatch,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = '''import gc
destroyed = 0
anchor = bytearray(b"\\x01\\x02")
class Token:
    def __del__(self):
        global destroyed
        destroyed += 1
def fail():
    raise ValueError("argument")
def rebind():
    global anchor
    anchor = bytearray(b"changed")
    gc.collect()
    return "little"
def main():
    caught = 0
    for index in range(32):
        try:
            int.from_bytes(Token(), fail())
        except ValueError:
            caught += 1
        try:
            int.from_bytes(Token(), "big")
        except TypeError:
            caught += 1
        try:
            int.from_bytes(b"payload", "invalid")
        except ValueError:
            caught += 1
    assert int.from_bytes(anchor, rebind()) == 513
    gc.collect()
    print(caught, destroyed)
main()
'''
    for backend, output in _execute(source, tmp_path, python_program_compiler, pcc_runtime_archive):
        assert output == "96 64\n", (backend, output)
