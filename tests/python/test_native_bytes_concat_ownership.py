"""Byte concatenation results and replaced locals have separate owners."""

import os
import subprocess
import sys

import pytest

from pcc.py_frontend.pipeline import compile_python


@pytest.mark.parametrize("kind", ["bytes", "bytearray"])
@pytest.mark.parametrize("statement", ["data += b''", "data = data + b''"])
def test_byte_concat_releases_replaced_buffers(
    tmp_path, pcc_py_runtime_archive, kind, statement,
):
    source = tmp_path / "concat.py"
    source.write_text(f'''
from pcc.extern import c_int64, extern
live_bytes = extern("pcc_os_heap_in_use_bytes", (), c_int64)
def main():
    data = {kind}(b"x" * 65536)
    before = live_bytes()
    index = 0
    while index < 64:
        {statement}
        index += 1
    after = live_bytes()
    print(len(data), data[0], data[-1], after - before)
main()
''', encoding="utf-8")
    output = tmp_path / "concat"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f"GC{gc}: {result.stderr}"
        size, first, last, growth = map(int, result.stdout.split())
        assert (size, first, last) == (65536, 120, 120)
        if gc == 0:
            assert growth < 262144, (kind, statement, growth)


def test_byte_concat_copies_both_payloads_and_preserves_family(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / "concat_payload.py"
    source.write_text('''import gc
def main():
    for length in [0, 1, 16, 257, 4096]:
        left = bytes([i % 256 for i in range(length)])
        right = bytes([255 - i % 256 for i in range(length + 3)])
        for a in [left, bytearray(left)]:
            for b in [right, bytearray(right)]:
                result = a + b
                gc.collect()
                print(type(result).__name__, len(result), result.hex())
                if isinstance(result, bytearray):
                    result[0] = 17
                print(a.hex(), b.hex())
main()
''', encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True,
                              text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    output = tmp_path / "concat_payload"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=15,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f"GC{gc}: {result.stderr}"
        assert result.stdout == expected.stdout, f"GC{gc}: payload or family mismatch"
