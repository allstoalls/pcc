"""Sized bytes constructors preserve zero-fill and error behavior."""

import os
import subprocess
import sys


_SOURCE = '''
def main():
    for size in [0, 1, 32, 257, True, False]:
        immutable = bytes(size)
        mutable = bytearray(size)
        print(len(immutable), len(mutable), immutable == bytes(mutable))
        print(immutable == b"\\x00" * len(immutable))
        if size:
            mutable[0] = 17
            mutable[-1] = 34
            print(mutable[0], mutable[-1], immutable[0])
    for bad in [-1, -257, 2 ** 100]:
        try:
            bytes(bad)
            print("missing bytes error")
        except ValueError:
            print("bytes negative")
        except OverflowError:
            print("bytes overflow")
        try:
            bytearray(bad)
            print("missing bytearray error")
        except ValueError:
            print("bytearray negative")
        except OverflowError:
            print("bytearray overflow")
    print(bytes([1, 2, 255]))
    print(bytearray(b"ok"))
    buffer = bytearray(2)
    try:
        buffer[-3] = 1
        print("missing index error")
    except IndexError:
        print("index error")
    try:
        buffer[0] = 256
        print("missing byte error")
    except ValueError:
        print("byte error")
main()
'''


def test_sized_bytes_constructors_match_cpython(tmp_path, pcc_diagnostic_runtime_archive):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "bytes_count.py"
    source.write_text(_SOURCE)
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=30)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "bytes_count"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_diagnostic_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend), PATH="/nonexistent", PCC_HOST_PYTHON="/usr/bin/false", PCC_RUNTIME_CC="/usr/bin/false"))
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == expected.stdout, backend
