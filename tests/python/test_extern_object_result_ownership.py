"""Explicit Python object results from extern calls transfer a new owner."""

import os
import subprocess

from pcc.py_frontend.pipeline import compile_python


def test_extern_object_results_are_consumed(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / "extern_objects.py"
    source.write_text('''
from pcc.extern import extern, c_obj, c_ptr, c_int64
from pcc.unsafe import null
make_bytes = extern("py_bytes_new", (c_ptr, c_int64), c_obj)
live_bytes = extern("pcc_os_heap_in_use_bytes", (), c_int64)
def allocate() -> bytes:
    return make_bytes(null(), 4096)
def scan(count: int) -> int:
    total = 0
    for index in range(count):
        total += len(allocate())
        total += len(make_bytes(null(), 4096))
    return total
def main():
    scan(8)
    before = live_bytes()
    first = scan(200)
    middle = live_bytes()
    second = scan(200)
    print(first, second, middle - before, live_bytes() - middle)
main()
''', encoding="utf-8")
    output = tmp_path / "extern_objects"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, f"GC{backend}: {result.stderr}"
        first, second, growth1, growth2 = map(int, result.stdout.split())
        assert first == second == 200 * 8192
        if backend == 0:
            assert growth1 < 65536 and growth2 < 65536, result.stdout
