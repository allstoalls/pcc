"""Bytes search windows must preserve indices without retaining copied tails."""

from pathlib import Path
import os
import subprocess
import sys

from pcc.py_frontend.pipeline import compile_python


def _compile(tmp_path: Path, source: str, archive: Path) -> Path:
    path = tmp_path / "find_start.py"
    path.write_text(source, encoding="utf-8")
    output = tmp_path / "find_start"
    compile_python(
        str(path), str(output), backend="self", libpython_mode="off",
        runtime_archive=str(archive),
    )
    return output


def test_bytes_find_start_matches_cpython(tmp_path, pcc_py_runtime_archive):
    source = '''
def main():
    data = b"abcabc"
    print(data.find(b"bc", 0))
    print(data.find(b"bc", 2))
    print(data.find(b"bc", -4))
    print(data.find(b"bc", -100))
    print(data.find(b"bc", 100))
    print(data.find(b"", 6))
    print(data.find(b"", 7))
    print(data.find(b"absent", 0))
    print(b"".find(b"", 0))
    mutable = bytearray(b"a\\x00b\\x00")
    print(mutable.find(b"\\x00", 2))
main()
'''
    output = _compile(tmp_path, source, pcc_py_runtime_archive)
    reference = subprocess.run(
        [sys.executable, str(tmp_path / "find_start.py")],
        capture_output=True, text=True, timeout=10,
    )
    assert reference.returncode == 0, reference.stderr
    for backend in range(5):
        result = subprocess.run(
            [str(output)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == reference.stdout, f"GC{backend}: {result.stdout}"


def test_bytes_find_start_does_not_retain_search_tails(
    tmp_path, pcc_py_runtime_archive,
):
    source = '''
from pcc.extern import c_int64, extern
live_bytes = extern("pcc_os_heap_in_use_bytes", (), c_int64)
def scan(data: bytes, count: int) -> int:
    index = 0
    total = 0
    while index < count:
        total += data.find(b"\\x00", index % 16)
        index += 1
    return total
def main():
    data = b"x" * 65535 + b"\\x00"
    scan(data, 16)
    before = live_bytes()
    first = scan(data, 128)
    middle = live_bytes()
    second = scan(data, 128)
    after = live_bytes()
    print(first, second, middle - before, after - middle)
main()
'''
    output = _compile(tmp_path, source, pcc_py_runtime_archive)
    result = subprocess.run(
        [str(output)], env=dict(os.environ, PCC_GC_BACKEND="0"),
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    first, second, growth1, growth2 = map(int, result.stdout.split())
    assert first == second == 65535 * 128
    assert growth1 < 32768 and growth2 < 32768, (growth1, growth2)


def test_bytes_find_windows_and_index_bounds_match_python(tmp_path, pcc_py_runtime_archive):
    source = '''
import gc
class Bound:
    def __init__(self, value):
        self.value = value
    def __index__(self):
        gc.collect()
        return self.value
def check(data: bytes):
    print(data.find(b"bc", 0, 3), data.find(b"bc", 0, 2))
    print(data.find(b"bc", -4, -1), data.find(b"", 6, 6))
    print(data.find(b"", 7, 100), data.find(b"", 3, 2))
    print(data.find(b"", None, None), data.find(b"bc", Bound(1), Bound(3)))
    print(data.find(b"", 10 ** 100, None), data.find(b"bc", -(10 ** 100), 10 ** 100))
    print(data.find(b"", Bound(10 ** 100), None))
    print(data.find(98, 2, 6), data.find(98, 0, 2))
    try:
        data.find(b"a", 1.5, 6)
    except TypeError:
        print("bad bound")
    for needle in [-1, 256, 10 ** 100]:
        try:
            data.find(needle, 0, 6)
        except ValueError:
            print("bad byte")
def main():
    check(b"abcabc")
    check(bytearray(b"abcabc"))
main()
'''
    output = _compile(tmp_path, source, pcc_py_runtime_archive)
    expected = subprocess.run([sys.executable, str(tmp_path / "find_start.py")],
                              capture_output=True, text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    for gc in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f"GC{gc}: {result.stderr}"
        assert result.stdout == expected.stdout, f"GC{gc}: {result.stdout}"
