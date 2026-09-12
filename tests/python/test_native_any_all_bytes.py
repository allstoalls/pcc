"""any/all consume byte values and release temporary byte sequences."""

import os
import subprocess
import sys

from pcc.py_frontend.pipeline import compile_python


def test_any_all_bytes_and_bytearray_match_python(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / "any_bytes.py"
    source.write_text('''
def check(data: bytes):
    print(any(data), all(data), any(data[1:]), all(data[1:]))
    print(any(bytearray(data)), all(bytearray(data)))
def main():
    check(b"")
    check(b"\\x00\\x00")
    check(b"\\x00\\x01")
    check(b"\\x01\\xff")
main()
''', encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True,
                              text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    output = tmp_path / "any_bytes"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f"GC{gc}: {result.stderr}"
        assert result.stdout == expected.stdout, f"GC{gc}: {result.stdout}"


def test_any_releases_its_temporary_byte_slice(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / "any_owner.py"
    source.write_text('''
from pcc.extern import extern, c_int64
live_bytes = extern("pcc_os_heap_in_use_bytes", (), c_int64)
def scan(data: bytes):
    index = 0
    while index < 64:
        if not any(data[1:]):
            raise AssertionError("missing nonzero byte")
        index += 1
def main():
    data = b"x" * 65536
    before = live_bytes()
    scan(data)
    print(live_bytes() - before)
main()
''', encoding="utf-8")
    output = tmp_path / "any_owner"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    result = subprocess.run([str(output)], capture_output=True, text=True, timeout=20,
                            env=dict(os.environ, PCC_GC_BACKEND="0"))
    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) < 262144, result.stdout
