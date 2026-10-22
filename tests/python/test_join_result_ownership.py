"""Join results carry an owner through raw-ABI Python borrowing consumers."""

import os
import subprocess

import pytest


@pytest.mark.parametrize("kind", ["bytes", "str", "bytearray"])
def test_join_results_release_on_rebind_argument_and_attribute_store(
    tmp_path, pcc_runtime_archive, python_program_compiler, monkeypatch, kind,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "join_owner.py"
    source.write_text('''import gc
from pcc.extern import extern, c_int64
heap = extern("pcc_os_heap_in_use_bytes", (), c_int64)
class Box:
    def __init__(self):
        self.value = None
def use(value):
    assert len(value) == 65536
def make(parts):
    return SEP.join(parts)
def scan(count: int, parts):
    box = Box()
    for index in range(count):
        local = SEP.join(parts)
        use(local)
        use(SEP.join(parts))
        use(make(parts))
        box.value = SEP.join(parts)
        use(box.value)
def main():
    chunk = CHUNK * 32768
    parts = [chunk, chunk]
    scan(32, parts)
    gc.collect()
    before = heap()
    scan(64, parts)
    gc.collect()
    print(heap() - before)
    assert parts[0] is chunk
    assert parts[1] is chunk
main()
'''.replace("SEP", '""' if kind == "str" else ('bytearray(b"")' if kind == "bytearray" else 'b""')).replace(
        "CHUNK", '"x"' if kind == "str" else 'b"x"'))
    binary = tmp_path / "join_owner"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30,
                             env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert run.returncode == 0, (backend, run.stdout, run.stderr)
        if backend == 0:
            assert int(run.stdout) < 16384, run.stdout
