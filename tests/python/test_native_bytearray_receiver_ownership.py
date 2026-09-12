"""Mutating statement consumers must release NEW receiver reads."""

import os
import subprocess

import pytest

from pcc.py_frontend.pipeline import compile_python


@pytest.mark.parametrize("method,argument,growth,receiver", [
    ("extend", "b''", 0, "attribute"),
    ("append", "120", 64, "attribute"),
    ("extend", "b''", 0, "subscript"),
    ("append", "120", 64, "subscript"),
    ("insert", "0, 120", 64, "subscript"),
    ("extend", "b''", 0, "local"),
    ("append", "120", 64, "local"),
    ("insert", "0, 120", 64, "local"),
    pytest.param("extend", "b''", 0, "parameter", marks=pytest.mark.xfail(
        strict=True,
        reason="Owned bytearray replacements of a borrowed parameter are not tracked",
    )),
])
def test_bytearray_receiver_does_not_retain_old_buffers(
    tmp_path, pcc_py_runtime_archive, method, argument, growth, receiver,
):
    target, annotation, initial = {
        "attribute": ("buffer.data", "object", "Buffer()"),
        "subscript": ("buffer[0]", "list[bytearray]", "[bytearray(b'x' * 65536)]"),
        "local": ("data", "bytearray", "bytearray(b'x' * 65536)"),
        "parameter": ("buffer", "bytearray", "bytearray(b'x' * 65536)"),
    }[receiver]
    local_setup = "    data = bytearray(b'x' * 65536)\n" if receiver == "local" else ""
    source = tmp_path / "receiver.py"
    source.write_text(f'''
from pcc.extern import c_int64, extern
live_bytes = extern("pcc_os_heap_in_use_bytes", (), c_int64)
class Buffer:
    def __init__(self):
        self.data = bytearray(b"x" * 65536)
def grow(buffer: {annotation}):
{local_setup}\
    before = live_bytes()
    index = 0
    while index < 64:
        {target}.{method}({argument})
        index += 1
    after = live_bytes()
    print(len({target}), after - before)
    print({target}[0], {target}[-1])
def main():
    buffer = {initial}
    grow(buffer)
main()
''', encoding="utf-8")
    output = tmp_path / "receiver"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        result = subprocess.run(
            [str(output)], capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert result.returncode == 0, f"GC{backend}: {result.stderr}"
        size, retained, first, last = map(int, result.stdout.split())
        assert (size, first, last) == (65536 + growth, 120, 120)
        # GC0 exposes a leaked reference independently of collection policy.
        # Other collectors execute the same accesses to detect stale releases.
        if backend == 0:
            assert retained < 262144, (method, retained)
