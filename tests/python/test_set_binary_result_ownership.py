"""Set operators return owned objects in assignments and discarded expressions."""

import os
import subprocess

import pytest


@pytest.mark.parametrize("operator", ["-", "|", "&", "^"])
@pytest.mark.parametrize("use", ["assign", "discard"])
def test_native_set_binary_result_releases_elements(
    tmp_path, monkeypatch, python_program_compiler, pcc_runtime_archive,
    operator, use,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    operation = "result = left OP right\n    assert len(result) == 300\n    assert result is not left" if use == "assign" else "left OP right"
    source = tmp_path / "set_result_owner.py"
    source.write_text('''import gc
from pcc.extern import extern, c_int64
heap = extern("pcc_os_heap_in_use_bytes", (), c_int64)
created = 0
destroyed = 0
class Item:
    def __init__(self, value):
        global created
        self.value = value
        self.payload = b"x" * 256
        created += 1
    def __del__(self):
        global destroyed
        destroyed += 1
def run():
    left = set()
    for i in range(300):
        left.add(Item(i))
    right = RIGHT
    OPERATION
    assert len(left) == 300
    gc.collect()
    assert created - destroyed == 300
def main():
    run()
    gc.collect()
    gc.collect()
    assert destroyed == created == 300
    before = heap()
    run()
    gc.collect()
    gc.collect()
    assert destroyed == created == 600
    print(created, destroyed, heap() - before)
main()
'''.replace("RIGHT", "left.copy()" if operator == "&" else "set()").replace("OPERATION", operation.replace("OP", operator)))
    binary = tmp_path / "set_result_owner"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        ran = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)),
        )
        assert ran.returncode == 0, (backend, ran.stdout, ran.stderr)
        created, destroyed, growth = map(int, ran.stdout.split())
        assert created == destroyed == 600
        if backend == 0:
            assert growth < 16384, growth
