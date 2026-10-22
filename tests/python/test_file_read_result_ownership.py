"""Native file read results own their temporary buffers at call boundaries."""

import os
import subprocess

import pytest


@pytest.mark.parametrize("method", ["read()", "read(32768)", "readline()", "readline(32768)"])
def test_file_read_temporaries_release_after_calls_and_errors(
    tmp_path, pcc_runtime_archive, python_program_compiler, monkeypatch, method,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"x" * 32768)
    source = tmp_path / "read_owner.py"
    source.write_text('''import gc
from pcc.extern import extern, c_int64
heap = extern("pcc_os_heap_in_use_bytes", (), c_int64)
destroyed = 0
class Payload:
    def __init__(self, data):
        self.data = data
    def __del__(self):
        global destroyed
        destroyed += 1
def decode(data):
    assert len(data) == 32768
    return Payload(data)
def fail(data):
    assert len(data) == 32768
    raise ValueError("consumer")
def scan(count: int):
    values = []
    for index in range(count):
        with open(PATH, "rb") as stream:
            values.append(decode(stream.METHOD))
    values.clear()
def main():
    scan(128)
    gc.collect()
    before = heap()
    scan(128)
    gc.collect()
    print(heap() - before, destroyed)
    caught = 0
    for index in range(16):
        with open(PATH, "rb") as stream:
            try:
                fail(stream.METHOD)
            except ValueError:
                caught += 1
    with open(PATH, "rb") as stream:
        stream.close()
        try:
            stream.METHOD
        except ValueError:
            caught += 1
    assert caught == 17
main()
'''.replace("PATH", repr(str(payload))).replace("METHOD", method))
    binary = tmp_path / "read_owner"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        growth, destroyed = map(int, result.stdout.split())
        assert destroyed == 256, (backend, result.stdout)
        if backend == 0:
            assert growth < 16384, growth
